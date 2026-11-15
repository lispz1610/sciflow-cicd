"""Comprehensive audit tests for Ticket P6.11 (#189):
Audit the final README against product behavior across all 5 Acceptance Criteria.
"""

from pathlib import Path
import unittest

from scripts.documentation_freshness import audit_repository
from tests.p6_test_support import extract_relative_markdown_targets

ROOT_DIR = Path(__file__).resolve().parents[1]
README_PATH = ROOT_DIR / "README.md"
DOCS_DIR = ROOT_DIR / "docs"
MAINTAINER_REF_PATH = DOCS_DIR / "maintainer-reference.md"
GLOSSARY_PATH = ROOT_DIR / "GLOSSARY.md"


class P6189FinalReadmeAuditTests(unittest.TestCase):
    """Systematically audit README.md user manual and docs/maintainer-reference.md per Issue #189 AC1-AC5."""

    @classmethod
    def setUpClass(cls):
        cls.readme_text = README_PATH.read_text(encoding="utf-8")
        cls.ref_text = MAINTAINER_REF_PATH.read_text(encoding="utf-8")
        cls.ref_lower = cls.ref_text.lower()

    # -------------------------------------------------------------------------
    # AC1: Described triggers, permissions, credentials, required checks, opt-ins,
    # and recovery steps match final implementation and recorded evidence.
    # -------------------------------------------------------------------------
    def test_ac1_triggers_permissions_credentials_checks_opt_ins_and_recovery(self):
        # Triggers
        for trigger in ("pull_request", "pull_request_target", "push"):
            self.assertIn(trigger, self.ref_text, f"Maintainer reference must mention trigger '{trigger}'")

        # Workflow permissions
        self.assertIn(
            "default_workflow_permissions: read",
            self.ref_text,
            "Maintainer reference must document read-only default workflow permissions",
        )
        self.assertIn(
            "issues: write",
            self.ref_text,
            "Maintainer reference must document issues: write for isolated findings reporters",
        )

        # Credentials & Tokens
        for cred in (
            "GITHUB_TOKEN",
            "POLICY_AUDIT_TOKEN",
            "TRIAGE_PROJECT_TOKEN",
            "OCR_LLM_AUTH_TOKEN",
            "OCR_LLM_URL",
            "OCR_LLM_MODEL",
            "OCR_LLM_USE_ANTHROPIC",
        ):
            self.assertIn(cred, self.ref_text, f"Maintainer reference must document credential/variable '{cred}'")
            self.assertIn(cred, self.readme_text, f"README manual must mention credential/variable '{cred}'")

        # Classic and Fine-grained PAT scopes
        self.assertIn("`repo`, `workflow`", self.ref_text)
        self.assertIn("Administration: Read and write", self.ref_text)

        # Verified Check Names
        verified_checks = [
            "status-check",
            "smoke-test",
            "test",
            "Issue State Gate",
            "documentation-freshness",
            "audit-report",
            "runtime-fingerprint",
        ]
        for check in verified_checks:
            self.assertIn(
                check,
                self.ref_text,
                f"Maintainer reference must document verified check/asset name '{check}'",
            )

        # Opt-Ins explicitly described
        self.assertIn(
            "Opt-In Hook Contract",
            self.ref_text,
            "Maintainer reference must document the Opt-In Hook Contract",
        )
        self.assertIn(
            "disabled by default",
            self.ref_text.lower(),
            "Maintainer reference must state hooks are disabled by default",
        )
        self.assertIn(
            "container-publish",
            self.ref_text,
            "Maintainer reference must document opt-in container publishing",
        )

        # Recovery table coverage
        self.assertIn("## Troubleshooting and recovery by symptom", self.ref_text)
        recovery_symptoms = [
            "CodeQL Default Setup unavailable",
            "Pull request check stuck in pending deadlock",
            "Two-pass pending rulesets",
            "Missing token permissions or scope",
            "Partial Setup Pull Request staging failure",
            "Failed or skipped Advisory Review",
            "Release promotion aborted on fail-closed SBOM export",
            "Zenodo DOI issuance lifecycle failures",
            "Wiki sync failure",
            "Skill catalog upgrade & local-edit conflicts",
            "Ephemeral GitHub-hosted runner resource limits",
            "Physical GPU availability & Google Colab handoff",
            "Limits of scientific reproducibility vs software test verification",
            "Persistent-VM isolation risks & self-hosted runner prohibition",
            "Planned / Deferred components: Issue-queue daemon",
        ]
        for symptom in recovery_symptoms:
            self.assertIn(
                symptom,
                self.ref_text,
                f"Recovery table must cover observable symptom '{symptom}'",
            )

    # -------------------------------------------------------------------------
    # AC2: README navigation, GitHub tab names, links, and minimal domain-file references.
    # -------------------------------------------------------------------------
    def test_ac2_navigation_github_tabs_links_and_domain_references(self):
        # 1. Maintainer Navigation Outline matches headings in docs/maintainer-reference.md
        headings = [
            line.strip().lstrip("#").strip()
            for line in self.ref_text.splitlines()
            if line.strip().startswith("## ")
        ]
        for expected_h in (
            "Requirements",
            "Supported profiles and practice decision matrix",
            "Architecture and trust boundaries",
            "Client setup",
            "GitHub manual steps",
            "Operation",
            "Troubleshooting and recovery by symptom",
            "Evidence and documentation boundary",
        ):
            self.assertIn(
                expected_h,
                headings,
                f"docs/maintainer-reference.md must have top-level heading '## {expected_h}'",
            )

        # 2. GitHub UI tab names in maintainer reference
        required_tab_hierarchies = [
            "Settings -> General",
            "Settings -> Rules -> Rulesets",
            "Settings -> Branches",
            "Settings -> Actions -> General",
            "Settings -> Code security",
            "Settings -> Secrets and variables -> Actions",
        ]
        for tab in required_tab_hierarchies:
            self.assertIn(
                tab,
                self.ref_text,
                f"Maintainer reference must include GitHub UI tab path '{tab}'",
            )

        # 3. Minimal domain-file references in README.md index
        domain_files = [
            "GLOSSARY.md",
            "docs/maintainer-reference.md",
            "docs/agents/domain.md",
            "docs/adr/0001-unify-repository-and-review-setup.md",
            "docs/adr/0002-one-setup-pull-request-for-selected-workflows.md",
            "docs/adr/0003-self-contained-release-candidate-verification.md",
            "docs/adr/0004-isolate-automated-report-identities.md",
            "docs/adr/0005-canonical-readme-and-wiki-fallback.md",
            "docs/agents/issue-tracker.md",
            "docs/agents/triage-labels.md",
            "docs/agents/issue-state-gate.md",
            "docs/agents/automation.md",
            "docs/agents/wiki.md",
        ]
        for df in domain_files:
            self.assertIn(df, self.readme_text, f"README must link domain/reference file '{df}'")

        # 4. Zero broken markdown links
        targets = extract_relative_markdown_targets(README_PATH)
        self.assertGreater(len(targets), 20)
        findings = audit_repository(ROOT_DIR)
        self.assertEqual(findings, [], f"Expected 0 doc freshness/link findings, got: {findings}")

    # -------------------------------------------------------------------------
    # AC3: Seven practice groups covered with profile-aware recommendations
    # and no claim of unperformed GPU, HPC, DOI, or security validation.
    # -------------------------------------------------------------------------
    def test_ac3_seven_practice_groups_and_strict_evidence_boundaries(self):
        # All 7 practice groups in Requirements (FR-1 through FR-7)
        for i in range(1, 8):
            self.assertIn(f"`FR-{i}`", self.ref_text)

        # Practice decision matrix covers 4 dimensions
        for dim in (
            "Supported Behavior or Manual Tutorial",
            "Suitable Scientific Repository Profile",
            "Optional or Unavailable Case",
            "Maintenance Cost or Limitation",
        ):
            self.assertIn(dim, self.ref_text)

        # Selective adoption guidance covers 3 archetypes
        self.assertIn("Small solo scientific Python repository", self.ref_text)
        self.assertIn("Small solo or lab C++ / MPI / HPC solver repository", self.ref_text)
        self.assertIn("Collaborative research package or general tooling repository", self.ref_text)

        # Strict evidence gates: No claim of unperformed GPU, HPC, DOI, or security validation
        self.assertIn("do not prove numerical/scientific reproducibility", self.ref_text)
        self.assertIn("before `issued` verification", self.ref_text)
        self.assertIn("proves GPU kernel execution", self.ref_text)

    # -------------------------------------------------------------------------
    # AC4: Optional or unavailable GPU, security, Zenodo, worker, and external-provider
    # paths are explicitly marked untested where applicable.
    # -------------------------------------------------------------------------
    def test_ac4_optional_and_unavailable_paths_explicitly_marked(self):
        # GPU / HPC limits
        self.assertIn("Unavailable / Platform Limit", self.ref_text)
        self.assertIn("Google Colab", self.ref_text)

        # Zenodo private manual vs public webhook
        self.assertIn("https://zenodo.org/deposit/new", self.ref_text)

        # Deferred components marked Planned / Deferred — Untested
        self.assertIn("Planned / Deferred — Untested", self.ref_text)
        self.assertIn("#167", self.ref_text)
        self.assertIn("#170", self.ref_text)
        self.assertIn("#171", self.ref_text)
        self.assertIn("#2", self.ref_text)
        self.assertIn("#102", self.ref_text)

        # Self-hosted runner prohibition
        self.assertIn(
            "Never register a personal VM as a GitHub self-hosted runner",
            self.ref_text,
        )

        # Security capability feature gates
        for state in ("[CONFIGURED]", "[MISSING]", "[UNSUPPORTED]", "[INACCESSIBLE]"):
            self.assertIn(state, self.ref_text)
            self.assertIn(state, self.readme_text)

    # -------------------------------------------------------------------------
    # AC5: New maintainer can follow user manual & reference from installation
    # to merged setup and verification of checks.
    # -------------------------------------------------------------------------
    def test_ac5_maintainer_walkthrough_and_verification_flow(self):
        # Section 4 Client setup has all 7 structured walkthrough steps in docs/maintainer-reference.md
        for step_title in (
            "1. Prerequisites, Token Scopes, and Installation",
            "2. Runtime Diagnostics and Repository Capability Audit",
            "3. Interactive Setup, Inventory & Resume State, Dry-Run, and Confirmation",
            "4. Headless Mode",
            "5. The Setup Pull Request, Owner Merge, and Two-Pass Gate Activation",
            "6. Repeat Convergence, Missing Capability Fallbacks, and Drift / Partial-Run Recovery",
            "7. Summary of Recorded Disposable-Repository Evidence",
        ):
            self.assertIn(step_title, self.ref_text)

        self.assertEqual(
            self.ref_text.count("**What the client changes:**"),
            7,
            "All 7 Client setup subsections must define 'What the client changes'",
        )
        self.assertEqual(
            self.ref_text.count("**What remains for the maintainer to do in GitHub:**"),
            7,
            "All 7 Client setup subsections must define 'What remains for the maintainer to do in GitHub'",
        )
        self.assertEqual(
            self.ref_text.count("**How to verify success:**"),
            7,
            "All 7 Client setup subsections must define 'How to verify success'",
        )

        # CLI flags, prompts, and exit codes in practical README manual
        self.assertIn("Apply these changes? [y/N]: ", self.readme_text)
        self.assertIn("Target repository (OWNER/REPO): ", self.readme_text)
        self.assertIn("1. Resume existing setup", self.readme_text)
        self.assertIn("2. Start from scratch", self.readme_text)
        self.assertIn("gh-setup doctor --repo OWNER/REPO --json", self.readme_text)
        self.assertIn("gh-setup dry-run --policy", self.readme_text)
        self.assertIn("gh-setup apply --policy", self.readme_text)
        self.assertIn("--dry-run-result", self.readme_text)

        # Exit codes in maintainer reference
        self.assertIn("Exit code `0`", self.ref_text)
        self.assertIn("Exit code `1`", self.ref_text)
        self.assertIn("Exit code `2`", self.ref_text)

        # Local verification commands in README manual
        for cmd in (
            "python3 -m unittest discover -s tests -p 'test_*.py' -v",
            "python3 scripts/check_markdown_links.py",
            "python3 scripts/documentation_freshness.py",
            "python3 scripts/lint_workflow_action_pins.py",
            "python3 scripts/audit_module_dependencies.py",
        ):
            self.assertIn(cmd, self.readme_text)


if __name__ == "__main__":
    unittest.main()
