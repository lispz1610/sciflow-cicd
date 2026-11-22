"""Tests for the implementation issue state gate."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts.issue_state_gate import (
    GitHubClient,
    evaluate_implementation_issues,
    format_summary,
    parse_implementation_issues,
    main,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestImplementationIssueParsing(unittest.TestCase):
    def test_parses_distinct_issue_numbers_from_explicit_section(self):
        body = """## Summary
Feature work.

## Implementation issues
- #81
- #82

## Notes
Anything else.
"""

        self.assertEqual(
            parse_implementation_issues(body, "lispz1610/sciflow-cicd"),
            [81, 82],
        )

    def test_accepts_same_repository_qualified_references_and_issue_urls(self):
        body = """## Implementation issues
- lispz1610/sciflow-cicd#81
- https://github.com/lispz1610/sciflow-cicd/issues/82
"""

        self.assertEqual(
            parse_implementation_issues(body, "lispz1610/sciflow-cicd"),
            [81, 82],
        )

    def test_missing_section_requires_actionable_pr_description(self):
        with self.assertRaisesRegex(ValueError, "Implementation issues"):
            parse_implementation_issues("## Summary\nFeature work", "owner/repo")

    def test_empty_section_requires_issue_reference(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            parse_implementation_issues("## Implementation issues\nNone", "owner/repo")

    def test_rejects_duplicate_sections(self):
        body = "## Implementation issues\n#1\n## Implementation issues\n#2"
        with self.assertRaisesRegex(ValueError, "exactly one"):
            parse_implementation_issues(body, "owner/repo")

    def test_rejects_duplicate_references_as_ambiguous_scope(self):
        with self.assertRaisesRegex(ValueError, "(?i)duplicate.*#81"):
            parse_implementation_issues(
                "## Implementation issues\n- #81\n- #81", "owner/repo"
            )

    def test_rejects_malformed_issue_numbers_with_specific_reason(self):
        with self.assertRaisesRegex(ValueError, "(?i)malformed.*#0"):
            parse_implementation_issues("## Implementation issues\n- #0", "owner/repo")

    def test_rejects_malformed_extra_reference_after_valid_issue(self):
        with self.assertRaisesRegex(ValueError, "(?i)malformed.*#-2"):
            parse_implementation_issues("## Implementation issues\n- #81\n- #-2", "owner/repo")
        with self.assertRaisesRegex(ValueError, "(?i)malformed.*#82x"):
            parse_implementation_issues("## Implementation issues\n- #81\n- #82x", "owner/repo")

    def test_rejects_malformed_issue_urls_with_specific_reason(self):
        with self.assertRaisesRegex(ValueError, "(?i)malformed.*issue URL"):
            parse_implementation_issues(
                "## Implementation issues\n- https://github.com/owner/repo/issues/nope",
                "owner/repo",
            )


class TestIssueStateEvaluation(unittest.TestCase):
    repository = "lispz1610/sciflow-cicd"
    repository_url = "https://api.github.com/repos/lispz1610/sciflow-cicd"

    def issue(self, number: int, state: str, **extra):
        return {
            "number": number,
            "title": f"Ticket {number}",
            "state": state,
            "body": "## Delivery\n- Pull request recorded",
            "repository_url": self.repository_url,
            "html_url": f"https://github.com/lispz1610/sciflow-cicd/issues/{number}",
            **extra,
        }

    def evaluate(self, body: str, issues: dict[int, dict]):
        def fetch(number):
            issue = issues[number]
            if isinstance(issue, Exception):
                raise issue
            return issue

        return evaluate_implementation_issues(body, self.repository, fetch)

    def test_all_closed_issues_pass_and_summary_lists_current_states(self):
        result = self.evaluate(
            "## Implementation issues\n- #81\n- #82",
            {81: self.issue(81, "closed"), 82: self.issue(82, "closed")},
        )

        self.assertTrue(result.passed)

    def test_closed_local_only_issue_does_not_satisfy_merge_gate(self):
        result = self.evaluate(
            "## Implementation issues\n- #81",
            {81: self.issue(81, "closed", body="## Delivery\n- Local-only exception: approved")},
        )

        self.assertFalse(result.passed)
        self.assertIn("local-only delivery", result.errors[0])

    def test_closed_issue_with_missing_or_invalid_body_fails_closed(self):
        for bad_body in (None, 23, {"text": "delivery"}):
            with self.subTest(body=bad_body):
                result = self.evaluate(
                    "## Implementation issues\n- #81",
                    {81: self.issue(81, "closed", body=bad_body)},
                )
                self.assertFalse(result.passed)
                self.assertIn("incomplete", format_summary(result).lower())

    def test_duplicate_delivery_sections_cannot_hide_local_only_exception(self):
        result = self.evaluate(
            "## Implementation issues\n- #81",
            {81: self.issue(81, "closed", body=(
                "## Delivery\n- Local-only exception: approved\n\n## Delivery\n- More notes"
            ))},
        )

        self.assertFalse(result.passed)
        self.assertIn("local-only delivery", result.errors[0])
        self.assertIn("#81", format_summary(result))
        self.assertIn("closed", format_summary(result))

    def test_open_issue_fails_and_names_blocker(self):
        result = self.evaluate(
            "## Implementation issues\n- #81",
            {81: self.issue(81, "open")},
        )

        self.assertFalse(result.passed)
        self.assertIn("#81", format_summary(result))
        self.assertIn("open", format_summary(result))
        self.assertIn("block merge", format_summary(result))

    def test_mixed_states_fail_and_report_both_issues(self):
        result = self.evaluate(
            "## Implementation issues\n- #81\n- #82",
            {81: self.issue(81, "closed"), 82: self.issue(82, "open")},
        )

        self.assertFalse(result.passed)
        summary = format_summary(result)
        self.assertIn("#81", summary)
        self.assertIn("#82", summary)
        self.assertIn("open", summary)

    def test_missing_section_fails_with_action(self):
        result = self.evaluate("## Summary\nWork", {})

        self.assertFalse(result.passed)
        self.assertIn("Implementation issues", format_summary(result))

    def test_pull_request_cannot_be_declared_as_implementation_issue(self):
        result = self.evaluate(
            "## Implementation issues\n- #81",
            {81: self.issue(81, "closed", pull_request={"url": "https://api.github.com/pr"})},
        )

        self.assertFalse(result.passed)
        self.assertIn("pull request", format_summary(result).lower())

    def test_issue_from_different_repository_fails_closed(self):
        issue = self.issue(81, "closed")
        issue["repository_url"] = "https://api.github.com/repos/other/project"
        result = self.evaluate("## Implementation issues\n- #81", {81: issue})

        self.assertFalse(result.passed)
        self.assertIn("repository", format_summary(result).lower())

    def test_inaccessible_issue_has_specific_fail_closed_reason(self):
        from scripts.issue_state_gate import GitHubApiError

        result = self.evaluate(
            "## Implementation issues\n- #81",
            {81: GitHubApiError("GitHub API returned HTTP 403: Resource not accessible")},
        )

        self.assertFalse(result.passed)
        self.assertIn("inaccessible", format_summary(result).lower())

    def test_deleted_issue_has_specific_fail_closed_reason(self):
        from scripts.issue_state_gate import GitHubApiError

        result = self.evaluate(
            "## Implementation issues\n- #81",
            {81: GitHubApiError("GitHub API returned HTTP 410: Gone")},
        )

        self.assertFalse(result.passed)
        self.assertIn("deleted", format_summary(result).lower())

    def test_api_404_is_reported_as_deleted_or_inaccessible_not_missing_scope(self):
        from scripts.issue_state_gate import GitHubApiError

        result = self.evaluate(
            "## Implementation issues\n- #81",
            {81: GitHubApiError("GitHub API returned HTTP 404: Not Found")},
        )

        self.assertFalse(result.passed)
        self.assertRegex(format_summary(result).lower(), "deleted or inaccessible")


class TestCheckPublication(unittest.TestCase):
    def test_publishes_named_check_for_the_current_head_sha(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        result = evaluate_implementation_issues(
            "## Implementation issues\n#81",
            "owner/repo",
            lambda number: {
                "number": number,
                "title": "Ticket",
                "state": "closed",
                "body": "## Delivery\n- Pull request recorded",
                "repository_url": "https://api.github.com/repos/owner/repo",
            },
        )
        sha = "0123456789abcdef" * 2 + "01234567"

        with patch("scripts.issue_state_gate.urlopen", return_value=response) as open_url:
            GitHubClient("token").publish_check("owner/repo", sha, result)

        request = open_url.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.github.com/repos/owner/repo/check-runs")
        self.assertEqual(payload["name"], "Issue State Gate")
        self.assertEqual(payload["head_sha"], sha)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["conclusion"], "success")
        self.assertIn("#81", payload["output"]["summary"])

    def test_open_issue_publishes_failure_with_blocker_summary(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        result = evaluate_implementation_issues(
            "## Implementation issues\n#81",
            "owner/repo",
            lambda number: {
                "number": number,
                "title": "Ticket",
                "state": "open",
                "repository_url": "https://api.github.com/repos/owner/repo",
            },
        )

        with patch("scripts.issue_state_gate.urlopen", return_value=response) as open_url:
            GitHubClient("token").publish_check("owner/repo", "a" * 40, result)

        payload = json.loads(open_url.call_args.args[0].data)
        self.assertEqual(payload["conclusion"], "failure")
        self.assertIn("#81", payload["output"]["summary"])
        self.assertIn("open", payload["output"]["summary"])

    def test_pull_request_lookup_uses_live_pull_request_endpoint(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"number": 7}'

        with patch("scripts.issue_state_gate.urlopen", return_value=response) as open_url:
            self.assertEqual(GitHubClient("token").fetch_pull_request("owner/repo", 7), {"number": 7})

        self.assertEqual(
            open_url.call_args.args[0].full_url,
            "https://api.github.com/repos/owner/repo/pulls/7",
        )

    def test_queued_event_does_not_publish_when_live_head_has_advanced(self):
        old_sha = "a" * 40
        new_sha = "b" * 40
        event = {
            "pull_request": {
                "number": 7,
                "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
                "head": {"sha": old_sha, "repo": {"full_name": "owner/repo"}},
                "body": "## Implementation issues\n#81",
            }
        }

        class FakeClient:
            def __init__(self, token, api_url):
                self.published = []

            def fetch_pull_request(self, repository, number):
                return {
                    "state": "open",
                    "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
                    "head": {"sha": new_sha, "repo": {"full_name": "owner/repo"}},
                    "body": "## Implementation issues\n#82",
                }

            def publish_check(self, *args):
                self.published.append(args)

        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as event_file:
            json.dump(event, event_file)
            event_file.flush()
            with patch.dict(os.environ, {
                "GITHUB_REPOSITORY": "owner/repo",
                "GITHUB_TOKEN": "token",
                "GITHUB_EVENT_PATH": event_file.name,
            }), patch("scripts.issue_state_gate.GitHubClient", FakeClient):
                self.assertEqual(main(), 0)

    def test_fork_pull_request_publishes_explicit_unsupported_failure(self):
        sha = "c" * 40
        event = {
            "pull_request": {
                "number": 9,
                "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
                "head": {"sha": sha, "repo": {"full_name": "contributor/repo"}},
                "body": "## Implementation issues\n#81",
            }
        }
        published = []

        class FakeClient:
            def __init__(self, token, api_url):
                pass

            def fetch_pull_request(self, repository, number):
                return {
                    "state": "open",
                    "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
                    "head": {"sha": sha, "repo": {"full_name": "contributor/repo"}},
                    "body": "## Implementation issues\n#81",
                }

            def publish_check(self, repository, head_sha, result):
                published.append((repository, head_sha, result))

        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as event_file:
            json.dump(event, event_file)
            event_file.flush()
            with patch.dict(os.environ, {
                "GITHUB_REPOSITORY": "owner/repo",
                "GITHUB_TOKEN": "token",
                "GITHUB_EVENT_PATH": event_file.name,
            }), patch("scripts.issue_state_gate.GitHubClient", FakeClient):
                self.assertEqual(main(), 1)

        self.assertEqual(len(published), 1)
        self.assertEqual(published[0][1], sha)
        self.assertIn("forks are unsupported", format_summary(published[0][2]))

    def test_workflow_refreshes_all_required_pull_request_events_without_path_filter(self):
        workflow = (REPO_ROOT / ".github/workflows/issue-state-gate.yml").read_text()

        self.assertRegex(workflow, r"types: \[opened, reopened, synchronize, edited\]")
        self.assertNotRegex(workflow, r"(?m)^\s+paths(?:-ignore)?:")

    def test_workflow_is_scoped_to_main_and_uses_only_trusted_base_code(self):
        workflow = (REPO_ROOT / ".github/workflows/issue-state-gate.yml").read_text()

        self.assertRegex(workflow, r"pull_request_target:\n\s+branches: \[main\]")
        self.assertIn("checks: write", workflow)
        self.assertIn("issues: read", workflow)
        self.assertIn("pull-requests: read", workflow)
        self.assertIn("python3 scripts/issue_state_gate.py", workflow)
        self.assertNotRegex(workflow, r"(?m)^\s+ref:\s*\$\{\{\s*github\.event\.pull_request\.head")
        self.assertRegex(workflow, r"actions/checkout@[0-9a-f]{40}")
        self.assertRegex(workflow, r"actions/setup-python@[0-9a-f]{40}")

    def test_rejects_issue_reference_for_another_repository(self):
        body = "## Implementation issues\n- other/project#81"
        with self.assertRaisesRegex(ValueError, "outside owner/repo"):
            parse_implementation_issues(body, "owner/repo")

    def test_list_check_runs_paginates_beyond_100_entries(self):
        client = GitHubClient("tok")
        page1 = {"total_count": 101, "check_runs": [{"id": i, "name": f"run-{i}"} for i in range(100)]}
        page2 = {"total_count": 101, "check_runs": [{"id": 100, "name": "run-100"}]}
        requested = []

        def fake_request(method, path):
            requested.append(path)
            if "&page=1" in path:
                return page1
            if "&page=2" in path:
                return page2
            return {"total_count": 101, "check_runs": []}

        client._request = fake_request
        runs = client.list_check_runs("owner/repo", "a" * 40)
        self.assertEqual(len(runs), 101)
        self.assertTrue(any("&page=1" in p for p in requested))
        self.assertTrue(any("&page=2" in p for p in requested))

    def test_repo_path_rejects_traversal_and_extra_slashes(self):
        with self.assertRaises(ValueError):
            GitHubClient._repo_path("../repo")
        with self.assertRaises(ValueError):
            GitHubClient._repo_path("owner/repo/extra")


if __name__ == "__main__":
    unittest.main()
