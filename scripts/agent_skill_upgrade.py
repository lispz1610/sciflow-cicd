"""Classify the difference between two agent skill catalog revisions.

This module is a pure comparison seam: it never reads or writes the
filesystem or the GitHub API. Both inputs are target manifests in the
shape produced by ``parse_agent_skills_manifest`` (or
``generate_target_manifest``), whose ``files`` mapping records relative
paths under ``.agents/skills/`` with SHA-256 content digests.

Removals and renames are identified separately from ordinary content
updates so callers can require an explicit maintainer decision before
any managed skill stops being tracked.
"""

SKILL_FILE_EXCLUSIONS = frozenset({"manifest.json"})

try:
    from scripts.agent_skill_id import skill_id_from_path
except ImportError:  # pragma: no cover
    from agent_skill_id import skill_id_from_path


def _skill_inventories(files):
    """Group a manifest file inventory into per-skill {filename: digest} maps.

    Keys are file names relative to the skill directory so a renamed skill
    with identical content compares equal to its previous inventory.
    """
    inventories = {}
    for relative_path, digest in files.items():
        skill = skill_id_from_path(relative_path)
        if skill is None:
            continue
        filename = relative_path[len(skill) + 1:]
        inventories.setdefault(skill, {})[filename] = digest
    return inventories


def _validate_manifest(value, label):
    if (not isinstance(value, dict) or not isinstance(value.get("files"), dict)
            or any(not isinstance(path, str) or not isinstance(digest, str)
                   for path, digest in value["files"].items())):
        raise ValueError(f"catalog upgrade classification requires manifests with file inventories: {label}")


def classify_catalog_upgrade(previous_manifest, new_manifest):
    """Compare two target manifests and classify the catalog upgrade.

    Returns a dict with:

    - ``removed_skills``: skill ids tracked by the previous manifest but
      absent from the new one, excluding skills detected as renames.
    - ``renamed_skills``: ``{"from": ..., "to": ...}`` pairs where a removed
      skill's complete file inventory matches an added skill exactly.
    - ``added_skills``: skill ids new to this revision, excluding rename
      targets.
    - ``removed_files``: every previously tracked file (except the managed
      manifest itself) absent from the new inventory.
    - ``changed_files``: files present in both inventories with different
      digests.
    - ``unchanged``: True when no tracked file was added, removed, or changed.
    - ``revision_changed``: True when the manifests' source commits differ.
    """
    _validate_manifest(previous_manifest, "previous")
    _validate_manifest(new_manifest, "new")

    previous_files = previous_manifest["files"]
    new_files = new_manifest["files"]

    removed_files = sorted(
        path for path in previous_files
        if path not in SKILL_FILE_EXCLUSIONS and path not in new_files
    )
    added_files = sorted(path for path in new_files if path not in previous_files)
    changed_files = sorted(
        path for path in previous_files
        if path in new_files and previous_files[path] != new_files[path]
    )

    previous_skills = _skill_inventories(previous_files)
    new_skills = _skill_inventories(new_files)
    dropped_skills = sorted(set(previous_skills) - set(new_skills))
    added_skills = sorted(set(new_skills) - set(previous_skills))

    renamed_skills = []
    rename_targets = set()
    for dropped in dropped_skills:
        for candidate in added_skills:
            if candidate in rename_targets:
                continue
            if previous_skills[dropped] == new_skills[candidate]:
                renamed_skills.append({"from": dropped, "to": candidate})
                rename_targets.add(candidate)
                break
    renamed_skills.sort(key=lambda pair: pair["from"])
    rename_sources = {pair["from"] for pair in renamed_skills}

    removed_skills = [skill for skill in dropped_skills if skill not in rename_sources]
    added_skills = [skill for skill in added_skills if skill not in rename_targets]

    return {
        "removed_skills": removed_skills,
        "renamed_skills": renamed_skills,
        "added_skills": added_skills,
        "removed_files": removed_files,
        "added_files": added_files,
        "changed_files": changed_files,
        "unchanged": not (removed_files or added_files or changed_files),
        "revision_changed": (
            previous_manifest.get("source_commit") != new_manifest.get("source_commit")
        ),
    }


def validate_removal_acknowledgment(dropped, acknowledged, classification=None):
    """Validate that ``acknowledged`` lists exactly the dropped skill ids.

    ``dropped`` is the sequence of skill ids the new catalog drops (removed
    skills plus rename sources). ``acknowledged`` is the caller's
    ``allow_removed_skills`` value (or None). Returns None when valid
    (including when nothing is dropped) and raises ValueError otherwise.
    """
    dropped_ids = set(dropped)
    if not dropped_ids:
        return
    if acknowledged is None:
        if classification is None:
            classification = {"removed_skills": sorted(dropped_ids), "renamed_skills": []}
        raise ValueError(
            f"agent skill upgrade requires an explicit removal decision: {removal_decision_hint(classification)}"
        )
    if (not isinstance(acknowledged, (list, tuple, set, frozenset))
            or set(acknowledged) != dropped_ids):
        raise ValueError(
            "allow_removed_skills must list exactly the skills the upstream catalog "
            f"revision drops: {sorted(dropped_ids)}")


def removal_decision_hint(classification):
    """Return the actionable hint for a blocked upgrade that drops skills."""
    removals = classification.get("removed_skills", [])
    renames = classification.get("renamed_skills", [])
    parts = []
    if removals:
        parts.append("removed: " + ", ".join(removals))
    if renames:
        parts.append("renamed: " + ", ".join(
            f"{pair['from']} -> {pair['to']}" for pair in renames))
    acknowledged = sorted(set(removals) | {pair["from"] for pair in renames})
    return (
        "upstream catalog revision no longer ships: " + "; ".join(parts)
        + ". Pass allow_removed_skills with exactly [" + ", ".join(acknowledged)
        + "] to stage their removal in the Setup Pull Request, or keep them installed"
          " by pinning the current catalog revision; target files are never deleted"
          " without that explicit selection"
    )
