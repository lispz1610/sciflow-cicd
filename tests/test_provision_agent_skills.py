import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
TESTS_DIR = ROOT_DIR / "tests"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from scripts.provision_agent_skills import (
    _rollback_destination,
    generate_target_manifest,
    load_verified_catalog,
    parse_agent_skills_manifest,
    provision_skills,
)


class ProvisionAgentSkillsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        self.destination = self.root / "installed"

    def write_case(self, entries, supported=None, license_content=None):
        files = []
        for source, destination, content in entries:
            source_path = self.snapshot / source
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(content)
            files.append({
                "source": source,
                "destination": destination,
                "sha256": hashlib.sha256(content).hexdigest(),
            })
        manifest = {
            "schema_version": 1,
            "source_repository": "https://github.com/mattpocock/skills",
            "source_commit": "a" * 40,
            "source_version": "1.3.1",
            "license": "MIT",
            "supported_skills": supported or ["engineering/example"],
            "files": files,
        }
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        if license_content is not None:
            license_path = self.root / "LICENSE"
            if isinstance(license_content, str):
                license_content = license_content.encode("utf-8")
            license_path.write_bytes(license_content)
        return manifest_path

    def test_provisions_verified_snapshot_deterministically(self):
        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example\n"),
            ("skills/engineering/example/guide.md", "engineering/example/guide.md", b"Guide\n"),
        ])

        first = provision_skills(self.snapshot, manifest, self.destination)
        first_bytes = {path.relative_to(self.destination).as_posix(): path.read_bytes()
                       for path in self.destination.rglob("*") if path.is_file()}
        second = provision_skills(self.snapshot, manifest, self.destination)
        second_bytes = {path.relative_to(self.destination).as_posix(): path.read_bytes()
                        for path in self.destination.rglob("*") if path.is_file()}

        self.assertEqual(first, second)
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(set(first_bytes), {
            "engineering/example/SKILL.md", "engineering/example/guide.md",
        })

    def test_rejects_traversal_before_creating_destination(self):
        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "../outside.txt", b"unsafe"),
        ])

        with self.assertRaisesRegex(ValueError, "destination path"):
            provision_skills(self.snapshot, manifest, self.destination)

        self.assertFalse(self.destination.exists())

    def test_rejects_symlinked_source_before_writing(self):
        actual = self.snapshot / "actual.md"
        actual.write_text("safe", encoding="utf-8")
        (self.snapshot / "skills").symlink_to(actual)
        manifest = self.write_case([])
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["files"] = [{
            "source": "skills/engineering/example/SKILL.md",
            "destination": "engineering/example/SKILL.md",
            "sha256": hashlib.sha256(b"safe").hexdigest(),
        }]
        manifest.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "symlink"):
            provision_skills(self.snapshot, manifest, self.destination)

        self.assertFalse(self.destination.exists())

    def test_rejects_symlinked_destination_before_writing(self):
        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"safe"),
        ])
        outside = self.root / "outside"
        outside.mkdir()
        self.destination.mkdir()
        (self.destination / "engineering").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "symlink"):
            provision_skills(self.snapshot, manifest, self.destination)

        self.assertEqual(list(outside.iterdir()), [])

    def test_rejects_duplicate_destinations_before_writing(self):
        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"one"),
            ("skills/engineering/other/SKILL.md", "engineering/example/SKILL.md", b"two"),
        ], supported=["engineering/example", "engineering/other"])

        with self.assertRaisesRegex(ValueError, "duplicate destination"):
            provision_skills(self.snapshot, manifest, self.destination)

        self.assertFalse(self.destination.exists())

    def test_rejects_malformed_late_entry_before_writing_any_file(self):
        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"one"),
        ])
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["files"].append({"source": "skills/engineering/example/missing.md"})
        manifest.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "malformed file entry"):
            provision_skills(self.snapshot, manifest, self.destination)

        self.assertFalse(self.destination.exists())

    def test_rejects_tampered_snapshot_before_writing_any_file(self):
        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"original"),
        ])
        (self.snapshot / "skills/engineering/example/SKILL.md").write_bytes(b"tampered")

        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            provision_skills(self.snapshot, manifest, self.destination)

        self.assertFalse(self.destination.exists())

    def test_provisions_skills_with_manifest_and_attribution_to_disposable_target(self):
        target_repo = self.root / "disposable_target"
        license_text = b"MIT License\n\nCopyright (c) 2026 Matt Pocock\n"
        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example\n"),
            ("skills/engineering/example/guide.md", "engineering/example/guide.md", b"Guide content\n"),
        ], license_content=license_text)

        staged = provision_skills(self.snapshot, manifest, target_repo, target_manifest=True)

        self.assertEqual(staged, (
            ".agents/skills/LICENSE",
            ".agents/skills/engineering/example/SKILL.md",
            ".agents/skills/engineering/example/guide.md",
            ".agents/skills/manifest.json",
        ))

        manifest_path = target_repo / ".agents/skills/manifest.json"
        license_path = target_repo / ".agents/skills/LICENSE"
        skill_path = target_repo / ".agents/skills/engineering/example/SKILL.md"
        guide_path = target_repo / ".agents/skills/engineering/example/guide.md"

        self.assertTrue(manifest_path.is_file())
        self.assertTrue(license_path.is_file())
        self.assertTrue(skill_path.is_file())
        self.assertTrue(guide_path.is_file())

        self.assertEqual(license_path.read_bytes(), license_text)
        self.assertEqual(skill_path.read_bytes(), b"# Example\n")
        self.assertEqual(guide_path.read_bytes(), b"Guide content\n")

        manifest_data = parse_agent_skills_manifest(manifest_path.read_bytes())
        self.assertIsNotNone(manifest_data)
        self.assertEqual(manifest_data["schema_version"], 1)
        self.assertEqual(manifest_data["managed_by"], "sciflow-cicd")
        self.assertEqual(manifest_data["bundle"], "mattpocock")
        self.assertEqual(manifest_data["source_repository"], "https://github.com/mattpocock/skills")
        self.assertEqual(manifest_data["source_commit"], "a" * 40)
        self.assertEqual(manifest_data["source_version"], "1.3.1")
        self.assertEqual(manifest_data["license"], "MIT")
        self.assertEqual(manifest_data["files"], {
            "LICENSE": hashlib.sha256(license_text).hexdigest(),
            "engineering/example/SKILL.md": hashlib.sha256(b"# Example\n").hexdigest(),
            "engineering/example/guide.md": hashlib.sha256(b"Guide content\n").hexdigest(),
        })

        # Test idempotency: re-running does not fail and preserves contents
        second_staged = provision_skills(self.snapshot, manifest, target_repo, target_manifest=True)
        self.assertEqual(staged, second_staged)

    def test_rejects_missing_license_before_staging_in_disposable_target(self):
        target_repo = self.root / "disposable_target"
        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example\n"),
        ])
        # Note: no LICENSE written in self.root

        with self.assertRaisesRegex(ValueError, "skill catalog license file is missing"):
            provision_skills(self.snapshot, manifest, target_repo, target_manifest=True)

        self.assertFalse(target_repo.exists())

    def test_rejects_empty_license_before_staging(self):
        target_repo = self.root / "disposable_target"
        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example\n"),
        ], license_content=b"   \n")

        with self.assertRaisesRegex(ValueError, "skill catalog license is empty"):
            provision_skills(self.snapshot, manifest, target_repo, target_manifest=True)

        self.assertFalse(target_repo.exists())

    def test_rejects_symlinked_license_before_staging(self):
        target_repo = self.root / "disposable_target"
        actual = self.root / "actual_license"
        actual.write_bytes(b"MIT License\n")
        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example\n"),
        ])
        (self.root / "LICENSE").symlink_to(actual)

        with self.assertRaisesRegex(ValueError, "symlink"):
            provision_skills(self.snapshot, manifest, target_repo, target_manifest=True)

        self.assertFalse(target_repo.exists())

    def test_rejects_license_content_mismatch_before_staging(self):
        target_repo = self.root / "disposable_target"
        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example\n"),
        ], license_content=b"GPL v3 License\n")

        with self.assertRaisesRegex(ValueError, "license content does not match declared license"):
            provision_skills(self.snapshot, manifest, target_repo, target_manifest=True)

        self.assertFalse(target_repo.exists())

    def test_rejects_malformed_provenance_before_staging(self):
        target_repo = self.root / "disposable_target"
        license_text = b"MIT License\n"
        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example\n"),
        ], license_content=license_text)

        # Tamper commit in manifest
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["source_commit"] = "not-a-40-char-hex-sha"
        manifest.write_text(json.dumps(data), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "skill snapshot provenance is malformed"):
            provision_skills(self.snapshot, manifest, target_repo, target_manifest=True)

        self.assertFalse(target_repo.exists())

    def test_rejects_unindexed_snapshot_file_before_staging(self):
        target_repo = self.root / "disposable_target"
        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example\n"),
        ], license_content=b"MIT License\n")

        # Add unindexed file to snapshot
        unindexed = self.snapshot / "skills/engineering/example/unindexed.txt"
        unindexed.write_bytes(b"unindexed")

        with self.assertRaisesRegex(ValueError, "snapshot files do not match the inventory"):
            provision_skills(self.snapshot, manifest, target_repo, target_manifest=True)

        self.assertFalse(target_repo.exists())

    def test_rejects_missing_snapshot_file_before_staging(self):
        target_repo = self.root / "disposable_target"
        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example\n"),
        ], license_content=b"MIT License\n")

        # Delete the file from snapshot
        (self.snapshot / "skills/engineering/example/SKILL.md").unlink()

        with self.assertRaisesRegex(ValueError, "snapshot files do not match the inventory"):
            provision_skills(self.snapshot, manifest, target_repo, target_manifest=True)

        self.assertFalse(target_repo.exists())

    def test_rejects_target_directory_collision_before_writing(self):
        target_repo = self.root / "disposable_target"
        target_repo.mkdir()
        # Create a regular file where .agents/skills/engineering directory needs to be created
        agents_dir = target_repo / ".agents" / "skills"
        agents_dir.mkdir(parents=True)
        (agents_dir / "engineering").write_text("collision-file", encoding="utf-8")

        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example\n"),
        ], license_content=b"MIT License\n")

        with self.assertRaisesRegex(ValueError, "destination directory collision"):
            provision_skills(self.snapshot, manifest, target_repo, target_manifest=True)

        # The collision file must remain untouched and no skill file written
        self.assertEqual((agents_dir / "engineering").read_text(encoding="utf-8"), "collision-file")
        self.assertFalse((agents_dir / "engineering" / "example").exists())

    def test_rejects_target_file_is_directory_collision_before_writing(self):
        target_repo = self.root / "disposable_target"
        target_repo.mkdir()
        # Create a directory where a skill file needs to be written
        bad_path = target_repo / ".agents/skills/engineering/example/SKILL.md"
        bad_path.mkdir(parents=True)

        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example\n"),
        ], license_content=b"MIT License\n")

        with self.assertRaisesRegex(ValueError, "destination is not a regular file"):
            provision_skills(self.snapshot, manifest, target_repo, target_manifest=True)

    def test_rejects_local_modifications_in_disposable_target(self):
        target_repo = self.root / "disposable_target"
        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example\n"),
        ], license_content=b"MIT License\n")

        provision_skills(self.snapshot, manifest, target_repo, target_manifest=True)

        # Locally tamper with the installed skill
        skill_path = target_repo / ".agents/skills/engineering/example/SKILL.md"
        skill_path.write_bytes(b"# Local edit\n")

        with self.assertRaisesRegex(ValueError, "destination has local changes"):
            provision_skills(self.snapshot, manifest, target_repo, target_manifest=True)

        # Content remains local edit
        self.assertEqual(skill_path.read_bytes(), b"# Local edit\n")

    def test_rejects_malformed_prior_manifest_in_disposable_target(self):
        target_repo = self.root / "disposable_target"
        manifest = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example\n"),
        ], license_content=b"MIT License\n")

        # Create target with malformed manifest
        manifest_file = target_repo / ".agents/skills/manifest.json"
        manifest_file.parent.mkdir(parents=True)
        manifest_file.write_text("not-json", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "existing target manifest is malformed"):
            provision_skills(self.snapshot, manifest, target_repo, target_manifest=True)

    def test_upgrade_cleanly_updates_matching_files_in_disposable_target(self):
        target_repo = self.root / "disposable_target"
        license_text = b"MIT License\n\nCopyright (c) 2026 Matt Pocock\n"
        manifest_v1 = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example v1\n"),
            ("skills/engineering/example/guide.md", "engineering/example/guide.md", b"Guide content\n"),
        ], license_content=license_text)

        provision_skills(self.snapshot, manifest_v1, target_repo, target_manifest=True)

        # Advance snapshot to v2
        (self.snapshot / "skills/engineering/example/SKILL.md").write_bytes(b"# Example v2 updated\n")
        files_v2 = [
            {
                "source": "skills/engineering/example/SKILL.md",
                "destination": "engineering/example/SKILL.md",
                "sha256": hashlib.sha256(b"# Example v2 updated\n").hexdigest(),
            },
            {
                "source": "skills/engineering/example/guide.md",
                "destination": "engineering/example/guide.md",
                "sha256": hashlib.sha256(b"Guide content\n").hexdigest(),
            },
        ]
        manifest_v2_data = {
            "schema_version": 1,
            "source_repository": "https://github.com/mattpocock/skills",
            "source_commit": "b" * 40,
            "source_version": "1.4.0",
            "license": "MIT",
            "supported_skills": ["engineering/example"],
            "files": files_v2,
        }
        manifest_v2 = self.root / "manifest_v2.json"
        manifest_v2.write_text(json.dumps(manifest_v2_data), encoding="utf-8")

        staged_v2 = provision_skills(self.snapshot, manifest_v2, target_repo, target_manifest=True)

        self.assertEqual(staged_v2, (
            ".agents/skills/LICENSE",
            ".agents/skills/engineering/example/SKILL.md",
            ".agents/skills/engineering/example/guide.md",
            ".agents/skills/manifest.json",
        ))

        skill_path = target_repo / ".agents/skills/engineering/example/SKILL.md"
        guide_path = target_repo / ".agents/skills/engineering/example/guide.md"
        manifest_path = target_repo / ".agents/skills/manifest.json"

        self.assertEqual(skill_path.read_bytes(), b"# Example v2 updated\n")
        self.assertEqual(guide_path.read_bytes(), b"Guide content\n")
        manifest_data = parse_agent_skills_manifest(manifest_path.read_bytes())
        self.assertEqual(manifest_data["source_version"], "1.4.0")
        self.assertEqual(manifest_data["source_commit"], "b" * 40)
        self.assertEqual(manifest_data["files"]["engineering/example/SKILL.md"],
                         hashlib.sha256(b"# Example v2 updated\n").hexdigest())

    def test_upgrade_blocks_local_edits_without_modifying_destination(self):
        target_repo = self.root / "disposable_target"
        license_text = b"MIT License\n\nCopyright (c) 2026 Matt Pocock\n"
        manifest_v1 = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example v1\n"),
        ], license_content=license_text)

        provision_skills(self.snapshot, manifest_v1, target_repo, target_manifest=True)

        # Owner edits target locally
        skill_path = target_repo / ".agents/skills/engineering/example/SKILL.md"
        skill_path.write_bytes(b"# Local custom edit\n")

        # Advance catalog to v2
        (self.snapshot / "skills/engineering/example/SKILL.md").write_bytes(b"# Example v2 updated\n")
        manifest_v2_data = {
            "schema_version": 1,
            "source_repository": "https://github.com/mattpocock/skills",
            "source_commit": "b" * 40,
            "source_version": "1.4.0",
            "license": "MIT",
            "supported_skills": ["engineering/example"],
            "files": [{
                "source": "skills/engineering/example/SKILL.md",
                "destination": "engineering/example/SKILL.md",
                "sha256": hashlib.sha256(b"# Example v2 updated\n").hexdigest(),
            }],
        }
        manifest_v2 = self.root / "manifest_v2.json"
        manifest_v2.write_text(json.dumps(manifest_v2_data), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "destination has local changes: .agents/skills/engineering/example/SKILL.md"):
            provision_skills(self.snapshot, manifest_v2, target_repo, target_manifest=True)

        # Verify nothing was modified
        self.assertEqual(skill_path.read_bytes(), b"# Local custom edit\n")
        manifest_path = target_repo / ".agents/skills/manifest.json"
        manifest_data = parse_agent_skills_manifest(manifest_path.read_bytes())
        self.assertEqual(manifest_data["source_version"], "1.3.1")

    def test_upgrade_blocks_deleted_prior_file_without_modifying_destination(self):
        target_repo = self.root / "disposable_target"
        license_text = b"MIT License\n\nCopyright (c) 2026 Matt Pocock\n"
        manifest_v1 = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example v1\n"),
            ("skills/engineering/example/guide.md", "engineering/example/guide.md", b"Guide content\n"),
        ], license_content=license_text)

        provision_skills(self.snapshot, manifest_v1, target_repo, target_manifest=True)

        # Delete guide.md in target repository to attempt to bypass conflict
        guide_path = target_repo / ".agents/skills/engineering/example/guide.md"
        guide_path.unlink()

        # Advance catalog to v2
        (self.snapshot / "skills/engineering/example/SKILL.md").write_bytes(b"# Example v2 updated\n")
        manifest_v2_data = {
            "schema_version": 1,
            "source_repository": "https://github.com/mattpocock/skills",
            "source_commit": "b" * 40,
            "source_version": "1.4.0",
            "license": "MIT",
            "supported_skills": ["engineering/example"],
            "files": [
                {
                    "source": "skills/engineering/example/SKILL.md",
                    "destination": "engineering/example/SKILL.md",
                    "sha256": hashlib.sha256(b"# Example v2 updated\n").hexdigest(),
                },
                {
                    "source": "skills/engineering/example/guide.md",
                    "destination": "engineering/example/guide.md",
                    "sha256": hashlib.sha256(b"Guide content\n").hexdigest(),
                },
            ],
        }
        manifest_v2 = self.root / "manifest_v2.json"
        manifest_v2.write_text(json.dumps(manifest_v2_data), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "destination has local changes: .agents/skills/engineering/example/guide.md"):
            provision_skills(self.snapshot, manifest_v2, target_repo, target_manifest=True)

        # SKILL.md remains v1 and manifest remains v1
        skill_path = target_repo / ".agents/skills/engineering/example/SKILL.md"
        self.assertEqual(skill_path.read_bytes(), b"# Example v1\n")
        manifest_path = target_repo / ".agents/skills/manifest.json"
        manifest_data = parse_agent_skills_manifest(manifest_path.read_bytes())
        self.assertEqual(manifest_data["source_version"], "1.3.1")

    def test_upgrade_blocks_when_newer_catalog_drops_a_skill(self):
        target_repo = self.root / "disposable_target"
        license_text = b"MIT License\n\nCopyright (c) 2026 Matt Pocock\n"
        manifest_v1 = self.write_case([
            ("skills/engineering/kept/SKILL.md", "engineering/kept/SKILL.md", b"# Kept\n"),
            ("skills/engineering/dropped/SKILL.md", "engineering/dropped/SKILL.md", b"# Dropped\n"),
        ], supported=["engineering/kept", "engineering/dropped"], license_content=license_text)

        provision_skills(self.snapshot, manifest_v1, target_repo, target_manifest=True)
        (self.snapshot / "skills/engineering/dropped/SKILL.md").unlink()

        dropped_path = target_repo / ".agents/skills/engineering/dropped/SKILL.md"
        manifest_v2_data = {
            "schema_version": 1,
            "source_repository": "https://github.com/mattpocock/skills",
            "source_commit": "b" * 40,
            "source_version": "1.4.0",
            "license": "MIT",
            "supported_skills": ["engineering/kept"],
            "files": [{
                "source": "skills/engineering/kept/SKILL.md",
                "destination": "engineering/kept/SKILL.md",
                "sha256": hashlib.sha256(b"# Kept\n").hexdigest(),
            }],
        }
        manifest_v2 = self.root / "manifest_v2.json"
        manifest_v2.write_text(json.dumps(manifest_v2_data), encoding="utf-8")

        with self.assertRaisesRegex(
            ValueError,
            r"upstream catalog revision no longer ships.*engineering/dropped.*allow_removed_skills",
        ):
            provision_skills(self.snapshot, manifest_v2, target_repo, target_manifest=True)

        self.assertEqual(dropped_path.read_bytes(), b"# Dropped\n")
        manifest_data = parse_agent_skills_manifest(
            (target_repo / ".agents/skills/manifest.json").read_bytes())
        self.assertEqual(manifest_data["source_version"], "1.3.1")

    def test_upgrade_with_acknowledged_removal_deletes_only_removed_skill(self):
        target_repo = self.root / "disposable_target"
        license_text = b"MIT License\n\nCopyright (c) 2026 Matt Pocock\n"
        manifest_v1 = self.write_case([
            ("skills/engineering/kept/SKILL.md", "engineering/kept/SKILL.md", b"# Kept v1\n"),
            ("skills/engineering/dropped/SKILL.md", "engineering/dropped/SKILL.md", b"# Dropped\n"),
            ("skills/engineering/dropped/guide.md", "engineering/dropped/guide.md", b"Guide\n"),
        ], supported=["engineering/kept", "engineering/dropped"], license_content=license_text)

        provision_skills(self.snapshot, manifest_v1, target_repo, target_manifest=True)
        (self.snapshot / "skills/engineering/dropped/SKILL.md").unlink()
        (self.snapshot / "skills/engineering/dropped/guide.md").unlink()

        (self.snapshot / "skills/engineering/kept/SKILL.md").write_bytes(b"# Kept v2\n")
        manifest_v2_data = {
            "schema_version": 1,
            "source_repository": "https://github.com/mattpocock/skills",
            "source_commit": "b" * 40,
            "source_version": "1.4.0",
            "license": "MIT",
            "supported_skills": ["engineering/kept"],
            "files": [{
                "source": "skills/engineering/kept/SKILL.md",
                "destination": "engineering/kept/SKILL.md",
                "sha256": hashlib.sha256(b"# Kept v2\n").hexdigest(),
            }],
        }
        manifest_v2 = self.root / "manifest_v2.json"
        manifest_v2.write_text(json.dumps(manifest_v2_data), encoding="utf-8")

        provision_skills(
            self.snapshot, manifest_v2, target_repo, target_manifest=True,
            allow_removals=["engineering/dropped"])

        self.assertEqual(
            (target_repo / ".agents/skills/engineering/kept/SKILL.md").read_bytes(),
            b"# Kept v2\n")
        self.assertFalse((target_repo / ".agents/skills/engineering/dropped").exists())
        manifest_data = parse_agent_skills_manifest(
            (target_repo / ".agents/skills/manifest.json").read_bytes())
        self.assertEqual(manifest_data["source_version"], "1.4.0")
        self.assertNotIn("engineering/dropped/SKILL.md", manifest_data["files"])

    def test_partial_removal_failure_rolls_back_to_consistent_state(self):
        target_repo = self.root / "disposable_target"
        license_text = b"MIT License\n"
        manifest_v1 = self.write_case([
            ("skills/engineering/kept/SKILL.md", "engineering/kept/SKILL.md", b"# Kept v1\n"),
            ("skills/engineering/dropped/SKILL.md", "engineering/dropped/SKILL.md", b"# Dropped\n"),
            ("skills/engineering/other/SKILL.md", "engineering/other/SKILL.md", b"# Other\n"),
        ], supported=["engineering/kept", "engineering/dropped", "engineering/other"],
            license_content=license_text)

        provision_skills(self.snapshot, manifest_v1, target_repo, target_manifest=True)

        # v2 drops 'dropped' and 'other', updates 'kept'
        (self.snapshot / "skills/engineering/dropped/SKILL.md").unlink()
        (self.snapshot / "skills/engineering/other/SKILL.md").unlink()
        (self.snapshot / "skills/engineering/kept/SKILL.md").write_bytes(b"# Kept v2\n")
        manifest_v2_data = {
            "schema_version": 1,
            "source_repository": "https://github.com/mattpocock/skills",
            "source_commit": "b" * 40,
            "source_version": "1.4.0",
            "license": "MIT",
            "supported_skills": ["engineering/kept"],
            "files": [{
                "source": "skills/engineering/kept/SKILL.md",
                "destination": "engineering/kept/SKILL.md",
                "sha256": hashlib.sha256(b"# Kept v2\n").hexdigest(),
            }],
        }
        manifest_v2 = self.root / "manifest_v2.json"
        manifest_v2.write_text(json.dumps(manifest_v2_data), encoding="utf-8")

        kept_path = target_repo / ".agents/skills/engineering/kept/SKILL.md"
        dropped_path = target_repo / ".agents/skills/engineering/dropped/SKILL.md"
        other_path = target_repo / ".agents/skills/engineering/other/SKILL.md"
        manifest_path = target_repo / ".agents/skills/manifest.json"

        original_unlink = Path.unlink

        def failing_unlink(path, *args, **kwargs):
            if path == other_path:
                raise OSError("simulated removal failure")
            return original_unlink(path, *args, **kwargs)

        with mock.patch("pathlib.Path.unlink", new=failing_unlink):
            with self.assertRaisesRegex(OSError, "simulated removal failure"):
                provision_skills(
                    self.snapshot, manifest_v2, target_repo, target_manifest=True,
                    allow_removals=["engineering/dropped", "engineering/other"])

        # Rolled back to the pre-upgrade consistent state: no mixed state.
        self.assertEqual(kept_path.read_bytes(), b"# Kept v1\n")
        self.assertEqual(dropped_path.read_bytes(), b"# Dropped\n")
        self.assertEqual(other_path.read_bytes(), b"# Other\n")
        manifest_data = parse_agent_skills_manifest(manifest_path.read_bytes())
        self.assertEqual(manifest_data["source_version"], "1.3.1")

    def test_rollback_continues_when_one_file_restore_fails_and_reports_context(self):
        target_repo = self.root / "disposable_target"
        license_text = b"MIT License\n"
        manifest_v1 = self.write_case([
            ("skills/engineering/kept/SKILL.md", "engineering/kept/SKILL.md", b"# Kept v1\n"),
            ("skills/engineering/dropped1/SKILL.md", "engineering/dropped1/SKILL.md", b"# Dropped 1\n"),
            ("skills/engineering/dropped2/SKILL.md", "engineering/dropped2/SKILL.md", b"# Dropped 2\n"),
        ], supported=["engineering/kept", "engineering/dropped1", "engineering/dropped2"],
            license_content=license_text)

        provision_skills(self.snapshot, manifest_v1, target_repo, target_manifest=True)

        # v2 drops dropped1 and dropped2, updates kept
        (self.snapshot / "skills/engineering/dropped1/SKILL.md").unlink()
        (self.snapshot / "skills/engineering/dropped2/SKILL.md").unlink()
        (self.snapshot / "skills/engineering/kept/SKILL.md").write_bytes(b"# Kept v2\n")

        manifest_v2_data = {
            "schema_version": 1,
            "source_repository": "https://github.com/mattpocock/skills",
            "source_commit": "b" * 40,
            "source_version": "1.4.0",
            "license": "MIT",
            "supported_skills": ["engineering/kept"],
            "files": [{
                "source": "skills/engineering/kept/SKILL.md",
                "destination": "engineering/kept/SKILL.md",
                "sha256": hashlib.sha256(b"# Kept v2\n").hexdigest(),
            }],
        }
        manifest_v2 = self.root / "manifest_v2.json"
        manifest_v2.write_text(json.dumps(manifest_v2_data), encoding="utf-8")

        kept_path = target_repo / ".agents/skills/engineering/kept/SKILL.md"
        dropped1_path = target_repo / ".agents/skills/engineering/dropped1/SKILL.md"
        dropped2_path = target_repo / ".agents/skills/engineering/dropped2/SKILL.md"
        manifest_path = target_repo / ".agents/skills/manifest.json"

        original_unlink = Path.unlink
        original_write_bytes = Path.write_bytes
        removal_failed = False
        restore_failed_triggered = False

        def failing_unlink(path, *args, **kwargs):
            nonlocal removal_failed
            if path == dropped2_path:
                removal_failed = True
                raise OSError("simulated removal failure")
            return original_unlink(path, *args, **kwargs)

        def failing_write_bytes(path, data, *args, **kwargs):
            nonlocal restore_failed_triggered
            # Fail restore of dropped1_path during rollback
            if removal_failed and path == dropped1_path:
                restore_failed_triggered = True
                raise OSError("simulated restore failure")
            return original_write_bytes(path, data, *args, **kwargs)

        with mock.patch("pathlib.Path.unlink", new=failing_unlink), \
             mock.patch("pathlib.Path.write_bytes", new=failing_write_bytes):
            with self.assertRaises(OSError) as ctx:
                provision_skills(
                    self.snapshot, manifest_v2, target_repo, target_manifest=True,
                    allow_removals=["engineering/dropped1", "engineering/dropped2"])

        exc = ctx.exception
        # Assert original removal error is raised
        self.assertEqual(str(exc), "simulated removal failure")
        self.assertTrue(restore_failed_triggered)

        # Assert secondary context contains restore failure
        self.assertTrue(hasattr(exc, "restore_failures"))
        self.assertEqual(len(exc.restore_failures), 1)
        self.assertIn("simulated restore failure", str(exc.restore_failures[0]))
        self.assertIsNotNone(exc.__context__)
        self.assertIn("simulated restore failure", str(exc.__context__))

        # Assert remaining files in backups were still restored
        self.assertEqual(kept_path.read_bytes(), b"# Kept v1\n")
        manifest_data = parse_agent_skills_manifest(manifest_path.read_bytes())
        self.assertEqual(manifest_data["source_version"], "1.3.1")
        self.assertEqual(dropped2_path.read_bytes(), b"# Dropped 2\n")

    def test_rollback_recreates_pruned_directories(self):
        target_repo = self.root / "disposable_target"
        license_text = b"MIT License\n"
        manifest_v1 = self.write_case([
            ("skills/engineering/dropped-dir/SKILL.md", "engineering/dropped-dir/SKILL.md", b"# Dropped root\n"),
            ("skills/engineering/dropped-dir/sub/extra.md", "engineering/dropped-dir/sub/extra.md", b"# Pruned sub\n"),
            ("skills/engineering/failing/SKILL.md", "engineering/failing/SKILL.md", b"# Failing\n"),
            ("skills/engineering/kept/SKILL.md", "engineering/kept/SKILL.md", b"# Kept\n"),
        ], supported=["engineering/dropped-dir", "engineering/failing", "engineering/kept"],
            license_content=license_text)

        provision_skills(self.snapshot, manifest_v1, target_repo, target_manifest=True)

        pruned_sub_dir = target_repo / ".agents/skills/engineering/dropped-dir/sub"
        pruned_skill_dir = target_repo / ".agents/skills/engineering/dropped-dir"
        pruned_skill_file = pruned_skill_dir / "SKILL.md"
        pruned_sub_file = pruned_sub_dir / "extra.md"
        failing_file = target_repo / ".agents/skills/engineering/failing/SKILL.md"
        kept_file = target_repo / ".agents/skills/engineering/kept/SKILL.md"

        self.assertTrue(pruned_sub_dir.is_dir())
        self.assertTrue(pruned_sub_file.is_file())

        # v2 drops dropped-dir and failing, keeps kept
        (self.snapshot / "skills/engineering/dropped-dir/SKILL.md").unlink()
        (self.snapshot / "skills/engineering/dropped-dir/sub/extra.md").unlink()
        (self.snapshot / "skills/engineering/failing/SKILL.md").unlink()

        manifest_v2_data = {
            "schema_version": 1,
            "source_repository": "https://github.com/mattpocock/skills",
            "source_commit": "b" * 40,
            "source_version": "1.4.0",
            "license": "MIT",
            "supported_skills": ["engineering/kept"],
            "files": [{
                "source": "skills/engineering/kept/SKILL.md",
                "destination": "engineering/kept/SKILL.md",
                "sha256": hashlib.sha256(b"# Kept\n").hexdigest(),
            }],
        }
        manifest_v2 = self.root / "manifest_v2.json"
        manifest_v2.write_text(json.dumps(manifest_v2_data), encoding="utf-8")

        original_unlink = Path.unlink
        dir_was_pruned = False

        def failing_unlink(path, *args, **kwargs):
            nonlocal dir_was_pruned
            if path == failing_file:
                # Confirm pruned_sub_dir and pruned_skill_dir were pruned prior to failure
                if not pruned_sub_dir.exists() and not pruned_skill_dir.exists():
                    dir_was_pruned = True
                raise OSError("simulated removal failure on second skill")
            return original_unlink(path, *args, **kwargs)

        with mock.patch("pathlib.Path.unlink", new=failing_unlink):
            with self.assertRaisesRegex(OSError, "simulated removal failure on second skill"):
                provision_skills(
                    self.snapshot, manifest_v2, target_repo, target_manifest=True,
                    allow_removals=["engineering/dropped-dir", "engineering/failing"])

        self.assertTrue(dir_was_pruned, "pruned directories should have been pruned before failure")
        # Assert that after rollback, the pruned directories exist again and contain their restored files
        self.assertTrue(pruned_skill_dir.is_dir())
        self.assertTrue(pruned_sub_dir.is_dir())
        self.assertTrue(pruned_skill_file.is_file())
        self.assertEqual(pruned_skill_file.read_bytes(), b"# Dropped root\n")
        self.assertTrue(pruned_sub_file.is_file())
        self.assertEqual(pruned_sub_file.read_bytes(), b"# Pruned sub\n")
        self.assertTrue(failing_file.is_file())
        self.assertEqual(failing_file.read_bytes(), b"# Failing\n")
        self.assertTrue(kept_file.is_file())
        self.assertEqual(kept_file.read_bytes(), b"# Kept\n")

    def test_rollback_destination_recreates_empty_pruned_dirs_and_accumulates_restore_failures(self):
        empty_dir = self.root / "empty_pruned_dir"
        file_to_restore = self.root / "restore_dir" / "file.txt"
        created_file = self.root / "created.txt"
        created_file.write_text("created")

        with mock.patch("pathlib.Path.write_bytes", side_effect=OSError("disk full")):
            failures = _rollback_destination(
                {file_to_restore: b"content"},
                {created_file},
                pruned_dirs=[empty_dir]
            )

        self.assertTrue(empty_dir.is_dir())
        self.assertFalse(created_file.exists())
        self.assertEqual(len(failures), 1)
        self.assertIn("disk full", str(failures[0]))

    def test_partial_write_failure_rolls_back_to_consistent_state(self):
        target_repo = self.root / "disposable_target"
        license_text = b"MIT License\n"
        manifest_v1 = self.write_case([
            ("skills/engineering/kept/SKILL.md", "engineering/kept/SKILL.md", b"# Kept v1\n"),
        ], supported=["engineering/kept"], license_content=license_text)

        provision_skills(self.snapshot, manifest_v1, target_repo, target_manifest=True)

        # v2 updates 'kept' and introduces 'added'
        (self.snapshot / "skills/engineering/kept/SKILL.md").write_bytes(b"# Kept v2\n")
        added_snapshot = self.snapshot / "skills/engineering/added/SKILL.md"
        added_snapshot.parent.mkdir(parents=True, exist_ok=True)
        added_snapshot.write_bytes(b"# Added v2\n")

        manifest_v2_data = {
            "schema_version": 1,
            "source_repository": "https://github.com/mattpocock/skills",
            "source_commit": "b" * 40,
            "source_version": "1.4.0",
            "license": "MIT",
            "supported_skills": ["engineering/added", "engineering/kept"],
            "files": [
                {
                    "source": "skills/engineering/added/SKILL.md",
                    "destination": "engineering/added/SKILL.md",
                    "sha256": hashlib.sha256(b"# Added v2\n").hexdigest(),
                },
                {
                    "source": "skills/engineering/kept/SKILL.md",
                    "destination": "engineering/kept/SKILL.md",
                    "sha256": hashlib.sha256(b"# Kept v2\n").hexdigest(),
                },
            ],
        }
        manifest_v2 = self.root / "manifest_v2.json"
        manifest_v2.write_text(json.dumps(manifest_v2_data), encoding="utf-8")

        kept_path = target_repo / ".agents/skills/engineering/kept/SKILL.md"
        added_path = target_repo / ".agents/skills/engineering/added/SKILL.md"
        manifest_path = target_repo / ".agents/skills/manifest.json"

        # Staged order is sorted by relative target:
        # 1. engineering/added/SKILL.md (created)
        # 2. engineering/kept/SKILL.md (modified)
        # 3. LICENSE (unmodified)
        # 4. manifest.json (modified)
        #
        # If we simulate a failure on writing manifest_path, both added_path (created)
        # and kept_path (modified) have already been written to disk before failure.
        original_write_bytes = Path.write_bytes
        write_failure_triggered = False

        def failing_write_bytes(path, data, *args, **kwargs):
            nonlocal write_failure_triggered
            if path == manifest_path and not write_failure_triggered:
                write_failure_triggered = True
                raise OSError("simulated write failure")
            return original_write_bytes(path, data, *args, **kwargs)

        with mock.patch("pathlib.Path.write_bytes", new=failing_write_bytes):
            with self.assertRaisesRegex(OSError, "simulated write failure"):
                provision_skills(
                    self.snapshot, manifest_v2, target_repo, target_manifest=True)

        self.assertTrue(write_failure_triggered)
        # Modified file rolled back to pre-upgrade v1 content
        self.assertEqual(kept_path.read_bytes(), b"# Kept v1\n")
        # Created file unlinked during rollback
        self.assertFalse(added_path.exists())
        # Target manifest matches pre-upgrade v1 state
        manifest_data = parse_agent_skills_manifest(manifest_path.read_bytes())
        self.assertEqual(manifest_data["source_version"], "1.3.1")
        self.assertNotIn("engineering/added/SKILL.md", manifest_data["files"])
        self.assertEqual(manifest_data["files"]["engineering/kept/SKILL.md"],
                         hashlib.sha256(b"# Kept v1\n").hexdigest())

    def test_upgrade_removal_acknowledgment_requires_the_exact_set(self):
        target_repo = self.root / "disposable_target"
        license_text = b"MIT License\n"
        manifest_v1 = self.write_case([
            ("skills/engineering/kept/SKILL.md", "engineering/kept/SKILL.md", b"# Kept\n"),
            ("skills/engineering/dropped/SKILL.md", "engineering/dropped/SKILL.md", b"# Dropped\n"),
            ("skills/engineering/other/SKILL.md", "engineering/other/SKILL.md", b"# Other\n"),
        ], supported=["engineering/kept", "engineering/dropped", "engineering/other"],
            license_content=license_text)

        provision_skills(self.snapshot, manifest_v1, target_repo, target_manifest=True)
        (self.snapshot / "skills/engineering/dropped/SKILL.md").unlink()
        (self.snapshot / "skills/engineering/other/SKILL.md").unlink()

        manifest_v2_data = {
            "schema_version": 1,
            "source_repository": "https://github.com/mattpocock/skills",
            "source_commit": "b" * 40,
            "source_version": "1.4.0",
            "license": "MIT",
            "supported_skills": ["engineering/kept"],
            "files": [{
                "source": "skills/engineering/kept/SKILL.md",
                "destination": "engineering/kept/SKILL.md",
                "sha256": hashlib.sha256(b"# Kept\n").hexdigest(),
            }],
        }
        manifest_v2 = self.root / "manifest_v2.json"
        manifest_v2.write_text(json.dumps(manifest_v2_data), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "exactly"):
            provision_skills(
                self.snapshot, manifest_v2, target_repo, target_manifest=True,
                allow_removals=["engineering/dropped"])

        with self.assertRaisesRegex(ValueError, "exactly"):
            provision_skills(
                self.snapshot, manifest_v2, target_repo, target_manifest=True,
                allow_removals=["engineering/dropped", "engineering/other", "engineering/absent"])

        self.assertTrue((target_repo / ".agents/skills/engineering/other/SKILL.md").exists())

    def test_locally_edited_removed_skill_blocks_acknowledged_removal(self):
        target_repo = self.root / "disposable_target"
        license_text = b"MIT License\n"
        manifest_v1 = self.write_case([
            ("skills/engineering/kept/SKILL.md", "engineering/kept/SKILL.md", b"# Kept\n"),
            ("skills/engineering/dropped/SKILL.md", "engineering/dropped/SKILL.md", b"# Dropped\n"),
        ], supported=["engineering/kept", "engineering/dropped"], license_content=license_text)

        provision_skills(self.snapshot, manifest_v1, target_repo, target_manifest=True)
        (self.snapshot / "skills/engineering/dropped/SKILL.md").unlink()

        dropped_path = target_repo / ".agents/skills/engineering/dropped/SKILL.md"
        dropped_path.write_bytes(b"# Local edit\n")

        manifest_v2_data = {
            "schema_version": 1,
            "source_repository": "https://github.com/mattpocock/skills",
            "source_commit": "b" * 40,
            "source_version": "1.4.0",
            "license": "MIT",
            "supported_skills": ["engineering/kept"],
            "files": [{
                "source": "skills/engineering/kept/SKILL.md",
                "destination": "engineering/kept/SKILL.md",
                "sha256": hashlib.sha256(b"# Kept\n").hexdigest(),
            }],
        }
        manifest_v2 = self.root / "manifest_v2.json"
        manifest_v2.write_text(json.dumps(manifest_v2_data), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "local changes"):
            provision_skills(
                self.snapshot, manifest_v2, target_repo, target_manifest=True,
                allow_removals=["engineering/dropped"])

        self.assertEqual(dropped_path.read_bytes(), b"# Local edit\n")
        self.assertTrue((target_repo / ".agents/skills/engineering/kept/SKILL.md").exists())

    def test_rename_is_acknowledged_through_its_previous_skill_id(self):
        target_repo = self.root / "disposable_target"
        license_text = b"MIT License\n"
        manifest_v1 = self.write_case([
            ("skills/engineering/old-name/SKILL.md", "engineering/old-name/SKILL.md", b"# Same\n"),
        ], supported=["engineering/old-name"], license_content=license_text)

        provision_skills(self.snapshot, manifest_v1, target_repo, target_manifest=True)

        (self.snapshot / "skills/engineering/new-name").mkdir(parents=True)
        (self.snapshot / "skills/engineering/new-name/SKILL.md").write_bytes(b"# Same\n")
        (self.snapshot / "skills/engineering/old-name/SKILL.md").unlink()
        manifest_v2_data = {
            "schema_version": 1,
            "source_repository": "https://github.com/mattpocock/skills",
            "source_commit": "b" * 40,
            "source_version": "1.4.0",
            "license": "MIT",
            "supported_skills": ["engineering/new-name"],
            "files": [{
                "source": "skills/engineering/new-name/SKILL.md",
                "destination": "engineering/new-name/SKILL.md",
                "sha256": hashlib.sha256(b"# Same\n").hexdigest(),
            }],
        }
        manifest_v2 = self.root / "manifest_v2.json"
        manifest_v2.write_text(json.dumps(manifest_v2_data), encoding="utf-8")

        with self.assertRaisesRegex(
            ValueError,
            r"upstream catalog revision no longer ships.*engineering/old-name.*allow_removed_skills",
        ):
            provision_skills(self.snapshot, manifest_v2, target_repo, target_manifest=True)

        provision_skills(
            self.snapshot, manifest_v2, target_repo, target_manifest=True,
            allow_removals=["engineering/old-name"])

        self.assertFalse((target_repo / ".agents/skills/engineering/old-name").exists())
        self.assertEqual(
            (target_repo / ".agents/skills/engineering/new-name/SKILL.md").read_bytes(),
            b"# Same\n")

    def test_untracked_file_inside_installed_skill_folder_is_preserved(self):
        target_repo = self.root / "disposable_target"
        license_text = b"MIT License\n"
        manifest_v1 = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example v1\n"),
        ], license_content=license_text)

        provision_skills(self.snapshot, manifest_v1, target_repo, target_manifest=True)

        untracked = target_repo / ".agents/skills/engineering/example/local-notes.md"
        untracked.write_bytes(b"# Owner notes\n")

        (self.snapshot / "skills/engineering/example/SKILL.md").write_bytes(b"# Example v2\n")
        manifest_v2_data = {
            "schema_version": 1,
            "source_repository": "https://github.com/mattpocock/skills",
            "source_commit": "b" * 40,
            "source_version": "1.4.0",
            "license": "MIT",
            "supported_skills": ["engineering/example"],
            "files": [{
                "source": "skills/engineering/example/SKILL.md",
                "destination": "engineering/example/SKILL.md",
                "sha256": hashlib.sha256(b"# Example v2\n").hexdigest(),
            }],
        }
        manifest_v2 = self.root / "manifest_v2.json"
        manifest_v2.write_text(json.dumps(manifest_v2_data), encoding="utf-8")

        provision_skills(self.snapshot, manifest_v2, target_repo, target_manifest=True)

        self.assertEqual(untracked.read_bytes(), b"# Owner notes\n")
        self.assertEqual(
            (target_repo / ".agents/skills/engineering/example/SKILL.md").read_bytes(),
            b"# Example v2\n")

    def test_local_edit_conflict_reports_installed_local_and_proposed_digests(self):
        target_repo = self.root / "disposable_target"
        license_text = b"MIT License\n"
        manifest_v1 = self.write_case([
            ("skills/engineering/example/SKILL.md", "engineering/example/SKILL.md", b"# Example v1\n"),
        ], license_content=license_text)

        provision_skills(self.snapshot, manifest_v1, target_repo, target_manifest=True)

        skill_path = target_repo / ".agents/skills/engineering/example/SKILL.md"
        skill_path.write_bytes(b"# Local edit\n")

        (self.snapshot / "skills/engineering/example/SKILL.md").write_bytes(b"# Example v2\n")
        manifest_v2_data = {
            "schema_version": 1,
            "source_repository": "https://github.com/mattpocock/skills",
            "source_commit": "b" * 40,
            "source_version": "1.4.0",
            "license": "MIT",
            "supported_skills": ["engineering/example"],
            "files": [{
                "source": "skills/engineering/example/SKILL.md",
                "destination": "engineering/example/SKILL.md",
                "sha256": hashlib.sha256(b"# Example v2\n").hexdigest(),
            }],
        }
        manifest_v2 = self.root / "manifest_v2.json"
        manifest_v2.write_text(json.dumps(manifest_v2_data), encoding="utf-8")

        with self.assertRaises(ValueError) as ctx:
            provision_skills(self.snapshot, manifest_v2, target_repo, target_manifest=True)

        message = str(ctx.exception)
        self.assertIn("installed sha256 " + hashlib.sha256(b"# Example v1\n").hexdigest(), message)
        self.assertIn("local sha256 " + hashlib.sha256(b"# Local edit\n").hexdigest(), message)
        self.assertIn("proposed sha256 " + hashlib.sha256(b"# Example v2\n").hexdigest(), message)

    def test_load_verified_catalog_checked_in_snapshot(self):
        repository = Path(__file__).resolve().parents[1]
        catalog_dir = repository / "data/agent-skills/mattpocock"

        files, manifest = load_verified_catalog(catalog_dir)

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["managed_by"], "sciflow-cicd")
        self.assertEqual(manifest["bundle"], "mattpocock")
        self.assertEqual(manifest["source_repository"], "https://github.com/mattpocock/skills")
        self.assertEqual(manifest["license"], "MIT")
        self.assertIn("LICENSE", manifest["files"])
        self.assertIn("engineering/implement-spec/SKILL.md", manifest["files"])
        self.assertEqual(len(manifest["files"]), 80)

        self.assertIn(".agents/skills/LICENSE", files)
        self.assertIn(".agents/skills/manifest.json", files)
        self.assertIn(".agents/skills/engineering/implement-spec/SKILL.md", files)
        self.assertEqual(len(files), 81)

    def test_load_verified_catalog_rejects_invalid_catalog(self):
        nonexistent = self.root / "nonexistent"
        with self.assertRaisesRegex(ValueError, "skill catalog directory is missing"):
            load_verified_catalog(nonexistent)

        # Missing manifest
        empty_dir = self.root / "empty_catalog"
        empty_dir.mkdir()
        with self.assertRaisesRegex(ValueError, "manifest is unreadable"):
            load_verified_catalog(empty_dir)


if __name__ == "__main__":
    unittest.main()
