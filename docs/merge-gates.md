# Merge gates

The `gates` stage (`scripts/configure_github_repo.py:reconcile_gates`) manages one named repository branch ruleset on `refs/heads/<default_branch>`. Dry run is the default. It reads the repository, an open evidence pull request (`--evidence-pr`), the installed Actions workflows, and successful workflow and check runs on either the PR head or GitHub's current test merge commit. Each required check must belong to the selected workflow's successful check suite, and the ruleset pins each required check to the originating GitHub Actions App ID (`integration_id`, e.g. `15368`).

## GitHub UI Navigation for Merge Settings and Branch Rules

Use these exact GitHub web interface navigation paths when inspecting or manually configuring pull request merge behavior and branch protection:

- **Pull Request Merge Methods & Automatic Head Branch Deletion:** `Settings -> General -> Pull Requests` (`allow_merge_commit`, `allow_squash_merge`, `allow_rebase_merge`, `delete_branch_on_merge`, `allow_update_branch`).
- **Branch Rulesets (managed by `gh-setup`):** `Settings -> Rules -> Rulesets`.
- **Classic Branch Protection (legacy / manual alternative):** `Settings -> Branches`.

## Required Check Names and Stable Status Check Anchors

Specify each check as `WORKFLOW_PATH=CHECK_NAME` (or `CHECK_NAME` in Interactive Setup / Policy JSON when mapped to `.github/workflows/ci.yml`). Use the exact job check name shown in the PR **Checks** tab and the installed workflow file path:

| Workflow Template / Profile | Workflow Path | Canonical Check Name (`CHECK_NAME`) | Notes |
| --- | --- | --- | --- |
| Scientific Python (`profile-ci`: `python` / `python-scientific`) | `.github/workflows/ci.yml` | `status-check` | Unmatrixed terminal **Stable Status Check** anchor ([guide](scientific-ci-profiles.md#stable-status-check-anchor-pattern)). |
| Scientific CMake / MPI (`profile-ci`: `cmake-mpi` / `cpp-hpc-mpi`) | `.github/workflows/ci.yml` | `status-check` or `smoke-test` | Defaults to `status-check`; supports custom `status_check_name: "smoke-test"` for MPI smoke gates. |
| Standard PR CI (`pr-ci` / `ci`) | `.github/workflows/ci.yml` | `ci` / `test` / `ci-check` | Single-job pull request CI check. |
| PR Dependency Review (`dependency-review`) | `.github/workflows/dependency-review.yml` | `Dependency Review` / `dependency-review` | Scans PR dependency manifest diffs for High/Critical CVEs and license compliance. |
| Issue State Gate (`issue-state-gate` stage) | `.github/workflows/issue-state-gate.yml` | `Issue State Gate` | Managed via the dedicated `issue-state-gate` ruleset ([see below](#optional-issue-state-gate)). |

Repeated check names are rejected, and **Advisory Review** (`.github/workflows/open-code-review.yml`) is never permitted as a required merge check. If a configured ruleset check name does not match the active or staged `profile-ci` workflow's declared `status_check_name`, `setup_github_repo.py` and `reconcile_gates` block the ruleset write with `Discrepancy detected: configured ruleset check '<check>' does not match profile workflow declared check '<declared_check>'`.

A workflow that is absent or inactive, a stale approval (when approvals `> 0`), an incomplete or failed run, or a changed PR head blocks the write. Repeat the dry run immediately before apply because PR and check state can change.

When updating a named ruleset, the command refuses to replace it if it contains other rule types, extra rule parameters or required checks, bypass actors, or unrelated conditions. Resolve that drift explicitly so unrelated protections are never silently removed.

## Solo Maintainer Merge Path (`required_approving_review_count: 0`)

Solo maintainers do not have a second human account to approve their own pull requests. `gh-setup` supports a first-class solo-maintainer merge path across all three interfaces while preserving a human pull-request merge decision and strict automated checks:

1. **Interactive Setup (`gh-setup`):** When prompted for `Reviewer username (or 'none' for solo maintainer with zero required approvals)`, enter `none` (or `solo` / `-`).
2. **Policy JSON (`gates` stage):** Omit `"reviewer"` (or set `"reviewer": null`) and set `"required_approving_review_count": 0`:
   ```json
   {
     "name": "gates",
     "ruleset_name": "quality-gate",
     "evidence_pr": 12,
     "required_approving_review_count": 0,
     "checks": {
       "status-check": ".github/workflows/ci.yml"
     }
   }
   ```
3. **Direct CLI (`scripts/configure_github_repo.py gates`):** Pass `--approvals 0` without `--reviewer`:
   ```bash
   python3 scripts/configure_github_repo.py --repo OWNER/REPO gates \
     --name quality-gate --evidence-pr 12 --approvals 0 \
     --check .github/workflows/ci.yml=status-check
   python3 scripts/configure_github_repo.py --repo OWNER/REPO --apply gates \
     --name quality-gate --evidence-pr 12 --approvals 0 \
     --check .github/workflows/ci.yml=status-check
   ```

### Effective Ruleset Parameters (`pull_request` and `required_status_checks`)

Whether configured for a solo maintainer (`required_approving_review_count: 0`) or a collaborative team (`required_approving_review_count: 1`+), `reconcile_gates` writes and verifies the following ruleset payload on `refs/heads/<default_branch>`:

- `pull_request` rule parameters:
  - `required_approving_review_count`: `0` (solo maintainer) or `>= 1` (collaborator path)
  - `dismiss_stale_reviews_on_push: True`
  - `require_code_owner_review: False`
  - `require_last_push_approval: False`
  - `required_review_thread_resolution: True`
- `required_status_checks` rule parameters:
  - `strict_required_status_checks_policy: True`
  - `required_status_checks`: `[{"context": "<CHECK_NAME>", "integration_id": <app_id>}]`

For detailed maintainer guides, GitHub UI walkthroughs, and recorded verification evidence, see [Proportional Security and Merge Controls](proportional-security-and-controls.md).

## Collaborator Merge Path (`required_approving_review_count >= 1`)

For multi-person repositories requiring an independent human reviewer (`--approvals 1` default):

```bash
python3 scripts/configure_github_repo.py --repo OWNER/REPO gates \
  --name code-review --evidence-pr 123 --reviewer REVIEWER \
  --check .github/workflows/ci.yml=ci-check
python3 scripts/configure_github_repo.py --repo OWNER/REPO --apply gates \
  --name code-review --evidence-pr 123 --reviewer REVIEWER \
  --check .github/workflows/ci.yml=ci-check
```

`reconcile_gates` verifies that `REVIEWER` is distinct from the evidence PR author, holds `write`, `maintain`, or `admin` collaborator permission on the repository, and has submitted a current `APPROVED` review on the evidence PR's `head.sha` (not superseded by `CHANGES_REQUESTED` or `DISMISSED`).

## Visibility, Plan Limits, and Two-Pass Activation

GitHub offers repository rulesets on **public** repositories under `Free` plans. **Private** repositories require a `Pro`, `Team`, or `Enterprise` plan (`Settings -> Rules -> Rulesets`). For a private repository, `reconcile_gates` checks the owner plan returned by `GET /users/{owner}` or `GET /orgs/{owner}` (`plan.name` in `pro`, `team`, `business`, `enterprise`) and stops with `private repository rulesets require a verifiable Pro, Team, or Enterprise plan` (`[UNSUPPORTED]` in `gh-setup doctor`) if the account is on `Free`. The token needs repository administration permission (`Administration: read/write`) and read access to pull requests, checks, Actions, and collaborator permissions.

When `workflows` / `ci-profile` and `gates` are selected together on a repository where the CI workflow is not yet on `main`, `gh-setup` stages the workflow in the draft **Setup Pull Request** and marks `gates` as `status: "pending"` (*"Merge the draft Setup Pull Request, then provide an approved evidence PR with successful CI checks before applying this gate"*). After merging the Setup PR and observing a passing check run on an open evidence PR, re-run `gh-setup` (or `gh-setup apply`) to activate the ruleset.

Select a workflow check as a required gate only after observing both its successful and failing conclusions. A review comment alone is not evidence of a blocking check. After applying, re-read the named ruleset and repeat the direct `gates` command to check for an empty change list (`[]`). An independent-reviewer live exercise against a remote two-account repository has not been recorded for this tool; the solo-maintainer live ruleset exercise is recorded in [Issue State Gate live evidence](issue-state-gate-live-evidence.md).

References: [repository rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository), [required status checks](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks), [GitHub Actions workflow runs API](https://docs.github.com/en/rest/actions/workflow-runs).

# Optional Issue State Gate

Policy may include `{"name": "issue-state-gate"}` to select the trusted
Issue State Gate workflow suite: the primary check, issue close/reopen refresh,
and operator reconciliation workflows. They are staged together in the same
draft Setup Pull Request as other selected workflows. Dry-Run reports the
workflow, context, source derivation, effective rule, prerequisites, and
conflicts without writing. Apply leaves the rule pending until the owner merges
the Setup Pull Request and a successful check from that active workflow is
observable on an open pull request. The source integration ID is read from
that check run; it is never guessed. Unsupported or unavailable evidence is
reported as pending or blocked and does not activate the rule.

The Issue State Gate is a separate ruleset requiring pull requests and the
exact required status check. It requires zero approving reviews, no code-owner
review, no last-push approval, and no review-thread resolution. Required status
checks do not require branch synchronization. It has no bypass actor. Existing
unmanaged rules, branch conditions, required checks, or
bypass actors on the managed `issue-state-gate` ruleset cause Apply to stop.
After writing, `gh-setup` reads the ruleset back and reports success only when
the active rule exactly matches the requested check and source.

If Apply reports `pending`, inspect and merge the Setup Pull Request, then
wait for a successful `Issue State Gate` check on an open pull request to main
and rerun the same policy. A blocked permission read needs Actions, pull
request, and check-run read access; ruleset writes need repository
administration access. If the repository does not use `main` as its default
branch, the stage reports unsupported. For a ruleset conflict, inspect its
existing conditions, rules, required checks, and bypass actors; preserve
those protections while resolving ownership before retrying.
