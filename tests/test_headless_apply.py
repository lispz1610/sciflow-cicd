"""Tests for headless policy apply integration."""

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
TESTS_DIR = ROOT_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from configure_github_repo import ApiError
from scripts.headless import run_headless_apply
from scripts.interactive_apply import execute_dry_run
from scripts.setup_github_repo import compute_policy_sha256, run_policy


def apply_with_plan(policy_path, repo=None, token=None):
    """Exercise Headless Mode with a saved public Dry-Run result."""
    preview = io.StringIO()
    with redirect_stdout(preview):
        run_headless_apply(policy_path, repo=repo, token=token, apply=False)
    with tempfile.TemporaryDirectory() as directory:
        plan_path = Path(directory) / "dry-run.json"
        plan_path.write_text(preview.getvalue(), encoding="utf-8")
        return run_headless_apply(policy_path, repo=repo, token=token,
                                  plan_path=str(plan_path))
from test_review_setup import review_stage


class TestHeadlessApplyMissingToken(unittest.TestCase):
    def test_missing_github_token_exits_1_with_error_on_stderr(self):
        """Missing GITHUB_TOKEN exits 1 with error on stderr."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps({"version": 1, "stages": [{"name": "actions"}]}))
            f.flush()
            policy_path = f.name

        try:
            stderr = io.StringIO()
            env_without_token = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
            with patch.dict(os.environ, env_without_token, clear=True), redirect_stderr(stderr):
                exit_code = run_headless_apply(policy_path, repo="octocat/Hello-World")

            self.assertEqual(exit_code, 1)
            err_output = stderr.getvalue()
            self.assertIn("GITHUB_TOKEN", err_output)
            self.assertIn("Error:", err_output)
        finally:
            if os.path.exists(policy_path):
                os.remove(policy_path)


class TestHeadlessApplyFileLoading(unittest.TestCase):
    def test_nonexistent_policy_file_exits_1_with_error_on_stderr(self):
        """Nonexistent policy file exits 1 with error on stderr."""
        stderr = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_TOKEN": "mock-token"}), redirect_stderr(stderr):
            exit_code = run_headless_apply("nonexistent_path_policy.json", repo="octocat/Hello-World")

        self.assertEqual(exit_code, 1)
        err_output = stderr.getvalue()
        self.assertIn("Error:", err_output)
        self.assertIn("nonexistent_path_policy.json", err_output)

    def test_malformed_json_exits_1_with_error_on_stderr(self):
        """Malformed JSON in policy file exits 1 with error on stderr."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{invalid json: not valid")
            f.flush()
            policy_path = f.name

        try:
            stderr = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_TOKEN": "mock-token"}), redirect_stderr(stderr):
                exit_code = run_headless_apply(policy_path, repo="octocat/Hello-World")

            self.assertEqual(exit_code, 1)
            err_output = stderr.getvalue()
            self.assertIn("Error:", err_output)
            self.assertTrue("json" in err_output.lower() or "decode" in err_output.lower())
        finally:
            if os.path.exists(policy_path):
                os.remove(policy_path)


class TestHeadlessApplyPolicySchema(unittest.TestCase):
    def test_public_review_is_rejected_before_any_write_with_safe_diagnostic(self):
        policy = {"version": 2, "repository": "acme/project", "stages": [
            {"name": "features", "enable": ["issues"]}, review_stage()]}
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            api = InventoryApi()
            api.repo["visibility"] = "public"
            stderr = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), redirect_stderr(stderr):
                result = apply_with_plan(str(policy_path), token="mock-token")
        self.assertEqual(result, 2)
        self.assertEqual(api.writes, [])

    def test_invalid_policy_schema_exits_1_with_error_on_stderr(self):
        """Invalid policy schema (failed validate_policy) exits 1 with error on stderr."""
        invalid_policies = [
            {"version": 1},  # missing stages
            {"version": 3, "stages": [{"name": "actions"}]},  # invalid version
            {"version": 1, "stages": []},  # empty stages
            {"version": 1, "stages": [{"name": "unknown_stage"}]},  # unknown stage
            {"version": 1, "stages": [{"name": "actions"}], "unknown_field": True},  # extra key
            {"version": 1, "stages": [{"name": "features", "enable": ["invalid_feature"]}]},  # invalid enable
        ]
        for idx, bad_policy in enumerate(invalid_policies):
            with tempfile.NamedTemporaryFile("w", suffix=f"_{idx}.json", delete=False) as f:
                f.write(json.dumps(bad_policy))
                f.flush()
                policy_path = f.name

            try:
                stderr = io.StringIO()
                with patch.dict(os.environ, {"GITHUB_TOKEN": "mock-token"}), redirect_stderr(stderr):
                    exit_code = run_headless_apply(policy_path, repo="octocat/Hello-World")

                self.assertEqual(exit_code, 1, f"Expected exit code 1 for invalid policy: {bad_policy}")
                err_output = stderr.getvalue()
                self.assertIn("Error:", err_output)
            finally:
                if os.path.exists(policy_path):
                    os.remove(policy_path)


class TestHeadlessApplyRepositoryResolution(unittest.TestCase):
    def test_missing_repository_exits_1_with_error_on_stderr(self):
        """Missing repository (neither --repo nor policy['repository']) exits 1."""
        policy_without_repo = {"version": 1, "stages": [{"name": "actions"}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps(policy_without_repo))
            f.flush()
            policy_path = f.name

        try:
            stderr = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_TOKEN": "mock-token"}), redirect_stderr(stderr):
                exit_code = run_headless_apply(policy_path, repo=None)

            self.assertEqual(exit_code, 1)
            err_output = stderr.getvalue()
            self.assertIn("Error:", err_output)
            self.assertIn("repository", err_output.lower())
        finally:
            if os.path.exists(policy_path):
                os.remove(policy_path)

    def test_invalid_repository_format_exits_1_with_error_on_stderr(self):
        """Invalid repository format provided via repo argument exits 1."""
        policy = {"version": 1, "stages": [{"name": "actions"}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps(policy))
            f.flush()
            policy_path = f.name

        try:
            stderr = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_TOKEN": "mock-token"}), redirect_stderr(stderr):
                exit_code = run_headless_apply(policy_path, repo="invalid-not-owner-repo")

            self.assertEqual(exit_code, 1)
            err_output = stderr.getvalue()
            self.assertIn("Error:", err_output)
            self.assertIn("repository", err_output.lower())
        finally:
            if os.path.exists(policy_path):
                os.remove(policy_path)


from test_setup_github_repo import InventoryApi


class TestHeadlessApplySuccess(unittest.TestCase):
    def test_successful_apply_with_repo_flag_prints_json_and_exits_0(self):
        """Successful apply with --repo executes run_policy, prints formatted JSON, exits 0."""
        policy = {"version": 1, "stages": [{"name": "actions"}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps(policy))
            f.flush()
            policy_path = f.name

        try:
            stdout = io.StringIO()
            stderr = io.StringIO()
            api = InventoryApi()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "mock-token"}), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = apply_with_plan(policy_path, repo="acme/project")

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            output_data = json.loads(stdout.getvalue())
            self.assertEqual(output_data["mode"], "apply")
            self.assertEqual(output_data["repository"], "acme/project")
            self.assertTrue(len(output_data["changes"]) > 0)
            self.assertTrue(all(c["status"] == "verified" for c in output_data["changes"]))
        finally:
            if os.path.exists(policy_path):
                os.remove(policy_path)

    def test_successful_apply_with_policy_repository_prints_json_and_exits_0(self):
        """Successful apply with policy['repository'] executes run_policy, prints formatted JSON, exits 0."""
        policy = {
            "version": 1,
            "repository": "acme/project",
            "stages": [{"name": "actions"}],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps(policy))
            f.flush()
            policy_path = f.name

        try:
            stdout = io.StringIO()
            stderr = io.StringIO()
            api = InventoryApi()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "mock-token"}), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = apply_with_plan(policy_path, repo=None)

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            output_data = json.loads(stdout.getvalue())
            self.assertEqual(output_data["mode"], "apply")
            self.assertEqual(output_data["repository"], "acme/project")
        finally:
            if os.path.exists(policy_path):
                os.remove(policy_path)

    def test_repo_flag_overrides_policy_repository(self):
        """CLI --repo overrides policy['repository']."""
        policy = {
            "version": 1,
            "repository": "other/ignore-me",
            "stages": [{"name": "actions"}],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps(policy))
            f.flush()
            policy_path = f.name

        try:
            stdout = io.StringIO()
            api = InventoryApi()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "mock-token"}), \
                 redirect_stdout(stdout):
                exit_code = apply_with_plan(policy_path, repo="acme/project")

            self.assertEqual(exit_code, 0)
            output_data = json.loads(stdout.getvalue())
            self.assertEqual(output_data["repository"], "acme/project")
        finally:
            if os.path.exists(policy_path):
                os.remove(policy_path)


class TestHeadlessApplyBlockedAndUnverified(unittest.TestCase):
    def test_blocked_change_status_prints_json_and_exits_2(self):
        """Blocked change status in run_policy result prints JSON and exits 2."""
        class BlockedApi(InventoryApi):
            def request(self, method, path, payload=None):
                if method == "GET" and path == "/repos/acme/project/actions/permissions":
                    raise ApiError("HTTP 403 Forbidden")
                return super().request(method, path, payload)

        policy = {"version": 1, "stages": [{"name": "actions"}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps(policy))
            f.flush()
            policy_path = f.name

        try:
            stdout = io.StringIO()
            stderr = io.StringIO()
            api = BlockedApi()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "mock-token"}), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = apply_with_plan(policy_path, repo="acme/project")

            self.assertEqual(exit_code, 2)
            output_data = json.loads(stdout.getvalue())
            self.assertTrue(any(c["status"] == "blocked" for c in output_data["changes"]))
        finally:
            if os.path.exists(policy_path):
                os.remove(policy_path)

    def test_unverified_change_status_prints_json_and_exits_2(self):
        """Unverified change status in run_policy result prints JSON and exits 2."""
        class IgnoringApi(InventoryApi):
            def request(self, method, path, payload=None):
                if method == "PUT" and path == "/repos/acme/project/actions/permissions":
                    self.writes.append((method, path, payload))
                    # Ignore the write; don't update self.actions
                    return 200, {}
                return super().request(method, path, payload)

        policy = {"version": 1, "stages": [{"name": "actions"}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps(policy))
            f.flush()
            policy_path = f.name

        try:
            stdout = io.StringIO()
            stderr = io.StringIO()
            api = IgnoringApi()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "mock-token"}), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = apply_with_plan(policy_path, repo="acme/project")

            self.assertEqual(exit_code, 2)
            output_data = json.loads(stdout.getvalue())
            self.assertTrue(any(c["status"] == "unverified" for c in output_data["changes"]))
        finally:
            if os.path.exists(policy_path):
                os.remove(policy_path)


class TestHeadlessApplyApiError(unittest.TestCase):
    def test_api_error_during_apply_reports_failed_operation_and_recovery(self):
        """A write failure preserves the computed Dry-Run and reports recovery."""
        class FailingWriteApi(InventoryApi):
            def request(self, method, path, payload=None):
                if method == "PUT" and path == "/repos/acme/project/actions/permissions":
                    raise ApiError("HTTP 500 Internal Server Error")
                return super().request(method, path, payload)

        policy = {"version": 1, "stages": [{"name": "actions"}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps(policy))
            f.flush()
            policy_path = f.name

        try:
            stdout = io.StringIO()
            stderr = io.StringIO()
            api = FailingWriteApi()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "mock-token"}), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = apply_with_plan(policy_path, repo="acme/project")

            self.assertEqual(exit_code, 2)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["changes"][0]["status"], "failed")
            self.assertIn("HTTP 500", result["changes"][0]["reason"])
            self.assertIn("recovery", result)
            self.assertTrue(result["changes"])
        finally:
            if os.path.exists(policy_path):
                os.remove(policy_path)


class TestHeadlessApplyFilesStage(unittest.TestCase):
    def test_files_stage_relative_source_paths_resolved(self):
        """Relative file paths in files stages are resolved relative to policy file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            content_file = tmpdir_path / "custom-template.md"
            content_file.write_text("template content", encoding="utf-8")

            policy = {
                "version": 1,
                "repository": "acme/project",
                "stages": [
                    {
                        "name": "files",
                        "items": [
                            {
                                "path": ".github/template.md",
                                "source": "custom-template.md",
                            }
                        ],
                    }
                ],
            }
            policy_file = tmpdir_path / "policy.json"
            policy_file.write_text(json.dumps(policy), encoding="utf-8")

            resolved_sources = []

            def fake_run_policy(api, repository, resolved_policy, apply=False,
                                accepted_dry_run=None, accepted_plan=None, **kwargs):
                for stage in resolved_policy.get("stages", []):
                    if stage.get("name") == "files":
                        for item in stage.get("items", []):
                            resolved_sources.append(item.get("source"))
                return {"mode": "apply" if apply else "dry-run", "repository": repository, "changes": []}

            with patch("scripts.headless.GitHubApi"), patch("scripts.headless.inventory", return_value={}), \
                 patch("scripts.headless.run_policy", side_effect=fake_run_policy), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "mock-token"}), \
                 redirect_stdout(io.StringIO()):
                exit_code = apply_with_plan(str(policy_file))

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(resolved_sources), 2)
            self.assertEqual(resolved_sources[0], str(content_file.resolve()))


SCIFLOW_CICD = ROOT_DIR / "sciflow-cicd"


class TestHeadlessApplyCliSubprocess(unittest.TestCase):
    def test_cli_subprocess_missing_token_exits_1(self):
        """sciflow-cicd apply without GITHUB_TOKEN exits 1 with error on stderr."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps({"version": 1, "repository": "acme/repo", "stages": [{"name": "actions"}]}))
            f.flush()
            policy_path = f.name

        try:
            env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
            proc = subprocess.run(
                [str(SCIFLOW_CICD), "apply", "--policy", policy_path],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("error:", proc.stderr.lower())
            self.assertIn("github_token", proc.stderr.lower())
        finally:
            if os.path.exists(policy_path):
                os.remove(policy_path)

    def test_cli_subprocess_nonexistent_policy_exits_1(self):
        """sciflow-cicd apply with nonexistent policy file exits 1 with error on stderr."""
        proc = subprocess.run(
            [str(SCIFLOW_CICD), "apply", "--policy", "nonexistent_cli_policy.json"],
            capture_output=True,
            text=True,
            env=dict(os.environ, GITHUB_TOKEN="mock-token"),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("error:", proc.stderr.lower())
        self.assertIn("nonexistent_cli_policy.json", proc.stderr.lower())

    def test_cli_subprocess_missing_repository_exits_1(self):
        """sciflow-cicd apply with missing repository exits 1 with error on stderr."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps({"version": 1, "stages": [{"name": "actions"}]}))
            f.flush()
            policy_path = f.name

        try:
            proc = subprocess.run(
                [str(SCIFLOW_CICD), "apply", "--policy", policy_path],
                capture_output=True,
                text=True,
                env=dict(os.environ, GITHUB_TOKEN="mock-token"),
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("error:", proc.stderr.lower())
            self.assertIn("repository", proc.stderr.lower())
        finally:
            if os.path.exists(policy_path):
                os.remove(policy_path)

    def test_cli_subprocess_invalid_schema_exits_1(self):
        """sciflow-cicd apply with invalid schema exits 1 with error on stderr."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps({"version": 99, "stages": []}))
            f.flush()
            policy_path = f.name

        try:
            proc = subprocess.run(
                [str(SCIFLOW_CICD), "apply", "--policy", policy_path, "--repo", "acme/project"],
                capture_output=True,
                text=True,
                env=dict(os.environ, GITHUB_TOKEN="mock-token"),
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("error:", proc.stderr.lower())
        finally:
            if os.path.exists(policy_path):
                os.remove(policy_path)


class _PublicWorkflowApi(InventoryApi):
    """InventoryApi for a public repository that supports workflow staging and apply."""

    def __init__(self):
        super().__init__()
        self.repo["visibility"] = "public"
        self.refs = {"master": "1" * 40}
        self.files = {}
        self.pulls = []

    def request(self, method, path, payload=None):
        base = "/repos/acme/project"
        if method == "GET" and "/git/commits/" in path:
            return 200, {"sha": path.rsplit("/", 1)[-1]}
        if method == "GET" and path.startswith(base + "/git/ref/heads/"):
            branch = unquote(path.removeprefix(base + "/git/ref/heads/"))
            if branch in self.refs:
                return 200, {"object": {"sha": self.refs[branch]}}
            return 404, None
        if method == "POST" and path == base + "/git/refs":
            branch = payload["ref"].removeprefix("refs/heads/")
            self.refs[branch] = payload["sha"]
            return 201, {"ref": payload["ref"]}
        if method == "GET" and path.startswith(base + "/compare/"):
            branch = unquote(path.split("...", 1)[1])
            files = [
                {"filename": fn, "status": "added"}
                for (ref, fn) in self.files
                if ref == branch
            ]
            return 200, {"files": files, "commits": []}
        if path.startswith(base + "/contents/"):
            parts = path.split("?ref=")
            filename = unquote(parts[0].removeprefix(base + "/contents/"))
            branch = unquote(parts[1]) if len(parts) > 1 else (payload or {}).get("branch", "master")
            if method == "GET":
                entry = self.files.get((branch, filename))
                if entry is None:
                    return 404, None
                return 200, entry
            if method == "PUT":
                self.writes.append((method, path, payload))
                self.files[(branch, filename)] = {
                    "type": "file",
                    "encoding": "base64",
                    "content": payload["content"],
                    "sha": "2" * 40,
                }
                return 201, {"content": {"sha": "2" * 40}}
        if method == "GET" and path.startswith(base + "/pulls/"):
            num = int(path.removeprefix(base + "/pulls/"))
            for pr in self.pulls:
                if pr["number"] == num:
                    return 200, pr
            return 404, None
        if method == "GET" and path.startswith(base + "/pulls"):
            return 200, self.pulls
        if method == "POST" and path == base + "/pulls":
            self.writes.append((method, path, payload))
            pr = {
                "number": len(self.pulls) + 1,
                "html_url": f"https://github.com/acme/project/pull/{len(self.pulls) + 1}",
                "draft": bool(payload.get("draft")),
                "head": {"ref": payload["head"]},
                "base": {"ref": payload["base"]},
            }
            self.pulls.append(pr)
            return 201, pr
        return super().request(method, path, payload)


class TestHeadlessApplyPublicSelfHostedForkGuard(unittest.TestCase):
    """Regression tests for Issue #69: public self-hosted pull_request fork_guard mutation."""

    @staticmethod
    def _public_self_hosted_policy():
        return {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {
                    "name": "workflows",
                    "workflows": [
                        {
                            "template": "pr-ci",
                            "template_version": 1,
                            "filename": "ci.yml",
                            "triggers": ["pull_request"],
                            "checkout_sha": "11bd71901bbe5b1630ceea73d27597364c9af683",
                            "commands": ["pytest"],
                            "runs_on": "self-hosted",
                        }
                    ],
                }
            ],
        }

    def test_run_policy_does_not_mutate_input_policy_on_public_self_hosted_pr(self):
        """run_policy must not mutate the caller's policy dictionary in-place."""
        policy = self._public_self_hosted_policy()
        original_policy = copy.deepcopy(policy)
        api = _PublicWorkflowApi()

        result = run_policy(api, "acme/project", policy, apply=False)

        self.assertEqual(policy, original_policy)
        self.assertNotIn("fork_guard", policy["stages"][0]["workflows"][0])
        self.assertEqual(result["policy_sha256"], compute_policy_sha256(original_policy))

    def test_headless_dry_run_then_apply_succeeds_with_matching_policy_sha256(self):
        """run_headless_apply(apply=False) followed by apply=True succeeds with matching policy_sha256."""
        policy = self._public_self_hosted_policy()
        expected_sha = compute_policy_sha256(policy)
        api = _PublicWorkflowApi()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            policy_path = tmpdir_path / "policy.json"
            plan_path = tmpdir_path / "dry-run.json"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")

            dry_run_stdout = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "mock-token"}), \
                 redirect_stdout(dry_run_stdout):
                dry_run_exit = run_headless_apply(str(policy_path), apply=False)

            self.assertEqual(dry_run_exit, 0)
            dry_run_data = json.loads(dry_run_stdout.getvalue())
            self.assertEqual(dry_run_data["policy_sha256"], expected_sha)
            plan_path.write_text(dry_run_stdout.getvalue(), encoding="utf-8")

            apply_stdout = io.StringIO()
            apply_stderr = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "mock-token"}), \
                 redirect_stdout(apply_stdout), redirect_stderr(apply_stderr):
                apply_exit = run_headless_apply(
                    str(policy_path), apply=True, dry_run_path=str(plan_path)
                )

            self.assertEqual(apply_exit, 0, msg=f"stderr={apply_stderr.getvalue()} stdout={apply_stdout.getvalue()}")
            apply_data = json.loads(apply_stdout.getvalue())
            self.assertEqual(apply_data["mode"], "apply")
            self.assertTrue(apply_data["changes"])
            self.assertTrue(
                all(c["status"] in ("verified", "staged", "applied", "pending") for c in apply_data["changes"]),
                msg=f"unexpected statuses: {apply_data['changes']}",
            )

    def test_interactive_setup_policy_and_dry_run_apply_in_headless_mode(self):
        """Saving Policy and Dry-Run from Interactive Setup on a public repo applies cleanly in Headless Mode."""
        policy = self._public_self_hosted_policy()
        saved_policy = copy.deepcopy(policy)
        api = _PublicWorkflowApi()

        dry_run_result = execute_dry_run(api, "acme/project", policy)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            policy_path = tmpdir_path / "policy.json"
            plan_path = tmpdir_path / "dry-run.json"
            policy_path.write_text(json.dumps(saved_policy), encoding="utf-8")
            plan_path.write_text(json.dumps(dry_run_result), encoding="utf-8")

            apply_stdout = io.StringIO()
            apply_stderr = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "mock-token"}), \
                 redirect_stdout(apply_stdout), redirect_stderr(apply_stderr):
                apply_exit = run_headless_apply(
                    str(policy_path), apply=True, dry_run_path=str(plan_path)
                )

            self.assertEqual(apply_exit, 0, msg=f"stderr={apply_stderr.getvalue()} stdout={apply_stdout.getvalue()}")
            apply_data = json.loads(apply_stdout.getvalue())
            self.assertEqual(apply_data["mode"], "apply")
            self.assertTrue(apply_data["changes"])
            self.assertTrue(
                all(c["status"] in ("verified", "staged", "applied", "pending") for c in apply_data["changes"]),
                msg=f"unexpected statuses: {apply_data['changes']}",
            )


if __name__ == "__main__":
    unittest.main()
