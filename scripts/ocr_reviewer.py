"""Automated advisory OCR review orchestrator with per-HEAD SHA deduplication."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TextIO, Tuple, Union

try:
    from scripts.configure_github_repo import ApiError
except ImportError:
    from configure_github_repo import ApiError

try:
    from scripts.ocr_app_auth import (
        OcrAppAuthError,
        OcrAppConfig,
        ScopedInstallationTokenManager,
        collect_env_secrets,
        redact_secrets,
        resolve_app_config,
    )
except ImportError:
    from ocr_app_auth import (
        OcrAppAuthError,
        OcrAppConfig,
        ScopedInstallationTokenManager,
        collect_env_secrets,
        redact_secrets,
        resolve_app_config,
    )

try:
    from scripts.ocr_poller import (
        ActiveEvaluationLock,
        DEFAULT_ELIGIBLE_LABEL,
        DEFAULT_POLL_INTERVAL_SECONDS,
        OcrPollerError,
        PrEligibilityConfig,
        PullRequestCandidate,
        SingleFlightConflictError,
        VALID_ELIGIBILITY_MODES,
        clamp_poll_interval,
        default_evaluation_lock_path,
        normalize_client_factory,
        poll_once,
        resolve_eligibility_config,
    )
except ImportError:
    from ocr_poller import (
        ActiveEvaluationLock,
        DEFAULT_ELIGIBLE_LABEL,
        DEFAULT_POLL_INTERVAL_SECONDS,
        OcrPollerError,
        PrEligibilityConfig,
        PullRequestCandidate,
        SingleFlightConflictError,
        VALID_ELIGIBILITY_MODES,
        clamp_poll_interval,
        default_evaluation_lock_path,
        normalize_client_factory,
        poll_once,
        resolve_eligibility_config,
    )

try:
    from scripts.ocr_preflight import (
        OcrPreflightError,
        OcrPreflightResult,
        preflight_ocr_runtime,
    )
except ImportError:
    from ocr_preflight import (
        OcrPreflightError,
        OcrPreflightResult,
        preflight_ocr_runtime,
    )

try:
    from scripts.ocr_publisher import (
        VALID_REVIEW_MODES,
        advisory_review_marker,
        find_remote_advisory_marker,
        format_advisory_review_body,
        publish_advisory_review,
        validate_advisory_publish_request,
    )
except ImportError:
    from ocr_publisher import (
        VALID_REVIEW_MODES,
        advisory_review_marker,
        find_remote_advisory_marker,
        format_advisory_review_body,
        publish_advisory_review,
        validate_advisory_publish_request,
    )

try:
    from scripts.ocr_runner import (
        DEFAULT_OCR_TIMEOUT_SECONDS,
        OcrExecutionError,
        OcrReviewError,
        execute_ocr_for_pr,
        extract_head_sha,
        fetch_live_pull_request,
    )
except ImportError:
    from ocr_runner import (
        DEFAULT_OCR_TIMEOUT_SECONDS,
        OcrExecutionError,
        OcrReviewError,
        execute_ocr_for_pr,
        extract_head_sha,
        fetch_live_pull_request,
    )

try:
    from scripts.ocr_safety import (
        LOCAL_DIRECT_EXECUTION_MODE,
        OcrSafetyError,
        assert_no_self_hosted_runner,
        check_same_repo_origin,
        guard_pr_for_ocr,
    )
except ImportError:
    from ocr_safety import (
        LOCAL_DIRECT_EXECUTION_MODE,
        OcrSafetyError,
        assert_no_self_hosted_runner,
        check_same_repo_origin,
        guard_pr_for_ocr,
    )

try:
    from scripts.ocr_state import (
        OcrStateError,
        default_review_state_path,
        is_head_reviewed,
        load_review_state as _load_review_state,
        record_head_changed_mid_run,
        record_head_reviewed,
        record_retryable_failure,
        save_review_state as _save_review_state,
        update_review_cursor,
        validate_lock_path,
    )
except ImportError:
    from ocr_state import (
        OcrStateError,
        default_review_state_path,
        is_head_reviewed,
        load_review_state as _load_review_state,
        record_head_changed_mid_run,
        record_head_reviewed,
        record_retryable_failure,
        save_review_state as _save_review_state,
        update_review_cursor,
        validate_lock_path,
    )


__all__ = [
    "CandidateReviewOutcome",
    "OcrExecutionError",
    "OcrReviewConfig",
    "OcrReviewError",
    "OcrReviewPassResult",
    "advisory_review_marker",
    "format_ocr_review_report",
    "load_review_state",
    "resolve_review_config",
    "run_ocr_review_cycles",
    "run_ocr_review_pass",
    "run_ocr_reviews",
    "save_review_state",
    "validate_advisory_publish_request",
]


@dataclass(frozen=True)
class OcrReviewConfig:
    """Immutable execution and publication options for advisory OCR review runs."""

    state_path: Optional[Union[str, Path]] = None
    lock_path: Optional[Union[str, Path]] = None
    ocr_bin: Optional[str] = None
    review_mode: str = "pr_review"
    max_reviews: Optional[int] = None
    ocr_timeout_seconds: float = DEFAULT_OCR_TIMEOUT_SECONDS
    interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    max_cycles: int = 1


def resolve_review_config(
    review_config: Optional[OcrReviewConfig] = None,
    *,
    state_path: Optional[Union[str, Path]] = None,
    lock_path: Optional[Union[str, Path]] = None,
    ocr_bin: Optional[str] = None,
    review_mode: str = "pr_review",
    max_reviews: Optional[int] = None,
    ocr_timeout_seconds: float = DEFAULT_OCR_TIMEOUT_SECONDS,
    interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_cycles: int = 1,
) -> OcrReviewConfig:
    """Return an explicit OcrReviewConfig or build one from legacy keyword parameters."""
    if review_config is not None:
        return review_config
    return OcrReviewConfig(
        state_path=state_path,
        lock_path=lock_path,
        ocr_bin=ocr_bin,
        review_mode=review_mode,
        max_reviews=max_reviews,
        ocr_timeout_seconds=ocr_timeout_seconds,
        interval_seconds=interval_seconds,
        max_cycles=max_cycles,
    )


@dataclass(frozen=True)
class CandidateReviewOutcome:
    """Result for a single pull request candidate evaluated in an OCR review pass."""

    repository: str
    pr_number: int
    head_sha: str
    outcome: str
    review_mode: str = "pr_review"
    comment_id: Optional[int] = None
    comment_url: Optional[str] = None
    observed_new_head_sha: Optional[str] = None
    attempts: int = 0
    error: Optional[str] = None
    detail: str = ""

    def to_dict(self, extra_secrets: Sequence[str] = ()) -> Dict[str, Any]:
        raw = asdict(self)
        return json.loads(redact_secrets(json.dumps(raw, default=str), extra_secrets))


@dataclass
class OcrReviewPassResult:
    """Structured result of a single-pass automated advisory OCR review run."""

    status: str
    passed: bool
    pending: bool = False
    preflight: Optional[OcrPreflightResult] = None
    outcomes: List[CandidateReviewOutcome] = field(default_factory=list)
    reviews_posted: int = 0
    skipped_already_reviewed: int = 0
    skipped_competing_workflow: int = 0
    skipped_indeterminate_workflow: int = 0
    skipped_unsafe: int = 0
    skipped_busy: bool = False
    head_changed_mid_run: int = 0
    retryable_failures: int = 0
    warnings: List[str] = field(default_factory=list)
    pending_reasons: List[str] = field(default_factory=list)
    instructions: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    next_cursor_index: int = 0
    next_cursor_repository: Optional[str] = None

    def to_dict(self, extra_secrets: Sequence[str] = ()) -> Dict[str, Any]:
        raw = {
            "status": self.status,
            "passed": self.passed,
            "pending": self.pending,
            "preflight": self.preflight.to_dict(extra_secrets=extra_secrets) if self.preflight else None,
            "outcomes": [item.to_dict(extra_secrets=extra_secrets) for item in self.outcomes],
            "reviews_posted": self.reviews_posted,
            "skipped_already_reviewed": self.skipped_already_reviewed,
            "skipped_competing_workflow": self.skipped_competing_workflow,
            "skipped_indeterminate_workflow": self.skipped_indeterminate_workflow,
            "skipped_unsafe": self.skipped_unsafe,
            "skipped_busy": self.skipped_busy,
            "head_changed_mid_run": self.head_changed_mid_run,
            "retryable_failures": self.retryable_failures,
            "warnings": list(self.warnings),
            "pending_reasons": list(self.pending_reasons),
            "instructions": list(self.instructions),
            "errors": list(self.errors),
            "next_cursor_index": self.next_cursor_index,
            "next_cursor_repository": self.next_cursor_repository,
        }
        return json.loads(redact_secrets(json.dumps(raw, default=str), extra_secrets))


def load_review_state(state_path: Optional[Union[str, Path]]) -> Dict[str, Any]:
    """Load durable OCR rotation and per-PR/per-HEAD review state safely without following symlinks."""
    return _load_review_state(state_path, error_cls=OcrReviewError)


def save_review_state(state_path: Optional[Union[str, Path]], state: Dict[str, Any]) -> None:
    """Persist OCR rotation and per-PR/per-HEAD state atomically via symlink-safe os.replace."""
    _save_review_state(state_path, state, error_cls=OcrReviewError)


def _filter_active_repositories(
    preflight: OcrPreflightResult,
    warnings: List[str],
) -> Tuple[List[str], int, int]:
    competing_repos = {wf.repository.lower() for wf in preflight.competing_workflows if wf.present and wf.active}
    indeterminate_by_repo = {
        wf.repository.lower(): wf
        for wf in [*preflight.workflow_checks, *preflight.indeterminate_workflows]
        if wf.indeterminate or wf.error
    }
    verified_safe_repos = {
        wf.repository.lower()
        for wf in preflight.workflow_checks
        if not wf.indeterminate and not wf.error and not (wf.present and wf.active)
    }
    eligible_repos = (
        list(preflight.app_preflight.eligible_repositories)
        if preflight.app_preflight is not None
        else []
    )
    active_repos: List[str] = []
    skipped_competing = 0
    skipped_indeterminate = 0
    for repo in eligible_repos:
        repo_key = repo.lower()
        if repo_key in competing_repos:
            skipped_competing += 1
            warnings.append(
                f"Skipping automated OCR reviews for '{repo}' because a competing Actions OCR workflow is active."
            )
        elif repo_key in indeterminate_by_repo or repo_key not in verified_safe_repos:
            skipped_competing += 1
            skipped_indeterminate += 1
            wf = indeterminate_by_repo.get(repo_key)
            detail = wf.error if wf and wf.error else "competing workflow safety could not be verified"
            warnings.append(
                f"Skipping automated OCR reviews for '{repo}' because competing workflow inspection "
                f"is indeterminate ({detail})."
            )
        else:
            active_repos.append(repo)
    return active_repos, skipped_competing, skipped_indeterminate


def _record_head_changed_outcome(
    *,
    state: Dict[str, Any],
    state_path: Optional[Union[str, Path]],
    repository: str,
    pr_number: int,
    head_sha: str,
    observed_new_head_sha: str,
    review_mode: str,
    detail: str,
    clock: Callable[[], datetime],
) -> CandidateReviewOutcome:
    record_head_changed_mid_run(
        state=state,
        state_path=state_path,
        repository=repository,
        pr_number=pr_number,
        head_sha=head_sha,
        observed_new_head_sha=observed_new_head_sha,
        clock=clock,
        error_cls=OcrReviewError,
    )
    return CandidateReviewOutcome(
        repository=repository,
        pr_number=pr_number,
        head_sha=head_sha,
        outcome="head_changed_mid_run",
        review_mode=review_mode,
        observed_new_head_sha=observed_new_head_sha,
        detail=detail,
    )


def _record_retryable_outcome(
    *,
    state: Dict[str, Any],
    state_path: Optional[Union[str, Path]],
    repository: str,
    pr_number: int,
    head_sha: str,
    review_mode: str,
    error_msg: str,
    clock: Callable[[], datetime],
    secrets_to_redact: Sequence[str],
) -> CandidateReviewOutcome:
    _head_rec, attempts, safe_err = record_retryable_failure(
        state=state,
        state_path=state_path,
        repository=repository,
        pr_number=pr_number,
        head_sha=head_sha,
        review_mode=review_mode,
        error_msg=error_msg,
        clock=clock,
        secrets_to_redact=secrets_to_redact,
        error_cls=OcrReviewError,
    )
    return CandidateReviewOutcome(
        repository=repository,
        pr_number=pr_number,
        head_sha=head_sha,
        outcome="retryable_failure",
        review_mode=review_mode,
        attempts=attempts,
        error=safe_err,
        detail=f"Recorded retryable failure (attempt {attempts}) without marking HEAD reviewed.",
    )


def run_ocr_review_pass(
    *,
    app_config: Optional[OcrAppConfig] = None,
    eligibility_config: Optional[PrEligibilityConfig] = None,
    review_config: Optional[OcrReviewConfig] = None,
    app_id: Optional[Union[str, int]] = None,
    private_key_path: Optional[Union[str, Path]] = None,
    allowlist: Sequence[str] = (),
    admin_app_ids: Sequence[Union[str, int]] = (),
    api_base_url: str = "https://api.github.com",
    state_path: Optional[Union[str, Path]] = None,
    lock_path: Optional[Union[str, Path]] = None,
    ocr_bin: Optional[str] = None,
    eligibility_mode: str = "either",
    eligible_label: str = DEFAULT_ELIGIBLE_LABEL,
    maintainers: Sequence[str] = (),
    include_drafts: bool = False,
    review_mode: str = "pr_review",
    max_reviews: Optional[int] = None,
    ocr_timeout_seconds: float = DEFAULT_OCR_TIMEOUT_SECONDS,
    env: Optional[Mapping[str, str]] = None,
    api_factory: Optional[Callable[[str], Any]] = None,
    signer: Optional[Callable[[bytes], bytes]] = None,
    which_fn: Optional[Callable[[str], Optional[str]]] = None,
    ocr_executor: Optional[Callable[[Dict[str, Any]], Any]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> OcrReviewPassResult:
    """Execute one guarded, deduplicated advisory OCR review pass across allowlisted repositories."""
    environ = env if env is not None else os.environ
    clock = now_fn if now_fn is not None else (lambda: datetime.now(timezone.utc))
    secrets_to_redact = collect_env_secrets(environ)

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
    rev_cfg = resolve_review_config(
        review_config,
        state_path=state_path,
        lock_path=lock_path,
        ocr_bin=ocr_bin,
        review_mode=review_mode,
        max_reviews=max_reviews,
        ocr_timeout_seconds=ocr_timeout_seconds,
    )

    if rev_cfg.review_mode not in VALID_REVIEW_MODES:
        return OcrReviewPassResult(
            status="error",
            passed=False,
            errors=[
                f"Forbidden or unsupported review_mode {rev_cfg.review_mode!r}; "
                f"only strictly advisory modes {sorted(VALID_REVIEW_MODES)} are permitted."
            ],
        )

    if rev_cfg.max_reviews is not None:
        if isinstance(rev_cfg.max_reviews, bool) or not isinstance(rev_cfg.max_reviews, int) or rev_cfg.max_reviews <= 0:
            return OcrReviewPassResult(
                status="error",
                passed=False,
                errors=["max_reviews must be a positive integer."],
            )

    if elig_cfg.eligibility_mode not in VALID_ELIGIBILITY_MODES:
        return OcrReviewPassResult(
            status="error",
            passed=False,
            errors=[
                f"Unsupported eligibility_mode {elig_cfg.eligibility_mode!r}; "
                f"expected one of {sorted(VALID_ELIGIBILITY_MODES)}."
            ],
        )

    runner_boundary = assert_no_self_hosted_runner(
        execution_mode=LOCAL_DIRECT_EXECUTION_MODE,
        env=environ,
        raise_on_failure=False,
    )
    if not runner_boundary.passed:
        return OcrReviewPassResult(
            status="error",
            passed=False,
            errors=[redact_secrets(runner_boundary.detail, secrets_to_redact)],
        )

    try:
        raw_lock = (
            rev_cfg.lock_path
            if rev_cfg.lock_path is not None
            else default_evaluation_lock_path(app_cfg.app_id)
        )
        effective_lock = validate_lock_path(raw_lock, error_cls=OcrReviewError)
    except (OcrReviewError, OcrAppAuthError) as exc:
        return OcrReviewPassResult(
            status="error",
            passed=False,
            errors=[redact_secrets(str(exc), secrets_to_redact)],
        )

    try:
        with ActiveEvaluationLock(effective_lock):
            return _run_locked_review_pass(
                app_cfg=app_cfg,
                elig_cfg=elig_cfg,
                rev_cfg=rev_cfg,
                environ=environ,
                clock=clock,
                secrets_to_redact=secrets_to_redact,
                which_fn=which_fn,
                ocr_executor=ocr_executor,
            )
    except SingleFlightConflictError:
        return OcrReviewPassResult(
            status="skipped_busy",
            passed=True,
            skipped_busy=True,
            warnings=["Single-flight evaluation lock is busy; skipped OCR review pass without modifying state."],
        )
    except (OcrPollerError, OcrReviewError) as exc:
        return OcrReviewPassResult(
            status="error",
            passed=False,
            errors=[redact_secrets(str(exc), secrets_to_redact)],
        )


def _run_locked_review_pass(
    *,
    app_cfg: OcrAppConfig,
    elig_cfg: PrEligibilityConfig,
    rev_cfg: OcrReviewConfig,
    environ: Mapping[str, str],
    clock: Callable[[], datetime],
    secrets_to_redact: List[str],
    which_fn: Optional[Callable[[str], Optional[str]]],
    ocr_executor: Optional[Callable[[Dict[str, Any]], Any]],
) -> OcrReviewPassResult:
    try:
        state = load_review_state(rev_cfg.state_path)
    except OcrReviewError as exc:
        return OcrReviewPassResult(
            status="error",
            passed=False,
            errors=[redact_secrets(str(exc), secrets_to_redact)],
        )

    client_factory = normalize_client_factory(app_cfg.api_base_url, app_cfg.api_factory)
    normalized_app_cfg = replace(
        resolve_app_config(
            app_cfg,
            expand_allowlist=True,
            merge_env_admin_ids=True,
            env=environ,
        ),
        api_factory=client_factory,
    )
    normalized_elig_cfg = resolve_eligibility_config(elig_cfg, expand_maintainers=True)

    try:
        preflight = preflight_ocr_runtime(
            app_config=normalized_app_cfg,
            eligibility_config=normalized_elig_cfg,
            ocr_bin=rev_cfg.ocr_bin,
            env=environ,
            inspect_pull_requests=False,
            which_fn=which_fn,
            now_fn=clock,
        )
    except (OcrPreflightError, OcrAppAuthError, OcrSafetyError) as exc:
        return OcrReviewPassResult(
            status="error",
            passed=False,
            errors=[redact_secrets(str(exc), secrets_to_redact)],
        )

    warnings = list(preflight.warnings)
    if preflight.status == "error":
        return OcrReviewPassResult(
            status="error",
            passed=False,
            preflight=preflight,
            warnings=warnings,
            instructions=list(preflight.instructions),
            errors=list(preflight.errors),
        )

    if preflight.status == "pending_configuration":
        return OcrReviewPassResult(
            status="pending_configuration",
            passed=True,
            pending=True,
            preflight=preflight,
            warnings=warnings,
            pending_reasons=list(preflight.pending_reasons),
            instructions=list(preflight.instructions),
        )

    active_repos, skipped_competing, skipped_indeterminate = _filter_active_repositories(preflight, warnings)
    if not active_repos:
        return OcrReviewPassResult(
            status="skipped_competing_workflow",
            passed=True,
            preflight=preflight,
            skipped_competing_workflow=skipped_competing,
            skipped_indeterminate_workflow=skipped_indeterminate,
            warnings=warnings,
            instructions=list(preflight.instructions),
        )

    saved_cursor = state.get("next_index")
    cursor_arg = (
        saved_cursor
        if isinstance(saved_cursor, int) and not isinstance(saved_cursor, bool) and saved_cursor >= 0
        else None
    )

    try:
        poll_res = poll_once(
            app_config=normalized_app_cfg.with_allowlist(active_repos),
            eligibility_config=normalized_elig_cfg,
            cursor_index=cursor_arg,
            state_path=None,
            lock_path=None,
            evaluator=None,
            now_fn=clock,
        )
    except (OcrPollerError, OcrAppAuthError, OcrSafetyError) as exc:
        return OcrReviewPassResult(
            status="error",
            passed=False,
            preflight=preflight,
            skipped_competing_workflow=skipped_competing,
            skipped_indeterminate_workflow=skipped_indeterminate,
            warnings=warnings,
            instructions=list(preflight.instructions),
            errors=[redact_secrets(str(exc), secrets_to_redact)],
        )

    update_review_cursor(
        rev_cfg.state_path,
        state,
        next_index=poll_res.next_cursor_index,
        next_repository=poll_res.next_cursor_repository,
        error_cls=OcrReviewError,
    )

    if not poll_res.passed and not poll_res.candidates:
        return OcrReviewPassResult(
            status="error",
            passed=False,
            preflight=preflight,
            skipped_competing_workflow=skipped_competing,
            skipped_indeterminate_workflow=skipped_indeterminate,
            warnings=warnings,
            instructions=list(preflight.instructions),
            errors=list(poll_res.errors),
            next_cursor_index=poll_res.next_cursor_index,
            next_cursor_repository=poll_res.next_cursor_repository,
        )

    outcomes: List[CandidateReviewOutcome] = []
    pending_candidates: List[PullRequestCandidate] = []
    skipped_dedup = 0
    for cand in poll_res.candidates:
        if is_head_reviewed(state, cand.repository, cand.number, cand.head_sha):
            skipped_dedup += 1
            outcomes.append(
                CandidateReviewOutcome(
                    repository=cand.repository,
                    pr_number=cand.number,
                    head_sha=cand.head_sha,
                    outcome="skipped_already_reviewed",
                    review_mode=rev_cfg.review_mode,
                    detail="HEAD SHA was already reviewed in durable state.",
                )
            )
        else:
            pending_candidates.append(cand)

    if rev_cfg.max_reviews is not None:
        pending_candidates = pending_candidates[: rev_cfg.max_reviews]

    reviews_posted = 0
    skipped_unsafe = 0
    head_changed_count = 0
    retryable_failures = 0
    errors = list(poll_res.errors)

    if pending_candidates:
        token_manager = normalized_app_cfg.build_token_manager(now_fn=clock)
        ocr_executable = preflight.executable.resolved_path or preflight.executable.requested

        try:
            for cand in pending_candidates:
                outcome = _evaluate_and_publish_candidate(
                    cand=cand,
                    active_repos=active_repos,
                    token_manager=token_manager,
                    client_factory=client_factory,
                    ocr_executable=ocr_executable,
                    ocr_executor=ocr_executor,
                    ocr_timeout_seconds=rev_cfg.ocr_timeout_seconds,
                    review_mode=rev_cfg.review_mode,
                    state=state,
                    state_path=rev_cfg.state_path,
                    environ=environ,
                    clock=clock,
                    secrets_to_redact=secrets_to_redact,
                )
                outcomes.append(outcome)
                if outcome.outcome == "reviewed":
                    reviews_posted += 1
                elif outcome.outcome == "skipped_already_reviewed":
                    skipped_dedup += 1
                elif outcome.outcome == "skipped_unsafe":
                    skipped_unsafe += 1
                elif outcome.outcome == "head_changed_mid_run":
                    head_changed_count += 1
                elif outcome.outcome == "retryable_failure":
                    retryable_failures += 1
                    if outcome.error:
                        errors.append(outcome.error)
        except OcrReviewError as exc:
            errors.append(redact_secrets(str(exc), secrets_to_redact))

    if retryable_failures > 0 or errors:
        status = "retryable_failure" if retryable_failures > 0 else "error"
        passed = False
    elif not poll_res.candidates and not outcomes:
        status = "no_candidates"
        passed = True
    else:
        status = "completed"
        passed = True

    return OcrReviewPassResult(
        status=status,
        passed=passed,
        pending=False,
        preflight=preflight,
        outcomes=outcomes,
        reviews_posted=reviews_posted,
        skipped_already_reviewed=skipped_dedup,
        skipped_competing_workflow=skipped_competing,
        skipped_indeterminate_workflow=skipped_indeterminate,
        skipped_unsafe=skipped_unsafe,
        skipped_busy=False,
        head_changed_mid_run=head_changed_count,
        retryable_failures=retryable_failures,
        warnings=warnings,
        instructions=list(preflight.instructions),
        errors=[redact_secrets(err, secrets_to_redact) for err in errors],
        next_cursor_index=poll_res.next_cursor_index,
        next_cursor_repository=poll_res.next_cursor_repository,
    )


def _evaluate_and_publish_candidate(
    *,
    cand: PullRequestCandidate,
    active_repos: Sequence[str],
    token_manager: ScopedInstallationTokenManager,
    client_factory: Callable[[str], Any],
    ocr_executable: str,
    ocr_executor: Optional[Callable[[Dict[str, Any]], Any]],
    ocr_timeout_seconds: float,
    review_mode: str,
    state: Dict[str, Any],
    state_path: Optional[Union[str, Path]],
    environ: Mapping[str, str],
    clock: Callable[[], datetime],
    secrets_to_redact: List[str],
) -> CandidateReviewOutcome:
    scoped = token_manager.get_token(cand.repository, installation_id=cand.installation_id)
    secrets_to_redact.append(scoped.token)
    repo_client = client_factory(scoped.token)

    try:
        live_pr = fetch_live_pull_request(repo_client, cand.repository, cand.number)
    except OcrExecutionError as exc:
        return _record_retryable_outcome(
            state=state,
            state_path=state_path,
            repository=cand.repository,
            pr_number=cand.number,
            head_sha=cand.head_sha,
            review_mode=review_mode,
            error_msg=str(exc),
            clock=clock,
            secrets_to_redact=secrets_to_redact,
        )

    extracted_sha = extract_head_sha(live_pr)
    live_head_sha = extracted_sha or cand.head_sha
    if str(live_pr.get("state") or "").strip().lower() != "open":
        return CandidateReviewOutcome(
            repository=cand.repository,
            pr_number=cand.number,
            head_sha=live_head_sha,
            outcome="skipped_unsafe",
            review_mode=review_mode,
            detail="Pull request is no longer open.",
        )

    if extracted_sha is None:
        guarded_check = guard_pr_for_ocr(
            live_pr,
            cand.repository,
            allowlist=active_repos,
            executor=None,
            execution_mode=LOCAL_DIRECT_EXECUTION_MODE,
            env=environ,
        )
        detail = (
            guarded_check.decision.detail or guarded_check.decision.reason
            if not guarded_check.decision.allowed
            else "Pull request head.sha is missing or malformed."
        )
        return CandidateReviewOutcome(
            repository=cand.repository,
            pr_number=cand.number,
            head_sha=live_head_sha,
            outcome="skipped_unsafe",
            review_mode=review_mode,
            detail=detail,
        )

    if is_head_reviewed(state, cand.repository, cand.number, live_head_sha):
        return CandidateReviewOutcome(
            repository=cand.repository,
            pr_number=cand.number,
            head_sha=live_head_sha,
            outcome="skipped_already_reviewed",
            review_mode=review_mode,
            detail="Live HEAD SHA was already reviewed in durable state.",
        )

    def _guarded_execute(pr_payload: Dict[str, Any], _decision: Any) -> CandidateReviewOutcome:
        remote_match = find_remote_advisory_marker(
            repo_client,
            repository=cand.repository,
            pr_number=cand.number,
            head_sha=live_head_sha,
        )
        if remote_match is not None:
            matched_mode = str(remote_match.get("review_mode") or review_mode)
            record_head_reviewed(
                state=state,
                state_path=state_path,
                repository=cand.repository,
                pr_number=cand.number,
                head_sha=live_head_sha,
                review_mode=matched_mode,
                comment_id=remote_match.get("comment_id"),
                comment_url=remote_match.get("comment_url"),
                clock=clock,
                increment_attempts=False,
                error_cls=OcrReviewError,
            )
            return CandidateReviewOutcome(
                repository=cand.repository,
                pr_number=cand.number,
                head_sha=live_head_sha,
                outcome="skipped_already_reviewed",
                review_mode=matched_mode,
                comment_id=remote_match.get("comment_id"),
                comment_url=remote_match.get("comment_url"),
                detail=(
                    f"Found existing remote advisory marker for "
                    f"{cand.repository}#{cand.number}@{live_head_sha[:12]}."
                ),
            )

        normalized_ocr = execute_ocr_for_pr(
            repo_client,
            pr_payload,
            cand.repository,
            cand.number,
            ocr_executable=ocr_executable,
            ocr_executor=ocr_executor,
            ocr_timeout_seconds=ocr_timeout_seconds,
            environ=environ,
            secrets_to_redact=secrets_to_redact,
        )

        # Mid-run HEAD recheck before posting
        recheck_pr = fetch_live_pull_request(repo_client, cand.repository, cand.number)
        recheck_head_sha = extract_head_sha(recheck_pr) or ""
        recheck_state = str(recheck_pr.get("state") or "").strip().lower()
        recheck_safety = check_same_repo_origin(recheck_pr, cand.repository, allowlist=active_repos)
        if not recheck_safety.allowed or recheck_state != "open" or not recheck_head_sha:
            detail = (
                f"PR failed safety recheck before publication ({recheck_safety.reason}, state={recheck_state})."
                if not recheck_safety.allowed or recheck_state != "open"
                else "Pull request head.sha is missing or malformed during pre-publication recheck."
            )
            return CandidateReviewOutcome(
                repository=cand.repository,
                pr_number=cand.number,
                head_sha=live_head_sha,
                outcome="skipped_unsafe",
                review_mode=review_mode,
                detail=detail,
            )

        if recheck_head_sha != live_head_sha:
            return _record_head_changed_outcome(
                state=state,
                state_path=state_path,
                repository=cand.repository,
                pr_number=cand.number,
                head_sha=live_head_sha,
                observed_new_head_sha=recheck_head_sha,
                review_mode=review_mode,
                detail=(
                    f"HEAD changed mid-run from {live_head_sha} to {recheck_head_sha}; "
                    "aborted publication of stale findings."
                ),
                clock=clock,
            )

        body = format_advisory_review_body(
            repository=cand.repository,
            pr_number=cand.number,
            head_sha=live_head_sha,
            ocr_result=normalized_ocr,
            secrets_to_redact=secrets_to_redact,
        )
        pub_status, pub_resp = publish_advisory_review(
            repo_client,
            repository=cand.repository,
            pr_number=cand.number,
            head_sha=live_head_sha,
            review_mode=review_mode,
            body=body,
        )
        if pub_status not in (200, 201):
            if pub_status == 422:
                post_fail_pr = fetch_live_pull_request(repo_client, cand.repository, cand.number)
                post_fail_sha = extract_head_sha(post_fail_pr) or ""
                if post_fail_sha and post_fail_sha != live_head_sha:
                    return _record_head_changed_outcome(
                        state=state,
                        state_path=state_path,
                        repository=cand.repository,
                        pr_number=cand.number,
                        head_sha=live_head_sha,
                        observed_new_head_sha=post_fail_sha,
                        review_mode=review_mode,
                        detail="HEAD changed during review publication; aborted stale review.",
                        clock=clock,
                    )
            msg = pub_resp.get("message", f"HTTP {pub_status}") if isinstance(pub_resp, dict) else f"HTTP {pub_status}"
            raise OcrExecutionError(f"GitHub review publication failed (HTTP {pub_status}): {msg}")

        head_rec = record_head_reviewed(
            state=state,
            state_path=state_path,
            repository=cand.repository,
            pr_number=cand.number,
            head_sha=live_head_sha,
            review_mode=review_mode,
            comment_id=pub_resp.get("id"),
            comment_url=pub_resp.get("html_url"),
            clock=clock,
            increment_attempts=True,
            error_cls=OcrReviewError,
        )

        return CandidateReviewOutcome(
            repository=cand.repository,
            pr_number=cand.number,
            head_sha=live_head_sha,
            outcome="reviewed",
            review_mode=review_mode,
            comment_id=pub_resp.get("id"),
            comment_url=pub_resp.get("html_url"),
            attempts=head_rec["attempts"],
            detail=f"Published advisory {review_mode} for {cand.repository}#{cand.number}@{live_head_sha[:12]}.",
        )

    try:
        guarded = guard_pr_for_ocr(
            live_pr,
            cand.repository,
            allowlist=active_repos,
            executor=_guarded_execute,
            execution_mode=LOCAL_DIRECT_EXECUTION_MODE,
            env=environ,
        )
    except OcrReviewError:
        raise
    except (
        OcrExecutionError,
        OcrPreflightError,
        OcrAppAuthError,
        OcrSafetyError,
        OcrStateError,
        OcrPollerError,
        ApiError,
        OSError,
        ValueError,
        http.client.HTTPException,
    ) as exc:
        return _record_retryable_outcome(
            state=state,
            state_path=state_path,
            repository=cand.repository,
            pr_number=cand.number,
            head_sha=live_head_sha,
            review_mode=review_mode,
            error_msg=str(exc),
            clock=clock,
            secrets_to_redact=secrets_to_redact,
        )

    if not guarded.executed:
        return CandidateReviewOutcome(
            repository=cand.repository,
            pr_number=cand.number,
            head_sha=live_head_sha,
            outcome="skipped_unsafe",
            review_mode=review_mode,
            detail=guarded.decision.detail or guarded.decision.reason,
        )

    return guarded.result


def format_ocr_review_report(
    result: OcrReviewPassResult,
    *,
    extra_secrets: Sequence[str] = (),
) -> str:
    """Format a secret-free human-readable report for an OCR review pass."""
    lines: List[str] = [
        "============================================================",
        "sciflow-cicd OCR: Advisory Review Pass",
        "============================================================",
        f"Status: {result.status}",
        f"Reviews posted: {result.reviews_posted}",
        f"Skipped (already reviewed HEAD): {result.skipped_already_reviewed}",
        f"Skipped (competing workflow): {result.skipped_competing_workflow}",
    ]
    if result.skipped_indeterminate_workflow:
        lines.append(f"Skipped (indeterminate competing workflow): {result.skipped_indeterminate_workflow}")
    lines.extend([
        f"Skipped (unsafe/fork): {result.skipped_unsafe}",
        f"HEAD changed mid-run: {result.head_changed_mid_run}",
        f"Retryable failures: {result.retryable_failures}",
    ])
    if result.skipped_busy:
        lines.append("Single-flight lock: busy (skipped duplicate evaluation)")
    if result.outcomes:
        lines.append("Candidate outcomes:")
        for item in result.outcomes:
            lines.append(
                f"  - {item.repository}#{item.pr_number}@{item.head_sha[:12]}: {item.outcome} ({item.detail})"
            )
    if result.pending_reasons:
        lines.append("Pending configuration:")
        for reason in result.pending_reasons:
            lines.append(f"  [pending] {reason}")
    if result.warnings:
        lines.append("Warnings:")
        for warning in result.warnings:
            lines.append(f"  [!] {warning}")
    if result.instructions:
        lines.append("Actionable instructions:")
        for inst in result.instructions:
            lines.append(f"  -> {inst}")
    if result.errors:
        lines.append("Errors:")
        for err in result.errors:
            lines.append(f"  [x] {err}")
    lines.append("============================================================")
    return redact_secrets("\n".join(lines) + "\n", extra_secrets)


def run_ocr_review_cycles(
    *,
    app_config: Optional[OcrAppConfig] = None,
    eligibility_config: Optional[PrEligibilityConfig] = None,
    review_config: Optional[OcrReviewConfig] = None,
    app_id: Optional[Union[str, int]] = None,
    private_key_path: Optional[Union[str, Path]] = None,
    allowlist: Sequence[str] = (),
    admin_app_ids: Sequence[Union[str, int]] = (),
    api_base_url: str = "https://api.github.com",
    state_path: Optional[Union[str, Path]] = None,
    lock_path: Optional[Union[str, Path]] = None,
    ocr_bin: Optional[str] = None,
    eligibility_mode: str = "either",
    eligible_label: str = DEFAULT_ELIGIBLE_LABEL,
    maintainers: Sequence[str] = (),
    include_drafts: bool = False,
    review_mode: str = "pr_review",
    max_reviews: Optional[int] = None,
    ocr_timeout_seconds: float = DEFAULT_OCR_TIMEOUT_SECONDS,
    interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_cycles: int = 1,
    env: Optional[Mapping[str, str]] = None,
    api_factory: Optional[Callable[[str], Any]] = None,
    signer: Optional[Callable[[bytes], bytes]] = None,
    which_fn: Optional[Callable[[str], Optional[str]]] = None,
    ocr_executor: Optional[Callable[[Dict[str, Any]], Any]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> List[OcrReviewPassResult]:
    """Run one or more bounded advisory OCR review cycles with interval clamping."""
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
    rev_cfg = resolve_review_config(
        review_config,
        state_path=state_path,
        lock_path=lock_path,
        ocr_bin=ocr_bin,
        review_mode=review_mode,
        max_reviews=max_reviews,
        ocr_timeout_seconds=ocr_timeout_seconds,
        interval_seconds=interval_seconds,
        max_cycles=max_cycles,
    )
    if isinstance(rev_cfg.max_cycles, bool) or not isinstance(rev_cfg.max_cycles, int) or rev_cfg.max_cycles <= 0:
        raise OcrReviewError("max_cycles must be a positive integer.")
    bounded_interval = clamp_poll_interval(rev_cfg.interval_seconds)
    sleeper = sleep_fn if sleep_fn is not None else time.sleep

    results: List[OcrReviewPassResult] = []
    for cycle_idx in range(rev_cfg.max_cycles):
        result = run_ocr_review_pass(
            app_config=app_cfg,
            eligibility_config=elig_cfg,
            review_config=rev_cfg,
            env=env,
            which_fn=which_fn,
            ocr_executor=ocr_executor,
            now_fn=now_fn,
        )
        results.append(result)
        if cycle_idx < rev_cfg.max_cycles - 1:
            if result.status in ("pending_configuration", "skipped_competing_workflow", "error"):
                break
            sleeper(bounded_interval)
    return results


def run_ocr_reviews(
    *,
    app_config: Optional[OcrAppConfig] = None,
    eligibility_config: Optional[PrEligibilityConfig] = None,
    review_config: Optional[OcrReviewConfig] = None,
    app_id: Optional[Union[str, int]] = None,
    private_key_path: Optional[Union[str, Path]] = None,
    allowlist: Sequence[str] = (),
    admin_app_ids: Sequence[Union[str, int]] = (),
    api_base_url: str = "https://api.github.com",
    state_path: Optional[Union[str, Path]] = None,
    lock_path: Optional[Union[str, Path]] = None,
    ocr_bin: Optional[str] = None,
    eligibility_mode: str = "either",
    eligible_label: str = DEFAULT_ELIGIBLE_LABEL,
    maintainers: Sequence[str] = (),
    include_drafts: bool = False,
    review_mode: str = "pr_review",
    max_reviews: Optional[int] = None,
    ocr_timeout_seconds: float = DEFAULT_OCR_TIMEOUT_SECONDS,
    interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_cycles: int = 1,
    json_output: bool = False,
    out_stream: Optional[TextIO] = None,
    err_stream: Optional[TextIO] = None,
    env: Optional[Mapping[str, str]] = None,
    api_factory: Optional[Callable[[str], Any]] = None,
    signer: Optional[Callable[[bytes], bytes]] = None,
    which_fn: Optional[Callable[[str], Optional[str]]] = None,
    ocr_executor: Optional[Callable[[Dict[str, Any]], Any]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> int:
    """CLI entrypoint wrapper for single-pass or bounded multi-cycle advisory OCR reviews."""
    out_s = out_stream if out_stream is not None else sys.stdout
    err_s = err_stream if err_stream is not None else sys.stderr
    environ = env if env is not None else os.environ
    secrets_to_redact = collect_env_secrets(environ)

    base_rev = resolve_review_config(
        review_config,
        state_path=state_path,
        lock_path=lock_path,
        ocr_bin=ocr_bin,
        review_mode=review_mode,
        max_reviews=max_reviews,
        ocr_timeout_seconds=ocr_timeout_seconds,
        interval_seconds=interval_seconds,
        max_cycles=max_cycles,
    )

    try:
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
        effective_state_path = (
            base_rev.state_path if base_rev.state_path is not None else default_review_state_path(app_cfg.app_id)
        )
        effective_rev = replace(base_rev, state_path=effective_state_path)
        cycles = run_ocr_review_cycles(
            app_config=app_cfg,
            eligibility_config=elig_cfg,
            review_config=effective_rev,
            env=environ,
            which_fn=which_fn,
            ocr_executor=ocr_executor,
            sleep_fn=sleep_fn,
            now_fn=now_fn,
        )
    except (OcrReviewError, OcrPreflightError, OcrPollerError, OcrAppAuthError, OcrSafetyError) as exc:
        cycles = [
            OcrReviewPassResult(
                status="error",
                passed=False,
                errors=[redact_secrets(str(exc), secrets_to_redact)],
            )
        ]

    if len(cycles) == 1 and base_rev.max_cycles == 1:
        single = cycles[0]
        if json_output:
            out_s.write(json.dumps(single.to_dict(extra_secrets=secrets_to_redact), indent=2) + "\n")
        else:
            out_s.write(format_ocr_review_report(single, extra_secrets=secrets_to_redact))
            if not single.passed and single.errors:
                err_s.write(redact_secrets("\n".join(single.errors) + "\n", secrets_to_redact))
        return 0 if single.passed else 1

    passed = bool(cycles) and all(c.passed for c in cycles)
    all_errors = [err for c in cycles for err in c.errors]
    if json_output:
        payload = {
            "status": cycles[-1].status,
            "passed": passed,
            "reviews_posted": sum(c.reviews_posted for c in cycles),
            "skipped_already_reviewed": sum(c.skipped_already_reviewed for c in cycles),
            "skipped_competing_workflow": sum(c.skipped_competing_workflow for c in cycles),
            "skipped_indeterminate_workflow": sum(c.skipped_indeterminate_workflow for c in cycles),
            "skipped_unsafe": sum(c.skipped_unsafe for c in cycles),
            "head_changed_mid_run": sum(c.head_changed_mid_run for c in cycles),
            "retryable_failures": sum(c.retryable_failures for c in cycles),
            "cycles": [c.to_dict(extra_secrets=secrets_to_redact) for c in cycles],
            "errors": all_errors,
        }
        out_s.write(redact_secrets(json.dumps(payload, indent=2), secrets_to_redact) + "\n")
    else:
        for cycle in cycles:
            out_s.write(format_ocr_review_report(cycle, extra_secrets=secrets_to_redact))
        if not passed and all_errors:
            err_s.write(redact_secrets("\n".join(all_errors) + "\n", secrets_to_redact))

    return 0 if passed else 1
