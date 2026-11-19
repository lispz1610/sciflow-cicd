# Versioned workflow installation
 
`scripts/install_github_workflows.py` stages explicitly selected GitHub Actions workflows in a draft pull request. Through Interactive Setup or Policy version 2, the optional reviewed Matt Pocock skill bundle can join those files in the same draft Setup Pull Request. The `sciflow-cicd` Policy `files` stage also joins selected caller-authored files to that Setup Pull Request in either Policy version. It does not merge them or enable unselected workflows. All selected files are unified in a single draft Setup Pull Request on an owned branch (`codex/workflows-*`).

```bash
python3 scripts/install_github_workflows.py --repo OWNER/REPO --policy examples/workflows-policy.json
python3 scripts/install_github_workflows.py --repo OWNER/REPO --policy examples/workflows-policy.json --apply
```

The workflows policy has `version: 1` and a nonempty `workflows` array (or appears under the unified `workflows` stage in Policy version 2). Each entry selects `template`, `template_version: 1`, a simple `.yml` or `.yaml` `filename`, `triggers`, a pinned 40-character `checkout_sha`, and optional `runs_on` runner labels.

All configurable workflow templates (`pr-ci`, `profile-ci`, `dependency-review`, and `container-publish`) and the **Review Setup** workflow (`.github/workflows/open-code-review.yml`) default `runs_on` to `"self-hosted"` (accepting either a runner label string or a nonempty list of label strings). Maintainers can choose to keep the self-hosted default to conserve GitHub Actions minutes quota, or select a **Hosted Runner** (`ubuntu-24.04`).

To guarantee security and pipeline stability, auxiliary control jobs (`detect-changes` and `status-check` in `profile-ci`) and fixed trusted governance workflows (`issue-state-gate*` and `documentation-freshness`) remain strictly pinned to `ubuntu-24.04` (hosted runner isolation). This hosted isolation ensures that fork pull requests never execute unvetted control logic on self-hosted machines and required **Stable Status Checks** never deadlock when self-hosted runner pools are offline or saturated.

When a configurable workflow targets a `self-hosted` runner with `pull_request` triggers in a public repository, `sciflow-cicd` injects a **Fork Execution Guard** (`if: github.event.pull_request.head.repo.full_name == github.repository`) by default on workload jobs and emits a Dry-Run advisory notice recommending GitHub's *"Require approval for all outside collaborators"* policy (`Settings -> Actions -> General`).

The unified `workflows` Policy stage replaces the deprecated `ci-profile` stage. Legacy option aliases (`options`, `pip_cache`, `git_lfs`) have been removed; policies configure options directly within each workflow object using canonical names (`cache`, `lfs`, etc.).

Supported configurable workflow templates and options include:
- **`pr-ci`**: Single unmatrixed job running caller-supplied `commands`. Configurable `runs_on`, `timeout_minutes`, `container`, `env`, `working_directory`, and optional `push_branches` / `schedule`.
- **`profile-ci`**: Scientific CI profiles (`python` and `cmake-mpi`) with matrix strategies, package manager or compiler caching, PR concurrency controls (`cancel-in-progress: true`), and stable status check reporting.
  - **`python` profile**: Supports `python_setup_mode` (`"setup-python"`, `"uv"`, or `"system"`). In `"system"` mode, the workflow relies on runner-provisioned Python without downloading or installing via `actions/setup-python`. Supports explicit `install_commands`, automatic dependency manifest detection (`requirements.txt`/`pyproject.toml`) defaulting `cache: false` when no manifest is present to prevent setup-python failures, and push-only `post_merge_commands` executed on `push` to default branch and excluded from `status-check`.
  - **`cmake-mpi` profile**: Supports `install_system_deps` (default `false` on `self-hosted` to prevent failing `sudo apt-get` commands on unprivileged runner environments, `true` on `ubuntu-24.04`), `compilers` matrix (`["gcc"]` or `["gcc", "clang"]`), `omp_threads` (integer or list of integers, default `2`), `ccache` compiler caching with `actions/cache` pinned commit SHA (`cache_sha`), push-only `post_merge_commands` and `extended_ranks` validation excluded from `status-check`, and explicit runner evidence demarcation notices.
- **`dependency-review`**: Pinned 40-character `action_sha` for `actions/dependency-review-action` with configurable `fail_on_severity` (`low`, `moderate`, `high`, `critical`), `allow_licenses`, `deny_licenses`, and `comment_summary_in_pr` (`always`, `never`, `on-failure`, or boolean). Default `runs_on: self-hosted`.
- **`container-publish` (or `container`)**: Opt-in container workflow for container compilation and registry publishing. Configurable `triggers` (`pull_request`, `push`, `workflow_dispatch`, `schedule`), `registry` (default `ghcr.io`), `dockerfile` (default `Dockerfile`), `context` (default `.`), `tags`, and pinned Action SHAs (`actions/checkout`, `docker/login-action`, and `docker/build-push-action`). On pull request runs, container publishing is disabled (`if: github.event_name != 'pull_request'` on login, and `push: ${{ github.event_name != 'pull_request' }}`), preventing registry pollution and fork permission failures. Default `runs_on: self-hosted`.
- **Interactive Setup**: Provides a global runner prompt proposing `self-hosted` by default with support for custom labels or switching to `ubuntu-24.04`, seamless Dependabot prompt integration, and Resume State inspection of all managed workflow files (`ci.yml`, `dependency-review.yml`, `container-publish.yml`, `open-code-review.yml`, `dependabot.yml`).

None of these templates use `pull_request_target` or repository secrets. Available triggers are `pull_request`, `push`, `workflow_dispatch`, and `schedule`; `pull_request` is required for PR-gated templates.

For scientific software releases, citation metadata (`CITATION.cff`), five-state DOI lifecycle governance, and Software Bill of Materials (SPDX 2.3 `sbom.spdx.json`) generation, see the [Release Candidate verification & promotion guide](release-evidence.md) and the [DOI issuance and deposit guidance](doi-guidance.md). Container publication remains completely decoupled from release qualification evidence: releasing software does not mandate container publishing, and container workflows do not substitute for release evidence.

## Scientific CI profiles and evidence boundaries

Scientific repositories require focused CI profiles that handle multi-version Python matrices, compiled CMake/MPI toolchains, and bounded smoke tests without overloading hosted virtual runners or hanging status checks. `sciflow-cicd` supports two scientific CI profiles under the `profile-ci` template:

1. **Scientific Python profile (`python`)**:
   - **Active workflow**: The Scientific Python profile is deployed in `.github/workflows/ci.yml` running unit tests across Python 3.10, 3.11, and 3.12 with an unmatrixed `status-check` anchor.
   - **Version matrix**: Multi-version testing (e.g. `["3.11", "3.12"]`) with `fail-fast: false` for thorough cross-version diagnostic reporting.
   - **Dependency caching**: Native wheel caching via `actions/setup-python` (`cache: "pip"`) or high-speed caching via `astral-sh/setup-uv` (`package_manager: "uv"`).
   - **Git LFS**: Optional LFS asset retrieval (`lfs: true`) in `actions/checkout` for reference test data fixtures.
   - **Path filtering**: Optional path patterns (e.g. `["**/*.py", "pyproject.toml"]`) to skip CI on documentation-only pull requests.

2. **CMake and MPI profile (`cmake-mpi`)**:
   - **Build & test tooling**: Automated CMake configure/build steps or custom `build_commands` / `test_commands`.
   - **Compiler caching**: Configures `ccache` in runner path and environment (`CCACHE_DIR`), avoiding redundant recompilation across PR pushes.
   - **Runner dependencies**: System apt packages (e.g. `libopenmpi-dev`, `openmpi-bin`).
   - **Bounded rank counts**: Smoke checks sized for virtual 2-vCPU runners (default `ranks: [1, 2]`), verifying MPI communicators and message exchange without thread oversubscription or artificial timeouts.
   - **OpenCL CPU emulation**: Optional POCL packages (`pocl-opencl-icd`, `ocl-icd-opencl-dev`, `opencl-headers`, `clinfo`) for host kernel compilation and CPU smoke testing.

### Stable status check anchor

Matrixed and path-filtered CI workflows often cause required checks in branch rulesets to hang indefinitely when jobs are skipped. All `profile-ci` workflows generate a terminal unmatrixed job named `status-check` with `if: always()`. It evaluates the `needs` context: if any upstream job failed or was cancelled, it fails; if all jobs succeeded or were skipped by path filters, it passes. Configure `status-check` as the required check in branch rulesets to ensure reliable gating.

### Rigorous evidence boundaries and external delegation

Passing tests on GitHub Actions hosted runners (`ubuntu-24.04`) verifies host compilation and software CPU execution only. It does **not** establish correctness on physical GPU hardware, accelerators, or multi-node cluster interconnects. Demarcate claims into **Observed** (virtual CPU execution), **Inferred** (host algorithm validity), and **Unverified** (physical hardware accelerators and cluster scaling).

Long-running simulations, hyperparameter sweeps, and GPU benchmarks must be decoupled from PR CI and delegated to external compute environments (e.g., Google Colab with GPU runtimes, or HPC clusters). Maintainers record observed external benchmark data and tolerances in pull request descriptions or Release Candidate Control Records.

For complete configuration specifications, matrix sizing guidelines, and the Colab handoff protocol, see the [Scientific CI profiles guide](scientific-ci-profiles.md).

When Advisory Review is configured (via Interactive Setup or a Policy version 2 `review` stage), its workflow (`.github/workflows/open-code-review.yml`) and trusted reporter (`scripts/review_issue_reporter.js`) are staged in the same unified draft Setup Pull Request. Both review and report jobs default to `runs_on: self-hosted` (with option to target hosted `ubuntu-24.04`) to eliminate Actions runner minute consumption during frequent review passes. The review workflow uses a trusted-base `pull_request_target` model with explicit `contents: read` and `pull-requests: write` permissions, skips forks, requires same-repository PRs, and never executes PR code with provider secrets. Because `pull_request_target` runs the workflow definition from the default branch (`main`), edits to `open-code-review.yml` in a PR take effect only after merging to `main`. Token budgets can be tuned (e.g. `max_tokens_budget: 14000`) and noisy lockfiles pruned via `.gitattributes` to accommodate compact context windows, with automatic long-context fallback via `OCR_LLM_FALLBACK_MODEL`. See the [Advisory Review operator guide](advisory-review.md).

When the Policy version 2 `agent-skills` stage selects `mattpocock`, the installer copies the verified repository snapshot to `.agents/skills/` and includes its generated source manifest in that Setup Pull Request. It accepts identical files, updates prior generated files only when their bytes match the previous manifest, and blocks the whole staging bundle if it finds diverged skill content. Dry-Run reports the upstream commit, version, file count, and destination.

The installer reads the default branch and all selected files before any write. It refuses an existing workflow file without
its management marker. A changed managed file requires its current GitHub blob SHA in `expected_sha`; this is an explicit
drift acknowledgement. The dry run reports paths and hashes, not command bodies. Apply creates a deterministic
`codex/workflows-*` branch, commits only selected workflow files there, checks that its diff contains no unselected files,
reads selected files back, and opens a draft PR. A second apply while the PR is open makes no writes; after merge, the default
branch matches and the Dry-Run result is empty. If a staged branch was edited unexpectedly or the default branch advanced
during preflight, apply stops. A failed write can leave a partial branch; rerun after resolving the error.

Set `GITHUB_TOKEN` in the environment. The token needs Contents write, Pull requests write, and Workflows write permissions for workflow file changes, plus the ability to read the target repository and refs. Never place the token in a policy or command argument. GitHub Actions must be enabled and the organization must permit the pinned actions. The command does not guess or install a project-specific command, runner matrix, dependency manifest, or security entitlement.

The generated YAML is parsed in the local test suite when PyYAML is installed. Before writing, the installer checks that every selected pinned Action commit exists in its upstream repository; review the source at that commit as well. The example checkout commit is [actions/checkout 4.2.2](https://github.com/actions/checkout/commit/11bd71901bbe5b1630ceea73d27597364c9af683). The optional dependency review Action requires a supported dependency graph and may be unavailable for the selected repository.

When the `dependabot` stage installs `.github/dependabot.yml`, Dependabot immediately scans existing dependency manifests and pinned workflow Action SHAs and opens up to `open_pull_requests_limit` (default `5`) concurrent pull requests per ecosystem to clear the initial backlog. Dependabot runs strictly on its configured `cadence` (`monthly` by default) and respects that cap of `5` concurrently open PRs—it does not open a new Dependabot PR every time a maintainer opens a regular PR. Because `documentation-freshness` runs on every PR that modifies `.github/workflows/**`, each initial Dependabot workflow bump PR also opens one managed Documentation Freshness issue until closed.

## Issue triage workflow

The `Issue triage` workflow adds `needs-triage` to a newly opened issue unless it already has one of the canonical triage labels. When the issue author comments on an issue labeled `needs-info`, the workflow removes `needs-info` and adds `needs-triage`, preserving all other labels. Comments from other users do not change triage labels. The label job has `contents: read` and `issues: write`; it uses the repository `GITHUB_TOKEN`. A separate project job runs only for newly opened issues when the repository Actions variable `TRIAGE_PROJECT_ID` contains the board's GraphQL node ID. With the variable unset, board placement is skipped.

To enable board placement, also create the repository Actions secret `TRIAGE_PROJECT_TOKEN` with access to that project. GitHub's repository-scoped `GITHUB_TOKEN` cannot access Projects V2. A classic personal access token with `project` and `repo` scopes is one supported option; the owner must authorize it for the target project and organization. Store the token only as an Actions secret. The project job uses that secret only for the GraphQL mutation and requests `contents: read` from its own workflow token for checkout. See [GitHub's Projects automation guidance](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions) for credential options.

If board placement fails, confirm that both `TRIAGE_PROJECT_ID` and `TRIAGE_PROJECT_TOKEN` are configured and that the token can edit the target project. Label triage is a separate job and can still succeed. To disable placement, remove the variable. The workflow does not retroactively relabel or move old issues; maintainers can rerun it for a specific issue event or apply the intended labels and project placement manually.

For the other continuous documentation checks, see the [automation guide](agents/automation.md). The Markdown link audit checks relative targets and Markdown anchors on PRs that change Markdown and on pushes to `main`. The PR audit job has only `contents: read`; a separate main-push-only managed-link reconciliation job receives `issues: write`. Reconciliation is a no-op unless `.github/managed-links.json` exists; configure that file with real repository issue or pull request identities before enabling it. The checked-in `examples/links.json` contains placeholders and is not used by the workflow. The workflow security audit runs on PRs changing `.github/workflows/**` and rejects non-local Actions that are not pinned to full commit SHAs.

The Documentation Freshness Audit and Advisory Review reporter may both report on one pull request, but maintain separate managed issues, PR link comments, and status contexts. Documentation reporting runs from trusted-base workflow code, reads same-repository PR Markdown as data, and skips fork PRs before its issue-writing job. Its reporter needs `actions: read`, `contents: read`, `issues: write`, `pull-requests: read`, and `statuses: write`; its issue refresh job needs the permissions listed in the [Documentation Freshness guide](documentation-freshness.md). Advisory Review's isolated reporting job needs `contents: read`, `issues: write`, and `pull-requests: read` and has no provider secret. Missing Issues enablement or effective write permission is a reporter failure. Inspect the failed run, restore the repository/workflow permission, then rerun it; do not manually claim that the report is clean. Follow each guide's own clean, closure, and reopen procedure.

## Live acceptance test

Use a disposable repository with Actions enabled and the required token permissions. Replace the example command with a harmless test command available on the runner. Run dry run, apply, and repeat apply; review and merge the draft PR. Open one PR with a command that exits zero and inspect its Actions run and check conclusion. Then deliberately change the test input in another PR so the same command exits nonzero; inspect the failed run and confirm the PR is not falsely reported as passing. Record the PR and run URLs, check names, token permissions, repository visibility, and whether a fork was used. Fork behavior is covered by local tests only.
