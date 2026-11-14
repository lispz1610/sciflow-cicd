#!/usr/bin/env python3
"""Publish explicitly selected, author-written Markdown pages to a GitHub Wiki.

The command clones the Wiki Git repository for each run. Dry run is the default;
--apply commits and pushes only changed pages. It never deletes Wiki pages.
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlsplit


PAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class WikiError(RuntimeError):
    pass


def run_git(*args, cwd=None, credential_url=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    parsed_url = urlsplit(credential_url or "")
    token = env.get("GITHUB_TOKEN", "").strip()
    if token and parsed_url.scheme == "https" and parsed_url.hostname:
        # Keep the credential in the process environment. Git receives only a
        # helper that reads it when HTTPS authentication is requested.
        host = parsed_url.hostname
        if parsed_url.port:
            host = f"{host}:{parsed_url.port}"
        env.update({
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": f"credential.https://{host}.helper",
            "GIT_CONFIG_VALUE_1": (
                '!f() { printf "username=x-access-token\\npassword=%s\\n" '
                '"$GITHUB_TOKEN"; }; f'
            ),
            "GIT_ASKPASS": "/bin/false",
        })
    result = subprocess.run(["git", *map(str, args)], cwd=cwd, env=env,
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise WikiError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def validate_pages(source_dir, pages):
    if not pages:
        raise ValueError("select at least one authored Markdown page")
    root = Path(source_dir).resolve()
    checked = []
    targets = set()
    for source_name, page_name in pages:
        if not PAGE_NAME.fullmatch(page_name) or page_name in (".", ".."):
            raise ValueError(f"invalid Wiki page name: {page_name}")
        if page_name in targets:
            raise ValueError(f"duplicate Wiki page name: {page_name}")
        targets.add(page_name)
        source = (root / source_name).resolve()
        if not source.is_relative_to(root) or not source.is_file() or source.suffix.lower() != ".md":
            raise ValueError(f"invalid authored Markdown source: {source_name}")
        checked.append((source.read_bytes(), page_name + ".md"))
    return checked


def publish_wiki(repository, source_dir, pages, wiki_url=None, apply=False, allow_uninitialized=False):
    """Return planned filenames and exact Wiki commit revision after optional push."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must be OWNER/REPOSITORY")
    checked = validate_pages(source_dir, pages)
    url = wiki_url or f"https://github.com/{repository}.wiki.git"
    with tempfile.TemporaryDirectory(prefix="github-wiki-") as temp:
        checkout = Path(temp) / "wiki"
        try:
            run_git("clone", "--quiet", "--", url, checkout, credential_url=url)
        except WikiError as error:
            if allow_uninitialized:
                return {
                    "mode": "skipped",
                    "reason": "Wiki repository is unavailable or uninitialized",
                    "error": str(error),
                    "changes": [],
                    "revision": None,
                    "links": {},
                }
            raise WikiError(f"Wiki clone failed; enable the Wiki and create its first page before publishing. {error}") from None
        try:
            revision = run_git("rev-parse", "HEAD", cwd=checkout)
        except WikiError as error:
            if allow_uninitialized:
                return {
                    "mode": "skipped",
                    "reason": "Wiki has no first page; initialize it in GitHub before publishing",
                    "error": str(error),
                    "changes": [],
                    "revision": None,
                    "links": {},
                }
            raise WikiError("Wiki has no first page; initialize it in GitHub before publishing") from None
        for _, name in checked:
            if (checkout / name).is_symlink():
                raise WikiError(f"refusing to read or overwrite Wiki page symlink: {name}")
        changes = [name for content, name in checked
                   if not (checkout / name).exists() or (checkout / name).read_bytes() != content]
        if changes and apply:
            for content, name in checked:
                if name in changes:
                    (checkout / name).write_bytes(content)
            run_git("add", "--", *changes, cwd=checkout)
            run_git("-c", "user.name=GitHub Wiki Publisher",
                    "-c", "user.email=wiki-publisher@users.noreply.github.com",
                    "commit", "-m", "Publish authored Wiki pages", cwd=checkout)
            run_git("push", "origin", "HEAD", cwd=checkout, credential_url=url)
            revision = run_git("rev-parse", "HEAD", cwd=checkout)
        base = f"https://github.com/{repository}/wiki"
        links = {name: {"page": f"{base}/{quote(name.removesuffix('.md'))}",
                        "revision": (f"{base}/{quote(name.removesuffix('.md'))}/{revision}"
                                     if apply or name not in changes else None)}
                 for _, name in checked}
        return {"mode": "apply" if apply else "dry-run", "changes": changes,
                "revision": revision, "links": links}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="OWNER/REPOSITORY")
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--page", action="append", required=True,
                        help="authored SOURCE.md=Wiki-Page (repeatable)")
    parser.add_argument("--wiki-url", help="override Wiki Git URL, for GitHub Enterprise or testing")
    parser.add_argument("--apply", action="store_true", help="commit and push changed pages")
    parser.add_argument("--allow-uninitialized", action="store_true",
                        help="skip without error if Wiki is uninitialized or unavailable")
    args = parser.parse_args(argv)
    try:
        pages = []
        for item in args.page:
            if "=" not in item:
                raise ValueError("--page must be SOURCE.md=Wiki-Page")
            pages.append(tuple(item.split("=", 1)))
        result = publish_wiki(args.repo, args.source_dir, pages, args.wiki_url, args.apply,
                              allow_uninitialized=args.allow_uninitialized)
    except (ValueError, WikiError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
