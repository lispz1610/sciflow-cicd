"""Tests for OCR runtime, provider config, competing workflow, and scope preflight."""

import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.ocr_preflight import (
    OcrPreflightError,
    preflight_ocr_runtime,
    format_ocr_preflight_report,
    run_ocr_preflight,
)


class FakePreflightGitHubServer:
    """In-memory fake GitHub API for OCR preflight tests."""

    def __init__(
        self,
        *,
        app_info,
        installations,
        repos_by_installation,
        repo_metadata=None,
        contents_by_repo=None,
        actions_workflows_by_repo=None,
        pulls_by_repo=None,
    ):
        self.app_info = app_info
        self.installations = installations
        self.repos_by_installation = repos_by_installation
        self.repo_metadata = repo_metadata or {}
        self.contents_by_repo = contents_by_repo or {}
        self.actions_workflows_by_repo = actions_workflows_by_repo or {}
        self.pulls_by_repo = pulls_by_repo or {}
        self.calls = []
        self.token_to_meta = {}
        self._seq = 0

    def api_factory(self, bearer_token):
        server = self

        class _Client:
            def request_with_headers(self, method, path, payload=None):
                server.calls.append((bearer_token, method, path, payload))
                if method == "GET" and path == "/app":
                    return 200, {}, server.app_info
                if method == "GET" and path.startswith("/app/installations"):
                    if "page=1" in path or "?" not in path:
                        return 200, {}, server.installations
                    return 200, {}, []
                if method == "POST" and path.startswith("/app/installations/") and path.endswith("/access_tokens"):
                    inst_id = int(path.split("/")[3])
                    server._seq += 1
                    minted = f"ghs_scoped_{inst_id}_{server._seq}"
                    perms = (payload or {}).get("permissions") or {"metadata": "read"}
                    repos = (payload or {}).get("repositories") or []
                    server.token_to_meta[minted] = (inst_id, list(repos))
                    return 201, {}, {
                        "token": minted,
                        "expires_at": "2026-11-14T05:00:00Z",
                        "permissions": perms,
                        "repositories": [{"name": r} for r in repos],
                    }
                if method == "GET" and path.startswith("/installation/repositories"):
                    inst_id, _ = server.token_to_meta.get(bearer_token, (None, []))
                    repos = server.repos_by_installation.get(inst_id, [])
                    if "page=1" in path or "?" not in path:
                        return 200, {}, {"total_count": len(repos), "repositories": repos}
                    return 200, {}, {"total_count": len(repos), "repositories": []}

                if method == "GET" and path.startswith("/repos/"):
                    path_only, _, query = path.partition("?")
                    parts = path_only.split("/")
                    if len(parts) < 4:
                        return 404, {}, {"message": "Not Found"}
                    repo_full = f"{parts[2]}/{parts[3]}"
                    _, scoped_repos = server.token_to_meta.get(bearer_token, (None, []))
                    if scoped_repos and parts[3] not in scoped_repos and repo_full not in scoped_repos:
                        return 403, {}, {"message": "Resource not accessible by integration"}

                    if len(parts) == 4:
                        meta = server.repo_metadata.get(repo_full)
                        if meta is None:
                            return 200, {}, {"full_name": repo_full, "default_branch": "main"}
                        if isinstance(meta, Exception):
                            raise meta
                        if isinstance(meta, tuple):
                            return meta[0], {}, meta[1]
                        return 200, {}, meta

                    if len(parts) >= 6 and parts[4] == "contents":
                        file_path = "/".join(parts[5:])
                        repo_files = server.contents_by_repo.get(repo_full, {})
                        entry = repo_files.get((file_path, query)) or repo_files.get(file_path)
                        if entry is None:
                            return 404, {}, {"message": "Not Found"}
                        if isinstance(entry, Exception):
                            raise entry
                        if isinstance(entry, tuple):
                            return entry[0], {}, entry[1]
                        encoded = base64.b64encode(entry.encode("utf-8")).decode("ascii")
                        return 200, {}, {
                            "type": "file",
                            "encoding": "base64",
                            "content": encoded,
                        }

                    if len(parts) == 7 and parts[4] == "actions" and parts[5] == "workflows":
                        wf_name = parts[6]
                        wf = server.actions_workflows_by_repo.get(repo_full, {}).get(wf_name)
                        if wf is None:
                            return 404, {}, {"message": "Not Found"}
                        if isinstance(wf, tuple):
                            return wf[0], {}, wf[1]
                        return 200, {}, wf

                    if len(parts) == 5 and parts[4] == "pulls":
                        items = server.pulls_by_repo.get(repo_full, [])
                        if "page=1" in path or "?" not in path:
                            return 200, {}, items
                        return 200, {}, []

                return 404, {}, {"message": "Not Found"}

            def request(self, method, path, payload=None):
                status, _headers, body = self.request_with_headers(method, path, payload=payload)
                return status, body

        return _Client()


def _base_server(**kwargs):
    return FakePreflightGitHubServer(
        app_info={
            "id": 4242,
            "slug": "ocr-reviewer",
            "name": "OCR Reviewer",
            "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
        },
        installations=[
            {
                "id": 1001,
                "account": {"login": "acme"},
                "repository_selection": "selected",
                "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
            }
        ],
        repos_by_installation={
            1001: [{"id": 1, "full_name": "acme/repo-a", "name": "repo-a"}],
        },
        **kwargs,
    )


class OcrPreflightReadinessTests(unittest.TestCase):
    def test_preflight_ready_when_runtime_provider_app_and_scope_valid(self):
        server = _base_server()
        env = {
            "OCR_LLM_AUTH_TOKEN": "sk-super-secret-token-value-123456",
            "OCR_LLM_URL": "https://api.example.com/v1",
            "OCR_LLM_MODEL": "claude-sonnet-4-5",
            "OCR_LLM_USE_ANTHROPIC": "true",
        }
        result = preflight_ocr_runtime(
            app_id=4242,
            allowlist=["acme/repo-a"],
            env=env,
            api_factory=server.api_factory,
            signer=lambda b: b"sig",
            which_fn=lambda name: "/usr/local/bin/ocr" if name == "ocr" else None,
        )

        self.assertEqual(result.status, "ready")
        self.assertTrue(result.ready)
        self.assertFalse(result.pending)
        self.assertTrue(result.executable.available)
        self.assertEqual(result.executable.resolved_path, "/usr/local/bin/ocr")
        self.assertTrue(result.provider.configured)
        self.assertEqual(result.provider.missing_variables, [])
        self.assertTrue(result.app_preflight.passed)
        self.assertEqual(result.competing_workflows, [])
        self.assertTrue(result.scoping["same_repo_only"])
        self.assertTrue(result.scoping["includes_manual_maintainer_prs"])
        self.assertIn("acme/repo-a", result.scoping["eligible_repositories"])

        serialized = json.dumps(result.to_dict())
        report = format_ocr_preflight_report(result)
        self.assertNotIn("sk-super-secret-token-value-123456", serialized)
        self.assertNotIn("sk-super-secret-token-value-123456", report)
        self.assertNotIn("ghs_scoped_", serialized)
        self.assertNotIn("ghs_scoped_", report)

    def test_missing_executable_reports_pending_configuration_and_exits_zero(self):
        server = _base_server()
        env = {
            "OCR_LLM_AUTH_TOKEN": "sk-secret-999999",
            "OCR_LLM_URL": "https://api.example.com/v1",
            "OCR_LLM_MODEL": "claude-sonnet-4-5",
            "OCR_LLM_USE_ANTHROPIC": "false",
        }
        out = io.StringIO()
        err = io.StringIO()
        exit_code = run_ocr_preflight(
            app_id=4242,
            allowlist=["acme/repo-a"],
            env=env,
            json_output=True,
            out_stream=out,
            err_stream=err,
            api_factory=server.api_factory,
            signer=lambda b: b"sig",
            which_fn=lambda _name: None,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(err.getvalue(), "")
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "pending_configuration")
        self.assertFalse(payload["ready"])
        self.assertTrue(payload["pending"])
        self.assertFalse(payload["executable"]["available"])
        self.assertTrue(any("ocr" in reason.lower() for reason in payload["pending_reasons"]))
        self.assertNotIn("sk-secret-999999", out.getvalue())

    def test_missing_provider_vars_reports_pending_configuration_without_leaking_partial_token(self):
        server = _base_server()
        env = {
            "OCR_LLM_AUTH_TOKEN": "sk-partial-secret-888888",
            "OCR_LLM_URL": "",
        }
        out = io.StringIO()
        err = io.StringIO()
        exit_code = run_ocr_preflight(
            app_id=4242,
            allowlist=["acme/repo-a"],
            env=env,
            json_output=True,
            out_stream=out,
            err_stream=err,
            api_factory=server.api_factory,
            signer=lambda b: b"sig",
            which_fn=lambda name: "/usr/local/bin/ocr" if name == "ocr" else None,
        )

        self.assertEqual(exit_code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "pending_configuration")
        self.assertTrue(payload["pending"])
        self.assertFalse(payload["provider"]["configured"])
        self.assertEqual(
            payload["provider"]["missing_variables"],
            ["OCR_LLM_URL", "OCR_LLM_MODEL", "OCR_LLM_USE_ANTHROPIC"],
        )
        self.assertNotIn("sk-partial-secret-888888", out.getvalue())

    def test_unsafe_llm_url_and_invalid_use_anthropic_fail_without_leaking_url_credentials(self):
        server = _base_server()
        for bad_url in (
            "http://api.example.com/v1",
            "https://user:topsecretpass@api.example.com/v1",
            "https://api.example.com/v1?api_key=leak",
            "https://api.example.com/v1#fragment",
        ):
            out = io.StringIO()
            err = io.StringIO()
            exit_code = run_ocr_preflight(
                app_id=4242,
                allowlist=["acme/repo-a"],
                env={
                    "OCR_LLM_AUTH_TOKEN": "sk-token-123456",
                    "OCR_LLM_URL": bad_url,
                    "OCR_LLM_MODEL": "gpt-4o",
                    "OCR_LLM_USE_ANTHROPIC": "maybe",
                },
                json_output=True,
                out_stream=out,
                err_stream=err,
                api_factory=server.api_factory,
                signer=lambda b: b"sig",
                which_fn=lambda name: "/usr/local/bin/ocr" if name == "ocr" else None,
            )
            self.assertEqual(exit_code, 1)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["status"], "error")
            self.assertFalse(payload["ready"])
            self.assertFalse(payload["pending"])
            self.assertIn("OCR_LLM_URL", payload["provider"]["invalid_variables"])
            self.assertIn("OCR_LLM_USE_ANTHROPIC", payload["provider"]["invalid_variables"])
            self.assertNotIn("topsecretpass", out.getvalue())
            self.assertNotIn("api_key=leak", out.getvalue())
            self.assertNotIn("sk-token-123456", out.getvalue())


class CompetingWorkflowDetectionTests(unittest.TestCase):
    def test_detects_active_competing_workflow_via_contents_when_actions_api_forbidden(self):
        active_workflow = (
            "# github-repo-setup workflow-template=open-code-review version=1\n"
            "name: \"Open Code Review\"\n"
            "on:\n"
            "  pull_request_target:\n"
            "    types: [opened, synchronize, reopened]\n"
            "jobs:\n"
            "  review:\n"
            "    if: github.event.pull_request.head.repo.full_name == github.repository\n"
            "    steps:\n"
            "      - uses: alibaba/open-code-review@0123456789abcdef0123456789abcdef01234567\n"
        )
        server = _base_server(
            repo_metadata={"acme/repo-a": {"full_name": "acme/repo-a", "default_branch": "develop"}},
            contents_by_repo={
                "acme/repo-a": {
                    (".github/workflows/open-code-review.yml", "ref=develop"): active_workflow,
                }
            },
            actions_workflows_by_repo={
                "acme/repo-a": {
                    "open-code-review.yml": (403, {"message": "Resource not accessible by integration"}),
                }
            },
        )
        env = {
            "OCR_LLM_AUTH_TOKEN": "sk-secret-abc-123456",
            "OCR_LLM_URL": "https://api.example.com/v1",
            "OCR_LLM_MODEL": "claude-sonnet-4-5",
            "OCR_LLM_USE_ANTHROPIC": "true",
        }
        result = preflight_ocr_runtime(
            app_id=4242,
            allowlist=["acme/repo-a"],
            env=env,
            api_factory=server.api_factory,
            signer=lambda b: b"sig",
            which_fn=lambda name: "/usr/local/bin/ocr" if name == "ocr" else None,
        )

        self.assertEqual(result.status, "ready")
        self.assertEqual(len(result.competing_workflows), 1)
        wf = result.competing_workflows[0]
        self.assertEqual(wf.repository, "acme/repo-a")
        self.assertEqual(wf.default_branch, "develop")
        self.assertTrue(wf.present)
        self.assertTrue(wf.active)
        self.assertIn("open-code-review.yml", wf.warning or "")
        self.assertIn("disable", (wf.instruction or "").lower())
        self.assertTrue(any("competing" in w.lower() for w in result.warnings))

        report = format_ocr_preflight_report(result)
        self.assertIn("Competing workflows detected (1):", report)
        self.assertIn("acme/repo-a", report)

        # Verify scoped installation tokens were minted per-repo for inspection
        token_calls = [c for c in server.calls if c[1] == "POST" and c[2].endswith("/access_tokens")]
        self.assertTrue(any(c[3].get("repositories") == ["repo-a"] for c in token_calls))

    def test_ignores_disabled_or_commented_competing_workflows(self):
        if_false_workflow = (
            "name: Open Code Review\n"
            "on:\n"
            "  pull_request_target:\n"
            "jobs:\n"
            "  review:\n"
            "    if: false\n"
            "    steps:\n"
            "      - uses: alibaba/open-code-review@0123456789abcdef0123456789abcdef01234567\n"
        )
        server = FakePreflightGitHubServer(
            app_info={
                "id": 4242,
                "slug": "ocr-reviewer",
                "name": "OCR Reviewer",
                "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
            },
            installations=[
                {
                    "id": 1001,
                    "account": {"login": "acme"},
                    "repository_selection": "selected",
                    "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                }
            ],
            repos_by_installation={
                1001: [
                    {"id": 1, "full_name": "acme/repo-a", "name": "repo-a"},
                    {"id": 2, "full_name": "acme/repo-b", "name": "repo-b"},
                ],
            },
            contents_by_repo={
                "acme/repo-a": {
                    (".github/workflows/open-code-review.yml", "ref=main"): "on:\n  pull_request_target:\n",
                },
                "acme/repo-b": {
                    (".github/workflows/open-code-review.yml", "ref=main"): if_false_workflow,
                },
            },
            actions_workflows_by_repo={
                "acme/repo-a": {
                    "open-code-review.yml": {"id": 99, "state": "disabled_manually"},
                },
            },
        )
        env = {
            "OCR_LLM_AUTH_TOKEN": "sk-secret-abc-123456",
            "OCR_LLM_URL": "https://api.example.com/v1",
            "OCR_LLM_MODEL": "claude-sonnet-4-5",
            "OCR_LLM_USE_ANTHROPIC": "true",
        }
        result = preflight_ocr_runtime(
            app_id=4242,
            allowlist=["acme/repo-a", "acme/repo-b"],
            env=env,
            api_factory=server.api_factory,
            signer=lambda b: b"sig",
            which_fn=lambda name: "/usr/local/bin/ocr" if name == "ocr" else None,
        )

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.competing_workflows, [])
        self.assertEqual(len(result.workflow_checks), 2)
        self.assertTrue(all(w.present and not w.active for w in result.workflow_checks))

    def test_competing_workflow_inspection_errors_surface_indeterminate_status_without_crashing_clean_repos(self):
        env = {
            "OCR_LLM_AUTH_TOKEN": "sk-secret-abc-123456",
            "OCR_LLM_URL": "https://api.example.com/v1",
            "OCR_LLM_MODEL": "claude-sonnet-4-5",
            "OCR_LLM_USE_ANTHROPIC": "true",
        }
        server = FakePreflightGitHubServer(
            app_info={
                "id": 4242,
                "slug": "ocr-reviewer",
                "name": "OCR Reviewer",
                "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
            },
            installations=[
                {
                    "id": 1001,
                    "account": {"login": "acme"},
                    "repository_selection": "selected",
                    "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                }
            ],
            repos_by_installation={
                1001: [
                    {"id": 1, "full_name": "acme/repo-net-err", "name": "repo-net-err"},
                    {"id": 2, "full_name": "acme/repo-bad-branch", "name": "repo-bad-branch"},
                    {"id": 3, "full_name": "acme/repo-bad-b64", "name": "repo-bad-b64"},
                    {"id": 4, "full_name": "acme/repo-bad-yaml", "name": "repo-bad-yaml"},
                    {"id": 5, "full_name": "acme/repo-clean", "name": "repo-clean"},
                ],
            },
            repo_metadata={
                "acme/repo-net-err": OSError("connection reset by peer"),
                "acme/repo-bad-branch": (502, {"message": "Bad Gateway"}),
            },
            contents_by_repo={
                "acme/repo-bad-b64": {
                    (".github/workflows/open-code-review.yml", "ref=main"): (
                        200,
                        {"type": "file", "encoding": "base64", "content": "!!!not-base64!!!"},
                    ),
                },
                "acme/repo-bad-yaml": {
                    (".github/workflows/open-code-review.yml", "ref=main"): ":::not valid yaml:::\n",
                },
                # acme/repo-clean intentionally absent -> HTTP 404 (safe)
            },
        )
        result = preflight_ocr_runtime(
            app_id=4242,
            allowlist=[
                "acme/repo-net-err",
                "acme/repo-bad-branch",
                "acme/repo-bad-b64",
                "acme/repo-bad-yaml",
                "acme/repo-clean",
            ],
            env=env,
            api_factory=server.api_factory,
            signer=lambda b: b"sig",
            which_fn=lambda name: "/usr/local/bin/ocr" if name == "ocr" else None,
        )

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.competing_workflows, [])
        self.assertEqual(
            [wf.repository for wf in result.indeterminate_workflows],
            [
                "acme/repo-net-err",
                "acme/repo-bad-branch",
                "acme/repo-bad-b64",
                "acme/repo-bad-yaml",
            ],
        )
        for wf in result.indeterminate_workflows:
            self.assertTrue(wf.indeterminate)
            self.assertEqual(wf.workflow_state, "indeterminate")
            self.assertTrue(wf.error)
            self.assertTrue(wf.warning)
            self.assertTrue(wf.instruction)

        clean_check = [wf for wf in result.workflow_checks if wf.repository == "acme/repo-clean"][0]
        self.assertFalse(clean_check.indeterminate)
        self.assertFalse(clean_check.present)
        self.assertFalse(clean_check.active)
        self.assertIsNone(clean_check.error)

        report = format_ocr_preflight_report(result)
        self.assertIn("Indeterminate competing-workflow checks (4):", report)
        for repo in ("acme/repo-net-err", "acme/repo-bad-branch", "acme/repo-bad-b64", "acme/repo-bad-yaml"):
            self.assertIn(repo, report)
        self.assertTrue(any("fail closed" in w.lower() for w in result.warnings))
        self.assertTrue(any("preflight" in inst.lower() for inst in result.instructions))

        payload = result.to_dict()
        self.assertEqual(len(payload["indeterminate_workflows"]), 4)

    def test_indeterminate_workflow_decoding_and_payload_edge_cases(self):
        env = {
            "OCR_LLM_AUTH_TOKEN": "sk-secret-abc-123456",
            "OCR_LLM_URL": "https://api.example.com/v1",
            "OCR_LLM_MODEL": "claude-sonnet-4-5",
            "OCR_LLM_USE_ANTHROPIC": "true",
        }
        non_utf8_b64 = base64.b64encode(b"\xff\xfe\x00\x01").decode("ascii")
        server = FakePreflightGitHubServer(
            app_info={
                "id": 4242,
                "slug": "ocr-reviewer",
                "name": "OCR Reviewer",
                "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
            },
            installations=[
                {
                    "id": 1001,
                    "account": {"login": "acme"},
                    "repository_selection": "selected",
                    "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                }
            ],
            repos_by_installation={
                1001: [
                    {"id": 1, "full_name": "acme/repo-empty-branch", "name": "repo-empty-branch"},
                    {"id": 2, "full_name": "acme/repo-non-utf8", "name": "repo-non-utf8"},
                    {"id": 3, "full_name": "acme/repo-wrong-enc", "name": "repo-wrong-enc"},
                    {"id": 4, "full_name": "acme/repo-dir", "name": "repo-dir"},
                    {"id": 5, "full_name": "acme/repo-empty-file", "name": "repo-empty-file"},
                ],
            },
            repo_metadata={
                "acme/repo-empty-branch": {"full_name": "acme/repo-empty-branch", "default_branch": "   "},
            },
            contents_by_repo={
                "acme/repo-non-utf8": {
                    (".github/workflows/open-code-review.yml", "ref=main"): (
                        200,
                        {"type": "file", "encoding": "base64", "content": non_utf8_b64},
                    ),
                },
                "acme/repo-wrong-enc": {
                    (".github/workflows/open-code-review.yml", "ref=main"): (
                        200,
                        {"type": "file", "encoding": "none", "content": "on: pull_request"},
                    ),
                },
                "acme/repo-dir": {
                    (".github/workflows/open-code-review.yml", "ref=main"): (
                        200,
                        {"type": "dir"},
                    ),
                },
                "acme/repo-empty-file": {
                    (".github/workflows/open-code-review.yml", "ref=main"): "   \n",
                },
            },
        )
        out = io.StringIO()
        err = io.StringIO()
        rc = run_ocr_preflight(
            app_id=4242,
            allowlist=[
                "acme/repo-empty-branch",
                "acme/repo-non-utf8",
                "acme/repo-wrong-enc",
                "acme/repo-dir",
                "acme/repo-empty-file",
            ],
            env=env,
            json_output=True,
            out_stream=out,
            err_stream=err,
            api_factory=server.api_factory,
            signer=lambda b: b"sig",
            which_fn=lambda name: "/usr/local/bin/ocr",
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(len(payload["indeterminate_workflows"]), 5)
        self.assertTrue(all(wf["indeterminate"] for wf in payload["indeterminate_workflows"]))
        self.assertTrue(all(wf["error"] for wf in payload["indeterminate_workflows"]))


class AppPermissionsExecutableAndScopingTests(unittest.TestCase):
    def test_rejects_admin_app_and_missing_pr_comment_permission(self):
        # 1. Admin App ID rejected via env var SCIFLOW_CICD_ADMIN_APP_ID
        server = _base_server()
        env = {
            "SCIFLOW_CICD_ADMIN_APP_ID": "4242",
            "OCR_LLM_AUTH_TOKEN": "sk-secret-abc-123456",
            "OCR_LLM_URL": "https://api.example.com/v1",
            "OCR_LLM_MODEL": "claude-sonnet-4-5",
            "OCR_LLM_USE_ANTHROPIC": "true",
        }
        out = io.StringIO()
        err = io.StringIO()
        exit_code = run_ocr_preflight(
            app_id=4242,
            allowlist=["acme/repo-a"],
            env=env,
            json_output=True,
            out_stream=out,
            err_stream=err,
            api_factory=server.api_factory,
            signer=lambda b: b"sig",
            which_fn=lambda name: "/usr/local/bin/ocr",
        )
        self.assertEqual(exit_code, 1)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "error")
        self.assertTrue(any("administrative" in e.lower() for e in payload["errors"]))
        self.assertNotIn("sk-secret-abc-123456", out.getvalue())

        # 2. App installation lacking pull_requests: write permission fails
        underprivileged_server = FakePreflightGitHubServer(
            app_info={
                "id": 4242,
                "slug": "ocr-reviewer",
                "name": "OCR Reviewer",
                "permissions": {"contents": "read", "pull_requests": "read", "metadata": "read"},
            },
            installations=[
                {
                    "id": 1001,
                    "account": {"login": "acme"},
                    "repository_selection": "selected",
                    "permissions": {"contents": "read", "pull_requests": "read", "metadata": "read"},
                }
            ],
            repos_by_installation={
                1001: [{"id": 1, "full_name": "acme/repo-a", "name": "repo-a"}],
            },
        )
        result = preflight_ocr_runtime(
            app_id=4242,
            allowlist=["acme/repo-a"],
            env={
                "OCR_LLM_AUTH_TOKEN": "sk-secret-abc-123456",
                "OCR_LLM_URL": "https://api.example.com/v1",
                "OCR_LLM_MODEL": "claude-sonnet-4-5",
                "OCR_LLM_USE_ANTHROPIC": "true",
            },
            api_factory=underprivileged_server.api_factory,
            signer=lambda b: b"sig",
            which_fn=lambda name: "/usr/local/bin/ocr",
        )
        self.assertEqual(result.status, "error")
        self.assertFalse(result.app_preflight.passed)
        self.assertIn("pull_requests:write", result.app_preflight.excluded_repositories[0].reason)

    def test_scoping_inspects_same_repo_manual_maintainer_prs_and_skips_forks(self):
        pulls = [
            {
                "number": 10,
                "state": "open",
                "draft": False,
                "title": "Maintainer manual PR without label",
                "user": {"login": "lead-maintainer"},
                "author_association": "MEMBER",
                "labels": [],
                "head": {"sha": "a" * 40, "ref": "feat", "repo": {"full_name": "acme/repo-a", "fork": False}},
                "base": {"ref": "main", "repo": {"full_name": "acme/repo-a"}},
            },
            {
                "number": 11,
                "state": "open",
                "draft": False,
                "title": "External PR with eligible label",
                "user": {"login": "contributor"},
                "author_association": "NONE",
                "labels": [{"name": "eligible for review"}],
                "head": {"sha": "b" * 40, "ref": "fix", "repo": {"full_name": "acme/repo-a", "fork": False}},
                "base": {"ref": "main", "repo": {"full_name": "acme/repo-a"}},
            },
            {
                "number": 12,
                "state": "open",
                "draft": False,
                "title": "Untrusted fork PR",
                "user": {"login": "fork-user"},
                "author_association": "NONE",
                "labels": [{"name": "eligible for review"}],
                "head": {"sha": "c" * 40, "ref": "patch", "repo": {"full_name": "fork-user/repo-a", "fork": True}},
                "base": {"ref": "main", "repo": {"full_name": "acme/repo-a"}},
            },
        ]
        server = _base_server(pulls_by_repo={"acme/repo-a": pulls})
        env = {
            "OCR_LLM_AUTH_TOKEN": "sk-secret-abc-123456",
            "OCR_LLM_URL": "https://api.example.com/v1",
            "OCR_LLM_MODEL": "claude-sonnet-4-5",
            "OCR_LLM_USE_ANTHROPIC": "true",
        }
        result = preflight_ocr_runtime(
            app_id=4242,
            allowlist=["acme/repo-a"],
            env=env,
            inspect_pull_requests=True,
            api_factory=server.api_factory,
            signer=lambda b: b"sig",
            which_fn=lambda name: "/usr/local/bin/ocr",
        )
        self.assertEqual(result.status, "ready")
        inspected = result.scoping["inspected_pull_requests"]["acme/repo-a"]
        self.assertEqual([item["number"] for item in inspected["eligible"]], [10, 11])
        self.assertEqual(inspected["skipped"], [{"number": 12, "reason": "fork_or_cross_repo"}])

        # Label-only mode warns that manual maintainer PRs without the label are excluded
        label_only = preflight_ocr_runtime(
            app_id=4242,
            allowlist=["acme/repo-a"],
            env=env,
            eligibility_mode="label",
            api_factory=server.api_factory,
            signer=lambda b: b"sig",
            which_fn=lambda name: "/usr/local/bin/ocr",
        )
        self.assertFalse(label_only.scoping["includes_manual_maintainer_prs"])
        self.assertTrue(any("excludes unlabeled" in w.lower() for w in label_only.warnings))

        with self.assertRaises(OcrPreflightError):
            preflight_ocr_runtime(
                app_id=4242,
                allowlist=["acme/repo-a"],
                env=env,
                eligibility_mode="unknown-mode",
                api_factory=server.api_factory,
                signer=lambda b: b"sig",
            )

    def test_ocr_bin_env_and_explicit_path_checks(self):
        server = _base_server()
        env_base = {
            "OCR_LLM_AUTH_TOKEN": "sk-secret-abc-123456",
            "OCR_LLM_URL": "https://api.example.com/v1",
            "OCR_LLM_MODEL": "claude-sonnet-4-5",
            "OCR_LLM_USE_ANTHROPIC": "true",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bin_path = tmp / "ocr-custom"
            bin_path.write_text("#!/bin/sh\necho 1.10.0\n", encoding="utf-8")

            # Non-executable file -> pending_configuration
            os.chmod(bin_path, 0o644)
            pending_res = preflight_ocr_runtime(
                app_id=4242,
                allowlist=["acme/repo-a"],
                env={**env_base, "OCR_CLI_BIN": str(bin_path)},
                api_factory=server.api_factory,
                signer=lambda b: b"sig",
            )
            self.assertEqual(pending_res.status, "pending_configuration")
            self.assertFalse(pending_res.executable.available)

            # Executable file -> ready
            os.chmod(bin_path, 0o755)
            ready_res = preflight_ocr_runtime(
                app_id=4242,
                allowlist=["acme/repo-a"],
                ocr_bin=str(bin_path),
                env=env_base,
                api_factory=server.api_factory,
                signer=lambda b: b"sig",
            )
            self.assertEqual(ready_res.status, "ready")
            self.assertTrue(ready_res.executable.available)
            self.assertEqual(ready_res.executable.resolved_path, str(bin_path))

            # Path traversal rejected as error
            err_res = preflight_ocr_runtime(
                app_id=4242,
                allowlist=["acme/repo-a"],
                ocr_bin="../evil/ocr",
                env=env_base,
                api_factory=server.api_factory,
                signer=lambda b: b"sig",
            )
            self.assertEqual(err_res.status, "error")
            self.assertTrue(any("traversal" in e.lower() for e in err_res.errors))


class CliAndLoopbackEndToEndTests(unittest.TestCase):
    def test_gh_setup_ocr_preflight_end_to_end_with_openssl_key_and_loopback_http(self):
        workflow_yaml = (
            "name: Open Code Review\n"
            "on:\n"
            "  pull_request_target:\n"
            "    types: [opened, synchronize]\n"
            "jobs:\n"
            "  review:\n"
            "    steps:\n"
            "      - uses: alibaba/open-code-review@0123456789abcdef0123456789abcdef01234567\n"
        )
        encoded_wf = base64.b64encode(workflow_yaml.encode("utf-8")).decode("ascii")

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def _send(self, code, payload):
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/app":
                    self._send(
                        200,
                        {
                            "id": 777,
                            "slug": "ocr-reviewer",
                            "name": "OCR Reviewer",
                            "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                        },
                    )
                    return
                if self.path.startswith("/app/installations"):
                    if "page=1" in self.path or "?" not in self.path:
                        self._send(
                            200,
                            [
                                {
                                    "id": 500,
                                    "account": {"login": "acme"},
                                    "repository_selection": "selected",
                                    "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                                }
                            ],
                        )
                    else:
                        self._send(200, [])
                    return
                if self.path.startswith("/installation/repositories"):
                    if "page=1" in self.path or "?" not in self.path:
                        self._send(
                            200,
                            {
                                "total_count": 1,
                                "repositories": [{"id": 1, "full_name": "acme/repo-a", "name": "repo-a"}],
                            },
                        )
                    else:
                        self._send(200, {"total_count": 1, "repositories": []})
                    return
                if self.path == "/repos/acme/repo-a":
                    self._send(200, {"full_name": "acme/repo-a", "default_branch": "main"})
                    return
                if self.path.startswith("/repos/acme/repo-a/contents/.github/workflows/open-code-review.yml"):
                    self._send(200, {"type": "file", "encoding": "base64", "content": encoded_wf})
                    return
                if self.path == "/repos/acme/repo-a/actions/workflows/open-code-review.yml":
                    self._send(403, {"message": "Resource not accessible by integration"})
                    return
                self._send(404, {"message": "Not Found"})

            def do_POST(self):
                if self.path == "/app/installations/500/access_tokens":
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                    repos = body.get("repositories") or ["repo-a"]
                    perms = body.get("permissions") or {"contents": "read", "pull_requests": "write", "metadata": "read"}
                    self._send(
                        201,
                        {
                            "token": "ghs_e2e_secret_preflight_token",
                            "expires_at": "2026-11-14T05:00:00Z",
                            "permissions": perms,
                            "repositories": [{"name": r} for r in repos],
                        },
                    )
                    return
                self._send(404, {"message": "Not Found"})

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp = Path(tmpdir)
                priv_key = tmp / "app.pem"
                subprocess.run(["openssl", "genrsa", "-out", str(priv_key), "2048"], check=True, capture_output=True)
                os.chmod(priv_key, 0o600)

                fake_ocr = tmp / "ocr"
                fake_ocr.write_text("#!/bin/sh\necho 1.10.0\n", encoding="utf-8")
                os.chmod(fake_ocr, 0o755)

                sciflow_cicd = ROOT_DIR / "sciflow-cicd"
                env = dict(os.environ)
                env.update(
                    {
                        "OCR_LLM_AUTH_TOKEN": "sk-e2e-super-secret-token",
                        "OCR_LLM_URL": "https://api.example.com/v1",
                        "OCR_LLM_MODEL": "claude-sonnet-4-5",
                        "OCR_LLM_USE_ANTHROPIC": "true",
                    }
                )
                proc = subprocess.run(
                    [
                        str(sciflow_cicd),
                        "ocr",
                        "preflight",
                        "--app-id",
                        "777",
                        "--private-key-path",
                        str(priv_key),
                        "--allowlist",
                        "acme/repo-a",
                        "--api-base-url",
                        f"http://127.0.0.1:{server.server_port}",
                        "--ocr-bin",
                        str(fake_ocr),
                        "--json",
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}")
                payload = json.loads(proc.stdout)
                self.assertEqual(payload["status"], "ready")
                self.assertTrue(payload["ready"])
                self.assertEqual(len(payload["competing_workflows"]), 1)
                self.assertEqual(payload["competing_workflows"][0]["repository"], "acme/repo-a")
                self.assertNotIn("sk-e2e-super-secret-token", proc.stdout + proc.stderr)
                self.assertNotIn("ghs_e2e_secret_preflight_token", proc.stdout + proc.stderr)
                self.assertNotIn("PRIVATE KEY", proc.stdout + proc.stderr)
        finally:
            server.shutdown()
            server.server_close()

    def test_preflight_ocr_runtime_accepts_app_and_eligibility_configs_without_local_client_wrapper(self):
        import scripts.ocr_preflight as ocr_preflight
        from scripts.ocr_app_auth import OcrAppConfig
        from scripts.ocr_poller import PrEligibilityConfig

        fake = FakePreflightGitHubServer(
            app_info={
                "id": 777,
                "slug": "ocr-bot",
                "name": "OCR Bot",
                "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
            },
            installations=[
                {
                    "id": 22,
                    "account": {"login": "acme"},
                    "repository_selection": "selected",
                    "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                }
            ],
            repos_by_installation={22: [{"full_name": "acme/repo-a"}]},
            repo_metadata={"acme/repo-a": {"full_name": "acme/repo-a", "default_branch": "main"}},
            pulls_by_repo={
                "acme/repo-a": [
                    {
                        "number": 5,
                        "state": "open",
                        "draft": False,
                        "author_association": "CONTRIBUTOR",
                        "user": {"login": "dev"},
                        "labels": [{"name": "ocr-ready"}],
                        "head": {"sha": "a" * 40, "repo": {"id": 1, "full_name": "acme/repo-a", "fork": False}},
                        "base": {"sha": "0" * 40, "repo": {"id": 1, "full_name": "acme/repo-a", "fork": False}},
                    }
                ]
            },
        )
        app_cfg = OcrAppConfig(
            app_id=777,
            allowlist=("acme/repo-a",),
            api_factory=fake.api_factory,
            signer=lambda _d: b"sig",
        )
        elig_cfg = PrEligibilityConfig(eligibility_mode="label", eligible_label="ocr-ready")
        res = preflight_ocr_runtime(
            app_config=app_cfg,
            eligibility_config=elig_cfg,
            inspect_pull_requests=True,
            which_fn=lambda _cmd: "/usr/local/bin/ocr",
            env={
                "OCR_LLM_AUTH_TOKEN": "sk-secret-abc-123456",
                "OCR_LLM_URL": "https://api.example.com/v1",
                "OCR_LLM_MODEL": "claude-sonnet-4-5",
                "OCR_LLM_USE_ANTHROPIC": "true",
            },
        )
        self.assertEqual(res.status, "ready")
        self.assertEqual(
            res.scoping["inspected_pull_requests"]["acme/repo-a"]["eligible"],
            [{"number": 5, "reason": "label:ocr-ready"}],
        )
        self.assertFalse(hasattr(ocr_preflight, "_client_get"))


if __name__ == "__main__":
    unittest.main()
