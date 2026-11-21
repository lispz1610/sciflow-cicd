"""Preflight OCR runtime executable, LLM provider configuration, limited App permissions, and allowlist scoping for external systemd runner."""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TextIO, Union
from urllib.parse import quote, urlsplit

try:
    from scripts.configure_github_repo import ApiError
except ImportError:
    from configure_github_repo import ApiError

try:
    from scripts.ocr_app_auth import (
        AppPreflightResult,
        OcrAppAuthError,
        OcrAppConfig,
        collect_env_secrets,
        preflight_ocr_app,
        redact_secrets,
        resolve_app_config,
    )
except ImportError:
    from ocr_app_auth import (
        AppPreflightResult,
        OcrAppAuthError,
        OcrAppConfig,
        collect_env_secrets,
        preflight_ocr_app,
        redact_secrets,
        resolve_app_config,
    )

try:
    from scripts.ocr_poller import (
        DEFAULT_ELIGIBLE_LABEL,
        MAINTAINER_ASSOCIATIONS,
        PrEligibilityConfig,
        VALID_ELIGIBILITY_MODES,
        evaluate_pr_eligibility,
        normalize_client_factory,
        resolve_eligibility_config,
    )
except ImportError:
    from ocr_poller import (
        DEFAULT_ELIGIBLE_LABEL,
        MAINTAINER_ASSOCIATIONS,
        PrEligibilityConfig,
        VALID_ELIGIBILITY_MODES,
        evaluate_pr_eligibility,
        normalize_client_factory,
        resolve_eligibility_config,
    )

try:
    from scripts.ocr_safety import safe_client_request
except ImportError:
    from ocr_safety import safe_client_request

REQUIRED_PROVIDER_ENV_VARS = (
    "OCR_LLM_AUTH_TOKEN",
    "OCR_LLM_URL",
    "OCR_LLM_MODEL",
    "OCR_LLM_USE_ANTHROPIC",
)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


class OcrPreflightError(RuntimeError):
    """Raised when OCR preflight configuration is invalid or unsafe."""


@dataclass(frozen=True)
class OcrExecutableStatus:
    """Availability check result for the OCR runtime executable."""

    requested: str
    available: bool
    resolved_path: Optional[str] = None
    reason: Optional[str] = None
    instruction: Optional[str] = None


@dataclass(frozen=True)
class ProviderConfigStatus:
    """Validation result for required LLM provider configuration."""

    configured: bool
    missing_variables: List[str] = field(default_factory=list)
    invalid_variables: Dict[str, str] = field(default_factory=dict)
    llm_url: Optional[str] = None
    llm_model: Optional[str] = None
    use_anthropic: Optional[bool] = None
    instructions: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class CompetingWorkflowStatus:
    """Inspection result for an existing Review Setup workflow on a repo's default branch."""

    repository: str
    workflow_path: str
    default_branch: Optional[str]
    present: bool
    active: bool
    workflow_state: Optional[str] = None
    warning: Optional[str] = None
    instruction: Optional[str] = None
    error: Optional[str] = None
    indeterminate: bool = False


@dataclass
class OcrPreflightResult:
    """Structured result of OCR runtime, provider, App scope, and competing-workflow preflight."""

    status: str  # "ready", "pending_configuration", or "error"
    ready: bool
    pending: bool
    executable: OcrExecutableStatus
    provider: ProviderConfigStatus
    app_preflight: Optional[AppPreflightResult]
    competing_workflows: List[CompetingWorkflowStatus] = field(default_factory=list)
    indeterminate_workflows: List[CompetingWorkflowStatus] = field(default_factory=list)
    workflow_checks: List[CompetingWorkflowStatus] = field(default_factory=list)
    scoping: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    pending_reasons: List[str] = field(default_factory=list)
    instructions: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self, extra_secrets: Sequence[str] = ()) -> Dict[str, Any]:
        """Return a JSON-serializable, secret-redacted representation."""
        raw = {
            "status": self.status,
            "ready": self.ready,
            "pending": self.pending,
            "executable": asdict(self.executable),
            "provider": asdict(self.provider),
            "app_preflight": self.app_preflight.to_dict() if self.app_preflight is not None else None,
            "competing_workflows": [asdict(item) for item in self.competing_workflows],
            "indeterminate_workflows": [asdict(item) for item in self.indeterminate_workflows],
            "workflow_checks": [asdict(item) for item in self.workflow_checks],
            "scoping": dict(self.scoping),
            "warnings": list(self.warnings),
            "pending_reasons": list(self.pending_reasons),
            "instructions": list(self.instructions),
            "errors": list(self.errors),
        }
        return json.loads(redact_secrets(json.dumps(raw, default=str), extra_secrets))


def _validate_ocr_bin_spec(candidate: str) -> str:
    if not isinstance(candidate, str) or not candidate.strip():
        raise OcrPreflightError("OCR executable specification must be a non-empty command name or path.")
    if _CONTROL_CHAR_RE.search(candidate):
        raise OcrPreflightError("OCR executable specification contains invalid control characters.")
    trimmed = candidate.strip()
    if any(part == ".." for part in trimmed.replace("\\", "/").split("/")):
        raise OcrPreflightError("OCR executable path must not contain '..' traversal segments.")
    return trimmed


def check_ocr_executable(
    ocr_bin: Optional[str] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    which_fn: Optional[Callable[[str], Optional[str]]] = None,
) -> OcrExecutableStatus:
    """Safely check whether the OCR executable is available and runnable."""
    environ = env if env is not None else os.environ
    raw_env_bin = environ.get("OCR_CLI_BIN")
    if ocr_bin is not None:
        requested = _validate_ocr_bin_spec(ocr_bin)
    elif raw_env_bin is not None and raw_env_bin != "":
        requested = _validate_ocr_bin_spec(raw_env_bin)
    else:
        requested = "ocr"

    resolved: Optional[str] = None
    if which_fn is not None:
        resolved = which_fn(requested)
    elif "/" in requested or os.sep in requested:
        path = Path(requested)
        if path.is_file() and os.access(path, os.X_OK):
            resolved = str(path)
    else:
        resolved = shutil.which(requested, path=environ.get("PATH"))

    if resolved:
        return OcrExecutableStatus(
            requested=requested,
            available=True,
            resolved_path=str(resolved),
        )

    return OcrExecutableStatus(
        requested=requested,
        available=False,
        resolved_path=None,
        reason=f"OCR executable '{requested}' was not found or is not executable.",
        instruction=(
            f"Install the OCR runtime executable ('{requested}') on PATH or specify "
            "--ocr-bin / OCR_CLI_BIN before running automated OCR reviews."
        ),
    )


def _validate_llm_url(raw_url: str) -> str:
    if _CONTROL_CHAR_RE.search(raw_url) or any(ch.isspace() for ch in raw_url):
        raise OcrPreflightError(
            "OCR_LLM_URL is invalid: must be an HTTPS URL without whitespace, embedded credentials, query, or fragment."
        )
    try:
        parsed = urlsplit(raw_url)
        _ = parsed.port
    except ValueError:
        raise OcrPreflightError(
            "OCR_LLM_URL is invalid: must be an HTTPS URL without embedded credentials, query, or fragment."
        ) from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or "@" in parsed.netloc
        or parsed.query
        or parsed.fragment
        or "?" in raw_url
        or "#" in raw_url
    ):
        raise OcrPreflightError(
            "OCR_LLM_URL is invalid: must be an HTTPS URL without embedded credentials, query, or fragment."
        )
    return raw_url


def check_provider_config(env: Optional[Mapping[str, str]] = None) -> ProviderConfigStatus:
    """Validate OCR LLM provider environment variables without exposing OCR_LLM_AUTH_TOKEN."""
    environ = env if env is not None else os.environ
    missing: List[str] = []
    invalid: Dict[str, str] = {}
    instructions: List[str] = []

    raw_token = environ.get("OCR_LLM_AUTH_TOKEN")
    if not isinstance(raw_token, str) or not raw_token.strip():
        missing.append("OCR_LLM_AUTH_TOKEN")
        instructions.append("Configure OCR_LLM_AUTH_TOKEN in the OCR worker environment.")

    validated_url: Optional[str] = None
    raw_url = environ.get("OCR_LLM_URL")
    if not isinstance(raw_url, str) or not raw_url.strip():
        missing.append("OCR_LLM_URL")
        instructions.append(
            "Configure OCR_LLM_URL to an HTTPS provider endpoint without embedded credentials, query, or fragment."
        )
    else:
        try:
            validated_url = _validate_llm_url(raw_url)
        except OcrPreflightError as exc:
            invalid["OCR_LLM_URL"] = str(exc)

    validated_model: Optional[str] = None
    raw_model = environ.get("OCR_LLM_MODEL")
    if not isinstance(raw_model, str) or not raw_model.strip():
        missing.append("OCR_LLM_MODEL")
        instructions.append("Configure OCR_LLM_MODEL with the target LLM model name.")
    else:
        stripped_model = raw_model.strip()
        if _CONTROL_CHAR_RE.search(stripped_model) or len(stripped_model) > 200:
            invalid["OCR_LLM_MODEL"] = (
                "OCR_LLM_MODEL is invalid: must be a single-line model name of at most 200 characters."
            )
        else:
            validated_model = stripped_model

    validated_use_anthropic: Optional[bool] = None
    raw_use_anthropic = environ.get("OCR_LLM_USE_ANTHROPIC")
    if not isinstance(raw_use_anthropic, str) or not raw_use_anthropic.strip():
        missing.append("OCR_LLM_USE_ANTHROPIC")
        instructions.append("Configure OCR_LLM_USE_ANTHROPIC strictly to 'true' or 'false'.")
    else:
        if raw_use_anthropic == "true":
            validated_use_anthropic = True
        elif raw_use_anthropic == "false":
            validated_use_anthropic = False
        else:
            invalid["OCR_LLM_USE_ANTHROPIC"] = (
                "OCR_LLM_USE_ANTHROPIC is invalid: must be strictly 'true' or 'false'."
            )

    configured = not missing and not invalid
    return ProviderConfigStatus(
        configured=configured,
        missing_variables=missing,
        invalid_variables=invalid,
        llm_url=validated_url,
        llm_model=validated_model,
        use_anthropic=validated_use_anthropic,
        instructions=instructions,
    )


def inspect_competing_workflow(
    client: Any = None,
    repository: str = "",
) -> CompetingWorkflowStatus:
    """External OCR polling operates without checking for or conflicting with in-repo Actions workflows."""
    return CompetingWorkflowStatus(
        repository=repository,
        workflow_path="",
        default_branch=None,
        present=False,
        active=False,
    )


def preflight_ocr_runtime(
    *,
    app_config: Optional[OcrAppConfig] = None,
    eligibility_config: Optional[PrEligibilityConfig] = None,
    app_id: Optional[Union[str, int]] = None,
    private_key_path: Optional[Union[str, Path]] = None,
    allowlist: Sequence[str] = (),
    admin_app_ids: Sequence[Union[str, int]] = (),
    api_base_url: str = "https://api.github.com",
    ocr_bin: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    eligibility_mode: str = "either",
    eligible_label: str = DEFAULT_ELIGIBLE_LABEL,
    maintainers: Sequence[str] = (),
    include_drafts: bool = False,
    inspect_pull_requests: bool = False,
    api_factory: Optional[Callable[[str], Any]] = None,
    signer: Optional[Callable[[bytes], bytes]] = None,
    which_fn: Optional[Callable[[str], Optional[str]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> OcrPreflightResult:
    """Preflight OCR runtime executable, provider configuration, limited App scope, and PR eligibility without expecting or checking for in-repo Actions review workflows."""
    environ = env if env is not None else os.environ
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

    errors: List[str] = []
    warnings: List[str] = []
    pending_reasons: List[str] = []
    instructions: List[str] = []

    if elig_cfg.eligibility_mode not in VALID_ELIGIBILITY_MODES:
        raise OcrPreflightError(
            f"Unsupported eligibility_mode {elig_cfg.eligibility_mode!r}; expected one of {sorted(VALID_ELIGIBILITY_MODES)}"
        )

    try:
        exec_status = check_ocr_executable(ocr_bin=ocr_bin, env=environ, which_fn=which_fn)
    except OcrPreflightError as exc:
        exec_status = OcrExecutableStatus(
            requested=str(ocr_bin or environ.get("OCR_CLI_BIN") or "ocr"),
            available=False,
            reason=str(exc),
        )
        errors.append(str(exc))

    provider_status = check_provider_config(env=environ)
    for var_err in provider_status.invalid_variables.values():
        errors.append(var_err)

    if not exec_status.available and not errors:
        if exec_status.reason:
            pending_reasons.append(exec_status.reason)
        if exec_status.instruction:
            instructions.append(exec_status.instruction)

    if provider_status.missing_variables:
        pending_reasons.append(
            "Missing required OCR provider configuration: " + ", ".join(provider_status.missing_variables)
        )
        instructions.extend(provider_status.instructions)

    app_result: Optional[AppPreflightResult] = None
    client_factory: Optional[Callable[[str], Any]] = None
    effective_app_cfg = app_cfg
    try:
        client_factory = normalize_client_factory(app_cfg.api_base_url, app_cfg.api_factory)
        effective_app_cfg = replace(app_cfg, api_factory=client_factory)
        app_result = preflight_ocr_app(app_config=effective_app_cfg)
        if not app_result.passed:
            errors.extend(app_result.errors or ["Limited OCR GitHub App preflight failed."])
    except OcrAppAuthError as exc:
        errors.append(redact_secrets(str(exc), secrets_to_redact))

    workflow_checks: List[CompetingWorkflowStatus] = []
    competing_workflows: List[CompetingWorkflowStatus] = []
    indeterminate_workflows: List[CompetingWorkflowStatus] = []
    scoping_samples: Dict[str, Any] = {}

    if app_result is not None and app_result.passed and client_factory is not None:
        clock = now_fn if now_fn is not None else (lambda: datetime.now(timezone.utc))
        token_manager = effective_app_cfg.build_token_manager(now_fn=clock)
        decision_by_repo = {dec.repository: dec for dec in app_result.decisions if dec.eligible}
        for repo in app_result.eligible_repositories:
            dec = decision_by_repo[repo]
            try:
                scoped = token_manager.get_token(repo, installation_id=int(dec.installation_id))
                secrets_to_redact.append(scoped.token)
                repo_client = client_factory(scoped.token)
                if inspect_pull_requests:
                    pr_status, pr_body = safe_client_request(
                        repo_client,
                        "GET",
                        f"/repos/{repo}/pulls?state=open&per_page=100&page=1",
                    )
                    if pr_status == 200 and isinstance(pr_body, list):
                        eligible_prs = []
                        skipped_prs = []
                        for item in pr_body:
                            if not isinstance(item, dict):
                                continue
                            is_elig, reason = evaluate_pr_eligibility(
                                item,
                                repo,
                                eligibility_config=elig_cfg,
                            )
                            entry = {"number": item.get("number"), "reason": reason}
                            if is_elig:
                                eligible_prs.append(entry)
                            else:
                                skipped_prs.append(entry)
                        scoping_samples[repo] = {"eligible": eligible_prs, "skipped": skipped_prs}
            except (OcrAppAuthError, ApiError, OSError, ValueError) as exc:
                errors.append(redact_secrets(f"Failed preflight inspection for '{repo}': {exc}", secrets_to_redact))

    includes_manual_maintainer_prs = elig_cfg.eligibility_mode in ("either", "maintainer")
    if not includes_manual_maintainer_prs:
        warnings.append(
            f"PR scoping is set to eligibility_mode='{elig_cfg.eligibility_mode}', which excludes unlabeled "
            "pull requests opened manually by maintainers."
        )

    scoping = {
        "allowlist": list(app_result.allowlist) if app_result is not None else list(app_cfg.allowlist),
        "eligible_repositories": list(app_result.eligible_repositories) if app_result is not None else [],
        "same_repo_only": True,
        "forks_excluded": True,
        "eligibility_mode": elig_cfg.eligibility_mode,
        "eligible_label": elig_cfg.eligible_label,
        "includes_manual_maintainer_prs": includes_manual_maintainer_prs,
        "maintainer_associations": sorted(MAINTAINER_ASSOCIATIONS),
        "explicit_maintainers": [str(m).strip() for m in elig_cfg.maintainers if str(m).strip()],
        "include_drafts": bool(elig_cfg.include_drafts),
    }
    if scoping_samples:
        scoping["inspected_pull_requests"] = scoping_samples

    redacted_errors = [redact_secrets(err, secrets_to_redact) for err in errors]
    redacted_warnings = [redact_secrets(w, secrets_to_redact) for w in warnings]
    redacted_pending = [redact_secrets(p, secrets_to_redact) for p in pending_reasons]
    redacted_instructions = [redact_secrets(i, secrets_to_redact) for i in instructions]

    if redacted_errors:
        status = "error"
        ready = False
        pending = False
    elif redacted_pending:
        status = "pending_configuration"
        ready = False
        pending = True
    else:
        status = "ready"
        ready = True
        pending = False

    return OcrPreflightResult(
        status=status,
        ready=ready,
        pending=pending,
        executable=exec_status,
        provider=provider_status,
        app_preflight=app_result,
        competing_workflows=competing_workflows,
        indeterminate_workflows=indeterminate_workflows,
        workflow_checks=workflow_checks,
        scoping=scoping,
        warnings=redacted_warnings,
        pending_reasons=redacted_pending,
        instructions=redacted_instructions,
        errors=redacted_errors,
    )


def format_ocr_preflight_report(
    result: OcrPreflightResult,
    *,
    extra_secrets: Sequence[str] = (),
) -> str:
    """Format a secret-free human-readable report for OCR preflight."""
    lines: List[str] = [
        "============================================================",
        "sciflow-cicd OCR: Runtime, Provider & Workflow Preflight",
        "============================================================",
        f"Status: {result.status}",
        f"OCR executable ({result.executable.requested}): "
        + (f"AVAILABLE ({result.executable.resolved_path})" if result.executable.available else "MISSING"),
        "Provider config: "
        + ("CONFIGURED" if result.provider.configured else "INCOMPLETE/INVALID"),
    ]
    if result.provider.missing_variables:
        lines.append(f"  Missing variables: {', '.join(result.provider.missing_variables)}")
    if result.provider.invalid_variables:
        for name, msg in result.provider.invalid_variables.items():
            lines.append(f"  Invalid {name}: {msg}")

    if result.app_preflight is not None:
        lines.append(
            f"GitHub App preflight: {'PASS' if result.app_preflight.passed else 'FAIL'} "
            f"(eligible repos: {', '.join(result.app_preflight.eligible_repositories) or '(none)'})"
        )

    lines.append(
        "PR scoping: "
        f"mode={result.scoping.get('eligibility_mode')}, "
        f"same_repo_only={result.scoping.get('same_repo_only')}, "
        f"includes_manual_maintainer_prs={result.scoping.get('includes_manual_maintainer_prs')}"
    )

    if result.competing_workflows:
        lines.append(f"Competing workflows detected ({len(result.competing_workflows)}):")
        for wf in result.competing_workflows:
            lines.append(
                f"  [!] {wf.repository}: {wf.workflow_path} on {wf.default_branch} (state={wf.workflow_state})"
            )
            if wf.instruction:
                lines.append(f"      Action: {wf.instruction}")

    if result.indeterminate_workflows:
        lines.append(f"Indeterminate competing-workflow checks ({len(result.indeterminate_workflows)}):")
        for wf in result.indeterminate_workflows:
            lines.append(
                f"  [?] {wf.repository}: {wf.workflow_path} on {wf.default_branch or '(unknown)'} "
                f"(state={wf.workflow_state or 'indeterminate'}; fail-closed skip)"
            )
            if wf.error:
                lines.append(f"      Error: {wf.error}")
            if wf.instruction:
                lines.append(f"      Action: {wf.instruction}")

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
            lines.append(f"  [✗] {err}")

    lines.append("============================================================")
    return redact_secrets("\n".join(lines) + "\n", extra_secrets)


def run_ocr_preflight(
    *,
    app_config: Optional[OcrAppConfig] = None,
    eligibility_config: Optional[PrEligibilityConfig] = None,
    app_id: Optional[Union[str, int]] = None,
    private_key_path: Optional[Union[str, Path]] = None,
    allowlist: Sequence[str] = (),
    admin_app_ids: Sequence[Union[str, int]] = (),
    api_base_url: str = "https://api.github.com",
    ocr_bin: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    eligibility_mode: str = "either",
    eligible_label: str = DEFAULT_ELIGIBLE_LABEL,
    maintainers: Sequence[str] = (),
    include_drafts: bool = False,
    inspect_pull_requests: bool = False,
    json_output: bool = False,
    out_stream: Optional[TextIO] = None,
    err_stream: Optional[TextIO] = None,
    api_factory: Optional[Callable[[str], Any]] = None,
    signer: Optional[Callable[[bytes], bytes]] = None,
    which_fn: Optional[Callable[[str], Optional[str]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> int:
    """Run OCR preflight and write formatted or JSON output.

    Returns 0 for 'ready' and 'pending_configuration' (non-blocking), and 1 for 'error'.
    """
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
        result = preflight_ocr_runtime(
            app_config=effective_app,
            eligibility_config=effective_elig,
            ocr_bin=ocr_bin,
            env=environ,
            inspect_pull_requests=inspect_pull_requests,
            which_fn=which_fn,
            now_fn=now_fn,
        )
    except (OcrPreflightError, OcrAppAuthError) as exc:
        safe_msg = redact_secrets(str(exc), secrets_to_redact)
        result = OcrPreflightResult(
            status="error",
            ready=False,
            pending=False,
            executable=OcrExecutableStatus(requested=str(ocr_bin or "ocr"), available=False),
            provider=ProviderConfigStatus(configured=False),
            app_preflight=None,
            errors=[safe_msg],
        )

    if json_output:
        out_s.write(json.dumps(result.to_dict(extra_secrets=secrets_to_redact), indent=2) + "\n")
    else:
        out_s.write(format_ocr_preflight_report(result, extra_secrets=secrets_to_redact))
        if result.status == "error" and result.errors:
            err_s.write(redact_secrets("\n".join(result.errors) + "\n", secrets_to_redact))

    return 0 if result.status in ("ready", "pending_configuration") else 1
