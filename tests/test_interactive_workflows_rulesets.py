"""Tests for interactive workflows and rulesets stage."""

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.interactive import Session
from scripts.interactive import extract_defaults_from_inventory
from scripts.setup_github_repo import validate_policy
from scripts.interactive_workflows_rulesets import (
    prompt_workflows_stage,
    prompt_python_ci_options,
    prompt_rulesets_stage,
    prompt_issue_state_gate_stage,
    prompt_agent_skills_stage,
    prompt_workflows_rulesets_stage,
    validate_pr_number,
    validate_reviewer,
    parse_check_spec,
    DEFAULT_CHECKOUT_SHA,
    DEFAULT_DEPENDENCY_SHA,
    DEFAULT_CI_COMMANDS,
)
from scripts.install_github_workflows import (
    detect_python_project,
    detect_python_versions,
    detect_python_package_manager,
    detect_git_lfs,
)


class InputValidationTests(unittest.TestCase):
    """Unit tests for standalone validation helper functions."""

    def test_validate_pr_number_valid(self):
        """Positive integers should validate successfully."""
        self.assertEqual(validate_pr_number("1"), 1)
        self.assertEqual(validate_pr_number("42"), 42)
        self.assertEqual(validate_pr_number(100), 100)

    def test_validate_pr_number_invalid(self):
        """Zero, negative integers, floats, and non-numeric strings should be rejected."""
        self.assertIsNone(validate_pr_number("0"))
        self.assertIsNone(validate_pr_number("-5"))
        self.assertIsNone(validate_pr_number("abc"))
        self.assertIsNone(validate_pr_number(""))
        self.assertIsNone(validate_pr_number("1.5"))
        self.assertIsNone(validate_pr_number(None))

    def test_validate_reviewer_valid(self):
        """Non-empty strings should validate successfully."""
        self.assertEqual(validate_reviewer("octocat"), "octocat")
        self.assertEqual(validate_reviewer("  mona-lisa  "), "mona-lisa")

    def test_validate_reviewer_invalid(self):
        """Empty or whitespace-only strings should be rejected."""
        self.assertIsNone(validate_reviewer(""))
        self.assertIsNone(validate_reviewer("   "))
        self.assertIsNone(validate_reviewer(None))

    def test_parse_check_spec_simple_name(self):
        """Check name alone defaults to .github/workflows/ci.yml."""
        result = parse_check_spec("ci-check")
        self.assertEqual(result, {"ci-check": ".github/workflows/ci.yml"})

    def test_parse_check_spec_path_equals_check(self):
        """WORKFLOW_PATH=CHECK_NAME format maps name to path."""
        result = parse_check_spec(".github/workflows/ci.yml=ci-check")
        self.assertEqual(result, {"ci-check": ".github/workflows/ci.yml"})

    def test_parse_check_spec_check_equals_path(self):
        """CHECK_NAME=WORKFLOW_PATH format maps name to path."""
        result = parse_check_spec("ci-check=.github/workflows/ci.yml")
        self.assertEqual(result, {"ci-check": ".github/workflows/ci.yml"})

    def test_parse_check_spec_relative_path_normalized(self):
        """Filename without .github/workflows/ prefix is normalized."""
        result = parse_check_spec("ci.yml=ci-check")
        self.assertEqual(result, {"ci-check": ".github/workflows/ci.yml"})

    def test_parse_check_spec_dependency_review(self):
        """Check name containing dependency-review defaults to dependency-review.yml."""
        result = parse_check_spec("dependency-review")
        self.assertEqual(result, {"dependency-review": ".github/workflows/dependency-review.yml"})

    def test_parse_check_spec_multiple_checks(self):
        """Multiple comma-separated checks are parsed."""
        result = parse_check_spec("ci-check, dependency-review")
        self.assertEqual(result, {
            "ci-check": ".github/workflows/ci.yml",
            "dependency-review": ".github/workflows/dependency-review.yml",
        })

    def test_parse_check_spec_invalid(self):
        """Invalid paths without yaml extension or empty names should be rejected."""
        self.assertIsNone(parse_check_spec(""))
        self.assertIsNone(parse_check_spec("   "))
        self.assertIsNone(parse_check_spec(".github/workflows/test.txt=ci-check"))


class WorkflowsStagePromptTests(unittest.TestCase):
    def test_inventory_resume_detects_active_issue_state_gate(self):
        defaults = extract_defaults_from_inventory({"rulesets": [{
            "name": "issue-state-gate", "enforcement": "active",
            "rules": [{"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "Issue State Gate", "integration_id": 991}],
            }}],
        }]})
        self.assertTrue(defaults["issue_state_gate"])

    def test_issue_state_gate_is_opt_in_and_resume_preselects_observed_rule(self):
        stream = io.StringIO("y\n")
        output = io.StringIO()
        stage = prompt_issue_state_gate_stage(in_stream=stream, out_stream=output)
        self.assertEqual(stage, {"name": "issue-state-gate"})
        self.assertIn("requires every implementation issue", output.getvalue())
        self.assertIn("Activation waits", output.getvalue())
        session = Session(repository="acme/project", api=MagicMock())
        session.resume_mode = True
        session.defaults = {"issue_state_gate": True}
        resumed = prompt_issue_state_gate_stage(session=session, in_stream=io.StringIO("\n"), out_stream=io.StringIO())
        self.assertEqual(resumed, {"name": "issue-state-gate"})

    """Tests for prompting and constructing the workflows stage payload."""

    def test_user_declines_staging_workflows(self):
        """When user declines staging workflows, returns None."""
        in_stream = io.StringIO("n\n")
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNone(stage)

    def test_user_selects_standard_ci_only(self):
        """When user selects standard CI, generates valid pr-ci workflow stage."""
        # Confirm: y, Multichoice: 1 (Standard CI)
        in_stream = io.StringIO("y\n1\n")
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(stage["name"], "workflows")
        self.assertEqual(len(stage["workflows"]), 1)

        wf = stage["workflows"][0]
        self.assertEqual(wf["template"], "pr-ci")
        self.assertEqual(wf["template_version"], 1)
        self.assertEqual(wf["filename"], "ci.yml")
        self.assertEqual(wf["triggers"], ["pull_request"])
        self.assertEqual(wf["checkout_sha"], DEFAULT_CHECKOUT_SHA)
        self.assertEqual(wf["commands"], DEFAULT_CI_COMMANDS)

        # Validate with policy validator
        policy = {"version": 1, "stages": [stage]}
        self.assertEqual(validate_policy(policy), [stage])

    def test_user_selects_dependency_review_only(self):
        """When user selects dependency review, generates valid dependency-review workflow stage."""
        # Confirm: y, Multichoice: 2 (Dependency review)
        in_stream = io.StringIO("y\n2\n")
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(stage["name"], "workflows")
        self.assertEqual(len(stage["workflows"]), 1)

        wf = stage["workflows"][0]
        self.assertEqual(wf["template"], "dependency-review")
        self.assertEqual(wf["template_version"], 1)
        self.assertEqual(wf["filename"], "dependency-review.yml")
        self.assertEqual(wf["triggers"], ["pull_request"])
        self.assertEqual(wf["checkout_sha"], DEFAULT_CHECKOUT_SHA)
        self.assertEqual(wf["action_sha"], DEFAULT_DEPENDENCY_SHA)

        policy = {"version": 1, "stages": [stage]}
        self.assertEqual(validate_policy(policy), [stage])

    def test_user_selects_both_workflows(self):
        """When user selects both workflows, generates stage with both templates."""
        # Confirm: y, Multichoice: 1, 2
        in_stream = io.StringIO("y\n1, 2\n")
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(stage["name"], "workflows")
        self.assertEqual(len(stage["workflows"]), 2)
        templates = [w["template"] for w in stage["workflows"]]
        self.assertIn("pr-ci", templates)
        self.assertIn("dependency-review", templates)

        policy = {"version": 1, "stages": [stage]}
        self.assertEqual(validate_policy(policy), [stage])

    def test_user_selects_none_in_multichoice(self):
        """When user answers yes to confirm but 'none' in multichoice, returns None."""
        in_stream = io.StringIO("y\nnone\n")
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNone(stage)

    def test_workflows_preselects_from_resume_defaults(self):
        """In resume mode, active workflows from defaults are pre-selected."""
        session = Session(
            repository="owner/repo",
            api=MagicMock(),
            resume_mode=True,
            defaults={"workflows": ["dependency-review"]},
        )
        # Empty input accepts pre-selected defaults (which should be dependency-review)
        in_stream = io.StringIO("y\n\n")
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(session=session, in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(len(stage["workflows"]), 1)
        self.assertEqual(stage["workflows"][0]["template"], "dependency-review")

    def test_user_selects_scientific_python_ci_profile(self):
        """User selects Scientific Python CI profile and answers matrix and tool questions."""
        # Confirm: y
        # Multichoice: 3 (Scientific Python CI)
        # Python versions: 3.10, 3.11, 3.12
        # Package manager: uv
        # Test commands: uv run pytest -v
        # Git LFS: y
        # Lint commands: flake8 .
        # Caching: y
        in_stream = io.StringIO("y\n3\n3.10, 3.11, 3.12\nuv\nuv run pytest -v\ny\nflake8 .\ny\n")
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(stage["name"], "workflows")
        self.assertEqual(len(stage["workflows"]), 1)

        wf = stage["workflows"][0]
        self.assertEqual(wf["template"], "profile-ci")
        self.assertEqual(wf["template_version"], 1)
        self.assertEqual(wf["filename"], "ci.yml")
        self.assertEqual(wf["profile"], "python")
        self.assertEqual(wf["checkout_sha"], DEFAULT_CHECKOUT_SHA)
        self.assertEqual(wf["python"]["versions"], ["3.10", "3.11", "3.12"])
        self.assertEqual(wf["python"]["package_manager"], "uv")
        self.assertEqual(wf["python"]["commands"], ["uv run pytest -v"])
        self.assertEqual(wf["python"]["lfs"], True)
        self.assertEqual(wf["python"]["lint_commands"], ["flake8 ."])
        self.assertEqual(wf["python"]["cache"], True)

    def test_user_selects_scientific_python_ci_profile_with_defaults(self):
        """User selects Scientific Python CI profile and accepts default answers."""
        # Confirm: y
        # Multichoice: 3
        # Versions: enter (default 3.11, 3.12)
        # Package manager: enter (default pip)
        # Commands: enter (default)
        # Git LFS: enter (default n)
        # Lint: enter (blank)
        # Caching: enter (default y)
        in_stream = io.StringIO("y\n3\n\n\n\n\n\n\n")
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        wf = stage["workflows"][0]
        self.assertEqual(wf["template"], "profile-ci")
        self.assertEqual(wf["profile"], "python")
        self.assertEqual(wf["python"]["package_manager"], "pip")
        self.assertEqual(wf["python"]["lfs"], False)
        self.assertEqual(wf["python"]["cache"], True)
        self.assertIn("3.11", wf["python"]["versions"])

    def test_user_selects_python_ci_and_dependency_review(self):
        """User selects both Dependency review and Scientific Python CI profile."""
        in_stream = io.StringIO("y\n2, 3\n3.11, 3.12\npip\npytest\nn\n\ny\n")
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(len(stage["workflows"]), 2)
        templates = {w["template"] for w in stage["workflows"]}
        self.assertEqual(templates, {"dependency-review", "profile-ci"})


class RulesetsStagePromptTests(unittest.TestCase):
    """Tests for prompting, reprompting, and constructing the rulesets (gates) stage."""

    def test_user_declines_gates(self):
        """When user declines configuring gates, returns None."""
        in_stream = io.StringIO("n\n")
        out_stream = io.StringIO()

        stage = prompt_rulesets_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNone(stage)

    def test_user_configures_gates_valid_inputs(self):
        """Valid inputs produce a well-formed gates stage payload."""
        # Confirm: y, PR: 42, Reviewer: octocat, Check: ci-check
        in_stream = io.StringIO("y\n42\noctocat\nci-check\n")
        out_stream = io.StringIO()

        stage = prompt_rulesets_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(stage["name"], "gates")
        self.assertEqual(stage["ruleset_name"], "quality-gate")
        self.assertEqual(stage["evidence_pr"], 42)
        self.assertEqual(stage["reviewer"], "octocat")
        self.assertEqual(stage["checks"], {"ci-check": ".github/workflows/ci.yml"})

        policy = {"version": 1, "stages": [stage]}
        self.assertEqual(validate_policy(policy), [stage])

    def test_pr_number_reprompts_on_invalid_input(self):
        """PR number prompt reprompts on non-integer, negative integer, and zero."""
        # invalid: 'abc', '-5', '0', then valid: '7'
        in_stream = io.StringIO("y\nabc\n-5\n0\n7\nreviewer1\nci-check\n")
        out_stream = io.StringIO()

        stage = prompt_rulesets_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(stage["evidence_pr"], 7)
        output = out_stream.getvalue()
        self.assertIn("positive integer", output.lower())

    def test_reviewer_reprompts_on_empty_input(self):
        """Reviewer prompt reprompts when empty string or whitespace entered."""
        # PR: 10, Reviewer: '', '   ', then 'bob', Check: ci-check
        in_stream = io.StringIO("y\n10\n\n   \nbob\nci-check\n")
        out_stream = io.StringIO()

        stage = prompt_rulesets_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(stage["reviewer"], "bob")

    def test_solo_maintainer_zero_approvals_with_none_reviewer(self):
        """Entering 'none' as reviewer configures solo maintainer mode with 0 required approvals."""
        in_stream = io.StringIO("y\n10\nnone\nci-check\n")
        out_stream = io.StringIO()

        stage = prompt_rulesets_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertNotIn("reviewer", stage)
        self.assertEqual(stage.get("required_approving_review_count"), 0)

    def test_workflow_check_with_explicit_path_and_reprompt(self):
        """Check name prompt accepts explicit WORKFLOW_PATH=CHECK_NAME format and reprompts on invalid."""
        # PR: 5, Reviewer: alice, Check: invalid, then valid
        in_stream = io.StringIO("y\n5\nalice\ninvalid.txt=bad\n.github/workflows/ci.yml=ci-check\n")
        out_stream = io.StringIO()

        stage = prompt_rulesets_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(stage["checks"], {"ci-check": ".github/workflows/ci.yml"})


class WorkflowsRulesetsCombinedStageTests(unittest.TestCase):
    """Tests for prompt_workflows_rulesets_stage coordinating both stages."""

    def test_both_workflows_and_gates_configured(self):
        """When user selects both workflows and gates, returns both stage dicts."""
        # Workflows confirm: y, multichoice: 1 (Standard CI)
        # Gates confirm: y, PR: 15, Reviewer: charlie, Check: ci-check
        in_stream = io.StringIO("y\n1\ny\n15\ncharlie\nci-check\nn\nn\n")
        out_stream = io.StringIO()

        stages = prompt_workflows_rulesets_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertEqual(len(stages), 2)
        self.assertEqual(stages[0]["name"], "workflows")
        self.assertEqual(stages[1]["name"], "gates")

        # Validate with policy validator
        policy = {"version": 1, "stages": stages}
        self.assertEqual(validate_policy(policy), stages)

    def test_workflows_only_configured(self):
        """When user configures workflows but declines gates, returns single stage."""
        # Workflows confirm: y, multichoice: 1
        # Gates confirm: n
        in_stream = io.StringIO("y\n1\nn\nn\nn\n")
        out_stream = io.StringIO()

        stages = prompt_workflows_rulesets_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0]["name"], "workflows")

    def test_gates_only_configured(self):
        """When user declines workflows but configures gates, returns single stage."""
        # Workflows confirm: n
        # Gates confirm: y, PR: 20, Reviewer: dana, Check: ci-check
        in_stream = io.StringIO("n\ny\n20\ndana\nci-check\nn\nn\n")
        out_stream = io.StringIO()

        stages = prompt_workflows_rulesets_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertEqual(len(stages), 1)
        self.assertEqual(stages[0]["name"], "gates")

    def test_neither_configured(self):
        """When user declines both workflows and gates, returns empty list."""
        in_stream = io.StringIO("n\nn\nn\nn\n")
        out_stream = io.StringIO()

        stages = prompt_workflows_rulesets_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertEqual(stages, [])

    def test_eof_handling_returns_none(self):
        """EOF on first prompt returns None."""
        in_stream = io.StringIO("")  # immediate EOF
        out_stream = io.StringIO()

        stages = prompt_workflows_rulesets_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNone(stages)

    def test_eof_handling_during_gates_prompts_returns_none(self):
        """EOF during PR number or reviewer prompt returns None."""
        # Confirm workflows: n, Confirm gates: y, PR: EOF
        in_stream = io.StringIO("n\ny\n")
        out_stream = io.StringIO()
        stages = prompt_workflows_rulesets_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNone(stages)

        # Confirm workflows: n, Confirm gates: y, PR: 10, Reviewer: EOF
        in_stream2 = io.StringIO("n\ny\n10\n")
        out_stream2 = io.StringIO()
        stages2 = prompt_workflows_rulesets_stage(in_stream=in_stream2, out_stream=out_stream2)
        self.assertIsNone(stages2)

    def test_session_streams_propagation(self):
        """When session is provided, its streams are used by default."""
        in_stream = io.StringIO("n\nn\nn\nn\n")
        out_stream = io.StringIO()
        session = Session(
            repository="owner/repo",
            api=MagicMock(),
            in_stream=in_stream,
            out_stream=out_stream,
        )

        stages = prompt_workflows_rulesets_stage(session=session)
        self.assertEqual(stages, [])
        self.assertIn("Stage standard CI or dependency review workflows", out_stream.getvalue())
        self.assertIn("Configure branch ruleset merge gates?", out_stream.getvalue())

    def test_agent_skills_is_opt_in_and_resume_preselects_detected_bundle(self):
        self.assertIsNone(prompt_agent_skills_stage(in_stream=io.StringIO("n\n"), out_stream=io.StringIO()))
        session = Session(repository="acme/project", api=MagicMock(), resume_mode=True,
                          defaults={"agent_skills": True})
        output = io.StringIO()
        stage = prompt_agent_skills_stage(session=session, in_stream=io.StringIO("\n"), out_stream=output)
        self.assertEqual(stage, {"name": "agent-skills", "bundle": "mattpocock"})
        self.assertIn("Recommended default: Yes", output.getvalue())
        self.assertIn(".agents/skills/", output.getvalue())

    def test_combined_interactive_stage_can_select_agent_skills(self):
        stages = prompt_workflows_rulesets_stage(in_stream=io.StringIO("n\nn\nn\ny\n"),
                                                  out_stream=io.StringIO())
        self.assertEqual(stages, [{"name": "agent-skills", "bundle": "mattpocock"}])
        self.assertEqual(validate_policy({"version": 2, "stages": stages}), stages)

    def test_gates_resume_mode_defaults(self):
        """In resume mode, gates defaults pre-populate and accept empty input."""
        session = Session(
            repository="owner/repo",
            api=MagicMock(),
            resume_mode=True,
            defaults={
                "gates": {
                    "ruleset_name": "custom-gate",
                    "evidence_pr": 99,
                    "reviewer": "preconfigured-reviewer",
                }
            },
        )
        # Confirm gates: default (y), PR: default (99), Reviewer: default, Check: default (ci-check)
        in_stream = io.StringIO("y\n\n\n\n")
        out_stream = io.StringIO()

        stage = prompt_rulesets_stage(session=session, in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(stage["ruleset_name"], "custom-gate")
        self.assertEqual(stage["evidence_pr"], 99)
        self.assertEqual(stage["reviewer"], "preconfigured-reviewer")
        self.assertEqual(stage["checks"], {"ci-check": ".github/workflows/ci.yml"})

    def test_interactive_module_reexport(self):
        """Verify prompt_workflows_rulesets_stage is exposed from scripts.interactive."""
        from scripts.interactive import prompt_workflows_rulesets_stage as reexported
        self.assertIs(reexported, prompt_workflows_rulesets_stage)


class LocalRepositoryDetectionTests(unittest.TestCase):
    """Unit tests for Python CI profile local workspace detection."""

    def test_detect_python_project_from_workspace_with_empty_inventory(self):
        """detect_python_project detects Python repository when inventory is empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
            self.assertTrue(detect_python_project(inventory={}, root_dir=tmpdir))

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            (tmppath / "requirements.txt").write_text("pytest>=7.0\n", encoding="utf-8")
            self.assertTrue(detect_python_project(inventory={}, root_dir=tmpdir))

    def test_prompt_workflows_stage_detects_python_from_session_root_dir(self):
        """prompt_workflows_stage preselects Scientific Python CI when root_dir contains pyproject.toml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "pyproject.toml").write_text("[project]\nname = 'sample'\n", encoding="utf-8")
            session = Session(
                repository="owner/repo",
                api=MagicMock(),
                inventory={},
            )
            session.root_dir = tmpdir
            out_stream = io.StringIO()
            stage = prompt_workflows_stage(session=session, in_stream=io.StringIO("n\n"), out_stream=out_stream)
            self.assertIsNone(stage)
            self.assertIn("Detected Python repository. Focused Scientific Python CI profile is available.", out_stream.getvalue())

    def test_prompt_python_ci_options_discovers_version_from_python_version_file(self):
        """prompt_python_ci_options discovers default matrix version from .python-version."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".python-version").write_text("3.10.8\n", encoding="utf-8")
            in_stream = io.StringIO("\n\n\n\n\n\n")
            out_stream = io.StringIO()
            opts = prompt_python_ci_options(
                root_dir=tmpdir,
                in_stream=in_stream,
                out_stream=out_stream,
            )
            self.assertEqual(opts["versions"], ["3.10"])

        # Also verify with Session.root_dir
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".python-version").write_text("3.9.1\n", encoding="utf-8")
            session = Session(repository="owner/repo", api=MagicMock())
            session.root_dir = tmpdir
            in_stream = io.StringIO("\n\n\n\n\n\n")
            opts = prompt_python_ci_options(
                session=session,
                in_stream=in_stream,
                out_stream=io.StringIO(),
            )
            self.assertEqual(opts["versions"], ["3.9"])

    def test_prompt_python_ci_options_preselects_git_lfs_when_filter_lfs_present(self):
        """prompt_python_ci_options preselects Git LFS if .gitattributes has filter=lfs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8")
            in_stream = io.StringIO("\n\n\n\n\n\n")
            opts = prompt_python_ci_options(
                root_dir=tmpdir,
                in_stream=in_stream,
                out_stream=io.StringIO(),
            )
            self.assertTrue(opts["lfs"])

        # And verify False when .gitattributes is absent or does not have filter=lfs
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".gitattributes").write_text("*.txt text\n", encoding="utf-8")
            in_stream = io.StringIO("\n\n\n\n\n\n")
            opts = prompt_python_ci_options(
                root_dir=tmpdir,
                in_stream=in_stream,
                out_stream=io.StringIO(),
            )
            self.assertFalse(opts["lfs"])

    def test_prompt_python_ci_options_discovers_package_manager_from_workspace(self):
        """prompt_python_ci_options discovers package manager uv when uv.lock is present."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "uv.lock").write_text("", encoding="utf-8")
            in_stream = io.StringIO("\n\n\n\n\n\n")
            opts = prompt_python_ci_options(
                root_dir=tmpdir,
                in_stream=in_stream,
                out_stream=io.StringIO(),
            )
            self.assertEqual(opts["package_manager"], "uv")


if __name__ == "__main__":
    unittest.main()
