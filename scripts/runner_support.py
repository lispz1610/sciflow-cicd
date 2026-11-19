"""Runner validation, label normalization, and YAML formatting helpers."""

from __future__ import annotations

from typing import Any, Sequence, Union

RunsOnValue = Union[str, Sequence[str]]


def validate_runs_on(runs_on: Any, as_tuple: bool = False) -> str | list[str] | tuple[str, ...]:
    """Validate runs_on value, defaulting to 'self-hosted' if None.

    Accepts non-empty string or non-empty sequence of unique label strings.
    If as_tuple is True, returns tuple of strings when multiple labels;
    otherwise returns a list of strings.
    """
    if runs_on is None:
        return "self-hosted"
    if isinstance(runs_on, str):
        if isinstance(runs_on, bool):
            raise ValueError("runs_on cannot be a boolean")
        val = runs_on.strip()
        if not val or "\n" in val or "\r" in val:
            raise ValueError(f"invalid runs_on string: {runs_on!r}")
        return val
    if isinstance(runs_on, (list, tuple)):
        if not runs_on:
            raise ValueError("runs_on list must not be empty")
        labels: list[str] = []
        for item in runs_on:
            if not isinstance(item, str) or isinstance(item, bool) or not item.strip():
                raise ValueError(f"invalid runs_on label: {item!r}")
            clean = item.strip()
            if clean in labels:
                raise ValueError(f"duplicate runs_on label: {clean!r}")
            labels.append(clean)
        return tuple(labels) if as_tuple else labels
    raise ValueError(f"runs_on must be a string or list of strings, got {type(runs_on).__name__}")


def format_runs_on(runs_on: Any, indent: str = "    ") -> str:
    """Format runs-on in generated YAML."""
    if isinstance(runs_on, str):
        return f"{indent}runs-on: {runs_on}"
    elif isinstance(runs_on, (list, tuple)):
        items = "\n".join(f"{indent}  - {item}" for item in runs_on)
        return f"{indent}runs-on:\n{items}"
    return f"{indent}runs-on: self-hosted"


_format_runs_on = format_runs_on


def is_self_hosted(runs_on: Any) -> bool:
    """Return True if runs_on targets self-hosted runner infrastructure."""
    if isinstance(runs_on, str):
        return "self-hosted" in runs_on or (
            runs_on != "ubuntu-24.04"
            and not runs_on.startswith("ubuntu-")
            and not runs_on.startswith("windows-")
            and not runs_on.startswith("macos-")
        )
    if isinstance(runs_on, (list, tuple)):
        return any(is_self_hosted(item) for item in runs_on)
    return True


_is_self_hosted = is_self_hosted
