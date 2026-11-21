"""Tests for policy assembly and persistence."""

import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.interactive import Session
from scripts.policy_assembly import (
    DEFAULT_POLICY_PATH,
    assemble_policy,
    persist_policy,
    assemble_and_persist_policy,
)


class PolicyAssemblyTests(unittest.TestCase):
    """Test suite for assembling policy dictionaries from stage payloads."""

    def test_assemble_policy_aggregates_stages(self):
        """Assembles flattened list of stages into version 1 policy dictionary with repository."""
        repo = "octocat/Hello-World"
        stages_input = [
            {"name": "features", "enable": ["issues", "wiki"]},
            [
                {"name": "actions"},
                {"name": "security", "enable": ["vulnerability-alerts"]},
                {"name": "codeql"},
            ],
            {"name": "workflows", "workflows": [{"template": "pr-ci", "filename": "ci.yml"}]},
        ]

        policy = assemble_policy(repository=repo, stages=stages_input)

        expected = {
            "version": 2,
            "repository": "octocat/Hello-World",
            "stages": [
                {"name": "features", "enable": ["issues", "wiki"]},
                {"name": "actions"},
                {"name": "security", "enable": ["vulnerability-alerts"]},
                {"name": "codeql"},
                {"name": "workflows", "workflows": [{"template": "pr-ci", "filename": "ci.yml"}]},
            ],
        }
        self.assertEqual(policy, expected)

    def test_assemble_policy_ignores_none_and_empty_lists(self):
        """None values and empty stage lists are filtered out during aggregation."""
        repo = "octocat/Hello-World"
        stages_input = [
            None,
            {"name": "features", "enable": ["issues"]},
            None,
            [],
            [{"name": "actions"}],
        ]

        policy = assemble_policy(repository=repo, stages=stages_input)

        expected = {
            "version": 2,
            "repository": "octocat/Hello-World",
            "stages": [
                {"name": "features", "enable": ["issues"]},
                {"name": "actions"},
            ],
        }
        self.assertEqual(policy, expected)

    def test_assemble_policy_single_dict_stage(self):
        """Single dictionary stage payload is handled cleanly."""
        policy = assemble_policy("octocat/Hello-World", {"name": "actions"})
        self.assertEqual(
            policy,
            {
                "version": 2,
                "repository": "octocat/Hello-World",
                "stages": [{"name": "actions"}],
            },
        )

    def test_assemble_policy_empty_stages_raises_value_error(self):
        """Assembling policy with empty stages list raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            assemble_policy("octocat/Hello-World", [])
        self.assertIn("stage", str(ctx.exception).lower())

        with self.assertRaises(ValueError) as ctx:
            assemble_policy("octocat/Hello-World", [None, [], None])
        self.assertIn("stage", str(ctx.exception).lower())

    def test_assemble_policy_invalid_repository_raises_value_error(self):
        """Invalid repository name format raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            assemble_policy("invalid-repo-name", [{"name": "actions"}])
        self.assertIn("repository", str(ctx.exception).lower())

    def test_assemble_policy_duplicate_stages_raises_value_error(self):
        """Duplicate stages in stage list raises ValueError from validator."""
        stages = [
            {"name": "features", "enable": ["issues"]},
            {"name": "features", "enable": ["wiki"]},
        ]
        with self.assertRaises(ValueError) as ctx:
            assemble_policy("octocat/Hello-World", stages)
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_assemble_policy_invalid_stage_payload_raises_value_error(self):
        """Unsupported stage name or keys raises ValueError from validator."""
        stages = [
            {"name": "unknown-stage", "foo": "bar"},
        ]
        with self.assertRaises(ValueError) as ctx:
            assemble_policy("octocat/Hello-World", stages)
        self.assertIn("unsupported", str(ctx.exception).lower())

    def test_assemble_policy_omits_review_stage(self):
        """Review stages are completely omitted during policy assembly."""
        stages = [
            {"name": "features", "enable": ["issues"]},
            {"name": "review", "workflow": {"template": "open-code-review"}},
            {"name": "actions"},
        ]
        policy = assemble_policy("octocat/Hello-World", stages)
        stage_names = [s["name"] for s in policy["stages"]]
        self.assertEqual(stage_names, ["features", "actions"])
        self.assertNotIn("review", stage_names)


class PolicyPersistenceTests(unittest.TestCase):
    """Test suite for writing policy files to disk and notifying the user."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.temp_dir, ignore_errors=True))

    def test_persist_policy_writes_file_and_creates_directory(self):
        """Creates target directory if missing and persists valid JSON policy."""
        policy = {
            "version": 1,
            "repository": "octocat/Hello-World",
            "stages": [
                {"name": "features", "enable": ["issues"]},
            ],
        }
        dest_path = Path(self.temp_dir) / ".local" / "setup-policy.json"
        self.assertFalse(dest_path.parent.exists())

        result_path = persist_policy(policy, output_path=dest_path)

        self.assertEqual(result_path, dest_path)
        self.assertTrue(dest_path.exists())
        content = json.loads(dest_path.read_text(encoding="utf-8"))
        self.assertEqual(content, policy)

    def test_persist_policy_informs_user_on_out_stream(self):
        """Notifies user of file location and how to reuse it in headless mode."""
        policy = {
            "version": 1,
            "repository": "octocat/Hello-World",
            "stages": [
                {"name": "features", "enable": ["issues"]},
            ],
        }
        dest_path = Path(self.temp_dir) / "policy.json"
        out_stream = io.StringIO()

        persist_policy(policy, output_path=dest_path, out_stream=out_stream)

        output = out_stream.getvalue()
        self.assertIn(str(dest_path), output)
        self.assertIn("headless mode", output.lower())
        self.assertIn(f"sciflow-cicd dry-run --policy {dest_path}", output)
        self.assertIn(f"sciflow-cicd apply --policy {dest_path} --dry-run-result {dest_path.parent / 'setup-dry-run.json'}", output)

    def test_persist_policy_rejects_invalid_policy(self):
        """Rejects invalid policy dictionary without writing file."""
        invalid_policy = {"version": 999, "stages": []}
        dest_path = Path(self.temp_dir) / "invalid.json"

        with self.assertRaises(ValueError):
            persist_policy(invalid_policy, output_path=dest_path)

        self.assertFalse(dest_path.exists())


class SessionPolicyIntegrationTests(unittest.TestCase):
    """Test suite for assemble_and_persist_policy integration with Session."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.temp_dir, ignore_errors=True))

    def test_assemble_and_persist_policy_success(self):
        """Assembles stages from session, writes policy file, and returns (policy, path)."""
        out_stream = io.StringIO()
        session = Session(
            repository="octocat/Hello-World",
            api=MagicMock(),
            out_stream=out_stream,
        )
        stages = [
            {"name": "features", "enable": ["issues"]},
            [{"name": "actions"}],
        ]
        target_path = Path(self.temp_dir) / "output" / "policy.json"

        policy, written_path = assemble_and_persist_policy(
            session=session,
            stages=stages,
            output_path=target_path,
        )

        self.assertEqual(written_path, target_path)
        self.assertEqual(policy["repository"], "octocat/Hello-World")
        self.assertEqual(len(policy["stages"]), 2)
        self.assertTrue(target_path.exists())

        loaded = json.loads(target_path.read_text(encoding="utf-8"))
        self.assertEqual(loaded, policy)

        out_content = out_stream.getvalue()
        self.assertIn(str(target_path), out_content)
        self.assertIn(f"sciflow-cicd apply --policy {target_path} --dry-run-result {target_path.parent / 'setup-dry-run.json'}", out_content)

    def test_assemble_and_persist_policy_default_output_path(self):
        """Uses DEFAULT_POLICY_PATH when output_path is not specified."""
        out_stream = io.StringIO()
        session = Session(
            repository="octocat/Hello-World",
            api=MagicMock(),
            out_stream=out_stream,
        )
        stages = [{"name": "actions"}]

        # Monkeypatch DEFAULT_POLICY_PATH to temp location
        temp_default = Path(self.temp_dir) / ".local" / "setup-policy.json"
        import scripts.policy_assembly as pa
        original_default = pa.DEFAULT_POLICY_PATH
        try:
            pa.DEFAULT_POLICY_PATH = temp_default
            policy, written_path = assemble_and_persist_policy(
                session=session,
                stages=stages,
            )
            self.assertEqual(written_path, temp_default)
            self.assertTrue(temp_default.exists())
        finally:
            pa.DEFAULT_POLICY_PATH = original_default

    def test_assemble_and_persist_policy_empty_stages_raises(self):
        """Empty stages raise ValueError and do not create policy file."""
        out_stream = io.StringIO()
        session = Session(
            repository="octocat/Hello-World",
            api=MagicMock(),
            out_stream=out_stream,
        )
        target_path = Path(self.temp_dir) / "should_not_exist.json"

        with self.assertRaises(ValueError):
            assemble_and_persist_policy(session=session, stages=[], output_path=target_path)

        self.assertFalse(target_path.exists())

    def test_assemble_and_persist_with_all_stage_payloads(self):
        """Verify full combination of features, security, codeql, workflows, and gates."""
        out_stream = io.StringIO()
        session = Session(
            repository="octocat/Hello-World",
            api=MagicMock(),
            out_stream=out_stream,
        )
        stages = [
            {"name": "features", "enable": ["issues", "wiki", "projects"]},
            [
                {"name": "actions"},
                {"name": "security", "enable": ["vulnerability-alerts", "secret-scanning"]},
                {"name": "codeql"},
            ],
            [
                {
                    "name": "workflows",
                    "workflows": [
                        {
                            "template": "pr-ci",
                            "template_version": 1,
                            "filename": "ci.yml",
                            "triggers": ["pull_request"],
                            "checkout_sha": "11bd71901bbe5b1630ceea73d27597364c9af683",
                            "commands": ["python3 -m unittest"],
                        }
                    ],
                },
                {
                    "name": "gates",
                    "ruleset_name": "quality-gate",
                    "evidence_pr": 42,
                    "reviewer": "octocat",
                    "checks": {"ci-check": ".github/workflows/ci.yml"},
                },
            ],
        ]
        target_path = Path(self.temp_dir) / "full_policy.json"

        policy, written_path = assemble_and_persist_policy(
            session=session,
            stages=stages,
            output_path=target_path,
        )

        self.assertEqual(written_path, target_path)
        self.assertEqual(len(policy["stages"]), 6)
        stage_names = [s["name"] for s in policy["stages"]]
        self.assertEqual(
            stage_names,
            ["features", "actions", "security", "codeql", "workflows", "gates"],
        )

    def test_interactive_reexports(self):
        """Verify policy assembly functions are re-exported in scripts.interactive."""
        import scripts.interactive as interactive_mod

        self.assertTrue(hasattr(interactive_mod, "assemble_policy"))
        self.assertTrue(hasattr(interactive_mod, "persist_policy"))
        self.assertTrue(hasattr(interactive_mod, "assemble_and_persist_policy"))
        self.assertTrue(hasattr(interactive_mod, "DEFAULT_POLICY_PATH"))
        self.assertIs(interactive_mod.assemble_policy, assemble_policy)
        self.assertIs(interactive_mod.persist_policy, persist_policy)
        self.assertIs(interactive_mod.assemble_and_persist_policy, assemble_and_persist_policy)


if __name__ == "__main__":
    unittest.main()
