"""Behavior tests for the setup and installed-repository documentation audit."""

import json
from pathlib import Path
import tempfile
import sys
import unittest

from scripts.documentation_freshness import audit_repository, TARGET_DOCS_CONFIG_MARKER
from scripts.documentation_audit_runner import make_report
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from scripts.install_github_workflows import _agent_skills_bundle, DOCUMENTATION_INVENTORY


class DocumentationFreshnessTests(unittest.TestCase):
    def test_target_audit_checks_only_declared_docs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("# Root\n", encoding="utf-8")
            (root / "unrelated.md").write_text("[broken](absent.md)\n", encoding="utf-8")
            declared = root / "docs/target.md"
            declared.parent.mkdir()
            declared.write_text("[Broken](missing-too.md)\n", encoding="utf-8")
            (root / ".github").mkdir()
            (root / ".github/documentation-freshness.json").write_text(json.dumps({
                "schema": TARGET_DOCS_CONFIG_MARKER, "managed_by": "github-repo-setup",
                "paths": ["docs/target.md"]}), encoding="utf-8")
            findings = audit_repository(root, target=True)
            self.assertTrue(any(item[0] == "docs/target.md" for item in findings))

    def test_installed_skill_provenance_must_match_the_pinned_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = Path(__file__).resolve().parents[1]
            for relative, content in _agent_skills_bundle()[0].items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            inventory = source_root / "data/agent-skills/mattpocock/manifest.json"
            (root / DOCUMENTATION_INVENTORY).parent.mkdir(parents=True)
            (root / DOCUMENTATION_INVENTORY).write_bytes(inventory.read_bytes())
            audit_repository(root, target=True)
            (root / ".agents/skills/engineering/implement-spec/SKILL.md").write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                audit_repository(root, target=True)

    def test_required_canonical_docs_and_provenance_are_reported_for_setup_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            findings = audit_repository(root)
            missing = {item[0] for item in findings if item[2] == "missing-canonical-doc"}
            self.assertIn("README.md", missing)
            self.assertIn("docs/agents/domain.md", missing)
            self.assertIn("docs/policy-reference.md", missing)
            self.assertIn("data/agent-skills/mattpocock/manifest.json",
                          {item[0] for item in findings if item[2] == "invalid-skill-provenance"})

    def test_irrelevant_pull_request_changes_are_noop_and_behavior_without_docs_needs_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            noop = make_report(root, repository="acme/project", run_id=42, run_attempt=1,
                               event="pull_request", head_sha="a" * 40, pull_number=8,
                               changed_files=["src/image.png"])
            self.assertEqual(noop["status"], "noop")
            result = make_report(root, repository="acme/project", run_id=42, run_attempt=1,
                                 event="pull_request", head_sha="a" * 40, pull_number=8,
                                 changed_files=["scripts/logic.py"])
            self.assertIn("semantic-review-required", {item["code"] for item in result["findings"]})

    def test_unrelated_markdown_does_not_satisfy_behavior_documentation_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = make_report(root, repository="acme/project", run_id=43, run_attempt=1,
                                 event="pull_request", head_sha="b" * 40, pull_number=9,
                                 changed_files=["scripts/logic.py", "skills/notes.md"])
            self.assertIn("semantic-review-required", {item["code"] for item in result["findings"]})

    def test_canonical_documentation_change_satisfies_behavior_documentation_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = make_report(root, repository="acme/project", run_id=44, run_attempt=1,
                                 event="pull_request", head_sha="c" * 40, pull_number=10,
                                 changed_files=["scripts/logic.py", "docs/agent-skills.md"])
            self.assertNotIn("semantic-review-required", {item["code"] for item in result["findings"]})


if __name__ == "__main__":
    unittest.main()
