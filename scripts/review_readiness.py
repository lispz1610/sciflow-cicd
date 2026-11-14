"""Read-only Review Run readiness checks; these never block safe workflow staging."""

import base64
import fnmatch
import json
from urllib.parse import quote

try:
    from scripts.review_workflow import UPSTREAM_ACTION_DEPENDENCIES
except ImportError:
    from review_workflow import UPSTREAM_ACTION_DEPENDENCIES

_ApiErrors = ()
try:
    import configure_github_repo
    _ApiErrors = _ApiErrors + (configure_github_repo.ApiError,)
except (ImportError, AttributeError):
    pass
try:
    import scripts.configure_github_repo
    _ApiErrors = _ApiErrors + (scripts.configure_github_repo.ApiError,)
except (ImportError, AttributeError):
    pass


def _read(api, path):
    try:
        return api.request("GET", path)
    except (_ApiErrors + (OSError, ValueError)):
        return None, None


def _allowed(selection, action):
    if not isinstance(selection, dict):
        return False
    patterns = selection.get("patterns_allowed")
    if not isinstance(patterns, list) or any(not isinstance(p, str) for p in patterns):
        return False
    if any(fnmatch.fnmatchcase(action, p) for p in patterns):
        return True
    owner = action.split("/", 1)[0]
    return owner == "actions" and selection.get("github_owned_allowed") is True


def review_readiness(api, repository, review):
    """Return actionable warnings without fetching a secret value or writing anything."""
    base = "/repos/" + repository
    warnings = [
        "Advisory findings reporting: Issues must be enabled and the reporter job's GITHUB_TOKEN needs issues: write and pull-requests: read; readiness cannot verify workflow-token permissions."
    ]
    status, actions = _read(api, base + "/actions/permissions")
    if status != 200 or not isinstance(actions, dict):
        warnings.append("Review Run readiness: cannot inspect Actions policy; owner should check repository Actions settings.")
    elif actions.get("enabled") is not True:
        warnings.append("Review Run readiness: Actions is disabled; owner should enable it before a review can run.")
    else:
        allowed = actions.get("allowed_actions")
        if allowed == "local_only":
            warnings.append("Review Run readiness: Actions policy blocks the external review Action; owner should allow pinned Actions.")
        elif allowed == "selected":
            sel_status, selected = _read(api, base + "/actions/permissions/selected-actions")
            required = (f"alibaba/open-code-review@{review.workflow.action_sha}", *UPSTREAM_ACTION_DEPENDENCIES)
            if sel_status != 200 or any(not _allowed(selected, action) for action in required):
                warnings.append("Review Run readiness: selected Actions policy does not prove that the review Action and nested dependencies are allowed; owner should update the allowlist.")
        elif allowed != "all":
            warnings.append("Review Run readiness: effective Actions policy is unknown; owner should inspect it.")

    owner = repository.split("/", 1)[0]
    repo_status, repo_info = _read(api, base)
    if repo_status == 200 and isinstance(repo_info, dict) and repo_info.get("has_issues") is False:
        warnings.append("Review findings reporting: Issues are disabled for this repository; enable Issues before expecting managed finding reports.")
    if repo_status == 200 and isinstance(repo_info, dict) and isinstance(repo_info.get("owner"), dict) and repo_info["owner"].get("type") == "Organization":
        org_status, org_actions = _read(api, f"/orgs/{owner}/actions/permissions")
        if org_status != 200 or not isinstance(org_actions, dict):
            warnings.append("Review Run readiness: inherited organization Actions policy is unproven; organization owner should inspect it.")
        elif org_actions.get("enabled_repositories") != "all" or org_actions.get("allowed_actions") != "all":
            warnings.append("Review Run readiness: organization Actions restrictions may block this repository or review Action; organization owner should verify the effective policy.")
    elif repo_status != 200 or not isinstance(repo_info, dict) or not isinstance(repo_info.get("owner"), dict):
        warnings.append("Review Run readiness: repository ownership is unknown; owner should inspect inherited Actions policy.")

    retention_status, retention = _read(api, base + "/actions/permissions/artifact-and-log-retention")
    if retention_status != 200 or not isinstance(retention, dict) or type(retention.get("days")) is not int:
        warnings.append("Review Run readiness: Actions log retention could not be checked; owner should inspect it.")
    elif retention["days"] > review.limits.max_retention_days:
        warnings.append(f"Review Run readiness: Actions retention is {retention['days']} days, above the selected {review.limits.max_retention_days}-day limit; owner should lower it.")

    secret_status, secret = _read(api, base + "/actions/secrets/OCR_LLM_AUTH_TOKEN")
    if secret_status != 200 or not isinstance(secret, dict) or secret.get("name") != "OCR_LLM_AUTH_TOKEN":
        warnings.append("Review pending configuration: add Actions secret OCR_LLM_AUTH_TOKEN in Settings > Secrets and variables > Actions.")
    expected = {"OCR_LLM_URL": review.llm.url, "OCR_LLM_MODEL": review.llm.model,
                "OCR_LLM_USE_ANTHROPIC": str(review.llm.use_anthropic).lower()}
    for name, value in expected.items():
        variable_status, variable = _read(api, base + "/actions/variables/" + name)
        if variable_status != 200 or not isinstance(variable, dict) or variable.get("name") != name:
            warnings.append(f"Review pending configuration: add Actions variable {name} in Settings > Secrets and variables > Actions.")
        elif variable.get("value") != value:
            warnings.append(f"Review pending configuration: Actions variable {name} differs from the selected Policy; owner should correct it.")
    return warnings


def validate_trusted_rule(api, repository, review):
    """Require a selected trusted rule to exist as a small JSON object at the current default head."""
    path = review.workflow.rule_path
    if path is None:
        return
    try:
        from scripts.install_github_workflows import _ref
    except ImportError:
        from install_github_workflows import _ref
    base = "/repos/" + repository
    status, repo = _read(api, base)
    if status != 200 or not isinstance(repo, dict) or not isinstance(repo.get("default_branch"), str):
        raise ValueError("cannot verify the trusted rule without a default branch")
    head = _ref(api, base, repo["default_branch"])
    if head is None:
        raise ValueError("cannot verify the trusted rule at the default branch head")
    status, file = _read(api, base + "/contents/" + quote(path, safe="/") + "?ref=" + head)
    if status != 200 or not isinstance(file, dict) or file.get("type") != "file" or file.get("encoding") != "base64":
        raise ValueError(f"trusted rule {path} must exist as a JSON file on the default branch")
    try:
        encoded = file["content"]
        if not isinstance(encoded, str) or len(encoded) > 1_400_000:
            raise ValueError
        data = base64.b64decode("".join(encoded.split()), validate=True)
        if len(data) > 1_000_000 or not isinstance(json.loads(data.decode("utf-8")), dict):
            raise ValueError
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise ValueError(f"trusted rule {path} must be a UTF-8 JSON object of at most 1 MB") from None
