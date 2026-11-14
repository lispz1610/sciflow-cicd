"""Tests for reconciling Issue State Gate results with live pull requests."""

from __future__ import annotations

import unittest

from scripts.reconcile_issue_state_gate import reconcile_issue_state_gate
from scripts.issue_state_gate import IssueGateResult

REPOSITORY = "owner/repo"


def pull(number: int, state: str, sha: str, body: str, *, merged: bool = False) -> dict:
    return {
        "number": number,
        "state": state,
        "merged": merged,
        "body": body,
        "html_url": f"https://github.com/owner/repo/pull/{number}",
        "head": {"sha": sha, "repo": {"full_name": REPOSITORY}},
        "base": {"ref": "main", "repo": {"full_name": REPOSITORY}},
    }


class FakeClient:
    def __init__(self, pulls: list[dict], checks: dict[int, list[dict]] | None = None):
        self.pulls = pulls
        self.checks = checks or {}
        self.issues = {77: {"number": 77, "state": "closed", "body": "## Delivery\n- Pull request recorded", "title": "Implementation", "html_url": "https://github.com/owner/repo/issues/77", "repository_url": "https://api.github.com/repos/owner/repo"}}
        self.published: list[tuple[int, str, IssueGateResult]] = []
        self.comments: list[tuple[int, str]] = []
        self.comment_records: dict[int, list[dict]] = {}

    def list_pull_requests(self, repository: str) -> list[dict]:
        return self.pulls

    def fetch_pull_request(self, repository: str, number: int) -> dict:
        return next(item for item in self.pulls if item["number"] == number)

    def list_check_runs(self, repository: str, sha: str) -> list[dict]:
        return [run for runs in self.checks.values() for run in runs if run.get("head_sha") == sha]

    def list_pull_request_commits(self, repository: str, number: int) -> list[dict]:
        runs = self.checks.get(number, [])
        return [{"sha": run["head_sha"]} for run in runs]

    def fetch_issue(self, repository: str, number: int) -> dict:
        return self.issues[number]

    def publish_check(self, repository: str, sha: str, result: IssueGateResult) -> None:
        self.published.append((0, sha, result))

    def update_check(self, repository: str, check_id: int, sha: str, result: IssueGateResult) -> None:
        self.published.append((check_id, sha, result))
        for runs in self.checks.values():
            for run in runs:
                if run.get("id") == check_id:
                    run["head_sha"] = sha
                    run["status"] = "completed"
                    run["conclusion"] = "success" if result.passed else "failure"

    def list_issue_comments(self, repository: str, number: int) -> list[dict]:
        return self.comment_records.get(number, [])

    def create_issue_comment(self, repository: str, number: int, body: str) -> None:
        self.comments.append((number, body))
        self.comment_records.setdefault(number, []).append({"body": body})


class TestIssueStateGateReconciliation(unittest.TestCase):
    def test_read_only_lists_missing_stale_and_wrong_source_results_with_expected_decision(self):
        pulls = [
            pull(10, "open", "a" * 40, "## Implementation issues\n- #77"),
            pull(11, "open", "b" * 40, "## Implementation issues\n- #77"),
            pull(12, "open", "c" * 40, "## Implementation issues\n- #77"),
        ]
        client = FakeClient(pulls, {
            11: [{"id": 51, "name": "Issue State Gate", "head_sha": "z" * 40, "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}}],
            12: [{"id": 52, "name": "Issue State Gate", "head_sha": "c" * 40, "status": "completed", "conclusion": "success", "app": {"slug": "external-app"}}],
        })

        result = reconcile_issue_state_gate(client, REPOSITORY, apply=False)

        self.assertEqual(result.exit_code, 0)
        self.assertEqual([(row.pull_number, row.status, row.expected_decision) for row in result.rows], [
            (10, "absent", "success"), (11, "wrong-head", "success"), (12, "wrong-source", "success")
        ])
        self.assertEqual(client.published, [])
        self.assertEqual(client.comments, [])

    def test_apply_updates_existing_check_idempotently_and_uses_live_head(self):
        pr = pull(10, "open", "a" * 40, "## Implementation issues\n- #77")
        client = FakeClient([pr], {10: [{"id": 52, "name": "Issue State Gate", "head_sha": "a" * 40, "status": "completed", "conclusion": "failure", "app": {"slug": "github-actions"}}]})

        first = reconcile_issue_state_gate(client, REPOSITORY, apply=True)
        second = reconcile_issue_state_gate(client, REPOSITORY, apply=True)

        self.assertEqual(first.rows[0].status, "refreshed")
        self.assertEqual(second.rows[0].status, "current")
        self.assertEqual([entry[0] for entry in client.published], [52])
        self.assertTrue(all(entry[2].passed for entry in client.published))

    def test_current_head_advance_revalidates_and_refreshes_the_new_head(self):
        listed = pull(10, "open", "a" * 40, "## Implementation issues\n- #77")
        client = FakeClient([listed])
        client.fetch_pull_request = lambda repository, number: pull(10, "open", "b" * 40, "## Implementation issues\n- #77")

        result = reconcile_issue_state_gate(client, REPOSITORY, apply=True)

        self.assertEqual(result.rows[0].head_sha, "b" * 40)
        self.assertEqual(result.rows[0].status, "refreshed")
        self.assertEqual(client.published[0][1], "b" * 40)

    def test_closed_unmerged_pr_notifies_closed_issues_without_changing_state(self):
        abandoned = pull(20, "closed", "d" * 40, "## Implementation issues\n- #77")
        client = FakeClient([abandoned])

        result = reconcile_issue_state_gate(client, REPOSITORY, apply=True)

        self.assertEqual(len(client.comments), 1)
        self.assertEqual(client.comments[0][0], 77)
        for option in ("reopen", "reassign", "local-only"):
            self.assertIn(option, client.comments[0][1])
        self.assertEqual(client.issues[77]["state"], "closed")
        self.assertEqual(result.recovery_notices, 1)
        replay = reconcile_issue_state_gate(client, REPOSITORY, apply=True)
        self.assertEqual(len(client.comments), 1)
        self.assertEqual(replay.recovery_notices, 0)

    def test_unrelated_or_merged_closed_pull_requests_do_not_notify_issues(self):
        unrelated = pull(20, "closed", "d" * 40, "## Implementation issues\n- #77", merged=False)
        unrelated["base"] = {"ref": "develop", "repo": {"full_name": REPOSITORY}}
        merged = pull(21, "closed", "e" * 40, "## Implementation issues\n- #77", merged=True)
        client = FakeClient([unrelated, merged])

        result = reconcile_issue_state_gate(client, REPOSITORY, apply=True)

        self.assertEqual(client.comments, [])
        self.assertEqual(result.recovery_notices, 0)

    def test_rest_list_merged_at_prevents_false_abandonment_notice(self):
        merged = pull(21, "closed", "e" * 40, "## Implementation issues\n- #77")
        merged.pop("merged")  # REST /pulls list response omits this detail field.
        merged["merged_at"] = "2026-10-12T10:00:00Z"
        client = FakeClient([merged])

        result = reconcile_issue_state_gate(client, REPOSITORY, apply=True)

        self.assertEqual(result.recovery_notices, 0)
        self.assertEqual(client.comments, [])

    def test_apply_does_not_update_a_check_owned_by_another_app(self):
        pr = pull(10, "open", "a" * 40, "## Implementation issues\n- #77")
        client = FakeClient([pr], {10: [{"id": 52, "name": "Issue State Gate", "head_sha": "a" * 40, "status": "completed", "conclusion": "failure", "app": {"slug": "external-app"}}]})

        result = reconcile_issue_state_gate(client, REPOSITORY, apply=True)

        self.assertEqual(result.rows[0].status, "refreshed")
        self.assertEqual(client.published[0][0], 0)
        self.assertEqual(client.checks[10][0]["app"]["slug"], "external-app")

    def test_partial_api_errors_do_not_prevent_other_pull_requests_from_refreshing(self):
        broken = pull(10, "open", "a" * 40, "## Implementation issues\n- #77")
        good = pull(11, "open", "b" * 40, "## Implementation issues\n- #77")
        client = FakeClient([broken, good])
        original_fetch = client.fetch_pull_request

        def fetch(repository: str, number: int) -> dict:
            if number == 10:
                raise RuntimeError("temporary failure")
            return original_fetch(repository, number)

        client.fetch_pull_request = fetch
        result = reconcile_issue_state_gate(client, REPOSITORY, apply=True)

        self.assertEqual(result.exit_code, 2)
        self.assertEqual([entry[1] for entry in client.published], ["b" * 40])


if __name__ == "__main__":
    unittest.main()
