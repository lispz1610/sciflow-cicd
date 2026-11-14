"""Tests for interactive repository features stage questionnaire."""

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.interactive import Session
from scripts.interactive_features import prompt_features_stage
from scripts.setup_github_repo import validate_policy


class TestInteractiveFeaturesStage(unittest.TestCase):
    """Test suite for repository features questionnaire stage."""

    def setUp(self):
        self.mock_api = MagicMock()

    def test_fresh_mode_no_features_preselected(self):
        """In fresh mode, no features should be preselected even if inventory/defaults exist."""
        in_stream = io.StringIO("\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
            inventory={"features": {"issues": True, "wiki": True, "projects": True}},
            defaults={"features": ["issues", "wiki", "projects"]},
        )

        result = prompt_features_stage(session)
        self.assertIsNone(result)

        output = out_stream.getvalue()
        # Verify no checkmarks in options
        self.assertIn("[ ] 1. Issues", output)
        self.assertIn("[ ] 2. Wiki", output)
        self.assertIn("[ ] 3. Projects", output)
        self.assertNotIn("[x]", output)

    def test_fresh_mode_user_selects_features(self):
        """In fresh mode, user can explicitly select features."""
        in_stream = io.StringIO("1, 3\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
        )

        result = prompt_features_stage(session)
        self.assertEqual(result, {"name": "features", "enable": ["issues", "projects"]})

    def test_resume_mode_preselects_from_defaults(self):
        """In resume mode, active features in session.defaults are preselected."""
        in_stream = io.StringIO("\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=True,
            defaults={"features": ["issues", "wiki"]},
        )

        result = prompt_features_stage(session)
        self.assertEqual(result, {"name": "features", "enable": ["issues", "wiki"]})

        output = out_stream.getvalue()
        self.assertIn("[x] 1. Issues", output)
        self.assertIn("[x] 2. Wiki", output)
        self.assertIn("[ ] 3. Projects", output)

    def test_resume_mode_preselects_from_inventory_if_defaults_missing(self):
        """In resume mode, falls back to inventory if session.defaults is empty."""
        in_stream = io.StringIO("\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=True,
            inventory={"features": {"issues": False, "wiki": True, "projects": True}},
            defaults={},
        )

        result = prompt_features_stage(session)
        self.assertEqual(result, {"name": "features", "enable": ["wiki", "projects"]})

        output = out_stream.getvalue()
        self.assertIn("[ ] 1. Issues", output)
        self.assertIn("[x] 2. Wiki", output)
        self.assertIn("[x] 3. Projects", output)

    def test_resume_mode_user_overrides_selection(self):
        """In resume mode, user can override the preselected defaults."""
        in_stream = io.StringIO("1\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=True,
            defaults={"features": ["wiki", "projects"]},
        )

        result = prompt_features_stage(session)
        self.assertEqual(result, {"name": "features", "enable": ["issues"]})

    def test_no_features_selected_explicit_none_returns_none(self):
        """Selecting 'none' or '0' returns None (no stage to configure)."""
        in_stream = io.StringIO("none\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=True,
            defaults={"features": ["issues"]},
        )

        result = prompt_features_stage(session)
        self.assertIsNone(result)

    def test_prompt_cancellation_or_eof_returns_none(self):
        """EOF on input returns None."""
        in_stream = io.StringIO("")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
        )

        result = prompt_features_stage(session)
        self.assertIsNone(result)

    def test_all_features_selected(self):
        """Selecting all available features constructs full enable list."""
        in_stream = io.StringIO("1, 2, 3\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
        )

        result = prompt_features_stage(session)
        self.assertEqual(result, {"name": "features", "enable": ["issues", "wiki", "projects"]})

    def test_payload_validates_against_policy_schema(self):
        """The returned stage payload must be strictly accepted by validate_policy."""
        in_stream = io.StringIO("1, 2\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
        )

        result = prompt_features_stage(session)
        self.assertIsNotNone(result)

        policy = {
            "version": 1,
            "stages": [result],
        }
        validated_stages = validate_policy(policy)
        self.assertEqual(len(validated_stages), 1)
        self.assertEqual(validated_stages[0], result)

    def test_stream_overrides(self):
        """Explicit streams passed to prompt_features_stage take precedence over session streams."""
        session_in = io.StringIO("should not be read\n")
        session_out = io.StringIO()
        override_in = io.StringIO("2\n")
        override_out = io.StringIO()

        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=session_in,
            out_stream=session_out,
            resume_mode=False,
        )

        result = prompt_features_stage(session, in_stream=override_in, out_stream=override_out)
        self.assertEqual(result, {"name": "features", "enable": ["wiki"]})
        self.assertIn("Wiki", override_out.getvalue())
        self.assertEqual(session_out.getvalue(), "")

    def test_reexport_from_scripts_interactive(self):
        """prompt_features_stage should also be exportable from scripts.interactive."""
        from scripts.interactive import prompt_features_stage as prompt_from_interactive
        self.assertIs(prompt_from_interactive, prompt_features_stage)

    def test_call_without_session(self):
        """Calling prompt_features_stage with session=None should work using stream overrides."""
        in_stream = io.StringIO("1\n")
        out_stream = io.StringIO()
        result = prompt_features_stage(None, in_stream=in_stream, out_stream=out_stream)
        self.assertEqual(result, {"name": "features", "enable": ["issues"]})

    def test_reprompts_on_invalid_input(self):
        """Prompts again when input contains invalid option number."""
        in_stream = io.StringIO("5\n1, 2\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
        )

        result = prompt_features_stage(session)
        self.assertEqual(result, {"name": "features", "enable": ["issues", "wiki"]})
        self.assertIn("Invalid selection", out_stream.getvalue())

    def test_resume_mode_with_no_active_features_in_defaults_or_inventory(self):
        """In resume mode with empty defaults and inventory, no options are preselected."""
        in_stream = io.StringIO("\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=True,
            defaults={"features": []},
            inventory={"features": {"issues": False, "wiki": False, "projects": False}},
        )

        result = prompt_features_stage(session)
        self.assertIsNone(result)
        output = out_stream.getvalue()
        self.assertNotIn("[x]", output)


if __name__ == "__main__":
    unittest.main()
