from __future__ import annotations

from pathlib import Path
import sys
from typing import Optional, TextIO, Dict, Any, List, TYPE_CHECKING

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

if TYPE_CHECKING:
    try:
        from scripts.interactive import Session
    except ImportError:
        from interactive import Session

try:
    from scripts.prompts import prompt_multichoice
except ImportError:
    from prompts import prompt_multichoice

FEATURE_KEYS = ("issues", "wiki", "projects")
FEATURE_LABELS = {
    "issues": "Issues",
    "wiki": "Wiki",
    "projects": "Projects",
}
FEATURE_OPTIONS = [FEATURE_LABELS[k] for k in FEATURE_KEYS]
LABEL_TO_KEY = {v: k for k, v in FEATURE_LABELS.items()}


def prompt_features_stage(
    session: Optional[Session] = None,
    in_stream: Optional[TextIO] = None,
    out_stream: Optional[TextIO] = None,
) -> Optional[Dict[str, Any]]:
    """Prompt the user for repository features to enable (Issues, Wiki, Projects) with explicit deactivation.

    Args:
        session: Active onboarding Session instance.
        in_stream: Optional input stream override.
        out_stream: Optional output stream override.

    Returns:
        A features stage dictionary matching standard schema:
            {"name": "features", "enable": [...], "disable": [...]}
        or None if no features are selected or input was aborted.
    """
    in_s = in_stream if in_stream is not None else (getattr(session, "in_stream", None) or sys.stdin)
    out_s = out_stream if out_stream is not None else (getattr(session, "out_stream", None) or sys.stdout)

    default_selection: Optional[List[str]] = None
    if session is not None and getattr(session, "resume_mode", False):
        active_features = set()
        defaults_dict = getattr(session, "defaults", None) or {}
        if "features" in defaults_dict:
            features_setting = defaults_dict["features"]
            if isinstance(features_setting, (list, tuple, set)):
                active_features = {f.lower() for f in features_setting if isinstance(f, str)}
            default_selection = [FEATURE_LABELS[k] for k in FEATURE_KEYS if k in active_features]
        else:
            inventory_dict = getattr(session, "inventory", None) or {}
            inv_features = inventory_dict.get("features", {})
            if isinstance(inv_features, dict):
                active_features = {k.lower() for k, v in inv_features.items() if v is True}
            elif isinstance(inv_features, (list, tuple, set)):
                active_features = {k.lower() for k in inv_features if isinstance(k, str)}
            if active_features:
                default_selection = [FEATURE_LABELS[k] for k in FEATURE_KEYS if k in active_features]
            else:
                default_selection = []
    else:
        # Fresh mode default: Issues enabled, Wiki disabled per ADR 0005
        default_selection = [FEATURE_LABELS["issues"]]

    inventory_features = getattr(session, "inventory", {}).get("features", {}) if session else {}
    live_parts = []
    for key in FEATURE_KEYS:
        current = inventory_features.get(key) if isinstance(inventory_features, dict) else None
        state = "enabled" if current is True else "disabled" if current is False else "unknown"
        live_parts.append(f"{FEATURE_LABELS[key]}: {state}")
    live_summary = "; ".join(live_parts)
    rec_summary = ", ".join(default_selection) if default_selection else "none"

    out_s.write("\nConfigure repository collaboration features (Issues tracks work, Wiki provides documentation, Projects organizes work).\n")
    out_s.write(f"Current features: {live_summary}. Recommended default: {rec_summary} (Wiki disabled per ADR 0005).\n")
    out_s.flush()

    selected = prompt_multichoice(
        message="Select repository features to enable (unselected features will be deactivated):",
        options=FEATURE_OPTIONS,
        default=default_selection,
        in_stream=in_s,
        out_stream=out_s,
    )

    if not selected:
        return None

    resolved_keys = {
        LABEL_TO_KEY.get(item, item.lower())
        for item in selected
    }
    enable_list = [k for k in FEATURE_KEYS if k in resolved_keys]
    disable_list = [k for k in FEATURE_KEYS if k not in resolved_keys]

    if not enable_list and not disable_list:
        return None

    payload: Dict[str, Any] = {
        "name": "features",
        "enable": enable_list,
    }
    if disable_list:
        payload["disable"] = disable_list

    return payload
