#!/usr/bin/env python3
#!/usr/bin/env python3
"""Create the trusted, schema-versioned artifact consumed by the report workflow."""

import argparse
import json
from pathlib import Path
import re
import sys

try:
    from scripts.documentation_freshness import CANONICAL_DOCS, audit_repository, selected_markdown_files
except ImportError:
    from documentation_freshness import CANONICAL_DOCS, audit_repository, selected_markdown_files


RELEVANT = re.compile(r"(?:^README\.md$|\.md$|^\.agents/skills/|^docs/|^scripts/|^\.github/(?:workflows/|documentation-freshness\.json$|documentation-skill-inventory\.json$)|^data/agent-skills/)", re.I)
BEHAVIOR = re.compile(r"(?:^scripts/.*\.(?:py|js)$|^\.github/workflows/.*\.ya?ml$)", re.I)


def make_report(root, *, repository, run_id, run_attempt, event, head_sha,
                pull_number=None, changed_files=(), target=False, mode="audit"):
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository identity is invalid")
    if type(run_id) is not int or run_id <= 0 or type(run_attempt) is not int or run_attempt <= 0:
        raise ValueError("producer run identity is invalid")
    if event not in ("pull_request", "push", "workflow_dispatch") or not re.fullmatch(r"[0-9a-f]{40}", head_sha or ""):
        raise ValueError("producer event or source revision is invalid")
    if (event == "pull_request") != (type(pull_number) is int and pull_number > 0):
        raise ValueError("pull request scope is invalid")
    safe_changes = []
    for path in changed_files:
        if (not isinstance(path, str) or path.startswith("/") or "\\" in path or
                any(part in ("", ".", "..") for part in path.split("/"))):
            raise ValueError("changed-file inventory has an unsafe path")
        safe_changes.append(path)
    relevant = event == "workflow_dispatch" or any(RELEVANT.search(path) for path in safe_changes)
    scope = ({"kind": "pull_request", "number": pull_number, "head_sha": head_sha}
             if event == "pull_request" else {"kind": "default_branch", "head_sha": head_sha})
    findings = []
    checked_files = []
    if relevant:
        findings = [{"path": path, "line": line, "code": code, "message": message}
                    for path, line, code, message in audit_repository(root, target=target)]
        checked_files = sorted(path.relative_to(Path(root).resolve()).as_posix()
                               for path in selected_markdown_files(root, target=target))
        if len(checked_files) > 500 or len(findings) > 200:
            raise ValueError("documentation audit exceeds the bounded report inventory")
        behavior_changed = any(BEHAVIOR.search(path) for path in safe_changes)
        if target:
            scoped_docs = {path.relative_to(Path(root).resolve()).as_posix()
                           for path in selected_markdown_files(root, target=True)}
        else:
            scoped_docs = set(CANONICAL_DOCS)
        docs_changed = any(path in scoped_docs for path in safe_changes)
        if behavior_changed and not docs_changed:
            findings.append({"path": "README.md", "line": 0, "code": "semantic-review-required",
                             "message": "Behavior changed without a documentation change; maintainer semantic review is required"})
        if ".github/documentation-freshness.json" in safe_changes:
            findings.append({"path": ".github/documentation-freshness.json", "line": 0,
                             "code": "documentation-scope-review-required",
                             "message": "The declared target documentation scope changed; maintainer review is required"})
    status = "noop" if not relevant else ("findings" if findings else "clean")
    return {"schema_version": 1,
            "producer": {"workflow": "documentation-audit", "run_id": run_id,
                         "run_attempt": run_attempt, "event": event, "repository": repository},
            "scope": scope, "status": status, "checked_files": checked_files,
            "findings": findings}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--pull-number", type=int)
    parser.add_argument("--changed-files", type=Path)
    parser.add_argument("--target", action="store_true")
    args = parser.parse_args(argv)
    try:
        changed = json.loads(args.changed_files.read_text(encoding="utf-8")) if args.changed_files else []
        if not isinstance(changed, list):
            raise ValueError("changed-file inventory must be an array")
        report = make_report(args.root, repository=args.repository, run_id=args.run_id,
                             run_attempt=args.run_attempt, event=args.event, head_sha=args.head_sha,
                             pull_number=args.pull_number, changed_files=changed, target=args.target)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ValueError) as error:
        print(f"Documentation audit failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
