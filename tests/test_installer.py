"""Tests for install.sh script and runtime preflight validations."""

import os
import shutil
import subprocess
from pathlib import Path
import stat
import tempfile
import unittest

ROOT_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT_DIR / "install.sh"


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(INSTALL_SH.exists(), "install.sh must exist")
        self.assertTrue(os.access(INSTALL_SH, os.X_OK), "install.sh must be executable")

    def _create_isolated_env(self, base_dir: Path) -> Path:
        """Create a mock bin directory containing core utilities needed by bash scripts."""
        mock_dir = base_dir / "mock_bin"
        mock_dir.mkdir(exist_ok=True)
        for utility in ("bash", "env", "mkdir", "dirname", "ln", "chmod"):
            real_path = shutil.which(utility)
            if real_path:
                sym = mock_dir / utility
                if not sym.exists():
                    sym.symlink_to(real_path)
        return mock_dir

    def _create_mock_bin(self, directory: Path, name: str, script_content: str) -> Path:
        """Create an executable mock command in the specified directory."""
        mock_path = directory / name
        mock_path.write_text(f"#!/bin/sh\n{script_content}\n", encoding="utf-8")
        mock_path.chmod(mock_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return mock_path

    def _run_install(
        self,
        tmp_path: Path,
        mock_dir: Path,
        extra_env: dict | None = None,
    ) -> subprocess.CompletedProcess:
        """Execute install.sh within an isolated environment directory."""
        install_dir = tmp_path / "share" / "gh-setup"
        bin_dir = tmp_path / "bin"
        env = os.environ.copy()
        env["HOME"] = str(tmp_path)
        env["GH_SETUP_REPO_URL"] = str(ROOT_DIR)
        env["GH_SETUP_INSTALL_DIR"] = str(install_dir)
        env["GH_SETUP_BIN_DIR"] = str(bin_dir)
        env["PATH"] = str(mock_dir)
        if extra_env:
            env.update(extra_env)
        return subprocess.run([str(INSTALL_SH)], env=env, capture_output=True, text=True)

    def test_install_script_runs_and_creates_symlink_and_doctor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            install_dir = tmp_path / "share" / "gh-setup"
            bin_dir = tmp_path / "bin"
            mock_dir = self._create_isolated_env(tmp_path)

            (mock_dir / "git").symlink_to(shutil.which("git"))
            (mock_dir / "python3").symlink_to(shutil.which("python3"))

            gh_script = 'if [ "$1" = "auth" ] && [ "$2" = "status" ]; then echo "Logged in"; exit 0; fi; exit 0'
            self._create_mock_bin(mock_dir, "gh", gh_script)

            proc = self._run_install(tmp_path, mock_dir)
            self.assertEqual(proc.returncode, 0, f"install.sh failed: {proc.stderr}")
            self.assertIn("installed successfully", proc.stdout)
            self.assertIn("not in your PATH", proc.stdout)

            target_bin = bin_dir / "gh-setup"
            self.assertTrue(target_bin.exists(), "Symlink in bin_dir must exist")
            self.assertTrue(target_bin.is_symlink(), "Target bin must be a symlink")
            self.assertEqual(target_bin.resolve(), (install_dir / "gh-setup").resolve())

            # Verify help command runs
            help_proc = subprocess.run(
                [str(target_bin), "--help"],
                env={"PATH": f"{mock_dir}:{bin_dir}"},
                capture_output=True,
                text=True,
            )
            self.assertEqual(help_proc.returncode, 0)
            self.assertIn("usage: gh-setup", help_proc.stdout)
            self.assertIn("apply", help_proc.stdout)
            self.assertIn("doctor", help_proc.stdout)

            # Verify doctor command runs
            doctor_proc = subprocess.run(
                [str(target_bin), "doctor"],
                env={"PATH": f"{mock_dir}:{bin_dir}"},
                capture_output=True,
                text=True,
            )
            self.assertEqual(doctor_proc.returncode, 0, f"gh-setup doctor failed: {doctor_proc.stderr}")
            self.assertIn("Verdict: System is healthy", doctor_proc.stdout)

            # Verify re-running installer upgrades existing directory cleanly
            rerun_proc = self._run_install(tmp_path, mock_dir)
            self.assertEqual(rerun_proc.returncode, 0, f"Rerunning install.sh failed: {rerun_proc.stderr}")
            self.assertIn("Updating existing gh-setup installation", rerun_proc.stdout)

    def test_install_fails_when_python_missing_or_outdated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            mock_dir = self._create_isolated_env(tmp_path)

            (mock_dir / "git").symlink_to(shutil.which("git"))
            self._create_mock_bin(mock_dir, "gh", 'exit 0')

            # 1. Python missing
            proc = self._run_install(tmp_path, mock_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Python 3 (version 3.10 or newer) is required", proc.stderr)

            # 2. Python outdated (e.g. 3.9)
            self._create_mock_bin(mock_dir, "python3", 'if [ "$1" = "-c" ]; then exit 1; fi; echo "Python 3.9.7"; exit 0')
            proc = self._run_install(tmp_path, mock_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Python 3.10 or newer is required", proc.stderr)

    def test_install_fails_when_git_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            mock_dir = self._create_isolated_env(tmp_path)

            (mock_dir / "python3").symlink_to(shutil.which("python3"))
            self._create_mock_bin(mock_dir, "gh", 'exit 0')

            proc = self._run_install(tmp_path, mock_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("git is required but was not found", proc.stderr)

    def test_install_fails_when_gh_missing_or_unauthenticated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            mock_dir = self._create_isolated_env(tmp_path)

            (mock_dir / "python3").symlink_to(shutil.which("python3"))
            (mock_dir / "git").symlink_to(shutil.which("git"))

            clean_env = {"GITHUB_TOKEN": "", "GH_TOKEN": ""}

            # 1. gh missing
            proc = self._run_install(tmp_path, mock_dir, extra_env=clean_env)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("GitHub CLI (gh) is required", proc.stderr)

            # 2. gh present but unauthenticated
            self._create_mock_bin(mock_dir, "gh", 'echo "You are not logged in"; exit 1')
            proc = self._run_install(tmp_path, mock_dir, extra_env=clean_env)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("GitHub CLI (gh) is not authenticated", proc.stderr)


if __name__ == "__main__":
    unittest.main()
