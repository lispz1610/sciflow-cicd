"""Shared fake GitHub API doubles for the test suite.

These doubles mirror the transport surface of :mod:`scripts.github_client`:
``request`` keeps the historical ``(status, body)`` shape used by repository
tooling, while ``request_with_headers`` and the inherited ``paginate`` expose
the unified client contract. Per-test routes belong in subclasses that override
``request`` and delegate to ``super().request`` for everything else.
"""

from __future__ import annotations

import base64
from pathlib import Path
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.github_client import API_VERSION, GitHubApi, normalize_headers


class FakeApi(GitHubApi):
    """In-memory repository settings API used by most reconciler tests."""

    def __init__(self, token=None, **kwargs):
        super().__init__(token, **kwargs)
        self.repo = {
            "default_branch": "master", "visibility": "public", "has_issues": False,
            "has_wiki": False, "has_projects": False,
            "allow_merge_commit": True, "allow_squash_merge": True,
            "allow_rebase_merge": True, "delete_branch_on_merge": False,
            "allow_update_branch": False,
            "security_and_analysis": {
                "secret_scanning": {"status": "disabled"},
                "secret_scanning_push_protection": {"status": "disabled"},
            },
        }
        self.actions = {"enabled": False, "allowed_actions": "all", "sha_pinning_required": False}
        self.selected_actions = {"github_owned_allowed": True, "verified_allowed": False,
                                 "patterns_allowed": []}
        self.workflow = {"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True}
        self.codeql = {"state": "not-configured", "languages": ["python"], "query_suite": "default"}
        self.features = {"vulnerability-alerts": False, "automated-security-fixes": False,
                         "private-vulnerability-reporting": False}
        self.writes = []
        self.reads = []
        self.rulesets = []
        self.pr = {"base": {"ref": "master"}, "head": {"sha": "abc"},
                   "merge_commit_sha": "merge-abc", "user": {"login": "author"}}
        self.check_runs = [{"name": "ci-check", "head_sha": "merge-abc", "status": "completed",
                            "conclusion": "success", "check_suite": {"id": 51},
                            "app": {"id": 15368}}]
        self.workflow_runs = [{"workflow_id": 11, "check_suite_id": 51,
                               "head_sha": "merge-abc", "event": "pull_request",
                               "status": "completed", "conclusion": "success"}]
        self.reviews = [{"user": {"login": "reviewer"}, "state": "APPROVED", "commit_id": "abc"}]

    def request_with_headers(self, method, path, payload=None):
        """Adapt the in-memory route table to the unified client's triple result."""
        status, body = self.request(method, path, payload)
        headers = normalize_headers(self._request_headers(None))
        headers["x-github-api-version"] = API_VERSION
        return status, headers, body

    def request(self, method, path, payload=None):
        if method == "GET":
            self.reads.append((method, path, payload))
        base = "/repos/acme/project"
        if method == "GET" and path.startswith(base + "/contents/.agents/skills/manifest.json?ref="):
            return 404, None
        if path == base:
            if method == "PATCH":
                self.writes.append((method, path, payload))
                self.repo.update({key: val for key, val in payload.items() if key != "security_and_analysis"})
                for key, val in payload.get("security_and_analysis", {}).items():
                    self.repo["security_and_analysis"][key] = val
            return 200, self.repo
        if path == base + "/actions/permissions":
            if method == "PUT":
                self.writes.append((method, path, payload))
                self.actions.update(payload)
            return 200, self.actions
        if path == base + "/actions/permissions/workflow":
            if method == "PUT":
                self.writes.append((method, path, payload))
                self.workflow.update(payload)
            return 200, self.workflow
        if path == base + "/actions/permissions/selected-actions":
            if method == "GET" and self.actions["allowed_actions"] != "selected":
                return 409, None
            if method == "PUT":
                if self.actions["allowed_actions"] != "selected":
                    raise AssertionError("selected-actions requires selected policy")
                self.writes.append((method, path, payload))
                self.selected_actions.update(payload)
                return 204, None
            return 200, self.selected_actions
        if path == base + "/code-scanning/default-setup":
            if method == "PATCH":
                self.writes.append((method, path, payload))
                self.codeql.update(payload)
                return 202, {"run_id": 42}
            return 200, self.codeql
        for feature, enabled in self.features.items():
            if path == base + "/" + feature:
                if enabled is None:
                    return 503, None
                if method == "PUT":
                    self.writes.append((method, path, payload))
                    self.features[feature] = True
                    return 204, None
                if feature == "vulnerability-alerts":
                    return (204, None) if enabled else (200, {"enabled": False})
                return 200, {"enabled": enabled}
        if path in (base + "/rulesets", base + "/rulesets?includes_parents=false&per_page=100"):
            if method == "POST":
                self.writes.append((method, path, payload))
                self.rulesets.append(dict(payload, id=42))
                return 201, self.rulesets[-1]
            return 200, self.rulesets
        if path == base + "/rulesets/42":
            if method == "PUT":
                self.writes.append((method, path, payload))
                self.rulesets[0].update(payload)
            return 200, self.rulesets[0]
        if path == base + "/pulls/7":
            return 200, self.pr
        if path == base + "/pulls/8":
            return 200, {"state": "open", "base": {"ref": "master"}, "head": {"sha": "bad"},
                         "merge_commit_sha": "merge-bad", "mergeable_state": "blocked"}
        if path == base + "/pulls/9":
            return 200, {"state": "open", "base": {"ref": "master"}, "head": {"sha": "good"},
                         "merge_commit_sha": "merge-good", "mergeable_state": "clean"}
        if path == base + "/pulls/9/reviews?per_page=100&page=1":
            return 200, [{"user": {"login": "reviewer"}, "state": "APPROVED", "commit_id": "good"}]
        if path == base + "/commits/merge-bad/check-runs?per_page=100&page=1":
            return 200, {"check_runs": [{"name": "ci-check", "conclusion": "failure"}]}
        if path == base + "/commits/merge-good/check-runs?per_page=100&page=1":
            return 200, {"check_runs": [{"name": "ci-check", "conclusion": "success"}]}
        if path == base + "/pulls/7/reviews?per_page=100&page=1":
            return 200, self.reviews
        if path == base + "/actions/workflows?per_page=100&page=1":
            return 200, {"workflows": [{"id": 11, "path": ".github/workflows/ci.yml", "name": "PR CI", "state": "active"}]}
        if path == base + "/actions/workflows/11/runs?head_sha=merge-abc&event=pull_request&per_page=100&page=1":
            return 200, {"workflow_runs": self.workflow_runs}
        if path == base + "/commits/merge-abc/check-runs?per_page=100&page=1":
            return 200, {"check_runs": self.check_runs}
        if path == base + "/commits/abc/check-runs?per_page=100&page=1":
            return 200, {"check_runs": []}
        if path == base + "/actions/workflows/11/runs?head_sha=abc&event=pull_request&per_page=100&page=1":
            return 200, {"workflow_runs": []}
        if path == base + "/collaborators/reviewer/permission":
            return 200, {"permission": "write"}
        raise AssertionError((method, path))


class InventoryApi(FakeApi):
    """Repository inventory double (labels and paginated ruleset routes)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.labels = {}

    def request(self, method, path, payload=None):
        if method == "GET" and path == "/repos/acme/project/code-scanning/default-setup":
            return 200, self.codeql
        if path.startswith("/repos/acme/project/rulesets?includes_parents=false&per_page=100&page="):
            return 200, self.rulesets
        if "/labels/" in path:
            name = path.split("/labels/")[-1]
            if name in self.labels:
                return 200, self.labels[name]
            return 404, None
        if method == "POST" and path.endswith("/labels"):
            self.writes.append((method, path, payload))
            if isinstance(payload, dict) and "name" in payload:
                self.labels[payload["name"]] = payload
            return 201, payload
        return super().request(method, path, payload)


class CleanRepoApi(InventoryApi):
    """Simulated GitHub API for a clean repository with no initial configuration."""

    def __init__(self, repo_name="acme/project", **kwargs):
        super().__init__(**kwargs)
        self.repo_name = repo_name
        self.repo["full_name"] = repo_name
        self.repo["has_issues"] = False
        self.repo["has_wiki"] = False
        self.repo["has_projects"] = False
        self.repo["allow_merge_commit"] = False
        self.repo["allow_squash_merge"] = False
        self.repo["allow_rebase_merge"] = False
        self.repo["delete_branch_on_merge"] = False
        self.repo["allow_update_branch"] = False
        self.workflow = {}
        self.actions = {
            "enabled": False,
            "allowed_actions": "all",
            "sha_pinning_required": False,
        }
        self.features = {
            "vulnerability-alerts": False,
            "automated-security-fixes": False,
            "private-vulnerability-reporting": False,
        }
        self.security_and_analysis = {
            "secret_scanning": {"status": "disabled"},
            "secret_scanning_push_protection": {"status": "disabled"},
        }
        self.rulesets = []
        self.workflow_files = {}
        self.workflow_refs = {}
        self.workflow_prs = []
        self.labels = {}
        self._inventory_workflow_done = False
        self._inventory_actions_done = False

    def request(self, method, path, payload=None):
        base = "/repos/" + self.repo_name
        if path == base + "/actions/permissions":
            if method == "GET":
                if not self._inventory_actions_done:
                    self._inventory_actions_done = True
                    return 200, self.actions
                return 200, {"enabled": True, "allowed_actions": "all"}
            if method == "PUT":
                self.writes.append((method, path, payload))
                self.actions.update(payload)
                return 200, self.actions
        if path == base + "/actions/permissions/workflow":
            if method == "GET":
                if not self._inventory_workflow_done:
                    self._inventory_workflow_done = True
                    return 200, self.workflow
                return 200, self.workflow or {"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True}
            if method == "PUT":
                self.writes.append((method, path, payload))
                if not self.workflow:
                    self.workflow = {"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True}
                self.workflow.update(payload)
                return 200, self.workflow
        if method == "GET" and "/labels/" in path:
            label_name = path.split("/labels/")[-1]
            if label_name in self.labels:
                return 200, self.labels[label_name]
            return 404, None
        if method == "POST" and path.startswith(base + "/labels"):
            self.writes.append((method, path, payload))
            if isinstance(payload, dict) and "name" in payload:
                self.labels[payload["name"]] = payload
            return 201, payload
        if method == "GET" and path.endswith("/actions/permissions/artifact-and-log-retention"):
            return 200, {"days": 30}
        if method == "GET" and ("/actions/secrets/" in path or "/actions/variables/" in path):
            return 404, None
        if method == "GET" and (path.startswith("/repos/actions/") or path.startswith("/repos/astral-sh/")) and "/git/commits/" in path:
            sha = path.split("/git/commits/")[1]
            return 200, {"sha": sha}
        if method == "GET" and path.startswith(base + "/git/ref/heads/"):
            branch = unquote(path[len(base + "/git/ref/heads/"):])
            if branch == self.repo.get("default_branch", "master"):
                return 200, {"object": {"sha": "a" * 40}}
            if branch in self.workflow_refs:
                return 200, {"object": {"sha": self.workflow_refs[branch]}}
            return 404, None
        if method == "GET" and path.startswith(base + "/pulls?state=open&base="):
            return 200, self.workflow_prs
        if method == "GET" and path.startswith(base + "/pulls?head="):
            return 200, self.workflow_prs
        if method == "POST" and path == base + "/git/refs":
            self.writes.append((method, path, payload))
            self.workflow_refs[payload["ref"].removeprefix("refs/heads/")] = payload["sha"]
            return 201, {"ref": payload["ref"]}
        if method == "GET" and path.startswith(base + "/compare/"):
            branch = unquote(path.split("...", 1)[1])
            return 200, {"files": [{"filename": filename, "status": "added"}
                                   for (ref, filename) in self.workflow_files if ref == branch]}
        if method == "GET" and path.startswith(base + "/contents/.github/workflows/"):
            wf_path = path.split("?")[0][len(base + "/contents/"):]
            branch = unquote(path.split("?ref=", 1)[1]) if "?ref=" in path else self.repo.get("default_branch", "master")
            if (branch, wf_path) in self.workflow_files:
                content_bytes = self.workflow_files[(branch, wf_path)]
                return 200, {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(content_bytes).decode("ascii"),
                    "sha": "b" * 40,
                }
            return 404, None
        if method == "PUT" and path.startswith(base + "/contents/.github/workflows/"):
            self.writes.append((method, path, payload))
            filename = path.removeprefix(base + "/contents/")
            content = base64.b64decode(payload["content"])
            self.workflow_files[(payload["branch"], filename)] = content
            return 200, {"content": {"sha": "b" * 40}}
        if method == "POST" and path == base + "/pulls":
            self.writes.append((method, path, payload))
            pull = {"number": 1, "html_url": "https://github.com/acme/project/pull/1",
                    "head": {"ref": payload["head"]}, "base": {"ref": payload["base"]}, "draft": True}
            self.workflow_prs.append(pull)
            return 201, pull
        return super().request(method, path, payload)


class IssuesApi(FakeApi):
    """Minimal issue-creation double for policy drift reporting tests."""

    def __init__(self, issues=None, **kwargs):
        super().__init__(**kwargs)
        self.issues = issues or []
        self.requests = []

    def request(self, method, path, payload=None):
        self.requests.append((method, path, payload))
        if method == "GET":
            return 200, self.issues
        if method == "POST":
            return 201, {"number": 12, "title": payload["title"], **payload}
        raise AssertionError((method, path, payload))


class DocumentationReportApi:
    """High-level reporter double (issue-shaped reports, not REST routes)."""

    def __init__(self):
        self.repository = "acme/project"
        self.issue = None
        self.statuses = []

    def list_reports(self, scope):
        return [self.issue] if self.issue else []

    def get_issue(self, number):
        if self.issue is None or self.issue["number"] != number:
            raise RuntimeError("missing issue")
        return self.issue

    def create_report(self, title, body, labels):
        self.issue = {"number": 71, "title": title, "body": body, "state": "open",
                      "labels": labels, "comments": []}
        return self.issue

    def update_report(self, issue, body):
        issue["body"] = body

    def reopen_report(self, issue, body):
        issue["body"] = body
        issue["state"] = "open"

    def ensure_triage_label(self, issue):
        if "needs-triage" not in issue["labels"]:
            issue["labels"].append("needs-triage")

    def comment(self, issue, body):
        issue["comments"].append(body)

    def current_head(self, scope):
        return scope["head_sha"]

    def scope_url(self, scope):
        return f"https://github.com/acme/project/pull/{scope['number']}"

    def set_status(self, sha, state, description, target_url):
        self.statuses.append((sha, state, description, target_url))
