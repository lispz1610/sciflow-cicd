"""Runner validation, label normalization, and YAML formatting helpers."""

from __future__ import annotations

from typing import Any, Optional, Sequence, TypedDict, Union

RunsOnValue = Union[str, Sequence[str]]


class RunnerAvailability(TypedDict):
    status: str
    total_count: Optional[int]
    reason: str


class RunnerAdvisory(TypedDict):
    availability: RunnerAvailability
    reason: str


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


RUNNER_ADVISORY_SUFFIX = "Advisory only; no changes were blocked."


def self_hosted_runs_on_values(
    stages: Optional[Sequence[dict]] = None,
    managed_workflows: Optional[Sequence[dict]] = None,
) -> list[Any]:
    """Collect self-hosted runs_on values from planned and non-OCR observed workflows."""
    values: list[Any] = []
    for stage in stages or []:
        if not isinstance(stage, dict) or stage.get("name") != "workflows":
            continue
        for workflow in stage.get("workflows", []) or []:
            if isinstance(workflow, dict):
                values.append(workflow.get("runs_on", "self-hosted"))
    for workflow in managed_workflows or []:
        if not isinstance(workflow, dict):
            continue
        if workflow.get("template") == "open-code-review":
            continue
        if workflow.get("filename") == "open-code-review.yml":
            continue
        values.append(workflow.get("runs_on", "self-hosted"))
    return [value for value in values if is_self_hosted(value)]


def inspect_runner_availability(api: Any, repository: str) -> RunnerAvailability:
    """Observe the Actions runner inventory without treating access limits as failures."""
    path = f"/repos/{repository}/actions/runners"
    try:
        status, body = api.request("GET", path)
    except Exception as exc:
        reason = f"GET {path}: {exc}"
        return {"status": "unverified", "total_count": None, "reason": reason}
    if status == 200 and isinstance(body, dict):
        count = body.get("total_count")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            return {"status": "empty" if count == 0 else "available", "total_count": count,
                    "reason": f"GET {path}: {count} registered runner(s)"}
    return {"status": "unverified", "total_count": None,
            "reason": f"GET {path}: HTTP {status}" if status is not None else f"GET {path}: no response"}


def runner_advisory_message(availability: RunnerAvailability) -> Optional[str]:
    """Describe a non-blocking Self-Hosted Runner availability finding."""
    if availability.get("status") == "empty":
        return (
            "Self-Hosted Runner availability: repository reports 0 registered Self-Hosted Runners "
            "for a workflow targeting a Self-Hosted Runner; register a Self-Hosted Runner or set "
            "runs_on to ubuntu-24.04 (Hosted Runner). " + RUNNER_ADVISORY_SUFFIX
        )
    if availability.get("status") == "unverified":
        return (
            "Self-Hosted Runner availability: could not verify the runner inventory ("
            + str(availability.get("reason", "unknown")) + "); if the workflow targets a "
            "Self-Hosted Runner, register one or set runs_on to ubuntu-24.04 (Hosted Runner). "
            + RUNNER_ADVISORY_SUFFIX
        )
    return None


def evaluate_runner_advisory(
    api: Any,
    repository: str,
    stages: Optional[Sequence[dict]] = None,
    managed_workflows: Optional[Sequence[dict]] = None,
) -> Optional[RunnerAdvisory]:
    """Return a structured advisory only for non-OCR workflows targeting self-hosted runners."""
    if not self_hosted_runs_on_values(stages=stages, managed_workflows=managed_workflows):
        return None
    availability = inspect_runner_availability(api, repository)
    reason = runner_advisory_message(availability)
    if reason is None:
        return None
    return {"availability": availability, "reason": reason}
