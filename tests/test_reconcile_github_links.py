"""Behavior tests for the optional managed-links command."""

import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from test_setup_repository_content import ContentApi


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_github_links.py"


def load_command():
    spec = importlib.util.spec_from_file_location("reconcile_github_links", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LinksCommandTests(unittest.TestCase):
    def test_dry_run_apply_and_repeat_apply(self):
        command = load_command()
        api = ContentApi()
        api.comments.append({"id": 1, "body": "Unrelated comment", "user": {"login": "other"}})
        entries = [{"issue": 7, "key": "planning", "links": [
            {"label": "Spec", "url": "https://github.com/acme/project/wiki/Spec-7"}]}]
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "links.json"
            source.write_text(json.dumps(entries), encoding="utf-8")
            with patch.object(command, "GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "test-only"}):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(command.main(["--repo", "acme/project", "--file", str(source)]), 0)
                plan = json.loads(output.getvalue())
                self.assertEqual(plan["mode"], "dry-run")
                self.assertEqual(len(plan["changes"]), 1)
                self.assertEqual(plan["changes"][0]["status"], "planned")
                self.assertEqual(api.writes, [])
                self.assertNotIn("Spec-7", output.getvalue())

                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(command.main([
                        "--repo", "acme/project", "--file", str(source), "--apply"]), 0)
                self.assertEqual(json.loads(output.getvalue())["changes"][0]["status"], "verified")
                self.assertEqual(api.comments[0]["body"], "Unrelated comment")
                self.assertEqual(len(api.comments), 2)

                api.writes.clear()
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(command.main([
                        "--repo", "acme/project", "--file", str(source), "--apply"]), 0)
                self.assertEqual(json.loads(output.getvalue())["changes"], [])
                self.assertEqual(api.writes, [])

    def test_duplicate_owned_comments_block_apply(self):
        command = load_command()
        api = ContentApi()
        marker = "<!-- github-repo-setup:planning -->"
        api.comments.extend([
            {"id": 1, "body": marker + "\nOld", "user": {"login": "operator"}},
            {"id": 2, "body": marker + "\nOld", "user": {"login": "operator"}},
        ])
        entries = [{"issue": 7, "key": "planning", "links": [
            {"label": "Spec", "url": "https://github.com/acme/project/wiki/Spec-7"}]}]
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "links.json"
            source.write_text(json.dumps(entries), encoding="utf-8")
            with patch.object(command, "GitHubApi", return_value=api), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "test-only"}), \
                 redirect_stderr(io.StringIO()):
                self.assertEqual(command.main([
                    "--repo", "acme/project", "--file", str(source), "--apply"]), 1)
        self.assertEqual(api.writes, [])


if __name__ == "__main__":
    unittest.main()
