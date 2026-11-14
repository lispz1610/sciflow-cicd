# Spec 3 implementation retrospective

## Outcome

[Spec 3 issue #61](https://github.com/lispz1610/github-repo-setup/issues/61) and its implementation tickets were closed after local commits and checks, before the integration branch was published. The maintainer expected a GitHub pull request. After they identified the gap, [PR #75](https://github.com/lispz1610/github-repo-setup/pull/75) was opened under `lispz1610` and merged into `main`.

The session also added and then reverted a `main-protection` ruleset. The repository has one human maintainer, who authors its issues and PRs. Required code-owner approval would have made that maintainer's own PRs impossible to approve. No such ruleset was applied to the remote repository.

## Findings and proposed environment changes

### 1. High: Define delivery before closing implementation issues

The [issue tracker guide](agents/issue-tracker.md) explains how to close a ticket but does not require a published PR for code changes. The `implement-spec` skill permits reporting an integration branch when it did not open a draft PR. Together, these instructions allowed local implementation to be treated as a completed GitHub delivery.

Add a completion criterion to the issue tracker guide for implementation tickets: publish the integration branch, verify that a PR targets the intended base branch and points to the final commit, link the PR from the issue, and close the issue when that PR is merged. If a task explicitly calls for local-only delivery, record that exception in the issue. A deterministic `gh pr view` check should precede closure.

### 2. Medium: Check reviewer feasibility before creating governance rules

The code-owner ruleset was designed before the workflow's actors were established. A PR author cannot approve their own PR, and a GitHub App cannot serve as a CODEOWNER. With one human maintainer who must remain the PR author, the proposed rule could not be satisfied.

Add an actor check to the [merge-gate guide](merge-gates.md): record who opens the PR, which GitHub identity the token uses, who can provide a counted approval, and who merges. Apply a review rule only after a representative PR proves that the required approval is possible. Keep this as a design check rather than a coding-style rule.

### 3. Medium: Verify remote state with the right credential

The GitHub connector returned 404 for ruleset reads, while an initial local `gh api` request failed to connect. A later authenticated `gh api` request confirmed that the repository had no rulesets or effective rules on `main`. The earlier failures showed limited access, not the repository's state.

Document a read-only fallback in the [issue tracker guide](agents/issue-tracker.md): check `gh` authentication and repository access, retry a repository-scoped API read when the connector lacks Administration permission, and report the state as unknown if neither path succeeds. Verify the authenticated account before publishing a branch or PR when authorship matters.

## Existing checks

The [Python CI workflow](../.github/workflows/python-app.yml) runs tests across Python 3.10, 3.11, and 3.12. The documentation-link and Action-pin workflows check their respective changes. These checks covered the code but could not detect that no PR had been opened. The delivery criterion above addresses that gap without adding another code test.
