"""Tests verifying fixes for findings identified during code review of self-hosted runner support."""

from __future__ import annotations

import io
import pathlib
import sys
import unittest
from unittest.mock import MagicMock

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
TESTS_DIR = ROOT_DIR / "tests"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    import yaml
except ImportError:
    yaml = None

import install_github_workflows as installer
import interactive_workflows_rulesets as interactive_mod
import runner_support
from interactive_workflows_rulesets import prompt_workflows_stage
from runner_support import format_runs_on, is_self_hosted, validate_runs_on

CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
LOGIN_SHA = "9780b0c442fbb1117ed29e0efdff1e18412f7567"
BUILD_SHA = "471d1dc4e07e5cdedd4c2171150001c434f0b7a4"
DEP_SHA = "554f0576cf3806a445399d52144e55ec598b7a2f"


class TestReviewFixSelfHosted(unittest.TestCase):
    """Test suite covering findings 1 through 5."""

    # -------------------------------------------------------------------------
    # Finding 1: Fork Execution Guard condition on multi-trigger workflows
    # -------------------------------------------------------------------------

    def test_finding1_fork_guard_condition_with_multi_triggers_in_profile_ci(self):
        """Fork guard condition allows push/dispatch and guards PR events."""
        spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request", "push", "workflow_dispatch"],
            "checkout_sha": CHECKOUT_SHA,
            "profile": "python",
            "runs_on": "self-hosted",
            "is_public": True,
            "python": {
                "versions": ["3.11"],
                "commands": ["pytest"],
            },
        }
        content = installer.render(spec).decode("utf-8")
        expected_if = (
            "    if: github.event_name != 'pull_request' || "
            "github.event.pull_request.head.repo.full_name == github.repository\n"
        )
        self.assertIn(expected_if, content)

    def test_finding1_fork_guard_condition_with_paths_in_profile_ci(self):
        """Fork guard condition with detect-changes requires run_ci=true and guards PRs."""
        spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request", "push"],
            "checkout_sha": CHECKOUT_SHA,
            "profile": "cmake-mpi",
            "paths": ["src/**", "include/**"],
            "runs_on": "self-hosted",
            "fork_guard": True,
            "cmake_mpi": {
                "ranks": [1, 2],
            },
        }
        content = installer.render(spec).decode("utf-8")
        expected_if = (
            "    if: needs.detect-changes.outputs.run_ci == 'true' && ("
            "github.event_name != 'pull_request' || "
            "github.event.pull_request.head.repo.full_name == github.repository)\n"
        )
        self.assertIn(expected_if, content)

    def test_finding1_fork_guard_condition_in_container_publish(self):
        """Fork guard in container-publish allows push/schedule and guards PRs."""
        spec = {
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": ["pull_request", "push"],
            "checkout_sha": CHECKOUT_SHA,
            "runs_on": "self-hosted",
            "is_public": True,
        }
        content = installer.render(spec).decode("utf-8")
        expected_if = (
            "    if: github.event_name != 'pull_request' || "
            "github.event.pull_request.head.repo.full_name == github.repository\n"
        )
        self.assertIn(expected_if, content)

    def test_finding1_fork_guard_condition_in_pr_ci(self):
        """Fork guard in pr-ci allows non-PR triggers and guards PRs."""
        spec = {
            "template": "pr-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request", "workflow_dispatch"],
            "checkout_sha": CHECKOUT_SHA,
            "commands": ["pytest"],
            "runs_on": "self-hosted",
            "fork_guard": True,
        }
        content = installer.render(spec).decode("utf-8")
        expected_if = (
            "    if: github.event_name != 'pull_request' || "
            "github.event.pull_request.head.repo.full_name == github.repository\n"
        )
        self.assertIn(expected_if, content)

    # -------------------------------------------------------------------------
    # Finding 2: Schedule cron expression validation and interactive prompting
    # -------------------------------------------------------------------------

    def test_finding2_schedule_trigger_requires_schedule_key(self):
        """When schedule is in triggers, missing schedule key raises ValueError."""
        # In pr-ci
        spec_pr = {
            "template": "pr-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request", "schedule"],
            "checkout_sha": CHECKOUT_SHA,
            "commands": ["pytest"],
        }
        with self.assertRaisesRegex(ValueError, "schedule trigger requires schedule cron expression"):
            installer.render(spec_pr)

        # In profile-ci
        spec_prof = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request", "schedule"],
            "checkout_sha": CHECKOUT_SHA,
            "profile": "python",
            "python": {"versions": ["3.11"], "commands": ["pytest"]},
        }
        with self.assertRaisesRegex(ValueError, "schedule trigger requires schedule cron expression"):
            installer.render(spec_prof)

        # In container-publish
        spec_cont = {
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": ["schedule"],
            "checkout_sha": CHECKOUT_SHA,
        }
        with self.assertRaisesRegex(ValueError, "schedule trigger requires schedule cron expression"):
            installer.render(spec_cont)

    def test_finding2_schedule_cron_validation_rejects_empty_or_invalid(self):
        """schedule cron strings must be nonempty and valid."""
        for invalid in ("", "   ", [], [""], ["   "], None):
            spec = {
                "template": "pr-ci",
                "template_version": 1,
                "filename": "ci.yml",
                "triggers": ["pull_request", "schedule"],
                "checkout_sha": CHECKOUT_SHA,
                "commands": ["pytest"],
                "schedule": invalid,
            }
            with self.assertRaises(ValueError):
                installer.render(spec)

    def test_finding2_valid_schedule_renders_cron_mapping(self):
        """Valid schedule string or list renders on.schedule.cron."""
        spec = {
            "template": "pr-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request", "schedule"],
            "checkout_sha": CHECKOUT_SHA,
            "commands": ["pytest"],
            "schedule": ["0 2 * * 1", "0 12 * * 5"],
        }
        content = installer.render(spec).decode("utf-8")
        self.assertIn("  schedule:\n    - cron: \"0 2 * * 1\"\n    - cron: \"0 12 * * 5\"\n", content)

    def test_finding2_interactive_prompt_schedule_for_pr_ci(self):
        """Interactive setup prompts for schedule cron when schedule trigger chosen."""
        # 1. confirm: y
        # 2. runner: default self-hosted (\n)
        # 3. runner labels: default (\n)
        # 4. multichoice: 1 (Standard CI)
        # 5. commands: default (\n)
        # 6. triggers: pull_request, schedule
        # 7. schedule cron: 0 4 * * 0
        # 8. paths: default none (\n)
        # 9. override runner: default No (\n)
        in_stream = io.StringIO("y\n\n\n1\n\npull_request, schedule\n0 4 * * 0\n\n\n")
        out_stream = io.StringIO()
        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        wf = stage["workflows"][0]
        self.assertIn("schedule", wf["triggers"])
        self.assertEqual(wf["schedule"], ["0 4 * * 0"])

    def test_finding2_interactive_prompt_schedule_for_container_publish(self):
        """Interactive setup prompts for schedule cron when schedule trigger chosen for container."""
        # 1. confirm: y
        # 2. runner: default (\n)
        # 3. labels: default (\n)
        # 4. multichoice: 5 (Container publish)
        # 5. triggers: schedule, push
        # 6. schedule cron: 0 1 * * *
        # 7. registry: default (\n)
        # 8. dockerfile: default (\n)
        # 9. context: default (\n)
        # 10. tags: default (\n)
        in_stream = io.StringIO("y\n\n\n5\nschedule, push\n0 1 * * *\n\n\n\n\n")
        out_stream = io.StringIO()
        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        wf = stage["workflows"][0]
        self.assertIn("schedule", wf["triggers"])
        self.assertEqual(wf["schedule"], ["0 1 * * *"])

    # -------------------------------------------------------------------------
    # Finding 3: paths filter in pr-ci
    # -------------------------------------------------------------------------

    def test_finding3_paths_filter_in_pr_ci_renders_detect_changes(self):
        """pr-ci with paths generates detect-changes job and depends on it."""
        spec = {
            "template": "pr-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT_SHA,
            "commands": ["pytest tests/"],
            "paths": ["src/**", "tests/**"],
            "runs_on": "self-hosted",
        }
        content = installer.render(spec).decode("utf-8")
        self.assertIn("  detect-changes:", content)
        self.assertIn("    runs-on: ubuntu-24.04", content)
        self.assertIn('patterns = ["src/**", "tests/**"]', content)
        self.assertIn("  ci:\n    needs: [detect-changes]\n    if: needs.detect-changes.outputs.run_ci == 'true'\n", content)

    def test_finding3_paths_filter_in_pr_ci_with_fork_guard(self):
        """pr-ci with paths and fork guard combines run_ci check with repo guard."""
        spec = {
            "template": "pr-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request", "push"],
            "checkout_sha": CHECKOUT_SHA,
            "commands": ["pytest"],
            "paths": ["lib/**"],
            "runs_on": "self-hosted",
            "fork_guard": True,
        }
        content = installer.render(spec).decode("utf-8")
        expected_if = (
            "    needs: [detect-changes]\n"
            "    if: needs.detect-changes.outputs.run_ci == 'true' && ("
            "github.event_name != 'pull_request' || "
            "github.event.pull_request.head.repo.full_name == github.repository)\n"
        )
        self.assertIn(expected_if, content)

    def test_finding3_paths_filter_validation_in_pr_ci(self):
        """Invalid paths in pr-ci are rejected."""
        for invalid in ([], [""], ["   "], "src/**", 123):
            spec = {
                "template": "pr-ci",
                "template_version": 1,
                "filename": "ci.yml",
                "triggers": ["pull_request"],
                "checkout_sha": CHECKOUT_SHA,
                "commands": ["pytest"],
                "paths": invalid,
            }
            with self.assertRaises(ValueError):
                installer.render(spec)

    def test_finding3_interactive_prompt_paths_for_pr_ci(self):
        """Interactive setup prompts for optional paths filter in PR CI."""
        # 1. confirm: y
        # 2. runner: default (\n)
        # 3. labels: default (\n)
        # 4. multichoice: 1 (Standard CI)
        # 5. commands: default (\n)
        # 6. triggers: default (\n)
        # 7. paths: src/**, tests/**
        # 8. override runner: default No (\n)
        in_stream = io.StringIO("y\n\n\n1\n\n\nsrc/**, tests/**\n\n")
        out_stream = io.StringIO()
        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        wf = stage["workflows"][0]
        self.assertEqual(wf["paths"], ["src/**", "tests/**"])

    # -------------------------------------------------------------------------
    # Finding 4: omp_threads scalar setting
    # -------------------------------------------------------------------------

    def test_finding4_omp_threads_sets_scalar_env_and_omits_matrix_axis(self):
        """omp_threads sets OMP_NUM_THREADS in step env and does NOT add to strategy matrix."""
        spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT_SHA,
            "profile": "cmake-mpi",
            "cmake_mpi": {
                "ranks": [1, 2],
                "omp_threads": 4,
            },
        }
        content = installer.render(spec).decode("utf-8")
        if yaml is not None:
            parsed = yaml.safe_load(content)
            matrix = parsed["jobs"]["ci"]["strategy"]["matrix"]
            self.assertNotIn("omp_threads", matrix)
            self.assertEqual(matrix["ranks"], [1, 2])
        self.assertIn('OMP_NUM_THREADS: "4"', content)

    def test_finding4_omp_threads_default_is_2(self):
        """Default omp_threads is 2 when omitted."""
        spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT_SHA,
            "profile": "cmake-mpi",
            "cmake_mpi": {
                "ranks": [1],
            },
        }
        content = installer.render(spec).decode("utf-8")
        self.assertIn('OMP_NUM_THREADS: "2"', content)

    def test_finding4_omp_threads_rejects_non_scalar_or_invalid_values(self):
        """omp_threads rejects lists, strings, booleans, zero, and negative integers."""
        for invalid in ([1, 2], [4], "4", True, False, 0, -2, None):
            spec = {
                "template": "profile-ci",
                "template_version": 1,
                "filename": "ci.yml",
                "triggers": ["pull_request"],
                "checkout_sha": CHECKOUT_SHA,
                "profile": "cmake-mpi",
                "cmake_mpi": {
                    "ranks": [1],
                    "omp_threads": invalid,
                },
            }
            with self.assertRaises(ValueError):
                installer.render(spec)

    # -------------------------------------------------------------------------
    # Finding 5: Runner validation and YAML formatting consolidation
    # -------------------------------------------------------------------------

    def test_finding5_runner_support_helpers(self):
        """Consolidated runner_support validates, normalizes, and formats runner configurations."""
        # validate_runs_on
        self.assertEqual(validate_runs_on(None), "self-hosted")
        self.assertEqual(validate_runs_on("self-hosted"), "self-hosted")
        self.assertEqual(validate_runs_on("ubuntu-24.04"), "ubuntu-24.04")
        self.assertEqual(validate_runs_on(["self-hosted", "gpu"]), ["self-hosted", "gpu"])
        self.assertEqual(validate_runs_on(["self-hosted", "gpu"], as_tuple=True), ("self-hosted", "gpu"))

        # Invalid runs_on
        for invalid in ("", "   ", [], ["self-hosted", "self-hosted"], True, 42, [""]):
            with self.assertRaises(ValueError):
                validate_runs_on(invalid)

        # format_runs_on
        self.assertEqual(format_runs_on("self-hosted"), "    runs-on: self-hosted")
        self.assertEqual(
            format_runs_on(["self-hosted", "gpu"]),
            "    runs-on:\n      - self-hosted\n      - gpu",
        )
        self.assertEqual(
            format_runs_on(("self-hosted", "arm64")),
            "    runs-on:\n      - self-hosted\n      - arm64",
        )

        # is_self_hosted
        self.assertTrue(is_self_hosted("self-hosted"))
        self.assertTrue(is_self_hosted(["self-hosted", "x64"]))
        self.assertTrue(is_self_hosted("custom-runner"))
        self.assertFalse(is_self_hosted("ubuntu-24.04"))
        self.assertFalse(is_self_hosted(["ubuntu-latest", "ubuntu-24.04"]))

    def test_finding5_exported_symbols_match(self):
        """installer re-exports runner_support symbols cleanly."""
        self.assertIs(installer.validate_runs_on, runner_support.validate_runs_on)
        self.assertIs(installer._format_runs_on, runner_support.format_runs_on)
        self.assertIs(installer._is_self_hosted, runner_support.is_self_hosted)


if __name__ == "__main__":
    unittest.main()
