"""Allowlisted pull request polling, fairness rotation, backoff, and single-flight guard for OCR."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TextIO, Tuple, Union
import urllib.error
import urllib.request

try:
    from scripts.configure_github_repo import API_VERSION, ApiError
except ImportError:
    from configure_github_repo import API_VERSION, ApiError

try:
    from scripts.ocr_app_auth import (
        MAX_RESPONSE_BYTES,
        AppPreflightResult,
        OcrAppAuthError,
        OcrAppConfig,
        ScopedInstallationTokenManager,
        collect_env_secrets,
        default_api_factory,
        dispatch_client_request,
        normalize_app_id,
        preflight_ocr_app,
        read_bounded_response,
        redact_secrets,
        resolve_app_config,
        split_csv_values,
        validate_api_base_url,
    )
except ImportError:
    from ocr_app_auth import (
        MAX_RESPONSE_BYTES,
        AppPreflightResult,
        OcrAppAuthError,
        OcrAppConfig,
        ScopedInstallationTokenManager,
        collect_env_secrets,
        default_api_factory,
        dispatch_client_request,
        normalize_app_id,
        preflight_ocr_app,
        read_bounded_response,
        redact_secrets,
        resolve_app_config,
        split_csv_values,
        validate_api_base_url,
    )

try:
    from scripts.ocr_safety import check_same_repo_origin, validate_ocr_api_path
except ImportError:
    from ocr_safety import check_same_repo_origin, validate_ocr_api_path

try:
    from scripts.ocr_state import default_ocr_runtime_dir
except ImportError:
    from ocr_state import default_ocr_runtime_dir

try:
    from scripts.ocr_state import load_json_state, update_json_state, validate_lock_path
except ImportError:
    from ocr_state import load_json_state, update_json_state, validate_lock_path


DEFAULT_ELIGIBLE_LABEL = "eligible for review"
MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
VALID_ELIGIBILITY_MODES = frozenset({"either", "label", "maintainer"})

MIN_POLL_INTERVAL_SECONDS = 5.0
MAX_POLL_INTERVAL_SECONDS = 3600.0
DEFAULT_POLL_INTERVAL_SECONDS = 60.0

MIN_BACKOFF_SECONDS = 1.0
DEFAULT_BASE_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 300.0
DEFAULT_MAX_RETRIES = 3

TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


class OcrPollerError(RuntimeError):
    """Raised when OCR polling configuration or execution fails."""


class SingleFlightConflictError(OcrPollerError):
    """Raised when another OCR review evaluation already holds the single-flight lock."""


class OcrHttpError(ApiError):
    """HTTP error preserving status code and normalized response headers for backoff decisions."""

    def __init__(self, status_code: int, message: str, headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}


class PollingGitHubClient:
    """Stdlib GitHub REST client exposing both (status, body) and (status, headers, body)."""

    def __init__(self, token: str, base_url: str = "https://api.github.com") -> None:
        self.token = token
        self.base_url = validate_api_base_url(base_url)

    def request_with_headers(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Dict[str, str], Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "github-repository-configurator",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = read_bounded_response(resp, context=f"{method} {path}")
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                body = json.loads(raw) if raw else None
                return resp.status, resp_headers, body
        except urllib.error.HTTPError as error:
            resp_headers = (
                {k.lower(): v for k, v in error.headers.items()}
                if getattr(error, "headers", None) is not None
                else {}
            )
            raw = error.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
            if method == "GET" and error.code == 404:
                return 404, resp_headers, None
            try:
                parsed = json.loads(raw)
                message = parsed.get("message", raw) if isinstance(parsed, dict) else raw
            except ValueError:
                message = raw
            return error.code, resp_headers, {"message": message}
        except urllib.error.URLError as error:
            raise ApiError(f"{method} {path}: {error.reason}") from None

    def request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Any]:
        status, _headers, body = self.request_with_headers(method, path, payload=payload)
        if status >= 400 and not (method == "GET" and status == 404):
            msg = body.get("message", f"HTTP {status}") if isinstance(body, dict) else f"HTTP {status}"
            raise ApiError(f"{method} {path}: HTTP {status}: {msg}")
        return status, body


@dataclass(frozen=True)
class PullRequestCandidate:
    """Eligible same-repository open pull request discovered by OCR polling."""

    repository: str
    installation_id: int
    number: int
    title: str
    head_sha: str
    head_ref: str
    base_ref: str
    head_repo: str
    base_repo: str
    author_login: str
    author_association: str
    labels: Tuple[str, ...]
    draft: bool
    eligibility_reason: str
    html_url: str = ""


@dataclass
class RepositoryPollRecord:
    """Per-repository polling diagnostics for a single poll cycle."""

    repository: str
    installation_id: int
    raw_items_seen: int = 0
    issues_filtered_out: int = 0
    pull_requests_seen: int = 0
    eligible_candidates: List[PullRequestCandidate] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    retries: int = 0
    rate_limit_wait_seconds: float = 0.0


@dataclass
class PollCycleResult:
    """Structured result of one OCR repository polling cycle."""

    passed: bool
    eligible_repositories: List[str]
    excluded_repositories: List[Dict[str, Any]]
    polled_repositories: List[str]
    candidates: List[PullRequestCandidate]
    selected_candidate: Optional[PullRequestCandidate] = None
    evaluation_ran: bool = False
    evaluation_skipped_busy: bool = False
    evaluation_result: Any = None
    next_cursor_index: int = 0
    next_cursor_repository: Optional[str] = None
    sleep_delays: List[float] = field(default_factory=list)
    repo_records: List[RepositoryPollRecord] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self, *, extra_secrets: Sequence[str] = ()) -> Dict[str, Any]:
        """Return a JSON-serializable, secret-redacted representation."""
        raw = {
            "passed": self.passed,
            "eligible_repositories": list(self.eligible_repositories),
            "excluded_repositories": list(self.excluded_repositories),
            "polled_repositories": list(self.polled_repositories),
            "candidates": [asdict(c) for c in self.candidates],
            "selected_candidate": asdict(self.selected_candidate) if self.selected_candidate else None,
            "evaluation_ran": self.evaluation_ran,
            "evaluation_skipped_busy": self.evaluation_skipped_busy,
            "evaluation_result": self.evaluation_result,
            "next_cursor_index": self.next_cursor_index,
            "next_cursor_repository": self.next_cursor_repository,
            "sleep_delays": list(self.sleep_delays),
            "repo_records": [
                {
                    "repository": rec.repository,
                    "installation_id": rec.installation_id,
                    "raw_items_seen": rec.raw_items_seen,
                    "issues_filtered_out": rec.issues_filtered_out,
                    "pull_requests_seen": rec.pull_requests_seen,
                    "eligible_candidates": [asdict(c) for c in rec.eligible_candidates],
                    "errors": [redact_secrets(err, extra_secrets) for err in rec.errors],
                    "retries": rec.retries,
                    "rate_limit_wait_seconds": rec.rate_limit_wait_seconds,
                }
                for rec in self.repo_records
            ],
            "errors": [redact_secrets(err, extra_secrets) for err in self.errors],
        }
        return json.loads(redact_secrets(json.dumps(raw, default=str), extra_secrets))


def is_pull_request_item(item: Any) -> bool:
    """Return True if item represents a GitHub Pull Request rather than an Issue or non-PR."""
    if not isinstance(item, dict):
        return False
    number = item.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return False
    if "pull_request" in item and item.get("pull_request") is None:
        return False
    html_url = item.get("html_url")
    if isinstance(html_url, str) and "/issues/" in html_url and not isinstance(item.get("pull_request"), dict):
        return False
    head = item.get("head")
    base = item.get("base")
    if isinstance(head, dict) and isinstance(base, dict):
        sha = head.get("sha")
        if isinstance(sha, str) and bool(sha.strip()):
            return True
    if isinstance(item.get("pull_request"), dict):
        return True
    return False


def is_same_repo_pull_request(pr: Dict[str, Any], repository: str) -> bool:
    """Return True if PR head and base repositories both match repository (no forks)."""
    return check_same_repo_origin(pr, repository).allowed


def _extract_label_names(pr: Dict[str, Any]) -> Tuple[str, ...]:
    raw_labels = pr.get("labels")
    if not isinstance(raw_labels, list):
        return ()
    names: List[str] = []
    for item in raw_labels:
        if isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        elif isinstance(item, str) and item.strip():
            names.append(item.strip())
    return tuple(names)


@dataclass(frozen=True)
class PrEligibilityConfig:
    """Immutable pull request eligibility scoping configuration."""

    eligibility_mode: str = "either"
    eligible_label: str = DEFAULT_ELIGIBLE_LABEL
    maintainers: Tuple[str, ...] = ()
    include_drafts: bool = False


def resolve_eligibility_config(
    eligibility_config: Optional[PrEligibilityConfig] = None,
    *,
    eligibility_mode: str = "either",
    eligible_label: str = DEFAULT_ELIGIBLE_LABEL,
    maintainers: Sequence[str] = (),
    include_drafts: bool = False,
    expand_maintainers: bool = False,
) -> PrEligibilityConfig:
    """Return an explicit PrEligibilityConfig or build one from legacy keyword parameters."""
    if eligibility_config is not None:
        cfg = eligibility_config
    else:
        cfg = PrEligibilityConfig(
            eligibility_mode=eligibility_mode,
            eligible_label=eligible_label,
            maintainers=tuple(maintainers),
            include_drafts=include_drafts,
        )
    if not expand_maintainers:
        return cfg
    return replace(
        cfg,
        maintainers=tuple(split_csv_values([str(x) for x in cfg.maintainers])),
    )


def evaluate_pr_eligibility(
    pr: Dict[str, Any],
    repository: str,
    *,
    eligibility_config: Optional[PrEligibilityConfig] = None,
    eligibility_mode: str = "either",
    eligible_label: str = DEFAULT_ELIGIBLE_LABEL,
    maintainers: Sequence[str] = (),
    include_drafts: bool = False,
) -> Tuple[bool, str]:
    """Evaluate whether an open PR is an eligible same-repo Advisory Review candidate."""
    elig = resolve_eligibility_config(
        eligibility_config,
        eligibility_mode=eligibility_mode,
        eligible_label=eligible_label,
        maintainers=maintainers,
        include_drafts=include_drafts,
    )
    if elig.eligibility_mode not in VALID_ELIGIBILITY_MODES:
        raise OcrPollerError(
            f"Unsupported eligibility_mode {elig.eligibility_mode!r}; expected one of {sorted(VALID_ELIGIBILITY_MODES)}"
        )
    if not is_pull_request_item(pr):
        return False, "not_a_pull_request"
    if str(pr.get("state") or "").strip().lower() != "open":
        return False, "not_open"
    if not elig.include_drafts and bool(pr.get("draft", False)):
        return False, "draft_pr"
    if not is_same_repo_pull_request(pr, repository):
        return False, "fork_or_cross_repo"

    labels = _extract_label_names(pr)
    target_label = elig.eligible_label.strip().lower()
    has_label = bool(target_label) and any(lbl.lower() == target_label for lbl in labels)

    user = pr.get("user") if isinstance(pr.get("user"), dict) else {}
    author_login = str(user.get("login") or "").strip()
    author_assoc = str(pr.get("author_association") or "").strip().upper()

    maintainer_set = {str(m).strip().lower() for m in elig.maintainers if str(m).strip()}
    if maintainer_set:
        is_maintainer = bool(author_login) and author_login.lower() in maintainer_set
    else:
        is_maintainer = author_assoc in MAINTAINER_ASSOCIATIONS

    if elig.eligibility_mode == "label":
        if has_label:
            return True, f"label:{elig.eligible_label.strip()}"
        return False, "missing_eligible_label"
    if elig.eligibility_mode == "maintainer":
        if is_maintainer:
            return True, f"maintainer:{author_assoc or author_login}"
        return False, "not_maintainer_pr"

    # eligibility_mode == "either"
    if has_label:
        return True, f"label:{elig.eligible_label.strip()}"
    if is_maintainer:
        return True, f"maintainer:{author_assoc or author_login}"
    return False, "not_labeled_or_maintainer"


class ActiveEvaluationLock:
    """Single-flight advisory lock ensuring at most one active OCR evaluation runs at a time."""

    _process_guard = threading.Lock()
    _active_paths: set[str] = set()

    def __init__(
        self,
        lock_path: Union[str, Path],
        *,
        repository: str = "",
        pr_number: Optional[int] = None,
        head_sha: str = "",
    ) -> None:
        if not lock_path:
            raise OcrPollerError("lock_path is required")
        self.lock_path = Path(lock_path)
        self.repository = repository
        self.pr_number = pr_number
        self.head_sha = head_sha
        self._fd: Optional[int] = None
        self._resolved_key: Optional[str] = None

    def acquire(self) -> "ActiveEvaluationLock":
        validate_lock_path(self.lock_path, error_cls=OcrPollerError)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_key = str(self.lock_path.resolve())

        with ActiveEvaluationLock._process_guard:
            if resolved_key in ActiveEvaluationLock._active_paths:
                raise SingleFlightConflictError(
                    f"Single-flight lock is already held in-process: {self.lock_path}"
                )
            ActiveEvaluationLock._active_paths.add(resolved_key)
            self._resolved_key = resolved_key

        fd: Optional[int] = None
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SingleFlightConflictError(
                    f"Single-flight lock is already held by another process: {self.lock_path}"
                ) from None

            meta = {
                "pid": os.getpid(),
                "acquired_at": datetime.now(timezone.utc).isoformat(),
                "repository": self.repository,
                "pr_number": self.pr_number,
                "head_sha": self.head_sha,
            }
            payload = (json.dumps(meta, indent=2) + "\n").encode("utf-8")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
            os.fsync(fd)
            self._fd = fd
            return self
        except Exception:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._release_in_process_key()
            raise

    def _release_in_process_key(self) -> None:
        if self._resolved_key is not None:
            with ActiveEvaluationLock._process_guard:
                ActiveEvaluationLock._active_paths.discard(self._resolved_key)
            self._resolved_key = None

    def release(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is not None:
            try:
                os.ftruncate(fd, 0)
            except OSError:
                pass
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        self._release_in_process_key()

    def __enter__(self) -> "ActiveEvaluationLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


def default_evaluation_lock_path(app_id: Union[str, int]) -> Path:
    """Return the shared default single-flight evaluation lock path for an OCR GitHub App ID."""
    normalized = normalize_app_id(app_id)
    return default_ocr_runtime_dir() / f"sciflow-cicd-ocr-eval-{normalized}.lock"


def normalize_client_factory(
    api_base_url: str,
    api_factory: Optional[Callable[[str], Any]],
) -> Callable[[str], Any]:
    """Return api_factory or a PollingGitHubClient factory bound to a validated api_base_url."""
    if api_factory is not None:
        return api_factory
    return default_api_factory(api_base_url, client_cls=PollingGitHubClient)


def _parse_retry_after(value: Optional[str], now_dt: datetime) -> Optional[float]:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (dt.astimezone(timezone.utc) - now_dt.astimezone(timezone.utc)).total_seconds())


def _compute_rate_limit_delay(
    headers: Dict[str, str],
    now_dt: datetime,
    *,
    min_backoff: float,
    max_backoff: float,
) -> Optional[float]:
    retry_after = _parse_retry_after(headers.get("retry-after"), now_dt)
    if retry_after is not None:
        return min(max_backoff, max(min_backoff, retry_after))

    remaining = headers.get("x-ratelimit-remaining")
    if remaining is not None and remaining.strip() == "0":
        reset_raw = headers.get("x-ratelimit-reset")
        if reset_raw:
            try:
                reset_epoch = float(reset_raw.strip())
                delta = max(0.0, reset_epoch - now_dt.timestamp()) + 1.0
                return min(max_backoff, max(min_backoff, delta))
            except ValueError:
                pass
        return min(max_backoff, min_backoff)
    return None


def _is_rate_limit_or_transient_status(status: int, headers: Dict[str, str], body: Any) -> bool:
    if status in TRANSIENT_HTTP_STATUSES:
        return True
    if status == 403:
        if "retry-after" in headers or headers.get("x-ratelimit-remaining") == "0":
            return True
        if isinstance(body, dict) and "rate limit" in str(body.get("message", "")).lower():
            return True
    return False


def _is_transient_api_error(exc: Exception) -> bool:
    if isinstance(exc, OcrHttpError):
        return _is_rate_limit_or_transient_status(exc.status_code, exc.headers, None)
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        return True
    if isinstance(exc, ApiError):
        msg = str(exc)
        for code in (429, 500, 502, 503, 504):
            if f"HTTP {code}" in msg:
                return True
        if "rate limit" in msg.lower():
            return True
        # Network errors from GitHubApi.request don't contain "HTTP <3-digit>"
        if "HTTP " not in msg:
            return True
    return False


def _request_with_backoff(
    client: Any,
    method: str,
    path: str,
    *,
    sleep_fn: Callable[[float], None],
    now_fn: Callable[[], datetime],
    max_retries: int,
    base_backoff_seconds: float,
    max_backoff_seconds: float,
    sleep_delays: List[float],
) -> Tuple[int, Dict[str, str], Any, int, float]:
    retries_used = 0
    for attempt in range(max_retries + 1):
        now_dt = now_fn()
        try:
            status, headers, body = dispatch_client_request(client, method, path)
        except (ApiError, urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            if _is_transient_api_error(exc) and attempt < max_retries:
                err_headers = exc.headers if isinstance(exc, OcrHttpError) else {}
                delay = _compute_rate_limit_delay(
                    err_headers,
                    now_dt,
                    min_backoff=MIN_BACKOFF_SECONDS,
                    max_backoff=max_backoff_seconds,
                )
                if delay is None:
                    delay = min(
                        max_backoff_seconds,
                        max(MIN_BACKOFF_SECONDS, base_backoff_seconds * (2 ** attempt)),
                    )
                sleep_fn(delay)
                sleep_delays.append(delay)
                retries_used += 1
                continue
            if isinstance(exc, OcrHttpError):
                raise
            raise OcrPollerError(f"{method} {path}: {exc}") from None

        if status == 200:
            rate_wait = 0.0
            if headers.get("x-ratelimit-remaining") == "0":
                computed = _compute_rate_limit_delay(
                    headers,
                    now_dt,
                    min_backoff=MIN_BACKOFF_SECONDS,
                    max_backoff=max_backoff_seconds,
                )
                if computed is not None:
                    rate_wait = computed
            return status, headers, body, retries_used, rate_wait

        if _is_rate_limit_or_transient_status(status, headers, body):
            msg = body.get("message", f"HTTP {status}") if isinstance(body, dict) else f"HTTP {status}"
            if attempt < max_retries:
                delay = _compute_rate_limit_delay(
                    headers,
                    now_dt,
                    min_backoff=MIN_BACKOFF_SECONDS,
                    max_backoff=max_backoff_seconds,
                )
                if delay is None:
                    delay = min(
                        max_backoff_seconds,
                        max(MIN_BACKOFF_SECONDS, base_backoff_seconds * (2 ** attempt)),
                    )
                sleep_fn(delay)
                sleep_delays.append(delay)
                retries_used += 1
                continue
            raise OcrHttpError(status, f"{method} {path}: HTTP {status}: {msg}", headers=headers)

        msg = body.get("message", f"HTTP {status}") if isinstance(body, dict) else f"HTTP {status}"
        raise OcrHttpError(status, f"{method} {path}: HTTP {status}: {msg}", headers=headers)

    raise OcrPollerError(f"{method} {path}: exceeded retry budget")


def _fetch_repo_pull_items(
    client: Any,
    repository: str,
    *,
    hydrate_issue_prs: bool = True,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
    max_backoff_seconds: float = MAX_BACKOFF_SECONDS,
    sleep_delays: Optional[List[float]] = None,
) -> Tuple[List[Any], int, float]:
    """Fetch open pull-request items from a repository with bounded retry/rate-limit backoff."""
    recorded_delays = sleep_delays if sleep_delays is not None else []
    all_items: List[Any] = []
    total_retries = 0
    max_rate_wait = 0.0

    for page in range(1, 101):
        path = validate_ocr_api_path(f"/repos/{repository}/pulls?state=open&per_page=100&page={page}")
        _status, _headers, body, retries_used, rate_wait = _request_with_backoff(
            client,
            "GET",
            path,
            sleep_fn=sleep_fn,
            now_fn=now_fn,
            max_retries=max_retries,
            base_backoff_seconds=base_backoff_seconds,
            max_backoff_seconds=max_backoff_seconds,
            sleep_delays=recorded_delays,
        )
        total_retries += retries_used
        max_rate_wait = max(max_rate_wait, rate_wait)
        if not isinstance(body, list):
            raise OcrPollerError(f"GET {path}: expected list of pull requests")
        all_items.extend(body)
        if len(body) < 100:
            break
        if rate_wait > 0:
            sleep_fn(rate_wait)
            recorded_delays.append(rate_wait)
            max_rate_wait = 0.0

    if not hydrate_issue_prs:
        return all_items, total_retries, max_rate_wait

    hydrated: List[Any] = []
    for item in all_items:
        if (
            isinstance(item, dict)
            and is_pull_request_item(item)
            and not (isinstance(item.get("head"), dict) and isinstance(item.get("base"), dict))
            and isinstance(item.get("number"), int)
        ):
            detail_path = f"/repos/{repository}/pulls/{item['number']}"
            try:
                d_status, _d_headers, d_body, d_retries, d_wait = _request_with_backoff(
                    client,
                    "GET",
                    detail_path,
                    sleep_fn=sleep_fn,
                    now_fn=now_fn,
                    max_retries=max_retries,
                    base_backoff_seconds=base_backoff_seconds,
                    max_backoff_seconds=max_backoff_seconds,
                    sleep_delays=recorded_delays,
                )
                total_retries += d_retries
                max_rate_wait = max(max_rate_wait, d_wait)
                if d_status == 200 and isinstance(d_body, dict):
                    hydrated.append(d_body)
                    continue
            except OcrHttpError:
                pass
        hydrated.append(item)
    return hydrated, total_retries, max_rate_wait


def discover_repository_pull_requests(
    client: Any,
    repository: str,
    *,
    hydrate_issue_prs: bool = True,
) -> List[Dict[str, Any]]:
    """Fetch open pull requests for a repository (filtering out issue-only items, preserving malformed PRs)."""
    items, _retries, _wait = _fetch_repo_pull_items(
        client,
        repository,
        hydrate_issue_prs=hydrate_issue_prs,
    )
    discovered: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if "pull_request" in item and item.get("pull_request") is None:
            continue
        html_url = item.get("html_url")
        if isinstance(html_url, str) and "/issues/" in html_url and not isinstance(item.get("pull_request"), dict):
            continue
        if is_pull_request_item(item) or "head" in item or "base" in item:
            discovered.append(item)
    return discovered


def _to_candidate(
    pr: Dict[str, Any],
    repository: str,
    installation_id: int,
    reason: str,
) -> PullRequestCandidate:
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    user = pr.get("user") if isinstance(pr.get("user"), dict) else {}
    return PullRequestCandidate(
        repository=repository,
        installation_id=installation_id,
        number=int(pr["number"]),
        title=str(pr.get("title") or ""),
        head_sha=str(head.get("sha") or ""),
        head_ref=str(head.get("ref") or ""),
        base_ref=str(base.get("ref") or ""),
        head_repo=str(head_repo.get("full_name") or ""),
        base_repo=str(base_repo.get("full_name") or ""),
        author_login=str(user.get("login") or ""),
        author_association=str(pr.get("author_association") or ""),
        labels=_extract_label_names(pr),
        draft=bool(pr.get("draft", False)),
        eligibility_reason=reason,
        html_url=str(pr.get("html_url") or ""),
    )


def clamp_poll_interval(interval_seconds: float) -> float:
    """Clamp polling interval seconds to the bounded [MIN_POLL_INTERVAL_SECONDS, MAX_POLL_INTERVAL_SECONDS] range."""
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float)):
        raise OcrPollerError("interval_seconds must be a finite non-negative number")
    val = float(interval_seconds)
    if not math.isfinite(val) or val < 0:
        raise OcrPollerError("interval_seconds must be a finite non-negative number")
    return min(MAX_POLL_INTERVAL_SECONDS, max(MIN_POLL_INTERVAL_SECONDS, val))


def _load_rotation_state(state_path: Optional[Union[str, Path]]) -> Dict[str, Any]:
    return load_json_state(
        state_path,
        error_cls=OcrPollerError,
        label="poll state",
    )


def _save_rotation_state(
    state_path: Optional[Union[str, Path]],
    next_index: int,
    next_repository: Optional[str],
) -> None:
    if state_path is None:
        return

    def _apply_rotation(state: Dict[str, Any]) -> Dict[str, Any]:
        state["next_index"] = next_index
        state["next_repository"] = next_repository
        return state

    update_json_state(
        state_path,
        _apply_rotation,
        error_cls=OcrPollerError,
        label="poll state",
    )


def _resolve_start_index(
    eligible_repositories: List[str],
    cursor_index: Optional[int],
    state_data: Dict[str, Any],
) -> int:
    total = len(eligible_repositories)
    if total == 0:
        return 0
    if cursor_index is not None:
        if isinstance(cursor_index, bool) or not isinstance(cursor_index, int) or cursor_index < 0:
            raise OcrPollerError("cursor_index must be a non-negative integer")
        return cursor_index % total
    saved_repo = state_data.get("next_repository")
    if isinstance(saved_repo, str) and saved_repo.strip():
        saved_lower = saved_repo.strip().lower()
        for idx, repo in enumerate(eligible_repositories):
            if repo.lower() == saved_lower:
                return idx
    saved_idx = state_data.get("next_index", 0)
    if isinstance(saved_idx, int) and not isinstance(saved_idx, bool) and saved_idx >= 0:
        return saved_idx % total
    return 0


def poll_once(
    *,
    app_config: Optional[OcrAppConfig] = None,
    eligibility_config: Optional[PrEligibilityConfig] = None,
    app_id: Optional[Union[str, int]] = None,
    private_key_path: Optional[Union[str, Path]] = None,
    allowlist: Sequence[str] = (),
    admin_app_ids: Sequence[Union[str, int]] = (),
    api_base_url: str = "https://api.github.com",
    api_factory: Optional[Callable[[str], Any]] = None,
    signer: Optional[Callable[[bytes], bytes]] = None,
    eligibility_mode: str = "either",
    eligible_label: str = DEFAULT_ELIGIBLE_LABEL,
    maintainers: Sequence[str] = (),
    include_drafts: bool = False,
    cursor_index: Optional[int] = None,
    state_path: Optional[Union[str, Path]] = None,
    lock_path: Optional[Union[str, Path]] = None,
    evaluator: Optional[Callable[[PullRequestCandidate], Any]] = None,
    max_repos_per_cycle: Optional[int] = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
    max_backoff_seconds: float = MAX_BACKOFF_SECONDS,
    sleep_fn: Optional[Callable[[float], None]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    _token_manager: Optional[ScopedInstallationTokenManager] = None,
) -> PollCycleResult:
    """Poll allowlisted & installed repositories once with fair rotation and return eligible PR candidates."""
    app_cfg = resolve_app_config(
        app_config,
        app_id=app_id,
        private_key_path=private_key_path,
        allowlist=allowlist,
        admin_app_ids=admin_app_ids,
        api_base_url=api_base_url,
        api_factory=api_factory,
        signer=signer,
    )
    elig_cfg = resolve_eligibility_config(
        eligibility_config,
        eligibility_mode=eligibility_mode,
        eligible_label=eligible_label,
        maintainers=maintainers,
        include_drafts=include_drafts,
    )
    if elig_cfg.eligibility_mode not in VALID_ELIGIBILITY_MODES:
        raise OcrPollerError(
            f"Unsupported eligibility_mode {elig_cfg.eligibility_mode!r}; expected one of {sorted(VALID_ELIGIBILITY_MODES)}"
        )
    if max_repos_per_cycle is not None:
        if isinstance(max_repos_per_cycle, bool) or not isinstance(max_repos_per_cycle, int) or max_repos_per_cycle <= 0:
            raise OcrPollerError("max_repos_per_cycle must be a positive integer")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise OcrPollerError("max_retries must be a non-negative integer")
    if lock_path is not None:
        validate_lock_path(lock_path, error_cls=OcrPollerError)

    sleeper = sleep_fn if sleep_fn is not None else time.sleep
    clock = now_fn if now_fn is not None else (lambda: datetime.now(timezone.utc))
    bounded_max_backoff = max(MIN_BACKOFF_SECONDS, float(max_backoff_seconds))
    bounded_base_backoff = max(MIN_BACKOFF_SECONDS, min(bounded_max_backoff, float(base_backoff_seconds)))

    state_data = _load_rotation_state(state_path)

    client_factory = normalize_client_factory(app_cfg.api_base_url, app_cfg.api_factory)
    effective_app_cfg = replace(app_cfg, api_factory=client_factory)
    preflight: AppPreflightResult = preflight_ocr_app(app_config=effective_app_cfg)
    excluded = [asdict(dec) for dec in preflight.excluded_repositories]
    if not preflight.passed or not preflight.eligible_repositories:
        return PollCycleResult(
            passed=False,
            eligible_repositories=list(preflight.eligible_repositories),
            excluded_repositories=excluded,
            polled_repositories=[],
            candidates=[],
            errors=list(preflight.errors) or ["No allowlisted repositories are eligible for OCR polling."],
        )

    eligible_repos = list(preflight.eligible_repositories)
    start_idx = _resolve_start_index(eligible_repos, cursor_index, state_data)
    rotated_repos = eligible_repos[start_idx:] + eligible_repos[:start_idx]
    repos_to_poll = rotated_repos[:max_repos_per_cycle] if max_repos_per_cycle is not None else rotated_repos

    decision_by_repo = {dec.repository: dec for dec in preflight.decisions if dec.eligible}
    token_manager = _token_manager or effective_app_cfg.build_token_manager(now_fn=clock)

    polled_repositories: List[str] = []
    all_candidates: List[PullRequestCandidate] = []
    repo_records: List[RepositoryPollRecord] = []
    sleep_delays: List[float] = []
    secrets_to_redact: List[str] = []
    cycle_errors: List[str] = []

    for repo_pos, repo in enumerate(repos_to_poll):
        decision = decision_by_repo[repo]
        installation_id = int(decision.installation_id)
        polled_repositories.append(repo)
        rec = RepositoryPollRecord(repository=repo, installation_id=installation_id)
        try:
            scoped_token = token_manager.get_token(repo, installation_id=installation_id)
            secrets_to_redact.append(scoped_token.token)
            repo_client = client_factory(scoped_token.token)
            items, retries_used, rate_wait = _fetch_repo_pull_items(
                repo_client,
                repo,
                sleep_fn=sleeper,
                now_fn=clock,
                max_retries=max_retries,
                base_backoff_seconds=bounded_base_backoff,
                max_backoff_seconds=bounded_max_backoff,
                sleep_delays=sleep_delays,
            )
            rec.retries = retries_used
            rec.rate_limit_wait_seconds = rate_wait
            rec.raw_items_seen = len(items)
            for item in items:
                if not is_pull_request_item(item):
                    rec.issues_filtered_out += 1
                    continue
                rec.pull_requests_seen += 1
                eligible, reason = evaluate_pr_eligibility(
                    item,
                    repo,
                    eligibility_config=elig_cfg,
                )
                if eligible:
                    candidate = _to_candidate(item, repo, installation_id, reason)
                    rec.eligible_candidates.append(candidate)
                    all_candidates.append(candidate)

            if rate_wait > 0 and repo_pos < len(repos_to_poll) - 1:
                sleeper(rate_wait)
                sleep_delays.append(rate_wait)
        except (OcrHttpError, OcrPollerError, OcrAppAuthError, ApiError, OSError) as exc:
            safe_err = redact_secrets(f"Failed polling '{repo}': {exc}", secrets_to_redact)
            rec.errors.append(safe_err)
            cycle_errors.append(safe_err)
        repo_records.append(rec)

    selected = all_candidates[0] if all_candidates else None
    evaluation_ran = False
    evaluation_skipped_busy = False
    evaluation_result: Any = None

    if selected is not None and evaluator is not None:
        effective_lock = Path(lock_path) if lock_path is not None else default_evaluation_lock_path(app_cfg.app_id)
        try:
            with ActiveEvaluationLock(
                effective_lock,
                repository=selected.repository,
                pr_number=selected.number,
                head_sha=selected.head_sha,
            ):
                evaluation_result = evaluator(selected)
                evaluation_ran = True
        except SingleFlightConflictError:
            evaluation_skipped_busy = True

    repo_to_idx = {repo: idx for idx, repo in enumerate(eligible_repos)}
    if selected is not None:
        next_cursor_index = (repo_to_idx[selected.repository] + 1) % len(eligible_repos)
    elif max_repos_per_cycle is not None and len(polled_repositories) < len(eligible_repos):
        next_cursor_index = (start_idx + len(polled_repositories)) % len(eligible_repos)
    else:
        next_cursor_index = (start_idx + 1) % len(eligible_repos)
    next_cursor_repo = eligible_repos[next_cursor_index]
    _save_rotation_state(state_path, next_cursor_index, next_cursor_repo)

    any_repo_succeeded = any(not rec.errors for rec in repo_records)
    return PollCycleResult(
        passed=any_repo_succeeded,
        eligible_repositories=eligible_repos,
        excluded_repositories=excluded,
        polled_repositories=polled_repositories,
        candidates=all_candidates,
        selected_candidate=selected,
        evaluation_ran=evaluation_ran,
        evaluation_skipped_busy=evaluation_skipped_busy,
        evaluation_result=evaluation_result,
        next_cursor_index=next_cursor_index,
        next_cursor_repository=next_cursor_repo,
        sleep_delays=sleep_delays,
        repo_records=repo_records,
        errors=cycle_errors,
    )


def poll_repositories(
    *,
    app_config: Optional[OcrAppConfig] = None,
    eligibility_config: Optional[PrEligibilityConfig] = None,
    app_id: Optional[Union[str, int]] = None,
    private_key_path: Optional[Union[str, Path]] = None,
    allowlist: Sequence[str] = (),
    admin_app_ids: Sequence[Union[str, int]] = (),
    api_base_url: str = "https://api.github.com",
    api_factory: Optional[Callable[[str], Any]] = None,
    signer: Optional[Callable[[bytes], bytes]] = None,
    eligibility_mode: str = "either",
    eligible_label: str = DEFAULT_ELIGIBLE_LABEL,
    maintainers: Sequence[str] = (),
    include_drafts: bool = False,
    cursor_index: Optional[int] = None,
    state_path: Optional[Union[str, Path]] = None,
    lock_path: Optional[Union[str, Path]] = None,
    evaluator: Optional[Callable[[PullRequestCandidate], Any]] = None,
    max_repos_per_cycle: Optional[int] = None,
    max_cycles: int = 1,
    interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
    max_backoff_seconds: float = MAX_BACKOFF_SECONDS,
    sleep_fn: Optional[Callable[[float], None]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> List[PollCycleResult]:
    """Run one or more bounded polling cycles with fair repository rotation and rate-limit backoff."""
    if isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles <= 0:
        raise OcrPollerError("max_cycles must be a positive integer")
    app_cfg = resolve_app_config(
        app_config,
        app_id=app_id,
        private_key_path=private_key_path,
        allowlist=allowlist,
        admin_app_ids=admin_app_ids,
        api_base_url=api_base_url,
        api_factory=api_factory,
        signer=signer,
    )
    elig_cfg = resolve_eligibility_config(
        eligibility_config,
        eligibility_mode=eligibility_mode,
        eligible_label=eligible_label,
        maintainers=maintainers,
        include_drafts=include_drafts,
    )
    bounded_interval = clamp_poll_interval(interval_seconds)
    sleeper = sleep_fn if sleep_fn is not None else time.sleep
    clock = now_fn if now_fn is not None else (lambda: datetime.now(timezone.utc))
    client_factory = normalize_client_factory(app_cfg.api_base_url, app_cfg.api_factory)
    effective_app_cfg = replace(app_cfg, api_factory=client_factory)
    token_manager = effective_app_cfg.build_token_manager(now_fn=clock)

    results: List[PollCycleResult] = []
    next_cursor = cursor_index
    for cycle_idx in range(max_cycles):
        result = poll_once(
            app_config=effective_app_cfg,
            eligibility_config=elig_cfg,
            cursor_index=next_cursor if state_path is None else None,
            state_path=state_path,
            lock_path=lock_path,
            evaluator=evaluator,
            max_repos_per_cycle=max_repos_per_cycle,
            max_retries=max_retries,
            base_backoff_seconds=base_backoff_seconds,
            max_backoff_seconds=max_backoff_seconds,
            sleep_fn=sleeper,
            now_fn=clock,
            _token_manager=token_manager,
        )
        results.append(result)
        next_cursor = result.next_cursor_index
        if cycle_idx < max_cycles - 1:
            if not result.eligible_repositories and not result.polled_repositories:
                break
            max_rate_wait = max((r.rate_limit_wait_seconds for r in result.repo_records), default=0.0)
            wait_seconds = max(bounded_interval, min(float(max_backoff_seconds), max_rate_wait))
            sleeper(wait_seconds)
            result.sleep_delays.append(wait_seconds)
    return results


def format_poll_report(
    result: PollCycleResult,
    *,
    extra_secrets: Sequence[str] = (),
) -> str:
    """Format a secret-free human-readable report for an OCR repository polling cycle."""
    lines: List[str] = [
        "============================================================",
        "sciflow-cicd OCR: Repository Pull Request Poll",
        "============================================================",
        f"Status: {'PASS' if result.passed else 'FAIL'}",
        f"Eligible repositories: {', '.join(result.eligible_repositories) or '(none)'}",
        f"Polled repositories: {', '.join(result.polled_repositories) or '(none)'}",
        f"Next cursor repository: {result.next_cursor_repository or '(none)'}",
    ]
    if result.candidates:
        lines.append(f"Eligible PR candidates ({len(result.candidates)}):")
        for cand in result.candidates:
            lines.append(
                f"  - {cand.repository}#{cand.number} [{cand.eligibility_reason}] "
                f"sha={cand.head_sha[:12]} author={cand.author_login}: {cand.title}"
            )
    else:
        lines.append("Eligible PR candidates (0): none found")

    if result.evaluation_ran:
        lines.append("Evaluation: ran under single-flight lock")
    elif result.evaluation_skipped_busy:
        lines.append("Evaluation: skipped (single-flight lock busy)")

    if result.errors:
        lines.append("Errors:")
        for err in result.errors:
            lines.append(f"  [x] {err}")
    lines.append("============================================================")
    return redact_secrets("\n".join(lines) + "\n", extra_secrets)


def run_ocr_poll(
    *,
    app_config: Optional[OcrAppConfig] = None,
    eligibility_config: Optional[PrEligibilityConfig] = None,
    app_id: Optional[Union[str, int]] = None,
    private_key_path: Optional[Union[str, Path]] = None,
    allowlist: Sequence[str] = (),
    admin_app_ids: Sequence[Union[str, int]] = (),
    api_base_url: str = "https://api.github.com",
    eligibility_mode: str = "either",
    eligible_label: str = DEFAULT_ELIGIBLE_LABEL,
    maintainers: Sequence[str] = (),
    include_drafts: bool = False,
    interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_cycles: int = 1,
    max_repos_per_cycle: Optional[int] = None,
    cursor_index: Optional[int] = None,
    state_path: Optional[Union[str, Path]] = None,
    lock_path: Optional[Union[str, Path]] = None,
    env: Optional[Mapping[str, str]] = None,
    json_output: bool = False,
    out_stream: Optional[TextIO] = None,
    err_stream: Optional[TextIO] = None,
    api_factory: Optional[Callable[[str], Any]] = None,
    signer: Optional[Callable[[bytes], bytes]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> int:
    """Run OCR pull request polling and write formatted or JSON output."""
    out_s = out_stream if out_stream is not None else sys.stdout
    err_s = err_stream if err_stream is not None else sys.stderr
    environ = env if env is not None else os.environ
    secrets_to_redact = collect_env_secrets(environ)

    try:
        effective_app = resolve_app_config(
            app_config,
            app_id=app_id,
            private_key_path=private_key_path,
            allowlist=allowlist,
            admin_app_ids=admin_app_ids,
            api_base_url=api_base_url,
            api_factory=api_factory,
            signer=signer,
            expand_allowlist=True,
            merge_env_admin_ids=True,
            env=environ,
        )
        effective_elig = resolve_eligibility_config(
            eligibility_config,
            eligibility_mode=eligibility_mode,
            eligible_label=eligible_label,
            maintainers=maintainers,
            include_drafts=include_drafts,
            expand_maintainers=True,
        )
        cycles = poll_repositories(
            app_config=effective_app,
            eligibility_config=effective_elig,
            cursor_index=cursor_index,
            state_path=state_path,
            lock_path=lock_path,
            max_repos_per_cycle=max_repos_per_cycle,
            max_cycles=max_cycles,
            interval_seconds=interval_seconds,
            sleep_fn=sleep_fn,
            now_fn=now_fn,
        )
    except (OcrPollerError, OcrAppAuthError) as exc:
        safe_msg = redact_secrets(str(exc), secrets_to_redact)
        cycles = [
            PollCycleResult(
                passed=False,
                eligible_repositories=[],
                excluded_repositories=[],
                polled_repositories=[],
                candidates=[],
                errors=[safe_msg],
            )
        ]

    passed = bool(cycles) and all(c.passed for c in cycles)
    if json_output:
        payload = {
            "passed": passed,
            "cycles": [c.to_dict(extra_secrets=secrets_to_redact) for c in cycles],
            "errors": [err for c in cycles for err in c.errors],
        }
        out_s.write(redact_secrets(json.dumps(payload, indent=2), secrets_to_redact) + "\n")
    else:
        for cycle in cycles:
            out_s.write(format_poll_report(cycle, extra_secrets=secrets_to_redact))
        if not passed:
            all_errs = [err for c in cycles for err in c.errors]
            if all_errs:
                err_s.write(redact_secrets("\n".join(all_errs) + "\n", secrets_to_redact))

    return 0 if passed else 1
