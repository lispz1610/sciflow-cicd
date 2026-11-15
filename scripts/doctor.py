"""Runtime diagnostics, environment preflight checks, and inventory repository control audits."""

import argparse
import base64
from dataclasses import dataclass
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Tuple


@dataclass
class DiagnosticResult:
    """Diagnostic check result with severity classification."""
    name: str
    passed: bool
    message: str
    is_fatal: bool = True


def check_python_version() -> DiagnosticResult:
    """Check if Python version is >= 3.10."""
    version = sys.version_info
    ver_str = f"{version.major}.{version.minor}.{version.micro}"
    if version >= (3, 10):
        return DiagnosticResult(
            name="Python Version",
            passed=True,
            message=f"Python {ver_str} (>= 3.10)",
            is_fatal=True,
        )
    return DiagnosticResult(
        name="Python Version",
        passed=False,
        message=f"Python {ver_str} is outdated (< 3.10). Please upgrade to Python 3.10+.",
        is_fatal=True,
    )


def check_git_installed() -> DiagnosticResult:
    """Check if git is available."""
    git_path = shutil.which("git")
    if not git_path:
        return DiagnosticResult(
            name="Git Installation",
            passed=False,
            message="git was not found in PATH. Please install git.",
            is_fatal=True,
        )
    try:
        res = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True)
        return DiagnosticResult(
            name="Git Installation",
            passed=True,
            message=res.stdout.strip(),
            is_fatal=True,
        )
    except Exception as exc:
        return DiagnosticResult(
            name="Git Installation",
            passed=False,
            message=f"git failed to execute: {exc}",
            is_fatal=True,
        )


def check_gh_installed_and_authenticated() -> DiagnosticResult:
    """Check if GitHub CLI (gh) is installed and authenticated."""
    gh_path = shutil.which("gh")
    if not gh_path:
        return DiagnosticResult(
            name="GitHub CLI (gh)",
            passed=False,
            message="GitHub CLI (gh) was not found in PATH. Install from https://cli.github.com",
            is_fatal=True,
        )

    try:
        res = subprocess.run([gh_path, "auth", "status"], capture_output=True, text=True)
        if res.returncode == 0:
            has_env_token = bool(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
            token_src = "environment token" if has_env_token else "saved account"
            return DiagnosticResult(
                name="GitHub CLI (gh)",
                passed=True,
                message=f"GitHub CLI is authenticated via {token_src}",
                is_fatal=True,
            )
        return DiagnosticResult(
            name="GitHub CLI (gh)",
            passed=False,
            message="GitHub CLI is not authenticated. Run 'gh auth login' or export GH_TOKEN.",
            is_fatal=True,
        )
    except Exception as exc:
        return DiagnosticResult(
            name="GitHub CLI (gh)",
            passed=False,
            message=f"GitHub CLI auth check failed: {exc}",
            is_fatal=True,
        )


def check_path_precedence(
    bin_dir: Optional[Path] = None, command_name: str = "sciflow-cicd"
) -> DiagnosticResult:
    """Check if user binary directory is in PATH and has precedence."""
    if bin_dir is not None:
        target_bin = bin_dir
    elif os.environ.get("SCIFLOW_CICD_BIN_DIR"):
        target_bin = Path(os.environ["SCIFLOW_CICD_BIN_DIR"]).expanduser()
    else:
        target_bin = Path.home() / ".local" / "bin"
    path_env = os.environ.get("PATH", "")
    paths = [p for p in path_env.split(":") if p]

    if str(target_bin) not in paths:
        return DiagnosticResult(
            name="PATH Configuration",
            passed=False,
            message=f"{target_bin} is not in your PATH. Add 'export PATH=\"{target_bin}:$PATH\"' to your profile.",
            is_fatal=False,
        )

    which_bin = shutil.which(command_name)
    expected_bin = target_bin / command_name
    if which_bin and Path(which_bin).resolve() != expected_bin.resolve():
        return DiagnosticResult(
            name="PATH Configuration",
            passed=False,
            message=f"{target_bin} is in PATH, but shadowed by {which_bin}. Give {target_bin} precedence.",
            is_fatal=False,
        )

    return DiagnosticResult(
        name="PATH Configuration",
        passed=True,
        message=f"{target_bin} is active in PATH",
        is_fatal=False,
    )


def audit_merge_controls(inv: dict, policy: Optional[dict] = None) -> Dict[str, Any]:
    """Audit merge settings against desired policy baseline."""
    desired_merge = {
        "allow_squash_merge": True,
        "delete_branch_on_merge": True,
    }
    if policy:
        merge_stage = next((s for s in policy.get("stages", []) if s.get("name") == "merge"), None)
        if merge_stage and isinstance(merge_stage.get("settings"), dict):
            desired_merge = merge_stage["settings"]

    live_merge = inv.get("merge", {})
    mismatches = []
    for k, v in desired_merge.items():
        if live_merge.get(k) != v:
            mismatches.append(f"{k} (current={live_merge.get(k)!r}, desired={v!r})")

    if not mismatches:
        return {
            "name": "merge_controls",
            "category": "Merge Controls",
            "status": "configured",
            "reason": "Merge controls match desired policy baseline",
            "guidance": None,
        }
    return {
        "name": "merge_controls",
        "category": "Merge Controls",
        "status": "missing",
        "reason": f"Merge settings mismatch: {', '.join(mismatches)}",
        "guidance": "Configure merge controls in GitHub Settings > General > Pull Requests",
    }


def audit_workflow_permissions(inv: dict) -> Dict[str, Any]:
    """Audit default workflow permissions for least-privilege read permissions."""
    wf_perms = inv.get("workflow_permissions", {})
    if not isinstance(wf_perms, dict) or wf_perms.get("status") == "inaccessible":
        return {
            "name": "default_workflow_permissions",
            "category": "Workflow Security",
            "status": "inaccessible",
            "reason": "Token lacks permission to inspect default workflow permissions",
            "guidance": "Configure workflow permissions in Settings > Actions > General or grant 'Actions: read' token permission",
        }
    if wf_perms.get("default_workflow_permissions") == "read" and not wf_perms.get("can_approve_pull_request_reviews"):
        return {
            "name": "default_workflow_permissions",
            "category": "Workflow Security",
            "status": "configured",
            "reason": "Default workflow token permissions are restricted to read-only with PR approval disabled",
            "guidance": None,
        }
    return {
        "name": "default_workflow_permissions",
        "category": "Workflow Security",
        "status": "missing",
        "reason": f"Workflow permissions are permissive (default_workflow_permissions={wf_perms.get('default_workflow_permissions')!r}, can_approve_pull_request_reviews={wf_perms.get('can_approve_pull_request_reviews')!r})",
        "guidance": "Set default workflow permissions to read-only and disable PR approvals in Settings > Actions > General > Workflow permissions",
    }


audit_workflow_permissions_control = audit_workflow_permissions


def audit_action_sha_pins(api: Any, repository: str) -> Dict[str, Any]:
    """Audit workflow files for 40-character commit SHA pins and explicit permissions."""
    try:
        from scripts.lint_workflow_action_pins import audit_workflow_content
    except ImportError:
        from lint_workflow_action_pins import audit_workflow_content

    try:
        wf_status, wf_contents = api.request("GET", f"/repos/{repository}/contents/.github/workflows")
        if wf_status == 404:
            return {
                "name": "action_sha_pins",
                "category": "Workflow Security",
                "status": "missing",
                "reason": "No workflows directory (.github/workflows) found in repository",
                "guidance": "Install required CI workflows with pinned action SHAs in .github/workflows",
            }
        if wf_status == 403:
            return {
                "name": "action_sha_pins",
                "category": "Workflow Security",
                "status": "inaccessible",
                "reason": "Token lacks permission to read .github/workflows",
                "guidance": "Grant 'Contents: read' token permission to inspect workflow files",
            }
        if wf_status == 200 and isinstance(wf_contents, list):
            wf_files = [f for f in wf_contents if isinstance(f, dict) and f.get("name", "").endswith((".yml", ".yaml"))]
            if not wf_files:
                return {
                    "name": "action_sha_pins",
                    "category": "Workflow Security",
                    "status": "missing",
                    "reason": "No .yml or .yaml files found in .github/workflows",
                    "guidance": "Install required CI workflows in .github/workflows",
                }
            pin_violations = []
            for item in wf_files:
                path = item.get("path") or f".github/workflows/{item.get('name')}"
                status_c, body_c = api.request("GET", f"/repos/{repository}/contents/{path}")
                if status_c == 200 and isinstance(body_c, dict) and "content" in body_c:
                    try:
                        content_str = base64.b64decode(body_c["content"]).decode("utf-8", errors="replace")
                    except Exception:
                        content_str = ""
                    violations = audit_workflow_content(content_str, filename=item.get("name", "workflow.yml"))
                    pin_violations.extend(violations)
            if pin_violations:
                return {
                    "name": "action_sha_pins",
                    "category": "Workflow Security",
                    "status": "missing",
                    "reason": f"Workflow security violations detected: {'; '.join(pin_violations[:3])}",
                    "guidance": "Pin all external Actions to full 40-character commit SHAs and declare minimal permissions",
                }
            return {
                "name": "action_sha_pins",
                "category": "Workflow Security",
                "status": "configured",
                "reason": f"All workflows ({len(wf_files)} active) enforce 40-character commit SHA pins and explicit permissions",
                "guidance": None,
            }
        return {
            "name": "action_sha_pins",
            "category": "Workflow Security",
            "status": "missing",
            "reason": f"Cannot inspect workflows in .github/workflows (HTTP {wf_status})",
            "guidance": "Inspect .github/workflows in repository Settings and source",
        }
    except Exception as exc:
        return {
            "name": "action_sha_pins",
            "category": "Workflow Security",
            "status": "inaccessible" if "HTTP 403" in str(exc) else "missing",
            "reason": f"Failed to inspect workflow pins: {exc}",
            "guidance": "Verify repository contents read access",
        }


def audit_dependabot_config(api: Any, repository: str) -> Dict[str, Any]:
    """Audit Dependabot configuration file and update grouping."""
    try:
        dep_status, dep_body = api.request("GET", f"/repos/{repository}/contents/.github/dependabot.yml")
        if dep_status == 200 and isinstance(dep_body, dict) and "content" in dep_body:
            try:
                dep_text = base64.b64decode(dep_body["content"]).decode("utf-8", errors="replace")
            except Exception:
                dep_text = ""
            has_groups = "groups:" in dep_text
            msg = "Dependabot configuration (.github/dependabot.yml) is active with update grouping" if has_groups else "Dependabot configuration (.github/dependabot.yml) is active"
            return {
                "name": "dependabot",
                "category": "Dependency Governance",
                "status": "configured",
                "reason": msg,
                "guidance": None,
            }
        if dep_status == 404:
            return {
                "name": "dependabot",
                "category": "Dependency Governance",
                "status": "missing",
                "reason": "Dependabot configuration (.github/dependabot.yml) is not present",
                "guidance": "Configure .github/dependabot.yml with monthly cadence and grouped updates via sciflow-cicd",
            }
        if dep_status == 403:
            return {
                "name": "dependabot",
                "category": "Dependency Governance",
                "status": "inaccessible",
                "reason": "Token lacks permission to inspect .github/dependabot.yml",
                "guidance": "Grant 'Contents: read' token permission",
            }
        return {
            "name": "dependabot",
            "category": "Dependency Governance",
            "status": "missing",
            "reason": f"Cannot verify .github/dependabot.yml (HTTP {dep_status})",
            "guidance": "Configure .github/dependabot.yml with restrained grouping via sciflow-cicd",
        }
    except Exception as exc:
        return {
            "name": "dependabot",
            "category": "Dependency Governance",
            "status": "inaccessible" if "HTTP 403" in str(exc) else "missing",
            "reason": f"Failed to inspect Dependabot configuration: {exc}",
            "guidance": "Verify repository contents read access",
        }


def audit_security_capabilities(inv: dict) -> List[Dict[str, Any]]:
    """Audit repository security capabilities from inventory inspection."""
    controls = []
    caps = inv.get("capabilities", {})
    cap_categories = {
        "rulesets": ("branch_rulesets", "Branch Rulesets & Merge Gates"),
        "vulnerability_alerts": ("vulnerability_alerts", "Vulnerability Alerts"),
        "secret_scanning": ("secret_scanning", "Secret Scanning"),
        "codeql": ("codeql", "CodeQL Default Setup"),
        "dependency_graph": ("dependency_graph", "Dependency Graph"),
        "dependency_review": ("dependency_review", "Dependency Review Gate"),
    }
    for cap_key, (ctl_name, ctl_cat) in cap_categories.items():
        cap_val = caps.get(cap_key)
        if isinstance(cap_val, dict):
            status = cap_val.get("status", "missing")
            reason = cap_val.get("reason", "No reason provided")
            guidance = cap_val.get("guidance")
        else:
            status = "missing"
            reason = f"{ctl_cat} not evaluated"
            guidance = None
        controls.append({
            "name": ctl_name,
            "category": ctl_cat,
            "status": status,
            "reason": reason,
            "guidance": guidance,
        })
    return controls


def audit_repository_controls(api: Any, repository: str, policy: Optional[dict] = None) -> dict:
    """Audit target repository security, merge controls, and permissions via inventory inspection."""
    try:
        from scripts.setup_github_repo import inventory
    except ImportError:
        from setup_github_repo import inventory

    # 1. Inspect repository inventory
    try:
        inv = inventory(api, repository)
    except Exception as exc:
        reason = str(exc)
        status = "inaccessible" if "HTTP 403" in reason or "401" in reason else "missing"
        return {
            "repository": repository,
            "verdict": "action_required",
            "summary": {
                "configured": 0,
                "missing": 1 if status == "missing" else 0,
                "unsupported": 0,
                "inaccessible": 1 if status == "inaccessible" else 0,
            },
            "controls": [
                {
                    "name": "repository_access",
                    "category": "Repository Access",
                    "status": status,
                    "reason": f"Cannot inspect repository {repository}: {reason}",
                    "guidance": "Verify token permissions and repository visibility (Administration: read, Contents: read)",
                }
            ],
        }

    controls: List[Dict[str, Any]] = [
        audit_merge_controls(inv, policy=policy),
        audit_workflow_permissions(inv),
        audit_action_sha_pins(api, repository),
        audit_dependabot_config(api, repository),
        *audit_security_capabilities(inv),
    ]

    summary = {
        "configured": sum(1 for c in controls if c["status"] == "configured"),
        "missing": sum(1 for c in controls if c["status"] == "missing"),
        "unsupported": sum(1 for c in controls if c["status"] == "unsupported"),
        "inaccessible": sum(1 for c in controls if c["status"] == "inaccessible"),
    }
    verdict = "healthy" if summary["missing"] == 0 and summary["inaccessible"] == 0 else "action_required"

    return {
        "repository": repository,
        "verdict": verdict,
        "summary": summary,
        "controls": controls,
    }


def format_doctor_report(audit_result: dict) -> str:
    """Format structured audit results into a readable terminal diagnostic report."""
    name_titles = {
        "merge_controls": "Merge Controls",
        "default_workflow_permissions": "Default Workflow Permissions",
        "action_sha_pins": "Action SHA Pins",
        "dependabot": "Dependabot Configuration",
        "branch_rulesets": "Branch Rulesets & Merge Gates",
        "vulnerability_alerts": "Vulnerability Alerts",
        "secret_scanning": "Secret Scanning",
        "codeql": "CodeQL Default Setup",
        "dependency_graph": "Dependency Graph",
        "dependency_review": "Dependency Review Gate",
    }
    lines = [
        "============================================================",
        "sciflow-cicd Doctor: Repository Security & Merge Controls Audit",
        f"Repository: {audit_result.get('repository', 'unknown')}",
        "============================================================",
        "",
    ]
    for ctl in audit_result.get("controls", []):
        tag = ctl["status"].upper()
        title = name_titles.get(ctl.get("name"), ctl.get("category", "Control"))
        lines.append(f"[{tag}] {title}: {ctl['reason']}")
        if ctl.get("guidance"):
            lines.append(f"  Action: {ctl['guidance']}")
    lines.append("")
    lines.append("============================================================")
    s = audit_result.get("summary", {})
    lines.append(
        f"Summary: {s.get('configured', 0)} CONFIGURED, "
        f"{s.get('missing', 0)} MISSING, "
        f"{s.get('unsupported', 0)} UNSUPPORTED, "
        f"{s.get('inaccessible', 0)} INACCESSIBLE"
    )
    if audit_result.get("verdict") == "healthy":
        lines.append("Verdict: System is healthy (all supported controls configured).")
    else:
        lines.append("Verdict: Action required to resolve missing or inaccessible controls.")
    return "\n".join(lines) + "\n"


def run_doctor(
    out_stream: Optional[TextIO] = None,
    err_stream: Optional[TextIO] = None,
    bin_dir: Optional[Path] = None,
    repo: Optional[str] = None,
    policy_path: Optional[str] = None,
    json_output: bool = False,
    token_env: str = "GITHUB_TOKEN",
    api: Any = None,
    command_name: str = "sciflow-cicd",
) -> int:
    """Run all diagnostic preflight checks or repository security controls audit."""
    out_s = out_stream if out_stream is not None else sys.stdout
    err_s = err_stream if err_stream is not None else sys.stderr

    if repo:
        if api is None:
            token = os.environ.get(token_env) or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            if not token:
                try:
                    res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
                    if res.returncode == 0 and res.stdout.strip():
                        token = res.stdout.strip()
                except Exception:
                    pass
            if not token:
                err_s.write(f"Error: {token_env} is not set; authentication token required to audit repository.\n")
                return 1
            try:
                from scripts.configure_github_repo import GitHubApi
            except ImportError:
                from configure_github_repo import GitHubApi
            api = GitHubApi(token)

        policy = None
        if policy_path:
            p_file = Path(policy_path)
            if not p_file.is_file():
                err_s.write(f"Error: Policy file not found: {policy_path}\n")
                return 1
            try:
                policy = json.loads(p_file.read_text(encoding="utf-8"))
            except Exception as exc:
                err_s.write(f"Error: Failed to parse policy file {policy_path}: {exc}\n")
                return 1

        audit_res = audit_repository_controls(api, repo, policy=policy)
        if json_output:
            out_s.write(json.dumps(audit_res, indent=2) + "\n")
        else:
            out_s.write(format_doctor_report(audit_res))
        return 0 if audit_res.get("verdict") == "healthy" else 1

    out_s.write("============================================================\n")
    out_s.write("sciflow-cicd Doctor: Runtime Diagnostics and Environment Health\n")
    out_s.write("============================================================\n")

    checks = [
        check_python_version(),
        check_git_installed(),
        check_gh_installed_and_authenticated(),
        check_path_precedence(bin_dir=bin_dir, command_name=command_name),
    ]

    all_fatal_passed = True
    for res in checks:
        symbol = "[✓]" if res.passed else ("[✗]" if res.is_fatal else "[!]")
        out_s.write(f"{symbol} {res.name}: {res.message}\n")
        if not res.passed and res.is_fatal:
            all_fatal_passed = False

    out_s.write("============================================================\n")
    if all_fatal_passed:
        out_s.write("Verdict: System is healthy and ready for sciflow-cicd.\n")
        return 0
    else:
        err_s.write("Verdict: Issues detected. Please resolve the reported errors above.\n")
        err_s.write("Run 'sciflow-cicd doctor' to verify after resolving issues.\n")
        return 1


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint for standalone doctor diagnostic command."""
    parser = argparse.ArgumentParser(
        prog="sciflow-cicd doctor",
        description="Diagnose environment health or audit target repository controls."
    )
    parser.add_argument("--repo", help="Target repository OWNER/REPO to audit")
    parser.add_argument("--policy", help="Path to policy JSON document for repository audit")
    parser.add_argument("--json", action="store_true", help="Output findings as structured JSON")
    parser.add_argument("--token-env", default="GITHUB_TOKEN", help="Environment variable for GitHub token")
    parser.add_argument("--bin-dir", type=Path, help="Target binary directory for PATH check")
    args = parser.parse_args(argv)
    return run_doctor(
        bin_dir=args.bin_dir,
        repo=args.repo,
        policy_path=args.policy,
        json_output=args.json,
        token_env=args.token_env,
    )


if __name__ == "__main__":
    sys.exit(main())
