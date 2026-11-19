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
            "runs_on": "ubuntu-24.04",
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
        self.assertEqual(test_step.get("env", {}).get("OMP_NUM_THREADS"), "2")

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
            "runs_on": "ubuntu-24.04",
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

    def test_render_cmake_mpi_self_hosted_default_and_demarcation(self):
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
            },
        }
        content = installer.render(workflow_spec)
        text = content.decode("utf-8")
        parsed = yaml.load(content, Loader=yaml.BaseLoader)

        ci_job = parsed["jobs"]["ci"]
        self.assertEqual(ci_job["runs-on"], "self-hosted")

        # Demarcation mentions self-hosted runner execution
        self.assertIn("Self-hosted runners execute on user-provisioned infrastructure", text)
        self.assertNotIn("GitHub Actions hosted runners execute on virtual CPU-only environments", text)

        step_runs = [step.get("run", "") for step in ci_job["steps"] if "run" in step]
        combined_runs = "\n".join(step_runs)
        self.assertIn("::notice title=Device Boundary Demarcation::GitHub Actions executes on self-hosted runner.", combined_runs)
        self.assertIn("Observed: Host compilation and CPU execution on self-hosted runner with ${{ matrix.ranks }} MPI rank(s).", combined_runs)

        # By default on self-hosted runners, install_system_deps is False (no apt-get)
        self.assertNotIn("sudo apt-get", combined_runs)
        self.assertNotIn("apt-get install", combined_runs)

        # Control jobs stay on ubuntu-24.04
        status_check = parsed["jobs"]["status-check"]
        self.assertEqual(status_check["runs-on"], "ubuntu-24.04")

    def test_render_cmake_mpi_custom_runs_on_list_and_control_pinning(self):
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
            "runs_on": ["self-hosted", "linux", "hpc-gpu"],
            "paths": ["src/**"],
            "cmake_mpi": {
                "ranks": [1, 2],
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.load(content, Loader=yaml.BaseLoader)

        ci_job = parsed["jobs"]["ci"]
        self.assertEqual(ci_job["runs-on"], ["self-hosted", "linux", "hpc-gpu"])

        # Control jobs remain pinned to ubuntu-24.04
        detect_job = parsed["jobs"]["detect-changes"]
        self.assertEqual(detect_job["runs-on"], "ubuntu-24.04")
        status_check = parsed["jobs"]["status-check"]
        self.assertEqual(status_check["runs-on"], "ubuntu-24.04")

    def test_render_cmake_mpi_fork_execution_guard(self):
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
            "runs_on": "self-hosted",
            "is_public": True,
            "cmake_mpi": {
                "ranks": [1, 2],
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.load(content, Loader=yaml.BaseLoader)
        ci_job = parsed["jobs"]["ci"]
        self.assertEqual(ci_job["if"], "github.event.pull_request.head.repo.full_name == github.repository")

    def test_render_cmake_mpi_opt_in_system_deps_on_self_hosted(self):
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
            "runs_on": "self-hosted",
            "cmake_mpi": {
                "ranks": [1],
                "install_system_deps": True,
                "runner_dependencies": ["libopenmpi-dev"],
                "ccache": True,
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.load(content, Loader=yaml.BaseLoader)
        ci_job = parsed["jobs"]["ci"]

        step_runs = [step.get("run", "") for step in ci_job["steps"] if "run" in step]
        combined_runs = "\n".join(step_runs)
        self.assertIn("sudo apt-get update && sudo apt-get install -y --no-install-recommends libopenmpi-dev", combined_runs)
        self.assertIn("sudo apt-get install -y --no-install-recommends ccache", combined_runs)

    def test_render_cmake_mpi_opt_out_system_deps_on_hosted(self):
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
            "runs_on": "ubuntu-24.04",
            "cmake_mpi": {
                "ranks": [1],
                "install_system_deps": False,
                "ccache": True,
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.load(content, Loader=yaml.BaseLoader)
        ci_job = parsed["jobs"]["ci"]

        step_runs = [step.get("run", "") for step in ci_job["steps"] if "run" in step]
        combined_runs = "\n".join(step_runs)
        self.assertNotIn("sudo apt-get", combined_runs)
        self.assertNotIn("apt-get install", combined_runs)

    def test_render_cmake_mpi_compilers_matrix_and_env_setting(self):
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
            "runs_on": "ubuntu-24.04",
            "cmake_mpi": {
                "ranks": [1, 2],
                "compilers": ["gcc", "clang"],
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.load(content, Loader=yaml.BaseLoader)
        ci_job = parsed["jobs"]["ci"]

        self.assertEqual(ci_job["name"], "CMake MPI CI (${{ matrix.compiler }}, ${{ matrix.ranks }}-rank)")
        self.assertEqual(ci_job["strategy"]["matrix"]["compiler"], ["gcc", "clang"])
        self.assertEqual(ci_job["strategy"]["matrix"]["ranks"], ["1", "2"])

        step_names = [step.get("name", "") for step in ci_job["steps"]]
        self.assertIn("Set compiler environment", step_names)
        env_step = next(step for step in ci_job["steps"] if step.get("name") == "Set compiler environment")
        self.assertIn('echo "CC=clang" >> "$GITHUB_ENV"', env_step["run"])
        self.assertIn('echo "CXX=clang++" >> "$GITHUB_ENV"', env_step["run"])
        self.assertIn('echo "CC=gcc" >> "$GITHUB_ENV"', env_step["run"])
        self.assertIn('echo "CXX=g++" >> "$GITHUB_ENV"', env_step["run"])

    def test_render_cmake_mpi_single_compiler_matrix(self):
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
                "ranks": [1],
                "compilers": ["gcc"],
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.load(content, Loader=yaml.BaseLoader)
        ci_job = parsed["jobs"]["ci"]
        self.assertEqual(ci_job["strategy"]["matrix"]["compiler"], ["gcc"])
        self.assertEqual(ci_job["name"], "CMake MPI CI (${{ matrix.compiler }}, ${{ matrix.ranks }}-rank)")

    def test_render_cmake_mpi_omp_threads_configuration(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is optional and unavailable")

        # Scalar int
        spec1 = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "cmake-mpi",
            "cmake_mpi": {
                "ranks": [1],
                "omp_threads": 4,
            },
        }
        content1 = installer.render(spec1)
        parsed1 = yaml.load(content1, Loader=yaml.BaseLoader)
        ci1 = parsed1["jobs"]["ci"]
        test_step1 = next(s for s in ci1["steps"] if "ctest" in s.get("run", ""))
        self.assertEqual(test_step1["env"]["OMP_NUM_THREADS"], "4")

        # Matrix list
        spec2 = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "cmake-mpi",
            "cmake_mpi": {
                "ranks": [1],
                "omp_threads": [1, 2],
            },
        }
        content2 = installer.render(spec2)
        parsed2 = yaml.load(content2, Loader=yaml.BaseLoader)
        ci2 = parsed2["jobs"]["ci"]
        self.assertEqual(ci2["strategy"]["matrix"]["omp_threads"], ["1", "2"])
        test_step2 = next(s for s in ci2["steps"] if "ctest" in s.get("run", ""))
        self.assertEqual(test_step2["env"]["OMP_NUM_THREADS"], "${{ matrix.omp_threads }}")

    def test_render_cmake_mpi_ccache_actions_cache(self):
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
                "ranks": [1],
                "ccache": True,
                "cache": True,
                "compilers": ["gcc", "clang"],
            },
        }
        content = installer.render(workflow_spec)
        text = content.decode("utf-8")
        parsed = yaml.load(content, Loader=yaml.BaseLoader)
        ci_job = parsed["jobs"]["ci"]

        self.assertIn("13aacd865c2ea734d23908f43a1f0d9bf8ec9456 # v4.2.0", text)
        cache_step = next(s for s in ci_job["steps"] if s.get("name") == "Restore compiler cache (ccache)")
        self.assertTrue(cache_step["uses"].startswith(f"actions/cache@{installer.DEFAULT_CACHE_SHA}"))
        self.assertEqual(cache_step["with"]["path"], ".ccache")
        self.assertIn("${{ matrix.compiler }}", cache_step["with"]["key"])
        self.assertIn("${{ github.sha }}", cache_step["with"]["key"])

    def test_render_cmake_mpi_post_merge_and_extended_ranks(self):
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
                "extended_ranks": [4],
                "post_merge_commands": ["make deploy-docs"],
            },
        }
        content = installer.render(workflow_spec)
        parsed = yaml.load(content, Loader=yaml.BaseLoader)

        self.assertIn("extended-validation", parsed["jobs"])
        ext_job = parsed["jobs"]["extended-validation"]
        self.assertEqual(ext_job["if"], "github.event_name == 'push'")
        self.assertEqual(ext_job["strategy"]["matrix"]["ranks"], ["4"])

        self.assertIn("post-merge", parsed["jobs"])
        pm_job = parsed["jobs"]["post-merge"]
        self.assertEqual(pm_job["if"], "github.event_name == 'push'")
        self.assertEqual(pm_job["steps"][-1]["run"], "make deploy-docs")

        # Verify status-check excludes both extended-validation and post-merge
        status_check = parsed["jobs"]["status-check"]
        needs = status_check["needs"]
        needs_list = [n.strip() for n in needs.strip("[]").split(",")] if isinstance(needs, str) else list(needs)
        self.assertNotIn("extended-validation", needs_list)
        self.assertNotIn("post-merge", needs_list)


class CMakeMPIInteractiveSetupTests(unittest.TestCase):
    """Test interactive prompting and CMake/MPI workflow staging."""

    def test_prompt_workflows_stage_selects_cmake_mpi_profile(self):
        # 1. Confirm staging workflows: y
        # 2. Self-hosted default: \n (Yes)
        # 3. Runner labels: \n (self-hosted)
        # 4. Select CMake / MPI CI (option 4) and dependency review (option 2): "4, 2"
        # 5. Manual build: n
        # 6. CMake build commands: default (newline)
        # 7. MPI rank counts: "1, 2, 4"
        # 8. Compilers: default gcc (newline)
        # 9. OpenMP threads: default 2 (newline)
        # 10. Test commands: default (newline)
        # 11. Install system deps: default No on self-hosted (newline)
        # 12. Runner dependencies: default (newline)
        # 13. Compiler cache (ccache): default (newline)
        # 14. OpenCL compilation: y
        # 15. Paths: default blank (newline)
        # 16. Triggers: default pull_request (newline)
        # 17. Status check name: default status-check (newline)
        user_input = "y\n\n\n4, 2\nn\n\n1, 2, 4\n\n\n\n\n\n\ny\n\n\n\n"
        in_stream = io.StringIO(user_input)
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(stage["name"], "workflows")
        self.assertEqual(len(stage["workflows"]), 2)

        cmake_wf = next(w for w in stage["workflows"] if w.get("profile") == "cmake-mpi")
        self.assertEqual(cmake_wf["template"], "profile-ci")
        self.assertEqual(cmake_wf["cmake_mpi"]["ranks"], [1, 2, 4])
        self.assertEqual(cmake_wf["cmake_mpi"]["compilers"], ["gcc"])
        self.assertEqual(cmake_wf["cmake_mpi"]["omp_threads"], 2)
        self.assertFalse(cmake_wf["cmake_mpi"]["install_system_deps"])
        self.assertTrue(cmake_wf["cmake_mpi"]["opencl"])
        self.assertFalse(cmake_wf["cmake_mpi"]["manual_build"])
        self.assertTrue(cmake_wf["cmake_mpi"]["ccache"])
        self.assertEqual(cmake_wf["triggers"], ["pull_request"])
        self.assertEqual(cmake_wf["status_check_name"], "status-check")

        # Validate with policy validator
        policy = {"version": 1, "stages": [stage]}
        self.assertEqual(validate_policy(policy), [stage])

    def test_prompt_workflows_stage_hosted_runner_defaults_install_system_deps(self):
        # 1. Confirm: y
        # 2. Self-hosted default: n (Hosted)
        # 3. Hosted runner: \n (ubuntu-24.04)
        # 4. Select option 4 (CMake CI): "4"
        # 5. Manual build: \n
        # 6. Build commands: \n
        # 7. MPI ranks: \n
        # 8. Compilers: \n
        # 9. OpenMP threads: \n
        # 10. Test commands: \n
        # 11. Install system deps: \n (default Yes on hosted)
        # 12. Runner dependencies: \n
        # 13. Compiler cache: \n
        # 14. OpenCL: \n
        # 15. Paths: \n
        # 16. Triggers: \n
        # 17. Status check name: \n
        user_input = "y\nn\n\n4\n\n\n\n\n\n\n\n\n\n\n\n\n\n"
        in_stream = io.StringIO(user_input)
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        cmake_wf = stage["workflows"][0]
        self.assertEqual(cmake_wf["runs_on"], "ubuntu-24.04")
        self.assertTrue(cmake_wf["cmake_mpi"]["install_system_deps"])


class CMakeMPIHeadlessStagingTests(unittest.TestCase):
    """Test Headless Mode Dry-Run and Setup Pull Request staging for cmake-mpi profile."""

    def test_headless_dry_run_and_apply_cmake_mpi_ci_profile(self):
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

    def test_rejects_invalid_compilers(self):
        for invalid in ([], ["   "], ["msvc"], ["gcc", "gcc"], [123]):
            with self.subTest(compilers=invalid):
                spec = {
                    "template": "profile-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT,
                    "profile": "cmake-mpi",
                    "cmake_mpi": {"compilers": invalid},
                }
                with self.assertRaises(ValueError):
                    installer.render(spec)

    def test_rejects_invalid_omp_threads(self):
        for invalid in (0, -1, "none", [], [0], [1, 1]):
            with self.subTest(omp_threads=invalid):
                spec = {
                    "template": "profile-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT,
                    "profile": "cmake-mpi",
                    "cmake_mpi": {"omp_threads": invalid},
                }
                with self.assertRaises(ValueError):
                    installer.render(spec)

    def test_rejects_invalid_extended_ranks(self):
        for invalid in ([], [0], [-2], ["one"], [4, 4]):
            with self.subTest(extended_ranks=invalid):
                spec = {
                    "template": "profile-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request", "push"],
                    "checkout_sha": CHECKOUT,
                    "profile": "cmake-mpi",
                    "cmake_mpi": {"extended_ranks": invalid},
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
        # Hit Enter for self-hosted default (uses default: True)
        # Hit Enter for runner labels (uses default: self-hosted)
        # Hit Enter for multichoice (uses preselected defaults: option 3 and option 2)
        # Hit Enter for manual build (default n)
        # Hit Enter for build commands (default)
        # Hit Enter for rank counts (default)
        # Hit Enter for compilers (default gcc)
        # Hit Enter for OpenMP threads (default 2)
        # Hit Enter for test commands (default)
        # Hit Enter for install system deps (default n)
        # Hit Enter for runner dependencies (default)
        # Hit Enter for ccache (default)
        # Hit Enter for opencl (default)
        # Hit Enter for paths (default)
        # Hit Enter for triggers (default)
        # Hit Enter for status check name (default)
        user_input = "\n" * 17
        in_stream = io.StringIO(user_input)
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(session=session, in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(len(stage["workflows"]), 2)
        templates = [w.get("template") for w in stage["workflows"]]
        self.assertIn("profile-ci", templates)
        self.assertIn("dependency-review", templates)

    def test_prompt_cmake_mpi_ci_options_interactive_flow(self):
        # 1. Manual build: y
        # 2. Build commands: "ninja"
        # 3. Rank counts: "1, 2, 8"
        # 4. Compilers: "gcc, clang"
        # 5. OpenMP threads: "4"
        # 6. Test commands: "ctest"
        # 7. Install system deps: y
        # 8. Runner deps: "openmpi-bin"
        # 9. Compiler cache: y
        # 10. OpenCL: n
        # 11. Paths: "src/**"
        # 12. Triggers: "pull_request, push"
        # 13. Status check name: "smoke-test"
        # 14. Post-merge commands: "echo deploy"
        # 15. Extended ranks: "8"
        user_input = "y\nninja\n1, 2, 8\ngcc, clang\n4\nctest\ny\nopenmpi-bin\ny\nn\nsrc/**\npull_request, push\nsmoke-test\necho deploy\n8\n"
        in_stream = io.StringIO(user_input)
        out_stream = io.StringIO()

        opts = prompt_cmake_mpi_ci_options(in_stream=in_stream, out_stream=out_stream)
        self.assertTrue(opts["manual_build"])
        self.assertEqual(opts["build_commands"], ["ninja"])
        self.assertEqual(opts["ranks"], [1, 2, 8])
        self.assertEqual(opts["compilers"], ["gcc", "clang"])
        self.assertEqual(opts["omp_threads"], 4)
        self.assertEqual(opts["test_commands"], ["ctest"])
        self.assertTrue(opts["install_system_deps"])
        self.assertEqual(opts["runner_dependencies"], ["openmpi-bin"])
        self.assertTrue(opts["ccache"])
        self.assertFalse(opts["opencl"])
        self.assertEqual(opts["paths"], ["src/**"])
        self.assertEqual(opts["triggers"], ["pull_request", "push"])
        self.assertEqual(opts["status_check_name"], "smoke-test")
        self.assertEqual(opts["post_merge_commands"], ["echo deploy"])
        self.assertEqual(opts["extended_ranks"], [8])

    def test_prompt_workflows_stage_selects_both_python_and_cmake_profiles_without_collision(self):
        # 1. Confirm staging workflows: y
        # 2. Self-hosted default: \n (Yes)
        # 3. Runner labels: \n (self-hosted)
        # 4. Select both Scientific Python CI (option 3) and CMake / MPI CI (option 4): "3, 4"
        # Python prompts:
        # 5. Python versions: "3.11, 3.12"
        # 5a. Python setup mode: default
        # 6. Package manager: pip
        # 6a. Install commands: skip (blank)
        # 7. Test commands: default
        # 8. Git LFS: n
        # 9. Lint commands: skip (blank)
        # 10. Dependency cache: y
        # 10a. Triggers: default (pull_request)
        # CMake prompts:
        # 11. Manual build: n
        # 12. CMake build commands: default
        # 13. MPI rank counts: default
        # 14. Compilers: default (gcc)
        # 15. OpenMP threads: default (2)
        # 16. Test commands: default
        # 17. Install system dependencies: default (No on self-hosted)
        # 18. Runner dependencies: default
        # 19. Compiler cache: default
        # 20. OpenCL: n
        # 21. Paths: default
        # 22. Triggers: default
        # 23. Status check name: default
        user_input = "y\n\n\n3, 4\n3.11, 3.12\n\npip\n\n\nn\n\ny\n\nn\n\n\n\n\n\n\n\n\nn\n\n\n\n"
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
