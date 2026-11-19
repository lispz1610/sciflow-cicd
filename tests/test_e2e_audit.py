"""End-to-end audit and validation test suite for sciflow-cicd.

Covers:
- Clean execution and audit of install.sh in fresh isolated environments (tempdir, PATH check, symlinks).
- Interactive configuration end-to-end against a clean repository:
  - Standard input simulation through public CLI entrypoints.
  - Repository validation and verification.
  - State detection identifying no existing configuration.
  - Questionnaire stage selections.
  - Policy assembly and persistence to .local/setup-policy.json.
  - Dry-run preview calculation without pre-emptive writes.
  - User confirmation and apply execution.
- Re-running interactive configuration against the configured repository:
  - State detection detecting existing setup.
  - Resumption mode pre-selecting detected defaults.
  - Dry-run calculation detecting convergence (zero planned changes).
  - No unexpected writes or modifications.
- Verifying complete test suite execution with no regressions.
"""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
TESTS_DIR = ROOT_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

INSTALL_SH = ROOT_DIR / "install.sh"
SCIFLOW_CICD_ENTRY = ROOT_DIR / "sciflow-cicd"

from test_setup_github_repo import InventoryApi
from scripts.cli import run_interactive, main as cli_main
from scripts.interactive import Session, has_existing_configuration


class CleanRepoApi(InventoryApi):
    """Simulated GitHub API for a clean repository with no initial configuration."""

    def __init__(self, repo_name="acme/project"):
        super().__init__()
        self.repo_name = repo_name
        self.repo["full_name"] = repo_name
        self.repo["has_issues"] = False
        self.repo["has_wiki"] = False
        self.repo["has_projects"] = False
        self.repo["allow_merge_commit"] = False
        self.repo["allow_squash_merge"] = False
        self.repo["allow_rebase_merge"] = False
        self.repo["delete_branch_on_merge"] = False
        self.repo["allow_update_branch"] = False
        self.workflow = {}
        self.actions = {
            "enabled": False,
            "allowed_actions": "all",
            "sha_pinning_required": False,
        }
        self.features = {
            "vulnerability-alerts": False,
            "automated-security-fixes": False,
            "private-vulnerability-reporting": False,
        }
        self.security_and_analysis = {
            "secret_scanning": {"status": "disabled"},
            "secret_scanning_push_protection": {"status": "disabled"},
        }
        self.rulesets = []
        self.workflow_files = {}
        self.workflow_refs = {}
        self.workflow_prs = []

    def request(self, method, path, payload=None):
        base = "/repos/" + self.repo_name
        if method == "GET" and (path.startswith("/repos/actions/") or path.startswith("/repos/astral-sh/")) and "/git/commits/" in path:
            sha = path.split("/git/commits/")[1]
            return 200, {"sha": sha}
        if method == "GET" and path.startswith(base + "/git/ref/heads/"):
            branch = unquote(path[len(base + "/git/ref/heads/"):])
            if branch == self.repo.get("default_branch", "master"):
                return 200, {"object": {"sha": "a" * 40}}
            if branch in self.workflow_refs:
                return 200, {"object": {"sha": self.workflow_refs[branch]}}
            return 404, None
        if method == "GET" and path.startswith(base + "/pulls?state=open&base="):
            return 200, self.workflow_prs
        if method == "GET" and path.startswith(base + "/pulls?head="):
            return 200, self.workflow_prs
        if method == "POST" and path == base + "/git/refs":
            self.writes.append((method, path, payload))
            self.workflow_refs[payload["ref"].removeprefix("refs/heads/")] = payload["sha"]
            return 201, {"ref": payload["ref"]}
        if method == "GET" and path.startswith(base + "/compare/"):
            branch = unquote(path.split("...", 1)[1])
            return 200, {"files": [{"filename": filename, "status": "added"}
                                   for (ref, filename) in self.workflow_files if ref == branch]}
        if method == "GET" and path.startswith(base + "/contents/.github/workflows/"):
            wf_path = path.split("?")[0][len(base + "/contents/"):]
            branch = unquote(path.split("?ref=", 1)[1]) if "?ref=" in path else self.repo.get("default_branch", "master")
            if (branch, wf_path) in self.workflow_files:
                import base64
                content_bytes = self.workflow_files[(branch, wf_path)]
                return 200, {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(content_bytes).decode("ascii"),
                    "sha": "b" * 40,
                }
            return 404, None
        if method == "PUT" and path.startswith(base + "/contents/.github/workflows/"):
            self.writes.append((method, path, payload))
            import base64
            filename = path.removeprefix(base + "/contents/")
            content = base64.b64decode(payload["content"])
            self.workflow_files[(payload["branch"], filename)] = content
            return 200, {"content": {"sha": "b" * 40}}
        if method == "POST" and path == base + "/pulls":
            self.writes.append((method, path, payload))
            pull = {"number": 1, "html_url": "https://github.com/acme/project/pull/1",
                    "head": {"ref": payload["head"]}, "base": {"ref": payload["base"]}, "draft": True}
            self.workflow_prs.append(pull)
            return 201, pull
        return super().request(method, path, payload)


class TestInstallerScriptAudit(unittest.TestCase):
    """Audit tests for the install.sh installer script in clean, isolated environments."""

    def _setup_mock_gh(self, directory: Path) -> Path:
        mock_bin = directory / "mock_bin"
        mock_bin.mkdir(exist_ok=True)
        gh = mock_bin / "gh"
        gh.write_text('#!/bin/sh\nif [ "$1" = "auth" ] && [ "$2" = "status" ]; then exit 0; fi\nexit 0\n')
        gh.chmod(0o755)
        return mock_bin

    def test_installer_clean_execution_in_fresh_environment(self):
        """Installer clones repository, creates symlink, and reports PATH notice when bin_dir is not in PATH."""
        self.assertTrue(INSTALL_SH.exists(), "install.sh must exist in repository root")
        self.assertTrue(os.access(INSTALL_SH, os.X_OK), "install.sh must be executable")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            install_dir = tmp_path / "share" / "sciflow-cicd"
            bin_dir = tmp_path / "bin"
            mock_bin = self._setup_mock_gh(tmp_path)

            env = os.environ.copy()
            env["SCIFLOW_CICD_REPO_URL"] = str(ROOT_DIR)
            env["SCIFLOW_CICD_INSTALL_DIR"] = str(install_dir)
            env["SCIFLOW_CICD_BIN_DIR"] = str(bin_dir)
            env["PATH"] = f"{mock_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}"

            proc = subprocess.run(
                [str(INSTALL_SH)],
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(proc.returncode, 0, f"install.sh failed with stderr: {proc.stderr}")
            self.assertIn("Cloning sciflow-cicd into", proc.stdout)
            self.assertIn("installed successfully to", proc.stdout)
            self.assertIn("not in your PATH", proc.stdout)

            target_bin = bin_dir / "sciflow-cicd"
            self.assertTrue(target_bin.exists(), "Symlink in bin_dir must exist")
            self.assertTrue(target_bin.is_symlink(), "Target binary must be a symlink")
            self.assertEqual(
                target_bin.resolve(),
                (install_dir / "sciflow-cicd").resolve(),
                "Symlink must resolve to cloned sciflow-cicd entrypoint",
            )
            self.assertTrue(
                os.access(target_bin, os.X_OK),
                "Installed symlink target must be executable",
            )

    def test_installer_when_bin_dir_in_path_and_execution_verification(self):
        """Installer suppresses PATH notice when bin_dir is in PATH, and installed binary runs correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            install_dir = tmp_path / "share" / "sciflow-cicd"
            bin_dir = tmp_path / "bin"
            mock_bin = self._setup_mock_gh(tmp_path)

            env = os.environ.copy()
            env["SCIFLOW_CICD_REPO_URL"] = str(ROOT_DIR)
            env["SCIFLOW_CICD_INSTALL_DIR"] = str(install_dir)
            env["SCIFLOW_CICD_BIN_DIR"] = str(bin_dir)
            env["PATH"] = f"{bin_dir}:{mock_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}"

            proc = subprocess.run(
                [str(INSTALL_SH)],
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(proc.returncode, 0, f"install.sh failed: {proc.stderr}")
            self.assertIn("installed successfully", proc.stdout)
            self.assertNotIn("not in your PATH", proc.stdout)

            # Verify direct invocation of the installed symlink
            target_bin = bin_dir / "sciflow-cicd"
            help_proc = subprocess.run(
                [str(target_bin), "--help"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(help_proc.returncode, 0)
            self.assertIn("sciflow-cicd", help_proc.stdout)
            self.assertIn("available commands", help_proc.stdout)
            self.assertIn("apply", help_proc.stdout)

    def test_installer_idempotent_reinstallation_updates_existing(self):
        """Re-running installer against an existing installation updates cleanly via git."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            install_dir = tmp_path / "share" / "sciflow-cicd"
            bin_dir = tmp_path / "bin"
            mock_bin = self._setup_mock_gh(tmp_path)

            env = os.environ.copy()
            env["SCIFLOW_CICD_REPO_URL"] = str(ROOT_DIR)
            env["SCIFLOW_CICD_INSTALL_DIR"] = str(install_dir)
            env["SCIFLOW_CICD_BIN_DIR"] = str(bin_dir)
            env["PATH"] = f"{bin_dir}:{mock_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}"

            # Initial installation
            proc1 = subprocess.run([str(INSTALL_SH)], env=env, capture_output=True, text=True)
            self.assertEqual(proc1.returncode, 0)

            # Re-installation in same location
            proc2 = subprocess.run([str(INSTALL_SH)], env=env, capture_output=True, text=True)
            self.assertEqual(proc2.returncode, 0)
            self.assertIn(
                "Updating existing sciflow-cicd installation",
                proc2.stdout,
                f"Expected update message in stdout: {proc2.stdout}",
            )
            self.assertIn("installed successfully", proc2.stdout)


class TestCleanRepositoryInteractiveEndToEnd(unittest.TestCase):
    """End-to-end interactive configuration tests against a clean repository."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.policy_file = Path(self.temp_dir) / ".local" / "setup-policy.json"
        self.api = CleanRepoApi(repo_name="acme/project")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_clean_repo_interactive_workflow_apply_and_verify(self):
        """End-to-end user journey: repo validation -> clean state detection -> stage selection -> policy assembly -> dry-run -> confirm -> apply."""
        # Simulated user inputs:
        # 1. Target repository: acme/project
        # 2. Features: 1 (Issues)
        # 3. Merge settings: n (decline)
        # 4. Actions permissions: n (decline)
        # 5. Security features: 1 (Vulnerability alerts)
        # 6. Repository content: n (decline)
        # 7. Workflows: n (decline)
        # 8. Gates: n (decline)
        # 9. Apply confirmation: y (confirm)
        user_inputs = "acme/project\n1\nn\nn\n1\nn\nn\nn\nn\nn\ny\n"
        in_stream = io.StringIO(user_inputs)
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        with patch("scripts.interactive.GitHubApi", return_value=self.api), \
             patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", self.policy_file), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "audit-test-token"}):
            exit_code = run_interactive(
                in_stream=in_stream,
                out_stream=out_stream,
                err_stream=err_stream,
            )

        self.assertEqual(exit_code, 0, f"Interactive session failed. stderr: {err_stream.getvalue()}")

        output = out_stream.getvalue()
        # 1. Repository validation
        self.assertIn("Repository 'acme/project' verified successfully.", output)

        # 2. State detection: clean repository
        self.assertIn("No previous configuration detected. Starting fresh setup.", output)

        # 3. Policy assembled and persisted
        self.assertIn("Policy file successfully written to", output)
        self.assertTrue(self.policy_file.exists(), "Policy file must be created at .local/setup-policy.json")
        policy = json.loads(self.policy_file.read_text(encoding="utf-8"))
        self.assertEqual(policy["version"], 2)
        self.assertEqual(policy["repository"], "acme/project")
        self.assertEqual(
            policy["stages"],
            [
                {"name": "features", "enable": ["issues"]},
                {"name": "security", "enable": ["vulnerability-alerts"]},
            ],
        )

        # 4. Dry-run preview
        self.assertIn("Dry-Run Summary: Proposed Changes for acme/project", output)
        self.assertIn("features", output)
        self.assertIn("security", output)

        # 5. User confirmation & apply
        self.assertIn("Apply these changes?", output)
        self.assertIn("Apply Summary:", output)
        self.assertIn("All changes applied and verified successfully.", output)
        self.assertIn("verified", output.lower())

        # 6. Writes executed against API
        self.assertEqual(len(self.api.writes), 3)
        self.assertTrue(self.api.repo.get("has_issues"))
        self.assertTrue(self.api.features.get("vulnerability-alerts"))
        self.assertTrue(self.api.features.get("automated-security-fixes"))

    def test_clean_repo_interactive_workflow_with_workflows_and_gates(self):
        """Interactive setup session configuring workflows and gates stages applies and verifies cleanly."""
        # Simulated user inputs:
        # 1. Target repository: acme/project
        # 2. Features: 1 (Issues)
        # 3. Merge settings: n
        # 4. Actions: n (decline)
        # 5. Security: none
        # 6. Repository content: n
        # 7. Workflows confirm: y
        # Runner default self-hosted: Yes (\n)
        # Runner labels: self-hosted (\n)
        # 8. Workflows selection: 1 (Standard PR CI)
        # Commands: default (\n)
        # Triggers: default (\n)
        # Override runner: default No (\n)
        # 9. Gates confirm: y
        # 10. Evidence PR: 7
        # 11. Reviewer: reviewer
        # 12. Check name: ci-check
        # 13. Apply confirm: y
        user_inputs = "acme/project\n1\nn\nn\nnone\nn\ny\n\n\n1\n\n\n\ny\n7\nreviewer\nci-check\nn\nn\ny\n"
        in_stream = io.StringIO(user_inputs)
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        with patch("scripts.interactive.GitHubApi", return_value=self.api), \
             patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", self.policy_file), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "audit-test-token"}):
            exit_code = run_interactive(
                in_stream=in_stream,
                out_stream=out_stream,
                err_stream=err_stream,
            )

        self.assertEqual(exit_code, 0, f"Interactive session failed: {err_stream.getvalue()}")
        output = out_stream.getvalue()

        # Repository verified
        self.assertIn("Repository 'acme/project' verified successfully.", output)

        # Policy was assembled and persisted with workflows and gates stages
        self.assertTrue(self.policy_file.exists())
        policy = json.loads(self.policy_file.read_text(encoding="utf-8"))
        self.assertEqual(policy["repository"], "acme/project")
        stage_names = [s["name"] for s in policy["stages"]]
        self.assertIn("workflows", stage_names)
        self.assertIn("gates", stage_names)

        # Dry-run summary contains both stages
        self.assertIn("Dry-Run Summary", output)
        self.assertIn("Stage: workflows", output)
        self.assertIn("Stage: gates", output)

        # Direct settings apply; the gate waits for the Setup Pull Request and CI evidence.
        self.assertIn("Apply Summary:", output)
        self.assertIn("Pending:", output)
        self.assertIn("successful CI checks", output)
        self.assertFalse(any(w[0] == "POST" and "rulesets" in w[1] for w in self.api.writes))
        self.assertIn("Verified direct settings are complete; pending work still needs owner action.", output)
        self.assertIn("Setup Pull Request: https://github.com/acme/project/pull/1", output)
        self.assertTrue(any("workflows" in w[1] for w in self.api.writes))

    def test_clean_repo_interactive_workflow_rejection_at_confirmation(self):
        """User rejects apply confirmation: policy is persisted for headless reuse, but no changes applied."""
        # Simulated user inputs:
        # Same questionnaire choices, but 'n' at apply confirmation
        user_inputs = "acme/project\n1\nn\nn\n1\nn\nn\nn\nn\nn\n"
        in_stream = io.StringIO(user_inputs)
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        with patch("scripts.interactive.GitHubApi", return_value=self.api), \
             patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", self.policy_file), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "audit-test-token"}):
            exit_code = run_interactive(
                in_stream=in_stream,
                out_stream=out_stream,
                err_stream=err_stream,
            )

        self.assertEqual(exit_code, 0)
        output = out_stream.getvalue()

        # Policy was persisted
        self.assertTrue(self.policy_file.exists())

        # Dry-run was shown
        self.assertIn("Dry-Run Summary", output)

        # Cancelled notice
        self.assertIn("cancelled", output.lower())
        self.assertIn("No changes were applied", output)

        # ZERO writes performed
        self.assertEqual(self.api.writes, [])
        self.assertFalse(self.api.repo.get("has_issues"))
        self.assertFalse(self.api.features.get("vulnerability-alerts"))

    def test_clean_repo_interactive_reprompt_on_invalid_repository_name(self):
        """Entering invalid repository format prompts descriptive error and reprompts until valid."""
        user_inputs = "invalid-name\nacme/project\n1\nn\nn\n1\nn\nn\nn\nn\nn\ny\n"
        in_stream = io.StringIO(user_inputs)
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        with patch("scripts.interactive.GitHubApi", return_value=self.api), \
             patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", self.policy_file), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "audit-test-token"}):
            exit_code = run_interactive(
                in_stream=in_stream,
                out_stream=out_stream,
                err_stream=err_stream,
            )

        self.assertEqual(exit_code, 0)
        output = out_stream.getvalue()
        self.assertIn("Invalid repository format 'invalid-name'", output)
        self.assertIn("Repository 'acme/project' verified successfully.", output)
        self.assertEqual(len(self.api.writes), 3)

    def test_clean_repo_interactive_missing_token_reports_error(self):
        """Missing GITHUB_TOKEN environment variable exits 1 with actionable error."""
        in_stream = io.StringIO("acme/project\n")
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        with patch.dict(os.environ, {}, clear=True):
            exit_code = run_interactive(
                in_stream=in_stream,
                out_stream=out_stream,
                err_stream=err_stream,
            )

        self.assertEqual(exit_code, 1)
        err = err_stream.getvalue()
        self.assertIn("GITHUB_TOKEN environment variable is required", err)


class TestReconfiguredRepositoryResumeConvergence(unittest.TestCase):
    """Tests for re-running interactive setup against configured repositories: resume mode, defaults, and convergence."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.policy_file = Path(self.temp_dir) / ".local" / "setup-policy.json"
        self.api = CleanRepoApi(repo_name="acme/project")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_rerun_interactive_detects_existing_setup_resumes_and_converges(self):
        """Re-running against configured repo detects setup, pre-fills defaults, computes dry-run with 0 changes, and converges without writes."""
        # Step 1: Initial run against clean repository
        run1_inputs = "acme/project\n1\nn\nn\n1\nn\nn\nn\nn\nn\ny\n"
        with patch("scripts.interactive.GitHubApi", return_value=self.api), \
             patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", self.policy_file), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "audit-test-token"}):
            exit_code1 = run_interactive(
                in_stream=io.StringIO(run1_inputs),
                out_stream=io.StringIO(),
                err_stream=io.StringIO(),
            )
        self.assertEqual(exit_code1, 0)
        writes_after_run1 = len(self.api.writes)
        self.assertEqual(writes_after_run1, 3)
        self.assertTrue(self.api.repo.get("has_issues"))
        self.assertTrue(self.api.features.get("vulnerability-alerts"))
        self.assertTrue(self.api.features.get("automated-security-fixes"))

        # Step 2: Re-run against configured repository
        # Simulated user inputs:
        # 1. Target repository: acme/project
        # 2. Resume choice: 1 (Resume existing setup)
        # 3. Features prompt: Enter (accept pre-selected default [1: Issues])
        # 4. Merge prompt: Enter (accept default n)
        # 5. Actions prompt: Enter (accept default n)
        # 6. Security prompt: Enter (accept pre-selected default [1: Vulnerability alerts])
        # 7. Repository content prompt: Enter (accept default n)
        # 8. Workflows prompt: Enter (accept default n)
        # 9. Gates prompt: Enter (accept default n)
        # Because convergence is achieved (0 planned changes), no apply confirmation is prompted!
        run2_inputs = "acme/project\n1\n\n\n\n\n\n\n\n\n\n"
        out_stream2 = io.StringIO()
        err_stream2 = io.StringIO()

        with patch("scripts.interactive.GitHubApi", return_value=self.api), \
             patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", self.policy_file), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "audit-test-token"}):
            exit_code2 = run_interactive(
                in_stream=io.StringIO(run2_inputs),
                out_stream=out_stream2,
                err_stream=err_stream2,
            )

        self.assertEqual(exit_code2, 0, f"Re-run failed. stderr: {err_stream2.getvalue()}")

        output2 = out_stream2.getvalue()

        # 1. Detected existing configuration
        self.assertIn("Existing repository configuration detected. How would you like to proceed?", output2)
        self.assertIn("Detected Repository Configuration:", output2)
        self.assertIn("Issues: Enabled", output2)
        self.assertIn("Vulnerability Alerts: Enabled", output2)

        # 2. Resume mode confirmation
        self.assertIn("Resuming setup with detected configuration as defaults.", output2)

        # 3. Verification of pre-selected defaults in questionnaires
        self.assertIn("[x] 1. Issues", output2)
        self.assertIn("[x] 1. Vulnerability alerts", output2)

        # 4. Dry-run calculation detects convergence
        self.assertIn("Dry-Run Summary: Proposed Changes for acme/project", output2)
        self.assertIn("No changes needed. Repository is already in the desired state.", output2)
        self.assertIn("Repository is already configured as declared. No changes to apply.", output2)

        # 5. Convergence: ZERO new writes executed!
        new_writes = len(self.api.writes) - writes_after_run1
        self.assertEqual(new_writes, 0, f"Expected 0 writes on converged re-run, but got {new_writes}")

    def test_rerun_interactive_scratch_mode_clears_defaults(self):
        """When existing configuration is detected, choosing start from scratch clears defaults."""
        # Pre-configure the repository state
        self.api.repo["has_issues"] = True
        self.api.features["vulnerability-alerts"] = True

        # User inputs:
        # 1. Target repository: acme/project
        # 2. Resume choice: 2 (Start from scratch)
        # 3. Features: none
        # 4. Merge settings: n
        # 5. Actions: n
        # 6. Security: none
        # 7. Repository content: n
        # 8. Workflows: n
        # 9. Gates: n
        user_inputs = "acme/project\n2\nnone\nn\nn\nnone\nn\nn\nn\nn\nn\n"
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        with patch("scripts.interactive.GitHubApi", return_value=self.api), \
             patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", self.policy_file), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "audit-test-token"}):
            exit_code = run_interactive(
                in_stream=io.StringIO(user_inputs),
                out_stream=out_stream,
                err_stream=err_stream,
            )

        self.assertEqual(exit_code, 0)
        output = out_stream.getvalue()
        self.assertIn("Starting setup from scratch with cleared defaults.", output)
        self.assertIn("No configuration changes selected. Setup completed without modifications.", output)
        self.assertEqual(self.api.writes, [])

    def test_rerun_interactive_resume_mode_with_incremental_change(self):
        """In resume mode, user can incrementally add a new stage and apply only the incremental change."""
        # Initial state: Issues enabled
        self.api.repo["has_issues"] = True

        # User chooses resume (1), then in features selects "1, 2" (Issues + Wiki), then declines others and confirms apply
        # 1. repo: acme/project
        # 2. resume: 1
        # 3. features: 1, 2 (Issues, Wiki)
        # 4. merge settings: n
        # 5. actions: n
        # 6. security: none
        # 7. repository content: n
        # 8. workflows: n
        # 9. gates: n
        # 10. apply confirm: y
        user_inputs = "acme/project\n1\n1, 2\nn\nn\nnone\nn\nn\nn\nn\nn\ny\n"
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        with patch("scripts.interactive.GitHubApi", return_value=self.api), \
             patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", self.policy_file), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "audit-test-token"}):
            exit_code = run_interactive(
                in_stream=io.StringIO(user_inputs),
                out_stream=out_stream,
                err_stream=err_stream,
            )

        self.assertEqual(exit_code, 0)
        output = out_stream.getvalue()
        self.assertIn("Dry-Run Summary", output)
        self.assertIn("Apply Summary", output)
        self.assertTrue(self.api.repo.get("has_wiki"))
        self.assertTrue(self.api.repo.get("has_issues"))


class TestFullTestSuiteRegression(unittest.TestCase):
    """Verify that all unit tests across the repository pass without regressions."""

    def test_all_unit_test_suites_pass_without_regressions(self):
        """Run all tests across tests/ directory in clean process and verify zero regressions."""
        if os.environ.get("SCIFLOW_CICD_REGRESSION_CHILD") == "1":
            return

        env = os.environ.copy()
        env["SCIFLOW_CICD_REGRESSION_CHILD"] = "1"
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(TESTS_DIR), "-p", "test_*.py"],
            cwd=str(ROOT_DIR),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Test suite regression detected! exit={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
        )
        self.assertIn("OK", proc.stderr)


if __name__ == "__main__":
    unittest.main()
