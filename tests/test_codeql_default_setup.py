from pathlib import Path
import sys
import unittest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from configure_github_repo import ApiError, reconcile_codeql
from setup_github_repo import run_policy, validate_policy


class CodeQLTestApi:
    def __init__(self, visibility="public", plan_name="free", languages=None, state="not-configured", query_suite="default"):
        self.visibility = visibility
        self.plan_name = plan_name
        self.languages = ["python"] if languages is None else languages
        self.state = state
        self.query_suite = query_suite
        self.actions_enabled = True
        self.writes = []
        self.reads = []
        self.repo_data = {
            "name": "project",
            "full_name": "acme/project",
            "visibility": self.visibility,
            "default_branch": "main",
            "owner": {"login": "acme", "type": "User"},
            "plan": {"name": self.plan_name},
        }

    def request(self, method, path, payload=None):
        if method == "GET":
            self.reads.append(path)
            if path in ("/repos/acme/project", "repos/acme/project"):
                return 200, dict(self.repo_data)
            if path == "/repos/acme/project/actions/permissions":
                return 200, {"enabled": self.actions_enabled}
            if path == "/repos/acme/project/actions/permissions/workflow":
                return 200, {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}
            if path == "/repos/acme/project/code-scanning/default-setup":
                if self.visibility == "private" and self.plan_name in ("free", "unknown"):
                    return 404, {"message": "Not Found"}
                return 200, {
                    "state": self.state,
                    "languages": list(self.languages),
                    "query_suite": self.query_suite,
                }
            if path == "/repos/acme/project/rulesets?includes_parents=false&per_page=100&page=1":
                return 200, []
            return 404, {"message": f"mock route not found: {path}"}
        if method == "PATCH":
            self.writes.append((method, path, payload))
            if path == "/repos/acme/project/code-scanning/default-setup":
                if "state" in payload:
                    self.state = payload["state"]
                if "query_suite" in payload:
                    self.query_suite = payload["query_suite"]
                return 200, {
                    "state": self.state,
                    "languages": list(self.languages),
                    "query_suite": self.query_suite,
                }
            return 200, {}
        return 404, {"message": f"unhandled method: {method}"}


class CodeQLDefaultSetupTests(unittest.TestCase):
    def test_codeql_default_setup_plans_standard_suite(self):
        api = CodeQLTestApi(visibility="public", languages=["python"])
        changes = reconcile_codeql(api, "acme/project", apply=False)
        self.assertEqual(changes, [("PATCH", "/repos/acme/project/code-scanning/default-setup", {"state": "configured"})])
        self.assertEqual(api.writes, [])

    def test_codeql_extended_suite_payload(self):
        api = CodeQLTestApi(visibility="public", languages=["python"])
        changes = reconcile_codeql(api, "acme/project", apply=False, query_suite="extended")
        self.assertEqual(changes, [("PATCH", "/repos/acme/project/code-scanning/default-setup", {"state": "configured", "query_suite": "extended"})])

    def test_invalid_query_suite_rejected(self):
        api = CodeQLTestApi(visibility="public", languages=["python"])
        with self.assertRaisesRegex(ValueError, "unsupported query_suite"):
            reconcile_codeql(api, "acme/project", query_suite="security-and-quality")

    def test_no_codeql_supported_languages_raises_error(self):
        api = CodeQLTestApi(visibility="public", languages=[])
        with self.assertRaisesRegex(ApiError, "no CodeQL-supported languages"):
            reconcile_codeql(api, "acme/project")

    def test_private_repo_without_ghas_reported_as_unsupported_in_policy_dry_run(self):
        api = CodeQLTestApi(visibility="private", plan_name="free", languages=["python"])
        policy = {
            "version": 1,
            "stages": [
                {"name": "codeql"},
            ],
        }
        res = run_policy(api, "acme/project", policy, apply=False)
        changes = res.get("changes", [])
        self.assertEqual(len(changes), 1)
        change = changes[0]
        self.assertEqual(change["stage"], "codeql")
        self.assertEqual(change["availability"], "unsupported")
        self.assertIn("GitHub Advanced Security", change["reason"])
        self.assertIn("Settings > Code security and analysis", change["guidance"])

    def test_unsupported_languages_reported_with_guidance_in_dry_run(self):
        api = CodeQLTestApi(visibility="public", languages=[])
        policy = {
            "version": 1,
            "stages": [
                {"name": "codeql"},
            ],
        }
        res = run_policy(api, "acme/project", policy, apply=False)
        changes = res.get("changes", [])
        self.assertEqual(len(changes), 1)
        change = changes[0]
        self.assertEqual(change["stage"], "codeql")
        self.assertEqual(change["availability"], "unsupported")
        self.assertIn("no CodeQL-supported languages detected", change["reason"])
        self.assertIn("Add CodeQL-supported source code", change["guidance"])

    def test_policy_validation_accepts_valid_codeql_stages(self):
        policy_default = {"version": 1, "stages": [{"name": "codeql"}]}
        validate_policy(policy_default)

        policy_suite = {"version": 1, "stages": [{"name": "codeql", "query_suite": "extended"}]}
        validate_policy(policy_suite)

        policy_invalid = {"version": 1, "stages": [{"name": "codeql", "query_suite": "security-and-quality"}]}
        with self.assertRaisesRegex(ValueError, "unsupported query_suite"):
            validate_policy(policy_invalid)

    def test_dry_run_and_apply_convergence(self):
        api = CodeQLTestApi(visibility="public", languages=["python", "javascript"])
        policy = {
            "version": 1,
            "stages": [
                {"name": "codeql"},
            ],
        }

        # Initial dry-run plans enablement
        plan = run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(len(plan["changes"]), 1)
        self.assertEqual(plan["changes"][0]["status"], "planned")

        # Apply configuration
        applied = run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=plan)
        self.assertEqual(len(applied["changes"]), 1)
        self.assertIn(applied["changes"][0]["status"], ("applied", "unverified", "pending"))
        self.assertEqual(api.state, "configured")

        # Second dry-run converges (no changes planned)
        plan_converged = run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(plan_converged["changes"], [])
        self.assertEqual(plan_converged["status"], "no-op")


if __name__ == "__main__":
    unittest.main()
