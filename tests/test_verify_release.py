"""Tests for release evidence verification and candidate identity (Ticket #25)."""

import io
import json
import os
import pathlib
import py_compile
import re
import sys
import tempfile
import subprocess
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

try:
    import yaml
except ImportError:
    yaml = None



ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SCIFLOW_CICD = ROOT / "sciflow-cicd"

VALID_RECORD_PAYLOAD = {
    "version": 1,
    "status": "active",
    "candidate": {
        "repository": "lispz1610/sciflow-cicd",
        "pull_request": 23,
        "target_version": "0.1.0",
        "head_sha": "9f4bb810a88060e48234538f7205224f0bee5659",
    },
    "required_gates": {
        "python_versions": ["3.10", "3.11", "3.12"],
    },
    "live_evidence": {
        "runtime_fingerprint": "a" * 64,
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
    },
    "operational_constraints": [
        "Private Repository Constraint for Advisory Review",
        "GitHub Token OAuth Scope for Workflow Staging",
    ],
    "unverified_scenarios": [
        "Live execution of corrected direct-command exit code for failed workflow staging",
    ],
}


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


class ReleaseCandidateIdentityTests(unittest.TestCase):
    """Unit and contract tests for release candidate identity verification."""

    def setUp(self):
        try:
            import verify_release
            self.mod = verify_release
        except ImportError:
            import scripts.verify_release as vr
            self.mod = vr

    def test_valid_record_passes_validation(self):
        record_json = json.dumps(VALID_RECORD_PAYLOAD)
        parsed = self.mod.parse_release_record(record_json)
        self.assertEqual(parsed["version"], 1)
        self.assertEqual(parsed["candidate"]["pull_request"], 23)

    def test_candidate_only_record_cannot_claim_ready(self):
        record = {"version": 1, "candidate": VALID_RECORD_PAYLOAD["candidate"]}
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "release.json"
            path.write_text(json.dumps(record))
            output = io.StringIO()
            result = self.mod.run_verify_release(
                record_path=str(path), output_format="json", out_stream=output,
                api=MockReadOnlyGitHubApi({
                    "/repos/lispz1610/sciflow-cicd/pulls/23": {
                        "base": {"repo": {"full_name": "lispz1610/sciflow-cicd"}},
                        "head": {"sha": VALID_RECORD_PAYLOAD["candidate"]["head_sha"]},
                    }
                }),
            )
            self.assertEqual(result, 1)
            self.assertEqual(json.loads(output.getvalue())["verdict"], "not ready")

    def test_record_rejects_unsupported_version(self):
        bad = dict(VALID_RECORD_PAYLOAD, version=2)
        with self.assertRaises(self.mod.RecordValidationError) as ctx:
            self.mod.parse_release_record(json.dumps(bad))
        self.assertIn("unsupported record version", str(ctx.exception).lower())

    def test_record_rejects_missing_required_fields(self):
        bad = dict(VALID_RECORD_PAYLOAD)
        del bad["candidate"]
        with self.assertRaises(self.mod.RecordValidationError):
            self.mod.parse_release_record(json.dumps(bad))

    def test_record_rejects_unexpected_keys(self):
        bad = dict(VALID_RECORD_PAYLOAD, unauthorized_extra_field="surprise")
        with self.assertRaises(self.mod.RecordValidationError) as ctx:
            self.mod.parse_release_record(json.dumps(bad))
        self.assertIn("unexpected key", str(ctx.exception).lower())

    def test_record_rejects_unexpected_nested_keys(self):
        bad = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad["candidate"]["extra_nested"] = 123
        with self.assertRaises(self.mod.RecordValidationError) as ctx:
            self.mod.parse_release_record(json.dumps(bad))
        self.assertIn("unexpected key", str(ctx.exception).lower())

    def test_record_rejects_duplicate_keys(self):
        raw_json = '{"version": 1, "status": "active", "candidate": {"repository": "a/b", "pull_request": 1, "target_version": "1.0", "head_sha": "' + ("a"*40) + '"}, "candidate": {"repository": "a/b", "pull_request": 2, "target_version": "1.0", "head_sha": "' + ("a"*40) + '"}, "required_gates": {"python_versions": ["3.10"]}, "live_evidence": {"runtime_fingerprint": "' + ("b"*64) + '", "target_repository": "a/val", "setup_pr": {"number": 1, "merge_commit": "' + ("c"*40) + '", "workflow_path": ".github/workflows/ocr.yml", "workflow_sha": "' + ("d"*40) + '", "action_sha": "' + ("e"*40) + '"}, "review_run": {"run_id": 100, "validation_pr": 2, "finding_id": "r123"}}, "operational_constraints": [], "unverified_scenarios": []}'
        with self.assertRaises(self.mod.RecordValidationError) as ctx:
            self.mod.parse_release_record(raw_json)
        self.assertIn("duplicate key", str(ctx.exception).lower())

    def test_record_rejects_secret_like_tokens(self):
        tokens_to_test = [
            "ghp_1234567890abcdef1234567890abcdef1234",
            "gho_dummyOAuthTokenForTestingReject000000",
            "github_pat_11AAAAAAA0123456789abcdef",
            "sk-ant-api03-abcdefghijklmnop",
            "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            "-----BEGIN OPENSSH PRIVATE KEY-----",
        ]
        for token in tokens_to_test:
            bad = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
            bad["operational_constraints"].append(f"Constraint with {token}")
            with self.assertRaises(self.mod.RecordValidationError) as ctx:
                self.mod.parse_release_record(json.dumps(bad))
            self.assertIn("secret-like", str(ctx.exception).lower())

    def test_candidate_identity_matching_head_passes(self):
        head_sha = "9f4bb810a88060e48234538f7205224f0bee5659"
        mock_api = MockReadOnlyGitHubApi({
            "/repos/lispz1610/sciflow-cicd/pulls/23": {
                "number": 23,
                "base": {"repo": {"full_name": "lispz1610/sciflow-cicd"}},
                "head": {"sha": head_sha},
                "state": "open",
            }
        })
        parsed = self.mod.parse_release_record(json.dumps(VALID_RECORD_PAYLOAD))
        result = self.mod.verify_candidate_identity(mock_api, parsed["candidate"])
        self.assertTrue(result["valid"])
        self.assertEqual(result["observed_head_sha"], head_sha)
        self.assertIn("matches", result["reason"].lower())

    def test_candidate_identity_changed_head_rejected_with_recovery(self):
        declared_sha = "9f4bb810a88060e48234538f7205224f0bee5659"
        actual_sha = "1111111111111111111111111111111111111111"
        mock_api = MockReadOnlyGitHubApi({
            "/repos/lispz1610/sciflow-cicd/pulls/23": {
                "number": 23,
                "base": {"repo": {"full_name": "lispz1610/sciflow-cicd"}},
                "head": {"sha": actual_sha},
                "state": "open",
            }
        })
        parsed = self.mod.parse_release_record(json.dumps(VALID_RECORD_PAYLOAD))
        result = self.mod.verify_candidate_identity(mock_api, parsed["candidate"])
        self.assertFalse(result["valid"])
        self.assertIn("mismatch", result["reason"].lower())
        self.assertIn(actual_sha, result["reason"])
        self.assertIn("recovery", result)
        self.assertTrue(len(result["recovery"]) > 0)

    def test_candidate_identity_missing_pr_rejected(self):
        mock_api = MockReadOnlyGitHubApi({
            "/repos/lispz1610/sciflow-cicd/pulls/23": (404, {"message": "Not Found"})
        })
        parsed = self.mod.parse_release_record(json.dumps(VALID_RECORD_PAYLOAD))
        result = self.mod.verify_candidate_identity(mock_api, parsed["candidate"])
        self.assertFalse(result["valid"])
        self.assertIn("not found", result["reason"].lower())
        self.assertIn("recovery", result)

    def test_api_failure_fails_closed_without_leaking_credentials(self):
        token_secret = "gho_super_secret_github_token_value_xyz"

        class FailingApiWithToken:
            def request(self, method, path, payload=None):
                raise RuntimeError(f"Error while calling {path} with token {token_secret}")

        parsed = self.mod.parse_release_record(json.dumps(VALID_RECORD_PAYLOAD))
        result = self.mod.verify_candidate_identity(FailingApiWithToken(), parsed["candidate"])
        self.assertFalse(result["valid"])
        self.assertNotIn(token_secret, result["reason"])
        self.assertNotIn(token_secret, result["recovery"])
        self.assertIn("[REDACTED]", result["reason"] + result["recovery"])

    def test_record_rejects_aws_key_and_credentials_in_url(self):
        bad_aws = dict(VALID_RECORD_PAYLOAD, unverified_scenarios=["AKIAIOSFODNN7EXAMPLE"])
        with self.assertRaises(self.mod.RecordValidationError) as ctx:
            self.mod.parse_release_record(json.dumps(bad_aws))
        self.assertIn("credential token detected", str(ctx.exception).lower())
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", str(ctx.exception))

        bad_url = dict(VALID_RECORD_PAYLOAD, unverified_scenarios=["https://deployer:secrettoken123@github.com/org/repo.git"])
        with self.assertRaises(self.mod.RecordValidationError) as ctx_url:
            self.mod.parse_release_record(json.dumps(bad_url))
        self.assertIn("credential token detected", str(ctx_url.exception).lower())
        self.assertNotIn("secrettoken123", str(ctx_url.exception))

    def test_record_secret_in_dict_key_does_not_leak_key_in_exception(self):
        secret_key = "AKIAIOSFODNN7EXAMPLE"
        bad_payload = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad_payload[secret_key] = "some-value"
        with self.assertRaises(self.mod.RecordValidationError) as ctx:
            self.mod.parse_release_record(json.dumps(bad_payload))
        err_msg = str(ctx.exception)
        self.assertIn("credential token detected", err_msg.lower())
        self.assertNotIn(secret_key, err_msg)
        self.assertIn(".key", err_msg)

    def test_record_rejects_empty_required_gates(self):
        bad = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad["required_gates"] = {}
        with self.assertRaises(self.mod.RecordValidationError) as ctx:
            self.mod.parse_release_record(json.dumps(bad))
        self.assertIn("python_versions", str(ctx.exception).lower())

    def test_record_rejects_empty_python_versions_list(self):
        bad = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad["required_gates"] = {"python_versions": []}
        with self.assertRaises(self.mod.RecordValidationError) as ctx:
            self.mod.parse_release_record(json.dumps(bad))
        self.assertIn("python_versions", str(ctx.exception).lower())

    def test_record_with_standalone_runtime_fingerprint_parses_successfully(self):
        # Full record with standalone live_evidence (runtime_fingerprint only)
        payload = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        payload["live_evidence"] = {
            "runtime_fingerprint": "a" * 64,
        }
        parsed = self.mod.parse_release_record(json.dumps(payload))
        self.assertEqual(parsed["live_evidence"]["runtime_fingerprint"], "a" * 64)
        self.assertEqual(set(parsed["live_evidence"].keys()), {"runtime_fingerprint"})
        self.assertNotIn("target_repository", parsed["live_evidence"])
        self.assertNotIn("setup_pr", parsed["live_evidence"])
        self.assertNotIn("review_run", parsed["live_evidence"])

        # Minimal self-contained record (omitting optional root keys)
        minimal_payload = {
            "version": 1,
            "candidate": VALID_RECORD_PAYLOAD["candidate"],
            "required_gates": VALID_RECORD_PAYLOAD["required_gates"],
            "live_evidence": {
                "runtime_fingerprint": "f" * 64,
            },
        }
        parsed_min = self.mod.parse_release_record(json.dumps(minimal_payload))
        self.assertEqual(parsed_min["live_evidence"], {"runtime_fingerprint": "f" * 64})
        self.assertEqual(list(parsed_min["live_evidence"].keys()), ["runtime_fingerprint"])

    def test_record_accepts_uppercase_and_mixed_case_runtime_fingerprint(self):
        # All uppercase hex
        payload_upper = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        upper_fp = "A" * 64
        payload_upper["live_evidence"] = {"runtime_fingerprint": upper_fp}
        parsed_upper = self.mod.parse_release_record(json.dumps(payload_upper))
        self.assertEqual(parsed_upper["live_evidence"]["runtime_fingerprint"], upper_fp)

        # Mixed-case hex
        payload_mixed = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        mixed_fp = "4Db1179C7eB581C0FDD5C891a801636B75f6Ef42540E420E7a05f3E5034e34E7"
        payload_mixed["live_evidence"] = {"runtime_fingerprint": mixed_fp}
        parsed_mixed = self.mod.parse_release_record(json.dumps(payload_mixed))
        self.assertEqual(parsed_mixed["live_evidence"]["runtime_fingerprint"], mixed_fp)

    def test_legacy_record_files_on_disk_parse_successfully(self):
        # Verify docs/release-control.json parses cleanly as a valid legacy record
        control_path = ROOT / "docs" / "release-control.json"
        self.assertTrue(control_path.is_file(), f"File not found: {control_path}")
        control_content = control_path.read_text(encoding="utf-8")
        parsed_control = self.mod.parse_release_record(control_content)

        self.assertEqual(parsed_control["version"], 1)
        self.assertEqual(parsed_control["status"], "candidate")
        self.assertEqual(parsed_control["candidate"]["repository"], "lispz1610/sciflow-cicd")
        self.assertEqual(parsed_control["candidate"]["pull_request"], 28)
        self.assertEqual(parsed_control["candidate"]["target_version"], "0.1.0-rc.1")
        self.assertEqual(parsed_control["candidate"]["head_sha"], "4c7c6cfeaaac74c69cbc39bff1977e803ec18efe")

        control_le = parsed_control["live_evidence"]
        self.assertEqual(control_le["runtime_fingerprint"], "4db1179c7eb581c0fdd5c891a801636b75f6ef42540e420e7a05f3e5034e34e7")
        self.assertTrue(bool(control_le["target_repository"]))
        self.assertEqual(control_le["setup_pr"]["number"], 8)
        self.assertEqual(control_le["setup_pr"]["merge_commit"], "032982c3d93d7317d1aea498dcdedcb3357a0896")
        self.assertEqual(control_le["setup_pr"]["workflow_path"], ".github/workflows/open-code-review.yml")
        self.assertEqual(control_le["setup_pr"]["workflow_sha"], "60e885c7cfe54084862da4fca0add3b714749439")
        self.assertEqual(control_le["setup_pr"]["action_sha"], "182898cf522da3d04157b422752d028417974e19")
        self.assertEqual(control_le["review_run"]["run_id"], 38094461332)
        self.assertEqual(control_le["review_run"]["validation_pr"], 10)
        self.assertEqual(control_le["review_run"]["finding_id"], "r4239553645")
        self.assertEqual(control_le["review_run"]["workflow_commit"], "032982c3d93d7317d1aea498dcdedcb3357a0896")

        # Verify docs/release-evidence.json parses cleanly as a valid legacy record
        evidence_path = ROOT / "docs" / "release-evidence.json"
        self.assertTrue(evidence_path.is_file(), f"File not found: {evidence_path}")
        evidence_content = evidence_path.read_text(encoding="utf-8")
        parsed_evidence = self.mod.parse_release_record(evidence_content)

        self.assertEqual(parsed_evidence["version"], 1)
        self.assertEqual(parsed_evidence["status"], "archived")
        self.assertEqual(parsed_evidence["candidate"]["repository"], "lispz1610/sciflow-cicd")
        self.assertEqual(parsed_evidence["candidate"]["pull_request"], 23)
        self.assertEqual(parsed_evidence["candidate"]["target_version"], "0.1.0")
        self.assertEqual(parsed_evidence["candidate"]["head_sha"], "9f4bb810a88060e48234538f7205224f0bee5659")

        evidence_le = parsed_evidence["live_evidence"]
        self.assertEqual(evidence_le["runtime_fingerprint"], "18b032e3e2e846458c953fb5445c8471e674454797183ad8113f8c53418a26bc")
        self.assertTrue(bool(evidence_le["target_repository"]))
        self.assertEqual(evidence_le["setup_pr"]["number"], 6)
        self.assertEqual(evidence_le["setup_pr"]["merge_commit"], "0daff4d51488d7875b1d454340bcb3ee9658ed2c")
        self.assertEqual(evidence_le["setup_pr"]["workflow_path"], ".github/workflows/open-code-review.yml")
        self.assertEqual(evidence_le["setup_pr"]["workflow_sha"], "edcf6a7385269f3421750b541d5309b3ec52d696")
        self.assertEqual(evidence_le["setup_pr"]["action_sha"], "182898cf522da3d04157b422752d028417974e19")
        self.assertEqual(evidence_le["review_run"]["run_id"], 38081922819)
        self.assertEqual(evidence_le["review_run"]["validation_pr"], 7)
        self.assertEqual(evidence_le["review_run"]["finding_id"], "r4239016462")

    def test_record_accepts_omission_of_individual_legacy_fields_in_live_evidence(self):
        # Omitting setup_pr and review_run
        payload1 = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        payload1["live_evidence"] = {
            "runtime_fingerprint": "b" * 64,
            "target_repository": "example-org/validation-target",
        }
        parsed1 = self.mod.parse_release_record(json.dumps(payload1))
        self.assertEqual(parsed1["live_evidence"]["runtime_fingerprint"], "b" * 64)
        self.assertEqual(parsed1["live_evidence"]["target_repository"], "example-org/validation-target")

        # Omitting target_repository and review_run
        payload2 = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        payload2["live_evidence"] = {
            "runtime_fingerprint": "c" * 64,
            "setup_pr": VALID_RECORD_PAYLOAD["live_evidence"]["setup_pr"],
        }
        parsed2 = self.mod.parse_release_record(json.dumps(payload2))
        self.assertEqual(parsed2["live_evidence"]["runtime_fingerprint"], "c" * 64)

        # Omitting target_repository and setup_pr
        payload3 = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        payload3["live_evidence"] = {
            "runtime_fingerprint": "d" * 64,
            "review_run": VALID_RECORD_PAYLOAD["live_evidence"]["review_run"],
        }
        parsed3 = self.mod.parse_release_record(json.dumps(payload3))
        self.assertEqual(parsed3["live_evidence"]["runtime_fingerprint"], "d" * 64)

    def test_record_rejects_live_evidence_missing_runtime_fingerprint(self):
        bad_empty = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad_empty["live_evidence"] = {}
        with self.assertRaises(self.mod.RecordValidationError) as ctx:
            self.mod.parse_release_record(json.dumps(bad_empty))
        self.assertIn("runtime_fingerprint", str(ctx.exception).lower())

        bad_legacy_only = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        del bad_legacy_only["live_evidence"]["runtime_fingerprint"]
        with self.assertRaises(self.mod.RecordValidationError) as ctx:
            self.mod.parse_release_record(json.dumps(bad_legacy_only))
        self.assertIn("runtime_fingerprint", str(ctx.exception).lower())

    def test_record_rejects_invalid_runtime_fingerprint_format(self):
        invalid_fingerprints = [
            # Wrong length strings
            "",                   # 0 chars (empty)
            "a",                  # 1 char
            "a" * 32,             # 32 chars
            "a" * 63,             # 63 chars (too short)
            "a" * 65,             # 65 chars (too long)
            "a" * 128,            # 128 chars
            # Non-hex characters
            "z" * 64,             # non-hex character 'z'
            "g" * 64,             # non-hex character 'g'
            ("a" * 63) + "g",     # trailing non-hex character
            "g" + ("a" * 63),     # leading non-hex character
            "!" * 64,             # non-hex punctuation
            "@" * 64,             # non-hex symbol
            # Whitespace and newlines
            " " + ("a" * 64),     # leading whitespace
            ("a" * 64) + " ",     # trailing whitespace
            ("a" * 32) + "\n" + ("a" * 31),  # embedded newline
            ("a" * 32) + " " + ("a" * 31),   # embedded space
            # Non-string types
            12345,                # integer
            12.34,                # float
            True,                 # boolean True
            False,                # boolean False
            None,                 # None
            ["a" * 64],           # list of string
            {"sha": "a" * 64},    # dict
        ]
        for fp in invalid_fingerprints:
            bad = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
            bad["live_evidence"] = {"runtime_fingerprint": fp}
            with self.assertRaises(self.mod.RecordValidationError) as ctx:
                self.mod.parse_release_record(json.dumps(bad))
            self.assertIn("runtime_fingerprint", str(ctx.exception).lower(), f"Expected 'runtime_fingerprint' in error for {fp!r}")

    def test_record_rejects_unknown_keys_in_live_evidence(self):
        # Unknown key alongside full legacy fields
        bad_legacy = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad_legacy["live_evidence"]["unexpected_custom_key"] = "extra"
        with self.assertRaises(self.mod.RecordValidationError) as ctx1:
            self.mod.parse_release_record(json.dumps(bad_legacy))
        self.assertIn("unexpected key", str(ctx1.exception).lower())
        self.assertIn("unexpected_custom_key", str(ctx1.exception))

        # Unknown key in standalone live_evidence
        bad_standalone = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad_standalone["live_evidence"] = {
            "runtime_fingerprint": "a" * 64,
            "extra_standalone_field": "disallowed",
        }
        with self.assertRaises(self.mod.RecordValidationError) as ctx2:
            self.mod.parse_release_record(json.dumps(bad_standalone))
        self.assertIn("unexpected key", str(ctx2.exception).lower())
        self.assertIn("extra_standalone_field", str(ctx2.exception))

        # Multiple unknown keys
        bad_multi = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad_multi["live_evidence"] = {
            "runtime_fingerprint": "a" * 64,
            "extra_one": 1,
            "extra_two": 2,
        }
        with self.assertRaises(self.mod.RecordValidationError) as ctx3:
            self.mod.parse_release_record(json.dumps(bad_multi))
        self.assertIn("unexpected key", str(ctx3.exception).lower())

        # Typos in known keys
        typos = [
            {"runtime_fingerprints": "a" * 64, "runtime_fingerprint": "a" * 64},
            {"target_repo": "example-org/validation-target", "runtime_fingerprint": "a" * 64},
            {"setup_pull_request": {"number": 1}, "runtime_fingerprint": "a" * 64},
            {"review_runs": {"run_id": 1}, "runtime_fingerprint": "a" * 64},
        ]
        for typo_dict in typos:
            bad_typo = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
            bad_typo["live_evidence"] = typo_dict
            with self.assertRaises(self.mod.RecordValidationError) as ctx_typo:
                self.mod.parse_release_record(json.dumps(bad_typo))
            self.assertIn("unexpected key", str(ctx_typo.exception).lower())


    def test_record_rejects_boolean_types_in_integer_fields(self):
        # Version must be int and not bool
        bad_version = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad_version["version"] = True
        with self.assertRaises(self.mod.RecordValidationError):
            self.mod.parse_release_record(json.dumps(bad_version))

        # Candidate pull_request must be int and not bool
        bad_pr = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad_pr["candidate"]["pull_request"] = True
        with self.assertRaises(self.mod.RecordValidationError):
            self.mod.parse_release_record(json.dumps(bad_pr))

        # Setup PR number must be int and not bool
        bad_spr = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad_spr["live_evidence"]["setup_pr"]["number"] = True
        with self.assertRaises(self.mod.RecordValidationError):
            self.mod.parse_release_record(json.dumps(bad_spr))

        # Review run_id must be int and not bool
        bad_run = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad_run["live_evidence"]["review_run"]["run_id"] = True
        with self.assertRaises(self.mod.RecordValidationError):
            self.mod.parse_release_record(json.dumps(bad_run))

        # Validation PR number must be int and not bool
        bad_val_pr = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad_val_pr["live_evidence"]["review_run"]["validation_pr"] = True
        with self.assertRaises(self.mod.RecordValidationError):
            self.mod.parse_release_record(json.dumps(bad_val_pr))

        # Finding ID must not be bool
        bad_finding = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad_finding["live_evidence"]["review_run"]["finding_id"] = True
        with self.assertRaises(self.mod.RecordValidationError):
            self.mod.parse_release_record(json.dumps(bad_finding))

    def test_candidate_identity_handles_null_nested_fields_without_attribute_error(self):
        # GitHub API returning base as None or base.repo as None
        mock_api = MockReadOnlyGitHubApi({
            "/repos/lispz1610/sciflow-cicd/pulls/23": {
                "number": 23,
                "base": None,
                "head": {"sha": "9f4bb810a88060e48234538f7205224f0bee5659"},
                "state": "open",
            }
        })
        parsed = self.mod.parse_release_record(json.dumps(VALID_RECORD_PAYLOAD))
        result = self.mod.verify_candidate_identity(mock_api, parsed["candidate"])
        self.assertTrue(result["valid"])

        mock_api_none_repo = MockReadOnlyGitHubApi({
            "/repos/lispz1610/sciflow-cicd/pulls/23": {
                "number": 23,
                "base": {"repo": None},
                "head": {"sha": "9f4bb810a88060e48234538f7205224f0bee5659"},
                "state": "open",
            }
        })
        result_none_repo = self.mod.verify_candidate_identity(mock_api_none_repo, parsed["candidate"])
        self.assertTrue(result_none_repo["valid"])

    def test_candidate_identity_rejects_base_repo_mismatch(self):
        mock_api = MockReadOnlyGitHubApi({
            "/repos/lispz1610/sciflow-cicd/pulls/23": {
                "number": 23,
                "base": {"repo": {"full_name": "other-org/other-repo"}},
                "head": {"sha": "9f4bb810a88060e48234538f7205224f0bee5659"},
                "state": "open",
            }
        })
        parsed = self.mod.parse_release_record(json.dumps(VALID_RECORD_PAYLOAD))
        result = self.mod.verify_candidate_identity(mock_api, parsed["candidate"])
        self.assertFalse(result["valid"])
        self.assertIn("base repository mismatch", result["reason"].lower())

    def test_zero_writes_enforced_during_verification(self):
        mock_api = MockReadOnlyGitHubApi({
            "/repos/lispz1610/sciflow-cicd/pulls/23": {
                "number": 23,
                "base": {"repo": {"full_name": "lispz1610/sciflow-cicd"}},
                "head": {"sha": "9f4bb810a88060e48234538f7205224f0bee5659"},
                "state": "open",
            }
        })
        parsed = self.mod.parse_release_record(json.dumps(VALID_RECORD_PAYLOAD))
        self.mod.verify_candidate_identity(mock_api, parsed["candidate"])
        self.assertTrue(len(mock_api.calls) > 0)
        for method, path, _ in mock_api.calls:
            self.assertEqual(method, "GET")

    def test_record_rejects_doi_with_non_issued_state(self):
        sample_doi = "10.5281/zenodo.1234567"
        # Test root container with requested, pending, and missing doi_state
        for non_issued in ("requested", "pending", "unselected", "unavailable"):
            bad_root = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
            bad_root["doi"] = sample_doi
            bad_root["doi_state"] = non_issued
            with self.assertRaises(self.mod.RecordValidationError) as ctx:
                self.mod.parse_release_record(json.dumps(bad_root))
            self.assertIn("record root", str(ctx.exception))
            self.assertIn("cannot be declared", str(ctx.exception).lower())

            bad_cand = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
            bad_cand["candidate"]["doi"] = sample_doi
            bad_cand["candidate"]["doi_state"] = non_issued
            with self.assertRaises(self.mod.RecordValidationError) as ctx:
                self.mod.parse_release_record(json.dumps(bad_cand))
            self.assertIn("candidate object", str(ctx.exception))
            self.assertIn("cannot be declared", str(ctx.exception).lower())

        # Test with missing doi_state
        bad_root_no_state = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad_root_no_state["doi"] = sample_doi
        with self.assertRaises(self.mod.RecordValidationError) as ctx:
            self.mod.parse_release_record(json.dumps(bad_root_no_state))
        self.assertIn("record root", str(ctx.exception))
        self.assertIn("cannot be declared", str(ctx.exception).lower())

        bad_cand_no_state = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad_cand_no_state["candidate"]["doi"] = sample_doi
        with self.assertRaises(self.mod.RecordValidationError) as ctx:
            self.mod.parse_release_record(json.dumps(bad_cand_no_state))
        self.assertIn("candidate object", str(ctx.exception))
        self.assertIn("cannot be declared", str(ctx.exception).lower())

    def test_record_rejects_issued_state_without_doi(self):
        # Root container
        bad_root = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad_root["doi_state"] = "issued"
        with self.assertRaises(self.mod.RecordValidationError) as ctx:
            self.mod.parse_release_record(json.dumps(bad_root))
        self.assertIn("record root", str(ctx.exception))
        self.assertIn("missing 'doi'", str(ctx.exception).lower())

        # Candidate container
        bad_cand = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        bad_cand["candidate"]["doi_state"] = "issued"
        with self.assertRaises(self.mod.RecordValidationError) as ctx:
            self.mod.parse_release_record(json.dumps(bad_cand))
        self.assertIn("candidate object", str(ctx.exception))
        self.assertIn("missing 'doi'", str(ctx.exception).lower())

    def test_record_accepts_issued_state_with_verified_doi(self):
        sample_doi = "10.5281/zenodo.1234567"
        good_root = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        good_root["doi_state"] = "issued"
        good_root["doi"] = sample_doi
        parsed_root = self.mod.parse_release_record(json.dumps(good_root))
        self.assertEqual(parsed_root["doi"], sample_doi)
        self.assertEqual(parsed_root["doi_state"], "issued")

        good_cand = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
        good_cand["candidate"]["doi_state"] = "issued"
        good_cand["candidate"]["doi"] = sample_doi
        parsed_cand = self.mod.parse_release_record(json.dumps(good_cand))
        self.assertEqual(parsed_cand["candidate"]["doi"], sample_doi)
        self.assertEqual(parsed_cand["candidate"]["doi_state"], "issued")

    def test_record_accepts_non_issued_state_without_doi(self):
        for state in ("requested", "pending", "unselected", "unavailable"):
            good_root = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
            good_root["doi_state"] = state
            parsed = self.mod.parse_release_record(json.dumps(good_root))
            self.assertEqual(parsed["doi_state"], state)
            self.assertNotIn("doi", parsed)

            good_cand = json.loads(json.dumps(VALID_RECORD_PAYLOAD))
            good_cand["candidate"]["doi_state"] = state
            parsed_c = self.mod.parse_release_record(json.dumps(good_cand))
            self.assertEqual(parsed_c["candidate"]["doi_state"], state)
            self.assertNotIn("doi", parsed_c["candidate"])



class CliVerifyReleaseCommandTests(unittest.TestCase):
    """Test CLI integration of verify-release command via sciflow-cicd and python module."""

    def test_cli_help_lists_verify_release_subcommand(self):
        from scripts.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["verify-release", "--record", "dummy.json", "--record-ref", "a" * 40])
        self.assertEqual(args.command, "verify-release")
        self.assertEqual(args.record, "dummy.json")
        self.assertEqual(args.record_ref, "a" * 40)

    def test_cli_missing_record_file_returns_nonzero_and_structured_error(self):
        import subprocess
        proc = subprocess.run(
            [str(SCIFLOW_CICD), "verify-release", "--record", "nonexistent-record.json", "--format", "json"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 1)
        data = json.loads(proc.stdout)
        self.assertEqual(data["verdict"], "not ready")
        self.assertIn("not found", data["reason"].lower())
        self.assertIn("recovery", data)

    def test_cli_malformed_record_returns_nonzero_and_actionable_recovery(self):
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write('{"version": 1, "invalid": true}')
            temp_path = f.name
        try:
            proc = subprocess.run(
                [str(SCIFLOW_CICD), "verify-release", "--record", temp_path, "--format", "json"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 1)
            data = json.loads(proc.stdout)
            self.assertEqual(data["verdict"], "not ready")
            self.assertIn("unexpected key", data["reason"].lower())
            self.assertIn("recovery", data)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)



class SelfContainedVerifierEngineTests(unittest.TestCase):
    """Tests for self-contained verifier engine without disposable targets or secondary tokens (Ticket #44)."""

    def setUp(self):
        try:
            import verify_release
            self.mod = verify_release
        except ImportError:
            import scripts.verify_release as vr
            self.mod = vr

    def test_verify_live_evidence_is_completely_removed(self):
        """Verify that verify_live_evidence function no longer exists in verify_release module."""
        self.assertFalse(
            hasattr(self.mod, "verify_live_evidence"),
            "verify_live_evidence function must be completely removed from scripts/verify_release.py",
        )

    def test_release_evidence_read_token_is_completely_removed(self):
        """Verify that RELEASE_EVIDENCE_READ_TOKEN is nowhere in scripts/verify_release.py."""
        script_path = ROOT / "scripts" / "verify_release.py"
        content = script_path.read_text(encoding="utf-8")
        self.assertNotIn(
            "RELEASE_EVIDENCE_READ_TOKEN",
            content,
            "RELEASE_EVIDENCE_READ_TOKEN must be completely removed from scripts/verify_release.py",
        )

    def _create_candidate_test_fixture(self):
        import subprocess
        head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        computed_fp = self.mod.compute_runtime_fingerprint(ROOT, commit_sha=head_sha)

        record = {
            "version": 1,
            "status": "active",
            "candidate": {
                "repository": "lispz1610/sciflow-cicd",
                "pull_request": 23,
                "target_version": "0.1.0",
                "head_sha": head_sha,
            },
            "required_gates": {
                "python_versions": ["3.10", "3.11", "3.12"],
            },
            "live_evidence": {
                "runtime_fingerprint": computed_fp,
            },
            "operational_constraints": [],
            "unverified_scenarios": [],
        }

        mock_api = MockReadOnlyGitHubApi({
            "/repos/lispz1610/sciflow-cicd/pulls/23": {
                "number": 23,
                "base": {"repo": {"full_name": "lispz1610/sciflow-cicd"}},
                "head": {"sha": head_sha},
                "state": "open",
            },
            f"/repos/lispz1610/sciflow-cicd/commits/{head_sha}/check-runs": {
                "total_count": 3,
                "check_runs": [
                    {"name": "build (3.10)", "status": "completed", "conclusion": "success", "head_sha": head_sha},
                    {"name": "build (3.11)", "status": "completed", "conclusion": "success", "head_sha": head_sha},
                    {"name": "build (3.12)", "status": "completed", "conclusion": "success", "head_sha": head_sha},
                ],
            },
        })
        return head_sha, record, mock_api

    def test_run_verify_release_with_standalone_runtime_fingerprint_passes(self):
        """Verify that run_verify_release qualifies candidate using only runtime_fingerprint and primary API."""
        head_sha, record, mock_api = self._create_candidate_test_fixture()

        with tempfile.TemporaryDirectory() as temp_dir:
            rec_file = pathlib.Path(temp_dir) / "release-evidence.json"
            rec_file.write_text(json.dumps(record), encoding="utf-8")
            out = io.StringIO()

            exit_code = self.mod.run_verify_release(
                record_path=str(rec_file),
                api=mock_api,
                repo_root=ROOT,
                output_format="json",
                out_stream=out,
            )

            self.assertEqual(exit_code, 0, f"run_verify_release failed: {out.getvalue()}")
            result = json.loads(out.getvalue())
            self.assertEqual(result["verdict"], "ready")
            self.assertIn("All release gates verified", result["reason"])

            # Verify no queries targeted disposable test targets or any foreign repo
            for method, path, _ in mock_api.calls:
                self.assertEqual(method, "GET")
                self.assertTrue(path.startswith("/repos/lispz1610/sciflow-cicd/"))

    def test_run_verify_release_uses_only_primary_client_and_no_secondary_token(self):
        """Verify that verifier creates only a single API client with GITHUB_TOKEN."""
        head_sha, record, mock_api = self._create_candidate_test_fixture()

        with tempfile.TemporaryDirectory() as temp_dir:
            rec_file = pathlib.Path(temp_dir) / "release-evidence.json"
            rec_file.write_text(json.dumps(record), encoding="utf-8")
            out = io.StringIO()

            with patch.dict(os.environ, {
                "GITHUB_TOKEN": "primary-gh-token",
            }), patch.object(self.mod, "get_default_api", return_value=mock_api) as mock_get_api:
                exit_code = self.mod.run_verify_release(
                    record_path=str(rec_file),
                    repo_root=ROOT,
                    output_format="json",
                    out_stream=out,
                )

                self.assertEqual(exit_code, 0, f"run_verify_release failed: {out.getvalue()}")
                # get_default_api must be called exactly once (for the primary client)
                self.assertEqual(mock_get_api.call_count, 1)
                self.assertEqual(mock_get_api.call_args.kwargs, {"token": None})


class WorkflowAndBaselineValidationTests(unittest.TestCase):
    """Validation for workflow syntax, permissions, and token absence (Ticket #47)."""

    def setUp(self):
        self.workflow_path = ROOT / ".github" / "workflows" / "python-app.yml"
        self.workflows_dir = ROOT / ".github" / "workflows"
        self.scripts_dir = ROOT / "scripts"
        self.tests_dir = ROOT / "tests"

    def test_workflow_yaml_syntax_validates_cleanly(self):
        """Validate .github/workflows/python-app.yml YAML syntax with yaml.safe_load."""
        self.assertTrue(self.workflow_path.exists(), f"Workflow file not found: {self.workflow_path}")
        content = self.workflow_path.read_text(encoding="utf-8")
        if yaml is not None:
            parsed = yaml.safe_load(content)
            self.assertIsInstance(parsed, dict, "Workflow root must parse as a dictionary")
            self.assertIn("name", parsed)
            # In YAML 1.1, unquoted 'on:' parses as boolean True; both 'on' and True represent workflow triggers
            self.assertTrue("on" in parsed or True in parsed, "Workflow triggers must be defined under 'on'")
            self.assertIn("permissions", parsed)
            self.assertIn("jobs", parsed)
            self.assertIn("build", parsed["jobs"])
            self.assertIn("verify-release", parsed["jobs"])
        else:
            self.assertIn("name:", content)
            self.assertIn("permissions:", content)
            self.assertIn("jobs:", content)
            self.assertIn("verify-release:", content)

    def test_workflow_permissions_minimal_for_verify_release_and_baseline(self):
        """Validate workflow permissions for verify-release job and root permissions."""
        content = self.workflow_path.read_text(encoding="utf-8")
        if yaml is not None:
            parsed = yaml.safe_load(content)
            # Root permissions must remain minimal
            self.assertEqual(
                parsed.get("permissions"),
                {"contents": "read"},
                "Root permissions must be minimal (contents: read)",
            )

            # verify-release job permissions must be strictly contents: read, checks: read, pull-requests: read
            verify_job = parsed["jobs"]["verify-release"]
            self.assertIn("permissions", verify_job, "verify-release job must declare explicit permissions")
            expected_permissions = {
                "contents": "read",
                "checks": "read",
                "pull-requests": "read",
            }
            self.assertEqual(
                verify_job["permissions"],
                expected_permissions,
                "verify-release job permissions must strictly match minimal read-only permissions",
            )

            # No job in the workflow may have write permissions
            for job_name, job_spec in parsed.get("jobs", {}).items():
                perms = job_spec.get("permissions", {})
                if isinstance(perms, dict):
                    for scope, level in perms.items():
                        self.assertEqual(
                            level,
                            "read",
                            f"Job {job_name} has non-read permission {scope}: {level}",
                        )
        else:
            self.assertIn("permissions:\n  contents: read", content)
            self.assertIn("contents: read", content)
            self.assertIn("checks: read", content)
            self.assertIn("pull-requests: read", content)
            self.assertNotIn("write", content)

    def _get_workflow_files(self):
        files = [f for f in self.workflows_dir.glob("*") if f.is_file() and f.suffix in (".yml", ".yaml")]
        self.assertTrue(len(files) > 0, "No workflow files found")
        return files

    def _get_script_files(self):
        files = [f for f in self.scripts_dir.glob("*") if f.is_file() and f.suffix in (".py", ".sh")]
        self.assertTrue(len(files) > 0, "No script files found")
        return files

    def _get_test_files(self):
        files = [f for f in self.tests_dir.glob("test_*.py") if f.is_file()]
        self.assertTrue(len(files) > 0, "No test files found")
        return files

    def test_absence_of_disposable_target_and_token_in_workflows_and_scripts(self):
        """Validate absence of RELEASE_EVIDENCE_READ_TOKEN and disposable targets in workflows, scripts, and tests."""
        token_name = "RELEASE_EVIDENCE_READ_TOKEN"
        disposable_target = "-".join(["open", "code", "review", "validation"])

        # Check all workflow files in .github/workflows/
        for wf_file in self._get_workflow_files():
            content = wf_file.read_text(encoding="utf-8")
            self.assertNotIn(
                token_name,
                content,
                f"Forbidden secret {token_name} found in workflow: {wf_file.name}",
            )
            self.assertNotIn(
                disposable_target,
                content,
                f"Forbidden target repo found in workflow: {wf_file.name}",
            )

        # Check all script files in scripts/
        for sc_file in self._get_script_files():
            content = sc_file.read_text(encoding="utf-8")
            self.assertNotIn(
                token_name,
                content,
                f"Forbidden secret {token_name} found in script: {sc_file.name}",
            )
            self.assertNotIn(
                disposable_target,
                content,
                f"Forbidden target repo found in script: {sc_file.name}",
            )

        # Check all active test files in tests/ (confirm zero occurrences of disposable target repo)
        for ts_file in self._get_test_files():
            content = ts_file.read_text(encoding="utf-8")
            self.assertNotIn(
                disposable_target,
                content,
                f"Forbidden target repo found in test file: {ts_file.name}",
            )

    def test_zero_occurrences_of_release_evidence_read_token_across_workflows_scripts_and_active_tests(self):
        """Assert zero occurrences of RELEASE_EVIDENCE_READ_TOKEN across workflows, scripts, and active test assertions."""
        token_name = "RELEASE_EVIDENCE_READ_TOKEN"

        # 1. Zero occurrences in workflows
        for wf_file in self._get_workflow_files():
            self.assertNotIn(token_name, wf_file.read_text(encoding="utf-8"), f"Token found in workflow {wf_file}")

        # 2. Zero occurrences in scripts
        for sc_file in self._get_script_files():
            self.assertNotIn(token_name, sc_file.read_text(encoding="utf-8"), f"Token found in script {sc_file}")

        # 3. In tests, verify that the token string is NEVER used in active assertions, fixtures, or environment settings
        # (It may only appear in negative assertion tests verifying that it has been purged).
        for test_file in self._get_test_files():
            lines = test_file.read_text(encoding="utf-8").splitlines()
            for idx, line in enumerate(lines, 1):
                if token_name in line:
                    context = "\n".join(lines[max(0, idx - 4) : min(len(lines), idx + 4)])
                    is_negative_check = (
                        "assertNotIn" in context
                        or "completely removed" in context
                        or "purged" in context
                        or "Zero occurrences" in context
                        or "zero occurrences" in context
                        or "negative assertion" in context
                        or "Forbidden secret" in context
                        or "Token found" in context
                        or line.strip().startswith('token_name = "RELEASE_EVIDENCE_READ_TOKEN"')
                        or line.strip().startswith('"""')
                        or line.strip().startswith("#")
                    )
                    self.assertTrue(
                        is_negative_check,
                        f"Active test or assertion using {token_name} at {test_file.name}:{idx}: {line.strip()}",
                    )
                    self.assertNotIn("environ[", line)
                    self.assertNotIn("environ.get", line)
                    self.assertNotIn(f'"{token_name}":', line)
                    self.assertNotIn(f"'{token_name}':", line)


class WorkflowCandidateGateValidationTests(unittest.TestCase):
    """Validation tests for Workflow Candidate Gate trigger, bypass, and syntax (Ticket #55)."""

    def setUp(self):
        self.workflow_path = ROOT / ".github" / "workflows" / "python-app.yml"
        self.assertTrue(self.workflow_path.exists(), f"Workflow file not found: {self.workflow_path}")
        self.workflow_content = self.workflow_path.read_text(encoding="utf-8")
        if yaml is not None:
            self.parsed_workflow = yaml.safe_load(self.workflow_content)
        else:
            self.parsed_workflow = None

    def _get_verify_release_step(self):
        self.assertIsNotNone(self.parsed_workflow, "YAML parser required for structured workflow inspection")
        jobs = self.parsed_workflow.get("jobs", {})
        self.assertIn("verify-release", jobs, "Job verify-release must be defined")
        verify_job = jobs["verify-release"]
        steps = verify_job.get("steps", [])
        for step in steps:
            if step.get("name") == "Verify release candidate evidence":
                return step
        self.fail("Step 'Verify release candidate evidence' not found in verify-release job")

    def _create_mock_repo(self, tmp_path: pathlib.Path, base_has_control: bool = False) -> pathlib.Path:
        origin = tmp_path / "origin.git"
        work = tmp_path / "work"
        init_dir = tmp_path / "init"

        # 1. Bare origin
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)

        # 2. Seed main branch on origin
        subprocess.run(["git", "init", "-b", "main", str(init_dir)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test Committer"], cwd=init_dir, check=True)
        subprocess.run(["git", "config", "user.email", "committer@example.com"], cwd=init_dir, check=True)
        (init_dir / "README.md").write_text("# Test Repo\n", encoding="utf-8")
        if base_has_control:
            (init_dir / "docs").mkdir(parents=True, exist_ok=True)
            (init_dir / "docs" / "release-control.json").write_text("{}", encoding="utf-8")
            (init_dir / "docs" / "release-control.md").write_text("# Control Report\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=init_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init main"], cwd=init_dir, check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=init_dir, check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=init_dir, check=True, capture_output=True)

        # 3. Work directory representing the checked-out PR workspace
        subprocess.run(["git", "init", "-b", "feature", str(work)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "PR Committer"], cwd=work, check=True)
        subprocess.run(["git", "config", "user.email", "pr@example.com"], cwd=work, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=work, check=True, capture_output=True)

        # Copy scripts and docs into work directory
        shutil.copytree(ROOT / "scripts", work / "scripts")
        (work / "docs").mkdir(parents=True, exist_ok=True)
        (work / "docs" / "release-evidence.json").write_text(
            (ROOT / "docs" / "release-evidence.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (work / "docs" / "production-release-audit.md").write_text(
            (ROOT / "docs" / "production-release-audit.md").read_text(encoding="utf-8"), encoding="utf-8"
        )

        return work

    def test_workflow_full_structure_and_syntax(self):
        """Validate full YAML structure, trigger events, and job requirements in python-app.yml."""
        if yaml is not None:
            parsed = self.parsed_workflow
            self.assertIsInstance(parsed, dict)
            self.assertEqual(parsed.get("name"), "Python application")
            self.assertEqual(parsed.get("permissions"), {"contents": "read"})

            # Triggers: both push and pull_request target main
            triggers = parsed.get("on") if "on" in parsed else parsed.get(True)
            self.assertIsInstance(triggers, dict)
            self.assertIn("push", triggers)
            self.assertIn("pull_request", triggers)
            self.assertEqual(triggers["push"].get("branches"), ["main"])
            self.assertEqual(triggers["pull_request"].get("branches"), ["main"])

            # Jobs: build and verify-release
            jobs = parsed.get("jobs", {})
            self.assertIn("build", jobs)
            self.assertIn("verify-release", jobs)

            # verify-release job dependencies and permissions
            verify_job = jobs["verify-release"]
            self.assertEqual(verify_job.get("needs"), "build")
            self.assertEqual(verify_job.get("runs-on"), "ubuntu-24.04")
            self.assertEqual(
                verify_job.get("permissions"),
                {"contents": "read", "checks": "read", "pull-requests": "read"},
            )

            # Steps
            steps = verify_job.get("steps", [])
            self.assertGreaterEqual(len(steps), 3)
            self.assertEqual(
                steps[0].get("uses"),
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            )
            self.assertEqual(steps[0].get("with", {}).get("fetch-depth"), 0)
            self.assertEqual(
                steps[1].get("uses"),
                "actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0",
            )
            self.assertEqual(steps[1].get("with", {}).get("python-version"), "3.12")
            self.assertEqual(steps[2].get("name"), "Verify release candidate evidence")

    def test_workflow_verify_release_step_defines_pr_labels_expression(self):
        """Validate that verify-release step defines PR_LABELS expression and handles empty lists."""
        step = self._get_verify_release_step()
        env = step.get("env", {})
        self.assertIn("GITHUB_TOKEN", env)
        self.assertEqual(env["GITHUB_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")
        self.assertIn("PR_LABELS", env)

        expected_expr = "${{ join(github.event.pull_request.labels.*.name, ',') }}"
        self.assertEqual(
            env["PR_LABELS"].strip(),
            expected_expr,
            "PR_LABELS must define the exact join(github.event.pull_request.labels.*.name, ',') expression",
        )

        # Validate simulated evaluation across diverse event contexts
        def evaluate_labels_expr(event_data):
            pr = event_data.get("pull_request")
            if not pr:
                return ""
            labels_list = pr.get("labels")
            if not labels_list or not isinstance(labels_list, list):
                return ""
            names = [
                lbl["name"]
                for lbl in labels_list
                if isinstance(lbl, dict) and "name" in lbl and isinstance(lbl["name"], str)
            ]
            return ",".join(names)

        # Scenario 1: Multiple labels
        payload_multiple = {
            "pull_request": {
                "labels": [{"name": "bug"}, {"name": "release-candidate"}, {"name": "enhancement"}]
            }
        }
        self.assertEqual(
            evaluate_labels_expr(payload_multiple),
            "bug,release-candidate,enhancement",
        )

        # Scenario 2: Single candidate label
        payload_single = {"pull_request": {"labels": [{"name": "release-candidate"}]}}
        self.assertEqual(evaluate_labels_expr(payload_single), "release-candidate")

        # Scenario 3: Empty labels list
        payload_empty = {"pull_request": {"labels": []}}
        self.assertEqual(evaluate_labels_expr(payload_empty), "")

        # Scenario 4: Missing labels attribute
        payload_no_labels = {"pull_request": {}}
        self.assertEqual(evaluate_labels_expr(payload_no_labels), "")

        # Scenario 5: Non-PR event
        payload_non_pr = {"push": {"ref": "refs/heads/main"}}
        self.assertEqual(evaluate_labels_expr(payload_non_pr), "")

    def test_workflow_run_script_branch_invocations_and_label_forwarding(self):
        """Validate that PR branch invokes verify_release.py with --labels and non-PR branch does not."""
        step = self._get_verify_release_step()
        run_script = step.get("run", "")

        # PR conditional branch inspection
        self.assertIn('if [ -n "$GITHUB_BASE_REF" ]; then', run_script)
        self.assertIn('git fetch origin "$GITHUB_BASE_REF" --depth=1', run_script)
        self.assertIn('control_ref="$(git rev-parse FETCH_HEAD)"', run_script)
        self.assertIn(
            'python3 scripts/verify_release.py --record docs/release-control.json --report docs/release-control.md --record-ref "$control_ref" --labels "$PR_LABELS" --ci',
            run_script,
        )
        self.assertIn("exit $?", run_script)

        # Non-PR fallback branch inspection
        self.assertIn(
            'python3 scripts/verify_release.py --record docs/release-evidence.json --report docs/production-release-audit.md --ci',
            run_script,
        )
        non_pr_call = [
            line.strip()
            for line in run_script.splitlines()
            if "docs/release-evidence.json" in line
        ]
        self.assertTrue(len(non_pr_call) > 0)
        self.assertNotIn("--labels", non_pr_call[0], "Non-PR branch should not pass --labels")

    def test_workflow_zero_undefined_variables_and_syntax_lint(self):
        """Verify zero undefined expressions, valid ${{ }} syntax, and clean bash syntax in run script."""
        step = self._get_verify_release_step()
        run_script = step.get("run", "")

        # 1. Check all GitHub Actions expression references in the step
        import re
        expr_pattern = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
        raw_step_text = json.dumps(step)
        expressions = expr_pattern.findall(raw_step_text)
        expected_allowed = {
            "secrets.GITHUB_TOKEN",
            "join(github.event.pull_request.labels.*.name, ',')",
            "github.event.pull_request.head.sha",
        }
        for expr in expressions:
            self.assertIn(
                expr.strip(),
                expected_allowed,
                f"Unexpected or unverified expression: {expr}",
            )

        # Verify no unclosed ${{ or malformed expression markers
        self.assertEqual(raw_step_text.count("${{"), raw_step_text.count("}}"))

        # 2. Bash syntax check (bash -n checks syntax without executing)
        proc = subprocess.run(
            ["bash", "-n"],
            input=run_script,
            text=True,
            capture_output=True,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"bash -n failed with syntax errors: {proc.stderr}",
        )

    def test_mock_workflow_execution_unlabeled_pr_bypasses_gate(self):
        """Simulate workflow run: block for an unlabeled PR; exits 0 with bypass message."""
        step = self._get_verify_release_step()
        run_script = step.get("run", "")

        with tempfile.TemporaryDirectory() as tmp_dir:
            work = self._create_mock_repo(pathlib.Path(tmp_dir), base_has_control=False)

            for labels_val in ("", "bug", "enhancement,documentation"):
                with self.subTest(labels=labels_val):
                    env = os.environ.copy()
                    env.update({
                        "GITHUB_BASE_REF": "main",
                        "GITHUB_REF": "refs/pull/42/merge",
                        "PR_LABELS": labels_val,
                    })
                    proc = subprocess.run(
                        ["bash", "-c", run_script],
                        cwd=work,
                        env=env,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(
                        proc.returncode,
                        0,
                        f"Unlabeled PR must exit 0, got {proc.returncode}. Stderr: {proc.stderr}",
                    )
                    self.assertIn(
                        "Pull request is not marked as a release candidate; skipping release gate",
                        proc.stdout,
                    )

    def test_mock_workflow_execution_labeled_candidate_without_control_record_fails_closed(self):
        """Simulate workflow run: block for candidate PR missing control record; fails closed with exit 1."""
        step = self._get_verify_release_step()
        run_script = step.get("run", "")

        with tempfile.TemporaryDirectory() as tmp_dir:
            work = self._create_mock_repo(pathlib.Path(tmp_dir), base_has_control=False)
            env = os.environ.copy()
            env.update({
                "GITHUB_BASE_REF": "main",
                "GITHUB_REF": "refs/pull/42/merge",
                "PR_LABELS": "release-candidate",
            })
            proc = subprocess.run(
                ["bash", "-c", run_script],
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                proc.returncode,
                1,
                f"Candidate PR without control record must fail closed with exit 1, got {proc.returncode}",
            )
            combined_output = proc.stdout + proc.stderr
            self.assertIn("Publish the trusted release control record on the base branch:", combined_output)
            self.assertIn("git checkout main", combined_output)
            self.assertIn("git pull origin main", combined_output)
            self.assertIn("git add docs/release-control.json docs/release-control.md", combined_output)
            self.assertIn("git push origin main", combined_output)
            self.assertIn("Verdict: NOT READY", combined_output)

    def test_mock_workflow_execution_non_pr_event_bypasses_gate(self):
        """Simulate workflow run: block for non-PR event (push to main); exits 0 with non-PR notice."""
        step = self._get_verify_release_step()
        run_script = step.get("run", "")

        with tempfile.TemporaryDirectory() as tmp_dir:
            work = self._create_mock_repo(pathlib.Path(tmp_dir), base_has_control=False)

            # Case 1: GITHUB_BASE_REF is empty string
            env1 = os.environ.copy()
            env1.update({
                "GITHUB_BASE_REF": "",
                "GITHUB_REF": "refs/heads/main",
                "PR_LABELS": "",
            })
            proc1 = subprocess.run(
                ["bash", "-c", run_script],
                cwd=work,
                env=env1,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc1.returncode, 0, f"Non-PR event must exit 0. Stderr: {proc1.stderr}")
            self.assertIn(
                "CI ref 'refs/heads/main' is not a pull request event; skipping release gate.",
                proc1.stdout,
            )

            # Case 2: GITHUB_BASE_REF is unset
            env2 = os.environ.copy()
            env2.pop("GITHUB_BASE_REF", None)
            env2.update({
                "GITHUB_REF": "refs/heads/main",
                "PR_LABELS": "",
            })
            proc2 = subprocess.run(
                ["bash", "-c", run_script],
                cwd=work,
                env=env2,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc2.returncode, 0, f"Unset base ref must exit 0. Stderr: {proc2.stderr}")
            self.assertIn(
                "CI ref 'refs/heads/main' is not a pull request event; skipping release gate.",
                proc2.stdout,
            )


class ReleaseCandidateGateLabelInspectionTests(unittest.TestCase):
    """Tests for PR label inspection and unmarked PR bypass in release verifier (Ticket #48)."""

    def setUp(self):
        try:
            import verify_release
            self.mod = verify_release
        except ImportError:
            import scripts.verify_release as vr
            self.mod = vr

    def test_cli_parsing_labels_argument_in_argparse(self):
        """Verify --labels flag is recognized by add_release_verification_arguments."""
        import argparse
        parser = argparse.ArgumentParser()
        self.mod.add_release_verification_arguments(parser)
        args = parser.parse_args(["--labels", "bug,release-candidate"])
        self.assertEqual(args.labels, "bug,release-candidate")

    def test_cli_parsing_labels_in_build_parser(self):
        """Verify --labels is recognized by cli.build_parser."""
        from scripts.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["verify-release", "--labels", "documentation,release-candidate"])
        self.assertEqual(args.labels, "documentation,release-candidate")

    def test_ci_mode_non_pr_ref_returns_zero_with_bypass_notice(self):
        """If GITHUB_REF is not a pull request, return 0 with bypass notice without reading record."""
        out = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_REF": "refs/heads/main"}):
            exit_code = self.mod.run_verify_release(
                record_path="nonexistent-record.json",
                record_ref="a" * 40,
                ci_mode=True,
                labels=["release-candidate"],
                out_stream=out,
            )
        self.assertEqual(exit_code, 0)
        output_str = out.getvalue()
        self.assertIn("not a pull request event", output_str)
        self.assertIn("skipping release gate", output_str)

    def test_ci_mode_unmarked_pr_without_labels_returns_zero_and_bypasses(self):
        """If labels is None/empty on PR event, print bypass notice and return 0 without reading record."""
        for empty_labels in [None, [], ""]:
            out = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/42/merge"}):
                exit_code = self.mod.run_verify_release(
                    record_path="nonexistent-record-path.json",
                    record_ref="a" * 40,
                    ci_mode=True,
                    labels=empty_labels,
                    out_stream=out,
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                out.getvalue(),
                "Pull request is not marked as a release candidate; skipping release gate\n",
            )

    def test_ci_mode_unmarked_pr_with_other_labels_returns_zero_and_bypasses(self):
        """If labels does not contain release-candidate, print bypass notice and return 0."""
        for labels in [["bug", "documentation"], "bug,documentation"]:
            out = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/42/merge"}):
                exit_code = self.mod.run_verify_release(
                    record_path="nonexistent-record-path.json",
                    record_ref="a" * 40,
                    ci_mode=True,
                    labels=labels,
                    out_stream=out,
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                out.getvalue(),
                "Pull request is not marked as a release candidate; skipping release gate\n",
            )

    def test_ci_mode_marked_candidate_does_not_bypass(self):
        """If release-candidate is in labels, do NOT bypass at this stage and proceed to verification."""
        for labels in [["release-candidate"], "bug,release-candidate", "Release-Candidate"]:
            out = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/42/merge"}):
                # When record_path does not exist, verification proceeds to read record and fails with 1
                exit_code = self.mod.run_verify_release(
                    record_path="nonexistent-record-path.json",
                    ci_mode=True,
                    labels=labels,
                    out_stream=out,
                )
            self.assertEqual(exit_code, 1)
            self.assertNotIn("Pull request is not marked as a release candidate", out.getvalue())

    def test_cli_main_verify_release_forwards_labels_and_bypasses_in_ci(self):
        """Test cli.py dispatcher forwards --labels and bypasses unmarked PRs in CI mode."""
        from scripts.cli import main
        out = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/99/merge"}), redirect_stdout(out):
            exit_code = main(["verify-release", "--ci", "--labels", "bug,enhancement", "--record", "nonexistent.json"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Pull request is not marked as a release candidate", out.getvalue())


class FailClosedControlRecordVerificationTests(unittest.TestCase):
    """Unit tests for Ticket #50: fail-closed verification for missing or mismatched base control records."""

    def setUp(self):
        try:
            import verify_release
            self.mod = verify_release
        except ImportError:
            import scripts.verify_release as vr
            self.mod = vr

    def _sample_valid_record(self, pr=23, head_sha="9f4bb810a88060e48234538f7205224f0bee5659", status="active"):
        return {
            "version": 1,
            "status": status,
            "candidate": {
                "repository": "lispz1610/sciflow-cicd",
                "pull_request": pr,
                "target_version": "0.1.0",
                "head_sha": head_sha,
            },
            "required_gates": {
                "python_versions": ["3.10", "3.11", "3.12"],
            },
            "live_evidence": {
                "runtime_fingerprint": "18b032e3e2e846458c953fb5445c8471e674454797183ad8113f8c53418a26bc",
            },
        }

    def test_missing_control_record_at_record_ref_fails_closed_with_recovery_commands(self):
        """Labeled candidate without docs/release-control.json at record_ref returns exit 1 with recovery commands."""
        out = io.StringIO()
        err = io.StringIO()
        record_ref = "1111111111111111111111111111111111111111"
        with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/42/merge"}):
            exit_code = self.mod.run_verify_release(
                record_path="docs/release-control.json",
                record_ref=record_ref,
                ci_mode=True,
                labels=["release-candidate"],
                out_stream=out,
                err_stream=err,
            )
        self.assertEqual(exit_code, 1)
        combined = out.getvalue() + err.getvalue()
        self.assertIn(record_ref, combined)
        self.assertIn("git checkout", combined)
        self.assertIn("git add", combined)
        self.assertIn("git commit", combined)
        self.assertIn("git push", combined)

    def test_missing_control_record_json_format_contains_recovery_commands(self):
        """In JSON format, missing record at record_ref returns exit 1 with formatted recovery commands."""
        out = io.StringIO()
        err = io.StringIO()
        record_ref = "1111111111111111111111111111111111111111"
        with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/42/merge"}):
            exit_code = self.mod.run_verify_release(
                record_path="docs/release-control.json",
                record_ref=record_ref,
                ci_mode=True,
                labels=["release-candidate"],
                output_format="json",
                out_stream=out,
                err_stream=err,
            )
        self.assertEqual(exit_code, 1)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["verdict"], "not ready")
        recovery = payload["recovery"]
        self.assertIn("git checkout", recovery)
        self.assertIn("git add", recovery)
        self.assertIn("git commit", recovery)
        self.assertIn("git push", recovery)

    def test_recovery_commands_respect_custom_base_ref_and_pr_number(self):
        """Recovery commands customize base branch from GITHUB_BASE_REF and PR number from GITHUB_REF."""
        out = io.StringIO()
        err = io.StringIO()
        record_ref = "2222222222222222222222222222222222222222"
        with patch.dict(os.environ, {
            "GITHUB_BASE_REF": "release-v1",
            "GITHUB_REF": "refs/pull/88/merge",
        }):
            exit_code = self.mod.run_verify_release(
                record_path="docs/release-control.json",
                record_ref=record_ref,
                ci_mode=True,
                labels=["release-candidate"],
                out_stream=out,
                err_stream=err,
            )
        self.assertEqual(exit_code, 1)
        combined = out.getvalue() + err.getvalue()
        self.assertIn("git checkout release-v1", combined)
        self.assertIn("git push origin release-v1", combined)
        self.assertIn("PR #88", combined)

    def test_labeled_candidate_mismatched_pr_number_fails_closed_with_expected_vs_observed(self):
        """Labeled candidate whose PR number does not match candidate['pull_request'] returns exit 1."""
        rec = self._sample_valid_record(pr=23)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(rec, f)
            rec_path = f.name
        try:
            out = io.StringIO()
            err = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/42/merge"}):
                exit_code = self.mod.run_verify_release(
                    record_path=rec_path,
                    ci_mode=True,
                    labels=["release-candidate"],
                    out_stream=out,
                    err_stream=err,
                )
            self.assertEqual(exit_code, 1)
            combined = out.getvalue() + err.getvalue()
            self.assertIn("expected PR #23", combined)
            self.assertIn("observed PR #42", combined)
            self.assertNotIn("skipping release gate", combined)
        finally:
            if os.path.exists(rec_path):
                os.remove(rec_path)

    def test_labeled_candidate_mismatched_pr_number_in_json_format(self):
        """Labeled candidate PR mismatch in JSON format returns verdict not ready and diagnostic reason."""
        rec = self._sample_valid_record(pr=23)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(rec, f)
            rec_path = f.name
        try:
            out = io.StringIO()
            err = io.StringIO()
            with patch.dict(os.environ, {
                "GITHUB_PR_NUMBER": "77",
                "GITHUB_REF": "refs/pull/77/merge",
            }):
                exit_code = self.mod.run_verify_release(
                    record_path=rec_path,
                    ci_mode=True,
                    labels=["release-candidate"],
                    output_format="json",
                    out_stream=out,
                    err_stream=err,
                )
            self.assertEqual(exit_code, 1)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["verdict"], "not ready")
            self.assertIn("expected PR #23", payload["reason"])
            self.assertIn("observed PR #77", payload["reason"])
        finally:
            if os.path.exists(rec_path):
                os.remove(rec_path)

    def test_labeled_candidate_mismatched_head_sha_fails_closed_with_expected_vs_observed(self):
        """Labeled candidate whose head commit SHA does not match record declared head_sha returns exit 1."""
        rec = self._sample_valid_record(head_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(rec, f)
            rec_path = f.name
        try:
            out = io.StringIO()
            err = io.StringIO()
            observed_sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            with patch.dict(os.environ, {
                "GITHUB_REF": "refs/pull/23/merge",
                "GITHUB_SHA": observed_sha,
                "GITHUB_HEAD_SHA": observed_sha,
            }):
                exit_code = self.mod.run_verify_release(
                    record_path=rec_path,
                    ci_mode=True,
                    labels=["release-candidate"],
                    out_stream=out,
                    err_stream=err,
                )
            self.assertEqual(exit_code, 1)
            combined = out.getvalue() + err.getvalue()
            self.assertIn("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", combined)
            self.assertIn("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", combined)
            self.assertIn("expected commit SHA", combined)
            self.assertIn("observed commit SHA", combined)
        finally:
            if os.path.exists(rec_path):
                os.remove(rec_path)

    def test_labeled_candidate_archived_record_still_skips_in_ci(self):
        """Archived records for historical candidate PRs still skip in CI with code 0."""
        rec = self._sample_valid_record(pr=23, status="archived")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(rec, f)
            rec_path = f.name
        try:
            out = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/23/merge"}):
                exit_code = self.mod.run_verify_release(
                    record_path=rec_path,
                    ci_mode=True,
                    labels=["release-candidate"],
                    out_stream=out,
                )
            self.assertEqual(exit_code, 0)
            self.assertIn("archived for historical candidate PR #23", out.getvalue())
        finally:
            if os.path.exists(rec_path):
                os.remove(rec_path)

    def test_output_streams_strictly_sanitize_secret_tokens(self):
        """Output streams strictly sanitize credentials in error and recovery diagnostics."""
        secret_token = "ghp_super_secret_github_personal_token_12345"
        out = io.StringIO()
        err = io.StringIO()
        with patch.dict(os.environ, {
            "GITHUB_REF": f"refs/pull/42/{secret_token}/merge",
        }):
            exit_code = self.mod.run_verify_release(
                record_path=f"docs/{secret_token}.json",
                record_ref="1111111111111111111111111111111111111111",
                ci_mode=True,
                labels=["release-candidate"],
                out_stream=out,
                err_stream=err,
            )
        self.assertEqual(exit_code, 1)
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn(secret_token, combined)
        self.assertIn("[REDACTED]", combined)

    def test_control_record_strictly_extracted_from_record_ref_never_from_untrusted_candidate_files(self):
        """Verifier extracts control record strictly from base record_ref, never reading untrusted candidate branch files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = pathlib.Path(tmp_dir)

            def git(*cmd):
                return subprocess.check_output(
                    ["git", *cmd], cwd=tmp_root, text=True, stderr=subprocess.DEVNULL
                ).strip()

            git("init", "-b", "main")
            git("config", "user.name", "Test Committer")
            git("config", "user.email", "committer@example.com")
            (tmp_root / "sciflow-cicd").write_text("#!/usr/bin/env python3\n")
            git("add", "sciflow-cicd")
            git("commit", "-m", "init base commit")
            base_empty_sha = git("rev-parse", "HEAD")

            # Create trusted base control record on base commit
            (tmp_root / "docs").mkdir()
            trusted_record = self._sample_valid_record(pr=23)
            (tmp_root / "docs" / "release-control.json").write_text(json.dumps(trusted_record))
            git("add", "docs/release-control.json")
            git("commit", "-m", "publish trusted control record on main")
            base_trusted_sha = git("rev-parse", "HEAD")

            # Switch to candidate branch and commit untrusted/tampered control record claiming PR #999
            git("checkout", "-b", "candidate-branch")
            untrusted_record = self._sample_valid_record(pr=999)
            (tmp_root / "docs" / "release-control.json").write_text(json.dumps(untrusted_record))
            git("add", "docs/release-control.json")
            git("commit", "-m", "untrusted control record in candidate tree")

            # Scenario A: When record_ref is base_empty_sha (no record at base),
            # even though docs/release-control.json exists in candidate checkout,
            # verifier MUST fail closed and NOT use candidate's untrusted file.
            out_a = io.StringIO()
            err_a = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/999/merge"}):
                exit_code_a = self.mod.run_verify_release(
                    record_path="docs/release-control.json",
                    record_ref=base_empty_sha,
                    ci_mode=True,
                    labels=["release-candidate"],
                    repo_root=tmp_root,
                    out_stream=out_a,
                    err_stream=err_a,
                )
            self.assertEqual(exit_code_a, 1)
            combined_a = out_a.getvalue() + err_a.getvalue()
            self.assertIn("unavailable at commit", combined_a)
            self.assertIn("git checkout", combined_a)

            # Scenario B: When record_ref is base_trusted_sha (trusted PR #23),
            # running for PR #999 must detect PR mismatch with trusted base record (expected PR #23, observed PR #999),
            # proving it read the base record and NOT the untrusted workspace record (which has PR #999).
            out_b = io.StringIO()
            err_b = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/999/merge"}):
                exit_code_b = self.mod.run_verify_release(
                    record_path="docs/release-control.json",
                    record_ref=base_trusted_sha,
                    ci_mode=True,
                    labels=["release-candidate"],
                    repo_root=tmp_root,
                    out_stream=out_b,
                    err_stream=err_b,
                )
            self.assertEqual(exit_code_b, 1)
            combined_b = out_b.getvalue() + err_b.getvalue()
            self.assertIn("expected PR #23", combined_b)
            self.assertIn("observed PR #999", combined_b)

    def test_observed_labels_included_in_failure_output(self):
        """Observed labels are presented in failure output in text mode and JSON mode."""
        rec = self._sample_valid_record(pr=23)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(rec, f)
            rec_path = f.name
        try:
            # 1. Text mode
            out_text = io.StringIO()
            err_text = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/99/merge"}):
                exit_code_text = self.mod.run_verify_release(
                    record_path=rec_path,
                    ci_mode=True,
                    labels=["release-candidate", "review-pending"],
                    out_stream=out_text,
                    err_stream=err_text,
                )
            self.assertEqual(exit_code_text, 1)
            combined_text = out_text.getvalue() + err_text.getvalue()
            self.assertIn("Observed labels: ['release-candidate', 'review-pending']", combined_text)

            # 2. JSON mode
            out_json = io.StringIO()
            err_json = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": "refs/pull/99/merge"}):
                exit_code_json = self.mod.run_verify_release(
                    record_path=rec_path,
                    ci_mode=True,
                    labels=["release-candidate", "review-pending"],
                    output_format="json",
                    out_stream=out_json,
                    err_stream=err_json,
                )
            self.assertEqual(exit_code_json, 1)
            payload = json.loads(out_json.getvalue())
            self.assertEqual(payload["observed"]["labels"], ["release-candidate", "review-pending"])
            self.assertTrue(
                any(
                    isinstance(x, dict) and x.get("labels") == ["release-candidate", "review-pending"]
                    for x in payload["evidence"]["observed"]
                )
            )
        finally:
            if os.path.exists(rec_path):
                os.remove(rec_path)


class LabeledCandidateCIMatrixAndFingerprintGateTests(unittest.TestCase):
    """Unit tests for verifying CI matrix runs, runtime fingerprint, report alignment, and GO verdict (Ticket #52)."""

    def setUp(self):
        try:
            import verify_release
            self.mod = verify_release
        except ImportError:
            import scripts.verify_release as vr
            self.mod = vr
        self.candidate_repo = "lispz1610/sciflow-cicd"
        self.candidate_pr = 23

    def _create_git_repo_fixture(self):
        tmp_dir = tempfile.TemporaryDirectory()
        tmp_root = pathlib.Path(tmp_dir.name)

        def git(*cmd):
            return subprocess.check_output(
                ["git", *cmd], cwd=tmp_root, text=True, stderr=subprocess.DEVNULL
            ).strip()

        git("init", "-b", "main")
        git("config", "user.name", "Test Committer")
        git("config", "user.email", "committer@example.com")
        (tmp_root / "sciflow-cicd").write_text("#!/usr/bin/env python3\nprint('sciflow-cicd')\n")
        (tmp_root / "scripts").mkdir()
        (tmp_root / "scripts" / "cli.py").write_text("print('cli')\n")
        git("add", ".")
        git("commit", "-m", "candidate head commit")
        candidate_head = git("rev-parse", "HEAD")

        computed_fp = self.mod.compute_runtime_fingerprint(tmp_root, commit_sha=candidate_head)

        record = {
            "version": 1,
            "status": "active",
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
                "runtime_fingerprint": computed_fp,
            },
            "operational_constraints": [],
            "unverified_scenarios": [],
        }

        report_content = (
            f"# Release Control Report\n\n"
            f"**Pull Request:** [#{self.candidate_pr}](https://github.com/{self.candidate_repo}/pull/{self.candidate_pr})\n"
            f"**Candidate Head SHA:** `{candidate_head}`\n"
            f"**Target Version:** `0.1.0`\n"
            f"**Current Release Verdict:** **READY**\n"
        )

        mock_api = MockReadOnlyGitHubApi({
            f"/repos/{self.candidate_repo}/pulls/{self.candidate_pr}": {
                "number": self.candidate_pr,
                "base": {"repo": {"full_name": self.candidate_repo}},
                "head": {"sha": candidate_head},
                "state": "open",
            },
            f"/repos/{self.candidate_repo}/commits/{candidate_head}/check-runs": {
                "total_count": 3,
                "check_runs": [
                    {"name": "build (3.10)", "status": "completed", "conclusion": "success", "head_sha": candidate_head},
                    {"name": "build (3.11)", "status": "completed", "conclusion": "success", "head_sha": candidate_head},
                    {"name": "build (3.12)", "status": "completed", "conclusion": "success", "head_sha": candidate_head},
                ],
            },
        })

        return tmp_dir, tmp_root, candidate_head, computed_fp, record, report_content, mock_api

    def test_labeled_candidate_full_success_emits_verdict_go_in_text_mode(self):
        """On full verification success for a labeled candidate, emit VERDICT: GO and exit 0 in text mode."""
        tmp_dir, tmp_root, cand_head, fp, record, report_content, mock_api = self._create_git_repo_fixture()
        with tmp_dir:
            docs = tmp_root / "docs"
            docs.mkdir(exist_ok=True)
            (docs / "release-control.json").write_text(json.dumps(record))
            (docs / "release-control.md").write_text(report_content)

            out = io.StringIO()
            err = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code = self.mod.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="text",
                    out_stream=out,
                    err_stream=err,
                )
            self.assertEqual(exit_code, 0, f"Failed: {out.getvalue()} {err.getvalue()}")
            text_output = out.getvalue()
            self.assertIn("VERDICT: GO", text_output)
            self.assertIn("All release gates verified", text_output)

    def test_labeled_candidate_full_success_emits_go_verdict_in_json_mode(self):
        """In JSON mode for a labeled candidate, full success outputs structured object with verdict 'GO'."""
        tmp_dir, tmp_root, cand_head, fp, record, report_content, mock_api = self._create_git_repo_fixture()
        with tmp_dir:
            docs = tmp_root / "docs"
            docs.mkdir(exist_ok=True)
            (docs / "release-control.json").write_text(json.dumps(record))
            (docs / "release-control.md").write_text(report_content)

            out = io.StringIO()
            err = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code = self.mod.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="json",
                    out_stream=out,
                    err_stream=err,
                )
            self.assertEqual(exit_code, 0, f"Failed: {out.getvalue()} {err.getvalue()}")
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["verdict"], "GO")
            self.assertEqual(payload.get("gate_verdict"), "GO")
            self.assertTrue(len(payload["evidence"]["observed"]) >= 3)
            self.assertIn("All release gates verified", payload["reason"])

    def test_labeled_candidate_failing_ci_check_exits_one_with_diagnostics(self):
        """Failing CI check run causes exit code 1 with diagnostic output naming the failing job."""
        tmp_dir, tmp_root, cand_head, fp, record, report_content, mock_api = self._create_git_repo_fixture()
        with tmp_dir:
            # Fail Python 3.12 job
            mock_api.responses[f"/repos/{self.candidate_repo}/commits/{cand_head}/check-runs"] = {
                "total_count": 3,
                "check_runs": [
                    {"name": "build (3.10)", "status": "completed", "conclusion": "success", "head_sha": cand_head},
                    {"name": "build (3.11)", "status": "completed", "conclusion": "success", "head_sha": cand_head},
                    {"name": "build (3.12)", "status": "completed", "conclusion": "failure", "head_sha": cand_head},
                ],
            }
            docs = tmp_root / "docs"
            docs.mkdir(exist_ok=True)
            (docs / "release-control.json").write_text(json.dumps(record))
            (docs / "release-control.md").write_text(report_content)

            out = io.StringIO()
            err = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code = self.mod.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="text",
                    out_stream=out,
                    err_stream=err,
                )
            self.assertEqual(exit_code, 1)
            combined = out.getvalue() + err.getvalue()
            self.assertIn("Verdict: NOT READY", combined)
            self.assertNotIn("VERDICT: GO", combined)
            self.assertIn("build (3.12)", combined)
            self.assertIn("failed with conclusion 'failure'", combined)

    def test_labeled_candidate_pending_ci_check_exits_one_with_diagnostics(self):
        """Pending or in_progress CI check causes exit code 1 with diagnostic output."""
        tmp_dir, tmp_root, cand_head, fp, record, report_content, mock_api = self._create_git_repo_fixture()
        with tmp_dir:
            mock_api.responses[f"/repos/{self.candidate_repo}/commits/{cand_head}/check-runs"] = {
                "total_count": 3,
                "check_runs": [
                    {"name": "build (3.10)", "status": "in_progress", "conclusion": None, "head_sha": cand_head},
                    {"name": "build (3.11)", "status": "completed", "conclusion": "success", "head_sha": cand_head},
                    {"name": "build (3.12)", "status": "completed", "conclusion": "success", "head_sha": cand_head},
                ],
            }
            docs = tmp_root / "docs"
            docs.mkdir(exist_ok=True)
            (docs / "release-control.json").write_text(json.dumps(record))
            (docs / "release-control.md").write_text(report_content)

            out = io.StringIO()
            err = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code = self.mod.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="text",
                    out_stream=out,
                    err_stream=err,
                )
            self.assertEqual(exit_code, 1)
            combined = out.getvalue() + err.getvalue()
            self.assertIn("Verdict: NOT READY", combined)
            self.assertIn("build (3.10)", combined)
            self.assertIn("status 'in_progress'", combined)

    def test_labeled_candidate_missing_ci_check_version_exits_one(self):
        """Missing CI check run for a required Python matrix version causes exit code 1."""
        tmp_dir, tmp_root, cand_head, fp, record, report_content, mock_api = self._create_git_repo_fixture()
        with tmp_dir:
            mock_api.responses[f"/repos/{self.candidate_repo}/commits/{cand_head}/check-runs"] = {
                "total_count": 2,
                "check_runs": [
                    {"name": "build (3.10)", "status": "completed", "conclusion": "success", "head_sha": cand_head},
                    {"name": "build (3.11)", "status": "completed", "conclusion": "success", "head_sha": cand_head},
                ],
            }
            docs = tmp_root / "docs"
            docs.mkdir(exist_ok=True)
            (docs / "release-control.json").write_text(json.dumps(record))
            (docs / "release-control.md").write_text(report_content)

            out = io.StringIO()
            err = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code = self.mod.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="text",
                    out_stream=out,
                    err_stream=err,
                )
            self.assertEqual(exit_code, 1)
            combined = out.getvalue() + err.getvalue()
            self.assertIn("Required CI job for Python 3.12 is missing", combined)

    def test_labeled_candidate_ci_check_head_sha_mismatch_exits_one(self):
        """CI check run on a different head commit SHA causes exit code 1."""
        tmp_dir, tmp_root, cand_head, fp, record, report_content, mock_api = self._create_git_repo_fixture()
        with tmp_dir:
            stale_sha = "0000000000000000000000000000000000000000"
            mock_api.responses[f"/repos/{self.candidate_repo}/commits/{cand_head}/check-runs"] = {
                "total_count": 3,
                "check_runs": [
                    {"name": "build (3.10)", "status": "completed", "conclusion": "success", "head_sha": stale_sha},
                    {"name": "build (3.11)", "status": "completed", "conclusion": "success", "head_sha": cand_head},
                    {"name": "build (3.12)", "status": "completed", "conclusion": "success", "head_sha": cand_head},
                ],
            }
            docs = tmp_root / "docs"
            docs.mkdir(exist_ok=True)
            (docs / "release-control.json").write_text(json.dumps(record))
            (docs / "release-control.md").write_text(report_content)

            out = io.StringIO()
            err = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code = self.mod.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="text",
                    out_stream=out,
                    err_stream=err,
                )
            self.assertEqual(exit_code, 1)
            combined = out.getvalue() + err.getvalue()
            self.assertIn("head SHA mismatch", combined)

    def test_labeled_candidate_runtime_fingerprint_mismatch_causes_exit_one(self):
        """Runtime fingerprint mismatch against candidate head commit causes exit code 1."""
        tmp_dir, tmp_root, cand_head, fp, record, report_content, mock_api = self._create_git_repo_fixture()
        with tmp_dir:
            # Declare a different fingerprint
            record["live_evidence"]["runtime_fingerprint"] = "f" * 64
            docs = tmp_root / "docs"
            docs.mkdir(exist_ok=True)
            (docs / "release-control.json").write_text(json.dumps(record))
            (docs / "release-control.md").write_text(report_content)

            out = io.StringIO()
            err = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code = self.mod.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="text",
                    out_stream=out,
                    err_stream=err,
                )
            self.assertEqual(exit_code, 1)
            combined = out.getvalue() + err.getvalue()
            self.assertIn("Runtime fingerprint mismatch", combined)
            self.assertIn("Recompute runtime fingerprint", combined)
            self.assertNotIn("VERDICT: GO", combined)

    def test_labeled_candidate_report_alignment_verified(self):
        """Report alignment verifies candidate PR, commit SHA, target version, and verdict."""
        tmp_dir, tmp_root, cand_head, fp, record, report_content, mock_api = self._create_git_repo_fixture()
        with tmp_dir:
            # 1. Target version mismatch in report
            bad_ver_report = report_content.replace("**Target Version:** `0.1.0`", "**Target Version:** `0.2.0`")
            docs = tmp_root / "docs"
            docs.mkdir(exist_ok=True)
            (docs / "release-control.json").write_text(json.dumps(record))
            (docs / "release-control.md").write_text(bad_ver_report)

            out = io.StringIO()
            err = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code = self.mod.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="text",
                    out_stream=out,
                    err_stream=err,
                )
            self.assertEqual(exit_code, 1)
            combined = out.getvalue() + err.getvalue()
            self.assertIn("Candidate target version mismatch", combined)

            # 2. Candidate head SHA mismatch in report
            bad_sha_report = report_content.replace(cand_head, "1" * 40)
            (docs / "release-control.md").write_text(bad_sha_report)
            out = io.StringIO()
            err = io.StringIO()
            with patch.dict(os.environ, {"GITHUB_REF": f"refs/pull/{self.candidate_pr}/merge"}):
                exit_code = self.mod.run_verify_release(
                    record_path="docs/release-control.json",
                    report_path="docs/release-control.md",
                    repo_root=tmp_root,
                    ci_mode=True,
                    labels=["release-candidate"],
                    api=mock_api,
                    output_format="text",
                    out_stream=out,
                    err_stream=err,
                )
            self.assertEqual(exit_code, 1)
            combined = out.getvalue() + err.getvalue()
            self.assertIn("candidate head SHA is missing or stale", combined)

    def test_validate_runtime_fingerprint_and_verify_report_alignment_helpers(self):
        """Direct unit test of validate_runtime_fingerprint and verify_report_alignment functions."""
        tmp_dir, tmp_root, cand_head, fp, record, report_content, mock_api = self._create_git_repo_fixture()
        with tmp_dir:
            # Test validate_runtime_fingerprint matching
            res_valid = self.mod.validate_runtime_fingerprint(
                tmp_root, declared_fingerprint=fp, commit_sha=cand_head
            )
            self.assertTrue(res_valid["valid"])
            self.assertEqual(res_valid["computed_fingerprint"], fp)

            # Test validate_runtime_fingerprint mismatch
            res_invalid = self.mod.validate_runtime_fingerprint(
                tmp_root, declared_fingerprint="0" * 64, commit_sha=cand_head
            )
            self.assertFalse(res_invalid["valid"])
            self.assertIn("Runtime fingerprint mismatch", res_invalid["reason"])

            # Test validate_runtime_fingerprint unavailable commit
            res_bad_sha = self.mod.validate_runtime_fingerprint(
                tmp_root, declared_fingerprint=fp, commit_sha="e" * 40
            )
            self.assertFalse(res_bad_sha["valid"])
            self.assertIn("unavailable in the local Git checkout", res_bad_sha["reason"])

            # Test verify_report_alignment directly
            rep_path = tmp_root / "report.md"
            rep_path.write_text(report_content)
            align_res = self.mod.verify_report_alignment(
                report_path=str(rep_path),
                candidate=record["candidate"],
                verified_verdict="ready",
            )
            self.assertTrue(align_res["valid"])

    def test_local_candidate_run_with_release_control_evaluates_candidate_and_outputs_verdict_go(self):
        """Invoking locally with release-control.json evaluates candidate and emits VERDICT: GO on success."""
        tmp_dir, tmp_root, cand_head, fp, record, report_content, mock_api = self._create_git_repo_fixture()
        with tmp_dir:
            docs = tmp_root / "docs"
            docs.mkdir(exist_ok=True)
            (docs / "release-control.json").write_text(json.dumps(record))
            (docs / "release-control.md").write_text(report_content)

            out = io.StringIO()
            err = io.StringIO()
            # Local run: no ci_mode, no labels passed
            exit_code = self.mod.run_verify_release(
                record_path=str(docs / "release-control.json"),
                report_path=str(docs / "release-control.md"),
                repo_root=tmp_root,
                ci_mode=False,
                labels=None,
                api=mock_api,
                output_format="text",
                out_stream=out,
                err_stream=err,
            )
            self.assertEqual(exit_code, 0, f"Failed: {out.getvalue()} {err.getvalue()}")
            text_out = out.getvalue()
            self.assertIn("VERDICT: GO", text_out)
            self.assertNotIn("Verdict: READY", text_out)


class DocumentationIntegrityAndRegressionTests(unittest.TestCase):
    """Regression tests for documentation link resolution, glossary integrity, and deliverables (#57)."""

    def setUp(self):
        try:
            import verify_release
            self.mod = verify_release
        except ImportError:
            import scripts.verify_release as vr
            self.mod = vr

    def test_markdown_relative_links_in_docs_and_root_resolve_correctly(self):
        """Verify that all relative markdown links across docs/ and root markdown files resolve to existing files."""
        md_files = [p for p in ROOT.glob("**/*.md") if ".git" not in p.parts]
        self.assertGreater(len(md_files), 10, "Expected multiple markdown files in repository")

        inline_code_pattern = re.compile(r"`[^`]+`")
        link_pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

        broken_links = []
        total_links_checked = 0

        for md_file in md_files:
            content = md_file.read_text(encoding="utf-8")
            lines = content.splitlines()
            in_code_block = False

            for line_no, line in enumerate(lines, 1):
                if line.strip().startswith("```"):
                    in_code_block = not in_code_block
                    continue
                if in_code_block:
                    continue

                # Strip inline code spans to avoid treating example links inside backticks as active links
                cleaned_line = inline_code_pattern.sub("", line)

                for match in link_pattern.finditer(cleaned_line):
                    _, target = match.groups()
                    target = target.strip()
                    # Skip external schemes, mailto, and in-page anchor-only links
                    if target.startswith(("http://", "https://", "mailto:")) or target.startswith("#"):
                        continue

                    total_links_checked += 1
                    # Handle path with anchor (e.g. "path/to/file.md#section")
                    target_file_part = target.split("#")[0]
                    if not target_file_part:
                        continue

                    # Check file existence relative to referencing document
                    resolved_path = (md_file.parent / target_file_part).resolve()
                    if not resolved_path.exists():
                        rel_source = md_file.relative_to(ROOT).as_posix()
                        broken_links.append((rel_source, line_no, target, resolved_path.as_posix()))

        self.assertGreater(total_links_checked, 20, f"Expected many relative links, checked {total_links_checked}")
        self.assertEqual(
            broken_links,
            [],
            f"Found {len(broken_links)} broken relative markdown links: {broken_links}",
        )

    def test_glossary_canonical_terms_defined_and_used_consistently(self):
        """Verify that GLOSSARY.md defines required canonical terms and Spec 2 docs use them consistently."""
        glossary_path = ROOT / "GLOSSARY.md"
        self.assertTrue(glossary_path.exists(), "Root GLOSSARY.md must exist")
        glossary_content = glossary_path.read_text(encoding="utf-8")

        canonical_terms = [
            "Interactive Setup",
            "Headless Mode",
            "Policy",
            "Inventory",
            "Resume State",
            "Dry-Run",
            "Advisory Review",
            "Review Setup",
            "Setup Pull Request",
            "Review Run",
            "Release Candidate",
            "Release Gate",
            "Control Record",
            "Runtime Fingerprint",
        ]
        for term in canonical_terms:
            self.assertIn(
                f"**{term}**",
                glossary_content,
                f"Canonical term '{term}' must be formally defined in GLOSSARY.md",
            )

        # Verify docs/agents/domain.md references GLOSSARY.md and docs/adr/
        domain_doc = ROOT / "docs" / "agents" / "domain.md"
        self.assertTrue(domain_doc.exists(), "docs/agents/domain.md must exist")
        domain_content = domain_doc.read_text(encoding="utf-8")
        self.assertIn("GLOSSARY.md", domain_content)
        self.assertIn("docs/adr/", domain_content)

        # Verify Spec 2 lifecycle documentation uses canonical terms
        rel_evidence = (ROOT / "docs" / "release-evidence.md").read_text(encoding="utf-8")
        for term in ["Release Candidate", "Release Gate", "Control Record", "Runtime Fingerprint"]:
            self.assertIn(
                term,
                rel_evidence,
                f"docs/release-evidence.md must use canonical term '{term}'",
            )

        # Verify ADR 0003 references core Spec 2 concepts
        adr_content = (ROOT / "docs" / "adr" / "0003-self-contained-release-candidate-verification.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("release candidate", adr_content.lower())
        self.assertIn("control record", adr_content.lower())
        self.assertIn("runtime fingerprint", adr_content.lower())

    def test_python_scripts_and_tests_compile_without_syntax_errors(self):
        """Ensure all Python scripts and tests compile cleanly with py_compile."""
        scripts = list((ROOT / "scripts").glob("*.py"))
        tests = list((ROOT / "tests").glob("*.py"))
        py_files = scripts + tests
        self.assertGreater(len(py_files), 20, "Expected Python files in scripts/ and tests/")

        for py_file in py_files:
            try:
                py_compile.compile(str(py_file), doraise=True)
            except py_compile.PyCompileError as exc:
                self.fail(f"Syntax error compiling {py_file}: {exc}")

    def test_workflow_yaml_syntax_and_deliverable_validation(self):
        """Ensure all GitHub Actions workflows parse as valid YAML and preserve Spec 2 wiring."""
        wf_files = list((ROOT / ".github" / "workflows").glob("*.yml")) + list(
            (ROOT / ".github" / "workflows").glob("*.yaml")
        )
        self.assertTrue(len(wf_files) > 0, "At least one workflow file must exist")

        for wf in wf_files:
            content = wf.read_text(encoding="utf-8")
            if yaml is not None:
                parsed = yaml.safe_load(content)
                self.assertIsInstance(parsed, dict, f"Workflow {wf.name} must parse as a dictionary")
                self.assertIn("name", parsed)
                self.assertIn("jobs", parsed)

        # Check python-app.yml specific release candidate wiring
        app_wf = ROOT / ".github" / "workflows" / "python-app.yml"
        app_content = app_wf.read_text(encoding="utf-8")
        self.assertIn("PR_LABELS:", app_content)
        self.assertIn('--labels "$PR_LABELS"', app_content)
        self.assertIn("--ci", app_content)

    def test_spec_deliverables_integration_verification(self):
        """Verify Spec 1 and Spec 2 deliverables and key API contracts."""
        # 1. Spec 2 verifier supports labels argument
        import inspect
        sig = inspect.signature(self.mod.run_verify_release)
        self.assertIn("labels", sig.parameters, "run_verify_release must accept 'labels'")

        # 2. Spec 2 CLI argument parser supports --labels
        import argparse
        parser = argparse.ArgumentParser()
        self.mod.add_release_verification_arguments(parser)
        parsed = parser.parse_args(["--labels", "release-candidate,enhancement"])
        self.assertEqual(parsed.labels, "release-candidate,enhancement")

        # 3. Spec 2 success verdict is 'VERDICT: GO'
        record = dict(VALID_RECORD_PAYLOAD)
        record["status"] = "candidate"
        record["operational_constraints"] = []
        report = (
            "# Release Audit Report\n\n"
            f"**Pull Request:** [#{record['candidate']['pull_request']}](https://github.com/{record['candidate']['repository']}/pull/{record['candidate']['pull_request']})\n\n"
            "**Target Version:** `0.1.0`\n\n"
            f"**Candidate Head SHA:** `{record['candidate']['head_sha']}`\n\n"
            "Current Release Verdict: **READY**\n"
        )
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)
            rec_file = td_p / "release-control.json"
            rep_file = td_p / "report.md"
            rec_file.write_text(json.dumps(record), encoding="utf-8")
            rep_file.write_text(report, encoding="utf-8")

            # Mock passing CI checks
            mock_api = MockReadOnlyGitHubApi({
                f"/repos/{record['candidate']['repository']}/pulls/{record['candidate']['pull_request']}": {
                    "base": {"repo": {"full_name": record["candidate"]["repository"]}},
                    "head": {"sha": record["candidate"]["head_sha"]},
                },
                f"/repos/{record['candidate']['repository']}/commits/{record['candidate']['head_sha']}/check-runs": {
                    "total_count": 3,
                    "check_runs": [
                        {
                            "name": f"build ({ver})",
                            "head_sha": record["candidate"]["head_sha"],
                            "status": "completed",
                            "conclusion": "success",
                        }
                        for ver in ["3.10", "3.11", "3.12"]
                    ],
                },
            })

            out = io.StringIO()
            with patch.object(
                self.mod,
                "validate_runtime_fingerprint",
                return_value={
                    "valid": True,
                    "reason": f"Runtime fingerprint {record['live_evidence']['runtime_fingerprint']} matches current repository runtime definitions",
                    "computed_fingerprint": record["live_evidence"]["runtime_fingerprint"],
                },
            ):
                code = self.mod.run_verify_release(
                    record_path=str(rec_file),
                    report_path=str(rep_file),
                    repo_root=td_p,
                    output_format="text",
                    out_stream=out,
                    api=mock_api,
                )
            self.assertEqual(code, 0)
            self.assertIn("VERDICT: GO", out.getvalue())

    def test_get_current_head_sha_prioritizes_head2_on_merge_commits(self):
        """When on a merge commit, _get_current_head_sha prioritizes HEAD^2 as candidate actual head."""
        with tempfile.TemporaryDirectory() as td:
            td_p = pathlib.Path(td)

            def git(*args):
                return subprocess.check_output(
                    ["git", *args], cwd=td_p, text=True, stderr=subprocess.DEVNULL
                ).strip()

            git("init", "-b", "main")
            git("config", "user.name", "Test User")
            git("config", "user.email", "test@example.invalid")

            # Commit 1 on main
            (td_p / "file.txt").write_text("v1\n")
            git("add", ".")
            git("commit", "-m", "commit 1")

            # Branch candidate
            git("checkout", "-b", "candidate")
            (td_p / "candidate.txt").write_text("cand v1\n")
            git("add", ".")
            git("commit", "-m", "candidate commit 1")
            cand_head_1 = git("rev-parse", "HEAD")

            # Checkout main and add commit 2
            git("checkout", "main")
            (td_p / "file.txt").write_text("v2\n")
            git("add", ".")
            git("commit", "-m", "main commit 2")

            # Create merge commit
            git("merge", "--no-ff", "candidate", "-m", "merge candidate")
            merge_commit = git("rev-parse", "HEAD")

            # Verify HEAD^2 is prioritized over HEAD (the merge commit)
            observed = self.mod._get_current_head_sha(td_p)
            self.assertEqual(observed, cand_head_1)
            self.assertNotEqual(observed, merge_commit)

            # Advance candidate branch
            git("checkout", "candidate")
            (td_p / "candidate.txt").write_text("cand v2\n")
            git("add", ".")
            git("commit", "-m", "candidate commit 2 (advanced)")
            cand_head_2 = git("rev-parse", "HEAD")

            # Merge advanced candidate into main
            git("checkout", "main")
            git("merge", "--no-ff", "candidate", "-m", "merge advanced candidate")
            advanced_merge = git("rev-parse", "HEAD")

            # Even when expected_sha is the old cand_head_1, observed must be the actual candidate head (cand_head_2)
            observed_advanced = self.mod._get_current_head_sha(td_p, expected_sha=cand_head_1)
            self.assertEqual(observed_advanced, cand_head_2)
            self.assertNotEqual(observed_advanced, cand_head_1)
            self.assertNotEqual(observed_advanced, advanced_merge)


if __name__ == "__main__":
    unittest.main()


