"""Tests for opt-in container build and GHCR publication workflow."""

import importlib.util
from pathlib import Path
import sys
import unittest

try:
    import yaml
except ImportError:
    yaml = None

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_github_workflows.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("install_github_workflows", SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

from tests.test_install_github_workflows import WorkflowApi, CHECKOUT, DEPENDENCY
from scripts.interactive_workflows_rulesets import prompt_workflows_stage
from scripts.interactive import Session
import io
from unittest.mock import MagicMock

LOGIN_SHA = "9780b0c442fbb1117ed29e0efdff1e18412f7567"
BUILD_SHA = "471d1dc4e07e5cdedd4c2171150001c434f0b7a4"


class ContainerWorkflowApi(WorkflowApi):
    """WorkflowApi with pinned Docker action commit endpoints."""

    def request(self, method, path, payload=None):
        if method == "GET":
            docker_commits = {
                f"/repos/docker/login-action/git/commits/{LOGIN_SHA}": LOGIN_SHA,
                f"/repos/docker/build-push-action/git/commits/{BUILD_SHA}": BUILD_SHA,
            }
            if path in docker_commits:
                return 200, {"sha": docker_commits[path]}
        return super().request(method, path, payload)


class ContainerWorkflowTests(unittest.TestCase):
    def test_default_install_contains_no_container_workflow(self):
        """Default install and policies must never install or contain a container workflow."""
        api = ContainerWorkflowApi()
        default_policy = {
            "version": 1,
            "workflows": [
                {
                    "template": "pr-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT,
                    "commands": ["python3 -m unittest discover -s tests"],
                }
            ],
        }
        plan = installer.install(api, "acme/project", default_policy, apply=False)
        staged_paths = [change["path"] for change in plan["changes"]]
        for path in staged_paths:
            self.assertNotIn("container", path.lower())
            self.assertNotIn("ghcr", path.lower())

    def test_opt_in_container_selection_renders_least_privilege_workflow(self):
        """Selecting container template renders a least-privilege GHCR publish workflow."""
        selected = {
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "login_action_sha": LOGIN_SHA,
            "build_action_sha": BUILD_SHA,
        }
        content = installer.render(selected).decode("utf-8")

        # Acceptance criteria checks:
        # 1. Pinned Action SHAs
        self.assertIn(f"actions/checkout@{CHECKOUT}", content)
        self.assertIn(f"docker/login-action@{LOGIN_SHA}", content)
        self.assertIn(f"docker/build-push-action@{BUILD_SHA}", content)

        # 2. Push to ghcr.io
        self.assertIn("ghcr.io", content)
        self.assertIn("registry: ghcr.io", content)
        self.assertIn("push: ${{ github.event_name != 'pull_request' }}", content)
        self.assertNotIn("push: true", content)

        # 3. Least privilege permissions: packages: write, contents: read only
        self.assertIn("permissions:", content)
        self.assertIn("contents: read", content)
        self.assertIn("packages: write", content)

        # 4. Guard login step against PR push
        self.assertIn("if: github.event_name != 'pull_request'", content)

        if yaml is not None:
            parsed = yaml.load(content, Loader=yaml.BaseLoader)
            self.assertEqual(parsed["permissions"], {"contents": "read", "packages": "write"})
            steps = parsed["jobs"]["publish-container"]["steps"]
            uses = [s.get("uses") for s in steps if "uses" in s]
            self.assertEqual(len(uses), 3)
            for action_ref in uses:
                # Must be pinned to 40-character SHA
                self.assertRegex(action_ref, r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-fA-F]{40}$")
            login_step = next(s for s in steps if s.get("name") == "Log in to GitHub Container Registry")
            self.assertEqual(login_step.get("if"), "github.event_name != 'pull_request'")
            build_step = next(s for s in steps if s.get("name") == "Build and push container image")
            self.assertEqual(build_step["with"]["push"], "${{ github.event_name != 'pull_request' }}")

    def test_opt_in_container_stages_in_draft_pr_with_pinned_actions(self):
        """Opt-in container workflow stages through draft Setup PR and checks action pins."""
        api = ContainerWorkflowApi()
        container_policy = {
            "version": 1,
            "workflows": [
                {
                    "template": "container-publish",
                    "template_version": 1,
                    "filename": "container-publish.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT,
                    "login_action_sha": LOGIN_SHA,
                    "build_action_sha": BUILD_SHA,
                }
            ],
        }
        result = installer.install(api, "acme/project", container_policy, apply=True)
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(len(api.prs), 1)
        staged_content = api.files[(result["branch"], ".github/workflows/container-publish.yml")][0]
        self.assertIn(b"packages: write", staged_content)
        self.assertIn(b"registry: ghcr.io", staged_content)

    def test_unverified_action_commit_blocks_container_workflow_staging(self):
        """Nonexistent action pin fails before writing."""
        api = ContainerWorkflowApi()
        unverified_policy = {
            "version": 1,
            "workflows": [
                {
                    "template": "container-publish",
                    "template_version": 1,
                    "filename": "container-publish.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": CHECKOUT,
                    "login_action_sha": "0" * 40,
                    "build_action_sha": BUILD_SHA,
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "pinned action commit does not exist"):
            installer.install(api, "acme/project", unverified_policy, apply=True)
        self.assertEqual(api.writes, [])

    def test_container_alias_and_default_action_shas(self):
        """'container' template alias uses default trusted pinned SHAs when omitted."""
        api = ContainerWorkflowApi()
        selected = {
            "template": "container",
            "template_version": 1,
            "filename": "container.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
        }
        content = installer.render(selected).decode("utf-8")
        self.assertIn("workflow-template=container version=1", content)
        self.assertIn("docker/login-action@" + installer.DEFAULT_CONTAINER_LOGIN_SHA, content)
        self.assertIn("docker/build-push-action@" + installer.DEFAULT_CONTAINER_BUILD_SHA, content)
        self.assertIn("packages: write", content)
        self.assertIn("contents: read", content)
        self.assertIn("push: ${{ github.event_name != 'pull_request' }}", content)
        self.assertIn("if: github.event_name != 'pull_request'", content)

    def test_container_workflow_guards_pr_push_and_login(self):
        """Container workflow skips login and disables push on pull requests."""
        selected = {
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": ["pull_request"],
            "checkout_sha": CHECKOUT,
            "login_action_sha": LOGIN_SHA,
            "build_action_sha": BUILD_SHA,
        }
        content = installer.render(selected).decode("utf-8")
        self.assertIn("if: github.event_name != 'pull_request'", content)
        self.assertIn("push: ${{ github.event_name != 'pull_request' }}", content)
        self.assertNotIn("push: true", content)
        self.assertIn("packages: write", content)
        self.assertIn("contents: read", content)

    def test_interactive_default_does_not_install_container_workflow(self):
        """Interactive prompt default selection does not include container workflow."""
        in_stream = io.StringIO("y\n\n\n\n\n\n\n\n")  # Accept workflows, accept default options
        out_stream = io.StringIO()
        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        templates = [wf["template"] for wf in stage["workflows"]]
        self.assertNotIn("container-publish", templates)
        self.assertNotIn("container", templates)

    def test_interactive_opt_in_container_workflow(self):
        """User explicitly choosing container option installs the container workflow."""
        in_stream = io.StringIO("y\n\n\n5\n")
        out_stream = io.StringIO()
        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        templates = [wf["template"] for wf in stage["workflows"]]
        self.assertEqual(templates, ["container-publish"])
        wf = stage["workflows"][0]
        self.assertEqual(wf["filename"], "container-publish.yml")
        self.assertIn("checkout_sha", wf)
        self.assertIn("login_action_sha", wf)
        self.assertIn("build_action_sha", wf)

    def test_interactive_resume_preselects_container_when_in_defaults(self):
        """Resume mode includes container when present in previous defaults."""
        session = Session(
            repository="owner/repo",
            api=MagicMock(),
            resume_mode=True,
            defaults={"workflows": ["container-publish"]},
        )
        in_stream = io.StringIO("y\n\n\n\n")
        out_stream = io.StringIO()
        stage = prompt_workflows_stage(session=session, in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(len(stage["workflows"]), 1)
        self.assertEqual(stage["workflows"][0]["template"], "container-publish")


if __name__ == "__main__":
    unittest.main()
