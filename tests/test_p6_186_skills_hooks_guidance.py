"""Tests for Ticket P6.8 (#186): Verify skill catalog and opt-in hook guidance."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

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
    validate_removal_acknowledgment,
)
from scripts.agent_skills_compatibility import audit_agent_skills_compatibility
from scripts.agent_skills_readiness import evaluate_agent_skills_readiness
from scripts.check_markdown_links import audit as audit_links
from scripts.documentation_freshness import audit_repository
from scripts.hook_contracts import (
    HookValidationError,
    MANIFEST_REQUIRED_FIELDS,
    SUPPORTED_EVENTS,
    SUPPORTED_RUNTIMES,
    filter_enabled_hooks,
    resolve_active_hooks,
    validate_hook_manifest,
    validate_hook_package,
    validate_hook_packages,
)
from scripts.install_github_workflows import (
    AGENT_SKILLS_LICENSE,
    AGENT_SKILLS_MANIFEST,
    DOCUMENTATION_CONFIG,
    DOCUMENTATION_GUIDE,
    DOCUMENTATION_INVENTORY,
    DOCUMENTATION_SCRIPT_FILES,
    DOCUMENTATION_WORKFLOW,
    install_selected,
    render_documentation_suite,
    render_hook_packages,
)
from scripts.provision_agent_skills import (
    load_verified_catalog,
    parse_agent_skills_manifest,
    provision_skills,
)
from scripts.setup_github_repo import run_policy
from scripts.verify_agent_skill_discovery import (
    discover_workspace_skills,
    verify_disposable_skill_discovery,
)
from tests.test_install_github_workflows import CHECKOUT, DEPENDENCY, WorkflowApi
from tests.test_optin_hook_staging import create_hook_package_fixture

ROOT_DIR = Path(__file__).resolve().parents[1]
README_PATH = ROOT_DIR / "README.md"
AGENT_SKILLS_DOC_PATH = ROOT_DIR / "docs" / "agent-skills.md"
CATALOG_DIR = ROOT_DIR / "data" / "agent-skills" / "mattpocock"
CATALOG_MANIFEST_PATH = CATALOG_DIR / "manifest.json"
CATALOG_SNAPSHOT_DIR = CATALOG_DIR / "snapshot"
CATALOG_LICENSE_PATH = CATALOG_DIR / "LICENSE"


class P6186SkillsAndHooksGuidanceTests(unittest.TestCase):
    """Acceptance tests for P6.8 (#186) documentation and disposable-target verification."""

    def test_ac1_docs_and_readme_explain_pinned_catalog_and_opt_in_hook_contract(self):
        """AC1: docs/agent-skills.md and README.md explain the pinned catalog and opt-in hooks."""
        readme = README_PATH.read_text(encoding="utf-8")
        doc = AGENT_SKILLS_DOC_PATH.read_text(encoding="utf-8")

        # README.md checks
        self.assertIn("docs/agent-skills.md", readme)
        self.assertIn("27", readme)
        self.assertIn("49dd158d1076134a641b33efb035946536778336", readme)
        self.assertIn(".agents/skills/", readme)
        self.assertIn("verify_agent_skill_discovery.py", readme)
        self.assertIn("Opt-In Hook Contract", readme)
        self.assertIn("disabled by default", readme)
        self.assertIn("opt_in: true", readme)

        # docs/agent-skills.md checks for AC1-AC5
        for required_token in (
            "https://github.com/mattpocock/skills",
            "49dd158d1076134a641b33efb035946536778336",
            "1.3.1",
            "MIT",
            "27",
            "20",
            "7",
            "79",
            "80",
            "81",
            ".agents/skills/manifest.json",
            ".agents/skills/LICENSE",
            ".github/workflows/documentation-freshness.yml",
            ".github/documentation-freshness.json",
            ".github/documentation-skill-inventory.json",
            "Opt-In Hook Contract",
            "Hook Package",
            "scripts/hook_contracts.py",
            "validate_hook_packages(None)",
            "resolve_active_hooks(None)",
            "HookValidationError",
            "opt_in: true",
            "opt_in: false",
            "classify_catalog_upgrade",
            "allow_removed_skills",
            "allow_removals",
            "destination has local changes",
            "codex/workflows-",
            "verify_agent_skill_discovery.py",
            "### Observed",
            "### Inferred",
            "### Unverified",
        ):
            self.assertIn(
                required_token,
                doc,
                f"docs/agent-skills.md must document '{required_token}' for Ticket #186",
            )

    def test_ac2_disposable_lifecycle_initial_repeat_upgrade_conflict_and_combined_staging(self):
        """AC2: Exercise disposable-target initial install, repeat noop, upgrade/removal, conflict, and combined PR."""
        with tempfile.TemporaryDirectory() as tmp:
            target_root = Path(tmp) / "disposable-target"
            # 1. Initial local installation into disposable target
            staged_paths = provision_skills(
                CATALOG_SNAPSHOT_DIR,
                CATALOG_MANIFEST_PATH,
                target_root,
                license_path=CATALOG_LICENSE_PATH,
                target_manifest=True,
            )
            self.assertEqual(len(staged_paths), 81)
            self.assertIn(".agents/skills/LICENSE", staged_paths)
            self.assertIn(".agents/skills/manifest.json", staged_paths)

            installed_manifest = parse_agent_skills_manifest(
                (target_root / ".agents/skills/manifest.json").read_bytes()
            )
            self.assertIsNotNone(installed_manifest)
            self.assertEqual(len(installed_manifest["files"]), 80)

            # 2. Repeat local installation convergence
            repeat_paths = provision_skills(
                CATALOG_SNAPSHOT_DIR,
                CATALOG_MANIFEST_PATH,
                target_root,
                license_path=CATALOG_LICENSE_PATH,
                target_manifest=True,
            )
            self.assertEqual(staged_paths, repeat_paths)

            # 3. Local-edit conflict detection preserves owner edit and reports SHA-256 digests
            edited_rel = "engineering/tdd/SKILL.md"
            edited_file = target_root / ".agents/skills" / edited_rel
            original_bytes = edited_file.read_bytes()
            edited_bytes = original_bytes + b"\n<!-- local maintainer customization -->\n"
            edited_file.write_bytes(edited_bytes)

            with self.assertRaises(ValueError) as conflict_ctx:
                provision_skills(
                    CATALOG_SNAPSHOT_DIR,
                    CATALOG_MANIFEST_PATH,
                    target_root,
                    license_path=CATALOG_LICENSE_PATH,
                    target_manifest=True,
                )
            err_msg = str(conflict_ctx.exception)
            self.assertIn("destination has local changes", err_msg)
            self.assertIn("installed sha256 " + hashlib.sha256(original_bytes).hexdigest(), err_msg)
            self.assertIn("local sha256 " + hashlib.sha256(edited_bytes).hexdigest(), err_msg)
            self.assertEqual(edited_file.read_bytes(), edited_bytes)

            # Restore clean file before testing catalog upgrade & removal acknowledgment
            edited_file.write_bytes(original_bytes)

        # 4. Remote Setup Pull Request staging: combined workflows + agent-skills + documentation-freshness
        api = WorkflowApi()
        repo = "acme/project"
        workflow_stage = {
            "name": "workflows",
            "workflows": [
                {
                    "template": "pr-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT,
                    "commands": ["python3 -m unittest discover -s tests"],
                },
                {
                    "template": "dependency-review",
                    "template_version": 1,
                    "filename": "dependency-review.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT,
                    "action_sha": DEPENDENCY,
                },
            ],
        }
        skills_stage = {"name": "agent-skills", "bundle": "mattpocock"}

        initial_pr = install_selected(
            api,
            repo,
            workflow_stage=workflow_stage,
            agent_skills_stage=skills_stage,
            apply=True,
        )
        self.assertEqual(initial_pr["status"], "pending_review")
        self.assertTrue(initial_pr["branch"].startswith("codex/workflows-"))
        self.assertEqual(len(api.prs), 1)
        branch = initial_pr["branch"]
        staged_on_branch = {path for (ref, path) in api.files if ref == branch}
        self.assertIn(".github/workflows/ci.yml", staged_on_branch)
        self.assertIn(".github/workflows/dependency-review.yml", staged_on_branch)
        self.assertIn(AGENT_SKILLS_MANIFEST, staged_on_branch)
        self.assertIn(AGENT_SKILLS_LICENSE, staged_on_branch)
        self.assertIn(DOCUMENTATION_WORKFLOW, staged_on_branch)
        self.assertIn(DOCUMENTATION_CONFIG, staged_on_branch)
        self.assertIn(DOCUMENTATION_INVENTORY, staged_on_branch)
        self.assertIn(DOCUMENTATION_GUIDE, staged_on_branch)
        for script_name in DOCUMENTATION_SCRIPT_FILES:
            self.assertIn(f"scripts/{script_name}", staged_on_branch)

        # Simulate owner merge of Setup PR into main, then repeat apply -> 0 writes, verified noop
        for (ref, path), item in tuple(api.files.items()):
            if ref == branch:
                api.files[("main", path)] = item
        api.prs.clear()
        api.writes.clear()

        repeat_pr = install_selected(
            api,
            repo,
            workflow_stage=workflow_stage,
            agent_skills_stage=skills_stage,
            apply=True,
        )
        self.assertEqual(repeat_pr["status"], "verified")
        self.assertEqual(repeat_pr["changes"], [])
        self.assertEqual(api.writes, [])
        self.assertEqual(api.prs, [])

        # 5. Catalog upgrade & removal acknowledgment classification
        baseline_files, old_manifest = load_verified_catalog(CATALOG_DIR)
        dropped_skill = "engineering/wizard"
        renamed_from = "productivity/wait-what"
        renamed_to = "productivity/wait-what-v2"

        new_manifest = copy.deepcopy(old_manifest)
        new_manifest["source_commit"] = "b" * 40
        new_manifest["source_version"] = "1.4.0"
        new_files_map = {}
        for rel_path, digest in old_manifest["files"].items():
            if rel_path.startswith(f"{dropped_skill}/"):
                continue
            if rel_path.startswith(f"{renamed_from}/"):
                new_files_map[rel_path.replace(f"{renamed_from}/", f"{renamed_to}/", 1)] = digest
            else:
                new_files_map[rel_path] = digest
        new_manifest["files"] = new_files_map

        classification = classify_catalog_upgrade(old_manifest, new_manifest)
        self.assertEqual(classification["removed_skills"], [dropped_skill])
        self.assertEqual(
            classification["renamed_skills"],
            [{"from": renamed_from, "to": renamed_to}],
        )
        self.assertTrue(classification["revision_changed"])

        dropped_all = [dropped_skill, renamed_from]
        with self.assertRaisesRegex(ValueError, "explicit removal decision"):
            validate_removal_acknowledgment(dropped_all, None, classification=classification)
        with self.assertRaisesRegex(ValueError, "exactly"):
            validate_removal_acknowledgment(dropped_all, [dropped_skill], classification=classification)
        validate_removal_acknowledgment(
            dropped_all, [dropped_skill, renamed_from], classification=classification
        )

    def test_ac3_skill_discovery_verifier_and_workspace_parity(self):
        """AC3: Verify disposable skill discovery (27/27 skills) and evidence report sections."""
        report = verify_disposable_skill_discovery()
        self.assertTrue(report.is_disposable)
        self.assertTrue(report.is_clean)
        self.assertEqual(len(report.skills), 27)
        self.assertTrue(report.manifest_match)
        self.assertEqual(report.manifest_missing, [])
        self.assertEqual(report.manifest_unexpected, [])
        self.assertEqual(report.errors, [])

        engineering_skills = [s for s in report.skills if s.skill_id.startswith("engineering/")]
        productivity_skills = [s for s in report.skills if s.skill_id.startswith("productivity/")]
        self.assertEqual(len(engineering_skills), 20)
        self.assertEqual(len(productivity_skills), 7)

        evidence_report = report.format_evidence_report()
        self.assertIn("### Observed", evidence_report)
        self.assertIn("### Inferred", evidence_report)
        self.assertIn("### Unverified", evidence_report)
        self.assertIn("Discovered 27 skill entrypoint(s)", evidence_report)
        self.assertIn("Catalog manifest parity: Verified", evidence_report)

        # Also verify discovery against the checked-in snapshot skills directory
        _, snapshot_skills, snapshot_errors = discover_workspace_skills(CATALOG_SNAPSHOT_DIR)
        self.assertEqual(snapshot_errors, [])
        self.assertEqual(len(snapshot_skills), 27)

    def test_ac4_opt_in_hook_contract_safe_defaults_veto_and_validation_failures(self):
        """AC4: Absent hooks remain disabled; opt_in=false vetoes with warning; invalid hooks fail before write."""
        # 1. Absent hooks remain absent and disabled
        self.assertEqual(validate_hook_packages(None), ())
        self.assertEqual(validate_hook_packages([]), ())
        self.assertEqual(filter_enabled_hooks(None), ())
        self.assertEqual(resolve_active_hooks(None), ())
        self.assertEqual(render_hook_packages(None, opt_in=True), {})
        self.assertEqual(
            MANIFEST_REQUIRED_FIELDS,
            frozenset({
                "schema_version",
                "name",
                "description",
                "event",
                "runtime",
                "entrypoint",
                "files",
                "permissions",
                "opt_in",
            }),
        )
        self.assertEqual(
            SUPPORTED_EVENTS,
            frozenset({"pre-tool-call", "post-tool-call", "session-start", "session-stop"}),
        )
        self.assertEqual(SUPPORTED_RUNTIMES, frozenset({"bash", "python3"}))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api = WorkflowApi()

            # 2. Double opt-in veto: Policy opt_in=True but hook manifest opt_in=False -> 0 staged files + warning
            dormant_pkg = create_hook_package_fixture(root, name="dormant-guard", opt_in=False)
            veto_policy = {
                "version": 2,
                "repository": "acme/project",
                "stages": [
                    {"name": "hooks", "opt_in": True, "packages": [str(dormant_pkg)]},
                ],
            }
            dry_run = run_policy(api, "acme/project", veto_policy, apply=False)
            self.assertEqual([c for c in dry_run["changes"] if c.get("stage") == "hooks"], [])
            self.assertTrue(
                any("dormant-guard" in w and "declares opt_in=false" in w for w in dry_run.get("warnings", []))
            )

            # 3. Invalid hooks fail validation (HookValidationError) before any file is staged
            # 3a. Missing required field
            with self.assertRaises(HookValidationError):
                validate_hook_manifest({"schema_version": 1, "name": "incomplete"})

            # 3b. Symlink inside package
            symlink_pkg = create_hook_package_fixture(root, name="symlink-pkg", symlink_file=True)
            with self.assertRaises(HookValidationError):
                validate_hook_package(symlink_pkg)

            # 3c. Path traversal in entrypoint
            traversal_pkg = create_hook_package_fixture(
                root, name="traversal-pkg", traversal_entrypoint="../escape.sh"
            )
            with self.assertRaises(HookValidationError):
                validate_hook_package(traversal_pkg)

            # 3d. SHA-256 content mismatch
            bad_hash_pkg = create_hook_package_fixture(root, name="bad-hash-pkg", corrupt_hash=True)
            with self.assertRaises(HookValidationError):
                 run_policy(
                    api,
                    "acme/project",
                    {
                        "version": 2,
                        "repository": "acme/project",
                        "stages": [{"name": "hooks", "opt_in": True, "packages": [str(bad_hash_pkg)]}],
                    },
                    apply=False,
                )
            self.assertEqual(api.writes, [])

            # 3e. Unindexed file
            unindexed_pkg = create_hook_package_fixture(root, name="unindexed-pkg", unindexed_file=True)
            with self.assertRaises(HookValidationError):
                validate_hook_package(unindexed_pkg)

            # 3f. Ambiguous hook.json + manifest.json
            ambiguous_pkg = create_hook_package_fixture(
                root, name="ambiguous-pkg", manifest_name="manifest.json"
            )
            (ambiguous_pkg / "hook.json").write_text(
                (ambiguous_pkg / "manifest.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with self.assertRaises(HookValidationError):
                validate_hook_package(ambiguous_pkg)

            # 3g. Duplicate hook names across package sequence
            dup_a = create_hook_package_fixture(root / "a", name="dup-hook")
            dup_b = create_hook_package_fixture(root / "b", name="dup-hook")
            with self.assertRaises(HookValidationError):
                validate_hook_packages([dup_a, dup_b])

    def test_ac5_source_revision_and_attribution_remain_inspectable(self):
        """AC5: Confirm catalog source revision, version, license, and file digests remain inspectable."""
        raw_catalog = json.loads(CATALOG_MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(raw_catalog["schema_version"], 1)
        self.assertEqual(raw_catalog["source_repository"], "https://github.com/mattpocock/skills")
        self.assertEqual(raw_catalog["source_commit"], "49dd158d1076134a641b33efb035946536778336")
        self.assertEqual(raw_catalog["source_version"], "1.3.1")
        self.assertEqual(raw_catalog["license"], "MIT")
        self.assertEqual(len(raw_catalog["supported_skills"]), 27)
        self.assertEqual(len(raw_catalog["files"]), 79)

        staged_files, target_manifest = load_verified_catalog(CATALOG_DIR)
        self.assertEqual(len(staged_files), 81)
        self.assertEqual(target_manifest["schema_version"], 1)
        self.assertEqual(target_manifest["managed_by"], "github-repo-setup")
        self.assertEqual(target_manifest["bundle"], "mattpocock")
        self.assertEqual(target_manifest["source_repository"], "https://github.com/mattpocock/skills")
        self.assertEqual(target_manifest["source_commit"], "49dd158d1076134a641b33efb035946536778336")
        self.assertEqual(target_manifest["source_version"], "1.3.1")
        self.assertEqual(target_manifest["license"], "MIT")
        self.assertEqual(len(target_manifest["files"]), 80)
        self.assertIn("LICENSE", target_manifest["files"])
        self.assertIn(b"MIT License", staged_files[AGENT_SKILLS_LICENSE])
        self.assertIn(b"Copyright (c) 2026 Matt Pocock", staged_files[AGENT_SKILLS_LICENSE])

        # Verify readiness and compatibility evaluation seams
        readiness = evaluate_agent_skills_readiness(
            lambda rel: (ROOT_DIR / rel).read_bytes() if (ROOT_DIR / rel).is_file() else None
        )
        self.assertTrue(readiness["ready"])
        compatibility = audit_agent_skills_compatibility(
            staged_files,
            available_tools={"gh", "git"},
            present_paths={
                "docs/agents/issue-tracker.md",
                "docs/agents/triage-labels.md",
                "docs/agents/domain.md",
                "GLOSSARY.md",
                "docs/adr",
            },
        )
        self.assertEqual(compatibility["counts"]["incompatible"], 0)
        self.assertEqual(len(compatibility["skills"]), 27)

        # Verify zero link or freshness errors across repository
        self.assertEqual(audit_links(ROOT_DIR), [])
        self.assertEqual(audit_repository(ROOT_DIR), [])


if __name__ == "__main__":
    unittest.main()
