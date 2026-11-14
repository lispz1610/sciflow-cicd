import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
TESTS_DIR = ROOT_DIR / "tests"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from scripts.agent_skill_upgrade import (
    classify_catalog_upgrade,
    removal_decision_hint,
    validate_removal_acknowledgment,
)
from scripts.install_github_workflows import (
    AGENT_SKILLS_MANIFEST,
    _resolve_skill_removals,
)
from scripts.provision_agent_skills import _resolve_removal_acknowledgment


def sha(content):
    return hashlib.sha256(content).hexdigest()


def manifest(files, commit="a" * 40, version="1.3.1"):
    return {
        "schema_version": 1,
        "managed_by": "github-repo-setup",
        "bundle": "mattpocock",
        "source_repository": "https://github.com/mattpocock/skills",
        "source_commit": commit,
        "source_version": version,
        "license": "MIT",
        "files": dict(files),
    }


class ClassifyCatalogUpgradeTests(unittest.TestCase):
    def test_identical_manifests_are_unchanged(self):
        files = {
            "LICENSE": sha(b"license"),
            "engineering/example/SKILL.md": sha(b"skill"),
        }

        result = classify_catalog_upgrade(manifest(files), manifest(files))

        self.assertEqual(result["removed_skills"], [])
        self.assertEqual(result["renamed_skills"], [])
        self.assertEqual(result["added_skills"], [])
        self.assertEqual(result["removed_files"], [])
        self.assertEqual(result["changed_files"], [])
        self.assertTrue(result["unchanged"])
        self.assertFalse(result["revision_changed"])

    def test_content_change_is_not_a_removal(self):
        old_files = {"engineering/example/SKILL.md": sha(b"v1")}
        new_files = {"engineering/example/SKILL.md": sha(b"v2")}

        result = classify_catalog_upgrade(manifest(old_files), manifest(new_files))

        self.assertEqual(result["removed_skills"], [])
        self.assertEqual(result["renamed_skills"], [])
        self.assertEqual(result["changed_files"], ["engineering/example/SKILL.md"])
        self.assertFalse(result["unchanged"])

    def test_upstream_skill_removal_is_identified(self):
        old_files = {
            "engineering/kept/SKILL.md": sha(b"kept"),
            "engineering/removed/SKILL.md": sha(b"removed"),
            "engineering/removed/guide.md": sha(b"guide"),
        }
        new_files = {"engineering/kept/SKILL.md": sha(b"kept")}

        result = classify_catalog_upgrade(manifest(old_files), manifest(new_files))

        self.assertEqual(result["removed_skills"], ["engineering/removed"])
        self.assertEqual(result["renamed_skills"], [])
        self.assertEqual(
            sorted(result["removed_files"]),
            ["engineering/removed/SKILL.md", "engineering/removed/guide.md"],
        )

    def test_upstream_rename_is_identified_separately_from_removal(self):
        old_files = {
            "engineering/renamed/SKILL.md": sha(b"same content"),
            "engineering/renamed/guide.md": sha(b"guide"),
            "engineering/gone/SKILL.md": sha(b"gone"),
        }
        new_files = {
            "engineering/new-name/SKILL.md": sha(b"same content"),
            "engineering/new-name/guide.md": sha(b"guide"),
        }

        result = classify_catalog_upgrade(manifest(old_files), manifest(new_files))

        self.assertEqual(result["removed_skills"], ["engineering/gone"])
        self.assertEqual(
            result["renamed_skills"],
            [{"from": "engineering/renamed", "to": "engineering/new-name"}],
        )
        self.assertEqual(result["added_skills"], [])

    def test_rename_requires_identical_content_so_edits_are_not_renames(self):
        old_files = {
            "engineering/renamed/SKILL.md": sha(b"original content"),
            "engineering/renamed/guide.md": sha(b"guide"),
        }
        new_files = {
            "engineering/new-name/SKILL.md": sha(b"edited content"),
            "engineering/new-name/guide.md": sha(b"guide"),
        }

        result = classify_catalog_upgrade(manifest(old_files), manifest(new_files))

        self.assertEqual(result["renamed_skills"], [])
        self.assertEqual(result["removed_skills"], ["engineering/renamed"])
        self.assertEqual(result["added_skills"], ["engineering/new-name"])

    def test_added_skill_is_identified(self):
        old_files = {"engineering/example/SKILL.md": sha(b"skill")}
        new_files = {
            "engineering/example/SKILL.md": sha(b"skill"),
            "engineering/added/SKILL.md": sha(b"added"),
        }

        result = classify_catalog_upgrade(manifest(old_files), manifest(new_files))

        self.assertEqual(result["added_skills"], ["engineering/added"])
        self.assertEqual(result["removed_skills"], [])
        self.assertEqual(result["removed_files"], [])

    def test_file_removal_inside_surviving_skill_is_not_a_skill_removal(self):
        old_files = {
            "engineering/example/SKILL.md": sha(b"skill"),
            "engineering/example/guide.md": sha(b"guide"),
        }
        new_files = {"engineering/example/SKILL.md": sha(b"skill")}

        result = classify_catalog_upgrade(manifest(old_files), manifest(new_files))

        self.assertEqual(result["removed_skills"], [])
        self.assertEqual(result["renamed_skills"], [])
        self.assertEqual(result["removed_files"], ["engineering/example/guide.md"])

    def test_revision_only_change_is_reported_separately_from_file_changes(self):
        files = {"engineering/example/SKILL.md": sha(b"skill")}

        result = classify_catalog_upgrade(
            manifest(files, commit="a" * 40, version="1.3.1"),
            manifest(files, commit="b" * 40, version="1.4.0"),
        )

        self.assertTrue(result["unchanged"])
        self.assertTrue(result["revision_changed"])
        self.assertEqual(result["removed_files"], [])
        self.assertEqual(result["changed_files"], [])

    def test_license_change_is_an_ordinary_changed_file(self):
        old_files = {"LICENSE": sha(b"license v1")}
        new_files = {"LICENSE": sha(b"license v2")}

        result = classify_catalog_upgrade(manifest(old_files), manifest(new_files))

        self.assertEqual(result["changed_files"], ["LICENSE"])
        self.assertEqual(result["removed_skills"], [])

    def test_rejects_manifests_without_file_inventories(self):
        with self.assertRaisesRegex(ValueError, "file inventories"):
            classify_catalog_upgrade({"files": "nope"}, manifest({}))

        with self.assertRaisesRegex(ValueError, "file inventories"):
            classify_catalog_upgrade(manifest({}), None)


class ValidateRemovalAcknowledgmentTests(unittest.TestCase):
    def test_nothing_dropped_is_accepted(self):
        self.assertIsNone(validate_removal_acknowledgment([], None))
        self.assertIsNone(validate_removal_acknowledgment([], ["engineering/x"]))
        self.assertIsNone(validate_removal_acknowledgment([], {"engineering/x"}))

    def test_exact_set_acknowledgment_is_accepted(self):
        dropped = ["engineering/dropped", "engineering/also-dropped"]
        self.assertIsNone(
            validate_removal_acknowledgment(dropped, ["engineering/dropped", "engineering/also-dropped"]))
        self.assertIsNone(
            validate_removal_acknowledgment(dropped, {"engineering/also-dropped", "engineering/dropped"}))
        self.assertIsNone(
            validate_removal_acknowledgment(dropped, tuple(dropped)))

    def test_none_acknowledgment_raises(self):
        with self.assertRaisesRegex(ValueError, "upstream catalog revision no longer ships"):
            validate_removal_acknowledgment(["engineering/dropped"], None)

    def test_wrong_set_acknowledgment_raises(self):
        with self.assertRaisesRegex(ValueError, "exactly"):
            validate_removal_acknowledgment(["engineering/dropped"], ["engineering/other-skill"])

    def test_extra_acknowledgment_raises(self):
        with self.assertRaisesRegex(ValueError, "exactly"):
            validate_removal_acknowledgment(["engineering/dropped"],
                                            ["engineering/dropped", "engineering/extra"])

    def test_missing_acknowledgment_raises(self):
        with self.assertRaisesRegex(ValueError, "exactly"):
            validate_removal_acknowledgment(["engineering/a", "engineering/b"], ["engineering/a"])

    def test_non_list_acknowledgment_raises(self):
        with self.assertRaisesRegex(ValueError, "exactly"):
            validate_removal_acknowledgment(["engineering/dropped"], "engineering/dropped")

    def test_acknowledgment_names_dropped_skills(self):
        with self.assertRaisesRegex(ValueError, "engineering/dropped"):
            validate_removal_acknowledgment(["engineering/dropped"], None)
        with self.assertRaisesRegex(ValueError, "engineering/dropped"):
            validate_removal_acknowledgment(["engineering/dropped"], ["engineering/other-skill"])

    def test_local_and_remote_flows_emit_same_message_as_removal_decision_hint(self):
        prior = manifest({
            "engineering/dropped/SKILL.md": sha(b"dropped content"),
            "engineering/kept/SKILL.md": sha(b"kept content"),
        })
        updated = manifest({
            "engineering/kept/SKILL.md": sha(b"kept content"),
        }, commit="b" * 40, version="1.4.0")

        classification = classify_catalog_upgrade(prior, updated)
        dropped = ["engineering/dropped"]
        expected_hint = removal_decision_hint(classification)
        expected_msg = (
            f"agent skill upgrade requires an explicit removal decision: {expected_hint}"
        )

        with self.assertRaises(ValueError) as validate_ctx:
            validate_removal_acknowledgment(dropped, None, classification=classification)
        self.assertEqual(str(validate_ctx.exception), expected_msg)

        # Default classification when omitted matches removed skills
        with self.assertRaises(ValueError) as default_ctx:
            validate_removal_acknowledgment(dropped, None)
        self.assertEqual(str(default_ctx.exception), expected_msg)

        # Local provisioning flow (_resolve_removal_acknowledgment)
        with self.assertRaises(ValueError) as local_ctx:
            _resolve_removal_acknowledgment(prior, updated, None)
        self.assertEqual(str(local_ctx.exception), expected_msg)

        # Remote staging flow (_resolve_skill_removals)
        files = {
            AGENT_SKILLS_MANIFEST: (
                (json.dumps(updated) + "\n").encode("utf-8"),
                None,
            )
        }
        with self.assertRaises(ValueError) as remote_ctx:
            _resolve_skill_removals(None, "base", "main", prior, files, None)
        self.assertEqual(str(remote_ctx.exception), expected_msg)

    def test_renamed_skill_flows_emit_same_message_as_removal_decision_hint(self):
        prior = manifest({
            "engineering/old/SKILL.md": sha(b"identical content"),
        })
        updated = manifest({
            "engineering/new/SKILL.md": sha(b"identical content"),
        }, commit="b" * 40, version="1.4.0")

        classification = classify_catalog_upgrade(prior, updated)
        dropped = ["engineering/old"]
        expected_hint = removal_decision_hint(classification)
        expected_msg = (
            f"agent skill upgrade requires an explicit removal decision: {expected_hint}"
        )

        with self.assertRaises(ValueError) as validate_ctx:
            validate_removal_acknowledgment(dropped, None, classification=classification)
        self.assertEqual(str(validate_ctx.exception), expected_msg)

        with self.assertRaises(ValueError) as local_ctx:
            _resolve_removal_acknowledgment(prior, updated, None)
        self.assertEqual(str(local_ctx.exception), expected_msg)

        files = {
            AGENT_SKILLS_MANIFEST: (
                (json.dumps(updated) + "\n").encode("utf-8"),
                None,
            )
        }
        with self.assertRaises(ValueError) as remote_ctx:
            _resolve_skill_removals(None, "base", "main", prior, files, None)
        self.assertEqual(str(remote_ctx.exception), expected_msg)


if __name__ == "__main__":
    unittest.main()
