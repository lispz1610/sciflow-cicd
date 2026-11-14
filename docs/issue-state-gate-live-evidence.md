# Issue State Gate live proof

Recorded 2026-10-12 against the disposable public repository
[`lispz1610/issue-state-gate-live-probe-20261012`](https://github.com/lispz1610/issue-state-gate-live-probe-20261012).
The probe issue is [#1](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/issues/1)
and the intentionally unmerged PR is [#2](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/pull/2).
The repo was created solely for this exercise and is retained so these observations
remain inspectable. The PR is still open; no merge was attempted.

## Repository and effective rule

- Visibility: public; default branch: `main`; Actions enabled; allowed actions:
  `all`.
- Default workflow token permission is `read`; approving PRs through the token
  is disabled. The gate job's Actions log showed only `checks: write`,
  `contents: read`, `issues: read`, and `pull-requests: read` (plus GitHub's
  implicit metadata read). The reconciliation workflow separately grants
  `issues: write` for its recovery comments.
- The public account accepted an active repository ruleset. The managed
  [Issue State Gate ruleset](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/rules/24936220)
  requires pull requests with zero required approvals, no code-owner or last-push
  approval, no review-thread resolution, no extra approval for unattributed
  changes, and all three merge methods. Its required status check is exact
  context `Issue State Gate`, source integration ID `15368` (`github-actions`),
  with strict branch synchronization disabled. The active readback has no bypass
  actors. The earlier probe-specific ruleset [24934871](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/rules/24934871)
  remains active with the same required status check and no pull-request rule.
  The same values appear in the
  [effective main rules API](https://api.github.com/repos/lispz1610/issue-state-gate-live-probe-20261012/rules/branches/main).
- Source and context came from an observed check run on the current PR head,
  alongside a successful run of the active trusted workflow. The integration
  app ID and context were then read back from the active ruleset.
- No `merge_queue` rule is configured and no merge-group event was run. Merge
  queue behavior is therefore untested and not claimed here.

## Live state transitions

The PR body declared `#1` under one `Implementation issues` section. Before
each issue close, `scripts/verify_issue_delivery.py` returned
`eligible: true` for PR #2 and the exact current branch SHA.

| Observed state | Evidence | GitHub merge decision |
| --- | --- | --- |
| Issue #1 reopened from closed state | [Issue-reopen refresh run](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/actions/runs/38267502165) published [failed check run 114873311699](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/runs/114873311699) on `bd2718a0e027c846ab40f9651dc22e4eb73a1b58`. | `mergeStateStatus=BLOCKED`; required check was `fail`. |
| Issue #1 closed again with eligible Delivery evidence | [Issue-close refresh run](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/actions/runs/38267587005) published [passing check run 114873597014](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/runs/114873597014) on the same SHA. | `mergeStateStatus=CLEAN`, `mergeable=MERGEABLE`; required check was `pass`. |
| New PR head with only an older check | PR advanced to `c62a0fadfde47a2e9c1e810f8f7a58bdb6215299` while the PR gate workflow was temporarily disabled. The previous `Issue State Gate` result was on an earlier SHA. `gh pr checks --required` reported no checks on this head. | `mergeStateStatus=BLOCKED`. |
| Read-only reconcile of that head | [Reconcile run 38267886071](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/actions/runs/38267886071) classified PR #2 as `wrong-head`, expected `success` at `c62a0fadfde47a2e9c1e810f8f7a58bdb6215299`. | Still `BLOCKED`; report mode made no writes. |
| Apply reconcile | [Reconcile run 38267939038](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/actions/runs/38267939038) refreshed PR #2 at the current head. [Check run 114874830167](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/runs/114874830167) passed from `github-actions`. | `mergeStateStatus=CLEAN`, `mergeable=MERGEABLE`; `gh pr checks --required` showed `Issue State Gate pass`. The ruleset remained active throughout. |
| Managed ruleset reapplied after exact-source verification | [Trusted Issue State Gate workflow run 38269427033](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/actions/runs/38269427033) and its [current-head check](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/runs/114879771921) succeeded. Ruleset 24936220 was read back active with the exact context/source, PR requirement, zero reviews, and strict synchronization false. | `mergeStateStatus=CLEAN`, `mergeable=MERGEABLE`, `reviewDecision` empty; issue #1 remains closed and PR #2 remains open and unmerged. |
| Issue #1 reopened after combined rule activation | [Refresh run 38270569126](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/actions/runs/38270569126) reevaluated PR #2 on the current SHA and published [failed check 114883560186](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/runs/114883560186). | `mergeStateStatus=BLOCKED`; required `Issue State Gate` conclusion was `failure`. The required check blocked merge while the issue was open. |
| Issue #1 closed again with verified current delivery | Before closing, `verify_issue_delivery.py` returned `eligible: true`, PR #2, and SHA `c62a0fadfde47a2e9c1e810f8f7a58bdb6215299`. [Refresh run 38270660347](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/actions/runs/38270660347) published [passing check 114883868007](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/runs/114883868007) on that same SHA. | `mergeStateStatus=CLEAN`, `mergeable=MERGEABLE`; required `Issue State Gate` conclusion was `success`. Issue #1 is closed; PR #2 remains open and unmerged. |

The latest issue body records the same PR URL and full SHA as the current head;
the delivery verifier returned `eligible: true`. Issue #1 is closed, PR #2 is
open and unmerged, and the current required check is successful.

## Cancellation, repair, and limits

- A workflow run was intentionally canceled on head
  `76652b121deb6b0a671bb64b3382972bbb811b97`:
  [run 38267664990](https://github.com/lispz1610/issue-state-gate-live-probe-20261012/actions/runs/38267664990).
  The run's job check was canceled, but the script had already published the
  separate required `Issue State Gate` check successfully. This did **not**
  cancel the required result. Attempting to change that check with the operator's
  OAuth token was rejected by GitHub (`403: You must authenticate via a GitHub
  App`), so a canceled required check itself was not demonstrated. The missing
  and wrong-head required-check cases above did block; report/apply reconciliation
  repaired the missing current-head result without a bypass.
- A first refresh attempt exposed a direct-script import failure in both
  refresh and reconcile entry points (`ModuleNotFoundError: No module named
  'scripts'`). The two scripts now fall back to importing their sibling module
  when run by file path. Regression coverage is in
  `tests/test_issue_gate_cli_imports.py`; 15 focused CLI, refresh, and reconcile
  tests passed. The probe was updated with this fix before the successful
  close/reopen and reconciliation observations.
- Issue updates, event delivery, check publication, and PR merge evaluation are
  separate asynchronous operations. The rule cannot make issue state changes
  atomic with a merge; the event refresh narrows that timing window. An operator
  can run the read-only reconcile workflow to inspect drift, then apply it to
  repair an absent, stale, or wrong-head check.
- The initial PR head had a prior failed workflow-job check from its open-issue
  run. GitHub continued to report `BLOCKED` after the custom check later passed
  on that same SHA. The later clean-head transitions above avoid that earlier
  failed run and show the effective gate decision on a head with a clean passing
  workflow run. No claim is made that a passing check instantly clears every
  previously failed run associated with that same commit.

The required PR head used for final reconciliation was
`c62a0fadfde47a2e9c1e810f8f7a58bdb6215299`. The disposable PR was never merged.
