"""Static architectural import analyzer and circular dependency linter."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Dict, List, Optional, Sequence, Set, Tuple, Union

IGNORED_DIR_NAMES = {
    ".git",
    ".github",
    ".venv",
    "venv",
    "env",
    ".env",
    ".worktrees",
    "tests",
    "__pycache__",
    "build",
    "dist",
    ".pytest_cache",
    ".eggs",
}

CORE_DOMAIN_MODULES = {
    "audit_module_dependencies",
    "check_markdown_links",
    "configure_github_repo",
    "dependabot_generator",
    "doctor",
    "doi_guidance",
    "export_release_sbom",
    "hook_contracts",
    "install_github_workflows",
    "issue_triage",
    "lint_workflow_action_pins",
    "ocr_app_auth",
    "ocr_poller",
    "ocr_preflight",
    "ocr_publisher",
    "ocr_reviewer",
    "ocr_runner",
    "ocr_safety",
    "ocr_state",
    "orchestrate_issue_frontier",
    "path_security",
    "policy_assembly",
    "policy_drift_audit",
    "provision_agent_skills",
    "publish_github_wiki",
    "reconcile_github_links",
    "release_promotion",
    "repository_capabilities",
    "review_policy",
    "review_readiness",
    "review_workflow",
    "runner_support",
    "setup_github_projects",
    "setup_github_repo",
    "setup_profiles",
    "setup_repository_content",
    "verify_agent_skill_discovery",
    "verify_release",
    "yaml_subset",
}


def is_cli_module(name: str) -> bool:
    """Return True if module is a CLI or interactive prompt module."""
    parts = name.split(".")
    short_name = parts[-1]
    if short_name == "cli" or short_name.startswith("cli_"):
        return True
    if short_name == "prompts" or short_name.startswith("prompts_"):
        return True
    if short_name.startswith("interactive"):
        return True
    return False


def is_core_domain_module(name: str) -> bool:
    """Return True if module belongs to core domain / engine logic."""
    parts = name.split(".")
    short_name = parts[-1]
    if short_name in CORE_DOMAIN_MODULES:
        return True
    # By default in synthetic tests or unlisted modules, any non-CLI module
    # (except headless runner) is treated as core domain.
    if not is_cli_module(short_name) and short_name not in ("headless",):
        return True
    return False


@dataclass(frozen=True)
class ImportRecord:
    source_module: str
    target_module: str
    file_path: Path
    lineno: int
    is_top_level: bool
    is_type_checking: bool
    imported_name: str


@dataclass
class LayeringViolation:
    source_module: str
    target_module: str
    file_path: Path
    lineno: int
    is_top_level: bool
    message: str = ""

    def __str__(self) -> str:
        level = "top-level" if self.is_top_level else "function-level"
        return (
            f"{self.file_path}:{self.lineno}: Layering violation ({level}): "
            f"core module '{self.source_module}' imports CLI/UI module '{self.target_module}'"
        )


@dataclass
class DependencyAuditResult:
    modules: Dict[str, Path]
    graph: Dict[str, Set[str]]
    cycles: List[List[str]]
    layering_violations: List[LayeringViolation]
    records: List[ImportRecord]

    @property
    def is_clean(self) -> bool:
        return len(self.cycles) == 0 and len(self.layering_violations) == 0


class ImportVisitor(ast.NodeVisitor):
    def __init__(self, source_module: str, file_path: Path, module_aliases: Dict[str, str]):
        self.source_module = source_module
        self.file_path = file_path
        self.module_aliases = module_aliases
        self.records: List[ImportRecord] = []
        self._fn_depth = 0
        self._type_checking_depth = 0

    def _is_type_checking_test(self, test_node: ast.AST) -> bool:
        if isinstance(test_node, ast.Name) and test_node.id == "TYPE_CHECKING":
            return True
        if isinstance(test_node, ast.Attribute) and test_node.attr == "TYPE_CHECKING":
            return True
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._fn_depth += 1
        self.generic_visit(node)
        self._fn_depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._fn_depth += 1
        self.generic_visit(node)
        self._fn_depth -= 1

    def visit_If(self, node: ast.If):
        if self._is_type_checking_test(node.test):
            self._type_checking_depth += 1
            for stmt in node.body:
                self.visit(stmt)
            self._type_checking_depth -= 1
            for stmt in node.orelse:
                self.visit(stmt)
        else:
            self.generic_visit(node)

    def _resolve_target(self, name: str) -> Optional[str]:
        if not name:
            return None
        if name in self.module_aliases:
            return self.module_aliases[name]
        parts = name.split(".")
        if parts[0] == "scripts" and len(parts) > 1 and parts[1] in self.module_aliases:
            return self.module_aliases[parts[1]]
        if parts[0] in self.module_aliases:
            return self.module_aliases[parts[0]]
        return None

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            target = self._resolve_target(alias.name)
            if target and target != self.source_module:
                self.records.append(
                    ImportRecord(
                        source_module=self.source_module,
                        target_module=target,
                        file_path=self.file_path,
                        lineno=node.lineno,
                        is_top_level=(self._fn_depth == 0),
                        is_type_checking=(self._type_checking_depth > 0),
                        imported_name=alias.name,
                    )
                )

    def visit_ImportFrom(self, node: ast.ImportFrom):
        mod = node.module or ""
        if mod == "scripts":
            for alias in node.names:
                target = self._resolve_target(alias.name)
                if target and target != self.source_module:
                    self.records.append(
                        ImportRecord(
                            source_module=self.source_module,
                            target_module=target,
                            file_path=self.file_path,
                            lineno=node.lineno,
                            is_top_level=(self._fn_depth == 0),
                            is_type_checking=(self._type_checking_depth > 0),
                            imported_name=f"{mod}.{alias.name}",
                        )
                    )
            return

        target = self._resolve_target(mod)
        if target and target != self.source_module:
            self.records.append(
                ImportRecord(
                    source_module=self.source_module,
                    target_module=target,
                    file_path=self.file_path,
                    lineno=node.lineno,
                    is_top_level=(self._fn_depth == 0),
                    is_type_checking=(self._type_checking_depth > 0),
                    imported_name=mod,
                )
            )


def discover_python_modules(root_dir: Path) -> Dict[str, Path]:
    """Scan and discover internal Python modules under root_dir and scripts/.

    Ignores hidden directories, tests, virtual environments, and caches.
    """
    root_dir = Path(root_dir).resolve()
    modules: Dict[str, Path] = {}

    for dirpath, dirnames, filenames in os.walk(root_dir):
        # Prune ignored directories in-place
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d not in IGNORED_DIR_NAMES
        ]

        current_path = Path(dirpath)
        try:
            rel = current_path.relative_to(root_dir)
        except ValueError:
            continue

        if rel.parts and (rel.parts[0].startswith(".") or rel.parts[0] in IGNORED_DIR_NAMES):
            continue

        for filename in filenames:
            if not filename.endswith(".py") or filename.startswith("."):
                continue
            if filename.startswith("test_") or filename.endswith("_test.py"):
                continue

            file_path = current_path / filename
            stem = file_path.stem
            rel_file = file_path.relative_to(root_dir)
            dotted = ".".join(rel_file.with_suffix("").parts)

            modules[stem] = file_path
            modules[dotted] = file_path

    return modules


def extract_module_imports(
    modules: Dict[str, Path],
) -> Tuple[Dict[str, str], List[ImportRecord]]:
    """Extract AST import records for all discovered modules."""
    module_aliases: Dict[str, str] = {}
    canonical_files: Dict[str, Path] = {}

    for key, path in modules.items():
        stem = path.stem
        module_aliases[key] = stem
        module_aliases[stem] = stem
        canonical_files[stem] = path

    records: List[ImportRecord] = []
    for stem, path in sorted(canonical_files.items()):
        try:
            content = path.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(path))
        except (OSError, SyntaxError) as error:
            print(f"Warning: could not parse {path}: {error}", file=sys.stderr)
            continue

        visitor = ImportVisitor(source_module=stem, file_path=path, module_aliases=module_aliases)
        visitor.visit(tree)
        records.extend(visitor.records)

    return module_aliases, records


def build_dependency_graph(
    discovered_modules: Dict[str, Path],
    records: List[ImportRecord],
    top_level_only: bool = True,
) -> Dict[str, Set[str]]:
    """Build directed graph of internal module dependencies."""
    canonical_modules = {path.stem for path in discovered_modules.values()}
    graph: Dict[str, Set[str]] = {mod: set() for mod in canonical_modules}

    for record in records:
        if record.is_type_checking:
            continue
        if top_level_only and not record.is_top_level:
            continue
        if record.source_module in graph and record.target_module in graph:
            graph[record.source_module].add(record.target_module)

    return graph


def find_cycles(graph: Dict[str, Set[str]]) -> List[List[str]]:
    """Find circular dependency cycles in directed graph.

    Returns deterministic list of elementary cycles, e.g. [['a', 'b', 'a']].
    """
    cycles: List[List[str]] = []
    seen_canonical_cycles: Set[Tuple[str, ...]] = set()

    # Detect direct self-loops first
    for node in sorted(graph.keys()):
        if node in graph.get(node, set()):
            cycle = [node, node]
            cycles.append(cycle)
            seen_canonical_cycles.add((node,))

    def dfs(current: str, path: List[str], visited_in_path: Set[str]):
        for neighbor in sorted(graph.get(current, set())):
            if neighbor == current:
                continue
            if neighbor in visited_in_path:
                idx = path.index(neighbor)
                cycle_nodes = path[idx:]
                min_idx = cycle_nodes.index(min(cycle_nodes))
                canonical = tuple(cycle_nodes[min_idx:] + cycle_nodes[:min_idx])
                if canonical not in seen_canonical_cycles:
                    seen_canonical_cycles.add(canonical)
                    cycles.append(list(canonical) + [canonical[0]])
            elif neighbor not in path:
                dfs(neighbor, path + [neighbor], visited_in_path | {neighbor})

    for node in sorted(graph.keys()):
        dfs(node, [node], {node})

    return sorted(cycles)


def check_layering_violations(
    records: List[ImportRecord],
) -> List[LayeringViolation]:
    """Check architectural layering boundary rules."""
    violations: List[LayeringViolation] = []
    seen: Set[Tuple[str, str, int]] = set()

    for rec in records:
        if rec.is_type_checking:
            continue
        if is_core_domain_module(rec.source_module) and is_cli_module(rec.target_module):
            key = (rec.source_module, rec.target_module, rec.lineno)
            if key not in seen:
                seen.add(key)
                violations.append(
                    LayeringViolation(
                        source_module=rec.source_module,
                        target_module=rec.target_module,
                        file_path=rec.file_path,
                        lineno=rec.lineno,
                        is_top_level=rec.is_top_level,
                    )
                )

    return violations


def audit_dependencies(
    root_dir: Union[str, Path] = ".",
    verbose: bool = False,
    include_deferred: bool = False,
) -> DependencyAuditResult:
    """Audit architectural module dependencies and circular dependencies."""
    root_path = Path(root_dir).resolve()
    modules = discover_python_modules(root_path)
    module_aliases, records = extract_module_imports(modules)

    graph = build_dependency_graph(
        discovered_modules=modules,
        records=records,
        top_level_only=not include_deferred,
    )
    cycles = find_cycles(graph)
    violations = check_layering_violations(records)

    return DependencyAuditResult(
        modules=modules,
        graph=graph,
        cycles=cycles,
        layering_violations=violations,
        records=records,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint for module dependency linter."""
    parser = argparse.ArgumentParser(
        description="Audit architectural module dependencies and cycle detection using AST."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Root directory of repository to audit (default: current working directory)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Display verbose dependency edges and discovered modules",
    )
    parser.add_argument(
        "--include-deferred",
        action="store_true",
        help="Include deferred function-level imports in cycle detection graph",
    )
    args = parser.parse_args(argv)

    result = audit_dependencies(
        root_dir=args.root,
        verbose=args.verbose,
        include_deferred=args.include_deferred,
    )

    if args.verbose:
        print(f"Discovered {len(result.modules)} modules under {args.root}")
        for mod, deps in sorted(result.graph.items()):
            if deps:
                print(f"  {mod} -> {', '.join(sorted(deps))}")

    if result.is_clean:
        total_edges = sum(len(deps) for deps in result.graph.values())
        canonical_count = len({p.stem for p in result.modules.values()})
        print(
            f"Architectural module dependency audit passed "
            f"({canonical_count} modules, {total_edges} dependencies, "
            f"0 cycles, 0 layering violations)."
        )
        return 0

    print("Architectural module dependency audit failed!\n", file=sys.stderr)
    if result.cycles:
        print("Detected Circular Dependencies:", file=sys.stderr)
        for cycle in result.cycles:
            print(f"  Cycle: {' -> '.join(cycle)}", file=sys.stderr)
        print("", file=sys.stderr)

    if result.layering_violations:
        print("Detected Layering Violations:", file=sys.stderr)
        for v in result.layering_violations:
            print(f"  {v}", file=sys.stderr)
        print("", file=sys.stderr)

    print(
        "Diagnostics: Core domain modules must remain independent of CLI/prompts presentation layers.",
        file=sys.stderr,
    )
    print(
        "Cycles between internal modules must be refactored into a directed acyclic hierarchy.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
