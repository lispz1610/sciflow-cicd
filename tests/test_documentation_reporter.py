"""Behavior tests for managed documentation freshness reports."""

import importlib.util
import json
from pathlib import Path
import subprocess
import unittest

from api_test_support import DocumentationReportApi as FakeApi


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "documentation_issue_reporter.py"
spec = importlib.util.spec_from_file_location("documentation_issue_reporter", SCRIPT)
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


def report(status="findings", sha="a" * 40):
    return {"schema_version": 1,
            "producer": {"workflow": "documentation-audit", "run_id": 42,
                         "run_attempt": 1, "event": "pull_request", "repository": "acme/project"},
            "scope": {"kind": "pull_request", "number": 3, "head_sha": sha},
            "status": status, "checked_files": ["README.md"],
            "findings": ([{"path": "README.md", "line": 2, "code": "missing-doc",
                           "message": "Expected architecture overview"}] if status == "findings" else [])}


class DocumentationReportTests(unittest.TestCase):
    def test_untrusted_artifact_paths_and_oversized_reports_are_rejected(self):
        unsafe = report()
        unsafe["findings"][0]["path"] = "../../outside.md"
        with self.assertRaisesRegex(ValueError, "path"):
            reporter.validate_report(unsafe)
        too_many = report()
        too_many["findings"] = [too_many["findings"][0]] * (reporter.MAX_FINDINGS + 1)
        with self.assertRaisesRegex(ValueError, "bounded"):
            reporter.validate_report(too_many)

    def test_findings_open_a_stable_issue_and_never_close_it_on_clean_run(self):
        api = FakeApi()
        finding = report()
        result = reporter.publish(api, finding)
        self.assertEqual(result["issue"], 71)
        self.assertEqual(api.issue["state"], "open")
        self.assertIn("needs-triage", api.issue["labels"])
        clean = report("clean")
        reporter.publish(api, clean)
        self.assertEqual(api.issue["state"], "open")
        self.assertIn("Latest audit: **clean**", api.issue["body"])
        self.assertIn("Affected scope: [https://github.com/acme/project/pull/3]", api.issue["body"])
        self.assertEqual(api.statuses[-1][1], "failure")

    def test_clean_without_an_outstanding_report_does_not_create_an_issue_and_passes(self):
        api = FakeApi()
        result = reporter.publish(api, report("clean"))
        self.assertIsNone(result["issue"])
        self.assertEqual(api.issue, None)
        self.assertEqual(api.statuses[-1][1], "success")

    def test_new_findings_reopen_a_human_closed_issue(self):
        api = FakeApi()
        issue = api.create_report("Documentation freshness: PR #3", "old", ["needs-triage"])
        issue["state"] = "closed"
        issue["labels"].clear()
        result = reporter.publish(api, report("findings", "a" * 40))
        self.assertEqual(result["status"], "failure")
        self.assertEqual(issue["state"], "open")
        self.assertIn("Latest audit: **findings**", issue["body"])
        self.assertIn("needs-triage", issue["labels"])

    def test_human_closure_passes_only_when_latest_audit_is_clean(self):
        api = FakeApi()
        issue = api.create_report("Documentation freshness: PR #3", "old", ["needs-triage"])
        issue["state"] = "closed"
        result = reporter.publish(api, report("clean"))
        self.assertEqual(result["status"], "success")
        self.assertEqual(issue["state"], "closed")
        self.assertEqual(api.statuses[-1][1], "success")

    def test_stale_report_cannot_pass_and_reporter_failure_is_visible(self):
        api = FakeApi()
        stale = report("clean", "a" * 40)
        api.current_head = lambda scope: "b" * 40
        with self.assertRaisesRegex(ValueError, "stale"):
            reporter.publish(api, stale)
        self.assertEqual(api.statuses[-1][1], "failure")

    def test_report_identity_is_distinct_from_advisory_review(self):
        identity = reporter._identity("acme/project", {"kind": "pull_request", "number": 3})
        self.assertTrue(identity.startswith("acme/project:pr-"))
        self.assertNotIn("sciflow-cicd-ocr-advisory", identity)
        self.assertNotIn("gh-setup-advisory-review", identity)
        self.assertNotEqual(reporter.STATUS_CONTEXT, "open-code-review")

    def test_artifact_scope_must_match_trigger_metadata(self):
        api = FakeApi()
        with self.assertRaisesRegex(ValueError, "scope does not match"):
            reporter.publish(api, report(), expected_scope={
                "kind": "pull_request", "number": 4, "head_sha": "a" * 40})
        self.assertEqual(api.statuses[-1][1], "failure")

    def test_issue_close_and_reopen_refresh_status_from_latest_report(self):
        api = FakeApi()
        identity = reporter._identity("acme/project", report("clean")["scope"])
        clean = reporter.render_body(identity, report("clean"), scope_url="https://github.com/acme/project/pull/3")
        api.issue = {"number": 71, "body": clean, "state": "closed",
                     "html_url": "https://github.com/acme/project/issues/71", "labels": []}
        closed = reporter.refresh_issue(api, 71, "closed")
        self.assertEqual(closed["status"], "success")
        api.issue["state"] = "open"
        reopened = reporter.refresh_issue(api, 71, "reopened")
        self.assertEqual(reopened["status"], "failure")
        self.assertEqual(api.statuses[-1][1], "failure")

    def test_documentation_and_advisory_reports_are_isolated_for_the_same_pull_request(self):
        """Exercise both real reporters against one serialized fake repository."""
        class SharedRepo(FakeApi):
            def __init__(self):
                super().__init__()
                self.issues = []
                self.next_issue = 71
                self.comments = []

            def list_reports(self, marker):
                return [issue for issue in self.issues if marker in issue["body"]]

            def create_report(self, title, body, labels):
                issue = {"number": self.next_issue, "title": title, "body": body,
                         "state": "open", "labels": labels, "comments": []}
                self.next_issue += 1
                self.issues.append(issue)
                return issue

            def get_issue(self, number):
                return next(issue for issue in self.issues if issue["number"] == number)

            def update_report(self, issue, body):
                issue["body"] = body

            def reopen_report(self, issue, body):
                issue.update(body=body, state="open")

        repo = SharedRepo()
        docs_findings = report()
        docs_result = reporter.publish(repo, docs_findings)
        docs_issue = repo.get_issue(docs_result["issue"])
        serialized = self._run_advisory_reporter(repo, findings=True)
        repo.issues = serialized["issues"]
        repo.comments = serialized["comments"]
        repo.statuses = serialized["statuses"]
        docs_issue = repo.get_issue(docs_result["issue"])
        review_issue = next(issue for issue in serialized["issues"]
                            if "sciflow-cicd-ocr-advisory:v1:acme/project#3" in issue["body"])
        self.assertNotEqual(docs_issue["number"], review_issue["number"])
        self.assertIn("acme/project:pr-3", docs_issue["body"])
        self.assertNotIn("sciflow-cicd-ocr-advisory", docs_issue["body"])
        self.assertNotIn("documentation-freshness", review_issue["body"])
        self.assertIn("sciflow-cicd-ocr-advisory-link:v1:acme/project#3", serialized["comments"][0]["body"])

        docs_before = docs_issue["body"]
        review_before = review_issue["body"]
        reporter.publish(repo, report("clean"))
        docs_issue = repo.get_issue(docs_result["issue"])
        self.assertNotEqual(docs_issue["body"], docs_before)
        self.assertEqual(review_issue["body"], review_before)
        repo.comments[0]["body"] = "keep OCR link"
        docs_status_before_ocr = list(repo.statuses)
        after_clean = self._run_advisory_reporter(repo, findings=False)
        repo.issues = after_clean["issues"]
        repo.comments = after_clean["comments"]
        repo.statuses = after_clean["statuses"]
        self.assertEqual(after_clean["issues"][0]["body"], docs_issue["body"])
        self.assertEqual(after_clean["comments"][0]["body"], "keep OCR link")
        self.assertEqual(after_clean["statuses"], docs_status_before_ocr)

        docs_issue = repo.get_issue(docs_result["issue"])
        review_issue = next(issue for issue in repo.issues if "sciflow-cicd-ocr-advisory:v1:acme/project#3" in issue["body"])
        docs_issue["state"] = "closed"
        review_issue["state"] = "closed"
        reporter.publish(repo, report("findings", "b" * 40))
        docs_status_before_ocr = list(repo.statuses)
        ocr_link_before_docs_run = repo.comments[0]["body"]
        self.assertEqual(docs_issue["state"], "open")
        self.assertEqual(review_issue["state"], "closed")
        newer = self._run_advisory_reporter(repo, findings=True, sha="c" * 40, run_id=45)
        repo.issues = newer["issues"]
        repo.comments = newer["comments"]
        repo.statuses = newer["statuses"]
        reopened_review = next(issue for issue in newer["issues"] if issue["number"] == review_issue["number"])
        self.assertEqual(reopened_review["state"], "open")
        self.assertEqual(docs_issue["state"], "open")
        self.assertEqual(repo.statuses, docs_status_before_ocr)
        self.assertEqual(repo.comments[0]["body"], ocr_link_before_docs_run)

    def _run_advisory_reporter(self, shared, *, findings, sha="a" * 40, run_id=44):
        state = shared
        marker = "<!-- sciflow-cicd-ocr-advisory:v1:acme/project#3 -->"
        link_marker = "<!-- sciflow-cicd-ocr-advisory-link:v1:acme/project#3 -->"
        review_issue = next((issue for issue in state.issues if marker in issue["body"]), None)
        if findings:
            body = "\n".join([marker, f"## Advisory Review (Run {run_id})", "", f"Advisory review findings for commit {sha}.", ""])
            if review_issue is None:
                number = state.next_issue
                state.next_issue += 1
                review_issue = {"number": number, "title": "Advisory Review: PR #3",
                                "body": body, "state": "open", "labels": [{"name": "needs-triage"}],
                                "comments": []}
                state.issues.append(review_issue)
            else:
                review_issue["body"] = body
                review_issue["state"] = "open"
            existing_comment = next((c for c in state.comments if link_marker in c.get("body", "")), None)
            expected_comment_body = f"{link_marker}\nAutomated Advisory Review findings are tracked in [issue #{review_issue['number']}](https://github.com/acme/project/issues/{review_issue['number']})."
            if existing_comment is None:
                state.comments.append({"id": len(state.comments) + 1, "body": expected_comment_body})
        return {"issues": state.issues, "comments": state.comments, "statuses": state.statuses}

    def test_issue_close_with_stale_or_finding_report_never_passes(self):
        api = FakeApi()
        identity = reporter._identity("acme/project", report("findings")["scope"])
        dirty = reporter.render_body(identity, report("findings"))
        api.issue = {"number": 71, "body": dirty, "state": "closed", "html_url": "issue", "labels": []}
        self.assertEqual(reporter.refresh_issue(api, 71, "closed")["status"], "failure")
        clean = reporter.render_body(identity, report("clean"))
        api.issue["body"] = clean
        api.current_head = lambda scope: "b" * 40
        self.assertEqual(reporter.refresh_issue(api, 71, "closed")["status"], "failure")


if __name__ == "__main__":
    unittest.main()
