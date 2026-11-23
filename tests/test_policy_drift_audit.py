"""Simulation tests for policy drift reporting."""

import json
import os
from pathlib import Path
import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from api_test_support import IssuesApi as FakeApi
from scripts.policy_drift_audit import report_drift
from scripts.configure_github_repo import ApiError, main as configure_main, run_policy_dry_run


class PolicyDriftAuditTests(unittest.TestCase):
    def test_clean_policy_creates_no_issue(self):
        api = FakeApi()

        result = report_drift(api, "acme/project", [])

        self.assertEqual(result, {"status": "clean"})
        self.assertEqual(api.requests, [])

    def test_converged_inspect_and_unchanged_records_create_no_issue(self):
        api = FakeApi()
        changes = [
            {
                "stage": "issue-state-gate",
                "scope": "acme/project",
                "operation": "inspect",
                "status": "planned",
                "reason": "Read and verify the active Issue State Gate rule without changing repository protections.",
            },
            {
                "stage": "settings",
                "operation": "PATCH",
                "path": "/repos/acme/project",
                "status": "unchanged",
            },
        ]

        result = report_drift(api, "acme/project", changes)

        self.assertEqual(result, {"status": "clean"})
        self.assertEqual(api.requests, [])

    def test_inspect_with_drift_creates_issue_for_drift_only(self):
        api = FakeApi()
        drift_item = {"path": "/repos/acme/project", "desired": {"has_issues": True}}
        changes = [
            {
                "stage": "issue-state-gate",
                "scope": "acme/project",
                "operation": "inspect",
                "status": "planned",
            },
            drift_item,
        ]

        result = report_drift(api, "acme/project", changes)

        self.assertEqual(result["status"], "created")
        method, path, payload = api.requests[-1]
        self.assertEqual((method, path), ("POST", "/repos/acme/project/issues"))
        self.assertEqual(payload["body"], json.dumps([drift_item], indent=2) + "\n")

    def test_drift_creates_triage_issue_with_only_exact_diff(self):
        api = FakeApi()
        diff = [{"path": "/repos/acme/project", "desired": {"has_issues": True}}]

        result = report_drift(api, "acme/project", diff)

        self.assertEqual(result["status"], "created")
        method, path, payload = api.requests[-1]
        self.assertEqual((method, path), ("POST", "/repos/acme/project/issues"))
        self.assertEqual(payload["title"], "Policy Drift Detected:")
        self.assertEqual(payload["body"], json.dumps(diff, indent=2) + "\n")
        self.assertEqual(payload["labels"], ["needs-triage"])

    def test_open_matching_issue_suppresses_duplicate(self):
        title = "Policy Drift Detected: weekly audit"
        api = FakeApi([{"title": title, "state": "open"}])

        result = report_drift(api, "acme/project", [{"path": "drift"}])

        self.assertEqual(result, {"status": "duplicate", "title": "Policy Drift Detected:"})
        self.assertEqual([request[0] for request in api.requests], ["GET"])

    def test_matching_pull_request_title_does_not_suppress_issue(self):
        api = FakeApi([{"title": "Policy Drift Detected: PR", "state": "open",
                        "pull_request": {"url": "https://api.github.com/repos/acme/project/pulls/1"}}])

        result = report_drift(api, "acme/project", [{"path": "drift"}])

        self.assertEqual(result["status"], "created")
        self.assertEqual([request[0] for request in api.requests], ["GET", "POST"])

    def test_policy_audit_without_token_names_required_credential_and_permissions(self):
        stderr = io.StringIO()
        args = ["--repo", "acme/project", "--policy", "examples/setup-policy.json",
                "--token-env", "POLICY_AUDIT_TOKEN"]

        with patch.dict(os.environ, {}, clear=True), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                configure_main(args)

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("set POLICY_AUDIT_TOKEN", stderr.getvalue())
        self.assertIn("Administration (read)", stderr.getvalue())
        self.assertIn("Contents (write)", stderr.getvalue())

    def test_policy_audit_reports_missing_settings_read_permission(self):
        class ForbiddenApi:
            def request(self, method, path, payload=None):
                return 403, {"message": "Resource not accessible by integration"}

        policy = json.loads((Path(__file__).resolve().parents[1] / "examples/setup-policy.json")
                            .read_text(encoding="utf-8"))

        with self.assertRaisesRegex(ApiError, r"Administration: read.*Contents: write"):
            run_policy_dry_run(ForbiddenApi(), "acme/project", policy)


if __name__ == "__main__":
    unittest.main()
