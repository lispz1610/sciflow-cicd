# P7 automated OCR pipeline validation

This document records the end-to-end verification of the automated Open Code Review (OCR) CLI pipeline (`gh-setup ocr`) for parent issue [#102](https://github.com/lispz1610/github-repo-setup/issues/102) and tickets [#205](https://github.com/lispz1610/github-repo-setup/issues/205)–[#210](https://github.com/lispz1610/github-repo-setup/issues/210).

## Evidence boundary

| Scope | Environment | Status | Notes |
| --- | --- | --- | --- |
| Full `gh-setup ocr` CLI pipeline (`preflight-app`, `preflight`, `poll`, `check-safety`, `run`) | Local subprocesses against loopback HTTP GitHub API server (`127.0.0.1`), real OpenSSL 3.0.13 RSA 2048-bit keypair + RS256 JWT signature verification, and executable fake `ocr` script | **Verified** (`tests/test_ocr_pipeline_e2e.py`) | Exercises process restarts, durable state, scoped token minting, fork safety, competing workflow skip, `pending_configuration`, and `COMMENT` / `issue_comment` advisory publication without network access. |
| Unit & module boundary suites (`ocr_app_auth`, `ocr_poller`, `ocr_preflight`, `ocr_safety`, `ocr_reviewer`, `cli`, `audit_module_dependencies`, `check_markdown_links`) | Local Python 3.12 test environment | **Verified** | Verifies JWT lifetime limits, symlink/path guards, rate-limit backoff, single-flight lock, secret redaction, clean architecture layering, and Markdown links. |
| Live GitHub.com limited OCR GitHub App installation & authentication | Live `api.github.com` with disposable private repository | **Unverified** | Requires registering a separate limited GitHub App (`contents: read`, `pull_requests: write`, `metadata: read`; no `administration`), installing it on a disposable repository, and supplying its App ID and PEM key locally. |
| Live external LLM provider execution | External HTTPS `OCR_LLM_URL` endpoint + real `OCR_LLM_AUTH_TOKEN` + upstream `ocr` binary | **Unverified** | Local end-to-end tests substitute a local executable `ocr` script; live provider latency, billing, and model output quality are not exercised in offline CI. |

## Verified end-to-end scenarios (`tests/test_ocr_pipeline_e2e.py`)

Each scenario invokes the `gh-setup ocr` CLI entrypoint as a fresh subprocess with real OpenSSL RS256 App JWT signing (`openssl dgst -sha256 -sign`) and server-side OpenSSL signature verification (`openssl dgst -sha256 -verify`):

1. **Limited GitHub App preflight and multi-repository PR polling**:
   - `gh-setup ocr preflight-app` rejects any configured administrative/client-setup App ID (`--admin-app-id` / `GH_SETUP_ADMIN_APP_ID` / `GH_SETUP_CLIENT_APP_ID`) before minting installation tokens (exit code `1`).
   - With a valid limited OCR App (`contents: read`, `pull_requests: write`, `metadata: read`), `preflight-app` verifies installation scope across allowlisted repositories and exits `0`.
   - `gh-setup ocr poll --max-repos-per-cycle 1 --state-path ...` rotates cleanly across multiple allowlisted repositories (`acme/alpha-repo`, `acme/beta-repo`) across separate process invocations and selects only eligible same-repository open pull requests.
2. **Missing-credential `pending_configuration` status**:
   - When `OCR_LLM_AUTH_TOKEN` or `OCR_LLM_*` provider environment variables are absent, `gh-setup ocr preflight` and `gh-setup ocr run` report `status: "pending_configuration"` (`pending: true`) with actionable setup instructions and exit `0` without blocking unrelated workflows, reading PR diffs (`/files`), invoking `ocr`, or publishing comments.
3. **Competing **Review Setup** Actions workflow detection and skip**:
   - When an allowlisted repository has an active `.github/workflows/open-code-review.yml` on its default branch, `gh-setup ocr preflight` flags it in `competing_workflows`.
   - `gh-setup ocr run` skips the competing repository (`skipped_competing_workflow: 1`) with migration guidance to avoid duplicate **Advisory Review** comments while still reviewing unaffected allowlisted repositories (or exits `0` with `status: "skipped_competing_workflow"` when all repositories compete).
4. **Fork safety boundary before diff read or OCR execution**:
   - `gh-setup ocr check-safety` reports `runner_boundary.passed: true` (`local_direct` mode, no self-hosted Actions runner) and classifies cross-repository/fork pull requests as `fork_pr` in `skipped_prs`.
   - `gh-setup ocr run` skips fork PRs before requesting `GET .../pulls/<number>/files`, invoking the `ocr` executable, or calling any LLM provider.
5. **Per-HEAD deduplication across restarts, new-commit re-review, and strictly advisory publication**:
   - Pass 1 posts one `COMMENT` pull request review (`POST .../pulls/<number>/reviews`, `event: "COMMENT"`, `commit_id: <head_sha>`) containing `<!-- gh-setup-ocr-advisory:v1:OWNER/REPO#PR@SHA -->` and persists durable state to `--state-path`.
   - Pass 2 (restarted subprocess, unchanged HEAD SHA) skips the already-reviewed HEAD SHA (`skipped_already_reviewed`) with zero `ocr` invocations and zero GitHub publish requests.
   - Pass 3 (after PR HEAD advances to a new commit SHA) triggers a fresh **Advisory Review** for the new HEAD SHA (verified in both `pr_review` and `issue_comment` advisory modes), with zero calls to `/check-runs`, `/check-suites`, `/statuses/`, `/merge`, `/actions/runners`, or `/graphql`, and zero secret leakage in stdout, stderr, or state files.

## Reproducing locally

```bash
python3 -m unittest tests/test_ocr_pipeline_e2e.py -v
python3 -m unittest tests/test_ocr_app_auth.py tests/test_ocr_poller.py tests/test_ocr_preflight.py tests/test_ocr_safety.py tests/test_ocr_reviewer.py tests/test_cli.py -v
python3 scripts/audit_module_dependencies.py
python3 scripts/check_markdown_links.py
```
