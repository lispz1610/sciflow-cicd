#!/usr/bin/env python3
"""Plan or apply selected managed link comments on GitHub Issues and PRs."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from configure_github_repo import ApiError, GitHubApi
from setup_repository_content import reconcile_links


REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def _public_change(change, status):
    method, path, payload = change
    return {
        "operation": method,
        "path": path,
        "body_sha256": hashlib.sha256(payload["body"].encode("utf-8")).hexdigest(),
        "status": status,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="OWNER/REPO")
    parser.add_argument("--file", required=True, type=Path, help="JSON array of link entries")
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--apply", action="store_true", help="write selected comments; default is dry run")
    args = parser.parse_args(argv)

    token = os.environ.get(args.token_env)
    if not token:
        parser.error(f"set {args.token_env} before running")
    try:
        if not REPOSITORY.fullmatch(args.repo):
            raise ValueError("repository must be OWNER/REPO")
        entries = json.loads(args.file.read_text(encoding="utf-8"))
        api = GitHubApi(token)
        changes = reconcile_links(api, args.repo, entries, apply=args.apply)
        remaining = reconcile_links(api, args.repo, entries) if args.apply and changes else []
    except (ApiError, ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    outstanding = {(method, path, json.dumps(payload, sort_keys=True))
                   for method, path, payload in remaining}
    result = {
        "mode": "apply" if args.apply else "dry-run",
        "repository": args.repo,
        "changes": [
            _public_change(change, (
                "planned" if not args.apply else
                "unverified" if (change[0], change[1], json.dumps(change[2], sort_keys=True))
                in outstanding else "verified"))
            for change in changes
        ],
    }
    print(json.dumps(result, indent=2))
    return 2 if any(change["status"] == "unverified" for change in result["changes"]) else 0


if __name__ == "__main__":
    sys.exit(main())
