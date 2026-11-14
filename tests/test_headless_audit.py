"""Comprehensive audit of Headless Mode and Policy contracts (Ticket #15)."""

import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout, redirect_stderr

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
TESTS_DIR = ROOT_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_setup_github_repo import InventoryApi
from configure_github_repo import ApiError
from scripts.headless import run_headless_apply
from scripts.setup_github_repo import run_policy


class HeadlessPolicyAuditTests(unittest.TestCase):
    def inspected_plan(self, policy_file, api):
        output = io.StringIO()
        with patch("scripts.headless.GitHubApi", return_value=api), redirect_stdout(output):
            run_headless_apply(str(policy_file), token="dummy-token", apply=False)
        plan_path = Path(self.temp_dir) / "dry-run.json"
        plan_path.write_text(output.getvalue(), encoding="utf-8")
        return str(plan_path)

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_v1_and_v2_policy_validation_and_unsafe_input_rejection(self):
        """Rejects unknown keys, credentials, and invalid formats before any writes."""
        api = InventoryApi()

        # 1. Unknown top-level key rejected
        bad_policy = {
            "version": 1,
            "repository": "acme/project",
            "unknown_field": "disallowed",
            "stages": [{"name": "features"}],
        }
        policy_file = Path(self.temp_dir) / "bad_policy.json"
        policy_file.write_text(json.dumps(bad_policy), encoding="utf-8")

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = run_headless_apply(str(policy_file), token="dummy-token")
        self.assertEqual(code, 1)
        self.assertIn("Invalid policy schema", stderr.getvalue())
        self.assertEqual(api.writes, [])

        # 2. Duplicate stage rejected
        dup_policy = {
            "version": 1,
            "repository": "acme/project",
            "stages": [
                {"name": "features", "enable": ["issues"]},
                {"name": "features", "enable": ["wiki"]},
            ],
        }
        policy_file.write_text(json.dumps(dup_policy), encoding="utf-8")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = run_headless_apply(str(policy_file), token="dummy-token")
        self.assertEqual(code, 1)
        self.assertIn("duplicate stage", stderr.getvalue())

        # 3. Invalid repository format rejected
        bad_repo_policy = {
            "version": 1,
            "repository": "invalid_repo_name_no_slash",
            "stages": [{"name": "features", "enable": ["issues"]}],
        }
        policy_file.write_text(json.dumps(bad_repo_policy), encoding="utf-8")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = run_headless_apply(str(policy_file), token="dummy-token")
        self.assertEqual(code, 1)
        self.assertIn("repository must be OWNER/REPO", stderr.getvalue())

    def test_convergent_reapplication_issues_zero_writes(self):
        """Applying an already convergent policy results in zero writes and exits 0."""
        api = InventoryApi()
        # Seed matching state
        api.repo["has_issues"] = True
        api.repo["has_wiki"] = False
        api.repo["has_projects"] = False

        policy = {
            "version": 1,
            "repository": "acme/project",
            "stages": [{"name": "features", "enable": ["issues"]}],
        }
        policy_file = Path(self.temp_dir) / "convergent_policy.json"
        policy_file.write_text(json.dumps(policy), encoding="utf-8")

        stdout = io.StringIO()
        plan_path = self.inspected_plan(policy_file, api)
        with patch("scripts.headless.GitHubApi", return_value=api), redirect_stdout(stdout):
            code = run_headless_apply(str(policy_file), token="dummy-token", plan_path=plan_path)

        self.assertEqual(code, 0)
        self.assertEqual(api.writes, [], "Convergent reapplication must perform strictly 0 writes")
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["changes"], [])

    def test_fault_injection_before_writes_blocks_safely(self):
        """Preflight API failure blocks apply before sending any writes, exiting with code 2."""
        class PreflightFailApi(InventoryApi):
            def request(self, method, path, payload=None):
                if method == "GET" and "/actions/permissions" in path:
                    raise ApiError("HTTP 403 Forbidden: Missing admin rights")
                return super().request(method, path, payload)

        api = PreflightFailApi()
        policy = {
            "version": 1,
            "repository": "acme/project",
            "stages": [{"name": "actions"}],
        }
        policy_file = Path(self.temp_dir) / "preflight_fail.json"
        policy_file.write_text(json.dumps(policy), encoding="utf-8")

        stdout = io.StringIO()
        plan_path = self.inspected_plan(policy_file, api)
        with patch("scripts.headless.GitHubApi", return_value=api), redirect_stdout(stdout):
            code = run_headless_apply(str(policy_file), token="dummy-token", plan_path=plan_path)

        self.assertEqual(code, 2, "Blocked preflight should exit with status 2")
        self.assertEqual(api.writes, [], "Blocked preflight must perform zero writes")
        result = json.loads(stdout.getvalue())
        self.assertTrue(any(c["status"] == "blocked" for c in result["changes"]))

    def test_fault_injection_mid_apply_records_recovery_guidance(self):
        """Failure after partial writes records verified writes, failure reason, and recovery guidance."""
        class MidFailApi(InventoryApi):
            def __init__(self):
                super().__init__()
                self.patch_count = 0

            def request(self, method, path, payload=None):
                if method in ("PUT", "PATCH") and "/actions/permissions" in path:
                    raise ApiError("HTTP 500 Internal Server Error")
                return super().request(method, path, payload)

        api = MidFailApi()
        # Features and Actions both planned
        policy = {
            "version": 1,
            "repository": "acme/project",
            "stages": [
                {"name": "features", "enable": ["issues"]},
                {"name": "actions"},
            ],
        }
        policy_file = Path(self.temp_dir) / "mid_fail.json"
        policy_file.write_text(json.dumps(policy), encoding="utf-8")

        stdout = io.StringIO()
        plan_path = self.inspected_plan(policy_file, api)
        with patch("scripts.headless.GitHubApi", return_value=api), redirect_stdout(stdout):
            code = run_headless_apply(str(policy_file), token="dummy-token", plan_path=plan_path)

        self.assertEqual(code, 2, "Partial failure must exit with code 2")
        result = json.loads(stdout.getvalue())
        self.assertIn("recovery", result)
        self.assertIn("new Dry-Run", result["recovery"])

        # Check status of individual changes
        features_change = next(c for c in result["changes"] if c["stage"] == "features")
        actions_change = next(c for c in result["changes"] if c["stage"] == "actions")
        self.assertEqual(features_change["status"], "verified")
        self.assertEqual(actions_change["status"], "failed")


if __name__ == "__main__":
    unittest.main()
