"""Unit tests for scripts/interactive.py greenfield and configuration detection."""

import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.interactive import has_existing_configuration


def _fresh_github_repo_inventory() -> dict:
    """Return an inventory matching a brand-new GitHub repository's default flags."""
    return {
        "repository": {
            "full_name": "acme/new-repo",
            "visibility": "public",
            "default_branch": "main",
        },
        "features": {
            "issues": True,
            "wiki": True,
            "projects": True,
        },
        "merge": {
            "allow_merge_commit": True,
            "allow_squash_merge": True,
            "allow_rebase_merge": True,
            "delete_branch_on_merge": False,
            "allow_update_branch": False,
        },
        "actions": {
            "enabled": True,
            "allowed_actions": "all",
        },
        "workflow_permissions": {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        },
        "security_and_analysis": {
            "secret_scanning": {"status": "disabled"},
            "secret_scanning_push_protection": {"status": "disabled"},
        },
        "vulnerability_alerts": {"status": 404},
        "automated_security_fixes": {"status": 404},
        "private_vulnerability_reporting": {"enabled": False},
        "codeql": {"state": "not-configured"},
        "rulesets": [],
        "agent_skills": False,
        "managed_workflows": [],
        "dependabot": None,
    }


class GreenfieldDetectionTests(unittest.TestCase):
    """Tests for has_existing_configuration ignoring default GitHub flags."""

    def test_newly_created_repo_with_default_github_flags_is_greenfield(self):
        inv = _fresh_github_repo_inventory()
        self.assertFalse(has_existing_configuration(inv))

    def test_hardened_merge_or_actions_policy_detected_as_configured(self):
        inv_branch = _fresh_github_repo_inventory()
        inv_branch["merge"]["delete_branch_on_merge"] = True
        self.assertTrue(has_existing_configuration(inv_branch))

        inv_actions = _fresh_github_repo_inventory()
        inv_actions["actions"]["allowed_actions"] = "selected"
        self.assertTrue(has_existing_configuration(inv_actions))

    def test_managed_artifacts_detected_as_configured(self):
        inv_wf = _fresh_github_repo_inventory()
        inv_wf["managed_workflows"] = [{"filename": "ci.yml", "template": "profile-ci"}]
        self.assertTrue(has_existing_configuration(inv_wf))

        inv_skills = _fresh_github_repo_inventory()
        inv_skills["agent_skills"] = True
        self.assertTrue(has_existing_configuration(inv_skills))

        inv_dep = _fresh_github_repo_inventory()
        inv_dep["dependabot"] = {"status": "managed", "package_ecosystems": ["pip"]}
        self.assertTrue(has_existing_configuration(inv_dep))


if __name__ == "__main__":
    unittest.main()
