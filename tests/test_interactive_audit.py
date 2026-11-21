"""Comprehensive audit of the interactive setup flow (Ticket #14)."""

import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
TESTS_DIR = ROOT_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_setup_github_repo import InventoryApi
from scripts.interactive import Session, run_interactive_workflow
from scripts.interactive_workflows_rulesets import (
    DEFAULT_CHECKOUT_SHA,
    DEFAULT_DEPENDENCY_SHA,
)

CHECKOUT_SHA = DEFAULT_CHECKOUT_SHA
DEPENDENCY_SHA = DEFAULT_DEPENDENCY_SHA


class AuditMockApi(InventoryApi):
    """Enhanced mock API supporting both repository inventory and workflow staging checks."""

    def __init__(self, repo_name="acme/project"):
        super().__init__()
        self.repo["full_name"] = repo_name
        self.repo["default_branch"] = "main"
        self.refs = {"main": "a" * 40}

    def request(self, method, path, payload=None):
        if method == "GET":
            if path == f"/repos/actions/checkout/git/commits/{CHECKOUT_SHA}":
                return 200, {"sha": CHECKOUT_SHA}
            if path == f"/repos/actions/dependency-review-action/git/commits/{DEPENDENCY_SHA}":
                return 200, {"sha": DEPENDENCY_SHA}
            if path.startswith("/repos/actions/checkout/git/commits/"):
                return 200, {"sha": path.split("/")[-1]}
            if path.startswith("/repos/actions/setup-python/git/commits/"):
                return 200, {"sha": path.split("/")[-1]}
            if path.startswith("/repos/astral-sh/setup-uv/git/commits/"):
                return 200, {"sha": path.split("/")[-1]}
            if path.startswith("/repos/actions/dependency-review-action/git/commits/"):
                return 200, {"sha": path.split("/")[-1]}
            if path.startswith("/repos/alibaba/open-code-review/git/commits/"):
                return 200, {"sha": path.split("/")[-1]}
            if path.endswith("/actions/permissions/artifact-and-log-retention"):
                return 200, {"days": 30}
            if "/actions/secrets/" in path or "/actions/variables/" in path:
                return 404, None
            if path.startswith(f"/repos/{self.repo['full_name']}/git/ref/heads/"):
                branch = path.removeprefix(f"/repos/{self.repo['full_name']}/git/ref/heads/")
                return (200, {"object": {"sha": self.refs[branch]}}) if branch in self.refs else (404, None)
            if path.startswith(f"/repos/{self.repo['full_name']}/pulls"):
                return 200, []
            if path.startswith(f"/repos/{self.repo['full_name']}/contents/"):
                return 404, None
        return super().request(method, path, payload)


class InteractiveSetupAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_fresh_repo_confirmation_rejection_prevents_all_writes(self):
        """Fresh repo with confirmation rejection must perform strictly 0 writes."""
        api = AuditMockApi("acme/project")
        temp_policy_path = Path(self.temp_dir) / ".local" / "setup-policy.json"

        # Inputs:
        # 1-click: c (customize)
        # features: 1 (Issues)
        # merge: n
        # actions: y
        # security: 1 (vulnerability-alerts)
        # content: n
        # workflows: n
        # gates: n
        # apply confirm: n (Rejection!)
        inputs = "c\n1\nn\ny\n1\nn\nn\nn\nn\nn\n"
        in_stream = io.StringIO(inputs)
        out_stream = io.StringIO()

        session = Session(
            repository="acme/project",
            api=api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
        )

        with patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", temp_policy_path):
            exit_code = run_interactive_workflow(session)

        self.assertEqual(exit_code, 0)
        # Strictly zero writes
        self.assertEqual(api.writes, [], "Rejection of confirmation must prevent all writes")
        # Auto-saved policy must be persisted for inspection/headless re-run
        self.assertTrue(temp_policy_path.exists())
        policy = json.loads(temp_policy_path.read_text(encoding="utf-8"))
        self.assertEqual(policy["repository"], "acme/project")
        output = out_stream.getvalue()
        self.assertIn("cancelled", output.lower())

    def test_fresh_repo_confirmation_acceptance_applies_changes(self):
        """Fresh repo with confirmation acceptance must apply changes and output summary."""
        api = AuditMockApi("acme/project")
        temp_policy_path = Path(self.temp_dir) / ".local" / "setup-policy.json"

        # Inputs:
        # 1-click: c (customize)
        # features: 1 (Issues)
        # merge: n
        # actions: y
        # security: 1 (vulnerability-alerts)
        # content: n
        # workflows: n
        # gates: n
        # apply confirm: y (Acceptance!)
        inputs = "c\n1\nn\ny\n1\nn\nn\nn\nn\nn\ny\n"
        in_stream = io.StringIO(inputs)
        out_stream = io.StringIO()

        session = Session(
            repository="acme/project",
            api=api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
        )

        with patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", temp_policy_path):
            exit_code = run_interactive_workflow(session)

        self.assertEqual(exit_code, 0)
        # Writes were executed
        self.assertTrue(len(api.writes) > 0, "Acceptance must execute planned writes")
        output = out_stream.getvalue()
        self.assertIn("Apply Summary:", output)
        self.assertIn("Verified:", output)

    def test_preconfigured_repo_resume_mode_preserves_existing_settings(self):
        """Pre-configured repository in Resume mode preserves existing inventory defaults."""
        api = AuditMockApi("acme/project")
        api.repo["has_issues"] = True
        api.repo["has_wiki"] = True
        api.actions["enabled"] = True

        temp_policy_path = Path(self.temp_dir) / ".local" / "setup-policy.json"

        # Inputs:
        # 1-click: c (customize)
        # features: accept defaults (press Enter)
        # merge: n
        # actions: accept defaults (press Enter -> y)
        # security: none
        # content: n
        # workflows: n
        # gates: n
        # apply confirm: n
        inputs = "c\n\nn\n\nnone\nn\nn\nn\nn\nn\n"
        in_stream = io.StringIO(inputs)
        out_stream = io.StringIO()

        session = Session(
            repository="acme/project",
            api=api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=True,
            defaults={
                "features": ["issues", "wiki"],
                "actions": {"enabled": True, "default_workflow_permissions": "read"},
            },
        )

        with patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", temp_policy_path):
            exit_code = run_interactive_workflow(session)

        self.assertEqual(exit_code, 0)
        self.assertTrue(temp_policy_path.exists())
        policy = json.loads(temp_policy_path.read_text(encoding="utf-8"))
        features_stage = next(s for s in policy["stages"] if s["name"] == "features")
        self.assertIn("issues", features_stage["enable"])
        self.assertIn("wiki", features_stage["enable"])

    def test_preconfigured_repo_scratch_mode_clears_defaults(self):
        """Pre-configured repository in Scratch mode ignores existing inventory defaults."""
        api = AuditMockApi("acme/project")
        api.repo["has_issues"] = True
        api.repo["has_wiki"] = True

        temp_policy_path = Path(self.temp_dir) / ".local" / "setup-policy.json"

        # User starts from scratch: defaults are empty
        # Inputs:
        # 1-click: c (customize)
        # features: 1 (only issues, not wiki)
        # merge: n
        # actions: n
        # security: none
        # content: n
        # workflows: n
        # gates: n
        # apply confirm: n
        inputs = "c\n1\nn\nn\nnone\nn\nn\nn\nn\nn\n"
        in_stream = io.StringIO(inputs)
        out_stream = io.StringIO()

        session = Session(
            repository="acme/project",
            api=api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
            defaults={},
        )

        with patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", temp_policy_path):
            exit_code = run_interactive_workflow(session)

        self.assertEqual(exit_code, 0)
        policy = json.loads(temp_policy_path.read_text(encoding="utf-8"))
        features_stage = next(s for s in policy["stages"] if s["name"] == "features")
        self.assertEqual(features_stage["enable"], ["issues"])

    def test_interactive_workflow_selection_creates_v2_policy_and_setup_pr_notice(self):
        """Selecting CI and advisory review creates Policy v2 and displays Setup PR notice."""
        api = AuditMockApi("acme/project")
        api.repo["visibility"] = "private"
        temp_policy_path = Path(self.temp_dir) / ".local" / "setup-policy.json"

        # Inputs:
        # 1-click: c (customize)
        # features: none
        # merge: n
        # actions: y
        # security: none
        # content: n
        # workflows confirm: y
        # select workflows: 1 (Standard CI)
        # Stage Advisory Review?: y
        # Use Anthropic-compatible protocol?: n
        # Provider HTTPS endpoint: https://api.example.com/v1
        # Provider model: gpt-4o-mini
        # LLM parameter configuration: Enter (default: Use provider defaults)
        # Review Run events: Enter (default)
        # Max tokens budget: Enter (default)
        # Timeout minutes: Enter (default)
        # Advanced options: n
        # gates: n
        # apply confirm: n
        inputs = "c\nnone\nn\ny\nnone\nn\ny\n1\ny\nn\nhttps://api.example.com/v1\ngpt-4o-mini\n\n\n\n\nn\nn\nn\nn\n"
        in_stream = io.StringIO(inputs)
        out_stream = io.StringIO()

        session = Session(
            repository="acme/project",
            api=api,
            repo_data={"visibility": "private", "default_branch": "main"},
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
        )

        with patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", temp_policy_path):
            exit_code = run_interactive_workflow(session)

        self.assertEqual(exit_code, 0)
        self.assertTrue(temp_policy_path.exists())
        policy = json.loads(temp_policy_path.read_text(encoding="utf-8"))
        # Policy version must be 2 when review stage is present
        self.assertEqual(policy["version"], 2)
        stage_names = [s["name"] for s in policy["stages"]]
        self.assertIn("review", stage_names)
        self.assertIn("workflows", stage_names)

        output = out_stream.getvalue()
        # Must show Dry-Run summary and note that owner must inspect and merge Setup PR
        self.assertIn("Dry-Run Summary", output)
        self.assertIn("Setup Pull Request", output)


if __name__ == "__main__":
    unittest.main()
