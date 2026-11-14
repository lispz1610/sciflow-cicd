"""Integration tests for publishing authored pages into a GitHub Wiki Git repo."""

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish_github_wiki.py"
spec = importlib.util.spec_from_file_location("publish_github_wiki", SCRIPT)
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class WikiPublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.remote = root / "wiki.git"
        git("init", "--bare", "-b", "master", str(self.remote))
        seed = root / "seed"
        git("clone", str(self.remote), str(seed))
        (seed / "Home.md").write_text("# Existing home\n")
        (seed / "Unmanaged.md").write_text("Keep this page\n")
        git("add", "Home.md", "Unmanaged.md", cwd=seed)
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "Initialize wiki", cwd=seed)
        git("push", "origin", "HEAD", cwd=seed)
        self.source = root / "authored"
        self.source.mkdir()
        (self.source / "spec.md").write_text("# Authored spec\n")

    def test_dry_run_does_not_create_a_revision(self):
        before = git("--git-dir", str(self.remote), "rev-parse", "HEAD")
        result = publisher.publish_wiki("acme/project", self.source, [("spec.md", "Spec-42")], str(self.remote))
        after = git("--git-dir", str(self.remote), "rev-parse", "HEAD")
        self.assertEqual(before, after)
        self.assertEqual(result["changes"], ["Spec-42.md"])
        self.assertIsNone(result["links"]["Spec-42.md"]["revision"])

    def test_direct_cli_dry_run_apply_and_repeat(self):
        argv = ["--repo", "acme/project", "--source-dir", str(self.source),
                "--page", "spec.md=Spec-42", "--wiki-url", str(self.remote)]
        before = git("--git-dir", str(self.remote), "rev-parse", "HEAD")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(publisher.main(argv), 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "dry-run")
        self.assertEqual(git("--git-dir", str(self.remote), "rev-parse", "HEAD"), before)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(publisher.main(argv + ["--apply"]), 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "apply")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(publisher.main(argv + ["--apply"]), 0)
        self.assertEqual(json.loads(output.getvalue())["changes"], [])

    def test_cli_authenticates_default_https_remote_without_exposing_token(self):
        token = "wiki-test-token-do-not-log"
        args_seen = []
        env_seen = []

        def fake_run(args, cwd=None, env=None, capture_output=False, text=False, check=False):
            args_seen.append(args)
            env_seen.append(env)
            if args[1] == "clone":
                Path(args[-1]).mkdir()
                (Path(args[-1]) / "Home.md").write_text("Home\n")
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[1:] == ["rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, "a" * 40, "")
            raise AssertionError(args)

        output = io.StringIO()
        argv = ["--repo", "acme/project", "--source-dir", str(self.source), "--page", "spec.md=Spec"]
        with patch.dict(os.environ, {"GITHUB_TOKEN": token}), \
                patch.object(publisher.subprocess, "run", side_effect=fake_run), \
                redirect_stdout(output):
            self.assertEqual(publisher.main(argv), 0)

        self.assertEqual(args_seen[0][-2], "https://github.com/acme/project.wiki.git")
        self.assertTrue(all(token not in " ".join(map(str, args)) for args in args_seen))
        self.assertTrue(all(env["GIT_TERMINAL_PROMPT"] == "0" for env in env_seen))
        self.assertTrue(all(env["GITHUB_TOKEN"] == token for env in env_seen))
        for env in env_seen:
            self.assertEqual(env["GITHUB_TOKEN"], token)
            self.assertTrue(all(token not in str(value) for key, value in env.items()
                                if key != "GITHUB_TOKEN"))
        self.assertIn("$GITHUB_TOKEN", env_seen[0]["GIT_CONFIG_VALUE_1"])
        self.assertNotIn(token, output.getvalue())

    def test_cli_reports_uninitialized_wiki_prerequisite(self):
        empty_remote = Path(self.temp.name) / "empty-cli-wiki.git"
        git("init", "--bare", "-b", "master", str(empty_remote))
        stderr = io.StringIO()
        argv = ["--repo", "acme/project", "--source-dir", str(self.source),
                "--page", "spec.md=Spec", "--wiki-url", str(empty_remote)]
        with redirect_stderr(stderr):
            self.assertEqual(publisher.main(argv), 1)
        self.assertIn("Wiki has no first page", stderr.getvalue())
        self.assertIn("initialize it in GitHub", stderr.getvalue())

    def test_apply_is_repeatable_and_preserves_unmanaged_pages(self):
        first = publisher.publish_wiki("acme/project", self.source, [("spec.md", "Spec-42")], str(self.remote), apply=True)
        second = publisher.publish_wiki("acme/project", self.source, [("spec.md", "Spec-42")], str(self.remote), apply=True)
        self.assertEqual(second["changes"], [])
        self.assertEqual(first["revision"], second["revision"])
        self.assertTrue(first["links"]["Spec-42.md"]["revision"].endswith("/" + first["revision"]))
        self.assertEqual(git("--git-dir", str(self.remote), "show", "HEAD:Unmanaged.md"), "Keep this page")
        self.assertEqual(git("--git-dir", str(self.remote), "show", "HEAD:Spec-42.md"), "# Authored spec")

    def test_rejects_invalid_target_name(self):
        with self.assertRaisesRegex(ValueError, "invalid Wiki page"):
            publisher.publish_wiki("acme/project", self.source, [("spec.md", "../Outside")], str(self.remote), apply=True)

    def test_rejects_wiki_page_symlink(self):
        root = Path(self.temp.name)
        clone = root / "symlink-seed"
        git("clone", str(self.remote), str(clone))
        (clone / "Spec-42.md").symlink_to("Unmanaged.md")
        git("add", "Spec-42.md", cwd=clone)
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "Add symlink", cwd=clone)
        git("push", "origin", "HEAD", cwd=clone)
        with self.assertRaisesRegex(publisher.WikiError, "symlink"):
            publisher.publish_wiki("acme/project", self.source, [("spec.md", "Spec-42")], str(self.remote), apply=True)

    def test_uninitialized_wiki_reports_first_page_prerequisite(self):
        empty_remote = Path(self.temp.name) / "empty-wiki.git"
        git("init", "--bare", "-b", "master", str(empty_remote))
        with self.assertRaisesRegex(publisher.WikiError, "first page"):
            publisher.publish_wiki("acme/project", self.source,
                                   [("spec.md", "Spec-42")], str(empty_remote), apply=True)


if __name__ == "__main__":
    unittest.main()
