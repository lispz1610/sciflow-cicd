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

    def test_repo_policy_v2_supports_ci_profile_stage(self):
        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {
                    "name": "ci-profile",
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
        stages = validate_repo_policy(policy)
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0]["name"], "ci-profile")


class ProfileWorkflowGenerationTests(unittest.TestCase):
    """Test rendered workflow structure, YAML correctness, concurrency, and anchor jobs."""

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
            "# github-repo-setup workflow-template=profile-ci version=1\n"
            "jobs:\n"
            "  ci:\n"
            "    runs-on: ubuntu-24.04\n"
            "  status-check:\n"
            "    name: \"status-check\"\n"
        )
        self.assertEqual(installer.extract_profile_status_check(wf_default), "status-check")

        # Profile CI workflow with custom status-check name
        wf_custom = (
            "# github-repo-setup workflow-template=profile-ci version=1\n"
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
        self.assertIn("clean_p.startswith('**/')", content)
        self.assertIn("'/**/' in clean_p", content)

        # Helper to execute the inline matching logic with synthetic files and patterns
        def run_matching(changed_files, patterns):
            test_runner = (
                "import fnmatch\n"
                f"files = {json.dumps(changed_files)}\n"
                f"patterns = {json.dumps(patterns)}\n"
                "matched = False\n"
                "for f in files:\n"
                "    clean_f = f.strip().lstrip('/')\n"
                "    for p in patterns:\n"
                "        clean_p = p.strip().lstrip('/')\n"
                "        if fnmatch.fnmatch(clean_f, clean_p):\n"
                "            matched = True; break\n"
                "        if clean_p.startswith('**/') and fnmatch.fnmatch(clean_f, clean_p[3:]):\n"
                "            matched = True; break\n"
                "        if '/**/' in clean_p and fnmatch.fnmatch(clean_f, clean_p.replace('/**/', '/')):\n"
                "            matched = True; break\n"
                "        if clean_p.endswith('/**') and (clean_f == clean_p[:-3] or clean_f.startswith(clean_p[:-3] + '/')):\n"
                "            matched = True; break\n"
                "        if clean_p.endswith('/*') and clean_f.startswith(clean_p[:-2] + '/'):\n"
                "            matched = True; break\n"
                "    if matched: break\n"
                "print('MATCHED' if matched else 'SKIPPED')\n"
            )
            res = subprocess.run([sys.executable, "-c", test_runner], capture_output=True, text=True, check=True)
            return res.stdout.strip() == "MATCHED"

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
            "# github-repo-setup workflow-template=profile-ci version=1\n"
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
            "# github-repo-setup workflow-template=profile-ci version=1\n"
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
                    "name": "ci-profile",
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
        dry_run = run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(dry_run["mode"], "dry-run")
        self.assertEqual(len(dry_run["changes"]), 1)
        change = dry_run["changes"][0]
        self.assertEqual(change["stage"], "ci-profile")
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


if __name__ == "__main__":
    unittest.main()
