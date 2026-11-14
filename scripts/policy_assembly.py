"""Policy assembly and persistence for gh-setup."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union, TextIO, TYPE_CHECKING

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    from scripts.setup_github_repo import REPOSITORY, validate_policy
except ImportError:
    from setup_github_repo import REPOSITORY, validate_policy

DEFAULT_POLICY_PATH = Path(".local/setup-policy.json")


def _flatten_stages(stages: Union[Sequence[Any], Any]) -> List[Dict[str, Any]]:
    """Flatten and filter stages, removing None and unpacking nested sequences."""
    flat: List[Dict[str, Any]] = []
    if isinstance(stages, dict):
        return [stages]

    for item in stages:
        if item is None:
            continue
        if isinstance(item, (list, tuple)):
            for sub_item in item:
                if sub_item is not None and isinstance(sub_item, dict):
                    flat.append(sub_item)
        elif isinstance(item, dict):
            flat.append(item)
    return flat


def assemble_policy(
    repository: str,
    stages: Union[Sequence[Any], Any],
) -> Dict[str, Any]:
    """Aggregate questionnaire stage payloads into a validated policy document."""
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise ValueError(f"repository must be OWNER/REPO, got: {repository!r}")

    flat_stages = _flatten_stages(stages)
    if not flat_stages:
        raise ValueError("Cannot assemble policy: at least one stage must be configured")

    policy = {
        "version": 2,
        "repository": repository,
        "stages": flat_stages,
    }
    validate_policy(policy)
    return policy


def persist_policy(
    policy: Dict[str, Any],
    output_path: Optional[Union[str, Path]] = None,
    out_stream: Optional[TextIO] = None,
) -> Path:
    """Validate and write policy dictionary to disk as JSON, notifying the user.

    Args:
        policy: The policy dictionary to persist.
        output_path: Target path for the policy file. Defaults to .local/setup-policy.json.
        out_stream: Optional output stream to inform the user.

    Returns:
        The Path where the policy was written.
    """
    validate_policy(policy)

    target_path = Path(output_path) if output_path is not None else DEFAULT_POLICY_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)

    serialized = json.dumps(policy, indent=2) + "\n"
    target_path.write_text(serialized, encoding="utf-8")

    if out_stream is not None:
        dry_run_path = target_path.parent / "setup-dry-run.json"
        out_stream.write(
            f"Policy file successfully written to {target_path}.\n"
            f"You can re-apply this configuration in headless mode with:\n"
            f"  gh-setup dry-run --policy {target_path} > {dry_run_path}\n"
            f"  gh-setup apply --policy {target_path} --dry-run-result {dry_run_path}\n"
        )
        out_stream.flush()

    return target_path


def assemble_and_persist_policy(
    session: Any,
    stages: Union[Sequence[Any], Any],
    output_path: Optional[Union[str, Path]] = None,
) -> Tuple[Dict[str, Any], Path]:
    """Assemble policy from session state and stage payloads, then persist to disk.

    Args:
        session: Active onboarding Session instance.
        stages: Sequence of stage payloads from interactive questionnaires.
        output_path: Target path for the policy file. Defaults to .local/setup-policy.json.

    Returns:
        A tuple of (assembled_policy_dict, written_path).
    """
    repo = getattr(session, "repository", None)
    if repo is None:
        raise ValueError("Session repository is required to assemble policy")

    policy = assemble_policy(repository=repo, stages=stages)
    out_s = getattr(session, "out_stream", None)
    target_path = persist_policy(policy, output_path=output_path, out_stream=out_s)
    return policy, target_path
