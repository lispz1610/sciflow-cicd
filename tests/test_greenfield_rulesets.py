"""Tests for decoupled greenfield ruleset onboarding and optional evidence_pr (Ticket #51)."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
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

import scripts.setup_github_repo as setup
from scripts.interactive_apply import format_apply_summary, format_dry_run_summary
from scripts.interactive_workflows_rulesets import prompt_rulesets_stage
from scripts.headless import run_headless_apply
from test_configure_github_repo import FakeApi


class InventoryApi(FakeApi):
    def request(self, method, path, payload=None):
        if method == "GET" and path == "/repos/acme/project/code-scanning/default-setup":
            return 200, self.codeql
        if path.startswith("/repos/acme/project/rulesets?includes_parents=false&per_page=100&page="):
            return 200, self.rulesets
        return super().request(method, path, payload)


class GreenfieldRulesetPolicyValidationTests(unittest.TestCase):
    """Test Policy schema and validation routines for gates stage with optional evidence_pr."""

    def test_validate_policy_accepts_gates_with_omitted_evidence_pr(self):
        stage = {
            "name": "gates",
            "ruleset_name": "quality-gate",
            "reviewer": "octocat",
            "checks": {"ci-check": ".github/workflows/ci.yml"},
        }
        policy = {"version": 1, "stages": [stage]}
        validated = setup.validate_policy(policy)
        self.assertEqual(validated, [stage])

    def test_validate_policy_accepts_gates_with_none_evidence_pr(self):
        stage = {
            "name": "gates",
            "ruleset_name": "quality-gate",
            "evidence_pr": None,
            "reviewer": "octocat",
            "checks": {"ci-check": ".github/workflows/ci.yml"},
        }
        policy = {"version": 1, "stages": [stage]}
        validated = setup.validate_policy(policy)
        self.assertEqual(validated, [stage])

    def test_validate_policy_accepts_gates_with_valid_integer_evidence_pr(self):
        stage = {
            "name": "gates",
            "ruleset_name": "quality-gate",
            "evidence_pr": 42,
            "reviewer": "octocat",
            "checks": {"ci-check": ".github/workflows/ci.yml"},
        }
        policy = {"version": 1, "stages": [stage]}
        validated = setup.validate_policy(policy)
        self.assertEqual(validated, [stage])

    def test_validate_policy_rejects_non_positive_or_non_integer_evidence_pr(self):
        for bad_pr in [0, -5, "42", True, 3.14]:
            policy = {
                "version": 1,
                "stages": [
                    {
                        "name": "gates",
                        "ruleset_name": "quality-gate",
                        "evidence_pr": bad_pr,
                        "reviewer": "octocat",
                        "checks": {"ci-check": ".github/workflows/ci.yml"},
                    }
                ],
            }
            with self.assertRaises(ValueError):
                setup.validate_policy(policy)

    def test_validate_policy_rejects_gates_missing_checks(self):
        policy = {
            "version": 1,
            "stages": [
                {
                    "name": "gates",
                    "ruleset_name": "quality-gate",
                    "reviewer": "octocat",
                    "checks": {},
                }
            ],
        }
        with self.assertRaises(ValueError):
            setup.validate_policy(policy)


class GreenfieldRulesetStagingTests(unittest.TestCase):
    """Test setup_repo dry-run and apply staging behavior with optional evidence_pr."""

    def test_dry_run_records_greenfield_gate_as_pending_without_writes(self):
        api = InventoryApi()
        policy = {
            "version": 1,
            "stages": [
                {
                    "name": "gates",
                    "ruleset_name": "quality-gate",
                    "reviewer": "octocat",
                    "checks": {"ci-check": ".github/workflows/ci.yml"},
                }
            ],
        }
        result = setup.run_policy(api, "acme/project", policy)
        self.assertEqual(api.writes, [])
        gate_changes = [c for c in result["changes"] if c["stage"] == "gates"]
        self.assertEqual(len(gate_changes), 1)
        self.assertEqual(gate_changes[0]["status"], "pending")
        self.assertIn("evidence pr", gate_changes[0]["reason"].lower())
        self.assertIn("setup pull request", gate_changes[0]["reason"].lower())

    def test_dry_run_records_gate_with_none_evidence_pr_as_pending(self):
        api = InventoryApi()
        policy = {
            "version": 1,
            "stages": [
                {
                    "name": "gates",
                    "ruleset_name": "quality-gate",
                    "evidence_pr": None,
                    "reviewer": "octocat",
                    "checks": {"ci-check": ".github/workflows/ci.yml"},
                }
            ],
        }
        result = setup.run_policy(api, "acme/project", policy)
        self.assertEqual(api.writes, [])
        gate_changes = [c for c in result["changes"] if c["stage"] == "gates"]
        self.assertEqual(len(gate_changes), 1)
        self.assertEqual(gate_changes[0]["status"], "pending")

    def test_apply_keeps_greenfield_gate_pending_without_calling_reconcile_gates(self):
        api = InventoryApi()
        policy = {
            "version": 1,
            "stages": [
                {
                    "name": "features",
                    "enable": ["wiki"],
                },
                {
                    "name": "gates",
                    "ruleset_name": "quality-gate",
                    "evidence_pr": None,
                    "reviewer": "octocat",
                    "checks": {"ci-check": ".github/workflows/ci.yml"},
                },
            ],
        }
        dry_run = setup.run_policy(api, "acme/project", policy)
        applied = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        # Verify direct setting (features) applied
        feature_change = next(c for c in applied["changes"] if c["stage"] == "features")
        self.assertEqual(feature_change["status"], "verified")
        # Verify gate remains pending
        gate_change = next(c for c in applied["changes"] if c["stage"] == "gates")
        self.assertEqual(gate_change["status"], "pending")
        # Verify no ruleset API writes were attempted
        self.assertFalse(any("rulesets" in path for _, path, _ in api.writes))

    def test_stage_changes_returns_empty_when_evidence_pr_is_none(self):
        api = InventoryApi()
        stage = {
            "name": "gates",
            "ruleset_name": "quality-gate",
            "evidence_pr": None,
            "reviewer": "octocat",
            "checks": {"ci-check": ".github/workflows/ci.yml"},
        }
        planned = setup._stage_changes(api, "acme/project", stage)
        self.assertEqual(planned, [])


class GreenfieldInteractivePromptTests(unittest.TestCase):
    """Test interactive prompt_rulesets_stage supporting skipped evidence_pr."""

    def test_skipping_evidence_pr_with_blank_input(self):
        # Confirm: y, PR: blank (skip), Reviewer: octocat, Check: ci-check
        in_stream = io.StringIO("y\n\noctocat\nci-check\n")
        out_stream = io.StringIO()

        stage = prompt_rulesets_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(stage["name"], "gates")
        self.assertIsNone(stage.get("evidence_pr"))
        self.assertEqual(stage["reviewer"], "octocat")
        self.assertEqual(stage["checks"], {"ci-check": ".github/workflows/ci.yml"})

    def test_skipping_evidence_pr_with_skip_keyword(self):
        # Confirm: y, PR: 'skip', Reviewer: octocat, Check: ci-check
        in_stream = io.StringIO("y\nskip\noctocat\nci-check\n")
        out_stream = io.StringIO()

        stage = prompt_rulesets_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertIsNone(stage.get("evidence_pr"))

    def test_skipping_evidence_pr_with_none_keyword(self):
        # Confirm: y, PR: 'none', Reviewer: none (solo maintainer), Check: ci-check
        in_stream = io.StringIO("y\nnone\nnone\nci-check\n")
        out_stream = io.StringIO()

        stage = prompt_rulesets_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertIsNone(stage.get("evidence_pr"))
        self.assertEqual(stage.get("required_approving_review_count"), 0)

    def test_compact_guidance_banner_is_within_two_lines(self):
        in_stream = io.StringIO("n\n")
        out_stream = io.StringIO()

        prompt_rulesets_stage(in_stream=in_stream, out_stream=out_stream)
        output = out_stream.getvalue().strip()
        lines = [line for line in output.split("\n") if line.strip() and not line.strip().startswith("Configure branch ruleset")]
        self.assertLessEqual(len(lines), 2)


class GreenfieldHeadlessAndSummaryTests(unittest.TestCase):
    """Test headless execution and dry-run/apply summaries for greenfield policies."""

    def test_dry_run_summary_and_apply_summary_format_pending_gate(self):
        dry_run_result = {
            "mode": "dry-run",
            "repository": "acme/project",
            "changes": [
                {
                    "stage": "gates",
                    "scope": "acme/project",
                    "status": "pending",
                    "reason": "Merge the draft Setup Pull Request or provide an approved evidence PR with successful CI checks before applying this gate",
                }
            ],
        }
        summary = format_dry_run_summary(dry_run_result)
        self.assertIn("Pending gates and rulesets", summary)
        self.assertIn("Status: pending", summary)

        apply_result = {
            "mode": "apply",
            "repository": "acme/project",
            "changes": [
                {
                    "stage": "gates",
                    "scope": "acme/project",
                    "status": "pending",
                    "reason": "Merge the draft Setup Pull Request or provide an approved evidence PR with successful CI checks before applying this gate",
                }
            ],
        }
        apply_summary = format_apply_summary(apply_result)
        self.assertIn("Pending: 1", apply_summary)
        self.assertIn("Verified direct settings are complete; pending work still needs owner action.", apply_summary)

    def test_headless_dry_run_and_apply_without_evidence_pr(self):
        policy_data = {
            "version": 1,
            "stages": [
                {
                    "name": "features",
                    "enable": ["wiki"],
                },
                {
                    "name": "gates",
                    "ruleset_name": "quality-gate",
                    "reviewer": "octocat",
                    "checks": {"ci-check": ".github/workflows/ci.yml"},
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            policy_file = Path(tmpdir) / "policy.json"
            policy_file.write_text(json.dumps(policy_data), encoding="utf-8")

            fake_api = InventoryApi()
            with patch("scripts.headless.GitHubApi", return_value=fake_api):
                preview_io = io.StringIO()
                with redirect_stdout(preview_io):
                    code = run_headless_apply(str(policy_file), repo="acme/project", token="dummy-token", apply=False)
                self.assertEqual(code, 0)
                dry_run_json = preview_io.getvalue()
                plan_file = Path(tmpdir) / "dry-run.json"
                plan_file.write_text(dry_run_json, encoding="utf-8")

                apply_io = io.StringIO()
                with redirect_stdout(apply_io):
                    code = run_headless_apply(
                        str(policy_file),
                        repo="acme/project",
                        token="dummy-token",
                        apply=True,
                        dry_run_path=str(plan_file),
                    )
                self.assertEqual(code, 0)
