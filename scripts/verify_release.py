#!/usr/bin/env python3
"""Release evidence verification engine and maintainer release-control command.

Verifies candidate identity, required CI matrix runs, live disposable validation evidence,
and consistency with human release reports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from scripts.configure_github_repo import ApiError, GitHubApi
except ImportError:
    from configure_github_repo import ApiError, GitHubApi

REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}\Z")
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-fA-F]{64}\Z")

# Sensitive patterns that must never appear inside Control Records or diagnostic logs
SECRET_PATTERNS = [
    re.compile(r"gh[opusr]_[A-Za-z0-9_]{8,}", re.IGNORECASE),
    re.compile(r"github_pat_[A-Za-z0-9_]{10,}", re.IGNORECASE),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]+", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9_-]{10,}", re.IGNORECASE),
    re.compile(r"nvapi-[A-Za-z0-9_-]{10,}", re.IGNORECASE),
    re.compile(r"AKIA[0-9A-Z]{16}", re.IGNORECASE),
    re.compile(r"(?:api[_-]?key|client[_-]?secret|auth[_-]?token)\s*[:=]\s*[^\s,;'\"]{8,}", re.IGNORECASE),
    re.compile(r"-----BEGIN\s+[A-Za-z0-9\s_-]+PRIVATE\s+KEY-----", re.IGNORECASE),
]
URL_CREDENTIAL_PATTERN = re.compile(r"https?://[^:\s@]+:[^@\s/]+@\S+", re.IGNORECASE)

ALLOWED_ROOT_KEYS = {
    "version",
    "status",
    "candidate",
    "required_gates",
    "live_evidence",
    "operational_constraints",
    "unverified_scenarios",
    "profile",
    "doi_state",
    "doi",
}
ALLOWED_CANDIDATE_KEYS = {
    "repository",
    "pull_request",
    "target_version",
    "head_sha",
    "profile",
    "doi_state",
    "doi",
}
ALLOWED_REQUIRED_GATES_KEYS = {"python_versions"}
ALLOWED_LIVE_EVIDENCE_KEYS = {
    "runtime_fingerprint",
    "target_repository",
    "setup_pr",
    "review_run",
}
ALLOWED_SETUP_PR_KEYS = {
    "number",
    "merge_commit",
    "workflow_path",
    "workflow_sha",
    "action_sha",
}
ALLOWED_REVIEW_RUN_KEYS = {"run_id", "validation_pr", "finding_id", "workflow_commit"}
SUPPORTED_PYTHON_VERSIONS = {"3.10", "3.11", "3.12"}


class RecordValidationError(ValueError):
    """Raised when a Control Record fails schema, type, or secret validation."""
    pass


def sanitize_credentials(text: str) -> str:
    """Sanitize credential tokens and auth headers from text."""
    if not isinstance(text, str):
        return text
    sanitized = text
    for pattern in SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    # Also sanitize generic bearer / authorization strings
    sanitized = re.sub(r"(Authorization:\s*Bearer\s+)[^\s'\"]+", r"\1[REDACTED]", sanitized, flags=re.IGNORECASE)
    # Redact user credentials embedded in URLs (e.g., https://user:pass@host)
    sanitized = re.sub(r"(https?://)([^:\s]+):([^@\s]+)@", r"\1[REDACTED]:[REDACTED]@", sanitized, flags=re.IGNORECASE)
    return sanitized


def _reject_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    """Object pairs hook for json.loads that raises RecordValidationError on duplicate keys."""
    obj: Dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise RecordValidationError(f"Duplicate key in Control Record: '{key}'")
        obj[key] = value
    return obj


def _scan_for_secrets(data: Any, path: str = "root") -> None:
    """Recursively scan data structures for secret-like tokens."""
    if isinstance(data, str):
        for pattern in SECRET_PATTERNS:
            if pattern.search(data):
                raise RecordValidationError(
                    f"Secret-like credential token detected in field '{path}': [REDACTED]"
                )
        if URL_CREDENTIAL_PATTERN.search(data):
            raise RecordValidationError(
                f"Secret-like credential token detected in field '{path}': [REDACTED]"
            )
    elif isinstance(data, dict):
        for k, v in data.items():
            if not isinstance(k, str):
                raise RecordValidationError(f"Non-string key in object at '{path}': {k!r}")
            _scan_for_secrets(k, f"{path}.key")
            _scan_for_secrets(v, f"{path}.{k}")
    elif isinstance(data, (list, tuple)):
        for idx, item in enumerate(data):
            _scan_for_secrets(item, f"{path}[{idx}]")


def _validate_dict_keys(d: Dict[str, Any], allowed_keys: set[str], path_name: str) -> None:
    """Ensure dictionary contains only allowed keys."""
    extra = set(d.keys()) - allowed_keys
    if extra:
        sorted_extra = [sanitize_credentials(k) for k in sorted(extra)]
        raise RecordValidationError(
            f"Unexpected key(s) in {path_name}: {sorted_extra}. Allowed keys: {sorted(allowed_keys)}"
        )


def parse_release_record(raw_content: str) -> Dict[str, Any]:
    """Parse and strictly validate a Control Record JSON document."""
    if not isinstance(raw_content, str) or not raw_content.strip():
        raise RecordValidationError("Control Record is empty or invalid string")

    try:
        data = json.loads(raw_content, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as err:
        raise RecordValidationError(f"Malformed release evidence JSON: {err}") from err

    if not isinstance(data, dict):
        raise RecordValidationError("Control Record root must be a JSON object")

    # Scan for secrets in parsed document
    _scan_for_secrets(data)

    # Validate root keys
    _validate_dict_keys(data, ALLOWED_ROOT_KEYS, "record root")

    # Version check
    if "version" not in data:
        raise RecordValidationError("Missing required 'version' field in Control Record")
    ver_val = data["version"]
    if not (isinstance(ver_val, int) and not isinstance(ver_val, bool)) or ver_val != 1:
        raise RecordValidationError(f"Unsupported record version: {ver_val!r}; expected 1")

    # Status check (optional, default: "active")
    if "status" in data:
        if not isinstance(data["status"], str) or data["status"] not in ("active", "candidate", "archived"):
            raise RecordValidationError(
                f"Invalid 'status' value: {data['status']!r}; expected 'active', 'candidate', or 'archived'"
            )

    # Candidate check
    if "candidate" not in data or not isinstance(data["candidate"], dict):
        raise RecordValidationError("Missing required 'candidate' object in Control Record")
    cand = data["candidate"]
    _validate_dict_keys(cand, ALLOWED_CANDIDATE_KEYS, "candidate object")

    for req in ("repository", "pull_request", "target_version", "head_sha"):
        if req not in cand:
            raise RecordValidationError(f"Missing required field '{req}' in candidate object")

    if not isinstance(cand["repository"], str) or not REPOSITORY_PATTERN.fullmatch(cand["repository"]):
        raise RecordValidationError(f"Invalid candidate repository format: {cand['repository']!r}; expected OWNER/REPO")
    cand_pr = cand["pull_request"]
    if not (isinstance(cand_pr, int) and not isinstance(cand_pr, bool)) or cand_pr <= 0:
        raise RecordValidationError(f"Candidate pull_request must be a positive integer, got: {cand_pr!r}")
    if not isinstance(cand["target_version"], str) or not cand["target_version"].strip():
        raise RecordValidationError("Candidate target_version must be a non-empty string")
    if not isinstance(cand["head_sha"], str) or not COMMIT_SHA_PATTERN.fullmatch(cand["head_sha"]):
        raise RecordValidationError(f"Candidate head_sha must be a 40-character commit hex SHA, got: {cand['head_sha']!r}")

    for container_name, container in (("candidate object", cand), ("record root", data)):
        if "profile" in container:
            prof = container["profile"]
            if not isinstance(prof, str) or prof not in ("minimal", "release", "scientific"):
                raise RecordValidationError(f"Invalid profile in {container_name}: {prof!r}; expected 'minimal', 'release', or 'scientific'")
        if "doi_state" in container:
            ds = container["doi_state"]
            if not isinstance(ds, str) or ds.strip().lower() not in ("unselected", "requested", "pending", "issued", "unavailable"):
                raise RecordValidationError(f"Invalid 'doi_state' in {container_name}: {ds!r}")
        if "doi" in container:
            d_val = container["doi"]
            if d_val is not None and (not isinstance(d_val, str) or not re.match(r"^10\.\d{4,9}/\S+$", d_val.strip())):
                raise RecordValidationError(f"Invalid 'doi' in {container_name}: {d_val!r}; expected 10.xxxx/...")

        # Enforce DOI issuance domain invariant: a DOI is NEVER recorded before issuance is verified.
        ds = container.get("doi_state")
        d_val = container.get("doi")
        has_doi = d_val is not None and isinstance(d_val, str) and bool(d_val.strip())
        is_issued = isinstance(ds, str) and ds.strip().lower() == "issued"

        if has_doi and not is_issued:
            raise RecordValidationError(
                f"Invalid DOI in {container_name}: 'doi' cannot be declared when doi_state is {ds!r}; expected 'issued'"
            )
        if is_issued and not has_doi:
            raise RecordValidationError(
                f"Missing 'doi' in {container_name}: 'doi' is required when doi_state is 'issued'"
            )

    # Required gates check
    if "required_gates" in data:
        rg = data["required_gates"]
        if not isinstance(rg, dict):
            raise RecordValidationError("'required_gates' must be a JSON object")
        _validate_dict_keys(rg, ALLOWED_REQUIRED_GATES_KEYS, "required_gates object")
        if "python_versions" not in rg:
            raise RecordValidationError("Missing required 'python_versions' in required_gates object")
        pv = rg["python_versions"]
        if not isinstance(pv, list) or not pv or any(not isinstance(v, str) or not v.strip() for v in pv):
            raise RecordValidationError("'python_versions' in required_gates must be a nonempty list of strings")
        if len(pv) != len(set(pv)) or set(pv) != SUPPORTED_PYTHON_VERSIONS:
            raise RecordValidationError("'python_versions' must list every supported Python matrix version exactly once")

    # Live evidence check
    if "live_evidence" in data:
        le = data["live_evidence"]
        if not isinstance(le, dict):
            raise RecordValidationError("'live_evidence' must be a JSON object")
        _validate_dict_keys(le, ALLOWED_LIVE_EVIDENCE_KEYS, "live_evidence object")

        if "runtime_fingerprint" not in le:
            raise RecordValidationError("Missing required 'runtime_fingerprint' in live_evidence")
        fp = le["runtime_fingerprint"]
        if not isinstance(fp, str) or not FINGERPRINT_PATTERN.fullmatch(fp):
            raise RecordValidationError("runtime_fingerprint in live_evidence must be a 64-character hex SHA-256")

        if "target_repository" in le:
            tr = le["target_repository"]
            if not isinstance(tr, str) or not REPOSITORY_PATTERN.fullmatch(tr):
                raise RecordValidationError("target_repository in live_evidence must be OWNER/REPO format")

        if "setup_pr" in le:
            spr = le["setup_pr"]
            if not isinstance(spr, dict):
                raise RecordValidationError("setup_pr in live_evidence must be an object")
            _validate_dict_keys(spr, ALLOWED_SETUP_PR_KEYS, "setup_pr object")
            spr_num = spr.get("number")
            if not (isinstance(spr_num, int) and not isinstance(spr_num, bool)) or spr_num <= 0:
                raise RecordValidationError("setup_pr.number must be a positive integer")
            if not isinstance(spr.get("merge_commit"), str) or not COMMIT_SHA_PATTERN.fullmatch(spr.get("merge_commit")):
                raise RecordValidationError("setup_pr.merge_commit must be a 40-character commit hex SHA")
            if not isinstance(spr.get("workflow_path"), str) or not spr.get("workflow_path").strip():
                raise RecordValidationError("setup_pr.workflow_path must be a non-empty string")
            if not isinstance(spr.get("workflow_sha"), str) or not COMMIT_SHA_PATTERN.fullmatch(spr.get("workflow_sha")):
                raise RecordValidationError("setup_pr.workflow_sha must be a 40-character hex SHA")
            if not isinstance(spr.get("action_sha"), str) or not COMMIT_SHA_PATTERN.fullmatch(spr.get("action_sha")):
                raise RecordValidationError("setup_pr.action_sha must be a 40-character hex SHA")

        if "review_run" in le:
            rr = le["review_run"]
            if not isinstance(rr, dict):
                raise RecordValidationError("review_run in live_evidence must be an object")
            _validate_dict_keys(rr, ALLOWED_REVIEW_RUN_KEYS, "review_run object")
            rr_run_id = rr.get("run_id")
            if not (isinstance(rr_run_id, int) and not isinstance(rr_run_id, bool)) or rr_run_id <= 0:
                raise RecordValidationError("review_run.run_id must be a positive integer")
            rr_val_pr = rr.get("validation_pr")
            if not (isinstance(rr_val_pr, int) and not isinstance(rr_val_pr, bool)) or rr_val_pr <= 0:
                raise RecordValidationError("review_run.validation_pr must be a positive integer")
            finding_id = rr.get("finding_id")
            if not ((isinstance(finding_id, int) and not isinstance(finding_id, bool)) or (isinstance(finding_id, str) and finding_id.strip())):
                raise RecordValidationError("review_run.finding_id must be a non-empty string or integer")
            if "workflow_commit" in rr and (
                not isinstance(rr["workflow_commit"], str)
                or not COMMIT_SHA_PATTERN.fullmatch(rr["workflow_commit"])
            ):
                raise RecordValidationError("review_run.workflow_commit must be a 40-character commit SHA")

    # Operational constraints check
    if "operational_constraints" in data:
        oc = data["operational_constraints"]
        if not isinstance(oc, list) or any(not isinstance(item, str) for item in oc):
            raise RecordValidationError("'operational_constraints' must be a list of strings")

    # Unverified scenarios check
    if "unverified_scenarios" in data:
        us = data["unverified_scenarios"]
        if not isinstance(us, list) or any(not isinstance(item, str) for item in us):
            raise RecordValidationError("'unverified_scenarios' must be a list of strings")

    return data


def verify_candidate_identity(api: Any, candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Verify release candidate pull request and head commit via read-only GitHub API call."""
    repo = candidate["repository"]
    pr_num = candidate["pull_request"]
    declared_sha = candidate["head_sha"].lower()

    path = f"/repos/{repo}/pulls/{pr_num}"
    try:
        status, body = api.request("GET", path)
    except Exception as err:
        err_msg = sanitize_credentials(str(err))
        return {
            "valid": False,
            "observed_head_sha": None,
            "reason": f"GitHub API failure while fetching PR #{pr_num} on {repo}: {err_msg}",
            "recovery": f"Check repository access and network connectivity for {repo}; fail closed on inaccessible state.",
        }

    if status == 404 or body is None:
        return {
            "valid": False,
            "observed_head_sha": None,
            "reason": f"Pull request #{pr_num} not found on repository {repo} (HTTP 404)",
            "recovery": f"Verify candidate PR number and repository name in Control Record.",
        }

    if status != 200 or not isinstance(body, dict):
        return {
            "valid": False,
            "observed_head_sha": None,
            "reason": f"Unexpected response from GitHub API for PR #{pr_num} on {repo}: HTTP {status}",
            "recovery": f"Inspect repository permissions and API availability for {repo}.",
        }

    base_repo = ((body.get("base") or {}).get("repo") or {}).get("full_name")
    if base_repo and base_repo.lower() != repo.lower():
        return {
            "valid": False,
            "observed_head_sha": None,
            "reason": f"PR #{pr_num} base repository mismatch: expected {repo}, got {base_repo}",
            "recovery": f"Ensure candidate PR #{pr_num} belongs to target repository {repo}.",
        }

    head_obj = body.get("head")
    if not isinstance(head_obj, dict) or "sha" not in head_obj:
        return {
            "valid": False,
            "observed_head_sha": None,
            "reason": f"Malformed PR #{pr_num} payload from GitHub API: missing head commit SHA",
            "recovery": f"Inspect GitHub API response format for {repo}.",
        }

    actual_sha = str(head_obj["sha"]).lower()
    if actual_sha != declared_sha:
        return {
            "valid": False,
            "observed_head_sha": actual_sha,
            "reason": (
                f"Candidate head commit mismatch: expected commit SHA {declared_sha}, "
                f"observed commit SHA {actual_sha} (record declares {declared_sha}, but PR #{pr_num} head is {actual_sha})"
            ),
            "recovery": f"Run required CI checks on current candidate head ({actual_sha}) and refresh Control Record.",
        }

    return {
        "valid": True,
        "observed_head_sha": actual_sha,
        "reason": f"Candidate PR #{pr_num} head commit matches declared SHA {declared_sha}",
        "recovery": "",
    }


def verify_ci_gates(
    api: Any,
    repository: str,
    head_sha: str,
    required_python_versions: Sequence[str],
) -> Dict[str, Any]:
    """Verify that all required Python matrix CI jobs passed on the current candidate head."""
    norm_head = head_sha.lower()
    base_path = f"/repos/{repository}/commits/{norm_head}/check-runs"
    path = f"{base_path}?per_page=100"
    try:
        status, body = api.request("GET", path)
        if status == 404:
            status, body = api.request("GET", base_path)
    except Exception as err:
        err_msg = sanitize_credentials(str(err))
        return {
            "valid": False,
            "reason": f"GitHub API failure while fetching check-runs on {repository} commit {norm_head}: {err_msg}",
            "recovery": "Verify GitHub API connectivity and token permissions for check-runs.",
        }

    if status != 200 or not isinstance(body, dict):
        return {
            "valid": False,
            "reason": f"Unexpected response from GitHub API for check-runs on {repository}: HTTP {status}",
            "recovery": f"Ensure candidate commit {norm_head} exists on {repository}.",
        }

    raw_check_runs = body.get("check_runs")
    if not isinstance(raw_check_runs, list):
        return {
            "valid": False,
            "reason": f"Malformed check-runs response from GitHub API on {repository}",
            "recovery": "Inspect GitHub Actions check-runs payload structure.",
        }

    check_runs = list(raw_check_runs)
    total_count = body.get("total_count")
    if not isinstance(total_count, int) or isinstance(total_count, bool) or total_count < len(check_runs):
        return {"valid": False, "reason": "Check-run response has an invalid total count", "recovery": "Retry or inspect the GitHub API response."}
    if isinstance(total_count, int) and not isinstance(total_count, bool) and total_count > len(check_runs):
        page = 2
        while len(check_runs) < total_count and page <= 100:
            next_path = f"{base_path}?per_page=100&page={page}"
            try:
                next_st, next_body = api.request("GET", next_path)
            except Exception as err:
                return {"valid": False, "reason": f"Cannot read all CI check-run pages: {sanitize_credentials(str(err))}", "recovery": "Retry after GitHub API access is restored."}
            if next_st != 200 or not isinstance(next_body, dict):
                return {"valid": False, "reason": "Cannot read all CI check-run pages", "recovery": "Retry after GitHub API access is restored."}
            more_runs = next_body.get("check_runs")
            if not isinstance(more_runs, list) or not more_runs:
                return {"valid": False, "reason": "CI check-run pagination ended before the declared total count", "recovery": "Retry the GitHub API query."}
            check_runs.extend(more_runs)
            page += 1
        if len(check_runs) < total_count:
            return {"valid": False, "reason": "CI check-run pagination limit was reached", "recovery": "Narrow the required checks or extend pagination support."}

    observed_jobs: Dict[str, Dict[str, Any]] = {}
    for cr in check_runs:
        if isinstance(cr, dict) and "name" in cr:
            observed_jobs[str(cr["name"])] = cr

    passing_jobs: List[str] = []
    for ver in required_python_versions:
        matching_runs = [observed_jobs[f"build ({ver})"]] if f"build ({ver})" in observed_jobs else []
        if not matching_runs:
            return {
                "valid": False,
                "reason": f"Required CI job for Python {ver} is missing on candidate head {norm_head}",
                "recovery": f"Trigger and complete the Python {ver} CI matrix job on {norm_head}.",
            }

        for target_run in matching_runs:
            run_name = str(target_run.get("name", f"Python {ver}"))
            cr_head = str(target_run.get("head_sha", "")).lower()
            if cr_head != norm_head:
                return {
                    "valid": False,
                    "reason": f"CI job {run_name} head SHA mismatch: ran on {cr_head}, candidate is {norm_head}",
                    "recovery": f"Re-run CI job {run_name} on current candidate head commit {norm_head}.",
                }

            st = str(target_run.get("status", "")).lower()
            if st != "completed":
                return {
                    "valid": False,
                    "reason": f"Required CI job {run_name} is in status '{st}' on candidate head {norm_head}",
                    "recovery": f"Wait for CI job {run_name} on {norm_head} to complete.",
                }

            conc = str(target_run.get("conclusion", "")).lower()
            if conc != "success":
                return {
                    "valid": False,
                    "reason": f"Required CI job {run_name} failed with conclusion '{conc}' on candidate head {norm_head}",
                    "recovery": f"Fix test or build failure in CI job {run_name} and re-run on candidate head {norm_head}.",
                }
            passing_jobs.append(run_name)

    return {
        "valid": True,
        "reason": f"All required Python CI jobs passed on candidate head {norm_head}: {', '.join(sorted(passing_jobs))}",
        "passing_jobs": passing_jobs,
    }


def _is_runtime_relevant_path(rel_path: str) -> bool:
    """Determine if a relative file path is part of the product runtime definition."""
    norm = rel_path.replace("\\", "/").removeprefix("./")
    if any(norm.startswith(p) for p in ("docs/", "tests/", ".git/", ".gemini/", ".codex/", ".agents/", ".agent/")):
        return False
    if "/__pycache__/" in norm or norm.startswith("__pycache__/"):
        return False
    if norm.endswith(".pyc") or norm.endswith(".md"):
        return False

    if norm in (
        "sciflow-cicd",
        "install.sh",
        "requirements.txt",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
    ) or (norm.startswith("requirements") and norm.endswith(".txt")):
        return True
    if norm.startswith("scripts/") and norm.endswith(".py"):
        return True
    if norm.startswith(".github/workflows/") and norm.endswith((".yml", ".yaml")):
        return True

    return False


def compute_runtime_fingerprint_from_files(
    files_map: Dict[str, Union[bytes, str]],
    rule_version: str = "v1",
) -> str:
    """Compute a deterministic SHA-256 fingerprint from a map of relative paths to contents."""
    relevant_files = []
    for path, content in files_map.items():
        if _is_runtime_relevant_path(path):
            norm_path = path.replace("\\", "/").removeprefix("./")
            raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
            relevant_files.append((norm_path, raw_bytes))

    relevant_files.sort(key=lambda x: x[0])

    hasher = hashlib.sha256()
    hasher.update(f"rule_version:{rule_version}\n".encode("utf-8"))
    for path, content_bytes in relevant_files:
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        hasher.update(f"{path}:{content_hash}\n".encode("utf-8"))

    return hasher.hexdigest()


def compute_runtime_fingerprint(
    repo_root: Union[str, Path],
    rule_version: str = "v1",
    commit_sha: Optional[str] = None,
) -> str:
    """Compute deterministic runtime fingerprint by inspecting repository files on disk or at a git commit."""
    root_path = Path(repo_root).resolve()

    if commit_sha:
        norm_sha = commit_sha.strip().lower()
        try:
            ls_res = subprocess.run(
                ["git", "ls-tree", "-r", norm_sha],
                cwd=str(root_path),
                capture_output=True,
                text=True,
                check=False,
            )
            if ls_res.returncode != 0 or not ls_res.stdout.strip():
                raise ValueError(f"Candidate commit {norm_sha} is unavailable in the local Git checkout")
            git_files: Dict[str, bytes] = {}
            for line in ls_res.stdout.strip().splitlines():
                parts = line.split(None, 3)
                if len(parts) != 4:
                    raise ValueError(f"Cannot parse Git tree for candidate commit {norm_sha}")
                blob_sha, path = parts[2], parts[3]
                if _is_runtime_relevant_path(path):
                    cat_res = subprocess.run(
                        ["git", "cat-file", "-p", blob_sha],
                        cwd=str(root_path), capture_output=True, check=False,
                    )
                    if cat_res.returncode != 0:
                        raise ValueError(f"Cannot read runtime file {path} at candidate commit {norm_sha}")
                    git_files[path] = cat_res.stdout
            if git_files:
                return compute_runtime_fingerprint_from_files(git_files, rule_version=rule_version)
            raise ValueError(f"Candidate commit {norm_sha} contains no runtime files")
        except OSError as err:
            raise ValueError(f"Cannot inspect candidate commit {norm_sha}: {err}") from err

    files_map: Dict[str, bytes] = {}

    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
        for fname in filenames:
            full_path = Path(dirpath) / fname
            try:
                rel_path = str(full_path.relative_to(root_path))
            except ValueError:
                continue
            if _is_runtime_relevant_path(rel_path):
                try:
                    files_map[rel_path] = full_path.read_bytes()
                except OSError:
                    pass

    gh_wf_dir = root_path / ".github" / "workflows"
    if gh_wf_dir.is_dir():
        for wf_file in gh_wf_dir.glob("*.yml"):
            try:
                rel_path = str(wf_file.relative_to(root_path))
                files_map[rel_path] = wf_file.read_bytes()
            except OSError:
                pass
        for wf_file in gh_wf_dir.glob("*.yaml"):
            try:
                rel_path = str(wf_file.relative_to(root_path))
                files_map[rel_path] = wf_file.read_bytes()
            except OSError:
                pass

    return compute_runtime_fingerprint_from_files(files_map, rule_version=rule_version)


def validate_runtime_fingerprint(
    repo_root: Union[str, Path],
    declared_fingerprint: str,
    commit_sha: Optional[str] = None,
    rule_version: str = "v1",
) -> Dict[str, Any]:
    """Validate declared runtime fingerprint against computed fingerprint at candidate commit."""
    norm_declared = declared_fingerprint.strip().lower()
    try:
        computed_fp = compute_runtime_fingerprint(repo_root, rule_version=rule_version, commit_sha=commit_sha)
    except ValueError as err:
        return {
            "valid": False,
            "reason": str(err),
            "recovery": "Fetch the candidate commit and retry release verification.",
            "computed_fingerprint": None,
        }
    if computed_fp != norm_declared:
        return {
            "valid": False,
            "reason": (
                f"Runtime fingerprint mismatch (recorded {norm_declared}, computed {computed_fp}): "
                f"runtime product code or templates were modified since live validation was recorded."
            ),
            "recovery": "Recompute runtime fingerprint for candidate commit and refresh the control record.",
            "computed_fingerprint": computed_fp,
        }
    return {
        "valid": True,
        "reason": f"Runtime fingerprint {norm_declared} matches current repository runtime definitions",
        "computed_fingerprint": computed_fp,
    }


def get_default_api(token: Optional[str] = None) -> GitHubApi:
    """Instantiate read-only GitHub API client using available authentication."""
    tok = token or os.environ.get("GITHUB_TOKEN")
    if not tok:
        for gh_cmd in ("/home/agent/.local/bin/gh", "gh"):
            try:
                res = subprocess.run([gh_cmd, "auth", "token"], capture_output=True, text=True, check=False)
                if res.returncode == 0 and res.stdout.strip():
                    tok = res.stdout.strip()
                    break
            except Exception:
                pass
    api_url = os.environ.get("GITHUB_API_URL")
    if api_url:
        return GitHubApi(token=tok or "", base_url=api_url)
    return GitHubApi(token=tok or "")


def verify_release_report(
    report_path: Union[str, Path],
    candidate: Dict[str, Any],
    verified_verdict: str,
    live_evidence_spec: Optional[Dict[str, Any]] = None,
    content_override: Optional[str] = None,
    repo_root: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Verify that human release report agrees with verified candidate identity, verdict, and cited evidence."""
    if content_override is None:
        rep_p = Path(report_path)
        if rep_p.is_absolute():
            path = rep_p
        elif repo_root and (Path(repo_root) / rep_p).is_file():
            path = Path(repo_root) / rep_p
        else:
            path = rep_p

        if not path.is_file():
            return {
                "valid": False,
                "reason": f"Human release report file not found: {report_path}",
                "recovery": "Provide a valid human release report path via --report.",
            }

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as err:
            return {
                "valid": False,
                "reason": f"Cannot read release report {report_path}: {err}",
                "recovery": f"Ensure release report {report_path} is readable.",
            }
    else:
        content = content_override

    # 1. Candidate PR in report
    pr_matches = re.findall(r"(?:pull\s*request|pr):\*{0,2}\s*(?:\[#|#)?(\d+)", content, re.IGNORECASE)
    if not pr_matches:
        pr_matches = re.findall(r"/pull/(\d+)", content, re.IGNORECASE)

    if not pr_matches:
        return {
            "valid": False,
            "reason": f"Release report {report_path} does not declare candidate pull request number",
            "recovery": "Include candidate Pull Request citation (e.g., 'Pull Request: [#23 ...]') in release report.",
        }

    reported_prs = [int(p) for p in pr_matches]
    if candidate["pull_request"] not in reported_prs:
        return {
            "valid": False,
            "reason": f"Candidate PR mismatch: report cites PR(s) {reported_prs}, record declares PR #{candidate['pull_request']}",
            "recovery": f"Update release report to reference verified candidate PR #{candidate['pull_request']}.",
        }

    candidate_head_match = re.search(r"\*\*Candidate Head SHA:\*\*\s*`?([0-9a-fA-F]{40})`?", content)
    if not candidate_head_match or candidate_head_match.group(1).lower() != candidate["head_sha"].lower():
        return {
            "valid": False,
            "reason": "Release report candidate head SHA is missing or stale",
            "recovery": f"Set Candidate Head SHA in the report to {candidate['head_sha']}.",
        }

    target_ver = str(candidate.get("target_version", "")).strip()
    if target_ver:
        ver_match = re.search(r"\*\*Target Version:\*\*\s*`?([^\n`*]+)`?", content, re.IGNORECASE)
        if ver_match:
            reported_ver = ver_match.group(1).strip()
            if reported_ver != target_ver:
                return {
                    "valid": False,
                    "reason": f"Candidate target version mismatch: report cites '{reported_ver}', record declares '{target_ver}'",
                    "recovery": f"Update release report to reference verified target version '{target_ver}'.",
                }



    # 2. Verdict in report (prioritize formal/current verdict headers, including multiline)
    verdict_match = re.search(
        r"(?:current\s+release\s+verdict|formal\s+verdict)\s*[:#]*\s*(?:\n\s*)?\*{0,2}(READY WITH DECLARED OPERATIONAL CONSTRAINTS|READY|NOT READY|PRONTO COM RESTRIÇÕES)\*{0,2}",
        content,
        re.IGNORECASE,
    )
    if not verdict_match:
        verdict_match = re.search(
            r"(?:verdict)\s*[:#]*\s*(?:\n\s*)?\*{0,2}(READY WITH DECLARED OPERATIONAL CONSTRAINTS|READY|NOT READY|PRONTO COM RESTRIÇÕES)\*{0,2}",
            content,
            re.IGNORECASE,
        )
    if not verdict_match:
        verdict_match = re.search(
            r"\*\*(READY WITH DECLARED OPERATIONAL CONSTRAINTS|READY|NOT READY|PRONTO COM RESTRIÇÕES)\*\*",
            content,
            re.IGNORECASE,
        )

    if not verdict_match:
        return {
            "valid": False,
            "reason": f"Release report {report_path} does not declare a formal verdict",
            "recovery": "Declare formal verdict in report (e.g., 'Current Release Verdict: **READY WITH DECLARED OPERATIONAL CONSTRAINTS**').",
        }

    raw_verdict = verdict_match.group(1).strip().lower()
    if "operational constraints" in raw_verdict or "restrições" in raw_verdict:
        normalized_report_verdict = "ready with declared operational constraints"
    elif "not ready" in raw_verdict:
        normalized_report_verdict = "not ready"
    elif "ready" in raw_verdict or "pronto" in raw_verdict:
        normalized_report_verdict = "ready"
    else:
        normalized_report_verdict = raw_verdict

    if verified_verdict == "not ready" and normalized_report_verdict != "not ready":
        return {
            "valid": False,
            "reason": (
                f"Contradictory release claims: human report claims '{verdict_match.group(1)}' "
                f"but verification engine determined 'not ready'."
            ),
            "recovery": "Update human report verdict to 'NOT READY' until all release gates pass.",
        }

    if normalized_report_verdict != verified_verdict:
        return {
            "valid": False,
            "reason": (
                f"Verdict mismatch: human report claims '{verdict_match.group(1)}', "
                f"but verified evidence verdict is '{verified_verdict}'."
            ),
            "recovery": f"Update report to align with verified verdict '{verified_verdict}'.",
        }

    # 3. Cited live evidence in report (only validate consistency if cited in report)
    if live_evidence_spec:
        setup_pr = live_evidence_spec.get("setup_pr")
        if setup_pr:
            spr_num = str(setup_pr.get("number", ""))
            spr_merge = str(setup_pr.get("merge_commit", "")).lower()
            setup_pr_cited = bool(
                re.search(r"\bsetup\s+(?:pr|pull\s+request)\b", content, re.IGNORECASE)
                or (spr_num and (f"pull/{spr_num}" in content or f"pulls/{spr_num}" in content))
                or (spr_merge and spr_merge in content.lower())
            )
            if setup_pr_cited:
                if (
                    spr_num
                    and f"Setup PR #{spr_num}" not in content
                    and f"Setup Pull Request #{spr_num}" not in content
                    and f"pull/{spr_num}" not in content
                    and f"pulls/{spr_num}" not in content
                ):
                    return {
                        "valid": False,
                        "reason": f"Report does not cite expected Setup PR #{spr_num}",
                        "recovery": f"Include Setup PR #{spr_num} citation in release report.",
                    }
                if spr_merge and ("merge" in content.lower() or spr_merge in content.lower()):
                    if spr_merge not in content.lower():
                        return {
                            "valid": False,
                            "reason": f"Report does not cite Setup PR merge commit {spr_merge}",
                            "recovery": f"Include Setup PR merge commit `{spr_merge}` in release report.",
                        }

        review_run = live_evidence_spec.get("review_run")
        if review_run:
            run_id = str(review_run.get("run_id", ""))
            finding_id = str(review_run.get("finding_id", ""))
            review_run_cited = bool(
                re.search(r"\bReview\s+Run\b", content, re.IGNORECASE)
                or "actions/runs" in content
                or (run_id and run_id in content)
                or re.search(r"\brun\s+\d+", content, re.IGNORECASE)
            )
            if review_run_cited and run_id and run_id not in content:
                return {
                    "valid": False,
                    "reason": f"Report does not cite eligible Review Run {run_id}",
                    "recovery": f"Include Review Run {run_id} citation in release report.",
                }
            finding_cited = bool(
                re.search(r"\b(?:advisory\s+)?finding\b", content, re.IGNORECASE)
                or "discussion_r" in content
                or (finding_id and finding_id in content)
            )
            if finding_cited and finding_id:
                clean_fid = finding_id.removeprefix("r").removeprefix("R")
                rfid = f"r{clean_fid}"
                Rf_id = f"R{clean_fid}"
                if (
                    finding_id not in content
                    and clean_fid not in content
                    and rfid not in content
                    and Rf_id not in content
                ):
                    return {
                        "valid": False,
                        "reason": f"Report does not cite advisory finding {finding_id}",
                        "recovery": f"Include advisory finding `{finding_id}` citation in release report.",
                    }

    return {
        "valid": True,
        "reason": f"Human release report {report_path} agrees with verified candidate PR #{candidate['pull_request']} and verdict '{verified_verdict}'",
    }


def verify_report_alignment(
    report_path: Union[str, Path],
    candidate: Dict[str, Any],
    verified_verdict: str,
    live_evidence_spec: Optional[Dict[str, Any]] = None,
    content_override: Optional[str] = None,
    repo_root: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Verify that human release report agrees with verified candidate identity, verdict, and cited evidence."""
    return verify_release_report(
        report_path=report_path,
        candidate=candidate,
        verified_verdict=verified_verdict,
        live_evidence_spec=live_evidence_spec,
        content_override=content_override,
        repo_root=repo_root,
    )


def _format_error_result(
    reason: str,
    recovery: str,
    unverified: Sequence[str],
    candidate: Optional[Dict[str, Any]] = None,
    observed: Optional[Union[Sequence[Any], Dict[str, Any]]] = None,
    inferred: Optional[Sequence[str]] = None,
    output_format: str = "text",
    out_stream: Optional[Any] = None,
    err_stream: Optional[Any] = None,
    observed_labels: Optional[Sequence[str]] = None,
) -> int:
    """Helper to format and write a NOT READY result, returning exit code 1."""
    out_s = out_stream if out_stream is not None else sys.stdout
    err_s = err_stream if err_stream is not None else sys.stderr

    obs_payload: Any
    if isinstance(observed, dict):
        obs_payload = dict(observed)
        if observed_labels is not None:
            obs_payload["labels"] = list(observed_labels)
    else:
        obs_payload = [sanitize_credentials(x) if isinstance(x, str) else x for x in (observed or [])]
        if observed_labels is not None:
            obs_payload.append({"labels": list(observed_labels)})

    res: Dict[str, Any] = {
        "candidate": candidate,
        "verdict": "not ready",
        "reason": sanitize_credentials(reason),
        "recovery": sanitize_credentials(recovery),
        "evidence": {
            "observed": obs_payload,
            "inferred": [sanitize_credentials(x) for x in (inferred or [])],
            "unverified": [sanitize_credentials(x) for x in unverified],
        },
    }
    if observed_labels is not None:
        res["observed"] = {"labels": list(observed_labels)}

    if output_format == "json":
        raw_json = json.dumps(res, indent=2) + "\n"
        out_s.write(sanitize_credentials(raw_json))
    else:
        labels_str = f"Observed labels: {list(observed_labels)}\n" if observed_labels is not None else ""
        if candidate is not None:
            cand_text = (
                f"Release Candidate: {candidate['repository']} PR #{candidate['pull_request']}\n"
                f"Verdict: NOT READY\n"
                f"Reason: {res['reason']}\n"
                f"{labels_str}"
                f"Recovery: {res['recovery']}\n"
            )
            out_s.write(sanitize_credentials(cand_text))
        else:
            err_text = (
                f"Error: {res['reason']}\n"
                f"{labels_str}"
                f"Recovery: {res['recovery']}\n"
                f"Verdict: NOT READY\n"
            )
            err_s.write(sanitize_credentials(err_text))

    return 1


def format_maintainer_recovery(
    record_path: str = "docs/release-control.json",
    report_path: Optional[str] = "docs/release-control.md",
    base_branch: Optional[str] = None,
    pr_number: Optional[int] = None,
) -> str:
    """Format exact git maintainer recovery commands to author and publish control record on base branch."""
    branch = base_branch or os.environ.get("GITHUB_BASE_REF", "").strip() or "main"
    rec = record_path if record_path else "docs/release-control.json"
    rep = report_path if report_path else "docs/release-control.md"
    pr_str = f"#{pr_number}" if pr_number is not None else "#<pr>"
    return (
        f"Publish the trusted release control record on the base branch:\n"
        f"  git checkout {branch}\n"
        f"  git pull origin {branch}\n"
        f"  git add {rec} {rep}\n"
        f"  git commit -m \"docs(release): publish release control for candidate PR {pr_str}\"\n"
        f"  git push origin {branch}"
    )


def _get_current_pr_number() -> Optional[int]:
    """Extract current pull request number from environment if available."""
    pr_env = os.environ.get("GITHUB_PR_NUMBER", "").strip()
    if pr_env and pr_env.isdigit():
        return int(pr_env)
    github_ref = os.environ.get("GITHUB_REF", "")
    if github_ref.startswith("refs/pull/"):
        parts = github_ref.split("/")
        if len(parts) > 2 and parts[2].isdigit():
            return int(parts[2])
    return None


def _get_current_head_sha(repo_root: Path, expected_sha: Optional[str] = None) -> Optional[str]:
    """Determine candidate head commit SHA from GITHUB_HEAD_SHA, local Git checkout, or GITHUB_SHA."""
    env_head = os.environ.get("GITHUB_HEAD_SHA", "").strip().lower()
    if env_head and COMMIT_SHA_PATTERN.fullmatch(env_head):
        return env_head

    try:
        # On PR merge commits (refs/pull/<N>/merge), HEAD^2 represents the candidate's actual head commit
        res_parent = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^2"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if res_parent.returncode == 0 and res_parent.stdout.strip():
            parent2_sha = res_parent.stdout.strip().lower()
            if COMMIT_SHA_PATTERN.fullmatch(parent2_sha):
                return parent2_sha
    except OSError:
        pass

    # Check if git rev-parse HEAD in repo_root matches expected_sha
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            head_sha = res.stdout.strip().lower()
            if COMMIT_SHA_PATTERN.fullmatch(head_sha):
                if expected_sha and head_sha == expected_sha.lower():
                    return head_sha
    except OSError:
        pass

    env_sha = os.environ.get("GITHUB_SHA", "").strip().lower()
    if env_sha and COMMIT_SHA_PATTERN.fullmatch(env_sha):
        return env_sha

    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            head_sha = res.stdout.strip().lower()
            if COMMIT_SHA_PATTERN.fullmatch(head_sha):
                return head_sha
    except OSError:
        pass

    return None


def _read_file_at_commit(repo_root: Path, commit_sha: str, relative_path: str) -> str:
    """Read a release control file from an exact Git commit outside the candidate tree."""
    path = Path(relative_path)
    if path.is_absolute() or not path.parts or any(part in ("..", ".") for part in path.parts):
        raise RecordValidationError(f"Release control path must be relative to repository root: {relative_path}")
    result = subprocess.run(
        ["git", "show", f"{commit_sha}:{path.as_posix()}"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RecordValidationError(f"Release control file {relative_path} is unavailable at commit {commit_sha}")
    return result.stdout.decode("utf-8")


def normalize_labels(labels: Optional[Union[str, Sequence[str]]]) -> List[str]:
    """Normalize labels into a list of cleaned label strings.

    Accepts None, a comma-separated string, or a sequence of strings.
    """
    if labels is None:
        return []
    if isinstance(labels, str):
        return [lbl.strip() for lbl in labels.split(",") if lbl.strip()]
    return [str(lbl).strip() for lbl in labels if str(lbl).strip()]


def run_verify_release(
    record_path: str = "docs/release-evidence.json",
    report_path: Optional[str] = None,
    repo: Optional[str] = None,
    token: Optional[str] = None,
    output_format: str = "text",
    ci_mode: bool = False,
    api: Optional[Any] = None,
    repo_root: Optional[Union[str, Path]] = None,
    out_stream: Optional[Any] = None,
    err_stream: Optional[Any] = None,
    record_ref: Optional[str] = None,
    labels: Optional[Sequence[str]] = None,
) -> int:
    """Run full release evidence verification command."""
    out_s = out_stream if out_stream is not None else sys.stdout
    err_s = err_stream if err_stream is not None else sys.stderr

    norm_labels = normalize_labels(labels)
    label_set = {lbl.lower() for lbl in norm_labels}
    is_candidate = "release-candidate" in label_set or (
        not ci_mode and (
            Path(record_path).name == "release-control.json"
            or (isinstance(record_path, str) and "release-control.json" in record_path)
        )
    )

    # CI Mode pre-evaluation: skip non-PR events and unmarked PRs before reading record
    if ci_mode:
        github_ref = os.environ.get("GITHUB_REF", "")
        pr_number = _get_current_pr_number()
        is_pr_event = (github_ref.startswith("refs/pull/") if github_ref else False) or (pr_number is not None)
        if github_ref and not is_pr_event:
            msg = f"CI ref '{github_ref}' is not a pull request event; skipping release gate."
            out_s.write(sanitize_credentials(msg) + "\n")
            return 0
        if not is_candidate:
            out_s.write("Pull request is not marked as a release candidate; skipping release gate\n")
            return 0

    root_dir = Path(repo_root) if repo_root is not None else Path(
        os.environ.get("SCIFLOW_CICD_REPO_ROOT", os.environ.get("GH_SETUP_REPO_ROOT", ROOT_DIR))
    )
    current_pr = _get_current_pr_number()
    base_branch = os.environ.get("GITHUB_BASE_REF", "").strip() or "main"

    report_content: Optional[str] = None
    try:
        if record_ref is not None:
            if not COMMIT_SHA_PATTERN.fullmatch(record_ref):
                raise RecordValidationError("--record-ref must be an exact 40-character commit SHA")
            content = _read_file_at_commit(root_dir, record_ref, record_path)
            if report_path:
                report_content = _read_file_at_commit(root_dir, record_ref, report_path)
        else:
            rec_p = Path(record_path)
            if rec_p.is_absolute():
                record_file = rec_p
            elif (root_dir / rec_p).is_file():
                record_file = root_dir / rec_p
            else:
                record_file = rec_p
            if not record_file.is_file():
                raise RecordValidationError(f"Control Record file not found: {record_path}")
            content = record_file.read_text(encoding="utf-8")
        record = parse_release_record(content)
        if not is_candidate and not ci_mode and record.get("status") == "candidate":
            is_candidate = True
    except (RecordValidationError, OSError, UnicodeDecodeError) as err:
        clean_msg = sanitize_credentials(str(err))
        if is_candidate or record_ref is not None:
            recovery_text = format_maintainer_recovery(
                record_path=record_path,
                report_path=report_path,
                base_branch=base_branch,
                pr_number=current_pr,
            )
            return _format_error_result(
                reason=f"Invalid Control Record: {clean_msg}",
                recovery=recovery_text,
                unverified=["Release control record"],
                output_format=output_format,
                out_stream=out_s,
                err_stream=err_s,
                observed_labels=norm_labels,
            )
        return _format_error_result(
            reason=f"Invalid Control Record: {clean_msg}",
            recovery="Correct the Control Record schema and remove sensitive or invalid fields.",
            unverified=["Control Record"],
            output_format=output_format,
            out_stream=out_s,
            err_stream=err_s,
            observed_labels=norm_labels,
        )

    candidate = record["candidate"]
    if repo and repo.lower() != candidate["repository"].lower():
        return _format_error_result(
            reason="Repository override conflicts with the Control Record",
            recovery="Use the repository declared in the record or correct the record.",
            unverified=["Candidate repository identity"],
            output_format=output_format, out_stream=out_s, err_stream=err_s,
            observed_labels=norm_labels,
        )

    # Archived check
    if record.get("status") == "archived":
        if ci_mode:
            msg = f"Control Record is archived for historical candidate PR #{candidate['pull_request']}; skipping release gate in CI."
            out_s.write(sanitize_credentials(msg) + "\n")
            return 0

    # PR number validation: do NOT skip if labeled candidate PR does not match
    if ci_mode and current_pr is not None and current_pr != candidate["pull_request"]:
        if is_candidate:
            return _format_error_result(
                candidate=candidate,
                reason=(
                    f"Candidate PR mismatch: expected PR #{candidate['pull_request']}, "
                    f"observed PR #{current_pr}"
                ),
                recovery=(
                    f"Update {record_path} on the base branch to declare candidate pull_request {current_pr}."
                ),
                observed=[f"Observed PR #{current_pr}"],
                unverified=["Candidate PR identity"],
                output_format=output_format,
                out_stream=out_s,
                err_stream=err_s,
                observed_labels=norm_labels,
            )
        elif ci_mode:
            msg = (
                f"Current pull request #{current_pr} does not match declared release candidate "
                f"PR #{candidate['pull_request']}; skipping release gate for unrelated development."
            )
            out_s.write(sanitize_credentials(msg) + "\n")
            return 0

    # Head commit SHA validation (candidate checkout / git HEAD / GITHUB_SHA)
    if is_candidate and (ci_mode or (root_dir / ".git").exists()):
        current_head_sha = _get_current_head_sha(root_dir, expected_sha=candidate["head_sha"])
        if current_head_sha is not None and current_head_sha.lower() != candidate["head_sha"].lower():
            return _format_error_result(
                candidate=candidate,
                reason=(
                    f"Candidate head commit SHA mismatch: expected commit SHA {candidate['head_sha'].lower()}, "
                    f"observed commit SHA {current_head_sha.lower()}"
                ),
                recovery=(
                    f"Align candidate branch head with declared head_sha or update {record_path} "
                    f"on base branch with observed commit SHA {current_head_sha.lower()}."
                ),
                observed=[f"Observed commit SHA {current_head_sha.lower()}"],
                unverified=["Candidate head commit alignment"],
                output_format=output_format,
                out_stream=out_s,
                err_stream=err_s,
                observed_labels=norm_labels,
            )

    required_gates = record.get("required_gates")
    live_evidence = record.get("live_evidence")
    if not required_gates or not isinstance(live_evidence, dict) or "runtime_fingerprint" not in live_evidence:
        return _format_error_result(
            reason="Mandatory CI and live evidence gates are missing from the release record",
            recovery="Declare Python CI versions and runtime fingerprint evidence.",
            unverified=["Required Python CI matrix gates", "Live runtime fingerprint evidence"],
            candidate=candidate, output_format=output_format, out_stream=out_s, err_stream=err_s,
            observed_labels=norm_labels,
        )

    api_client = api if api is not None else get_default_api(token=token)

    observed_items: List[str] = []
    inferred_items: List[str] = []
    unverified_items: List[str] = list(record.get("unverified_scenarios", []))

    # 1. Candidate Identity
    cand_check = verify_candidate_identity(api_client, candidate)
    if not cand_check["valid"]:
        return _format_error_result(
            candidate=candidate,
            reason=cand_check["reason"],
            recovery=cand_check.get("recovery", "Ensure candidate PR and head SHA match GitHub state."),
            observed=[cand_check["reason"]] if cand_check.get("observed_head_sha") else [],
            unverified=["Candidate head commit alignment"],
            output_format=output_format,
            out_stream=out_s,
            err_stream=err_s,
            observed_labels=norm_labels,
        )
    observed_items.append(cand_check["reason"])

    # 2. Required CI Gates
    req_gates = record.get("required_gates")
    if req_gates and "python_versions" in req_gates:
        ci_res = verify_ci_gates(api_client, candidate["repository"], candidate["head_sha"], req_gates["python_versions"])
        if not ci_res["valid"]:
            return _format_error_result(
                candidate=candidate,
                reason=ci_res["reason"],
                recovery=ci_res.get("recovery", "Ensure all required CI checks pass on current candidate head commit."),
                observed=observed_items,
                unverified=["Required Python CI matrix gates"],
                output_format=output_format,
                out_stream=out_s,
                err_stream=err_s,
                observed_labels=norm_labels,
            )
        observed_items.append(ci_res["reason"])

    # 3. Runtime Fingerprint Verification
    live_ev = record.get("live_evidence")
    if live_ev and "runtime_fingerprint" in live_ev:
        fp_res = validate_runtime_fingerprint(
            root_dir,
            declared_fingerprint=live_ev["runtime_fingerprint"],
            commit_sha=candidate["head_sha"],
        )
        if not fp_res["valid"]:
            return _format_error_result(
                candidate=candidate,
                reason=fp_res["reason"],
                recovery=fp_res.get("recovery", "Recompute runtime fingerprint for candidate commit and refresh the control record."),
                observed=observed_items,
                unverified=["Live runtime validation currency"],
                output_format=output_format,
                out_stream=out_s,
                err_stream=err_s,
                observed_labels=norm_labels,
            )
        observed_items.append(fp_res["reason"])

    # Determine preliminary verdict before report check
    constraints = record.get("operational_constraints", [])
    tentative_verdict = "ready with declared operational constraints" if constraints else "ready"

    # 4. Human Release Report Consistency
    if report_path:
        rep_res = verify_report_alignment(
            report_path,
            candidate,
            tentative_verdict,
            live_ev,
            report_content,
            repo_root=root_dir,
        )
        if not rep_res["valid"]:
            return _format_error_result(
                candidate=candidate,
                reason=rep_res["reason"],
                recovery=rep_res.get("recovery", "Update human release report to match verified candidate and evidence."),
                observed=observed_items,
                unverified=["Human release report agreement"],
                output_format=output_format,
                out_stream=out_s,
                err_stream=err_s,
                observed_labels=norm_labels,
            )
        observed_items.append(rep_res["reason"])

    # All mandatory gates passed!
    final_verdict = tentative_verdict
    gate_verdict = "GO"
    res = {
        "candidate": candidate,
        "verdict": "GO" if is_candidate else final_verdict,
        "gate_verdict": gate_verdict,
        "readiness": final_verdict,
        "reason": f"All release gates verified for candidate PR #{candidate['pull_request']}",
        "recovery": "",
        "evidence": {
            "observed": observed_items,
            "inferred": inferred_items,
            "unverified": unverified_items,
        },
    }

    if output_format == "json":
        out_s.write(json.dumps(res, indent=2) + "\n")
    else:
        if is_candidate:
            out_s.write(
                f"Release Candidate: {candidate['repository']} PR #{candidate['pull_request']}\n"
                f"Target Version: {candidate['target_version']}\n"
                f"Head SHA: {candidate['head_sha']}\n"
                f"VERDICT: GO\n"
                f"Reason: {res['reason']}\n"
            )
        else:
            out_s.write(
                f"Release Candidate: {candidate['repository']} PR #{candidate['pull_request']}\n"
                f"Target Version: {candidate['target_version']}\n"
                f"Head SHA: {candidate['head_sha']}\n"
                f"Verdict: {final_verdict.upper()}\n"
                f"Reason: {res['reason']}\n"
            )
    return 0


def add_release_verification_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the shared maintainer command options on either CLI entry point."""
    parser.add_argument(
        "--record",
        default="docs/release-evidence.json",
        metavar="PATH",
        help="Path to Control Record JSON document",
    )
    parser.add_argument(
        "--record-ref",
        default=None,
        metavar="COMMIT_SHA",
        help="Read record and report at an exact trusted Git commit outside the candidate head",
    )
    parser.add_argument(
        "--report",
        default="docs/production-release-audit.md",
        metavar="PATH",
        help="Path to human release audit report Markdown file",
    )
    parser.add_argument(
        "--repo",
        default=None,
        metavar="OWNER/REPO",
        help="Target GitHub repository in OWNER/REPO format",
    )
    parser.add_argument(
        "--token",
        default=None,
        metavar="TOKEN",
        help="GitHub API authentication token",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Run in CI mode (skip unrelated PRs or archived candidates)",
    )
    parser.add_argument(
        "--labels",
        default=None,
        metavar="LABELS",
        help="Comma-separated pull request labels for release gate candidate signal",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint for standalone invocation of verify_release."""
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="verify_release",
        description="Verify release candidate identity, CI jobs, live validation evidence, and release report.",
    )
    add_release_verification_arguments(parser)

    args = parser.parse_args(argv)
    return run_verify_release(
        record_path=args.record,
        report_path=args.report,
        repo=args.repo,
        token=args.token,
        output_format=args.format,
        ci_mode=args.ci,
        record_ref=args.record_ref,
        labels=args.labels,
    )


if __name__ == "__main__":
    sys.exit(main())
