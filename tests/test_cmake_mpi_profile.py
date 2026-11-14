"""Tests for CMake and MPI CI profile with smoke tests, ccache, and device boundary demarcation."""

import base64
import io
import json
import os
import pathlib
import sys
import tempfile
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

import install_github_workflows as installer
from setup_github_repo import run_policy, validate_policy
from interactive_workflows_rulesets import prompt_workflows_stage, prompt_cmake_mpi_ci_options
from interactive import Session
from test_install_github_workflows import WorkflowApi

CHECKOUT = "11bd71901bbe5b1630ceea73d27597364c9af683"


class CMakeMPIProjectDetectionTests(unittest.TestCase):
    """Test detection of CMake and MPI / OpenCL projects via API and local workspace."""

    def test_detect_cmake_mpi_project_from_api_with_mpi_and_opencl(self):
        cmake_content = (
            "cmake_minimum_required(VERSION 3.20)\n"
            "project(ScientificSimulation CXX)\n"
            "find_package(MPI REQUIRED)\n"
            "find_package(OpenCL REQUIRED)\n"
            "add_executable(sim src/main.cpp)\n"
            "target_link_libraries(sim PRIVATE MPI::MPI_CXX OpenCL::OpenCL)\n"
        )
        api = MagicMock()
        api.request.return_value = (
            200,
            {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(cmake_content.encode("utf-8")).decode("ascii"),
                "sha": "1234567890abcdef1234567890abcdef12345678",
            },
        )
        detected = installer.detect_cmake_mpi_project(api, "owner/sim-repo", "main")
        self.assertTrue(detected["detected"])
        self.assertTrue(detected["has_mpi"])
        self.assertTrue(detected["has_opencl"])
        self.assertIn("libopenmpi-dev", detected["runner_dependencies"])
        self.assertIn("openmpi-bin", detected["runner_dependencies"])
        self.assertIn("pocl-opencl-icd", detected["runner_dependencies"])
        self.assertEqual(detected["ranks"], [1, 2])

    def test_detect_cmake_mpi_project_from_local_workspace(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cmake_file = pathlib.Path(tmp_dir) / "CMakeLists.txt"
            cmake_file.write_text("cmake_minimum_required(VERSION 3.20)\nfind_package(MPI REQUIRED)\n")
            detected = installer.detect_cmake_mpi_project(workspace_dir=tmp_dir)
            self.assertTrue(detected["detected"])
            self.assertTrue(detected["has_mpi"])
            self.assertFalse(detected["has_opencl"])
            self.assertEqual(detected["ranks"], [1, 2])

    def test_detect_cmake_mpi_project_not_found(self):
        api = MagicMock()
        api.request.return_value = (404, {"message": "Not Found"})
        with tempfile.TemporaryDirectory() as tmp_dir:
            detected = installer.detect_cmake_mpi_project(api, "owner/empty-repo", "main", workspace_dir=tmp_dir)
            self.assertFalse(detected["detected"])
            self.assertFalse(detected["has_mpi"])


class CMakeMPIProfileWorkflowRenderingTests(unittest.TestCase):
    """Test rendered CMake/MPI CI profile workflow YAML, ccache, smoke tests, and demarcation."""

    def test_render_cmake_mpi_profile_default_ranks_and_demarcation(self):
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
                "build_commands": ["cmake -B build -S .", "cmake --build build"],
                "test_commands": ["ctest --test-dir build --output-on-failure"],
                "runner_dependencies": ["libopenmpi-dev", "openmpi-bin"],
                "ccache": True,
            },
        }
        content = installer.render(workflow_spec)
        text = content.decode("utf-8")
        parsed = yaml.load(content, Loader=yaml.BaseLoader)

        # Header documentation contains explicit evidence demarcation
        self.assertIn("EVIDENCE DEMARCATION", text)
        self.assertIn("GitHub Actions hosted runners execute on virtual CPU-only environments", text)
        self.assertIn("does NOT verify execution on", text)
        self.assertIn("physical GPU hardware", text)

        # Job configuration
        self.assertIn("ci", parsed["jobs"])
        ci_job = parsed["jobs"]["ci"]
        self.assertEqual(ci_job["strategy"]["matrix"]["ranks"], ["1", "2"])

        # Steps inspection
        step_names = [step.get("name", "") for step in ci_job["steps"]]
        step_runs = [step.get("run", "") for step in ci_job["steps"] if "run" in step]
        combined_runs = "\n".join(step_runs)

        # Evidence demarcation step with GitHub Actions notice annotation
        self.assertTrue(any("Demarcate" in name for name in step_names))
        self.assertIn("::notice title=Device Boundary Demarcation::", combined_runs)
        self.assertIn("Unverified: Real GPU/accelerator hardware execution", combined_runs)

        # Compiler cache (ccache) configuration
        self.assertTrue(any("ccache" in step.get("name", "").lower() or "ccache" in step.get("run", "") for step in ci_job["steps"]))
        self.assertIn("GITHUB_PATH", combined_runs)
        self.assertIn("CCACHE_DIR", combined_runs)

        # Runner dependencies
        self.assertIn("apt-get install", combined_runs)
        self.assertIn("libopenmpi-dev", combined_runs)
        self.assertIn("openmpi-bin", combined_runs)

        # Build and test commands
        self.assertIn("cmake -B build -S .", combined_runs)
        self.assertIn("cmake --build build", combined_runs)
        self.assertIn("ctest --test-dir build", combined_runs)

        # Test step exposes MPI_RANKS for dynamic matrix rank parameterization
        test_step = next(step for step in ci_job["steps"] if "ctest --test-dir build" in step.get("run", ""))
        self.assertEqual(test_step.get("env", {}).get("MPI_RANKS"), "${{ matrix.ranks }}")

        # Stable status check anchor
        self.assertIn("status-check", parsed["jobs"])
        self.assertEqual(parsed["jobs"]["status-check"]["if"], "always()")

    def test_render_cmake_mpi_profile_with_opencl_and_demarcation(self):
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
                "ranks": [1, 2, 4],
                "opencl": True,
            },
        }
        content = installer.render(workflow_spec)
        text = content.decode("utf-8")
        parsed = yaml.load(content, Loader=yaml.BaseLoader)

        ci_job = parsed["jobs"]["ci"]
        self.assertEqual(ci_job["strategy"]["matrix"]["ranks"], ["1", "2", "4"])

        step_runs = [step.get("run", "") for step in ci_job["steps"] if "run" in step]
        combined_runs = "\n".join(step_runs)

        # OpenCL runner packages present
        self.assertIn("pocl-opencl-icd", combined_runs)
        self.assertIn("clinfo", combined_runs)

        # OpenCL device boundary demarcation
        self.assertIn("::notice title=OpenCL Device Boundary::", combined_runs)
        self.assertIn("OpenCL execution uses CPU POCL runtime", combined_runs)

    def test_render_cmake_mpi_profile_manual_build(self):
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
                "manual_build": True,
                "build_commands": ["make -j4"],
                "test_commands": ["mpirun -n 1 ./test_bin", "mpirun -n 2 ./test_bin"],
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.load(content, Loader=yaml.BaseLoader)
        ci_job = parsed["jobs"]["ci"]

        step_runs = [step.get("run", "") for step in ci_job["steps"] if "run" in step]
        combined_runs = "\n".join(step_runs)
        self.assertIn("make -j4", combined_runs)
        self.assertIn("mpirun -n 1 ./test_bin", combined_runs)
        self.assertIn("mpirun -n 2 ./test_bin", combined_runs)


class CMakeMPIInteractiveSetupTests(unittest.TestCase):
    """Test interactive prompting and CMake/MPI workflow staging."""

    def test_prompt_workflows_stage_selects_cmake_mpi_profile(self):
        # 1. Confirm staging workflows: y
        # 2. Select CMake / MPI CI (option 4) and dependency review (option 2): "4, 2"
        # 3. Manual build: n
        # 4. CMake build commands: default (newline)
        # 5. MPI rank counts: "1, 2, 4"
        # 6. Test commands: default (newline)
        # 7. Runner dependencies: default (newline)
        # 8. Compiler cache (ccache): default (newline)
        # 9. OpenCL compilation: y
        user_input = "y\n4, 2\nn\n\n1, 2, 4\n\n\n\ny\n"
        in_stream = io.StringIO(user_input)
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(stage["name"], "workflows")
        self.assertEqual(len(stage["workflows"]), 2)

        cmake_wf = next(w for w in stage["workflows"] if w.get("profile") == "cmake-mpi")
        self.assertEqual(cmake_wf["template"], "profile-ci")
        self.assertEqual(cmake_wf["cmake_mpi"]["ranks"], [1, 2, 4])
        self.assertTrue(cmake_wf["cmake_mpi"]["opencl"])
        self.assertFalse(cmake_wf["cmake_mpi"]["manual_build"])
        self.assertTrue(cmake_wf["cmake_mpi"]["ccache"])

        # Validate with policy validator
        policy = {"version": 1, "stages": [stage]}
        self.assertEqual(validate_policy(policy), [stage])


class CMakeMPIHeadlessStagingTests(unittest.TestCase):
    """Test Headless Mode Dry-Run and Setup Pull Request staging for cmake-mpi profile."""

    def test_headless_dry_run_and_apply_cmake_mpi_ci_profile(self):
        api = WorkflowApi()
        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {
                    "name": "ci-profile",
                    "profile": "cmake-mpi",
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT,
                    "cmake_mpi": {
                        "ranks": [1, 2],
                        "build_commands": ["cmake -B build -S .", "cmake --build build"],
                        "test_commands": ["ctest --test-dir build --output-on-failure"],
                        "runner_dependencies": ["libopenmpi-dev", "openmpi-bin"],
                        "ccache": True,
                        "opencl": False,
                        "manual_build": False,
                    },
                }
            ],
        }

        # Dry-Run result
        dry_run = run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(dry_run["mode"], "dry-run")
        self.assertEqual(len(dry_run["changes"]), 1)
        change = dry_run["changes"][0]
        self.assertEqual(change["path"], ".github/workflows/ci.yml")
        self.assertEqual(change["status"], "planned")
        self.assertEqual(api.writes, [])

        # Apply into draft Setup Pull Request
        applied = run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(applied["mode"], "apply")
        self.assertEqual(len(api.prs), 1)
        self.assertEqual(applied["setup_pull_request"], "https://github.com/acme/project/pull/1")

    def test_cmake_mpi_with_path_filtering_in_headless_mode(self):
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
            "paths": ["CMakeLists.txt", "src/**", "include/**"],
            "cmake_mpi": {
                "ranks": [1, 2],
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.load(content, Loader=yaml.BaseLoader)
        self.assertIn("detect-changes", parsed["jobs"])
        self.assertIn("ci", parsed["jobs"])
        self.assertIn("status-check", parsed["jobs"])
        self.assertEqual(parsed["jobs"]["status-check"]["needs"], ["detect-changes", "ci"])


class CMakeMPIValidationAndResumeTests(unittest.TestCase):
    """Test validation errors and interactive resume preselection."""

    def test_rejects_invalid_ranks(self):
        for invalid_ranks in ([0], [-2], ["one"], [1, 1], []):
            with self.subTest(ranks=invalid_ranks):
                spec = {
                    "template": "profile-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT,
                    "profile": "cmake-mpi",
                    "cmake_mpi": {"ranks": invalid_ranks},
                }
                with self.assertRaises(ValueError):
                    installer.render(spec)

    def test_rejects_empty_or_non_string_build_commands(self):
        for invalid_cmds in ([], ["   "], [123]):
            with self.subTest(cmds=invalid_cmds):
                spec = {
                    "template": "profile-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT,
                    "profile": "cmake-mpi",
                    "cmake_mpi": {"build_commands": invalid_cmds},
                }
                with self.assertRaises(ValueError):
                    installer.render(spec)

    def test_resume_mode_preselects_cmake_mpi_profile(self):
        session = Session(repository="acme/project", api=MagicMock())
        session.resume_mode = True
        session.defaults = {
            "workflows": [
                {"template": "profile-ci", "profile": "cmake-mpi"},
                {"template": "dependency-review"},
            ]
        }
        # Hit Enter for confirmation (uses default: True)
        # Hit Enter for multichoice (uses preselected defaults: option 3 and option 2)
        # Hit Enter for manual build (default n)
        # Hit Enter for build commands (default)
        # Hit Enter for rank counts (default)
        # Hit Enter for test commands (default)
        # Hit Enter for runner dependencies (default)
        # Hit Enter for ccache (default)
        # Hit Enter for opencl (default)
        user_input = "\n\n\n\n\n\n\n\n\n"
        in_stream = io.StringIO(user_input)
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(session=session, in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(len(stage["workflows"]), 2)
        templates = [w.get("template") for w in stage["workflows"]]
        self.assertIn("profile-ci", templates)
        self.assertIn("dependency-review", templates)

    def test_prompt_cmake_mpi_ci_options_interactive_flow(self):
        # Manual build: y
        # Build commands: "ninja"
        # Rank counts: "1, 2, 8"
        # Test commands: "ctest"
        # Runner deps: "openmpi-bin"
        # Compiler cache: y
        # OpenCL: n
        user_input = "y\nninja\n1, 2, 8\nctest\nopenmpi-bin\ny\nn\n"
        in_stream = io.StringIO(user_input)
        out_stream = io.StringIO()

        opts = prompt_cmake_mpi_ci_options(in_stream=in_stream, out_stream=out_stream)
        self.assertTrue(opts["manual_build"])
        self.assertEqual(opts["build_commands"], ["ninja"])
        self.assertEqual(opts["ranks"], [1, 2, 8])
        self.assertEqual(opts["test_commands"], ["ctest"])
        self.assertEqual(opts["runner_dependencies"], ["openmpi-bin"])
        self.assertTrue(opts["ccache"])
        self.assertFalse(opts["opencl"])

    def test_prompt_workflows_stage_selects_both_python_and_cmake_profiles_without_collision(self):
        # 1. Confirm staging workflows: y
        # 2. Select both Scientific Python CI (option 3) and CMake / MPI CI (option 4): "3, 4"
        # Python prompts:
        # 3. Python versions: "3.11, 3.12"
        # 4. Package manager: pip
        # 5. Test commands: default
        # 6. Git LFS: n
        # 7. Lint commands: skip (blank)
        # 8. Dependency cache: y
        # CMake prompts:
        # 9. Manual build: n
        # 10. CMake build commands: default
        # 11. MPI rank counts: default
        # 12. Test commands: default
        # 13. Runner dependencies: default
        # 14. Compiler cache: default
        # 15. OpenCL: n
        user_input = "y\n3, 4\n3.11, 3.12\npip\n\nn\n\ny\nn\n\n\n\n\n\nn\n"
        in_stream = io.StringIO(user_input)
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(len(stage["workflows"]), 2)

        py_wf = next(w for w in stage["workflows"] if w.get("profile") == "python")
        cmake_wf = next(w for w in stage["workflows"] if w.get("profile") == "cmake-mpi")

        # Distinct filenames avoid collision
        self.assertEqual(py_wf["filename"], "python-ci.yml")
        self.assertEqual(cmake_wf["filename"], "cmake-ci.yml")
        self.assertNotEqual(py_wf["filename"], cmake_wf["filename"])

        # Validate with policy validator
        policy = {"version": 1, "stages": [stage]}
        self.assertEqual(validate_policy(policy), [stage])


if __name__ == "__main__":
    unittest.main()
