"""Merge and authored repository content questions for Interactive Setup."""

from __future__ import annotations

import re
from urllib.parse import quote
from typing import TYPE_CHECKING

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
    ("allow_merge_commit", "Merge commits", "preserves all commits and creates a merge commit"),
    ("allow_squash_merge", "Squash merge", "combines the pull request into one commit"),
    ("allow_rebase_merge", "Rebase merge", "replays commits on the base branch"),
    ("delete_branch_on_merge", "Delete branch after merge", "removes the merged head branch"),
    ("allow_update_branch", "Update pull request branch", "lets users bring the head branch up to date"),
)


def _state(value):
    return "enabled" if value is True else "disabled" if value is False else "unknown"


def prompt_merge_stage(session: Session, raise_on_interrupt: bool = False):
    """Offer explicit merge-setting changes, leaving settings alone when skipped."""
    observed = (session.inventory or {}).get("merge", {})
    if not isinstance(observed, dict):
        observed = {}
    output = session.out_stream
    output.write("\nMerge settings determine how pull requests become commits.\n")
    for key, label, effect in MERGE_OPTIONS:
        output.write(f"  {label}: {_state(observed.get(key))}; enabling it {effect}.\n")
    output.write("Keep at least one merge method enabled. No leaves every current merge setting unchanged.\n")
    output.write("Recommended default: No; change these only when your repository needs different merge behavior.\n")
    output.flush()
    if not prompt_confirm("Change merge settings?", default=False, raise_on_interrupt=raise_on_interrupt,
                          in_stream=session.in_stream, out_stream=output):
        return None

    settings = {}
    for key, label, effect in MERGE_OPTIONS:
        current = observed.get(key)
        output.write(f"{label}: enabling it {effect}. Current: {_state(current)}.\n")
        output.flush()
        selected = prompt_confirm(f"Enable {label}?", default=current is True,
                                  raise_on_interrupt=raise_on_interrupt,
                                  in_stream=session.in_stream, out_stream=output)
        if selected is None:
            return None
        if selected != current:
            settings[key] = selected

    if not any(settings.get(key, observed.get(key)) for key, _, _ in MERGE_OPTIONS[:3]):
        output.write("Warning: at least one merge method must stay enabled. Merge settings were skipped.\n")
        output.flush()
        return None
    return {"name": "merge", "settings": settings} if settings else None


def prompt_content_stages(session: Session, raise_on_interrupt: bool = False):
    """Collect caller-authored labels and files only when selected."""
    output = session.out_stream
    output.write("\nRepository content adds labels and owner-authored files to support collaboration.\n")
    output.write("Current labels and authored files are inspected by name or path after you select one.\n")
    output.write("  Labels organize Issues and pull requests. A label with the same name may have its color or description updated.\n")
    output.write("  Authored files provide Issue forms, PR templates, or docs from local source files. Existing files are updated only after you confirm their current blob SHA; this guards against overwriting others' work.\n")
    output.write("No leaves repository content unchanged. Recommended default: No.\n")
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
        items = []
        while True:
            name = prompt_text("Label name", required=True, raise_on_interrupt=raise_on_interrupt,
                               in_stream=session.in_stream, out_stream=output)
            if name is None:
                return None
            label_path = f"/repos/{session.repository}/labels/{quote(name, safe='')}"
            status, current = session.api.request("GET", label_path)
            if status == 200 and isinstance(current, dict):
                output.write(f"Current label {name}: color {current.get('color', 'unknown')}; description {current.get('description', '')}. Its color and description may be updated.\n")
            elif status == 404:
                current = {}
                output.write(f"Label {name} does not exist; it will be created.\n")
            else:
                raise ApiError(f"cannot inspect label {name}: HTTP {status}")
            output.flush()
            while True:
                color = prompt_text("Label color (six hex digits, without #)",
                                    default=current.get("color"), required=True,
                                    raise_on_interrupt=raise_on_interrupt,
                                    in_stream=session.in_stream, out_stream=output)
                if color is None:
                    return None
                if re.fullmatch(r"[0-9a-fA-F]{6}", color):
                    break
                output.write("Please enter exactly six hexadecimal digits.\n")
                output.flush()
            while True:
                description = prompt_text("Label description (up to 100 characters)",
                                          default=current.get("description"),
                                          raise_on_interrupt=raise_on_interrupt,
                                          in_stream=session.in_stream, out_stream=output)
                if description is None:
                    return None
                if len(description) <= 100:
                    break
                output.write("Please use at most 100 characters.\n")
                output.flush()
            items.append({"name": name, "color": color, "description": description})
            if not prompt_confirm("Add another label?", default=False,
                                  raise_on_interrupt=raise_on_interrupt,
                                  in_stream=session.in_stream, out_stream=output):
                break
        stages.append({"name": "labels", "items": items})

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
