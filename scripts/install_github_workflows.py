#!/usr/bin/env python3
"""Plan or open a reviewable PR for caller-selected GitHub Actions workflows."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote

from configure_github_repo import ApiError, GitHubApi, get_object

try:
    from scripts.hook_contracts import (
        MANIFEST_FILENAMES,
        NAME_PATTERN,
        HookPackage,
        HookValidationError,
        _safe_relative_path,
        filter_enabled_hooks,
        validate_hook_manifest,
        validate_hook_packages,
    )
except ImportError:
    from hook_contracts import (
        MANIFEST_FILENAMES,
        NAME_PATTERN,
        HookPackage,
        HookValidationError,
        _safe_relative_path,
        filter_enabled_hooks,
        validate_hook_manifest,
        validate_hook_packages,
    )

try:
    from scripts.agent_skill_upgrade import classify_catalog_upgrade, validate_removal_acknowledgment
except ImportError:  # pragma: no cover
    from agent_skill_upgrade import classify_catalog_upgrade, validate_removal_acknowledgment

try:
    from scripts.agent_skill_id import skill_id_from_path
except ImportError:  # pragma: no cover
    from agent_skill_id import skill_id_from_path


class StagingFailure(ApiError):
    """A failed Setup branch write with observed per-file recovery state."""

    def __init__(self, message, branch, file_statuses):
        super().__init__(message)
        self.branch = branch
        self.file_statuses = file_statuses


REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml\Z")
SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
MARKER = "# sciflow-cicd workflow-template="
LEGACY_MARKER = "# github-repo-setup workflow-template="
REVIEW_MARKER = "# open-code-review-setup workflow-template=open-code-review version=1"
REVIEW_RUNTIME_PATH = "scripts/review_issue_reporter.js"
REVIEW_RUNTIME_MARKER = b"// sciflow-cicd runtime-template=open-code-review version=1\n"
SETUP_PREFIX = "codex/workflows-"
LEGACY_REVIEW_BRANCH = "open-code-review-setup"
AGENT_SKILLS_MANIFEST = ".agents/skills/manifest.json"
AGENT_SKILL_ID = re.compile(r"(?:engineering|productivity)/[a-z0-9][a-z0-9-]*\Z")
AGENT_SKILLS_LICENSE = ".agents/skills/LICENSE"
TEMPLATES = {"pr-ci", "profile-ci", "dependency-review", "container-publish", "container"}
TRIGGERS = {"pull_request", "workflow_dispatch", "push", "schedule"}

ALLOWED_WORKFLOW_KEYS = {
    "template", "template_version", "filename", "triggers", "checkout_sha",
    "expected_sha", "runs_on", "timeout_minutes", "container", "env",
    "working_directory", "push_branches", "fork_guard", "is_public", "schedule",
}


def validate_runs_on(runs_on):
    """Validate runs_on value, defaulting to 'self-hosted' if None.

    Accepts non-empty string or non-empty list of unique label strings.
    """
    if runs_on is None:
        return "self-hosted"
    if isinstance(runs_on, str):
        if isinstance(runs_on, bool):
            raise ValueError("runs_on cannot be a boolean")
        val = runs_on.strip()
        if not val or "\n" in val or "\r" in val:
            raise ValueError(f"invalid runs_on string: {runs_on!r}")
        return val
    if isinstance(runs_on, list):
        if not runs_on:
            raise ValueError("runs_on list must not be empty")
        labels = []
        for item in runs_on:
            if not isinstance(item, str) or isinstance(item, bool) or not item.strip():
                raise ValueError(f"invalid runs_on label: {item!r}")
            clean = item.strip()
            if clean in labels:
                raise ValueError(f"duplicate runs_on label: {clean!r}")
            labels.append(clean)
        return labels
    raise ValueError(f"runs_on must be a string or list of strings, got {type(runs_on).__name__}")


def _format_runs_on(runs_on, indent="    ") -> str:
    """Format runs-on in generated YAML."""
    if isinstance(runs_on, str):
        return f"{indent}runs-on: {runs_on}"
    elif isinstance(runs_on, list):
        items = "\n".join(f"{indent}  - {item}" for item in runs_on)
        return f"{indent}runs-on:\n{items}"
    return f"{indent}runs-on: self-hosted"


def _is_self_hosted(runs_on) -> bool:
    if isinstance(runs_on, str):
        return "self-hosted" in runs_on or (runs_on != "ubuntu-24.04" and not runs_on.startswith("ubuntu-") and not runs_on.startswith("windows-") and not runs_on.startswith("macos-"))
    if isinstance(runs_on, list):
        return any(_is_self_hosted(item) for item in runs_on)
    return True
DEFAULT_CONTAINER_LOGIN_SHA = "9780b0c442fbb1117ed29e0efdff1e18412f7567"
DEFAULT_CONTAINER_BUILD_SHA = "471d1dc4e07e5cdedd4c2171150001c434f0b7a4"
DEFAULT_CACHE_SHA = "13aacd865c2ea734d23908f43a1f0d9bf8ec9456"
CI_PROFILES = {"python", "scientific-python", "cmake-mpi", "cmake"}
DEFAULT_CHECKOUT_SHA = "11bd71901bbe5b1630ceea73d27597364c9af683"
DEFAULT_DEPENDENCY_SHA = "56339e523c0409420f6c2c9a2f4292bbb3c07dd3"
DEFAULT_SETUP_PYTHON_SHA = "ece7cb06caefa5fff74198d8649806c4678c61a1"
DEFAULT_SETUP_UV_SHA = "d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86"
DEFAULT_PYTHON_VERSIONS = ["3.10", "3.11", "3.12"]
DEFAULT_PYTHON_COMMANDS = ["pytest -v"]
ACTION_VERSION_TAGS = {
    "11bd71901bbe5b1630ceea73d27597364c9af683": "v4.2.2",
    "56339e523c0409420f6c2c9a2f4292bbb3c07dd3": "v4.5.0",
    "ece7cb06caefa5fff74198d8649806c4678c61a1": "v5.4.0",
    "d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86": "v5.3.0",
    "16503c80cf5a76fa8f73f55462fa418931eb6f8b": "v5.3.0",
    "5a2ce3f5b92ee19cbb1541a4984c76d921601d7c": "v4.5.0",
    "08c6903cd8c0fde910a37f88322edcfb5dd907a8": "v5.0.0",
    "fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09": "v4.2.2",
    DEFAULT_CONTAINER_LOGIN_SHA: "v3.3.0",
    DEFAULT_CONTAINER_BUILD_SHA: "v6.15.0",
    DEFAULT_CACHE_SHA: "v4.2.0",
}


def _action_tag(sha: str) -> str:
    tag = ACTION_VERSION_TAGS.get(sha.lower())
    return f" # {tag}" if tag else ""
ISSUE_STATE_GATE_FILES = (
    "issue-state-gate.yml",
    "issue-state-gate-refresh.yml",
    "issue-state-gate-reconcile.yml",
    "issue-delivery-enforce.yml",
)
ISSUE_STATE_GATE_SCRIPTS = (
    "issue_state_gate.py",
    "refresh_issue_state_gate.py",
    "reconcile_issue_state_gate.py",
    "enforce_issue_delivery.py",
    "verify_issue_delivery.py",
    "configure_github_repo.py",
)
DOCUMENTATION_WORKFLOW = ".github/workflows/documentation-freshness.yml"
DOCUMENTATION_RUNTIME_MARKER = b"# sciflow-cicd runtime-template=documentation-freshness version=1\n"
DOCUMENTATION_SCRIPT_FILES = (
    "check_markdown_links.py", "documentation_freshness.py", "documentation_audit_runner.py",
    "documentation_issue_reporter.py",
)
DOCUMENTATION_GUIDE = "docs/documentation-freshness.md"
DOCUMENTATION_GUIDE_MARKER = "<!-- sciflow-cicd documentation-template=documentation-freshness version=1 -->\n"
DOCUMENTATION_CONFIG = ".github/documentation-freshness.json"
DOCUMENTATION_INVENTORY = ".github/documentation-skill-inventory.json"
DOCUMENTATION_INVENTORY_SHA256 = "6fdc34eb6c3c67a9751225c31de2a6ba3ca1d61a2607530382149c775bbd4723"


def _agent_skills_bundle():
    """Load the repository-pinned Matt Pocock snapshot and target inventory."""
    try:
        from scripts.provision_agent_skills import load_verified_catalog
    except ImportError:
        from provision_agent_skills import load_verified_catalog
    snapshot = Path(__file__).resolve().parents[1] / "data/agent-skills/mattpocock"
    return load_verified_catalog(snapshot)


def _parse_agent_skills_manifest(content):
    try:
        from scripts.provision_agent_skills import parse_agent_skills_manifest
    except ImportError:
        from provision_agent_skills import parse_agent_skills_manifest
    return parse_agent_skills_manifest(content)


def render_hook_packages(packages=None, opt_in=True):
    """Render validated hook package files for staging under .agents/hooks/<name>/...

    - If not opt_in or not packages: returns empty dict {}, no hooks staged.
    - Validates packages using validate_hook_packages(packages): if any package is invalid
      (schema, hash mismatch, traversal path, symlinks, missing entrypoint, unindexed file),
      fails before any staging write.
    - If valid and opt_in=True, returns mapping of .agents/hooks/<name>/... -> bytes.
    """
    if not packages:
        return {}
    if isinstance(packages, (str, Path, HookPackage)):
        packages = [packages]
    pkg_dirs = []
    for p in packages:
        if isinstance(p, HookPackage):
            pkg_dirs.append(p.package_dir)
        elif isinstance(p, (str, Path)):
            pkg_dirs.append(Path(p))
        else:
            raise HookValidationError(f"Invalid hook package entry: {p!r}")
    validated = validate_hook_packages(pkg_dirs)
    if not opt_in:
        return {}
    enabled = filter_enabled_hooks(validated)
    rendered = {}
    for pkg in enabled:
        manifest_rel = pkg.manifest_path.name
        rendered[f".agents/hooks/{pkg.manifest.name}/{manifest_rel}"] = pkg.manifest_path.read_bytes()
        for rel_file in sorted(pkg.manifest.files.keys()):
            file_path = pkg.package_dir / rel_file
            rendered[f".agents/hooks/{pkg.manifest.name}/{rel_file}"] = file_path.read_bytes()
    return rendered


def _verify_staged_hooks(files, hook_paths, hook_packages=None):
    """Verify that any .agents/hooks/ files in files match reviewed/validated hook packages."""
    if hook_packages is not None:
        expected = render_hook_packages(hook_packages, opt_in=True)
        if any(path not in expected or files[path] != (expected[path], None) for path in hook_paths):
            raise ValueError("hook files must exactly match reviewed and validated hook packages")
        if any(path not in hook_paths for path in expected):
            raise ValueError("not all rendered hook package files are included in files to stage")
        return

    hooks_by_name = {}
    for path in hook_paths:
        parts = path.split("/")
        if len(parts) < 4:
            raise ValueError(f"invalid hook path structure: {path}")
        hook_name = parts[2]
        if not NAME_PATTERN.fullmatch(hook_name):
            raise ValueError(f"invalid hook name in path: {path}")
        rel_path = "/".join(parts[3:])
        _safe_relative_path(rel_path, "hook staged file")
        hooks_by_name.setdefault(hook_name, {})[rel_path] = files[path]

    for hook_name, hook_files in hooks_by_name.items():
        found_manifests = [fname for fname in MANIFEST_FILENAMES if fname in hook_files]
        if not found_manifests:
            raise ValueError(f"hook {hook_name!r} missing manifest ({' or '.join(MANIFEST_FILENAMES)})")
        if len(found_manifests) > 1:
            raise ValueError(f"ambiguous hook manifests for {hook_name!r}")
        manifest_filename = found_manifests[0]
        manifest_tuple = hook_files[manifest_filename]
        if manifest_tuple[1] is not None:
            raise ValueError(f"hook manifest expected_sha must be None: {hook_name}")
        try:
            mdata = json.loads(manifest_tuple[0].decode("utf-8"))
        except Exception as e:
            raise ValueError(f"failed to parse hook manifest for {hook_name!r}: {e}") from e

        manifest = validate_hook_manifest(mdata)
        if manifest.name != hook_name:
            raise ValueError(f"hook directory name {hook_name!r} does not match manifest name {manifest.name!r}")
        if not manifest.opt_in:
            raise ValueError(f"hook {hook_name!r} manifest has opt_in=False; cannot stage opted-out hook")

        for rel_file, expected_sha in manifest.files.items():
            if rel_file not in hook_files:
                raise ValueError(f"declared hook file missing from staged files: {rel_file}")
            file_tuple = hook_files[rel_file]
            if file_tuple[1] is not None:
                raise ValueError(f"hook file expected_sha must be None: {rel_file}")
            actual_sha = hashlib.sha256(file_tuple[0]).hexdigest().lower()
            if actual_sha != expected_sha:
                raise ValueError(f"content hash mismatch for hook file {rel_file}: expected {expected_sha}, got {actual_sha}")

        for rel_file in hook_files:
            if rel_file == manifest_filename:
                continue
            if rel_file not in manifest.files:
                raise ValueError(f"unindexed file found in staged hook {hook_name!r}: {rel_file}")
RUNTIME_MARKER = b"# sciflow-cicd runtime-template=issue-state-gate version=1\n"
STATUS_CHECK_INLINE_SCRIPT = (
    "import json, sys, os\n"
    'needs = json.loads(os.environ.get("NEEDS_CONTEXT", "{}"))\n'
    'failed = [name for name, job in needs.items() if job.get("result") in ("failure", "cancelled")]\n'
    'skipped = [name for name, job in needs.items() if job.get("result") == "skipped"]\n'
    'success = [name for name, job in needs.items() if job.get("result") == "success"]\n'
    'print(f"Status check results - success: {success}, skipped: {skipped}, failed: {failed}")\n'
    "if failed:\n"
    '    sys.exit(f"Status check failed: {failed}")\n'
    'print("Status check passed.")'
)


def _yaml_string(value):
    return json.dumps(value, ensure_ascii=True)


def evaluate_status_check(needs):
    """Evaluate GitHub Actions needs context for stable status check reporting.

    Passing condition: zero failed or cancelled jobs (success or skipped are allowed).
    Returns (success: bool, message: str, details: dict).
    """
    if not isinstance(needs, dict):
        return False, "Invalid needs context", {"success": [], "skipped": [], "failed": []}
    failed = [name for name, job in needs.items() if isinstance(job, dict) and job.get("result") in ("failure", "cancelled")]
    skipped = [name for name, job in needs.items() if isinstance(job, dict) and job.get("result") == "skipped"]
    success = [name for name, job in needs.items() if isinstance(job, dict) and job.get("result") == "success"]
    is_success = len(failed) == 0
    details = {"success": sorted(success), "skipped": sorted(skipped), "failed": sorted(failed)}
    if is_success:
        msg = f"Stable status check passed: {len(success)} succeeded, {len(skipped)} skipped."
    else:
        msg = f"Stable status check failed: jobs {failed} did not succeed."
    return is_success, msg, details


def extract_profile_status_check(workflow_content: str) -> str:
    """Extract declared profile status-check job name from workflow YAML content.

    Returns the declared status check name if present (defaulting to 'status-check'),
    or an empty string if this is not a profile workflow or no status-check job is defined.
    """
    if not isinstance(workflow_content, str):
        return ""
    if "workflow-template=profile-ci" not in workflow_content and "status-check:" not in workflow_content:
        return ""
    declared_check = "status-check"
    m = re.search(r'status-check:\s*\n(?:\s+name:\s*(?:"([^"]+)"|\'([^\']+)\'|(\S+)))?', workflow_content)
    if m:
        val = m.group(1) or m.group(2) or m.group(3)
        if val:
            declared_check = val
    return declared_check


def inspect_remote_workflow_status_check(api, repository, path, ref=None):
    """Fetch a remote workflow from GitHub repository contents and extract its declared status check name.

    Returns declared status check string or None if unparseable/absent.
    """
    if not api or not repository or not path:
        return None
    try:
        path_encoded = quote(path.lstrip("/"), safe="/")
        base = f"/repos/{repository}"
        url = f"{base}/contents/{path_encoded}"
        if ref:
            url += f"?ref={quote(ref, safe='')}"
        status, body = api.request("GET", url)
        if status == 200 and isinstance(body, dict) and body.get("encoding") == "base64" and isinstance(body.get("content"), str):
            wf_bytes = base64.b64decode("".join(body["content"].split()))
            wf_str = wf_bytes.decode("utf-8", errors="replace")
            return extract_profile_status_check(wf_str) or None
    except Exception:
        pass
    return None


def detect_cmake_mpi_project(api=None, repository=None, default_branch=None, workspace_dir=None):
    """Detect if a repository or local workspace contains a CMake project with MPI/OpenCL.

    Returns a dict with:
        detected: bool - True if CMake project (CMakeLists.txt) is detected
        has_mpi: bool - True if MPI references found in CMakeLists.txt
        has_opencl: bool - True if OpenCL references found in CMakeLists.txt
        ranks: list[int] - Recommended smoke test rank counts [1, 2]
        runner_dependencies: list[str] - Recommended apt packages
        build_commands: list[str] - Recommended build commands
        test_commands: list[str] - Recommended test commands
        ccache: bool - Default True
        opencl: bool - Default matches has_opencl
        manual_build: bool - Default False
    """
    default_branch = default_branch or "main"
    cmake_text = None

    if api is not None and repository is not None:
        try:
            url = f"/repos/{repository}/contents/CMakeLists.txt?ref={quote(default_branch, safe='')}"
            status, body = api.request("GET", url)
            if status == 200 and isinstance(body, dict) and isinstance(body.get("content"), str):
                encoded = "".join(body["content"].split())
                cmake_text = base64.b64decode(encoded).decode("utf-8", errors="replace")
        except Exception:
            pass

    if cmake_text is None:
        dir_path = Path(workspace_dir) if workspace_dir else Path.cwd()
        cmake_file = dir_path / "CMakeLists.txt"
        if cmake_file.is_file():
            try:
                cmake_text = cmake_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

    if cmake_text is None:
        return {
            "detected": False,
            "has_mpi": False,
            "has_opencl": False,
        }

    has_mpi = any(token in cmake_text for token in ("MPI", "find_package(MPI", "MPI_C", "MPI_CXX", "mpi.h", "MPI_Init"))
    has_opencl = any(token.lower() in cmake_text.lower() for token in ("OpenCL", "find_package(OpenCL", "CL/cl.h", "opencl"))

    deps = ["libopenmpi-dev", "openmpi-bin"]
    if has_opencl:
        deps.extend(["pocl-opencl-icd", "ocl-icd-opencl-dev", "opencl-headers", "clinfo"])

    return {
        "detected": True,
        "has_mpi": has_mpi,
        "has_opencl": has_opencl,
        "ranks": [1, 2],
        "runner_dependencies": deps,
        "build_commands": ["cmake -B build -S .", "cmake --build build"],
        "test_commands": ["ctest --test-dir build --output-on-failure"],
        "ccache": True,
        "opencl": has_opencl,
        "manual_build": False,
    }


def _glob_to_regex(pat: str) -> re.Pattern:
    """Convert a glob pattern with ** globstar support into a compiled regex."""
    i, n = 0, len(pat)
    res = []
    while i < n:
        c = pat[i]
        if c == "*":
            if i + 1 < n and pat[i + 1] == "*":
                if i + 2 < n and pat[i + 2] == "/":
                    res.append("(?:.*/)?")
                    i += 3
                    continue
                else:
                    res.append(".*")
                    i += 2
                    continue
            else:
                res.append("[^/]*")
                i += 1
        elif c == "?":
            res.append("[^/]")
            i += 1
        else:
            res.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(res) + "$")


def match_path_patterns(changed_files, patterns):
    """Return True if any changed file matches any pattern in patterns."""
    import fnmatch
    for path in changed_files:
        clean_path = path.strip().lstrip("/")
        for pattern in patterns:
            pat = pattern.strip().lstrip("/")
            try:
                rx = _glob_to_regex(pat)
                if rx.match(clean_path):
                    return True
            except Exception:
                pass
            if fnmatch.fnmatch(clean_path, pat):
                return True
            if pat.endswith("/**"):
                prefix = pat[:-3]
                if clean_path.startswith(prefix + "/") or clean_path == prefix:
                    return True
            elif pat.endswith("/*"):
                prefix = pat[:-2]
                if clean_path.startswith(prefix + "/"):
                    return True
    return False


def _render_profile_workflow(workflow):
    """Render a focused CI profile workflow with stable status check reporting."""
    allowed = {
        "name", "template", "template_version", "filename", "triggers", "checkout_sha", "expected_sha",
        "profile", "profile_options", "paths", "concurrency", "status_check_name",
        "python", "cmake_mpi", "cmake-mpi", "commands", "test_commands", "lint_commands",
        "setup_python_sha", "setup_uv_sha", "action_sha", "lfs", "cache", "package_manager",
        "runs_on", "timeout_minutes", "container", "env", "working_directory", "push_branches",
        "fork_guard", "is_public", "schedule",
        "python_setup_mode", "install_commands", "post_merge_commands",
        "install_system_deps", "compilers", "omp_threads", "extended_ranks", "cache_sha",
    }
    if set(workflow) - allowed:
        raise ValueError(f"unsupported keys in profile workflow: {set(workflow) - allowed}")
    if type(workflow.get("template_version")) is not int or workflow["template_version"] != 1:
        raise ValueError("select a supported version 1 workflow template")
    if not isinstance(workflow.get("filename"), str) or not FILENAME.fullmatch(workflow["filename"]):
        raise ValueError("workflow filename must be a simple .yml or .yaml name")

    profile_name = workflow.get("profile")
    if not isinstance(profile_name, str) or profile_name not in CI_PROFILES:
        raise ValueError(f"unsupported CI profile: {profile_name!r}; choose 'python' or 'cmake-mpi'")

    opts = dict(workflow.get("profile_options") or {})
    if profile_name in ("python", "scientific-python"):
        python_opts = workflow.get("python") or {}
        if not isinstance(python_opts, dict):
            raise ValueError("python options must be an object")
        opts = {**python_opts, **opts}
    if profile_name in ("cmake-mpi", "cmake"):
        cmake_opts = workflow.get("cmake_mpi") or workflow.get("cmake-mpi") or {}
        if not isinstance(cmake_opts, dict):
            raise ValueError("cmake_mpi options must be an object")
        opts = {**cmake_opts, **opts}

    runs_on = validate_runs_on(workflow.get("runs_on") or opts.get("runs_on"))
    is_sh = _is_self_hosted(runs_on)

    triggers = workflow.get("triggers") or opts.get("triggers")
    if (not isinstance(triggers, list) or not triggers or any(not isinstance(trigger, str) for trigger in triggers) or
            len(triggers) != len(set(triggers)) or
            any(trigger not in TRIGGERS for trigger in triggers)):
        raise ValueError("select unique triggers")
    if "pull_request" not in triggers:
        raise ValueError("a reviewable workflow requires pull_request")
    if "fork_guard" in workflow and type(workflow["fork_guard"]) is not bool:
        raise ValueError("fork_guard must be a boolean")
    if "timeout_minutes" in workflow:
        tm = workflow["timeout_minutes"]
        if type(tm) is not int or tm <= 0:
            raise ValueError("timeout_minutes must be a positive integer")
    if "container" in workflow:
        cont = workflow["container"]
        if not isinstance(cont, str) or not cont.strip():
            raise ValueError("container must be a non-empty string")
    if "env" in workflow:
        env = workflow["env"]
        if not isinstance(env, dict) or not env or any(not isinstance(k, str) or not k.strip() for k in env.keys()):
            raise ValueError("env must be a non-empty dict of string keys")
    if "working_directory" in workflow:
        wd = workflow["working_directory"]
        if not isinstance(wd, str) or not wd.strip():
            raise ValueError("working_directory must be a non-empty string path")
    if "push_branches" in workflow:
        pb = workflow["push_branches"]
        if not isinstance(pb, list) or not pb or any(not isinstance(b, str) or not b.strip() for b in pb):
            raise ValueError("push_branches must be a non-empty list of branch strings")
    if "schedule" in workflow:
        sched = workflow["schedule"]
        if isinstance(sched, str):
            if not sched.strip():
                raise ValueError("schedule cron string must not be empty")
        elif isinstance(sched, list):
            if not sched or any(not isinstance(s, str) or not s.strip() for s in sched):
                raise ValueError("schedule list must contain non-empty cron strings")
        else:
            raise ValueError("schedule must be a cron string or list of cron strings")
    if not isinstance(workflow.get("checkout_sha"), str) or not SHA.fullmatch(workflow["checkout_sha"]):
        raise ValueError("checkout_sha must be a pinned 40-character commit SHA")
    if "expected_sha" in workflow and (not isinstance(workflow["expected_sha"], str) or
                                      not SHA.fullmatch(workflow["expected_sha"])):
        raise ValueError("expected_sha must be a 40-character blob SHA")
    if "setup_python_sha" in workflow and (not isinstance(workflow["setup_python_sha"], str) or
                                           not SHA.fullmatch(workflow["setup_python_sha"])):
        raise ValueError("setup_python_sha must be a pinned 40-character commit SHA")
    if "setup_uv_sha" in workflow and (not isinstance(workflow["setup_uv_sha"], str) or
                                       not SHA.fullmatch(workflow["setup_uv_sha"])):
        raise ValueError("setup_uv_sha must be a pinned 40-character commit SHA")
    if "cache_sha" in workflow and (not isinstance(workflow["cache_sha"], str) or
                                   not SHA.fullmatch(workflow["cache_sha"])):
        raise ValueError("cache_sha must be a pinned 40-character commit SHA")

    compilers = None
    extended_ranks = None
    if profile_name in ("python", "scientific-python"):
        versions = opts.get("versions") or opts.get("python_versions") or ["3.11", "3.12"]
        if not isinstance(versions, list) or not versions:
            raise ValueError("python versions must be a nonempty list")
        for ver in versions:
            if not isinstance(ver, str) or not re.fullmatch(r"\d+\.\d+", ver):
                raise ValueError(f"invalid python version: {ver!r}; expected format '3.11'")
        if len(versions) != len(set(versions)):
            raise ValueError("duplicate python versions specified")
        python_setup_mode = workflow.get("python_setup_mode") or opts.get("python_setup_mode", "setup-python")
        if python_setup_mode not in ("setup-python", "uv", "system"):
            raise ValueError("python_setup_mode must be setup-python, uv, or system")
        pkg_manager = workflow.get("package_manager") or opts.get("package_manager", "pip")
        if pkg_manager not in ("pip", "uv", "none"):
            raise ValueError("package_manager must be pip, uv, or none")
        install_commands = workflow.get("install_commands") or opts.get("install_commands")
        if install_commands is not None and (not isinstance(install_commands, list) or not install_commands or any(not isinstance(cmd, str) or not cmd.strip() for cmd in install_commands)):
            raise ValueError("install_commands must be a nonempty list of strings")
        commands = opts.get("commands") or opts.get("test_commands") or workflow.get("commands") or ["python3 -m unittest discover -s tests"]
        if not isinstance(commands, list) or not commands or any(not isinstance(cmd, str) or not cmd.strip() for cmd in commands):
            raise ValueError("test commands must be a nonempty list of strings")
        lint_commands = opts.get("lint_commands") or workflow.get("lint_commands")
        if lint_commands is not None and (not isinstance(lint_commands, list) or any(not isinstance(cmd, str) or not cmd.strip() for cmd in lint_commands)):
            raise ValueError("lint_commands must be a list of non-empty strings")
        post_merge_commands = workflow.get("post_merge_commands") or opts.get("post_merge_commands")
        if post_merge_commands is not None and (not isinstance(post_merge_commands, list) or not post_merge_commands or any(not isinstance(cmd, str) or not cmd.strip() for cmd in post_merge_commands)):
            raise ValueError("post_merge_commands must be a nonempty list of strings")
        lfs = bool(opts.get("lfs") if opts.get("lfs") is not None else workflow.get("lfs", False))
        cache = bool(opts.get("cache") if opts.get("cache") is not None else workflow.get("cache", True))
    else:
        lfs = bool(opts.get("lfs") if opts.get("lfs") is not None else workflow.get("lfs", False))
        ranks = opts["ranks"] if "ranks" in opts else opts.get("mpi_ranks")
        if ranks is None:
            ranks = [1, 2]
        if not isinstance(ranks, list) or not ranks:
            raise ValueError("mpi ranks must be a nonempty list")
        for r in ranks:
            if not isinstance(r, int) or isinstance(r, bool) or r <= 0:
                raise ValueError(f"mpi rank counts must be positive integers, got: {r!r}")
        if len(ranks) != len(set(ranks)):
            raise ValueError("duplicate mpi rank counts specified")

        compilers = opts.get("compilers") if "compilers" in opts else workflow.get("compilers")
        if compilers is not None:
            if not isinstance(compilers, list) or not compilers:
                raise ValueError("compilers must be a nonempty list of strings")
            for c in compilers:
                if not isinstance(c, str) or not c.strip():
                    raise ValueError("compilers must be a nonempty list of strings")
                if c.strip() not in ("gcc", "clang"):
                    raise ValueError(f"invalid compiler: {c!r}; choose 'gcc' or 'clang'")
            if len(compilers) != len(set(compilers)):
                raise ValueError("duplicate compilers specified")
            compilers = [c.strip() for c in compilers]

        omp_threads = opts.get("omp_threads") if "omp_threads" in opts else workflow.get("omp_threads", 2)
        if isinstance(omp_threads, list):
            if not omp_threads or any(not isinstance(t, int) or isinstance(t, bool) or t <= 0 for t in omp_threads):
                raise ValueError("omp_threads list must be nonempty positive integers")
            if len(omp_threads) != len(set(omp_threads)):
                raise ValueError("duplicate omp_threads specified")
        elif isinstance(omp_threads, int):
            if isinstance(omp_threads, bool) or omp_threads <= 0:
                raise ValueError("omp_threads must be a positive integer")
        elif isinstance(omp_threads, str):
            if not omp_threads.isdigit() or int(omp_threads) <= 0:
                raise ValueError("omp_threads must be a positive integer")
            omp_threads = int(omp_threads)
        else:
            raise ValueError("omp_threads must be an integer, string, or list of integers")

        if "install_system_deps" in opts:
            install_system_deps = bool(opts["install_system_deps"])
        elif "install_system_deps" in workflow:
            install_system_deps = bool(workflow["install_system_deps"])
        else:
            install_system_deps = not is_sh

        extended_ranks = opts.get("extended_ranks") if "extended_ranks" in opts else workflow.get("extended_ranks")
        if extended_ranks is not None:
            if not isinstance(extended_ranks, list) or not extended_ranks:
                raise ValueError("extended_ranks must be a nonempty list of positive integers")
            for r in extended_ranks:
                if not isinstance(r, int) or isinstance(r, bool) or r <= 0:
                    raise ValueError(f"extended_ranks must be positive integers, got: {r!r}")
            if len(extended_ranks) != len(set(extended_ranks)):
                raise ValueError("duplicate extended_ranks specified")

        post_merge_commands = opts.get("post_merge_commands") if "post_merge_commands" in opts else workflow.get("post_merge_commands")
        if post_merge_commands is not None and (not isinstance(post_merge_commands, list) or not post_merge_commands or any(not isinstance(cmd, str) or not cmd.strip() for cmd in post_merge_commands)):
            raise ValueError("post_merge_commands must be a nonempty list of strings")

        manual_build = bool(opts.get("manual_build", False))
        if "build_commands" in opts:
            build_commands = opts["build_commands"]
        elif manual_build:
            build_commands = ["make -j"]
        else:
            build_commands = ["cmake -B build -S .", "cmake --build build"]
        if not isinstance(build_commands, list) or not build_commands or any(not isinstance(cmd, str) or not cmd.strip() for cmd in build_commands):
            raise ValueError("build_commands must be a nonempty list of strings")
        if "test_commands" in opts:
            test_commands = opts["test_commands"]
        elif "commands" in opts:
            test_commands = opts["commands"]
        elif "commands" in workflow:
            test_commands = workflow["commands"]
        else:
            test_commands = ["ctest --test-dir build --output-on-failure"]
        if not isinstance(test_commands, list) or not test_commands or any(not isinstance(cmd, str) or not cmd.strip() for cmd in test_commands):
            raise ValueError("test_commands must be a nonempty list of strings")
        opencl = bool(opts.get("opencl", False))
        runner_deps = list(opts.get("runner_dependencies") or opts.get("apt_packages") or ["libopenmpi-dev", "openmpi-bin"])
        if opencl:
            for pkg in ["pocl-opencl-icd", "ocl-icd-opencl-dev", "opencl-headers", "clinfo"]:
                if pkg not in runner_deps:
                    runner_deps.append(pkg)
        if compilers and "clang" in compilers and "clang" not in runner_deps:
            runner_deps.append("clang")
        if not isinstance(runner_deps, list) or any(not isinstance(pkg, str) or not pkg.strip() for pkg in runner_deps):
            raise ValueError("runner_dependencies must be a list of package strings")
        ccache = bool(opts.get("ccache", True))
        cache = bool(opts.get("cache") if opts.get("cache") is not None else workflow.get("cache", False))
        cache_sha = (opts.get("cache_sha") if "cache_sha" in opts else workflow.get("cache_sha", DEFAULT_CACHE_SHA)).lower()
        if not isinstance(cache_sha, str) or not SHA.fullmatch(cache_sha):
            raise ValueError("cache_sha must be a pinned 40-character commit SHA")

    paths = workflow.get("paths") if "paths" in workflow else opts.get("paths")
    if paths is not None and (not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not p.strip() for p in paths)):
        raise ValueError("paths must be a nonempty list of pattern strings")
    status_check_name = workflow.get("status_check_name") or opts.get("status_check_name") or "status-check"
    if not isinstance(status_check_name, str) or not status_check_name.strip():
        raise ValueError("status_check_name must be a non-empty string")

    lines = [
        f"{MARKER}profile-ci version=1",
    ]
    if profile_name in ("cmake-mpi", "cmake"):
        if is_sh:
            lines += [
                "# EVIDENCE DEMARCATION:",
                "# Self-hosted runners execute on user-provisioned infrastructure.",
                "# Passing single-rank or multi-rank checks on CPU (including CPU-emulated OpenCL via POCL)",
                "# verifies host compilation and software execution only; it does NOT verify execution on",
                "# physical GPU hardware, accelerators, or multi-node cluster interconnects.",
                "# Real accelerator and device execution remain UNVERIFIED.",
            ]
        else:
            lines += [
                "# EVIDENCE DEMARCATION:",
                "# GitHub Actions hosted runners execute on virtual CPU-only environments.",
                "# Passing single-rank or multi-rank checks on CPU (including CPU-emulated OpenCL via POCL)",
                "# verifies host compilation and software execution only; it does NOT verify execution on",
                "# physical GPU hardware, accelerators, or multi-node cluster interconnects.",
                "# Real accelerator and device execution remain UNVERIFIED.",
            ]
    lines += [
        f"name: {_yaml_string('Python CI' if profile_name in ('python', 'scientific-python') else 'CMake MPI CI')}",
        "on:",
    ]
    for trigger in triggers:
        if trigger == "push" and workflow.get("push_branches"):
            lines.append("  push:")
            lines.append("    branches:")
            for b in workflow["push_branches"]:
                lines.append(f"      - {b}")
        elif trigger == "schedule" and workflow.get("schedule"):
            lines.append("  schedule:")
            sched = workflow["schedule"]
            if isinstance(sched, str):
                sched = [sched]
            for c in sched:
                lines.append(f"    - cron: {_yaml_string(c)}")
        else:
            lines.append(f"  {trigger}:")
    lines += [
        "permissions:",
        "  contents: read",
        "concurrency:",
        '  group: "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}"',
        "  cancel-in-progress: true",
        "jobs:",
    ]

    if paths:
        lines += [
            "  detect-changes:",
            '    name: "Detect Changes"',
            "    runs-on: ubuntu-24.04",
            "    permissions:",
            "      contents: read",
            "    outputs:",
            "      run_ci: ${{ steps.filter.outputs.run_ci }}",
            "    steps:",
            f"      - uses: actions/checkout@{workflow['checkout_sha'].lower()}{_action_tag(workflow['checkout_sha'])}",
            "        with:",
            "          fetch-depth: 100",
            "      - id: filter",
            '        name: "Check changed paths"',
            "        run: |",
            "          python3 -c '",
            "          import fnmatch, os, subprocess, sys",
            f"          patterns = {json.dumps(paths)}",
            '          base_ref = os.environ.get("GITHUB_BASE_REF")',
            '          event_name = os.environ.get("GITHUB_EVENT_NAME")',
            '          if event_name != "pull_request" or not base_ref:',
            '              with open(os.environ["GITHUB_OUTPUT"], "a") as f:',
            '                  f.write("run_ci=true\\n")',
            "              sys.exit(0)",
            '          subprocess.run(["git", "fetch", "origin", base_ref, "--depth=100"], check=False)',
            '          diff = subprocess.run(["git", "diff", "--name-only", f"origin/{base_ref}...HEAD"], capture_output=True, text=True, check=False)',
            '          files = [line.strip() for line in diff.stdout.splitlines() if line.strip()]',
            "          matched = False",
            "          for f in files:",
            "              clean_f = f.strip().lstrip('/')",
            "              for p in patterns:",
            "                  clean_p = p.strip().lstrip('/')",
            "                  if fnmatch.fnmatch(clean_f, clean_p):",
            "                      matched = True; break",
            "                  if clean_p.startswith('**/') and fnmatch.fnmatch(clean_f, clean_p[3:]):",
            "                      matched = True; break",
            "                  if '/**/' in clean_p and fnmatch.fnmatch(clean_f, clean_p.replace('/**/', '/')):",
            "                      matched = True; break",
            "                  if clean_p.endswith('/**') and (clean_f == clean_p[:-3] or clean_f.startswith(clean_p[:-3] + '/')):",
            "                      matched = True; break",
            "                  if clean_p.endswith('/*') and clean_f.startswith(clean_p[:-2] + '/'):",
            "                      matched = True; break",
            "              if matched: break",
            '          run_ci = "true" if matched else "false"',
            '          with open(os.environ["GITHUB_OUTPUT"], "a") as f:',
            '              f.write(f"run_ci={run_ci}\\n")',
            "          '",
        ]

    is_sh = _is_self_hosted(runs_on)
    fg_flag = workflow.get("fork_guard")
    is_pub = bool(workflow.get("is_public") or workflow.get("visibility") == "public")
    apply_fg = (fg_flag is True or (is_pub and fg_flag is not False)) and is_sh and ("pull_request" in triggers)

    if profile_name in ("python", "scientific-python"):
        ci_name = "Python CI (${{ matrix.python-version }})"
    elif compilers:
        ci_name = "CMake MPI CI (${{ matrix.compiler }}, ${{ matrix.ranks }}-rank)"
    else:
        ci_name = "CMake MPI CI (${{ matrix.ranks }}-rank)"

    lines += [
        "  ci:",
        f"    name: {_yaml_string(ci_name)}",
    ]
    if paths:
        lines.append("    needs: [detect-changes]")
        if apply_fg:
            lines.append("    if: needs.detect-changes.outputs.run_ci == 'true' && github.event.pull_request.head.repo.full_name == github.repository")
        else:
            lines.append("    if: needs.detect-changes.outputs.run_ci == 'true'")
    elif apply_fg:
        lines.append("    if: github.event.pull_request.head.repo.full_name == github.repository")
    lines.append(_format_runs_on(runs_on, indent="    "))
    if "timeout_minutes" in workflow:
        lines.append(f"    timeout-minutes: {workflow['timeout_minutes']}")
    if "container" in workflow:
        lines.append(f"    container: {_yaml_string(workflow['container'])}")
    if "env" in workflow:
        lines.append("    env:")
        for k, v in workflow["env"].items():
            lines.append(f"      {k}: {_yaml_string(str(v))}")
    if "working_directory" in workflow:
        lines += ["    defaults:", "      run:", f"        working-directory: {_yaml_string(workflow['working_directory'])}"]
    lines += [
        "    permissions:",
        "      contents: read",
    ]

    if profile_name in ("python", "scientific-python"):
        lines += [
            "    strategy:",
            "      fail-fast: false",
            "      matrix:",
            f"        python-version: {json.dumps(versions)}",
            "    steps:",
            f"      - uses: actions/checkout@{workflow['checkout_sha'].lower()}{_action_tag(workflow['checkout_sha'])}",
        ]
        if lfs:
            lines += [
                "        with:",
                "          lfs: true",
            ]
        if python_setup_mode != "system":
            setup_python_sha = workflow.get("setup_python_sha", DEFAULT_SETUP_PYTHON_SHA).lower()
            lines += [
                f"      - uses: actions/setup-python@{setup_python_sha}{_action_tag(setup_python_sha)}",
                "        with:",
                '          python-version: "${{ matrix.python-version }}"',
            ]
            if cache and (pkg_manager == "pip" and python_setup_mode != "uv"):
                lines.append('          cache: "pip"')
            if pkg_manager == "uv" or python_setup_mode == "uv":
                setup_uv_sha = workflow.get("setup_uv_sha", DEFAULT_SETUP_UV_SHA).lower()
                lines += [
                    f"      - uses: astral-sh/setup-uv@{setup_uv_sha}{_action_tag(setup_uv_sha)}",
                ]
                if cache:
                    lines += [
                        "        with:",
                        "          enable-cache: true",
                    ]
        if install_commands:
            for cmd in install_commands:
                lines.append("      - run: " + _yaml_string(cmd))
        if lint_commands:
            for cmd in lint_commands:
                lines.append("      - run: " + _yaml_string(cmd))
        for cmd in commands:
            lines.append("      - run: " + _yaml_string(cmd))
    else:
        matrix_dict = {}
        if compilers:
            matrix_dict["compiler"] = compilers
        matrix_dict["ranks"] = ranks
        if isinstance(omp_threads, list):
            matrix_dict["omp_threads"] = omp_threads

        lines += [
            "    strategy:",
            "      fail-fast: false",
            "      matrix:",
        ]
        for mk, mv in matrix_dict.items():
            lines.append(f"        {mk}: {json.dumps(mv)}")

        lines += [
            "    steps:",
            f"      - uses: actions/checkout@{workflow['checkout_sha'].lower()}{_action_tag(workflow['checkout_sha'])}",
        ]
        if lfs:
            lines += [
                "        with:",
                "          lfs: true",
            ]
        if is_sh:
            lines += [
                '      - name: "Demarcate Host CPU vs Accelerator Verification Boundary"',
                "        run: |",
                '          echo "::notice title=Device Boundary Demarcation::GitHub Actions executes on self-hosted runner. Device/GPU kernels tested under CPU fallbacks or OpenCL POCL do not constitute real GPU validation."',
                '          echo "Observed: Host compilation and CPU execution on self-hosted runner with ${{ matrix.ranks }} MPI rank(s)."',
                '          echo "Unverified: Real GPU/accelerator hardware execution and multi-node interconnect scaling."',
            ]
        else:
            lines += [
                '      - name: "Demarcate Host CPU vs Accelerator Verification Boundary"',
                "        run: |",
                '          echo "::notice title=Device Boundary Demarcation::GitHub Actions executes on hosted CPU runner. Device/GPU kernels tested under CPU fallbacks or OpenCL POCL do not constitute real GPU validation."',
                '          echo "Observed: Host compilation and CPU execution on ubuntu-24.04 with ${{ matrix.ranks }} MPI rank(s)."',
                '          echo "Unverified: Real GPU/accelerator hardware execution and multi-node interconnect scaling."',
            ]
        if opencl:
            lines += [
                '      - name: "Demarcate OpenCL Runtime Boundary"',
                "        run: |",
                '          echo "::notice title=OpenCL Device Boundary::OpenCL tests execute via CPU runtime (POCL). Real GPU device kernel verification is UNVERIFIED."',
                '          echo "OpenCL execution uses CPU POCL runtime; physical GPU accelerator execution is unverified."',
            ]
        if compilers:
            lines += [
                '      - name: "Set compiler environment"',
                "        run: |",
                '          if [ "${{ matrix.compiler }}" = "clang" ]; then',
                '            echo "CC=clang" >> "$GITHUB_ENV"',
                '            echo "CXX=clang++" >> "$GITHUB_ENV"',
                "          else",
                '            echo "CC=gcc" >> "$GITHUB_ENV"',
                '            echo "CXX=g++" >> "$GITHUB_ENV"',
                "          fi",
            ]
        if install_system_deps and runner_deps:
            pkgs_str = " ".join(runner_deps)
            lines.append(f"      - run: {_yaml_string('sudo apt-get update && sudo apt-get install -y --no-install-recommends ' + pkgs_str)}")
        if ccache:
            if install_system_deps:
                ccache_script = (
                    "sudo apt-get install -y --no-install-recommends ccache\n"
                    'echo "/usr/lib/ccache" >> "$GITHUB_PATH"\n'
                    'echo "CCACHE_DIR=${{ github.workspace }}/.ccache" >> "$GITHUB_ENV"\n'
                    'echo "CCACHE_MAXSIZE=500M" >> "$GITHUB_ENV"'
                )
            else:
                ccache_script = (
                    'echo "/usr/lib/ccache" >> "$GITHUB_PATH"\n'
                    'echo "CCACHE_DIR=${{ github.workspace }}/.ccache" >> "$GITHUB_ENV"\n'
                    'echo "CCACHE_MAXSIZE=500M" >> "$GITHUB_ENV"'
                )
            lines += [
                '      - name: "Configure compiler cache (ccache)"',
                "        run: |",
                *[f"          {line}" for line in ccache_script.splitlines()],
            ]
            if cache:
                cache_tag = _action_tag(cache_sha)
                if compilers:
                    key_str = "${{ runner.os }}-ccache-${{ matrix.compiler }}-${{ github.sha }}"
                    restore_keys = (
                        "${{ runner.os }}-ccache-${{ matrix.compiler }}-\n"
                        "${{ runner.os }}-ccache-"
                    )
                else:
                    key_str = "${{ runner.os }}-ccache-${{ github.sha }}"
                    restore_keys = "${{ runner.os }}-ccache-"
                lines += [
                    '      - name: "Restore compiler cache (ccache)"',
                    f"        uses: actions/cache@{cache_sha}{cache_tag}",
                    "        with:",
                    "          path: .ccache",
                    f"          key: {key_str}",
                    "          restore-keys: |",
                    *[f"            {rk}" for rk in restore_keys.splitlines()],
                ]
        if opencl:
            lines += [
                '      - name: "Verify OpenCL CPU platform and devices"',
                "        run: clinfo",
            ]
        for cmd in build_commands:
            lines.append("      - run: " + _yaml_string(cmd))
        omp_val = "${{ matrix.omp_threads }}" if isinstance(omp_threads, list) else str(omp_threads)
        for cmd in test_commands:
            lines += [
                "      - run: " + _yaml_string(cmd),
                "        env:",
                '          MPI_RANKS: "${{ matrix.ranks }}"',
                f'          OMP_NUM_THREADS: "{omp_val}"',
            ]

    dependent_jobs = "[detect-changes, ci]" if paths else "[ci]"
    lines += [
        "  status-check:",
        f"    name: {_yaml_string(status_check_name)}",
        f"    needs: {dependent_jobs}",
        "    if: always()",
        "    runs-on: ubuntu-24.04",
        "    permissions:",
        "      contents: read",
        "    steps:",
        '      - name: "Evaluate status check"',
        "        env:",
        "          NEEDS_CONTEXT: ${{ toJson(needs) }}",
        "        run: |",
        "          python3 -c '",
        *[f"          {line}" for line in STATUS_CHECK_INLINE_SCRIPT.splitlines()],
        "          '",
    ]
    if profile_name in ("python", "scientific-python") and post_merge_commands and "push" in triggers:
        lines += [
            "  post-merge:",
            '    name: "Post-merge"',
            _format_runs_on(runs_on, indent="    "),
            "    if: github.event_name == 'push'",
            "    permissions:",
            "      contents: read",
        ]
        if "timeout_minutes" in workflow:
            lines.append(f"    timeout-minutes: {workflow['timeout_minutes']}")
        if "container" in workflow:
            lines.append(f"    container: {_yaml_string(workflow['container'])}")
        if "env" in workflow:
            lines.append("    env:")
            for k, v in workflow["env"].items():
                lines.append(f"      {k}: {_yaml_string(str(v))}")
        if "working_directory" in workflow:
            lines += ["    defaults:", "      run:", f"        working-directory: {_yaml_string(workflow['working_directory'])}"]
        lines += [
            "    steps:",
            f"      - uses: actions/checkout@{workflow['checkout_sha'].lower()}{_action_tag(workflow['checkout_sha'])}",
        ]
        if lfs:
            lines += [
                "        with:",
                "          lfs: true",
            ]
        for cmd in post_merge_commands:
            lines.append("      - run: " + _yaml_string(cmd))

    if profile_name in ("cmake-mpi", "cmake") and "push" in triggers:
        if extended_ranks:
            lines += [
                "  extended-validation:",
                '    name: "Extended MPI Validation (${{ matrix.ranks }}-rank)"',
                _format_runs_on(runs_on, indent="    "),
                "    if: github.event_name == 'push'",
                "    permissions:",
                "      contents: read",
            ]
            if "timeout_minutes" in workflow:
                lines.append(f"    timeout-minutes: {workflow['timeout_minutes']}")
            if "container" in workflow:
                lines.append(f"    container: {_yaml_string(workflow['container'])}")
            if "env" in workflow:
                lines.append("    env:")
                for k, v in workflow["env"].items():
                    lines.append(f"      {k}: {_yaml_string(str(v))}")
            if "working_directory" in workflow:
                lines += ["    defaults:", "      run:", f"        working-directory: {_yaml_string(workflow['working_directory'])}"]
            lines += [
                "    strategy:",
                "      fail-fast: false",
                "      matrix:",
                f"        ranks: {json.dumps(extended_ranks)}",
                "    steps:",
                f"      - uses: actions/checkout@{workflow['checkout_sha'].lower()}{_action_tag(workflow['checkout_sha'])}",
            ]
            if lfs:
                lines += [
                    "        with:",
                    "          lfs: true",
                ]
            if compilers:
                lines += [
                    '      - name: "Set compiler environment"',
                    "        run: |",
                    '          echo "CC=gcc" >> "$GITHUB_ENV"',
                    '          echo "CXX=g++" >> "$GITHUB_ENV"',
                ]
            if install_system_deps and runner_deps:
                pkgs_str = " ".join(runner_deps)
                lines.append(f"      - run: {_yaml_string('sudo apt-get update && sudo apt-get install -y --no-install-recommends ' + pkgs_str)}")
            for cmd in build_commands:
                lines.append("      - run: " + _yaml_string(cmd))
            for cmd in test_commands:
                lines += [
                    "      - run: " + _yaml_string(cmd),
                    "        env:",
                    '          MPI_RANKS: "${{ matrix.ranks }}"',
                    f'          OMP_NUM_THREADS: "{omp_val}"',
                ]

        if post_merge_commands:
            lines += [
                "  post-merge:",
                '    name: "Post-merge"',
                _format_runs_on(runs_on, indent="    "),
                "    if: github.event_name == 'push'",
                "    permissions:",
                "      contents: read",
            ]
            if "timeout_minutes" in workflow:
                lines.append(f"    timeout-minutes: {workflow['timeout_minutes']}")
            if "container" in workflow:
                lines.append(f"    container: {_yaml_string(workflow['container'])}")
            if "env" in workflow:
                lines.append("    env:")
                for k, v in workflow["env"].items():
                    lines.append(f"      {k}: {_yaml_string(str(v))}")
            if "working_directory" in workflow:
                lines += ["    defaults:", "      run:", f"        working-directory: {_yaml_string(workflow['working_directory'])}"]
            lines += [
                "    steps:",
                f"      - uses: actions/checkout@{workflow['checkout_sha'].lower()}{_action_tag(workflow['checkout_sha'])}",
            ]
            if lfs:
                lines += [
                    "        with:",
                    "          lfs: true",
                ]
            for cmd in post_merge_commands:
                lines.append("      - run: " + _yaml_string(cmd))
    return ("\n".join(lines) + "\n").encode("utf-8")


def render(workflow):
    """Render one fixed, versioned template with caller-supplied values."""
    if not isinstance(workflow, dict):
        raise ValueError("workflow must be an object")
    template = workflow.get("template")
    if template == "issue-state-gate":
        if (set(workflow) - {"template", "template_version", "filename", "expected_sha"} or
                workflow.get("template_version") != 1 or workflow.get("filename") != "issue-state-gate.yml"):
            raise ValueError("select the trusted version 1 Issue State Gate workflow")
        source = Path(__file__).resolve().parent.parent / ".github/workflows/issue-state-gate.yml"
        content = source.read_bytes()
        if not (content.startswith(b"# sciflow-cicd workflow-template=issue-state-gate version=1\n") or
                content.startswith(b"# github-repo-setup workflow-template=issue-state-gate version=1\n")):
            raise ValueError("trusted Issue State Gate workflow marker is missing")
        return content

    if template == "profile-ci" or "profile" in workflow:
        return _render_profile_workflow(workflow)

    allowed = set(ALLOWED_WORKFLOW_KEYS)
    if template == "pr-ci":
        allowed |= {"commands"}
    elif template in ("container-publish", "container"):
        allowed |= {
            "action_sha", "login_action_sha", "build_action_sha",
            "registry", "dockerfile", "context", "tags",
        }
    elif template == "dependency-review":
        allowed |= {"action_sha", "fail_on_severity", "allow_licenses", "deny_licenses", "comment_summary_in_pr"}
    else:
        allowed |= {"action_sha"}
    if (template not in TEMPLATES or type(workflow.get("template_version")) is not int or
            workflow["template_version"] != 1 or set(workflow) - allowed):
        raise ValueError("select a supported version 1 workflow template")
    if not isinstance(workflow.get("filename"), str) or not FILENAME.fullmatch(workflow["filename"]):
        raise ValueError("workflow filename must be a simple .yml or .yaml name")
    runs_on = validate_runs_on(workflow.get("runs_on"))
    triggers = workflow.get("triggers")
    if (not isinstance(triggers, list) or not triggers or any(not isinstance(trigger, str) for trigger in triggers) or
            len(triggers) != len(set(triggers)) or
            any(trigger not in TRIGGERS for trigger in triggers)):
        raise ValueError("select unique triggers")
    if template not in ("container-publish", "container") and "pull_request" not in triggers:
        raise ValueError("a reviewable workflow requires pull_request")
    if not isinstance(workflow.get("checkout_sha"), str) or not SHA.fullmatch(workflow["checkout_sha"]):
        raise ValueError("checkout_sha must be a pinned 40-character commit SHA")
    if "expected_sha" in workflow and (not isinstance(workflow["expected_sha"], str) or
                                      not SHA.fullmatch(workflow["expected_sha"])):
        raise ValueError("expected_sha must be a 40-character blob SHA")
    if "fork_guard" in workflow and type(workflow["fork_guard"]) is not bool:
        raise ValueError("fork_guard must be a boolean")
    if "timeout_minutes" in workflow:
        tm = workflow["timeout_minutes"]
        if type(tm) is not int or tm <= 0:
            raise ValueError("timeout_minutes must be a positive integer")
    if "container" in workflow:
        cont = workflow["container"]
        if not isinstance(cont, str) or not cont.strip():
            raise ValueError("container must be a non-empty string")
    if "env" in workflow:
        env = workflow["env"]
        if not isinstance(env, dict) or not env or any(not isinstance(k, str) or not k.strip() for k in env.keys()):
            raise ValueError("env must be a non-empty dict of string keys")
    if "working_directory" in workflow:
        wd = workflow["working_directory"]
        if not isinstance(wd, str) or not wd.strip():
            raise ValueError("working_directory must be a non-empty string path")
    if "push_branches" in workflow:
        pb = workflow["push_branches"]
        if not isinstance(pb, list) or not pb or any(not isinstance(b, str) or not b.strip() for b in pb):
            raise ValueError("push_branches must be a non-empty list of branch strings")
    if "schedule" in workflow:
        sched = workflow["schedule"]
        if isinstance(sched, str):
            if not sched.strip():
                raise ValueError("schedule cron string must not be empty")
        elif isinstance(sched, list):
            if not sched or any(not isinstance(s, str) or not s.strip() for s in sched):
                raise ValueError("schedule list must contain non-empty cron strings")
        else:
            raise ValueError("schedule must be a cron string or list of cron strings")
    if template == "pr-ci":
        commands = workflow.get("commands")
        if (not isinstance(commands, list) or not commands or any(
                not isinstance(command, str) or not command.strip() or "\x00" in command
                for command in commands)):
            raise ValueError("PR CI needs caller-supplied nonempty commands")
    elif template in ("container-publish", "container"):
        for key in ("action_sha", "login_action_sha", "build_action_sha"):
            if key in workflow and (not isinstance(workflow[key], str) or not SHA.fullmatch(workflow[key])):
                raise ValueError(f"{key} must be a pinned 40-character commit SHA")
        if "registry" in workflow:
            reg = workflow["registry"]
            if not isinstance(reg, str) or not reg.strip():
                raise ValueError("registry must be a non-empty string")
        if "dockerfile" in workflow:
            df = workflow["dockerfile"]
            if not isinstance(df, str) or not df.strip():
                raise ValueError("dockerfile must be a non-empty string")
        if "context" in workflow:
            ctx = workflow["context"]
            if not isinstance(ctx, str) or not ctx.strip():
                raise ValueError("context must be a non-empty string")
        if "tags" in workflow:
            raw_t = workflow["tags"]
            if isinstance(raw_t, str):
                if not raw_t.strip():
                    raise ValueError("tags string must not be empty")
            elif isinstance(raw_t, list):
                if not raw_t or any(not isinstance(t, str) or not t.strip() for t in raw_t):
                    raise ValueError("tags list must contain non-empty strings")
            else:
                raise ValueError("tags must be a string or list of strings")
    else:
        if not isinstance(workflow.get("action_sha"), str) or not SHA.fullmatch(workflow["action_sha"]):
            raise ValueError("action_sha must be a pinned 40-character commit SHA")
        if "fail_on_severity" in workflow:
            sev = workflow["fail_on_severity"]
            if not isinstance(sev, str) or sev.lower() not in {"low", "moderate", "high", "critical"}:
                raise ValueError("fail_on_severity must be one of: low, moderate, high, critical")
        if "allow_licenses" in workflow:
            lic = workflow["allow_licenses"]
            if isinstance(lic, list):
                if not lic or any(not isinstance(x, str) or not x.strip() for x in lic):
                    raise ValueError("allow_licenses list must contain non-empty strings")
            elif isinstance(lic, str):
                if not lic.strip():
                    raise ValueError("allow_licenses string must not be empty")
            else:
                raise ValueError("allow_licenses must be a list of strings or comma-separated string")
        if "deny_licenses" in workflow:
            lic = workflow["deny_licenses"]
            if isinstance(lic, list):
                if not lic or any(not isinstance(x, str) or not x.strip() for x in lic):
                    raise ValueError("deny_licenses list must contain non-empty strings")
            elif isinstance(lic, str):
                if not lic.strip():
                    raise ValueError("deny_licenses string must not be empty")
            else:
                raise ValueError("deny_licenses must be a list of strings or comma-separated string")
        if "comment_summary_in_pr" in workflow:
            comm = workflow["comment_summary_in_pr"]
            if isinstance(comm, str):
                if comm.lower() not in {"always", "never", "on-failure"}:
                    raise ValueError("comment_summary_in_pr must be one of: always, never, on-failure")
            elif not isinstance(comm, bool):
                raise ValueError("comment_summary_in_pr must be a boolean or one of: always, never, on-failure")

    if template in ("container-publish", "container"):
        login_sha = workflow.get("login_action_sha", DEFAULT_CONTAINER_LOGIN_SHA).lower()
        build_sha = workflow.get("build_action_sha", workflow.get("action_sha", DEFAULT_CONTAINER_BUILD_SHA)).lower()
        registry = workflow.get("registry", "ghcr.io")
        dockerfile = workflow.get("dockerfile", "Dockerfile")
        context = workflow.get("context", ".")
        tags = workflow.get("tags", ["ghcr.io/${{ github.repository }}:${{ github.sha }}"])

        is_sh = _is_self_hosted(runs_on)
        fg_flag = workflow.get("fork_guard")
        is_pub = bool(workflow.get("is_public") or workflow.get("visibility") == "public")
        apply_fg = (fg_flag is True or (is_pub and fg_flag is not False)) and is_sh and ("pull_request" in triggers)

        lines = [
            f"{MARKER}{template} version=1",
            f"name: {_yaml_string('Publish Container')}",
            "on:",
        ]
        for trigger in triggers:
            if trigger == "push" and workflow.get("push_branches"):
                lines.append("  push:")
                lines.append("    branches:")
                for b in workflow["push_branches"]:
                    lines.append(f"      - {b}")
            elif trigger == "schedule" and workflow.get("schedule"):
                lines.append("  schedule:")
                sched = workflow["schedule"]
                if isinstance(sched, str):
                    sched = [sched]
                for c in sched:
                    lines.append(f"    - cron: {_yaml_string(c)}")
            else:
                lines.append(f"  {trigger}:")
        lines += [
            "permissions:",
            "  contents: read",
            "  packages: write",
            "jobs:",
            "  publish-container:",
        ]
        if apply_fg:
            lines.append("    if: github.event.pull_request.head.repo.full_name == github.repository")
        lines.append(_format_runs_on(runs_on, indent="    "))
        lines += [
            "    steps:",
            f"      - uses: actions/checkout@{workflow['checkout_sha'].lower()}{_action_tag(workflow['checkout_sha'])}",
            f"      - name: {'Log in to GitHub Container Registry' if registry == 'ghcr.io' else f'Log in to {registry}'}",
            "        if: github.event_name != 'pull_request'",
            f"        uses: docker/login-action@{login_sha}{_action_tag(login_sha)}",
            "        with:",
            f"          registry: {registry}",
            "          username: ${{ github.actor }}",
            "          password: ${{ secrets.GITHUB_TOKEN }}",
            "      - name: Build and push container image",
            f"        uses: docker/build-push-action@{build_sha}{_action_tag(build_sha)}",
            "        with:",
            f"          context: {context}",
        ]
        if dockerfile != "Dockerfile":
            lines.append(f"          file: {dockerfile}")
        lines.append("          push: ${{ github.event_name != 'pull_request' }}")
        if isinstance(tags, str):
            if "\n" in tags:
                lines.append("          tags: |")
                for t in tags.splitlines():
                    if t.strip():
                        lines.append(f"            {t.strip()}")
            else:
                lines.append(f"          tags: {tags}")
        elif isinstance(tags, list):
            if len(tags) == 1:
                lines.append(f"          tags: {tags[0]}")
            else:
                lines.append("          tags: |")
                for t in tags:
                    lines.append(f"            {t}")
        return ("\n".join(lines) + "\n").encode("utf-8")

    is_sh = _is_self_hosted(runs_on)
    fg_flag = workflow.get("fork_guard")
    is_pub = bool(workflow.get("is_public") or workflow.get("visibility") == "public")
    apply_fg = (fg_flag is True or (is_pub and fg_flag is not False)) and is_sh and ("pull_request" in triggers)

    lines = [f"{MARKER}{template} version=1", f"name: {_yaml_string('PR CI' if template == 'pr-ci' else 'Dependency Review')}", "on:"]
    for trigger in triggers:
        if trigger == "push" and workflow.get("push_branches"):
            lines.append("  push:")
            lines.append("    branches:")
            for b in workflow["push_branches"]:
                lines.append(f"      - {b}")
        elif trigger == "schedule" and workflow.get("schedule"):
            lines.append("  schedule:")
            sched = workflow["schedule"]
            if isinstance(sched, str):
                sched = [sched]
            for c in sched:
                lines.append(f"    - cron: {_yaml_string(c)}")
        else:
            lines.append(f"  {trigger}:")
    lines += ["permissions:", "  contents: read"]
    if template == "dependency-review":
        lines.append("  pull-requests: read")
    lines += ["jobs:", "  " + ("ci:" if template == "pr-ci" else "dependency-review:")]
    if apply_fg:
        lines.append("    if: github.event.pull_request.head.repo.full_name == github.repository")
    lines += ["    permissions:", "      contents: read"]
    if template == "dependency-review":
        lines.append("      pull-requests: read")
    if template == "pr-ci":
        lines.append(_format_runs_on(runs_on, indent="    "))
        if "timeout_minutes" in workflow:
            lines.append(f"    timeout-minutes: {workflow['timeout_minutes']}")
        if "container" in workflow:
            lines.append(f"    container: {_yaml_string(workflow['container'])}")
        if "env" in workflow:
            lines.append("    env:")
            for k, v in workflow["env"].items():
                lines.append(f"      {k}: {_yaml_string(str(v))}")
        if "working_directory" in workflow:
            lines += ["    defaults:", "      run:", f"        working-directory: {_yaml_string(workflow['working_directory'])}"]
    else:
        lines.append(_format_runs_on(runs_on, indent="    "))
    lines += ["    steps:",
              f"      - uses: actions/checkout@{workflow['checkout_sha'].lower()}{_action_tag(workflow['checkout_sha'])}"]
    if template == "pr-ci":
        for command in commands:
            lines.append("      - run: " + _yaml_string(command))
    else:
        lines.append(f"      - uses: actions/dependency-review-action@{workflow['action_sha'].lower()}{_action_tag(workflow['action_sha'])}")
        with_lines = []
        if "fail_on_severity" in workflow:
            with_lines.append(f"          fail-on-severity: {workflow['fail_on_severity'].lower()}")
        if "allow_licenses" in workflow:
            lic = workflow["allow_licenses"]
            val = ", ".join(x.strip() for x in lic) if isinstance(lic, list) else lic.strip()
            with_lines.append(f"          allow-licenses: {_yaml_string(val)}")
        if "deny_licenses" in workflow:
            lic = workflow["deny_licenses"]
            val = ", ".join(x.strip() for x in lic) if isinstance(lic, list) else lic.strip()
            with_lines.append(f"          deny-licenses: {_yaml_string(val)}")
        if "comment_summary_in_pr" in workflow:
            comm = workflow["comment_summary_in_pr"]
            val = "always" if comm is True else ("never" if comm is False else comm.lower())
            with_lines.append(f"          comment-summary-in-pr: {_yaml_string(val)}")
        if with_lines:
            lines.append("        with:")
            lines.extend(with_lines)
    return ("\n".join(lines) + "\n").encode("utf-8")


def render_issue_state_gate_suite():
    """Return trusted workflows and all Python modules called by them."""
    workflow_dir = Path(__file__).resolve().parent.parent / ".github/workflows"
    files = {}
    for filename in ISSUE_STATE_GATE_FILES:
        content = (workflow_dir / filename).read_bytes()
        template = filename.removesuffix(".yml")
        marker = f"# sciflow-cicd workflow-template={template} version=1\n".encode()
        legacy_marker = f"# github-repo-setup workflow-template={template} version=1\n".encode()
        if not (content.startswith(marker) or content.startswith(legacy_marker)):
            raise ValueError(f"trusted {template} workflow marker is missing")
        files[".github/workflows/" + filename] = (content, None)
    script_dir = Path(__file__).resolve().parent
    for filename in ISSUE_STATE_GATE_SCRIPTS:
        source = (script_dir / filename).read_bytes()
        if not source.startswith(b"#!/usr/bin/env python3\n"):
            raise ValueError(f"trusted {filename} runtime entrypoint is missing")
        files["scripts/" + filename] = (RUNTIME_MARKER + source, None)
    return files


def render_documentation_suite(expected_sha=None):
    """Return the trusted report workflow, scripts, and target operator guide."""
    root = Path(__file__).resolve().parent.parent
    workflow = (root / DOCUMENTATION_WORKFLOW).read_bytes()
    marker = b"# sciflow-cicd workflow-template=documentation-freshness version=1\n"
    legacy_marker = b"# github-repo-setup workflow-template=documentation-freshness version=1\n"
    if not (workflow.startswith(marker) or workflow.startswith(legacy_marker)) or b'DOCS_TARGET_MODE: "false"' not in workflow:
        raise ValueError("trusted documentation freshness workflow marker is missing")
    workflow = workflow.replace(b'DOCS_TARGET_MODE: "false"', b'DOCS_TARGET_MODE: "true"', 1)
    files = {DOCUMENTATION_WORKFLOW: (workflow, expected_sha)}
    for filename in DOCUMENTATION_SCRIPT_FILES:
        source = (root / "scripts" / filename).read_bytes()
        if not source.startswith(b"#!/usr/bin/env python3\n"):
            raise ValueError(f"trusted documentation runtime entrypoint is missing: {filename}")
        files["scripts/" + filename] = (DOCUMENTATION_RUNTIME_MARKER + source, None)
    guide = (root / DOCUMENTATION_GUIDE).read_bytes()
    if not guide.startswith(DOCUMENTATION_GUIDE_MARKER.encode()):
        raise ValueError("trusted documentation operator guide marker is missing")
    files[DOCUMENTATION_GUIDE] = (guide, None)
    config = {"schema": "sciflow-cicd/documentation-freshness/v1",
              "managed_by": "sciflow-cicd", "paths": [DOCUMENTATION_GUIDE]}
    files[DOCUMENTATION_CONFIG] = ((json.dumps(config, indent=2, sort_keys=True) + "\n").encode(), None)
    inventory = (root / "data/agent-skills/mattpocock/manifest.json").read_bytes()
    if hashlib.sha256(inventory).hexdigest() != DOCUMENTATION_INVENTORY_SHA256:
        raise ValueError("trusted documentation skill inventory hash mismatch")
    files[DOCUMENTATION_INVENTORY] = (inventory, None)
    return files


def render_review_reporter():
    """Return the trusted Advisory Review runtime staged with its workflow."""
    source = (Path(__file__).resolve().parent / "review_issue_reporter.js").read_bytes()
    if not source.startswith(REVIEW_RUNTIME_MARKER):
        raise ValueError("trusted Advisory Review reporter marker is missing")
    return source


def _managed_documentation_config(content):
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return False
    return (isinstance(value, dict) and set(value) == {"schema", "managed_by", "paths"} and
            value.get("schema") in ("sciflow-cicd/documentation-freshness/v1", "github-repo-setup/documentation-freshness/v1") and
            value.get("managed_by") in ("sciflow-cicd", "github-repo-setup") and
            value.get("paths") == [DOCUMENTATION_GUIDE])


def validate_policy(policy):
    if (not isinstance(policy, dict) or set(policy) != {"version", "workflows"} or
            type(policy["version"]) is not int or policy["version"] != 1 or
            not isinstance(policy["workflows"], list) or not policy["workflows"]):
        raise ValueError("policy needs version 1 and a nonempty workflows list")
    files = {}
    for workflow in policy["workflows"]:
        content = render(workflow)
        path = ".github/workflows/" + workflow["filename"]
        if path in files:
            raise ValueError("duplicate workflow filename")
        files[path] = (content, workflow.get("expected_sha"))
        if workflow.get("template") == "issue-state-gate":
            del files[path]
            for trusted_path, trusted_file in render_issue_state_gate_suite().items():
                if trusted_path in files:
                    raise ValueError("duplicate workflow filename")
                files[trusted_path] = trusted_file
    return files


def _verify_action_commits(api, selected):
    for repository, sha in sorted(set(selected)):
        path = f"/repos/{repository}/git/commits/{sha}"
        status, body = api.request("GET", path)
        if status == 404:
            raise ValueError(f"pinned action commit does not exist: {repository}@{sha}")
        if status != 200 or not isinstance(body, dict) or body.get("sha", "").lower() != sha:
            raise ApiError(f"cannot verify pinned action commit: {repository}@{sha}")


def _file(api, base, path, branch):
    url = base + "/contents/" + quote(path, safe="/") + "?ref=" + quote(branch, safe="")
    status, body = api.request("GET", url)
    if status == 404:
        return None
    if status != 200 or not isinstance(body, dict) or body.get("type") != "file" or body.get("encoding") != "base64":
        raise ApiError(f"cannot verify workflow file: {path} on {branch}")
    try:
        encoded = body["content"]
        if not isinstance(encoded, str):
            raise TypeError("content is not a string")
        data = base64.b64decode("".join(encoded.split()), validate=True)
    except (KeyError, TypeError, ValueError):
        raise ApiError(f"malformed workflow file: {path} on {branch}") from None
    if not isinstance(body.get("sha"), str):
        raise ApiError(f"workflow blob SHA unavailable: {path} on {branch}")
    return data, body["sha"]


def _ref(api, base, branch):
    status, body = api.request("GET", base + "/git/ref/heads/" + quote(branch, safe="/"))
    if status == 404:
        return None
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("object"), dict):
        raise ApiError("cannot verify branch reference")
    sha = body["object"].get("sha")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ApiError("branch commit SHA unavailable")
    return sha


def _pull(api, base, owner, branch, default):
    path = (base + "/pulls?head=" + quote(owner + ":" + branch, safe="") +
            "&base=" + quote(default, safe="") + "&state=open&per_page=100")
    status, pulls = api.request("GET", path)
    if status != 200 or not isinstance(pulls, list) or len(pulls) > 1:
        raise ApiError("cannot uniquely verify workflow pull request")
    return pulls[0] if pulls else None


def _open_setup_pulls(api, base, default):
    path = base + "/pulls?state=open&base=" + quote(default, safe="") + "&per_page=100"
    status, pulls = api.request("GET", path)
    if status != 200 or not isinstance(pulls, list) or len(pulls) >= 100:
        raise ApiError("cannot verify all open Setup Pull Requests")
    found = []
    for pull in pulls:
        if not isinstance(pull, dict) or not isinstance(pull.get("head"), dict):
            raise ApiError("cannot verify open Setup Pull Request metadata")
        branch = pull["head"].get("ref")
        if branch == LEGACY_REVIEW_BRANCH or (isinstance(branch, str) and branch.startswith(SETUP_PREFIX)):
            if not isinstance(pull.get("html_url"), str):
                raise ApiError("Setup Pull Request URL unavailable")
            found.append(pull)
    return found


def _is_managed_content(path, content, authored_paths=()):
    """Return True if the file content matches known managed templates (authored files are unmanaged)."""
    if path in authored_paths:
        return False
    if path.startswith("scripts/"):
        return content.startswith((RUNTIME_MARKER, DOCUMENTATION_RUNTIME_MARKER, REVIEW_RUNTIME_MARKER))
    if path == DOCUMENTATION_GUIDE:
        return content.startswith(DOCUMENTATION_GUIDE_MARKER.encode())
    if path == DOCUMENTATION_CONFIG:
        return _managed_documentation_config(content)
    if path == DOCUMENTATION_INVENTORY:
        return hashlib.sha256(content).hexdigest() == DOCUMENTATION_INVENTORY_SHA256
    if path == AGENT_SKILLS_MANIFEST:
        return _parse_agent_skills_manifest(content) is not None
    if path.startswith(".agents/skills/"):
        reviewed_skills, _ = _agent_skills_bundle()
        return reviewed_skills.get(path) == content
    if path.startswith(".agents/hooks/"):
        if path.endswith(("/manifest.json", "/hook.json")):
            try:
                data = json.loads(content.decode("utf-8"))
                validate_hook_manifest(data)
                return True
            except Exception:
                return False
        return False
    if path == ".github/workflows/open-code-review.yml":
        return content.startswith((
            REVIEW_MARKER.encode(),
            (MARKER + "open-code-review version=1").encode(),
            (LEGACY_MARKER + "open-code-review version=1").encode(),
        ))
    return content.startswith((MARKER.encode(), LEGACY_MARKER.encode()))


def _check_branch_diff(api, base, default, branch, selected_paths):
    path = base + "/compare/" + quote(default, safe="") + "..." + quote(branch, safe="/")
    status, comparison = api.request("GET", path)
    if status != 200 or not isinstance(comparison, dict) or not isinstance(comparison.get("files"), list):
        raise ApiError("cannot verify workflow branch diff")
    changed = comparison["files"]
    if (len(changed) >= 300 or
            (isinstance(comparison.get("total_files"), int) and comparison["total_files"] != len(changed))):
        raise ApiError("workflow branch diff is too large to verify completely")
    if any(not isinstance(item, dict) or item.get("filename") not in selected_paths or
           item.get("status") not in ("added", "modified", "removed") for item in changed):
        raise ValueError("unrelated branch changes detected; refusing workflow PR")


AUTOMATION_COMMIT_PREFIXES = (
    "Install selected workflow template",
    "Update selected workflow template",
    "Stage selected repository asset",
    "Install selected repository asset",
    "Update selected repository asset",
    "Update selected workflow templates",
)


class CommitMetadata:
    """Encapsulates commit author, committer, and verification metadata to detect owner vs automation commits."""

    def __init__(self, commit_dict):
        self.raw = commit_dict if isinstance(commit_dict, dict) else {}
        git_commit = self.raw.get("commit", {}) if isinstance(self.raw.get("commit"), dict) else {}
        author_info = git_commit.get("author", {}) if isinstance(git_commit.get("author"), dict) else {}
        committer_info = git_commit.get("committer", {}) if isinstance(git_commit.get("committer"), dict) else {}

        self.author_name = author_info.get("name", "")
        self.author_email = author_info.get("email", "")
        self.committer_name = committer_info.get("name", "")
        self.committer_email = committer_info.get("email", "")

        gh_author = self.raw.get("author", {}) if isinstance(self.raw.get("author"), dict) else {}
        gh_committer = self.raw.get("committer", {}) if isinstance(self.raw.get("committer"), dict) else {}

        self.author_login = gh_author.get("login", "")
        self.author_type = gh_author.get("type", "")
        self.committer_login = gh_committer.get("login", "")
        self.message = git_commit.get("message", "")

    def is_owner_edit(self) -> bool:
        """Return True if metadata indicates a human owner commit rather than tool automation."""
        is_api_automation_committer = (
            self.committer_login in ("web-flow", "github-actions[bot]", "github-actions") or
            self.committer_name == "GitHub" or
            self.committer_email == "noreply@github.com"
        )
        is_bot_author = self.author_type == "Bot" or self.author_login.endswith("[bot]")
        has_automation_prefix = self.message.startswith(AUTOMATION_COMMIT_PREFIXES)

        if is_api_automation_committer or is_bot_author:
            return not has_automation_prefix

        return True


def _check_branch_owner_edits(api, base, default, branch):
    """Return True if branch contains commits authored by a human/owner rather than tool automation."""
    path = base + "/compare/" + quote(default, safe="") + "..." + quote(branch, safe="/")
    status, comparison = api.request("GET", path)
    if status != 200 or not isinstance(comparison, dict):
        raise ApiError("cannot verify workflow branch comparison")
    commits = comparison.get("commits", [])
    if not isinstance(commits, list):
        return False
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        meta = CommitMetadata(commit)
        if meta.is_owner_edit():
            return True
    return False


def _resolve_skill_removals(api, base, default, previous_manifest, files, allow_removed_skills):
    """Classify a catalog upgrade against an installed managed bundle.

    Returns ``(acknowledged_ids, removal_targets, classification)`` where
    ``removal_targets`` maps acknowledged prior files to their installed
    digests, verified against the live default branch. Raises ValueError when
    the upgrade drops skills without an exact explicit acknowledgment, or
    when a dropped skill was locally edited (never deletable through this
    flow).
    """
    new_manifest = _parse_agent_skills_manifest(files[AGENT_SKILLS_MANIFEST][0])
    if new_manifest is None:
        raise ValueError("agent skill conflict: staged skills manifest is not managed")
    classification = classify_catalog_upgrade(previous_manifest, new_manifest)
    dropped = list(classification["removed_skills"])
    dropped.extend(pair["from"] for pair in classification["renamed_skills"])
    if not dropped:
        return frozenset(), {}, classification
    validate_removal_acknowledgment(dropped, allow_removed_skills, classification=classification)

    removal_targets = {}
    for prior_rel, prior_hash in previous_manifest.get("files", {}).items():
        if skill_id_from_path(prior_rel) not in set(dropped):
            continue
        prior_full = ".agents/skills/" + prior_rel
        current = _file(api, base, prior_full, default)
        if current is None:
            continue
        local_hash = hashlib.sha256(current[0]).hexdigest()
        if local_hash != prior_hash:
            raise ValueError(
                "agent skill conflict: existing file diverged from its manifest: "
                f"{prior_full} (installed sha256 {prior_hash}, local sha256 {local_hash}); "
                "a locally edited skill cannot be deleted through this flow")
        removal_targets[prior_full] = prior_hash
    return frozenset(dropped), removal_targets, classification


def stage_workflows(api, repository, files, action_pins=(), apply=False, authored_paths=(), hook_packages=None, allow_removed_skills=None):
    """Stage rendered selected workflow bytes through one guarded Setup Pull Request."""
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise ValueError("repository must be OWNER/REPO")
    skill_paths = {path for path in files if path.startswith(".agents/skills/")}
    reviewed_skills = {}
    if skill_paths:
        reviewed_skills, _ = _agent_skills_bundle()
        if any(path not in reviewed_skills or files[path] != (reviewed_skills[path], None)
               for path in skill_paths):
            raise ValueError("agent-skills files must exactly match the reviewed Matt Pocock bundle")
    hook_paths = {path for path in files if path.startswith(".agents/hooks/")}
    if hook_paths:
        _verify_staged_hooks(files, hook_paths, hook_packages=hook_packages)
    if not files or any(not (path in authored_paths or re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", path) or
                         path in {"scripts/" + name for name in ISSUE_STATE_GATE_SCRIPTS} or
                         path in {"scripts/" + name for name in DOCUMENTATION_SCRIPT_FILES} or
                         path == REVIEW_RUNTIME_PATH or
                         path == DOCUMENTATION_GUIDE or
                         path == DOCUMENTATION_CONFIG or
                         path == DOCUMENTATION_INVENTORY or
                         path == AGENT_SKILLS_MANIFEST or
                         path == AGENT_SKILLS_LICENSE or
                         path == ".github/dependabot.yml" or
                         re.fullmatch(r"\.agents/skills/(?:engineering|productivity)/[a-z0-9][a-z0-9-]*/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", path) or
                         re.fullmatch(r"\.agents/hooks/[a-z0-9][a-z0-9-]*/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", path)) or
                        not isinstance(content, bytes) or
                        (path.startswith((".agents/skills/", ".agents/hooks/")) and expected_sha is not None) or
                        (path not in authored_paths and not path.startswith((".agents/skills/", ".agents/hooks/")) and
                         path != ".github/dependabot.yml" and
                         not content.startswith((MARKER.encode(), LEGACY_MARKER.encode(), REVIEW_MARKER.encode(), RUNTIME_MARKER,
                                                  DOCUMENTATION_RUNTIME_MARKER, REVIEW_RUNTIME_MARKER,
                                                  DOCUMENTATION_GUIDE_MARKER.encode())) and
                         not (path == DOCUMENTATION_CONFIG and _managed_documentation_config(content)) and
                         not (path == DOCUMENTATION_INVENTORY and
                              hashlib.sha256(content).hexdigest() == DOCUMENTATION_INVENTORY_SHA256)) or
                        (expected_sha is not None and (not isinstance(expected_sha, str) or not SHA.fullmatch(expected_sha)))
                        for path, (content, expected_sha) in files.items()):
        raise ValueError("selected workflow files must be managed, pinned, and use workflow paths")
    _verify_action_commits(api, action_pins)
    base = "/repos/" + repository
    repo = get_object(api, base)
    default = repo.get("default_branch")
    if not isinstance(default, str) or not default:
        raise ApiError("default branch unavailable")
    head_sha = _ref(api, base, default)
    if head_sha is None:
        raise ApiError("default branch reference unavailable")
    previous_skill_manifest = None
    removal_targets = {}
    upgrade_classification = None
    if skill_paths:
        manifest_file = _file(api, base, AGENT_SKILLS_MANIFEST, default)
        if manifest_file is not None:
            previous_skill_manifest = _parse_agent_skills_manifest(manifest_file[0])
            if previous_skill_manifest is None:
                raise ValueError("agent skill conflict: existing skills manifest is not managed")
            acknowledged_removals, removal_targets, upgrade_classification = _resolve_skill_removals(
                api, base, default, previous_skill_manifest, files, allow_removed_skills)
            for prior_rel, prior_hash in previous_skill_manifest.get("files", {}).items():
                prior_full = ".agents/skills/" + prior_rel
                if prior_full not in files:
                    if skill_id_from_path(prior_rel) in acknowledged_removals:
                        continue
                    prior_cur = _file(api, base, prior_full, default)
                    if prior_cur is None:
                        raise ValueError(
                            "agent skill conflict: existing file diverged from its manifest: "
                            f"{prior_full} (installed sha256 {prior_hash}, local sha256 absent)")
                    local_hash = hashlib.sha256(prior_cur[0]).hexdigest()
                    if local_hash != prior_hash:
                        raise ValueError(
                            "agent skill conflict: existing file diverged from its manifest: "
                            f"{prior_full} (installed sha256 {prior_hash}, local sha256 {local_hash})")
        else:
            upgrade_classification = None
            removal_targets = {}
    previous_hook_manifests = {}
    if hook_paths:
        hook_names = {path.split("/")[2] for path in hook_paths}
        for hname in hook_names:
            for mname in MANIFEST_FILENAMES:
                mpath = f".agents/hooks/{hname}/{mname}"
                mf = _file(api, base, mpath, default)
                if mf is not None:
                    try:
                        mdata = json.loads(mf[0].decode("utf-8"))
                        previous_hook_manifests[hname] = validate_hook_manifest(mdata)
                    except Exception:
                        raise ValueError(f"hook conflict: existing hook manifest is not managed: {mpath}")
                    break
    changes = []
    default_files = {}
    for path, (content, expected_sha) in files.items():
        current = _file(api, base, path, default)
        default_files[path] = current
        previous_skill_owned = False
        if path in skill_paths and current is not None:
            if path == AGENT_SKILLS_MANIFEST:
                previous_skill_owned = _parse_agent_skills_manifest(current[0]) is not None
                if not previous_skill_owned:
                    raise ValueError("agent skill conflict: existing skills manifest is not managed")
            elif current[0] != content:
                relative = path.removeprefix(".agents/skills/")
                prior_hash = previous_skill_manifest["files"].get(relative) if previous_skill_manifest else None
                if prior_hash != hashlib.sha256(current[0]).hexdigest():
                    raise ValueError(f"agent skill conflict: existing file diverged from its manifest: {path}")
                previous_skill_owned = True
        elif path in skill_paths and current is None and previous_skill_manifest is not None:
            relative = path.removeprefix(".agents/skills/")
            if relative in previous_skill_manifest.get("files", {}):
                raise ValueError(f"agent skill conflict: existing file diverged from its manifest: {path}")
        previous_hook_owned = False
        if path in hook_paths and current is not None:
            hook_name = path.split("/")[2]
            prev_m = previous_hook_manifests.get(hook_name)
            is_manifest_file = path.endswith(("/manifest.json", "/hook.json"))
            if is_manifest_file:
                if prev_m is None:
                    raise ValueError(f"hook conflict: existing hook manifest is not managed: {path}")
                previous_hook_owned = True
            elif current[0] != content:
                if prev_m is None:
                    raise ValueError(f"hook conflict: unmanaged file exists in target hook directory: {path}")
                rel_file = path.removeprefix(f".agents/hooks/{hook_name}/")
                prior_hash = prev_m.files.get(rel_file)
                if prior_hash != hashlib.sha256(current[0]).hexdigest():
                    raise ValueError(f"hook conflict: existing file diverged from its manifest: {path}")
                previous_hook_owned = True
            else:
                previous_hook_owned = True
        if current is not None:
            if path in authored_paths:
                managed = expected_sha is not None and current[1] == expected_sha
            elif path.startswith("scripts/"):
                managed = current[0].startswith((RUNTIME_MARKER, DOCUMENTATION_RUNTIME_MARKER,
                                                 REVIEW_RUNTIME_MARKER))
            elif path == DOCUMENTATION_GUIDE:
                managed = current[0].startswith(DOCUMENTATION_GUIDE_MARKER.encode())
            elif path == DOCUMENTATION_CONFIG:
                managed = _managed_documentation_config(current[0])
            elif path == DOCUMENTATION_INVENTORY:
                managed = hashlib.sha256(current[0]).hexdigest() == DOCUMENTATION_INVENTORY_SHA256
            elif path.startswith(".agents/skills/"):
                managed = previous_skill_owned
            elif path.startswith(".agents/hooks/"):
                managed = previous_hook_owned
            elif path == ".github/workflows/open-code-review.yml":
                managed = current[0].startswith((
                    REVIEW_MARKER.encode(),
                    (MARKER + "open-code-review version=1").encode(),
                    (LEGACY_MARKER + "open-code-review version=1").encode(),
                ))
            else:
                managed = current[0].startswith((MARKER.encode(), LEGACY_MARKER.encode()))
            if current[0] == content:
                continue
            if not managed:
                if path in authored_paths:
                    raise ValueError(f"authored file conflict: {path}; supply current expected_sha")
                raise ValueError(f"unmanaged workflow or runtime exists: {path}")
            if not path.startswith((".agents/skills/", ".agents/hooks/")) and current[1] != expected_sha:
                raise ValueError(f"managed workflow drift: {path}; supply current expected_sha")
        elif expected_sha is not None:
            raise ValueError(f"managed workflow drift: {path}; expected file is absent")
        reason = ("caller selected authored file" if path in authored_paths
                  else "caller selected opt-in hook package" if path in hook_paths
                  else "caller selected versioned workflow template")
        changes.append({"path": path, "scope": repository,
                        "reason": reason,
                        "operation": "create" if current is None else "update",
                        "current_sha": current[1] if current else None,
                        "desired_sha256": hashlib.sha256(content).hexdigest()})
    for removal_path in sorted(removal_targets):
        current = _file(api, base, removal_path, default)
        if current is None:
            continue
        changes.append({"path": removal_path, "scope": repository,
                        "reason": "caller acknowledged upstream skill removal",
                        "operation": "remove",
                        "current_sha": current[1],
                        "desired_sha256": None})
    result = {"repository": repository, "default_branch": default, "changes": changes,
              "status": "planned" if not apply else "verified"}
    if upgrade_classification is not None and (changes or upgrade_classification["revision_changed"]):
        new_manifest = _parse_agent_skills_manifest(files[AGENT_SKILLS_MANIFEST][0])
        result["agent_skill_upgrade"] = {
            "from": {
                "source_commit": previous_skill_manifest.get("source_commit"),
                "source_version": previous_skill_manifest.get("source_version"),
            },
            "to": {
                "source_commit": new_manifest.get("source_commit"),
                "source_version": new_manifest.get("source_version"),
            },
            "removed_skills": list(upgrade_classification["removed_skills"]),
            "renamed_skills": list(upgrade_classification["renamed_skills"]),
        }
    if not changes:
        return result
    digest = hashlib.sha256(json.dumps({path: hashlib.sha256(data).hexdigest()
                                        for path, (data, _) in files.items()}, sort_keys=True).encode()).hexdigest()[:12]
    candidate_branch = SETUP_PREFIX + digest

    open_pulls = _open_setup_pulls(api, base, default)
    active_pull = None
    if open_pulls:
        for existing in open_pulls:
            existing_branch = existing["head"]["ref"]
            if existing_branch == LEGACY_REVIEW_BRANCH or not existing_branch.startswith(SETUP_PREFIX):
                raise ValueError(f"open Setup Pull Request conflicts: {existing['html_url']}; "
                                 f"resolve it before staging workflows")
        if len(open_pulls) > 1:
            raise ValueError("multiple open Setup Pull Requests detected; resolve duplicates before staging workflows")
        existing = open_pulls[0]
        existing_branch = existing["head"]["ref"]
        if existing.get("draft") is not True:
            raise ValueError(f"Setup Pull Request must remain draft: {existing['html_url']}")
        if _check_branch_owner_edits(api, base, default, existing_branch):
            raise ValueError(f"workflow branch drift: owner edits detected on Setup Pull Request branch {existing_branch} "
                             f"({existing['html_url']}); guarding against destructive overwrites")
        branch = existing_branch
        active_pull = existing
    else:
        branch = candidate_branch

    result["branch"] = branch
    branch_sha = _ref(api, base, branch)
    owner = repository.split("/", 1)[0]
    pull = active_pull or (_pull(api, base, owner, branch, default) if branch_sha else None)
    if pull and pull.get("draft") is not True:
        raise ValueError(f"Setup Pull Request must remain draft: {pull.get('html_url')}")
    if open_pulls and not pull:
        raise ApiError(f"cannot verify existing Setup Pull Request: {open_pulls[0]['html_url']}")
    if branch_sha:
        _check_branch_diff(api, base, default, branch, set(files) | set(removal_targets))
        all_staged = True
        for change in changes:
            path = change["path"]
            if change.get("operation") == "remove":
                if _file(api, base, path, branch) is not None:
                    all_staged = False
                continue
            staged = _file(api, base, path, branch)
            curr_default = default_files[path][0] if default_files[path] else b""
            expected_sha = files[path][1] if isinstance(files[path], (list, tuple)) and len(files[path]) > 1 else None
            if staged is not None and staged[0] not in (files[path][0], curr_default):
                if expected_sha and staged[1] == expected_sha:
                    pass
                elif not _is_managed_content(path, staged[0], authored_paths):
                    pr_url = pull.get("html_url", "unknown") if pull else "unknown"
                    raise ValueError(f"workflow branch drift: owner edits detected on Setup Pull Request branch {branch} "
                                     f"({pr_url}): {path}; guarding against destructive overwrites")
            if staged is None or staged[0] != files[path][0]:
                all_staged = False
        if all_staged and pull:
            result["changes"] = []
            result["pull_request"] = pull.get("html_url")
            result["status"] = "pending_review"
            return result
    if not apply:
        if pull:
            result["pull_request"] = pull.get("html_url")
            result["status"] = "pending_review"
        return result
    if _ref(api, base, default) != head_sha:
        raise ApiError("default branch advanced during workflow preflight; rerun Dry-Run")
    if branch_sha is not None and _ref(api, base, branch) != branch_sha:
        raise ApiError(f"Setup branch {branch} changed during workflow preflight; inspect it before retrying")
    writes_started = False
    attempted_path = None
    try:
        if branch_sha is None:
            writes_started = True
            status, _ = api.request("POST", base + "/git/refs", {"ref": "refs/heads/" + branch, "sha": head_sha})
            if status != 201:
                raise ApiError("failed to create workflow branch")
        for change in changes:
            path = change["path"]
            if change.get("operation") == "remove":
                staged = _file(api, base, path, branch)
                if staged is None:
                    continue
                writes_started = True
                attempted_path = path
                status, _ = api.request(
                    "DELETE", base + "/contents/" + quote(path, safe="/"),
                    {"message": "Remove selected agent skill file",
                     "branch": branch, "sha": staged[1]})
                if status not in (200, 201):
                    raise ApiError(f"failed to remove staged skill: {path}")
                attempted_path = None
                continue
            content = files[path][0]
            staged = _file(api, base, path, branch)
            if staged is not None and staged[0] == content:
                continue
            msg_prefix = "Update" if staged is not None else "Install"
            msg_type = "repository asset" if path in authored_paths else "workflow template"
            payload = {"message": f"{msg_prefix} selected {msg_type}", "content": base64.b64encode(content).decode(),
                       "branch": branch}
            if staged is not None:
                payload["sha"] = staged[1]
            writes_started = True
            attempted_path = path
            status, _ = api.request("PUT", base + "/contents/" + quote(path, safe="/"), payload)
            if status not in (200, 201):
                raise ApiError(f"failed to stage workflow: {path}")
            attempted_path = None
        for change in changes:
            if change.get("operation") == "remove":
                if _file(api, base, change["path"], branch) is not None:
                    raise ApiError(f"staged skill removal could not be verified: {change['path']}")
                continue
            staged = _file(api, base, change["path"], branch)
            if staged is None or staged[0] != files[change["path"]][0]:
                raise ApiError(f"staged workflow could not be verified: {change['path']}")
        _check_branch_diff(api, base, default, branch, set(files) | set(removal_targets))
        if _ref(api, base, default) != head_sha:
            raise ApiError("default branch advanced during workflow staging; rerun Dry-Run")
        pr_body = "Review the selected, versioned workflow templates and caller-supplied commands."
        if removal_targets:
            removed_ids = sorted({skill_id_from_path(path) for path in removal_targets})
            pr_body += ("\n\nThis agent skill catalog upgrade stops shipping the following upstream skills,"
                        " deleted by this pull request through an explicitly acknowledged selection: "
                        + ", ".join(removed_ids) + ".")
        if upgrade_classification is not None and upgrade_classification["renamed_skills"]:
            pr_body += ("\n\nUpstream skill renames: " + ", ".join(
                f"{pair['from']} -> {pair['to']}"
                for pair in upgrade_classification["renamed_skills"]) + ".")
        if pull is None:
            status, pull = api.request("POST", base + "/pulls", {
                "title": "Install selected GitHub workflows", "head": branch, "base": default,
                "body": pr_body, "draft": True})
            if status != 201 or not isinstance(pull, dict):
                raise ApiError("failed to create workflow pull request")
        pull = _pull(api, base, owner, branch, default)
        if (not isinstance(pull, dict) or not isinstance(pull.get("html_url"), str) or
                pull.get("draft") is not True or not isinstance(pull.get("head"), dict) or
                pull["head"].get("ref") != branch or not isinstance(pull.get("base"), dict) or
                pull["base"].get("ref") != default):
            raise ApiError("draft workflow pull request could not be verified")
    except (ApiError, ValueError) as error:
        if writes_started:
            file_statuses = {}
            for change in changes:
                path = change["path"]
                try:
                    staged = _file(api, base, path, branch)
                except (ApiError, ValueError):
                    file_statuses[path] = "unverified"
                    continue
                if change.get("operation") == "remove":
                    if staged is None:
                        file_statuses[path] = "applied"
                    elif path == attempted_path:
                        file_statuses[path] = "failed"
                    else:
                        file_statuses[path] = "pending"
                    continue
                if staged is not None and staged[0] == files[path][0]:
                    file_statuses[path] = "applied"
                elif path == attempted_path:
                    file_statuses[path] = "failed"
                else:
                    file_statuses[path] = "pending"
            raise StagingFailure(
                f"Setup may be partial on branch {branch}: {error}. Inspect branch and diff before retrying; do not delete it automatically",
                branch, file_statuses) from None
        raise
    result["pull_request"] = pull.get("html_url")
    result["status"] = "pending_review"
    return result


def _verify_dependency_review_capability(api, repository):
    status, repo = api.request("GET", f"/repos/{repository}")
    if status == 200 and isinstance(repo, dict):
        if repo.get("visibility") == "private":
            try:
                from scripts.repository_capabilities import detect_repository_plan
            except ImportError:
                from repository_capabilities import detect_repository_plan
            plan_name, _ = detect_repository_plan(api, repo)
            if plan_name not in ("business", "enterprise"):
                raise ApiError("Dependency review on private repositories requires GitHub Advanced Security")


def install(api, repository, policy, apply=False):
    """Render generic workflows and stage them through the shared Setup PR boundary."""
    files = validate_policy(policy)
    pins = []
    for workflow in policy["workflows"]:
        if workflow["template"] == "issue-state-gate":
            continue
        pins.append(("actions/checkout", workflow["checkout_sha"].lower()))
        if workflow["template"] == "dependency-review":
            _verify_dependency_review_capability(api, repository)
            pins.append(("actions/dependency-review-action", workflow["action_sha"].lower()))
        elif workflow["template"] in ("container-publish", "container"):
            login_sha = workflow.get("login_action_sha", DEFAULT_CONTAINER_LOGIN_SHA).lower()
            build_sha = workflow.get("build_action_sha", workflow.get("action_sha", DEFAULT_CONTAINER_BUILD_SHA)).lower()
            pins.append(("docker/login-action", login_sha))
            pins.append(("docker/build-push-action", build_sha))
        elif workflow["template"] == "profile-ci":
            profile_name = workflow.get("profile", "")
            if profile_name in ("python", "scientific-python"):
                setup_mode = workflow.get("python_setup_mode")
                if not setup_mode and isinstance(workflow.get("python"), dict):
                    setup_mode = workflow["python"].get("python_setup_mode")
                if not setup_mode and isinstance(workflow.get("profile_options"), dict):
                    setup_mode = workflow["profile_options"].get("python_setup_mode")
                if setup_mode != "system":
                    pins.append(("actions/setup-python", workflow.get("setup_python_sha", DEFAULT_SETUP_PYTHON_SHA).lower()))
                    pkg_mgr = workflow.get("package_manager")
                    if not pkg_mgr and isinstance(workflow.get("python"), dict):
                        pkg_mgr = workflow["python"].get("package_manager")
                    if not pkg_mgr and isinstance(workflow.get("profile_options"), dict):
                        pkg_mgr = workflow["profile_options"].get("package_manager")
                    if pkg_mgr == "uv" or workflow.get("package_manager") == "uv" or setup_mode == "uv":
                        pins.append(("astral-sh/setup-uv", workflow.get("setup_uv_sha", DEFAULT_SETUP_UV_SHA).lower()))
            elif profile_name in ("cmake-mpi", "cmake"):
                opts = workflow.get("cmake_mpi") or workflow.get("cmake-mpi") or workflow.get("profile_options") or {}
                cache_enabled = bool(opts.get("cache") if isinstance(opts, dict) and opts.get("cache") is not None else workflow.get("cache", False))
                ccache_enabled = bool(opts.get("ccache", True)) if isinstance(opts, dict) else bool(workflow.get("ccache", True))
                if cache_enabled and ccache_enabled:
                    cache_sha = (opts.get("cache_sha") if isinstance(opts, dict) else None) or workflow.get("cache_sha") or DEFAULT_CACHE_SHA
                    pins.append(("actions/cache", cache_sha.lower()))
    return stage_workflows(api, repository, files, pins, apply=apply)


def validate_allow_removed_skills(allowed_removals):
    """Validate the agent-skills removal acknowledgment.

    Returns the validated list, or ``None`` when no acknowledgment was given.
    Raises ValueError when the value is not a nonempty list of unique,
    well-formed skill ids (matched against AGENT_SKILL_ID).
    """
    if allowed_removals is None:
        return None
    if (not isinstance(allowed_removals, list) or not allowed_removals
            or any(not isinstance(skill, str) or not AGENT_SKILL_ID.fullmatch(skill)
                   for skill in allowed_removals)
            or len(set(allowed_removals)) != len(allowed_removals)):
        raise ValueError(
            "allow_removed_skills must be a nonempty list of unique skill ids "
            "like engineering/implement-spec")
    return allowed_removals


def install_selected(api, repository, workflow_stage=None, review_stage=None, issue_state_gate_stage=None,
                     agent_skills_stage=None, documentation_stage=None, apply=False, files_stage=None,
                     ci_profile_stage=None, dependabot_stage=None, hooks_stage=None):
    """Stage all selected workflow bytes in one Setup Pull Request."""
    files = {}
    pins = []
    authored_paths = set()
    if workflow_stage is not None:
        files.update(validate_policy({"version": 1, "workflows": workflow_stage["workflows"]}))
        for workflow in workflow_stage["workflows"]:
            pins.append(("actions/checkout", workflow["checkout_sha"].lower()))
            if workflow["template"] == "dependency-review":
                _verify_dependency_review_capability(api, repository)
                pins.append(("actions/dependency-review-action", workflow["action_sha"].lower()))
            elif workflow["template"] in ("container-publish", "container"):
                login_sha = workflow.get("login_action_sha", DEFAULT_CONTAINER_LOGIN_SHA).lower()
                build_sha = workflow.get("build_action_sha", workflow.get("action_sha", DEFAULT_CONTAINER_BUILD_SHA)).lower()
                pins.append(("docker/login-action", login_sha))
                pins.append(("docker/build-push-action", build_sha))
            elif workflow["template"] == "profile-ci":
                profile_name = workflow.get("profile", "")
                if profile_name in ("python", "scientific-python"):
                    setup_mode = workflow.get("python_setup_mode")
                    if not setup_mode and isinstance(workflow.get("python"), dict):
                        setup_mode = workflow["python"].get("python_setup_mode")
                    if not setup_mode and isinstance(workflow.get("profile_options"), dict):
                        setup_mode = workflow["profile_options"].get("python_setup_mode")
                    if setup_mode != "system":
                        pins.append(("actions/setup-python", workflow.get("setup_python_sha", DEFAULT_SETUP_PYTHON_SHA).lower()))
                        pkg_mgr = workflow.get("package_manager")
                        if not pkg_mgr and isinstance(workflow.get("python"), dict):
                            pkg_mgr = workflow["python"].get("package_manager")
                        if not pkg_mgr and isinstance(workflow.get("profile_options"), dict):
                            pkg_mgr = workflow["profile_options"].get("package_manager")
                        if pkg_mgr == "uv" or workflow.get("package_manager") == "uv" or setup_mode == "uv":
                            pins.append(("astral-sh/setup-uv", workflow.get("setup_uv_sha", DEFAULT_SETUP_UV_SHA).lower()))
                elif profile_name in ("cmake-mpi", "cmake"):
                    opts = workflow.get("cmake_mpi") or workflow.get("cmake-mpi") or workflow.get("profile_options") or {}
                    cache_enabled = bool(opts.get("cache") if isinstance(opts, dict) and opts.get("cache") is not None else workflow.get("cache", False))
                    ccache_enabled = bool(opts.get("ccache", True)) if isinstance(opts, dict) else bool(workflow.get("ccache", True))
                    if cache_enabled and ccache_enabled:
                        cache_sha = (opts.get("cache_sha") if isinstance(opts, dict) else None) or workflow.get("cache_sha") or DEFAULT_CACHE_SHA
                        pins.append(("actions/cache", cache_sha.lower()))
    if ci_profile_stage is not None:
        raise ValueError("unsupported ci_profile_stage: use workflows stage with template 'profile-ci' instead")
    if review_stage is not None:
        try:
            from scripts.review_policy import validate_policy as validate_review
            from scripts.review_workflow import WORKFLOW_PATH, render_workflow
            from scripts.review_readiness import validate_trusted_rule
        except ImportError:
            from review_policy import validate_policy as validate_review
            from review_workflow import WORKFLOW_PATH, render_workflow
            from review_readiness import validate_trusted_rule
        review = validate_review(review_stage)
        validate_trusted_rule(api, repository, review)
        if WORKFLOW_PATH in files:
            raise ValueError("review workflow path conflicts with another selected workflow")
        files[WORKFLOW_PATH] = (render_workflow(review), review.workflow.expected_sha)
        if REVIEW_RUNTIME_PATH in files:
            raise ValueError("review reporter path conflicts with another selected runtime")
        files[REVIEW_RUNTIME_PATH] = (render_review_reporter(), review.workflow.reporter_expected_sha)
        pins.append(("alibaba/open-code-review", review.workflow.action_sha))
    if issue_state_gate_stage is not None:
        trusted = {"template": "issue-state-gate", "template_version": 1,
                   "filename": "issue-state-gate.yml"}
        issue_files = validate_policy({"version": 1, "workflows": [trusted]})
        if set(files) & set(issue_files):
            raise ValueError("Issue State Gate suite path conflicts with another selected workflow")
        files.update(issue_files)
    if agent_skills_stage is not None:
        if not isinstance(agent_skills_stage, dict) or agent_skills_stage.get("name") != "agent-skills" \
                or agent_skills_stage.get("bundle") != "mattpocock" \
                or not set(agent_skills_stage) <= {"name", "bundle", "allow_removed_skills", "expected_sha"}:
            raise ValueError("agent-skills bundle must be mattpocock")
        allowed_removals = validate_allow_removed_skills(
            agent_skills_stage.get("allow_removed_skills"))
        skill_files = {
            path: (content, None) for path, content in _agent_skills_bundle()[0].items()
        }
        if set(files) & set(skill_files):
            raise ValueError("Matt Pocock agent skill paths conflict with another selected stage")
        files.update(skill_files)
    if documentation_stage is not None or agent_skills_stage is not None:
        if documentation_stage is not None and documentation_stage != {"name": "documentation-freshness"}:
            raise ValueError("documentation freshness stage has unsupported settings")
        exp_sha = agent_skills_stage.get("expected_sha") if isinstance(agent_skills_stage, dict) else None
        documentation_files = render_documentation_suite(expected_sha=exp_sha)
        if set(files) & set(documentation_files):
            raise ValueError("documentation freshness paths conflict with another selected stage")
        files.update(documentation_files)
    if files_stage is not None:
        from setup_repository_content import selected_file_bytes
        authored = selected_file_bytes(files_stage["items"])
        if set(files) & set(authored):
            raise ValueError("authored file path conflicts with another selected stage")
        authored_paths = set(authored)
        files.update(authored)
    if dependabot_stage is not None:
        try:
            from scripts.dependabot_generator import render_dependabot_config, validate_dependabot_stage
        except ImportError:
            from dependabot_generator import render_dependabot_config, validate_dependabot_stage
        dep_config = validate_dependabot_stage(dependabot_stage)
        dep_content = render_dependabot_config(dep_config)
        dep_path = ".github/dependabot.yml"
        if dep_path in files:
            raise ValueError("Dependabot configuration path conflicts with another selected stage")
        files[dep_path] = (dep_content.encode("utf-8"), None)
    if hooks_stage is not None:
        packages = hooks_stage.get("packages", [])
        opt_in = hooks_stage.get("opt_in", False)
        hook_files_dict = render_hook_packages(packages, opt_in=opt_in)
        hook_files = {path: (content, None) for path, content in hook_files_dict.items()}
        if set(files) & set(hook_files):
            raise ValueError("hook paths conflict with another selected stage")
        files.update(hook_files)
    if not files:
        base = "/repos/" + repository
        repo = get_object(api, base)
        default = repo.get("default_branch") if isinstance(repo, dict) else "main"
        return {"repository": repository, "default_branch": default, "changes": [], "status": "verified" if apply else "planned"}
    hook_pkgs = hooks_stage.get("packages") if (hooks_stage and hooks_stage.get("opt_in", False)) else None
    skill_removals = (agent_skills_stage.get("allow_removed_skills")
                      if isinstance(agent_skills_stage, dict) else None)
    return stage_workflows(api, repository, files, pins, apply=apply, authored_paths=authored_paths,
                           hook_packages=hook_pkgs, allow_removed_skills=skill_removals)


PYTHON_INDICATOR_FILES = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    ".python-version",
    "environment.yml",
)


def detect_python_project(api=None, repository=None, root_dir=None, inventory=None):
    """Detect whether the target repository is a Python project from metadata, API, or local files."""
    if inventory and isinstance(inventory, dict):
        if inventory.get("repository", {}).get("language") == "Python":
            return True
        langs = inventory.get("languages")
        if isinstance(langs, dict) and langs.get("Python", 0) > 0:
            return True

    if api is not None and repository:
        base = f"/repos/{repository}"
        try:
            res = api.request("GET", f"{base}/languages")
            if isinstance(res, tuple) and len(res) == 2:
                status, langs = res
                if status == 200 and isinstance(langs, dict) and langs.get("Python", 0) > 0:
                    return True
        except Exception:
            pass
        try:
            res = api.request("GET", base)
            if isinstance(res, tuple) and len(res) == 2:
                status, repo_info = res
                if status == 200 and isinstance(repo_info, dict) and repo_info.get("language") == "Python":
                    return True
        except Exception:
            pass

    if root_dir is not None:
        root = Path(root_dir)
        for fname in PYTHON_INDICATOR_FILES:
            if (root / fname).is_file():
                return True
        # Check for python files in common code paths
        for sub in ("", "src", "tests", "scripts"):
            subpath = root / sub if sub else root
            if subpath.is_dir():
                try:
                    if any(subpath.glob("*.py")):
                        return True
                except OSError:
                    pass
        return False

    return False


def detect_python_package_manager(root_dir=None):
    """Detect default or existing package manager (pip or uv) from project files."""
    root = Path(root_dir) if root_dir is not None else Path.cwd()
    if (root / "uv.lock").is_file():
        return "uv"
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            content = pyproject.read_text(encoding="utf-8", errors="ignore")
            if "[tool.uv]" in content:
                return "uv"
        except OSError:
            pass
    return "pip"


def detect_git_lfs(root_dir=None, api=None, repository=None):
    """Detect if repository uses Git LFS from .gitattributes or git configuration."""
    if root_dir is not None:
        root = Path(root_dir)
        gitattrs = root / ".gitattributes"
        if gitattrs.is_file():
            try:
                content = gitattrs.read_text(encoding="utf-8", errors="ignore")
                if "filter=lfs" in content:
                    return True
            except OSError:
                pass
    return False


def detect_python_versions(root_dir=None):
    """Detect configured or recommended Python versions for CI matrix."""
    if root_dir is not None:
        py_ver_file = Path(root_dir) / ".python-version"
        if py_ver_file.is_file():
            try:
                ver_line = py_ver_file.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
                if ver_line:
                    v = ver_line[0].strip()
                    m = re.match(r"^(\d+\.\d+)", v)
                    if m:
                        matched = m.group(1)
                        return [matched]
            except OSError:
                pass
    return list(DEFAULT_PYTHON_VERSIONS)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="OWNER/REPO")
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        policy = json.loads(args.policy.read_text(encoding="utf-8"))
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise ValueError("GITHUB_TOKEN is required")
        result = install(GitHubApi(token), args.repo, policy, args.apply)
        print(json.dumps(result, indent=2, sort_keys=True))
    except (ApiError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
