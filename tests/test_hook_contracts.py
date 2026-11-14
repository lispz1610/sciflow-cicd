"""Unit tests for opt-in hook package contracts and validation."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.hook_contracts import (
    HookManifest,
    HookPackage,
    HookValidationError,
    SCHEMA_VERSION,
    SUPPORTED_EVENTS,
    SUPPORTED_RUNTIMES,
    filter_enabled_hooks,
    resolve_active_hooks,
    validate_hook_manifest,
    validate_hook_package,
    validate_hook_packages,
)


def _compute_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class HookManifestValidationTests(unittest.TestCase):
    """Test schema validation for hook manifests."""

    def setUp(self):
        self.valid_script = b"#!/usr/bin/env bash\necho 'hello'\n"
        self.script_hash = _compute_sha256(self.valid_script)
        self.valid_manifest_dict = {
            "schema_version": 1,
            "name": "audit-tool-guard",
            "description": "Pre-execution guard for dangerous commands",
            "event": "pre-tool-call",
            "runtime": "bash",
            "entrypoint": "guard.sh",
            "files": {
                "guard.sh": self.script_hash,
            },
            "permissions": ["workspace-read"],
            "opt_in": True,
        }

    def test_valid_manifest_acceptance(self):
        manifest = validate_hook_manifest(self.valid_manifest_dict)
        self.assertEqual(manifest.schema_version, 1)
        self.assertEqual(manifest.name, "audit-tool-guard")
        self.assertEqual(manifest.description, "Pre-execution guard for dangerous commands")
        self.assertEqual(manifest.event, "pre-tool-call")
        self.assertEqual(manifest.runtime, "bash")
        self.assertEqual(manifest.entrypoint, "guard.sh")
        self.assertEqual(manifest.files, {"guard.sh": self.script_hash})
        self.assertEqual(manifest.permissions, ("workspace-read",))
        self.assertTrue(manifest.opt_in)

    def test_rejects_non_dict_manifest(self):
        for invalid in ["string", 123, None, [1, 2, 3]]:
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_manifest(invalid)
            self.assertIn("must be a JSON object/dict", str(ctx.exception))

    def test_rejects_missing_required_fields(self):
        for field in ("schema_version", "name", "description", "event", "runtime", "entrypoint", "files", "permissions", "opt_in"):
            bad = dict(self.valid_manifest_dict)
            del bad[field]
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_manifest(bad)
            self.assertIn(f"missing required fields: ['{field}']", str(ctx.exception))

    def test_rejects_unexpected_extra_fields(self):
        bad = dict(self.valid_manifest_dict)
        bad["unexpected_payload"] = "malicious"
        with self.assertRaises(HookValidationError) as ctx:
            validate_hook_manifest(bad)
        self.assertIn("unexpected extra fields", str(ctx.exception))

    def test_rejects_invalid_schema_version(self):
        for bad_version in (0, 2, "1", None, True):
            bad = dict(self.valid_manifest_dict)
            bad["schema_version"] = bad_version
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_manifest(bad)
            self.assertIn("Invalid schema_version", str(ctx.exception))

    def test_rejects_invalid_names(self):
        for bad_name in ("", "-leading", "UPPERCASE", "with spaces", "with_underscore", "name@invalid", "a/b"):
            bad = dict(self.valid_manifest_dict)
            bad["name"] = bad_name
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_manifest(bad)
            self.assertIn("Invalid hook name", str(ctx.exception))

    def test_rejects_empty_or_whitespace_description(self):
        for bad_desc in ("", "   ", "\t\n", None, 123):
            bad = dict(self.valid_manifest_dict)
            bad["description"] = bad_desc
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_manifest(bad)
            self.assertIn("description must be a nonempty string", str(ctx.exception))

    def test_rejects_unsupported_events(self):
        for bad_event in ("on-push", "tool_call", "post-commit", "", None):
            bad = dict(self.valid_manifest_dict)
            bad["event"] = bad_event
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_manifest(bad)
            self.assertIn("Invalid hook event", str(ctx.exception))

    def test_accepts_all_supported_events(self):
        for event in SUPPORTED_EVENTS:
            manifest_dict = dict(self.valid_manifest_dict)
            manifest_dict["event"] = event
            manifest = validate_hook_manifest(manifest_dict)
            self.assertEqual(manifest.event, event)

    def test_rejects_unsupported_runtimes(self):
        for bad_runtime in ("ruby", "node", "powershell", "perl", "", None):
            bad = dict(self.valid_manifest_dict)
            bad["runtime"] = bad_runtime
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_manifest(bad)
            self.assertIn("Invalid hook runtime", str(ctx.exception))

    def test_accepts_all_supported_runtimes(self):
        for runtime in SUPPORTED_RUNTIMES:
            manifest_dict = dict(self.valid_manifest_dict)
            manifest_dict["runtime"] = runtime
            manifest = validate_hook_manifest(manifest_dict)
            self.assertEqual(manifest.runtime, runtime)

    def test_rejects_non_boolean_opt_in(self):
        for bad_opt_in in (1, 0, "true", "false", None, [], {}):
            bad = dict(self.valid_manifest_dict)
            bad["opt_in"] = bad_opt_in
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_manifest(bad)
            self.assertIn("opt_in must be an explicit boolean", str(ctx.exception))

    def test_rejects_invalid_permissions(self):
        for bad_perms in ("workspace-read", None, [""], ["   "], [123]):
            bad = dict(self.valid_manifest_dict)
            bad["permissions"] = bad_perms
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_manifest(bad)
            self.assertIn("permissions", str(ctx.exception))

    def test_accepts_empty_permissions_list(self):
        manifest_dict = dict(self.valid_manifest_dict)
        manifest_dict["permissions"] = []
        manifest = validate_hook_manifest(manifest_dict)
        self.assertEqual(manifest.permissions, ())

    def test_rejects_invalid_files_mapping(self):
        for bad_files in ({}, [], "guard.sh", None):
            bad = dict(self.valid_manifest_dict)
            bad["files"] = bad_files
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_manifest(bad)
            self.assertIn("files must be a nonempty mapping", str(ctx.exception))

    def test_rejects_invalid_sha256_digest(self):
        for bad_hash in ("short", "g" * 64, "123", "", None):
            bad = dict(self.valid_manifest_dict)
            bad["files"] = {"guard.sh": bad_hash}
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_manifest(bad)
            self.assertIn("Invalid SHA-256 digest", str(ctx.exception))

    def test_rejects_entrypoint_not_in_files(self):
        bad = dict(self.valid_manifest_dict)
        bad["entrypoint"] = "other_script.sh"
        with self.assertRaises(HookValidationError) as ctx:
            validate_hook_manifest(bad)
        self.assertIn("Entrypoint 'other_script.sh' is not declared in files mapping", str(ctx.exception))

    def test_rejects_path_traversal_in_entrypoint(self):
        for bad_entrypoint in ("../escape.sh", "sub/../../escape.sh", "/etc/passwd", "guard\\script.sh", "guard\x00.sh", "./guard.sh"):
            bad = dict(self.valid_manifest_dict)
            bad["entrypoint"] = bad_entrypoint
            bad["files"] = {bad_entrypoint: self.script_hash}
            with self.assertRaises(HookValidationError):
                validate_hook_manifest(bad)

    def test_rejects_path_traversal_in_files_keys(self):
        for bad_path in ("../outside.sh", "/bin/sh", "foo//bar.sh", "foo/./bar.sh", "foo\\bar.sh"):
            bad = dict(self.valid_manifest_dict)
            bad["files"] = {"guard.sh": self.script_hash, bad_path: self.script_hash}
            with self.assertRaises(HookValidationError):
                validate_hook_manifest(bad)


class HookPackageValidationTests(unittest.TestCase):
    """Test filesystem validation of hook packages."""

    def _create_synthetic_package(
        self,
        temp_dir: str,
        manifest_filename: str = "hook.json",
        opt_in: bool = True,
        extra_files: dict = None,
    ) -> Path:
        pkg_dir = Path(temp_dir) / "test-hook-pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)

        entry_content = b"#!/usr/bin/env bash\necho 'running hook'\n"
        entry_hash = _compute_sha256(entry_content)
        (pkg_dir / "guard.sh").write_bytes(entry_content)

        files_map = {"guard.sh": entry_hash}

        if extra_files:
            for rel_path, content in extra_files.items():
                target = pkg_dir / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                files_map[rel_path] = _compute_sha256(content)

        manifest = {
            "schema_version": 1,
            "name": "test-guard",
            "description": "Synthetic hook for testing",
            "event": "pre-tool-call",
            "runtime": "bash",
            "entrypoint": "guard.sh",
            "files": files_map,
            "permissions": ["workspace-read"],
            "opt_in": opt_in,
        }
        (pkg_dir / manifest_filename).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return pkg_dir

    def test_valid_package_with_hook_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pkg_dir = self._create_synthetic_package(temp_dir, manifest_filename="hook.json")
            pkg = validate_hook_package(pkg_dir)
            self.assertEqual(pkg.manifest.name, "test-guard")
            self.assertTrue(pkg.is_enabled)
            self.assertEqual(pkg.manifest_path.name, "hook.json")

    def test_valid_package_with_manifest_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pkg_dir = self._create_synthetic_package(temp_dir, manifest_filename="manifest.json")
            pkg = validate_hook_package(pkg_dir)
            self.assertEqual(pkg.manifest.name, "test-guard")
            self.assertTrue(pkg.is_enabled)
            self.assertEqual(pkg.manifest_path.name, "manifest.json")

    def test_valid_package_with_subdirectories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            extra = {"lib/helper.sh": b"helper code\n", "assets/config.json": b'{"key": "val"}'}
            pkg_dir = self._create_synthetic_package(temp_dir, extra_files=extra)
            pkg = validate_hook_package(pkg_dir)
            self.assertIn("lib/helper.sh", pkg.manifest.files)
            self.assertIn("assets/config.json", pkg.manifest.files)

    def test_packaged_vs_enabled_distinction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pkg_dir = self._create_synthetic_package(temp_dir, opt_in=False)
            pkg = validate_hook_package(pkg_dir)
            self.assertFalse(pkg.is_enabled)
            self.assertFalse(pkg.manifest.opt_in)

    def test_rejects_missing_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pkg_dir = Path(temp_dir) / "empty-pkg"
            pkg_dir.mkdir()
            (pkg_dir / "script.sh").write_bytes(b"echo 1")
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_package(pkg_dir)
            self.assertIn("missing manifest", str(ctx.exception))

    def test_rejects_ambiguous_manifests(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pkg_dir = self._create_synthetic_package(temp_dir, manifest_filename="hook.json")
            # Create a second manifest file
            (pkg_dir / "manifest.json").write_text((pkg_dir / "hook.json").read_text(encoding="utf-8"), encoding="utf-8")
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_package(pkg_dir)
            self.assertIn("Ambiguous hook package manifests", str(ctx.exception))

    def test_rejects_nonexistent_or_file_package_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            non_existent = Path(temp_dir) / "non-existent"
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_package(non_existent)
            self.assertIn("does not exist", str(ctx.exception))

            regular_file = Path(temp_dir) / "file.txt"
            regular_file.write_text("hello", encoding="utf-8")
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_package(regular_file)
            self.assertIn("is not a directory", str(ctx.exception))

    def test_rejects_tampered_file_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pkg_dir = self._create_synthetic_package(temp_dir)
            # Tamper with the script
            (pkg_dir / "guard.sh").write_bytes(b"tampered content\n")
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_package(pkg_dir)
            self.assertIn("Content hash mismatch", str(ctx.exception))

    def test_rejects_missing_declared_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pkg_dir = self._create_synthetic_package(temp_dir)
            (pkg_dir / "guard.sh").unlink()
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_package(pkg_dir)
            self.assertIn("Declared file missing from package", str(ctx.exception))

    def test_rejects_unindexed_file_in_package(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pkg_dir = self._create_synthetic_package(temp_dir)
            # Add an unindexed file
            (pkg_dir / "rogue.sh").write_bytes(b"rogue script\n")
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_package(pkg_dir)
            self.assertIn("Unindexed file found in hook package: rogue.sh", str(ctx.exception))

    def test_rejects_symlink_file_in_package(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pkg_dir = self._create_synthetic_package(temp_dir)
            outside_file = Path(temp_dir) / "outside.sh"
            outside_file.write_bytes(b"echo outside")
            # Symlink outside file into package
            (pkg_dir / "link.sh").symlink_to(outside_file)
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_package(pkg_dir)
            self.assertIn("Symlinks are prohibited", str(ctx.exception))

    def test_rejects_symlink_directory_in_package(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pkg_dir = self._create_synthetic_package(temp_dir)
            outside_dir = Path(temp_dir) / "outside_dir"
            outside_dir.mkdir()
            (pkg_dir / "link_dir").symlink_to(outside_dir)
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_package(pkg_dir)
            self.assertIn("Symlinks are prohibited", str(ctx.exception))

    def test_rejects_symlink_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pkg_dir = Path(temp_dir) / "symlink-manifest-pkg"
            pkg_dir.mkdir()
            outside_manifest = Path(temp_dir) / "outside_manifest.json"
            outside_manifest.write_text('{"schema_version": 1}', encoding="utf-8")
            (pkg_dir / "hook.json").symlink_to(outside_manifest)
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_package(pkg_dir)
            self.assertIn("Symlinks are prohibited", str(ctx.exception))

    def test_rejects_symlink_package_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            real_pkg = self._create_synthetic_package(temp_dir)
            symlink_pkg = Path(temp_dir) / "symlink-pkg-dir"
            symlink_pkg.symlink_to(real_pkg)
            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_package(symlink_pkg)
            self.assertIn("Symlinks are prohibited", str(ctx.exception))


class MultiPackageAndSafeDefaultsTests(unittest.TestCase):
    """Test batch validation, safe defaults, and opt-in filtering."""

    def test_safe_defaults_when_none_or_empty(self):
        # Empty inputs must return empty tuple with no active execution path
        self.assertEqual(validate_hook_packages(None), ())
        self.assertEqual(validate_hook_packages([]), ())
        self.assertEqual(filter_enabled_hooks(None), ())
        self.assertEqual(filter_enabled_hooks([]), ())
        self.assertEqual(resolve_active_hooks(None), ())
        self.assertEqual(resolve_active_hooks([]), ())

    def test_filter_enabled_hooks_distinguishes_opt_in(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create two packages: one opted-in, one disabled
            p1_dir = Path(temp_dir) / "pkg-1"
            p1_dir.mkdir()
            s1 = b"echo 1\n"
            (p1_dir / "run.sh").write_bytes(s1)
            (p1_dir / "hook.json").write_text(json.dumps({
                "schema_version": 1,
                "name": "hook-one",
                "description": "First hook",
                "event": "session-start",
                "runtime": "bash",
                "entrypoint": "run.sh",
                "files": {"run.sh": _compute_sha256(s1)},
                "permissions": [],
                "opt_in": True,
            }), encoding="utf-8")

            p2_dir = Path(temp_dir) / "pkg-2"
            p2_dir.mkdir()
            s2 = b"echo 2\n"
            (p2_dir / "run.sh").write_bytes(s2)
            (p2_dir / "hook.json").write_text(json.dumps({
                "schema_version": 1,
                "name": "hook-two",
                "description": "Second hook (disabled)",
                "event": "session-stop",
                "runtime": "bash",
                "entrypoint": "run.sh",
                "files": {"run.sh": _compute_sha256(s2)},
                "permissions": [],
                "opt_in": False,
            }), encoding="utf-8")

            packages = validate_hook_packages([p1_dir, p2_dir])
            self.assertEqual(len(packages), 2)
            self.assertTrue(packages[0].is_enabled)
            self.assertFalse(packages[1].is_enabled)

            enabled = filter_enabled_hooks(packages)
            self.assertEqual(len(enabled), 1)
            self.assertEqual(enabled[0].manifest.name, "hook-one")

            active = resolve_active_hooks([p1_dir, p2_dir])
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0].manifest.name, "hook-one")

    def test_rejects_duplicate_hook_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            p1_dir = Path(temp_dir) / "pkg-a"
            p1_dir.mkdir()
            s1 = b"echo a\n"
            (p1_dir / "run.sh").write_bytes(s1)
            manifest_json = json.dumps({
                "schema_version": 1,
                "name": "duplicate-hook",
                "description": "Duplicate hook test",
                "event": "session-start",
                "runtime": "bash",
                "entrypoint": "run.sh",
                "files": {"run.sh": _compute_sha256(s1)},
                "permissions": [],
                "opt_in": True,
            })
            (p1_dir / "hook.json").write_text(manifest_json, encoding="utf-8")

            p2_dir = Path(temp_dir) / "pkg-b"
            p2_dir.mkdir()
            (p2_dir / "run.sh").write_bytes(s1)
            (p2_dir / "hook.json").write_text(manifest_json, encoding="utf-8")

            with self.assertRaises(HookValidationError) as ctx:
                validate_hook_packages([p1_dir, p2_dir])
            self.assertIn("Duplicate hook name 'duplicate-hook'", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
