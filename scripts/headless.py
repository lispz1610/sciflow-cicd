"""Headless policy execution runner for sciflow-cicd."""

import json
import hashlib
import os
import sys
from pathlib import Path
from typing import Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from configure_github_repo import ApiError, GitHubApi
from setup_github_repo import REPOSITORY, compute_policy_sha256, inventory, is_fatal_block, run_policy, validate_policy


def run_headless_apply(
    policy_path: str,
    repo: Optional[str] = None,
    token: Optional[str] = None,
    apply: bool = True,
    dry_run_path: Optional[str] = None,
    plan_path: Optional[str] = None,
) -> int:
    """Inspect or execute a Policy without interactive prompts."""
    dry_run_input = dry_run_path or plan_path
    if token is None:
        token = os.environ.get("GITHUB_TOKEN")

    if not token:
        print("Error: GITHUB_TOKEN environment variable is required", file=sys.stderr)
        return 1

    path = Path(policy_path)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        print(f"Error: Failed to read policy file '{policy_path}': {error}", file=sys.stderr)
        return 1

    try:
        policy = json.loads(content)
    except json.JSONDecodeError as error:
        print(f"Error: Malformed JSON in policy file '{policy_path}': {error}", file=sys.stderr)
        return 1

    try:
        validate_policy(policy)
    except ValueError as error:
        print(f"Error: Invalid policy schema in '{policy_path}': {error}", file=sys.stderr)
        return 1

    target_repo = repo if repo else policy.get("repository")
    if not target_repo:
        print(
            f"Error: Repository must be specified via --repo or declared in '{policy_path}'",
            file=sys.stderr,
        )
        return 1

    if not isinstance(target_repo, str) or not REPOSITORY.fullmatch(target_repo):
        print(
            f"Error: Invalid repository format '{target_repo}': must be OWNER/REPO",
            file=sys.stderr,
        )
        return 1

    policy_dir = path.parent
    if isinstance(policy, dict) and isinstance(policy.get("stages"), list):
        for stage in policy["stages"]:
            if isinstance(stage, dict) and stage.get("name") == "files" and isinstance(stage.get("items"), list):
                for item in stage["items"]:
                    if isinstance(item, dict) and isinstance(item.get("source"), str):
                        source_path = Path(item["source"])
                        if not source_path.is_absolute():
                            item["source"] = str((policy_dir / source_path).resolve())

    policy_sha256 = compute_policy_sha256(policy)
    accepted_plan = None
    if apply:
        if not dry_run_input:
            print("Error: --dry-run-result PATH is required; save sciflow-cicd dry-run JSON and pass it to apply", file=sys.stderr)
            return 1
        try:
            accepted_plan = json.loads(Path(dry_run_input).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            print(f"Error: Cannot read Dry-Run result '{dry_run_input}': {error}", file=sys.stderr)
            return 1
        if not isinstance(accepted_plan, dict) or accepted_plan.get("mode") != "dry-run" or \
                accepted_plan.get("repository") != target_repo or \
                accepted_plan.get("policy_sha256") != policy_sha256 or \
                not isinstance(accepted_plan.get("changes"), list):
            print("Error: --dry-run-result must be a Dry-Run result for this Policy and repository", file=sys.stderr)
            return 1

    api = GitHubApi(token)
    try:
        observed = inventory(api, target_repo) if not apply else None
        result = run_policy(api, target_repo, policy, apply=True,
                            accepted_dry_run=accepted_plan) if apply else run_policy(api, target_repo, policy)
        if observed is not None:
            result["inventory"] = observed
        if not apply:
            result["policy_sha256"] = policy_sha256
    except (ApiError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))

    if any(change.get("status") in ("unverified", "failed") or is_fatal_block(change)
           for change in result.get("changes", [])):
        return 2

    return 0
