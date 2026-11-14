"""Tests for P6.7 (#185): Verify optional release and citation guidance."""

from __future__ import annotations

import hashlib
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
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import export_release_sbom  # noqa: E402
import install_github_workflows  # noqa: E402
import release_promotion  # noqa: E402
import verify_release  # noqa: E402
from doi_guidance import (  # noqa: E402
    DOI_PATTERN,
    DoiError,
    DoiState,
    DoiStateMachine,
    format_doi_release_notes,
    update_citation_cff_doi,
    validate_citation_cff_doi,
    validate_release_doi_metadata,
)

README_PATH = ROOT / "README.md"
RELEASE_EVIDENCE_DOC = ROOT / "docs" / "release-evidence.md"
DOI_GUIDANCE_DOC = ROOT / "docs" / "doi-guidance.md"


class _DisposableMockApi:
    """Simulated GitHub API for disposable target release and workflow exercises."""

    def __init__(
        self,
        repo_slug: str = "octo/disposable-sci-release",
        sbom_response: tuple[int, dict | None] | None = None,
    ):
        self.repo_slug = repo_slug
        self.sbom_response = sbom_response
        self.created_tags: list[dict] = []
        self.created_refs: list[dict] = []
        self.created_releases: list[dict] = []

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict | list | None]:
        if method == "GET" and "/git/commits/" in path:
            sha = path.split("/")[-1]
            return 200, {"sha": sha}
        if method == "GET" and path == f"/repos/{self.repo_slug}":
            return 200, {"full_name": self.repo_slug, "default_branch": "main", "visibility": "public"}
        if method == "GET" and "/git/ref/heads/main" in path:
            return 200, {"object": {"sha": "1111111111111111111111111111111111111111"}}
        if method == "GET" and "/contents/" in path:
            return 404, {"message": "Not Found"}
        if method == "GET" and "/pulls" in path:
            return 200, []
        if method == "GET" and path == f"/repos/{self.repo_slug}/dependency-graph/sbom":
            if self.sbom_response is not None:
                return self.sbom_response
            return 200, {
                "sbom": {
                    "spdxVersion": "SPDX-2.3",
                    "dataLicense": "CC0-1.0",
                    "SPDXID": "SPDXRef-DOCUMENT",
                    "name": self.repo_slug,
                    "documentNamespace": f"https://github.com/{self.repo_slug}/dependency_graph/sbom-v1.0.0",
                    "creationInfo": {
                        "created": "2026-11-11T00:00:00Z",
                        "creators": ["Tool: GitHub.com-Dependency-Graph"],
                    },
                    "packages": [
                        {
                            "SPDXID": "SPDXRef-Package-numpy",
                            "name": "numpy",
                            "versionInfo": "1.26.4",
                            "downloadLocation": "NOASSERTION",
                        },
                        {
                            "SPDXID": "SPDXRef-Package-scipy",
                            "name": "scipy",
                            "versionInfo": "1.12.0",
                            "downloadLocation": "NOASSERTION",
                        },
                    ],
                    "relationships": [
                        {
                            "spdxElementId": "SPDXRef-DOCUMENT",
                            "relatedSpdxElement": "SPDXRef-Package-numpy",
                            "relationshipType": "DESCRIBES",
                        }
                    ],
                }
            }
        if method == "POST" and path == f"/repos/{self.repo_slug}/git/tags":
            tag_obj = dict(payload or {}, sha="a" * 40)
            self.created_tags.append(tag_obj)
            return 201, tag_obj
        if method == "POST" and path == f"/repos/{self.repo_slug}/git/refs":
            ref_obj = dict(payload or {})
            self.created_refs.append(ref_obj)
            return 201, ref_obj
        if method == "POST" and path == f"/repos/{self.repo_slug}/releases":
            rel_obj = dict(payload or {}, id=101)
            self.created_releases.append(rel_obj)
            return 201, rel_obj
        return 404, {"message": f"Unmocked endpoint: {method} {path}"}


class P6185ReleaseCitationGuidanceTests(unittest.TestCase):
    """Acceptance tests for P6.7 (#185) documentation and disposable release execution."""

    # -------------------------------------------------------------------------
    # Criterion 1: Explicit-tag release, citation metadata, public Zenodo,
    # private manual deposit, optional assets/profiles, SBOM, and containers
    # -------------------------------------------------------------------------
    def test_ac1_docs_and_readme_cover_all_publication_features(self):
        readme = README_PATH.read_text(encoding="utf-8")
        rel_doc = RELEASE_EVIDENCE_DOC.read_text(encoding="utf-8")
        doi_doc = DOI_GUIDANCE_DOC.read_text(encoding="utf-8")

        # README coverage
        for token in (
            "--tag v<target_version>",
            "docs/release-control.json",
            "minimal",
            "release",
            "scientific",
            "CITATION.cff",
            "sbom.spdx.json",
            "unselected",
            "requested",
            "pending",
            "issued",
            "unavailable",
            "allow_fallback=False, require_packages=True",
            "container-publish",
            "ghcr.io",
            "https://zenodo.org/deposit/new",
        ):
            self.assertIn(token, readme, f"README.md missing required P6.7 token: {token}")

        # docs/release-evidence.md coverage
        for token in (
            "--tag v<target_version>",
            "candidate.target_version",
            "Refusing to replace existing release tag",
            "annotated immutable tag",
            "cff-version",
            "message",
            "title",
            "authors",
            "date-released",
            "YYYY-MM-DD",
            "audit-report.md",
            "runtime-fingerprint.txt",
            "release-notes.md",
            "sbom.spdx.json",
            "CITATION.cff",
            "scripts/export_release_sbom.py",
            "SPDX-2.3",
            ".github/workflows/container-publish.yml",
            "ghcr.io/",
            "workflow_dispatch",
        ):
            self.assertIn(token, rel_doc, f"docs/release-evidence.md missing required token: {token}")

        # docs/doi-guidance.md coverage
        for token in (
            "Zenodo",
            "https://zenodo.org/deposit/new",
            "https://doi.org/",
            "Concept DOI",
            "Version DOI",
            "Restricted Access",
            "Embargoed Access",
            "CITATION.cff",
            "docs/release-control.json",
            "docs/release-evidence.json",
        ):
            self.assertIn(token, doi_doc, f"docs/doi-guidance.md missing required token: {token}")

    # -------------------------------------------------------------------------
    # Criterion 2: Distinguish issued DOI from requested, pending, unavailable,
    # or unselected (not-selected) DOI and where maintainer confirms it
    # -------------------------------------------------------------------------
    def test_ac2_doi_states_confirmation_locations_and_never_record_before_issued(self):
        rel_doc = RELEASE_EVIDENCE_DOC.read_text(encoding="utf-8")
        doi_doc = DOI_GUIDANCE_DOC.read_text(encoding="utf-8")

        for state_name in ("unselected", "requested", "pending", "issued", "unavailable"):
            self.assertIn(state_name, doi_doc.lower())
            self.assertIn(state_name, rel_doc.lower())

        # Must explicitly mention not-selected / unselected equivalence and regex pattern
        self.assertIn("not-selected", doi_doc.lower())
        self.assertIn("^10\\.\\d{4,9}/\\s+$", doi_doc.lower())
        self.assertIn("https://doi.org/<doi>", doi_doc.lower())

        # Must explain all 5 confirmation locations:
        # 1. Zenodo dashboard, 2. https://doi.org/<doi>, 3. CITATION.cff,
        # 4. docs/release-control.json, 5. docs/release-evidence.json
        for location in (
            "Zenodo",
            "https://doi.org/",
            "CITATION.cff",
            "docs/release-control.json",
            "docs/release-evidence.json",
        ):
            self.assertIn(location, doi_doc, f"docs/doi-guidance.md must explain confirmation at {location}")
            self.assertIn(location, rel_doc, f"docs/release-evidence.md must reference confirmation at {location}")

        # Never record DOI before issued verification
        self.assertIn("never recorded", doi_doc.lower())
        self.assertIn("never recorded", rel_doc.lower())

    # -------------------------------------------------------------------------
    # Criterion 3: Fail-closed SBOM abort before creating Git tag or Release
    # and step-by-step recovery
    # -------------------------------------------------------------------------
    def test_ac3_sbom_fail_closed_abort_and_step_by_step_recovery_documented(self):
        rel_doc = RELEASE_EVIDENCE_DOC.read_text(encoding="utf-8")
        for required in (
            "allow_fallback=False, require_packages=True",
            "HTTP 404",
            "HTTP 401/403",
            "zero packages",
            "before creating the Git tag or GitHub Release",
            "Settings -> Code security -> Dependency graph",
            '"profile": "minimal"',
        ):
            self.assertIn(
                required,
                rel_doc,
                f"docs/release-evidence.md must document SBOM fail-closed contract & recovery: {required}",
            )

    # -------------------------------------------------------------------------
    # Criterion 4: Concrete verification step and honest unavailable /
    # not-selected path for each publication option
    # -------------------------------------------------------------------------
    def test_ac4_each_option_has_verification_step_and_honest_unavailable_or_not_selected_path(self):
        rel_doc = RELEASE_EVIDENCE_DOC.read_text(encoding="utf-8")
        self.assertIn(
            "## 6. Publication Feature Selection, Verification Steps, and Unavailable / Not-Selected Paths",
            rel_doc,
        )
        for option in (
            "`minimal`",
            "`release`",
            "`scientific`",
            "Public Zenodo",
            "Private-source manual deposit",
            "`container-publish`",
        ):
            self.assertIn(option, rel_doc, f"Selection & verification matrix must cover {option}")
        self.assertIn("not-selected", rel_doc.lower())
        self.assertIn("unavailable", rel_doc.lower())

    # -------------------------------------------------------------------------
    # Criterion 5: Disposable target environment exercise recorded in
    # docs/release-evidence.md using Observed / Inferred / Unverified format
    # and verified end-to-end in this test module
    # -------------------------------------------------------------------------
    def test_ac5_disposable_release_exercise_recorded_with_observed_inferred_unverified(self):
        rel_doc = RELEASE_EVIDENCE_DOC.read_text(encoding="utf-8")
        self.assertIn(
            "## 7. Disposable Target Environment Validation Exercise (`Observed` / `Inferred` / `Unverified`)",
            rel_doc,
        )
        for marker in ("**Observed**", "**Inferred**", "**Unverified**"):
            self.assertIn(marker, rel_doc, f"docs/release-evidence.md must use {marker} evidence demarcation")

        for recorded_item in (
            "`v1.0.0`",
            "`audit-report.md`",
            "`runtime-fingerprint.txt`",
            "`release-notes.md`",
            "`CITATION.cff`",
            "`sbom.spdx.json`",
            "Zenodo",
            "ghcr.io",
        ):
            self.assertIn(recorded_item, rel_doc, f"Validation record must include {recorded_item}")

    def test_ac5_disposable_end_to_end_execution_across_profiles_sbom_abort_doi_and_containers(self):
        """Exercise minimal, release, scientific, SBOM fail-closed abort, DoiState transitions, and container-publish."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)

            def git(*args: str) -> str:
                return subprocess.check_output(["git", *args], cwd=root, text=True).strip()

            git("init", "-q")
            git("config", "user.name", "Disposable Maintainer")
            git("config", "user.email", "maintainer@example.invalid")
            git("config", "commit.gpgsign", "false")

            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "solver.py").write_text(" VERSION = '1.0.0'\n", encoding="utf-8")
            cff_without_doi = (
                "cff-version: 1.2.0\n"
                'message: "If you use this software, please cite it as below."\n'
                'title: "Disposable Scientific Solver"\n'
                "version: 1.0.0\n"
                "date-released: 2026-11-11\n"
                "authors:\n"
                '  - family-names: "Noether"\n'
                '    given-names: "Emmy"\n'
            )
            (root / "CITATION.cff").write_text(cff_without_doi, encoding="utf-8")
            git("add", "scripts/solver.py", "CITATION.cff")
            git("commit", "-qm", "initial main")
            git("branch", "-M", "main")

            git("checkout", "-qb", "candidate-v1.0.0")
            (root / "scripts" / "solver.py").write_text("VERSION = '1.0.0'\n", encoding="utf-8")
            git("add", "scripts/solver.py")
            git("commit", "-qm", "freeze candidate v1.0.0")
            candidate_sha = git("rev-parse", "HEAD")

            git("checkout", "-q", "main")
            tree_sha = git("rev-parse", "candidate-v1.0.0^{tree}")
            merge_sha = git("commit-tree", tree_sha, "-p", "main", "-p", candidate_sha, "-m", "Merge PR #42")
            git("update-ref", "refs/heads/main", merge_sha)
            git("checkout", "-q", "main")

            fingerprint = verify_release.compute_runtime_fingerprint(root, commit_sha=merge_sha)
            repo_slug = "octo/disposable-sci-release"

            base_control = {
                "version": 1,
                "status": "candidate",
                "candidate": {
                    "repository": repo_slug,
                    "pull_request": 42,
                    "target_version": "1.0.0",
                    "head_sha": candidate_sha,
                },
                "required_gates": {"python_versions": ["3.10", "3.11", "3.12"]},
                "live_evidence": {"runtime_fingerprint": fingerprint},
                "operational_constraints": ["CPU-only runner smoke validation"],
                "unverified_scenarios": ["Multi-node HPC scaling"],
            }
            base_evidence = dict(base_control, status="active")
            audit_report = (
                f"# Production Release Audit Report: {repo_slug}\n\n"
                f"**Pull Request:** [#42](https://github.com/{repo_slug}/pull/42)\n"
                f"**Candidate Head SHA:** `{candidate_sha}`\n"
                f"**Target Version:** `1.0.0`\n"
                f"**Current Release Verdict:** **READY WITH DECLARED OPERATIONAL CONSTRAINTS**\n"
            )

            docs_dir = root / "docs"
            docs_dir.mkdir(parents=True, exist_ok=True)
            control_path = docs_dir / "release-control.json"
            evidence_path = docs_dir / "release-evidence.json"
            report_path = docs_dir / "release-control.md"
            control_path.write_text(json.dumps(base_control, indent=2), encoding="utf-8")
            evidence_path.write_text(json.dumps(base_evidence, indent=2), encoding="utf-8")
            report_path.write_text(audit_report, encoding="utf-8")

            event = {
                "repository": {"full_name": repo_slug},
                "pull_request": {
                    "number": 42,
                    "merged": True,
                    "base": {"ref": "main"},
                    "head": {"sha": candidate_sha},
                    "merge_commit_sha": merge_sha,
                    "labels": [{"name": "release-candidate"}],
                },
            }

            api = _DisposableMockApi(repo_slug=repo_slug)

            # 1. Exercise `minimal` profile
            minimal_dir = root / "out-minimal"
            minimal_payload = release_promotion.prepare_release_payload(
                event,
                control_path=control_path,
                evidence_path=evidence_path,
                audit_report_path=report_path,
                output_dir=minimal_dir,
                repo_root=root,
                api=api,
                tag="v1.0.0",
                profile="minimal",
            )
            self.assertEqual(minimal_payload["tag"], "v1.0.0")
            self.assertEqual(minimal_payload["optional_assets"], [])
            self.assertEqual(
                sorted(p.name for p in minimal_dir.iterdir()),
                ["audit-report.md", "release-notes.md", "runtime-fingerprint.txt"],
            )

            # 2. Exercise `release` profile
            release_dir = root / "out-release"
            release_payload = release_promotion.prepare_release_payload(
                event,
                control_path=control_path,
                evidence_path=evidence_path,
                audit_report_path=report_path,
                output_dir=release_dir,
                repo_root=root,
                api=api,
                tag="v1.0.0",
                profile="release",
            )
            self.assertEqual(release_payload["optional_assets"], ["sbom.spdx.json"])
            self.assertEqual(
                sorted(p.name for p in release_dir.iterdir()),
                ["audit-report.md", "release-notes.md", "runtime-fingerprint.txt", "sbom.spdx.json"],
            )

            # 3. Exercise `scientific` profile (with DoiState.UNSELECTED -> no DOI in CITATION.cff)
            sci_dir = root / "out-scientific"
            sci_payload = release_promotion.prepare_release_payload(
                event,
                control_path=control_path,
                evidence_path=evidence_path,
                audit_report_path=report_path,
                output_dir=sci_dir,
                repo_root=root,
                api=api,
                tag="v1.0.0",
                profile="scientific",
            )
            self.assertEqual(sci_payload["optional_assets"], ["CITATION.cff", "sbom.spdx.json"])
            self.assertEqual(
                sorted(p.name for p in sci_dir.iterdir()),
                ["CITATION.cff", "audit-report.md", "release-notes.md", "runtime-fingerprint.txt", "sbom.spdx.json"],
            )
            sci_notes = (sci_dir / "release-notes.md").read_text(encoding="utf-8")
            self.assertIn("It does not prove scientific reproducibility.", sci_notes)

            # 4. Exercise fail-closed SBOM abort (HTTP 404 disabled & HTTP 200 empty packages)
            for label, bad_resp, expected_substr in (
                ("404-disabled", (404, {"message": "Dependency graph is not enabled."}), "enable dependency graph"),
                (
                    "empty-packages",
                    (
                        200,
                        {
                            "sbom": {
                                "spdxVersion": "SPDX-2.3",
                                "dataLicense": "CC0-1.0",
                                "SPDXID": "SPDXRef-DOCUMENT",
                                "name": repo_slug,
                                "packages": [],
                            }
                        },
                    ),
                    "dependency manifests",
                ),
            ):
                abort_dir = root / f"out-abort-{label}"
                bad_api = _DisposableMockApi(repo_slug=repo_slug, sbom_response=bad_resp)
                with self.assertRaises(release_promotion.ReleasePromotionError) as ctx:
                    release_promotion.prepare_release_payload(
                        event,
                        control_path=control_path,
                        evidence_path=evidence_path,
                        audit_report_path=report_path,
                        output_dir=abort_dir,
                        repo_root=root,
                        api=bad_api,
                        tag="v1.0.0",
                        profile="scientific",
                    )
                self.assertFalse(abort_dir.exists(), f"Output dir must not be created on SBOM abort ({label})")
                self.assertIn(expected_substr, str(ctx.exception).lower())

            # 5. Exercise DoiState transitions and guard rules
            sm = DoiStateMachine(DoiState.UNSELECTED)
            sm = sm.transition(DoiState.REQUESTED)
            sm = sm.transition(DoiState.PENDING)
            with self.assertRaises(DoiError):
                sm.transition(DoiState.PENDING, doi="10.5281/zenodo.1234567")

            verified_doi = "10.5281/zenodo.1234567"
            sm_issued = sm.transition(DoiState.ISSUED, doi=verified_doi)
            self.assertEqual(sm_issued.state, DoiState.ISSUED)
            self.assertEqual(sm_issued.doi, verified_doi)

            # Update CITATION.cff and Control Records to ISSUED and promote
            cff_issued = update_citation_cff_doi(cff_without_doi, doi_state=DoiState.ISSUED, doi=verified_doi)
            (root / "CITATION.cff").write_text(cff_issued, encoding="utf-8")
            control_issued = dict(base_control, doi_state="issued", doi=verified_doi)
            evidence_issued = dict(base_evidence, doi_state="issued", doi=verified_doi)
            control_path.write_text(json.dumps(control_issued, indent=2), encoding="utf-8")
            evidence_path.write_text(json.dumps(evidence_issued, indent=2), encoding="utf-8")

            issued_dir = root / "out-issued"
            issued_payload = release_promotion.prepare_release_payload(
                event,
                control_path=control_path,
                evidence_path=evidence_path,
                audit_report_path=report_path,
                output_dir=issued_dir,
                repo_root=root,
                api=api,
                tag="v1.0.0",
                profile="scientific",
            )
            self.assertEqual(issued_payload["tag"], "v1.0.0")
            self.assertIn(f"DOI: `{verified_doi}`", (issued_dir / "release-notes.md").read_text(encoding="utf-8"))

            # 6. Exercise container-publish workflow rendering & staging
            container_wf = {
                "template": "container-publish",
                "template_version": 1,
                "filename": "container-publish.yml",
                "triggers": ["pull_request", "workflow_dispatch"],
                "checkout_sha": install_github_workflows.DEFAULT_CHECKOUT_SHA,
                "login_action_sha": install_github_workflows.DEFAULT_CONTAINER_LOGIN_SHA,
                "build_action_sha": install_github_workflows.DEFAULT_CONTAINER_BUILD_SHA,
            }
            rendered_container = install_github_workflows.render(container_wf).decode("utf-8")
            self.assertIn("# sciflow-cicd workflow-template=container-publish version=1", rendered_container)
            self.assertIn("registry: ghcr.io", rendered_container)
            self.assertIn("packages: write", rendered_container)
            self.assertIn("push: ${{ github.event_name != 'pull_request' }}", rendered_container)


if __name__ == "__main__":
    unittest.main()
