# Issue tracker: GitHub

Issues and specs for this repository live in GitHub Issues. Use `gh` from this checkout; the origin remote identifies `lispz1610/sciflow-cicd`.

## Operations

- List: `gh issue list --state open --json number,title,body,labels,assignees`.
- Read: `gh issue view <number> --comments`.
- Create: `gh issue create --title "..." --body-file <file>`.
- Update labels: `gh issue edit <number> --add-label <label>` or `--remove-label <label>`.
- Close: `gh issue close <number> --comment "..."`.
- Link a child: `gh issue create --parent <number>`, or `gh issue edit <parent> --add-sub-issue <child>`.
- Record blockers: `gh issue edit <child> --add-blocked-by <blocker>`.

## Pull requests as a triage surface

**PRs as a request surface: no.** Triage GitHub Issues; do not treat external PRs as requests unless this setting changes.

## Skill vocabulary

"Publish to the issue tracker" means create a GitHub issue. "Fetch the relevant ticket" means read its GitHub issue, including comments and labels.

## Wayfinding

A map is an issue labelled `wayfinder:map`; its tickets are child issues. Use native issue dependencies for blockers. Claim a ready ticket by assigning it to the working developer. On resolution, record the answer in a comment, close the child issue, and link the outcome from the map.

## Automated issue frontier orchestration

The repository's event-driven issue frontier orchestrator (`.github/workflows/issue-frontier-orchestrator.yml` running `scripts/orchestrate_issue_frontier.py`) automatically evaluates and transitions dependent issues:

- **Event trigger:** Executes whenever an issue is closed (`issues: [closed]`).
- **Dependency discovery:** Discovers dependent tickets using native GitHub GraphQL connections (`blockedBy` / `blocking`) and Markdown body declarations (`## Blocked by` bullet lists, inline `Blocked by: #...`, qualified `owner/repo#...` references, and full issue URLs).
- **Frontier transition invariant:** A dependent ticket transitions to the active frontier if and only if **all** of its declared blockers are closed. If any blocker remains open, the ticket remains unlabelled and blocked.
- **Transition actions:**
  1. Automatically attaches the `ready-for-agent` label to the unblocked ticket.
  2. Posts a structured wayfinder comment detailing the resolved blockers (`🚀 All blockers resolved ([#...]). This ticket is unblocked and now on the active frontier ready for agent implementation.`).
- **Cycle protection:** Employs DFS cycle detection to prevent transitioning tickets involved in dependency loops, recording diagnostic warnings instead.

See the [Issue DAG Frontier Orchestration guide](../dependency-graph-governance.md#issue-dag-frontier-orchestration) for detailed operations, dry-run commands, and recovery procedures.


## Managed report issue ownership

Automated report issues are not shared between reporters. Documentation
Freshness and Advisory Review each use a workflow-owned identity marker, PR
link comment, and lifecycle. Human triage and closure apply to the specific
report's instructions; the shared `needs-triage` label does not make reports
interchangeable. See the [Documentation Freshness guide](../documentation-freshness.md)
and [Advisory Review guide](../advisory-review.md).

## Implementation delivery and closure

For an implementation issue, publish the integration branch as a pull request to `main` before closing the issue. Record the pull request and the exact full head commit SHA in the issue's `Delivery` section, and include an `Implementation issues` section in the pull request body that links back to each implementation issue. For example:

Keep its canonical `ready-for-agent` or `ready-for-human` label through closure. The close handler also treats a structured `## Parent` issue link or a declaration in an open, same-repository integration pull request targeting `main` as implementation scope. It reads these signals from live GitHub data; removing a role label does not exempt a declared implementation issue. An in-scope issue with no `Delivery` section fails verification and is reopened. An unrelated issue with none of these signals is left closed. If the handler cannot verify the open pull request list, it reports an API failure and makes no issue write; rerun it after access is restored.

```markdown
## Implementation issues
- https://github.com/OWNER/REPOSITORY/issues/123
```

Close an implementation issue after its published pull request is accepted and before it is merged. This lets the required Issue State Gate verify that every implementation issue is closed at merge time.

Use this order:

1. Publish the integration branch as an open pull request targeting `main`.
2. Add this evidence to the implementation issue body, using exactly one pull request link and one full 40-character head SHA:

   ```markdown
   ## Delivery
   - Pull request: https://github.com/OWNER/REPOSITORY/pull/123
   - Head SHA: `0123456789abcdef0123456789abcdef01234567`
   ```

3. Verify the published evidence before closing the issue:

   ```sh
   GH_TOKEN="$(gh auth token)" python3 scripts/verify_issue_delivery.py \
     --repo OWNER/REPOSITORY --issue ISSUE_NUMBER
   ```

   The command is read-only and emits one JSON result. `eligible: true` means the issue's pull request is published and open, targets `main`, belongs to the same repository, has an open delivery branch at the recorded SHA, and links back to the issue in its `Implementation issues` section, or is a valid merged recovery path as described below. Ineligible `code` values identify absent or ambiguous delivery evidence, inaccessible, closed, or draft pull requests, repository/base/branch mismatches, stale SHAs, a missing issue link, an issue not closed after a merged recovery PR, local-only delivery, and API failures. Resolve any ineligible result before closure; API failures do not count as verification.

4. After the pull request is published and accepted, close the implementation issue before merging. Do not use a closing keyword as a substitute for the explicit `Implementation issues` list; the issue must already be closed for the required check to pass.

A pull request that is already merged is accepted only as a recovery path for an issue that was closed after the merge. The recorded `Delivery` head SHA must still match the merged pull request's head SHA, the pull request must report a non-empty `merged_at` and a valid 40-character `merge_commit_sha`, its base must be `main` in the same repository, and its body must link the issue from exactly one `## Implementation issues` or `## Parent` section. The delivery branch is not queried because the merge may have deleted it. A closed but unmerged pull request remains ineligible. This recovery path does not replace the normal requirement to verify an open pull request before closing the issue.

Local-only work does not satisfy the normal implementation delivery check. Use it only when the issue explicitly calls for local-only delivery by recording `Local-only exception` in its `Delivery` section. The close handler allows that documented exception, but the normal pull-request verifier reports it as ineligible and the Issue State Gate fails if a PR lists that closed issue.

### Enforcing closure evidence

The issue-delivery enforcement workflow handles `issues: [closed]` events for implementation issues. The event payload is only a trigger: the handler re-reads the issue and checks its current state, then runs the same live pull request and branch validation described above. A qualifying issue stays closed and receives a managed verification receipt bound to the exact issue body. Replayed close events recognize that receipt, re-read the linked pull request, and do not reject the closure if the shared integration branch has advanced since verification. If two reads disagree while the first verification is in flight, the handler reports `verification_race` and makes no enforcement write; rerun after the pull request settles. If the verifier returns ineligible evidence, the handler posts one managed diagnostic comment and reopens the issue. A comment is written before reopening so a retry can finish a failed reopen without creating a second diagnostic.

The handler returns one JSON result with `action` and `code`. An `api_failure` exits unsuccessfully and makes no enforcement write; API failures must be resolved and the workflow rerun. A stale close event for an issue that is already open is a no-op. If reopening fails after the diagnostic is recorded, rerun the workflow for that issue: the existing managed diagnostic is detected and the handler retries the reopen.

The workflow uses `contents: read`, `issues: write`, and `pull-requests: read`. It runs checked-in code from the repository's trusted default branch and does not check out or execute pull request content. Runs are serialized by issue number so simultaneous close events do not race while checking for the managed diagnostic. The GitHub Actions token reads live issue labels, issue state, comments, and pull request evidence; it updates only the managed issue comment and state. The selected Issue State Gate Setup Pull Request also stages the Python runtime modules called by this workflow and the gate, refresh, and reconciliation workflows. Installation stops on conflicting unmanaged runtime files.
