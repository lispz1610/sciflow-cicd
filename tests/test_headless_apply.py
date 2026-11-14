"""Tests for headless policy apply integration."""

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


GH_SETUP = ROOT_DIR / "gh-setup"


class TestHeadlessApplyCliSubprocess(unittest.TestCase):
    def test_cli_subprocess_missing_token_exits_1(self):
        """gh-setup apply without GITHUB_TOKEN exits 1 with error on stderr."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps({"version": 1, "repository": "acme/repo", "stages": [{"name": "actions"}]}))
            f.flush()
            policy_path = f.name

        try:
            env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}
            proc = subprocess.run(
                [str(GH_SETUP), "apply", "--policy", policy_path],
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
        """gh-setup apply with nonexistent policy file exits 1 with error on stderr."""
        proc = subprocess.run(
            [str(GH_SETUP), "apply", "--policy", "nonexistent_cli_policy.json"],
            capture_output=True,
            text=True,
            env=dict(os.environ, GITHUB_TOKEN="mock-token"),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("error:", proc.stderr.lower())
        self.assertIn("nonexistent_cli_policy.json", proc.stderr.lower())

    def test_cli_subprocess_missing_repository_exits_1(self):
        """gh-setup apply with missing repository exits 1 with error on stderr."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps({"version": 1, "stages": [{"name": "actions"}]}))
            f.flush()
            policy_path = f.name

        try:
            proc = subprocess.run(
                [str(GH_SETUP), "apply", "--policy", policy_path],
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
        """gh-setup apply with invalid schema exits 1 with error on stderr."""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps({"version": 99, "stages": []}))
            f.flush()
            policy_path = f.name

        try:
            proc = subprocess.run(
                [str(GH_SETUP), "apply", "--policy", policy_path, "--repo", "acme/project"],
                capture_output=True,
                text=True,
                env=dict(os.environ, GITHUB_TOKEN="mock-token"),
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("error:", proc.stderr.lower())
        finally:
            if os.path.exists(policy_path):
                os.remove(policy_path)


if __name__ == "__main__":
    unittest.main()
