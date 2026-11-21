"""Extraction and state utilities for interactive session defaults from inventory."""

from __future__ import annotations

from typing import Any, Dict


def extract_defaults_from_inventory(inventory_data: dict) -> dict:
    """Extract default prompt selections from detected inventory state."""
    if not isinstance(inventory_data, dict):
        return {}

    features_dict = inventory_data.get("features", {})
    enabled_features = [
        name for name in ("issues", "wiki", "projects")
        if features_dict.get(name) is True
    ]

    merge_dict = {
        key: val for key, val in inventory_data.get("merge", {}).items()
        if isinstance(val, bool)
    }

    workflow = inventory_data.get("workflow_permissions", {})
    actions = inventory_data.get("actions", {})
    actions_defaults = {}
    if isinstance(workflow, dict):
        if "default_workflow_permissions" in workflow:
            actions_defaults["default_workflow_permissions"] = workflow["default_workflow_permissions"]
        if "can_approve_pull_request_reviews" in workflow:
            actions_defaults["can_approve_pull_request_reviews"] = workflow["can_approve_pull_request_reviews"]
    if isinstance(actions, dict):
        if "allowed_actions" in actions:
            actions_defaults["allowed_actions"] = actions["allowed_actions"]
        if "enabled" in actions:
            actions_defaults["enabled"] = actions["enabled"]

    enabled_security = []
    if inventory_data.get("vulnerability_alerts", {}).get("status") == 204:
        enabled_security.append("vulnerability-alerts")
    if (
        inventory_data.get("automated_security_fixes", {}).get("status") == 200
        or inventory_data.get("automated_security_fixes", {}).get("enabled") is True
    ):
        enabled_security.append("automated-security-fixes")

    sec_analysis = inventory_data.get("security_and_analysis", {})
    if isinstance(sec_analysis, dict):
        if sec_analysis.get("secret_scanning", {}).get("status") == "enabled":
            enabled_security.append("secret-scanning")
        if sec_analysis.get("secret_scanning_push_protection", {}).get("status") == "enabled":
            enabled_security.append("push-protection")

    if inventory_data.get("codeql", {}).get("state") in ("configured", "enforced"):
        enabled_security.append("codeql")

    if inventory_data.get("private_vulnerability_reporting", {}).get("enabled") is True:
        enabled_security.append("private-vulnerability-reporting")

    if inventory_data.get("dependency_graph", {}).get("status") == "enabled":
        enabled_security.append("dependency-graph")

    rulesets = inventory_data.get("rulesets")
    issue_state_gate = any(
        isinstance(item, dict) and item.get("name") == "issue-state-gate"
        and item.get("enforcement") == "active"
        and isinstance(item.get("rules"), list)
        and any(isinstance(rule, dict) and rule.get("type") == "required_status_checks"
                and isinstance(rule.get("parameters"), dict)
                and any(isinstance(check, dict) and check.get("context") == "Issue State Gate"
                        for check in rule["parameters"].get("required_status_checks", []))
                for rule in item["rules"])
        for item in rulesets
    ) if isinstance(rulesets, list) else False

    return {
        "features": enabled_features,
        "merge": merge_dict,
        "actions": actions_defaults,
        "security": enabled_security,
        "issue_state_gate": issue_state_gate,
        "agent_skills": inventory_data.get("agent_skills") is True,
    }


def clear_defaults() -> dict:
    """Return cleared/empty defaults for starting from scratch."""
    return {}
