"""Tests for allowlisted OCR pull request polling, fairness, backoff, and single-flight guard."""

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.ocr_poller import (
    ActiveEvaluationLock,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_BACKOFF_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
    OcrPollerError,
    PullRequestCandidate,
    SingleFlightConflictError,
    format_poll_report,
    poll_once,
    poll_repositories,
    run_ocr_poll,
)


class FakePollingGitHubServer:
    """In-memory fake GitHub App & PR API with controllable headers, errors, and pages."""

    def __init__(
        self,
        *,
        app_info,
        installations,
        repos_by_installation,
        pulls_by_repo=None,
        pull_details_by_repo=None,
        repo_handlers=None,
    ):
        self.app_info = app_info
        self.installations = installations
        self.repos_by_installation = repos_by_installation
        self.pulls_by_repo = pulls_by_repo or {}
        self.pull_details_by_repo = pull_details_by_repo or {}
        self.repo_handlers = repo_handlers or {}
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
                    parts = path.split("?", 1)[0].split("/")
                    # /repos/{owner}/{repo}/pulls or /repos/{owner}/{repo}/pulls/{num}
                    if len(parts) >= 5 and parts[4] == "pulls":
                        repo_full = f"{parts[2]}/{parts[3]}"
                        _, scoped_repos = server.token_to_meta.get(bearer_token, (None, []))
                        if scoped_repos and parts[3] not in scoped_repos and repo_full not in scoped_repos:
                            return 403, {}, {"message": "Resource not accessible by integration"}
                        handler = server.repo_handlers.get(repo_full)
                        if handler is not None and len(parts) == 5:
                            return handler(path, bearer_token)
                        if len(parts) == 6:
                            pr_num = int(parts[5])
                            detail = server.pull_details_by_repo.get(repo_full, {}).get(pr_num)
                            if detail is None:
                                return 404, {}, {"message": "Not Found"}
                            return 200, {}, detail
                        items = server.pulls_by_repo.get(repo_full, [])
                        if "page=1" in path or "?" not in path:
                            return 200, {}, items
                        return 200, {}, []
                return 404, {}, {"message": "Not Found"}

            def request(self, method, path, payload=None):
                status, _headers, body = self.request_with_headers(method, path, payload=payload)
                return status, body

        return _Client()


def _make_pr(
    repo,
    number,
    *,
    state="open",
    head_repo=None,
    head_sha=None,
    labels=(),
    author="contributor",
    author_association="CONTRIBUTOR",
    draft=False,
    fork=False,
):
    return {
        "number": number,
        "state": state,
        "title": f"PR #{number} in {repo}",
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "draft": draft,
        "user": {"login": author},
        "author_association": author_association,
        "labels": [{"name": lbl} for lbl in labels],
        "head": {
            "sha": head_sha or f"sha{number:037d}",
            "ref": f"feature-{number}",
            "repo": {"full_name": head_repo or repo, "fork": fork},
        },
        "base": {
            "ref": "main",
            "repo": {"full_name": repo, "fork": False},
        },
    }


class AllowlistAndPrFilteringTests(unittest.TestCase):
    def test_polls_only_allowlist_and_installation_intersection_and_filters_issues_and_forks(self):
        server = FakePollingGitHubServer(
            app_info={
                "id": 3333,
                "slug": "ocr-limited-app",
                "name": "OCR Limited App",
                "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
            },
            installations=[
                {
                    "id": 701,
                    "account": {"login": "acme", "type": "Organization"},
                    "repository_selection": "selected",
                    "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                },
                {
                    "id": 702,
                    "account": {"login": "beta", "type": "Organization"},
                    "repository_selection": "selected",
                    "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                },
            ],
            repos_by_installation={
                701: [
                    {"id": 1, "name": "eligible-repo", "full_name": "acme/eligible-repo"},
                    {"id": 2, "name": "unlisted-repo", "full_name": "acme/unlisted-repo"},
                ],
                702: [
                    {"id": 3, "name": "other-installed-repo", "full_name": "beta/other-installed-repo"},
                ],
            },
            pulls_by_repo={
                "acme/eligible-repo": [
                    # Issue object accidentally/mixed in response (no head/base, issue URL)
                    {
                        "number": 10,
                        "state": "open",
                        "title": "Just an issue",
                        "html_url": "https://github.com/acme/eligible-repo/issues/10",
                        "labels": [{"name": "eligible for review"}],
                    },
                    # Closed PR
                    _make_pr("acme/eligible-repo", 11, state="closed", labels=("eligible for review",)),
                    # Fork PR
                    _make_pr(
                        "acme/eligible-repo",
                        12,
                        head_repo="outsider/eligible-repo",
                        fork=True,
                        labels=("eligible for review",),
                    ),
                    # Same-repo PR without label and non-maintainer author
                    _make_pr("acme/eligible-repo", 13, author="external-dev", author_association="CONTRIBUTOR"),
                    # Eligible same-repo PR with label
                    _make_pr("acme/eligible-repo", 14, labels=("eligible for review",)),
                ],
                "acme/unlisted-repo": [
                    _make_pr("acme/unlisted-repo", 99, labels=("eligible for review",)),
                ],
                "beta/other-installed-repo": [
                    _make_pr("beta/other-installed-repo", 88, labels=("eligible for review",)),
                ],
            },
        )

        result = poll_once(
            app_id=3333,
            allowlist=["acme/eligible-repo", "acme/not-installed-repo"],
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.eligible_repositories, ["acme/eligible-repo"])
        self.assertEqual(result.polled_repositories, ["acme/eligible-repo"])
        self.assertEqual([c.number for c in result.candidates], [14])
        self.assertEqual(result.candidates[0].repository, "acme/eligible-repo")
        self.assertEqual(result.repo_records[0].issues_filtered_out, 1)

        called_paths = [path for _, _, path, _ in server.calls]
        self.assertFalse(any("unlisted-repo" in p for p in called_paths))
        self.assertFalse(any("other-installed-repo" in p for p in called_paths))
        self.assertFalse(any("not-installed-repo" in p for p in called_paths))

        serialized = json.dumps(result.to_dict())
        self.assertNotIn("ghs_", serialized)
        self.assertNotIn("eyJ", serialized)


class MultiRepositoryFairnessTests(unittest.TestCase):
    def _make_three_repo_server(self):
        return FakePollingGitHubServer(
            app_info={
                "id": 3333,
                "slug": "ocr-app",
                "name": "OCR App",
                "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
            },
            installations=[
                {
                    "id": 701,
                    "account": {"login": "acme", "type": "Organization"},
                    "repository_selection": "selected",
                    "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                }
            ],
            repos_by_installation={
                701: [
                    {"id": 1, "name": "repo-a", "full_name": "acme/repo-a"},
                    {"id": 2, "name": "repo-b", "full_name": "acme/repo-b"},
                    {"id": 3, "name": "repo-c", "full_name": "acme/repo-c"},
                ]
            },
            pulls_by_repo={
                "acme/repo-a": [_make_pr("acme/repo-a", 101, labels=("eligible for review",))],
                "acme/repo-b": [_make_pr("acme/repo-b", 201, labels=("eligible for review",))],
                "acme/repo-c": [_make_pr("acme/repo-c", 301, labels=("eligible for review",))],
            },
        )

    def test_rotates_fairly_across_repositories_with_persisted_cursor_and_no_starvation(self):
        server = self._make_three_repo_server()
        sleep_calls = []
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "poll-state.json"

            # All 3 repos always have eligible PRs; selected_candidate must still rotate A -> B -> C -> A
            cycles = poll_repositories(
                app_id=3333,
                allowlist=["acme/repo-a", "acme/repo-b", "acme/repo-c"],
                api_factory=server.api_factory,
                signer=lambda data: b"fake-sig",
                state_path=state_path,
                max_cycles=4,
                interval_seconds=15,
                sleep_fn=sleep_calls.append,
            )

            self.assertEqual(len(cycles), 4)
            self.assertEqual(
                [c.selected_candidate.repository for c in cycles],
                ["acme/repo-a", "acme/repo-b", "acme/repo-c", "acme/repo-a"],
            )
            self.assertEqual(
                [c.selected_candidate.number for c in cycles],
                [101, 201, 301, 101],
            )
            # Sleeps occur between the 4 cycles (3 sleeps of 15s)
            self.assertEqual(sleep_calls, [15.0, 15.0, 15.0])

            # Next single poll_once resumes from persisted state at repo-b (not repo-a)
            resumed = poll_once(
                app_id=3333,
                allowlist=["acme/repo-a", "acme/repo-b", "acme/repo-c"],
                api_factory=server.api_factory,
                signer=lambda data: b"fake-sig",
                state_path=state_path,
                max_repos_per_cycle=1,
            )
            self.assertEqual(resumed.polled_repositories, ["acme/repo-b"])
            self.assertEqual(resumed.selected_candidate.repository, "acme/repo-b")
            self.assertEqual(resumed.next_cursor_repository, "acme/repo-c")

    def test_state_path_rejects_symlinks(self):
        server = self._make_three_repo_server()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            real_state = tmp / "real.json"
            real_state.write_text("{}", encoding="utf-8")
            link_state = tmp / "link.json"
            os.symlink(real_state, link_state)

            with self.assertRaises(OcrPollerError) as ctx:
                poll_once(
                    app_id=3333,
                    allowlist=["acme/repo-a", "acme/repo-b", "acme/repo-c"],
                    api_factory=server.api_factory,
                    signer=lambda data: b"fake-sig",
                    state_path=link_state,
                )
            self.assertIn("symlink", str(ctx.exception).lower())

    def test_poll_preserves_pull_request_dedup_records_on_shared_state_path(self):
        server = self._make_three_repo_server()
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "shared-ocr-state.json"
            initial_state = {
                "version": 1,
                "next_index": 1,
                "next_repository": "acme/repo-b",
                "pull_requests": {
                    "acme/repo-a#101": {
                        "repository": "acme/repo-a",
                        "pr_number": 101,
                        "status": "reviewed",
                        "last_reviewed_head_sha": "a" * 40,
                        "heads": {"a" * 40: {"status": "reviewed"}},
                    }
                },
            }
            state_path.write_text(json.dumps(initial_state, indent=2) + "\n", encoding="utf-8")

            res = poll_once(
                app_id=3333,
                allowlist=["acme/repo-a", "acme/repo-b", "acme/repo-c"],
                api_factory=server.api_factory,
                signer=lambda data: b"fake-sig",
                state_path=state_path,
                max_repos_per_cycle=1,
            )

            self.assertEqual(res.polled_repositories, ["acme/repo-b"])
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["next_index"], 2)
            self.assertEqual(persisted["next_repository"], "acme/repo-c")
            self.assertEqual(persisted["version"], 1)
            self.assertEqual(persisted["pull_requests"], initial_state["pull_requests"])


class RateLimitAndBackoffTests(unittest.TestCase):
    def _base_server_kwargs(self):
        return dict(
            app_info={
                "id": 3333,
                "slug": "ocr-app",
                "name": "OCR App",
                "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
            },
            installations=[
                {
                    "id": 701,
                    "account": {"login": "acme", "type": "Organization"},
                    "repository_selection": "selected",
                    "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                }
            ],
            repos_by_installation={
                701: [
                    {"id": 1, "name": "repo-a", "full_name": "acme/repo-a"},
                    {"id": 2, "name": "repo-b", "full_name": "acme/repo-b"},
                ]
            },
        )

    def test_polling_interval_is_clamped_and_rejects_negative(self):
        server = FakePollingGitHubServer(
            **self._base_server_kwargs(),
            pulls_by_repo={
                "acme/repo-a": [_make_pr("acme/repo-a", 1, labels=("eligible for review",))],
                "acme/repo-b": [],
            },
        )
        sleeps_low = []
        poll_repositories(
            app_id=3333,
            allowlist=["acme/repo-a"],
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
            max_cycles=2,
            interval_seconds=0.05,
            sleep_fn=sleeps_low.append,
        )
        self.assertEqual(sleeps_low, [MIN_POLL_INTERVAL_SECONDS])

        sleeps_high = []
        poll_repositories(
            app_id=3333,
            allowlist=["acme/repo-a"],
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
            max_cycles=2,
            interval_seconds=999_999,
            sleep_fn=sleeps_high.append,
        )
        self.assertEqual(sleeps_high, [MAX_POLL_INTERVAL_SECONDS])

        with self.assertRaises(OcrPollerError):
            poll_repositories(
                app_id=3333,
                allowlist=["acme/repo-a"],
                api_factory=server.api_factory,
                signer=lambda data: b"fake-sig",
                max_cycles=2,
                interval_seconds=-5,
                sleep_fn=lambda _: None,
            )

    def test_respects_retry_after_and_x_ratelimit_reset_headers(self):
        fixed_now = datetime(2026, 11, 14, 1, 0, 0, tzinfo=timezone.utc)
        reset_epoch = int(fixed_now.timestamp()) + 25
        attempts = {"repo-a": 0}

        def repo_a_handler(path, token):
            attempts["repo-a"] += 1
            if attempts["repo-a"] == 1:
                return 429, {"Retry-After": "12"}, {"message": "Too many requests"}
            if attempts["repo-a"] == 2:
                return 403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset_epoch)}, {
                    "message": "API rate limit exceeded"
                }
            return 200, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(fixed_now.timestamp()) + 10)}, [
                _make_pr("acme/repo-a", 15, labels=("eligible for review",))
            ]

        server = FakePollingGitHubServer(
            **self._base_server_kwargs(),
            pulls_by_repo={"acme/repo-b": []},
            repo_handlers={"acme/repo-a": repo_a_handler},
        )
        sleeps = []
        result = poll_once(
            app_id=3333,
            allowlist=["acme/repo-a", "acme/repo-b"],
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
            sleep_fn=sleeps.append,
            now_fn=lambda: fixed_now,
            max_retries=3,
            max_backoff_seconds=120.0,
        )

        self.assertTrue(result.passed)
        self.assertEqual([c.number for c in result.candidates], [15])
        self.assertEqual(attempts["repo-a"], 3)
        # 1st sleep from Retry-After: 12; 2nd sleep from X-RateLimit-Reset (25s + 1s buffer = 26s);
        # 3rd sleep from 200 response exhausted X-RateLimit-Remaining=0 (10s + 1s buffer = 11s) before polling repo-b
        self.assertEqual(sleeps, [12.0, 26.0, 11.0])

    def test_transient_5xx_and_network_errors_back_off_exponentially_without_crashing_other_repos(self):
        attempts = {"repo-a": 0, "repo-b": 0}

        def repo_a_handler(path, token):
            attempts["repo-a"] += 1
            if attempts["repo-a"] == 1:
                raise urllib.error.URLError("temporary dns failure")
            if attempts["repo-a"] == 2:
                return 503, {}, {"message": "Service Unavailable"}
            return 200, {}, [_make_pr("acme/repo-a", 21, labels=("eligible for review",))]

        def repo_b_handler(path, token):
            attempts["repo-b"] += 1
            return 502, {"Retry-After": "0"}, {"message": "Bad Gateway"}

        server = FakePollingGitHubServer(
            **self._base_server_kwargs(),
            repo_handlers={"acme/repo-a": repo_a_handler, "acme/repo-b": repo_b_handler},
        )
        sleeps = []
        result = poll_once(
            app_id=3333,
            allowlist=["acme/repo-a", "acme/repo-b"],
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
            sleep_fn=sleeps.append,
            max_retries=2,
            base_backoff_seconds=2.0,
            max_backoff_seconds=60.0,
        )

        self.assertTrue(result.passed)
        self.assertEqual([c.number for c in result.candidates], [21])
        self.assertEqual(attempts["repo-a"], 3)
        self.assertEqual(attempts["repo-b"], 3)
        # repo-a: 2.0 (attempt 0), 4.0 (attempt 1); repo-b: Retry-After: 0 floored to MIN_BACKOFF_SECONDS (1.0) twice
        self.assertEqual(sleeps, [2.0, 4.0, MIN_BACKOFF_SECONDS, MIN_BACKOFF_SECONDS])
        rec_b = [r for r in result.repo_records if r.repository == "acme/repo-b"][0]
        self.assertTrue(any("502" in err for err in rec_b.errors))


class EligibilityModesAndHydrationTests(unittest.TestCase):
    def test_eligibility_modes_draft_guard_and_issue_vs_pr_hydration(self):
        server = FakePollingGitHubServer(
            app_info={
                "id": 3333,
                "slug": "ocr-app",
                "name": "OCR App",
                "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
            },
            installations=[
                {
                    "id": 701,
                    "account": {"login": "acme", "type": "Organization"},
                    "repository_selection": "selected",
                    "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                }
            ],
            repos_by_installation={
                701: [{"id": 1, "name": "repo-a", "full_name": "acme/repo-a"}]
            },
            pulls_by_repo={
                "acme/repo-a": [
                    # Pure issue with pull_request: None
                    {
                        "number": 1,
                        "state": "open",
                        "title": "Issue with null pull_request",
                        "pull_request": None,
                        "labels": [{"name": "eligible for review"}],
                    },
                    # /issues-style PR item requiring hydration via /pulls/2
                    {
                        "number": 2,
                        "state": "open",
                        "title": "Hydrated PR",
                        "pull_request": {"url": "https://api.github.com/repos/acme/repo-a/pulls/2"},
                    },
                    # Maintainer PR without label
                    _make_pr("acme/repo-a", 3, author="bob", author_association="OWNER"),
                    # Draft labeled PR
                    _make_pr("acme/repo-a", 4, draft=True, labels=("eligible for review",)),
                    # Custom-maintainer PR with CONTRIBUTOR association
                    _make_pr("acme/repo-a", 5, author="alice", author_association="CONTRIBUTOR"),
                ]
            },
            pull_details_by_repo={
                "acme/repo-a": {
                    2: _make_pr("acme/repo-a", 2, author="carol", labels=("Eligible for Review",)),
                }
            },
        )

        # Mode: label -> only #2
        res_label = poll_once(
            app_id=3333,
            allowlist=["acme/repo-a"],
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
            eligibility_mode="label",
        )
        self.assertEqual([c.number for c in res_label.candidates], [2])
        self.assertEqual(res_label.repo_records[0].issues_filtered_out, 1)

        # Mode: maintainer (via author_association) -> only #3
        res_maint = poll_once(
            app_id=3333,
            allowlist=["acme/repo-a"],
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
            eligibility_mode="maintainer",
        )
        self.assertEqual([c.number for c in res_maint.candidates], [3])

        # Mode: maintainer with explicit maintainers=["alice"] -> only #5
        res_explicit = poll_once(
            app_id=3333,
            allowlist=["acme/repo-a"],
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
            eligibility_mode="maintainer",
            maintainers=["alice"],
        )
        self.assertEqual([c.number for c in res_explicit.candidates], [5])

        # Mode: either with include_drafts=True -> #2, #3, #4
        res_drafts = poll_once(
            app_id=3333,
            allowlist=["acme/repo-a"],
            api_factory=server.api_factory,
            signer=lambda data: b"fake-sig",
            eligibility_mode="either",
            include_drafts=True,
        )
        self.assertEqual([c.number for c in res_drafts.candidates], [2, 3, 4])


class SingleFlightLockTests(unittest.TestCase):
    def test_single_flight_lock_blocks_second_concurrent_evaluation_and_releases_on_error(self):
        server = FakePollingGitHubServer(
            app_info={
                "id": 3333,
                "slug": "ocr-app",
                "name": "OCR App",
                "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
            },
            installations=[
                {
                    "id": 701,
                    "account": {"login": "acme", "type": "Organization"},
                    "repository_selection": "selected",
                    "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                }
            ],
            repos_by_installation={
                701: [
                    {"id": 1, "name": "repo-a", "full_name": "acme/repo-a"},
                    {"id": 2, "name": "repo-b", "full_name": "acme/repo-b"},
                ]
            },
            pulls_by_repo={
                "acme/repo-a": [_make_pr("acme/repo-a", 11, labels=("eligible for review",))],
                "acme/repo-b": [_make_pr("acme/repo-b", 22, labels=("eligible for review",))],
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "ocr-eval.lock"
            entered = threading.Event()
            release = threading.Event()
            evaluated = []

            def slow_evaluator(candidate: PullRequestCandidate):
                evaluated.append((candidate.repository, candidate.number))
                lock_meta = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(lock_meta["repository"], candidate.repository)
                self.assertEqual(lock_meta["pr_number"], candidate.number)
                entered.set()
                release.wait(timeout=5.0)
                return {"status": "reviewed"}

            first_results = []
            t1 = threading.Thread(
                target=lambda: first_results.append(
                    poll_once(
                        app_id=3333,
                        allowlist=["acme/repo-a", "acme/repo-b"],
                        api_factory=server.api_factory,
                        signer=lambda data: b"fake-sig",
                        lock_path=lock_path,
                        evaluator=slow_evaluator,
                    )
                )
            )
            t1.start()
            self.assertTrue(entered.wait(timeout=3.0), "First evaluator never acquired lock")

            # While first evaluation holds lock, a second poll_once for repo-b must skip evaluation
            second_res = poll_once(
                app_id=3333,
                allowlist=["acme/repo-b"],
                api_factory=server.api_factory,
                signer=lambda data: b"fake-sig",
                lock_path=lock_path,
                evaluator=lambda cand: evaluated.append((cand.repository, cand.number)),
            )
            self.assertTrue(second_res.passed)
            self.assertFalse(second_res.evaluation_ran)
            self.assertTrue(second_res.evaluation_skipped_busy)
            self.assertEqual(len(evaluated), 1)

            release.set()
            t1.join(timeout=3.0)
            self.assertEqual(len(first_results), 1)
            self.assertTrue(first_results[0].evaluation_ran)
            self.assertFalse(first_results[0].evaluation_skipped_busy)
            self.assertEqual(first_results[0].evaluation_result, {"status": "reviewed"})

            # After failure inside evaluator, lock must still be released cleanly
            def failing_evaluator(cand):
                raise RuntimeError("boom")

            with self.assertRaises(RuntimeError):
                poll_once(
                    app_id=3333,
                    allowlist=["acme/repo-b"],
                    api_factory=server.api_factory,
                    signer=lambda data: b"fake-sig",
                    lock_path=lock_path,
                    evaluator=failing_evaluator,
                )

            # Subsequent acquisition succeeds
            with ActiveEvaluationLock(lock_path, repository="acme/repo-b", pr_number=22) as lk:
                self.assertIsNotNone(lk)
                with self.assertRaises(SingleFlightConflictError):
                    ActiveEvaluationLock(lock_path).acquire()

            # Symlink lock path is rejected
            symlink_lock = Path(tmpdir) / "symlink.lock"
            os.symlink(lock_path, symlink_lock)
            with self.assertRaises(OcrPollerError) as ctx:
                ActiveEvaluationLock(symlink_lock).acquire()
            self.assertIn("symlink", str(ctx.exception).lower())


class CliAndLoopbackEndToEndTests(unittest.TestCase):
    def test_sciflow_cicd_ocr_poll_end_to_end_with_openssl_key_and_loopback_http_server(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            priv_key = tmp / "ocr-app.pem"
            subprocess.run(
                ["openssl", "genrsa", "-out", str(priv_key), "2048"],
                check=True,
                capture_output=True,
            )
            os.chmod(priv_key, 0o600)

            state_path = tmp / "poll-state.json"
            lock_path = tmp / "eval.lock"
            seen_paths = []
            repo_a_hits = [0]

            class _Handler(BaseHTTPRequestHandler):
                def log_message(self, format, *args):
                    return

                def _send_json(self, status, body, extra_headers=None):
                    raw = json.dumps(body).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    for k, v in (extra_headers or {}).items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(raw)

                def do_GET(self):
                    seen_paths.append(self.path)
                    if self.path == "/app":
                        self._send_json(
                            200,
                            {
                                "id": 7777,
                                "slug": "ocr-limited-app",
                                "name": "OCR Limited App",
                                "permissions": {
                                    "contents": "read",
                                    "pull_requests": "write",
                                    "metadata": "read",
                                },
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
                                    "permissions": {
                                        "contents": "read",
                                        "pull_requests": "write",
                                        "metadata": "read",
                                    },
                                }
                            ],
                        )
                    elif self.path.startswith("/installation/repositories"):
                        self._send_json(
                            200,
                            {
                                "total_count": 3,
                                "repositories": [
                                    {"id": 1, "name": "repo-a", "full_name": "acme/repo-a"},
                                    {"id": 2, "name": "repo-b", "full_name": "acme/repo-b"},
                                    {"id": 3, "name": "unlisted", "full_name": "acme/unlisted"},
                                ],
                            },
                        )
                    elif self.path.startswith("/repos/acme/repo-a/pulls"):
                        repo_a_hits[0] += 1
                        if repo_a_hits[0] == 1:
                            self._send_json(
                                429,
                                {"message": "Rate limited"},
                                extra_headers={"Retry-After": "1"},
                            )
                        else:
                            self._send_json(
                                200,
                                [
                                    {
                                        "number": 5,
                                        "state": "open",
                                        "title": "Issue item",
                                        "html_url": "https://github.com/acme/repo-a/issues/5",
                                    },
                                    _make_pr("acme/repo-a", 10, labels=("eligible for review",)),
                                ],
                            )
                    elif self.path.startswith("/repos/acme/repo-b/pulls"):
                        self._send_json(
                            200,
                            [_make_pr("acme/repo-b", 20, author="maintainer", author_association="OWNER")],
                        )
                    else:
                        self._send_json(404, {"message": "Not Found"})

                def do_POST(self):
                    self._send_json(
                        201,
                        {
                            "token": "ghs_loopback_secret_token_123",
                            "expires_at": "2026-11-14T05:00:00Z",
                            "permissions": {
                                "contents": "read",
                                "pull_requests": "write",
                                "metadata": "read",
                            },
                        },
                    )

            server = HTTPServer(("127.0.0.1", 0), _Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                proc = subprocess.run(
                    [
                        str(ROOT_DIR / "sciflow-cicd"),
                        "ocr",
                        "poll",
                        "--app-id",
                        "7777",
                        "--private-key-path",
                        str(priv_key),
                        "--allowlist",
                        "acme/repo-a,acme/repo-b,acme/missing",
                        "--api-base-url",
                        base_url,
                        "--state-path",
                        str(state_path),
                        "--lock-path",
                        str(lock_path),
                        "--json",
                    ],
                    capture_output=True,
                    text=True,
                )
            finally:
                server.shutdown()
                server.server_close()

            self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}")
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["passed"])
            self.assertEqual(len(payload["cycles"]), 1)
            cycle = payload["cycles"][0]
            self.assertEqual(cycle["eligible_repositories"], ["acme/repo-a", "acme/repo-b"])
            self.assertEqual([c["number"] for c in cycle["candidates"]], [10, 20])
            self.assertEqual(cycle["next_cursor_repository"], "acme/repo-b")
            self.assertEqual(repo_a_hits[0], 2)
            self.assertFalse(any("unlisted" in p for p in seen_paths))
            self.assertFalse(any("missing" in p for p in seen_paths))
            self.assertNotIn("ghs_loopback_secret_token_123", proc.stdout + proc.stderr)
            self.assertNotIn("eyJ", proc.stdout + proc.stderr)
            self.assertNotIn("PRIVATE KEY", proc.stdout + proc.stderr)

    def test_run_ocr_poll_text_report_and_admin_env_rejection(self):
        out = io.StringIO()
        err = io.StringIO()
        saved = os.environ.get("SCIFLOW_CICD_ADMIN_APP_ID")
        os.environ["SCIFLOW_CICD_ADMIN_APP_ID"] = "9999"
        try:
            rc = run_ocr_poll(
                app_id="9999",
                private_key_path="/nonexistent/key.pem",
                allowlist=["acme/repo-a"],
                out_stream=out,
                err_stream=err,
            )
        finally:
            if saved is None:
                os.environ.pop("SCIFLOW_CICD_ADMIN_APP_ID", None)
            else:
                os.environ["SCIFLOW_CICD_ADMIN_APP_ID"] = saved
        self.assertEqual(rc, 1)
        combined = out.getvalue() + err.getvalue()
        self.assertIn("FAIL", combined)
        self.assertIn("administrative", combined.lower())

        sample_result = poll_once(
            app_id=9999,
            allowlist=["acme/repo-a"],
            admin_app_ids=[9999],
        )
        text = format_poll_report(sample_result)
        self.assertIn("FAIL", text)

    def test_run_ocr_poll_redacts_env_secrets_in_json_and_text_errors(self):
        secret = "custom-env-llm-secret-67890"
        saved = os.environ.get("OCR_LLM_AUTH_TOKEN")
        os.environ["OCR_LLM_AUTH_TOKEN"] = secret
        try:
            out_text = io.StringIO()
            err_text = io.StringIO()
            rc_text = run_ocr_poll(
                app_id="3333",
                private_key_path=f"/nonexistent/{secret}/key.pem",
                allowlist=["acme/repo-a"],
                out_stream=out_text,
                err_stream=err_text,
            )
            self.assertEqual(rc_text, 1)
            combined_text = out_text.getvalue() + err_text.getvalue()
            self.assertNotIn(secret, combined_text)
            self.assertIn("[REDACTED]", combined_text)

            out_json = io.StringIO()
            rc_json = run_ocr_poll(
                app_id="3333",
                private_key_path=f"/nonexistent/{secret}/key.pem",
                allowlist=["acme/repo-a"],
                json_output=True,
                out_stream=out_json,
            )
            self.assertEqual(rc_json, 1)
            self.assertNotIn(secret, out_json.getvalue())
            self.assertIn("[REDACTED]", out_json.getvalue())
        finally:
            if saved is None:
                os.environ.pop("OCR_LLM_AUTH_TOKEN", None)
            else:
                os.environ["OCR_LLM_AUTH_TOKEN"] = saved

    def test_pr_eligibility_config_and_poll_once_with_config_dataclasses_and_no_dead_aliases(self):
        from dataclasses import FrozenInstanceError
        import scripts.ocr_poller as ocr_poller
        from scripts.ocr_app_auth import OcrAppConfig
        from scripts.ocr_poller import PrEligibilityConfig

        elig_cfg = PrEligibilityConfig(
            eligibility_mode="label",
            eligible_label="ocr-ready",
            maintainers=("alice",),
            include_drafts=False,
        )
        with self.assertRaises(FrozenInstanceError):
            elig_cfg.eligibility_mode = "either"  # type: ignore[misc]

        fake = FakePollingGitHubServer(
            app_info={
                "id": 7777,
                "slug": "ocr-bot",
                "name": "OCR Bot",
                "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
            },
            installations=[
                {
                    "id": 11,
                    "account": {"login": "acme"},
                    "repository_selection": "selected",
                    "permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"},
                }
            ],
            repos_by_installation={11: [{"full_name": "acme/repo-a"}]},
            pulls_by_repo={
                "acme/repo-a": [
                    _make_pr("acme/repo-a", 101, labels=["ocr-ready"], author_association="CONTRIBUTOR"),
                    _make_pr("acme/repo-a", 102, labels=[], author_association="OWNER"),
                ]
            },
        )
        app_cfg = OcrAppConfig(
            app_id=7777,
            allowlist=("acme/repo-a",),
            api_factory=fake.api_factory,
            signer=lambda d: b"sig",
        )
        res = poll_once(app_config=app_cfg, eligibility_config=elig_cfg)
        self.assertTrue(res.passed)
        self.assertEqual([c.number for c in res.candidates], [101])

        for dead_alias in ("_normalize_client_factory", "_clamp_poll_interval"):
            self.assertFalse(hasattr(ocr_poller, dead_alias), f"Dead alias still present: {dead_alias}")


if __name__ == "__main__":
    unittest.main()
