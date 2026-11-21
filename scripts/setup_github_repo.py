#!/usr/bin/env python3
"""Inventory and apply an explicit, versioned GitHub repository setup policy."""

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import quote

from configure_github_repo import (
    ApiError, GitHubApi, FEATURES, SECURITY_FEATURES, reconcile_actions,
    reconcile_actions_policy, reconcile_codeql, reconcile_features,
    reconcile_merge, reconcile_security, reconcile_gates, reconcile_issue_state_gate,
)
if __package__ == "scripts":
    from scripts.configure_github_repo import ApiError as PackageApiError
else:
    PackageApiError = ApiError
from install_github_workflows import (DEFAULT_CHECKOUT_SHA, DEFAULT_SETUP_PYTHON_SHA, DEFAULT_SETUP_UV_SHA,
                                      ISSUE_STATE_GATE_FILES, StagingFailure, install, install_selected,
                                      _agent_skills_bundle, _file, render_documentation_suite,
                                      extract_profile_status_check, inspect_remote_workflow_status_check,
                                      validate_allow_removed_skills)
from review_policy import validate_policy as validate_review_policy
from setup_repository_content import reconcile_files, reconcile_labels, selected_file_bytes

try:
    from scripts.agent_skills_readiness import evaluate_agent_skills_readiness
except ImportError:  # pragma: no cover
    from agent_skills_readiness import evaluate_agent_skills_readiness

try:
    from scripts.agent_skills_compatibility import audit_agent_skills_compatibility, probe_cli_tools
except ImportError:  # pragma: no cover
    from agent_skills_compatibility import audit_agent_skills_compatibility, probe_cli_tools

try:
    from scripts.hook_contracts import HookValidationError, validate_hook_packages
except ImportError:  # pragma: no cover
    from hook_contracts import HookValidationError, validate_hook_packages


REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def _guidance_for_path(path, availability):
    if availability == "inaccessible":
        if "/actions/permissions" in path:
            return ("Configure Actions permissions manually in GitHub Settings > Actions > General, "
                    "or grant 'Actions: read/write' token permission.")
        if "/code-scanning" in path:
            return ("Configure CodeQL manually in GitHub Settings > Code security and analysis > CodeQL analysis, "
                    "or grant 'Code scanning alerts: read/write' token permission.")
        if "/rulesets" in path:
            return ("Configure branch rulesets manually in GitHub Settings > Rules > Rulesets, "
                    "or grant 'Administration: read/write' token permission.")
        if "/vulnerability-alerts" in path or "/automated-security-fixes" in path:
            return ("Configure Dependabot alerts manually in GitHub Settings > Code security and analysis, "
                    "or grant 'Administration: read/write' token permission.")
        if "/private-vulnerability-reporting" in path:
            return ("Configure Private vulnerability reporting manually in GitHub Settings > Code security and analysis, "
                    "or grant 'Administration: read/write' token permission.")
        return "Check token permissions or configure manually in repository Settings."
    return None


def _guidance_for_stage(stage, reason, availability):
    if availability == "inaccessible":
        if stage in ("security", "dependency-graph"):
            return ("Configure security features manually in GitHub Settings > Code security and analysis, "
                    "or grant 'Administration: read/write' or 'Security events: read/write' token permissions.")
        if stage == "codeql":
            return ("Configure CodeQL manually in GitHub Settings > Code security and analysis > CodeQL analysis, "
                    "or grant 'Code scanning alerts: read/write' token permission.")
        if stage in ("gates", "issue-state-gate"):
            return ("Configure branch rulesets manually in GitHub Settings > Rules > Rulesets, "
                    "or grant 'Administration: read/write' token permission.")
        if stage in ("actions", "actions-policy"):
            return ("Configure Actions permissions manually in GitHub Settings > Actions > General, "
                    "or grant 'Actions: read/write' token permission.")
        return "Check token permissions or configure manually in repository Settings."
    if availability == "unsupported":
        if "supports private repositories only" in reason:
            return "Review Setup supports private repositories only; remove the review stage or make repository private."
        if "available only for public repositories" in reason:
            return ("Private vulnerability reporting is available only for public repositories; "
                    "manage security advisories in Security > Advisories.")
        if "pro, team, or enterprise plan" in reason.lower() or "verifiable pro" in reason.lower():
            return ("Private repository rulesets require a GitHub Pro, Team, or Enterprise plan; "
                    "upgrade your account or configure branch protection rules in Settings > Branches.")
        if "advanced security" in reason.lower():
            return ("Enable GitHub Advanced Security in Settings > Code security and analysis, "
                    "or upgrade owner account plan.")
        if "no codeql-supported languages" in reason.lower():
            return ("Add CodeQL-supported source code (e.g. Python, JavaScript, Go, Java, C/C++, C#, Ruby, Swift) "
                    "or configure code scanning via advanced setup workflow.")
        if "fork" in reason.lower():
            return "Pull requests from forks are unsupported by the Issue State Gate."
        return reason
    return "Check repository configuration and token permissions."


def _read_optional(api, path, accepted=(200,)):
    try:
        status, body = api.request("GET", path)
    except (ApiError, PackageApiError) as error:
        avail = "inaccessible" if "HTTP 403" in str(error) else "unknown"
        res = {"status": avail, "reason": str(error)}
        guidance = _guidance_for_path(path, avail)
        if guidance:
            res["guidance"] = guidance
        return res
    if status not in accepted:
        return {"status": "unknown", "reason": f"GET {path}: HTTP {status}"}
    if status == 200 and not isinstance(body, dict):
        return {"status": "unknown", "reason": f"GET {path}: malformed response"}
    return body if status == 200 else {"status": status}


def _repository_file_reader(api, repository):
    status, repo_data = api.request("GET", "/repos/" + repository)
    if status != 200 or not isinstance(repo_data, dict) or not isinstance(repo_data.get("default_branch"), str):
        raise ApiError(f"GET /repos/{repository}: HTTP {status}")
    default_branch = repo_data["default_branch"]
    base = "/repos/" + repository

    def read_file(path):
        entry = _file(api, base, path, default_branch)
        return entry[0] if entry else None

    return read_file


def _rulesets(api, base):
    items = []
    for page in range(1, 21):
        path = base + f"/rulesets?includes_parents=false&per_page=100&page={page}"
        try:
            status, body = api.request("GET", path)
        except ApiError as error:
            avail = "inaccessible" if "HTTP 403" in str(error) else "unknown"
            res = {"status": avail, "reason": str(error)}
            guidance = _guidance_for_path(path, avail)
            if guidance:
                res["guidance"] = guidance
            return res
        if status != 200 or not isinstance(body, list):
            return {"status": "unknown", "reason": f"GET {path}: invalid response"}
        items.extend(body)
        if len(body) < 100:
            return items
    return {"status": "unknown", "reason": "ruleset pagination exceeded 20 pages"}


def inventory(api, repository):
    """Report observed repository setup state, never inventing unavailable values."""
    if not REPOSITORY.fullmatch(repository):
        raise ValueError("repository must be OWNER/REPO")
    base = "/repos/" + repository
    status, repo = api.request("GET", base)
    if status != 200 or not isinstance(repo, dict):
        raise ApiError(f"GET {base}: HTTP {status}; repository state unavailable")
    actions = _read_optional(api, base + "/actions/permissions")
    workflow = _read_optional(api, base + "/actions/permissions/workflow")
    codeql = _read_optional(api, base + "/code-scanning/default-setup")
    security = repo.get("security_and_analysis")
    if not isinstance(security, dict):
        security = {"status": "unknown", "reason": "security_and_analysis is unavailable"}
    alerts = _read_optional(api, base + "/vulnerability-alerts", (200, 204))
    fixes = _read_optional(api, base + "/automated-security-fixes")
    reporting = _read_optional(api, base + "/private-vulnerability-reporting")
    if repo.get("visibility") == "public":
        graph = {"status": "enabled", "reason": "public repository"}
    elif alerts.get("status") == 204 or alerts.get("enabled") is True:
        graph = {"status": "enabled", "reason": "vulnerability alerts enabled"}
    elif alerts.get("enabled") is False:
        graph = {"status": "disabled", "reason": "vulnerability alerts explicitly reported disabled"}
    else:
        graph_status = alerts.get("status", "unknown")
        graph = {"status": graph_status,
                 "reason": "cannot verify dependency graph from the available API: " +
                           alerts.get("reason", "vulnerability alerts were not observed")}
        if graph_status == "inaccessible" and alerts.get("guidance"):
            graph["guidance"] = alerts["guidance"]
    feature_names = {"issues": "has_issues", "wiki": "has_wiki", "projects": "has_projects"}
    agent_skills = None
    default_branch = repo.get("default_branch")
    if isinstance(default_branch, str) and default_branch:
        manifest_path = base + "/contents/.agents/skills/manifest.json?ref=" + quote(default_branch, safe="")
        manifest_body = _read_optional(api, manifest_path)
        if isinstance(manifest_body, dict) and manifest_body.get("encoding") == "base64" and isinstance(manifest_body.get("content"), str):
            try:
                marker = json.loads(base64.b64decode(manifest_body["content"]).decode("utf-8"))
                if (isinstance(marker, dict) and marker.get("schema_version") == 1
                        and marker.get("managed_by") in ("sciflow-cicd", "github-repo-setup")
                        and marker.get("bundle") == "mattpocock"):
                    agent_skills = True
            except (ValueError, UnicodeError):
                pass
    inv = {
        "repository": {"full_name": repo.get("full_name", repository),
                       "visibility": repo.get("visibility", "unknown"),
                       "default_branch": repo.get("default_branch", "unknown")},
        "features": {name: repo.get(key, "unknown") for name, key in feature_names.items()},
        "wiki_setup": {
            "initial_page": {"status": "unknown", "reason": "Wiki Git repository requires a first page"},
            "editing_restriction": {"status": "unsupported", "reason": "no supported repository REST field"},
        },
        "merge": {key: repo.get(key, "unknown") for key in (
            "allow_merge_commit", "allow_squash_merge", "allow_rebase_merge",
            "delete_branch_on_merge", "allow_update_branch")},
        "actions": actions, "workflow_permissions": workflow,
        "security_and_analysis": security, "vulnerability_alerts": alerts,
        "automated_security_fixes": fixes, "private_vulnerability_reporting": reporting,
        "codeql": codeql, "dependency_graph": graph,
        "agent_skills": agent_skills,
        "rulesets": _rulesets(api, base),
    }
    try:
        from scripts.repository_capabilities import (
            evaluate_repository_capabilities,
            detect_repository_plan,
            detect_caller_permissions,
        )
    except ImportError:
        from repository_capabilities import (
            evaluate_repository_capabilities,
            detect_repository_plan,
            detect_caller_permissions,
        )
    plan_name, _ = detect_repository_plan(api, repo)
    caller_perms = detect_caller_permissions(repo)
    inv["repository"]["plan"] = plan_name
    inv["repository"]["permissions"] = caller_perms
    inv["capabilities"] = evaluate_repository_capabilities(api, repo, inv)
    return inv


def validate_policy(policy):
    if not isinstance(policy, dict) or not ({"version", "stages"} <= set(policy) <= {"version", "stages", "repository"}):
        raise ValueError("policy must contain only version and stages")
    if "repository" in policy and (not isinstance(policy["repository"], str) or not REPOSITORY.fullmatch(policy["repository"])):
        raise ValueError("repository must be OWNER/REPO")
    if type(policy["version"]) is not int or policy["version"] not in (1, 2):
        raise ValueError("unsupported policy version")
    stages = policy["stages"]
    if not isinstance(stages, list) or not stages:
        raise ValueError("policy stages must be a nonempty list")
    seen = set()
    for stage in stages:
        if not isinstance(stage, dict) or not isinstance(stage.get("name"), str):
            raise ValueError("each stage needs a name")
        name = stage["name"]
        if name == "links":
            raise ValueError(
                "unsupported stage: links; use scripts/reconcile_github_links.py "
                "with a separate managed links document"
            )
        if name in seen:
            raise ValueError(f"duplicate stage: {name}")
        seen.add(name)
        allowed = {"features": {"name", "enable"}, "actions": {"name"},
                   "security": {"name", "enable"}, "codeql": {"name", "query_suite"},
                   "dependency-graph": {"name"},
                   "merge": {"name", "settings"},
                   "actions-policy": {"name", "settings"},
                   "labels": {"name", "items"}, "files": {"name", "items"},
                   "issue-state-gate": {"name"},
                   "gates": {"name", "ruleset_name", "evidence_pr", "reviewer", "checks", "required_approving_review_count"},
                   "workflows": {"name", "workflows"},
                   "ci-profile": {"name", "profile", "filename", "triggers", "checkout_sha",
                                  "expected_sha", "paths", "options", "profile_options",
                                  "python", "cmake_mpi", "cmake-mpi", "concurrency",
                                  "status_check_name", "template", "template_version"},
                   "agent-skills": {"name", "bundle", "allow_removed_skills", "expected_sha"},
                   "hooks": {"name", "opt_in", "packages"},
                   "dependabot": {"name", "cadence", "package_ecosystems", "open_pull_requests_limit", "groups"},
                   "review": {"name", "workflow", "llm", "limits"}}
        if name not in allowed or not set(stage) <= allowed[name]:
            raise ValueError(f"unsupported stage or policy key: {name}")
        if name == "codeql":
            qs = stage.get("query_suite", "default")
            if qs not in ("default", "extended"):
                raise ValueError(f"unsupported query_suite: {qs!r}; choose 'default' or 'extended'")
        if name == "dependabot":
            try:
                from scripts.dependabot_generator import validate_dependabot_stage
            except ImportError:
                from dependabot_generator import validate_dependabot_stage
            validate_dependabot_stage(stage)
        if name == "ci-profile":
            from install_github_workflows import CI_PROFILES, DEFAULT_CHECKOUT_SHA
            from install_github_workflows import render as render_wf
            prof = stage.get("profile")
            if not isinstance(prof, str) or prof not in CI_PROFILES:
                raise ValueError(f"unsupported CI profile: {prof!r}; choose 'python' or 'cmake-mpi'")
            profile_wf = dict(stage)
            profile_wf.setdefault("template", "profile-ci")
            profile_wf.setdefault("template_version", 1)
            profile_wf.setdefault("filename", "ci.yml")
            profile_wf.setdefault("triggers", ["pull_request"])
            profile_wf.setdefault("checkout_sha", DEFAULT_CHECKOUT_SHA)
            render_wf(profile_wf)
        if name == "agent-skills":
            if policy["version"] != 2:
                raise ValueError("agent-skills stage requires Policy version 2")
            if (not set(stage) <= {"name", "bundle", "allow_removed_skills", "expected_sha"}
                    or stage.get("bundle") != "mattpocock"):
                raise ValueError("agent-skills bundle must be mattpocock")
            allowed_removals = validate_allow_removed_skills(stage.get("allow_removed_skills"))
        if name == "hooks":
            if policy["version"] != 2:
                raise ValueError("hooks stage requires Policy version 2")
            if "opt_in" in stage and type(stage["opt_in"]) is not bool:
                raise ValueError("hooks opt_in must be a boolean")
            if "packages" in stage:
                if not isinstance(stage["packages"], list):
                    raise ValueError("hooks packages must be a list of paths")
                for pkg in stage["packages"]:
                    if not isinstance(pkg, (str, Path)):
                        raise ValueError("hooks package path must be a string or Path")
        if name == "review":
            if policy["version"] != 2:
                raise ValueError("review stage requires Policy version 2")
            validate_review_policy(stage)
        if name == "gates":
            review_count = stage.get("required_approving_review_count", 1)
            if not isinstance(review_count, int) or review_count < 0:
                raise ValueError("required_approving_review_count must be a non-negative integer")
            if not isinstance(stage.get("evidence_pr"), int) or not stage.get("checks"):
                raise ValueError("gates requires evidence_pr and checks")
            if review_count > 0 and (not isinstance(stage.get("reviewer"), str) or not stage.get("reviewer")):
                raise ValueError("gates with required approvals requires reviewer")
            _gate_checks(stage)
        if name == "workflows" and (not isinstance(stage.get("workflows"), list) or not stage.get("workflows")):
            raise ValueError("workflows requires a nonempty workflows list")
        if name in ("features", "security"):
            values = stage.get("enable")
            choices = FEATURES if name == "features" else SECURITY_FEATURES
            if not isinstance(values, list) or not values or any(
                not isinstance(value, str) or value not in choices for value in values
            ) or len(values) != len(set(values)):
                raise ValueError(f"invalid {name} enable list")
        if name in ("merge", "actions-policy") and not isinstance(stage.get("settings"), dict):
            raise ValueError(f"{name} requires settings")
        if name in ("labels", "files") and not isinstance(stage.get("items"), list):
            raise ValueError(f"{name} requires items")
    if "dependency-graph" in seen and any(
        stage["name"] == "security" and "vulnerability-alerts" in stage["enable"]
        for stage in stages
    ):
        raise ValueError("dependency-graph and vulnerability-alerts use the same endpoint")
    return stages


def _gate_checks(stage):
    """Normalize selected merge checks to their workflow paths."""
    raw_checks = stage["checks"]
    checks = {}
    if isinstance(raw_checks, dict):
        for k, v in raw_checks.items():
            path = v
            if not path.startswith(".github/workflows/") and (path.endswith(".yml") or path.endswith(".yaml")):
                path = f".github/workflows/{path}"
            if re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", k) and not re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", path):
                checks[path] = k
            else:
                checks[k] = path
    elif isinstance(raw_checks, list):
        for item in raw_checks:
            if isinstance(item, str):
                if "=" in item:
                    left_part, right_part = [part.strip() for part in item.split("=", 1)]
                    if left_part.endswith((".yml", ".yaml")) or left_part.startswith(".github/"):
                        workflow_path, check_name = left_part, right_part
                    elif right_part.endswith((".yml", ".yaml")) or right_part.startswith(".github/"):
                        check_name, workflow_path = left_part, right_part
                    else:
                        workflow_path, check_name = left_part, right_part
                else:
                    check_name = item.strip()
                    if "dependency-review" in check_name.lower():
                        workflow_path = ".github/workflows/dependency-review.yml"
                    else:
                        workflow_path = ".github/workflows/ci.yml"
                if not workflow_path.startswith(".github/workflows/"):
                    workflow_path = f".github/workflows/{workflow_path}"
                checks[check_name] = workflow_path
            elif isinstance(item, dict):
                if "name" in item and "path" in item:
                    check_name = item["name"]
                    path = item["path"]
                    if not path.startswith(".github/workflows/"):
                        path = f".github/workflows/{path}"
                    checks[check_name] = path
                elif "context" in item and "path" in item:
                    check_name = item["context"]
                    path = item["path"]
                    if not path.startswith(".github/workflows/"):
                        path = f".github/workflows/{path}"
                    checks[check_name] = path
                else:
                    for k, v in item.items():
                        path = v
                        if not path.startswith(".github/workflows/"):
                            path = f".github/workflows/{path}"
                        checks[k] = path
    else:
        raise ValueError("invalid checks format for gates stage")
    if ".github/workflows/open-code-review.yml" in checks.values():
        raise ValueError("Advisory Review cannot be a required merge check")
    return checks


def _detect_profile_check_discrepancy(gate_checks, workflow_stage_input=None, ci_profile_stage_input=None, api=None, repository=None):
    """Detect discrepancies between configured gate checks and staged or active profile workflows."""
    profile_checks = {}
    if ci_profile_stage_input:
        path = ".github/workflows/" + ci_profile_stage_input.get("filename", "ci.yml")
        profile_checks[path] = ci_profile_stage_input.get("status_check_name", "status-check")
    if workflow_stage_input and isinstance(workflow_stage_input.get("workflows"), list):
        for wf in workflow_stage_input["workflows"]:
            if isinstance(wf, dict) and (wf.get("template") == "profile-ci" or "profile" in wf or wf.get("name") == "ci-profile"):
                path = ".github/workflows/" + wf.get("filename", "ci.yml")
                profile_checks[path] = wf.get("status_check_name", "status-check")

    for chk, path in gate_checks.items():
        expected = profile_checks.get(path)
        if expected is not None:
            if chk != expected:
                return (
                    f"Discrepancy detected: configured ruleset check '{chk}' does not match "
                    f"profile workflow declared check '{expected}' for {path}"
                )
        elif api and repository:
            declared_check = inspect_remote_workflow_status_check(api, repository, path)
            if declared_check and chk != declared_check:
                return (
                    f"Discrepancy detected: configured ruleset check '{chk}' does not match "
                    f"profile workflow declared check '{declared_check}' for {path}"
                )
    return None


def _stage_changes(api, repository, stage, planned_actions=False, issue_gate_details=None):
    name = stage["name"]
    if name == "features":
        return reconcile_features(api, repository, stage["enable"])
    if name == "actions":
        return reconcile_actions(api, repository)
    if name == "security":
        repo = _read_optional(api, "/repos/" + repository)
        if isinstance(repo, dict) and repo.get("visibility") == "private":
            try:
                from scripts.repository_capabilities import detect_repository_plan
            except ImportError:
                from repository_capabilities import detect_repository_plan
            plan_name, _ = detect_repository_plan(api, repo)
            if plan_name not in ("business", "enterprise"):
                if any(f in ("secret-scanning", "push-protection") for f in stage.get("enable", [])):
                    raise ApiError(
                        "Secret scanning on private repositories requires GitHub Advanced Security or an Enterprise plan"
                    )
        features = list(stage["enable"])
        if "vulnerability-alerts" in features and "automated-security-fixes" not in features:
            features.append("automated-security-fixes")
        return reconcile_security(api, repository, features)
    if name == "merge":
        return reconcile_merge(api, repository, stage["settings"])
    if name == "actions-policy":
        return reconcile_actions_policy(api, repository, stage["settings"])
    if name == "dependency-graph":
        repo = _read_optional(api, "/repos/" + repository)
        if repo.get("visibility") == "public":
            return []
        if repo.get("visibility") not in ("private", "internal"):
            raise ApiError("cannot verify repository visibility for dependency graph")
        return reconcile_security(api, repository, ["vulnerability-alerts", "automated-security-fixes"])
    if name == "labels":
        return reconcile_labels(api, repository, stage["items"])
    if name == "files":
        return reconcile_files(api, repository, stage["items"])
    if name == "gates":
        return reconcile_gates(
            api,
            repository,
            stage.get("ruleset_name", "code-review"),
            stage["evidence_pr"],
            stage.get("reviewer"),
            _gate_checks(stage),
            apply=False,
            required_approving_review_count=stage.get("required_approving_review_count", 1),
        )
    if name == "issue-state-gate":
        return reconcile_issue_state_gate(api, repository, apply=False, details=issue_gate_details)
    if name == "workflows":
        res = install(api, repository, {"version": 1, "workflows": stage["workflows"]}, apply=False)
        return [("PUT", change["path"], {"message": "Install workflow", "content": change["desired_sha256"]}) for change in res.get("changes", [])]
    if name == "ci-profile":
        profile_wf = dict(stage)
        profile_wf.setdefault("template", "profile-ci")
        profile_wf.setdefault("template_version", 1)
        profile_wf.setdefault("filename", "ci.yml")
        profile_wf.setdefault("triggers", ["pull_request"])
        profile_wf.setdefault("checkout_sha", DEFAULT_CHECKOUT_SHA)
        res = install(api, repository, {"version": 1, "workflows": [profile_wf]}, apply=False)
        return [("PUT", change["path"], {"message": "Install CI profile workflow", "content": change["desired_sha256"]}) for change in res.get("changes", [])]
    if name == "codeql":
        try:
            return reconcile_codeql(
                api,
                repository,
                planned_actions=planned_actions,
                query_suite=stage.get("query_suite", "default"),
            )
        except ApiError as error:
            if "HTTP 403" in str(error):
                raise
            repo = _read_optional(api, "/repos/" + repository)
            if isinstance(repo, dict) and repo.get("visibility") == "private":
                try:
                    from scripts.repository_capabilities import detect_repository_plan
                except ImportError:
                    from repository_capabilities import detect_repository_plan
                plan_name, _ = detect_repository_plan(api, repo)
                if plan_name not in ("business", "enterprise"):
                    raise ApiError(
                        "CodeQL default setup on private repositories requires GitHub Advanced Security"
                    )
            raise
    raise ValueError(f"unsupported stage: {name}")


def _current(api, stage, method, path, desired):
    if method == "POST" or not path.startswith("/"):
        return {"status": "absent"}
    status, body = api.request("GET", path)
    if stage in ("security", "dependency-graph") and method == "PUT" and desired is None:
        if status == 204:
            return {"enabled": True}
        if status == 200 and isinstance(body, dict) and type(body.get("enabled")) is bool:
            return {"enabled": body["enabled"]}
    if stage == "files":
        return {"blob_sha": body.get("sha") if status == 200 and isinstance(body, dict) else None}
    if status == 200 and isinstance(body, dict):
        if isinstance(desired, dict):
            return {key: body.get(key, "unknown") for key in desired}
        return body
    return {"http_status": status}


def _public_desired(stage, desired):
    if stage in ("security", "dependency-graph") and desired is None:
        return {"enabled": True}
    if stage == "files":
        return {"branch": desired["branch"], "sha": desired.get("sha"),
                "content_sha256": hashlib.sha256(base64.b64decode(desired["content"])).hexdigest()}
    return desired


def _preflight_failure(stage, repository, error):
    reason = str(error)
    match = re.search(r"\bGET\s+(\/\S+)", reason)
    if ("only for public repositories" in reason or "supports private repositories only" in reason
          or "require a verifiable" in reason or "advanced security" in reason.lower()
          or "pro, team, or enterprise" in reason.lower()
          or "no codeql-supported languages" in reason.lower()):
        availability = "unsupported"
    elif "HTTP 403" in reason:
        availability = "inaccessible"
    else:
        availability = "unknown"
    guidance = _guidance_for_stage(stage, reason, availability)
    return {"stage": stage, "scope": repository, "operation": "inspect",
            "path": match.group(1).rstrip(":;") if match else "/repos/" + repository,
            "availability": availability, "reason": reason, "guidance": guidance,
            "status": "blocked"}


def _issue_gate_details():
    return {
        "workflow": ".github/workflows/issue-state-gate.yml",
        "companion_workflows": [".github/workflows/" + filename
                                for filename in ISSUE_STATE_GATE_FILES[1:]],
        "context": "Issue State Gate",
        "source": "GitHub Actions integration ID read from the exact Issue State Gate check; require a successful run of the active trusted workflow on the same PR head",
        "effective_rule": "Require pull requests and the exact Issue State Gate check on main, without required reviews or branch synchronization",
        "prerequisites": ["repository default branch is main", "merge the draft Setup Pull Request", "read access to Actions, pull requests, and check runs", "repository administration permission to write the ruleset", "observe one successful exact-source check on an open pull request"],
        "conflicts": "Existing unmanaged protections or bypass actors in the issue-state-gate ruleset block replacement.",
    }


def is_fatal_block(change):
    """Return True if a change status represents a fatal block preventing apply."""
    if not isinstance(change, dict) or change.get("status") != "blocked":
        return False
    # Inaccessible optional capabilities do not block applying unblocked direct settings or Setup PRs
    stage = change.get("stage")
    availability = change.get("availability")
    if stage in ("codeql", "security", "dependency-graph") and availability == "inaccessible":
        return False
    return True


def _handle_stage_preflight_failure(stage_name, repository, error, changes, warnings, apply):
    """Consolidated handler for preflight and stage inspection failures with guidance formatting."""
    blocked = _preflight_failure(stage_name, repository, error)
    if stage_name == "issue-state-gate":
        blocked.update(_issue_gate_details())
    changes.append(blocked)
    is_optional_or_inaccessible = (
        stage_name in ("gates", "issue-state-gate", "codeql", "security", "dependency-graph")
        or blocked.get("availability") == "inaccessible"
    )
    if is_optional_or_inaccessible:
        guidance = blocked.get("guidance", "")
        action_suffix = f" Action: {guidance}" if guidance else ""
        warning_msg = f"{stage_name}: {blocked['reason']}.{action_suffix}"
        warnings.append(warning_msg)
        return None
    return {"mode": "apply" if apply else "dry-run", "repository": repository, "changes": changes}


def _planned_identity(changes):
    """The complete public, reviewable operation identity of a Dry-Run."""
    return [(change.get("stage"), change.get("operation"), change.get("path"),
             change.get("current"), change.get("desired"), change.get("desired_sha256"),
             change.get("status")) for change in changes]


def _workflow_identity(plan):
    return (plan.get("default_branch"), plan.get("branch"), plan.get("pull_request"),
            [(item.get("path"), item.get("operation"), item.get("current_sha"),
              item.get("desired_sha256")) for item in plan.get("changes", [])])


def _verify_workflow_identity(api, repository, workflow_stage, files_stage, workflow_plan, ci_profile_stage=None, dependabot_stage=None, hooks_stage=None):
    options = {"files_stage": files_stage} if files_stage else {}
    if ci_profile_stage:
        options["ci_profile_stage"] = ci_profile_stage
    if dependabot_stage:
        options["dependabot_stage"] = dependabot_stage
    if hooks_stage:
        options["hooks_stage"] = hooks_stage
    fresh = install_selected(api, repository, *workflow_stage, apply=False, **options)
    if _workflow_identity(fresh) != _workflow_identity(workflow_plan):
        raise ApiError("Selected files changed since Dry-Run; inspect a new Dry-Run")


def compute_policy_sha256(policy):
    """Return deterministic SHA-256 hex digest of a policy mapping."""
    return hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def run_policy(api, repository, policy, apply=False, accepted_dry_run=None, accepted_plan=None):
    """Preflight every selected stage, then apply writes and verify convergence."""
    if accepted_dry_run is None and accepted_plan is not None:
        accepted_dry_run = accepted_plan
    if not REPOSITORY.fullmatch(repository):
        raise ValueError("repository must be OWNER/REPO")
    stages = validate_policy(policy)
    review_stage = next((stage for stage in stages if stage["name"] == "review"), None)
    workflow_stage_input = next((stage for stage in stages if stage["name"] == "workflows"), None)
    ci_profile_stage_input = next((stage for stage in stages if stage["name"] == "ci-profile"), None)
    issue_state_gate_stage = next((stage for stage in stages if stage["name"] == "issue-state-gate"), None)
    agent_skills_stage = next((stage for stage in stages if stage["name"] == "agent-skills"), None)
    dependabot_stage = next((stage for stage in stages if stage["name"] == "dependabot"), None)
    hooks_stage = next((stage for stage in stages if stage["name"] == "hooks"), None)
    files_stage = next((stage for stage in stages if stage["name"] == "files"), None)
    if files_stage:
        selected_file_bytes(files_stage["items"])
    if review_stage is not None:
        observed_repo = _read_optional(api, "/repos/" + repository)
        visibility = observed_repo.get("visibility")
        if visibility != "private":
            reason = ("Review Setup supports private repositories only; remove the review stage before applying"
                      if visibility in ("public", "internal") else
                      "Cannot verify repository visibility for Review Setup: " +
                      observed_repo.get("reason", "visibility was not observed"))
            failure = _preflight_failure("review", repository, ApiError(reason))
            if visibility not in ("public", "internal"):
                failure["availability"] = observed_repo.get("status", "unknown")
            return {"mode": "apply" if apply else "dry-run", "repository": repository,
                    "changes": [failure]}
    changes = []
    operations = []
    workflow_stage = (workflow_stage_input, None, issue_state_gate_stage, agent_skills_stage) if (
        workflow_stage_input or ci_profile_stage_input or issue_state_gate_stage or agent_skills_stage or files_stage or dependabot_stage or hooks_stage
    ) else None
    workflow_changes = []
    workflow_plan = None
    workflow_error = None
    pending_gates = []
    warnings = []
    agent_skills_readiness = None
    agent_skills_compatibility = None
    if workflow_stage:
        try:
            install_options = {"agent_skills_stage": workflow_stage[3], "apply": False}
            if files_stage:
                install_options["files_stage"] = files_stage
            if ci_profile_stage_input:
                install_options["ci_profile_stage"] = ci_profile_stage_input
            if dependabot_stage:
                install_options["dependabot_stage"] = dependabot_stage
            if hooks_stage:
                install_options["hooks_stage"] = hooks_stage
            workflow_plan = install_selected(
                api, repository, workflow_stage[0], workflow_stage[1], workflow_stage[2],
                **install_options,
            )
        except (ApiError, ValueError) as error:
            if isinstance(error, HookValidationError):
                raise
            workflow_error = str(error)
    staged_paths = {item["path"] for item in workflow_plan.get("changes", [])} if workflow_plan else set()
    selected_paths = {".github/workflows/" + item["filename"]
                      for item in workflow_stage_input["workflows"] if "filename" in item} if workflow_stage_input else set()
    if ci_profile_stage_input:
        selected_paths.add(".github/workflows/" + ci_profile_stage_input.get("filename", "ci.yml"))
    if issue_state_gate_stage:
        selected_paths.update(".github/workflows/" + filename
                              for filename in ISSUE_STATE_GATE_FILES)
    if agent_skills_stage:
        selected_paths.update(render_documentation_suite())
        reader = None
        try:
            reader = _repository_file_reader(api, repository)
            agent_skills_readiness = evaluate_agent_skills_readiness(reader)
        except (ApiError, ValueError) as error:
            agent_skills_readiness = {"status": "unknown", "reason": str(error)}
        if agent_skills_readiness.get("status") == "unknown":
            warnings.append(
                "agent-skills: could not evaluate repository setup readiness: "
                + agent_skills_readiness.get("reason", "unknown")
            )
        elif not agent_skills_readiness.get("ready", False):
            missing = ", ".join(sorted(item["name"] for item in agent_skills_readiness["prerequisites"]
                                       if not item["present"]))
            warnings.append(
                "agent-skills: repository setup is incomplete (" + missing + "); staging the skill "
                "bundle does not complete setup - run the setup-matt-pocock-skills skill and "
                "re-run this policy to verify")
        agent_skills_compatibility = None
        if reader is not None and not workflow_error:
            try:
                present_paths = {path for path in
                                 ("docs/agents/issue-tracker.md", "docs/agents/triage-labels.md",
                                  "docs/agents/domain.md", "GLOSSARY.md")
                                 if reader(path) is not None}
                if "docs/agents/domain.md" in present_paths:
                    present_paths.add("docs/adr")
                skill_files, _ = _agent_skills_bundle()
                dropped = set()
                if workflow_plan and workflow_plan.get("agent_skill_upgrade"):
                    dropped = set(workflow_plan["agent_skill_upgrade"].get("removed_skills", ()))
                agent_skills_compatibility = audit_agent_skills_compatibility(
                    skill_files, available_tools=probe_cli_tools(),
                    present_paths=present_paths, excluded_skill_ids=dropped)
            except (ApiError, ValueError):
                agent_skills_compatibility = None
            if agent_skills_compatibility is not None:
                counts = agent_skills_compatibility["counts"]
                manual = [item for item in agent_skills_compatibility["skills"]
                          if item["category"] == "requires-manual-setup"]
                if manual:
                    missing_conventions = sorted({path for item in manual
                                                  for path in item["missing_conventions"]})
                    missing_tools = sorted({tool for item in manual for tool in item["missing_tools"]})
                    details = []
                    if missing_conventions:
                        details.append("missing conventions: " + ", ".join(missing_conventions))
                    if missing_tools:
                        details.append("unavailable tools: " + ", ".join(missing_tools))
                    warnings.append(
                        "agent-skills: " + str(counts["requires-manual-setup"]) + " of "
                        + str(len(agent_skills_compatibility["skills"])) + " staged skills require "
                        "manual setup before use (" + "; ".join(details) + "); run the "
                        "setup-matt-pocock-skills skill or install the missing CLIs")
                if counts["incompatible"]:
                    names = sorted(item["id"] for item in agent_skills_compatibility["skills"]
                                   if item["category"] == "incompatible")
                    warnings.append(
                        "agent-skills: " + ", ".join(names) + " reference skills that are not "
                        "staged after the acknowledged removal; they cannot run as installed")
    if dependabot_stage:
        selected_paths.add(".github/dependabot.yml")
    if hooks_stage and hooks_stage.get("opt_in"):
        from install_github_workflows import render_hook_packages
        hook_pkgs = validate_hook_packages(hooks_stage.get("packages", []))
        for pkg in hook_pkgs:
            if not pkg.is_enabled:
                warnings.append(
                    f"hooks: package '{pkg.manifest.name}' declares opt_in=false in its manifest and was ignored; "
                    f"set opt_in=true in {pkg.manifest_path.name} to stage it"
                )
        rendered_hooks = render_hook_packages(hooks_stage.get("packages", []), opt_in=True)
        selected_paths.update(rendered_hooks.keys())
    if files_stage:
        selected_paths.update(item["path"] for item in files_stage["items"] if isinstance(item, dict) and isinstance(item.get("path"), str))
    if workflow_plan and workflow_plan.get("status") == "pending_review":
        staged_paths.update(selected_paths)
    actions_planned = False
    workflows_emitted = False
    documentation_paths = set(render_documentation_suite()) if agent_skills_stage else set()
    for stage in stages:
        if stage["name"] == "review":
            continue
        if stage["name"] in ("workflows", "ci-profile", "agent-skills", "files", "dependabot", "hooks"):
            current_stage_name = stage["name"]
            if workflow_error:
                change = {"stage": current_stage_name, "scope": repository,
                          "reason": workflow_error, "status": "blocked"}
                if "advanced security" in workflow_error.lower():
                    change["availability"] = "unsupported"
                    change["guidance"] = _guidance_for_stage(current_stage_name, workflow_error, "unsupported")
                changes.append(change)
                continue
            if current_stage_name == "agent-skills":
                stage_items = [item for item in workflow_plan["changes"] if item["path"].startswith(".agents/skills/")]
            elif current_stage_name == "hooks":
                stage_items = [item for item in workflow_plan["changes"] if item["path"].startswith(".agents/hooks/")]
            elif current_stage_name == "files":
                file_paths = {item["path"] for item in files_stage["items"]}
                stage_items = [item for item in workflow_plan["changes"] if item["path"] in file_paths]
            elif current_stage_name == "ci-profile":
                ci_path = ".github/workflows/" + ci_profile_stage_input.get("filename", "ci.yml")
                stage_items = [item for item in workflow_plan["changes"] if item["path"] == ci_path]
            elif current_stage_name == "dependabot":
                stage_items = [item for item in workflow_plan["changes"] if item["path"] == ".github/dependabot.yml"]
            else:
                stage_items = [item for item in workflow_plan["changes"]
                               if not item["path"].startswith(".agents/skills/")
                               and not item["path"].startswith(".agents/hooks/")
                               and item["path"] not in documentation_paths
                               and item["path"] != ".github/dependabot.yml"
                               and item["path"] not in ({entry["path"] for entry in files_stage["items"]} if files_stage else set())]
            for item in stage_items:
                change = dict(item, stage=current_stage_name, status="planned")
                if current_stage_name == "dependabot":
                    change["reason"] = f"Configure Dependabot with {stage.get('cadence', 'monthly')} update cadence and grouped pull requests"
                elif current_stage_name == "agent-skills":
                    if item.get("operation") == "remove":
                        change["reason"] = ("Remove skill dropped by the upstream catalog revision; "
                                            "explicitly acknowledged selection, never a silent deletion")
                    else:
                        _, bundle_manifest = _agent_skills_bundle()
                        change["reason"] = (
                            f"Stage reviewed Matt Pocock skills from {bundle_manifest['source_repository']} "
                            f"at commit {bundle_manifest['source_commit']} ({bundle_manifest['source_version']})"
                        )
                        change["desired"] = {
                            "bundle": "mattpocock",
                            "source_commit": bundle_manifest["source_commit"],
                            "source_version": bundle_manifest["source_version"],
                            "file_count": len(bundle_manifest["files"]),
                        }
                elif current_stage_name == "hooks":
                    change["reason"] = f"Stage opt-in hook package at {item['path']}"
                workflow_changes.append(change)
                changes.append(change)
            if current_stage_name == "agent-skills":
                for item in workflow_plan["changes"]:
                    if item["path"] not in documentation_paths:
                        continue
                    change = dict(item, stage="documentation-freshness", status="planned",
                                  reason="Stage the trusted documentation audit and managed report alongside agent skills")
                    workflow_changes.append(change)
                    changes.append(change)
            continue
        if stage["name"] in ("gates", "issue-state-gate"):
            paths = set(_gate_checks(stage).values()) if stage["name"] == "gates" else {".github/workflows/issue-state-gate.yml"}
            if workflow_error and paths & selected_paths:
                blocked = {"stage": stage["name"], "scope": repository, "status": "blocked",
                           "reason": "Selected workflows could not be staged; resolve the workflow conflict before configuring this gate"}
                if stage["name"] == "issue-state-gate":
                    blocked.update(_issue_gate_details())
                changes.append(blocked)
                continue
            if stage["name"] == "gates":
                gate_checks = _gate_checks(stage)
                discrepancy = _detect_profile_check_discrepancy(
                    gate_checks,
                    workflow_stage_input=workflow_stage_input,
                    ci_profile_stage_input=ci_profile_stage_input,
                    api=api,
                    repository=repository,
                )
                if discrepancy:
                    blocked = {
                        "stage": "gates",
                        "scope": repository,
                        "status": "blocked",
                        "reason": discrepancy,
                    }
                    warnings.append(f"gates: {discrepancy}")
                    changes.append(blocked)
                    continue
            dependent = sorted(paths & staged_paths)
            if dependent:
                change = {"stage": stage["name"], "scope": repository, "status": "pending",
                          "reason": ("Merge the draft Setup Pull Request, then wait for a successful exact-source Issue State Gate check before applying this rule: " if stage["name"] == "issue-state-gate" else "Merge the draft Setup Pull Request, then provide an approved evidence PR with successful CI checks before applying this gate: ") + ", ".join(dependent)}
                if stage["name"] == "issue-state-gate":
                    change.update(_issue_gate_details())
                pending_gates.append(change)
                changes.append(change)
                continue
        try:
            observed_issue_gate = {} if stage["name"] == "issue-state-gate" else None
            planned = _stage_changes(api, repository, stage, planned_actions=actions_planned,
                                     issue_gate_details=observed_issue_gate)
            if stage["name"] == "issue-state-gate":
                gate_report = {"stage": "issue-state-gate", "scope": repository,
                               "operation": "inspect", "status": "planned",
                               "reason": "Read and verify the active Issue State Gate rule without changing repository protections."}
                gate_report.update(_issue_gate_details())
                if observed_issue_gate:
                    gate_report.update(observed_issue_gate)
                changes.append(gate_report)
            for method, path, desired in planned:
                if path.endswith("/vulnerability-alerts"):
                    reason = "enables vulnerability alerts and dependency graph"
                elif path.endswith("/automated-security-fixes"):
                    reason = "enables automated security fixes for detected vulnerabilities"
                else:
                    reason = "selected by policy"
                current = _current(api, stage["name"], method, path, desired)
                change = {"stage": stage["name"], "operation": method, "path": path,
                          "current": current,
                          "desired": _public_desired(stage["name"], desired), "scope": repository,
                          "reason": reason, "status": "planned"}
                if stage["name"] == "issue-state-gate":
                    change.update(_issue_gate_details())
                    if observed_issue_gate:
                        change.update(observed_issue_gate)
                operations.append((change, method, path, desired))
                changes.append(change)
        except (ApiError, ValueError) as error:
            if isinstance(error, ValueError) and stage["name"] not in ("gates", "issue-state-gate"):
                raise
            early_exit = _handle_stage_preflight_failure(
                stage["name"], repository, error, changes, warnings, apply
            )
            if early_exit is not None:
                return early_exit
            continue
        if stage["name"] == "actions":
            actions_planned = True
    result = {"mode": "apply" if apply else "dry-run", "repository": repository, "changes": changes}
    if warnings:
        result["warnings"] = warnings
    if workflow_plan and workflow_plan.get("pull_request"):
        result["setup_pull_request"] = workflow_plan["pull_request"]
    if workflow_plan and workflow_plan.get("agent_skill_upgrade"):
        result["agent_skill_upgrade"] = workflow_plan["agent_skill_upgrade"]
    if not changes and not result.get("setup_pull_request"):
        result["status"] = "no-op"
    if agent_skills_stage:
        _, bundle_manifest = _agent_skills_bundle()
        result["agent_skills_bundle"] = {
            "bundle": "mattpocock",
            "source_repository": bundle_manifest["source_repository"],
            "source_commit": bundle_manifest["source_commit"],
            "source_version": bundle_manifest["source_version"],
            "file_count": len(bundle_manifest["files"]),
            "destination": ".agents/skills/",
        }
        result["agent_skills_readiness"] = agent_skills_readiness
        if agent_skills_compatibility is not None:
            result["agent_skills_compatibility"] = agent_skills_compatibility
    policy_digest = compute_policy_sha256(policy)
    if not apply:
        result["policy_sha256"] = policy_digest
        return result
    if any(is_fatal_block(change) for change in changes):
        return result
    if not isinstance(accepted_dry_run, dict):
        missing_res = {
            "mode": "apply",
            "repository": repository,
            "changes": [{
                "stage": "policy",
                "scope": repository,
                "operation": "inspect",
                "status": "blocked",
                "reason": "Direct setting writes without an accepted Dry-Run are blocked; inspect a Dry-Run first.",
            }],
        }
        if warnings:
            missing_res["warnings"] = warnings
        return missing_res
    if (
        accepted_dry_run.get("repository") != repository or
        accepted_dry_run.get("policy_sha256") != policy_digest or
        _planned_identity(accepted_dry_run.get("changes", [])) != _planned_identity(changes)
    ):
        mismatch_res = {"mode": "apply", "repository": repository, "changes": [{
            "stage": "policy", "scope": repository, "operation": "inspect",
            "status": "blocked", "reason": "Repository state changed since the accepted Dry-Run; inspect a new Dry-Run before applying."}]}
        if warnings:
            mismatch_res["warnings"] = warnings
        return mismatch_res
    if workflow_plan:
        try:
            _verify_workflow_identity(api, repository, workflow_stage, files_stage, workflow_plan, ci_profile_stage=ci_profile_stage_input, dependabot_stage=dependabot_stage, hooks_stage=hooks_stage)
        except (ApiError, ValueError) as error:
            return {"mode": "apply", "repository": repository, "changes": [{
                "stage": "workflows", "scope": repository, "operation": "inspect",
                "status": "blocked", "reason": str(error)}]}
    try:
        for change, method, path, desired in operations:
            if method == "POST":
                stage = next(item for item in stages if item["name"] == change["stage"])
                if (method, path, desired) not in _stage_changes(api, repository, stage):
                    raise ApiError("Repository state changed before write; inspect a new Dry-Run")
            observed_current = _current(api, change["stage"], method, path, desired)
            selected_actions_unlocked = (
                change["stage"] == "actions-policy" and
                path.endswith("/actions/permissions/selected-actions") and
                change["current"] == {"http_status": 409} and
                isinstance(observed_current, dict) and "http_status" not in observed_current and
                any(prior["path"] == path.removesuffix("/selected-actions") and
                    prior["status"] == "applied" and
                    prior["desired"].get("allowed_actions") == "selected"
                    for prior in changes if isinstance(prior, dict) and "path" in prior)
            )
            if observed_current != change["current"] and not selected_actions_unlocked:
                raise ApiError("Repository state changed before write; inspect a new Dry-Run")
            response_status, response_body = api.request(method, path, desired)
            change["status"] = "applied"
            if change["stage"] == "codeql":
                run_id = response_body.get("run_id") if isinstance(response_body, dict) else None
                if response_status == 202 and type(run_id) is int and run_id > 0:
                    change["validation_run_id"] = run_id
                    change["status"] = "pending"
                else:
                    change["status"] = "unverified"
    except (ApiError, ValueError) as error:
        change["status"] = "failed"
        change["reason"] = str(error)
        failed_change = change
        for earlier, earlier_method, earlier_path, earlier_desired in operations:
            if earlier is failed_change:
                continue
            if earlier["status"] == "pending":
                continue
            if earlier["status"] != "applied":
                earlier["status"] = "pending"
                earlier["reason"] = "Not attempted after the earlier setting operation failed"
                continue
            try:
                if earlier_method == "POST":
                    stage = next(item for item in stages if item["name"] == earlier["stage"])
                    verified = ((earlier_method, earlier_path, earlier_desired)
                                not in _stage_changes(api, repository, stage))
                else:
                    verified = (_current(api, earlier["stage"], earlier_method,
                                         earlier_path, earlier_desired) == earlier["desired"])
                earlier["status"] = "verified" if verified else "unverified"
                if not verified:
                    earlier["reason"] = "Readback did not confirm the requested setting"
            except (ApiError, ValueError) as read_error:
                earlier["status"] = "unverified"
                earlier["reason"] = "Readback unavailable: " + str(read_error)
        for file_change in workflow_changes:
            if file_change["status"] == "planned":
                file_change["status"] = "pending"
                file_change["reason"] = "Not attempted after the earlier setting operation failed"
        result["recovery"] = (
            f"Inspect {repository} and resolve the failed setting; save and accept a new Dry-Run "
            "from observed repository state before retrying pending or unverified operations."
        )
        return result
    if workflow_stage and workflow_plan:
        workflow_staging_started = False
        try:
            _verify_workflow_identity(api, repository, workflow_stage, files_stage, workflow_plan, ci_profile_stage=ci_profile_stage_input, dependabot_stage=dependabot_stage, hooks_stage=hooks_stage)
            install_options = {"apply": True}
            if files_stage:
                install_options["files_stage"] = files_stage
            if ci_profile_stage_input:
                install_options["ci_profile_stage"] = ci_profile_stage_input
            if dependabot_stage:
                install_options["dependabot_stage"] = dependabot_stage
            if hooks_stage:
                install_options["hooks_stage"] = hooks_stage
            workflow_staging_started = True
            staged = install_selected(api, repository, *workflow_stage, **install_options)
            result["setup_pull_request"] = staged.get("pull_request")
            result["setup_branch"] = staged.get("branch")
            for change in workflow_changes:
                change["status"] = "pending"
                change["reason"] = "Inspect and merge the draft Setup Pull Request"
        except (ApiError, ValueError) as error:
            if isinstance(error, StagingFailure):
                result["setup_branch"] = error.branch
                for change in workflow_changes:
                    change["status"] = error.file_statuses.get(change["path"], "unverified")
                    if change["status"] == "applied":
                        change["reason"] = "Staged on the Setup branch; retry to finish the Setup Pull Request"
                    elif change["status"] == "pending":
                        change["reason"] = "Not staged yet; retry after resolving the staging failure"
                    else:
                        change["reason"] = str(error)
                if not any(change["status"] in ("failed", "unverified") for change in workflow_changes):
                    changes.append({"stage": "workflows", "scope": repository,
                                    "operation": "create-setup-pull-request", "status": "failed",
                                    "reason": str(error)})
            else:
                for change in workflow_changes:
                    change["status"] = "failed" if workflow_staging_started else "pending"
                    change["reason"] = str(error) if workflow_staging_started else "Not attempted; workflow identity check failed"
                if not workflow_staging_started:
                    changes.append({"stage": "workflows", "scope": repository,
                                    "operation": "inspect", "status": "failed", "reason": str(error)})
            result["recovery"] = f"Inspect the workflow staging branch for {repository} before retrying: {error}"
            for gate in pending_gates:
                gate["status"] = "blocked"
                gate["reason"] = "Workflow staging failed; resolve the Setup Pull Request before configuring this gate"
    remaining = set()
    unreadable_stages = {}
    for stage in stages:
        if stage["name"] in ("workflows", "ci-profile", "review", "agent-skills", "files", "dependabot", "hooks") or (stage["name"] == "gates" and
                                            any(item["stage"] == "gates" and item["status"] in ("pending", "blocked")
                                                for item in changes)):
            continue
        try:
            for method, path, desired in _stage_changes(api, repository, stage):
                remaining.add((method, path, json.dumps(desired, sort_keys=True)))
        except (ApiError, ValueError) as error:
            unreadable_stages[stage["name"]] = str(error)
    for change, method, path, desired in operations:
        if change["stage"] in unreadable_stages:
            change["status"] = "unverified"
            change["reason"] = "Readback unavailable: " + unreadable_stages[change["stage"]]
            continue
        key = (method, path, json.dumps(desired, sort_keys=True))
        if key not in remaining and change["status"] not in ("pending", "unverified"):
            change["status"] = "verified"
        elif key in remaining:
            change["status"] = "unverified"
    for change in changes:
        if change.get("stage") == "issue-state-gate" and change.get("operation") == "inspect":
            if any(item.get("stage") == "issue-state-gate" and item.get("status") in ("pending", "blocked", "failed", "unverified")
                   for item in changes):
                change["status"] = "blocked"
            elif apply:
                change["status"] = "verified"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="OWNER/REPO")
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inventory", help="read-only repository setup inventory")
    policy_parser = sub.add_parser("policy", help="inspect a Dry-Run or apply an explicit setup Policy")
    policy_parser.add_argument("--file", required=True, type=Path)
    policy_parser.add_argument("--apply", action="store_true")
    policy_parser.add_argument("--dry-run-result", metavar="PATH", dest="dry_run_result",
                               help="saved Dry-Run JSON required for apply")
    policy_parser.add_argument("--plan", metavar="PATH", dest="dry_run_result",
                               help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    import os
    token = os.environ.get(args.token_env)
    if not token:
        parser.error(f"set {args.token_env} before running")
    try:
        if args.command == "inventory":
            result = inventory(GitHubApi(token), args.repo)
        elif args.command == "policy":
            try:
                from scripts.headless import run_headless_apply
            except ImportError:
                from headless import run_headless_apply
            return run_headless_apply(str(args.file), repo=args.repo, token=token,
                                      apply=args.apply, plan_path=args.dry_run_result)
    except (ApiError, ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
