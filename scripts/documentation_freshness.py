#!/usr/bin/env python3
"""Validate required documentation, skill provenance, and relative Markdown links."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

try:
    from scripts.check_markdown_links import audit
except ImportError:
    from check_markdown_links import audit


CANONICAL_DOCS = (
    "README.md", "GLOSSARY.md", "docs/agents/issue-tracker.md",
    "docs/agents/domain.md", "docs/agents/automation.md", "docs/policy-reference.md",
    "docs/workflows.md", "docs/agent-skills.md",
    "docs/adr/0002-one-setup-pull-request-for-selected-workflows.md",
)
PROVENANCE = "https://github.com/mattpocock/skills"
TARGET_DOCS_CONFIG = ".github/documentation-freshness.json"
TARGET_DOCS_CONFIG_MARKER = "github-repo-setup/documentation-freshness/v1"
PINNED_INVENTORY = ".github/documentation-skill-inventory.json"
PINNED_INVENTORY_SHA256 = "6fdc34eb6c3c67a9751225c31de2a6ba3ca1d61a2607530382149c775bbd4723"
PINNED_LICENSE_SHA256 = "0e7ac423bf2c6e223b7c5b156f8cf72da49d748e56a1641402c31f22ad07dbb5"
TARGET_DOCS_CONFIG_CONTENT = {
    "schema": TARGET_DOCS_CONFIG_MARKER,
    "managed_by": "github-repo-setup",
    "paths": ["docs/documentation-freshness.md"],
}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def _safe_relative(value):
    return (isinstance(value, str) and value and "\\" not in value and "\x00" not in value and
            not value.startswith("/") and all(part not in ("", ".", "..") for part in value.split("/")))


def _installed_manifest(root):
    path = root / ".agents/skills/manifest.json"
    if not path.exists():
        return []
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("installed agent skills manifest is unreadable or invalid JSON") from None
    required = {"schema_version", "managed_by", "bundle", "source_repository", "source_commit",
                "source_version", "license", "files"}
    if (not isinstance(manifest, dict) or set(manifest) != required or
            type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1 or
            manifest["managed_by"] not in ("sciflow-cicd", "github-repo-setup") or manifest["bundle"] != "mattpocock" or
            manifest["source_repository"] != PROVENANCE or not isinstance(manifest["source_commit"], str) or
            not COMMIT.fullmatch(manifest["source_commit"]) or not isinstance(manifest["source_version"], str) or
            not manifest["source_version"] or manifest["license"] != "MIT" or
            not isinstance(manifest["files"], dict) or not manifest["files"]):
        raise ValueError("installed agent skills provenance or manifest schema is invalid")
    inventory_path = root / PINNED_INVENTORY
    if inventory_path.is_symlink() or not inventory_path.is_file():
        raise ValueError("pinned skill provenance inventory is missing or unsafe")
    inventory_bytes = inventory_path.read_bytes()
    if hashlib.sha256(inventory_bytes).hexdigest() != PINNED_INVENTORY_SHA256:
        raise ValueError("pinned skill provenance inventory hash mismatch")
    try:
        inventory = json.loads(inventory_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("pinned skill provenance inventory is malformed") from None
    if (not isinstance(inventory, dict) or inventory.get("source_repository") != PROVENANCE or
            inventory.get("source_commit") != manifest["source_commit"] or
            inventory.get("source_version") != manifest["source_version"] or
            inventory.get("license") != "MIT" or not isinstance(inventory.get("files"), list)):
        raise ValueError("installed agent skills provenance does not match the pinned inventory")
    expected = {item.get("destination"): item.get("sha256") for item in inventory["files"]
                if isinstance(item, dict) and isinstance(item.get("destination"), str)}
    expected["LICENSE"] = PINNED_LICENSE_SHA256
    if manifest["files"] != expected:
        raise ValueError("installed agent skills manifest differs from the pinned inventory")
    declared = {}
    for relative, digest in manifest["files"].items():
        if not _safe_relative(relative) or not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError("installed agent skills manifest has an unsafe path or hash")
        target = root / ".agents/skills" / relative
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"installed agent skills file is missing or unsafe: {relative}")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != digest:
            raise ValueError(f"installed agent skills provenance hash mismatch: {relative}")
        declared[relative] = target
    actual_files = {path.relative_to(root / ".agents/skills").as_posix()
                    for path in (root / ".agents/skills").rglob("*") if path.is_file() and path.name != "manifest.json"}
    if actual_files != set(declared):
        raise ValueError("installed agent skills files do not match the manifest inventory")
    return list(declared.values())


def selected_markdown_files(root, *, target=False):
    """Return exactly the Markdown inventory this audit mode may inspect."""
    root = Path(root).resolve()
    if not target:
        return sorted(path for path in root.rglob("*.md")
                      if not any(part in {".git", ".venv", "venv", "node_modules", "__pycache__"}
                                 for part in path.relative_to(root).parts))
    files = [path for path in _installed_manifest(root) if path.suffix.lower() == ".md"]
    config_path = root / TARGET_DOCS_CONFIG
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError("target documentation scope configuration is invalid JSON") from None
        if (not isinstance(config, dict) or set(config) != {"schema", "managed_by", "paths"} or
                config.get("schema") not in (TARGET_DOCS_CONFIG_MARKER, "sciflow-cicd/documentation-freshness/v1") or
                config.get("managed_by") not in ("sciflow-cicd", "github-repo-setup") or not isinstance(config.get("paths"), list) or
                len(config["paths"]) > 100):
            raise ValueError("target documentation scope configuration is malformed")
        seen = set()
        for relative in config["paths"]:
            if not _safe_relative(relative) or not relative.endswith(".md") or relative in seen:
                raise ValueError("target documentation scope contains an unsafe or duplicate Markdown path")
            seen.add(relative)
            target_path = root / relative
            if target_path.is_symlink() or not target_path.is_file():
                raise ValueError(f"declared target documentation is missing or unsafe: {relative}")
            files.append(target_path)
    return sorted(set(files))


def audit_repository(root, *, target=False):
    """Return bounded ``(path, line, code, message)`` findings."""
    root = Path(root).resolve()
    findings = []
    if not target:
        for relative in CANONICAL_DOCS:
            if not (root / relative).is_file():
                findings.append((relative, 0, "missing-canonical-doc", "Required canonical documentation file is missing"))
        try:
            try:
                from scripts.provision_agent_skills import _verified_files
            except ImportError:
                from provision_agent_skills import _verified_files
            snapshot = root / "data/agent-skills/mattpocock"
            _verified_files(snapshot / "snapshot", snapshot / "manifest.json")
        except (ImportError, OSError, ValueError):
            findings.append(("data/agent-skills/mattpocock/manifest.json", 0, "invalid-skill-provenance",
                             "Pinned skill bundle manifest, inventory, or source hashes are invalid"))
    markdown_files = selected_markdown_files(root, target=target)
    errors = audit(root, markdown_files=markdown_files)
    for source, line, target_path, reason in errors:
        findings.append((source.relative_to(root).as_posix(), line, "broken-relative-link",
                         f"Link {target_path!r}: {reason}"))
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--target", action="store_true", help="audit only declared/installed target documentation")
    args = parser.parse_args(argv)
    try:
        findings = audit_repository(args.root, target=args.target)
    except (OSError, ValueError) as error:
        print(f"Documentation audit failed: {error}", file=sys.stderr)
        return 2
    if findings:
        for path, line, code, message in findings:
            print(f"{path}:{line}: [{code}] {message}", file=sys.stderr)
        print(f"Documentation audit found {len(findings)} issue(s).", file=sys.stderr)
        return 1
    print("Documentation freshness audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
