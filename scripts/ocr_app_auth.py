"""Limited GitHub App preflight and repository-scoped installation token manager for OCR."""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TextIO, Tuple, Union
from urllib.parse import urlparse

try:
    from scripts.configure_github_repo import ApiError, GitHubApi, get_object, get_pages
except ImportError:
    from configure_github_repo import ApiError, GitHubApi, get_object, get_pages

try:
    from scripts.path_security import reject_symlink_components
except ImportError:
    from path_security import reject_symlink_components

REQUIRED_APP_PERMISSIONS: Dict[str, str] = {
    "contents": "read",
    "pull_requests": "write",
    "metadata": "read",
}
FORBIDDEN_APP_PERMISSIONS = (
    "administration",
    "organization_administration",
    "secrets",
    "organization_secrets",
    "dependabot_secrets",
    "organization_dependabot_secrets",
    "codespaces_secrets",
    "codespaces_user_secrets",
    "organization_codespaces_secrets",
    "environments",
    "repository_hooks",
    "organization_hooks",
    "members",
    "organization_self_hosted_runners",
    "organization_personal_access_tokens",
    "organization_personal_access_token_requests",
    "organization_custom_roles",
    "organization_custom_org_roles",
    "repository_custom_properties",
    "organization_custom_properties",
    "organization_user_blocking",
)
_FORBIDDEN_APP_PERMISSION_SET = frozenset(FORBIDDEN_APP_PERMISSIONS)
_PERMISSION_RANK = {"none": 0, "read": 1, "write": 2, "admin": 3}
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "sciflow-cicd"
LEGACY_CONFIG_DIR = Path.home() / ".config" / "gh-setup"
ADMIN_APP_ID_ENV_VARS = (
    "SCIFLOW_CICD_ADMIN_APP_ID",
    "SCIFLOW_CICD_CLIENT_APP_ID",
    "GH_SETUP_ADMIN_APP_ID",
    "GH_SETUP_CLIENT_APP_ID",
)


def resolve_config_dir() -> Path:
    """Return the configuration directory for sciflow-cicd, supporting legacy fallback."""
    if DEFAULT_CONFIG_DIR.exists() or not LEGACY_CONFIG_DIR.exists():
        return DEFAULT_CONFIG_DIR
    return LEGACY_CONFIG_DIR
DEFAULT_OCR_SECRET_ENV_KEYS: Tuple[str, ...] = (
    "OCR_LLM_AUTH_TOKEN",
    "RUNNER_REGISTRATION_TOKEN",
    "ACTIONS_RUNNER_INPUT_TOKEN",
)

DEFAULT_JWT_TTL_SECONDS = 540
MAX_JWT_LIFETIME_SECONDS = 600
JWT_CLOCK_SKEW_SECONDS = 60
DEFAULT_REFRESH_BUFFER_SECONDS = 120

_SECRET_REDACTION_PATTERNS = (
    re.compile(
        r"-----BEGIN [A-Z0-9 _-]+PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 _-]+PRIVATE KEY-----",
        re.MULTILINE,
    ),
    re.compile(r"-----BEGIN [A-Z0-9 _-]+PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b"),
    re.compile(r"\bgh[opusr]_[A-Za-z0-9_]{6,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{10,}\b"),
    re.compile(r"(Authorization:\s*Bearer\s+)[^\s'\"]+", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}", re.IGNORECASE),
)


class OcrAppAuthError(RuntimeError):
    """Raised when limited GitHub App validation or token minting fails."""


def redact_secrets(text: str, extra_secrets: Sequence[str] = ()) -> str:
    """Redact private keys, JWTs, and GitHub tokens from diagnostic text."""
    if not isinstance(text, str):
        return str(text)
    redacted = text
    for secret in extra_secrets:
        if isinstance(secret, str) and len(secret) >= 6:
            redacted = redacted.replace(secret, "[REDACTED]")
    for pattern in _SECRET_REDACTION_PATTERNS:
        if pattern.pattern.startswith("(Authorization:"):
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def collect_env_secrets(
    env: Optional[Mapping[str, str]] = None,
    keys: Sequence[str] = DEFAULT_OCR_SECRET_ENV_KEYS,
) -> List[str]:
    """Collect non-empty secret values from environment variables for redaction."""
    environ = env if env is not None else os.environ
    secrets: List[str] = []
    for key in keys:
        val = environ.get(key)
        if isinstance(val, str) and val:
            secrets.append(val)
    return secrets


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def normalize_app_id(app_id: Union[str, int]) -> str:
    """Validate and normalize a GitHub App ID to a canonical decimal string."""
    if isinstance(app_id, bool):
        raise OcrAppAuthError("GitHub App ID must be a positive integer")
    raw = str(app_id).strip()
    if not raw.isdigit() or int(raw) <= 0:
        raise OcrAppAuthError("GitHub App ID must be a positive integer")
    return str(int(raw))


@dataclass(frozen=True)
class OcrAppConfig:
    """Immutable limited GitHub App credential, allowlist, and API scope configuration."""

    app_id: Union[str, int]
    private_key_path: Optional[Union[str, Path]] = None
    allowlist: Tuple[str, ...] = ()
    admin_app_ids: Tuple[Union[str, int], ...] = ()
    api_base_url: str = "https://api.github.com"
    api_factory: Optional[Callable[[str], Any]] = None
    signer: Optional[Callable[[bytes], bytes]] = None

    def with_allowlist(self, allowlist: Sequence[str]) -> "OcrAppConfig":
        """Return a copy of this configuration with an updated repository allowlist."""
        return replace(self, allowlist=tuple(allowlist))

    def resolve_client_factory(self) -> Callable[[str], Any]:
        """Return explicit api_factory or the default factory for api_base_url."""
        return self.api_factory if self.api_factory is not None else default_api_factory(self.api_base_url)

    def build_token_manager(
        self,
        *,
        client_factory: Optional[Callable[[str], Any]] = None,
        refresh_buffer_seconds: int = DEFAULT_REFRESH_BUFFER_SECONDS,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> "ScopedInstallationTokenManager":
        """Construct a ScopedInstallationTokenManager from this configuration."""
        effective_cfg = replace(self, api_factory=client_factory) if client_factory is not None else self
        return ScopedInstallationTokenManager(
            app_config=effective_cfg,
            refresh_buffer_seconds=refresh_buffer_seconds,
            now_fn=now_fn,
        )


def resolve_app_config(
    app_config: Optional[OcrAppConfig] = None,
    *,
    app_id: Optional[Union[str, int]] = None,
    private_key_path: Optional[Union[str, Path]] = None,
    allowlist: Sequence[str] = (),
    admin_app_ids: Sequence[Union[str, int]] = (),
    api_base_url: str = "https://api.github.com",
    api_factory: Optional[Callable[[str], Any]] = None,
    signer: Optional[Callable[[bytes], bytes]] = None,
    expand_allowlist: bool = False,
    merge_env_admin_ids: bool = False,
    env: Optional[Mapping[str, str]] = None,
) -> OcrAppConfig:
    """Return an explicit OcrAppConfig or build one from legacy keyword parameters."""
    if app_config is not None:
        cfg = app_config
    else:
        if app_id is None:
            raise OcrAppAuthError("GitHub App ID must be a positive integer")
        cfg = OcrAppConfig(
            app_id=app_id,
            private_key_path=private_key_path,
            allowlist=tuple(allowlist),
            admin_app_ids=tuple(admin_app_ids),
            api_base_url=api_base_url,
            api_factory=api_factory,
            signer=signer,
        )
    if not expand_allowlist and not merge_env_admin_ids:
        return cfg
    new_allowlist = (
        tuple(split_csv_values([str(x) for x in cfg.allowlist]))
        if expand_allowlist
        else cfg.allowlist
    )
    new_admin_ids = (
        tuple(merge_admin_app_ids(cfg.admin_app_ids, env=env))
        if merge_env_admin_ids
        else cfg.admin_app_ids
    )
    return replace(cfg, allowlist=new_allowlist, admin_app_ids=new_admin_ids)


def validate_private_key_path(private_key_path: Union[str, Path]) -> Path:
    """Validate that private_key_path is a non-symlink, regular, owner-only PEM file."""
    if not private_key_path:
        raise OcrAppAuthError("Private key path is required")
    path = Path(private_key_path)
    reject_symlink_components(
        path,
        include_leaf=True,
        error_cls=OcrAppAuthError,
        message_format="Symlinks are prohibited in private key path: {piece}",
    )
    if not path.exists():
        raise OcrAppAuthError(f"Private key file does not exist: {path}")
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise OcrAppAuthError(f"Cannot inspect private key file {path}: {exc}") from None
    if stat.S_ISLNK(st.st_mode):
        raise OcrAppAuthError(f"Symlinks are prohibited in private key path: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise OcrAppAuthError(f"Private key path must be a regular file: {path}")
    if os.name == "posix" and (st.st_mode & 0o077) != 0:
        raise OcrAppAuthError(
            f"Private key file {path} has overly permissive permissions; restrict to owner-only (chmod 600)."
        )
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OcrAppAuthError(f"Cannot read private key file {path}: {exc}") from None
    if "PRIVATE KEY" not in content:
        raise OcrAppAuthError(f"Private key file {path} does not appear to contain a PEM private key")
    return path


def sign_rs256_openssl(signing_input: bytes, private_key_path: Path) -> bytes:
    """Sign JWT payload with RS256 using the system openssl CLI."""
    try:
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(private_key_path)],
            input=signing_input,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        raise OcrAppAuthError("openssl executable was not found in PATH") from None
    except OSError as exc:
        raise OcrAppAuthError(redact_secrets(f"Failed to execute openssl: {exc}")) from None

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise OcrAppAuthError(
            redact_secrets(f"openssl RS256 signing failed: {stderr or f'exit code {proc.returncode}'}")
        )
    if not proc.stdout:
        raise OcrAppAuthError("openssl RS256 signing produced an empty signature")
    return proc.stdout


def generate_app_jwt(
    app_id: Union[str, int],
    private_key_path: Optional[Union[str, Path]] = None,
    *,
    now: Optional[int] = None,
    ttl_seconds: int = DEFAULT_JWT_TTL_SECONDS,
    signer: Optional[Callable[[bytes], bytes]] = None,
) -> str:
    """Generate a short-lived RS256 GitHub App JWT (<= 10 minutes total lifetime)."""
    normalized_id = normalize_app_id(app_id)
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise OcrAppAuthError("JWT ttl_seconds must be a positive integer")
    if ttl_seconds + JWT_CLOCK_SKEW_SECONDS > MAX_JWT_LIFETIME_SECONDS:
        raise OcrAppAuthError(
            f"JWT lifetime exceeds GitHub maximum of {MAX_JWT_LIFETIME_SECONDS} seconds"
        )

    current_ts = int(time.time()) if now is None else int(now)
    iat = current_ts - JWT_CLOCK_SKEW_SECONDS
    exp = current_ts + ttl_seconds

    header_b64 = _b64url_encode(
        json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8")
    )
    payload_b64 = _b64url_encode(
        json.dumps({"iat": iat, "exp": exp, "iss": normalized_id}, separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")

    if signer is not None:
        try:
            signature = signer(signing_input)
        except OcrAppAuthError:
            raise
        except Exception as exc:
            raise OcrAppAuthError(redact_secrets(f"JWT signing failed: {exc}")) from None
    else:
        if private_key_path is None:
            raise OcrAppAuthError("private_key_path is required when no custom signer is provided")
        validated_path = validate_private_key_path(private_key_path)
        signature = sign_rs256_openssl(signing_input, validated_path)

    if not isinstance(signature, (bytes, bytearray)) or not signature:
        raise OcrAppAuthError("JWT signer returned an empty or invalid signature")
    return f"{header_b64}.{payload_b64}.{_b64url_encode(bytes(signature))}"


@dataclass(frozen=True)
class AppIdentity:
    """Sanitized GitHub App identity metadata."""

    app_id: str
    slug: str
    name: str
    permissions: Dict[str, str]


@dataclass(frozen=True)
class RepositoryPreflightDecision:
    """Allowlist evaluation decision for a single target repository."""

    repository: str
    eligible: bool
    installation_id: Optional[int]
    permissions: Dict[str, str]
    reason: Optional[str] = None
    setup_instruction: Optional[str] = None


@dataclass
class AppPreflightResult:
    """Structured result of limited GitHub App preflight."""

    passed: bool
    app: Optional[AppIdentity]
    installations: List[Dict[str, Any]]
    allowlist: List[str]
    eligible_repositories: List[str]
    excluded_repositories: List[RepositoryPreflightDecision]
    decisions: List[RepositoryPreflightDecision]
    required_permissions: Dict[str, str] = field(default_factory=lambda: dict(REQUIRED_APP_PERMISSIONS))
    errors: List[str] = field(default_factory=list)

    def to_dict(self, *, extra_secrets: Sequence[str] = ()) -> Dict[str, Any]:
        """Return a JSON-serializable, secret-redacted representation."""
        raw = {
            "passed": self.passed,
            "app": asdict(self.app) if self.app is not None else None,
            "installations": list(self.installations),
            "allowlist": list(self.allowlist),
            "eligible_repositories": list(self.eligible_repositories),
            "excluded_repositories": [asdict(item) for item in self.excluded_repositories],
            "decisions": [asdict(item) for item in self.decisions],
            "required_permissions": dict(self.required_permissions),
            "errors": [redact_secrets(err, extra_secrets) for err in self.errors],
        }
        return json.loads(redact_secrets(json.dumps(raw), extra_secrets))


def _normalize_permissions(raw_permissions: Any) -> Dict[str, str]:
    if not isinstance(raw_permissions, dict):
        return {}
    normalized: Dict[str, str] = {}
    for key, val in raw_permissions.items():
        if isinstance(key, str) and val is not None:
            normalized[key.strip().lower()] = (
                val.strip().lower() if isinstance(val, str) else str(val).strip().lower()
            )
    return normalized


def _has_forbidden_permissions(permissions: Dict[str, str]) -> List[str]:
    found = []
    for name, level in sorted(permissions.items()):
        if not level or level == "none":
            continue
        if name in _FORBIDDEN_APP_PERMISSION_SET:
            found.append(f"{name}:{level}")
            continue
        level_rank = _PERMISSION_RANK.get(level)
        if name in REQUIRED_APP_PERMISSIONS:
            max_level = REQUIRED_APP_PERMISSIONS[name]
            if level_rank is None or level_rank > _PERMISSION_RANK[max_level]:
                found.append(f"{name}:{level}")
            continue
        # Reject any non-OCR write/admin (or unrecognized non-read) permission scope.
        if level_rank is None or level_rank >= _PERMISSION_RANK["write"]:
            found.append(f"{name}:{level}")
    return found


def _missing_required_permissions(permissions: Dict[str, str]) -> List[str]:
    missing = []
    for perm, required_level in REQUIRED_APP_PERMISSIONS.items():
        actual_level = permissions.get(perm, "none")
        if _PERMISSION_RANK.get(actual_level, 0) < _PERMISSION_RANK[required_level]:
            missing.append(f"{perm}:{required_level} (current: {actual_level})")
    return missing


def _normalize_allowlist(allowlist: Sequence[str]) -> Tuple[List[str], List[str]]:
    seen_lower = set()
    valid: List[str] = []
    errors: List[str] = []
    for entry in allowlist:
        if not isinstance(entry, str) or not entry.strip():
            errors.append("Allowlist contains an empty repository entry.")
            continue
        candidate = entry.strip()
        if not REPO_PATTERN.match(candidate):
            errors.append(f"Invalid repository format in allowlist: '{candidate}' (expected OWNER/REPO).")
            continue
        key = candidate.lower()
        if key not in seen_lower:
            seen_lower.add(key)
            valid.append(candidate)
    return valid, errors


def validate_api_base_url(api_base_url: str) -> str:
    """Validate that api_base_url uses HTTPS or loopback HTTP for local tests."""
    parsed = urlparse(api_base_url.strip())
    if parsed.scheme == "https" and parsed.netloc:
        return api_base_url.strip().rstrip("/")
    if parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1"):
        return api_base_url.strip().rstrip("/")
    raise OcrAppAuthError(
        f"API base URL must use HTTPS (or loopback HTTP for local tests): {api_base_url!r}"
    )


def default_api_factory(
    api_base_url: str,
    *,
    client_cls: Any = GitHubApi,
) -> Callable[[str], Any]:
    """Return a factory constructing GitHub API clients for a validated base URL."""
    validated_url = validate_api_base_url(api_base_url)
    return lambda token: client_cls(token, base_url=validated_url)


def dispatch_client_request(
    client: Any,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, str], Any]:
    """Invoke a GitHub client's request_with_headers or request method uniformly."""
    if hasattr(client, "request_with_headers"):
        if payload is not None:
            status, raw_headers, body = client.request_with_headers(method, path, payload=payload)
        else:
            status, raw_headers, body = client.request_with_headers(method, path)
        headers = (
            {str(k).strip().lower(): str(v).strip() for k, v in raw_headers.items()}
            if isinstance(raw_headers, dict)
            else {}
        )
        return status, headers, body
    if payload is not None:
        status, body = client.request(method, path, payload=payload)
    else:
        status, body = client.request(method, path)
    return status, {}, body


def client_request(
    client: Any,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    path_validator: Optional[Callable[[str], str]] = None,
) -> Tuple[int, Any]:
    """Validate path if requested and invoke client request returning (status, body)."""
    target_path = path_validator(path) if path_validator is not None else path
    status, _headers, body = dispatch_client_request(client, method, target_path, payload=payload)
    return status, body


def preflight_ocr_app(
    app_id: Optional[Union[str, int]] = None,
    private_key_path: Optional[Union[str, Path]] = None,
    allowlist: Sequence[str] = (),
    *,
    admin_app_ids: Sequence[Union[str, int]] = (),
    api_base_url: str = "https://api.github.com",
    api_factory: Optional[Callable[[str], Any]] = None,
    signer: Optional[Callable[[bytes], bytes]] = None,
    app_config: Optional[OcrAppConfig] = None,
) -> AppPreflightResult:
    """Validate limited OCR GitHub App identity, installation scope, and repository allowlist."""
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
    normalized_id = normalize_app_id(cfg.app_id)
    normalized_admin_ids = {normalize_app_id(item) for item in cfg.admin_app_ids if str(item).strip()}
    normalized_allowlist, allowlist_errors = _normalize_allowlist(cfg.allowlist)

    if normalized_id in normalized_admin_ids:
        return AppPreflightResult(
            passed=False,
            app=None,
            installations=[],
            allowlist=normalized_allowlist,
            eligible_repositories=[],
            excluded_repositories=[],
            decisions=[],
            errors=[
                f"GitHub App ID {normalized_id} is configured as an administrative/client-setup App "
                "and is rejected for automated OCR. Configure a separate limited OCR GitHub App."
            ],
        )

    if allowlist_errors:
        return AppPreflightResult(
            passed=False,
            app=None,
            installations=[],
            allowlist=normalized_allowlist,
            eligible_repositories=[],
            excluded_repositories=[],
            decisions=[],
            errors=allowlist_errors,
        )

    if not normalized_allowlist:
        return AppPreflightResult(
            passed=False,
            app=None,
            installations=[],
            allowlist=[],
            eligible_repositories=[],
            excluded_repositories=[],
            decisions=[],
            errors=["Explicit repository allowlist is empty; provide at least one OWNER/REPO."],
        )

    factory = cfg.resolve_client_factory()
    jwt_token = generate_app_jwt(normalized_id, cfg.private_key_path, signer=cfg.signer)
    secrets_to_redact = [jwt_token]

    try:
        app_api = factory(jwt_token)
        app_payload = get_object(app_api, "/app")
        raw_installations = get_pages(app_api, "/app/installations")
    except (ApiError, OcrAppAuthError, OSError, ValueError) as exc:
        return AppPreflightResult(
            passed=False,
            app=None,
            installations=[],
            allowlist=normalized_allowlist,
            eligible_repositories=[],
            excluded_repositories=[],
            decisions=[],
            errors=[redact_secrets(f"Failed to query GitHub App metadata: {exc}", secrets_to_redact)],
        )

    app_perms = _normalize_permissions(app_payload.get("permissions"))
    app_identity = AppIdentity(
        app_id=str(app_payload.get("id", normalized_id)),
        slug=str(app_payload.get("slug") or ""),
        name=str(app_payload.get("name") or ""),
        permissions=app_perms,
    )

    if app_identity.app_id in normalized_admin_ids:
        return AppPreflightResult(
            passed=False,
            app=app_identity,
            installations=[],
            allowlist=normalized_allowlist,
            eligible_repositories=[],
            excluded_repositories=[],
            decisions=[],
            errors=[
                f"GitHub App ID {app_identity.app_id} is configured as an administrative/client-setup App "
                "and is rejected for automated OCR. Configure a separate limited OCR GitHub App."
            ],
        )

    forbidden_app_perms = _has_forbidden_permissions(app_perms)
    if forbidden_app_perms:
        return AppPreflightResult(
            passed=False,
            app=app_identity,
            installations=[],
            allowlist=normalized_allowlist,
            eligible_repositories=[],
            excluded_repositories=[],
            decisions=[],
            errors=[
                f"GitHub App '{app_identity.slug or app_identity.app_id}' holds forbidden non-OCR "
                f"privileged permission(s) ({', '.join(forbidden_app_perms)}). Use a limited OCR App "
                "restricted to contents: read, pull_requests: write, and metadata: read."
            ],
        )

    allowlist_owners = {repo.split("/", 1)[0].lower() for repo in normalized_allowlist}
    installations_summary: List[Dict[str, Any]] = []
    installations_by_owner: Dict[str, List[Dict[str, Any]]] = {}
    accessible_repos: Dict[str, Tuple[int, Dict[str, str]]] = {}

    for inst in raw_installations:
        if not isinstance(inst, dict):
            continue
        inst_id = inst.get("id")
        inst_perms = _normalize_permissions(inst.get("permissions"))
        account = inst.get("account") if isinstance(inst.get("account"), dict) else {}
        account_login = str(account.get("login") or "")
        inst_summary = {
            "id": inst_id,
            "account": account_login,
            "repository_selection": inst.get("repository_selection", ""),
            "permissions": inst_perms,
        }
        installations_summary.append(inst_summary)

        forbidden_inst_perms = _has_forbidden_permissions(inst_perms)
        if forbidden_inst_perms:
            return AppPreflightResult(
                passed=False,
                app=app_identity,
                installations=installations_summary,
                allowlist=normalized_allowlist,
                eligible_repositories=[],
                excluded_repositories=[],
                decisions=[],
                errors=[
                    f"Installation {inst_id} holds forbidden non-OCR privileged permission(s) "
                    f"({', '.join(forbidden_inst_perms)}). Remove privileged permissions from the OCR App."
                ],
            )

        owner_key = account_login.lower()
        if owner_key:
            installations_by_owner.setdefault(owner_key, []).append(inst_summary)

        # Only query installation repositories for owners present in the explicit allowlist.
        if owner_key not in allowlist_owners or not isinstance(inst_id, int):
            continue
        if _PERMISSION_RANK.get(inst_perms.get("metadata", "none"), 0) < _PERMISSION_RANK["read"]:
            continue

        try:
            _, token_body = app_api.request(
                "POST",
                f"/app/installations/{inst_id}/access_tokens",
                payload={"permissions": {"metadata": "read"}},
            )
            discovery_token = token_body.get("token") if isinstance(token_body, dict) else None
            if not isinstance(discovery_token, str) or not discovery_token:
                raise OcrAppAuthError(f"Installation {inst_id} did not return a discovery token")
            secrets_to_redact.append(discovery_token)
            discovery_perms = _normalize_permissions(
                token_body.get("permissions") if isinstance(token_body, dict) else None
            )
            forbidden_disc_perms = _has_forbidden_permissions(discovery_perms)
            if forbidden_disc_perms:
                raise OcrAppAuthError(
                    f"Discovery token for installation {inst_id} unexpectedly granted forbidden non-OCR "
                    f"privileged permission(s) ({', '.join(forbidden_disc_perms)})"
                )
            if _PERMISSION_RANK.get(discovery_perms.get("metadata", "none"), 0) < _PERMISSION_RANK["read"]:
                raise OcrAppAuthError(
                    f"Discovery token for installation {inst_id} omitted or lacked required metadata:read permission"
                )
            inst_api = factory(discovery_token)
            repos = get_pages(inst_api, "/installation/repositories", key="repositories")
        except (ApiError, OcrAppAuthError, OSError, ValueError) as exc:
            return AppPreflightResult(
                passed=False,
                app=app_identity,
                installations=installations_summary,
                allowlist=normalized_allowlist,
                eligible_repositories=[],
                excluded_repositories=[],
                decisions=[],
                errors=[
                    redact_secrets(
                        f"Failed to list accessible repositories for installation {inst_id}: {exc}",
                        secrets_to_redact,
                    )
                ],
            )

        for repo_obj in repos:
            if not isinstance(repo_obj, dict):
                continue
            full_name = repo_obj.get("full_name")
            if isinstance(full_name, str) and full_name:
                accessible_repos[full_name.lower()] = (inst_id, inst_perms)

    decisions: List[RepositoryPreflightDecision] = []
    eligible_repositories: List[str] = []
    excluded_repositories: List[RepositoryPreflightDecision] = []

    for repo in normalized_allowlist:
        owner_display, _ = repo.split("/", 1)
        owner_key = owner_display.lower()
        repo_key = repo.lower()

        if repo_key in accessible_repos:
            inst_id, inst_perms = accessible_repos[repo_key]
            missing = _missing_required_permissions(inst_perms)
            if missing:
                decision = RepositoryPreflightDecision(
                    repository=repo,
                    eligible=False,
                    installation_id=inst_id,
                    permissions=inst_perms,
                    reason=f"Installation {inst_id} lacks required permissions: {', '.join(missing)}",
                    setup_instruction=(
                        f"Update OCR GitHub App installation {inst_id} permissions for '{repo}' to grant "
                        f"{', '.join(missing)} (and keep administration disabled)."
                    ),
                )
                decisions.append(decision)
                excluded_repositories.append(decision)
            else:
                decision = RepositoryPreflightDecision(
                    repository=repo,
                    eligible=True,
                    installation_id=inst_id,
                    permissions=dict(REQUIRED_APP_PERMISSIONS),
                    reason=None,
                    setup_instruction=None,
                )
                decisions.append(decision)
                eligible_repositories.append(repo)
        else:
            owner_installations = installations_by_owner.get(owner_key, [])
            if owner_installations:
                first_inst = owner_installations[0]
                inst_id = first_inst.get("id")
                inst_perms = first_inst.get("permissions", {})
                missing = _missing_required_permissions(inst_perms)
                if "metadata:read" in " ".join(missing):
                    reason = f"Installation {inst_id} lacks required permissions: {', '.join(missing)}"
                    instruction = (
                        f"Grant {', '.join(missing)} on installation {inst_id} and include '{repo}' "
                        "in the selected repositories."
                    )
                else:
                    reason = f"Repository '{repo}' is not granted installation access under installation {inst_id}"
                    instruction = (
                        f"Add '{repo}' to the OCR GitHub App installation {inst_id} repository access list "
                        "(contents: read, pull_requests: write, metadata: read)."
                    )
                decision = RepositoryPreflightDecision(
                    repository=repo,
                    eligible=False,
                    installation_id=inst_id if isinstance(inst_id, int) else None,
                    permissions=inst_perms,
                    reason=reason,
                    setup_instruction=instruction,
                )
            else:
                decision = RepositoryPreflightDecision(
                    repository=repo,
                    eligible=False,
                    installation_id=None,
                    permissions={},
                    reason=f"No OCR GitHub App installation found for account '{owner_display}'",
                    setup_instruction=(
                        f"Install the limited OCR GitHub App on '{repo}' with permissions "
                        "contents: read, pull_requests: write, and metadata: read (without administration)."
                    ),
                )
            decisions.append(decision)
            excluded_repositories.append(decision)

    errors: List[str] = []
    if not eligible_repositories:
        errors.append("No allowlisted repositories are eligible for automated OCR.")

    return AppPreflightResult(
        passed=bool(eligible_repositories),
        app=app_identity,
        installations=installations_summary,
        allowlist=normalized_allowlist,
        eligible_repositories=eligible_repositories,
        excluded_repositories=excluded_repositories,
        decisions=decisions,
        errors=errors,
    )


def _parse_iso_utc(timestamp: Any) -> datetime:
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise OcrAppAuthError("Installation token response missing valid 'expires_at' timestamp")
    normalized = timestamp.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise OcrAppAuthError("Installation token response contained unparseable 'expires_at' timestamp") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ScopedInstallationToken:
    """Short-lived installation token strictly scoped to one repository."""

    repository: str
    installation_id: int
    token: str = field(repr=False)
    expires_at: datetime
    permissions: Dict[str, str]

    def __str__(self) -> str:
        return repr(self)

    def is_expired(
        self,
        *,
        now: Optional[datetime] = None,
        buffer_seconds: int = DEFAULT_REFRESH_BUFFER_SECONDS,
    ) -> bool:
        """Return True if token is expired or within buffer_seconds of expiring."""
        current = datetime.now(timezone.utc) if now is None else now
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc) >= (self.expires_at - timedelta(seconds=buffer_seconds))


class ScopedInstallationTokenManager:
    """Mints, caches, and refreshes repository-scoped GitHub App installation tokens."""

    def __init__(
        self,
        app_id: Optional[Union[str, int]] = None,
        private_key_path: Optional[Union[str, Path]] = None,
        *,
        admin_app_ids: Sequence[Union[str, int]] = (),
        api_base_url: str = "https://api.github.com",
        api_factory: Optional[Callable[[str], Any]] = None,
        signer: Optional[Callable[[bytes], bytes]] = None,
        refresh_buffer_seconds: int = DEFAULT_REFRESH_BUFFER_SECONDS,
        now_fn: Optional[Callable[[], datetime]] = None,
        app_config: Optional[OcrAppConfig] = None,
    ) -> None:
        cfg = resolve_app_config(
            app_config,
            app_id=app_id,
            private_key_path=private_key_path,
            admin_app_ids=admin_app_ids,
            api_base_url=api_base_url,
            api_factory=api_factory,
            signer=signer,
        )
        self.app_id = normalize_app_id(cfg.app_id)
        self.private_key_path = cfg.private_key_path
        self.admin_app_ids = {normalize_app_id(item) for item in cfg.admin_app_ids if str(item).strip()}
        self.api_base_url = cfg.api_base_url
        self._api_factory = cfg.resolve_client_factory()
        self._signer = cfg.signer
        self.refresh_buffer_seconds = max(0, int(refresh_buffer_seconds))
        self._now_fn = now_fn if now_fn is not None else (lambda: datetime.now(timezone.utc))
        self._cache: Dict[Tuple[int, str], ScopedInstallationToken] = {}

    def preflight(self, allowlist: Sequence[str]) -> AppPreflightResult:
        """Run limited App preflight using this manager's credentials and configuration."""
        return preflight_ocr_app(
            app_id=self.app_id,
            private_key_path=self.private_key_path,
            allowlist=allowlist,
            admin_app_ids=sorted(self.admin_app_ids),
            api_base_url=self.api_base_url,
            api_factory=self._api_factory,
            signer=self._signer,
        )

    def invalidate(
        self,
        repository: Optional[str] = None,
        installation_id: Optional[int] = None,
    ) -> None:
        """Clear cached installation tokens for one repository or all repositories."""
        if repository is None and installation_id is None:
            self._cache.clear()
            return
        repo_key = repository.strip().lower() if repository is not None else None
        keys_to_delete = [
            key
            for key in self._cache
            if (installation_id is None or key[0] == installation_id)
            and (repo_key is None or key[1] == repo_key)
        ]
        for key in keys_to_delete:
            del self._cache[key]

    def get_token(
        self,
        repository: str,
        installation_id: int,
        *,
        force_refresh: bool = False,
    ) -> ScopedInstallationToken:
        """Return a valid, cached or freshly minted token scoped solely to repository."""
        if self.app_id in self.admin_app_ids:
            raise OcrAppAuthError(
                f"GitHub App ID {self.app_id} is configured as an administrative/client-setup App "
                "and cannot mint OCR installation tokens."
            )
        if not isinstance(repository, str) or not REPO_PATTERN.match(repository.strip()):
            raise OcrAppAuthError(f"Invalid repository format: {repository!r} (expected OWNER/REPO)")
        if isinstance(installation_id, bool) or not isinstance(installation_id, int) or installation_id <= 0:
            raise OcrAppAuthError("installation_id must be a positive integer")

        normalized_repo = repository.strip()
        repo_name = normalized_repo.split("/", 1)[1]
        cache_key = (installation_id, normalized_repo.lower())
        current_time = self._now_fn()

        cached = self._cache.get(cache_key)
        if (
            cached is not None
            and not force_refresh
            and not cached.is_expired(now=current_time, buffer_seconds=self.refresh_buffer_seconds)
        ):
            return cached

        now_epoch = int(current_time.timestamp()) if self._now_fn is not None else None
        jwt_token = generate_app_jwt(
            self.app_id,
            self.private_key_path,
            now=now_epoch,
            signer=self._signer,
        )
        secrets_to_redact = [jwt_token]

        try:
            app_api = self._api_factory(jwt_token)
            status, body = app_api.request(
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
                payload={
                    "repositories": [repo_name],
                    "permissions": dict(REQUIRED_APP_PERMISSIONS),
                },
            )
        except (ApiError, OcrAppAuthError, OSError, ValueError) as exc:
            raise OcrAppAuthError(
                redact_secrets(
                    f"Failed to mint scoped installation token for '{normalized_repo}': {exc}",
                    secrets_to_redact,
                )
            ) from None

        if status not in (200, 201) or not isinstance(body, dict):
            raise OcrAppAuthError(
                f"Unexpected response minting installation token for '{normalized_repo}': HTTP {status}"
            )

        raw_token = body.get("token")
        if not isinstance(raw_token, str) or not raw_token:
            raise OcrAppAuthError(f"GitHub did not return an installation token for '{normalized_repo}'")
        secrets_to_redact.append(raw_token)

        expires_at = _parse_iso_utc(body.get("expires_at"))
        granted_perms = _normalize_permissions(body.get("permissions"))

        forbidden_perms = _has_forbidden_permissions(granted_perms)
        if forbidden_perms:
            raise OcrAppAuthError(
                redact_secrets(
                    f"Minted token for '{normalized_repo}' unexpectedly granted forbidden non-OCR "
                    f"privileged permission(s) ({', '.join(forbidden_perms)}).",
                    secrets_to_redact,
                )
            )

        missing_perms = _missing_required_permissions(granted_perms)
        if missing_perms:
            raise OcrAppAuthError(
                redact_secrets(
                    f"Minted token for '{normalized_repo}' lacks required permissions: "
                    f"{', '.join(missing_perms)}.",
                    secrets_to_redact,
                )
            )

        returned_repos = body.get("repositories")
        if isinstance(returned_repos, list):
            returned_names = {
                str(item.get("full_name") or item.get("name") or "").lower()
                for item in returned_repos
                if isinstance(item, dict)
            }
            if not returned_names or not returned_names.issubset({repo_name.lower(), normalized_repo.lower()}):
                raise OcrAppAuthError(
                    redact_secrets(
                        f"Minted token scope for '{normalized_repo}' did not match selected repository.",
                        secrets_to_redact,
                    )
                )

        scoped = ScopedInstallationToken(
            repository=normalized_repo,
            installation_id=installation_id,
            token=raw_token,
            expires_at=expires_at,
            permissions=granted_perms,
        )
        self._cache[cache_key] = scoped
        return scoped


def split_csv_values(values: Optional[Sequence[str]]) -> List[str]:
    """Expand repeatable or comma-separated string values into stripped non-empty items."""
    items: List[str] = []
    if not values:
        return items
    for raw in values:
        if not isinstance(raw, str):
            continue
        for part in raw.split(","):
            trimmed = part.strip()
            if trimmed:
                items.append(trimmed)
    return items


def merge_admin_app_ids(
    admin_app_ids: Sequence[Union[str, int]] = (),
    env: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """Expand admin App IDs and merge SCIFLOW_CICD_ADMIN_APP_ID / GH_SETUP_ADMIN_APP_ID / GH_SETUP_CLIENT_APP_ID env vars."""
    environ = env if env is not None else os.environ
    merged = split_csv_values([str(item) for item in admin_app_ids])
    for env_var in ADMIN_APP_ID_ENV_VARS:
        merged.extend(split_csv_values([environ.get(env_var, "")]))
    return merged


def format_app_preflight_report(
    result: AppPreflightResult,
    *,
    extra_secrets: Sequence[str] = (),
) -> str:
    """Format a secret-free human-readable report for OCR GitHub App preflight."""
    lines: List[str] = [
        "============================================================",
        "sciflow-cicd OCR: Limited GitHub App Preflight",
        "============================================================",
        f"Verdict: {'PASS' if result.passed else 'FAIL'}",
    ]
    if result.app is not None:
        label = result.app.slug or result.app.name or result.app.app_id
        lines.append(f"GitHub App: {label} (id={result.app.app_id})")
    lines.append(
        "Required permissions: "
        + ", ".join(f"{k}: {v}" for k, v in result.required_permissions.items())
        + " (administration: prohibited)"
    )

    if result.installations:
        lines.append("Installations:")
        for inst in result.installations:
            perms_str = ", ".join(f"{k}: {v}" for k, v in sorted(inst.get("permissions", {}).items()))
            lines.append(
                f"  - id={inst.get('id')} account={inst.get('account')} "
                f"selection={inst.get('repository_selection')} permissions=[{perms_str}]"
            )

    if result.eligible_repositories:
        lines.append("Eligible allowlisted repositories:")
        eligible_decisions = {d.repository: d for d in result.decisions if d.eligible}
        for repo in result.eligible_repositories:
            dec = eligible_decisions.get(repo)
            inst_note = f" (installation={dec.installation_id})" if dec and dec.installation_id else ""
            lines.append(f"  [✓] {repo}{inst_note}")

    if result.excluded_repositories:
        lines.append("Excluded allowlisted repositories:")
        for dec in result.excluded_repositories:
            lines.append(f"  [✗] {dec.repository}: {dec.reason}")
            if dec.setup_instruction:
                lines.append(f"      Setup: {dec.setup_instruction}")

    if result.errors:
        lines.append("Errors:")
        for err in result.errors:
            lines.append(f"  [✗] {err}")

    lines.append("============================================================")
    return redact_secrets("\n".join(lines) + "\n", extra_secrets)


def run_app_preflight(
    *,
    app_id: Union[str, int],
    private_key_path: Union[str, Path],
    allowlist: Sequence[str],
    admin_app_ids: Sequence[Union[str, int]] = (),
    api_base_url: str = "https://api.github.com",
    env: Optional[Mapping[str, str]] = None,
    json_output: bool = False,
    out_stream: Optional[TextIO] = None,
    err_stream: Optional[TextIO] = None,
    api_factory: Optional[Callable[[str], Any]] = None,
    signer: Optional[Callable[[bytes], bytes]] = None,
) -> int:
    """Run limited GitHub App preflight and write formatted or JSON output."""
    out_s = out_stream if out_stream is not None else sys.stdout
    err_s = err_stream if err_stream is not None else sys.stderr
    environ = env if env is not None else os.environ
    secrets_to_redact = collect_env_secrets(environ)

    try:
        app_cfg = resolve_app_config(
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
        result = preflight_ocr_app(app_config=app_cfg)
    except OcrAppAuthError as exc:
        safe_msg = redact_secrets(str(exc), secrets_to_redact)
        result = AppPreflightResult(
            passed=False,
            app=None,
            installations=[],
            allowlist=split_csv_values([str(x) for x in allowlist]),
            eligible_repositories=[],
            excluded_repositories=[],
            decisions=[],
            errors=[safe_msg],
        )

    if json_output:
        out_s.write(json.dumps(result.to_dict(extra_secrets=secrets_to_redact), indent=2) + "\n")
    else:
        report = format_app_preflight_report(result, extra_secrets=secrets_to_redact)
        out_s.write(report)
        if not result.passed and result.errors:
            err_s.write(redact_secrets("\n".join(result.errors) + "\n", secrets_to_redact))

    return 0 if result.passed else 1
