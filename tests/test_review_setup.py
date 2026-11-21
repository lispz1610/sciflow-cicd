"""Public seams for the optional Advisory Review stage."""

import io
import os
import subprocess
import textwrap
import unittest
from unittest.mock import Mock, patch

from scripts.interactive import Session
from scripts.install_github_workflows import (install_selected, REVIEW_RUNTIME_PATH,
                                              REVIEW_RUNTIME_MARKER)
from scripts.review_policy import validate_policy as validate_review
from scripts.review_readiness import review_readiness
from scripts.review_workflow import render_workflow, WORKFLOW_PATH
from scripts.setup_github_repo import run_policy, validate_policy


def review_stage():
    return {
        "name": "review",
        "workflow": {"template": "open-code-review", "template_version": 1,
                     "filename": "open-code-review.yml", "triggers": ["pull_request_target"],
                     "events": ["opened", "synchronize", "reopened"], "fork_policy": "skip",
                     "action_sha": "182898cf522da3d04157b422752d028417974e19",
                     "ocr_version": "1.12.12", "max_tokens_budget": 50000,
                     "timeout_minutes": 30},
        "llm": {"url": "https://api.example.com/v1", "model": "review-model-v1",
                "use_anthropic": False, "secret_name": "OCR_LLM_AUTH_TOKEN"},
        "limits": {"max_retention_days": 30},
    }


class ReviewPolicyTests(unittest.TestCase):
    def test_version_two_accepts_nonsecret_review_stage_and_version_one_rejects_it(self):
        stage = review_stage()
        self.assertEqual(validate_policy({"version": 2, "stages": [stage]}), [stage])
        with self.assertRaisesRegex(ValueError, "requires Policy version 2"):
            validate_policy({"version": 1, "stages": [stage]})
        stage["llm"]["provider_key"] = "unsafe"
        with self.assertRaises(ValueError):
            validate_policy({"version": 2, "stages": [stage]})

    def test_review_policy_runs_on_defaults_and_validation(self):
        stage = review_stage()
        policy = validate_review(stage)
        self.assertEqual(policy.workflow.runs_on, "self-hosted")

        stage_hosted = review_stage()
        stage_hosted["workflow"]["runs_on"] = "ubuntu-24.04"
        self.assertEqual(validate_review(stage_hosted).workflow.runs_on, "ubuntu-24.04")

        stage_labels = review_stage()
        stage_labels["workflow"]["runs_on"] = ["self-hosted", "gpu"]
        self.assertEqual(validate_review(stage_labels).workflow.runs_on, ("self-hosted", "gpu"))

        for invalid in ("", "   ", [], [""], ["self-hosted", "self-hosted"], True, 123, ["self-hosted", 1]):
            bad_stage = review_stage()
            bad_stage["workflow"]["runs_on"] = invalid
            with self.assertRaises(ValueError):
                validate_review(bad_stage)

    def test_review_workflow_renders_runs_on_for_both_jobs(self):
        import yaml
        # Default self-hosted
        workflow_bytes = render_workflow(validate_review(review_stage()))
        parsed = yaml.safe_load(workflow_bytes.decode())
        self.assertEqual(parsed["jobs"]["review"]["runs-on"], "self-hosted")
        self.assertEqual(parsed["jobs"]["report"]["runs-on"], "self-hosted")

        # Hosted runner
        stage_hosted = review_stage()
        stage_hosted["workflow"]["runs_on"] = "ubuntu-24.04"
        parsed_hosted = yaml.safe_load(render_workflow(validate_review(stage_hosted)).decode())
        self.assertEqual(parsed_hosted["jobs"]["review"]["runs-on"], "ubuntu-24.04")
        self.assertEqual(parsed_hosted["jobs"]["report"]["runs-on"], "ubuntu-24.04")

        # Custom runner labels
        stage_labels = review_stage()
        stage_labels["workflow"]["runs_on"] = ["self-hosted", "linux", "gpu"]
        parsed_labels = yaml.safe_load(render_workflow(validate_review(stage_labels)).decode())
        self.assertEqual(parsed_labels["jobs"]["review"]["runs-on"], ["self-hosted", "linux", "gpu"])
        self.assertEqual(parsed_labels["jobs"]["report"]["runs-on"], ["self-hosted", "linux", "gpu"])

    def test_review_run_guard_names_missing_configuration_without_logging_values(self):
        workflow = render_workflow(validate_review(review_stage())).decode()
        self.assertIn("pull_request_target:", workflow)
        self.assertIn('run-name: "Review PR #${{ github.event.pull_request.number }} workflow ${{ github.workflow_sha }}"', workflow)
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("pull-requests: write", workflow)
        self.assertIn("::error::Missing Actions secret OCR_LLM_AUTH_TOKEN", workflow)
        self.assertIn("::error::Missing Actions variable OCR_LLM_URL", workflow)
        self.assertIn("::error::Missing Actions variable OCR_LLM_FALLBACK_MODEL", workflow)
        self.assertLess(workflow.index("Check Review Run configuration"), workflow.index("uses: alibaba/open-code-review@"))
        self.assertNotIn("actions/checkout@", workflow.split("  report:\n", 1)[0])
        self.assertIn('upload_artifacts: "false"', workflow)

    def test_actionable_findings_use_a_separate_issue_writer_job(self):
        workflow = render_workflow(validate_review(review_stage())).decode()
        review_job = workflow.split("  report:\n", 1)[0]
        report_job = workflow.split("  report:\n", 1)[1]
        self.assertIn("  issues: write", report_job)
        self.assertIn("  pull-requests: read", report_job)
        self.assertNotIn("issues: write", review_job)
        self.assertIn("id: review", review_job)
        self.assertIn("steps.review.outputs.comments_total", review_job)
        self.assertIn("steps.review.outputs.comments_routed", review_job)
        self.assertIn("needs.review.result == 'success'", report_job)
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", report_job)
        self.assertIn("needs.review.outputs.comments_total", report_job)
        self.assertIn("actions/github-script@", report_job)
        self.assertIn("actions/checkout@", report_job)
        self.assertIn("github.event.pull_request.base.sha", report_job)
        self.assertNotIn("continue-on-error", report_job)
        self.assertIn("review_issue_reporter", report_job)

    def test_review_run_guard_fails_at_runtime_without_exposing_secret(self):
        workflow = render_workflow(validate_review(review_stage())).decode()
        script = textwrap.dedent(workflow.split("        run: |\n", 1)[1].split("      - uses:", 1)[0])
        env = {**os.environ, "OCR_TOKEN": "provider-secret-sentinel", "OCR_URL": "",
               "OCR_MODEL": "review-model-v1", "OCR_PROTOCOL": "false"}
        run = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 1)
        self.assertIn("::error::Missing Actions variable OCR_LLM_URL", run.stdout)
        self.assertNotIn("provider-secret-sentinel", run.stdout + run.stderr)

    def test_one_staging_call_contains_review_and_ci_bytes(self):
        generic = {"name": "workflows", "workflows": [{"template": "pr-ci", "template_version": 1,
                   "filename": "ci.yml", "triggers": ["pull_request"],
                   "checkout_sha": "11bd71901bbe5b1630ceea73d27597364c9af683", "commands": ["echo ready"]}]}
        with patch("scripts.install_github_workflows.stage_workflows", return_value={"changes": []}) as staged:
            install_selected(Mock(), "owner/private", generic, review_stage(), apply=False)
        files = staged.call_args.args[2]
        self.assertEqual(set(files), {".github/workflows/ci.yml"})
        self.assertNotIn(WORKFLOW_PATH, files)
        self.assertNotIn(REVIEW_RUNTIME_PATH, files)

    def test_reporter_expected_sha_is_validated_separately_from_workflow_sha(self):
        stage = review_stage()
        stage["workflow"]["reporter_expected_sha"] = "b" * 40
        self.assertEqual(validate_review(stage).workflow.reporter_expected_sha, "b" * 40)
        stage["workflow"]["reporter_expected_sha"] = "invalid"
        with self.assertRaisesRegex(ValueError, "reporter_expected_sha"):
            validate_review(stage)

    def test_public_target_rejects_review_before_writes(self):
        api = Mock()
        api.request.return_value = (200, {"visibility": "public"})
        result = run_policy(api, "owner/public", {"version": 2, "stages": [review_stage()]}, apply=True)
        self.assertEqual(result["changes"][0]["availability"], "unsupported")
        self.assertIn("private repositories only", result["changes"][0]["reason"])
        self.assertEqual([call.args[0] for call in api.request.call_args_list], ["GET"])

    def test_readiness_warnings_do_not_block_staging(self):
        api = Mock()
        api.request.return_value = (200, {"visibility": "private"})
        result = run_policy(api, "owner/private", {"version": 2, "stages": [review_stage()]})
        self.assertEqual(result["changes"], [])
        self.assertNotIn("warnings", result)

    def test_readiness_reports_missing_items_and_mismatch_without_printing_values(self):
        replies = {
            "/repos/owner/private": (200, {"owner": {"type": "User"}, "has_issues": False}),
            "/repos/owner/private/actions/permissions": (200, {"enabled": False, "allowed_actions": "all"}),
            "/repos/owner/private/actions/permissions/artifact-and-log-retention": (200, {"days": 90}),
            "/repos/owner/private/actions/secrets/OCR_LLM_AUTH_TOKEN": (404, {}),
            "/repos/owner/private/actions/variables/OCR_LLM_URL": (200, {"name": "OCR_LLM_URL", "value": "https://wrong.example/secret-query"}),
            "/repos/owner/private/actions/variables/OCR_LLM_MODEL": (404, {}),
            "/repos/owner/private/actions/variables/OCR_LLM_USE_ANTHROPIC": (200, {"name": "OCR_LLM_USE_ANTHROPIC", "value": "false"}),
        }
        api = Mock()
        api.request.side_effect = lambda method, path: replies[path]
        warnings = review_readiness(api, "owner/private", validate_review(review_stage()))
        report = "\n".join(warnings)
        for expected in ("Actions is disabled", "retention is 90 days", "Issues are disabled"):
            self.assertIn(expected, report)
        for omitted in ("OCR_LLM_AUTH_TOKEN", "OCR_LLM_URL", "OCR_LLM_MODEL"):
            self.assertNotIn(omitted, report)
        self.assertNotIn("wrong.example", report)
        self.assertTrue(all(call.args[0] == "GET" for call in api.request.call_args_list))


if __name__ == "__main__":
    unittest.main()
