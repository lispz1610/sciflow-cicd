"""Regression tests for issue #87 dead-code and module-linter cleanup."""

import contextlib
import hashlib
import inspect
import io
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for extra_path in (ROOT, ROOT / "scripts"):
    if str(extra_path) not in sys.path:
        sys.path.insert(0, str(extra_path))

from scripts import audit_module_dependencies as module_linter
from scripts import documentation_audit_runner, ocr_preflight, ocr_reviewer, review_readiness
from scripts import install_github_workflows, interactive_defaults, interactive_workflows_rulesets
from scripts import doctor, interactive, interactive_actions_security, interactive_customization
from scripts import interactive_repository_content, provision_agent_skills


class DeadScaffoldingRemovalTests(unittest.TestCase):
    def test_noop_competing_workflow_helper_removed(self):
        self.assertFalse(hasattr(ocr_preflight, "inspect_competing_workflow"))
        self.assertFalse(hasattr(ocr_reviewer, "_filter_active_repositories"))
        preflight_fields = set(ocr_preflight.OcrPreflightResult.__dataclass_fields__.keys())
        self.assertNotIn("competing_workflows", preflight_fields)
        self.assertNotIn("indeterminate_workflows", preflight_fields)
        self.assertNotIn("workflow_checks", preflight_fields)
        reviewer_fields = set(ocr_reviewer.OcrReviewPassResult.__dataclass_fields__.keys())
        self.assertNotIn("skipped_competing_workflow", reviewer_fields)
        self.assertNotIn("skipped_indeterminate_workflow", reviewer_fields)
        self.assertNotIn(
            "warning",
            {field.name for field in ocr_preflight.CompetingWorkflowStatus.__dataclass_fields__.values()},
        )

    def test_dead_review_readiness_symbols_removed(self):
        self.assertFalse(hasattr(review_readiness, "validate_trusted_rule"))
        self.assertFalse(hasattr(review_readiness, "_allowed"))
        self.assertFalse(hasattr(review_readiness, "UPSTREAM_ACTION_DEPENDENCIES"))
        self.assertTrue(callable(review_readiness.review_readiness))

    def test_legacy_review_markers_removed(self):
        self.assertFalse(hasattr(install_github_workflows, "REVIEW_MARKER"))
        self.assertFalse(hasattr(install_github_workflows, "REVIEW_RUNTIME_PATH"))

    def test_documentation_audit_runner_has_single_shebang_and_no_mode_parameter(self):
        source = (ROOT / "scripts" / "documentation_audit_runner.py").read_text(encoding="utf-8")
        self.assertEqual(source.splitlines()[0], "#!/usr/bin/env python3")
        self.assertFalse(source.splitlines()[1].startswith("#!"))
        self.assertNotIn("mode", inspect.signature(documentation_audit_runner.make_report).parameters)

    def test_unused_imports_removed_from_script_modules(self):
        removed_symbols = {
            interactive: (
                "prompt_rulesets_stage", "execute_dry_run", "format_dry_run_summary",
                "display_dry_run_summary", "prompt_apply_confirmation", "execute_apply",
                "format_apply_summary",
            ),
            doctor: ("re", "Tuple"),
            interactive_defaults: ("Any", "Dict"),
            interactive_actions_security: ("Union",),
            interactive_customization: ("List", "Union", "get_profile"),
            interactive_repository_content: ("re", "List", "PromptExit"),
            interactive_workflows_rulesets: (
                "DEFAULT_SETUP_PYTHON_SHA", "DEFAULT_SETUP_UV_SHA", "DEFAULT_PYTHON_VERSIONS",
                "DEFAULT_CMAKE_BUILD_COMMANDS", "DEFAULT_CMAKE_TEST_COMMANDS",
            ),
            install_github_workflows: (
                "DEFAULT_CMAKE_BUILD_COMMANDS", "DEFAULT_CMAKE_TEST_COMMANDS",
                "format_runs_on", "is_self_hosted",
            ),
        }
        for module, symbols in removed_symbols.items():
            for symbol in symbols:
                self.assertFalse(
                    hasattr(module, symbol),
                    f"{module.__name__} still exposes unused symbol {symbol}",
                )


class AgentSkillPreflightTests(unittest.TestCase):
    def test_prior_manifest_files_are_hashed_once(self):
        content = b"unchanged skill bytes"
        digest = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / ".agents" / "skills" / "demo" / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_bytes(content)

            real_sha256 = hashlib.sha256
            with mock.patch.object(
                provision_agent_skills.hashlib, "sha256", wraps=real_sha256
            ) as sha256_spy:
                provision_agent_skills._preflight_destination(
                    temp_dir, [], {"files": {"demo/SKILL.md": digest}}
                )
            self.assertEqual(sha256_spy.call_count, 1)


class SetupStageCleanupTests(unittest.TestCase):
    def test_legacy_files_and_workflows_stages_are_not_handled_by_stage_changes(self):
        from scripts import setup_github_repo

        for name in ("files", "workflows"):
            with self.subTest(stage=name):
                with self.assertRaisesRegex(ValueError, "unsupported stage"):
                    setup_github_repo._stage_changes(None, "acme/project", {"name": name})


class LinkReconciliationImportTests(unittest.TestCase):
    def test_link_reconciliation_imports_as_package_and_as_script(self):
        package_import = subprocess.run(
            [sys.executable, "-c",
             "import scripts.reconcile_github_links; import scripts.setup_repository_content; print('OK')"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(package_import.returncode, 0, package_import.stderr)
        self.assertEqual(package_import.stdout.strip(), "OK")

        script_help = subprocess.run(
            [sys.executable, "scripts/reconcile_github_links.py", "--help"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(script_help.returncode, 0, script_help.stderr)
        self.assertIn("usage", script_help.stdout.lower())


class ModuleLinterRegistrationTests(unittest.TestCase):
    def test_all_core_domain_modules_are_registered(self):
        module_names = {
            path.stem
            for path in (ROOT / "scripts").glob("*.py")
            if not path.name.startswith("_")
        }
        unregistered = {
            name for name in module_names
            if not module_linter.is_cli_module(name)
            and name != "headless"
            and name not in module_linter.CORE_DOMAIN_MODULES
        }
        self.assertEqual(unregistered, set())

    def test_verbose_discovery_count_matches_canonical_module_count(self):
        audit = module_linter.audit_dependencies(root_dir=ROOT)
        canonical_count = len({path.stem for path in audit.modules.values()})

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = module_linter.main(["--root", str(ROOT), "--verbose"])
        text = output.getvalue()
        self.assertEqual(exit_code, 0, text)
        discovered = re.search(r"Discovered (\d+) modules", text)
        self.assertIsNotNone(discovered, text)
        self.assertEqual(int(discovered.group(1)), canonical_count)
        self.assertIn(f"({canonical_count} modules,", text)


if __name__ == "__main__":
    unittest.main()
