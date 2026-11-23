"""Tests for CI profile workflow generation, schema validation, and stable status checks."""

import base64
import json
import os
import pathlib
import re
import subprocess
import sys
import unittest
from unittest.mock import MagicMock

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

TESTS_DIR = pathlib.Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import install_github_workflows as installer
from setup_github_repo import validate_policy as validate_repo_policy
from test_install_github_workflows import WorkflowApi

CHECKOUT = "11bd71901bbe5b1630ceea73d27597364c9af683"
DEPENDENCY = "56339e523c0409420f6c2c9a2f4292bbb3c07dd3"


def _nested_profile_policy(profile, nested_key, nested_options):
    return {
        "version": 1,
        "workflows": [
            {
                "template": "profile-ci",
                "template_version": 1,
                "filename": "ci.yml",
                "triggers": ["pull_request"],
                "checkout_sha": CHECKOUT,
                "profile": profile,
                nested_key: nested_options,
            }
        ],
    }


class ProfileWorkflowSchemaTests(unittest.TestCase):
    """Test policy schema validation for CI profiles and profile-specific options."""

    def test_valid_python_ci_profile_in_workflows_stage(self):
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
                        "versions": ["3.10", "3.11", "3.12"],
                        "package_manager": "pip",
                        "commands": ["pytest -v"],
                        "lint_commands": ["flake8 ."],
                        "lfs": True,
                        "cache": True,
                    },
                    "paths": ["**/*.py", "pyproject.toml"],
                }
            ],
        }
        files = installer.validate_policy(policy)
        self.assertIn(".github/workflows/ci.yml", files)

    def test_valid_cmake_mpi_ci_profile_in_workflows_stage(self):
        policy = {
            "version": 1,
            "workflows": [
                {
                    "template": "profile-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT,
                    "profile": "cmake-mpi",
                    "cmake_mpi": {
                        "ranks": [1, 2, 4],
                        "build_commands": ["cmake -B build -S .", "cmake --build build"],
                        "test_commands": ["ctest --test-dir build --output-on-failure"],
                        "runner_dependencies": ["libopenmpi-dev", "openmpi-bin"],
                        "ccache": True,
                        "opencl": False,
                        "manual_build": False,
                    },
                    "paths": ["CMakeLists.txt", "src/**", "include/**", "tests/**"],
                }
            ],
        }
        files = installer.validate_policy(policy)
        self.assertIn(".github/workflows/ci.yml", files)

    def test_rejects_unsupported_ci_profile(self):
        policy = {
            "version": 1,
            "workflows": [
                {
                    "template": "profile-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT,
                    "profile": "rust-cargo",
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "unsupported CI profile"):
            installer.validate_policy(policy)

    def test_rejects_invalid_python_versions(self):
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
                    "python": {"versions": ["invalid-ver"]},
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "invalid python version"):
            installer.validate_policy(policy)

    def test_rejects_invalid_mpi_ranks(self):
        policy = {
            "version": 1,
            "workflows": [
                {
                    "template": "profile-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT,
                    "profile": "cmake-mpi",
                    "cmake_mpi": {"ranks": [0, -1]},
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "rank"):
            installer.validate_policy(policy)

    def test_rejects_invalid_package_manager(self):
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
                    "python": {"package_manager": "conda"},
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "package_manager"):
            installer.validate_policy(policy)

    def test_nested_python_pip_cache_alias_raises_with_canonical_key(self):
        policy = _nested_profile_policy(
            "python", "python", {"versions": ["3.11"], "commands": ["pytest"], "pip_cache": True}
        )
        with self.assertRaises(ValueError) as ctx:
            installer.validate_policy(policy)
        self.assertIn("pip_cache", str(ctx.exception))
        self.assertIn("use 'cache'", str(ctx.exception))

    def test_nested_python_git_lfs_alias_raises_with_canonical_key(self):
        policy = _nested_profile_policy(
            "python", "python", {"versions": ["3.11"], "commands": ["pytest"], "git_lfs": True}
        )
        with self.assertRaises(ValueError) as ctx:
            installer.validate_policy(policy)
        self.assertIn("git_lfs", str(ctx.exception))
        self.assertIn("use 'lfs'", str(ctx.exception))

    def test_nested_python_options_alias_raises_with_canonical_key(self):
        policy = _nested_profile_policy(
            "python", "python", {"versions": ["3.11"], "commands": ["pytest"], "options": ["pytest -v"]}
        )
        with self.assertRaises(ValueError) as ctx:
            installer.validate_policy(policy)
        self.assertIn("options", str(ctx.exception))
        self.assertIn("use 'profile_options'", str(ctx.exception))

    def test_nested_cmake_mpi_legacy_aliases_raise_with_canonical_keys(self):
        for alias, canonical in (("pip_cache", "cache"), ("git_lfs", "lfs"), ("options", "profile_options")):
            with self.subTest(alias=alias):
                policy = _nested_profile_policy(
                    "cmake-mpi", "cmake_mpi", {"ranks": [1], alias: True}
                )
                with self.assertRaises(ValueError) as ctx:
                    installer.validate_policy(policy)
                self.assertIn(alias, str(ctx.exception))
                self.assertIn(f"use '{canonical}'", str(ctx.exception))

    def test_nested_cmake_mpi_hyphenated_block_rejects_legacy_alias(self):
        policy = _nested_profile_policy(
            "cmake-mpi", "cmake-mpi", {"ranks": [1], "pip_cache": True}
        )
        with self.assertRaises(ValueError) as ctx:
            installer.validate_policy(policy)
        self.assertIn("use 'cache'", str(ctx.exception))

    def test_nested_python_unknown_key_raises_with_allowed_keys(self):
        policy = _nested_profile_policy(
            "python", "python", {"versions": ["3.11"], "commands": ["pytest"], "runtimes": ["3.11"]}
        )
        with self.assertRaises(ValueError) as ctx:
            installer.validate_policy(policy)
        self.assertIn("runtimes", str(ctx.exception))
        self.assertIn("allowed keys:", str(ctx.exception))
        self.assertIn("versions", str(ctx.exception))

    def test_nested_cmake_mpi_unknown_key_raises_with_allowed_keys(self):
        policy = _nested_profile_policy(
            "cmake-mpi", "cmake_mpi", {"ranks": [1], "mpi_version": "openmpi"}
        )
        with self.assertRaises(ValueError) as ctx:
            installer.validate_policy(policy)
        self.assertIn("mpi_version", str(ctx.exception))
        self.assertIn("allowed keys:", str(ctx.exception))
        self.assertIn("compilers", str(ctx.exception))

    def test_nested_block_legacy_alias_rejected_even_when_profile_differs(self):
        policy = _nested_profile_policy(
            "python", "cmake_mpi", {"ranks": [1], "pip_cache": True}
        )
        with self.assertRaises(ValueError) as ctx:
            installer.validate_policy(policy)
        self.assertIn("use 'cache'", str(ctx.exception))

    def test_repo_policy_v2_rejects_ci_profile_and_supports_workflows_stage(self):
        policy_legacy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {
                    "name": "ci-profile",
                    "profile": "python",
                }
            ],
        }
        with self.assertRaises(ValueError) as ctx:
            validate_repo_policy(policy_legacy)
        self.assertIn("ci-profile", str(ctx.exception))

        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {
                    "name": "workflows",
                    "workflows": [
                        {
                            "template": "profile-ci",
                            "template_version": 1,
                            "profile": "python",
                            "filename": "ci.yml",
                            "triggers": ["pull_request"],
                            "checkout_sha": CHECKOUT,
                            "python": {
                                "versions": ["3.11", "3.12"],
                                "commands": ["pytest"],
                            },
                        }
                    ],
                }
            ],
        }
        stages = validate_repo_policy(policy)
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0]["name"], "workflows")


class ProfileWorkflowGenerationTests(unittest.TestCase):
    """Test rendered workflow structure, YAML correctness, concurrency, and anchor jobs."""

    def test_valid_python_nested_keys_still_render(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is optional and unavailable")

        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request", "push"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "python": {
                "versions": ["3.11"],
                "python_setup_mode": "setup-python",
                "package_manager": "pip",
                "install_commands": ["pip install -e ."],
                "commands": ["pytest"],
                "lint_commands": ["flake8 ."],
                "post_merge_commands": ["echo done"],
                "lfs": False,
                "cache": True,
                "paths": ["src/**"],
                "runs_on": "self-hosted",
                "status_check_name": "python-gate",
            },
        }
        parsed = yaml.safe_load(installer.render(workflow_spec))
        self.assertIn("ci", parsed["jobs"])
        self.assertIn("status-check", parsed["jobs"])

    def test_valid_cmake_mpi_nested_keys_still_render(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is optional and unavailable")

        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request", "push"],
            "checkout_sha": CHECKOUT,
            "profile": "cmake-mpi",
            "cmake_mpi": {
                "ranks": [1, 2],
                "compilers": ["gcc", "clang"],
                "omp_threads": 2,
                "install_system_deps": False,
                "extended_ranks": [4],
                "build_commands": ["cmake -B build -S .", "cmake --build build"],
                "test_commands": ["ctest --test-dir build"],
                "runner_dependencies": ["libopenmpi-dev", "openmpi-bin"],
                "ccache": True,
                "opencl": False,
                "manual_build": False,
                "post_merge_commands": ["echo done"],
                "cache": False,
                "cache_sha": "3" * 40,
                "lfs": False,
                "paths": ["CMakeLists.txt"],
                "runs_on": "self-hosted",
                "status_check_name": "cmake-gate",
            },
        }
        parsed = yaml.safe_load(installer.render(workflow_spec))
        self.assertIn("ci", parsed["jobs"])
        self.assertIn("status-check", parsed["jobs"])

    def test_python_profile_rendered_yaml_and_concurrency(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is optional and unavailable")

        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "python": {
                "versions": ["3.11", "3.12"],
                "package_manager": "pip",
                "commands": ["pytest -v"],
                "lint_commands": ["flake8 ."],
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.load(content, Loader=yaml.BaseLoader)

        # Check concurrency controls
        self.assertIn("concurrency", parsed)
        self.assertEqual(
            parsed["concurrency"]["group"],
            "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}",
        )
        self.assertTrue(parsed["concurrency"]["cancel-in-progress"])

        # Check matrix jobs
        self.assertIn("ci", parsed["jobs"])
        ci_job = parsed["jobs"]["ci"]
        self.assertIn("strategy", ci_job)
        self.assertIn("matrix", ci_job["strategy"])
        self.assertEqual(ci_job["strategy"]["matrix"]["python-version"], ["3.11", "3.12"])
        self.assertEqual(ci_job["strategy"]["fail-fast"], "false")

        # Check permissions
        self.assertEqual(parsed["permissions"]["contents"], "read")
        self.assertEqual(ci_job.get("permissions", {}).get("contents"), "read")

        # Check stable status-check anchor job
        self.assertIn("status-check", parsed["jobs"])
        anchor_job = parsed["jobs"]["status-check"]
        self.assertEqual(anchor_job["if"], "always()")
        self.assertIn("ci", anchor_job["needs"])

    def test_cmake_mpi_profile_rendered_yaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is optional and unavailable")

        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "cmake-mpi",
            "cmake_mpi": {
                "ranks": [1, 2],
                "build_commands": ["cmake -B build", "cmake --build build"],
                "test_commands": ["ctest --test-dir build"],
                "runner_dependencies": ["libopenmpi-dev", "openmpi-bin"],
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.load(content, Loader=yaml.BaseLoader)

        self.assertIn("concurrency", parsed)
        self.assertTrue(parsed["concurrency"]["cancel-in-progress"])
        self.assertIn("ci", parsed["jobs"])
        self.assertIn("status-check", parsed["jobs"])

    def test_path_filtering_renders_change_detection_job(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is optional and unavailable")

        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "paths": ["src/**", "tests/**"],
            "python": {
                "versions": ["3.11"],
                "commands": ["pytest"],
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.load(content, Loader=yaml.BaseLoader)

        # Must define detect-changes job
        self.assertIn("detect-changes", parsed["jobs"])
        detect_job = parsed["jobs"]["detect-changes"]
        self.assertIn("outputs", detect_job)

        # ci job must depend on detect-changes output
        ci_job = parsed["jobs"]["ci"]
        self.assertIn("detect-changes", ci_job.get("needs", []))
        self.assertIn("needs.detect-changes.outputs.run_ci == 'true'", ci_job.get("if", ""))

        # status-check anchor must depend on both and run always()
        anchor_job = parsed["jobs"]["status-check"]
        self.assertEqual(anchor_job["if"], "always()")
        self.assertIn("ci", anchor_job["needs"])
        self.assertIn("detect-changes", anchor_job["needs"])

    def test_python_profile_rendered_yaml_uv_package_manager_and_cache(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is optional and unavailable")

        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "python": {
                "versions": ["3.10", "3.11", "3.12"],
                "package_manager": "uv",
                "commands": ["uv run pytest -v"],
                "cache": True,
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.load(content, Loader=yaml.BaseLoader)

        ci_job = parsed["jobs"]["ci"]
        self.assertEqual(ci_job.get("permissions", {}).get("contents"), "read")
        steps = ci_job["steps"]

        # Step 0: checkout
        self.assertTrue(steps[0]["uses"].startswith(f"actions/checkout@{CHECKOUT}"))
        # Step 1: setup-python (no cache: pip when package_manager is uv)
        self.assertTrue(steps[1]["uses"].startswith("actions/setup-python@"))
        self.assertNotIn("cache", steps[1].get("with", {}))
        # Step 2: setup-uv with enable-cache: true
        self.assertTrue(steps[2]["uses"].startswith("astral-sh/setup-uv@"))
        self.assertEqual(len(steps[2]["uses"].split("@")[1]), 40)
        self.assertEqual(steps[2]["with"]["enable-cache"], "true")

    def test_python_profile_rendered_yaml_uv_without_cache(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is optional and unavailable")

        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "python": {
                "versions": ["3.11"],
                "package_manager": "uv",
                "commands": ["uv run pytest"],
                "cache": False,
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.load(content, Loader=yaml.BaseLoader)
        steps = parsed["jobs"]["ci"]["steps"]
        setup_uv_step = next(s for s in steps if s.get("uses", "").startswith("astral-sh/setup-uv@"))
        self.assertNotIn("with", setup_uv_step)

    def test_python_profile_rendered_yaml_pip_without_cache(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is optional and unavailable")

        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "python": {
                "versions": ["3.11"],
                "package_manager": "pip",
                "commands": ["pytest"],
                "cache": False,
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.load(content, Loader=yaml.BaseLoader)
        steps = parsed["jobs"]["ci"]["steps"]
        setup_python_step = next(s for s in steps if s.get("uses", "").startswith("actions/setup-python@"))
        self.assertNotIn("cache", setup_python_step.get("with", {}))

    def test_python_profile_cache_defaults_to_false_when_direct_render_omits_it(self):
        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "python": {
                "versions": ["3.11"],
                "package_manager": "pip",
                "commands": ["pytest"],
            },
        }
        omitted = installer.render(workflow_spec).decode("utf-8")
        self.assertNotIn('cache: "pip"', omitted)

        explicit_spec = dict(workflow_spec, python=dict(workflow_spec["python"], cache=True))
        explicit = installer.render(explicit_spec).decode("utf-8")
        self.assertIn('cache: "pip"', explicit)

    def test_python_profile_git_lfs_checkout(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is optional and unavailable")

        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "python": {
                "versions": ["3.11"],
                "commands": ["pytest"],
                "lfs": True,
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.load(content, Loader=yaml.BaseLoader)
        checkout_step = parsed["jobs"]["ci"]["steps"][0]
        self.assertEqual(checkout_step.get("with", {}).get("lfs"), "true")

    def test_python_profile_custom_action_shas(self):
        custom_python_sha = "1" * 40
        custom_uv_sha = "2" * 40
        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "setup_python_sha": custom_python_sha,
            "setup_uv_sha": custom_uv_sha,
            "profile": "python",
            "python": {
                "versions": ["3.11"],
                "package_manager": "uv",
                "commands": ["pytest"],
            },
        }
        content = installer.render(workflow_spec).decode("utf-8")
        self.assertIn(f"actions/setup-python@{custom_python_sha}", content)
        self.assertIn(f"astral-sh/setup-uv@{custom_uv_sha}", content)

    def test_rejects_invalid_custom_action_shas(self):
        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "setup_uv_sha": "invalid-sha",
            "profile": "python",
            "python": {
                "versions": ["3.11"],
                "commands": ["pytest"],
            },
        }
        with self.assertRaisesRegex(ValueError, "setup_uv_sha"):
            installer.render(workflow_spec)


class StableStatusCheckReportingTests(unittest.TestCase):
    """Test the status check reporting evaluator across executing, failing, and skipped paths."""

    def test_anchor_succeeds_when_all_jobs_succeed(self):
        needs_context = {
            "ci": {"result": "success", "outputs": {}},
        }
        passed, msg, details = installer.evaluate_status_check(needs_context)
        self.assertTrue(passed)
        self.assertEqual(details["success"], ["ci"])
        self.assertEqual(details["failed"], [])
        self.assertEqual(details["skipped"], [])

    def test_anchor_succeeds_when_upstream_job_skipped(self):
        """Crucial requirement: when paths/matrix are skipped, status check must NOT hang or fail."""
        needs_context = {
            "detect-changes": {"result": "success", "outputs": {"run_ci": "false"}},
            "ci": {"result": "skipped", "outputs": {}},
        }
        passed, msg, details = installer.evaluate_status_check(needs_context)
        self.assertTrue(passed, "Skipped jobs must allow the stable status check anchor to pass")
        self.assertEqual(details["skipped"], ["ci"])
        self.assertEqual(details["failed"], [])

    def test_anchor_fails_when_any_job_fails(self):
        needs_context = {
            "ci": {"result": "failure", "outputs": {}},
        }
        passed, msg, details = installer.evaluate_status_check(needs_context)
        self.assertFalse(passed)
        self.assertIn("ci", details["failed"])

    def test_anchor_fails_when_job_cancelled(self):
        needs_context = {
            "ci": {"result": "cancelled", "outputs": {}},
        }
        passed, msg, details = installer.evaluate_status_check(needs_context)
        self.assertFalse(passed)
        self.assertIn("ci", details["failed"])

    def test_path_matching_evaluator(self):
        patterns = ["src/**", "tests/**", "*.py"]
        # Executing path matches
        self.assertTrue(installer.match_path_patterns(["src/module/core.py"], patterns))
        self.assertTrue(installer.match_path_patterns(["tests/test_foo.py"], patterns))
        self.assertTrue(installer.match_path_patterns(["setup.py"], patterns))
        # Skipped path does not match
        self.assertFalse(installer.match_path_patterns(["docs/README.md", "LICENSE"], patterns))

    def test_path_matching_globstar_expansion(self):
        # **/*.py globstar pattern must match root-level files as well as nested files
        patterns = ["**/*.py"]
        self.assertTrue(installer.match_path_patterns(["main.py"], patterns))
        self.assertTrue(installer.match_path_patterns(["setup.py"], patterns))
        self.assertTrue(installer.match_path_patterns(["src/module.py"], patterns))
        self.assertTrue(installer.match_path_patterns(["src/deep/nested/tool.py"], patterns))
        self.assertFalse(installer.match_path_patterns(["README.md"], patterns))
        self.assertFalse(installer.match_path_patterns(["src/data.json"], patterns))

        # Nested globstar patterns
        deep_patterns = ["tests/**/test_*.py"]
        self.assertTrue(installer.match_path_patterns(["tests/test_foo.py"], deep_patterns))
        self.assertTrue(installer.match_path_patterns(["tests/unit/test_bar.py"], deep_patterns))
        self.assertTrue(installer.match_path_patterns(["tests/unit/nested/test_baz.py"], deep_patterns))
        self.assertFalse(installer.match_path_patterns(["test_foo.py"], deep_patterns))
        self.assertFalse(installer.match_path_patterns(["tests/helper.py"], deep_patterns))

    def test_extract_profile_status_check(self):
        # Profile CI workflow with default status-check
        wf_default = (
            "# sciflow-cicd workflow-template=profile-ci version=1\n"
            "jobs:\n"
            "  ci:\n"
            "    runs-on: ubuntu-24.04\n"
            "  status-check:\n"
            "    name: \"status-check\"\n"
        )
        self.assertEqual(installer.extract_profile_status_check(wf_default), "status-check")

        # Profile CI workflow with custom status-check name
        wf_custom = (
            "# sciflow-cicd workflow-template=profile-ci version=1\n"
            "jobs:\n"
            "  ci:\n"
            "    runs-on: ubuntu-24.04\n"
            "  status-check:\n"
            "    name: \"cmake-mpi-gate\"\n"
        )
        self.assertEqual(installer.extract_profile_status_check(wf_custom), "cmake-mpi-gate")

        # Non-profile workflow without status-check job
        wf_other = "name: Other\njobs:\n  build:\n    runs-on: ubuntu-24.04\n"
        self.assertEqual(installer.extract_profile_status_check(wf_other), "")
        self.assertEqual(installer.extract_profile_status_check(None), "")

    def test_inline_detect_changes_globstar_matching(self):
        """Verify the inline detect-changes script accurately matches main.py against **/*.py and subpaths against tests/**/*.py."""
        wf = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "paths": ["**/*.py", "tests/**/*.py"],
        }
        content = installer.render(wf).decode("utf-8")
        self.assertIn("_glob_to_regex", content)
        self.assertIn("paths_match", content)

        # Execute the exact inline matcher source shared with local evaluation.
        namespace = {}
        exec(installer.INLINE_PATH_MATCHER_SOURCE, namespace)
        inline_match = namespace["paths_match"]

        def run_matching(changed_files, patterns):
            return inline_match(changed_files, patterns)

        # Root file vs **/*.py
        self.assertTrue(run_matching(["main.py"], ["**/*.py"]))
        self.assertTrue(run_matching(["setup.py"], ["**/*.py"]))
        # Intermediate paths vs **/*.py
        self.assertTrue(run_matching(["src/core/main.py"], ["**/*.py"]))
        # Non-matching file vs **/*.py
        self.assertFalse(run_matching(["README.md"], ["**/*.py"]))
        self.assertFalse(run_matching(["docs/manual.rst"], ["**/*.py"]))

        # Subpaths vs tests/**/*.py
        self.assertTrue(run_matching(["tests/unit/test.py"], ["tests/**/*.py"]))
        self.assertTrue(run_matching(["tests/unit/deep/test_core.py"], ["tests/**/*.py"]))
        self.assertTrue(run_matching(["tests/test.py"], ["tests/**/*.py"]))
        # Non-matching file vs tests/**/*.py
        self.assertFalse(run_matching(["test.py"], ["tests/**/*.py"]))
        self.assertFalse(run_matching(["src/unit/test.py"], ["tests/**/*.py"]))

    def test_inspect_remote_workflow_status_check(self):
        """inspect_remote_workflow_status_check retrieves and parses remote workflow status checks from GitHub API contents correctly."""
        class MockApi:
            def __init__(self, responses):
                self.responses = responses
                self.calls = []

            def request(self, method, url, body=None):
                self.calls.append((method, url))
                if url in self.responses:
                    return self.responses[url]
                return 404, {"message": "Not Found"}

        # 1. Status 200 base64 payload for profile workflow with default status-check
        wf_default_yaml = (
            "# sciflow-cicd workflow-template=profile-ci version=1\n"
            "name: Python CI\n"
            "jobs:\n"
            "  ci:\n"
            "    runs-on: ubuntu-24.04\n"
            "  status-check:\n"
            "    name: \"status-check\"\n"
        )
        b64_default = base64.b64encode(wf_default_yaml.encode("utf-8")).decode("ascii")

        # 2. Status 200 base64 payload with custom check name
        wf_custom_yaml = (
            "# sciflow-cicd workflow-template=profile-ci version=1\n"
            "name: CMake MPI CI\n"
            "jobs:\n"
            "  ci:\n"
            "    runs-on: ubuntu-24.04\n"
            "  status-check:\n"
            "    name: \"cmake-mpi-quality-gate\"\n"
        )
        b64_custom = base64.b64encode(wf_custom_yaml.encode("utf-8")).decode("ascii")

        # 3. Non-profile workflow
        wf_other_yaml = (
            "name: Other Workflow\n"
            "jobs:\n"
            "  build:\n"
            "    runs-on: ubuntu-24.04\n"
        )
        b64_other = base64.b64encode(wf_other_yaml.encode("utf-8")).decode("ascii")

        api = MockApi({
            "/repos/acme/project/contents/.github/workflows/ci.yml": (200, {
                "encoding": "base64",
                "content": b64_default,
            }),
            "/repos/acme/project/contents/.github/workflows/cmake.yml?ref=feature-branch": (200, {
                "encoding": "base64",
                "content": b64_custom,
            }),
            "/repos/acme/project/contents/.github/workflows/build.yml": (200, {
                "encoding": "base64",
                "content": b64_other,
            }),
            "/repos/acme/project/contents/.github/workflows/invalid.yml": (200, {
                "encoding": "none",
                "content": "not base64",
            }),
        })

        # Test valid default check
        check1 = installer.inspect_remote_workflow_status_check(api, "acme/project", ".github/workflows/ci.yml")
        self.assertEqual(check1, "status-check")

        # Test valid custom check with ref parameter
        check2 = installer.inspect_remote_workflow_status_check(api, "acme/project", ".github/workflows/cmake.yml", ref="feature-branch")
        self.assertEqual(check2, "cmake-mpi-quality-gate")
        self.assertIn(("GET", "/repos/acme/project/contents/.github/workflows/cmake.yml?ref=feature-branch"), api.calls)

        # Test absent file (404) returns None
        check_absent = installer.inspect_remote_workflow_status_check(api, "acme/project", ".github/workflows/missing.yml")
        self.assertIsNone(check_absent)

        # Test non-profile workflow returns None
        check_other = installer.inspect_remote_workflow_status_check(api, "acme/project", ".github/workflows/build.yml")
        self.assertIsNone(check_other)

        # Test invalid encoding returns None
        check_invalid = installer.inspect_remote_workflow_status_check(api, "acme/project", ".github/workflows/invalid.yml")
        self.assertIsNone(check_invalid)

        # Test missing / None arguments return None
        self.assertIsNone(installer.inspect_remote_workflow_status_check(None, "acme/project", ".github/workflows/ci.yml"))
        self.assertIsNone(installer.inspect_remote_workflow_status_check(api, None, ".github/workflows/ci.yml"))
        self.assertIsNone(installer.inspect_remote_workflow_status_check(api, "acme/project", None))

        # Test API exception handling
        class BrokenApi:
            def request(self, *args, **kwargs):
                raise RuntimeError("network failure")

        self.assertIsNone(installer.inspect_remote_workflow_status_check(BrokenApi(), "acme/project", ".github/workflows/ci.yml"))

    def test_status_check_inline_script_evaluation(self):
        """STATUS_CHECK_INLINE_SCRIPT matches the expected status check evaluation behavior."""
        script = installer.STATUS_CHECK_INLINE_SCRIPT
        self.assertIsInstance(script, str)
        self.assertIn("NEEDS_CONTEXT", script)
        self.assertIn("Status check results", script)

        # 1. Success case: all jobs succeeded
        needs_all_success = json.dumps({
            "ci": {"result": "success"},
            "detect-changes": {"result": "success"},
        })
        res = subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "NEEDS_CONTEXT": needs_all_success},
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Status check passed.", res.stdout)
        self.assertIn("success: ['ci', 'detect-changes']", res.stdout)

        # 2. Success case with skipped jobs
        needs_with_skipped = json.dumps({
            "ci": {"result": "skipped"},
            "detect-changes": {"result": "success"},
        })
        res = subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "NEEDS_CONTEXT": needs_with_skipped},
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Status check passed.", res.stdout)
        self.assertIn("skipped: ['ci']", res.stdout)

        # 3. Failure case: one or more jobs failed
        needs_with_failure = json.dumps({
            "ci": {"result": "failure"},
            "detect-changes": {"result": "success"},
        })
        res = subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "NEEDS_CONTEXT": needs_with_failure},
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Status check failed: ['ci']", res.stderr)

        # 4. Failure case: job cancelled
        needs_with_cancelled = json.dumps({
            "ci": {"result": "cancelled"},
        })
        res = subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "NEEDS_CONTEXT": needs_with_cancelled},
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Status check failed: ['ci']", res.stderr)

        # 5. Empty needs context passes
        res = subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "NEEDS_CONTEXT": "{}"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Status check passed.", res.stdout)


class ProfileWorkflowStagingTests(unittest.TestCase):
    """Test staging profile workflows through install and install_selected."""

    def test_install_stages_profile_workflow_into_pr(self):
        api = WorkflowApi()
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
                        "commands": ["pytest"],
                    },
                }
            ],
        }
        dry_run = installer.install(api, "acme/project", policy, apply=False)
        self.assertEqual(len(dry_run["changes"]), 1)
        self.assertEqual(api.writes, [])

        result = installer.install(api, "acme/project", policy, apply=True)
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(len(api.prs), 1)

    def test_run_policy_dry_run_and_apply_v2_python_ci_profile(self):
        from setup_github_repo import run_policy
        api = WorkflowApi()
        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {
                    "name": "workflows",
                    "workflows": [
                        {
                            "template": "profile-ci",
                            "template_version": 1,
                            "profile": "python",
                            "filename": "ci.yml",
                            "triggers": ["pull_request"],
                            "checkout_sha": CHECKOUT,
                            "python": {
                                "versions": ["3.10", "3.11"],
                                "package_manager": "uv",
                                "commands": ["uv run pytest"],
                                "lfs": True,
                                "cache": True,
                            },
                        }
                    ],
                }
            ],
        }
        dry_run = run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(dry_run["mode"], "dry-run")
        self.assertEqual(len(dry_run["changes"]), 1)
        change = dry_run["changes"][0]
        self.assertEqual(change["stage"], "workflows")
        self.assertEqual(change["path"], ".github/workflows/ci.yml")
        self.assertEqual(change["status"], "planned")
        self.assertIsNotNone(change.get("desired_sha256"))

        # Apply using the accepted dry run
        apply_res = run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(apply_res["mode"], "apply")
        self.assertIn("setup_pull_request", apply_res)
        self.assertEqual(len(api.prs), 1)


class ScientificPythonDetectionTests(unittest.TestCase):
    """Test Python repository detection, package manager detection, and Git LFS detection."""

    def test_detect_python_project_from_files(self):
        import tempfile
        for filename in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile"):
            with tempfile.TemporaryDirectory() as tmpdir:
                path = pathlib.Path(tmpdir) / filename
                path.write_text("# marker\n", encoding="utf-8")
                self.assertTrue(installer.detect_python_project(root_dir=tmpdir), f"Failed detecting {filename}")

    def test_detect_python_project_negative(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            (pathlib.Path(tmpdir) / "README.md").write_text("# Hello\n", encoding="utf-8")
            self.assertFalse(installer.detect_python_project(root_dir=tmpdir))

    def test_detect_python_project_from_api_languages(self):
        class LanguageApi:
            def request(self, method, path, payload=None):
                if path == "/repos/acme/project/languages":
                    return 200, {"Python": 12000, "Shell": 500}
                return 404, None

        self.assertTrue(installer.detect_python_project(api=LanguageApi(), repository="acme/project"))

    def test_detect_python_project_from_repo_metadata(self):
        class RepoApi:
            def request(self, method, path, payload=None):
                if path == "/repos/acme/project":
                    return 200, {"language": "Python"}
                return 404, None

        self.assertTrue(installer.detect_python_project(api=RepoApi(), repository="acme/project"))

    def test_detect_python_package_manager(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            (pathlib.Path(tmpdir) / "uv.lock").write_text("", encoding="utf-8")
            self.assertEqual(installer.detect_python_package_manager(root_dir=tmpdir), "uv")

        with tempfile.TemporaryDirectory() as tmpdir:
            (pathlib.Path(tmpdir) / "requirements.txt").write_text("pytest\n", encoding="utf-8")
            self.assertEqual(installer.detect_python_package_manager(root_dir=tmpdir), "pip")

    def test_detect_git_lfs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            (pathlib.Path(tmpdir) / ".gitattributes").write_text("*.dat filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8")
            self.assertTrue(installer.detect_git_lfs(root_dir=tmpdir))

        with tempfile.TemporaryDirectory() as tmpdir:
            (pathlib.Path(tmpdir) / ".gitattributes").write_text("*.txt text\n", encoding="utf-8")
            self.assertFalse(installer.detect_git_lfs(root_dir=tmpdir))

    def test_python_profile_system_setup_mode_omits_setup_actions(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is optional and unavailable")

        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "python": {
                "versions": ["3.11"],
                "python_setup_mode": "system",
                "commands": ["pytest"],
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.safe_load(content)
        ci_steps = parsed["jobs"]["ci"]["steps"]
        step_uses = [s.get("uses", "") for s in ci_steps if "uses" in s]

        self.assertTrue(any(u.startswith("actions/checkout@") for u in step_uses))
        self.assertFalse(any("setup-python" in u for u in step_uses), "actions/setup-python must not be rendered in system mode")
        self.assertFalse(any("setup-uv" in u for u in step_uses), "setup-uv must not be rendered in system mode")

    def test_python_profile_invalid_setup_mode_raises(self):
        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "python": {
                "versions": ["3.11"],
                "python_setup_mode": "conda-custom",
                "commands": ["pytest"],
            },
        }
        with self.assertRaisesRegex(ValueError, "python_setup_mode"):
            installer.render(workflow_spec)

    def test_python_profile_install_commands_rendered_before_lint_and_tests(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is optional and unavailable")

        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "python": {
                "versions": ["3.11"],
                "install_commands": ["pip install -r requirements.txt", "pip install -e ."],
                "lint_commands": ["flake8 ."],
                "commands": ["pytest -v"],
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.safe_load(content)
        ci_steps = parsed["jobs"]["ci"]["steps"]
        run_cmds = [s["run"] for s in ci_steps if "run" in s]

        self.assertEqual(run_cmds, [
            "pip install -r requirements.txt",
            "pip install -e .",
            "flake8 .",
            "pytest -v",
        ])

    def test_python_profile_invalid_install_commands_raises(self):
        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "python": {
                "versions": ["3.11"],
                "install_commands": ["   "],
                "commands": ["pytest"],
            },
        }
        with self.assertRaisesRegex(ValueError, "install_commands"):
            installer.render(workflow_spec)

    def test_python_profile_post_merge_job_rendered_when_push_in_triggers(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is optional and unavailable")

        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request", "push"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "runs_on": "self-hosted",
            "python": {
                "versions": ["3.11", "3.12"],
                "commands": ["pytest"],
                "post_merge_commands": ["python -m twine upload dist/*"],
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.safe_load(content)

        self.assertIn("post-merge", parsed["jobs"])
        post_merge_job = parsed["jobs"]["post-merge"]
        self.assertEqual(post_merge_job["runs-on"], "self-hosted")
        self.assertEqual(post_merge_job["if"], "github.event_name == 'push'")
        self.assertEqual(post_merge_job["permissions"]["contents"], "read")

        pm_steps = post_merge_job["steps"]
        self.assertTrue(pm_steps[0]["uses"].startswith(f"actions/checkout@{CHECKOUT}"))
        self.assertEqual(pm_steps[1]["run"], "python -m twine upload dist/*")

        # Verify status-check does NOT include post-merge in needs
        status_check = parsed["jobs"]["status-check"]
        needs = status_check["needs"]
        if isinstance(needs, str):
            needs_list = [n.strip() for n in needs.strip("[]").split(",")]
        else:
            needs_list = list(needs)
        self.assertNotIn("post-merge", needs_list)

    def test_python_profile_post_merge_job_omitted_without_push_trigger(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is optional and unavailable")

        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "python": {
                "versions": ["3.11"],
                "commands": ["pytest"],
                "post_merge_commands": ["echo deploy"],
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.safe_load(content)
        self.assertNotIn("post-merge", parsed["jobs"])

    def test_python_profile_control_jobs_stay_on_ubuntu_24_04(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is optional and unavailable")

        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "paths": ["src/**"],
            "runs_on": "self-hosted",
            "python": {
                "versions": ["3.11"],
                "commands": ["pytest"],
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.safe_load(content)
        self.assertEqual(parsed["jobs"]["detect-changes"]["runs-on"], "ubuntu-24.04")
        self.assertEqual(parsed["jobs"]["status-check"]["runs-on"], "ubuntu-24.04")
        self.assertEqual(parsed["jobs"]["ci"]["runs-on"], "self-hosted")

    def test_install_python_profile_system_mode_does_not_pin_setup_python(self):
        from unittest.mock import patch
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
                        "versions": ["3.11"],
                        "python_setup_mode": "system",
                        "commands": ["pytest"],
                    },
                }
            ],
        }
        with patch.object(installer, "stage_workflows") as mock_stage:
            mock_stage.return_value = {"status": "planned", "changes": []}
            installer.install(WorkflowApi(), "acme/project", policy, apply=False)
            self.assertTrue(mock_stage.called)
            pins = mock_stage.call_args[0][3]
            pin_actions = [p[0] for p in pins]
            self.assertIn("actions/checkout", pin_actions)
            self.assertNotIn("actions/setup-python", pin_actions)
            self.assertNotIn("astral-sh/setup-uv", pin_actions)

    def test_install_python_profile_uv_mode_pins_setup_uv(self):
        from unittest.mock import patch
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
                        "versions": ["3.11"],
                        "python_setup_mode": "uv",
                        "commands": ["uv run pytest"],
                    },
                }
            ],
        }
        with patch.object(installer, "stage_workflows") as mock_stage:
            mock_stage.return_value = {"status": "planned", "changes": []}
            installer.install(WorkflowApi(), "acme/project", policy, apply=False)
            self.assertTrue(mock_stage.called)
            pins = mock_stage.call_args[0][3]
            pin_actions = [p[0] for p in pins]
            self.assertIn("actions/checkout", pin_actions)
            self.assertIn("actions/setup-python", pin_actions)
            self.assertIn("astral-sh/setup-uv", pin_actions)

    def test_python_profile_post_merge_commands_at_workflow_level(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is optional and unavailable")

        workflow_spec = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request", "push"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "post_merge_commands": ["make publish"],
            "python": {
                "versions": ["3.11"],
                "commands": ["pytest"],
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.safe_load(content)
        self.assertIn("post-merge", parsed["jobs"])
        self.assertEqual(parsed["jobs"]["post-merge"]["steps"][1]["run"], "make publish")


if __name__ == "__main__":
    unittest.main()
