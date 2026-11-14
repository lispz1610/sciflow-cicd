"""Unit tests for opt-in hook package staging in the Setup Pull Request (Ticket #165)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
TESTS_DIR = ROOT_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_install_github_workflows import (
    CHECKOUT,
    DEPENDENCY,
    WorkflowApi,
    installer,
)
from scripts.hook_contracts import (
    HookPackage,
    HookValidationError,
    validate_hook_package,
    validate_hook_packages,
)
from scripts.install_github_workflows import (
    install_selected,
    render_hook_packages,
    stage_workflows,
)
from scripts.setup_github_repo import run_policy, validate_policy


def _compute_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest().lower()


def create_hook_package_fixture(
    base_dir: Path,
    name: str = "audit-guard",
    description: str = "Audit tool execution guard",
    event: str = "pre-tool-call",
    runtime: str = "bash",
    entrypoint: str = "guard.sh",
    script_bytes: bytes = b"#!/usr/bin/env bash\necho 'audit-guard running'\n",
    extra_files: dict[str, bytes] | None = None,
    opt_in: bool = True,
    corrupt_hash: bool = False,
    symlink_file: bool = False,
    traversal_entrypoint: str | None = None,
    manifest_name: str = "manifest.json",
    unindexed_file: bool = False,
    permissions: list[str] | None = None,
) -> Path:
    pkg_dir = base_dir / name
    pkg_dir.mkdir(parents=True, exist_ok=True)

    files_map: dict[str, str] = {}

    entrypoint_path = pkg_dir / entrypoint
    entrypoint_path.parent.mkdir(parents=True, exist_ok=True)
    entrypoint_path.write_bytes(script_bytes)
    files_map[entrypoint] = "0" * 64 if corrupt_hash else _compute_sha256(script_bytes)

    if extra_files:
        for rel_path, data in extra_files.items():
            fpath = pkg_dir / rel_path
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_bytes(data)
            files_map[rel_path] = _compute_sha256(data)

    if unindexed_file:
        extra = pkg_dir / "unindexed.txt"
        extra.write_bytes(b"I am not in manifest.json\n")

    if symlink_file:
        target = pkg_dir / "target.sh"
        target.write_bytes(b"echo 'target'\n")
        link = pkg_dir / "link.sh"
        if link.exists():
            link.unlink()
        os.symlink(target, link)
        files_map["link.sh"] = _compute_sha256(b"echo 'target'\n")

    manifest_data = {
        "schema_version": 1,
        "name": name,
        "description": description,
        "event": event,
        "runtime": runtime,
        "entrypoint": traversal_entrypoint or entrypoint,
        "files": files_map,
        "permissions": permissions or ["workspace-read"],
        "opt_in": opt_in,
    }

    manifest_file = pkg_dir / manifest_name
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
    return pkg_dir


class OptInHookStagingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.addCleanup(self.temp_dir.cleanup)

    def test_optin_valid_hooks_stages_in_setup_pr(self):
        """Opt-in with valid hook packages successfully stages them in the Setup Pull Request."""
        pkg_dir = create_hook_package_fixture(self.root, name="audit-guard")
        rendered = render_hook_packages([pkg_dir], opt_in=True)

        self.assertIn(".agents/hooks/audit-guard/manifest.json", rendered)
        self.assertIn(".agents/hooks/audit-guard/guard.sh", rendered)

        api = WorkflowApi()
        files = {path: (content, None) for path, content in rendered.items()}
        result = stage_workflows(api, "acme/project", files, apply=True, hook_packages=[pkg_dir])

        self.assertTrue(result["pull_request"].startswith("https://github.com/acme/project/pull/"))
        self.assertTrue(result["branch"].startswith("codex/workflows-"))
        branch = result["branch"]

        self.assertEqual(len(api.prs), 1)
        created_pr = api.prs[0]
        self.assertTrue(created_pr["draft"])
        self.assertEqual(created_pr["head"]["ref"], branch)

        self.assertIn((branch, ".agents/hooks/audit-guard/manifest.json"), api.files)
        self.assertIn((branch, ".agents/hooks/audit-guard/guard.sh"), api.files)

        staged_manifest = json.loads(api.files[(branch, ".agents/hooks/audit-guard/manifest.json")][0].decode("utf-8"))
        self.assertEqual(staged_manifest["name"], "audit-guard")
        self.assertTrue(staged_manifest["opt_in"])

    def test_install_selected_with_hooks_stage(self):
        """install_selected integrates hooks_stage into the unified Setup PR."""
        pkg_dir = create_hook_package_fixture(self.root, name="sec-guard")
        api = WorkflowApi()

        hooks_stage = {
            "name": "hooks",
            "opt_in": True,
            "packages": [str(pkg_dir)],
        }
        result = install_selected(api, "acme/project", hooks_stage=hooks_stage, apply=True)

        self.assertTrue(result["pull_request"].startswith("https://github.com/acme/project/pull/"))
        branch = result["branch"]
        self.assertIn((branch, ".agents/hooks/sec-guard/manifest.json"), api.files)
        self.assertIn((branch, ".agents/hooks/sec-guard/guard.sh"), api.files)

    def test_run_policy_v2_with_hooks_stage_lifecycle(self):
        """Policy v2 with hooks stage creates draft PR and plans expected changes."""
        pkg_dir = create_hook_package_fixture(self.root, name="policy-hook")
        api = WorkflowApi()

        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {
                    "name": "hooks",
                    "opt_in": True,
                    "packages": [str(pkg_dir)],
                }
            ],
        }

        # Dry run
        dry_run = run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(dry_run["mode"], "dry-run")
        hook_changes = [c for c in dry_run["changes"] if c.get("stage") == "hooks"]
        self.assertEqual(len(hook_changes), 2)
        paths = {c["path"] for c in hook_changes}
        self.assertEqual(paths, {".agents/hooks/policy-hook/manifest.json", ".agents/hooks/policy-hook/guard.sh"})
        self.assertTrue(all(c["status"] == "planned" for c in hook_changes))

        # Apply
        applied = run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(applied["mode"], "apply")
        self.assertTrue(applied["setup_pull_request"].startswith("https://github.com/acme/project/pull/"))
        self.assertTrue(len(api.writes) > 0)

    def test_opt_out_stages_zero_hook_files(self):
        """When opt_in is False, zero hook files are staged and no hooks are enabled."""
        pkg_dir = create_hook_package_fixture(self.root, name="optout-guard")

        # render_hook_packages with opt_in=False
        rendered = render_hook_packages([pkg_dir], opt_in=False)
        self.assertEqual(rendered, {})

        # install_selected with opt_in=False
        api = WorkflowApi()
        hooks_stage = {
            "name": "hooks",
            "opt_in": False,
            "packages": [str(pkg_dir)],
        }
        result = install_selected(api, "acme/project", hooks_stage=hooks_stage, apply=True)
        self.assertEqual(result.get("changes"), [])
        self.assertNotIn("pull_request", result)
        self.assertEqual(api.writes, [])

    def test_no_hooks_supplied_stages_zero_files(self):
        """When no hooks are supplied (None or empty), zero files are staged."""
        self.assertEqual(render_hook_packages(None, opt_in=True), {})
        self.assertEqual(render_hook_packages([], opt_in=True), {})

        api = WorkflowApi()
        hooks_stage = {
            "name": "hooks",
            "opt_in": True,
            "packages": [],
        }
        result = install_selected(api, "acme/project", hooks_stage=hooks_stage, apply=True)
        self.assertEqual(result.get("changes"), [])
        self.assertEqual(api.writes, [])

    def test_dormant_hook_package_with_opt_in_false(self):
        """Hook package declaring opt_in=False in its manifest stays absent/dormant."""
        pkg_dir = create_hook_package_fixture(self.root, name="dormant-hook", opt_in=False)

        # Package is valid on disk
        pkg = validate_hook_package(pkg_dir)
        self.assertFalse(pkg.is_enabled)

        # render_hook_packages omits dormant hook
        rendered = render_hook_packages([pkg_dir], opt_in=True)
        self.assertEqual(rendered, {})

    def test_invalid_hook_bad_hash_fails_before_staging_without_writes(self):
        """Supplied hook with content hash mismatch fails before staging with zero writes."""
        pkg_dir = create_hook_package_fixture(self.root, name="bad-hash-hook", corrupt_hash=True)
        api = WorkflowApi()

        with self.assertRaises(HookValidationError):
            render_hook_packages([pkg_dir], opt_in=True)

        with self.assertRaises(HookValidationError):
            install_selected(
                api,
                "acme/project",
                hooks_stage={"name": "hooks", "opt_in": True, "packages": [str(pkg_dir)]},
                apply=True,
            )
        self.assertEqual(api.writes, [], "Invalid hook package must abort before any write")

    def test_invalid_hook_symlink_fails_before_staging_without_writes(self):
        """Supplied hook with symlink fails before staging with zero writes."""
        pkg_dir = create_hook_package_fixture(self.root, name="symlink-hook", symlink_file=True)
        api = WorkflowApi()

        with self.assertRaises(HookValidationError):
            render_hook_packages([pkg_dir], opt_in=True)

        with self.assertRaises(HookValidationError):
            install_selected(
                api,
                "acme/project",
                hooks_stage={"name": "hooks", "opt_in": True, "packages": [str(pkg_dir)]},
                apply=True,
            )
        self.assertEqual(api.writes, [], "Symlink hook package must abort before any write")

    def test_invalid_hook_traversal_fails_before_staging_without_writes(self):
        """Supplied hook with path traversal in entrypoint fails before staging with zero writes."""
        pkg_dir = create_hook_package_fixture(
            self.root, name="traversal-hook", traversal_entrypoint="../outside.sh"
        )
        api = WorkflowApi()

        with self.assertRaises(HookValidationError):
            render_hook_packages([pkg_dir], opt_in=True)

        with self.assertRaises(HookValidationError):
            install_selected(
                api,
                "acme/project",
                hooks_stage={"name": "hooks", "opt_in": True, "packages": [str(pkg_dir)]},
                apply=True,
            )
        self.assertEqual(api.writes, [], "Traversal hook package must abort before any write")

    def test_invalid_hook_event_and_runtime_fails_before_staging_without_writes(self):
        """Supplied hook with unsupported event or runtime fails before staging with zero writes."""
        # Unsupported event
        pkg_bad_event = create_hook_package_fixture(self.root, name="bad-event", event="post-commit")
        with self.assertRaises(HookValidationError):
            render_hook_packages([pkg_bad_event], opt_in=True)

        # Unsupported runtime
        pkg_bad_runtime = create_hook_package_fixture(self.root, name="bad-runtime", runtime="ruby")
        with self.assertRaises(HookValidationError):
            render_hook_packages([pkg_bad_runtime], opt_in=True)

    def test_invalid_hook_unindexed_file_fails_before_staging_without_writes(self):
        """Supplied hook with unindexed file in package directory fails before staging."""
        pkg_dir = create_hook_package_fixture(self.root, name="unindexed-hook", unindexed_file=True)
        api = WorkflowApi()

        with self.assertRaises(HookValidationError):
            render_hook_packages([pkg_dir], opt_in=True)

        with self.assertRaises(HookValidationError):
            install_selected(
                api,
                "acme/project",
                hooks_stage={"name": "hooks", "opt_in": True, "packages": [str(pkg_dir)]},
                apply=True,
            )
        self.assertEqual(api.writes, [], "Unindexed hook package must abort before any write")

    def test_target_conflict_local_edits_block_staging(self):
        """Target conflict protection: local edits in .agents/hooks/ block staging before writes."""
        # Seed main branch with valid prior hook package
        api = WorkflowApi()
        prior_script = b"#!/usr/bin/env bash\necho 'version 1'\n"
        prior_hash = _compute_sha256(prior_script)
        prior_manifest = {
            "schema_version": 1,
            "name": "conflict-guard",
            "description": "Conflict guard",
            "event": "pre-tool-call",
            "runtime": "bash",
            "entrypoint": "guard.sh",
            "files": {"guard.sh": prior_hash},
            "permissions": ["workspace-read"],
            "opt_in": True,
        }
        api.files[("main", ".agents/hooks/conflict-guard/manifest.json")] = (
            json.dumps(prior_manifest).encode("utf-8"),
            "sha_manifest_1",
        )
        # Owner modified guard.sh locally on main (diverging from manifest)
        tampered_script = b"#!/usr/bin/env bash\necho 'locally modified by owner'\n"
        api.files[("main", ".agents/hooks/conflict-guard/guard.sh")] = (tampered_script, "sha_guard_1")

        # Now attempt to stage a new package version
        new_script = b"#!/usr/bin/env bash\necho 'version 2'\n"
        pkg_dir = create_hook_package_fixture(
            self.root, name="conflict-guard", script_bytes=new_script
        )

        with self.assertRaisesRegex(ValueError, "hook conflict: existing file diverged from its manifest"):
            install_selected(
                api,
                "acme/project",
                hooks_stage={"name": "hooks", "opt_in": True, "packages": [str(pkg_dir)]},
                apply=True,
            )
        self.assertEqual(api.writes, [], "Local modification conflict must abort before any write")

    def test_target_conflict_clean_upgrade_succeeds(self):
        """Clean upgrade: when existing files on main match prior manifest, staging succeeds."""
        api = WorkflowApi()
        v1_script = b"#!/usr/bin/env bash\necho 'v1'\n"
        v1_hash = _compute_sha256(v1_script)
        v1_manifest = {
            "schema_version": 1,
            "name": "upgrade-guard",
            "description": "Upgrade guard",
            "event": "pre-tool-call",
            "runtime": "bash",
            "entrypoint": "guard.sh",
            "files": {"guard.sh": v1_hash},
            "permissions": ["workspace-read"],
            "opt_in": True,
        }
        api.files[("main", ".agents/hooks/upgrade-guard/manifest.json")] = (
            json.dumps(v1_manifest).encode("utf-8"),
            "sha_m_1",
        )
        api.files[("main", ".agents/hooks/upgrade-guard/guard.sh")] = (v1_script, "sha_g_1")

        # Now stage v2
        v2_script = b"#!/usr/bin/env bash\necho 'v2 upgrade'\n"
        pkg_dir = create_hook_package_fixture(
            self.root, name="upgrade-guard", script_bytes=v2_script
        )

        result = install_selected(
            api,
            "acme/project",
            hooks_stage={"name": "hooks", "opt_in": True, "packages": [str(pkg_dir)]},
            apply=True,
        )
        self.assertTrue(result["pull_request"].startswith("https://github.com/acme/project/pull/"))
        branch = result["branch"]
        self.assertEqual(api.files[(branch, ".agents/hooks/upgrade-guard/guard.sh")][0], v2_script)

    def test_target_conflict_unmanaged_file_blocks_staging(self):
        """Unmanaged file existing in target hook directory blocks staging before writes."""
        api = WorkflowApi()
        # guard.sh exists without any manifest
        api.files[("main", ".agents/hooks/unmanaged-guard/guard.sh")] = (b"unmanaged content", "sha_unmanaged")

        pkg_dir = create_hook_package_fixture(self.root, name="unmanaged-guard")

        with self.assertRaisesRegex(ValueError, "hook conflict: unmanaged file exists in target hook directory"):
            install_selected(
                api,
                "acme/project",
                hooks_stage={"name": "hooks", "opt_in": True, "packages": [str(pkg_dir)]},
                apply=True,
            )
        self.assertEqual(api.writes, [], "Unmanaged target file must abort before any write")

    def test_safe_defaults_default_policy_contains_no_hooks(self):
        """Safe defaults: default policy contains no hooks stage, staging zero hook files."""
        # Policy with only standard stages
        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {"name": "features", "enable": ["issues"]},
            ],
        }
        stages = validate_policy(policy)
        self.assertIsNone(next((s for s in stages if s["name"] == "hooks"), None))

        api = WorkflowApi()
        result = run_policy(api, "acme/project", policy, apply=False)
        self.assertFalse(any(c.get("stage") == "hooks" for c in result.get("changes", [])))

    def test_hooks_stage_requires_policy_v2(self):
        """Hooks stage in Policy v1 is rejected."""
        policy_v1 = {
            "version": 1,
            "repository": "acme/project",
            "stages": [
                {"name": "hooks", "opt_in": True, "packages": []},
            ],
        }
        with self.assertRaisesRegex(ValueError, "hooks stage requires Policy version 2"):
            validate_policy(policy_v1)

    def test_invalid_hooks_fail_even_when_opt_in_false(self):
        """Invalid hooks fail validation even if caller sets opt_in=False."""
        bad_pkg = create_hook_package_fixture(self.root, name="bad-hash-optout", corrupt_hash=True)
        with self.assertRaises(HookValidationError):
            render_hook_packages([bad_pkg], opt_in=False)

    def test_ambiguous_manifests_fail_before_staging(self):
        """Package having both hook.json and manifest.json fails before staging."""
        pkg_dir = create_hook_package_fixture(self.root, name="ambiguous-hook", manifest_name="manifest.json")
        hook_json = pkg_dir / "hook.json"
        hook_json.write_text((pkg_dir / "manifest.json").read_text(encoding="utf-8"), encoding="utf-8")

        api = WorkflowApi()
        with self.assertRaises(HookValidationError):
            render_hook_packages([pkg_dir], opt_in=True)
        with self.assertRaises(HookValidationError):
            install_selected(api, "acme/project", hooks_stage={"name": "hooks", "opt_in": True, "packages": [str(pkg_dir)]}, apply=True)
        self.assertEqual(api.writes, [])

    def test_multiple_valid_hook_packages_staged_together(self):
        """Multiple distinct valid hook packages are rendered and staged together."""
        pkg1 = create_hook_package_fixture(self.root, name="hook-one")
        pkg2 = create_hook_package_fixture(self.root, name="hook-two")

        rendered = render_hook_packages([pkg1, pkg2], opt_in=True)
        self.assertIn(".agents/hooks/hook-one/manifest.json", rendered)
        self.assertIn(".agents/hooks/hook-one/guard.sh", rendered)
        self.assertIn(".agents/hooks/hook-two/manifest.json", rendered)
        self.assertIn(".agents/hooks/hook-two/guard.sh", rendered)

        api = WorkflowApi()
        res = install_selected(
            api,
            "acme/project",
            hooks_stage={"name": "hooks", "opt_in": True, "packages": [str(pkg1), str(pkg2)]},
            apply=True,
        )
        self.assertTrue(res["pull_request"].startswith("https://github.com/acme/project/pull/"))
        branch = res["branch"]
        self.assertIn((branch, ".agents/hooks/hook-one/guard.sh"), api.files)
        self.assertIn((branch, ".agents/hooks/hook-two/guard.sh"), api.files)

    def test_duplicate_hook_names_fail_before_staging(self):
        """Supplying duplicate hook names across package directories fails before staging."""
        dir1 = self.root / "copy1"
        dir2 = self.root / "copy2"
        create_hook_package_fixture(dir1, name="duplicate-hook")
        create_hook_package_fixture(dir2, name="duplicate-hook")

        with self.assertRaises(HookValidationError):
            render_hook_packages([dir1 / "duplicate-hook", dir2 / "duplicate-hook"], opt_in=True)

    def test_exact_reuse_of_open_matching_hook_pr_issues_zero_writes(self):
        """Re-staging with identical valid hook package reuses the open PR with zero writes."""
        pkg_dir = create_hook_package_fixture(self.root, name="idempotent-hook")
        api = WorkflowApi()

        first = install_selected(
            api,
            "acme/project",
            hooks_stage={"name": "hooks", "opt_in": True, "packages": [str(pkg_dir)]},
            apply=True,
        )
        self.assertTrue(len(api.writes) > 0)
        api.writes.clear()

        second = install_selected(
            api,
            "acme/project",
            hooks_stage={"name": "hooks", "opt_in": True, "packages": [str(pkg_dir)]},
            apply=True,
        )
        self.assertEqual(second["pull_request"], first["pull_request"])
        self.assertEqual(second["branch"], first["branch"])
        self.assertEqual(api.writes, [], "Reusing matching open Setup PR must perform zero writes")

    def test_invalid_hook_in_dry_run_fails_with_validator_error_without_writes(self):
        """Invalid supplied hook causes run_policy dry-run to fail with validator error with zero writes."""
        bad_pkg = create_hook_package_fixture(self.root, name="corrupt-hash-pkg", corrupt_hash=True)
        api = WorkflowApi()

        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {
                    "name": "hooks",
                    "opt_in": True,
                    "packages": [str(bad_pkg)],
                }
            ],
        }

        with self.assertRaises(HookValidationError) as ctx:
            run_policy(api, "acme/project", policy, apply=False)

        self.assertIn("Content hash mismatch for guard.sh", str(ctx.exception))
        self.assertEqual(api.writes, [], "Zero writes must occur on validation failure")

    def test_double_opt_in_manifest_veto_produces_dry_run_warning_and_stages_zero_files(self):
        """When hooks stage opts in but package manifest has opt_in=False, manifest acts as veto and emits warning."""
        dormant_pkg = create_hook_package_fixture(self.root, name="vetoed-pkg", opt_in=False)
        api = WorkflowApi()

        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {
                    "name": "hooks",
                    "opt_in": True,
                    "packages": [str(dormant_pkg)],
                }
            ],
        }

        dry_run = run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(dry_run["mode"], "dry-run")
        # Zero hook files staged
        hook_changes = [c for c in dry_run.get("changes", []) if c.get("stage") == "hooks"]
        self.assertEqual(hook_changes, [])

        # Warning emitted naming the ignored package
        warnings = dry_run.get("warnings", [])
        self.assertTrue(any("vetoed-pkg" in w and "declares opt_in=false" in w for w in warnings),
                        f"Expected veto warning in warnings: {warnings}")

    def test_double_opt_in_both_true_stages_hooks_without_veto_warning(self):
        """When both hooks stage and package manifest have opt_in=True, files are planned without veto warning."""
        active_pkg = create_hook_package_fixture(self.root, name="active-pkg", opt_in=True)
        api = WorkflowApi()

        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {
                    "name": "hooks",
                    "opt_in": True,
                    "packages": [str(active_pkg)],
                }
            ],
        }

        dry_run = run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(dry_run["mode"], "dry-run")
        hook_changes = [c for c in dry_run.get("changes", []) if c.get("stage") == "hooks"]
        self.assertEqual(len(hook_changes), 2)
        paths = {c["path"] for c in hook_changes}
        self.assertEqual(paths, {".agents/hooks/active-pkg/manifest.json", ".agents/hooks/active-pkg/guard.sh"})

        # No veto warning in warnings
        warnings = dry_run.get("warnings", [])
        self.assertFalse(any("active-pkg" in w and "declares opt_in=false" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
