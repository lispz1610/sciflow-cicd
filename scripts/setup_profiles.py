"""Setup Profile catalog, Universal Common Baseline Governance, and Archetype Recommendation Engine.

This module defines the 6 repository archetypes, their Universal Common Baseline
inheritance, heuristic archetype detection based on repository markers and live inventory,
and complete valid Version 2 Policy generation.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    from scripts.setup_github_repo import (
        DEFAULT_CHECKOUT_SHA,
        validate_policy,
    )
    from scripts.install_github_workflows import (
        DEFAULT_CONTAINER_BUILD_SHA,
        DEFAULT_CONTAINER_LOGIN_SHA,
        DEFAULT_DEPENDENCY_SHA,
        PYTHON_INDICATOR_FILES,
        detect_cmake_mpi_project,
        detect_python_project,
    )
except ImportError:
    from setup_github_repo import (
        DEFAULT_CHECKOUT_SHA,
        validate_policy,
    )
    from install_github_workflows import (
        DEFAULT_CONTAINER_BUILD_SHA,
        DEFAULT_CONTAINER_LOGIN_SHA,
        DEFAULT_DEPENDENCY_SHA,
        PYTHON_INDICATOR_FILES,
        detect_cmake_mpi_project,
        detect_python_project,
    )

# 5 Canonical Triage Labels
CANONICAL_TRIAGE_LABELS: List[Dict[str, str]] = [
    {
        "name": "needs-triage",
        "color": "ededed",
        "description": "Needs maintainer evaluation",
    },
    {
        "name": "bug",
        "color": "d73a4a",
        "description": "Something isn't working",
    },
    {
        "name": "enhancement",
        "color": "a2eeef",
        "description": "New feature or request",
    },
    {
        "name": "documentation",
        "color": "0075ca",
        "description": "Improvements or additions to documentation",
    },
    {
        "name": "ready-for-agent",
        "color": "0e8a16",
        "description": "Ready for an agent to implement",
    },
]

# Universal Common Baseline Stages
COMMON_BASELINE_STAGES: List[Dict[str, Any]] = [
    {
        "name": "features",
        "enable": ["issues"],
    },
    {
        "name": "merge",
        "settings": {
            "allow_squash_merge": True,
            "allow_merge_commit": False,
            "allow_rebase_merge": False,
            "delete_branch_on_merge": True,
            "allow_update_branch": True,
        },
    },
    {
        "name": "actions-policy",
        "settings": {
            "default_workflow_permissions": "read",
        },
    },
    {
        "name": "security",
        "enable": ["vulnerability-alerts", "automated-security-fixes"],
    },
    {
        "name": "labels",
        "items": [dict(label) for label in CANONICAL_TRIAGE_LABELS],
    },
]


@dataclass(frozen=True)
class Profile:
    """Repository setup profile definition."""

    id: str
    display_name: str
    description: str
    tags: Tuple[str, ...] = field(default_factory=tuple)


# The 6 Canonical Setup Profiles
SCIENTIFIC_PYTHON = Profile(
    id="scientific-python",
    display_name="Scientific Python",
    description="Universal Baseline + Python test matrix with package caching (pip/uv), Git LFS, CodeQL",
    tags=("python", "matrix", "caching", "lfs", "codeql"),
)

HPC_CMAKE_MPI = Profile(
    id="hpc-cmake-mpi",
    display_name="HPC C++ / MPI",
    description="Universal Baseline + CMake/MPI with ccache, runner dependencies, rank 1-2 smoke tests, CodeQL",
    tags=("cmake", "mpi", "hpc", "ccache", "smoke-tests", "codeql"),
)

AGENT_READY_GOVERNANCE = Profile(
    id="agent-ready-governance",
    display_name="Agent-Ready Governance",
    description="Universal Baseline + Matt Pocock agent skills bundle, Documentation Freshness Audit, Issue State Gate",
    tags=("agents", "skills", "doc-freshness", "issue-state-gate"),
)

CONTAINERIZED_SCIENTIFIC = Profile(
    id="containerized-scientific",
    display_name="Containerized Scientific Application",
    description="Universal Baseline + detected CI + container build and publish workflow (container-publish.yml to GHCR) + Dependency Review",
    tags=("container", "docker", "ghcr", "dependency-review"),
)

FULL_SUITE = Profile(
    id="full-suite",
    display_name="Full Scientific CI/CD Suite",
    description="Universal Baseline + detected CI + Dependency Review + Container Publish + Agent Skills bundle + Doc Freshness + Issue State Gate",
    tags=("full-suite", "ci", "container", "dependency-review", "agent-skills", "issue-state-gate"),
)

MINIMAL_LEAN = Profile(
    id="minimal-lean",
    display_name="Minimal / Lean Governance",
    description="Universal Baseline only (no extra workflows or vendored skills)",
    tags=("minimal", "baseline", "lean"),
)

PROFILE_CATALOG: Dict[str, Profile] = {
    SCIENTIFIC_PYTHON.id: SCIENTIFIC_PYTHON,
    HPC_CMAKE_MPI.id: HPC_CMAKE_MPI,
    AGENT_READY_GOVERNANCE.id: AGENT_READY_GOVERNANCE,
    CONTAINERIZED_SCIENTIFIC.id: CONTAINERIZED_SCIENTIFIC,
    FULL_SUITE.id: FULL_SUITE,
    MINIMAL_LEAN.id: MINIMAL_LEAN,
}


def list_profiles() -> List[Profile]:
    """Return all 6 registered Setup Profiles."""
    return list(PROFILE_CATALOG.values())


def get_profile(profile_id: str) -> Profile:
    """Retrieve a Setup Profile by ID, raising ValueError if not found."""
    if profile_id not in PROFILE_CATALOG:
        raise ValueError(
            f"Unknown profile ID {profile_id!r}. Supported profiles: {list(PROFILE_CATALOG.keys())}"
        )
    return PROFILE_CATALOG[profile_id]


CONTAINER_FILE_MARKERS = {
    "Dockerfile",
    "Containerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
}

AGENT_FILE_MARKERS = {
    "AGENTS.md",
    "CLAUDE.md",
}

HPC_FILE_MARKERS = {
    "CMakeLists.txt",
    "Makefile",
    "configure.ac",
}


def recommend_profile(
    repo_dir: Optional[Union[str, Path]] = None,
    inventory: Optional[Dict[str, Any]] = None,
) -> Profile:
    """Inspect repository file markers and live inventory to recommend the best setup profile.

    Recommendation heuristics:
    1. If container markers are present:
       - If agent markers are also present -> Full Scientific CI/CD Suite
       - Otherwise -> Containerized Scientific Application
    2. If CMake / MPI / C++ markers are present or C/C++ is primary language -> HPC C++ / MPI
    3. If Python markers or Python is primary language -> Scientific Python
    4. If agent markers are present -> Agent-Ready Governance
    5. Default / Fallback -> Minimal / Lean Governance
    """
    path = Path(repo_dir) if repo_dir is not None else None
    has_dir = path is not None and path.is_dir()

    # Check for container indicators
    has_container = False
    if has_dir:
        has_container = any((path / marker).is_file() for marker in CONTAINER_FILE_MARKERS)

    # Check for agent indicators
    has_agents = False
    if has_dir:
        has_agents = (
            any((path / marker).is_file() for marker in AGENT_FILE_MARKERS)
            or (path / ".agents").is_dir()
            or (path / "docs" / "agents").is_dir()
        )

    # Check for CMake / MPI indicators
    has_cmake = False
    if has_dir:
        cmake_info = detect_cmake_mpi_project(workspace_dir=str(path))
        if cmake_info and cmake_info.get("detected"):
            has_cmake = True
        else:
            has_cmake = (path / "CMakeLists.txt").is_file()

    # Check for Python indicators
    has_python = False
    if has_dir:
        has_python = detect_python_project(root_dir=str(path))
        if not has_python:
            has_python = any((path / marker).is_file() for marker in PYTHON_INDICATOR_FILES)

    # Inspect live inventory if available
    if inventory and isinstance(inventory, dict):
        repo_data = inventory.get("repository", {})
        primary_lang = repo_data.get("language")
        langs = inventory.get("languages", {})
        if primary_lang == "Python" or "Python" in langs:
            has_python = True
        if primary_lang in ("C", "C++", "Fortran") or any(l in langs for l in ("C", "C++", "Fortran")):
            has_cmake = True
        if inventory.get("agent_skills") is True:
            has_agents = True

    # Archetype decision hierarchy
    if has_container:
        if has_agents:
            return FULL_SUITE
        return CONTAINERIZED_SCIENTIFIC

    if has_cmake:
        return HPC_CMAKE_MPI

    if has_python:
        return SCIENTIFIC_PYTHON

    if has_agents:
        return AGENT_READY_GOVERNANCE

    return MINIMAL_LEAN


def profile_to_policy(
    profile: Union[Profile, str],
    repository: Optional[str] = None,
    repo_dir: Optional[Union[str, Path]] = None,
    inventory: Optional[Dict[str, Any]] = None,
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate a complete valid Version 2 Policy document from a Setup Profile.

    Args:
        profile: Profile instance or profile ID string.
        repository: Optional target OWNER/REPO slug.
        repo_dir: Optional local repository path for fine-tuning options.
        inventory: Optional live repository inventory.
        options: Optional profile configuration overrides.

    Returns:
        Dict[str, Any] containing a validated Version 2 Policy document.
    """
    if isinstance(profile, str):
        profile_obj = get_profile(profile)
    elif isinstance(profile, Profile) or (hasattr(profile, "id") and hasattr(profile, "display_name")):
        profile_obj = profile
    else:
        raise TypeError(f"profile must be Profile or str, got {type(profile).__name__}")

    options = dict(options or {})
    stages: List[Dict[str, Any]] = [copy.deepcopy(s) for s in COMMON_BASELINE_STAGES]

    if profile_obj.id == "scientific-python":
        py_opts = options.get("python", {})
        versions = py_opts.get("versions") or ["3.11", "3.12"]
        pkg_manager = py_opts.get("package_manager") or "pip"
        commands = py_opts.get("commands") or ["pytest"]
        cache = py_opts.get("cache", True)
        lfs = py_opts.get("lfs", True)

        ci_stage: Dict[str, Any] = {
            "name": "ci-profile",
            "profile": "python",
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "python": {
                "versions": versions,
                "package_manager": pkg_manager,
                "cache": cache,
                "lfs": lfs,
                "commands": commands,
            },
        }
        stages.append(ci_stage)
        stages.append({"name": "codeql", "query_suite": "default"})

    elif profile_obj.id == "hpc-cmake-mpi":
        cmake_opts = options.get("cmake-mpi") or options.get("cmake_mpi") or {}
        ranks = cmake_opts.get("ranks") or [1, 2]
        ccache = cmake_opts.get("ccache", True)
        runner_deps = cmake_opts.get("runner_dependencies") or ["libopenmpi-dev", "openmpi-bin"]
        build_cmds = cmake_opts.get("build_commands") or ["cmake -B build -S .", "cmake --build build"]
        test_cmds = cmake_opts.get("test_commands") or ["ctest --test-dir build --output-on-failure"]

        ci_stage = {
            "name": "ci-profile",
            "profile": "cmake-mpi",
            "filename": "ci.yml",
            "triggers": ["pull_request"],
            "checkout_sha": DEFAULT_CHECKOUT_SHA,
            "cmake-mpi": {
                "ranks": ranks,
                "ccache": ccache,
                "runner_dependencies": runner_deps,
                "build_commands": build_cmds,
                "test_commands": test_cmds,
            },
        }
        stages.append(ci_stage)
        stages.append({"name": "codeql", "query_suite": "default"})

    elif profile_obj.id == "agent-ready-governance":
        stages.append({"name": "agent-skills", "bundle": "mattpocock"})
        stages.append({"name": "issue-state-gate"})

    elif profile_obj.id == "containerized-scientific":
        # Check if python or cmake detected for CI
        is_python = detect_python_project(root_dir=str(repo_dir)) if repo_dir else False
        is_cmake = (
            detect_cmake_mpi_project(workspace_dir=str(repo_dir)).get("detected", False)
            if repo_dir
            else False
        )
        if inventory and isinstance(inventory, dict):
            lang = inventory.get("repository", {}).get("language")
            if lang == "Python":
                is_python = True
            elif lang in ("C", "C++", "Fortran"):
                is_cmake = True

        if is_cmake:
            stages.append({
                "name": "ci-profile",
                "profile": "cmake-mpi",
                "filename": "ci.yml",
                "triggers": ["pull_request"],
                "checkout_sha": DEFAULT_CHECKOUT_SHA,
                "cmake-mpi": {
                    "ranks": [1, 2],
                    "ccache": True,
                    "runner_dependencies": ["libopenmpi-dev", "openmpi-bin"],
                    "build_commands": ["cmake -B build -S .", "cmake --build build"],
                    "test_commands": ["ctest --test-dir build --output-on-failure"],
                },
            })
        elif is_python:
            stages.append({
                "name": "ci-profile",
                "profile": "python",
                "filename": "ci.yml",
                "triggers": ["pull_request"],
                "checkout_sha": DEFAULT_CHECKOUT_SHA,
                "python": {
                    "versions": ["3.11", "3.12"],
                    "package_manager": "pip",
                    "cache": True,
                    "lfs": False,
                    "commands": ["pytest"],
                },
            })

        workflows_list = [
            {
                "template": "container-publish",
                "template_version": 1,
                "filename": "container-publish.yml",
                "triggers": ["pull_request"],
                "checkout_sha": DEFAULT_CHECKOUT_SHA,
                "login_action_sha": DEFAULT_CONTAINER_LOGIN_SHA,
                "build_action_sha": DEFAULT_CONTAINER_BUILD_SHA,
            },
            {
                "template": "dependency-review",
                "template_version": 1,
                "filename": "dependency-review.yml",
                "triggers": ["pull_request"],
                "checkout_sha": DEFAULT_CHECKOUT_SHA,
                "action_sha": DEFAULT_DEPENDENCY_SHA,
                "fail_on_severity": "high",
                "allow_licenses": "MIT, Apache-2.0, BSD-3-Clause, BSD-2-Clause, ISC, Python-2.0",
            },
        ]
        stages.append({
            "name": "workflows",
            "workflows": workflows_list,
        })

    elif profile_obj.id == "full-suite":
        is_python = detect_python_project(root_dir=str(repo_dir)) if repo_dir else False
        is_cmake = (
            detect_cmake_mpi_project(workspace_dir=str(repo_dir)).get("detected", False)
            if repo_dir
            else False
        )
        if inventory and isinstance(inventory, dict):
            lang = inventory.get("repository", {}).get("language")
            if lang == "Python":
                is_python = True
            elif lang in ("C", "C++", "Fortran"):
                is_cmake = True

        if is_cmake:
            stages.append({
                "name": "ci-profile",
                "profile": "cmake-mpi",
                "filename": "ci.yml",
                "triggers": ["pull_request"],
                "checkout_sha": DEFAULT_CHECKOUT_SHA,
                "cmake-mpi": {
                    "ranks": [1, 2],
                    "ccache": True,
                    "runner_dependencies": ["libopenmpi-dev", "openmpi-bin"],
                    "build_commands": ["cmake -B build -S .", "cmake --build build"],
                    "test_commands": ["ctest --test-dir build --output-on-failure"],
                },
            })
        elif is_python:
            stages.append({
                "name": "ci-profile",
                "profile": "python",
                "filename": "ci.yml",
                "triggers": ["pull_request"],
                "checkout_sha": DEFAULT_CHECKOUT_SHA,
                "python": {
                    "versions": ["3.11", "3.12"],
                    "package_manager": "pip",
                    "cache": True,
                    "lfs": False,
                    "commands": ["pytest"],
                },
            })

        workflows_list = [
            {
                "template": "container-publish",
                "template_version": 1,
                "filename": "container-publish.yml",
                "triggers": ["pull_request"],
                "checkout_sha": DEFAULT_CHECKOUT_SHA,
                "login_action_sha": DEFAULT_CONTAINER_LOGIN_SHA,
                "build_action_sha": DEFAULT_CONTAINER_BUILD_SHA,
            },
            {
                "template": "dependency-review",
                "template_version": 1,
                "filename": "dependency-review.yml",
                "triggers": ["pull_request"],
                "checkout_sha": DEFAULT_CHECKOUT_SHA,
                "action_sha": DEFAULT_DEPENDENCY_SHA,
                "fail_on_severity": "high",
                "allow_licenses": "MIT, Apache-2.0, BSD-3-Clause, BSD-2-Clause, ISC, Python-2.0",
            },
        ]
        stages.append({
            "name": "workflows",
            "workflows": workflows_list,
        })
        stages.append({"name": "agent-skills", "bundle": "mattpocock"})
        stages.append({"name": "issue-state-gate"})

    elif profile_obj.id == "minimal-lean":
        pass  # Universal baseline only

    policy: Dict[str, Any] = {
        "version": 2,
        "stages": stages,
    }
    if repository is not None:
        policy["repository"] = repository

    validate_policy(policy)
    return policy
