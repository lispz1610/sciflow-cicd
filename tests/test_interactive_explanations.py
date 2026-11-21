"""User-visible explanations in the Interactive Setup transcript."""

import io
import unittest

from scripts.interactive import Session, prompt_target_repository, format_inventory_summary
from scripts.interactive_features import prompt_features_stage
from scripts.interactive_actions_security import prompt_actions_permissions, prompt_security_features
from scripts.interactive_workflows_rulesets import prompt_workflows_stage, prompt_rulesets_stage
from scripts.interactive_repository_content import prompt_merge_stage, prompt_content_stages
from scripts.interactive_apply import format_dry_run_summary, format_apply_summary


class InteractiveExplanationTests(unittest.TestCase):
    def test_target_and_inventory_explain_scope_and_visibility(self):
        output = io.StringIO()
        repository = prompt_target_repository(io.StringIO("acme/project\n"), output)
        summary = format_inventory_summary({
            "repository": {"full_name": "acme/project", "visibility": "private", "default_branch": "main"},
            "features": {"issues": True},
        })

        self.assertEqual(repository, "acme/project")
        self.assertIn("Inventory only reads", output.getvalue())
        self.assertIn("repository permissions", output.getvalue())
        self.assertLess(output.getvalue().index("Inventory only reads"), output.getvalue().index("Target repository"))
        self.assertIn("acme/project", summary)
        self.assertIn("private", summary)
        self.assertIn("main", summary)

    def test_features_explain_current_value_consequence_and_default_before_choice(self):
        output = io.StringIO()
        session = Session(
            "acme/project",
            api=object(),
            inventory={"features": {"issues": True, "wiki": False, "projects": False}},
            defaults={"features": ["issues"]},
            resume_mode=True,
            in_stream=io.StringIO("\n"),
            out_stream=output,
        )

        stage = prompt_features_stage(session)

        self.assertEqual(stage, {"name": "features", "enable": ["issues"]})
        transcript = output.getvalue()
        self.assertIn("Issues: enabled", transcript)
        self.assertIn("Wiki: disabled", transcript)
        self.assertIn("Projects: disabled", transcript)
        self.assertIn("Recommended default: Issues", transcript)
        self.assertIn("Unselected features are left unchanged", transcript)
        self.assertLess(transcript.index("Recommended default"), transcript.index("Select repository features"))

    def test_actions_and_security_explain_current_settings_and_effects(self):
        output = io.StringIO()
        session = Session(
            "acme/project",
            api=object(),
            inventory={
                "actions": {"enabled": True, "allowed_actions": "selected"},
                "workflow_permissions": {"default_workflow_permissions": "write"},
                "vulnerability_alerts": {"status": 204},
                "security_and_analysis": {"secret_scanning": {"status": "disabled"}},
                "codeql": {"state": "not-configured"},
            },
            in_stream=io.StringIO("n\nnone\n"),
            out_stream=output,
        )

        self.assertFalse(prompt_actions_permissions(session))
        self.assertEqual(prompt_security_features(session), [])
        transcript = output.getvalue()
        for expected in (
            "Current Actions: enabled",
            "allowed actions: selected",
            "default token permissions: write",
            "Recommended default: Yes",
            "Vulnerability alerts: enabled",
            "Secret scanning: disabled",
            "CodeQL default setup: not configured",
            "plan or organization policy",
        ):
            self.assertIn(expected, transcript)
        self.assertLess(transcript.index("Current Actions"), transcript.index("Configure default GitHub Actions"))

    def test_workflows_and_gates_explain_pr_and_evidence_before_choice(self):
        output = io.StringIO()
        session = Session(
            "acme/project",
            api=object(),
            inventory={"rulesets": [], "actions": {"enabled": True}},
            in_stream=io.StringIO("n\nn\n"),
            out_stream=output,
        )

        self.assertIsNone(prompt_workflows_stage(session))
        self.assertIsNone(prompt_rulesets_stage(session))
        transcript = output.getvalue()
        for expected in (
            "Standard PR CI",
            "Dependency review",
            "Pinned Action",
            "Setup Pull Request",
            "Current rulesets: none",
            "evidence PR",
            "reviewer",
            "successful CI check",
            "Recommended default: No",
        ):
            self.assertIn(expected, transcript)
        self.assertLess(transcript.index("Setup Pull Request"), transcript.index("Stage standard CI"))

    def test_merge_and_content_explain_current_state_and_skip_by_default(self):
        output = io.StringIO()
        session = Session(
            "acme/project",
            api=object(),
            inventory={"merge": {
                "allow_merge_commit": False, "allow_squash_merge": True,
                "allow_rebase_merge": False, "delete_branch_on_merge": False,
                "allow_update_branch": True,
            }},
            in_stream=io.StringIO("n\nn\n"),
            out_stream=output,
        )

        self.assertIsNone(prompt_merge_stage(session))
        self.assertEqual(prompt_content_stages(session), [])
        transcript = output.getvalue()
        for expected in (
            "Merge commits: disabled",
            "Squash merge: enabled",
            "Rebase merge: disabled",
            "Delete branch after merge: disabled",
            "Update pull request branch: enabled",
            "at least one merge method",
            "Labels organize Issues and pull requests",
            "Authored files",
            "current blob SHA",
            "Recommended default: No",
        ):
            self.assertIn(expected, transcript)

    def test_merge_selection_preserves_one_method_and_changes_only_selected_values(self):
        output = io.StringIO()
        session = Session(
            "acme/project", api=object(),
            inventory={"merge": {
                "allow_merge_commit": True, "allow_squash_merge": False,
                "allow_rebase_merge": False, "delete_branch_on_merge": False,
                "allow_update_branch": False,
            }},
            in_stream=io.StringIO("y\ny\ny\nn\nn\nn\n"),
            out_stream=output,
        )

        stage = prompt_merge_stage(session)

        self.assertEqual(stage, {"name": "merge", "settings": {"allow_squash_merge": True}})
        self.assertIn("enabling it combines the pull request into one commit", output.getvalue())

    def test_merge_prompt_warns_when_all_methods_would_be_disabled(self):
        output = io.StringIO()
        session = Session(
            "acme/project", api=object(),
            inventory={"merge": {
                "allow_merge_commit": True, "allow_squash_merge": False,
                "allow_rebase_merge": False, "delete_branch_on_merge": False,
                "allow_update_branch": False,
            }},
            in_stream=io.StringIO("y\nn\nn\nn\nn\nn\n"),
            out_stream=output,
        )

        self.assertIsNone(prompt_merge_stage(session))
        self.assertIn("at least one merge method must stay enabled", output.getvalue())

    def test_existing_authored_file_requires_visible_sha_acknowledgement(self):
        class FileApi:
            def request(self, method, path):
                self_path = "/repos/acme/project/contents/docs/setup.md?ref=main"
                if (method, path) != ("GET", self_path):
                    raise AssertionError((method, path))
                return 200, {"sha": "abc123"}

        output = io.StringIO()
        session = Session(
            "acme/project", api=FileApi(),
            inventory={"repository": {"default_branch": "main"}},
            in_stream=io.StringIO("y\n2\ndocs/setup.md\n/tmp/setup.md\ny\nn\n"),
            out_stream=output,
        )

        stages = prompt_content_stages(session)

        self.assertEqual(stages, [{"name": "files", "items": [{
            "path": "docs/setup.md", "source": "/tmp/setup.md", "expected_sha": "abc123",
        }]}])
        transcript = output.getvalue()
        self.assertIn("current blob SHA abc123", transcript)
        self.assertLess(transcript.index("current blob SHA abc123"), transcript.index("Update this existing file?"))

    def test_existing_label_shows_current_values_before_change(self):
        class LabelApi:
            def request(self, method, path):
                if (method, path) != ("GET", "/repos/acme/project/labels/bug"):
                    raise AssertionError((method, path))
                return 200, {"color": "f00000", "description": "Old description"}

        output = io.StringIO()
        session = Session(
            "acme/project", api=LabelApi(),
            in_stream=io.StringIO("y\n1\nbug\n00ff00\nNew description\nn\n"),
            out_stream=output,
        )

        stages = prompt_content_stages(session)

        self.assertEqual(stages, [{"name": "labels", "items": [{
            "name": "bug", "color": "00ff00", "description": "New description",
        }]}])
        self.assertIn("Current label bug: color f00000; description Old description", output.getvalue())

    def test_dry_run_and_result_distinguish_writes_pr_warnings_and_failures(self):
        planned = format_dry_run_summary({
            "repository": "acme/project",
            "changes": [
                {"stage": "features", "operation": "PATCH", "path": "/repos/acme/project", "status": "planned"},
                {"stage": "workflows", "operation": "PUT", "path": ".github/workflows/ci.yml", "status": "planned"},
                {"stage": "gates", "status": "pending", "reason": "CI evidence is needed"},
                {"stage": "security", "status": "blocked", "reason": "organization policy"},
            ],
            "warnings": ["Actions retention exceeds the requested limit"],
        })
        self.assertIn("Direct repository settings", planned)
        self.assertIn("one draft Setup Pull Request", planned)
        self.assertIn("Warnings", planned)
        self.assertIn("Blocked", planned)
        self.assertIn("Pending gates", planned)
        self.assertIn("inspect and merge", planned)

        applied = format_apply_summary({
            "changes": [
                {"status": "verified", "stage": "features"},
                {"status": "pending", "stage": "gates"},
                {"status": "blocked", "stage": "security"},
                {"status": "failed", "stage": "workflows"},
            ],
            "setup_pull_request": "https://github.com/acme/project/pull/7",
            "setup_branch": "sciflow-cicd/workflows",
            "recovery": "Inspect the partial setup branch before retrying.",
        })
        self.assertIn("Verified: 1", applied)
        self.assertIn("Pending: 1", applied)
        self.assertIn("Blocked: 1", applied)
        self.assertIn("Failed: 1", applied)
        self.assertIn("https://github.com/acme/project/pull/7", applied)
        self.assertIn("sciflow-cicd/workflows", applied)
        self.assertIn("Inspect the partial setup branch before retrying.", applied)

        reused = format_dry_run_summary({
            "repository": "acme/project", "changes": [],
            "setup_pull_request": "https://github.com/acme/project/pull/7",
        })
        self.assertIn("https://github.com/acme/project/pull/7", reused)

    def test_compact_contextual_banners_at_most_two_lines(self):
        """Standardized interactive guidance banners must not exceed 2 lines preceding prompts."""
        from scripts.interactive import format_profile_banner, format_catalog_banner
        from scripts.setup_profiles import list_profiles

        for profile in list_profiles():
            rec_banner = format_profile_banner(profile, is_recommended=True)
            lines_rec = [l for l in rec_banner.splitlines() if l.strip()]
            self.assertLessEqual(len(lines_rec), 2, f"Recommended banner for {profile.id} exceeded 2 lines")

            act_banner = format_profile_banner(profile, is_recommended=False)
            lines_act = [l for l in act_banner.splitlines() if l.strip()]
            self.assertLessEqual(len(lines_act), 2, f"Active banner for {profile.id} exceeded 2 lines")

        catalog_banner = format_catalog_banner()
        lines_cat = [l for l in catalog_banner.splitlines() if l.strip()]
        self.assertLessEqual(len(lines_cat), 2, "Catalog banner exceeded 2 lines")


if __name__ == "__main__":
    unittest.main()
