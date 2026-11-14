# Isolate automated report identities

Status: accepted

## Context

The Documentation Freshness Audit and Advisory Review can both report on one
pull request. A shared search rule or link comment would let a clean result,
human closure, or newer finding from one workflow overwrite the other
workflow's evidence. Each reporter also has a distinct status contract:
Documentation Freshness publishes `documentation-freshness`; Advisory Review
is advisory and does not publish that status.

## Decision

Each reporter owns a versioned issue identity marker, PR link comment marker,
managed body markers, and lifecycle. Documentation Freshness uses
`gh-setup-documentation-freshness:v1:` and an identity of
`OWNER/REPO:pr-N` or `OWNER/REPO:default-branch`. Advisory Review uses
`gh-setup-advisory-review:v1:OWNER/REPO#PR` and its `gh-setup-advisory-review-*`
markers. A reporter may only select and update resources carrying its own
marker. Shared labels such as `needs-triage` are ordinary triage metadata and
do not define report identity.

Documentation Freshness clean reruns refresh but do not close an issue;
findings reopen it, and its status passes only when the latest audit is clean
and the managed issue is closed. Advisory Review clean reruns refresh an
existing report but do not close it; same-head human closure is respected, and
actionable findings on a newer head reopen it. Neither reporter changes the
other's issue, PR link comment, or status.

## Consequences

Cross-reporter behavior is tested against one fake repository containing both
reports. Operator guides describe each workflow's permissions, report identity,
clean and closure behavior, and recovery independently. Any future migration
must preserve ownership boundaries or explicitly migrate each report type
without adopting the other's markers.
