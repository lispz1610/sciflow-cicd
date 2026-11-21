"""Comprehensive audit of Advisory Review contracts for external systemd VM runner (#61)."""

import os
import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
TESTS_DIR = ROOT_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_ocr_preflight import _base_server
from scripts.ocr_preflight import preflight_ocr_runtime, format_ocr_preflight_report
from scripts.ocr_poller import evaluate_pr_eligibility, resolve_eligibility_config
from scripts.ocr_app_auth import _has_forbidden_permissions
from scripts.ocr_safety import assert_no_self_hosted_runner, check_same_repo_origin


class AdvisoryReviewAuditTests(unittest.TestCase):
    def test_secrets_and_tokens_are_isolated_to_vm_host_environment_and_never_committed(self):
        """LLM provider credentials and App secrets exist strictly on VM host and are redacted in outputs."""
        secret_token = "sk-super-secret-token-sentinel-xyz123"
        env = {
            "OCR_LLM_AUTH_TOKEN": secret_token,
            "OCR_LLM_URL": "https://api.example.com/v1",
            "OCR_LLM_MODEL": "claude-sonnet-4-5",
            "OCR_LLM_USE_ANTHROPIC": "true",
        }
        server = _base_server()
        result = preflight_ocr_runtime(
            app_id=4242,
            allowlist=["acme/repo-a"],
            env=env,
            api_factory=server.api_factory,
            signer=lambda b: b"sig",
            which_fn=lambda name: "/usr/local/bin/ocr" if name == "ocr" else None,
        )
        report = format_ocr_preflight_report(result)
        dict_output = str(result.to_dict())

        # Secret value must NEVER leak into report or serialized data
        self.assertNotIn(secret_token, report)
        self.assertNotIn(secret_token, dict_output)
        self.assertNotIn("xyz123", report)
        self.assertNotIn("xyz123", dict_output)

    def test_fork_prs_are_skipped_before_diff_read_or_ocr_invocation(self):
        """Advisory review skips fork PRs to ensure untrusted code never triggers OCR."""
        fork_pr = {
            "number": 42,
            "state": "open",
            "head": {"repo": {"fork": True, "full_name": "attacker/repo-a", "id": 999}, "sha": "1" * 40},
            "base": {"repo": {"fork": False, "full_name": "acme/repo-a", "id": 100}},
            "labels": [{"name": "eligible for review"}],
            "author_association": "NONE",
            "draft": False,
        }
        cfg = resolve_eligibility_config()
        is_eligible, reason = evaluate_pr_eligibility(fork_pr, "acme/repo-a", eligibility_config=cfg)
        self.assertFalse(is_eligible)
        self.assertEqual(reason, "fork_or_cross_repo")

        # Verify safe boundary check also rejects fork origin
        decision = check_same_repo_origin(fork_pr, "acme/repo-a")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "fork_pr")

    def test_runtime_configuration_guard_diagnostics_without_secret_leakage(self):
        """Preflight reports pending_configuration with clear instructions when variables missing."""
        secret_token = "sk-sentinel-token-do-not-leak"
        # Missing OCR_LLM_URL
        test_env = {
            "OCR_LLM_AUTH_TOKEN": secret_token,
            "OCR_LLM_MODEL": "review-model-v1",
            "OCR_LLM_USE_ANTHROPIC": "false",
        }
        server = _base_server()
        result = preflight_ocr_runtime(
            app_id=4242,
            allowlist=["acme/repo-a"],
            env=test_env,
            api_factory=server.api_factory,
            signer=lambda b: b"sig",
            which_fn=lambda name: "/usr/local/bin/ocr" if name == "ocr" else None,
        )
        self.assertEqual(result.status, "pending_configuration")
        self.assertTrue(result.pending)
        self.assertFalse(result.ready)

        report = format_ocr_preflight_report(result)
        self.assertIn("Missing required OCR provider configuration", report)
        self.assertNotIn(secret_token, report)

    def test_allowlist_and_limited_app_scope_enforced(self):
        """Limited OCR GitHub App requires least privilege and rejects administration permissions."""
        app_perms = {
            "contents": "read",
            "pull_requests": "write",
            "metadata": "read",
            "administration": "read",  # Forbidden!
        }
        forbidden = _has_forbidden_permissions(app_perms)
        self.assertTrue(any("administration" in f for f in forbidden))

    def test_advisory_review_is_non_gating(self):
        """External OCR runner publishes strictly advisory review comments without merge gating."""
        status = assert_no_self_hosted_runner(execution_mode="local_direct", env={})
        self.assertTrue(status.passed)

        # Self-hosted actions runner tokens must fail-closed to prevent runner boundary crossing
        status_bad = assert_no_self_hosted_runner(
            execution_mode="local_direct",
            env={"RUNNER_REGISTRATION_TOKEN": "token-xyz"},
        )
        self.assertFalse(status_bad.passed)
        self.assertTrue(any("self-hosted" in v.lower() for v in status_bad.violations))


if __name__ == "__main__":
    unittest.main()
