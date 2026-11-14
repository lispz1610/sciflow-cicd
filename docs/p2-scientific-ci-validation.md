# P2 scientific CI profiles validation

This document records the end-to-end validation of the **Scientific CI Profiles** (`python-scientific` / `scientific-python` / `python` and `cmake-mpi` / `cpp-hpc-mpi`) for Spec #97, Issue #131, and P6.5 ([#183](https://github.com/lispz1610/github-repo-setup/issues/183)).

## Target repositories and verification scope

1. **Combined disposable scientific CI target**: `lispz1610/gh-setup-p2-sci-ci-20260419-203418` (`python-scientific` workflow run `24639303152` and `cmake-mpi` workflow run `24639321584`, plus local disposable verification in Section 3).
2. **Dedicated Python profile sandbox**: [`lispz1610/gh-setup-python-p2-sandbox`](https://github.com/lispz1610/gh-setup-python-p2-sandbox) (workflow run `42454914550`, observed on 2026-11-08 UTC).
3. **Dedicated CMake/MPI profile sandbox**: [`lispz1610/gh-setup-cmake-p2-sandbox`](https://github.com/lispz1610/gh-setup-cmake-p2-sandbox) (workflow run `42454936292`, observed on 2026-11-08 UTC).

No secrets, credentials, or tokens are stored in the targets, Policy files, or this record. Following the `Observed / Inferred / Unverified` evidence gate, **CPU-only smoke checks do not claim, prove, or verify real GPU execution, full multi-node HPC cluster scaling, or numerical/scientific reproducibility**.

### Recorded check names across profiles

| Profile (`profile` / `README.md` name) | Pull-Request Check Names (`pull_request`) | Default-Branch Push Check Names (`push` to `main`) | Terminal Required Check (`status_check_name`) |
| --- | --- | --- | --- |
| **`python-scientific`** (`profile: "python"` / `"scientific-python"`) | `Detect Changes` (`detect-changes`), `lint`, `unit-tests` (`Python CI (3.10)`, `Python CI (3.11)`, `Python CI (3.12)`), and terminal anchor `status-check` | `full-test-suite`, `coverage` (gated on `push` to `refs/heads/main`; skipped on PRs and excluded from `status-check` `needs:`) | `status-check` |
| **`cmake-mpi` / `cpp-hpc-mpi`** (`profile: "cmake-mpi"` / `"cmake"`) | `Detect Changes` (`detect-changes`), `smoke-build-test` (`CMake MPI CI (1-rank)`, `CMake MPI CI (2-rank)`), and terminal anchor `status-check` / `smoke-test` | `extended-mpi-validation` (4 oversubscribed MPI ranks on `push` to `refs/heads/main`; skipped on PRs and excluded from terminal PR check `needs:`) | `status-check` (default) or `smoke-test` |
| **Standard PR CI** (`template: "pr-ci"` / `ci`) | `PR CI` (`ci` / `test`) | None (`pull_request` only) | `ci` / `test` |

---

## 1. Scientific Python CI profile validation (`python-scientific` / `python`)

### Initial repository state

The target repository `lispz1610/gh-setup-python-p2-sandbox` (and `lispz1610/gh-setup-p2-sci-ci-20260419-203418`) was initialized with minimal valid scientific Python test infrastructure on `main` at commit `412bc3a`:
- `pyproject.toml`: Minimal build system and project definition
- `tests/test_math.py`: Unit test suite verifying basic mathematical operations
- `README.md`: Project description

### Policy configuration

The validation Policy in [`examples/p2-scientific-validation/python-policy.json`](../examples/p2-scientific-validation/python-policy.json) selects:
- Features: issues and wiki enabled
- Merge options: squash merge enabled, merge commit disabled, rebase merge disabled, delete branch on merge enabled, update branch enabled
- CI profile: `python` staged in `.github/workflows/ci.yml` on `pull_request`, pinned `actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683`, matrix versions `["3.11", "3.12"]`, `package_manager: "pip"`, `cache: true`, and test command `python3 -m unittest discover -s tests`.

```json
{
  "version": 2,
  "repository": "lispz1610/gh-setup-python-p2-sandbox",
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
      "name": "ci-profile",
      "profile": "python",
      "filename": "ci.yml",
      "triggers": ["pull_request"],
      "checkout_sha": "11bd71901bbe5b1630ceea73d27597364c9af683",
      "python": {
        "versions": ["3.11", "3.12"],
        "package_manager": "pip",
        "commands": ["python3 -m unittest discover -s tests"],
        "cache": true
      }
    }
  ]
}
```

### Dry-Run result

Executing `./gh-setup dry-run --policy examples/p2-scientific-validation/python-policy.json` returned exit 0 with planned mutations and zero writes:

```json
{
  "mode": "dry-run",
  "repository": "lispz1610/gh-setup-python-p2-sandbox",
  "changes": [
    {
      "stage": "merge",
      "operation": "PATCH",
      "path": "/repos/lispz1610/gh-setup-python-p2-sandbox",
      "status": "planned"
    },
    {
      "path": ".github/workflows/ci.yml",
      "stage": "ci-profile",
      "operation": "create",
      "status": "planned"
    }
  ],
  "policy_sha256": "ccf7929f60513eecf3007475f7e2ee300aa330c2f1e06926c6e640df0e3bdc74"
}
```

### Headless apply and Setup Pull Request staging

Applying the accepted dry-run result via `./gh-setup apply`:
- Applied direct repository merge settings
- Staged `.github/workflows/ci.yml` onto branch `codex/workflows-a0a50e831efa`
- Created reviewable draft [`Setup Pull Request #1`](https://github.com/lispz1610/gh-setup-python-p2-sandbox/pull/1)
- Verified diff contains only the single selected file: `+49 -0 .github/workflows/ci.yml`
- Setup PR #1 was marked ready for review and squashed and merged into `main` at commit `485bd42`. The setup branch `codex/workflows-a0a50e831efa` was automatically deleted.

### Candidate Pull Request and CI workflow execution

A candidate pull request [`Candidate Pull Request #2`](https://github.com/lispz1610/gh-setup-python-p2-sandbox/pull/2) was created from branch `candidate-python-test` (commit `c9532a6`) adding a 2x2 matrix determinant test to `tests/test_math.py`.

GitHub Actions workflow run [`42454914550`](https://github.com/lispz1610/gh-setup-python-p2-sandbox/actions/runs/42454914550) (and corresponding `python-scientific` run `24639303152` on `lispz1610/gh-setup-p2-sci-ci-20260419-203418` covering `lint`, `unit-tests`, `status-check`, and default-branch `full-test-suite` / `coverage`) was triggered:
- Matrix legs (`unit-tests` / `ci`):
  - `Python CI (3.11)` (Job ID 128651769225): Completed in 6s (success)
  - `Python CI (3.12)` (Job ID 128651769466): Completed in 5s (success)
- Dependency caching: `actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1` with `cache: "pip"` (`pip_cache: true`)
- Terminal **Stable Status Check**: `status-check` (Job ID 128651798342): Completed in 3s (success)
  - Evaluated context: `{"ci": {"result": "success", "outputs": {}}}`
  - Observed output:
    ```text
    Status check results - success: ['ci'], skipped: [], failed: []
    Status check passed.
    ```
- Final PR check status: `0 cancelled, 0 failing, 3 successful, 0 skipped, and 0 pending checks`.
- Candidate PR #2 merged cleanly into `main` at commit `b8bd8e9`.

### Post-merge Headless idempotency

Re-running Headless dry-run and apply with the Python Policy against `lispz1610/gh-setup-python-p2-sandbox`:
- Dry-Run output (exit code 0):
  ```json
  {
    "mode": "dry-run",
    "repository": "lispz1610/gh-setup-python-p2-sandbox",
    "changes": [],
    "status": "no-op",
    "policy_sha256": "ccf7929f60513eecf3007475f7e2ee300aa330c2f1e06926c6e640df0e3bdc74"
  }
  ```
- Apply output (exit code 0):
  ```json
  {
    "mode": "apply",
    "repository": "lispz1610/gh-setup-python-p2-sandbox",
    "changes": [],
    "status": "no-op",
    "setup_pull_request": null,
    "setup_branch": null
  }
  ```
- Result: Zero modifications, zero redundant branches, zero duplicate PRs.

---

## 2. Scientific CMake and MPI CI profile validation (`cmake-mpi` / `cpp-hpc-mpi`)

### Initial repository state

The target repository `lispz1610/gh-setup-cmake-p2-sandbox` (and `lispz1610/gh-setup-p2-sci-ci-20260419-203418`) was initialized with minimal valid C++ / MPI / CMake code on `main` at commit `af9ae5a`:
- `CMakeLists.txt`: Configures MPI target with CMake 3.20+, defines executables and CTest tests `mpi_smoke_1rank` and `mpi_smoke_2rank`
- `test.cpp`: Implements `MPI_Init`, communicator size/rank queries, and collective `MPI_Allreduce` summation verification
- `README.md`: Project description

### Policy configuration

The validation Policy in [`examples/p2-scientific-validation/cmake-mpi-policy.json`](../examples/p2-scientific-validation/cmake-mpi-policy.json) selects:
- Features: issues and wiki enabled
- Merge options: squash merge enabled, merge commit disabled, rebase merge disabled, delete branch on merge enabled, update branch enabled
- CI profile: `cmake-mpi` staged in `.github/workflows/ci.yml` on `pull_request`, pinned `actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683`, matrix ranks `[1, 2]`, runner dependencies `["libopenmpi-dev", "openmpi-bin"]`, `ccache: true`, `opencl: false`, build commands `["cmake -B build -S .", "cmake --build build"]`, test commands `["ctest --test-dir build --output-on-failure"]`.

```json
{
  "version": 2,
  "repository": "lispz1610/gh-setup-cmake-p2-sandbox",
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
      "name": "ci-profile",
      "profile": "cmake-mpi",
      "filename": "ci.yml",
      "triggers": ["pull_request"],
      "checkout_sha": "11bd71901bbe5b1630ceea73d27597364c9af683",
      "cmake_mpi": {
        "ranks": [1, 2],
        "build_commands": ["cmake -B build -S .", "cmake --build build"],
        "test_commands": ["ctest --test-dir build --output-on-failure"],
        "runner_dependencies": ["libopenmpi-dev", "openmpi-bin"],
        "ccache": true,
        "opencl": false,
        "manual_build": false
      }
    }
  ]
}
```

### Dry-Run result

Executing `./gh-setup dry-run --policy examples/p2-scientific-validation/cmake-mpi-policy.json` returned exit 0 with planned mutations and zero writes:

```json
{
  "mode": "dry-run",
  "repository": "lispz1610/gh-setup-cmake-p2-sandbox",
  "changes": [
    {
      "stage": "merge",
      "operation": "PATCH",
      "path": "/repos/lispz1610/gh-setup-cmake-p2-sandbox",
      "status": "planned"
    },
    {
      "path": ".github/workflows/ci.yml",
      "stage": "ci-profile",
      "operation": "create",
      "status": "planned"
    }
  ],
  "policy_sha256": "0866004ec54e1c01a28b58de0c4fd39b69122c577417b2a268d10d14a957f8c4"
}
```

### Headless apply and Setup Pull Request staging

Applying the accepted dry-run result via `./gh-setup apply`:
- Applied direct repository merge settings
- Staged `.github/workflows/ci.yml` onto branch `codex/workflows-d75b13cd7585`
- Created reviewable draft [`Setup Pull Request #1`](https://github.com/lispz1610/gh-setup-cmake-p2-sandbox/pull/1)
- Verified diff contains only the single selected file: `+64 -0 .github/workflows/ci.yml`
- Setup PR #1 was marked ready for review and squashed and merged into `main` at commit `a98b33b`. The setup branch `codex/workflows-d75b13cd7585` was automatically deleted.

### Candidate Pull Request and CI workflow execution

A candidate pull request [`Candidate Pull Request #2`](https://github.com/lispz1610/gh-setup-cmake-p2-sandbox/pull/2) was created from branch `candidate-cmake-test` (commit `a8f7863`) adding an `MPI_Bcast` collective validation step to `test.cpp`.

GitHub Actions workflow run [`42454936292`](https://github.com/lispz1610/gh-setup-cmake-p2-sandbox/actions/runs/42454936292) (and corresponding `cmake-mpi` run `24639321584` on `lispz1610/gh-setup-p2-sci-ci-20260419-203418` covering `smoke-build-test`, terminal `smoke-test` / `status-check`, and default-branch `extended-mpi-validation`) was triggered:
- Evidence demarcation annotation:
  ```text
  ::notice title=Device Boundary Demarcation::GitHub Actions executes on hosted CPU runner. Device/GPU kernels tested under CPU fallbacks or OpenCL POCL do not constitute real GPU validation.
  Observed: Host compilation and CPU execution on ubuntu-24.04 with 1 MPI rank(s).
  Unverified: Real GPU/accelerator hardware execution and multi-node interconnect scaling.
  ```
- Runner dependencies & ccache:
  - Installed `libopenmpi-dev` and `openmpi-bin`
  - Configured `ccache` at `/usr/lib/ccache` and `CCACHE_DIR`
- Matrix legs (`smoke-build-test` / `ci`):
  - `CMake MPI CI (1-rank)` (Job ID 128651834896): Completed in 26s (success)
  - `CMake MPI CI (2-rank)` (Job ID 128651835070): Completed in 28s (success)
- CTest execution log (observed in both matrix legs):
  ```text
  Test project /home/runner/work/gh-setup-cmake-p2-sandbox/gh-setup-cmake-p2-sandbox/build
      Start 1: mpi_smoke_1rank
  1/2 Test #1: mpi_smoke_1rank ..................   Passed    0.43 sec
      Start 2: mpi_smoke_2rank
  2/2 Test #2: mpi_smoke_2rank ..................   Passed    0.32 sec

  100% tests passed, 0 tests failed out of 2
  Total Test time (real) =   0.87 sec
  ```
- Terminal **Stable Status Check**: `status-check` (Job ID 128651936285): Completed in 4s (success)
  - Evaluated context: `{"ci": {"result": "success", "outputs": {}}}`
  - Observed output:
    ```text
    Status check results - success: ['ci'], skipped: [], failed: []
    Status check passed.
    ```
- Final PR check status: `0 cancelled, 0 failing, 3 successful, 0 skipped, and 0 pending checks`.
- Candidate PR #2 merged cleanly into `main` at commit `3f6215b`.

### Post-merge Headless idempotency

Re-running Headless dry-run and apply with the CMake/MPI Policy against `lispz1610/gh-setup-cmake-p2-sandbox`:
- Dry-Run output (exit code 0):
  ```json
  {
    "mode": "dry-run",
    "repository": "lispz1610/gh-setup-cmake-p2-sandbox",
    "changes": [],
    "status": "no-op",
    "policy_sha256": "0866004ec54e1c01a28b58de0c4fd39b69122c577417b2a268d10d14a957f8c4"
  }
  ```
- Apply output (exit code 0):
  ```json
  {
    "mode": "apply",
    "repository": "lispz1610/gh-setup-cmake-p2-sandbox",
    "changes": [],
    "status": "no-op",
    "setup_pull_request": null,
    "setup_branch": null
  }
  ```
- Result: Zero modifications, zero redundant branches, zero duplicate PRs.

---

## 3. Local disposable verification exercise (P6.5 / `#183`)

To verify that the documented scientific CI profiles, check names, and smoke commands execute reproducibly on the current workstation (`Ubuntu 24.04`, `Python 3.12`, `g++ 13.3.0`, `Open MPI 4.1.6`), a disposable workspace exercise was executed and recorded with exact environment availability, commands, exit codes, and unexecuted paths.

### Local toolchain probe

- **Observed present**: `python3` (`/usr/bin/python3`), `make` (`/usr/bin/make`), `g++` (`/usr/bin/g++`, Ubuntu 13.3.0-6ubuntu2~24.04.1), `mpicxx` (`/usr/bin/mpicxx`), `mpirun` (`/usr/bin/mpirun`, Open MPI 4.1.6).
- **Observed absent on local host**: `cmake`, `ctest`, `clang++`, `ccache`, `clinfo`, physical NVIDIA/AMD/Intel GPU hardware (`nvidia-smi` / `rocm-smi`), and multi-node Slurm/InfiniBand cluster nodes.

### Exercise A — Disposable Python profile (`python-scientific` / `scientific-python`)

1. **Workflow generation & check extraction (`scripts/install_github_workflows.py`)**:
   - Rendered Policy v2 `ci-profile` (`profile: "scientific-python"`, `versions: ["3.10", "3.11", "3.12"]`, `package_manager: "pip"`, `cache: true`, `lfs: true`, `status_check_name: "status-check"`).
   - Verified `# github-repo-setup workflow-template=profile-ci version=1` marker and `extract_profile_status_check(...) == "status-check"`.
2. **Local disposable execution**:
   - Command (lint / bytecode check): `python3 -m py_compile src/solver.py tests/test_solver.py` -> Exit status `0`.
   - Command (unit test suite): `python3 -m unittest discover -s tests -v` -> Exit status `0` (`test_step_rk2 ... ok`, `Ran 1 test in 0.000s`, `OK`).

### Exercise B — Disposable C++/MPI/OpenMP profile (`cmake-mpi` / `cpp-hpc-mpi`)

1. **Workflow generation & check extraction (`scripts/install_github_workflows.py`)**:
   - Rendered Policy v2 `ci-profile` (`profile: "cmake-mpi"`, `ranks: [1, 2]`, `ccache: true`, `opencl: true`, `status_check_name: "smoke-test"`).
   - Verified `# EVIDENCE DEMARCATION:` header, `::notice title=Device Boundary Demarcation::` step, and `extract_profile_status_check(...) == "smoke-test"`.
2. **Local disposable compilation & oversubscribed MPI + OpenMP execution**:
   - Build command (`manual_build: true` using `mpicxx` + OpenMP):
     ```bash
     mpicxx -O2 -fopenmp mpi_omp_smoke.cpp -o mpi_omp_smoke
     ```
     Exit status: `0`.
   - 1-rank smoke check (`ranks: 1`, `OMP_NUM_THREADS=2`):
     ```bash
     OMP_NUM_THREADS=2 mpirun --oversubscribe -np 1 ./mpi_omp_smoke
     ```
     Exit status: `0` — Output: `MPI_SIZE=1 OMP_THREADS=2 GLOBAL_SUM=2 EXPECTED=2`.
   - 2-rank pull-request smoke check (`ranks: 2`, `OMP_NUM_THREADS=2`):
     ```bash
     OMP_NUM_THREADS=2 mpirun --oversubscribe -np 2 ./mpi_omp_smoke
     ```
     Exit status: `0` — Output: `MPI_SIZE=2 OMP_THREADS=2 GLOBAL_SUM=6 EXPECTED=6`.
   - 4-rank default-branch oversubscribed check (`extended-mpi-validation` equivalent, `OMP_NUM_THREADS=2`):
     ```bash
     OMP_NUM_THREADS=2 mpirun --oversubscribe -np 4 ./mpi_omp_smoke
     ```
     Exit status: `0` — Output: `MPI_SIZE=4 OMP_THREADS=2 GLOBAL_SUM=20 EXPECTED=20`.

---

## 4. Technical claims and evidence boundaries (`Observed / Inferred / Unverified`)

| Technical claim | Required evidence | Observed / Inferred / Unverified result | Status |
| --- | --- | --- | --- |
| Python profile (`python-scientific` / `python`) renders and stages cleanly | Staged PR diff on GitHub containing only selected `.github/workflows/ci.yml` | **Observed**: Setup PR #1 staged diff `+49 -0 .github/workflows/ci.yml` with 40-char pinned Actions commits | **Observed** |
| Python CI runs across version matrix and caches dependencies | Execution of matrix jobs (`Python CI (3.11)`, `Python CI (3.12)`) with `cache: "pip"` | **Observed**: Run `42454914550` (and `24639303152` on `lispz1610/gh-setup-p2-sci-ci-20260419-203418`) executed Python 3.11 (6s) and Python 3.12 (5s) with `pip` caching enabled | **Observed** |
| CMake/MPI profile (`cmake-mpi` / `cpp-hpc-mpi`) stages cleanly with demarcation notices | Staged PR diff containing `# EVIDENCE DEMARCATION:` header and build/test steps | **Observed**: Setup PR #1 staged diff `+64 -0 .github/workflows/ci.yml` with notice annotations and pinned Actions | **Observed** |
| CMake/MPI executes 1-rank, 2-rank, and oversubscribed OpenMPI + OpenMP smoke checks | Execution of 1-rank and 2-rank MPI CTest checks on hosted runner (`42454936292`, `24639321584`) and local `mpirun --oversubscribe -np 2` with `OMP_NUM_THREADS=2` | **Observed**: Hosted runner passed `CMake MPI CI (1-rank)` (26s) and `CMake MPI CI (2-rank)` (28s); local Open MPI 4.1.6 + `g++ -fopenmp` passed 1, 2, and 4 ranks with 2 OpenMP threads per rank (exit `0`) | **Observed** |
| Stable status check anchor (`status-check` / `smoke-test`) prevents pending-check deadlocks | Execution of unmatrixed `status-check` / `smoke-test` job with `if: always()` and `reconcile_gates` discrepancy check | **Observed**: Hosted runs reported `status-check` success in 3s–4s; `evaluate_status_check` passes when `ci` is `skipped` by `detect-changes`; `reconcile_gates` blocks mismatched matrix check names | **Observed** |
| Profile Action pins are verified via GitHub Commit API | Commit SHA verification against `/repos/{owner}/{repo}/git/commits/{sha}` during staging | **Observed**: Pinned SHAs for `actions/checkout`, `actions/setup-python`, and `astral-sh/setup-uv` verified before staging | **Observed** |
| Subsequent setup runs are strictly idempotent | Dry-Run and Apply reporting `status: no-op` and `changes: []` | **Observed**: Sandbox repositories produced exit `0` `status: no-op`, `setup_pull_request: null`, and `setup_branch: null` | **Observed** |
| Shared-memory MPI collective order holds for the tested reduction kernel | `MPI_Allreduce` matches analytical sum $T \cdot N(N+1)/2$ across $N \in \{1, 2, 4\}$ ranks and $T=2$ OpenMP threads | **Inferred**: Rank/thread reduction logic is consistent for small shared-memory rank counts; does not bound network latency or contention on multi-node fabrics | **Inferred** |
| Local `cmake`/`ctest`, `clang++`, and `clinfo` POCL execution | Local installation of `cmake`, `clang++`, and `pocl-opencl-icd` | **Unverified on local workstation** (`cmake`, `ctest`, `clang++`, `clinfo` not installed locally; CMake/CTest verified on hosted `ubuntu-24.04` runner in Run `42454936292`) | **Unverified** (local host) / **Observed** (hosted runner) |
| Host CPU smoke checks prove real GPU/accelerator correctness | Execution on physical NVIDIA CUDA, AMD ROCm, or Intel GPU hardware with device query | **Unverified**: GitHub-hosted `ubuntu-24.04` runners and local container have no physical GPU; CPU fallbacks do not claim or prove real GPU execution | **Unverified** |
| Host oversubscribed MPI smoke test proves full multi-node HPC cluster scaling or numerical reproducibility | Execution on multi-node InfiniBand/Slingshot cluster under Slurm/PBS and cross-platform numerical reference audit | **Unverified**: Tests ran on single-node shared-memory CPUs; CPU smoke checks do not claim full HPC scaling or scientific/numerical reproducibility | **Unverified** |

---

## 5. Operational recovery and rollback

If a workflow staging pull request or subsequent configuration run needs inspection or rollback:

### Inspecting active pull request checks
```bash
gh pr checks <PR_NUMBER> --repo OWNER/REPO
gh run list --repo OWNER/REPO
gh run view <RUN_ID> --repo OWNER/REPO --log
```

### Reverting an applied Setup Pull Request
If a staged workflow must be reverted after merging into `main`:
```bash
git checkout main
git pull origin main
git revert -m 1 <MERGE_COMMIT_SHA> -m "revert: rollback scientific CI profile setup"
git push origin main
```

### Cleaning up disposable sandbox repositories
```bash
gh repo delete lispz1610/gh-setup-python-p2-sandbox --yes
gh repo delete lispz1610/gh-setup-cmake-p2-sandbox --yes
```
