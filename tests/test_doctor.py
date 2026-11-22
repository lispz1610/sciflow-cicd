"""Tests for sciflow-cicd doctor command and environment diagnostics."""

import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
TESTS_DIR = ROOT_DIR / "tests"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

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
from runner_api_support import RunnerInventoryApiMixin


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


class MockDoctorApi(RunnerInventoryApiMixin):
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
        runner_total_count=None,
        runner_status=200,
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
        self.init_runner_inventory(total_count=runner_total_count, status=runner_status)
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
        runner_response = self.runner_inventory_response(path)
        if runner_response is not None:
            return runner_response
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
            filename = path.split("/")[-1].split("?")[0]
            if self.workflows and filename in self.workflows:
                content_bytes = self.workflows[filename].encode("utf-8")
                return 200, {
                    "name": filename,
                    "path": f".github/workflows/{filename}",
                    "content": import_base64().b64encode(content_bytes).decode("ascii"),
                    "encoding": "base64",
                    "type": "file",
                    "sha": "a" * 40,
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
                "type": "file",
                "sha": "b" * 40,
            }
        return 404, {"message": f"mock route not found: {path}"}


def import_base64():
    import base64
    return base64


class DoctorRepositoryAuditTests(unittest.TestCase):
    """Tests for sciflow-cicd doctor auditing repository controls via inventory inspection."""

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

    def test_self_hosted_runner_advisory_is_non_blocking(self):
        """Zero registered Self-Hosted Runners is reported as an advisory, not a failure."""
        self_hosted_wf = (
            "# sciflow-cicd workflow-template=pr-ci version=1\n"
            "name: PR CI\n"
            "permissions:\n"
            "  contents: read\n"
            "jobs:\n"
            "  ci:\n"
            "    runs-on: self-hosted\n"
            "    steps:\n"
            "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n"
        )
        dep_yml = (
            "version: 2\nupdates:\n"
            "  - package-ecosystem: 'pip'\n    directory: '/'\n"
            "    schedule:\n      interval: 'monthly'\n"
        )
        api = MockDoctorApi(
            visibility="public", workflows={"ci.yml": self_hosted_wf},
            dependabot=dep_yml, runner_total_count=0,
        )
        out = io.StringIO()
        code = run_doctor(out_stream=out, repo="acme/project", api=api)

        self.assertEqual(code, 0)
        self.assertEqual(api.writes, [])
        report = out.getvalue()
        self.assertIn("[ADVISORY] Self-Hosted Runner Availability", report)
        self.assertIn("0 registered Self-Hosted Runners", report)
        self.assertIn("register a Self-Hosted Runner", report)
        self.assertIn("ubuntu-24.04", report)
        self.assertIn("Verdict: System is healthy", report)
        self.assertIn("/repos/acme/project/actions/runners", api.reads)

    def test_runner_inventory_access_limits_are_informational_in_doctor(self):
        """HTTP 403 and 404 from the runner inventory never turn Doctor into a failing audit."""
        dep_yml = (
            "version: 2\nupdates:\n"
            "  - package-ecosystem: 'pip'\n    directory: '/'\n"
            "    schedule:\n      interval: 'monthly'\n"
        )
        for status in (403, 404):
            with self.subTest(status=status):
                api = MockDoctorApi(
                    visibility="public", dependabot=dep_yml, runner_status=status,
                    workflows={"ci.yml": (
                        "# sciflow-cicd workflow-template=pr-ci version=1\n"
                        "name: PR CI\npermissions:\n  contents: read\n"
                        "jobs:\n  ci:\n    runs-on: self-hosted\n"
                        "    steps:\n"
                        "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n"
                    )},
                )
                out = io.StringIO()
                code = run_doctor(out_stream=out, repo="acme/project", api=api)

                self.assertEqual(code, 0)
                self.assertEqual(api.writes, [])
                report = out.getvalue()
                self.assertIn("[ADVISORY] Self-Hosted Runner Availability", report)
                self.assertIn(f"HTTP {status}", report)
                self.assertIn("Advisory only", report)
                self.assertIn("Verdict: System is healthy", report)

    def test_hosted_runner_workflows_do_not_query_runner_inventory(self):
        """Hosted Runner workflows never trigger a runner-inventory query in Doctor."""
        api = MockDoctorApi(visibility="public", workflows={"ci.yml": (
            "# sciflow-cicd workflow-template=pr-ci version=1\n"
            "name: PR CI\njobs:\n  ci:\n    runs-on: ubuntu-24.04\n"
            "    steps:\n"
            "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n"
        )})
        out = io.StringIO()
        code = run_doctor(out_stream=out, repo="acme/project", api=api)

        self.assertEqual(code, 1)
        self.assertNotIn("/repos/acme/project/actions/runners", api.reads)
        self.assertNotIn("Self-Hosted Runner Availability", out.getvalue())

    def test_ocr_workflow_does_not_query_runner_inventory(self):
        """Self-hosted OCR workflows never trigger a runner-inventory query in Doctor."""
        ocr_wf = (
            "# sciflow-cicd workflow-template=open-code-review version=1\n"
            "name: Open Code Review\npermissions:\n  contents: read\n"
            "jobs:\n  review:\n    runs-on: self-hosted\n"
            "    steps:\n"
            "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n"
        )
        api = MockDoctorApi(
            visibility="private", workflows={"open-code-review.yml": ocr_wf},
            runner_total_count=0,
        )
        out = io.StringIO()
        code = run_doctor(out_stream=out, repo="acme/project", api=api)

        self.assertEqual(api.writes, [])
        self.assertNotIn("/repos/acme/project/actions/runners", api.reads)
        self.assertNotIn("Self-Hosted Runner Availability", out.getvalue())
        self.assertEqual(code, 1)

    def test_review_stage_policy_does_not_query_runner_inventory(self):
        """A self-hosted review stage never triggers a runner-inventory query in Doctor."""
        api = MockDoctorApi(visibility="private", workflows={}, runner_total_count=0)
        policy = {"version": 2, "stages": [{"name": "review", "workflow": {"runs_on": "self-hosted"}}]}
        result = audit_repository_controls(api, "acme/project", policy=policy)

        self.assertNotIn("/repos/acme/project/actions/runners", api.reads)
        self.assertFalse(any(control["name"] == "self_hosted_runner_availability"
                             for control in result["controls"]))

    def test_canonical_workflow_fallback_when_directory_listing_is_unavailable(self):
        class FallbackApi(MockDoctorApi):
            def request(self, method, path, payload=None):
                if method == "GET" and path == "/repos/acme/project/contents/.github/workflows":
                    return 404, {"message": "Not Found"}
                return super().request(method, path, payload)

        api = FallbackApi(
            visibility="public",
            workflows={"ci.yml": (
                "# sciflow-cicd workflow-template=pr-ci version=1\n"
                "name: PR CI\npermissions:\n  contents: read\n"
                "jobs:\n  ci:\n    runs-on: self-hosted\n"
                "    steps:\n"
                "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n"
            )},
            runner_total_count=0,
        )
        result = audit_repository_controls(api, "acme/project")
        controls = {control["name"]: control for control in result["controls"]}

        self.assertIn("/repos/acme/project/actions/runners", api.reads)
        self.assertEqual(controls["self_hosted_runner_availability"]["status"], "advisory")

    def test_self_hosted_pr_ci_workflow_emits_runner_advisory(self):
        """An active self-hosted pr-ci.yml is discovered and emits a non-blocking advisory."""
        pr_ci_wf = (
            "# sciflow-cicd workflow-template=pr-ci version=1\n"
            "name: PR CI\npermissions:\n  contents: read\n"
            "jobs:\n  ci:\n    runs-on: self-hosted\n"
            "    steps:\n"
            "      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683\n"
        )
        dep_yml = (
            "version: 2\nupdates:\n"
            "  - package-ecosystem: 'pip'\n    directory: '/'\n"
            "    schedule:\n      interval: 'monthly'\n"
        )
        api = MockDoctorApi(
            visibility="public", workflows={"pr-ci.yml": pr_ci_wf},
            dependabot=dep_yml, runner_total_count=0,
        )
        out = io.StringIO()
        code = run_doctor(out_stream=out, repo="acme/project", api=api)

        self.assertEqual(code, 0)
        self.assertEqual(api.writes, [])
        self.assertIn("/repos/acme/project/actions/runners", api.reads)
        report = out.getvalue()
        self.assertIn("[ADVISORY] Self-Hosted Runner Availability", report)
        self.assertIn("0 registered Self-Hosted Runners", report)
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


class ResolveGitHubTokenTests(unittest.TestCase):
    """Tests for unified GitHub token resolution."""

    def test_explicit_token_wins_and_strips(self):
        from scripts.configure_github_repo import resolve_github_token

        with patch.dict(os.environ, {"GITHUB_TOKEN": "env-tok", "GH_TOKEN": "gh-tok"}, clear=True):
            self.assertEqual(resolve_github_token(explicit_token="  explicit-tok  "), "explicit-tok")
            self.assertIsNone(resolve_github_token(explicit_token="   "))

    def test_custom_token_env_fails_closed_without_fallback(self):
        from scripts.configure_github_repo import resolve_github_token

        with patch.dict(os.environ, {"GITHUB_TOKEN": "gh-default", "GH_TOKEN": "gh-fallback"}, clear=True), \
                patch("subprocess.run") as mock_sub:
            self.assertIsNone(resolve_github_token(token_env="CUSTOM_ADMIN_TOKEN"))
            mock_sub.assert_not_called()

        with patch.dict(os.environ, {"CUSTOM_ADMIN_TOKEN": "  custom-tok  "}, clear=True):
            self.assertEqual(resolve_github_token(token_env="CUSTOM_ADMIN_TOKEN"), "custom-tok")

    def test_default_token_env_falls_back_github_to_gh_to_cli(self):
        from scripts.configure_github_repo import resolve_github_token

        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok1", "GH_TOKEN": "tok2"}, clear=True):
            self.assertEqual(resolve_github_token(), "tok1")

        with patch.dict(os.environ, {"GH_TOKEN": "tok2"}, clear=True):
            self.assertEqual(resolve_github_token(), "tok2")

        mock_proc = MagicMock(returncode=0, stdout="  cli-tok \n")
        with patch.dict(os.environ, {}, clear=True), \
                patch("subprocess.run", return_value=mock_proc) as mock_sub:
            self.assertEqual(resolve_github_token(), "cli-tok")
            mock_sub.assert_called_once()

        mock_fail = MagicMock(returncode=1, stdout="ignored")
        with patch.dict(os.environ, {}, clear=True), \
                patch("subprocess.run", return_value=mock_fail):
            self.assertIsNone(resolve_github_token())


class DoctorContractHardeningTests(unittest.TestCase):
    """Tests for #78 doctor --policy, --json local mode, error summary, and fail-closed pins."""

    def test_run_doctor_policy_without_repo_resolves_target_from_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "policy.json"
            policy_path.write_text(json.dumps({"version": 1, "repository": "acme/project", "stages": []}), encoding="utf-8")
            api = MockDoctorApi()
            out = io.StringIO()
            err = io.StringIO()
            rc = run_doctor(out_stream=out, err_stream=err, policy_path=str(policy_path), json_output=True, api=api)
            self.assertIn(rc, (0, 1), err.getvalue())
            data = json.loads(out.getvalue())
            self.assertEqual(data["repository"], "acme/project")

    def test_doctor_and_interactive_and_headless_token_wiring(self):
        from scripts.interactive import init_interactive_session
        from scripts.headless import run_headless_apply

        # Custom --token-env in run_doctor fails closed even when GITHUB_TOKEN is set
        out = io.StringIO()
        err = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_TOKEN": "default-tok", "GH_TOKEN": "gh-tok"}, clear=True):
            rc = run_doctor(out_stream=out, err_stream=err, repo="acme/project", token_env="CUSTOM_TOK")
        self.assertEqual(rc, 1)
        self.assertIn("CUSTOM_TOK is not set", err.getvalue())

        # Default token_env in run_doctor falls back to gh auth token
        mock_proc = MagicMock(returncode=0, stdout="cli-tok\n")
        with patch.dict(os.environ, {}, clear=True), \
                patch("subprocess.run", return_value=mock_proc), \
                patch("scripts.configure_github_repo.GitHubApi", return_value=MockDoctorApi()) as mock_api_cls:
            rc = run_doctor(out_stream=io.StringIO(), err_stream=io.StringIO(), repo="acme/project", json_output=True)
        self.assertIn(rc, (0, 1))
        mock_api_cls.assert_called_once_with("cli-tok")

        # Interactive setup resolves GH_TOKEN without GITHUB_TOKEN
        with patch.dict(os.environ, {"GH_TOKEN": "gh-interactive-tok"}, clear=True), \
                patch("scripts.interactive.GitHubApi") as mock_api_cls, \
                patch("scripts.interactive.prompt_target_repository", return_value=None):
            init_interactive_session(in_stream=io.StringIO(""), out_stream=io.StringIO(), err_stream=io.StringIO())
        mock_api_cls.assert_called_once_with("gh-interactive-tok")

        # Headless mode resolves GH_TOKEN without GITHUB_TOKEN
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps({"version": 1, "repository": "acme/project", "stages": [{"name": "actions"}]}))
            policy_path = f.name
        try:
            with patch.dict(os.environ, {"GH_TOKEN": "gh-headless-tok"}, clear=True), \
                    patch("scripts.headless.GitHubApi") as mock_api_cls, \
                    patch("scripts.headless.inventory", return_value={}), \
                    patch("scripts.headless.run_policy", return_value={"mode": "dry-run", "changes": []}):
                rc = run_headless_apply(policy_path, apply=False)
            self.assertEqual(rc, 0)
            mock_api_cls.assert_called_once_with("gh-headless-tok")
        finally:
            if os.path.exists(policy_path):
                os.remove(policy_path)

    def test_run_doctor_policy_without_repository_field_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            policy_path = Path(tmpdir) / "policy.json"
            policy_path.write_text(json.dumps({"version": 1, "stages": []}), encoding="utf-8")
            out = io.StringIO()
            err = io.StringIO()
            rc = run_doctor(out_stream=out, err_stream=err, policy_path=str(policy_path), api=MockDoctorApi())
            self.assertEqual(rc, 1)
            self.assertIn("Repository", err.getvalue())

    def test_run_doctor_local_mode_json_output(self):
        out = io.StringIO()
        err = io.StringIO()
        with patch("scripts.doctor.check_python_version", return_value=DiagnosticResult("Python Version", True, "Python 3.12", True)), \
                patch("scripts.doctor.check_git_installed", return_value=DiagnosticResult("Git Installation", True, "git 2.40", True)), \
                patch("scripts.doctor.check_gh_installed_and_authenticated", return_value=DiagnosticResult("GitHub CLI (gh)", True, "ok", True)), \
                patch("scripts.doctor.check_path_precedence", return_value=DiagnosticResult("PATH Configuration", True, "ok", False)):
            rc = run_doctor(out_stream=out, err_stream=err, json_output=True)
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["verdict"], "healthy")
        self.assertEqual(len(payload["checks"]), 4)
        self.assertTrue(all(c["passed"] for c in payload["checks"]))

    def test_repository_access_error_summary_includes_advisory_zero(self):
        class BrokenApi:
            def request(self, method, path):
                raise RuntimeError("HTTP 403 Forbidden")

        res = audit_repository_controls(BrokenApi(), "acme/project")
        self.assertEqual(res["verdict"], "action_required")
        self.assertIn("advisory", res["summary"])
        self.assertEqual(res["summary"]["advisory"], 0)

    def test_audit_action_sha_pins_fails_closed_on_download_or_decode_error(self):
        class DownloadFailApi(MockDoctorApi):
            def request(self, method, path):
                if path == "/repos/acme/project/contents/.github/workflows/ci.yml":
                    return 500, {"message": "Internal Server Error"}
                return super().request(method, path)

        api_dl = DownloadFailApi(workflows={"ci.yml": "permissions: {}\n"})
        res_dl = audit_action_sha_pins(api_dl, "acme/project")
        self.assertEqual(res_dl["status"], "missing")
        self.assertIn("500", res_dl["reason"])

        class ForbiddenFileApi(MockDoctorApi):
            def request(self, method, path):
                if path == "/repos/acme/project/contents/.github/workflows/ci.yml":
                    return 403, {"message": "Forbidden"}
                return super().request(method, path)

        api_403 = ForbiddenFileApi(workflows={"ci.yml": "permissions: {}\n"})
        res_403 = audit_action_sha_pins(api_403, "acme/project")
        self.assertEqual(res_403["status"], "inaccessible")

        class BadDecodeApi(MockDoctorApi):
            def request(self, method, path):
                if path == "/repos/acme/project/contents/.github/workflows/ci.yml":
                    return 200, {"content": "!!!not-valid-base64!!!"}
                return super().request(method, path)

        api_bad_b64 = BadDecodeApi(workflows={"ci.yml": "permissions: {}\n"})
        res_bad_b64 = audit_action_sha_pins(api_bad_b64, "acme/project")
        self.assertEqual(res_bad_b64["status"], "missing")
        self.assertIn("decode", res_bad_b64["reason"].lower())

        class BadUtf8Api(MockDoctorApi):
            def request(self, method, path):
                if path == "/repos/acme/project/contents/.github/workflows/ci.yml":
                    import base64 as _b64
                    return 200, {"content": _b64.b64encode(b"\xff\xfe\xfd").decode("ascii")}
                return super().request(method, path)

        api_bad_utf8 = BadUtf8Api(workflows={"ci.yml": "permissions: {}\n"})
        res_bad_utf8 = audit_action_sha_pins(api_bad_utf8, "acme/project")
        self.assertEqual(res_bad_utf8["status"], "missing")
        self.assertIn("decode", res_bad_utf8["reason"].lower())


if __name__ == "__main__":
    unittest.main()
