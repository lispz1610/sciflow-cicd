"""Tests for least-privilege workflow token permissions and Action SHA-pin enforcement (#140)."""

import os
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS_DIR = str(Path(__file__).resolve().parents[1] / "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from configure_github_repo import ApiError, reconcile_actions, reconcile_actions_policy
import install_github_workflows as installer
from install_github_workflows import (
    DEFAULT_CHECKOUT_SHA,
    DEFAULT_DEPENDENCY_SHA,
    DEFAULT_SETUP_PYTHON_SHA,
    render,
)
from lint_workflow_action_pins import (
    audit_workflow_content,
    audit_workflow_permissions,
    audit_workflows,
)
import setup_github_repo as setup


class FakeActionsApi:
    """Mock API for Actions permissions and workflow token settings."""

    def __init__(self, default_perm="write", can_approve=True):
        self.default_perm = default_perm
        self.can_approve = can_approve
        self.writes = []

    def request(self, method, path, payload=None):
        self.writes.append((method, path, payload))
        if method == "GET":
            if path == "/repos/acme/project":
                return 200, {
                    "full_name": "acme/project",
                    "visibility": "public",
                    "default_branch": "main",
                    "permissions": {"admin": True, "push": True, "pull": True},
                }
            if path == "/repos/acme/project/actions/permissions":
                return 200, {
                    "enabled": True,
                    "allowed_actions": "all",
                    "sha_pinning_required": False,
                }
            if path == "/repos/acme/project/actions/permissions/workflow":
                return 200, {
                    "default_workflow_permissions": self.default_perm,
                    "can_approve_pull_request_reviews": self.can_approve,
                }
        elif method == "PUT":
            if path == "/repos/acme/project/actions/permissions/workflow":
                if "default_workflow_permissions" in payload:
                    self.default_perm = payload["default_workflow_permissions"]
                if "can_approve_pull_request_reviews" in payload:
                    self.can_approve = payload["can_approve_pull_request_reviews"]
                return 200, {
                    "default_workflow_permissions": self.default_perm,
                    "can_approve_pull_request_reviews": self.can_approve,
                }
            return 200, {}
        return 404, {}


class WorkflowSecurityControlsTests(unittest.TestCase):
    """Test suite for P3.4 Least-privilege permissions and Action SHA-pin enforcement."""

    def test_rendered_workflows_include_inline_version_comments(self):
        wf = {
            "template": "dependency-review",
            "template_version": 1,
            "filename": "dependency-review.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "action_sha": DEFAULT_DEPENDENCY_SHA,
        }
        rendered = render(wf).decode("utf-8")
        self.assertIn(f"actions/checkout@{DEFAULT_CHECKOUT_SHA} # v4.2.2", rendered)
        self.assertIn(f"actions/dependency-review-action@{DEFAULT_DEPENDENCY_SHA} # v4.8.0", rendered)

    def test_profile_workflows_include_inline_version_comments(self):
        wf = {
            "template": "profile-ci",
            "template_version": 1,
            "profile": "python",
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "setup_python_sha": DEFAULT_SETUP_PYTHON_SHA,
            "profile_options": {"package_manager": "pip", "python_versions": ["3.11"]},
        }
        rendered = installer.render(wf).decode("utf-8")
        self.assertIn(f"actions/checkout@{DEFAULT_CHECKOUT_SHA} # v4.2.2", rendered)
        self.assertIn(f"actions/setup-python@{DEFAULT_SETUP_PYTHON_SHA} # v6.3.0", rendered)

    def test_rendered_workflows_declare_explicit_job_level_least_privilege_permissions(self):
        for template in ("pr-ci", "dependency-review"):
            wf = {
                "template": template,
                "template_version": 1,
                "filename": "ci.yml" if template == "pr-ci" else "dep.yml",
                "triggers": ["pull_request"],
                "checkout_sha": DEFAULT_CHECKOUT_SHA,
            }
            if template == "pr-ci":
                wf["commands"] = ["pytest"]
            else:
                wf["action_sha"] = DEFAULT_DEPENDENCY_SHA
            rendered = render(wf).decode("utf-8")
            self.assertIn("permissions:\n  contents: read", rendered)
            self.assertIn("    permissions:\n      contents: read", rendered)

    def test_linter_rejects_missing_permission_declarations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            wf_path = Path(temp_dir) / "no_perms.yml"
            wf_path.write_text(
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-24.04\n"
                "    steps:\n"
                "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n",
                encoding="utf-8",
            )
            findings = audit_workflow_permissions(temp_dir)
            self.assertEqual(len(findings), 1)
            self.assertIn("missing explicit permissions declaration", findings[0])

    def test_linter_rejects_broad_write_all_permissions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            wf_path = Path(temp_dir) / "write_all.yml"
            wf_path.write_text(
                "permissions: write-all\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-24.04\n"
                "    steps:\n"
                "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n",
                encoding="utf-8",
            )
            findings = audit_workflow_permissions(temp_dir)
            self.assertEqual(len(findings), 1)
            self.assertIn("overly broad permissions 'write-all'", findings[0])

    def test_linter_accepts_properly_declared_minimal_permissions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            wf_path = Path(temp_dir) / "valid.yml"
            wf_path.write_text(
                "permissions:\n"
                "  contents: read\n"
                "jobs:\n"
                "  test:\n"
                "    permissions:\n"
                "      contents: read\n"
                "    runs-on: ubuntu-24.04\n"
                "    steps:\n"
                "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n",
                encoding="utf-8",
            )
            findings = audit_workflow_permissions(temp_dir)
            self.assertEqual(findings, [])

    def test_actions_policy_can_set_default_workflow_permissions_to_read(self):
        api = FakeActionsApi(default_perm="write", can_approve=True)
        changes = reconcile_actions_policy(
            api,
            "acme/project",
            {"workflow_permissions": "read"},
            apply=True,
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0][0], "PUT")
        self.assertEqual(changes[0][1], "/repos/acme/project/actions/permissions/workflow")
        self.assertEqual(changes[0][2]["default_workflow_permissions"], "read")
        self.assertEqual(changes[0][2]["can_approve_pull_request_reviews"], False)
        self.assertEqual(api.default_perm, "read")
        self.assertFalse(api.can_approve)

    def test_actions_policy_rejects_invalid_workflow_permissions(self):
        api = FakeActionsApi()
        with self.assertRaisesRegex(ValueError, "workflow_permissions must be 'read' or 'write'"):
            reconcile_actions_policy(
                api,
                "acme/project",
                {"workflow_permissions": "invalid-permission"},
            )

    def test_policy_dry_run_reflects_workflow_permissions_change(self):
        api = FakeActionsApi(default_perm="write", can_approve=True)
        policy = {
            "version": 1,
            "repository": "acme/project",
            "stages": [
                {
                    "name": "actions-policy",
                    "settings": {"workflow_permissions": "read"},
                }
            ],
        }
        result = setup.run_policy(api, "acme/project", policy, apply=False)
        changes = result.get("changes", [])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["stage"], "actions-policy")
        self.assertEqual(changes[0]["path"], "/repos/acme/project/actions/permissions/workflow")
        self.assertEqual(changes[0]["desired"]["default_workflow_permissions"], "read")
        self.assertEqual(changes[0]["status"], "planned")

    def test_audit_workflows_enforces_permissions_by_default(self):
        """audit_workflows checks least-privilege permissions by default."""
        with tempfile.TemporaryDirectory() as temporary:
            wf_dir = Path(temporary)
            (wf_dir / "ci.yml").write_text(
                "jobs:\n"
                "  build:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567\n",
                encoding="utf-8",
            )
            findings = audit_workflows(wf_dir)
            self.assertTrue(any("missing explicit permissions declaration" in f for f in findings))

    def test_audit_workflow_content_rejects_missing_permissions_and_floating_pins(self):
        """audit_workflow_content helper validates both pinning and permissions."""
        content = (
            "jobs:\n"
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
        )
        violations = audit_workflow_content(content, "ci.yml")
        self.assertTrue(any("missing explicit permissions declaration" in v for v in violations))
        self.assertTrue(any("unsafe uses reference" in v for v in violations))

    def test_repo_trust_boundary_workflows_scope_permissions_and_persist_credentials(self):
        """Trust-boundary workflows use persist-credentials: false and job-scoped permissions."""
        import yaml

        wf_dir = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        isg = yaml.safe_load((wf_dir / "issue-state-gate.yml").read_text(encoding="utf-8"))
        checkout_steps = [
            s for s in isg["jobs"]["evaluate-implementation-issues"]["steps"]
            if "actions/checkout" in s.get("uses", "")
        ]
        self.assertTrue(checkout_steps)
        for step in checkout_steps:
            self.assertIs(step.get("with", {}).get("persist-credentials"), False)

        for wf_name in ("release-promotion.yml", "wiki-sync.yml", "workflow-security-audit.yml"):
            wf = yaml.safe_load((wf_dir / wf_name).read_text(encoding="utf-8"))
            top_perms = wf.get("permissions", {})
            self.assertNotIn("write", top_perms.values(), f"{wf_name} should not grant write at top-level")
            for job_name, job in wf.get("jobs", {}).items():
                self.assertIn("permissions", job, f"{wf_name} job {job_name} must declare explicit permissions")


if __name__ == "__main__":
    unittest.main()
