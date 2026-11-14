"""Tests for interactive session initialization and repository validation."""

import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.interactive import Session, is_valid_repository, init_interactive_session


class RepositoryValidationTests(unittest.TestCase):
    """Tests for repository naming pattern validation."""

    def test_valid_repository_names(self):
        """Valid repository names in OWNER/REPO format should pass."""
        valid_cases = [
            "octocat/Hello-World",
            "owner/repo",
            "my-org/my-project",
            "org_name/repo_123",
            "user.name/repo.name",
            "a/b",
            "Team-123/service-api_v2",
        ]
        for repo in valid_cases:
            with self.subTest(repo=repo):
                self.assertTrue(is_valid_repository(repo), f"Expected '{repo}' to be valid")

    def test_invalid_repository_names(self):
        """Invalid repository names should fail validation."""
        invalid_cases = [
            "",
            "   ",
            "noslash",
            "/repo",
            "owner/",
            "owner/repo/extra",
            "owner//repo",
            "owner/repo with spaces",
            "owner@domain/repo",
            "owner/repo!name",
            "owner/repo#1",
        ]
        for repo in invalid_cases:
            with self.subTest(repo=repo):
                self.assertFalse(is_valid_repository(repo), f"Expected '{repo}' to be invalid")


class SessionClassTests(unittest.TestCase):
    """Tests for the Session context object."""

    def test_session_initialization_attributes(self):
        """Session object should expose repository, api, repo_data, and token."""
        mock_api = MagicMock()
        repo_data = {"id": 12345, "default_branch": "main"}
        session = Session(
            repository="test-owner/test-repo",
            api=mock_api,
            repo_data=repo_data,
            token="ghp_test_token",
        )

        self.assertEqual(session.repository, "test-owner/test-repo")
        self.assertIs(session.api, mock_api)
        self.assertEqual(session.repo_data, repo_data)
        self.assertEqual(session.token, "ghp_test_token")

    def test_session_defaults(self):
        """Session object should provide sensible defaults for optional fields."""
        mock_api = MagicMock()
        session = Session(
            repository="owner/repo",
            api=mock_api,
        )
        self.assertEqual(session.repository, "owner/repo")
        self.assertIs(session.api, mock_api)
        self.assertEqual(session.repo_data, {})
        self.assertIsNone(session.token)


class TokenValidationTests(unittest.TestCase):
    """Tests for GitHub token environment variable validation."""

    def test_missing_github_token_prints_error_and_returns_none(self):
        """When GITHUB_TOKEN is not set, init_interactive_session outputs clear error and returns None."""
        err_stream = io.StringIO()
        out_stream = io.StringIO()

        with patch.dict(os.environ, {}, clear=True):
            session = init_interactive_session(
                token=None,
                err_stream=err_stream,
                out_stream=out_stream,
            )

        self.assertIsNone(session)
        combined_output = err_stream.getvalue() + out_stream.getvalue()
        self.assertIn("GITHUB_TOKEN", combined_output)
        self.assertIn("error", combined_output.lower())

    def test_empty_github_token_prints_error_and_returns_none(self):
        """When GITHUB_TOKEN is empty or whitespace only, produces clear error and returns None."""
        err_stream = io.StringIO()
        out_stream = io.StringIO()

        with patch.dict(os.environ, {"GITHUB_TOKEN": "   "}):
            session = init_interactive_session(
                token=None,
                err_stream=err_stream,
                out_stream=out_stream,
            )

        self.assertIsNone(session)
        combined_output = err_stream.getvalue() + out_stream.getvalue()
        self.assertIn("GITHUB_TOKEN", combined_output)
        self.assertIn("error", combined_output.lower())


class RepositoryPromptingTests(unittest.TestCase):
    """Tests for prompting and reprompting target repository."""

    def test_prompt_repository_valid_first_attempt(self):
        """Prompting accepts valid OWNER/REPO on first try."""
        in_stream = io.StringIO("octocat/Hello-World\n")
        out_stream = io.StringIO()

        from scripts.interactive import prompt_target_repository
        repo = prompt_target_repository(in_stream=in_stream, out_stream=out_stream)

        self.assertEqual(repo, "octocat/Hello-World")
        self.assertIn("repository", out_stream.getvalue().lower())

    def test_prompt_repository_reprompts_on_invalid_format(self):
        """Prompting reprompts when user enters invalid format strings until valid format is entered."""
        in_stream = io.StringIO("invalid-name\nowner/\noctocat/Hello-World\n")
        out_stream = io.StringIO()

        from scripts.interactive import prompt_target_repository
        repo = prompt_target_repository(in_stream=in_stream, out_stream=out_stream)

        self.assertEqual(repo, "octocat/Hello-World")
        output = out_stream.getvalue()
        self.assertIn("invalid", output.lower())
        self.assertIn("owner/repo", output.lower())

    def test_prompt_repository_eof_returns_none(self):
        """Immediate EOF returns None without crashing."""
        in_stream = io.StringIO("")
        out_stream = io.StringIO()

        from scripts.interactive import prompt_target_repository
        repo = prompt_target_repository(in_stream=in_stream, out_stream=out_stream)

        self.assertIsNone(repo)

    def test_prompt_repository_eof_after_invalid_attempt_returns_none(self):
        """EOF after invalid attempt returns None cleanly."""
        in_stream = io.StringIO("invalid-repo\n")
        out_stream = io.StringIO()

        from scripts.interactive import prompt_target_repository
        repo = prompt_target_repository(in_stream=in_stream, out_stream=out_stream)

        self.assertIsNone(repo)
        self.assertIn("invalid", out_stream.getvalue().lower())


class RepositoryVerificationTests(unittest.TestCase):
    """Tests for API verification of repository existence and accessibility."""

    def test_verify_repository_success_200(self):
        """API returning 200 OK returns repository data dictionary."""
        from scripts.interactive import verify_repository

        mock_api = MagicMock()
        expected_data = {"id": 100, "name": "project", "full_name": "acme/project", "default_branch": "main"}
        mock_api.request.return_value = (200, expected_data)

        out_stream = io.StringIO()
        result = verify_repository(mock_api, "acme/project", out_stream=out_stream)

        self.assertEqual(result, expected_data)
        mock_api.request.assert_called_once_with("GET", "/repos/acme/project")
        self.assertIn("verified", out_stream.getvalue().lower())

    def test_verify_repository_not_found_404(self):
        """API returning 404 prints informative error and returns None."""
        from scripts.interactive import verify_repository

        mock_api = MagicMock()
        mock_api.request.return_value = (404, None)

        err_stream = io.StringIO()
        out_stream = io.StringIO()
        result = verify_repository(
            mock_api,
            "acme/nonexistent",
            out_stream=out_stream,
            err_stream=err_stream,
        )

        self.assertIsNone(result)
        mock_api.request.assert_called_once_with("GET", "/repos/acme/nonexistent")
        output = err_stream.getvalue() + out_stream.getvalue()
        self.assertIn("404", output)
        self.assertIn("acme/nonexistent", output)
        self.assertIn("not found", output.lower())

    def test_verify_repository_api_error_returns_none(self):
        """API raising ApiError prints informative error and returns None."""
        from scripts.configure_github_repo import ApiError
        from scripts.interactive import verify_repository

        mock_api = MagicMock()
        mock_api.request.side_effect = ApiError("GET /repos/acme/project: HTTP 401: Bad credentials")

        err_stream = io.StringIO()
        out_stream = io.StringIO()
        result = verify_repository(
            mock_api,
            "acme/project",
            out_stream=out_stream,
            err_stream=err_stream,
        )

        self.assertIsNone(result)
        output = err_stream.getvalue() + out_stream.getvalue()
        self.assertIn("bad credentials", output.lower())
        self.assertIn("error", output.lower())


class InitInteractiveSessionTests(unittest.TestCase):
    """End-to-end tests for the init_interactive_session orchestrator."""

    def test_init_session_success(self):
        """Successful initialization returns Session ready for next stages."""
        in_stream = io.StringIO("acme/project\n")
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        mock_api = MagicMock()
        repo_data = {"id": 42, "name": "project", "default_branch": "main"}
        mock_api.request.return_value = (200, repo_data)

        session = init_interactive_session(
            token="ghp_test123",
            api=mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            err_stream=err_stream,
        )

        self.assertIsNotNone(session)
        self.assertIsInstance(session, Session)
        self.assertEqual(session.repository, "acme/project")
        self.assertIs(session.api, mock_api)
        self.assertEqual(session.repo_data, repo_data)
        self.assertEqual(session.token, "ghp_test123")
        self.assertIn("verified", out_stream.getvalue().lower())

    def test_init_session_reprompts_on_invalid_format_then_succeeds(self):
        """Invalid format strings reprompt until valid repository is provided."""
        in_stream = io.StringIO("invalid_format\nacme/valid-repo\n")
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        mock_api = MagicMock()
        mock_api.request.return_value = (200, {"id": 10})

        session = init_interactive_session(
            token="ghp_test123",
            api=mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            err_stream=err_stream,
        )

        self.assertIsNotNone(session)
        self.assertEqual(session.repository, "acme/valid-repo")
        self.assertIn("invalid", out_stream.getvalue().lower())

    def test_init_session_reprompts_on_404_then_succeeds(self):
        """Repository returning 404 displays error and reprompts for repository."""
        in_stream = io.StringIO("acme/nonexistent\nacme/real-repo\n")
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        mock_api = MagicMock()
        mock_api.request.side_effect = [
            (404, None),
            (200, {"id": 20, "name": "real-repo"}),
        ]

        session = init_interactive_session(
            token="ghp_test123",
            api=mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            err_stream=err_stream,
        )

        self.assertIsNotNone(session)
        self.assertEqual(session.repository, "acme/real-repo")
        combined = err_stream.getvalue() + out_stream.getvalue()
        self.assertIn("404", combined)

    def test_init_session_404_eof_returns_none(self):
        """When 404 occurs and user sends EOF, returns None."""
        in_stream = io.StringIO("acme/nonexistent\n")
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        mock_api = MagicMock()
        mock_api.request.return_value = (404, None)

        session = init_interactive_session(
            token="ghp_test123",
            api=mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            err_stream=err_stream,
        )

        self.assertIsNone(session)
        combined = err_stream.getvalue() + out_stream.getvalue()
        self.assertIn("404", combined)

    def test_init_session_api_error_returns_none(self):
        """When ApiError occurs during verification, returns None."""
        from scripts.configure_github_repo import ApiError

        in_stream = io.StringIO("acme/forbidden-repo\n")
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        mock_api = MagicMock()
        mock_api.request.side_effect = ApiError("GET /repos/acme/forbidden-repo: HTTP 403: Resource not accessible")

        session = init_interactive_session(
            token="ghp_test123",
            api=mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            err_stream=err_stream,
        )

        self.assertIsNone(session)
        combined = err_stream.getvalue() + out_stream.getvalue()
        self.assertIn("403", combined)

    def test_init_session_eof_on_prompt_returns_none(self):
        """Immediate EOF on repository prompt returns None."""
        in_stream = io.StringIO("")
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        mock_api = MagicMock()

        session = init_interactive_session(
            token="ghp_test123",
            api=mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            err_stream=err_stream,
        )

        self.assertIsNone(session)


class CliInteractiveWiringTests(unittest.TestCase):
    """Tests for cli.py wiring to interactive initialization."""

    def test_run_interactive_returns_zero_on_success(self):
        """run_interactive returns 0 when session initialization succeeds."""
        from scripts.cli import run_interactive

        mock_session = MagicMock(spec=Session)
        with patch("scripts.cli.init_interactive_session", return_value=mock_session) as mock_init:
            out_stream = io.StringIO()
            code = run_interactive(out_stream=out_stream)
            self.assertEqual(code, 0)
            mock_init.assert_called_once()
            self.assertIn("interactive", out_stream.getvalue().lower())

    def test_run_interactive_returns_one_on_failure(self):
        """run_interactive returns 1 when session initialization fails."""
        from scripts.cli import run_interactive

        with patch("scripts.cli.init_interactive_session", return_value=None) as mock_init:
            out_stream = io.StringIO()
            code = run_interactive(out_stream=out_stream)
            self.assertEqual(code, 1)
            mock_init.assert_called_once()

    def test_main_with_no_args_invokes_run_interactive(self):
        """main([]) routes to run_interactive."""
        from scripts.cli import main

        with patch("scripts.cli.run_interactive", return_value=0) as mock_run:
            code = main([])
            self.assertEqual(code, 0)
            mock_run.assert_called_once()

    def test_main_with_setup_command_invokes_run_interactive(self):
        """main(['setup']) routes to run_interactive."""
        from scripts.cli import main

        with patch("scripts.cli.run_interactive", return_value=0) as mock_run:
            code = main(["setup"])
            self.assertEqual(code, 0)
            mock_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
