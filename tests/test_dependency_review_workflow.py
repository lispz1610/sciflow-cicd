"""Tests for proportional Dependency Review workflow with capability gating and license allowlists (#139)."""

import io
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock

SCRIPTS_DIR = str(Path(__file__).resolve().parents[1] / "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from interactive import Session
from interactive_workflows_rulesets import prompt_workflows_stage
import install_github_workflows as installer
from install_github_workflows import (
    DEFAULT_CHECKOUT_SHA,
    DEFAULT_DEPENDENCY_SHA,
    ApiError,
    render,
    validate_policy as validate_workflows_policy,
)
import setup_github_repo as setup
from repository_capabilities import (
    CapabilityStatus,
    classify_capabilities,
    evaluate_repository_capabilities,
)


class DependencyReviewApi:
    """Mock GitHub API for dependency review workflow tests."""

    def __init__(self, visibility="public", plan_name="free", owner_type="User", admin=True):
        self.visibility = visibility
        self.plan_name = plan_name
        self.owner_type = owner_type
        self.admin = admin
        self.writes = []
        self.prs = []
        self.files = {}
        self.refs = {"main": "1" * 40}

    def request(self, method, path, payload=None):
        self.writes.append((method, path, payload))
        if method == "GET":
            if path == "/repos/acme/project":
                return 200, {
                    "full_name": "acme/project",
                    "visibility": self.visibility,
                    "default_branch": "main",
                    "permissions": {
                        "admin": self.admin,
                        "push": True,
                        "pull": True,
                    },
                    "owner": {
                        "login": "acme",
                        "type": self.owner_type,
                    },
                }
            if path in ("/users/acme", "/orgs/acme"):
                return 200, {
                    "login": "acme",
                    "type": self.owner_type,
                    "plan": {"name": self.plan_name},
                }
            if path.startswith("/repos/actions/checkout/git/commits/"):
                sha = path.split("/")[-1]
                return 200, {"sha": sha}
            if path.startswith("/repos/actions/dependency-review-action/git/commits/"):
                sha = path.split("/")[-1]
                return 200, {"sha": sha}
            if path == "/repos/acme/project/git/ref/heads/main":
                return 200, {"object": {"sha": self.refs["main"]}}
            if path.startswith("/repos/acme/project/git/ref/heads/"):
                branch = path.split("/heads/")[-1]
                if branch in self.refs:
                    return 200, {"object": {"sha": self.refs[branch]}}
                return 404, {"message": "Not Found"}
            if path.startswith("/repos/acme/project/pulls"):
                return 200, self.prs
            if path.startswith("/repos/acme/project/contents/"):
                return 404, {"message": "Not Found"}
            if path == "/repos/acme/project/vulnerability-alerts":
                return (204, None) if self.admin else (403, {"message": "Forbidden"})
            if path == "/repos/acme/project/community/profile":
                return 404, {"message": "Not Found"}
            if path == "/repos/acme/project/rulesets":
                return 200, []
        elif method == "POST":
            if path == "/repos/acme/project/git/refs":
                ref_name = payload["ref"].replace("refs/heads/", "")
                self.refs[ref_name] = payload["sha"]
                return 201, {"ref": payload["ref"], "object": {"sha": payload["sha"]}}
            if path == "/repos/acme/project/pulls":
                pr = {
                    "number": len(self.prs) + 1,
                    "html_url": f"https://github.com/acme/project/pull/{len(self.prs) + 1}",
                    "head": {"ref": payload["head"].split(":")[-1]},
                    "base": {"ref": payload["base"]},
                    "state": "open",
                }
                self.prs.append(pr)
                return 201, pr
        elif method == "PUT":
            if "/contents/" in path:
                return 201, {
                    "content": {"sha": "d" * 40},
                    "commit": {"sha": "c" * 40},
                }
        return 404, {"message": "Not Found"}


class DependencyReviewWorkflowTests(unittest.TestCase):
    """Test suite for P3.3 Dependency Review workflow implementation."""

    def test_workflow_schema_validation_accepts_valid_options(self):
        wf = {
            "template": "dependency-review",
            "template_version": 1,
            "filename": "dependency-review.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "action_sha": DEFAULT_DEPENDENCY_SHA,
            "fail_on_severity": "high",
            "allow_licenses": ["MIT", "Apache-2.0", "BSD-3-Clause"],
            "deny_licenses": ["GPL-3.0"],
            "comment_summary_in_pr": "always",
        }
        rendered = render(wf).decode("utf-8")
        self.assertIn("fail-on-severity: high", rendered)
        self.assertIn('allow-licenses: "MIT, Apache-2.0, BSD-3-Clause"', rendered)
        self.assertIn('deny-licenses: "GPL-3.0"', rendered)
        self.assertIn('comment-summary-in-pr: "always"', rendered)
        self.assertIn("actions/dependency-review-action@" + DEFAULT_DEPENDENCY_SHA, rendered)

    def test_workflow_schema_validation_accepts_string_allow_licenses(self):
        wf = {
            "template": "dependency-review",
            "template_version": 1,
            "filename": "dependency-review.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "action_sha": DEFAULT_DEPENDENCY_SHA,
            "fail_on_severity": "critical",
            "allow_licenses": "MIT, Apache-2.0, BSD-2-Clause",
        }
        rendered = render(wf).decode("utf-8")
        self.assertIn("fail-on-severity: critical", rendered)
        self.assertIn('allow-licenses: "MIT, Apache-2.0, BSD-2-Clause"', rendered)

    def test_workflow_schema_validation_rejects_invalid_severity(self):
        wf = {
            "template": "dependency-review",
            "template_version": 1,
            "filename": "dependency-review.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "action_sha": DEFAULT_DEPENDENCY_SHA,
            "fail_on_severity": "extreme",
        }
        with self.assertRaisesRegex(ValueError, "fail_on_severity must be one of"):
            render(wf)

    def test_workflow_schema_validation_rejects_invalid_licenses(self):
        wf = {
            "template": "dependency-review",
            "template_version": 1,
            "filename": "dependency-review.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "action_sha": DEFAULT_DEPENDENCY_SHA,
            "allow_licenses": 12345,
        }
        with self.assertRaisesRegex(ValueError, "allow_licenses must be a list"):
            render(wf)

    def test_workflow_schema_validation_rejects_unknown_keys(self):
        wf = {
            "template": "dependency-review",
            "template_version": 1,
            "filename": "dependency-review.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "action_sha": DEFAULT_DEPENDENCY_SHA,
            "unknown_extra_option": True,
        }
        with self.assertRaisesRegex(ValueError, "select a supported version 1 workflow template"):
            render(wf)

    def test_workflow_rendered_permissions_contain_job_level_least_privilege(self):
        wf = {
            "template": "dependency-review",
            "template_version": 1,
            "filename": "dependency-review.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "action_sha": DEFAULT_DEPENDENCY_SHA,
        }
        rendered = render(wf).decode("utf-8")
        # Top-level permissions
        self.assertIn("permissions:\n  contents: read\n  pull-requests: read", rendered)
        # Job-level permissions
        self.assertIn("  dependency-review:\n    permissions:\n      contents: read\n      pull-requests: read\n    runs-on: ubuntu-24.04", rendered)

    def test_interactive_omits_dependency_review_when_unsupported_on_private_free_repo(self):
        session = Session(
            repository="acme/project",
            api=DependencyReviewApi(visibility="private", plan_name="free"),
            inventory={
                "repository": {"full_name": "acme/project", "visibility": "private", "plan": "free"},
                "capabilities": {
                    "dependency_review": {
                        "status": "unsupported",
                        "reason": "Dependency review on private repositories requires GitHub Advanced Security",
                    }
                },
            },
        )
        # User confirms staging, and enters 1 (which will now be Standard PR CI)
        in_stream = io.StringIO("y\n1\n")
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(session=session, in_stream=in_stream, out_stream=out_stream)
        output = out_stream.getvalue()

        self.assertIn("Dependency review is unsupported on this repository", output)
        self.assertNotIn("2. Dependency review", output)
        self.assertIsNotNone(stage)
        self.assertEqual(len(stage["workflows"]), 1)
        self.assertEqual(stage["workflows"][0]["template"], "pr-ci")

    def test_interactive_offers_dependency_review_when_supported_on_public_repo(self):
        session = Session(
            repository="acme/project",
            api=DependencyReviewApi(visibility="public"),
            inventory={
                "repository": {"full_name": "acme/project", "visibility": "public"},
                "capabilities": {
                    "dependency_review": {
                        "status": "configured",
                        "reason": "Dependency review is supported for public repositories",
                    }
                },
            },
        )
        in_stream = io.StringIO("y\n2\n")
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(session=session, in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(len(stage["workflows"]), 1)
        self.assertEqual(stage["workflows"][0]["template"], "dependency-review")
        self.assertEqual(stage["workflows"][0]["fail_on_severity"], "high")
        self.assertIn("MIT", stage["workflows"][0]["allow_licenses"])

    def test_dry_run_rejects_dependency_review_on_private_free_repo(self):
        api = DependencyReviewApi(visibility="private", plan_name="free", admin=True)
        policy = {
            "version": 1,
            "repository": "acme/project",
            "stages": [
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
                        }
                    ],
                }
            ],
        }
        res = setup.run_policy(api, "acme/project", policy, apply=False)
        changes = res.get("changes", [])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["status"], "blocked")
        self.assertEqual(changes[0]["availability"], "unsupported")
        self.assertIn("Advanced Security", changes[0]["reason"])

    def test_dry_run_stages_dependency_review_on_public_repo(self):
        api = DependencyReviewApi(visibility="public", admin=True)
        policy = {
            "version": 1,
            "repository": "acme/project",
            "stages": [
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
                            "allow_licenses": "MIT, Apache-2.0",
                        }
                    ],
                }
            ],
        }
        res = setup.run_policy(api, "acme/project", policy, apply=False)
        changes = res.get("changes", [])
        dep_change = next((c for c in changes if c.get("path") == ".github/workflows/dependency-review.yml"), None)
        self.assertIsNotNone(dep_change)
        self.assertEqual(dep_change["status"], "planned")


if __name__ == "__main__":
    unittest.main()
