"""Tests for P6.1 (#179) canonical README route, documentation boundary, and Wiki fallback."""

from pathlib import Path
import unittest

from scripts.documentation_freshness import audit_repository
from tests.p6_test_support import extract_relative_markdown_targets

ROOT_DIR = Path(__file__).resolve().parents[1]
README_PATH = ROOT_DIR / "README.md"
DOCS_DIR = ROOT_DIR / "docs"
MAINTAINER_REF_PATH = DOCS_DIR / "maintainer-reference.md"
GLOSSARY_PATH = ROOT_DIR / "GLOSSARY.md"
ADR_0005_PATH = DOCS_DIR / "adr" / "0005-canonical-readme-and-wiki-fallback.md"
DOMAIN_DOC_PATH = DOCS_DIR / "agents" / "domain.md"


class P6DocumentationBoundaryTests(unittest.TestCase):
    def test_readme_exposes_concise_user_manual_and_maintainer_reference_outline(self):
        readme = README_PATH.read_text(encoding="utf-8")
        readme_headings = [
            line.strip().lstrip("#").strip().lower()
            for line in readme.splitlines()
            if line.strip().startswith("## ")
        ]
        for fragment in (
            "prerequisites",
            "quick start",
            "headless mode",
            "setup pull request",
            "features",
            "direct scripts",
            "troubleshooting",
            "documentation index",
        ):
            self.assertTrue(
                any(fragment in h for h in readme_headings),
                f"README.md user manual must have a top-level '## ' section covering '{fragment}'; found {readme_headings}",
            )

        ref_doc = MAINTAINER_REF_PATH.read_text(encoding="utf-8")
        headings = [
            line.strip().lstrip("#").strip().lower()
            for line in ref_doc.splitlines()
            if line.strip().startswith("## ")
        ]
        required_heading_fragments = (
            "requirements",
            "supported profiles",
            "architecture",
            "client setup",
            "github manual steps",
            "operation",
            "troubleshooting",
            "evidence",
        )
        for fragment in required_heading_fragments:
            self.assertTrue(
                any(fragment in h for h in headings),
                f"docs/maintainer-reference.md must have a top-level '## ' section covering '{fragment}'; found {headings}",
            )

    def test_orphaned_scratch_files_are_retired_from_docs(self):
        self.assertFalse(
            (DOCS_DIR / "refactoring_issues_sketch.md").exists(),
            "docs/refactoring_issues_sketch.md must be retired so wiki-sync does not publish scratch notes",
        )
        self.assertFalse(
            (DOCS_DIR / "refactoring_plan").exists(),
            "docs/refactoring_plan scratch file must be retired",
        )

    def test_every_retained_markdown_doc_is_reachable_from_readme(self):
        direct_targets = extract_relative_markdown_targets(README_PATH)
        self.assertIn(
            GLOSSARY_PATH.resolve(),
            direct_targets,
            "GLOSSARY.md must be linked directly from README.md",
        )
        self.assertIn(
            (ROOT_DIR / "AGENTS.md").resolve(),
            direct_targets,
            "AGENTS.md must be linked directly from README.md",
        )

        all_docs_md = sorted(DOCS_DIR.rglob("*.md"))
        self.assertGreater(len(all_docs_md), 0)
        unreachable = [
            path.relative_to(ROOT_DIR).as_posix()
            for path in all_docs_md
            if path.resolve() not in direct_targets
        ]
        self.assertEqual(
            unreachable,
            [],
            f"Every retained .md file in docs/ must be directly linked from README.md; unreachable: {unreachable}",
        )

    def test_wiki_fallback_and_github_issues_boundary_are_recorded_in_adr_and_reference(self):
        self.assertTrue(
            ADR_0005_PATH.is_file(),
            "docs/adr/0005-canonical-readme-and-wiki-fallback.md must exist",
        )
        adr_text = ADR_0005_PATH.read_text(encoding="utf-8")
        self.assertIn("README.md", adr_text)
        self.assertIn("Wiki", adr_text)
        self.assertIn("GitHub Issues", adr_text)

        domain_text = DOMAIN_DOC_PATH.read_text(encoding="utf-8")
        self.assertIn("0005-canonical-readme-and-wiki-fallback.md", domain_text)

        ref_text = MAINTAINER_REF_PATH.read_text(encoding="utf-8")
        self.assertIn("adr/0005-canonical-readme-and-wiki-fallback.md", ref_text)
        self.assertIn("secondary mirror", ref_text.lower())
        self.assertIn("agents/issue-tracker.md", ref_text)

    def test_glossary_follows_domain_modeling_format(self):
        glossary = GLOSSARY_PATH.read_text(encoding="utf-8")
        self.assertTrue(glossary.startswith("# "), "GLOSSARY.md must start with context heading")
        self.assertIn("## Language", glossary)
        self.assertIn("_Avoid_:", glossary)
        # Ensure key P6 domain concepts are defined
        for term in (
            "**Interactive Setup**:",
            "**Headless Mode**:",
            "**Policy**:",
            "**Inventory**:",
            "**Resume State**:",
            "**Dry-Run**:",
            "**Setup Pull Request**:",
            "**Advisory Review**:",
            "**Documentation Freshness Audit**:",
            "**Release Candidate**:",
            "**Release Gate**:",
            "**Control Record**:",
            "**Runtime Fingerprint**:",
            "**Hook Package**:",
            "**Opt-In Hook Contract**:",
        ):
            self.assertIn(term, glossary, f"GLOSSARY.md must define {term}")

    def test_documentation_freshness_and_markdown_links_pass_zero_findings(self):
        findings = audit_repository(ROOT_DIR)
        self.assertEqual(findings, [], f"Expected 0 documentation freshness/link findings, got: {findings}")

    def test_maintainer_reference_documents_functional_requirements_fr1_through_fr7(self):
        ref_doc = MAINTAINER_REF_PATH.read_text(encoding="utf-8")
        for fr_id in ("FR-1", "FR-2", "FR-3", "FR-4", "FR-5", "FR-6", "FR-7"):
            self.assertIn(
                fr_id,
                ref_doc,
                f"docs/maintainer-reference.md must explicitly define functional requirement {fr_id}",
            )
        self.assertIn(
            "#github-manual-steps",
            ref_doc,
            "Functional requirements in docs/maintainer-reference.md must link to #github-manual-steps",
        )

    def test_seven_practice_decision_matrix_covers_four_required_dimensions_and_stays_in_sync(self):
        text = MAINTAINER_REF_PATH.read_text(encoding="utf-8")
        text_lower = text.lower()
        for required_header in (
            "supported behavior or manual tutorial",
            "suitable scientific repository profile",
            "optional or unavailable case",
            "maintenance cost or limitation",
        ):
            self.assertIn(
                required_header,
                text_lower,
                f"docs/maintainer-reference.md must include the decision matrix dimension '{required_header}'",
            )
        for group_num in range(1, 8):
            self.assertRegex(
                text,
                rf"\*\*{group_num}\.\s+",
                f"docs/maintainer-reference.md must include practice group {group_num} in the decision matrix",
            )

    def test_maintainer_reference_distinguishes_smoke_checks_real_gpu_hpc_validation_and_publication_workflows(self):
        ref_doc = MAINTAINER_REF_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "### Distinguishing PR Smoke Checks, Real GPU/HPC Validation, and Publication Workflows",
            ref_doc,
            "docs/maintainer-reference.md must explicitly distinguish PR smoke checks, real GPU/HPC validation, and publication workflows",
        )
        for required_term in (
            "Ordinary Python/C++ pull-request smoke checks",
            "Real GPU/HPC hardware validation",
            "Publication workflows",
            "do not prove numerical/scientific reproducibility",
        ):
            self.assertIn(
                required_term,
                ref_doc,
                f"docs/maintainer-reference.md 3-way validation boundary section must include '{required_term}'",
            )

    def test_maintainer_reference_provides_selective_adoption_guidance_for_small_scientific_repositories(self):
        ref_doc = MAINTAINER_REF_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "### Profile Decision Matrix and Selective Adoption for Small Scientific Repositories",
            ref_doc,
        )
        for phrase in (
            "small scientific repository",
            "Enable Now (Core Baseline)",
            "Enable Later (When Milestone Reached)",
            "Skip / Leave Disabled (Zero Maintenance Overhead)",
        ):
            self.assertIn(
                phrase,
                ref_doc,
                f"docs/maintainer-reference.md must include small-repository selective adoption guidance phrase '{phrase}'",
            )


if __name__ == "__main__":
    unittest.main()
