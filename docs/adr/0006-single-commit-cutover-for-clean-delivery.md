# Single-commit cutover for clean product delivery

Status: accepted

## Context

The active development repository contains hundreds of iterative exploration commits, temporary integration branches, and review fixups accumulated across specs P0 through P7. Delivering or publishing this tree directly to a new target repository for academic evaluation and production presentation exposes obsolete commit noise, transient artifacts, and historical branch clutter that complicate review.

## Decision

We decided to execute a single-commit cutover when publishing the finalized codebase to the new target repository. The entire verified working tree is exported as a single canonical root commit on the default branch (`main`), while the current development repository is preserved as an immutable development archive and historical audit record.

## Consequences

The target repository opens with a clean, professional, and audit-ready Git history consisting of the finalized v1.0 release. All unit and end-to-end tests, module dependency linters, Markdown links, and CLI entrypoints are verified on the target tree before publication. Detailed commit-by-commit exploration history, PR review discussions, and disposable sandbox trials remain preserved in the development repository without burdening production delivery.

## References

- [Issue #217: Project finalization and cutover](https://github.com/lispz1610/github-repo-setup/issues/217)
