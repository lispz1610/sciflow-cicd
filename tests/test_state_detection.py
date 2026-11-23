"""Tests for state detection and inventory integration in interactive setup."""

import hashlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    import yaml
except ImportError:
    yaml = None

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from scripts import setup_github_repo as setup_repo_module
from scripts.dependabot_generator import render_dependabot_config
from scripts.install_github_workflows import render as render_workflow
from scripts.review_policy import validate_policy as validate_review_policy
from scripts.review_workflow import render_workflow as render_review_workflow
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

    def test_has_existing_configuration_false_with_only_default_feature_enabled(self):
        """Returns False if only default GitHub repository features are enabled."""
        inv = minimal_empty_inventory()
        inv["features"]["issues"] = True
        self.assertFalse(has_existing_configuration(inv))

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

    def test_run_state_detection_omits_advisory_review_workflow(self):
        """Repository inventory displays CI and security status without Advisory Review workflow detection."""
        inv = sample_inventory_data()
        mock_api = MagicMock()
        in_stream = io.StringIO("2\n")  # start from scratch
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
            self.assertFalse(hasattr(session, "review_workflow") and session.review_workflow is not None)
            output = out_stream.getvalue()
            self.assertNotIn("Advisory Review workflow", output)
            self.assertNotIn("open-code-review", output)


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


class ManagedWorkflowsAndDependabotDetectionTests(unittest.TestCase):
    """Tests for detecting existing managed workflow files and Dependabot in inventory and resume state."""

    def test_has_existing_configuration_true_with_managed_workflows(self):
        inv = minimal_empty_inventory()
        inv["managed_workflows"] = [
            {"template": "profile-ci", "profile": "python", "filename": "ci.yml", "sha": "a" * 40}
        ]
        self.assertTrue(has_existing_configuration(inv))

    def test_has_existing_configuration_true_with_dependabot(self):
        inv = minimal_empty_inventory()
        inv["dependabot"] = {
            "status": "managed",
            "cadence": "monthly",
            "package_ecosystems": ["pip", "github-actions"],
        }
        self.assertTrue(has_existing_configuration(inv))

    def test_format_inventory_summary_includes_managed_workflows_and_dependabot(self):
        inv = sample_inventory_data()
        inv["managed_workflows"] = [
            {"filename": "ci.yml"},
            {"filename": "dependency-review.yml"},
        ]
        inv["dependabot"] = {
            "status": "managed",
            "cadence": "weekly",
        }
        summary = format_inventory_summary(inv)
        self.assertIn("Managed Workflows: ci.yml, dependency-review.yml", summary)
        self.assertIn("Dependabot: Configured (weekly cadence)", summary)

    def test_format_inventory_summary_unconfigured_workflows_and_dependabot(self):
        inv = minimal_empty_inventory()
        summary = format_inventory_summary(inv)
        self.assertIn("Managed Workflows: None detected", summary)
        self.assertIn("Dependabot: Not configured", summary)

    def test_extract_defaults_from_inventory_workflows_and_dependabot(self):
        inv = minimal_empty_inventory()
        sha_ci = "1" * 40
        sha_dep = "2" * 40
        sha_cnt = "3" * 40
        sha_cmake = "4" * 40
        inv["managed_workflows"] = [
            {
                "template": "profile-ci",
                "profile": "python",
                "filename": "ci.yml",
                "expected_sha": sha_ci,
                "runs_on": "self-hosted",
                "triggers": ["pull_request", "push"],
                "versions": ["3.11", "3.12"],
                "python_setup_mode": "uv",
                "package_manager": "uv",
                "install_commands": ["uv pip install -e ."],
                "commands": ["pytest -v"],
                "cache": True,
                "lfs": True,
                "lint_commands": ["flake8 src"],
                "paths": ["src/**"],
            },
            {
                "template": "dependency-review",
                "filename": "dependency-review.yml",
                "expected_sha": sha_dep,
                "runs_on": "ubuntu-latest",
                "fail_on_severity": "moderate",
                "allow_licenses": "MIT, Apache-2.0",
                "deny_licenses": "GPL-3.0",
                "comment_summary_in_pr": "always",
            },
            {
                "template": "container-publish",
                "filename": "container-publish.yml",
                "expected_sha": sha_cnt,
                "runs_on": "ubuntu-latest",
                "triggers": ["push"],
                "registry": "ghcr.io",
                "dockerfile": "Dockerfile.prod",
                "context": ".",
                "tags": "ghcr.io/acme/app:latest",
            },
            {
                "template": "profile-ci",
                "profile": "cmake-mpi",
                "filename": "cmake-ci.yml",
                "expected_sha": sha_cmake,
                "runs_on": "self-hosted",
                "compilers": ["gcc", "clang"],
                "ranks": [1, 2, 4],
                "build_commands": ["cmake -B build", "cmake --build build"],
                "test_commands": ["ctest --test-dir build"],
                "omp_threads": 4,
                "install_system_deps": True,
                "ccache": True,
                "opencl": True,
                "paths": ["src/**"],
            },
        ]
        inv["dependabot"] = {
            "status": "managed",
            "cadence": "weekly",
            "package_ecosystems": ["pip", "github-actions"],
            "open_pull_requests_limit": 10,
            "groups": True,
        }

        defaults = extract_defaults_from_inventory(inv)

        self.assertIn("workflows", defaults)
        self.assertEqual(len(defaults["workflows"]), 4)

        # Python options check
        py = defaults.get("python")
        self.assertIsNotNone(py)
        self.assertEqual(py["versions"], ["3.11", "3.12"])
        self.assertEqual(py["python_setup_mode"], "uv")
        self.assertEqual(py["package_manager"], "uv")
        self.assertEqual(py["install_commands"], ["uv pip install -e ."])
        self.assertEqual(py["commands"], ["pytest -v"])
        self.assertTrue(py["cache"])
        self.assertTrue(py["lfs"])
        self.assertEqual(py["lint_commands"], ["flake8 src"])
        self.assertEqual(py["paths"], ["src/**"])
        self.assertEqual(py["triggers"], ["pull_request", "push"])
        self.assertEqual(py["runs_on"], "self-hosted")
        self.assertEqual(py["expected_sha"], sha_ci)

        # CMake options check
        cmake = defaults.get("cmake_mpi")
        self.assertIsNotNone(cmake)
        self.assertEqual(cmake["compilers"], ["gcc", "clang"])
        self.assertEqual(cmake["ranks"], [1, 2, 4])
        self.assertEqual(cmake["build_commands"], ["cmake -B build", "cmake --build build"])
        self.assertEqual(cmake["test_commands"], ["ctest --test-dir build"])
        self.assertEqual(cmake["omp_threads"], 4)
        self.assertTrue(cmake["install_system_deps"])
        self.assertTrue(cmake["ccache"])
        self.assertTrue(cmake["opencl"])
        self.assertEqual(cmake["paths"], ["src/**"])
        self.assertEqual(cmake["runs_on"], "self-hosted")
        self.assertEqual(cmake["expected_sha"], sha_cmake)

        # Dependency review options check
        dep_rev = defaults.get("dependency_review")
        self.assertIsNotNone(dep_rev)
        self.assertEqual(dep_rev["fail_on_severity"], "moderate")
        self.assertEqual(dep_rev["allow_licenses"], "MIT, Apache-2.0")
        self.assertEqual(dep_rev["deny_licenses"], "GPL-3.0")
        self.assertEqual(dep_rev["comment_summary_in_pr"], "always")
        self.assertEqual(dep_rev["runs_on"], "ubuntu-latest")
        self.assertEqual(dep_rev["expected_sha"], sha_dep)

        # Container options check
        cont = defaults.get("container")
        self.assertIsNotNone(cont)
        self.assertEqual(cont["triggers"], ["push"])
        self.assertEqual(cont["registry"], "ghcr.io")
        self.assertEqual(cont["dockerfile"], "Dockerfile.prod")
        self.assertEqual(cont["context"], ".")
        self.assertEqual(cont["tags"], "ghcr.io/acme/app:latest")
        self.assertEqual(cont["runs_on"], "ubuntu-latest")
        self.assertEqual(cont["expected_sha"], sha_cnt)

        # Dependabot options check
        dep = defaults.get("dependabot")
        self.assertIsNotNone(dep)
        self.assertEqual(dep["cadence"], "weekly")
        self.assertEqual(dep["package_ecosystems"], ["pip", "github-actions"])
        self.assertEqual(dep["open_pull_requests_limit"], 10)
        self.assertTrue(dep["groups"])

    def test_extract_defaults_from_raw_workflow_files(self):
        ci_yaml = """# Managed by sciflow-cicd
name: "Python CI"
on:
  pull_request:
    paths:
      - "lib/**"
jobs:
  test:
    runs-on: self-hosted
    strategy:
      matrix:
        python-version: ["3.10", "3.11"]
    steps:
      - uses: actions/checkout@1111111111111111111111111111111111111111
        with:
          lfs: true
      - uses: astral-sh/setup-uv@2222222222222222222222222222222222222222
        with:
          enable-cache: true
      - run: uv pip install .
      - run: pytest
"""
        dep_yaml = """# Dependabot configuration generated by sciflow-cicd
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "monthly"
    open-pull-requests-limit: 7
    groups:
      dependencies:
        patterns: ["*"]
"""
        inv = {
            "workflow_files": {
                ".github/workflows/ci.yml": ci_yaml,
                ".github/dependabot.yml": dep_yaml,
            }
        }
        defaults = extract_defaults_from_inventory(inv)
        self.assertIn("workflows", defaults)
        self.assertIn("python", defaults)
        self.assertIn("dependabot", defaults)
        self.assertEqual(defaults["python"]["versions"], ["3.10", "3.11"])
        self.assertEqual(defaults["python"]["runs_on"], "self-hosted")
        self.assertEqual(defaults["python"]["python_setup_mode"], "uv")
        self.assertEqual(defaults["dependabot"]["open_pull_requests_limit"], 7)

    def test_run_state_detection_detects_workflows_and_dependabot_from_api(self):
        ci_bytes = b"# Managed by sciflow-cicd\nname: Python CI\non:\n  pull_request:\njobs:\n  ci:\n    runs-on: self-hosted\n    steps:\n      - uses: actions/checkout@1111111111111111111111111111111111111111\n      - run: pytest\n"
        dep_bytes = b"# Dependabot configuration generated by sciflow-cicd\nversion: 2\nupdates:\n  - package-ecosystem: pip\n    schedule:\n      interval: monthly\n"
        import base64, hashlib
        ci_sha = hashlib.sha1(b"blob " + str(len(ci_bytes)).encode() + b"\0" + ci_bytes).hexdigest()
        dep_sha = hashlib.sha1(b"blob " + str(len(dep_bytes)).encode() + b"\0" + dep_bytes).hexdigest()

        def fake_request(method, path):
            if path == "/repos/acme/project":
                return 200, {
                    "full_name": "acme/project",
                    "visibility": "public",
                    "default_branch": "main",
                }
            if path == "/repos/acme/project/contents/.github/workflows/ci.yml?ref=main":
                return 200, {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(ci_bytes).decode(),
                    "sha": ci_sha,
                }
            if path == "/repos/acme/project/contents/.github/dependabot.yml?ref=main":
                return 200, {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(dep_bytes).decode(),
                    "sha": dep_sha,
                }
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
        self.assertIn("workflows", session.defaults)
        self.assertIn("python", session.defaults)
        self.assertEqual(session.defaults["python"]["runs_on"], "self-hosted")
        self.assertIn("dependabot", session.defaults)
        self.assertEqual(session.defaults["dependabot"]["cadence"], "monthly")
        summary = out_stream.getvalue()
        self.assertIn("Managed Workflows: ci.yml", summary)
        self.assertIn("Dependabot: Configured (monthly cadence)", summary)


def generated_managed_files():
    sha = "a" * 40
    files = {
        ".github/workflows/ci.yml": render_workflow({
            "template": "profile-ci", "template_version": 1, "filename": "ci.yml",
            "profile": "python", "triggers": ["pull_request"], "checkout_sha": sha,
            "runs_on": "self-hosted", "timeout_minutes": 15,
            "python": {"versions": ["3.11", "3.12"], "commands": ["pytest -q"],
                       "cache": True, "lfs": True, "install_commands": ["pip install -e ."],
                       "lint_commands": ["ruff check ."]},
        }),
        ".github/workflows/cmake-ci.yml": render_workflow({
            "template": "profile-ci", "template_version": 1, "filename": "cmake-ci.yml",
            "profile": "cmake-mpi", "triggers": ["pull_request", "push"], "checkout_sha": sha,
            "runs_on": ["self-hosted", "linux"],
            "cmake_mpi": {"ranks": [1, 2], "compilers": ["gcc", "clang"],
                          "extended_ranks": [1, 2, 4], "omp_threads": 4,
                          "install_system_deps": True, "ccache": True, "opencl": True,
                          "build_commands": ["cmake -B build -S ."],
                          "test_commands": ["ctest --test-dir build"]},
        }),
        ".github/workflows/pr-ci.yml": render_workflow({
            "template": "pr-ci", "template_version": 1, "filename": "pr-ci.yml",
            "triggers": ["pull_request", "push"], "checkout_sha": sha,
            "commands": ["python3 -m unittest discover -s tests"], "runs_on": "self-hosted",
        }),
        ".github/workflows/dependency-review.yml": render_workflow({
            "template": "dependency-review", "template_version": 1,
            "filename": "dependency-review.yml", "triggers": ["pull_request"],
            "checkout_sha": sha, "action_sha": sha, "fail_on_severity": "moderate",
        }),
        ".github/workflows/container-publish.yml": render_workflow({
            "template": "container-publish", "template_version": 1,
            "filename": "container-publish.yml", "triggers": ["push"], "checkout_sha": sha,
            "registry": "ghcr.io", "dockerfile": "Dockerfile.prod", "context": ".",
            "tags": "ghcr.io/acme/app:latest",
        }),
        ".github/workflows/open-code-review.yml": render_review_workflow(validate_review_policy({
            "name": "review",
            "workflow": {"template": "open-code-review", "template_version": 1,
                         "filename": "open-code-review.yml", "triggers": ["pull_request_target"],
                         "events": ["opened", "synchronize", "reopened"], "fork_policy": "skip",
                         "action_sha": "182898cf522da3d04157b422752d028417974e19",
                         "ocr_version": "1.12.12", "max_tokens_budget": 50000,
                         "timeout_minutes": 30},
            "llm": {"url": "https://api.example.com/v1", "model": "review-model-v1",
                    "use_anthropic": False, "secret_name": "OCR_LLM_AUTH_TOKEN"},
            "limits": {"max_retention_days": 30},
        })),
        ".github/dependabot.yml": render_dependabot_config({
            "name": "dependabot", "cadence": "weekly",
            "package_ecosystems": ["pip", "github-actions"],
            "open_pull_requests_limit": 7, "groups": True,
        }).encode("utf-8"),
    }
    return files


def blob_sha(content):
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


class StdlibFallbackInventoryParsingTests(unittest.TestCase):
    @unittest.skipIf(yaml is None, "PyYAML oracle is unavailable")
    def test_python_profile_inventory_parsing_without_pyyaml_matches_pyyaml(self):
        content = generated_managed_files()[".github/workflows/ci.yml"]
        path = ".github/workflows/ci.yml"
        sha = blob_sha(content)
        expected = setup_repo_module.parse_workflow_content(path, content, sha)

        with patch.object(setup_repo_module, "yaml", None):
            actual = setup_repo_module.parse_workflow_content(path, content, sha)

        self.assertEqual(actual, expected)

    @unittest.skipIf(yaml is None, "PyYAML oracle is unavailable")
    def test_generated_workflow_inventory_parsing_without_pyyaml_matches_pyyaml(self):
        for path, content in generated_managed_files().items():
            if path.endswith("dependabot.yml"):
                continue
            with self.subTest(path=path):
                sha = blob_sha(content)
                expected = setup_repo_module.parse_workflow_content(path, content, sha)
                with patch.object(setup_repo_module, "yaml", None):
                    actual = setup_repo_module.parse_workflow_content(path, content, sha)
                self.assertEqual(actual, expected)

    @unittest.skipIf(yaml is None, "PyYAML oracle is unavailable")
    def test_pr_ci_inventory_parsing_without_pyyaml_matches_pyyaml(self):
        content = generated_managed_files()[".github/workflows/pr-ci.yml"]
        path = ".github/workflows/pr-ci.yml"
        sha = blob_sha(content)
        expected = setup_repo_module.parse_workflow_content(path, content, sha)

        with patch.object(setup_repo_module, "yaml", None):
            actual = setup_repo_module.parse_workflow_content(path, content, sha)

        self.assertEqual(actual, expected)
        self.assertEqual(actual["template"], "pr-ci")
        self.assertEqual(actual["triggers"], ["pull_request", "push"])
        self.assertEqual(actual["commands"], ["python3 -m unittest discover -s tests"])

    def test_dependabot_settings_survive_without_pyyaml(self):
        content = generated_managed_files()[".github/dependabot.yml"]
        with patch.object(setup_repo_module, "yaml", None):
            parsed = setup_repo_module.parse_dependabot_content(content, blob_sha(content))

        self.assertEqual(parsed["cadence"], "weekly")
        self.assertEqual(parsed["package_ecosystems"], ["pip", "github-actions"])
        self.assertEqual(parsed["open_pull_requests_limit"], 7)
        self.assertTrue(parsed["groups"])

    @unittest.skipIf(yaml is None, "PyYAML oracle is unavailable")
    def test_dependabot_inventory_parsing_without_pyyaml_matches_pyyaml(self):
        content = generated_managed_files()[".github/dependabot.yml"]
        sha = blob_sha(content)
        expected = setup_repo_module.parse_dependabot_content(content, sha)
        with patch.object(setup_repo_module, "yaml", None):
            actual = setup_repo_module.parse_dependabot_content(content, sha)
        self.assertEqual(actual, expected)

    def test_legacy_paths_and_runner_labels_survive_without_pyyaml(self):
        content = b'''# Managed by sciflow-cicd
name: Python CI
on:
  pull_request:
    paths:
      - "lib/**"
  push:
jobs:
  ci:
    runs-on:
      - self-hosted
      - gpu
    strategy:
      matrix:
        python-version: ["3.10", "3.11"]
    steps:
      - uses: actions/setup-python@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        with:
          cache: "pip"
      - run: "pytest -q"
'''
        path = ".github/workflows/ci.yml"
        sha = blob_sha(content)
        with patch.object(setup_repo_module, "yaml", None):
            parsed = setup_repo_module.parse_workflow_content(path, content, sha)

        self.assertEqual(parsed["triggers"], ["pull_request", "push"])
        self.assertEqual(parsed["paths"], ["lib/**"])
        self.assertEqual(parsed["runs_on"], ["self-hosted", "gpu"])
        self.assertEqual(parsed["versions"], ["3.10", "3.11"])
        self.assertTrue(parsed["cache"])
        self.assertEqual(parsed["commands"], ["pytest -q"])

    def test_quoted_flow_values_and_comments_survive_without_pyyaml(self):
        content = b'''# Managed by sciflow-cicd
name: "CI, with comma"
on:
  pull_request:
    paths:
      - "src/**"
jobs:
  ci:
    runs-on: [self-hosted, "linux,arm"]
    steps:
      - run: "echo 'a, b # not a comment' && pytest -q"
'''
        with patch.object(setup_repo_module, "yaml", None):
            parsed = setup_repo_module.parse_workflow_content(
                ".github/workflows/ci.yml", content, blob_sha(content))

        self.assertEqual(parsed["paths"], ["src/**"])
        self.assertEqual(parsed["runs_on"], ["self-hosted", "linux,arm"])
        self.assertEqual(parsed["commands"], ["echo 'a, b # not a comment' && pytest -q"])

    def test_python_setup_mode_and_cache_survive_without_pyyaml(self):
        content = b'''# sciflow-cicd workflow-template=profile-ci version=1
name: "Python CI"
on:
  pull_request:
jobs:
  ci:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        python-version: ["3.12"]
    steps:
      - uses: actions/checkout@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        with:
          lfs: true
      - uses: astral-sh/setup-uv@bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
        with:
          enable-cache: true
      - run: "uv pip install -e ."
      - run: "pytest -q"
'''
        path = ".github/workflows/ci.yml"
        with patch.object(setup_repo_module, "yaml", None):
            parsed = setup_repo_module.parse_workflow_content(path, content, blob_sha(content))

        self.assertEqual(parsed["python_setup_mode"], "uv")
        self.assertEqual(parsed["package_manager"], "uv")
        self.assertTrue(parsed["cache"])
        self.assertTrue(parsed["lfs"])
        self.assertEqual(parsed["install_commands"], ["uv pip install -e ."])
        self.assertEqual(parsed["commands"], ["pytest -q"])

    def test_resume_state_defaults_survive_without_pyyaml(self):
        files = generated_managed_files()
        inventory_data = {"workflow_files": {path: content.decode("utf-8")
                                             for path, content in files.items()}}
        with patch.object(setup_repo_module, "yaml", None):
            defaults = extract_defaults_from_inventory(inventory_data)

        self.assertEqual(defaults["python"]["versions"], ["3.11", "3.12"])
        self.assertEqual(defaults["python"]["runs_on"], "self-hosted")
        self.assertEqual(defaults["cmake_mpi"]["compilers"], ["gcc", "clang"])
        self.assertEqual(defaults["cmake_mpi"]["runs_on"], ["self-hosted", "linux"])
        self.assertEqual(defaults["dependency_review"]["fail_on_severity"], "moderate")
        self.assertEqual(defaults["container"]["dockerfile"], "Dockerfile.prod")
        self.assertEqual(defaults["dependabot"]["cadence"], "weekly")
        self.assertEqual(defaults["dependabot"]["package_ecosystems"], ["pip", "github-actions"])

    @unittest.skipIf(yaml is None, "PyYAML oracle is unavailable")
    def test_resume_state_defaults_without_pyyaml_match_pyyaml(self):
        files = generated_managed_files()
        inventory_data = {"workflow_files": {path: content.decode("utf-8")
                                             for path, content in files.items()}}
        expected = extract_defaults_from_inventory(inventory_data)
        with patch.object(setup_repo_module, "yaml", None):
            actual = extract_defaults_from_inventory(inventory_data)
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
