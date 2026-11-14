import unittest

from scripts.agent_skill_id import skill_id_from_path


class SkillIdFromPathTests(unittest.TestCase):
    def test_full_skill_path_converts_to_skill_id(self):
        self.assertEqual(
            skill_id_from_path(".agents/skills/engineering/tdd/SKILL.md"),
            "engineering/tdd")

    def test_relative_skill_path_converts_to_skill_id(self):
        self.assertEqual(
            skill_id_from_path("engineering/tdd/SKILL.md"),
            "engineering/tdd")

    def test_productivity_category_converts_to_skill_id(self):
        self.assertEqual(
            skill_id_from_path("productivity/ask-matt/SKILL.md"),
            "productivity/ask-matt")

    def test_manifest_is_not_a_skill(self):
        self.assertIsNone(skill_id_from_path(".agents/skills/manifest.json"))

    def test_license_is_not_a_skill(self):
        self.assertIsNone(skill_id_from_path(".agents/skills/LICENSE"))

    def test_skill_directory_without_file_is_not_a_skill(self):
        self.assertIsNone(skill_id_from_path("engineering/tdd"))

    def test_non_skill_path_is_not_a_skill(self):
        self.assertIsNone(skill_id_from_path("docs/agents/issue-tracker.md"))
        self.assertIsNone(skill_id_from_path(""))
        self.assertIsNone(skill_id_from_path("engineering/"))


if __name__ == "__main__":
    unittest.main()
