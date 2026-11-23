"""Verification of agent runtime skill discovery in disposable targets.

This domain module provisions an inert snapshot of vendored skills into a
disposable target directory, verifies static discovery contracts (UTF-8
encoding, entrypoint presence, YAML frontmatter syntax, required name and
description fields), and probes for an installed agent runtime CLI (such as
Antigravity or Claude Code).

If a live agent CLI is available, it executes runtime discovery against the
target workspace and records live evidence. If the runtime is unavailable, it
cleanly detects absence, does not fake success, and produces a structured report
marking live execution as Unverified per repository evidence rules.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

try:
    from scripts.path_security import reject_symlink_components
    from scripts.provision_agent_skills import (
        _load_manifest,
        load_verified_catalog,
        provision_skills,
    )
except ImportError:
    from path_security import reject_symlink_components
    from provision_agent_skills import (
        _load_manifest,
        load_verified_catalog,
        provision_skills,
    )

SKILL_FILENAME: str = "SKILL.md"
NAME_PATTERN: re.Pattern = re.compile(r"^[a-z0-9][a-z0-9-]*\Z")
FRONTMATTER_DELIMITER: str = "---"
CANDIDATE_RUNTIMES: Tuple[str, ...] = ("antigravity", "agy", "claude", "gemini")


class SkillDiscoveryError(ValueError):
    """Raised when skill discovery or validation fails."""


def _reject_symlink_components(path: Union[str, Path], *, include_leaf: bool = True) -> None:
    """Reject existing symlinks in path or ancestors without resolving through them."""
    reject_symlink_components(
        path,
        include_leaf=include_leaf,
        error_cls=SkillDiscoveryError,
        message_format="symlink is not allowed in path: {piece}",
    )


@dataclass(frozen=True)
class DiscoveredSkill:
    """Metadata and validation result for a discovered workspace skill."""

    skill_id: str
    name: str
    description: str
    relative_path: str
    file_size: int
    sha256: str
    metadata: Dict[str, Any]


@dataclass(frozen=True)
class RuntimeProbeResult:
    """Result of probing and optionally executing an agent CLI runtime."""

    runtime_bin: Optional[str]
    available: bool
    version: Optional[str]
    command: Optional[str]
    exit_status: Optional[int]
    stdout: Optional[str]
    stderr: Optional[str]
    # True only when a skill-listing command exited 0 and its output names every
    # expected installed skill. A bare binary or a --help fallback never counts.
    discovery_verified: bool = False
    missing_ids: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkillDiscoveryReport:
    """Comprehensive verification report conforming to repository evidence standards."""

    target_dir: str
    is_disposable: bool
    skills: List[DiscoveredSkill]
    manifest_match: bool
    manifest_expected: List[str]
    manifest_missing: List[str]
    manifest_unexpected: List[str]
    runtime_probe: RuntimeProbeResult
    errors: List[str]

    @property
    def is_clean(self) -> bool:
        """Return True if static skill discovery succeeded without errors or parity mismatches."""
        return len(self.errors) == 0 and self.manifest_match

    def to_dict(self) -> Dict[str, Any]:
        """Convert report to JSON-serializable dictionary."""
        return {
            "target_dir": self.target_dir,
            "is_disposable": self.is_disposable,
            "is_clean": self.is_clean,
            "skills_count": len(self.skills),
            "skills": [asdict(s) for s in self.skills],
            "manifest_match": self.manifest_match,
            "manifest_expected": self.manifest_expected,
            "manifest_missing": self.manifest_missing,
            "manifest_unexpected": self.manifest_unexpected,
            "runtime_probe": asdict(self.runtime_probe),
            "errors": self.errors,
        }

    def format_evidence_report(self, command_line: Optional[str] = None) -> str:
        """Format report following Observed / Inferred / Unverified evidence standard."""
        cmd = command_line or f"python3 scripts/verify_agent_skill_discovery.py --target-dir {self.target_dir}"
        lines: List[str] = [
            "# Agent Runtime Skill Discovery Verification Report",
            "",
            "## Summary",
            f"- Target directory: `{self.target_dir}`" + (" (disposable)" if self.is_disposable else ""),
            f"- Discovered skills: {len(self.skills)}",
            f"- Catalog manifest parity: {'Verified' if self.manifest_match else 'Mismatch'}",
            f"- Agent runtime CLI: {'Available' if self.runtime_probe.available else 'Unavailable'}",
            f"- Runtime skill discovery: {'Verified' if self.runtime_probe.discovery_verified else 'Unverified'}",
            "",
            "### Observed",
            f"- Command: `{cmd}`",
            f"- Input: Target directory containing `.agents/skills/` ({len(self.skills)} skills discovered)",
            f"- Environment: Python {platform.python_version()} on {platform.system().lower()} ({platform.machine()})",
            f"- Static audit result: {'clean' if self.is_clean else 'not clean'} (report-level result, not a runtime exit status)",
            "- Output:",
            f"  - Discovered {len(self.skills)} skill entrypoint(s) matching `{SKILL_FILENAME}`.",
        ]

        if self.errors:
            lines.append("  - Discovery validation errors:")
            for err in self.errors:
                lines.append(f"    - {err}")
        else:
            lines.append("  - All discovered entrypoints verified: valid UTF-8 encoding, non-empty, and valid YAML frontmatter.")
            lines.append("  - Required frontmatter fields present on all skills: `name` (lowercase hyphenated identifier) and `description` (non-empty string).")

        if self.manifest_expected:
            if self.manifest_match:
                lines.append(f"  - Catalog parity verified: all {len(self.manifest_expected)} expected skills match discovered skills exactly.")
            else:
                if self.manifest_missing:
                    lines.append(f"  - Missing expected skills: {', '.join(sorted(self.manifest_missing))}")
                if self.manifest_unexpected:
                    lines.append(f"  - Unexpected skills found: {', '.join(sorted(self.manifest_unexpected))}")

        if self.runtime_probe.available:
            lines.append(f"  - Agent runtime CLI binary detected: `{self.runtime_probe.runtime_bin}`")
            if self.runtime_probe.version:
                lines.append(f"    - Version: {self.runtime_probe.version}")
            if self.runtime_probe.command:
                lines.append(f"    - Runtime command: `{self.runtime_probe.command}`")
                lines.append(f"    - Runtime command exit status: {self.runtime_probe.exit_status}")
                if self.runtime_probe.stdout:
                    lines.append("    - Command stdout:")
                    for out_line in self.runtime_probe.stdout.strip().splitlines()[:20]:
                        lines.append(f"        {out_line}")
                if self.runtime_probe.stderr:
                    lines.append("    - Command stderr:")
                    for err_line in self.runtime_probe.stderr.strip().splitlines()[:10]:
                        lines.append(f"        {err_line}")
        else:
            lines.append("  - Live agent runtime CLI probe: No supported CLI runtime binary found in PATH.")

        lines.extend([
            "",
            "### Inferred",
            f"- Reasoning: All {len(self.skills)} discovered skills conform to the workspace skill standard (`.agents/skills/**/SKILL.md` with YAML frontmatter).",
            "- The skill files satisfy the static contract checked above; no claim is made about how any runtime indexes them unless runtime discovery is listed as Verified.",
            "- Uncertainty: Token consumption during runtime progressive disclosure, dynamic activation heuristics, and tool execution boundaries depend on host agent runtime configuration.",
            "",
            "### Unverified",
        ])

        probe = self.runtime_probe
        if probe.discovery_verified:
            lines.append("- None: the runtime skill-listing command exited 0 and named every installed skill.")
        elif probe.available:
            lines.append("- Runtime skill discovery is UNVERIFIED: the runtime binary is present but no skill-listing command output named all installed skills.")
            if probe.command and probe.command.endswith("--help"):
                lines.append("- The `--help` fallback shows only that the binary runs; it is not discovery evidence.")
            if probe.missing_ids:
                shown = ", ".join(probe.missing_ids[:10])
                more = f" (+{len(probe.missing_ids) - 10} more)" if len(probe.missing_ids) > 10 else ""
                lines.append(f"- Skills not named in runtime output: {shown}{more}")
            lines.append("- Missing check: a runtime command that lists skills discovered from the target workspace.")
        else:
            lines.append("- Live agent runtime CLI execution is UNVERIFIED.")
            lines.append(f"- Missing check: Execution of live agent CLI runtime discovery command against the target workspace.")
            lines.append(f"- Required environment: An installed and configured agent CLI runtime in PATH (searched candidates: {', '.join(CANDIDATE_RUNTIMES)}).")

        return "\n".join(lines)


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter delimited by '---' from Markdown text.

    Returns a tuple of (frontmatter_dict, markdown_body).
    Raises SkillDiscoveryError if frontmatter is missing, unclosed, or invalid.
    """
    if content.startswith("\ufeff"):
        content = content.lstrip("\ufeff")
    if not content.startswith(FRONTMATTER_DELIMITER):
        raise SkillDiscoveryError("Skill file must begin with YAML frontmatter delimiter '---'")
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        raise SkillDiscoveryError("Skill file must begin with '---' on the first line")

    end_idx = -1
    for idx in range(1, len(lines)):
        if lines[idx].strip() == FRONTMATTER_DELIMITER:
            end_idx = idx
            break

    if end_idx == -1:
        raise SkillDiscoveryError("Unclosed YAML frontmatter: missing terminating '---'")

    fm_raw = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1:])

    try:
        import yaml  # type: ignore
        parsed = yaml.safe_load(fm_raw)
        if not isinstance(parsed, dict):
            raise SkillDiscoveryError("Frontmatter must be a YAML mapping")
        return parsed, body
    except ImportError:
        pass
    except SkillDiscoveryError:
        raise
    except Exception as error:
        raise SkillDiscoveryError(f"Malformed YAML frontmatter: {error}") from error

    # Shared deterministic YAML subset parser as the fallback.
    try:
        from scripts.yaml_subset import parse_yaml_subset as _shared_parse
    except ImportError:
        from yaml_subset import parse_yaml_subset as _shared_parse
    try:
        parsed = _shared_parse(fm_raw, strict=True)
    except ValueError as error:
        raise SkillDiscoveryError(f"Malformed YAML frontmatter: {error}") from error
    if not isinstance(parsed, dict):
        raise SkillDiscoveryError("Frontmatter must be a YAML mapping")
    return parsed, body


def validate_skill_file(skill_path: Path, skills_root: Path) -> DiscoveredSkill:
    """Validate a single SKILL.md file and return its DiscoveredSkill metadata."""
    _reject_symlink_components(skill_path)
    if not skill_path.is_file():
        raise SkillDiscoveryError(f"Skill entrypoint is not a regular file: {skill_path}")

    try:
        content_bytes = skill_path.read_bytes()
    except OSError as error:
        raise SkillDiscoveryError(f"Cannot read skill file: {skill_path}") from error

    if not content_bytes.strip():
        raise SkillDiscoveryError(f"Skill file is empty: {skill_path}")

    try:
        text = content_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SkillDiscoveryError(f"Skill entrypoint is not valid UTF-8: {skill_path}") from error

    fm, _ = parse_frontmatter(text)

    name = fm.get("name")
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise SkillDiscoveryError(
            f"Invalid or missing 'name' in frontmatter: {skill_path} (must be lowercase hyphenated identifier)"
        )

    description = fm.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillDiscoveryError(
            f"Invalid or missing 'description' in frontmatter: {skill_path} (must be non-empty string)"
        )

    rel_path = skill_path.relative_to(skills_root)
    # Derive skill_id from directory containing SKILL.md
    skill_dir_rel = rel_path.parent.as_posix()
    skill_id = skill_dir_rel if skill_dir_rel and skill_dir_rel != "." else name

    extra_metadata = {k: v for k, v in fm.items() if k not in ("name", "description")}

    return DiscoveredSkill(
        skill_id=skill_id,
        name=name,
        description=description.strip(),
        relative_path=rel_path.as_posix(),
        file_size=len(content_bytes),
        sha256=hashlib.sha256(content_bytes).hexdigest(),
        metadata=extra_metadata,
    )


def resolve_skills_root(target_dir: Path) -> Path:
    """Determine the skills root directory within target_dir."""
    target_dir = Path(os.path.abspath(target_dir))
    _reject_symlink_components(target_dir)

    agents_skills = target_dir / ".agents" / "skills"
    if agents_skills.is_dir():
        return agents_skills

    if target_dir.name == "skills" and target_dir.parent.name == ".agents":
        return target_dir

    skills_sub = target_dir / "skills"
    if skills_sub.is_dir():
        return skills_sub

    return target_dir


def discover_workspace_skills(target_dir: Union[str, Path]) -> Tuple[Path, List[DiscoveredSkill], List[str]]:
    """Scan workspace for installed skills and validate discovery contracts.

    Returns (skills_root, discovered_skills, errors).
    """
    target_path = Path(os.path.abspath(target_dir))
    skills_root = resolve_skills_root(target_path)
    _reject_symlink_components(skills_root)

    discovered: List[DiscoveredSkill] = []
    errors: List[str] = []

    if not skills_root.exists() or not skills_root.is_dir():
        errors.append(f"Skills directory does not exist: {skills_root}")
        return skills_root, discovered, errors

    skill_files: List[Path] = []
    for root, dirs, files in os.walk(skills_root):
        # Prune hidden or ignored directories
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "node_modules")]
        for file in files:
            if file == SKILL_FILENAME:
                full_path = Path(root) / file
                skill_files.append(full_path)

    for skill_path in sorted(skill_files):
        try:
            skill = validate_skill_file(skill_path, skills_root)
            discovered.append(skill)
        except (SkillDiscoveryError, ValueError) as error:
            errors.append(str(error))

    # Check for duplicate skill IDs
    seen_ids: Set[str] = set()
    for skill in discovered:
        if skill.skill_id in seen_ids:
            errors.append(f"Duplicate skill ID discovered: {skill.skill_id}")
        seen_ids.add(skill.skill_id)

    return skills_root, sorted(discovered, key=lambda s: s.skill_id), errors


def probe_and_execute_runtime(
    target_dir: Path,
    custom_bin: Optional[str] = None,
    expected_skill_ids: Optional[Sequence[str]] = None,
) -> RuntimeProbeResult:
    """Probe for agent runtime CLI and execute discovery command if present.

    Discovery is marked verified only when ``<bin> skills list`` exits 0 and its
    output names every id in ``expected_skill_ids``. Falling back to ``--help``
    records that the binary runs, not that it discovered the skills.
    """
    candidates: Sequence[str]
    if custom_bin:
        candidates = (custom_bin,)
    else:
        candidates = CANDIDATE_RUNTIMES

    runtime_bin: Optional[str] = None
    for candidate in candidates:
        candidate_path = shutil.which(candidate)
        if candidate_path:
            runtime_bin = candidate_path
            break
        as_path = Path(candidate)
        if as_path.is_file() and os.access(as_path, os.X_OK):
            runtime_bin = str(as_path.resolve())
            break

    if not runtime_bin:
        return RuntimeProbeResult(
            runtime_bin=None,
            available=False,
            version=None,
            command=None,
            exit_status=None,
            stdout=None,
            stderr=None,
        )

    # Runtime detected: extract version
    version_str: Optional[str] = None
    try:
        ver_proc = subprocess.run(
            [runtime_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if ver_proc.returncode == 0:
            version_str = ver_proc.stdout.strip() or ver_proc.stderr.strip()
    except Exception:
        version_str = None

    # Execute discovery command against target workspace
    expected = sorted(set(expected_skill_ids or ()))
    discovery_cmd = [runtime_bin, "skills", "list"]
    proc = _run_probe_command(discovery_cmd, target_dir)

    verified = False
    missing: List[str] = list(expected)
    if proc is not None and proc.returncode == 0 and expected:
        output = proc.stdout or ""
        missing = [sid for sid in expected if not _output_names_skill(output, sid)]
        verified = not missing

    if proc is None or proc.returncode != 0:
        # Record that the binary at least runs; this is never discovery evidence.
        fallback_cmd = [runtime_bin, "--help"]
        fallback = _run_probe_command(fallback_cmd, target_dir)
        if proc is None or (fallback is not None and fallback.returncode == 0):
            discovery_cmd = fallback_cmd
            proc = fallback
        verified = False
        missing = list(expected)

    if proc is None:
        return RuntimeProbeResult(
            runtime_bin=runtime_bin,
            available=True,
            version=version_str,
            command=" ".join(discovery_cmd),
            exit_status=None,
            stdout=None,
            stderr="runtime command could not be executed or timed out",
            discovery_verified=False,
            missing_ids=missing,
        )

    return RuntimeProbeResult(
        runtime_bin=runtime_bin,
        available=True,
        version=version_str,
        command=" ".join(discovery_cmd),
        exit_status=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        discovery_verified=verified,
        missing_ids=missing,
    )


def _run_probe_command(
    command: Sequence[str], target_dir: Path
) -> Optional["subprocess.CompletedProcess[str]"]:
    """Run a probe command in the target; return None if it cannot run or times out."""
    try:
        return subprocess.run(
            list(command),
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None


def _output_names_skill(output: str, skill_id: str) -> bool:
    """Return True when output mentions the skill id or its final path segment."""
    names = {skill_id, skill_id.rsplit("/", 1)[-1]}
    return any(
        re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", output) for name in names
    )

def verify_disposable_skill_discovery(
    target_dir: Optional[Union[str, Path]] = None,
    catalog_snapshot: Optional[Union[str, Path]] = None,
    catalog_manifest: Optional[Union[str, Path]] = None,
    runtime_bin: Optional[str] = None,
) -> SkillDiscoveryReport:
    """Perform end-to-end skill discovery verification in disposable target directory."""
    repo_root = Path(__file__).resolve().parents[1]
    if catalog_manifest is None:
        catalog_manifest = repo_root / "data/agent-skills/mattpocock/manifest.json"
    if catalog_snapshot is None:
        catalog_snapshot = repo_root / "data/agent-skills/mattpocock/snapshot"

    catalog_manifest_path = Path(os.path.abspath(catalog_manifest))
    catalog_snapshot_path = Path(os.path.abspath(catalog_snapshot))

    expected_skills: List[str] = []
    manifest_errors: List[str] = []
    if not catalog_manifest_path.is_file():
        manifest_errors.append(f"Catalog manifest not found: {catalog_manifest_path}")
    else:
        try:
            m_data, _ = _load_manifest(catalog_manifest_path)
            supported = m_data.get("supported_skills")
            if not isinstance(supported, list) or not all(isinstance(i, str) for i in supported):
                manifest_errors.append(
                    f"Catalog manifest has no valid 'supported_skills' list: {catalog_manifest_path}"
                )
            elif not supported:
                manifest_errors.append(
                    f"Catalog manifest lists no supported skills: {catalog_manifest_path}"
                )
            else:
                expected_skills = sorted(supported)
        except Exception as error:
            manifest_errors.append(
                f"Catalog manifest unreadable: {catalog_manifest_path}: {error}"
            )

    is_disposable = False
    temp_obj: Optional[tempfile.TemporaryDirectory] = None

    if target_dir is None:
        is_disposable = True
        temp_obj = tempfile.TemporaryDirectory()
        target_path = Path(temp_obj.name)
        # Provision full verified bundle into disposable target
        provision_skills(
            catalog_snapshot_path,
            catalog_manifest_path,
            target_path,
            target_manifest=True,
        )
    else:
        target_path = Path(os.path.abspath(target_dir))

    try:
        skills_root, skills, errors = discover_workspace_skills(target_path)

        discovered_ids = {s.skill_id for s in skills}
        expected_set = set(expected_skills)

        manifest_missing = sorted(expected_set - discovered_ids)
        manifest_unexpected = sorted(discovered_ids - expected_set) if expected_skills else []
        # Fail closed: parity is only a match when a valid manifest was read.
        manifest_match = (
            not manifest_errors
            and len(manifest_missing) == 0
            and len(manifest_unexpected) == 0
        )

        runtime_probe = probe_and_execute_runtime(
            target_path,
            custom_bin=runtime_bin,
            expected_skill_ids=sorted(discovered_ids),
        )

        return SkillDiscoveryReport(
            target_dir=str(target_path),
            is_disposable=is_disposable,
            skills=skills,
            manifest_match=manifest_match,
            manifest_expected=expected_skills,
            manifest_missing=manifest_missing,
            manifest_unexpected=manifest_unexpected,
            runtime_probe=runtime_probe,
            errors=errors + manifest_errors,
        )
    finally:
        if temp_obj is not None:
            temp_obj.cleanup()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint for skill discovery verification."""
    parser = argparse.ArgumentParser(
        description="Verify agent runtime skill discovery in disposable target."
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=None,
        help="Target directory to audit (default: provisions disposable temporary directory)",
    )
    parser.add_argument(
        "--catalog-snapshot",
        type=Path,
        default=None,
        help="Path to skill catalog snapshot directory",
    )
    parser.add_argument(
        "--catalog-manifest",
        type=Path,
        default=None,
        help="Path to skill catalog manifest.json",
    )
    parser.add_argument(
        "--runtime-bin",
        type=str,
        default=None,
        help="Custom agent runtime binary name or executable path to probe",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format: text (evidence report) or json (default: text)",
    )
    args = parser.parse_args(argv)

    cmd_line = f"python3 scripts/verify_agent_skill_discovery.py"
    if args.target_dir:
        cmd_line += f" --target-dir {args.target_dir}"
    if args.runtime_bin:
        cmd_line += f" --runtime-bin {args.runtime_bin}"

    report = verify_disposable_skill_discovery(
        target_dir=args.target_dir,
        catalog_snapshot=args.catalog_snapshot,
        catalog_manifest=args.catalog_manifest,
        runtime_bin=args.runtime_bin,
    )

    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.format_evidence_report(command_line=cmd_line))

    return 0 if report.is_clean else 1


if __name__ == "__main__":
    sys.exit(main())
