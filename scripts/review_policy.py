"""Validate the Advisory Review stage of a gh-setup Policy.

Adapted from open-code-review-setup commit 6e33cffa673eb4d017f43dd1a3f16500097992a9.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urlsplit


_EVENTS = frozenset({"opened", "synchronize", "reopened"})
_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_RULE_PATH = re.compile(r"(?:docs|\.opencodereview)/[A-Za-z0-9_./-]+\.json\Z")
_SECRET_NAME = "OCR_LLM_AUTH_TOKEN"


@dataclass(frozen=True)
class WorkflowPolicy:
    template: str
    template_version: int
    filename: str
    triggers: tuple[str, ...]
    events: tuple[str, ...]
    fork_policy: str
    action_sha: str
    ocr_version: str
    max_tokens_budget: int
    timeout_minutes: int
    expected_sha: str | None = None
    reporter_expected_sha: str | None = None
    rule_path: str | None = None


@dataclass(frozen=True)
class LlmPolicy:
    url: str
    model: str
    use_anthropic: bool
    secret_name: str


@dataclass(frozen=True)
class LimitsPolicy:
    max_retention_days: int


@dataclass(frozen=True)
class ReviewPolicy:
    workflow: WorkflowPolicy
    llm: LlmPolicy
    limits: LimitsPolicy


def _object(value: object, name: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{name} must be an object with exactly: {', '.join(sorted(keys))}")
    return value


def _bounded_integer(value: object, name: str, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{name} must be an integer between {lower} and {upper}")
    return value


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _validate_rule_path(value: object) -> str:
    if not isinstance(value, str) or _RULE_PATH.fullmatch(value) is None:
        raise ValueError("rule_path must name a JSON file under docs or .opencodereview")
    if any(segment in {"", ".", ".."} for segment in value.split("/")):
        raise ValueError("rule_path must not contain empty or traversal path segments")
    return value


def validate_policy(data: object) -> ReviewPolicy:
    """Validate one review stage and return immutable nonsecret values."""
    if not isinstance(data, dict) or set(data) != {"name", "workflow", "llm", "limits"} or data.get("name") != "review":
        raise ValueError("review stage must contain exactly: name, workflow, llm, limits")

    raw_workflow = data["workflow"]
    if not isinstance(raw_workflow, dict):
        raise ValueError("workflow must be an object")
    required_workflow = {
        "template", "template_version", "filename", "triggers", "events", "fork_policy",
        "action_sha", "ocr_version", "max_tokens_budget", "timeout_minutes",
    }
    optional_workflow = {"expected_sha", "reporter_expected_sha", "rule_path"}
    if not required_workflow <= set(raw_workflow) or set(raw_workflow) - required_workflow - optional_workflow:
        allowed = required_workflow | optional_workflow
        raise ValueError(f"workflow must contain required fields and only these fields: {', '.join(sorted(allowed))}")
    if raw_workflow["template"] != "open-code-review":
        raise ValueError("workflow template must be open-code-review")
    if type(raw_workflow["template_version"]) is not int or raw_workflow["template_version"] != 1:
        raise ValueError("workflow template_version must be integer 1")
    if raw_workflow["filename"] != "open-code-review.yml":
        raise ValueError("workflow filename must be open-code-review.yml")
    triggers = raw_workflow["triggers"]
    if triggers != ["pull_request_target"]:
        raise ValueError("workflow triggers must be exactly pull_request_target")
    events = raw_workflow["events"]
    if (not isinstance(events, list) or not events or any(not isinstance(item, str) for item in events)
            or len(events) != len(set(events)) or any(item not in _EVENTS for item in events)):
        raise ValueError("workflow events must be a nonempty unique subset of opened, synchronize, reopened")
    if raw_workflow["fork_policy"] != "skip":
        raise ValueError("workflow fork_policy must be skip")
    action_sha = raw_workflow["action_sha"]
    if not _is_sha(action_sha):
        raise ValueError("workflow action_sha must be a 40-character commit SHA")
    ocr_version = raw_workflow["ocr_version"]
    if not isinstance(ocr_version, str) or _VERSION.fullmatch(ocr_version) is None:
        raise ValueError("workflow ocr_version must be a pinned semantic version")
    if tuple(int(part) for part in ocr_version.split(".")) < (1, 10, 0):
        raise ValueError("workflow ocr_version must be at least 1.10.0")
    max_tokens_budget = _bounded_integer(raw_workflow["max_tokens_budget"], "max_tokens_budget", 1, 1_000_000)
    timeout_minutes = _bounded_integer(raw_workflow["timeout_minutes"], "timeout_minutes", 1, 120)

    expected_sha = raw_workflow.get("expected_sha")
    if "expected_sha" in raw_workflow:
        if not _is_sha(expected_sha):
            raise ValueError("workflow expected_sha must be a 40-character blob SHA")
        expected_sha = expected_sha.lower()
    reporter_expected_sha = raw_workflow.get("reporter_expected_sha")
    if "reporter_expected_sha" in raw_workflow:
        if not _is_sha(reporter_expected_sha):
            raise ValueError("workflow reporter_expected_sha must be a 40-character blob SHA")
        reporter_expected_sha = reporter_expected_sha.lower()
    rule_path = raw_workflow.get("rule_path")
    if "rule_path" in raw_workflow:
        rule_path = _validate_rule_path(rule_path)

    raw_llm = _object(data["llm"], "llm", {"url", "model", "use_anthropic", "secret_name"})
    url = raw_llm["url"]
    if not isinstance(url, str):
        raise ValueError("LLM URL must be HTTPS without credentials, query, or fragment")
    try:
        parsed_url = urlsplit(url)
        parsed_url.port
    except ValueError:
        raise ValueError("LLM URL must be HTTPS without credentials, query, or fragment") from None
    if (parsed_url.scheme != "https" or not parsed_url.hostname or "@" in parsed_url.netloc
            or parsed_url.query or parsed_url.fragment or "?" in url or "#" in url
            or any(character.isspace() for character in url)):
        raise ValueError("LLM URL must be HTTPS without credentials, query, or fragment")
    model = raw_llm["model"]
    if not isinstance(model, str) or not 1 <= len(model.strip()) <= 200:
        raise ValueError("LLM model must be a nonempty caller-selected name of at most 200 characters")
    use_anthropic = raw_llm["use_anthropic"]
    if type(use_anthropic) is not bool:
        raise ValueError("llm use_anthropic must be a boolean")
    if raw_llm["secret_name"] != _SECRET_NAME:
        raise ValueError(f"llm secret_name must be {_SECRET_NAME}")

    raw_limits = _object(data["limits"], "limits", {"max_retention_days"})
    max_retention_days = _bounded_integer(raw_limits["max_retention_days"], "max_retention_days", 1, 400)

    return ReviewPolicy(
        workflow=WorkflowPolicy(
            template="open-code-review",
            template_version=1,
            filename="open-code-review.yml",
            triggers=("pull_request_target",),
            events=tuple(events),
            fork_policy="skip",
            action_sha=action_sha.lower(),
            ocr_version=ocr_version,
            max_tokens_budget=max_tokens_budget,
            timeout_minutes=timeout_minutes,
            expected_sha=expected_sha,
            reporter_expected_sha=reporter_expected_sha,
            rule_path=rule_path,
        ),
        llm=LlmPolicy(
            url=url,
            model=model.strip(),
            use_anthropic=use_anthropic,
            secret_name=_SECRET_NAME,
        ),
        limits=LimitsPolicy(max_retention_days=max_retention_days),
    )
