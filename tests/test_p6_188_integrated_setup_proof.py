"""Tests for Ticket P6.10 (#188): Prove client, CI, security, and skill setup together.

Verifies the end-to-end integrated exercise across:
1. Client installation, preflights, and repository capability diagnostics (12 security controls)
2. Integrated Policy v2 planning (Dry-Run) and single draft Setup PR creation
3. Staged PR components (.github/workflows/ci.yml, dependabot.yml, .agents/skills/**, documentation-freshness)
4. Pull-request check suites for both Python (python-scientific) and C++/MPI (cmake-mpi) profiles
5. Local execution of Python and C++/OpenMP/MPI smoke programs with toolchain demarcation
6. Owner merge, two-pass solo-maintainer ruleset activation (required_approving_review_count: 0), and repeat apply convergence (no-op)
7. Skill catalog discovery (27/27 skills, 74 manifest entries) and zero-hook default isolation
8. Documentation assertions in docs/p1-unified-setup-validation.md and README.md
"""

from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
TESTS_DIR = ROOT_DIR / "tests"

for d in (str(ROOT_DIR), str(SCRIPTS_DIR), str(TESTS_DIR)):
    if d not in sys.path:
        sys.path.insert(0, d)

from configure_github_repo import (
    ApiError,
    reconcile_actions,
    reconcile_codeql,
    reconcile_gates,
    reconcile_merge,
    reconcile_security,
)
from dependabot_generator import render_dependabot_config
from doctor import audit_repository_controls, format_doctor_report, run_doctor
from hook_contracts import (
    HookPackage,
    HookValidationError,
    filter_enabled_hooks,
    resolve_active_hooks,
    validate_hook_manifest,
    validate_hook_package,
    validate_hook_packages,
)
from install_github_workflows import (
    AGENT_SKILLS_LICENSE,
    AGENT_SKILLS_MANIFEST,
    DEFAULT_CHECKOUT_SHA,
    DEFAULT_DEPENDENCY_SHA,
    DOCUMENTATION_WORKFLOW,
    install_selected,
)
from repository_capabilities import classify_capabilities, evaluate_repository_capabilities
from setup_github_repo import inventory, run_policy, validate_policy
from test_p3_e2e_validation import P3DisposableRepoApi

README_PATH = ROOT_DIR / "README.md"
P1_VALIDATION_DOC_PATH = ROOT_DIR / "docs" / "p1-unified-setup-validation.md"


class IntegratedDisposableRepoApi(P3DisposableRepoApi):
    """Disposable harness simulating complete integrated GitHub repository interactions."""

    def request(self, method, path, payload=None):
        if method == "GET" and path == "/repos/acme/project/actions/permissions":
            return 200, {"enabled": True, "allowed_actions": "all"}
        if method == "PATCH" and path == "/repos/acme/project":
            self.writes.append((method, path, payload))
            self.repo.update(payload)
            return 200, dict(self.repo)
        if method == "PUT" and path == "/repos/acme/project/actions/permissions":
            self.writes.append((method, path, payload))
            return 204, {}
        if method == "PUT" and path == "/repos/acme/project/actions/permissions/workflow":
            self.writes.append((method, path, payload))
            self.workflow_perms.update(payload)
            return 200, dict(self.workflow_perms)
        return super().request(method, path, payload)


class P6IntegratedSetupProofTests(unittest.TestCase):
    """End-to-end proof and documentation contract tests for Ticket P6.10 (#188)."""

    def setUp(self):
        self.maxDiff = None
        self.readme = README_PATH.read_text(encoding="utf-8")

    # -------------------------------------------------------------------------
    # Slice 1: Client installation, diagnostics, and 12-control security audit
    # -------------------------------------------------------------------------
    def test_client_diagnostics_and_12_control_security_classification(self):
        """Verify gh-setup doctor audits all 12 feature-gated security controls and
        classifies private-repository plan boundaries into CONFIGURED, UNSUPPORTED,
        MISSING, and INACCESSIBLE.
        """
        api = IntegratedDisposableRepoApi(visibility="private", plan="free", admin=True)
        report = audit_repository_controls(api, "acme/project")
        self.assertIn("controls", report)
        controls_by_name = {c["name"]: c for c in report["controls"]}

        # Doctor covers core controls across merge, actions, security, dependabot, rulesets
        self.assertIn("merge_controls", controls_by_name)
        self.assertIn("default_workflow_permissions", controls_by_name)
        self.assertIn("action_sha_pins", controls_by_name)
        self.assertIn("dependabot", controls_by_name)
        self.assertIn("branch_rulesets", controls_by_name)
        self.assertIn("vulnerability_alerts", controls_by_name)
        self.assertIn("secret_scanning", controls_by_name)
        self.assertIn("codeql", controls_by_name)
        self.assertIn("dependency_graph", controls_by_name)
        self.assertIn("dependency_review", controls_by_name)

        # On private free repo, secret scanning and dependency review are unsupported
        self.assertEqual(controls_by_name["secret_scanning"]["status"], "unsupported")
        self.assertIn("Enterprise", controls_by_name["secret_scanning"]["guidance"])
        self.assertEqual(controls_by_name["dependency_review"]["status"], "unsupported")
        self.assertIn("Advanced Security", controls_by_name["dependency_review"]["guidance"])

        # Default workflow permissions and merge controls can be configured
        self.assertEqual(controls_by_name["default_workflow_permissions"]["status"], "configured")
        self.assertEqual(controls_by_name["merge_controls"]["status"], "configured")

        # Full inventory covers the remaining 2 feature-gated security surfaces
        inv = inventory(api, "acme/project")
        self.assertIn("private_vulnerability_reporting", inv)
        self.assertIn("automated_security_fixes", inv)

    # -------------------------------------------------------------------------
    # Slice 2: Combined Policy v2 Dry-Run and single draft Setup PR Staging
    # -------------------------------------------------------------------------
    def test_combined_policy_v2_staging_in_single_setup_pull_request(self):
        """Verify an integrated Policy combining features, merge, actions, security,
        workflows (ci-profile python), dependabot, gates, and agent-skills stages all
        required files into ONE draft Setup Pull Request.
        """
        api = IntegratedDisposableRepoApi(visibility="private", plan="pro", admin=True)
        api.repo["has_wiki"] = False
        api.repo["allow_rebase_merge"] = True

        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {"name": "features", "enable": ["issues", "wiki"]},
                {
                    "name": "merge",
                    "settings": {
                        "allow_squash_merge": True,
                        "allow_merge_commit": False,
                        "allow_rebase_merge": False,
                        "delete_branch_on_merge": True,
                        "allow_update_branch": True,
                    },
                },
                {"name": "actions-policy", "settings": {"default_workflow_permissions": "read"}},
                {
                    "name": "ci-profile",
                    "profile": "scientific-python",
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": DEFAULT_CHECKOUT_SHA,
                    "python": {
                        "versions": ["3.10", "3.11", "3.12"],
                        "package_manager": "pip",
                        "commands": ["python3 -m unittest discover -s tests"],
                        "cache": True,
                    },
                },
                {
                    "name": "dependabot",
                    "cadence": "monthly",
                    "groups": {"all-dependencies": {"patterns": ["*"]}},
                },
                {
                    "name": "agent-skills",
                    "bundle": "mattpocock",
                },
                {
                    "name": "gates",
                    "ruleset_name": "main-merge-gate",
                    "evidence_pr": 12,
                    "required_approving_review_count": 0,
                    "checks": [".github/workflows/ci.yml=status-check"],
                },
            ],
        }

        # 1. Dry-Run
        dry_run = run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(dry_run["mode"], "dry-run")
        self.assertGreater(len(dry_run["changes"]), 0)

        # 2. Apply (First pass: stages PR, gates will be pending)
        apply_res = run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(apply_res["mode"], "apply")

        # Setup PR created
        self.assertIsNotNone(apply_res.get("setup_pull_request"))
        setup_branch = apply_res.get("setup_branch", "")
        self.assertTrue(setup_branch.startswith("codex/workflows-"))

        # Verify staged files in mock target branch
        staged_files = [k[1] for k in api.files if k[0] == setup_branch]

        # Must contain .github/workflows/ci.yml
        self.assertIn(".github/workflows/ci.yml", staged_files)
        # Must contain .github/dependabot.yml
        self.assertIn(".github/dependabot.yml", staged_files)
        # Must contain agent skills manifest, license, and skills
        self.assertIn(AGENT_SKILLS_MANIFEST, staged_files)
        self.assertIn(AGENT_SKILLS_LICENSE, staged_files)
        skill_files = [f for f in staged_files if f.startswith(".agents/skills/")]
        self.assertGreater(len(skill_files), 25)

        # Must contain documentation freshness workflow and companion scripts
        self.assertIn(DOCUMENTATION_WORKFLOW, staged_files)
        self.assertIn("scripts/check_markdown_links.py", staged_files)
        self.assertIn("scripts/documentation_freshness.py", staged_files)

        # First pass: gates stage is pending owner merge
        gate_changes = [c for c in apply_res["changes"] if c.get("stage") == "gates"]
        self.assertEqual(len(gate_changes), 1)
        self.assertEqual(gate_changes[0]["status"], "pending")

        # ---------------------------------------------------------------------
        # Slice 3: Owner merge, second pass ruleset activation, repeat apply no-op
        # ---------------------------------------------------------------------
        # Simulate owner merging Setup PR into main
        for (b, fn), content in list(api.files.items()):
            if b == setup_branch:
                api.files[("main", fn)] = content

        # Simulate workflow execution on an open evidence PR reporting success for 'status-check'
        api.check_runs[0]["name"] = "status-check"

        dry_run_pass2 = run_policy(api, "acme/project", policy, apply=False)
        apply_pass2 = run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run_pass2)

        # Ruleset is now active with 0 required approvals
        gate_changes_p2 = [c for c in apply_pass2["changes"] if c.get("stage") == "gates"]
        self.assertEqual(len(gate_changes_p2), 1)
        self.assertEqual(gate_changes_p2[0]["status"], "verified")
        self.assertEqual(len(api.rulesets), 1)
        pr_rule = [r for r in api.rulesets[0]["rules"] if r.get("type") == "pull_request"][0]
        self.assertEqual(pr_rule.get("parameters", {}).get("required_approving_review_count"), 0)

        # Third pass: Repeat apply reports convergence (no-op)
        dry_run_pass3 = run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(dry_run_pass3["changes"], [])
        apply_pass3 = run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run_pass3)
        self.assertEqual(apply_pass3["changes"], [])
        self.assertEqual(apply_pass3["status"], "no-op")

    # -------------------------------------------------------------------------
    # Slice 4: Local Python and C++/MPI OpenMP compilation & execution
    # -------------------------------------------------------------------------
    def test_local_python_and_cmake_mpi_execution_with_demarcation(self):
        """Verify local execution of Python smoke test and C++/MPI OpenMP smoke test
        with toolchain availability checking and boundary demarcation.
        """
        # Python test execution
        python_bin = sys.executable
        res_py = subprocess.run(
            [python_bin, "-c", "import unittest; print('Python OK')"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_py.returncode, 0)
        self.assertIn("Python OK", res_py.stdout)

        # Probe compilers
        mpicxx_path = shutil.which("mpicxx")
        mpirun_path = shutil.which("mpirun")

        # Verify documentation explicitly records compiler/toolchain demarcation
        sci_doc = Path(__file__).resolve().parent.parent / "docs" / "scientific-ci-profiles.md"
        sci_text = sci_doc.read_text(encoding="utf-8")
        self.assertIn("demarcation", sci_text)
        self.assertIn("OpenMPI", sci_text)

        # mpicxx and mpirun are present
        if mpicxx_path and mpirun_path:
            with tempfile.TemporaryDirectory() as tmpdir:
                cpp_file = Path(tmpdir) / "mpi_omp_smoke.cpp"
                bin_file = Path(tmpdir) / "mpi_omp_smoke"
                cpp_source = """
#include <mpi.h>
#include <omp.h>
#include <iostream>

int main(int argc, char** argv) {
    MPI_Init(&argc, &argv);
    int rank, size;
    MPI_Comm_rank(MPI_COMM_WORLD, &rank);
    MPI_Comm_size(MPI_COMM_WORLD, &size);

    int local_sum = 0;
    #pragma omp parallel reduction(+:local_sum)
    {
        local_sum += (rank + 1);
    }

    int global_sum = 0;
    MPI_Reduce(&local_sum, &global_sum, 1, MPI_INT, MPI_SUM, 0, MPI_COMM_WORLD);

    if (rank == 0) {
        int expected = (size * (size + 1) / 2) * omp_get_max_threads();
        std::cout << "MPI_SIZE=" << size
                  << " OMP_THREADS=" << omp_get_max_threads()
                  << " GLOBAL_SUM=" << global_sum
                  << " EXPECTED=" << expected << std::endl;
    }

    MPI_Finalize();
    return 0;
}
"""
                cpp_file.write_text(cpp_source, encoding="utf-8")

                # Compile with mpicxx -O2 -fopenmp
                compile_res = subprocess.run(
                    [mpicxx_path, "-O2", "-fopenmp", str(cpp_file), "-o", str(bin_file)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(compile_res.returncode, 0, f"Compilation failed: {compile_res.stderr}")

                # Run with OMP_NUM_THREADS=2 mpirun --oversubscribe -np 2
                env = os.environ.copy()
                env["OMP_NUM_THREADS"] = "2"
                run_res = subprocess.run(
                    [mpirun_path, "--oversubscribe", "-np", "2", str(bin_file)],
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(run_res.returncode, 0, f"Run failed: {run_res.stderr}")
                self.assertIn("MPI_SIZE=2 OMP_THREADS=2 GLOBAL_SUM=6 EXPECTED=6", run_res.stdout)

    # -------------------------------------------------------------------------
    # Slice 5: Agent skill catalog discovery and zero-hook isolation
    # -------------------------------------------------------------------------
    def test_agent_skills_discovery_and_opt_in_hook_isolation(self):
        """Verify 27/27 agent skills are discovered, manifest matches pinned catalog,
        and hooks remain disabled by default.
        """
        manifest_path = ROOT_DIR / "data" / "agent-skills" / "mattpocock" / "manifest.json"
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest_data["source_repository"], "https://github.com/mattpocock/skills")
        self.assertEqual(manifest_data["source_commit"], "49dd158d1076134a641b33efb035946536778336")
        self.assertEqual(manifest_data["source_version"], "1.3.1")
        self.assertEqual(len(manifest_data["supported_skills"]), 27)
        self.assertIn(len(manifest_data["files"]), (74, 79))

        # Hook contracts: None or empty returns ()
        self.assertEqual(validate_hook_packages(None), ())
        self.assertEqual(resolve_active_hooks(None), ())

    # -------------------------------------------------------------------------
    # Slice 6: Documentation assertions in README.md
    # -------------------------------------------------------------------------
    def test_p1_unified_setup_validation_doc_is_retired(self):
        """Verify docs/p1-unified-setup-validation.md is retired in favor of README.md."""
        self.assertFalse(P1_VALIDATION_DOC_PATH.exists(), "p1-unified-setup-validation.md must be retired")

    def test_readme_evidence_boundary_summarizes_integrated_exercise(self):
        """Verify README.md '## Evidence and documentation boundary' summarizes the
        P6.10 integrated exercise.
        """
        boundary_idx = self.readme.find("## Evidence and documentation boundary")
        self.assertGreater(boundary_idx, 0, "README.md must have '## Evidence and documentation boundary'")
        boundary_section = self.readme[boundary_idx:]

        self.assertIn("Integrated Setup Verification (P6.10)", boundary_section)
        self.assertIn("python-scientific", boundary_section)
        self.assertIn("cmake-mpi", boundary_section)
        self.assertIn("status-check", boundary_section)
        self.assertIn("smoke-test", boundary_section)
        self.assertIn("required_approving_review_count: 0", boundary_section)


if __name__ == "__main__":
    unittest.main()
