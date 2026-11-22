#!/usr/bin/env python3
"""Evaluate the state of implementation issues declared by a pull request."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import re
import sys
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

try:
    from scripts.path_security import validated_repo_api_path
except ImportError:  # pragma: no cover - direct script execution
    from path_security import validated_repo_api_path


_SECTION_HEADER = re.compile(r"(?im)^#{1,6}\s+Implementation issues\s*#*\s*$")
_NEXT_HEADER = re.compile(r"(?m)^#{1,6}\s+")
_ISSUE_REFERENCE = re.compile(
    r"(?<![\w/])(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)|#(\d+))\b"
)
_ISSUE_URL = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+)\b",
    re.IGNORECASE,
)
_MALFORMED_REFERENCE = re.compile(r"(?<![\w/])#(?:-\d+|0+|[A-Za-z][A-Za-z0-9_-]*|\d+[A-Za-z_-][A-Za-z0-9_-]*)\b")
_MALFORMED_ISSUE_URL = re.compile(
    r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/([^\s?#/]+)",
    re.IGNORECASE,
)
_HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")
_DELIVERY_HEADER = re.compile(r"(?im)^##\s+Delivery\s*#*\s*$")
_LOCAL_ONLY_EXCEPTION = re.compile(r"(?im)^\s*(?:[-*]\s*)?local[- ]only exception\b")


@dataclass(frozen=True)
class IssueState:
    """A resolved implementation issue and the state observed by GitHub."""

    number: int
    state: str
    title: str
    url: str


@dataclass
class IssueGateResult:
    """The result of evaluating all declared implementation issues."""

    issues: list[IssueState] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors and bool(self.issues) and all(
            issue.state == "closed" for issue in self.issues
        )


def parse_implementation_issues(body: str, repository: str) -> list[int]:
    """Return sorted, distinct same-repository issue numbers from the named section.

    The pull request body must contain exactly one Markdown section titled
    ``Implementation issues``. Bare ``#123`` references are relative to the PR
    repository; qualified references and issue URLs must name that same repo.
    """
    matches = list(_SECTION_HEADER.finditer(body or ""))
    if not matches:
        raise ValueError(
            "Add an `Implementation issues` section with one or more issue references."
        )
    if len(matches) != 1:
        raise ValueError("Use exactly one `Implementation issues` section.")

    target_repository = repository.strip().lower()
    start = matches[0].end()
    next_heading = _NEXT_HEADER.search(body, start)
    section = body[start : next_heading.start() if next_heading else len(body)]
    issue_numbers: set[int] = set()
    malformed = _MALFORMED_REFERENCE.search(section)
    if malformed:
        raise ValueError(
            f"Malformed implementation issue reference {malformed.group(0)}; use a positive issue number."
        )
    for match in _MALFORMED_ISSUE_URL.finditer(section):
        if not match.group(1).isdigit() or int(match.group(1)) < 1:
            raise ValueError(
                f"Malformed implementation issue URL ending in {match.group(1)!r}; use a positive issue number."
            )

    for line in section.splitlines():
        # URLs are consumed first so their numeric portion is not treated as a
        # bare issue reference as well.
        remaining = _ISSUE_URL.sub(" ", line)
        for match in _ISSUE_URL.finditer(line):
            if match.group(1).lower() != target_repository:
                raise ValueError(
                    f"Issue URL {match.group(0)} is outside {repository}."
                )
            number = int(match.group(2))
            if number > 0:
                if number in issue_numbers:
                    raise ValueError(f"Duplicate implementation issue reference #{number} makes the scope ambiguous.")
                issue_numbers.add(number)

        for match in _ISSUE_REFERENCE.finditer(remaining):
            qualified_repository = match.group(1)
            number_text = match.group(2) or match.group(3)
            if (
                qualified_repository
                and qualified_repository.lower() != target_repository
            ):
                raise ValueError(
                    f"Issue reference {match.group(0)} is outside {repository}."
                )
            number = int(number_text)
            if number > 0:
                if number in issue_numbers:
                    raise ValueError(f"Duplicate implementation issue reference #{number} makes the scope ambiguous.")
                issue_numbers.add(number)

    if not issue_numbers:
        raise ValueError(
            "The `Implementation issues` section must include at least one valid issue number."
        )
    return sorted(issue_numbers)


def _repository_identity(repository_url: str) -> str | None:
    """Extract owner/repository from a GitHub API repository URL."""
    path = [part for part in urlparse(repository_url).path.split("/") if part]
    if len(path) < 3 or path[-3] != "repos":
        return None
    return f"{path[-2]}/{path[-1]}".lower()


def _has_local_only_delivery(issue_body: Any) -> bool:
    if not isinstance(issue_body, str):
        return False
    sections = list(_DELIVERY_HEADER.finditer(issue_body))
    for section in sections:
        start = section.end()
        next_heading = _NEXT_HEADER.search(issue_body, start)
        content = issue_body[start : next_heading.start() if next_heading else len(issue_body)]
        if _LOCAL_ONLY_EXCEPTION.search(content):
            return True
    return False


def evaluate_implementation_issues(
    body: str,
    repository: str,
    fetch_issue: Callable[[int], dict[str, Any]],
) -> IssueGateResult:
    """Resolve declared issues and pass only when every valid issue is closed."""
    result = IssueGateResult()
    try:
        issue_numbers = parse_implementation_issues(body, repository)
    except ValueError as exc:
        result.errors.append(str(exc))
        return result

    expected_repository = repository.strip().lower()
    for number in issue_numbers:
        try:
            issue = fetch_issue(number)
        except Exception as exc:  # API errors must fail closed and remain actionable.
            status_match = _HTTP_STATUS.search(str(exc))
            status = int(status_match.group(1)) if status_match else None
            if status == 403:
                result.errors.append(
                    f"Issue #{number} is inaccessible to the workflow token; grant the required read access or remove the reference."
                )
            elif status == 410:
                result.errors.append(
                    f"Issue #{number} was deleted and cannot be verified."
                )
            elif status == 404:
                result.errors.append(
                    f"Issue #{number} is deleted or inaccessible and cannot be verified."
                )
            else:
                result.errors.append(f"Could not resolve issue #{number}: {exc}")
            continue

        if issue.get("number") != number:
            result.errors.append(
                f"GitHub returned issue #{issue.get('number')} when resolving #{number}."
            )
            continue

        issue_repository = _repository_identity(str(issue.get("repository_url", "")))
        if issue_repository != expected_repository:
            result.errors.append(
                f"Issue #{number} could not be verified in repository {repository}."
            )
            continue

        if "pull_request" in issue:
            result.issues.append(
                IssueState(
                    number=number,
                    state="pull request",
                    title=str(issue.get("title", "")),
                    url=str(issue.get("html_url", "")),
                )
            )
            result.errors.append(
                f"#{number} is a pull request; declare a closed implementation issue instead."
            )
            continue

        state = str(issue.get("state", "unknown")).lower()
        result.issues.append(
            IssueState(
                number=number,
                state=state,
                title=str(issue.get("title", "")),
                url=str(issue.get("html_url", "")),
            )
        )
        if state not in {"open", "closed"}:
            result.errors.append(f"Issue #{number} has an unrecognized state: {state}.")
        elif state == "closed" and not isinstance(issue.get("body"), str):
            result.errors.append(
                f"Issue #{number} has an incomplete GitHub response: body is missing or invalid."
            )
        elif state == "closed" and _has_local_only_delivery(issue.get("body")):
            result.errors.append(
                f"Issue #{number} is marked as local-only delivery; local-only exceptions do not satisfy the Issue State Gate."
            )

    return result


def format_summary(result: IssueGateResult) -> str:
    """Format a stable Markdown summary for the GitHub check run."""
    lines = ["Declared implementation issues and their current GitHub states:", ""]
    if result.issues:
        lines.extend(["| Issue | State | Title |", "| --- | --- | --- |"])
        for issue in result.issues:
            label = f"[#{issue.number}]({issue.url})" if issue.url else f"#{issue.number}"
            title = issue.title.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {label} | {issue.state} | {title} |")
    else:
        lines.append("No issue state could be verified.")

    blockers = [f"#{issue.number}" for issue in result.issues if issue.state == "open"]
    if blockers:
        lines.extend(["", f"Open implementation issues block merge: {', '.join(blockers)}."])

    if result.errors:
        lines.extend(["", "**Action required**"])
        lines.extend(f"- {error}" for error in result.errors)
    elif result.passed:
        lines.extend(["", "All declared implementation issues are closed."])
    return "\n".join(lines)


def _check_run_payload(head_sha: str, result: IssueGateResult) -> dict[str, Any]:
    passed = result.passed
    return {
        "name": "Issue State Gate",
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": "success" if passed else "failure",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "output": {
            "title": "All implementation issues are closed" if passed else "Implementation issue state gate failed",
            "summary": format_summary(result),
        },
    }


class GitHubApiError(RuntimeError):
    """Raised when a GitHub API request fails."""


class GitHubClient:
    """Minimal REST client for issue lookup and check-run publication."""

    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        self.token = token
        self.api_url = api_url.rstrip("/")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.api_url}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "sciflow-cicd-issue-state-gate",
            },
        )
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise GitHubApiError(f"GitHub API returned HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise GitHubApiError(f"GitHub API request failed: {exc}") from exc

    @staticmethod
    def _repo_path(repository: str) -> str:
        return validated_repo_api_path(repository)

    def fetch_issue(self, repository: str, number: int) -> dict[str, Any]:
        return self._request("GET", f"{self._repo_path(repository)}/issues/{number}")

    def fetch_pull_request(self, repository: str, number: int) -> dict[str, Any]:
        """Read the live pull request so an old event cannot publish over a newer head."""
        return self._request("GET", f"{self._repo_path(repository)}/pulls/{number}")

    def _list_pages(self, path: str, description: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self._request("GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
            if not isinstance(response, list) or any(not isinstance(item, dict) for item in response):
                raise GitHubApiError(f"GitHub returned an invalid {description} list.")
            items.extend(response)
            if len(response) < 100:
                return items
            page += 1

    def list_open_pull_requests(self, repository: str) -> list[dict[str, Any]]:
        """List every open pull request, following the REST API's page size."""
        return self._list_pages(f"{self._repo_path(repository)}/pulls?state=open", "open pull request")

    def list_pull_requests(self, repository: str) -> list[dict[str, Any]]:
        """List open and closed pull requests for gate reconciliation."""
        return self._list_pages(f"{self._repo_path(repository)}/pulls?state=all", "pull request")

    def list_check_runs(self, repository: str, head_sha: str) -> list[dict[str, Any]]:
        """List check runs recorded on a commit, following pagination."""
        runs: list[dict[str, Any]] = []
        page = 1
        base = f"{self._repo_path(repository)}/commits/{quote(head_sha, safe='')}/check-runs"
        while True:
            response = self._request("GET", f"{base}?per_page=100&page={page}")
            if not isinstance(response, dict) or not isinstance(response.get("check_runs"), list):
                raise GitHubApiError("GitHub returned an invalid check run list.")
            page_runs = [run for run in response["check_runs"] if isinstance(run, dict)]
            runs.extend(page_runs)
            if len(response["check_runs"]) < 100:
                return runs
            total_count = response.get("total_count")
            if isinstance(total_count, int) and len(runs) >= total_count:
                return runs
            page += 1

    def list_pull_request_commits(self, repository: str, number: int) -> list[dict[str, Any]]:
        """List commits belonging to a pull request, following pagination."""
        return self._list_pages(f"{self._repo_path(repository)}/pulls/{number}/commits", "pull request commit")

    def update_check(
        self, repository: str, check_id: int, head_sha: str, result: IssueGateResult
    ) -> None:
        """Update an existing Issue State Gate run so apply remains idempotent."""
        payload = _check_run_payload(head_sha, result)
        self._request("PATCH", f"{self._repo_path(repository)}/check-runs/{check_id}", payload)

    def list_issue_comments(self, repository: str, number: int) -> list[dict[str, Any]]:
        """List comments on an issue, following pagination."""
        return self._list_pages(f"{self._repo_path(repository)}/issues/{number}/comments", "issue comment")

    def create_issue_comment(self, repository: str, number: int, body: str) -> None:
        """Post a visible recovery notice on an issue."""
        self._request("POST", f"{self._repo_path(repository)}/issues/{number}/comments", {"body": body})

    def publish_check(
        self,
        repository: str,
        head_sha: str,
        result: IssueGateResult,
    ) -> None:
        payload = _check_run_payload(head_sha, result)
        self._request("POST", f"{self._repo_path(repository)}/check-runs", payload)


def main(event_path: str | None = None) -> int:
    """Evaluate the current PR event and publish a check against its head SHA."""
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

    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        print("This workflow requires a pull_request event.", file=sys.stderr)
        return 2
    base = pull_request.get("base", {})
    repository = str(base.get("repo", {}).get("full_name", ""))
    expected_repository = os.environ.get("GITHUB_REPOSITORY", "")
    event_head = pull_request.get("head", {})
    head_sha = str(event_head.get("sha", ""))
    if base.get("ref") != "main":
        print("Issue State Gate only evaluates pull requests targeting main.", file=sys.stderr)
        return 2
    if not repository or repository.lower() != expected_repository.lower():
        print(
            "The pull request base repository does not match this workflow repository.",
            file=sys.stderr,
        )
        return 2
    if not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
        print("The pull request has no valid current head SHA.", file=sys.stderr)
        return 2

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print(
            "GITHUB_TOKEN is required to resolve issues and publish the check.",
            file=sys.stderr,
        )
        return 2

    client = GitHubClient(
        token,
        os.environ.get("GITHUB_API_URL", "https://api.github.com"),
    )
    number = pull_request.get("number")
    if not isinstance(number, int) or number < 1:
        print("The pull request event has no valid pull request number.", file=sys.stderr)
        return 2
    try:
        live_pull_request = client.fetch_pull_request(repository, number)
    except (GitHubApiError, ValueError) as exc:
        print(f"Could not revalidate the live pull request: {exc}", file=sys.stderr)
        return 2

    live_base = live_pull_request.get("base", {})
    live_base_repo = live_base.get("repo", {}).get("full_name", "")
    live_head = live_pull_request.get("head", {})
    live_head_sha = str(live_head.get("sha", ""))
    if (
        live_pull_request.get("state") != "open"
        or live_base.get("ref") != "main"
        or str(live_base_repo).lower() != repository.lower()
    ):
        print("The live pull request is closed or no longer targets this repository's main branch.", file=sys.stderr)
        return 2
    if live_head_sha != head_sha:
        # A synchronize event for the new head owns the check; an older event
        # must not attach stale input to that newer commit.
        print("The pull request head changed while this event was queued; the newer event will evaluate it.")
        return 0

    event_head_repo = event_head.get("repo", {}).get("full_name", "")
    live_head_repo = live_head.get("repo", {}).get("full_name", "")
    if (
        not event_head_repo
        or not live_head_repo
        or event_head_repo.lower() != repository.lower()
        or live_head_repo.lower() != repository.lower()
    ):
        result = IssueGateResult(
            errors=["Pull requests from forks are unsupported by the Issue State Gate."]
        )
        try:
            client.publish_check(repository, live_head_sha, result)
        except (GitHubApiError, ValueError) as exc:
            print(f"Could not publish the Issue State Gate check: {exc}", file=sys.stderr)
            return 2
        print(format_summary(result))
        return 1

    result = evaluate_implementation_issues(
        str(live_pull_request.get("body") or ""),
        repository,
        lambda number: client.fetch_issue(repository, number),
    )
    try:
        client.publish_check(repository, head_sha, result)
    except (GitHubApiError, ValueError) as exc:
        print(f"Could not publish the Issue State Gate check: {exc}", file=sys.stderr)
        return 2

    print(format_summary(result))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
