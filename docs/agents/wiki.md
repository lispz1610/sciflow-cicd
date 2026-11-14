# Wiki publishing

The repository Wiki is a separate Git repository. The `Wiki sync` workflow publishes the repository's authored Markdown pages to it after changes to `README.md` or `docs/**` reach `main`:

- `README.md` is published as the Wiki `Home` page.
- Markdown files under `docs/` are sorted by path and published with `/` replaced by `-` in the page name. For example, `docs/agents/wiki.md` becomes `agents-wiki`.
- The publisher updates only the pages listed in its command. It does not delete Wiki pages that are not in the current source set.
- The workflow uses the repository `GITHUB_TOKEN` with `contents: write` to push to the Wiki Git remote. GitHub Actions must be enabled and permitted to write repository contents.

## First-time setup

GitHub does not create the Wiki Git repository until its first page exists. Before relying on automation:

1. Enable the Wiki in repository settings.
2. Create the first Wiki page in GitHub's Wiki UI. The publisher cannot initialize an empty Wiki.
3. Set any Wiki editing restrictions in GitHub's UI. The repository API and publisher cannot configure them.
4. Confirm Actions is enabled and that the repository's workflow token is allowed to write contents.

Subsequent changes publish automatically after a qualifying push to `main`. Changes on a pull request branch do not publish before merge.

## Dry run and manual publishing

Preview an explicitly selected set of authored Markdown pages before applying it:

```bash
python3 scripts/publish_github_wiki.py --repo OWNER/REPO \
  --source-dir . --page README.md=Home --page docs/agents/wiki.md=agents-wiki
```

Add `--apply` to publish that selection. The script reports the resulting Wiki revision. It requires Git credentials with write access to the Wiki remote; for an HTTPS GitHub URL it can use `GITHUB_TOKEN` from the environment. Do not put credentials in the command line. For GitHub Enterprise or a test remote, provide `--wiki-url`.

The automated workflow applies its full page selection directly. If it fails, inspect the failed `Wiki sync` run and confirm the Wiki has an initial page, Wiki is enabled, and the Actions token has write access. Fix the cause, then use **Re-run jobs** on the failed run or merge a new change to `README.md` or `docs/**` to trigger another run. For a missing or incorrect page, run the dry run above with the intended page mapping, review the selected paths, and apply it. Unlisted pages remain untouched, so removing a source file does not remove its old Wiki page; remove obsolete pages in the Wiki UI when desired.
