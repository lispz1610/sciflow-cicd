#!/usr/bin/env python3
"""Shared standard-library GitHub REST client.

This module is the single place for GitHub REST transport concerns: request
headers, bounded response reads, error mapping and pagination. Domain modules
subclass or delegate to :class:`GitHubApi` instead of constructing their own
``urllib`` boilerplate.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
import urllib.error
import urllib.request
from urllib.request import Request

API_VERSION = "2022-11-28"
DEFAULT_BASE_URL = "https://api.github.com"
DEFAULT_TIMEOUT = 20
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_PAGES = 100


def urlopen(request: Any, *, timeout: int = DEFAULT_TIMEOUT) -> Any:
    """Delegate to ``urllib.request.urlopen`` so callers can patch either seam."""
    return urllib.request.urlopen(request, timeout=timeout)


class ApiError(RuntimeError):
    """Raised when a GitHub API request cannot be completed or verified."""


class GitHubHttpError(ApiError):
    """API error that retains the HTTP status and normalized response headers."""

    def __init__(self, status_code: int, message: str, headers: Optional[Mapping[str, str]] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.headers = normalize_headers(headers)


def normalize_headers(headers: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """Return response headers as a lowercase string mapping."""
    if not headers:
        return {}
    return {str(key).strip().lower(): str(value).strip() for key, value in headers.items()}


def read_bounded_response(
    response: Any,
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
    context: str = "HTTP response",
) -> bytes:
    """Read at most ``max_bytes`` from an HTTP response, failing closed on overflow."""
    raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ApiError(f"{context}: response exceeded MAX_RESPONSE_BYTES ({max_bytes} bytes)")
    return raw


def _parse_body(raw: bytes) -> Any:
    return json.loads(raw) if raw else None


def _message_from_body(raw: bytes, fallback: str) -> str:
    if not raw:
        return fallback
    try:
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return raw.decode("utf-8", errors="replace")
    if isinstance(parsed, dict) and isinstance(parsed.get("message"), str):
        return parsed["message"]
    return fallback


class GitHubApi:
    """Minimal, bounded GitHub REST client shared by all repository tooling."""

    def __init__(
        self,
        token: Optional[str],
        base_url: str = DEFAULT_BASE_URL,
        *,
        user_agent: str = "sciflow-cicd",
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.token = token or ""
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.max_response_bytes = max_response_bytes
        self.timeout = timeout
        self.scopes: Optional[Sequence[str]] = None

    def _request_headers(self, data: Optional[bytes]) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": self.user_agent,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        return headers

    def _capture_scopes(self, headers: Optional[Mapping[str, Any]]) -> None:
        value = None
        if headers is not None and hasattr(headers, "get"):
            value = headers.get("X-OAuth-Scopes")
        if value is not None:
            self.scopes = [scope.strip() for scope in str(value).split(",") if scope.strip()]

    def request_with_headers(
        self,
        method: str,
        path: str,
        payload: Optional[Any] = None,
    ) -> Tuple[int, Dict[str, str], Any]:
        """Return ``(status, headers, body)`` without raising for HTTP error statuses.

        Network failures and oversized responses raise :class:`ApiError`. A GET
        404 is returned as an empty body so callers can distinguish absence from
        failure, matching the historical repository-configuration contract.
        """
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=data,
            headers=self._request_headers(data),
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = read_bounded_response(
                    response,
                    max_bytes=self.max_response_bytes,
                    context=f"{method} {path}",
                )
                self._capture_scopes(getattr(response, "headers", None))
                status = getattr(response, "status", 200)
                if not isinstance(status, int):  # tolerate simple test doubles
                    status = 200
                return status, normalize_headers(getattr(response, "headers", None)), _parse_body(raw)
        except urllib.error.HTTPError as error:
            headers = normalize_headers(getattr(error, "headers", None))
            self._capture_scopes(getattr(error, "headers", None))
            raw = error.read(self.max_response_bytes)
            error.close()
            if method == "GET" and error.code == 404:
                return 404, headers, None
            return error.code, headers, {"message": _message_from_body(raw, f"HTTP {error.code}")}
        except urllib.error.URLError as error:
            raise ApiError(f"{method} {path}: {error.reason}") from None

    def request(
        self,
        method: str,
        path: str,
        payload: Optional[Any] = None,
    ) -> Tuple[int, Any]:
        """Return ``(status, body)``, raising :class:`ApiError` on failure statuses."""
        status, _headers, body = self.request_with_headers(method, path, payload=payload)
        if status >= 400 and not (method == "GET" and status == 404):
            message = body.get("message", f"HTTP {status}") if isinstance(body, dict) else f"HTTP {status}"
            raise ApiError(f"{method} {path}: HTTP {status}: {message}")
        return status, body

    def paginate(
        self,
        path: str,
        *,
        key: Optional[str] = None,
        per_page: int = 100,
        max_pages: int = MAX_PAGES,
    ) -> list[Any]:
        """Collect a complete small REST collection, failing closed on bad shapes."""
        items: list[Any] = []
        for page in range(1, max_pages + 1):
            separator = "&" if "?" in path else "?"
            status, body = self.request("GET", f"{path}{separator}per_page={per_page}&page={page}")
            if status != 200:
                raise ApiError(f"GET {path}: HTTP {status}")
            batch = body.get(key) if key and isinstance(body, dict) else body
            if not isinstance(batch, list):
                raise ApiError(f"cannot verify paginated {key or 'list'} response")
            items.extend(batch)
            if len(batch) < per_page:
                return items
        raise ApiError(f"collection exceeds {max_pages} pages; cannot verify complete preflight")


def get(api: GitHubApi, path: str) -> Any:
    status, body = api.request("GET", path)
    if status != 200:
        raise ApiError(f"GET {path}: HTTP {status}; check repository and token permissions")
    return body


def get_object(api: GitHubApi, path: str) -> Dict[str, Any]:
    body = get(api, path)
    if not isinstance(body, dict):
        raise ApiError(f"GET {path}: cannot verify malformed response")
    return body


def get_pages(api: GitHubApi, path: str, key: Optional[str] = None) -> list[Any]:
    """Read complete small GitHub collections; reject unexpected response shapes."""
    return api.paginate(path, key=key) if hasattr(api, "paginate") else _get_pages_compat(api, path, key)


def _get_pages_compat(api: Any, path: str, key: Optional[str]) -> list[Any]:
    items: list[Any] = []
    for page in range(1, MAX_PAGES + 1):
        separator = "&" if "?" in path else "?"
        body = get(api, f"{path}{separator}per_page=100&page={page}")
        batch = body.get(key) if key and isinstance(body, dict) else body
        if not isinstance(batch, list):
            raise ApiError(f"cannot verify paginated {key or 'list'} response")
        items.extend(batch)
        if len(batch) < 100:
            return items
    raise ApiError("collection exceeds 100 pages; cannot verify complete preflight")
