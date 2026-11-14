"""Tests for state detection and inventory integration in interactive setup."""

import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.interactive import (
    Session,
    init_interactive_session,
    run_state_detection,
    format_inventory_summary,
    display_inventory_summary,
    has_existing_configuration,
    prompt_resume_choice,
    extract_defaults_from_inventory,
    clear_defaults,
)
from scripts.configure_github_repo import ApiError


def sample_inventory_data():
    """Return a representative inventory dictionary with active configurations."""
    return {
        "repository": {
            "full_name": "acme/rocket-app",
            "visibility": "public",
            "default_branch": "main",
        },
        "features": {
            "issues": True,
            "wiki": False,
            "projects": True,
        },
        "wiki_setup": {
            "initial_page": {"status": "unknown", "reason": "..."},
            "editing_restriction": {"status": "unsupported", "reason": "..."},
        },
        "merge": {
            "allow_merge_commit": True,
            "allow_squash_merge": True,
            "allow_rebase_merge": False,
            "delete_branch_on_merge": True,
            "allow_update_branch": True,
        },
        "actions": {
            "enabled": True,
            "allowed_actions": "selected",
        },
        "workflow_permissions": {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        },
        "security_and_analysis": {
            "secret_scanning": {"status": "enabled"},
            "secret_scanning_push_protection": {"status": "enabled"},
        },
        "vulnerability_alerts": {"status": 204},
        "automated_security_fixes": {"status": 200, "enabled": True},
        "private_vulnerability_reporting": {"status": 200, "enabled": True},
        "codeql": {"state": "configured"},
        "dependency_graph": {"status": "enabled"},
        "rulesets": [{"id": 1, "name": "Main Protection"}],
    }


def minimal_empty_inventory():
    """Return an inventory dictionary with no active features or security."""
    return {
        "repository": {
            "full_name": "acme/empty-app",
            "visibility": "private",
            "default_branch": "main",
        },
        "features": {
            "issues": False,
            "wiki": False,
            "projects": False,
        },
        "wiki_setup": {
            "initial_page": {"status": "unknown"},
            "editing_restriction": {"status": "unsupported"},
        },
        "merge": {
            "allow_merge_commit": False,
            "allow_squash_merge": False,
            "allow_rebase_merge": False,
            "delete_branch_on_merge": False,
            "allow_update_branch": False,
        },
        "actions": {"status": "unknown"},
        "workflow_permissions": {},
        "security_and_analysis": {
            "secret_scanning": {"status": "disabled"},
            "secret_scanning_push_protection": {"status": "disabled"},
        },
        "vulnerability_alerts": {"status": 404},
        "automated_security_fixes": {"status": 404},
        "private_vulnerability_reporting": {"status": "unknown"},
        "codeql": {"state": "not-configured"},
        "dependency_graph": {"status": "disabled"},
        "rulesets": [],
    }


class SessionStateAttributesTests(unittest.TestCase):
    """Tests for resume_mode, inventory, and defaults attributes on Session."""

    def test_session_state_attributes_defaults(self):
        """Session should default resume_mode to False, inventory to empty dict, and defaults to empty dict."""
        mock_api = MagicMock()
        session = Session(repository="owner/repo", api=mock_api)

        self.assertFalse(session.resume_mode)
        self.assertEqual(session.inventory, {})
        self.assertEqual(session.defaults, {})

    def test_session_state_attributes_custom_values(self):
        """Session should accept custom initial values for state detection attributes."""
        mock_api = MagicMock()
        sample_inv = {"features": {"issues": True}}
        sample_defaults = {"features": ["issues"]}

        session = Session(
            repository="owner/repo",
            api=mock_api,
            resume_mode=True,
            inventory=sample_inv,
            defaults=sample_defaults,
        )

        self.assertTrue(session.resume_mode)
        self.assertEqual(session.inventory, sample_inv)
        self.assertEqual(session.defaults, sample_defaults)


class InventorySummaryFormattingTests(unittest.TestCase):
    """Tests for formatting and displaying detected repository configuration summaries."""

    def test_format_summary_includes_all_key_sections(self):
        """Summary output must include sections for Features, Merge, Actions, and Security."""
        inv = sample_inventory_data()
        summary = format_inventory_summary(inv)

        self.assertIn("Features", summary)
        self.assertIn("Issues", summary)
        self.assertIn("Wiki", summary)
        self.assertIn("Projects", summary)

        self.assertIn("Merge", summary)
        self.assertIn("Squash", summary)
        self.assertIn("Rebase", summary)

        self.assertIn("Actions", summary)
        self.assertIn("Workflow Permissions", summary)

        self.assertIn("Security", summary)
        self.assertIn("Vulnerability Alerts", summary)
        self.assertIn("Secret Scanning", summary)
        self.assertIn("CodeQL", summary)

    def test_format_summary_reflects_active_and_disabled_values(self):
        """Summary accurately represents enabled/disabled statuses of detected settings."""
        inv = sample_inventory_data()
        summary = format_inventory_summary(inv)

        self.assertIn("Issues: Enabled", summary)
        self.assertIn("Wiki: Disabled", summary)
        self.assertIn("Projects: Enabled", summary)
        self.assertIn("Squash Merge: Enabled", summary)
        self.assertIn("Rebase Merge: Disabled", summary)
        self.assertIn("Workflow Permissions: read", summary)
        self.assertIn("Vulnerability Alerts: Enabled", summary)

    def test_display_summary_writes_to_out_stream(self):
        """display_inventory_summary writes formatted text to output stream."""
        inv = sample_inventory_data()
        out_stream = io.StringIO()

        display_inventory_summary(inv, out_stream=out_stream)
        output = out_stream.getvalue()

        self.assertIn("Features", output)
        self.assertIn("Security", output)
        self.assertTrue(output.endswith("\n"))


class ExistingConfigurationDetectionTests(unittest.TestCase):
    """Tests for detecting whether previous configurations exist."""

    def test_has_existing_configuration_true_when_settings_configured(self):
        """Returns True when repository has active features or security."""
        inv = sample_inventory_data()
        self.assertTrue(has_existing_configuration(inv))

    def test_has_existing_configuration_true_with_any_feature_enabled(self):
        """Returns True if at least one feature is enabled."""
        inv = minimal_empty_inventory()
        inv["features"]["issues"] = True
        self.assertTrue(has_existing_configuration(inv))

    def test_has_existing_configuration_false_when_all_empty_or_disabled(self):
        """Returns False when repository has no active settings configured."""
        inv = minimal_empty_inventory()
        self.assertFalse(has_existing_configuration(inv))

    def test_has_existing_configuration_handles_empty_dict(self):
        """Handles empty dict without error and returns False."""
        self.assertFalse(has_existing_configuration({}))


class ResumeChoicePromptTests(unittest.TestCase):
    """Tests for prompt_resume_choice asking user whether to resume or start fresh."""

    def test_prompt_resume_choice_selecting_option_1_returns_true(self):
        """Selecting option 1 (Resume) returns True."""
        in_stream = io.StringIO("1\n")
        out_stream = io.StringIO()

        choice = prompt_resume_choice(in_stream=in_stream, out_stream=out_stream)
        self.assertIs(choice, True)
        output = out_stream.getvalue()
        self.assertIn("Resume", output)
        self.assertIn("scratch", output.lower())

    def test_prompt_resume_choice_default_returns_true(self):
        """Pressing Enter to accept default returns True (resume)."""
        in_stream = io.StringIO("\n")
        out_stream = io.StringIO()

        choice = prompt_resume_choice(in_stream=in_stream, out_stream=out_stream)
        self.assertIs(choice, True)

    def test_prompt_resume_choice_selecting_option_2_returns_false(self):
        """Selecting option 2 (Start from scratch) returns False."""
        in_stream = io.StringIO("2\n")
        out_stream = io.StringIO()

        choice = prompt_resume_choice(in_stream=in_stream, out_stream=out_stream)
        self.assertIs(choice, False)

    def test_prompt_resume_choice_eof_returns_none(self):
        """EOF returns None."""
        in_stream = io.StringIO("")
        out_stream = io.StringIO()

        choice = prompt_resume_choice(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNone(choice)


class DefaultsExtractionTests(unittest.TestCase):
    """Tests for pre-populating defaults when resuming and clearing defaults when starting from scratch."""

    def test_extract_defaults_from_inventory(self):
        """Extracts features, merge, actions, and security defaults accurately."""
        inv = sample_inventory_data()
        defaults = extract_defaults_from_inventory(inv)

        self.assertIn("features", defaults)
        self.assertIn("merge", defaults)
        self.assertIn("actions", defaults)
        self.assertIn("security", defaults)

        self.assertEqual(sorted(defaults["features"]), ["issues", "projects"])
        self.assertTrue(defaults["merge"]["allow_squash_merge"])
        self.assertFalse(defaults["merge"]["allow_rebase_merge"])
        self.assertEqual(defaults["actions"]["default_workflow_permissions"], "read")
        self.assertEqual(defaults["actions"]["can_approve_pull_request_reviews"], False)

        self.assertIn("vulnerability-alerts", defaults["security"])
        self.assertIn("automated-security-fixes", defaults["security"])
        self.assertIn("secret-scanning", defaults["security"])
        self.assertIn("push-protection", defaults["security"])
        self.assertIn("codeql", defaults["security"])
        self.assertIn("private-vulnerability-reporting", defaults["security"])

    def test_clear_defaults_returns_empty_dict(self):
        """clear_defaults returns an empty dict for fresh setup."""
        cleared = clear_defaults()
        self.assertEqual(cleared, {})


class RunStateDetectionTests(unittest.TestCase):
    """Tests for run_state_detection orchestrator."""

    def test_run_state_detection_resuming_populates_session(self):
        """When user chooses to resume, session stores inventory, resume_mode=True, and pre-populated defaults."""
        inv = sample_inventory_data()
        mock_api = MagicMock()
        in_stream = io.StringIO("1\n")  # Choose resume
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        session = Session(
            repository="acme/rocket-app",
            api=mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            err_stream=err_stream,
        )

        with patch("scripts.interactive.query_inventory", return_value=inv) as mock_inv:
            success = run_state_detection(session)

            self.assertTrue(success)
            mock_inv.assert_called_once_with(mock_api, "acme/rocket-app")
            self.assertTrue(session.resume_mode)
            self.assertEqual(session.inventory, inv)
            self.assertIn("issues", session.defaults["features"])
            self.assertIn("projects", session.defaults["features"])
            self.assertNotIn("wiki", session.defaults["features"])

            output = out_stream.getvalue()
            self.assertIn("Features", output)
            self.assertIn("Security", output)
            self.assertIn("Resuming", output)

    def test_run_state_detection_scratch_clears_defaults(self):
        """When user chooses to start from scratch, session stores resume_mode=False and cleared defaults."""
        inv = sample_inventory_data()
        mock_api = MagicMock()
        in_stream = io.StringIO("2\n")  # Choose start from scratch
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        session = Session(
            repository="acme/rocket-app",
            api=mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            err_stream=err_stream,
        )

        with patch("scripts.interactive.query_inventory", return_value=inv):
            success = run_state_detection(session)

            self.assertTrue(success)
            self.assertFalse(session.resume_mode)
            self.assertEqual(session.inventory, inv)
            self.assertEqual(session.defaults, {})
            self.assertIn("scratch", out_stream.getvalue().lower())

    def test_run_state_detection_no_existing_config_defaults_to_fresh(self):
        """When repository has no previous configuration, defaults to fresh setup without prompting."""
        inv = minimal_empty_inventory()
        mock_api = MagicMock()
        in_stream = io.StringIO("")  # No prompt input needed
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        session = Session(
            repository="acme/empty-app",
            api=mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            err_stream=err_stream,
        )

        with patch("scripts.interactive.query_inventory", return_value=inv):
            success = run_state_detection(session)

            self.assertTrue(success)
            self.assertFalse(session.resume_mode)
            self.assertEqual(session.inventory, inv)
            self.assertEqual(session.defaults, {})
            self.assertIn("fresh", out_stream.getvalue().lower())

    def test_run_state_detection_api_error_reports_failure(self):
        """When query_inventory raises ApiError, outputs error message and returns False."""
        mock_api = MagicMock()
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        session = Session(
            repository="acme/broken-app",
            api=mock_api,
            out_stream=out_stream,
            err_stream=err_stream,
        )

        with patch("scripts.interactive.query_inventory", side_effect=ApiError("HTTP 500: Server error")):
            success = run_state_detection(session)

            self.assertFalse(success)
            self.assertIn("HTTP 500", err_stream.getvalue())
            self.assertIn("error", err_stream.getvalue().lower())

    def test_run_state_detection_eof_on_prompt_returns_false(self):
        """When EOF is encountered on resume prompt, returns False cleanly."""
        inv = sample_inventory_data()
        mock_api = MagicMock()
        in_stream = io.StringIO("")  # Immediate EOF
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        session = Session(
            repository="acme/rocket-app",
            api=mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            err_stream=err_stream,
        )

        with patch("scripts.interactive.query_inventory", return_value=inv):
            success = run_state_detection(session)
            self.assertFalse(success)


class InteractiveFlowWiringTests(unittest.TestCase):
    """Tests for wiring state detection into the interactive session flow."""

    def test_init_interactive_session_with_detect_state_runs_detection(self):
        """init_interactive_session with detect_state=True runs state detection."""
        in_stream = io.StringIO("acme/project\n1\n")
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        mock_api = MagicMock()
        mock_api.request.return_value = (200, {"id": 1, "name": "project"})
        inv = sample_inventory_data()

        with patch("scripts.interactive.query_inventory", return_value=inv) as mock_inv:
            session = init_interactive_session(
                token="ghp_test_token",
                api=mock_api,
                in_stream=in_stream,
                out_stream=out_stream,
                err_stream=err_stream,
                detect_state=True,
            )

            self.assertIsNotNone(session)
            self.assertTrue(session.resume_mode)
            self.assertEqual(session.inventory, inv)
            mock_inv.assert_called_once_with(mock_api, "acme/project")

    def test_run_interactive_wires_state_detection(self):
        """cli.run_interactive executes state detection after session initialization."""
        from scripts.cli import run_interactive

        mock_session = Session(
            repository="acme/project",
            api=MagicMock(),
        )

        with patch("scripts.cli.init_interactive_session", return_value=mock_session) as mock_init:
            with patch("scripts.cli.run_state_detection", return_value=True) as mock_detect:
                with patch("scripts.cli.run_interactive_workflow", return_value=0) as mock_workflow:
                    out_stream = io.StringIO()
                    code = run_interactive(out_stream=out_stream)

                    self.assertEqual(code, 0)
                    mock_init.assert_called_once()
                    mock_detect.assert_called_once_with(mock_session)
                    mock_workflow.assert_called_once_with(mock_session)

    def test_run_interactive_returns_one_if_state_detection_fails(self):
        """cli.run_interactive returns 1 when state detection returns False."""
        from scripts.cli import run_interactive

        mock_session = Session(
            repository="acme/project",
            api=MagicMock(),
        )

        with patch("scripts.cli.init_interactive_session", return_value=mock_session):
            with patch("scripts.cli.run_state_detection", return_value=False):
                out_stream = io.StringIO()
                code = run_interactive(out_stream=out_stream)

                self.assertEqual(code, 1)

    def test_run_state_detection_with_real_inventory_query(self):
        """End-to-end integration: run_state_detection calls real inventory function on api client."""
        def fake_request(method, path):
            if path == "/repos/acme/project":
                return 200, {
                    "full_name": "acme/project",
                    "visibility": "public",
                    "default_branch": "main",
                    "has_issues": True,
                    "has_wiki": False,
                    "has_projects": True,
                    "allow_merge_commit": True,
                    "allow_squash_merge": True,
                    "allow_rebase_merge": False,
                    "delete_branch_on_merge": True,
                    "allow_update_branch": True,
                    "security_and_analysis": {
                        "secret_scanning": {"status": "enabled"},
                        "secret_scanning_push_protection": {"status": "disabled"},
                    },
                }
            if path == "/repos/acme/project/actions/permissions":
                return 200, {"enabled": True, "allowed_actions": "all"}
            if path == "/repos/acme/project/actions/permissions/workflow":
                return 200, {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}
            if path == "/repos/acme/project/code-scanning/default-setup":
                return 200, {"state": "configured"}
            if path == "/repos/acme/project/vulnerability-alerts":
                return 204, None
            if path == "/repos/acme/project/automated-security-fixes":
                return 200, {"enabled": True}
            if path == "/repos/acme/project/private-vulnerability-reporting":
                return 200, {"enabled": True}
            if "rulesets" in path:
                return 200, []
            return 404, None

        mock_api = MagicMock()
        mock_api.request.side_effect = fake_request

        in_stream = io.StringIO("1\n")
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        session = Session(
            repository="acme/project",
            api=mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            err_stream=err_stream,
        )

        success = run_state_detection(session)

        self.assertTrue(success)
        self.assertTrue(session.resume_mode)
        self.assertIn("issues", session.defaults["features"])
        self.assertIn("vulnerability-alerts", session.defaults["security"])
        self.assertEqual(session.defaults["actions"]["default_workflow_permissions"], "read")


if __name__ == "__main__":
    unittest.main()
