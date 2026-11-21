# Self-hosted runner default and unified workflow schema

Status: accepted

## Context

`sciflow-cicd` previously hardcoded `runs-on: ubuntu-24.04` across all version 1 workflow templates (`pr-ci`, `profile-ci`, `dependency-review`, `container-publish`, and `open-code-review.yml`). Running heavy CI matrices and frequent **Advisory Review** runs exclusively on GitHub-hosted runners consumes substantial Actions minutes quota, and hardcoded `sudo apt-get` / `setup-python` steps break when targeting dedicated hardware or HPC environments. At the same time, the Policy schema accumulated overlapping workflow definitions (`workflows` vs. `ci-profile` stages and conflicting option aliases) and diverged from Interactive Setup and documentation.

## Decision

1. **Default `self-hosted` runner for configurable workflows (including Review Setup):** All configurable workflow templates (`pr-ci`, `profile-ci`, `dependency-review`, `container-publish`) and the **Review Setup** workflow (`.github/workflows/open-code-review.yml`, both `review` and `report` jobs) default `runs_on` to `"self-hosted"` (accepting either a runner label string or a non-empty list of labels) and prompt the maintainer whether to keep the default or switch to a **Hosted Runner** (`ubuntu-24.04`).
2. **Preserve `local_direct` boundary for Automated OCR Pipeline and hosted execution for fixed/control jobs:** The local `sciflow-cicd ocr` CLI pipeline retains its strict non-self-hosted (`local_direct`) safety boundary in `scripts/ocr_safety.py`. Fixed trusted-suite workflows (`issue-state-gate`, `documentation-freshness`) and auxiliary control jobs (`detect-changes`, `status-check` in `profile-ci`) remain on `ubuntu-24.04` so fork pull requests never execute unvetted control logic on self-hosted machines and required **Stable Status Checks** never deadlock.
3. **Public-repository Fork Execution Guard:** When a configurable workflow targets a `self-hosted` runner with `pull_request` triggers in a public repository, `sciflow-cicd` injects a **Fork Execution Guard** (`if: github.event.pull_request.head.repo.full_name == github.repository`) by default on workload jobs and surfaces a Dry-Run warning recommending GitHub's *"Require approval for all outside collaborators"* policy.
4. **Environment provisioning & unified Policy schema (breaking cleanup):** When targeting `self-hosted`, `cmake-mpi` disables `sudo apt-get` system package installation by default (opt-in) and `python` supports `system` runtime mode alongside `setup-python` and `uv` with automatic dependency-manifest detection for caching. The redundant `ci-profile` Policy stage and legacy aliases (`options`, `pip_cache`, `git_lfs`) are removed in favor of a single canonical schema under `workflows`.

## Consequences

- Maintainers save GitHub Actions quota out of the box across CI and **Review Setup**, while lightweight gatekeepers (`detect-changes`, `status-check`, `issue-state-gate`, `documentation-freshness`) stay isolated on hosted VMs.
- Future readers must note why `.github/workflows/open-code-review.yml` defaults to `self-hosted` while `sciflow-cicd ocr` continues to reject self-hosted runner environments: the former avoids Actions quota consumption inside GitHub Actions, whereas the latter is an operator-run local process with limited App credentials.
- Existing Policies using the removed `ci-profile` stage or legacy `template_version: 1` assumptions must migrate to the unified `workflows` schema.
