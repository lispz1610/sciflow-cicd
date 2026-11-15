# Advisory Review operator guide

`sciflow-cicd` supports **Advisory Review** through two complementary operating modes:

1. **Repository-installed Actions workflow (Review Setup)**: When enabled via **Interactive Setup** or declared in a **Policy** version 2 document, `sciflow-cicd` stages an advisory code review GitHub Actions workflow (`.github/workflows/open-code-review.yml`) and its trusted issue reporter (`scripts/review_issue_reporter.js`) through one unified draft **Setup Pull Request**.
2. **Local operator CLI pipeline (`sciflow-cicd ocr`)**: Monitors an explicit repository allowlist using a dedicated, limited OCR GitHub App and runs local `ocr` evaluations directly in `local_direct` mode without self-hosted Actions runners, posting one strictly advisory review per PR HEAD commit SHA.

Both modes are strictly advisory: neither approves pull requests, requests blocking changes, or creates a required merge check. See the test suite (`tests/test_ocr_pipeline_e2e.py`) for the recorded local end-to-end verification and live-run evidence boundary.

## Key architectural principles

1. **Advisory only**: The review workflow posts automated review comments on pull requests. It does not approve PRs, merge PRs, create branch protection rulesets, or serve as a required merge check.
2. **One Setup Pull Request**: Advisory Review is staged alongside any other selected CI or dependency workflows in a single, owned branch (`codex/workflows-*`) and one draft Setup Pull Request. The repository owner reviews and merges this PR.
3. **Private-only initial support**: Initial release supports private target repositories only. Public repository support is deferred and tracked under [Issue #2](https://github.com/lispz1610/sciflow-cicd/issues/2) pending verification of Actions event policies for public repositories.
4. **Bring Your Own Key (BYOK)**: `sciflow-cicd` never asks for, receives, prints, or stores your LLM provider API key. The API key is stored strictly by the repository owner in GitHub Actions secrets as `OCR_LLM_AUTH_TOKEN`.
5. **Trusted-base execution**: The workflow uses `pull_request_target` anchored to the base repository, requires same-repository PRs (`github.event.pull_request.head.repo.full_name == github.repository`), skips forks, runs with minimum explicit permissions (`contents: read`, `pull-requests: write`), and never checks out or executes untrusted PR code with provider secrets.
6. **Isolated findings reporting**: A second job runs after each successful same-repository review. It has `contents: read`, `issues: write`, and `pull-requests: read` but no provider secret, creates or updates one managed issue per PR, and keeps a separate managed link comment on the PR. New actionable reports use `needs-triage`; the reporter creates that label if missing. A later clean review updates an existing report's status while retaining earlier finding counts, links, and triage label; the issue stays open unless a maintainer closed it. Later actionable runs retain up to three previous actionable reports for context. The review job retains its existing permissions.

The managed issue identity is `sciflow-cicd-ocr-advisory:v1:OWNER/REPO#PR`; the reporter's PR link comment and status/findings blocks use their own `sciflow-cicd-ocr-advisory-*` markers. This identity is independent of Documentation Freshness (`sciflow-cicd-documentation-freshness:v1:`). Each workflow only finds its own issue and link marker, so clean, close, or reopen events in one report lifecycle do not change the other issue, PR link, or status.

## Supported provider protocols

Two provider protocols are supported:
- **OpenAI-compatible**: Set `"use_anthropic": false` in policy. Required repository variables:
  - `OCR_LLM_URL`: e.g. `https://api.openai.com/v1`
  - `OCR_LLM_MODEL`: e.g. `gpt-4o-mini`
  - `OCR_LLM_USE_ANTHROPIC`: `false`
- **Anthropic**: Set `"use_anthropic": true` in policy. Required repository variables:
  - `OCR_LLM_URL`: e.g. `https://api.anthropic.com/v1`
  - `OCR_LLM_MODEL`: e.g. `claude-3-5-sonnet-20241022`
  - `OCR_LLM_USE_ANTHROPIC`: `true`

Both protocols require the secret `OCR_LLM_AUTH_TOKEN` in GitHub Actions secrets.

## Readiness warnings vs staging blockers

`sciflow-cicd` distinguishes safe staging from Review Run readiness:
- **Safe staging**: The workflow file diff, pins, blob SHA, and draft PR are verified before write. If valid, the draft Setup PR is created or updated.
- **Readiness warnings**: If `OCR_LLM_AUTH_TOKEN` or any of the three `OCR_LLM_*` variables are missing from GitHub, or if Actions permissions/retention settings are restrictive, `sciflow-cicd` reports actionable warnings and marks setup status as `pending_configuration`. These warnings do **not** block safe PR staging.
- **Review Run diagnostics**: The rendered workflow contains an early runtime check. If secrets or variables are missing when a PR triggers a Review Run, the job fails immediately with clear GitHub Actions error diagnostics (e.g. `::error::Missing Actions secret OCR_LLM_AUTH_TOKEN`), without exposing any secret values or invoking the external provider.
- **Finding report readiness**: Issues must be enabled for the target repository. The isolated reporter job needs `GITHUB_TOKEN` permissions `contents: read`, `issues: write`, and `pull-requests: read`; readiness warns about issue access because setup cannot verify effective workflow-token permissions. A clean review creates no issue unless a report already exists. Issue bodies contain bounded counters, a human triage checklist, and validated PR/run/summary/inline-comment links, not raw review output. Missing or malformed action counters and persistent GitHub API errors fail the reporter job visibly.

## Recovery and idempotency

- **Idempotency**: Re-running `sciflow-cicd` or `sciflow-cicd apply` against matching repository state performs zero new writes and reuses an existing matching draft PR.
- **Finding report recovery**: A stable hidden marker identifies the managed issue for each PR. Retries find that issue and its managed PR comment before creating replacements. A human-closed issue remains closed for events on the same head; actionable findings on a newer PR head reopen the report and apply `needs-triage`. Stale runs are ignored when the PR head has moved. Transient API failures retry three times; unresolved failures remain visible on the reporter job.
- **Missing issue permissions**: Enable Issues on the repository and ensure the isolated reporter job's token can read contents and pull requests and write issues. The reporter cannot grant its own permissions. Update the workflow permissions or repository policy, then rerun the failed Review Run; a missing permission is an error, not a zero-findings report.
- **Managed updates & drift**: Modifying an existing managed review workflow requires its current GitHub blob SHA in `workflow.expected_sha`. Modifying the separately managed reporter script requires its current blob SHA in `workflow.reporter_expected_sha`. Interactive Setup asks for confirmation before adding these acknowledgements. Unmanaged files or unrecognized modifications result in a conflict error to prevent accidental overwrites.
- **Partial-branch recovery**: If a failure occurs during branch write or PR creation, `sciflow-cicd` reports the exact branch and recovery steps. Branches are never silently deleted.

## Automated OCR CLI pipeline (`sciflow-cicd ocr`)

The `sciflow-cicd ocr` subcommands let maintainers operate **Advisory Review** across an explicit repository allowlist directly from a local operator environment (`local_direct` mode), independently of GitHub Actions **Review Runs**.

### Limited GitHub App permission boundary

Unlike repository configuration (`sciflow-cicd setup` / `sciflow-cicd apply`), which uses a maintainer `GITHUB_TOKEN` (and never requires a GitHub App), `sciflow-cicd ocr` authenticates with a **dedicated, limited OCR GitHub App**:

- **Required App permissions**: `contents: read`, `pull_requests: write`, `metadata: read`.
- **Forbidden permissions**: `administration` (at any access level) is explicitly rejected during preflight and token minting.
- **Separation from administrative Apps**: Never reuse an administrative or `sciflow-cicd` client GitHub App ID for automated OCR. Pass `--admin-app-id ID` (or set `SCIFLOW_CICD_ADMIN_APP_ID`) so `sciflow-cicd ocr` rejects administrative Apps before minting installation tokens.
- **Private key hygiene**: Store the App PEM private key as a regular, non-symlink file with owner-only permissions (`chmod 600`). `sciflow-cicd ocr` signs short-lived RS256 App JWTs (at most 600s lifetime) using the system `openssl` CLI and exchanges them for installation tokens scoped to a single allowlisted repository per operation.

### Provider environment variables and `pending_configuration`

The local worker environment supplies the OCR executable and LLM provider settings:

- `OCR_CLI_BIN` (or `--ocr-bin`): Executable name or path (defaults to `ocr` on `PATH`).
- `OCR_LLM_AUTH_TOKEN`: Secret API key for the provider (redacted from all CLI reports, JSON output, review bodies, and state files).
- `OCR_LLM_URL`: HTTPS provider endpoint without whitespace, embedded credentials, query strings, or fragments (e.g., `https://api.openai.com/v1` or `https://api.anthropic.com/v1`).
- `OCR_LLM_MODEL`: Target model identifier (single line, at most 200 characters).
- `OCR_LLM_USE_ANTHROPIC`: Strictly `"true"` or `"false"`.

If the `ocr` executable or any required `OCR_LLM_*` variable is missing, `sciflow-cicd ocr preflight` and `sciflow-cicd ocr run` exit cleanly (`0`) with `status: "pending_configuration"`, actionable remediation instructions, and **zero** PR diff reads, OCR invocations, or GitHub comments. Invalid values (such as a non-HTTPS `OCR_LLM_URL` or invalid `OCR_LLM_USE_ANTHROPIC`), missing App permissions, or forbidden `administration` permissions fail visibly with exit code `1`.

### Pipeline commands

| Subcommand | Purpose | Key flags |
| --- | --- | --- |
| `sciflow-cicd ocr preflight-app` | Verify limited OCR GitHub App identity, absence of `administration`, admin-App separation, and allowlist installation scope. | `--app-id`, `--private-key-path`, `--allowlist`, `--admin-app-id`, `--json` |
| `sciflow-cicd ocr preflight` | Verify `ocr` executable, `OCR_LLM_*` provider config, App scope, PR eligibility rules, and competing `.github/workflows/open-code-review.yml` workflows. | `--ocr-bin`, `--eligibility-mode`, `--eligible-label`, `--maintainer`, `--include-drafts`, `--inspect-prs`, `--json` |
| `sciflow-cicd ocr poll` | Poll allowlisted repositories with bounded intervals, rate-limit/5xx backoff, and durable round-robin rotation. | `--max-cycles`, `--max-repos-per-cycle`, `--interval`, `--cursor`, `--state-path`, `--lock-path`, `--json` |
| `sciflow-cicd ocr check-safety` | Verify `local_direct` non-self-hosted runner boundary and classify open PRs against the fail-closed same-repository origin check. | `--execution-mode`, `--json` |
| `sciflow-cicd ocr run` | Execute single-pass (`--max-cycles 1`, default) or bounded continuous advisory review cycles: preflight, competing-workflow filter, poll, fork-safety guard, per-HEAD deduplication, `ocr` subprocess, and advisory comment publication. | `--state-path`, `--lock-path`, `--review-mode`, `--max-reviews`, `--interval`, `--max-cycles`, `--json` |

Example single-pass invocation:

```bash
export OCR_LLM_AUTH_TOKEN="..."
export OCR_LLM_URL="https://api.openai.com/v1"
export OCR_LLM_MODEL="gpt-4o-mini"
export OCR_LLM_USE_ANTHROPIC="false"

sciflow-cicd ocr preflight \
  --app-id 123456 \
  --admin-app-id 999999 \
  --private-key-path ~/.config/sciflow-cicd/ocr-app.pem \
  --allowlist OWNER/REPO_A,OWNER/REPO_B

sciflow-cicd ocr run \
  --app-id 123456 \
  --admin-app-id 999999 \
  --private-key-path ~/.config/sciflow-cicd/ocr-app.pem \
  --allowlist OWNER/REPO_A,OWNER/REPO_B \
  --state-path .local/ocr-state.json \
  --lock-path .local/ocr.lock
```

### Fork safety, non-self-hosted boundary, and PR eligibility

- **No self-hosted Actions runners**: `sciflow-cicd ocr check-safety` and `sciflow-cicd ocr run` require `execution_mode=local_direct` and fail closed if self-hosted runner tokens (`RUNNER_REGISTRATION_TOKEN`, `ACTIONS_RUNNER_REGISTRATION_TOKEN`, etc.) or self-hosted runner mode variables are detected.
- **Fail-closed fork boundary**: Every pull request is checked before reading PR diffs (`/files`), invoking `ocr`, or calling the LLM provider. A PR is skipped (`fork_pr` / `skipped_unsafe`) unless `head.repo.fork` is not `true`, `head.repo.full_name` equals `base.repo.full_name` and the allowlisted `OWNER/REPO`, and repository IDs match when present.
- **Candidate eligibility**: By default (`--eligibility-mode either`), open non-draft same-repository PRs are eligible if they carry the `eligible for review` label (configurable via `--eligible-label`) or are authored by a repository maintainer (`OWNER`, `MEMBER`, `COLLABORATOR`, or explicit `--maintainer` logins).

### Per-HEAD deduplication and strictly advisory publication

- **Durable state and single-flight lock**: Pass `--state-path` (atomic JSON writes, symlink-prohibited) and `--lock-path` (single-flight `flock` guard). When `--state-path` is omitted on `sciflow-cicd ocr run`, it defaults to a symlink-safe per-App ID file (`sciflow-cicd-ocr-state-<app_id>.json` in the system temp directory) so per-HEAD deduplication persists across process restarts out of the box. Sharing one `--state-path` between `sciflow-cicd ocr poll` and `sciflow-cicd ocr run` preserves `pull_requests` records via read-modify-write. Each reviewed `(OWNER/REPO, PR, HEAD SHA)` is recorded as `reviewed` only after publication succeeds.
- **Remote marker fallback and re-review on new commits**: Unchanged HEAD SHAs are skipped silently across process restarts (`skipped_already_reviewed`); if local state is missing or lost, `sciflow-cicd ocr run` inspects existing PR reviews and issue comments for `<!-- sciflow-cicd-ocr-advisory:v1:OWNER/REPO#PR@SHA -->` before invoking `ocr` and skips publication when found. Pushing a new commit updates `head.sha` and triggers one new **Advisory Review** for that SHA. If `head.sha` changes mid-run while `ocr` is executing, stale findings are discarded (`head_changed_mid_run`) without posting.
- **Advisory-only endpoints**: Publication permits only `POST /repos/OWNER/REPO/pulls/N/reviews` with `event: "COMMENT"` and `commit_id: <head_sha>` (`--review-mode pr_review`, default), or `POST /repos/OWNER/REPO/issues/N/comments` (`--review-mode issue_comment`). Approvals, change requests, check runs, commit statuses, merges, and GraphQL mutations are prohibited. Every comment embeds `<!-- sciflow-cicd-ocr-advisory:v1:OWNER/REPO#PR@SHA -->`.

### Coexistence and migration with Review Setup Actions workflows

`sciflow-cicd ocr preflight` and `sciflow-cicd ocr run` inspect `.github/workflows/open-code-review.yml` on each allowlisted repository's default branch:

- **Automatic skip**: When an active **Review Setup** workflow is detected on the default branch, `sciflow-cicd ocr run` always skips that repository (`skipped_competing_workflow`) and emits migration guidance so maintainers never receive duplicate **Advisory Review** feedback from both an Actions **Review Run** (with its **Managed Report Issue**) and the local `sciflow-cicd ocr` worker.
- **Fail-closed indeterminate checks**: If competing-workflow inspection cannot prove safety for a repository (for example, default-branch lookup errors, API/network failures, or malformed/unreadable `.github/workflows/open-code-review.yml` content), `sciflow-cicd ocr preflight` surfaces the affected repository under `indeterminate_workflows` and `sciflow-cicd ocr run` fails closed by skipping that repository (`skipped_indeterminate_workflow` / `skipped_competing_workflow`) with actionable recovery guidance while continuing to process other cleanly verified allowlisted repositories.
- **Migrating from Actions Review Setup to `sciflow-cicd ocr`**: Either disable the `Open Code Review` workflow in GitHub (`Actions > Open Code Review > Disable workflow`) or remove `.github/workflows/open-code-review.yml` from the default branch via a reviewed pull request, then re-run `sciflow-cicd ocr preflight`.

### Automated OCR recovery steps

- **Admin App or `administration` permission rejected**: Create or select a separate limited OCR GitHub App with only `contents: read`, `pull_requests: write`, and `metadata: read`, and remove `administration`.
- **`pending_configuration` status**: Ensure `ocr` is installed on `PATH` (or set `OCR_CLI_BIN` / `--ocr-bin`) and export `OCR_LLM_AUTH_TOKEN`, `OCR_LLM_URL`, `OCR_LLM_MODEL`, and `OCR_LLM_USE_ANTHROPIC`. Re-run `sciflow-cicd ocr preflight` to confirm `status: ready`.
- **Transient OCR or GitHub API failure (`retryable_failure`)**: Failed evaluations are recorded with `status: "retryable_failure"` without marking the HEAD SHA `reviewed`; fix the network/provider issue and re-run `sciflow-cicd ocr run` to retry the same HEAD SHA cleanly.
- **Single-flight lock busy (`skipped_busy`)**: Another `sciflow-cicd ocr run` or `poll` process holds `--lock-path`. Wait for the active evaluation to finish before starting another pass.
- **Corrupted or symlinked state/lock/key paths**: `sciflow-cicd ocr` refuses symlinked `--private-key-path`, `--state-path`, and `--lock-path` components and keys with group/world permissions. Fix permissions (`chmod 600`) or replace non-regular/symlinked paths; if a state file is corrupt, inspect and repair or remove `.local/ocr-state.json` (previously posted GitHub comments remain identifiable by their `sciflow-cicd-ocr-advisory:v1:...` HTML marker).

Local end-to-end tests (`tests/test_ocr_pipeline_e2e.py`) verify the full `sciflow-cicd ocr` CLI pipeline against a local loopback GitHub API server, real OpenSSL RS256 key generation/JWT verification, and an executable fake `ocr` script. Live GitHub.com App installations and live external LLM provider runs remain unverified in offline CI.

## Credential-free Policy v2 examples

### OpenAI-compatible protocol

```json
{
  "version": 2,
  "stages": [
    {
      "name": "features",
      "enable": ["issues"]
    },
    {
      "name": "actions"
    },
    {
      "name": "merge",
      "settings": {
        "delete_branch_on_merge": true
      }
    },
    {
      "name": "actions-policy",
      "settings": {
        "sha_pinning_required": true
      }
    },
    {
      "name": "review",
      "workflow": {
        "template": "open-code-review",
        "template_version": 1,
        "filename": "open-code-review.yml",
        "triggers": ["pull_request_target"],
        "events": ["opened", "synchronize", "reopened"],
        "fork_policy": "skip",
        "action_sha": "182898cf522da3d04157b422752d028417974e19",
        "ocr_version": "1.12.12",
        "max_tokens_budget": 50000,
        "timeout_minutes": 30
      },
      "llm": {
        "url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "use_anthropic": false,
        "secret_name": "OCR_LLM_AUTH_TOKEN"
      },
      "limits": {
        "max_retention_days": 30
      }
    }
  ]
}
```

### Anthropic protocol

```json
{
  "version": 2,
  "stages": [
    {
      "name": "features",
      "enable": ["issues"]
    },
    {
      "name": "actions"
    },
    {
      "name": "merge",
      "settings": {
        "delete_branch_on_merge": true
      }
    },
    {
      "name": "actions-policy",
      "settings": {
        "sha_pinning_required": true
      }
    },
    {
      "name": "review",
      "workflow": {
        "template": "open-code-review",
        "template_version": 1,
        "filename": "open-code-review.yml",
        "triggers": ["pull_request_target"],
        "events": ["opened", "synchronize", "reopened"],
        "fork_policy": "skip",
        "action_sha": "182898cf522da3d04157b422752d028417974e19",
        "ocr_version": "1.12.12",
        "max_tokens_budget": 50000,
        "timeout_minutes": 30
      },
      "llm": {
        "url": "https://api.anthropic.com/v1",
        "model": "claude-3-5-sonnet-20241022",
        "use_anthropic": true,
        "secret_name": "OCR_LLM_AUTH_TOKEN"
      },
      "limits": {
        "max_retention_days": 30
      }
    }
  ]
}
```

---

## Integrated Review Setup Architecture Specification

This section consolidates the integrated product contract merging review setup into the canonical `sciflow-cicd` client:

### Product Contract & Boundaries

`sciflow-cicd` configures repository settings and optionally enables Advisory Review. It is the only installed command. Review Setup is off by default for a new repository. The integrated release supports Review Setup for private target repositories only; public repository support is tracked in [GitHub issue #2](https://github.com/lispz1610/sciflow-cicd/issues/2).

Advisory Review posts findings on pull requests. It does not approve or merge them, create a required check, or change a ruleset. The owner controls whether to merge the draft Setup Pull Request that installs it. Repository settings that GitHub exposes directly still apply directly. Every selected workflow in one setup session, including PR CI, dependency review, and Advisory Review, goes into one Setup Pull Request.

The workflow keeps the existing trusted-base `pull_request_target` design, same-repository PR guard, explicit minimum token permissions, pinned upstream Action and CLI, per-PR cancellation, timeout, token budget, and suppression of raw artifacts. It treats PR content as review data and never checks out or executes untrusted PR code with the provider secret. Fork PRs remain skipped. The review is a bring-your-own-key integration; setup never asks for or receives the provider key.

### Staging, Planning & Outcomes

The review stage stores only nonsecret provider configuration: HTTPS endpoint, model, provider protocol, fixed secret name `OCR_LLM_AUTH_TOKEN`, selected events, token budget, timeout, optional trusted rule path, retention limit, and exact pins. The required repository variables are `OCR_LLM_URL`, `OCR_LLM_MODEL`, and `OCR_LLM_USE_ANTHROPIC`. The provider key is supplied by the owner directly in GitHub Actions secrets. Policies, examples, Dry-Run output, and logs never contain the key.

Readiness is separate from safe staging. Missing `OCR_LLM_AUTH_TOKEN`, missing or mismatched `OCR_LLM_*` variables, disabled or restrictive Actions policy, and excessive retention produce actionable warnings during setup and do not prevent a safe Setup Pull Request. Setup may finish with **review pending configuration** and Headless Mode exits zero when the selected safe changes were applied or staged successfully and only readiness warnings remain.

