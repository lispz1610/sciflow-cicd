"""Tests for P6.5 (#183): Verify scientific CI profile guidance and validation evidence."""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import install_github_workflows as installer
from configure_github_repo import reconcile_gates
from setup_github_repo import _detect_profile_check_discrepancy, validate_policy as validate_repo_policy

README_PATH = ROOT_DIR / "README.md"
SCI_PROFILES_DOC_PATH = ROOT_DIR / "docs" / "scientific-ci-profiles.md"
P2_VALIDATION_DOC_PATH = ROOT_DIR / "docs" / "p2-scientific-ci-validation.md"


class ScientificCIProfileGuidanceTests(unittest.TestCase):
    """Verify scientific CI profile documentation and rendered workflow behavior (#183)."""

    def test_scientific_ci_profiles_doc_covers_checks_matrices_caching_lfs_and_limits(self):
        """AC1: docs/scientific-ci-profiles.md and README.md document PR checks, default-branch checks, matrices, caching, Git LFS, and CPU limits."""
        guide = SCI_PROFILES_DOC_PATH.read_text(encoding="utf-8")
        readme = README_PATH.read_text(encoding="utf-8")

        # 1. Profile names and aliases
        for profile_token in ("python-scientific", "scientific-python", "cmake-mpi", "cpp-hpc-mpi"):
            self.assertIn(
                profile_token,
                guide,
                f"docs/scientific-ci-profiles.md must document profile identifier '{profile_token}'",
            )

        # 2. Generated pull-request checks and terminal required checks
        for check_name in (
            "status-check",
            "smoke-test",
            "lint",
            "unit-tests",
            "smoke-build-test",
            "Python CI (3.11)",
            "Python CI (3.12)",
            "CMake MPI CI (1-rank)",
            "CMake MPI CI (2-rank)",
        ):
            self.assertIn(
                check_name,
                guide,
                f"docs/scientific-ci-profiles.md must document check name '{check_name}'",
            )

        # 3. Default-branch push checks
        for push_check in ("full-test-suite", "coverage", "extended-mpi-validation"):
            self.assertIn(
                push_check,
                guide,
                f"docs/scientific-ci-profiles.md must document default-branch check '{push_check}'",
            )
            self.assertIn(
                push_check,
                readme,
                f"README.md must document default-branch check '{push_check}'",
            )

        # 4. Optional matrices (Python versions, compilers, MPI ranks, OpenMP threads)
        for matrix_spec in (
            '["3.10", "3.11", "3.12"]',
            '["3.11", "3.12"]',
            '["3.12"]',
            '["gcc"]',
            '["gcc", "clang"]',
            "mpi_ranks",
            "omp_threads",
            "OMP_NUM_THREADS",
        ):
            self.assertIn(
                matrix_spec,
                guide,
                f"docs/scientific-ci-profiles.md must document matrix/thread option '{matrix_spec}'",
            )

        # 5. Caching options (pip, uv, ccache with CCACHE_MAXSIZE=500M / CCACHE_DIR)
        for cache_token in (
            "pip_cache",
            'cache: "pip"',
            "enable-cache: true",
            "ccache",
            "CCACHE_MAXSIZE=500M",
            "CCACHE_DIR",
        ):
            self.assertIn(
                cache_token,
                guide,
                f"docs/scientific-ci-profiles.md must document caching detail '{cache_token}'",
            )

        # 6. Git LFS choices and quota/bandwidth trade-offs
        for lfs_token in ("git_lfs", "lfs: true", "bandwidth", "quota"):
            self.assertIn(
                lfs_token,
                guide,
                f"docs/scientific-ci-profiles.md must document Git LFS detail '{lfs_token}'",
            )

        # 7. Scientific validation limits on ubuntu-24.04 CPU runners
        for limit_token in (
            "ubuntu-24.04",
            "mpirun --oversubscribe -np 2",
            "pocl-opencl-icd",
            "POCL",
        ):
            self.assertIn(
                limit_token,
                guide,
                f"docs/scientific-ci-profiles.md must document runner/validation limit '{limit_token}'",
            )

    def test_stable_required_checks_prevent_skipped_workflow_pending_deadlock(self):
        """AC3: Guide and code explain and enforce how stable required checks avoid pending-check deadlocks from skipped jobs."""
        guide = SCI_PROFILES_DOC_PATH.read_text(encoding="utf-8")

        # Anchor preserved for docs/merge-gates.md link
        self.assertIn(
            "## Stable status check anchor pattern",
            guide,
            "docs/scientific-ci-profiles.md must preserve '## Stable status check anchor pattern' heading for merge-gates.md anchor link",
        )
        for required_explanation in (
            "if: always()",
            "inspect_remote_workflow_status_check",
            "reconcile_gates",
            "Expected — Waiting for status to be reported",
            "detect-changes",
            "full-test-suite",
            "coverage",
            "extended-mpi-validation",
        ):
            self.assertIn(
                required_explanation,
                guide,
                f"docs/scientific-ci-profiles.md must explain '{required_explanation}' in stable status check section",
            )

        # Verify code behavior: evaluate_status_check passes when upstream ci job is skipped by detect-changes
        passed, msg, details = installer.evaluate_status_check(
            {
                "detect-changes": {"result": "success", "outputs": {"run_ci": "false"}},
                "ci": {"result": "skipped", "outputs": {}},
            }
        )
        self.assertTrue(passed, msg)
        self.assertEqual(details["skipped"], ["ci"])
        self.assertEqual(details["failed"], [])

        # Verify discrepancy detector enforces matching status_check_name (both 'status-check' and 'smoke-test')
        discrepancy_py = _detect_profile_check_discrepancy(
            {"Python CI (3.11)": ".github/workflows/ci.yml"},
            ci_profile_stage_input={"profile": "python", "filename": "ci.yml", "status_check_name": "status-check"},
        )
        self.assertIsNotNone(discrepancy_py)
        self.assertIn("Discrepancy detected", discrepancy_py)

        no_discrepancy_mpi = _detect_profile_check_discrepancy(
            {"smoke-test": ".github/workflows/ci.yml"},
            ci_profile_stage_input={"profile": "cmake-mpi", "filename": "ci.yml", "status_check_name": "smoke-test"},
        )
        self.assertIsNone(no_discrepancy_mpi)

    def test_cpu_smoke_checks_never_claim_gpu_hpc_scaling_or_numerical_reproducibility(self):
        """AC4: Guide and validation doc explicitly separate Observed, Inferred, and Unverified and do not claim GPU/HPC/reproducibility from CPU smoke checks."""
        self.assertFalse(P2_VALIDATION_DOC_PATH.exists(), "p2-scientific-ci-validation.md must be retired")
        for doc_path in (SCI_PROFILES_DOC_PATH, README_PATH):
            text = doc_path.read_text(encoding="utf-8")
            text_lower = text.lower()
            self.assertIn("gpu", text_lower, f"{doc_path.name} must discuss GPU boundary")
            self.assertIn("reproducibility", text_lower, f"{doc_path.name} must state reproducibility boundary")

        text = SCI_PROFILES_DOC_PATH.read_text(encoding="utf-8")
        for category in ("Observed", "Inferred", "Unverified"):
            self.assertIn(
                category,
                text,
                f"{SCI_PROFILES_DOC_PATH.name} must use the '{category}' evidence gate category",
            )
        self.assertRegex(
            text,
            r"do(?:es)?\s+not\s+(?:claim|prove|verify|constitute).*?(?:GPU|multi-node|reproducibility)",
            f"{SCI_PROFILES_DOC_PATH.name} must explicitly state that CPU-only smoke checks do not claim real GPU, full HPC scaling, or numerical reproducibility",
        )

    def test_p2_validation_doc_records_disposable_targets_profiles_checks_and_unexecuted_paths(self):
        """AC2 & AC5: Verified CI profile guidance records target repos, selected profiles, observed checks, and unexecuted paths."""
        self.assertFalse(P2_VALIDATION_DOC_PATH.exists(), "p2-scientific-ci-validation.md must be retired")
        guide_text = SCI_PROFILES_DOC_PATH.read_text(encoding="utf-8")

        for required_record in (
            "python-scientific",
            "cmake-mpi",
            "status-check",
            "smoke-test",
            "lint",
            "unit-tests",
            "smoke-build-test",
            "full-test-suite",
            "coverage",
            "mpirun",
            "Unverified",
        ):
            self.assertIn(
                required_record,
                guide_text,
                f"docs/scientific-ci-profiles.md must record '{required_record}'",
            )

    def test_disposable_python_and_cmake_mpi_workflow_generation_and_local_smoke_execution(self):
        """AC2: Exercise disposable Python and C++/MPI profiles via install_github_workflows.py and execute smoke commands."""
        # 1. Render Python scientific profile
        py_policy = {
            "version": 2,
            "repository": "OWNER/REPO",
            "stages": [
                {
                    "name": "workflows",
                    "workflows": [
                        {
                            "template": "profile-ci",
                            "template_version": 1,
                            "profile": "scientific-python",
                            "filename": "ci.yml",
                            "triggers": ["pull_request"],
                            "checkout_sha": installer.DEFAULT_CHECKOUT_SHA,
                            "status_check_name": "status-check",
                            "paths": ["**/*.py", "pyproject.toml"],
                            "python": {
                                "versions": ["3.10", "3.11", "3.12"],
                                "package_manager": "pip",
                                "commands": ["python3 -m unittest discover -s tests -v"],
                                "lint_commands": ["python3 -m py_compile src/solver.py tests/test_solver.py"],
                                "lfs": True,
                                "cache": True,
                            },
                        }
                    ],
                }
            ],
        }
        validated_py = validate_repo_policy(py_policy)
        self.assertEqual(len(validated_py), 1)
        py_yaml = installer.render(validated_py[0]["workflows"][0]).decode("utf-8")
        self.assertIn("# sciflow-cicd workflow-template=profile-ci version=1", py_yaml)
        self.assertIn('python-version: ["3.10", "3.11", "3.12"]', py_yaml)
        self.assertIn('cache: "pip"', py_yaml)
        self.assertIn("lfs: true", py_yaml)
        self.assertEqual(installer.extract_profile_status_check(py_yaml), "status-check")

        # 2. Render CMake/MPI profile (with custom status_check_name='smoke-test' and default 'status-check')
        mpi_policy = {
            "version": 2,
            "repository": "OWNER/REPO",
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
                            "checkout_sha": installer.DEFAULT_CHECKOUT_SHA,
                            "status_check_name": "smoke-test",
                            "paths": ["CMakeLists.txt", "src/**", "*.cpp"],
                            "runs_on": "ubuntu-24.04",
                            "cmake_mpi": {
                                "ranks": [1, 2],
                                "build_commands": ["cmake -B build -S .", "cmake --build build"],
                                "test_commands": [
                                    "ctest --test-dir build --output-on-failure",
                                    "OMP_NUM_THREADS=2 mpirun --oversubscribe -np 2 ./build/mpi_omp_smoke",
                                ],
                                "runner_dependencies": ["libopenmpi-dev", "openmpi-bin"],
                                "ccache": True,
                                "opencl": True,
                            },
                        }
                    ],
                }
            ],
        }
        validated_mpi = validate_repo_policy(mpi_policy)
        self.assertEqual(len(validated_mpi), 1)
        mpi_yaml = installer.render(validated_mpi[0]["workflows"][0]).decode("utf-8")
        self.assertIn("# sciflow-cicd workflow-template=profile-ci version=1", mpi_yaml)
        self.assertIn("# EVIDENCE DEMARCATION:", mpi_yaml)
        self.assertIn("ranks: [1, 2]", mpi_yaml)
        self.assertIn("ccache", mpi_yaml)
        self.assertIn("pocl-opencl-icd", mpi_yaml)
        self.assertIn("mpirun --oversubscribe -np 2", mpi_yaml)
        self.assertEqual(installer.extract_profile_status_check(mpi_yaml), "smoke-test")

        # 3. Execute disposable Python lint + unit test commands in temporary workspace
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "src").mkdir()
            (tmp_path / "tests").mkdir()
            (tmp_path / "src" / "solver.py").write_text(
                "def step_rk2(y: float, dt: float, k: float = -1.0) -> float:\n"
                "    k1 = k * y\n"
                "    k2 = k * (y + 0.5 * dt * k1)\n"
                "    return y + dt * k2\n",
                encoding="utf-8",
            )
            (tmp_path / "tests" / "test_solver.py").write_text(
                "import sys\n"
                "import unittest\n"
                "sys.path.insert(0, 'src')\n"
                "from solver import step_rk2\n\n"
                "class SolverSmokeTest(unittest.TestCase):\n"
                "    def test_step_rk2_accuracy(self):\n"
                "        self.assertAlmostEqual(step_rk2(1.0, 0.1, k=-1.0), 0.905, places=12)\n\n"
                "if __name__ == '__main__':\n"
                "    unittest.main()\n",
                encoding="utf-8",
            )
            lint_res = subprocess.run(
                [sys.executable, "-m", "py_compile", "src/solver.py", "tests/test_solver.py"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(lint_res.returncode, 0, lint_res.stderr)

            test_res = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(test_res.returncode, 0, test_res.stderr)
            self.assertIn("OK", test_res.stderr)

            # 4. If mpicxx and mpirun are present locally, compile and run 2-rank oversubscribed MPI + OpenMP smoke test
            if shutil.which("mpicxx") and shutil.which("mpirun"):
                (tmp_path / "mpi_omp_smoke.cpp").write_text(
                    "#include <mpi.h>\n"
                    "#include <omp.h>\n"
                    "#include <cstdio>\n"
                    "int main(int argc, char** argv) {\n"
                    "    MPI_Init(&argc, &argv);\n"
                    "    int rank = 0, size = 0;\n"
                    "    MPI_Comm_rank(MPI_COMM_WORLD, &rank);\n"
                    "    MPI_Comm_size(MPI_COMM_WORLD, &size);\n"
                    "    int thread_sum = 0, num_threads = 0;\n"
                    "    #pragma omp parallel reduction(+:thread_sum)\n"
                    "    {\n"
                    "        #pragma omp single\n"
                    "        num_threads = omp_get_num_threads();\n"
                    "        thread_sum += 1;\n"
                    "    }\n"
                    "    int local_val = (rank + 1) * thread_sum;\n"
                    "    int global_sum = 0;\n"
                    "    MPI_Allreduce(&local_val, &global_sum, 1, MPI_INT, MPI_SUM, MPI_COMM_WORLD);\n"
                    "    int expected = num_threads * (size * (size + 1) / 2);\n"
                    "    if (rank == 0) {\n"
                    "        std::printf(\"MPI_SIZE=%d OMP_THREADS=%d GLOBAL_SUM=%d EXPECTED=%d\\n\", size, num_threads, global_sum, expected);\n"
                    "    }\n"
                    "    MPI_Finalize();\n"
                    "    return (global_sum == expected) ? 0 : 1;\n"
                    "}\n",
                    encoding="utf-8",
                )
                build_res = subprocess.run(
                    ["mpicxx", "-O2", "-fopenmp", "mpi_omp_smoke.cpp", "-o", "mpi_omp_smoke"],
                    cwd=tmp_path,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(build_res.returncode, 0, build_res.stderr)

                mpi_env = {
                    **os.environ,
                    "OMP_NUM_THREADS": "2",
                    "OMPI_ALLOW_RUN_AS_ROOT": "1",
                    "OMPI_ALLOW_RUN_AS_ROOT_CONFIRM": "1",
                }
                mpi_res = subprocess.run(
                    ["mpirun", "--oversubscribe", "-np", "2", "./mpi_omp_smoke"],
                    cwd=tmp_path,
                    env=mpi_env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(mpi_res.returncode, 0, mpi_res.stderr)
                self.assertIn("MPI_SIZE=2 OMP_THREADS=2 GLOBAL_SUM=6 EXPECTED=6", mpi_res.stdout)


if __name__ == "__main__":
    unittest.main()
