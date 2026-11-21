# Advisory Review operator guide

`sciflow-cicd` provides **Advisory Review** exclusively through an external systemd service running on a dedicated VM via the Automated OCR Pipeline (`sciflow-cicd ocr`).

The external runner monitors an explicit repository allowlist using a dedicated, limited OCR GitHub App and runs local `ocr` evaluations directly in `local_direct` execution mode without repository-staged Actions review workflows. It posts strictly advisory reviews bound to each pull request HEAD commit SHA.

Advisory Review is strictly non-blocking: it never approves pull requests, requests blocking changes, or creates a required merge check. See the test suite (`tests/test_ocr_pipeline_e2e.py`) for recorded local end-to-end verification and live-run evidence boundaries.

## Key architectural principles

1. **External systemd VM runner**: Advisory Review runs as a background systemd service on a dedicated virtual machine, completely decoupled from repository GitHub Actions workflows.
2. **Advisory only**: The review runner posts automated review comments on pull requests. It does not approve PRs, merge PRs, create branch protection rulesets, or serve as a required merge check.
3. **Repository allowlisting**: The external runner processes only repositories explicitly declared in its allowlist (`--allowlist OWNER/REPO_A,OWNER/REPO_B`).
4. **Bring Your Own Key (BYOK) on VM host**: Provider API credentials (`OCR_LLM_AUTH_TOKEN`, `OCR_LLM_URL`, `OCR_LLM_MODEL`, `OCR_LLM_FALLBACK_MODEL`, `OCR_LLM_USE_ANTHROPIC`) are stored securely on the dedicated VM host (e.g. `/etc/sciflow-cicd/ocr.env`), never in repository secrets, policies, or Git commits.
5. **Fail-closed fork boundary**: Every pull request is checked before reading PR diffs (`/files`), invoking `ocr`, or calling the LLM provider. Pull requests originating from repository forks are strictly skipped.
6. **Per-HEAD deduplication**: Unchanged HEAD commit SHAs are skipped across polling cycles and daemon restarts. Pushing a new commit updates `head.sha` and triggers one new Advisory Review for that commit.
7. **Clean decoupling from Actions**: The external runner validates host requirements (GitHub App permissions, LLM keys, OCR binary, allowlist scope) without checking for or conflicting with in-repo Actions review workflows.

## Supported provider protocols

Two provider protocols are supported via VM host environment variables:

- **OpenAI-compatible**: Set `OCR_LLM_USE_ANTHROPIC="false"`.
  - `OCR_LLM_URL`: e.g. `https://api.openai.com/v1` (or Google AI Studio `https://generativelanguage.googleapis.com/v1beta/openai/`)
  - `OCR_LLM_MODEL`: e.g. `gpt-4o-mini` or `gemma-4-31b-it`
  - `OCR_LLM_FALLBACK_MODEL`: e.g. `gemini-3.8-flash` (used by long-context failover)
  - `OCR_LLM_USE_ANTHROPIC`: `false`
- **Anthropic**: Set `OCR_LLM_USE_ANTHROPIC="true"`.
  - `OCR_LLM_URL`: e.g. `https://api.anthropic.com/v1`
  - `OCR_LLM_MODEL`: e.g. `claude-3-5-sonnet-20241022`
  - `OCR_LLM_FALLBACK_MODEL`: e.g. `claude-3-7-sonnet-20250219`
  - `OCR_LLM_USE_ANTHROPIC`: `true`

Both protocols require `OCR_LLM_AUTH_TOKEN` exported in the runner environment.

## Limited GitHub App permission boundary

`sciflow-cicd ocr` authenticates with a **dedicated, limited OCR GitHub App**:

- **Required App permissions**: `contents: read`, `pull_requests: write`, `metadata: read`.
- **Forbidden permissions**: `administration` (at any access level) is explicitly rejected during preflight and token minting.
- **Separation from administrative Apps**: Never reuse an administrative or `sciflow-cicd` client GitHub App ID for automated OCR. Pass `--admin-app-id ID` (or set `SCIFLOW_CICD_ADMIN_APP_ID`) so `sciflow-cicd ocr` rejects administrative Apps before minting installation tokens.
- **Private key hygiene**: Store the App PEM private key as a regular, non-symlink file with owner-only permissions (`chmod 600`). `sciflow-cicd ocr` signs short-lived RS256 App JWTs (at most 600s lifetime) using the system `openssl` CLI and exchanges them for installation tokens scoped to a single allowlisted repository per operation.

## Systemd service configuration

Deploy the Automated OCR Pipeline as a persistent systemd service on your dedicated VM:

### 1. Environment configuration (`/etc/sciflow-cicd/ocr.env`)

```bash
# Provider credentials
OCR_LLM_AUTH_TOKEN="sk-your-provider-key"
OCR_LLM_URL="https://api.openai.com/v1"
OCR_LLM_MODEL="gpt-4o-mini"
OCR_LLM_FALLBACK_MODEL="gemini-3.8-flash"
OCR_LLM_USE_ANTHROPIC="false"

# GitHub App configuration
SCIFLOW_OCR_APP_ID="123456"
SCIFLOW_OCR_KEY_PATH="/etc/sciflow-cicd/ocr-app.pem"
SCIFLOW_OCR_ALLOWLIST="OWNER/REPO_A,OWNER/REPO_B"
```

Ensure strict file permissions on the credentials file:
```bash
sudo chmod 600 /etc/sciflow-cicd/ocr.env
sudo chmod 600 /etc/sciflow-cicd/ocr-app.pem
```

### 2. Service unit (`/etc/systemd/system/sciflow-ocr.service`)

```ini
[Unit]
Description=SciFlow Automated OCR Advisory Review Runner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=sciflow
Group=sciflow
EnvironmentFile=/etc/sciflow-cicd/ocr.env
ExecStartPre=/usr/local/bin/sciflow-cicd ocr preflight \
  --app-id ${SCIFLOW_OCR_APP_ID} \
  --private-key-path ${SCIFLOW_OCR_KEY_PATH} \
  --allowlist ${SCIFLOW_OCR_ALLOWLIST}
ExecStart=/usr/local/bin/sciflow-cicd ocr run \
  --app-id ${SCIFLOW_OCR_APP_ID} \
  --private-key-path ${SCIFLOW_OCR_KEY_PATH} \
  --allowlist ${SCIFLOW_OCR_ALLOWLIST} \
  --state-path /var/lib/sciflow-cicd/ocr-state.json \
  --lock-path /var/run/sciflow-cicd/ocr.lock \
  --interval 60 \
  --max-cycles 0
Restart=always
RestartSec=10
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

Enable and activate the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sciflow-ocr
```

## Pipeline subcommands

| Subcommand | Purpose | Key flags |
| --- | --- | --- |
| `sciflow-cicd ocr preflight-app` | Verify limited OCR GitHub App identity, absence of `administration`, admin-App separation, and allowlist installation scope. | `--app-id`, `--private-key-path`, `--allowlist`, `--admin-app-id`, `--json` |
| `sciflow-cicd ocr preflight` | Verify `ocr` executable, `OCR_LLM_*` provider config, App scope, PR eligibility rules, and allowlist accessibility. | `--ocr-bin`, `--eligibility-mode`, `--eligible-label`, `--maintainer`, `--include-drafts`, `--inspect-prs`, `--json` |
| `sciflow-cicd ocr poll` | Poll allowlisted repositories with bounded intervals, rate-limit/5xx backoff, and durable round-robin rotation. | `--max-cycles`, `--max-repos-per-cycle`, `--interval`, `--cursor`, `--state-path`, `--lock-path`, `--json` |
| `sciflow-cicd ocr check-safety` | Verify `local_direct` non-self-hosted runner boundary and classify open PRs against the fail-closed same-repository origin check. | `--execution-mode`, `--json` |
| `sciflow-cicd ocr run` | Execute single-pass (`--max-cycles 1`, default) or bounded continuous advisory review cycles: preflight, poll, fork-safety guard, per-HEAD deduplication, `ocr` subprocess, and advisory comment publication. | `--state-path`, `--lock-path`, `--review-mode`, `--max-reviews`, `--interval`, `--max-cycles`, `--json` |

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

## Fork safety, non-self-hosted boundary, and PR eligibility

- **No self-hosted Actions runners**: `sciflow-cicd ocr check-safety` and `sciflow-cicd ocr run` require `execution_mode=local_direct` and fail closed if self-hosted runner tokens (`RUNNER_REGISTRATION_TOKEN`, `ACTIONS_RUNNER_REGISTRATION_TOKEN`, etc.) or self-hosted runner mode variables are detected.
- **Fail-closed fork boundary**: Every pull request is checked before reading PR diffs (`/files`), invoking `ocr`, or calling the LLM provider. A PR is skipped (`fork_pr` / `skipped_unsafe`) unless `head.repo.fork` is not `true`, `head.repo.full_name` equals `base.repo.full_name` and the allowlisted `OWNER/REPO`, and repository IDs match when present.
- **Candidate eligibility**: By default (`--eligibility-mode either`), open non-draft same-repository PRs are eligible if they carry the `eligible for review` label (configurable via `--eligible-label`) or are authored by a repository maintainer (`OWNER`, `MEMBER`, `COLLABORATOR`, or explicit `--maintainer` logins).

## Per-HEAD deduplication and strictly advisory publication

- **Durable state and single-flight lock**: Pass `--state-path` (atomic JSON writes, symlink-prohibited) and `--lock-path` (single-flight `flock` guard). When `--state-path` is omitted on `sciflow-cicd ocr run`, it defaults to a symlink-safe per-App ID file (`sciflow-cicd-ocr-state-<app_id>.json` in the system temp directory) so per-HEAD deduplication persists across process restarts out of the box. Sharing one `--state-path` between `sciflow-cicd ocr poll` and `sciflow-cicd ocr run` preserves `pull_requests` records via read-modify-write. Each reviewed `(OWNER/REPO, PR, HEAD SHA)` is recorded as `reviewed` only after publication succeeds.
- **Remote marker fallback and re-review on new commits**: Unchanged HEAD SHAs are skipped silently across process restarts (`skipped_already_reviewed`); if local state is missing or lost, `sciflow-cicd ocr run` inspects existing PR reviews and issue comments for `<!-- sciflow-cicd-ocr-advisory:v1:OWNER/REPO#PR@SHA -->` before invoking `ocr` and skips publication when found. Pushing a new commit updates `head.sha` and triggers one new **Advisory Review** for that SHA. If `head.sha` changes mid-run while `ocr` is executing, stale findings are discarded (`head_changed_mid_run`) without posting.
- **Advisory-only endpoints**: Publication permits only `POST /repos/OWNER/REPO/pulls/N/reviews` with `event: "COMMENT"` and `commit_id: <head_sha>` (`--review-mode pr_review`, default), or `POST /repos/OWNER/REPO/issues/N/comments` (`--review-mode issue_comment`). Approvals, change requests, check runs, commit statuses, merges, and GraphQL mutations are prohibited. Every comment embeds `<!-- sciflow-cicd-ocr-advisory:v1:OWNER/REPO#PR@SHA -->`.

## Automated OCR recovery steps

- **Admin App or `administration` permission rejected**: Create or select a separate limited OCR GitHub App with only `contents: read`, `pull_requests: write`, and `metadata: read`, and remove `administration`.
- **`pending_configuration` status**: Ensure `ocr` is installed on `PATH` (or set `OCR_CLI_BIN` / `--ocr-bin`) and export `OCR_LLM_AUTH_TOKEN`, `OCR_LLM_URL`, `OCR_LLM_MODEL`, and `OCR_LLM_USE_ANTHROPIC`. Re-run `sciflow-cicd ocr preflight` to confirm `status: ready`.
- **Transient OCR or GitHub API failure (`retryable_failure`)**: Failed evaluations are recorded with `status: "retryable_failure"` without marking the HEAD SHA `reviewed`; fix the network/provider issue and re-run `sciflow-cicd ocr run` to retry the same HEAD SHA cleanly.
- **Single-flight lock busy (`skipped_busy`)**: Another `sciflow-cicd ocr run` or `poll` process holds `--lock-path`. Wait for the active evaluation to finish before starting another pass.
- **Corrupted or symlinked state/lock/key paths**: `sciflow-cicd ocr` refuses symlinked `--private-key-path`, `--state-path`, and `--lock-path` components and keys with group/world permissions. Fix permissions (`chmod 600`) or replace non-regular/symlinked paths; if a state file is corrupt, inspect and repair or remove `.local/ocr-state.json` (previously posted GitHub comments remain identifiable by their `sciflow-cicd-ocr-advisory:v1:...` HTML marker).

Local end-to-end tests (`tests/test_ocr_pipeline_e2e.py`) verify the full `sciflow-cicd ocr` CLI pipeline against a local loopback GitHub API server, real OpenSSL RS256 key generation/JWT verification, and an executable fake `ocr` script. Live GitHub.com App installations and live external LLM provider runs remain unverified in offline CI.

---

## Integrated Review Setup Architecture Specification

This section consolidates the integrated product contract establishing Advisory Review as an external systemd runner:

### Product Contract & Boundaries

`sciflow-cicd ocr` operates Advisory Review exclusively as an external systemd runner on a dedicated VM across allowlisted repositories. It does not stage or install review workflows into repository Actions.

Advisory Review posts findings as PR review comments. It does not approve or merge them, create a required check, or change a ruleset.

The external runner enforces strict isolation: same-repository PR guard, explicit minimum token permissions (`contents: read`, `pull_requests: write`, `metadata: read`), suppression of untrusted code execution, per-PR head deduplication, and single-flight lock concurrency control. Fork PRs remain skipped. The review is a bring-your-own-key integration; credentials exist solely on the VM host.

### Staging, Planning & Outcomes

The external daemon holds provider configuration (`OCR_LLM_AUTH_TOKEN`, `OCR_LLM_URL`, `OCR_LLM_MODEL`, `OCR_LLM_FALLBACK_MODEL`, `OCR_LLM_USE_ANTHROPIC`) in local VM environment files. Repository setup policies, PRs, Dry-Run previews, and Git logs never contain provider keys.

Readiness preflight (`sciflow-cicd ocr preflight`) verifies the local `ocr` binary, provider environment variables, limited GitHub App credentials, and allowlist repository visibility. If credentials or binaries are missing, preflight outputs `pending_configuration` and exits cleanly with code `0`.
