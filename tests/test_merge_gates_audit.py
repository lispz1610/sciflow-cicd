"""Comprehensive contract audit tests for merge gates and evidence PR approval."""

import importlib.util
import io
import os
import pathlib
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

CFG_SCRIPT = SCRIPTS_DIR / "configure_github_repo.py"
SETUP_SCRIPT = SCRIPTS_DIR / "setup_github_repo.py"

cfg_spec = importlib.util.spec_from_file_location("configure_github_repo", CFG_SCRIPT)
configurator = importlib.util.module_from_spec(cfg_spec)
cfg_spec.loader.exec_module(configurator)

setup_spec = importlib.util.spec_from_file_location("setup_github_repo", SETUP_SCRIPT)
setup_mod = importlib.util.module_from_spec(setup_spec)
setup_spec.loader.exec_module(setup_mod)


class FakeGatesApi:
    """Mock GitHub API simulating evidence PRs, check runs, workflows, and rulesets."""

    def __init__(self):
        self.repo = {
            "default_branch": "main",
            "visibility": "public",
        }
        self.prs = {
            10: {
                "state": "open",
                "base": {"ref": "main"},
                "head": {"sha": "head-10"},
                "merge_commit_sha": "merge-10",
                "user": {"login": "contributor"},
            },
            11: {
                "state": "closed",
                "base": {"ref": "main"},
                "head": {"sha": "head-11"},
                "merge_commit_sha": "merge-11",
                "user": {"login": "contributor"},
            },
            12: {
                "state": "open",
                "base": {"ref": "develop"},
                "head": {"sha": "head-12"},
                "merge_commit_sha": "merge-12",
                "user": {"login": "contributor"},
            },
        }
        self.reviews = {
            10: [
                {"user": {"login": "lead-reviewer"}, "state": "APPROVED", "commit_id": "head-10"}
            ]
        }
        self.workflows = [
            {"id": 101, "path": ".github/workflows/ci.yml", "name": "CI", "state": "active"}
        ]
        self.workflow_runs = {
            (101, "head-10"): [],
            (101, "merge-10"): [
                {
                    "workflow_id": 101,
                    "check_suite_id": 501,
                    "head_sha": "merge-10",
                    "event": "pull_request",
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
        }
        self.check_runs = {
            "head-10": [],
            "merge-10": [
                {
                    "name": "build-and-test",
                    "head_sha": "merge-10",
                    "status": "completed",
                    "conclusion": "success",
                    "check_suite": {"id": 501},
                    "app": {"id": 15368},
                }
            ],
        }
        self.collaborator_permissions = {
            "lead-reviewer": "write",
        }
        self.rulesets = []
        self.writes = []
        self.reads = []

    def request(self, method, path, payload=None):
        if method == "GET":
            self.reads.append((method, path, payload))

        base = "/repos/acme/project"

        if path == base:
            return 200, self.repo

        # Collaborator permissions
        if path.startswith(base + "/collaborators/"):
            username = path.removeprefix(base + "/collaborators/").removesuffix("/permission")
            perm = self.collaborator_permissions.get(username, "none")
            return 200, {"permission": perm}

        # PRs
        if path.startswith(base + "/pulls/") and "/reviews" not in path:
            pr_num = int(path.removeprefix(base + "/pulls/"))
            if pr_num in self.prs:
                return 200, dict(self.prs[pr_num])
            return 404, {"message": "Not Found"}

        # Reviews
        if path.startswith(base + "/pulls/") and path.endswith("/reviews?per_page=100&page=1"):
            pr_num = int(path.split("/pulls/")[1].split("/")[0])
            return 200, list(self.reviews.get(pr_num, []))

        # Installed workflows
        if path == base + "/actions/workflows?per_page=100&page=1":
            return 200, {"workflows": self.workflows}

        # Workflow runs
        if path.startswith(base + "/actions/workflows/"):
            parts = path.split("/actions/workflows/")[1].split("/runs?")
            wf_id = int(parts[0])
            params = dict(q.split("=") for q in parts[1].split("&"))
            head_sha = params.get("head_sha")
            runs = self.workflow_runs.get((wf_id, head_sha), [])
            return 200, {"workflow_runs": runs}

        # Check runs on commit
        if path.startswith(base + "/commits/") and "/check-runs" in path:
            sha = path.split("/commits/")[1].split("/check-runs")[0]
            runs = self.check_runs.get(sha, [])
            return 200, {"check_runs": runs}

        # Rulesets
        if path in (base + "/rulesets", base + "/rulesets?includes_parents=false&per_page=100"):
            if method == "POST":
                self.writes.append((method, path, payload))
                created = dict(payload, id=len(self.rulesets) + 1)
                self.rulesets.append(created)
                return 201, created
            return 200, list(self.rulesets)

        if path.startswith(base + "/rulesets/"):
            rs_id = int(path.removeprefix(base + "/rulesets/"))
            matching = [r for r in self.rulesets if r.get("id") == rs_id]
            if not matching:
                return 404, {"message": "Ruleset not found"}
            if method == "PUT":
                self.writes.append((method, path, payload))
                matching[0].update(payload)
                return 200, matching[0]
            return 200, matching[0]

        return 404, {"message": f"Endpoint not mocked: {method} {path}"}


class MergeGatesAuditTest(unittest.TestCase):
    """Audit tests for reconcile_gates and staged gates workflow."""

    def setUp(self):
        self.api = FakeGatesApi()
        self.checks = {"build-and-test": ".github/workflows/ci.yml"}

    # 1. Verification of Open Evidence PR and Target Branch
    def test_open_evidence_pr_with_independent_approval_succeeds(self):
        changes = configurator.reconcile_gates(
            self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0][0], "POST")
        self.assertEqual(changes[0][1], "/repos/acme/project/rulesets")
        payload = changes[0][2]
        self.assertEqual(payload["name"], "code-review")
        self.assertEqual(payload["enforcement"], "active")
        self.assertEqual(payload["conditions"]["ref_name"]["include"], ["refs/heads/main"])
        # Status checks pinned to app id
        status_rule = [r for r in payload["rules"] if r["type"] == "required_status_checks"][0]
        self.assertEqual(
            status_rule["parameters"]["required_status_checks"],
            [{"context": "build-and-test", "integration_id": 15368}],
        )

    def test_closed_evidence_pr_rejected(self):
        with self.assertRaisesRegex(ValueError, "evidence PR must be open"):
            configurator.reconcile_gates(
                self.api, "acme/project", "code-review", 11, "lead-reviewer", self.checks, apply=True
            )
        self.assertEqual(self.api.writes, [])

    def test_evidence_pr_targeting_non_default_branch_rejected(self):
        with self.assertRaisesRegex(ValueError, "evidence PR must target the default branch"):
            configurator.reconcile_gates(
                self.api, "acme/project", "code-review", 12, "lead-reviewer", self.checks, apply=True
            )
        self.assertEqual(self.api.writes, [])

    # 2. Independent Reviewer and Approval Currency
    def test_self_approval_rejected(self):
        with self.assertRaisesRegex(ValueError, "reviewer must be independent"):
            configurator.reconcile_gates(
                self.api, "acme/project", "code-review", 10, "contributor", self.checks, apply=True
            )
        self.assertEqual(self.api.writes, [])

    def test_missing_pr_author_rejected_as_non_independent(self):
        self.api.prs[10]["user"] = None
        with self.assertRaisesRegex(ValueError, "reviewer must be independent"):
            configurator.reconcile_gates(
                self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
            )
        self.assertEqual(self.api.writes, [])

    def test_deleted_unrelated_reviewer_does_not_crash_gate_command(self):
        self.api.reviews[10].insert(0, {
            "user": None, "state": "COMMENTED", "commit_id": "head-10"
        })
        with patch.object(configurator, "GitHubApi", return_value=self.api), \
                patch.dict(os.environ, {"GITHUB_TOKEN": "test-only"}), \
                redirect_stdout(io.StringIO()):
            exit_code = configurator.main([
                "--repo", "acme/project", "--apply", "gates", "--name", "code-review",
                "--evidence-pr", "10", "--reviewer", "lead-reviewer",
                "--check", ".github/workflows/ci.yml=build-and-test"
            ])
        self.assertEqual(exit_code, 0)
        self.assertTrue(any(method == "POST" for method, _, _ in self.api.writes))

    def test_reviewer_with_insufficient_permission_rejected(self):
        self.api.collaborator_permissions["lead-reviewer"] = "read"
        with self.assertRaisesRegex(ValueError, "reviewer needs write, maintain, or admin permission"):
            configurator.reconcile_gates(
                self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
            )
        self.assertEqual(self.api.writes, [])

    def test_stale_approval_on_previous_commit_rejected(self):
        self.api.reviews[10] = [
            {"user": {"login": "lead-reviewer"}, "state": "APPROVED", "commit_id": "older-sha"}
        ]
        with self.assertRaisesRegex(ValueError, "reviewer approval on the current PR head is required"):
            configurator.reconcile_gates(
                self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
            )
        self.assertEqual(self.api.writes, [])

    def test_superseded_approval_with_changes_requested_rejected(self):
        # Reviewer approved, but later requested changes
        self.api.reviews[10] = [
            {"user": {"login": "lead-reviewer"}, "state": "APPROVED", "commit_id": "head-10"},
            {"user": {"login": "lead-reviewer"}, "state": "CHANGES_REQUESTED", "commit_id": "head-10"},
        ]
        with self.assertRaisesRegex(ValueError, "reviewer approval on the current PR head is required"):
            configurator.reconcile_gates(
                self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
            )
        self.assertEqual(self.api.writes, [])

    def test_superseded_approval_with_dismissed_rejected(self):
        # Reviewer approval was dismissed
        self.api.reviews[10] = [
            {"user": {"login": "lead-reviewer"}, "state": "APPROVED", "commit_id": "head-10"},
            {"user": {"login": "lead-reviewer"}, "state": "DISMISSED", "commit_id": "head-10"},
        ]
        with self.assertRaisesRegex(ValueError, "reviewer approval on the current PR head is required"):
            configurator.reconcile_gates(
                self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
            )
        self.assertEqual(self.api.writes, [])

    def test_approval_with_subsequent_comment_remains_valid(self):
        # Reviewer approved on head-10, then left a COMMENTED review
        self.api.reviews[10] = [
            {"user": {"login": "lead-reviewer"}, "state": "APPROVED", "commit_id": "head-10"},
            {"user": {"login": "lead-reviewer"}, "state": "COMMENTED", "commit_id": "head-10"},
        ]
        changes = configurator.reconcile_gates(
            self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
        )
        self.assertEqual(len(changes), 1)

    # 3. Active Workflow and Check Execution
    def test_inactive_or_missing_workflow_rejected(self):
        self.api.workflows = [
            {"id": 101, "path": ".github/workflows/ci.yml", "name": "CI", "state": "disabled_manually"}
        ]
        with self.assertRaisesRegex(ValueError, "installed workflow is missing or inactive"):
            configurator.reconcile_gates(
                self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
            )
        self.assertEqual(self.api.writes, [])

    def test_ambiguous_repeated_check_name_rejected(self):
        # Two check runs with the same name on merge-10
        self.api.check_runs["merge-10"] = [
            {
                "name": "build-and-test",
                "head_sha": "merge-10",
                "status": "completed",
                "conclusion": "success",
                "check_suite": {"id": 501},
                "app": {"id": 15368},
            },
            {
                "name": "build-and-test",
                "head_sha": "merge-10",
                "status": "completed",
                "conclusion": "success",
                "check_suite": {"id": 501},
                "app": {"id": 15368},
            },
        ]
        with self.assertRaisesRegex(ValueError, "ambiguous check name on merge-10"):
            configurator.reconcile_gates(
                self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
            )
        self.assertEqual(self.api.writes, [])

    def test_unrelated_workflow_check_suite_rejected(self):
        # Check run belongs to check_suite 999, not 501 from ci.yml
        self.api.check_runs["merge-10"][0]["check_suite"]["id"] = 999
        with self.assertRaisesRegex(ValueError, "missing successful check: build-and-test"):
            configurator.reconcile_gates(
                self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
            )
        self.assertEqual(self.api.writes, [])

    def test_failing_check_run_blocks_ruleset(self):
        self.api.check_runs["merge-10"][0]["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "missing successful check: build-and-test"):
            configurator.reconcile_gates(
                self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
            )
        self.assertEqual(self.api.writes, [])

    def test_head_change_during_preflight_rejected(self):
        original_request = self.api.request
        pr_fetch_count = [0]

        def intercept(method, path, payload=None):
            if path == "/repos/acme/project/pulls/10" and method == "GET":
                pr_fetch_count[0] += 1
                if pr_fetch_count[0] > 1:
                    # Head changed between preflight and final check
                    modified = dict(self.api.prs[10], head={"sha": "moved-head"})
                    return 200, modified
            return original_request(method, path, payload)

        self.api.request = intercept
        with self.assertRaisesRegex(ValueError, "evidence PR changed during preflight; retry"):
            configurator.reconcile_gates(
                self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
            )
        self.assertEqual(self.api.writes, [])

    # 4. Ruleset Drift Protection
    def test_existing_ruleset_with_unmanaged_rule_blocks_replacement(self):
        configurator.reconcile_gates(
            self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
        )
        self.api.writes.clear()
        # Add an unmanaged deletion rule
        self.api.rulesets[0]["rules"].append({"type": "deletion"})
        with self.assertRaisesRegex(configurator.ApiError, "existing ruleset contains unmanaged rules"):
            configurator.reconcile_gates(
                self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
            )
        self.assertEqual(self.api.writes, [])

    def test_existing_ruleset_with_unmanaged_status_check_blocks_replacement(self):
        configurator.reconcile_gates(
            self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
        )
        self.api.writes.clear()
        # Add an unmanaged check context
        checks_rule = [r for r in self.api.rulesets[0]["rules"] if r["type"] == "required_status_checks"][0]
        checks_rule["parameters"]["required_status_checks"].append({"context": "unmanaged-custom-linter"})
        with self.assertRaisesRegex(configurator.ApiError, "existing ruleset contains unmanaged required checks"):
            configurator.reconcile_gates(
                self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
            )
        self.assertEqual(self.api.writes, [])

    def test_existing_ruleset_with_unmanaged_bypass_actors_blocks_replacement(self):
        configurator.reconcile_gates(
            self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
        )
        self.api.writes.clear()
        self.api.rulesets[0]["bypass_actors"] = [{"actor_id": 1, "actor_type": "Team"}]
        with self.assertRaisesRegex(configurator.ApiError, "existing ruleset contains unmanaged bypass actors"):
            configurator.reconcile_gates(
                self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
            )
        self.assertEqual(self.api.writes, [])

    def test_existing_ruleset_with_extra_protected_branch_is_preserved(self):
        configurator.reconcile_gates(
            self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
        )
        self.api.writes.clear()
        protected = self.api.rulesets[0]["conditions"]["ref_name"]["include"]
        protected.append("refs/heads/release")
        errors = io.StringIO()
        with patch.object(configurator, "GitHubApi", return_value=self.api), \
                patch.dict(os.environ, {"GITHUB_TOKEN": "test-only"}), \
                redirect_stderr(errors):
            exit_code = configurator.main([
                "--repo", "acme/project", "--apply", "gates", "--name", "code-review",
                "--evidence-pr", "10", "--reviewer", "lead-reviewer",
                "--check", ".github/workflows/ci.yml=build-and-test"
            ])
        self.assertEqual(exit_code, 1)
        self.assertIn("unmanaged conditions", errors.getvalue())
        self.assertEqual(self.api.writes, [])
        self.assertEqual(protected, ["refs/heads/main", "refs/heads/release"])

    # 5. Deferred Gate Status in setup_github_repo when CI Staged in Setup PR
    def test_gate_deferred_when_workflow_is_staged_in_setup_pr(self):
        policy = {
            "version": 2,
            "stages": [
                {
                    "name": "workflows",
                    "workflows": [
                        {
                            "name": "CI",
                            "filename": "ci.yml",
                            "template": "ci",
                            "checkout_sha": "a" * 40,
                            "action_sha": "b" * 40,
                        }
                    ],
                },
                {
                    "name": "gates",
                    "ruleset_name": "code-review",
                    "evidence_pr": 10,
                    "reviewer": "lead-reviewer",
                    "checks": {"build-and-test": ".github/workflows/ci.yml"},
                },
            ],
        }

        with patch.object(setup_mod, "install_selected") as mock_install:
            mock_install.return_value = {
                "status": "pending_review",
                "pull_request": 99,
                "branch": "setup/workflows",
                "changes": [{"path": ".github/workflows/ci.yml", "desired_sha256": "abc"}],
            }
            # Dry-run
            preview = setup_mod.run_policy(self.api, "acme/project", policy, apply=False)
            gate_changes = [c for c in preview["changes"] if c["stage"] == "gates"]
            self.assertEqual(len(gate_changes), 1)
            self.assertEqual(gate_changes[0]["status"], "pending")
            self.assertIn("Merge the draft Setup Pull Request", gate_changes[0]["reason"])
            self.assertEqual(self.api.writes, [])

            # Apply
            result = setup_mod.run_policy(self.api, "acme/project", policy, apply=True, accepted_dry_run=preview)
            apply_gates = [c for c in result["changes"] if c["stage"] == "gates"]
            self.assertEqual(len(apply_gates), 1)
            self.assertEqual(apply_gates[0]["status"], "pending")
            # No rulesets were directly created/written because gates are deferred
            self.assertEqual(self.api.writes, [])

    def test_gate_blocked_when_workflow_staging_fails(self):
        policy = {
            "version": 2,
            "stages": [
                {
                    "name": "workflows",
                    "workflows": [
                        {
                            "name": "CI",
                            "filename": "ci.yml",
                            "template": "ci",
                            "checkout_sha": "a" * 40,
                            "action_sha": "b" * 40,
                        }
                    ],
                },
                {
                    "name": "gates",
                    "ruleset_name": "code-review",
                    "evidence_pr": 10,
                    "reviewer": "lead-reviewer",
                    "checks": {"build-and-test": ".github/workflows/ci.yml"},
                },
            ],
        }

        with patch.object(setup_mod, "install_selected") as mock_install:
            mock_install.side_effect = setup_mod.ApiError("Staging branch conflict")
            preview = setup_mod.run_policy(self.api, "acme/project", policy, apply=False)
            gate_changes = [c for c in preview["changes"] if c["stage"] == "gates"]
            self.assertEqual(len(gate_changes), 1)
            self.assertEqual(gate_changes[0]["status"], "blocked")
            self.assertIn("conflict", gate_changes[0]["reason"])

    # 6. Real Case Demonstration: Blocking vs Permitting Check Runs
    def test_concrete_comparison_blocking_check_vs_permitting_check(self):
        # Case A: Blocking check run (failure on test)
        self.api.check_runs["merge-10"][0]["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "missing successful check: build-and-test"):
            configurator.reconcile_gates(
                self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
            )
        self.assertEqual(self.api.writes, [], "Zero writes must occur when check blocks")

        # Case B: Permitting check run (success on test with active workflow and valid approval)
        self.api.check_runs["merge-10"][0]["conclusion"] = "success"
        changes = configurator.reconcile_gates(
            self.api, "acme/project", "code-review", 10, "lead-reviewer", self.checks, apply=True
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(len(self.api.writes), 1)
        self.assertEqual(self.api.writes[0][0], "POST")
        self.assertEqual(self.api.rulesets[0]["name"], "code-review")


if __name__ == "__main__":
    unittest.main()
