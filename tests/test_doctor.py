"""Tests for gh-setup doctor command and environment diagnostics."""

import io
import os
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from scripts.doctor import (
    DiagnosticResult,
    check_python_version,
    check_git_installed,
    check_gh_installed_and_authenticated,
    check_path_precedence,
    run_doctor,
    audit_merge_controls,
    audit_workflow_permissions,
    audit_action_sha_pins,
    audit_dependabot_config,
    audit_security_capabilities,
    audit_repository_controls,
)


class DoctorDiagnosticsTests(unittest.TestCase):
    def test_python_version_check(self):
        res = check_python_version()
        self.assertTrue(res.passed)
        self.assertIn("Python", res.message)

    @patch("shutil.which", return_value="/usr/bin/git")
    @patch("subprocess.run")
    def test_git_installed_check_success(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=0, stdout="git version 2.40.0\n")
        res = check_git_installed()
        self.assertTrue(res.passed)
        self.assertIn("git version 2.40.0", res.message)

    @patch("shutil.which", return_value=None)
    def test_git_missing_check_fails(self, mock_which):
        res = check_git_installed()
        self.assertFalse(res.passed)
        self.assertIn("git was not found", res.message)

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("subprocess.run")
    @patch.dict(os.environ, {}, clear=True)
    def test_gh_authenticated_check_success(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=0, stdout="Logged in to github.com\n")
        res = check_gh_installed_and_authenticated()
        self.assertTrue(res.passed)
        self.assertIn("authenticated", res.message)

    @patch("shutil.which", return_value=None)
    def test_gh_missing_check_fails(self, mock_which):
        res = check_gh_installed_and_authenticated()
        self.assertFalse(res.passed)
        self.assertIn("GitHub CLI (gh) was not found", res.message)

    @patch("shutil.which", return_value="/usr/bin/gh")
    @patch("subprocess.run")
    @patch.dict(os.environ, {}, clear=True)
    def test_gh_unauthenticated_fails(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(returncode=1, stderr="You are not logged in\n")
        res = check_gh_installed_and_authenticated()
        self.assertFalse(res.passed)
        self.assertIn("not authenticated", res.message)

    def test_path_precedence_check(self):
        bin_dir = Path("/custom/bin")
        expected_binary = bin_dir / "sciflow-cicd"

        with patch.dict(os.environ, {"PATH": "/custom/bin:/usr/bin"}):
            with patch("shutil.which", return_value=str(expected_binary)):
                res = check_path_precedence(bin_dir)
                self.assertTrue(res.passed)
                self.assertIn("active in PATH", res.message)

            with patch("shutil.which", return_value="/usr/bin/sciflow-cicd"):
                res = check_path_precedence(bin_dir)
                self.assertFalse(res.passed)
                self.assertIn("shadowed by /usr/bin/sciflow-cicd", res.message)

            res = check_path_precedence(Path("/nonexistent/bin"))
            self.assertFalse(res.passed)
            self.assertIn("not in your PATH", res.message)

    @patch("scripts.doctor.check_gh_installed_and_authenticated")
    @patch("scripts.doctor.check_git_installed")
    @patch("scripts.doctor.check_python_version")
    def test_run_doctor_all_passed(self, mock_py, mock_git, mock_gh):
        mock_py.return_value = DiagnosticResult("Python Version", True, "OK")
        mock_git.return_value = DiagnosticResult("Git Installation", True, "OK")
        mock_gh.return_value = DiagnosticResult("GitHub CLI (gh)", True, "OK")

        out = io.StringIO()
        err = io.StringIO()
        code = run_doctor(out_stream=out, err_stream=err)
        self.assertEqual(code, 0)
        self.assertIn("Verdict: System is healthy", out.getvalue())

    @patch("scripts.doctor.check_gh_installed_and_authenticated")
    @patch("scripts.doctor.check_git_installed")
    @patch("scripts.doctor.check_python_version")
    def test_run_doctor_fails_when_fatal_check_fails(self, mock_py, mock_git, mock_gh):
        mock_py.return_value = DiagnosticResult("Python Version", True, "OK")
        mock_git.return_value = DiagnosticResult("Git Installation", True, "OK")
        mock_gh.return_value = DiagnosticResult("GitHub CLI (gh)", False, "Not logged in", is_fatal=True)

        out = io.StringIO()
        err = io.StringIO()
        code = run_doctor(out_stream=out, err_stream=err)
        self.assertEqual(code, 1)
        self.assertIn("Verdict: Issues detected", err.getvalue())


class MockDoctorApi:
    """Mock GitHub API strictly recording reads and writes."""

    def __init__(
        self,
        visibility="public",
        plan_name="free",
        admin=True,
        merge_settings=None,
        workflow_permissions=None,
        workflows=None,
        dependabot=None,
        codeql_state="configured",
        rulesets=None,
        forbidden_paths=None,
        secret_scanning_enabled=None,
    ):
        self.visibility = visibility
        self.plan_name = plan_name
        self.admin = admin
        self.merge_settings = merge_settings or {
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": False,
            "delete_branch_on_merge": True,
            "allow_update_branch": True,
        }
        self.workflow_permissions = workflow_permissions or {
            "default_workflow_permissions": "read",
            "can_approve_pull_request_reviews": False,
        }
        self.workflows = workflows
        self.dependabot = dependabot
        self.codeql_state = codeql_state
        self.secret_scanning_enabled = True if secret_scanning_enabled is None else secret_scanning_enabled
        self.rulesets = rulesets if rulesets is not None else [{"id": 1, "name": "quality-gate"}]
        self.forbidden_paths = set(forbidden_paths or [])
        self.reads = []
        self.writes = []

    def request(self, method, path, payload=None):
        if method != "GET":
            self.writes.append((method, path, payload))
            return 200, {}

        self.reads.append(path)
        if path in self.forbidden_paths:
            from configure_github_repo import ApiError
            raise ApiError(f"GET {path}: HTTP 403; forbidden")

        if path in ("/repos/acme/project", "repos/acme/project"):
            sec = {"secret_scanning": {"status": "enabled"}} if self.secret_scanning_enabled else {"secret_scanning": {"status": "disabled"}}
            return 200, {
                "name": "project",
                "full_name": "acme/project",
                "visibility": self.visibility,
                "default_branch": "main",
                "permissions": {"admin": self.admin, "push": True, "pull": True},
                "owner": {"login": "acme", "type": "User"},
                "plan": {"name": self.plan_name},
                "security_and_analysis": sec,
                **self.merge_settings,
            }
        if path == "/users/acme":
            return 200, {"login": "acme", "plan": {"name": self.plan_name}}
        if path == "/repos/acme/project/actions/permissions":
            return 200, {"enabled": True}
        if path == "/repos/acme/project/actions/permissions/workflow":
            return 200, dict(self.workflow_permissions)
        if path == "/repos/acme/project/vulnerability-alerts":
            return 204, {}
        if path == "/repos/acme/project/automated-security-fixes":
            return 200, {"enabled": True}
        if path == "/repos/acme/project/private-vulnerability-reporting":
            return 200, {"enabled": False}
        if path == "/repos/acme/project/code-scanning/default-setup":
            if self.visibility == "private" and self.plan_name in ("free", "unknown"):
                return 404, {"message": "Not Found"}
            return 200, {"state": self.codeql_state, "languages": ["python"], "query_suite": "default"}
        if path.startswith("/repos/acme/project/rulesets"):
            return 200, list(self.rulesets)
        if path == "/repos/acme/project/contents/.github/workflows":
            if self.workflows is None:
                return 404, {"message": "Not Found"}
            files = []
            for name in self.workflows:
                files.append({"name": name, "path": f".github/workflows/{name}", "type": "file"})
            return 200, files
        if path.startswith("/repos/acme/project/contents/.github/workflows/"):
            filename = path.split("/")[-1]
            if self.workflows and filename in self.workflows:
                content_bytes = self.workflows[filename].encode("utf-8")
                return 200, {
                    "name": filename,
                    "path": f".github/workflows/{filename}",
                    "content": import_base64().b64encode(content_bytes).decode("ascii"),
                    "encoding": "base64",
                }
            return 404, {"message": "Not Found"}
        if path == "/repos/acme/project/contents/.github/dependabot.yml":
            if self.dependabot is None:
                return 404, {"message": "Not Found"}
            content_bytes = self.dependabot.encode("utf-8")
            return 200, {
                "name": "dependabot.yml",
                "path": ".github/dependabot.yml",
                "content": import_base64().b64encode(content_bytes).decode("ascii"),
                "encoding": "base64",
            }
        return 404, {"message": f"mock route not found: {path}"}


def import_base64():
    import base64
    return base64


class DoctorRepositoryAuditTests(unittest.TestCase):
    """Tests for gh-setup doctor auditing repository controls via inventory inspection."""

    def test_clean_repository_state_all_configured(self):
        """When repository conforms to baseline, all controls report CONFIGURED and zero mutations occur."""
        pinned_wf = (
            "name: CI\n"
            "permissions:\n"
            "  contents: read\n"
            "jobs:\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n"
        )
        dep_yml = (
            "version: 2\n"
            "updates:\n"
            "  - package-ecosystem: 'pip'\n"
            "    directory: '/'\n"
            "    schedule:\n"
            "      interval: 'monthly'\n"
            "    groups:\n"
            "      python-dependencies:\n"
            "        patterns: ['*']\n"
        )
        api = MockDoctorApi(
            visibility="public",
            workflows={"ci.yml": pinned_wf},
            dependabot=dep_yml,
        )

        out = io.StringIO()
        code = run_doctor(out_stream=out, repo="acme/project", api=api)
        self.assertEqual(code, 0)
        self.assertEqual(api.writes, [])  # Strictly read-only: ZERO writes!

        report = out.getvalue()
        self.assertIn("[CONFIGURED] Merge Controls", report)
        self.assertIn("[CONFIGURED] Default Workflow Permissions", report)
        self.assertIn("[CONFIGURED] Action SHA Pins", report)
        self.assertIn("[CONFIGURED] Dependabot Configuration", report)
        self.assertIn("[CONFIGURED] Vulnerability Alerts", report)
        self.assertIn("Verdict: System is healthy", report)

    def test_drifting_and_missing_controls_report_guidance_and_fail(self):
        """When controls are permissive or missing, reports [MISSING] with actionable Settings paths."""
        unpinned_wf = (
            "name: CI\n"
            "permissions: write-all\n"
            "jobs:\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
        )
        api = MockDoctorApi(
            visibility="public",
            merge_settings={"allow_squash_merge": False, "delete_branch_on_merge": False},
            workflow_permissions={"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True},
            workflows={"ci.yml": unpinned_wf},
            dependabot=None,  # Missing dependabot.yml
            rulesets=[],       # Missing rulesets
        )

        out = io.StringIO()
        code = run_doctor(out_stream=out, repo="acme/project", api=api)
        self.assertEqual(code, 1)
        self.assertEqual(api.writes, [])

        report = out.getvalue()
        self.assertIn("[MISSING] Merge Controls", report)
        self.assertIn("Settings > General > Pull Requests", report)
        self.assertIn("[MISSING] Default Workflow Permissions", report)
        self.assertIn("Settings > Actions > General", report)
        self.assertIn("[MISSING] Dependabot Configuration", report)
        self.assertIn(".github/dependabot.yml", report)
        self.assertIn("[MISSING] Branch Rulesets & Merge Gates", report)
        self.assertIn("Settings > Rules", report)
        self.assertIn("Verdict: Action required", report)

    def test_unsupported_controls_on_private_free_repo(self):
        """Private repository on free plan reports CodeQL and Dependency Review as [UNSUPPORTED]."""
        api = MockDoctorApi(
            visibility="private",
            plan_name="free",
            rulesets=[],
        )

        out = io.StringIO()
        code = run_doctor(out_stream=out, repo="acme/project", api=api)
        self.assertEqual(code, 1)  # Missing rulesets causes exit code 1
        self.assertEqual(api.writes, [])

        report = out.getvalue()
        self.assertIn("[UNSUPPORTED] CodeQL Default Setup", report)
        self.assertIn("requires GitHub Advanced Security", report)
        self.assertIn("[UNSUPPORTED] Dependency Review Gate", report)
        self.assertIn("[UNSUPPORTED] Branch Rulesets & Merge Gates", report)

    def test_inaccessible_controls_report_token_guidance(self):
        """HTTP 403 on permission endpoints reports [INACCESSIBLE] with remediation guidance."""
        api = MockDoctorApi(
            visibility="public",
            admin=False,
            forbidden_paths={"/repos/acme/project/actions/permissions/workflow"},
        )

        out = io.StringIO()
        code = run_doctor(out_stream=out, repo="acme/project", api=api)
        self.assertEqual(code, 1)
        self.assertEqual(api.writes, [])

        report = out.getvalue()
        self.assertIn("[INACCESSIBLE] Default Workflow Permissions", report)
        self.assertIn("Actions: read", report)

    def test_json_output_mode(self):
        """When --json is specified, outputs valid structured JSON."""
        api = MockDoctorApi(visibility="public")
        out = io.StringIO()
        code = run_doctor(out_stream=out, repo="acme/project", json_output=True, api=api)
        self.assertEqual(api.writes, [])

        import json
        data = json.loads(out.getvalue())
        self.assertEqual(data["repository"], "acme/project")
        self.assertIn("verdict", data)
        self.assertIn("summary", data)
        self.assertIn("configured", data["summary"])
        self.assertIn("missing", data["summary"])
        self.assertIn("unsupported", data["summary"])
        self.assertIn("inaccessible", data["summary"])
        self.assertIsInstance(data["controls"], list)


class DoctorDecomposedSubcheckersTests(unittest.TestCase):
    """Targeted unit tests for decomposed diagnostic sub-checkers."""

    def test_audit_merge_controls_configured_and_mismatched(self):
        inv_matching = {
            "merge": {
                "allow_squash_merge": True,
                "delete_branch_on_merge": True,
                "allow_merge_commit": False,
                "allow_rebase_merge": False,
            }
        }
        res = audit_merge_controls(inv_matching)
        self.assertEqual(res["status"], "configured")
        self.assertEqual(res["name"], "merge_controls")
        self.assertIn("match desired policy baseline", res["reason"])

        inv_drift = {
            "merge": {
                "allow_squash_merge": False,
                "delete_branch_on_merge": False,
                "allow_merge_commit": True,
                "allow_rebase_merge": False,
            }
        }
        res_drift = audit_merge_controls(inv_drift)
        self.assertEqual(res_drift["status"], "missing")
        self.assertIn("allow_squash_merge", res_drift["reason"])
        self.assertIn("delete_branch_on_merge", res_drift["reason"])

        custom_policy = {"stages": [{"name": "merge", "settings": {"allow_rebase_merge": True}}]}
        res_custom = audit_merge_controls(inv_matching, policy=custom_policy)
        self.assertEqual(res_custom["status"], "missing")
        self.assertIn("allow_rebase_merge", res_custom["reason"])

    def test_audit_workflow_permissions_configured_and_inaccessible(self):
        inv_ok = {
            "workflow_permissions": {
                "default_workflow_permissions": "read",
                "can_approve_pull_request_reviews": False,
            }
        }
        res_ok = audit_workflow_permissions(inv_ok)
        self.assertEqual(res_ok["status"], "configured")
        self.assertEqual(res_ok["name"], "default_workflow_permissions")

        inv_inacc = {"workflow_permissions": {"status": "inaccessible"}}
        res_inacc = audit_workflow_permissions(inv_inacc)
        self.assertEqual(res_inacc["status"], "inaccessible")
        self.assertIn("Actions: read", res_inacc["guidance"])

        inv_perm = {
            "workflow_permissions": {
                "default_workflow_permissions": "write",
                "can_approve_pull_request_reviews": True,
            }
        }
        res_perm = audit_workflow_permissions(inv_perm)
        self.assertEqual(res_perm["status"], "missing")
        self.assertIn("Workflow permissions are permissive", res_perm["reason"])

    def test_audit_action_sha_pins(self):
        wf_pinned = (
            "name: CI\n"
            "permissions:\n"
            "  contents: read\n"
            "jobs:\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n"
        )
        api_ok = MockDoctorApi(workflows={"ci.yml": wf_pinned})
        res_ok = audit_action_sha_pins(api_ok, "acme/project")
        self.assertEqual(res_ok["status"], "configured")
        self.assertEqual(res_ok["name"], "action_sha_pins")

        wf_unpinned = (
            "name: CI\n"
            "permissions:\n"
            "  contents: read\n"
            "jobs:\n"
            "  test:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
        )
        api_bad = MockDoctorApi(workflows={"ci.yml": wf_unpinned})
        res_bad = audit_action_sha_pins(api_bad, "acme/project")
        self.assertEqual(res_bad["status"], "missing")
        self.assertIn("actions/checkout@v4", res_bad["reason"])

        api_missing = MockDoctorApi(workflows=None)
        res_missing = audit_action_sha_pins(api_missing, "acme/project")
        self.assertEqual(res_missing["status"], "missing")
        self.assertIn("No workflows directory", res_missing["reason"])

        api_inacc = MockDoctorApi(forbidden_paths={"/repos/acme/project/contents/.github/workflows"})
        res_inacc = audit_action_sha_pins(api_inacc, "acme/project")
        self.assertEqual(res_inacc["status"], "inaccessible")

    def test_audit_dependabot_config(self):
        dep_good = (
            "version: 2\n"
            "updates:\n"
            "  - package-ecosystem: 'pip'\n"
            "    directory: '/'\n"
            "    schedule:\n"
            "      interval: 'monthly'\n"
            "    groups:\n"
            "      python-dependencies:\n"
            "        patterns: ['*']\n"
        )
        api_good = MockDoctorApi(dependabot=dep_good)
        res_good = audit_dependabot_config(api_good, "acme/project")
        self.assertEqual(res_good["status"], "configured")
        self.assertEqual(res_good["name"], "dependabot")

        api_missing = MockDoctorApi(dependabot=None)
        res_missing = audit_dependabot_config(api_missing, "acme/project")
        self.assertEqual(res_missing["status"], "missing")

        api_inacc = MockDoctorApi(forbidden_paths={"/repos/acme/project/contents/.github/dependabot.yml"})
        res_inacc = audit_dependabot_config(api_inacc, "acme/project")
        self.assertEqual(res_inacc["status"], "inaccessible")

    def test_audit_security_capabilities(self):
        inv_caps = {
            "capabilities": {
                "rulesets": {"status": "configured"},
                "vulnerability_alerts": {"status": "configured"},
                "secret_scanning": {"status": "unsupported"},
                "codeql": {"status": "missing"},
                "dependency_graph": {"status": "configured"},
                "dependency_review": {"status": "inaccessible"},
            }
        }
        res_caps = audit_security_capabilities(inv_caps)
        self.assertEqual(len(res_caps), 6)
        statuses = {c["name"]: c["status"] for c in res_caps}
        self.assertEqual(statuses["branch_rulesets"], "configured")
        self.assertEqual(statuses["vulnerability_alerts"], "configured")
        self.assertEqual(statuses["secret_scanning"], "unsupported")
        self.assertEqual(statuses["codeql"], "missing")
        self.assertEqual(statuses["dependency_graph"], "configured")
        self.assertEqual(statuses["dependency_review"], "inaccessible")


if __name__ == "__main__":
    unittest.main()
