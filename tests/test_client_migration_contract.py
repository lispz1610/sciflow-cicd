"""Migration behavior at the public sciflow-cicd boundary."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.cli import main
from api_test_support import CleanRepoApi


class ClientMigrationContractTests(unittest.TestCase):
    def test_retired_links_policy_stage_directs_user_to_supported_command_before_api_access(self):
        api = CleanRepoApi()
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "policy.json"
            policy_path.write_text(json.dumps({
                "version": 1, "repository": "acme/project",
                "stages": [{"name": "links", "items": []}],
            }), encoding="utf-8")
            error = io.StringIO()
            with patch("scripts.headless.GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "fake-token"}), \
                 contextlib.redirect_stderr(error):
                code = main(["apply", "--policy", str(policy_path)])

        self.assertEqual(code, 1)
        self.assertIn("reconcile_github_links.py", error.getvalue())
        self.assertEqual(api.writes, [])


if __name__ == "__main__":
    unittest.main()
