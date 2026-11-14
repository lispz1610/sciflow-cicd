"""Unit tests for terminal prompt utilities."""

import io
import os
from pathlib import Path
import sys
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(ROOT_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "scripts"))

import prompts


class TestPromptText(unittest.TestCase):
    def test_prompt_text_basic(self):
        in_stream = io.StringIO("my-repo\n")
        out_stream = io.StringIO()
        result = prompts.prompt_text("Enter repo", in_stream=in_stream, out_stream=out_stream)
        self.assertEqual(result, "my-repo")
        self.assertIn("Enter repo: ", out_stream.getvalue())

    def test_prompt_text_default_value(self):
        in_stream = io.StringIO("\n")
        out_stream = io.StringIO()
        result = prompts.prompt_text("Enter branch", default="main", in_stream=in_stream, out_stream=out_stream)
        self.assertEqual(result, "main")
        self.assertIn("Enter branch [main]: ", out_stream.getvalue())

    def test_prompt_text_custom_with_default(self):
        in_stream = io.StringIO("develop\n")
        out_stream = io.StringIO()
        result = prompts.prompt_text("Enter branch", default="main", in_stream=in_stream, out_stream=out_stream)
        self.assertEqual(result, "develop")

    def test_prompt_text_required_reprompts_until_non_empty(self):
        in_stream = io.StringIO("\n   \nowner/repo\n")
        out_stream = io.StringIO()
        result = prompts.prompt_text("Repository", required=True, in_stream=in_stream, out_stream=out_stream)
        self.assertEqual(result, "owner/repo")
        output = out_stream.getvalue()
        self.assertIn("required", output.lower())
        self.assertEqual(output.count("Repository: "), 3)

    def test_prompt_text_eof_returns_none(self):
        in_stream = io.StringIO("")  # Immediate EOF
        out_stream = io.StringIO()
        result = prompts.prompt_text("Enter name", in_stream=in_stream, out_stream=out_stream)
        self.assertIsNone(result)


class TestPromptChoice(unittest.TestCase):
    def test_prompt_choice_valid_selection(self):
        options = ["Public", "Private", "Internal"]
        in_stream = io.StringIO("2\n")
        out_stream = io.StringIO()
        result = prompts.prompt_choice("Choose visibility", options, in_stream=in_stream, out_stream=out_stream)
        self.assertEqual(result, 1)  # Index 1 corresponds to "Private"
        output = out_stream.getvalue()
        self.assertIn("1. Public", output)
        self.assertIn("2. Private", output)
        self.assertIn("3. Internal", output)

    def test_prompt_choice_default_selection(self):
        options = ["Public", "Private", "Internal"]
        in_stream = io.StringIO("\n")
        out_stream = io.StringIO()
        result = prompts.prompt_choice("Choose visibility", options, default=0, in_stream=in_stream, out_stream=out_stream)
        self.assertEqual(result, 0)  # Default is index 0 ("Public")
        self.assertIn("[1]", out_stream.getvalue())

    def test_prompt_choice_reprompts_on_invalid_and_out_of_range(self):
        options = ["Public", "Private"]
        in_stream = io.StringIO("abc\n0\n5\n1\n")
        out_stream = io.StringIO()
        result = prompts.prompt_choice("Choose visibility", options, in_stream=in_stream, out_stream=out_stream)
        self.assertEqual(result, 0)
        output = out_stream.getvalue()
        self.assertIn("Invalid selection", output)

    def test_prompt_choice_empty_options_raises_value_error(self):
        in_stream = io.StringIO("1\n")
        out_stream = io.StringIO()
        with self.assertRaises(ValueError):
            prompts.prompt_choice("Empty", [], in_stream=in_stream, out_stream=out_stream)

    def test_prompt_choice_eof_returns_none(self):
        options = ["Public", "Private"]
        in_stream = io.StringIO("")
        out_stream = io.StringIO()
        result = prompts.prompt_choice("Choose visibility", options, in_stream=in_stream, out_stream=out_stream)
        self.assertIsNone(result)


class TestPromptMultiChoice(unittest.TestCase):
    def test_prompt_multichoice_selection_returns_items(self):
        options = ["Issues", "Wiki", "Projects", "Discussions"]
        in_stream = io.StringIO("1, 3\n")
        out_stream = io.StringIO()
        result = prompts.prompt_multichoice("Enable features", options, in_stream=in_stream, out_stream=out_stream)
        self.assertEqual(result, ["Issues", "Projects"])
        output = out_stream.getvalue()
        self.assertIn("1. Issues", output)
        self.assertIn("2. Wiki", output)
        self.assertIn("3. Projects", output)

    def test_prompt_multichoice_return_indices(self):
        options = ["Issues", "Wiki", "Projects"]
        in_stream = io.StringIO("2, 3\n")
        out_stream = io.StringIO()
        result = prompts.prompt_multichoice(
            "Enable features", options, return_indices=True, in_stream=in_stream, out_stream=out_stream
        )
        self.assertEqual(result, [1, 2])

    def test_prompt_multichoice_preselected_display_and_default(self):
        options = ["Issues", "Wiki", "Projects"]
        # Preselect Issues (index 0) and Projects (index 2)
        in_stream = io.StringIO("\n")
        out_stream = io.StringIO()
        result = prompts.prompt_multichoice(
            "Enable features",
            options,
            default=[0, 2],
            in_stream=in_stream,
            out_stream=out_stream,
        )
        self.assertEqual(result, ["Issues", "Projects"])
        output = out_stream.getvalue()
        self.assertIn("[x] 1. Issues", output)
        self.assertIn("[ ] 2. Wiki", output)
        self.assertIn("[x] 3. Projects", output)

    def test_prompt_multichoice_preselected_by_name(self):
        options = ["Issues", "Wiki", "Projects"]
        in_stream = io.StringIO("\n")
        out_stream = io.StringIO()
        result = prompts.prompt_multichoice(
            "Enable features",
            options,
            default=["Wiki"],
            in_stream=in_stream,
            out_stream=out_stream,
        )
        self.assertEqual(result, ["Wiki"])
        output = out_stream.getvalue()
        self.assertIn("[ ] 1. Issues", output)
        self.assertIn("[x] 2. Wiki", output)

    def test_prompt_multichoice_override_preselection(self):
        options = ["Issues", "Wiki", "Projects"]
        in_stream = io.StringIO("2\n")
        out_stream = io.StringIO()
        result = prompts.prompt_multichoice(
            "Enable features",
            options,
            default=[0, 2],
            in_stream=in_stream,
            out_stream=out_stream,
        )
        self.assertEqual(result, ["Wiki"])

    def test_prompt_multichoice_reprompts_on_invalid_input(self):
        options = ["Issues", "Wiki", "Projects"]
        in_stream = io.StringIO("1, 99\ninvalid\n1, 2\n")
        out_stream = io.StringIO()
        result = prompts.prompt_multichoice("Enable features", options, in_stream=in_stream, out_stream=out_stream)
        self.assertEqual(result, ["Issues", "Wiki"])
        output = out_stream.getvalue()
        self.assertIn("Invalid selection", output)

    def test_prompt_multichoice_empty_options_raises_value_error(self):
        with self.assertRaises(ValueError):
            prompts.prompt_multichoice("Empty", [])

    def test_prompt_multichoice_eof_returns_none(self):
        options = ["Issues", "Wiki"]
        in_stream = io.StringIO("")
        out_stream = io.StringIO()
        result = prompts.prompt_multichoice("Enable features", options, in_stream=in_stream, out_stream=out_stream)
        self.assertIsNone(result)


class TestPromptConfirm(unittest.TestCase):
    def test_prompt_confirm_yes(self):
        for val in ["y\n", "Y\n", "yes\n", "YES\n"]:
            in_stream = io.StringIO(val)
            out_stream = io.StringIO()
            res = prompts.prompt_confirm("Continue?", in_stream=in_stream, out_stream=out_stream)
            self.assertTrue(res)

    def test_prompt_confirm_no(self):
        for val in ["n\n", "N\n", "no\n", "NO\n"]:
            in_stream = io.StringIO(val)
            out_stream = io.StringIO()
            res = prompts.prompt_confirm("Continue?", in_stream=in_stream, out_stream=out_stream)
            self.assertFalse(res)

    def test_prompt_confirm_default_false(self):
        in_stream = io.StringIO("\n")
        out_stream = io.StringIO()
        res = prompts.prompt_confirm("Continue?", default=False, in_stream=in_stream, out_stream=out_stream)
        self.assertFalse(res)
        self.assertIn("[y/N]: ", out_stream.getvalue())

    def test_prompt_confirm_default_true(self):
        in_stream = io.StringIO("\n")
        out_stream = io.StringIO()
        res = prompts.prompt_confirm("Continue?", default=True, in_stream=in_stream, out_stream=out_stream)
        self.assertTrue(res)
        self.assertIn("[Y/n]: ", out_stream.getvalue())

    def test_prompt_confirm_reprompts_on_invalid(self):
        in_stream = io.StringIO("maybe\ny\n")
        out_stream = io.StringIO()
        res = prompts.prompt_confirm("Continue?", in_stream=in_stream, out_stream=out_stream)
        self.assertTrue(res)
        self.assertIn("Please enter 'y' or 'n'", out_stream.getvalue())

    def test_prompt_confirm_eof_returns_none(self):
        in_stream = io.StringIO("")
        out_stream = io.StringIO()
        res = prompts.prompt_confirm("Continue?", in_stream=in_stream, out_stream=out_stream)
        self.assertIsNone(res)


class MockInterruptStream:
    """Mock input stream that raises KeyboardInterrupt on readline."""
    def readline(self):
        raise KeyboardInterrupt()


class TestCleanTermination(unittest.TestCase):
    def test_keyboard_interrupt_caught_cleanly_returns_none(self):
        in_stream = MockInterruptStream()
        out_stream = io.StringIO()

        self.assertIsNone(prompts.prompt_text("Name", in_stream=in_stream, out_stream=out_stream))
        self.assertIsNone(prompts.prompt_choice("Choice", ["A", "B"], in_stream=in_stream, out_stream=out_stream))
        self.assertIsNone(prompts.prompt_multichoice("Multi", ["A", "B"], in_stream=in_stream, out_stream=out_stream))
        self.assertIsNone(prompts.prompt_confirm("Confirm", in_stream=in_stream, out_stream=out_stream))

    def test_raise_on_interrupt_raises_prompt_exit(self):
        in_stream = MockInterruptStream()
        out_stream = io.StringIO()

        with self.assertRaises(prompts.PromptExit):
            prompts.prompt_text("Name", raise_on_interrupt=True, in_stream=in_stream, out_stream=out_stream)
        with self.assertRaises(prompts.PromptExit):
            prompts.prompt_choice("Choice", ["A", "B"], raise_on_interrupt=True, in_stream=in_stream, out_stream=out_stream)
        with self.assertRaises(prompts.PromptExit):
            prompts.prompt_multichoice("Multi", ["A", "B"], raise_on_interrupt=True, in_stream=in_stream, out_stream=out_stream)
        with self.assertRaises(prompts.PromptExit):
            prompts.prompt_confirm("Confirm", raise_on_interrupt=True, in_stream=in_stream, out_stream=out_stream)


if __name__ == "__main__":
    unittest.main()
