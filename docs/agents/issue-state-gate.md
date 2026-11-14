# Pull request implementation issue gate

The `Issue State Gate` workflow checks every pull request targeting `main`.
The pull request description must identify the implementation issues that it
completes so the gate can verify that each issue is closed before merge.

## Pull request description format

Add exactly one Markdown section named `Implementation issues` and list one or
more same-repository issue references:

```markdown
## Implementation issues
- #81
- #82
```

Bare `#number` references belong to the pull request's base repository. A
qualified `owner/repository#number` reference or a GitHub issue URL is also
accepted when it names that same repository. Repeated references fail as
ambiguous scope; list each issue exactly once.
The section must contain at least one positive issue number. Missing or empty
sections, duplicate sections, and references to other repositories fail with
an actionable check summary.

Pull requests are not implementation issues. The gate resolves every number
through the GitHub Issues API, verifies that the returned repository matches
the pull request's base repository, and rejects API records that represent
pull requests. It passes only when all declared issues have state `closed`.
Open issues are listed by number and current state in the failed check summary.

## Ownership and operation

The pull request author owns the `Implementation issues` section and should
update it when the implementation scope changes. Reviewers confirm that the
listed issues describe the work being merged. The workflow runs from the
trusted base revision and treats the pull request body as input data; it does
not check out or execute pull request code. It publishes a check named
`Issue State Gate` against the pull request's current head SHA using the
repository `GITHUB_TOKEN` with only the read permissions needed to resolve
issues and `checks: write` to publish the result.

The check reports issue state; repository rules must separately require this
check before it blocks merging. That ruleset setup is tracked by issue #86.

The workflow runs without path filters when a PR is opened, reopened,
synchronized, or edited. It re-reads the live PR before evaluating: if the
event's head SHA is stale, that run exits without publishing a check, leaving
the newer event to publish the current result. The current PR body is used
after this revalidation, so queued edits cannot publish an obsolete issue list
onto the same commit. Pull requests from forks are unsupported and receive a
failing check with that reason.

The `Issue State Gate Refresh` workflow also runs when an issue is closed or
reopened. It paginates through open pull requests, finds declarations that
explicitly list the changed issue, re-reads each matching pull request, and
publishes a fresh `Issue State Gate` check for its current head. The evaluator
reads each declared issue's current state, so delayed or replayed events
converge on the latest state. Malformed declarations are handled by the normal
pull request check and are not treated as a match by the refresh workflow.

Issue state changes and Actions runs are asynchronous. Between an issue being
reopened and the refresh workflow publishing its failure, the prior successful
check may still be visible; a merge racing that update can therefore observe
the prior result. The event-driven refresh narrows this window but cannot make
the issue update and pull request merge atomic.

Invalid references, cross-repository references, duplicate references,
inaccessible issues (HTTP 403), deleted issues (HTTP 410), and issues that
GitHub hides as deleted or inaccessible (HTTP 404) fail closed with a specific
summary. Other API failures also fail closed and include the API error.

## Refreshing a check

Editing the PR description or pushing a commit automatically starts a fresh
run. To retry the same event manually, open **Actions → Issue State Gate**,
select its run, and choose **Re-run all jobs**. The rerun still checks the live
PR and skips if that event's head SHA is outdated. A manual rerun cannot make
a fork-based PR supported.

## Reconciling missed or stale results

The **Issue State Gate Reconcile** workflow provides an operator recovery path
when an event was missed, delayed, or produced an outdated check. Start it from
**Actions → Issue State Gate Reconcile → Run workflow**. Leave `apply` unchecked
for a read-only report. The report lists each open PR targeting `main`, its
current head SHA, the expected decision from the shared Issue State Gate
evaluator, and one result classification:

- `absent`: no Issue State Gate check was found.
- `wrong-head`: a check exists on an earlier PR commit, not the current head.
- `wrong-source`: a check exists on the current head, but it was not published
  by the `github-actions` app.
- `stale`: the current-source check is incomplete or its conclusion differs
  from the evaluator's current decision.
- `current`: the current-source check matches the evaluator's decision.

Select `apply` only after reviewing the report. Apply re-reads every live PR,
evaluates its current body and declared issue states through the same evaluator
used by the normal gate, and updates an existing check run or creates one at
the live head. Re-running apply after it succeeds leaves the matching result
unchanged. If the head advances while reconciliation is running, only the newly
read live head is evaluated and refreshed. API failures are reported and do not
stop reconciliation of other PRs; fix access/connectivity and run it again.

Closing a PR without merging also triggers recovery handling. When a closed,
unmerged PR targeting `main` cites a closed implementation issue, apply posts
one visible, replay-safe notice on that issue with options to reopen the PR,
reassign the issue to a replacement PR, or record an explicitly permitted
local-only delivery. The notice does not change the issue state. Update the
issue's `Delivery` evidence after selecting a recovery path. The automatic
closed-PR event runs in apply mode; manual workflow runs remain read-only by
default.

For local operator use, the same report and apply modes are available with a
token that can read pull requests, issues, and check runs, and can write checks
and issue comments in the repository:

```sh
GITHUB_TOKEN="$(gh auth token)" python3 scripts/reconcile_issue_state_gate.py --repo OWNER/REPOSITORY
GITHUB_TOKEN="$(gh auth token)" python3 scripts/reconcile_issue_state_gate.py --repo OWNER/REPOSITORY --apply
```
