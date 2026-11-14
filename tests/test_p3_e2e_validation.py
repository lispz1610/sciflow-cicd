"""End-to-end validation test suite for P3 proportional security and merge controls."""

import base64
import io
import json
from pathlib import Path
import sys
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
TESTS_DIR = ROOT_DIR / "tests"
for d in (str(ROOT_DIR), str(SCRIPTS_DIR), str(TESTS_DIR)):
    if d not in sys.path:
        sys.path.insert(0, d)

from configure_github_repo import ApiError, GitHubApi, reconcile_gates
from doctor import audit_repository_controls, run_doctor, format_doctor_report
from install_github_workflows import DEFAULT_CHECKOUT_SHA, DEFAULT_DEPENDENCY_SHA
from repository_capabilities import evaluate_repository_capabilities
from setup_github_repo import inventory, run_policy, validate_policy
from test_install_github_workflows import WorkflowApi


class P3DisposableRepoApi(WorkflowApi):
    """Disposable repository test harness for complete P3 end-to-end evaluation."""

    def __init__(self, visibility="public", plan="free", admin=True):
        super().__init__()
        self.visibility = visibility
        self.plan = plan
        self.admin = admin
        self.rulesets = []
        self.codeql_state = "not-configured"
        self.workflow_perms = {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        }
        self.repo.update({
            "name": "project",
            "full_name": "acme/project",
            "visibility": visibility,
            "default_branch": "main",
            "has_issues": True,
            "has_wiki": True,
            "has_projects": True,
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": False,
            "delete_branch_on_merge": True,
            "allow_update_branch": True,
            "owner": {"login": "acme", "type": "User"},
            "permissions": {"admin": admin, "push": True, "pull": True},
            "plan": {"name": plan},
            "security_and_analysis": {
                "secret_scanning": {"status": "enabled"},
                "secret_scanning_push_protection": {"status": "enabled"},
            },
        })
        self.reviews = []
        self.check_runs = [
            {
                "name": "status-check",
                "head_sha": "merge-abc",
                "status": "completed",
                "conclusion": "success",
                "check_suite": {"id": 101},
                "app": {"id": 15368},
            },
            {
                "name": "Dependency Review",
                "head_sha": "merge-abc",
                "status": "completed",
                "conclusion": "success",
                "check_suite": {"id": 102},
                "app": {"id": 15368},
            },
        ]
        self.workflow_runs = [
            {
                "workflow_id": 10,
                "check_suite_id": 101,
                "head_sha": "merge-abc",
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "workflow_id": 20,
                "check_suite_id": 102,
                "head_sha": "merge-abc",
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
            },
        ]
        self.workflows_list = [
            {"id": 10, "path": ".github/workflows/ci.yml", "state": "active"},
            {"id": 20, "path": ".github/workflows/dependency-review.yml", "state": "active"},
        ]

    def request(self, method, path, payload=None):
        if method == "GET":
            if path in ("/repos/acme/project", "repos/acme/project"):
                return 200, dict(self.repo)
            if path == "/users/acme":
                return 200, {"login": "acme", "plan": {"name": self.plan}}
            if path == "/repos/acme/project/actions/permissions/workflow":
                return 200, dict(self.workflow_perms)
            if path == "/repos/acme/project/vulnerability-alerts":
                return 204, {}
            if path == "/repos/acme/project/automated-security-fixes":
                return 200, {"enabled": True}
            if path == "/repos/acme/project/private-vulnerability-reporting":
                return 200, {"enabled": False}
            if path == "/repos/acme/project/code-scanning/default-setup":
                if self.visibility == "private" and self.plan in ("free", "unknown"):
                    return 404, {"message": "Not Found"}
                return 200, {"state": self.codeql_state, "languages": ["python"], "query_suite": "default"}
            if path == "/repos/acme/project/rulesets" or path.startswith("/repos/acme/project/rulesets?"):
                return 200, list(self.rulesets)
            if path.startswith("/repos/acme/project/rulesets/"):
                rid = int(path.split("/")[-1])
                for r in self.rulesets:
                    if r.get("id") == rid:
                        return 200, dict(r)
                return 404, {"message": "Ruleset not found"}
            if path.startswith("/repos/acme/project/actions/workflows/10/runs"):
                return 200, {"workflow_runs": [self.workflow_runs[0]]}
            if path.startswith("/repos/acme/project/actions/workflows/20/runs"):
                return 200, {"workflow_runs": [self.workflow_runs[1]]}
            if path.startswith("/repos/acme/project/actions/workflows"):
                return 200, {"workflows": self.workflows_list}
            if path.startswith("/repos/acme/project/commits/merge-abc/check-runs"):
                return 200, {"check_runs": self.check_runs}
            if path == "/repos/acme/project/pulls/12":
                return 200, {
                    "number": 12,
                    "state": "open",
                    "base": {"ref": "main"},
                    "head": {"sha": "merge-abc"},
                    "merge_commit_sha": "merge-abc",
                    "user": {"login": "solo-maintainer"},
                }
            if path == "/repos/acme/project/pulls/12/reviews":
                return 200, list(self.reviews)
            if path == "/repos/acme/project/contents/.github/workflows":
                items = []
                for (branch, fname) in self.files:
                    if branch == "main" and fname.startswith(".github/workflows/"):
                        file_name = fname.split("/")[-1]
                        items.append({"name": file_name, "path": fname, "type": "file"})
                if items:
                    return 200, items
                return 404, {"message": "Not Found"}
            if path.startswith("/repos/acme/project/contents/"):
                from urllib.parse import unquote
                parts = path.split("?ref=")
                filename = parts[0].removeprefix("/repos/acme/project/contents/")
                branch = unquote(parts[1]) if len(parts) > 1 else "main"
                key = (branch, filename)
                content = self.files.get(key)
                if content is None and branch != "main":
                    content = self.files.get(("main", filename))
                if content is None:
                    return 404, {"message": "Not Found"}
                data, sha = content
                encoded = base64.b64encode(data).decode()
                return 200, {"type": "file", "encoding": "base64", "content": encoded, "sha": sha}

        if method == "PATCH" and path == "/repos/acme/project/code-scanning/default-setup":
            self.writes.append((method, path, payload))
            self.codeql_state = payload.get("state", "configured")
            return 200, {"state": self.codeql_state, "languages": ["python"], "query_suite": payload.get("query_suite", "default")}

        if method == "POST" and path == "/repos/acme/project/rulesets":
            self.writes.append((method, path, payload))
            new_ruleset = dict(payload, id=len(self.rulesets) + 1)
            self.rulesets.append(new_ruleset)
            return 201, new_ruleset

        if method == "PUT" and path.startswith("/repos/acme/project/rulesets/"):
            self.writes.append((method, path, payload))
            rid = int(path.split("/")[-1])
            for idx, r in enumerate(self.rulesets):
                if r.get("id") == rid:
                    self.rulesets[idx] = dict(payload, id=rid)
            return 200, payload

        return super().request(method, path, payload)


class P3EndToEndValidationTests(unittest.TestCase):
    """End-to-end tests exercising proportional security, solo rulesets, and diagnostics."""

    def test_capability_detection_across_repository_tiers(self):
        """Capabilities accurately classify public vs private repository tiers."""
        # Public free repository: CodeQL and Dependency Review are fully supported/configured
        public_api = P3DisposableRepoApi(visibility="public", plan="free")
        public_inv = inventory(public_api, "acme/project")
        public_caps = public_inv.get("capabilities", {})
        self.assertEqual(public_caps["dependency_graph"]["status"], "configured")
        self.assertEqual(public_caps["dependency_review"]["status"], "configured")
        self.assertIn("public repositories", public_caps["dependency_review"]["reason"])

        # Private free repository: CodeQL, Dependency Review, and Rulesets are unsupported
        private_api = P3DisposableRepoApi(visibility="private", plan="free")
        private_inv = inventory(private_api, "acme/project")
        private_caps = private_inv.get("capabilities", {})
        self.assertEqual(private_caps["codeql"]["status"], "unsupported")
        self.assertIn("requires GitHub Advanced Security", private_caps["codeql"]["reason"])
        self.assertEqual(private_caps["dependency_review"]["status"], "unsupported")
        self.assertIn("requires GitHub Advanced Security", private_caps["dependency_review"]["reason"])
        self.assertEqual(private_caps["rulesets"]["status"], "unsupported")
        self.assertIn("require a Pro, Team, or Enterprise plan", private_caps["rulesets"]["reason"])

    def test_dependabot_and_dependency_review_staging_in_setup_pr(self):
        """Dependabot and Dependency Review workflows stage cleanly into draft Setup PR."""
        api = P3DisposableRepoApi(visibility="public", plan="free")
        policy = {
            "version": 1,
            "stages": [
                {
                    "name": "dependabot",
                    "cadence": "monthly",
                    "package_ecosystems": ["pip", "github-actions"],
                    "open_pull_requests_limit": 5,
                    "groups": {
                        "pip-dependencies": ["*"],
                        "github-actions-dependencies": ["actions/*"],
                    },
                },
                {
                    "name": "workflows",
                    "workflows": [
                        {
                            "template": "dependency-review",
                            "template_version": 1,
                            "filename": "dependency-review.yml",
                            "triggers": ["pull_request"],
                            "checkout_sha": DEFAULT_CHECKOUT_SHA,
                            "action_sha": DEFAULT_DEPENDENCY_SHA,
                            "fail_on_severity": "high",
                            "allow_licenses": ["MIT", "Apache-2.0"],
                        }
                    ],
                },
            ],
        }

        # Dry run verifies proposed staging
        plan = run_policy(api, "acme/project", policy, apply=False)
        changes = plan["changes"]
        self.assertEqual(len(changes), 2)
        paths = [c["path"] for c in changes]
        self.assertIn(".github/dependabot.yml", paths)
        self.assertIn(".github/workflows/dependency-review.yml", paths)
        for c in changes:
            self.assertEqual(c["status"], "planned")

        # Apply creates the files in the staging branch and opens Setup PR
        applied = run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=plan)
        self.assertIn("setup_pull_request", applied)
        self.assertEqual(applied["setup_pull_request"], "https://github.com/acme/project/pull/1")
        self.assertIn("setup_branch", applied)
        self.assertTrue(applied["setup_branch"].startswith("codex/workflows-"))

        staged_filenames = [fname for (_, fname) in api.files.keys()]
        self.assertIn(".github/dependabot.yml", staged_filenames)
        self.assertIn(".github/workflows/dependency-review.yml", staged_filenames)

        # Inspect staged dependabot.yml content
        dep_content = next(data.decode("utf-8") for (b, fname), (data, _) in api.files.items() if fname == ".github/dependabot.yml")
        self.assertIn('"monthly"', dep_content)
        self.assertIn("groups:", dep_content)
        self.assertIn("pip-dependencies:", dep_content)
        self.assertIn("open-pull-requests-limit: 5", dep_content)

        # Inspect staged dependency-review.yml content
        wf_content = next(data.decode("utf-8") for (b, fname), (data, _) in api.files.items() if fname == ".github/workflows/dependency-review.yml")
        self.assertIn("permissions:", wf_content)
        self.assertIn("contents: read", wf_content)
        self.assertIn("pull-requests: read", wf_content)
        self.assertIn("actions/dependency-review-action@", wf_content)

    def test_solo_maintainer_merge_path_with_zero_required_approvals(self):
        """Solo maintainer ruleset requires status checks but zero secondary approvals."""
        api = P3DisposableRepoApi(visibility="public", plan="free")
        checks = {
            "status-check": ".github/workflows/ci.yml",
            "Dependency Review": ".github/workflows/dependency-review.yml",
        }

        # Reconcile gates for solo maintainer with 0 required approvals
        changes = reconcile_gates(
            api,
            "acme/project",
            "solo-merge-controls",
            pull_number=12,
            reviewer=None,
            checks=checks,
            apply=True,
            required_approving_review_count=0,
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(len(api.rulesets), 1)

        active_ruleset = api.rulesets[0]
        self.assertEqual(active_ruleset["name"], "solo-merge-controls")

        # Verify PR rule has required_approving_review_count: 0
        pr_rule = next(r for r in active_ruleset["rules"] if r["type"] == "pull_request")
        self.assertEqual(pr_rule["parameters"]["required_approving_review_count"], 0)

        # Verify status checks rule has exact required checks pinned to app ID
        checks_rule = next(r for r in active_ruleset["rules"] if r["type"] == "required_status_checks")
        req_checks = checks_rule["parameters"]["required_status_checks"]
        self.assertEqual(len(req_checks), 2)
        contexts = {c["context"] for c in req_checks}
        self.assertEqual(contexts, {"status-check", "Dependency Review"})
        for c in req_checks:
            self.assertEqual(c["integration_id"], 15368)

        # Secondary apply is idempotent (returns [] with zero additional writes)
        write_count = len([w for w in api.writes if w[0] in ("POST", "PUT", "PATCH")])
        second_changes = reconcile_gates(
            api,
            "acme/project",
            "solo-merge-controls",
            pull_number=12,
            reviewer=None,
            checks=checks,
            apply=True,
            required_approving_review_count=0,
        )
        self.assertEqual(second_changes, [])
        new_write_count = len([w for w in api.writes if w[0] in ("POST", "PUT", "PATCH")])
        self.assertEqual(write_count, new_write_count)

    def test_missing_required_checks_block_ruleset_creation(self):
        """Missing or unproven status check blocks ruleset activation."""
        api = P3DisposableRepoApi(visibility="public", plan="free")
        # Checks specify an unproven check name
        checks = {
            "nonexistent-check": ".github/workflows/ci.yml",
        }
        with self.assertRaisesRegex(ValueError, "missing successful check: nonexistent-check"):
            reconcile_gates(
                api,
                "acme/project",
                "broken-gate",
                pull_number=12,
                reviewer=None,
                checks=checks,
                apply=True,
                required_approving_review_count=0,
            )

    def test_doctor_diagnostics_live_reporting(self):
        """Doctor accurately audits and categorizes controls across configured, missing, and unsupported states."""
        # 1. Configured repository
        clean_wf = (
            "name: CI\n"
            "permissions:\n"
            "  contents: read\n"
            "jobs:\n"
            "  ci:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n"
        )
        clean_dep = (
            "version: 2\n"
            "updates:\n"
            "  - package-ecosystem: 'pip'\n"
            "    directory: '/'\n"
            "    schedule:\n"
            "      interval: 'monthly'\n"
            "    groups:\n"
            "      all: {'patterns': ['*']}\n"
        )
        clean_api = P3DisposableRepoApi(visibility="public", plan="free")
        clean_api.files[("main", ".github/workflows/ci.yml")] = (
            clean_wf.encode("utf-8"), "sha1"
        )
        clean_api.files[("main", ".github/dependabot.yml")] = (
            clean_dep.encode("utf-8"), "sha2"
        )
        clean_api.codeql_state = "configured"
        clean_api.rulesets = [{"id": 1, "name": "quality-gate"}]

        clean_out = io.StringIO()
        clean_code = run_doctor(out_stream=clean_out, repo="acme/project", api=clean_api)
        self.assertEqual(clean_code, 0)
        clean_report = clean_out.getvalue()
        self.assertIn("Verdict: System is healthy", clean_report)
        self.assertIn("[CONFIGURED] Merge Controls", clean_report)
        self.assertIn("[CONFIGURED] Default Workflow Permissions", clean_report)
        self.assertIn("[CONFIGURED] Action SHA Pins", clean_report)
        self.assertIn("[CONFIGURED] Dependabot Configuration", clean_report)
        self.assertIn("[CONFIGURED] CodeQL Default Setup", clean_report)
        self.assertIn("[CONFIGURED] Branch Rulesets & Merge Gates", clean_report)

        # 2. Unsupported capabilities on private free repo
        private_api = P3DisposableRepoApi(visibility="private", plan="free")
        private_api.files[("main", ".github/workflows/ci.yml")] = (
            clean_wf.encode("utf-8"), "sha1"
        )
        private_api.files[("main", ".github/dependabot.yml")] = (
            clean_dep.encode("utf-8"), "sha2"
        )
        priv_out = io.StringIO()
        priv_code = run_doctor(out_stream=priv_out, repo="acme/project", api=private_api)
        priv_report = priv_out.getvalue()
        self.assertIn("[UNSUPPORTED] CodeQL Default Setup", priv_report)
        self.assertIn("requires GitHub Advanced Security", priv_report)
        self.assertIn("[UNSUPPORTED] Dependency Review Gate", priv_report)
        self.assertIn("[UNSUPPORTED] Branch Rulesets & Merge Gates", priv_report)

        # 3. JSON format verification
        json_out = io.StringIO()
        run_doctor(out_stream=json_out, repo="acme/project", json_output=True, api=clean_api)
        json_data = json.loads(json_out.getvalue())
        self.assertEqual(json_data["repository"], "acme/project")
        self.assertEqual(json_data["verdict"], "healthy")
        self.assertIsInstance(json_data["controls"], list)
        self.assertEqual(json_data["summary"]["missing"], 0)


if __name__ == "__main__":
    unittest.main()
