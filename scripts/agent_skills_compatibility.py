"""Classify staged agent skills by what they need before they can run.

This module is a pure audit seam over the staged bundle bytes: it never reads
the filesystem or the GitHub API. Requirements are grounded in the bundle's
own text: backticked CLI commands name required tools, literal convention
paths name required repository documents, and backticked slash references
(``/skill-name``) name required sibling skills.

A skill is ``incompatible`` when it references a sibling skill that is not
part of the staged selection. It ``requires-manual-setup`` when a referenced
repository document is absent from the target repository or a referenced CLI
is not installed. Otherwise it is ``ready``.

Mentions of ``AGENTS.md`` and ``CLAUDE.md`` are deliberately not tracked as
conventions: the skills degrade gracefully when either index is absent and
the setup skill creates them on demand.
"""

import re
import shutil

try:
    from scripts.agent_skill_id import skill_id_from_path
except ImportError:  # pragma: no cover
    from agent_skill_id import skill_id_from_path

SKILL_ROOT = ".agents/skills/"
TOOL_NAMES = ("gh", "glab", "git")
CONVENTION_PATHS = ("docs/agents/issue-tracker.md", "docs/agents/triage-labels.md",
                    "docs/agents/domain.md", "GLOSSARY.md", "docs/adr")
SKILL_REFERENCE = re.compile(r"`/([a-z0-9][a-z0-9-]+)`")
CATEGORIES = ("ready", "requires-manual-setup", "incompatible")


def probe_cli_tools():
    return {name for name in TOOL_NAMES if shutil.which(name)}


def audit_agent_skills_compatibility(files, *, available_tools, present_paths,
                                     excluded_skill_ids=()):
    skill_texts = {}
    for path, content in sorted(files.items()):
        skill_id = skill_id_from_path(path)
        if skill_id is None:
            continue
        skill_texts.setdefault(skill_id, []).append(content)
    installed_ids = set(skill_texts) - set(excluded_skill_ids)
    name_to_ids = {}
    for skill_id in skill_texts:
        name_to_ids.setdefault(skill_id.rsplit("/", 1)[1], set()).add(skill_id)
    for excluded_id in excluded_skill_ids:
        name_to_ids.setdefault(excluded_id.rsplit("/", 1)[1], set()).add(excluded_id)

    report = {"skills": [], "counts": {category: 0 for category in CATEGORIES}}
    for skill_id in sorted(installed_ids):
        text = b"\n".join(skill_texts[skill_id]).decode("utf-8", "replace")
        required_tools = sorted(name for name in TOOL_NAMES if _matches_backtick_tool_command(text, name))
        required_conventions = sorted(path for path in CONVENTION_PATHS if path in text)
        referenced_ids = set()
        for name in SKILL_REFERENCE.findall(text):
            referenced_ids.update(name_to_ids.get(name, ()))
        missing_skills = sorted(referenced_ids - installed_ids)
        missing_tools = sorted(set(required_tools) - set(available_tools))
        missing_conventions = sorted(set(required_conventions) - set(present_paths))
        if missing_skills:
            category = "incompatible"
        elif missing_conventions or missing_tools:
            category = "requires-manual-setup"
        else:
            category = "ready"
        report["skills"].append({
            "id": skill_id,
            "category": category,
            "required_tools": required_tools,
            "required_conventions": required_conventions,
            "required_skills": sorted(referenced_ids),
            "missing_tools": missing_tools,
            "missing_conventions": missing_conventions,
            "missing_skills": missing_skills,
        })
        report["counts"][category] += 1
    return report


def _matches_backtick_tool_command(text, tool):
    return re.search(r"`" + tool + r" [a-z]", text) is not None
