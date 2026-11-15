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

        # 2. V2 OpenAI-compatible review example
        v2_openai = EXAMPLES_DIR / "setup-policy-v2-review-openai.json"
        self.assertTrue(v2_openai.exists(), "examples/setup-policy-v2-review-openai.json must exist")
        v2_openai_data = json.loads(v2_openai.read_text(encoding="utf-8"))
        self.assertEqual(v2_openai_data["version"], 2)
        stages_openai = validate_policy(v2_openai_data)
        review_stages_openai = [s for s in stages_openai if s.get("name") == "review"]
        self.assertEqual(len(review_stages_openai), 1)
        parsed_openai = validate_review_stage(review_stages_openai[0])
        self.assertFalse(parsed_openai.llm.use_anthropic)

        # 3. V2 Anthropic review example
        v2_anthropic = EXAMPLES_DIR / "setup-policy-v2-review-anthropic.json"
        self.assertTrue(v2_anthropic.exists(), "examples/setup-policy-v2-review-anthropic.json must exist")
        v2_anthropic_data = json.loads(v2_anthropic.read_text(encoding="utf-8"))
        self.assertEqual(v2_anthropic_data["version"], 2)
        stages_anthropic = validate_policy(v2_anthropic_data)
        review_stages_anthropic = [s for s in stages_anthropic if s.get("name") == "review"]
        self.assertEqual(len(review_stages_anthropic), 1)
        parsed_anthropic = validate_review_stage(review_stages_anthropic[0])
        self.assertTrue(parsed_anthropic.llm.use_anthropic)

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
        # Single installed command
        self.assertIn("sciflow-cicd", readme_content)
        # Policy versions
        self.assertIn("Policy version 1", readme_content)
        self.assertIn("Policy version 2", readme_content)
        # Setup PR and owner merge
        self.assertIn("Setup Pull Request", readme_content)
        self.assertIn("owner merge", readme_content.lower())
        # Private-only initial support
        self.assertIn("private", readme_content.lower())
        # Readiness warnings and diagnostics
        self.assertIn("readiness", readme_content.lower())
        self.assertIn("diagnostics", readme_content.lower())
        self.assertIn("recovery", readme_content.lower())
        # Stale claim removed
        self.assertNotIn(
            "This repository does not install or manage that review workflow",
            readme_content,
        )

    def test_readme_and_advisory_guide_cover_automated_ocr_pipeline_and_evidence_boundary(self):
        readme_content = README_PATH.read_text(encoding="utf-8")
        advisory_guide = (DOCS_DIR / "advisory-review.md").read_text(encoding="utf-8")

        for subcommand in ("preflight-app", "preflight", "poll", "check-safety", "run"):
            self.assertIn(subcommand, readme_content)
            self.assertIn(subcommand, advisory_guide)

        for required_term in (
            "pending_configuration",
            "OCR_LLM_AUTH_TOKEN",
            ".github/workflows/open-code-review.yml",
            "Advisory Review",
            "Review Setup",
        ):
            self.assertIn(required_term, readme_content)
            self.assertIn(required_term, advisory_guide)

        self.assertIn("test_ocr_pipeline_e2e.py", advisory_guide)


if __name__ == "__main__":
    unittest.main()
