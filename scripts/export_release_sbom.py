#!/usr/bin/env python3
"""Export standardized SPDX SBOM from GitHub Dependency Graph API."""

from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, Optional, Sequence, Tuple, Union
import urllib.error
import urllib.request

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from scripts.verify_release import sanitize_credentials
except ImportError:
    try:
        from verify_release import sanitize_credentials
    except ImportError:
        def sanitize_credentials(text: str) -> str:  # type: ignore
            if not isinstance(text, str):
                return text
            return re.sub(r"gh[opusr]_[A-Za-z0-9_]{8,}", "[REDACTED]", text)


class SbomError(RuntimeError):
    """Base exception for SBOM retrieval and formatting failures."""

    def __init__(self, message: str, recovery: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.recovery = recovery

    def __str__(self) -> str:
        if self.recovery:
            return f"{self.message}. Recovery: {self.recovery}"
        return self.message


class SbomExportError(SbomError):
    """Raised when the GitHub Dependency Graph API request fails."""


class SbomFormatError(SbomError):
    """Raised when the SBOM API response is malformed or invalid."""


class SbomValidationError(SbomError):
    """Raised when the SBOM fails validation rules (e.g. missing or empty packages)."""


def get_sbom_recovery(reason: str, status: Optional[int] = None) -> str:
    """Return an actionable recovery instruction based on failure reason or HTTP status."""
    lower = reason.lower()
    if status == 404 or "404" in lower or "not enabled" in lower or "not found" in lower:
        return (
            "Enable Dependency Graph in repository settings under Settings → "
            "Code security and analysis → Dependency graph."
        )
    if status in (401, 403) or "token" in lower or "unauthorized" in lower or "admin rights" in lower:
        return "Ensure GITHUB_TOKEN or GH_TOKEN is provided with 'contents: read' access to the Dependency Graph API."
    if "empty" in lower or "no packages" in lower or "unindexed" in lower or "packages" in lower:
        return (
            "Ensure dependency manifests (e.g. requirements.txt, pyproject.toml) are present, "
            "submit dependency snapshots, or retry after dependency graph indexing completes."
        )
    return "Check Dependency Graph API availability and repository settings, or retry after indexing completes."


class SbomGitHubApi:
    """Minimal HTTP client for Dependency Graph API requests."""

    def __init__(self, token: Optional[str] = None, base_url: str = "https://api.github.com"):
        self.token = token
        self.base_url = base_url.rstrip("/")

    def request(self, method: str, path: str, payload: Optional[Any] = None) -> Tuple[int, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-repo-setup-sbom",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        url = self.base_url + path
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                body = {"message": raw}
            return error.code, body
        except urllib.error.URLError as error:
            raise SbomExportError(f"{method} {path}: {error.reason}") from None


def create_api_client(token: Optional[str] = None, api_url: str = "https://api.github.com") -> Any:
    return SbomGitHubApi(token=token, base_url=api_url)


def _handle_api_failure(
    repo: str,
    reason: str,
    allow_fallback: bool,
    exc: Optional[Exception] = None,
    status: Optional[int] = None,
) -> Dict[str, Any]:
    """Emit a warning and return a fallback SBOM, or raise when fallback is disabled."""
    if not allow_fallback:
        recovery = get_sbom_recovery(reason, status=status)
        if exc is not None:
            if isinstance(exc, SbomError) and not exc.recovery:
                exc.recovery = recovery
            raise exc
        raise SbomExportError(
            f"Failed to export SBOM for {repo}: {sanitize_credentials(reason)}",
            recovery=recovery,
        )
    sanitized = sanitize_credentials(str(reason))
    print(
        f"Warning: GitHub Dependency Graph SBOM unavailable: {sanitized}. Generating fallback SPDX document.",
        file=sys.stderr,
    )
    return create_fallback_sbom(repo, reason)


def fetch_dependency_graph_sbom(
    repo: str,
    token: Optional[str] = None,
    api_url: str = "https://api.github.com",
    api: Any = None,
) -> Tuple[int, Any]:
    """Query GitHub Dependency Graph API for repository SBOM."""
    path = f"/repos/{repo}/dependency-graph/sbom"
    client = api if api is not None else SbomGitHubApi(token=token, base_url=api_url)
    try:
        status, body = client.request("GET", path)
        return status, body
    except Exception as error:
        sanitized = sanitize_credentials(str(error))
        raise SbomExportError(f"GitHub API request failed: {sanitized}") from None


def format_spdx_document(payload: Any, repo: str, require_packages: bool = False) -> Dict[str, Any]:
    """Extract and validate SPDX document structure from GitHub API payload."""
    if not isinstance(payload, dict):
        raise SbomFormatError("SBOM payload must be a JSON object", recovery=get_sbom_recovery("format error"))
    doc = payload.get("sbom") if "sbom" in payload and isinstance(payload.get("sbom"), dict) else payload
    if not isinstance(doc, dict):
        raise SbomFormatError("SPDX document must be a JSON object", recovery=get_sbom_recovery("format error"))
    if "spdxVersion" not in doc or not isinstance(doc["spdxVersion"], str):
        raise SbomFormatError("SPDX document missing 'spdxVersion'", recovery=get_sbom_recovery("format error"))
    if not doc["spdxVersion"].startswith("SPDX-"):
        raise SbomFormatError(f"Invalid spdxVersion format: {doc['spdxVersion']!r}", recovery=get_sbom_recovery("format error"))
    if "SPDXID" not in doc or not isinstance(doc["SPDXID"], str):
        raise SbomFormatError("SPDX document missing 'SPDXID'", recovery=get_sbom_recovery("format error"))
    if require_packages:
        packages = doc.get("packages")
        if not isinstance(packages, list) or len(packages) == 0:
            raise SbomValidationError(
                f"SPDX SBOM for {repo} contains no packages (empty or unindexed dependency graph)",
                recovery=get_sbom_recovery("empty packages"),
            )
    return doc


def create_fallback_sbom(repo: str, reason: str = "Dependency graph unavailable") -> Dict[str, Any]:
    """Generate a valid minimal SPDX 2.3 document when dependency graph is unindexed or disabled."""
    sanitized_reason = sanitize_credentials(str(reason))
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": repo,
        "documentNamespace": f"https://github.com/{repo}/dependency_graph/sbom/fallback",
        "creationInfo": {
            "created": now,
            "creators": [
                "Tool: GitHub-Repo-Setup-SBOM-Fallback"
            ],
            "comment": sanitized_reason,
        },
        "packages": [],
        "relationships": [],
        "comment": f"Fallback SBOM generated because dependency graph is unavailable: {sanitized_reason}",
    }


def export_release_sbom(
    repo: str,
    output_path: Union[str, Path],
    token: Optional[str] = None,
    api_url: str = "https://api.github.com",
    api: Any = None,
    allow_fallback: bool = True,
    require_packages: bool = False,
) -> Dict[str, Any]:
    """Retrieve SBOM from GitHub Dependency Graph API or generate fallback, then write to output."""
    if not isinstance(repo, str) or not repo.strip() or "/" not in repo:
        raise SbomExportError(f"Invalid repository specification: {repo!r}")

    resolved_token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    doc: Optional[Dict[str, Any]] = None
    if api is None and not resolved_token:
        msg = "No GITHUB_TOKEN or GH_TOKEN provided"
        doc = _handle_api_failure(
            repo,
            msg,
            allow_fallback=allow_fallback,
            exc=SbomExportError(
                "GITHUB_TOKEN or GH_TOKEN is required to export release SBOM",
                recovery=get_sbom_recovery(msg, status=401),
            ),
            status=401,
        )
    else:
        try:
            status, body = fetch_dependency_graph_sbom(
                repo=repo,
                token=resolved_token,
                api_url=api_url,
                api=api,
            )
        except SbomExportError as error:
            if not error.recovery:
                error.recovery = get_sbom_recovery(str(error))
            doc = _handle_api_failure(repo, str(error), allow_fallback=allow_fallback, exc=error)
        else:
            if status == 200:
                try:
                    doc = format_spdx_document(body, repo, require_packages=require_packages)
                except (SbomFormatError, SbomValidationError) as error:
                    doc = _handle_api_failure(
                        repo,
                        f"Malformed SBOM payload: {error}",
                        allow_fallback=allow_fallback,
                        exc=error,
                        status=status,
                    )
            else:
                msg = f"HTTP {status}"
                if isinstance(body, dict) and body.get("message"):
                    msg += f": {body['message']}"
                doc = _handle_api_failure(
                    repo,
                    msg,
                    allow_fallback=allow_fallback,
                    exc=SbomExportError(
                        f"Failed to export SBOM for {repo}: {sanitize_credentials(msg)}",
                        recovery=get_sbom_recovery(msg, status=status),
                    ),
                    status=status,
                )

    if doc is None:
        raise SbomExportError("Failed to obtain SBOM document")

    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return doc


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="Target GitHub repository (OWNER/REPO)",
    )
    parser.add_argument(
        "--output",
        default=Path("sbom.spdx.json"),
        type=Path,
        help="Output path for SPDX JSON (default: sbom.spdx.json)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub token (defaults to GITHUB_TOKEN or GH_TOKEN env var)",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        help="GitHub API base URL",
    )
    parser.add_argument(
        "--no-fallback",
        dest="allow_fallback",
        action="store_false",
        default=True,
        help="Disable fallback and fail on Dependency Graph API error",
    )
    parser.add_argument(
        "--require-packages",
        dest="require_packages",
        action="store_true",
        default=False,
        help="Require non-empty packages list in generated SBOM",
    )
    args = parser.parse_args(argv)

    if not args.repo:
        print("Error: --repo or GITHUB_REPOSITORY environment variable is required", file=sys.stderr)
        return 1

    require_packages = args.require_packages or not args.allow_fallback

    try:
        export_release_sbom(
            repo=args.repo,
            output_path=args.output,
            token=args.token,
            api_url=args.api_url,
            allow_fallback=args.allow_fallback,
            require_packages=require_packages,
        )
    except (SbomError, OSError) as error:
        print(f"Failed to export SBOM: {sanitize_credentials(str(error))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
