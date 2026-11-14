"""Tests for release report agreement, CI gate integration, and end-to-end verification (Ticket #27)."""

import json
import os
import pathlib
import sys
import tempfile
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    import verify_release
except ImportError:
    import scripts.verify_release as verify_release


SAMPLE_REPORT_TEXT = """# Production Release Audit Verdict and Consolidated Findings

**Audit Subject:** `gh-setup` Repository Configuration & Advisory Review Tooling  
**Parent Issue:** [#11 Production audit and release criteria](https://github.com/lispz1610/github-repo-setup/issues/11)
**Pull Request:** [#23 Audit and Production Release Readiness for gh-setup](https://github.com/lispz1610/github-repo-setup/pull/23)  
**Candidate Head SHA:** `9f4bb810a88060e48234538f7205224f0bee5659`
**Date:** 2026-10-10  
**Current Release Verdict:** **READY WITH DECLARED OPERATIONAL CONSTRAINTS**

The integrated release gate was satisfied by [Setup PR #6](https://github.com/example-org/validation-target/pull/6), merge commit `0daff4d51488d7875b1d454340bcb3ee9658ed2c`, successful [Review Run 38081922819](https://github.com/example-org/validation-target/actions/runs/38081922819), and its [inline advisory finding](https://github.com/example-org/validation-target/pull/7#discussion_r4239016462).
"""


class ReportAgreementTests(unittest.TestCase):
    """Tests asserting human release report agreement with verified record."""

    def setUp(self):
        self.candidate = {
            "repository": "lispz1610/github-repo-setup",
            "pull_request": 23,
            "target_version": "0.1.0",
            "head_sha": "9f4bb810a88060e48234538f7205224f0bee5659",
        }
        self.live_evidence = {
            "target_repository": "example-org/validation-target",
            "setup_pr": {
                "number": 6,
                "merge_commit": "0daff4d51488d7875b1d454340bcb3ee9658ed2c",
                "workflow_path": ".github/workflows/open-code-review.yml",
                "workflow_sha": "edcf6a7385269f3421750b541d5309b3ec52d696",
                "action_sha": "182898cf522da3d04157b422752d028417974e19",
            },
            "review_run": {
                "run_id": 38081922819,
                "validation_pr": 7,
                "finding_id": "r4239016462",
            },
        }

    def test_report_agreement_passes_when_verdict_and_evidence_match(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(SAMPLE_REPORT_TEXT)
            report_file = f.name
        try:
            res = verify_release.verify_release_report(
                report_path=report_file,
                candidate=self.candidate,
                verified_verdict="ready with declared operational constraints",
                live_evidence_spec=self.live_evidence,
            )
            self.assertTrue(res["valid"])
            self.assertIn("agrees", res["reason"].lower())
        finally:
            if os.path.exists(report_file):
                os.remove(report_file)

    def test_report_rejects_stale_candidate_head(self):
        report = SAMPLE_REPORT_TEXT.replace(self.candidate["head_sha"], "f" * 40)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(report)
            report_file = f.name
        try:
            result = verify_release.verify_release_report(
                report_file, self.candidate, "ready with declared operational constraints", self.live_evidence
            )
            self.assertFalse(result["valid"])
            self.assertIn("head sha", result["reason"].lower())
        finally:
            os.remove(report_file)

    def test_report_agreement_rejects_contradictory_ready_verdict(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(SAMPLE_REPORT_TEXT)
            report_file = f.name
        try:
            # Verifier determined 'not ready', but report claims READY WITH CONSTRAINTS
            res = verify_release.verify_release_report(
                report_path=report_file,
                candidate=self.candidate,
                verified_verdict="not ready",
                live_evidence_spec=self.live_evidence,
            )
            self.assertFalse(res["valid"])
            self.assertIn("contradictory", res["reason"].lower())
            self.assertIn("recovery", res)
        finally:
            if os.path.exists(report_file):
                os.remove(report_file)

    def test_report_agreement_rejects_wrong_candidate_pr(self):
        bad_report = SAMPLE_REPORT_TEXT.replace("Pull Request:** [#23", "Pull Request:** [#99")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(bad_report)
            report_file = f.name
        try:
            res = verify_release.verify_release_report(
                report_path=report_file,
                candidate=self.candidate,
                verified_verdict="ready with declared operational constraints",
                live_evidence_spec=self.live_evidence,
            )
            self.assertFalse(res["valid"])
            self.assertIn("mismatch", res["reason"].lower())
        finally:
            if os.path.exists(report_file):
                os.remove(report_file)

    def test_report_agreement_rejects_mismatched_cited_evidence(self):
        bad_report = SAMPLE_REPORT_TEXT.replace("38081922819", "99999999999")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(bad_report)
            report_file = f.name
        try:
            res = verify_release.verify_release_report(
                report_path=report_file,
                candidate=self.candidate,
                verified_verdict="ready with declared operational constraints",
                live_evidence_spec=self.live_evidence,
            )
            self.assertFalse(res["valid"])
            self.assertIn("review run", res["reason"].lower())
        finally:
            if os.path.exists(report_file):
                os.remove(report_file)

    def test_report_agreement_accepts_canonical_glossary_setup_pull_request_term(self):
        # Replace 'Setup PR #6' with canonical GLOSSARY term 'Setup Pull Request #6' without url link
        custom_report = SAMPLE_REPORT_TEXT.replace(
            "[Setup PR #6](https://github.com/example-org/validation-target/pull/6)",
            "Setup Pull Request #6",
        )
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(custom_report)
            report_file = f.name
        try:
            res = verify_release.verify_release_report(
                report_path=report_file,
                candidate=self.candidate,
                verified_verdict="ready with declared operational constraints",
                live_evidence_spec=self.live_evidence,
            )
            self.assertTrue(res["valid"], f"Expected valid, got: {res.get('reason')}")
        finally:
            if os.path.exists(report_file):
                os.remove(report_file)

    def test_report_agreement_accepts_finding_id_without_r_prefix_in_report(self):
        # Report cites 4239016462 while record has r4239016462
        custom_report = SAMPLE_REPORT_TEXT.replace("r4239016462", "4239016462")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(custom_report)
            report_file = f.name
        try:
            res = verify_release.verify_release_report(
                report_path=report_file,
                candidate=self.candidate,
                verified_verdict="ready with declared operational constraints",
                live_evidence_spec=self.live_evidence,
            )
            self.assertTrue(res["valid"], f"Expected valid, got: {res.get('reason')}")
        finally:
            if os.path.exists(report_file):
                os.remove(report_file)

    def test_report_agreement_accepts_finding_id_with_r_prefix_when_record_has_digits(self):
        # Record has finding_id "4239016462" (no r), and report has "r4239016462"
        record_evidence = dict(self.live_evidence)
        record_evidence["review_run"] = dict(self.live_evidence["review_run"], finding_id="4239016462")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(SAMPLE_REPORT_TEXT)
            report_file = f.name
        try:
            res = verify_release.verify_release_report(
                report_path=report_file,
                candidate=self.candidate,
                verified_verdict="ready with declared operational constraints",
                live_evidence_spec=record_evidence,
            )
            self.assertTrue(res["valid"], f"Expected valid, got: {res.get('reason')}")
        finally:
            if os.path.exists(report_file):
                os.remove(report_file)

    def test_report_agreement_with_multiline_formal_verdict(self):
        multiline_report = """# Release Report
Pull Request: [#23 ...](https://github.com/lispz1610/github-repo-setup/pull/23)
**Candidate Head SHA:** `9f4bb810a88060e48234538f7205224f0bee5659`

### Formal Verdict:
**READY WITH DECLARED OPERATIONAL CONSTRAINTS**

Evidence: Setup PR #6, merge 0daff4d51488d7875b1d454340bcb3ee9658ed2c, run 38081922819, finding r4239016462.
"""
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(multiline_report)
            report_file = f.name
        try:
            res = verify_release.verify_release_report(
                report_path=report_file,
                candidate=self.candidate,
                verified_verdict="ready with declared operational constraints",
                live_evidence_spec=self.live_evidence,
            )
            self.assertTrue(res["valid"])
            self.assertIn("agrees", res["reason"].lower())
        finally:
            if os.path.exists(report_file):
                os.remove(report_file)

    def test_report_agreement_with_superseded_and_candidate_prs(self):
        multi_pr_report = """# Release Report
Superseded PR: [#15 Earlier Candidate](https://github.com/lispz1610/github-repo-setup/pull/15)
Candidate Pull Request: [#23 Active Candidate](https://github.com/lispz1610/github-repo-setup/pull/23)
**Candidate Head SHA:** `9f4bb810a88060e48234538f7205224f0bee5659`

### Formal Verdict:
**READY WITH DECLARED OPERATIONAL CONSTRAINTS**

Evidence: Setup PR #6, merge 0daff4d51488d7875b1d454340bcb3ee9658ed2c, run 38081922819, finding r4239016462.
"""
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(multi_pr_report)
            report_file = f.name
        try:
            res = verify_release.verify_release_report(
                report_path=report_file,
                candidate=self.candidate,
                verified_verdict="ready with declared operational constraints",
                live_evidence_spec=self.live_evidence,
            )
            self.assertTrue(res["valid"])
            self.assertIn("agrees", res["reason"].lower())
        finally:
            if os.path.exists(report_file):
                os.remove(report_file)

    def test_report_agreement_passes_when_disposable_citations_omitted(self):
        # A release report that only declares candidate PR and verdict, omitting disposable setup PR and review run
        clean_report = """# Release Report
Pull Request: [#23 Production Candidate](https://github.com/lispz1610/github-repo-setup/pull/23)
**Candidate Head SHA:** `9f4bb810a88060e48234538f7205224f0bee5659`

### Formal Verdict:
**READY WITH DECLARED OPERATIONAL CONSTRAINTS**

All required gates and unit tests passed.
"""
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(clean_report)
            report_file = f.name
        try:
            res = verify_release.verify_release_report(
                report_path=report_file,
                candidate=self.candidate,
                verified_verdict="ready with declared operational constraints",
                live_evidence_spec=self.live_evidence,
            )
            self.assertTrue(res["valid"], f"Expected valid, got: {res.get('reason')}")
            self.assertIn("agrees", res["reason"].lower())
        finally:
            if os.path.exists(report_file):
                os.remove(report_file)


class CiGateIntegrationTests(unittest.TestCase):
    """Tests for CI gating behavior and non-blocking unaffected PRs."""

    def test_ci_mode_skips_unrelated_pr(self):
        import io
        record = {
            "version": 1,
            "candidate": {
                "repository": "lispz1610/github-repo-setup",
                "pull_request": 23,
                "target_version": "0.1.0",
                "head_sha": "a" * 40,
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(record, f)
            rec_path = f.name
        try:
            out = io.StringIO()
            old_ref = os.environ.get("GITHUB_REF")
            os.environ["GITHUB_REF"] = "refs/pull/42/merge"
            try:
                exit_code = verify_release.run_verify_release(
                    record_path=rec_path,
                    ci_mode=True,
                    out_stream=out,
                )
                self.assertEqual(exit_code, 0)
                self.assertIn("skipping release gate", out.getvalue().lower())
            finally:
                if old_ref is not None:
                    os.environ["GITHUB_REF"] = old_ref
                else:
                    os.environ.pop("GITHUB_REF", None)
        finally:
            if os.path.exists(rec_path):
                os.remove(rec_path)

    def test_ci_mode_skips_push_event_on_main(self):
        import io
        record = {
            "version": 1,
            "status": "active",
            "candidate": {
                "repository": "lispz1610/github-repo-setup",
                "pull_request": 23,
                "target_version": "0.1.0",
                "head_sha": "a" * 40,
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(record, f)
            rec_path = f.name
        try:
            out = io.StringIO()
            old_ref = os.environ.get("GITHUB_REF")
            os.environ["GITHUB_REF"] = "refs/heads/main"
            try:
                exit_code = verify_release.run_verify_release(
                    record_path=rec_path,
                    ci_mode=True,
                    out_stream=out,
                )
                self.assertEqual(exit_code, 0)
                self.assertIn("not a pull request event", out.getvalue().lower())
            finally:
                if old_ref is not None:
                    os.environ["GITHUB_REF"] = old_ref
                else:
                    os.environ.pop("GITHUB_REF", None)
        finally:
            if os.path.exists(rec_path):
                os.remove(rec_path)

    def test_ci_mode_skips_archived_record(self):
        import io
        record = {
            "version": 1,
            "status": "archived",
            "candidate": {
                "repository": "lispz1610/github-repo-setup",
                "pull_request": 23,
                "target_version": "0.1.0",
                "head_sha": "a" * 40,
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(record, f)
            rec_path = f.name
        try:
            out = io.StringIO()
            with unittest.mock.patch.dict(os.environ, {"GITHUB_REF": "refs/pull/23/merge"}):
                exit_code = verify_release.run_verify_release(
                    record_path=rec_path,
                    ci_mode=True,
                    labels=["release-candidate"],
                    out_stream=out,
                )
            self.assertEqual(exit_code, 0)
            self.assertIn("archived", out.getvalue().lower())
        finally:
            if os.path.exists(rec_path):
                os.remove(rec_path)


if __name__ == "__main__":
    unittest.main()
