"""Tests for shared OCR symlink-safe state persistence and lock-path validation."""

from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.ocr_state import (
    OcrStateError,
    is_head_reviewed,
    load_review_state,
    record_head_changed_mid_run,
    record_head_reviewed,
    record_retryable_failure,
    save_review_state,
    validate_lock_path,
)


class OcrStateTests(unittest.TestCase):
    def test_validate_lock_path_rejects_symlinks_with_custom_error(self):
        class _CustomErr(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            real_target = tmp / "real.lock"
            real_target.write_text("", encoding="utf-8")
            sym_lock = tmp / "sym.lock"
            os.symlink(real_target, sym_lock)

            with self.assertRaises(_CustomErr):
                validate_lock_path(sym_lock, error_cls=_CustomErr)

            valid = validate_lock_path(tmp / "clean.lock", error_cls=_CustomErr)
            self.assertEqual(valid, tmp / "clean.lock")

    def test_review_state_transitions_persist_and_redact_secrets(self):
        fixed_now = lambda: datetime(2026, 11, 14, 1, 0, 0, tzinfo=timezone.utc)
        sha1 = "a" * 40
        sha2 = "b" * 40

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            state = load_review_state(state_path)
            self.assertEqual(state, {"version": 1, "pull_requests": {}})
            self.assertFalse(is_head_reviewed(state, "acme/repo-a", 12, sha1))

            _, attempts, safe_err = record_retryable_failure(
                state=state,
                state_path=state_path,
                repository="acme/repo-a",
                pr_number=12,
                head_sha=sha1,
                review_mode="pr_review",
                error_msg="Failed with ghs_secret_value_123",
                clock=fixed_now,
                secrets_to_redact=("ghs_secret_value_123",),
            )
            self.assertEqual(attempts, 1)
            self.assertNotIn("ghs_secret_value_123", safe_err)
            self.assertFalse(is_head_reviewed(state, "acme/repo-a", 12, sha1))

            record_head_changed_mid_run(
                state=state,
                state_path=state_path,
                repository="acme/repo-a",
                pr_number=12,
                head_sha=sha1,
                observed_new_head_sha=sha2,
                clock=fixed_now,
            )
            self.assertFalse(is_head_reviewed(state, "acme/repo-a", 12, sha1))

            record_head_reviewed(
                state=state,
                state_path=state_path,
                repository="acme/repo-a",
                pr_number=12,
                head_sha=sha2,
                review_mode="pr_review",
                comment_id=9001,
                comment_url="https://github.com/acme/repo-a/pull/12#pullrequestreview-9001",
                clock=fixed_now,
                increment_attempts=True,
            )
            reloaded = load_review_state(state_path)
            self.assertTrue(is_head_reviewed(reloaded, "acme/repo-a", 12, sha2))
            save_review_state(state_path, reloaded)
            self.assertTrue(is_head_reviewed(load_review_state(state_path), "acme/repo-a", 12, sha2))
            self.assertIsNotNone(OcrStateError)


if __name__ == "__main__":
    unittest.main()
