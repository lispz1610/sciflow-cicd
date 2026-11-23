"""Tests for Dependabot configuration generator with restrained update grouping."""

import sys
from pathlib import Path
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
TESTS_DIR = ROOT_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from api_test_support import CleanRepoApi, FakeApi
import hashlib
import base64


class DependabotFakeApi(CleanRepoApi):
    def request(self, method, path, payload=None):
        base = "/repos/" + self.repo_name
        from urllib.parse import unquote
        clean_path = unquote(path)
        if clean_path.startswith(base + "/contents/.github/dependabot.yml"):
            if method == "GET":
                branch = clean_path.split("?ref=", 1)[1] if "?ref=" in clean_path else self.repo.get("default_branch", "master")
                if (branch, ".github/dependabot.yml") in self.workflow_files:
                    content = self.workflow_files[(branch, ".github/dependabot.yml")]
                    return 200, {
                        "type": "file",
                        "sha": hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest(),
                        "content": base64.b64encode(content).decode(),
                        "encoding": "base64",
                    }
                return 404, None
            if method == "PUT":
                branch = payload["branch"]
                self.writes.append((method, path, payload))
                self.workflow_files[(branch, ".github/dependabot.yml")] = base64.b64decode(payload["content"])
                return 201, {"content": {"sha": "b" * 40}}
        return super().request(method, path, payload)
from scripts.dependabot_generator import (
    validate_dependabot_stage,
    render_dependabot_config,
    detect_dependabot_ecosystems,
)
from scripts.interactive_dependabot import prompt_dependabot_stage
import scripts.setup_github_repo as setup


class DependabotGeneratorTests(unittest.TestCase):
    def test_default_dependabot_stage_validation(self):
        stage = {"name": "dependabot"}
        normalized = validate_dependabot_stage(stage)
        self.assertEqual(normalized["cadence"], "monthly")
        self.assertIn("github-actions", normalized["package_ecosystems"])
        self.assertTrue(normalized["groups"])

    def test_custom_dependabot_stage_validation(self):
        stage = {
            "name": "dependabot",
            "cadence": "weekly",
            "package_ecosystems": ["pip", "github-actions"],
            "open_pull_requests_limit": 5,
        }
        normalized = validate_dependabot_stage(stage)
        self.assertEqual(normalized["cadence"], "weekly")
        self.assertEqual(normalized["package_ecosystems"], ["pip", "github-actions"])
        self.assertEqual(normalized["open_pull_requests_limit"], 5)

    def test_invalid_cadence_rejected(self):
        stage = {"name": "dependabot", "cadence": "hourly"}
        with self.assertRaises(ValueError):
            validate_dependabot_stage(stage)

    def test_unsupported_ecosystems_rejected(self):
        """Speculative ecosystems (npm, cargo, etc.) are pruned and rejected."""
        stage = {"name": "dependabot", "package_ecosystems": ["npm"]}
        with self.assertRaisesRegex(ValueError, "invalid package_ecosystems"):
            validate_dependabot_stage(stage)

    def test_automated_security_fixes_staged_with_vulnerability_alerts(self):
        """Security stage with vulnerability-alerts also stages automated-security-fixes."""
        api = DependabotFakeApi()
        policy = {
            "version": 1,
            "repository": "acme/project",
            "stages": [
                {"name": "security", "enable": ["vulnerability-alerts"]}
            ]
        }
        result = setup.run_policy(api, "acme/project", policy, apply=False)
        changes = result.get("changes", [])
        paths = [c.get("path") for c in changes]
        self.assertIn("/repos/acme/project/vulnerability-alerts", paths)
        self.assertIn("/repos/acme/project/automated-security-fixes", paths)

    def test_render_dependabot_yaml_structure(self):
        stage = {
            "name": "dependabot",
            "cadence": "monthly",
            "package_ecosystems": ["pip", "github-actions"],
            "groups": True,
        }
        yaml_text = render_dependabot_config(stage)
        self.assertIn("version: 2", yaml_text)
        self.assertIn('package-ecosystem: "pip"', yaml_text)
        self.assertIn('package-ecosystem: "github-actions"', yaml_text)
        self.assertIn('interval: "monthly"', yaml_text)
        self.assertIn("groups:", yaml_text)
        self.assertIn('patterns:\n          - "*"', yaml_text)
        self.assertIn('- "minor"', yaml_text)
        self.assertIn('- "patch"', yaml_text)

    def test_policy_validation_accepts_dependabot_stage(self):
        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {"name": "dependabot", "cadence": "monthly"}
            ]
        }
        stages = setup.validate_policy(policy)
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0]["name"], "dependabot")

    def test_policy_dry_run_stages_dependabot_file(self):
        api = DependabotFakeApi()
        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {"name": "dependabot", "cadence": "monthly"}
            ]
        }
        result = setup.run_policy(api, "acme/project", policy, apply=False)
        changes = result.get("changes", [])
        dependabot_changes = [c for c in changes if c.get("path") == ".github/dependabot.yml"]
        self.assertEqual(len(dependabot_changes), 1)
        self.assertEqual(dependabot_changes[0]["status"], "planned")
        self.assertEqual(dependabot_changes[0]["stage"], "dependabot")
        self.assertEqual(dependabot_changes[0]["operation"], "create")

    def test_policy_apply_creates_setup_pull_request(self):
        api = DependabotFakeApi()
        policy = {
            "version": 2,
            "repository": "acme/project",
            "stages": [
                {"name": "dependabot", "cadence": "monthly"}
            ]
        }
        preview = setup.run_policy(api, "acme/project", policy, apply=False)
        applied = setup.run_policy(api, "acme/project", policy, apply=True, accepted_dry_run=preview)
        self.assertIn("setup_pull_request", applied)
        self.assertIn("setup_branch", applied)
        changes = applied.get("changes", [])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["path"], ".github/dependabot.yml")
        self.assertEqual(changes[0]["status"], "pending")


    def test_prompt_dependabot_stage_confirmed(self):
        import io
        from scripts.interactive import Session
        session = Session(
            repository="acme/project",
            api=FakeApi(),
            in_stream=io.StringIO("y\n"),
            out_stream=io.StringIO(),
            err_stream=io.StringIO(),
        )
        stage = prompt_dependabot_stage(session)
        self.assertIsNotNone(stage)
        self.assertEqual(stage["name"], "dependabot")
        self.assertEqual(stage["cadence"], "monthly")

    def test_prompt_dependabot_stage_declined(self):
        import io
        from scripts.interactive import Session
        session = Session(
            repository="acme/project",
            api=FakeApi(),
            in_stream=io.StringIO("n\n"),
            out_stream=io.StringIO(),
            err_stream=io.StringIO(),
        )
        stage = prompt_dependabot_stage(session)
        self.assertIsNone(stage)


if __name__ == "__main__":
    unittest.main()
