"""Safely copy the reviewed Matt Pocock skill snapshot into a destination tree.

Skill files are treated as inert repository data. This module never imports or
executes content from the snapshot.
"""

import hashlib
import json
import os
from pathlib import Path
import re

try:
    from scripts.path_security import reject_symlink_components, safe_relative_path
except ImportError:  # pragma: no cover
    from path_security import reject_symlink_components, safe_relative_path

try:
    from scripts.agent_skill_upgrade import classify_catalog_upgrade, validate_removal_acknowledgment
except ImportError:  # pragma: no cover
    from agent_skill_upgrade import classify_catalog_upgrade, validate_removal_acknowledgment

try:
    from scripts.agent_skill_id import skill_id_from_path
except ImportError:  # pragma: no cover
    from agent_skill_id import skill_id_from_path


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SKILL_ID = re.compile(r"(?:engineering|productivity)/[a-z0-9][a-z0-9-]*\Z")
_MANIFEST_KEYS = {
    "schema_version", "source_repository", "source_commit", "source_version",
    "license", "supported_skills", "files",
}
_TARGET_MANIFEST_KEYS = {
    "schema_version", "managed_by", "bundle", "source_repository",
    "source_commit", "source_version", "license", "files",
}


def _safe_relative_path(value, label):
    return safe_relative_path(value, label, error_cls=ValueError)


def _reject_symlink_components(path, *, include_leaf=True):
    """Reject existing symlinks without resolving through them."""
    reject_symlink_components(
        path,
        include_leaf=include_leaf,
        error_cls=ValueError,
        message_format="symlink is not allowed in path: {piece}",
    )


def _validate_license(license_path, declared_license="MIT"):
    license_path = Path(license_path)
    _reject_symlink_components(license_path)
    if not license_path.exists():
        raise ValueError(f"skill catalog license file is missing: {license_path}")
    if not license_path.is_file():
        raise ValueError(f"skill catalog license is not a regular file: {license_path}")
    try:
        content = license_path.read_bytes()
    except OSError as error:
        raise ValueError(f"skill catalog license is unreadable: {license_path}") from error
    if not content.strip():
        raise ValueError("skill catalog license is empty")
    text = content.decode("utf-8", errors="replace")
    if declared_license == "MIT" and ("MIT License" not in text and "MIT" not in text):
        raise ValueError("skill catalog license content does not match declared license")
    return content


def _load_manifest(manifest_path):
    manifest_path = Path(manifest_path)
    _reject_symlink_components(manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("skill snapshot manifest is unreadable or invalid JSON") from error
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise ValueError("skill snapshot manifest has malformed fields")
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["source_repository"] != "https://github.com/mattpocock/skills"
            or not isinstance(manifest["source_version"], str) or not manifest["source_version"]
            or manifest["license"] != "MIT"
            or not isinstance(manifest["source_commit"], str)
            or not _COMMIT.fullmatch(manifest["source_commit"])):
        raise ValueError("skill snapshot provenance is malformed")
    skills = manifest["supported_skills"]
    if (not isinstance(skills, list) or not skills
            or any(not isinstance(skill, str) or not _SKILL_ID.fullmatch(skill) for skill in skills)
            or len(set(skills)) != len(skills)):
        raise ValueError("supported skill inventory is malformed")
    files = manifest["files"]
    if not isinstance(files, list) or not files:
        raise ValueError("skill file inventory is malformed")

    sources = set()
    destinations = set()
    indexed_skills = set()
    normalized = []
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"source", "destination", "sha256"}:
            raise ValueError("malformed file entry in skill snapshot manifest")
        source = _safe_relative_path(entry["source"], "source")
        destination = _safe_relative_path(entry["destination"], "destination")
        if source.as_posix() in sources:
            raise ValueError("duplicate source path in skill file inventory")
        if destination.as_posix() in destinations:
            raise ValueError(f"duplicate destination path: {destination.as_posix()}")
        if (len(source.parts) < 3 or source.parts[0] != "skills"
                or not _SKILL_ID.fullmatch("/".join(source.parts[1:3]))
                or tuple(source.parts[1:]) != destination.parts):
            raise ValueError("source and destination must name a supported skill file")
        skill = "/".join(destination.parts[:2])
        indexed_skills.add(skill)
        if not isinstance(entry["sha256"], str) or not _SHA256.fullmatch(entry["sha256"]):
            raise ValueError("malformed SHA-256 in skill file inventory")
        sources.add(source.as_posix())
        destinations.add(destination.as_posix())
        normalized.append((source, destination, entry["sha256"]))

    dest_posix = sorted(destinations)
    for i in range(len(dest_posix) - 1):
        if dest_posix[i + 1].startswith(dest_posix[i] + "/"):
            raise ValueError(f"destination path collision: {dest_posix[i]}")

    if indexed_skills != set(skills):
        raise ValueError("supported skill inventory does not match its files")
    for skill in skills:
        if f"{skill}/SKILL.md" not in destinations:
            raise ValueError(f"supported skill is missing SKILL.md: {skill}")
    return manifest, sorted(normalized, key=lambda item: item[1].as_posix())


def _snapshot_files(snapshot_root):
    files = set()
    for current, directories, filenames in os.walk(snapshot_root, followlinks=False):
        current_path = Path(current)
        for name in tuple(directories):
            path = current_path / name
            if path.is_symlink():
                raise ValueError(f"symlink is not allowed in snapshot: {path}")
        for name in filenames:
            path = current_path / name
            if path.is_symlink():
                raise ValueError(f"symlink is not allowed in snapshot: {path}")
            files.add(path.relative_to(snapshot_root).as_posix())
    return files


def _preflight_destination(destination, records, prior_manifest=None, acknowledged_removals=frozenset()):
    destination = Path(os.path.abspath(destination))
    _reject_symlink_components(destination)
    if destination.exists() and not destination.is_dir():
        raise ValueError(f"destination is not a directory: {destination}")
    for _, relative_target, expected_hash in records:
        target = destination / relative_target
        _reject_symlink_components(target)

        # Check for directory collisions: parent components cannot be files
        current = target.parent
        while current != destination and destination in current.parents:
            if current.exists() and not current.is_dir():
                raise ValueError(f"destination directory collision: {current.relative_to(destination).as_posix()} is not a directory")
            current = current.parent

        if not target.exists():
            if prior_manifest is not None:
                rel_posix = relative_target.as_posix()
                rel_key = rel_posix.removeprefix(".agents/skills/") if rel_posix.startswith(".agents/skills/") else rel_posix
                if rel_key in prior_manifest.get("files", {}):
                    raise ValueError(f"destination has local changes: {rel_posix}")
            continue
        if not target.is_file():
            raise ValueError(f"destination is not a regular file: {relative_target.as_posix()}")
        actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            if prior_manifest is not None:
                rel_posix = relative_target.as_posix()
                rel_key = rel_posix.removeprefix(".agents/skills/") if rel_posix.startswith(".agents/skills/") else rel_posix
                if rel_key == "manifest.json":
                    continue
                prior_files = prior_manifest.get("files", {})
                if isinstance(prior_files, dict) and prior_files.get(rel_key) == actual_hash:
                    continue
            prior_hash = None
            if prior_manifest is not None:
                prior_files = prior_manifest.get("files", {})
                if isinstance(prior_files, dict):
                    prior_hash = prior_files.get(rel_key)
            raise ValueError(
                f"destination has local changes: {relative_target.as_posix()} "
                f"(installed sha256 {prior_hash or 'none'}, "
                f"local sha256 {actual_hash}, proposed sha256 {expected_hash})")

    if prior_manifest is not None:
        prior_files = prior_manifest.get("files", {})
        if isinstance(prior_files, dict):
            records_keys = {
                (r[1].as_posix().removeprefix(".agents/skills/") if hasattr(r[1], "as_posix") else str(r[1]).removeprefix(".agents/skills/"))
                for r in records
            }
            for prior_rel, prior_hash in prior_files.items():
                target = destination / ".agents/skills" / prior_rel
                acknowledged = skill_id_from_path(prior_rel) in acknowledged_removals
                if not target.exists():
                    if acknowledged:
                        continue
                    raise ValueError(
                        f"destination has local changes: .agents/skills/{prior_rel} "
                        f"(installed sha256 {prior_hash}, local sha256 absent)")
                if not target.is_file():
                    raise ValueError(f"destination is not a regular file: .agents/skills/{prior_rel}")
                actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
                # Verify that files not in the new records still match their prior hash
                if prior_rel not in records_keys and actual_hash != prior_hash:
                    raise ValueError(
                        f"destination has local changes: .agents/skills/{prior_rel} "
                        f"(installed sha256 {prior_hash}, local sha256 {actual_hash})")
                if not target.is_file():
                    raise ValueError(f"destination is not a regular file: .agents/skills/{prior_rel}")
                actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
                # Verify that files not in the new records still match their prior hash
                records_keys = {
                    (r[1].as_posix().removeprefix(".agents/skills/") if hasattr(r[1], "as_posix") else str(r[1]).removeprefix(".agents/skills/"))
                    for r in records
                }
                if prior_rel not in records_keys and actual_hash != prior_hash:
                    raise ValueError(f"destination has local changes: .agents/skills/{prior_rel}")


def _verified_files(snapshot_root, manifest_path):
    snapshot_root = Path(snapshot_root)
    _reject_symlink_components(snapshot_root)
    if not snapshot_root.is_dir():
        raise ValueError("skill snapshot directory is missing")
    manifest, records = _load_manifest(manifest_path)
    actual_files = _snapshot_files(snapshot_root)
    indexed_sources = {source.as_posix() for source, _, _ in records}
    if actual_files != indexed_sources:
        raise ValueError("snapshot files do not match the inventory")

    checked = []
    for source, relative_target, expected_hash in records:
        source_path = snapshot_root / source
        _reject_symlink_components(source_path)
        try:
            content = source_path.read_bytes()
        except OSError as error:
            raise ValueError(f"snapshot source is unreadable: {source.as_posix()}") from error
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise ValueError(f"snapshot hash mismatch: {source.as_posix()}")
        checked.append((relative_target, content, expected_hash))

    return records, checked


def load_verified_skills(snapshot_root, manifest_path):
    """Return the reviewed skill snapshot as destination-path/bytes pairs."""
    _, checked = _verified_files(snapshot_root, manifest_path)
    return tuple((path.as_posix(), content) for path, content, _ in checked)


def parse_agent_skills_manifest(content):
    """Parse and validate a generated agent skills target manifest."""
    if isinstance(content, (bytes, bytearray)):
        try:
            text = content.decode("utf-8")
        except UnicodeError:
            return None
    elif isinstance(content, str):
        text = content
    else:
        return None
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if (not isinstance(value, dict) or set(value) != _TARGET_MANIFEST_KEYS
            or value.get("schema_version") != 1
            or value.get("managed_by") not in ("sciflow-cicd", "github-repo-setup")
            or value.get("bundle") != "mattpocock"
            or value.get("source_repository") != "https://github.com/mattpocock/skills"
            or not isinstance(value.get("source_commit"), str)
            or not _COMMIT.fullmatch(value["source_commit"])
            or not isinstance(value.get("source_version"), str)
            or not value.get("source_version")
            or value.get("license") != "MIT"
            or not isinstance(value.get("files"), dict)):
        return None
    files = value["files"]
    if any(not isinstance(path, str) or path.startswith("/") or ".." in path.split("/")
           or not isinstance(digest, str) or not _SHA256.fullmatch(digest)
           for path, digest in files.items()):
        return None
    return value


def generate_target_manifest(source_manifest, records, license_bytes):
    """Generate target manifest data recording revision, license, attribution, and file hashes."""
    license_hash = hashlib.sha256(license_bytes).hexdigest()
    file_hashes = {"LICENSE": license_hash}
    for item in records:
        if len(item) == 3:
            target = item[1] if isinstance(item[1], Path) else Path(item[0])
            rel_str = target.as_posix() if isinstance(target, Path) else str(target)
            file_hashes[rel_str] = item[2]
    return {
        "schema_version": 1,
        "managed_by": "sciflow-cicd",
        "bundle": "mattpocock",
        "source_repository": source_manifest["source_repository"],
        "source_commit": source_manifest["source_commit"],
        "source_version": source_manifest["source_version"],
        "license": source_manifest["license"],
        "files": dict(sorted(file_hashes.items())),
    }


def load_verified_catalog(catalog_dir=None):
    """Validate catalog provenance, license, and snapshot files, returning the staging bundle."""
    if catalog_dir is None:
        catalog_dir = Path(__file__).resolve().parents[1] / "data/agent-skills/mattpocock"
    catalog_dir = Path(os.path.abspath(catalog_dir))
    _reject_symlink_components(catalog_dir)
    if not catalog_dir.is_dir():
        raise ValueError(f"skill catalog directory is missing: {catalog_dir}")
    manifest_path = catalog_dir / "manifest.json"
    snapshot_root = catalog_dir / "snapshot"
    license_path = catalog_dir / "LICENSE"

    manifest_data, records = _load_manifest(manifest_path)
    license_bytes = _validate_license(license_path, manifest_data["license"])
    records, checked = _verified_files(snapshot_root, manifest_path)

    target_manifest = generate_target_manifest(manifest_data, records, license_bytes)
    manifest_bytes = (json.dumps(target_manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")

    files = {
        ".agents/skills/" + relative_target.as_posix(): content
        for relative_target, content, _ in checked
    }
    files[".agents/skills/LICENSE"] = license_bytes
    files[".agents/skills/manifest.json"] = manifest_bytes

    return files, target_manifest


def _resolve_removal_acknowledgment(prior_manifest, new_manifest, allow_removals):
    """Validate the explicit removal decision for a catalog upgrade.

    Returns the frozenset of acknowledged skill ids, or an empty frozenset
    when the upgrade removes nothing. Raises ValueError when the upgrade
    drops skills and no exact acknowledgment was supplied.
    """
    if prior_manifest is None:
        return frozenset()
    classification = classify_catalog_upgrade(prior_manifest, new_manifest)
    dropped = list(classification["removed_skills"])
    dropped.extend(pair["from"] for pair in classification["renamed_skills"])
    if not dropped:
        return frozenset()
    validate_removal_acknowledgment(dropped, allow_removals, classification=classification)
    return frozenset(dropped)


def _attach_restore_failures(error, restore_failures):
    """Attach restore failures as secondary context to the primary error."""
    if restore_failures:
        error.restore_failures = restore_failures
        error.__context__ = OSError(f"rollback restore failures: {restore_failures}")
        if hasattr(error, "add_note"):
            error.add_note(f"rollback restore failures: {restore_failures}")


def _rollback_destination(backups, created, pruned_dirs=None):
    """Roll back every destination mutation and remove newly created files."""
    restore_failures = []
    if pruned_dirs:
        for directory in pruned_dirs:
            try:
                Path(directory).mkdir(parents=True, exist_ok=True)
            except OSError as err:
                restore_failures.append(err)
    for target_file, original_bytes in backups.items():
        try:
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_bytes(original_bytes)
        except OSError as err:
            restore_failures.append(err)
    for target_file in created:
        try:
            target_file.unlink()
        except OSError:
            pass
    return restore_failures


def provision_skills(snapshot_root, manifest_path, destination, *, license_path=None,
                    target_manifest=False, allow_removals=None):
    """Validate the complete snapshot, then copy it into ``destination``.

    If ``target_manifest`` is True, destination receives the full skill bundle
    including ``.agents/skills/LICENSE`` and ``.agents/skills/manifest.json``.
    All catalog provenance, license, complete file inventory, and target
    compatibility are validated before any write to the destination.
    Existing files are accepted only when they already match the snapshot.

    When the destination already holds a managed bundle from an older catalog
    revision and the new snapshot drops skills, the upgrade is blocked unless
    ``allow_removals`` lists exactly those skill ids; acknowledged skills are
    then deleted only when their installed content still matches the prior
    manifest, so locally edited files are never removed.
    """
    records, checked = _verified_files(snapshot_root, manifest_path)
    manifest_data, _ = _load_manifest(manifest_path)

    destination = Path(os.path.abspath(destination))
    _reject_symlink_components(destination)
    if destination.exists() and not destination.is_dir():
        raise ValueError(f"destination is not a directory: {destination}")

    if target_manifest:
        if license_path is None:
            license_path = Path(manifest_path).parent / "LICENSE"
        license_bytes = _validate_license(license_path, manifest_data["license"])
        target_manifest_data = generate_target_manifest(manifest_data, records, license_bytes)
        manifest_bytes = (json.dumps(target_manifest_data, indent=2, sort_keys=True) + "\n").encode("utf-8")

        if destination.name == "skills" and destination.parent.name == ".agents":
            target_root = destination.parent.parent
        else:
            target_root = destination

        staged_items = []
        for relative_target, content, expected_hash in checked:
            staged_items.append((
                Path(".agents/skills") / relative_target,
                content,
                expected_hash
            ))
        staged_items.append((
            Path(".agents/skills/LICENSE"),
            license_bytes,
            hashlib.sha256(license_bytes).hexdigest()
        ))
        staged_items.append((
            Path(".agents/skills/manifest.json"),
            manifest_bytes,
            hashlib.sha256(manifest_bytes).hexdigest()
        ))

        existing_manifest_path = target_root / ".agents/skills/manifest.json"
        prior_manifest = None
        if existing_manifest_path.exists():
            _reject_symlink_components(existing_manifest_path)
            if not existing_manifest_path.is_file():
                raise ValueError("existing target manifest is not a regular file")
            prior_manifest = parse_agent_skills_manifest(existing_manifest_path.read_bytes())
            if prior_manifest is None:
                raise ValueError("existing target manifest is malformed")

        acknowledged_removals = _resolve_removal_acknowledgment(
            prior_manifest, target_manifest_data, allow_removals)

        _preflight_destination(target_root, [(None, rel_path, exp_hash) for rel_path, _, exp_hash in staged_items], prior_manifest=prior_manifest, acknowledged_removals=acknowledged_removals)

        # Back up every destination file we are about to mutate so a partial
        # write or removal failure can be rolled back to a consistent state
        # instead of leaving the destination half-upgraded (some new files written,
        # old manifest or old skills left behind).
        backups = {}
        created = set()
        for rel_path, _content, _ in staged_items:
            target_file = target_root / rel_path
            if target_file.exists():
                backups[target_file] = target_file.read_bytes()
            else:
                created.add(target_file)

        try:
            for rel_path, content, _ in staged_items:
                target_file = target_root / rel_path
                if target_file.exists() and target_file.read_bytes() == content:
                    continue
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_bytes(content)
        except OSError as error:
            restore_failures = _rollback_destination(backups, created)
            _attach_restore_failures(error, restore_failures)
            raise

        if acknowledged_removals and prior_manifest is not None:
            skills_root = target_root / ".agents/skills"
            removed_files = []
            for prior_rel in sorted(prior_manifest.get("files", {})):
                if skill_id_from_path(prior_rel) not in acknowledged_removals:
                    continue
                removed_file = skills_root / prior_rel
                if removed_file.exists():
                    backups[removed_file] = removed_file.read_bytes()
                    removed_files.append(removed_file)

            first_error = None
            pruned_dirs = []
            for removed_file in removed_files:
                try:
                    removed_file.unlink()
                    parent = removed_file.parent
                    while skills_root in parent.parents or parent == skills_root:
                        try:
                            parent.rmdir()
                            pruned_dirs.append(parent)
                        except OSError:
                            break
                        parent = parent.parent
                except OSError as error:
                    if first_error is None:
                        first_error = error
                    break

            if first_error is not None:
                # Roll back every mutation performed so far and re-raise.
                restore_failures = _rollback_destination(backups, created, pruned_dirs=pruned_dirs)
                _attach_restore_failures(first_error, restore_failures)
                raise first_error

        return tuple(sorted(rel_path.as_posix() for rel_path, _, _ in staged_items))
    else:
        if license_path is not None or (Path(manifest_path).parent / "LICENSE").exists():
            actual_license_path = license_path or (Path(manifest_path).parent / "LICENSE")
            if license_path is not None or actual_license_path.exists():
                _validate_license(actual_license_path, manifest_data["license"])

        _preflight_destination(destination, records)
        for relative_target, content, _ in checked:
            target = destination / relative_target
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return tuple(relative_target.as_posix() for relative_target, _, _ in checked)
