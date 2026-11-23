"""Regression tests for P2 workflow/prompt/dependabot/inventory deduplication."""

import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
for _path in (str(ROOT_DIR), str(SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from scripts import dependabot_generator, install_github_workflows, repository_capabilities
from scripts.dependabot_generator import render_dependabot_config, validate_dependabot_stage
from scripts.install_github_workflows import LEGACY_MARKER, MARKER, _glob_to_regex, match_path_patterns


class PathMatchingParityTests(unittest.TestCase):
    def test_rendered_detect_changes_python_matches_local_glob_semantics(self):
        paths = ["scripts/**/*.py", "docs/**", "src/*.c", "README.md", "tests/**"]
        lines = install_github_workflows._render_detect_changes_job_lines("checkout" * 10, paths)
        rendered = "\n".join(lines)
        self.assertIn("import re", rendered)
        self.assertIn("_glob_to_regex", rendered)
        cases = {
            "scripts/foo.py": True,
            "scripts/a/b/c.py": True,
            "docs/guide/index.md": True,
            "src/main.c": True,
            "src/a/b.c": False,
            "README.md": True,
            "tests/unit/test_x.py": True,
            "other/file.py": False,
        }
        # Execute the exact inline source embedded in the workflow and compare
        # its decisions to the local helpers across many paths.
        namespace = {}
        exec(install_github_workflows.INLINE_PATH_MATCHER_SOURCE, namespace)
        inline_match = namespace["paths_match"]
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(inline_match([path], paths), expected)
                self.assertEqual(match_path_patterns([path], paths), expected)
                self.assertEqual(inline_match([path], paths),
                                 match_path_patterns([path], paths))

    def test_glob_regex_handles_globstar_and_question_mark(self):
        self.assertTrue(_glob_to_regex("src/**/*.py").match("src/a/b.py"))
        self.assertTrue(_glob_to_regex("src/**/*.py").match("src/b.py"))
        self.assertFalse(_glob_to_regex("src/**/*.py").match("src/a/b.c"))
        self.assertTrue(_glob_to_regex("src/?.py").match("src/a.py"))


class ExpectedShaHelperTests(unittest.TestCase):
    def test_helper_recognizes_current_and_legacy_managed_markers(self):
        from scripts.interactive_workflows_rulesets import managed_workflow_expected_sha

        def response(content, sha):
            encoded = base64.b64encode(content.encode()).decode()
            return (200, {"type": "file", "encoding": "base64", "content": encoded, "sha": sha})

        api = MagicMock()
        marker_sha = "a" * 40
        legacy_sha = "b" * 40
        api.request.side_effect = [
            response(MARKER + "profile-ci version=1\n", marker_sha),
            response(LEGACY_MARKER + "profile-ci version=1\n", legacy_sha),
            response("# unmanaged\n", "c" * 40),
        ]
        self.assertEqual(managed_workflow_expected_sha(api, "acme/project", "a.yml", "main"), marker_sha)
        self.assertEqual(managed_workflow_expected_sha(api, "acme/project", "b.yml", "main"), legacy_sha)
        self.assertIsNone(managed_workflow_expected_sha(api, "acme/project", "c.yml", "main"))


class InventorySinglePassTests(unittest.TestCase):
    def test_repository_capabilities_reuse_predetected_plan_and_scopes(self):
        api = MagicMock()
        api.request.return_value = (200, {"login": "acme", "plan": {"name": "pro"}})
        repo = {"owner": {"login": "acme", "type": "User"}, "permissions": {"push": True}}
        inv = {"repository": {}, "features": {}, "actions": {}, "security_and_analysis": {}}

        plan_name, plan = repository_capabilities.detect_repository_plan(api, repo)
        self.assertEqual(plan_name, "pro")
        perms = repository_capabilities.detect_caller_permissions(repo, api=api)
        before = len(api.request.call_args_list)
        repository_capabilities.evaluate_repository_capabilities(
            api, repo, inv, plan_name=plan_name, plan=plan, caller_permissions=perms
        )
        self.assertEqual(len(api.request.call_args_list), before,
                         "capability evaluation must not repeat /user or plan probes")


class DependabotGroupsTests(unittest.TestCase):
    def test_custom_groups_mapping_is_rendered(self):
        stage = {
            "name": "dependabot",
            "package_ecosystems": ["pip"],
            "groups": {
                "security": {
                    "patterns": ["numpy*", "scipy*"],
                    "update-types": ["minor", "patch"],
                }
            },
        }
        config = validate_dependabot_stage(stage)
        self.assertEqual(config["groups"], stage["groups"])
        text = render_dependabot_config(stage)
        self.assertIn("      security:", text)
        self.assertIn('          - "numpy*"', text)
        self.assertIn('          - "scipy*"', text)
        self.assertNotIn("      pip-dependencies:", text)


class SharedRunnerAndCheckSpecTests(unittest.TestCase):
    def test_check_spec_parsing_is_shared_between_prompts_and_setup(self):
        from scripts import interactive_workflows_rulesets, setup_github_repo
        from scripts.workflow_defaults import parse_check_spec

        specs = [
            "ci-check",
            "dependency-review",
            ".github/workflows/ci.yml=status-check",
            "check=ci.yml",
        ]
        for spec in specs:
            with self.subTest(spec=spec):
                expected = parse_check_spec(spec)
                self.assertEqual(interactive_workflows_rulesets.parse_check_spec(spec), expected)
                self.assertEqual(
                    setup_github_repo._gate_checks({"checks": [spec]}), expected)

    def test_runner_target_helpers(self):
        from scripts.workflow_defaults import normalize_runner_labels, resolve_runner_target

        self.assertEqual(
            normalize_runner_labels("self-hosted, gpu", default="self-hosted"),
            ["self-hosted", "gpu"])
        self.assertEqual(normalize_runner_labels(""), "self-hosted")
        self.assertEqual(resolve_runner_target("global", None), "global")
        self.assertEqual(resolve_runner_target("global", "override"), "override")

    def test_hosted_runner_default_matches_canonical_glossary(self):
        from scripts.workflow_defaults import DEFAULT_HOSTED_RUNNER

        self.assertEqual(DEFAULT_HOSTED_RUNNER, "ubuntu-24.04")


class SharedWorkflowRenderingTests(unittest.TestCase):
    FORK_GUARD = (
        "github.event_name != 'pull_request' || "
        "github.event.pull_request.head.repo.full_name == github.repository"
    )

    def _render(self, template, **extra):
        workflow = {
            "template": template,
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": ["push", "schedule", "pull_request"],
            "checkout_sha": "a" * 40,
            "push_branches": ["main"],
            "schedule": ["0 2 * * 1"],
            "fork_guard": True,
            "is_public": True,
            "runs_on": "self-hosted",
            **extra,
        }
        return install_github_workflows.render(workflow).decode("utf-8")

    def test_trigger_and_fork_guard_rendering_is_shared_across_templates(self):
        rendered = (
            self._render("profile-ci", profile="python", commands=["echo hi"]),
            self._render("pr-ci", commands=["echo hi"]),
            self._render("container-publish"),
        )
        for content in rendered:
            with self.subTest(content=content.splitlines()[0]):
                self.assertIn('  push:\n    branches:\n      - "main"', content)
                self.assertIn('  schedule:\n    - cron: "0 2 * * 1"', content)
                self.assertIn(f"    if: {self.FORK_GUARD}", content)

    def test_detect_changes_and_fork_guard_condition_is_shared(self):
        expected = f"    if: needs.detect-changes.outputs.run_ci == 'true' && ({self.FORK_GUARD})"
        for template, extra in (
            ("profile-ci", {"profile": "python", "commands": ["echo hi"]}),
            ("pr-ci", {"commands": ["echo hi"]}),
        ):
            with self.subTest(template=template):
                content = self._render(
                    template, triggers=["pull_request"], paths=["**/*.py"], **extra)
                self.assertIn("    needs: [detect-changes]", content)
                self.assertIn(expected, content)


class SharedPinCollectionTests(unittest.TestCase):
    def test_install_and_install_selected_use_shared_pin_helper(self):
        self.assertTrue(callable(install_github_workflows.collect_workflow_action_pins))
        workflows = [{
            "template": "profile-ci",
            "checkout_sha": "a" * 40,
            "profile": "python",
            "setup_python_sha": "b" * 40,
        }]
        pins = install_github_workflows.collect_workflow_action_pins(workflows)
        self.assertIn(("actions/checkout", "a" * 40), pins)
        self.assertIn(("actions/setup-python", "b" * 40), pins)

    def test_centralized_python_defaults(self):
        from scripts.workflow_defaults import DEFAULT_PYTHON_VERSIONS, DEFAULT_TEST_COMMANDS

        self.assertIn("3.11", DEFAULT_PYTHON_VERSIONS)
        self.assertTrue(any("unittest" in cmd for cmd in DEFAULT_TEST_COMMANDS))


if __name__ == "__main__":
    unittest.main()
