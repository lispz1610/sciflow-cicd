"""Comprehensive test suite for selective customization menu controller and end-to-end dry-run/apply integration (Ticket #55)."""

import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
TESTS_DIR = ROOT_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_setup_github_repo import InventoryApi
from test_interactive_one_click import OneClickMockApi
from configure_github_repo import ApiError
from scripts.prompts import PromptExit
from scripts.interactive import Session, run_interactive_workflow, extract_defaults_from_inventory
from scripts.setup_profiles import (
    Profile,
    SCIENTIFIC_PYTHON,
    HPC_CMAKE_MPI,
    AGENT_READY_GOVERNANCE,
    MINIMAL_LEAN,
    get_profile,
    recommend_profile,
)
from scripts.interactive_apply import (
    format_dry_run_summary,
    display_dry_run_summary,
    format_apply_summary,
    run_interactive_apply,
)
from scripts.interactive_customization import (
    format_customization_menu,
    prompt_customization_selection,
    run_customization,
    classify_change_diff,
)


class TestCustomizationMenuFormatting(unittest.TestCase):
    """Test compact section menu presentation and terminal width constraints."""

    def test_format_customization_menu_contains_all_sections(self):
        menu = format_customization_menu()
        self.assertIn("Selective Customization Menu", menu)
        self.assertIn("Features", menu)
        self.assertIn("Merge Hygiene", menu)
        self.assertIn("Security & CodeQL", menu)
        self.assertIn("Workflows & CI", menu)
        self.assertIn("Governance / Ruleset Gates", menu)
        self.assertIn("Review & Proceed to Dry-Run", menu)
        self.assertIn("Cancel", menu)

    def test_menu_lines_within_80_to_100_columns(self):
        menu = format_customization_menu()
        for line in menu.splitlines():
            self.assertLessEqual(
                len(line),
                100,
                f"Line exceeds 100 columns ({len(line)} chars): {line!r}",
            )


class TestCustomizationMenuSelection(unittest.TestCase):
    """Test user selection parsing, shortcut keys, defaults, and invalid input handling."""

    def test_prompt_selection_numeric_inputs(self):
        for num in ["1", "2", "3", "4", "5", "6", "7"]:
            in_s = io.StringIO(f"{num}\n")
            out_s = io.StringIO()
            choice = prompt_customization_selection(in_stream=in_s, out_stream=out_s)
            self.assertEqual(choice, num)

    def test_prompt_selection_letter_shortcuts(self):
        mappings = {
            "f": "1",
            "m": "2",
            "s": "3",
            "w": "4",
            "g": "5",
            "r": "6",
            "c": "7",
            "F": "1",
            "M": "2",
            "S": "3",
            "W": "4",
            "G": "5",
            "R": "6",
            "C": "7",
        }
        for key, expected in mappings.items():
            in_s = io.StringIO(f"{key}\n")
            out_s = io.StringIO()
            choice = prompt_customization_selection(in_stream=in_s, out_stream=out_s)
            self.assertEqual(choice, expected)

    def test_prompt_selection_default_on_enter(self):
        in_s = io.StringIO("\n")
        out_s = io.StringIO()
        choice = prompt_customization_selection(in_stream=in_s, out_stream=out_s, default="6")
        self.assertEqual(choice, "6")

    def test_prompt_selection_invalid_reprompts(self):
        in_s = io.StringIO("invalid\nxyz\n2\n")
        out_s = io.StringIO()
        choice = prompt_customization_selection(in_stream=in_s, out_stream=out_s)
        self.assertEqual(choice, "2")
        self.assertIn("Invalid selection", out_s.getvalue())

    def test_prompt_selection_eof_trapped(self):
        in_s = io.StringIO("")
        out_s = io.StringIO()
        choice = prompt_customization_selection(in_stream=in_s, out_stream=out_s, raise_on_interrupt=False)
        self.assertIsNone(choice)

        in_s_raise = io.StringIO("")
        out_s_raise = io.StringIO()
        with self.assertRaises(PromptExit):
            prompt_customization_selection(in_stream=in_s_raise, out_stream=out_s_raise, raise_on_interrupt=True)


class TestSelectiveCustomizationController(unittest.TestCase):
    """Test jumping to sections, cumulative modifications, returning to menu, and dry-run."""

    def setUp(self):
        self.api = OneClickMockApi("acme/project")

    def test_proceed_directly_to_dry_run_without_modifications(self):
        in_s = io.StringIO("6\nn\n")  # Select 6 (proceed to Dry-Run), then decline apply
        out_s = io.StringIO()
        session = Session(
            repository="acme/project",
            api=self.api,
            in_stream=in_s,
            out_stream=out_s,
        )

        exit_code = run_customization(session, active_profile=SCIENTIFIC_PYTHON)
        self.assertEqual(exit_code, 0)
        output = out_s.getvalue()
        self.assertIn("Dry-Run Summary", output)
        self.assertIn("Scientific Python", output)
        self.assertIn("Apply cancelled", output)

    def test_jump_to_features_and_proceed_to_dry_run(self):
        # 1: jump to features
        # "1": select Issues in multichoice
        # 6: proceed to Dry-Run
        # n: decline apply
        in_s = io.StringIO("1\n1\n6\nn\n")
        out_s = io.StringIO()
        session = Session(
            repository="acme/project",
            api=self.api,
            in_stream=in_s,
            out_stream=out_s,
        )

        exit_code = run_customization(session, active_profile=SCIENTIFIC_PYTHON)
        self.assertEqual(exit_code, 0)
        output = out_s.getvalue()
        self.assertIn("Features configuration updated", output)
        self.assertIn("Dry-Run Summary", output)

    def test_jump_to_merge_hygiene_and_proceed_to_dry_run(self):
        # 2: jump to merge
        # y: Change merge settings?
        # 1: Squash merge
        # 6: proceed to Dry-Run
        # n: decline apply
        in_s = io.StringIO("2\ny\n1\n6\nn\n")
        out_s = io.StringIO()
        session = Session(
            repository="acme/project",
            api=self.api,
            in_stream=in_s,
            out_stream=out_s,
        )

        exit_code = run_customization(session, active_profile=SCIENTIFIC_PYTHON)
        self.assertEqual(exit_code, 0)
        output = out_s.getvalue()
        self.assertIn("Merge hygiene configuration updated", output)
        self.assertIn("Dry-Run Summary", output)

    def test_jump_to_security_and_proceed_to_dry_run(self):
        # 3: jump to security
        # y: Configure Actions default permissions as read-only?
        # 1: select Vulnerability alerts
        # n: decline Dependabot
        # 6: proceed to Dry-Run
        # n: decline apply
        in_s = io.StringIO("3\ny\n1\nn\n6\nn\n")
        out_s = io.StringIO()
        session = Session(
            repository="acme/project",
            api=self.api,
            in_stream=in_s,
            out_stream=out_s,
        )

        exit_code = run_customization(session, active_profile=SCIENTIFIC_PYTHON)
        self.assertEqual(exit_code, 0)
        output = out_s.getvalue()
        self.assertIn("Security & CodeQL configuration updated", output)
        self.assertIn("Dry-Run Summary", output)

    def test_jump_to_governance_rulesets_with_skipped_evidence_pr(self):
        # 5: jump to governance / ruleset gates
        # y: configure merge gates?
        # skip: evidence PR (greenfield)
        # octocat: required reviewer
        # status-check: CI check matching active CI profile
        # 6: proceed to Dry-Run
        # n: decline apply
        in_s = io.StringIO("5\ny\nskip\noctocat\nstatus-check\n6\nn\n")
        out_s = io.StringIO()
        session = Session(
            repository="acme/project",
            api=self.api,
            in_stream=in_s,
            out_stream=out_s,
        )

        exit_code = run_customization(session, active_profile=SCIENTIFIC_PYTHON)
        self.assertEqual(exit_code, 0)
        output = out_s.getvalue()
        self.assertIn("Governance / Ruleset Gates configuration updated", output)
        self.assertIn("Pending gates and rulesets", output)

    def test_multiple_section_modifications_cumulative(self):
        # 1: features -> 1 (Issues)
        # 2: merge -> y -> 1 (Squash)
        # 6: proceed to Dry-Run
        # n: decline apply
        in_s = io.StringIO("1\n1\n2\ny\n1\n6\nn\n")
        out_s = io.StringIO()
        session = Session(
            repository="acme/project",
            api=self.api,
            in_stream=in_s,
            out_stream=out_s,
        )

        exit_code = run_customization(session, active_profile=SCIENTIFIC_PYTHON)
        self.assertEqual(exit_code, 0)
        output = out_s.getvalue()
        self.assertIn("Features configuration updated", output)
        self.assertIn("Merge hygiene configuration updated", output)
        self.assertIn("Dry-Run Summary", output)

    def test_cancel_option_exits_cleanly(self):
        in_s = io.StringIO("7\n")
        out_s = io.StringIO()
        session = Session(
            repository="acme/project",
            api=self.api,
            in_stream=in_s,
            out_stream=out_s,
        )

        exit_code = run_customization(session, active_profile=SCIENTIFIC_PYTHON)
        self.assertEqual(exit_code, 0)
        self.assertIn("Setup cancelled", out_s.getvalue())

    def test_eof_at_menu_level_exits_cleanly(self):
        in_s = io.StringIO("")
        out_s = io.StringIO()
        session = Session(
            repository="acme/project",
            api=self.api,
            in_stream=in_s,
            out_stream=out_s,
        )

        exit_code = run_customization(session, active_profile=SCIENTIFIC_PYTHON)
        self.assertEqual(exit_code, 0)
        self.assertIn("Setup cancelled", out_s.getvalue())

    def test_sub_stage_interrupt_returns_to_menu(self):
        # Select 2 (merge), but simulate interrupt in prompt_merge_stage
        # Then select 7 (cancel)
        in_s = io.StringIO("2\n7\n")
        out_s = io.StringIO()
        session = Session(
            repository="acme/project",
            api=self.api,
            in_stream=in_s,
            out_stream=out_s,
        )

        with patch("scripts.interactive_customization.prompt_merge_stage", side_effect=PromptExit("interrupted")):
            exit_code = run_customization(session, active_profile=SCIENTIFIC_PYTHON)
            self.assertEqual(exit_code, 0)
            output = out_s.getvalue()
            self.assertIn("cancelled; returning to menu", output)
            self.assertIn("Setup cancelled", output)


class TestResumeStatePrePopulation(unittest.TestCase):
    """Test that resume mode pre-populates defaults from live inventory."""

    def test_resume_mode_populates_defaults_from_inventory(self):
        inventory = {
            "repository": {"language": "Python", "default_branch": "main"},
            "features": {"issues": True, "wiki": True, "projects": False},
            "merge": {
                "allow_squash_merge": True,
                "allow_merge_commit": False,
                "allow_rebase_merge": False,
                "delete_branch_on_merge": True,
                "allow_update_branch": True,
            },
            "workflow_permissions": {"default_workflow_permissions": "read"},
            "vulnerability_alerts": {"status": 204},
            "automated_security_fixes": {"status": 200},
            "agent_skills": True,
        }
        defaults = extract_defaults_from_inventory(inventory)
        self.assertIn("issues", defaults["features"])
        self.assertIn("wiki", defaults["features"])
        self.assertTrue(defaults["merge"]["allow_squash_merge"])
        self.assertEqual(defaults["actions"]["default_workflow_permissions"], "read")
        self.assertIn("vulnerability-alerts", defaults["security"])
        self.assertIn("automated-security-fixes", defaults["security"])
        self.assertTrue(defaults["agent_skills"])

    def test_recommend_profile_with_agent_skills_in_live_inventory(self):
        inventory = {
            "repository": {"language": None},
            "agent_skills": True,
        }
        rec = recommend_profile(repo_dir=None, inventory=inventory)
        self.assertEqual(rec.id, "agent-ready-governance")


class TestDryRunAndApplyFormatting(unittest.TestCase):
    """Test active setup profile identity, comparison table, diffs, and pending ruleset status."""

    def test_dry_run_summary_displays_active_profile_identity(self):
        dry_run_result = {
            "mode": "dry-run",
            "repository": "acme/project",
            "changes": [
                {
                    "stage": "features",
                    "operation": "PATCH",
                    "path": "/repos/acme/project",
                    "current": {"has_issues": False},
                    "desired": {"has_issues": True},
                    "status": "planned",
                }
            ],
        }
        summary = format_dry_run_summary(dry_run_result, profile=SCIENTIFIC_PYTHON)
        self.assertIn("Active Setup Profile: Scientific Python (scientific-python)", summary)
        self.assertIn("Dry-Run Summary", summary)

    def test_dry_run_summary_displays_comparison_table_and_diffs(self):
        dry_run_result = {
            "mode": "dry-run",
            "repository": "acme/project",
            "changes": [
                {
                    "stage": "features",
                    "operation": "PATCH",
                    "path": "/repos/acme/project",
                    "current": {"has_issues": False},
                    "desired": {"has_issues": True},
                    "status": "planned",
                },
                {
                    "stage": "workflows",
                    "operation": "create",
                    "path": ".github/workflows/ci.yml",
                    "current": None,
                    "desired": "content",
                    "status": "planned",
                },
                {
                    "stage": "gates",
                    "operation": "inspect",
                    "path": "/repos/acme/project/rulesets",
                    "reason": "Merge the draft Setup Pull Request or provide an approved evidence PR with successful CI checks before applying this gate",
                    "status": "pending",
                },
            ],
        }
        summary = format_dry_run_summary(dry_run_result, profile=SCIENTIFIC_PYTHON)
        self.assertIn("Diff Summary:", summary)
        self.assertIn("modified", summary)
        self.assertIn("added", summary)
        self.assertIn("pending", summary)
        self.assertIn("Pending gates and rulesets", summary)

    def test_apply_summary_displays_active_profile_modified_assets_and_next_steps(self):
        apply_result = {
            "mode": "apply",
            "repository": "acme/project",
            "changes": [
                {
                    "stage": "features",
                    "operation": "PATCH",
                    "path": "/repos/acme/project",
                    "status": "verified",
                },
                {
                    "stage": "gates",
                    "operation": "inspect",
                    "path": "/repos/acme/project/rulesets",
                    "status": "pending",
                    "reason": "Pending draft Setup PR merge",
                },
            ],
            "setup_pull_request": "https://github.com/acme/project/pull/42",
        }
        summary = format_apply_summary(apply_result, profile=SCIENTIFIC_PYTHON)
        self.assertIn("Active Setup Profile: Scientific Python (scientific-python)", summary)
        self.assertIn("Verified: 1", summary)
        self.assertIn("Pending: 1", summary)
        self.assertIn("Modified Assets:", summary)
        self.assertIn("features", summary)
        self.assertIn("Next Steps:", summary)
        self.assertIn("https://github.com/acme/project/pull/42", summary)

    def test_summary_line_lengths_within_100_columns(self):
        dry_run_result = {
            "mode": "dry-run",
            "repository": "acme/project",
            "changes": [
                {
                    "stage": "workflows",
                    "operation": "create",
                    "path": ".github/workflows/really-long-workflow-filename-for-scientific-pipeline.yml",
                    "status": "planned",
                },
                {
                    "stage": "gates",
                    "operation": "inspect",
                    "path": "/repos/acme/project/rulesets",
                    "reason": "Merge the draft Setup Pull Request or provide an approved evidence PR with successful CI checks before applying this gate",
                    "status": "pending",
                },
            ],
        }
        summary = format_dry_run_summary(dry_run_result, profile=SCIENTIFIC_PYTHON)
        for line in summary.splitlines():
            self.assertLessEqual(len(line), 100, f"Line exceeds 100 cols ({len(line)}): {line!r}")


class TestOneClickMenuIntegration(unittest.TestCase):
    """Test 1-click prompt routing to customization controller on 'c'."""

    def test_one_click_c_routes_to_customization(self):
        # 1-click: 'c' -> enters customization
        # menu: '7' -> Cancel
        in_s = io.StringIO("c\n7\n")
        out_s = io.StringIO()
        session = Session(
            repository="acme/project",
            api=InventoryApi(),
            in_stream=in_s,
            out_stream=out_s,
        )

        exit_code = run_interactive_workflow(session, profile=SCIENTIFIC_PYTHON)
        self.assertEqual(exit_code, 0)
        output = out_s.getvalue()
        self.assertIn("Selective Customization Menu", output)
        self.assertIn("Setup cancelled", output)

    def test_resume_mode_preserves_disable_wiki(self):
        in_s = io.StringIO("6\n")
        out_s = io.StringIO()
        session = Session(
            repository="acme/project",
            api=InventoryApi(),
            in_stream=in_s,
            out_stream=out_s,
            resume_mode=True,
            defaults={"features": ["issues"]},
            inventory={"repository": {"language": "Python"}},
        )

        with patch("scripts.interactive_customization.run_interactive_apply", return_value=0) as mock_apply:
            exit_code = run_customization(session, active_profile=SCIENTIFIC_PYTHON)
            self.assertEqual(exit_code, 0)
            policy = mock_apply.call_args[0][1]
            features_stage = next(s for s in policy["stages"] if s["name"] == "features")
            self.assertIn("issues", features_stage["enable"])
            self.assertIn("wiki", features_stage.get("disable", []))

    def test_customization_codeql_stage_cleanup(self):
        # Section 3 returns security stages without codeql
        in_s = io.StringIO("3\n6\n")
        out_s = io.StringIO()
        session = Session(
            repository="acme/project",
            api=InventoryApi(),
            in_stream=in_s,
            out_stream=out_s,
        )

        dummy_sec_stages = [
            {"name": "actions-policy", "settings": {"default_workflow_permissions": "read"}},
            {"name": "security", "enable": ["vulnerability-alerts"]},
        ]
        with patch("scripts.interactive_customization.prompt_actions_security_stage", return_value=dummy_sec_stages), \
             patch("scripts.interactive_customization.prompt_dependabot_stage", return_value=None), \
             patch("scripts.interactive_customization.run_interactive_apply", return_value=0) as mock_apply:
            exit_code = run_customization(session, active_profile=SCIENTIFIC_PYTHON)
            self.assertEqual(exit_code, 0)
            policy = mock_apply.call_args[0][1]
            stage_names = {s["name"] for s in policy["stages"]}
            self.assertNotIn("codeql", stage_names)
            self.assertIn("security", stage_names)

    def test_customization_workflows_pops_ci_profile(self):
        # Section 4 returns workflows stage -> ci-profile should be popped
        in_s = io.StringIO("4\n6\n")
        out_s = io.StringIO()
        session = Session(
            repository="acme/project",
            api=InventoryApi(),
            in_stream=in_s,
            out_stream=out_s,
        )

        dummy_wf_stage = {
            "name": "workflows",
            "workflows": [{"template": "python-ci", "filename": "python-ci.yml"}],
        }
        with patch("scripts.interactive_customization.prompt_workflows_stage", return_value=dummy_wf_stage), \
             patch("scripts.interactive_customization.run_interactive_apply", return_value=0) as mock_apply:
            exit_code = run_customization(session, active_profile=SCIENTIFIC_PYTHON)
            self.assertEqual(exit_code, 0)
            policy = mock_apply.call_args[0][1]
            stage_names = {s["name"] for s in policy["stages"]}
            self.assertIn("workflows", stage_names)
            self.assertNotIn("ci-profile", stage_names)
            self.assertNotIn("review", stage_names)

    def test_resume_state_selected_actions_without_allowlist_subkeys_does_not_crash(self):
        in_s = io.StringIO("6\n")
        out_s = io.StringIO()
        session = Session(
            repository="acme/project",
            api=InventoryApi(),
            in_stream=in_s,
            out_stream=out_s,
            resume_mode=True,
            defaults={
                "actions": {
                    "allowed_actions": "selected",
                    "default_workflow_permissions": "read",
                }
            },
        )
        with patch("scripts.interactive_customization.run_interactive_apply", return_value=0) as mock_apply:
            exit_code = run_customization(session, active_profile=SCIENTIFIC_PYTHON)
            self.assertEqual(exit_code, 0)
            policy = mock_apply.call_args[0][1]
            ap = next((s for s in policy["stages"] if s["name"] == "actions-policy"), None)
            self.assertIsNotNone(ap)
            self.assertNotEqual(ap["settings"].get("allowed_actions"), "selected")

    def test_option_3_pops_both_actions_and_actions_policy_and_wires_dependabot(self):
        in_s = io.StringIO("3\n3\n6\n")
        out_s = io.StringIO()
        session = Session(
            repository="acme/project",
            api=InventoryApi(),
            in_stream=in_s,
            out_stream=out_s,
        )
        first_sec = [
            {"name": "actions", "enable": True},
            {"name": "actions-policy", "settings": {"default_workflow_permissions": "read"}},
            {"name": "security", "enable": ["vulnerability-alerts"]},
        ]
        second_sec = [
            {"name": "security", "enable": ["vulnerability-alerts"]},
        ]
        dep_stage = {
            "name": "dependabot",
            "cadence": "monthly",
            "package_ecosystems": ["pip"],
            "open_pull_requests_limit": 5,
            "groups": True,
        }
        with patch("scripts.interactive_customization.prompt_actions_security_stage", side_effect=[first_sec, second_sec]), \
             patch("scripts.interactive_customization.prompt_dependabot_stage", side_effect=[dep_stage, None]) as mock_dep, \
             patch("scripts.interactive_customization.run_interactive_apply", return_value=0) as mock_apply:
            exit_code = run_customization(session, active_profile=SCIENTIFIC_PYTHON)
            self.assertEqual(exit_code, 0)
            self.assertEqual(mock_dep.call_count, 2)
            policy = mock_apply.call_args[0][1]
            stage_names = {s["name"] for s in policy["stages"]}
            self.assertNotIn("actions", stage_names)
            self.assertNotIn("actions-policy", stage_names)
            self.assertNotIn("dependabot", stage_names)
            self.assertIn("security", stage_names)
