"""Public sciflow-cicd contract for one repository setting."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.cli import main, run_interactive
from scripts.setup_github_repo import main as direct_policy_main
from scripts.interactive import Session
from scripts.interactive_apply import run_interactive_apply
from test_e2e_audit import CleanRepoApi
from test_review_setup import review_stage


class TestClientCoreContract(unittest.TestCase):
    def test_direct_policy_requires_same_accepted_dry_run_as_headless_mode(self):
        api = CleanRepoApi()
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"
            dry_run_path = Path(directory) / "dry-run.json"
            policy_path.write_text(json.dumps({"version": 1, "repository": "acme/project",
                                               "stages": [{"name": "features", "enable": ["issues"]}]}))
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch("scripts.setup_github_repo.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(direct_policy_main(["--repo", "acme/project", "policy",
                                                         "--file", str(policy_path), "--apply"]), 1)
                self.assertEqual(api.writes, [])

                direct_output = io.StringIO()
                with contextlib.redirect_stdout(direct_output):
                    self.assertEqual(direct_policy_main(["--repo", "acme/project", "policy",
                                                         "--file", str(policy_path)]), 0)
                self.assertEqual(api.writes, [])
                headless_output = io.StringIO()
                with contextlib.redirect_stdout(headless_output):
                    self.assertEqual(main(["dry-run", "--policy", str(policy_path)]), 0)
                self.assertEqual(json.loads(direct_output.getvalue())["changes"],
                                 json.loads(headless_output.getvalue())["changes"])
                dry_run_path.write_text(direct_output.getvalue())

                applied_output = io.StringIO()
                with contextlib.redirect_stdout(applied_output):
                    self.assertEqual(direct_policy_main(["--repo", "acme/project", "policy",
                        "--file", str(policy_path), "--apply", "--dry-run-result", str(dry_run_path)]), 0)
                self.assertEqual(api.writes, [("PATCH", "/repos/acme/project", {"has_issues": True})])
                self.assertEqual(json.loads(applied_output.getvalue())["changes"][0]["status"], "verified")

    def test_direct_policy_rejects_a_stale_dry_run_before_writing(self):
        api = CleanRepoApi()
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"
            dry_run_path = Path(directory) / "dry-run.json"
            policy_path.write_text(json.dumps({"version": 1, "repository": "acme/project",
                                               "stages": [{"name": "features", "enable": ["issues"]}]}))
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}):
                dry_run_output = io.StringIO()
                with contextlib.redirect_stdout(dry_run_output):
                    self.assertEqual(direct_policy_main(["--repo", "acme/project", "policy",
                                                         "--file", str(policy_path)]), 0)
                dry_run_path.write_text(dry_run_output.getvalue())
                api.repo["has_issues"] = True
                apply_output = io.StringIO()
                with contextlib.redirect_stdout(apply_output):
                    self.assertEqual(direct_policy_main(["--repo", "acme/project", "policy",
                        "--file", str(policy_path), "--apply", "--dry-run-result", str(dry_run_path)]), 2)
                self.assertEqual(json.loads(apply_output.getvalue())["changes"][0]["status"], "blocked")
                self.assertEqual(api.writes, [])

    def test_apply_requires_reviewed_dry_run_and_rejects_stale_state(self):
        api = CleanRepoApi()
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"
            plan_path = Path(directory) / "dry-run.json"
            policy_path.write_text(json.dumps({"version": 2, "repository": "acme/project",
                                               "stages": [{"name": "features", "enable": ["issues"]}]}))
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["apply", "--policy", str(policy_path)]), 1)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["dry-run", "--policy", str(policy_path)]), 0)
                plan_path.write_text(output.getvalue())
                api.repo["has_issues"] = True
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["apply", "--policy", str(policy_path),
                                           "--plan", str(plan_path)]), 2)
                self.assertEqual(json.loads(output.getvalue())["changes"][0]["status"], "blocked")
                self.assertEqual(api.writes, [])

    def test_conflicting_file_blocks_wiki_setting_before_any_write(self):
        class ConflictingFileApi(CleanRepoApi):
            def request(self, method, path, payload=None):
                if method == "GET" and "/contents/docs/guide.md" in path:
                    import base64
                    return 200, {"type": "file", "encoding": "base64",
                                 "content": base64.b64encode(b"owner text").decode(),
                                 "sha": "b" * 40}
                return super().request(method, path, payload)

        api = ConflictingFileApi()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "guide.md"
            source.write_text("selected text")
            policy_path = Path(directory) / "policy.json"
            policy_path.write_text(json.dumps({"version": 2, "repository": "acme/project",
                "stages": [{"name": "features", "enable": ["wiki"]},
                           {"name": "files", "items": [{"path": "docs/guide.md", "source": str(source)}]}]}))
            output = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(main(["dry-run", "--policy", str(policy_path)]), 2)
            plan_path = Path(directory) / "dry-run.json"
            plan_path.write_text(output.getvalue())
            output = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(main(["apply", "--policy", str(policy_path),
                                       "--plan", str(plan_path)]), 2)
            self.assertEqual(api.writes, [])

    def test_private_alerts_404_remains_unknown_and_cannot_be_enabled(self):
        class HiddenAlertsApi(CleanRepoApi):
            def request(self, method, path, payload=None):
                if method == "GET" and path.endswith("/vulnerability-alerts"):
                    return 404, None
                return super().request(method, path, payload)

        api = HiddenAlertsApi()
        api.repo["visibility"] = "private"
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"
            policy_path.write_text(json.dumps({
                "version": 2, "repository": "acme/project",
                "stages": [{"name": "security", "enable": ["vulnerability-alerts"]}],
            }))
            output = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}), \
                 contextlib.redirect_stdout(output):
                code = main(["dry-run", "--policy", str(policy_path)])
            self.assertEqual(code, 2)
            result = json.loads(output.getvalue())
            self.assertEqual(result["inventory"]["dependency_graph"]["status"], "unknown")
            self.assertEqual(result["changes"][0]["availability"], "unknown")
            self.assertEqual(result["changes"][0]["operation"], "inspect")
            self.assertEqual(api.writes, [])

    def test_permission_denial_is_identified_on_selected_operation(self):
        class DeniedApi(CleanRepoApi):
            def request(self, method, path, payload=None):
                if method == "GET" and path.endswith("/actions/permissions"):
                    from configure_github_repo import ApiError
                    raise ApiError(f"GET {path}: HTTP 403: Resource not accessible")
                return super().request(method, path, payload)

        api = DeniedApi()
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"
            policy_path.write_text(json.dumps({
                "version": 2, "repository": "acme/project",
                "stages": [{"name": "actions"}],
            }))
            output = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}), \
                 contextlib.redirect_stdout(output):
                code = main(["dry-run", "--policy", str(policy_path)])
            self.assertEqual(code, 2)
            result = json.loads(output.getvalue())
            self.assertEqual(result["inventory"]["actions"]["status"], "inaccessible")
            self.assertEqual(result["changes"][0]["availability"], "inaccessible")
            self.assertEqual(result["changes"][0]["path"], "/repos/acme/project/actions/permissions")
            self.assertEqual(api.writes, [])

    def test_malformed_read_blocks_selected_actions_and_unknown_policy_key_fails_early(self):
        class MalformedApi(CleanRepoApi):
            def request(self, method, path, payload=None):
                if method == "GET" and path.endswith("/actions/permissions"):
                    return 200, []
                return super().request(method, path, payload)

        api = MalformedApi()
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"
            policy_path.write_text(json.dumps({"version": 2, "repository": "acme/project",
                                               "stages": [{"name": "actions"}]}))
            output = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}), \
                 contextlib.redirect_stdout(output):
                code = main(["dry-run", "--policy", str(policy_path)])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["changes"][0]["availability"], "unknown")
            self.assertEqual(api.writes, [])

            policy_path.write_text(json.dumps({"version": 2, "repository": "acme/project",
                                               "stages": [{"name": "features", "enable": ["issues"],
                                                           "unrecognized": True}]}))
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}), \
                 contextlib.redirect_stderr(io.StringIO()):
                code = main(["apply", "--policy", str(policy_path)])
            self.assertEqual(code, 1)
            self.assertEqual(api.writes, [])

    def test_public_repository_reports_review_setup_as_unsupported(self):
        api = CleanRepoApi()
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"
            policy_path.write_text(json.dumps({"version": 2, "repository": "acme/project",
                                               "stages": [review_stage()]}))
            output = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}), \
                 contextlib.redirect_stdout(output):
                code = main(["dry-run", "--policy", str(policy_path)])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["changes"][0]["availability"], "unsupported")
            self.assertEqual(api.writes, [])

    def test_interactive_setup_stops_at_blocked_dry_run_without_apply_prompt(self):
        class DeniedApi(CleanRepoApi):
            def request(self, method, path, payload=None):
                if method == "GET" and path.endswith("/actions/permissions"):
                    from configure_github_repo import ApiError
                    raise ApiError(f"GET {path}: HTTP 403: Resource not accessible")
                return super().request(method, path, payload)

        api = DeniedApi()
        output = io.StringIO()
        session = Session("acme/project", api, in_stream=io.StringIO("y\n"),
                          out_stream=output, err_stream=io.StringIO())
        code = run_interactive_apply(session, {"version": 2,
                                                "stages": [{"name": "actions"}]})
        self.assertEqual(code, 2)
        self.assertIn("inaccessible", output.getvalue())
        self.assertNotIn("Apply these changes?", output.getvalue())
        self.assertEqual(api.writes, [])

    def test_interactive_and_headless_propose_the_same_issues_change(self):
        api = CleanRepoApi()
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "setup-policy.json"
            interactive_output = io.StringIO()
            with patch("scripts.interactive.GitHubApi", return_value=api), \
                 patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", policy_path), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}):
                code = run_interactive(
                    in_stream=io.StringIO("acme/project\nc\n1\nn\nn\nnone\nn\nn\nn\nn\nn\n"),
                    out_stream=interactive_output,
                    err_stream=io.StringIO(),
                )
            self.assertEqual(code, 0)
            self.assertIn("PATCH /repos/acme/project", interactive_output.getvalue())
            self.assertEqual(api.writes, [])
            self.assertEqual(json.loads(policy_path.read_text())["stages"],
                             [{"name": "features", "enable": ["issues"]}])

            headless_output = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}), \
                 contextlib.redirect_stdout(headless_output):
                code = main(["dry-run", "--policy", str(policy_path)])
            self.assertEqual(code, 0)
            self.assertEqual(api.writes, [])
            result = json.loads(headless_output.getvalue())
            self.assertEqual(result["inventory"]["features"]["issues"], False)
            self.assertEqual(result["changes"], [{
                "stage": "features", "operation": "PATCH", "path": "/repos/acme/project",
                "current": {"has_issues": False}, "desired": {"has_issues": True},
                "scope": "acme/project", "reason": "selected by policy", "status": "planned",
            }])

            apply_output = io.StringIO()
            plan_path = Path(directory) / "dry-run.json"
            plan_path.write_text(headless_output.getvalue())
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}), \
                 contextlib.redirect_stdout(apply_output):
                code = main(["apply", "--policy", str(policy_path), "--dry-run-result", str(plan_path)])
            self.assertEqual(code, 0)
            self.assertEqual(api.writes, [("PATCH", "/repos/acme/project", {"has_issues": True})])
            self.assertEqual(json.loads(apply_output.getvalue())["changes"][0]["status"], "verified")

            api.writes.clear()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}), \
                 contextlib.redirect_stdout(io.StringIO()) as refreshed:
                self.assertEqual(main(["dry-run", "--policy", str(policy_path)]), 0)
            plan_path.write_text(refreshed.getvalue())
            noop_output = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}), \
                 contextlib.redirect_stdout(noop_output):
                code = main(["apply", "--policy", str(policy_path), "--dry-run-result", str(plan_path)])
            self.assertEqual(code, 0)
            self.assertEqual(api.writes, [])
            self.assertEqual(json.loads(noop_output.getvalue())["changes"], [])

    def test_version_one_issues_policy_remains_supported(self):
        api = CleanRepoApi()
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "version-one.json"
            policy_path.write_text(json.dumps({
                "version": 1, "repository": "acme/project",
                "stages": [{"name": "features", "enable": ["issues"]}],
            }))
            output = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}), \
                 contextlib.redirect_stdout(output):
                code = main(["dry-run", "--policy", str(policy_path)])
            self.assertEqual(code, 0)
            self.assertEqual(api.writes, [])
            self.assertEqual(json.loads(output.getvalue())["changes"][0]["desired"],
                             {"has_issues": True})


if __name__ == "__main__":
    unittest.main()
