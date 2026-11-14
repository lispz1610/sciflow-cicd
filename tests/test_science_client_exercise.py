"""Public client exercise against a disposable scientific repository API."""

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from scripts import cli, interactive


class OptionalReadApi:
    def __init__(self):
        self.writes = []

    def request(self, method, path, payload=None):
        if method != "GET":
            self.writes.append((method, path, payload))
        base = "/repos/acme/science-sandbox"
        if path == base:
            return 200, {
                "full_name": "acme/science-sandbox", "visibility": "private",
                "default_branch": "main", "has_issues": False, "has_wiki": False,
                "has_projects": False, "allow_merge_commit": True,
                "allow_squash_merge": True, "allow_rebase_merge": True,
                "delete_branch_on_merge": False, "allow_update_branch": False,
            }
        if path == base + "/code-scanning/default-setup":
            raise interactive.ApiError("GET " + path + ": HTTP 403: code scanning is unavailable")
        if path.endswith("/rulesets?includes_parents=false&per_page=100&page=1"):
            return 200, []
        return 404, None


class ScienceClientExerciseTests(unittest.TestCase):
    def test_interactive_inventory_survives_optional_codeql_403(self):
        api = OptionalReadApi()
        output = io.StringIO()
        errors = io.StringIO()
        session = interactive.Session(
            repository="acme/science-sandbox", api=api,
            in_stream=io.StringIO("2\n"), out_stream=output, err_stream=errors,
        )
        with patch.object(cli, "init_interactive_session", return_value=session), \
                patch.object(interactive, "prompt_features_stage", return_value=None), \
                patch.object(interactive, "prompt_merge_stage", return_value=None), \
                patch.object(interactive, "prompt_actions_security_stage", return_value=[]), \
                patch.object(interactive, "prompt_content_stages", return_value=[]), \
                patch.object(interactive, "prompt_workflows_rulesets_stage", return_value=[]):
            exit_code = cli.main([])
        self.assertEqual(exit_code, 0, output.getvalue() + errors.getvalue())
        self.assertEqual(session.inventory["codeql"]["status"], "inaccessible")
        self.assertEqual(api.writes, [])
        self.assertEqual(errors.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
