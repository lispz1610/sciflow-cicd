"""Extraction and state utilities for interactive session defaults from inventory."""

from __future__ import annotations

try:
    from scripts.workflow_defaults import DEFAULT_HOSTED_RUNNER, DEFAULT_PYTHON_VERSIONS, DEFAULT_TEST_COMMANDS
except ImportError:
    from workflow_defaults import DEFAULT_HOSTED_RUNNER, DEFAULT_PYTHON_VERSIONS, DEFAULT_TEST_COMMANDS


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
                "versions": wf.get("versions") or list(DEFAULT_PYTHON_VERSIONS),
                "python_setup_mode": wf.get("python_setup_mode", "setup-python"),
                "package_manager": wf.get("package_manager", "pip"),
                "install_commands": wf.get("install_commands"),
                "commands": wf.get("commands") or list(DEFAULT_TEST_COMMANDS),
                "cache": wf.get("cache", False),
                "lfs": wf.get("lfs", False),
                "lint_commands": wf.get("lint_commands"),
                "paths": wf.get("paths"),
                "triggers": wf.get("triggers", ["pull_request"]),
                "runs_on": wf.get("runs_on", DEFAULT_HOSTED_RUNNER),
                "expected_sha": wf.get("expected_sha") or wf.get("sha"),
            }
            if wf.get("checkout_sha"):
                python_opts["checkout_sha"] = wf["checkout_sha"]
            if wf.get("setup_python_sha"):
                python_opts["setup_python_sha"] = wf["setup_python_sha"]
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
            if wf.get("checkout_sha"):
                cmake_opts["checkout_sha"] = wf["checkout_sha"]
        elif tmpl in ("dependency-review", "dependency"):
            dep_review_opts = {
                "fail_on_severity": wf.get("fail_on_severity", "high"),
                "allow_licenses": wf.get("allow_licenses", "MIT, Apache-2.0, BSD-3-Clause, BSD-2-Clause, ISC, Python-2.0"),
                "deny_licenses": wf.get("deny_licenses", ""),
                "comment_summary_in_pr": wf.get("comment_summary_in_pr", "never"),
                "runs_on": wf.get("runs_on", DEFAULT_HOSTED_RUNNER),
                "expected_sha": wf.get("expected_sha") or wf.get("sha"),
            }
            if wf.get("checkout_sha"):
                dep_review_opts["checkout_sha"] = wf["checkout_sha"]
        elif tmpl in ("container-publish", "container"):
            container_opts = {
                "triggers": wf.get("triggers", ["pull_request", "push"]),
                "registry": wf.get("registry", "ghcr.io"),
                "dockerfile": wf.get("dockerfile", "Dockerfile"),
                "context": wf.get("context", "."),
                "tags": wf.get("tags"),
                "runs_on": wf.get("runs_on", DEFAULT_HOSTED_RUNNER),
                "expected_sha": wf.get("expected_sha") or wf.get("sha"),
            }
            if wf.get("checkout_sha"):
                container_opts["checkout_sha"] = wf["checkout_sha"]

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
