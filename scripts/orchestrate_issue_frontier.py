#!/usr/bin/env python3
"""Evaluate issue closure events and orchestrate transitions on the issue dependency DAG."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
import logging
import os
import re
import sys
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    from scripts.path_security import validated_repo_api_path
except ImportError:  # pragma: no cover - direct script execution
    from path_security import validated_repo_api_path

logger = logging.getLogger("scripts.orchestrate_issue_frontier")


class GitHubApiError(RuntimeError):
    """Raised when GitHub API requests fail or return error responses."""


@dataclass
class BlockerRecord:
    issue_number: int
    blockers: list[int] = field(default_factory=list)
    unresolved_blockers: list[int] = field(default_factory=list)
    state: str = "open"

    @property
    def is_unblocked(self) -> bool:
        return len(self.unresolved_blockers) == 0 and len(self.blockers) > 0


@dataclass
class FrontierTransition:
    issue_number: int
    resolved_blockers: list[int]
    labels_added: list[str]
    comment: str | None = None

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _extract_issue_references(
    body: str,
    keywords: str | Sequence[str],
    repo: str | None = None,
) -> list[int]:
    """Extract issue references from markdown body given section/inline keywords.

    Supports:
      - Section headers matching keywords (e.g. '## Blocked by', '## Blocking')
      - Inline lines (e.g. 'Blocked by: #123', 'Blocking: #123')
      - Qualified references (owner/repo#123) matching repo
      - Full issue URLs matching repo
      - Simple #123 references
    """
    if not body:
        return []

    target_repo = repo.lower() if repo else None
    references: set[int] = set()

    keyword_list = [keywords] if isinstance(keywords, str) else list(keywords)
    raw_texts: list[str] = []

    for kw in keyword_list:
        pattern_kw = r"\s+".join(re.escape(word) for word in kw.split())
        section_pattern = rf"(?im)^#{{1,4}}\s*{pattern_kw}[:\s]*(.*?)(?=^#{{1,4}}\s+|\Z)"
        section_match = re.search(section_pattern, body, re.DOTALL)
        if section_match:
            raw_texts.append(section_match.group(1))

        inline_pattern = rf"(?im)^{pattern_kw}[:\s]+(.*?)$"
        inline_matches = re.findall(inline_pattern, body)
        raw_texts.extend(inline_matches)

    for text in raw_texts:
        cleaned = text.strip().lower()
        if cleaned in ("none", "n/a", "none."):
            continue

        # Look for full issue URLs: https://github.com/owner/repo/issues/123
        for match in re.finditer(r"https?://github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)", text):
            match_repo, num_str = match.group(1).lower(), match.group(2)
            if target_repo is None or match_repo == target_repo:
                num = int(num_str)
                if num > 0:
                    references.add(num)

        # Look for qualified references: owner/repo#123
        for match in re.finditer(r"\b([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)#(\d+)\b", text):
            match_repo, num_str = match.group(1).lower(), match.group(2)
            if target_repo is None or match_repo == target_repo:
                num = int(num_str)
                if num > 0:
                    references.add(num)

        # Look for simple references: #123
        sanitized = re.sub(r"https?://github\.com/[^/\s]+/[^/\s]+/issues/\d+", " ", text)
        sanitized = re.sub(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+#\d+", " ", sanitized)

        for match in re.finditer(r"#(\d+)\b", sanitized):
            num = int(match.group(1))
            if num > 0:
                references.add(num)

    return sorted(references)


def parse_blocked_by(body: str, repo: str | None = None) -> list[int]:
    """Extract blocker issue numbers from markdown issue body.

    Supports:
      - '## Blocked by' sections with bullet lists (- #123, * #123, • #123)
      - Inline lines like 'Blocked by: #123, #124'
      - Qualified references (owner/repo#123) matching repo
      - Full issue URLs matching repo
    """
    return _extract_issue_references(body, ["Blocked by"], repo=repo)


def parse_blocking(body: str, repo: str | None = None) -> list[int]:
    """Extract downstream issues blocked by this issue from markdown body."""
    return _extract_issue_references(body, ["Blocking"], repo=repo)


def detect_cycles(graph: dict[int, list[int]]) -> list[list[int]]:
    """Detect cycles in an adjacency graph (u -> list of nodes u depends on/is blocked by).

    Returns a list of cycle paths, e.g. [[1, 2, 1]].
    """
    visited: set[int] = set()
    rec_stack: list[int] = []
    cycles: list[list[int]] = []

    def dfs(node: int) -> None:
        visited.add(node)
        rec_stack.append(node)

        for neighbor in graph.get(node, []):
            if neighbor in rec_stack:
                # Cycle found!
                cycle_start = rec_stack.index(neighbor)
                cycle_path = rec_stack[cycle_start:] + [neighbor]
                cycles.append(cycle_path)
            elif neighbor not in visited:
                dfs(neighbor)

        rec_stack.pop()

    for node in list(graph.keys()):
        if node not in visited:
            dfs(node)

    return cycles


WAYFINDER_SIGNATURES = ("All blockers resolved", "wayfinder:frontier")


def format_wayfinder_comment(resolved_blockers: list[int]) -> str:
    """Format the wayfinder comment announcing transition to the active frontier."""
    blockers_str = ", ".join(f"#{n}" for n in sorted(resolved_blockers))
    return (
        f"🚀 All blockers resolved ([{blockers_str}]). "
        "This ticket is unblocked and now on the active frontier ready for agent implementation."
    )


def has_wayfinder_comment(comments: list[dict]) -> bool:
    """Return True if any comment in comments contains a wayfinder marker."""
    for c in comments:
        if isinstance(c, dict):
            body = c.get("body") or ""
            if any(sig in body for sig in WAYFINDER_SIGNATURES):
                return True
    return False


class GitHubClient:
    """REST and GraphQL client for GitHub issue operations."""

    def __init__(
        self,
        token: str,
        api_url: str = "https://api.github.com",
        repository: str = "",
    ):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.repository = repository

    def _request(
        self,
        url: str,
        payload: dict | list | None = None,
        method: str | None = None,
    ) -> dict | list:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        if method is None:
            method = "POST" if payload is not None else "GET"

        request = Request(
            url,
            data=data,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
            method=method,
        )
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read()
        except (HTTPError, URLError) as error:
            raise GitHubApiError(f"GitHub API request failed for {url}: {error}") from error

        result = json.loads(body) if body else {}
        if isinstance(result, dict) and result.get("errors"):
            raise GitHubApiError(f"GitHub API returned errors: {result['errors']}")
        return result

    def _repo_path(self) -> str:
        return validated_repo_api_path(self.repository)

    def get_issue(self, number: int) -> dict:
        url = f"{self.api_url}{self._repo_path()}/issues/{number}"
        res = self._request(url)
        if not isinstance(res, dict):
            raise GitHubApiError(f"Unexpected response for issue #{number}")
        return res

    def list_open_issues(self) -> list[dict]:
        issues = []
        repo_path = self._repo_path()
        for page in range(1, 101):
            url = f"{self.api_url}{repo_path}/issues?state=open&per_page=100&page={page}"
            res = self._request(url)
            if not isinstance(res, list):
                raise GitHubApiError(f"Unexpected response listing issues for {self.repository}")
            for item in res:
                if isinstance(item, dict) and "pull_request" not in item:
                    issues.append(item)
            if len(res) < 100:
                break
        return issues

    def list_comments(self, number: int) -> list[dict]:
        comments: list[dict] = []
        repo_path = self._repo_path()
        page = 1
        while True:
            url = f"{self.api_url}{repo_path}/issues/{number}/comments?per_page=100&page={page}"
            res = self._request(url)
            if not isinstance(res, list):
                raise GitHubApiError(f"Unexpected response listing comments for issue #{number}")
            comments.extend(item for item in res if isinstance(item, dict))
            if len(res) < 100:
                break
            page += 1
        return comments

    def get_issue_dependencies_graphql(self, number: int) -> dict | None:
        """Query native GraphQL blockedBy and blocking fields for an issue."""
        if "/" not in self.repository:
            return None
        owner, repo_name = self.repository.split("/", 1)
        query = (
            "query($owner: String!, $repo: String!, $number: Int!) {"
            "  repository(owner: $owner, name: $repo) {"
            "    issue(number: $number) {"
            "      number"
            "      state"
            "      labels(first: 50) { nodes { name } }"
            "      blockedBy(first: 100) { nodes { number state } }"
            "      blocking(first: 100) { nodes { number state } }"
            "    }"
            "  }"
            "}"
        )
        try:
            url = f"{self.api_url}/graphql"
            result = self._request(
                url,
                payload={
                    "query": query,
                    "variables": {"owner": owner, "repo": repo_name, "number": number},
                },
                method="POST",
            )
            if isinstance(result, dict):
                data = result.get("data", {})
                if isinstance(data, dict):
                    repo_data = data.get("repository", {})
                    if isinstance(repo_data, dict):
                        return repo_data.get("issue")
        except GitHubApiError as exc:
            logger.info("GraphQL dependency query unavailable for #%d: %s", number, exc)
            return None
        return None

    def add_labels(self, number: int, labels: list[str]) -> None:
        url = f"{self.api_url}{self._repo_path()}/issues/{number}/labels"
        self._request(url, payload={"labels": labels}, method="POST")

    def add_comment(self, number: int, body: str) -> None:
        url = f"{self.api_url}{self._repo_path()}/issues/{number}/comments"
        self._request(url, payload={"body": body}, method="POST")


class IssueFrontierOrchestrator:
    """Evaluates DAG blocker dependencies and transitions unblocked issues."""

    def __init__(self, repository: str):
        self.repository = repository

    def evaluate_closed_issue(
        self,
        closed_issue_number: int,
        client: GitHubClient,
        dry_run: bool = False,
    ) -> list[FrontierTransition]:
        """Evaluate dependencies following the closure of closed_issue_number."""
        logger.info(
            "Evaluating frontier transition following closure of issue #%d in %s",
            closed_issue_number,
            self.repository,
        )

        # 1. Identify candidate dependent issues
        candidates: dict[int, dict] = {}

        # 1a. Native GraphQL blocking check on closed issue
        gql_closed = client.get_issue_dependencies_graphql(closed_issue_number)
        if gql_closed and isinstance(gql_closed.get("blocking"), dict):
            nodes = gql_closed["blocking"].get("nodes", [])
            for node in nodes:
                if isinstance(node, dict) and isinstance(node.get("number"), int):
                    num = node["number"]
                    if num != closed_issue_number:
                        candidates[num] = {}

        # 1b. Fetch closed issue's body to see if it lists Blocking: #...
        try:
            closed_issue_data = client.get_issue(closed_issue_number)
            if isinstance(closed_issue_data, dict):
                blocking_from_body = parse_blocking(
                    closed_issue_data.get("body", "") or "",
                    self.repository,
                )
                for num in blocking_from_body:
                    if num != closed_issue_number:
                        candidates[num] = {}
        except GitHubApiError as exc:
            logger.warning("Could not fetch closed issue #%d: %s", closed_issue_number, exc)

        # 1c. Scan open issues in the repository for references to closed_issue_number
        try:
            open_issues = client.list_open_issues()
            for issue in open_issues:
                num = issue.get("number")
                if not isinstance(num, int) or num == closed_issue_number:
                    continue
                body = issue.get("body") or ""
                body_blockers = parse_blocked_by(body, self.repository)
                if closed_issue_number in body_blockers:
                    candidates[num] = issue
        except GitHubApiError as exc:
            logger.warning("Failed to list open issues: %s", exc)

        if not candidates:
            logger.info("No dependent candidate issues found for #%d", closed_issue_number)
            return []

        logger.info(
            "Found candidate dependent issues: %s",
            sorted(candidates.keys()),
        )

        # 2. For each candidate, evaluate ALL incoming blocking edges
        # Build dependency graph for cycle detection among candidates and their blockers
        dep_graph: dict[int, list[int]] = {}
        candidate_blockers: dict[int, set[int]] = {}
        candidate_issue_objects: dict[int, dict] = {}
        issue_cache: dict[int, dict] = dict(candidates)

        def _get_issue_and_blockers(num: int) -> tuple[dict | None, set[int]]:
            issue_obj = issue_cache.get(num)
            if not issue_obj:
                try:
                    issue_obj = client.get_issue(num)
                    issue_cache[num] = issue_obj
                except GitHubApiError as exc:
                    logger.warning("Could not fetch issue #%d: %s", num, exc)
                    return None, set()

            blist: set[int] = set()
            gql_data = client.get_issue_dependencies_graphql(num)
            if gql_data and isinstance(gql_data.get("blockedBy"), dict):
                for node in gql_data["blockedBy"].get("nodes", []):
                    if isinstance(node, dict) and isinstance(node.get("number"), int):
                        blist.add(node["number"])

            body_blockers = parse_blocked_by(issue_obj.get("body") or "", self.repository)
            blist.update(body_blockers)
            return issue_obj, blist

        # Populate candidate objects and discover transitive open dependencies for cycle checking
        nodes_to_inspect: list[int] = sorted(candidates.keys())
        inspected: set[int] = set()

        while nodes_to_inspect:
            current_num = nodes_to_inspect.pop(0)
            if current_num in inspected:
                continue
            inspected.add(current_num)

            issue_obj, blist = _get_issue_and_blockers(current_num)
            if not issue_obj:
                continue

            state = issue_obj.get("state", "").lower()
            if current_num in candidates:
                if state != "open":
                    logger.info("Candidate #%d is not open; skipping", current_num)
                    continue
                candidate_issue_objects[current_num] = issue_obj
                # If neither GraphQL nor markdown listed blockers, but closed_issue had it in blocking
                if not blist:
                    blist.add(closed_issue_number)
                candidate_blockers[current_num] = blist

            dep_graph[current_num] = list(blist)

            # Queue open blockers to uncover multi-node cycles
            for b in blist:
                if b not in inspected and b != closed_issue_number:
                    nodes_to_inspect.append(b)

        # Run cycle detection on graph
        detected_cycles = detect_cycles(dep_graph)
        cyclic_nodes: set[int] = set()
        for cycle in detected_cycles:
            logger.warning("Cyclic dependency detected: %s", " -> ".join(f"#{n}" for n in cycle))
            cyclic_nodes.update(cycle)

        transitions: list[FrontierTransition] = []

        # Cache issue states to avoid redundant fetches
        state_cache: dict[int, str] = {closed_issue_number: "closed"}

        for cand_num, cand_issue in candidate_issue_objects.items():
            if cand_num in cyclic_nodes:
                logger.warning("Issue #%d is involved in a dependency cycle; cannot transition", cand_num)
                continue

            blockers = candidate_blockers.get(cand_num, set())
            if not blockers:
                logger.info("Issue #%d has no recorded blockers; skipping transition", cand_num)
                continue

            unresolved_blockers: list[int] = []

            for blocker_num in blockers:
                if blocker_num == cand_num:
                    unresolved_blockers.append(blocker_num)
                    continue

                if blocker_num in state_cache:
                    blocker_state = state_cache[blocker_num]
                else:
                    try:
                        b_issue = client.get_issue(blocker_num)
                        blocker_state = b_issue.get("state", "").lower()
                        state_cache[blocker_num] = blocker_state
                    except GitHubApiError as exc:
                        logger.warning(
                            "Referenced blocker #%d for issue #%d not found or inaccessible: %s",
                            blocker_num,
                            cand_num,
                            exc,
                        )
                        blocker_state = "unknown"
                        state_cache[blocker_num] = blocker_state

                if blocker_state != "closed":
                    unresolved_blockers.append(blocker_num)

            record = BlockerRecord(
                issue_number=cand_num,
                blockers=sorted(blockers),
                unresolved_blockers=unresolved_blockers,
                state=cand_issue.get("state", "open"),
            )

            if not record.is_unblocked:
                logger.info(
                    "Issue #%d remains blocked by: %s",
                    cand_num,
                    unresolved_blockers,
                )
                continue

            # All blockers are closed! Transition this issue.
            logger.info("All blockers closed for issue #%d: %s", cand_num, sorted(blockers))

            # Check existing labels
            existing_labels = {
                lbl.get("name")
                for lbl in cand_issue.get("labels", [])
                if isinstance(lbl, dict) and isinstance(lbl.get("name"), str)
            }

            labels_to_add: list[str] = []
            if "ready-for-agent" not in existing_labels:
                labels_to_add.append("ready-for-agent")

            comment_body = format_wayfinder_comment(sorted(blockers))

            existing_comments: list[dict] = []
            if hasattr(client, "list_comments"):
                try:
                    existing_comments = client.list_comments(cand_num)
                except GitHubApiError as exc:
                    logger.warning("Could not list comments for issue #%d: %s", cand_num, exc)

            already_commented = has_wayfinder_comment(existing_comments)

            if not dry_run:
                if labels_to_add:
                    client.add_labels(cand_num, labels_to_add)
                if not already_commented:
                    client.add_comment(cand_num, comment_body)
                else:
                    logger.info("Wayfinder comment already exists on issue #%d; skipping duplicate comment", cand_num)

            transitions.append(
                FrontierTransition(
                    issue_number=cand_num,
                    resolved_blockers=sorted(blockers),
                    labels_added=labels_to_add,
                    comment=comment_body if not already_commented else None,
                )
            )

        return transitions


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for issue frontier orchestration."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event",
        default=os.environ.get("EVENT_PATH") or os.environ.get("GITHUB_EVENT_PATH"),
        help="Path to GitHub Actions event JSON file",
    )
    parser.add_argument(
        "--token-env",
        default="GITHUB_TOKEN",
        help="Environment variable containing GitHub access token (default: GITHUB_TOKEN)",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Target repository owner/repo (default: from event or GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        help="GitHub API URL (default: https://api.github.com)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Calculate transitions without making mutation API calls",
    )

    args = parser.parse_args(argv)

    if not args.event:
        print("Error: Event file path is required (--event or EVENT_PATH/GITHUB_EVENT_PATH)", file=sys.stderr)
        return 2

    try:
        with open(args.event, encoding="utf-8") as f:
            payload = json.load(f)
    except OSError as err:
        print(f"Error: Unable to read event file {args.event}: {err}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as err:
        print(f"Error: Invalid JSON in event file {args.event}: {err}", file=sys.stderr)
        return 2

    action = payload.get("action")
    issue = payload.get("issue")

    if action != "closed":
        print(f"Event action '{action}' is not 'closed'; skipping orchestration (no-op).")
        return 0

    if not isinstance(issue, dict) or "pull_request" in issue:
        print("Event target is not a standard issue; skipping orchestration (no-op).")
        return 0

    closed_number = issue.get("number")
    if not isinstance(closed_number, int):
        print("Issue payload does not contain a valid issue number.", file=sys.stderr)
        return 1

    repo = (
        args.repo
        or (payload.get("repository") or {}).get("full_name")
        or os.environ.get("GITHUB_REPOSITORY")
    )
    if not repo:
        print("Error: Repository is required (--repo or GITHUB_REPOSITORY)", file=sys.stderr)
        return 1

    token = os.environ.get(args.token_env, "")
    if not token and not args.dry_run:
        print(f"Error: Token in environment variable '{args.token_env}' is required for live execution", file=sys.stderr)
        return 1

    client = GitHubClient(token=token, api_url=args.api_url, repository=repo)
    orchestrator = IssueFrontierOrchestrator(repository=repo)

    transitions = orchestrator.evaluate_closed_issue(
        closed_number,
        client,
        dry_run=args.dry_run,
    )

    result = {
        "closed_issue": closed_number,
        "repository": repo,
        "dry_run": args.dry_run,
        "transitions_count": len(transitions),
        "transitions": [t.to_dict() if hasattr(t, "to_dict") else t for t in transitions],
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
