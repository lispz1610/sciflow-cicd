"""Smoke tests for running issue gate operator scripts by file path."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class TestIssueGateCliImports(unittest.TestCase):
    def test_refresh_script_starts_from_outside_the_repository(self):
        script = REPOSITORY_ROOT / "scripts/refresh_issue_state_gate.py"
        with tempfile.TemporaryDirectory() as working_directory:
            event_path = Path(working_directory) / "event.json"
            event_path.write_text(json.dumps({"pull_request": {"number": 2}}), encoding="utf-8")
            with patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(event_path)}):
                child_env = {key: value for key, value in os.environ.items()
                             if not key.startswith("GITHUB_")}
                result = subprocess.run(
                    [sys.executable, str(script)],
                    cwd=working_directory,
                    env=child_env,
                    capture_output=True,
                    text=True,
                    check=False,
                )

        self.assertEqual(result.returncode, 2)
        self.assertIn("GITHUB_EVENT_PATH is required", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_reconcile_script_help_works_from_outside_the_repository(self):
        script = REPOSITORY_ROOT / "scripts/reconcile_issue_state_gate.py"
        with tempfile.TemporaryDirectory() as working_directory:
            result = subprocess.run(
                [sys.executable, str(script), "--help"],
                cwd=working_directory,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Reconcile Issue State Gate", result.stdout)


if __name__ == "__main__":
    unittest.main()
