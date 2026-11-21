"""End-to-end tests for the automated OCR CLI pipeline (preflight-app, preflight, poll, check-safety, run)."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
SCIFLOW_CICD = ROOT_DIR / "sciflow-cicd"


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _verify_rs256_jwt(token: str, public_key_path: Path, expected_iss: str) -> Dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("JWT must have 3 segments")
    header = json.loads(_b64url_decode(parts[0]).decode("utf-8"))
    payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
    if header.get("alg") != "RS256" or header.get("typ") != "JWT":
        raise ValueError(f"Unexpected JWT header: {header!r}")
    if str(payload.get("iss")) != str(expected_iss):
        raise ValueError(f"Unexpected JWT iss: {payload.get('iss')!r} != {expected_iss!r}")
    if int(payload["exp"]) - int(payload["iat"]) > 600:
        raise ValueError("JWT lifetime exceeds 600 seconds")

    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    sig_bytes = _b64url_decode(parts[2])
    with tempfile.TemporaryDirectory() as tmp:
        sig_file = Path(tmp) / "jwt.sig"
        sig_file.write_bytes(sig_bytes)
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(public_key_path), "-signature", str(sig_file)],
            input=signing_input,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise ValueError(f"OpenSSL JWT signature verification failed: {proc.stderr.decode('utf-8', 'replace')}")
    return payload


@dataclass
class _RepoState:
    full_name: str
    repo_id: int
    installation_id: int
    default_branch: str = "main"
    pull_requests: List[Dict[str, Any]] = field(default_factory=list)
    pr_files: Dict[int, List[Dict[str, Any]]] = field(default_factory=dict)
    competing_workflow_yaml: Optional[str] = None
    competing_workflow_raw_content: Optional[Dict[str, Any]] = None
    competing_workflow_error_status: Optional[int] = None
    competing_workflow_state: Optional[str] = None
    reviews_posted: List[Dict[str, Any]] = field(default_factory=list)
    issue_comments_posted: List[Dict[str, Any]] = field(default_factory=list)


class _LoopbackGitHubState:
    def __init__(
        self,
        *,
        app_id: str,
        admin_app_id: str,
        public_key_path: Path,
        repos: Dict[str, _RepoState],
    ) -> None:
        self.app_id = str(app_id)
        self.admin_app_id = str(admin_app_id)
        self.public_key_path = public_key_path
        self.repos = {k.lower(): v for k, v in repos.items()}
        self.lock = threading.Lock()
        self.requests: List[Dict[str, Any]] = []
        self.minted_tokens: Dict[str, Dict[str, Any]] = {}
        self.jwt_verifications: int = 0
        self._token_seq = 0
        self._comment_seq = 100

    def reset_requests(self) -> None:
        with self.lock:
            self.requests.clear()

    def record_request(self, method: str, path: str, body: Any, auth_kind: str) -> None:
        with self.lock:
            self.requests.append(
                {
                    "method": method,
                    "path": path,
                    "body": body,
                    "auth_kind": auth_kind,
                }
            )

    def mint_token(self, installation_id: int, requested_body: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        with self.lock:
            self._token_seq += 1
            token = f"ghs_e2e_scoped_{installation_id}_{self._token_seq}"
            permissions = requested_body.get("permissions") or {
                "contents": "read",
                "pull_requests": "write",
                "metadata": "read",
            }
            req_repos = requested_body.get("repositories")
            if isinstance(req_repos, list) and req_repos:
                scoped_repos = [
                    r.full_name
                    for r in self.repos.values()
                    if r.installation_id == installation_id and r.full_name.split("/", 1)[1] in req_repos
                ]
            else:
                scoped_repos = [
                    r.full_name for r in self.repos.values() if r.installation_id == installation_id
                ]
            meta = {
                "installation_id": installation_id,
                "permissions": dict(permissions),
                "repositories": scoped_repos,
            }
            self.minted_tokens[token] = meta
            return token, {
                "token": token,
                "expires_at": "2099-01-01T00:00:00Z",
                "permissions": dict(permissions),
                "repositories": [
                    {"name": full.split("/", 1)[1], "full_name": full} for full in scoped_repos
                ],
            }


def _make_handler(state: _LoopbackGitHubState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def _send_json(self, status: int, payload: Any) -> None:
            raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        def _read_json_body(self) -> Any:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return None
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))

        def _authenticate(self) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return None, None
            token = auth[len("Bearer ") :].strip()
            if token.startswith("ghs_e2e_scoped_"):
                with state.lock:
                    meta = state.minted_tokens.get(token)
                return ("installation", meta) if meta else (None, None)
            try:
                payload = _verify_rs256_jwt(token, state.public_key_path, expected_iss=state.app_id)
                with state.lock:
                    state.jwt_verifications += 1
                return "jwt", payload
            except ValueError:
                return None, None

        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            path_only = parsed.path
            query = parse_qs(parsed.query)
            body = self._read_json_body() if method in ("POST", "PATCH", "PUT") else None
            auth_kind, auth_meta = self._authenticate()
            state.record_request(method, self.path, body, auth_kind or "unauthenticated")

            if auth_kind is None:
                self._send_json(401, {"message": "Bad credentials"})
                return

            # App endpoints (JWT auth required)
            if method == "GET" and path_only == "/app":
                if auth_kind != "jwt":
                    self._send_json(403, {"message": "JWT required"})
                    return
                self._send_json(
                    200,
                    {
                        "id": int(state.app_id),
                        "slug": "ocr-advisory-app",
                        "name": "OCR Advisory App",
                        "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                    },
                )
                return

            if method == "GET" and path_only == "/app/installations":
                if auth_kind != "jwt":
                    self._send_json(403, {"message": "JWT required"})
                    return
                seen_inst: Dict[int, Dict[str, Any]] = {}
                for repo in state.repos.values():
                    owner = repo.full_name.split("/", 1)[0]
                    if repo.installation_id not in seen_inst:
                        seen_inst[repo.installation_id] = {
                            "id": repo.installation_id,
                            "account": {"login": owner},
                            "repository_selection": "selected",
                            "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                        }
                self._send_json(200, list(seen_inst.values()))
                return

            if method == "POST" and path_only.startswith("/app/installations/") and path_only.endswith("/access_tokens"):
                if auth_kind != "jwt":
                    self._send_json(403, {"message": "JWT required"})
                    return
                inst_str = path_only.split("/")[3]
                if not inst_str.isdigit():
                    self._send_json(404, {"message": "Not Found"})
                    return
                _, resp = state.mint_token(int(inst_str), body if isinstance(body, dict) else {})
                self._send_json(201, resp)
                return

            if method == "GET" and path_only == "/installation/repositories":
                if auth_kind != "installation" or not auth_meta:
                    self._send_json(403, {"message": "Installation token required"})
                    return
                repos_payload = [
                    {"name": full.split("/", 1)[1], "full_name": full}
                    for full in auth_meta["repositories"]
                ]
                self._send_json(200, {"total_count": len(repos_payload), "repositories": repos_payload})
                return

            # Repository endpoints (installation token scoped to repo)
            parts = path_only.strip("/").split("/")
            if len(parts) >= 3 and parts[0] == "repos":
                full_name = f"{parts[1]}/{parts[2]}".lower()
                with state.lock:
                    repo = state.repos.get(full_name)
                if repo is None:
                    self._send_json(404, {"message": "Not Found"})
                    return
                if auth_kind != "installation" or not auth_meta:
                    self._send_json(403, {"message": "Installation token required"})
                    return
                allowed_lower = {r.lower() for r in auth_meta["repositories"]}
                if full_name not in allowed_lower:
                    self._send_json(403, {"message": f"Token not scoped to {repo.full_name}"})
                    return

                # GET /repos/{owner}/{repo}
                if method == "GET" and len(parts) == 3:
                    self._send_json(200, {"full_name": repo.full_name, "default_branch": repo.default_branch})
                    return

                # GET /repos/{owner}/{repo}/contents/.github/workflows/open-code-review.yml
                if (
                    method == "GET"
                    and parts[3:] == ["contents", ".github", "workflows", "open-code-review.yml"]
                ):
                    with state.lock:
                        wf_yaml = repo.competing_workflow_yaml
                        wf_raw = repo.competing_workflow_raw_content
                        wf_err_status = repo.competing_workflow_error_status
                    if wf_err_status is not None:
                        self._send_json(wf_err_status, {"message": "Simulated workflow inspection error"})
                        return
                    if wf_raw is not None:
                        self._send_json(200, wf_raw)
                        return
                    if wf_yaml is None:
                        self._send_json(404, {"message": "Not Found"})
                        return
                    encoded = base64.b64encode(wf_yaml.encode("utf-8")).decode("ascii")
                    self._send_json(200, {"type": "file", "encoding": "base64", "content": encoded})
                    return

                # GET /repos/{owner}/{repo}/actions/workflows/open-code-review.yml
                if method == "GET" and parts[3:] == ["actions", "workflows", "open-code-review.yml"]:
                    with state.lock:
                        wf_state = repo.competing_workflow_state
                    if wf_state is None:
                        self._send_json(404, {"message": "Not Found"})
                        return
                    self._send_json(200, {"state": wf_state})
                    return

                # GET /repos/{owner}/{repo}/pulls
                if method == "GET" and parts[3:] == ["pulls"]:
                    page = int(query.get("page", ["1"])[0])
                    if page > 1:
                        self._send_json(200, [])
                        return
                    with state.lock:
                        prs_copy = json.loads(json.dumps(repo.pull_requests))
                    self._send_json(200, prs_copy)
                    return

                # GET /repos/{owner}/{repo}/pulls/{number}
                if method == "GET" and len(parts) == 5 and parts[3] == "pulls" and parts[4].isdigit():
                    pr_num = int(parts[4])
                    with state.lock:
                        found = next((p for p in repo.pull_requests if p.get("number") == pr_num), None)
                        payload = json.loads(json.dumps(found)) if found is not None else None
                    if payload is None:
                        self._send_json(404, {"message": "Not Found"})
                        return
                    self._send_json(200, payload)
                    return

                # GET /repos/{owner}/{repo}/pulls/{number}/files
                if (
                    method == "GET"
                    and len(parts) == 6
                    and parts[3] == "pulls"
                    and parts[4].isdigit()
                    and parts[5] == "files"
                ):
                    pr_num = int(parts[4])
                    with state.lock:
                        files = json.loads(json.dumps(repo.pr_files.get(pr_num, [])))
                    self._send_json(200, files)
                    return

                # GET /repos/{owner}/{repo}/pulls/{number}/reviews
                if (
                    method == "GET"
                    and len(parts) == 6
                    and parts[3] == "pulls"
                    and parts[4].isdigit()
                    and parts[5] == "reviews"
                ):
                    pr_num = int(parts[4])
                    page = int((query.get("page") or ["1"])[0])
                    per_page = int((query.get("per_page") or ["100"])[0])
                    with state.lock:
                        items = [
                            {
                                "id": entry["id"],
                                "body": entry["payload"].get("body", ""),
                                "commit_id": entry["payload"].get("commit_id"),
                                "html_url": (
                                    f"https://github.com/{repo.full_name}/pull/{pr_num}"
                                    f"#pullrequestreview-{entry['id']}"
                                ),
                            }
                            for entry in repo.reviews_posted
                            if entry["pr_number"] == pr_num
                        ]
                    start = max(0, (page - 1) * per_page)
                    self._send_json(200, items[start : start + per_page])
                    return

                # GET /repos/{owner}/{repo}/issues/{number}/comments
                if (
                    method == "GET"
                    and len(parts) == 6
                    and parts[3] == "issues"
                    and parts[4].isdigit()
                    and parts[5] == "comments"
                ):
                    pr_num = int(parts[4])
                    page = int((query.get("page") or ["1"])[0])
                    per_page = int((query.get("per_page") or ["100"])[0])
                    with state.lock:
                        items = [
                            {
                                "id": entry["id"],
                                "body": entry["payload"].get("body", ""),
                                "html_url": f"https://github.com/{repo.full_name}/pull/{pr_num}#issuecomment-{entry['id']}",
                            }
                            for entry in repo.issue_comments_posted
                            if entry["pr_number"] == pr_num
                        ]
                    start = max(0, (page - 1) * per_page)
                    self._send_json(200, items[start : start + per_page])
                    return

                # POST /repos/{owner}/{repo}/pulls/{number}/reviews
                if (
                    method == "POST"
                    and len(parts) == 6
                    and parts[3] == "pulls"
                    and parts[4].isdigit()
                    and parts[5] == "reviews"
                ):
                    pr_num = int(parts[4])
                    with state.lock:
                        state._comment_seq += 1
                        rid = state._comment_seq
                        entry = {"pr_number": pr_num, "id": rid, "payload": body}
                        repo.reviews_posted.append(entry)
                    self._send_json(
                        201,
                        {
                            "id": rid,
                            "html_url": f"https://github.com/{repo.full_name}/pull/{pr_num}#pullrequestreview-{rid}",
                        },
                    )
                    return

                # POST /repos/{owner}/{repo}/issues/{number}/comments
                if (
                    method == "POST"
                    and len(parts) == 6
                    and parts[3] == "issues"
                    and parts[4].isdigit()
                    and parts[5] == "comments"
                ):
                    pr_num = int(parts[4])
                    with state.lock:
                        state._comment_seq += 1
                        cid = state._comment_seq
                        entry = {"pr_number": pr_num, "id": cid, "payload": body}
                        repo.issue_comments_posted.append(entry)
                    self._send_json(
                        201,
                        {
                            "id": cid,
                            "html_url": f"https://github.com/{repo.full_name}/pull/{pr_num}#issuecomment-{cid}",
                        },
                    )
                    return

            self._send_json(404, {"message": f"Unhandled mock endpoint {method} {self.path}"})

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

    return Handler


def _make_pr(
    *,
    number: int,
    title: str,
    head_sha: str,
    base_repo: str,
    base_repo_id: int,
    head_repo: Optional[str] = None,
    head_repo_id: Optional[int] = None,
    head_fork: bool = False,
    author_login: str = "maintainer1",
    author_association: str = "OWNER",
    labels: Tuple[str, ...] = ("eligible for review",),
    draft: bool = False,
) -> Dict[str, Any]:
    resolved_head_repo = head_repo or base_repo
    resolved_head_id = head_repo_id if head_repo_id is not None else base_repo_id
    return {
        "number": number,
        "state": "open",
        "draft": draft,
        "title": title,
        "body": f"Automated E2E test PR #{number}",
        "html_url": f"https://github.com/{base_repo}/pull/{number}",
        "user": {"login": author_login, "type": "User"},
        "author_association": author_association,
        "labels": [{"name": lbl} for lbl in labels],
        "head": {
            "ref": f"feature/pr-{number}",
            "sha": head_sha,
            "repo": {
                "id": resolved_head_id,
                "full_name": resolved_head_repo,
                "fork": head_fork,
            },
        },
        "base": {
            "ref": "main",
            "sha": "0" * 40,
            "repo": {
                "id": base_repo_id,
                "full_name": base_repo,
                "fork": False,
            },
        },
    }


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL CLI is required for RS256 App JWT E2E tests")
class OcrPipelineEndToEndTests(unittest.TestCase):
    """End-to-end CLI verification of the automated OCR pipeline against loopback GitHub API + OpenSSL."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="ocr-e2e-")
        cls.tmp_path = Path(cls._tmpdir.name)
        cls.private_key_path = cls.tmp_path / "ocr-app.private-key.pem"
        cls.public_key_path = cls.tmp_path / "ocr-app.public-key.pem"

        gen_proc = subprocess.run(
            ["openssl", "genrsa", "-out", str(cls.private_key_path), "2048"],
            capture_output=True,
            check=False,
        )
        if gen_proc.returncode != 0:
            raise RuntimeError(f"Failed generating test RSA key: {gen_proc.stderr.decode('utf-8', 'replace')}")
        os.chmod(cls.private_key_path, 0o600)

        pub_proc = subprocess.run(
            ["openssl", "rsa", "-in", str(cls.private_key_path), "-pubout", "-out", str(cls.public_key_path)],
            capture_output=True,
            check=False,
        )
        if pub_proc.returncode != 0:
            raise RuntimeError(f"Failed extracting RSA public key: {pub_proc.stderr.decode('utf-8', 'replace')}")

        cls.ocr_log_path = cls.tmp_path / "ocr-invocations.jsonl"
        cls.ocr_bin_path = cls.tmp_path / "fake-ocr"
        cls.ocr_bin_path.write_text(
            f"""#!/usr/bin/env python3
import json
import os
import sys

log_path = {str(cls.ocr_log_path)!r}
ctx_path = os.environ.get("OCR_PR_CONTEXT_FILE", "")
ctx = {{}}
if ctx_path and os.path.exists(ctx_path):
    with open(ctx_path, "r", encoding="utf-8") as fh:
        ctx = json.load(fh)

record = {{
    "repo": os.environ.get("OCR_REPOSITORY"),
    "pr": os.environ.get("OCR_PR_NUMBER"),
    "head_sha": os.environ.get("OCR_HEAD_SHA"),
    "no_update": os.environ.get("OCR_NO_UPDATE"),
    "llm_url": os.environ.get("OCR_LLM_URL"),
    "llm_model": os.environ.get("OCR_LLM_MODEL"),
    "use_anthropic": os.environ.get("OCR_LLM_USE_ANTHROPIC"),
    "has_auth_token": bool(os.environ.get("OCR_LLM_AUTH_TOKEN")),
    "files": [f.get("filename") for f in ctx.get("files", [])],
}}
with open(log_path, "a", encoding="utf-8") as out:
    out.write(json.dumps(record) + "\\n")

print(json.dumps({{
    "summary": f"E2E advisory findings for {{record['repo']}}#{{record['pr']}}@{{record['head_sha'][:8]}}",
    "findings": [
        {{
            "file": (record["files"][0] if record["files"] else "src/app.py"),
            "line": 12,
            "severity": "info",
            "message": "Check edge case handling on empty input.",
        }}
    ],
}}))
""",
            encoding="utf-8",
        )
        os.chmod(cls.ocr_bin_path, 0o755)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    def setUp(self) -> None:
        if self.ocr_log_path.exists():
            self.ocr_log_path.unlink()
        self._test_tmpdir = tempfile.TemporaryDirectory(prefix="ocr-e2e-run-")
        self.test_tmp_path = Path(self._test_tmpdir.name)
        self.app_id = "9001"
        self.admin_app_id = "1001"
        self.repo_a = "acme/alpha-repo"
        self.repo_b = "acme/beta-repo"
        self.repos = {
            self.repo_a: _RepoState(
                full_name=self.repo_a,
                repo_id=101,
                installation_id=501,
                pull_requests=[
                    _make_pr(
                        number=10,
                        title="Add solver test",
                        head_sha="a" * 40,
                        base_repo=self.repo_a,
                        base_repo_id=101,
                    ),
                    _make_pr(
                        number=11,
                        title="Untrusted fork PR",
                        head_sha="f" * 40,
                        base_repo=self.repo_a,
                        base_repo_id=101,
                        head_repo="external/alpha-repo",
                        head_repo_id=999,
                        head_fork=True,
                        author_login="external-contrib",
                        author_association="NONE",
                    ),
                ],
                pr_files={
                    10: [
                        {
                            "filename": "solver.py",
                            "status": "modified",
                            "additions": 5,
                            "deletions": 1,
                            "patch": "@@ -1 +1 @@",
                        }
                    ],
                    11: [{"filename": "evil.py", "status": "added", "additions": 10, "deletions": 0, "patch": "@@ +1 @@"}],
                },
            ),
            self.repo_b: _RepoState(
                full_name=self.repo_b,
                repo_id=202,
                installation_id=501,
                pull_requests=[
                    _make_pr(
                        number=20,
                        title="Update docs helper",
                        head_sha="b" * 40,
                        base_repo=self.repo_b,
                        base_repo_id=202,
                    ),
                ],
                pr_files={
                    20: [{"filename": "helper.py", "status": "modified", "additions": 3, "deletions": 0, "patch": "@@ +1 @@"}],
                },
            ),
        }
        self.server_state = _LoopbackGitHubState(
            app_id=self.app_id,
            admin_app_id=self.admin_app_id,
            public_key_path=self.public_key_path,
            repos=self.repos,
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.server_state))
        self.port = self.httpd.server_address[1]
        self.api_base_url = f"http://127.0.0.1:{self.port}"
        self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.server_thread.join(timeout=5)
        self._test_tmpdir.cleanup()

    def _run_cli(
        self,
        args: List[str],
        *,
        env_overrides: Optional[Dict[str, str]] = None,
        clear_provider_env: bool = False,
    ) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        for runner_key in ("RUNNER_ENVIRONMENT", "RUNNER_LABELS", "OCR_RUNNER_LABELS"):
            env.pop(runner_key, None)
        env["TMPDIR"] = str(self.test_tmp_path)
        if clear_provider_env:
            for key in ("OCR_LLM_AUTH_TOKEN", "OCR_LLM_URL", "OCR_LLM_MODEL", "OCR_LLM_USE_ANTHROPIC", "OCR_CLI_BIN"):
                env.pop(key, None)
        else:
            env.update(
                {
                    "OCR_LLM_AUTH_TOKEN": "e2e-fake-provider-secret-value",
                    "OCR_LLM_URL": "https://api.openai.com/v1",
                    "OCR_LLM_MODEL": "gpt-4o-mini",
                    "OCR_LLM_USE_ANTHROPIC": "false",
                    "OCR_CLI_BIN": str(self.ocr_bin_path),
                }
            )
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            [sys.executable, str(SCIFLOW_CICD), "ocr", *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT_DIR),
            check=False,
        )

    def _read_ocr_invocations(self) -> List[Dict[str, Any]]:
        if not self.ocr_log_path.exists():
            return []
        lines = [line for line in self.ocr_log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [json.loads(line) for line in lines]

    def test_limited_app_preflight_rejects_admin_app_and_polls_multiple_repositories(self) -> None:
        # 1. Admin/client App ID is rejected before any token minting
        bad_proc = self._run_cli(
            [
                "preflight-app",
                "--app-id",
                self.admin_app_id,
                "--admin-app-id",
                self.admin_app_id,
                "--private-key-path",
                str(self.private_key_path),
                "--allowlist",
                f"{self.repo_a},{self.repo_b}",
                "--api-base-url",
                self.api_base_url,
                "--json",
            ]
        )
        self.assertEqual(bad_proc.returncode, 1, bad_proc.stderr)
        bad_data = json.loads(bad_proc.stdout)
        self.assertFalse(bad_data["passed"])
        self.assertEqual(self.server_state.minted_tokens, {})

        # 2. Limited OCR App passes preflight with real OpenSSL RS256 JWT verification
        good_proc = self._run_cli(
            [
                "preflight-app",
                "--app-id",
                self.app_id,
                "--admin-app-id",
                self.admin_app_id,
                "--private-key-path",
                str(self.private_key_path),
                "--allowlist",
                f"{self.repo_a},{self.repo_b}",
                "--api-base-url",
                self.api_base_url,
                "--json",
            ]
        )
        self.assertEqual(good_proc.returncode, 0, good_proc.stderr)
        good_data = json.loads(good_proc.stdout)
        self.assertTrue(good_data["passed"])
        self.assertEqual(good_data["eligible_repositories"], [self.repo_a, self.repo_b])
        self.assertGreaterEqual(self.server_state.jwt_verifications, 1)

        # 3. Multi-repository round-robin polling across process restarts
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir) / "poll-state.json"
            poll_1 = self._run_cli(
                [
                    "poll",
                    "--app-id",
                    self.app_id,
                    "--admin-app-id",
                    self.admin_app_id,
                    "--private-key-path",
                    str(self.private_key_path),
                    "--allowlist",
                    f"{self.repo_a},{self.repo_b}",
                    "--api-base-url",
                    self.api_base_url,
                    "--max-repos-per-cycle",
                    "1",
                    "--max-cycles",
                    "1",
                    "--state-path",
                    str(state_path),
                    "--json",
                ]
            )
            self.assertEqual(poll_1.returncode, 0, poll_1.stderr)
            cycle_1 = json.loads(poll_1.stdout)["cycles"][0]
            self.assertEqual(cycle_1["polled_repositories"], [self.repo_a])
            self.assertEqual([c["number"] for c in cycle_1["candidates"]], [10])

            poll_2 = self._run_cli(
                [
                    "poll",
                    "--app-id",
                    self.app_id,
                    "--admin-app-id",
                    self.admin_app_id,
                    "--private-key-path",
                    str(self.private_key_path),
                    "--allowlist",
                    f"{self.repo_a},{self.repo_b}",
                    "--api-base-url",
                    self.api_base_url,
                    "--max-repos-per-cycle",
                    "1",
                    "--max-cycles",
                    "1",
                    "--state-path",
                    str(state_path),
                    "--json",
                ]
            )
            self.assertEqual(poll_2.returncode, 0, poll_2.stderr)
            cycle_2 = json.loads(poll_2.stdout)["cycles"][0]
            self.assertEqual(cycle_2["polled_repositories"], [self.repo_b])
            self.assertEqual([c["number"] for c in cycle_2["candidates"]], [20])

    def test_missing_credentials_report_pending_configuration_without_ocr_or_publish(self) -> None:
        preflight_proc = self._run_cli(
            [
                "preflight",
                "--app-id",
                self.app_id,
                "--admin-app-id",
                self.admin_app_id,
                "--private-key-path",
                str(self.private_key_path),
                "--allowlist",
                f"{self.repo_a},{self.repo_b}",
                "--api-base-url",
                self.api_base_url,
                "--ocr-bin",
                str(self.ocr_bin_path),
                "--json",
            ],
            clear_provider_env=True,
        )
        self.assertEqual(preflight_proc.returncode, 0, preflight_proc.stderr)
        preflight_data = json.loads(preflight_proc.stdout)
        self.assertEqual(preflight_data["status"], "pending_configuration")
        self.assertTrue(preflight_data["pending"])
        self.assertIn("OCR_LLM_AUTH_TOKEN", preflight_data["provider"]["missing_variables"])

        self.server_state.reset_requests()
        run_proc = self._run_cli(
            [
                "run",
                "--app-id",
                self.app_id,
                "--admin-app-id",
                self.admin_app_id,
                "--private-key-path",
                str(self.private_key_path),
                "--allowlist",
                f"{self.repo_a},{self.repo_b}",
                "--api-base-url",
                self.api_base_url,
                "--ocr-bin",
                str(self.ocr_bin_path),
                "--json",
            ],
            clear_provider_env=True,
        )
        self.assertEqual(run_proc.returncode, 0, run_proc.stderr)
        run_data = json.loads(run_proc.stdout)
        self.assertEqual(run_data["status"], "pending_configuration")
        self.assertTrue(run_data["pending"])
        self.assertTrue(run_data["passed"])
        self.assertEqual(run_data["reviews_posted"], 0)
        self.assertEqual(self._read_ocr_invocations(), [])
        self.assertFalse(
            any(
                "/files" in req["path"] or "/reviews" in req["path"] or "/comments" in req["path"]
                for req in self.server_state.requests
            )
        )

    def test_external_ocr_pipeline_runs_cleanly_without_actions_workflow_conflict(self) -> None:
        """External OCR daemon polls allowlisted repositories cleanly without checking or conflicting with in-repo Actions workflows."""
        active_workflow_yaml = (
            "name: Open Code Review\n"
            "on:\n"
            "  pull_request_target:\n"
            "    types: [opened, synchronize, reopened]\n"
            "jobs:\n"
            "  review:\n"
            "    runs-on: ubuntu-latest\n"
        )
        self.repos[self.repo_a].competing_workflow_yaml = active_workflow_yaml
        self.repos[self.repo_a].competing_workflow_state = "active"

        preflight_proc = self._run_cli(
            [
                "preflight",
                "--app-id",
                self.app_id,
                "--admin-app-id",
                self.admin_app_id,
                "--private-key-path",
                str(self.private_key_path),
                "--allowlist",
                f"{self.repo_a},{self.repo_b}",
                "--api-base-url",
                self.api_base_url,
                "--json",
            ]
        )
        self.assertEqual(preflight_proc.returncode, 0, preflight_proc.stderr)
        preflight_data = json.loads(preflight_proc.stdout)
        self.assertEqual(preflight_data["status"], "ready")
        self.assertEqual(preflight_data["competing_workflows"], [])
        self.assertEqual(preflight_data["indeterminate_workflows"], [])

        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir) / "ocr-state.json"
            run_proc = self._run_cli(
                [
                    "run",
                    "--app-id",
                    self.app_id,
                    "--admin-app-id",
                    self.admin_app_id,
                    "--private-key-path",
                    str(self.private_key_path),
                    "--allowlist",
                    f"{self.repo_a},{self.repo_b}",
                    "--api-base-url",
                    self.api_base_url,
                    "--state-path",
                    str(state_path),
                    "--json",
                ]
            )
        self.assertEqual(run_proc.returncode, 0, run_proc.stderr)
        run_data = json.loads(run_proc.stdout)
        self.assertEqual(run_data["status"], "completed")
        self.assertEqual(run_data["reviews_posted"], 2)
        self.assertEqual(len(self.repos[self.repo_a].reviews_posted), 1)
        self.assertEqual(len(self.repos[self.repo_b].reviews_posted), 1)
        invocations = self._read_ocr_invocations()
        self.assertEqual([inv["repo"] for inv in invocations], [self.repo_a, self.repo_b])

    def test_fork_prs_skipped_before_diff_read_or_ocr_invocation(self) -> None:
        safety_proc = self._run_cli(
            [
                "check-safety",
                "--app-id",
                self.app_id,
                "--admin-app-id",
                self.admin_app_id,
                "--private-key-path",
                str(self.private_key_path),
                "--allowlist",
                self.repo_a,
                "--api-base-url",
                self.api_base_url,
                "--json",
            ]
        )
        self.assertEqual(safety_proc.returncode, 0, safety_proc.stderr)
        safety_data = json.loads(safety_proc.stdout)
        self.assertTrue(safety_data["passed"])
        self.assertTrue(safety_data["runner_boundary"]["passed"])
        self.assertEqual([p["pr_number"] for p in safety_data["allowed_prs"]], [10])
        self.assertEqual([p["pr_number"] for p in safety_data["skipped_prs"]], [11])
        self.assertEqual(safety_data["skipped_prs"][0]["reason"], "fork_pr")

        # Keep only the fork PR open in repo_a to prove zero diff/OCR/publish calls occur
        self.repos[self.repo_a].pull_requests = [
            pr for pr in self.repos[self.repo_a].pull_requests if pr["number"] == 11
        ]
        self.server_state.reset_requests()

        run_proc = self._run_cli(
            [
                "run",
                "--app-id",
                self.app_id,
                "--admin-app-id",
                self.admin_app_id,
                "--private-key-path",
                str(self.private_key_path),
                "--allowlist",
                self.repo_a,
                "--api-base-url",
                self.api_base_url,
                "--json",
            ]
        )
        self.assertEqual(run_proc.returncode, 0, run_proc.stderr)
        run_data = json.loads(run_proc.stdout)
        self.assertEqual(run_data["status"], "no_candidates")
        self.assertEqual(run_data["reviews_posted"], 0)
        self.assertEqual(self._read_ocr_invocations(), [])
        self.assertFalse(
            any("/pulls/11/files" in req["path"] or "/pulls/11/reviews" in req["path"] for req in self.server_state.requests)
        )

    def test_per_head_deduplication_across_restarts_new_commit_rereview_and_advisory_only(self) -> None:
        sha_v1 = "a" * 40
        sha_v2 = "c" * 40
        with tempfile.TemporaryDirectory() as state_dir:
            state_path = Path(state_dir) / "ocr-review-state.json"
            lock_path = Path(state_dir) / "ocr-review.lock"

            # Pass 1: Reviews repo_a#10@sha_v1 and repo_b#20@b*40 with COMMENT review
            run_1 = self._run_cli(
                [
                    "run",
                    "--app-id",
                    self.app_id,
                    "--admin-app-id",
                    self.admin_app_id,
                    "--private-key-path",
                    str(self.private_key_path),
                    "--allowlist",
                    f"{self.repo_a},{self.repo_b}",
                    "--api-base-url",
                    self.api_base_url,
                    "--state-path",
                    str(state_path),
                    "--lock-path",
                    str(lock_path),
                    "--review-mode",
                    "pr_review",
                    "--json",
                ]
            )
            self.assertEqual(run_1.returncode, 0, run_1.stderr)
            data_1 = json.loads(run_1.stdout)
            self.assertEqual(data_1["status"], "completed")
            self.assertEqual(data_1["reviews_posted"], 2)
            self.assertEqual(len(self.repos[self.repo_a].reviews_posted), 1)
            posted_a1 = self.repos[self.repo_a].reviews_posted[0]["payload"]
            self.assertEqual(posted_a1["event"], "COMMENT")
            self.assertEqual(posted_a1["commit_id"], sha_v1)
            self.assertIn(
                f"sciflow-cicd-ocr-advisory:v1:{self.repo_a}#10@{sha_v1}",
                posted_a1["body"],
            )
            self.assertNotIn("e2e-fake-provider-secret-value", run_1.stdout)
            self.assertNotIn("e2e-fake-provider-secret-value", state_path.read_text(encoding="utf-8"))

            # Pass 2 (fresh process restart, unchanged HEADs): Deduplicates both PRs with 0 OCR runs
            if self.ocr_log_path.exists():
                self.ocr_log_path.unlink()
            run_2 = self._run_cli(
                [
                    "run",
                    "--app-id",
                    self.app_id,
                    "--admin-app-id",
                    self.admin_app_id,
                    "--private-key-path",
                    str(self.private_key_path),
                    "--allowlist",
                    f"{self.repo_a},{self.repo_b}",
                    "--api-base-url",
                    self.api_base_url,
                    "--state-path",
                    str(state_path),
                    "--lock-path",
                    str(lock_path),
                    "--review-mode",
                    "pr_review",
                    "--json",
                ]
            )
            self.assertEqual(run_2.returncode, 0, run_2.stderr)
            data_2 = json.loads(run_2.stdout)
            self.assertEqual(data_2["reviews_posted"], 0)
            self.assertEqual(data_2["skipped_already_reviewed"], 2)
            self.assertEqual(self._read_ocr_invocations(), [])
            self.assertEqual(len(self.repos[self.repo_a].reviews_posted), 1)

            # Pass 3: Advance PR #10 HEAD to sha_v2 and verify re-review in issue_comment advisory mode
            self.repos[self.repo_a].pull_requests[0]["head"]["sha"] = sha_v2
            run_3 = self._run_cli(
                [
                    "run",
                    "--app-id",
                    self.app_id,
                    "--admin-app-id",
                    self.admin_app_id,
                    "--private-key-path",
                    str(self.private_key_path),
                    "--allowlist",
                    f"{self.repo_a},{self.repo_b}",
                    "--api-base-url",
                    self.api_base_url,
                    "--state-path",
                    str(state_path),
                    "--lock-path",
                    str(lock_path),
                    "--review-mode",
                    "issue_comment",
                    "--json",
                ]
            )
            self.assertEqual(run_3.returncode, 0, run_3.stderr)
            data_3 = json.loads(run_3.stdout)
            self.assertEqual(data_3["reviews_posted"], 1)
            self.assertEqual(data_3["skipped_already_reviewed"], 1)
            invocations_3 = self._read_ocr_invocations()
            self.assertEqual(len(invocations_3), 1)
            self.assertEqual(invocations_3[0]["repo"], self.repo_a)
            self.assertEqual(invocations_3[0]["head_sha"], sha_v2)
            self.assertEqual(len(self.repos[self.repo_a].issue_comments_posted), 1)
            comment_body = self.repos[self.repo_a].issue_comments_posted[0]["payload"]["body"]
            self.assertIn(
                f"sciflow-cicd-ocr-advisory:v1:{self.repo_a}#10@{sha_v2}",
                comment_body,
            )

        # Ensure zero non-advisory endpoints were ever requested across the entire lifecycle
        forbidden_fragments = ("/check-runs", "/check-suites", "/statuses/", "/merge", "/actions/runners", "/graphql")
        for req in self.server_state.requests:
            for frag in forbidden_fragments:
                self.assertNotIn(frag, req["path"], f"Forbidden non-advisory endpoint called: {req}")

    def test_default_state_path_shared_poll_state_and_remote_marker_deduplication(self) -> None:
        # 1. Calling `sciflow-cicd ocr run` WITHOUT --state-path uses default_review_state_path(app_id)
        #    and deduplicates across process restarts out of the box.
        default_state = self.test_tmp_path / f"sciflow-cicd-ocr-state-{self.app_id}.json"
        self.assertFalse(default_state.exists())

        run_default_1 = self._run_cli(
            [
                "run",
                "--app-id",
                self.app_id,
                "--admin-app-id",
                self.admin_app_id,
                "--private-key-path",
                str(self.private_key_path),
                "--allowlist",
                f"{self.repo_a},{self.repo_b}",
                "--api-base-url",
                self.api_base_url,
                "--json",
            ]
        )
        self.assertEqual(run_default_1.returncode, 0, run_default_1.stderr)
        data_d1 = json.loads(run_default_1.stdout)
        self.assertEqual(data_d1["reviews_posted"], 2)
        self.assertTrue(default_state.exists())

        # 2. Running `sciflow-cicd ocr poll` sharing the same --state-path preserves pull_requests records.
        poll_shared = self._run_cli(
            [
                "poll",
                "--app-id",
                self.app_id,
                "--admin-app-id",
                self.admin_app_id,
                "--private-key-path",
                str(self.private_key_path),
                "--allowlist",
                f"{self.repo_a},{self.repo_b}",
                "--api-base-url",
                self.api_base_url,
                "--max-repos-per-cycle",
                "1",
                "--state-path",
                str(default_state),
                "--json",
            ]
        )
        self.assertEqual(poll_shared.returncode, 0, poll_shared.stderr)
        persisted_after_poll = json.loads(default_state.read_text(encoding="utf-8"))
        self.assertIn(f"{self.repo_a}#10", persisted_after_poll["pull_requests"])
        self.assertIn(f"{self.repo_b}#20", persisted_after_poll["pull_requests"])

        # 3. Second `sciflow-cicd ocr run` without --state-path skips both PRs via persisted default state.
        if self.ocr_log_path.exists():
            self.ocr_log_path.unlink()
        run_default_2 = self._run_cli(
            [
                "run",
                "--app-id",
                self.app_id,
                "--admin-app-id",
                self.admin_app_id,
                "--private-key-path",
                str(self.private_key_path),
                "--allowlist",
                f"{self.repo_a},{self.repo_b}",
                "--api-base-url",
                self.api_base_url,
                "--json",
            ]
        )
        self.assertEqual(run_default_2.returncode, 0, run_default_2.stderr)
        data_d2 = json.loads(run_default_2.stdout)
        self.assertEqual(data_d2["reviews_posted"], 0)
        self.assertEqual(data_d2["skipped_already_reviewed"], 2)
        self.assertEqual(self._read_ocr_invocations(), [])

        # 4. Delete local state file completely -> remote marker fallback skips both PRs without OCR or duplicate posts.
        default_state.unlink()
        self.assertFalse(default_state.exists())
        run_after_loss = self._run_cli(
            [
                "run",
                "--app-id",
                self.app_id,
                "--admin-app-id",
                self.admin_app_id,
                "--private-key-path",
                str(self.private_key_path),
                "--allowlist",
                f"{self.repo_a},{self.repo_b}",
                "--api-base-url",
                self.api_base_url,
                "--json",
            ]
        )
        self.assertEqual(run_after_loss.returncode, 0, run_after_loss.stderr)
        data_loss = json.loads(run_after_loss.stdout)
        self.assertEqual(data_loss["reviews_posted"], 0)
        self.assertEqual(data_loss["skipped_already_reviewed"], 2)
        self.assertEqual(self._read_ocr_invocations(), [])
        self.assertEqual(len(self.repos[self.repo_a].reviews_posted), 1)
        self.assertEqual(len(self.repos[self.repo_b].reviews_posted), 1)



if __name__ == "__main__":
    unittest.main()
