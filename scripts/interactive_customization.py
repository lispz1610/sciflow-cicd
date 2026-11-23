"""Selective customization menu controller and interactive stages router for sciflow-cicd."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
from typing import Any, Dict, Optional, TextIO, TYPE_CHECKING

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

if TYPE_CHECKING:
    try:
        from scripts.interactive import Session
    except ImportError:
        from interactive import Session

try:
    from scripts.prompts import prompt_text, PromptExit
    from scripts.setup_profiles import (
        Profile,
        recommend_profile,
        profile_to_policy,
    )
    from scripts.policy_assembly import assemble_and_persist_policy
    from scripts.interactive_features import prompt_features_stage
    from scripts.interactive_repository_content import prompt_merge_stage
    from scripts.interactive_actions_security import prompt_actions_security_stage
    from scripts.interactive_dependabot import prompt_dependabot_stage
    from scripts.interactive_workflows_rulesets import (
        prompt_workflows_stage,
        prompt_rulesets_stage,
    )
    from scripts.interactive_apply import run_interactive_apply, classify_change_diff
    from scripts.configure_github_repo import SECURITY_FEATURES
except ImportError:
    from prompts import prompt_text, PromptExit
    from setup_profiles import (
        Profile,
        recommend_profile,
        profile_to_policy,
    )
    from policy_assembly import assemble_and_persist_policy
    from interactive_features import prompt_features_stage
    from interactive_repository_content import prompt_merge_stage
    from interactive_actions_security import prompt_actions_security_stage
    from interactive_dependabot import prompt_dependabot_stage
    from interactive_workflows_rulesets import (
        prompt_workflows_stage,
        prompt_rulesets_stage,
    )
    from interactive_apply import run_interactive_apply, classify_change_diff
    from configure_github_repo import SECURITY_FEATURES


CUSTOMIZATION_SECTIONS = [
    ("1", "f", "Features", "Issues, Wiki, Projects collaboration settings"),
    ("2", "m", "Merge Hygiene", "Pull request merge methods and branch hygiene"),
    ("3", "s", "Security & CodeQL", "Actions permissions, Dependabot, CodeQL scanning"),
    ("4", "w", "Workflows & CI", "CI test matrix and workflow templates"),
    ("5", "g", "Governance / Ruleset Gates", "Branch rulesets and required merge checks"),
    ("6", "r", "Review & Proceed to Dry-Run", "Review configuration and run preflight dry-run"),
    ("7", "c", "Cancel", "Discard modifications and exit setup"),
]

SECTION_SHORTCUT_MAP: Dict[str, str] = {
    "1": "1",
    "f": "1",
    "features": "1",
    "2": "2",
    "m": "2",
    "merge": "2",
    "3": "3",
    "s": "3",
    "security": "3",
    "4": "4",
    "w": "4",
    "workflows": "4",
    "ci": "4",
    "5": "5",
    "g": "5",
    "governance": "5",
    "gates": "5",
    "rulesets": "5",
    "6": "6",
    "r": "6",
    "review": "6",
    "d": "6",
    "dry-run": "6",
    "7": "7",
    "c": "7",
    "cancel": "7",
    "q": "7",
    "quit": "7",
}


def format_customization_menu() -> str:
    """Format compact selective customization menu (at most standard terminal width)."""
    lines = [
        "Selective Customization Menu:",
        "  1. Features                   - Issues, Wiki, Projects collaboration settings",
        "  2. Merge Hygiene               - Pull request merge methods and branch hygiene",
        "  3. Security & CodeQL           - Actions permissions, Dependabot, CodeQL scanning",
        "  4. Workflows & CI              - CI test matrix and workflow templates",
        "  5. Governance / Ruleset Gates  - Branch rulesets and required merge checks",
        "  6. Review & Proceed to Dry-Run - Review configuration and run preflight dry-run",
        "  7. Cancel                      - Discard modifications and exit setup",
    ]
    return "\n".join(lines)


def prompt_customization_selection(
    in_stream: Optional[TextIO] = None,
    out_stream: Optional[TextIO] = None,
    default: Optional[str] = "6",
    raise_on_interrupt: bool = False,
) -> Optional[str]:
    """Prompt maintainer for customization section selection with shortcut acceleration.

    Returns:
        Normalized section key string ("1" through "7") or None on interruption/EOF.
    """
    out_s = out_stream if out_stream is not None else sys.stdout

    while True:
        resp = prompt_text(
            "Select a section to configure [1-7, or f/m/s/w/g/r/c]",
            default=default,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_stream,
            out_stream=out_s,
        )
        if resp is None:
            return None

        clean = resp.strip().lower()
        if clean in SECTION_SHORTCUT_MAP:
            return SECTION_SHORTCUT_MAP[clean]

        out_s.write(
            "Invalid selection. Please enter a number between 1 and 7, or section key (f/m/s/w/g/r/c).\n"
        )
        out_s.flush()


def _ensure_resume_defaults(session: Any) -> None:
    """Ensure session defaults are pre-populated from live inventory when resume mode is active."""
    if not getattr(session, "resume_mode", False):
        return

    existing_defaults = getattr(session, "defaults", None)
    if not existing_defaults:
        inventory_data = getattr(session, "inventory", None) or {}
        try:
            from scripts.interactive_defaults import extract_defaults_from_inventory
        except ImportError:
            try:
                from interactive_defaults import extract_defaults_from_inventory
            except ImportError:
                extract_defaults_from_inventory = None

        if extract_defaults_from_inventory is not None:
            session.defaults = extract_defaults_from_inventory(inventory_data)


def run_customization(
    session: Any,
    active_profile: Optional[Profile] = None,
) -> int:
    """Selective customization menu controller routing maintainers to domain stages.

    Allows maintainers to jump directly to any configuration section, modify it,
    and return to the menu or proceed directly to Dry-Run. Pre-populates stage
    defaults from live inventory when resume state is active.
    """
    repo = getattr(session, "repository", None)
    if not isinstance(repo, str):
        return 0

    out_s = getattr(session, "out_stream", None) or sys.stdout
    err_s = getattr(session, "err_stream", None) or sys.stderr
    in_s = getattr(session, "in_stream", None) or sys.stdin
    repo_dir = getattr(session, "repo_dir", None)

    # Pre-populate defaults from live inventory if in resume mode
    _ensure_resume_defaults(session)

    # Resolve active setup profile
    if active_profile is None:
        active_profile = getattr(session, "active_profile", None)
    if active_profile is None:
        active_profile = recommend_profile(
            repo_dir=repo_dir,
            inventory=getattr(session, "inventory", None),
        )
    session.active_profile = active_profile

    # Generate baseline stages from active profile
    base_policy = profile_to_policy(
        active_profile,
        repository=repo,
        repo_dir=repo_dir,
        inventory=getattr(session, "inventory", None),
    )
    stages_by_name: Dict[str, Dict[str, Any]] = {
        s["name"]: copy.deepcopy(s)
        for s in base_policy.get("stages", [])
        if isinstance(s, dict) and "name" in s
    }

    # If resume mode is active, align baseline stages with detected inventory settings
    if getattr(session, "resume_mode", False) and getattr(session, "defaults", None):
        defaults = session.defaults
        if defaults.get("features") is not None:
            enabled_feats = list(defaults.get("features", []))
            existing_feats = stages_by_name.get("features", {})
            existing_disable = existing_feats.get("disable", [])
            preserved_disable = [d for d in existing_disable if d not in enabled_feats]
            if "wiki" not in enabled_feats and "wiki" not in preserved_disable and "wiki" in existing_disable:
                preserved_disable.append("wiki")
            feat_stage: Dict[str, Any] = {
                "name": "features",
                "enable": enabled_feats,
            }
            if preserved_disable:
                feat_stage["disable"] = preserved_disable
            stages_by_name["features"] = feat_stage
        if defaults.get("merge"):
            merge_settings = dict(defaults["merge"])
            if any(merge_settings.get(k) is True for k in ("allow_merge_commit", "allow_squash_merge", "allow_rebase_merge")):
                stages_by_name["merge"] = {
                    "name": "merge",
                    "settings": merge_settings,
                }
        if defaults.get("actions"):
            actions_policy_allowed = {
                "allowed_actions",
                "sha_pinning_required",
                "github_owned_allowed",
                "verified_allowed",
                "patterns_allowed",
                "workflow_permissions",
                "default_workflow_permissions",
            }
            actions_settings = {
                k: v for k, v in defaults["actions"].items()
                if k in actions_policy_allowed
            }
            if actions_settings.get("allowed_actions") == "selected":
                selected_keys = {"github_owned_allowed", "verified_allowed", "patterns_allowed"}
                if not selected_keys <= set(actions_settings):
                    actions_settings.pop("allowed_actions", None)
                    for sk in selected_keys:
                        actions_settings.pop(sk, None)
            if actions_settings:
                stages_by_name["actions-policy"] = {
                    "name": "actions-policy",
                    "settings": actions_settings,
                }
        if defaults.get("security"):
            valid_sec = [s for s in defaults["security"] if s in SECURITY_FEATURES]
            if valid_sec:
                stages_by_name["security"] = {
                    "name": "security",
                    "enable": valid_sec,
                }

    out_s.write("\nEntering selective customization mode...\n")
    out_s.flush()

    while True:
        out_s.write(format_customization_menu() + "\n")
        out_s.flush()

        try:
            choice = prompt_customization_selection(
                in_stream=in_s,
                out_stream=out_s,
                default="6",
                raise_on_interrupt=True,
            )
        except (PromptExit, KeyboardInterrupt, EOFError):
            out_s.write("Setup cancelled.\n")
            out_s.flush()
            return 0

        if choice is None or choice == "7":
            out_s.write("Setup cancelled.\n")
            out_s.flush()
            return 0

        if choice == "1":
            try:
                feature_stage = prompt_features_stage(session)
                if feature_stage is not None:
                    stages_by_name["features"] = feature_stage
                    out_s.write("Features configuration updated.\n")
                    out_s.flush()
            except (PromptExit, KeyboardInterrupt):
                out_s.write("\nFeatures customization cancelled; returning to menu.\n")
                out_s.flush()

        elif choice == "2":
            try:
                merge_stage = prompt_merge_stage(session, raise_on_interrupt=True)
                if merge_stage is not None:
                    stages_by_name["merge"] = merge_stage
                    out_s.write("Merge hygiene configuration updated.\n")
                    out_s.flush()
            except (PromptExit, KeyboardInterrupt):
                out_s.write("\nMerge hygiene customization cancelled; returning to menu.\n")
                out_s.flush()

        elif choice == "3":
            try:
                sec_stages = prompt_actions_security_stage(session)
                if sec_stages is not None:
                    stages_by_name.pop("actions", None)
                    stages_by_name.pop("actions-policy", None)
                    stages_by_name.pop("security", None)
                    stages_by_name.pop("codeql", None)
                    for s in sec_stages:
                        stages_by_name[s["name"]] = s
                    dependabot_default = None
                    if getattr(session, "resume_mode", False):
                        res_defaults = getattr(session, "defaults", {}) or {}
                        dependabot_default = bool(isinstance(res_defaults, dict) and res_defaults.get("dependabot"))
                    dep_stage = prompt_dependabot_stage(session, default=dependabot_default)
                    if dep_stage is not None:
                        stages_by_name[dep_stage["name"]] = dep_stage
                    else:
                        stages_by_name.pop("dependabot", None)
                    out_s.write("Security & CodeQL configuration updated.\n")
                    out_s.flush()
            except (PromptExit, KeyboardInterrupt):
                out_s.write("\nSecurity customization cancelled; returning to menu.\n")
                out_s.flush()

        elif choice == "4":
            try:
                wf_stage = prompt_workflows_stage(
                    session,
                    raise_on_interrupt=True,
                    root_dir=str(repo_dir) if repo_dir else None,
                )
                if wf_stage is not None:
                    stages_by_name.pop("ci-profile", None)
                    stages_by_name[wf_stage["name"]] = wf_stage
                    if session is not None:
                        session.staged_workflows = wf_stage

                out_s.write("Workflows & CI configuration updated.\n")
                out_s.flush()
            except (PromptExit, KeyboardInterrupt):
                out_s.write("\nWorkflows customization cancelled; returning to menu.\n")
                out_s.flush()

        elif choice == "5":
            try:
                gate_stage = prompt_rulesets_stage(
                    session,
                    raise_on_interrupt=True,
                    workflows_stage=stages_by_name.get("workflows"),
                )
                if gate_stage is not None:
                    stages_by_name["gates"] = gate_stage
                    out_s.write("Governance / Ruleset Gates configuration updated.\n")
                    out_s.flush()
            except (PromptExit, KeyboardInterrupt):
                out_s.write("\nGovernance customization cancelled; returning to menu.\n")
                out_s.flush()

        elif choice == "6":
            # Review and proceed to Dry-Run
            stages_list = list(stages_by_name.values())
            if not stages_list:
                out_s.write("No configuration changes selected. Setup completed without modifications.\n")
                out_s.flush()
                return 0

            try:
                policy, _ = assemble_and_persist_policy(session, stages_list)
            except ValueError as error:
                err_s.write(f"Error assembling policy: {error}\n")
                err_s.flush()
                return 1

            return run_interactive_apply(session, policy, profile=active_profile)
