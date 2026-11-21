"""Dry-Run, user confirmation, and apply execution for Interactive Setup."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, TextIO, TYPE_CHECKING

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

if TYPE_CHECKING:
    try:
        from scripts.interactive import Session
    except ImportError:
        from interactive import Session

try:
    from configure_github_repo import ApiError, GitHubApi
    from setup_github_repo import run_policy, is_fatal_block
    from prompts import prompt_confirm
    from setup_profiles import Profile, get_profile
except ImportError:
    from scripts.configure_github_repo import ApiError, GitHubApi
    from scripts.setup_github_repo import run_policy, is_fatal_block
    from scripts.prompts import prompt_confirm
    from scripts.setup_profiles import Profile, get_profile


def execute_dry_run(
    api: GitHubApi,
    repository: str,
    policy: Dict[str, Any],
) -> Dict[str, Any]:
    """Execute Dry-Run preflight of Policy without modifying GitHub resources."""
    return run_policy(api, repository, policy, apply=False)


def classify_change_diff(change: Dict[str, Any]) -> str:
    """Classify a proposed change into unchanged, added, modified, or pending."""
    status = change.get("status", "")
    stage = change.get("stage", "")
    op = (change.get("operation") or "").upper()
    current = change.get("current")
    desired = change.get("desired")

    if status == "pending" or stage == "gates":
        if status == "pending" or change.get("evidence_pr") is None:
            return "pending"
    if status == "unchanged" or (current is not None and current == desired):
        return "unchanged"
    if op in ("POST", "CREATE", "ADD") or (isinstance(current, dict) and current.get("status") == "absent") or current is None:
        return "added"
    if op in ("PATCH", "PUT", "UPDATE", "REMOVE", "DELETE") or (current is not None and current != desired):
        return "modified"
    return "modified"


def _resolve_profile_display(profile: Optional[Union[Any, str]]) -> Optional[str]:
    """Return formatted active setup profile banner line if available."""
    if profile is None:
        return None
    if isinstance(profile, str):
        try:
            prof_obj = get_profile(profile)
            return f"Active Setup Profile: {prof_obj.display_name} ({prof_obj.id})"
        except Exception:
            return f"Active Setup Profile: {profile}"
    if hasattr(profile, "display_name") and hasattr(profile, "id"):
        return f"Active Setup Profile: {profile.display_name} ({profile.id})"
    if isinstance(profile, dict) and "display_name" in profile:
        return f"Active Setup Profile: {profile['display_name']} ({profile.get('id', '')})"
    return f"Active Setup Profile: {profile}"


def _format_comparison_table(changes: List[Dict[str, Any]]) -> List[str]:
    """Format structured 84-column tabular preview of proposed changes and diffs."""
    lines = [
        "-" * 84,
        f"{'Stage':<16} {'Setting / Resource':<38} {'Diff':<10} {'Status':<14}",
        "-" * 84,
    ]
    unchanged = 0
    added = 0
    modified = 0
    pending = 0

    for c in changes:
        diff_type = classify_change_diff(c)
        if diff_type == "unchanged":
            unchanged += 1
        elif diff_type == "added":
            added += 1
        elif diff_type == "modified":
            modified += 1
        elif diff_type == "pending":
            pending += 1

        stage = (c.get("stage") or "general")[:16]
        path_or_reason = c.get("path") or c.get("reason") or "direct setting"
        if len(path_or_reason) > 38:
            target = path_or_reason[:35] + "..."
        else:
            target = path_or_reason
        status = (c.get("status") or "planned")[:14]
        lines.append(f"{stage:<16} {target:<38} {diff_type:<10} {status:<14}")

    lines.append("-" * 84)
    lines.append(
        f"Diff Summary: {unchanged} unchanged, {added} added, {modified} modified, {pending} pending "
        f"(Total: {len(changes)})"
    )
    lines.append("-" * 84)
    return lines


def format_dry_run_summary(
    dry_run_result: Dict[str, Any],
    profile: Optional[Union[Profile, str, Dict[str, Any]]] = None,
) -> str:
    """Format Dry-Run planned changes into a clear, user-friendly summary."""
    repository = dry_run_result.get("repository", "unknown")
    changes: List[Dict[str, Any]] = dry_run_result.get("changes", [])
    prof = profile if profile is not None else dry_run_result.get("profile")
    prof_line = _resolve_profile_display(prof)

    lines = [
        "=" * 60,
        f"Dry-Run Summary: Proposed Changes for {repository}",
    ]
    if prof_line:
        lines.append(prof_line)
    lines.append("=" * 60)

    warnings = dry_run_result.get("warnings", [])
    setup_pr = dry_run_result.get("setup_pull_request")
    skills_bundle = dry_run_result.get("agent_skills_bundle")

    if not changes:
        lines.append("No changes needed. Repository is already in the desired state.")
        lines.append("Diff Summary: 0 unchanged, 0 added, 0 modified, 0 pending (Total: 0)")
        if skills_bundle:
            lines.append(
                "Matt Pocock agent skills: "
                f"{skills_bundle['source_repository']}@{skills_bundle['source_commit']} "
                f"(version {skills_bundle['source_version']}, {skills_bundle['file_count']} files, "
                f"destination {skills_bundle['destination']})."
            )
        if setup_pr:
            lines.append(f"Existing draft Setup Pull Request: {setup_pr}")
            lines.append("Owner action: inspect and merge it before workflows run.")
        if warnings:
            lines.append("Warnings:")
            lines.extend(f"  - {warning}" for warning in warnings)
        lines.append("=" * 60)
        return "\n".join(lines)

    # Render tabular comparison preview
    lines.extend(_format_comparison_table(changes))

    by_stage: Dict[str, List[Dict[str, Any]]] = {}
    for change in changes:
        stage = change.get("stage", "general")
        by_stage.setdefault(stage, []).append(change)

    for stage, stage_changes in by_stage.items():
        if stage == "agent-skills":
            lines.append("\nAgent skill files proposed in one draft Setup Pull Request:")
            lines.append("  The reviewed Matt Pocock source commit and inventory are shown in each selected change.")
            lines.append("  The owner must inspect and merge the Setup Pull Request before the files are added.")
        elif stage in ("workflows", "review", "hooks"):
            lines.append("\nWorkflow files proposed in one draft Setup Pull Request:")
            lines.append("  The owner must inspect and merge the Setup Pull Request before workflows run.")
        elif stage == "gates":
            lines.append("\nPending gates and rulesets:")
        else:
            lines.append("\nDirect repository settings:")
        lines.append(f"\nStage: {stage}")
        if stage == "agent-skills" and skills_bundle:
            lines.append(
                f"  Source: {skills_bundle['source_repository']}@{skills_bundle['source_commit']} "
                f"(version {skills_bundle['source_version']}, {skills_bundle['file_count']} files)"
            )
        for change in stage_changes:
            op = change.get("operation")
            path = change.get("path")
            status = change.get("status", "planned")
            reason = change.get("reason")
            desired = change.get("desired")

            if op and path:
                lines.append(f"  - {op} {path}")
            elif reason:
                lines.append(f"  - {reason}")

            if isinstance(desired, dict) and desired:
                details = ", ".join(f"{k}={v}" for k, v in desired.items())
                lines.append(f"    Desired: {details}")

            if status == "blocked":
                blocked_reason = reason or "unknown preflight check failure"
                availability = change.get("availability")
                if not is_fatal_block(change):
                    label = (
                        f"blocked (non-fatal degradation): {availability}"
                        if availability else "blocked (non-fatal degradation)"
                    )
                else:
                    label = f"blocked: {availability}" if availability else "blocked"
                lines.append(f"    Status: {label} ({blocked_reason})")
                guidance = change.get("guidance")
                if guidance:
                    lines.append(f"    Guidance: {guidance}")
            else:
                lines.append(f"    Status: {status}")

    if warnings:
        lines.append("\nWarnings:")
        lines.extend(f"  - {warning}" for warning in warnings)
    if setup_pr:
        lines.append(f"\nExisting draft Setup Pull Request: {setup_pr}")
    fatal_blocks = [c for c in changes if is_fatal_block(c)]
    degraded_blocks = [c for c in changes if c.get("status") == "blocked" and not is_fatal_block(c)]
    if fatal_blocks:
        lines.append("\nBlocked changes need owner action before they can be applied.")
    elif degraded_blocks:
        lines.append("\nOptional capabilities are degraded; unblocked direct settings remain applicable.")

    lines.append("\n------------------------------------------------------------")
    lines.append(f"Total calculated changes: {len(changes)}")
    lines.append("============================================================")
    return "\n".join(lines)


def display_dry_run_summary(
    dry_run_result: Dict[str, Any],
    out_stream: Optional[TextIO] = None,
    profile: Optional[Union[Profile, str, Dict[str, Any]]] = None,
) -> None:
    """Display formatted Dry-Run summary to output stream."""
    out_s = out_stream if out_stream is not None else sys.stdout
    prof = profile if profile is not None else dry_run_result.get("profile")
    out_s.write(format_dry_run_summary(dry_run_result, profile=prof) + "\n")
    out_s.flush()


def prompt_apply_confirmation(
    in_stream: Optional[TextIO] = None,
    out_stream: Optional[TextIO] = None,
) -> bool:
    """Prompt user for confirmation to apply proposed changes, defaulting to No."""
    result = prompt_confirm(
        "Apply these changes?",
        default=False,
        in_stream=in_stream,
        out_stream=out_stream,
    )
    return bool(result)


def format_apply_summary(
    apply_result: Dict[str, Any],
    profile: Optional[Union[Profile, str, Dict[str, Any]]] = None,
) -> str:
    """Format summary outcome of applied policy changes."""
    changes = apply_result.get("changes", [])
    total = len(changes)
    verified = sum(1 for c in changes if c.get("status") == "verified")
    pending = sum(1 for c in changes if c.get("status") == "pending")
    unverified = sum(1 for c in changes if c.get("status") == "unverified")
    blocked = sum(1 for c in changes if c.get("status") == "blocked")
    failed = sum(1 for c in changes if c.get("status") == "failed")
    fatal_blocked = sum(1 for c in changes if is_fatal_block(c))
    degraded_blocked = sum(1 for c in changes if c.get("status") == "blocked" and not is_fatal_block(c))

    prof = profile if profile is not None else apply_result.get("profile")
    prof_line = _resolve_profile_display(prof)

    lines = ["\nApply Summary:"]
    if prof_line:
        lines.append(f"  {prof_line}")
    lines.extend([
        f"  Total changes: {total}",
        f"  Verified: {verified}",
    ])
    if pending > 0:
        lines.append(f"  Pending: {pending}")
    lines.append(f"  Unverified: {unverified}")
    lines.append(f"  Blocked: {blocked}")
    if degraded_blocked > 0:
        lines.append(f"    (non-fatal degraded capabilities: {degraded_blocked})")
    lines.append(f"  Failed: {failed}")

    setup_pr = apply_result.get("setup_pull_request") or apply_result.get("setup_pr_url")
    setup_branch = apply_result.get("setup_branch")
    if setup_pr:
        lines.append(f"  Setup Pull Request: {setup_pr}")
        lines.append("  Owner action: inspect and merge the draft Setup Pull Request before workflows run.")
    if setup_branch:
        lines.append(f"  Setup branch: {setup_branch}")
    recovery = apply_result.get("recovery")
    if recovery:
        lines.append(f"  Recovery: {recovery}")
    warnings = apply_result.get("warnings", [])
    if warnings:
        lines.append("  Warnings and actionable guidance:")
        lines.extend(f"    - {warning}" for warning in warnings)

    if unverified == 0 and blocked == 0 and failed == 0 and pending == 0 and not warnings:
        lines.append("All changes applied and verified successfully.")
    elif unverified == 0 and fatal_blocked == 0 and failed == 0 and degraded_blocked > 0:
        lines.append("Verified direct settings are complete; optional capabilities degraded (see guidance).")
    elif unverified == 0 and fatal_blocked == 0 and failed == 0:
        lines.append("Verified direct settings are complete; pending work still needs owner action.")
    else:
        lines.append("Warning: Some changes failed, could not be verified, or were blocked.")

    # Persistent feedback: summary of modified assets and next steps
    applied_changes = [c for c in changes if c.get("status") in ("verified", "applied")]
    pending_changes = [c for c in changes if c.get("status") == "pending"]

    if applied_changes:
        lines.append("\nModified Assets:")
        for c in applied_changes:
            stg = c.get("stage", "general")
            op = c.get("operation")
            path = c.get("path") or c.get("reason") or "setting"
            desc = f"  - [{stg}] {op} {path}" if op else f"  - [{stg}] {path}"
            if len(desc) > 88:
                desc = desc[:85] + "..."
            lines.append(desc)

    lines.append("\nNext Steps:")
    if setup_pr:
        lines.append(f"  1. Review and merge draft Setup Pull Request: {setup_pr}")
        if pending_changes:
            lines.append("  2. Once the Setup PR is merged and CI passes, branch rulesets will activate.")
        else:
            lines.append("  2. Workflows and repository content will become active on default branch.")
    elif pending_changes:
        lines.append("  1. Complete owner action on pending gates (merge Setup PR / provide evidence PR).")
        lines.append("  2. Re-run setup to verify and enforce branch protection rulesets.")
    elif fatal_blocked > 0:
        lines.append("  1. Inspect the blocked changes and error guidance above.")
        lines.append("  2. Resolve prerequisites before re-applying.")
    else:
        lines.append("  - Repository setup is complete and active. Push commits or open PRs to trigger CI.")

    return "\n".join(lines)


def execute_apply(
    api: GitHubApi,
    repository: str,
    policy: Dict[str, Any],
    out_stream: Optional[TextIO] = None,
    accepted_dry_run: Optional[Dict[str, Any]] = None,
    accepted_plan: Optional[Dict[str, Any]] = None,
    profile: Optional[Union[Profile, str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Execute policy apply with writes and log change outcomes in real-time."""
    if accepted_dry_run is None and accepted_plan is not None:
        accepted_dry_run = accepted_plan
    if not isinstance(accepted_dry_run, dict):
        raise ApiError("Direct setting writes without an accepted Dry-Run are blocked; inspect a Dry-Run first.")

    out_s = out_stream if out_stream is not None else sys.stdout

    out_s.write(f"\nApplying changes to {repository}...\n")
    out_s.flush()

    result = run_policy(api, repository, policy, apply=True, accepted_dry_run=accepted_dry_run)
    if profile is not None:
        result["profile"] = profile
    elif isinstance(accepted_dry_run, dict) and "profile" in accepted_dry_run:
        result["profile"] = accepted_dry_run["profile"]

    changes = result.get("changes", [])
    for change in changes:
        status = change.get("status", "unknown")
        stage = change.get("stage", "general")
        op = change.get("operation")
        path = change.get("path")
        val_run = change.get("validation_run_id")

        if op and path:
            msg = f"  [{status}] {stage}: {op} {path}"
            if val_run:
                msg += f" (validation run: {val_run})"
            if status in ("failed", "blocked", "unverified") and change.get("reason"):
                msg += f" — {change['reason']}"
            out_s.write(msg + "\n")
        else:
            reason = change.get("reason", "unknown")
            out_s.write(f"  [{status}] {stage}: {reason}\n")
        out_s.flush()

    out_s.write(format_apply_summary(result, profile=profile) + "\n")
    out_s.flush()

    return result


def run_interactive_apply(
    session: Any,
    policy: Dict[str, Any],
    profile: Optional[Union[Profile, str, Dict[str, Any]]] = None,
) -> int:
    """Run Dry-Run, prompt confirmation, and apply execution."""
    out_s = getattr(session, "out_stream", None) or sys.stdout
    err_s = getattr(session, "err_stream", None) or sys.stderr
    in_s = getattr(session, "in_stream", None) or sys.stdin
    api = getattr(session, "api", None)
    repo = getattr(session, "repository", None)

    if api is None or not isinstance(repo, str):
        err_s.write("Error: Active session with valid api client and repository required\n")
        return 1

    active_prof = profile or getattr(session, "active_profile", None) or policy.get("profile")

    # 1. Execute Dry-Run
    try:
        dry_run_result = execute_dry_run(api, repo, policy)
        if active_prof is not None:
            dry_run_result["profile"] = (
                active_prof.id if hasattr(active_prof, "id") else str(active_prof)
            )
    except ApiError as error:
        err_s.write(f"Error during dry-run preflight: {error}\n")
        return 1
    except Exception as error:
        err_s.write(f"Unexpected error during dry-run: {error}\n")
        return 1

    # 2. Display summary
    display_dry_run_summary(dry_run_result, out_stream=out_s, profile=active_prof)

    changes = dry_run_result.get("changes", [])
    if any(is_fatal_block(change) for change in changes):
        return 2
    if not changes:
        out_s.write("Repository is already configured as declared. No changes to apply.\n")
        out_s.flush()
        return 0

    # 3. Prompt confirmation
    confirmed = prompt_apply_confirmation(in_stream=in_s, out_stream=out_s)
    if not confirmed:
        out_s.write("Apply cancelled. No changes were applied to the repository.\n")
        out_s.flush()
        return 0

    # Save accepted Dry-Run JSON
    dry_run_path = getattr(session, "dry_run_path", None) or Path(".local/setup-dry-run.json")
    try:
        Path(dry_run_path).parent.mkdir(parents=True, exist_ok=True)
        with open(dry_run_path, "w", encoding="utf-8") as f:
            json.dump(dry_run_result, f, indent=2)
            f.write("\n")
        out_s.write(f"Accepted Dry-Run result saved to {dry_run_path}\n")
        out_s.flush()
    except Exception as exc:
        err_s.write(f"Warning: Could not save dry-run result to {dry_run_path}: {exc}\n")
        err_s.flush()

    # 4. Execute apply
    try:
        apply_result = execute_apply(
            api,
            repo,
            policy,
            out_stream=out_s,
            accepted_dry_run=dry_run_result,
            profile=active_prof,
        )
    except ApiError as error:
        err_s.write(f"Error: {error}\n")
        err_s.flush()
        return 1
    except Exception as error:
        err_s.write(f"Unexpected error applying policy: {error}\n")
        err_s.flush()
        return 1

    apply_changes = apply_result.get("changes", [])
    if any(c.get("status") in ("unverified", "failed") or is_fatal_block(c) for c in apply_changes):
        return 2

    return 0
