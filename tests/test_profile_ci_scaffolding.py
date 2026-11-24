"""Unit tests for CI profile Action pin verification and legacy pr-ci contract."""

import pathlib
import sys
import unittest
from unittest.mock import MagicMock, patch

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

TESTS_DIR = pathlib.Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import yaml

import install_github_workflows as installer
from test_install_github_workflows import CHECKOUT, DEPENDENCY, WorkflowApi


class ProfileActionPinsTests(unittest.TestCase):
    """Test Action pin verification for CI profiles in install() and install_selected()."""

    def setUp(self):
        self.api = WorkflowApi()
        self.repo = "acme/project"

    def test_install_python_profile_pip_pins_setup_python(self):
        policy = {
            "version": 1,
            "workflows": [
                {
                    "template": "profile-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT,
                    "profile": "python",
                    "python": {
                        "versions": ["3.11", "3.12"],
                        "package_manager": "pip",
                        "commands": ["pytest"],
                    },
                }
            ],
        }
        with patch.object(installer, "stage_workflows") as mock_stage:
            mock_stage.return_value = {"status": "planned", "changes": []}
            installer.install(self.api, self.repo, policy, apply=False)

            self.assertTrue(mock_stage.called)
            pins = mock_stage.call_args[0][3]
            pin_actions = [p[0] for p in pins]

            self.assertIn("actions/checkout", pin_actions)
            self.assertIn("actions/setup-python", pin_actions)
            self.assertNotIn("astral-sh/setup-uv", pin_actions)

            setup_py_pin = next(p for p in pins if p[0] == "actions/setup-python")
            self.assertEqual(setup_py_pin[1], installer.DEFAULT_SETUP_PYTHON_SHA.lower())

    def test_install_python_profile_uv_pins_setup_python_and_setup_uv(self):
        policy = {
            "version": 1,
            "workflows": [
                {
                    "template": "profile-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT,
                    "profile": "python",
                    "package_manager": "uv",
                    "python": {
                        "versions": ["3.11", "3.12"],
                        "commands": ["uv run pytest"],
                    },
                }
            ],
        }
        with patch.object(installer, "stage_workflows") as mock_stage:
            mock_stage.return_value = {"status": "planned", "changes": []}
            installer.install(self.api, self.repo, policy, apply=False)

            self.assertTrue(mock_stage.called)
            pins = mock_stage.call_args[0][3]
            pin_actions = [p[0] for p in pins]

            self.assertIn("actions/checkout", pin_actions)
            self.assertIn("actions/setup-python", pin_actions)
            self.assertIn("astral-sh/setup-uv", pin_actions)

            setup_uv_pin = next(p for p in pins if p[0] == "astral-sh/setup-uv")
            self.assertEqual(setup_uv_pin[1], installer.DEFAULT_SETUP_UV_SHA.lower())

    def test_install_scientific_python_custom_shas(self):
        custom_python_sha = "1" * 40
        custom_uv_sha = "2" * 40
        policy = {
            "version": 1,
            "workflows": [
                {
                    "template": "profile-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT,
                    "profile": "scientific-python",
                    "package_manager": "uv",
                    "setup_python_sha": custom_python_sha,
                    "setup_uv_sha": custom_uv_sha,
                    "python": {
                        "versions": ["3.11"],
                        "commands": ["pytest"],
                    },
                }
            ],
        }
        with patch.object(installer, "stage_workflows") as mock_stage:
            mock_stage.return_value = {"status": "planned", "changes": []}
            installer.install(self.api, self.repo, policy, apply=False)

            pins = mock_stage.call_args[0][3]
            self.assertIn(("actions/setup-python", custom_python_sha), pins)
            self.assertIn(("astral-sh/setup-uv", custom_uv_sha), pins)

    def test_install_selected_ci_profile_pip_pins(self):
        wf = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": installer.DEFAULT_CHECKOUT_SHA,
            "profile": "python",
            "python": {
                "versions": ["3.11"],
                "package_manager": "pip",
                "commands": ["pytest"],
            },
        }
        with patch.object(installer, "stage_workflows") as mock_stage:
            mock_stage.return_value = {"status": "planned", "changes": []}
            installer.install_selected(self.api, self.repo, workflow_stage={"workflows": [wf]})

            pins = mock_stage.call_args[0][3]
            pin_actions = [p[0] for p in pins]

            self.assertIn("actions/checkout", pin_actions)
            self.assertIn("actions/setup-python", pin_actions)
            self.assertNotIn("astral-sh/setup-uv", pin_actions)

    def test_install_selected_ci_profile_uv_pins(self):
        wf = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": installer.DEFAULT_CHECKOUT_SHA,
            "profile": "python",
            "package_manager": "uv",
            "python": {
                "versions": ["3.11"],
                "commands": ["uv run pytest"],
            },
        }
        with patch.object(installer, "stage_workflows") as mock_stage:
            mock_stage.return_value = {"status": "planned", "changes": []}
            installer.install_selected(self.api, self.repo, workflow_stage={"workflows": [wf]})

            pins = mock_stage.call_args[0][3]
            pin_actions = [p[0] for p in pins]

            self.assertIn("actions/checkout", pin_actions)
            self.assertIn("actions/setup-python", pin_actions)
            self.assertIn("astral-sh/setup-uv", pin_actions)

    def test_install_selected_scientific_python_uv_pins(self):
        wf = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": installer.DEFAULT_CHECKOUT_SHA,
            "profile": "scientific-python",
            "python": {
                "versions": ["3.11"],
                "package_manager": "uv",
                "commands": ["pytest"],
            },
        }
        with patch.object(installer, "stage_workflows") as mock_stage:
            mock_stage.return_value = {"status": "planned", "changes": []}
            installer.install_selected(self.api, self.repo, workflow_stage={"workflows": [wf]})

            pins = mock_stage.call_args[0][3]
            pin_actions = [p[0] for p in pins]

            self.assertIn("actions/setup-python", pin_actions)
            self.assertIn("astral-sh/setup-uv", pin_actions)

    def test_install_cmake_mpi_profile_does_not_pin_python_or_uv(self):
        wf = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": installer.DEFAULT_CHECKOUT_SHA,
            "profile": "cmake-mpi",
            "cmake_mpi": {
                "ranks": [1, 2],
            },
        }
        with patch.object(installer, "stage_workflows") as mock_stage:
            mock_stage.return_value = {"status": "planned", "changes": []}
            installer.install_selected(self.api, self.repo, workflow_stage={"workflows": [wf]})

            pins = mock_stage.call_args[0][3]
            pin_actions = [p[0] for p in pins]

            self.assertIn("actions/checkout", pin_actions)
            self.assertNotIn("actions/setup-python", pin_actions)
            self.assertNotIn("astral-sh/setup-uv", pin_actions)

    def test_unverified_setup_python_commit_blocks_staging(self):
        wf = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": installer.DEFAULT_CHECKOUT_SHA,
            "profile": "python",
            "setup_python_sha": "f" * 40,
            "python": {
                "versions": ["3.11"],
                "commands": ["pytest"],
            },
        }
        with self.assertRaisesRegex(ValueError, "pinned action commit does not exist: actions/setup-python@"):
            installer.install_selected(self.api, self.repo, workflow_stage={"workflows": [wf]})

    def test_unverified_setup_uv_commit_blocks_staging(self):
        wf = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": installer.DEFAULT_CHECKOUT_SHA,
            "profile": "python",
            "package_manager": "uv",
            "setup_uv_sha": "e" * 40,
            "python": {
                "versions": ["3.11"],
                "commands": ["pytest"],
            },
        }
        with self.assertRaisesRegex(ValueError, "pinned action commit does not exist: astral-sh/setup-uv@"):
            installer.install_selected(self.api, self.repo, workflow_stage={"workflows": [wf]})


class LegacyPrCiRenderContractTests(unittest.TestCase):
    """Test legacy pr-ci contract restoration in render()."""

    def test_legacy_pr_ci_has_no_concurrency_and_no_status_check(self):
        workflow = {
            "template": "pr-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "commands": ["pytest -v", "ruff check ."],
        }
        raw_bytes = installer.render(workflow)
        content = raw_bytes.decode("utf-8")

        # Must not contain concurrency block or cancel-in-progress
        self.assertNotIn("concurrency:", content)
        self.assertNotIn("cancel-in-progress", content)

        # Must not contain status-check job
        self.assertNotIn("status-check:", content)
        self.assertNotIn("Evaluate status check", content)

        # Parse YAML to verify exact structure
        parsed = yaml.safe_load(content)

        # Concurrency must be absent
        self.assertNotIn("concurrency", parsed)

        # Jobs must contain ONLY 'ci'
        self.assertEqual(list(parsed["jobs"].keys()), ["ci"])

        ci_job = parsed["jobs"]["ci"]
        self.assertEqual(ci_job["runs-on"], "self-hosted")
        self.assertEqual(parsed.get("permissions", {}).get("contents"), "read")

        # Steps must have checkout followed by the two run commands
        steps = ci_job["steps"]
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0]["uses"], f"actions/checkout@{CHECKOUT.lower()}")
        self.assertEqual(steps[1]["run"], "pytest -v")
        self.assertEqual(steps[2]["run"], "ruff check .")

    def test_profile_ci_retains_concurrency_and_status_check(self):
        """Contrast: profile-ci retains concurrency and status-check."""
        workflow = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "python": {
                "versions": ["3.11"],
                "commands": ["pytest"],
            },
        }
        raw_bytes = installer.render(workflow)
        content = raw_bytes.decode("utf-8")

        self.assertIn("concurrency:", content)
        self.assertIn("cancel-in-progress: true", content)
        self.assertIn("status-check:", content)

        parsed = yaml.safe_load(content)
        self.assertIn("concurrency", parsed)
        self.assertIn("status-check", parsed["jobs"])
        self.assertIn("ci", parsed["jobs"])

    def test_scientific_python_default_pins_and_adr0007_runner_isolation(self):
        """Default checkout and setup-python pins match v7.0.1/v7.1.0 while control jobs stay on ubuntu-24.04."""
        self.assertEqual(installer.DEFAULT_CHECKOUT_SHA, "3d3c42e5aac5ba805825da76410c181273ba90b1")
        self.assertEqual(installer.DEFAULT_SETUP_PYTHON_SHA, "6d4fd22b20465ccc0f7354476588e8df7c9ca0e0")

        workflow = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": installer.DEFAULT_CHECKOUT_SHA,
            "profile": "scientific-python",
            "runs_on": "self-hosted",
            "paths": ["src/**"],
            "python": {
                "versions": ["3.12"],
                "commands": ["pytest -q"],
            },
        }
        content = installer.render(workflow).decode("utf-8")
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1", content)
        self.assertIn("actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 # v7.1.0", content)

        parsed = yaml.safe_load(content)
        self.assertEqual(parsed["jobs"]["detect-changes"]["runs-on"], "ubuntu-24.04")
        self.assertEqual(parsed["jobs"]["status-check"]["runs-on"], "ubuntu-24.04")
        self.assertEqual(parsed["jobs"]["ci"]["runs-on"], "self-hosted")


if __name__ == "__main__":
    unittest.main()
