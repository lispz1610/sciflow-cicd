"""Behavior tests for the versioned workflow installer."""

import base64
import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest
import copy
import io
import hashlib
import json
import os
import re
from contextlib import redirect_stdout
from urllib.parse import unquote
from unittest.mock import patch


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "install_github_workflows.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("install_github_workflows", SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
from setup_github_repo import run_policy
from scripts import cli, headless

CHECKOUT = "11bd71901bbe5b1630ceea73d27597364c9af683"
DEPENDENCY = "56339e523c0409420f6c2c9a2f4292bbb3c07dd3"
CONTAINER_LOGIN = "9780b0c442fbb1117ed29e0efdff1e18412f7567"
CONTAINER_BUILD = "471d1dc4e07e5cdedd4c2171150001c434f0b7a4"


class WorkflowApi:
    def __init__(self):
        self.base = "/repos/acme/project"
        self.repo = {"default_branch": "main", "full_name": "acme/project"}
        self.refs = {"main": "a" * 40}
        self.files = {}
        self.prs = []
        self.writes = []
        self.wrap_base64 = False
        self.deletions = set()
        self.hide_pulls = False
        self.change_default_on_second_ref_read = False
        self.default_ref_reads = 0

    def request(self, method, path, payload=None):
        base = self.base
        if method != "GET":
            self.writes.append((method, path, payload))
        action_commits = {
            "/repos/actions/checkout/git/commits/" + CHECKOUT: CHECKOUT,
            "/repos/actions/checkout/git/commits/" + installer.DEFAULT_CHECKOUT_SHA: installer.DEFAULT_CHECKOUT_SHA,
            "/repos/actions/dependency-review-action/git/commits/" + DEPENDENCY: DEPENDENCY,
            "/repos/actions/setup-python/git/commits/" + installer.DEFAULT_SETUP_PYTHON_SHA: installer.DEFAULT_SETUP_PYTHON_SHA,
            "/repos/astral-sh/setup-uv/git/commits/" + installer.DEFAULT_SETUP_UV_SHA: installer.DEFAULT_SETUP_UV_SHA,
            "/repos/docker/login-action/git/commits/" + CONTAINER_LOGIN: CONTAINER_LOGIN,
            "/repos/docker/build-push-action/git/commits/" + CONTAINER_BUILD: CONTAINER_BUILD,
        }
        if path in action_commits and method == "GET":
            return 200, {"sha": action_commits[path]}
        if path.startswith(("/repos/actions/checkout/git/commits/",
                            "/repos/actions/dependency-review-action/git/commits/",
                            "/repos/actions/setup-python/git/commits/",
                            "/repos/astral-sh/setup-uv/git/commits/",
                            "/repos/docker/login-action/git/commits/",
                            "/repos/docker/build-push-action/git/commits/")) and method == "GET":
            return 404, None
        if path == base and method == "GET":
            return 200, self.repo
        if path.startswith(base + "/git/ref/heads/") and method == "GET":
            branch = unquote(path.removeprefix(base + "/git/ref/heads/"))
            if branch == "main":
                self.default_ref_reads += 1
                if self.change_default_on_second_ref_read and self.default_ref_reads == 2:
                    self.refs["main"] = "d" * 40
            return (200, {"object": {"sha": self.refs[branch]}}) if branch in self.refs else (404, None)
        if path == base + "/git/refs" and method == "POST":
            branch = payload["ref"].removeprefix("refs/heads/")
            self.refs[branch] = payload["sha"]
            return 201, {"ref": payload["ref"]}
        if path.startswith(base + "/compare/") and method == "GET":
            branch = unquote(path.split("...", 1)[1])
            files = [{"filename": filename,
                      "status": "modified" if ("main", filename) in self.files else "added"}
                     for (ref, filename), (content, _) in self.files.items()
                     if ref == branch and (("main", filename) not in self.files or
                                           self.files[("main", filename)][0] != content)]
            files.extend({"filename": filename, "status": "removed"}
                         for (ref, filename) in self.deletions if ref == branch)
            commits = getattr(self, "commits", {}).get(branch, [])
            return 200, {"files": files, "commits": commits}
        if path.startswith(base + "/contents/"):
            parts = path.split("?ref=")
            filename = parts[0].removeprefix(base + "/contents/")
            branch = unquote(parts[1]) if len(parts) > 1 else payload["branch"]
            key = (branch, filename)
            if method == "GET":
                if key in self.deletions:
                    return 404, None
                content = self.files.get(key)
                if content is None and branch != "main":
                    content = self.files.get(("main", filename))
                if content is None:
                    return 404, None
                data, sha = content
                encoded = base64.b64encode(data).decode()
                if self.wrap_base64:
                    encoded = "\n".join(encoded[i:i + 76] for i in range(0, len(encoded), 76)) + "\n"
                return 200, {"type": "file", "encoding": "base64", "content": encoded, "sha": sha}
            if method == "DELETE":
                if key in self.files:
                    sha = self.files.pop(key)[1]
                elif ("main", filename) in self.files:
                    sha = self.files[("main", filename)][1]
                else:
                    return 404, None
                self.deletions.add(key)
                self.refs[branch] = str(len(self.writes)).zfill(40)
                return 200, {"content": {"sha": sha}}
            content = base64.b64decode(payload["content"])
            self.files[key] = (content, str(len(self.writes)).zfill(40))
            self.refs[branch] = str(len(self.writes)).zfill(40)
            return 201, {"content": {"sha": self.files[key][1]}}
        if path.startswith(base + "/pulls?head=") and method == "GET":
            branch = unquote(path.split("head=acme%3A", 1)[1].split("&", 1)[0])
            return 200, [] if self.hide_pulls else [pr for pr in self.prs if pr["head"]["ref"] == branch]
        if path == base + "/pulls?state=open&base=main&per_page=100" and method == "GET":
            return 200, self.prs
        if path == base + "/pulls" and method == "POST":
            pr = {"number": len(self.prs) + 1, "html_url": "https://github.com/acme/project/pull/1",
                  "head": {"ref": payload["head"]}, "base": {"ref": payload["base"]},
                  "draft": payload["draft"]}
            self.prs.append(pr)
            return 201, pr
        if method == "GET":
            return 404, None
        raise AssertionError((method, path, payload))


def policy():
    return {"version": 1, "workflows": [
        {"template": "pr-ci", "template_version": 1, "filename": "ci.yml",
         "triggers": ["pull_request"], "checkout_sha": CHECKOUT,
         "commands": ["python3 -m unittest discover -s tests"]}]}


class WorkflowInstallerTests(unittest.TestCase):
    def test_sciflow_cicd_second_file_identity_failure_keeps_unattempted_files_pending(self):
        class ConcurrentChangeApi(WorkflowApi):
            def __init__(self):
                super().__init__()
                self.repo["has_wiki"] = False

            def request(self, method, path, payload=None):
                if method == "PATCH" and path == self.base:
                    self.writes.append((method, path, payload))
                    self.repo.update(payload)
                    self.files[("main", "docs/guide.md")] = (b"owner review\n", "b" * 40)
                    return 200, self.repo
                return super().request(method, path, payload)

        api = ConcurrentChangeApi()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "guide.md").write_text("owner review\n")
            policy_file = root / "policy.json"
            policy_file.write_text(json.dumps({"version": 2, "repository": "acme/project",
                "stages": [{"name": "features", "enable": ["wiki"]},
                           {"name": "files", "items": [{"path": "docs/guide.md", "source": "guide.md"}]}]}))
            with patch.object(headless, "GitHubApi", return_value=api), \
                    patch.dict(os.environ, {"GITHUB_TOKEN": "test-only"}):
                preview = io.StringIO()
                with redirect_stdout(preview):
                    self.assertEqual(cli.main(["dry-run", "--policy", str(policy_file)]), 0)
                dry_run_file = root / "dry-run.json"
                dry_run_file.write_text(preview.getvalue())
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.main(["apply", "--policy", str(policy_file),
                                               "--dry-run-result", str(dry_run_file)]), 2)
                result = json.loads(output.getvalue())
                self.assertEqual([item["status"] for item in result["changes"]],
                                 ["verified", "pending", "failed"])
                self.assertIn("Selected files changed", result["changes"][-1]["reason"])
                self.assertEqual([write[0] for write in api.writes], ["PATCH"])

    def test_sciflow_cicd_recovers_when_branch_creation_response_is_ambiguous(self):
        class AmbiguousBranchApi(WorkflowApi):
            fail_branch = True

            def request(self, method, path, payload=None):
                if method == "POST" and path == self.base + "/git/refs" and self.fail_branch:
                    self.fail_branch = False
                    super().request(method, path, payload)
                    raise installer.ApiError("connection closed after branch creation")
                return super().request(method, path, payload)

        api = AmbiguousBranchApi()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "guide.md").write_text("owner review\n")
            policy_file = root / "policy.json"
            policy_file.write_text(json.dumps({"version": 2, "repository": "acme/project",
                "stages": [{"name": "files", "items": [{"path": "docs/guide.md", "source": "guide.md"}]}]}))
            with patch.object(headless, "GitHubApi", return_value=api), \
                    patch.dict(os.environ, {"GITHUB_TOKEN": "test-only"}):
                preview = io.StringIO()
                with redirect_stdout(preview):
                    self.assertEqual(cli.main(["dry-run", "--policy", str(policy_file)]), 0)
                dry_run_file = root / "dry-run.json"
                dry_run_file.write_text(preview.getvalue())
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.main(["apply", "--policy", str(policy_file),
                                               "--dry-run-result", str(dry_run_file)]), 2)
                failed = json.loads(output.getvalue())
                self.assertIn(failed["setup_branch"], api.refs)
                self.assertEqual([item["status"] for item in failed["changes"]], ["pending", "failed"])
                self.assertIn("connection closed", failed["recovery"])
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.main(["apply", "--policy", str(policy_file),
                                               "--dry-run-result", str(dry_run_file)]), 0)
                self.assertEqual(len(api.prs), 1)

    def test_sciflow_cicd_reports_pr_creation_failure_after_all_files_staged(self):
        class InterruptedApi(WorkflowApi):
            fail_pull = True

            def request(self, method, path, payload=None):
                if method == "POST" and path == self.base + "/pulls" and self.fail_pull:
                    self.fail_pull = False
                    raise installer.ApiError("GitHub rejected the pull request")
                return super().request(method, path, payload)

        api = InterruptedApi()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "guide.md").write_text("owner review\n")
            policy_file = root / "policy.json"
            policy_file.write_text(json.dumps({"version": 2, "repository": "acme/project",
                "stages": [{"name": "files", "items": [{"path": "docs/guide.md", "source": "guide.md"}]}]}))
            with patch.object(headless, "GitHubApi", return_value=api), \
                    patch.dict(os.environ, {"GITHUB_TOKEN": "test-only"}):
                preview = io.StringIO()
                with redirect_stdout(preview):
                    self.assertEqual(cli.main(["dry-run", "--policy", str(policy_file)]), 0)
                plan_file = root / "dry-run.json"
                plan_file.write_text(preview.getvalue())
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.main(["apply", "--policy", str(policy_file),
                                               "--plan", str(plan_file)]), 2)
                failed = json.loads(output.getvalue())
                self.assertEqual(failed["changes"][0]["status"], "applied")
                self.assertTrue(any(change["status"] == "failed" for change in failed["changes"]))
                self.assertIn(failed["setup_branch"], api.refs)
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.main(["apply", "--policy", str(policy_file),
                                               "--plan", str(plan_file)]), 0)
                self.assertEqual(len(api.prs), 1)

    def test_sciflow_cicd_reports_partial_staging_and_retries_one_setup_pr(self):
        class InterruptedApi(WorkflowApi):
            fail_path = "docs/second.md"

            def request(self, method, path, payload=None):
                if method == "PUT" and self.fail_path and path.endswith("/contents/" + self.fail_path):
                    self.fail_path = None
                    raise installer.ApiError("GitHub rejected the second file")
                return super().request(method, path, payload)

        api = InterruptedApi()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for name in ("first", "second", "third"):
                (root / (name + ".md")).write_text(name + "\n")
            selected = {"version": 2, "repository": "acme/project", "stages": [{
                "name": "files", "items": [
                    {"path": "docs/" + name + ".md", "source": name + ".md"}
                    for name in ("first", "second", "third")],
            }]}
            policy_file = root / "policy.json"
            policy_file.write_text(json.dumps(selected))
            with patch.object(headless, "GitHubApi", return_value=api), \
                    patch.dict(os.environ, {"GITHUB_TOKEN": "test-only"}):
                preview = io.StringIO()
                with redirect_stdout(preview):
                    self.assertEqual(cli.main(["dry-run", "--policy", str(policy_file)]), 0)
                plan_file = root / "dry-run.json"
                plan_file.write_text(preview.getvalue())
                failed_output = io.StringIO()
                with redirect_stdout(failed_output):
                    self.assertEqual(cli.main(["apply", "--policy", str(policy_file),
                                               "--plan", str(plan_file)]), 2)
                failed = json.loads(failed_output.getvalue())
                states = {change["path"]: change["status"] for change in failed["changes"]}
                self.assertEqual(states, {"docs/first.md": "applied",
                                          "docs/second.md": "failed",
                                          "docs/third.md": "pending"})
                self.assertIn(failed["setup_branch"], api.refs)
                self.assertIn("retry", failed["recovery"].lower())
                self.assertEqual(api.prs, [])
                retry_output = io.StringIO()
                with redirect_stdout(retry_output):
                    self.assertEqual(cli.main(["apply", "--policy", str(policy_file),
                                               "--plan", str(plan_file)]), 0)
                retry = json.loads(retry_output.getvalue())
                self.assertEqual({change["status"] for change in retry["changes"]}, {"pending"})
                self.assertEqual(retry["setup_pull_request"], "https://github.com/acme/project/pull/1")
                self.assertEqual(len(api.prs), 1)
                self.assertEqual(len([write for write in api.writes if write[0] == "POST" and
                                      write[1].endswith("/pulls")]), 1)
                self.assertEqual(len([write for write in api.writes if write[0] == "PUT" and
                                      write[1].endswith("/contents/docs/first.md")]), 1)

    def test_sciflow_cicd_headless_files_apply_reports_owner_review(self):
        api = WorkflowApi()
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "guide.md"
            source.write_text("Review this document\n")
            selected = {"version": 2, "repository": "acme/project", "stages": [
                {"name": "files", "items": [{"path": "docs/guide.md", "source": "guide.md"}]},
            ]}
            policy_file = pathlib.Path(directory) / "policy.json"
            policy_file.write_text(json.dumps(selected))
            output = io.StringIO()
            with patch.object(headless, "GitHubApi", return_value=api), \
                    patch.dict(os.environ, {"GITHUB_TOKEN": "test-only"}), redirect_stdout(output):
                preview = io.StringIO()
                with redirect_stdout(preview):
                    self.assertEqual(cli.main(["dry-run", "--policy", str(policy_file)]), 0)
                plan_file = pathlib.Path(directory) / "dry-run.json"
                plan_file.write_text(preview.getvalue())
                code = cli.main(["apply", "--policy", str(policy_file), "--plan", str(plan_file)])
            result = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(result["changes"][0]["status"], "pending")
            self.assertEqual(result["setup_pull_request"], "https://github.com/acme/project/pull/1")
            self.assertNotIn(("main", "docs/guide.md"), api.files)

    def test_policy_files_dry_run_and_apply_use_one_reviewed_pr(self):
        api = WorkflowApi()
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "guide.md"
            source.write_text("Private setup detail\n")
            selected = {"version": 2, "stages": [
                {"name": "files", "items": [{"path": "docs/guide.md", "source": str(source)}]},
                {"name": "workflows", "workflows": policy()["workflows"]},
            ]}
            preview = run_policy(api, "acme/project", selected)
            self.assertEqual(api.writes, [])
            file_change = next(change for change in preview["changes"] if change["stage"] == "files")
            self.assertEqual(file_change["path"], "docs/guide.md")
            self.assertEqual(file_change["desired_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertNotIn("Private setup detail", json.dumps(preview))
            applied = run_policy(api, "acme/project", selected, apply=True, accepted_dry_run=preview)
            self.assertEqual(len(api.prs), 1)
            self.assertEqual(api.files[(applied["setup_branch"], "docs/guide.md")][0], source.read_bytes())
            self.assertNotIn(("main", "docs/guide.md"), api.files)
            self.assertEqual(next(change for change in applied["changes"] if change["stage"] == "files")["status"], "pending")

    def test_policy_files_matching_default_branch_are_no_op(self):
        api = WorkflowApi()
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "guide.md"
            source.write_text("Already installed\n")
            api.files[("main", "docs/guide.md")] = (source.read_bytes(), "a" * 40)
            selected = {"version": 2, "stages": [{"name": "files", "items": [
                {"path": "docs/guide.md", "source": str(source)}]}]}
            preview = run_policy(api, "acme/project", selected)
            result = run_policy(api, "acme/project", selected, apply=True, accepted_dry_run=preview)
            self.assertEqual(result["changes"], [])
            self.assertEqual(api.writes, [])

    def test_policy_files_conflict_preserves_owner_file(self):
        api = WorkflowApi()
        with tempfile.TemporaryDirectory() as directory:
            source = pathlib.Path(directory) / "guide.md"
            source.write_text("Proposed\n")
            api.files[("main", "docs/guide.md")] = (b"Owner edit\n", "a" * 40)
            selected = {"version": 2, "stages": [{"name": "files", "items": [
                {"path": "docs/guide.md", "source": str(source)}]}]}
            preview = run_policy(api, "acme/project", selected)
            result = run_policy(api, "acme/project", selected, apply=True, accepted_dry_run=preview)
            self.assertEqual(result["changes"][0]["status"], "blocked")
            self.assertIn("authored file conflict", result["changes"][0]["reason"])
            self.assertEqual(api.writes, [])
            self.assertEqual(api.files[("main", "docs/guide.md")][0], b"Owner edit\n")

    def test_review_stage_is_not_staged_in_setup_pr(self):
        review = {
            "name": "review",
            "workflow": {
                "template": "open-code-review",
                "template_version": 1,
                "filename": "open-code-review.yml",
                "triggers": ["pull_request_target"],
                "events": ["opened", "synchronize", "reopened"],
                "fork_policy": "skip",
                "action_sha": "182898cf522da3d04157b422752d028417974e19",
                "ocr_version": "1.12.12",
                "max_tokens_budget": 50000,
                "timeout_minutes": 30,
            },
            "llm": {
                "url": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
                "use_anthropic": False,
                "secret_name": "OCR_LLM_AUTH_TOKEN",
            },
            "limits": {"max_retention_days": 30},
        }
        api = WorkflowApi()
        result = installer.install_selected(api, "acme/project", review_stage=review, apply=True)
        staged = {path for ref, path in api.files if ref == result.get("branch")}
        self.assertNotIn(".github/workflows/open-code-review.yml", staged)
        self.assertNotIn("scripts/review_issue_reporter.js", staged)
        self.assertEqual(staged, set())
        self.assertEqual(result.get("changes"), [])

        workflow_stage = {
            "workflows": [
                {
                    "template": "pr-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT,
                    "commands": ["pytest"],
                }
            ]
        }
        res = installer.install_selected(api, "acme/project", workflow_stage=workflow_stage, review_stage=review, apply=True)
        staged = {path for ref, path in api.files if ref == res.get("branch")}
        self.assertIn(".github/workflows/ci.yml", staged)
        self.assertNotIn(".github/workflows/open-code-review.yml", staged)
        self.assertNotIn("scripts/review_issue_reporter.js", staged)

    def test_render_review_reporter_removed(self):
        self.assertFalse(hasattr(installer, "render_review_reporter"))

    def test_agent_skills_upgrade_changes_only_files_matching_the_previous_manifest(self):
        api = WorkflowApi()
        stage = {"name": "agent-skills", "bundle": "mattpocock"}
        first = installer.install_selected(api, "acme/project", agent_skills_stage=stage, apply=True)
        first_branch = first["branch"]
        for path, item in tuple(api.files.items()):
            ref, filename = path
            if ref == first_branch:
                api.files[("main", filename)] = item
        api.prs.clear()  # The owner merged the initial Setup Pull Request.
        selected_path = ".agents/skills/engineering/implement-spec/SKILL.md"
        baseline_files, old_manifest = installer._agent_skills_bundle()
        updated_files = dict(baseline_files)
        updated_files[selected_path] = baseline_files[selected_path] + b"\nUpdated reviewed copy.\n"
        updated_manifest = copy.deepcopy(old_manifest)
        updated_manifest["source_commit"] = "d" * 40
        updated_manifest["source_version"] = "9.9.9"
        updated_manifest["files"][selected_path.removeprefix(".agents/skills/")] = hashlib.sha256(
            updated_files[selected_path]
        ).hexdigest()
        updated_files[installer.AGENT_SKILLS_MANIFEST] = (
            json.dumps(updated_manifest, indent=2, sort_keys=True) + "\n"
        ).encode()

        with patch.object(installer, "_agent_skills_bundle", return_value=(updated_files, updated_manifest)):
            result = installer.install_selected(api, "acme/project", agent_skills_stage=stage, apply=True)

        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(len(api.prs), 1)
        changed = {path for (ref, path) in api.files if ref == result["branch"]}
        self.assertEqual(changed, {selected_path, installer.AGENT_SKILLS_MANIFEST})
        self.assertEqual(api.files[(result["branch"], selected_path)][0], updated_files[selected_path])

    def test_agent_skills_upgrade_blocks_diverged_owner_file_without_partial_writes(self):
        api = WorkflowApi()
        stage = {"name": "agent-skills", "bundle": "mattpocock"}
        first = installer.install_selected(api, "acme/project", agent_skills_stage=stage, apply=True)
        path = ".agents/skills/engineering/implement-spec/SKILL.md"
        api.files[("main", path)] = (b"owner edit\n", "b" * 40)
        api.files[("main", installer.AGENT_SKILLS_MANIFEST)] = api.files[
            (first["branch"], installer.AGENT_SKILLS_MANIFEST)
        ]
        api.prs.clear()
        prior_writes = list(api.writes)

        with self.assertRaisesRegex(ValueError, "diverged from its manifest"):
            installer.install_selected(api, "acme/project", agent_skills_stage=stage, apply=True)

        self.assertEqual(api.writes, prior_writes)
        self.assertEqual(api.prs, [])

    def test_agent_skills_bundle_is_staged_with_selected_workflows_in_one_setup_pr(self):
        api = WorkflowApi()

        result = installer.install_selected(
            api, "acme/project", workflow_stage={"name": "workflows", "workflows": [
                {"template": "pr-ci", "template_version": 1, "filename": "ci.yml",
                 "triggers": ["pull_request"], "checkout_sha": CHECKOUT,
                 "commands": ["python3 -m unittest discover -s tests"]},
            ]},
            agent_skills_stage={"name": "agent-skills", "bundle": "mattpocock"}, apply=True,
        )

        branch = result["branch"]
        staged_paths = {path for ref, path in api.files if ref == branch}
        source_manifest = json.loads((pathlib.Path(__file__).resolve().parents[1] /
                                      "data/agent-skills/mattpocock/manifest.json").read_text())
        expected_paths = {".github/workflows/ci.yml", ".agents/skills/LICENSE", ".agents/skills/manifest.json"}
        expected_paths.update(".agents/skills/" + item["destination"] for item in source_manifest["files"])
        expected_paths.update(installer.render_documentation_suite())
        self.assertEqual(staged_paths, expected_paths)
        self.assertEqual(len(api.prs), 1)
        self.assertEqual(result["status"], "pending_review")

    def test_documentation_audit_suite_is_staged_as_one_setup_pull_request(self):
        api = WorkflowApi()
        result = installer.install_selected(
            api, "acme/project", documentation_stage={"name": "documentation-freshness"}, apply=True,
        )
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(len(api.prs), 1)
        expected = set(installer.render_documentation_suite())
        staged = {path for ref, path in api.files if ref == result["branch"]}
        self.assertEqual(staged, expected)
        workflow = api.files[(result["branch"], installer.DOCUMENTATION_WORKFLOW)][0]
        self.assertIn(b'DOCS_TARGET_MODE: "true"', workflow)
        self.assertIn(b"github.event.pull_request.head.repo.full_name == github.repository", workflow)
        self.assertIn("scripts/github_client.py", expected)

    def test_staged_documentation_reporter_imports_github_client_in_isolation(self):
        suite = installer.render_documentation_suite()
        self.assertIn("scripts/github_client.py", suite)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            for rel_path, (content, _sha) in suite.items():
                dest = root / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
            env = {key: val for key, val in os.environ.items() if key != "PYTHONPATH"}
            proc = subprocess.run(
                [sys.executable, "scripts/documentation_issue_reporter.py", "--help"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("documentation", proc.stdout.lower())

    def test_issue_state_gate_and_documentation_suite_share_github_client_without_conflict(self):
        api = WorkflowApi()
        result = installer.install_selected(
            api,
            "acme/project",
            issue_state_gate_stage={"name": "issue-state-gate"},
            documentation_stage={"name": "documentation-freshness"},
            apply=True,
        )
        self.assertEqual(result["status"], "pending_review")
        staged = {path for ref, path in api.files if ref == result["branch"]}
        self.assertIn("scripts/github_client.py", staged)
        self.assertIn("scripts/documentation_issue_reporter.py", staged)
        self.assertIn("scripts/issue_state_gate.py", staged)

    def test_agent_skills_bundle_refuses_conflicting_target_file_without_writes(self):
        api = WorkflowApi()
        api.files[("main", ".agents/skills/engineering/implement-spec/SKILL.md")] = (
            b"owner-written content\n", "b" * 40,
        )

        with self.assertRaisesRegex(ValueError, "agent skill conflict"):
            installer.install_selected(
                api, "acme/project", agent_skills_stage={"name": "agent-skills", "bundle": "mattpocock"},
                apply=True,
            )

        self.assertEqual(api.writes, [])
        self.assertEqual(api.prs, [])

    def test_agent_skills_bundle_fails_when_catalog_has_invalid_provenance_or_license(self):
        api = WorkflowApi()
        stage = {"name": "agent-skills", "bundle": "mattpocock"}
        with patch("scripts.provision_agent_skills.load_verified_catalog", side_effect=ValueError("skill snapshot provenance is malformed")):
            with self.assertRaisesRegex(ValueError, "skill snapshot provenance is malformed"):
                installer.install_selected(api, "acme/project", agent_skills_stage=stage, apply=True)
        self.assertEqual(api.writes, [])
        self.assertEqual(api.prs, [])

    def test_allow_removed_skills_validation_is_shared_and_rejects_invalid_ids(self):
        api = WorkflowApi()
        invalid = [
            [], ["engineering/tdd", "engineering/tdd"], ["engineering/UpperCase"],
            ["engineering/-leading"], ["engineering/has space"],
            ["engineering/dot.file"], ["engineering/"], ["engineering"], ["productivity/Upper"], 123, ["engineering/tdd", 123], "engineering/tdd",
        ]
        for case in invalid:
            with self.subTest(case=case):
                with self.assertRaisesRegex(ValueError, "nonempty list of unique skill ids"):
                    installer.validate_allow_removed_skills(case)
                with self.assertRaisesRegex(ValueError, "nonempty list of unique skill ids"):
                    installer.install_selected(
                        api, "acme/project", agent_skills_stage={
                            "name": "agent-skills", "bundle": "mattpocock",
                            "allow_removed_skills": case}, apply=True)
        self.assertEqual(installer.validate_allow_removed_skills(["engineering/tdd"]),
                         ["engineering/tdd"])
        self.assertEqual(installer.validate_allow_removed_skills(
            ["engineering/tdd", "productivity/implement-spec"]),
            ["engineering/tdd", "productivity/implement-spec"])

    def test_issue_state_gate_renders_only_the_checked_in_trusted_workflow(self):
        content = installer.render({"template": "issue-state-gate", "template_version": 1,
                                    "filename": "issue-state-gate.yml"})
        self.assertIn(b"name: Issue State Gate", content)
        self.assertIn(b"checks: write", content)
        self.assertIn(b"actions/checkout@", content)
        with self.assertRaises(ValueError):
            installer.render({"template": "issue-state-gate", "template_version": 1,
                              "filename": "other.yml"})

    def test_issue_state_gate_setup_pr_contains_refresh_and_recovery_workflows(self):
        api = WorkflowApi()

        result = installer.install_selected(
            api, "acme/project", issue_state_gate_stage={"name": "issue-state-gate"}, apply=True
        )

        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(len(api.prs), 1)
        branch = result["branch"]
        installed_paths = {path for (ref, path) in api.files if ref == branch}
        self.assertEqual(installed_paths, {
            ".github/workflows/issue-state-gate.yml",
            ".github/workflows/issue-state-gate-refresh.yml",
            ".github/workflows/issue-state-gate-reconcile.yml",
            ".github/workflows/issue-delivery-enforce.yml",
            "scripts/issue_state_gate.py",
            "scripts/refresh_issue_state_gate.py",
            "scripts/reconcile_issue_state_gate.py",
            "scripts/enforce_issue_delivery.py",
            "scripts/verify_issue_delivery.py",
            "scripts/configure_github_repo.py",
            "scripts/github_client.py",
            "scripts/path_security.py",
        })
        self.assertTrue(all(api.files[(branch, path)][0].startswith(b"# sciflow-cicd ")
                            for path in installed_paths))

    def test_issue_gate_runtime_conflict_blocks_setup_before_writes(self):
        api = WorkflowApi()
        api.files[("main", "scripts/configure_github_repo.py")] = (b"# existing repository utility\n", "b" * 40)

        with self.assertRaisesRegex(ValueError, "unmanaged.*configure_github_repo"):
            installer.install_selected(api, "acme/project", issue_state_gate_stage={"name": "issue-state-gate"}, apply=True)

        self.assertEqual(api.writes, [])

    def test_issue_gate_suite_rejects_path_collision_with_selected_workflow(self):
        api = WorkflowApi()
        selected = policy()
        selected["workflows"][0]["filename"] = "issue-state-gate-refresh.yml"

        with self.assertRaisesRegex(ValueError, "conflicts"):
            installer.install_selected(
                api, "acme/project", workflow_stage=selected,
                issue_state_gate_stage={"name": "issue-state-gate"}, apply=True,
            )

        self.assertEqual(api.writes, [])

    def test_staged_closure_handler_runs_by_path_without_source_checkout(self):
        selected = installer.render_issue_state_gate_suite()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for path, (content, _) in selected.items():
                if path.startswith("scripts/"):
                    target = root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
            result = subprocess.run(
                [sys.executable, str(root / "scripts/enforce_issue_delivery.py"), "--help"],
                cwd=root, capture_output=True, text=True, check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--issue", result.stdout)

    def test_rerunning_setup_when_files_already_staged_is_idempotent_no_op(self):
        api = WorkflowApi()
        first = installer.install(api, "acme/project", policy(), apply=True)
        self.assertEqual(len(api.prs), 1)
        api.writes.clear()

        second = installer.install(api, "acme/project", policy(), apply=True)
        self.assertEqual(second["status"], "pending_review")
        self.assertEqual(second["changes"], [])
        self.assertEqual(second["pull_request"], first["pull_request"])
        self.assertEqual(len(api.prs), 1)
        self.assertEqual(api.writes, [])

    def test_updating_policy_updates_existing_open_setup_pr_branch_cleanly(self):
        api = WorkflowApi()
        first = installer.install(api, "acme/project", policy(), apply=True)
        self.assertEqual(len(api.prs), 1)

        selected = policy()
        selected["workflows"][0]["commands"] = ["python3 -m pytest"]
        api.writes.clear()

        updated = installer.install(api, "acme/project", selected, apply=True)
        self.assertEqual(updated["pull_request"], first["pull_request"])
        self.assertEqual(len(api.prs), 1, "Must not create duplicate Pull Requests")
        put_writes = [w for w in api.writes if w[0] == "PUT"]
        self.assertTrue(len(put_writes) >= 1)
        self.assertIn("Update selected workflow template", put_writes[0][2]["message"])

    def test_existing_setup_pr_branch_with_owner_edits_guards_against_destructive_overwrites(self):
        api = WorkflowApi()
        first = installer.install(api, "acme/project", policy(), apply=True)
        branch = api.prs[0]["head"]["ref"]

        api.commits = {
            branch: [
                {"commit": {"message": "owner custom tweak", "author": {"name": "repo-owner"}}}
            ]
        }
        selected = policy()
        selected["workflows"][0]["commands"] = ["python3 -m pytest"]
        api.writes.clear()

        with self.assertRaisesRegex(ValueError, "owner edits detected"):
            installer.install(api, "acme/project", selected, apply=True)
        self.assertEqual(api.writes, [], "Must not overwrite branch when owner edits exist")

    def test_authored_file_modified_on_pr_branch_blocks_destructive_overwrite(self):
        """Modified authored file on Setup PR branch is detected as drift and blocks overwrite."""
        api = WorkflowApi()
        files = {"docs/guide.md": (b"# Authored Guide\nOriginal content\n", None)}
        installer.stage_workflows(api, "acme/project", files, apply=True, authored_paths={"docs/guide.md"})
        branch = api.prs[0]["head"]["ref"]

        # Owner modifies docs/guide.md on the Setup PR branch
        api.files[(branch, "docs/guide.md")] = (b"# Authored Guide\nOwner customized edits\n", "e" * 40)
        api.writes.clear()

        # Tool attempts to stage docs/guide.md again with original content
        with self.assertRaisesRegex(ValueError, "guarding against destructive overwrites"):
            installer.stage_workflows(api, "acme/project", files, apply=True, authored_paths={"docs/guide.md"})
        self.assertEqual(api.writes, [])

    def test_owner_commit_detected_via_author_metadata_despite_automation_message(self):
        """Owner commits are identified by author metadata rather than commit message prefixes."""
        api = WorkflowApi()
        installer.install(api, "acme/project", policy(), apply=True)
        branch = api.prs[0]["head"]["ref"]

        # Commit has automation message prefix, but author is human owner
        api.commits = {
            branch: [
                {
                    "author": {"login": "repo-owner", "type": "User"},
                    "commit": {
                        "message": "Install selected workflow template (owner manual edit)",
                        "author": {"name": "Repo Owner", "email": "owner@example.com"},
                    },
                }
            ]
        }
        selected = policy()
        selected["workflows"][0]["commands"] = ["python3 -m pytest"]
        api.writes.clear()

        with self.assertRaisesRegex(ValueError, "owner edits detected"):
            installer.install(api, "acme/project", selected, apply=True)
        self.assertEqual(api.writes, [])

    def test_legacy_review_pr_blocks_workflow_staging(self):
        api = WorkflowApi()
        api.prs.append({"number": 7, "html_url": "https://github.com/acme/project/pull/7",
                        "head": {"ref": "open-code-review-setup"}, "base": {"ref": "main"}, "draft": True})
        with self.assertRaisesRegex(ValueError, "pull/7"):
            installer.install(api, "acme/project", policy(), apply=True)
        self.assertEqual(api.writes, [])

    def test_workflow_conflict_does_not_cancel_independent_direct_settings(self):
        class CombinedApi(WorkflowApi):
            def __init__(self):
                super().__init__()
                self.repo["has_wiki"] = False

            def request(self, method, path, payload=None):
                if method == "PATCH" and path == self.base:
                    self.writes.append((method, path, payload))
                    self.repo.update(payload)
                    return 200, self.repo
                return super().request(method, path, payload)

        api = CombinedApi()
        api.prs.append({"number": 7, "html_url": "https://github.com/acme/project/pull/7",
                        "head": {"ref": "open-code-review-setup"}, "base": {"ref": "main"}, "draft": True})
        selected = {"version": 1, "stages": [
            {"name": "features", "enable": ["wiki"]},
            {"name": "workflows", "workflows": policy()["workflows"]},
        ]}
        result = run_policy(api, "acme/project", selected, apply=True)
        self.assertFalse(api.repo["has_wiki"])
        self.assertEqual({item["status"] for item in result["changes"]}, {"planned", "blocked"})
        self.assertEqual(api.writes, [])

    def test_failed_staging_reports_partial_branch_for_manual_recovery(self):
        class FailingApi(WorkflowApi):
            def request(self, method, path, payload=None):
                if method == "POST" and path == self.base + "/pulls":
                    self.writes.append((method, path, payload))
                    return 500, None
                return super().request(method, path, payload)

        api = FailingApi()
        with self.assertRaisesRegex(installer.ApiError, "codex/workflows-.*Inspect"):
            installer.install(api, "acme/project", policy(), apply=True)
        self.assertTrue(any(ref.startswith("codex/workflows-") for ref in api.refs))

    def test_policy_dry_run_and_apply_stage_selected_workflows_in_one_setup_pr(self):
        api = WorkflowApi()
        workflow_policy = policy()
        workflow_policy["workflows"].append({"template": "dependency-review", "template_version": 1,
                                              "filename": "dependencies.yml", "triggers": ["pull_request"],
                                              "checkout_sha": CHECKOUT, "action_sha": DEPENDENCY})
        selected = {"version": 1, "stages": [{"name": "workflows", "workflows": workflow_policy["workflows"]}]}
        dry_run = run_policy(api, "acme/project", selected)
        self.assertEqual({item["path"] for item in dry_run["changes"]}, {
            ".github/workflows/ci.yml", ".github/workflows/dependencies.yml"})
        self.assertEqual(api.writes, [])
        applied = run_policy(api, "acme/project", selected, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(len(api.prs), 1)
        self.assertEqual(applied["setup_pull_request"], "https://github.com/acme/project/pull/1")
        self.assertEqual({item["status"] for item in applied["changes"]}, {"pending"})
        api.writes.clear()
        repeated_dry_run = run_policy(api, "acme/project", selected)
        repeated = run_policy(api, "acme/project", selected, apply=True, accepted_dry_run=repeated_dry_run)
        self.assertEqual(api.writes, [])
        self.assertEqual(repeated["setup_pull_request"], applied["setup_pull_request"])

    def test_dry_run_creates_no_branch_and_apply_opens_one_pr(self):
        api = WorkflowApi()
        planned = installer.install(api, "acme/project", policy())
        self.assertEqual(len(planned["changes"]), 1)
        self.assertEqual(api.writes, [])
        result = installer.install(api, "acme/project", policy(), apply=True)
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(result["pull_request"], "https://github.com/acme/project/pull/1")
        self.assertEqual(installer.install(api, "acme/project", policy(), apply=True)["changes"], [])

    def test_ci_is_read_only_pinned_and_has_explicit_safe_trigger(self):
        content = installer.render(policy()["workflows"][0]).decode()
        self.assertIn("on:\n  pull_request:", content)
        self.assertIn("permissions:\n  contents: read", content)
        self.assertIn("actions/checkout@" + CHECKOUT, content)
        self.assertIn("python3 -m unittest discover -s tests", content)
        self.assertNotIn("pull_request_target", content)

    def test_unmanaged_or_drifted_default_file_blocks_without_writes(self):
        api = WorkflowApi()
        api.files[("main", ".github/workflows/ci.yml")] = (b"name: someone else's workflow\n", "b" * 40)
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            installer.install(api, "acme/project", policy(), apply=True)
        self.assertEqual(api.writes, [])

    def test_existing_managed_file_needs_explicit_expected_sha(self):
        api = WorkflowApi()
        api.files[("main", ".github/workflows/ci.yml")] = (installer.render(policy()["workflows"][0]).replace(b"unittest", b"pytest"), "b" * 40)
        with self.assertRaisesRegex(ValueError, "drift"):
            installer.install(api, "acme/project", policy())
        selected = policy()
        selected["workflows"][0]["expected_sha"] = "b" * 40
        self.assertEqual(len(installer.install(api, "acme/project", selected)["changes"]), 1)

    def test_rejects_unpinned_action_and_unsafe_trigger(self):
        selected = policy()
        selected["workflows"][0]["checkout_sha"] = "v4"
        with self.assertRaises(ValueError):
            installer.render(selected["workflows"][0])
        selected["workflows"][0]["checkout_sha"] = CHECKOUT
        selected["workflows"][0]["triggers"] = ["pull_request_target"]
        with self.assertRaises(ValueError):
            installer.render(selected["workflows"][0])

    def test_rejects_unknown_template_before_any_write(self):
        api = WorkflowApi()
        selected = policy()
        selected["workflows"][0]["template"] = "external-review"
        with self.assertRaisesRegex(ValueError, "supported version 1 workflow template"):
            installer.install(api, "acme/project", selected, apply=True)
        self.assertEqual(api.writes, [])

    def test_nonexistent_pinned_action_commit_blocks_before_write(self):
        api = WorkflowApi()
        selected = policy()
        selected["workflows"][0]["checkout_sha"] = "a" * 40
        with self.assertRaisesRegex(ValueError, "pinned action commit"):
            installer.install(api, "acme/project", selected, apply=True)
        self.assertEqual(api.writes, [])

    def test_optional_dependency_review_requires_pin_and_read_only_permissions(self):
        selected = {"template": "dependency-review", "template_version": 1,
                    "filename": "dependency-review.yml", "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT, "action_sha": DEPENDENCY}
        content = installer.render(selected).decode()
        self.assertIn("dependency-review-action@" + DEPENDENCY, content)
        self.assertIn("contents: read", content)
        self.assertIn("pull-requests: read", content)

    def test_reads_github_wrapped_base64_file_content(self):
        api = WorkflowApi()
        api.wrap_base64 = True
        api.files[("main", ".github/workflows/ci.yml")] = (installer.render(policy()["workflows"][0]), "b" * 40)
        self.assertEqual(installer.install(api, "acme/project", policy())["changes"], [])

    def test_existing_branch_edit_blocks_apply(self):
        api = WorkflowApi()
        initial = installer.install(api, "acme/project", policy(), apply=True)
        api.files[(initial["branch"], ".github/workflows/ci.yml")] = (b"unexpected edit", "c" * 40)
        api.writes.clear()
        with self.assertRaisesRegex(ValueError, "branch drift"):
            installer.install(api, "acme/project", policy(), apply=True)
        self.assertEqual(api.writes, [])

    def test_rendered_templates_parse_as_yaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is optional and unavailable")
        selected = policy()["workflows"][0]
        selected["commands"] = ["printf 'hello: world\n'", "echo \"quoted\""]
        for workflow in (selected, {"template": "dependency-review", "template_version": 1,
                                    "filename": "dependency.yml", "triggers": ["pull_request"],
                                    "checkout_sha": CHECKOUT, "action_sha": DEPENDENCY}):
            parsed = yaml.load(installer.render(workflow), Loader=yaml.BaseLoader)
            self.assertEqual(list(parsed["on"]), ["pull_request"])
            self.assertEqual(parsed["permissions"]["contents"], "read")
            if workflow["template"] == "pr-ci":
                self.assertEqual([step["run"] for step in parsed["jobs"]["ci"]["steps"] if "run" in step],
                                 workflow["commands"])

    def test_apply_does_not_claim_pr_when_readback_cannot_find_it(self):
        api = WorkflowApi()
        api.hide_pulls = True
        with self.assertRaisesRegex(installer.ApiError, "pull request.*verified"):
            installer.install(api, "acme/project", policy(), apply=True)

    def test_default_branch_advance_blocks_first_write(self):
        api = WorkflowApi()
        api.change_default_on_second_ref_read = True
        with self.assertRaisesRegex(installer.ApiError, "default branch advanced"):
            installer.install(api, "acme/project", policy(), apply=True)
        self.assertEqual(api.writes, [])

    def test_existing_branch_with_unrelated_file_is_never_used_for_pr(self):
        api = WorkflowApi()
        initial = installer.install(api, "acme/project", policy(), apply=True)
        api.files[(initial["branch"], "unrelated.txt")] = (b"unrelated", "e" * 40)
        api.writes.clear()
        with self.assertRaisesRegex(ValueError, "unrelated branch changes"):
            installer.install(api, "acme/project", policy(), apply=True)
        self.assertEqual(api.writes, [])

    def test_existing_branch_with_unrelated_modified_file_is_rejected(self):
        api = WorkflowApi()
        api.files[("main", "README.md")] = (b"original", "f" * 40)
        initial = installer.install(api, "acme/project", policy(), apply=True)
        api.files[(initial["branch"], "README.md")] = (b"different", "e" * 40)
        api.writes.clear()
        with self.assertRaisesRegex(ValueError, "unrelated branch changes"):
            installer.install(api, "acme/project", policy(), apply=True)
        self.assertEqual(api.writes, [])

    def test_selected_ci_and_dependency_review_share_one_pr(self):
        api = WorkflowApi()
        selected = policy()
        selected["workflows"].append({"template": "dependency-review", "template_version": 1,
                                      "filename": "dependencies.yml", "triggers": ["pull_request"],
                                      "checkout_sha": CHECKOUT, "action_sha": DEPENDENCY})
        result = installer.install(api, "acme/project", selected, apply=True)
        self.assertEqual(len([entry for entry in api.writes if entry[0] == "PUT"]), 2)
        self.assertEqual(len(api.prs), 1)
        api.writes.clear()
        again = installer.install(api, "acme/project", selected, apply=True)
        self.assertEqual(again["changes"], [])
        self.assertEqual(api.writes, [])

    def test_default_install_excludes_container_workflow(self):
        api = WorkflowApi()
        default_policy = policy()
        plan = installer.install(api, "acme/project", default_policy, apply=False)
        paths = [item["path"] for item in plan["changes"]]
        self.assertIn(".github/workflows/ci.yml", paths)
        self.assertNotIn(".github/workflows/container-publish.yml", paths)
        self.assertNotIn(".github/workflows/container.yml", paths)

    def test_opt_in_container_workflow_installs_with_least_privilege(self):
        api = WorkflowApi()
        selected = policy()
        selected["workflows"].append({
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "login_action_sha": CONTAINER_LOGIN,
            "build_action_sha": CONTAINER_BUILD,
        })
        result = installer.install(api, "acme/project", selected, apply=True)
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(len(api.prs), 1)
        container_content = api.files[(result["branch"], ".github/workflows/container-publish.yml")][0].decode("utf-8")
        self.assertIn("permissions:", container_content)
        self.assertIn("contents: read", container_content)
        self.assertIn("packages: write", container_content)
        self.assertIn(f"docker/login-action@{CONTAINER_LOGIN}", container_content)
        self.assertIn("if: github.event_name != 'pull_request'", container_content)
        self.assertIn(f"docker/build-push-action@{CONTAINER_BUILD}", container_content)
        self.assertIn("push: ${{ github.event_name != 'pull_request' }}", container_content)
        self.assertNotIn("push: true", container_content)
        self.assertIn('registry: "ghcr.io"', container_content)

    def test_extract_profile_status_check_handles_escaped_quotes_and_reordered_keys(self):
        # Double-quoted with escaped quotes and reordered job keys
        wf_reordered_double = (
            "# sciflow-cicd workflow-template=profile-ci version=1\n"
            "jobs:\n"
            "  ci:\n"
            "    runs-on: ubuntu-24.04\n"
            "  status-check:\n"
            "    needs: [ci]\n"
            "    if: always()\n"
            "    runs-on: ubuntu-24.04\n"
            '    name: "custom \\"quoted\\" check"\n'
            "    steps:\n"
            '      - name: "Evaluate status check"\n'
        )
        self.assertEqual(
            installer.extract_profile_status_check(wf_reordered_double),
            'custom "quoted" check',
        )

        # Single-quoted with doubled single quotes ('') and reordered job keys
        wf_reordered_single = (
            "# sciflow-cicd workflow-template=profile-ci version=1\n"
            "jobs:\n"
            "  status-check:\n"
            "    runs-on: ubuntu-24.04\n"
            "    if: always()\n"
            "    name: 'team''s ''ci'' gate'\n"
            "    steps:\n"
            "      - name: 'Step name'\n"
        )
        self.assertEqual(
            installer.extract_profile_status_check(wf_reordered_single),
            "team's 'ci' gate",
        )

        # Status-check without job-level name: must NOT pick up step name or next job's name
        wf_no_job_name = (
            "# sciflow-cicd workflow-template=profile-ci version=1\n"
            "jobs:\n"
            "  status-check:\n"
            "    needs: [ci]\n"
            "    runs-on: ubuntu-24.04\n"
            "    steps:\n"
            '      - name: "Evaluate status check"\n'
            "  post-merge:\n"
            '    name: "Post-merge"\n'
        )
        self.assertEqual(
            installer.extract_profile_status_check(wf_no_job_name),
            "status-check",
        )

        # Round-trip from rendered profile workflow with escaped quotes in status_check_name
        rendered = installer._render_profile_workflow(
            {
                "template": "profile-ci",
                "template_version": 1,
                "profile": "python",
                "filename": "ci.yml",
                "triggers": ["pull_request"],
                "checkout_sha": CHECKOUT,
                "status_check_name": 'ci "smoke" & \'unit\'',
            }
        )
        self.assertEqual(
            installer.extract_profile_status_check(rendered.decode("utf-8")),
            'ci "smoke" & \'unit\'',
        )

    def test_issue_state_gate_cas_drift_and_expected_sha_forwarding(self):
        from setup_github_repo import validate_policy as validate_repo_policy
        api = WorkflowApi()
        suite = installer.render_issue_state_gate_suite()
        drifted = suite[".github/workflows/issue-state-gate.yml"][0] + b"# drifted\n"
        api.files[("main", ".github/workflows/issue-state-gate.yml")] = (drifted, "b" * 40)

        with self.assertRaisesRegex(ValueError, "managed workflow drift"):
            installer.install_selected(api, "acme/project", issue_state_gate_stage={"name": "issue-state-gate"})
        with self.assertRaisesRegex(ValueError, "managed workflow drift"):
            installer.install_selected(
                api, "acme/project",
                issue_state_gate_stage={"name": "issue-state-gate", "expected_sha": "c" * 40},
            )
        plan = installer.install_selected(
            api, "acme/project",
            issue_state_gate_stage={"name": "issue-state-gate", "expected_sha": "b" * 40},
        )
        self.assertTrue(any(c["path"] == ".github/workflows/issue-state-gate.yml" for c in plan["changes"]))

        validate_repo_policy({"version": 2, "stages": [{"name": "issue-state-gate", "expected_sha": "b" * 40}]})
        with self.assertRaisesRegex(ValueError, "expected_sha must be a 40-character blob SHA"):
            validate_repo_policy({"version": 2, "stages": [{"name": "issue-state-gate", "expected_sha": "bad"}]})

    def test_documentation_freshness_cas_drift_rejection(self):
        api = WorkflowApi()
        suite = installer.render_documentation_suite()
        drifted = suite[installer.DOCUMENTATION_WORKFLOW][0] + b"# drifted\n"
        api.files[("main", installer.DOCUMENTATION_WORKFLOW)] = (drifted, "b" * 40)

        with self.assertRaisesRegex(ValueError, "managed workflow drift"):
            installer.install_selected(api, "acme/project", documentation_stage={"name": "documentation-freshness"})
        plan = installer.install_selected(
            api, "acme/project",
            agent_skills_stage={"name": "agent-skills", "bundle": "mattpocock", "expected_sha": "b" * 40},
        )
        self.assertTrue(any(c["path"] == installer.DOCUMENTATION_WORKFLOW for c in plan["changes"]))

    def test_dependabot_expected_sha_forwarding_and_cas_drift(self):
        from dependabot_generator import validate_dependabot_stage
        from setup_github_repo import validate_policy as validate_repo_policy

        validated = validate_dependabot_stage({"name": "dependabot", "expected_sha": "b" * 40})
        self.assertEqual(validated["expected_sha"], "b" * 40)
        with self.assertRaisesRegex(ValueError, "expected_sha must be a 40-character blob SHA"):
            validate_dependabot_stage({"name": "dependabot", "expected_sha": "bad-sha"})

        validate_repo_policy({"version": 2, "stages": [{"name": "dependabot", "expected_sha": "b" * 40}]})

        api = WorkflowApi()
        with self.assertRaisesRegex(ValueError, "expected file is absent"):
            installer.install_selected(
                api, "acme/project",
                dependabot_stage={"name": "dependabot", "expected_sha": "b" * 40},
            )

    def test_install_selected_with_issue_state_gate_in_workflow_stage(self):
        api = WorkflowApi()
        plan = installer.install_selected(
            api, "acme/project",
            workflow_stage={"workflows": [{
                "template": "issue-state-gate",
                "template_version": 1,
                "filename": "issue-state-gate.yml",
            }]},
        )
        self.assertTrue(any(c["path"] == ".github/workflows/issue-state-gate.yml" for c in plan["changes"]))

        suite = installer.render_issue_state_gate_suite()
        drifted = suite[".github/workflows/issue-state-gate.yml"][0] + b"# drifted\n"
        api.files[("main", ".github/workflows/issue-state-gate.yml")] = (drifted, "b" * 40)
        with self.assertRaisesRegex(ValueError, "managed workflow drift"):
            installer.install_selected(
                api, "acme/project",
                workflow_stage={"workflows": [{
                    "template": "issue-state-gate",
                    "template_version": 1,
                    "filename": "issue-state-gate.yml",
                }]},
            )
        plan_cas = installer.install_selected(
            api, "acme/project",
            workflow_stage={"workflows": [{
                "template": "issue-state-gate",
                "template_version": 1,
                "filename": "issue-state-gate.yml",
                "expected_sha": "b" * 40,
            }]},
        )
        self.assertTrue(any(c["path"] == ".github/workflows/issue-state-gate.yml" for c in plan_cas["changes"]))

    def test_issue_state_gate_expected_shas_policy_validation(self):
        from setup_github_repo import validate_policy as validate_repo_policy

        valid_shas = {
            ".github/workflows/issue-state-gate.yml": "a" * 40,
            ".github/workflows/issue-state-gate-refresh.yml": "b" * 40,
            "scripts/issue_state_gate.py": "c" * 40,
        }
        # Accepts expected_shas on both standalone issue-state-gate stage and workflows entry
        validate_repo_policy({"version": 2, "stages": [{"name": "issue-state-gate", "expected_shas": valid_shas}]})
        validate_repo_policy({
            "version": 2,
            "stages": [{
                "name": "workflows",
                "workflows": [{
                    "template": "issue-state-gate",
                    "template_version": 1,
                    "filename": "issue-state-gate.yml",
                    "expected_shas": valid_shas,
                }],
            }],
        })
        rendered = installer.validate_policy({
            "version": 1,
            "workflows": [{
                "template": "issue-state-gate",
                "template_version": 1,
                "filename": "issue-state-gate.yml",
                "expected_sha": "a" * 40,
                "expected_shas": {
                    ".github/workflows/issue-state-gate-refresh.yml": "b" * 40,
                    "scripts/issue_state_gate.py": "c" * 40,
                },
            }],
        })
        self.assertEqual(rendered[".github/workflows/issue-state-gate.yml"][1], "a" * 40)
        self.assertEqual(rendered[".github/workflows/issue-state-gate-refresh.yml"][1], "b" * 40)
        self.assertEqual(rendered["scripts/issue_state_gate.py"][1], "c" * 40)

        invalid_cases = [
            ("expected_shas must be a mapping", ["not-a-dict"]),
            ("unknown suite path", {".github/workflows/unknown.yml": "a" * 40}),
            ("40-character blob SHA", {"scripts/issue_state_gate.py": "bad-sha"}),
        ]
        for pattern, bad_shas in invalid_cases:
            with self.subTest(pattern=pattern, kind="stage"):
                with self.assertRaisesRegex(ValueError, pattern):
                    validate_repo_policy({"version": 2, "stages": [{"name": "issue-state-gate", "expected_shas": bad_shas}]})
            with self.subTest(pattern=pattern, kind="workflow"):
                with self.assertRaisesRegex(ValueError, pattern):
                    validate_repo_policy({
                        "version": 2,
                        "stages": [{
                            "name": "workflows",
                            "workflows": [{
                                "template": "issue-state-gate",
                                "template_version": 1,
                                "filename": "issue-state-gate.yml",
                                "expected_shas": bad_shas,
                            }],
                        }],
                    })

        # Conflicting expected_sha and expected_shas for primary workflow
        with self.assertRaisesRegex(ValueError, "conflicting"):
            validate_repo_policy({
                "version": 2,
                "stages": [{
                    "name": "issue-state-gate",
                    "expected_sha": "a" * 40,
                    "expected_shas": {".github/workflows/issue-state-gate.yml": "b" * 40},
                }],
            })
        with self.assertRaisesRegex(ValueError, "conflicting"):
            installer.validate_policy({
                "version": 1,
                "workflows": [{
                    "template": "issue-state-gate",
                    "template_version": 1,
                    "filename": "issue-state-gate.yml",
                    "expected_sha": "a" * 40,
                    "expected_shas": {".github/workflows/issue-state-gate.yml": "b" * 40},
                }],
            })

    def test_issue_state_gate_multi_file_expected_shas_staging_and_convergence(self):
        suite = installer.render_issue_state_gate_suite()
        companion_path = ".github/workflows/issue-state-gate-refresh.yml"
        script_path = "scripts/issue_state_gate.py"
        companion_sha = "1" * 40
        script_sha = "2" * 40

        def make_api():
            api = WorkflowApi()
            for path, (content, _) in suite.items():
                if path == companion_path:
                    api.files[("main", path)] = (content + b"# old companion\n", companion_sha)
                elif path == script_path:
                    api.files[("main", path)] = (content + b"# old script\n", script_sha)
                else:
                    api.files[("main", path)] = (content, "9" * 40)
            return api

        # Missing expected_shas surfaces all drifting paths
        with self.assertRaises(ValueError) as ctx:
            installer.install_selected(
                make_api(), "acme/project",
                issue_state_gate_stage={"name": "issue-state-gate"},
            )
        self.assertIn("managed workflow drift", str(ctx.exception))
        self.assertIn(companion_path, str(ctx.exception))
        self.assertIn(script_path, str(ctx.exception))

        # Matching expected_shas (with unchanged files omitted) stages both via issue_state_gate_stage
        api_stage = make_api()
        expected_shas = {companion_path: companion_sha, script_path: script_sha}
        plan = installer.install_selected(
            api_stage, "acme/project",
            issue_state_gate_stage={"name": "issue-state-gate", "expected_shas": expected_shas},
            apply=True,
        )
        self.assertEqual(
            {c["path"] for c in plan["changes"]},
            {companion_path, script_path},
        )
        self.assertIsNotNone(plan.get("pull_request"))

        # Simulate PR merge onto main -> repeat run is a no-op
        for path in (companion_path, script_path):
            api_stage.files[("main", path)] = (suite[path][0], "a" * 40)
        noop = installer.install_selected(
            api_stage, "acme/project",
            issue_state_gate_stage={"name": "issue-state-gate", "expected_shas": expected_shas},
        )
        self.assertEqual(noop["changes"], [])

        # Same upgrade via workflows stage entry
        api_wf = make_api()
        plan_wf = installer.install_selected(
            api_wf, "acme/project",
            workflow_stage={"workflows": [{
                "template": "issue-state-gate",
                "template_version": 1,
                "filename": "issue-state-gate.yml",
                "expected_shas": expected_shas,
            }]},
            apply=True,
        )
        self.assertEqual(
            {c["path"] for c in plan_wf["changes"]},
            {companion_path, script_path},
        )
        self.assertIsNotNone(plan_wf.get("pull_request"))

    def test_dependabot_update_managed_and_reject_unmanaged_on_default_and_pr_branch(self):
        from dependabot_generator import render_dependabot_config, validate_dependabot_stage

        api = WorkflowApi()
        v1 = render_dependabot_config(validate_dependabot_stage({"name": "dependabot", "cadence": "monthly"})).encode("utf-8")
        api.files[("main", ".github/dependabot.yml")] = (v1, "d" * 40)

        # Updating managed dependabot.yml on default branch without expected_sha fails CAS drift
        with self.assertRaisesRegex(ValueError, "managed workflow drift"):
            installer.install_selected(
                api, "acme/project",
                dependabot_stage={"name": "dependabot", "cadence": "weekly"},
            )

        # Updating managed dependabot.yml on default branch with matching expected_sha succeeds
        result = installer.install_selected(
            api, "acme/project",
            dependabot_stage={"name": "dependabot", "cadence": "weekly", "expected_sha": "d" * 40},
            apply=True,
        )
        self.assertEqual(result["status"], "pending_review")
        branch = result["branch"]
        self.assertIn((branch, ".github/dependabot.yml"), api.files)

        # Re-staging updated managed dependabot.yml onto the open Setup PR branch succeeds
        api.writes.clear()
        updated = installer.install_selected(
            api, "acme/project",
            dependabot_stage={"name": "dependabot", "cadence": "daily", "expected_sha": "d" * 40},
            apply=True,
        )
        self.assertEqual(updated["status"], "pending_review")
        self.assertEqual(updated["branch"], branch)

        # If dependabot.yml on the open Setup PR branch is replaced with unmanaged content, fail closed
        api.files[(branch, ".github/dependabot.yml")] = (
            b'version: 2\nupdates:\n  - package-ecosystem: "pip"\n    directory: "/"\n',
            "e" * 40,
        )
        api.writes.clear()
        with self.assertRaisesRegex(ValueError, "owner edits detected"):
            installer.install_selected(
                api, "acme/project",
                dependabot_stage={"name": "dependabot", "cadence": "weekly", "expected_sha": "d" * 40},
                apply=True,
            )
        self.assertEqual(api.writes, [])

        # Unmanaged user-authored dependabot.yml on default branch fails closed even with expected_sha
        api2 = WorkflowApi()
        api2.files[("main", ".github/dependabot.yml")] = (
            b'version: 2\nupdates:\n  - package-ecosystem: "pip"\n    directory: "/"\n',
            "f" * 40,
        )
        with self.assertRaisesRegex(ValueError, "unmanaged workflow or runtime exists"):
            installer.install_selected(
                api2, "acme/project",
                dependabot_stage={"name": "dependabot", "cadence": "weekly", "expected_sha": "f" * 40},
            )

    def test_hook_package_update_on_open_setup_pr_branch_and_fail_closed_on_drift(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pkg_dir = pathlib.Path(temp_dir) / "hook-one"
            pkg_dir.mkdir()
            s1 = b"#!/usr/bin/env bash\necho v1\n"
            (pkg_dir / "run.sh").write_bytes(s1)
            (pkg_dir / "manifest.json").write_text(json.dumps({
                "schema_version": 1,
                "name": "hook-one",
                "description": "Hook one",
                "event": "session-start",
                "runtime": "bash",
                "entrypoint": "run.sh",
                "files": {"run.sh": hashlib.sha256(s1).hexdigest()},
                "permissions": [],
                "opt_in": True,
            }), encoding="utf-8")

            api = WorkflowApi()
            first = installer.install_selected(
                api, "acme/project",
                hooks_stage={"opt_in": True, "packages": [str(pkg_dir)]},
                apply=True,
            )
            self.assertEqual(first["status"], "pending_review")
            branch = first["branch"]

            # Update hook package to v2 and re-stage onto the open Setup PR branch
            s2 = b"#!/usr/bin/env bash\necho v2\n"
            (pkg_dir / "run.sh").write_bytes(s2)
            (pkg_dir / "manifest.json").write_text(json.dumps({
                "schema_version": 1,
                "name": "hook-one",
                "description": "Hook one v2",
                "event": "session-start",
                "runtime": "bash",
                "entrypoint": "run.sh",
                "files": {"run.sh": hashlib.sha256(s2).hexdigest()},
                "permissions": [],
                "opt_in": True,
            }), encoding="utf-8")

            api.writes.clear()
            updated = installer.install_selected(
                api, "acme/project",
                hooks_stage={"opt_in": True, "packages": [str(pkg_dir)]},
                apply=True,
            )
            self.assertEqual(updated["status"], "pending_review")
            self.assertEqual(updated["branch"], branch)
            self.assertEqual(api.files[(branch, ".agents/hooks/hook-one/run.sh")][0], s2)

            # Manually edit run.sh on the PR branch without matching its manifest -> must fail closed
            api.files[(branch, ".agents/hooks/hook-one/run.sh")] = (b"#!/usr/bin/env bash\necho owner-edit\n", "e" * 40)
            s3 = b"#!/usr/bin/env bash\necho v3\n"
            (pkg_dir / "run.sh").write_bytes(s3)
            (pkg_dir / "manifest.json").write_text(json.dumps({
                "schema_version": 1,
                "name": "hook-one",
                "description": "Hook one v3",
                "event": "session-start",
                "runtime": "bash",
                "entrypoint": "run.sh",
                "files": {"run.sh": hashlib.sha256(s3).hexdigest()},
                "permissions": [],
                "opt_in": True,
            }), encoding="utf-8")
            api.writes.clear()
            with self.assertRaisesRegex(ValueError, "owner edits detected"):
                installer.install_selected(
                    api, "acme/project",
                    hooks_stage={"opt_in": True, "packages": [str(pkg_dir)]},
                    apply=True,
                )
            self.assertEqual(api.writes, [])

    def test_agent_skills_prior_version_update_on_open_setup_pr_branch_and_fail_closed_on_drift(self):
        api = WorkflowApi()
        stage = {"name": "agent-skills", "bundle": "mattpocock"}
        first = installer.install_selected(api, "acme/project", agent_skills_stage=stage, apply=True)
        self.assertEqual(first["status"], "pending_review")
        branch = first["branch"]

        selected_path = ".agents/skills/engineering/implement-spec/SKILL.md"
        baseline_files, old_manifest = installer._agent_skills_bundle()
        updated_files = dict(baseline_files)
        updated_files[selected_path] = baseline_files[selected_path] + b"\nUpdated reviewed copy.\n"
        updated_manifest = copy.deepcopy(old_manifest)
        updated_manifest["source_commit"] = "d" * 40
        updated_manifest["source_version"] = "9.9.9"
        updated_manifest["files"][selected_path.removeprefix(".agents/skills/")] = hashlib.sha256(
            updated_files[selected_path]
        ).hexdigest()
        updated_files[installer.AGENT_SKILLS_MANIFEST] = (
            json.dumps(updated_manifest, indent=2, sort_keys=True) + "\n"
        ).encode()

        # Re-stage updated bundle onto the open Setup PR branch containing prior-version bundle
        api.writes.clear()
        with patch.object(installer, "_agent_skills_bundle", return_value=(updated_files, updated_manifest)):
            updated = installer.install_selected(api, "acme/project", agent_skills_stage=stage, apply=True)
        self.assertEqual(updated["status"], "pending_review")
        self.assertEqual(updated["branch"], branch)
        self.assertEqual(api.files[(branch, selected_path)][0], updated_files[selected_path])

        # Manually edit skill file on the open Setup PR branch so it diverges from its manifest -> fail closed
        api.files[(branch, selected_path)] = (b"manual owner edit on PR branch\n", "e" * 40)
        v3_files = dict(updated_files)
        v3_files[selected_path] = baseline_files[selected_path] + b"\nThird reviewed copy.\n"
        v3_manifest = copy.deepcopy(updated_manifest)
        v3_manifest["source_commit"] = "e" * 40
        v3_manifest["source_version"] = "10.0.0"
        v3_manifest["files"][selected_path.removeprefix(".agents/skills/")] = hashlib.sha256(
            v3_files[selected_path]
        ).hexdigest()
        v3_files[installer.AGENT_SKILLS_MANIFEST] = (
            json.dumps(v3_manifest, indent=2, sort_keys=True) + "\n"
        ).encode()

        api.writes.clear()
        with patch.object(installer, "_agent_skills_bundle", return_value=(v3_files, v3_manifest)):
            with self.assertRaisesRegex(ValueError, "owner edits detected"):
                installer.install_selected(api, "acme/project", agent_skills_stage=stage, apply=True)
        self.assertEqual(api.writes, [])

    def test_runner_dependencies_rejects_invalid_apt_packages(self):
        base_wf = {
            "template": "profile-ci",
            "template_version": 1,
            "profile": "cmake-mpi",
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "runs_on": "ubuntu-24.04",
        }
        for bad_pkg in ["libfoo; rm -rf /", "libfoo && id", "BadUpper", "a", "-leading-dash", "pkg\nname"]:
            wf = copy.deepcopy(base_wf)
            wf["cmake_mpi"] = {"runner_dependencies": [bad_pkg]}
            with self.assertRaises(ValueError):
                installer.render(wf)

        wf_alias = copy.deepcopy(base_wf)
        wf_alias["cmake_mpi"] = {"apt_packages": ["gcc-14; id"]}
        with self.assertRaises(ValueError):
            installer.render(wf_alias)

        wf_valid = copy.deepcopy(base_wf)
        wf_valid["cmake_mpi"] = {"runner_dependencies": ["libopenmpi-dev", "g++", "python3.12"]}
        rendered = installer.render(wf_valid).decode("utf-8")
        self.assertIn("libopenmpi-dev g++ python3.12", rendered)

    def test_env_keys_validated_across_templates(self):
        pr_wf = {
            "template": "pr-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "commands": ["pytest"],
        }
        profile_wf = {
            "template": "profile-ci",
            "template_version": 1,
            "profile": "python",
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
        }
        for bad_key in ["BAD-KEY", "1INVALID", "FOO: bar", "FOO\nBAR", "BAD KEY"]:
            for base in (pr_wf, profile_wf):
                wf = copy.deepcopy(base)
                wf["env"] = {bad_key: "val"}
                with self.assertRaises(ValueError):
                    installer.render(wf)

        for base in (pr_wf, profile_wf):
            wf = copy.deepcopy(base)
            wf["env"] = {"VALID_ENV_1": "ok", "_PRIVATE": "1"}
            rendered = installer.render(wf).decode("utf-8")
            self.assertIn('VALID_ENV_1: "ok"', rendered)

    def test_scalar_workflow_fields_quoted_and_reject_newlines(self):
        container_base = {
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": ["pull_request", "push"],
            "checkout_sha": CHECKOUT,
            "login_action_sha": CONTAINER_LOGIN,
            "build_action_sha": CONTAINER_BUILD,
        }
        for field, bad_val in [
            ("registry", "ghcr.io\nevil: true"),
            ("registry", "ghcr.io\revil: true"),
            ("context", ".\nevil: true"),
            ("dockerfile", "Dockerfile\r\nevil: true"),
            ("tags", "ghcr.io/org/app:latest\nevil: true"),
            ("tags", ["ghcr.io/org/app:latest\nevil"]),
            ("push_branches", ["main\nevil"]),
        ]:
            wf = copy.deepcopy(container_base)
            wf[field] = bad_val
            with self.assertRaises(ValueError):
                installer.render(wf)

        wf_good = copy.deepcopy(container_base)
        wf_good.update({
            "registry": "ghcr.io",
            "context": "./app",
            "dockerfile": "docker/Dockerfile",
            "tags": "ghcr.io/acme/app:v1",
            "push_branches": ["main", "release/*"],
        })
        rendered = installer.render(wf_good).decode("utf-8")
        self.assertIn('registry: "ghcr.io"', rendered)
        self.assertIn('context: "./app"', rendered)
        self.assertIn('file: "docker/Dockerfile"', rendered)
        self.assertIn('tags: "ghcr.io/acme/app:v1"', rendered)
        self.assertIn('- "main"', rendered)
        self.assertIn('- "release/*"', rendered)

    def test_dependency_review_pull_requests_permission_matches_comment_summary(self):
        base = {
            "template": "dependency-review",
            "template_version": 1,
            "filename": "dependency-review.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "action_sha": DEPENDENCY,
        }
        for val in ("always", "on-failure", True):
            wf = dict(base, comment_summary_in_pr=val)
            rendered = installer.render(wf).decode("utf-8")
            self.assertIn("permissions:\n  contents: read\n  pull-requests: write", rendered)
            self.assertIn("    permissions:\n      contents: read\n      pull-requests: write", rendered)

        for val in ("never", False):
            wf = dict(base, comment_summary_in_pr=val)
            rendered = installer.render(wf).decode("utf-8")
            self.assertIn("permissions:\n  contents: read\n  pull-requests: read", rendered)
            self.assertIn("    permissions:\n      contents: read\n      pull-requests: read", rendered)


class ActionVersionTagTests(unittest.TestCase):
    """Live workflow pins must render accurate inline version comments."""

    repo_root = pathlib.Path(__file__).resolve().parents[1]

    # Tags resolved from the upstream GitHub API for the SHAs currently pinned
    # in .github/workflows (checked 2026-11-22).
    LIVE_PINS = {
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1": "v7.0.1",
        "actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0": "v7.1.0",
        "actions/dependency-review-action@a1d282b36b6f3519aa1f3fc636f609c47dddb294": "v5.0.0",
        "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9": "v7.0.2",
        "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333": "v8.0.2",
    }

    def _live_references(self):
        references = set()
        for workflow in sorted((self.repo_root / ".github" / "workflows").glob("*.yml")):
            content = workflow.read_text(encoding="utf-8")
            references.update("{}@{}".format(*ref) for ref in re.findall(
                r"uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-f]{40})", content))
        return references

    def test_every_live_action_pin_has_an_accurate_tag(self):
        references = self._live_references()
        self.assertTrue(references)
        for reference, expected_tag in self.LIVE_PINS.items():
            self.assertIn(reference, references)
        for reference in references:
            sha = reference.rsplit("@", 1)[1]
            self.assertIn(sha, installer.ACTION_VERSION_TAGS, f"missing tag for {reference}")
        for reference, expected_tag in self.LIVE_PINS.items():
            sha = reference.rsplit("@", 1)[1]
            self.assertEqual(installer.ACTION_VERSION_TAGS[sha], expected_tag)

    def test_rendered_workflows_emit_live_version_comments(self):
        checkout = "3d3c42e5aac5ba805825da76410c181273ba90b1"
        setup_python = "6d4fd22b20465ccc0f7354476588e8df7c9ca0e0"
        dependency = "a1d282b36b6f3519aa1f3fc636f609c47dddb294"

        pr_ci = installer.render({
            "template": "pr-ci", "template_version": 1, "filename": "ci.yml",
            "triggers": ["pull_request"], "checkout_sha": checkout,
            "commands": ["pytest -v"],
        }).decode("utf-8")
        self.assertIn(f"actions/checkout@{checkout} # v7.0.1", pr_ci)

        profile_ci = installer.render({
            "template": "profile-ci", "template_version": 1, "profile": "python",
            "filename": "ci.yml", "triggers": ["pull_request"],
            "checkout_sha": checkout, "setup_python_sha": setup_python,
            "profile_options": {"package_manager": "pip", "python_versions": ["3.12"]},
        }).decode("utf-8")
        self.assertIn(f"actions/checkout@{checkout} # v7.0.1", profile_ci)
        self.assertIn(f"actions/setup-python@{setup_python} # v7.1.0", profile_ci)

        dep_review = installer.render({
            "template": "dependency-review", "template_version": 1,
            "filename": "dependency-review.yml", "triggers": ["pull_request"],
            "checkout_sha": checkout, "action_sha": dependency,
        }).decode("utf-8")
        self.assertIn(f"actions/dependency-review-action@{dependency} # v5.0.0", dep_review)

    def test_static_workflow_comments_match_live_tags(self):
        pattern = re.compile(
            r"uses:\s*([^\s@]+)@([0-9a-f]{40})(?:\s+#\s*(v\S+))?"
        )
        for workflow in sorted((self.repo_root / ".github" / "workflows").glob("*.yml")):
            for lineno, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
                match = pattern.search(line)
                if match is None:
                    continue
                name, sha, comment = match.groups()
                reference = f"{name}@{sha}"
                expected = self.LIVE_PINS.get(reference)
                self.assertIsNotNone(expected, f"missing live tag for {reference}")
                self.assertEqual(
                    comment,
                    expected,
                    f"{workflow.relative_to(self.repo_root)}:{lineno} has "
                    f"comment {comment!r}; expected {expected!r}",
                )

    def test_action_tag_handles_case_non_sha_defaults_and_legacy_pins(self):
        checkout = "3d3c42e5aac5ba805825da76410c181273ba90b1"
        self.assertEqual(installer._action_tag(checkout.upper()), " # v7.0.1")
        for non_sha in (None, 7, "", "v7.0.1", "https://github.com/actions/checkout"):
            self.assertEqual(installer._action_tag(non_sha), "", f"unexpected tag for {non_sha!r}")

        # Legacy pins are still emitted by templates and remain documented.
        for sha in (
            "11bd71901bbe5b1630ceea73d27597364c9af683",
            "56339e523c0409420f6c2c9a2f4292bbb3c07dd3",
            "ece7cb06caefa5fff74198d8649806c4678c61a1",
            "d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86",
            "16503c80cf5a76fa8f73f55462fa418931eb6f8b",
            "5a2ce3f5b92ee19cbb1541a4984c76d921601d7c",
            "08c6903cd8c0fde910a37f88322edcfb5dd907a8",
            "fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09",
            installer.DEFAULT_CONTAINER_LOGIN_SHA,
            installer.DEFAULT_CONTAINER_BUILD_SHA,
            installer.DEFAULT_CACHE_SHA,
        ):
            self.assertIn(sha, installer.ACTION_VERSION_TAGS)


if __name__ == "__main__":
    unittest.main()
