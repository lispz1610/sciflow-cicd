"""Unit and integration tests for interactive actions and security configuration stage."""

import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.interactive import Session
from scripts.setup_github_repo import validate_policy
from scripts.interactive_actions_security import (
    prompt_actions_permissions,
    prompt_security_features,
    prompt_security_stage,
    build_actions_payload,
    build_security_payload,
    build_codeql_payload,
    build_actions_security_stages,
    prompt_actions_security_stage,
)


def create_test_session(
    repository: str = "acme/test-repo",
    in_str: str = "",
    resume_mode: bool = False,
    defaults: dict = None,
    inventory: dict = None,
) -> tuple[Session, io.StringIO, io.StringIO]:
    in_stream = io.StringIO(in_str)
    out_stream = io.StringIO()
    err_stream = io.StringIO()
    session = Session(
        repository=repository,
        api=MagicMock(),
        in_stream=in_stream,
        out_stream=out_stream,
        err_stream=err_stream,
        resume_mode=resume_mode,
        defaults=defaults if defaults is not None else {},
        inventory=inventory if inventory is not None else {},
    )
    return session, out_stream, err_stream


class ActionsWorkflowPermissionsPromptTests(unittest.TestCase):
    """Tests for prompting user for default Actions workflow permissions."""

    def test_prompt_actions_permissions_user_confirms(self):
        """User answers 'y' -> returns True."""
        session, out_s, _ = create_test_session(in_str="y\n")
        result = prompt_actions_permissions(session)
        self.assertTrue(result)
        out_text = out_s.getvalue().lower()
        self.assertTrue("actions" in out_text or "workflow" in out_text)

    def test_prompt_actions_permissions_user_declines(self):
        """User answers 'n' -> returns False."""
        session, _, _ = create_test_session(in_str="n\n")
        result = prompt_actions_permissions(session)
        self.assertFalse(result)

    def test_prompt_actions_permissions_eof_returns_none(self):
        """EOF on input stream -> returns None."""
        session, _, _ = create_test_session(in_str="")
        result = prompt_actions_permissions(session)
        self.assertIsNone(result)

    def test_prompt_actions_permissions_default_in_fresh_mode(self):
        """In fresh mode (resume_mode=False), default should be True (read-only default tokens)."""
        session, out_s, _ = create_test_session(in_str="\n", resume_mode=False)
        result = prompt_actions_permissions(session)
        self.assertTrue(result)
        self.assertIn("[Y/n]", out_s.getvalue())

    def test_prompt_actions_permissions_resume_mode_with_active_read_permissions(self):
        """When resuming with detected read permissions, default is True."""
        defaults = {
            "actions": {
                "default_workflow_permissions": "read",
                "can_approve_pull_request_reviews": False,
            }
        }
        session, out_s, _ = create_test_session(
            in_str="\n",
            resume_mode=True,
            defaults=defaults,
        )
        result = prompt_actions_permissions(session)
        self.assertTrue(result)
        self.assertIn("[Y/n]", out_s.getvalue())

    def test_prompt_actions_permissions_resume_mode_with_write_permissions(self):
        """When resuming with detected write permissions (not read-only), default is False."""
        defaults = {
            "actions": {
                "default_workflow_permissions": "write",
                "can_approve_pull_request_reviews": True,
            }
        }
        session, out_s, _ = create_test_session(
            in_str="\n",
            resume_mode=True,
            defaults=defaults,
        )
        result = prompt_actions_permissions(session)
        self.assertFalse(result)
        self.assertIn("[y/N]", out_s.getvalue())

    def test_prompt_actions_permissions_resume_mode_with_empty_defaults(self):
        """When resuming with empty defaults, default is False."""
        session, out_s, _ = create_test_session(
            in_str="\n",
            resume_mode=True,
            defaults={},
        )
        result = prompt_actions_permissions(session)
        self.assertFalse(result)
        self.assertIn("[y/N]", out_s.getvalue())


class SecurityFeaturesPromptTests(unittest.TestCase):
    """Tests for prompting user for repository security capabilities."""

    def test_prompt_security_features_select_all(self):
        """User selects all 6 options -> returns all 6 features."""
        session, _, _ = create_test_session(in_str="1, 2, 3, 4, 5, 6\n")
        result = prompt_security_features(session)
        self.assertEqual(
            result,
            [
                "vulnerability-alerts",
                "automated-security-fixes",
                "secret-scanning",
                "push-protection",
                "private-vulnerability-reporting",
                "codeql",
            ],
        )

    def test_prompt_security_features_select_subset(self):
        """User selects options 1, 6 -> returns vulnerability-alerts and codeql."""
        session, _, _ = create_test_session(in_str="1, 6\n")
        result = prompt_security_features(session)
        self.assertEqual(result, ["vulnerability-alerts", "codeql"])

    def test_prompt_security_features_select_secret_scanning_only(self):
        """User selects option 3 -> returns secret-scanning only."""
        session, _, _ = create_test_session(in_str="3\n")
        result = prompt_security_features(session)
        self.assertEqual(result, ["secret-scanning"])

    def test_prompt_security_features_select_expanded_security_features(self):
        """User selects Dependabot fixes (2), push protection (4), and private reporting (5)."""
        session, _, _ = create_test_session(in_str="2, 4, 5\n")
        result = prompt_security_features(session)
        self.assertEqual(
            result,
            [
                "automated-security-fixes",
                "push-protection",
                "private-vulnerability-reporting",
            ],
        )

    def test_prompt_security_features_select_none(self):
        """User types 'none' -> returns empty list."""
        session, _, _ = create_test_session(in_str="none\n")
        result = prompt_security_features(session)
        self.assertEqual(result, [])

    def test_prompt_security_features_select_zero(self):
        """User types '0' -> returns empty list."""
        session, _, _ = create_test_session(in_str="0\n")
        result = prompt_security_features(session)
        self.assertEqual(result, [])

    def test_prompt_security_features_eof_returns_none(self):
        """EOF on input stream -> returns None."""
        session, _, _ = create_test_session(in_str="")
        result = prompt_security_features(session)
        self.assertIsNone(result)

    def test_prompt_security_features_fresh_mode_defaults_all_public(self):
        """In fresh mode on a public repo, all 6 security features are pre-selected by default."""
        session, out_s, _ = create_test_session(
            in_str="\n",
            resume_mode=False,
            inventory={"repository": {"visibility": "public"}},
        )
        result = prompt_security_features(session)
        self.assertEqual(
            result,
            [
                "vulnerability-alerts",
                "automated-security-fixes",
                "secret-scanning",
                "push-protection",
                "private-vulnerability-reporting",
                "codeql",
            ],
        )
        out_text = out_s.getvalue()
        self.assertIn("[x] 1. Vulnerability alerts", out_text)
        self.assertIn("[x] 2. Dependabot automated security fixes", out_text)
        self.assertIn("[x] 3. Secret scanning", out_text)
        self.assertIn("[x] 4. Secret scanning push protection", out_text)
        self.assertIn("[x] 5. Private vulnerability reporting", out_text)
        self.assertIn("[x] 6. CodeQL default setup", out_text)

    def test_prompt_security_features_fresh_mode_private_repo_excludes_private_reporting(self):
        """On private repositories, private vulnerability reporting is excluded from fresh defaults."""
        session, out_s, _ = create_test_session(
            in_str="\n",
            resume_mode=False,
            inventory={"repository": {"visibility": "private"}},
        )
        result = prompt_security_features(session)
        self.assertNotIn("private-vulnerability-reporting", result)
        out_text = out_s.getvalue()
        self.assertIn("[ ] 5. Private vulnerability reporting", out_text)

    def test_prompt_security_features_resume_mode_preselection(self):
        """When resuming, only previously active security features are pre-selected."""
        defaults = {
            "security": ["vulnerability-alerts", "codeql"]
        }
        session, out_s, _ = create_test_session(
            in_str="\n",
            resume_mode=True,
            defaults=defaults,
        )
        result = prompt_security_features(session)
        self.assertEqual(result, ["vulnerability-alerts", "codeql"])
        out_text = out_s.getvalue()
        self.assertIn("[x] 1.", out_text)
        self.assertIn("[ ] 2.", out_text)
        self.assertIn("[ ] 3.", out_text)
        self.assertIn("[ ] 4.", out_text)
        self.assertIn("[ ] 5.", out_text)
        self.assertIn("[x] 6.", out_text)

    def test_prompt_security_features_resume_mode_empty_preselection(self):
        """When resuming with no active security features, none are pre-selected."""
        defaults = {
            "security": []
        }
        session, out_s, _ = create_test_session(
            in_str="\n",
            resume_mode=True,
            defaults=defaults,
        )
        result = prompt_security_features(session)
        self.assertEqual(result, [])
        out_text = out_s.getvalue()
        self.assertIn("[ ] 1.", out_text)
        self.assertIn("[ ] 2.", out_text)
        self.assertIn("[ ] 3.", out_text)
        self.assertIn("[ ] 4.", out_text)
        self.assertIn("[ ] 5.", out_text)
        self.assertIn("[ ] 6.", out_text)


class StagePayloadConstructionTests(unittest.TestCase):
    """Tests for constructing policy stage payloads."""

    def test_build_actions_payload(self):
        """build_actions_payload returns {'name': 'actions'}."""
        payload = build_actions_payload()
        self.assertEqual(payload, {"name": "actions"})

    def test_build_codeql_payload(self):
        """build_codeql_payload returns {'name': 'codeql'}."""
        payload = build_codeql_payload()
        self.assertEqual(payload, {"name": "codeql"})

    def test_build_security_payload_multiple_features(self):
        """build_security_payload with multiple features constructs expected payload."""
        features = ["vulnerability-alerts", "automated-security-fixes", "secret-scanning", "push-protection"]
        payload = build_security_payload(features)
        self.assertEqual(
            payload,
            {
                "name": "security",
                "enable": ["vulnerability-alerts", "automated-security-fixes", "secret-scanning", "push-protection"],
            },
        )

    def test_build_security_payload_single_feature(self):
        """build_security_payload with single feature constructs expected payload."""
        payload = build_security_payload(["vulnerability-alerts"])
        self.assertEqual(payload, {"name": "security", "enable": ["vulnerability-alerts"]})

    def test_build_security_payload_empty_features_returns_none(self):
        """build_security_payload with empty list returns None."""
        payload = build_security_payload([])
        self.assertIsNone(payload)

    def test_build_security_payload_filters_out_codeql(self):
        """build_security_payload excludes 'codeql' as CodeQL belongs in its own stage."""
        payload = build_security_payload(["vulnerability-alerts", "codeql"])
        self.assertEqual(payload, {"name": "security", "enable": ["vulnerability-alerts"]})

    def test_build_security_payload_only_codeql_returns_none(self):
        """build_security_payload with only codeql returns None (codeql is handled separately)."""
        payload = build_security_payload(["codeql"])
        self.assertIsNone(payload)

    def test_build_actions_security_stages_all_selected(self):
        """All options selected constructs actions, security, and codeql stages."""
        stages = build_actions_security_stages(
            actions=True,
            security_features=[
                "vulnerability-alerts",
                "automated-security-fixes",
                "secret-scanning",
                "push-protection",
                "private-vulnerability-reporting",
                "codeql",
            ],
        )
        expected = [
            {"name": "actions"},
            {
                "name": "security",
                "enable": [
                    "vulnerability-alerts",
                    "automated-security-fixes",
                    "secret-scanning",
                    "push-protection",
                    "private-vulnerability-reporting",
                ],
            },
            {"name": "codeql"},
        ]
        self.assertEqual(stages, expected)

        # Validate against policy validator
        policy = {"version": 1, "stages": stages}
        validated = validate_policy(policy)
        self.assertEqual(validated, expected)

    def test_build_actions_security_stages_actions_only(self):
        """Only actions selected constructs actions stage."""
        stages = build_actions_security_stages(actions=True, security_features=[])
        expected = [{"name": "actions"}]
        self.assertEqual(stages, expected)

        policy = {"version": 1, "stages": stages}
        validated = validate_policy(policy)
        self.assertEqual(validated, expected)

    def test_build_actions_security_stages_security_only(self):
        """Only security features selected constructs security stage."""
        stages = build_actions_security_stages(
            actions=False,
            security_features=["vulnerability-alerts", "automated-security-fixes"],
        )
        expected = [
            {"name": "security", "enable": ["vulnerability-alerts", "automated-security-fixes"]},
        ]
        self.assertEqual(stages, expected)

        policy = {"version": 1, "stages": stages}
        validated = validate_policy(policy)
        self.assertEqual(validated, expected)

    def test_build_actions_security_stages_codeql_only(self):
        """Only codeql selected constructs codeql stage."""
        stages = build_actions_security_stages(
            actions=False,
            security_features=["codeql"],
        )
        expected = [{"name": "codeql"}]
        self.assertEqual(stages, expected)

        policy = {"version": 1, "stages": stages}
        validated = validate_policy(policy)
        self.assertEqual(validated, expected)

    def test_build_actions_security_stages_none_selected(self):
        """Nothing selected returns empty list."""
        stages = build_actions_security_stages(actions=False, security_features=[])
        self.assertEqual(stages, [])


class InteractiveActionsSecurityStageFlowTests(unittest.TestCase):
    """End-to-end flow tests for prompt_actions_security_stage."""

    def test_full_flow_accept_all_fresh_mode(self):
        """Fresh mode with default inputs generates actions, security, and codeql stages."""
        session, _, _ = create_test_session(
            in_str="\n\n",  # default for actions, default for security
            resume_mode=False,
            inventory={"repository": {"visibility": "public"}},
        )
        stages = prompt_actions_security_stage(session)
        expected = [
            {"name": "actions"},
            {
                "name": "security",
                "enable": [
                    "vulnerability-alerts",
                    "automated-security-fixes",
                    "secret-scanning",
                    "push-protection",
                    "private-vulnerability-reporting",
                ],
            },
            {"name": "codeql"},
        ]
        self.assertEqual(stages, expected)

    def test_full_flow_custom_selection(self):
        """User selects read-only actions and only secret scanning."""
        session, _, _ = create_test_session(
            in_str="y\n3\n",  # Yes to actions, option 3 (secret-scanning) for security
            resume_mode=False,
        )
        stages = prompt_actions_security_stage(session)
        expected = [
            {"name": "actions"},
            {"name": "security", "enable": ["secret-scanning"]},
        ]
        self.assertEqual(stages, expected)

    def test_full_flow_decline_actions_select_codeql_only(self):
        """User declines actions and selects codeql only."""
        session, _, _ = create_test_session(
            in_str="n\n6\n",  # No to actions, option 6 (codeql)
            resume_mode=False,
        )
        stages = prompt_actions_security_stage(session)
        expected = [{"name": "codeql"}]
        self.assertEqual(stages, expected)

    def test_full_flow_decline_all(self):
        """User declines actions and selects none for security."""
        session, _, _ = create_test_session(
            in_str="n\nnone\n",
            resume_mode=False,
        )
        stages = prompt_actions_security_stage(session)
        self.assertEqual(stages, [])

    def test_full_flow_resume_mode_defaults(self):
        """Resume mode defaults are preserved when pressing enter."""
        defaults = {
            "actions": {"default_workflow_permissions": "read"},
            "security": ["secret-scanning", "push-protection"],
        }
        session, _, _ = create_test_session(
            in_str="\n\n",
            resume_mode=True,
            defaults=defaults,
        )
        stages = prompt_actions_security_stage(session)
        expected = [
            {"name": "actions"},
            {"name": "security", "enable": ["secret-scanning", "push-protection"]},
        ]
        self.assertEqual(stages, expected)

    def test_full_flow_eof_on_actions_returns_none(self):
        """EOF on actions prompt returns None."""
        session, _, _ = create_test_session(in_str="", resume_mode=False)
        stages = prompt_actions_security_stage(session)
        self.assertIsNone(stages)

    def test_full_flow_eof_on_security_returns_none(self):
        """EOF on security prompt returns None."""
        session, _, _ = create_test_session(in_str="y\n", resume_mode=False)
        stages = prompt_actions_security_stage(session)
        self.assertIsNone(stages)

    def test_import_from_interactive_module(self):
        """prompt_actions_security_stage is accessible via scripts.interactive export."""
        from scripts.interactive import prompt_actions_security_stage as interactive_stage_fn
        self.assertIs(interactive_stage_fn, prompt_actions_security_stage)

    def test_inventory_fallback_for_actions_and_security(self):
        """When defaults are empty in resume mode, falls back to inspecting session.inventory."""
        inventory = {
            "repository": {"visibility": "public"},
            "workflow_permissions": {"default_workflow_permissions": "read"},
            "vulnerability_alerts": {"status": 204},
            "automated_security_fixes": {"status": 200, "enabled": True},
            "security_and_analysis": {
                "secret_scanning": {"status": "enabled"},
                "secret_scanning_push_protection": {"status": "enabled"},
            },
            "private_vulnerability_reporting": {"enabled": True},
            "codeql": {"state": "configured"},
        }
        session, _, _ = create_test_session(
            in_str="\n\n",
            resume_mode=True,
            defaults={},
            inventory=inventory,
        )
        stages = prompt_actions_security_stage(session)
        expected = [
            {"name": "actions"},
            {
                "name": "security",
                "enable": [
                    "vulnerability-alerts",
                    "automated-security-fixes",
                    "secret-scanning",
                    "push-protection",
                    "private-vulnerability-reporting",
                ],
            },
            {"name": "codeql"},
        ]
        self.assertEqual(stages, expected)

        # Full policy validation
        policy = {
            "version": 1,
            "repository": "acme/test-repo",
            "stages": stages,
        }
        validated = validate_policy(policy)
        self.assertEqual(validated, expected)


if __name__ == "__main__":
    unittest.main()
