"""Pure helper converting a skill file path to its skill id.

Accepts either a path relative to the skill catalog (e.g.
``engineering/tdd/SKILL.md``) or a path rooted at the catalog directory
(e.g. ``.agents/skills/engineering/tdd/SKILL.md``) and returns the skill
id (``engineering/tdd``), or ``None`` when the path is not a skill file.
"""

SKILL_CATEGORIES = ("engineering", "productivity")
SKILL_ROOT = (".agents", "skills")


def skill_id_from_path(path):
    """Return the skill id for a skill file path, or ``None`` if not one."""
    if not isinstance(path, str) or not path:
        return None
    parts = path.split("/")
    if (len(parts) >= len(SKILL_ROOT) + 1
            and tuple(parts[:len(SKILL_ROOT)]) == SKILL_ROOT):
        parts = parts[len(SKILL_ROOT):]
    if len(parts) < 3 or parts[0] not in SKILL_CATEGORIES:
        return None
    return "/".join(parts[:2])
