#!/usr/bin/env python3
"""Reconcile supported GitHub repository settings without changing content.

Dry run is the default. Only --apply sends writes. Requires a token with repository
Administration permission; the token is read from an environment variable.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Optional
import urllib.error
import urllib.parse
import urllib.request


API_VERSION = "2022-11-28"
FEATURES = {"issues": "has_issues", "wiki": "has_wiki", "projects": "has_projects"}
SECURITY_FEATURES = (
    "vulnerability-alerts", "automated-security-fixes", "secret-scanning",
    "push-protection", "private-vulnerability-reporting",
)
MERGE_SETTINGS = (
    "allow_merge_commit", "allow_squash_merge", "allow_rebase_merge",
    "delete_branch_on_merge", "allow_update_branch",
)
MERGE_METHODS = MERGE_SETTINGS[:3]


class ApiError(RuntimeError):
    pass


def resolve_github_token(
    token_env: str = "GITHUB_TOKEN",
    explicit_token: Optional[str] = None,
) -> Optional[str]:
    """Resolve a GitHub API token from an explicit value, environment variable, or gh CLI.

    When ``token_env`` is the default ``"GITHUB_TOKEN"``, falls back in order:
    ``GITHUB_TOKEN`` -> ``GH_TOKEN`` -> ``gh auth token``.
    When a custom ``token_env`` is specified, reads only that environment variable
    and fails closed without fallback.
    """
    if explicit_token is not None:
        stripped = explicit_token.strip()
        return stripped if stripped else None

    if token_env != "GITHUB_TOKEN":
        val = os.environ.get(token_env)
        return val.strip() if val and val.strip() else None

    for env_name in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(env_name)
        if val and val.strip():
            return val.strip()

    try:
        res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return None


class GitHubApi:
    def __init__(self, token, base_url="https://api.github.com"):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.scopes = None

    def request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + self.token,
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "github-repository-configurator",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
                oauth_scopes = response.headers.get("X-OAuth-Scopes")
                if oauth_scopes is not None:
                    self.scopes = [s.strip() for s in oauth_scopes.split(",") if s.strip()]
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            oauth_scopes = getattr(error, "headers", {}).get("X-OAuth-Scopes") if hasattr(error, "headers") else None
            if oauth_scopes is not None:
                self.scopes = [s.strip() for s in oauth_scopes.split(",") if s.strip()]
            raw = error.read().decode("utf-8", errors="replace")
            if method == "GET" and error.code == 404:
                return 404, None
            try:
                message = json.loads(raw).get("message", raw)
            except ValueError:
                message = raw
            raise ApiError(f"{method} {path}: HTTP {error.code}: {message}") from None
        except urllib.error.URLError as error:
            raise ApiError(f"{method} {path}: {error.reason}") from None


def get(api, path):
    status, body = api.request("GET", path)
    if status != 200:
        raise ApiError(f"GET {path}: HTTP {status}; check repository and token permissions")
    return body


def get_object(api, path):
    body = get(api, path)
    if not isinstance(body, dict):
        raise ApiError(f"GET {path}: cannot verify malformed response")
    return body


def get_pages(api, path, key=None):
    """Read complete small GitHub collections; reject unexpected response shapes."""
    items = []
    for page in range(1, 101):
        separator = "&" if "?" in path else "?"
        body = get(api, f"{path}{separator}per_page=100&page={page}")
        batch = body.get(key) if key and isinstance(body, dict) else body
        if not isinstance(batch, list):
            raise ApiError(f"cannot verify paginated {key or 'list'} response")
        items.extend(batch)
        if len(batch) < 100:
            return items
    raise ApiError("collection exceeds 100 pages; cannot verify complete preflight")


def write(api, method, path, payload, changes, apply):
    changes.append((method, path, payload))
    if apply:
        api.request(method, path, payload)


def apply_changes(api, changes, apply):
    if apply:
        for method, path, payload in changes:
            api.request(method, path, payload)
    return changes


def reconcile_features(api, repository, features=None, disable=None, apply=False):
    """Enable or disable selected repository features."""
    features = features or []
    disable = disable or []
    if not features and not disable:
        raise ValueError("select one or more supported repository features")
    if any(feature not in FEATURES for feature in features):
        raise ValueError("select one or more supported repository features")
    if any(feature not in FEATURES for feature in disable):
        raise ValueError("select one or more supported repository features")
    base = "/repos/" + repository
    changes = []
    repo = get_object(api, base)
    wanted = {}
    for feature in features:
        wanted[FEATURES[feature]] = True
    for feature in disable:
        wanted[FEATURES[feature]] = False
    if any(type(repo.get(key)) is not bool for key in wanted):
        raise ApiError("cannot verify selected repository feature state")
    patch = {key: value for key, value in wanted.items() if repo.get(key) != value}
    if patch:
        write(api, "PATCH", base, patch, changes, False)
    return apply_changes(api, changes, apply)


def reconcile_actions(api, repository, apply=False):
    """Enable Actions and set default workflow tokens to read-only."""
    base = "/repos/" + repository
    changes = []
    actions_path = base + "/actions/permissions"
    actions = get_object(api, actions_path)
    if type(actions.get("enabled")) is not bool:
        raise ApiError("cannot verify Actions enabled state")
    if not actions.get("enabled", False):
        write(api, "PUT", actions_path, {"enabled": True}, changes, False)

    workflow_path = actions_path + "/workflow"
    workflow = get_object(api, workflow_path)
    if (workflow.get("default_workflow_permissions") not in ("read", "write") or
        type(workflow.get("can_approve_pull_request_reviews")) is not bool):
        raise ApiError("cannot verify workflow token permissions")
    workflow_patch = {}
    if workflow.get("default_workflow_permissions") != "read":
        workflow_patch["default_workflow_permissions"] = "read"
    if workflow.get("can_approve_pull_request_reviews") is not False:
        workflow_patch["can_approve_pull_request_reviews"] = False
    if workflow_patch:
        write(api, "PUT", workflow_path, workflow_patch, changes, False)
    return apply_changes(api, changes, apply)


def reconcile_merge(api, repository, settings, apply=False):
    """Change only selected merge settings without disabling every method."""
    if not isinstance(settings, dict) or not settings or any(
        key not in MERGE_SETTINGS or type(value) is not bool
        for key, value in settings.items()
    ):
        raise ValueError("select supported boolean merge settings")
    base = "/repos/" + repository
    repo = get_object(api, base)
    current = {key: repo.get(key) for key in MERGE_SETTINGS}
    if any(type(value) is not bool for value in current.values()):
        raise ApiError("cannot verify all current merge settings")
    if not any(settings.get(key, current[key]) for key in MERGE_METHODS):
        raise ValueError("at least one merge method must remain enabled")
    patch = {key: value for key, value in settings.items() if current[key] != value}
    return apply_changes(api, [("PATCH", base, patch)] if patch else [], apply)


def reconcile_actions_policy(api, repository, settings, apply=False):
    """Reconcile selected Actions policy, optional allowlist, and default workflow permissions."""
    allowed_keys = {"allowed_actions", "sha_pinning_required", "github_owned_allowed",
                    "verified_allowed", "patterns_allowed", "workflow_permissions",
                    "default_workflow_permissions"}
    if not isinstance(settings, dict) or not settings or not set(settings) <= allowed_keys:
        raise ValueError("select supported Actions policy settings")
    allowed_actions = settings.get("allowed_actions")
    if allowed_actions is not None and allowed_actions not in ("all", "local_only", "selected"):
        raise ValueError("invalid allowed_actions value")
    if "sha_pinning_required" in settings and type(settings["sha_pinning_required"]) is not bool:
        raise ValueError("sha_pinning_required must be boolean")
    workflow_perm = settings.get("workflow_permissions") or settings.get("default_workflow_permissions")
    if workflow_perm is not None and workflow_perm not in ("read", "write"):
        raise ValueError("workflow_permissions must be 'read' or 'write'")
    selected_keys = {"github_owned_allowed", "verified_allowed", "patterns_allowed"}
    if set(settings) & selected_keys and allowed_actions != "selected":
        raise ValueError("selected action settings require allowed_actions=selected")
    for key in ("github_owned_allowed", "verified_allowed"):
        if key in settings and type(settings[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    if "patterns_allowed" in settings and (
        not isinstance(settings["patterns_allowed"], list) or any(
            not isinstance(value, str) or not value for value in settings["patterns_allowed"]
        )
    ):
        raise ValueError("patterns_allowed must be a list of nonempty strings")
    if allowed_actions == "selected" and not selected_keys <= set(settings):
        raise ValueError("selected actions require a complete allowlist policy")
    base = "/repos/" + repository
    path = base + "/actions/permissions"
    current = get_object(api, path)
    if allowed_actions is not None and current.get("allowed_actions") not in ("all", "local_only", "selected"):
        raise ApiError("cannot verify current allowed Actions policy")
    if "sha_pinning_required" in settings and type(current.get("sha_pinning_required")) is not bool:
        raise ApiError("cannot verify current Actions SHA pinning state")
    if settings.get("patterns_allowed") and get_object(api, base).get("visibility") != "public":
        raise ValueError("patterns_allowed is supported only for public repositories")
    wf_keys = {"workflow_permissions", "default_workflow_permissions"}
    patch = {key: value for key, value in settings.items()
             if key not in selected_keys and key not in wf_keys and current.get(key) != value}
    changes = []
    if patch:
        if type(current.get("enabled")) is not bool:
            raise ApiError("cannot verify current Actions enabled state")
        if current.get("allowed_actions") not in ("all", "local_only", "selected"):
            raise ApiError("cannot verify current allowed Actions policy")
        if type(current.get("sha_pinning_required")) is not bool:
            raise ApiError("cannot verify current Actions SHA pinning state")
        desired = {key: current[key] for key in ("enabled", "allowed_actions", "sha_pinning_required")}
        desired.update(patch)
        changes.append(("PUT", path, desired))
    if allowed_actions == "selected":
        desired = {key: settings[key] for key in selected_keys}
        if current.get("allowed_actions") == "selected":
            existing = get_object(api, path + "/selected-actions")
        else:
            existing = None  # This endpoint is inaccessible until the policy changes.
        if existing is None or any(existing.get(key) != value for key, value in desired.items()):
            changes.append(("PUT", path + "/selected-actions", desired))
    if workflow_perm is not None:
        wf_path = path + "/workflow"
        current_wf = get_object(api, wf_path)
        if (current_wf.get("default_workflow_permissions") not in ("read", "write") or
            type(current_wf.get("can_approve_pull_request_reviews")) is not bool):
            raise ApiError("cannot verify workflow token permissions")
        wf_patch = {}
        if current_wf.get("default_workflow_permissions") != workflow_perm:
            wf_patch["default_workflow_permissions"] = workflow_perm
        if workflow_perm == "read" and current_wf.get("can_approve_pull_request_reviews") is not False:
            wf_patch["can_approve_pull_request_reviews"] = False
        if wf_patch:
            changes.append(("PUT", wf_path, wf_patch))
    return apply_changes(api, changes, apply)


def reconcile_security(api, repository, features, apply=False):
    """Enable only selected security features whose state can be observed."""
    if not features or any(feature not in SECURITY_FEATURES for feature in features):
        raise ValueError("select one or more supported security features")
    base = "/repos/" + repository
    repo = get_object(api, base)
    changes = []
    analysis_names = {
        "secret-scanning": "secret_scanning",
        "push-protection": "secret_scanning_push_protection",
    }
    security_patch = {}
    for feature in features:
        if feature not in analysis_names:
            continue
        analysis = repo.get("security_and_analysis")
        current = analysis.get(analysis_names[feature]) if isinstance(analysis, dict) else None
        if not isinstance(current, dict) or current.get("status") not in ("enabled", "disabled"):
            raise ApiError(f"cannot verify {feature} state on {repository}")
        if current["status"] == "disabled":
            security_patch[analysis_names[feature]] = {"status": "enabled"}
    if security_patch:
        write(api, "PATCH", base, {"security_and_analysis": security_patch}, changes, False)

    for feature in features:
        if feature in analysis_names:
            continue
        if feature == "private-vulnerability-reporting" and repo.get("visibility") != "public":
            raise ApiError("private vulnerability reporting is available only for public repositories")
        path = base + "/" + feature
        status, body = api.request("GET", path)
        if status == 404:
            raise ApiError(f"GET {path}: HTTP 404; state is unknown (resource may be absent or inaccessible)")
        if feature == "vulnerability-alerts":
            enabled = status == 204 or (status == 200 and isinstance(body, dict) and body.get("enabled") is True)
            valid = status == 204 or (status == 200 and isinstance(body, dict) and type(body.get("enabled")) is bool)
        elif feature == "automated-security-fixes":
            enabled = status == 200 and isinstance(body, dict) and body.get("enabled") is True
            valid = status == 200 and isinstance(body, dict) and type(body.get("enabled")) is bool
        else:
            enabled = status == 200 and isinstance(body, dict) and body.get("enabled") is True
            valid = status == 200 and isinstance(body, dict)
        if not valid:
            raise ApiError(f"GET {path}: unexpected HTTP {status}")
        if not enabled:
            write(api, "PUT", path, None, changes, False)
    return apply_changes(api, changes, apply)


def reconcile_codeql(api, repository, apply=False, planned_actions=False, query_suite="default"):
    """Enable CodeQL default setup when Actions, language support, and the API support it."""
    if query_suite not in ("default", "extended"):
        raise ValueError(f"unsupported query_suite: {query_suite!r}; choose 'default' or 'extended'")
    base = "/repos/" + repository
    actions = get_object(api, base + "/actions/permissions")
    if type(actions.get("enabled")) is not bool:
        raise ApiError("cannot verify Actions enabled state")
    if actions.get("enabled") is not True and not planned_actions:
        raise ApiError("Actions must be enabled before CodeQL default setup")
    path = base + "/code-scanning/default-setup"
    configuration = get_object(api, path)
    if configuration.get("languages") == []:
        raise ApiError("CodeQL default setup is unavailable: no CodeQL-supported languages detected in repository")
    state = configuration.get("state")
    if state in ("configured", "enforced"):
        current_suite = configuration.get("query_suite") or "default"
        if current_suite == query_suite:
            return []
        desired = {"state": "configured", "query_suite": query_suite}
        return apply_changes(api, [("PATCH", path, desired)], apply)
    if state != "not-configured":
        raise ApiError(f"cannot verify CodeQL default setup state: {state!r}")
    desired = {"state": "configured"}
    if query_suite != "default":
        desired["query_suite"] = query_suite
    return apply_changes(api, [("PATCH", path, desired)], apply)


def reconcile_gates(
    api,
    repository,
    ruleset_name,
    pull_number,
    reviewer,
    checks,
    apply=False,
    required_approving_review_count=1,
):
    """Create a branch ruleset after an evidence PR proves each workflow check."""
    if not isinstance(required_approving_review_count, int) or required_approving_review_count < 0:
        raise ValueError("required_approving_review_count must be a non-negative integer")
    if not isinstance(checks, dict) or not checks or any(
        not name or not re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", path)
        for name, path in checks.items()
    ):
        raise ValueError("provide unique check names mapped to workflow paths")
    base = "/repos/" + repository
    repo = get_object(api, base)
    branch = repo.get("default_branch")
    if not branch:
        raise ApiError("cannot verify default branch")
    if repo.get("visibility") != "public":
        owner = repo.get("owner")
        if not isinstance(owner, dict) or owner.get("type") not in ("User", "Organization"):
            raise ApiError("cannot verify private repository owner and plan")
        owner_path = "orgs" if owner["type"] == "Organization" else "users"
        owner_info = get_object(api, f"/{owner_path}/{urllib.parse.quote(owner['login'], safe='')}")
        plan = owner_info.get("plan")
        if not isinstance(plan, dict) or plan.get("name", "").lower() not in (
            "pro", "team", "business", "enterprise"
        ):
            raise ApiError("private repository rulesets require a verifiable Pro, Team, or Enterprise plan")
    pr_path = base + f"/pulls/{pull_number}"
    pr = get_object(api, pr_path)
    if pr["base"]["ref"] != branch:
        raise ValueError("evidence PR must target the default branch")
    if pr.get("state") not in (None, "open"):
        raise ValueError("evidence PR must be open")
    head = pr["head"]["sha"]
    if required_approving_review_count > 0:
        if not reviewer:
            raise ValueError("reviewer is required when required_approving_review_count > 0")
        user = pr.get("user")
        author = user.get("login", "") if isinstance(user, dict) else ""
        if not author or author.casefold() == reviewer.casefold():
            raise ValueError("reviewer must be independent of the evidence PR author")
        encoded_reviewer = urllib.parse.quote(reviewer, safe="")
        permission = get(api, base + "/collaborators/" + encoded_reviewer + "/permission")["permission"]
        if permission not in ("write", "maintain", "admin"):
            raise ValueError("reviewer needs write, maintain, or admin permission")
        reviews = get_pages(api, pr_path + "/reviews")
        reviewer_reviews = [
            review for review in reviews
            if isinstance(review.get("user"), dict)
            and review["user"].get("login", "").casefold() == reviewer.casefold()
        ]
        decisive_reviews = [
            r for r in reviewer_reviews
            if r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
        ]
        if not decisive_reviews or decisive_reviews[-1].get("state") != "APPROVED" or decisive_reviews[-1].get("commit_id") != head:
            raise ValueError("reviewer approval on the current PR head is required")
    workflows = get_pages(api, base + "/actions/workflows", "workflows")
    installed = {workflow.get("path"): workflow for workflow in workflows
                 if workflow.get("state") == "active"}
    for path in checks.values():
        if not installed.get(path):
            raise ValueError(f"installed workflow is missing or inactive: {path}")
    try:
        from install_github_workflows import inspect_remote_workflow_status_check
    except ImportError:
        try:
            from scripts.install_github_workflows import inspect_remote_workflow_status_check
        except Exception:
            inspect_remote_workflow_status_check = None
    if inspect_remote_workflow_status_check is not None:
        for check, path in checks.items():
            declared_check = inspect_remote_workflow_status_check(api, repository, path, ref=branch)
            if declared_check and check != declared_check:
                raise ValueError(
                    f"discrepancy detected: configured ruleset check '{check}' does not match "
                    f"profile workflow declared check '{declared_check}' for {path}"
                )
    candidate_shas = [head]
    if pr.get("merge_commit_sha") and pr["merge_commit_sha"] != head:
        candidate_shas.append(pr["merge_commit_sha"])
    proven = {}
    for candidate in candidate_shas:
        sha = urllib.parse.quote(candidate, safe="")
        check_runs = get_pages(api, base + f"/commits/{sha}/check-runs", "check_runs")
        for check, path in checks.items():
            workflow_id = installed[path]["id"]
            run_path = base + f"/actions/workflows/{workflow_id}/runs?head_sha={sha}&event=pull_request"
            workflow_runs = get_pages(api, run_path, "workflow_runs")
            suites = {run.get("check_suite_id") for run in workflow_runs
                      if run.get("workflow_id") == workflow_id and run.get("head_sha") == candidate
                      and run.get("event") == "pull_request" and run.get("status") == "completed"
                      and run.get("conclusion") == "success"}
            matches = [run for run in check_runs if run.get("name") == check]
            if len(matches) > 1:
                raise ValueError(f"ambiguous check name on {candidate}: {check}")
            if matches and matches[0].get("head_sha") == candidate and (
                matches[0].get("status") == "completed" and matches[0].get("conclusion") == "success"
                and matches[0].get("check_suite", {}).get("id") in suites
                and isinstance(matches[0].get("app", {}).get("id"), int)
            ):
                proven[check] = matches[0]["app"]["id"]
    missing = set(checks) - set(proven)
    if missing:
        raise ValueError("missing successful check: " + ", ".join(sorted(missing)))
    latest = get_object(api, pr_path)
    if latest["head"]["sha"] != head or latest.get("merge_commit_sha") != pr.get("merge_commit_sha"):
        raise ValueError("evidence PR changed during preflight; retry")
    payload = {
        "name": ruleset_name,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/" + branch], "exclude": []}},
        "rules": [
            {"type": "pull_request", "parameters": {
                "required_approving_review_count": required_approving_review_count,
                "dismiss_stale_reviews_on_push": True,
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_review_thread_resolution": True,
            }},
            {"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": [{"context": check, "integration_id": proven[check]}
                                           for check in checks],
            }},
        ],
    }
    rulesets = get(api, base + "/rulesets?includes_parents=false&per_page=100")
    if len(rulesets) == 100:
        raise ApiError("ruleset list has 100 entries; refusing to create a possible duplicate")
    ours = [item for item in rulesets if item["name"] == ruleset_name]
    if len(ours) > 1:
        raise ApiError("multiple managed rulesets exist; resolve duplicates manually")
    changes = []
    if not ours:
        write(api, "POST", base + "/rulesets", payload, changes, apply)
    else:
        current = get(api, base + f"/rulesets/{ours[0]['id']}")
        if current.get("bypass_actors") not in (None, []):
            raise ApiError("existing ruleset contains unmanaged bypass actors; refusing replacement")
        conditions = current.get("conditions")
        if conditions != payload["conditions"]:
            raise ApiError("existing ruleset contains unmanaged conditions; refusing replacement")
        if any(current.get(key) != value for key, value in payload.items()):
            current_rules = current.get("rules")
            managed_types = {rule["type"] for rule in payload["rules"]}
            if not isinstance(current_rules, list) or any(
                not isinstance(rule, dict) or rule.get("type") not in managed_types
                for rule in current_rules
            ):
                raise ApiError("existing ruleset contains unmanaged rules; refusing replacement")
            desired_rules = {rule["type"]: rule for rule in payload["rules"]}
            for rule in current_rules:
                parameters = rule.get("parameters", {})
                desired_parameters = desired_rules[rule["type"]].get("parameters", {})
                if not isinstance(parameters, dict) or set(parameters) - set(desired_parameters):
                    raise ApiError("existing ruleset contains unmanaged rule parameters; refusing replacement")
                if rule["type"] == "required_status_checks":
                    selected = {check["context"] for check in
                                desired_parameters["required_status_checks"]}
                    if any(check.get("context") not in selected for check in
                           parameters.get("required_status_checks", [])):
                        raise ApiError("existing ruleset contains unmanaged required checks; refusing replacement")
            write(api, "PUT", base + f"/rulesets/{ours[0]['id']}", payload, changes, apply)
    return changes


def reconcile_issue_state_gate(api, repository, apply=False, details=None):
    """Require the trusted Issue State Gate check on the default branch."""
    base = "/repos/" + repository
    repo = get_object(api, base)
    branch = repo.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise ApiError("cannot verify default branch for Issue State Gate")
    if branch != "main":
        raise ValueError("Issue State Gate supports repositories whose default branch is main")
    workflows = get_pages(api, base + "/actions/workflows", "workflows")
    workflow = next((item for item in workflows if item.get("path") ==
                     ".github/workflows/issue-state-gate.yml" and item.get("state") == "active"), None)
    if workflow is None or type(workflow.get("id")) is not int:
        raise ValueError("Issue State Gate workflow is not active; merge the Setup Pull Request first")
    pulls = get(api, base + "/pulls?state=open&base=" + urllib.parse.quote(branch, safe="") + "&per_page=100")
    if not isinstance(pulls, list) or len(pulls) >= 100:
        raise ApiError("cannot verify all open pull requests for Issue State Gate evidence")
    integration_id = None
    for pull in pulls:
        if not isinstance(pull, dict) or not isinstance(pull.get("head"), dict):
            raise ApiError("cannot verify pull request metadata for Issue State Gate evidence")
        head = pull["head"].get("sha")
        number = pull.get("number")
        if not isinstance(head, str) or type(number) is not int:
            raise ApiError("cannot verify pull request head for Issue State Gate evidence")
        sha = urllib.parse.quote(head, safe="")
        runs = get_pages(api, base + f"/commits/{sha}/check-runs", "check_runs")
        matching = [run for run in runs if run.get("name") == "Issue State Gate" and run.get("head_sha") == head]
        if len(matching) > 1:
            raise ValueError(f"ambiguous Issue State Gate checks on pull request #{number}")
        if not matching:
            continue
        check = matching[0]
        if check.get("status") != "completed" or check.get("conclusion") != "success":
            continue
        app_id = check.get("app", {}).get("id") if isinstance(check.get("app"), dict) else None
        if type(app_id) is not int:
            continue
        workflow_runs = get_pages(api, base + f"/actions/workflows/{workflow['id']}/runs?head_sha={sha}&event=pull_request_target", "workflow_runs")
        if any(run.get("workflow_id") == workflow["id"] and run.get("head_sha") == head
               and run.get("event") == "pull_request_target" and run.get("status") == "completed"
               and run.get("conclusion") == "success"
               for run in workflow_runs):
            integration_id = app_id
            break
    if integration_id is None:
        raise ValueError("Issue State Gate source is unsupported or no successful exact-source check is observable yet")
    if details is not None:
        details["source"] = {"provider": "GitHub Actions", "integration_id": integration_id,
                             "workflow_id": workflow["id"]}
    payload = {
        "name": "issue-state-gate", "target": "branch", "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/" + branch], "exclude": []}},
        "rules": [
            {"type": "pull_request", "parameters": {
                "required_approving_review_count": 0,
                "dismiss_stale_reviews_on_push": False,
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_review_thread_resolution": False,
                "required_reviewers": [],
                "require_extra_approval_for_unattributed_changes": False,
                "allowed_merge_methods": ["merge", "squash", "rebase"],
            }},
            {"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": False,
                "required_status_checks": [{"context": "Issue State Gate", "integration_id": integration_id}],
            }},
        ],
    }
    rulesets = get(api, base + "/rulesets?includes_parents=false&per_page=100")
    if not isinstance(rulesets, list) or len(rulesets) >= 100:
        raise ApiError("cannot verify all repository rulesets")
    ours = [item for item in rulesets if item.get("name") == payload["name"]]
    if len(ours) > 1:
        raise ApiError("multiple Issue State Gate rulesets exist; resolve duplicates manually")
    changes = []
    target = base + "/rulesets"
    method = "POST"
    if ours:
        target += "/" + str(ours[0].get("id"))
        current = get_object(api, target)
        if current.get("bypass_actors") not in (None, []):
            raise ApiError("Issue State Gate ruleset has bypass actors; refusing replacement")
        if current.get("target") not in (None, "branch"):
            raise ApiError("Issue State Gate ruleset has an unmanaged target; refusing replacement")
        if current.get("conditions") != payload["conditions"]:
            raise ApiError("Issue State Gate ruleset has unmanaged branch conditions; refusing replacement")
        current_rules = current.get("rules")
        managed_rule_types = {rule["type"] for rule in payload["rules"]}
        if not isinstance(current_rules, list) or any(
            not isinstance(rule, dict) or rule.get("type") not in managed_rule_types or
            set(rule) - {"type", "parameters"} for rule in current_rules
        ):
            raise ApiError("Issue State Gate ruleset contains unmanaged protections; refusing replacement")
        for rule in current_rules:
            rule_type = rule["type"]
            params = rule.get("parameters")
            allowed_parameters = set(next(
                rule["parameters"] for rule in payload["rules"] if rule["type"] == rule_type
            ))
            if rule_type == "required_status_checks":
                # GitHub includes this stable API field in ruleset readback even
                # when it was not explicitly sent by the creator.
                allowed_parameters.add("do_not_enforce_on_create")
            else:
                # GitHub returns these parameters for pull_request rules; set
                # them explicitly so normalization does not introduce approval
                # or merge-method requirements outside this stage's intent.
                allowed_parameters.update({
                    "required_reviewers", "require_extra_approval_for_unattributed_changes",
                    "allowed_merge_methods",
                })
            if not isinstance(params, dict) or set(params) - allowed_parameters:
                raise ApiError("Issue State Gate ruleset contains unmanaged parameters; refusing replacement")
            if rule_type == "required_status_checks":
                if params.get("do_not_enforce_on_create", False) is not False:
                    raise ApiError("Issue State Gate ruleset defers enforcement on new branches; refusing replacement")
                selected_checks = params.get("required_status_checks", [])
                if not isinstance(selected_checks, list) or any(
                    not isinstance(item, dict) or item.get("context") != "Issue State Gate"
                    for item in selected_checks
                ):
                    raise ApiError("Issue State Gate ruleset contains unmanaged required checks; refusing replacement")
        if all(current.get(key) == value for key, value in payload.items()):
            return changes
        method = "PUT"
    changes.append((method, target, payload))
    if apply:
        status, _ = api.request(method, target, payload)
        if status not in (200, 201):
            raise ApiError(f"{method} {target}: could not activate Issue State Gate")
        if method == "POST":
            rulesets = get(api, base + "/rulesets?includes_parents=false&per_page=100")
            matches = [item for item in rulesets if item.get("name") == payload["name"]]
            if len(matches) != 1 or not isinstance(matches[0].get("id"), int):
                raise ApiError("Issue State Gate ruleset could not be read back uniquely after creation")
            readback = get_object(api, base + f"/rulesets/{matches[0]['id']}")
        else:
            readback = get_object(api, target)
        if any(readback.get(key) != value for key, value in payload.items() if key != "rules"):
            raise ApiError("Issue State Gate ruleset readback did not match the requested active rule")
        observed_rules = readback.get("rules")
        desired_rules = {rule["type"]: rule["parameters"] for rule in payload["rules"]}
        if not isinstance(observed_rules, list) or len(observed_rules) != len(desired_rules):
            raise ApiError("Issue State Gate ruleset readback did not match the requested active rule")
        observed_by_type = {rule.get("type"): rule.get("parameters") for rule in observed_rules
                            if isinstance(rule, dict)}
        if set(observed_by_type) != set(desired_rules):
            raise ApiError("Issue State Gate ruleset readback did not match the requested active rule")
        for rule_type, expected_parameters in desired_rules.items():
            observed_parameters = observed_by_type[rule_type]
            if not isinstance(observed_parameters, dict) or any(
                observed_parameters.get(key) != value for key, value in expected_parameters.items()
            ):
                raise ApiError("Issue State Gate ruleset readback did not match the requested active rule")
            if rule_type == "required_status_checks" and observed_parameters.get(
                "do_not_enforce_on_create", False
            ) is not False:
                raise ApiError("Issue State Gate ruleset readback disables enforcement on new branches")
            if rule_type == "pull_request" and any(
                observed_parameters.get(key, value) != value for key, value in (
                    ("required_reviewers", []),
                    ("require_extra_approval_for_unattributed_changes", False),
                    ("allowed_merge_methods", ["merge", "squash", "rebase"]),
                )
            ):
                raise ApiError("Issue State Gate ruleset readback adds review or merge restrictions")
    return changes


def run_policy_dry_run(api, repository, policy):
    """Return a checked policy dry run with actionable permission diagnostics."""
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        from setup_github_repo import run_policy, validate_policy
        validate_policy(policy)
        result = run_policy(api, repository, policy, apply=False)
    except (ApiError, ValueError) as error:
        raise ApiError(
            "policy audit could not read all declared settings; POLICY_AUDIT_TOKEN must be "
            "authorized for this repository with Administration (read) and Contents (write) "
            f"permissions, and permitted by organization policy: {error}"
        ) from None
    changes = result.get("changes", [])
    blocked = [change for change in changes if change.get("status") == "blocked"]
    if blocked:
        raise ApiError(
            "policy audit returned blocked settings; check POLICY_AUDIT_TOKEN repository access "
            "(Administration: read, Contents: write) and organization policy: "
            + "; ".join(change.get("reason", "unknown API failure") for change in blocked)
        )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="OWNER/REPOSITORY")
    parser.add_argument("--apply", action="store_true", help="send planned writes; default is dry run")
    parser.add_argument("--policy", help="dry-run a checked-in setup policy JSON document")
    parser.add_argument("--token-env", default="GITHUB_TOKEN", help="environment variable holding an admin token")
    sub = parser.add_subparsers(dest="stage")
    features = sub.add_parser("features", help="enable selected repository features")
    features.add_argument("--enable", action="append", choices=FEATURES, required=True)
    sub.add_parser("actions", help="enable Actions with read-only default workflow tokens")
    security = sub.add_parser("security", help="enable selected security features")
    security.add_argument("--enable", action="append", choices=SECURITY_FEATURES, required=True)
    sub.add_parser("codeql", help="enable CodeQL default setup when available")
    gates = sub.add_parser("gates", help="activate a code PR ruleset after check and reviewer preflight")
    gates.add_argument("--name", required=True, help="managed ruleset name")
    gates.add_argument("--evidence-pr", type=int, required=True)
    gates.add_argument("--reviewer", help="independent reviewer username (required if approvals > 0)")
    gates.add_argument("--approvals", type=int, default=1, help="required approving review count (default: 1; 0 for solo maintainer)")
    gates.add_argument("--check", action="append", required=True,
                       help="WORKFLOW_PATH=CHECK_NAME; repeat as needed")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("--repo must be OWNER/REPOSITORY")
    if args.policy and args.apply:
        parser.error("--policy is a dry-run audit and cannot be combined with --apply")
    if args.policy and args.stage:
        parser.error("--policy cannot be combined with a settings subcommand")
    if not args.policy and not args.stage:
        parser.error("select a settings subcommand or provide --policy PATH")
    token = resolve_github_token(token_env=args.token_env)
    if not token:
        if args.policy:
            parser.error(
                f"set {args.token_env}; policy dry runs require a repository-scoped token "
                "with Administration (read) and Contents (write) permissions"
            )
        parser.error(f"set {args.token_env} before running")
    api = GitHubApi(token)
    try:
        if args.policy:
            policy_path = Path(args.policy)
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            result = run_policy_dry_run(api, args.repo, policy)
            print(json.dumps(result, indent=2))
            return 0
        if args.stage == "features":
            changes = reconcile_features(api, args.repo, features=args.enable, apply=args.apply)
        elif args.stage == "actions":
            changes = reconcile_actions(api, args.repo, args.apply)
        elif args.stage == "security":
            changes = reconcile_security(api, args.repo, args.enable, args.apply)
        elif args.stage == "codeql":
            changes = reconcile_codeql(api, args.repo, args.apply)
        else:
            checks = {}
            for entry in args.check:
                if "=" not in entry:
                    raise ValueError("--check must be WORKFLOW_PATH=CHECK_NAME")
                path, name = entry.split("=", 1)
                if name in checks:
                    raise ValueError("duplicate required check name: " + name)
                checks[name] = path
            if args.approvals > 0 and not args.reviewer:
                parser.error("--reviewer is required when approvals > 0")
            changes = reconcile_gates(
                api,
                args.repo,
                args.name,
                args.evidence_pr,
                args.reviewer,
                checks,
                args.apply,
                required_approving_review_count=args.approvals,
            )
            if args.apply and changes and reconcile_gates(
                api,
                args.repo,
                args.name,
                args.evidence_pr,
                args.reviewer,
                checks,
                required_approving_review_count=args.approvals,
            ):
                raise ApiError("ruleset apply did not converge on readback")
    except (ApiError, OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "changes": [
        {"method": method, "path": path, "body": body} for method, path, body in changes
    ]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
