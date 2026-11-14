"""Tests for P6.3 (#181): Explain architecture and trust boundaries in README.md."""

from pathlib import Path
import unittest

from scripts.documentation_freshness import audit_repository
from tests.p6_test_support import extract_readme_section

ROOT_DIR = Path(__file__).resolve().parents[1]
README_PATH = ROOT_DIR / "README.md"
GLOSSARY_PATH = ROOT_DIR / "GLOSSARY.md"


class P6181ArchitectureTrustBoundariesTests(unittest.TestCase):
    def setUp(self):
        self.readme = README_PATH.read_text(encoding="utf-8")
        self.section = extract_readme_section(
            self.readme, "Architecture and trust boundaries"
        )
        self.assertTrue(
            self.section,
            "README.md must contain a '## Architecture and trust boundaries' section",
        )

    def test_ac1_identifies_credentials_and_write_boundaries_for_all_components(self):
        """AC1: Identifies which credentials and writes belong to client, Actions, target repo, advisory review, and planned VM worker."""
        # 1. Local sciflow-cicd client
        for token_or_detail in (
            "GITHUB_TOKEN",
            "PROJECTS_V2_TOKEN",
            "codex/workflows-",
            "Setup Pull Request",
        ):
            self.assertIn(
                token_or_detail,
                self.section,
                f"Architecture section must document local sciflow-cicd client boundary detail '{token_or_detail}'",
            )
        self.assertRegex(
            self.section,
            r"(?i)never merges.*(setup|pr).*or.*stores llm",
            "Architecture section must state that sciflow-cicd never merges PRs or stores LLM keys",
        )

        # 2. GitHub-hosted Actions jobs & two-job split (ADR 0004)
        for actions_detail in (
            "default_workflow_permissions: read",
            "POLICY_AUDIT_TOKEN",
            "TRIAGE_PROJECT_TOKEN",
            "OCR_LLM_AUTH_TOKEN",
            "OCR_LLM_URL",
            "OCR_LLM_MODEL",
            "OCR_LLM_USE_ANTHROPIC",
            "open-code-review.yml",
            "documentation-freshness.yml",
            "0004-isolate-automated-report-identities.md",
            "issues: write",
        ):
            self.assertIn(
                actions_detail,
                self.section,
                f"Architecture section must document GitHub-hosted Actions / two-job split detail '{actions_detail}'",
            )

        # 3. Target scientific repository
        for repo_detail in (
            "rulesets",
            "human maintainer",
        ):
            self.assertIn(
                repo_detail.lower(),
                self.section.lower(),
                f"Architecture section must document target scientific repository boundary detail '{repo_detail}'",
            )

        # 4. Future limited VM worker & personal VM
        for vm_detail in (
            "limited VM worker",
            "personal VM",
            "Planned / Deferred",
            "disposable worktrees",
            "draft PR",
        ):
            self.assertIn(
                vm_detail.lower(),
                self.section.lower(),
                f"Architecture section must document future limited VM worker / personal VM detail '{vm_detail}'",
            )

    def test_ac2_distinguishes_ephemeral_github_hosted_jobs_from_personal_vm_and_warns_against_self_hosted_runner(self):
        """AC2: Explicitly identifies ephemeral GitHub-hosted jobs vs personal VM and warns never to register personal VM as a self-hosted runner."""
        for required_phrase in (
            "ephemeral GitHub-hosted",
            "ubuntu-24.04",
            "destroyed after each job",
            "personal VM is NOT a GitHub self-hosted Actions runner",
            "never register a personal VM as a GitHub self-hosted",
        ):
            self.assertIn(
                required_phrase.lower(),
                self.section.lower(),
                f"Architecture section must explicitly include '{required_phrase}'",
            )

    def test_ac3_traces_issue_end_to_end_with_mermaid_diagram_and_implemented_vs_planned_table(self):
        """AC3: Traces an Issue end-to-end through eligibility/triage, implementation/draft PR, PR checks/advisory review, and human merge/closure verification with Mermaid diagram and trace table."""
        self.assertIn(
            "```mermaid",
            self.section,
            "Architecture section must include a Mermaid diagram tracing the architecture and Issue lifecycle",
        )
        for trace_item in (
            "issue-triage.yml",
            "issue-frontier-orchestrator.yml",
            "ready-for-agent",
            "status-check",
            "smoke-test",
            "issue-state-gate.yml",
            "documentation-freshness.yml",
            "open-code-review.yml",
            "review_issue_reporter.js",
            "enforce_issue_delivery.py",
            "verify_issue_delivery.py",
            "Issue State Gate",
            "#167",
            "#170",
            "#171",
            "#2",
            "#102",
        ):
            self.assertIn(
                trace_item,
                self.section,
                f"End-to-end Issue lifecycle trace in Architecture section must include '{trace_item}'",
            )
        # Verify both Implemented and Planned / Deferred markers appear in the trace
        self.assertIn("**Implemented**", self.section)
        self.assertIn("**Planned / Deferred**", self.section)

    def test_ac4_identifies_persistent_vm_security_risks_controls_and_remaining_limitations(self):
        """AC4: Explicitly identifies security risks of executing repository code on a persistent VM, intended controls, and remaining limitations."""
        for risk in (
            "cross-run state persistence",
            "cached credential theft",
            "lateral movement",
            "poisoned build caches",
            "lingering background processes",
        ):
            self.assertIn(
                risk.lower(),
                self.section.lower(),
                f"Architecture section must identify persistent-VM security risk '{risk}'",
            )
        for control_or_limitation in (
            "never registering as a GitHub Actions",
            "disposable worktrees/containers",
            "least-privilege tokens",
            "human inspection of draft PR diffs",
            "remaining limitations",
        ):
            self.assertIn(
                control_or_limitation.lower(),
                self.section.lower(),
                f"Architecture section must document persistent-VM control or limitation '{control_or_limitation}'",
            )

    def test_ac5_no_deferred_component_is_described_as_already_available(self):
        """AC5: No deferred component (limited VM worker, Issue queue daemon, public-repo OCR worker loop) is described as already available."""
        for deferred_term in (
            "limited VM worker",
            "Issue queue daemon",
            "public-repo OCR",
        ):
            self.assertIn(
                deferred_term.lower(),
                self.section.lower(),
                f"Architecture section must explicitly name deferred capability '{deferred_term}' to mark its boundary",
            )
        # Ensure we do not falsely claim the limited VM worker or issue queue daemon is implemented
        self.assertNotRegex(
            self.section,
            r"(?i)(limited VM worker|Issue queue daemon|public-repo OCR worker loop)\s*\|\s*\*\*Implemented\*\*",
            "Deferred components must never be marked as Implemented",
        )

    def test_glossary_and_documentation_freshness_stay_clean(self):
        findings = audit_repository(ROOT_DIR)
        self.assertEqual(findings, [], f"Expected 0 documentation freshness findings, got: {findings}")


if __name__ == "__main__":
    unittest.main()
