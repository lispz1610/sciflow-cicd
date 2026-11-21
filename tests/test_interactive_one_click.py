"""Tests for 1-click interactive setup session ([Y/n/c/o]) and compact banners."""

from __future__ import annotations

import io
from pathlib import Path
import sys
import tempfile
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
from test_interactive_audit import AuditMockApi


class OneClickMockApi(AuditMockApi):
    """Mock API supporting actions commits, workflows, and label preflight."""

    def __init__(self, repo_name="acme/project"):
        super().__init__(repo_name=repo_name)
        self.actions["enabled"] = True

    def request(self, method, path, payload=None):
        if method == "GET":
            if "/labels/" in path:
                return 404, None
        return super().request(method, path, payload)
from scripts.interactive import (
    Session,
    format_profile_banner,
    format_catalog_banner,
    prompt_catalog_selection,
    prompt_target_repository,
    prompt_resume_choice,
    run_interactive_workflow,
)
from scripts.setup_profiles import (
    list_profiles,
    get_profile,
    SCIENTIFIC_PYTHON,
    HPC_CMAKE_MPI,
    MINIMAL_LEAN,
)
from scripts.prompts import PromptExit


class InteractiveOneClickBannerTests(unittest.TestCase):
    """Verify that guidance banners directly preceding prompts do not exceed 2 lines."""

    def test_recommended_profile_banner_at_most_two_lines(self):
        banner = format_profile_banner(SCIENTIFIC_PYTHON, is_recommended=True)
        lines = [line for line in banner.splitlines() if line.strip()]
        self.assertLessEqual(len(lines), 2, f"Banner has {len(lines)} lines; expected at most 2: {banner!r}")
        self.assertIn("Recommended Setup Profile:", lines[0])
        self.assertIn("Scientific Python", lines[0])

    def test_active_profile_banner_after_switch_at_most_two_lines(self):
        banner = format_profile_banner(HPC_CMAKE_MPI, is_recommended=False)
        lines = [line for line in banner.splitlines() if line.strip()]
        self.assertLessEqual(len(lines), 2, f"Banner has {len(lines)} lines; expected at most 2: {banner!r}")
        self.assertIn("Active Setup Profile:", lines[0])
        self.assertIn("HPC C++ / MPI", lines[0])

    def test_catalog_banner_at_most_two_lines(self):
        banner = format_catalog_banner()
        lines = [line for line in banner.splitlines() if line.strip()]
        self.assertLessEqual(len(lines), 2, f"Banner has {len(lines)} lines; expected at most 2: {banner!r}")

    def test_target_repository_banner_at_most_two_lines(self):
        out_stream = io.StringIO()
        in_stream = io.StringIO("octocat/Hello-World\n")
        prompt_target_repository(in_stream=in_stream, out_stream=out_stream)
        output = out_stream.getvalue()
        # Extract pre-prompt banner before prompt text
        banner_part = output.split("Target repository (OWNER/REPO)")[0]
        lines = [line for line in banner_part.splitlines() if line.strip()]
        self.assertLessEqual(len(lines), 2, f"Target repo banner has {len(lines)} lines; expected <= 2: {banner_part!r}")
        self.assertIn("Inventory only reads", banner_part)
        self.assertIn("repository permissions", banner_part)

    def test_resume_choice_banner_at_most_two_lines(self):
        out_stream = io.StringIO()
        in_stream = io.StringIO("1\n")
        prompt_resume_choice(in_stream=in_stream, out_stream=out_stream)
        output = out_stream.getvalue()
        banner_part = output.split("Existing repository configuration detected")[0]
        lines = [line for line in banner_part.splitlines() if line.strip()]
        self.assertLessEqual(len(lines), 2, f"Resume banner has {len(lines)} lines; expected <= 2: {banner_part!r}")


class InteractiveOneClickFlowTests(unittest.TestCase):
    """Verify 1-click acceptance [Y], cancellation [n], catalog switching [o], and EOF/SIGINT."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo_dir = Path(self.temp_dir.name)

    def test_one_click_acceptance_with_capital_y(self):
        """Entering Y immediately resolves policy, runs dry-run, and prompts apply confirmation."""
        api = OneClickMockApi("acme/project")
        # pyproject.toml marker -> Scientific Python
        (self.repo_dir / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")

        # 1-click prompt: Y (accept recommended profile)
        # apply confirmation prompt: n (cancel apply)
        inputs = "Y\nn\n"
        in_stream = io.StringIO(inputs)
        out_stream = io.StringIO()

        session = Session(
            repository="acme/project",
            api=api,
            repo_dir=self.repo_dir,
            in_stream=in_stream,
            out_stream=out_stream,
        )

        exit_code = run_interactive_workflow(session)
        self.assertEqual(exit_code, 0)
        output = out_stream.getvalue()
        self.assertIn("Recommended Setup Profile: Scientific Python", output)
        self.assertIn("Dry-Run Summary", output)
        self.assertIn("Apply cancelled", output)

    def test_one_click_acceptance_with_empty_enter(self):
        """Pressing empty Enter defaults to uppercase Y (accelerated acceptance)."""
        api = OneClickMockApi("acme/project")
        (self.repo_dir / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")

        # 1-click prompt: empty Enter -> Y
        # apply confirmation prompt: n
        inputs = "\nn\n"
        in_stream = io.StringIO(inputs)
        out_stream = io.StringIO()

        session = Session(
            repository="acme/project",
            api=api,
            repo_dir=self.repo_dir,
            in_stream=in_stream,
            out_stream=out_stream,
        )

        exit_code = run_interactive_workflow(session)
        self.assertEqual(exit_code, 0)
        output = out_stream.getvalue()
        self.assertIn("Recommended Setup Profile: Scientific Python", output)
        self.assertIn("Dry-Run Summary", output)

    def test_cancellation_with_n_exits_cleanly(self):
        """Entering n aborts the session cleanly without writes or stack trace."""
        api = InventoryApi()
        inputs = "n\n"
        in_stream = io.StringIO(inputs)
        out_stream = io.StringIO()

        session = Session(
            repository="acme/project",
            api=api,
            repo_dir=self.repo_dir,
            in_stream=in_stream,
            out_stream=out_stream,
        )

        exit_code = run_interactive_workflow(session)
        self.assertEqual(exit_code, 0)
        self.assertEqual(api.writes, [])
        output = out_stream.getvalue()
        self.assertIn("Setup cancelled", output)
        self.assertNotIn("Traceback", output)

    def test_immediate_eof_exits_cleanly(self):
        """Immediate EOF on prompt aborts cleanly without leaking raw stack trace."""
        api = InventoryApi()
        inputs = ""
        in_stream = io.StringIO(inputs)
        out_stream = io.StringIO()

        session = Session(
            repository="acme/project",
            api=api,
            repo_dir=self.repo_dir,
            in_stream=in_stream,
            out_stream=out_stream,
        )

        exit_code = run_interactive_workflow(session)
        self.assertEqual(exit_code, 0)
        output = out_stream.getvalue()
        self.assertIn("Setup cancelled", output)
        self.assertNotIn("Traceback", output)

    def test_profile_switching_with_o_updates_active_profile(self):
        """Entering 'o' displays catalog of 6 profiles and allows selecting an alternative archetype."""
        api = OneClickMockApi("acme/project")
        # repo has pyproject.toml -> recommends Scientific Python
        (self.repo_dir / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")

        # 1-click prompt: o (other profiles)
        # catalog selection: 2 (HPC C++ / MPI)
        # next 1-click prompt: Y (accept new active profile)
        # apply confirmation prompt: n (cancel apply)
        inputs = "o\n2\nY\nn\n"
        in_stream = io.StringIO(inputs)
        out_stream = io.StringIO()

        session = Session(
            repository="acme/project",
            api=api,
            repo_dir=self.repo_dir,
            in_stream=in_stream,
            out_stream=out_stream,
        )

        exit_code = run_interactive_workflow(session)
        self.assertEqual(exit_code, 0)
        output = out_stream.getvalue()
        self.assertIn("Recommended Setup Profile: Scientific Python", output)
        self.assertIn("Setup Profile Catalog", output)
        self.assertIn("HPC C++ / MPI", output)
        self.assertIn("Active Setup Profile: HPC C++ / MPI", output)
        self.assertIn("Dry-Run Summary", output)

    def test_catalog_selection_helper_displays_all_six_profiles(self):
        """prompt_catalog_selection presents all 6 canonical profiles."""
        out_stream = io.StringIO()
        in_stream = io.StringIO("6\n")

        selected = prompt_catalog_selection(
            in_stream=in_stream,
            out_stream=out_stream,
            current_profile_id="scientific-python",
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, "minimal-lean")
        output = out_stream.getvalue()
        for p in list_profiles():
            self.assertIn(p.display_name, output)

    def test_customization_with_c_routes_to_customization_handler(self):
        """Entering 'c' routes into the customization handler."""
        api = InventoryApi()
        # 1-click: c (customize)
        # customization handler (sequential questionnaire fallback for ticket 53):
        # features: 1 (Issues)
        # merge: n
        # actions: y
        # security: 1
        # content: n
        # workflows: n
        # gates: n
        # apply confirm: n
        inputs = "c\n1\nn\ny\n1\nn\nn\nn\nn\nn\n"
        in_stream = io.StringIO(inputs)
        out_stream = io.StringIO()

        session = Session(
            repository="acme/project",
            api=api,
            repo_dir=self.repo_dir,
            in_stream=in_stream,
            out_stream=out_stream,
        )

        exit_code = run_interactive_workflow(session)
        self.assertEqual(exit_code, 0)
        output = out_stream.getvalue()
        self.assertIn("Dry-Run Summary", output)


if __name__ == "__main__":
    unittest.main()
