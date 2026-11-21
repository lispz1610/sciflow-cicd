#!/usr/bin/env python3
"""Read-only check that an implementation issue has a published integration PR."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import quote

try:
    from scripts.configure_github_repo import ApiError, GitHubApi
except ImportError:  # pragma: no cover - direct script execution
    from configure_github_repo import ApiError, GitHubApi


PR_URL_RE = re.compile(r"https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)(?:\b|/)", re.IGNORECASE)
SHA_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])")
ISSUE_URL_RE = re.compile(r"https?://github\.com/([^/\s]+)/([^/\s]+)/issues/(\d+)(?:\b|/)", re.IGNORECASE)
QUALIFIED_ISSUE_RE = re.compile(r"\b([\w.-]+)/([\w.-]+)#(\d+)\b")


class DeliveryApiError(RuntimeError):
    """Raised when a remote response cannot safely establish delivery evidence."""


def _ineligible(code: str, **details) -> dict:
    return {"eligible": False, "code": code, **details}


def _section(body: str, heading: str) -> tuple[bool, str]:
    pattern = re.compile(rf"(?im)^##\s+{re.escape(heading)}\s*#*\s*$")
    matches = list(pattern.finditer(body))
    if not matches:
        return False, ""
    if len(matches) != 1:
        return True, ""
    start = matches[0].end()
    next_heading = re.search(r"(?m)^#{1,2}\s+", body[start:])
    end = start + next_heading.start() if next_heading else len(body)
    return True, body[start:end]


def _delivery_evidence(body: str, repository: str) -> tuple[str | None, int | None, str | None]:
    found, content = _section(body, "Delivery")
    if not found:
        return "delivery_absent", None, None
    urls = list(PR_URL_RE.finditer(content))
    shas = list(SHA_RE.finditer(content))
    if re.search(r"(?im)^\s*(?:[-*]\s*)?local[- ]only exception\b", content):
        return "local_only_ineligible", None, None
    if len(urls) != 1 or len(shas) != 1:
        return "delivery_ambiguous", None, None
    match = urls[0]
    linked_repo = f"{match.group(1)}/{match.group(2)}".lower()
    if linked_repo != repository.lower():
        return "wrong_repository", None, None
    return None, int(match.group(3)), shas[0].group(0).lower()


def _issue_linked_in_section(body: str, heading: str, repository: str, issue_number: int) -> bool:
    found, content = _section(body, heading)
    if not found:
        return False
    for match in ISSUE_URL_RE.finditer(content):
        if (f"{match.group(1)}/{match.group(2)}".lower() == repository.lower()
                and int(match.group(3)) == issue_number):
            return True
    for match in QUALIFIED_ISSUE_RE.finditer(content):
        if (f"{match.group(1)}/{match.group(2)}".lower() == repository.lower()
                and int(match.group(3)) == issue_number):
            return True
    return bool(re.search(rf"(?<![\w/])#0*{issue_number}\b", content))


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _read_object(api: GitHubApi, path: str, *, missing_code: str | None = None) -> dict:
    try:
        status, body = api.request("GET", path)
    except ApiError as exc:
        if missing_code == "pr_inaccessible" and "HTTP 403" in str(exc):
            raise LookupError(missing_code) from exc
        raise DeliveryApiError(str(exc)) from exc
    if status == 404 and missing_code:
        raise LookupError(missing_code)
    if status != 200 or not isinstance(body, dict):
        raise DeliveryApiError(f"GET {path}: cannot verify remote response")
    return body


def verify_issue_delivery(api: GitHubApi, repository: str, issue_number: int) -> dict:
    """Verify issue Delivery evidence against its published or merged PR."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or issue_number < 1:
        return _ineligible("api_failure", message="invalid repository or issue number")
    try:
        issue = _read_object(api, f"/repos/{repository}/issues/{issue_number}")
        issue_body = issue.get("body")
        if not isinstance(issue_body, str):
            return _ineligible("delivery_absent", issue=issue_number)
        code, pr_number, recorded_sha = _delivery_evidence(issue_body, repository)
        if code:
            return _ineligible(code, issue=issue_number)

        pr = _read_object(api, f"/repos/{repository}/pulls/{pr_number}", missing_code="pr_inaccessible")
        state = pr.get("state")
        merged_at = pr.get("merged_at")
        merged_at_value = _parse_timestamp(merged_at)
        merge_commit_sha = pr.get("merge_commit_sha")
        if state == "open" and merged_at is None:
            delivery_mode = "open"
        elif (
            state == "closed"
            and merged_at_value is not None
            and isinstance(merge_commit_sha, str)
            and re.fullmatch(r"[0-9a-fA-F]{40}", merge_commit_sha)
        ):
            delivery_mode = "merged"
        else:
            return _ineligible("pr_closed", issue=issue_number, pull_request=pr_number)
        if delivery_mode == "merged":
            closed_at_value = _parse_timestamp(issue.get("closed_at"))
            if (issue.get("state") != "closed" or closed_at_value is None
                    or closed_at_value <= merged_at_value):
                return _ineligible("issue_not_closed_after_merge", issue=issue_number,
                                   pull_request=pr_number)
        else:
            if type(pr.get("draft")) is not bool:
                raise DeliveryApiError("pull request response is missing publication state")
            if pr["draft"]:
                return _ineligible("pr_unpublished", issue=issue_number, pull_request=pr_number)
        base = pr.get("base")
        head = pr.get("head")
        if not isinstance(base, dict) or not isinstance(head, dict):
            raise DeliveryApiError("pull request response is missing base or head data")
        base_repo = base.get("repo")
        head_repo = head.get("repo")
        if not isinstance(base_repo, dict) or not isinstance(head_repo, dict):
            raise DeliveryApiError("pull request response is missing repository identity")
        base_repo_name = base_repo.get("full_name")
        head_repo_name = head_repo.get("full_name")
        if not isinstance(base_repo_name, str) or not isinstance(head_repo_name, str):
            raise DeliveryApiError("pull request response has malformed repository identity")
        if (base_repo_name.lower() != repository.lower()
                or head_repo_name.lower() != repository.lower()):
            return _ineligible("wrong_repository", issue=issue_number, pull_request=pr_number)
        if base.get("ref") != "main":
            return _ineligible("wrong_base", issue=issue_number, pull_request=pr_number)
        branch = head.get("ref")
        live_sha = head.get("sha")
        if not isinstance(branch, str) or not branch or not isinstance(live_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", live_sha):
            raise DeliveryApiError("pull request head data is malformed")
        pr_body = pr.get("body") if isinstance(pr.get("body"), str) else ""
        implementation_linked = _issue_linked_in_section(pr_body, "Implementation issues", repository, issue_number)
        parent_linked = delivery_mode == "merged" and _issue_linked_in_section(pr_body, "Parent", repository, issue_number)
        if implementation_linked == parent_linked:
            return _ineligible("issue_not_linked", issue=issue_number, pull_request=pr_number)

        if delivery_mode == "open":
            branch_path = f"/repos/{repository}/branches/{quote(branch, safe='')}"
            try:
                branch_data = _read_object(api, branch_path, missing_code="wrong_branch")
            except LookupError:
                return _ineligible("wrong_branch", issue=issue_number, pull_request=pr_number)
            commit = branch_data.get("commit")
            branch_sha = commit.get("sha") if isinstance(commit, dict) else None
            if not isinstance(branch_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", branch_sha):
                raise DeliveryApiError("delivery branch response is malformed")
            if branch_sha.lower() != recorded_sha:
                return _ineligible("stale_sha", issue=issue_number, pull_request=pr_number)
        if live_sha.lower() != recorded_sha:
            return _ineligible("stale_sha", issue=issue_number, pull_request=pr_number)
        return {"eligible": True, "code": "eligible", "issue": issue_number,
                "pull_request": pr_number, "head_sha": recorded_sha}
    except LookupError:
        return _ineligible("pr_inaccessible", issue=issue_number)
    except DeliveryApiError as exc:
        return _ineligible("api_failure", issue=issue_number, message=str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="Repository in owner/name form")
    parser.add_argument("--issue", required=True, type=int, help="Implementation issue number")
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        result = _ineligible("api_failure", issue=args.issue, message="GH_TOKEN or GITHUB_TOKEN is required")
    else:
        api = GitHubApi(token, os.environ.get("GITHUB_API_URL", "https://api.github.com"))
        result = verify_issue_delivery(api, args.repo, args.issue)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["eligible"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
