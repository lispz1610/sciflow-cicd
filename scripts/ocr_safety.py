"""Fail-closed fork safety boundary and local-execution guards for automated OCR."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TextIO, Tuple, Union

try:
    from scripts.configure_github_repo import ApiError
except ImportError:
    from configure_github_repo import ApiError

try:
    from scripts.ocr_app_auth import (
        AppPreflightResult,
        OcrAppAuthError,
        OcrAppConfig,
        REPO_PATTERN,
        client_request,
        collect_env_secrets,
        preflight_ocr_app,
        redact_secrets,
        resolve_app_config,
        split_csv_values,
    )
except ImportError:
    from ocr_app_auth import (
        AppPreflightResult,
        OcrAppAuthError,
        OcrAppConfig,
        REPO_PATTERN,
        client_request,
        collect_env_secrets,
        preflight_ocr_app,
        redact_secrets,
        resolve_app_config,
        split_csv_values,
    )


LOCAL_DIRECT_EXECUTION_MODE = "local_direct"
_RUNNER_API_PATH_RE = re.compile(r"/actions/runners(?:/|$)", re.IGNORECASE)
_SELF_HOSTED_ENV_TOKEN_KEYS = (
    "RUNNER_REGISTRATION_TOKEN",
    "ACTIONS_RUNNER_REGISTRATION_TOKEN",
    "ACTIONS_RUNNER_INPUT_TOKEN",
    "GITHUB_RUNNER_TOKEN",
    "ACTIONS_RUNNER_TOKEN",
)
_SELF_HOSTED_ENV_MODE_KEYS = ("OCR_EXECUTION_MODE", "OCR_RUNNER_MODE")
_SELF_HOSTED_CONFIG_TOKEN_KEYS = ("registration_token", "runner_registration_token", "jitconfig")
_SELF_HOSTED_CONFIG_URL_KEYS = ("runner_registration_url", "registration_url")


class OcrSafetyError(RuntimeError):
    """Raised when OCR safety validation or boundary enforcement fails."""


class SelfHostedRunnerError(OcrSafetyError):
    """Raised when self-hosted Actions runner registration or execution is detected."""


@dataclass(frozen=True)
class RunnerBoundaryStatus:
    """Preflight boundary assertion confirming OCR operates without self-hosted Actions runners."""

    passed: bool
    execution_mode: str
    self_hosted_runner_detected: bool
    violations: Tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable, secret-redacted dictionary representation."""
        return {
            "passed": self.passed,
            "execution_mode": self.execution_mode,
            "self_hosted_runner_detected": self.self_hosted_runner_detected,
            "violations": [redact_secrets(v) for v in self.violations],
            "detail": redact_secrets(self.detail),
        }


@dataclass(frozen=True)
class ForkSafetyDecision:
    """Structured allow/skip decision for a pull request at the fork safety boundary."""

    allowed: bool
    reason: str
    expected_repository: str
    pr_number: Optional[int] = None
    head_repo: Optional[str] = None
    base_repo: Optional[str] = None
    head_repo_id: Optional[int] = None
    base_repo_id: Optional[int] = None
    head_fork: Optional[bool] = None
    head_sha: Optional[str] = None
    author_login: Optional[str] = None
    author_type: Optional[str] = None
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable, secret-redacted dictionary representation."""
        raw = asdict(self)
        raw["detail"] = redact_secrets(self.detail)
        return raw


def _extract_pr_number(pr: Any) -> Optional[int]:
    if not isinstance(pr, dict):
        return None
    num = pr.get("number")
    if isinstance(num, int) and not isinstance(num, bool) and num > 0:
        return num
    return None


def _extract_author_info(pr: Any) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(pr, dict):
        return None, None
    user = pr.get("user")
    if not isinstance(user, dict):
        return None, None
    login = user.get("login")
    user_type = user.get("type")
    return (
        login.strip() if isinstance(login, str) and login.strip() else None,
        user_type.strip() if isinstance(user_type, str) and user_type.strip() else None,
    )


def _extract_repo_side(
    pr: Dict[str, Any],
    side: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate and extract `head`/`base` repo metadata or return a specific skip reason."""
    raw_ref = pr.get(side)
    if raw_ref is None:
        return None, f"missing_{side}_metadata"
    if not isinstance(raw_ref, dict):
        return None, f"unreadable_{side}_metadata"
    raw_repo = raw_ref.get("repo")
    if raw_repo is None:
        return None, f"missing_{side}_repository"
    if not isinstance(raw_repo, dict):
        return None, f"unreadable_{side}_repository"
    return raw_repo, None


def _parse_repo_id(repo_dict: Dict[str, Any], side: str) -> Tuple[Optional[int], Optional[str]]:
    if "id" not in repo_dict or repo_dict.get("id") is None:
        return None, None
    raw_id = repo_dict.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
        return None, f"unreadable_{side}_repository"
    return raw_id, None


def _parse_repo_full_name(repo_dict: Dict[str, Any], side: str) -> Tuple[Optional[str], Optional[str]]:
    raw_name = repo_dict.get("full_name")
    if raw_name is None or (isinstance(raw_name, str) and not raw_name.strip()):
        return None, f"missing_{side}_repository"
    if not isinstance(raw_name, str):
        return None, f"unreadable_{side}_repository"
    cleaned = raw_name.strip()
    if not REPO_PATTERN.match(cleaned):
        return None, f"unreadable_{side}_repository"
    return cleaned, None


def check_same_repo_origin(
    pr: Any,
    expected_repository: str,
    *,
    allowlist: Optional[Sequence[str]] = None,
) -> ForkSafetyDecision:
    """Enforce a fail-closed same-repository origin check on a pull request payload."""
    expected_clean = expected_repository.strip() if isinstance(expected_repository, str) else ""
    pr_number = _extract_pr_number(pr)
    author_login, author_type = _extract_author_info(pr)
    head_sha: Optional[str] = None
    if isinstance(pr, dict) and isinstance(pr.get("head"), dict):
        raw_sha = pr["head"].get("sha")
        if isinstance(raw_sha, str) and raw_sha.strip():
            head_sha = raw_sha.strip()

    def _deny(
        reason: str,
        detail: str,
        *,
        head_repo: Optional[str] = None,
        base_repo: Optional[str] = None,
        head_repo_id: Optional[int] = None,
        base_repo_id: Optional[int] = None,
        head_fork: Optional[bool] = None,
    ) -> ForkSafetyDecision:
        return ForkSafetyDecision(
            allowed=False,
            reason=reason,
            expected_repository=expected_clean,
            pr_number=pr_number,
            head_repo=head_repo,
            base_repo=base_repo,
            head_repo_id=head_repo_id,
            base_repo_id=base_repo_id,
            head_fork=head_fork,
            head_sha=head_sha,
            author_login=author_login,
            author_type=author_type,
            detail=detail,
        )

    if not expected_clean or not REPO_PATTERN.match(expected_clean):
        return _deny(
            "invalid_expected_repository",
            f"Expected repository {expected_repository!r} is not a valid OWNER/REPO identifier.",
        )

    if allowlist is not None:
        expanded = split_csv_values([str(item) for item in allowlist])
        allowed_lower = {item.lower() for item in expanded if REPO_PATTERN.match(item)}
        if expected_clean.lower() not in allowed_lower:
            return _deny(
                "repository_not_allowlisted",
                f"Repository '{expected_clean}' is not in the explicit OCR allowlist.",
            )

    if not isinstance(pr, dict):
        return _deny(
            "unreadable_pull_request",
            "Pull request payload must be a JSON object.",
        )

    head_repo_dict, head_err = _extract_repo_side(pr, "head")
    if head_err is not None or head_repo_dict is None:
        return _deny(
            head_err or "unreadable_head_repository",
            "Pull request head repository metadata is missing or unreadable.",
        )

    base_repo_dict, base_err = _extract_repo_side(pr, "base")
    if base_err is not None or base_repo_dict is None:
        return _deny(
            base_err or "unreadable_base_repository",
            "Pull request base repository metadata is missing or unreadable.",
        )

    head_full, head_name_err = _parse_repo_full_name(head_repo_dict, "head")
    if head_name_err is not None or head_full is None:
        return _deny(
            head_name_err or "unreadable_head_repository",
            "Pull request head.repo.full_name is missing or invalid.",
        )

    base_full, base_name_err = _parse_repo_full_name(base_repo_dict, "base")
    if base_name_err is not None or base_full is None:
        return _deny(
            base_name_err or "unreadable_base_repository",
            "Pull request base.repo.full_name is missing or invalid.",
            head_repo=head_full,
        )

    raw_fork = head_repo_dict.get("fork")
    if raw_fork is not None and not isinstance(raw_fork, bool):
        return _deny(
            "unreadable_head_repository",
            "Pull request head.repo.fork must be a boolean when present.",
            head_repo=head_full,
            base_repo=base_full,
        )
    head_fork = raw_fork if isinstance(raw_fork, bool) else None

    head_id, head_id_err = _parse_repo_id(head_repo_dict, "head")
    if head_id_err is not None:
        return _deny(
            head_id_err,
            "Pull request head.repo.id must be a positive integer when present.",
            head_repo=head_full,
            base_repo=base_full,
            head_fork=head_fork,
        )

    base_id, base_id_err = _parse_repo_id(base_repo_dict, "base")
    if base_id_err is not None:
        return _deny(
            base_id_err,
            "Pull request base.repo.id must be a positive integer when present.",
            head_repo=head_full,
            base_repo=base_full,
            head_repo_id=head_id,
            head_fork=head_fork,
        )

    if head_fork is True:
        return _deny(
            "fork_pr",
            f"Head repository '{head_full}' is marked as a fork.",
            head_repo=head_full,
            base_repo=base_full,
            head_repo_id=head_id,
            base_repo_id=base_id,
            head_fork=head_fork,
        )

    if head_full.lower() != base_full.lower():
        return _deny(
            "fork_pr",
            f"Cross-repository pull request rejected: head '{head_full}' != base '{base_full}'.",
            head_repo=head_full,
            base_repo=base_full,
            head_repo_id=head_id,
            base_repo_id=base_id,
            head_fork=head_fork,
        )

    if (head_id is None) != (base_id is None):
        return _deny(
            "missing_repository_id",
            "Pull request repository ID metadata is incomplete between head and base.",
            head_repo=head_full,
            base_repo=base_full,
            head_repo_id=head_id,
            base_repo_id=base_id,
            head_fork=head_fork,
        )

    if head_id is not None and base_id is not None and head_id != base_id:
        return _deny(
            "repository_id_mismatch",
            f"Head repository ID ({head_id}) does not match base repository ID ({base_id}).",
            head_repo=head_full,
            base_repo=base_full,
            head_repo_id=head_id,
            base_repo_id=base_id,
            head_fork=head_fork,
        )

    if base_full.lower() != expected_clean.lower():
        return _deny(
            "mismatched_repository",
            f"Pull request repository '{base_full}' does not match expected '{expected_clean}'.",
            head_repo=head_full,
            base_repo=base_full,
            head_repo_id=head_id,
            base_repo_id=base_id,
            head_fork=head_fork,
        )

    return ForkSafetyDecision(
        allowed=True,
        reason="same_repository",
        expected_repository=expected_clean,
        pr_number=pr_number,
        head_repo=head_full,
        base_repo=base_full,
        head_repo_id=head_id,
        base_repo_id=base_id,
        head_fork=head_fork,
        head_sha=head_sha,
        author_login=author_login,
        author_type=author_type,
        detail=f"Verified same-repository origin for '{base_full}'.",
    )


@dataclass(frozen=True)
class GuardedExecutionResult:
    """Outcome of running OCR callbacks behind the fork and runner safety boundary."""

    executed: bool
    decision: ForkSafetyDecision
    runner_boundary: RunnerBoundaryStatus
    result: Any = None


def filter_safe_prs(
    prs: Sequence[Any],
    expected_repository: str,
    *,
    allowlist: Optional[Sequence[str]] = None,
) -> Tuple[List[Dict[str, Any]], List[ForkSafetyDecision], List[ForkSafetyDecision]]:
    """Partition pull requests into safe same-repo payloads and allowed/skipped decisions."""
    safe_prs: List[Dict[str, Any]] = []
    allowed: List[ForkSafetyDecision] = []
    skipped: List[ForkSafetyDecision] = []
    for pr in prs:
        decision = check_same_repo_origin(pr, expected_repository, allowlist=allowlist)
        if decision.allowed:
            safe_prs.append(pr)
            allowed.append(decision)
        else:
            skipped.append(decision)
    return safe_prs, allowed, skipped


def guard_pr_for_ocr(
    pr: Any,
    expected_repository: str,
    *,
    allowlist: Optional[Sequence[str]] = None,
    executor: Optional[Callable[[Dict[str, Any], ForkSafetyDecision], Any]] = None,
    execution_mode: str = LOCAL_DIRECT_EXECUTION_MODE,
    env: Optional[Mapping[str, str]] = None,
    runner_config: Optional[Mapping[str, Any]] = None,
) -> GuardedExecutionResult:
    """Execute OCR review callback only after runner and same-repository origin checks pass."""
    runner_boundary = assert_no_self_hosted_runner(
        execution_mode=execution_mode,
        env=env,
        runner_config=runner_config,
    )
    if not runner_boundary.passed:
        author_login, author_type = _extract_author_info(pr)
        decision = ForkSafetyDecision(
            allowed=False,
            reason="self_hosted_runner_forbidden",
            expected_repository=expected_repository.strip() if isinstance(expected_repository, str) else "",
            pr_number=_extract_pr_number(pr),
            author_login=author_login,
            author_type=author_type,
            detail=runner_boundary.detail,
        )
        return GuardedExecutionResult(
            executed=False,
            decision=decision,
            runner_boundary=runner_boundary,
            result=None,
        )

    decision = check_same_repo_origin(pr, expected_repository, allowlist=allowlist)
    if not decision.allowed:
        return GuardedExecutionResult(
            executed=False,
            decision=decision,
            runner_boundary=runner_boundary,
            result=None,
        )

    outcome = executor(pr, decision) if executor is not None else None
    return GuardedExecutionResult(
        executed=True,
        decision=decision,
        runner_boundary=runner_boundary,
        result=outcome,
    )


def validate_ocr_api_path(path: str) -> str:
    """Reject GitHub Actions runner registration/management API endpoints for OCR."""
    if not isinstance(path, str) or not path.strip():
        raise SelfHostedRunnerError("OCR API path must be a non-empty string.")
    if _RUNNER_API_PATH_RE.search(path):
        raise SelfHostedRunnerError(
            "OCR pipeline is prohibited from accessing GitHub Actions runner registration or management endpoints."
        )
    return path


def _contains_self_hosted_label(raw: Any) -> bool:
    if isinstance(raw, str):
        tokens = [part.strip().lower() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple, set)):
        tokens = [str(part).strip().lower() for part in raw if str(part).strip()]
    else:
        return False
    return any("self-hosted" in tok or "self_hosted" in tok for tok in tokens)


def assert_no_self_hosted_runner(
    *,
    execution_mode: str = LOCAL_DIRECT_EXECUTION_MODE,
    env: Optional[Mapping[str, str]] = None,
    runner_config: Optional[Mapping[str, Any]] = None,
    api_paths: Sequence[str] = (),
    raise_on_failure: bool = False,
) -> RunnerBoundaryStatus:
    """Verify OCR runs in local direct execution mode with zero self-hosted Actions runner signals."""
    environ = env if env is not None else os.environ
    mode_norm = str(execution_mode or "").strip().lower()
    violations: List[str] = []

    if mode_norm != LOCAL_DIRECT_EXECUTION_MODE:
        violations.append(
            f"Unsupported OCR execution mode {execution_mode!r}; only '{LOCAL_DIRECT_EXECUTION_MODE}' is permitted."
        )

    for key in _SELF_HOSTED_ENV_TOKEN_KEYS:
        val = environ.get(key)
        if isinstance(val, str) and val.strip():
            violations.append(f"Forbidden self-hosted runner credential environment variable '{key}' is set.")

    runner_env = str(environ.get("RUNNER_ENVIRONMENT") or "").strip().lower()
    if runner_env == "self-hosted":
        violations.append(
            "Environment indicates execution inside a GitHub Actions self-hosted runner "
            "(RUNNER_ENVIRONMENT=self-hosted)."
        )

    for mode_key in _SELF_HOSTED_ENV_MODE_KEYS:
        val = str(environ.get(mode_key) or "").strip().lower()
        if val and val != LOCAL_DIRECT_EXECUTION_MODE:
            violations.append(f"Environment variable '{mode_key}' requests non-direct runner execution.")

    for label_key in ("RUNNER_LABELS", "OCR_RUNNER_LABELS"):
        if _contains_self_hosted_label(environ.get(label_key)):
            violations.append(f"Environment variable '{label_key}' specifies 'self-hosted' runner labels.")

    if isinstance(runner_config, Mapping):
        if runner_config.get("self_hosted") is True or runner_config.get("self-hosted") is True:
            violations.append("Runner configuration explicitly enables self_hosted execution.")
        for runs_key in ("runs_on", "runs-on"):
            if _contains_self_hosted_label(runner_config.get(runs_key)):
                violations.append(f"Runner configuration '{runs_key}' targets 'self-hosted'.")
        for tok_key in _SELF_HOSTED_CONFIG_TOKEN_KEYS:
            val = runner_config.get(tok_key)
            if isinstance(val, str) and val.strip():
                violations.append(f"Runner configuration provides forbidden '{tok_key}'.")
        for url_key in _SELF_HOSTED_CONFIG_URL_KEYS:
            val = runner_config.get(url_key)
            if isinstance(val, str) and val.strip():
                violations.append(f"Runner configuration provides forbidden '{url_key}'.")

    for path in api_paths:
        try:
            validate_ocr_api_path(path)
        except SelfHostedRunnerError as exc:
            violations.append(str(exc))

    passed = len(violations) == 0
    detail = (
        "Verified local direct OCR execution mode without GitHub self-hosted Actions runner registration or execution."
        if passed
        else "; ".join(violations)
    )
    status = RunnerBoundaryStatus(
        passed=passed,
        execution_mode=mode_norm or str(execution_mode),
        self_hosted_runner_detected=not passed,
        violations=tuple(violations),
        detail=detail,
    )
    if not passed and raise_on_failure:
        raise SelfHostedRunnerError(detail)
    return status


@dataclass
class SafetyCheckResult:
    """Structured result of evaluating OCR runner and fork safety boundaries."""

    passed: bool
    runner_boundary: RunnerBoundaryStatus
    eligible_repositories: List[str]
    excluded_repositories: List[Dict[str, Any]]
    allowed_prs: List[ForkSafetyDecision]
    skipped_prs: List[ForkSafetyDecision]
    errors: List[str] = field(default_factory=list)

    def to_dict(self, extra_secrets: Sequence[str] = ()) -> Dict[str, Any]:
        """Return a JSON-serializable, secret-redacted representation."""
        raw = {
            "passed": self.passed,
            "runner_boundary": self.runner_boundary.to_dict(),
            "eligible_repositories": list(self.eligible_repositories),
            "excluded_repositories": list(self.excluded_repositories),
            "allowed_prs": [dec.to_dict() for dec in self.allowed_prs],
            "skipped_prs": [dec.to_dict() for dec in self.skipped_prs],
            "errors": [redact_secrets(err, extra_secrets) for err in self.errors],
        }
        return json.loads(redact_secrets(json.dumps(raw, default=str), extra_secrets))


def safe_client_request(
    client: Any,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Any]:
    """Validate an OCR API path against forbidden runner endpoints and invoke the client."""
    return client_request(
        client,
        method,
        path,
        payload=payload,
        path_validator=validate_ocr_api_path,
    )


def _default_fetch_repo_prs(client: Any, repository: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for page in range(1, 101):
        path = f"/repos/{repository}/pulls?state=open&per_page=100&page={page}"
        status, body = safe_client_request(client, "GET", path)
        if status != 200 or not isinstance(body, list):
            raise OcrSafetyError(f"GET {path}: expected list of pull requests (HTTP {status}).")
        for item in body:
            if not isinstance(item, dict):
                continue
            html_url = item.get("html_url")
            if isinstance(html_url, str) and "/issues/" in html_url and not isinstance(item.get("pull_request"), dict):
                continue
            items.append(item)
        if len(body) < 100:
            break
    return items


def evaluate_repository_safety(
    *,
    app_config: Optional[OcrAppConfig] = None,
    app_id: Optional[Union[str, int]] = None,
    private_key_path: Optional[Union[str, Path]] = None,
    allowlist: Sequence[str] = (),
    admin_app_ids: Sequence[Union[str, int]] = (),
    api_base_url: str = "https://api.github.com",
    execution_mode: str = LOCAL_DIRECT_EXECUTION_MODE,
    env: Optional[Mapping[str, str]] = None,
    runner_config: Optional[Mapping[str, Any]] = None,
    api_factory: Optional[Callable[[str], Any]] = None,
    signer: Optional[Callable[[bytes], bytes]] = None,
    pr_fetcher: Optional[Callable[[Any, str], List[Dict[str, Any]]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> SafetyCheckResult:
    """Verify runner boundary, scope limited App tokens, and filter PRs via fork safety boundary."""
    runner_boundary = assert_no_self_hosted_runner(
        execution_mode=execution_mode,
        env=env,
        runner_config=runner_config,
    )
    if not runner_boundary.passed:
        return SafetyCheckResult(
            passed=False,
            runner_boundary=runner_boundary,
            eligible_repositories=[],
            excluded_repositories=[],
            allowed_prs=[],
            skipped_prs=[],
            errors=[runner_boundary.detail],
        )

    cfg = resolve_app_config(
        app_config,
        app_id=app_id,
        private_key_path=private_key_path,
        allowlist=allowlist,
        admin_app_ids=admin_app_ids,
        api_base_url=api_base_url,
        api_factory=api_factory,
        signer=signer,
    )
    client_factory = cfg.resolve_client_factory()
    preflight: AppPreflightResult = preflight_ocr_app(
        app_config=replace(cfg, api_factory=client_factory),
    )
    excluded = [asdict(dec) for dec in preflight.excluded_repositories]
    if not preflight.passed or not preflight.eligible_repositories:
        return SafetyCheckResult(
            passed=False,
            runner_boundary=runner_boundary,
            eligible_repositories=list(preflight.eligible_repositories),
            excluded_repositories=excluded,
            allowed_prs=[],
            skipped_prs=[],
            errors=list(preflight.errors) or ["No allowlisted repositories are eligible for OCR safety check."],
        )

    clock = now_fn if now_fn is not None else (lambda: datetime.now(timezone.utc))
    token_manager = cfg.build_token_manager(client_factory=client_factory, now_fn=clock)
    fetcher = pr_fetcher if pr_fetcher is not None else _default_fetch_repo_prs
    decision_by_repo = {dec.repository: dec for dec in preflight.decisions if dec.eligible}

    allowed_prs: List[ForkSafetyDecision] = []
    skipped_prs: List[ForkSafetyDecision] = []
    errors: List[str] = []
    secrets_to_redact: List[str] = []

    for repo in preflight.eligible_repositories:
        dec = decision_by_repo[repo]
        try:
            scoped = token_manager.get_token(repo, installation_id=int(dec.installation_id))
            secrets_to_redact.append(scoped.token)
            repo_client = client_factory(scoped.token)
            prs = fetcher(repo_client, repo)
            _safe, allowed, skipped = filter_safe_prs(
                prs,
                repo,
                allowlist=preflight.eligible_repositories,
            )
            allowed_prs.extend(allowed)
            skipped_prs.extend(skipped)
        except (OcrSafetyError, OcrAppAuthError, ApiError, OSError, ValueError) as exc:
            errors.append(redact_secrets(f"Failed safety check for '{repo}': {exc}", secrets_to_redact))

    return SafetyCheckResult(
        passed=len(errors) == 0,
        runner_boundary=runner_boundary,
        eligible_repositories=list(preflight.eligible_repositories),
        excluded_repositories=excluded,
        allowed_prs=allowed_prs,
        skipped_prs=skipped_prs,
        errors=[redact_secrets(err, secrets_to_redact) for err in errors],
    )


def format_safety_report(
    result: SafetyCheckResult,
    *,
    extra_secrets: Sequence[str] = (),
) -> str:
    """Format a secret-free human-readable report for OCR fork and runner safety checks."""
    lines: List[str] = [
        "============================================================",
        "sciflow-cicd OCR: Fork & Runner Safety Boundary Check",
        "============================================================",
        f"Status: {'PASS' if result.passed else 'FAIL'}",
        (
            f"Runner boundary: {'PASS' if result.runner_boundary.passed else 'FAIL'} "
            f"(mode={result.runner_boundary.execution_mode}, "
            f"self_hosted_detected={result.runner_boundary.self_hosted_runner_detected})"
        ),
        f"Eligible repositories: {', '.join(result.eligible_repositories) or '(none)'}",
        f"Allowed PRs ({len(result.allowed_prs)}):",
    ]
    if result.allowed_prs:
        for dec in result.allowed_prs:
            lines.append(
                f"  [allow] {dec.expected_repository}#{dec.pr_number} "
                f"reason={dec.reason} author={dec.author_login or 'unknown'} ({dec.author_type or 'unknown'})"
            )
    else:
        lines.append("  (none)")

    lines.append(f"Skipped PRs ({len(result.skipped_prs)}):")
    if result.skipped_prs:
        for dec in result.skipped_prs:
            lines.append(
                f"  [skip] {dec.expected_repository}#{dec.pr_number} "
                f"reason={dec.reason}: {dec.detail}"
            )
    else:
        lines.append("  (none)")

    if result.errors:
        lines.append("Errors:")
        for err in result.errors:
            lines.append(f"  [x] {err}")

    lines.append("============================================================")
    return redact_secrets("\n".join(lines) + "\n", extra_secrets)


def run_ocr_safety_check(
    *,
    app_config: Optional[OcrAppConfig] = None,
    app_id: Optional[Union[str, int]] = None,
    private_key_path: Optional[Union[str, Path]] = None,
    allowlist: Sequence[str] = (),
    admin_app_ids: Sequence[Union[str, int]] = (),
    api_base_url: str = "https://api.github.com",
    execution_mode: str = LOCAL_DIRECT_EXECUTION_MODE,
    env: Optional[Mapping[str, str]] = None,
    json_output: bool = False,
    out_stream: Optional[TextIO] = None,
    err_stream: Optional[TextIO] = None,
    api_factory: Optional[Callable[[str], Any]] = None,
    signer: Optional[Callable[[bytes], bytes]] = None,
    pr_fetcher: Optional[Callable[[Any, str], List[Dict[str, Any]]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> int:
    """Run OCR fork & runner safety boundary check and write formatted or JSON output."""
    out_s = out_stream if out_stream is not None else sys.stdout
    err_s = err_stream if err_stream is not None else sys.stderr
    environ = env if env is not None else os.environ

    secrets_to_redact = collect_env_secrets(environ)

    try:
        effective_cfg = resolve_app_config(
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
        result = evaluate_repository_safety(
            app_config=effective_cfg,
            execution_mode=execution_mode,
            env=environ,
            pr_fetcher=pr_fetcher,
            now_fn=now_fn,
        )
    except (OcrSafetyError, OcrAppAuthError) as exc:
        safe_msg = redact_secrets(str(exc), secrets_to_redact)
        result = SafetyCheckResult(
            passed=False,
            runner_boundary=assert_no_self_hosted_runner(execution_mode=execution_mode, env=environ),
            eligible_repositories=[],
            excluded_repositories=[],
            allowed_prs=[],
            skipped_prs=[],
            errors=[safe_msg],
        )

    if json_output:
        out_s.write(json.dumps(result.to_dict(extra_secrets=secrets_to_redact), indent=2) + "\n")
    else:
        out_s.write(format_safety_report(result, extra_secrets=secrets_to_redact))
        if not result.passed and result.errors:
            err_s.write(redact_secrets("\n".join(result.errors) + "\n", secrets_to_redact))

    return 0 if result.passed else 1
