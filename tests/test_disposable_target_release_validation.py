"""End-to-end disposable target validation for scientific release path, SBOM, citation, and containers (Ticket #156)."""

from __future__ import annotations

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

import release_promotion
import export_release_sbom
import verify_release
import install_github_workflows
from doi_guidance import DoiState, DoiStateMachine, validate_citation_cff_doi


class DisposableTargetApi:
    """Simulated GitHub API for a disposable target repository."""

    def __init__(
        self,
        repo_slug: str = "octo/disposable-target",
        sbom_response: tuple[int, dict] | None = None,
    ):
        self.repo_slug = repo_slug
        self.sbom_response = sbom_response
        self.created_tags: list[dict] = []
        self.created_refs: list[dict] = []
        self.created_releases: list[dict] = []
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict | None]:
        self.calls.append((method, path, payload))

        # Pinned Docker actions commit verification
        if method == "GET" and "/git/commits/" in path:
            sha = path.split("/")[-1]
            return 200, {"sha": sha}

        # Dependency Graph SBOM endpoint
        if method == "GET" and path == f"/repos/{self.repo_slug}/dependency-graph/sbom":
            if self.sbom_response is not None:
                return self.sbom_response
            # Default successful SPDX 2.3 payload
            return 200, {
                "sbom": {
                    "spdxVersion": "SPDX-2.3",
                    "dataLicense": "CC0-1.0",
                    "SPDXID": "SPDXRef-DOCUMENT",
                    "name": self.repo_slug,
                    "documentNamespace": f"https://github.com/{self.repo_slug}/dependency_graph/sbom-test",
                    "creationInfo": {
                        "created": "2026-11-09T00:00:00Z",
                        "creators": ["Tool: GitHub.com-Dependency-Graph"],
                    },
                    "packages": [
                        {
                            "SPDXID": "SPDXRef-Package-numpy",
                            "name": "numpy",
                            "versionInfo": "1.26.0",
                            "downloadLocation": "NOASSERTION",
                        },
                        {
                            "SPDXID": "SPDXRef-Package-scipy",
                            "name": "scipy",
                            "versionInfo": "1.11.0",
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

        # Tag creation endpoint
        if method == "POST" and path == f"/repos/{self.repo_slug}/git/tags":
            tag_entry = dict(payload or {}, sha="tag_object_sha_1234567890abcdef")
            self.created_tags.append(tag_entry)
            return 201, tag_entry

        # Ref creation endpoint
        if method == "POST" and path == f"/repos/{self.repo_slug}/git/refs":
            ref_entry = dict(payload or {})
            self.created_refs.append(ref_entry)
            return 201, ref_entry

        # Release creation endpoint
        if method == "POST" and path == f"/repos/{self.repo_slug}/releases":
            release_entry = dict(payload or {}, id=12345)
            self.created_releases.append(release_entry)
            return 201, release_entry

        # Tag query
        if method == "GET" and path.startswith(f"/repos/{self.repo_slug}/git/ref/tags/"):
            tag_name = path.split("/")[-1]
            for ref in self.created_refs:
                if ref.get("ref") == f"refs/tags/{tag_name}":
                    return 200, ref
            return 404, {"message": "Not Found"}

        # Repository info
        if method == "GET" and path == f"/repos/{self.repo_slug}":
            return 200, {
                "full_name": self.repo_slug,
                "default_branch": "main",
                "visibility": "public",
            }

        return 404, {"message": f"Endpoint not mocked: {path}"}


class DisposableTargetHarness:
    """Provisions a disposable git repository target and prepares release evidence."""

    VALID_CFF = (
        "cff-version: 1.2.0\n"
        "message: \"If you use this scientific software, please cite it as below.\"\n"
        "title: \"Disposable Scientific Simulation Package\"\n"
        "version: 1.0.0\n"
        "date-released: 2026-11-09\n"
        "authors:\n"
        "  - family-names: \"Curie\"\n"
        "    given-names: \"Marie\"\n"
        "    orcid: \"https://orcid.org/0000-0000-0000-0001\"\n"
    )

    def __init__(self, repo_slug: str = "octo/disposable-target", version: str = "1.0.0"):
        self.repo_slug = repo_slug
        self.version = version
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.assets_dir = self.root / ".release-assets"
        self._init_git_repo()

    def cleanup(self):
        self.temp_dir.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=self.root, text=True).strip()

    def _init_git_repo(self):
        self.git("init", "-q")
        self.git("config", "user.name", "Disposable Target Maintainer")
        self.git("config", "user.email", "maintainer@example.invalid")
        self.git("config", "commit.gpgsign", "false")

        # Initial commit on main with runtime code
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "scripts" / "runtime.py").write_text("# scientific simulation runtime\nVERSION = '1.0.0'\n")
        (self.root / "CITATION.cff").write_text(self.VALID_CFF, encoding="utf-8")
        self.git("add", "scripts/runtime.py", "CITATION.cff")
        self.git("commit", "-qm", "initial main commit")
        self.git("branch", "-M", "main")

        # Create candidate branch and candidate commit
        self.git("checkout", "-b", "candidate-1.0.0")
        (self.root / "scripts" / "runtime.py").write_text("# scientific simulation runtime\nVERSION = '1.0.0'\n# frozen\n")
        self.git("add", "scripts/runtime.py")
        self.git("commit", "-qm", "candidate freeze")
        self.candidate_sha = self.git("rev-parse", "HEAD")

        # Switch back to main and simulate merge
        self.git("checkout", "main")
        tree_sha = self.git("rev-parse", "candidate-1.0.0^{tree}")
        self.merge_sha = self.git("commit-tree", tree_sha, "-p", "main", "-p", self.candidate_sha, "-m", "Merge PR #42")
        self.git("update-ref", "refs/heads/main", self.merge_sha)
        self.git("checkout", "main")

        # Calculate exact runtime fingerprint at the merge commit
        self.fingerprint = verify_release.compute_runtime_fingerprint(self.root, commit_sha=self.merge_sha)

        # Author trusted Control Record and evidence
        self.control = {
            "version": 1,
            "status": "candidate",
            "candidate": {
                "repository": self.repo_slug,
                "pull_request": 42,
                "target_version": self.version,
                "head_sha": self.candidate_sha,
            },
            "required_gates": {"python_versions": ["3.10", "3.11", "3.12"]},
            "live_evidence": {"runtime_fingerprint": self.fingerprint},
            "operational_constraints": ["Target executed in container-isolated runner"],
            "unverified_scenarios": [],
        }
        self.evidence = dict(self.control, status="active")

        # Author audit report
        self.audit_report_text = (
            f"# Production Release Audit Report: {self.repo_slug}\n\n"
            f"**Pull Request:** [#{self.control['candidate']['pull_request']}]"
            f"(https://github.com/{self.repo_slug}/pull/{self.control['candidate']['pull_request']})\n"
            f"**Candidate Head SHA:** `{self.candidate_sha}`\n"
            f"**Target Version:** `{self.version}`\n"
            f"**Current Release Verdict:** **READY WITH DECLARED OPERATIONAL CONSTRAINTS**\n"
        )

        (self.root / "docs").mkdir(parents=True, exist_ok=True)
        (self.root / "docs" / "release-control.json").write_text(json.dumps(self.control, indent=2))
        (self.root / "docs" / "release-evidence.json").write_text(json.dumps(self.evidence, indent=2))
        (self.root / "docs" / "release-control.md").write_text(self.audit_report_text, encoding="utf-8")

        # Event payload
        self.event = {
            "repository": {"full_name": self.repo_slug},
            "pull_request": {
                "number": 42,
                "merged": True,
                "base": {"ref": "main"},
                "head": {"sha": self.candidate_sha},
                "merge_commit_sha": self.merge_sha,
                "labels": [{"name": "release-candidate"}],
            },
        }

    def execute_promotion(
        self,
        api: DisposableTargetApi,
        profile: str | None = "scientific",
        tag: str | None = None,
    ) -> dict | None:
        """Run prepare_release_payload and simulate annotated tag and release creation."""
        tag = tag or f"v{self.version}"
        payload = release_promotion.prepare_release_payload(
            self.event,
            control_path=self.root / "docs" / "release-control.json",
            evidence_path=self.root / "docs" / "release-evidence.json",
            audit_report_path=self.root / "docs" / "release-control.md",
            output_dir=self.assets_dir,
            repo_root=self.root,
            api=api,
            tag=tag,
            profile=profile,
        )
        if payload is not None and not payload.get("skipped"):
            # Simulate workflow steps: tag creation & release creation
            notes_file = self.assets_dir / "release-notes.md"
            notes = notes_file.read_text(encoding="utf-8") if notes_file.exists() else ""
            status_tag, tag_obj = api.request(
                "POST",
                f"/repos/{self.repo_slug}/git/tags",
                payload={"tag": payload["tag"], "message": notes, "object": payload["target_sha"], "type": "commit"},
            )
            api.request(
                "POST",
                f"/repos/{self.repo_slug}/git/refs",
                payload={"ref": f"refs/tags/{payload['tag']}", "sha": tag_obj.get("sha")},
            )
            api.request(
                "POST",
                f"/repos/{self.repo_slug}/releases",
                payload={
                    "tag_name": payload["tag"],
                    "target_commitish": payload["target_sha"],
                    "name": payload["version"],
                    "body": notes,
                },
            )
        return payload


class DisposableTargetReleaseValidationTests(unittest.TestCase):
    """End-to-end validation of complete scientific release path on a disposable target."""

    def setUp(self):
        self.harness = DisposableTargetHarness()
        self.addCleanup(self.harness.cleanup)
        self.api = DisposableTargetApi(repo_slug=self.harness.repo_slug)

    # -------------------------------------------------------------------------
    # Acceptance Criterion 1: Complete selected release path publication
    # -------------------------------------------------------------------------
    def test_disposable_target_publishes_selected_release_path_with_inspectable_assets(self):
        """A disposable target publishes scientific release path with inspectable tag, release, citation & assets."""
        payload = self.harness.execute_promotion(self.api, profile="scientific", tag="v1.0.0")

        self.assertIsNotNone(payload)
        self.assertEqual(payload["tag"], "v1.0.0")
        self.assertEqual(payload["version"], "1.0.0")
        self.assertEqual(payload["target_sha"], self.harness.merge_sha.lower())
        self.assertEqual(payload["runtime_fingerprint"], self.harness.fingerprint.lower())
        self.assertEqual(sorted(payload["optional_assets"]), ["CITATION.cff", "sbom.spdx.json"])

        # 1. Inspectable Tag and Release creation
        self.assertEqual(len(self.api.created_tags), 1)
        tag_obj = self.api.created_tags[0]
        self.assertEqual(tag_obj["tag"], "v1.0.0")
        self.assertEqual(tag_obj["object"], self.harness.merge_sha.lower())

        self.assertEqual(len(self.api.created_refs), 1)
        self.assertEqual(self.api.created_refs[0]["ref"], "refs/tags/v1.0.0")

        self.assertEqual(len(self.api.created_releases), 1)
        release_obj = self.api.created_releases[0]
        self.assertEqual(release_obj["tag_name"], "v1.0.0")
        self.assertEqual(release_obj["name"], "1.0.0")
        self.assertEqual(release_obj["target_commitish"], self.harness.merge_sha.lower())

        # 2. Inspectable Release Notes & Reproducibility Disclaimer
        notes = (self.harness.assets_dir / "release-notes.md").read_text(encoding="utf-8")
        self.assertIn("Release `1.0.0` for `octo/disposable-target`.", notes)
        self.assertIn(f"Pull request: #{self.harness.control['candidate']['pull_request']}", notes)
        self.assertIn(f"Candidate head: `{self.harness.candidate_sha.lower()}`", notes)
        self.assertIn(f"Merged commit: `{self.harness.merge_sha.lower()}`", notes)
        self.assertIn(f"Runtime fingerprint: `{self.harness.fingerprint.lower()}`", notes)
        self.assertIn("Software Bill of Materials: `sbom.spdx.json`.", notes)
        self.assertIn("Citation metadata: `CITATION.cff`.", notes)
        self.assertIn(
            "This release records verified evidence for the selected commit. It does not prove scientific reproducibility.",
            notes,
        )
        self.assertIn("Operational constraints:", notes)
        self.assertIn("- Target executed in container-isolated runner", notes)

        # 3. Inspectable All 4 Assets Present in output directory
        assets = list(self.harness.assets_dir.glob("*"))
        asset_names = {a.name for a in assets}
        self.assertIn("audit-report.md", asset_names)
        self.assertIn("runtime-fingerprint.txt", asset_names)
        self.assertIn("CITATION.cff", asset_names)
        self.assertIn("sbom.spdx.json", asset_names)
        self.assertIn("release-notes.md", asset_names)

        # Check audit-report.md content
        audit_content = (self.harness.assets_dir / "audit-report.md").read_text(encoding="utf-8")
        self.assertEqual(audit_content, self.harness.audit_report_text)

        # Check runtime-fingerprint.txt content
        fp_content = (self.harness.assets_dir / "runtime-fingerprint.txt").read_text(encoding="utf-8").strip()
        self.assertEqual(fp_content, self.harness.fingerprint.lower())

        # Check CITATION.cff content
        cff_content = (self.harness.assets_dir / "CITATION.cff").read_text(encoding="utf-8")
        self.assertIn("cff-version: 1.2.0", cff_content)
        self.assertIn("title: \"Disposable Scientific Simulation Package\"", cff_content)
        self.assertIn("version: 1.0.0", cff_content)
        self.assertIn("family-names: \"Curie\"", cff_content)

        # Check sbom.spdx.json content
        sbom_data = json.loads((self.harness.assets_dir / "sbom.spdx.json").read_text(encoding="utf-8"))
        self.assertEqual(sbom_data["spdxVersion"], "SPDX-2.3")
        self.assertEqual(sbom_data["SPDXID"], "SPDXRef-DOCUMENT")
        pkg_names = {p["name"] for p in sbom_data["packages"]}
        self.assertIn("numpy", pkg_names)
        self.assertIn("scipy", pkg_names)

    # -------------------------------------------------------------------------
    # Acceptance Criterion 2: Explicit demonstration of SBOM failure & default no container
    # -------------------------------------------------------------------------
    def test_sbom_failure_when_dependency_graph_disabled_aborts_and_leaves_recovery(self):
        """SBOM failure when Dependency Graph is disabled aborts publication, uploads no assets, and creates no tag."""
        # Clean output dir
        if self.harness.assets_dir.exists():
            for f in self.harness.assets_dir.iterdir():
                f.unlink()
            self.harness.assets_dir.rmdir()

        # Simulated API returning 404 Dependency graph not enabled
        disabled_api = DisposableTargetApi(
            repo_slug=self.harness.repo_slug,
            sbom_response=(404, {"message": "Dependency graph is not enabled for this repository"}),
        )

        with self.assertRaises(release_promotion.ReleasePromotionError) as ctx:
            self.harness.execute_promotion(disabled_api, profile="scientific", tag="v1.0.0")

        err = ctx.exception
        err_msg = str(err).lower()
        self.assertIn("cannot generate release sbom", err_msg)
        # Actionable recovery instructions must be present
        self.assertTrue(bool(err.recovery))
        recovery_msg = err.recovery.lower()
        self.assertIn("enable dependency graph", recovery_msg)
        self.assertIn("settings", recovery_msg)

        # Invariant checks: No assets uploaded/written, no tag created, no release created
        self.assertFalse(self.harness.assets_dir.exists())
        self.assertEqual(len(disabled_api.created_tags), 0)
        self.assertEqual(len(disabled_api.created_refs), 0)
        self.assertEqual(len(disabled_api.created_releases), 0)

    def test_sbom_failure_when_dependency_graph_unindexed_aborts_and_leaves_recovery(self):
        """SBOM failure when Dependency Graph has no indexed packages aborts publication with recovery instructions."""
        # Clean output dir
        if self.harness.assets_dir.exists():
            for f in self.harness.assets_dir.iterdir():
                f.unlink()
            self.harness.assets_dir.rmdir()

        # Simulated API returning SPDX document with empty packages list (unindexed)
        unindexed_api = DisposableTargetApi(
            repo_slug=self.harness.repo_slug,
            sbom_response=(200, {
                "sbom": {
                    "spdxVersion": "SPDX-2.3",
                    "dataLicense": "CC0-1.0",
                    "SPDXID": "SPDXRef-DOCUMENT",
                    "name": self.harness.repo_slug,
                    "documentNamespace": f"https://github.com/{self.harness.repo_slug}/dependency_graph/sbom-empty",
                    "creationInfo": {
                        "created": "2026-11-09T00:00:00Z",
                        "creators": ["Tool: GitHub.com-Dependency-Graph"],
                    },
                    "packages": [],
                }
            }),
        )

        with self.assertRaises(release_promotion.ReleasePromotionError) as ctx:
            self.harness.execute_promotion(unindexed_api, profile="scientific", tag="v1.0.0")

        err = ctx.exception
        err_msg = str(err).lower()
        self.assertIn("no packages", err_msg)
        # Actionable recovery instructions for manifests / indexing
        self.assertTrue(bool(err.recovery))
        recovery_msg = err.recovery.lower()
        self.assertIn("dependency manifests", recovery_msg)

        # Invariant checks: No assets uploaded/written, no tag created, no release created
        self.assertFalse(self.harness.assets_dir.exists())
        self.assertEqual(len(unindexed_api.created_tags), 0)
        self.assertEqual(len(unindexed_api.created_refs), 0)
        self.assertEqual(len(unindexed_api.created_releases), 0)

    def test_default_install_on_target_contains_no_container_workflow(self):
        """Default installation on the target repository stages no container workflow."""
        class MockInstallApi:
            def request(self, method: str, path: str, payload: dict | None = None):
                if method == "GET" and path == f"/repos/{self_slug}":
                    return 200, {"full_name": self_slug, "default_branch": "main"}
                if method == "GET" and "/git/ref/heads/main" in path:
                    return 200, {"object": {"sha": "1111111111111111111111111111111111111111"}}
                if method == "GET" and path == f"/repos/{self_slug}/branches/main":
                    return 200, {"commit": {"sha": "1111111111111111111111111111111111111111"}}
                if method == "GET" and "/git/commits/" in path:
                    sha = path.split("/")[-1]
                    return 200, {"sha": sha}
                if method == "GET" and "/contents/" in path:
                    return 404, {"message": "Not Found"}
                if method == "GET" and "/pulls" in path:
                    return 200, []
                return 404, None

        self_slug = self.harness.repo_slug
        api = MockInstallApi()
        default_policy = {
            "version": 1,
            "workflows": [
                {
                    "template": "pr-ci",
                    "template_version": 1,
                    "filename": "ci.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": "11bd71901bbe5b1630ceea73d27597364c9af683",
                    "commands": ["python3 -m unittest discover -s tests"],
                }
            ],
        }

        plan = install_github_workflows.install(api, self_slug, default_policy, apply=False)
        staged_paths = [change["path"] for change in plan["changes"]]

        # Exactly 1 workflow staged: ci.yml
        self.assertEqual(len(staged_paths), 1)
        self.assertEqual(staged_paths[0], ".github/workflows/ci.yml")

        # Zero container workflows staged by default
        for path in staged_paths:
            self.assertNotIn("container", path.lower())
            self.assertNotIn("ghcr", path.lower())

    def test_opt_in_container_selection_on_target_stages_least_privilege_workflow(self):
        """Explicit container selection on the target stages least-privilege GHCR workflow."""
        class MockContainerApi:
            def request(self, method: str, path: str, payload: dict | None = None):
                if method == "GET" and path == f"/repos/{self_slug}":
                    return 200, {"full_name": self_slug, "default_branch": "main"}
                if method == "GET" and "/git/ref/heads/main" in path:
                    return 200, {"object": {"sha": "1111111111111111111111111111111111111111"}}
                if method == "GET" and path == f"/repos/{self_slug}/branches/main":
                    return 200, {"commit": {"sha": "1111111111111111111111111111111111111111"}}
                if method == "GET" and "/git/commits/" in path:
                    sha = path.split("/")[-1]
                    return 200, {"sha": sha}
                if method == "GET" and "/contents/" in path:
                    return 404, {"message": "Not Found"}
                if method == "GET" and "/pulls" in path:
                    return 200, []
                return 404, None

        self_slug = self.harness.repo_slug
        api = MockContainerApi()
        container_policy = {
            "version": 1,
            "workflows": [
                {
                    "template": "container-publish",
                    "template_version": 1,
                    "filename": "container-publish.yml",
                    "triggers": ["pull_request"],
                    "checkout_sha": "11bd71901bbe5b1630ceea73d27597364c9af683",
                    "login_action_sha": "9780b0c442fbb1117ed29e0efdff1e18412f7567",
                    "build_action_sha": "471d1dc4e07e5cdedd4c2171150001c434f0b7a4",
                }
            ],
        }

        plan = install_github_workflows.install(api, self_slug, container_policy, apply=False)
        staged_paths = [change["path"] for change in plan["changes"]]

        self.assertIn(".github/workflows/container-publish.yml", staged_paths)
        container_change = next(c for c in plan["changes"] if c["path"] == ".github/workflows/container-publish.yml")
        self.assertEqual(container_change["operation"], "create")

        # Verify rendered container workflow content matches planned desired_sha256
        import hashlib
        rendered_bytes = install_github_workflows.render(container_policy["workflows"][0])
        self.assertEqual(hashlib.sha256(rendered_bytes).hexdigest(), container_change["desired_sha256"])
        content = rendered_bytes.decode("utf-8")

        # Verify container workflow properties:
        # Pinned SHAs
        self.assertIn("actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683", content)
        self.assertIn("docker/login-action@9780b0c442fbb1117ed29e0efdff1e18412f7567", content)
        self.assertIn("docker/build-push-action@471d1dc4e07e5cdedd4c2171150001c434f0b7a4", content)
        # Least privilege
        self.assertIn("contents: read", content)
        self.assertIn("packages: write", content)
        # GHCR registry
        self.assertIn('registry: "ghcr.io"', content)
        self.assertIn("push: ${{ github.event_name != 'pull_request' }}", content)
        self.assertIn("if: github.event_name != 'pull_request'", content)

    # -------------------------------------------------------------------------
    # Acceptance Criterion: Citation metadata & DOI governance on target
    # -------------------------------------------------------------------------
    def test_citation_doi_governance_on_target_rejects_unverified_placeholder_doi(self):
        """Target release candidate with unverified placeholder DOI in CITATION.cff aborts publication."""
        # Case 1: Malformed placeholder DOI string fails CITATION.cff validation during promotion
        malformed_cff = self.harness.VALID_CFF + "doi: placeholder-10.1234/unverified\n"
        (self.harness.root / "CITATION.cff").write_text(malformed_cff, encoding="utf-8")

        with self.assertRaises(release_promotion.ReleasePromotionError) as ctx:
            self.harness.execute_promotion(self.api, profile="scientific", tag="v1.0.0")

        self.assertIn("doi", str(ctx.exception).lower())
        self.assertEqual(len(self.api.created_tags), 0)

        # Case 2: Syntactically valid DOI pattern (e.g. Zenodo) without ISSUED doi_state aborts publication
        zenodo_cff = self.harness.VALID_CFF + "doi: 10.5281/zenodo.1234567\n"
        (self.harness.root / "CITATION.cff").write_text(zenodo_cff, encoding="utf-8")

        with self.assertRaises(release_promotion.ReleasePromotionError) as ctx_zenodo:
            self.harness.execute_promotion(self.api, profile="scientific", tag="v1.0.0")

        self.assertIn("citation.cff", str(ctx_zenodo.exception).lower())
        self.assertIn("never recorded", str(ctx_zenodo.exception).lower())
        self.assertEqual(len(self.api.created_tags), 0)

        # Case 3: Guard check raises DoiError if a DOI is recorded when state is not ISSUED
        with self.assertRaises(Exception) as doi_ctx:
            validate_citation_cff_doi(
                self.harness.VALID_CFF + "doi: 10.5281/zenodo.1234567\n",
                doi_state=DoiState.REQUESTED,
            )
        self.assertIn("never recorded", str(doi_ctx.exception))

    def test_citation_with_verified_issued_doi_succeeds_on_target(self):
        """Target release candidate with verified ISSUED doi_state and matching DOI succeeds in publication."""
        # Clean output dir
        if self.harness.assets_dir.exists():
            for f in self.harness.assets_dir.iterdir():
                f.unlink()
            self.harness.assets_dir.rmdir()

        verified_doi = "10.5281/zenodo.1234567"
        cff_with_doi = self.harness.VALID_CFF + f"doi: {verified_doi}\n"
        (self.harness.root / "CITATION.cff").write_text(cff_with_doi, encoding="utf-8")

        self.harness.control["doi_state"] = "issued"
        self.harness.control["doi"] = verified_doi
        self.harness.evidence["doi_state"] = "issued"
        self.harness.evidence["doi"] = verified_doi
        (self.harness.root / "docs" / "release-control.json").write_text(json.dumps(self.harness.control, indent=2))
        (self.harness.root / "docs" / "release-evidence.json").write_text(json.dumps(self.harness.evidence, indent=2))

        payload = self.harness.execute_promotion(self.api, profile="scientific", tag="v1.0.0")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["optional_assets"], ["CITATION.cff", "sbom.spdx.json"])
        cff_text = (self.harness.assets_dir / "CITATION.cff").read_text(encoding="utf-8")
        self.assertIn(f"doi: {verified_doi}", cff_text)
        notes = (self.harness.assets_dir / "release-notes.md").read_text(encoding="utf-8")
        self.assertIn(f"DOI: `{verified_doi}`", notes)

    def test_dynamic_profile_selection_on_target_derives_scientific(self):
        """Candidate object declaring 'profile': 'scientific' is dynamically promoted without explicit workflow arg."""
        # Clean output dir
        if self.harness.assets_dir.exists():
            for f in self.harness.assets_dir.iterdir():
                f.unlink()
            self.harness.assets_dir.rmdir()

        # Update Control Record candidate object with profile: scientific
        self.harness.control["candidate"]["profile"] = "scientific"
        self.harness.evidence["candidate"]["profile"] = "scientific"
        # Reset doi_state if previously modified
        self.harness.control.pop("doi_state", None)
        self.harness.control.pop("doi", None)
        self.harness.evidence.pop("doi_state", None)
        self.harness.evidence.pop("doi", None)
        (self.harness.root / "docs" / "release-control.json").write_text(json.dumps(self.harness.control, indent=2))
        (self.harness.root / "docs" / "release-evidence.json").write_text(json.dumps(self.harness.evidence, indent=2))

        # Reset clean CITATION.cff without DOI
        (self.harness.root / "CITATION.cff").write_text(self.harness.VALID_CFF, encoding="utf-8")

        # Execute promotion without specifying profile (profile=None)
        payload = self.harness.execute_promotion(self.api, profile=None, tag="v1.0.0")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["optional_assets"], ["CITATION.cff", "sbom.spdx.json"])
        self.assertTrue((self.harness.assets_dir / "CITATION.cff").is_file())
        self.assertTrue((self.harness.assets_dir / "sbom.spdx.json").is_file())

    def test_citation_doi_state_machine_transitions_on_target(self):
        """Verify the five canonical DOI states and guard rules on the target."""
        # UNSELECTED -> REQUESTED -> PENDING -> ISSUED
        sm = DoiStateMachine(DoiState.UNSELECTED)
        self.assertEqual(sm.state, DoiState.UNSELECTED)

        sm = sm.transition(DoiState.REQUESTED)
        self.assertEqual(sm.state, DoiState.REQUESTED)

        sm = sm.transition(DoiState.PENDING)
        self.assertEqual(sm.state, DoiState.PENDING)

        # Cannot set DOI on PENDING
        with self.assertRaises(Exception):
            sm.transition(DoiState.PENDING, doi="10.5281/zenodo.1234567")

        # Transition to ISSUED with verified DOI
        sm = sm.transition(DoiState.ISSUED, doi="10.5281/zenodo.1234567")
        self.assertEqual(sm.state, DoiState.ISSUED)
        self.assertEqual(sm.doi, "10.5281/zenodo.1234567")


if __name__ == "__main__":
    unittest.main()
