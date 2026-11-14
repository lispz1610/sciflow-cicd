"""Behavior tests for the repository's relative Markdown link audit."""

import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_markdown_links.py"


def load_command():
    spec = importlib.util.spec_from_file_location("check_markdown_links", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MarkdownLinkAuditTests(unittest.TestCase):
    def test_valid_relative_file_and_heading_links_pass(self):
        command = load_command()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "README.md").write_text(
                "# Start Here\n\n[Guide](docs/guide.md#quick-start)\n", encoding="utf-8"
            )
            (root / "docs").mkdir()
            (root / "docs/guide.md").write_text(
                "# Quick Start\n\n![Diagram](diagram.svg#rendered)\n", encoding="utf-8"
            )
            (root / "docs/diagram.svg").write_text("<svg />", encoding="utf-8")

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(command.main(["--root", str(root)]), 0)

            self.assertIn("0 broken links", output.getvalue())

    def test_missing_file_and_markdown_anchor_fail_with_locations(self):
        command = load_command()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "README.md").write_text(
                "# Home\n\n[Missing](absent.md)\n[Bad anchor](guide.md#no-such-heading)\n",
                encoding="utf-8",
            )
            (root / "guide.md").write_text("# Existing Heading\n", encoding="utf-8")

            errors = io.StringIO()
            with redirect_stderr(errors):
                self.assertEqual(command.main(["--root", str(root)]), 1)

            self.assertIn("README.md:3", errors.getvalue())
            self.assertIn("README.md:4", errors.getvalue())
            self.assertIn("absent.md", errors.getvalue())
            self.assertIn("#no-such-heading", errors.getvalue())

    def test_reference_links_resolve_defined_destinations_and_anchors(self):
        command = load_command()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "README.md").write_text(
                "[Guide][target]\n\n[target]: docs/guide.md#quick-start\n"
                '<a href="docs/guide.md#quick-start">Quick start</a>\n',
                encoding="utf-8",
            )
            (root / "docs").mkdir()
            (root / "docs/guide.md").write_text("# Quick Start\n", encoding="utf-8")

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(command.main(["--root", str(root)]), 0)

            self.assertIn("0 broken links", output.getvalue())

    def test_broken_reference_targets_and_html_href_anchors_are_reported(self):
        command = load_command()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "README.md").write_text(
                "[Missing][target]\n\n[target]: docs/absent.md\n"
                "[Missing collapsed][]\n\n[Missing collapsed]: docs/collapsed-absent.md\n"
                '<a href="docs/guide.md#missing-anchor">Guide</a>\n',
                encoding="utf-8",
            )
            (root / "docs").mkdir()
            (root / "docs/guide.md").write_text("# Existing Heading\n", encoding="utf-8")

            errors = io.StringIO()
            with redirect_stderr(errors):
                self.assertEqual(command.main(["--root", str(root)]), 1)

            self.assertIn("docs/absent.md", errors.getvalue())
            self.assertIn("docs/collapsed-absent.md", errors.getvalue())
            self.assertIn("docs/guide.md#missing-anchor", errors.getvalue())
            self.assertIn("Markdown anchor does not exist", errors.getvalue())

    def test_external_links_code_fences_and_asset_fragments_are_ignored(self):
        command = load_command()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "README.md").write_text(
                """# Home

[External](https://example.com/no-file#no-heading)

```md
[Example](missing.md#missing)
```

[Asset](image.png#not-an-html-anchor)
""",
                encoding="utf-8",
            )
            (root / "image.png").write_bytes(b"image")

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(command.main(["--root", str(root)]), 0)

            self.assertIn("0 broken links", output.getvalue())


if __name__ == "__main__":
    unittest.main()
