"""Tests for the sciflow-cicd CLI skeleton and help interface."""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.cli import build_parser, main, run_interactive

SCIFLOW_CICD = ROOT_DIR / "sciflow-cicd"


class CliSkeletonSubprocessTests(unittest.TestCase):
    def test_executable_permissions_and_shebang(self):
        """Verify sciflow-cicd exists, is executable, and specifies python3 shebang."""
        self.assertTrue(SCIFLOW_CICD.exists(), "sciflow-cicd executable must exist at root")
        self.assertTrue(os.access(SCIFLOW_CICD, os.X_OK), "sciflow-cicd must have executable permissions")

        with open(SCIFLOW_CICD, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
        self.assertTrue(
            first_line.startswith("#!") and "python3" in first_line,
            f"sciflow-cicd shebang must specify python3, got: {first_line}",
        )

    def test_help_flag_displays_usage_description_and_commands(self):
        """Verify sciflow-cicd --help outputs description, usage, and available commands (including apply)."""
        proc = subprocess.run(
            [str(SCIFLOW_CICD), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, f"sciflow-cicd --help failed: {proc.stderr}")
        stdout = proc.stdout.lower()

        self.assertIn("usage:", stdout, "Help output must include usage")
        self.assertIn("github repository configuration", stdout, "Help output must include description")
        self.assertIn("available commands", stdout, "Help output must include available commands section")
        self.assertIn("apply", stdout, "Help output must list available apply command")

    def test_apply_policy_invocation(self):
        """Verify sciflow-cicd apply --policy <path> invokes headless apply runner."""
        proc = subprocess.run(
            [str(SCIFLOW_CICD), "apply", "--policy", "sample-policy.json"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("error:", proc.stderr.lower())
        self.assertIn("github_token", proc.stderr.lower())

    def test_zero_arguments_routes_to_interactive_entrypoint(self):
        """Verify sciflow-cicd with no arguments routes to interactive entrypoint."""
        proc = subprocess.run(
            [str(SCIFLOW_CICD)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0, "Interactive session should exit non-zero when GITHUB_TOKEN is missing")
        self.assertIn(
            "interactive",
            proc.stdout.lower(),
            f"Expected interactive entrypoint message, got: {proc.stdout}",
        )


    def test_invalid_command_produces_error_and_exits_nonzero(self):
        """Verify invalid commands output descriptive error on stderr and exit non-zero."""
        proc = subprocess.run(
            [str(SCIFLOW_CICD), "nonexistent-command"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0, "Invalid command should exit non-zero")
        self.assertIn("error", proc.stderr.lower(), "stderr should contain descriptive error")

    def test_invalid_flag_produces_error_and_exits_nonzero(self):
        """Verify invalid flags output descriptive error on stderr and exit non-zero."""
        proc = subprocess.run(
            [str(SCIFLOW_CICD), "--unknown-flag"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0, "Invalid flag should exit non-zero")
        self.assertIn("error", proc.stderr.lower(), "stderr should contain descriptive error")

    def test_apply_missing_policy_flag_produces_error_and_exits_nonzero(self):
        """Verify sciflow-cicd apply without --policy produces error and exits non-zero."""
        proc = subprocess.run(
            [str(SCIFLOW_CICD), "apply"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0, "apply without --policy should exit non-zero")
        self.assertIn("error", proc.stderr.lower(), "stderr should contain descriptive error")
        self.assertIn("--policy", proc.stderr, "stderr should mention missing --policy argument")

    def test_symlinked_executable_executes_successfully(self):
        """Verify that sciflow-cicd executes correctly when invoked through a symlink."""
        with tempfile.TemporaryDirectory() as tmpdir:
            symlink_path = Path(tmpdir) / "sciflow-cicd-link"
            os.symlink(SCIFLOW_CICD, symlink_path)
            proc = subprocess.run(
                [str(symlink_path), "--help"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, f"Symlink execution failed: {proc.stderr}")
            self.assertIn("usage:", proc.stdout.lower())


class CliModuleUnitTests(unittest.TestCase):
    def test_build_parser_options(self):
        """Verify parser is configured with expected commands and arguments."""
        parser = build_parser()
        self.assertEqual(parser.prog, "sciflow-cicd")

        # Parsing empty args shouldn't be handled by parser directly (handled by main)
        # But parsing valid apply command should work
        parsed = parser.parse_args(["apply", "--policy", "my-policy.json"])
        self.assertEqual(parsed.command, "apply")
        self.assertEqual(parsed.policy, "my-policy.json")
        self.assertIsNone(parsed.repo)

        # Parsing apply with optional --repo should work
        parsed_with_repo = parser.parse_args(["apply", "--policy", "my-policy.json", "--repo", "octocat/hello"])
        self.assertEqual(parsed_with_repo.command, "apply")
        self.assertEqual(parsed_with_repo.policy, "my-policy.json")
        self.assertEqual(parsed_with_repo.repo, "octocat/hello")

        # Parsing ocr preflight-app should work
        parsed_ocr = parser.parse_args(
            [
                "ocr",
                "preflight-app",
                "--app-id",
                "12345",
                "--private-key-path",
                "/tmp/ocr.pem",
                "--allowlist",
                "acme/repo-a,acme/repo-b",
                "--admin-app-id",
                "999",
                "--json",
            ]
        )
        self.assertEqual(parsed_ocr.command, "ocr")
        self.assertEqual(parsed_ocr.ocr_command, "preflight-app")
        self.assertEqual(parsed_ocr.app_id, "12345")
        self.assertEqual(parsed_ocr.private_key_path, "/tmp/ocr.pem")
        self.assertEqual(parsed_ocr.allowlist, ["acme/repo-a,acme/repo-b"])
        self.assertEqual(parsed_ocr.admin_app_id, ["999"])
        self.assertTrue(parsed_ocr.json)

        # Parsing ocr poll should work
        parsed_poll = parser.parse_args(
            [
                "ocr",
                "poll",
                "--app-id",
                "12345",
                "--private-key-path",
                "/tmp/ocr.pem",
                "--allowlist",
                "acme/repo-a,acme/repo-b",
                "--eligibility-mode",
                "label",
                "--eligible-label",
                "eligible for review",
                "--interval",
                "30",
                "--max-cycles",
                "2",
                "--json",
            ]
        )
        self.assertEqual(parsed_poll.command, "ocr")
        self.assertEqual(parsed_poll.ocr_command, "poll")
        self.assertEqual(parsed_poll.app_id, "12345")
        self.assertEqual(parsed_poll.eligibility_mode, "label")
        self.assertEqual(parsed_poll.max_cycles, 2)
        self.assertTrue(parsed_poll.json)

        # Parsing ocr preflight should work
        parsed_preflight = parser.parse_args(
            [
                "ocr",
                "preflight",
                "--app-id",
                "12345",
                "--private-key-path",
                "/tmp/ocr.pem",
                "--allowlist",
                "acme/repo-a,acme/repo-b",
                "--ocr-bin",
                "/usr/local/bin/ocr",
                "--eligibility-mode",
                "either",
                "--inspect-prs",
                "--json",
            ]
        )
        self.assertEqual(parsed_preflight.command, "ocr")
        self.assertEqual(parsed_preflight.ocr_command, "preflight")
        self.assertEqual(parsed_preflight.app_id, "12345")
        self.assertEqual(parsed_preflight.private_key_path, "/tmp/ocr.pem")
        self.assertEqual(parsed_preflight.allowlist, ["acme/repo-a,acme/repo-b"])
        self.assertEqual(parsed_preflight.ocr_bin, "/usr/local/bin/ocr")
        self.assertTrue(parsed_preflight.inspect_prs)
        self.assertTrue(parsed_preflight.json)

        # Parsing ocr check-safety should work
        parsed_safety = parser.parse_args(
            [
                "ocr",
                "check-safety",
                "--app-id",
                "12345",
                "--private-key-path",
                "/tmp/ocr.pem",
                "--allowlist",
                "acme/repo-a,acme/repo-b",
                "--execution-mode",
                "local_direct",
                "--json",
            ]
        )
        self.assertEqual(parsed_safety.command, "ocr")
        self.assertEqual(parsed_safety.ocr_command, "check-safety")
        self.assertEqual(parsed_safety.app_id, "12345")
        self.assertEqual(parsed_safety.private_key_path, "/tmp/ocr.pem")
        self.assertEqual(parsed_safety.allowlist, ["acme/repo-a,acme/repo-b"])
        self.assertEqual(parsed_safety.execution_mode, "local_direct")
        self.assertTrue(parsed_safety.json)

        # Parsing ocr run should work with all advisory review options
        parsed_run = parser.parse_args(
            [
                "ocr",
                "run",
                "--app-id",
                "12345",
                "--private-key-path",
                "/tmp/ocr.pem",
                "--allowlist",
                "acme/repo-a,acme/repo-b",
                "--admin-app-id",
                "999",
                "--api-base-url",
                "http://127.0.0.1:8080",
                "--state-path",
                "/tmp/ocr-state.json",
                "--lock-path",
                "/tmp/ocr.lock",
                "--ocr-bin",
                "/usr/local/bin/ocr",
                "--eligibility-mode",
                "maintainer",
                "--eligible-label",
                "review-me",
                "--maintainer",
                "alice,bob",
                "--include-drafts",
                "--review-mode",
                "issue_comment",
                "--max-reviews",
                "3",
                "--interval",
                "15",
                "--max-cycles",
                "4",
                "--json",
            ]
        )
        self.assertEqual(parsed_run.command, "ocr")
        self.assertEqual(parsed_run.ocr_command, "run")
        self.assertEqual(parsed_run.app_id, "12345")
        self.assertEqual(parsed_run.private_key_path, "/tmp/ocr.pem")
        self.assertEqual(parsed_run.allowlist, ["acme/repo-a,acme/repo-b"])
        self.assertEqual(parsed_run.admin_app_id, ["999"])
        self.assertEqual(parsed_run.api_base_url, "http://127.0.0.1:8080")
        self.assertEqual(parsed_run.state_path, "/tmp/ocr-state.json")
        self.assertEqual(parsed_run.lock_path, "/tmp/ocr.lock")
        self.assertEqual(parsed_run.ocr_bin, "/usr/local/bin/ocr")
        self.assertEqual(parsed_run.eligibility_mode, "maintainer")
        self.assertEqual(parsed_run.eligible_label, "review-me")
        self.assertEqual(parsed_run.maintainer, ["alice,bob"])
        self.assertTrue(parsed_run.include_drafts)
        self.assertEqual(parsed_run.review_mode, "issue_comment")
        self.assertEqual(parsed_run.max_reviews, 3)
        self.assertEqual(parsed_run.interval, 15.0)
        self.assertEqual(parsed_run.max_cycles, 4)
        self.assertTrue(parsed_run.json)

        # Default interval and max_cycles for ocr run preserve single-pass default
        parsed_run_defaults = parser.parse_args(
            [
                "ocr",
                "run",
                "--app-id",
                "12345",
                "--private-key-path",
                "/tmp/ocr.pem",
                "--allowlist",
                "acme/repo-a",
            ]
        )
        self.assertEqual(parsed_run_defaults.interval, 60.0)
        self.assertEqual(parsed_run_defaults.max_cycles, 1)

        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "ocr",
                    "run",
                    "--app-id",
                    "12345",
                    "--private-key-path",
                    "/tmp/ocr.pem",
                    "--allowlist",
                    "acme/repo-a",
                    "--allow-competing-workflow",
                ]
            )

    def test_main_routing(self):
        """Verify main() routing behavior with explicit argv."""
        from unittest.mock import MagicMock, patch

        mock_session = MagicMock()
        # Empty argv routes to interactive
        saved_stdout = sys.stdout
        try:
            sys.stdout = io.StringIO()
            with patch("scripts.cli.init_interactive_session", return_value=mock_session):
                ret_interactive = main([])
            self.assertEqual(ret_interactive, 0)
            self.assertIn("interactive", sys.stdout.getvalue().lower())

            # Apply command routes to headless apply
            with patch("scripts.cli.run_headless_apply", return_value=0) as mock_apply:
                ret_apply = main(["apply", "--policy", "test.json", "--repo", "acme/project"])
                self.assertEqual(ret_apply, 0)
                mock_apply.assert_called_once_with(policy_path="test.json", repo="acme/project", plan_path=None)

            # OCR preflight-app command routes to run_app_preflight
            with patch("scripts.ocr_app_auth.run_app_preflight", return_value=0) as mock_ocr:
                ret_ocr = main(
                    [
                        "ocr",
                        "preflight-app",
                        "--app-id",
                        "555",
                        "--private-key-path",
                        "/tmp/key.pem",
                        "--allowlist",
                        "acme/repo-a",
                        "--json",
                    ]
                )
                self.assertEqual(ret_ocr, 0)
                mock_ocr.assert_called_once()

            # OCR poll command routes to run_ocr_poll
            with patch("scripts.ocr_poller.run_ocr_poll", return_value=0) as mock_poll:
                ret_poll = main(
                    [
                        "ocr",
                        "poll",
                        "--app-id",
                        "555",
                        "--private-key-path",
                        "/tmp/key.pem",
                        "--allowlist",
                        "acme/repo-a",
                        "--json",
                    ]
                )
                self.assertEqual(ret_poll, 0)
                mock_poll.assert_called_once()

            # OCR preflight command routes to run_ocr_preflight
            with patch("scripts.ocr_preflight.run_ocr_preflight", return_value=0) as mock_preflight:
                ret_preflight = main(
                    [
                        "ocr",
                        "preflight",
                        "--app-id",
                        "555",
                        "--private-key-path",
                        "/tmp/key.pem",
                        "--allowlist",
                        "acme/repo-a",
                        "--ocr-bin",
                        "ocr",
                        "--json",
                    ]
                )
                self.assertEqual(ret_preflight, 0)
                mock_preflight.assert_called_once()

            # OCR check-safety command routes to run_ocr_safety_check
            with patch("scripts.ocr_safety.run_ocr_safety_check", return_value=0) as mock_safety:
                ret_safety = main(
                    [
                        "ocr",
                        "check-safety",
                        "--app-id",
                        "555",
                        "--private-key-path",
                        "/tmp/key.pem",
                        "--allowlist",
                        "acme/repo-a",
                        "--json",
                    ]
                )
                self.assertEqual(ret_safety, 0)
                mock_safety.assert_called_once()

            # OCR run command routes to run_ocr_reviews
            with patch("scripts.ocr_reviewer.run_ocr_reviews", return_value=0) as mock_run:
                ret_run = main(
                    [
                        "ocr",
                        "run",
                        "--app-id",
                        "555",
                        "--private-key-path",
                        "/tmp/key.pem",
                        "--allowlist",
                        "acme/repo-a",
                        "--state-path",
                        "/tmp/state.json",
                        "--lock-path",
                        "/tmp/ocr.lock",
                        "--review-mode",
                        "pr_review",
                        "--max-reviews",
                        "2",
                        "--interval",
                        "15",
                        "--max-cycles",
                        "2",
                        "--json",
                    ]
                )
                self.assertEqual(ret_run, 0)
                mock_run.assert_called_once()
                self.assertEqual(mock_run.call_args.kwargs["interval_seconds"], 15.0)
                self.assertEqual(mock_run.call_args.kwargs["max_cycles"], 2)
        finally:
            sys.stdout = saved_stdout

    def test_run_interactive_success(self):
        """Verify interactive entrypoint executes and returns 0 when session initializes."""
        from unittest.mock import MagicMock, patch

        mock_session = MagicMock()
        saved_stdout = sys.stdout
        try:
            sys.stdout = io.StringIO()
            with patch("scripts.cli.init_interactive_session", return_value=mock_session):
                self.assertEqual(run_interactive(), 0)
            self.assertIn("interactive", sys.stdout.getvalue().lower())
        finally:
            sys.stdout = saved_stdout

    def test_run_interactive_failure(self):
        """Verify interactive entrypoint returns 1 when session initialization fails."""
        from unittest.mock import patch

        saved_stdout = sys.stdout
        try:
            sys.stdout = io.StringIO()
            with patch("scripts.cli.init_interactive_session", return_value=None):
                self.assertEqual(run_interactive(), 1)
        finally:
            sys.stdout = saved_stdout

    def test_ocr_subcommands_use_dispatch_table(self):
        """Verify OCR subcommands are routed through an explicit dispatch mapping."""
        import scripts.cli as cli_mod

        handlers = getattr(cli_mod, "_OCR_COMMAND_HANDLERS", None)
        self.assertIsInstance(handlers, dict)
        self.assertEqual(
            set(handlers.keys()),
            {"preflight-app", "preflight", "poll", "check-safety", "run"},
        )
        for name, fn in handlers.items():
            self.assertTrue(callable(fn), f"Handler for {name} must be callable")


if __name__ == "__main__":
    unittest.main()
