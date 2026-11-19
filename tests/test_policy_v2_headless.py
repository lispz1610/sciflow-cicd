"""Headless Policy v2 behavior at the sciflow-cicd apply seam."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.headless import run_headless_apply
from scripts.review_policy import validate_policy as validate_review
from scripts.review_workflow import WORKFLOW_PATH, render_workflow
from scripts.setup_github_repo import run_policy
from test_install_github_workflows import CHECKOUT, WorkflowApi
from test_review_setup import review_stage
from test_setup_github_repo import InventoryApi


class ReviewApi(WorkflowApi):
    def __init__(self):
        super().__init__()
        self.repo.update({"visibility": "private", "owner": {"type": "User"}})

    def request(self, method, path, payload=None):
        if method == "GET" and path == "/repos/alibaba/open-code-review/git/commits/182898cf522da3d04157b422752d028417974e19":
            return 200, {"sha": "182898cf522da3d04157b422752d028417974e19"}
        if method == "GET" and path == self.base + "/actions/permissions":
            return 200, {"enabled": True, "allowed_actions": "all"}
        if method == "GET" and path == self.base + "/actions/permissions/artifact-and-log-retention":
            return 200, {"days": 30}
        if method == "GET" and path.startswith(self.base + "/actions/secrets/"):
            return 404, {}
        if method == "GET" and path.startswith(self.base + "/actions/variables/"):
            return 404, {}
        return super().request(method, path, payload)


class PolicyV2HeadlessTests(unittest.TestCase):
    def apply(self, api, policy):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            output = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), redirect_stdout(output):
                preview = io.StringIO()
                with redirect_stdout(preview):
                    run_headless_apply(str(path), token="mock-token", apply=False)
                plan_path = Path(directory) / "dry-run.json"
                plan_path.write_text(preview.getvalue(), encoding="utf-8")
                exit_code = run_headless_apply(str(path), token="mock-token",
                                               plan_path=str(plan_path))
        return exit_code, json.loads(output.getvalue())

    def test_missing_credentials_leave_successful_staged_setup_pending_configuration(self):
        api = ReviewApi()
        policy = {"version": 2, "repository": "acme/project", "stages": [review_stage()]}
        exit_code, outcome = self.apply(api, policy)
        self.assertEqual(exit_code, 0)
        self.assertEqual(outcome["review_status"], "pending_configuration")
        self.assertEqual(len(api.prs), 1)
        self.assertIn("OCR_LLM_AUTH_TOKEN", "\n".join(outcome["warnings"]))
        self.assertNotIn("mock-token", json.dumps(outcome))
        staged_wf = [c for (ref, path), (c, s) in api.files.items() if path == WORKFLOW_PATH][0].decode("utf-8")
        self.assertIn("runs-on: self-hosted", staged_wf)

    def test_headless_review_stage_runner_options(self):
        # Staging with custom runner labels
        api = ReviewApi()
        stage_custom = review_stage()
        stage_custom["workflow"]["runs_on"] = ["self-hosted", "gpu"]
        policy_custom = {"version": 2, "repository": "acme/project", "stages": [stage_custom]}
        exit_code, outcome = self.apply(api, policy_custom)
        self.assertEqual(exit_code, 0)
        staged_wf = [c for (ref, path), (c, s) in api.files.items() if path == WORKFLOW_PATH][0].decode("utf-8")
        self.assertIn("runs-on:\n      - self-hosted\n      - gpu", staged_wf)

        # Staging with hosted runner ubuntu-24.04
        api_hosted = ReviewApi()
        stage_hosted = review_stage()
        stage_hosted["workflow"]["runs_on"] = "ubuntu-24.04"
        policy_hosted = {"version": 2, "repository": "acme/project", "stages": [stage_hosted]}
        exit_code_h, outcome_h = self.apply(api_hosted, policy_hosted)
        self.assertEqual(exit_code_h, 0)
        staged_hosted_wf = [c for (ref, path), (c, s) in api_hosted.files.items() if path == WORKFLOW_PATH][0].decode("utf-8")
        self.assertIn("runs-on: ubuntu-24.04", staged_hosted_wf)

    def test_repeating_same_policy_performs_no_new_writes(self):
        api = ReviewApi()
        policy = {"version": 2, "repository": "acme/project", "stages": [review_stage()]}
        self.assertEqual(self.apply(api, policy)[0], 0)
        first_writes = list(api.writes)
        second_exit, second = self.apply(api, policy)
        self.assertEqual(second_exit, 0)
        self.assertEqual(api.writes, first_writes)
        self.assertEqual(second["review_status"], "pending_configuration")

    def test_dry_run_reads_inventory_without_writing(self):
        api = ReviewApi()
        policy = {"version": 2, "stages": [review_stage()]}
        result = run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(result["changes"][0]["status"], "planned")
        self.assertEqual(api.writes, [])

    def test_changed_managed_review_requires_current_blob_sha(self):
        api = ReviewApi()
        current = render_workflow(validate_review(review_stage())) + b"# Owner approved legacy content\n"
        api.files[("main", WORKFLOW_PATH)] = (current, "b" * 40)
        policy = {"version": 2, "repository": "acme/project", "stages": [review_stage()]}
        exit_code, result = self.apply(api, policy)
        self.assertEqual(exit_code, 2)
        self.assertIn("expected_sha", result["changes"][0]["reason"])
        self.assertEqual(api.writes, [])

        policy["stages"][0]["workflow"]["expected_sha"] = "b" * 40
        exit_code, result = self.apply(api, policy)
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(api.prs), 1)
        self.assertEqual(result["review_status"], "pending_configuration")

    def test_unmanaged_review_is_blocked_even_with_matching_sha(self):
        api = ReviewApi()
        api.files[("main", WORKFLOW_PATH)] = (b"name: unowned\n", "b" * 40)
        stage = review_stage()
        stage["workflow"]["expected_sha"] = "b" * 40
        exit_code, result = self.apply(api, {"version": 2, "repository": "acme/project", "stages": [stage]})
        self.assertEqual(exit_code, 2)
        self.assertIn("unmanaged workflow", result["changes"][0]["reason"])
        self.assertEqual(api.writes, [])

    def test_strict_policy_schema_rejects_secret_before_any_api_call(self):
        api = ReviewApi()
        stage = review_stage()
        stage["llm"]["provider_key"] = "never-print-this"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps({"version": 2, "repository": "acme/project", "stages": [stage]}), encoding="utf-8")
            stderr = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), redirect_stderr(stderr):
                exit_code = run_headless_apply(str(path), token="mock-token")
        self.assertEqual(exit_code, 1)
        self.assertIn("Invalid policy schema", stderr.getvalue())
        self.assertNotIn("never-print-this", stderr.getvalue())
        self.assertEqual(api.writes, [])

    def test_version_two_direct_stage_is_valid_for_public_repository(self):
        api = InventoryApi()
        exit_code, outcome = self.apply(api, {"version": 2, "repository": "acme/project", "stages": [
            {"name": "features", "enable": ["issues"]}]})
        self.assertEqual(exit_code, 0)
        self.assertEqual(outcome["changes"][0]["status"], "verified")
        self.assertTrue(api.repo["has_issues"])

    def test_agent_skills_stage_is_opt_in_and_policy_v2_only(self):
        from scripts.setup_github_repo import validate_policy

        stage = {"name": "agent-skills", "bundle": "mattpocock"}
        self.assertEqual(validate_policy({"version": 2, "stages": [stage]}), [stage])
        with self.assertRaisesRegex(ValueError, "Policy version 2"):
            validate_policy({"version": 1, "stages": [stage]})
        with self.assertRaisesRegex(ValueError, "bundle must be mattpocock"):
            validate_policy({"version": 2, "stages": [{"name": "agent-skills", "bundle": "other"}]})

    def test_agent_skills_and_selected_workflow_share_one_pr_and_repeat_converges(self):
        api = WorkflowApi()
        policy = {"version": 2, "repository": "acme/project", "stages": [
            {"name": "workflows", "workflows": [{"template": "pr-ci", "template_version": 1,
             "filename": "ci.yml", "triggers": ["pull_request"], "checkout_sha": CHECKOUT,
             "commands": ["python3 -m unittest discover -s tests"]}]},
            {"name": "agent-skills", "bundle": "mattpocock"},
        ]}
        first_exit, first = self.apply(api, policy)
        self.assertEqual(first_exit, 0)
        self.assertEqual(len(api.prs), 1)
        branch = first["setup_branch"]
        paths = {path for ref, path in api.files if ref == branch}
        self.assertIn(".github/workflows/ci.yml", paths)
        self.assertIn(".agents/skills/manifest.json", paths)
        self.assertIn(".agents/skills/LICENSE", paths)
        self.assertIn(".agents/skills/engineering/implement-spec/SKILL.md", paths)
        skill_changes = [change for change in first["changes"] if change["stage"] == "agent-skills"]
        self.assertTrue(skill_changes)
        self.assertIn("source_commit", skill_changes[0]["desired"])

        prior_writes = list(api.writes)
        second_exit, second = self.apply(api, policy)
        self.assertEqual(second_exit, 0)
        self.assertEqual(api.writes, prior_writes)
        self.assertEqual(len(api.prs), 1)
        self.assertEqual(second["setup_pull_request"], first["setup_pull_request"])

    def test_malformed_agent_skill_selection_fails_before_api_writes(self):
        from scripts.setup_github_repo import validate_policy

        with self.assertRaisesRegex(ValueError, "bundle must be mattpocock"):
            validate_policy({"version": 2, "stages": [{"name": "agent-skills", "bundle": "unknown"}]})

    def test_agent_skills_dry_run_reports_source_and_performs_no_writes(self):
        from scripts.interactive_apply import format_dry_run_summary

        api = WorkflowApi()
        result = run_policy(api, "acme/project", {"version": 2, "stages": [
            {"name": "agent-skills", "bundle": "mattpocock"},
        ]}, apply=False)
        self.assertEqual(api.writes, [])
        self.assertIn("agent_skills_bundle", result)
        source = result["agent_skills_bundle"]
        self.assertEqual(source["destination"], ".agents/skills/")
        self.assertEqual(source["file_count"], len(json.loads(
            (Path(__file__).resolve().parents[1] / "data/agent-skills/mattpocock/manifest.json").read_text()
        )["files"]) + 1)
        summary = format_dry_run_summary(result)
        self.assertIn(source["source_commit"], summary)
        self.assertIn("Source:", summary)

    def test_agent_skills_run_reports_readiness_and_warns_when_prerequisites_missing(self):
        api = WorkflowApi()
        policy = {"version": 2, "repository": "acme/project", "stages": [
            {"name": "agent-skills", "bundle": "mattpocock"}]}
        with patch("setup_github_repo.probe_cli_tools",
                   return_value={"gh", "glab", "git"}),                 patch("scripts.setup_github_repo.probe_cli_tools",
                      return_value={"gh", "glab", "git"}):
            exit_code, outcome = self.apply(api, policy)

        self.assertEqual(exit_code, 0)
        readiness = outcome["agent_skills_readiness"]
        self.assertFalse(readiness["ready"])
        names = {item["name"] for item in readiness["prerequisites"]}
        self.assertEqual(names, {"issue-tracker", "triage-labels", "domain-docs"})
        self.assertTrue(any("repository setup is incomplete" in warning for warning in outcome["warnings"]))
        self.assertTrue(any("setup-matt-pocock-skills" in warning for warning in outcome["warnings"]))
        self.assertTrue(any("does not complete" in warning for warning in outcome["warnings"]))
        compatibility = outcome["agent_skills_compatibility"]
        self.assertGreater(compatibility["counts"]["requires-manual-setup"], 0)
        self.assertTrue(any("require manual setup" in warning for warning in outcome["warnings"]))
        self.assertIn("missing conventions: GLOSSARY.md", "\n".join(outcome["warnings"]))

    def test_configured_repository_agent_skills_readiness_has_no_warning(self):
        api = WorkflowApi()
        api.files[("main", "docs/agents/issue-tracker.md")] = (b"# Issue tracker\n", "c" * 40)
        api.files[("main", "docs/agents/triage-labels.md")] = (b"# Triage labels\n", "d" * 40)
        api.files[("main", "docs/agents/domain.md")] = (b"# Domain docs\n", "e" * 40)
        api.files[("main", "GLOSSARY.md")] = (b"# Glossary\n", "f" * 40)
        policy = {"version": 2, "repository": "acme/project", "stages": [
            {"name": "agent-skills", "bundle": "mattpocock"}]}
        with patch("setup_github_repo.probe_cli_tools",
                   return_value={"gh", "glab", "git"}),                 patch("scripts.setup_github_repo.probe_cli_tools",
                      return_value={"gh", "glab", "git"}):
            exit_code, outcome = self.apply(api, policy)

        self.assertEqual(exit_code, 0)
        self.assertTrue(outcome["agent_skills_readiness"]["ready"])
        self.assertFalse(any("repository setup is incomplete" in warning for warning in outcome.get("warnings", [])))
        self.assertTrue(outcome["agent_skills_compatibility"]["counts"]["ready"] > 0)
        self.assertEqual(outcome["agent_skills_compatibility"]["counts"]["requires-manual-setup"], 0)
        self.assertFalse(any("require manual setup" in warning for warning in outcome.get("warnings", [])))

    def test_agent_skills_readiness_failure_is_reported_without_blocking(self):
        class BrokenReadinessApi(WorkflowApi):
            def request(self, method, path, payload=None):
                if method == "GET" and path.startswith("/repos/acme/project/contents/docs/agents/"):
                    return 200, {"type": "dir", "encoding": "base64", "content": "bogus"}
                return super().request(method, path, payload)

        api = BrokenReadinessApi()
        policy = {"version": 2, "repository": "acme/project", "stages": [
            {"name": "agent-skills", "bundle": "mattpocock"}]}
        exit_code, outcome = self.apply(api, policy)

        self.assertEqual(exit_code, 0)
        readiness = outcome["agent_skills_readiness"]
        self.assertEqual(readiness["status"], "unknown")
        self.assertTrue(any("readiness" in warning for warning in outcome["warnings"]))

    def test_acknowledged_removal_reports_incompatible_skill_references(self):
        import copy
        from unittest.mock import patch

        from scripts import install_github_workflows as installer

        api = WorkflowApi()
        # Simulate a repository that already has the previous catalog revision installed.
        baseline_files, old_manifest = installer._agent_skills_bundle()
        for path, content in baseline_files.items():
            api.files[("main", path)] = (content, "s" * 40)

        prefix = "engineering/tdd/"
        file_prefix = ".agents/skills/" + prefix
        updated_files = {path: content for path, content in baseline_files.items()
                         if not path.startswith(file_prefix)}
        updated_manifest = copy.deepcopy(old_manifest)
        for relative in list(updated_manifest["files"]):
            if relative.startswith(prefix):
                del updated_manifest["files"][relative]
        updated_manifest["source_commit"] = "c" * 40
        updated_manifest["source_version"] = "2.0.0"
        updated_files[installer.AGENT_SKILLS_MANIFEST] = (
            json.dumps(updated_manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")

        policy = {"version": 2, "repository": "acme/project", "stages": [
            {"name": "agent-skills", "bundle": "mattpocock",
             "allow_removed_skills": ["engineering/tdd"]}]}
        with patch("install_github_workflows._agent_skills_bundle",
                   return_value=(updated_files, updated_manifest)), \
                patch("scripts.install_github_workflows._agent_skills_bundle",
                      return_value=(updated_files, updated_manifest)), \
                patch("setup_github_repo._agent_skills_bundle",
                      return_value=(updated_files, updated_manifest)), \
                patch("scripts.setup_github_repo._agent_skills_bundle",
                      return_value=(updated_files, updated_manifest)), \
                patch("setup_github_repo.probe_cli_tools",
                      return_value={"gh", "glab", "git"}), \
                patch("scripts.setup_github_repo.probe_cli_tools",
                      return_value={"gh", "glab", "git"}):
            exit_code, outcome = self.apply(api, policy)

        self.assertEqual(exit_code, 0)
        compatibility = outcome["agent_skills_compatibility"]
        self.assertGreater(compatibility["counts"]["incompatible"], 0)
        incompatible = {item["id"] for item in compatibility["skills"]
                        if item["category"] == "incompatible"}
        self.assertIn("engineering/ask-matt", incompatible)
        self.assertNotIn("engineering/tdd", incompatible)
        self.assertTrue(any("they cannot run as installed" in warning for warning in outcome["warnings"]))
        removed = {change["path"] for change in outcome["changes"]
                   if change["stage"] == "agent-skills" and change.get("operation") == "remove"}
        self.assertTrue(removed)

    def test_existing_version_one_example_keeps_its_stage_behavior(self):
        api = InventoryApi()
        api.actions["enabled"] = True
        example = Path(__file__).resolve().parents[1] / "examples/setup-policy.json"
        policy = json.loads(example.read_text(encoding="utf-8"))
        policy["repository"] = "acme/project"
        exit_code, outcome = self.apply(api, policy)
        self.assertEqual(exit_code, 0)
        self.assertEqual({change["stage"] for change in outcome["changes"]},
                         {"features", "actions", "merge", "actions-policy"})
        self.assertTrue(all(change["status"] == "verified" for change in outcome["changes"]))

    def test_invalid_allow_removed_skills_rejects_policy_before_any_write(self):
        api = ReviewApi()
        base_stage = {"name": "agent-skills", "bundle": "mattpocock"}
        invalid = [
            dict(base_stage, allow_removed_skills=[]),
            dict(base_stage, allow_removed_skills=["engineering/tdd", "engineering/tdd"]),
            dict(base_stage, allow_removed_skills=["engineering/UpperCase"]),
            dict(base_stage, allow_removed_skills=["engineering/-leading"]),
            dict(base_stage, allow_removed_skills=["engineering/has space"]),
            dict(base_stage, allow_removed_skills=123),
        ]
        for stage in invalid:
            with self.subTest(stage=stage):
                policy = {"version": 2, "repository": "acme/project", "stages": [stage]}
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "policy.json"
                    path.write_text(json.dumps(policy), encoding="utf-8")
                    stderr = io.StringIO()
                    with patch("scripts.headless.GitHubApi", return_value=api), \
                            redirect_stderr(stderr):
                        exit_code = run_headless_apply(str(path), token="mock-token")
                self.assertEqual(exit_code, 1, f"Expected rejection for {stage}")
                self.assertIn("Invalid policy schema", stderr.getvalue())
                self.assertEqual(api.writes, [])
                self.assertEqual(api.prs, [])

        valid = dict(base_stage, allow_removed_skills=["engineering/tdd",
                                                       "productivity/implement-spec"])
        exit_code, _ = self.apply(
            api, {"version": 2, "repository": "acme/project", "stages": [valid]})
        self.assertEqual(exit_code, 0)
