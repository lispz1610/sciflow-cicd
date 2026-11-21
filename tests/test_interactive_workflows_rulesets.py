"""Tests for interactive workflows and rulesets stage."""

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.interactive import Session, run_interactive_workflow
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
        # Confirm: y, Self-hosted: default Yes, Labels: default self-hosted, Multichoice: 1 (Standard CI),
        # Commands: default, Triggers: default, Paths: default (none), Override runner: default No
        in_stream = io.StringIO("y\n\n\n1\n\n\n\n\n")
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
        self.assertEqual(wf["runs_on"], "self-hosted")

        # Validate with policy validator
        policy = {"version": 1, "stages": [stage]}
        self.assertEqual(validate_policy(policy), [stage])

    def test_user_selects_dependency_review_only(self):
        """When user selects dependency review, generates valid dependency-review workflow stage."""
        # Confirm: y, Self-hosted: default Yes, Labels: default self-hosted, Multichoice: 2 (Dependency review),
        # Severity: default high, Allow: default, Deny: default, Comment: default never
        in_stream = io.StringIO("y\n\n\n2\n\n\n\n\n")
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
        # Confirm: y, Self-hosted: default Yes, Labels: default self-hosted, Multichoice: 1, 2,
        # PR CI: Commands: default, Triggers: default, Paths: default (none), Override runner: default No
        # Dependency Review: Severity: default, Allow: default, Deny: default, Comment: default
        in_stream = io.StringIO("y\n\n\n1, 2\n\n\n\n\n\n\n\n\n")
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
        in_stream = io.StringIO("y\n\n\nnone\n")
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
        in_stream = io.StringIO("y\n\n\n\n\n\n\n\n")
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(session=session, in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        self.assertEqual(len(stage["workflows"]), 1)
        self.assertEqual(stage["workflows"][0]["template"], "dependency-review")

    def test_user_selects_scientific_python_ci_profile(self):
        """User selects Scientific Python CI profile and answers matrix and tool questions."""
        # Confirm: y
        # Self-hosted: default Yes
        # Labels: default self-hosted
        # Multichoice: 3 (Scientific Python CI)
        # Python versions: 3.10, 3.11, 3.12
        # Setup mode: default setup-python
        # Package manager: uv
        # Install commands: default none
        # Test commands: uv run pytest -v
        # Git LFS: y
        # Lint commands: flake8 .
        # Caching: y
        # Triggers: default pull_request
        in_stream = io.StringIO("y\n\n\n3\n3.10, 3.11, 3.12\n\nuv\n\nuv run pytest -v\ny\nflake8 .\ny\n\n")
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
        self.assertEqual(wf["python"]["python_setup_mode"], "setup-python")
        self.assertEqual(wf["python"]["package_manager"], "uv")
        self.assertEqual(wf["python"]["commands"], ["uv run pytest -v"])
        self.assertEqual(wf["python"]["lfs"], True)
        self.assertEqual(wf["python"]["lint_commands"], ["flake8 ."])
        self.assertEqual(wf["python"]["cache"], True)

    def test_user_selects_scientific_python_ci_profile_with_defaults(self):
        """User selects Scientific Python CI profile and accepts default answers."""
        # Confirm: y
        # Self-hosted: default Yes
        # Labels: default self-hosted
        # Multichoice: 3
        # Versions: enter (default 3.11, 3.12)
        # Setup mode: enter (default setup-python)
        # Package manager: enter (default pip)
        # Install commands: enter (none)
        # Commands: enter (default)
        # Git LFS: enter (default n)
        # Lint: enter (blank)
        # Caching: enter (default n when no manifest in workspace)
        # Triggers: enter (default pull_request)
        in_stream = io.StringIO("y\n\n\n3\n\n\n\n\n\n\n\n\n\n")
        out_stream = io.StringIO()

        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        wf = stage["workflows"][0]
        self.assertEqual(wf["template"], "profile-ci")
        self.assertEqual(wf["profile"], "python")
        self.assertEqual(wf["python"]["python_setup_mode"], "setup-python")
        self.assertEqual(wf["python"]["package_manager"], "pip")
        self.assertEqual(wf["python"]["lfs"], False)
        self.assertEqual(wf["python"]["cache"], False)
        self.assertIn("3.11", wf["python"]["versions"])

    def test_user_selects_python_ci_and_dependency_review(self):
        """User selects both Dependency review and Scientific Python CI profile."""
        in_stream = io.StringIO("y\n\n\n2, 3\n3.11, 3.12\n\npip\n\npytest\nn\n\ny\n\n\n\n\n\n\n")
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
        # Workflows confirm: y, Self-hosted: default Yes, Labels: default self-hosted, Multichoice: 1 (Standard CI),
        # Commands: default, Triggers: default, Paths: default (none), Override runner: default No
        # Gates confirm: y, PR: 15, Reviewer: charlie, Check: ci-check
        in_stream = io.StringIO("y\n\n\n1\n\n\n\n\ny\n15\ncharlie\nci-check\nn\nn\n")
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
        # Workflows confirm: y, Self-hosted: default Yes, Labels: default self-hosted, Multichoice: 1,
        # Commands: default, Triggers: default, Paths: default (none), Override runner: default No
        # Gates confirm: n
        in_stream = io.StringIO("y\n\n\n1\n\n\n\n\nn\nn\nn\n")
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
            in_stream = io.StringIO("\n" * 10)
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
            in_stream = io.StringIO("\n" * 10)
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
            in_stream = io.StringIO("\n" * 10)
            opts = prompt_python_ci_options(
                root_dir=tmpdir,
                in_stream=in_stream,
                out_stream=io.StringIO(),
            )
            self.assertTrue(opts["lfs"])

        # And verify False when .gitattributes is absent or does not have filter=lfs
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".gitattributes").write_text("*.txt text\n", encoding="utf-8")
            in_stream = io.StringIO("\n" * 10)
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
            in_stream = io.StringIO("\n" * 10)
            opts = prompt_python_ci_options(
                root_dir=tmpdir,
                in_stream=in_stream,
                out_stream=io.StringIO(),
            )
            self.assertEqual(opts["package_manager"], "uv")

    def test_prompt_python_ci_options_manifest_guard_defaults_cache_false_without_manifest(self):
        """When no manifest (requirements.txt, pyproject.toml, setup.py) exists, cache defaults to False with advisory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            in_stream = io.StringIO("\n" * 10)
            out_stream = io.StringIO()
            opts = prompt_python_ci_options(
                root_dir=tmpdir,
                in_stream=in_stream,
                out_stream=out_stream,
            )
            self.assertFalse(opts["cache"])
            output = out_stream.getvalue()
            self.assertIn("No dependency manifest", output)
            self.assertIn("caching disabled by default", output)

    def test_prompt_python_ci_options_manifest_guard_defaults_cache_true_with_manifest(self):
        """When manifest (pyproject.toml) exists, cache defaults to True."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "pyproject.toml").write_text("[project]\nname = 'pkg'\n", encoding="utf-8")
            in_stream = io.StringIO("\n" * 10)
            out_stream = io.StringIO()
            opts = prompt_python_ci_options(
                root_dir=tmpdir,
                in_stream=in_stream,
                out_stream=out_stream,
            )
            self.assertTrue(opts["cache"])
            self.assertNotIn("No dependency manifest", out_stream.getvalue())

    def test_prompt_python_ci_options_prompts_setup_mode_and_install_commands(self):
        """Prompt accepts custom python_setup_mode ('system') and optional install_commands."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "pyproject.toml").write_text("[project]\nname = 'pkg'\n", encoding="utf-8")
            # Prompts:
            # 1. versions: default (enter)
            # 2. setup mode: system
            # 3. package manager: pip
            # 4. install commands: pip install -e .
            # 5. test commands: pytest
            # 6. git lfs: n
            # 7. lint: flake8
            # 8. cache: default (enter)
            in_stream = io.StringIO("\nsystem\npip\npip install -e .\npytest\nn\nflake8\n\n")
            out_stream = io.StringIO()
            opts = prompt_python_ci_options(
                root_dir=tmpdir,
                in_stream=in_stream,
                out_stream=out_stream,
            )
            self.assertEqual(opts["python_setup_mode"], "system")
            self.assertEqual(opts["install_commands"], ["pip install -e ."])
            self.assertEqual(opts["commands"], ["pytest"])
            self.assertEqual(opts["lint_commands"], ["flake8"])

    def test_prompt_workflows_stage_prompts_post_merge_when_push_selected(self):
        """When user selects push trigger for Python CI, prompts for post_merge_commands."""
        # 1. Stage workflows? y
        # 2. Self-hosted default? (enter -> y)
        # 3. Runner labels? (enter -> self-hosted)
        # 4. Multichoice: 3 (Scientific Python CI)
        # 5. Versions: (enter)
        # 6. Setup mode: (enter -> setup-python)
        # 7. Package manager: (enter -> pip)
        # 8. Install commands: (enter -> none)
        # 9. Test commands: (enter)
        # 10. Git LFS: (enter -> n)
        # 11. Lint: (enter -> none)
        # 12. Cache: (enter)
        # 13. Workflow triggers: pull_request, push
        # 14. Post-merge commands: python deploy.py
        in_stream = io.StringIO(
            "y\n\n\n3\n\n\n\n\n\n\n\n\npull_request, push\npython deploy.py\n"
        )
        out_stream = io.StringIO()
        stage = prompt_workflows_stage(in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        wf = stage["workflows"][0]
        self.assertEqual(wf["triggers"], ["pull_request", "push"])
        self.assertEqual(wf["post_merge_commands"], ["python deploy.py"])


class InteractiveDependabotAndResumeWorkflowTests(unittest.TestCase):
    """Tests for Dependabot prompt integration in run_interactive_workflow and resume mode workflows."""

    @patch("scripts.interactive.run_interactive_apply", return_value=0)
    @patch("scripts.interactive.assemble_and_persist_policy")
    @patch("scripts.interactive.prompt_workflows_rulesets_stage", return_value=[])
    @patch("scripts.interactive.prompt_content_stages", return_value=[])
    @patch("scripts.interactive.prompt_actions_security_stage", return_value=[])
    @patch("scripts.interactive.prompt_merge_stage", return_value=None)
    @patch("scripts.interactive.prompt_features_stage")
    def test_run_interactive_workflow_prompts_dependabot_and_includes_stage(
        self,
        mock_features,
        mock_merge,
        mock_actions,
        mock_content,
        mock_wf_rules,
        mock_assemble,
        mock_apply,
    ):
        mock_features.return_value = {"name": "features", "enable": ["issues"]}
        mock_assemble.return_value = ({"version": 2, "repository": "owner/repo", "stages": []}, Path("dummy"))

        in_stream = io.StringIO("y\n")  # confirm dependabot
        out_stream = io.StringIO()
        mock_api = MagicMock()

        session = Session(
            repository="owner/repo",
            api=mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
            defaults={},
        )

        exit_code = run_interactive_workflow(session)
        self.assertEqual(exit_code, 0)

        mock_assemble.assert_called_once()
        _, stages_arg = mock_assemble.call_args[0]
        stage_names = [s.get("name") for s in stages_arg if isinstance(s, dict)]
        self.assertIn("features", stage_names)
        self.assertIn("dependabot", stage_names)

        dep_stage = next(s for s in stages_arg if s.get("name") == "dependabot")
        self.assertEqual(dep_stage["cadence"], "monthly")
        self.assertTrue(dep_stage["groups"])

    @patch("scripts.interactive.run_interactive_apply", return_value=0)
    @patch("scripts.interactive.assemble_and_persist_policy")
    @patch("scripts.interactive.prompt_workflows_rulesets_stage", return_value=[])
    @patch("scripts.interactive.prompt_content_stages", return_value=[])
    @patch("scripts.interactive.prompt_actions_security_stage", return_value=[])
    @patch("scripts.interactive.prompt_merge_stage", return_value=None)
    @patch("scripts.interactive.prompt_features_stage")
    def test_run_interactive_workflow_declining_dependabot_omits_stage(
        self,
        mock_features,
        mock_merge,
        mock_actions,
        mock_content,
        mock_wf_rules,
        mock_assemble,
        mock_apply,
    ):
        mock_features.return_value = {"name": "features", "enable": ["issues"]}
        mock_assemble.return_value = ({"version": 2, "repository": "owner/repo", "stages": []}, Path("dummy"))

        in_stream = io.StringIO("n\n")  # decline dependabot
        out_stream = io.StringIO()
        mock_api = MagicMock()

        session = Session(
            repository="owner/repo",
            api=mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=False,
            defaults={},
        )

        exit_code = run_interactive_workflow(session)
        self.assertEqual(exit_code, 0)

        mock_assemble.assert_called_once()
        _, stages_arg = mock_assemble.call_args[0]
        stage_names = [s.get("name") for s in stages_arg if isinstance(s, dict)]
        self.assertIn("features", stage_names)
        self.assertNotIn("dependabot", stage_names)

    @patch("scripts.interactive.run_interactive_apply", return_value=0)
    @patch("scripts.interactive.assemble_and_persist_policy")
    @patch("scripts.interactive.prompt_workflows_rulesets_stage", return_value=[])
    @patch("scripts.interactive.prompt_content_stages", return_value=[])
    @patch("scripts.interactive.prompt_actions_security_stage", return_value=[])
    @patch("scripts.interactive.prompt_merge_stage", return_value=None)
    @patch("scripts.interactive.prompt_features_stage")
    def test_run_interactive_workflow_resume_mode_defaults_dependabot(
        self,
        mock_features,
        mock_merge,
        mock_actions,
        mock_content,
        mock_wf_rules,
        mock_assemble,
        mock_apply,
    ):
        mock_features.return_value = {"name": "features", "enable": ["issues"]}
        mock_assemble.return_value = ({"version": 2, "repository": "owner/repo", "stages": []}, Path("dummy"))

        in_stream = io.StringIO("\n")  # accept default=True
        out_stream = io.StringIO()
        mock_api = MagicMock()

        session = Session(
            repository="owner/repo",
            api=mock_api,
            in_stream=in_stream,
            out_stream=out_stream,
            resume_mode=True,
            defaults={
                "dependabot": {
                    "cadence": "weekly",
                    "package_ecosystems": ["pip", "github-actions"],
                    "open_pull_requests_limit": 8,
                    "groups": True,
                }
            },
        )

        exit_code = run_interactive_workflow(session)
        self.assertEqual(exit_code, 0)

        output = out_stream.getvalue()
        self.assertIn("Recommended default: Yes", output)

        mock_assemble.assert_called_once()
        _, stages_arg = mock_assemble.call_args[0]
        dep_stage = next(s for s in stages_arg if s.get("name") == "dependabot")
        self.assertEqual(dep_stage["cadence"], "weekly")
        self.assertEqual(dep_stage["open_pull_requests_limit"], 8)

    def test_resume_mode_prepopulates_workflow_defaults_and_dependabot(self):
        """Re-running interactive setup in resume mode pre-populates all workflow defaults and dependabot."""
        ci_sha = "a" * 40
        dep_sha = "b" * 40
        inventory_data = {
            "managed_workflows": [
                {
                    "template": "profile-ci",
                    "profile": "python",
                    "filename": "ci.yml",
                    "expected_sha": ci_sha,
                    "runs_on": "self-hosted",
                    "triggers": ["pull_request", "push"],
                    "versions": ["3.11", "3.12"],
                    "python_setup_mode": "uv",
                    "package_manager": "uv",
                    "install_commands": ["uv pip install -r requirements.txt"],
                    "commands": ["pytest"],
                    "cache": True,
                    "lfs": True,
                    "lint_commands": ["ruff check ."],
                },
                {
                    "template": "dependency-review",
                    "filename": "dependency-review.yml",
                    "expected_sha": dep_sha,
                    "runs_on": "ubuntu-latest",
                    "fail_on_severity": "critical",
                    "allow_licenses": "MIT",
                    "deny_licenses": "GPL-3.0",
                    "comment_summary_in_pr": "always",
                },
            ],
            "dependabot": {
                "status": "managed",
                "cadence": "weekly",
                "package_ecosystems": ["pip", "github-actions"],
                "open_pull_requests_limit": 3,
                "groups": True,
            },
        }

        mock_api = MagicMock()
        session = Session(
            repository="owner/repo",
            api=mock_api,
            resume_mode=True,
            inventory=inventory_data,
            defaults=extract_defaults_from_inventory(inventory_data),
        )

        self.assertIn("workflows", session.defaults)
        self.assertEqual(len(session.defaults["workflows"]), 2)
        self.assertEqual(session.defaults["python"]["runs_on"], "self-hosted")
        self.assertEqual(session.defaults["python"]["package_manager"], "uv")
        self.assertEqual(session.defaults["python"]["python_setup_mode"], "uv")
        self.assertEqual(session.defaults["python"]["commands"], ["pytest"])
        self.assertEqual(session.defaults["dependency_review"]["fail_on_severity"], "critical")
        self.assertEqual(session.defaults["dependabot"]["cadence"], "weekly")

        in_stream = io.StringIO("\n" * 25)
        out_stream = io.StringIO()
        session.in_stream = in_stream
        session.out_stream = out_stream

        stage = prompt_workflows_stage(session=session, in_stream=in_stream, out_stream=out_stream)
        self.assertIsNotNone(stage)
        templates = [wf.get("template") for wf in stage.get("workflows", [])]
        self.assertIn("profile-ci", templates)
        self.assertIn("dependency-review", templates)

        py_wf = next(wf for wf in stage["workflows"] if wf.get("template") == "profile-ci")
        self.assertEqual(py_wf["runs_on"], "self-hosted")
        self.assertEqual(py_wf["python"]["package_manager"], "uv")
        self.assertEqual(py_wf["python"]["python_setup_mode"], "uv")

    def test_resume_workflow_prefill_without_pyyaml(self):
        ci_yaml = """# Managed by sciflow-cicd
name: Python CI
on:
  pull_request:
jobs:
  ci:
    runs-on: self-hosted
    strategy:
      matrix:
        python-version: ["3.12"]
    steps:
      - uses: actions/checkout@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
      - run: "pytest -q"
"""
        dep_yaml = """# Dependabot configuration generated by sciflow-cicd
version: 2
updates:
  - package-ecosystem: "pip"
    schedule:
      interval: "weekly"
    open-pull-requests-limit: 9
    groups:
      python-dependencies:
        patterns: ["*"]
"""
        inventory_data = {
            "workflow_files": {
                ".github/workflows/ci.yml": ci_yaml,
                ".github/dependabot.yml": dep_yaml,
            }
        }
        with patch("scripts.setup_github_repo.yaml", None):
            defaults = extract_defaults_from_inventory(inventory_data)

        session = Session(
            repository="owner/repo",
            api=MagicMock(),
            resume_mode=True,
            defaults=defaults,
        )
        in_stream = io.StringIO("\n" * 25)
        out_stream = io.StringIO()
        stage = prompt_workflows_stage(session=session, in_stream=in_stream, out_stream=out_stream)

        self.assertIsNotNone(stage)
        py_wf = next(wf for wf in stage["workflows"] if wf.get("template") == "profile-ci")
        self.assertEqual(py_wf["python"]["versions"], ["3.12"])
        self.assertEqual(py_wf["python"]["commands"], ["pytest -q"])
        self.assertEqual(session.defaults["dependabot"]["open_pull_requests_limit"], 9)
        self.assertEqual(session.defaults["dependabot"]["cadence"], "weekly")
        self.assertTrue(session.defaults["dependabot"]["groups"])


if __name__ == "__main__":
    unittest.main()
