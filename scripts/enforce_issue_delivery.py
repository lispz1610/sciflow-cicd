#!/usr/bin/env python3
"""Reopen closed implementation issues that lack verified PR delivery evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

try:
    from scripts.configure_github_repo import ApiError, GitHubApi, get_pages
    from scripts.issue_state_gate import parse_implementation_issues
    from scripts.verify_issue_delivery import verify_issue_delivery
except ImportError:  # pragma: no cover - direct script execution
    from configure_github_repo import ApiError, GitHubApi, get_pages
    from issue_state_gate import parse_implementation_issues
    from verify_issue_delivery import verify_issue_delivery


DIAGNOSTIC_MARKER = "<!-- issue-delivery-enforcement:v1 -->"
VERIFIED_MARKER_RE = re.compile(
    r"<!-- issue-delivery-verified:v1 pr=(\d+) body_sha256=([0-9a-f]{64}) -->"
)
IMPLEMENTATION_LABELS = {"ready-for-agent", "ready-for-human"}
PARENT_SECTION = re.compile(r"(?im)^##\s+Parent\s*#*\s*$")
IMPLEMENTATION_SECTION = re.compile(r"(?im)^#{1,6}\s+Implementation issues\s*#*\s*$")
NEXT_SECTION = re.compile(r"(?m)^#{1,6}\s+")


def _result(action: str, code: str, issue: int, **details) -> dict:
    return {"action": action, "code": code, "issue": issue, **details}


def _read_issue(api: GitHubApi, repository: str, issue_number: int) -> dict:
    path = f"/repos/{repository}/issues/{issue_number}"
    status, body = api.request("GET", path)
    if status != 200 or not isinstance(body, dict):
        raise ApiError(f"GET {path}: cannot verify current issue state")
    return body


def _read_comments(api: GitHubApi, repository: str, issue_number: int) -> list[dict]:
    path = f"/repos/{repository}/issues/{issue_number}/comments"
    return get_pages(api, path, key=None)


def _has_managed_diagnostic(comments: list[dict]) -> bool:
    return any(
        isinstance(comment, dict)
        and isinstance(comment.get("body"), str)
        and DIAGNOSTIC_MARKER in comment["body"]
        for comment in comments
    )


def _body_fingerprint(issue: dict) -> str | None:
    body = issue.get("body")
    if not isinstance(body, str):
        return None
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _has_parent_link(body: str, repository: str) -> bool:
    match = PARENT_SECTION.search(body)
    if match is None:
        return False
    next_section = NEXT_SECTION.search(body, match.end())
    content = body[match.end():next_section.start() if next_section else len(body)]
    try:
        parse_implementation_issues("## Implementation issues\n" + content, repository)
    except ValueError as exc:
        raise ApiError(f"cannot verify issue Parent link: {exc}") from exc
    return True


def _declared_by_integration_pull_request(api: GitHubApi, repository: str, issue_number: int) -> bool:
    pulls = get_pages(api, f"/repos/{repository}/pulls?state=open", key=None)
    for pull in pulls:
        if not isinstance(pull, dict):
            raise ApiError("cannot verify malformed open pull request list")
        base = pull.get("base")
        if not isinstance(base, dict) or not isinstance(base.get("repo"), dict):
            raise ApiError("cannot verify open pull request base repository")
        if not isinstance(base.get("ref"), str) or not isinstance(base["repo"].get("full_name"), str):
            raise ApiError("cannot verify open pull request base repository")
        if base["ref"] != "main" or base["repo"]["full_name"].casefold() != repository.casefold():
            continue
        body = pull.get("body")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise ApiError("cannot verify open pull request body")
        if not IMPLEMENTATION_SECTION.search(body):
            continue
        try:
            numbers = parse_implementation_issues(body, repository)
        except ValueError as exc:
            raise ApiError(f"cannot verify open pull request implementation issues: {exc}") from exc
        if issue_number in numbers:
            return True
    return False


def _verified_receipt(comments: list[dict], body_sha256: str | None) -> int | None:
    if body_sha256 is None:
        return None
    for comment in comments:
        if not isinstance(comment, dict) or not isinstance(comment.get("body"), str):
            continue
        match = VERIFIED_MARKER_RE.search(comment["body"])
        if match and match.group(2) == body_sha256:
            return int(match.group(1))
    return None


def _post_verified_receipt(
    api: GitHubApi, repository: str, issue_number: int, pull_request: int, body_sha256: str,
) -> None:
    path = f"/repos/{repository}/issues/{issue_number}/comments"
    body = (
        f"<!-- issue-delivery-verified:v1 pr={pull_request} body_sha256={body_sha256} -->\n"
        f"Delivery evidence was verified when this issue was closed. The linked pull request is #{pull_request}."
    )
    status, response = api.request("POST", path, {"body": body})
    if status not in (200, 201) or not isinstance(response, dict):
        raise ApiError(f"POST {path}: could not verify delivery receipt comment")


def _verify_receipt_pull_request(api: GitHubApi, repository: str, issue_number: int, pull_request: int) -> dict:
    path = f"/repos/{repository}/pulls/{pull_request}"
    status, response = api.request("GET", path)
    if status != 200 or not isinstance(response, dict):
        raise ApiError(f"GET {path}: could not re-read pull request for verified issue #{issue_number}")
    return response


def _post_diagnostic(api: GitHubApi, repository: str, issue_number: int, code: str) -> None:
    path = f"/repos/{repository}/issues/{issue_number}/comments"
    body = (
        f"{DIAGNOSTIC_MARKER}\n"
        "This implementation issue was closed before its delivery evidence passed verification. "
        f"The verifier returned `{code}`. The issue is being reopened; publish or correct the "
        "delivery pull request and its recorded head SHA, then close the issue after the "
        "published pull request has been accepted and before it is merged."
    )
    status, response = api.request("POST", path, {"body": body})
    if status not in (200, 201) or not isinstance(response, dict):
        raise ApiError(f"POST {path}: could not verify managed diagnostic comment")


def _reopen_issue(api: GitHubApi, repository: str, issue_number: int) -> None:
    path = f"/repos/{repository}/issues/{issue_number}"
    status, response = api.request("PATCH", path, {"state": "open"})
    if status != 200 or not isinstance(response, dict) or response.get("state") != "open":
        raise ApiError(f"PATCH {path}: could not verify issue was reopened")


def _record_qualification(
    api: GitHubApi, repository: str, issue_number: int, verification: dict, body_fingerprint: str | None,
) -> dict:
    current = _read_issue(api, repository, issue_number)
    if current.get("state") != "closed":
        return _result("none", "issue_not_closed", issue_number)
    if _body_fingerprint(current) != body_fingerprint or body_fingerprint is None:
        return _result("error", "verification_race", issue_number,
                       message="issue body changed during delivery verification; rerun the event")
    confirmed = verify_issue_delivery(api, repository, issue_number)
    if confirmed["code"] == "api_failure":
        return _result("error", "api_failure", issue_number,
                       message=confirmed.get("message", "delivery evidence could not be verified"))
    if (not confirmed["eligible"]
            or confirmed.get("pull_request") != verification.get("pull_request")
            or confirmed.get("head_sha") != verification.get("head_sha")):
        return _result("error", "verification_race", issue_number,
                       message="delivery evidence changed during verification; rerun after the pull request settles")
    _post_verified_receipt(api, repository, issue_number,
                           verification["pull_request"], body_fingerprint)
    return _result("none", "eligible", issue_number,
                   pull_request=verification.get("pull_request"),
                   head_sha=verification.get("head_sha"), receipt="recorded")


def handle_issue_closed_event(api: GitHubApi, repository: str, issue_number: int) -> dict:
    """Re-read and enforce issue delivery after a `issues.closed` event.

    The event payload is only a trigger. Current GitHub state and delivery evidence
    determine whether a write is needed. A managed comment is recorded before the
    reopen write so retries can safely finish a partial attempt without duplicating it.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or issue_number < 1:
        return _result("error", "api_failure", issue_number, message="invalid repository or issue number")

    try:
        issue = _read_issue(api, repository, issue_number)
        if issue.get("state") != "closed":
            return _result("none", "issue_not_closed", issue_number)

        labels = issue.get("labels")
        if not isinstance(labels, list) or any(not isinstance(label, dict) or
                not isinstance(label.get("name"), str) for label in labels):
            return _result("error", "api_failure", issue_number,
                           message="issue labels are missing or malformed")
        body = issue.get("body")
        if not isinstance(body, str):
            return _result("error", "api_failure", issue_number,
                           message="issue body is missing or malformed")
        in_scope = (any(label["name"] in IMPLEMENTATION_LABELS for label in labels)
                    or _has_parent_link(body, repository)
                    or _declared_by_integration_pull_request(api, repository, issue_number))
        if not in_scope:
            return _result("none", "not_implementation_issue", issue_number)

        comments = _read_comments(api, repository, issue_number)
        fingerprint = _body_fingerprint(issue)
        receipt_pr = _verified_receipt(comments, fingerprint)
        if receipt_pr is not None:
            _verify_receipt_pull_request(api, repository, issue_number, receipt_pr)
            return _result("none", "eligible", issue_number, pull_request=receipt_pr,
                           verified_receipt=True)

        verification = verify_issue_delivery(api, repository, issue_number)
        if verification["eligible"]:
            return _record_qualification(api, repository, issue_number, verification, fingerprint)
        if verification["code"] == "local_only_ineligible":
            current = _read_issue(api, repository, issue_number)
            if current.get("state") != "closed":
                return _result("none", "issue_not_closed", issue_number)
            if _body_fingerprint(current) != fingerprint:
                return _result("error", "verification_race", issue_number,
                               message="issue body changed during local-only exception verification; rerun the event")
            return _result("none", "local_only_exception", issue_number)
        if verification["code"] == "api_failure":
            return _result("error", "api_failure", issue_number,
                           message=verification.get("message", "delivery evidence could not be verified"))

        # Recheck live issue state and evidence after the initial verification. This
        # makes event replays and issue updates during validation safe to handle.
        current = _read_issue(api, repository, issue_number)
        if current.get("state") != "closed":
            return _result("none", "issue_not_closed", issue_number)
        latest = verify_issue_delivery(api, repository, issue_number)
        if latest["eligible"]:
            return _record_qualification(api, repository, issue_number, latest, fingerprint)
        if latest["code"] == "api_failure":
            return _result("error", "api_failure", issue_number,
                           message=latest.get("message", "delivery evidence could not be verified"))

        had_diagnostic = _has_managed_diagnostic(comments)
        # Avoid overwriting a state change made while checking comments. A tiny
        # race remains between this read and PATCH, so the workflow serializes runs
        # per issue as well.
        current = _read_issue(api, repository, issue_number)
        if current.get("state") != "closed":
            return _result("none", "issue_not_closed", issue_number,
                           diagnostic="existing" if had_diagnostic else "not_posted")
        if not had_diagnostic:
            _post_diagnostic(api, repository, issue_number, latest["code"])
        _reopen_issue(api, repository, issue_number)
        return _result("reopened", latest["code"], issue_number,
                       diagnostic="existing" if had_diagnostic else "posted")
    except ApiError as exc:
        return _result("error", "api_failure", issue_number, message=str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Repository in owner/name form")
    parser.add_argument("--issue", required=True, type=int, help="Closed implementation issue number")
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        result = _result("error", "api_failure", args.issue,
                         message="GH_TOKEN or GITHUB_TOKEN is required")
    else:
        api = GitHubApi(token, os.environ.get("GITHUB_API_URL", "https://api.github.com"))
        result = handle_issue_closed_event(api, args.repo, args.issue)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["action"] != "error" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
