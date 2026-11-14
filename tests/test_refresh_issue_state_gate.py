"""Tests for refreshing Issue State Gate checks after issue state changes."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from scripts.issue_state_gate import GitHubApiError
from scripts.refresh_issue_state_gate import refresh_checks_for_issue


REPOSITORY = "owner/repo"
REPOSITORY_URL = "https://api.github.com/repos/owner/repo"


def issue(number: int, state: str) -> dict:
    return {
        "number": number,
        "state": state,
        "body": "## Delivery\n- Pull request recorded",
        "title": f"Issue {number}",
        "repository_url": REPOSITORY_URL,
        "html_url": f"https://github.com/owner/repo/issues/{number}",
    }


def pull(number: int, body: str, sha: str, *, base: str = "main") -> dict:
    return {
        "number": number,
        "state": "open",
        "body": body,
        "head": {"sha": sha, "repo": {"full_name": REPOSITORY}},
        "base": {"ref": base, "repo": {"full_name": REPOSITORY}},
    }


class FakeGitHubClient:
    def __init__(self, pulls: list[dict], live_pulls: dict[int, dict], state: str = "closed"):
        self.pulls = pulls
        self.live_pulls = live_pulls
        self.state = state
        self.published: list[tuple[str, object]] = []
        self.issue_reads: list[int] = []

    def fetch_issue(self, repository: str, number: int) -> dict:
        self.issue_reads.append(number)
        return issue(number, self.state)

    def list_open_pull_requests(self, repository: str) -> list[dict]:
        return self.pulls

    def fetch_pull_request(self, repository: str, number: int) -> dict:
        return self.live_pulls[number]

    def publish_check(self, repository: str, head_sha: str, result: object) -> None:
        self.published.append((head_sha, result))


class TestIssueGateRefresh(unittest.TestCase):
    def test_refreshes_every_matching_open_pr_at_its_live_head_and_ignores_others(self):
        matching = "## Implementation issues\n- #77"
        pulls = [
            pull(10, matching, "a" * 40),
            pull(11, "## Implementation issues\n- #78", "b" * 40),
            pull(12, matching, "c" * 40),
            pull(13, matching, "d" * 40, base="develop"),
            pull(14, "## Implementation issues\n- #77\n- #77", "e" * 40),
            pull(15, matching, "i" * 40),
        ]
        live_pulls = {
            10: pull(10, matching, "f" * 40),
            11: pulls[1],
            12: pull(12, matching, "7" * 40),
            13: pulls[3],
            14: pulls[4],
            15: pull(15, "## Implementation issues\n- #78", "8" * 40),
        }
        client = FakeGitHubClient(pulls, live_pulls)

        result = refresh_checks_for_issue(client, REPOSITORY, 77)

        self.assertEqual(result, 0)
        self.assertEqual([sha for sha, _ in client.published], ["f" * 40, "7" * 40])
        self.assertTrue(client.published[0][1].passed)
        self.assertEqual(client.issue_reads, [77, 77])

    def test_issue_state_refresh_workflow_handles_close_and_reopen_events(self):
        from pathlib import Path

        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/issue-state-gate-refresh.yml"
        ).read_text()

        self.assertIn("issues:", workflow)
        self.assertIn("types: [closed, reopened]", workflow)
        self.assertIn("checks: write", workflow)
        self.assertIn("pull-requests: read", workflow)
        self.assertIn("run: python3 scripts/refresh_issue_state_gate.py", workflow)

    def test_replayed_refresh_reflects_final_reopened_issue_state(self):
        pr = pull(10, "## Implementation issues\n- #77", "a" * 40)
        client = FakeGitHubClient([pr], {10: pr}, state="open")

        result = refresh_checks_for_issue(client, REPOSITORY, 77)

        self.assertEqual(result, 1)
        sha, check = client.published[0]
        self.assertEqual(sha, "a" * 40)
        self.assertFalse(check.passed)
        self.assertEqual(check.issues[0].state, "open")

    def test_issue_lookup_errors_publish_failure_instead_of_leaving_green_check(self):
        pr = pull(10, "## Implementation issues\n- #77", "a" * 40)
        client = FakeGitHubClient([pr], {10: pr})
        client.fetch_issue = lambda repository, number: (_ for _ in ()).throw(
            GitHubApiError("GitHub API returned HTTP 403: denied")
        )

        result = refresh_checks_for_issue(client, REPOSITORY, 77)

        self.assertEqual(result, 1)
        self.assertFalse(client.published[0][1].passed)
        self.assertIn("inaccessible", client.published[0][1].errors[0])

    def test_pull_request_revalidation_error_publishes_failure_for_listed_head(self):
        listed = pull(10, "## Implementation issues\n- #77", "a" * 40)
        client = FakeGitHubClient([listed], {})
        client.fetch_pull_request = lambda repository, number: (_ for _ in ()).throw(
            GitHubApiError("temporary API failure")
        )

        result = refresh_checks_for_issue(client, REPOSITORY, 77)

        self.assertEqual(result, 1)
        self.assertEqual(client.published[0][0], "a" * 40)
        self.assertFalse(client.published[0][1].passed)
        self.assertIn("revalidate", client.published[0][1].errors[0].lower())

    def test_open_pull_request_listing_follows_all_pages(self):
        pages = [
            [{"number": number} for number in range(1, 101)],
            [{"number": 101}],
        ]
        responses = []
        for page in pages:
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(page).encode()
            responses.append(response)

        from scripts.issue_state_gate import GitHubClient

        with patch("scripts.issue_state_gate.urlopen", side_effect=responses) as open_url:
            result = GitHubClient("token").list_open_pull_requests(REPOSITORY)

        self.assertEqual(len(result), 101)
        self.assertEqual(
            [call.args[0].full_url for call in open_url.call_args_list],
            [
                "https://api.github.com/repos/owner/repo/pulls?state=open&per_page=100&page=1",
                "https://api.github.com/repos/owner/repo/pulls?state=open&per_page=100&page=2",
            ],
        )
