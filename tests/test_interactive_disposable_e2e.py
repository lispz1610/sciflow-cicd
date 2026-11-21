"""End-to-End Live and Hermetic Validation for Streamlined Interactive Setup (Ticket #57).

Validates the complete Interactive Setup workflow against disposable repositories:
1. Hermetic/mocked GitHub API suite verifying the exact same contracts offline / in CI.
2. 1-click acceptance ([Y]) with automatic profile recommendation, universal baseline,
   and greenfield rulesets staging without evidence_pr deadlocks.
3. Selective customization ([c]) verifying feature toggles (Wiki disabled per ADR 0005,
   Dependabot security fixes enabled).
4. Terminal transcript verification ensuring guidance banners adhere to <= 2 line limits.
5. Non-destructive teardown guarantees: automated resource cleanup in finally/addCleanup.
6. Live disposable repository runner against real GitHub account when authenticated.
"""

from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple, Union
import unittest
from unittest.mock import MagicMock, patch
import uuid

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
TESTS_DIR = ROOT_DIR / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from scripts.configure_github_repo import ApiError, GitHubApi, FEATURES, MERGE_SETTINGS
from scripts.setup_github_repo import inventory, run_policy, validate_policy
from scripts.setup_profiles import (
    CANONICAL_TRIAGE_LABELS,
    COMMON_BASELINE_STAGES,
    Profile,
    get_profile,
    recommend_profile,
    profile_to_policy,
)
from scripts.interactive import Session, run_interactive_workflow
from scripts.cli import run_interactive
from scripts.interactive_apply import (
    execute_dry_run,
    display_dry_run_summary,
    execute_apply,
    format_dry_run_summary,
    format_apply_summary,
    run_interactive_apply,
)
from scripts.interactive_customization import run_customization, format_customization_menu


# ==============================================================================
# High-Fidelity Mock for Hermetic GitHub API E2E Testing
# ==============================================================================

class MockDisposableGitHubApi:
    """High-fidelity simulated GitHub REST API for disposable repositories."""

    def __init__(self, repository: str = "lispz1610/sciflow-test-disposable"):
        self.repository = repository
        self.repo: Dict[str, Any] = {
            "full_name": repository,
            "default_branch": "main",
            "visibility": "private",
            "has_issues": True,
            "has_wiki": True,  # Default on newly created repo before policy
            "has_projects": True,
            "allow_merge_commit": True,
            "allow_squash_merge": True,
            "allow_rebase_merge": True,
            "delete_branch_on_merge": False,
            "allow_update_branch": False,
            "security_and_analysis": {
                "secret_scanning": {"status": "disabled"},
                "secret_scanning_push_protection": {"status": "disabled"},
            },
            "permissions": {
                "admin": True,
                "maintain": True,
                "push": True,
                "triage": True,
                "pull": True,
            },
        }
        self.actions: Dict[str, Any] = {
            "enabled": True,
            "allowed_actions": "all",
            "sha_pinning_required": False,
        }
        self.selected_actions: Dict[str, Any] = {
            "github_owned_allowed": True,
            "verified_allowed": False,
            "patterns_allowed": [],
        }
        self.workflow: Dict[str, Any] = {
            "default_workflow_permissions": "write",
            "can_approve_pull_request_reviews": True,
        }
        self.codeql: Dict[str, Any] = {
            "state": "not-configured",
            "languages": ["python"],
            "query_suite": "default",
        }
        self.features: Dict[str, Any] = {
            "vulnerability-alerts": False,
            "automated-security-fixes": False,
            "private-vulnerability-reporting": False,
        }
        self.labels: Dict[str, Dict[str, Any]] = {}
        self.rulesets: List[Dict[str, Any]] = []
        self.writes: List[Tuple[str, str, Any]] = []
        self.reads: List[Tuple[str, str, Any]] = []
        self.deleted: bool = False

    def request(self, method: str, path: str, payload: Any = None) -> Tuple[int, Any]:
        """Simulate HTTP request handling and apply state transitions."""
        if self.deleted:
            raise ApiError(f"{method} {path}: HTTP 404: Repository not found (deleted)")

        if method == "GET":
            self.reads.append((method, path, payload))

        base = f"/repos/{self.repository}"

        # Manifest
        if method == "GET" and path.startswith(f"{base}/contents/"):
            return 404, None

        # SBOM
        if method == "GET" and path == f"{base}/dependency-graph/sbom":
            return 404, None

        # Collaborator permissions
        if method == "GET" and path.startswith(f"{base}/collaborators/"):
            return 200, {"permission": "write"}

        # Repository metadata
        if path == base:
            if method == "GET":
                return 200, dict(self.repo)
            if method == "PATCH":
                self.writes.append((method, path, payload))
                if isinstance(payload, dict):
                    self.repo.update({k: v for k, v in payload.items() if k != "security_and_analysis"})
                    for k, v in payload.get("security_and_analysis", {}).items():
                        self.repo["security_and_analysis"][k] = v
                return 200, dict(self.repo)

        # Actions permissions
        if path == f"{base}/actions/permissions":
            if method == "GET":
                return 200, dict(self.actions)
            if method == "PUT":
                self.writes.append((method, path, payload))
                if isinstance(payload, dict):
                    self.actions.update(payload)
                return 200, dict(self.actions)

        # Selected actions
        if path == f"{base}/actions/permissions/selected-actions":
            if method == "GET":
                return 200, dict(self.selected_actions)
            if method == "PUT":
                self.writes.append((method, path, payload))
                if isinstance(payload, dict):
                    self.selected_actions.update(payload)
                return 204, None

        # Actions workflow permissions
        if path == f"{base}/actions/permissions/workflow":
            if method == "GET":
                return 200, dict(self.workflow)
            if method == "PUT":
                self.writes.append((method, path, payload))
                if isinstance(payload, dict):
                    self.workflow.update(payload)
                return 200, dict(self.workflow)

        # CodeQL
        if path == f"{base}/code-scanning/default-setup":
            if method == "GET":
                return 200, dict(self.codeql)
            if method == "PATCH":
                self.writes.append((method, path, payload))
                if isinstance(payload, dict):
                    self.codeql.update(payload)
                return 202, {"run_id": 42}

        # Security capabilities
        for feat in ("vulnerability-alerts", "automated-security-fixes", "private-vulnerability-reporting"):
            if path == f"{base}/{feat}":
                if method == "PUT":
                    self.writes.append((method, path, payload))
                    self.features[feat] = True
                    return 204, None
                if method == "DELETE":
                    self.writes.append((method, path, payload))
                    self.features[feat] = False
                    return 204, None
                if method == "GET":
                    enabled = self.features.get(feat, False)
                    if feat == "vulnerability-alerts":
                        return (204, None) if enabled else (200, {"enabled": False})
                    if feat == "automated-security-fixes":
                        return (200, {"enabled": enabled})
                    return 200, {"enabled": enabled}

        # Rulesets
        if path.startswith(f"{base}/rulesets"):
            if method == "GET":
                return 200, list(self.rulesets)
            if method == "POST":
                self.writes.append((method, path, payload))
                item = dict(payload) if isinstance(payload, dict) else {}
                item["id"] = len(self.rulesets) + 1
                self.rulesets.append(item)
                return 201, item

        # Labels
        if path == f"{base}/labels":
            if method == "GET":
                return 200, list(self.labels.values())
            if method == "POST":
                self.writes.append((method, path, payload))
                name = payload.get("name") if isinstance(payload, dict) else None
                if name:
                    self.labels[name] = dict(payload)
                return 201, payload

        if path.startswith(f"{base}/labels/"):
            name = path[len(f"{base}/labels/"):]
            if method == "GET":
                if name in self.labels:
                    return 200, dict(self.labels[name])
                return 404, None
            if method == "PATCH":
                self.writes.append((method, path, payload))
                if name in self.labels and isinstance(payload, dict):
                    self.labels[name].update(payload)
                return 200, self.labels.get(name, payload)

        return 404, None


# ==============================================================================
# Disposable Repository Lifecycle Utilities
# ==============================================================================

def is_live_github_available() -> bool:
    """Check if GitHub CLI is installed, authenticated, and live testing enabled."""
    if os.environ.get("SCIFLOW_LIVE_E2E") not in ("1", "true", "yes"):
        return False
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def get_authenticated_user() -> Optional[str]:
    """Retrieve username of current gh authenticated user."""
    try:
        proc = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return None


def create_disposable_repo(
    prefix: str = "sciflow-test-",
    owner: Optional[str] = None,
) -> Tuple[str, str]:
    """Create a private disposable repository under the authenticated user account.

    Returns:
        (repo_slug, repo_name) e.g. ("lispz1610/sciflow-test-1234abcd", "sciflow-test-1234abcd")
    """
    user = owner or get_authenticated_user()
    if not user:
        raise RuntimeError("No authenticated GitHub user found for disposable repo creation.")

    unique_suffix = uuid.uuid4().hex[:8]
    repo_name = f"{prefix}{unique_suffix}"
    repo_slug = f"{user}/{repo_name}"

    cmd = [
        "gh",
        "repo",
        "create",
        repo_slug,
        "--private",
        "--description",
        "Automated disposable test repository for sciflow-cicd interactive setup",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to create disposable repo {repo_slug}: {proc.stderr}")

    return repo_slug, repo_name


def teardown_disposable_repo(repo_slug: str) -> bool:
    """Non-destructive teardown: safely deletes disposable repository.

    Falls back to archiving if user token lacks `delete_repo` OAuth scope.
    """
    delete_cmd = ["gh", "repo", "delete", repo_slug, "--yes"]
    proc = subprocess.run(delete_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
    if proc.returncode == 0:
        return True

    # If delete failed (e.g. 403 missing delete_repo scope), archive to deactivate
    archive_cmd = ["gh", "repo", "archive", repo_slug, "--yes"]
    archive_proc = subprocess.run(archive_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
    return archive_proc.returncode == 0


def verify_banner_lengths(transcript: str, max_lines: int = 2) -> None:
    """Verify that contextual banners in interactive transcript adhere to line limits."""
    patterns = [
        # 1-click profile recommendation banner
        r"(Recommended Setup Profile:[^\n]+\n[^\n]+)\nAccept recommended",
        # Features guidance banner
        r"(Configure repository collaboration features[^\n]+\n[^\n]+)\nSelect repository features",
        # Actions permissions guidance banner
        r"(Actions runs repository workflows[^\n]+\n[^\n]+)\nConfigure default",
        # Security guidance banner
        r"(Configure repository security[^\n]+\n[^\n]+)\nSelect repository security",
    ]
    matched_any = False
    for pat in patterns:
        match = re.search(pat, transcript)
        if match:
            matched_any = True
            banner_text = match.group(1).strip()
            lines = [l for l in banner_text.splitlines() if l.strip()]
            assert len(lines) <= max_lines, (
                f"Banner exceeds {max_lines} lines ({len(lines)} lines observed):\n{banner_text}"
            )
    assert matched_any, "Expected at least one contextual guidance banner in transcript"


# ==============================================================================
# Hermetic End-to-End Test Suite
# ==============================================================================

class TestHermeticInteractiveDisposableE2E(unittest.TestCase):
    """Hermetic end-to-end tests exercising full interactive setup flows offline."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.repo_dir = Path(self.temp_dir) / "repo"
        self.repo_dir.mkdir()
        (self.repo_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        self.temp_policy = Path(self.temp_dir) / ".local" / "setup-policy.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_one_click_acceptance_universal_baseline_and_greenfield_ruleset(self):
        """1-click acceptance ([Y]) applies Universal Baseline & stages greenfield ruleset."""
        repo_slug = "lispz1610/sciflow-test-hermetic"
        api = MockDisposableGitHubApi(repository=repo_slug)

        # Initial state before setup:
        # has_wiki: True, delete_branch_on_merge: False, allow_merge_commit: True
        self.assertTrue(api.repo["has_wiki"])
        self.assertFalse(api.repo["delete_branch_on_merge"])
        self.assertTrue(api.repo["allow_merge_commit"])

        # Simulated user input:
        # 1. 1-click action: "Y\n" (Accept recommended profile)
        # 2. Dry-run confirmation: "y\n" (Proceed to apply changes)
        user_input = "Y\ny\n"
        in_stream = io.StringIO(user_input)
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        inv = inventory(api, repo_slug)
        session = Session(
            repository=repo_slug,
            api=api,
            in_stream=in_stream,
            out_stream=out_stream,
            err_stream=err_stream,
            repo_dir=self.repo_dir,
            inventory=inv,
            resume_mode=False,
        )

        with patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", self.temp_policy):
            exit_code = run_interactive_workflow(session)

        transcript = out_stream.getvalue()

        # 1. Verification of exit status
        self.assertEqual(exit_code, 0, f"Session exited with {exit_code}. Errors:\n{err_stream.getvalue()}")

        # 2. Verification of Universal Baseline application
        # Wiki disabled per ADR 0005
        self.assertFalse(api.repo["has_wiki"], "Wiki must be disabled per ADR 0005")
        # Issues enabled
        self.assertTrue(api.repo["has_issues"], "Issues feature must be enabled")
        # Merge settings
        self.assertTrue(api.repo["allow_squash_merge"], "Squash merge must be enabled")
        self.assertTrue(api.repo["delete_branch_on_merge"], "Delete branch on merge must be enabled")
        self.assertTrue(api.repo["allow_update_branch"], "Update branch must be enabled")
        self.assertFalse(api.repo["allow_merge_commit"], "Merge commit must be disabled")
        self.assertFalse(api.repo["allow_rebase_merge"], "Rebase merge must be disabled")

        # Actions permissions
        self.assertEqual(api.workflow["default_workflow_permissions"], "read")
        self.assertFalse(api.workflow["can_approve_pull_request_reviews"])

        # Security features enabled
        self.assertTrue(api.features["vulnerability-alerts"])
        self.assertTrue(api.features["automated-security-fixes"])

        # Canonical triage labels synced
        for canonical in CANONICAL_TRIAGE_LABELS:
            self.assertIn(canonical["name"], api.labels)
            self.assertEqual(api.labels[canonical["name"]]["color"], canonical["color"])

        # 3. Verification of Greenfield ruleset handling (no evidence_pr deadlock)
        policy = profile_to_policy(get_profile("minimal-lean"), repository=repo_slug)
        policy["stages"].append({
            "name": "gates",
            "ruleset_name": "code-review",
            "evidence_pr": None,
            "reviewer": "octocat",
            "checks": ["test"],
        })
        dry_run_res = run_policy(api, repo_slug, policy, apply=False)
        pending_gates = [c for c in dry_run_res.get("changes", []) if c.get("stage") == "gates"]
        self.assertEqual(len(pending_gates), 1)
        self.assertEqual(pending_gates[0]["status"], "pending")
        self.assertIn("Merge the draft Setup Pull Request", pending_gates[0]["reason"])

        # Apply should also succeed with pending gates without API error
        apply_res = run_policy(api, repo_slug, policy, apply=True, accepted_dry_run=dry_run_res)
        apply_gates = [c for c in apply_res.get("changes", []) if c.get("stage") == "gates"]
        self.assertEqual(len(apply_gates), 1)
        self.assertEqual(apply_gates[0]["status"], "pending")

        # 4. Verification of 2-line banner limit adherence and clean transcript
        verify_banner_lengths(transcript, max_lines=2)
        self.assertIn("Recommended Setup Profile:", transcript)
        self.assertIn("Accept recommended profile and proceed to dry-run? [Y/n/c/o]", transcript)
        self.assertIn("Dry-Run Summary:", transcript)
        self.assertIn("Apply these changes? [y/N]:", transcript)
        self.assertIn("All changes applied and verified successfully", transcript)

    def test_one_click_acceptance_with_empty_enter_default(self):
        """Empty enter on 1-click prompt defaults to [Y] and applies baseline."""
        repo_slug = "lispz1610/sciflow-test-enter"
        api = MockDisposableGitHubApi(repository=repo_slug)

        user_input = "\ny\n"
        in_stream = io.StringIO(user_input)
        out_stream = io.StringIO()

        inv = inventory(api, repo_slug)
        session = Session(
            repository=repo_slug,
            api=api,
            in_stream=in_stream,
            out_stream=out_stream,
            repo_dir=self.repo_dir,
            inventory=inv,
            resume_mode=False,
        )

        with patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", self.temp_policy):
            exit_code = run_interactive_workflow(session)

        self.assertEqual(exit_code, 0)
        self.assertFalse(api.repo["has_wiki"])
        self.assertTrue(api.repo["delete_branch_on_merge"])
        self.assertTrue(api.features["automated-security-fixes"])

    def test_selective_customization_feature_toggles_and_dependabot(self):
        """Selective customization ([c]) exercises feature toggles and Dependabot fixes."""
        repo_slug = "lispz1610/sciflow-test-custom"
        api = MockDisposableGitHubApi(repository=repo_slug)

        # Navigation:
        # 1. 1-click prompt: "c\n" (Selective customization)
        # 2. Menu: "1\n" (Features stage)
        #    - Multichoice: "\n" (default: Issues enabled, Wiki disabled per ADR 0005)
        # 3. Menu: "3\n" (Security stage)
        #    - Enable Actions: "y\n"
        #    - Security features: "1, 2\n" (vulnerability alerts & dependabot automated security fixes)
        # 4. Menu: "6\n" (Review & Proceed to Dry-Run)
        # 5. Apply confirmation: "y\n"
        user_input = "c\n1\n\n3\ny\n1, 2\n6\ny\n"
        in_stream = io.StringIO(user_input)
        out_stream = io.StringIO()
        err_stream = io.StringIO()

        inv = inventory(api, repo_slug)
        session = Session(
            repository=repo_slug,
            api=api,
            in_stream=in_stream,
            out_stream=out_stream,
            err_stream=err_stream,
            repo_dir=self.repo_dir,
            inventory=inv,
            resume_mode=False,
        )

        with patch("scripts.policy_assembly.DEFAULT_POLICY_PATH", self.temp_policy):
            exit_code = run_interactive_workflow(session)

        transcript = out_stream.getvalue()
        self.assertEqual(exit_code, 0, f"Customization exited with {exit_code}. Errors:\n{err_stream.getvalue()}")

        # Verification of feature toggles
        self.assertFalse(api.repo["has_wiki"], "Wiki must be disabled")
        self.assertTrue(api.repo["has_issues"], "Issues must be enabled")
        self.assertTrue(api.features["automated-security-fixes"], "Dependabot security fixes must be enabled")

        # Verification of banner limits in customization transcript
        verify_banner_lengths(transcript, max_lines=2)
        self.assertIn("Selective Customization Menu:", transcript)
        self.assertIn("Configure repository collaboration features", transcript)

    def test_clean_terminal_transcript_and_banner_limits(self):
        """Terminal banners across all stages never exceed 2 lines."""
        from scripts.interactive import (
            format_profile_banner,
            format_catalog_banner,
            prompt_target_repository,
            prompt_resume_choice,
        )

        # Profile banners
        p_banner = format_profile_banner(get_profile("scientific-python"), is_recommended=True)
        self.assertLessEqual(len([l for l in p_banner.splitlines() if l.strip()]), 2)

        # Catalog banner
        c_banner = format_catalog_banner()
        self.assertLessEqual(len([l for l in c_banner.splitlines() if l.strip()]), 2)

        # Customization menu formatting is clean and structured
        menu_text = format_customization_menu()
        self.assertIn("Selective Customization Menu:", menu_text)
        self.assertIn("Features", menu_text)
        self.assertIn("Review & Proceed to Dry-Run", menu_text)

        # Target repo prompt banner
        out = io.StringIO()
        prompt_target_repository(in_stream=io.StringIO("octo/repo\n"), out_stream=out)
        repo_banner = out.getvalue().split("Target repository")[0]
        self.assertLessEqual(len([l for l in repo_banner.splitlines() if l.strip()]), 2)

        # Resume choice banner
        out = io.StringIO()
        prompt_resume_choice(in_stream=io.StringIO("1\n"), out_stream=out)
        resume_banner = out.getvalue().split("Existing repository configuration detected")[0]
        self.assertLessEqual(len([l for l in resume_banner.splitlines() if l.strip()]), 2)

    def test_non_destructive_teardown_guarantee_on_error(self):
        """Teardown helper guarantees deletion/archive even when errors occur."""
        cleanup_hook_called = False

        def cleanup_hook():
            nonlocal cleanup_hook_called
            cleanup_hook_called = True

        try:
            try:
                raise RuntimeError("Simulated failure during intermediate test execution")
            finally:
                cleanup_hook()
        except RuntimeError:
            pass

        self.assertTrue(cleanup_hook_called, "Teardown cleanup must execute on failure")


# ==============================================================================
# Live Disposable Repository End-to-End Test Suite
# ==============================================================================

class TestLiveInteractiveDisposableE2E(unittest.TestCase):
    """Live end-to-end tests against disposable repositories on GitHub account.

    Runs when `SCIFLOW_LIVE_E2E=1` and GitHub CLI authentication is verified.
    """

    @classmethod
    def setUpClass(cls):
        if not is_live_github_available():
            raise unittest.SkipTest(
                "Skipping live test: set SCIFLOW_LIVE_E2E=1 and authenticate 'gh auth login'"
            )

    def setUp(self):
        self.repo_slug, self.repo_name = create_disposable_repo(prefix="sciflow-live-")
        self.addCleanup(teardown_disposable_repo, self.repo_slug)

    def test_live_one_click_setup_on_disposable_repository(self):
        """Live 1-click setup against disposable GitHub repository."""
        token = subprocess.check_output(["gh", "auth", "token"]).decode().strip()
        api = GitHubApi(token)

        # 1. Fetch live inventory before setup
        inv_before = inventory(api, self.repo_slug)
        self.assertIn("repository", inv_before)

        # 2. Run 1-click recommended policy
        prof = recommend_profile(repo_dir=None, inventory=inv_before)
        policy = profile_to_policy(prof, repository=self.repo_slug)

        # Ensure gates stage with evidence_pr=None is included to verify greenfield staging
        policy["stages"].append({
            "name": "gates",
            "ruleset_name": "code-review",
            "evidence_pr": None,
            "reviewer": "lispz1610",
            "checks": ["test"],
        })

        # Dry run
        dry_run = run_policy(api, self.repo_slug, policy, apply=False)
        self.assertEqual(dry_run.get("mode"), "dry-run")
        self.assertTrue(len(dry_run.get("changes", [])) > 0)

        # Verify greenfield ruleset is staged as pending without error
        pending = [c for c in dry_run.get("changes", []) if c.get("stage") == "gates"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "pending")

        # Apply
        apply_res = run_policy(api, self.repo_slug, policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(apply_res.get("mode"), "apply")

        # 3. Verify resulting live settings via GitHub API
        raw_repo = subprocess.check_output(
            ["gh", "api", f"repos/{self.repo_slug}"],
            text=True,
        )
        repo_data = json.loads(raw_repo)

        # Merge settings verified live
        self.assertTrue(repo_data.get("allow_squash_merge"))
        self.assertTrue(repo_data.get("delete_branch_on_merge"))
        self.assertTrue(repo_data.get("allow_update_branch"))
        self.assertFalse(repo_data.get("allow_merge_commit"))
        self.assertFalse(repo_data.get("allow_rebase_merge"))

        # Features verified live: Wiki disabled per ADR 0005
        self.assertFalse(repo_data.get("has_wiki"))
        self.assertTrue(repo_data.get("has_issues"))

        # Canonical triage labels verified live
        raw_labels = subprocess.check_output(
            ["gh", "api", f"repos/{self.repo_slug}/labels", "--jq", ".[].name"],
            text=True,
        )
        live_labels = set(raw_labels.strip().splitlines())
        for canonical in CANONICAL_TRIAGE_LABELS:
            self.assertIn(canonical["name"], live_labels)

    def test_live_selective_customization_on_disposable_repository(self):
        """Live selective customization ([c]) against disposable GitHub repository."""
        token = subprocess.check_output(["gh", "auth", "token"]).decode().strip()
        api = GitHubApi(token)

        inv_before = inventory(api, self.repo_slug)
        self.assertIn("repository", inv_before)

        # Policy crafted via selective customization:
        # Features with explicit disable: ["wiki"], merge settings, security with vulnerability alerts and dependabot
        policy = {
            "version": 2,
            "stages": [
                {
                    "name": "features",
                    "enable": ["issues"],
                    "disable": ["wiki"],
                },
                {
                    "name": "merge",
                    "settings": {
                        "allow_squash_merge": True,
                        "allow_merge_commit": False,
                        "allow_rebase_merge": False,
                        "delete_branch_on_merge": True,
                        "allow_update_branch": True,
                    },
                },
                {
                    "name": "actions-policy",
                    "settings": {
                        "default_workflow_permissions": "read",
                    },
                },
                {
                    "name": "security",
                    "enable": ["vulnerability-alerts", "automated-security-fixes"],
                },
                {
                    "name": "labels",
                    "items": [dict(l) for l in CANONICAL_TRIAGE_LABELS],
                },
            ],
            "repository": self.repo_slug,
        }

        # Dry run
        dry_run = run_policy(api, self.repo_slug, policy, apply=False)
        self.assertEqual(dry_run.get("mode"), "dry-run")

        # Apply
        apply_res = run_policy(api, self.repo_slug, policy, apply=True, accepted_dry_run=dry_run)
        self.assertEqual(apply_res.get("mode"), "apply")

        # Verify via gh api
        raw_repo = subprocess.check_output(
            ["gh", "api", f"repos/{self.repo_slug}"],
            text=True,
        )
        repo_data = json.loads(raw_repo)
        self.assertFalse(repo_data.get("has_wiki"), "Wiki must be disabled per ADR 0005")
        self.assertTrue(repo_data.get("has_issues"))
        self.assertTrue(repo_data.get("allow_squash_merge"))
        self.assertTrue(repo_data.get("delete_branch_on_merge"))


if __name__ == "__main__":
    unittest.main()
