"""Deterministic standard-library YAML subset parser used by Inventory."""

import json
import re


def _yaml_scan(text):
    """Scan characters with quote/depth state, honoring YAML doubled single quotes."""
    quote = None
    depth = 0
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if quote == "'" and char == "'" and index + 1 < len(text) and text[index + 1] == "'":
            yield index, char, True, depth
            index += 1
            yield index, text[index], True, depth
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        yield index, char, quote is not None, depth
        index += 1


def _yaml_split_flow(text):
    parts = []
    buf = []
    for _, char, in_quote, depth in _yaml_scan(text):
        if char == "," and not in_quote and depth == 0:
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
        else:
            buf.append(char)
    part = "".join(buf).strip()
    if part:
        parts.append(part)
    return parts


def _yaml_split_mapping(text):
    for index, char, in_quote, depth in _yaml_scan(text):
        if char == ":" and not in_quote and depth == 0 and (index + 1 == len(text) or text[index + 1].isspace()):
            return text[:index].strip(), text[index + 1:].strip()
    return text.strip(), ""


def _yaml_strip_comment(text):
    for index, char, in_quote, depth in _yaml_scan(text):
        if char == "#" and not in_quote and depth == 0 and (index == 0 or text[index - 1].isspace()):
            return text[:index].rstrip()
    return text.rstrip()


def _yaml_plain_scalar(text):
    value = text.strip()
    if not value:
        return None
    if value.startswith('"'):
        try:
            loaded = json.loads(value)
        except (ValueError, TypeError):
            return value[1:-1] if value.endswith('"') and len(value) > 1 else value
        return loaded
    if value.startswith("'"):
        return value[1:-1].replace("''", "'") if value.endswith("'") and len(value) > 1 else value
    if value.startswith("["):
        return [_yaml_plain_scalar(part) for part in _yaml_split_flow(value[1:-1])]
    if value.startswith("{"):
        result = {}
        for part in _yaml_split_flow(value[1:-1]):
            key, item = _yaml_split_mapping(part)
            result[str(_yaml_plain_scalar(key))] = _yaml_plain_scalar(item)
        return result
    lowered = value.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "~"):
        return None
    if re.fullmatch(r"[-+]?\d+", value):
        return int(value)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?", value):
        return float(value)
    return value


def _yaml_key(text):
    value = _yaml_plain_scalar(text)
    return value if type(value) is bool else str(value)


def _yaml_indent(line):
    return len(line) - len(line.lstrip(" "))


def _yaml_ignorable(line):
    return not line.strip() or line.lstrip().startswith("#") or line.strip() in ("---", "...")


def _yaml_block_scalar(header, lines, index, parent_indent):
    chomp = "-" if "-" in header else ("+" if "+" in header else "")
    collected = []
    content_indent = None
    cursor = index
    while cursor < len(lines):
        raw = lines[cursor]
        if not raw.strip():
            collected.append("")
            cursor += 1
            continue
        current = _yaml_indent(raw)
        if current <= parent_indent:
            break
        if content_indent is None:
            content_indent = current
        collected.append(raw[content_indent:] if len(raw) >= content_indent else raw.lstrip())
        cursor += 1
    if chomp != "+":
        while collected and collected[-1] == "":
            collected.pop()
    text = "\n".join(collected)
    if header.startswith(">"):
        paragraphs = []
        current = []
        for line in collected:
            if line == "":
                if current:
                    paragraphs.append(" ".join(current))
                    current = []
            else:
                current.append(line)
        if current:
            paragraphs.append(" ".join(current))
        text = "\n".join(paragraphs)
    if text and chomp != "-":
        text += "\n"
    return text, cursor


def _yaml_read_child(lines, index, parent_indent):
    cursor = index
    while cursor < len(lines) and _yaml_ignorable(lines[cursor]):
        cursor += 1
    if cursor >= len(lines) or _yaml_indent(lines[cursor]) <= parent_indent:
        return None, index
    return _parse_yaml_node(lines, cursor, _yaml_indent(lines[cursor]))


def _yaml_read_value(raw, lines, index, parent_indent):
    value = _yaml_strip_comment(raw).strip()
    if value.startswith("|") or value.startswith(">"):
        return _yaml_block_scalar(value, lines, index, parent_indent)
    if not value:
        return _yaml_read_child(lines, index, parent_indent)
    return _yaml_plain_scalar(value), index


def _parse_yaml_mapping(lines, index, indent, seed=None):
    result = dict(seed or {})
    cursor = index
    while cursor < len(lines):
        line = lines[cursor]
        if _yaml_ignorable(line):
            cursor += 1
            continue
        if _yaml_indent(line) != indent:
            break
        stripped = line.strip()
        if stripped.startswith("-") and (len(stripped) == 1 or stripped[1].isspace()):
            break
        key, raw = _yaml_split_mapping(stripped)
        cursor += 1
        value, cursor = _yaml_read_value(raw, lines, cursor, indent)
        result[_yaml_key(key)] = value
    return result, cursor


def _parse_yaml_sequence(lines, index, indent):
    items = []
    cursor = index
    while cursor < len(lines):
        line = lines[cursor]
        if _yaml_ignorable(line):
            cursor += 1
            continue
        if _yaml_indent(line) != indent:
            break
        stripped = line.strip()
        if not (stripped.startswith("-") and (len(stripped) == 1 or stripped[1].isspace())):
            break
        rest = stripped[1:].strip()
        cursor += 1
        quoted = rest.startswith(('"', "'"))
        key, raw = ("", "") if quoted else _yaml_split_mapping(rest)
        if not quoted and (key != rest or raw):
            value, nested = _yaml_read_value(raw, lines, cursor, indent + 2)
            item, cursor = _parse_yaml_mapping(lines, nested, indent + 2, {_yaml_key(key): value})
            items.append(item)
        else:
            value, cursor = _yaml_read_value(rest, lines, cursor, indent)
            items.append(value)
    return items, cursor


def _parse_yaml_node(lines, index, indent):
    cursor = index
    while cursor < len(lines) and _yaml_ignorable(lines[cursor]):
        cursor += 1
    if cursor >= len(lines):
        return None, cursor
    stripped = lines[cursor].strip()
    if stripped.startswith("-") and (len(stripped) == 1 or stripped[1].isspace()):
        return _parse_yaml_sequence(lines, cursor, _yaml_indent(lines[cursor]))
    return _parse_yaml_mapping(lines, cursor, _yaml_indent(lines[cursor]))


def parse_yaml_subset(text):
    parsed, _ = _parse_yaml_node(text.splitlines(), 0, 0)
    return parsed if isinstance(parsed, dict) else {}
