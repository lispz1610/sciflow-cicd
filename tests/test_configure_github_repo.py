"""Contract tests for the GitHub repository settings reconciler."""

import importlib.util
import io
import json
import os
import pathlib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from api_test_support import FakeApi


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "configure_github_repo.py"
spec = importlib.util.spec_from_file_location("configure_github_repo", SCRIPT)
configurator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configurator)


class FeatureTests(unittest.TestCase):
    def test_selecting_wiki_does_not_change_other_features(self):
        api = FakeApi()
        changes = configurator.reconcile_features(api, "acme/project", ["wiki"], apply=False)
        self.assertEqual(changes, [("PATCH", "/repos/acme/project", {"has_wiki": True})])
        self.assertEqual(api.writes, [])

    def test_second_apply_is_no_op_and_preserves_other_settings(self):
        api = FakeApi()
        api.repo["description"] = "keep me"
        configurator.reconcile_features(api, "acme/project", ["wiki"], apply=True)
        api.writes.clear()
        self.assertEqual(configurator.reconcile_features(api, "acme/project", ["wiki"], apply=True), [])
        self.assertEqual(api.writes, [])
        self.assertEqual(api.repo["description"], "keep me")

    def test_missing_feature_state_blocks_write(self):
        api = FakeApi()
        del api.repo["has_wiki"]
        with self.assertRaisesRegex(configurator.ApiError, "cannot verify"):
            configurator.reconcile_features(api, "acme/project", ["wiki"], apply=True)
        self.assertEqual(api.writes, [])

    def test_explicit_feature_deactivation_patches_feature_to_false(self):
        api = FakeApi()
        api.repo["has_wiki"] = True
        changes = configurator.reconcile_features(api, "acme/project", disable=["wiki"], apply=False)
        self.assertEqual(changes, [("PATCH", "/repos/acme/project", {"has_wiki": False})])
        self.assertEqual(api.writes, [])

    def test_enable_and_disable_features_together(self):
        api = FakeApi()
        api.repo["has_issues"] = False
        api.repo["has_wiki"] = True
        changes = configurator.reconcile_features(
            api, "acme/project", features=["issues"], disable=["wiki"], apply=False
        )
        self.assertEqual(
            changes,
            [("PATCH", "/repos/acme/project", {"has_issues": True, "has_wiki": False})],
        )

    def test_main_features_enable_apply(self):
        api = FakeApi()
        stdout = io.StringIO()
        argv = ["--repo", "acme/project", "--apply", "features", "--enable", "wiki"]
        with patch.object(configurator, "GitHubApi", return_value=api), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "test-only"}), \
             redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            rc = configurator.main(argv)
        self.assertEqual(rc, 0)
        self.assertEqual(api.writes, [("PATCH", "/repos/acme/project", {"has_wiki": True})])
        self.assertTrue(api.repo["has_wiki"])
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["mode"], "apply")
        self.assertEqual(output["changes"], [{"method": "PATCH", "path": "/repos/acme/project", "body": {"has_wiki": True}}])

    def test_main_features_enable_dry_run(self):
        api = FakeApi()
        stdout = io.StringIO()
        argv = ["--repo", "acme/project", "features", "--enable", "wiki"]
        with patch.object(configurator, "GitHubApi", return_value=api), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "test-only"}), \
             redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            rc = configurator.main(argv)
        self.assertEqual(rc, 0)
        self.assertEqual(api.writes, [])
        self.assertFalse(api.repo["has_wiki"])
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["mode"], "dry-run")
        self.assertEqual(output["changes"], [{"method": "PATCH", "path": "/repos/acme/project", "body": {"has_wiki": True}}])


class ActionsTests(unittest.TestCase):
    def test_actions_stage_does_not_change_repo_features(self):
        api = FakeApi()
        changes = configurator.reconcile_actions(api, "acme/project", apply=True)
        self.assertTrue(changes)
        self.assertFalse(api.repo["has_wiki"])
        self.assertFalse(api.repo["has_issues"])

    def test_selected_action_policy_converges_after_transition(self):
        api = FakeApi()
        config = {"allowed_actions": "selected", "sha_pinning_required": True,
                  "github_owned_allowed": True, "verified_allowed": False,
                  "patterns_allowed": ["actions/checkout@*"]}
        changes = configurator.reconcile_actions_policy(api, "acme/project", config, apply=True)
        self.assertEqual(len(changes), 2)
        self.assertEqual(api.actions["allowed_actions"], "selected")
        api.writes.clear()
        self.assertEqual(configurator.reconcile_actions_policy(api, "acme/project", config, apply=True), [])
        self.assertEqual(api.writes, [])

    def test_actions_policy_put_preserves_required_enabled_state(self):
        api = FakeApi()
        changes = configurator.reconcile_actions_policy(
            api, "acme/project", {"sha_pinning_required": True})
        self.assertEqual(changes[0][2], {"enabled": False, "allowed_actions": "all",
                                         "sha_pinning_required": True})
        self.assertEqual(api.writes, [])

    def test_actions_policy_requires_observed_enabled_state(self):
        api = FakeApi()
        del api.actions["enabled"]
        with self.assertRaisesRegex(configurator.ApiError, "enabled"):
            configurator.reconcile_actions_policy(api, "acme/project",
                                                   {"sha_pinning_required": True}, apply=True)
        self.assertEqual(api.writes, [])

    def test_private_repository_rejects_public_action_patterns(self):
        api = FakeApi()
        api.repo["visibility"] = "private"
        with self.assertRaisesRegex(ValueError, "public"):
            configurator.reconcile_actions_policy(api, "acme/project", {
                "allowed_actions": "selected", "github_owned_allowed": True,
                "verified_allowed": False, "patterns_allowed": ["actions/checkout@*"]}, apply=True)
        self.assertEqual(api.writes, [])

    def test_missing_actions_state_blocks_write(self):
        api = FakeApi()
        del api.actions["enabled"]
        with self.assertRaisesRegex(configurator.ApiError, "cannot verify"):
            configurator.reconcile_actions(api, "acme/project", apply=True)
        self.assertEqual(api.writes, [])


class MergeTests(unittest.TestCase):
    def test_selected_merge_settings_preserve_unselected_fields(self):
        api = FakeApi()
        changes = configurator.reconcile_merge(api, "acme/project", {
            "allow_rebase_merge": False, "delete_branch_on_merge": True}, apply=True)
        self.assertEqual(changes, [("PATCH", "/repos/acme/project", {
            "allow_rebase_merge": False, "delete_branch_on_merge": True})])
        self.assertTrue(api.repo["allow_squash_merge"])
        self.assertEqual(configurator.reconcile_merge(api, "acme/project", {
            "allow_rebase_merge": False, "delete_branch_on_merge": True}, apply=True), [])

    def test_rejects_disabling_every_merge_method(self):
        api = FakeApi()
        with self.assertRaisesRegex(ValueError, "merge method"):
            configurator.reconcile_merge(api, "acme/project", {
                "allow_merge_commit": False, "allow_squash_merge": False,
                "allow_rebase_merge": False}, apply=True)
        self.assertEqual(api.writes, [])


class SecurityTests(unittest.TestCase):
    def test_every_supported_security_option_repeats_without_writes(self):
        for feature in configurator.SECURITY_FEATURES:
            with self.subTest(feature=feature):
                api = FakeApi()
                first = configurator.reconcile_security(api, "acme/project", [feature], apply=True)
                self.assertTrue(first)
                api.writes.clear()
                self.assertEqual(configurator.reconcile_security(
                    api, "acme/project", [feature], apply=True), [])
                self.assertEqual(api.writes, [])

    def test_selecting_alerts_does_not_enable_other_security_features(self):
        api = FakeApi()
        changes = configurator.reconcile_security(api, "acme/project", ["vulnerability-alerts"], apply=True)
        self.assertEqual([path for _, path, _ in changes], ["/repos/acme/project/vulnerability-alerts"])
        self.assertFalse(api.features["automated-security-fixes"])

    def test_unknown_security_state_fails_without_writes(self):
        api = FakeApi()
        del api.repo["security_and_analysis"]
        with self.assertRaisesRegex(configurator.ApiError, "cannot verify"):
            configurator.reconcile_security(api, "acme/project", ["secret-scanning"], apply=True)
        self.assertEqual(api.writes, [])

    def test_private_repo_rejects_public_only_reporting(self):
        api = FakeApi()
        api.repo["visibility"] = "private"
        with self.assertRaisesRegex(configurator.ApiError, "public repositories"):
            configurator.reconcile_security(api, "acme/project", ["private-vulnerability-reporting"], apply=True)
        self.assertEqual(api.writes, [])

    def test_read_failure_does_not_partially_apply(self):
        api = FakeApi()
        api.features["automated-security-fixes"] = None
        with self.assertRaises(configurator.ApiError):
            configurator.reconcile_security(api, "acme/project", ["vulnerability-alerts", "automated-security-fixes"], apply=True)
        self.assertEqual(api.writes, [])


class CodeQLTests(unittest.TestCase):
    def test_dry_run_plans_enablement_without_writes(self):
        api = FakeApi()
        api.actions["enabled"] = True
        self.assertEqual(
            configurator.reconcile_codeql(api, "acme/project"),
            [("PATCH", "/repos/acme/project/code-scanning/default-setup", {"state": "configured"})],
        )
        self.assertEqual(api.writes, [])

    def test_apply_is_idempotent(self):
        api = FakeApi()
        api.actions["enabled"] = True
        configurator.reconcile_codeql(api, "acme/project", apply=True)
        api.writes.clear()
        self.assertEqual(configurator.reconcile_codeql(api, "acme/project", apply=True), [])
        self.assertEqual(api.writes, [])

    def test_disabled_actions_block_enablement(self):
        api = FakeApi()
        with self.assertRaisesRegex(configurator.ApiError, "Actions"):
            configurator.reconcile_codeql(api, "acme/project", apply=True)
        self.assertEqual(api.writes, [])

    def test_unrecognized_state_blocks_enablement(self):
        api = FakeApi()
        api.actions["enabled"] = True
        api.codeql["state"] = "configuring"
        with self.assertRaisesRegex(configurator.ApiError, "state"):
            configurator.reconcile_codeql(api, "acme/project", apply=True)
        self.assertEqual(api.writes, [])

    def test_empty_languages_blocks_enablement(self):
        api = FakeApi()
        api.actions["enabled"] = True
        api.codeql["languages"] = []
        with self.assertRaisesRegex(configurator.ApiError, "no CodeQL-supported languages"):
            configurator.reconcile_codeql(api, "acme/project", apply=True)
        self.assertEqual(api.writes, [])

    def test_extended_query_suite_request(self):
        api = FakeApi()
        api.actions["enabled"] = True
        changes = configurator.reconcile_codeql(api, "acme/project", apply=False, query_suite="extended")
        self.assertEqual(changes, [("PATCH", "/repos/acme/project/code-scanning/default-setup", {"state": "configured", "query_suite": "extended"})])
        self.assertEqual(api.writes, [])


class GateTests(unittest.TestCase):
    def test_cli_help_does_not_offer_bespoke_gate_audit(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as exit_status:
            configurator.main(["--repo", "acme/project", "--help"])
        self.assertEqual(exit_status.exception.code, 0)
        self.assertNotIn("gates-audit", output.getvalue())

    CHECKS = {"ci-check": ".github/workflows/ci.yml"}

    class IssueGateApi(FakeApi):
        def __init__(self):
            super().__init__()
            self.repo["default_branch"] = "main"
            self.issue_check = {"name": "Issue State Gate", "head_sha": "head-1",
                                "status": "completed", "conclusion": "success",
                                "check_suite": {"id": 77}, "app": {"id": 991}}

        def request(self, method, path, payload=None):
            base = "/repos/acme/project"
            if path == base + "/actions/workflows?per_page=100&page=1":
                return 200, {"workflows": [{"id": 31, "path": ".github/workflows/issue-state-gate.yml", "state": "active"}]}
            if path == base + "/pulls?state=open&base=main&per_page=100":
                return 200, [{"number": 4, "head": {"sha": "head-1"}}]
            if path == base + "/commits/head-1/check-runs?per_page=100&page=1":
                return 200, {"check_runs": [self.issue_check]}
            if path == base + "/actions/workflows/31/runs?head_sha=head-1&event=pull_request_target&per_page=100&page=1":
                return 200, {"workflow_runs": [{"workflow_id": 31, "head_sha": "head-1", "event": "pull_request_target", "status": "completed", "conclusion": "success", "check_suite_id": 77}]}
            if path == base + "/rulesets?includes_parents=false&per_page=100":
                if method == "POST":
                    self.writes.append((method, path, payload))
                    stored = dict(payload, id=42)
                    stored["rules"] = [dict(rule, parameters={
                        **rule["parameters"],
                        **({"do_not_enforce_on_create": False}
                           if rule["type"] == "required_status_checks" else {}),
                    }) for rule in payload["rules"]]
                    self.rulesets.append(stored)
                    return 201, self.rulesets[-1]
                return 200, self.rulesets
            if path == base + "/rulesets/42":
                if method == "PUT":
                    self.writes.append((method, path, payload))
                    self.rulesets[0].update(payload)
                return 200, self.rulesets[0]
            return super().request(method, path, payload)

    def test_issue_state_gate_uses_observed_check_source_and_readback(self):
        api = self.IssueGateApi()
        changes = configurator.reconcile_issue_state_gate(api, "acme/project", apply=False)
        rules = changes[0][2]["rules"]
        self.assertEqual(rules[0], {"type": "pull_request", "parameters": {
            "required_approving_review_count": 0,
            "dismiss_stale_reviews_on_push": False,
            "require_code_owner_review": False,
            "require_last_push_approval": False,
            "required_review_thread_resolution": False,
            "required_reviewers": [],
            "require_extra_approval_for_unattributed_changes": False,
            "allowed_merge_methods": ["merge", "squash", "rebase"],
        }})
        self.assertEqual(rules[1], {"type": "required_status_checks", "parameters": {
            "strict_required_status_checks_policy": False,
            "required_status_checks": [{"context": "Issue State Gate", "integration_id": 991}],
        }})
        self.assertEqual(api.writes, [])
        configurator.reconcile_issue_state_gate(api, "acme/project", apply=True)
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(configurator.reconcile_issue_state_gate(api, "acme/project", apply=True), [])
        self.assertEqual(len(api.writes), 1)

    def test_issue_state_gate_defers_without_exact_successful_source(self):
        api = self.IssueGateApi()
        api.issue_check["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "unsupported or no successful exact-source"):
            configurator.reconcile_issue_state_gate(api, "acme/project", apply=True)
        self.assertEqual(api.writes, [])

    def test_issue_state_gate_accepts_successful_workflow_job_and_exact_app_check_on_same_head(self):
        class SeparateCheckSuiteApi(self.IssueGateApi):
            def request(self, method, path, payload=None):
                status, body = super().request(method, path, payload)
                if path.endswith("event=pull_request_target&per_page=100&page=1"):
                    body["workflow_runs"][0]["check_suite_id"] = 991
                return status, body

        api = SeparateCheckSuiteApi()

        changes = configurator.reconcile_issue_state_gate(api, "acme/project", apply=False)

        self.assertEqual(changes[0][2]["rules"][1]["parameters"]["required_status_checks"], [
            {"context": "Issue State Gate", "integration_id": 991}
        ])

    def test_issue_state_gate_refuses_unmanaged_rule_conflict(self):
        api = self.IssueGateApi()
        api.rulesets = [{"id": 42, "name": "issue-state-gate", "target": "branch",
                         "enforcement": "active", "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
                         "bypass_actors": [], "rules": [{"type": "required_signatures"}] }]
        with self.assertRaisesRegex(configurator.ApiError, "unmanaged protections"):
            configurator.reconcile_issue_state_gate(api, "acme/project", apply=True)
        self.assertEqual(api.writes, [])

    def test_issue_state_gate_upgrades_legacy_status_only_rule_without_strict_sync(self):
        api = self.IssueGateApi()
        current = configurator.reconcile_issue_state_gate(api, "acme/project")[0][2]
        current["rules"] = [current["rules"][1]]
        current["rules"][0]["parameters"]["strict_required_status_checks_policy"] = True
        api.rulesets = [dict(current, id=42)]

        changes = configurator.reconcile_issue_state_gate(api, "acme/project", apply=False)

        self.assertEqual(changes[0][0], "PUT")
        desired = changes[0][2]
        self.assertEqual(desired["rules"][0]["parameters"]["required_approving_review_count"], 0)
        self.assertFalse(desired["rules"][1]["parameters"]["strict_required_status_checks_policy"])

    def test_issue_state_gate_removes_managed_reviewer_requirement(self):
        api = self.IssueGateApi()
        current = configurator.reconcile_issue_state_gate(api, "acme/project")[0][2]
        current["rules"][0]["parameters"]["required_approving_review_count"] = 1
        api.rulesets = [dict(current, id=42)]

        changes = configurator.reconcile_issue_state_gate(api, "acme/project", apply=False)

        self.assertEqual(changes[0][0], "PUT")
        self.assertEqual(changes[0][2]["rules"][0]["parameters"]["required_approving_review_count"], 0)
        self.assertEqual(api.writes, [])

    def test_issue_state_gate_refuses_unknown_existing_pull_request_parameter(self):
        api = self.IssueGateApi()
        current = configurator.reconcile_issue_state_gate(api, "acme/project")[0][2]
        current["rules"][0]["parameters"]["custom_unmanaged_parameter"] = True
        api.rulesets = [dict(current, id=42)]

        with self.assertRaisesRegex(configurator.ApiError, "unmanaged parameters"):
            configurator.reconcile_issue_state_gate(api, "acme/project", apply=True)
        self.assertEqual(api.writes, [])


    def test_gates_cli_reports_unverified_when_write_does_not_converge(self):
        class IgnoringApi(FakeApi):
            def request(self, method, path, payload=None):
                if method == "POST" and path == "/repos/acme/project/rulesets":
                    self.writes.append((method, path, payload))
                    return 201, dict(payload, id=42)
                return super().request(method, path, payload)

        api = IgnoringApi()
        argv = ["--repo", "acme/project", "--apply", "gates", "--name", "quality-gate",
                "--evidence-pr", "7", "--reviewer", "reviewer",
                "--check", ".github/workflows/ci.yml=ci-check"]
        with patch.object(configurator, "GitHubApi", return_value=api), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "test-only"}), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertNotEqual(configurator.main(argv), 0)
        self.assertEqual(len(api.writes), 1)

    def test_requires_proven_successful_check_before_creation(self):
        api = FakeApi()
        with self.assertRaisesRegex(ValueError, "missing successful check"):
            configurator.reconcile_gates(api, "acme/project", "quality-gate", 7, "reviewer", {"unknown": ".github/workflows/ci.yml"}, apply=True)
        self.assertEqual(api.writes, [])

    def test_creates_once_then_no_op(self):
        api = FakeApi()
        configurator.reconcile_gates(api, "acme/project", "quality-gate", 7, "reviewer", self.CHECKS, apply=True)
        api.writes.clear()
        self.assertEqual(configurator.reconcile_gates(api, "acme/project", "quality-gate", 7, "reviewer", self.CHECKS, apply=True), [])
        self.assertEqual(api.writes, [])

    def test_existing_ruleset_with_unmanaged_rule_blocks_replacement(self):
        api = FakeApi()
        configurator.reconcile_gates(api, "acme/project", "quality-gate", 7, "reviewer", self.CHECKS, apply=True)
        api.rulesets[0]["rules"].append({"type": "deletion"})
        api.writes.clear()
        with self.assertRaisesRegex(configurator.ApiError, "unmanaged"):
            configurator.reconcile_gates(
                api, "acme/project", "quality-gate", 7, "reviewer", self.CHECKS, apply=True)
        self.assertEqual(api.writes, [])
        self.assertIn({"type": "deletion"}, api.rulesets[0]["rules"])

    def test_existing_ruleset_with_unmanaged_rule_parameter_blocks_replacement(self):
        api = FakeApi()
        configurator.reconcile_gates(api, "acme/project", "quality-gate", 7, "reviewer", self.CHECKS, apply=True)
        api.rulesets[0]["rules"][0]["parameters"]["require_extra_approval_for_unattributed_changes"] = True
        api.writes.clear()
        with self.assertRaisesRegex(configurator.ApiError, "unmanaged"):
            configurator.reconcile_gates(
                api, "acme/project", "quality-gate", 7, "reviewer", self.CHECKS, apply=True)
        self.assertEqual(api.writes, [])

    def test_ruleset_lookup_excludes_parent_rules(self):
        api = FakeApi()
        configurator.reconcile_gates(api, "acme/project", "quality-gate", 7, "reviewer", self.CHECKS, apply=False)
        self.assertIn(("GET", "/repos/acme/project/rulesets?includes_parents=false&per_page=100", None), api.reads)

    def test_ruleset_name_is_supplied_by_caller(self):
        api = FakeApi()
        changes = configurator.reconcile_gates(api, "acme/project", "my-custom-policy", 7, "reviewer", self.CHECKS)
        self.assertEqual(changes[0][2]["name"], "my-custom-policy")

    def test_rejects_success_from_unrelated_workflow(self):
        api = FakeApi()
        api.workflow_runs[0]["check_suite_id"] = 999
        with self.assertRaisesRegex(ValueError, "missing successful check"):
            configurator.reconcile_gates(api, "acme/project", "quality-gate", 7, "reviewer", self.CHECKS, apply=True)
        self.assertEqual(api.writes, [])

    def test_rejects_unapproved_reviewer(self):
        api = FakeApi()
        api.reviews = []
        with self.assertRaisesRegex(ValueError, "approval"):
            configurator.reconcile_gates(api, "acme/project", "quality-gate", 7, "reviewer", self.CHECKS, apply=True)
        self.assertEqual(api.writes, [])

    def test_rejects_inactive_workflow(self):
        api = FakeApi()
        with self.assertRaisesRegex(ValueError, "installed workflow"):
            configurator.reconcile_gates(api, "acme/project", "quality-gate", 7, "reviewer", {"ci-check": ".github/workflows/missing.yml"}, apply=True)
        self.assertEqual(api.writes, [])

    def test_rejects_stale_approval(self):
        api = FakeApi()
        api.reviews[0]["commit_id"] = "older"
        with self.assertRaisesRegex(ValueError, "approval"):
            configurator.reconcile_gates(api, "acme/project", "quality-gate", 7, "reviewer", self.CHECKS, apply=True)
        self.assertEqual(api.writes, [])

    def test_private_free_plan_blocks_ruleset(self):
        api = FakeApi()
        api.repo.update({"visibility": "private", "owner": {"login": "acme", "type": "Organization"}})
        original = api.request
        def request(method, path, payload=None):
            if path == "/orgs/acme":
                return 200, {"plan": {"name": "free"}}
            return original(method, path, payload)
        api.request = request
        with self.assertRaisesRegex(configurator.ApiError, "Pro, Team, or Enterprise"):
            configurator.reconcile_gates(api, "acme/project", "quality-gate", 7, "reviewer", self.CHECKS, apply=True)
        self.assertEqual(api.writes, [])

    def test_failed_check_blocks_ruleset(self):
        api = FakeApi()
        api.check_runs[0]["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "missing successful check"):
            configurator.reconcile_gates(api, "acme/project", "quality-gate", 7, "reviewer", self.CHECKS, apply=True)
        self.assertEqual(api.writes, [])

    def test_check_app_is_pinned_in_ruleset(self):
        api = FakeApi()
        changes = configurator.reconcile_gates(api, "acme/project", "quality-gate", 7, "reviewer", self.CHECKS)
        checks = changes[0][2]["rules"][1]["parameters"]["required_status_checks"]
        self.assertEqual(checks, [{"context": "ci-check", "integration_id": 15368}])

if __name__ == "__main__":
    unittest.main()
