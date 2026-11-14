"""Comprehensive audit of the unified Setup Pull Request workflow staging (Ticket #18)."""

import base64
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
TESTS_DIR = ROOT_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_install_github_workflows import (
    CHECKOUT,
    DEPENDENCY,
    WorkflowApi,
    installer,
)
from scripts.setup_github_repo import run_policy


def multi_workflow_policy():
    return {
        "version": 1,
        "workflows": [
            {
                "template": "pr-ci",
                "template_version": 1,
                "filename": "ci.yml",
                "triggers": ["pull_request"],
                "checkout_sha": CHECKOUT,
                "commands": ["python3 -m unittest discover -s tests"],
            },
            {
                "template": "dependency-review",
                "template_version": 1,
                "filename": "dependency-review.yml",
                "triggers": ["pull_request"],
                "checkout_sha": CHECKOUT,
                "action_sha": DEPENDENCY,
            },
        ],
    }


class WorkflowStagingAuditTests(unittest.TestCase):
    def test_unified_setup_pr_renders_bytes_pins_and_draft_state(self):
        """Unified setup PR stages multiple workflows in one draft PR on codex/workflows-*."""
        api = WorkflowApi()
        result = installer.install(api, "acme/project", multi_workflow_policy(), apply=True)

        self.assertTrue(result["pull_request"].startswith("https://github.com/acme/project/pull/"))
        self.assertTrue(result["branch"].startswith("codex/workflows-"))

        # Verify PR was created with draft=True
        self.assertEqual(len(api.prs), 1)
        created_pr = api.prs[0]
        self.assertTrue(created_pr["draft"])
        self.assertEqual(created_pr["base"]["ref"], "main")
        self.assertEqual(created_pr["head"]["ref"], result["branch"])

        # Verify files were written to the branch
        branch = result["branch"]
        self.assertIn((branch, ".github/workflows/ci.yml"), api.files)
        self.assertIn((branch, ".github/workflows/dependency-review.yml"), api.files)

    def test_exact_reuse_of_open_matching_setup_pr_issues_zero_writes(self):
        """Re-running with exact matching workflow bundle reuses existing PR with zero writes."""
        api = WorkflowApi()
        # First staging creates branch, files, and PR
        first = installer.install(api, "acme/project", multi_workflow_policy(), apply=True)
        self.assertTrue(len(api.writes) > 0)
        api.writes.clear()

        # Second staging with matching policy reuses the PR
        second = installer.install(api, "acme/project", multi_workflow_policy(), apply=True)
        self.assertEqual(second["pull_request"], first["pull_request"])
        self.assertEqual(second["branch"], first["branch"])
        self.assertEqual(api.writes, [], "Reusing matching open Setup PR must perform zero writes")

    def test_unmanaged_workflow_file_blocks_staging_without_writes(self):
        """Unmanaged workflow file on default branch blocks staging without writing."""
        api = WorkflowApi()
        # Pre-seed unmanaged ci.yml on main (without ownership marker)
        api.files[("main", ".github/workflows/ci.yml")] = (b"name: Custom Unmanaged CI\n", "sha123")

        with self.assertRaisesRegex(ValueError, "unmanaged"):
            installer.install(api, "acme/project", multi_workflow_policy(), apply=True)
        self.assertEqual(api.writes, [], "Unmanaged workflow must abort with zero writes")

    def test_drifted_managed_workflow_without_expected_sha_blocks_staging(self):
        """Managed workflow with mismatched blob SHA blocks staging when expected_sha is absent."""
        api = WorkflowApi()
        # Pre-seed managed ci.yml with marker but unexpected sha
        managed_header = installer.MARKER + "pr-ci version=1\n"
        api.files[("main", ".github/workflows/ci.yml")] = (
            managed_header.encode() + b"name: Drifted\n",
            "different-sha",
        )

        with self.assertRaisesRegex(ValueError, "expected_sha"):
            installer.install(api, "acme/project", multi_workflow_policy(), apply=True)
        self.assertEqual(api.writes, [], "Drifted managed workflow must abort with zero writes")

    def test_default_branch_head_movement_aborts_staging_safely(self):
        """If default branch head moves during staging, aborts without writing PR."""
        api = WorkflowApi()
        api.change_default_on_second_ref_read = True

        with self.assertRaisesRegex((ValueError, installer.ApiError), "default branch"):
            installer.install(api, "acme/project", multi_workflow_policy(), apply=True)
        # Verify no PR was created
        self.assertEqual(api.prs, [])


if __name__ == "__main__":
    unittest.main()
