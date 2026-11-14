"""Tests for advisory per-HEAD OCR review execution and durable deduplication."""

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

from scripts.ocr_reviewer import (
    OcrExecutionError,
    OcrReviewError,
    advisory_review_marker,
    run_ocr_review_pass,
    run_ocr_reviews,
    validate_advisory_publish_request,
)
from scripts.ocr_poller import ActiveEvaluationLock
from scripts.ocr_safety import SelfHostedRunnerError


def _make_pr(
    *,
    number=42,
    head_sha="a" * 40,
    base_sha="0" * 40,
    head_ref="feat-ocr",
    base_ref="main",
    head_full_name="acme/repo-a",
    base_full_name="acme/repo-a",
    head_id=101,
    base_id=101,
    head_fork=False,
    base_fork=False,
    state="open",
    draft=False,
    author_login="alice",
    author_type="User",
    author_association="MEMBER",
    labels=None,
):
    return {
        "number": number,
        "state": state,
        "draft": draft,
        "title": f"PR #{number}: Improve parser",
        "body": "Refines parser bounds.",
        "html_url": f"https://github.com/{base_full_name}/pull/{number}",
        "user": {"login": author_login, "type": author_type},
        "author_association": author_association,
        "labels": labels if labels is not None else [{"name": "eligible for review"}],
        "head": {
            "sha": head_sha,
            "ref": head_ref,
            "repo": {
                "id": head_id,
                "full_name": head_full_name,
                "fork": head_fork,
            },
        },
        "base": {
            "sha": base_sha,
            "ref": base_ref,
            "repo": {
                "id": base_id,
                "full_name": base_full_name,
                "fork": base_fork,
            },
        },
    }


def _ready_env(extra=None):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "OCR_LLM_AUTH_TOKEN": "sk-test-secret-token-value",
        "OCR_LLM_URL": "https://api.example.com/v1",
        "OCR_LLM_MODEL": "gpt-4o-mini",
        "OCR_LLM_USE_ANTHROPIC": "false",
    }
    if extra:
        env.update(extra)
    return env


class FakeReviewGitHubServer:
    """In-memory fake GitHub REST API supporting OCR preflight, polling, and review publication."""

    def __init__(
        self,
        *,
        app_id=3333,
        repositories=("acme/repo-a",),
        repo_metadata=None,
        pulls_by_repo=None,
        files_by_pr=None,
        contents_by_repo=None,
        actions_workflows_by_repo=None,
        on_get_pr=None,
        fail_publish_statuses=None,
        seeded_reviews_by_pr=None,
        seeded_comments_by_pr=None,
        fail_list_reviews_statuses=None,
    ):
        self.app_info = {
            "id": int(app_id),
            "slug": "ocr-limited-app",
            "name": "OCR Limited App",
            "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
        }
        self.installations = [
            {
                "id": 901,
                "account": {"login": "acme", "type": "Organization"},
                "repository_selection": "selected",
                "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
            }
        ]
        self.repositories = list(repositories)
        self.repo_metadata = repo_metadata if repo_metadata is not None else {}
        self.pulls_by_repo = pulls_by_repo if pulls_by_repo is not None else {}
        self.files_by_pr = files_by_pr if files_by_pr is not None else {}
        self.contents_by_repo = contents_by_repo if contents_by_repo is not None else {}
        self.actions_workflows_by_repo = actions_workflows_by_repo if actions_workflows_by_repo is not None else {}
        self.on_get_pr = on_get_pr
        self.fail_publish_statuses = list(fail_publish_statuses or [])
        self.seeded_reviews_by_pr = seeded_reviews_by_pr if seeded_reviews_by_pr is not None else {}
        self.seeded_comments_by_pr = seeded_comments_by_pr if seeded_comments_by_pr is not None else {}
        self.fail_list_reviews_statuses = list(fail_list_reviews_statuses or [])
        self.calls = []
        self.posted_reviews = []
        self.posted_issue_comments = []
        self.token_to_meta = {}
        self._seq = 0
        self._comment_seq = 1000

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
                    repos = [
                        {"id": 100 + idx, "name": repo.split("/")[1], "full_name": repo}
                        for idx, repo in enumerate(server.repositories, start=1)
                    ]
                    if "page=1" in path or "?" not in path:
                        return 200, {}, {"total_count": len(repos), "repositories": repos}
                    return 200, {}, {"total_count": len(repos), "repositories": []}

                path_only, _, query = path.partition("?")
                parts = path_only.split("/")
                if len(parts) >= 4 and parts[1] == "repos":
                    repo_full = f"{parts[2]}/{parts[3]}"
                    if len(parts) == 4 and method == "GET":
                        meta = server.repo_metadata.get(repo_full)
                        if meta is None:
                            return 200, {}, {"full_name": repo_full, "default_branch": "main"}
                        if isinstance(meta, Exception):
                            raise meta
                        if isinstance(meta, tuple):
                            return meta[0], {}, meta[1]
                        return 200, {}, meta

                    if len(parts) >= 6 and parts[4] == "contents" and method == "GET":
                        file_path = "/".join(parts[5:])
                        entry = server.contents_by_repo.get(repo_full, {}).get(file_path)
                        if entry is None:
                            return 404, {}, {"message": "Not Found"}
                        if isinstance(entry, Exception):
                            raise entry
                        if isinstance(entry, tuple):
                            return entry[0], {}, entry[1]
                        return 200, {}, entry

                    if len(parts) == 7 and parts[4] == "actions" and parts[5] == "workflows" and method == "GET":
                        wf_name = parts[6]
                        wf = server.actions_workflows_by_repo.get(repo_full, {}).get(wf_name)
                        if wf is None:
                            return 404, {}, {"message": "Not Found"}
                        return 200, {}, wf

                    if len(parts) == 5 and parts[4] == "pulls" and method == "GET":
                        if "page=1" in query or not query:
                            return 200, {}, list(server.pulls_by_repo.get(repo_full, []))
                        return 200, {}, []

                    if len(parts) == 6 and parts[4] == "pulls" and method == "GET":
                        pr_num = int(parts[5])
                        if server.on_get_pr is not None:
                            server.on_get_pr(repo_full, pr_num)
                        for item in server.pulls_by_repo.get(repo_full, []):
                            if item.get("number") == pr_num:
                                return 200, {}, dict(item)
                        return 404, {}, {"message": "Not Found"}

                    if len(parts) == 7 and parts[4] == "pulls" and parts[6] == "files" and method == "GET":
                        pr_num = int(parts[5])
                        return 200, {}, list(server.files_by_pr.get((repo_full, pr_num), []))

                    if len(parts) == 7 and parts[4] == "pulls" and parts[6] == "reviews" and method == "GET":
                        pr_num = int(parts[5])
                        if server.fail_list_reviews_statuses:
                            code = server.fail_list_reviews_statuses.pop(0)
                            return code, {}, {"message": f"Injected HTTP {code}"}
                        items = list(server.seeded_reviews_by_pr.get((repo_full, pr_num), []))
                        for posted in server.posted_reviews:
                            if posted["repository"] == repo_full and posted["pr_number"] == pr_num:
                                items.append(
                                    {
                                        "id": posted["id"],
                                        "html_url": posted["html_url"],
                                        "body": posted["payload"].get("body", ""),
                                        "commit_id": posted["payload"].get("commit_id"),
                                    }
                                )
                        return server._paginate(items, query)

                    if len(parts) == 7 and parts[4] == "issues" and parts[6] == "comments" and method == "GET":
                        pr_num = int(parts[5])
                        items = list(server.seeded_comments_by_pr.get((repo_full, pr_num), []))
                        for posted in server.posted_issue_comments:
                            if posted["repository"] == repo_full and posted["pr_number"] == pr_num:
                                items.append(
                                    {
                                        "id": posted["id"],
                                        "html_url": posted["html_url"],
                                        "body": posted["payload"].get("body", ""),
                                    }
                                )
                        return server._paginate(items, query)

                    if len(parts) == 7 and parts[4] == "pulls" and parts[6] == "reviews" and method == "POST":
                        pr_num = int(parts[5])
                        if server.fail_publish_statuses:
                            code = server.fail_publish_statuses.pop(0)
                            return code, {}, {"message": f"Injected HTTP {code}"}
                        server._comment_seq += 1
                        record = {
                            "id": server._comment_seq,
                            "repository": repo_full,
                            "pr_number": pr_num,
                            "payload": dict(payload or {}),
                            "html_url": (
                                f"https://github.com/{repo_full}/pull/{pr_num}"
                                f"#pullrequestreview-{server._comment_seq}"
                            ),
                        }
                        server.posted_reviews.append(record)
                        return 200, {}, {"id": record["id"], "html_url": record["html_url"]}

                    if len(parts) == 7 and parts[4] == "issues" and parts[6] == "comments" and method == "POST":
                        pr_num = int(parts[5])
                        if server.fail_publish_statuses:
                            code = server.fail_publish_statuses.pop(0)
                            return code, {}, {"message": f"Injected HTTP {code}"}
                        server._comment_seq += 1
                        record = {
                            "id": server._comment_seq,
                            "repository": repo_full,
                            "pr_number": pr_num,
                            "payload": dict(payload or {}),
                            "html_url": f"https://github.com/{repo_full}/issues/{pr_num}#issuecomment-{server._comment_seq}",
                        }
                        server.posted_issue_comments.append(record)
                        return 201, {}, {"id": record["id"], "html_url": record["html_url"]}

                return 404, {}, {"message": f"Unhandled {method} {path}"}

            def request(self, method, path, payload=None):
                status, _headers, body = self.request_with_headers(method, path, payload=payload)
                if status >= 400 and not (method == "GET" and status == 404):
                    from scripts.configure_github_repo import ApiError
                    msg = body.get("message", f"HTTP {status}") if isinstance(body, dict) else f"HTTP {status}"
                    raise ApiError(f"{method} {path}: HTTP {status}: {msg}")
                return status, body

        return _Client()

    @staticmethod
    def _paginate(items, query):
        from urllib.parse import parse_qs

        params = parse_qs(query or "")
        per_page = int((params.get("per_page") or ["100"])[0])
        page = int((params.get("page") or ["1"])[0])
        start = max(0, (page - 1) * per_page)
        return 200, {}, items[start : start + per_page]


class AdvisoryOcrDeduplicationTests(unittest.TestCase):
    def test_posts_one_advisory_review_per_head_sha_and_skips_unchanged_head_across_restart(self):
        sha1 = "a" * 40
        pr = _make_pr(number=42, head_sha=sha1)
        fake = FakeReviewGitHubServer(
            pulls_by_repo={"acme/repo-a": [pr]},
            files_by_pr={
                ("acme/repo-a", 42): [
                    {"filename": "parser.py", "status": "modified", "additions": 3, "deletions": 1}
                ]
            },
        )
        ocr_calls = []

        def _fake_ocr(context):
            ocr_calls.append(context["head_sha"])
            return {
                "summary": "Found one bounds check suggestion.",
                "findings": [
                    {"file": "parser.py", "line": 12, "severity": "medium", "message": "Check upper bound."}
                ],
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "ocr-state.json"
            lock_path = Path(tmpdir) / "ocr.lock"

            first = run_ocr_review_pass(
                app_id=3333,
                private_key_path=None,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"fake-sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=_fake_ocr,
            )

            self.assertTrue(first.passed)
            self.assertEqual(first.status, "completed")
            self.assertEqual(len(ocr_calls), 1)
            self.assertEqual(len(fake.posted_reviews), 1)
            posted = fake.posted_reviews[0]["payload"]
            self.assertEqual(posted["event"], "COMMENT")
            self.assertEqual(posted["commit_id"], sha1)
            self.assertIn(advisory_review_marker("acme/repo-a", 42, sha1), posted["body"])
            self.assertIn("Check upper bound.", posted["body"])
            self.assertTrue(state_path.exists())

            # Second pass simulating a process restart with unchanged HEAD SHA skips silently
            second = run_ocr_review_pass(
                app_id=3333,
                private_key_path=None,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"fake-sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=_fake_ocr,
            )

            self.assertTrue(second.passed)
            self.assertEqual(len(ocr_calls), 1)
            self.assertEqual(len(fake.posted_reviews), 1)
            self.assertEqual(second.outcomes[0].outcome, "skipped_already_reviewed")

    def test_run_ocr_reviews_uses_default_state_path_and_deduplicates_without_flag(self):
        from unittest.mock import patch
        from scripts.ocr_reviewer import default_review_state_path

        sha1 = "d" * 40
        pr = _make_pr(number=43, head_sha=sha1)
        fake = FakeReviewGitHubServer(pulls_by_repo={"acme/repo-a": [pr]})
        ocr_calls = []

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("scripts.ocr_state.tempfile.gettempdir", return_value=tmpdir), patch(
                "scripts.ocr_poller.tempfile.gettempdir", return_value=tmpdir
            ):
                expected_state = Path(tmpdir) / "sciflow-cicd-ocr-state-3333.json"
                self.assertEqual(default_review_state_path(3333), expected_state)
                self.assertEqual(default_review_state_path("3333"), expected_state)

                out1 = io.StringIO()
                rc1 = run_ocr_reviews(
                    app_id="03333",
                    allowlist=["acme/repo-a"],
                    state_path=None,
                    lock_path=None,
                    env=_ready_env(),
                    api_factory=fake.api_factory,
                    signer=lambda data: b"sig",
                    which_fn=lambda cmd: "/usr/local/bin/ocr",
                    ocr_executor=lambda ctx: (ocr_calls.append(ctx["head_sha"]), {"summary": "ok", "findings": []})[1],
                    json_output=True,
                    out_stream=out1,
                )
                self.assertEqual(rc1, 0)
                payload1 = json.loads(out1.getvalue())
                self.assertEqual(payload1["reviews_posted"], 1)
                self.assertTrue(expected_state.exists())

                out2 = io.StringIO()
                rc2 = run_ocr_reviews(
                    app_id=3333,
                    allowlist=["acme/repo-a"],
                    state_path=None,
                    lock_path=None,
                    env=_ready_env(),
                    api_factory=fake.api_factory,
                    signer=lambda data: b"sig",
                    which_fn=lambda cmd: "/usr/local/bin/ocr",
                    ocr_executor=lambda ctx: (ocr_calls.append(ctx["head_sha"]), {"summary": "ok", "findings": []})[1],
                    json_output=True,
                    out_stream=out2,
                )
                self.assertEqual(rc2, 0)
                payload2 = json.loads(out2.getvalue())
                self.assertEqual(payload2["reviews_posted"], 0)
                self.assertEqual(payload2["skipped_already_reviewed"], 1)
                self.assertEqual(len(ocr_calls), 1)
                self.assertEqual(len(fake.posted_reviews), 1)

    def test_new_commit_triggers_fresh_review_and_mid_run_head_change_aborts_stale_post(self):
        sha1 = "1" * 40
        sha2 = "2" * 40
        sha3 = "3" * 40
        sha4 = "4" * 40
        pr = _make_pr(number=51, head_sha=sha1)
        fake = FakeReviewGitHubServer(pulls_by_repo={"acme/repo-a": [pr]})

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            lock_path = Path(tmpdir) / "ocr.lock"

            # 1. Review sha1
            res1 = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: {"summary": f"Review for {ctx['head_sha']}", "findings": []},
            )
            self.assertTrue(res1.passed)
            self.assertEqual(res1.reviews_posted, 1)
            self.assertEqual(fake.posted_reviews[-1]["payload"]["commit_id"], sha1)

            # 2. Push new commit sha2 -> triggers fresh review tied to sha2
            pr["head"]["sha"] = sha2
            res2 = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: {"summary": f"Review for {ctx['head_sha']}", "findings": []},
            )
            self.assertTrue(res2.passed)
            self.assertEqual(res2.reviews_posted, 1)
            self.assertEqual(len(fake.posted_reviews), 2)
            self.assertEqual(fake.posted_reviews[-1]["payload"]["commit_id"], sha2)

            # 3. Push sha3, but during OCR execution the PR HEAD advances to sha4 -> abort stale post
            pr["head"]["sha"] = sha3

            def _mid_run_advancing_ocr(ctx):
                self.assertEqual(ctx["head_sha"], sha3)
                pr["head"]["sha"] = sha4
                return {"summary": "Stale findings for sha3", "findings": []}

            res3 = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=_mid_run_advancing_ocr,
            )
            self.assertTrue(res3.passed)
            self.assertEqual(res3.reviews_posted, 0)
            self.assertEqual(res3.head_changed_mid_run, 1)
            self.assertEqual(res3.outcomes[0].outcome, "head_changed_mid_run")
            self.assertEqual(res3.outcomes[0].observed_new_head_sha, sha4)
            # Still only 2 posted reviews (no stale review posted on sha3 or sha4)
            self.assertEqual(len(fake.posted_reviews), 2)

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            pr_state = saved["pull_requests"]["acme/repo-a#51"]
            self.assertEqual(pr_state["heads"][sha3]["status"], "head_changed_mid_run")
            self.assertEqual(pr_state["heads"][sha3]["observed_new_head_sha"], sha4)

            # 4. Next poll picks up sha4 and posts fresh review for sha4
            res4 = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: {"summary": f"Review for {ctx['head_sha']}", "findings": []},
            )
            self.assertTrue(res4.passed)
            self.assertEqual(res4.reviews_posted, 1)
            self.assertEqual(len(fake.posted_reviews), 3)
            self.assertEqual(fake.posted_reviews[-1]["payload"]["commit_id"], sha4)
            self.assertIn(advisory_review_marker("acme/repo-a", 51, sha4), fake.posted_reviews[-1]["payload"]["body"])

    def test_remote_marker_fallback_skips_when_local_state_missing_and_allows_new_sha(self):
        sha_old = "a" * 40
        sha_reviewed = "b" * 40
        sha_commented = "c" * 40
        sha_new = "d" * 40

        pr_review_marked = _make_pr(number=61, head_sha=sha_reviewed)
        pr_comment_marked = _make_pr(number=62, head_sha=sha_commented)

        # Put the matching marker on page 2 (>100 reviews) to verify pagination handling
        filler_reviews = [
            {"id": idx, "body": "Human comment without advisory marker", "html_url": f"https://example.test/{idx}"}
            for idx in range(1, 102)
        ]
        filler_reviews.append(
            {
                "id": 9901,
                "body": f"{advisory_review_marker('acme/repo-a', 61, sha_reviewed)}\nPrior advisory review",
                "html_url": "https://github.com/acme/repo-a/pull/61#pullrequestreview-9901",
            }
        )
        # Issue comment with marker for PR #62 (even though run uses review_mode='pr_review'), plus old SHA marker
        seeded_comments = [
            {
                "id": 8800,
                "body": f"{advisory_review_marker('acme/repo-a', 62, sha_old)}\nOld commit",
                "html_url": "https://github.com/acme/repo-a/issues/62#issuecomment-8800",
            },
            {
                "id": 8801,
                "body": f"{advisory_review_marker('acme/repo-a', 62, sha_commented)}\nCurrent commit",
                "html_url": "https://github.com/acme/repo-a/issues/62#issuecomment-8801",
            },
        ]

        fake = FakeReviewGitHubServer(
            pulls_by_repo={"acme/repo-a": [pr_review_marked, pr_comment_marked]},
            seeded_reviews_by_pr={("acme/repo-a", 61): filler_reviews},
            seeded_comments_by_pr={("acme/repo-a", 62): seeded_comments},
        )
        ocr_calls = []

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "fresh-state.json"
            lock_path = Path(tmpdir) / "ocr.lock"

            res = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                review_mode="pr_review",
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: (ocr_calls.append(ctx), {"summary": "Should not run", "findings": []})[1],
            )
            self.assertTrue(res.passed, res.errors)
            self.assertEqual(res.reviews_posted, 0)
            self.assertEqual(res.skipped_already_reviewed, 2)
            self.assertEqual(ocr_calls, [])
            self.assertEqual(fake.posted_reviews, [])
            self.assertEqual(fake.posted_issue_comments, [])
            self.assertFalse(any("/files" in call[2] for call in fake.calls))

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["pull_requests"]["acme/repo-a#61"]["heads"][sha_reviewed]["status"],
                "reviewed",
            )
            self.assertEqual(
                saved["pull_requests"]["acme/repo-a#62"]["heads"][sha_commented]["status"],
                "reviewed",
            )

            # Advancing PR #62 to sha_new triggers a fresh review despite prior markers for old SHAs
            pr_comment_marked["head"]["sha"] = sha_new
            res2 = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                review_mode="pr_review",
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: (ocr_calls.append(ctx["head_sha"]), {"summary": "New SHA review", "findings": []})[1],
            )
            self.assertTrue(res2.passed, res2.errors)
            self.assertEqual(res2.reviews_posted, 1)
            self.assertEqual(res2.skipped_already_reviewed, 1)
            self.assertEqual(ocr_calls, [sha_new])
            self.assertEqual(len(fake.posted_reviews), 1)
            self.assertEqual(fake.posted_reviews[0]["payload"]["commit_id"], sha_new)

    def test_remote_marker_inspection_error_fails_safely_without_duplicate_post(self):
        sha = "e" * 40
        pr = _make_pr(number=63, head_sha=sha)
        fake = FakeReviewGitHubServer(
            pulls_by_repo={"acme/repo-a": [pr]},
            seeded_reviews_by_pr={
                ("acme/repo-a", 63): [
                    {
                        "id": 9902,
                        "body": advisory_review_marker("acme/repo-a", 63, sha),
                        "html_url": "https://github.com/acme/repo-a/pull/63#pullrequestreview-9902",
                    }
                ]
            },
            fail_list_reviews_statuses=[503],
        )
        ocr_calls = []

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            lock_path = Path(tmpdir) / "ocr.lock"

            res_err = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: (ocr_calls.append(ctx), {"summary": "Should not run", "findings": []})[1],
            )
            self.assertFalse(res_err.passed)
            self.assertEqual(res_err.status, "retryable_failure")
            self.assertEqual(res_err.retryable_failures, 1)
            self.assertEqual(ocr_calls, [])
            self.assertEqual(fake.posted_reviews, [])

            # Once remote endpoint recovers, marker is found and skipped without OCR
            res_ok = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: (ocr_calls.append(ctx), {"summary": "Should not run", "findings": []})[1],
            )
            self.assertTrue(res_ok.passed, res_ok.errors)
            self.assertEqual(res_ok.skipped_already_reviewed, 1)
            self.assertEqual(res_ok.reviews_posted, 0)
            self.assertEqual(ocr_calls, [])
            self.assertEqual(fake.posted_reviews, [])


class AdvisoryOnlyEnforcementTests(unittest.TestCase):
    def test_issue_comment_mode_and_forbids_non_advisory_events_checks_merges_and_drafts(self):
        sha = "b" * 40
        pr = _make_pr(number=77, head_sha=sha)
        fake = FakeReviewGitHubServer(pulls_by_repo={"acme/repo-a": [pr]})

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            state_path = tmp / "state.json"
            lock_path = tmp / "ocr.lock"

            # Forbidden review modes fail closed before any API calls
            for bad_mode in ("APPROVE", "REQUEST_CHANGES", "approve", "request_changes", "merge", "check_run"):
                res = run_ocr_review_pass(
                    app_id=3333,
                    allowlist=["acme/repo-a"],
                    state_path=state_path,
                    lock_path=lock_path,
                    review_mode=bad_mode,
                    env=_ready_env(),
                    api_factory=fake.api_factory,
                    signer=lambda data: b"sig",
                    which_fn=lambda cmd: "/usr/local/bin/ocr",
                )
                self.assertFalse(res.passed)
                self.assertEqual(res.status, "error")
                self.assertEqual(len(fake.calls), 0)

            # Direct publish guard rejects all non-advisory mutations
            for method, path, payload in (
                ("POST", "/repos/acme/repo-a/pulls/77/reviews", {"commit_id": sha, "event": "APPROVE", "body": "lgtm"}),
                (
                    "POST",
                    "/repos/acme/repo-a/pulls/77/reviews",
                    {"commit_id": sha, "event": "REQUEST_CHANGES", "body": "fix"},
                ),
                ("POST", "/repos/acme/repo-a/check-runs", {"name": "ocr", "head_sha": sha}),
                ("POST", f"/repos/acme/repo-a/statuses/{sha}", {"state": "success"}),
                ("PUT", "/repos/acme/repo-a/pulls/77/merge", {"commit_title": "merge"}),
                ("PATCH", "/repos/acme/repo-a/pulls/77", {"state": "closed"}),
                ("POST", "/graphql", {"query": "mutation { markPullRequestReadyForReview }"}),
                ("POST", "/repos/acme/repo-a/actions/runners/registration-token", {}),
            ):
                with self.assertRaises((OcrReviewError, SelfHostedRunnerError)):
                    validate_advisory_publish_request(method, path, payload, expected_head_sha=sha)

            # OCR output attempting to coerce APPROVE is rejected without posting
            res_bad_out = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: {"event": "APPROVE", "summary": "Approve PR"},
            )
            self.assertFalse(res_bad_out.passed)
            self.assertEqual(len(fake.posted_reviews), 0)
            self.assertEqual(len(fake.posted_issue_comments), 0)

            # Valid issue_comment mode posts advisory comment with header + HEAD SHA marker
            res_comment = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                review_mode="issue_comment",
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: {"summary": "Advisory note.", "findings": []},
            )
            self.assertTrue(res_comment.passed)
            self.assertEqual(len(fake.posted_issue_comments), 1)
            body = fake.posted_issue_comments[0]["payload"]["body"]
            self.assertIn("Advisory Open Code Review", body)
            self.assertIn(advisory_review_marker("acme/repo-a", 77, sha), body)
            # Ensure zero non-advisory API endpoints were ever touched
            for _tok, method, path, _payload in fake.calls:
                if method != "GET" and not path.endswith("/access_tokens"):
                    self.assertTrue(path.endswith("/issues/77/comments"))


class FailureRetryPreflightAndSafetyTests(unittest.TestCase):
    def test_transient_failures_record_retryable_state_and_recover_on_retry(self):
        sha = "c" * 40
        pr = _make_pr(number=88, head_sha=sha)
        fake = FakeReviewGitHubServer(
            pulls_by_repo={"acme/repo-a": [pr]},
            fail_publish_statuses=[503],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            lock_path = Path(tmpdir) / "ocr.lock"

            # 1. Transient OCR failure containing a secret must be redacted and recorded as retryable_failure
            def _failing_ocr(_ctx):
                raise OcrExecutionError("provider 502 with sk-test-secret-token-value")

            r1 = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=_failing_ocr,
            )
            self.assertFalse(r1.passed)
            self.assertEqual(r1.status, "retryable_failure")
            self.assertEqual(r1.retryable_failures, 1)
            self.assertEqual(r1.outcomes[0].attempts, 1)
            self.assertNotIn("sk-test-secret-token-value", json.dumps(r1.to_dict()))
            self.assertEqual(len(fake.posted_reviews), 0)

            # 2. OCR succeeds, but GitHub publish returns 503 -> still retryable_failure, attempts == 2
            r2 = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: {"summary": "Looks good.", "findings": []},
            )
            self.assertFalse(r2.passed)
            self.assertEqual(r2.retryable_failures, 1)
            self.assertEqual(r2.outcomes[0].attempts, 2)
            saved_mid = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_mid["pull_requests"]["acme/repo-a#88"]["heads"][sha]["status"], "retryable_failure")
            self.assertEqual(saved_mid["pull_requests"]["acme/repo-a#88"]["heads"][sha]["attempts"], 2)

            # 3. Third pass succeeds and marks reviewed
            r3 = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: {"summary": "Looks good.", "findings": []},
            )
            self.assertTrue(r3.passed)
            self.assertEqual(r3.reviews_posted, 1)
            self.assertEqual(len(fake.posted_reviews), 1)
            saved_ok = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_ok["pull_requests"]["acme/repo-a#88"]["heads"][sha]["status"], "reviewed")
            self.assertEqual(saved_ok["pull_requests"]["acme/repo-a#88"]["heads"][sha]["attempts"], 3)

    def test_unexpected_internal_errors_are_not_downgraded_to_retryable_failure(self):
        sha = "f" * 40
        pr = _make_pr(number=89, head_sha=sha)
        fake = FakeReviewGitHubServer(pulls_by_repo={"acme/repo-a": [pr]})

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            for exc_cls in (RuntimeError, TypeError, KeyError):
                def _buggy_executor(_ctx, err_cls=exc_cls):
                    raise err_cls(f"unexpected {err_cls.__name__}")

                with self.assertRaises(exc_cls):
                    run_ocr_review_pass(
                        app_id=3333,
                        allowlist=["acme/repo-a"],
                        state_path=tmp / f"state-{exc_cls.__name__}.json",
                        lock_path=tmp / "ocr.lock",
                        env=_ready_env(),
                        api_factory=fake.api_factory,
                        signer=lambda data: b"sig",
                        which_fn=lambda cmd: "/usr/local/bin/ocr",
                        ocr_executor=_buggy_executor,
                    )

    def test_pending_configuration_competing_workflow_fork_and_self_hosted_boundaries(self):
        pr_safe = _make_pr(number=91, head_sha="d" * 40)
        pr_fork = _make_pr(number=92, head_sha="e" * 40, head_full_name="outsider/repo-a", head_id=999, head_fork=True)
        fake = FakeReviewGitHubServer(pulls_by_repo={"acme/repo-a": [pr_safe, pr_fork]})

        # Missing provider config -> pending_configuration returns exit 0 cleanly without blocking
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            state_path = tmp / "state.json"
            lock_path = tmp / "ocr.lock"

            rc_pending = run_ocr_reviews(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                env={"PATH": os.environ.get("PATH", "")},
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                json_output=True,
                out_stream=out,
            )
            self.assertEqual(rc_pending, 0)
            pending_payload = json.loads(out.getvalue())
            self.assertEqual(pending_payload["status"], "pending_configuration")
            self.assertTrue(pending_payload["pending"])
            self.assertEqual(len(fake.posted_reviews), 0)

            # Self-hosted runner env fails closed before any API/OCR calls
            calls_before = len(fake.calls)
            ocr_invoked = []
            res_runner = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                env=_ready_env({"RUNNER_ENVIRONMENT": "self-hosted"}),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: ocr_invoked.append(ctx),
            )
            self.assertFalse(res_runner.passed)
            self.assertEqual(len(fake.calls), calls_before)
            self.assertEqual(ocr_invoked, [])

            # Competing active Actions workflow always skips review with warning and migration guidance
            import base64

            wf_b64 = base64.b64encode(b"on:\n  pull_request_target:\n").decode("ascii")
            fake_competing = FakeReviewGitHubServer(
                pulls_by_repo={"acme/repo-a": [pr_safe]},
                contents_by_repo={
                    "acme/repo-a": {
                        ".github/workflows/open-code-review.yml": {
                            "type": "file",
                            "encoding": "base64",
                            "content": wf_b64,
                        }
                    }
                },
                actions_workflows_by_repo={"acme/repo-a": {"open-code-review.yml": {"state": "active"}}},
            )
            res_competing = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                env=_ready_env(),
                api_factory=fake_competing.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: {"summary": "Should not run", "findings": []},
            )
            self.assertTrue(res_competing.passed)
            self.assertEqual(res_competing.status, "skipped_competing_workflow")
            self.assertEqual(res_competing.skipped_competing_workflow, 1)
            self.assertEqual(len(fake_competing.posted_reviews), 0)
            self.assertTrue(any("Disable the workflow" in inst for inst in res_competing.instructions))
            with self.assertRaises(TypeError):
                run_ocr_review_pass(
                    app_id=3333,
                    allowlist=["acme/repo-a"],
                    state_path=state_path,
                    lock_path=lock_path,
                    allow_competing_workflow=True,
                    env=_ready_env(),
                    api_factory=fake_competing.api_factory,
                    signer=lambda data: b"sig",
                )

            # Fork PR is excluded while same-repo PR proceeds
            seen_prs = []
            res_fork = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: (
                    seen_prs.append(ctx["pr_number"]),
                    {"summary": "Safe PR only.", "findings": []},
                )[1],
            )
            self.assertTrue(res_fork.passed)
            self.assertEqual(seen_prs, [91])

    def test_indeterminate_competing_workflow_fails_closed_without_crashing_clean_repos(self):
        import base64

        pr_api_err = _make_pr(
            number=101, head_sha="1" * 40, base_full_name="acme/repo-api-err", head_full_name="acme/repo-api-err"
        )
        pr_bad_wf = _make_pr(
            number=102, head_sha="2" * 40, base_full_name="acme/repo-bad-wf", head_full_name="acme/repo-bad-wf"
        )
        pr_clean = _make_pr(number=103, head_sha="3" * 40, base_full_name="acme/repo-clean", head_full_name="acme/repo-clean")
        pr_inactive = _make_pr(
            number=104, head_sha="4" * 40, base_full_name="acme/repo-inactive", head_full_name="acme/repo-inactive"
        )

        inactive_b64 = base64.b64encode(b"on:\n  pull_request_target:\n").decode("ascii")

        # 1. Single indeterminate repo (network error on branch lookup) -> zero OCR runs or PR posts
        single_err_server = FakeReviewGitHubServer(
            repositories=("acme/repo-api-err",),
            repo_metadata={"acme/repo-api-err": OSError("DNS lookup failed")},
            pulls_by_repo={"acme/repo-api-err": [pr_api_err]},
        )
        ocr_calls_single = []
        with tempfile.TemporaryDirectory() as single_tmp:
            res_single = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-api-err"],
                state_path=Path(single_tmp) / "state.json",
                lock_path=Path(single_tmp) / "ocr.lock",
                env=_ready_env(),
                api_factory=single_err_server.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: (
                    ocr_calls_single.append(ctx),
                    {"summary": "Must not run", "findings": []},
                )[1],
            )
        self.assertTrue(res_single.passed)
        self.assertEqual(res_single.status, "skipped_competing_workflow")
        self.assertEqual(res_single.skipped_competing_workflow, 1)
        self.assertEqual(res_single.skipped_indeterminate_workflow, 1)
        self.assertEqual(ocr_calls_single, [])
        self.assertEqual(single_err_server.posted_reviews, [])
        self.assertTrue(any("acme/repo-api-err" in w for w in res_single.warnings))
        self.assertTrue(any("preflight" in inst.lower() for inst in res_single.instructions))

        # 2. Mixed repos: indeterminate repos fail closed while clean 404 and inactive repos proceed
        mixed_server = FakeReviewGitHubServer(
            repositories=(
                "acme/repo-api-err",
                "acme/repo-bad-wf",
                "acme/repo-clean",
                "acme/repo-inactive",
            ),
            pulls_by_repo={
                "acme/repo-api-err": [pr_api_err],
                "acme/repo-bad-wf": [pr_bad_wf],
                "acme/repo-clean": [pr_clean],
                "acme/repo-inactive": [pr_inactive],
            },
            contents_by_repo={
                "acme/repo-api-err": {
                    ".github/workflows/open-code-review.yml": (503, {"message": "Service Unavailable"}),
                },
                "acme/repo-bad-wf": {
                    ".github/workflows/open-code-review.yml": {
                        "type": "file",
                        "encoding": "base64",
                        "content": "%%%invalid-base64%%%",
                    },
                },
                "acme/repo-inactive": {
                    ".github/workflows/open-code-review.yml": {
                        "type": "file",
                        "encoding": "base64",
                        "content": inactive_b64,
                    },
                },
            },
            actions_workflows_by_repo={
                "acme/repo-inactive": {
                    "open-code-review.yml": {"id": 55, "state": "disabled_manually"},
                },
            },
        )
        seen_repos = []
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            rc = run_ocr_reviews(
                app_id=3333,
                allowlist=[
                    "acme/repo-api-err",
                    "acme/repo-bad-wf",
                    "acme/repo-clean",
                    "acme/repo-inactive",
                ],
                state_path=Path(tmpdir) / "state.json",
                lock_path=Path(tmpdir) / "ocr.lock",
                env=_ready_env(),
                api_factory=mixed_server.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: (
                    seen_repos.append(ctx["repository"]),
                    {"summary": "Clean repo review.", "findings": []},
                )[1],
                out_stream=out,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(seen_repos, ["acme/repo-clean", "acme/repo-inactive"])
        self.assertEqual(
            [r["repository"] for r in mixed_server.posted_reviews],
            ["acme/repo-clean", "acme/repo-inactive"],
        )
        report_text = out.getvalue()
        self.assertIn("Skipped (competing workflow): 2", report_text)
        self.assertIn("Skipped (indeterminate competing workflow): 2", report_text)
        self.assertIn("acme/repo-api-err", report_text)
        self.assertIn("acme/repo-bad-wf", report_text)

    def test_rejects_symlinked_state_path(self):
        fake = FakeReviewGitHubServer()
        with tempfile.TemporaryDirectory() as tmpdir:
            real_file = Path(tmpdir) / "real.json"
            real_file.write_text("{}\n", encoding="utf-8")
            link_file = Path(tmpdir) / "link.json"
            os.symlink(real_file, link_file)

            res = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=link_file,
                lock_path=Path(tmpdir) / "ocr.lock",
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
            )
            self.assertFalse(res.passed)
            self.assertEqual(res.status, "error")
            self.assertIn("Symlinks", res.errors[0])

    def test_single_flight_lock_busy_and_max_reviews_round_robin(self):
        pr_a = _make_pr(number=10, head_sha="a" * 40, head_full_name="acme/repo-a", base_full_name="acme/repo-a")
        pr_b = _make_pr(number=20, head_sha="b" * 40, head_full_name="acme/repo-b", base_full_name="acme/repo-b")
        fake = FakeReviewGitHubServer(
            repositories=("acme/repo-a", "acme/repo-b"),
            pulls_by_repo={"acme/repo-a": [pr_a], "acme/repo-b": [pr_b]},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            lock_path = Path(tmpdir) / "ocr.lock"

            # When single-flight lock is already held, review pass skips cleanly without calling OCR
            ocr_calls = []
            with ActiveEvaluationLock(lock_path, repository="acme/repo-a", pr_number=10, head_sha="a" * 40):
                busy_res = run_ocr_review_pass(
                    app_id=3333,
                    allowlist=["acme/repo-a", "acme/repo-b"],
                    state_path=Path(tmpdir) / "busy-state.json",
                    lock_path=lock_path,
                    env=_ready_env(),
                    api_factory=fake.api_factory,
                    signer=lambda data: b"sig",
                    which_fn=lambda cmd: "/usr/local/bin/ocr",
                    ocr_executor=lambda ctx: (ocr_calls.append(ctx), {"summary": "ok", "findings": []})[1],
                )
            self.assertTrue(busy_res.passed)
            self.assertTrue(busy_res.skipped_busy)
            self.assertEqual(busy_res.status, "skipped_busy")
            self.assertEqual(ocr_calls, [])

            # max_reviews=1 reviews repo-a first, then next pass reviews repo-b
            r1 = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a", "acme/repo-b"],
                state_path=state_path,
                lock_path=lock_path,
                max_reviews=1,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: {"summary": f"Reviewed {ctx['repository']}", "findings": []},
            )
            self.assertTrue(r1.passed)
            self.assertEqual(r1.reviews_posted, 1)
            self.assertEqual(fake.posted_reviews[0]["repository"], "acme/repo-a")

            r2 = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a", "acme/repo-b"],
                state_path=state_path,
                lock_path=lock_path,
                max_reviews=1,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: {"summary": f"Reviewed {ctx['repository']}", "findings": []},
            )
            self.assertTrue(r2.passed)
            self.assertEqual(r2.reviews_posted, 1)
            self.assertEqual(fake.posted_reviews[1]["repository"], "acme/repo-b")

    def test_real_subprocess_ocr_failure_and_timeout_record_retryable_state(self):
        sha = "9" * 40
        pr = _make_pr(number=30, head_sha=sha)
        fake = FakeReviewGitHubServer(pulls_by_repo={"acme/repo-a": [pr]})

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            state_path = tmp / "state.json"
            lock_path = tmp / "ocr.lock"
            fail_ocr = tmp / "fail-ocr"
            fail_ocr.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stderr.write('upstream 503 with sk-test-secret-token-value\\n')\n"
                "sys.exit(2)\n",
                encoding="utf-8",
            )
            os.chmod(fail_ocr, 0o700)

            res_fail = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                ocr_bin=str(fail_ocr),
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
            )
            self.assertFalse(res_fail.passed)
            self.assertEqual(res_fail.status, "retryable_failure")
            self.assertEqual(res_fail.outcomes[0].attempts, 1)
            self.assertNotIn("sk-test-secret-token-value", res_fail.outcomes[0].error or "")
            self.assertEqual(len(fake.posted_reviews), 0)

            slow_ocr = tmp / "slow-ocr"
            slow_ocr.write_text(
                "#!/usr/bin/env python3\nimport time\ntime.sleep(2)\n",
                encoding="utf-8",
            )
            os.chmod(slow_ocr, 0o700)

            res_timeout = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=state_path,
                lock_path=lock_path,
                ocr_bin=str(slow_ocr),
                ocr_timeout_seconds=0.1,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
            )
            self.assertFalse(res_timeout.passed)
            self.assertEqual(res_timeout.status, "retryable_failure")
            self.assertEqual(res_timeout.outcomes[0].attempts, 2)
            self.assertIn("timed out", res_timeout.outcomes[0].error or "")

    def test_busy_lock_does_not_read_or_overwrite_holder_state(self):
        pr_a = _make_pr(number=10, head_sha="a" * 40, head_full_name="acme/repo-a", base_full_name="acme/repo-a")
        fake = FakeReviewGitHubServer(pulls_by_repo={"acme/repo-a": [pr_a]})
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            state_path = tmp / "state.json"
            lock_path = tmp / "ocr.lock"
            holder_payload = json.dumps(
                {
                    "version": 1,
                    "next_index": 1,
                    "next_repository": "acme/repo-b",
                    "pull_requests": {
                        "acme/repo-a#10": {
                            "status": "reviewed",
                            "last_reviewed_head_sha": "a" * 40,
                            "heads": {"a" * 40: {"status": "reviewed"}},
                        }
                    },
                },
                indent=2,
            ) + "\n"
            state_path.write_text(holder_payload, encoding="utf-8")
            uncreated_state = tmp / "uncreated-state.json"

            with ActiveEvaluationLock(lock_path, repository="acme/repo-a", pr_number=10, head_sha="a" * 40):
                busy_res = run_ocr_review_pass(
                    app_id=3333,
                    allowlist=["acme/repo-a"],
                    state_path=state_path,
                    lock_path=lock_path,
                    env=_ready_env(),
                    api_factory=fake.api_factory,
                    signer=lambda data: b"sig",
                    which_fn=lambda cmd: "/usr/local/bin/ocr",
                    ocr_executor=lambda ctx: {"summary": "ok", "findings": []},
                )
                busy_res_new = run_ocr_review_pass(
                    app_id=3333,
                    allowlist=["acme/repo-a"],
                    state_path=uncreated_state,
                    lock_path=lock_path,
                    env=_ready_env(),
                    api_factory=fake.api_factory,
                    signer=lambda data: b"sig",
                    which_fn=lambda cmd: "/usr/local/bin/ocr",
                    ocr_executor=lambda ctx: {"summary": "ok", "findings": []},
                )

            self.assertTrue(busy_res.passed)
            self.assertTrue(busy_res.skipped_busy)
            self.assertEqual(busy_res.status, "skipped_busy")
            self.assertEqual(state_path.read_text(encoding="utf-8"), holder_payload)

            self.assertTrue(busy_res_new.passed)
            self.assertTrue(busy_res_new.skipped_busy)
            self.assertEqual(busy_res_new.status, "skipped_busy")
            self.assertFalse(uncreated_state.exists())

    def test_default_lock_paths_match_and_mutually_exclude_poll_and_run(self):
        from unittest.mock import patch
        from scripts.ocr_poller import default_evaluation_lock_path, poll_once

        expected_lock = Path(tempfile.gettempdir()) / "sciflow-cicd-ocr-eval-3333.lock"
        self.assertEqual(default_evaluation_lock_path(3333), expected_lock)
        self.assertEqual(default_evaluation_lock_path("3333"), expected_lock)

        pr_a = _make_pr(number=11, head_sha="a" * 40)
        fake = FakeReviewGitHubServer(pulls_by_repo={"acme/repo-a": [pr_a]})
        nested_run_results = []

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("scripts.ocr_poller.tempfile.gettempdir", return_value=tmpdir), patch(
                "scripts.ocr_state.tempfile.gettempdir", return_value=tmpdir
            ):
                def _poll_evaluator(_cand):
                    nested_run_results.append(
                        run_ocr_review_pass(
                            app_id=3333,
                            allowlist=["acme/repo-a"],
                            state_path=Path(tmpdir) / "state.json",
                            lock_path=None,
                            env=_ready_env(),
                            api_factory=fake.api_factory,
                            signer=lambda data: b"sig",
                            which_fn=lambda cmd: "/usr/local/bin/ocr",
                            ocr_executor=lambda ctx: {"summary": "should not execute", "findings": []},
                        )
                    )
                    return "polled"

                poll_res = poll_once(
                    app_id=3333,
                    allowlist=["acme/repo-a"],
                    lock_path=None,
                    evaluator=_poll_evaluator,
                    api_factory=fake.api_factory,
                    signer=lambda data: b"sig",
                )
                self.assertTrue(poll_res.evaluation_ran)
                self.assertEqual(len(nested_run_results), 1)
                self.assertTrue(nested_run_results[0].skipped_busy)
                self.assertEqual(nested_run_results[0].status, "skipped_busy")

                nested_poll_results = []

                def _ocr_exec(_ctx):
                    nested_poll_results.append(
                        poll_once(
                            app_id=3333,
                            allowlist=["acme/repo-a"],
                            lock_path=None,
                            evaluator=lambda _c: "should-not-run",
                            api_factory=fake.api_factory,
                            signer=lambda data: b"sig",
                        )
                    )
                    return {"summary": "Reviewed", "findings": []}

                run_res = run_ocr_review_pass(
                    app_id=3333,
                    allowlist=["acme/repo-a"],
                    state_path=Path(tmpdir) / "state.json",
                    lock_path=None,
                    env=_ready_env(),
                    api_factory=fake.api_factory,
                    signer=lambda data: b"sig",
                    which_fn=lambda cmd: "/usr/local/bin/ocr",
                    ocr_executor=_ocr_exec,
                )
                self.assertTrue(run_res.passed)
                self.assertEqual(run_res.reviews_posted, 1)
                self.assertEqual(len(nested_poll_results), 1)
                self.assertTrue(nested_poll_results[0].evaluation_skipped_busy)
                self.assertFalse(nested_poll_results[0].evaluation_ran)

    def test_concurrent_poll_once_and_run_preserve_shared_state_updates(self):
        from scripts.ocr_poller import poll_once
        from scripts.ocr_state import record_head_reviewed

        pr_a = _make_pr(
            number=10,
            head_sha="a" * 40,
            head_full_name="acme/repo-a",
            base_full_name="acme/repo-a",
        )
        fake = FakeReviewGitHubServer(
            repositories=("acme/repo-a", "acme/repo-b", "acme/repo-c"),
            pulls_by_repo={"acme/repo-a": [pr_a], "acme/repo-b": [], "acme/repo-c": []},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            state_path = tmp / "shared-state.json"
            lock_path = tmp / "ocr.lock"

            # While run_ocr_review_pass is mid-execution, a concurrent poll_once (evaluator=None)
            # advances rotation state on the shared state_path; neither update may clobber the other.
            def _ocr_exec_with_concurrent_poll(_ctx):
                poll_res = poll_once(
                    app_id=3333,
                    allowlist=["acme/repo-a", "acme/repo-b", "acme/repo-c"],
                    state_path=state_path,
                    cursor_index=1,
                    max_repos_per_cycle=1,
                    evaluator=None,
                    api_factory=fake.api_factory,
                    signer=lambda data: b"sig",
                )
                self.assertTrue(poll_res.passed)
                self.assertEqual(poll_res.next_cursor_index, 2)
                self.assertEqual(poll_res.next_cursor_repository, "acme/repo-c")
                return {"summary": "Reviewed", "findings": []}

            run_res = run_ocr_review_pass(
                app_id=3333,
                allowlist=["acme/repo-a", "acme/repo-b", "acme/repo-c"],
                state_path=state_path,
                lock_path=lock_path,
                max_reviews=1,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=_ocr_exec_with_concurrent_poll,
            )
            self.assertTrue(run_res.passed)
            self.assertEqual(run_res.reviews_posted, 1)

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["next_index"], 2)
            self.assertEqual(persisted["next_repository"], "acme/repo-c")
            self.assertIn("acme/repo-a#10", persisted["pull_requests"])

            # Stress concurrent threads updating cursor via poll_once and PR state via record_head_reviewed
            errors = []

            def _worker(idx: int) -> None:
                try:
                    if idx % 2 == 0:
                        poll_once(
                            app_id=3333,
                            allowlist=["acme/repo-a", "acme/repo-b", "acme/repo-c"],
                            state_path=state_path,
                            max_repos_per_cycle=1,
                            evaluator=None,
                            api_factory=fake.api_factory,
                            signer=lambda data: b"sig",
                        )
                    else:
                        from datetime import datetime, timezone

                        record_head_reviewed(
                            state={},
                            state_path=state_path,
                            repository="acme/repo-b",
                            pr_number=idx,
                            head_sha="b" * 40,
                            review_mode="pr_review",
                            comment_id=idx,
                            comment_url=f"https://example.invalid/{idx}",
                            clock=lambda: datetime(2026, 11, 14, 1, 0, 0, tzinfo=timezone.utc),
                            increment_attempts=True,
                        )
                except Exception as exc:  # pragma: no cover - collected for assertion
                    errors.append(exc)

            threads = [threading.Thread(target=_worker, args=(i,)) for i in range(1, 11)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)
            self.assertEqual(errors, [])

            final_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("acme/repo-a#10", final_state["pull_requests"])
            for pr_idx in (1, 3, 5, 7, 9):
                self.assertIn(f"acme/repo-b#{pr_idx}", final_state["pull_requests"])

    def test_malformed_non_dict_live_head_fails_closed_as_skipped_unsafe(self):
        for bad_head in ("bad-head", ["x"]):
            pr = _make_pr(number=55, head_sha="c" * 40)
            ocr_calls = []

            def _corrupt_head(repo, pr_num, bad_value=bad_head, pr_ref=pr):
                pr_ref["head"] = bad_value

            fake = FakeReviewGitHubServer(
                pulls_by_repo={"acme/repo-a": [pr]},
                on_get_pr=_corrupt_head,
            )
            with tempfile.TemporaryDirectory() as tmpdir:
                res = run_ocr_review_pass(
                    app_id=3333,
                    allowlist=["acme/repo-a"],
                    lock_path=Path(tmpdir) / "ocr.lock",
                    env=_ready_env(),
                    api_factory=fake.api_factory,
                    signer=lambda data: b"sig",
                    which_fn=lambda cmd: "/usr/local/bin/ocr",
                    ocr_executor=lambda ctx: (ocr_calls.append(ctx), {"summary": "bad", "findings": []})[1],
                )
            self.assertTrue(res.passed, res.errors)
            self.assertEqual(res.status, "completed")
            self.assertEqual(res.skipped_unsafe, 1)
            self.assertEqual(res.retryable_failures, 0)
            self.assertEqual(res.reviews_posted, 0)
            self.assertEqual(res.outcomes[0].outcome, "skipped_unsafe")
            self.assertIn("head", res.outcomes[0].detail.lower())
            self.assertEqual(ocr_calls, [])
            self.assertEqual(fake.posted_reviews, [])

    def test_multi_cycle_run_clamps_interval_and_reviews_new_commit_on_cycle_2(self):
        sha1 = "1" * 40
        sha2 = "2" * 40
        pr = _make_pr(number=71, head_sha=sha1)
        fake = FakeReviewGitHubServer(pulls_by_repo={"acme/repo-a": [pr]})
        sleeps = []

        def _sleep_and_push_commit(delay):
            sleeps.append(delay)
            if len(sleeps) == 1:
                pr["head"]["sha"] = sha2

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out = io.StringIO()
            rc = run_ocr_reviews(
                app_id=3333,
                allowlist=["acme/repo-a"],
                state_path=tmp / "state.json",
                lock_path=tmp / "ocr.lock",
                interval_seconds=1.0,
                max_cycles=3,
                sleep_fn=_sleep_and_push_commit,
                env=_ready_env(),
                api_factory=fake.api_factory,
                signer=lambda data: b"sig",
                which_fn=lambda cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: {"summary": f"Reviewed {ctx['head_sha'][:8]}", "findings": []},
                json_output=True,
                out_stream=out,
            )

        self.assertEqual(rc, 0)
        self.assertEqual(sleeps, [5.0, 5.0])
        self.assertEqual(len(fake.posted_reviews), 2)
        self.assertEqual(fake.posted_reviews[0]["payload"]["commit_id"], sha1)
        self.assertEqual(fake.posted_reviews[1]["payload"]["commit_id"], sha2)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["reviews_posted"], 2)
        self.assertEqual(len(payload["cycles"]), 3)
        self.assertEqual(payload["cycles"][0]["reviews_posted"], 1)
        self.assertEqual(payload["cycles"][1]["reviews_posted"], 1)
        self.assertEqual(payload["cycles"][2]["reviews_posted"], 0)
        self.assertEqual(payload["cycles"][2]["skipped_already_reviewed"], 1)


class CliSubprocessEndToEndTests(unittest.TestCase):
    def test_gh_setup_ocr_run_end_to_end_with_fake_ocr_cli_openssl_key_and_loopback_http(self):
        sha1 = "f" * 40
        sha2 = "e" * 40
        pr = _make_pr(number=105, head_sha=sha1)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            priv_key = tmp / "ocr-app.pem"
            subprocess.run(
                ["openssl", "genrsa", "-out", str(priv_key), "2048"],
                check=True,
                capture_output=True,
            )
            os.chmod(priv_key, 0o600)

            state_path = tmp / "ocr-state.json"
            lock_path = tmp / "ocr.lock"
            ocr_calls_log = tmp / "ocr-calls.log"
            fake_ocr_bin = tmp / "fake-ocr"
            fake_ocr_bin.write_text(
                f"""#!/usr/bin/env python3
import json, os, sys
ctx_file = os.environ["OCR_PR_CONTEXT_FILE"]
with open(ctx_file, "r", encoding="utf-8") as fh:
    ctx = json.load(fh)
assert "GITHUB_TOKEN" not in os.environ
assert "GH_TOKEN" not in os.environ
with open({str(ocr_calls_log)!r}, "a", encoding="utf-8") as log:
    log.write(ctx["head_sha"] + "\\n")
print(json.dumps({{
    "summary": f"Reviewed {{ctx['repository']}}#{{ctx['pr_number']}} at {{ctx['head_sha'][:8]}}",
    "findings": [{{"file": "app.py", "line": 7, "severity": "low", "message": "Use explicit constant."}}],
}}))
""",
                encoding="utf-8",
            )
            os.chmod(fake_ocr_bin, 0o700)

            posted_reviews = []
            current_pr = dict(pr)

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
                    path_only, _, _ = self.path.partition("?")
                    if path_only == "/app":
                        self._send_json(
                            200,
                            {
                                "id": 7777,
                                "slug": "ocr-limited-app",
                                "name": "OCR Limited App",
                                "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                            },
                        )
                    elif path_only == "/app/installations":
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
                    elif path_only == "/installation/repositories":
                        self._send_json(
                            200,
                            {
                                "total_count": 1,
                                "repositories": [{"id": 101, "name": "repo-a", "full_name": "acme/repo-a"}],
                            },
                        )
                    elif path_only == "/repos/acme/repo-a":
                        self._send_json(200, {"full_name": "acme/repo-a", "default_branch": "main"})
                    elif path_only.startswith("/repos/acme/repo-a/contents/"):
                        self._send_json(404, {"message": "Not Found"})
                    elif path_only == "/repos/acme/repo-a/pulls":
                        self._send_json(200, [current_pr])
                    elif path_only == "/repos/acme/repo-a/pulls/105":
                        self._send_json(200, current_pr)
                    elif path_only == "/repos/acme/repo-a/pulls/105/files":
                        self._send_json(200, [{"filename": "app.py", "status": "modified", "additions": 2, "deletions": 0}])
                    elif path_only == "/repos/acme/repo-a/pulls/105/reviews":
                        self._send_json(
                            200,
                            [
                                {
                                    "id": 5000 + idx + 1,
                                    "body": rev.get("body", ""),
                                    "commit_id": rev.get("commit_id"),
                                    "html_url": f"https://github.com/acme/repo-a/pull/105#pullrequestreview-{5000 + idx + 1}",
                                }
                                for idx, rev in enumerate(posted_reviews)
                            ],
                        )
                    elif path_only == "/repos/acme/repo-a/issues/105/comments":
                        self._send_json(200, [])
                    else:
                        self._send_json(404, {"message": "Not Found"})

                def do_POST(self):
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length) if length > 0 else b"{}"
                    body = json.loads(raw.decode("utf-8"))
                    if self.path.endswith("/access_tokens"):
                        self._send_json(
                            201,
                            {
                                "token": "ghs_loopback_review_secret_789",
                                "expires_at": "2026-11-14T05:00:00Z",
                                "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                            },
                        )
                    elif self.path == "/repos/acme/repo-a/pulls/105/reviews":
                        posted_reviews.append(body)
                        self._send_json(
                            200,
                            {
                                "id": 5000 + len(posted_reviews),
                                "html_url": (
                                    "https://github.com/acme/repo-a/pull/105"
                                    f"#pullrequestreview-{5000 + len(posted_reviews)}"
                                ),
                            },
                        )
                    else:
                        self._send_json(404, {"message": "Not Found"})

            server = HTTPServer(("127.0.0.1", 0), _Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                clean_env = {
                    k: v
                    for k, v in os.environ.items()
                    if k not in ("RUNNER_ENVIRONMENT", "GITHUB_TOKEN", "GH_TOKEN")
                }
                clean_env.update(_ready_env())

                cmd = [
                    str(ROOT_DIR / "sciflow-cicd"),
                    "ocr",
                    "run",
                    "--app-id",
                    "7777",
                    "--private-key-path",
                    str(priv_key),
                    "--allowlist",
                    "acme/repo-a",
                    "--api-base-url",
                    base_url,
                    "--state-path",
                    str(state_path),
                    "--lock-path",
                    str(lock_path),
                    "--ocr-bin",
                    str(fake_ocr_bin),
                    "--json",
                ]

                proc1 = subprocess.run(cmd, env=clean_env, capture_output=True, text=True)
                self.assertEqual(proc1.returncode, 0, proc1.stderr + proc1.stdout)
                out1 = json.loads(proc1.stdout)
                self.assertEqual(out1["reviews_posted"], 1)
                self.assertEqual(len(posted_reviews), 1)
                self.assertEqual(posted_reviews[0]["event"], "COMMENT")
                self.assertEqual(posted_reviews[0]["commit_id"], sha1)
                self.assertNotIn("ghs_loopback_review_secret_789", proc1.stdout + proc1.stderr)
                self.assertNotIn("sk-test-secret-token-value", proc1.stdout + proc1.stderr)

                # Restart / second invocation on unchanged HEAD SHA skips silently
                proc2 = subprocess.run(cmd, env=clean_env, capture_output=True, text=True)
                self.assertEqual(proc2.returncode, 0, proc2.stderr + proc2.stdout)
                out2 = json.loads(proc2.stdout)
                self.assertEqual(out2["reviews_posted"], 0)
                self.assertEqual(out2["skipped_already_reviewed"], 1)
                self.assertEqual(len(posted_reviews), 1)

                # Push new commit sha2 -> third invocation reviews sha2
                current_pr = _make_pr(number=105, head_sha=sha2)
                proc3 = subprocess.run(cmd, env=clean_env, capture_output=True, text=True)
                self.assertEqual(proc3.returncode, 0, proc3.stderr + proc3.stdout)
                out3 = json.loads(proc3.stdout)
                self.assertEqual(out3["reviews_posted"], 1)
                self.assertEqual(len(posted_reviews), 2)
                self.assertEqual(posted_reviews[1]["commit_id"], sha2)
                self.assertEqual(ocr_calls_log.read_text(encoding="utf-8").splitlines(), [sha1, sha2])
            finally:
                server.shutdown()
                server.server_close()


class ModularBoundaryAndConfigTests(unittest.TestCase):
    def test_ocr_runner_and_publisher_modules_expose_focused_boundaries(self):
        from scripts import ocr_reviewer
        from scripts.ocr_publisher import (
            advisory_review_marker as pub_marker,
            format_advisory_review_body as pub_format,
            validate_advisory_publish_request as pub_validate,
        )
        from scripts.ocr_runner import (
            build_sanitized_context,
            extract_head_sha,
            validate_and_normalize_ocr_output,
        )

        sha = "d" * 40
        pr = _make_pr(number=88, head_sha=sha)
        self.assertEqual(extract_head_sha(pr), sha)
        ctx = build_sanitized_context(pr, "acme/repo-a", [{"filename": "a.py", "status": "modified"}])
        self.assertEqual(ctx["pr_number"], 88)
        normalized = validate_and_normalize_ocr_output(
            {"summary": "All good", "findings": ["Clean"]},
            secrets_to_redact=(),
        )
        self.assertEqual(normalized["summary"], "All good")

        body = pub_format(
            repository="acme/repo-a",
            pr_number=88,
            head_sha=sha,
            ocr_result=normalized,
        )
        self.assertIn(pub_marker("acme/repo-a", 88, sha), body)
        pub_validate(
            "POST",
            "/repos/acme/repo-a/pulls/88/reviews",
            {"commit_id": sha, "event": "COMMENT", "body": body},
            expected_head_sha=sha,
        )
        self.assertFalse(hasattr(ocr_reviewer, "_client_request"))

    def test_run_ocr_review_pass_accepts_immutable_config_dataclasses(self):
        from dataclasses import FrozenInstanceError
        from scripts.ocr_app_auth import OcrAppConfig
        from scripts.ocr_poller import PrEligibilityConfig
        from scripts.ocr_reviewer import OcrReviewConfig

        sha = "e" * 40
        fake = FakeReviewGitHubServer(pulls_by_repo={"acme/repo-a": [_make_pr(number=91, head_sha=sha)]})
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rev_cfg = OcrReviewConfig(
                state_path=tmp / "state.json",
                lock_path=tmp / "ocr.lock",
                review_mode="pr_review",
            )
            with self.assertRaises(FrozenInstanceError):
                rev_cfg.review_mode = "issue_comment"  # type: ignore[misc]

            app_cfg = OcrAppConfig(
                app_id=3333,
                allowlist=("acme/repo-a",),
                api_factory=fake.api_factory,
                signer=lambda _d: b"sig",
            )
            elig_cfg = PrEligibilityConfig(eligibility_mode="either")
            res = run_ocr_review_pass(
                app_config=app_cfg,
                eligibility_config=elig_cfg,
                review_config=rev_cfg,
                env=_ready_env(),
                which_fn=lambda _cmd: "/usr/local/bin/ocr",
                ocr_executor=lambda ctx: {"summary": "OK", "findings": []},
            )
        self.assertTrue(res.passed, res.errors)
        self.assertEqual(res.reviews_posted, 1)


if __name__ == "__main__":
    unittest.main()
