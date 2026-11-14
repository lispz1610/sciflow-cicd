"""Tests for P6.9 (#187): Document limitations and recovery by symptom."""

from pathlib import Path
import sys
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from doi_guidance import DoiError, DoiState, DoiStateMachine, validate_release_doi_metadata
from export_release_sbom import get_sbom_recovery
from install_github_workflows import StagingFailure
from tests.p6_test_support import extract_readme_section

README_PATH = ROOT_DIR / "README.md"


class P6187LimitationsRecoveryTests(unittest.TestCase):
    """Verify limitations and recovery by symptom documentation and runtime error contracts (#187)."""

    def setUp(self):
        self.readme = README_PATH.read_text(encoding="utf-8")
        self.section = extract_readme_section(
            self.readme, "Troubleshooting and recovery by symptom"
        )
        self.assertTrue(
            self.section,
            "README.md must contain a '## Troubleshooting and recovery by symptom' section",
        )

    def test_ac3_structure_and_five_required_dimensions(self):
        """AC3: Every recovery entry identifies symptom, UI tab, local inspection, recovery steps, and verification status."""
        for required_header in (
            "Observable Symptom",
            "GitHub UI Tab / Surface",
            "Local Command or State Inspection",
            "Actionable Recovery Steps & Confirmation Evidence",
            "Verification Status",
        ):
            self.assertIn(
                required_header,
                self.section,
                f"Troubleshooting section must include table column / entry header '{required_header}'",
            )

        for required_status in (
            "Observed / Verified",
            "Unavailable / Platform Limit",
            "Planned / Deferred — Untested",
        ):
            self.assertIn(
                required_status,
                self.section,
                f"Troubleshooting section must include canonical status marker '{required_status}'",
            )

    def test_ac1_codeql_setup_modes_and_feature_gates(self):
        """AC1: Documents CodeQL Default Setup vs custom workflow conflicts, no supported languages, GHAS plan gate, pending scan status, and UI path."""
        for codeql_detail in (
            "CodeQL Default Setup",
            "codeql*.yml",
            "no CodeQL-supported languages detected",
            "[UNSUPPORTED]",
            "pending",
            "Settings -> Code security -> Code scanning -> CodeQL analysis",
        ):
            self.assertIn(
                codeql_detail.lower(),
                self.section.lower(),
                f"Troubleshooting section must document CodeQL detail '{codeql_detail}'",
            )

    def test_ac1_skipped_required_checks_and_pending_deadlock(self):
        """AC1: Documents pending deadlock when ruleset requires push-only job, path-filtered workflow, or raw matrix job instead of stable terminal check, plus reconcile_gates discrepancy detection."""
        for deadlock_detail in (
            "Expected — Waiting for status to be reported",
            "full-test-suite",
            "coverage",
            "extended-mpi-validation",
            "status-check",
            "smoke-test",
            "discrepancy detected",
            "reconcile_gates",
        ):
            self.assertIn(
                deadlock_detail.lower(),
                self.section.lower(),
                f"Troubleshooting section must document check deadlock / discrepancy detail '{deadlock_detail}'",
            )

    def test_ac1_ephemeral_github_hosted_jobs_and_fallbacks(self):
        """AC1: Documents ephemeral runner limits (2-4 vCPUs, memory/time limits, destroyed state), oversubscribed MPI, and POCL OpenCL fallback."""
        for runner_detail in (
            "2–4 vCPUs",
            "destroyed state after each job",
            "mpirun --oversubscribe -np 2",
            "pocl",
        ):
            self.assertIn(
                runner_detail.lower(),
                self.section.lower(),
                f"Troubleshooting section must document ephemeral runner limitation '{runner_detail}'",
            )

    def test_ac1_persistent_vm_isolation_and_runner_prohibition(self):
        """AC1: Documents never registering personal VM as GitHub self-hosted runner, cross-run state persistence, credential theft, lateral movement, poisoned caches, lingering processes, and isolation controls."""
        for isolation_detail in (
            "never register a personal vm as a github self-hosted",
            "cross-run state persistence",
            "credential theft",
            "lateral movement",
            "poisoned caches",
            "lingering processes",
            "disposable worktrees",
            "least-privilege",
            "human diff review",
        ):
            self.assertIn(
                isolation_detail.lower(),
                self.section.lower(),
                f"Troubleshooting section must document persistent-VM risk/control detail '{isolation_detail}'",
            )

    def test_ac1_zenodo_doi_issuance_lifecycle(self):
        """AC1: Documents 5-state DOI lifecycle, never recording reserved/pending DOI before issued resolution, and public webhook vs private manual deposit."""
        for doi_detail in (
            "unselected",
            "requested",
            "pending",
            "issued",
            "unavailable",
            "never record",
            "citation.cff",
            "docs/release-control.json",
            "https://doi.org/",
            "https://zenodo.org/deposit/new",
        ):
            self.assertIn(
                doi_detail.lower(),
                self.section.lower(),
                f"Troubleshooting section must document Zenodo DOI lifecycle detail '{doi_detail}'",
            )

    def test_ac1_real_gpu_availability_and_colab_handoff(self):
        """AC1: Documents physical GPUs and HPC fabrics absence on standard runners, CPU smoke vs device kernel truth, and Colab handoff."""
        for gpu_detail in (
            "physical gpus",
            "cuda",
            "rocm",
            "infiniband",
            "ubuntu-24.04",
            "colab",
        ):
            self.assertIn(
                gpu_detail.lower(),
                self.section.lower(),
                f"Troubleshooting section must document GPU availability limitation '{gpu_detail}'",
            )

    def test_ac1_limits_of_scientific_reproducibility(self):
        """AC1: Documents that passing CI, CITATION.cff, and SBOM verify software build/test execution, NOT bit-for-bit numerical determinism or independent validity."""
        for sci_limit in (
            "reproducibility",
            "numerical determinism",
            "citation.cff",
            "sbom",
        ):
            self.assertIn(
                sci_limit.lower(),
                self.section.lower(),
                f"Troubleshooting section must document scientific reproducibility limitation '{sci_limit}'",
            )

    def test_ac2_missing_permissions_or_secrets(self):
        """AC2: Documents recovery for missing GITHUB_TOKEN, workflow scope (HTTP 403/404), [INACCESSIBLE] doctor controls, POLICY_AUDIT_TOKEN, TRIAGE_PROJECT_TOKEN, OCR_LLM_AUTH_TOKEN / OCR_LLM_*."""
        for perm_detail in (
            "github_token",
            "workflow",
            "[inaccessible]",
            "policy_audit_token",
            "triage_project_token",
            "ocr_llm_auth_token",
            "ocr_llm_",
        ):
            self.assertIn(
                perm_detail.lower(),
                self.section.lower(),
                f"Troubleshooting section must document permission/secret recovery detail '{perm_detail}'",
            )

    def test_ac2_partial_setup_pr_and_state_drift(self):
        """AC2: Documents StagingFailure mid-branch, branch reuse without duplicating PRs, CAS expected_sha conflicts, and exit code 2."""
        for staging_detail in (
            "stagingfailure",
            "codex/workflows-",
            "reuse",
            "expected_sha",
            "repository state changed since the accepted dry-run",
            "exit code 2",
        ):
            self.assertIn(
                staging_detail.lower(),
                self.section.lower(),
                f"Troubleshooting section must document partial Setup PR / CAS drift recovery detail '{staging_detail}'",
            )

    def test_ac2_two_pass_pending_rulesets_and_issue_state_gate(self):
        """AC2: Documents two-pass pending rulesets and Issue State Gate when staging workflows together before merge."""
        for two_pass_detail in (
            "status: \"pending\"",
            "two-pass",
            "gates",
            "issue-state-gate",
            "draft setup pull request",
        ):
            self.assertIn(
                two_pass_detail.lower(),
                self.section.lower(),
                f"Troubleshooting section must document two-pass pending ruleset detail '{two_pass_detail}'",
            )

    def test_ac2_failed_or_skipped_advisory_review(self):
        """AC2: Documents review_status: pending_configuration readiness warnings, Actions failure, non-blocking guarantee, and isolated report-findings job."""
        for review_detail in (
            "review_status: pending_configuration",
            "open code review",
            "non-blocking",
            "never a required merge check",
            "report-findings",
            "review_issue_reporter.js",
        ):
            self.assertIn(
                review_detail.lower(),
                self.section.lower(),
                f"Troubleshooting section must document advisory review recovery detail '{review_detail}'",
            )

    def test_ac2_aborted_sbom_releases(self):
        """AC2: Documents fail-closed export_release_sbom aborting release/scientific promotion before tag/release creation on 404, 401/403, or zero packages."""
        for sbom_detail in (
            "cannot generate release sbom",
            "export_release_sbom",
            "dependency graph",
            "packages: []",
            "minimal",
        ):
            self.assertIn(
                sbom_detail.lower(),
                self.section.lower(),
                f"Troubleshooting section must document aborted SBOM release recovery detail '{sbom_detail}'",
            )

    def test_ac2_wiki_sync_failure(self):
        """AC2: Documents Wiki sync failure on .wiki.git, first page UI bootstrap, and private Free plan fallback."""
        for wiki_detail in (
            "wiki sync",
            ".wiki.git",
            "create the first page",
            "secondary mirror",
        ):
            self.assertIn(
                wiki_detail.lower(),
                self.section.lower(),
                f"Troubleshooting section must document Wiki sync recovery detail '{wiki_detail}'",
            )

    def test_ac2_skill_catalog_upgrade_and_local_edit_conflicts(self):
        """AC2: Documents destination has local changes / conflict and unacknowledged dropped skills (allow_removed_skills)."""
        for skill_detail in (
            "destination has local changes",
            "status: \"conflict\"",
            "allow_removed_skills",
            "expected_sha",
        ):
            self.assertIn(
                skill_detail.lower(),
                self.section.lower(),
                f"Troubleshooting section must document skill conflict recovery detail '{skill_detail}'",
            )

    def test_ac2_planned_deferred_components(self):
        """AC2: Explicitly marks Issue-queue daemon recovery, autonomous limited VM worker recovery, and automated worker OCR fix loop as Planned / Deferred referencing #167, #170, #171, #2, #102."""
        for deferred_ref in (
            "#167",
            "#170",
            "#171",
            "#2",
            "#102",
            "Planned / Deferred — Untested",
        ):
            self.assertIn(
                deferred_ref,
                self.section,
                f"Troubleshooting section must explicitly mark deferred components with reference '{deferred_ref}'",
            )

    def test_runtime_error_messages_alignment(self):
        """Verify actual Python runtime error and recovery messages match documented recovery steps."""
        # 1. SBOM recovery messages
        self.assertIn("Settings → Code security", get_sbom_recovery("404 Not Found", status=404))
        self.assertIn("GITHUB_TOKEN or GH_TOKEN", get_sbom_recovery("403 Forbidden", status=403))
        self.assertIn("manifests", get_sbom_recovery("empty packages list"))

        # 2. DOI state machine guards
        with self.assertRaises(DoiError) as ctx:
            validate_release_doi_metadata({"doi_state": "requested", "doi": "10.5281/zenodo.1234567"})
        self.assertIn("A DOI string cannot be recorded in release metadata when state is 'requested'", str(ctx.exception))

        sm = DoiStateMachine(DoiState.REQUESTED)
        with self.assertRaises(DoiError) as ctx2:
            sm.transition(DoiState.PENDING, doi="10.5281/zenodo.1234567")
        self.assertIn("ONLY after issuance is verified in 'issued' state", str(ctx2.exception))

        # 3. StagingFailure message contract
        branch = "codex/workflows-1234abcd"
        error_text = "Network timeout"
        msg = f"Setup may be partial on branch {branch}: {error_text}. Inspect branch and diff before retrying; do not delete it automatically"
        failure = StagingFailure(msg, branch, {"path": "failed"})
        self.assertIn("Setup may be partial on branch codex/workflows-1234abcd", str(failure))
        self.assertIn("do not delete it automatically", str(failure))


if __name__ == "__main__":
    unittest.main()
