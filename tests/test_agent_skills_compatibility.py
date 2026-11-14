import unittest
from unittest.mock import patch

from scripts.agent_skills_compatibility import audit_agent_skills_compatibility, probe_cli_tools


def bundle(*entries):
    files = {}
    for skill_id, body in entries:
        files[f".agents/skills/{skill_id}/SKILL.md"] = body
    return files


class AuditAgentSkillsCompatibilityTests(unittest.TestCase):
    def test_skill_without_requirements_is_ready(self):
        files = bundle(("engineering/implement", b"# Implement\n\nDo the work.\n"))

        report = audit_agent_skills_compatibility(
            files, available_tools=set(), present_paths=set())

        self.assertEqual(report["counts"], {"ready": 1, "requires-manual-setup": 0, "incompatible": 0})
        entry = report["skills"][0]
        self.assertEqual(entry["id"], "engineering/implement")
        self.assertEqual(entry["category"], "ready")
        self.assertEqual(entry["missing_skills"], [])

    def test_missing_convention_requires_manual_setup(self):
        files = bundle(("engineering/domain-modeling",
                        b"# Domain modeling\n\nRead GLOSSARY.md first.\n"))

        report = audit_agent_skills_compatibility(
            files, available_tools=set(), present_paths=set())

        entry = report["skills"][0]
        self.assertEqual(entry["category"], "requires-manual-setup")
        self.assertEqual(entry["missing_conventions"], ["GLOSSARY.md"])
        self.assertEqual(entry["required_conventions"], ["GLOSSARY.md"])

    def test_present_convention_is_ready(self):
        files = bundle(("engineering/domain-modeling",
                        b"# Domain modeling\n\nRead GLOSSARY.md first.\n"))

        report = audit_agent_skills_compatibility(
            files, available_tools=set(), present_paths={"GLOSSARY.md"})

        self.assertEqual(report["skills"][0]["category"], "ready")

    def test_unavailable_tool_requires_manual_setup(self):
        files = bundle(("engineering/triage", b"# Triage\n\nRun `gh label list`.\n"))

        report = audit_agent_skills_compatibility(
            files, available_tools={"git"}, present_paths=set())

        entry = report["skills"][0]
        self.assertEqual(entry["category"], "requires-manual-setup")
        self.assertEqual(entry["missing_tools"], ["gh"])
        self.assertEqual(entry["required_tools"], ["gh"])

    def test_available_tool_is_ready(self):
        files = bundle(("engineering/triage", b"# Triage\n\nRun `gh label list`.\n"))

        report = audit_agent_skills_compatibility(
            files, available_tools={"gh"}, present_paths=set())

        self.assertEqual(report["skills"][0]["category"], "ready")

    def test_prose_tool_mention_without_command_is_not_required(self):
        files = bundle(("engineering/ask-matt", b"# Ask Matt\n\nThe gh CLI is helpful.\n"))

        report = audit_agent_skills_compatibility(
            files, available_tools=set(), present_paths=set())

        self.assertEqual(report["skills"][0]["category"], "ready")
        self.assertEqual(report["skills"][0]["required_tools"], [])

    def test_reference_to_excluded_skill_is_incompatible(self):
        files = bundle(("engineering/implement", b"# Implement\n\nStart with `/tdd`.\n"),
                       ("engineering/tdd", b"# TDD\n\nRed green refactor.\n"))

        report = audit_agent_skills_compatibility(
            files, available_tools=set(), present_paths=set(),
            excluded_skill_ids=("engineering/tdd",))

        entries = {entry["id"]: entry for entry in report["skills"]}
        self.assertNotIn("engineering/tdd", entries)
        self.assertEqual(entries["engineering/implement"]["category"], "incompatible")
        self.assertEqual(entries["engineering/implement"]["missing_skills"], ["engineering/tdd"])
        self.assertEqual(report["counts"]["incompatible"], 1)

    def test_reference_to_staged_skill_is_ready(self):
        files = bundle(("engineering/implement", b"# Implement\n\nStart with `/tdd`.\n"),
                       ("engineering/tdd", b"# TDD\n\nRed green refactor.\n"))

        report = audit_agent_skills_compatibility(
            files, available_tools=set(), present_paths=set())

        self.assertEqual(report["counts"]["ready"], 2)

    def test_reference_to_absent_and_excluded_skill_is_incompatible(self):
        files = bundle(("engineering/implement", b"# Implement\n\nStart with `/tdd`.\n"))

        report = audit_agent_skills_compatibility(
            files, available_tools=set(), present_paths=set(),
            excluded_skill_ids=("engineering/tdd",))

        entries = {entry["id"]: entry for entry in report["skills"]}
        self.assertEqual(entries["engineering/implement"]["category"], "incompatible")
        self.assertEqual(entries["engineering/implement"]["missing_skills"], ["engineering/tdd"])
        self.assertEqual(report["counts"]["incompatible"], 1)

    def test_slash_reference_to_unknown_name_is_ignored(self):
        files = bundle(("engineering/implement", b"# Implement\n\nTry `/unknown-skill`.\n"))

        report = audit_agent_skills_compatibility(
            files, available_tools=set(), present_paths=set())

        self.assertEqual(report["skills"][0]["category"], "ready")
        self.assertEqual(report["skills"][0]["missing_skills"], [])

    def test_report_lists_sorted_ids_and_counts(self):
        files = bundle(("productivity/zeta", b"Plain.\n"),
                       ("engineering/alpha", b"Uses GLOSSARY.md.\n"),
                       ("productivity/mid", b"Run `git log`.\n"))

        report = audit_agent_skills_compatibility(
            files, available_tools=set(), present_paths=set())

        self.assertEqual([entry["id"] for entry in report["skills"]],
                         ["engineering/alpha", "productivity/mid", "productivity/zeta"])
        self.assertEqual(report["counts"],
                         {"ready": 1, "requires-manual-setup": 2, "incompatible": 0})

    def test_non_skill_files_are_ignored(self):
        files = {".agents/skills/manifest.json": b"{}",
                 ".agents/skills/LICENSE": b"MIT\n",
                 ".agents/skills/engineering/implement/SKILL.md": b"Plain.\n"}

        report = audit_agent_skills_compatibility(
            files, available_tools=set(), present_paths=set())

        self.assertEqual(len(report["skills"]), 1)

    def test_probe_cli_tools_reports_installed_tools_only(self):
        with patch("scripts.agent_skills_compatibility.shutil.which",
                   side_effect=lambda name: "/usr/bin/" + name if name == "gh" else None):
            self.assertEqual(probe_cli_tools(), {"gh"})


if __name__ == "__main__":
    unittest.main()
