import copy
import hashlib
import json
from pathlib import Path
import sys
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

from tests.test_install_github_workflows import (
    CHECKOUT,
    DEPENDENCY,
    WorkflowApi,
    installer,
)


class SkillLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.api = WorkflowApi()
        self.repo = "acme/project"
        self.stage = {"name": "agent-skills", "bundle": "mattpocock"}

    def merge_branch(self, branch):
        """Simulate the owner merging the branch's Setup Pull Request into main."""
        for (ref, path), item in tuple(self.api.files.items()):
            if ref == branch:
                self.api.files[("main", path)] = item
        for (ref, path) in tuple(self.api.deletions):
            if ref == branch:
                self.api.files.pop(("main", path), None)
        self.api.prs.clear()
        self.api.writes.clear()

    def dropped_skill_bundle(self, skill="engineering/implement-spec"):
        """Build a catalog revision that no longer ships one installed skill."""
        baseline_files, old_manifest = installer._agent_skills_bundle()
        prefix = f".agents/skills/{skill}/"
        updated_files = {path: content for path, content in baseline_files.items()
                         if not path.startswith(prefix)}
        updated_manifest = copy.deepcopy(old_manifest)
        for relative in list(updated_manifest["files"]):
            if relative.startswith(f"{skill}/"):
                del updated_manifest["files"][relative]
        updated_manifest["source_commit"] = "c" * 40
        updated_manifest["source_version"] = "2.0.0"
        updated_files[installer.AGENT_SKILLS_MANIFEST] = (
            json.dumps(updated_manifest, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        return updated_files, updated_manifest, skill

    def test_catalog_upgrade_removing_skill_blocks_by_default_with_explicit_decision(self):
        first = installer.install_selected(
            self.api, self.repo, agent_skills_stage=self.stage, apply=True,
        )
        self.merge_branch(first["branch"])

        updated_files, _, dropped_skill = self.dropped_skill_bundle()
        dropped_path = f".agents/skills/{dropped_skill}/SKILL.md"
        self.api.writes.clear()

        with patch.object(installer, "_agent_skills_bundle", return_value=(updated_files, None)):
            with self.assertRaises(ValueError) as ctx:
                installer.install_selected(
                    self.api, self.repo, agent_skills_stage=self.stage, apply=True,
                )

        self.assertIn("upstream catalog revision no longer ships", str(ctx.exception))
        self.assertIn(f"removed: {dropped_skill}", str(ctx.exception))
        self.assertIn("allow_removed_skills", str(ctx.exception))
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.api.prs, [])
        self.assertIn(("main", dropped_path), self.api.files)

    def test_acknowledged_removal_stages_deletions_in_setup_pr(self):
        first = installer.install_selected(
            self.api, self.repo, agent_skills_stage=self.stage, apply=True,
        )
        self.merge_branch(first["branch"])
        _, installed_manifest = installer._agent_skills_bundle()

        updated_files, _, dropped_skill = self.dropped_skill_bundle()
        dropped_path = f".agents/skills/{dropped_skill}/SKILL.md"

        with patch.object(installer, "_agent_skills_bundle", return_value=(updated_files, None)):
            result = installer.install_selected(
                self.api, self.repo,
                agent_skills_stage={
                    "name": "agent-skills", "bundle": "mattpocock",
                    "allow_removed_skills": [dropped_skill],
                },
                apply=True,
            )

        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(len(self.api.prs), 1)

        branch = result["branch"]
        self.assertNotIn((branch, dropped_path), self.api.files)
        self.assertIn((branch, installer.AGENT_SKILLS_MANIFEST), self.api.files)
        self.assertIn(("main", dropped_path), self.api.files)

        removal_changes = [change for change in result["changes"]
                           if change.get("operation") == "remove"]
        self.assertTrue(removal_changes)
        self.assertTrue(all(
            change["path"].startswith(f".agents/skills/{dropped_skill}/")
            for change in removal_changes))

        upgrade = result["agent_skill_upgrade"]
        self.assertEqual(upgrade["removed_skills"], [dropped_skill])
        self.assertEqual(upgrade["renamed_skills"], [])
        self.assertEqual(upgrade["from"]["source_commit"], installed_manifest["source_commit"])
        self.assertEqual(upgrade["from"]["source_version"], installed_manifest["source_version"])
        self.assertEqual(upgrade["to"]["source_commit"], "c" * 40)
        self.assertEqual(upgrade["to"]["source_version"], "2.0.0")

    def test_acknowledged_removal_requires_exact_skill_set(self):
        first = installer.install_selected(
            self.api, self.repo, agent_skills_stage=self.stage, apply=True,
        )
        self.merge_branch(first["branch"])

        updated_files, _, dropped_skill = self.dropped_skill_bundle()
        self.api.writes.clear()

        with patch.object(installer, "_agent_skills_bundle", return_value=(updated_files, None)):
            with self.assertRaises(ValueError) as ctx:
                installer.install_selected(
                    self.api, self.repo,
                    agent_skills_stage={
                        "name": "agent-skills", "bundle": "mattpocock",
                        "allow_removed_skills": ["engineering/other-skill"],
                    },
                    apply=True,
                )

        self.assertIn("exactly", str(ctx.exception))
        self.assertIn(dropped_skill, str(ctx.exception))
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.api.prs, [])

    def test_locally_edited_dropped_skill_blocks_acknowledged_removal(self):
        first = installer.install_selected(
            self.api, self.repo, agent_skills_stage=self.stage, apply=True,
        )
        self.merge_branch(first["branch"])

        updated_files, _, dropped_skill = self.dropped_skill_bundle()
        dropped_path = f".agents/skills/{dropped_skill}/SKILL.md"
        self.api.files[("main", dropped_path)] = (b"# Local edit\n", "f" * 40)
        self.api.writes.clear()

        with patch.object(installer, "_agent_skills_bundle", return_value=(updated_files, None)):
            with self.assertRaises(ValueError) as ctx:
                installer.install_selected(
                    self.api, self.repo,
                    agent_skills_stage={
                        "name": "agent-skills", "bundle": "mattpocock",
                        "allow_removed_skills": [dropped_skill],
                    },
                    apply=True,
                )

        self.assertIn("agent skill conflict: existing file diverged from its manifest", str(ctx.exception))
        self.assertIn(dropped_path, str(ctx.exception))
        self.assertEqual(self.api.files[("main", dropped_path)][0], b"# Local edit\n")
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.api.prs, [])

    def test_upstream_rename_is_classified_and_uses_previous_skill_id(self):
        first = installer.install_selected(
            self.api, self.repo, agent_skills_stage=self.stage, apply=True,
        )
        self.merge_branch(first["branch"])

        baseline_files, old_manifest = installer._agent_skills_bundle()
        old_skill = "engineering/implement-spec"
        new_skill = "engineering/implement-spec-renamed"
        old_prefix = f".agents/skills/{old_skill}/"
        updated_files = {}
        for path, content in baseline_files.items():
            if path.startswith(old_prefix):
                updated_files[path.replace(old_prefix, f".agents/skills/{new_skill}/")] = content
            else:
                updated_files[path] = content
        updated_manifest = copy.deepcopy(old_manifest)
        renamed_files = {}
        for relative, digest in updated_manifest["files"].items():
            if relative.startswith(f"{old_skill}/"):
                renamed_files[relative.replace(f"{old_skill}/", f"{new_skill}/")] = digest
            else:
                renamed_files[relative] = digest
        updated_manifest["files"] = renamed_files
        updated_manifest["source_commit"] = "c" * 40
        updated_manifest["source_version"] = "2.0.0"
        updated_files[installer.AGENT_SKILLS_MANIFEST] = (
            json.dumps(updated_manifest, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

        with patch.object(installer, "_agent_skills_bundle", return_value=(updated_files, None)):
            with self.assertRaises(ValueError) as ctx:
                installer.install_selected(
                    self.api, self.repo, agent_skills_stage=self.stage, apply=True,
                )
        self.assertIn("upstream catalog revision no longer ships", str(ctx.exception))
        self.assertIn(f"renamed: {old_skill} -> {new_skill}", str(ctx.exception))
        self.assertIn("allow_removed_skills", str(ctx.exception))

        with patch.object(installer, "_agent_skills_bundle", return_value=(updated_files, None)):
            result = installer.install_selected(
                self.api, self.repo,
                agent_skills_stage={
                    "name": "agent-skills", "bundle": "mattpocock",
                    "allow_removed_skills": [old_skill],
                },
                apply=True,
            )

        branch = result["branch"]
        self.assertNotIn((branch, f".agents/skills/{old_skill}/SKILL.md"), self.api.files)
        self.assertIn((branch, f".agents/skills/{new_skill}/SKILL.md"), self.api.files)
        self.assertEqual(result["agent_skill_upgrade"]["renamed_skills"],
                         [{"from": old_skill, "to": new_skill}])
        self.assertEqual(result["agent_skill_upgrade"]["removed_skills"], [])

    def test_repeat_upgrade_after_removal_merge_is_verified_noop(self):
        first = installer.install_selected(
            self.api, self.repo, agent_skills_stage=self.stage, apply=True,
        )
        self.merge_branch(first["branch"])

        updated_files, _, dropped_skill = self.dropped_skill_bundle()

        with patch.object(installer, "_agent_skills_bundle", return_value=(updated_files, None)):
            upgrade = installer.install_selected(
                self.api, self.repo,
                agent_skills_stage={
                    "name": "agent-skills", "bundle": "mattpocock",
                    "allow_removed_skills": [dropped_skill],
                },
                apply=True,
            )
        self.merge_branch(upgrade["branch"])
        self.api.writes.clear()

        with patch.object(installer, "_agent_skills_bundle", return_value=(updated_files, None)):
            repeat = installer.install_selected(
                self.api, self.repo,
                agent_skills_stage={
                    "name": "agent-skills", "bundle": "mattpocock",
                    "allow_removed_skills": [dropped_skill],
                },
                apply=True,
            )

        self.assertEqual(repeat["status"], "verified")
        self.assertEqual(repeat["changes"], [])
        self.assertNotIn("branch", repeat)
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.api.prs, [])
        self.assertNotIn(("main", f".agents/skills/{dropped_skill}/SKILL.md"), self.api.files)

    def test_partial_branch_staging_recovers_without_duplicates(self):
        first = installer.install_selected(
            self.api, self.repo, agent_skills_stage=self.stage, apply=True,
        )
        branch = first["branch"]
        self.assertEqual(len(self.api.prs), 1)

        # Simulate a partial prior staging operation: drop some staged skill
        # files from the branch exactly as an interrupted write would leave it
        dropped_from_branch = sorted(
            path for (ref, path) in self.api.files
            if ref == branch and path.startswith(".agents/skills/engineering/")
        )[:3]
        for path in dropped_from_branch:
            del self.api.files[(branch, path)]
        self.api.writes.clear()

        second = installer.install_selected(
            self.api, self.repo, agent_skills_stage=self.stage, apply=True,
        )

        self.assertEqual(second["status"], "pending_review")
        self.assertEqual(second["pull_request"], first["pull_request"])
        self.assertEqual(len(self.api.prs), 1)

        branch_paths = [path for (ref, path) in self.api.files if ref == branch]
        self.assertEqual(len(branch_paths), len(set(branch_paths)))
        for path in dropped_from_branch:
            self.assertIn((branch, path), self.api.files)

    def test_acknowledged_removal_resumes_without_duplicates_or_ghosts(self):
        class PartialRemovalApi(WorkflowApi):
            """The removal DELETE is recorded, but PR creation is interrupted."""

            fail_removal_pull = False

            def request(self, method, path, payload=None):
                if (method == "POST" and path == self.base + "/pulls"
                        and self.fail_removal_pull):
                    self.fail_removal_pull = False
                    raise installer.ApiError("GitHub rejected the pull request")
                return super().request(method, path, payload)

        api = PartialRemovalApi()
        repo = self.repo
        stage = self.stage

        first = installer.install_selected(api, repo, agent_skills_stage=stage, apply=True)
        for (ref, path), item in tuple(api.files.items()):
            if ref == first["branch"]:
                api.files[("main", path)] = item
        api.prs.clear()
        api.writes.clear()

        updated_files, _, dropped_skill = self.dropped_skill_bundle()
        dropped_path = f".agents/skills/{dropped_skill}/SKILL.md"

        api.fail_removal_pull = True
        removal_branch = None
        with patch.object(installer, "_agent_skills_bundle", return_value=(updated_files, None)):
            with self.assertRaises(installer.StagingFailure) as ctx:
                installer.install_selected(
                    api, repo,
                    agent_skills_stage={
                        "name": "agent-skills", "bundle": "mattpocock",
                        "allow_removed_skills": [dropped_skill],
                    },
                    apply=True,
                )
        removal_branch = ctx.exception.branch

        # The removal delete was recorded: the dropped skill file is gone from the
        # removal branch and tracked as a deletion on that branch.
        self.assertNotIn((removal_branch, dropped_path), api.files)
        self.assertTrue(
            any(ref == removal_branch and path == dropped_path
                for (ref, path) in api.deletions))

        # Resume on the same branch: recovers without duplicate file entries and
        # without ghost/deleted entries.
        with patch.object(installer, "_agent_skills_bundle", return_value=(updated_files, None)):
            result = installer.install_selected(
                api, repo,
                agent_skills_stage={
                    "name": "agent-skills", "bundle": "mattpocock",
                    "allow_removed_skills": [dropped_skill],
                },
                apply=True,
            )

        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(result["branch"], removal_branch)
        self.assertEqual(len(api.prs), 1)

        branch_paths = [path for (ref, path) in api.files if ref == removal_branch]
        self.assertEqual(len(branch_paths), len(set(branch_paths)))
        self.assertNotIn(dropped_path, branch_paths)
        self.assertIn((removal_branch, installer.AGENT_SKILLS_MANIFEST), api.files)

    def test_bundle_file_ordering_does_not_make_unchanged_target_look_changed(self):
        first = installer.install_selected(
            self.api, self.repo, agent_skills_stage=self.stage, apply=True,
        )
        self.merge_branch(first["branch"])

        baseline_files, baseline_manifest = installer._agent_skills_bundle()
        reordered_files = dict(reversed(list(baseline_files.items())))
        self.api.writes.clear()

        with patch.object(installer, "_agent_skills_bundle",
                          return_value=(reordered_files, baseline_manifest)):
            result = installer.install_selected(
                self.api, self.repo, agent_skills_stage=self.stage, apply=True,
            )

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["changes"], [])
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.api.prs, [])

    def test_initial_installation_stages_skills_manifest_and_license(self):
        result = installer.install_selected(
            self.api, self.repo, agent_skills_stage=self.stage, apply=True,
        )

        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(len(self.api.prs), 1)
        branch = result["branch"]

        staged_paths = {path for ref, path in self.api.files if ref == branch}
        self.assertIn(".agents/skills/manifest.json", staged_paths)
        self.assertIn(".agents/skills/LICENSE", staged_paths)
        self.assertIn(".agents/skills/engineering/implement-spec/SKILL.md", staged_paths)

        manifest_bytes, _ = self.api.files[(branch, ".agents/skills/manifest.json")]
        manifest_data = installer._parse_agent_skills_manifest(manifest_bytes)
        self.assertIsNotNone(manifest_data)
        self.assertEqual(manifest_data["schema_version"], 1)
        self.assertEqual(manifest_data["managed_by"], "sciflow-cicd")
        self.assertEqual(manifest_data["bundle"], "mattpocock")
        self.assertEqual(manifest_data["source_repository"], "https://github.com/mattpocock/skills")
        self.assertEqual(manifest_data["license"], "MIT")
        self.assertIn("LICENSE", manifest_data["files"])
        self.assertIn("engineering/implement-spec/SKILL.md", manifest_data["files"])
        self.assertEqual(len(manifest_data["files"]), 80)

        license_bytes, _ = self.api.files[(branch, ".agents/skills/LICENSE")]
        self.assertIn(b"MIT License", license_bytes)

    def test_repeat_installation_idempotent_after_merge(self):
        first = installer.install_selected(
            self.api, self.repo, agent_skills_stage=self.stage, apply=True,
        )
        first_branch = first["branch"]

        # Simulate owner merging the Setup Pull Request into main
        for (ref, path), item in tuple(self.api.files.items()):
            if ref == first_branch:
                self.api.files[("main", path)] = item
        self.api.prs.clear()
        self.api.writes.clear()

        # Repeat installation with identical catalog / configuration
        second = installer.install_selected(
            self.api, self.repo, agent_skills_stage=self.stage, apply=True,
        )

        self.assertEqual(second["status"], "verified")
        self.assertEqual(second["changes"], [])
        self.assertNotIn("branch", second)
        self.assertNotIn("pull_request", second)
        self.assertEqual(len(self.api.prs), 0)
        self.assertEqual(len(self.api.writes), 0)

    def test_repeat_installation_idempotent_when_pr_pending_review(self):
        first = installer.install_selected(
            self.api, self.repo, agent_skills_stage=self.stage, apply=True,
        )
        self.assertEqual(len(self.api.prs), 1)
        self.api.writes.clear()

        # Re-running before PR merge must detect the open draft Setup PR and create no new commits/PRs
        second = installer.install_selected(
            self.api, self.repo, agent_skills_stage=self.stage, apply=True,
        )

        self.assertEqual(second["status"], "pending_review")
        self.assertEqual(second["changes"], [])
        self.assertEqual(second["pull_request"], first["pull_request"])
        self.assertEqual(len(self.api.prs), 1)
        self.assertEqual(len(self.api.writes), 0)

    def test_catalog_upgrade_cleanly_upgrades_matching_skills(self):
        first = installer.install_selected(
            self.api, self.repo, agent_skills_stage=self.stage, apply=True,
        )
        first_branch = first["branch"]

        # Simulate PR merge into main
        for (ref, path), item in tuple(self.api.files.items()):
            if ref == first_branch:
                self.api.files[("main", path)] = item
        self.api.prs.clear()

        selected_path = ".agents/skills/engineering/implement-spec/SKILL.md"
        baseline_files, old_manifest = installer._agent_skills_bundle()
        updated_files = dict(baseline_files)
        updated_files[selected_path] = baseline_files[selected_path] + b"\nUpdated upstream guidance.\n"

        updated_manifest = copy.deepcopy(old_manifest)
        updated_manifest["source_commit"] = "c" * 40
        updated_manifest["source_version"] = "2.0.0"
        updated_manifest["files"][selected_path.removeprefix(".agents/skills/")] = hashlib.sha256(
            updated_files[selected_path]
        ).hexdigest()
        updated_files[installer.AGENT_SKILLS_MANIFEST] = (
            json.dumps(updated_manifest, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

        with patch.object(installer, "_agent_skills_bundle", return_value=(updated_files, updated_manifest)):
            upgrade_result = installer.install_selected(
                self.api, self.repo, agent_skills_stage=self.stage, apply=True,
            )

        self.assertEqual(upgrade_result["status"], "pending_review")
        self.assertEqual(len(self.api.prs), 1)
        changed_files = {path for (ref, path) in self.api.files if ref == upgrade_result["branch"]}
        self.assertEqual(changed_files, {selected_path, installer.AGENT_SKILLS_MANIFEST})
        self.assertEqual(self.api.files[(upgrade_result["branch"], selected_path)][0], updated_files[selected_path])

    def test_local_target_edit_conflict_stops_bundle_before_any_write(self):
        first = installer.install_selected(
            self.api, self.repo, agent_skills_stage=self.stage, apply=True,
        )
        first_branch = first["branch"]
        for (ref, path), item in tuple(self.api.files.items()):
            if ref == first_branch:
                self.api.files[("main", path)] = item
        self.api.prs.clear()

        # Local edit on target skill file
        conflict_path = ".agents/skills/engineering/implement-spec/SKILL.md"
        self.api.files[("main", conflict_path)] = (b"# Local diverged edit\n", "f" * 40)
        self.api.writes.clear()

        with self.assertRaises(ValueError) as ctx:
            installer.install_selected(
                self.api, self.repo, agent_skills_stage=self.stage, apply=True,
            )

        self.assertIn("agent skill conflict: existing file diverged from its manifest", str(ctx.exception))
        self.assertIn(conflict_path, str(ctx.exception))
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.api.prs, [])

    def test_local_target_deletion_conflict_stops_bundle_before_any_write(self):
        first = installer.install_selected(
            self.api, self.repo, agent_skills_stage=self.stage, apply=True,
        )
        first_branch = first["branch"]
        for (ref, path), item in tuple(self.api.files.items()):
            if ref == first_branch:
                self.api.files[("main", path)] = item
        self.api.prs.clear()

        # Target skill deleted from main branch while manifest remains
        conflict_path = ".agents/skills/engineering/implement-spec/SKILL.md"
        del self.api.files[("main", conflict_path)]
        self.api.writes.clear()

        with self.assertRaises(ValueError) as ctx:
            installer.install_selected(
                self.api, self.repo, agent_skills_stage=self.stage, apply=True,
            )

        self.assertIn("agent skill conflict: existing file diverged from its manifest", str(ctx.exception))
        self.assertIn(conflict_path, str(ctx.exception))
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.api.prs, [])

    def test_unmanaged_target_skill_file_stops_bundle_before_any_write(self):
        conflict_path = ".agents/skills/engineering/implement-spec/SKILL.md"
        self.api.files[("main", conflict_path)] = (b"# Preexisting unmanaged file\n", "e" * 40)
        self.api.writes.clear()

        with self.assertRaises(ValueError) as ctx:
            installer.install_selected(
                self.api, self.repo, agent_skills_stage=self.stage, apply=True,
            )

        self.assertIn("agent skill conflict", str(ctx.exception))
        self.assertIn(conflict_path, str(ctx.exception))
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.api.prs, [])

    def test_skill_plus_workflow_staging_bundles_all_in_one_setup_pr(self):
        result = installer.install_selected(
            self.api,
            self.repo,
            workflow_stage={
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
            },
            issue_state_gate_stage={"name": "issue-state-gate"},
            agent_skills_stage=self.stage,
            apply=True,
        )

        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(len(self.api.prs), 1)

        branch = result["branch"]
        staged_paths = {path for ref, path in self.api.files if ref == branch}

        # Workflows
        self.assertIn(".github/workflows/ci.yml", staged_paths)
        self.assertIn(".github/workflows/dependency-review.yml", staged_paths)
        self.assertIn(".github/workflows/issue-state-gate.yml", staged_paths)
        self.assertIn(".github/workflows/issue-state-gate-refresh.yml", staged_paths)
        self.assertIn("scripts/issue_state_gate.py", staged_paths)
        self.assertIn("scripts/refresh_issue_state_gate.py", staged_paths)

        # Agent skills
        self.assertIn(".agents/skills/manifest.json", staged_paths)
        self.assertIn(".agents/skills/LICENSE", staged_paths)
        self.assertIn(".agents/skills/engineering/implement-spec/SKILL.md", staged_paths)

        # Documentation freshness suite
        self.assertIn(".github/workflows/documentation-freshness.yml", staged_paths)
        self.assertIn("docs/documentation-freshness.md", staged_paths)

    def test_local_skill_conflict_with_workflows_stops_entire_bundle_without_writes(self):
        # Target repo has diverged skill file
        conflict_path = ".agents/skills/engineering/implement-spec/SKILL.md"
        self.api.files[("main", conflict_path)] = (b"# Diverged skill\n", "d" * 40)
        self.api.writes.clear()

        with self.assertRaises(ValueError) as ctx:
            installer.install_selected(
                self.api,
                self.repo,
                workflow_stage={
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
                },
                issue_state_gate_stage={"name": "issue-state-gate"},
                agent_skills_stage=self.stage,
                apply=True,
            )

        self.assertIn("agent skill conflict: existing file diverged from its manifest", str(ctx.exception))
        self.assertIn(conflict_path, str(ctx.exception))

        # Absolute zero writes and zero PRs
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.api.prs, [])


if __name__ == "__main__":
    unittest.main()
