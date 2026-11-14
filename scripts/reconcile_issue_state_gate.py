#!/usr/bin/env python3
"""Reconcile Issue State Gate check runs with current pull request and issue data."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import os
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


_RECOVERY_MARKER = "<!-- issue-state-gate-recovery:pr-{} -->"


@dataclass
class ReconciliationRow:
    pull_number: int
    status: str
    expected_decision: str
    head_sha: str
    detail: str = ""


@dataclass
class ReconciliationResult:
    rows: list[ReconciliationRow] = field(default_factory=list)
    recovery_notices: int = 0
    errors: list[str] = field(default_factory=list)
    exit_code: int = 0


def _is_target_pull_request(pull: dict[str, Any], repository: str) -> bool:
    base = pull.get("base")
    return (
        isinstance(base, dict)
        and base.get("ref") == "main"
        and isinstance(base.get("repo"), dict)
        and str(base["repo"].get("full_name", "")).casefold() == repository.casefold()
    )


def _check_source(run: dict[str, Any]) -> str:
    app = run.get("app")
    return str(app.get("slug", "")) if isinstance(app, dict) else ""


def _check_state(
    runs_by_sha: dict[str, list[dict[str, Any]]],
    head_sha: str,
    expected_decision: str,
) -> tuple[str, dict[str, Any] | None]:
    current = [run for run in runs_by_sha.get(head_sha, []) if run.get("name") == "Issue State Gate"]
    owned = [run for run in current if _check_source(run) == "github-actions"]
    if owned:
        latest = max(owned, key=lambda run: int(run.get("id", 0)))
        actual = "success" if latest.get("status") == "completed" and latest.get("conclusion") == "success" else "failure"
        if latest.get("status") != "completed" or actual != expected_decision:
            return "stale", latest
        return "current", latest
    if current:
        return "wrong-source", max(current, key=lambda run: int(run.get("id", 0)))
    if any(
        run.get("name") == "Issue State Gate"
        for sha, runs in runs_by_sha.items()
        if sha != head_sha
        for run in runs
    ):
        return "wrong-head", None
    return "absent", None


def _recovery_notice(repository: str, pull: dict[str, Any], issue_number: int) -> str:
    number = int(pull["number"])
    url = str(pull.get("html_url") or f"https://github.com/{repository}/pull/{number}")
    return "\n".join([
        f"Implementation issue #{issue_number} is closed, but its cited pull request [#{number}]({url}) was closed without merging.",
        "Please choose a recovery path: reopen the pull request and continue the delivery, reassign the issue to a replacement pull request, or explicitly record and approve a local-only delivery if the issue permits it.",
        "This notice does not change the issue state. Update the issue's Delivery evidence after choosing a path.",
        _RECOVERY_MARKER.format(number),
    ])


def _notify_abandoned_pr(client: Any, repository: str, pull: dict[str, Any], result: ReconciliationResult) -> None:
    if (pull.get("state") != "closed" or pull.get("merged") is True
            or pull.get("merged_at") is not None or not _is_target_pull_request(pull, repository)):
        return
    try:
        numbers = parse_implementation_issues(str(pull.get("body") or ""), repository)
    except ValueError:
        return
    for number in numbers:
        try:
            issue = client.fetch_issue(repository, number)
            if issue.get("state") != "closed":
                continue
            marker = _RECOVERY_MARKER.format(int(pull["number"]))
            comments = client.list_issue_comments(repository, number)
            if any(marker in str(comment.get("body", "")) for comment in comments):
                continue
            client.create_issue_comment(repository, number, _recovery_notice(repository, pull, number))
            result.recovery_notices += 1
        except (GitHubApiError, ValueError, KeyError) as exc:
            result.errors.append(f"Could not post recovery notice for issue #{number} and PR #{pull.get('number')}: {exc}")
            result.exit_code = 2


def reconcile_issue_state_gate(client: Any, repository: str, *, apply: bool = False) -> ReconciliationResult:
    """Report gate drift, optionally refresh checks and notify abandoned deliveries."""
    result = ReconciliationResult()
    try:
        pulls = client.list_pull_requests(repository)
    except (GitHubApiError, ValueError) as exc:
        result.errors.append(f"Could not list pull requests: {exc}")
        result.exit_code = 2
        return result

    for listed in pulls:
        _notify_abandoned_pr(client, repository, listed, result) if apply else None
        if listed.get("state") != "open" or not _is_target_pull_request(listed, repository):
            continue
        number = listed.get("number")
        try:
            live = client.fetch_pull_request(repository, number)
        except Exception as exc:
            result.errors.append(f"Could not revalidate PR #{number}: {exc}")
            result.exit_code = 2
            continue
        if live.get("state") != "open" or not _is_target_pull_request(live, repository):
            continue
        head = live.get("head")
        head_sha = str(head.get("sha", "")) if isinstance(head, dict) else ""
        if len(head_sha) != 40:
            result.errors.append(f"PR #{number} has no valid current head SHA.")
            result.exit_code = 2
            continue
        try:
            head_repo = head.get("repo") if isinstance(head, dict) else None
            if not isinstance(head_repo, dict) or str(head_repo.get("full_name", "")).casefold() != repository.casefold():
                expected = IssueGateResult(
                    errors=["Pull requests from forks are unsupported by the Issue State Gate."]
                )
            else:
                expected = evaluate_implementation_issues(
                    str(live.get("body") or ""), repository,
                    lambda issue_number: client.fetch_issue(repository, issue_number),
                )
            expected_decision = "success" if expected.passed else "failure"
            commits = client.list_pull_request_commits(repository, number)
            shas = {head_sha}
            shas.update(
                str((commit.get("sha") or (commit.get("commit") or {}).get("sha") or ""))
                for commit in commits
                if isinstance(commit, dict)
            )
            runs_by_sha = {sha: client.list_check_runs(repository, sha) for sha in shas if len(sha) == 40}
        except Exception as exc:
            result.errors.append(f"Could not reconcile PR #{number}: {exc}")
            result.exit_code = 2
            continue

        status, existing = _check_state(runs_by_sha, head_sha, expected_decision)
        row = ReconciliationRow(number, status, expected_decision, head_sha, format_summary(expected))
        result.rows.append(row)
        if apply and status != "current":
            try:
                if (
                    existing is not None
                    and existing.get("id") is not None
                    and existing.get("head_sha") == head_sha
                    and _check_source(existing) == "github-actions"
                ):
                    client.update_check(repository, int(existing["id"]), head_sha, expected)
                else:
                    client.publish_check(repository, head_sha, expected)
                row.status = "refreshed"
            except Exception as exc:
                result.errors.append(f"Could not refresh PR #{number}: {exc}")
                result.exit_code = 2
        elif expected.errors and result.exit_code == 0:
            # A correctly reported failure is still a successful reconciliation.
            result.exit_code = 0

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/repository")
    parser.add_argument("--apply", action="store_true", help="refresh checks and post abandoned-delivery recovery notices")
    args = parser.parse_args(argv)
    if not args.repo:
        print("--repo or GITHUB_REPOSITORY is required.", file=sys.stderr)
        return 2
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("GITHUB_TOKEN is required.", file=sys.stderr)
        return 2
    client = GitHubClient(token, os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    result = reconcile_issue_state_gate(client, args.repo, apply=args.apply)
    for row in result.rows:
        print(f"PR #{row.pull_number}: {row.status}; expected {row.expected_decision} at {row.head_sha}")
        print(row.detail)
    for error in result.errors:
        print(error, file=sys.stderr)
    print(f"Recovery notices posted: {result.recovery_notices}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
