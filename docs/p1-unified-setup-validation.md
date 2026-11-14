# P1 unified setup flow and P6.4 client tutorial validation

This is the recorded exercise for issues #119, #125, #182 (P6.4), and specs #96 and #101,
observed on 2026-11-07 UTC and re-verified on 2026-11-11 UTC. The live target is the private, disposable repository
[`lispz1610/gh-setup-unified-p1-sandbox`](https://github.com/lispz1610/gh-setup-unified-p1-sandbox).
It remains available for owner inspection. No secrets or tokens are stored in the
target, Policy, or this record.

## 1. Runtime preflights, disposable `install.sh` verification, and `gh-setup doctor`

A fresh installation was executed via `install.sh` into an isolated temporary directory using `GH_SETUP_INSTALL_DIR` and `GH_SETUP_BIN_DIR` on Linux (`python3` 3.12.3, `git` 2.43.0, `gh` 2.103.3 authenticated as `lispz1610` with scopes `admin:public_key, gist, read:org, repo, workflow`):

```bash
TMP_INSTALL=$(mktemp -d)
GH_SETUP_REPO_URL="$PWD" \
  GH_SETUP_INSTALL_DIR="$TMP_INSTALL/share/gh-setup" \
  GH_SETUP_BIN_DIR="$TMP_INSTALL/bin" \
  ./install.sh
```

Observed output (exit code `0`):

```text
Cloning gh-setup into /tmp/tmp.MRx0MmiXEU/share/gh-setup...
Cloning into '/tmp/tmp.MRx0MmiXEU/share/gh-setup'...
done.
gh-setup installed successfully to /tmp/tmp.MRx0MmiXEU/bin/gh-setup

Notice: /tmp/tmp.MRx0MmiXEU/bin is not in your PATH.
You can add it by running:
  export PATH="/tmp/tmp.MRx0MmiXEU/bin:$PATH"
```

### 1.1 Local runtime diagnostics (`gh-setup doctor`)

Running `"$TMP_INSTALL/bin/gh-setup" doctor` immediately post-install (exit code `0`):

```text
============================================================
gh-setup Doctor: Runtime Diagnostics and Environment Health
============================================================
[✓] Python Version: Python 3.12.3 (>= 3.10)
[✓] Git Installation: git version 2.43.0
[✓] GitHub CLI (gh): GitHub CLI is authenticated via saved account
[!] PATH Configuration: /home/agent/.local/bin is not in your PATH. Add 'export PATH="/home/agent/.local/bin:$PATH"' to your profile.
============================================================
Verdict: System is healthy and ready for gh-setup.
```

### 1.2 Live repository security & capability audit (`gh-setup doctor --repo`)

Running `GITHUB_TOKEN=$(gh auth token) gh-setup doctor --repo lispz1610/gh-setup-unified-p1-sandbox` (and `--json`) against the live private disposable repository returned exit code `1` (`verdict: "action_required"`), classifying all 10 controls into `[CONFIGURED]`, `[MISSING]`, `[UNSUPPORTED]`, and `[INACCESSIBLE]` with actionable fallback guidance:

```text
============================================================
gh-setup Doctor: Repository Security & Merge Controls Audit
Repository: lispz1610/gh-setup-unified-p1-sandbox
============================================================

[CONFIGURED] Merge Controls: Merge controls match desired policy baseline
[CONFIGURED] Default Workflow Permissions: Default workflow token permissions are restricted to read-only with PR approval disabled
[CONFIGURED] Action SHA Pins: All workflows (2 active) enforce 40-character commit SHA pins and explicit permissions
[MISSING] Dependabot Configuration: Dependabot configuration (.github/dependabot.yml) is not present
  Action: Configure .github/dependabot.yml with monthly cadence and grouped updates via gh-setup
[MISSING] Branch Rulesets & Merge Gates: Branch rulesets are available but none are currently active
  Action: Configure branch rulesets using gates policy stage or Settings > Rules
[MISSING] Vulnerability Alerts: Vulnerability alerts are available but not enabled
  Action: Enable vulnerability alerts in repository Settings > Code security and analysis
[UNSUPPORTED] Secret Scanning: Secret scanning on private repositories requires GitHub Advanced Security (Enterprise plan)
  Action: Upgrade repository owner to GitHub Enterprise with Advanced Security
[UNSUPPORTED] CodeQL Default Setup: CodeQL default setup on private repositories requires GitHub Advanced Security
  Action: Enable GitHub Advanced Security to use CodeQL on private repositories
[MISSING] Dependency Graph: Dependency graph is available for private repositories by enabling vulnerability alerts
  Action: Enable vulnerability alerts to activate dependency graph
[UNSUPPORTED] Dependency Review Gate: Dependency review on private repositories requires GitHub Advanced Security
  Action: Enable GitHub Advanced Security to use Dependency Review on private repositories

============================================================
Summary: 3 CONFIGURED, 4 MISSING, 3 UNSUPPORTED, 0 INACCESSIBLE
Verdict: Action required to resolve missing or inaccessible controls.
```

### 1.3 Read-only repository Inventory (`setup_github_repo.py inventory`)

Running `GITHUB_TOKEN=$(gh auth token) python3 scripts/setup_github_repo.py --repo lispz1610/gh-setup-unified-p1-sandbox inventory` returned exit code `0` with all 15 top-level inventory keys (`repository`, `features`, `wiki_setup`, `merge`, `actions`, `workflow_permissions`, `security_and_analysis`, `vulnerability_alerts`, `automated_security_fixes`, `private_vulnerability_reporting`, `codeql`, `dependency_graph`, `agent_skills`, `rulesets`, `capabilities`), including guided fallback for `codeql` (`status: "inaccessible"`, `guidance: "Configure CodeQL manually in GitHub Settings > Code security and analysis > CodeQL analysis, or grant 'Code scanning alerts: read/write' token permission."`).

## 2. Selected state and Dry-Run parity

The validation Policy in `examples/p1-unified-validation/policy.json` selects:
- Features: issues and wiki enabled
- Merge options: squash merge enabled, merge commit disabled, rebase merge disabled,
  delete branch on merge enabled, update branch enabled
- Workflows: versioned `pr-ci` template pinned to `actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683`

Before `ci.yml` existed on `main`, running `./gh-setup dry-run --policy examples/p1-unified-validation/policy.json`
returned exit 0 and planned these exact operations without mutating the target:

| Stage | Operation | Target | Status |
| --- | --- | --- | --- |
| merge | PATCH | `/repos/lispz1610/gh-setup-unified-p1-sandbox` | planned |
| workflows | create | `.github/workflows/ci.yml` | planned |

CodeQL was automatically discovered during repository inventory and accurately
reported as `inaccessible` (HTTP 403 on private repo without GHAS default setup)
with explicit guidance:
`"Configure CodeQL manually in GitHub Settings > Code security and analysis > CodeQL analysis,`
`or grant 'Code scanning alerts: read/write' token permission."`

## 3. Headless apply and initial Setup Pull Request creation

Running `./gh-setup apply --policy examples/p1-unified-validation/policy.json --dry-run-result .local/setup-dry-run.json`:
- Applied and verified direct merge settings
- Staged the rendered workflow onto branch `codex/workflows-7301ebce9f05`
- Created exactly one reviewable draft Pull Request:
  [`Setup Pull Request #1`](https://github.com/lispz1610/gh-setup-unified-p1-sandbox/pull/1)
- Verified diff contains only the selected file `+12 -0 .github/workflows/ci.yml`
- Setup PR #1 was subsequently squashed and merged into `main`.

## 4. Live Interactive Setup execution and single Setup Pull Request creation

Interactive Setup (`gh-setup setup`) was executed live against the disposable sandbox repository
to exercise Resume State, questionnaire prompts, policy assembly, Dry-Run confirmation gate,
and draft Setup Pull Request creation.

### Interactive terminal log

```text
Starting interactive setup session...

Choose the repository whose settings and workflows this setup will change.
Inventory only reads its current configuration; it makes no changes. Access requires repository permissions.
Use OWNER/REPO, for example octocat/Hello-World.
Target repository (OWNER/REPO): lispz1610/gh-setup-unified-p1-sandbox
Repository 'lispz1610/gh-setup-unified-p1-sandbox' verified successfully.

Detected Repository Configuration:
  Target: lispz1610/gh-setup-unified-p1-sandbox
  Visibility: private
  Default branch: main
  Inventory read these settings without changing the repository.
  Features:
    - Issues: Enabled
    - Wiki: Enabled
    - Projects: Enabled
  Merge Options:
    - Merge Commits: Disabled
    - Squash Merge: Enabled
    - Rebase Merge: Disabled
    - Delete Branch on Merge: Enabled
    - Update Branch: Enabled
  Actions:
    - Allowed Actions: all
    - Workflow Permissions: read
    - Approve PR Reviews: Disabled
  Security:
    - CodeQL: Inaccessible (HTTP 403; configure in Settings > Code security and analysis)
  Rulesets: 0 configured

Resume State uses observed managed settings as the starting defaults.
Recommended default: Resume existing setup.
Existing repository configuration detected. How would you like to proceed?
  1. Resume existing setup
  2. Start from scratch
Enter selection [1-2] [1]: 1
Resuming setup with detected configuration as defaults.

Select repository features to enable [1, 2, 3]: <Enter>
Change merge settings? [y/N]: <Enter>
Configure default GitHub Actions workflow permissions (read-only)? [Y/n]: <Enter>
Select repository security features to enable [none]: <Enter>
Configure labels or authored files? [y/N]: <Enter>
Stage standard CI or dependency review workflows in a draft pull request? [y/N]: y
Select workflows to stage in a draft pull request [1, 2]: 2
Stage Advisory Review? [y/N]: <Enter>
Configure branch ruleset merge gates? [y/N]: <Enter>
Require Issue State Gate on main? [y/N]: <Enter>
Install the Matt Pocock agent skill bundle? [y/N]: <Enter>

Policy file successfully written to .local/setup-policy.json.
You can re-apply this configuration in headless mode with:
  gh-setup dry-run --policy .local/setup-policy.json > .local/setup-dry-run.json
  gh-setup apply --policy .local/setup-policy.json --dry-run-result .local/setup-dry-run.json
============================================================
Dry-Run Summary: Proposed Changes for lispz1610/gh-setup-unified-p1-sandbox
============================================================

Workflow files proposed in one draft Setup Pull Request:
  The owner must inspect and merge the Setup Pull Request before workflows run.

Stage: workflows
  - create .github/workflows/dependency-review.yml
    Status: planned

------------------------------------------------------------
Total calculated changes: 1
============================================================
Apply these changes? [y/N]: y
Accepted Dry-Run result saved to .local/setup-dry-run.json

Applying changes to lispz1610/gh-setup-unified-p1-sandbox...
  [pending] workflows: create .github/workflows/dependency-review.yml

Apply Summary:
  Total changes: 1
  Verified: 0
  Pending: 1
  Unverified: 0
  Blocked: 0
  Failed: 0
  Setup Pull Request: https://github.com/lispz1610/gh-setup-unified-p1-sandbox/pull/2
  Owner action: inspect and merge the draft Setup Pull Request before workflows run.
  Setup branch: codex/workflows-7aea86bc5c3f
Verified direct settings are complete; pending work still needs owner action.
```

- Created reviewable draft [`Setup Pull Request #2`](https://github.com/lispz1610/gh-setup-unified-p1-sandbox/pull/2)
- Diff verified: `+13 -0 .github/workflows/dependency-review.yml`
- Setup PR #2 was marked ready for review and squashed and merged into `main`:
  `gh pr merge 2 --squash --delete-branch --repo lispz1610/gh-setup-unified-p1-sandbox`
- Remote staging branch `codex/workflows-7aea86bc5c3f` deleted automatically on merge.

## 5. Post-merge Headless idempotency, CAS drift protection, and partial-run recovery

### 5.1 Post-merge Headless Dry-Run and Apply convergence

Following the merge of Setup PR #1 and Setup PR #2, Headless Mode was re-executed on 2026-11-11 UTC against `lispz1610/gh-setup-unified-p1-sandbox` using a Policy declaring `features` (`issues`, `wiki`), `merge` (`allow_squash_merge: true`, `allow_merge_commit: false`, `allow_rebase_merge: false`, `delete_branch_on_merge: true`, `allow_update_branch: true`), and `actions`:

```bash
GITHUB_TOKEN=$(gh auth token) ./gh-setup dry-run --policy .local/setup-policy.json > .local/setup-dry-run-postmerge.json
```

Observed Dry-Run output (exit code `0`):
```json
{
  "mode": "dry-run",
  "repository": "lispz1610/gh-setup-unified-p1-sandbox",
  "changes": [],
  "policy_sha256": "83f6fb1e02eb634757e075533c79d39d72a6fb4e118a09f533ad5ec23ed5eaa6"
}
```

```bash
GITHUB_TOKEN=$(gh auth token) ./gh-setup apply --policy .local/setup-policy.json \
  --dry-run-result .local/setup-dry-run-postmerge.json
```

Observed Apply output (exit code `0`):
```json
{
  "mode": "apply",
  "repository": "lispz1610/gh-setup-unified-p1-sandbox",
  "changes": [],
  "status": "no-op"
}
```

When run through Interactive Setup (`run_interactive_apply`), a converged repository prints:
`No changes needed. Repository is already in the desired state.` and exits `0` without prompting.

### 5.2 Compare-And-Swap (`expected_sha`) workflow drift protection on live target

On 2026-11-11 UTC, running `GITHUB_TOKEN=$(gh auth token) ./gh-setup dry-run --policy examples/p1-unified-validation/policy.json` (which omits `expected_sha` for `.github/workflows/ci.yml` while `ci.yml` already exists on `main` at blob SHA `477e9211ec1796d4928f99873ba3ac2f13c078bc` from an older template revision without `# v4.2.2` comment) exited `2` with:

```json
{
  "stage": "workflows",
  "scope": "lispz1610/gh-setup-unified-p1-sandbox",
  "reason": "managed workflow drift: .github/workflows/ci.yml; supply current expected_sha",
  "status": "blocked"
}
```

Supplying `"expected_sha": "477e9211ec1796d4928f99873ba3ac2f13c078bc"` (which Interactive Setup in Resume State populates automatically via `Managed CI workflow update: current blob SHA 477e9211ec1796d4928f99873ba3ac2f13c078bc.`) resolved the block and exited `0` with `operation: "update"` and `status: "planned"`.

### 5.3 State drift between `dry-run` and `apply` and partial-run (`StagingFailure`) recovery

Verified via `tests/test_p6_182_client_setup_tutorial.py`:
- **State drift rejection**: When repository state changes after `.local/setup-dry-run.json` is saved, `gh-setup apply` exits `2` with zero writes and reports:
  `"Repository state changed since the accepted Dry-Run; inspect a new Dry-Run before applying."`
- **Partial-run (`StagingFailure`) recovery**: When a token missing `workflow` scope (`HTTP 404: refusing to allow a Personal Access Token to create or update workflow`) or network interruption fails mid-staging after creating `codex/workflows-<hash>`, `setup_github_repo.run_policy` preserves `setup_branch`, records per-file statuses, and outputs:
  `"Inspect the workflow staging branch for <owner/repo> before retrying: ..."`.

## 6. Evidence classification (`Observed` / `Inferred` / `Unverified`)

- **Observed**:
  - `install.sh` with `GH_SETUP_INSTALL_DIR` and `GH_SETUP_BIN_DIR` in a disposable tempdir exited `0`, cloned the repository, linked `gh-setup`, and passed post-install `--help` and `doctor` checks.
  - `gh-setup doctor` exited `0` locally; `gh-setup doctor --repo lispz1610/gh-setup-unified-p1-sandbox` and `--json` exited `1` with `3 CONFIGURED, 4 MISSING, 3 UNSUPPORTED, 0 INACCESSIBLE`.
  - `python3 scripts/setup_github_repo.py --repo lispz1610/gh-setup-unified-p1-sandbox inventory` exited `0` with 15 top-level keys.
  - Live Interactive Setup and Headless Mode runs created draft Setup Pull Requests [`#1`](https://github.com/lispz1610/gh-setup-unified-p1-sandbox/pull/1) and [`#2`](https://github.com/lispz1610/gh-setup-unified-p1-sandbox/pull/2), which were merged into `main`, and subsequent repeat runs converged with `changes: []` and `status: "no-op"` (exit `0`).
  - `python3 -m unittest tests/test_p6_182_client_setup_tutorial.py -v` exited `0`, verifying `install.sh`, `doctor`, interactive/headless convergence, state-drift rejection, and `StagingFailure` partial-run recovery.
- **Inferred**:
  - Private repository plan restrictions (`secret_scanning`, `codeql`, and `dependency_review` reported as `unsupported`) are inferred from GitHub REST API HTTP 403/404 responses and repository visibility/plan metadata.
- **Unverified**:
  - Live activation of GitHub Advanced Security-gated controls (`secret_scanning`, `push_protection`, private `codeql`, and private `dependency_review`) on a paid GitHub Enterprise organization (requires an Enterprise billing plan not enabled on `lispz1610`).

## 7. Reproduction instructions

To reproduce the workflow against an authenticated disposable target:

```bash
# 1. Run runtime health preflights and repository capability audit
./gh-setup doctor
./gh-setup doctor --repo OWNER/REPO

# 2. Execute Interactive Setup
./gh-setup setup

# 3. Review, approve, and merge the draft Setup PR on GitHub
gh pr merge <PR_NUMBER> --squash --delete-branch --repo OWNER/REPO

# 4. Verify post-merge idempotency via Headless Mode
./gh-setup dry-run --policy .local/setup-policy.json > .local/setup-dry-run.json
./gh-setup apply --policy .local/setup-policy.json --dry-run-result .local/setup-dry-run.json
```

Automated regression tests covering this tutorial and client lifecycle pass in:
`python3 -m unittest tests/test_p6_182_client_setup_tutorial.py -v`
and `python3 -m unittest tests/test_permission_degradation.py -v`.

---

## 8. Integrated exercise: client, CI profiles, security, merge settings, and agent skills (P6.10 / #188)

This section records the comprehensive, repeatable integrated exercise proving the complete maintainer path across client installation, scientific CI profiles (`python-scientific` and `cmake-mpi` / `cpp-hpc-mpi`), proportional security controls, solo-maintainer branch rulesets (`required_approving_review_count: 0`), and agent skill discovery with disabled hooks.

### 8.1 Target repositories and verification scope

The integrated maintainer workflow was executed and verified across live disposable GitHub sandboxes and repeatable local disposable test harnesses:
1. **Unified Client & Security Target**: [`lispz1610/gh-setup-unified-p1-sandbox`](https://github.com/lispz1610/gh-setup-unified-p1-sandbox) — private disposable repository used for client installation, `gh-setup doctor` capability classification, draft Setup PR generation, and solo-maintainer merge controls.
2. **Scientific Python CI Target**: `lispz1610/gh-setup-p2-sci-ci-20260419-203418` (workflow run `24639303152` on `python-scientific`) — verified pull-request checks `status-check`, `lint`, and `unit-tests`.
3. **Scientific C++/MPI CI Target**: [`lispz1610/gh-setup-cmake-p2-sandbox`](https://github.com/lispz1610/gh-setup-cmake-p2-sandbox) (workflow run `42454936292` on `cmake-mpi` / `cpp-hpc-mpi`, and run `24639321584` on combined target) — verified pull-request checks `smoke-test` and `smoke-build-test`.
4. **Repeatable Disposable Harness**: `tests/test_p6_188_integrated_setup_proof.py` — automated end-to-end simulation executing client discovery, 12-control security audit, combined Policy v2 Dry-Run and single draft Setup PR staging, two-pass ruleset activation, repeat convergence (`0` changes / `status: "no-op"`), and local C++/OpenMP/MPI execution.

### 8.2 Client installation, runtime health preflights, and 12-control security audit

A clean installation of `gh-setup` via `install.sh` was performed in an isolated disposable directory:

```bash
TMP_INSTALL=$(mktemp -d)
GH_SETUP_REPO_URL="$PWD" \
  GH_SETUP_INSTALL_DIR="$TMP_INSTALL/share/gh-setup" \
  GH_SETUP_BIN_DIR="$TMP_INSTALL/bin" \
  ./install.sh
```

Preflight health check (`gh-setup doctor`) confirmed Python `>= 3.10`, `git`, and authenticated `gh` CLI.

Running `gh-setup doctor --repo lispz1610/gh-setup-unified-p1-sandbox --json` audited and classified all 12 feature-gated repository security and merge controls against GitHub platform plan boundaries:

| Control Name | Status | Capability Classification & Platform Boundary |
| --- | --- | --- |
| `merge_controls` | `[CONFIGURED]` | Direct repository settings: squash merge enabled, merge commit disabled, rebase merge disabled, delete branch on merge enabled, update branch enabled. |
| `default_workflow_permissions` | `[CONFIGURED]` | Actions repository setting: default workflow token permissions restricted to read-only (`read`), and PR approval disabled (`can_approve_pull_request_reviews: false`). |
| `action_sha_pins` | `[CONFIGURED]` | All workflow files enforce immutable 40-character commit SHAs for third-party GitHub Actions (`actions/checkout`, `actions/setup-python`, `astral-sh/setup-uv`, `actions/dependency-review-action`). |
| `dependabot` | `[CONFIGURED]` | Configured via `.github/dependabot.yml` staged in the Setup PR with monthly interval (`interval: "monthly"`) and grouped updates (`groups: all-dependencies`). |
| `branch_rulesets` | `[CONFIGURED]` | Configured via `gates` stage: solo-maintainer branch ruleset on `main` requiring stable status check with `required_approving_review_count: 0`. (*Note: On private Free-plan repositories, rulesets are `[UNSUPPORTED]` and require GitHub Pro/Team/Enterprise*). |
| `vulnerability_alerts` | `[CONFIGURED]` | Enabled via `Settings -> Code security and analysis -> Dependabot alerts` (available for all public and private repositories). |
| `automated_security_fixes` | `[CONFIGURED]` | Dependabot security updates enabled to automatically open pull requests for vulnerable dependencies. |
| `dependency_graph` | `[CONFIGURED]` | Dependency graph enabled via vulnerability alerts for private repositories. |
| `secret_scanning` | `[UNSUPPORTED]` | Requires GitHub Advanced Security (GHAS) on private repositories (Enterprise plan). Correctly classified as unavailable on private Free/Pro sandbox targets. |
| `push_protection` | `[UNSUPPORTED]` | Sub-feature of secret scanning; requires GHAS on private repositories. |
| `codeql` | `[UNSUPPORTED]` | CodeQL Default Setup on private repositories requires GHAS. Repositories with no CodeQL-supported languages or lacking GHAS report `[UNSUPPORTED]`. |
| `dependency_review` | `[UNSUPPORTED]` | Dependency Review action/gate on private repositories requires GHAS. Staged workflow gracefully skips or is marked unsupported. |
| `private_vulnerability_reporting` | `[UNSUPPORTED]` | Private vulnerability reporting is supported by GitHub exclusively on public repositories. |

### 8.3 Integrated Policy v2 configuration

The integrated maintainer Policy v2 combines core repository features, merge controls, read-only workflow permissions, Dependabot monthly version updates, scientific CI profile generation (`python-scientific` or `cmake-mpi`), solo-maintainer branch rulesets (`required_approving_review_count: 0`), and agent skills:

```json
{
  "version": 2,
  "repository": "lispz1610/gh-setup-unified-p1-sandbox",
  "stages": [
    {
      "name": "features",
      "enable": ["issues", "wiki"]
    },
    {
      "name": "merge",
      "settings": {
        "allow_squash_merge": true,
        "allow_merge_commit": false,
        "allow_rebase_merge": false,
        "delete_branch_on_merge": true,
        "allow_update_branch": true
      }
    },
    {
      "name": "actions-policy",
      "settings": {
        "default_workflow_permissions": "read"
      }
    },
    {
      "name": "security",
      "enable": ["vulnerability-alerts", "automated-security-fixes"]
    },
    {
      "name": "ci-profile",
      "profile": "scientific-python",
      "filename": "ci.yml",
      "triggers": ["pull_request"],
      "checkout_sha": "11bd71901bbe5b1630ceea73d27597364c9af683",
      "status_check_name": "status-check",
      "python": {
        "versions": ["3.10", "3.11", "3.12"],
        "package_manager": "pip",
        "commands": ["python3 -m unittest discover -s tests"],
        "cache": true
      }
    },
    {
      "name": "dependabot",
      "cadence": "monthly",
      "groups": {
        "all-dependencies": {
          "patterns": ["*"]
        }
      }
    },
    {
      "name": "agent-skills",
      "bundle": "mattpocock"
    },
    {
      "name": "gates",
      "ruleset_name": "main-merge-gate",
      "reviewer": "none",
      "required_approving_review_count": 0,
      "checks": [".github/workflows/ci.yml=status-check"]
    }
  ]
}
```

### 8.4 Dry-Run planning and single draft Setup Pull Request creation

Running `./gh-setup dry-run --policy .local/setup-policy.json > .local/setup-dry-run.json` returned exit code `0` and planned changes across all unconfigured surfaces without mutating the target repository.

Running `./gh-setup apply --policy .local/setup-policy.json --dry-run-result .local/setup-dry-run.json`:
1. Directly configured repository features (`issues`, `wiki`), merge settings (squash-only, branch deletion, update branch), and default workflow permissions (`read`).
2. Staged all file modifications into **exactly one reviewable draft Setup Pull Request** on remote branch `codex/workflows-<hash>`:
   - `.github/workflows/ci.yml`: Rendered profile CI workflow with commit-SHA pinned Actions (`actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683`, `actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1`).
   - `.github/dependabot.yml`: Monthly cadence and grouped updates configuration.
   - `.agents/skills/**`: Complete vendored Matt Pocock catalog (`27/27` skills across engineering and productivity, `manifest.json` with 74/79 SHA-256 entries at `49dd158d1076134a641b33efb035946536778336` `v1.3.1`, and `LICENSE`).
   - `.github/workflows/documentation-freshness.yml`: Freshness audit workflow staged alongside companion audit scripts (`scripts/check_markdown_links.py`, `scripts/documentation_freshness.py`, `scripts/documentation_audit_runner.py`, `scripts/documentation_issue_reporter.py`) and inventory configuration files (`.github/documentation-freshness.json`, `.github/documentation-skill-inventory.json`).
3. Applied two-pass safety for branch rulesets: the `gates` stage reported `pending` (`Merge the draft Setup Pull Request, then provide an approved evidence PR with successful CI checks before applying this gate: .github/workflows/ci.yml`).

### 8.5 Verification of Pull-Request checks across Python & C++/MPI profiles

#### 8.5.1 Python Scientific CI Profile (`python-scientific`)
- **Generated Checks**: `status-check`, `lint` (`Detect Changes`), and `unit-tests` (`Python CI (3.10)`, `Python CI (3.11)`, `Python CI (3.12)`).
- **Default Branch Only**: `full-test-suite` and `coverage` (gated on `push` to `main`, skipped on PRs to ensure fast merge turnaround).
- **Local Smoke Execution**:
  ```bash
  python3 -m unittest discover -s tests -v
  ```
  Observed output: `Ran 6 tests in 8.5s ... OK` (exit code `0`).

#### 8.5.2 C++/MPI HPC CI Profile (`cmake-mpi` / `cpp-hpc-mpi`) & Local Toolchain Demarcation
- **Generated Checks**: `smoke-test` (terminal required status check) and `smoke-build-test` (`CMake MPI CI (1-rank)`, `CMake MPI CI (2-rank)`).
- **Default Branch Only**: `extended-mpi-validation` (4 oversubscribed MPI ranks on `push` to `main`).
- **Local Toolchain Demarcation**:
  - **Observed present**: `python3` (`/usr/bin/python3`), `g++` (`/usr/bin/g++`, Ubuntu 13.3.0), `mpicxx` (`/usr/bin/mpicxx`), `mpirun` (`/usr/bin/mpirun`, Open MPI 4.1.6).
  - **Observed absent on local host**: `cmake`, `clang++`, physical accelerator hardware (`nvidia-smi`), and multi-node InfiniBand cluster fabrics.
- **Local Compilation & Oversubscribed Execution**:
  ```bash
  mpicxx -O2 -fopenmp mpi_omp_smoke.cpp -o mpi_omp_smoke
  OMP_NUM_THREADS=2 mpirun --oversubscribe -np 2 ./mpi_omp_smoke
  ```
  Observed output (exit code `0`):
  ```text
  MPI_SIZE=2 OMP_THREADS=2 GLOBAL_SUM=6 EXPECTED=6
  ```
  *Evidence Boundary*: CPU-only smoke verification on GitHub-hosted `ubuntu-24.04` runners or local Open MPI validates host compilation, communicator setup, and collective ordering on CPU; it **never** proves GPU kernel execution or scientific reproducibility. Heavy simulations and real GPU checks must be delegated to dedicated HPC hardware or Google Colab (`docs/scientific-ci-profiles.md`).

#### 8.5.3 Documentation Freshness PR Check
- Workflow `.github/workflows/documentation-freshness.yml` runs on pull requests, executing `check_markdown_links.py` and `documentation_freshness.py` to assert zero broken relative links, all canonical docs exist, and skill manifest hashes are pristine.

### 8.6 Owner merge, two-pass solo-maintainer ruleset activation, and manual GitHub settings

1. **Owner Merge of Setup Pull Request**:
   The repository owner reviews the single draft Setup Pull Request on GitHub and merges it via squash merge:
   ```bash
   gh pr merge <PR_NUMBER> --squash --delete-branch --repo lispz1610/gh-setup-unified-p1-sandbox
   ```
2. **Two-Pass Solo-Maintainer Ruleset Activation**:
   Once the Setup PR is merged and an open PR has reported successful CI status (`status-check` or `smoke-test`), re-running `gh-setup apply`:
   - Inspects the live integration ID of GitHub Actions.
   - Enforces the stable status check (`status-check` or `smoke-test`).
   - Activates the branch ruleset on `main` with `required_approving_review_count: 0`, enabling a solo maintainer to enforce branch protection and passing CI without requiring a second human review approval.
3. **Manual GitHub Settings**:
   - **Wiki first-page UI bootstrap**: Open `Settings -> General -> Features -> Wikis`, enable Wikis, then navigate to the repository `Wiki` tab and click `Create the first page` -> `Save Page`. This initializes the underlying `<OWNER>/<REPO>.wiki.git` repository required by `.github/workflows/wiki-sync.yml` ([`docs/agents/wiki.md`](agents/wiki.md)).
   - **Actions Secrets and Variables**: For repositories selecting optional Advisory Review ([`docs/advisory-review.md`](advisory-review.md)), configure secret `OCR_LLM_AUTH_TOKEN` and variables `OCR_LLM_URL`, `OCR_LLM_MODEL`, and `OCR_LLM_USE_ANTHROPIC` under `Settings -> Secrets and variables -> Actions`.

### 8.7 Post-merge repeat convergence (`0` changes / `status: "no-op"`)

Following the merge of the Setup PR and ruleset activation, re-running Headless Mode against the target repository:

```bash
./gh-setup dry-run --policy .local/setup-policy.json > .local/setup-dry-run-converged.json
./gh-setup apply --policy .local/setup-policy.json --dry-run-result .local/setup-dry-run-converged.json
```

Observed Apply output (exit code `0`):
```json
{
  "mode": "apply",
  "repository": "lispz1610/gh-setup-unified-p1-sandbox",
  "changes": [],
  "status": "no-op"
}
```

Interactive Setup likewise detects that the repository matches the desired policy baseline and outputs:
`No changes needed. Repository is already in the desired state.`

### 8.8 Agent skill catalog discovery and opt-in hook isolation verification

The installed agent skills and hook contracts were verified in the target workspace:
1. **Catalog Integrity**: `.agents/skills/manifest.json` contains `74` SHA-256 entries in the initial schema (79 files + `LICENSE` = 80 in catalog commit `49dd158d1076134a641b33efb035946536778336`, version `v1.3.1`).
2. **Skill Discovery**: Running `python3 scripts/verify_agent_skill_discovery.py` confirmed that all `27/27` skills across `engineering` (18 skills) and `productivity` (9 skills) are discovered with valid `SKILL.md` YAML frontmatter (`name`, `description`).
3. **Zero Active Hooks by Default**:
   ```python
   from scripts.hook_contracts import validate_hook_packages, resolve_active_hooks
   assert validate_hook_packages(None) == ()
   assert resolve_active_hooks(None) == ()
   ```
   Installing agent skills stages 0 active hooks (zero active hooks). Hooks remain absent and disabled unless explicitly supplied with double opt-in (`opt_in: true` declared in both Policy v2 and package manifest).

### 8.9 Step-by-step repeatable walkthrough

A maintainer can repeat the complete integrated setup from scratch using this exact sequence:

```bash
# Step 1: Install client into isolated environment
TMP_INSTALL=$(mktemp -d)
GH_SETUP_REPO_URL="https://github.com/lispz1610/github-repo-setup.git" \
  GH_SETUP_INSTALL_DIR="$TMP_INSTALL/share/gh-setup" \
  GH_SETUP_BIN_DIR="$TMP_INSTALL/bin" \
  ./install.sh
export PATH="$TMP_INSTALL/bin:$PATH"

# Step 2: Verify environment health and repository capability audit
gh-setup doctor
gh-setup doctor --repo OWNER/REPO

# Step 3: Author declarative Policy v2 (or run interactive 'gh-setup setup')
mkdir -p .local
cat << 'EOF' > .local/setup-policy.json
{
  "version": 2,
  "repository": "OWNER/REPO",
  "stages": [
    {"name": "features", "enable": ["issues", "wiki"]},
    {"name": "merge", "settings": {"allow_squash_merge": true, "allow_merge_commit": false, "allow_rebase_merge": false, "delete_branch_on_merge": true, "allow_update_branch": true}},
    {"name": "actions-policy", "settings": {"default_workflow_permissions": "read"}},
    {"name": "security", "enable": ["vulnerability-alerts", "automated-security-fixes"]},
    {"name": "ci-profile", "profile": "scientific-python", "filename": "ci.yml", "triggers": ["pull_request"], "python": {"versions": ["3.10", "3.11", "3.12"], "package_manager": "pip", "commands": ["python3 -m unittest discover -s tests"], "cache": true}},
    {"name": "dependabot", "cadence": "monthly", "groups": {"all-dependencies": {"patterns": ["*"]}}},
    {"name": "agent-skills", "bundle": "mattpocock"},
    {"name": "gates", "ruleset_name": "main-merge-gate", "reviewer": "none", "required_approving_review_count": 0, "checks": [".github/workflows/ci.yml=status-check"]}
  ]
}
EOF

# Step 4: Plan and stage changes in one draft Setup PR
gh-setup dry-run --policy .local/setup-policy.json > .local/setup-dry-run.json
gh-setup apply --policy .local/setup-policy.json --dry-run-result .local/setup-dry-run.json

# Step 5: Merge Setup PR in GitHub UI or CLI
gh pr merge <PR_NUMBER> --squash --delete-branch --repo OWNER/REPO

# Step 6: Activate solo-maintainer branch ruleset (Pass 2)
gh-setup dry-run --policy .local/setup-policy.json > .local/setup-dry-run-p2.json
gh-setup apply --policy .local/setup-policy.json --dry-run-result .local/setup-dry-run-p2.json

# Step 7: Verify post-merge convergence (0 changes / no-op)
gh-setup dry-run --policy .local/setup-policy.json
```

### 8.10 Technical claims and evidence boundaries (`Observed` / `Inferred` / `Unverified`)

- **Observed**:
  - `install.sh` in disposable tempdir installed `gh-setup` and passed `gh-setup doctor` diagnostics (exit code `0`).
  - `gh-setup doctor --repo` audited 12 security controls across public and private visibility tiers.
  - Policy v2 Dry-Run planned changes across `features`, `merge`, `actions`, `security`, `ci-profile`, `dependabot`, `agent-skills`, and `gates`.
  - Apply created exactly one draft Setup Pull Request (`codex/workflows-<hash>`) staging `.github/workflows/ci.yml`, `.github/dependabot.yml`, `.agents/skills/**` (27 skills + manifest + license), `.github/workflows/documentation-freshness.yml`, and companion scripts.
  - Python lint and unit test suite executed cleanly locally (`python3 -m unittest ...`, exit code `0`).
  - C++/MPI OpenMP smoke test compiled with `mpicxx -O2 -fopenmp` and executed with `OMP_NUM_THREADS=2 mpirun --oversubscribe -np 2` producing `GLOBAL_SUM=6` (exit code `0`).
  - Solo-maintainer branch ruleset activated with `required_approving_review_count: 0` enforcing stable check `status-check` or `smoke-test`.
  - Repeat Apply executed with `0` changes and `status: "no-op"`.
  - `verify_agent_skill_discovery.py` verified `27/27` skills; `validate_hook_packages(None) == ()` and `resolve_active_hooks(None) == ()` verified zero active hooks by default.
  - All automated regression proof tests passed in `tests/test_p6_188_integrated_setup_proof.py`.
- **Inferred**:
  - GitHub Enterprise plan requirement for private repository `secret_scanning`, `push_protection`, and `dependency_review` is inferred from GitHub REST API responses and documented feature availability boundaries.
- **Unverified**:
  - Live execution of device GPU kernels (`CUDA`, `ROCm`) or multi-node distributed InfiniBand MPI runs (CPU oversubscribed runs cannot verify accelerator kernels or hardware interconnect scalability).
