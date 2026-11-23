"""Comprehensive test suite for interactive dry-run preview and apply execution."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
TESTS_DIR = ROOT_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from api_test_support import InventoryApi
from configure_github_repo import ApiError
from setup_github_repo import run_policy
from scripts.interactive import Session
from scripts.interactive_apply import (
    execute_dry_run,
    format_dry_run_summary,
    display_dry_run_summary,
    prompt_apply_confirmation,
    execute_apply,
    format_apply_summary,
    run_interactive_apply,
)
from scripts.interactive import run_interactive_workflow
from scripts.cli import run_interactive


class TestDryRunExecution(unittest.TestCase):
    """Test executing dry-run using run_policy(apply=False) without sending writes."""

    def test_dry_run_executes_run_policy_with_apply_false_without_writes(self):
        """Dry-run calculates changes and never issues writes (POST/PUT/PATCH/DELETE)."""
        api = InventoryApi()
        policy = {
            "version": 1,
            "repository": "acme/project",
            "stages": [
                {"name": "features", "enable": ["issues", "wiki"]},
                {"name": "actions"},
            ],
        }

        result = execute_dry_run(api, "acme/project", policy)

        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["repository"], "acme/project")
        self.assertEqual(api.writes, [], "Dry-run must not perform any write requests")
        self.assertTrue(len(result["changes"]) >= 2)
        for change in result["changes"]:
            self.assertEqual(change["status"], "planned")
            self.assertIn("stage", change)
            self.assertIn("operation", change)
            self.assertIn("path", change)

    def test_dry_run_handles_preflight_blocked_status(self):
        """When preflight encounters an ApiError, status is reported as blocked without writes."""
        class BlockedApi(InventoryApi):
            def request(self, method, path, payload=None):
                if method == "GET" and path == "/repos/acme/project/actions/permissions":
                    raise ApiError("HTTP 403 Forbidden")
                return super().request(method, path, payload)

        api = BlockedApi()
        policy = {
            "version": 1,
            "repository": "acme/project",
            "stages": [{"name": "actions"}],
        }

        result = execute_dry_run(api, "acme/project", policy)

        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(api.writes, [])
        self.assertTrue(any(c["status"] == "blocked" for c in result["changes"]))


class TestDryRunSummaryDisplay(unittest.TestCase):
    """Test formatting and display of user-friendly dry-run proposed changes summary."""

    def test_format_dry_run_summary_displays_planned_changes(self):
        """Summary contains header, stages, operations, paths, and total count."""
        dry_run_result = {
            "mode": "dry-run",
            "repository": "acme/project",
            "changes": [
                {
                    "stage": "features",
                    "operation": "PATCH",
                    "path": "/repos/acme/project",
                    "current": {"has_issues": False, "has_wiki": False},
                    "desired": {"has_issues": True, "has_wiki": True},
                    "reason": "selected by policy",
                    "status": "planned",
                },
                {
                    "stage": "actions",
                    "operation": "PUT",
                    "path": "/repos/acme/project/actions/permissions",
                    "current": {"enabled": False},
                    "desired": {"enabled": True, "allowed_actions": "all"},
                    "reason": "selected by policy",
                    "status": "planned",
                },
            ],
        }

        summary = format_dry_run_summary(dry_run_result)

        self.assertIn("Dry-Run", summary)
        self.assertIn("acme/project", summary)
        self.assertIn("features", summary)
        self.assertIn("PATCH /repos/acme/project", summary)
        self.assertIn("actions", summary)
        self.assertIn("PUT /repos/acme/project/actions/permissions", summary)
        self.assertIn("planned", summary)
        self.assertIn("Total calculated changes: 2", summary)

    def test_format_dry_run_summary_empty_changes(self):
        """When changes list is empty, summary indicates repository is already up to date."""
        dry_run_result = {
            "mode": "dry-run",
            "repository": "acme/project",
            "changes": [],
        }

        summary = format_dry_run_summary(dry_run_result)

        self.assertIn("acme/project", summary)
        self.assertTrue("no changes" in summary.lower() or "already" in summary.lower())

    def test_format_dry_run_summary_blocked_changes(self):
        """When a change status is blocked, summary highlights blocked status and reason."""
        dry_run_result = {
            "mode": "dry-run",
            "repository": "acme/project",
            "changes": [
                {
                    "stage": "security",
                    "reason": "HTTP 403 Forbidden",
                    "status": "blocked",
                }
            ],
        }

        summary = format_dry_run_summary(dry_run_result)

        self.assertIn("security", summary)
        self.assertIn("blocked", summary.lower())
        self.assertIn("HTTP 403 Forbidden", summary)

    def test_display_dry_run_summary_outputs_to_stream(self):
        """display_dry_run_summary writes formatted text to output stream."""
        dry_run_result = {
            "mode": "dry-run",
            "repository": "acme/project",
            "changes": [
                {
                    "stage": "actions",
                    "operation": "PUT",
                    "path": "/repos/acme/project/actions/permissions",
                    "status": "planned",
                }
            ],
        }
        out_stream = io.StringIO()

        display_dry_run_summary(dry_run_result, out_stream=out_stream)

        self.assertIn("acme/project", out_stream.getvalue())
        self.assertIn("actions", out_stream.getvalue())


class TestConfirmationPrompt(unittest.TestCase):
    """Test confirmation prompt (`Apply these changes? (y/N)`) defaulting to No."""

    def test_confirmation_prompt_default_is_no(self):
        """Empty input defaults to False (No) and shows [y/N] indicator."""
        in_stream = io.StringIO("\n")
        out_stream = io.StringIO()

        confirmed = prompt_apply_confirmation(in_stream=in_stream, out_stream=out_stream)

        self.assertFalse(confirmed)
        prompt_text = out_stream.getvalue()
        self.assertIn("Apply these changes?", prompt_text)
        self.assertIn("[y/N]", prompt_text)

    def test_confirmation_prompt_explicit_no(self):
        """Entering 'n' or 'no' returns False."""
        for val in ("n\n", "no\n", "N\n", "NO\n"):
            with self.subTest(val=val.strip()):
                in_stream = io.StringIO(val)
                out_stream = io.StringIO()
                self.assertFalse(prompt_apply_confirmation(in_stream=in_stream, out_stream=out_stream))

    def test_confirmation_prompt_explicit_yes(self):
        """Entering 'y' or 'yes' returns True."""
        for val in ("y\n", "yes\n", "Y\n", "YES\n"):
            with self.subTest(val=val.strip()):
                in_stream = io.StringIO(val)
                out_stream = io.StringIO()
                self.assertTrue(prompt_apply_confirmation(in_stream=in_stream, out_stream=out_stream))

    def test_confirmation_prompt_eof_returns_false(self):
        """EOF on input returns False."""
        in_stream = io.StringIO("")
        out_stream = io.StringIO()

        confirmed = prompt_apply_confirmation(in_stream=in_stream, out_stream=out_stream)

        self.assertFalse(confirmed)


class TestInteractiveApplyRunner(unittest.TestCase):
    def test_confirmed_dry_run_does_not_apply_new_remote_change(self):
        class ChangedOnConfirmation(io.StringIO):
            def readline(self, *args):
                api.repo["has_issues"] = False
                return super().readline(*args)

        api = InventoryApi()
        api.repo["has_issues"] = True
        session = Session(api=api, repository="acme/project",
                          in_stream=ChangedOnConfirmation("y\n"),
                          out_stream=io.StringIO(), err_stream=io.StringIO())
        policy = {"version": 1, "stages": [
            {"name": "features", "enable": ["issues", "wiki"]}]}

        self.assertEqual(run_interactive_apply(session, policy), 2)
        self.assertEqual(api.writes, [])
        self.assertIn("changed", session.out_stream.getvalue().lower())

    """Test interactive dry-run preview, confirmation, and apply runner."""

    def test_rejection_aborts_cleanly_without_writes(self):
        """Declining confirmation returns 0 and performs zero write requests."""
        api = InventoryApi()
        in_stream = io.StringIO("n\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/project",
            api=api,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        policy = {
            "version": 1,
            "repository": "acme/project",
            "stages": [{"name": "features", "enable": ["issues"]}],
        }

        exit_code = run_interactive_apply(session, policy)

        self.assertEqual(exit_code, 0)
        self.assertEqual(api.writes, [], "No writes should occur when confirmation is rejected")
        output = out_stream.getvalue()
        self.assertIn("cancelled", output.lower())

    def test_default_no_aborts_cleanly_without_writes(self):
        """Empty input (default No) returns 0 and performs zero write requests."""
        api = InventoryApi()
        in_stream = io.StringIO("\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/project",
            api=api,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        policy = {
            "version": 1,
            "repository": "acme/project",
            "stages": [{"name": "features", "enable": ["issues"]}],
        }

        exit_code = run_interactive_apply(session, policy)

        self.assertEqual(exit_code, 0)
        self.assertEqual(api.writes, [])

    def test_confirmation_executes_apply_and_logs_results(self):
        """Confirming with 'y' executes apply, performs writes, logs changes and summary, returns 0."""
        api = InventoryApi()
        in_stream = io.StringIO("y\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/project",
            api=api,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        policy = {
            "version": 1,
            "repository": "acme/project",
            "stages": [
                {"name": "features", "enable": ["issues", "wiki"]},
                {"name": "actions"},
            ],
        }

        exit_code = run_interactive_apply(session, policy)

        self.assertEqual(exit_code, 0)
        self.assertTrue(len(api.writes) >= 2, "Apply must execute write requests")
        output = out_stream.getvalue()
        self.assertIn("verified", output.lower())
        self.assertIn("summary", output.lower())
        self.assertIn("Total changes: 3", output)
        self.assertIn("Verified: 3", output)

    def test_execute_apply_blocks_writes_when_accepted_plan_is_none(self):
        """Calling execute_apply without an accepted Dry-Run raises ApiError and blocks writes."""
        api = InventoryApi()
        policy = {"version": 1, "repository": "acme/project", "stages": [{"name": "features", "enable": ["issues"]}]}
        with self.assertRaises(ApiError) as ctx:
            execute_apply(api, "acme/project", policy, accepted_plan=None)
        self.assertIn("Dry-Run", str(ctx.exception))
        self.assertEqual(api.writes, [])

    def test_user_confirmation_saves_accepted_dry_run_json(self):
        """When user confirms apply, the accepted Dry-Run JSON is saved to session.dry_run_path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dry_run_file = Path(tmpdir) / ".local" / "setup-dry-run.json"
            api = InventoryApi()
            in_stream = io.StringIO("y\n")
            out_stream = io.StringIO()
            session = Session(
                repository="acme/project",
                api=api,
                in_stream=in_stream,
                out_stream=out_stream,
            )
            session.dry_run_path = dry_run_file
            policy = {
                "version": 1,
                "repository": "acme/project",
                "stages": [{"name": "features", "enable": ["issues"]}],
            }

            exit_code = run_interactive_apply(session, policy)
            self.assertEqual(exit_code, 0)
            self.assertTrue(dry_run_file.exists(), "Accepted Dry-Run JSON file must be saved on confirmation")
            saved_data = json.loads(dry_run_file.read_text(encoding="utf-8"))
            self.assertEqual(saved_data.get("repository"), "acme/project")
            self.assertEqual(saved_data.get("mode"), "dry-run")
            self.assertIn("Accepted Dry-Run result saved to", out_stream.getvalue())

    def test_declining_confirmation_does_not_save_dry_run_json(self):
        """When user declines confirmation, dry_run_path is not written."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dry_run_file = Path(tmpdir) / ".local" / "setup-dry-run.json"
            api = InventoryApi()
            in_stream = io.StringIO("n\n")
            out_stream = io.StringIO()
            session = Session(
                repository="acme/project",
                api=api,
                in_stream=in_stream,
                out_stream=out_stream,
            )
            session.dry_run_path = dry_run_file
            policy = {
                "version": 1,
                "repository": "acme/project",
                "stages": [{"name": "features", "enable": ["issues"]}],
            }

            exit_code = run_interactive_apply(session, policy)
            self.assertEqual(exit_code, 0)
            self.assertFalse(dry_run_file.exists(), "Dry-Run file must not be saved when apply is cancelled")

    def test_no_changes_needed_exits_0_without_prompting(self):
        """When dry-run detects zero planned changes, informs user and exits 0 without writes."""
        api = InventoryApi()
        api.repo["has_issues"] = True
        in_stream = io.StringIO("y\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/project",
            api=api,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        policy = {
            "version": 1,
            "repository": "acme/project",
            "stages": [{"name": "features", "enable": ["issues"]}],
        }

        exit_code = run_interactive_apply(session, policy)

        self.assertEqual(exit_code, 0)
        self.assertEqual(api.writes, [])
        output = out_stream.getvalue()
        self.assertTrue("no changes" in output.lower() or "already" in output.lower())

    def test_classify_change_diff_inspect_operation_is_unchanged(self):
        """Inspection records (case-insensitive) classify as unchanged unless pending/blocked."""
        from scripts.interactive_apply import classify_change_diff

        self.assertEqual(
            classify_change_diff({"stage": "issue-state-gate", "operation": "inspect", "status": "planned"}),
            "unchanged",
        )
        self.assertEqual(
            classify_change_diff({"stage": "issue-state-gate", "operation": "INSPECT", "status": "planned"}),
            "unchanged",
        )
        self.assertEqual(
            classify_change_diff({"stage": "issue-state-gate", "operation": "inspect", "status": "pending"}),
            "pending",
        )
        self.assertNotEqual(
            classify_change_diff({"stage": "issue-state-gate", "operation": "inspect", "status": "blocked"}),
            "unchanged",
        )

    def test_converged_inspect_only_dry_run_exits_0_without_prompting(self):
        """Dry-run with only inspect/unchanged records and no setup PR exits 0 without prompting."""
        api = InventoryApi()
        out_stream = io.StringIO()
        session = Session(
            repository="acme/project",
            api=api,
            in_stream=io.StringIO("n\n"),
            out_stream=out_stream,
        )
        policy = {"version": 1, "repository": "acme/project", "stages": [{"name": "issue-state-gate"}]}
        converged_dry_run = {
            "mode": "dry-run",
            "repository": "acme/project",
            "changes": [
                {
                    "stage": "issue-state-gate",
                    "scope": "acme/project",
                    "operation": "inspect",
                    "status": "planned",
                    "reason": "Read and verify the active Issue State Gate rule without changing repository protections.",
                }
            ],
        }

        with patch("scripts.interactive_apply.execute_dry_run", return_value=converged_dry_run), \
             patch("scripts.interactive_apply.prompt_apply_confirmation", side_effect=AssertionError("should not prompt")):
            exit_code = run_interactive_apply(session, policy)

        self.assertEqual(exit_code, 0)
        self.assertEqual(api.writes, [])
        self.assertIn("Repository is already configured as declared. No changes to apply.\n", out_stream.getvalue())

    def test_inspect_only_with_setup_pr_still_prompts(self):
        """Dry-run with only inspect records still prompts when setup_pull_request is present."""
        api = InventoryApi()
        out_stream = io.StringIO()
        session = Session(
            repository="acme/project",
            api=api,
            in_stream=io.StringIO("n\n"),
            out_stream=out_stream,
        )
        policy = {"version": 1, "repository": "acme/project", "stages": [{"name": "issue-state-gate"}]}
        dry_run_with_pr = {
            "mode": "dry-run",
            "repository": "acme/project",
            "changes": [
                {
                    "stage": "issue-state-gate",
                    "scope": "acme/project",
                    "operation": "inspect",
                    "status": "planned",
                }
            ],
            "setup_pull_request": {"branch": "chore/setup", "title": "Setup workflows"},
        }

        with patch("scripts.interactive_apply.execute_dry_run", return_value=dry_run_with_pr), \
             patch("scripts.interactive_apply.prompt_apply_confirmation", return_value=False) as mock_prompt:
            exit_code = run_interactive_apply(session, policy)

        self.assertEqual(exit_code, 0)
        mock_prompt.assert_called_once()
        self.assertIn("Apply cancelled.", out_stream.getvalue())

    def test_unverified_change_status_returns_2(self):
        """If convergence check fails and change is unverified, exits 2 and logs status."""
        class IgnoringApi(InventoryApi):
            def request(self, method, path, payload=None):
                if method == "PATCH" and path == "/repos/acme/project":
                    self.writes.append((method, path, payload))
                    # Return 200 without actually updating repo state
                    return 200, self.repo
                return super().request(method, path, payload)

        api = IgnoringApi()
        in_stream = io.StringIO("y\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/project",
            api=api,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        policy = {
            "version": 1,
            "repository": "acme/project",
            "stages": [{"name": "features", "enable": ["issues"]}],
        }

        exit_code = run_interactive_apply(session, policy)

        self.assertEqual(exit_code, 2)
        output = out_stream.getvalue()
        self.assertIn("unverified", output.lower())

    def test_api_error_during_apply_writes_returns_1(self):
        """A failed first write stays in the structured Apply summary."""
        class FailingApi(InventoryApi):
            def request(self, method, path, payload=None):
                if method == "PATCH" and path == "/repos/acme/project":
                    raise ApiError("HTTP 500 Internal Server Error")
                return super().request(method, path, payload)

        api = FailingApi()
        in_stream = io.StringIO("y\n")
        out_stream = io.StringIO()
        err_stream = io.StringIO()
        session = Session(
            repository="acme/project",
            api=api,
            in_stream=in_stream,
            out_stream=out_stream,
            err_stream=err_stream,
        )
        policy = {
            "version": 1,
            "repository": "acme/project",
            "stages": [{"name": "features", "enable": ["issues"]}],
        }

        exit_code = run_interactive_apply(session, policy)

        self.assertEqual(exit_code, 2)
        self.assertIn("HTTP 500", out_stream.getvalue())
        self.assertIn("Recovery:", out_stream.getvalue())


class TestFullInteractiveWorkflow(unittest.TestCase):
    """Test full interactive workflow from questionnaires to dry-run and apply."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.temp_dir, ignore_errors=True))

    def test_full_workflow_confirm_and_apply(self):
        """Complete workflow: state detection -> features -> actions/security -> workflows -> policy assembly ->
        dry-run -> confirm -> apply."""
        # Setup inputs:
        # 1-click: c (customize)
        # customization menu: 1 (features)
        # features multichoice: 1 (Issues)
        # customization menu: 6 (Review & Proceed to Dry-Run)
        # apply confirmation: y
        inputs = "c\n1\n1\n6\ny\n"
        in_stream = io.StringIO(inputs)
        out_stream = io.StringIO()

        api = InventoryApi()
        temp_policy_path = Path(self.temp_dir) / ".local" / "setup-policy.json"

        with patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", temp_policy_path):
            session = Session(
                repository="acme/project",
                api=api,
                in_stream=in_stream,
                out_stream=out_stream,
                resume_mode=False,
            )

            exit_code = run_interactive_workflow(session)

        self.assertEqual(exit_code, 0)
        # Policy was persisted
        self.assertTrue(temp_policy_path.exists())
        policy = json.loads(temp_policy_path.read_text(encoding="utf-8"))
        self.assertEqual(policy["repository"], "acme/project")

        # Writes were executed
        self.assertTrue(len(api.writes) >= 2)
        output = out_stream.getvalue()
        self.assertIn("Dry-Run", output)
        self.assertIn("Apply Summary", output)
        self.assertIn("verified", output.lower())

    def test_full_workflow_rejection_at_confirmation(self):
        """Workflow persists policy but performs no writes when user rejects apply confirmation."""
        # Setup inputs:
        # 1-click: c (customize)
        # customization menu: 1 (features)
        # features multichoice: 1 (Issues)
        # customization menu: 6 (Review & Proceed to Dry-Run)
        # apply confirmation: n (Rejection!)
        inputs = "c\n1\n1\n6\nn\n"
        in_stream = io.StringIO(inputs)
        out_stream = io.StringIO()

        api = InventoryApi()
        temp_policy_path = Path(self.temp_dir) / ".local" / "setup-policy.json"

        with patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", temp_policy_path):
            session = Session(
                repository="acme/project",
                api=api,
                in_stream=in_stream,
                out_stream=out_stream,
                resume_mode=False,
            )

            exit_code = run_interactive_workflow(session)

        self.assertEqual(exit_code, 0)
        # Policy was persisted for later reuse
        self.assertTrue(temp_policy_path.exists())
        # Zero writes performed!
        self.assertEqual(api.writes, [])
        output = out_stream.getvalue()
        self.assertIn("cancelled", output.lower())

    def test_full_workflow_no_stages_selected(self):
        """When user cancels customization, exits cleanly with 0 and no writes."""
        # 1-click: c (customize)
        # customization menu: 7 (Cancel)
        inputs = "c\n7\n"
        in_stream = io.StringIO(inputs)
        out_stream = io.StringIO()

        api = InventoryApi()
        session = Session(
            repository="acme/project",
            api=api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
        )

        exit_code = run_interactive_workflow(session)

        self.assertEqual(exit_code, 0)
        self.assertEqual(api.writes, [])
        self.assertTrue("cancelled" in out_stream.getvalue().lower())

    def test_run_interactive_cli_integration(self):
        """cli.run_interactive initiates full flow and returns exit status."""
        api = InventoryApi()
        inputs = "c\n1\n1\n6\ny\n"
        in_stream = io.StringIO(inputs)
        out_stream = io.StringIO()

        temp_policy_path = Path(self.temp_dir) / ".local" / "setup-policy.json"

        mock_session = Session(
            repository="acme/project",
            api=api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
        )

        with patch("scripts.cli.init_interactive_session", return_value=mock_session), \
             patch("scripts.cli.run_state_detection", return_value=True), \
             patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", temp_policy_path):
            exit_code = run_interactive(
                in_stream=in_stream,
                out_stream=out_stream,
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(len(api.writes) >= 2)


class TestDryRunGuardedApplyContract(unittest.TestCase):
    """Verify strict contract enforcement for issue #121 (P1.6)."""

    def test_run_policy_dry_run_includes_policy_sha256(self):
        """execute_dry_run and run_policy(apply=False) must include policy_sha256."""
        api = InventoryApi()
        policy = {"version": 1, "repository": "acme/project", "stages": [{"name": "features", "enable": ["issues"]}]}
        result = run_policy(api, "acme/project", policy, apply=False)
        self.assertIn("policy_sha256", result)
        self.assertEqual(len(result["policy_sha256"]), 64)

    def test_run_policy_apply_blocks_writes_when_accepted_dry_run_is_none(self):
        """run_policy(apply=True) without accepted_dry_run must refuse writes and return blocked status."""
        api = InventoryApi()
        policy = {"version": 1, "repository": "acme/project", "stages": [{"name": "features", "enable": ["issues"]}]}
        result = run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=None)
        self.assertEqual(api.writes, [], "Direct setting writes without an accepted Dry-Run must be blocked")
        self.assertEqual(result["changes"][0]["status"], "blocked")
        self.assertIn("Dry-Run", result["changes"][0]["reason"])

    def test_run_policy_apply_blocks_writes_when_accepted_dry_run_is_corrupted(self):
        """run_policy(apply=True) with a non-dict or corrupted accepted_dry_run must refuse writes."""
        api = InventoryApi()
        policy = {"version": 1, "repository": "acme/project", "stages": [{"name": "features", "enable": ["issues"]}]}
        result = run_policy(api, "acme/project", policy, apply=True, accepted_dry_run="corrupted-string")
        self.assertEqual(api.writes, [], "Direct setting writes with corrupted Dry-Run must be blocked")
        self.assertEqual(result["changes"][0]["status"], "blocked")
        self.assertIn("Dry-Run", result["changes"][0]["reason"])

    def test_run_policy_apply_blocks_writes_when_policy_sha256_mismatches(self):
        """run_policy(apply=True) with a tampered policy_sha256 must refuse writes."""
        api = InventoryApi()
        policy = {"version": 1, "repository": "acme/project", "stages": [{"name": "features", "enable": ["issues"]}]}
        dry_run = run_policy(api, "acme/project", policy, apply=False)
        dry_run["policy_sha256"] = "0" * 64
        result = run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(api.writes, [], "Writes must be refused when policy_sha256 does not match")
        self.assertEqual(result["changes"][0]["status"], "blocked")

    def test_interactive_dry_run_saved_json_matches_headless_apply_schema(self):
        """Accepted Dry-Run JSON saved by interactive setup is validated cleanly by headless apply."""
        with tempfile.TemporaryDirectory() as tmpdir:
            policy_file = Path(tmpdir) / "policy.json"
            dry_run_file = Path(tmpdir) / "setup-dry-run.json"
            policy = {"version": 1, "repository": "acme/project", "stages": [{"name": "features", "enable": ["issues"]}]}
            policy_file.write_text(json.dumps(policy), encoding="utf-8")

            api = InventoryApi()
            in_stream = io.StringIO("y\n")
            out_stream = io.StringIO()
            session = Session(
                repository="acme/project",
                api=api,
                in_stream=in_stream,
                out_stream=out_stream,
            )
            session.dry_run_path = dry_run_file

            exit_code = run_interactive_apply(session, policy)
            self.assertEqual(exit_code, 0)
            self.assertTrue(dry_run_file.is_file())

            # Read back and verify headless schema requirements:
            # - mode == "dry-run"
            # - repository == target_repo
            # - policy_sha256 matches
            # - changes is list
            saved_dry_run = json.loads(dry_run_file.read_text(encoding="utf-8"))
            self.assertEqual(saved_dry_run.get("mode"), "dry-run")
            self.assertEqual(saved_dry_run.get("repository"), "acme/project")
            self.assertIn("policy_sha256", saved_dry_run)
            self.assertIsInstance(saved_dry_run.get("changes"), list)

            # Confirm output does not mention proscribed term "plan"
            output = out_stream.getvalue()
            self.assertNotIn("dry-run plan", output.lower())
            self.assertIn("dry-run result", output.lower())

            # Directly invoke run_headless_apply with the saved dry run file against a repo in pre-apply state
            fresh_api = InventoryApi()
            from scripts.headless import run_headless_apply
            with patch("scripts.headless.GitHubApi", return_value=fresh_api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "mock-token"}), \
                 redirect_stdout(io.StringIO()):
                headless_exit = run_headless_apply(
                    str(policy_file),
                    repo="acme/project",
                    apply=True,
                    dry_run_path=str(dry_run_file),
                )
            self.assertEqual(headless_exit, 0)


if __name__ == "__main__":
    unittest.main()
