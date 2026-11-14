"""Tests for installation, CLI, CI, and documentation audit (Ticket #13)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT_DIR / "install.sh"
WORKFLOW_PATH = ROOT_DIR / ".github" / "workflows" / "python-app.yml"
README_PATH = ROOT_DIR / "README.md"
SCIFLOW_CICD = ROOT_DIR / "sciflow-cicd"


class CliCiAuditTests(unittest.TestCase):
    def test_installer_update_existing_installation(self):
        """install.sh should successfully update an existing installation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            install_dir = tmp_path / "share" / "sciflow-cicd"
            bin_dir = tmp_path / "bin"

            mock_bin = tmp_path / "mock_bin"
            mock_bin.mkdir()
            mock_gh = mock_bin / "gh"
            mock_gh.write_text('#!/bin/sh\nif [ "$1" = "auth" ] && [ "$2" = "status" ]; then exit 0; fi\nexit 0\n')
            mock_gh.chmod(0o755)

            env = os.environ.copy()
            env["SCIFLOW_CICD_REPO_URL"] = str(ROOT_DIR)
            env["SCIFLOW_CICD_INSTALL_DIR"] = str(install_dir)
            env["SCIFLOW_CICD_BIN_DIR"] = str(bin_dir)
            env["PATH"] = f"{mock_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}"

            # 1. First installation
            proc1 = subprocess.run(
                [str(INSTALL_SH)],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc1.returncode, 0, f"First install failed: {proc1.stderr}")
            self.assertTrue((bin_dir / "sciflow-cicd").exists())

            # Re-running install on the same directory updates it.
            proc2 = subprocess.run(
                [str(INSTALL_SH)], env=env, capture_output=True, text=True
            )
            self.assertEqual(proc2.returncode, 0, f"Update install failed: {proc2.stderr}")
            self.assertIn("Updating existing sciflow-cicd installation", proc2.stdout)
            self.assertTrue((bin_dir / "sciflow-cicd").exists())

    def test_installer_warns_when_older_command_precedes_install_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            install_dir = tmp_path / "share" / "sciflow-cicd"
            bin_dir = tmp_path / "bin"
            old_dir = tmp_path / "old-bin"
            old_dir.mkdir()
            old_command = old_dir / "sciflow-cicd"
            old_command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            old_command.chmod(0o755)
            mock_gh = old_dir / "gh"
            mock_gh.write_text('#!/bin/sh\nif [ "$1" = "auth" ] && [ "$2" = "status" ]; then exit 0; fi\nexit 0\n')
            mock_gh.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "SCIFLOW_CICD_REPO_URL": str(ROOT_DIR),
                "SCIFLOW_CICD_INSTALL_DIR": str(install_dir),
                "SCIFLOW_CICD_BIN_DIR": str(bin_dir),
                "PATH": f"{old_dir}:{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            })
            result = subprocess.run([str(INSTALL_SH)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("not first in your PATH", result.stdout)
            self.assertIn(f'export PATH="{bin_dir}:$PATH"', result.stdout)

    def test_ci_workflow_has_matrix_for_announced_python_versions(self):
        """CI workflow must test across announced Python versions (3.10, 3.11, 3.12)."""
        content = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("matrix:", content, "CI workflow should define a strategy matrix")
        self.assertIn('"3.10"', content)
        self.assertIn('"3.11"', content)
        self.assertIn('"3.12"', content)
        self.assertIn("matrix.python-version", content)

    def test_ci_workflow_distinguishes_blocking_from_informative_lint(self):
        """CI workflow must have a blocking syntax check and distinguish informative warnings."""
        content = WORKFLOW_PATH.read_text(encoding="utf-8")
        # Blocking syntax check
        self.assertIn("--select=E9,F63,F7,F82", content)
        # Informative styling step marked with exit-zero
        self.assertIn("--exit-zero", content)

    def test_readme_policy_examples_recommend_gh_setup(self):
        """README examples for policy application should recommend 'sciflow-cicd apply'."""
        content = README_PATH.read_text(encoding="utf-8")
        # Quick start section 5 should recommend sciflow-cicd apply
        self.assertIn(
            "sciflow-cicd apply --policy .local/setup-policy.json --dry-run-result .local/setup-dry-run.json",
            content,
            "README should recommend sciflow-cicd apply for policy runs",
        )


if __name__ == "__main__":
    unittest.main()
