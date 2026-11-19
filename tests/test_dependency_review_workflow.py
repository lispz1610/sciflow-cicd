"""Tests for proportional Dependency Review workflow with capability gating and license allowlists (#139)."""

import io
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock

try:
    import yaml
except ImportError:
    yaml = None

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
        self.assertIn("  dependency-review:\n    permissions:\n      contents: read\n      pull-requests: read\n    runs-on: self-hosted", rendered)

    def test_workflow_runs_on_custom_hosted_and_labels(self):
        wf_hosted = {
            "template": "dependency-review",
            "template_version": 1,
            "filename": "dependency-review.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "action_sha": DEFAULT_DEPENDENCY_SHA,
            "runs_on": "ubuntu-24.04",
        }
        rendered = render(wf_hosted).decode("utf-8")
        self.assertIn("runs-on: ubuntu-24.04", rendered)

        wf_labels = {
            "template": "dependency-review",
            "template_version": 1,
            "filename": "dependency-review.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "action_sha": DEFAULT_DEPENDENCY_SHA,
            "runs_on": ["self-hosted", "linux", "gpu"],
        }
        rendered_labels = render(wf_labels).decode("utf-8")
        if yaml is not None:
            parsed = yaml.safe_load(rendered_labels)
            self.assertEqual(parsed["jobs"]["dependency-review"]["runs-on"], ["self-hosted", "linux", "gpu"])

    def test_fork_execution_guard_on_self_hosted_dependency_review(self):
        # 1. fork_guard: True on self-hosted
        wf = {
            "template": "dependency-review",
            "template_version": 1,
            "filename": "dependency-review.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "action_sha": DEFAULT_DEPENDENCY_SHA,
            "runs_on": "self-hosted",
            "fork_guard": True,
        }
        rendered = render(wf).decode("utf-8")
        if yaml is not None:
            parsed = yaml.safe_load(rendered)
            self.assertEqual(
                parsed["jobs"]["dependency-review"]["if"],
                "github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository",
            )

        # 2. is_public: True on self-hosted without explicit fork_guard
        wf_pub = {
            "template": "dependency-review",
            "template_version": 1,
            "filename": "dependency-review.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "action_sha": DEFAULT_DEPENDENCY_SHA,
            "runs_on": "self-hosted",
            "is_public": True,
        }
        rendered_pub = render(wf_pub).decode("utf-8")
        if yaml is not None:
            parsed_pub = yaml.safe_load(rendered_pub)
            self.assertEqual(
                parsed_pub["jobs"]["dependency-review"]["if"],
                "github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository",
            )

        # 3. Not applied on hosted runner (ubuntu-24.04) even if fork_guard is True
        wf_hosted = {
            "template": "dependency-review",
            "template_version": 1,
            "filename": "dependency-review.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "action_sha": DEFAULT_DEPENDENCY_SHA,
            "runs_on": "ubuntu-24.04",
            "fork_guard": True,
        }
        rendered_hosted = render(wf_hosted).decode("utf-8")
        if yaml is not None:
            parsed_hosted = yaml.safe_load(rendered_hosted)
            self.assertNotIn("if", parsed_hosted["jobs"]["dependency-review"])

        # 4. Not applied on private repo when fork_guard is False
        wf_priv = {
            "template": "dependency-review",
            "template_version": 1,
            "filename": "dependency-review.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "action_sha": DEFAULT_DEPENDENCY_SHA,
            "runs_on": "self-hosted",
            "fork_guard": False,
        }
        rendered_priv = render(wf_priv).decode("utf-8")
        if yaml is not None:
            parsed_priv = yaml.safe_load(rendered_priv)
            self.assertNotIn("if", parsed_priv["jobs"]["dependency-review"])

    def test_interactive_dependency_review_prompts_custom_options(self):
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
        # 1. confirm workflows: y
        # 2. use self-hosted: default Yes (\n)
        # 3. runner labels: default self-hosted (\n)
        # 4. select workflows: 2 (dependency review)
        # 5. fail_on_severity: critical
        # 6. allow_licenses: Apache-2.0, MIT
        # 7. deny_licenses: GPL-3.0
        # 8. comment_summary_in_pr: always
        in_stream = io.StringIO("y\n\n\n2\ncritical\nApache-2.0, MIT\nGPL-3.0\nalways\n")
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(session=session, in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(len(stage["workflows"]), 1)
        wf = stage["workflows"][0]
        self.assertEqual(wf["template"], "dependency-review")
        self.assertEqual(wf["fail_on_severity"], "critical")
        self.assertEqual(wf["allow_licenses"], "Apache-2.0, MIT")
        self.assertEqual(wf["deny_licenses"], "GPL-3.0")
        self.assertEqual(wf["comment_summary_in_pr"], "always")
        self.assertEqual(wf["runs_on"], "self-hosted")

        rendered = render(wf).decode("utf-8")
        if yaml is not None:
            parsed = yaml.safe_load(rendered)
            step = parsed["jobs"]["dependency-review"]["steps"][1]
            self.assertEqual(step["with"]["fail-on-severity"], "critical")
            self.assertEqual(step["with"]["allow-licenses"], "Apache-2.0, MIT")
            self.assertEqual(step["with"]["deny-licenses"], "GPL-3.0")
            self.assertEqual(step["with"]["comment-summary-in-pr"], "always")

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
        # Confirm: y, Self-hosted: default, Labels: default, Choice: 2, Severity: default, Allow: default, Deny: default, Comment: default
        in_stream = io.StringIO("y\n\n\n2\n\n\n\n\n")
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(session=session, in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(len(stage["workflows"]), 1)
        self.assertEqual(stage["workflows"][0]["template"], "dependency-review")
        self.assertEqual(stage["workflows"][0]["fail_on_severity"], "high")
        self.assertIn("MIT", stage["workflows"][0]["allow_licenses"])
        self.assertEqual(stage["workflows"][0]["runs_on"], "self-hosted")

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
