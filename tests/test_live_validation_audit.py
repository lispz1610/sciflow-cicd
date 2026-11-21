"""Tests verifying disposable repository audit contracts and critical flows (Ticket #21)."""

import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

CFG_SCRIPT = SCRIPTS_DIR / "configure_github_repo.py"
SETUP_SCRIPT = SCRIPTS_DIR / "setup_github_repo.py"

cfg_spec = importlib.util.spec_from_file_location("configure_github_repo", CFG_SCRIPT)
configurator = importlib.util.module_from_spec(cfg_spec)
cfg_spec.loader.exec_module(configurator)

setup_spec = importlib.util.spec_from_file_location("setup_github_repo", SETUP_SCRIPT)
setup_mod = importlib.util.module_from_spec(setup_spec)
setup_spec.loader.exec_module(setup_mod)


class LiveValidationAuditContractTest(unittest.TestCase):
    """Contract verification for flows audited on the disposable repository."""

    def test_inventory_contains_all_13_audited_sections(self):
        class ReadOnlyApi:
            def request(self, method, path, payload=None):
                if method != "GET":
                    raise AssertionError(f"Write attempted during inventory: {method} {path}")
                if path == "/repos/acme/project":
                    return 200, {"full_name": "acme/project", "visibility": "private", "default_branch": "main"}
                if path == "/repos/acme/project/actions/permissions":
                    return 200, {"enabled": True, "allowed_actions": "all", "sha_pinning_required": False}
                if path == "/repos/acme/project/actions/permissions/workflow":
                    return 200, {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}
                if path.endswith("/rulesets"):
                    return 200, []
                return 404, None

        api = ReadOnlyApi()
        inv = setup_mod.inventory(api, "acme/project")
        expected_keys = {
            "repository", "features", "wiki_setup", "merge", "actions",
            "workflow_permissions", "security_and_analysis", "vulnerability_alerts",
            "automated_security_fixes", "private_vulnerability_reporting", "codeql",
            "dependency_graph", "agent_skills", "rulesets", "capabilities"
        }
        self.assertEqual(set(inv.keys()), expected_keys)
        self.assertEqual(inv["repository"]["full_name"], "acme/project")

    def test_dry_run_with_expected_sha_plans_update_with_zero_writes(self):
        import base64
        legacy_bytes = b"# sciflow-cicd workflow-template=pr-ci version=1\nname: \"PR CI\"\non:\n  pull_request:\n"
        legacy_b64 = base64.b64encode(legacy_bytes).decode("ascii")

        policy = {
            "version": 2,
            "stages": [
                {
                    "name": "workflows",
                    "workflows": [
                        {
                            "template": "pr-ci",
                            "template_version": 1,
                            "filename": "ci.yml",
                            "triggers": ["pull_request"],
                            "checkout_sha": setup_mod.DEFAULT_CHECKOUT_SHA,
                            "commands": ["pytest"],
                            "expected_sha": "36759ab14ff5bbb8bfab606beff9a5475da29de4",
                        }
                    ],
                }
            ],
        }

        class MockApi:
            def __init__(self):
                self.writes = []

            def request(self, method, path, payload=None):
                if method != "GET":
                    self.writes.append((method, path, payload))
                if path == "/repos/acme/project":
                    return 200, {"default_branch": "main", "visibility": "private"}
                if path == "/repos/acme/project/git/ref/heads/main":
                    return 200, {"object": {"sha": "1" * 40}}
                if path.startswith("/repos/acme/project/contents/.github/workflows/ci.yml"):
                    return 200, {"type": "file", "encoding": "base64", "content": legacy_b64, "sha": "36759ab14ff5bbb8bfab606beff9a5475da29de4"}
                if path == f"/repos/actions/checkout/git/commits/{setup_mod.DEFAULT_CHECKOUT_SHA}":
                    return 200, {"sha": setup_mod.DEFAULT_CHECKOUT_SHA}
                if path == "/repos/acme/project/actions/permissions":
                    return 200, {"enabled": True, "allowed_actions": "all"}
                if path.startswith("/repos/acme/project/pulls"):
                    return 200, []
                return 404, None

        api = MockApi()
        preview = setup_mod.run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(preview["mode"], "dry-run")
        changes = {item["path"]: item for item in preview["changes"]}
        self.assertEqual(set(changes), {".github/workflows/ci.yml"})
        workflow = changes[".github/workflows/ci.yml"]
        self.assertEqual(workflow["operation"], "update")
        self.assertEqual(workflow["current_sha"], "36759ab14ff5bbb8bfab606beff9a5475da29de4")
        self.assertTrue(all(item["status"] == "planned" for item in changes.values()))
        self.assertEqual(api.writes, [])

    def test_reapplication_convergence_yields_zero_changes(self):
        policy = {
            "version": 1,
            "stages": [
                {
                    "name": "merge",
                    "settings": {
                        "allow_update_branch": True,
                    },
                }
            ],
        }

        class ConvergedApi:
            def __init__(self):
                self.writes = []

            def request(self, method, path, payload=None):
                if method != "GET":
                    self.writes.append((method, path, payload))
                if path == "/repos/acme/project":
                    return 200, {
                        "default_branch": "main",
                        "visibility": "private",
                        "allow_merge_commit": True,
                        "allow_squash_merge": True,
                        "allow_rebase_merge": True,
                        "delete_branch_on_merge": False,
                        "allow_update_branch": True,
                    }
                return 404, None

        api = ConvergedApi()
        dry_run = setup_mod.run_policy(api, "acme/project", policy, apply=False)
        result = setup_mod.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(result["mode"], "apply")
        self.assertEqual(result["changes"], [])
        self.assertEqual(api.writes, [])

    def test_recoverable_failure_on_missing_workflow_scope_retains_recovery_guidance(self):
        policy = {
            "version": 2,
            "stages": [
                {
                    "name": "workflows",
                    "workflows": [
                        {
                            "template": "pr-ci",
                            "template_version": 1,
                            "filename": "ci.yml",
                            "triggers": ["pull_request"],
                            "checkout_sha": setup_mod.DEFAULT_CHECKOUT_SHA,
                            "commands": ["pytest"],
                        }
                    ],
                }
            ],
        }

        class WorkflowScopeMissingApi:
            def request(self, method, path, payload=None):
                if path == "/repos/acme/project":
                    return 200, {"default_branch": "main", "visibility": "private"}
                if path == f"/repos/actions/checkout/git/commits/{setup_mod.DEFAULT_CHECKOUT_SHA}":
                    return 200, {"sha": setup_mod.DEFAULT_CHECKOUT_SHA}
                if path == "/repos/acme/project/git/ref/heads/main":
                    return 200, {"object": {"sha": "1" * 40}}
                if path.startswith("/repos/acme/project/contents/.github/workflows/ci.yml"):
                    if method == "PUT":
                        # 404 returned by GitHub when token lacks workflow scope
                        return 404, {"message": "Not Found"}
                    return 404, None
                if path == "/repos/acme/project/git/refs":
                    return 201, {"ref": "refs/heads/codex/workflows-test"}
                if path.startswith("/repos/acme/project/git/ref/heads/codex/workflows-"):
                    return 404, None
                if path == "/repos/acme/project/actions/permissions":
                    return 200, {"enabled": True, "allowed_actions": "all"}
                if path.startswith("/repos/acme/project/actions/"):
                    return 200, {"name": "present"}
                if path.startswith("/repos/acme/project/pulls"):
                    return 200, []
                return 404, None

        api = WorkflowScopeMissingApi()
        with tempfile.TemporaryDirectory() as directory:
            policy_path = pathlib.Path(directory) / "policy.json"
            dry_run_path = pathlib.Path(directory) / "dry-run.json"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            dry_run_output = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                    patch.dict(os.environ, {"GITHUB_TOKEN": "test-only"}), \
                    redirect_stdout(dry_run_output):
                self.assertEqual(setup_mod.main([
                    "--repo", "acme/project", "policy", "--file", str(policy_path)
                ]), 0)
            dry_run_path.write_text(dry_run_output.getvalue(), encoding="utf-8")
            output = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                    patch.dict(os.environ, {"GITHUB_TOKEN": "test-only"}), \
                    redirect_stdout(output):
                exit_code = setup_mod.main([
                    "--repo", "acme/project", "policy", "--file", str(policy_path),
                    "--apply", "--dry-run-result", str(dry_run_path)
                ])
        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(result["mode"], "apply")
        self.assertIn("recovery", result)
        self.assertIn("Inspect the workflow staging branch", result["recovery"])
        self.assertEqual(result["changes"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
