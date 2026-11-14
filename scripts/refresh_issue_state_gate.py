#!/usr/bin/env python3
"""Refresh Issue State Gate checks after an implementation issue changes state."""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

try:
    from scripts.issue_state_gate import (
        GitHubApiError,
        GitHubClient,
        IssueGateResult,
        evaluate_implementation_issues,
        format_summary,
        parse_implementation_issues,
    )
except ImportError:  # pragma: no cover - direct script execution
    from issue_state_gate import (
        GitHubApiError,
        GitHubClient,
        IssueGateResult,
        evaluate_implementation_issues,
        format_summary,
        parse_implementation_issues,
    )


_SHA = re.compile(r"[0-9a-fA-F]{40}")


def _same_repository_pull_request(pull_request: dict[str, Any], repository: str) -> bool:
    base = pull_request.get("base")
    return (
        isinstance(base, dict)
        and base.get("ref") == "main"
        and isinstance(base.get("repo"), dict)
        and str(base["repo"].get("full_name", "")).casefold() == repository.casefold()
    )


def refresh_checks_for_issue(
    client: GitHubClient,
    repository: str,
    issue_number: int,
) -> int:
    """Re-evaluate matching open PRs against their live descriptions and heads.

    The issue event only identifies the issue. Its current state is fetched by
    the shared evaluator while each matching PR is refreshed, so delayed and
    replayed close/reopen events converge on GitHub's current issue state.
    """
    if not isinstance(issue_number, int) or issue_number < 1:
        print("The issue event has no valid issue number.", file=sys.stderr)
        return 2

    try:
        listed_pull_requests = client.list_open_pull_requests(repository)
    except (GitHubApiError, ValueError) as exc:
        print(f"Could not list open pull requests for Issue State Gate refresh: {exc}", file=sys.stderr)
        return 2

    overall_status = 0
    for listed_pull_request in listed_pull_requests:
        if not _same_repository_pull_request(listed_pull_request, repository):
            continue
        listed_body = str(listed_pull_request.get("body") or "")
        try:
            declared_issues = parse_implementation_issues(listed_body, repository)
        except ValueError:
            # The normal pull_request workflow reports malformed declarations.
            # They cannot establish that this issue belongs to this pull request.
            continue
        if issue_number not in declared_issues:
            continue

        pull_number = listed_pull_request.get("number")
        listed_head = listed_pull_request.get("head")
        listed_sha = str(listed_head.get("sha", "")) if isinstance(listed_head, dict) else ""
        if not isinstance(pull_number, int) or pull_number < 1:
            print("An open pull request record has no valid number.", file=sys.stderr)
            overall_status = 2
            continue

        try:
            live_pull_request = client.fetch_pull_request(repository, pull_number)
        except (GitHubApiError, ValueError) as exc:
            error = f"Could not revalidate pull request #{pull_number}: {exc}"
            failure = IssueGateResult(errors=[error])
            if _SHA.fullmatch(listed_sha):
                try:
                    client.publish_check(repository, listed_sha, failure)
                except (GitHubApiError, ValueError) as publish_error:
                    print(
                        f"{error}; could not publish a failing Issue State Gate check: {publish_error}",
                        file=sys.stderr,
                    )
                    overall_status = 2
                    continue
            print(error, file=sys.stderr)
            overall_status = max(overall_status, 1)
            continue

        if (
            live_pull_request.get("state") != "open"
            or not _same_repository_pull_request(live_pull_request, repository)
        ):
            # A PR closed or retargeted after the collection response no longer
            # has an active merge gate to refresh.
            continue

        live_head = live_pull_request.get("head")
        live_sha = str(live_head.get("sha", "")) if isinstance(live_head, dict) else ""
        if not _SHA.fullmatch(live_sha):
            print(f"Pull request #{pull_number} has no valid live head SHA.", file=sys.stderr)
            overall_status = 2
            continue

        live_body = str(live_pull_request.get("body") or "")
        try:
            live_issues = parse_implementation_issues(live_body, repository)
        except ValueError as exc:
            result = IssueGateResult(errors=[str(exc)])
        else:
            if issue_number not in live_issues:
                # The issue link was removed while the collection request was
                # in flight, so this event no longer affects the PR's gate.
                continue
            result = evaluate_implementation_issues(
                live_body,
                repository,
                lambda number: client.fetch_issue(repository, number),
            )

        try:
            client.publish_check(repository, live_sha, result)
        except (GitHubApiError, ValueError) as exc:
            print(f"Could not publish the Issue State Gate check for PR #{pull_number}: {exc}", file=sys.stderr)
            overall_status = 2
            continue

        print(f"Refreshed Issue State Gate for PR #{pull_number} at {live_sha}:")
        print(format_summary(result))
        if not result.passed:
            overall_status = max(overall_status, 1)

    return overall_status


def main(event_path: str | None = None) -> int:
    """Handle a close or reopen event using live issue and pull request data."""
    path = event_path or os.environ.get("GITHUB_EVENT_PATH", "")
    if not path:
        print("GITHUB_EVENT_PATH is required.", file=sys.stderr)
        return 2
    try:
        with open(path, encoding="utf-8") as event_file:
            event = json.load(event_file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read GitHub event payload: {exc}", file=sys.stderr)
        return 2

    issue = event.get("issue")
    if not isinstance(issue, dict) or "pull_request" in issue:
        print("This workflow requires an issue event.", file=sys.stderr)
        return 2
    issue_number = issue.get("number")
    if not isinstance(issue_number, int) or issue_number < 1:
        print("The issue event has no valid issue number.", file=sys.stderr)
        return 2

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not repository:
        print("GITHUB_REPOSITORY is required.", file=sys.stderr)
        return 2
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("GITHUB_TOKEN is required to refresh Issue State Gate checks.", file=sys.stderr)
        return 2

    client = GitHubClient(token, os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    return refresh_checks_for_issue(client, repository, issue_number)


if __name__ == "__main__":
    raise SystemExit(main())
