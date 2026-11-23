"""Interactive questionnaire stage for GitHub Actions and Security configuration."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Optional, Sequence, List, TYPE_CHECKING

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

if TYPE_CHECKING:
    try:
        from scripts.interactive import Session
    except ImportError:
        from interactive import Session

try:
    from scripts.prompts import prompt_confirm, prompt_multichoice
    from scripts.configure_github_repo import SECURITY_FEATURES
except ImportError:
    from prompts import prompt_confirm, prompt_multichoice
    from configure_github_repo import SECURITY_FEATURES


SECURITY_OPTIONS = [
    ("vulnerability-alerts", "Vulnerability alerts"),
    ("automated-security-fixes", "Dependabot automated security fixes"),
    ("secret-scanning", "Secret scanning"),
    ("push-protection", "Secret scanning push protection"),
    ("private-vulnerability-reporting", "Private vulnerability reporting"),
    ("codeql", "CodeQL default setup"),
]


def prompt_actions_permissions(
    session: Session,
    default: Optional[bool] = None,
) -> Optional[bool]:
    """Prompt user whether to configure default GitHub Actions workflow permissions as read-only.

    Returns:
        True if read-only permissions should be configured,
        False if declined,
        None on EOF or prompt interruption.
    """
    if default is not None:
        effective_default = default
    elif session.resume_mode:
        actions_defaults = session.defaults.get("actions", {})
        perm = actions_defaults.get("default_workflow_permissions")
        if perm is None and isinstance(session.inventory, dict):
            perm = session.inventory.get("workflow_permissions", {}).get("default_workflow_permissions")
        effective_default = (perm == "read")
    else:
        effective_default = True

    inventory = session.inventory if isinstance(session.inventory, dict) else {}
    actions = inventory.get("actions", {})
    permissions = inventory.get("workflow_permissions", {})
    enabled = actions.get("enabled") if isinstance(actions, dict) else None
    allowed = actions.get("allowed_actions", "unknown") if isinstance(actions, dict) else "unknown"
    token = permissions.get("default_workflow_permissions", "unknown") if isinstance(permissions, dict) else "unknown"
    state = "enabled" if enabled is True else "disabled" if enabled is False else "unknown"
    session.out_stream.write("\nActions runs repository workflows. Repository or organization policy may restrict which Actions can run.\n")
    session.out_stream.write(f"Current Actions: {state}; allowed actions: {allowed}; default token permissions: {token}. Recommended default: {'Yes' if effective_default else 'No'}.\n")
    session.out_stream.flush()

    return prompt_confirm(
        "Configure default GitHub Actions workflow permissions (read-only)?",
        default=effective_default,
        in_stream=session.in_stream,
        out_stream=session.out_stream,
    )


def prompt_security_features(
    session: Session,
    default: Optional[Sequence[str]] = None,
) -> Optional[List[str]]:
    """Prompt user for enabling security capabilities (vulnerability alerts, Dependabot fixes, secret scanning, push protection, private vulnerability reporting, CodeQL).

    Returns:
        List of selected security feature identifiers (strings),
        None on EOF or prompt interruption.
    """
    key_to_index = {key: idx for idx, (key, _) in enumerate(SECURITY_OPTIONS)}
    label_to_key = {label.lower(): key for key, label in SECURITY_OPTIONS}
    for key, _ in SECURITY_OPTIONS:
        label_to_key[key.lower()] = key

    inventory = session.inventory if isinstance(session.inventory, dict) else {}
    caps = inventory.get("capabilities", {})
    repo_meta = inventory.get("repository", {})
    visibility = repo_meta.get("visibility") if isinstance(repo_meta, dict) else None

    alerts = inventory.get("vulnerability_alerts", {})
    fixes = inventory.get("automated_security_fixes", {})
    analysis = inventory.get("security_and_analysis", {})
    codeql = inventory.get("codeql", {})
    reporting = inventory.get("private_vulnerability_reporting", {})

    alerts_state = "enabled" if isinstance(alerts, dict) and alerts.get("status") == 204 else "disabled" if isinstance(alerts, dict) and alerts.get("status") == 404 else "unknown"
    fixes_state = "enabled" if isinstance(fixes, dict) and (fixes.get("status") == 200 or fixes.get("enabled") is True) else "disabled" if isinstance(fixes, dict) and fixes.get("status") == 404 else "unknown"
    scanning_status = analysis.get("secret_scanning", {}).get("status") if isinstance(analysis, dict) else None
    scanning_state = scanning_status if scanning_status in ("enabled", "disabled") else "unknown"
    push_status = analysis.get("secret_scanning_push_protection", {}).get("status") if isinstance(analysis, dict) else None
    push_state = push_status if push_status in ("enabled", "disabled") else "unknown"
    pvr_state = "enabled" if isinstance(reporting, dict) and reporting.get("enabled") is True else "disabled" if isinstance(reporting, dict) and reporting.get("enabled") is False else "unknown"
    codeql_state = "configured" if isinstance(codeql, dict) and codeql.get("state") in ("configured", "enforced") else "not configured" if isinstance(codeql, dict) and codeql.get("state") == "not-configured" else "unknown"

    if caps:
        if caps.get("vulnerability_alerts", {}).get("status") in ("unsupported", "inaccessible"):
            alerts_state = caps["vulnerability_alerts"]["status"]
        if caps.get("automated_security_fixes", {}).get("status") in ("unsupported", "inaccessible"):
            fixes_state = caps["automated_security_fixes"]["status"]
        if caps.get("secret_scanning", {}).get("status") in ("unsupported", "inaccessible"):
            scanning_state = caps["secret_scanning"]["status"]
        if caps.get("push_protection", {}).get("status") in ("unsupported", "inaccessible"):
            push_state = caps["push_protection"]["status"]
        if caps.get("private_vulnerability_reporting", {}).get("status") in ("unsupported", "inaccessible"):
            pvr_state = caps["private_vulnerability_reporting"]["status"]
        if caps.get("codeql", {}).get("status") in ("unsupported", "inaccessible"):
            codeql_state = caps["codeql"]["status"]

    if default is not None:
        default_keys = list(default)
    elif session.resume_mode:
        active_security = session.defaults.get("security", [])
        if not active_security and isinstance(session.inventory, dict):
            active_security = []
            if alerts_state == "enabled":
                active_security.append("vulnerability-alerts")
            if fixes_state == "enabled":
                active_security.append("automated-security-fixes")
            if scanning_state == "enabled":
                active_security.append("secret-scanning")
            if push_state == "enabled":
                active_security.append("push-protection")
            if pvr_state == "enabled":
                active_security.append("private-vulnerability-reporting")
            if codeql_state == "configured":
                active_security.append("codeql")
        default_keys = [key for key, _ in SECURITY_OPTIONS if key in active_security]
    else:
        default_keys = [
            key for key, _ in SECURITY_OPTIONS
            if caps.get(key.replace("-", "_"), {}).get("status") not in ("unsupported", "inaccessible")
            and not (key == "private-vulnerability-reporting" and visibility == "private")
        ]

    default_indices = [key_to_index[k] for k in default_keys if k in key_to_index]
    options = [label for _, label in SECURITY_OPTIONS]

    session.out_stream.write("\nConfigure repository security, vulnerability alerts, and code analysis (plan or organization policy may apply).\n")
    rec_summary = ", ".join(SECURITY_OPTIONS[i][1] for i in default_indices) if default_indices else "none"
    session.out_stream.write(
        f"Current security: Vulnerability alerts: {alerts_state}; Dependabot fixes: {fixes_state}; Secret scanning: {scanning_state}; Push protection: {push_state}; Private vulnerability reporting: {pvr_state}; CodeQL default setup: {codeql_state}. Recommended default: {rec_summary}.\n"
    )
    session.out_stream.flush()

    selected = prompt_multichoice(
        "Select repository security features to enable:",
        options=options,
        default=default_indices,
        return_indices=True,
        in_stream=session.in_stream,
        out_stream=session.out_stream,
    )
    if selected is None:
        return None

    selected_keys = []
    for item in selected:
        if isinstance(item, int):
            if 0 <= item < len(SECURITY_OPTIONS):
                selected_keys.append(SECURITY_OPTIONS[item][0])
        elif isinstance(item, str):
            clean = item.strip().lower()
            if clean in label_to_key:
                selected_keys.append(label_to_key[clean])
            elif item in key_to_index:
                selected_keys.append(item)

    return selected_keys


# Alias for compatibility
prompt_security_stage = prompt_security_features


def build_actions_payload() -> dict:
    """Construct payload for actions stage."""
    return {"name": "actions"}


def build_codeql_payload() -> dict:
    """Construct payload for codeql stage."""
    return {"name": "codeql"}


def build_security_payload(features: Sequence[str]) -> Optional[dict]:
    """Construct payload for security stage with selected features, excluding CodeQL."""
    sec_features = [f for f in features if f in SECURITY_FEATURES]
    if not sec_features:
        return None

    # Deduplicate while preserving order
    unique_features = list(dict.fromkeys(sec_features))
    return {
        "name": "security",
        "enable": unique_features,
    }


def build_actions_security_stages(
    actions: bool,
    security_features: Sequence[str],
) -> List[dict]:
    """Construct the list of policy stages for the selected actions and security options."""
    stages = []

    if actions:
        stages.append(build_actions_payload())

    sec_payload = build_security_payload(security_features)
    if sec_payload is not None:
        stages.append(sec_payload)

    if "codeql" in security_features:
        stages.append(build_codeql_payload())

    return stages


def prompt_actions_security_stage(session: Session) -> Optional[List[dict]]:
    """Execute interactive questionnaire for Actions permissions and Security capabilities.

    Returns:
        List of constructed policy stage dictionaries, or None on abort/interruption.
    """
    actions_enabled = prompt_actions_permissions(session)
    if actions_enabled is None:
        return None

    selected_security = prompt_security_features(session)
    if selected_security is None:
        return None

    return build_actions_security_stages(
        actions=actions_enabled,
        security_features=selected_security,
    )
