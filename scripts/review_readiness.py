"""Read-only Review Run readiness checks; these never block safe workflow staging."""

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


def review_readiness(api, repository, review=None):
    """Return actionable warnings without checking in-repo review secrets or tokens."""
    base = "/repos/" + repository
    warnings = []
    status, actions = _read(api, base + "/actions/permissions")
    if status != 200 or not isinstance(actions, dict):
        warnings.append("Review Run readiness: cannot inspect Actions policy; owner should check repository Actions settings.")
    elif actions.get("enabled") is not True:
        warnings.append("Review Run readiness: Actions is disabled; owner should enable it before a review can run.")

    owner = repository.split("/", 1)[0]
    repo_status, repo_info = _read(api, base)
    if repo_status == 200 and isinstance(repo_info, dict) and repo_info.get("has_issues") is False:
        warnings.append("Review findings reporting: Issues are disabled for this repository; enable Issues before expecting managed finding reports.")
    if repo_status == 200 and isinstance(repo_info, dict) and isinstance(repo_info.get("owner"), dict) and repo_info["owner"].get("type") == "Organization":
        org_status, org_actions = _read(api, f"/orgs/{owner}/actions/permissions")
        if org_status != 200 or not isinstance(org_actions, dict):
            warnings.append("Review Run readiness: inherited organization Actions policy is unproven; organization owner should inspect it.")
    elif repo_status != 200 or not isinstance(repo_info, dict) or not isinstance(repo_info.get("owner"), dict):
        warnings.append("Review Run readiness: repository ownership is unknown; owner should inspect inherited Actions policy.")

    if review is not None and hasattr(review, "limits") and getattr(review.limits, "max_retention_days", None):
        retention_status, retention = _read(api, base + "/actions/permissions/artifact-and-log-retention")
        if retention_status != 200 or not isinstance(retention, dict) or type(retention.get("days")) is not int:
            warnings.append("Review Run readiness: Actions log retention could not be checked; owner should inspect it.")
        elif retention["days"] > review.limits.max_retention_days:
            warnings.append(f"Review Run readiness: Actions retention is {retention['days']} days, above the selected {review.limits.max_retention_days}-day limit; owner should lower it.")

    return warnings
