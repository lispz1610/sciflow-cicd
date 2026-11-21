"""Tests for enforcing implementation delivery evidence after issue closure."""

from __future__ import annotations

import unittest

from scripts.configure_github_repo import ApiError
from scripts.enforce_issue_delivery import handle_issue_closed_event


class FakeGitHubApi:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        value = self.responses.get((method, path), self.responses.get(path))
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(method, path, payload)
        if isinstance(value, tuple):
            return value
        if method == "PATCH" and path.endswith("/issues/81"):
            self.responses[path]["state"] = payload["state"]
            return 200, dict(self.responses[path])
        if method == "POST" and path.endswith("/issues/81/comments"):
            comment = {"body": payload["body"]}
            comments_path = path + "?per_page=100&page=1"
            self.responses[comments_path].append(comment)
            return 201, comment
        if value is None:
            return 404, None
        return (201 if method == "POST" else 200), value


class EnforceIssueDeliveryTests(unittest.TestCase):
    repo = "acme/project"
    issue = 81
    sha = "a" * 40
    issue_path = f"/repos/{repo}/issues/{issue}"
    comments_path = f"{issue_path}/comments?per_page=100&page=1"
    pr_path = f"/repos/{repo}/pulls/12"
    branch_path = f"/repos/{repo}/branches/codex%2Fintegration"
    open_pulls_page = f"/repos/{repo}/pulls?state=open&per_page=100&page=1"

    def declared_pull(self, number=12, body=None):
        return {
            "number": number,
            "state": "open",
            "body": body or f"## Implementation issues\n- #{self.issue}",
            "base": {"ref": "main", "repo": {"full_name": self.repo}},
        }

    def responses(self, *, issue_state="closed", closed_at=None, issue_body="No delivery recorded", comments=None):
        return {
            self.issue_path: {"state": issue_state, "closed_at": closed_at, "body": issue_body,
                              "labels": [{"name": "ready-for-agent"}]},
            self.comments_path: comments or [],
            self.pr_path: {
                "state": "open",
                "draft": False,
                "base": {"ref": "main", "repo": {"full_name": self.repo}},
                "head": {"ref": "codex/integration", "sha": self.sha, "repo": {"full_name": self.repo}},
                "body": f"## Implementation issues\n- https://github.com/{self.repo}/issues/{self.issue}",
            },
            self.branch_path: {"commit": {"sha": self.sha}},
            self.open_pulls_page: [],
        }

    def test_closed_issue_with_invalid_evidence_gets_one_diagnostic_then_reopens(self):
        api = FakeGitHubApi(self.responses())

        result = handle_issue_closed_event(api, self.repo, self.issue)

        writes = [(method, path, payload) for method, path, payload in api.calls if method != "GET"]
        self.assertEqual(result["action"], "reopened")
        self.assertEqual(result["code"], "delivery_absent")
        self.assertEqual([method for method, _, _ in writes], ["POST", "PATCH"])
        self.assertEqual(writes[0][1], self.comments_path.split("?")[0])
        self.assertIn("issue-delivery-enforcement", writes[0][2]["body"])
        self.assertIn("before it is merged", writes[0][2]["body"])
        self.assertEqual(writes[1][2], {"state": "open"})

    def test_unrelated_closed_issue_is_not_reopened(self):
        responses = self.responses()
        responses[self.issue_path]["labels"] = [{"name": "needs-triage"}]
        responses[self.open_pulls_page] = [self.declared_pull(body="## Implementation issues\n- #90")]
        api = FakeGitHubApi(responses)

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["code"], "not_implementation_issue")
        self.assertFalse(any(method != "GET" for method, _, _ in api.calls))

    def test_pr_declaration_accepts_same_repository_issue_url(self):
        responses = self.responses()
        responses[self.issue_path]["labels"] = []
        responses[self.open_pulls_page] = [self.declared_pull(
            body=f"## Implementation issues\n- https://github.com/{self.repo}/issues/{self.issue}"
        )]
        api = FakeGitHubApi(responses)

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["action"], "reopened")

    def test_pr_declared_issue_without_role_label_still_requires_delivery(self):
        responses = self.responses()
        responses[self.issue_path]["labels"] = []
        responses[self.open_pulls_page] = [self.declared_pull()]
        api = FakeGitHubApi(responses)

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["action"], "reopened")
        self.assertEqual(result["code"], "delivery_absent")

    def test_parent_link_marks_unlabeled_implementation_issue_before_pr_exists(self):
        responses = self.responses(issue_body="## Parent\n- #70\n\nNo delivery recorded")
        responses[self.issue_path]["labels"] = []
        api = FakeGitHubApi(responses)

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["action"], "reopened")
        self.assertEqual(result["code"], "delivery_absent")

    def test_pr_scope_scan_follows_pagination(self):
        responses = self.responses()
        responses[self.issue_path]["labels"] = []
        responses[self.open_pulls_page] = [self.declared_pull(number=n, body="## Implementation issues\n- #90")
                                           for n in range(100)]
        responses[f"/repos/{self.repo}/pulls?state=open&per_page=100&page=2"] = [self.declared_pull()]
        api = FakeGitHubApi(responses)

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["action"], "reopened")

    def test_pr_scope_api_failure_keeps_issue_closed_and_reports_error(self):
        responses = self.responses()
        responses[self.issue_path]["labels"] = []
        responses[self.open_pulls_page] = ApiError("GitHub API unavailable")
        api = FakeGitHubApi(responses)

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["code"], "api_failure")
        self.assertEqual(api.responses[self.issue_path]["state"], "closed")
        self.assertFalse(any(method != "GET" for method, _, _ in api.calls))

    def test_malformed_pr_scope_keeps_issue_closed_and_reports_error(self):
        responses = self.responses()
        responses[self.issue_path]["labels"] = []
        responses[self.open_pulls_page] = [self.declared_pull(body="## Implementation issues\n- #-2")]
        api = FakeGitHubApi(responses)

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["code"], "api_failure")
        self.assertEqual(api.responses[self.issue_path]["state"], "closed")
        self.assertFalse(any(method != "GET" for method, _, _ in api.calls))

    def test_qualifying_closure_stays_closed_and_records_receipt(self):
        body = f"## Delivery\n- Pull request: https://github.com/{self.repo}/pull/12\n- Head SHA: `{self.sha}`"
        api = FakeGitHubApi(self.responses(issue_body=body))

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["action"], "none")
        self.assertEqual(result["code"], "eligible")
        writes = [(method, path, payload) for method, path, payload in api.calls if method != "GET"]
        self.assertEqual([method for method, _, _ in writes], ["POST"])
        self.assertIn("issue-delivery-verified:v1", writes[0][2]["body"])

    def test_merged_delivery_closure_stays_closed_and_records_receipt(self):
        body = f"## Delivery\n- Pull request: https://github.com/{self.repo}/pull/12\n- Head SHA: `{self.sha}`"
        responses = self.responses(issue_body=body, closed_at="2026-11-21T16:08:20Z")
        responses[self.pr_path].update({
            "state": "closed",
            "merged_at": "2026-11-21T15:33:48Z",
            "merge_commit_sha": "c" * 40,
            "body": f"## Parent\n- https://github.com/{self.repo}/issues/{self.issue}",
        })
        api = FakeGitHubApi(responses)

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["action"], "none")
        self.assertEqual(result["code"], "eligible")
        self.assertEqual(api.responses[self.issue_path]["state"], "closed")
        writes = [(method, path, payload) for method, path, payload in api.calls if method != "GET"]
        self.assertEqual([method for method, _, _ in writes], ["POST"])
        self.assertIn("issue-delivery-verified:v1", writes[0][2]["body"])

    def test_closed_unmerged_delivery_is_reopened(self):
        body = f"## Delivery\n- Pull request: https://github.com/{self.repo}/pull/12\n- Head SHA: `{self.sha}`"
        responses = self.responses(issue_body=body)
        responses[self.pr_path].update({"state": "closed", "merged_at": None})
        api = FakeGitHubApi(responses)

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["action"], "reopened")
        self.assertEqual(result["code"], "pr_closed")
        self.assertEqual(api.responses[self.issue_path]["state"], "open")

    def test_explicit_local_only_exception_stays_closed_without_pr_receipt(self):
        body = "## Delivery\n- Local-only exception: approved by maintainer."
        api = FakeGitHubApi(self.responses(issue_body=body))

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["action"], "none")
        self.assertEqual(result["code"], "local_only_exception")
        self.assertEqual(api.responses[self.issue_path]["state"], "closed")
        self.assertFalse(any(method != "GET" for method, _, _ in api.calls))

    def test_verified_closure_replay_survives_linked_pr_head_advance(self):
        body = f"## Delivery\n- Pull request: https://github.com/{self.repo}/pull/12\n- Head SHA: `{self.sha}`"
        api = FakeGitHubApi(self.responses(issue_body=body))

        first = handle_issue_closed_event(api, self.repo, self.issue)
        writes_after_first = len([method for method, _, _ in api.calls if method != "GET"])
        api.responses[self.pr_path]["head"]["sha"] = "b" * 40
        api.responses[self.branch_path]["commit"]["sha"] = "b" * 40
        second = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(first["code"], "eligible")
        self.assertEqual(second["code"], "eligible")
        self.assertTrue(second["verified_receipt"])
        self.assertEqual(len([method for method, _, _ in api.calls if method != "GET"]), writes_after_first)
        self.assertEqual(api.responses[self.issue_path]["state"], "closed")

    def test_head_advance_between_qualifying_reads_is_visible_and_does_not_reopen(self):
        body = f"## Delivery\n- Pull request: https://github.com/{self.repo}/pull/12\n- Head SHA: `{self.sha}`"
        responses = self.responses(issue_body=body)
        pr_template = responses[self.pr_path]
        pr_reads = 0
        branch_reads = 0

        def changing_pr(_method, _path, _payload):
            nonlocal pr_reads
            pr_reads += 1
            pr = dict(pr_template)
            pr["head"] = dict(pr["head"])
            pr["head"]["sha"] = self.sha if pr_reads == 1 else "b" * 40
            return 200, pr

        def changing_branch(_method, _path, _payload):
            nonlocal branch_reads
            branch_reads += 1
            sha = self.sha if branch_reads == 1 else "b" * 40
            return 200, {"commit": {"sha": sha}}

        responses[self.pr_path] = changing_pr
        responses[self.branch_path] = changing_branch
        api = FakeGitHubApi(responses)

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["action"], "error")
        self.assertEqual(result["code"], "verification_race")
        self.assertFalse(any(method != "GET" for method, _, _ in api.calls))

    def test_replayed_closure_does_not_duplicate_managed_diagnostic(self):
        api = FakeGitHubApi(self.responses(comments=[{"body": "<!-- issue-delivery-enforcement:v1 --> already reported"}]))

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["action"], "reopened")
        self.assertEqual([method for method, _, _ in api.calls if method != "GET"], ["PATCH"])

    def test_second_delivery_event_after_success_is_a_no_op(self):
        api = FakeGitHubApi(self.responses())

        first = handle_issue_closed_event(api, self.repo, self.issue)
        writes_after_first = len([method for method, _, _ in api.calls if method != "GET"])
        second = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(first["action"], "reopened")
        self.assertEqual(second["code"], "issue_not_closed")
        self.assertEqual(len([method for method, _, _ in api.calls if method != "GET"]), writes_after_first)

    def test_delivery_api_failure_does_not_reopen_issue(self):
        responses = self.responses()
        responses[self.issue_path]["body"] = (
            f"## Delivery\nhttps://github.com/{self.repo}/pull/12\n{self.sha}"
        )
        responses[self.pr_path] = ApiError("network unavailable")
        api = FakeGitHubApi(responses)

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["action"], "error")
        self.assertEqual(result["code"], "api_failure")
        self.assertFalse(any(method != "GET" for method, _, _ in api.calls))

    def test_already_open_issue_is_ignored_after_re_read(self):
        api = FakeGitHubApi(self.responses(issue_state="open"))

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["action"], "none")
        self.assertEqual(result["code"], "issue_not_closed")
        self.assertFalse(any(method != "GET" for method, _, _ in api.calls))

    def test_issue_reopened_during_validation_is_not_written_again(self):
        responses = self.responses()
        reads = 0

        def issue_changes_state(_method, _path, _payload):
            nonlocal reads
            reads += 1
            state = "open" if reads >= 5 else "closed"
            return 200, {"state": state, "body": "No delivery recorded",
                         "labels": [{"name": "ready-for-agent"}]}

        responses[self.issue_path] = issue_changes_state
        api = FakeGitHubApi(responses)

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["code"], "issue_not_closed")
        self.assertFalse(any(method != "GET" for method, _, _ in api.calls))

    def test_comment_failure_does_not_reopen_issue_and_is_reported(self):
        responses = self.responses()
        responses[("POST", self.issue_path + "/comments")] = ApiError("HTTP 403 forbidden")
        api = FakeGitHubApi(responses)

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["action"], "error")
        self.assertEqual(result["code"], "api_failure")
        self.assertNotIn(("PATCH", self.issue_path, {"state": "open"}), api.calls)

    def test_reopen_failure_is_reported_after_managed_comment_is_recorded(self):
        responses = self.responses()
        responses[("PATCH", self.issue_path)] = ApiError("HTTP 403 forbidden")
        api = FakeGitHubApi(responses)

        result = handle_issue_closed_event(api, self.repo, self.issue)

        self.assertEqual(result["action"], "error")
        self.assertEqual(result["code"], "api_failure")
        self.assertTrue(any(method == "POST" for method, _, _ in api.calls))
        self.assertTrue(any(method == "PATCH" for method, _, _ in api.calls))


if __name__ == "__main__":
    unittest.main()
