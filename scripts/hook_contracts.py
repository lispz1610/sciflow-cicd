"""Domain module defining opt-in hook package contracts, schema, and validation.

Hook packages provide event-driven lifecycle extensions for agent runs.
In accordance with repository safety policies, hook packages are disabled by
default, require explicit opt-in, and undergo strict cryptographic and path
safety validation before any staging or execution.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

try:
    from scripts.path_security import reject_symlink_components, safe_relative_path
except ImportError:  # pragma: no cover
    from path_security import reject_symlink_components, safe_relative_path

SCHEMA_VERSION: int = 1
MANIFEST_FILENAMES: Tuple[str, ...] = ("hook.json", "manifest.json")
NAME_PATTERN: re.Pattern = re.compile(r"^[a-z0-9][a-z0-9-]*\Z")
SHA256_PATTERN: re.Pattern = re.compile(r"^[0-9a-f]{64}\Z")

SUPPORTED_EVENTS: frozenset[str] = frozenset({
    "pre-tool-call",
    "post-tool-call",
    "session-start",
    "session-stop",
})

SUPPORTED_RUNTIMES: frozenset[str] = frozenset({
    "bash",
    "python3",
})

MANIFEST_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "schema_version",
    "name",
    "description",
    "event",
    "runtime",
    "entrypoint",
    "files",
    "permissions",
    "opt_in",
})


class HookValidationError(ValueError):
    """Raised when a hook package or manifest fails validation."""


@dataclass(frozen=True)
class HookManifest:
    """Parsed and validated hook manifest data."""

    schema_version: int
    name: str
    description: str
    event: str
    runtime: str
    entrypoint: str
    files: Dict[str, str]
    permissions: Tuple[str, ...]
    opt_in: bool


@dataclass(frozen=True)
class HookPackage:
    """Validated hook package on disk."""

    manifest: HookManifest
    package_dir: Path
    manifest_path: Path

    @property
    def is_enabled(self) -> bool:
        """Return True if the hook package has explicit opt_in=True."""
        return self.manifest.opt_in


def _safe_relative_path(value: Any, label: str) -> Path:
    """Validate that a path is a safe, canonical relative POSIX path."""
    return safe_relative_path(value, label, error_cls=HookValidationError)


def _reject_symlink_components(path: Path) -> None:
    """Reject if the path itself or any ancestor is a symlink."""
    reject_symlink_components(
        path,
        error_cls=HookValidationError,
        message_format="Symlinks are prohibited in hook packages: {piece}",
    )


def _compute_sha256(file_path: Path) -> str:
    """Compute lowercase hex SHA-256 digest of a file."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest().lower()


def validate_hook_manifest(data: Any) -> HookManifest:
    """Validate a raw hook manifest dictionary against the schema."""
    if not isinstance(data, dict):
        raise HookValidationError(f"Hook manifest must be a JSON object/dict, got {type(data).__name__}")

    actual_keys = set(data.keys())
    if actual_keys != MANIFEST_REQUIRED_FIELDS:
        missing = MANIFEST_REQUIRED_FIELDS - actual_keys
        extra = actual_keys - MANIFEST_REQUIRED_FIELDS
        details = []
        if missing:
            details.append(f"missing required fields: {sorted(missing)}")
        if extra:
            details.append(f"unexpected extra fields: {sorted(extra)}")
        raise HookValidationError(f"Invalid hook manifest schema: {'; '.join(details)}")

    # schema_version
    schema_version = data["schema_version"]
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        raise HookValidationError(
            f"Invalid schema_version: expected integer {SCHEMA_VERSION}, got {schema_version!r}"
        )

    # name
    name = data["name"]
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise HookValidationError(
            f"Invalid hook name: must be an alphanumeric/hyphen string matching {NAME_PATTERN.pattern}, got {name!r}"
        )

    # description
    description = data["description"]
    if not isinstance(description, str) or not description.strip():
        raise HookValidationError("Hook description must be a nonempty string")

    # event
    event = data["event"]
    if not isinstance(event, str) or event not in SUPPORTED_EVENTS:
        raise HookValidationError(
            f"Invalid hook event '{event}'. Supported events: {sorted(SUPPORTED_EVENTS)}"
        )

    # runtime
    runtime = data["runtime"]
    if not isinstance(runtime, str) or runtime not in SUPPORTED_RUNTIMES:
        raise HookValidationError(
            f"Invalid hook runtime '{runtime}'. Supported runtimes: {sorted(SUPPORTED_RUNTIMES)}"
        )

    # entrypoint
    entrypoint_str = data["entrypoint"]
    entrypoint_path = _safe_relative_path(entrypoint_str, "entrypoint")
    entrypoint = entrypoint_path.as_posix()

    # opt_in
    opt_in = data["opt_in"]
    if type(opt_in) is not bool:
        raise HookValidationError(f"opt_in must be an explicit boolean (True or False), got {opt_in!r}")

    # permissions
    permissions_raw = data["permissions"]
    if not isinstance(permissions_raw, list):
        raise HookValidationError(f"permissions must be a list of strings, got {type(permissions_raw).__name__}")
    permissions: List[str] = []
    for idx, perm in enumerate(permissions_raw):
        if not isinstance(perm, str) or not perm.strip():
            raise HookValidationError(f"permissions[{idx}] must be a nonempty string, got {perm!r}")
        permissions.append(perm.strip())

    # files
    files_raw = data["files"]
    if not isinstance(files_raw, dict) or not files_raw:
        raise HookValidationError("files must be a nonempty mapping of relative file paths to SHA-256 digests")

    normalized_files: Dict[str, str] = {}
    for rel_str, digest in files_raw.items():
        rel_path = _safe_relative_path(rel_str, "file")
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest.lower()):
            raise HookValidationError(
                f"Invalid SHA-256 digest for file {rel_str!r}: must be a 64-character lowercase hex string"
            )
        normalized_files[rel_path.as_posix()] = digest.lower()

    # entrypoint must be declared in files
    if entrypoint not in normalized_files:
        raise HookValidationError(
            f"Entrypoint {entrypoint!r} is not declared in files mapping"
        )

    return HookManifest(
        schema_version=schema_version,
        name=name,
        description=description,
        event=event,
        runtime=runtime,
        entrypoint=entrypoint,
        files=normalized_files,
        permissions=tuple(permissions),
        opt_in=opt_in,
    )


def validate_hook_package(package_dir: Union[str, Path]) -> HookPackage:
    """Validate a hook package directory on disk and return a HookPackage."""
    pkg_raw_path = Path(package_dir)
    pkg_path = pkg_raw_path.resolve()
    if not pkg_raw_path.exists() or not pkg_path.exists():
        raise HookValidationError(f"Hook package directory does not exist: {package_dir}")
    if not pkg_path.is_dir():
        raise HookValidationError(f"Hook package path is not a directory: {package_dir}")

    # Symlink checks on input path and resolved path
    _reject_symlink_components(pkg_raw_path)
    _reject_symlink_components(pkg_path)

    # Find manifest
    found_manifests = [
        pkg_path / fname for fname in MANIFEST_FILENAMES if (pkg_path / fname).exists()
    ]
    if not found_manifests:
        raise HookValidationError(
            f"Hook package missing manifest ({' or '.join(MANIFEST_FILENAMES)}) in {package_dir}"
        )
    if len(found_manifests) > 1:
        names = ", ".join(p.name for p in found_manifests)
        raise HookValidationError(
            f"Ambiguous hook package manifests: multiple manifest files found ({names}) in {package_dir}"
        )

    manifest_path = found_manifests[0]
    _reject_symlink_components(manifest_path)
    if not manifest_path.is_file():
        raise HookValidationError(f"Hook manifest is not a regular file: {manifest_path}")

    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
        manifest_data = json.loads(raw_text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise HookValidationError(f"Failed to read or parse hook manifest at {manifest_path}: {err}") from err

    manifest = validate_hook_manifest(manifest_data)

    # Walk directory to detect symlinks and discover all files on disk
    disk_files: Set[str] = set()
    for root, dirs, files in os.walk(pkg_path, followlinks=False):
        current_root = Path(root)
        for d in dirs:
            dir_full = current_root / d
            if dir_full.is_symlink():
                raise HookValidationError(f"Symlinks are prohibited in hook packages: {dir_full}")
        for f in files:
            file_full = current_root / f
            if file_full.is_symlink():
                raise HookValidationError(f"Symlinks are prohibited in hook packages: {file_full}")
            rel_posix = file_full.relative_to(pkg_path).as_posix()
            disk_files.add(rel_posix)

    # Check that all declared files exist and have matching SHA-256
    for rel_file, expected_sha in manifest.files.items():
        file_path = pkg_path / rel_file
        if not file_path.exists():
            raise HookValidationError(f"Declared file missing from package: {rel_file}")
        if not file_path.is_file():
            raise HookValidationError(f"Declared file is not a regular file: {rel_file}")
        actual_sha = _compute_sha256(file_path)
        if actual_sha != expected_sha:
            raise HookValidationError(
                f"Content hash mismatch for {rel_file}: expected {expected_sha}, got {actual_sha}"
            )

    # Check for unindexed files (excluding manifest file)
    manifest_rel_name = manifest_path.relative_to(pkg_path).as_posix()
    for rel_file in disk_files:
        if rel_file == manifest_rel_name:
            continue
        if rel_file not in manifest.files:
            raise HookValidationError(f"Unindexed file found in hook package: {rel_file}")

    return HookPackage(
        manifest=manifest,
        package_dir=pkg_path,
        manifest_path=manifest_path,
    )


def validate_hook_packages(
    package_dirs: Optional[Sequence[Union[str, Path]]] = None,
) -> Tuple[HookPackage, ...]:
    """Validate a sequence of hook package directories.

    Enforces safe defaults: if package_dirs is None or empty, returns an empty
    tuple with zero active execution paths.
    """
    if not package_dirs:
        return ()
    packages: List[HookPackage] = []
    seen_names: Set[str] = set()
    for p in package_dirs:
        pkg = validate_hook_package(p)
        if pkg.manifest.name in seen_names:
            raise HookValidationError(f"Duplicate hook name {pkg.manifest.name!r} in package sequence")
        seen_names.add(pkg.manifest.name)
        packages.append(pkg)
    return tuple(packages)


def filter_enabled_hooks(
    packages: Optional[Sequence[HookPackage]] = None,
) -> Tuple[HookPackage, ...]:
    """Filter validated hook packages for only those with explicit opt_in=True.

    Enforces safe defaults: if packages is None or empty, returns an empty tuple.
    """
    if not packages:
        return ()
    return tuple(pkg for pkg in packages if pkg.is_enabled)


def resolve_active_hooks(
    package_dirs: Optional[Sequence[Union[str, Path]]] = None,
) -> Tuple[HookPackage, ...]:
    """Validate hook package directories and return only enabled hooks.

    Enforces safe defaults: if package_dirs is None or empty, returns an empty
    tuple with zero active execution paths.
    """
    if not package_dirs:
        return ()
    validated = validate_hook_packages(package_dirs)
    return filter_enabled_hooks(validated)
