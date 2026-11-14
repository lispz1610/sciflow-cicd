"""Tests for repository capability, plan, and permission preflight detection."""

import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
TESTS_DIR = ROOT_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from test_configure_github_repo import FakeApi
from scripts.repository_capabilities import (
    CapabilityStatus,
    CapabilityResult,
    detect_repository_plan,
    detect_caller_permissions,
    classify_capabilities,
    evaluate_repository_capabilities,
)
import scripts.setup_github_repo as setup


class CapabilitiesFakeApi(FakeApi):
    def __init__(self, plan_name="free", owner_type="User", admin=True, visibility="public", scopes=None):
        super().__init__()
        self.plan_name = plan_name
        self.owner_type = owner_type
        self.scopes = scopes
        self.repo["visibility"] = visibility
        self.repo["owner"] = {"login": "acme", "type": owner_type}
        self.repo["permissions"] = {
            "admin": admin,
            "push": True,
            "pull": True,
            "triage": True,
            "maintain": admin,
        }

    def request(self, method, path, payload=None):
        if method == "GET":
            if path == "/user":
                return 200, {
                    "login": "acme",
                    "type": "User",
                    "plan": {"name": self.plan_name},
                }
            if path in ("/users/acme", "/orgs/acme"):
                return 200, {
                    "login": "acme",
                    "type": self.owner_type,
                    "plan": {"name": self.plan_name},
                }
            if path == "/repos/acme/project/code-scanning/default-setup":
                return 200, self.codeql
            if "rulesets" in path:
                return 200, self.rulesets
        return super().request(method, path, payload)


class RepositoryCapabilitiesTests(unittest.TestCase):
    def test_detect_plan_public_repository(self):
        api = CapabilitiesFakeApi(visibility="public", plan_name="free")
        plan_name, plan_dict = detect_repository_plan(api, api.repo)
        self.assertEqual(plan_name, "free")
        self.assertIsNotNone(plan_dict)

    def test_detect_plan_private_repository_free(self):
        api = CapabilitiesFakeApi(visibility="private", plan_name="free")
        plan_name, plan_dict = detect_repository_plan(api, api.repo)
        self.assertEqual(plan_name, "free")

    def test_detect_plan_private_repository_enterprise(self):
        api = CapabilitiesFakeApi(visibility="private", plan_name="enterprise", owner_type="Organization")
        plan_name, plan_dict = detect_repository_plan(api, api.repo)
        self.assertEqual(plan_name, "enterprise")

    def test_detect_plan_personal_account_pro(self):
        """User-owned repo detects Pro plan from GET /user endpoint."""
        api = CapabilitiesFakeApi(visibility="private", plan_name="pro", owner_type="User")
        plan_name, plan_dict = detect_repository_plan(api, api.repo)
        self.assertEqual(plan_name, "pro")
        self.assertEqual(plan_dict.get("name"), "pro")

    def test_private_team_repo_rulesets_supported_but_ghas_unsupported(self):
        """Private repo on Team plan supports rulesets, but GHAS controls remain unsupported."""
        api = CapabilitiesFakeApi(visibility="private", plan_name="team", owner_type="Organization", admin=True)
        inv = setup.inventory(api, "acme/project")

        caps = inv.get("capabilities", {})
        self.assertEqual(caps["rulesets"]["status"], "missing")
        self.assertEqual(caps["secret_scanning"]["status"], "unsupported")
        self.assertEqual(caps["codeql"]["status"], "unsupported")
        self.assertEqual(caps["dependency_review"]["status"], "unsupported")

    def test_token_lacking_oauth_scopes_reports_inaccessible(self):
        """Token with restricted OAuth scopes reports security features as inaccessible."""
        api = CapabilitiesFakeApi(visibility="public", admin=True, scopes=["public_repo"])
        inv = setup.inventory(api, "acme/project")

        caps = inv.get("capabilities", {})
        self.assertEqual(caps["codeql"]["status"], "inaccessible")
        self.assertIn("security_events", caps["codeql"]["reason"])

    def test_detect_caller_permissions_admin(self):
        api = CapabilitiesFakeApi(admin=True)
        perms = detect_caller_permissions(api.repo)
        self.assertTrue(perms["admin"])
        self.assertTrue(perms["push"])

    def test_detect_caller_permissions_non_admin(self):
        api = CapabilitiesFakeApi(admin=False)
        perms = detect_caller_permissions(api.repo)
        self.assertFalse(perms["admin"])
        self.assertTrue(perms["push"])

    def test_public_repo_capabilities_available_or_configured(self):
        api = CapabilitiesFakeApi(visibility="public", admin=True)
        api.repo["security_and_analysis"]["secret_scanning"]["status"] = "enabled"
        api.codeql["state"] = "configured"
        api.rulesets = [{"id": 1, "name": "main-gate"}]
        inv = setup.inventory(api, "acme/project")

        caps = inv.get("capabilities", {})
        self.assertIn("vulnerability_alerts", caps)
        self.assertIn("secret_scanning", caps)
        self.assertIn("codeql", caps)
        self.assertIn("dependency_graph", caps)
        self.assertIn("rulesets", caps)
        self.assertIn("dependency_review", caps)

        self.assertEqual(caps["secret_scanning"]["status"], "configured")
        self.assertEqual(caps["codeql"]["status"], "configured")
        self.assertEqual(caps["dependency_graph"]["status"], "configured")
        self.assertEqual(caps["rulesets"]["status"], "configured")
        self.assertEqual(caps["dependency_review"]["status"], "configured")

    def test_private_free_repo_capabilities_marked_unsupported(self):
        api = CapabilitiesFakeApi(visibility="private", plan_name="free", admin=True)
        inv = setup.inventory(api, "acme/project")

        caps = inv.get("capabilities", {})
        # On private free repos, secret scanning, CodeQL default setup, rulesets, and dependency review are unsupported
        self.assertEqual(caps["secret_scanning"]["status"], "unsupported")
        self.assertIn("Advanced Security", caps["secret_scanning"]["reason"])

        self.assertEqual(caps["codeql"]["status"], "unsupported")
        self.assertIn("Advanced Security", caps["codeql"]["reason"])

        self.assertEqual(caps["rulesets"]["status"], "unsupported")
        self.assertIn("Pro, Team, or Enterprise", caps["rulesets"]["reason"])

        self.assertEqual(caps["dependency_review"]["status"], "unsupported")
        self.assertIn("Advanced Security", caps["dependency_review"]["reason"])

    def test_private_enterprise_repo_capabilities_supported(self):
        api = CapabilitiesFakeApi(visibility="private", plan_name="enterprise", owner_type="Organization", admin=True)
        api.repo["security_and_analysis"]["secret_scanning"]["status"] = "disabled"
        inv = setup.inventory(api, "acme/project")

        caps = inv.get("capabilities", {})
        # On enterprise, secret scanning and rulesets are supported; if disabled, they are 'missing', not 'unsupported'
        self.assertEqual(caps["secret_scanning"]["status"], "missing")
        self.assertEqual(caps["rulesets"]["status"], "missing")
        self.assertEqual(caps["dependency_review"]["status"], "missing")

    def test_token_lacking_admin_reports_inaccessible(self):
        api = CapabilitiesFakeApi(visibility="public", admin=False)
        inv = setup.inventory(api, "acme/project")

        caps = inv.get("capabilities", {})
        self.assertEqual(caps["vulnerability_alerts"]["status"], "inaccessible")
        self.assertEqual(caps["secret_scanning"]["status"], "inaccessible")
        self.assertEqual(caps["codeql"]["status"], "inaccessible")
        self.assertEqual(caps["rulesets"]["status"], "inaccessible")

    def test_policy_dry_run_rejects_unsupported_private_free_security(self):
        api = CapabilitiesFakeApi(visibility="private", plan_name="free", admin=True)
        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {"name": "security", "enable": ["secret-scanning"]}
            ]
        }
        result = setup.run_policy(api, "acme/project", policy, apply=False)
        changes = result.get("changes", [])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["status"], "blocked")
        self.assertEqual(changes[0]["availability"], "unsupported")
        self.assertIn("Advanced Security", changes[0]["reason"])


if __name__ == "__main__":
    unittest.main()
