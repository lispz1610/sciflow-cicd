# Open Code Review Setup v1 Implementation Plan

> [!NOTE] Historical Document
> This implementation plan is preserved as historical documentation for the initial independent release. The review integration has since been merged into `github-repo-setup` under the [Integrated Review Setup specification](../integrated-review-setup-spec.md).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an independent private setup project that installs advisory OpenCodeReview comments into a target repository's pull request workflow through a guarded draft PR.

**Architecture:** A Python CLI validates an owner-supplied JSON policy, reads GitHub prerequisites, renders one fixed workflow, and stages it through an owned REST client. The installed GitHub Actions workflow runs the pinned upstream Action and posts advisory comments; a separately hosted GitHub App is deferred pending a demonstrated policy block or measured volume.

**Tech Stack:** Python 3.11+ standard library, `unittest`, GitHub REST API, GitHub Actions, pinned Alibaba OpenCodeReview Action and CLI.

**Spec:** [Open Code Review setup v1 specification](spec.md). Read the [historical handoff](../open-code-review-handoff.md) before extracting source. The new files named below belong in the private `lispz1610/open-code-review-setup` repository, except where a task explicitly names `github-repo-setup`.

**Execution units:** The [sequential task list](tasks.md) splits the six plan phases into ten medium-sized deliveries with explicit acceptance criteria.

## Global Constraints

- Keep all code, documentation, tests, comments, and generated files in English.
- Keep the setup project independent of `github-repo-setup` at runtime and in tests. Do not resurrect `scripts/run_setup.py`.
- The only installed target path is `.github/workflows/open-code-review.yml`; the review workflow is separate from CI.
- Default events are `opened`, `synchronize`, `reopened`; accept an owner-selected nonempty unique subset of these events, skip fork PRs, and cancel the older run for the same PR.
- Use explicit `contents: read` and `pull-requests: write`; do not approve, merge, or create a required check.
- The owner configures `OCR_LLM_AUTH_TOKEN`, `OCR_LLM_URL`, `OCR_LLM_MODEL`, and `OCR_LLM_USE_ANTHROPIC`; the setup CLI never receives the provider key.
- Do not broaden Actions permissions or allowed Actions automatically. Fail closed when the effective policy cannot be proved to permit the pinned Action and dependencies.
- Keep the historical Action SHA `182898cf522da3d04157b422752d028417974e19` and CLI `1.12.12` as initial candidates; verify them and their dependencies before the release, and record any deliberate replacement.
- Dry run makes no GitHub writes. Apply stages a draft PR. Changed managed files need the current blob SHA. An old marker is migratable only with that SHA. Unmanaged files are rejected.
- A successful v1 requires both isolated tests and a live disposable-repository run with an authorized provider key; record failures as failures rather than inferred successes.

## File Map

| New-project path | Responsibility |
| --- | --- |
| `pyproject.toml` | Python floor, console entry point, package metadata, isolated test command. |
| `src/open_code_review_setup/policy.py` | Strict JSON contract and typed policy objects. |
| `src/open_code_review_setup/workflow.py` | Fixed-path workflow renderer, ownership markers, pinned references. |
| `src/open_code_review_setup/github.py` | Small REST client, response validation, GitHub object access. |
| `src/open_code_review_setup/preflight.py` | Read-only Actions, provider metadata, retention, and trusted rule checks. |
| `src/open_code_review_setup/staging.py` | Snapshot, drift checks, draft-PR staging, readback, idempotence. |
| `src/open_code_review_setup/cli.py` | `--repo`, `--policy`, `--apply`, safe structured output and errors. |
| `examples/open-code-review-policy.json` | Credential-free policy showing both protocols through documented alternatives. |
| `README.md`, `docs/operator-guide.md`, `SECURITY.md` | Installation and owner setup, advisory contract, recovery, release and security contact. |
| `tests/__init__.py`, `fake_github.py`, `test_policy.py`, `test_workflow.py`, `test_preflight.py`, `test_staging.py`, `test_cli.py` | Owned isolated tests and fake REST API with no fixtures from the source repository. |
| `docs/live-validation-YYYY-MM-DD.md` | Sanitized real acceptance evidence. |

## Review Focus

- A repository default branch advances between preflight and apply: `test_apply_rejects_advanced_default_head` in Task 4 must prove no workflow write occurs.
- A previous attempt leaves a setup branch with unrelated changes: `test_rejects_unrelated_existing_branch_diff` in Task 4 must reject it without overwriting.
- An inherited or selected Actions restriction cannot be fully inspected: `test_unknown_effective_actions_policy_fails_closed` in Task 3 must give a diagnostic and make no write.
- A legacy workflow has a matching old marker but no current SHA acknowledgement: `test_legacy_marker_requires_expected_sha` in Task 4 must reject migration.
- A configured rule path points at PR-controlled or non-object JSON: `test_rule_path_reads_default_branch_object_only` in Task 3 must reject it.

---

### Task 1: Independent package and policy validation

**Files:** Create `pyproject.toml`, `src/open_code_review_setup/__init__.py`, `src/open_code_review_setup/policy.py`, `tests/__init__.py`, `tests/test_policy.py`, and the credential-free `examples/open-code-review-policy.json`.

**Interfaces:** Produce immutable `WorkflowPolicy`, `LlmPolicy`, `LimitsPolicy`, and `ReviewPolicy` dataclasses; `validate_policy(data: object) -> ReviewPolicy`; `load_policy(path: Path) -> ReviewPolicy`. Later tasks consume these objects without importing the old repository. Preserve the historical JSON names and reject unknown keys. Fix `filename` to `open-code-review.yml`; the example contains all three events while validation accepts a nonempty unique subset. Support `expected_sha` and `rule_path` as optional fields.

- [ ] Write `test_accepts_openai_and_anthropic_policy`, `test_rejects_unknown_key_or_filename_or_events`, `test_rejects_credential_bearing_url`, and `test_rejects_invalid_sha_budget_timeout_or_rule_path`. Assert the fixed secret name, lowercase protocol variable mapping, HTTPS URL shape, positive limits, and 40-character SHA rules from the spec.
- [ ] Run `python3 -m unittest tests.test_policy -v` in a clean checkout; confirm the new tests fail for missing behavior.
- [ ] Implement the immutable policy types, strict parser, and example without credential values. Use Python 3.11+ and only the standard library at runtime.
- [ ] Rerun `python3 -m unittest tests.test_policy -v`; require all tests to pass. Inspect imports for old-repository modules and scan examples and fixtures for credential values. The legacy ownership marker is allowed as a literal migration input.
- [ ] Commit this independently testable package contract.

### Task 2: Render the dedicated trusted workflow

**Files:** Create `src/open_code_review_setup/workflow.py`, `tests/test_workflow.py`.

**Interfaces:** Produce `WORKFLOW_PATH: str = ".github/workflows/open-code-review.yml"`, `NEW_MARKER: str`, `LEGACY_MARKER: str`, and `render_workflow(policy: ReviewPolicy) -> bytes`. The output is deterministic UTF-8 bytes for one file. Task 4 uses the markers and output bytes to decide ownership and drift.

- [ ] Write `test_renders_three_events_and_same_pr_cancellation`, `test_renders_explicit_permissions_and_fork_skip`, `test_renders_pinned_action_and_provider_inputs`, and `test_uses_only_trusted_rule_path`. Assert the exact event list, fixed path, SHA reference, timeout, `llm_extra_body: "{}"`, artifact suppression, and absence of PR-head checkout or shell execution of PR code.
- [ ] Run `python3 -m unittest tests.test_workflow -v`; confirm failures.
- [ ] Render a versioned new-project ownership marker and the constrained workflow, quoting caller-controlled scalar values safely. Keep the upstream Action's trusted-base checkout contract explicit in comments or docs.
- [ ] Rerun the focused tests and compare the rendered Action inputs against the selected upstream `action.yml` at the pinned SHA. Record its nested `uses` dependencies for Task 3.
- [ ] Commit the renderer and its contract tests.

### Task 3: Owned GitHub client and read-only preflight

**Files:** Create `src/open_code_review_setup/github.py`, `src/open_code_review_setup/preflight.py`, `tests/fake_github.py`, `tests/test_preflight.py`.

**Interfaces:** `GitHubApi(token: str).request(method: str, path: str, body: object | None = None) -> tuple[int, object]`; `preflight(api: GitHubApi, repository: str, policy: ReviewPolicy) -> PreflightSnapshot`. `PreflightSnapshot` includes `default_branch: str`, `default_head_sha: str`, and nonsecret observations needed for the plan. Task 4 consumes this snapshot. Use a fake API owned by these tests and reusable in later test files.

- [ ] Write tests for Actions disabled, `local_only`, allowed `all`, selected policy with a demonstrably permitted pinned Action and dependencies, and `test_unknown_effective_actions_policy_fails_closed`. Assert diagnostics name the blocked setting and required owner action; assert the fake API receives only `GET` requests.
- [ ] Write tests for absent secret metadata, mismatched variables, retention above the policy maximum, missing default branch, and `test_rule_path_reads_default_branch_object_only`. Test both OpenAI-compatible and Anthropic variable values without using a real credential.
- [ ] Run `python3 -m unittest tests.test_preflight -v`; confirm failures.
- [ ] Implement the REST client and preflight. Read repository settings and default-head snapshot once, check exact variable values, inspect the accessible Actions allow policy and recorded Action dependencies, and fail closed where effective restrictions cannot be proved. Never call permissions-setting endpoints.
- [ ] Rerun focused tests. Verify against current [GitHub Actions permissions API](https://docs.github.com/en/rest/actions/permissions) documentation that the exact read endpoints and required setup-token scopes in the operator guide match the implementation.
- [ ] Commit the read-only preflight.

### Task 4: Guarded draft-PR staging and migration

**Files:** Create `src/open_code_review_setup/staging.py`, `tests/test_staging.py`.

**Interfaces:** `prepare_install(api: GitHubApi, repository: str, policy: ReviewPolicy) -> PreparedInstall` calls preflight and render, captures the default head and current file blob, and computes the selected branch and planned change. `apply_install(api: GitHubApi, prepared: PreparedInstall) -> InstallResult` is the sole write path. Results include `planned`, `pending_review`, or `verified` status, selected branch, change description, and draft PR URL when present; they contain no token or key.

- [ ] Write `test_dry_run_has_no_write`, `test_apply_stages_only_fixed_workflow_and_verifies_draft_pr`, and `test_repeat_on_open_or_merged_workflow_has_no_write`. Assert all write methods and target paths, branch diff, staged bytes, PR `draft: true`, and idempotent repeat.
- [ ] Write `test_managed_update_requires_current_sha`, `test_legacy_marker_requires_expected_sha`, `test_unmanaged_file_is_rejected`, `test_apply_rejects_advanced_default_head`, `test_rejects_unrelated_existing_branch_diff`, and `test_partial_write_reports_recovery_branch`. Verify the old marker is eligible only at the fixed path and with its current SHA.
- [ ] Run `python3 -m unittest tests.test_staging -v`; confirm failures.
- [ ] Implement deterministic branch naming, selected-path checks, optimistic head recheck, staged-content readback, pinned Action reference check, and draft PR readback. Fail on any unverified state, report partial writes, and never delete a setup branch automatically.
- [ ] Rerun focused tests. Review every REST write call and prove its file path or Git ref is derived from the fixed workflow and validated repository.
- [ ] Commit the staging path.

### Task 5: CLI and operator documentation

**Files:** Create `src/open_code_review_setup/cli.py`, `src/open_code_review_setup/__main__.py`, `tests/test_cli.py`, `README.md`, `docs/operator-guide.md`, and `SECURITY.md`; update the package entry point in `pyproject.toml`.

**Interfaces:** `main(argv: Sequence[str] | None = None) -> int` parses `--repo OWNER/REPO --policy PATH [--apply]`, reads setup `GITHUB_TOKEN`, calls `prepare_install`, optionally `apply_install`, and prints safe JSON. Console command and `python -m open_code_review_setup` must behave alike. No provider key argument exists.

- [ ] Write `test_cli_dry_run_never_writes`, `test_cli_apply_returns_draft_pr_url`, and `test_cli_error_redacts_tokens_and_provider_values`; run `python3 -m unittest tests.test_cli -v` and confirm failures.
- [ ] Implement the CLI with structured stdout, actionable stderr, and stable nonzero errors. Document manual owner setup of Actions, the secret, variables, permitted Action dependencies, workflow review and merge, retention, and quota/budget monitoring. Explain OpenAI-compatible and Anthropic examples, the advisory nature of comments, logging exposure, partial-write recovery, and old-marker migration. State `lispz1610` maintenance, Python 3.11+, private no-public-license status, security contact, and tagged release gates.
- [ ] Rerun `python3 -m unittest discover -s tests -p 'test_*.py' -v` from a checkout with no source-repository path on `PYTHONPATH`; require all tests to pass. Run the installed CLI help and an offline fake-API smoke check. Confirm examples and evidence contain no credential values.
- [ ] Commit the runnable CLI and operator guide.

### Task 6: Live acceptance and source-repository handoff

**Files:** Create `docs/live-validation-YYYY-MM-DD.md` in the new project. After the new project's gates pass, modify `github-repo-setup/README.md` and `github-repo-setup/docs/open-code-review-handoff.md` to link to the private destination, preserving the historical record.

**Interfaces:** The live report records target repository visibility, sanitized run and PR URLs, Action/CLI pins, policy settings without credentials, observed review comments, failures, and the final repeat result. It distinguishes fixture evidence from live behavior and identifies any unverified case.

- [ ] Create a disposable repository under an account authorized to spend its Actions and provider quota; configure the provider secret and three variables manually. Record that Actions and the pinned dependencies are allowed. Do not commit the key.
- [ ] Run the CLI without `--apply` and verify no remote writes; then apply and inspect the draft PR's single-file diff, permissions, fixed Action pin, and fork guard. Merge only after owner review.
- [ ] Open a same-repository test PR containing a deliberate issue whose diagnosis needs an unchanged file. Verify a posted review cites that file and the relevant fact absent from the PR diff. Push a new commit and reopen the PR to observe the selected triggers and same-PR cancellation; test a fork PR and verify no provider-backed review runs for it.
- [ ] Repeat setup after merge and verify no new branch, file write, or draft PR. Record actual runtime, Actions minutes, provider token use or quota limits, and sanitized links. Treat 429s, policy restrictions, missing approvals, and other unavailable live cases as unverified until rerun.
- [ ] Run the new project's isolated suite and CLI smoke check once more. Tag a v1 release only after every required isolated and live gate passes. Then update the source repository's links and run `python3 -m unittest discover -s tests -p 'test_*.py' -q` there; require no OpenCodeReview code import and no regression.
- [ ] Commit the acceptance evidence and source-repository documentation in their respective repositories. Publish or merge only through the owner's normal review process.

## Self-review and completion gate

Before execution, check each requirement in `spec.md` against the six tasks. Verify that all five Review Focus tests are present, that Task 3's `PreflightSnapshot` is the value Task 4 consumes, and that no task assumes a hosted GitHub App or automatic permission change. The work is complete only after the new private repository exists, its independent CLI and tests pass, the live advisory review is observed, and the source repository links to it.
