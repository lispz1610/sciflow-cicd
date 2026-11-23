from pathlib import Path
import sys
import unittest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
TESTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from configure_github_repo import ApiError, reconcile_gates, main as configure_main
from setup_github_repo import run_policy, validate_policy
from api_test_support import FakeApi


class SoloMaintainerRulesetTests(unittest.TestCase):
    CHECKS = {"ci-check": ".github/workflows/ci.yml"}

    def test_reconcile_gates_solo_maintainer_zero_approvals(self):
        api = FakeApi()
        # Evidence PR author is 'author', no reviewer provided
        changes = reconcile_gates(
            api,
            "acme/project",
            "solo-quality-gate",
            7,
            reviewer=None,
            checks=self.CHECKS,
            apply=False,
            required_approving_review_count=0,
        )
        self.assertEqual(len(changes), 1)
        method, path, payload = changes[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/repos/acme/project/rulesets")
        self.assertEqual(payload["name"], "solo-quality-gate")

        pr_rule = next(r for r in payload["rules"] if r["type"] == "pull_request")
        self.assertEqual(pr_rule["parameters"]["required_approving_review_count"], 0)

        checks_rule = next(r for r in payload["rules"] if r["type"] == "required_status_checks")
        self.assertEqual(
            checks_rule["parameters"]["required_status_checks"],
            [{"context": "ci-check", "integration_id": 15368}],
        )

    def test_solo_maintainer_does_not_require_reviewer_or_approval(self):
        api = FakeApi()
        # Clear any reviews from FakeApi
        api.reviews.clear()
        # Even with no reviews and no reviewer, reconcile_gates succeeds when required_approving_review_count=0
        changes = reconcile_gates(
            api,
            "acme/project",
            "solo-gate",
            7,
            reviewer=None,
            checks=self.CHECKS,
            apply=True,
            required_approving_review_count=0,
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(len(api.writes), 1)

    def test_standard_mode_requires_reviewer_and_approval(self):
        api = FakeApi()
        # When required_approving_review_count=1, missing reviewer raises ValueError
        with self.assertRaisesRegex(ValueError, "reviewer is required"):
            reconcile_gates(
                api,
                "acme/project",
                "team-gate",
                7,
                reviewer=None,
                checks=self.CHECKS,
                required_approving_review_count=1,
            )

    def test_transition_from_team_to_solo_ruleset(self):
        api = FakeApi()
        # Create initial ruleset with 1 approval
        reconcile_gates(
            api,
            "acme/project",
            "quality-gate",
            7,
            reviewer="reviewer",
            checks=self.CHECKS,
            apply=True,
            required_approving_review_count=1,
        )
        self.assertEqual(len(api.rulesets), 1)
        initial_pr_rule = next(r for r in api.rulesets[0]["rules"] if r["type"] == "pull_request")
        self.assertEqual(initial_pr_rule["parameters"]["required_approving_review_count"], 1)

        api.writes.clear()
        # Transition to solo maintainer ruleset with 0 approvals
        changes = reconcile_gates(
            api,
            "acme/project",
            "quality-gate",
            7,
            reviewer=None,
            checks=self.CHECKS,
            apply=True,
            required_approving_review_count=0,
        )
        self.assertEqual(len(changes), 1)
        method, path, payload = changes[0]
        self.assertEqual(method, "PUT")
        updated_pr_rule = next(r for r in payload["rules"] if r["type"] == "pull_request")
        self.assertEqual(updated_pr_rule["parameters"]["required_approving_review_count"], 0)

        # Subsequent check converges with no writes
        api.writes.clear()
        no_changes = reconcile_gates(
            api,
            "acme/project",
            "quality-gate",
            7,
            reviewer=None,
            checks=self.CHECKS,
            apply=True,
            required_approving_review_count=0,
        )
        self.assertEqual(no_changes, [])
        self.assertEqual(api.writes, [])

    def test_unmanaged_drift_rejected(self):
        api = FakeApi()
        reconcile_gates(
            api,
            "acme/project",
            "quality-gate",
            7,
            reviewer=None,
            checks=self.CHECKS,
            apply=True,
            required_approving_review_count=0,
        )
        # Introduce unmanaged rule to ruleset
        api.rulesets[0]["rules"].append({"type": "unmanaged_type", "parameters": {}})
        api.writes.clear()
        with self.assertRaisesRegex(ApiError, "unmanaged"):
            reconcile_gates(
                api,
                "acme/project",
                "quality-gate",
                7,
                reviewer=None,
                checks=self.CHECKS,
                apply=True,
                required_approving_review_count=0,
            )

    def test_policy_validation_solo_maintainer_stage(self):
        valid_policy = {
            "version": 1,
            "stages": [
                {
                    "name": "gates",
                    "ruleset_name": "solo-gate",
                    "evidence_pr": 7,
                    "required_approving_review_count": 0,
                    "checks": self.CHECKS,
                }
            ],
        }
        validate_policy(valid_policy)

        # Negative approval count rejected
        invalid_count_policy = {
            "version": 1,
            "stages": [
                {
                    "name": "gates",
                    "ruleset_name": "solo-gate",
                    "evidence_pr": 7,
                    "required_approving_review_count": -1,
                    "checks": self.CHECKS,
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            validate_policy(invalid_count_policy)

        # approvals > 0 without reviewer rejected
        missing_reviewer_policy = {
            "version": 1,
            "stages": [
                {
                    "name": "gates",
                    "ruleset_name": "team-gate",
                    "evidence_pr": 7,
                    "required_approving_review_count": 1,
                    "checks": self.CHECKS,
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "gates with required approvals requires reviewer"):
            validate_policy(missing_reviewer_policy)

    def test_setup_github_repo_run_policy_with_solo_gates(self):
        api = FakeApi()
        policy = {
            "version": 1,
            "stages": [
                {
                    "name": "gates",
                    "ruleset_name": "solo-gate",
                    "evidence_pr": 7,
                    "required_approving_review_count": 0,
                    "checks": self.CHECKS,
                }
            ],
        }
        plan = run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(len(plan["changes"]), 1)
        self.assertEqual(plan["changes"][0]["status"], "planned")

        applied = run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=plan)
        self.assertEqual(len(applied["changes"]), 1)
        self.assertEqual(applied["changes"][0]["status"], "verified")

        converged = run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(converged["changes"], [])
        self.assertEqual(converged["status"], "no-op")


if __name__ == "__main__":
    unittest.main()
