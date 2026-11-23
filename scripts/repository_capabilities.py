"""Repository capabilities, account plan, and permission preflight detection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote


class CapabilityStatus(str, Enum):
    CONFIGURED = "configured"
    MISSING = "missing"
    UNSUPPORTED = "unsupported"
    INACCESSIBLE = "inaccessible"


@dataclass(frozen=True)
class CapabilityResult:
    name: str
    status: str
    reason: str
    guidance: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"status": self.status, "reason": self.reason}
        if self.guidance:
            d["guidance"] = self.guidance
        return d


def detect_repository_plan(api: Any, repo_data: dict) -> Tuple[str, Optional[dict]]:
    """Detect account plan for repository owner.

    Queries GET /user when the repository is owned by the authenticated caller
    to detect personal account plan (e.g. Pro), otherwise falls back to GET /users/{login}
    or GET /orgs/{login}.

    Returns (plan_name, plan_dict).
    """
    owner = repo_data.get("owner", {})
    if not isinstance(owner, dict) or not owner.get("login"):
        return ("unknown", None)

    owner_login = owner["login"]
    owner_type = owner.get("type", "User")

    if owner_type == "User":
        try:
            u_status, user_info = api.request("GET", "/user")
            if u_status == 200 and isinstance(user_info, dict):
                if user_info.get("login") == owner_login and isinstance(user_info.get("plan"), dict):
                    plan = user_info["plan"]
                    name = plan.get("name", "unknown").lower()
                    return (name, plan)
        except Exception:
            pass

    owner_path = "orgs" if owner_type == "Organization" else "users"
    path = f"/{owner_path}/{quote(owner_login, safe='')}"

    try:
        status, owner_info = api.request("GET", path)
        if status == 200 and isinstance(owner_info, dict):
            plan = owner_info.get("plan")
            if isinstance(plan, dict):
                name = plan.get("name", "unknown").lower()
                return (name, plan)
    except Exception:
        pass
    return ("unknown", None)


def detect_token_scopes(api: Any) -> Optional[List[str]]:
    """Detect OAuth/PAT permission scopes for the current token."""
    if api is None:
        return None
    if hasattr(api, "scopes") and api.scopes is not None:
        return list(api.scopes)
    try:
        status, _ = api.request("GET", "/user")
        if hasattr(api, "scopes") and api.scopes is not None:
            return list(api.scopes)
    except Exception:
        pass
    return getattr(api, "scopes", None)


def detect_caller_permissions(repo_data: dict, api: Any = None) -> dict:
    """Extract caller permissions from repository data and inspect token OAuth scopes."""
    perms = repo_data.get("permissions")
    result: Dict[str, Any] = {
        "admin": False,
        "push": False,
        "pull": False,
        "triage": False,
        "maintain": False,
        "scopes": None,
    }
    if isinstance(perms, dict):
        result.update({
            "admin": perms.get("admin") is True,
            "push": perms.get("push") is True,
            "pull": perms.get("pull") is True,
            "triage": perms.get("triage") is True,
            "maintain": perms.get("maintain") is True,
        })
    if api is not None:
        result["scopes"] = detect_token_scopes(api)
    return result


def classify_capabilities(
    repo_data: dict,
    plan_name: str,
    permissions: dict,
    inventory_data: dict,
) -> Dict[str, CapabilityResult]:
    """Classify repository capabilities into configured, missing, unsupported, inaccessible."""
    caps: Dict[str, CapabilityResult] = {}
    visibility = repo_data.get("visibility", "unknown")
    is_public = visibility == "public"
    is_private = visibility == "private"
    has_admin = permissions.get("admin") is True
    plan = plan_name.lower()
    is_enterprise = plan in ("business", "enterprise")
    scopes = permissions.get("scopes")
    has_repo_scope = scopes is None or "repo" in scopes or (not is_private and "public_repo" in scopes)
    has_security_events_scope = scopes is None or "security_events" in scopes or "repo" in scopes

    # 1. Vulnerability alerts
    alerts_inv = inventory_data.get("vulnerability_alerts", {})
    if not has_admin:
        caps["vulnerability_alerts"] = CapabilityResult(
            name="vulnerability_alerts",
            status=CapabilityStatus.INACCESSIBLE.value,
            reason="Token lacks repository administration permission to inspect or configure vulnerability alerts",
        )
    elif not has_repo_scope:
        caps["vulnerability_alerts"] = CapabilityResult(
            name="vulnerability_alerts",
            status=CapabilityStatus.INACCESSIBLE.value,
            reason="Token lacks 'repo' OAuth scope to inspect or configure vulnerability alerts",
            guidance="Regenerate token with 'repo' scope",
        )
    elif alerts_inv.get("status") in (200, 204) or alerts_inv.get("enabled") is True:
        caps["vulnerability_alerts"] = CapabilityResult(
            name="vulnerability_alerts",
            status=CapabilityStatus.CONFIGURED.value,
            reason="Vulnerability alerts are enabled",
        )
    else:
        caps["vulnerability_alerts"] = CapabilityResult(
            name="vulnerability_alerts",
            status=CapabilityStatus.MISSING.value,
            reason="Vulnerability alerts are available but not enabled",
            guidance="Enable vulnerability alerts in repository Settings > Code security and analysis",
        )

    # 2. Secret scanning
    sec_analysis = inventory_data.get("security_and_analysis", {})
    secret_status = (
        sec_analysis.get("secret_scanning", {}).get("status")
        if isinstance(sec_analysis, dict)
        else None
    )
    if is_private and not is_enterprise:
        caps["secret_scanning"] = CapabilityResult(
            name="secret_scanning",
            status=CapabilityStatus.UNSUPPORTED.value,
            reason="Secret scanning on private repositories requires GitHub Advanced Security (Enterprise plan)",
            guidance="Upgrade repository owner to GitHub Enterprise with Advanced Security",
        )
    elif not has_admin:
        caps["secret_scanning"] = CapabilityResult(
            name="secret_scanning",
            status=CapabilityStatus.INACCESSIBLE.value,
            reason="Token lacks repository administration permission to inspect or configure secret scanning",
        )
    elif not has_repo_scope:
        caps["secret_scanning"] = CapabilityResult(
            name="secret_scanning",
            status=CapabilityStatus.INACCESSIBLE.value,
            reason="Token lacks 'repo' OAuth scope to inspect or configure secret scanning",
            guidance="Regenerate token with 'repo' scope",
        )
    elif secret_status == "enabled":
        caps["secret_scanning"] = CapabilityResult(
            name="secret_scanning",
            status=CapabilityStatus.CONFIGURED.value,
            reason="Secret scanning is enabled",
        )
    else:
        caps["secret_scanning"] = CapabilityResult(
            name="secret_scanning",
            status=CapabilityStatus.MISSING.value,
            reason="Secret scanning is available but not enabled",
            guidance="Enable secret scanning in Settings > Code security and analysis",
        )

    # 3. CodeQL default setup
    codeql_inv = inventory_data.get("codeql", {})
    codeql_state = codeql_inv.get("state") if isinstance(codeql_inv, dict) else None
    if is_private and not is_enterprise:
        caps["codeql"] = CapabilityResult(
            name="codeql",
            status=CapabilityStatus.UNSUPPORTED.value,
            reason="CodeQL default setup on private repositories requires GitHub Advanced Security",
            guidance="Enable GitHub Advanced Security to use CodeQL on private repositories",
        )
    elif not has_admin:
        caps["codeql"] = CapabilityResult(
            name="codeql",
            status=CapabilityStatus.INACCESSIBLE.value,
            reason="Token lacks repository administration permission to inspect or configure CodeQL",
        )
    elif not has_security_events_scope:
        caps["codeql"] = CapabilityResult(
            name="codeql",
            status=CapabilityStatus.INACCESSIBLE.value,
            reason="Token lacks 'security_events' or 'repo' OAuth scope required for CodeQL",
            guidance="Regenerate token with 'security_events' or 'repo' scope",
        )
    elif codeql_state in ("configured", "enforced"):
        caps["codeql"] = CapabilityResult(
            name="codeql",
            status=CapabilityStatus.CONFIGURED.value,
            reason=f"CodeQL default setup is {codeql_state}",
        )
    else:
        caps["codeql"] = CapabilityResult(
            name="codeql",
            status=CapabilityStatus.MISSING.value,
            reason="CodeQL default setup is available but not configured",
            guidance="Configure CodeQL default setup via Policy or Settings > Code security and analysis",
        )

    # 4. Dependency graph
    graph_inv = inventory_data.get("dependency_graph", {})
    graph_enabled = isinstance(graph_inv, dict) and (
        graph_inv.get("status") == "enabled" or graph_inv.get("enabled") is True
    )
    if is_public:
        caps["dependency_graph"] = CapabilityResult(
            name="dependency_graph",
            status=CapabilityStatus.CONFIGURED.value,
            reason="Dependency graph is automatically enabled for public repositories",
        )
    elif graph_enabled:
        caps["dependency_graph"] = CapabilityResult(
            name="dependency_graph",
            status=CapabilityStatus.CONFIGURED.value,
            reason="Dependency graph is enabled",
        )
    elif alerts_inv.get("status") in (200, 204) or alerts_inv.get("enabled") is True:
        caps["dependency_graph"] = CapabilityResult(
            name="dependency_graph",
            status=CapabilityStatus.CONFIGURED.value,
            reason="Dependency graph is enabled via vulnerability alerts",
        )
    elif not has_admin:
        caps["dependency_graph"] = CapabilityResult(
            name="dependency_graph",
            status=CapabilityStatus.INACCESSIBLE.value,
            reason="Token lacks repository administration permission to inspect dependency graph",
        )
    elif not has_repo_scope:
        caps["dependency_graph"] = CapabilityResult(
            name="dependency_graph",
            status=CapabilityStatus.INACCESSIBLE.value,
            reason="Token lacks 'repo' OAuth scope to inspect dependency graph",
            guidance="Regenerate token with 'repo' scope",
        )
    else:
        caps["dependency_graph"] = CapabilityResult(
            name="dependency_graph",
            status=CapabilityStatus.MISSING.value,
            reason="Dependency graph is available for private repositories by enabling vulnerability alerts",
            guidance="Enable vulnerability alerts to activate dependency graph",
        )

    # 5. Rulesets
    rulesets_inv = inventory_data.get("rulesets", [])
    ruleset_endpoint_available = isinstance(rulesets_inv, list)
    if is_private and (plan == "free" or (plan == "unknown" and not ruleset_endpoint_available)):
        caps["rulesets"] = CapabilityResult(
            name="rulesets",
            status=CapabilityStatus.UNSUPPORTED.value,
            reason="Branch rulesets on private repositories require a Pro, Team, or Enterprise plan",
            guidance="Upgrade owner account to Pro, Team, or Enterprise plan for ruleset support",
        )
    elif not has_admin:
        caps["rulesets"] = CapabilityResult(
            name="rulesets",
            status=CapabilityStatus.INACCESSIBLE.value,
            reason="Token lacks repository administration permission to inspect or configure rulesets",
        )
    elif not has_repo_scope:
        caps["rulesets"] = CapabilityResult(
            name="rulesets",
            status=CapabilityStatus.INACCESSIBLE.value,
            reason="Token lacks 'repo' OAuth scope to inspect or configure rulesets",
            guidance="Regenerate token with 'repo' scope",
        )
    elif isinstance(rulesets_inv, list) and len(rulesets_inv) > 0:
        caps["rulesets"] = CapabilityResult(
            name="rulesets",
            status=CapabilityStatus.CONFIGURED.value,
            reason=f"Branch rulesets are configured ({len(rulesets_inv)} active)",
        )
    else:
        caps["rulesets"] = CapabilityResult(
            name="rulesets",
            status=CapabilityStatus.MISSING.value,
            reason="Branch rulesets are available but none are currently active",
            guidance="Configure branch rulesets using gates policy stage or Settings > Rules",
        )

    # 6. Dependency review
    if is_public:
        caps["dependency_review"] = CapabilityResult(
            name="dependency_review",
            status=CapabilityStatus.CONFIGURED.value,
            reason="Dependency review is supported for public repositories",
        )
    elif is_private and not is_enterprise:
        caps["dependency_review"] = CapabilityResult(
            name="dependency_review",
            status=CapabilityStatus.UNSUPPORTED.value,
            reason="Dependency review on private repositories requires GitHub Advanced Security",
            guidance="Enable GitHub Advanced Security to use Dependency Review on private repositories",
        )
    elif not has_admin:
        caps["dependency_review"] = CapabilityResult(
            name="dependency_review",
            status=CapabilityStatus.INACCESSIBLE.value,
            reason="Token lacks repository administration permission to inspect Dependency Review prerequisites",
        )
    else:
        if graph_enabled or alerts_inv.get("status") in (200, 204) or alerts_inv.get("enabled") is True:
            caps["dependency_review"] = CapabilityResult(
                name="dependency_review",
                status=CapabilityStatus.CONFIGURED.value,
                reason="Dependency review is supported with dependency graph enabled",
            )
        else:
            caps["dependency_review"] = CapabilityResult(
                name="dependency_review",
                status=CapabilityStatus.MISSING.value,
                reason="Dependency review requires dependency graph to be enabled",
                guidance="Enable vulnerability alerts or dependency graph to use Dependency Review",
            )

    # 7. Secret scanning push protection
    push_status = (
        sec_analysis.get("secret_scanning_push_protection", {}).get("status")
        if isinstance(sec_analysis, dict)
        else None
    )
    if is_private and not is_enterprise:
        caps["push_protection"] = CapabilityResult(
            name="push_protection",
            status=CapabilityStatus.UNSUPPORTED.value,
            reason="Push protection on private repositories requires GitHub Advanced Security (Enterprise plan)",
            guidance="Upgrade repository owner to GitHub Enterprise with Advanced Security",
        )
    elif not has_admin:
        caps["push_protection"] = CapabilityResult(
            name="push_protection",
            status=CapabilityStatus.INACCESSIBLE.value,
            reason="Token lacks repository administration permission to inspect or configure push protection",
        )
    elif not has_repo_scope:
        caps["push_protection"] = CapabilityResult(
            name="push_protection",
            status=CapabilityStatus.INACCESSIBLE.value,
            reason="Token lacks 'repo' OAuth scope to inspect or configure push protection",
            guidance="Regenerate token with 'repo' scope",
        )
    elif push_status == "enabled":
        caps["push_protection"] = CapabilityResult(
            name="push_protection",
            status=CapabilityStatus.CONFIGURED.value,
            reason="Secret scanning push protection is enabled",
        )
    else:
        caps["push_protection"] = CapabilityResult(
            name="push_protection",
            status=CapabilityStatus.MISSING.value,
            reason="Secret scanning push protection is available but not enabled",
            guidance="Enable push protection in Settings > Code security and analysis",
        )

    # 8. Automated security fixes
    fixes_inv = inventory_data.get("automated_security_fixes", {})
    if not has_admin:
        caps["automated_security_fixes"] = CapabilityResult(
            name="automated_security_fixes",
            status=CapabilityStatus.INACCESSIBLE.value,
            reason="Token lacks repository administration permission to inspect or configure automated security fixes",
        )
    elif not has_repo_scope:
        caps["automated_security_fixes"] = CapabilityResult(
            name="automated_security_fixes",
            status=CapabilityStatus.INACCESSIBLE.value,
            reason="Token lacks 'repo' OAuth scope to inspect or configure automated security fixes",
            guidance="Regenerate token with 'repo' scope",
        )
    elif isinstance(fixes_inv, dict) and (fixes_inv.get("status") == 200 or fixes_inv.get("enabled") is True):
        caps["automated_security_fixes"] = CapabilityResult(
            name="automated_security_fixes",
            status=CapabilityStatus.CONFIGURED.value,
            reason="Automated security fixes are enabled",
        )
    else:
        caps["automated_security_fixes"] = CapabilityResult(
            name="automated_security_fixes",
            status=CapabilityStatus.MISSING.value,
            reason="Automated security fixes are available but not enabled",
            guidance="Enable Dependabot security updates in repository Settings > Code security and analysis",
        )

    # 9. Private vulnerability reporting
    pvr_inv = inventory_data.get("private_vulnerability_reporting", {})
    if is_private:
        caps["private_vulnerability_reporting"] = CapabilityResult(
            name="private_vulnerability_reporting",
            status=CapabilityStatus.UNSUPPORTED.value,
            reason="Private vulnerability reporting is only supported on public repositories",
            guidance="Make repository public to enable private vulnerability reporting",
        )
    elif not has_admin:
        caps["private_vulnerability_reporting"] = CapabilityResult(
            name="private_vulnerability_reporting",
            status=CapabilityStatus.INACCESSIBLE.value,
            reason="Token lacks repository administration permission to inspect or configure private vulnerability reporting",
        )
    elif not has_repo_scope:
        caps["private_vulnerability_reporting"] = CapabilityResult(
            name="private_vulnerability_reporting",
            status=CapabilityStatus.INACCESSIBLE.value,
            reason="Token lacks 'repo' OAuth scope to inspect or configure private vulnerability reporting",
            guidance="Regenerate token with 'repo' scope",
        )
    elif isinstance(pvr_inv, dict) and pvr_inv.get("enabled") is True:
        caps["private_vulnerability_reporting"] = CapabilityResult(
            name="private_vulnerability_reporting",
            status=CapabilityStatus.CONFIGURED.value,
            reason="Private vulnerability reporting is enabled",
        )
    else:
        caps["private_vulnerability_reporting"] = CapabilityResult(
            name="private_vulnerability_reporting",
            status=CapabilityStatus.MISSING.value,
            reason="Private vulnerability reporting is available but not enabled",
            guidance="Enable private vulnerability reporting in Settings > Code security and analysis",
        )

    return caps


def evaluate_repository_capabilities(api: Any, repo_data: dict, inventory_data: dict,
                                     plan_name: Optional[str] = None,
                                     plan: Optional[dict] = None,
                                     caller_permissions: Optional[dict] = None) -> Dict[str, dict]:
    """Classify repository capabilities, reusing pre-detected plan and scopes.

    ``setup_github_repo.collect_inventory`` already probes the plan and caller
    permissions once. Passing those results here avoids redundant ``/user``
    and plan-capability HTTP requests during the same inspection pass.
    """
    if plan_name is None:
        plan_name, plan = detect_repository_plan(api, repo_data)
    if caller_permissions is None:
        caller_permissions = detect_caller_permissions(repo_data, api=api)
    classified = classify_capabilities(repo_data, plan_name, caller_permissions, inventory_data)
    return {name: res.to_dict() for name, res in classified.items()}
