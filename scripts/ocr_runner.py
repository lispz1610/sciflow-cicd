"""PR context preparation and isolated OCR subprocess execution for automated advisory review."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

try:
    from scripts.configure_github_repo import ApiError
except ImportError:
    from configure_github_repo import ApiError

try:
    from scripts.ocr_app_auth import redact_secrets
except ImportError:
    from ocr_app_auth import redact_secrets

try:
    from scripts.ocr_safety import safe_client_request
except ImportError:
    from ocr_safety import safe_client_request


DEFAULT_OCR_TIMEOUT_SECONDS = 120.0
ALLOWED_SUBPROCESS_ENV_KEYS = (
    "PATH",
    "LD_LIBRARY_PATH",
    "HOME",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "OCR_CLI_BIN",
    "OCR_LLM_AUTH_TOKEN",
    "OCR_LLM_URL",
    "OCR_LLM_MODEL",
    "OCR_LLM_USE_ANTHROPIC",
    "OCR_LLM_EXTRA_BODY",
)


class OcrReviewError(RuntimeError):
    """Raised when OCR review configuration, safety, or state handling fails."""


class OcrExecutionError(RuntimeError):
    """Raised when the OCR CLI or provider fails in a retryable manner."""


def extract_head_sha(pr_payload: Any) -> Optional[str]:
    """Safely extract non-empty head.sha from a PR payload, returning None if malformed."""
    if not isinstance(pr_payload, dict):
        return None
    head_obj = pr_payload.get("head")
    if not isinstance(head_obj, dict):
        return None
    raw_sha = head_obj.get("sha")
    if not isinstance(raw_sha, str) or not raw_sha.strip():
        return None
    return raw_sha.strip()


def fetch_live_pull_request(client: Any, repository: str, pr_number: int) -> Dict[str, Any]:
    """Fetch live pull request metadata from GitHub via the OCR safety-checked client."""
    path = f"/repos/{repository}/pulls/{pr_number}"
    try:
        status, body = safe_client_request(client, "GET", path)
    except (ApiError, OSError) as exc:
        raise OcrExecutionError(f"Failed fetching live PR {repository}#{pr_number}: {exc}") from None
    if status != 200 or not isinstance(body, dict):
        msg = body.get("message", f"HTTP {status}") if isinstance(body, dict) else f"HTTP {status}"
        raise OcrExecutionError(f"GET {path}: HTTP {status}: {msg}")
    return body


def fetch_pr_files(client: Any, repository: str, pr_number: int) -> List[Dict[str, Any]]:
    """Fetch and sanitize changed PR file metadata/patches for OCR context."""
    path = f"/repos/{repository}/pulls/{pr_number}/files?per_page=100"
    try:
        status, body = safe_client_request(client, "GET", path)
    except (ApiError, OSError) as exc:
        raise OcrExecutionError(f"Failed fetching PR files for {repository}#{pr_number}: {exc}") from None
    if status == 404:
        return []
    if status != 200 or not isinstance(body, list):
        msg = body.get("message", f"HTTP {status}") if isinstance(body, dict) else f"HTTP {status}"
        raise OcrExecutionError(f"GET {path}: HTTP {status}: {msg}")
    sanitized: List[Dict[str, Any]] = []
    for entry in body[:100]:
        if not isinstance(entry, dict):
            continue
        sanitized.append(
            {
                "filename": str(entry.get("filename") or ""),
                "status": str(entry.get("status") or ""),
                "additions": entry.get("additions", 0),
                "deletions": entry.get("deletions", 0),
                "patch": str(entry.get("patch") or "")[:8000],
            }
        )
    return sanitized


def build_sanitized_context(
    pr_payload: Dict[str, Any],
    repository: str,
    files: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build a minimal, secret-free PR context dictionary for the OCR runner."""
    head = pr_payload.get("head") if isinstance(pr_payload.get("head"), dict) else {}
    base = pr_payload.get("base") if isinstance(pr_payload.get("base"), dict) else {}
    user = pr_payload.get("user") if isinstance(pr_payload.get("user"), dict) else {}
    return {
        "repository": repository,
        "pr_number": int(pr_payload["number"]),
        "title": str(pr_payload.get("title") or ""),
        "body": str(pr_payload.get("body") or "")[:8000],
        "head_sha": str(head.get("sha") or ""),
        "base_sha": str(base.get("sha") or ""),
        "head_ref": str(head.get("ref") or ""),
        "base_ref": str(base.get("ref") or ""),
        "author_login": str(user.get("login") or ""),
        "html_url": str(pr_payload.get("html_url") or ""),
        "files": files,
    }


def build_ocr_subprocess_env(
    environ: Mapping[str, str],
    context: Dict[str, Any],
    context_file: Path,
) -> Dict[str, str]:
    """Construct a minimal allowlisted environment for the OCR CLI subprocess."""
    sub_env: Dict[str, str] = {}
    for key in ALLOWED_SUBPROCESS_ENV_KEYS:
        val = environ.get(key)
        if isinstance(val, str):
            if key == "OCR_LLM_EXTRA_BODY" and not val.strip():
                continue
            sub_env[key] = val
    sub_env["OCR_NO_UPDATE"] = "1"
    sub_env["OCR_REPOSITORY"] = str(context["repository"])
    sub_env["OCR_PR_NUMBER"] = str(context["pr_number"])
    sub_env["OCR_HEAD_SHA"] = str(context["head_sha"])
    sub_env["OCR_BASE_REF"] = str(context["base_ref"])
    sub_env["OCR_HEAD_REF"] = str(context["head_ref"])
    sub_env["OCR_PR_CONTEXT_FILE"] = str(context_file)
    return sub_env


def run_ocr_subprocess(
    ocr_executable: str,
    context: Dict[str, Any],
    *,
    environ: Mapping[str, str],
    timeout_seconds: float,
    secrets_to_redact: Sequence[str],
) -> Any:
    """Invoke the OCR executable in an isolated temporary directory with bounded timeout."""
    payload_bytes = (json.dumps(context, indent=2) + "\n").encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="sciflow-cicd-ocr-ctx-") as tmpdir:
        os.chmod(tmpdir, 0o700)
        ctx_path = Path(tmpdir) / "pr-context.json"
        ctx_path.write_bytes(payload_bytes)
        os.chmod(ctx_path, 0o600)
        sub_env = build_ocr_subprocess_env(environ, context, ctx_path)
        cmd = [
            ocr_executable,
            "--repo",
            str(context["repository"]),
            "--pr",
            str(context["pr_number"]),
            "--head-sha",
            str(context["head_sha"]),
            "--context",
            str(ctx_path),
        ]
        try:
            proc = subprocess.run(
                cmd,
                input=payload_bytes,
                capture_output=True,
                env=sub_env,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise OcrExecutionError(
                f"OCR executable timed out after {timeout_seconds:.1f}s for "
                f"{context['repository']}#{context['pr_number']}."
            ) from None
        except OSError as exc:
            raise OcrExecutionError(
                redact_secrets(f"Failed executing OCR executable: {exc}", secrets_to_redact)
            ) from None

    stdout_text = proc.stdout.decode("utf-8", errors="replace").strip()
    stderr_text = proc.stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        detail = stderr_text or stdout_text or f"exit code {proc.returncode}"
        raise OcrExecutionError(
            redact_secrets(f"OCR executable failed (rc={proc.returncode}): {detail}", secrets_to_redact)
        )
    if not stdout_text:
        raise OcrExecutionError("OCR executable produced empty output.")

    try:
        parsed = json.loads(stdout_text)
    except ValueError:
        return {"summary": stdout_text, "findings": []}

    if isinstance(parsed, dict):
        return parsed
    return {"summary": stdout_text, "findings": []}


def validate_and_normalize_ocr_output(
    raw_output: Any,
    *,
    secrets_to_redact: Sequence[str],
) -> Dict[str, Any]:
    """Validate OCR output structure, forbid non-advisory events, and redact secrets."""
    if isinstance(raw_output, str):
        cleaned = raw_output.strip()
        if not cleaned:
            raise OcrExecutionError("OCR output was empty.")
        return {"summary": redact_secrets(cleaned, secrets_to_redact), "findings": []}

    if not isinstance(raw_output, dict):
        raise OcrExecutionError("OCR output must be a JSON object or non-empty text.")

    raw_event = raw_output.get("event")
    if raw_event is not None and str(raw_event).strip().upper() != "COMMENT":
        raise OcrReviewError(
            f"OCR output requested forbidden review event {raw_event!r}; only advisory 'COMMENT' is allowed."
        )

    summary = str(raw_output.get("summary") or "").strip()
    raw_findings = raw_output.get("findings")
    findings: List[Dict[str, Any]] = []
    if isinstance(raw_findings, list):
        for item in raw_findings[:50]:
            if isinstance(item, dict):
                findings.append(
                    {
                        "file": str(item.get("file") or ""),
                        "line": item.get("line"),
                        "severity": str(item.get("severity") or "info"),
                        "message": redact_secrets(str(item.get("message") or "").strip(), secrets_to_redact),
                    }
                )
            elif isinstance(item, str) and item.strip():
                findings.append(
                    {
                        "file": "",
                        "line": None,
                        "severity": "info",
                        "message": redact_secrets(item.strip(), secrets_to_redact),
                    }
                )

    if not summary and not findings:
        raise OcrExecutionError("OCR output contained neither a summary nor findings.")

    return {
        "summary": redact_secrets(summary or "Automated advisory OCR review completed.", secrets_to_redact),
        "findings": findings,
    }


def execute_ocr_for_pr(
    repo_client: Any,
    pr_payload: Dict[str, Any],
    repository: str,
    pr_number: int,
    *,
    ocr_executable: str,
    ocr_executor: Optional[Callable[[Dict[str, Any]], Any]],
    ocr_timeout_seconds: float,
    environ: Mapping[str, str],
    secrets_to_redact: Sequence[str],
) -> Dict[str, Any]:
    """Fetch PR files, build sanitized context, run OCR, and return normalized findings."""
    files = fetch_pr_files(repo_client, repository, pr_number)
    context = build_sanitized_context(pr_payload, repository, files)
    if ocr_executor is not None:
        raw_ocr = ocr_executor(context)
    else:
        raw_ocr = run_ocr_subprocess(
            ocr_executable,
            context,
            environ=environ,
            timeout_seconds=ocr_timeout_seconds,
            secrets_to_redact=secrets_to_redact,
        )
    return validate_and_normalize_ocr_output(
        raw_ocr,
        secrets_to_redact=secrets_to_redact,
    )
