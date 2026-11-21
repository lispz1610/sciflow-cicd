"""Behavior tests for inventory and policy execution."""

import importlib.util
import base64
import io
import json
import os
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from test_configure_github_repo import FakeApi


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup_github_repo.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("setup_github_repo", SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class InventoryApi(FakeApi):
    def __init__(self):
        super().__init__()
        self.labels = {}

    def request(self, method, path, payload=None):
        if method == "GET" and path == "/repos/acme/project/code-scanning/default-setup":
            return 200, self.codeql
        if path.startswith("/repos/acme/project/rulesets?includes_parents=false&per_page=100&page="):
            return 200, self.rulesets
        if "/labels/" in path:
            name = path.split("/labels/")[-1]
            if name in self.labels:
                return 200, self.labels[name]
            return 404, None
        if method == "POST" and path.endswith("/labels"):
            self.writes.append((method, path, payload))
            if isinstance(payload, dict) and "name" in payload:
                self.labels[payload["name"]] = payload
            return 201, payload
        return super().request(method, path, payload)


class InventoryTests(unittest.TestCase):
    def test_inventory_reports_observed_values_and_unknown_optional_state(self):
        api = InventoryApi()
        api.repo["full_name"] = "acme/project"
        api.repo["visibility"] = "private"
        result = setup.inventory(api, "acme/project")
        self.assertEqual(result["repository"]["default_branch"], "master")
        self.assertEqual(result["features"]["wiki"], False)
        self.assertEqual(result["actions"]["enabled"], False)
        self.assertEqual(result["codeql"]["state"], "not-configured")
        self.assertEqual(result["dependency_graph"]["status"], "disabled")
        self.assertEqual(result["wiki_setup"]["editing_restriction"]["status"], "unsupported")

    def test_inventory_does_not_claim_known_status_after_permission_error(self):
        class DeniedApi(InventoryApi):
            def request(self, method, path, payload=None):
                if path.endswith("/code-scanning/default-setup"):
                    raise setup.ApiError("HTTP 403")
                return super().request(method, path, payload)

        result = setup.inventory(DeniedApi(), "acme/project")
        self.assertEqual(result["codeql"]["status"], "inaccessible")

    def test_inventory_paginates_rulesets(self):
        class PagedApi(InventoryApi):
            def request(self, method, path, payload=None):
                if "rulesets?includes_parents=false&per_page=100&page=1" in path:
                    return 200, [{"id": i, "name": f"gate-{i}"} for i in range(100)]
                if "rulesets?includes_parents=false&per_page=100&page=2" in path:
                    return 200, [{"id": 100, "name": "last"}]
                return super().request(method, path, payload)

        result = setup.inventory(PagedApi(), "acme/project")
        self.assertEqual(len(result["rulesets"]), 101)

    def test_inventory_detects_managed_agent_skill_bundle_for_resume_state(self):
        class InstalledSkillsApi(InventoryApi):
            def request(self, method, path, payload=None):
                if path.startswith("/repos/acme/project/contents/.agents/skills/manifest.json?ref="):
                    manifest = json.dumps({"schema_version": 1, "managed_by": "sciflow-cicd",
                                           "bundle": "mattpocock"}).encode()
                    return 200, {"encoding": "base64", "content": base64.b64encode(manifest).decode()}
                return super().request(method, path, payload)

        result = setup.inventory(InstalledSkillsApi(), "acme/project")
        self.assertTrue(result["agent_skills"])


class PolicyTests(unittest.TestCase):
    def test_issue_state_gate_policy_reports_source_and_verifies_active_rule(self):
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
                        self.rulesets.append(dict(payload, id=42))
                        return 201, self.rulesets[-1]
                    return 200, self.rulesets
                if path == base + "/rulesets/42":
                    return 200, self.rulesets[0]
                return super().request(method, path, payload)

        api = IssueGateApi()
        policy = {"version": 2, "stages": [{"name": "issue-state-gate"}]}
        with patch.object(setup, "install_selected", return_value={"changes": []}):
            dry_run = setup.run_policy(api, "acme/project", policy)
            self.assertFalse(api.writes)
            source_report = next(change for change in dry_run["changes"]
                                 if change.get("operation") == "inspect")
            self.assertEqual(source_report["source"]["integration_id"], 991)
            applied = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertTrue(any(method == "POST" and path.endswith("/rulesets")
                            for method, path, _ in api.writes))
        self.assertEqual(next(change for change in applied["changes"]
                              if change.get("operation") == "inspect")["status"], "verified")

    def test_issue_state_gate_stages_trusted_workflow_and_defers_rule_without_writes(self):
        api = InventoryApi()
        policy = {"version": 2, "stages": [{"name": "issue-state-gate"}]}
        with patch.object(setup, "install_selected", return_value={
            "changes": [{"path": ".github/workflows/issue-state-gate.yml", "desired_sha256": "abc"}],
            "status": "planned",
        }):
            preview = setup.run_policy(api, "acme/project", policy)
        gate = next(change for change in preview["changes"] if change["stage"] == "issue-state-gate")
        self.assertEqual(gate["status"], "pending")
        self.assertEqual(gate["workflow"], ".github/workflows/issue-state-gate.yml")
        self.assertEqual(gate["context"], "Issue State Gate")
        self.assertIn("integration ID", gate["source"])
        self.assertEqual(api.writes, [])

    def test_issue_state_gate_policy_rejects_unknown_keys(self):
        with self.assertRaises(ValueError):
            setup.validate_policy({"version": 1, "stages": [{"name": "issue-state-gate", "reviewer": "someone"}]})

    def test_issue_state_gate_missing_read_permission_reports_blocked_without_writes(self):
        class DeniedApi(InventoryApi):
            def request(self, method, path, payload=None):
                if path == "/repos/acme/project/actions/workflows?per_page=100&page=1":
                    raise setup.ApiError("HTTP 403")
                return super().request(method, path, payload)

        api = DeniedApi()
        api.repo["default_branch"] = "main"
        with patch.object(setup, "install_selected", return_value={"changes": []}):
            result = setup.run_policy(api, "acme/project", {"version": 1, "stages": [{"name": "issue-state-gate"}]})
        issue_gate = next(change for change in result["changes"] if change["stage"] == "issue-state-gate")
        self.assertEqual(issue_gate["status"], "blocked")
        self.assertIn("administration permission", issue_gate["prerequisites"][-2])
        self.assertEqual(api.writes, [])

    def test_new_ci_workflow_leaves_gate_pending_while_direct_settings_apply(self):
        api = InventoryApi()
        policy = {"version": 1, "stages": [
            {"name": "gates", "ruleset_name": "quality-gate", "evidence_pr": 7,
             "reviewer": "reviewer", "checks": {"ci-check": ".github/workflows/ci.yml"}},
            {"name": "features", "enable": ["wiki"]},
            {"name": "workflows", "workflows": [{"template": "pr-ci"}]},
        ]}
        planned_workflow = {"path": ".github/workflows/ci.yml", "desired_sha256": "abc"}
        with patch.object(setup, "install_selected", side_effect=[
            {"changes": [planned_workflow]}, {"changes": [planned_workflow]},
            {"changes": [planned_workflow]}, {"changes": [planned_workflow]},
            {"changes": [planned_workflow], "pull_request": "https://github.com/acme/project/pull/8"},
        ]):
            dry_run = setup.run_policy(api, "acme/project", policy)
            self.assertEqual(api.writes, [])
            gate = next(c for c in dry_run["changes"] if c["stage"] == "gates")
            self.assertEqual(gate["status"], "pending")
            self.assertIn("merge", gate["reason"].lower())
            self.assertIn("successful", gate["reason"].lower())
            applied = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(next(c for c in applied["changes"] if c["stage"] == "gates")["status"], "pending")
        self.assertEqual(next(c for c in applied["changes"] if c["stage"] == "features")["status"], "verified")
        self.assertFalse(any("rulesets" in path for _, path, _ in api.writes))

    def test_installed_ci_with_successful_evidence_allows_gate(self):
        api = InventoryApi()
        policy = {"version": 1, "stages": [{"name": "gates", "evidence_pr": 7,
            "reviewer": "reviewer", "checks": {"ci-check": ".github/workflows/ci.yml"}}]}
        dry_run = setup.run_policy(api, "acme/project", policy)
        result = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(result["changes"][0]["status"], "verified")
        self.assertTrue(any(method == "POST" and path.endswith("/rulesets")
                            for method, path, _ in api.writes))

    def test_matching_open_setup_pr_keeps_gate_pending(self):
        api = InventoryApi()
        policy = {"version": 1, "stages": [
            {"name": "workflows", "workflows": [{"filename": "ci.yml"}]},
            {"name": "gates", "evidence_pr": 7, "reviewer": "reviewer",
             "checks": {"ci-check": ".github/workflows/ci.yml"}},
        ]}
        with patch.object(setup, "install_selected", return_value={
            "status": "pending_review", "changes": [],
            "pull_request": "https://github.com/acme/project/pull/8",
        }):
            result = setup.run_policy(api, "acme/project", policy)
        self.assertEqual(result["changes"][0]["status"], "pending")
        self.assertEqual(result["setup_pull_request"], "https://github.com/acme/project/pull/8")
        self.assertEqual(api.writes, [])

    def test_failed_ci_evidence_blocks_gate_but_allows_unrelated_setting(self):
        api = InventoryApi()
        api.check_runs[0]["conclusion"] = "failure"
        policy = {"version": 1, "stages": [
            {"name": "gates", "evidence_pr": 7, "reviewer": "reviewer",
             "checks": {"ci-check": ".github/workflows/ci.yml"}},
            {"name": "features", "enable": ["wiki"]},
        ]}
        result = setup.run_policy(api, "acme/project", policy, apply=True)
        self.assertEqual([c["status"] for c in result["changes"]], ["blocked", "planned"])
        self.assertIn("missing successful check", result["changes"][0]["reason"])
        self.assertEqual(api.writes, [])
        self.assertFalse(any("rulesets" in path for _, path, _ in api.writes))

    def test_advisory_review_cannot_be_required_check(self):
        api = InventoryApi()
        policy = {"version": 1, "stages": [
            {"name": "features", "enable": ["wiki"]},
            {"name": "gates", "evidence_pr": 7, "reviewer": "reviewer",
             "checks": {"review": ".github/workflows/open-code-review.yml"}},
        ]}
        with self.assertRaisesRegex(ValueError, "Advisory Review"):
            setup.run_policy(api, "acme/project", policy, apply=True)
        self.assertEqual(api.writes, [])

    def test_cli_help_does_not_offer_codeql_analysis_audit(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as exit_status:
            setup.main(["--repo", "acme/project", "--help"])
        self.assertEqual(exit_status.exception.code, 0)
        self.assertNotIn("codeql-audit", output.getvalue())

    def test_documented_example_runs_through_cli_without_writes(self):
        api = InventoryApi()
        example = Path(__file__).resolve().parents[1] / "examples" / "setup-policy.json"
        output = io.StringIO()
        with patch("scripts.headless.GitHubApi", return_value=api), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "test-only"}), redirect_stdout(output):
            code = setup.main(["--repo", "acme/project", "policy", "--file", str(example)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "dry-run")
        self.assertEqual(api.writes, [])

    def test_policy_plan_contains_current_and_desired_without_writes(self):
        api = InventoryApi()
        result = setup.run_policy(api, "acme/project", {"version": 1, "stages": [
            {"name": "features", "enable": ["wiki"]},
        ]})
        self.assertEqual(result["changes"][0]["current"], {"has_wiki": False})
        self.assertEqual(result["changes"][0]["desired"], {"has_wiki": True})
        self.assertEqual(result["changes"][0]["status"], "planned")
        self.assertEqual(api.writes, [])

    def test_policy_validates_all_stages_before_writes(self):
        api = InventoryApi()
        policy = {"version": 1, "stages": [
            {"name": "features", "enable": ["wiki"]},
            {"name": "security", "enable": ["nonexistent"]},
        ]}
        with self.assertRaises(ValueError):
            setup.run_policy(api, "acme/project", policy, apply=True)
        self.assertEqual(api.writes, [])

    def test_links_stage_is_rejected_before_any_policy_write(self):
        api = InventoryApi()
        policy = {"version": 1, "stages": [
            {"name": "features", "enable": ["wiki"]},
            {"name": "links", "entries": [{"issue": 7, "key": "planning", "links": [
                {"label": "Spec", "url": "https://github.com/acme/project/wiki/Spec-7"}]}]},
        ]}
        with self.assertRaisesRegex(ValueError, "unsupported stage"):
            setup.run_policy(api, "acme/project", policy, apply=True)
        self.assertEqual(api.writes, [])

    def test_second_policy_apply_has_no_changes(self):
        api = InventoryApi()
        policy = {"version": 1, "stages": [{"name": "features", "enable": ["wiki"]}]}
        dry_run = setup.run_policy(api, "acme/project", policy)
        setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        api.writes.clear()
        dry_run2 = setup.run_policy(api, "acme/project", policy)
        result = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run2)
        self.assertEqual(result["changes"], [])
        self.assertEqual(api.writes, [])

    def test_unknown_policy_key_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            setup.validate_policy({"version": 1, "stages": [{"name": "features", "enable": ["wiki"], "typo": True}]})

    def test_features_stage_with_disable_is_valid(self):
        policy = {
            "version": 1,
            "stages": [{"name": "features", "enable": ["issues"], "disable": ["wiki"]}],
        }
        validated = setup.validate_policy(policy)
        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]["disable"], ["wiki"])

    def test_features_stage_with_overlapping_enable_and_disable_is_rejected(self):
        policy = {
            "version": 1,
            "stages": [{"name": "features", "enable": ["wiki"], "disable": ["wiki"]}],
        }
        with self.assertRaisesRegex(ValueError, "both enabled and disabled"):
            setup.validate_policy(policy)

    def test_actions_policy_is_verified_after_apply(self):
        api = InventoryApi()
        policy = {"version": 1, "stages": [{"name": "actions-policy", "settings": {
            "allowed_actions": "local_only", "sha_pinning_required": True}}]}
        dry_run = setup.run_policy(api, "acme/project", policy)
        result = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual([change["status"] for change in result["changes"]], ["verified"])
        dry_run2 = setup.run_policy(api, "acme/project", policy)
        self.assertEqual(setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run2)["changes"], [])

    def test_selected_actions_policy_applies_dependent_requests_once(self):
        api = InventoryApi()
        policy = {"version": 1, "stages": [{"name": "actions-policy", "settings": {
            "allowed_actions": "selected", "github_owned_allowed": True,
            "verified_allowed": False, "patterns_allowed": []}}]}
        dry_run = setup.run_policy(api, "acme/project", policy)
        result = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertFalse(any(change["status"] == "failed" for change in result["changes"]))
        self.assertEqual(len(api.writes), 2)

    def test_ignored_setting_is_reported_unverified(self):
        class IgnoringApi(InventoryApi):
            def request(self, method, path, payload=None):
                if method == "PATCH" and path == "/repos/acme/project":
                    self.writes.append((method, path, payload))
                    return 200, self.repo
                return super().request(method, path, payload)

        api = IgnoringApi()
        policy = {"version": 1, "stages": [{"name": "features", "enable": ["wiki"]}]}
        dry_run = setup.run_policy(api, "acme/project", policy)
        result = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(result["changes"][0]["status"], "unverified")

    def test_dependency_graph_plan_discloses_alerts_coupling(self):
        api = InventoryApi()
        api.repo["visibility"] = "private"
        result = setup.run_policy(api, "acme/project", {"version": 1, "stages": [
            {"name": "dependency-graph"}]})
        self.assertEqual(result["changes"][0]["path"], "/repos/acme/project/vulnerability-alerts")
        self.assertIn("alerts", result["changes"][0]["reason"])
        self.assertEqual(api.writes, [])

    def test_dependency_graph_is_already_enabled_for_public_repository(self):
        api = InventoryApi()
        dry_run = setup.run_policy(api, "acme/project", {"version": 1, "stages": [
            {"name": "dependency-graph"}]})
        result = setup.run_policy(api, "acme/project", {"version": 1, "stages": [
            {"name": "dependency-graph"}]}, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(result["changes"], [])
        self.assertEqual(api.writes, [])

    def test_private_dependency_graph_apply_converges(self):
        api = InventoryApi()
        api.repo["visibility"] = "private"
        policy = {"version": 1, "stages": [{"name": "dependency-graph"}]}
        dry_run = setup.run_policy(api, "acme/project", policy)
        first = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(first["changes"][0]["status"], "verified")
        dry_run2 = setup.run_policy(api, "acme/project", policy)
        self.assertEqual(setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run2)["changes"], [])

    def test_unavailable_security_feature_blocks_before_any_write(self):
        class DeniedApi(InventoryApi):
            def request(self, method, path, payload=None):
                if path.endswith("/code-scanning/default-setup"):
                    raise setup.ApiError("HTTP 403: Code Security unavailable")
                return super().request(method, path, payload)

        api = DeniedApi()
        result = setup.run_policy(api, "acme/project", {"version": 1, "stages": [
            {"name": "features", "enable": ["wiki"]}, {"name": "codeql"}]}, apply=True)
        self.assertEqual(result["changes"][-1]["status"], "blocked")
        self.assertEqual(api.writes, [])

    def test_actions_and_codeql_can_be_planned_together_before_writes(self):
        api = InventoryApi()
        policy = {"version": 1, "stages": [
            {"name": "actions"}, {"name": "codeql"}]}
        plan = setup.run_policy(api, "acme/project", policy)
        self.assertEqual([change["stage"] for change in plan["changes"]],
                         ["actions", "actions", "codeql"])
        self.assertEqual(api.writes, [])
        result = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=plan)
        self.assertEqual(result["changes"][-1]["status"], "pending")
        self.assertEqual(result["changes"][-1]["validation_run_id"], 42)

    def test_codeql_write_without_validation_run_stays_unverified(self):
        class NoRunApi(InventoryApi):
            def request(self, method, path, payload=None):
                if method == "PATCH" and path.endswith("/code-scanning/default-setup"):
                    self.codeql.update(payload)
                    self.writes.append((method, path, payload))
                    return 200, {"state": "configured"}
                return super().request(method, path, payload)

        api = NoRunApi()
        api.actions["enabled"] = True
        policy = {"version": 1, "stages": [{"name": "codeql"}]}
        dry_run = setup.run_policy(api, "acme/project", policy)
        result = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(result["changes"][0]["status"], "unverified")

    def test_partial_apply_verifies_completed_write(self):
        class FailingApi(InventoryApi):
            def request(self, method, path, payload=None):
                if method == "PUT" and path.endswith("/actions/permissions/workflow"):
                    raise setup.ApiError("HTTP 409: organization policy")
                return super().request(method, path, payload)

        api = FailingApi()
        policy = {"version": 1, "stages": [{"name": "actions"}]}
        dry_run = setup.run_policy(api, "acme/project", policy)
        result = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual([change["status"] for change in result["changes"]], ["verified", "failed"])
        self.assertIn("new Dry-Run", result["recovery"])
        self.assertTrue(api.actions["enabled"])

    def test_first_write_failure_is_structured_and_retry_recovers(self):
        class FailingOnceApi(InventoryApi):
            fail = True

            def request(self, method, path, payload=None):
                if method == "PUT" and path.endswith("/actions/permissions") and self.fail:
                    self.fail = False
                    raise setup.ApiError("HTTP 503: retry later")
                return super().request(method, path, payload)

        api = FailingOnceApi()
        policy = {"version": 1, "stages": [{"name": "actions"}]}
        dry_run = setup.run_policy(api, "acme/project", policy)
        first = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual([c["status"] for c in first["changes"]], ["failed", "pending"])
        self.assertIn("new Dry-Run", first["recovery"])
        self.assertEqual(api.writes, [])
        dry_run2 = setup.run_policy(api, "acme/project", policy)
        second = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run2)
        self.assertEqual([c["status"] for c in second["changes"]], ["verified", "verified"])
        api.writes.clear()
        dry_run3 = setup.run_policy(api, "acme/project", policy)
        self.assertEqual(setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run3)["changes"], [])
        self.assertEqual(api.writes, [])

    def test_changed_state_after_preflight_blocks_before_write(self):
        class ChangingApi(InventoryApi):
            repository_reads = 0

            def request(self, method, path, payload=None):
                if method == "GET" and path == "/repos/acme/project":
                    self.repository_reads += 1
                    if self.repository_reads == 5:
                        self.repo["has_issues"] = True
                return super().request(method, path, payload)

        api = ChangingApi()
        policy = {"version": 1, "stages": [{"name": "features", "enable": ["issues"]}]}
        dry_run = setup.run_policy(api, "acme/project", policy)
        result = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(result["changes"][0]["status"], "failed")
        self.assertEqual(api.writes, [])

    def test_successful_write_with_unreadable_readback_is_unverified(self):
        class ReadbackDeniedApi(InventoryApi):
            def request(self, method, path, payload=None):
                if method == "GET" and path == "/repos/acme/project" and self.writes:
                    raise setup.ApiError("HTTP 403: readback denied")
                return super().request(method, path, payload)

        api = ReadbackDeniedApi()
        policy = {"version": 1, "stages": [{"name": "features", "enable": ["wiki"]}]}
        dry_run = setup.run_policy(api, "acme/project", policy)
        result = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(result["changes"][0]["status"], "unverified")
        self.assertIn("readback denied", result["changes"][0]["reason"])
        self.assertEqual(len(api.writes), 1)

    def test_duplicate_alert_and_graph_selection_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "same endpoint"):
            setup.validate_policy({"version": 1, "stages": [
                {"name": "dependency-graph"},
                {"name": "security", "enable": ["vulnerability-alerts"]},
            ]})

    def test_stage_changes_handles_gates_stage_with_dict_and_list_checks(self):
        api = InventoryApi()
        stage_dict = {
            "name": "gates",
            "ruleset_name": "quality-gate",
            "evidence_pr": 7,
            "reviewer": "reviewer",
            "checks": {"ci-check": ".github/workflows/ci.yml"},
        }
        changes_dict = setup._stage_changes(api, "acme/project", stage_dict)
        self.assertEqual(len(changes_dict), 1)
        self.assertEqual(changes_dict[0][0], "POST")
        self.assertEqual(changes_dict[0][1], "/repos/acme/project/rulesets")

        stage_list_str = {
            "name": "gates",
            "ruleset_name": "quality-gate",
            "evidence_pr": 7,
            "reviewer": "reviewer",
            "checks": [".github/workflows/ci.yml=ci-check"],
        }
        changes_list = setup._stage_changes(api, "acme/project", stage_list_str)
        self.assertEqual(len(changes_list), 1)

        stage_list_dict = {
            "name": "gates",
            "ruleset_name": "quality-gate",
            "evidence_pr": 7,
            "reviewer": "reviewer",
            "checks": [{"name": "ci-check", "path": ".github/workflows/ci.yml"}],
        }
        changes_dict_list = setup._stage_changes(api, "acme/project", stage_list_dict)
        self.assertEqual(len(changes_dict_list), 1)

    def test_stage_changes_handles_workflows_stage(self):
        api = InventoryApi()
        stage = {
            "name": "workflows",
            "workflows": [
                {
                    "template": "pr-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": "11bd71901bbe5b1630ceea73d27597364c9af683",
                    "commands": ["pytest"],
                }
            ],
        }
        with patch.object(setup, "install", return_value={"changes": [
            {"path": ".github/workflows/ci.yml", "desired_sha256": "abc123hash"}
        ]}) as mock_install:
            changes = setup._stage_changes(api, "acme/project", stage)
            mock_install.assert_called_once_with(
                api, "acme/project", {"version": 1, "workflows": stage["workflows"]}, apply=False
            )
            self.assertEqual(changes, [
                ("PUT", ".github/workflows/ci.yml", {"message": "Install workflow", "content": "abc123hash"})
            ])

    def test_stage_changes_unsupported_stage_raises_value_error(self):
        api = InventoryApi()
        with self.assertRaisesRegex(ValueError, "unsupported stage"):
            setup._stage_changes(api, "acme/project", {"name": "unsupported-dummy"})


if __name__ == "__main__":
    unittest.main()
