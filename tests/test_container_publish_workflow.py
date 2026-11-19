"""Tests for container-publish workflow alignment with self-hosted runners, triggers, and interactive setup."""

import io
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock

try:
    import yaml
except ImportError:
    yaml = None

SCRIPTS_DIR = str(Path(__file__).resolve().parents[1] / "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from interactive import Session
from interactive_workflows_rulesets import prompt_workflows_stage
import install_github_workflows as installer
from install_github_workflows import (
    DEFAULT_CHECKOUT_SHA,
    DEFAULT_CONTAINER_LOGIN_SHA,
    DEFAULT_CONTAINER_BUILD_SHA,
    render,
    validate_policy,
)


class ContainerPublishWorkflowTests(unittest.TestCase):
    """Test suite for container-publish workflow template enhancements (Issue #37)."""

    def test_default_runs_on_is_self_hosted(self):
        """Omitting runs_on defaults to 'self-hosted' for container-publish."""
        wf = {
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "login_action_sha": DEFAULT_CONTAINER_LOGIN_SHA,
            "build_action_sha": DEFAULT_CONTAINER_BUILD_SHA,
        }
        content = render(wf).decode("utf-8")
        self.assertIn("runs-on: self-hosted", content)
        if yaml is not None:
            parsed = yaml.safe_load(content)
            self.assertEqual(parsed["jobs"]["publish-container"]["runs-on"], "self-hosted")

    def test_custom_runs_on_string_and_labels_list(self):
        """Custom runs_on accepts non-empty string or list of label strings."""
        wf_str = {
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "runs_on": "ubuntu-24.04",
        }
        content_str = render(wf_str).decode("utf-8")
        self.assertIn("runs-on: ubuntu-24.04", content_str)

        wf_list = {
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "runs_on": ["self-hosted", "linux", "arm64"],
        }
        content_list = render(wf_list).decode("utf-8")
        if yaml is not None:
            parsed = yaml.safe_load(content_list)
            self.assertEqual(
                parsed["jobs"]["publish-container"]["runs-on"],
                ["self-hosted", "linux", "arm64"],
            )

    def test_fork_execution_guard_on_public_repo_and_flag(self):
        """Fork guard is applied on publish-container job when running on self-hosted with PR trigger."""
        # 1. fork_guard: True on self-hosted
        wf_guard = {
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": ["pull_request", "push"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "runs_on": "self-hosted",
            "fork_guard": True,
        }
        content = render(wf_guard).decode("utf-8")
        if yaml is not None:
            parsed = yaml.safe_load(content)
            self.assertEqual(
                parsed["jobs"]["publish-container"]["if"],
                "github.event.pull_request.head.repo.full_name == github.repository",
            )

        # 2. is_public: True on self-hosted
        wf_pub = {
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "runs_on": "self-hosted",
            "is_public": True,
        }
        content_pub = render(wf_pub).decode("utf-8")
        if yaml is not None:
            parsed_pub = yaml.safe_load(content_pub)
            self.assertEqual(
                parsed_pub["jobs"]["publish-container"]["if"],
                "github.event.pull_request.head.repo.full_name == github.repository",
            )

    def test_fork_execution_guard_not_applied_when_conditions_not_met(self):
        """Fork guard is omitted when running on hosted runner, or pull_request is absent, or fork_guard is False."""
        # 1. Hosted runner (ubuntu-24.04)
        wf_hosted = {
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "runs_on": "ubuntu-24.04",
            "fork_guard": True,
        }
        content_hosted = render(wf_hosted).decode("utf-8")
        if yaml is not None:
            parsed = yaml.safe_load(content_hosted)
            self.assertNotIn("if", parsed["jobs"]["publish-container"])

        # 2. pull_request not in triggers (e.g. push only)
        wf_push_only = {
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": ["push"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "runs_on": "self-hosted",
            "fork_guard": True,
        }
        content_push = render(wf_push_only).decode("utf-8")
        if yaml is not None:
            parsed = yaml.safe_load(content_push)
            self.assertNotIn("if", parsed["jobs"]["publish-container"])

        # 3. fork_guard: False on private repo
        wf_priv = {
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "runs_on": "self-hosted",
            "fork_guard": False,
        }
        content_priv = render(wf_priv).decode("utf-8")
        if yaml is not None:
            parsed = yaml.safe_load(content_priv)
            self.assertNotIn("if", parsed["jobs"]["publish-container"])

    def test_pull_request_non_publishing_behaviour(self):
        """PR runs guard login and set push: false via ${{ github.event_name != 'pull_request' }}."""
        wf = {
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": ["pull_request", "push"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
        }
        content = render(wf).decode("utf-8")
        if yaml is not None:
            parsed = yaml.safe_load(content)
            steps = parsed["jobs"]["publish-container"]["steps"]
            login_step = next(s for s in steps if "docker/login-action" in s.get("uses", ""))
            self.assertEqual(login_step.get("if"), "github.event_name != 'pull_request'")
            build_step = next(s for s in steps if "docker/build-push-action" in s.get("uses", ""))
            self.assertEqual(build_step["with"]["push"], "${{ github.event_name != 'pull_request' }}")

    def test_configurable_registry_dockerfile_context_tags(self):
        """Workflow renders custom registry, dockerfile, context, and tags."""
        wf = {
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": ["push"],
            "push_branches": ["main"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "registry": "docker.io",
            "dockerfile": "docker/Dockerfile.prod",
            "context": "./backend",
            "tags": ["docker.io/acme/app:latest", "docker.io/acme/app:${{ github.sha }}"],
        }
        content = render(wf).decode("utf-8")
        self.assertIn("registry: docker.io", content)
        self.assertIn("file: docker/Dockerfile.prod", content)
        self.assertIn("context: ./backend", content)
        self.assertIn("docker.io/acme/app:latest", content)
        self.assertIn("docker.io/acme/app:${{ github.sha }}", content)

        if yaml is not None:
            parsed = yaml.safe_load(content)
            steps = parsed["jobs"]["publish-container"]["steps"]
            login_step = next(s for s in steps if "docker/login-action" in s.get("uses", ""))
            self.assertEqual(login_step["with"]["registry"], "docker.io")
            self.assertEqual(login_step["name"], "Log in to docker.io")

            build_step = next(s for s in steps if "docker/build-push-action" in s.get("uses", ""))
            self.assertEqual(build_step["with"]["context"], "./backend")
            self.assertEqual(build_step["with"]["file"], "docker/Dockerfile.prod")

    def test_configurable_triggers_push_workflow_dispatch_schedule(self):
        """Workflow accepts varied triggers without requiring pull_request."""
        wf = {
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": ["push", "workflow_dispatch", "schedule"],
            "push_branches": ["main", "v*"],
            "schedule": "0 2 * * *",
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
        }
        content = render(wf).decode("utf-8")
        if yaml is not None:
            parsed = yaml.safe_load(content)
            on = parsed["on"] if "on" in parsed else parsed[True]
            self.assertIn("workflow_dispatch", on)
            self.assertEqual(on["push"]["branches"], ["main", "v*"])
            self.assertEqual(on["schedule"], [{"cron": "0 2 * * *"}])

    def test_interactive_setup_prompts_custom_options(self):
        """Interactive setup prompts for triggers, registry, dockerfile, context, and tags."""
        # 1. confirm workflows: y
        # 2. use self-hosted default Yes: \n
        # 3. runner labels default self-hosted: \n
        # 4. select workflows: 5 (container-publish)
        # 5. triggers: pull_request, push, workflow_dispatch
        # 6. registry: quay.io
        # 7. dockerfile: deploy/Dockerfile
        # 8. context: ./services
        # 9. tags: quay.io/org/repo:edge, quay.io/org/repo:${{ github.sha }}
        in_stream = io.StringIO(
            "y\n\n\n5\n"
            "pull_request, push, workflow_dispatch\n"
            "quay.io\n"
            "deploy/Dockerfile\n"
            "./services\n"
            "quay.io/org/repo:edge, quay.io/org/repo:${{ github.sha }}\n"
        )
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(len(stage["workflows"]), 1)
        wf = stage["workflows"][0]
        self.assertEqual(wf["template"], "container-publish")
        self.assertEqual(wf["triggers"], ["pull_request", "push", "workflow_dispatch"])
        self.assertEqual(wf["registry"], "quay.io")
        self.assertEqual(wf["dockerfile"], "deploy/Dockerfile")
        self.assertEqual(wf["context"], "./services")
        self.assertEqual(
            wf["tags"],
            ["quay.io/org/repo:edge", "quay.io/org/repo:${{ github.sha }}"],
        )
        self.assertEqual(wf["runs_on"], "self-hosted")

        rendered = render(wf).decode("utf-8")
        if yaml is not None:
            parsed = yaml.safe_load(rendered)
            self.assertEqual(parsed["jobs"]["publish-container"]["runs-on"], "self-hosted")
            steps = parsed["jobs"]["publish-container"]["steps"]
            build_step = next(s for s in steps if "docker/build-push-action" in s.get("uses", ""))
            self.assertEqual(build_step["with"]["file"], "deploy/Dockerfile")
            self.assertEqual(build_step["with"]["context"], "./services")

    def test_interactive_setup_inherits_custom_hosted_runner(self):
        """Container workflow inherits user-specified global runner."""
        # 1. confirm workflows: y
        # 2. use self-hosted: n
        # 3. hosted runner: ubuntu-22.04
        # 4. select workflows: 5 (container-publish)
        # 5-9: accept all defaults (\n * 5)
        in_stream = io.StringIO("y\nn\nubuntu-22.04\n5\n\n\n\n\n\n")
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        wf = stage["workflows"][0]
        self.assertEqual(wf["runs_on"], "ubuntu-22.04")
        rendered = render(wf).decode("utf-8")
        if yaml is not None:
            parsed = yaml.safe_load(rendered)
            self.assertEqual(parsed["jobs"]["publish-container"]["runs-on"], "ubuntu-22.04")


if __name__ == "__main__":
    unittest.main()
