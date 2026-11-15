"""Shared symlink-safe atomic JSON state persistence for automated OCR polling and reviews."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Callable, Dict, Iterator, Optional, Sequence, Tuple, Type, Union

try:
    from scripts.ocr_app_auth import normalize_app_id, redact_secrets
except ImportError:
    from ocr_app_auth import normalize_app_id, redact_secrets

try:
    from scripts.path_security import reject_symlink_components
except ImportError:
    from path_security import reject_symlink_components


class OcrStateError(RuntimeError):
    """Raised when OCR state path validation, reading, or atomic persistence fails."""


_STATE_LOCKS_GUARD = threading.Lock()
_STATE_LOCKS: Dict[str, threading.Lock] = {}


def _get_process_state_lock(resolved_path: str) -> threading.Lock:
    with _STATE_LOCKS_GUARD:
        lock = _STATE_LOCKS.get(resolved_path)
        if lock is None:
            lock = threading.Lock()
            _STATE_LOCKS[resolved_path] = lock
        return lock


@contextmanager
def state_file_lock(
    state_path: Union[str, Path],
    *,
    error_cls: Type[Exception] = OcrStateError,
    label: str = "OCR state",
) -> Iterator[None]:
    """Serialize in-process and cross-process read-modify-write access to state_path."""
    path = Path(state_path)
    reject_symlink_components(
        path,
        include_leaf=True,
        error_cls=error_cls,
        message_format=f"Symlinks are prohibited in {label} path: {{piece}}",
    )
    lock_path = path.parent / f".{path.name}.lock"
    reject_symlink_components(
        lock_path,
        include_leaf=True,
        error_cls=error_cls,
        message_format=f"Symlinks are prohibited in {label} lock path: {{piece}}",
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise error_cls(f"Failed to prepare {label} directory for {path}: {exc}") from None

    thread_lock = _get_process_state_lock(str(path.resolve()))
    with thread_lock:
        fd: Optional[int] = None
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        except OSError as exc:
            raise error_cls(f"Failed to lock {label} file {path}: {exc}") from None
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(fd)
                except OSError:
                    pass


def default_review_state_path(app_id: Union[str, int]) -> Path:
    """Return the durable symlink-safe default OCR state path scoped by GitHub App ID."""
    normalized = normalize_app_id(app_id)
    return Path(tempfile.gettempdir()) / f"sciflow-cicd-ocr-state-{normalized}.json"


def _load_json_state_unlocked(
    path: Path,
    *,
    default_factory: Optional[Callable[[], Dict[str, Any]]] = None,
    error_cls: Type[Exception] = OcrStateError,
    label: str = "OCR state",
) -> Dict[str, Any]:
    if not path.exists():
        return default_factory() if default_factory is not None else {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise error_cls(f"Failed to read {label} file {path}: {exc}") from None
    if not isinstance(data, dict):
        raise error_cls(f"{label} file {path} must contain a JSON object.")
    return data


def load_json_state(
    state_path: Optional[Union[str, Path]],
    *,
    default_factory: Optional[Callable[[], Dict[str, Any]]] = None,
    error_cls: Type[Exception] = OcrStateError,
    label: str = "OCR state",
) -> Dict[str, Any]:
    """Load JSON object state safely without following symlinks."""
    if state_path is None:
        return default_factory() if default_factory is not None else {}
    path = Path(state_path)
    reject_symlink_components(
        path,
        include_leaf=True,
        error_cls=error_cls,
        message_format=f"Symlinks are prohibited in {label} path: {{piece}}",
    )
    if not path.exists():
        return default_factory() if default_factory is not None else {}
    with state_file_lock(path, error_cls=error_cls, label=label):
        return _load_json_state_unlocked(
            path,
            default_factory=default_factory,
            error_cls=error_cls,
            label=label,
        )


def _save_json_state_unlocked(
    path: Path,
    state: Dict[str, Any],
    *,
    error_cls: Type[Exception] = OcrStateError,
    label: str = "OCR state",
) -> None:
    reject_symlink_components(
        path,
        include_leaf=True,
        error_cls=error_cls,
        message_format=f"Symlinks are prohibited in {label} path: {{piece}}",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")

    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.tmp.",
            delete=False,
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            reject_symlink_components(
                tmp_path,
                include_leaf=True,
                error_cls=error_cls,
                message_format=f"Symlinks are prohibited in {label} temp path: {{piece}}",
            )
            tmp_file.write(payload)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        if tmp_path is not None:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
        raise error_cls(f"Failed to write {label} file {path}: {exc}") from None


def save_json_state(
    state_path: Optional[Union[str, Path]],
    state: Dict[str, Any],
    *,
    error_cls: Type[Exception] = OcrStateError,
    label: str = "OCR state",
) -> None:
    """Persist JSON object state atomically with 0o600 permissions and symlink checks."""
    if state_path is None:
        return
    path = Path(state_path)
    with state_file_lock(path, error_cls=error_cls, label=label):
        _save_json_state_unlocked(
            path,
            state,
            error_cls=error_cls,
            label=label,
        )


def update_json_state(
    state_path: Optional[Union[str, Path]],
    updater: Callable[[Dict[str, Any]], Dict[str, Any]],
    *,
    default_factory: Optional[Callable[[], Dict[str, Any]]] = None,
    error_cls: Type[Exception] = OcrStateError,
    label: str = "OCR state",
) -> Dict[str, Any]:
    """Perform symlink-safe, serialized atomic read-modify-write on a JSON state file."""
    if state_path is None:
        current = default_factory() if default_factory is not None else {}
        return updater(current)
    path = Path(state_path)
    with state_file_lock(path, error_cls=error_cls, label=label):
        current = _load_json_state_unlocked(
            path,
            default_factory=default_factory,
            error_cls=error_cls,
            label=label,
        )
        updated = updater(current)
        _save_json_state_unlocked(
            path,
            updated,
            error_cls=error_cls,
            label=label,
        )
        return updated


def validate_lock_path(
    lock_path: Union[str, Path],
    *,
    error_cls: Type[Exception] = OcrStateError,
) -> Path:
    """Validate that a single-flight lock path contains no symlink components."""
    path = Path(lock_path)
    reject_symlink_components(
        path,
        include_leaf=True,
        error_cls=error_cls,
        message_format="Symlinks are prohibited in lock path: {piece}",
    )
    return path


def pr_state_key(repository: str, pr_number: int) -> str:
    """Return canonical lowercase key for per-PR review state entries."""
    return f"{repository.strip().lower()}#{int(pr_number)}"


def load_review_state(
    state_path: Optional[Union[str, Path]],
    *,
    error_cls: Type[Exception] = OcrStateError,
) -> Dict[str, Any]:
    """Load durable OCR rotation and per-PR/per-HEAD review state safely without following symlinks."""
    data = load_json_state(
        state_path,
        default_factory=lambda: {"version": 1, "pull_requests": {}},
        error_cls=error_cls,
        label="OCR state",
    )
    data.setdefault("version", 1)
    prs = data.get("pull_requests")
    if not isinstance(prs, dict):
        data["pull_requests"] = {}
    return data


def save_review_state(
    state_path: Optional[Union[str, Path]],
    state: Dict[str, Any],
    *,
    error_cls: Type[Exception] = OcrStateError,
) -> None:
    """Persist OCR rotation and per-PR/per-HEAD state atomically via symlink-safe os.replace."""
    save_json_state(
        state_path,
        state,
        error_cls=error_cls,
        label="OCR state",
    )


def _now_iso(clock: Callable[[], datetime]) -> str:
    return clock().astimezone(timezone.utc).isoformat()


def ensure_pr_head_record(
    state: Dict[str, Any],
    repository: str,
    pr_number: int,
    head_sha: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Ensure PR and HEAD SHA state entries exist and return (pr_entry, head_record)."""
    prs = state.setdefault("pull_requests", {})
    key = pr_state_key(repository, pr_number)
    entry = prs.get(key)
    if not isinstance(entry, dict):
        entry = {
            "repository": repository,
            "pr_number": int(pr_number),
            "last_reviewed_head_sha": None,
            "last_seen_head_sha": head_sha,
            "status": "pending",
            "heads": {},
        }
        prs[key] = entry
    entry["repository"] = repository
    entry["pr_number"] = int(pr_number)
    entry["last_seen_head_sha"] = head_sha
    heads = entry.setdefault("heads", {})
    head_rec = heads.get(head_sha)
    if not isinstance(head_rec, dict):
        head_rec = {
            "status": "pending",
            "attempts": 0,
            "review_mode": None,
            "comment_id": None,
            "comment_url": None,
            "reviewed_at": None,
            "updated_at": None,
            "last_error": None,
            "observed_new_head_sha": None,
        }
        heads[head_sha] = head_rec
    return entry, head_rec


def is_head_reviewed(state: Dict[str, Any], repository: str, pr_number: int, head_sha: str) -> bool:
    """Return True if durable state marks the PR's head_sha as already reviewed."""
    prs = state.get("pull_requests")
    if not isinstance(prs, dict):
        return False
    entry = prs.get(pr_state_key(repository, pr_number))
    if not isinstance(entry, dict):
        return False
    heads = entry.get("heads")
    if not isinstance(heads, dict):
        return False
    head_rec = heads.get(head_sha)
    return isinstance(head_rec, dict) and head_rec.get("status") == "reviewed"


def _normalize_review_state_root(state: Dict[str, Any]) -> Dict[str, Any]:
    state.setdefault("version", 1)
    prs = state.get("pull_requests")
    if not isinstance(prs, dict):
        state["pull_requests"] = {}
    return state


def update_review_cursor(
    state_path: Optional[Union[str, Path]],
    state: Dict[str, Any],
    *,
    next_index: int,
    next_repository: Optional[str],
    error_cls: Type[Exception] = OcrStateError,
) -> None:
    """Update rotation cursor in memory and atomically merge into durable state."""
    state["next_index"] = next_index
    state["next_repository"] = next_repository
    if state_path is None:
        return

    def _apply_cursor(disk_state: Dict[str, Any]) -> Dict[str, Any]:
        _normalize_review_state_root(disk_state)
        disk_state["next_index"] = next_index
        disk_state["next_repository"] = next_repository
        return disk_state

    update_json_state(
        state_path,
        _apply_cursor,
        default_factory=lambda: {"version": 1, "pull_requests": {}},
        error_cls=error_cls,
        label="OCR state",
    )


def _persist_pr_record_mutation(
    *,
    state: Dict[str, Any],
    state_path: Optional[Union[str, Path]],
    mutator: Callable[[Dict[str, Any]], Any],
    error_cls: Type[Exception],
) -> Any:
    result = mutator(state)
    if state_path is not None:
        def _apply_to_disk(disk_state: Dict[str, Any]) -> Dict[str, Any]:
            _normalize_review_state_root(disk_state)
            mutator(disk_state)
            return disk_state

        update_json_state(
            state_path,
            _apply_to_disk,
            default_factory=lambda: {"version": 1, "pull_requests": {}},
            error_cls=error_cls,
            label="OCR state",
        )
    return result


def record_head_reviewed(
    *,
    state: Dict[str, Any],
    state_path: Optional[Union[str, Path]],
    repository: str,
    pr_number: int,
    head_sha: str,
    review_mode: str,
    comment_id: Optional[int],
    comment_url: Optional[str],
    clock: Callable[[], datetime],
    increment_attempts: bool,
    error_cls: Type[Exception] = OcrStateError,
) -> Dict[str, Any]:
    """Record a reviewed PR HEAD SHA in durable state and persist atomically."""
    now_iso = _now_iso(clock)

    def _mutate(target_state: Dict[str, Any]) -> Dict[str, Any]:
        entry, head_rec = ensure_pr_head_record(target_state, repository, pr_number, head_sha)
        head_rec["status"] = "reviewed"
        if increment_attempts:
            head_rec["attempts"] = int(head_rec.get("attempts") or 0) + 1
        head_rec["review_mode"] = review_mode
        head_rec["comment_id"] = comment_id
        head_rec["comment_url"] = comment_url
        head_rec["reviewed_at"] = (
            head_rec.get("reviewed_at") or now_iso if not increment_attempts else now_iso
        )
        head_rec["updated_at"] = now_iso
        head_rec["last_error"] = None
        head_rec["observed_new_head_sha"] = None
        entry["status"] = "reviewed"
        entry["last_reviewed_head_sha"] = head_sha
        entry["last_seen_head_sha"] = head_sha
        return head_rec

    return _persist_pr_record_mutation(
        state=state,
        state_path=state_path,
        mutator=_mutate,
        error_cls=error_cls,
    )


def record_head_changed_mid_run(
    *,
    state: Dict[str, Any],
    state_path: Optional[Union[str, Path]],
    repository: str,
    pr_number: int,
    head_sha: str,
    observed_new_head_sha: str,
    clock: Callable[[], datetime],
    error_cls: Type[Exception] = OcrStateError,
) -> Dict[str, Any]:
    """Record a mid-run HEAD SHA change without marking the stale HEAD as reviewed."""
    now_iso = _now_iso(clock)

    def _mutate(target_state: Dict[str, Any]) -> Dict[str, Any]:
        entry, head_rec = ensure_pr_head_record(target_state, repository, pr_number, head_sha)
        head_rec["status"] = "head_changed_mid_run"
        head_rec["observed_new_head_sha"] = observed_new_head_sha
        head_rec["updated_at"] = now_iso
        entry["status"] = "head_changed_mid_run"
        entry["last_seen_head_sha"] = observed_new_head_sha
        return head_rec

    return _persist_pr_record_mutation(
        state=state,
        state_path=state_path,
        mutator=_mutate,
        error_cls=error_cls,
    )


def record_retryable_failure(
    *,
    state: Dict[str, Any],
    state_path: Optional[Union[str, Path]],
    repository: str,
    pr_number: int,
    head_sha: str,
    review_mode: str,
    error_msg: str,
    clock: Callable[[], datetime],
    secrets_to_redact: Sequence[str] = (),
    error_cls: Type[Exception] = OcrStateError,
) -> Tuple[Dict[str, Any], int, str]:
    """Record a retryable OCR failure with redacted error details and without marking HEAD reviewed."""
    safe_err = redact_secrets(error_msg, secrets_to_redact)
    now_iso = _now_iso(clock)

    def _mutate(target_state: Dict[str, Any]) -> Tuple[Dict[str, Any], int, str]:
        entry, head_rec = ensure_pr_head_record(target_state, repository, pr_number, head_sha)
        attempts = int(head_rec.get("attempts") or 0) + 1
        head_rec["status"] = "retryable_failure"
        head_rec["attempts"] = attempts
        head_rec["review_mode"] = review_mode
        head_rec["updated_at"] = now_iso
        head_rec["last_error"] = safe_err
        entry["status"] = "retryable_failure"
        entry["last_seen_head_sha"] = head_sha
        return head_rec, attempts, safe_err

    return _persist_pr_record_mutation(
        state=state,
        state_path=state_path,
        mutator=_mutate,
        error_cls=error_cls,
    )
