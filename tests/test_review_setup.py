"""Public seams for the optional Advisory Review stage."""

import io
import os
import subprocess
import textwrap
import unittest
from unittest.mock import Mock, patch

from scripts.interactive import Session
from scripts.interactive_review import (prompt_review_stage, suggest_llm_extra_body,
                                        _existing_defaults)
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
        self.assertEqual(set(files), {".github/workflows/ci.yml", WORKFLOW_PATH,
                                      REVIEW_RUNTIME_PATH})
        self.assertIn(b"pull_request_target", files[WORKFLOW_PATH][0])
        self.assertTrue(files[REVIEW_RUNTIME_PATH][0].startswith(REVIEW_RUNTIME_MARKER))
        self.assertIn(b"module.exports = { run, syncReviewIssue }", files[REVIEW_RUNTIME_PATH][0])

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
        plan = {"changes": [{"path": WORKFLOW_PATH, "operation": "create", "current_sha": None,
                             "desired_sha256": "a" * 64},
                            {"path": REVIEW_RUNTIME_PATH, "operation": "create", "current_sha": None,
                             "desired_sha256": "b" * 64}], "status": "planned"}
        with patch("scripts.setup_github_repo.review_readiness", return_value=["Review pending configuration: add Actions secret OCR_LLM_AUTH_TOKEN"]), \
             patch("scripts.setup_github_repo.install_selected", return_value=plan) as installer:
            result = run_policy(api, "owner/private", {"version": 2, "stages": [review_stage()]})
        self.assertEqual({item["path"] for item in result["changes"]},
                         {WORKFLOW_PATH, REVIEW_RUNTIME_PATH})
        self.assertTrue(all(item["status"] == "planned" and item["stage"] == "review"
                            for item in result["changes"]))
        self.assertTrue(result["warnings"])
        installer.assert_called_once()

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
        for expected in ("Actions is disabled", "retention is 90 days", "OCR_LLM_AUTH_TOKEN",
                         "OCR_LLM_URL differs", "OCR_LLM_MODEL", "Issues must be enabled",
                         "issues: write", "Issues are disabled"):
            self.assertIn(expected, report)
        self.assertNotIn("wrong.example", report)
        self.assertTrue(all(call.args[0] == "GET" for call in api.request.call_args_list))


class ReviewPromptTests(unittest.TestCase):
    def test_unmanaged_reporter_blocks_review_selection(self):
        output = io.StringIO()
        session = Session("owner/private", Mock(), repo_data={"visibility": "private", "default_branch": "main"},
                          in_stream=io.StringIO("y\n"), out_stream=output)
        session.review_workflow = {"status": "absent"}
        with patch("scripts.interactive_review.detect_review_reporter",
                   return_value={"status": "unmanaged", "sha": "a" * 40}):
            self.assertIsNone(prompt_review_stage(session))
        self.assertIn(REVIEW_RUNTIME_PATH, output.getvalue())

    def test_new_private_repository_defaults_off_and_explains_options(self):
        output = io.StringIO()
        session = Session("owner/private", Mock(), repo_data={"visibility": "private"},
                          in_stream=io.StringIO("\n"), out_stream=output)
        session.review_workflow = {"status": "absent"}
        self.assertIsNone(prompt_review_stage(session))
        transcript = output.getvalue()
        for phrase in ("billing", "provider", "OCR_LLM_AUTH_TOKEN", "OCR_LLM_URL",
                       "not", "Recommended default: No", "Issues enabled", "issues: write"):
            self.assertIn(phrase, transcript)

    def test_public_repository_skips_review_without_prompt(self):
        output = io.StringIO()
        session = Session("owner/public", Mock(), repo_data={"visibility": "public"},
                          in_stream=io.StringIO(""), out_stream=output)
        session.review_workflow = {"status": "absent"}
        self.assertIsNone(prompt_review_stage(session))
        self.assertIn("private repositories only", output.getvalue())

    def test_owner_can_select_review_without_entering_secret(self):
        output = io.StringIO()
        answers = "y\nn\nhttps://api.example.com/v1\nreview-model-v1\n\n\n\n\nn\n"
        session = Session("owner/private", Mock(), repo_data={"visibility": "private"},
                          in_stream=io.StringIO(answers), out_stream=output)
        session.review_workflow = {"status": "absent"}
        stage = prompt_review_stage(session, raise_on_interrupt=True)
        self.assertEqual(stage["name"], "review")
        self.assertEqual(stage["llm"]["model"], "review-model-v1")
        self.assertEqual(set(stage["llm"]), {"url", "model", "use_anthropic", "secret_name"})
        self.assertIn("Token budget", output.getvalue())

    def test_extra_body_validation_and_rendering(self):
        stage = review_stage()
        stage["llm"]["extra_body"] = {"temperature": 0.2, "top_p": 0.95}
        policy = validate_review(stage)
        self.assertEqual(policy.llm.extra_body, {"temperature": 0.2, "top_p": 0.95})
        workflow = render_workflow(policy).decode()
        self.assertIn("llm_extra_body: '{\"temperature\": 0.2, \"top_p\": 0.95}'", workflow)

        # Accepts nested thinking configuration dict
        stage_thinking = review_stage()
        stage_thinking["llm"]["extra_body"] = {"thinking": {"type": "enabled", "budget_tokens": 2048}}
        policy_thinking = validate_review(stage_thinking)
        self.assertEqual(policy_thinking.llm.extra_body, {"thinking": {"type": "enabled", "budget_tokens": 2048}})

        # Rejects raw strings, non-dicts, and empty keys
        for bad in ("not-json", '{"temperature": 0.2}', [1, 2, 3], {"": 1}):
            invalid_stage = review_stage()
            invalid_stage["llm"]["extra_body"] = bad
            with self.assertRaisesRegex(ValueError, "llm extra_body must be a JSON object"):
                validate_review(invalid_stage)

        # Unknown llm key diagnostic mentions optional extra_body
        unknown_key_stage = review_stage()
        unknown_key_stage["llm"]["provider_key"] = "unsafe"
        with self.assertRaisesRegex(ValueError, r"llm must be an object with exactly.*optional fields \(extra_body\)"):
            validate_review(unknown_key_stage)

    def test_suggest_llm_extra_body(self):
        self.assertEqual(suggest_llm_extra_body("claude-3-7-sonnet-20250219", True),
                         {"temperature": 1.0, "thinking": {"type": "enabled", "budget_tokens": 2048}})
        self.assertEqual(suggest_llm_extra_body("claude-3-5-sonnet", True),
                         {"temperature": 0.2, "top_p": 0.95})
        self.assertEqual(suggest_llm_extra_body("gemini-2.0-flash-thinking", False),
                         {"temperature": 0.7})
        self.assertEqual(suggest_llm_extra_body("o3-mini", False),
                         {"reasoning_effort": "medium"})
        self.assertEqual(suggest_llm_extra_body("gpt-4o", False),
                         {"temperature": 0.2, "top_p": 0.95})

    def test_existing_defaults_recovers_only_primary_extra_body(self):
        stage = review_stage()
        stage["llm"]["extra_body"] = {"note": "it's a \"test\"", "temperature": 0.15}
        workflow_bytes = render_workflow(validate_review(stage))
        defaults = _existing_defaults(workflow_bytes)
        self.assertEqual(defaults.get("extra_body"), {"note": "it's a \"test\"", "temperature": 0.15})

        # Legacy workflow where primary step has no llm_extra_body must not inherit fallback_review's extra_body
        legacy_without_primary_extra = (
            b"      - uses: alibaba/open-code-review@182898cf522da3d04157b422752d028417974e19\n"
            b"        id: review\n"
            b"        with:\n"
            b"          llm_model: ${{ vars.OCR_LLM_MODEL }}\n"
            b"      - name: Fallback Review\n"
            b"        id: fallback_review\n"
            b"        with:\n"
            b"          llm_extra_body: '{\"reasoning_effort\": \"medium\"}'\n"
        )
        self.assertIsNone(_existing_defaults(legacy_without_primary_extra)["extra_body"])

    def test_owner_can_select_suggested_extra_body(self):
        output = io.StringIO()
        # 2 = Use suggested defaults for claude-3-7-sonnet
        answers = "y\ny\nhttps://api.example.com/v1\nclaude-3-7-sonnet\n2\n\n\n\nn\n"
        session = Session("owner/private", Mock(), repo_data={"visibility": "private"},
                          in_stream=io.StringIO(answers), out_stream=output)
        session.review_workflow = {"status": "absent"}
        stage = prompt_review_stage(session, raise_on_interrupt=True)
        self.assertEqual(
            stage["llm"]["extra_body"],
            {"temperature": 1.0, "thinking": {"type": "enabled", "budget_tokens": 2048}},
        )

    def test_owner_can_configure_individual_llm_parameters(self):
        output = io.StringIO()
        # 3 = Configure individual parameters
        # temp=0.2, top_p=0.9, top_k=40, thinking_mode=enabled, budget_tokens=4096, effort=medium
        answers = (
            "y\nn\nhttps://api.example.com/v1\ngpt-4o\n"
            "3\n0.2\n0.9\n40\nenabled\n4096\nmedium\n"
            "\n\n\nn\n"
        )
        session = Session("owner/private", Mock(), repo_data={"visibility": "private"},
                          in_stream=io.StringIO(answers), out_stream=output)
        session.review_workflow = {"status": "absent"}
        stage = prompt_review_stage(session, raise_on_interrupt=True)
        self.assertEqual(
            stage["llm"]["extra_body"],
            {
                "temperature": 0.2,
                "top_p": 0.9,
                "top_k": 40,
                "thinking": {"type": "enabled", "budget_tokens": 4096},
                "reasoning_effort": "medium",
            },
        )

    def test_owner_can_enter_raw_extra_body_json(self):
        output = io.StringIO()
        # 4 = Enter raw llm_extra_body JSON
        answers = "y\nn\nhttps://api.example.com/v1\ngpt-4o\n4\n{\"temperature\": 0.3}\n\n\n\nn\n"
        session = Session("owner/private", Mock(), repo_data={"visibility": "private"},
                          in_stream=io.StringIO(answers), out_stream=output)
        session.review_workflow = {"status": "absent"}
        stage = prompt_review_stage(session, raise_on_interrupt=True)
        self.assertEqual(stage["llm"]["extra_body"], {"temperature": 0.3})
        self.assertIn("LLM parameter configuration", output.getvalue())

    def test_managed_change_needs_blob_sha_confirmation(self):
        output = io.StringIO()
        existing = render_workflow(validate_review(review_stage()))
        answers = "\nn\nhttps://api.example.com/v1\nreview-model-v1\n\n\n60000\n\n\ny\n"
        session = Session("owner/private", Mock(), repo_data={"visibility": "private"},
                          in_stream=io.StringIO(answers), out_stream=output, resume_mode=True)
        session.review_workflow = {"status": "managed", "sha": "a" * 40, "content": existing}
        stage = prompt_review_stage(session, raise_on_interrupt=True)
        self.assertEqual(stage["workflow"]["expected_sha"], "a" * 40)
        self.assertIn("blob SHA " + "a" * 40, output.getvalue())


if __name__ == "__main__":
    unittest.main()
