"""End-to-end and contract tests for release evidence verification and CI workflow (Ticket #27)."""

import base64
import contextlib
import functools
from http.server import HTTPServer, BaseHTTPRequestHandler
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    import verify_release
except ImportError:
    import scripts.verify_release as verify_release

GH_SETUP = ROOT / "gh-setup"


class MockReadOnlyGitHubApi:
    """Simulated GitHub API with strict read-only assertion."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method != "GET":
            raise AssertionError(f"Write operation attempted during release verification: {method} {path}")
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


def make_complete_green_responses(candidate_repo, candidate_pr, candidate_head):
    """Return simulated API responses for a fully green release candidate."""
    return {
        # Candidate PR
        f"/repos/{candidate_repo}/pulls/{candidate_pr}": {
            "number": candidate_pr,
            "head": {"sha": candidate_head},
            "base": {"repo": {"full_name": candidate_repo}},
            "state": "open",
        },
        # Candidate CI matrix
        f"/repos/{candidate_repo}/commits/{candidate_head}/check-runs": {
            "total_count": 3,
            "check_runs": [
                {"name": "build (3.10)", "status": "completed", "conclusion": "success", "head_sha": candidate_head},
                {"name": "build (3.11)", "status": "completed", "conclusion": "success", "head_sha": candidate_head},
                {"name": "build (3.12)", "status": "completed", "conclusion": "success", "head_sha": candidate_head},
            ],
        },
    }


class _MockGitHubHttpHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler to serve mocked GitHub API endpoints for subprocess CLI tests."""

    def __init__(self, responses, *args, **kwargs):
        self.responses = responses
        super().__init__(*args, **kwargs)

    def do_GET(self):
        base_path = self.path.split("?")[0]
        resp = self.responses.get(self.path) or self.responses.get(base_path)
        if resp is not None:
            if isinstance(resp, tuple):
                status_code, body = resp
            else:
                status_code, body = 200, resp
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            raw = json.dumps({"message": f"Not Found: {self.path}"}).encode("utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    def log_message(self, format, *args):
        pass  # Suppress HTTP log messages


@contextlib.contextmanager
def run_mock_github_server(responses):
    """Run an in-process HTTP server simulating GitHub API on a free local port."""
    handler_class = functools.partial(_MockGitHubHttpHandler, responses)
    server = HTTPServer(("127.0.0.1", 0), handler_class)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


class VerifyReleaseEndToEndTests(unittest.TestCase):
    """End-to-end assertions testing complete evidence chains and failure modes."""

    def setUp(self):
        self.candidate_repo = "lispz1610/github-repo-setup"
        self.candidate_pr = 23
        try:
            self.candidate_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip()
        except Exception:
            self.candidate_head = "9f4bb810a88060e48234538f7205224f0bee5659"

    def test_ci_verifies_candidate_from_record_committed_outside_candidate_head(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = pathlib.Path(tmp_dir)

            def git(*args):
                return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

            git("init", "-q")
            git("config", "user.name", "Release test")
            git("config", "user.email", "release-test@example.invalid")
            (root / "gh-setup").write_text("#!/usr/bin/env python3\n")
            git("add", "gh-setup")
            git("commit", "-qm", "candidate")
            candidate_head = git("rev-parse", "HEAD")
            candidate_branch = git("branch", "--show-current")
            fingerprint = verify_release.compute_runtime_fingerprint(root, commit_sha=candidate_head)

            git("switch", "-qc", "release-control")
            docs = root / "docs"
            docs.mkdir()
            record = {
                "version": 1,
                "status": "active",
                "candidate": {
                    "repository": self.candidate_repo,
                    "pull_request": self.candidate_pr,
                    "target_version": "0.1.0",
                    "head_sha": candidate_head,
                },
                "required_gates": {"python_versions": ["3.10", "3.11", "3.12"]},
                "live_evidence": {
                    "runtime_fingerprint": fingerprint,
                },
                "operational_constraints": ["Private Repository Constraint for Advisory Review"],
            }
            (docs / "release-control.json").write_text(json.dumps(record))
            (docs / "release-control.md").write_text(
                f"**Pull Request:** [#{self.candidate_pr}](https://github.com/{self.candidate_repo}/pull/{self.candidate_pr})\n"
                f"**Candidate Head SHA:** `{candidate_head}`\n"
                "**Current Release Verdict:** **READY WITH DECLARED OPERATIONAL CONSTRAINTS**\n"
            )
            git("add", "docs")
            git("commit", "-qm", "publish release control")
            control_ref = git("rev-parse", "HEAD")
            git("switch", "-q", candidate_branch)
            self.assertFalse((docs / "release-control.json").exists())

            responses = make_complete_green_responses(
                self.candidate_repo, self.candidate_pr, candidate_head
            )
            api = MockReadOnlyGitHubApi(responses)
            output = __import__("io").StringIO()
            with __import__("unittest.mock", fromlist=["patch"]).patch.dict(
                os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}
            ):
                exit_code = verify_release.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    record_ref=control_ref,
                    repo_root=root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=api,
                    output_format="json",
                    out_stream=output,
                )
            self.assertEqual(exit_code, 0, output.getvalue())
            result = json.loads(output.getvalue())
            self.assertEqual(result["candidate"]["head_sha"], candidate_head)
            self.assertEqual(result["evidence"]["inferred"], [])
            self.assertTrue(api.calls)
            for method, path, _ in api.calls:
                self.assertEqual(method, "GET")
                self.assertTrue(path.startswith(f"/repos/{self.candidate_repo}/"))

    def test_complete_green_candidate_passes_with_constraints(self):
        responses = make_complete_green_responses(
            self.candidate_repo, self.candidate_pr, self.candidate_head
        )
        mock_api = MockReadOnlyGitHubApi(responses)

        # Build mock repo directory to control runtime fingerprint
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = pathlib.Path(tmp_dir)
            (tmp_path / "gh-setup").write_text("#!/usr/bin/env python3")
            (tmp_path / "install.sh").write_text("#!/bin/bash")
            (tmp_path / "scripts").mkdir()
            (tmp_path / "scripts" / "cli.py").write_text("print('cli')")

            expected_fp = verify_release.compute_runtime_fingerprint(ROOT, commit_sha=self.candidate_head)

            record = {
                "version": 1,
                "candidate": {
                    "repository": self.candidate_repo,
                    "pull_request": self.candidate_pr,
                    "target_version": "0.1.0",
                    "head_sha": self.candidate_head,
                },
                "required_gates": {"python_versions": ["3.10", "3.11", "3.12"]},
                "live_evidence": {
                    "runtime_fingerprint": expected_fp,
                },
                "operational_constraints": ["Private Repository Constraint for Advisory Review"],
                "unverified_scenarios": ["Enterprise ruleset enforcement"],
            }

            rec_file = tmp_path / "record.json"
            rec_file.write_text(json.dumps(record))

            report_content = f"""# Release Audit Report
**Pull Request:** [#{self.candidate_pr}](https://github.com/{self.candidate_repo}/pull/{self.candidate_pr})
**Candidate Head SHA:** `{self.candidate_head}`
**Current Release Verdict:** **READY WITH DECLARED OPERATIONAL CONSTRAINTS**
"""
            rep_file = tmp_path / "report.md"
            rep_file.write_text(report_content)

            out = io = __import__("io").StringIO()
            exit_code = verify_release.run_verify_release(
                record_path=str(rec_file),
                report_path=str(rep_file),
                api=mock_api,
                repo_root=ROOT,
                output_format="json",
                out_stream=out,
            )

            self.assertEqual(exit_code, 0)
            res = json.loads(out.getvalue())
            self.assertEqual(res["verdict"], "ready with declared operational constraints")
            self.assertTrue(len(res["evidence"]["observed"]) >= 3)
            self.assertEqual(res["evidence"]["unverified"], ["Enterprise ruleset enforcement"])

            # Verify 0 writes were attempted and all queries target candidate repo
            self.assertTrue(len(mock_api.calls) > 0)
            for m, path, _ in mock_api.calls:
                self.assertEqual(m, "GET")
                self.assertTrue(path.startswith(f"/repos/{self.candidate_repo}/"))

    def test_ci_job_failure_produces_not_ready_and_recovery(self):
        responses = make_complete_green_responses(
            self.candidate_repo, self.candidate_pr, self.candidate_head
        )
        # Fail Python 3.12 check run
        cr_list = responses[f"/repos/{self.candidate_repo}/commits/{self.candidate_head}/check-runs"]["check_runs"]
        for cr in cr_list:
            if cr["name"] == "build (3.12)":
                cr["conclusion"] = "failure"

        mock_api = MockReadOnlyGitHubApi(responses)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = pathlib.Path(tmp_dir)
            (tmp_path / "gh-setup").write_text("#!/usr/bin/env python3")
            expected_fp = verify_release.compute_runtime_fingerprint(tmp_path)

            record = {
                "version": 1,
                "candidate": {
                    "repository": self.candidate_repo,
                    "pull_request": self.candidate_pr,
                    "target_version": "0.1.0",
                    "head_sha": self.candidate_head,
                },
                "required_gates": {"python_versions": ["3.10", "3.11", "3.12"]},
                "live_evidence": {
                    "runtime_fingerprint": expected_fp,
                },
            }
            rec_file = tmp_path / "record.json"
            rec_file.write_text(json.dumps(record))

            out = __import__("io").StringIO()
            exit_code = verify_release.run_verify_release(
                record_path=str(rec_file),
                api=mock_api,
                repo_root=ROOT,
                output_format="json",
                out_stream=out,
            )
            self.assertEqual(exit_code, 1)
            res = json.loads(out.getvalue())
            self.assertEqual(res["verdict"], "not ready")
            self.assertIn("failed", res["reason"].lower())
            self.assertIn("build (3.12)", res["reason"])
            self.assertTrue(len(res["recovery"]) > 0)

    def test_fingerprint_mismatch_fails_closed(self):
        responses = make_complete_green_responses(
            self.candidate_repo, self.candidate_pr, self.candidate_head
        )
        mock_api = MockReadOnlyGitHubApi(responses)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = pathlib.Path(tmp_dir)
            (tmp_path / "gh-setup").write_text("#!/usr/bin/env python3")

            record = {
                "version": 1,
                "candidate": {
                    "repository": self.candidate_repo,
                    "pull_request": self.candidate_pr,
                    "target_version": "0.1.0",
                    "head_sha": self.candidate_head,
                },
                "required_gates": {"python_versions": ["3.10", "3.11", "3.12"]},
                "live_evidence": {
                    "runtime_fingerprint": "0" * 64,  # Intentionally mismatched fingerprint
                },
            }
            rec_file = tmp_path / "record.json"
            rec_file.write_text(json.dumps(record))

            out = __import__("io").StringIO()
            exit_code = verify_release.run_verify_release(
                record_path=str(rec_file),
                api=mock_api,
                repo_root=ROOT,
                output_format="json",
                out_stream=out,
            )
            self.assertEqual(exit_code, 1)
            res = json.loads(out.getvalue())
            self.assertEqual(res["verdict"], "not ready")
            self.assertIn("fingerprint mismatch", res["reason"].lower())

    def test_legacy_record_with_historical_fields_verifies_cleanly_in_e2e(self):
        responses = make_complete_green_responses(
            self.candidate_repo, self.candidate_pr, self.candidate_head
        )
        mock_api = MockReadOnlyGitHubApi(responses)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = pathlib.Path(tmp_dir)
            expected_fp = verify_release.compute_runtime_fingerprint(ROOT, commit_sha=self.candidate_head)

            legacy_record = {
                "version": 1,
                "candidate": {
                    "repository": self.candidate_repo,
                    "pull_request": self.candidate_pr,
                    "target_version": "0.1.0",
                    "head_sha": self.candidate_head,
                },
                "required_gates": {"python_versions": ["3.10", "3.11", "3.12"]},
                "live_evidence": {
                    "runtime_fingerprint": expected_fp,
                    "target_repository": "legacy-org/legacy-repo",
                    "setup_pr": {
                        "number": 1,
                        "merge_commit": "1" * 40,
                        "workflow_path": ".github/workflows/review.yml",
                        "workflow_sha": "2" * 40,
                        "action_sha": "3" * 40,
                    },
                    "review_run": {
                        "run_id": 12345,
                        "validation_pr": 2,
                        "finding_id": "r100",
                    },
                },
            }
            rec_file = tmp_path / "record.json"
            rec_file.write_text(json.dumps(legacy_record))

            out = __import__("io").StringIO()
            exit_code = verify_release.run_verify_release(
                record_path=str(rec_file),
                api=mock_api,
                repo_root=ROOT,
                output_format="json",
                out_stream=out,
            )
            self.assertEqual(exit_code, 0, out.getvalue())
            res = json.loads(out.getvalue())
            self.assertEqual(res["verdict"], "ready")
            self.assertTrue(len(mock_api.calls) > 0)
            for _, path, _ in mock_api.calls:
                self.assertTrue(path.startswith(f"/repos/{self.candidate_repo}/"))

    def test_ci_workflow_has_verify_release_job_with_correct_configuration(self):
        wf_path = ROOT / ".github" / "workflows" / "python-app.yml"
        self.assertTrue(wf_path.is_file(), "python-app.yml must exist")
        content = wf_path.read_text(encoding="utf-8")

        self.assertIn("verify-release:", content)
        self.assertIn("needs: build", content)
        self.assertIn("contents: read", content)
        self.assertIn("checks: read", content)
        self.assertIn("pull-requests: read", content)
        self.assertIn("scripts/verify_release.py", content)
        self.assertIn("--ci", content)


class VerifyReleaseSignalAndBypassE2ETests(unittest.TestCase):
    """End-to-end and CLI tests for candidate signal and unmarked PR bypass (Ticket #49)."""

    def test_unit_unlabeled_pr_in_ci_mode_outputs_bypass_notice_and_exits_zero(self):
        """Calling run_verify_release with ci_mode=True on an unlabeled PR outputs bypass notice and exits 0."""
        test_labels = [None, [], "", ["bug", "enhancement"], "documentation,chore"]
        for lbls in test_labels:
            with self.subTest(labels=lbls):
                out = io.StringIO()
                err = io.StringIO()
                with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/42/merge"}):
                    # Using a nonexistent record path guarantees that if bypass did not occur,
                    # an error would be raised / return code 1 returned.
                    exit_code = verify_release.run_verify_release(
                        record_path="nonexistent-release-record.json",
                        record_ref="b" * 40,
                        ci_mode=True,
                        labels=lbls,
                        out_stream=out,
                        err_stream=err,
                    )
                self.assertEqual(exit_code, 0)
                output_str = out.getvalue()
                self.assertIn("Pull request is not marked as a release candidate; skipping release gate", output_str)
                self.assertEqual(err.getvalue(), "")

    def test_cli_invokes_verify_release_ci_unlabeled_exits_zero_with_log_output(self):
        """CLI test invokes verify_release --ci --labels 'bug,enhancement' and verifies exit 0 and log output."""
        env = os.environ.copy()
        env["GITHUB_REF"] = "refs/pull/101/merge"

        # Direct verify_release script invocation
        proc_script = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "verify_release.py"),
                "--ci",
                "--labels",
                "bug,enhancement",
                "--record",
                "nonexistent-record.json",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc_script.returncode, 0)
        self.assertIn("Pull request is not marked as a release candidate; skipping release gate", proc_script.stdout)

        # Invocation via gh-setup wrapper
        proc_wrapper = subprocess.run(
            [
                str(GH_SETUP),
                "verify-release",
                "--ci",
                "--labels",
                "bug,enhancement",
                "--record",
                "nonexistent-record.json",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc_wrapper.returncode, 0)
        self.assertIn("Pull request is not marked as a release candidate; skipping release gate", proc_wrapper.stdout)

    def test_non_pr_events_in_ci_mode_exit_zero(self):
        """Test verifies that non-PR events (GITHUB_REF=refs/heads/main) exit 0."""
        # Unit test verification
        out = io.StringIO()
        err = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_REF": "refs/heads/main"}):
            exit_code = verify_release.run_verify_release(
                record_path="nonexistent-record.json",
                record_ref="c" * 40,
                ci_mode=True,
                labels=["release-candidate"],
                out_stream=out,
                err_stream=err,
            )
        self.assertEqual(exit_code, 0)
        output_str = out.getvalue()
        self.assertIn("CI ref 'refs/heads/main' is not a pull request event; skipping release gate.", output_str)
        self.assertEqual(err.getvalue(), "")

        # CLI test verification
        env = os.environ.copy()
        env["GITHUB_REF"] = "refs/heads/main"
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "verify_release.py"),
                "--ci",
                "--labels",
                "release-candidate",
                "--record",
                "nonexistent-record.json",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("CI ref 'refs/heads/main' is not a pull request event; skipping release gate.", proc.stdout)

    def test_candidate_signal_proceeds_past_initial_bypass_logic(self):
        """Test verifies that passing labels=['release-candidate'] proceeds past initial bypass logic."""
        test_labels = [
            ["release-candidate"],
            ["bug", "release-candidate"],
            "release-candidate",
            "bug,release-candidate",
            "Release-Candidate",
            "  RELEASE-CANDIDATE  ",
        ]
        for lbls in test_labels:
            with self.subTest(labels=lbls):
                out = io.StringIO()
                err = io.StringIO()
                with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/42/merge"}):
                    # Because record_path does not exist, proceeding past bypass logic will
                    # attempt to load the record and exit 1 (RecordValidationError).
                    exit_code = verify_release.run_verify_release(
                        record_path="nonexistent-record.json",
                        ci_mode=True,
                        labels=lbls,
                        out_stream=out,
                        err_stream=err,
                    )
                self.assertEqual(exit_code, 1)
                combined_output = out.getvalue() + err.getvalue()
                self.assertNotIn("Pull request is not marked as a release candidate", combined_output)
                self.assertNotIn("skipping release gate", combined_output)
                self.assertIn("Control Record file not found", combined_output)

        # CLI test verification with release-candidate signal
        env = os.environ.copy()
        env["GITHUB_REF"] = "refs/pull/42/merge"
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "verify_release.py"),
                "--ci",
                "--labels",
                "release-candidate",
                "--record",
                "nonexistent-record.json",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 1)
        combined_cli = proc.stdout + proc.stderr
        self.assertNotIn("Pull request is not marked as a release candidate", combined_cli)
        self.assertNotIn("skipping release gate", combined_cli)
        self.assertIn("Control Record file not found", combined_cli)


class CandidateFailureModeAndRecoveryE2ETests(unittest.TestCase):
    """End-to-end and CLI tests for candidate failure mode and recovery diagnostic validation (Ticket #51)."""

    def setUp(self):
        self.candidate_repo = "lispz1610/github-repo-setup"
        self.candidate_pr = 23
        try:
            self.candidate_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip()
        except Exception:
            self.candidate_head = "9f4bb810a88060e48234538f7205224f0bee5659"

    def _sample_valid_record(
        self,
        pr=23,
        head_sha="9f4bb810a88060e48234538f7205224f0bee5659",
        status="active",
        fp="18b032e3e2e846458c953fb5445c8471e674454797183ad8113f8c53418a26bc",
    ):
        return {
            "version": 1,
            "status": status,
            "candidate": {
                "repository": self.candidate_repo,
                "pull_request": pr,
                "target_version": "0.1.0",
                "head_sha": head_sha,
            },
            "required_gates": {
                "python_versions": ["3.10", "3.11", "3.12"],
            },
            "live_evidence": {
                "runtime_fingerprint": fp,
            },
        }

    def test_marked_candidate_without_base_record_exits_one_and_emits_recovery_e2e(self):
        """Marked candidate without base record exits 1 and emits actionable recovery instructions in e2e."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = pathlib.Path(tmp_dir)

            def git(*cmd):
                return subprocess.check_output(
                    ["git", *cmd], cwd=tmp_root, text=True, stderr=subprocess.DEVNULL
                ).strip()

            git("init", "-b", "main")
            git("config", "user.name", "Test Committer")
            git("config", "user.email", "committer@example.com")
            (tmp_root / "gh-setup").write_text("#!/usr/bin/env python3\n")
            git("add", "gh-setup")
            git("commit", "-m", "init base commit")
            base_sha = git("rev-parse", "HEAD")

            # Create candidate branch without docs/release-control.json on main
            git("checkout", "-b", "candidate-branch")
            (tmp_root / "scripts").mkdir()
            (tmp_root / "scripts" / "foo.py").write_text("print('foo')\n")
            git("add", "scripts/foo.py")
            git("commit", "-m", "candidate commit")

            # E2E Text format assertion
            out_text = io.StringIO()
            err_text = io.StringIO()
            with patch.dict(os.environ, {
                "GITHUB_REF": "refs/pull/42/merge",
                "GITHUB_BASE_REF": "main",
            }):
                exit_code_text = verify_release.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    record_ref=base_sha,
                    ci_mode=True,
                    labels=["release-candidate"],
                    repo_root=tmp_root,
                    out_stream=out_text,
                    err_stream=err_text,
                )
            self.assertEqual(exit_code_text, 1)
            combined_text = out_text.getvalue() + err_text.getvalue()
            self.assertIn("Publish the trusted release control record on the base branch:", combined_text)
            self.assertIn("git checkout main", combined_text)
            self.assertIn("git pull origin main", combined_text)
            self.assertIn("git add docs/release-control.json docs/release-control.md", combined_text)
            self.assertIn('git commit -m "docs(release): publish release control for candidate PR #42"', combined_text)
            self.assertIn("git push origin main", combined_text)
            self.assertIn("Verdict: NOT READY", combined_text)

            # E2E JSON format assertion
            out_json = io.StringIO()
            err_json = io.StringIO()
            with patch.dict(os.environ, {
                "GITHUB_REF": "refs/pull/42/merge",
                "GITHUB_BASE_REF": "main",
            }):
                exit_code_json = verify_release.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    record_ref=base_sha,
                    ci_mode=True,
                    labels=["release-candidate"],
                    repo_root=tmp_root,
                    output_format="json",
                    out_stream=out_json,
                    err_stream=err_json,
                )
            self.assertEqual(exit_code_json, 1)
            payload = json.loads(out_json.getvalue())
            self.assertEqual(payload["verdict"], "not ready")
            self.assertIn("Release control file docs/release-control.json is unavailable", payload["reason"])
            recovery = payload["recovery"]
            self.assertIn("git checkout main", recovery)
            self.assertIn("git pull origin main", recovery)
            self.assertIn("git add docs/release-control.json docs/release-control.md", recovery)
            self.assertIn('git commit -m "docs(release): publish release control for candidate PR #42"', recovery)
            self.assertIn("git push origin main", recovery)

    def test_cli_marked_candidate_without_base_record_exits_one_and_emits_recovery(self):
        """CLI invocation for marked candidate without base record exits 1 and emits actionable recovery instructions."""
        env = os.environ.copy()
        env["GITHUB_REF"] = "refs/pull/42/merge"
        env["GITHUB_BASE_REF"] = "main"

        # Direct verify_release script invocation
        proc_script = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "verify_release.py"),
                "--ci",
                "--labels",
                "release-candidate",
                "--record-ref",
                "1111111111111111111111111111111111111111",
                "--record",
                "docs/release-control.json",
                "--report",
                "docs/release-control.md",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc_script.returncode, 1)
        combined_script = proc_script.stdout + proc_script.stderr
        self.assertIn("git checkout main", combined_script)
        self.assertIn("git add docs/release-control.json docs/release-control.md", combined_script)
        self.assertIn('git commit -m "docs(release): publish release control for candidate PR #42"', combined_script)
        self.assertIn("git push origin main", combined_script)

        # gh-setup wrapper invocation
        proc_wrapper = subprocess.run(
            [
                str(GH_SETUP),
                "verify-release",
                "--ci",
                "--labels",
                "release-candidate",
                "--record-ref",
                "1111111111111111111111111111111111111111",
                "--record",
                "docs/release-control.json",
                "--report",
                "docs/release-control.md",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc_wrapper.returncode, 1)
        combined_wrapper = proc_wrapper.stdout + proc_wrapper.stderr
        self.assertIn("git checkout main", combined_wrapper)
        self.assertIn("git add docs/release-control.json docs/release-control.md", combined_wrapper)
        self.assertIn("git push origin main", combined_wrapper)

        # JSON format CLI invocation
        proc_json = subprocess.run(
            [
                str(GH_SETUP),
                "verify-release",
                "--ci",
                "--labels",
                "release-candidate",
                "--record-ref",
                "1111111111111111111111111111111111111111",
                "--record",
                "docs/release-control.json",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc_json.returncode, 1)
        payload = json.loads(proc_json.stdout)
        self.assertEqual(payload["verdict"], "not ready")
        self.assertIn("git checkout main", payload["recovery"])
        self.assertIn("git push origin main", payload["recovery"])

    def test_marked_candidate_with_pr_number_mismatch_exits_one_displaying_expected_vs_observed_pr_e2e(self):
        """Marked candidate with PR number mismatch exits 1 displaying expected vs observed PR."""
        rec = self._sample_valid_record(pr=23)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(rec, f)
            rec_path = f.name
        try:
            # Text format
            out_text = io.StringIO()
            err_text = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/77/merge"}):
                exit_code_text = verify_release.run_verify_release(
                    record_path=rec_path,
                    ci_mode=True,
                    labels=["release-candidate"],
                    out_stream=out_text,
                    err_stream=err_text,
                )
            self.assertEqual(exit_code_text, 1)
            combined_text = out_text.getvalue() + err_text.getvalue()
            self.assertIn("Candidate PR mismatch: expected PR #23, observed PR #77", combined_text)
            self.assertIn("expected PR #23", combined_text)
            self.assertIn("observed PR #77", combined_text)
            self.assertNotIn("skipping release gate", combined_text)
            self.assertIn("Verdict: NOT READY", combined_text)

            # JSON format
            out_json = io.StringIO()
            err_json = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/77/merge"}):
                exit_code_json = verify_release.run_verify_release(
                    record_path=rec_path,
                    ci_mode=True,
                    labels=["release-candidate"],
                    output_format="json",
                    out_stream=out_json,
                    err_stream=err_json,
                )
            self.assertEqual(exit_code_json, 1)
            payload = json.loads(out_json.getvalue())
            self.assertEqual(payload["verdict"], "not ready")
            self.assertIn("expected PR #23", payload["reason"])
            self.assertIn("observed PR #77", payload["reason"])
        finally:
            if os.path.exists(rec_path):
                os.remove(rec_path)

    def test_cli_marked_candidate_with_pr_number_mismatch_exits_one_displaying_expected_vs_observed_pr(self):
        """CLI invocation for marked candidate with PR number mismatch exits 1 and displays expected vs observed PR."""
        rec = self._sample_valid_record(pr=23)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(rec, f)
            rec_path = f.name
        try:
            env = os.environ.copy()
            env["GITHUB_REF"] = "refs/pull/77/merge"

            # Direct script CLI invocation
            proc_script = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "verify_release.py"),
                    "--ci",
                    "--labels",
                    "release-candidate",
                    "--record",
                    rec_path,
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc_script.returncode, 1)
            combined_script = proc_script.stdout + proc_script.stderr
            self.assertIn("expected PR #23", combined_script)
            self.assertIn("observed PR #77", combined_script)
            self.assertNotIn("skipping release gate", combined_script)

            # gh-setup wrapper invocation with json format
            proc_json = subprocess.run(
                [
                    str(GH_SETUP),
                    "verify-release",
                    "--ci",
                    "--labels",
                    "release-candidate",
                    "--record",
                    rec_path,
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc_json.returncode, 1)
            payload = json.loads(proc_json.stdout)
            self.assertEqual(payload["verdict"], "not ready")
            self.assertIn("expected PR #23", payload["reason"])
            self.assertIn("observed PR #77", payload["reason"])
        finally:
            if os.path.exists(rec_path):
                os.remove(rec_path)

    def test_marked_candidate_with_stale_head_commit_sha_exits_one_displaying_expected_vs_observed_sha_e2e(self):
        """Marked candidate with stale head commit SHA exits 1 displaying expected vs observed commit SHA."""
        expected_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        observed_sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        rec = self._sample_valid_record(pr=23, head_sha=expected_sha)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(rec, f)
            rec_path = f.name
        try:
            # 1. Local checkout mismatch via GITHUB_SHA / GITHUB_HEAD_SHA
            out_text = io.StringIO()
            err_text = io.StringIO()
            with patch.dict(os.environ, {
                "GITHUB_REF": "refs/pull/23/merge",
                "GITHUB_SHA": observed_sha,
                "GITHUB_HEAD_SHA": observed_sha,
            }):
                exit_code_text = verify_release.run_verify_release(
                    record_path=rec_path,
                    ci_mode=True,
                    labels=["release-candidate"],
                    out_stream=out_text,
                    err_stream=err_text,
                )
            self.assertEqual(exit_code_text, 1)
            combined_text = out_text.getvalue() + err_text.getvalue()
            self.assertIn(f"expected commit SHA {expected_sha}", combined_text)
            self.assertIn(f"observed commit SHA {observed_sha}", combined_text)
            self.assertIn("Verdict: NOT READY", combined_text)

            # JSON format
            out_json = io.StringIO()
            err_json = io.StringIO()
            with patch.dict(os.environ, {
                "GITHUB_REF": "refs/pull/23/merge",
                "GITHUB_SHA": observed_sha,
                "GITHUB_HEAD_SHA": observed_sha,
            }):
                exit_code_json = verify_release.run_verify_release(
                    record_path=rec_path,
                    ci_mode=True,
                    labels=["release-candidate"],
                    output_format="json",
                    out_stream=out_json,
                    err_stream=err_json,
                )
            self.assertEqual(exit_code_json, 1)
            payload = json.loads(out_json.getvalue())
            self.assertEqual(payload["verdict"], "not ready")
            self.assertIn(f"expected commit SHA {expected_sha}", payload["reason"])
            self.assertIn(f"observed commit SHA {observed_sha}", payload["reason"])

            # 2. Candidate head SHA mismatch via GitHub API verify_candidate_identity
            api_observed_sha = "cccccccccccccccccccccccccccccccccccccccc"
            responses = {
                f"/repos/{self.candidate_repo}/pulls/23": {
                    "number": 23,
                    "head": {"sha": api_observed_sha},
                    "base": {"repo": {"full_name": self.candidate_repo}},
                    "state": "open",
                },
            }
            mock_api = MockReadOnlyGitHubApi(responses)
            out_api = io.StringIO()
            err_api = io.StringIO()
            with tempfile.TemporaryDirectory() as empty_git_dir:
                empty_root = pathlib.Path(empty_git_dir)
                subprocess.check_output(
                    ["git", "init", "-b", "main"], cwd=empty_root, text=True, stderr=subprocess.DEVNULL
                )
                with patch.dict(os.environ, {
                    "GITHUB_REF": "refs/pull/23/merge",
                    "GITHUB_SHA": expected_sha,
                }):
                    exit_code_api = verify_release.run_verify_release(
                        record_path=rec_path,
                        api=mock_api,
                        repo_root=empty_root,
                        ci_mode=True,
                        labels=["release-candidate"],
                        out_stream=out_api,
                        err_stream=err_api,
                    )
            self.assertEqual(exit_code_api, 1)
            combined_api = out_api.getvalue() + err_api.getvalue()
            self.assertIn(expected_sha, combined_api)
            self.assertIn(api_observed_sha, combined_api)
            self.assertIn("Verdict: NOT READY", combined_api)
        finally:
            if os.path.exists(rec_path):
                os.remove(rec_path)

    def test_cli_marked_candidate_with_stale_head_commit_sha_exits_one_displaying_expected_vs_observed_sha(self):
        """CLI invocation for marked candidate with stale head commit SHA exits 1 displaying expected vs observed commit SHA."""
        expected_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        observed_sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        rec = self._sample_valid_record(pr=23, head_sha=expected_sha)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(rec, f)
            rec_path = f.name
        try:
            env = os.environ.copy()
            env["GITHUB_REF"] = "refs/pull/23/merge"
            env["GITHUB_SHA"] = observed_sha
            env["GITHUB_HEAD_SHA"] = observed_sha

            # Direct script CLI invocation
            proc_script = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "verify_release.py"),
                    "--ci",
                    "--labels",
                    "release-candidate",
                    "--record",
                    rec_path,
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc_script.returncode, 1)
            combined_script = proc_script.stdout + proc_script.stderr
            self.assertIn(f"expected commit SHA {expected_sha}", combined_script)
            self.assertIn(f"observed commit SHA {observed_sha}", combined_script)

            # gh-setup wrapper invocation with JSON format
            proc_json = subprocess.run(
                [
                    str(GH_SETUP),
                    "verify-release",
                    "--ci",
                    "--labels",
                    "release-candidate",
                    "--record",
                    rec_path,
                    "--format",
                    "json",
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc_json.returncode, 1)
            payload = json.loads(proc_json.stdout)
            self.assertEqual(payload["verdict"], "not ready")
            self.assertIn(f"expected commit SHA {expected_sha}", payload["reason"])
            self.assertIn(f"observed commit SHA {observed_sha}", payload["reason"])
        finally:
            if os.path.exists(rec_path):
                os.remove(rec_path)

    def test_all_error_outputs_have_tokens_and_secrets_sanitized_e2e(self):
        """Test verifies that all error outputs have tokens and secrets sanitized across error modes in e2e."""
        secret_tokens = [
            "ghp_0123456789abcdefghijklmnopqrstuv",
            "gho_0123456789abcdefghijklmnopqrstuv",
            "github_pat_11AAAAAAA0123456789_abcdefghijklmnopqrstuvwxyz",
            "Bearer secret_token_value_abcdef123456",
            "sk-abcdefghijklmnopqrstuvwxyz123456",
            "https://myuser:supersecretpass123@github.com/myorg/myrepo.git",
        ]

        for token in secret_tokens:
            with self.subTest(token=token):
                # Scenario 1: Secret embedded in error path / filename
                out = io.StringIO()
                err = io.StringIO()
                with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/42/merge"}):
                    exit_code = verify_release.run_verify_release(
                        record_path=f"docs/{token}.json",
                        record_ref="1111111111111111111111111111111111111111",
                        ci_mode=True,
                        labels=["release-candidate"],
                        out_stream=out,
                        err_stream=err,
                    )
                self.assertEqual(exit_code, 1)
                combined = out.getvalue() + err.getvalue()
                self.assertNotIn(token, combined)
                self.assertIn("[REDACTED]", combined)

                # Scenario 2: Secret embedded in GITHUB_BASE_REF (maintainer recovery command)
                out_ref = io.StringIO()
                err_ref = io.StringIO()
                with patch.dict(os.environ, {
                    "GITHUB_REF": "refs/pull/42/merge",
                    "GITHUB_BASE_REF": f"release-{token}",
                }):
                    exit_code_ref = verify_release.run_verify_release(
                        record_path="nonexistent-record.json",
                        record_ref="1111111111111111111111111111111111111111",
                        ci_mode=True,
                        labels=["release-candidate"],
                        out_stream=out_ref,
                        err_stream=err_ref,
                    )
                self.assertEqual(exit_code_ref, 1)
                combined_ref = out_ref.getvalue() + err_ref.getvalue()
                self.assertNotIn(token, combined_ref)
                self.assertIn("[REDACTED]", combined_ref)

                # Scenario 3: Secret embedded in JSON format error output
                out_json = io.StringIO()
                err_json = io.StringIO()
                with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/42/{token}/merge"}):
                    exit_code_json = verify_release.run_verify_release(
                        record_path=f"docs/{token}.json",
                        record_ref="1111111111111111111111111111111111111111",
                        ci_mode=True,
                        labels=["release-candidate"],
                        output_format="json",
                        out_stream=out_json,
                        err_stream=err_json,
                    )
                self.assertEqual(exit_code_json, 1)
                raw_json = out_json.getvalue() + err_json.getvalue()
                self.assertNotIn(token, raw_json)
                self.assertIn("[REDACTED]", raw_json)

    def test_cli_all_error_outputs_have_tokens_and_secrets_sanitized(self):
        """CLI invocation verifies that all error outputs have tokens and secrets sanitized."""
        secret_token = "ghp_cli_super_secret_token_1234567890"
        env = os.environ.copy()
        env["GITHUB_REF"] = f"refs/pull/42/{secret_token}/merge"

        # Direct script CLI invocation
        proc_script = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "verify_release.py"),
                "--ci",
                "--labels",
                "release-candidate",
                "--record-ref",
                "1111111111111111111111111111111111111111",
                "--record",
                f"docs/{secret_token}.json",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc_script.returncode, 1)
        combined_script = proc_script.stdout + proc_script.stderr
        self.assertNotIn(secret_token, combined_script)
        self.assertIn("[REDACTED]", combined_script)

        # gh-setup wrapper invocation with json format
        proc_wrapper = subprocess.run(
            [
                str(GH_SETUP),
                "verify-release",
                "--ci",
                "--labels",
                "release-candidate",
                "--record-ref",
                "1111111111111111111111111111111111111111",
                "--record",
                f"docs/{secret_token}.json",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc_wrapper.returncode, 1)
        combined_wrapper = proc_wrapper.stdout + proc_wrapper.stderr
        self.assertNotIn(secret_token, combined_wrapper)
        self.assertIn("[REDACTED]", combined_wrapper)


class RegisteredCandidateEndToEndValidationTests(unittest.TestCase):
    """End-to-end success and failure path validation for registered release candidates (Ticket #53)."""

    def setUp(self):
        self.candidate_repo = "lispz1610/github-repo-setup"
        self.candidate_pr = 23

    def _create_candidate_repo_fixture(
        self,
        record_status="active",
        modify_candidate_after_fingerprint=False,
        declared_fingerprint=None,
    ):
        """Create a complete Git repo with candidate branch and separate base control branch."""
        tmp_dir = tempfile.TemporaryDirectory()
        tmp_root = pathlib.Path(tmp_dir.name)

        def git(*cmd):
            return subprocess.check_output(
                ["git", *cmd], cwd=tmp_root, text=True, stderr=subprocess.DEVNULL
            ).strip()

        git("init", "-b", "main")
        git("config", "user.name", "Release Maintainer")
        git("config", "user.email", "maintainer@example.invalid")

        # Initial candidate runtime assets
        (tmp_root / "gh-setup").write_text("#!/usr/bin/env python3\nprint('gh-setup entrypoint')\n")
        (tmp_root / "install.sh").write_text("#!/bin/bash\necho install\n")
        (tmp_root / "scripts").mkdir()
        (tmp_root / "scripts" / "cli.py").write_text("print('cli module')\n")
        (tmp_root / "scripts" / "verify_release.py").write_text("# verifier stub\n")
        git("add", ".")
        git("commit", "-m", "candidate initial commit")
        candidate_head = git("rev-parse", "HEAD")
        candidate_branch = git("branch", "--show-current")

        initial_fp = verify_release.compute_runtime_fingerprint(tmp_root, commit_sha=candidate_head)

        # Base release control branch
        git("checkout", "-b", "release-control")
        docs = tmp_root / "docs"
        docs.mkdir(exist_ok=True)

        if modify_candidate_after_fingerprint:
            # Candidate code was modified, but control record declares previous fingerprint
            git("checkout", candidate_branch)
            (tmp_root / "scripts" / "new_feature.py").write_text("print('modified candidate code')\n")
            git("add", "scripts/new_feature.py")
            git("commit", "-m", "candidate modified runtime code")
            candidate_head = git("rev-parse", "HEAD")
            git("checkout", "release-control")

        recorded_fp = declared_fingerprint if declared_fingerprint is not None else initial_fp

        record = {
            "version": 1,
            "status": record_status,
            "candidate": {
                "repository": self.candidate_repo,
                "pull_request": self.candidate_pr,
                "target_version": "0.1.0",
                "head_sha": candidate_head,
            },
            "required_gates": {
                "python_versions": ["3.10", "3.11", "3.12"],
            },
            "live_evidence": {
                "runtime_fingerprint": recorded_fp,
            },
            "operational_constraints": [],
            "unverified_scenarios": [],
        }
        (docs / "release-control.json").write_text(json.dumps(record, indent=2))

        report_content = (
            f"# Release Control Report\n\n"
            f"**Pull Request:** [#{self.candidate_pr}](https://github.com/{self.candidate_repo}/pull/{self.candidate_pr})\n"
            f"**Candidate Head SHA:** `{candidate_head}`\n"
            f"**Target Version:** `0.1.0`\n"
            f"**Current Release Verdict:** **READY**\n"
        )
        (docs / "release-control.md").write_text(report_content)
        git("add", "docs/release-control.json", "docs/release-control.md")
        git("commit", "-m", "publish release control record and report")
        control_ref = git("rev-parse", "HEAD")

        # Return to candidate branch
        git("checkout", candidate_branch)

        return tmp_dir, tmp_root, candidate_head, control_ref, recorded_fp, record, report_content

    def test_marked_candidate_full_success_emits_verdict_go_in_text_mode_e2e(self):
        """Marked candidate with matching record, passing CI, and valid fingerprint exits 0 and emits VERDICT: GO in text mode."""
        tmp_dir, tmp_root, cand_head, control_ref, fp, record, report_content = self._create_candidate_repo_fixture()
        with tmp_dir:
            responses = make_complete_green_responses(self.candidate_repo, self.candidate_pr, cand_head)
            mock_api = MockReadOnlyGitHubApi(responses)

            out = io.StringIO()
            err = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code = verify_release.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    record_ref=control_ref,
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="text",
                    out_stream=out,
                    err_stream=err,
                )

            self.assertEqual(exit_code, 0, err.getvalue())
            output_text = out.getvalue()
            self.assertIn("VERDICT: GO", output_text)
            self.assertNotIn("Verdict: READY", output_text)
            self.assertIn(f"Release Candidate: {self.candidate_repo} PR #{self.candidate_pr}", output_text)
            self.assertIn(f"Head SHA: {cand_head}", output_text)
            self.assertIn(f"Reason: All release gates verified for candidate PR #{self.candidate_pr}", output_text)
            self.assertEqual(err.getvalue(), "")

            # Confirm all API calls are strictly read-only GET requests on candidate repo
            self.assertTrue(len(mock_api.calls) > 0)
            for method, path, _ in mock_api.calls:
                self.assertEqual(method, "GET")
                self.assertTrue(path.startswith(f"/repos/{self.candidate_repo}/"))

    def test_marked_candidate_full_success_structured_json_mode_e2e(self):
        """Marked candidate with full evidence chain outputs verdict: 'GO' and gate_verdict: 'GO' in JSON mode."""
        tmp_dir, tmp_root, cand_head, control_ref, fp, record, report_content = self._create_candidate_repo_fixture()
        with tmp_dir:
            responses = make_complete_green_responses(self.candidate_repo, self.candidate_pr, cand_head)
            mock_api = MockReadOnlyGitHubApi(responses)

            out = io.StringIO()
            err = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code = verify_release.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    record_ref=control_ref,
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="json",
                    out_stream=out,
                    err_stream=err,
                )

            self.assertEqual(exit_code, 0, err.getvalue())
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["verdict"], "GO")
            self.assertEqual(payload["gate_verdict"], "GO")
            self.assertEqual(payload["readiness"], "ready")
            self.assertEqual(payload["candidate"]["head_sha"], cand_head)
            self.assertEqual(payload["candidate"]["pull_request"], self.candidate_pr)
            self.assertEqual(payload["candidate"]["target_version"], "0.1.0")
            self.assertEqual(payload["candidate"]["repository"], self.candidate_repo)
            self.assertEqual(payload["reason"], f"All release gates verified for candidate PR #{self.candidate_pr}")
            self.assertEqual(payload["recovery"], "")
            self.assertEqual(payload["evidence"]["inferred"], [])
            self.assertEqual(payload["evidence"]["unverified"], [])

            observed = payload["evidence"]["observed"]
            self.assertTrue(any(f"PR #{self.candidate_pr} head commit matches declared SHA" in o for o in observed))
            self.assertTrue(any("All required Python CI jobs passed" in o for o in observed))
            self.assertTrue(any(f"Runtime fingerprint {fp} matches" in o for o in observed))
            self.assertTrue(any("agrees with verified candidate" in o for o in observed))

    def test_marked_candidate_pending_ci_checks_returns_code_one_with_job_diagnostic_e2e(self):
        """Pending CI check run ('in_progress') returns exit code 1 with diagnostic naming the unpassed job."""
        tmp_dir, tmp_root, cand_head, control_ref, fp, record, report_content = self._create_candidate_repo_fixture()
        with tmp_dir:
            responses = make_complete_green_responses(self.candidate_repo, self.candidate_pr, cand_head)
            cr_list = responses[f"/repos/{self.candidate_repo}/commits/{cand_head}/check-runs"]["check_runs"]
            for cr in cr_list:
                if cr["name"] == "build (3.11)":
                    cr["status"] = "in_progress"
                    cr["conclusion"] = None

            mock_api = MockReadOnlyGitHubApi(responses)

            # 1. Text format
            out_text = io.StringIO()
            err_text = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code_text = verify_release.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    record_ref=control_ref,
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="text",
                    out_stream=out_text,
                    err_stream=err_text,
                )

            self.assertEqual(exit_code_text, 1)
            combined_text = out_text.getvalue() + err_text.getvalue()
            self.assertIn("Required CI job build (3.11) is in status 'in_progress' on candidate head", combined_text)
            self.assertIn("Wait for CI job build (3.11)", combined_text)
            self.assertIn("Verdict: NOT READY", combined_text)
            self.assertNotIn("VERDICT: GO", combined_text)

            # 2. JSON format
            out_json = io.StringIO()
            err_json = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code_json = verify_release.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    record_ref=control_ref,
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="json",
                    out_stream=out_json,
                    err_stream=err_json,
                )

            self.assertEqual(exit_code_json, 1)
            payload = json.loads(out_json.getvalue())
            self.assertEqual(payload["verdict"], "not ready")
            self.assertIn("Required CI job build (3.11) is in status 'in_progress'", payload["reason"])
            self.assertIn("Wait for CI job build (3.11)", payload["recovery"])

    def test_marked_candidate_failed_ci_checks_returns_code_one_with_job_diagnostic_e2e(self):
        """Failed CI check run ('failure') returns exit code 1 with diagnostic naming the failing job."""
        tmp_dir, tmp_root, cand_head, control_ref, fp, record, report_content = self._create_candidate_repo_fixture()
        with tmp_dir:
            responses = make_complete_green_responses(self.candidate_repo, self.candidate_pr, cand_head)
            cr_list = responses[f"/repos/{self.candidate_repo}/commits/{cand_head}/check-runs"]["check_runs"]
            for cr in cr_list:
                if cr["name"] == "build (3.12)":
                    cr["status"] = "completed"
                    cr["conclusion"] = "failure"

            mock_api = MockReadOnlyGitHubApi(responses)

            # 1. Text format
            out_text = io.StringIO()
            err_text = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code_text = verify_release.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    record_ref=control_ref,
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="text",
                    out_stream=out_text,
                    err_stream=err_text,
                )

            self.assertEqual(exit_code_text, 1)
            combined_text = out_text.getvalue() + err_text.getvalue()
            self.assertIn("Required CI job build (3.12) failed with conclusion 'failure' on candidate head", combined_text)
            self.assertIn("Fix test or build failure in CI job build (3.12)", combined_text)
            self.assertIn("Verdict: NOT READY", combined_text)
            self.assertNotIn("VERDICT: GO", combined_text)

            # 2. JSON format
            out_json = io.StringIO()
            err_json = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code_json = verify_release.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    record_ref=control_ref,
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="json",
                    out_stream=out_json,
                    err_stream=err_json,
                )

            self.assertEqual(exit_code_json, 1)
            payload = json.loads(out_json.getvalue())
            self.assertEqual(payload["verdict"], "not ready")
            self.assertIn("Required CI job build (3.12) failed with conclusion 'failure'", payload["reason"])
            self.assertIn("Fix test or build failure in CI job build (3.12)", payload["recovery"])

    def test_marked_candidate_runtime_fingerprint_mismatch_returns_code_one_with_diagnostic_e2e(self):
        """Candidate code modification leading to fingerprint mismatch returns exit code 1 with mismatch diagnostic."""
        # Create repo fixture with an extra/modified runtime file on the candidate head
        tmp_dir, tmp_root, cand_head, control_ref, fp, record, report_content = self._create_candidate_repo_fixture(
            modify_candidate_after_fingerprint=True
        )
        with tmp_dir:
            responses = make_complete_green_responses(self.candidate_repo, self.candidate_pr, cand_head)
            mock_api = MockReadOnlyGitHubApi(responses)

            # 1. Text format
            out_text = io.StringIO()
            err_text = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code_text = verify_release.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    record_ref=control_ref,
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="text",
                    out_stream=out_text,
                    err_stream=err_text,
                )

            self.assertEqual(exit_code_text, 1)
            combined_text = out_text.getvalue() + err_text.getvalue()
            self.assertIn("Runtime fingerprint mismatch (recorded", combined_text)
            self.assertIn("runtime product code or templates were modified since live validation was recorded", combined_text)
            self.assertIn("Recompute runtime fingerprint for candidate commit and refresh the control record.", combined_text)
            self.assertIn("Verdict: NOT READY", combined_text)
            self.assertNotIn("VERDICT: GO", combined_text)

            # 2. JSON format
            out_json = io.StringIO()
            err_json = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code_json = verify_release.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    record_ref=control_ref,
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="json",
                    out_stream=out_json,
                    err_stream=err_json,
                )

            self.assertEqual(exit_code_json, 1)
            payload = json.loads(out_json.getvalue())
            self.assertEqual(payload["verdict"], "not ready")
            self.assertIn("Runtime fingerprint mismatch", payload["reason"])
            self.assertIn("Recompute runtime fingerprint for candidate commit", payload["recovery"])

    def test_marked_candidate_archived_control_record_skips_gate_with_code_zero_e2e(self):
        """Archived Control Record (status: 'archived') skips gate in CI mode with exit code 0."""
        tmp_dir, tmp_root, cand_head, control_ref, fp, record, report_content = self._create_candidate_repo_fixture(
            record_status="archived"
        )
        with tmp_dir:
            mock_api = MockReadOnlyGitHubApi({})

            # Text format
            out_text = io.StringIO()
            err_text = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code_text = verify_release.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    record_ref=control_ref,
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="text",
                    out_stream=out_text,
                    err_stream=err_text,
                )

            self.assertEqual(exit_code_text, 0)
            self.assertIn(
                f"Control Record is archived for historical candidate PR #{self.candidate_pr}; skipping release gate in CI.",
                out_text.getvalue(),
            )
            # Confirm no API calls were made
            self.assertEqual(len(mock_api.calls), 0)

    def test_cli_registered_candidate_full_success_via_scripts_and_gh_setup(self):
        """CLI invocations for marked candidate with complete evidence exit 0 and emit VERDICT: GO."""
        tmp_dir, tmp_root, cand_head, control_ref, fp, record, report_content = self._create_candidate_repo_fixture()
        with tmp_dir:
            responses = make_complete_green_responses(self.candidate_repo, self.candidate_pr, cand_head)
            with run_mock_github_server(responses) as server_url:
                env = os.environ.copy()
                env["GITHUB_API_URL"] = server_url
                env["GITHUB_TOKEN"] = "mock-auth-token"
                env["GITHUB_REF"] = f"refs/pull/{self.candidate_pr}/merge"
                env["GH_SETUP_REPO_ROOT"] = str(tmp_root)

                # 1. Direct scripts/verify_release.py CLI execution (text format)
                proc_script = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPTS_DIR / "verify_release.py"),
                        "--record",
                        "docs/release-control.json",
                        "--report",
                        "docs/release-control.md",
                        "--record-ref",
                        control_ref,
                        "--ci",
                        "--labels",
                        "release-candidate",
                    ],
                    cwd=tmp_root,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(proc_script.returncode, 0, proc_script.stderr)
                self.assertIn("VERDICT: GO", proc_script.stdout)
                self.assertNotIn("Verdict: READY", proc_script.stdout)
                self.assertIn(f"Head SHA: {cand_head}", proc_script.stdout)

                # 2. gh-setup wrapper execution (text format)
                proc_wrapper = subprocess.run(
                    [
                        str(GH_SETUP),
                        "verify-release",
                        "--record",
                        "docs/release-control.json",
                        "--report",
                        "docs/release-control.md",
                        "--record-ref",
                        control_ref,
                        "--ci",
                        "--labels",
                        "release-candidate",
                    ],
                    cwd=tmp_root,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(proc_wrapper.returncode, 0, proc_wrapper.stderr)
                self.assertIn("VERDICT: GO", proc_wrapper.stdout)
                self.assertNotIn("Verdict: READY", proc_wrapper.stdout)

                # 3. gh-setup wrapper execution (json format)
                proc_json = subprocess.run(
                    [
                        str(GH_SETUP),
                        "verify-release",
                        "--record",
                        "docs/release-control.json",
                        "--report",
                        "docs/release-control.md",
                        "--record-ref",
                        control_ref,
                        "--ci",
                        "--labels",
                        "release-candidate",
                        "--format",
                        "json",
                    ],
                    cwd=tmp_root,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(proc_json.returncode, 0, proc_json.stderr)
                payload = json.loads(proc_json.stdout)
                self.assertEqual(payload["verdict"], "GO")
                self.assertEqual(payload["gate_verdict"], "GO")
                self.assertEqual(payload["candidate"]["head_sha"], cand_head)

    def test_cli_registered_candidate_ci_pending_and_failed_checks_exit_one_with_job_diagnostic(self):
        """CLI invocations for pending or failed CI matrix jobs exit 1 with diagnostic naming the unpassed jobs."""
        tmp_dir, tmp_root, cand_head, control_ref, fp, record, report_content = self._create_candidate_repo_fixture()
        with tmp_dir:
            # 1. Pending check run (build (3.11))
            pending_responses = make_complete_green_responses(self.candidate_repo, self.candidate_pr, cand_head)
            cr_list = pending_responses[f"/repos/{self.candidate_repo}/commits/{cand_head}/check-runs"]["check_runs"]
            for cr in cr_list:
                if cr["name"] == "build (3.11)":
                    cr["status"] = "in_progress"
                    cr["conclusion"] = None

            with run_mock_github_server(pending_responses) as server_url:
                env = os.environ.copy()
                env["GITHUB_API_URL"] = server_url
                env["GITHUB_TOKEN"] = "mock-auth-token"
                env["GITHUB_REF"] = f"refs/pull/{self.candidate_pr}/merge"
                env["GH_SETUP_REPO_ROOT"] = str(tmp_root)

                # scripts/verify_release.py
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPTS_DIR / "verify_release.py"),
                        "--record",
                        "docs/release-control.json",
                        "--report",
                        "docs/release-control.md",
                        "--record-ref",
                        control_ref,
                        "--ci",
                        "--labels",
                        "release-candidate",
                    ],
                    cwd=tmp_root,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(proc.returncode, 1)
                combined = proc.stdout + proc.stderr
                self.assertIn("build (3.11) is in status 'in_progress'", combined)
                self.assertNotIn("VERDICT: GO", combined)

                # gh-setup wrapper with json format
                proc_wrapper = subprocess.run(
                    [
                        str(GH_SETUP),
                        "verify-release",
                        "--record",
                        "docs/release-control.json",
                        "--report",
                        "docs/release-control.md",
                        "--record-ref",
                        control_ref,
                        "--ci",
                        "--labels",
                        "release-candidate",
                        "--format",
                        "json",
                    ],
                    cwd=tmp_root,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(proc_wrapper.returncode, 1)
                payload = json.loads(proc_wrapper.stdout)
                self.assertEqual(payload["verdict"], "not ready")
                self.assertIn("build (3.11) is in status 'in_progress'", payload["reason"])

            # 2. Failed check run (build (3.12))
            failed_responses = make_complete_green_responses(self.candidate_repo, self.candidate_pr, cand_head)
            cr_list2 = failed_responses[f"/repos/{self.candidate_repo}/commits/{cand_head}/check-runs"]["check_runs"]
            for cr in cr_list2:
                if cr["name"] == "build (3.12)":
                    cr["status"] = "completed"
                    cr["conclusion"] = "failure"

            with run_mock_github_server(failed_responses) as server_url:
                env = os.environ.copy()
                env["GITHUB_API_URL"] = server_url
                env["GITHUB_TOKEN"] = "mock-auth-token"
                env["GITHUB_REF"] = f"refs/pull/{self.candidate_pr}/merge"
                env["GH_SETUP_REPO_ROOT"] = str(tmp_root)

                proc = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPTS_DIR / "verify_release.py"),
                        "--record",
                        "docs/release-control.json",
                        "--report",
                        "docs/release-control.md",
                        "--record-ref",
                        control_ref,
                        "--ci",
                        "--labels",
                        "release-candidate",
                    ],
                    cwd=tmp_root,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(proc.returncode, 1)
                combined = proc.stdout + proc.stderr
                self.assertIn("build (3.12) failed with conclusion 'failure'", combined)
                self.assertNotIn("VERDICT: GO", combined)

                # gh-setup wrapper with json format
                proc_wrapper = subprocess.run(
                    [
                        str(GH_SETUP),
                        "verify-release",
                        "--record",
                        "docs/release-control.json",
                        "--report",
                        "docs/release-control.md",
                        "--record-ref",
                        control_ref,
                        "--ci",
                        "--labels",
                        "release-candidate",
                        "--format",
                        "json",
                    ],
                    cwd=tmp_root,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(proc_wrapper.returncode, 1)
                payload = json.loads(proc_wrapper.stdout)
                self.assertEqual(payload["verdict"], "not ready")
                self.assertIn("build (3.12) failed with conclusion 'failure'", payload["reason"])

    def test_cli_registered_candidate_runtime_fingerprint_mismatch_exits_one_with_diagnostic(self):
        """CLI invocations for modified candidate runtime code exit 1 with fingerprint mismatch diagnostic."""
        tmp_dir, tmp_root, cand_head, control_ref, fp, record, report_content = self._create_candidate_repo_fixture(
            modify_candidate_after_fingerprint=True
        )
        with tmp_dir:
            responses = make_complete_green_responses(self.candidate_repo, self.candidate_pr, cand_head)
            with run_mock_github_server(responses) as server_url:
                env = os.environ.copy()
                env["GITHUB_API_URL"] = server_url
                env["GITHUB_TOKEN"] = "mock-auth-token"
                env["GITHUB_REF"] = f"refs/pull/{self.candidate_pr}/merge"
                env["GH_SETUP_REPO_ROOT"] = str(tmp_root)

                # 1. scripts/verify_release.py CLI
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPTS_DIR / "verify_release.py"),
                        "--record",
                        "docs/release-control.json",
                        "--report",
                        "docs/release-control.md",
                        "--record-ref",
                        control_ref,
                        "--ci",
                        "--labels",
                        "release-candidate",
                    ],
                    cwd=tmp_root,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(proc.returncode, 1)
                combined = proc.stdout + proc.stderr
                self.assertIn("Runtime fingerprint mismatch", combined)
                self.assertIn("Recompute runtime fingerprint", combined)
                self.assertNotIn("VERDICT: GO", combined)

                # 2. gh-setup wrapper with json format
                proc_wrapper = subprocess.run(
                    [
                        str(GH_SETUP),
                        "verify-release",
                        "--record",
                        "docs/release-control.json",
                        "--report",
                        "docs/release-control.md",
                        "--record-ref",
                        control_ref,
                        "--ci",
                        "--labels",
                        "release-candidate",
                        "--format",
                        "json",
                    ],
                    cwd=tmp_root,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(proc_wrapper.returncode, 1)
                payload = json.loads(proc_wrapper.stdout)
                self.assertEqual(payload["verdict"], "not ready")
                self.assertIn("Runtime fingerprint mismatch", payload["reason"])
                self.assertIn("Recompute runtime fingerprint", payload["recovery"])

    def test_cli_registered_candidate_archived_control_record_skips_gate_with_code_zero(self):
        """CLI invocations for archived Control Record (status: 'archived') skip gate with exit code 0."""
        tmp_dir, tmp_root, cand_head, control_ref, fp, record, report_content = self._create_candidate_repo_fixture(
            record_status="archived"
        )
        with tmp_dir:
            env = os.environ.copy()
            env["GITHUB_REF"] = f"refs/pull/{self.candidate_pr}/merge"
            env["GH_SETUP_REPO_ROOT"] = str(tmp_root)

            # 1. scripts/verify_release.py CLI
            proc_script = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "verify_release.py"),
                    "--record",
                    "docs/release-control.json",
                    "--report",
                    "docs/release-control.md",
                    "--record-ref",
                    control_ref,
                    "--ci",
                    "--labels",
                    "release-candidate",
                ],
                cwd=tmp_root,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc_script.returncode, 0, proc_script.stderr)
            self.assertIn(
                f"Control Record is archived for historical candidate PR #{self.candidate_pr}; skipping release gate in CI.",
                proc_script.stdout,
            )

            # 2. gh-setup wrapper
            proc_wrapper = subprocess.run(
                [
                    str(GH_SETUP),
                    "verify-release",
                    "--record",
                    "docs/release-control.json",
                    "--report",
                    "docs/release-control.md",
                    "--record-ref",
                    control_ref,
                    "--ci",
                    "--labels",
                    "release-candidate",
                ],
                cwd=tmp_root,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc_wrapper.returncode, 0, proc_wrapper.stderr)
            self.assertIn(
                f"Control Record is archived for historical candidate PR #{self.candidate_pr}; skipping release gate in CI.",
                proc_wrapper.stdout,
            )


if __name__ == "__main__":
    unittest.main()

