"""Unit tests for the architectural module dependency linter."""

from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

from scripts.audit_module_dependencies import (
    DependencyAuditResult,
    LayeringViolation,
    audit_dependencies,
    build_dependency_graph,
    discover_python_modules,
    find_cycles,
    main,
)


class ModuleDiscoveryTests(unittest.TestCase):
    def test_discovers_scripts_and_root_modules_ignoring_hidden_and_tests(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / "root_mod.py").write_text("# root module\n", encoding="utf-8")
            scripts_dir = temp_path / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "tool_a.py").write_text("# tool\n", encoding="utf-8")
            (scripts_dir / "tool_b.py").write_text("# tool\n", encoding="utf-8")

            # Directories that must be ignored
            hidden_dir = temp_path / ".venv" / "lib"
            hidden_dir.mkdir(parents=True)
            (hidden_dir / "ignored.py").write_text("# ignored\n", encoding="utf-8")

            tests_dir = temp_path / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_something.py").write_text("# test\n", encoding="utf-8")

            worktrees_dir = temp_path / ".worktrees" / "wt1"
            worktrees_dir.mkdir(parents=True)
            (worktrees_dir / "ignored_wt.py").write_text("# ignored\n", encoding="utf-8")

            modules = discover_python_modules(temp_path)

            self.assertIn("root_mod", modules)
            self.assertIn("tool_a", modules)
            self.assertIn("tool_b", modules)
            self.assertNotIn("ignored", modules)
            self.assertNotIn("test_something", modules)
            self.assertNotIn("ignored_wt", modules)


class CycleDetectionTests(unittest.TestCase):
    def test_finds_no_cycles_in_dag(self):
        graph = {
            "a": {"b", "c"},
            "b": {"c"},
            "c": set(),
        }
        cycles = find_cycles(graph)
        self.assertEqual(cycles, [])

    def test_detects_direct_circular_dependency(self):
        graph = {
            "a": {"b"},
            "b": {"a"},
        }
        cycles = find_cycles(graph)
        self.assertTrue(len(cycles) >= 1)
        cycle_str = " -> ".join(cycles[0])
        self.assertTrue(
            cycle_str in ("a -> b -> a", "b -> a -> b"),
            f"Expected direct cycle representation, got: {cycle_str}",
        )

    def test_detects_transitive_circular_dependency(self):
        graph = {
            "a": {"b"},
            "b": {"c"},
            "c": {"a"},
        }
        cycles = find_cycles(graph)
        self.assertTrue(len(cycles) >= 1)
        cycle_str = " -> ".join(cycles[0])
        valid_cycle_strs = ("a -> b -> c -> a", "b -> c -> a -> b", "c -> a -> b -> c")
        self.assertTrue(
            cycle_str in valid_cycle_strs,
            f"Expected transitive cycle representation, got: {cycle_str}",
        )

    def test_detects_self_cycle(self):
        graph = {
            "a": {"a"},
        }
        cycles = find_cycles(graph)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(" -> ".join(cycles[0]), "a -> a")


class LayeringViolationTests(unittest.TestCase):
    def test_detects_top_level_layering_violation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            scripts_dir = temp_path / "scripts"
            scripts_dir.mkdir()

            (scripts_dir / "prompts.py").write_text("def prompt(): pass\n", encoding="utf-8")
            (scripts_dir / "configure_github_repo.py").write_text(
                "import scripts.prompts\n",
                encoding="utf-8",
            )

            result = audit_dependencies(temp_path)
            self.assertFalse(result.is_clean)
            self.assertTrue(len(result.layering_violations) >= 1)
            violation = result.layering_violations[0]
            self.assertEqual(violation.source_module, "configure_github_repo")
            self.assertEqual(violation.target_module, "prompts")
            self.assertIn("configure_github_repo", str(violation))
            self.assertIn("prompts", str(violation))

    def test_detects_function_level_layering_violation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            scripts_dir = temp_path / "scripts"
            scripts_dir.mkdir()

            (scripts_dir / "cli.py").write_text("def cli_func(): pass\n", encoding="utf-8")
            (scripts_dir / "verify_release.py").write_text(
                "def verify():\n"
                "    from scripts.cli import cli_func\n"
                "    cli_func()\n",
                encoding="utf-8",
            )

            result = audit_dependencies(temp_path)
            self.assertFalse(result.is_clean)
            self.assertTrue(len(result.layering_violations) >= 1)
            violation = result.layering_violations[0]
            self.assertEqual(violation.source_module, "verify_release")
            self.assertEqual(violation.target_module, "cli")

    def test_detects_interactive_wildcard_layering_violation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            scripts_dir = temp_path / "scripts"
            scripts_dir.mkdir()

            (scripts_dir / "interactive_review.py").write_text("def review(): pass\n", encoding="utf-8")
            (scripts_dir / "policy_assembly.py").write_text(
                "from interactive_review import review\n",
                encoding="utf-8",
            )

            result = audit_dependencies(temp_path)
            self.assertFalse(result.is_clean)
            self.assertTrue(len(result.layering_violations) >= 1)
            violation = result.layering_violations[0]
            self.assertEqual(violation.source_module, "policy_assembly")
            self.assertEqual(violation.target_module, "interactive_review")


class TypeCheckingGuardTests(unittest.TestCase):
    def test_type_checking_imports_are_excluded_from_cycle_graph(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            scripts_dir = temp_path / "scripts"
            scripts_dir.mkdir()

            (scripts_dir / "module_a.py").write_text(
                "from scripts.module_b import Helper\n",
                encoding="utf-8",
            )
            (scripts_dir / "module_b.py").write_text(
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n"
                "    from scripts.module_a import Session\n"
                "class Helper: pass\n",
                encoding="utf-8",
            )

            result = audit_dependencies(temp_path)
            self.assertTrue(result.is_clean)
            self.assertEqual(result.cycles, [])


class CliEntrypointTests(unittest.TestCase):
    def test_cli_returns_nonzero_on_violations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            scripts_dir = temp_path / "scripts"
            scripts_dir.mkdir()

            (scripts_dir / "a.py").write_text("import b\n", encoding="utf-8")
            (scripts_dir / "b.py").write_text("import a\n", encoding="utf-8")

            exit_code = main(["--root", str(temp_path)])
            self.assertEqual(exit_code, 1)

    def test_cli_returns_zero_on_clean_project(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            scripts_dir = temp_path / "scripts"
            scripts_dir.mkdir()

            (scripts_dir / "helper.py").write_text("def help_me(): pass\n", encoding="utf-8")
            (scripts_dir / "runner.py").write_text("from helper import help_me\n", encoding="utf-8")

            exit_code = main(["--root", str(temp_path)])
            self.assertEqual(exit_code, 0)


class RepositoryCleanArchitectureTests(unittest.TestCase):
    def test_repository_has_no_cycles_or_layering_violations(self):
        result = audit_dependencies(ROOT)
        diagnostics = []
        if result.cycles:
            diagnostics.append(f"Cycles: {result.cycles}")
        if result.layering_violations:
            diagnostics.append(f"Violations: {[str(v) for v in result.layering_violations]}")
        error_msg = "\n".join(diagnostics)
        self.assertTrue(result.is_clean, f"Repository dependency audit failed:\n{error_msg}")


if __name__ == "__main__":
    unittest.main()
