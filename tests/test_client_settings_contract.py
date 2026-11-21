"""Public client behavior for retained repository settings."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.cli import main, run_interactive
from configure_github_repo import ApiError
from test_e2e_audit import CleanRepoApi
from test_setup_github_repo import InventoryApi


class ClientSettingsContractTests(unittest.TestCase):
    def run_client(self, api, command, stage):
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"
            policy_path.write_text(json.dumps({"version": 2, "repository": "acme/project",
                                               "stages": [stage]}), encoding="utf-8")
            output = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}), \
                 contextlib.redirect_stdout(output):
                args = [command, "--policy", str(policy_path)]
                if command == "apply":
                    preview = io.StringIO()
                    with contextlib.redirect_stdout(preview):
                        self.assertEqual(main(["dry-run", "--policy", str(policy_path)]), 0)
                    plan_path = Path(directory) / "dry-run.json"
                    plan_path.write_text(preview.getvalue())
                    args += ["--dry-run-result", str(plan_path)]
                code = main(args)
        return code, json.loads(output.getvalue())

    def test_security_settings_report_meaningful_states_and_apply_only_previewed_writes(self):
        api = InventoryApi()
        stage = {"name": "security", "enable": ["private-vulnerability-reporting", "secret-scanning"]}
        code, preview = self.run_client(api, "dry-run", stage)
        self.assertEqual(code, 0)
        self.assertEqual(api.writes, [])
        self.assertEqual({(item["path"], item["status"]) for item in preview["changes"]}, {
            ("/repos/acme/project/private-vulnerability-reporting", "planned"),
            ("/repos/acme/project", "planned"),
        })
        alert = next(item for item in preview["changes"] if item["path"].endswith("private-vulnerability-reporting"))
        self.assertEqual(alert["current"], {"enabled": False})
        self.assertEqual(alert["desired"], {"enabled": True})
        self.assertEqual(alert["reason"], "selected by policy")

        code, applied = self.run_client(api, "apply", stage)
        self.assertEqual(code, 0)
        self.assertEqual([(method, path) for method, path, _ in api.writes],
                         [(item["operation"], item["path"]) for item in preview["changes"]])
        self.assertEqual({item["status"] for item in applied["changes"]}, {"verified"})
        api.writes.clear()
        code, repeated = self.run_client(api, "apply", stage)
        self.assertEqual(code, 0)
        self.assertEqual(repeated["status"], "no-op")
        self.assertEqual(repeated["changes"], [])
        self.assertEqual(api.writes, [])

    def test_partial_setting_failure_reports_verified_failed_and_pending_then_retries_safely(self):
        class FailingWorkflowApi(InventoryApi):
            fail_workflow = True

            def request(self, method, path, payload=None):
                if method == "PUT" and path.endswith("/actions/permissions/workflow") and self.fail_workflow:
                    raise ApiError(f"PUT {path}: HTTP 403: Resource not accessible")
                return super().request(method, path, payload)

        api = FailingWorkflowApi()
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"
            policy_path.write_text(json.dumps({"version": 2, "repository": "acme/project",
                "stages": [{"name": "actions"},
                           {"name": "security", "enable": ["private-vulnerability-reporting"]}]}))

            def client(command):
                output = io.StringIO()
                with patch("scripts.headless.GitHubApi", return_value=api), \
                     patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}), \
                     contextlib.redirect_stdout(output):
                    args = [command, "--policy", str(policy_path)]
                    if command == "apply":
                        args += ["--plan", str(plan_path)]
                    code = main(args)
                return code, json.loads(output.getvalue())

            code, dry_run = client("dry-run")
            self.assertEqual(code, 0)
            self.assertEqual(api.writes, [])
            plan_path = Path(directory) / "dry-run.json"
            plan_path.write_text(json.dumps(dry_run))

            code, result = client("apply")
            self.assertEqual(code, 2)
            self.assertEqual([(item["path"], item["status"]) for item in result["changes"]], [
                ("/repos/acme/project/actions/permissions", "verified"),
                ("/repos/acme/project/actions/permissions/workflow", "failed"),
                ("/repos/acme/project/private-vulnerability-reporting", "pending"),
            ])
            self.assertIn("HTTP 403", result["changes"][1]["reason"])
            self.assertIn("new Dry-Run", result["recovery"])
            self.assertEqual([(method, path) for method, path, _ in api.writes],
                             [("PUT", "/repos/acme/project/actions/permissions")])

            api.fail_workflow = False
            api.writes.clear()
            code, refreshed = client("dry-run")
            self.assertEqual(code, 0)
            self.assertEqual([item["path"] for item in refreshed["changes"]], [
                "/repos/acme/project/actions/permissions/workflow",
                "/repos/acme/project/private-vulnerability-reporting",
            ])
            plan_path.write_text(json.dumps(refreshed))
            code, retried = client("apply")
            self.assertEqual(code, 0)
            self.assertEqual({item["status"] for item in retried["changes"]}, {"verified"})
            self.assertEqual([(method, path) for method, path, _ in api.writes], [
                ("PUT", "/repos/acme/project/actions/permissions/workflow"),
                ("PUT", "/repos/acme/project/private-vulnerability-reporting"),
            ])

    def test_partial_setting_failure_keeps_unreadable_completed_write_unverified(self):
        class UnreadableAfterWriteApi(InventoryApi):
            def request(self, method, path, payload=None):
                if method == "PUT" and path.endswith("/actions/permissions/workflow"):
                    raise ApiError(f"PUT {path}: HTTP 403: Resource not accessible")
                if method == "GET" and path.endswith("/actions/permissions") and self.writes:
                    raise ApiError(f"GET {path}: HTTP 403: Resource not accessible")
                return super().request(method, path, payload)

        api = UnreadableAfterWriteApi()
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"
            policy_path.write_text(json.dumps({"version": 2, "repository": "acme/project",
                                               "stages": [{"name": "actions"}]}))
            plan_path = Path(directory) / "dry-run.json"
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["dry-run", "--policy", str(policy_path)]), 0)
                plan_path.write_text(output.getvalue())
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["apply", "--policy", str(policy_path),
                                           "--plan", str(plan_path)]), 2)
            result = json.loads(output.getvalue())
            self.assertEqual([item["status"] for item in result["changes"]],
                             ["unverified", "failed"])
            self.assertIn("Readback unavailable", result["changes"][0]["reason"])
            self.assertEqual(len(api.writes), 1)
    def test_each_retained_setting_family_has_changed_and_no_op_paths(self):
        cases = [
            ({"name": "features", "enable": ["wiki"]}, lambda api: None),
            ({"name": "actions"}, lambda api: None),
            ({"name": "merge", "settings": {"delete_branch_on_merge": True}}, lambda api: None),
            ({"name": "actions-policy", "settings": {"sha_pinning_required": True}}, lambda api: None),
            ({"name": "codeql"}, lambda api: api.actions.update(enabled=True)),
        ]
        for stage, prepare in cases:
            with self.subTest(stage=stage["name"]):
                api = InventoryApi()
                prepare(api)
                code, preview = self.run_client(api, "dry-run", stage)
                self.assertEqual(code, 0)
                self.assertEqual(api.writes, [])
                self.assertTrue(preview["changes"])
                self.assertTrue(all({"current", "desired", "reason", "status"} <= set(item)
                                    for item in preview["changes"]))
                code, applied = self.run_client(api, "apply", stage)
                self.assertEqual(code, 0)
                self.assertEqual({item["status"] for item in applied["changes"]} - {"verified", "pending", "unverified"}, set())
                api.writes.clear()
                code, repeated = self.run_client(api, "apply", stage)
                self.assertEqual(code, 0)
                self.assertEqual(repeated["status"], "no-op")
                self.assertEqual(api.writes, [])

    def test_interactive_actions_and_security_selection_matches_headless_dry_run(self):
        api = CleanRepoApi()
        api.workflow = {"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True}
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"
            interactive_output = io.StringIO()
            with patch("scripts.interactive.GitHubApi", return_value=api), \
                 patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", policy_path), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}):
                code = run_interactive(
                    in_stream=io.StringIO("acme/project\n2\nc\nnone\nn\ny\n2\nn\nn\nn\nn\nn\n"),
                    out_stream=interactive_output,
                    err_stream=io.StringIO(),
                )
            self.assertEqual(code, 0, interactive_output.getvalue())
            self.assertEqual(api.writes, [])
            self.assertTrue(policy_path.exists(), interactive_output.getvalue())
            self.assertEqual(json.loads(policy_path.read_text())["stages"], [
                {"name": "actions"}, {"name": "security", "enable": ["secret-scanning"]},
            ])
            output = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}), \
                 contextlib.redirect_stdout(output):
                code = main(["dry-run", "--policy", str(policy_path)])
            self.assertEqual(code, 0, output.getvalue())
            for item in json.loads(output.getvalue())["changes"]:
                self.assertIn(f'{item["operation"]} {item["path"]}', interactive_output.getvalue())
            self.assertEqual(api.writes, [])


if __name__ == "__main__":
    unittest.main()
