"""Tests for integrated documentation, examples, and historical boundaries."""

import json
from pathlib import Path
import sys
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from scripts.setup_github_repo import validate_policy
from scripts.review_policy import validate_policy as validate_review_stage

ROOT_DIR = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = ROOT_DIR / "examples"
DOCS_DIR = ROOT_DIR / "docs"
README_PATH = ROOT_DIR / "README.md"


class IntegratedDocsTests(unittest.TestCase):
    def test_examples_exist_and_validate_cleanly(self):
        # 1. Existing v1 example
        v1_example = EXAMPLES_DIR / "setup-policy.json"
        self.assertTrue(v1_example.exists(), "examples/setup-policy.json must exist")
        v1_data = json.loads(v1_example.read_text(encoding="utf-8"))
        self.assertEqual(v1_data["version"], 1)
        stages_v1 = validate_policy(v1_data)
        self.assertTrue(len(stages_v1) > 0)

        # 2. V2 Agent Skills example
        v2_skills = EXAMPLES_DIR / "setup-policy-v2-agent-skills.json"
        self.assertTrue(v2_skills.exists(), "examples/setup-policy-v2-agent-skills.json must exist")
        v2_skills_data = json.loads(v2_skills.read_text(encoding="utf-8"))
        self.assertEqual(v2_skills_data["version"], 2)
        stages_skills = validate_policy(v2_skills_data)
        self.assertTrue(len(stages_skills) > 0)
        review_stages = [s for s in stages_skills if s.get("name") == "review"]
        self.assertEqual(len(review_stages), 0)

    def test_examples_and_docs_are_credential_free(self):
        forbidden_substrings = [
            "never-print-this",
            "provider_key",
            "sk-ant-api",
            "sk-proj-",
        ]
        scanned_files = list(EXAMPLES_DIR.glob("*.json")) + list(DOCS_DIR.glob("**/*.md")) + [README_PATH]
        for file_path in scanned_files:
            # Skip historical git-ignored or local files
            if ".local" in file_path.parts:
                continue
            content = file_path.read_text(encoding="utf-8")
            for forbidden in forbidden_substrings:
                self.assertNotIn(
                    forbidden,
                    content,
                    f"Found forbidden credential/token pattern '{forbidden}' in {file_path}",
                )

    def test_historical_documents_marked_and_link_to_integrated_spec(self):
        historical_files = [
            DOCS_DIR / "open-code-review-handoff.md",
            DOCS_DIR / "open-code-review-project" / "spec.md",
            DOCS_DIR / "open-code-review-project" / "tasks.md",
            DOCS_DIR / "open-code-review-project" / "implementation-plan.md",
        ]
        for h_file in historical_files:
            self.assertFalse(h_file.exists(), f"Historical scratch file {h_file} must be retired")
        advisory_guide = (DOCS_DIR / "advisory-review.md").read_text(encoding="utf-8")
        self.assertIn("Integrated Review Setup Architecture Specification", advisory_guide)

    def test_readme_and_guides_cover_required_integration_topics(self):
        readme_content = README_PATH.read_text(encoding="utf-8")
        ref_content = (DOCS_DIR / "maintainer-reference.md").read_text(encoding="utf-8")
        # Single installed command & Setup PR in concise README
        self.assertIn("sciflow-cicd", readme_content)
        self.assertIn("Setup Pull Request", readme_content)
        self.assertIn("owner review and merge", readme_content.lower())
        # Policy versions & diagnostic/recovery details in maintainer reference
        self.assertIn("Policy version 1", ref_content)
        self.assertIn("Policy version 2", ref_content)
        self.assertIn("Setup Pull Request", ref_content)
        self.assertIn("owner merge", ref_content.lower())
        self.assertIn("private", readme_content.lower())
        self.assertIn("readiness", ref_content.lower())
        self.assertIn("diagnostics", ref_content.lower())
        self.assertIn("recovery", ref_content.lower())
        # Stale claim removed
        self.assertNotIn(
            "This repository does not install or manage that review workflow",
            readme_content,
        )
        self.assertNotIn(
            "This repository does not install or manage that review workflow",
            ref_content,
        )

    def test_readme_and_advisory_guide_cover_automated_ocr_pipeline_and_evidence_boundary(self):
        readme_content = README_PATH.read_text(encoding="utf-8")
        ref_content = (DOCS_DIR / "maintainer-reference.md").read_text(encoding="utf-8")
        advisory_guide = (DOCS_DIR / "advisory-review.md").read_text(encoding="utf-8")

        for subcommand in ("preflight-app", "preflight", "poll", "check-safety", "run"):
            self.assertIn(subcommand, readme_content)
            self.assertIn(subcommand, advisory_guide)

        for required_term in (
            "pending_configuration",
            "OCR_LLM_AUTH_TOKEN",
            "systemd",
            "allowlist",
            "Advisory Review",
        ):
            self.assertIn(required_term, readme_content)
            self.assertIn(required_term, advisory_guide)

        self.assertNotIn(".github/workflows/open-code-review.yml", readme_content)
        self.assertNotIn(".github/workflows/open-code-review.yml", advisory_guide)

        self.assertIn("Review Setup", ref_content)
        self.assertIn("Review Setup", advisory_guide)
        self.assertIn("test_ocr_pipeline_e2e.py", advisory_guide)


class CurrentDocumentationConsistencyTests(unittest.TestCase):
    """Guard the issue #88 documentation refresh against future drift."""

    def test_user_docs_avoid_retired_review_workflow_and_fallback_step(self):
        for path in (README_PATH, DOCS_DIR / "workflows.md"):
            content = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertNotIn("open-code-review.yml", content)
                self.assertNotIn("fallback_review", content)

        ref_content = (DOCS_DIR / "maintainer-reference.md").read_text(encoding="utf-8")
        self.assertNotIn("and Review Setup (`open-code-review.yml`)", ref_content)

    def test_future_works_matrix_uses_existing_repository_paths(self):
        content = (DOCS_DIR / "future-works.md").read_text(encoding="utf-8")
        for stale in (
            "scripts/policy_storage.py",
            "scripts/dry_run_generator.py",
            "tests/test_policy_storage.py",
            "tests/test_install_workflows.py",
            "tests/test_scientific_ci.py",
            "tests/test_repo_capabilities.py",
            "tests/test_rulesets.py",
            "tests/test_release_candidate.py",
            "tests/test_export_sbom.py",
            "tests/test_agent_skills.py",
        ):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, content)
        for current in (
            "scripts/policy_assembly.py",
            "tests/test_install_github_workflows.py",
            "tests/test_profile_ci_scaffolding.py",
            "tests/test_repository_capabilities.py",
            "tests/test_solo_maintainer_ruleset.py",
            "tests/test_verify_release.py",
            "tests/test_release_sbom.py",
            "tests/test_provision_agent_skills.py",
        ):
            with self.subTest(current=current):
                self.assertIn(current, content)

    def test_release_audit_matrix_uses_existing_artifact_paths(self):
        content = (DOCS_DIR / "production-release-audit.md").read_text(encoding="utf-8")
        self.assertNotIn("docs/audit-baseline-matrix.md", content)
        self.assertNotIn("tests/test_audit_baseline.py", content)
        self.assertIn("tests/test_live_validation_audit.py", content)

    def test_dependency_governance_matches_current_linter_and_ci_split(self):
        content = (DOCS_DIR / "dependency-graph-governance.md").read_text(encoding="utf-8")
        self.assertIn("66 modules, 136 dependencies", content)
        self.assertIn("push-only", content)
        self.assertIn("ci.yml", content)
        self.assertIn(
            "actions/dependency-review-action@a1d282b36b6f3519aa1f3fc636f609c47dddb294",
            content,
        )

    def test_action_pin_example_uses_live_checkout_sha_and_tag(self):
        content = (DOCS_DIR / "proportional-security-and-controls.md").read_text(encoding="utf-8")
        self.assertIn(
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
            content,
        )


if __name__ == "__main__":
    unittest.main()
