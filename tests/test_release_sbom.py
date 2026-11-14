"""Unit tests for GitHub Dependency Graph SBOM export utility."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import export_release_sbom


class ReleaseSbomExportTests(unittest.TestCase):
    def setUp(self):
        self.sample_spdx = {
            "spdxVersion": "SPDX-2.3",
            "dataLicense": "CC0-1.0",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": "com.github.octo/widget",
            "documentNamespace": "https://github.com/octo/widget/dependency_graph/sbom-12345",
            "creationInfo": {
                "created": "2026-10-12T00:00:00Z",
                "creators": ["Tool: GitHub.com-Dependency-Graph"],
            },
            "packages": [
                {
                    "SPDXID": "SPDXRef-Package-PyYAML",
                    "name": "PyYAML",
                    "versionInfo": "6.0.1",
                    "downloadLocation": "NOASSERTION",
                }
            ],
            "relationships": [
                {
                    "spdxElementId": "SPDXRef-DOCUMENT",
                    "relatedSpdxElement": "SPDXRef-Package-PyYAML",
                    "relationshipType": "DESCRIBES",
                }
            ],
        }

    def test_format_spdx_document_extracts_nested_sbom_field(self):
        wrapped = {"sbom": self.sample_spdx}
        doc = export_release_sbom.format_spdx_document(wrapped, "octo/widget")
        self.assertEqual(doc["spdxVersion"], "SPDX-2.3")
        self.assertEqual(doc["SPDXID"], "SPDXRef-DOCUMENT")
        self.assertEqual(len(doc["packages"]), 1)
        self.assertEqual(doc["packages"][0]["name"], "PyYAML")

    def test_format_spdx_document_accepts_root_spdx_document(self):
        doc = export_release_sbom.format_spdx_document(self.sample_spdx, "octo/widget")
        self.assertEqual(doc["spdxVersion"], "SPDX-2.3")
        self.assertEqual(doc["name"], "com.github.octo/widget")

    def test_format_spdx_document_rejects_invalid_payload(self):
        with self.assertRaises(export_release_sbom.SbomFormatError):
            export_release_sbom.format_spdx_document({"invalid": "data"}, "octo/widget")
        with self.assertRaises(export_release_sbom.SbomFormatError):
            export_release_sbom.format_spdx_document("not a dict", "octo/widget")  # type: ignore

    def test_format_spdx_document_rejects_empty_packages_when_required(self):
        empty_doc = dict(self.sample_spdx, packages=[])
        with self.assertRaises(export_release_sbom.SbomValidationError) as ctx:
            export_release_sbom.format_spdx_document(empty_doc, "octo/widget", require_packages=True)
        self.assertIn("no packages", str(ctx.exception).lower())
        self.assertTrue(bool(ctx.exception.recovery))
        self.assertIn("manifests", ctx.exception.recovery.lower())

    def test_create_fallback_sbom_generates_valid_spdx_structure(self):
        fallback = export_release_sbom.create_fallback_sbom("octo/widget", "Dependency graph disabled")
        self.assertEqual(fallback["spdxVersion"], "SPDX-2.3")
        self.assertEqual(fallback["SPDXID"], "SPDXRef-DOCUMENT")
        self.assertEqual(fallback["name"], "octo/widget")
        self.assertEqual(fallback["packages"], [])
        self.assertIn("Dependency graph disabled", fallback["comment"])
        self.assertTrue(fallback["documentNamespace"].startswith("https://github.com/octo/widget/"))

    def test_export_release_sbom_successful_fetch_and_formatting(self):
        sample = self.sample_spdx

        class MockApi:
            def request(self, method, path):
                self_path = "/repos/octo/widget/dependency-graph/sbom"
                if method == "GET" and path == self_path:
                    return 200, {"sbom": sample}
                return 404, None

        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "sbom.spdx.json"
            result = export_release_sbom.export_release_sbom(
                repo="octo/widget",
                output_path=output_file,
                token="test-token",
                api=MockApi(),
            )
            self.assertTrue(output_file.is_file())
            content = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(content["spdxVersion"], "SPDX-2.3")
            self.assertEqual(content["packages"][0]["name"], "PyYAML")
            self.assertEqual(result["SPDXID"], "SPDXRef-DOCUMENT")

    def test_export_release_sbom_fallback_on_404_not_found(self):
        class MockApi:
            def request(self, method, path):
                return 404, {"message": "Dependency graph is not enabled for this repository."}

        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "sbom.spdx.json"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = export_release_sbom.export_release_sbom(
                    repo="octo/widget",
                    output_path=output_file,
                    token="test-token",
                    api=MockApi(),
                    allow_fallback=True,
                )
            self.assertTrue(output_file.is_file())
            content = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(content["spdxVersion"], "SPDX-2.3")
            self.assertEqual(content["packages"], [])
            self.assertIn("Warning: GitHub Dependency Graph SBOM unavailable", stderr.getvalue())
            self.assertIn("fallback", content["comment"].lower())

    def test_export_release_sbom_fallback_on_non_200_status(self):
        class MockApi:
            def request(self, method, path):
                return 403, {"message": "Must have admin rights to Repository."}

        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "sbom.spdx.json"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = export_release_sbom.export_release_sbom(
                    repo="octo/widget",
                    output_path=output_file,
                    token="test-token",
                    api=MockApi(),
                    allow_fallback=True,
                )
            self.assertTrue(output_file.is_file())
            content = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(content["spdxVersion"], "SPDX-2.3")

    def test_export_release_sbom_error_when_fallback_disabled(self):
        class MockApi:
            def request(self, method, path):
                return 404, {"message": "Not Found"}

        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "sbom.spdx.json"
            with self.assertRaises(export_release_sbom.SbomExportError) as ctx:
                export_release_sbom.export_release_sbom(
                    repo="octo/widget",
                    output_path=output_file,
                    token="test-token",
                    api=MockApi(),
                    allow_fallback=False,
                )
            self.assertFalse(output_file.exists())
            self.assertIn("Dependency Graph in repository settings", ctx.exception.recovery)
            self.assertIn("Recovery:", str(ctx.exception))

    def test_export_release_sbom_empty_packages_fails_with_validation_error(self):
        empty_doc = dict(self.sample_spdx, packages=[])
        class MockApi:
            def request(self, method, path):
                return 200, {"sbom": empty_doc}

        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "sbom.spdx.json"
            with self.assertRaises(export_release_sbom.SbomValidationError) as ctx:
                export_release_sbom.export_release_sbom(
                    repo="octo/widget",
                    output_path=output_file,
                    token="test-token",
                    api=MockApi(),
                    allow_fallback=False,
                    require_packages=True,
                )
            self.assertFalse(output_file.exists())
            self.assertIn("manifests", ctx.exception.recovery.lower())
            self.assertIn("Recovery:", str(ctx.exception))

    def test_export_release_sbom_redacts_credentials_in_diagnostics(self):
        secret_token = "ghp_SecretToken1234567890abcdef"
        class MockApi:
            def request(self, method, path):
                raise RuntimeError(f"Failed with token {secret_token}")

        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "sbom.spdx.json"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                export_release_sbom.export_release_sbom(
                    repo="octo/widget",
                    output_path=output_file,
                    token=secret_token,
                    api=MockApi(),
                    allow_fallback=True,
                )
            log_output = stderr.getvalue()
            self.assertNotIn(secret_token, log_output)
            self.assertIn("[REDACTED]", log_output)

    def test_cli_successful_invocation(self):
        sample = self.sample_spdx

        class MockApi:
            def request(self, method, path):
                return 200, {"sbom": sample}

        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "sbom.spdx.json"
            with patch("export_release_sbom.SbomGitHubApi", return_value=MockApi()):
                exit_code = export_release_sbom.main([
                    "--repo", "octo/widget",
                    "--output", str(output_file),
                    "--token", "dummy-token",
                ])
            self.assertEqual(exit_code, 0)
            self.assertTrue(output_file.is_file())
            content = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(content["packages"][0]["name"], "PyYAML")

    def test_cli_failure_with_no_fallback_flag(self):
        class MockApi:
            def request(self, method, path):
                return 404, {"message": "Not found"}

        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "sbom.spdx.json"
            stderr = io.StringIO()
            with patch("export_release_sbom.SbomGitHubApi", return_value=MockApi()):
                with contextlib.redirect_stderr(stderr):
                    exit_code = export_release_sbom.main([
                        "--repo", "octo/widget",
                        "--output", str(output_file),
                        "--token", "dummy-token",
                        "--no-fallback",
                    ])
            self.assertEqual(exit_code, 1)
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertIn("Failed to export SBOM", stderr.getvalue())
            self.assertIn("Recovery:", stderr.getvalue())
            self.assertIn("Settings", stderr.getvalue())

    def test_cli_failure_with_empty_packages_and_no_fallback(self):
        empty_doc = dict(self.sample_spdx, packages=[])
        class MockApi:
            def request(self, method, path):
                return 200, {"sbom": empty_doc}

        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "sbom.spdx.json"
            stderr = io.StringIO()
            with patch("export_release_sbom.SbomGitHubApi", return_value=MockApi()):
                with contextlib.redirect_stderr(stderr):
                    exit_code = export_release_sbom.main([
                        "--repo", "octo/widget",
                        "--output", str(output_file),
                        "--token", "dummy-token",
                        "--no-fallback",
                    ])
            self.assertEqual(exit_code, 1)
            self.assertIn("Recovery:", stderr.getvalue())
            self.assertIn("manifests", stderr.getvalue().lower())

    def test_handle_api_failure_fallback_and_raise(self):
        fallback = export_release_sbom._handle_api_failure("octo/widget", "Dependency graph unavailable", allow_fallback=True)
        self.assertEqual(fallback["spdxVersion"], "SPDX-2.3")
        self.assertIn("Dependency graph unavailable", fallback["comment"])

        custom_exc = export_release_sbom.SbomFormatError("Custom format error")
        with self.assertRaises(export_release_sbom.SbomFormatError):
            export_release_sbom._handle_api_failure("octo/widget", "Custom format error", allow_fallback=False, exc=custom_exc)

        with self.assertRaises(export_release_sbom.SbomExportError):
            export_release_sbom._handle_api_failure("octo/widget", "API failure", allow_fallback=False)


if __name__ == "__main__":
    unittest.main()
