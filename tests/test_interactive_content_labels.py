"""Tests for repository content and canonical triage label synchronization."""

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.interactive import Session
from scripts.interactive_repository_content import prompt_content_stages
from scripts.setup_profiles import CANONICAL_TRIAGE_LABELS
from scripts.setup_github_repo import validate_policy


class InteractiveContentLabelsTests(unittest.TestCase):
    """Test suite for canonical triage label synchronization (deprecating manual typing)."""

    def setUp(self):
        self.mock_api = MagicMock()

    def test_selecting_labels_automatically_synchronizes_5_canonical_labels(self):
        """Selecting option 1 (Labels) stages the 5 canonical triage labels without manual typing."""
        # Input: 'y' to configure content, '1' to select Labels
        in_stream = io.StringIO("y\n1\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
        )

        stages = prompt_content_stages(session)
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0]["name"], "labels")

        expected_items = [dict(label) for label in CANONICAL_TRIAGE_LABELS]
        self.assertEqual(stages[0]["items"], expected_items)

        output = out_stream.getvalue()
        self.assertIn("Synchronizing 5 canonical triage labels", output)
        self.assertIn("needs-triage", output)
        self.assertIn("bug", output)
        self.assertIn("enhancement", output)
        self.assertIn("documentation", output)
        self.assertIn("ready-for-agent", output)

        # Policy validation
        policy = {"version": 1, "stages": stages}
        validated = validate_policy(policy)
        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]["name"], "labels")

    def test_declining_content_stages_returns_empty_list(self):
        """User declines configuring labels or authored files."""
        in_stream = io.StringIO("n\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
        )

        stages = prompt_content_stages(session)
        self.assertEqual(stages, [])


if __name__ == "__main__":
    unittest.main()
