"""Merge and authored repository content questions for Interactive Setup."""

from __future__ import annotations

import re
from urllib.parse import quote
from typing import TYPE_CHECKING, Optional, Dict, Any, List

try:
    from scripts.prompts import PromptExit, prompt_confirm, prompt_multichoice, prompt_text
    from scripts.configure_github_repo import ApiError
except ImportError:
    from prompts import PromptExit, prompt_confirm, prompt_multichoice, prompt_text
    from configure_github_repo import ApiError

if TYPE_CHECKING:
    try:
        from scripts.interactive import Session
    except ImportError:
        from interactive import Session


MERGE_OPTIONS = (
    ("allow_squash_merge", "Squash merge", "combines the pull request into one commit"),
    ("delete_branch_on_merge", "Delete branch after merge", "removes the merged head branch"),
    ("allow_update_branch", "Update pull request branch", "lets users bring the head branch up to date"),
    ("allow_merge_commit", "Merge commits", "preserves all commits and creates a merge commit"),
    ("allow_rebase_merge", "Rebase merge", "replays commits on the base branch"),
)


def _state(value):
    return "enabled" if value is True else "disabled" if value is False else "unknown"


def prompt_merge_stage(session: Session, raise_on_interrupt: bool = False) -> Optional[Dict[str, Any]]:
    """Offer unified multichoice merge-setting and branch hygiene configuration.

    Args:
        session: Active onboarding Session instance.
        raise_on_interrupt: If True, raises PromptExit when cancelled.

    Returns:
        Policy stage dict {'name': 'merge', 'settings': {...}} or None if cancelled or skipped.
    """
    observed = (session.inventory or {}).get("merge", {})
    if not isinstance(observed, dict):
        observed = {}
    output = session.out_stream

    # Determine default indices
    if getattr(session, "resume_mode", False):
        defaults_merge = getattr(session, "defaults", {}).get("merge", {})
        if isinstance(defaults_merge, dict) and defaults_merge:
            if isinstance(defaults_merge.get("settings"), dict):
                settings_dict = defaults_merge["settings"]
            else:
                settings_dict = defaults_merge
        else:
            settings_dict = observed
        default_indices = [
            i for i, (key, _, _) in enumerate(MERGE_OPTIONS)
            if settings_dict.get(key) is True
        ]
    else:
        # Recommended baseline per ADR 0007: Squash merge, Delete branch, Update branch
        default_indices = [0, 1, 2]

    output.write("\nMerge settings determine how pull requests become commits (Squash merge: enabling it combines the pull request into one commit). Keep at least one merge method enabled.\n")
    output.write(
        f"Current: Squash merge: {_state(observed.get('allow_squash_merge'))}; "
        f"Delete branch after merge: {_state(observed.get('delete_branch_on_merge'))}; "
        f"Update pull request branch: {_state(observed.get('allow_update_branch'))}; "
        f"Merge commits: {_state(observed.get('allow_merge_commit'))}; "
        f"Rebase merge: {_state(observed.get('allow_rebase_merge'))}. "
        "Recommended default: No; change these only when your repository needs different merge behavior.\n"
    )
    output.flush()

    if not prompt_confirm("Change merge settings?", default=False, raise_on_interrupt=raise_on_interrupt,
                          in_stream=session.in_stream, out_stream=output):
        return None

    options = [label for _, label, _ in MERGE_OPTIONS]
    selected = prompt_multichoice(
        message="Select merge methods and branch hygiene settings to enable",
        options=options,
        default=default_indices,
        return_indices=True,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=session.in_stream,
        out_stream=output,
    )
    if selected is None:
        return None

    selected_set = {
        MERGE_OPTIONS[i][0] if isinstance(i, int) and 0 <= i < len(MERGE_OPTIONS) else i
        for i in selected
    }

    # At least one merge method (squash, merge commit, or rebase) must stay enabled
    method_keys = {"allow_squash_merge", "allow_merge_commit", "allow_rebase_merge"}
    if not any(k in selected_set for k in method_keys):
        output.write("Warning: at least one merge method must stay enabled. Merge settings were skipped.\n")
        output.flush()
        return None

    settings = {}
    for key, label, effect in MERGE_OPTIONS:
        current = observed.get(key)
        new_val = (key in selected_set)
        if current is None or new_val != current:
            settings[key] = new_val

    return {"name": "merge", "settings": settings} if settings else None


def prompt_content_stages(session: Session, raise_on_interrupt: bool = False):
    """Collect caller-authored labels and files only when selected."""
    output = session.out_stream
    output.write("\nRepository content adds labels and owner-authored files to support collaboration (Labels organize Issues and pull requests).\n")
    output.write("Authored files update only after confirming current blob SHA. Recommended default: No.\n")
    output.flush()
    if not prompt_confirm("Configure labels or authored files?", default=False,
                          raise_on_interrupt=raise_on_interrupt,
                          in_stream=session.in_stream, out_stream=output):
        return []

    selected = prompt_multichoice("Select repository content to configure", ["Labels", "Authored files"],
                                  return_indices=True, raise_on_interrupt=raise_on_interrupt,
                                  in_stream=session.in_stream, out_stream=output)
    if selected is None:
        return None
    stages = []
    if 0 in selected:
        try:
            from scripts.setup_profiles import CANONICAL_TRIAGE_LABELS
        except ImportError:
            from setup_profiles import CANONICAL_TRIAGE_LABELS

        output.write("Synchronizing 5 canonical triage labels: needs-triage, bug, enhancement, documentation, ready-for-agent.\n")
        output.flush()
        stages.append({
            "name": "labels",
            "items": [dict(label) for label in CANONICAL_TRIAGE_LABELS],
        })

    if 1 in selected:
        items = []
        while True:
            destination = prompt_text("Repository file path", required=True,
                                      raise_on_interrupt=raise_on_interrupt,
                                      in_stream=session.in_stream, out_stream=output)
            if destination is None:
                return None
            source = prompt_text("Local source file path", required=True,
                                 raise_on_interrupt=raise_on_interrupt,
                                 in_stream=session.in_stream, out_stream=output)
            if source is None:
                return None
            branch = (session.inventory or {}).get("repository", {}).get("default_branch")
            if not isinstance(branch, str) or not branch or branch == "unknown":
                raise ApiError("cannot inspect authored file without a known default branch")
            path = f"/repos/{session.repository}/contents/{quote(destination, safe='/')}?ref={quote(branch, safe='')}"
            status, body = session.api.request("GET", path)
            item = {"path": destination, "source": source}
            if status == 200 and isinstance(body, dict) and isinstance(body.get("sha"), str):
                output.write(f"Existing file {destination} has current blob SHA {body['sha']}.\n")
                output.write("Confirm this update only if you recognize the current file; Dry-Run will reject later drift.\n")
                output.flush()
                if not prompt_confirm("Update this existing file?", default=False,
                                      raise_on_interrupt=raise_on_interrupt,
                                      in_stream=session.in_stream, out_stream=output):
                    output.write("Existing file update skipped.\n")
                    output.flush()
                    if not prompt_confirm("Add another authored file?", default=False,
                                          raise_on_interrupt=raise_on_interrupt,
                                          in_stream=session.in_stream, out_stream=output):
                        break
                    continue
                item["expected_sha"] = body["sha"]
            elif status != 404:
                raise ApiError(f"cannot inspect existing file {destination}: HTTP {status}")
            else:
                output.write(f"File {destination} does not exist on {branch}; selecting it creates the file.\n")
                output.flush()
            items.append(item)
            if not prompt_confirm("Add another authored file?", default=False,
                                  raise_on_interrupt=raise_on_interrupt,
                                  in_stream=session.in_stream, out_stream=output):
                break
        if items:
            stages.append({"name": "files", "items": items})
    return stages
