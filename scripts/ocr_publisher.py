"""Advisory OCR payload formatting, remote marker inspection, and strict COMMENT publication."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from scripts.configure_github_repo import ApiError
except ImportError:
    from configure_github_repo import ApiError

try:
    from scripts.ocr_app_auth import redact_secrets
except ImportError:
    from ocr_app_auth import redact_secrets

try:
    from scripts.ocr_runner import OcrExecutionError, OcrReviewError
except ImportError:
    from ocr_runner import OcrExecutionError, OcrReviewError

try:
    from scripts.ocr_safety import safe_client_request, validate_ocr_api_path
except ImportError:
    from ocr_safety import safe_client_request, validate_ocr_api_path


VALID_REVIEW_MODES = frozenset({"pr_review", "issue_comment"})
ADVISORY_MARKER_PREFIX = "sciflow-cicd-ocr-advisory:v1"
LEGACY_ADVISORY_MARKER_PREFIX = "gh-setup-ocr-advisory:v1"
MAX_REVIEW_BODY_CHARS = 32000

_FORBIDDEN_PUBLISH_PATH_PATTERNS = (
    re.compile(r"/check-runs(?:/|$)", re.IGNORECASE),
    re.compile(r"/check-suites(?:/|$)", re.IGNORECASE),
    re.compile(r"/statuses/", re.IGNORECASE),
    re.compile(r"/pulls/\d+/merge(?:/|$)", re.IGNORECASE),
    re.compile(r"/graphql(?:/|$)", re.IGNORECASE),
)
_ALLOWED_PR_REVIEW_PATH_RE = re.compile(r"^/repos/[^/]+/[^/]+/pulls/\d+/reviews\Z")
_ALLOWED_ISSUE_COMMENT_PATH_RE = re.compile(r"^/repos/[^/]+/[^/]+/issues/\d+/comments\Z")


def advisory_review_marker(repository: str, pr_number: int, head_sha: str) -> str:
    """Return the stable HTML comment marker tying an advisory review to repo, PR, and HEAD SHA."""
    return f"<!-- {ADVISORY_MARKER_PREFIX}:{repository}#{pr_number}@{head_sha} -->"


def validate_advisory_publish_request(
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]],
    *,
    expected_head_sha: Optional[str] = None,
) -> None:
    """Enforce strictly advisory publication: forbid approvals, change requests, checks, merges, or draft mutations."""
    norm_method = str(method or "").strip().upper()
    validate_ocr_api_path(path)
    for pattern in _FORBIDDEN_PUBLISH_PATH_PATTERNS:
        if pattern.search(path):
            raise OcrReviewError(
                f"Advisory OCR is prohibited from writing to non-advisory endpoint '{path}'."
            )
    if norm_method != "POST":
        raise OcrReviewError(
            f"Advisory OCR only permits POST comment/review creation; '{norm_method} {path}' is forbidden."
        )
    if not isinstance(payload, dict):
        raise OcrReviewError("Advisory OCR publish payload must be a JSON object.")

    if _ALLOWED_PR_REVIEW_PATH_RE.match(path):
        event = payload.get("event")
        if event != "COMMENT":
            raise OcrReviewError(
                f"Advisory OCR forbids pull request review event {event!r}; only 'COMMENT' is allowed."
            )
        commit_id = payload.get("commit_id")
        if not isinstance(commit_id, str) or not commit_id.strip():
            raise OcrReviewError("Advisory PR review must specify commit_id tied to the reviewed HEAD SHA.")
        if expected_head_sha is not None and commit_id != expected_head_sha:
            raise OcrReviewError(
                f"Advisory PR review commit_id '{commit_id}' does not match reviewed HEAD SHA '{expected_head_sha}'."
            )
        body = payload.get("body")
        if not isinstance(body, str) or not body.strip():
            raise OcrReviewError("Advisory PR review body must be non-empty.")
        return

    if _ALLOWED_ISSUE_COMMENT_PATH_RE.match(path):
        if "event" in payload and payload.get("event") not in (None, "COMMENT"):
            raise OcrReviewError(
                f"Advisory OCR forbids non-advisory event {payload.get('event')!r} on issue comments."
            )
        body = payload.get("body")
        if not isinstance(body, str) or not body.strip():
            raise OcrReviewError("Advisory issue comment body must be non-empty.")
        return

    raise OcrReviewError(
        f"Advisory OCR is prohibited from publishing to '{norm_method} {path}'."
    )


_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-`])@([A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?(?:/[A-Za-z0-9_-]+)?)"
)
_MARKDOWN_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(((?:[^()\s]+|\([^()]*\))+)\)")


def _is_allowed_markdown_url(url: str, *, repository: Optional[str]) -> bool:
    stripped = url.strip()
    if not stripped:
        return False
    lower = stripped.lower()
    if lower.startswith(("#", "/", "./", "../")):
        return True
    if repository:
        repo_prefix = f"https://github.com/{repository.strip('/')}/"
        if lower == repo_prefix.rstrip("/").lower() or lower.startswith(repo_prefix.lower()):
            return True
    return False


def sanitize_advisory_markdown(text: str, *, repository: Optional[str] = None) -> str:
    """Neutralize untrusted @mentions and external Markdown links/images in OCR output."""
    if not text:
        return ""

    def _replace_link(match: re.Match[str]) -> str:
        is_image = match.group(1) == "!"
        label = match.group(2).strip()
        url = match.group(3).strip()
        if not is_image and _is_allowed_markdown_url(url, repository=repository):
            return match.group(0)
        return label or "[external link removed]"

    sanitized = _MARKDOWN_LINK_RE.sub(_replace_link, text)
    return _MENTION_RE.sub(lambda m: f"`@{m.group(1)}`", sanitized)


def format_advisory_review_body(
    *,
    repository: str,
    pr_number: int,
    head_sha: str,
    ocr_result: Dict[str, Any],
    secrets_to_redact: Sequence[str] = (),
    files_truncated: Optional[bool] = None,
) -> str:
    """Format strictly advisory OCR findings with structured header and HEAD SHA marker."""
    marker = advisory_review_marker(repository, pr_number, head_sha)
    raw_summary = str(ocr_result.get("summary") or "Automated advisory OCR review completed.").strip()
    summary = sanitize_advisory_markdown(raw_summary, repository=repository)
    findings = ocr_result.get("findings") or []
    is_truncated = bool(
        files_truncated if files_truncated is not None else ocr_result.get("files_truncated")
    )

    lines: List[str] = [
        marker,
        "## Advisory Open Code Review",
        "",
        f"> **Advisory only**: Automated findings for `{repository}#{pr_number}` at commit `{head_sha}`. "
        "This review does not approve, request changes, or block merge.",
        "",
        f"**Reviewed HEAD:** `{head_sha}`",
        f"**Findings:** {len(findings)}",
    ]
    if is_truncated:
        lines.extend(
            [
                "",
                "> **Note:** Changed files were truncated at the 100-file boundary; files beyond the first 100 were not inspected.",
            ]
        )
    lines.extend(
        [
            "",
            "### Summary",
            summary,
        ]
    )
    if findings:
        lines.extend(["", "### Findings"])
        for idx, finding in enumerate(findings, start=1):
            loc_parts: List[str] = []
            if finding.get("file"):
                loc_parts.append(str(finding["file"]))
            if finding.get("line") is not None:
                loc_parts.append(f"L{finding['line']}")
            loc = ":".join(loc_parts)
            severity = str(finding.get("severity") or "info").upper()
            msg = sanitize_advisory_markdown(
                str(finding.get("message") or "").strip(),
                repository=repository,
            )
            prefix = f"{idx}. **[{severity}]**"
            if loc:
                prefix += f" `{loc}` —"
            lines.append(f"{prefix} {msg}".rstrip())

    body = redact_secrets("\n".join(lines).strip() + "\n", secrets_to_redact)
    if len(body) > MAX_REVIEW_BODY_CHARS:
        body = body[: MAX_REVIEW_BODY_CHARS - 40].rstrip() + "\n\n_(truncated)_\n"
    return body


def publish_advisory_review(
    client: Any,
    *,
    repository: str,
    pr_number: int,
    head_sha: str,
    review_mode: str,
    body: str,
) -> Tuple[int, Dict[str, Any]]:
    """Validate and publish a strictly advisory COMMENT review or issue comment."""
    if review_mode == "pr_review":
        path = f"/repos/{repository}/pulls/{pr_number}/reviews"
        payload: Dict[str, Any] = {
            "commit_id": head_sha,
            "event": "COMMENT",
            "body": body,
        }
    elif review_mode == "issue_comment":
        path = f"/repos/{repository}/issues/{pr_number}/comments"
        payload = {"body": body}
    else:
        raise OcrReviewError(
            f"Unsupported review_mode {review_mode!r}; expected one of {sorted(VALID_REVIEW_MODES)}."
        )

    validate_advisory_publish_request("POST", path, payload, expected_head_sha=head_sha)
    try:
        status, resp_body = safe_client_request(client, "POST", path, payload=payload)
    except (ApiError, OSError) as exc:
        raise OcrExecutionError(f"Failed publishing advisory review to {path}: {exc}") from None
    return status, resp_body if isinstance(resp_body, dict) else {}


def find_remote_advisory_marker(
    client: Any,
    *,
    repository: str,
    pr_number: int,
    head_sha: str,
) -> Optional[Dict[str, Any]]:
    """Inspect existing PR reviews and issue comments for a matching HEAD advisory marker."""
    marker = advisory_review_marker(repository, pr_number, head_sha)
    marker_lower = marker.lower()
    legacy_marker = f"<!-- {LEGACY_ADVISORY_MARKER_PREFIX}:{repository}#{pr_number}@{head_sha} -->"
    legacy_marker_lower = legacy_marker.lower()

    endpoints = (
        (f"/repos/{repository}/pulls/{pr_number}/reviews", "pr_review"),
        (f"/repos/{repository}/issues/{pr_number}/comments", "issue_comment"),
    )
    for path_prefix, mode_label in endpoints:
        for page in range(1, 101):
            path = f"{path_prefix}?per_page=100&page={page}"
            try:
                status, body = safe_client_request(client, "GET", path)
            except (ApiError, OSError) as exc:
                raise OcrExecutionError(
                    f"Failed inspecting remote advisory markers at {path}: {exc}"
                ) from None
            if status == 404:
                break
            if status != 200 or not isinstance(body, list):
                msg = (
                    body.get("message", f"HTTP {status}")
                    if isinstance(body, dict)
                    else f"HTTP {status}"
                )
                raise OcrExecutionError(f"GET {path}: HTTP {status}: {msg}")
            for item in body:
                if not isinstance(item, dict):
                    continue
                raw_body = item.get("body")
                if isinstance(raw_body, str) and (
                    marker in raw_body
                    or marker_lower in raw_body.lower()
                    or legacy_marker in raw_body
                    or legacy_marker_lower in raw_body.lower()
                ):
                    return {
                        "review_mode": mode_label,
                        "comment_id": item.get("id"),
                        "comment_url": item.get("html_url"),
                    }
            if len(body) < 100:
                break
    return None
