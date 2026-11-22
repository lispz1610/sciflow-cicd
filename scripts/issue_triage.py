#!/usr/bin/env python3
"""Apply the repository's issue triage policy to a GitHub Actions event."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    from scripts.path_security import validated_repo_api_path
except ImportError:  # pragma: no cover - direct script execution
    from path_security import validated_repo_api_path


TRIAGE_LABELS = {
    "needs-triage",
    "needs-info",
    "ready-for-agent",
    "ready-for-human",
    "wontfix",
}


def plan_event(event_name: str, payload: dict, project_id: str | None = None) -> dict:
    """Return label and project changes for a supported issue event."""
    issue = payload.get("issue")
    plan = {
        "labels_to_add": [],
        "labels_to_remove": [],
        "project_item_node_id": None,
    }
    if not isinstance(issue, dict) or "pull_request" in issue:
        return plan

    labels = {
        label.get("name")
        for label in issue.get("labels", [])
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    }
    action = payload.get("action")

    if event_name == "issues" and action == "opened":
        if not labels.intersection(TRIAGE_LABELS):
            plan["labels_to_add"].append("needs-triage")
        if project_id:
            plan["project_item_node_id"] = issue.get("node_id")
    elif event_name == "issue_comment" and action == "created":
        comment = payload.get("comment")
        issue_author = (issue.get("user") or {}).get("login")
        comment_author = ((comment or {}).get("user") or {}).get("login")
        if issue_author and comment_author == issue_author and "needs-info" in labels:
            plan["labels_to_remove"].append("needs-info")
            plan["labels_to_add"].append("needs-triage")
    return plan


class GitHubApiError(RuntimeError):
    """Raised when GitHub rejects a triage API request."""


def _request(url: str, token: str, payload: dict | None = None) -> dict | list:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=data,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
        method="POST" if data is not None else "DELETE",
    )
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read()
    except (HTTPError, URLError) as error:
        raise GitHubApiError(f"GitHub API request failed: {error}") from error
    result = json.loads(body) if body else {}
    if isinstance(result, dict) and result.get("errors"):
        raise GitHubApiError(f"GitHub GraphQL request failed: {result['errors']}")
    return result


def apply_plan(
    repository: str,
    issue_number: int,
    plan: dict,
    token: str,
    api_url: str,
    *,
    project_token: str | None = None,
    project_id: str | None = None,
) -> None:
    """Apply a plan while preserving all labels not explicitly transitioned."""
    try:
        repo_path = validated_repo_api_path(repository)
    except ValueError as exc:
        raise GitHubApiError(str(exc)) from exc
    issue_url = f"{api_url}{repo_path}/issues/{issue_number}"
    if plan["labels_to_add"]:
        _request(f"{issue_url}/labels", token, {"labels": plan["labels_to_add"]})
    for label in plan["labels_to_remove"]:
        _request(f"{issue_url}/labels/{quote(label, safe='')}", token)

    project_item_node_id = plan["project_item_node_id"]
    if project_item_node_id:
        if not project_token:
            raise GitHubApiError("TRIAGE_PROJECT_TOKEN is required to add issues to Projects V2")
        if not project_id:
            raise GitHubApiError("TRIAGE_PROJECT_ID is required to add issues to Projects V2")
        result = _request(
            f"{api_url}/graphql",
            project_token,
            {
                "query": "mutation($project:ID!,$content:ID!){addProjectV2ItemById(input:{projectId:$project,contentId:$content}){item{id}}}",
                "variables": {"project": project_id, "content": project_item_node_id},
            },
        )
        data = result.get("data") if isinstance(result, dict) else None
        item = data.get("addProjectV2ItemById") if isinstance(data, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get("item"), dict) or not item["item"].get("id"):
            raise GitHubApiError("GitHub GraphQL response did not contain a project item")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("all", "labels", "project"), default="all")
    args = parser.parse_args()
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        print("GITHUB_EVENT_PATH is required", file=sys.stderr)
        return 2
    try:
        with open(event_path, encoding="utf-8") as event_file:
            payload = json.load(event_file)
        issue = payload.get("issue", {})
        configured_project_id = os.environ.get("TRIAGE_PROJECT_ID") or None
        plan = plan_event(
            os.environ.get("GITHUB_EVENT_NAME", ""),
            payload,
            configured_project_id if args.mode != "labels" else None,
        )
        if args.mode == "project":
            plan["labels_to_add"] = []
            plan["labels_to_remove"] = []
        elif args.mode == "labels":
            plan["project_item_node_id"] = None
        print(json.dumps(plan, sort_keys=True))
        if not plan["labels_to_add"] and not plan["labels_to_remove"] and not plan["project_item_node_id"]:
            return 0
        token = os.environ.get("GITHUB_TOKEN", "")
        if (plan["labels_to_add"] or plan["labels_to_remove"]) and not token:
            raise GitHubApiError("GITHUB_TOKEN is required to apply issue labels")
        apply_plan(
            os.environ["GITHUB_REPOSITORY"],
            issue["number"],
            plan,
            token,
            os.environ.get("GITHUB_API_URL", "https://api.github.com"),
            project_token=os.environ.get("TRIAGE_PROJECT_TOKEN"),
            project_id=configured_project_id,
        )
    except (OSError, ValueError, KeyError, GitHubApiError) as error:
        print(f"Issue triage failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
