#!/usr/bin/env python3
"""Check relative links between Markdown files in a repository."""

import argparse
from collections import Counter
from html import unescape
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import unquote, urlsplit


LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(<[^>]+>|(?:\\.|[^()\s])+)(?:\s+[^)]*)?\)")
REFERENCE_DEFINITION = re.compile(
    r"^ {0,3}\[([^\]]+)\]:[ \t]*(<[^>]+>|(?:\\.|[^\s])+)(?:[ \t]+.*)?[ \t]*$"
)
REFERENCE_LINK = re.compile(
    r"(?<!\\)!?\[([^\]\n]+)\](?:[ \t]*\[([^\]\n]*)\])?(?![ \t]*\()"
)
HTML_HREF = re.compile(r"\bhref\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.IGNORECASE)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
SETEXT = re.compile(r"^ {0,3}(=+|-+)\s*$")
ANCHOR = re.compile(r"<a\s+(?:id|name)\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
HTML_ID = re.compile(r"\sid\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
HTML_TAG = re.compile(r"<[^>]*>")
INLINE_CODE = re.compile(r"(`+).*?\1")


def _slug(text):
    text = unescape(HTML_TAG.sub("", text))
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.strip().lower()
    slug = []
    pending_dash = False
    for char in text:
        if char.isspace():
            pending_dash = bool(slug)
        elif char == "-" or char == "_" or unicodedata.category(char)[0] in {"L", "N"}:
            if pending_dash and slug and slug[-1] != "-":
                slug.append("-")
            slug.append(char)
            pending_dash = False
    return "".join(slug)


def _anchors(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    anchors = set()
    duplicates = Counter()
    previous_line = ""
    in_fence = None
    for line in lines:
        fence = FENCE.match(line)
        if in_fence:
            if fence and fence.group(1)[0] == in_fence[0] and len(fence.group(1)) >= len(in_fence):
                in_fence = None
            previous_line = ""
            continue
        if fence:
            in_fence = fence.group(1)
            previous_line = ""
            continue
        for match in ANCHOR.finditer(line):
            anchors.add(unquote(match.group(1)))
        for match in HTML_ID.finditer(line):
            anchors.add(unquote(match.group(1)))

        heading = HEADING.match(line)
        if heading:
            slug = _slug(heading.group(2))
            suffix = duplicates[slug]
            duplicates[slug] += 1
            anchors.add(slug if suffix == 0 else f"{slug}-{suffix}")
        elif previous_line and SETEXT.match(line):
            slug = _slug(previous_line)
            suffix = duplicates[slug]
            duplicates[slug] += 1
            anchors.add(slug if suffix == 0 else f"{slug}-{suffix}")
        previous_line = line
    return anchors


def _markdown_files(root):
    ignored = {".git", ".venv", "venv", "__pycache__", "node_modules"}
    return sorted(
        path for path in root.rglob("*.md")
        if not any(part in ignored for part in path.relative_to(root).parts)
    )


def _reference_definitions(lines):
    references = {}
    definition_lines = set()
    in_fence = None
    for line_number, line in enumerate(lines, start=1):
        fence = FENCE.match(line)
        if in_fence:
            if fence and fence.group(1)[0] == in_fence[0] and len(fence.group(1)) >= len(in_fence):
                in_fence = None
            continue
        if fence:
            in_fence = fence.group(1)
            continue

        searchable_line = INLINE_CODE.sub(lambda match: " " * len(match.group()), line)
        definition = REFERENCE_DEFINITION.match(searchable_line)
        if not definition:
            continue
        label = " ".join(definition.group(1).split()).casefold()
        destination = definition.group(2)
        if destination.startswith("<") and destination.endswith(">"):
            destination = destination[1:-1]
        references.setdefault(label, destination)
        definition_lines.add(line_number)
    return references, definition_lines


def _check_link_target(root, source, line_number, target, anchor_cache, errors):
    target = unescape(target.replace("\\ ", " "))
    parts = urlsplit(target)
    if parts.scheme or target.startswith("//"):
        return
    if not parts.path and not parts.fragment:
        return

    relative_path = unquote(parts.path)
    destination = (source.parent / relative_path).resolve() if relative_path else source
    if not destination.is_relative_to(root):
        errors.append((source, line_number, target, "path escapes the repository"))
        return
    if not destination.exists():
        errors.append((source, line_number, target, "target does not exist"))
        return
    if parts.fragment and destination.suffix.lower() == ".md":
        if destination not in anchor_cache:
            anchor_cache[destination] = _anchors(destination)
        fragment = unquote(parts.fragment)
        if fragment not in anchor_cache[destination]:
            errors.append((source, line_number, target, "Markdown anchor does not exist"))


def audit(root, markdown_files=None):
    root = root.resolve()
    markdown_files = list(markdown_files) if markdown_files is not None else _markdown_files(root)
    anchor_cache = {}
    errors = []

    for source in markdown_files:
        text = source.read_text(encoding="utf-8")
        lines = text.splitlines()
        references, definition_lines = _reference_definitions(lines)
        in_fence = None
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
                label = link.group(2)
                if not label:
                    label = link.group(1)
                normalized_label = " ".join(label.split()).casefold()
                if normalized_label in references:
                    targets.append(references[normalized_label])

            for href in HTML_HREF.finditer(searchable_line):
                targets.append(next(value for value in href.groups() if value is not None))

            for target in targets:
                _check_link_target(root, source, line_number, target, anchor_cache, errors)

    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root (default: current directory)")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        parser.error(f"repository root is not a directory: {args.root}")

    errors = audit(args.root)
    if errors:
        for source, line, target, reason in errors:
            print(f"{source.relative_to(args.root.resolve())}:{line}: {target!r}: {reason}", file=sys.stderr)
        print(f"Markdown link audit found {len(errors)} broken link(s).", file=sys.stderr)
        return 1
    print("Markdown link audit passed (0 broken links).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
