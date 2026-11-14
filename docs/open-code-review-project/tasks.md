# Open Code Review Setup v1: Sequential Task List

> [!NOTE] Historical Document
> This document is preserved as historical task tracking for the initial independent release. The review integration has since been merged into `github-repo-setup` under the [Integrated Review Setup specification](../integrated-review-setup-spec.md).

**Destination:** private `lispz1610/open-code-review-setup`. **Execution order:** T01 through T10, one task at a time. This list divides the [implementation plan](implementation-plan.md) into medium-sized deliverables; the [specification](spec.md) and [handoff](../open-code-review-handoff.md) define the product contract. All new-project paths below are relative to the destination repository unless a task explicitly names `github-repo-setup`.

**Owner decision on 2026-10-09:** T09 was accepted for the implementation sequence after a real advisory review used unchanged project context, repeat setup made no writes, and same-PR cancellation was observed. Successful `opened` and `synchronize` reviews, provider timeout tuning, and a live fork exercise remain deferred and unverified. T10 may produce a limited private v1 release with these gaps visible; this exception supersedes the stricter live-case completion gates below for that release only.

## Instructions for each task executor

Read this document, the spec, and only the plan section and historical source needed for the current task. Keep changes inside the named files unless a test exposes a necessary adjacent fix. Do not start the next task until the current acceptance criteria pass and the current task has a commit. At handoff, report the commit, commands and results, and any unresolved evidence. Never report a live behavior as verified from an isolated test. Use Python 3.11+, standard-library runtime code, and English for all files. Preserve the BYOK and advisory boundaries. The setup token and provider key must never appear in source, fixtures, logs, or task reports.

| Task | Deliverable | Depends on | Plan phase |
| --- | --- | --- | --- |
| T01 | Private package and strict policy contract | None | 1 |
| T02 | Deterministic trusted workflow renderer | T01 | 2 |
| T03 | Owned GitHub REST client and fake API | T02 | 3 |
| T04 | Read-only repository preflight | T02, T03 | 3 |
| T05 | Installation plan and drift detection | T04 | 4 |
| T06 | Guarded apply and draft PR | T05 | 4 |
| T07 | CLI and operator guide | T06 | 5 |
| T08 | Disposable target and workflow installation | T07 | 6 |
| T09 | Live PR behavior and repeat evidence | T08 | 6 |
| T10 | Release and source-repository handoff | T09 | 6 |

### Task 1: T01 — Create the package and validate policy v1

**Scope:** Create the private destination repository and `pyproject.toml`, `src/open_code_review_setup/__init__.py`, `src/open_code_review_setup/policy.py`, `tests/__init__.py`, `tests/test_policy.py`, and `examples/open-code-review-policy.json`. Define immutable `WorkflowPolicy`, `LlmPolicy`, `LimitsPolicy`, and `ReviewPolicy`; expose `validate_policy(data: object) -> ReviewPolicy` and `load_policy(path: Path) -> ReviewPolicy`. Keep historical JSON field names, fixed filename `open-code-review.yml`, default example events `opened`, `synchronize`, `reopened`, and an owner-selected nonempty unique subset of those events. Reject extra keys, unsafe paths or URL components, invalid SHAs, invalid budgets, and secret names other than `OCR_LLM_AUTH_TOKEN`.

**Acceptance:**

- The new GitHub repository is private and a fresh checkout runs `python3 -m unittest tests.test_policy -v` successfully with no source-repository path on `PYTHONPATH`.
- Tests accept both OpenAI-compatible and Anthropic configurations and reject each invalid class above. The example contains no credential value.
- Runtime and tests import nothing from `github-repo-setup`. Record the commit and exact test result.

### Task 2: T02 — Render one safe review workflow

**Scope:** Create `src/open_code_review_setup/workflow.py` and `tests/test_workflow.py`. Expose `WORKFLOW_PATH = ".github/workflows/open-code-review.yml"`, `NEW_MARKER`, `LEGACY_MARKER`, and `render_workflow(policy: ReviewPolicy) -> bytes`. Render a fixed-path, deterministic workflow with `pull_request_target`, selected events, same-PR cancellation, fork skip, explicit `contents: read` and `pull-requests: write`, timeout, pinned Action and CLI, BYOK inputs, `llm_extra_body: "{}"`, and no raw artifact upload. Recheck the candidate Action SHA `182898cf522da3d04157b422752d028417974e19`, CLI `1.12.12`, and nested `uses` dependencies against upstream; document the observed pins for T04.

**Acceptance:**

- `python3 -m unittest tests.test_workflow -v` passes, including default and subset events, deterministic bytes, quoted caller-controlled values, provider wiring, and the fixed path.
- The generated workflow references only the trusted default-branch code for its privileged execution; it adds no PR-head checkout or command that executes PR code. The upstream trusted-base assumption and dependency pins are recorded.
- A second render of the same policy is byte-for-byte identical. Record the commit and test result.

### Task 3: T03 — Own the GitHub API boundary

**Scope:** Create `src/open_code_review_setup/github.py`, `tests/fake_github.py`, and `tests/test_github.py`. Expose `GitHubApi(token: str).request(method: str, path: str, body: object | None = None) -> tuple[int, object]` and a typed API error. Handle GitHub API version and headers, path encoding, malformed responses, non-success statuses, and redacted errors. Keep a reusable in-memory fake with a request log for later tasks.

**Acceptance:**

- `python3 -m unittest tests.test_github -v` passes for GET and write responses, malformed JSON, API error propagation, path encoding, and token redaction.
- The client has no dependency on scripts or fixtures from `github-repo-setup`; the fake can assert the exact method, path, and body of every request.
- No test makes a live network request. Record the commit and test result.

### Task 4: T04 — Check target prerequisites without changing settings

**Scope:** Create `src/open_code_review_setup/preflight.py` and `tests/test_preflight.py`. Expose `preflight(api: GitHubApi, repository: str, policy: ReviewPolicy) -> PreflightSnapshot`, including default branch and head SHA. Check Actions enabled, effective permission for the pinned Action and T02 dependencies, log/artifact retention, secret metadata, exact three variable values, and optional `rule_path` as a JSON object on the default branch. For inherited or selected policies, stop with a specific diagnostic if permission cannot be proved. Never enable Actions or broaden policy.

**Acceptance:**

- `python3 -m unittest tests.test_preflight -v` passes for allowed and blocked policies, unknown effective policy, missing secret, variable mismatch, retention excess, absent default branch, and trusted-rule failures.
- Every preflight test verifies that its fake API saw only GET requests; no provider key value is fetched or printed.
- The returned snapshot binds the checks to a specific default-branch head SHA. Record the commit and test result.

### Task 5: T05 — Plan installation and detect drift

**Scope:** Create `src/open_code_review_setup/staging.py` and the first part of `tests/test_staging.py`. Expose `prepare_install(api: GitHubApi, repository: str, policy: ReviewPolicy) -> PreparedInstall`. Call T04 preflight and T02 render; read the fixed target path and any deterministic setup branch or open PR. Produce a stable, nonsecret plan without writes. Accept a changed new-marker or old-marker workflow only when `expected_sha` matches its current blob. Reject unmanaged files, missing acknowledged files, and unrelated existing branch changes.

**Acceptance:**

- Focused staging tests pass for absent file, identical file, managed update, legacy migration, missing or wrong `expected_sha`, unmanaged file, and unrelated branch diff.
- Every planning case asserts zero POST, PUT, PATCH, and DELETE requests. The planned change names only `.github/workflows/open-code-review.yml`.
- The resulting `PreparedInstall` carries the preflight default head and expected blob SHA for T06. Record the commit and test result.

### Task 6: T06 — Apply through a verified draft PR

**Scope:** Extend `staging.py` and `test_staging.py` with `apply_install(api: GitHubApi, prepared: PreparedInstall) -> InstallResult`. Recheck the default head, verify the pinned Action reference, create or reuse the deterministic setup branch, write only the selected workflow, inspect the selected diff and staged blob, create or reuse a **draft** PR, and read it back. Report the branch and recovery steps after partial writes. Never delete or silently overwrite an existing branch.

**Acceptance:**

- `python3 -m unittest tests.test_staging -v` passes for first apply, repeated apply with open PR, repeat after merge, branch or blob drift, advanced default head, unrelated branch changes, and partial-write recovery.
- Tests assert exact write paths, only one selected workflow diff, `draft: true`, final blob bytes, PR URL, and zero writes when the plan is unchanged or preflight fails.
- Results and errors contain no setup token or provider key. Record the commit and test result.

### Task 7: T07 — Expose the CLI and document owner setup

**Scope:** Create `src/open_code_review_setup/cli.py`, `src/open_code_review_setup/__main__.py`, `tests/test_cli.py`, `README.md`, `docs/operator-guide.md`, and `SECURITY.md`; add the console entry point to `pyproject.toml`. Implement `main(argv: Sequence[str] | None = None) -> int` for `--repo OWNER/REPO --policy PATH [--apply]` using setup `GITHUB_TOKEN`. Document manual configuration of `OCR_LLM_AUTH_TOKEN` and the three repository variables, Actions policy, exact token permissions, dry run, draft PR review and merge, BYOK billing, advisory comments, drift migration, partial-write recovery, and release/security ownership.

**Acceptance:**

- `python3 -m unittest discover -s tests -p 'test_*.py' -v` passes from a clean checkout without the source repository on `PYTHONPATH`.
- The installed console command and `python -m open_code_review_setup` show help, return safe structured results for dry run and apply with the fake API, and redact secrets in errors.
- README and operator guide name every manual GitHub setting, distinguish Actions minutes from provider API charges, and explain that Copilot Student model limits do not select the BYOK model. Record the commit and test result.

### Task 8: T08 — Install the workflow in a disposable repository

**Scope:** Start `docs/live-validation-YYYY-MM-DD.md` in the new project. Use a disposable target repository, authorized provider API key, and known Actions allowance. Configure the target secret and variables; run dry run, apply, inspect the one-file draft PR, and merge it through owner review. Record the installed workflow and its settings. Leave the behavioral PR cases to T09.

**Acceptance:**

- The report links the disposable target and draft PR, shows the pre-apply dry run made no writes, and records owner inspection of a diff containing only `.github/workflows/open-code-review.yml`.
- The merged file matches the rendered bytes, including pin, permissions, fork guard, selected events, and BYOK inputs. Actions policy and the provider secret/variables are confirmed without recording secret values.
- The report marks review behavior as pending T09 rather than claiming a successful review. Record the commit, setup commands, and sanitized results.

### Task 9: T09 — Verify live PR behavior and repeat safety

**Scope:** Extend `docs/live-validation-YYYY-MM-DD.md`. Open a same-repository test PR whose expected finding depends on an unchanged project file. Exercise a new commit, reopening, overlapping runs for cancellation, and a fork PR. Repeat the setup CLI after the workflow is merged.

**Acceptance:**

- The report links sanitized target PR and run evidence showing a posted advisory review that cites a fact from the unchanged file, absent from the PR diff. It records the selected Action/CLI pins and actual runtime or quota measurements.
- Open, synchronize, and reopen runs are observed; an overlapping older run is canceled; the fork PR produces no provider-backed review. Repeated setup makes no new branch, file write, or PR.
- Any blocked, rate-limited, or unavailable live case is explicitly marked unverified and rerun before v1 is declared ready. Credentials and raw provider output are absent from the report. Record the commit and test result.

### Task 10: T10 — Release and update the source handoff

**Scope:** After T09 passes, run the new project's full isolated suite and CLI smoke check again, tag a v1 release, and update `github-repo-setup/README.md` and `github-repo-setup/docs/open-code-review-handoff.md` with the new private repository location and access note. Preserve the historical source snapshot; do not reintroduce review code into the source repository.

**Acceptance:**

- The new project's clean-checkout tests and CLI smoke check pass, T09 evidence covers every required live case, and the release tag identifies the verified commit.
- The source repository links to the new project, labels it private, and `python3 -m unittest discover -s tests -p 'test_*.py' -q` passes there with no import from the review project.
- Record both repository commits, the release tag, exact verification commands, and any remaining limitation. The task is incomplete if the live review or repeat-idempotence evidence is missing.
