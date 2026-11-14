"""Report the repository prerequisites that staged agent skills expect.

This module is a pure evaluation seam over a ``read_file`` callable so it can
inspect a GitHub repository (via the contents API) or a local checkout. It
never writes files and it never recreates a documentation tree: copying the
skill bundle into ``.agents/skills/`` is not completion of repository setup.
"""

PREREQUISITES = (
    {
        "name": "issue-tracker",
        "paths": ("docs/agents/issue-tracker.md",),
        "guidance": (
            "run the setup-matt-pocock-skills skill and record where issues "
            "live for this repository in docs/agents/issue-tracker.md "
            "(GitHub Issues by default; GitLab or local markdown under "
            ".scratch/ are also supported)"
        ),
    },
    {
        "name": "triage-labels",
        "paths": ("docs/agents/triage-labels.md",),
        "guidance": (
            "run the setup-matt-pocock-skills skill and confirm the five "
            "canonical triage labels (needs-triage, needs-info, "
            "ready-for-agent, ready-for-human, wontfix) or record existing "
            "overrides in docs/agents/triage-labels.md"
        ),
    },
    {
        "name": "domain-docs",
        "paths": ("docs/agents/domain.md", "GLOSSARY.md"),
    },
)

INCOMPLETE_NOTE = (
    "Staging the skill bundle does not complete repository setup; run the "
    "setup-matt-pocock-skills skill to configure the missing prerequisites "
    "listed above"
)


def evaluate_agent_skills_readiness(read_file):
    report = {"ready": True, "prerequisites": []}
    for spec in PREREQUISITES:
        present = {}
        for path in spec["paths"]:
            content = read_file(path)
            present[path] = content is not None
        missing_paths = [path for path in spec["paths"] if not present[path]]
        item = {
            "name": spec["name"],
            "present": not missing_paths,
            "missing_paths": missing_paths,
        }
        if missing_paths:
            item["guidance"] = _domain_guidance(present) if spec["name"] == "domain-docs" else spec["guidance"]
            report["ready"] = False
        report["prerequisites"].append(item)
    if not report["ready"]:
        report["note"] = INCOMPLETE_NOTE
    return report


def _domain_guidance(present):
    if present.get("GLOSSARY.md"):
        return (
            "GLOSSARY.md detected at the repository root; run the "
            "setup-matt-pocock-skills skill and record the domain doc "
            "layout (glossary and ADR locations) in docs/agents/domain.md"
        )
    return (
        "no domain documents detected; run the setup-matt-pocock-skills "
        "skill to create GLOSSARY.md at the repository root, record the "
        "doc layout in docs/agents/domain.md, and store decisions under "
        "docs/adr/"
    )
