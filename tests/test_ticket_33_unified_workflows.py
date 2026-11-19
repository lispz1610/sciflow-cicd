"""Tests for Ticket #33: Unified Workflows Schema, Runner Options, and Fork Guard."""

import io
import json
import unittest
import yaml

from scripts import install_github_workflows as installer
from scripts import setup_github_repo as setup_mod
from scripts import interactive_workflows_rulesets as interactive_mod
from scripts.prompts import PromptExit


CHECKOUT = installer.DEFAULT_CHECKOUT_SHA


class FakeRepoApi:
    """Mock GitHub API for testing repo policy runs."""

    def __init__(self, visibility="public", is_private=False):
        self.visibility = visibility
        self.is_private = is_private
        self.prs = []

    def request(self, method, path, body=None):
        if method == "GET" and path == "/repos/acme/project":
            return 200, {
                "name": "project",
                "full_name": "acme/project",
                "visibility": self.visibility,
                "private": self.is_private,
                "default_branch": "main",
            }
        if method == "GET" and "/git/commits/" in path:
            sha = path.split("/")[-1]
            return 200, {"sha": sha}
        if method == "GET" and "/git/ref/" in path:
            return 200, {"object": {"sha": "1111111111111111111111111111111111111111"}}
        if method == "GET" and "/contents/" in path:
            return 404, {"message": "Not Found"}
        if method == "POST" and path == "/repos/acme/project/pulls":
            self.prs.append(body)
            return 201, {"number": 42, "html_url": "https://github.com/acme/project/pull/42"}
        return 200, {}


class SchemaUnificationTests(unittest.TestCase):
    """Test deprecation and rejection of ci-profile stage and legacy aliases."""

    def test_validate_policy_rejects_ci_profile_stage(self):
        policy = {
            "version": 2,
            "stages": [
                {
                    "name": "ci-profile",
                    "profile": "python",
                }
            ],
        }
        with self.assertRaises(ValueError) as ctx:
            setup_mod.validate_policy(policy)
        msg = str(ctx.exception)
        self.assertIn("ci-profile", msg)
        self.assertIn("workflows", msg)

    def test_legacy_aliases_rejected_in_workflow_spec(self):
        for bad_key in ("options", "pip_cache", "git_lfs"):
            wf = {
                "template": "profile-ci",
                "template_version": 1,
                "filename": "ci.yml",
                "triggers": ["pull_request"],
                "checkout_sha": CHECKOUT,
                "profile": "python",
                bad_key: True,
            }
            with self.assertRaises(ValueError):
                installer.render(wf)


class RunnerAndExecutionOptionsTests(unittest.TestCase):
    """Test validation and rendering of runs_on and common execution fields."""

    def test_validate_runs_on_defaults_and_types(self):
        self.assertEqual(installer.validate_runs_on(None), "self-hosted")
        self.assertEqual(installer.validate_runs_on("self-hosted"), "self-hosted")
        self.assertEqual(installer.validate_runs_on("ubuntu-24.04"), "ubuntu-24.04")
        self.assertEqual(
            installer.validate_runs_on(["self-hosted", "gpu"]),
            ["self-hosted", "gpu"],
        )

        # Invalid cases
        with self.assertRaises(ValueError):
            installer.validate_runs_on("")
        with self.assertRaises(ValueError):
            installer.validate_runs_on([])
        with self.assertRaises(ValueError):
            installer.validate_runs_on(["self-hosted", "self-hosted"])
        with self.assertRaises(ValueError):
            installer.validate_runs_on(True)
        with self.assertRaises(ValueError):
            installer.validate_runs_on(123)
        with self.assertRaises(ValueError):
            installer.validate_runs_on([""])

    def test_pr_ci_renders_runner_options(self):
        # Default self-hosted
        wf_default = {
            "template": "pr-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "commands": ["pytest"],
        }
        content = installer.render(wf_default).decode("utf-8")
        parsed = yaml.safe_load(content)
        self.assertEqual(parsed["jobs"]["ci"]["runs-on"], "self-hosted")

        # List runner labels
        wf_list = {
            "template": "pr-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "commands": ["pytest"],
            "runs_on": ["self-hosted", "linux", "arm64"],
        }
        content_list = installer.render(wf_list).decode("utf-8")
        parsed_list = yaml.safe_load(content_list)
        self.assertEqual(parsed_list["jobs"]["ci"]["runs-on"], ["self-hosted", "linux", "arm64"])

    def test_pr_ci_renders_execution_fields(self):
        wf = {
            "template": "pr-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request", "push", "workflow_dispatch"],
            "push_branches": ["main", "dev"],
            "checkout_sha": CHECKOUT,
            "commands": ["pytest -v"],
            "runs_on": "self-hosted",
            "timeout_minutes": 30,
            "container": "python:3.11-slim",
            "env": {"FOO": "bar", "CI_BUILD": "1"},
            "working_directory": "src",
        }
        content = installer.render(wf).decode("utf-8")
        parsed = yaml.load(content, Loader=yaml.BaseLoader)

        job = parsed["jobs"]["ci"]
        self.assertEqual(job["runs-on"], "self-hosted")
        self.assertEqual(job["timeout-minutes"], "30")
        self.assertEqual(job["container"], "python:3.11-slim")
        self.assertEqual(job["env"], {"FOO": "bar", "CI_BUILD": "1"})
        self.assertEqual(job["defaults"]["run"]["working-directory"], "src")

        # Check triggers
        on_dict = parsed["on"]
        self.assertIn("pull_request", on_dict)
        self.assertIn("workflow_dispatch", on_dict)
        self.assertEqual(on_dict["push"]["branches"], ["main", "dev"])

    def test_profile_ci_pins_detect_changes_and_status_check_to_hosted(self):
        wf = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "paths": ["src/**"],
            "runs_on": ["self-hosted", "hpc-node"],
            "python": {
                "versions": ["3.11"],
                "commands": ["pytest"],
            },
        }
        content = installer.render(wf).decode("utf-8")
        parsed = yaml.safe_load(content)

        # detect-changes pinned to ubuntu-24.04
        self.assertEqual(parsed["jobs"]["detect-changes"]["runs-on"], "ubuntu-24.04")
        # status-check pinned to ubuntu-24.04
        self.assertEqual(parsed["jobs"]["status-check"]["runs-on"], "ubuntu-24.04")
        # ci uses runs_on
        self.assertEqual(parsed["jobs"]["ci"]["runs-on"], ["self-hosted", "hpc-node"])


class ForkExecutionGuardTests(unittest.TestCase):
    """Test automatic fork execution guard injection and fail-closed checks."""

    def test_fork_guard_auto_injected_for_public_repo_self_hosted_pr(self):
        wf = {
            "template": "pr-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "commands": ["pytest"],
            "runs_on": "self-hosted",
            "fork_guard": True,
        }
        content = installer.render(wf).decode("utf-8")
        parsed = yaml.safe_load(content)
        self.assertEqual(
            parsed["jobs"]["ci"]["if"],
            "github.event.pull_request.head.repo.full_name == github.repository",
        )

    def test_fork_guard_with_paths_in_profile_ci(self):
        wf = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "profile": "python",
            "paths": ["src/**"],
            "runs_on": "self-hosted",
            "fork_guard": True,
            "python": {
                "versions": ["3.11"],
                "commands": ["pytest"],
            },
        }
        content = installer.render(wf).decode("utf-8")
        parsed = yaml.safe_load(content)
        expected_if = (
            "needs.detect-changes.outputs.run_ci == 'true' && "
            "github.event.pull_request.head.repo.full_name == github.repository"
        )
        self.assertEqual(parsed["jobs"]["ci"]["if"], expected_if)

    def test_dry_run_fails_closed_when_fork_guard_explicitly_disabled_on_public_repo(self):
        api = FakeRepoApi(visibility="public", is_private=False)
        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {
                    "name": "workflows",
                    "workflows": [
                        {
                            "template": "pr-ci",
                            "template_version": 1,
                            "filename": "ci.yml",
                            "triggers": ["pull_request"],
                            "checkout_sha": CHECKOUT,
                            "commands": ["pytest"],
                            "runs_on": "self-hosted",
                            "fork_guard": False,
                        }
                    ],
                }
            ],
        }
        res = setup_mod.run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(res["mode"], "dry-run")
        blocked_change = next((c for c in res["changes"] if c.get("status") == "blocked"), None)
        self.assertIsNotNone(blocked_change)
        self.assertIn("fork", blocked_change["reason"].lower())

    def test_dry_run_surfaces_outside_collaborator_warning_on_public_repo(self):
        api = FakeRepoApi(visibility="public", is_private=False)
        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {
                    "name": "workflows",
                    "workflows": [
                        {
                            "template": "pr-ci",
                            "template_version": 1,
                            "filename": "ci.yml",
                            "triggers": ["pull_request"],
                            "checkout_sha": CHECKOUT,
                            "commands": ["pytest"],
                            "runs_on": "self-hosted",
                        }
                    ],
                }
            ],
        }
        res = setup_mod.run_policy(api, "acme/project", policy, apply=False)
        self.assertEqual(res["mode"], "dry-run")
        warnings = res.get("warnings", [])
        self.assertTrue(
            any("collaborator" in w.lower() for w in warnings),
            f"Expected outside collaborator warning in {warnings}",
        )


class InteractivePromptsTests(unittest.TestCase):
    """Test interactive workflow prompting with runner selection and pr-ci customization."""

    def test_prompt_workflows_stage_default_self_hosted_runner(self):
        # 1. confirm stage workflows: y
        # 2. global runner default (self-hosted): y (default Enter)
        # 3. runner labels: \n (accepts "self-hosted")
        # 4. multichoice: 1 (pr-ci)
        # 5. pr-ci commands: \n (accepts default)
        # 6. pr-ci triggers: \n (accepts default pull_request)
        # 7. pr-ci override runner: n (default Enter)
        in_stream = io.StringIO("y\n\n\n1\n\n\n\n")
        out_stream = io.StringIO()

        stage = interactive_mod.prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        wf = stage["workflows"][0]
        self.assertEqual(wf["template"], "pr-ci")
        self.assertEqual(wf["runs_on"], "self-hosted")
        self.assertEqual(wf["triggers"], ["pull_request"])

    def test_prompt_workflows_stage_hosted_runner_and_pr_ci_customization(self):
        # 1. confirm stage workflows: y
        # 2. global runner self-hosted: n (choose hosted)
        # 3. hosted runner: \n (accepts "ubuntu-24.04")
        # 4. multichoice: 1 (pr-ci)
        # 5. pr-ci commands: ruff check .; pytest -v\n
        # 6. pr-ci triggers: pull_request, push\n
        # 7. pr-ci override runner: n\n
        in_stream = io.StringIO("y\nn\n\n1\nruff check .; pytest -v\npull_request, push\nn\n")
        out_stream = io.StringIO()

        stage = interactive_mod.prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        wf = stage["workflows"][0]
        self.assertEqual(wf["template"], "pr-ci")
        self.assertEqual(wf["runs_on"], "ubuntu-24.04")
        self.assertEqual(wf["commands"], ["ruff check .", "pytest -v"])
        self.assertEqual(wf["triggers"], ["pull_request", "push"])


if __name__ == "__main__":
    unittest.main()
