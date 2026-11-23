"""Unit and integration tests for the unified multichoice Merge stage."""

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.interactive import Session
from scripts.interactive_repository_content import prompt_merge_stage
from scripts.setup_github_repo import validate_policy


class InteractiveMergeStageTests(unittest.TestCase):
    """Test suite for the unified multichoice merge settings questionnaire."""

    def setUp(self):
        self.mock_api = MagicMock()

    def test_declining_merge_stage_returns_none(self):
        """User declines changing merge settings -> returns None."""
        in_stream = io.StringIO("n\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
        )

        stage = prompt_merge_stage(session)
        self.assertIsNone(stage)
        self.assertIn("Merge settings determine how pull requests become commits", out_stream.getvalue())

    def test_fresh_mode_preselects_recommended_baseline(self):
        """In fresh mode, Squash merge, Delete branch on merge, and Update branch enabled are pre-selected."""
        in_stream = io.StringIO("y\n\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
            inventory={
                "merge": {
                    "allow_merge_commit": True,
                    "allow_squash_merge": False,
                    "allow_rebase_merge": False,
                    "delete_branch_on_merge": False,
                    "allow_update_branch": False,
                }
            },
        )

        stage = prompt_merge_stage(session)
        self.assertIsNotNone(stage)
        self.assertEqual(stage["name"], "merge")
        expected_settings = {
            "allow_squash_merge": True,
            "delete_branch_on_merge": True,
            "allow_update_branch": True,
            "allow_merge_commit": False,
        }
        self.assertEqual(stage["settings"], expected_settings)

        output = out_stream.getvalue()
        # Check multichoice syntax markers
        self.assertIn("[x] 1. Squash merge", output)
        self.assertIn("[x] 2. Delete branch after merge", output)
        self.assertIn("[x] 3. Update pull request branch", output)
        self.assertIn("[ ] 4. Merge commits", output)
        self.assertIn("[ ] 5. Rebase merge", output)
        # Check state summary
        self.assertIn("Merge settings determine how pull requests become commits", output)
        self.assertIn("Recommended default:", output)

    def test_custom_selection_squash_only(self):
        """User explicitly selects option 1 (Squash merge only)."""
        in_stream = io.StringIO("y\n1\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
            inventory={
                "merge": {
                    "allow_merge_commit": True,
                    "allow_squash_merge": False,
                    "allow_rebase_merge": False,
                    "delete_branch_on_merge": False,
                    "allow_update_branch": False,
                }
            },
        )

        stage = prompt_merge_stage(session)
        self.assertEqual(
            stage,
            {
                "name": "merge",
                "settings": {
                    "allow_squash_merge": True,
                    "allow_merge_commit": False,
                },
            },
        )

    def test_custom_selection_merge_commit_and_rebase(self):
        """User selects merge commit and rebase merge options."""
        in_stream = io.StringIO("y\n4, 5\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
            inventory={
                "merge": {
                    "allow_merge_commit": True,
                    "allow_squash_merge": True,
                    "allow_rebase_merge": False,
                    "delete_branch_on_merge": True,
                    "allow_update_branch": True,
                }
            },
        )

        stage = prompt_merge_stage(session)
        self.assertEqual(
            stage,
            {
                "name": "merge",
                "settings": {
                    "allow_squash_merge": False,
                    "delete_branch_on_merge": False,
                    "allow_update_branch": False,
                    "allow_rebase_merge": True,
                },
            },
        )

    def test_warning_when_no_merge_method_is_selected(self):
        """When user selects only branch hygiene (2, 3) without any merge method, warns and returns None."""
        in_stream = io.StringIO("y\n2, 3\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
        )

        stage = prompt_merge_stage(session)
        self.assertIsNone(stage)
        self.assertIn("at least one merge method must stay enabled", out_stream.getvalue())

    def test_resume_mode_preserves_existing_settings(self):
        """In resume mode, active settings in session.defaults are pre-selected."""
        defaults = {
            "merge": {
                "settings": {
                    "allow_squash_merge": False,
                    "allow_merge_commit": True,
                    "allow_rebase_merge": False,
                    "delete_branch_on_merge": True,
                    "allow_update_branch": False,
                }
            }
        }
        in_stream = io.StringIO("y\n1, 2, 4\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=True,
            defaults=defaults,
            inventory={
                "merge": {
                    "allow_squash_merge": False,
                    "allow_merge_commit": True,
                    "allow_rebase_merge": False,
                    "delete_branch_on_merge": True,
                    "allow_update_branch": False,
                }
            },
        )

        stage = prompt_merge_stage(session)
        self.assertEqual(
            stage,
            {
                "name": "merge",
                "settings": {
                    "allow_squash_merge": True,
                },
            },
        )
        output = out_stream.getvalue()
        self.assertIn("[ ] 1. Squash merge", output)
        self.assertIn("[x] 2. Delete branch after merge", output)
        self.assertIn("[ ] 3. Update pull request branch", output)
        self.assertIn("[x] 4. Merge commits", output)
        self.assertIn("[ ] 5. Rebase merge", output)

    def test_policy_validation_of_constructed_stage(self):
        """Constructed merge stage validates cleanly against Policy schema."""
        in_stream = io.StringIO("y\n1, 2, 3\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
        )

        stage = prompt_merge_stage(session)
        policy = {"version": 1, "stages": [stage]}
        validated = validate_policy(policy)
        self.assertEqual(len(validated), 1)
        self.assertEqual(validated[0]["name"], "merge")

    def test_resume_mode_supports_flat_merge_defaults(self):
        """In resume mode, flat session.defaults['merge'] dict preselects options over inventory."""
        in_stream = io.StringIO("y\n\n")
        out_stream = io.StringIO()
        session = Session(
            repository="acme/test-repo",
            api=self.mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=True,
            defaults={
                "merge": {
                    "allow_squash_merge": False,
                    "allow_merge_commit": False,
                    "allow_rebase_merge": True,
                    "delete_branch_on_merge": True,
                    "allow_update_branch": False,
                }
            },
            inventory={
                "merge": {
                    "allow_squash_merge": True,
                    "allow_merge_commit": False,
                    "allow_rebase_merge": False,
                    "delete_branch_on_merge": False,
                    "allow_update_branch": False,
                }
            },
        )

        stage = prompt_merge_stage(session)
        output = out_stream.getvalue()
        self.assertIn("[ ] 1. Squash merge", output)
        self.assertIn("[x] 2. Delete branch after merge", output)
        self.assertIn("[ ] 3. Update pull request branch", output)
        self.assertIn("[ ] 4. Merge commits", output)
        self.assertIn("[x] 5. Rebase merge", output)
        self.assertEqual(
            stage,
            {
                "name": "merge",
                "settings": {
                    "allow_squash_merge": False,
                    "delete_branch_on_merge": True,
                    "allow_rebase_merge": True,
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
