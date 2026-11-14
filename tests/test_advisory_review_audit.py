"""Comprehensive audit of Review Setup and Advisory Review contracts (Ticket #19)."""

import io
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
TESTS_DIR = ROOT_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_review_setup import review_stage
from scripts.review_policy import validate_policy as validate_review
from scripts.review_workflow import render_workflow, WORKFLOW_PATH
from scripts.setup_github_repo import run_policy, validate_policy


class AdvisoryReviewAuditTests(unittest.TestCase):
    def test_secrets_and_tokens_are_never_in_policy_or_workflow_rendering(self):
        """Policy documents and rendered workflows must never embed secret values."""
        stage = review_stage()
        # Verify provider_key or secret value is rejected in policy
        stage_with_secret = review_stage()
        stage_with_secret["llm"]["provider_key"] = "sk-ant-test-secret"
        with self.assertRaisesRegex(ValueError, "llm must be an object with exactly"):
            validate_policy({"version": 2, "stages": [stage_with_secret]})

        # Rendered workflow references secret by name via secrets.OCR_LLM_AUTH_TOKEN
        workflow_bytes = render_workflow(validate_review(stage))
        workflow_text = workflow_bytes.decode("utf-8")
        self.assertIn("secrets.OCR_LLM_AUTH_TOKEN", workflow_text)
        self.assertNotIn("sk-ant-", workflow_text)
        self.assertNotIn("sk-proj-", workflow_text)

    def test_fork_prs_are_skipped_and_only_the_reporter_checks_out_trusted_base(self):
        """Advisory review uses pull_request_target on trusted base and skips forks."""
        workflow_bytes = render_workflow(validate_review(review_stage()))
        workflow_text = workflow_bytes.decode("utf-8")

        # 1. Triggered on pull_request_target (trusted base ref)
        self.assertIn("pull_request_target:", workflow_text)

        # 2. Skips forks so untrusted head code cannot execute with permissions
        self.assertIn(
            "github.event.pull_request.head.repo.full_name == github.repository",
            workflow_text,
        )

        # 3. The secret-bearing review job does not check out PR code. The
        # separate reporter job checks out the base with credentials disabled.
        review_job, report_job = workflow_text.split("  report:\n", 1)
        self.assertNotIn("actions/checkout@", review_job)
        self.assertIn("actions/checkout@", report_job)
        self.assertIn("persist-credentials: false", report_job)
        self.assertIn("issues: write", report_job)

        # 4. Permissions strictly read for contents, write for pull-requests comments
        self.assertIn("contents: read", workflow_text)
        self.assertIn("pull-requests: write", workflow_text)

    def test_runtime_configuration_guard_diagnostics_without_secret_leakage(self):
        """Configuration check step fails with clear ::error:: when secrets/vars missing."""
        workflow_bytes = render_workflow(validate_review(review_stage()))
        workflow_text = workflow_bytes.decode("utf-8")

        # Extract bash check script
        script = textwrap.dedent(
            workflow_text.split("        run: |\n", 1)[1].split("      - uses:", 1)[0]
        )

        # Execute script with sentinel token but missing URL
        test_env = {
            **os.environ,
            "OCR_TOKEN": "my-secret-token-sentinel",
            "OCR_URL": "",
            "OCR_MODEL": "review-model-v1",
            "OCR_PROTOCOL": "false",
        }
        proc = subprocess.run(["bash", "-c", script], env=test_env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("::error::Missing Actions variable OCR_LLM_URL", proc.stdout)
        # Ensure sentinel secret value was never emitted in stdout or stderr
        self.assertNotIn("my-secret-token-sentinel", proc.stdout + proc.stderr)

    def test_private_repository_constraint_enforced(self):
        """Public repository is blocked before writes with descriptive message."""
        api = Mock()
        api.request.return_value = (200, {"visibility": "public"})

        result = run_policy(api, "owner/public-repo", {"version": 2, "stages": [review_stage()]}, apply=True)
        self.assertEqual(result["changes"][0]["availability"], "unsupported")
        self.assertIn("private repositories only", result["changes"][0]["reason"])

        # Confirm only read requests were issued
        methods = [call.args[0] for call in api.request.call_args_list]
        self.assertEqual(methods, ["GET"])

    def test_advisory_review_is_non_gating(self):
        """Advisory review comments on PRs without approving or acting as required check."""
        workflow_bytes = render_workflow(validate_review(review_stage()))
        workflow_text = workflow_bytes.decode("utf-8")
        # Review workflow only uploads comments, does not submit approval reviews
        self.assertIn("pull-requests: write", workflow_text)
        self.assertNotIn("review_status: APPROVED", workflow_text)


if __name__ == "__main__":
    unittest.main()
