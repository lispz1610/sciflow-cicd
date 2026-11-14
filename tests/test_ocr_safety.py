"""Tests for strict fail-closed OCR fork safety boundary and runner guards."""

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

from scripts.ocr_poller import evaluate_pr_eligibility, is_same_repo_pull_request
from scripts.ocr_safety import (
    SelfHostedRunnerError,
    assert_no_self_hosted_runner,
    check_same_repo_origin,
    evaluate_repository_safety,
    filter_safe_prs,
    format_safety_report,
    guard_pr_for_ocr,
    run_ocr_safety_check,
    validate_ocr_api_path,
)


def _make_pr(
    *,
    number=42,
    head_full_name="acme/repo-a",
    base_full_name="acme/repo-a",
    head_id=101,
    base_id=101,
    head_fork=False,
    base_fork=False,
    author_login="alice",
    author_type="User",
    author_association="MEMBER",
    head_sha="a" * 40,
):
    return {
        "number": number,
        "state": "open",
        "title": f"PR #{number}",
        "user": {"login": author_login, "type": author_type},
        "author_association": author_association,
        "head": {
            "sha": head_sha,
            "ref": f"feat-{number}",
            "repo": {
                "id": head_id,
                "full_name": head_full_name,
                "fork": head_fork,
            },
        },
        "base": {
            "ref": "main",
            "repo": {
                "id": base_id,
                "full_name": base_full_name,
                "fork": base_fork,
            },
        },
    }


class SameRepoOriginBoundaryTests(unittest.TestCase):
    def test_allows_same_repo_pr_from_maintainer_or_agent_and_rejects_forks_and_malformed_metadata(self):
        maintainer_pr = _make_pr(
            number=1,
            author_login="maintainer-alice",
            author_type="User",
            author_association="OWNER",
        )
        agent_pr = _make_pr(
            number=2,
            author_login="codex-agent[bot]",
            author_type="Bot",
            author_association="NONE",
        )

        dec_maintainer = check_same_repo_origin(maintainer_pr, "acme/repo-a", allowlist=["acme/repo-a"])
        self.assertTrue(dec_maintainer.allowed)
        self.assertEqual(dec_maintainer.reason, "same_repository")
        self.assertEqual(dec_maintainer.pr_number, 1)

        dec_agent = check_same_repo_origin(agent_pr, "Acme/Repo-A", allowlist=["acme/repo-a"])
        self.assertTrue(dec_agent.allowed)
        self.assertEqual(dec_agent.reason, "same_repository")
        self.assertEqual(dec_agent.author_type, "Bot")

        # Cross-repo / fork PRs fail closed with fork_pr
        cross_repo_pr = _make_pr(number=3, head_full_name="outsider/repo-a", head_id=999, head_fork=True)
        dec_fork = check_same_repo_origin(cross_repo_pr, "acme/repo-a")
        self.assertFalse(dec_fork.allowed)
        self.assertEqual(dec_fork.reason, "fork_pr")

        # Fork flag alone with matching full_name fails closed
        spoofed_name_fork = _make_pr(number=4, head_full_name="acme/repo-a", head_fork=True)
        dec_spoof = check_same_repo_origin(spoofed_name_fork, "acme/repo-a")
        self.assertFalse(dec_spoof.allowed)
        self.assertEqual(dec_spoof.reason, "fork_pr")

        # Repository ID mismatch fails closed even if full_name matches
        id_mismatch_pr = _make_pr(number=5, head_id=101, base_id=202)
        dec_id = check_same_repo_origin(id_mismatch_pr, "acme/repo-a")
        self.assertFalse(dec_id.allowed)
        self.assertEqual(dec_id.reason, "repository_id_mismatch")

        # Mismatched expected repository / non-allowlisted repository
        other_repo_pr = _make_pr(number=6, head_full_name="acme/repo-b", base_full_name="acme/repo-b")
        dec_mismatch = check_same_repo_origin(other_repo_pr, "acme/repo-a")
        self.assertFalse(dec_mismatch.allowed)
        self.assertEqual(dec_mismatch.reason, "mismatched_repository")

        dec_unlisted = check_same_repo_origin(maintainer_pr, "acme/repo-a", allowlist=["acme/repo-b"])
        self.assertFalse(dec_unlisted.allowed)
        self.assertEqual(dec_unlisted.reason, "repository_not_allowlisted")

        # Missing or unreadable head / head.repo / base.repo fail closed with specific reasons
        missing_head_repo = _make_pr(number=7)
        missing_head_repo["head"]["repo"] = None
        dec_missing_hr = check_same_repo_origin(missing_head_repo, "acme/repo-a")
        self.assertFalse(dec_missing_hr.allowed)
        self.assertEqual(dec_missing_hr.reason, "missing_head_repository")

        unreadable_head_repo = _make_pr(number=8)
        unreadable_head_repo["head"]["repo"] = "corrupted"
        dec_bad_hr = check_same_repo_origin(unreadable_head_repo, "acme/repo-a")
        self.assertFalse(dec_bad_hr.allowed)
        self.assertEqual(dec_bad_hr.reason, "unreadable_head_repository")

        missing_head = _make_pr(number=9)
        del missing_head["head"]
        dec_missing_h = check_same_repo_origin(missing_head, "acme/repo-a")
        self.assertFalse(dec_missing_h.allowed)
        self.assertEqual(dec_missing_h.reason, "missing_head_metadata")

        unreadable_head = _make_pr(number=10)
        unreadable_head["head"] = ["not-a-dict"]
        dec_bad_h = check_same_repo_origin(unreadable_head, "acme/repo-a")
        self.assertFalse(dec_bad_h.allowed)
        self.assertEqual(dec_bad_h.reason, "unreadable_head_metadata")

        missing_base_repo = _make_pr(number=11)
        missing_base_repo["base"]["repo"] = None
        dec_missing_br = check_same_repo_origin(missing_base_repo, "acme/repo-a")
        self.assertFalse(dec_missing_br.allowed)
        self.assertEqual(dec_missing_br.reason, "missing_base_repository")

        unreadable_base_repo = _make_pr(number=12)
        unreadable_base_repo["base"]["repo"] = {"full_name": "invalid_repo_name"}
        dec_bad_br = check_same_repo_origin(unreadable_base_repo, "acme/repo-a")
        self.assertFalse(dec_bad_br.allowed)
        self.assertEqual(dec_bad_br.reason, "unreadable_base_repository")

        partial_id_pr = _make_pr(number=13)
        del partial_id_pr["head"]["repo"]["id"]
        dec_partial_id = check_same_repo_origin(partial_id_pr, "acme/repo-a")
        self.assertFalse(dec_partial_id.allowed)
        self.assertEqual(dec_partial_id.reason, "missing_repository_id")


class SelfHostedRunnerBoundaryTests(unittest.TestCase):
    def test_allows_local_direct_and_github_hosted_ci_env_while_rejecting_self_hosted_runner_signals(self):
        clean_status = assert_no_self_hosted_runner(env={"PATH": "/usr/bin"})
        self.assertTrue(clean_status.passed)
        self.assertEqual(clean_status.execution_mode, "local_direct")
        self.assertFalse(clean_status.self_hosted_runner_detected)
        self.assertEqual(clean_status.violations, ())

        # Standard GitHub-hosted CI environment must not false-positive
        ci_status = assert_no_self_hosted_runner(
            env={
                "GITHUB_ACTIONS": "true",
                "RUNNER_ENVIRONMENT": "github-hosted",
                "RUNNER_OS": "Linux",
                "RUNNER_NAME": "GitHub Actions 4",
            }
        )
        self.assertTrue(ci_status.passed)
        self.assertFalse(ci_status.self_hosted_runner_detected)

        # Reject non-local-direct execution mode
        mode_status = assert_no_self_hosted_runner(execution_mode="self-hosted", env={})
        self.assertFalse(mode_status.passed)
        self.assertTrue(mode_status.self_hosted_runner_detected)

        # Reject self-hosted runner env vars and registration tokens
        for bad_env in (
            {"RUNNER_REGISTRATION_TOKEN": "tok-secret-123"},
            {"ACTIONS_RUNNER_INPUT_TOKEN": "tok-secret-456"},
            {"RUNNER_ENVIRONMENT": "self-hosted"},
            {"OCR_EXECUTION_MODE": "self-hosted"},
            {"RUNNER_LABELS": "linux,self-hosted"},
        ):
            status = assert_no_self_hosted_runner(env=bad_env)
            self.assertFalse(status.passed, f"Expected failure for env={bad_env}")
            self.assertTrue(status.self_hosted_runner_detected)
            # Never leak token secrets in status detail/violations
            self.assertNotIn("tok-secret", status.detail)
            for v in status.violations:
                self.assertNotIn("tok-secret", v)

        # Reject self-hosted runner config dictionaries
        for bad_cfg in (
            {"self_hosted": True},
            {"runs_on": "self-hosted"},
            {"runs-on": ["ubuntu-latest", "self-hosted"]},
            {"registration_token": "reg-tok"},
            {"runner_registration_url": "https://github.com/acme/repo/actions/runners"},
        ):
            status = assert_no_self_hosted_runner(env={}, runner_config=bad_cfg)
            self.assertFalse(status.passed, f"Expected failure for runner_config={bad_cfg}")
            self.assertTrue(status.self_hosted_runner_detected)

        # Reject runner registration / JIT config endpoints
        self.assertEqual(
            validate_ocr_api_path("/repos/acme/repo-a/pulls?state=open"),
            "/repos/acme/repo-a/pulls?state=open",
        )
        for runner_path in (
            "/repos/acme/repo-a/actions/runners/registration-token",
            "/orgs/acme/actions/runners/generate-jitconfig",
            "/repos/acme/repo-a/actions/runners/remove-token",
        ):
            with self.assertRaises(SelfHostedRunnerError):
                validate_ocr_api_path(runner_path)
            path_status = assert_no_self_hosted_runner(env={}, api_paths=[runner_path])
            self.assertFalse(path_status.passed)
            self.assertTrue(path_status.self_hosted_runner_detected)

        with self.assertRaises(SelfHostedRunnerError):
            assert_no_self_hosted_runner(
                env={"RUNNER_ENVIRONMENT": "self-hosted"},
                raise_on_failure=True,
            )


class ExecutionGateWrapperTests(unittest.TestCase):
    def test_filter_safe_prs_partitions_same_repo_from_forks_and_malformed_payloads(self):
        maintainer_pr = _make_pr(number=10, author_login="alice", author_type="User")
        bot_pr = _make_pr(number=11, author_login="dependabot[bot]", author_type="Bot")
        fork_pr = _make_pr(number=12, head_full_name="evil/repo-a", head_id=999, head_fork=True)
        missing_hr = _make_pr(number=13)
        missing_hr["head"]["repo"] = None
        bad_head = _make_pr(number=14)
        bad_head["head"] = "corrupt"
        mismatch_id = _make_pr(number=15, head_id=101, base_id=888)

        safe_prs, allowed, skipped = filter_safe_prs(
            [maintainer_pr, bot_pr, fork_pr, missing_hr, bad_head, mismatch_id],
            "acme/repo-a",
            allowlist=["acme/repo-a"],
        )

        self.assertEqual([pr["number"] for pr in safe_prs], [10, 11])
        self.assertEqual([d.pr_number for d in allowed], [10, 11])
        self.assertEqual(
            [(d.pr_number, d.reason) for d in skipped],
            [
                (12, "fork_pr"),
                (13, "missing_head_repository"),
                (14, "unreadable_head_metadata"),
                (15, "repository_id_mismatch"),
            ],
        )

    def test_guard_pr_for_ocr_triggers_zero_executor_calls_for_unsafe_prs(self):
        import inspect

        sig = inspect.signature(guard_pr_for_ocr)
        for removed in ("checkout_fn", "diff_reader", "ocr_runner", "provider_caller"):
            self.assertNotIn(removed, sig.parameters)

        events = []

        def executor_fn(pr, decision):
            events.append(("executor", pr["number"], decision.reason))
            return "executed"

        fork_pr = _make_pr(number=20, head_full_name="attacker/repo-a", head_id=777, head_fork=True)
        missing_hr = _make_pr(number=21)
        missing_hr["head"]["repo"] = None
        bad_hr = _make_pr(number=22)
        bad_hr["head"]["repo"] = 12345
        id_mismatch = _make_pr(number=23, head_id=101, base_id=505)

        for unsafe_pr, expected_reason in (
            (fork_pr, "fork_pr"),
            (missing_hr, "missing_head_repository"),
            (bad_hr, "unreadable_head_repository"),
            (id_mismatch, "repository_id_mismatch"),
        ):
            res_exec = guard_pr_for_ocr(
                unsafe_pr,
                "acme/repo-a",
                allowlist=["acme/repo-a"],
                executor=executor_fn,
                env={},
            )
            self.assertFalse(res_exec.executed)
            self.assertFalse(res_exec.decision.allowed)
            self.assertEqual(res_exec.decision.reason, expected_reason)

        # Self-hosted runner env also blocks even a same-repo PR before any execution
        same_repo_pr = _make_pr(number=24, author_login="agent-bot[bot]", author_type="Bot")
        res_runner = guard_pr_for_ocr(
            same_repo_pr,
            "acme/repo-a",
            allowlist=["acme/repo-a"],
            executor=executor_fn,
            env={"RUNNER_REGISTRATION_TOKEN": "secret-reg-token"},
        )
        self.assertFalse(res_runner.executed)
        self.assertFalse(res_runner.runner_boundary.passed)
        self.assertEqual(res_runner.decision.reason, "self_hosted_runner_forbidden")
        self.assertEqual(events, [])

        # Valid same-repo agent PR executes cleanly under local_direct mode
        ok_exec = guard_pr_for_ocr(
            same_repo_pr,
            "acme/repo-a",
            allowlist=["acme/repo-a"],
            executor=executor_fn,
            env={},
        )
        self.assertTrue(ok_exec.executed)
        self.assertTrue(ok_exec.decision.allowed)
        self.assertEqual(ok_exec.result, "executed")
        self.assertEqual(events, [("executor", 24, "same_repository")])

    def test_ocr_poller_enforces_strict_repo_id_and_allows_maintainer_and_agent_same_repo_prs(self):
        id_mismatch = _make_pr(number=30, head_id=101, base_id=202)
        id_mismatch["labels"] = [{"name": "eligible for review"}]
        self.assertFalse(is_same_repo_pull_request(id_mismatch, "acme/repo-a"))
        self.assertEqual(
            evaluate_pr_eligibility(id_mismatch, "acme/repo-a"),
            (False, "fork_or_cross_repo"),
        )

        missing_hr = _make_pr(number=31)
        missing_hr["head"]["repo"] = None
        missing_hr["labels"] = [{"name": "eligible for review"}]
        self.assertFalse(is_same_repo_pull_request(missing_hr, "acme/repo-a"))
        self.assertEqual(
            evaluate_pr_eligibility(missing_hr, "acme/repo-a"),
            (False, "fork_or_cross_repo"),
        )

        agent_pr = _make_pr(
            number=32,
            author_login="ci-agent[bot]",
            author_type="Bot",
            author_association="NONE",
        )
        agent_pr["labels"] = [{"name": "eligible for review"}]
        self.assertTrue(is_same_repo_pull_request(agent_pr, "acme/repo-a"))
        self.assertEqual(
            evaluate_pr_eligibility(agent_pr, "acme/repo-a"),
            (True, "label:eligible for review"),
        )

        maintainer_pr = _make_pr(
            number=33,
            author_login="alice",
            author_type="User",
            author_association="OWNER",
        )
        self.assertTrue(is_same_repo_pull_request(maintainer_pr, "acme/repo-a"))
        self.assertEqual(
            evaluate_pr_eligibility(maintainer_pr, "acme/repo-a"),
            (True, "maintainer:OWNER"),
        )


class _FakeSafetyGitHubServer:
    def __init__(self, pulls_by_repo):
        self.pulls_by_repo = pulls_by_repo
        self.calls = []

    def api_factory(self, bearer_token):
        server = self

        class _Client:
            def request_with_headers(self, method, path, payload=None):
                server.calls.append((bearer_token, method, path, payload))
                if method == "GET" and path == "/app":
                    return 200, {}, {
                        "id": 3333,
                        "slug": "ocr-limited-app",
                        "name": "OCR Limited App",
                        "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                    }
                if method == "GET" and path.startswith("/app/installations"):
                    if "page=1" in path or "?" not in path:
                        return 200, {}, [
                            {
                                "id": 701,
                                "account": {"login": "acme", "type": "Organization"},
                                "repository_selection": "selected",
                                "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                            }
                        ]
                    return 200, {}, []
                if method == "POST" and path == "/app/installations/701/access_tokens":
                    return 201, {}, {
                        "token": "ghs_safety_secret_token_999",
                        "expires_at": "2026-11-14T05:00:00Z",
                        "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                    }
                if method == "GET" and path.startswith("/installation/repositories"):
                    if "page=1" in path or "?" not in path:
                        return 200, {}, {
                            "total_count": 1,
                            "repositories": [{"id": 101, "name": "repo-a", "full_name": "acme/repo-a"}],
                        }
                    return 200, {}, {"total_count": 1, "repositories": []}
                if method == "GET" and path.startswith("/repos/acme/repo-a/pulls"):
                    if "page=1" in path or "?" not in path:
                        return 200, {}, server.pulls_by_repo.get("acme/repo-a", [])
                    return 200, {}, []
                return 404, {}, {"message": "Not Found"}

            def request(self, method, path, payload=None):
                status, _headers, body = self.request_with_headers(method, path, payload=payload)
                return status, body

        return _Client()


class SafetyCheckCliAndLoopbackTests(unittest.TestCase):
    def test_evaluate_repository_safety_and_run_ocr_safety_check_text_and_runner_rejection(self):
        maintainer_pr = _make_pr(number=10, author_login="alice", author_type="User", author_association="OWNER")
        agent_pr = _make_pr(number=11, author_login="codex[bot]", author_type="Bot", author_association="NONE")
        fork_pr = _make_pr(number=12, head_full_name="outsider/repo-a", head_id=999, head_fork=True)
        missing_hr = _make_pr(number=13)
        missing_hr["head"]["repo"] = None
        mismatch_id = _make_pr(number=14, head_id=101, base_id=202)
        issue_item = {
            "number": 15,
            "state": "open",
            "title": "Not a PR",
            "html_url": "https://github.com/acme/repo-a/issues/15",
        }

        fake = _FakeSafetyGitHubServer(
            {"acme/repo-a": [maintainer_pr, agent_pr, fork_pr, missing_hr, mismatch_id, issue_item]}
        )

        result = evaluate_repository_safety(
            app_id=3333,
            allowlist=["acme/repo-a"],
            api_factory=fake.api_factory,
            signer=lambda data: b"fake-sig",
            env={},
        )
        self.assertTrue(result.passed)
        self.assertTrue(result.runner_boundary.passed)
        self.assertEqual([d.pr_number for d in result.allowed_prs], [10, 11])
        self.assertEqual(
            [(d.pr_number, d.reason) for d in result.skipped_prs],
            [
                (12, "fork_pr"),
                (13, "missing_head_repository"),
                (14, "repository_id_mismatch"),
            ],
        )

        report = format_safety_report(result)
        self.assertIn("PASS", report)
        self.assertIn("fork_pr", report)
        self.assertIn("missing_head_repository", report)
        self.assertNotIn("ghs_safety_secret_token_999", report)

        # Text CLI runner succeeds when clean and fails closed on self-hosted runner env
        out = io.StringIO()
        err = io.StringIO()
        rc_ok = run_ocr_safety_check(
            app_id=3333,
            private_key_path=None,
            allowlist=["acme/repo-a"],
            api_factory=fake.api_factory,
            signer=lambda data: b"fake-sig",
            env={},
            out_stream=out,
            err_stream=err,
        )
        self.assertEqual(rc_ok, 0)
        self.assertIn("Allowed PRs (2)", out.getvalue())
        self.assertIn("Skipped PRs (3)", out.getvalue())

        calls_before = len(fake.calls)
        out_bad = io.StringIO()
        err_bad = io.StringIO()
        rc_bad = run_ocr_safety_check(
            app_id=3333,
            private_key_path=None,
            allowlist=["acme/repo-a"],
            api_factory=fake.api_factory,
            signer=lambda data: b"fake-sig",
            env={"RUNNER_REGISTRATION_TOKEN": "reg-secret-xyz"},
            out_stream=out_bad,
            err_stream=err_bad,
        )
        self.assertEqual(rc_bad, 1)
        # Runner boundary fails before any PR polling/API calls happen
        self.assertEqual(len(fake.calls), calls_before)
        self.assertNotIn("reg-secret-xyz", out_bad.getvalue() + err_bad.getvalue())

    def test_gh_setup_ocr_check_safety_end_to_end_with_openssl_key_and_loopback_http(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            priv_key = Path(tmpdir) / "ocr-app.pem"
            subprocess.run(
                ["openssl", "genrsa", "-out", str(priv_key), "2048"],
                check=True,
                capture_output=True,
            )
            os.chmod(priv_key, 0o600)

            maintainer_pr = _make_pr(number=101, author_login="alice", author_type="User", author_association="OWNER")
            agent_pr = _make_pr(number=102, author_login="ocr-agent[bot]", author_type="Bot", author_association="NONE")
            fork_pr = _make_pr(number=103, head_full_name="forker/repo-a", head_id=777, head_fork=True)
            missing_hr = _make_pr(number=104)
            missing_hr["head"]["repo"] = None
            malformed_head = _make_pr(number=105)
            malformed_head["head"] = "corrupt"

            seen_paths = []

            class _Handler(BaseHTTPRequestHandler):
                def log_message(self, format, *args):
                    return

                def _send_json(self, code, payload):
                    data = json.dumps(payload).encode("utf-8")
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

                def do_GET(self):
                    seen_paths.append(self.path)
                    if self.path == "/app":
                        self._send_json(
                            200,
                            {
                                "id": 7777,
                                "slug": "ocr-limited-app",
                                "name": "OCR Limited App",
                                "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                            },
                        )
                    elif self.path.startswith("/app/installations"):
                        self._send_json(
                            200,
                            [
                                {
                                    "id": 901,
                                    "account": {"login": "acme", "type": "Organization"},
                                    "repository_selection": "selected",
                                    "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                                }
                            ],
                        )
                    elif self.path.startswith("/installation/repositories"):
                        self._send_json(
                            200,
                            {
                                "total_count": 1,
                                "repositories": [{"id": 101, "name": "repo-a", "full_name": "acme/repo-a"}],
                            },
                        )
                    elif self.path.startswith("/repos/acme/repo-a/pulls"):
                        self._send_json(200, [maintainer_pr, agent_pr, fork_pr, missing_hr, malformed_head])
                    else:
                        self._send_json(404, {"message": "Not Found"})

                def do_POST(self):
                    seen_paths.append(self.path)
                    self._send_json(
                        201,
                        {
                            "token": "ghs_loopback_safety_secret_456",
                            "expires_at": "2026-11-14T05:00:00Z",
                            "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                        },
                    )

            server = HTTPServer(("127.0.0.1", 0), _Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                clean_env = {k: v for k, v in os.environ.items() if k != "RUNNER_ENVIRONMENT"}
                proc = subprocess.run(
                    [
                        str(ROOT_DIR / "gh-setup"),
                        "ocr",
                        "check-safety",
                        "--app-id",
                        "7777",
                        "--private-key-path",
                        str(priv_key),
                        "--allowlist",
                        "acme/repo-a",
                        "--api-base-url",
                        base_url,
                        "--json",
                    ],
                    env=clean_env,
                    capture_output=True,
                    text=True,
                )
            finally:
                server.shutdown()
                server.server_close()

            self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}")
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["passed"])
            self.assertTrue(payload["runner_boundary"]["passed"])
            self.assertEqual([d["pr_number"] for d in payload["allowed_prs"]], [101, 102])
            self.assertEqual(
                [(d["pr_number"], d["reason"]) for d in payload["skipped_prs"]],
                [(103, "fork_pr"), (104, "missing_head_repository"), (105, "unreadable_head_metadata")],
            )
            self.assertFalse(any("/actions/runners" in p for p in seen_paths))
            self.assertNotIn("ghs_loopback_safety_secret_456", proc.stdout + proc.stderr)
            self.assertNotIn("eyJ", proc.stdout + proc.stderr)
            self.assertNotIn("PRIVATE KEY", proc.stdout + proc.stderr)

    def test_safe_client_request_and_evaluate_repository_safety_with_app_config(self):
        from scripts.ocr_app_auth import OcrAppConfig
        from scripts.ocr_safety import safe_client_request

        class _Client:
            def request(self, method, path, payload=None):
                if path == "/app":
                    return 200, {
                        "id": 7777,
                        "slug": "ocr-bot",
                        "name": "OCR Bot",
                        "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                    }
                if path.startswith("/app/installations?"):
                    return 200, [
                        {
                            "id": 50,
                            "account": {"login": "acme"},
                            "repository_selection": "selected",
                            "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                        }
                    ]
                if path.startswith("/installation/repositories?"):
                    return 200, {"repositories": [{"full_name": "acme/repo-a"}]}
                if path == "/app/installations/50/access_tokens":
                    return 201, {
                        "token": "ghs_safety_cfg_secret",
                        "expires_at": "2026-11-14T05:00:00Z",
                        "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                        "repositories": [{"full_name": "acme/repo-a"}],
                    }
                if path.startswith("/repos/acme/repo-a/pulls"):
                    return 200, [_make_pr(number=99)]
                return 404, {"message": "Not Found"}

        with self.assertRaises(SelfHostedRunnerError):
            safe_client_request(_Client(), "GET", "/orgs/acme/actions/runners")

        status, body = safe_client_request(_Client(), "GET", "/repos/acme/repo-a/pulls?state=open")
        self.assertEqual(status, 200)
        self.assertEqual(body[0]["number"], 99)

        cfg = OcrAppConfig(
            app_id=7777,
            allowlist=("acme/repo-a",),
            api_factory=lambda _tok: _Client(),
            signer=lambda _d: b"sig",
        )
        res = evaluate_repository_safety(app_config=cfg, env={})
        self.assertTrue(res.passed)
        self.assertEqual([d.pr_number for d in res.allowed_prs], [99])


if __name__ == "__main__":
    unittest.main()
