"""Tests for verifying a published implementation PR before issue closure."""

from __future__ import annotations

import unittest

from scripts.configure_github_repo import ApiError
from scripts.verify_issue_delivery import verify_issue_delivery


class FakeGitHubApi:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path))
        value = self.responses.get(path)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            return value
        if value is None:
            return 404, None
        return 200, value


class IssueDeliveryTests(unittest.TestCase):
    repo = "acme/project"
    sha = "a" * 40
    issue_path = f"/repos/{repo}/issues/80"
    pr_path = f"/repos/{repo}/pulls/12"
    branch_path = f"/repos/{repo}/branches/codex%2Fintegration"

    def responses(self, *, issue_body=None, issue_state="open", closed_at=None, pr_body=None, pr_repo=None, base="main", state="open", pr_sha=None, branch_sha=None, merged_at=None, merge_commit_sha=None):
        return {
            self.issue_path: {"state": issue_state, "closed_at": closed_at,
                              "body": issue_body or f"## Delivery\nPR: https://github.com/{self.repo}/pull/12\nHead SHA: `{self.sha}`"},
            self.pr_path: {
                "state": state,
                "draft": False,
                "merged_at": merged_at,
                "merge_commit_sha": merge_commit_sha,
                "base": {"ref": base, "repo": {"full_name": self.repo}},
                "head": {"ref": "codex/integration", "sha": pr_sha or self.sha, "repo": {"full_name": pr_repo or self.repo}},
                "body": pr_body or "## Implementation issues\n- Fixes https://github.com/acme/project/issues/80",
            },
            self.branch_path: {"commit": {"sha": branch_sha or self.sha}},
        }

    def check(self, responses):
        api = FakeGitHubApi(responses)
        result = verify_issue_delivery(api, self.repo, 80)
        return api, result

    def test_qualifying_delivery_reads_issue_pr_and_live_branch(self):
        api, result = self.check(self.responses())

        self.assertEqual(result, {"eligible": True, "code": "eligible", "issue": 80, "pull_request": 12, "head_sha": self.sha})
        self.assertEqual(api.calls, [
            ("GET", self.issue_path),
            ("GET", self.pr_path),
            ("GET", self.branch_path),
        ])

    def test_repeated_reads_are_deterministic_and_read_only(self):
        api = FakeGitHubApi(self.responses())

        first = verify_issue_delivery(api, self.repo, 80)
        second = verify_issue_delivery(api, self.repo, 80)

        self.assertEqual(first, second)
        self.assertEqual(api.calls.count(("GET", self.issue_path)), 2)
        self.assertEqual(api.calls.count(("GET", self.pr_path)), 2)
        self.assertEqual(api.calls.count(("GET", self.branch_path)), 2)
        self.assertTrue(all(method == "GET" for method, _ in api.calls))

    def test_missing_delivery_section_is_ineligible(self):
        _, result = self.check({self.issue_path: {"body": "No delivery recorded"}})
        self.assertEqual(result["code"], "delivery_absent")

    def test_ambiguous_delivery_references_are_ineligible(self):
        body = f"## Delivery\nhttps://github.com/{self.repo}/pull/12\nhttps://github.com/{self.repo}/pull/13\n{self.sha}"
        _, result = self.check(self.responses(issue_body=body))
        self.assertEqual(result["code"], "delivery_ambiguous")

    def test_local_only_delivery_is_ineligible_on_the_pr_path(self):
        _, result = self.check({self.issue_path: {"body": "## Delivery\nLocal-only exception"}})
        self.assertEqual(result["code"], "local_only_ineligible")

    def test_delivery_link_to_another_repository_is_rejected(self):
        body = f"## Delivery\nhttps://github.com/other/project/pull/12\n{self.sha}"
        _, result = self.check(self.responses(issue_body=body))
        self.assertEqual(result["code"], "wrong_repository")

    def test_inaccessible_pr_is_ineligible(self):
        responses = self.responses()
        responses[self.pr_path] = (404, None)
        _, result = self.check(responses)
        self.assertEqual(result["code"], "pr_inaccessible")

    def test_forbidden_pr_is_reported_as_inaccessible(self):
        responses = self.responses()
        responses[self.pr_path] = ApiError("GET /pulls/12: HTTP 403: Resource not accessible")
        _, result = self.check(responses)
        self.assertEqual(result["code"], "pr_inaccessible")

    def test_draft_pr_is_not_published_delivery(self):
        responses = self.responses()
        responses[self.pr_path]["draft"] = True
        _, result = self.check(responses)
        self.assertEqual(result["code"], "pr_unpublished")

    def test_closed_pr_is_ineligible(self):
        _, result = self.check(self.responses(state="closed"))
        self.assertEqual(result["code"], "pr_closed")

    def test_merged_pr_with_parent_link_is_eligible_without_branch_lookup(self):
        pr_body = f"## Parent\n- https://github.com/{self.repo}/issues/80"
        responses = self.responses(
            issue_state="closed",
            closed_at="2026-11-21T16:08:20Z",
            state="closed",
            merged_at="2026-11-21T15:33:48Z",
            merge_commit_sha="c" * 40,
            pr_body=pr_body,
        )

        api, result = self.check(responses)

        self.assertEqual(result, {"eligible": True, "code": "eligible", "issue": 80, "pull_request": 12, "head_sha": self.sha})
        self.assertEqual(api.calls, [("GET", self.issue_path), ("GET", self.pr_path)])

    def test_open_issue_with_merged_pr_is_not_a_recovery_path(self):
        pr_body = f"## Parent\n- https://github.com/{self.repo}/issues/80"
        responses = self.responses(
            state="closed",
            merged_at="2026-11-21T15:33:48Z",
            merge_commit_sha="c" * 40,
            pr_body=pr_body,
        )

        _, result = self.check(responses)

        self.assertEqual(result["code"], "issue_not_closed_after_merge")

    def test_issue_closed_before_merge_is_not_a_recovery_path(self):
        pr_body = f"## Parent\n- https://github.com/{self.repo}/issues/80"
        responses = self.responses(
            issue_state="closed",
            closed_at="2026-11-21T15:00:00Z",
            state="closed",
            merged_at="2026-11-21T15:33:48Z",
            merge_commit_sha="c" * 40,
            pr_body=pr_body,
        )

        _, result = self.check(responses)

        self.assertEqual(result["code"], "issue_not_closed_after_merge")

    def test_merged_pr_linking_both_sections_is_ambiguous(self):
        pr_body = (f"## Implementation issues\n- https://github.com/{self.repo}/issues/80\n\n"
                   f"## Parent\n- https://github.com/{self.repo}/issues/80")
        responses = self.responses(
            issue_state="closed",
            closed_at="2026-11-21T16:08:20Z",
            state="closed",
            merged_at="2026-11-21T15:33:48Z",
            merge_commit_sha="c" * 40,
            pr_body=pr_body,
        )

        _, result = self.check(responses)

        self.assertEqual(result["code"], "issue_not_linked")

    def test_cross_repository_head_is_rejected(self):
        _, result = self.check(self.responses(pr_repo="fork/project"))
        self.assertEqual(result["code"], "wrong_repository")

    def test_non_main_base_is_rejected(self):
        _, result = self.check(self.responses(base="release"))
        self.assertEqual(result["code"], "wrong_base")

    def test_missing_delivery_branch_is_rejected(self):
        responses = self.responses()
        del responses[self.branch_path]
        _, result = self.check(responses)
        self.assertEqual(result["code"], "wrong_branch")

    def test_stale_pr_or_branch_head_is_rejected(self):
        _, result = self.check(self.responses(branch_sha="b" * 40))
        self.assertEqual(result["code"], "stale_sha")

    def test_pr_must_explicitly_link_the_issue(self):
        _, result = self.check(self.responses(pr_body="## Implementation issues\n- https://github.com/acme/project/issues/81"))
        self.assertEqual(result["code"], "issue_not_linked")

    def test_open_pr_parent_link_is_not_qualifying_delivery(self):
        pr_body = f"## Parent\n- https://github.com/{self.repo}/issues/80"
        _, result = self.check(self.responses(pr_body=pr_body))
        self.assertEqual(result["code"], "issue_not_linked")

    def test_api_failures_are_distinct_from_ineligible_evidence(self):
        _, result = self.check({self.issue_path: ApiError("network unavailable")})
        self.assertEqual(result["code"], "api_failure")
        self.assertFalse(result["eligible"])

    def test_malformed_live_pr_response_is_an_api_failure(self):
        responses = self.responses()
        responses[self.pr_path] = {"state": "open"}
        _, result = self.check(responses)
        self.assertEqual(result["code"], "api_failure")


if __name__ == "__main__":
    unittest.main()
