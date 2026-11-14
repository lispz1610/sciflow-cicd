"""Reject floating external action references in GitHub Actions workflows."""

import argparse
from pathlib import Path
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs PyYAML
    yaml = None


FULL_SHA_REFERENCE = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*@[0-9a-fA-F]{40}$"
)


def _location_parts(value, path=()):
    """Yield each uses value with a readable YAML path and optional step name."""
    if isinstance(value, dict):
        step_name = value.get("name")
        for key, child in value.items():
            child_path = (*path, str(key))
            if key == "uses":
                location = ".".join(path) or "document"
                if step_name:
                    location += f' (step "{step_name}")'
                yield location, child
            else:
                yield from _location_parts(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _location_parts(child, (*path, f"[{index}]"))


def audit_workflow_content(content_str, filename="workflow.yml"):
    """Audit workflow content string for unsafe Action references and missing explicit permissions."""
    findings = []
    if yaml is not None:
        try:
            workflow = yaml.load(content_str, Loader=yaml.BaseLoader)
        except Exception as error:
            return [f"{filename}: could not parse workflow: {error}"]
        if not isinstance(workflow, dict):
            return [f"{filename}: workflow must contain a YAML mapping"]

        for location, reference in _location_parts(workflow):
            if not isinstance(reference, str):
                findings.append(f"{filename}: {location}: uses reference must be a string")
            elif reference.startswith("./") and len(reference) > 2:
                continue
            elif reference.startswith("docker://"):
                continue
            elif not FULL_SHA_REFERENCE.fullmatch(reference):
                findings.append(f"{filename}: {location}: unsafe uses reference {reference!r}")

        has_top_permissions = "permissions" in workflow
        jobs = workflow.get("jobs", {})
        if not isinstance(jobs, dict) or not jobs:
            findings.append(f"{filename}: workflow must define at least one job")
        else:
            all_jobs_have_permissions = all(
                isinstance(job, dict) and "permissions" in job
                for job in jobs.values()
            )
            if not has_top_permissions and not all_jobs_have_permissions:
                findings.append(f"{filename}: missing explicit permissions declaration (declare permissions at workflow or job level)")

        top_perms = workflow.get("permissions")
        if top_perms in ("write-all", "read-all"):
            findings.append(f"{filename}: overly broad permissions {top_perms!r}; declare minimal scopes")
    else:
        has_perms = False
        for line in content_str.splitlines():
            match = re.search(r"^\s*uses:\s*['\"]?([^'\"#\s]+)", line)
            if match:
                ref = match.group(1).strip()
                if not (ref.startswith("./") or ref.startswith("docker://") or FULL_SHA_REFERENCE.fullmatch(ref)):
                    findings.append(f"{filename}: unsafe uses reference {ref!r}")
            if re.search(r"^\s*permissions:", line):
                has_perms = True
            if re.search(r"^\s*permissions:\s*['\"]?(write-all|read-all)['\"]?", line):
                findings.append(f"{filename}: overly broad permissions; declare minimal scopes")
        if not has_perms:
            findings.append(f"{filename}: missing explicit permissions declaration")

    return findings


def audit_workflow_permissions(workflow_dir):
    """Return errors for workflows missing explicit least-privilege permissions."""
    workflow_dir = Path(workflow_dir)
    findings = []
    if yaml is None:
        return ["PyYAML is required to audit workflow files"]

    workflow_paths = (*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml"))
    for path in sorted(workflow_paths):
        try:
            workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        except (OSError, yaml.YAMLError) as error:
            findings.append(f"{path}: could not parse workflow: {error}")
            continue
        if not isinstance(workflow, dict):
            findings.append(f"{path}: workflow must contain a YAML mapping")
            continue

        has_top_permissions = "permissions" in workflow
        jobs = workflow.get("jobs", {})
        if not isinstance(jobs, dict) or not jobs:
            findings.append(f"{path}: workflow must define at least one job")
            continue

        all_jobs_have_permissions = all(
            isinstance(job, dict) and "permissions" in job
            for job in jobs.values()
        )
        if not has_top_permissions and not all_jobs_have_permissions:
            findings.append(f"{path}: missing explicit permissions declaration (declare permissions at workflow or job level)")

        top_perms = workflow.get("permissions")
        if top_perms in ("write-all", "read-all"):
            findings.append(f"{path}: overly broad permissions {top_perms!r}; declare minimal scopes")
    return findings


def audit_workflows(workflow_dir, check_permissions=True):
    """Return errors for unreadable workflows or unsafe `uses:` references."""
    workflow_dir = Path(workflow_dir)
    findings = []
    if yaml is None:
        return ["PyYAML is required to audit workflow files"]

    workflow_paths = (*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml"))
    for path in sorted(workflow_paths):
        try:
            content_str = path.read_text(encoding="utf-8")
        except OSError as error:
            findings.append(f"{path}: could not read workflow: {error}")
            continue
        try:
            workflow = yaml.load(content_str, Loader=yaml.BaseLoader)
        except yaml.YAMLError as error:
            findings.append(f"{path}: could not parse workflow: {error}")
            continue
        if not isinstance(workflow, dict):
            findings.append(f"{path}: workflow must contain a YAML mapping")
            continue

        for location, reference in _location_parts(workflow):
            if not isinstance(reference, str):
                findings.append(f"{path}: {location}: uses reference must be a string")
            elif reference.startswith("./") and len(reference) > 2:
                continue
            elif reference.startswith("docker://"):
                continue
            elif not FULL_SHA_REFERENCE.fullmatch(reference):
                findings.append(f"{path}: {location}: unsafe uses reference {reference!r}")

        if check_permissions:
            has_top_permissions = "permissions" in workflow
            jobs = workflow.get("jobs", {})
            if not isinstance(jobs, dict) or not jobs:
                findings.append(f"{path}: workflow must define at least one job")
                continue
            all_jobs_have_permissions = all(
                isinstance(job, dict) and "permissions" in job
                for job in jobs.values()
            )
            if not has_top_permissions and not all_jobs_have_permissions:
                findings.append(f"{path}: missing explicit permissions declaration (declare permissions at workflow or job level)")
            top_perms = workflow.get("permissions")
            if top_perms in ("write-all", "read-all"):
                findings.append(f"{path}: overly broad permissions {top_perms!r}; declare minimal scopes")

    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workflow-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".github" / "workflows",
        help="directory containing workflow .yml and .yaml files",
    )
    parser.add_argument(
        "--no-check-permissions",
        action="store_true",
        help="skip auditing explicit workflow permissions",
    )
    args = parser.parse_args(argv)

    findings = audit_workflows(args.workflow_dir, check_permissions=not args.no_check_permissions)
    if findings:
        for finding in findings:
            print(finding, file=sys.stderr)
        return 1
    print(f"All workflow action references and permissions in {args.workflow_dir} are pinned, local, and explicit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
