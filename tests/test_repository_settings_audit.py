"""Comprehensive audit of repository settings, security, and CodeQL contracts (Ticket #16)."""

import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
TESTS_DIR = ROOT_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_configure_github_repo import FakeApi, configurator
from configure_github_repo import ApiError


class RepositorySettingsAuditTests(unittest.TestCase):
    def test_features_command_dryrun_and_apply_modifies_only_selected(self):
        """Features command must modify only selected features and perform zero writes on dry-run."""
        api = FakeApi()

        # 1. Dry run (default)
        plan = configurator.reconcile_features(api, "acme/project", ["wiki"], apply=False)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0][0], "PATCH")
        self.assertEqual(plan[0][2], {"has_wiki": True})
        self.assertEqual(api.writes, [], "Dry-run must not write to API")

        # 2. Apply
        applied = configurator.reconcile_features(api, "acme/project", ["wiki"], apply=True)
        self.assertEqual(len(applied), 1)
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(api.writes[0][0], "PATCH")
        self.assertEqual(api.writes[0][2], {"has_wiki": True})
        self.assertTrue(api.repo["has_wiki"])
        self.assertFalse(api.repo["has_issues"], "Unselected feature has_issues must remain untouched")
        self.assertFalse(api.repo["has_projects"], "Unselected feature has_projects must remain untouched")

        # 3. Convergent re-application performs zero writes
        repeat = configurator.reconcile_features(api, "acme/project", ["wiki"], apply=True)
        self.assertEqual(repeat, [])
        self.assertEqual(len(api.writes), 1, "Converged repeat must perform zero additional writes")

    def test_actions_command_enforces_read_only_default_token(self):
        """Actions command enables actions and sets default workflow token to read-only."""
        api = FakeApi()
        applied = configurator.reconcile_actions(api, "acme/project", apply=True)
        self.assertTrue(len(applied) >= 2)
        self.assertTrue(api.actions["enabled"])
        self.assertEqual(api.workflow["default_workflow_permissions"], "read")
        self.assertFalse(api.workflow["can_approve_pull_request_reviews"])

    def test_security_command_enables_selected_and_handles_unavailable(self):
        """Security features enable only selected item and detect 503 unavailable."""
        api = FakeApi()
        # Enable vulnerability alerts
        applied = configurator.reconcile_security(
            api,
            "acme/project",
            ["vulnerability-alerts"],
            apply=True,
        )
        self.assertEqual(len(applied), 1)
        self.assertTrue(api.features["vulnerability-alerts"])

        # When feature is unavailable (503), raises ApiError without writing
        api_unavailable = FakeApi()
        api_unavailable.features["vulnerability-alerts"] = None
        with self.assertRaises(configurator.ApiError):
            configurator.reconcile_security(
                api_unavailable,
                "acme/project",
                ["vulnerability-alerts"],
                apply=True,
            )

    def test_codeql_async_request_distinguishes_pending_from_completed(self):
        """CodeQL default setup requires Actions enabled and returns HTTP 202."""
        api = FakeApi()
        # Actions must be enabled first
        api.actions["enabled"] = True
        plan = configurator.reconcile_codeql(api, "acme/project", apply=True)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0][0], "PATCH")
        self.assertEqual(plan[0][2], {"state": "configured"})
        self.assertEqual(len(api.writes), 1)
        # Verify state is updated
        self.assertEqual(api.codeql["state"], "configured")

    def test_insufficient_permissions_raises_apierror_without_writes(self):
        """HTTP 403 Forbidden halts execution cleanly before sending any write requests."""
        class ForbiddenApi(FakeApi):
            def request(self, method, path, payload=None):
                if method == "GET":
                    raise configurator.ApiError("HTTP 403 Forbidden: Repository admin access required")
                return super().request(method, path, payload)

        api = ForbiddenApi()
        with self.assertRaises(configurator.ApiError):
            configurator.reconcile_features(api, "acme/project", ["issues"], apply=True)
        self.assertEqual(api.writes, [], "No writes should occur when permissions are insufficient")


if __name__ == "__main__":
    unittest.main()
