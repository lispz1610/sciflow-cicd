#!/usr/bin/env python3
"""Prepare a fail-closed GitHub Release payload from trusted Control Records."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.verify_release import (  # noqa: E402
    ApiError,
    FINGERPRINT_PATTERN,
    GitHubApi,
    RecordValidationError,
    compute_runtime_fingerprint,
    parse_release_record,
    sanitize_credentials,
    verify_release_report,
)
from scripts.configure_github_repo import get_pages  # noqa: E402

try:
    from scripts.doi_guidance import (
        DOI_PATTERN,
        DoiError,
        DoiState,
        format_doi_release_notes,
        parse_cff_metadata,
        parse_cff_scalars,
        validate_citation_cff_doi,
        validate_release_doi_metadata,
    )
except ImportError:
    from doi_guidance import (
        DOI_PATTERN,
        DoiError,
        DoiState,
        format_doi_release_notes,
        parse_cff_metadata,
        parse_cff_scalars,
        validate_citation_cff_doi,
        validate_release_doi_metadata,
    )

try:
    from scripts.export_release_sbom import export_release_sbom, get_sbom_recovery
except ImportError:
    try:
        from export_release_sbom import export_release_sbom, get_sbom_recovery
    except ImportError:
        export_release_sbom = None  # type: ignore
        get_sbom_recovery = None  # type: ignore

SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
CFF_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CITATION_FILE = "CITATION.cff"
SBOM_FILE = "sbom.spdx.json"
# Optional release assets attached for each profile; the evidence assets are always attached.
RELEASE_PROFILES = {
    "minimal": (),
    "release": (SBOM_FILE,),
    "scientific": (CITATION_FILE, SBOM_FILE),
}
TRUSTED_RELEASE_FILES = {
    "docs/release-control.json",
    "docs/release-control.md",
    "docs/release-evidence.json",
}


class ReleasePromotionError(ValueError):
    """Raised when release promotion inputs are missing, stale, or inconsistent."""

    def __init__(self, message: str, recovery: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.recovery = recovery

    def __str__(self) -> str:
        if self.recovery and self.recovery not in self.message:
            return f"{self.message}. Recovery: {self.recovery}"
        return self.message


def _read_record(path: Path, name: str) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        return parse_release_record(raw)
    except (OSError, RecordValidationError) as error:
        raise ReleasePromotionError(f"Invalid {name}: {error}") from error


def _event_labelled(event: Dict[str, Any]) -> bool:
    pull_request = event.get("pull_request")
    labels = pull_request.get("labels", []) if isinstance(pull_request, dict) else []
    return isinstance(labels, list) and any(
        isinstance(label, dict) and label.get("name") == "release-candidate"
        for label in labels
    )


def _matching_identity(control: Dict[str, Any], evidence: Dict[str, Any]) -> bool:
    control_candidate = control["candidate"]
    evidence_candidate = evidence["candidate"]
    fields = ("repository", "pull_request", "target_version", "head_sha")
    return all(control_candidate[field] == evidence_candidate[field] for field in fields)


def _top_level_cff(text: str) -> Dict[str, str]:
    """Return top-level CITATION.cff keys with unquoted scalar values (reused from doi_guidance)."""
    return parse_cff_scalars(text)


def validate_citation_metadata(
    citation_path: Path,
    version: str,
    *,
    control: Optional[Dict[str, Any]] = None,
    evidence: Optional[Dict[str, Any]] = None,
    doi_state: Optional[Union[str, DoiState]] = None,
) -> str:
    """Return validated CITATION.cff text or raise with a message naming the first problem."""
    try:
        text, keys = parse_cff_metadata(citation_path)
    except OSError as error:
        raise ReleasePromotionError(f"Cannot read {CITATION_FILE} at {citation_path}: {error}") from error
    for required in ("cff-version", "message", "title", "authors"):
        if required not in keys:
            raise ReleasePromotionError(f"Invalid {CITATION_FILE}: missing required field {required!r}")
    for scalar in ("cff-version", "message", "title"):
        if not keys[scalar]:
            raise ReleasePromotionError(f"Invalid {CITATION_FILE}: field {scalar!r} is empty")
    if not re.search(r"^authors:[ \t]*\n(?:[ \t]*#.*\n)*[ \t]*-[ \t]", text, re.MULTILINE):
        raise ReleasePromotionError(f"Invalid {CITATION_FILE}: 'authors' must list at least one author")
    if "version" in keys and keys["version"] != version:
        raise ReleasePromotionError(
            f"Invalid {CITATION_FILE}: version {keys['version']!r} does not match release version {version!r}"
        )
    if "date-released" in keys and not CFF_DATE_PATTERN.fullmatch(keys["date-released"]):
        raise ReleasePromotionError(f"Invalid {CITATION_FILE}: 'date-released' must be YYYY-MM-DD")
    if "doi" in keys and not DOI_PATTERN.fullmatch(keys["doi"]):
        raise ReleasePromotionError(f"Invalid {CITATION_FILE}: 'doi' is not a valid DOI")

    effective_doi_state = doi_state
    expected_doi = None
    if effective_doi_state is None and control is not None:
        effective_doi_state = control.get("doi_state") or control.get("candidate", {}).get("doi_state")
        expected_doi = control.get("doi") or control.get("candidate", {}).get("doi")
    if effective_doi_state is None and evidence is not None:
        effective_doi_state = evidence.get("doi_state") or evidence.get("candidate", {}).get("doi_state")
        if expected_doi is None:
            expected_doi = evidence.get("doi") or evidence.get("candidate", {}).get("doi")

    if effective_doi_state is None:
        effective_doi_state = DoiState.UNSELECTED

    try:
        validate_citation_cff_doi(text, doi_state=effective_doi_state, expected_doi=expected_doi)
    except DoiError as error:
        raise ReleasePromotionError(f"Invalid {CITATION_FILE}: {error}") from error

    return text


def _release_notes(
    repository: str,
    pull_request: int,
    version: str,
    candidate_sha: str,
    merge_sha: str,
    control: Dict[str, Any],
    evidence: Dict[str, Any],
    optional_assets: tuple = (SBOM_FILE,),
    doi_metadata: Optional[Tuple[str, Optional[str]]] = None,
) -> str:
    constraints = sorted(
        set(control.get("operational_constraints", []))
        | set(evidence.get("operational_constraints", []))
    )
    unverified = sorted(
        set(control.get("unverified_scenarios", []))
        | set(evidence.get("unverified_scenarios", []))
    )
    lines = [
        f"Release `{version}` for `{repository}`.",
        "",
        f"Pull request: #{pull_request}",
        f"Candidate head: `{candidate_sha.lower()}`",
        f"Merged commit: `{merge_sha.lower()}`",
        f"Runtime fingerprint: `{control['live_evidence']['runtime_fingerprint'].lower()}`",
        "",
        "Release evidence: `docs/release-evidence.json` and `docs/release-control.json`.",
        "Audit report: `docs/release-control.md`.",
    ]
    if SBOM_FILE in optional_assets:
        lines.append(f"Software Bill of Materials: `{SBOM_FILE}`.")
    if CITATION_FILE in optional_assets:
        lines.append(f"Citation metadata: `{CITATION_FILE}`.")
    if doi_metadata and doi_metadata[0]:
        lines.append(format_doi_release_notes(doi_metadata[0], doi_metadata[1]))
    lines.extend([
        "",
        "This release records verified evidence for the selected commit. It does not prove scientific reproducibility.",
    ])
    if constraints:
        lines.extend(["", "Operational constraints:", *[f"- {item}" for item in constraints]])
    if unverified:
        lines.extend(["", "Unverified scenarios:", *[f"- {item}" for item in unverified]])
    return "\n".join(lines) + "\n"


def prepare_release_payload(
    event: Dict[str, Any],
    *,
    control_path: Path,
    evidence_path: Path,
    audit_report_path: Path,
    output_dir: Path,
    repo_root: Path,
    api: Any = None,
    tag: Optional[str] = None,
    profile: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return release metadata and write assets, or return None for a safe bypass.

    Publication requires an explicit ``tag`` equal to ``v<target_version>``. The ``profile``
    selects optional assets; a profile that includes ``CITATION.cff`` validates it first.
    """
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict) or pull_request.get("merged") is not True or not _event_labelled(event):
        return None
    if not isinstance(tag, str) or not tag.strip():
        raise ReleasePromotionError("Release promotion requires an explicit tag (for example v1.2.3); refusing to publish")
    tag = tag.strip()
    if profile is not None and profile not in RELEASE_PROFILES:
        raise ReleasePromotionError(
            f"Unknown release profile {profile!r}; choose one of {', '.join(sorted(RELEASE_PROFILES))}"
        )
    base = pull_request.get("base")
    if not isinstance(base, dict) or base.get("ref") != "main":
        raise ReleasePromotionError("Release promotion requires a pull request merged into main")

    repository = event.get("repository", {}).get("full_name") if isinstance(event.get("repository"), dict) else None
    pr_number = pull_request.get("number")
    head = pull_request.get("head")
    candidate_sha = head.get("sha") if isinstance(head, dict) else None
    merge_sha = pull_request.get("merge_commit_sha")
    valid_pr_number = isinstance(pr_number, int) and not isinstance(pr_number, bool) and pr_number > 0
    if not isinstance(repository, str) or not repository or not valid_pr_number:
        raise ReleasePromotionError("Merged release-candidate event is missing repository or pull request identity")
    if not isinstance(candidate_sha, str) or not SHA_PATTERN.fullmatch(candidate_sha):
        raise ReleasePromotionError("Merged release-candidate event has an invalid candidate head SHA")
    if not isinstance(merge_sha, str) or not SHA_PATTERN.fullmatch(merge_sha):
        raise ReleasePromotionError("Merged release-candidate event has no valid merge commit SHA")

    control = _read_record(control_path, "Control Record")
    evidence = _read_record(evidence_path, "release evidence")

    if profile is None:
        candidate_meta = control.get("candidate", {}) if isinstance(control.get("candidate"), dict) else {}
        profile = candidate_meta.get("profile") or control.get("profile") or "release"
    if profile not in RELEASE_PROFILES:
        raise ReleasePromotionError(
            f"Unknown release profile {profile!r}; choose one of {', '.join(sorted(RELEASE_PROFILES))}"
        )
    optional_assets = RELEASE_PROFILES[profile]

    control_status = control.get("status", "active")
    evidence_status = evidence.get("status", "active")
    if control_status not in ("active", "candidate"):
        raise ReleasePromotionError(
            f"Control Record status is {control_status!r}; promotion requires active candidate evidence"
        )
    if evidence_status not in ("active", "candidate"):
        raise ReleasePromotionError(
            f"release evidence status is {evidence_status!r}; archived or inactive evidence cannot be promoted"
        )
    if not _matching_identity(control, evidence):
        raise ReleasePromotionError("Control Record and release evidence candidate identity does not match")

    control_candidate = control.get("candidate", {}) if isinstance(control.get("candidate"), dict) else {}
    evidence_candidate = evidence.get("candidate", {}) if isinstance(evidence.get("candidate"), dict) else {}
    control_doi_state = control.get("doi_state") or control_candidate.get("doi_state")
    evidence_doi_state = evidence.get("doi_state") or evidence_candidate.get("doi_state")
    control_doi = control.get("doi") or control_candidate.get("doi")
    evidence_doi = evidence.get("doi") or evidence_candidate.get("doi")

    doi_metadata: Optional[Tuple[str, Optional[str]]] = None
    if control_doi_state or control_doi:
        try:
            doi_metadata = validate_release_doi_metadata({
                "doi_state": control_doi_state or DoiState.UNSELECTED.value,
                "doi": control_doi,
            })
        except DoiError as error:
            raise ReleasePromotionError(f"Invalid Control Record DOI metadata: {error}") from error

    if evidence_doi_state or evidence_doi:
        try:
            ev_doi_meta = validate_release_doi_metadata({
                "doi_state": evidence_doi_state or DoiState.UNSELECTED.value,
                "doi": evidence_doi,
            })
            if doi_metadata is None:
                doi_metadata = ev_doi_meta
        except DoiError as error:
            raise ReleasePromotionError(f"Invalid release evidence DOI metadata: {error}") from error

    norm_control_doi_state = control_doi_state.strip().lower() if (isinstance(control_doi_state, str) and control_doi_state.strip()) else DoiState.UNSELECTED.value
    norm_evidence_doi_state = evidence_doi_state.strip().lower() if (isinstance(evidence_doi_state, str) and evidence_doi_state.strip()) else DoiState.UNSELECTED.value
    if norm_control_doi_state != norm_evidence_doi_state:
        raise ReleasePromotionError("Control Record and release evidence DOI state does not match")

    norm_control_doi = control_doi.strip() if (isinstance(control_doi, str) and control_doi.strip()) else None
    norm_evidence_doi = evidence_doi.strip() if (isinstance(evidence_doi, str) and evidence_doi.strip()) else None
    if (norm_control_doi or norm_evidence_doi) and norm_control_doi != norm_evidence_doi:
        raise ReleasePromotionError("Control Record and release evidence DOI does not match")

    candidate = control["candidate"]
    if candidate["repository"].casefold() != repository.casefold() or candidate["pull_request"] != pr_number:
        raise ReleasePromotionError("Control Record candidate identity does not match the merged pull request")
    if candidate["head_sha"].lower() != candidate_sha.lower():
        raise ReleasePromotionError("Control Record candidate head does not match the merged pull request head")
    version = candidate["target_version"]
    if not VERSION_PATTERN.fullmatch(version):
        raise ReleasePromotionError(f"Control Record target version {version!r} is not a safe semantic version")

    control_fingerprint = control["live_evidence"]["runtime_fingerprint"].lower()
    evidence_fingerprint = evidence["live_evidence"]["runtime_fingerprint"].lower()
    if not FINGERPRINT_PATTERN.fullmatch(control_fingerprint) or control_fingerprint != evidence_fingerprint:
        raise ReleasePromotionError("Control Record and release evidence runtime fingerprints do not match")
    try:
        merged_fingerprint = compute_runtime_fingerprint(repo_root, commit_sha=merge_sha)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise ReleasePromotionError(f"Cannot compute Runtime Fingerprint for merged commit {merge_sha}: {error}") from error
    if merged_fingerprint.lower() != control_fingerprint:
        raise ReleasePromotionError(
            "Runtime Fingerprint at the actual merged commit does not match active release evidence"
        )
    try:
        audit_report = audit_report_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ReleasePromotionError(f"Cannot read release audit report {audit_report_path}: {error}") from error
    if not audit_report.strip():
        raise ReleasePromotionError("Release audit report is empty")
    constraints = set(control.get("operational_constraints", [])) | set(evidence.get("operational_constraints", []))
    verdict = "ready with declared operational constraints" if constraints else "ready"
    report_check = verify_release_report(
        audit_report_path,
        candidate,
        verdict,
        live_evidence_spec=control["live_evidence"],
        content_override=audit_report,
        repo_root=repo_root,
    )
    if not report_check["valid"]:
        raise ReleasePromotionError(f"Release audit report does not agree with active evidence: {report_check['reason']}")

    if tag != f"v{version}":
        raise ReleasePromotionError(
            f"Explicit tag {tag!r} does not match the Control Record target version (expected 'v{version}')"
        )
    citation_text = None
    if CITATION_FILE in optional_assets:
        citation_text = validate_citation_metadata(
            repo_root / CITATION_FILE,
            version,
            control=control,
            evidence=evidence,
        )
    sbom_doc = None
    if SBOM_FILE in optional_assets:
        if export_release_sbom is None:
            raise ReleasePromotionError(
                "Release promotion requires SBOM export support; export_release_sbom module is unavailable",
                recovery="Ensure scripts/export_release_sbom.py is installed and importable.",
            )
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        with tempfile.TemporaryDirectory() as staging:
            staging_sbom = Path(staging) / SBOM_FILE
            try:
                sbom_doc = export_release_sbom(
                    repo=repository,
                    output_path=staging_sbom,
                    token=token,
                    api_url=api_url,
                    api=api,
                    allow_fallback=False,
                    require_packages=True,
                )
            except Exception as error:
                recovery = getattr(error, "recovery", "")
                if not recovery and get_sbom_recovery is not None:
                    recovery = get_sbom_recovery(str(error))
                raise ReleasePromotionError(
                    f"Cannot generate release SBOM: {error}",
                    recovery=recovery,
                ) from error

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "audit-report.md").write_text(audit_report, encoding="utf-8")
    (output_dir / "runtime-fingerprint.txt").write_text(control_fingerprint + "\n", encoding="utf-8")
    notes = _release_notes(
        repository,
        pr_number,
        version,
        candidate_sha,
        merge_sha,
        control,
        evidence,
        optional_assets,
        doi_metadata=doi_metadata,
    )
    (output_dir / "release-notes.md").write_text(notes, encoding="utf-8")
    if citation_text is not None:
        (output_dir / CITATION_FILE).write_text(citation_text, encoding="utf-8")
    if sbom_doc is not None:
        (output_dir / SBOM_FILE).write_text(json.dumps(sbom_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "tag": tag,
        "target_sha": merge_sha.lower(),
        "version": version,
        "runtime_fingerprint": control_fingerprint,
        "optional_assets": list(optional_assets),
    }


def verify_trusted_release_gate(event: Dict[str, Any], repo_root: Path, api: Any = None) -> None:
    """Recheck the Gate against records that the candidate PR did not change."""
    pull_request = event.get("pull_request")
    repository = event.get("repository")
    if not isinstance(pull_request, dict) or not isinstance(repository, dict):
        raise ReleasePromotionError("Release Gate event is missing pull request identity")
    repo_name = repository.get("full_name")
    pr_number = pull_request.get("number")
    head = pull_request.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    if (not isinstance(repo_name, str) or not isinstance(pr_number, int)
            or not isinstance(head_sha, str) or not SHA_PATTERN.fullmatch(head_sha)):
        raise ReleasePromotionError("Release Gate event has invalid candidate identity")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ReleasePromotionError("GITHUB_TOKEN is required to recheck the Release Gate")
    if api is None:
        api = GitHubApi(token, base_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"))

    try:
        files = get_pages(api, f"/repos/{repo_name}/pulls/{pr_number}/files")
    except ApiError as error:
        raise ReleasePromotionError(f"Cannot verify candidate file provenance: {sanitize_credentials(str(error))}") from None
    if len(files) >= 3000:
        raise ReleasePromotionError("Cannot verify candidate file provenance beyond GitHub's 3000-file limit")
    for file in files:
        if not isinstance(file, dict) or not isinstance(file.get("filename"), str):
            raise ReleasePromotionError("Cannot verify malformed pull request file list")
        if file["filename"] in TRUSTED_RELEASE_FILES or file.get("previous_filename") in TRUSTED_RELEASE_FILES:
            raise ReleasePromotionError(
                "Release Candidate changed trusted release records; publish Control Record and report directly to main"
            )

    env = dict(os.environ)
    env.update({
        "GITHUB_PR_NUMBER": str(pr_number),
        "GITHUB_HEAD_SHA": head_sha,
        "GITHUB_BASE_REF": "main",
        "GITHUB_REF": f"refs/pull/{pr_number}/head",
        "SCIFLOW_CICD_REPO_ROOT": str(repo_root.resolve()),
    })
    command = [
        sys.executable,
        str(ROOT / "scripts" / "verify_release.py"),
        "--record", "docs/release-control.json",
        "--report", "docs/release-control.md",
        "--repo", repo_name,
        "--format", "json",
        "--labels", "release-candidate",
        "--ci",
    ]
    try:
        result = subprocess.run(command, cwd=repo_root, env=env, capture_output=True, text=True, check=False)
    except OSError as error:
        raise ReleasePromotionError(f"Cannot recheck Release Gate: {error}") from error
    if result.returncode:
        diagnostic = sanitize_credentials(result.stderr.strip() or result.stdout.strip())
        raise ReleasePromotionError(f"Release Gate recheck failed: {diagnostic[:1200]}")
    try:
        verdict = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ReleasePromotionError("Release Gate recheck returned malformed JSON") from None
    if not isinstance(verdict, dict) or verdict.get("gate_verdict") != "GO":
        raise ReleasePromotionError("Release Gate recheck did not return a GO verdict")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", type=Path, required=True, help="GitHub pull_request event JSON")
    parser.add_argument("--control", type=Path, default=Path("docs/release-control.json"))
    parser.add_argument("--evidence", type=Path, default=Path("docs/release-evidence.json"))
    parser.add_argument("--audit-report", type=Path, default=Path("docs/release-control.md"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--tag", default=None, help="explicit release tag, e.g. v1.2.3; required to publish")
    parser.add_argument("--profile", choices=sorted(RELEASE_PROFILES), default=None,
                        help="optional assets: minimal (none), release (SBOM), scientific (CITATION.cff and SBOM); defaults to candidate profile or 'release'")
    parser.add_argument("--verify-gate", action="store_true", help="recheck trusted Release Gate evidence before promotion")
    args = parser.parse_args(argv)
    try:
        event = json.loads(args.event.read_text(encoding="utf-8"))
        if not isinstance(event, dict):
            raise ReleasePromotionError("GitHub event payload root must be a JSON object")
        pull_request = event.get("pull_request")
        if not isinstance(pull_request, dict) or pull_request.get("merged") is not True or not _event_labelled(event):
            payload = None
        else:
            if args.verify_gate:
                verify_trusted_release_gate(event, args.repo_root)
            payload = prepare_release_payload(
                event,
                control_path=args.control,
                evidence_path=args.evidence,
                audit_report_path=args.audit_report,
                output_dir=args.output_dir,
                repo_root=args.repo_root,
                tag=args.tag,
                profile=args.profile,
            )
    except (OSError, json.JSONDecodeError, ReleasePromotionError) as error:
        print(f"Release promotion failed: {error}", file=sys.stderr)
        return 1
    if payload is None:
        print(json.dumps({"skipped": True}))
        return 0
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
