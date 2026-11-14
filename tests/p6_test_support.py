"""Shared test support helpers for P6 test suites."""

from __future__ import annotations

from pathlib import Path
import re
import sys

# Ensure repository root is on sys.path so scripts and tests can be imported cleanly
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.check_markdown_links import (
    FENCE,
    HTML_HREF,
    INLINE_CODE,
    LINK,
    REFERENCE_LINK,
    _reference_definitions,
)

__all__ = [
    "extract_readme_section",
    "extract_relative_markdown_targets",
]


def extract_readme_section(readme_text: str, heading_title: str) -> str:
    """Extract a top-level ('##') section from README text by heading title.

    Parameters
    ----------
    readme_text : str
        The full markdown content of README.md.
    heading_title : str
        The title of the heading to extract, with or without leading '##'.

    Returns
    -------
    str
        The matched section including the heading and all contents up to
        the next '##' heading or EOF. Returns an empty string if not found.
    """
    clean_title = heading_title.strip().lstrip("#").strip()
    pattern = rf"^##\s+{re.escape(clean_title)}(?:\b|\s*$).*?(?=^## |\Z)"
    match = re.search(pattern, readme_text, flags=re.MULTILINE | re.DOTALL)
    if not match:
        return ""
    return match.group(0)


def extract_relative_markdown_targets(source_path: Path) -> set[Path]:
    """Return resolved file paths for all relative links in ``source_path``.

    Extracts inline links, reference links, and HTML href targets while ignoring
    code fences, inline code blocks, external URLs, and intra-document anchor fragments.
    Only paths that actually exist on the filesystem are included in the returned set.
    """
    source_path = Path(source_path)
    text = source_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    references, definition_lines = _reference_definitions(lines)
    in_fence = None
    resolved: set[Path] = set()

    for line_number, line in enumerate(lines, start=1):
        fence = FENCE.match(line)
        if in_fence:
            if fence and fence.group(1)[0] == in_fence[0] and len(fence.group(1)) >= len(in_fence):
                in_fence = None
            continue
        if fence:
            in_fence = fence.group(1)
            continue
        if line_number in definition_lines:
            continue

        searchable_line = INLINE_CODE.sub(lambda match: " " * len(match.group()), line)
        targets = []
        for link in LINK.finditer(searchable_line):
            target = link.group(1)
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            targets.append(target)
        for link in REFERENCE_LINK.finditer(searchable_line):
            label = link.group(2) or link.group(1)
            normalized_label = " ".join(label.split()).casefold()
            if normalized_label in references:
                targets.append(references[normalized_label])
        for href in HTML_HREF.finditer(searchable_line):
            targets.append(next(value for value in href.groups() if value is not None))

        for target in targets:
            if "://" in target or target.startswith("//") or target.startswith("#"):
                continue
            path_part = target.split("#", 1)[0].strip()
            if not path_part:
                continue
            candidate = (source_path.parent / path_part).resolve()
            if candidate.exists():
                resolved.add(candidate)
    return resolved
