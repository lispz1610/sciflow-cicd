"""Unit tests for agent runtime skill discovery in disposable targets."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from scripts.verify_agent_skill_discovery import (
    DiscoveredSkill,
    RuntimeProbeResult,
    SkillDiscoveryError,
    SkillDiscoveryReport,
    discover_workspace_skills,
    main,
    parse_frontmatter,
    probe_and_execute_runtime,
    resolve_skills_root,
    validate_skill_file,
    verify_disposable_skill_discovery,
)


class AgentSkillDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_parse_frontmatter_valid(self):
        text = (
            "---\n"
            "name: test-skill\n"
            "description: Test skill description.\n"
            "disable-model-invocation: true\n"
            "---\n"
            "\n"
            "# Test Skill\n"
            "Body content.\n"
        )
        fm, body = parse_frontmatter(text)
        self.assertEqual(fm["name"], "test-skill")
        self.assertEqual(fm["description"], "Test skill description.")
        self.assertTrue(fm.get("disable-model-invocation"))
        self.assertIn("# Test Skill", body)

    def test_parse_frontmatter_folded_scalar(self):
        text = (
            "---\n"
            "name: test-folded\n"
            "description: >-\n"
            "  First line of description.\n"
            "  Second line of description.\n"
            "---\n"
            "# Body\n"
        )
        fm, body = parse_frontmatter(text)
        self.assertEqual(fm["name"], "test-folded")
        self.assertEqual(fm["description"], "First line of description. Second line of description.")

    def test_parse_frontmatter_errors(self):
        # Missing starting delimiter
        with self.assertRaises(SkillDiscoveryError):
            parse_frontmatter("name: no-delim\ndescription: missing start")

        # Unclosed delimiter
        with self.assertRaises(SkillDiscoveryError):
            parse_frontmatter("---\nname: unclosed\ndescription: test\n")

        # Non-mapping YAML
        with self.assertRaises(SkillDiscoveryError):
            parse_frontmatter("---\n- item1\n- item2\n---\n")

    def test_validate_skill_file_success(self):
        skill_dir = self.root / ".agents/skills/engineering/example"
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        content = (
            "---\n"
            "name: example\n"
            "description: Example engineering skill.\n"
            "---\n"
            "# Instructions\n"
        ).encode("utf-8")
        skill_file.write_bytes(content)

        skills_root = self.root / ".agents/skills"
        discovered = validate_skill_file(skill_file, skills_root)

        self.assertEqual(discovered.skill_id, "engineering/example")
        self.assertEqual(discovered.name, "example")
        self.assertEqual(discovered.description, "Example engineering skill.")
        self.assertEqual(discovered.file_size, len(content))
        self.assertEqual(discovered.sha256, hashlib.sha256(content).hexdigest())

    def test_validate_skill_file_encoding_and_format_errors(self):
        skills_root = self.root / ".agents/skills"
        skills_root.mkdir(parents=True)

        # Empty file
        empty_file = skills_root / "empty" / "SKILL.md"
        empty_file.parent.mkdir()
        empty_file.write_bytes(b"")
        with self.assertRaises(SkillDiscoveryError) as ctx:
            validate_skill_file(empty_file, skills_root)
        self.assertIn("empty", str(ctx.exception).lower())

        # Invalid UTF-8
        bad_utf8 = skills_root / "bad_utf8" / "SKILL.md"
        bad_utf8.parent.mkdir()
        bad_utf8.write_bytes(b"\xff\xfe\x00\x00")
        with self.assertRaises(SkillDiscoveryError) as ctx:
            validate_skill_file(bad_utf8, skills_root)
        self.assertIn("utf-8", str(ctx.exception).lower())

        # Invalid name pattern (uppercase / spaces)
        bad_name = skills_root / "bad_name" / "SKILL.md"
        bad_name.parent.mkdir()
        bad_name.write_text("---\nname: Bad Name\ndescription: test\n---\n", encoding="utf-8")
        with self.assertRaises(SkillDiscoveryError) as ctx:
            validate_skill_file(bad_name, skills_root)
        self.assertIn("name", str(ctx.exception).lower())

        # Missing description
        no_desc = skills_root / "no_desc" / "SKILL.md"
        no_desc.parent.mkdir()
        no_desc.write_text("---\nname: valid-name\n---\n", encoding="utf-8")
        with self.assertRaises(SkillDiscoveryError) as ctx:
            validate_skill_file(no_desc, skills_root)
        self.assertIn("description", str(ctx.exception).lower())

    def test_discover_workspace_skills_hierarchy(self):
        skills_root = self.root / ".agents/skills"
        skills_root.mkdir(parents=True)

        # Flat skill
        flat = skills_root / "standalone"
        flat.mkdir()
        (flat / "SKILL.md").write_text(
            "---\nname: standalone\ndescription: Flat skill\n---\n# Doc\n",
            encoding="utf-8",
        )

        # Categorized skill
        cat = skills_root / "category" / "nested"
        cat.mkdir(parents=True)
        (cat / "SKILL.md").write_text(
            "---\nname: nested\ndescription: Nested skill\n---\n# Doc\n",
            encoding="utf-8",
        )

        # Hidden directory should be ignored
        hidden = skills_root / ".hidden" / "ignored"
        hidden.mkdir(parents=True)
        (hidden / "SKILL.md").write_text(
            "---\nname: ignored\ndescription: Should be ignored\n---\n",
            encoding="utf-8",
        )

        root_out, skills, errors = discover_workspace_skills(self.root)
        self.assertEqual(root_out, skills_root)
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(skills), 2)
        skill_ids = [s.skill_id for s in skills]
        self.assertIn("standalone", skill_ids)
        self.assertIn("category/nested", skill_ids)

    def test_runtime_probe_unavailable(self):
        # Probing non-existent binary
        probe = probe_and_execute_runtime(self.root, custom_bin="nonexistent-agent-bin-xyz")
        self.assertFalse(probe.available)
        self.assertIsNone(probe.runtime_bin)
        self.assertIsNone(probe.exit_status)

    def test_runtime_probe_mock_executable(self):
        mock_bin = self.root / "mock_agent.sh"
        mock_bin.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            "  echo 'mock-agent v1.0.0'\n"
            "  exit 0\n"
            "fi\n"
            "echo 'Discovered workspace skills: engineering/example'\n"
            "exit 0\n"
        )
        mock_bin.chmod(mock_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        probe = probe_and_execute_runtime(self.root, custom_bin=str(mock_bin))
        self.assertTrue(probe.available)
        self.assertEqual(probe.runtime_bin, str(mock_bin.resolve()))
        self.assertIn("mock-agent v1.0.0", probe.version or "")
        self.assertEqual(probe.exit_status, 0)
        self.assertIn("Discovered workspace skills", probe.stdout or "")

    def _mock_runtime(self, skills_list_body: str) -> Path:
        mock_bin = self.root / "mock_runtime.sh"
        mock_bin.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then echo 'mock 1.0'; exit 0; fi\n"
            "if [ \"$1\" = \"--help\" ]; then echo 'usage: mock'; exit 0; fi\n"
            + skills_list_body
        )
        mock_bin.chmod(mock_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return mock_bin

    def test_runtime_probe_verified_only_when_output_names_all_skills(self):
        mock_bin = self._mock_runtime("echo 'skills: standalone, category/nested'\nexit 0\n")
        probe = probe_and_execute_runtime(
            self.root,
            custom_bin=str(mock_bin),
            expected_skill_ids=["standalone", "category/nested"],
        )
        self.assertTrue(probe.discovery_verified)
        self.assertEqual(probe.missing_ids, [])
        self.assertEqual(probe.exit_status, 0)

    def test_runtime_probe_unverified_when_output_omits_a_skill(self):
        mock_bin = self._mock_runtime("echo 'skills: standalone'\nexit 0\n")
        probe = probe_and_execute_runtime(
            self.root,
            custom_bin=str(mock_bin),
            expected_skill_ids=["standalone", "category/nested"],
        )
        self.assertTrue(probe.available)
        self.assertFalse(probe.discovery_verified)
        self.assertEqual(probe.missing_ids, ["category/nested"])

    def test_runtime_probe_help_fallback_is_not_discovery(self):
        mock_bin = self._mock_runtime("echo 'unknown command' >&2\nexit 2\n")
        probe = probe_and_execute_runtime(
            self.root, custom_bin=str(mock_bin), expected_skill_ids=["standalone"]
        )
        self.assertTrue(probe.available)
        self.assertFalse(probe.discovery_verified)
        self.assertTrue((probe.command or "").endswith("--help"))
        self.assertEqual(probe.exit_status, 0)
        self.assertEqual(probe.missing_ids, ["standalone"])

    def test_runtime_probe_without_expected_ids_is_unverified(self):
        mock_bin = self._mock_runtime("echo 'anything'\nexit 0\n")
        probe = probe_and_execute_runtime(self.root, custom_bin=str(mock_bin))
        self.assertFalse(probe.discovery_verified)

    def test_report_states_only_observed_runtime_evidence(self):
        mock_bin = self._mock_runtime("echo 'unknown command' >&2\nexit 2\n")
        report = verify_disposable_skill_discovery(runtime_bin=str(mock_bin))
        text = report.format_evidence_report()
        self.assertNotIn("- Exit status:", text)
        self.assertIn("Runtime command exit status: 0", text)
        self.assertIn("Runtime skill discovery: Unverified", text)
        self.assertIn("`--help` fallback shows only that the binary runs", text)
        self.assertNotIn("index these skills", text)
        self.assertNotIn("execution verified against target", text)

    def test_report_verified_when_runtime_lists_every_skill(self):
        # Runtime that prints every installed SKILL.md directory name.
        mock_bin = self._mock_runtime(
            "find .agents/skills -name SKILL.md | while read f; do basename $(dirname $f); done\nexit 0\n"
        )
        report = verify_disposable_skill_discovery(runtime_bin=str(mock_bin))
        self.assertTrue(report.runtime_probe.discovery_verified)
        self.assertIn("Runtime skill discovery: Verified", report.format_evidence_report())

    def _target_with_one_skill(self) -> Path:
        target = self.root / "target"
        skill = target / ".agents/skills/standalone"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: standalone\ndescription: Flat skill\n---\n# Doc\n",
            encoding="utf-8",
        )
        return target

    def _assert_parity_fails_closed(self, manifest_path: Path, fragment: str):
        report = verify_disposable_skill_discovery(
            target_dir=self._target_with_one_skill(),
            catalog_manifest=manifest_path,
            runtime_bin="nonexistent-agent-bin-xyz",
        )
        self.assertFalse(report.manifest_match)
        self.assertFalse(report.is_clean)
        self.assertTrue(any(fragment in e for e in report.errors), report.errors)
        self.assertIn("Catalog manifest parity: Mismatch", report.format_evidence_report())

    def test_parity_fails_closed_on_malformed_manifest(self):
        bad = self.root / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self._assert_parity_fails_closed(bad, "Catalog manifest unreadable")

    def test_parity_fails_closed_on_missing_manifest(self):
        self._assert_parity_fails_closed(self.root / "absent.json", "Catalog manifest not found")

    def test_parity_fails_closed_on_empty_skill_list(self):
        empty = self.root / "empty.json"
        empty.write_text(json.dumps({"supported_skills": []}), encoding="utf-8")
        self._assert_parity_fails_closed(empty, "Catalog manifest unreadable")

    def test_parity_fails_closed_on_missing_skill_list_key(self):
        nokey = self.root / "nokey.json"
        nokey.write_text(json.dumps({"other": 1}), encoding="utf-8")
        self._assert_parity_fails_closed(nokey, "Catalog manifest unreadable")

    def test_verify_disposable_skill_discovery_end_to_end(self):
        # End-to-end verification against repository catalog
        report = verify_disposable_skill_discovery()
        self.assertTrue(report.is_disposable)
        self.assertTrue(report.is_clean)
        self.assertEqual(len(report.skills), 27)
        self.assertTrue(report.manifest_match)
        self.assertEqual(len(report.manifest_missing), 0)
        self.assertEqual(len(report.manifest_unexpected), 0)
        self.assertEqual(len(report.errors), 0)

        evidence_text = report.format_evidence_report()
        self.assertIn("### Observed", evidence_text)
        self.assertIn("### Inferred", evidence_text)
        self.assertIn("### Unverified", evidence_text)
        self.assertIn("Discovered 27 skill entrypoint(s)", evidence_text)
        self.assertIn("Live agent runtime CLI execution is UNVERIFIED", evidence_text)

    def test_cli_main_text_and_json(self):
        buf_text = io.StringIO()
        with contextlib.redirect_stdout(buf_text):
            ret = main(["--format", "text"])
        self.assertEqual(ret, 0)
        self.assertIn("Agent Runtime Skill Discovery Verification Report", buf_text.getvalue())

        buf_json = io.StringIO()
        with contextlib.redirect_stdout(buf_json):
            ret = main(["--format", "json"])
        self.assertEqual(ret, 0)
        parsed = json.loads(buf_json.getvalue())
        self.assertTrue(parsed["is_clean"])
        self.assertEqual(parsed["skills_count"], 27)


if __name__ == "__main__":
    unittest.main()
