"""Tests for P6.6 (#184): Verify security and merge-setting tutorials."""

import io
import json
from pathlib import Path
import re
import sys
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
TESTS_DIR = ROOT_DIR / "tests"
for d in (str(ROOT_DIR), str(SCRIPTS_DIR), str(TESTS_DIR)):
    if d not in sys.path:
        sys.path.insert(0, d)

from configure_github_repo import (
    ApiError,
    reconcile_actions,
    reconcile_actions_policy,
    reconcile_codeql,
    reconcile_gates,
    reconcile_merge,
    reconcile_security,
)
from dependabot_generator import render_dependabot_config, validate_dependabot_stage
from doctor import audit_repository_controls, format_doctor_report, run_doctor
from install_github_workflows import DEFAULT_CHECKOUT_SHA, DEFAULT_DEPENDENCY_SHA
from interactive_workflows_rulesets import prompt_rulesets_stage
from repository_capabilities import classify_capabilities, evaluate_repository_capabilities
from setup_github_repo import inventory, run_policy, validate_policy
from test_p3_e2e_validation import P3DisposableRepoApi

PROPORTIONAL_SECURITY_DOC = ROOT_DIR / "docs" / "proportional-security-and-controls.md"
MERGE_GATES_DOC = ROOT_DIR / "docs" / "merge-gates.md"
README_DOC = ROOT_DIR / "README.md"


class P6SecurityAndMergeTutorialsTests(unittest.TestCase):
    """End-to-end documentation and implementation contract tests for P6.6 (#184)."""

    def setUp(self):
        self.security_doc = PROPORTIONAL_SECURITY_DOC.read_text(encoding="utf-8")
        self.merge_doc = MERGE_GATES_DOC.read_text(encoding="utf-8")
        self.readme_doc = README_DOC.read_text(encoding="utf-8")

    # -------------------------------------------------------------------------
    # Slice 1 (AC 1): Exact GitHub UI navigation and options across docs
    # -------------------------------------------------------------------------
    def test_ac1_exact_github_ui_navigation_paths_and_options(self):
        """AC 1: docs/proportional-security-and-controls.md, docs/merge-gates.md, and README.md
        name the exact GitHub UI navigation and options for all 7 required control surfaces.
        """
        combined = "\n".join([self.security_doc, self.merge_doc, self.readme_doc])

        # 1. Branch rules or protection (Rulesets + classic branch protection)
        self.assertIn("Settings -> Rules -> Rulesets", self.security_doc)
        self.assertIn("Settings -> Branches", self.security_doc)
        self.assertIn("Settings -> Rules -> Rulesets", self.merge_doc)
        self.assertIn("Settings -> Branches", self.merge_doc)
        self.assertIn("Settings -> Rules -> Rulesets", self.readme_doc)

        # 2. Pull request merge methods & head branch deletion
        self.assertIn("Settings -> General -> Pull Requests", self.security_doc)
        self.assertIn("Settings -> General -> Pull Requests", self.merge_doc)
        for merge_key in (
            "allow_merge_commit",
            "allow_squash_merge",
            "allow_rebase_merge",
            "delete_branch_on_merge",
            "allow_update_branch",
        ):
            self.assertIn(merge_key, self.security_doc)

        # 3. Actions permissions & default workflow permissions
        self.assertIn("Settings -> Actions -> General", self.security_doc)
        self.assertIn("Require actions to be pinned to a full-length commit SHA", self.security_doc)
        self.assertIn("Read repository contents and packages permissions", self.security_doc)
        self.assertIn("Allow GitHub Actions to create and approve pull requests", self.security_doc)
        self.assertIn("sha_pinning_required", self.security_doc)
        self.assertIn("default_workflow_permissions", self.security_doc)
        self.assertIn("can_approve_pull_request_reviews", self.security_doc)

        # 4. Dependabot alerts, security updates, and monthly grouped version updates
        self.assertIn("Settings -> Code security", self.security_doc)
        self.assertIn(".github/dependabot.yml", self.security_doc)
        self.assertIn("dependabot_generator.py", self.security_doc)
        self.assertIn('interval: "monthly"', self.security_doc)
        self.assertIn("open-pull-requests-limit: 5", self.security_doc)
        self.assertIn("groups:", self.security_doc)
        self.assertIn("all-dependencies", self.security_doc)
        self.assertIn("pip-dependencies", self.security_doc)

        # 5. Dependency review
        self.assertIn("Settings -> Code security -> Dependency graph", self.security_doc)
        self.assertIn(".github/workflows/dependency-review.yml", self.security_doc)

        # 6. CodeQL default setup
        self.assertIn(
            "Settings -> Code security -> Code scanning -> CodeQL analysis -> Set up -> Default",
            self.security_doc,
        )
        self.assertIn('query_suite: "default"', self.security_doc)
        self.assertIn('"extended"', self.security_doc)

        # 7. Related secrets and variables when selected
        self.assertIn("Settings -> Secrets and variables -> Actions", self.security_doc)
        self.assertIn("Settings -> Secrets and variables -> Actions", self.readme_doc)
        for token_or_var in (
            "POLICY_AUDIT_TOKEN",
            "TRIAGE_PROJECT_TOKEN",
            "OCR_LLM_AUTH_TOKEN",
            "OCR_LLM_URL",
            "OCR_LLM_MODEL",
            "OCR_LLM_USE_ANTHROPIC",
            "TRIAGE_PROJECT_ID",
        ):
            self.assertIn(
                token_or_var,
                self.security_doc,
                f"docs/proportional-security-and-controls.md must document {token_or_var}",
            )
            self.assertIn(
                token_or_var,
                combined,
                f"Combined documentation must include {token_or_var}",
            )

    # -------------------------------------------------------------------------
    # Slice 2 (AC 2): Visibility, plan, and feature availability branching
    # -------------------------------------------------------------------------
    def test_ac2_visibility_plan_and_feature_branching_in_docs_and_code(self):
        """AC 2: Instructions explicitly branch by repository visibility (public vs private),
        GitHub plan (Free vs Pro/Team/Enterprise), and feature availability; unavailable
        private-repository security features are never promised and are classified as
        [UNSUPPORTED] or [INACCESSIBLE].
        """
        for doc_name, text in (
            ("proportional-security-and-controls.md", self.security_doc),
            ("merge-gates.md", self.merge_doc),
        ):
            self.assertIn("public", text.lower(), f"{doc_name} must cover public visibility")
            self.assertIn("private", text.lower(), f"{doc_name} must cover private visibility")
            self.assertIn("Free", text, f"{doc_name} must cover Free plan")
            self.assertIn("Pro", text, f"{doc_name} must cover Pro plan")
            self.assertIn("Team", text, f"{doc_name} must cover Team plan")
            self.assertIn("Enterprise", text, f"{doc_name} must cover Enterprise plan")

        for status_tag in ("[CONFIGURED]", "[MISSING]", "[UNSUPPORTED]", "[INACCESSIBLE]"):
            self.assertIn(status_tag, self.security_doc)

        self.assertIn("no CodeQL-supported languages detected", self.security_doc)
        self.assertIn("private vulnerability reporting is available only for public repositories", self.security_doc.lower())

        # Verify code contract across 4 repository tiers:
        # Tier A: Public Free -> all capabilities supported/configured
        pub_free_api = P3DisposableRepoApi(visibility="public", plan="free", admin=True)
        pub_free_inv = inventory(pub_free_api, "acme/project")
        pub_caps = pub_free_inv["capabilities"]
        self.assertEqual(pub_caps["dependency_graph"]["status"], "configured")
        self.assertEqual(pub_caps["dependency_review"]["status"], "configured")
        self.assertEqual(pub_caps["secret_scanning"]["status"], "configured")
        self.assertEqual(pub_caps["codeql"]["status"], "missing")  # available to configure
        self.assertEqual(pub_caps["rulesets"]["status"], "missing")  # available to configure

        # Tier B: Private Free -> rulesets, secret_scanning, codeql, dependency_review are UNSUPPORTED
        priv_free_api = P3DisposableRepoApi(visibility="private", plan="free", admin=True)
        priv_free_inv = inventory(priv_free_api, "acme/project")
        priv_free_caps = priv_free_inv["capabilities"]
        self.assertEqual(priv_free_caps["rulesets"]["status"], "unsupported")
        self.assertEqual(priv_free_caps["secret_scanning"]["status"], "unsupported")
        self.assertEqual(priv_free_caps["codeql"]["status"], "unsupported")
        self.assertEqual(priv_free_caps["dependency_review"]["status"], "unsupported")
        self.assertEqual(priv_free_caps["vulnerability_alerts"]["status"], "configured")
        self.assertEqual(priv_free_caps["dependency_graph"]["status"], "configured")

        # Tier C: Private Pro/Team -> rulesets supported (missing until created), GHAS features still UNSUPPORTED
        priv_pro_api = P3DisposableRepoApi(visibility="private", plan="pro", admin=True)
        priv_pro_inv = inventory(priv_pro_api, "acme/project")
        priv_pro_caps = priv_pro_inv["capabilities"]
        self.assertEqual(priv_pro_caps["rulesets"]["status"], "missing")
        self.assertEqual(priv_pro_caps["secret_scanning"]["status"], "unsupported")
        self.assertEqual(priv_pro_caps["codeql"]["status"], "unsupported")
        self.assertEqual(priv_pro_caps["dependency_review"]["status"], "unsupported")

        # Tier D: Non-admin caller -> INACCESSIBLE
        non_admin_api = P3DisposableRepoApi(visibility="public", plan="free", admin=False)
        non_admin_inv = inventory(non_admin_api, "acme/project")
        non_admin_caps = non_admin_inv["capabilities"]
        self.assertEqual(non_admin_caps["vulnerability_alerts"]["status"], "inaccessible")
        self.assertEqual(non_admin_caps["secret_scanning"]["status"], "inaccessible")
        self.assertEqual(non_admin_caps["codeql"]["status"], "inaccessible")
        self.assertEqual(non_admin_caps["rulesets"]["status"], "inaccessible")

        # Verify private-vulnerability-reporting is rejected on private repositories
        with self.assertRaisesRegex(ApiError, "available only for public repositories"):
            reconcile_security(priv_free_api, "acme/project", ["private-vulnerability-reporting"])

    # -------------------------------------------------------------------------
    # Slice 3 (AC 3): Solo-maintainer merge path contract & documentation
    # -------------------------------------------------------------------------
    def test_ac3_solo_maintainer_merge_path_preserves_human_merge_without_second_reviewer(self):
        """AC 3: The solo-maintainer merge path (reviewer='none' in Interactive Setup,
        'required_approving_review_count': 0 in Policy JSON, --approvals 0 in configure_github_repo.py gates)
        preserves a human merge decision (required_review_thread_resolution: True,
        strict_required_status_checks_policy: True, dismiss_stale_reviews_on_push: True)
        without requiring an unavailable second human reviewer (required_approving_review_count: 0).
        """
        for doc_name, text in (
            ("proportional-security-and-controls.md", self.security_doc),
            ("merge-gates.md", self.merge_doc),
        ):
            self.assertIn("required_approving_review_count", text, f"{doc_name} must document required_approving_review_count")
            self.assertIn("--approvals 0", text, f"{doc_name} must document --approvals 0")
            self.assertIn("required_review_thread_resolution", text, f"{doc_name} must document required_review_thread_resolution")
            self.assertIn("strict_required_status_checks_policy", text, f"{doc_name} must document strict_required_status_checks_policy")
            self.assertIn("dismiss_stale_reviews_on_push", text, f"{doc_name} must document dismiss_stale_reviews_on_push")
            self.assertIn("none", text, f"{doc_name} must document 'none' interactive reviewer input")

        # Verify Interactive prompt_rulesets_stage with 'none'
        in_stream = io.StringIO("y\n12\nnone\nstatus-check\n")
        out_stream = io.StringIO()
        stage = prompt_rulesets_stage(
            session=None,
            in_stream=in_stream,
            out_stream=out_stream,
            workflows_stage={"workflows": [{"template": "profile-ci", "profile": "python", "status_check_name": "status-check"}]},
        )
        self.assertEqual(
            stage,
            {
                "name": "gates",
                "ruleset_name": "quality-gate",
                "evidence_pr": 12,
                "checks": {"status-check": ".github/workflows/ci.yml"},
                "required_approving_review_count": 0,
            },
        )
        self.assertNotIn("reviewer", stage)

        # Verify reconcile_gates produces exact ruleset parameters for solo maintainer
        api = P3DisposableRepoApi(visibility="public", plan="free")
        changes = reconcile_gates(
            api,
            "acme/project",
            "quality-gate",
            pull_number=12,
            reviewer=None,
            checks={"status-check": ".github/workflows/ci.yml"},
            apply=True,
            required_approving_review_count=0,
        )
        self.assertEqual(len(changes), 1)
        ruleset = api.rulesets[0]
        pr_rule = next(r for r in ruleset["rules"] if r["type"] == "pull_request")["parameters"]
        self.assertEqual(pr_rule["required_approving_review_count"], 0)
        self.assertIs(pr_rule["dismiss_stale_reviews_on_push"], True)
        self.assertIs(pr_rule["require_code_owner_review"], False)
        self.assertIs(pr_rule["require_last_push_approval"], False)
        self.assertIs(pr_rule["required_review_thread_resolution"], True)

        checks_rule = next(r for r in ruleset["rules"] if r["type"] == "required_status_checks")["parameters"]
        self.assertIs(checks_rule["strict_required_status_checks_policy"], True)
        self.assertEqual(
            checks_rule["required_status_checks"],
            [{"context": "status-check", "integration_id": 15368}],
        )

    # -------------------------------------------------------------------------
    # Slice 4 (AC 4): Required check names, Dependabot config, and two-pass recovery
    # -------------------------------------------------------------------------
    def test_ac4_documented_settings_check_names_and_two_pass_recovery_match_implementation(self):
        """AC 4: Documented settings and required check names (status-check, smoke-test, test,
        Issue State Gate) match the implemented proportional-security configuration and its
        two-pass recovery checks.
        """
        for check_name in ("status-check", "smoke-test", "test", "Issue State Gate"):
            self.assertIn(
                check_name,
                self.security_doc,
                f"docs/proportional-security-and-controls.md must document check name {check_name!r}",
            )
            self.assertIn(
                check_name,
                self.merge_doc,
                f"docs/merge-gates.md must document check name {check_name!r}",
            )

        # Verify Dependabot generator output matches documented YAML contract
        dep_yaml = render_dependabot_config(
            {
                "name": "dependabot",
                "cadence": "monthly",
                "package_ecosystems": ["pip", "github-actions"],
                "open_pull_requests_limit": 5,
                "groups": True,
            }
        )
        self.assertIn('interval: "monthly"', dep_yaml)
        self.assertIn("open-pull-requests-limit: 5", dep_yaml)
        self.assertIn("pip-dependencies:", dep_yaml)
        self.assertIn("actions:", dep_yaml)

        # Verify two-pass pending behavior when workflows and gates are selected together
        api = P3DisposableRepoApi(visibility="public", plan="free")
        two_pass_policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {
                    "name": "ci-profile",
                    "profile": "python",
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": DEFAULT_CHECKOUT_SHA,
                    "status_check_name": "status-check",
                },
                {
                    "name": "gates",
                    "ruleset_name": "quality-gate",
                    "evidence_pr": 12,
                    "required_approving_review_count": 0,
                    "checks": {"status-check": ".github/workflows/ci.yml"},
                },
                {
                    "name": "issue-state-gate",
                },
            ],
        }
        dry_run = run_policy(api, "acme/project", two_pass_policy, apply=False)
        gate_change = next(c for c in dry_run["changes"] if c.get("stage") == "gates")
        isg_change = next(c for c in dry_run["changes"] if c.get("stage") == "issue-state-gate")
        self.assertEqual(gate_change["status"], "pending")
        self.assertIn("Merge the draft Setup Pull Request", gate_change["reason"])
        self.assertEqual(isg_change["status"], "pending")
        self.assertIn("Merge the draft Setup Pull Request", isg_change["reason"])

        # Verify discrepancy detection when ruleset check mismatches CI profile status_check_name
        mismatch_policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {
                    "name": "ci-profile",
                    "profile": "cmake-mpi",
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": DEFAULT_CHECKOUT_SHA,
                    "status_check_name": "smoke-test",
                },
                {
                    "name": "gates",
                    "ruleset_name": "quality-gate",
                    "evidence_pr": 12,
                    "required_approving_review_count": 0,
                    "checks": {"wrong-check": ".github/workflows/ci.yml"},
                },
            ],
        }
        mismatch_dry_run = run_policy(api, "acme/project", mismatch_policy, apply=False)
        blocked_gate = next(c for c in mismatch_dry_run["changes"] if c.get("stage") == "gates")
        self.assertEqual(blocked_gate["status"], "blocked")
        self.assertIn("Discrepancy detected", blocked_gate["reason"])
        self.assertIn("smoke-test", blocked_gate["reason"])

    # -------------------------------------------------------------------------
    # Slice 5 (AC 5): Recorded verification evidence in Observed/Inferred/Unverified format
    # -------------------------------------------------------------------------
    def test_ac5_recorded_verification_evidence_matrix_in_proportional_security_doc(self):
        """AC 5: Record verification evidence in docs/proportional-security-and-controls.md
        (using the Observed / Inferred / Unverified format) marking each of the 12 gated steps
        across public vs private Free/Pro repositories as verified, unavailable ([UNSUPPORTED]),
        or not selected with the plan or capability evidence.
        """
        for section_marker in ("Observed", "Inferred", "Unverified"):
            self.assertIn(
                section_marker,
                self.security_doc,
                f"docs/proportional-security-and-controls.md must include '{section_marker}' evidence classification",
            )

        required_12_controls = (
            "merge_controls",
            "default_workflow_permissions",
            "action_sha_pins",
            "dependabot",
            "branch_rulesets",
            "vulnerability_alerts",
            "secret_scanning",
            "push_protection",
            "private_vulnerability_reporting",
            "codeql",
            "dependency_graph",
            "dependency_review",
        )
        for control_id in required_12_controls:
            self.assertIn(
                f"`{control_id}`",
                self.security_doc,
                f"docs/proportional-security-and-controls.md must record verification evidence for `{control_id}`",
            )

        for required_state_label in ("`verified`", "`unavailable` (`[UNSUPPORTED]`)", "`not selected`"):
            self.assertIn(
                required_state_label,
                self.security_doc,
                f"docs/proportional-security-and-controls.md must include state classification {required_state_label}",
            )


if __name__ == "__main__":
    unittest.main()
