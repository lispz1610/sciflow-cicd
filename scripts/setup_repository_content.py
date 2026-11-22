"""Reconcile caller-authored repository files, labels, and link comments."""

import base64
from pathlib import Path, PurePosixPath
import re
from urllib.parse import quote, urlparse

from configure_github_repo import ApiError, apply_changes


LABEL_COLOR = re.compile(r"[0-9a-fA-F]{6}\Z")
LINK_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _resource(api, path, missing=False):
    status, body = api.request("GET", path)
    if missing and status == 404:
        return None
    if status != 200 or not isinstance(body, dict):
        raise ApiError(f"GET {path}: HTTP {status}; resource unavailable or malformed")
    return body


def reconcile_labels(api, repository, items, apply=False):
    """Create or update explicitly named labels without touching others."""
    if not isinstance(items, list) or not items:
        raise ValueError("select at least one label")
    seen = set()
    for item in items:
        if (not isinstance(item, dict) or set(item) != {"name", "color", "description"} or
            not isinstance(item["name"], str) or not item["name"].strip() or
            not isinstance(item["color"], str) or not LABEL_COLOR.fullmatch(item["color"]) or
            not isinstance(item["description"], str) or len(item["description"]) > 100):
            raise ValueError("each label needs name, six-digit color, and description")
        folded = item["name"].casefold()
        if folded in seen:
            raise ValueError("duplicate label name")
        seen.add(folded)
    base = "/repos/" + repository + "/labels"
    changes = []
    for item in items:
        path = base + "/" + quote(item["name"], safe="")
        current = _resource(api, path, missing=True)
        desired = {"name": item["name"], "color": item["color"].lower(),
                   "description": item["description"]}
        if current is None:
            changes.append(("POST", base, desired))
        else:
            patch = {key: value for key, value in desired.items()
                     if key != "name" and current.get(key) != value}
            if patch:
                changes.append(("PATCH", path, patch))
    return apply_changes(api, changes, apply)


def _destination(path):
    if not isinstance(path, str) or not path or "\\" in path:
        raise ValueError("invalid managed file destination")
    parts = PurePosixPath(path).parts
    if path.startswith("/") or any(part in (".", "..") for part in parts):
        raise ValueError("invalid managed file destination")
    valid = (
        (path.startswith(".github/ISSUE_TEMPLATE/") and path.endswith((".yml", ".yaml", ".md"))) or
        path == ".github/pull_request_template.md" or
        (path.startswith(".github/PULL_REQUEST_TEMPLATE/") and path.endswith(".md")) or
        (path.startswith("docs/") and path.endswith(".md"))
    )
    if not valid or len(parts) < 2:
        raise ValueError("managed files must be Issue forms, PR templates, or authored docs")
    return path


def selected_file_bytes(items):
    """Validate selected authored files and return bytes for Setup Pull Request staging."""
    if not isinstance(items, list) or not items:
        raise ValueError("select at least one authored file")
    files = {}
    for item in items:
        if (not isinstance(item, dict) or not {"path", "source"} <= set(item) or
                not set(item) <= {"path", "source", "expected_sha"}):
            raise ValueError("each file needs path and source")
        destination = _destination(item["path"])
        if destination in files:
            raise ValueError("duplicate managed file destination")
        if not isinstance(item["source"], str) or not Path(item["source"]).is_file():
            raise ValueError("authored source file is unavailable")
        expected_sha = item.get("expected_sha")
        if expected_sha is not None and (not isinstance(expected_sha, str) or not
                                         re.fullmatch(r"[0-9a-fA-F]{40}", expected_sha)):
            raise ValueError("expected_sha must be a 40-character blob SHA")
        files[destination] = (Path(item["source"]).read_bytes(), expected_sha)
    return files


def reconcile_files(api, repository, items, apply=False):
    """Install supplied files using existing blob SHAs as drift guards."""
    if not isinstance(items, list) or not items:
        raise ValueError("select at least one authored file")
    seen = set()
    for item in items:
        if (not isinstance(item, dict) or not {"path", "source"} <= set(item) or
            not set(item) <= {"path", "source", "expected_sha"}):
            raise ValueError("each file needs path and source")
        destination = _destination(item["path"])
        if destination in seen:
            raise ValueError("duplicate managed file destination")
        seen.add(destination)
        if not isinstance(item["source"], str) or not Path(item["source"]).is_file():
            raise ValueError("authored source file is unavailable")
        expected_sha = item.get("expected_sha")
        if expected_sha is not None and (not isinstance(expected_sha, str) or not
                                         re.fullmatch(r"[0-9a-fA-F]{40}", expected_sha)):
            raise ValueError("expected_sha must be a 40-character blob SHA")
    repo = _resource(api, "/repos/" + repository)
    branch = repo.get("default_branch")
    if not isinstance(branch, str) or not branch:
        raise ApiError("cannot verify default branch for managed files")
    changes = []
    for item in items:
        destination = item["path"]
        path = "/repos/" + repository + "/contents/" + quote(destination, safe="/")
        current = _resource(api, path + "?ref=" + quote(branch, safe=""), missing=True)
        data = Path(item["source"]).read_bytes()
        if current is not None:
            if current.get("type") != "file" or current.get("encoding") != "base64":
                raise ApiError(f"cannot verify managed file content: {destination}")
            try:
                previous = base64.b64decode(current["content"], validate=False)
            except (KeyError, ValueError, TypeError):
                raise ApiError(f"cannot decode managed file content: {destination}") from None
            if previous == data:
                continue
            if item.get("expected_sha") != current.get("sha"):
                raise ApiError(f"managed file drift detected: {destination}; supply current expected_sha")
        elif item.get("expected_sha") is not None:
            raise ApiError(f"managed file drift detected: {destination}; expected file is absent")
        payload = {"message": "Publish caller-authored repository file",
                   "content": base64.b64encode(data).decode("ascii"), "branch": branch}
        if current is not None:
            payload["sha"] = current["sha"]
        changes.append(("PUT", path, payload))
    return apply_changes(api, changes, apply)


def _comments(api, path):
    comments = []
    for page in range(1, 21):
        status, body = api.request("GET", path + f"?per_page=100&page={page}")
        if status != 200 or not isinstance(body, list):
            raise ApiError(f"GET {path}: HTTP {status}; cannot list comments")
        comments.extend(body)
        if len(body) < 100:
            return comments
    raise ApiError("issue comment pagination exceeded 20 pages")


def reconcile_links(api, repository, entries, apply=False):
    """Maintain a single caller-selected links comment per Issue or PR."""
    if not isinstance(entries, list) or not entries:
        raise ValueError("select at least one Issue or PR for links")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"issue", "key", "links"}:
            raise ValueError("each link entry needs issue, key, and links")
        if type(entry["issue"]) is not int or entry["issue"] < 1:
            raise ValueError("issue number must be positive")
        if not isinstance(entry["key"], str) or not LINK_KEY.fullmatch(entry["key"]):
            raise ValueError("invalid link key")
        identity = (entry["issue"], entry["key"])
        if identity in seen:
            raise ValueError("duplicate managed links entry")
        seen.add(identity)
        if not isinstance(entry["links"], list) or not entry["links"]:
            raise ValueError("select one or more links")
        for link in entry["links"]:
            if (not isinstance(link, dict) or set(link) != {"label", "url"} or
                not isinstance(link["label"], str) or not link["label"] or
                any(char in link["label"] for char in "[]\n\r") or
                not isinstance(link["url"], str)):
                raise ValueError("invalid link label or URL")
            parsed = urlparse(link["url"])
            if parsed.scheme != "https" or not parsed.netloc or any(
                char in link["url"] for char in "<>\n\r)"
            ):
                raise ValueError("links must be HTTPS URLs")
    actor = _resource(api, "/user").get("login")
    if not isinstance(actor, str) or not actor:
        raise ApiError("cannot verify authenticated actor for managed comments")
    base = "/repos/" + repository
    changes = []
    for entry in entries:
        number = entry["issue"]
        issue_path = base + f"/issues/{number}"
        _resource(api, issue_path)
        marker = f"<!-- sciflow-cicd:{entry['key']} -->"
        legacy_marker = f"<!-- github-repo-setup:{entry['key']} -->"
        body = marker + "\n" + "\n".join(
            f"- [{link['label']}]({link['url']})" for link in entry["links"])
        path = issue_path + "/comments"
        owned = [comment for comment in _comments(api, path)
                 if isinstance(comment, dict) and
                 (marker in comment.get("body", "") or legacy_marker in comment.get("body", "")) and
                 isinstance(comment.get("user"), dict) and
                 comment["user"].get("login", "").casefold() == actor.casefold()]
        if len(owned) > 1:
            raise ApiError(f"multiple managed link comments found for Issue or PR {number}")
        if not owned:
            changes.append(("POST", path, {"body": body}))
        elif owned[0].get("body") != body:
            changes.append(("PATCH", base + f"/issues/comments/{owned[0]['id']}", {"body": body}))
    return apply_changes(api, changes, apply)
