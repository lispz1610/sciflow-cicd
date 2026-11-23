#!/usr/bin/env python3
"""Single source of truth for workflow defaults and shared prompt helpers."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


DEFAULT_PYTHON_VERSIONS = ["3.11", "3.12"]
DEFAULT_TEST_COMMANDS = ["python3 -m unittest discover -s tests -p 'test_*.py'"]
DEFAULT_CI_COMMANDS = DEFAULT_TEST_COMMANDS
DEFAULT_CMAKE_BUILD_COMMANDS = ["cmake -B build -S .", "cmake --build build"]
DEFAULT_CMAKE_TEST_COMMANDS = ["ctest --test-dir build --output-on-failure"]
DEFAULT_SCHEDULE = "0 2 * * 1"
DEFAULT_CONTAINER_SCHEDULE = "0 2 * * *"
DEFAULT_STATUS_CHECK_NAME = "status-check"
DEFAULT_SELF_HOSTED_RUNNER = "self-hosted"
DEFAULT_HOSTED_RUNNER = "ubuntu-latest"
WORKFLOW_PATH_PATTERN = re.compile(r"^\.github/workflows/[^/]+\.ya?ml\Z")


def resolve_runner_target(global_runs_on: Any, local_override: Any = None) -> Any:
    """Return the effective runner labels, preferring an explicit local override."""
    return global_runs_on if local_override is None else local_override


def normalize_runner_labels(raw_labels: Any, default: str = DEFAULT_SELF_HOSTED_RUNNER) -> Any:
    """Normalize comma-separated or list runner labels to one label or a list."""
    if isinstance(raw_labels, list):
        labels = [str(label).strip() for label in raw_labels if str(label).strip()]
    else:
        text = str(raw_labels or "")
        labels = [part.strip() for part in text.split(",") if part.strip()]
    labels = labels or [default]
    return labels[0] if len(labels) == 1 else labels


def default_schedule_string(defaults: Optional[Dict[str, Any]],
                            fallback: str = DEFAULT_SCHEDULE) -> str:
    """Resolve the cron expression to show as a schedule prompt default."""
    value = (defaults or {}).get("schedule", [fallback])
    if isinstance(value, list) and value:
        return str(value[0]).strip() or fallback
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def normalize_cron_schedule(raw_value: Any, default: str = DEFAULT_SCHEDULE) -> List[str]:
    """Normalize a schedule prompt answer into a single cron expression list."""
    if isinstance(raw_value, list):
        values = [str(value).strip() for value in raw_value if str(value).strip()]
    else:
        text = str(raw_value or "")
        values = [part.strip() for part in text.split(",") if part.strip()]
    return [values[0] if values else default]


def resolve_check_spec(defaults: Optional[Dict[str, Any]], key: str = "status_check_name",
                       fallback: str = DEFAULT_STATUS_CHECK_NAME) -> str:
    """Resolve a check specification from defaults with a safe fallback."""
    value = (defaults or {}).get(key)
    return str(value).strip() or fallback if isinstance(value, str) else fallback


def normalize_workflow_path(path: str) -> str:
    """Return a canonical ``.github/workflows/...`` path for a workflow file."""
    if not isinstance(path, str) or path.startswith(".github/workflows/"):
        return path
    return f".github/workflows/{path}"


def parse_check_spec(spec: str) -> Optional[Dict[str, str]]:
    """Parse a check specification into ``{check_name: workflow_path}``.

    Shared by interactive prompts and Setup stage normalization so both
    accept the same shorthand forms and validate workflow paths identically.
    """
    if not isinstance(spec, str):
        return None
    stripped = spec.strip()
    if not stripped:
        return None
    tokens = [token.strip() for token in stripped.split(",") if token.strip()]
    if not tokens:
        return None

    checks: Dict[str, str] = {}
    for token in tokens:
        if "=" in token:
            left, right = [part.strip() for part in token.split("=", 1)]
            if not left or not right:
                return None
            if left.endswith((".yml", ".yaml")) or left.startswith(".github/"):
                workflow_path, check_name = left, right
            elif right.endswith((".yml", ".yaml")) or right.startswith(".github/"):
                check_name, workflow_path = left, right
            else:
                workflow_path, check_name = left, right
        else:
            check_name = token
            if "dependency-review" in check_name.lower():
                workflow_path = ".github/workflows/dependency-review.yml"
            else:
                workflow_path = ".github/workflows/ci.yml"

        workflow_path = normalize_workflow_path(workflow_path)
        if not check_name or not WORKFLOW_PATH_PATTERN.fullmatch(workflow_path):
            return None
        checks[check_name] = workflow_path

    return checks if checks else None
