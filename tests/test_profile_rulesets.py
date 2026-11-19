"""Tests for merge gate and branch ruleset alignment with scientific CI profile check names (#129)."""

import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import configure_github_repo as configurator
from configure_github_repo import ApiError, GitHubApi
from interactive import Session
from interactive_workflows_rulesets import (
    prompt_rulesets_stage,
    prompt_workflows_rulesets_stage,
)
import setup_github_repo as setup_mod


class FakeProfileApi:
    """Mock GitHub API simulating repositories, profiles, check-runs, workflows, and rulesets."""

    def __init__(self, repo_visibility="public", workflow_file_content=None):
        self.repo = {
            "default_branch": "main",
            "visibility": repo_visibility,
            "full_name": "acme/project",
        }
        self.prs = {
            10: {
                "state": "open",
                "base": {"ref": "main"},
                "head": {"sha": "head-10"},
                "merge_commit_sha": "merge-10",
                "user": {"login": "contributor"},
            }
        }
        self.reviews = {
            10: [
                {"user": {"login": "lead-reviewer"}, "state": "APPROVED", "commit_id": "head-10"}
            ]
        }
        self.workflows = [
            {"id": 201, "path": ".github/workflows/ci.yml", "name": "Python CI", "state": "active"}
        ]
        self.workflow_runs = {
            (201, "head-10"): [],
            (201, "merge-10"): [
                {
                    "workflow_id": 201,
                    "check_suite_id": 601,
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
                    "name": "status-check",
                    "head_sha": "merge-10",
                    "status": "completed",
                    "conclusion": "success",
                    "check_suite": {"id": 601},
                    "app": {"id": 15368},
                }
            ],
        }
        self.collaborator_permissions = {
            "lead-reviewer": "write",
        }
        self.workflow_file_content = workflow_file_content
        self.rulesets = []
        self.writes = []
        self.reads = []

    def request(self, method, path, payload=None):
        if method == "GET":
            self.reads.append((method, path, payload))

        base = "/repos/acme/project"

        action_commits = {
            f"/repos/actions/checkout/git/commits/{setup_mod.DEFAULT_CHECKOUT_SHA}": setup_mod.DEFAULT_CHECKOUT_SHA,
            f"/repos/actions/setup-python/git/commits/{setup_mod.DEFAULT_SETUP_PYTHON_SHA}": setup_mod.DEFAULT_SETUP_PYTHON_SHA,
            f"/repos/astral-sh/setup-uv/git/commits/{setup_mod.DEFAULT_SETUP_UV_SHA}": setup_mod.DEFAULT_SETUP_UV_SHA,
        }
        if path in action_commits and method == "GET":
            return 200, {"sha": action_commits[path]}
        if path.startswith(("/repos/actions/checkout/git/commits/",
                            "/repos/actions/setup-python/git/commits/",
                            "/repos/astral-sh/setup-uv/git/commits/")) and method == "GET":
            return 404, None
        if path == base + "/git/ref/heads/main" and method == "GET":
            return 200, {"object": {"sha": "d" * 40}}
        if path.startswith(base + "/git/ref/heads/") and method == "GET":
            return 404, {"message": "Not Found"}
        if path == base + "/git/refs" and method == "POST":
            return 201, {"ref": payload.get("ref"), "object": {"sha": payload.get("sha")}}
        if path.startswith(base + "/git/blobs") and method == "POST":
            return 201, {"sha": "b" * 40}
        if path.startswith(base + "/git/trees") and method == "POST":
            return 201, {"sha": "t" * 40}
        if path.startswith(base + "/git/commits") and method == "POST":
            return 201, {"sha": "c" * 40}
        if path == base + "/pulls" and method == "POST":
            return 201, {"number": 99, "html_url": "https://github.com/acme/project/pull/99"}

        if path == base:
            return 200, self.repo

        if path.startswith(base + "/collaborators/"):
            username = path.removeprefix(base + "/collaborators/").removesuffix("/permission")
            perm = self.collaborator_permissions.get(username, "none")
            return 200, {"permission": perm}

        if path.startswith(base + "/pulls?") or path == base + "/pulls":
            return 200, []

        if path.startswith(base + "/pulls/") and "/reviews" not in path:
            pr_num = int(path.removeprefix(base + "/pulls/"))
            if pr_num in self.prs:
                return 200, dict(self.prs[pr_num])
            return 404, {"message": "Not Found"}

        if path.startswith(base + "/pulls/") and path.endswith("/reviews?per_page=100&page=1"):
            pr_num = int(path.split("/pulls/")[1].split("/")[0])
            return 200, list(self.reviews.get(pr_num, []))

        if path == base + "/actions/workflows?per_page=100&page=1":
            return 200, {"workflows": self.workflows}

        if path.startswith(base + "/actions/workflows/"):
            parts = path.split("/actions/workflows/")[1].split("/runs?")
            wf_id = int(parts[0])
            params = dict(q.split("=") for q in parts[1].split("&"))
            head_sha = params.get("head_sha")
            runs = self.workflow_runs.get((wf_id, head_sha), [])
            return 200, {"workflow_runs": runs}

        if path.startswith(base + "/commits/") and "/check-runs" in path:
            sha = path.split("/commits/")[1].split("/check-runs")[0]
            runs = self.check_runs.get(sha, [])
            return 200, {"check_runs": runs}

        if path.startswith(base + "/contents/.github/workflows/ci.yml"):
            if self.workflow_file_content is not None:
                import base64
                encoded = base64.b64encode(self.workflow_file_content).decode("ascii")
                return 200, {
                    "type": "file",
                    "encoding": "base64",
                    "content": encoded,
                    "sha": "a" * 40,
                }
            return 404, {"message": "Not Found"}

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

        return 404, {"message": f"Unhandled path: {path}"}


class InteractiveProfileRulesetsAlignmentTests(unittest.TestCase):
    """Tests for Interactive Setup detecting profile CI and proposing stable check names."""

    def test_prompt_rulesets_stage_proposes_status_check_when_profile_ci_staged(self):
        """When profile-ci is in staged workflows, proposes 'status-check' as default."""
        staged_wf = {
            "name": "workflows",
            "workflows": [
                {
                    "template": "profile-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": setup_mod.DEFAULT_CHECKOUT_SHA,
                    "profile": "python",
                }
            ],
        }
        # Inputs: Confirm: y, PR: 10, Reviewer: alice, Check: press enter to accept default
        in_stream = io.StringIO("y\n10\nalice\n\n")
        out_stream = io.StringIO()

        stage = prompt_rulesets_stage(
            workflows_stage=staged_wf,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        self.assertIsNotNone(stage)
        self.assertEqual(stage["checks"], {"status-check": ".github/workflows/ci.yml"})
        output = out_stream.getvalue()
        self.assertIn("status-check", output)

    def test_prompt_rulesets_stage_proposes_custom_status_check_name(self):
        """When profile workflow specifies custom status_check_name, proposes it."""
        staged_wf = {
            "name": "workflows",
            "workflows": [
                {
                    "template": "profile-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": setup_mod.DEFAULT_CHECKOUT_SHA,
                    "profile": "cmake-mpi",
                    "status_check_name": "cmake-mpi-gate",
                }
            ],
        }
        in_stream = io.StringIO("y\n10\nalice\n\n")
        out_stream = io.StringIO()

        stage = prompt_rulesets_stage(
            workflows_stage=staged_wf,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        self.assertIsNotNone(stage)
        self.assertEqual(stage["checks"], {"cmake-mpi-gate": ".github/workflows/ci.yml"})

    def test_prompt_rulesets_stage_detects_profile_from_defaults(self):
        """When session.defaults has ci_profile or profile-ci, proposes 'status-check'."""
        session = Session(
            repository="acme/project",
            api=FakeProfileApi(),
            defaults={
                "ci_profile": {
                    "profile": "python",
                }
            },
        )
        in_stream = io.StringIO("y\n10\nalice\n\n")
        out_stream = io.StringIO()

        stage = prompt_rulesets_stage(
            session=session,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        self.assertIsNotNone(stage)
        self.assertEqual(stage["checks"], {"status-check": ".github/workflows/ci.yml"})

    def test_prompt_rulesets_stage_detects_active_profile_in_repo(self):
        """When repository has active profile-ci workflow, proposes 'status-check'."""
        profile_content = (
            b"# sciflow-cicd workflow-template=profile-ci version=1\n"
            b"name: Python CI\n"
            b"jobs:\n"
            b"  ci:\n"
            b"    runs-on: ubuntu-24.04\n"
            b"  status-check:\n"
            b"    name: \"status-check\"\n"
        )
        api = FakeProfileApi(workflow_file_content=profile_content)
        session = Session(
            repository="acme/project",
            api=api,
            inventory={"repository": {"default_branch": "main"}},
        )
        in_stream = io.StringIO("y\n10\nalice\n\n")
        out_stream = io.StringIO()

        stage = prompt_rulesets_stage(
            session=session,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        self.assertIsNotNone(stage)
        self.assertEqual(stage["checks"], {"status-check": ".github/workflows/ci.yml"})

    def test_prompt_rulesets_stage_reports_discrepancy_with_inventory_ruleset(self):
        """When inventory ruleset requires a different check than staged profile, reports discrepancy."""
        staged_wf = {
            "name": "workflows",
            "workflows": [
                {
                    "template": "profile-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": setup_mod.DEFAULT_CHECKOUT_SHA,
                    "profile": "python",
                }
            ],
        }
        inv = {
            "rulesets": [
                {
                    "name": "quality-gate",
                    "rules": [
                        {
                            "type": "required_status_checks",
                            "parameters": {
                                "required_status_checks": [
                                    {"context": "legacy-ci", "integration_id": 1}
                                ]
                            },
                        }
                    ],
                }
            ]
        }
        session = Session(
            repository="acme/project",
            api=FakeProfileApi(),
            inventory=inv,
        )
        in_stream = io.StringIO("y\n10\nalice\n\n")
        out_stream = io.StringIO()

        stage = prompt_rulesets_stage(
            session=session,
            workflows_stage=staged_wf,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        self.assertIsNotNone(stage)
        self.assertEqual(stage["checks"], {"status-check": ".github/workflows/ci.yml"})
        output = out_stream.getvalue()
        self.assertIn("Discrepancy", output)


class ReconcileGatesProfileValidationTests(unittest.TestCase):
    """Tests for reconcile_gates verifying stable check names for profile workflows."""

    def test_reconcile_gates_succeeds_with_profile_status_check(self):
        """reconcile_gates creates ruleset when check matches profile's declared check name."""
        profile_content = (
            b"# sciflow-cicd workflow-template=profile-ci version=1\n"
            b"name: Python CI\n"
            b"jobs:\n"
            b"  ci:\n"
            b"    runs-on: ubuntu-24.04\n"
            b"  status-check:\n"
            b"    name: \"status-check\"\n"
        )
        api = FakeProfileApi(workflow_file_content=profile_content)
        checks = {"status-check": ".github/workflows/ci.yml"}

        changes = configurator.reconcile_gates(
            api, "acme/project", "quality-gate", 10, "lead-reviewer", checks, apply=True
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0][0], "POST")
        payload = changes[0][2]
        status_rule = [r for r in payload["rules"] if r["type"] == "required_status_checks"][0]
        self.assertEqual(
            status_rule["parameters"]["required_status_checks"],
            [{"context": "status-check", "integration_id": 15368}],
        )

    def test_reconcile_gates_rejects_discrepant_check_name_for_profile_workflow(self):
        """reconcile_gates rejects configured check name that does not match declared profile check."""
        profile_content = (
            b"# sciflow-cicd workflow-template=profile-ci version=1\n"
            b"name: Python CI\n"
            b"jobs:\n"
            b"  ci:\n"
            b"    runs-on: ubuntu-24.04\n"
            b"  status-check:\n"
            b"    name: \"status-check\"\n"
        )
        api = FakeProfileApi(workflow_file_content=profile_content)
        # Even if someone passed a check named 'ci-check', it must be rejected as discrepant
        checks = {"ci-check": ".github/workflows/ci.yml"}

        with self.assertRaisesRegex(ValueError, "discrepancy detected.*does not match"):
            configurator.reconcile_gates(
                api, "acme/project", "quality-gate", 10, "lead-reviewer", checks, apply=True
            )


class DryRunAndHeadlessProfileRulesetsTests(unittest.TestCase):
    """Tests for Dry-Run discrepancy detection and Headless Mode reconciliation."""

    def test_dry_run_reports_discrepancy_for_staged_profile_and_mismatched_gate_check(self):
        """Dry-run blocks gate and reports discrepancy when ruleset check does not match staged profile."""
        policy = {
            "version": 2,
            "stages": [
                {
                    "name": "workflows",
                    "workflows": [
                        {
                            "name": "CI",
                            "template": "profile-ci",
                            "template_version": 1,
                            "filename": "ci.yml",
                            "triggers": ["pull_request"],
                            "checkout_sha": setup_mod.DEFAULT_CHECKOUT_SHA,
                            "profile": "python",
                        }
                    ],
                },
                {
                    "name": "gates",
                    "ruleset_name": "quality-gate",
                    "evidence_pr": 10,
                    "reviewer": "lead-reviewer",
                    "checks": {"ci-check": ".github/workflows/ci.yml"},
                },
            ],
        }
        api = FakeProfileApi()
        result = setup_mod.run_policy(api, "acme/project", policy, apply=False)

        gate_changes = [c for c in result["changes"] if c["stage"] == "gates"]
        self.assertEqual(len(gate_changes), 1)
        self.assertEqual(gate_changes[0]["status"], "blocked")
        self.assertIn("Discrepancy detected", gate_changes[0]["reason"])
        self.assertIn("status-check", gate_changes[0]["reason"])
        self.assertTrue(any("Discrepancy detected" in w for w in result.get("warnings", [])))

    def test_dry_run_passes_pending_gate_when_check_matches_profile_status_check(self):
        """Dry-run defers gate as pending when ruleset check matches staged profile status-check."""
        policy = {
            "version": 2,
            "stages": [
                {
                    "name": "workflows",
                    "workflows": [
                        {
                            "name": "CI",
                            "template": "profile-ci",
                            "template_version": 1,
                            "filename": "ci.yml",
                            "triggers": ["pull_request"],
                            "checkout_sha": setup_mod.DEFAULT_CHECKOUT_SHA,
                            "profile": "cmake-mpi",
                        }
                    ],
                },
                {
                    "name": "gates",
                    "ruleset_name": "quality-gate",
                    "evidence_pr": 10,
                    "reviewer": "lead-reviewer",
                    "checks": {"status-check": ".github/workflows/ci.yml"},
                },
            ],
        }
        api = FakeProfileApi()
        result = setup_mod.run_policy(api, "acme/project", policy, apply=False)

        gate_changes = [c for c in result["changes"] if c["stage"] == "gates"]
        self.assertEqual(len(gate_changes), 1)
        self.assertEqual(gate_changes[0]["status"], "pending")
        self.assertIn("Merge the draft Setup Pull Request", gate_changes[0]["reason"])
        self.assertFalse(any("Discrepancy" in w for w in result.get("warnings", [])))

    def test_dry_run_detects_discrepancy_with_ci_profile_stage(self):
        """Policy with 'profile-ci' workflow and mismatched 'gates' check reports discrepancy."""
        policy = {
            "version": 2,
            "stages": [
                {
                    "name": "workflows",
                    "workflows": [
                        {
                            "template": "profile-ci",
                            "template_version": 1,
                            "profile": "scientific-python",
                            "filename": "ci.yml",
                            "triggers": ["pull_request"],
                            "checkout_sha": "11bd71901bbe5b1630ceea73d27597364c9af683",
                        }
                    ],
                },
                {
                    "name": "gates",
                    "ruleset_name": "quality-gate",
                    "evidence_pr": 10,
                    "reviewer": "lead-reviewer",
                    "checks": {"Python CI (3.11)": ".github/workflows/ci.yml"},
                },
            ],
        }
        api = FakeProfileApi()
        result = setup_mod.run_policy(api, "acme/project", policy, apply=False)

        gate_changes = [c for c in result["changes"] if c["stage"] == "gates"]
        self.assertEqual(len(gate_changes), 1)
        self.assertEqual(gate_changes[0]["status"], "blocked")
        self.assertIn("Discrepancy detected", gate_changes[0]["reason"])

    def test_headless_mode_reconciles_installed_profile_gates(self):
        """Headless mode successfully reconciles gates when targeting installed profile checks."""
        profile_content = (
            b"# sciflow-cicd workflow-template=profile-ci version=1\n"
            b"name: CMake MPI CI\n"
            b"jobs:\n"
            b"  ci:\n"
            b"    runs-on: ubuntu-24.04\n"
            b"  status-check:\n"
            b"    name: \"status-check\"\n"
        )
        api = FakeProfileApi(workflow_file_content=profile_content)
        policy = {
            "version": 2,
            "stages": [
                {
                    "name": "gates",
                    "ruleset_name": "quality-gate",
                    "evidence_pr": 10,
                    "reviewer": "lead-reviewer",
                    "checks": {"status-check": ".github/workflows/ci.yml"},
                }
            ],
        }
        # 1. Dry run
        dry_run = setup_mod.run_policy(api, "acme/project", policy, apply=False)
        gate_changes = [c for c in dry_run["changes"] if c["stage"] == "gates"]
        self.assertEqual(len(gate_changes), 1)
        self.assertEqual(gate_changes[0]["status"], "planned")

        # 2. Apply
        applied = setup_mod.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=dry_run)
        applied_gates = [c for c in applied["changes"] if c["stage"] == "gates"]
        self.assertEqual(len(applied_gates), 1)
        self.assertEqual(applied_gates[0]["status"], "verified")
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(api.writes[0][0], "POST")

    def test_end_to_end_prompt_workflows_rulesets_stage_with_python_profile(self):
        """Selecting Python profile in prompt_workflows_rulesets_stage proposes status-check for gates."""
        # 1. Stage workflows? y
        # 1a. Default self-hosted runner? y (enter)
        # 1b. Runner labels: enter (self-hosted)
        # 2. Selection: 3 (Scientific Python CI)
        # 3. Python versions: enter (default)
        # 4. Package manager: enter (default)
        # 5. Test commands: enter (default)
        # 6. Git LFS: n
        # 7. Lint: enter (skip)
        # 8. Cache: y
        # 9. Configure gates? y
        # 10. Evidence PR: 10
        # 11. Reviewer: reviewer1
        # 12. Check name: enter (should default to status-check)
        # 13. Issue state gate: n
        # 14. Agent skills: n
        inputs = [
            "y",
            "",
            "",
            "3",
            "",
            "",
            "",
            "n",
            "",
            "y",
            "y",
            "10",
            "reviewer1",
            "",
            "n",
            "n",
        ]
        in_stream = io.StringIO("\n".join(inputs) + "\n")
        out_stream = io.StringIO()

        session = Session(repository="acme/project", api=FakeProfileApi())
        stages = prompt_workflows_rulesets_stage(
            session=session,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        self.assertIsNotNone(stages)
        gates_stage = next(s for s in stages if s["name"] == "gates")
        self.assertEqual(gates_stage["checks"], {"status-check": ".github/workflows/ci.yml"})

    def test_end_to_end_prompt_workflows_rulesets_stage_with_cmake_mpi_profile(self):
        """Selecting CMake/MPI profile in prompt_workflows_rulesets_stage proposes status-check for gates."""
        # 1. Stage workflows? y
        # 1a. Default self-hosted runner? y (enter)
        # 1b. Runner labels: enter (self-hosted)
        # 2. Selection: 4 (CMake/MPI CI)
        # 3. Manual build: n
        # 4. Build command: enter (default)
        # 5. Ranks: enter (default)
        # 6. Smoke tests: enter (default)
        # 7. Runner deps: enter (default)
        # 8. Ccache: y
        # 9. OpenCL: n
        # 10. Configure gates? y
        # 11. Evidence PR: 10
        # 12. Reviewer: lead-reviewer
        # 13. Check name: enter (should default to status-check)
        # 14. Issue state gate: n
        # 15. Agent skills: n
        inputs = [
            "y",
            "",
            "",
            "4",
            "n",
            "",
            "",
            "",
            "",
            "y",
            "n",
            "y",
            "10",
            "lead-reviewer",
            "",
            "n",
            "n",
        ]
        in_stream = io.StringIO("\n".join(inputs) + "\n")
        out_stream = io.StringIO()

        session = Session(repository="acme/project", api=FakeProfileApi())
        stages = prompt_workflows_rulesets_stage(
            session=session,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        self.assertIsNotNone(stages)
        gates_stage = next(s for s in stages if s["name"] == "gates")
        self.assertEqual(gates_stage["checks"], {"status-check": ".github/workflows/ci.yml"})

    def test_reconcile_gates_succeeds_with_cmake_mpi_profile(self):
        """reconcile_gates succeeds when check matches CMake/MPI profile stable check name."""
        profile_content = (
            b"# sciflow-cicd workflow-template=profile-ci version=1\n"
            b"# EVIDENCE DEMARCATION:\n"
            b"name: CMake MPI CI\n"
            b"jobs:\n"
            b"  ci:\n"
            b"    name: \"CMake MPI CI (${{ matrix.ranks }}-rank)\"\n"
            b"  status-check:\n"
            b"    name: \"status-check\"\n"
        )
        api = FakeProfileApi(workflow_file_content=profile_content)
        checks = {"status-check": ".github/workflows/ci.yml"}

        changes = configurator.reconcile_gates(
            api, "acme/project", "cmake-quality-gate", 10, "lead-reviewer", checks, apply=True
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0][0], "POST")
        status_rule = [r for r in changes[0][2]["rules"] if r["type"] == "required_status_checks"][0]
        self.assertEqual(
            status_rule["parameters"]["required_status_checks"],
            [{"context": "status-check", "integration_id": 15368}],
        )

    def test_reconcile_gates_rejects_cmake_mpi_discrepant_check(self):
        """reconcile_gates rejects dynamic matrix check name like 'CMake MPI CI (1-rank)'."""
        profile_content = (
            b"# sciflow-cicd workflow-template=profile-ci version=1\n"
            b"# EVIDENCE DEMARCATION:\n"
            b"name: CMake MPI CI\n"
            b"jobs:\n"
            b"  ci:\n"
            b"    name: \"CMake MPI CI (${{ matrix.ranks }}-rank)\"\n"
            b"  status-check:\n"
            b"    name: \"status-check\"\n"
        )
        api = FakeProfileApi(workflow_file_content=profile_content)
        checks = {"CMake MPI CI (1-rank)": ".github/workflows/ci.yml"}

        with self.assertRaisesRegex(ValueError, "discrepancy detected.*does not match"):
            configurator.reconcile_gates(
                api, "acme/project", "cmake-quality-gate", 10, "lead-reviewer", checks, apply=True
            )


if __name__ == "__main__":
    unittest.main()
