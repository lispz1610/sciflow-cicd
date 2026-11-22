"""Simulation tests for fail-closed release promotion payload generation."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import release_promotion
import verify_release


_USE_DEFAULT = object()


class _DefaultMockApi:
    def request(self, method, path):
        if method == "GET" and path == "/repos/octo/widget/dependency-graph/sbom":
            return 200, {
                "sbom": {
                    "spdxVersion": "SPDX-2.3",
                    "dataLicense": "CC0-1.0",
                    "SPDXID": "SPDXRef-DOCUMENT",
                    "name": "octo/widget",
                    "documentNamespace": "https://github.com/octo/widget/dependency_graph/sbom-default",
                    "creationInfo": {"created": "2026-10-12T00:00:00Z", "creators": ["Tool: GitHub.com-Dependency-Graph"]},
                    "packages": [{"SPDXID": "SPDXRef-Package-Default", "name": "default-pkg", "versionInfo": "1.0.0"}],
                    "relationships": [],
                }
            }
        return 404, None


class ReleasePromotionSimulationTests(unittest.TestCase):
    def setUp(self):
        self.repo_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.repo_temp.cleanup)
        self.repo_root = Path(self.repo_temp.name)
        (self.repo_root / "scripts").mkdir()
        (self.repo_root / "scripts" / "runtime.py").write_text("VALUE = 1\n")
        self.git("init", "-q")
        self.git("config", "user.name", "Release test")
        self.git("config", "user.email", "release-test@example.invalid")
        self.git("add", "scripts/runtime.py")
        self.git("commit", "-qm", "candidate")
        candidate_sha = self.git("rev-parse", "HEAD")
        tree_sha = self.git("rev-parse", "HEAD^{tree}")
        merge_sha = self.git("commit-tree", tree_sha, "-p", candidate_sha, "-m", "simulated merge")
        fingerprint = verify_release.compute_runtime_fingerprint(self.repo_root, commit_sha=merge_sha)
        self.event = {
            "repository": {"full_name": "octo/widget"},
            "pull_request": {
                "number": 41,
                "merged": True,
                "base": {"ref": "main"},
                "head": {"sha": candidate_sha},
                "merge_commit_sha": merge_sha,
                "labels": [{"name": "release-candidate"}],
            },
        }
        self.control = self.record("candidate", candidate_sha, fingerprint)
        self.evidence = self.record("active", candidate_sha, fingerprint)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo_root, text=True).strip()

    def record(self, status, head_sha="a" * 40, fingerprint="c" * 64):
        return {
            "version": 1,
            "status": status,
            "candidate": {
                "repository": "octo/widget",
                "pull_request": 41,
                "target_version": "1.2.3",
                "head_sha": head_sha,
            },
            "required_gates": {"python_versions": ["3.10", "3.11", "3.12"]},
            "live_evidence": {"runtime_fingerprint": fingerprint},
            "operational_constraints": ["documented limit"],
            "unverified_scenarios": [],
        }

    def prepare(self, directory, event=None, control=None, evidence=None, api=_USE_DEFAULT, tag="v1.2.3", **options):
        if api is _USE_DEFAULT:
            api = _DefaultMockApi()
        docs = Path(directory)
        docs.mkdir(exist_ok=True)
        (docs / "control.json").write_text(json.dumps(control or self.control))
        (docs / "evidence.json").write_text(json.dumps(evidence or self.evidence))
        candidate = (control or self.control)["candidate"]
        verdict = (
            "READY WITH DECLARED OPERATIONAL CONSTRAINTS"
            if (control or self.control).get("operational_constraints")
            else "READY"
        )
        audit_report = (
            f"# Verified release audit\n\n"
            f"**Pull Request:** [#{candidate['pull_request']}]"
            f"(https://github.com/octo/widget/pull/{candidate['pull_request']})\n"
            f"**Candidate Head SHA:** `{candidate['head_sha']}`\n"
            f"**Target Version:** `{candidate['target_version']}`\n"
            f"**Current Release Verdict:** **{verdict}**\n"
        )
        (docs / "audit.md").write_text(audit_report)
        return release_promotion.prepare_release_payload(
            event or self.event,
            control_path=docs / "control.json",
            evidence_path=docs / "evidence.json",
            audit_report_path=docs / "audit.md",
            output_dir=docs / "assets",
            repo_root=self.repo_root,
            api=api,
            tag=tag,
            **options,
        )

    VALID_CITATION = (
        "cff-version: 1.2.0\n"
        "message: \"If you use this software, please cite it as below.\"\n"
        "title: Widget\n"
        "version: 1.2.3\n"
        "date-released: 2026-01-31\n"
        "authors:\n"
        "  - family-names: Doe\n"
        "    given-names: Jane\n"
    )

    def write_citation(self, text=None):
        (self.repo_root / "CITATION.cff").write_text(self.VALID_CITATION if text is None else text, encoding="utf-8")

    def test_missing_tag_refuses_publication_and_writes_no_assets(self):
        for tag in (None, "", "  "):
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(release_promotion.ReleasePromotionError, "explicit tag"):
                    self.prepare(tmp, tag=tag)
                self.assertFalse((Path(tmp) / "assets").exists())

    def test_tag_must_match_control_record_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(release_promotion.ReleasePromotionError, "does not match"):
                self.prepare(tmp, tag="v9.9.9")
            self.assertFalse((Path(tmp) / "assets").exists())

    def test_cli_requires_tag_when_promoting(self):
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            event_path.write_text(json.dumps(self.event))
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                status = release_promotion.main(["--event", str(event_path), "--output-dir", str(Path(tmp) / "a")])
            self.assertEqual(status, 1)
            self.assertIn("explicit tag", stderr.getvalue())

    def test_scientific_profile_attaches_validated_citation_and_sbom(self):
        self.write_citation()
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(io.StringIO()):
                payload = self.prepare(tmp, profile="scientific")
            assets = Path(tmp) / "assets"
            self.assertEqual(payload["tag"], "v1.2.3")
            self.assertEqual(payload["optional_assets"], ["CITATION.cff", "sbom.spdx.json"])
            self.assertEqual((assets / "CITATION.cff").read_text(), self.VALID_CITATION)
            self.assertTrue((assets / "sbom.spdx.json").is_file())
            notes = (assets / "release-notes.md").read_text()
            self.assertIn("does not prove scientific reproducibility", notes)

    def test_invalid_citation_aborts_before_any_asset_is_written(self):
        cases = {
            "missing file": None,
            "no title": self.VALID_CITATION.replace("title: Widget\n", ""),
            "no authors": self.VALID_CITATION.split("authors:")[0],
            "version mismatch": self.VALID_CITATION.replace("version: 1.2.3", "version: 2.0.0"),
            "bad date": self.VALID_CITATION.replace("2026-01-31", "31/01/2026"),
            "bad doi": self.VALID_CITATION + "doi: not-a-doi\n",
        }
        for name, text in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                citation = self.repo_root / "CITATION.cff"
                if text is None:
                    citation.unlink(missing_ok=True)
                else:
                    self.write_citation(text)
                with self.assertRaisesRegex(release_promotion.ReleasePromotionError, "CITATION.cff"):
                    self.prepare(tmp, profile="scientific")
                self.assertFalse((Path(tmp) / "assets").exists())

    def test_minimal_profile_attaches_no_optional_assets_and_ignores_citation(self):
        self.write_citation("not valid")
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.prepare(tmp, profile="minimal")
            assets = Path(tmp) / "assets"
            self.assertEqual(payload["optional_assets"], [])
            self.assertFalse((assets / "sbom.spdx.json").exists())
            self.assertFalse((assets / "CITATION.cff").exists())
            self.assertNotIn("Software Bill of Materials", (assets / "release-notes.md").read_text())

    def test_release_profile_default_attaches_only_sbom(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(io.StringIO()):
                payload = self.prepare(tmp)
            self.assertEqual(payload["optional_assets"], ["sbom.spdx.json"])
            self.assertFalse((Path(tmp) / "assets" / "CITATION.cff").exists())

    def test_candidate_declaring_scientific_profile_is_promoted_dynamically(self):
        self.write_citation()
        control_scientific = json.loads(json.dumps(self.control))
        control_scientific["candidate"]["profile"] = "scientific"
        evidence_scientific = json.loads(json.dumps(self.evidence))
        evidence_scientific["candidate"]["profile"] = "scientific"
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(io.StringIO()):
                payload = self.prepare(tmp, control=control_scientific, evidence=evidence_scientific, profile=None)
            assets = Path(tmp) / "assets"
            self.assertEqual(payload["optional_assets"], ["CITATION.cff", "sbom.spdx.json"])
            self.assertTrue((assets / "CITATION.cff").is_file())
            self.assertTrue((assets / "sbom.spdx.json").is_file())

    def test_root_level_scientific_profile_is_recognized_and_enforces_citation_validation(self):
        control_root_sci = json.loads(json.dumps(self.control))
        control_root_sci.pop("profile", None)
        control_root_sci["candidate"].pop("profile", None)
        control_root_sci["profile"] = "scientific"

        evidence_root_sci = json.loads(json.dumps(self.evidence))
        evidence_root_sci.pop("profile", None)
        evidence_root_sci["candidate"].pop("profile", None)

        # Case 1: CITATION.cff is missing -> promotion fails closed because scientific profile requires CITATION.cff
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(release_promotion.ReleasePromotionError) as ctx:
                self.prepare(tmp, control=control_root_sci, evidence=evidence_root_sci, profile=None)
            self.assertIn("CITATION.cff", str(ctx.exception))

        # Case 2: CITATION.cff is present -> promotion succeeds and includes both CITATION.cff and sbom.spdx.json
        self.write_citation()
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(io.StringIO()):
                payload = self.prepare(tmp, control=control_root_sci, evidence=evidence_root_sci, profile=None)
            assets = Path(tmp) / "assets"
            self.assertEqual(payload["optional_assets"], ["CITATION.cff", "sbom.spdx.json"])
            self.assertTrue((assets / "CITATION.cff").is_file())
            self.assertTrue((assets / "sbom.spdx.json").is_file())

    def test_cff_parsing_reuses_doi_guidance_and_parses_scalars(self):
        cff_content = (
            'cff-version: 1.2.0\n'
            'message: "Cite this repository"\n'
            "title: 'My Module'\n"
            'authors:\n'
            '  - name: Alice\n'
        )
        keys = release_promotion._top_level_cff(cff_content)
        self.assertEqual(keys.get("cff-version"), "1.2.0")
        self.assertEqual(keys.get("message"), "Cite this repository")
        self.assertEqual(keys.get("title"), "My Module")
        self.assertEqual(keys.get("authors"), "")

    def test_citation_with_doi_fails_closed_when_doi_state_is_not_issued(self):
        # Even with a syntactically valid DOI, promotion fails closed if doi_state is unverified/not issued
        cff_with_doi = self.VALID_CITATION + "doi: 10.5281/zenodo.1234567\n"
        self.write_citation(cff_with_doi)

        # Case 1: doi_state is not specified in Control Record (defaults to unselected)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(release_promotion.ReleasePromotionError) as ctx:
                self.prepare(tmp, profile="scientific")
            self.assertIn("CITATION.cff", str(ctx.exception))
            self.assertIn("never recorded", str(ctx.exception))

        # Case 2: doi_state is explicitly 'pending' or 'requested'
        for non_issued_state in ("pending", "requested", "unselected"):
            control_pending = json.loads(json.dumps(self.control))
            control_pending["doi_state"] = non_issued_state
            evidence_pending = json.loads(json.dumps(self.evidence))
            evidence_pending["doi_state"] = non_issued_state
            with self.subTest(state=non_issued_state), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(release_promotion.ReleasePromotionError) as ctx:
                    self.prepare(tmp, control=control_pending, evidence=evidence_pending, profile="scientific")
                self.assertIn("CITATION.cff", str(ctx.exception))
                self.assertIn(non_issued_state, str(ctx.exception))

    def test_citation_with_verified_doi_succeeds_when_doi_state_is_issued(self):
        doi = "10.5281/zenodo.1234567"
        cff_with_doi = self.VALID_CITATION + f"doi: {doi}\n"
        self.write_citation(cff_with_doi)

        control_issued = json.loads(json.dumps(self.control))
        control_issued["doi_state"] = "issued"
        control_issued["doi"] = doi
        evidence_issued = json.loads(json.dumps(self.evidence))
        evidence_issued["doi_state"] = "issued"
        evidence_issued["doi"] = doi

        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(io.StringIO()):
                payload = self.prepare(tmp, control=control_issued, evidence=evidence_issued, profile="scientific")
            assets = Path(tmp) / "assets"
            self.assertEqual(payload["optional_assets"], ["CITATION.cff", "sbom.spdx.json"])
            self.assertEqual((assets / "CITATION.cff").read_text(encoding="utf-8"), cff_with_doi)
            notes = (assets / "release-notes.md").read_text(encoding="utf-8")
            self.assertIn(f"DOI: `{doi}`", notes)

    def test_control_record_and_evidence_doi_metadata_mismatch_fails_closed(self):
        self.write_citation()
        control = json.loads(json.dumps(self.control))
        control["doi_state"] = "pending"
        evidence = json.loads(json.dumps(self.evidence))
        evidence["doi_state"] = "issued"
        evidence["doi"] = "10.5281/zenodo.1234567"

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(release_promotion.ReleasePromotionError, "DOI state does not match"):
                self.prepare(tmp, control=control, evidence=evidence, profile="release")

    def test_control_record_and_evidence_asymmetric_doi_state_fails_closed(self):
        self.write_citation()
        doi = "10.5281/zenodo.1234567"

        # Case 1: Control specifies issued DOI, evidence has no DOI metadata
        control_issued = json.loads(json.dumps(self.control))
        control_issued["doi_state"] = "issued"
        control_issued["doi"] = doi
        evidence_empty = json.loads(json.dumps(self.evidence))

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(release_promotion.ReleasePromotionError, "DOI state does not match"):
                self.prepare(tmp, control=control_issued, evidence=evidence_empty, profile="release")

        # Case 2: Evidence specifies issued DOI, control has no DOI metadata
        control_empty = json.loads(json.dumps(self.control))
        evidence_issued = json.loads(json.dumps(self.evidence))
        evidence_issued["doi_state"] = "issued"
        evidence_issued["doi"] = doi

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(release_promotion.ReleasePromotionError, "DOI state does not match"):
                self.prepare(tmp, control=control_empty, evidence=evidence_issued, profile="release")

        # Case 3: Control specifies pending DOI, evidence has no DOI metadata
        control_pending = json.loads(json.dumps(self.control))
        control_pending["doi_state"] = "pending"

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(release_promotion.ReleasePromotionError, "DOI state does not match"):
                self.prepare(tmp, control=control_pending, evidence=evidence_empty, profile="release")

        # Case 4: Evidence specifies pending DOI, control has no DOI metadata
        evidence_pending = json.loads(json.dumps(self.evidence))
        evidence_pending["doi_state"] = "pending"

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(release_promotion.ReleasePromotionError, "DOI state does not match"):
                self.prepare(tmp, control=control_empty, evidence=evidence_pending, profile="release")

    def test_control_record_and_evidence_mismatched_doi_fails_closed(self):
        self.write_citation()
        control = json.loads(json.dumps(self.control))
        control["doi_state"] = "issued"
        control["doi"] = "10.5281/zenodo.1234567"
        evidence = json.loads(json.dumps(self.evidence))
        evidence["doi_state"] = "issued"
        evidence["doi"] = "10.5281/zenodo.9999999"

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(release_promotion.ReleasePromotionError, "DOI does not match"):
                self.prepare(tmp, control=control, evidence=evidence, profile="release")

    def test_unknown_profile_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(release_promotion.ReleasePromotionError, "profile"):
                self.prepare(tmp, profile="everything")

    def test_unmerged_pull_request_is_bypassed_without_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = json.loads(json.dumps(self.event))
            event["pull_request"]["merged"] = False
            result = self.prepare(tmp, event=event)
            self.assertIsNone(result)
            self.assertFalse((Path(tmp) / "assets").exists())
            event_path = Path(tmp) / "event.json"
            event_path.write_text(json.dumps(event))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = release_promotion.main([
                    "--event", str(event_path),
                    "--output-dir", str(Path(tmp) / "cli-assets"),
                ])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(stdout.getvalue()), {"skipped": True})

    def test_unlabeled_pull_request_is_bypassed_without_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = json.loads(json.dumps(self.event))
            event["pull_request"]["labels"] = [{"name": "bug"}]
            result = self.prepare(tmp, event=event)
            self.assertIsNone(result)
            self.assertFalse((Path(tmp) / "assets").exists())

    def test_candidate_merged_outside_main_cannot_be_promoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = json.loads(json.dumps(self.event))
            event["pull_request"]["base"]["ref"] = "maintenance"
            with self.assertRaisesRegex(release_promotion.ReleasePromotionError, "main"):
                self.prepare(tmp, event=event)
            self.assertFalse((Path(tmp) / "assets").exists())

    def test_matching_active_evidence_generates_release_assets_and_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stderr(io.StringIO()):
                payload = self.prepare(tmp)
            assets = Path(tmp) / "assets"
            self.assertEqual(payload["tag"], "v1.2.3")
            self.assertNotEqual(payload["target_sha"], self.event["pull_request"]["head"]["sha"])
            self.assertEqual(payload["target_sha"], self.event["pull_request"]["merge_commit_sha"])
            self.assertEqual(payload["version"], "1.2.3")
            self.assertEqual(payload["runtime_fingerprint"], self.control["live_evidence"]["runtime_fingerprint"])
            self.assertEqual((assets / "audit-report.md").read_text(), (Path(tmp) / "audit.md").read_text())
            self.assertEqual((assets / "runtime-fingerprint.txt").read_text(), payload["runtime_fingerprint"] + "\n")
            self.assertTrue((assets / "sbom.spdx.json").is_file())
            sbom = json.loads((assets / "sbom.spdx.json").read_text(encoding="utf-8"))
            self.assertEqual(sbom["spdxVersion"], "SPDX-2.3")
            self.assertEqual(sbom["SPDXID"], "SPDXRef-DOCUMENT")
            notes = (assets / "release-notes.md").read_text()
            self.assertIn("Pull request: #41", notes)
            self.assertIn("Merged commit: `" + self.event["pull_request"]["merge_commit_sha"] + "`", notes)
            self.assertIn("documented limit", notes)
            self.assertIn("Software Bill of Materials: `sbom.spdx.json`.", notes)

    def test_release_promotion_asset_generation_with_sbom_from_api(self):
        sample_sbom = {
            "spdxVersion": "SPDX-2.3",
            "dataLicense": "CC0-1.0",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": "octo/widget",
            "documentNamespace": "https://github.com/octo/widget/dependency_graph/sbom-test",
            "creationInfo": {"created": "2026-10-12T00:00:00Z", "creators": ["Tool: GitHub.com-Dependency-Graph"]},
            "packages": [{"SPDXID": "SPDXRef-Package-Test", "name": "test-pkg", "versionInfo": "1.0.0"}],
            "relationships": [],
        }

        class MockApi:
            def request(self, method, path):
                if method == "GET" and path == "/repos/octo/widget/dependency-graph/sbom":
                    return 200, {"sbom": sample_sbom}
                return 404, None

        with tempfile.TemporaryDirectory() as tmp:
            self.prepare(tmp, api=MockApi())
            assets = Path(tmp) / "assets"
            self.assertTrue((assets / "sbom.spdx.json").is_file())
            sbom = json.loads((assets / "sbom.spdx.json").read_text(encoding="utf-8"))
            self.assertEqual(len(sbom["packages"]), 1)
            self.assertEqual(sbom["packages"][0]["name"], "test-pkg")

    def test_sbom_failure_404_dependency_graph_disabled_aborts_with_recovery(self):
        class Mock404Api:
            def request(self, method, path):
                return 404, {"message": "Dependency graph is not enabled for this repository."}

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(release_promotion.ReleasePromotionError) as ctx:
                self.prepare(tmp, api=Mock404Api())
            self.assertFalse((Path(tmp) / "assets").exists())
            err_msg = str(ctx.exception)
            self.assertIn("Dependency Graph in repository settings", err_msg)
            self.assertIn("Recovery:", err_msg)

    def test_sbom_failure_empty_packages_aborts_with_recovery(self):
        class MockEmptyApi:
            def request(self, method, path):
                return 200, {
                    "sbom": {
                        "spdxVersion": "SPDX-2.3",
                        "dataLicense": "CC0-1.0",
                        "SPDXID": "SPDXRef-DOCUMENT",
                        "name": "octo/widget",
                        "packages": [],
                    }
                }

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(release_promotion.ReleasePromotionError) as ctx:
                self.prepare(tmp, api=MockEmptyApi())
            self.assertFalse((Path(tmp) / "assets").exists())
            err_msg = str(ctx.exception)
            self.assertIn("manifests", err_msg.lower())
            self.assertIn("Recovery:", err_msg)

    def test_sbom_failure_api_error_aborts_with_recovery(self):
        class Mock500Api:
            def request(self, method, path):
                return 500, {"message": "Internal Server Error"}

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(release_promotion.ReleasePromotionError) as ctx:
                self.prepare(tmp, api=Mock500Api())
            self.assertFalse((Path(tmp) / "assets").exists())
            err_msg = str(ctx.exception)
            self.assertIn("Recovery:", err_msg)

    def test_sbom_failure_malformed_payload_aborts_with_recovery(self):
        class MockMalformedApi:
            def request(self, method, path):
                return 200, {"not_sbom": True}

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(release_promotion.ReleasePromotionError) as ctx:
                self.prepare(tmp, api=MockMalformedApi())
            self.assertFalse((Path(tmp) / "assets").exists())
            err_msg = str(ctx.exception)
            self.assertIn("Recovery:", err_msg)

    def test_no_empty_or_placeholder_sbom_is_ever_uploaded(self):
        class MockFallbackApi:
            def request(self, method, path):
                return 200, {
                    "sbom": {
                        "spdxVersion": "SPDX-2.3",
                        "dataLicense": "CC0-1.0",
                        "SPDXID": "SPDXRef-DOCUMENT",
                        "name": "octo/widget",
                        "comment": "Fallback document",
                        "packages": [],
                    }
                }

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(release_promotion.ReleasePromotionError):
                self.prepare(tmp, api=MockFallbackApi())
            self.assertFalse((Path(tmp) / "assets").exists())

    def test_cli_sbom_failure_exits_nonzero_with_clean_recovery_message(self):
        class Mock404Api:
            def request(self, method, path):
                return 404, {"message": "Dependency graph is not enabled."}

        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp)
            (docs / "control.json").write_text(json.dumps(self.control))
            (docs / "evidence.json").write_text(json.dumps(self.evidence))
            candidate = self.control["candidate"]
            (docs / "audit.md").write_text(
                f"# Verified release audit\n\n"
                f"**Pull Request:** [#{candidate['pull_request']}](https://github.com/octo/widget/pull/41)\n"
                f"**Candidate Head SHA:** `{candidate['head_sha']}`\n"
                f"**Target Version:** `{candidate['target_version']}`\n"
                f"**Current Release Verdict:** **READY WITH DECLARED OPERATIONAL CONSTRAINTS**\n"
            )
            event_path = docs / "event.json"
            event_path.write_text(json.dumps(self.event))
            output_dir = docs / "assets"

            stderr = io.StringIO()
            with patch("scripts.export_release_sbom.SbomGitHubApi", return_value=Mock404Api()), \
                 patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}):
                with contextlib.redirect_stderr(stderr):
                    status = release_promotion.main([
                        "--event", str(event_path),
                        "--control", str(docs / "control.json"),
                        "--evidence", str(docs / "evidence.json"),
                        "--audit-report", str(docs / "audit.md"),
                        "--output-dir", str(output_dir),
                        "--repo-root", str(self.repo_root),
                        "--tag", "v1.2.3",
                        "--profile", "release",
                    ])
            self.assertEqual(status, 1)
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertIn("Release promotion failed: Cannot generate release SBOM", stderr.getvalue())
            self.assertIn("Dependency Graph in repository settings", stderr.getvalue())
            self.assertIn("Recovery:", stderr.getvalue())
            self.assertFalse(output_dir.exists())

    def test_invalid_record_reports_clean_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            event_path = Path(tmp) / "event.json"
            event_path.write_text(json.dumps(self.event))
            control_path = Path(tmp) / "control.json"
            control_path.write_text("{invalid json")
            with contextlib.redirect_stderr(stderr):
                status = release_promotion.main([
                    "--event", str(event_path),
                    "--control", str(control_path),
                    "--evidence", str(Path(tmp) / "missing-evidence.json"),
                    "--audit-report", str(Path(tmp) / "missing-audit.md"),
                    "--output-dir", str(Path(tmp) / "assets"),
                    "--repo-root", str(ROOT),
                    "--tag", "v1.2.3",
                ])
            self.assertEqual(status, 1)
            self.assertIn("Release promotion failed: Invalid Control Record:", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_archived_or_mismatched_records_fail_with_actionable_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(release_promotion.ReleasePromotionError, "archived"):
                self.prepare(
                    tmp,
                    evidence=self.record(
                        "archived",
                        self.event["pull_request"]["head"]["sha"],
                        self.control["live_evidence"]["runtime_fingerprint"],
                    ),
                )
        with tempfile.TemporaryDirectory() as tmp:
            stale = self.record(
                "active",
                self.event["pull_request"]["head"]["sha"],
                self.control["live_evidence"]["runtime_fingerprint"],
            )
            stale["candidate"]["pull_request"] = 23
            with self.assertRaisesRegex(release_promotion.ReleasePromotionError, "candidate identity"):
                self.prepare(tmp, evidence=stale)

    def test_candidate_cannot_supply_its_own_release_authority(self):
        class FilesApi:
            def request(self, method, path):
                self_path = "/repos/octo/widget/pulls/41/files?per_page=100&page=1"
                assert method == "GET" and path == self_path
                return 200, [{"filename": "docs/release-control.json"}]

        with patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}):
            with self.assertRaisesRegex(release_promotion.ReleasePromotionError, "trusted release records"):
                release_promotion.verify_trusted_release_gate(self.event, self.repo_root, api=FilesApi())

    def test_gate_is_rechecked_before_promotion(self):
        class FilesApi:
            def request(self, method, path):
                assert method == "GET" and path.endswith("/pulls/41/files?per_page=100&page=1")
                return 200, []

        failed = subprocess.CompletedProcess([], 1, '{"gate_verdict":"NOT READY"}', "")
        with patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}):
            with patch.object(release_promotion.subprocess, "run", return_value=failed):
                with self.assertRaisesRegex(release_promotion.ReleasePromotionError, "Release Gate"):
                    release_promotion.verify_trusted_release_gate(self.event, self.repo_root, api=FilesApi())

        passed = subprocess.CompletedProcess([], 0, '{"gate_verdict":"GO"}', "")
        with patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}):
            with patch.object(release_promotion.subprocess, "run", return_value=passed) as run:
                release_promotion.verify_trusted_release_gate(self.event, self.repo_root, api=FilesApi())
        command = run.call_args.args[0]
        self.assertEqual(command[1], str(release_promotion.ROOT / "scripts" / "verify_release.py"))
        self.assertIn("--ci", command)
        self.assertIn("--record", command)
        self.assertIn("--report", command)
        self.assertEqual(run.call_args.kwargs["env"]["GITHUB_HEAD_SHA"], self.event["pull_request"]["head"]["sha"])
        self.assertEqual(run.call_args.kwargs["env"]["GITHUB_PR_NUMBER"], "41")
        self.assertEqual(run.call_args.kwargs["env"]["GITHUB_REF"], "refs/pull/41/head")
        self.assertEqual(run.call_args.kwargs["env"]["SCIFLOW_CICD_REPO_ROOT"], str(self.repo_root.resolve()))

    def test_trusted_ancestor_excludes_candidate_code_for_merge_squash_and_rebase(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)

            def git(*arguments):
                return subprocess.check_output(["git", *arguments], cwd=repository, text=True).strip()

            def commit_file(value):
                (repository / "release.py").write_text(value, encoding="utf-8")
                git("add", "release.py")
                git("commit", "-qm", value)
                return git("rev-parse", "HEAD")

            git("init", "-q")
            git("config", "user.name", "Release test")
            git("config", "user.email", "release-test@example.invalid")
            base = commit_file("trusted")
            git("checkout", "-qb", "candidate")
            candidate = commit_file("untrusted")
            git("checkout", "-q", "--detach", base)
            git("checkout", "-qB", "main")
            (repository / "base.txt").write_text("base moved\n", encoding="utf-8")
            git("add", "base.txt")
            git("commit", "-qm", "advance main")
            premerge = git("rev-parse", "HEAD")
            merged_tree = git("rev-parse", f"{candidate}^{{tree}}")
            merge = git("commit-tree", merged_tree, "-p", premerge, "-p", candidate, "-m", "merge")
            squash = git("commit-tree", merged_tree, "-p", premerge, "-m", "squash")
            rebased = git("commit-tree", merged_tree, "-p", premerge, "-m", "rebased")
            rebased_tail = git("commit-tree", merged_tree, "-p", rebased, "-m", "rebased tail")

            for merged_sha in (merge, squash, rebased_tail):
                with self.subTest(merge_sha=merged_sha):
                    ancestor = git("merge-base", "--all", candidate, f"{merged_sha}^1")
                    self.assertEqual(ancestor, base)


class ReleasePromotionWorkflowTests(unittest.TestCase):
    def test_release_publication_controls_have_code_owners(self):
        owners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
        for path in (
            "/.github/CODEOWNERS",
            "/.github/workflows/release-promotion.yml",
            "/scripts/release_promotion.py",
            "/scripts/verify_release.py",
            "/scripts/configure_github_repo.py",
            "/scripts/export_release_sbom.py",
        ):
            self.assertIn(f"{path} @lispz1610", owners)

    def test_workflow_is_narrowly_triggered_and_publishes_immutable_merged_commit(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is required to validate GitHub Actions workflow structure")
        workflow_path = ROOT / ".github" / "workflows" / "release-promotion.yml"
        workflow = yaml.load(workflow_path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["on"]["pull_request"]["types"], ["closed"])
        self.assertEqual(workflow["on"]["pull_request"]["branches"], ["main"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        job = workflow["jobs"]["promote-release"]
        self.assertEqual(job["permissions"], {"contents": "write", "checks": "read", "pull-requests": "read"})
        self.assertIn("pull_request.merged == true", job["if"])
        self.assertIn("release-candidate", job["if"])
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        steps = job["steps"]
        checkout = steps[0]
        self.assertEqual(checkout["with"]["ref"], "${{ github.event.pull_request.merge_commit_sha }}")
        fetch = next(step for step in steps if step.get("name") == "Fetch the candidate commit for fingerprint verification")
        self.assertIn('git cat-file -e "$HEAD_SHA^{commit}"', fetch["run"])
        self.assertIn('refs/pull/$PR_NUMBER/head', fetch["run"])
        self.assertIn('test "$(git rev-parse FETCH_HEAD)" = "$HEAD_SHA"', fetch["run"])
        self.assertEqual(fetch["env"]["GH_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")
        trusted = next(step for step in steps if step.get("name") == "Check out trusted release verification code")
        self.assertIn('git merge-base --all "$HEAD_SHA" "$MERGE_SHA^1"', trusted["run"])
        self.assertIn('git worktree add --detach', trusted["run"])
        prepare = next(step for step in steps if step.get("name") == "Validate release evidence and prepare assets")
        self.assertIn("--verify-gate", prepare["run"])
        self.assertIn('"$RUNNER_TEMP/trusted-release-code/scripts/release_promotion.py"', prepare["run"])
        self.assertIn('release_profile="$(python3 -c', prepare["run"])
        self.assertIn('--profile "$release_profile"', prepare["run"])
        self.assertEqual(prepare["env"]["GITHUB_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")
        for step in steps:
            if "uses" in step:
                self.assertRegex(step["uses"], r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
        tag = next(step for step in steps if "annotated immutable tag" in step.get("name", ""))
        self.assertIn('"type": "commit"', tag["run"])
        self.assertIn('"object": target_sha', tag["run"])
        self.assertIn('git/tags" --method POST', tag["run"])
        self.assertIn('git/refs" --method POST', tag["run"])
        self.assertIn('Refusing to replace existing release tag', tag["run"])
        self.assertNotIn("--force", tag["run"])
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        sbom_steps = [step for step in steps if step.get("name") == "Export release SBOM"]
        self.assertEqual(len(sbom_steps), 0)
        release = next(step for step in steps if "Publish GitHub Release" in step.get("name", ""))
        self.assertIn("audit-report.md", release["run"])
        self.assertIn("runtime-fingerprint.txt", release["run"])
        self.assertIn("OPTIONAL_ASSETS", release["run"])

    def test_workflow_release_promotion_extracts_candidate_and_root_profile(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is required to validate GitHub Actions workflow structure")
        workflow_path = ROOT / ".github" / "workflows" / "release-promotion.yml"
        workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        prepare = next(step for step in workflow["jobs"]["promote-release"]["steps"]
                       if step.get("name") == "Validate release evidence and prepare assets")
        run_script = prepare["run"]
        expected_py = (
            'data=json.load(open(sys.argv[1], encoding="utf-8")); '
            'print(data.get("candidate", {}).get("profile") or data.get("profile") or "release")'
        )
        self.assertIn(expected_py, run_script)

        # Test execution of the python snippet directly against various control record shapes
        py_code = (
            'import json, sys\n'
            'data = json.loads(sys.argv[1])\n'
            'print(data.get("candidate", {}).get("profile") or data.get("profile") or "release")\n'
        )
        cases = [
            ({"candidate": {"profile": "scientific"}}, "scientific"),
            ({"profile": "scientific"}, "scientific"),
            ({"candidate": {"profile": "minimal"}, "profile": "scientific"}, "minimal"),
            ({"candidate": {}}, "release"),
            ({}, "release"),
        ]
        for data, expected in cases:
            res = subprocess.run(
                [sys.executable, "-c", py_code, json.dumps(data)],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(res.stdout.strip(), expected)


if __name__ == "__main__":
    unittest.main()
