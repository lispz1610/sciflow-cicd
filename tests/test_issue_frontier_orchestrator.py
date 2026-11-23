"""Unit tests for the issue DAG frontier orchestrator and blocker transition engine."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from scripts.orchestrate_issue_frontier import (
    BlockerRecord,
    FrontierTransition,
    GitHubApiError,
    GitHubClient,
    IssueFrontierOrchestrator,
    _extract_issue_references,
    detect_cycles,
    format_wayfinder_comment,
    has_wayfinder_comment,
    main,
    parse_blocked_by,
    parse_blocking,
)


class TestMarkdownBlockerParsing(unittest.TestCase):
    """Verify parsing of blocker references from issue bodies."""

    def test_parse_blocked_by_standard_bullet_list(self):
        body = (
            "## What to build\n"
            "Build this feature.\n\n"
            "## Blocked by\n\n"
            "- #71\n"
            "* #72\n"
            "• #73\n"
        )
        self.assertEqual(parse_blocked_by(body), [71, 72, 73])

    def test_parse_blocked_by_urls_and_qualified_references(self):
        body = (
            "## Blocked by\n"
            "- lispz1610/sciflow-cicd#101\n"
            "- https://github.com/lispz1610/sciflow-cicd/issues/102\n"
            "- other-org/other-repo#999\n"
        )
        # Qualified references for the same repo or issue URLs should be extracted
        blockers = parse_blocked_by(body, repo="lispz1610/sciflow-cicd")
        self.assertEqual(blockers, [101, 102])

    def test_parse_blocked_by_inline_colon_syntax(self):
        body = (
            "Parent: #69\n"
            "Blocked by: #40, #41\n"
            "Blocking: #45\n"
        )
        self.assertEqual(parse_blocked_by(body), [40, 41])

    def test_parse_blocked_by_empty_or_none(self):
        self.assertEqual(parse_blocked_by(""), [])
        self.assertEqual(parse_blocked_by("## Blocked by\n\nNone"), [])
        self.assertEqual(parse_blocked_by("## Blocked by\n\nN/A"), [])

    def test_parse_blocked_by_malformed_references(self):
        body = (
            "## Blocked by\n"
            "- #\n"
            "- #abc\n"
            "- #0\n"
            "- #-5\n"
            "- #50\n"
        )
        self.assertEqual(parse_blocked_by(body), [50])

    def test_parse_blocking_standard_references(self):
        body = (
            "## Parent\n#69\n\n"
            "## Blocking\n- #73\n- #74\n"
        )
        self.assertEqual(parse_blocking(body), [73, 74])

    def test_parse_blocking_inline_reference(self):
        body = "Blocking: lispz1610/sciflow-cicd#73\n"
        self.assertEqual(parse_blocking(body, repo="lispz1610/sciflow-cicd"), [73])


class TestCycleDetection(unittest.TestCase):
    """Verify cyclic dependency detection in issue dependency graphs."""

    def test_acyclic_graph_reports_no_cycles(self):
        graph = {
            1: [],
            2: [1],
            3: [1],
            4: [2, 3],
        }
        cycles = detect_cycles(graph)
        self.assertEqual(cycles, [])

    def test_detect_direct_mutual_cycle(self):
        graph = {
            1: [2],
            2: [1],
        }
        cycles = detect_cycles(graph)
        self.assertTrue(len(cycles) > 0)
        # Cycle contains 1 and 2
        flat = {node for cycle in cycles for node in cycle}
        self.assertIn(1, flat)
        self.assertIn(2, flat)

    def test_detect_three_node_cycle(self):
        graph = {
            1: [2],
            2: [3],
            3: [1],
        }
        cycles = detect_cycles(graph)
        self.assertTrue(len(cycles) > 0)
        flat = {node for cycle in cycles for node in cycle}
        self.assertEqual(flat, {1, 2, 3})

    def test_detect_self_loop(self):
        graph = {
            1: [1],
        }
        cycles = detect_cycles(graph)
        self.assertTrue(len(cycles) > 0)
        self.assertIn(1, cycles[0])


class MockGitHubClient:
    """In-memory mock for GitHubClient to simulate API responses and track calls."""

    def __init__(
        self,
        issues: dict[int, dict] | None = None,
        graphql_data: dict[int, dict] | None = None,
        comments: dict[int, list[dict]] | None = None,
    ):
        self.issues = issues or {}
        self.graphql_data = graphql_data or {}
        self.comments = comments or {}
        self.added_labels: list[tuple[int, list[str]]] = []
        self.posted_comments: list[tuple[int, str]] = []
        self.graphql_available = True

    def get_issue(self, number: int) -> dict:
        if number not in self.issues:
            raise GitHubApiError(f"Issue #{number} not found (404)")
        return self.issues[number]

    def list_open_issues(self) -> list[dict]:
        return [
            issue for issue in self.issues.values()
            if issue.get("state", "").lower() == "open"
        ]

    def list_comments(self, number: int) -> list[dict]:
        existing = list(self.comments.get(number, []))
        posted = [{"body": body} for num, body in self.posted_comments if num == number]
        return existing + posted

    def get_issue_dependencies_graphql(self, number: int) -> dict | None:
        if not self.graphql_available:
            return None
        return self.graphql_data.get(number)

    def add_labels(self, number: int, labels: list[str]) -> None:
        self.added_labels.append((number, labels))
        if number in self.issues:
            current_labels = self.issues[number].setdefault("labels", [])
            for lbl in labels:
                if not any(isinstance(l, dict) and l.get("name") == lbl for l in current_labels):
                    current_labels.append({"name": lbl})

    def add_comment(self, number: int, body: str) -> None:
        self.posted_comments.append((number, body))


class TestFrontierOrchestration(unittest.TestCase):
    """Test DAG frontier transitions under various topologies."""

    def setUp(self):
        self.orchestrator = IssueFrontierOrchestrator(repository="lispz1610/sciflow-cicd")

    def test_single_blocker_resolution_transitions_issue(self):
        # Issue 71 is closed. Issue 72 is blocked solely by #71.
        mock_issues = {
            71: {
                "number": 71,
                "state": "closed",
                "title": "Blocker task",
                "body": "## Blocking\n- #72",
                "labels": [],
            },
            72: {
                "number": 72,
                "state": "open",
                "title": "Dependent task",
                "body": "## Blocked by\n- #71",
                "labels": [],
            },
        }
        client = MockGitHubClient(issues=mock_issues)

        transitions = self.orchestrator.evaluate_closed_issue(71, client)

        self.assertEqual(len(transitions), 1)
        transition = transitions[0]
        self.assertEqual(transition["issue_number"], 72)
        self.assertEqual(transition["resolved_blockers"], [71])
        self.assertIn("ready-for-agent", transition["labels_added"])

        # Check client calls
        self.assertEqual(len(client.added_labels), 1)
        self.assertEqual(client.added_labels[0], (72, ["ready-for-agent"]))
        self.assertEqual(len(client.posted_comments), 1)
        self.assertEqual(client.posted_comments[0][0], 72)
        self.assertIn("All blockers resolved ([#71])", client.posted_comments[0][1])

    def test_multi_blocker_convergence_all_closed(self):
        # Issue 73 is blocked by #71 and #72.
        # #71 was already closed earlier. #72 is closed now.
        mock_issues = {
            71: {
                "number": 71,
                "state": "closed",
                "title": "First blocker",
                "body": "",
                "labels": [],
            },
            72: {
                "number": 72,
                "state": "closed",
                "title": "Second blocker",
                "body": "## Blocking\n- #73",
                "labels": [],
            },
            73: {
                "number": 73,
                "state": "open",
                "title": "Target task",
                "body": "## Blocked by\n- #71\n- #72",
                "labels": [],
            },
        }
        client = MockGitHubClient(issues=mock_issues)

        transitions = self.orchestrator.evaluate_closed_issue(72, client)

        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["issue_number"], 73)
        self.assertEqual(transitions[0]["resolved_blockers"], [71, 72])
        self.assertIn("All blockers resolved ([#71, #72])", client.posted_comments[0][1])

    def test_partially_blocked_is_noop(self):
        # Issue 73 is blocked by #71 and #72.
        # #71 closes, but #72 is still open.
        mock_issues = {
            71: {
                "number": 71,
                "state": "closed",
                "title": "First blocker",
                "body": "",
                "labels": [],
            },
            72: {
                "number": 72,
                "state": "open",
                "title": "Second blocker",
                "body": "",
                "labels": [],
            },
            73: {
                "number": 73,
                "state": "open",
                "title": "Target task",
                "body": "## Blocked by\n- #71\n- #72",
                "labels": [],
            },
        }
        client = MockGitHubClient(issues=mock_issues)

        transitions = self.orchestrator.evaluate_closed_issue(71, client)

        self.assertEqual(len(transitions), 0)
        self.assertEqual(len(client.added_labels), 0)
        self.assertEqual(len(client.posted_comments), 0)

    def test_diamond_dag_evaluation(self):
        # 10 is closed.
        # 20 blocked by [10].
        # 30 blocked by [10].
        # 40 blocked by [20, 30].
        mock_issues = {
            10: {"number": 10, "state": "closed", "body": "## Blocking\n- #20\n- #30", "labels": []},
            20: {"number": 20, "state": "open", "body": "## Blocked by\n- #10\n## Blocking\n- #40", "labels": []},
            30: {"number": 30, "state": "open", "body": "## Blocked by\n- #10\n## Blocking\n- #40", "labels": []},
            40: {"number": 40, "state": "open", "body": "## Blocked by\n- #20\n- #30", "labels": []},
        }
        client = MockGitHubClient(issues=mock_issues)

        transitions = self.orchestrator.evaluate_closed_issue(10, client)

        transitioned_numbers = {t["issue_number"] for t in transitions}
        self.assertEqual(transitioned_numbers, {20, 30})
        self.assertNotIn(40, transitioned_numbers)

    def test_issue_already_having_ready_for_agent_label(self):
        # Issue 72 already has label 'ready-for-agent'.
        # Label should not be re-added, but wayfinder comment is still posted.
        mock_issues = {
            71: {"number": 71, "state": "closed", "body": "", "labels": []},
            72: {
                "number": 72,
                "state": "open",
                "body": "## Blocked by\n- #71",
                "labels": [{"name": "ready-for-agent"}],
            },
        }
        client = MockGitHubClient(issues=mock_issues)

        transitions = self.orchestrator.evaluate_closed_issue(71, client)

        self.assertEqual(len(transitions), 1)
        self.assertEqual(client.added_labels, [])  # No label add call needed
        self.assertEqual(len(client.posted_comments), 1)
        self.assertEqual(client.posted_comments[0][0], 72)

    def test_blocker_not_found_treated_as_unresolved(self):
        # Issue 72 references blocker #999 which does not exist (404)
        mock_issues = {
            71: {"number": 71, "state": "closed", "body": "", "labels": []},
            72: {
                "number": 72,
                "state": "open",
                "body": "## Blocked by\n- #71\n- #999",
                "labels": [],
            },
        }
        client = MockGitHubClient(issues=mock_issues)

        with self.assertLogs("scripts.orchestrate_issue_frontier", level="WARNING") as cm:
            transitions = self.orchestrator.evaluate_closed_issue(71, client)

        self.assertEqual(transitions, [])
        self.assertEqual(len(client.added_labels), 0)
        self.assertTrue(any("999" in log for log in cm.output))

    def test_cycle_detection_prevents_transition_and_logs_warning(self):
        # Issue 20 is blocked by 10 and 30.
        # Issue 30 is blocked by 20 (cycle between 20 and 30).
        mock_issues = {
            10: {"number": 10, "state": "closed", "body": "", "labels": []},
            20: {"number": 20, "state": "open", "body": "## Blocked by\n- #10\n- #30", "labels": []},
            30: {"number": 30, "state": "open", "body": "## Blocked by\n- #20", "labels": []},
        }
        client = MockGitHubClient(issues=mock_issues)

        with self.assertLogs("scripts.orchestrate_issue_frontier", level="WARNING") as cm:
            transitions = self.orchestrator.evaluate_closed_issue(10, client)

        self.assertEqual(transitions, [])
        self.assertTrue(any("Cyclic dependency" in log for log in cm.output))

    def test_graphql_native_blocking_and_blocked_by_support(self):
        # GraphQL returns blocking and blockedBy structures
        mock_issues = {
            71: {"number": 71, "state": "closed", "body": "", "labels": []},
            72: {"number": 72, "state": "open", "body": "", "labels": []},
        }
        mock_graphql = {
            71: {
                "number": 71,
                "state": "CLOSED",
                "blocking": {"nodes": [{"number": 72, "state": "OPEN"}]},
            },
            72: {
                "number": 72,
                "state": "OPEN",
                "blockedBy": {"nodes": [{"number": 71, "state": "CLOSED"}]},
            },
        }
        client = MockGitHubClient(issues=mock_issues, graphql_data=mock_graphql)

        transitions = self.orchestrator.evaluate_closed_issue(71, client)

        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["issue_number"], 72)
        self.assertEqual(transitions[0]["resolved_blockers"], [71])

    def test_graphql_fallback_to_markdown_when_graphql_unavailable(self):
        mock_issues = {
            71: {"number": 71, "state": "closed", "body": "## Blocking\n- #72", "labels": []},
            72: {"number": 72, "state": "open", "body": "## Blocked by\n- #71", "labels": []},
        }
        client = MockGitHubClient(issues=mock_issues)
        client.graphql_available = False

        transitions = self.orchestrator.evaluate_closed_issue(71, client)

        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["issue_number"], 72)

    def test_dry_run_does_not_mutate_state(self):
        mock_issues = {
            71: {"number": 71, "state": "closed", "body": "## Blocking\n- #72", "labels": []},
            72: {"number": 72, "state": "open", "body": "## Blocked by\n- #71", "labels": []},
        }
        client = MockGitHubClient(issues=mock_issues)

        transitions = self.orchestrator.evaluate_closed_issue(71, client, dry_run=True)

        self.assertEqual(len(transitions), 1)
        self.assertEqual(len(client.added_labels), 0)
        self.assertEqual(len(client.posted_comments), 0)

    def test_duplicate_wayfinder_comment_suppression_when_comment_already_exists(self):
        mock_issues = {
            71: {"number": 71, "state": "closed", "body": "## Blocking\n- #72", "labels": []},
            72: {"number": 72, "state": "open", "body": "## Blocked by\n- #71", "labels": []},
        }
        mock_comments = {
            72: [{"body": "🚀 All blockers resolved ([#71]). This ticket is unblocked and now on the active frontier ready for agent implementation."}]
        }
        client = MockGitHubClient(issues=mock_issues, comments=mock_comments)

        transitions = self.orchestrator.evaluate_closed_issue(71, client)

        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0].issue_number, 72)
        # Label is still added
        self.assertEqual(len(client.added_labels), 1)
        self.assertEqual(client.added_labels[0], (72, ["ready-for-agent"]))
        # But comment was NOT added (suppressed)
        self.assertEqual(len(client.posted_comments), 0)
        self.assertIsNone(transitions[0].comment)

    def test_duplicate_wayfinder_comment_suppression_with_wayfinder_frontier_tag(self):
        mock_issues = {
            71: {"number": 71, "state": "closed", "body": "## Blocking\n- #72", "labels": [{"name": "ready-for-agent"}]},
            72: {"number": 72, "state": "open", "body": "## Blocked by\n- #71", "labels": [{"name": "ready-for-agent"}]},
        }
        mock_comments = {
            72: [{"body": "<!-- wayfinder:frontier --> Ticket unblocked"}]
        }
        client = MockGitHubClient(issues=mock_issues, comments=mock_comments)

        transitions = self.orchestrator.evaluate_closed_issue(71, client)

        self.assertEqual(len(transitions), 1)
        self.assertEqual(len(client.added_labels), 0)
        self.assertEqual(len(client.posted_comments), 0)


class TestWayfinderCommentFormatting(unittest.TestCase):
    def test_formats_single_and_multiple_blockers(self):
        c1 = format_wayfinder_comment([71])
        self.assertIn("🚀 All blockers resolved ([#71])", c1)
        self.assertIn("active frontier ready for agent implementation", c1)

        c2 = format_wayfinder_comment([70, 71])
        self.assertIn("🚀 All blockers resolved ([#70, #71])", c2)


class TestCliAndEventHandling(unittest.TestCase):
    """Test CLI behavior, event parsing, and environment variables."""

    def test_non_closed_event_exits_clean_noop(self):
        payload = {"action": "opened", "issue": {"number": 71}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(payload, f)
            event_path = f.name

        exit_code = main(["--event", event_path, "--dry-run"])
        self.assertEqual(exit_code, 0)
        Path(event_path).unlink()

    def test_pull_request_event_exits_clean_noop(self):
        payload = {"action": "closed", "issue": {"number": 71, "pull_request": {}}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(payload, f)
            event_path = f.name

        exit_code = main(["--event", event_path, "--dry-run"])
        self.assertEqual(exit_code, 0)
        Path(event_path).unlink()

    def test_missing_event_path_exits_2(self):
        with patch.dict("os.environ", {}, clear=True):
            exit_code = main([])
            self.assertEqual(exit_code, 2)

    def test_missing_token_on_live_run_exits_1(self):
        payload = {
            "action": "closed",
            "issue": {"number": 71},
            "repository": {"full_name": "lispz1610/sciflow-cicd"},
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(payload, f)
            event_path = f.name

        with patch.dict("os.environ", {"GITHUB_TOKEN": ""}, clear=True):
            exit_code = main(["--event", event_path])
            self.assertEqual(exit_code, 1)

        Path(event_path).unlink()


class TestGitHubClientOperations(unittest.TestCase):
    """Test GitHubClient pagination and comment listing."""

    def test_list_open_issues_pagination(self):
        client = GitHubClient(token="token", repository="octo/widget")
        # Page 1 returning 100 items (99 issues, 1 PR)
        page1 = [{"number": i, "state": "open"} for i in range(1, 100)] + [{"number": 100, "pull_request": {}}]
        # Page 2 returning 2 items (<100, terminates loop)
        page2 = [{"number": 101, "state": "open"}, {"number": 102, "state": "open"}]

        calls = []

        def fake_request(url, **kwargs):
            calls.append(url)
            qs = parse_qs(urlparse(url).query)
            page_val = qs.get("page", ["1"])[0]
            if page_val == "1":
                return page1
            elif page_val == "2":
                return page2
            return []

        with patch.object(client, "_request", side_effect=fake_request):
            issues = client.list_open_issues()

        self.assertEqual(len(calls), 2)
        self.assertIn("page=1", calls[0])
        self.assertIn("page=2", calls[1])
        # 99 from page 1 + 2 from page 2 = 101 (PR on page 1 filtered out)
        self.assertEqual(len(issues), 101)
        self.assertEqual([i["number"] for i in issues[:3]], [1, 2, 3])
        self.assertEqual([i["number"] for i in issues[-2:]], [101, 102])

    def test_list_comments(self):
        client = GitHubClient(token="token", repository="octo/widget")
        expected = [{"id": 1, "body": "comment 1"}]
        with patch.object(client, "_request", return_value=expected) as mock_req:
            comments = client.list_comments(42)
        self.assertEqual(comments, expected)
        self.assertIn("/issues/42/comments", mock_req.call_args[0][0])


class TestDataclassesAndSharedParser(unittest.TestCase):
    """Test BlockerRecord, FrontierTransition, and _extract_issue_references."""

    def test_extract_issue_references_custom_keywords(self):
        body = (
            "## Depends on\n"
            "- #55\n"
            "Depends on: #56\n"
        )
        refs = _extract_issue_references(body, ["Depends on"])
        self.assertEqual(refs, [55, 56])

    def test_blocker_record_dataclass_properties(self):
        rec_unblocked = BlockerRecord(issue_number=10, blockers=[1, 2], unresolved_blockers=[])
        self.assertTrue(rec_unblocked.is_unblocked)

        rec_blocked = BlockerRecord(issue_number=10, blockers=[1, 2], unresolved_blockers=[1])
        self.assertFalse(rec_blocked.is_unblocked)

        rec_empty = BlockerRecord(issue_number=10, blockers=[], unresolved_blockers=[])
        self.assertFalse(rec_empty.is_unblocked)

    def test_frontier_transition_dataclass_and_dict_access(self):
        trans = FrontierTransition(
            issue_number=72,
            resolved_blockers=[70, 71],
            labels_added=["ready-for-agent"],
            comment="hello",
        )
        self.assertEqual(trans.issue_number, 72)
        self.assertEqual(trans["issue_number"], 72)
        self.assertEqual(trans["resolved_blockers"], [70, 71])
        as_d = trans.to_dict()
        self.assertEqual(as_d["issue_number"], 72)
        self.assertEqual(as_d["labels_added"], ["ready-for-agent"])

    def test_has_wayfinder_comment_detection(self):
        self.assertTrue(has_wayfinder_comment([{"body": "🚀 All blockers resolved ([#1])"}]))
        self.assertTrue(has_wayfinder_comment([{"body": "wayfinder:frontier comment"}]))
        self.assertFalse(has_wayfinder_comment([{"body": "just a normal comment"}]))
        self.assertFalse(has_wayfinder_comment([]))

    def test_list_comments_paginates_beyond_100_entries(self):
        client = GitHubClient("tok", repository="owner/repo")
        page1 = [{"id": i, "body": f"comment {i}"} for i in range(100)]
        page2 = [{"id": 100, "body": "wayfinder:frontier"}]
        requested_urls = []

        def fake_request(url, payload=None, method=None):
            requested_urls.append(url)
            if "&page=1" in url:
                return page1
            if "&page=2" in url:
                return page2
            return []

        client._request = fake_request
        comments = client.list_comments(42)
        self.assertEqual(len(comments), 101)
        self.assertTrue(any("&page=1" in u for u in requested_urls))
        self.assertTrue(any("&page=2" in u for u in requested_urls))

    def test_github_client_validates_and_quotes_repository(self):
        bad_client = GitHubClient("tok", repository="../etc")
        with self.assertRaises(ValueError):
            bad_client.get_issue(1)

    def test_github_client_rejects_unexpected_url_origins(self):
        from scripts.orchestrate_issue_frontier import GitHubApiError

        client = GitHubClient("tok", repository="owner/repo")
        for bad_url in (
            "https://evil.example.com/repos/owner/repo/issues/1",
            "https://api.github.com.evil.com/repos/owner/repo/issues/1",
            "//evil.example.com/repos/owner/repo/issues/1",
            "relative/without/leading/slash",
        ):
            with self.subTest(bad_url=bad_url):
                with self.assertRaises(GitHubApiError):
                    client._request(bad_url)

        with patch.object(client, "request_with_headers", return_value=(200, {}, {"number": 1})) as req:
            res = client._request("/repos/owner/repo/issues/1")
        self.assertEqual(res, {"number": 1})
        req.assert_called_once_with("GET", "/repos/owner/repo/issues/1", payload=None)


if __name__ == "__main__":
    unittest.main()
