"""Tests for shared P6 test support module (tests/p6_test_support.py)."""

from pathlib import Path
import tempfile
import unittest

from tests.p6_test_support import (
    extract_readme_section,
    extract_relative_markdown_targets,
)

SAMPLE_README = """# Repository Title

Some intro text.

## Requirements

Prerequisites and system info.

- Python 3.10+
- Git

## Architecture and trust boundaries

Here is architecture details.

```bash
# code block inside architecture
echo "hello"
```

### Subheading A

Some subsection.

## Final Section

Ending contents with no trailing heading.
"""


class P6TestSupportTests(unittest.TestCase):
    def test_extract_readme_section_matches_intermediate_section(self):
        section = extract_readme_section(SAMPLE_README, "Architecture and trust boundaries")
        self.assertTrue(section.startswith("## Architecture and trust boundaries"))
        self.assertIn("Here is architecture details.", section)
        self.assertIn("### Subheading A", section)
        self.assertNotIn("## Final Section", section)

    def test_extract_readme_section_accepts_leading_hashes_and_whitespace(self):
        section = extract_readme_section(SAMPLE_README, "  ##  Requirements  ")
        self.assertTrue(section.startswith("## Requirements"))
        self.assertIn("Python 3.10+", section)
        self.assertNotIn("## Architecture and trust boundaries", section)

    def test_extract_readme_section_matches_final_section_to_eof(self):
        section = extract_readme_section(SAMPLE_README, "Final Section")
        self.assertTrue(section.startswith("## Final Section"))
        self.assertIn("Ending contents with no trailing heading.", section)

    def test_extract_readme_section_returns_empty_when_not_found(self):
        section = extract_readme_section(SAMPLE_README, "Nonexistent Section")
        self.assertEqual(section, "")

    def test_extract_relative_markdown_targets_resolves_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            target1 = tmp / "target1.md"
            target1.write_text("# Target 1\n", encoding="utf-8")
            target2 = tmp / "docs" / "target2.md"
            target2.parent.mkdir(parents=True, exist_ok=True)
            target2.write_text("# Target 2\n", encoding="utf-8")

            source_file = tmp / "index.md"
            source_content = (
                "# Source\n\n"
                "[Link 1](target1.md)\n"
                "[Link 2 with anchor](docs/target2.md#section-anchor)\n"
                "[Missing target](missing.md)\n"
                "[External target](https://example.com/doc)\n"
                "[Local anchor](#top)\n"
                "```markdown\n[Fenced ignored](docs/target2.md)\n```\n"
                "Reference link [ref-link][ref-target].\n\n"
                "[ref-target]: target1.md\n"
                '<a href="docs/target2.md">HTML Link</a>\n'
            )
            source_file.write_text(source_content, encoding="utf-8")

            extracted = extract_relative_markdown_targets(source_file)
            self.assertEqual(
                extracted,
                {target1.resolve(), target2.resolve()},
            )


if __name__ == "__main__":
    unittest.main()
