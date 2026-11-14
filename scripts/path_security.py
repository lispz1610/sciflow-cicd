"""Shared path security and symlink rejection utilities.

Provides safe relative POSIX path validation and strict symlink traversal guards
used across skill provisioning, hook contracts, and discovery modules.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional, Type, Union


def safe_relative_path(
    value: Any,
    label: str = "path",
    *,
    error_cls: Type[Exception] = ValueError,
) -> Path:
    """Validate that value is a safe, canonical relative POSIX path.

    Rejects null bytes, backslashes, absolute paths, empty segments, and traversal
    components ('.' and '..'). Raises error_cls on violation.
    """
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise error_cls(f"{label} path must be a nonempty POSIX relative path")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise error_cls(f"{label} path contains invalid traversal or empty segments: {value!r}")
    path = Path(value)
    if path.is_absolute():
        raise error_cls(f"{label} path must not be absolute: {value!r}")
    if path.as_posix() != value:
        raise error_cls(f"{label} path must be canonical POSIX: {value!r}")
    return path


def reject_symlink_components(
    path: Union[str, Path],
    *,
    include_leaf: bool = True,
    error_cls: Type[Exception] = ValueError,
    message_format: Optional[str] = None,
) -> None:
    """Reject existing symlinks in path or any of its ancestors without resolving through them.

    Raises error_cls if any checked component is a symlink.
    """
    absolute = Path(os.path.abspath(path))
    pieces = list(reversed((absolute, *absolute.parents)))
    if not include_leaf:
        pieces = pieces[:-1]
    for piece in pieces:
        if piece.is_symlink():
            if message_format:
                msg = message_format.format(piece=piece)
            else:
                msg = f"Symlinks are prohibited in path: {piece}"
            raise error_cls(msg)
