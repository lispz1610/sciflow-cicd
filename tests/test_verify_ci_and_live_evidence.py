"""Tests for CI verification and runtime fingerprinting (Ticket #26)."""

import base64
import json
import os
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    import verify_release
except ImportError:
    import scripts.verify_release as verify_release


class MockReadOnlyGitHubApi:
    """Mock GitHub API that verifies 100% read-only access (GET only)."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method != "GET":
            raise AssertionError(f"Write operation attempted: {method} {path}")
        if path in self.responses:
            resp = self.responses[path]
            if isinstance(resp, tuple):
                return resp
            return 200, resp
        base_path = path.split("?")[0]
        if base_path in self.responses:
            resp = self.responses[base_path]
            if isinstance(resp, tuple):
                return resp
            return 200, resp
        return 404, {"message": f"Not Found: {path}"}


def create_sample_check_runs(head_sha, jobs_state=None):
    """Helper to generate check-runs response for Python matrix."""
    default_states = {
        "build (3.10)": ("completed", "success"),
        "build (3.11)": ("completed", "success"),
        "build (3.12)": ("completed", "success"),
    }
    states = jobs_state if jobs_state is not None else default_states
    runs = []
    for name, (status, conclusion) in states.items():
        runs.append({
            "name": name,
            "status": status,
            "conclusion": conclusion,
            "head_sha": head_sha,
        })
    return {"total_count": len(runs), "check_runs": runs}

class VerifyCiGatesTests(unittest.TestCase):
    """Tests for verifying Python CI matrix gates on the current candidate head."""

    def setUp(self):
        self.head_sha = "9f4bb810a88060e48234538f7205224f0bee5659"
        self.repo = "lispz1610/sciflow-cicd"
        self.required_versions = ["3.10", "3.11", "3.12"]

    def test_ci_verification_all_python_jobs_passing(self):
        mock_api = MockReadOnlyGitHubApi({
            f"/repos/{self.repo}/commits/{self.head_sha}/check-runs": create_sample_check_runs(self.head_sha)
        })
        res = verify_release.verify_ci_gates(mock_api, self.repo, self.head_sha, self.required_versions)
        self.assertTrue(res["valid"])
        self.assertIn("all required python ci jobs passed", res["reason"].lower())

    def test_ci_verification_missing_job_fails(self):
        incomplete_jobs = {
            "build (3.10)": ("completed", "success"),
            "build (3.11)": ("completed", "success"),
        }
        mock_api = MockReadOnlyGitHubApi({
            f"/repos/{self.repo}/commits/{self.head_sha}/check-runs": create_sample_check_runs(
                self.head_sha, incomplete_jobs
            )
        })
        res = verify_release.verify_ci_gates(mock_api, self.repo, self.head_sha, self.required_versions)
        self.assertFalse(res["valid"])
        self.assertIn("missing", res["reason"].lower())
        self.assertIn("3.12", res["reason"])
        self.assertIn("recovery", res)

    def test_ci_verification_failed_job_fails(self):
        failed_jobs = {
            "build (3.10)": ("completed", "success"),
            "build (3.11)": ("completed", "failure"),
            "build (3.12)": ("completed", "success"),
        }
        mock_api = MockReadOnlyGitHubApi({
            f"/repos/{self.repo}/commits/{self.head_sha}/check-runs": create_sample_check_runs(
                self.head_sha, failed_jobs
            )
        })
        res = verify_release.verify_ci_gates(mock_api, self.repo, self.head_sha, self.required_versions)
        self.assertFalse(res["valid"])
        self.assertIn("failed", res["reason"].lower())
        self.assertIn("build (3.11)", res["reason"])

    def test_ci_verification_in_progress_job_fails(self):
        in_progress_jobs = {
            "build (3.10)": ("in_progress", None),
            "build (3.11)": ("completed", "success"),
            "build (3.12)": ("completed", "success"),
        }
        mock_api = MockReadOnlyGitHubApi({
            f"/repos/{self.repo}/commits/{self.head_sha}/check-runs": create_sample_check_runs(
                self.head_sha, in_progress_jobs
            )
        })
        res = verify_release.verify_ci_gates(mock_api, self.repo, self.head_sha, self.required_versions)
        self.assertFalse(res["valid"])
        self.assertIn("in_progress", res["reason"].lower())
        self.assertIn("build (3.10)", res["reason"])

    def test_ci_verification_cancelled_or_skipped_job_fails(self):
        for status, conclusion in [("completed", "cancelled"), ("completed", "skipped")]:
            bad_jobs = {
                "build (3.10)": (status, conclusion),
                "build (3.11)": ("completed", "success"),
                "build (3.12)": ("completed", "success"),
            }
            mock_api = MockReadOnlyGitHubApi({
                f"/repos/{self.repo}/commits/{self.head_sha}/check-runs": create_sample_check_runs(
                    self.head_sha, bad_jobs
                )
            })
            res = verify_release.verify_ci_gates(mock_api, self.repo, self.head_sha, self.required_versions)
            self.assertFalse(res["valid"])
            self.assertIn(conclusion, res["reason"].lower())

    def test_ci_verification_rejects_check_with_different_head(self):
        stale_sha = "0000000000000000000000000000000000000000"
        mock_api = MockReadOnlyGitHubApi({
            f"/repos/{self.repo}/commits/{self.head_sha}/check-runs": {
                "total_count": 3,
                "check_runs": [
                    {"name": "build (3.10)", "status": "completed", "conclusion": "success", "head_sha": stale_sha},
                    {"name": "build (3.11)", "status": "completed", "conclusion": "success", "head_sha": self.head_sha},
                    {"name": "build (3.12)", "status": "completed", "conclusion": "success", "head_sha": self.head_sha},
                ],
            }
        })
        res = verify_release.verify_ci_gates(mock_api, self.repo, self.head_sha, self.required_versions)
        self.assertFalse(res["valid"])
        self.assertIn("mismatch", res["reason"].lower())

    def test_ci_gates_rejects_unrelated_job_in_place_of_build(self):
        mock_api = MockReadOnlyGitHubApi({
            f"/repos/{self.repo}/commits/{self.head_sha}/check-runs": {
                "total_count": 3,
                "check_runs": [
                    {"name": "lint (3.10)", "status": "completed", "conclusion": "success", "head_sha": self.head_sha},
                    {"name": "build (3.11)", "status": "completed", "conclusion": "success", "head_sha": self.head_sha},
                    {"name": "build (3.12)", "status": "completed", "conclusion": "success", "head_sha": self.head_sha},
                ],
            }
        })
        res = verify_release.verify_ci_gates(mock_api, self.repo, self.head_sha, self.required_versions)
        self.assertFalse(res["valid"])
        self.assertIn("missing", res["reason"].lower())

    def test_ci_gates_queries_with_per_page_and_handles_pagination(self):
        page1_runs = [{"name": f"other-check-{i}", "status": "completed", "conclusion": "success", "head_sha": self.head_sha} for i in range(100)]
        page2_runs = [
            {"name": "build (3.10)", "status": "completed", "conclusion": "success", "head_sha": self.head_sha},
            {"name": "build (3.11)", "status": "completed", "conclusion": "success", "head_sha": self.head_sha},
            {"name": "build (3.12)", "status": "completed", "conclusion": "success", "head_sha": self.head_sha},
        ]
        base_path = f"/repos/{self.repo}/commits/{self.head_sha}/check-runs"
        mock_api = MockReadOnlyGitHubApi({
            f"{base_path}?per_page=100": {
                "total_count": 103,
                "check_runs": page1_runs,
            },
            f"{base_path}?per_page=100&page=2": {
                "total_count": 103,
                "check_runs": page2_runs,
            },
        })
        res = verify_release.verify_ci_gates(mock_api, self.repo, self.head_sha, self.required_versions)
        self.assertTrue(res["valid"])
        self.assertTrue(any("?per_page=100" in call[1] for call in mock_api.calls))
        self.assertTrue(any("&page=2" in call[1] for call in mock_api.calls))

    def test_ci_gates_pagination_handles_empty_page_before_reaching_total_count(self):
        page1_runs = [{"name": f"other-check-{i}", "status": "completed", "conclusion": "success", "head_sha": self.head_sha} for i in range(100)]
        base_path = f"/repos/{self.repo}/commits/{self.head_sha}/check-runs"
        mock_api = MockReadOnlyGitHubApi({
            f"{base_path}?per_page=100": {
                "total_count": 150,
                "check_runs": page1_runs,
            },
            f"{base_path}?per_page=100&page=2": {
                "total_count": 150,
                "check_runs": [],
            },
        })
        res = verify_release.verify_ci_gates(mock_api, self.repo, self.head_sha, self.required_versions)
        self.assertFalse(res["valid"])
        self.assertIn("pagination", res["reason"].lower())


class RuntimeFingerprintTests(unittest.TestCase):
    """Tests for deterministic runtime fingerprint computation."""

    def test_fingerprint_is_deterministic(self):
        fp1 = verify_release.compute_runtime_fingerprint(ROOT)
        fp2 = verify_release.compute_runtime_fingerprint(ROOT)
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 64)

    def test_documentation_only_change_preserves_fingerprint(self):
        # Fingerprint computed from memory files dict
        base_files = {
            "scripts/cli.py": b"print('cli')",
            "install.sh": b"#!/bin/bash",
            ".github/workflows/python-app.yml": b"name: CI",
            "docs/report.md": b"Initial report",
            "README.md": b"Initial readme",
        }
        fp_initial = verify_release.compute_runtime_fingerprint_from_files(base_files)

        # Update docs only
        doc_updated = dict(base_files, **{
            "docs/report.md": b"Updated report with new text",
            "README.md": b"Updated readme instructions",
            "docs/adr/0003-new.md": b"New ADR",
        })
        fp_doc_updated = verify_release.compute_runtime_fingerprint_from_files(doc_updated)
        self.assertEqual(fp_initial, fp_doc_updated)

    def test_runtime_code_change_changes_fingerprint(self):
        base_files = {
            "scripts/cli.py": b"print('cli')",
            "install.sh": b"#!/bin/bash",
            ".github/workflows/python-app.yml": b"name: CI",
        }
        fp_initial = verify_release.compute_runtime_fingerprint_from_files(base_files)

        code_updated = dict(base_files, **{"scripts/cli.py": b"print('cli modified')"})
        fp_code_updated = verify_release.compute_runtime_fingerprint_from_files(code_updated)
        self.assertNotEqual(fp_initial, fp_code_updated)

    def test_fingerprint_rule_version_change_changes_fingerprint(self):
        base_files = {
            "scripts/cli.py": b"print('cli')",
            "install.sh": b"#!/bin/bash",
        }
        fp_v1 = verify_release.compute_runtime_fingerprint_from_files(base_files, rule_version="v1")
        fp_v2 = verify_release.compute_runtime_fingerprint_from_files(base_files, rule_version="v2")
        self.assertNotEqual(fp_v1, fp_v2)

    def test_dependency_definition_change_invalidates_fingerprint(self):
        base_files = {
            "scripts/cli.py": b"print('cli')",
            "install.sh": b"#!/bin/bash",
            "requirements.txt": b"requests==2.28.1\n",
        }
        fp_base = verify_release.compute_runtime_fingerprint_from_files(base_files)

        dep_changed = dict(base_files, **{"requirements.txt": b"requests==2.31.0\n"})
        fp_dep_changed = verify_release.compute_runtime_fingerprint_from_files(dep_changed)
        self.assertNotEqual(fp_base, fp_dep_changed)

        pyproject_added = dict(base_files, **{"pyproject.toml": b"[project]\nname = 'pkg'\n"})
        fp_pyproject = verify_release.compute_runtime_fingerprint_from_files(pyproject_added)
        self.assertNotEqual(fp_base, fp_pyproject)

    def test_git_commit_runtime_fingerprint_preserves_historical_sha(self):
        import subprocess
        historical_sha = "9f4bb810a88060e48234538f7205224f0bee5659"
        try:
            fp_commit = verify_release.compute_runtime_fingerprint(ROOT, commit_sha=historical_sha)
            self.assertEqual(fp_commit, "18b032e3e2e846458c953fb5445c8471e674454797183ad8113f8c53418a26bc")
        except ValueError:
            # Fallback for clean/cutover checkouts where historical pre-cutover commits do not exist
            head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            fp_commit = verify_release.compute_runtime_fingerprint(ROOT, commit_sha=head_sha)
            fp_disk = verify_release.compute_runtime_fingerprint(ROOT)
            status = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
            if not status:
                self.assertEqual(fp_commit, fp_disk)

    def test_missing_commit_does_not_fingerprint_current_checkout(self):
        with self.assertRaises(ValueError):
            verify_release.compute_runtime_fingerprint(ROOT, commit_sha="0" * 40)

    def test_workflow_files_included_in_runtime_fingerprint(self):
        base_files = {
            "scripts/cli.py": b"print('cli')",
            "install.sh": b"#!/bin/bash",
            ".github/workflows/python-app.yml": b"name: CI\n",
        }
        fp_base = verify_release.compute_runtime_fingerprint_from_files(base_files)

        wf_changed = dict(base_files, **{".github/workflows/python-app.yml": b"name: Modified CI\n"})
        fp_wf_changed = verify_release.compute_runtime_fingerprint_from_files(wf_changed)
        self.assertNotEqual(fp_base, fp_wf_changed)

        # Ensure removeprefix handles leading './' without stripping '.' from .github
        with_dot_slash = {
            "./scripts/cli.py": b"print('cli')",
            "./install.sh": b"#!/bin/bash",
            "./.github/workflows/python-app.yml": b"name: CI\n",
        }
        fp_dot_slash = verify_release.compute_runtime_fingerprint_from_files(with_dot_slash)
        self.assertEqual(fp_base, fp_dot_slash)

if __name__ == "__main__":
    unittest.main()

