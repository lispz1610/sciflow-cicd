"""Workflow contract tests for documentation audit and issue reporting."""

from pathlib import Path
import re
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/documentation-freshness.yml"


class DocumentationWorkflowTests(unittest.TestCase):
    def test_pr_validation_is_read_only_and_reporter_permissions_are_isolated(self):
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        jobs = workflow["jobs"]
        pr_guard = "github.event.pull_request.head.repo.full_name == github.repository"
        self.assertIn(pr_guard, jobs["audit"]["if"])
        self.assertIn(pr_guard, jobs["report"]["if"])
        self.assertIn("github.event_name == 'push'", jobs["audit"]["if"])
        self.assertIn("github.event_name == 'workflow_dispatch'", jobs["audit"]["if"])
        self.assertNotIn("github.event_name == 'issues'", jobs["audit"]["if"])
        self.assertEqual(jobs["audit"]["permissions"], {"contents": "read", "pull-requests": "read"})
        self.assertEqual(jobs["report"]["permissions"], {
            "actions": "read", "contents": "read", "issues": "write",
            "pull-requests": "read", "statuses": "write"})
        self.assertIn("pull_request_target", workflow["on"] if "on" in workflow else workflow[True])
        self.assertIn("workflow_dispatch", workflow["on"] if "on" in workflow else workflow[True])
        self.assertEqual((workflow["on"] if "on" in workflow else workflow[True])["issues"]["types"],
                         ["closed", "reopened"])
        self.assertEqual(jobs["refresh"]["permissions"], {
            "contents": "read", "issues": "read", "pull-requests": "read", "statuses": "write"})
        self.assertIn("--refresh-issue", "\n".join(step.get("run", "") for step in jobs["refresh"]["steps"]))
        rendered = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("--changed-files changed-files.json", rendered)
        self.assertIn("DOCS_TARGET_MODE: \"false\"", rendered)
        self.assertNotIn("continue-on-error", rendered)

    def test_actions_are_pinned_and_target_markdown_is_treated_as_data(self):
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], r"@[0-9a-f]{40}(?:\s|$)")
        rendered = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Check out target revision as data", rendered)
        self.assertIn("persist-credentials: false", rendered)
        self.assertIn("retention-days: 2", rendered)


if __name__ == "__main__":
    unittest.main()
