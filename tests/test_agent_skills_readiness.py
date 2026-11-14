import unittest

from scripts.agent_skills_readiness import evaluate_agent_skills_readiness


class FakeRepository:
    def __init__(self, files=None):
        self.files = files or {}

    def read_file(self, path):
        return self.files.get(path)


class EvaluateAgentSkillsReadinessTests(unittest.TestCase):
    def test_empty_repository_reports_every_prerequisite_missing(self):
        report = evaluate_agent_skills_readiness(FakeRepository().read_file)

        self.assertFalse(report["ready"])
        names = {item["name"] for item in report["prerequisites"]}
        self.assertEqual(names, {"issue-tracker", "triage-labels", "domain-docs"})
        for item in report["prerequisites"]:
            self.assertFalse(item["present"])
            self.assertTrue(item["missing_paths"])
            self.assertIn("setup-matt-pocock-skills", item["guidance"])

    def test_configured_repository_is_ready(self):
        repository = FakeRepository({
            "docs/agents/issue-tracker.md": b"# Issue tracker\nGitHub Issues\n",
            "docs/agents/triage-labels.md": b"# Triage labels\n",
            "docs/agents/domain.md": b"# Domain docs\n",
            "GLOSSARY.md": b"# Glossary\n",
        })

        report = evaluate_agent_skills_readiness(repository.read_file)

        self.assertTrue(report["ready"])
        self.assertTrue(all(item["present"] for item in report["prerequisites"]))
        self.assertNotIn("note", report)

    def test_partial_configuration_reports_only_missing_prerequisites(self):
        repository = FakeRepository({
            "docs/agents/issue-tracker.md": b"# Issue tracker\n",
        })

        report = evaluate_agent_skills_readiness(repository.read_file)

        self.assertFalse(report["ready"])
        by_name = {item["name"]: item for item in report["prerequisites"]}
        self.assertTrue(by_name["issue-tracker"]["present"])
        self.assertFalse(by_name["triage-labels"]["present"])
        self.assertFalse(by_name["domain-docs"]["present"])

    def test_issue_tracker_guidance_names_supported_choices(self):
        report = evaluate_agent_skills_readiness(FakeRepository().read_file)
        guidance = next(item["guidance"] for item in report["prerequisites"]
                        if item["name"] == "issue-tracker")

        self.assertIn("docs/agents/issue-tracker.md", guidance)
        self.assertIn("GitHub", guidance)
        self.assertIn(".scratch/", guidance)

    def test_triage_labels_guidance_names_the_five_canonical_labels(self):
        report = evaluate_agent_skills_readiness(FakeRepository().read_file)
        guidance = next(item["guidance"] for item in report["prerequisites"]
                        if item["name"] == "triage-labels")

        for label in ("needs-triage", "needs-info", "ready-for-agent",
                      "ready-for-human", "wontfix"):
            self.assertIn(label, guidance)
        self.assertIn("docs/agents/triage-labels.md", guidance)

    def test_domain_guidance_uses_detected_glossary_location(self):
        repository = FakeRepository({
            "docs/agents/issue-tracker.md": b"# Issue tracker\n",
            "docs/agents/triage-labels.md": b"# Triage labels\n",
            "GLOSSARY.md": b"# Glossary\n",
        })

        report = evaluate_agent_skills_readiness(repository.read_file)
        domain = next(item for item in report["prerequisites"]
                      if item["name"] == "domain-docs")

        self.assertFalse(domain["present"])
        self.assertIn("GLOSSARY.md detected", domain["guidance"])
        self.assertIn("docs/agents/domain.md", domain["missing_paths"])

    def test_incomplete_setup_note_distinguishes_staging_from_completion(self):
        report = evaluate_agent_skills_readiness(FakeRepository().read_file)

        self.assertIn("note", report)
        self.assertIn("does not complete", report["note"])
        self.assertIn("setup-matt-pocock-skills", report["note"])


if __name__ == "__main__":
    unittest.main()
