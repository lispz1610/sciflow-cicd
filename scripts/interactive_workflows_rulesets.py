"""Interactive questionnaire stage for workflows and branch rulesets (merge gates)."""

from __future__ import annotations

import os
from pathlib import Path
import re
import sys
from typing import Optional, TextIO, Dict, Any, List, TYPE_CHECKING

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

if TYPE_CHECKING:
    try:
        from scripts.interactive import Session
    except ImportError:
        from interactive import Session

try:
    from scripts.prompts import prompt_confirm, prompt_multichoice, prompt_text, PromptExit
    from scripts.install_github_workflows import (
        DEFAULT_CONTAINER_BUILD_SHA,
        DEFAULT_CONTAINER_LOGIN_SHA,
        DEFAULT_CHECKOUT_SHA,
        DEFAULT_DEPENDENCY_SHA,
        DEFAULT_SETUP_PYTHON_SHA,
        DEFAULT_SETUP_UV_SHA,
        detect_python_project,
        detect_python_package_manager,
        detect_python_versions,
        detect_git_lfs,
        detect_cmake_mpi_project,
        extract_profile_status_check,
        _is_self_hosted,
        MARKER,
        LEGACY_MARKER,
        _file,
    )
except ImportError:
    from prompts import prompt_confirm, prompt_multichoice, prompt_text, PromptExit
    from install_github_workflows import (
        DEFAULT_CONTAINER_BUILD_SHA,
        DEFAULT_CONTAINER_LOGIN_SHA,
        DEFAULT_CHECKOUT_SHA,
        DEFAULT_DEPENDENCY_SHA,
        DEFAULT_SETUP_PYTHON_SHA,
        DEFAULT_SETUP_UV_SHA,
        detect_python_project,
        detect_python_package_manager,
        detect_python_versions,
        detect_git_lfs,
        detect_cmake_mpi_project,
        extract_profile_status_check,
        _is_self_hosted,
        MARKER,
        LEGACY_MARKER,
        _file,
    )

try:
    from scripts.workflow_defaults import (
        DEFAULT_CI_COMMANDS,
        DEFAULT_PYTHON_VERSIONS,
        DEFAULT_CMAKE_BUILD_COMMANDS,
        DEFAULT_CMAKE_TEST_COMMANDS,
        DEFAULT_SCHEDULE,
        DEFAULT_CONTAINER_SCHEDULE,
        DEFAULT_STATUS_CHECK_NAME,
        default_schedule_string,
        normalize_cron_schedule,
        resolve_check_spec,
    )
except ImportError:
    from workflow_defaults import (
        DEFAULT_CI_COMMANDS,
        DEFAULT_PYTHON_VERSIONS,
        DEFAULT_CMAKE_BUILD_COMMANDS,
        DEFAULT_CMAKE_TEST_COMMANDS,
        DEFAULT_SCHEDULE,
        DEFAULT_CONTAINER_SCHEDULE,
        DEFAULT_STATUS_CHECK_NAME,
        default_schedule_string,
        normalize_cron_schedule,
        resolve_check_spec,
    )

WORKFLOW_PATH_PATTERN = re.compile(r"^\.github/workflows/[^/]+\.ya?ml\Z")


def managed_workflow_expected_sha(api, repository: str, filename: str,
                                  default_branch: str) -> Optional[str]:
    """Return the blob SHA when a workflow is managed by current or legacy markers.

    All workflow prompt stages share this single lookup so managed updates
    preserve author SHA preconditions regardless of which marker generation
    created the remote file. Returns ``None`` on missing/unmanaged files or
    API errors, matching the historical best-effort behavior.
    """
    if not api or not repository or not filename:
        return None
    path = filename if filename.startswith(".github/workflows/") else f".github/workflows/{filename}"
    try:
        current = _file(api, f"/repos/{repository}", path, default_branch)
    except Exception:
        return None
    if not current or not current[0]:
        return None
    content = current[0]
    if isinstance(content, bytes):
        managed = content.startswith((MARKER.encode(), LEGACY_MARKER.encode()))
    else:
        managed = str(content).startswith((MARKER, LEGACY_MARKER))
    return current[1] if managed else None


def validate_pr_number(value: Any) -> Optional[int]:
    """Validate that value represents a positive integer PR number."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped.isdigit():
            return None
        num = int(stripped)
        return num if num > 0 else None
    return None


def validate_reviewer(value: Any) -> Optional[str]:
    """Validate that reviewer is a non-empty string username."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None


def parse_check_spec(spec: str) -> Optional[Dict[str, str]]:
    """Parse check specification string into a dict of {check_name: workflow_path}.

    Supported formats:
        - "ci-check" -> {"ci-check": ".github/workflows/ci.yml"}
        - ".github/workflows/ci.yml=ci-check" -> {"ci-check": ".github/workflows/ci.yml"}
        - "ci-check=.github/workflows/ci.yml" -> {"ci-check": ".github/workflows/ci.yml"}
        - "ci.yml=ci-check" -> {"ci-check": ".github/workflows/ci.yml"}
        - Comma-separated combinations of above.
    """
    if not isinstance(spec, str):
        return None
    stripped = spec.strip()
    if not stripped:
        return None

    tokens = [t.strip() for t in stripped.split(",") if t.strip()]
    if not tokens:
        return None

    checks: Dict[str, str] = {}
    for token in tokens:
        if "=" in token:
            p1, p2 = [part.strip() for part in token.split("=", 1)]
            if not p1 or not p2:
                return None
            if p1.endswith((".yml", ".yaml")) or p1.startswith(".github/"):
                path, name = p1, p2
            elif p2.endswith((".yml", ".yaml")) or p2.startswith(".github/"):
                name, path = p1, p2
            else:
                path, name = p1, p2
        else:
            name = token
            if "dependency-review" in name.lower():
                path = ".github/workflows/dependency-review.yml"
            else:
                path = ".github/workflows/ci.yml"

        if not path.startswith(".github/workflows/"):
            path = f".github/workflows/{path}"

        if not name or not WORKFLOW_PATH_PATTERN.fullmatch(path):
            return None

        checks[name] = path

    return checks if checks else None


def _split_commands(raw: str) -> List[str]:
    """Split a command string on newlines and semicolons, stripping surrounding whitespace."""
    commands: List[str] = []
    for line in raw.splitlines():
        for part in line.split(";"):
            stripped = part.strip()
            if stripped:
                commands.append(stripped)
    return commands


def _resolve_root_dir(root_dir: Optional[str], session: Optional[Session]) -> str:
    """Resolve the repository root directory from explicit argument, session, or cwd."""
    return str(
        root_dir
        or getattr(session, "repo_dir", None)
        or getattr(session, "root_dir", None)
        or os.getcwd()
    )


def prompt_python_ci_options(
    session: Optional[Session] = None,
    in_stream: Optional[TextIO] = None,
    out_stream: Optional[TextIO] = None,
    raise_on_interrupt: bool = False,
    defaults: Optional[Dict[str, Any]] = None,
    root_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Collect Python version matrix, package manager caching, test commands, and Git LFS settings."""
    in_s = in_stream if in_stream is not None else (getattr(session, "in_stream", None) or sys.stdin)
    out_s = out_stream if out_stream is not None else (getattr(session, "out_stream", None) or sys.stdout)
    defaults = dict(defaults or {})
    if session is not None and getattr(session, "resume_mode", False):
        session_defaults = getattr(session, "defaults", {}) or {}
        defaults = {**session_defaults.get("python", {}), **defaults}

    root_dir = _resolve_root_dir(root_dir, session)

    # 1. Python versions
    default_versions = defaults.get("versions") or detect_python_versions(root_dir=root_dir)
    default_versions_str = ", ".join(default_versions) if isinstance(default_versions, list) else str(default_versions)
    while True:
        raw_versions = prompt_text(
            "Python versions for test matrix (comma-separated, e.g. '3.10, 3.11, 3.12')",
            default=default_versions_str,
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_versions is None:
            raise PromptExit("Cancelled during python versions prompt")
        vers = [v.strip() for v in raw_versions.split(",") if v.strip()]
        if vers and all(re.fullmatch(r"\d+\.\d+", v) for v in vers) and len(vers) == len(set(vers)):
            python_versions = vers
            break
        out_s.write("Invalid Python versions. Expected format like '3.10, 3.11, 3.12' with unique versions.\n")
        out_s.flush()

    # 2. Python setup mode
    default_setup_mode = defaults.get("python_setup_mode", "setup-python")
    while True:
        raw_mode = prompt_text(
            "Python setup mode ('setup-python', 'uv', or 'system')",
            default=default_setup_mode,
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_mode is None:
            raise PromptExit("Cancelled during python setup mode prompt")
        smode = raw_mode.strip().lower()
        if smode in ("setup-python", "uv", "system"):
            python_setup_mode = smode
            break
        out_s.write("Invalid setup mode. Please choose 'setup-python', 'uv', or 'system'.\n")
        out_s.flush()

    # 3. Package manager
    default_pkg_mgr = defaults.get("package_manager") or detect_python_package_manager(root_dir=root_dir)
    while True:
        raw_pkg = prompt_text(
            "Package manager for dependency caching (pip or uv)",
            default=default_pkg_mgr,
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_pkg is None:
            raise PromptExit("Cancelled during package manager prompt")
        pkg = raw_pkg.strip().lower()
        if pkg in ("pip", "uv", "none"):
            package_manager = pkg
            break
        out_s.write("Invalid package manager. Please choose 'pip' or 'uv'.\n")
        out_s.flush()

    # 4. Install commands (optional)
    default_install_cmds = defaults.get("install_commands")
    if isinstance(default_install_cmds, list):
        default_install_str = "; ".join(default_install_cmds)
    elif isinstance(default_install_cmds, str):
        default_install_str = default_install_cmds
    else:
        default_install_str = ""
    raw_install = prompt_text(
        "Install commands to run before tests (optional, leave blank to skip)",
        default=default_install_str,
        required=False,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if raw_install is None:
        raise PromptExit("Cancelled during install commands prompt")
    install_commands = _split_commands(raw_install) or None

    # 5. Test commands
    default_cmds = defaults.get("commands")
    if isinstance(default_cmds, list):
        default_cmds_str = "; ".join(default_cmds)
    elif isinstance(default_cmds, str):
        default_cmds_str = default_cmds
    else:
        default_cmds_str = "python3 -m unittest discover -s tests"
    while True:
        raw_cmds = prompt_text(
            "Test commands to run in CI (e.g. 'pytest -v' or 'python3 -m unittest discover -s tests')",
            default=default_cmds_str,
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_cmds is None:
            raise PromptExit("Cancelled during test commands prompt")
        commands = _split_commands(raw_cmds)
        if commands:
            break
        out_s.write("Test commands are required.\n")
        out_s.flush()

    # 6. Git LFS checkout
    default_lfs = bool(defaults.get("lfs", detect_git_lfs(root_dir=root_dir)))
    lfs = prompt_confirm(
        "Configure Git LFS checkout for large data assets?",
        default=default_lfs,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if lfs is None:
        raise PromptExit("Cancelled during Git LFS prompt")

    # 7. Lint commands (optional)
    default_lint_cmds = defaults.get("lint_commands")
    if isinstance(default_lint_cmds, list):
        default_lint_str = "; ".join(default_lint_cmds)
    elif isinstance(default_lint_cmds, str):
        default_lint_str = default_lint_cmds
    else:
        default_lint_str = ""
    raw_lint = prompt_text(
        "Lint commands (optional, leave blank to skip)",
        default=default_lint_str,
        required=False,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if raw_lint is None:
        raise PromptExit("Cancelled during lint commands prompt")
    if isinstance(raw_lint, list):
        lint_commands = [c.strip() for c in raw_lint if isinstance(c, str) and c.strip()] or None
    elif isinstance(raw_lint, str):
        lint_commands = _split_commands(raw_lint) or None
    else:
        lint_commands = None

    # 8. Dependency caching
    has_manifest = any((Path(root_dir) / m).exists() for m in ("requirements.txt", "pyproject.toml", "setup.py"))
    if not has_manifest and "cache" not in defaults:
        out_s.write("Notice: No dependency manifest (requirements.txt, pyproject.toml, or setup.py) detected; caching disabled by default.\n")
        out_s.flush()
    default_cache = bool(defaults["cache"]) if "cache" in defaults else has_manifest
    cache = prompt_confirm(
        "Enable package manager dependency caching?",
        default=default_cache,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if cache is None:
        raise PromptExit("Cancelled during dependency caching prompt")

    result = {
        "versions": python_versions,
        "python_setup_mode": python_setup_mode,
        "package_manager": package_manager,
        "commands": commands,
        "lfs": lfs,
        "cache": cache,
    }
    if install_commands:
        result["install_commands"] = install_commands
    if lint_commands:
        result["lint_commands"] = lint_commands
    return result


def prompt_cmake_mpi_ci_options(
    session: Optional[Session] = None,
    in_stream: Optional[TextIO] = None,
    out_stream: Optional[TextIO] = None,
    raise_on_interrupt: bool = False,
    defaults: Optional[Dict[str, Any]] = None,
    detected_cmake: Optional[Dict[str, Any]] = None,
    runs_on: Optional[Any] = None,
) -> Dict[str, Any]:
    """Collect CMake build commands, MPI rank counts, test commands, runner dependencies, ccache, and OpenCL options."""
    in_s = in_stream if in_stream is not None else (getattr(session, "in_stream", None) or sys.stdin)
    out_s = out_stream if out_stream is not None else (getattr(session, "out_stream", None) or sys.stdout)
    defaults = dict(defaults or {})
    if session is not None and getattr(session, "resume_mode", False):
        session_defaults = getattr(session, "defaults", {}) or {}
        defaults = {**session_defaults.get("cmake_mpi", {}), **defaults}

    target_runs_on = runs_on if runs_on is not None else defaults.get("runs_on", "self-hosted")
    is_sh = _is_self_hosted(target_runs_on)
    default_install_deps = defaults.get("install_system_deps")
    if default_install_deps is None:
        default_install_deps = not is_sh
    else:
        default_install_deps = bool(default_install_deps)

    out_s.write("\nConfigure CMake / MPI CI profile:\n")
    default_manual = bool(defaults.get("manual_build", False))
    manual_build = prompt_confirm(
        "Use manual build commands instead of standard CMake (cmake -B build -S . && cmake --build build)?",
        default=default_manual,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if manual_build is None:
        raise PromptExit("Cancelled during manual build prompt")

    default_build = defaults.get("build_commands")
    if isinstance(default_build, list):
        default_build_str = "; ".join(default_build)
    elif isinstance(default_build, str):
        default_build_str = default_build
    else:
        default_build_str = "make -j" if manual_build else "cmake -B build -S .; cmake --build build"

    build_raw = prompt_text(
        "Enter build command(s) (semicolon-separated):",
        default=default_build_str,
        required=True,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if build_raw is None:
        raise PromptExit("Cancelled during build commands prompt")
    build_commands = [c.strip() for c in build_raw.split(";") if c.strip()]

    default_ranks = defaults.get("ranks")
    if isinstance(default_ranks, list):
        default_ranks_str = ", ".join(str(r) for r in default_ranks)
    elif isinstance(default_ranks, str):
        default_ranks_str = default_ranks
    else:
        default_ranks_str = "1, 2"

    ranks_raw = prompt_text(
        "Enter MPI rank counts for smoke checks (comma-separated positive integers):",
        default=default_ranks_str,
        required=True,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if ranks_raw is None:
        raise PromptExit("Cancelled during MPI rank counts prompt")
    ranks = []
    for part in ranks_raw.split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            val = int(part)
            if val not in ranks:
                ranks.append(val)
    if not ranks:
        ranks = [1, 2]

    default_compilers = defaults.get("compilers")
    if isinstance(default_compilers, list):
        default_compilers_str = ", ".join(default_compilers)
    elif isinstance(default_compilers, str):
        default_compilers_str = default_compilers
    else:
        default_compilers_str = "gcc"

    while True:
        raw_compilers = prompt_text(
            "Compiler matrix (comma-separated, e.g. 'gcc' or 'gcc, clang'):",
            default=default_compilers_str,
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_compilers is None:
            raise PromptExit("Cancelled during compiler matrix prompt")
        comps = [c.strip() for c in raw_compilers.split(",") if c.strip()]
        if comps and all(c in ("gcc", "clang") for c in comps) and len(comps) == len(set(comps)):
            compilers = comps
            break
        out_s.write("Invalid compiler matrix. Choose from 'gcc', 'clang' (e.g. 'gcc' or 'gcc, clang').\n")
        out_s.flush()

    default_omp = defaults.get("omp_threads", 2)
    default_omp_str = str(default_omp)
    while True:
        raw_omp = prompt_text(
            "OpenMP threads (OMP_NUM_THREADS, e.g. '2'):",
            default=default_omp_str,
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_omp is None:
            raise PromptExit("Cancelled during OpenMP threads prompt")
        raw_omp = raw_omp.strip()
        if raw_omp.isdigit() and int(raw_omp) > 0:
            omp_threads = int(raw_omp)
            break
        out_s.write("Invalid OpenMP thread count. Expected a positive integer like '2'.\n")
        out_s.flush()

    default_test = defaults.get("test_commands")
    if isinstance(default_test, list):
        default_test_str = "; ".join(default_test)
    elif isinstance(default_test, str):
        default_test_str = default_test
    else:
        default_test_str = "ctest --test-dir build --output-on-failure"

    test_raw = prompt_text(
        "Enter smoke test command(s) (semicolon-separated):",
        default=default_test_str,
        required=True,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if test_raw is None:
        raise PromptExit("Cancelled during test commands prompt")
    test_commands = [c.strip() for c in test_raw.split(";") if c.strip()]

    install_system_deps = prompt_confirm(
        "Install system dependencies via apt-get? (Recommended: No on self-hosted, Yes on hosted)",
        default=default_install_deps,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if install_system_deps is None:
        raise PromptExit("Cancelled during install system dependencies prompt")

    default_deps = defaults.get("runner_dependencies")
    if isinstance(default_deps, list):
        default_deps_str = ", ".join(default_deps)
    elif isinstance(default_deps, str):
        default_deps_str = default_deps
    else:
        default_deps_str = "libopenmpi-dev, openmpi-bin"
        if "clang" in compilers:
            default_deps_str += ", clang"

    deps_raw = prompt_text(
        "Enter runner dependencies (comma-separated apt packages):",
        default=default_deps_str,
        required=True,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if deps_raw is None:
        raise PromptExit("Cancelled during runner dependencies prompt")
    runner_deps = [d.strip() for d in deps_raw.split(",") if d.strip()]

    default_ccache = bool(defaults.get("ccache", True))
    ccache_enabled = prompt_confirm(
        "Enable compiler cache (ccache)?",
        default=default_ccache,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if ccache_enabled is None:
        raise PromptExit("Cancelled during ccache prompt")

    if "opencl" in defaults:
        default_opencl = bool(defaults["opencl"])
    else:
        default_opencl = detected_cmake.get("has_opencl", False) if detected_cmake else False
    opencl_enabled = prompt_confirm(
        "Enable OpenCL compilation and CPU runtime emulation (POCL)?",
        default=default_opencl,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if opencl_enabled is None:
        raise PromptExit("Cancelled during OpenCL prompt")
    if opencl_enabled:
        for pkg in ["pocl-opencl-icd", "ocl-icd-opencl-dev", "opencl-headers", "clinfo"]:
            if pkg not in runner_deps:
                runner_deps.append(pkg)

    default_paths = defaults.get("paths")
    if isinstance(default_paths, list):
        default_paths_str = ", ".join(default_paths)
    elif isinstance(default_paths, str):
        default_paths_str = default_paths
    else:
        default_paths_str = ""
    raw_paths = prompt_text(
        "Path patterns to trigger CI (comma-separated, e.g. 'CMakeLists.txt, src/**', leave blank for all files):",
        default=default_paths_str,
        required=False,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if raw_paths is None:
        raise PromptExit("Cancelled during paths prompt")
    paths = [p.strip() for p in raw_paths.split(",") if p.strip()] if raw_paths.strip() else None

    default_triggers = defaults.get("triggers", ["pull_request"])
    default_triggers_str = ", ".join(default_triggers) if isinstance(default_triggers, list) else str(default_triggers)
    raw_triggers = prompt_text(
        "Workflow triggers for CMake MPI CI (comma-separated: pull_request, push, workflow_dispatch, schedule):",
        default=default_triggers_str,
        required=True,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if raw_triggers is None:
        raise PromptExit("Cancelled during triggers prompt")
    triggers = [t.strip() for t in raw_triggers.split(",") if t.strip()]
    if not triggers:
        triggers = ["pull_request"]

    schedule = None
    if "schedule" in triggers:
        raw_sched = prompt_text(
            "Schedule cron expression:",
            default=default_schedule_string(defaults),
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_sched is None:
            raise PromptExit("Cancelled during schedule prompt")
        schedule = normalize_cron_schedule(raw_sched)

    default_status = resolve_check_spec(defaults)
    raw_status = prompt_text(
        "Terminal status check name:",
        default=default_status,
        required=True,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if raw_status is None:
        raise PromptExit("Cancelled during status check name prompt")
    status_check_name = raw_status.strip() or "status-check"

    post_merge_commands = None
    extended_ranks = None
    if "push" in triggers:
        default_pm = defaults.get("post_merge_commands")
        default_pm_str = "; ".join(default_pm) if isinstance(default_pm, list) else (str(default_pm) if default_pm else "")
        raw_pm = prompt_text(
            "Post-merge commands to run on push (optional, leave blank to skip):",
            default=default_pm_str,
            required=False,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_pm is None:
            raise PromptExit("Cancelled during post-merge commands prompt")
        if raw_pm.strip():
            post_merge_commands = [c.strip() for c in raw_pm.split(";") if c.strip()]

        default_ext = defaults.get("extended_ranks")
        default_ext_str = ", ".join(str(r) for r in default_ext) if isinstance(default_ext, list) else (str(default_ext) if default_ext else "")
        raw_ext = prompt_text(
            "Extended MPI rank counts on push (optional, comma-separated e.g. '4', leave blank to skip):",
            default=default_ext_str,
            required=False,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_ext is None:
            raise PromptExit("Cancelled during extended ranks prompt")
        if raw_ext.strip():
            extended_ranks = []
            for part in raw_ext.split(","):
                part = part.strip()
                if part.isdigit() and int(part) > 0:
                    val = int(part)
                    if val not in extended_ranks:
                        extended_ranks.append(val)

    result = {
        "ranks": ranks,
        "compilers": compilers,
        "omp_threads": omp_threads,
        "build_commands": build_commands,
        "test_commands": test_commands,
        "install_system_deps": install_system_deps,
        "runner_dependencies": runner_deps,
        "ccache": ccache_enabled,
        "opencl": opencl_enabled,
        "manual_build": manual_build,
        "triggers": triggers,
        "status_check_name": status_check_name,
    }
    if paths:
        result["paths"] = paths
    if post_merge_commands:
        result["post_merge_commands"] = post_merge_commands
    if extended_ranks:
        result["extended_ranks"] = extended_ranks
    if schedule:
        result["schedule"] = schedule
    if "cache" in defaults:
        result["cache"] = bool(defaults["cache"])
    return result


def prompt_workflows_stage(
    session: Optional[Session] = None,
    in_stream: Optional[TextIO] = None,
    out_stream: Optional[TextIO] = None,
    raise_on_interrupt: bool = False,
    root_dir: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Prompt user whether to stage standard CI, scientific Python CI, or dependency review workflows in a draft PR.

    Returns:
        A workflows stage dict matching standard policy schema:
            {"name": "workflows", "workflows": [...]}
        or None if declined or cancelled.
    """
    in_s = in_stream if in_stream is not None else (getattr(session, "in_stream", None) or sys.stdin)
    out_s = out_stream if out_stream is not None else (getattr(session, "out_stream", None) or sys.stdout)
    root_dir = _resolve_root_dir(root_dir, session)

    default_confirm = False
    default_indices: Optional[List[int]] = [0, 1]

    detected_cmake = None
    repo_target = (getattr(session, "repository", None) or getattr(session, "repo_name", None)) if session else None
    if session and getattr(session, "api", None) and repo_target:
        default_branch = ((getattr(session, "inventory", None) or {}).get("repository", {}) or {}).get("default_branch", "main")
        detected_cmake = detect_cmake_mpi_project(api=session.api, repository=repo_target, default_branch=default_branch)
    if not detected_cmake or not detected_cmake.get("detected"):
        detected_cmake = detect_cmake_mpi_project(workspace_dir=root_dir)

    inventory = (getattr(session, "inventory", None) or {}) if session else {}
    caps = inventory.get("capabilities", {})
    repo_data = inventory.get("repository", {})
    dep_review_supported = True
    if caps:
        dep_cap = caps.get("dependency_review")
        if dep_cap and dep_cap.get("status") == "unsupported":
            dep_review_supported = False
    elif repo_data.get("visibility") == "private":
        plan = repo_data.get("plan", "unknown")
        if plan in ("free", "unknown"):
            dep_review_supported = False

    workflow_option_defs = [
        ("pr-ci", "Standard PR CI (ci.yml)"),
        ("dependency-review", "Dependency review (dependency-review.yml)"),
        ("python-ci", "Scientific Python CI profile (ci.yml)"),
        ("cmake-ci", "CMake / MPI CI (ci.yml)"),
        ("container-publish", "Container build and publish (container-publish.yml)"),
    ]
    if not dep_review_supported:
        workflow_option_defs = [opt for opt in workflow_option_defs if opt[0] != "dependency-review"]

    options = [label for _, label in workflow_option_defs]
    opt_key_to_idx = {key: i for i, (key, _) in enumerate(workflow_option_defs)}

    default_indices: List[int] = []
    default_confirm = False

    if session is not None and getattr(session, "resume_mode", False):
        defaults = getattr(session, "defaults", {}) or {}
        workflows_default = defaults.get("workflows")
        if isinstance(workflows_default, list) and workflows_default:
            default_confirm = True
            default_indices = []
            for item in workflows_default:
                t = item if isinstance(item, str) else item.get("template", "")
                p = "" if isinstance(item, str) else item.get("profile", "")
                if p in ("cmake-mpi", "cmake"):
                    if "cmake-ci" in opt_key_to_idx:
                        default_indices.append(opt_key_to_idx["cmake-ci"])
                elif t in ("profile-ci", "python") or p in ("python", "scientific-python"):
                    if "python-ci" in opt_key_to_idx:
                        default_indices.append(opt_key_to_idx["python-ci"])
                elif t in ("pr-ci", "ci", "standard-ci"):
                    if "pr-ci" in opt_key_to_idx:
                        default_indices.append(opt_key_to_idx["pr-ci"])
                elif t in ("dependency-review", "dependency"):
                    if "dependency-review" in opt_key_to_idx:
                        default_indices.append(opt_key_to_idx["dependency-review"])
                elif t in ("container-publish", "container"):
                    if "container-publish" in opt_key_to_idx:
                        default_indices.append(opt_key_to_idx["container-publish"])
            if not default_indices:
                default_indices = [opt_key_to_idx[k] for k in ["pr-ci", "dependency-review"] if k in opt_key_to_idx]
        elif workflows_default is not None and not workflows_default:
            default_confirm = False
    elif detected_cmake and detected_cmake.get("detected"):
        default_indices = [opt_key_to_idx[k] for k in ["cmake-ci", "dependency-review"] if k in opt_key_to_idx]

    target_repo = getattr(session, "repository", None) or getattr(session, "repo_name", None)
    is_python = detect_python_project(
        api=getattr(session, "api", None),
        repository=target_repo,
        inventory=getattr(session, "inventory", None),
        root_dir=root_dir,
    )
    if is_python and not (detected_cmake and detected_cmake.get("detected")) and (session is None or not getattr(session, "resume_mode", False)):
        default_indices = [opt_key_to_idx[k] for k in ["python-ci", "dependency-review"] if k in opt_key_to_idx]

    current_workflows = (getattr(session, "inventory", {}) or {}).get("workflows") if session else None
    if isinstance(current_workflows, list):
        current_text = ", ".join(str(item) for item in current_workflows) or "none"
    else:
        current_text = "unknown until workflow preflight"
    out_s.write("\nWorkflows automate checks on pull requests. Existing workflow files are checked for ownership and changes before staging.\n")
    out_s.write(f"Current workflow state: {current_text}.\n")
    if detected_cmake and detected_cmake.get("detected"):
        out_s.write("  Detected CMake project (CMakeLists.txt)" + (" with MPI configuration" if detected_cmake.get("has_mpi") else "") + ".\n")
        if detected_cmake.get("has_opencl"):
            out_s.write("  Detected OpenCL references in CMakeLists.txt.\n")
    out_s.write("  Standard PR CI (ci.yml): runs project checks on pull requests; configure its commands for this repository.\n")
    if dep_review_supported:
        out_s.write("  Dependency review (dependency-review.yml): flags newly introduced vulnerable dependencies.\n")
    else:
        out_s.write("  Dependency review is unsupported on this repository (requires GitHub Advanced Security on private repositories).\n")
    out_s.write("  Scientific Python CI (ci.yml): runs Python matrix tests with caching and Git LFS support.\n")
    out_s.write("  CMake / MPI CI (ci.yml): focused CMake and MPI CI with runner dependencies, ccache, and documented rank smoke tests.\n")
    out_s.write("  Container build and publish (container-publish.yml): builds and publishes a container image to GitHub Container Registry.\n")
    if is_python:
        out_s.write("Detected Python repository. Focused Scientific Python CI profile is available.\n")
    out_s.write("Pinned Action revisions keep an upstream Action version fixed until you explicitly update it.\n")
    out_s.write("Selected workflows go into a draft Setup Pull Request. The owner must inspect and merge it before checks run. No leaves workflows unchanged.\n")
    out_s.write(f"Recommended default: {'Yes' if default_confirm else 'No'}.\n")
    out_s.flush()

    confirm = prompt_confirm(
        "Stage standard CI or dependency review workflows in a draft pull request?",
        default=default_confirm,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if not confirm:
        return None

    use_self_hosted = prompt_confirm(
        "Use self-hosted runner as the default for configured workflows? (Recommended: Yes)",
        default=True,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if use_self_hosted is None:
        return None

    if use_self_hosted:
        runner_labels_raw = prompt_text(
            "Runner label(s) for self-hosted workflows (comma-separated)",
            default="self-hosted",
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if runner_labels_raw is None:
            return None
        labels = [l.strip() for l in runner_labels_raw.split(",") if l.strip()]
        global_runs_on = labels[0] if len(labels) == 1 else labels
    else:
        hosted_runner = prompt_text(
            "Hosted runner for workflows",
            default="ubuntu-24.04",
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if hosted_runner is None:
            return None
        global_runs_on = hosted_runner.strip()

    if session is not None:
        session.default_runner = global_runs_on

    options = [label for _, label in workflow_option_defs]

    selected = prompt_multichoice(
        "Select workflows to stage in a draft pull request:",
        options=options,
        default=default_indices,
        return_indices=True,
        required=False,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if not selected:
        return None

    selected_keys = {workflow_option_defs[i][0] for i in selected if i < len(workflow_option_defs)}
    workflows_list: List[Dict[str, Any]] = []
    default_branch = ((getattr(session, "inventory", None) or {}).get("repository", {}) or {}).get("default_branch", "main")
    profile_selected = False

    has_both_profiles = ("python-ci" in selected_keys and "cmake-ci" in selected_keys)
    python_filename = "python-ci.yml" if has_both_profiles else "ci.yml"
    cmake_filename = "cmake-ci.yml" if has_both_profiles else "ci.yml"
    if has_both_profiles:
        out_s.write("Multiple CI profiles selected; assigning python-ci.yml and cmake-ci.yml to prevent filename collision.\n")

    if "python-ci" in selected_keys:
        profile_selected = True
        py_defaults = (getattr(session, "defaults", {}) or {}).get("python") if session else None
        try:
            py_opts = prompt_python_ci_options(
                session=session,
                in_stream=in_s,
                out_stream=out_s,
                raise_on_interrupt=raise_on_interrupt,
                defaults=py_defaults,
                root_dir=root_dir,
            )
        except PromptExit:
            if raise_on_interrupt:
                raise
            return None
        default_triggers = ["pull_request"]
        if py_defaults and "triggers" in py_defaults:
            default_triggers = py_defaults["triggers"]
        default_triggers_str = ", ".join(default_triggers) if isinstance(default_triggers, list) else str(default_triggers)
        raw_triggers = prompt_text(
            "Workflow triggers for Python CI (comma-separated: pull_request, push, workflow_dispatch, schedule)",
            default=default_triggers_str,
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_triggers is None:
            return None
        py_triggers = [t.strip() for t in raw_triggers.split(",") if t.strip()]
        if not py_triggers:
            py_triggers = ["pull_request"]

        py_schedule = None
        if "schedule" in py_triggers:
            raw_sched = prompt_text(
                "Schedule cron expression:",
                default=default_schedule_string(py_defaults),
                required=True,
                raise_on_interrupt=raise_on_interrupt,
                in_stream=in_s,
                out_stream=out_s,
            )
            if raw_sched is None:
                return None
            py_schedule = normalize_cron_schedule(raw_sched)

        post_merge_commands = None
        if "push" in py_triggers:
            default_pm = py_defaults.get("post_merge_commands") if py_defaults else None
            default_pm_str = "; ".join(default_pm) if isinstance(default_pm, list) else (str(default_pm) if default_pm else "")
            raw_pm = prompt_text(
                "Post-merge commands to run on push (optional, leave blank to skip)",
                default=default_pm_str,
                required=False,
                raise_on_interrupt=raise_on_interrupt,
                in_stream=in_s,
                out_stream=out_s,
            )
            if raw_pm is None:
                return None
            post_merge_commands = _split_commands(raw_pm) or None

        py_ci_entry: Dict[str, Any] = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": python_filename,
            "triggers": py_triggers,
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "profile": "python",
            "python": py_opts,
            "runs_on": global_runs_on,
        }
        if py_defaults and py_defaults.get("paths"):
            py_ci_entry["paths"] = py_defaults["paths"]
        if post_merge_commands:
            py_ci_entry["post_merge_commands"] = post_merge_commands
        if py_schedule:
            py_ci_entry["schedule"] = py_schedule
        if session and getattr(session, "api", None) and target_repo:
            expected_sha = managed_workflow_expected_sha(session.api, target_repo, f".github/workflows/{python_filename}", default_branch)
            if expected_sha:
                py_ci_entry["expected_sha"] = expected_sha
                out_s.write(f"Managed CI workflow update: current blob SHA {expected_sha}.\n")
        workflows_list.append(py_ci_entry)

    if "cmake-ci" in selected_keys:
        profile_selected = True
        cmake_defaults = (getattr(session, "defaults", {}) or {}).get("cmake_mpi") if session else None
        try:
            cmake_opts = prompt_cmake_mpi_ci_options(
                session=session,
                in_stream=in_s,
                out_stream=out_s,
                raise_on_interrupt=raise_on_interrupt,
                defaults=cmake_defaults,
                detected_cmake=detected_cmake,
                runs_on=global_runs_on,
            )
        except PromptExit:
            if raise_on_interrupt:
                raise
            return None

        triggers = cmake_opts.pop("triggers", ["pull_request"])
        paths = cmake_opts.pop("paths", None)
        schedule = cmake_opts.pop("schedule", None)
        status_check_name = cmake_opts.pop("status_check_name", "status-check")
        post_merge_commands = cmake_opts.pop("post_merge_commands", None)
        extended_ranks = cmake_opts.pop("extended_ranks", None)

        cmake_wf: Dict[str, Any] = {
            "template": "profile-ci",
            "template_version": 1,
            "filename": cmake_filename,
            "triggers": triggers,
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "profile": "cmake-mpi",
            "cmake_mpi": cmake_opts,
            "runs_on": global_runs_on,
            "status_check_name": status_check_name,
        }
        if paths:
            cmake_wf["paths"] = paths
        if schedule:
            cmake_wf["schedule"] = schedule
        if post_merge_commands:
            cmake_wf["post_merge_commands"] = post_merge_commands
        if extended_ranks:
            cmake_wf["extended_ranks"] = extended_ranks
        if session and getattr(session, "api", None) and target_repo:
            expected_sha = managed_workflow_expected_sha(session.api, target_repo, f".github/workflows/{cmake_filename}", default_branch)
            if expected_sha:
                cmake_wf["expected_sha"] = expected_sha
                out_s.write(f"Managed CI workflow update: current blob SHA {expected_sha}.\n")
        workflows_list.append(cmake_wf)

    if "pr-ci" in selected_keys and not profile_selected:
        default_cmd_str = "; ".join(DEFAULT_CI_COMMANDS)
        raw_cmd = prompt_text(
            "Test commands to run in PR CI (semicolon-separated)",
            default=default_cmd_str,
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_cmd is None:
            return None
        pr_ci_commands = [c.strip() for c in raw_cmd.split(";") if c.strip()]
        if not pr_ci_commands:
            pr_ci_commands = list(DEFAULT_CI_COMMANDS)

        raw_triggers = prompt_text(
            "Workflow triggers for PR CI (comma-separated: pull_request, push, workflow_dispatch, schedule)",
            default="pull_request",
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_triggers is None:
            return None
        pr_ci_triggers = [t.strip() for t in raw_triggers.split(",") if t.strip()]
        if not pr_ci_triggers:
            pr_ci_triggers = ["pull_request"]

        pr_schedule = None
        if "schedule" in pr_ci_triggers:
            raw_sched = prompt_text(
                "Schedule cron expression:",
                default=DEFAULT_SCHEDULE,
                required=True,
                raise_on_interrupt=raise_on_interrupt,
                in_stream=in_s,
                out_stream=out_s,
            )
            if raw_sched is None:
                return None
            pr_schedule = normalize_cron_schedule(raw_sched)

        raw_paths = prompt_text(
            "Paths filter patterns for PR CI (comma-separated, optional, e.g. 'src/**', leave blank to run on all paths):",
            default="",
            required=False,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_paths is None:
            return None
        pr_paths = [p.strip() for p in raw_paths.split(",") if p.strip()] if raw_paths.strip() else None

        override_runner = prompt_confirm(
            "Override runner specifically for PR CI?",
            default=False,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if override_runner is None:
            return None

        if override_runner:
            override_self_hosted = prompt_confirm(
                "Use self-hosted runner for PR CI? (Recommended: Yes)",
                default=True,
                raise_on_interrupt=raise_on_interrupt,
                in_stream=in_s,
                out_stream=out_s,
            )
            if override_self_hosted:
                custom_labels = prompt_text(
                    "Runner label(s) for PR CI (comma-separated)",
                    default="self-hosted",
                    required=True,
                    raise_on_interrupt=raise_on_interrupt,
                    in_stream=in_s,
                    out_stream=out_s,
                )
                if custom_labels is None:
                    return None
                clist = [l.strip() for l in custom_labels.split(",") if l.strip()]
                pr_ci_runs_on = clist[0] if len(clist) == 1 else clist
            else:
                hosted_override = prompt_text(
                    "Hosted runner for PR CI",
                    default="ubuntu-24.04",
                    required=True,
                    raise_on_interrupt=raise_on_interrupt,
                    in_stream=in_s,
                    out_stream=out_s,
                )
                if hosted_override is None:
                    return None
                pr_ci_runs_on = hosted_override.strip()
        else:
            pr_ci_runs_on = global_runs_on

        ci_entry: Dict[str, Any] = {
            "template": "pr-ci",
            "template_version": 1,
            "filename": "ci.yml",
            "triggers": pr_ci_triggers,
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "commands": pr_ci_commands,
            "runs_on": pr_ci_runs_on,
        }
        if pr_schedule:
            ci_entry["schedule"] = pr_schedule
        if pr_paths:
            ci_entry["paths"] = pr_paths
        if session and getattr(session, "api", None) and target_repo:
            expected_sha = managed_workflow_expected_sha(session.api, target_repo, ".github/workflows/ci.yml", default_branch)
            if expected_sha:
                ci_entry["expected_sha"] = expected_sha
                out_s.write(f"Managed CI workflow update: current blob SHA {expected_sha}.\n")
        workflows_list.append(ci_entry)
    elif "pr-ci" in selected_keys and profile_selected:
        out_s.write("Focused CI profile selected; supersedes Standard PR CI for ci.yml.\n")
    if "dependency-review" in selected_keys:
        dep_defaults = (getattr(session, "defaults", {}) or {}).get("dependency_review") or {}
        if not dep_defaults and isinstance((getattr(session, "defaults", {}) or {}).get("workflows"), list):
            for wf_item in (getattr(session, "defaults", {}) or {}).get("workflows", []):
                if isinstance(wf_item, dict) and wf_item.get("template") == "dependency-review":
                    dep_defaults = wf_item
                    break

        default_sev = dep_defaults.get("fail_on_severity", "high")
        while True:
            raw_sev = prompt_text(
                "Fail on severity level for dependency review (low, moderate, high, critical)",
                default=default_sev,
                required=True,
                raise_on_interrupt=raise_on_interrupt,
                in_stream=in_s,
                out_stream=out_s,
            )
            if raw_sev is None:
                return None
            s_val = raw_sev.strip().lower()
            if s_val in ("low", "moderate", "high", "critical"):
                fail_on_severity = s_val
                break
            out_s.write("Invalid severity level. Choose one of: low, moderate, high, critical.\n")
            out_s.flush()

        default_allow = dep_defaults.get("allow_licenses", "MIT, Apache-2.0, BSD-3-Clause, BSD-2-Clause, ISC, Python-2.0")
        if isinstance(default_allow, list):
            default_allow = ", ".join(default_allow)
        raw_allow = prompt_text(
            "Allowed licenses (comma-separated)",
            default=default_allow,
            required=False,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_allow is None:
            return None
        allow_licenses = raw_allow.strip()

        default_deny = dep_defaults.get("deny_licenses", "")
        if isinstance(default_deny, list):
            default_deny = ", ".join(default_deny)
        raw_deny = prompt_text(
            "Denied licenses (optional, comma-separated, leave blank for none)",
            default=default_deny,
            required=False,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_deny is None:
            return None
        deny_licenses = raw_deny.strip()

        default_comment = dep_defaults.get("comment_summary_in_pr", "never")
        if isinstance(default_comment, bool):
            default_comment = "always" if default_comment else "never"
        while True:
            raw_comm = prompt_text(
                "Comment summary in pull request (always, never, on-failure)",
                default=default_comment,
                required=True,
                raise_on_interrupt=raise_on_interrupt,
                in_stream=in_s,
                out_stream=out_s,
            )
            if raw_comm is None:
                return None
            c_val = raw_comm.strip().lower()
            if c_val in ("always", "never", "on-failure"):
                comment_summary_in_pr = c_val
                break
            out_s.write("Invalid option. Choose one of: always, never, on-failure.\n")
            out_s.flush()

        dep_entry: Dict[str, Any] = {
            "template": "dependency-review",
            "template_version": 1,
            "filename": "dependency-review.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "action_sha": DEFAULT_DEPENDENCY_SHA,
            "fail_on_severity": fail_on_severity,
            "allow_licenses": allow_licenses,
            "runs_on": global_runs_on,
        }
        if deny_licenses:
            dep_entry["deny_licenses"] = deny_licenses
        if comment_summary_in_pr:
            dep_entry["comment_summary_in_pr"] = comment_summary_in_pr

        if session and getattr(session, "api", None) and target_repo:
            expected_sha = managed_workflow_expected_sha(session.api, target_repo, ".github/workflows/dependency-review.yml", default_branch)
            if expected_sha:
                dep_entry["expected_sha"] = expected_sha
                out_s.write(f"Managed dependency review workflow update: current blob SHA {expected_sha}.\n")
        workflows_list.append(dep_entry)
    if "container-publish" in selected_keys:
        cont_defaults = (getattr(session, "defaults", {}) or {}).get("container") or {}
        if not cont_defaults and isinstance((getattr(session, "defaults", {}) or {}).get("workflows"), list):
            for wf_item in (getattr(session, "defaults", {}) or {}).get("workflows", []):
                if isinstance(wf_item, dict) and wf_item.get("template") in ("container-publish", "container"):
                    cont_defaults = wf_item
                    break

        default_triggers = cont_defaults.get("triggers", ["pull_request", "push"])
        default_triggers_str = ", ".join(default_triggers) if isinstance(default_triggers, list) else str(default_triggers)
        valid_trigger_names = {"pull_request", "push", "workflow_dispatch", "schedule"}
        while True:
            raw_triggers = prompt_text(
                "Workflow triggers for container publish (comma-separated: pull_request, push, workflow_dispatch, schedule)",
                default=default_triggers_str,
                required=True,
                raise_on_interrupt=raise_on_interrupt,
                in_stream=in_s,
                out_stream=out_s,
            )
            if raw_triggers is None:
                return None
            c_triggers = [t.strip() for t in raw_triggers.split(",") if t.strip()]
            if c_triggers and all(t in valid_trigger_names for t in c_triggers) and len(c_triggers) == len(set(c_triggers)):
                container_triggers = c_triggers
                break
            out_s.write("Invalid triggers. Choose unique triggers from: pull_request, push, workflow_dispatch, schedule.\n")
            out_s.flush()

        container_schedule = None
        if "schedule" in container_triggers:
            raw_sched = prompt_text(
                "Schedule cron expression:",
                default=default_schedule_string(cont_defaults, DEFAULT_CONTAINER_SCHEDULE),
                required=True,
                raise_on_interrupt=raise_on_interrupt,
                in_stream=in_s,
                out_stream=out_s,
            )
            if raw_sched is None:
                return None
            container_schedule = normalize_cron_schedule(raw_sched, DEFAULT_CONTAINER_SCHEDULE)

        default_registry = cont_defaults.get("registry", "ghcr.io")
        raw_reg = prompt_text(
            "Container registry",
            default=default_registry,
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_reg is None:
            return None
        container_registry = raw_reg.strip()

        default_dockerfile = cont_defaults.get("dockerfile", "Dockerfile")
        raw_df = prompt_text(
            "Dockerfile path",
            default=default_dockerfile,
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_df is None:
            return None
        container_dockerfile = raw_df.strip()

        default_context = cont_defaults.get("context", ".")
        raw_ctx = prompt_text(
            "Docker build context",
            default=default_context,
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_ctx is None:
            return None
        container_context = raw_ctx.strip()

        default_tags = cont_defaults.get("tags")
        if isinstance(default_tags, list):
            default_tags_str = ", ".join(default_tags)
        elif isinstance(default_tags, str):
            default_tags_str = default_tags
        else:
            default_tags_str = f"{container_registry}/${{{{ github.repository }}}}:${{{{ github.sha }}}}"
        raw_tags = prompt_text(
            "Container image tags (comma-separated or single tag)",
            default=default_tags_str,
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_tags is None:
            return None
        tags_list = [t.strip() for t in raw_tags.split(",") if t.strip()]
        container_tags = tags_list if len(tags_list) > 1 else [raw_tags.strip()]

        container_entry: Dict[str, Any] = {
            "template": "container-publish",
            "template_version": 1,
            "filename": "container-publish.yml",
            "triggers": container_triggers,
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "login_action_sha": DEFAULT_CONTAINER_LOGIN_SHA,
            "build_action_sha": DEFAULT_CONTAINER_BUILD_SHA,
            "registry": container_registry,
            "dockerfile": container_dockerfile,
            "context": container_context,
            "tags": container_tags,
            "runs_on": global_runs_on,
        }
        if container_schedule:
            container_entry["schedule"] = container_schedule
        if session and getattr(session, "api", None) and target_repo:
            expected_sha = managed_workflow_expected_sha(session.api, target_repo, ".github/workflows/container-publish.yml", default_branch)
            if expected_sha:
                container_entry["expected_sha"] = expected_sha
                out_s.write(f"Managed container publish workflow update: current blob SHA {expected_sha}.\n")
        workflows_list.append(container_entry)

    if not workflows_list:
        return None

    return {
        "name": "workflows",
        "workflows": workflows_list,
    }


def prompt_rulesets_stage(
    session: Optional[Session] = None,
    in_stream: Optional[TextIO] = None,
    out_stream: Optional[TextIO] = None,
    raise_on_interrupt: bool = False,
    workflows_stage: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Prompt user whether to configure branch ruleset merge gates and collect required inputs.

    Returns:
        A gates stage dict matching standard policy schema:
            {"name": "gates", "ruleset_name": "quality-gate", "evidence_pr": ..., "reviewer": ..., "checks": ...}
        or None if declined or cancelled.
    """
    in_s = in_stream if in_stream is not None else (getattr(session, "in_stream", None) or sys.stdin)
    out_s = out_stream if out_stream is not None else (getattr(session, "out_stream", None) or sys.stdout)

    default_confirm = False
    default_pr = None
    default_reviewer = None
    default_ruleset_name = "quality-gate"
    default_check = "ci-check"

    # Detect staged, active, or configured profile workflows
    detected_profile_wf = None
    candidates = []
    if workflows_stage and isinstance(workflows_stage.get("workflows"), list):
        candidates.extend(workflows_stage["workflows"])
    elif session and getattr(session, "staged_workflows", None):
        staged = getattr(session, "staged_workflows")
        if isinstance(staged, dict) and isinstance(staged.get("workflows"), list):
            candidates.extend(staged["workflows"])

    if session and getattr(session, "defaults", None):
        defs = session.defaults
        if isinstance(defs.get("workflows"), list):
            candidates.extend(defs["workflows"])
        if isinstance(defs.get("ci_profile"), dict):
            candidates.append(defs["ci_profile"])

    for item in candidates:
        if isinstance(item, dict):
            if item.get("template") == "profile-ci" or "profile" in item or item.get("name") == "ci-profile":
                detected_profile_wf = item
                break

    if not detected_profile_wf and session and getattr(session, "api", None) and (getattr(session, "repository", None) or getattr(session, "repo_name", None)):
        target_repo = getattr(session, "repository", None) or getattr(session, "repo_name", None)
        default_branch = ((getattr(session, "inventory", None) or {}).get("repository", {}) or {}).get("default_branch", "main")
        try:
            try:
                from scripts.install_github_workflows import _file
            except ImportError:
                from install_github_workflows import _file
            ci_data = _file(session.api, f"/repos/{target_repo}", ".github/workflows/ci.yml", default_branch)
            if ci_data and ci_data[0]:
                content_text = ci_data[0].decode("utf-8", errors="replace")
                declared_name = extract_profile_status_check(content_text)
                if declared_name:
                    detected_profile_wf = {"status_check_name": declared_name}
        except Exception:
            pass

    if detected_profile_wf:
        default_check = detected_profile_wf.get("status_check_name") or "status-check"

    gates_default = None
    if session is not None and getattr(session, "resume_mode", False):
        defaults = getattr(session, "defaults", {}) or {}
        gates_default = defaults.get("gates")
        if isinstance(gates_default, dict):
            default_confirm = True
            if "evidence_pr" in gates_default:
                default_pr = str(gates_default["evidence_pr"])
            if "reviewer" in gates_default:
                default_reviewer = str(gates_default["reviewer"])
            if "ruleset_name" in gates_default:
                default_ruleset_name = str(gates_default["ruleset_name"])
            if "checks" in gates_default:
                c_val = gates_default["checks"]
                if isinstance(c_val, dict):
                    default_check = ", ".join(f"{path}={name}" if path != ".github/workflows/ci.yml" else name for name, path in c_val.items())
                elif isinstance(c_val, str):
                    default_check = c_val

    rulesets = (getattr(session, "inventory", {}) or {}).get("rulesets") if session else None
    if isinstance(rulesets, list):
        current_rulesets = ", ".join(item.get("name", "unnamed") for item in rulesets if isinstance(item, dict)) or "none"
        if detected_profile_wf:
            for item in rulesets:
                if isinstance(item, dict) and isinstance(item.get("rules"), list):
                    for r in item["rules"]:
                        if isinstance(r, dict) and r.get("type") == "required_status_checks":
                            for sc in (r.get("parameters", {}) or {}).get("required_status_checks", []):
                                ctx = sc.get("context")
                                if ctx and ctx != default_check:
                                    out_s.write(
                                        f"Discrepancy detected: inventory ruleset check '{ctx}' does not match "
                                        f"active CI profile check '{default_check}'. Proposing '{default_check}'.\n"
                                    )
    else:
        current_rulesets = "unknown"
    out_s.write(f"\nRulesets protect the default branch (Current rulesets: {current_rulesets}).\n")
    out_s.write(
        f"Yes needs evidence PR (or skip on greenfield), reviewer, and successful CI check. "
        f"Recommended default: {'Yes' if default_confirm else 'No'}.\n"
    )
    out_s.flush()

    confirm = prompt_confirm(
        "Configure branch ruleset merge gates?",
        default=default_confirm,
        raise_on_interrupt=raise_on_interrupt,
        in_stream=in_s,
        out_stream=out_s,
    )
    if not confirm:
        return None

    # Collect Evidence PR number with validation and reprompting
    pr_number: Optional[int] = None
    while True:
        raw_pr = prompt_text(
            "Evidence PR number (or 'skip' / blank on greenfield repos)",
            default=default_pr,
            required=False,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_pr is None:
            return None
        cleaned = raw_pr.strip().lower()
        if not cleaned or cleaned in ("skip", "none", "-", "greenfield"):
            pr_number = None
            break
        validated_pr = validate_pr_number(raw_pr)
        if validated_pr is not None:
            pr_number = validated_pr
            break
        out_s.write("Invalid evidence PR number. Please enter a positive integer or 'skip'.\n")
        out_s.flush()

    # Collect Reviewer username with validation and reprompting
    reviewer: Optional[str] = None
    approvals_count = gates_default.get("required_approving_review_count") if isinstance(gates_default, dict) else None
    while True:
        raw_reviewer = prompt_text(
            "Reviewer username (or 'none' for solo maintainer with zero required approvals)",
            default=default_reviewer,
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_reviewer is None:
            return None
        if raw_reviewer.strip().lower() in ("none", "solo", "-"):
            reviewer = None
            if approvals_count is None:
                approvals_count = 0
            break
        validated_reviewer = validate_reviewer(raw_reviewer)
        if validated_reviewer is not None:
            reviewer = validated_reviewer
            break
        out_s.write("Reviewer username is required and must not be empty. Enter 'none' for solo maintainer.\n")
        out_s.flush()

    # Collect Workflow check name with validation and reprompting
    checks: Dict[str, str]
    while True:
        raw_check = prompt_text(
            f"Workflow check name (e.g. '{default_check}' or '.github/workflows/ci.yml={default_check}')",
            default=default_check,
            required=True,
            raise_on_interrupt=raise_on_interrupt,
            in_stream=in_s,
            out_stream=out_s,
        )
        if raw_check is None:
            return None
        parsed_checks = parse_check_spec(raw_check)
        if parsed_checks is not None:
            checks = parsed_checks
            break
        out_s.write("Invalid workflow check format. Expected CHECK_NAME or WORKFLOW_PATH=CHECK_NAME.\n")
        out_s.flush()

    result: Dict[str, Any] = {
        "name": "gates",
        "ruleset_name": default_ruleset_name,
        "evidence_pr": pr_number,
        "checks": checks,
    }
    if reviewer is not None:
        result["reviewer"] = reviewer
    if approvals_count is not None:
        result["required_approving_review_count"] = approvals_count
    return result


def prompt_workflows_rulesets_stage(
    session: Optional[Session] = None,
    in_stream: Optional[TextIO] = None,
    out_stream: Optional[TextIO] = None,
    root_dir: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Execute questionnaire for both workflows and rulesets stages.

    Returns:
        List of generated stage dictionaries (e.g. [{"name": "workflows", ...}, {"name": "gates", ...}]),
        an empty list if neither was selected, or None if input was aborted (EOF).
    """
    in_s = in_stream if in_stream is not None else (getattr(session, "in_stream", None) or sys.stdin)
    out_s = out_stream if out_stream is not None else (getattr(session, "out_stream", None) or sys.stdout)
    root_dir = _resolve_root_dir(root_dir, session)

    stages: List[Dict[str, Any]] = []

    try:
        wf_stage = prompt_workflows_stage(
            session=session,
            in_stream=in_s,
            out_stream=out_s,
            raise_on_interrupt=True,
            root_dir=root_dir,
        )
        if wf_stage is not None:
            stages.append(wf_stage)
            if session is not None:
                session.staged_workflows = wf_stage

        gates_stage = prompt_rulesets_stage(
            session=session,
            in_stream=in_s,
            out_stream=out_s,
            raise_on_interrupt=True,
            workflows_stage=wf_stage,
        )
        if gates_stage is not None:
            stages.append(gates_stage)

        issue_state_gate = prompt_issue_state_gate_stage(session, in_stream=in_s, out_stream=out_s)
        if issue_state_gate is not None:
            stages.append(issue_state_gate)

        agent_skills_stage = prompt_agent_skills_stage(session, in_stream=in_s, out_stream=out_s)
        if agent_skills_stage is not None:
            stages.append(agent_skills_stage)
    except PromptExit:
        return None

    return stages


def prompt_agent_skills_stage(session=None, in_stream=None, out_stream=None):
    """Offer the reviewed Matt Pocock skill snapshot as an opt-in bundle."""
    in_s = in_stream if in_stream is not None else (getattr(session, "in_stream", None) or sys.stdin)
    out_s = out_stream if out_stream is not None else (getattr(session, "out_stream", None) or sys.stdout)
    defaults = getattr(session, "defaults", {}) or {} if session is not None else {}
    default = bool(getattr(session, "resume_mode", False) and defaults.get("agent_skills") is True)
    out_s.write("Optional Matt Pocock agent skills copy reviewed skill files into .agents/skills/ as repository data. They are not executed by setup. Selecting the bundle also stages a pinned Documentation Freshness Audit, its trusted reporter, and a short operator guide in the same draft Setup Pull Request. The audit checks only installed or explicitly declared target documentation. Existing owner edits are preserved and reported as conflicts. The documentation-freshness status is advisory until an exact required-check rule is configured and read back.\n")
    out_s.write(f"Recommended default: {'Yes' if default else 'No'}.\n")
    out_s.flush()
    if not prompt_confirm("Install the Matt Pocock agent skill bundle?", default=default,
                          raise_on_interrupt=True, in_stream=in_s, out_stream=out_s):
        return None
    return {"name": "agent-skills", "bundle": "mattpocock"}


def prompt_issue_state_gate_stage(session=None, in_stream=None, out_stream=None):
    """Ask whether the exact trusted issue-state check should block merges to main."""
    in_s = in_stream if in_stream is not None else (getattr(session, "in_stream", None) or sys.stdin)
    out_s = out_stream if out_stream is not None else (getattr(session, "out_stream", None) or sys.stdout)
    default = False
    defaults = getattr(session, "defaults", {}) or {} if session is not None else {}
    if getattr(session, "resume_mode", False) and isinstance(defaults, dict):
        default = defaults.get("issue_state_gate") is True
    out_s.write("The optional Issue State Gate requires every implementation issue listed in a pull request's Implementation issues section to be closed before merging to main. It requires no reviewer approval and is staged in the draft Setup Pull Request. Activation waits until its exact successful check source is observable.\n")
    out_s.write(f"Recommended default: {'Yes' if default else 'No'}.\n")
    out_s.flush()
    if not prompt_confirm("Require Issue State Gate on main?", default=default,
                          raise_on_interrupt=True,
                          in_stream=in_s, out_stream=out_s):
        return None
    return {"name": "issue-state-gate"}
