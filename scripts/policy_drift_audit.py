"""Open a triage issue when a policy dry run detects repository drift."""

import json
import argparse
import os
from pathlib import Path
import sys

try:
    from scripts.configure_github_repo import ApiError, GitHubApi, get_pages
except ImportError:
    from configure_github_repo import ApiError, GitHubApi, get_pages


def report_drift(api, repository, changes):
    """Create one issue for nonempty exact dry-run changes, suppressing open duplicates."""
    if not changes:
        return {"status": "clean"}

    title = "Policy Drift Detected:"
    open_issues = get_pages(api, f"/repos/{repository}/issues?state=open", None)
    if any(issue.get("state") == "open" and isinstance(issue.get("title"), str)
           and issue["title"].startswith(title)
           and "pull_request" not in issue
           for issue in open_issues if isinstance(issue, dict)):
        return {"status": "duplicate", "title": title}

    payload = {
        "title": title,
        "body": json.dumps(changes, indent=2) + "\n",
        "labels": ["needs-triage"],
    }
    status, issue = api.request("POST", f"/repos/{repository}/issues", payload)
    if status not in (200, 201) or not isinstance(issue, dict):
        raise ApiError("could not verify policy drift issue creation")
    return {"status": "created", "number": issue.get("number"), "title": title}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="OWNER/REPOSITORY")
    parser.add_argument("--report", required=True, help="JSON output from configure_github_repo.py --policy")
    parser.add_argument("--token-env", default="GITHUB_TOKEN", help="environment variable holding an issue-write token")
    args = parser.parse_args(argv)
    token = os.environ.get(args.token_env)
    if not token:
        parser.error(f"set {args.token_env} before running")
    try:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        if not isinstance(report, dict) or report.get("mode") != "dry-run" or not isinstance(report.get("changes"), list):
            raise ValueError("report must be a configure_github_repo.py dry-run result")
        result = report_drift(GitHubApi(token), args.repo, report["changes"])
    except (ApiError, OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
