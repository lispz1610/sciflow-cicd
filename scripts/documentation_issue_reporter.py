#!/usr/bin/env python3
"""Publish bounded documentation audit artifacts as managed GitHub issues."""

import argparse
import html
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MARKER = "<!-- sciflow-cicd-documentation-freshness:v1:"
LEGACY_MARKER = "<!-- gh-setup-documentation-freshness:v1:"
STATUS_CONTEXT = "documentation-freshness"
MAX_FINDINGS = 200
MAX_FILES = 500
MAX_TEXT = 2000
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
ISSUE_MARKER = re.compile(r"<!-- (?:sciflow-cicd|gh-setup)-documentation-freshness:v1:([^\s]+) -->")
AUDIT_STATE = re.compile(r"Latest audit: \*\*(clean|findings|error)\*\*")
AUDIT_SHA = re.compile(r"Audited revision: `([0-9a-f]{40})`")


def _relative_path(value):
    return (isinstance(value, str) and 0 < len(value) <= 500 and
            not value.startswith(("/", "\\")) and "\\" not in value and
            all(part not in ("", ".", "..") for part in value.split("/")) and
            "\x00" not in value)


def validate_report(value):
    """Validate the artifact schema and reject unbounded or hostile paths."""
    if not isinstance(value, dict) or set(value) != {
            "schema_version", "producer", "scope", "status", "checked_files", "findings"}:
        raise ValueError("documentation report has malformed fields")
    producer = value["producer"]
    scope = value["scope"]
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1 or
            not isinstance(producer, dict) or set(producer) != {
                "workflow", "run_id", "run_attempt", "event", "repository"} or
            producer["workflow"] != "documentation-audit" or
            type(producer["run_id"]) is not int or producer["run_id"] <= 0 or
            type(producer["run_attempt"]) is not int or producer["run_attempt"] <= 0 or
            producer["event"] not in ("pull_request", "push", "workflow_dispatch") or
            not isinstance(producer["repository"], str) or not REPO.fullmatch(producer["repository"])):
        raise ValueError("documentation report producer identity is invalid")
    if not isinstance(scope, dict) or scope.get("kind") not in ("pull_request", "default_branch"):
        raise ValueError("documentation report scope is invalid")
    expected_scope = {"kind", "number", "head_sha"} if scope.get("kind") == "pull_request" else {"kind", "head_sha"}
    if (set(scope) != expected_scope or not isinstance(scope.get("head_sha"), str) or
            not SHA.fullmatch(scope["head_sha"]) or
            (scope["kind"] == "pull_request" and (type(scope.get("number")) is not int or scope["number"] <= 0))):
        raise ValueError("documentation report scope identity is invalid")
    if value["status"] not in ("clean", "findings", "error", "noop"):
        raise ValueError("documentation report status is invalid")
    checked = value["checked_files"]
    findings = value["findings"]
    if (not isinstance(checked, list) or len(checked) > MAX_FILES or
            any(not _relative_path(path) for path in checked) or len(set(checked)) != len(checked)):
        raise ValueError("documentation report checked-file list is malformed or unbounded")
    if (not isinstance(findings, list) or len(findings) > MAX_FINDINGS or
            (value["status"] == "clean" and findings) or
            (value["status"] == "findings" and not findings) or
            (value["status"] in ("clean", "noop") and findings)):
        raise ValueError("documentation report findings are malformed or unbounded")
    for finding in findings:
        if (not isinstance(finding, dict) or set(finding) != {"path", "line", "code", "message"} or
                not _relative_path(finding["path"]) or type(finding["line"]) is not int or finding["line"] < 0 or
                not isinstance(finding["code"], str) or not re.fullmatch(r"[a-z0-9-]{1,80}", finding["code"]) or
                not isinstance(finding["message"], str) or not finding["message"] or
                len(finding["message"]) > MAX_TEXT):
            raise ValueError("documentation report finding path, message, or fields are malformed")
    return value


def _identity(repo, scope):
    suffix = f"pr-{scope['number']}" if scope["kind"] == "pull_request" else "default-branch"
    return f"{repo}:{suffix}"


def _safe(value):
    return html.escape(value, quote=False).replace("`", "&#96;")


def render_body(identity, value, report_url=None, scope_url=None):
    lines = [MARKER + identity + " -->", "## Documentation freshness report", "",
             f"Latest audit: **{value['status']}**", "",
             f"Producer run: `{value['producer']['run_id']}` attempt `{value['producer']['run_attempt']}`",
             f"Audited revision: `{value['scope']['head_sha']}`", ""]
    if report_url:
        lines.append(f"[Open audit run]({report_url})")
        lines.append("")
    if scope_url:
        lines.append(f"Affected scope: [{_safe(scope_url)}]({scope_url})")
        lines.append("")
    body_limit = 55_000
    if value["checked_files"]:
        lines += ["Checked files:"]
        used = len("\n".join(lines))
        shown = 0
        for path in value["checked_files"]:
            row = f"- `{_safe(path)}`"
            if used + len(row) + 2 > body_limit - 500:
                break
            lines.append(row)
            used += len(row) + 1
            shown += 1
        omitted = len(value["checked_files"]) - shown
        if omitted:
            lines.append(f"- … {omitted} additional file(s); see the linked audit run")
        lines.append("")
    if value["findings"]:
        lines += ["Findings:"]
        used = len("\n".join(lines))
        shown = 0
        for finding in value["findings"]:
            row = f"- **{_safe(finding['code'])}** `{_safe(finding['path'])}:{finding['line']}` — {_safe(finding['message'])}"
            if used + len(row) + 2 > body_limit - 600:
                break
            lines.append(row)
            used += len(row) + 1
            shown += 1
        omitted = len(value["findings"]) - shown
        if omitted:
            lines.append(f"- … {omitted} additional finding(s); see the linked audit run")
    elif value["status"] == "error":
        lines.append("The audit could not produce a trustworthy result. Inspect the workflow run and restore its required permissions before retrying.")
    else:
        lines.append("The latest audit found no documentation freshness issues. A maintainer must close this issue after reviewing the report.")
    lines += ["", "Maintainer resolution:",
              "1. Review the findings and affected scope.",
              "2. Update or justify the documentation for the behavior and links involved.",
              "3. Rerun the audit and confirm the latest result is clean for the current revision.",
              "4. Close this issue after that review; clean reruns never close it automatically."]
    body = "\n".join(lines)
    if len(body) > 60_000:
        raise ValueError("rendered documentation report exceeded its size limit")
    return body


class GitHubApi:
    """Small REST adapter kept replaceable at the issue/status API boundary."""

    def __init__(self, token, repository):
        if not token:
            raise ValueError("GITHUB_TOKEN is required for documentation reporting")
        if not REPO.fullmatch(repository):
            raise ValueError("GITHUB_REPOSITORY must be OWNER/REPO")
        self.repository = repository
        self.base = f"https://api.github.com/repos/{repository}"
        self.token = token

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = Request("https://api.github.com" + path, data=data, method=method,
                          headers={"Accept": "application/vnd.github+json",
                                   "Authorization": f"Bearer {self.token}",
                                   "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urlopen(request, timeout=20) as response:
                payload = response.read(2_000_001)
                if len(payload) > 2_000_000:
                    raise ValueError("GitHub API response exceeded the report size limit")
                return response.status, json.loads(payload) if payload else None
        except HTTPError as error:
            detail = error.read(2000).decode("utf-8", "replace")
            raise RuntimeError(f"GitHub API {method} failed ({error.code}): {detail}") from None
        except (URLError, TimeoutError) as error:
            raise RuntimeError(f"GitHub API request failed: {error}") from None

    def current_head(self, scope):
        if scope["kind"] == "pull_request":
            status, value = self.request("GET", f"/repos/{self.repository}/pulls/{scope['number']}")
            if status != 200 or not isinstance(value, dict) or not isinstance(value.get("head"), dict):
                raise RuntimeError("cannot verify current pull request head")
            return value["head"].get("sha")
        status, repo = self.request("GET", f"/repos/{self.repository}")
        if status != 200 or not isinstance(repo, dict) or not isinstance(repo.get("default_branch"), str):
            raise RuntimeError("cannot verify default branch")
        status, branch = self.request("GET", f"/repos/{self.repository}/git/ref/heads/{repo['default_branch']}")
        if status != 200 or not isinstance(branch, dict) or not isinstance(branch.get("object"), dict):
            raise RuntimeError("cannot verify default branch head")
        return branch["object"].get("sha")

    def scope_url(self, scope):
        origin = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        if scope["kind"] == "pull_request":
            return f"{origin}/{self.repository}/pull/{scope['number']}"
        return f"{origin}/{self.repository}"

    def list_reports(self, marker):
        matches = []
        for page in range(1, 101):
            status, issues = self.request("GET", f"/repos/{self.repository}/issues?state=all&per_page=100&page={page}")
            if status != 200 or not isinstance(issues, list) or len(issues) > 100:
                raise RuntimeError("cannot verify managed documentation reports")
            matches.extend(issue for issue in issues if isinstance(issue, dict) and
                           isinstance(issue.get("body"), str) and
                           (MARKER + marker + " -->" in issue["body"] or LEGACY_MARKER + marker + " -->" in issue["body"]))
            if len(issues) < 100:
                return matches
        raise RuntimeError("documentation report lookup exceeded the pagination limit")

    def get_issue(self, number):
        status, issue = self.request("GET", f"/repos/{self.repository}/issues/{number}")
        if status != 200 or not isinstance(issue, dict):
            raise RuntimeError("cannot read documentation report issue")
        return issue

    def create_report(self, title, body, labels):
        status, issue = self.request("POST", f"/repos/{self.repository}/issues",
                                     {"title": title, "body": body, "labels": labels})
        if status != 201 or not isinstance(issue, dict):
            raise RuntimeError("cannot create managed documentation report issue")
        return issue

    def update_report(self, issue, body):
        status, _ = self.request("PATCH", f"/repos/{self.repository}/issues/{issue['number']}", {"body": body})
        if status != 200:
            raise RuntimeError("cannot refresh managed documentation report")

    def reopen_report(self, issue, body):
        status, _ = self.request("PATCH", f"/repos/{self.repository}/issues/{issue['number']}",
                                 {"body": body, "state": "open"})
        if status != 200:
            raise RuntimeError("cannot reopen managed documentation report")

    def ensure_triage_label(self, issue):
        labels = issue.get("labels") if isinstance(issue, dict) else None
        if isinstance(labels, list) and any(
                isinstance(label, dict) and label.get("name") == "needs-triage" for label in labels):
            return
        status, _ = self.request("POST", f"/repos/{self.repository}/issues/{issue['number']}/labels",
                                 {"labels": ["needs-triage"]})
        if status != 200:
            raise RuntimeError("cannot apply the canonical needs-triage label")

    def set_status(self, sha, state, description, target_url):
        status, _ = self.request("POST", f"/repos/{self.repository}/statuses/{sha}", {
            "state": state, "context": STATUS_CONTEXT, "description": description[:140],
            "target_url": target_url,
        })
        if status != 201:
            raise RuntimeError("cannot publish documentation freshness status")


def publish(api, raw_report, *, expected_repository=None, expected_run_id=None, expected_run_attempt=None, expected_event=None,
            expected_scope=None, report_url=None):
    try:
        value = validate_report(raw_report)
        producer = value["producer"]
        if expected_repository is not None and producer["repository"] != expected_repository:
            raise ValueError("documentation report repository does not match its workflow run")
        if expected_run_id is not None and producer["run_id"] != expected_run_id:
            raise ValueError("documentation report producer run does not match its workflow run")
        if expected_run_attempt is not None and producer["run_attempt"] != expected_run_attempt:
            raise ValueError("documentation report attempt does not match its workflow run")
        if expected_event is not None and producer["event"] != expected_event:
            raise ValueError("documentation report event does not match its workflow run")
        if expected_scope is not None and value["scope"] != expected_scope:
            raise ValueError("documentation report scope does not match its workflow event")
    except Exception:
        fallback_sha = expected_scope.get("head_sha") if isinstance(expected_scope, dict) else None
        if isinstance(fallback_sha, str) and SHA.fullmatch(fallback_sha):
            try:
                api.set_status(fallback_sha, "failure", "Documentation artifact identity is invalid", report_url)
            except Exception:
                pass
        raise
    if value["status"] == "noop":
        return {"issue": None, "status": "noop"}
    scope = value["scope"]
    repo = expected_repository or producer["repository"]
    identity = _identity(repo, scope)
    try:
        current = api.current_head(scope)
        if not isinstance(current, str) or not SHA.fullmatch(current):
            raise RuntimeError("cannot verify current documentation report head")
        if current != scope["head_sha"]:
            api.set_status(current, "failure", "Documentation audit result is stale", report_url)
            raise ValueError("documentation report is stale; rerun the audit for the current revision")
        reports = api.list_reports(identity)
        if len(reports) > 1:
            raise RuntimeError("multiple issues claim the managed documentation report identity")
        issue = reports[0] if reports else None
        title = (f"Documentation freshness: PR #{scope['number']}" if scope["kind"] == "pull_request"
                 else "Documentation freshness: default branch")
        scope_url = api.scope_url(scope) if hasattr(api, "scope_url") else None
        body = render_body(identity, value, report_url, scope_url)
        if issue is None:
            if value["status"] != "clean":
                issue = api.create_report(title, body, ["needs-triage"])
        elif issue.get("state") == "closed":
            if value["status"] != "clean":
                api.reopen_report(issue, body)
                issue["state"] = "open"
                if hasattr(api, "ensure_triage_label"):
                    api.ensure_triage_label(issue)
            else:
                api.update_report(issue, body)
        else:
            api.update_report(issue, body)
            if value["status"] != "clean" and hasattr(api, "ensure_triage_label"):
                api.ensure_triage_label(issue)
        passed = value["status"] == "clean" and (issue is None or issue.get("state") == "closed")
        api.set_status(scope["head_sha"], "success" if passed else "failure",
                       "Documentation report is closed after a clean audit" if passed else
                       "Documentation report needs human review or the latest audit has findings",
                       report_url or (issue.get("html_url") if issue else None))
        return {"issue": issue.get("number") if issue else None,
                "status": "success" if passed else "failure"}
    except Exception:
        try:
            api.set_status(scope["head_sha"], "failure", "Documentation report could not be verified", report_url)
        except Exception:
            pass
        raise


def refresh_issue(api, issue_number, action):
    """Refresh the report commit status after a managed issue is closed or reopened."""
    if type(issue_number) is not int or issue_number <= 0 or action not in ("closed", "reopened"):
        raise ValueError("issue refresh event is invalid")
    issue = api.get_issue(issue_number)
    body = issue.get("body") if isinstance(issue, dict) else None
    match = ISSUE_MARKER.search(body or "") if isinstance(body, str) else None
    if not match:
        return {"status": "noop", "issue": issue_number}
    identity = match.group(1)
    expected_prefix = api.repository + ":"
    if not identity.startswith(expected_prefix):
        return {"status": "noop", "issue": issue_number}
    suffix = identity.removeprefix(expected_prefix)
    if suffix == "default-branch":
        scope = {"kind": "default_branch"}
    else:
        pr_match = re.fullmatch(r"pr-([1-9][0-9]*)", suffix)
        if not pr_match:
            return {"status": "noop", "issue": issue_number}
        scope = {"kind": "pull_request", "number": int(pr_match.group(1))}
    if action == "reopened" and hasattr(api, "ensure_triage_label"):
        api.ensure_triage_label(issue)
    audit = AUDIT_STATE.search(body)
    audited_sha = AUDIT_SHA.search(body)
    scope["head_sha"] = audited_sha.group(1) if audited_sha else None
    current = api.current_head(scope)
    if not isinstance(current, str) or not SHA.fullmatch(current):
        raise RuntimeError("cannot verify current documentation report head")
    clean_latest = bool(audit and audit.group(1) == "clean" and audited_sha and audited_sha.group(1) == current)
    closed_clean = issue.get("state") == "closed" and clean_latest
    state = "success" if closed_clean else "failure"
    description = ("Documentation report is closed after a clean audit" if closed_clean else
                   "Documentation report is open, stale, or has findings")
    api.set_status(current, state, description, issue.get("html_url"))
    return {"status": state, "issue": issue_number, "head_sha": current}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--report")
    operation.add_argument("--refresh-issue", type=int)
    parser.add_argument("--action", choices=("closed", "reopened"))
    args = parser.parse_args(argv)
    try:
        repository = os.environ.get("GITHUB_REPOSITORY")
        api = GitHubApi(os.environ.get("GITHUB_TOKEN"), repository or "")
        if args.refresh_issue is not None:
            if args.action is None:
                raise ValueError("--action is required with --refresh-issue")
            result = refresh_issue(api, args.refresh_issue, args.action)
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.action is not None:
            raise ValueError("--action is valid only with --refresh-issue")
        if os.path.getsize(args.report) > 1_000_000:
            raise ValueError("documentation artifact exceeds the size limit")
        with open(args.report, encoding="utf-8") as source:
            report = json.load(source)
        run_id = os.environ.get("DOCS_PRODUCER_RUN_ID")
        expected_event = os.environ.get("DOCS_EXPECTED_EVENT")
        run_attempt = os.environ.get("DOCS_EXPECTED_RUN_ATTEMPT")
        expected_head = os.environ.get("DOCS_EXPECTED_HEAD_SHA")
        pr_number = os.environ.get("DOCS_EXPECTED_PR_NUMBER")
        expected_scope = None
        if expected_head:
            expected_scope = ({"kind": "pull_request", "number": int(pr_number), "head_sha": expected_head}
                              if pr_number else {"kind": "default_branch", "head_sha": expected_head})
        result = publish(api, report, expected_repository=repository,
                         expected_run_id=int(run_id) if run_id else None,
                         expected_run_attempt=int(run_attempt) if run_attempt else None,
                         expected_event=expected_event, expected_scope=expected_scope,
                         report_url=os.environ.get("DOCS_RUN_URL"))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Documentation report failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
