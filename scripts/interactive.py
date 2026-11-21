"""Interactive Setup session and repository validation for sciflow-cicd."""

from pathlib import Path
import re
import sys
from typing import Optional, TextIO

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    from scripts.configure_github_repo import ApiError, GitHubApi
    from scripts.prompts import prompt_text, prompt_choice, PromptExit
    from scripts.setup_github_repo import (
        inventory as query_inventory,
        inspect_managed_workflows,
        inspect_dependabot,
    )
    from scripts.interactive_features import prompt_features_stage
    from scripts.interactive_repository_content import prompt_merge_stage, prompt_content_stages
    from scripts.interactive_workflows_rulesets import (
        prompt_workflows_stage,
        prompt_rulesets_stage,
        prompt_workflows_rulesets_stage,
    )
    from scripts.interactive_dependabot import prompt_dependabot_stage
except ImportError:
    from configure_github_repo import ApiError, GitHubApi
    from prompts import prompt_text, prompt_choice, PromptExit
    from setup_github_repo import (
        inventory as query_inventory,
        inspect_managed_workflows,
        inspect_dependabot,
    )
    from interactive_features import prompt_features_stage
    from interactive_repository_content import prompt_merge_stage, prompt_content_stages
    from interactive_workflows_rulesets import (
        prompt_workflows_stage,
        prompt_rulesets_stage,
        prompt_workflows_rulesets_stage,
    )
    try:
        from interactive_dependabot import prompt_dependabot_stage
    except ImportError:
        prompt_dependabot_stage = None

REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def is_valid_repository(repo: str) -> bool:
    """Validate repository string against expected OWNER/REPO format."""
    if not isinstance(repo, str):
        return False
    return bool(REPOSITORY_PATTERN.match(repo.strip()))


class Session:
    """Context and state container for an Interactive Setup session."""

    def __init__(
        self,
        repository: str,
        api: GitHubApi,
        repo_data: Optional[dict] = None,
        token: Optional[str] = None,
        in_stream: Optional[TextIO] = None,
        out_stream: Optional[TextIO] = None,
        err_stream: Optional[TextIO] = None,
        resume_mode: bool = False,
        inventory: Optional[dict] = None,
        defaults: Optional[dict] = None,
    ):
        self.repository = repository
        self.api = api
        self.repo_data = repo_data if repo_data is not None else {}
        self.token = token
        self.in_stream = in_stream or sys.stdin
        self.out_stream = out_stream or sys.stdout
        self.err_stream = err_stream or sys.stderr
        self.resume_mode = resume_mode
        self.inventory = inventory if inventory is not None else {}
        self.defaults = defaults if defaults is not None else {}


def prompt_target_repository(
    in_stream: Optional[TextIO] = None,
    out_stream: Optional[TextIO] = None,
) -> Optional[str]:
    """Prompt user for target repository (OWNER/REPO) with format validation and reprompting."""
    out_s = out_stream if out_stream is not None else sys.stdout

    out_s.write("\nChoose the repository whose settings and workflows this setup will change.\n")
    out_s.write("Inventory only reads its current configuration; it makes no changes. Access requires repository permissions.\n")
    out_s.write("Use OWNER/REPO, for example octocat/Hello-World.\n")
    out_s.flush()

    while True:
        repo_input = prompt_text(
            "Target repository (OWNER/REPO)",
            required=True,
            in_stream=in_stream,
            out_stream=out_stream,
        )
        if repo_input is None:
            return None

        if not is_valid_repository(repo_input):
            out_s.write(
                f"Invalid repository format '{repo_input}'. "
                f"Expected format is 'OWNER/REPO' (e.g. octocat/Hello-World).\n"
            )
            out_s.flush()
            continue

        return repo_input


def verify_repository(
    api: GitHubApi,
    repository: str,
    out_stream: Optional[TextIO] = None,
    err_stream: Optional[TextIO] = None,
) -> Optional[dict]:
    """Verify that repository exists and is accessible using GitHubApi.

    Returns repository data dict on HTTP 200, or None on 404/ApiError.
    """
    out_s = out_stream if out_stream is not None else sys.stdout
    err_s = err_stream if err_stream is not None else sys.stderr

    try:
        status, body = api.request("GET", f"/repos/{repository}")
    except ApiError as error:
        err_s.write(
            f"Error: Failed to access repository '{repository}': {error}\n"
        )
        err_s.flush()
        return None
    except Exception as error:
        err_s.write(
            f"Error: Unexpected error verifying repository '{repository}': {error}\n"
        )
        err_s.flush()
        return None

    if status == 200 and isinstance(body, dict):
        out_s.write(f"Repository '{repository}' verified successfully.\n")
        out_s.flush()
        return body

    if status == 404:
        err_s.write(
            f"Error: Repository '{repository}' not found (HTTP 404). "
            f"Please verify the repository name and your token permissions.\n"
        )
        err_s.flush()
        return None

    err_s.write(
        f"Error: Unexpected HTTP status {status} verifying repository '{repository}'.\n"
    )
    err_s.flush()
    return None




def format_inventory_summary(inventory_data: dict) -> str:
    """Format detected repository settings into a concise, human-readable summary."""
    repo = inventory_data.get("repository", {})
    if not isinstance(repo, dict):
        repo = {}
    lines = [
        "Detected Repository Configuration:",
        f"  Target: {repo.get('full_name', 'unknown')}",
        f"  Visibility: {repo.get('visibility', 'unknown')}",
        f"  Default branch: {repo.get('default_branch', 'unknown')}",
        "  Inventory read these settings without changing the repository.",
    ]

    # Features
    features = inventory_data.get("features", {})
    lines.append("  Features:")
    for key, label in [("issues", "Issues"), ("wiki", "Wiki"), ("projects", "Projects")]:
        val = features.get(key)
        status = "Enabled" if val is True else ("Disabled" if val is False else "Unknown")
        lines.append(f"    - {label}: {status}")

    # Merge Settings
    merge = inventory_data.get("merge", {})
    lines.append("  Merge Options:")
    merge_labels = [
        ("allow_merge_commit", "Merge Commits"),
        ("allow_squash_merge", "Squash Merge"),
        ("allow_rebase_merge", "Rebase Merge"),
        ("delete_branch_on_merge", "Delete Branch on Merge"),
        ("allow_update_branch", "Update Branch"),
    ]
    for key, label in merge_labels:
        val = merge.get(key)
        status = "Enabled" if val is True else ("Disabled" if val is False else "Unknown")
        lines.append(f"    - {label}: {status}")

    # Actions
    actions = inventory_data.get("actions", {})
    workflow = inventory_data.get("workflow_permissions", {})
    lines.append("  Actions:")
    if isinstance(actions, dict) and "allowed_actions" in actions:
        lines.append(f"    - Allowed Actions: {actions['allowed_actions']}")
    elif isinstance(actions, dict) and actions.get("status") == "inaccessible":
        lines.append("    - Allowed Actions: Inaccessible (HTTP 403; configure in Settings > Actions > General)")
    if isinstance(workflow, dict):
        if workflow.get("status") == "inaccessible":
            lines.append("    - Workflow Permissions: Inaccessible (HTTP 403; configure in Settings > Actions > General)")
        else:
            wf_perm = workflow.get("default_workflow_permissions", "Unknown")
            lines.append(f"    - Workflow Permissions: {wf_perm}")
            pr_review = workflow.get("can_approve_pull_request_reviews")
            pr_status = "Enabled" if pr_review is True else ("Disabled" if pr_review is False else "Unknown")
            lines.append(f"    - Approve PR Reviews: {pr_status}")

    # Security
    sec_analysis = inventory_data.get("security_and_analysis", {})
    alerts = inventory_data.get("vulnerability_alerts", {})
    fixes = inventory_data.get("automated_security_fixes", {})
    codeql = inventory_data.get("codeql", {})
    reporting = inventory_data.get("private_vulnerability_reporting", {})

    lines.append("  Security:")
    if isinstance(alerts, dict) and alerts.get("status") == "inaccessible":
        alert_status = "Inaccessible (HTTP 403; configure in Settings > Code security and analysis)"
    else:
        alert_status = "Enabled" if alerts.get("status") == 204 else ("Disabled" if alerts.get("status") == 404 else "Unknown")
    lines.append(f"    - Vulnerability Alerts: {alert_status}")

    if isinstance(fixes, dict) and fixes.get("status") == "inaccessible":
        fix_status = "Inaccessible (HTTP 403; configure in Settings > Code security and analysis)"
    else:
        fix_status = (
            "Enabled" if (fixes.get("status") == 200 or fixes.get("enabled") is True)
            else ("Disabled" if fixes.get("status") == 404 else "Unknown")
        )
    lines.append(f"    - Automated Security Fixes: {fix_status}")

    if isinstance(sec_analysis, dict):
        secret_scanning_status = sec_analysis.get("secret_scanning", {}).get("status")
        ss_status = ("Enabled" if secret_scanning_status == "enabled"
                     else ("Disabled" if secret_scanning_status == "disabled" else "Not available"))
        lines.append(f"    - Secret Scanning: {ss_status}")

        push_protection_status = sec_analysis.get("secret_scanning_push_protection", {}).get("status")
        ssp_status = ("Enabled" if push_protection_status == "enabled"
                      else ("Disabled" if push_protection_status == "disabled" else "Not available"))
        lines.append(f"    - Secret Scanning Push Protection: {ssp_status}")

    if isinstance(codeql, dict):
        if codeql.get("status") == "inaccessible":
            cql_status = "Inaccessible (HTTP 403; configure in Settings > Code security and analysis)"
        else:
            state = codeql.get("state")
            cql_status = ("Configured" if state in ("configured", "enforced")
                          else ("Not configured" if state == "not-configured" else "Unknown"))
        lines.append(f"    - CodeQL: {cql_status}")

    if isinstance(reporting, dict):
        if reporting.get("status") == "inaccessible":
            pvr_status = "Inaccessible (HTTP 403; configure in Settings > Code security and analysis)"
        else:
            private_reporting_enabled = reporting.get("enabled")
            pvr_status = ("Enabled" if private_reporting_enabled is True
                          else ("Disabled" if private_reporting_enabled is False else "Unknown"))
        lines.append(f"    - Private Vulnerability Reporting: {pvr_status}")

    rulesets = inventory_data.get("rulesets")
    if isinstance(rulesets, dict) and rulesets.get("status") == "inaccessible":
        lines.append("  Rulesets: Inaccessible (HTTP 403; configure in Settings > Rules > Rulesets)")
    elif isinstance(rulesets, list):
        lines.append(f"  Rulesets: {len(rulesets)} configured")

    skills_status = "Installed" if inventory_data.get("agent_skills") is True else (
        "Not detected" if inventory_data.get("agent_skills") is False else "Unknown"
    )
    lines.append(f"  Matt Pocock agent skills: {skills_status}")

    managed_wf = inventory_data.get("managed_workflows")
    if managed_wf:
        wf_list = list(managed_wf.values()) if isinstance(managed_wf, dict) else managed_wf
        managed_names = [
            wf.get("filename") or wf.get("name") or wf.get("template", "workflow")
            for wf in wf_list if isinstance(wf, dict)
        ]
        if managed_names:
            lines.append(f"  Managed Workflows: {', '.join(managed_names)}")
        else:
            lines.append("  Managed Workflows: None detected")
    else:
        lines.append("  Managed Workflows: None detected")

    dep = inventory_data.get("dependabot")
    if isinstance(dep, dict) and (dep.get("status") in ("managed", "configured", True) or dep.get("package_ecosystems")):
        cadence = dep.get("cadence", "monthly")
        lines.append(f"  Dependabot: Configured ({cadence} cadence)")
    elif dep is True:
        lines.append("  Dependabot: Configured")
    else:
        lines.append("  Dependabot: Not configured")

    return "\n".join(lines)


def display_inventory_summary(
    inventory_data: dict,
    out_stream: Optional[TextIO] = None,
) -> None:
    """Display concise summary of detected repository settings to output stream."""
    out_s = out_stream if out_stream is not None else sys.stdout
    out_s.write(format_inventory_summary(inventory_data) + "\n")
    out_s.flush()


def has_existing_configuration(inventory_data: dict) -> bool:
    """Check if repository inventory contains previous configuration settings."""
    if not isinstance(inventory_data, dict):
        return False

    features = inventory_data.get("features", {})
    if any(features.get(f) is True for f in ("issues", "wiki", "projects")):
        return True

    merge = inventory_data.get("merge", {})
    if any(merge.get(k) is True for k in (
        "allow_merge_commit", "allow_squash_merge", "allow_rebase_merge",
        "delete_branch_on_merge", "allow_update_branch"
    )):
        return True

    workflow = inventory_data.get("workflow_permissions", {})
    if isinstance(workflow, dict) and workflow.get("default_workflow_permissions"):
        return True
    actions = inventory_data.get("actions", {})
    if isinstance(actions, dict) and actions.get("enabled") is True:
        return True

    sec_analysis = inventory_data.get("security_and_analysis", {})
    if isinstance(sec_analysis, dict):
        for val in sec_analysis.values():
            if isinstance(val, dict) and val.get("status") == "enabled":
                return True

    if inventory_data.get("vulnerability_alerts", {}).get("status") == 204:
        return True
    if (
        inventory_data.get("automated_security_fixes", {}).get("status") == 200
        or inventory_data.get("automated_security_fixes", {}).get("enabled") is True
    ):
        return True
    if inventory_data.get("codeql", {}).get("state") in ("configured", "enforced"):
        return True
    if inventory_data.get("private_vulnerability_reporting", {}).get("enabled") is True:
        return True

    rulesets = inventory_data.get("rulesets")
    if isinstance(rulesets, list) and len(rulesets) > 0:
        return True

    if inventory_data.get("agent_skills") is True:
        return True

    managed_wf = inventory_data.get("managed_workflows")
    if managed_wf:
        if isinstance(managed_wf, dict) and any(
            (isinstance(v, dict) and (v.get("status") in ("managed", True) or v.get("template"))) or v is True
            for v in managed_wf.values()
        ):
            return True
        if isinstance(managed_wf, list) and len(managed_wf) > 0:
            return True

    dep = inventory_data.get("dependabot")
    if dep:
        if isinstance(dep, dict) and (dep.get("status") in ("managed", "configured", True) or dep.get("package_ecosystems")):
            return True
        if dep is True:
            return True

    return False


def prompt_resume_choice(
    in_stream: Optional[TextIO] = None,
    out_stream: Optional[TextIO] = None,
) -> Optional[bool]:
    """Prompt user to choose between resuming existing setup or starting from scratch.

    Returns:
        True if resuming/updating existing setup,
        False if starting from scratch,
        None on EOF or prompt interruption.
    """
    out_s = out_stream if out_stream is not None else sys.stdout
    out_s.write("\nResume State uses observed managed settings as the starting defaults.\n")
    out_s.write("  Resume existing setup: preselect detected settings so you can review or extend them.\n")
    out_s.write("  Start from scratch: clear prompt defaults; existing repository settings still remain until explicitly changed.\n")
    out_s.write("Recommended default: Resume existing setup.\n")
    out_s.flush()
    options = [
        "Resume existing setup",
        "Start from scratch",
    ]
    choice = prompt_choice(
        "Existing repository configuration detected. How would you like to proceed?",
        options=options,
        default=0,
        in_stream=in_stream,
        out_stream=out_stream,
    )
    if choice is None:
        return None
    return choice == 0


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

    # Workflow and Dependabot defaults extraction
    managed_wf = inventory_data.get("managed_workflows")
    if isinstance(managed_wf, dict):
        workflows_list = [v for v in managed_wf.values() if isinstance(v, dict)]
    elif isinstance(managed_wf, list):
        workflows_list = [v for v in managed_wf if isinstance(v, dict)]
    elif isinstance(inventory_data.get("workflows"), list):
        workflows_list = [v for v in inventory_data["workflows"] if isinstance(v, dict)]
    else:
        workflows_list = []

    if not workflows_list and isinstance(inventory_data.get("workflow_files"), dict):
        try:
            from scripts.setup_github_repo import parse_workflow_content
        except ImportError:
            from setup_github_repo import parse_workflow_content
        for wpath, wcontent in inventory_data["workflow_files"].items():
            if isinstance(wcontent, str):
                wcontent = wcontent.encode("utf-8")
            if isinstance(wcontent, bytes):
                import hashlib
                sha = hashlib.sha1(b"blob " + str(len(wcontent)).encode() + b"\0" + wcontent).hexdigest()
                parsed = parse_workflow_content(wpath, wcontent, sha)
                if parsed:
                    workflows_list.append(parsed)

    python_opts = None
    cmake_opts = None
    dep_review_opts = None
    container_opts = None

    for wf in workflows_list:
        tmpl = wf.get("template", "")
        prof = wf.get("profile", "")
        if prof in ("python", "scientific-python") or (tmpl in ("profile-ci", "python") and prof not in ("cmake-mpi", "cmake")):
            python_opts = {
                "versions": wf.get("versions") or ["3.11", "3.12"],
                "python_setup_mode": wf.get("python_setup_mode", "setup-python"),
                "package_manager": wf.get("package_manager", "pip"),
                "install_commands": wf.get("install_commands"),
                "commands": wf.get("commands") or ["python3 -m unittest discover -s tests -p 'test_*.py'"],
                "cache": wf.get("cache", False),
                "lfs": wf.get("lfs", False),
                "lint_commands": wf.get("lint_commands"),
                "paths": wf.get("paths"),
                "triggers": wf.get("triggers", ["pull_request"]),
                "runs_on": wf.get("runs_on", "ubuntu-latest"),
                "expected_sha": wf.get("expected_sha") or wf.get("sha"),
            }
        elif prof in ("cmake-mpi", "cmake") or tmpl in ("cmake-ci", "cmake-mpi"):
            cmake_opts = {
                "compilers": wf.get("compilers"),
                "ranks": wf.get("ranks", [1, 2]),
                "build_commands": wf.get("build_commands", ["cmake -B build -S .", "cmake --build build"]),
                "test_commands": wf.get("test_commands", ["ctest --test-dir build --output-on-failure"]),
                "omp_threads": wf.get("omp_threads", 2),
                "install_system_deps": wf.get("install_system_deps", False),
                "ccache": wf.get("ccache", True),
                "opencl": wf.get("opencl", False),
                "paths": wf.get("paths"),
                "triggers": wf.get("triggers", ["pull_request"]),
                "runs_on": wf.get("runs_on", "self-hosted"),
                "expected_sha": wf.get("expected_sha") or wf.get("sha"),
            }
        elif tmpl in ("dependency-review", "dependency"):
            dep_review_opts = {
                "fail_on_severity": wf.get("fail_on_severity", "high"),
                "allow_licenses": wf.get("allow_licenses", "MIT, Apache-2.0, BSD-3-Clause, BSD-2-Clause, ISC, Python-2.0"),
                "deny_licenses": wf.get("deny_licenses", ""),
                "comment_summary_in_pr": wf.get("comment_summary_in_pr", "never"),
                "runs_on": wf.get("runs_on", "ubuntu-latest"),
                "expected_sha": wf.get("expected_sha") or wf.get("sha"),
            }
        elif tmpl in ("container-publish", "container"):
            container_opts = {
                "triggers": wf.get("triggers", ["pull_request", "push"]),
                "registry": wf.get("registry", "ghcr.io"),
                "dockerfile": wf.get("dockerfile", "Dockerfile"),
                "context": wf.get("context", "."),
                "tags": wf.get("tags"),
                "runs_on": wf.get("runs_on", "ubuntu-latest"),
                "expected_sha": wf.get("expected_sha") or wf.get("sha"),
            }

    if isinstance(inventory_data.get("python"), dict):
        python_opts = {**(python_opts or {}), **inventory_data["python"]}
    if isinstance(inventory_data.get("cmake_mpi"), dict):
        cmake_opts = {**(cmake_opts or {}), **inventory_data["cmake_mpi"]}
    if isinstance(inventory_data.get("dependency_review"), dict):
        dep_review_opts = {**(dep_review_opts or {}), **inventory_data["dependency_review"]}
    if isinstance(inventory_data.get("container"), dict):
        container_opts = {**(container_opts or {}), **inventory_data["container"]}

    dep_opts = None
    dep_inv = inventory_data.get("dependabot")
    if not dep_inv and isinstance(inventory_data.get("workflow_files"), dict):
        dep_content = inventory_data["workflow_files"].get(".github/dependabot.yml")
        if dep_content:
            if isinstance(dep_content, str):
                dep_content = dep_content.encode("utf-8")
            try:
                from scripts.setup_github_repo import parse_dependabot_content
            except ImportError:
                from setup_github_repo import parse_dependabot_content
            import hashlib
            sha = hashlib.sha1(b"blob " + str(len(dep_content)).encode() + b"\0" + dep_content).hexdigest()
            dep_inv = parse_dependabot_content(dep_content, sha)

    if isinstance(dep_inv, dict) and dep_inv.get("status") not in ("absent", False):
        dep_opts = {
            "cadence": dep_inv.get("cadence", "monthly"),
            "package_ecosystems": dep_inv.get("package_ecosystems", ["github-actions"]),
            "open_pull_requests_limit": dep_inv.get("open_pull_requests_limit", 5),
            "groups": dep_inv.get("groups", True),
        }
    elif dep_inv is True:
        dep_opts = {
            "cadence": "monthly",
            "package_ecosystems": ["github-actions"],
            "open_pull_requests_limit": 5,
            "groups": True,
        }

    defaults = {
        "features": enabled_features,
        "merge": merge_dict,
        "actions": actions_defaults,
        "security": enabled_security,
        "issue_state_gate": issue_state_gate,
        "agent_skills": inventory_data.get("agent_skills") is True,
    }
    if workflows_list:
        defaults["workflows"] = workflows_list
    if python_opts is not None:
        defaults["python"] = python_opts
    if cmake_opts is not None:
        defaults["cmake_mpi"] = cmake_opts
    if dep_review_opts is not None:
        defaults["dependency_review"] = dep_review_opts
    if container_opts is not None:
        defaults["container"] = container_opts
    if dep_opts is not None:
        defaults["dependabot"] = dep_opts
    return defaults


def clear_defaults() -> dict:
    """Return cleared/empty defaults for starting from scratch."""
    return {}


def run_state_detection(session: Session) -> bool:
    """Query inventory silently, display summary, and prompt user for resume mode.

    Populates session.inventory, session.resume_mode, and session.defaults.
    Returns True on success, False on error or EOF.
    """
    if not isinstance(getattr(session, "repository", None), str):
        return True

    out_s = session.out_stream if session.out_stream is not None else sys.stdout
    err_s = session.err_stream if session.err_stream is not None else sys.stderr

    try:
        inv = query_inventory(session.api, session.repository)
    except ApiError as error:
        err_s.write(
            f"Error: Failed to query repository inventory for '{session.repository}': {error}\n"
        )
        err_s.flush()
        return False
    except Exception as error:
        err_s.write(
            f"Error: Unexpected error querying inventory for '{session.repository}': {error}\n"
        )
        err_s.flush()
        return False

    session.inventory = inv

    default_branch = inv.get("repository", {}).get("default_branch")
    try:
        from scripts.setup_github_repo import inspect_managed_workflows, inspect_dependabot
    except ImportError:
        from setup_github_repo import inspect_managed_workflows, inspect_dependabot
    if "managed_workflows" not in inv:
        inv["managed_workflows"] = inspect_managed_workflows(session.api, session.repository, default_branch)
    if "dependabot" not in inv:
        dep_data = inspect_dependabot(session.api, session.repository, default_branch)
        if dep_data:
            inv["dependabot"] = dep_data

    display_inventory_summary(inv, out_stream=out_s)

    if has_existing_configuration(inv):
        choice = prompt_resume_choice(
            in_stream=session.in_stream,
            out_stream=out_s,
        )
        if choice is None:
            return False

        if choice:
            session.resume_mode = True
            session.defaults = extract_defaults_from_inventory(inv)
            out_s.write("Resuming setup with detected configuration as defaults.\n")
            out_s.flush()
        else:
            session.resume_mode = False
            session.defaults = clear_defaults()
            out_s.write("Starting setup from scratch with cleared defaults.\n")
            out_s.flush()
    else:
        out_s.write("No previous configuration detected. Starting fresh setup.\n")
        out_s.flush()
        session.resume_mode = False
        session.defaults = clear_defaults()

    return True


def init_interactive_session(
    token: Optional[str] = None,
    token_env: str = "GITHUB_TOKEN",
    api: Optional[GitHubApi] = None,
    in_stream: Optional[TextIO] = None,
    out_stream: Optional[TextIO] = None,
    err_stream: Optional[TextIO] = None,
    detect_state: bool = False,
) -> Optional[Session]:
    """Initialize an Interactive Setup session.

    Validates GitHub token, prompts for repository, validates naming format,
    and verifies repository accessibility.
    """
    err_s = err_stream if err_stream is not None else sys.stderr
    out_s = out_stream if out_stream is not None else sys.stdout

    import os

    effective_token = token if token is not None else os.environ.get(token_env)
    if effective_token is None or not effective_token.strip():
        err_s.write(
            f"Error: {token_env} environment variable is required. "
            f"Please set {token_env} with appropriate repository permissions before running.\n"
        )
        err_s.flush()
        return None

    api_client = api if api is not None else GitHubApi(effective_token.strip())

    while True:
        repo = prompt_target_repository(in_stream=in_stream, out_stream=out_s)
        if repo is None:
            return None

        body = verify_repository(
            api_client,
            repo,
            out_stream=out_s,
            err_stream=err_s,
        )
        if body is None:
            continue

        session = Session(
            repository=repo,
            api=api_client,
            repo_data=body,
            token=effective_token.strip(),
            in_stream=in_stream,
            out_stream=out_s,
            err_stream=err_s,
        )
        if detect_state:
            if not run_state_detection(session):
                return None
        return session


try:
    from scripts.interactive_actions_security import prompt_actions_security_stage
except ImportError:
    try:
        from interactive_actions_security import prompt_actions_security_stage
    except ImportError:
        pass

try:
    from scripts.policy_assembly import (
        DEFAULT_POLICY_PATH,
        assemble_policy,
        persist_policy,
        assemble_and_persist_policy,
    )
except ImportError:
    try:
        from policy_assembly import (
            DEFAULT_POLICY_PATH,
            assemble_policy,
            persist_policy,
            assemble_and_persist_policy,
        )
    except ImportError:
        pass

try:
    from scripts.interactive_apply import (
        execute_dry_run,
        format_dry_run_summary,
        display_dry_run_summary,
        prompt_apply_confirmation,
        execute_apply,
        format_apply_summary,
        run_interactive_apply,
    )
except ImportError:
    try:
        from interactive_apply import (
            execute_dry_run,
            format_dry_run_summary,
            display_dry_run_summary,
            prompt_apply_confirmation,
            execute_apply,
            format_apply_summary,
            run_interactive_apply,
        )
    except ImportError:
        pass


def run_interactive_workflow(session: Session) -> int:
    """Execute the Interactive Setup workflow: questionnaires, Policy assembly, Dry-Run, confirmation, and apply."""
    if not isinstance(getattr(session, "repository", None), str):
        return 0

    out_s = session.out_stream if session.out_stream is not None else sys.stdout
    err_s = session.err_stream if session.err_stream is not None else sys.stderr

    stages = []

    # 1. Questionnaire: Features Stage
    features_stage = prompt_features_stage(session)
    if features_stage is not None:
        stages.append(features_stage)

    # 2. Merge settings
    try:
        merge_stage = prompt_merge_stage(session, raise_on_interrupt=True)
    except PromptExit:
        out_s.write("Setup cancelled.\n")
        return 0
    if merge_stage is not None:
        stages.append(merge_stage)

    # 3. Actions and security
    actions_sec_stages = prompt_actions_security_stage(session)
    if actions_sec_stages is None:
        out_s.write("Setup cancelled.\n")
        out_s.flush()
        return 0
    if actions_sec_stages:
        stages.extend(actions_sec_stages)

    # 4. Authored repository content
    try:
        content_stages = prompt_content_stages(session, raise_on_interrupt=True)
    except PromptExit:
        out_s.write("Setup cancelled.\n")
        return 0
    except ApiError as error:
        err_s.write(f"Error inspecting repository content: {error}\n")
        return 1
    if content_stages is None:
        out_s.write("Setup cancelled.\n")
        return 0
    stages.extend(content_stages)

    # 5. Workflows and rulesets
    workflows_rules_stages = prompt_workflows_rulesets_stage(session)
    if workflows_rules_stages is None:
        out_s.write("Setup cancelled.\n")
        out_s.flush()
        return 0
    if workflows_rules_stages:
        stages.extend(workflows_rules_stages)

    # 6. Dependabot
    try:
        from scripts.interactive_dependabot import prompt_dependabot_stage
    except ImportError:
        try:
            from interactive_dependabot import prompt_dependabot_stage
        except ImportError:
            prompt_dependabot_stage = None

    if prompt_dependabot_stage is not None:
        dependabot_default = None
        if getattr(session, "resume_mode", False):
            defaults = getattr(session, "defaults", {}) or {}
            if isinstance(defaults, dict) and defaults.get("dependabot"):
                dependabot_default = True
            else:
                dependabot_default = False
        try:
            dependabot_stage = prompt_dependabot_stage(session, default=dependabot_default)
        except PromptExit:
            out_s.write("Setup cancelled.\n")
            return 0
        if dependabot_stage is not None:
            stages.append(dependabot_stage)

    # If no stages were chosen
    if not stages:
        out_s.write("No configuration changes selected. Setup completed without modifications.\n")
        out_s.flush()
        return 0

    # 6. Policy assembly and persistence
    try:
        policy, policy_path = assemble_and_persist_policy(session, stages)
    except ValueError as error:
        err_s.write(f"Error assembling policy: {error}\n")
        err_s.flush()
        return 1

    # 7. Dry-Run, user confirmation, and apply execution
    return run_interactive_apply(session, policy)
