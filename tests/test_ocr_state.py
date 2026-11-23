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

    def test_default_review_state_path_uses_user_0700_dir_and_lock_uses_o_nofollow(self):
        import os
        import stat
        from unittest import mock
        from scripts import ocr_state

        with tempfile.TemporaryDirectory() as tmpdir:
            xdg_dir = Path(tmpdir) / "xdg_runtime"
            xdg_dir.mkdir(mode=0o700)
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(xdg_dir)}):
                state_path = ocr_state.default_review_state_path(3333)
                self.assertEqual(state_path, xdg_dir / "sciflow-cicd" / "sciflow-cicd-ocr-state-3333.json")
                self.assertEqual(stat.S_IMODE(state_path.parent.stat().st_mode), 0o700)

            home_dir = Path(tmpdir) / "home"
            home_dir.mkdir(mode=0o700)
            env_without_xdg = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
            env_without_xdg["HOME"] = str(home_dir)
            with mock.patch.dict(os.environ, env_without_xdg, clear=True):
                state_path = ocr_state.default_review_state_path(3333)
                self.assertEqual(
                    state_path,
                    home_dir / ".config" / "sciflow-cicd" / "sciflow-cicd-ocr-state-3333.json",
                )
                self.assertEqual(stat.S_IMODE(state_path.parent.stat().st_mode), 0o700)

            target_state = Path(tmpdir) / "state.json"
            opened_flags = []
            real_open = os.open

            def spy_open(path, flags, mode=0o777, *args, **kwargs):
                opened_flags.append(flags)
                return real_open(path, flags, mode, *args, **kwargs)

            with mock.patch("scripts.ocr_state.os.open", side_effect=spy_open):
                with ocr_state.state_file_lock(target_state):
                    pass
            self.assertTrue(opened_flags)
            self.assertTrue(all(f & os.O_NOFOLLOW for f in opened_flags))


if __name__ == "__main__":
    unittest.main()
