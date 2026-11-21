# sciflow-cicd User Manual

`sciflow-cicd` configures, audits, and maintains scientific and software GitHub repositories using declarative policies and drift-checked Dry-Run previews. Direct repository settings apply via REST API; all file changes (workflows, Dependabot config, documentation, and agent skills) are staged together in a single draft **Setup Pull Request** for owner review and merge.

> Looking for architecture diagrams, trust boundaries, functional requirements (`FR-1`–`FR-7`), or detailed verification evidence? See the [Maintainer Reference](docs/maintainer-reference.md).

---

## 1. Prerequisites & Installation

### Requirements
- **Python 3.10+**
- **`git`** and **GitHub CLI (`gh`)**
- **`GITHUB_TOKEN`** exported in your shell environment with only the scopes your setup needs:
  - **Classic PAT / `gh` token:** `repo`, `workflow`, `read:org` (add `project` if managing Projects V2).
  - **Fine-grained PAT:** `Metadata` (read), `Administration`, `Contents`, `Workflows`, `Pull requests`, and `Issues` (read/write).

### Install & Authenticate

```bash
# Option A: Install globally (defaults: ~/.local/share/sciflow-cicd and ~/.local/bin/sciflow-cicd)
./install.sh

# Custom install paths:
# SCIFLOW_CICD_INSTALL_DIR=/opt/sciflow-cicd SCIFLOW_CICD_BIN_DIR=~/.local/bin ./install.sh

# Option B: Run directly from the cloned repository directory
git clone https://github.com/lispz1610/sciflow-cicd.git
cd sciflow-cicd
chmod +x sciflow-cicd
```

Even when `gh auth status` shows you are logged in, `sciflow-cicd` reads `GITHUB_TOKEN` from the shell environment. Export it before running any command:

```bash
gh auth status
export GITHUB_TOKEN="$(gh auth token)"
```

> **Path Note:** `sciflow-cicd` is the canonical executable name. If not installed onto `PATH` via `./install.sh`, run `./sciflow-cicd` from inside the repository root (or active worktree).

---

## 2. Quick Start

### Step 1: Audit Environment & Target Repository (Read-Only)

```bash
# Check local runtime (Python, git, gh auth, PATH)
sciflow-cicd doctor

# Audit target repository capabilities and controls (read-only)
sciflow-cicd doctor --repo OWNER/REPO
sciflow-cicd doctor --repo OWNER/REPO --json
```

`doctor` classifies each control as:
- `[CONFIGURED]`: Active and aligned with policy.
- `[MISSING]`: Supported on your repo/plan, but not enabled yet (for example, `Vulnerability Alerts` / `Dependency Graph` on a private repo before toggling them in **Settings -> Code security**).
- `[UNSUPPORTED]`: Unavailable on your repository visibility or GitHub plan (for example, CodeQL or Secret Scanning on private Free-plan repositories without GitHub Advanced Security).
- `[INACCESSIBLE]`: Blocked by missing token scopes or permissions.
- `[ADVISORY]`: A non-blocking observation, such as zero registered Self-Hosted Runners or an unverifiable runner inventory for a non-OCR workflow; it does not change verdict/exit code.

### Step 2: Run Interactive Setup

```bash
sciflow-cicd
```

Interactive Setup guides you through four phases:
1. Entering `Target repository (OWNER/REPO): ` and inspecting current configuration (**Resume State**: `1. Resume existing setup` or `2. Start from scratch`, including inspection of managed workflow files `ci.yml`, `dependency-review.yml`, `container-publish.yml`, `open-code-review.yml`, and `dependabot.yml`).
2. Selecting **Stage 1–5** options (repo features, merge rules, read-only Actions & security, labels/files, global runner selection defaulting to `self-hosted` with optional custom labels or hosted `ubuntu-24.04`, CI profiles, Advisory Review, Dependabot prompt integration, branch rulesets, Issue State Gate, and Matt Pocock agent skills).
3. Saving your choices to `.local/setup-policy.json` and previewing a read-only **Dry-Run** diff.
4. Prompting `Apply these changes? [y/N]: ` before mutating any remote settings or opening a draft Setup PR.

---

## 3. Headless Mode (Reusable Policies & CI)

Use Headless Mode to preview and apply declarative policies non-interactively using Compare-and-Swap (CAS) anti-drift protection:

```bash
# 1. Generate and inspect a read-only Dry-Run snapshot
sciflow-cicd dry-run --policy .local/setup-policy.json > .local/setup-dry-run.json

# 2. Review planned mutations before applying
grep -E '"(stage|operation|path|status)"' .local/setup-dry-run.json

# 3. Apply using the verified Dry-Run snapshot (aborts if repo state drifted)
sciflow-cicd apply --policy .local/setup-policy.json --dry-run-result .local/setup-dry-run.json

# Or target a specific repo with a template policy:
sciflow-cicd dry-run --policy examples/setup-policy.json --repo OWNER/REPO > .local/setup-dry-run.json
sciflow-cicd apply --policy examples/setup-policy.json --repo OWNER/REPO --dry-run-result .local/setup-dry-run.json
```

**Important boundaries:**
- **`files` stage scope:** Policy `files` stages only Issue forms (`.github/ISSUE_TEMPLATE/*`), PR templates (`.github/PULL_REQUEST_TEMPLATE*`), and authored `docs/*.md`. Commit root files (`CITATION.cff`, `.gitattributes`, `requirements.txt`) directly to Git or onto the Setup PR branch.
- **Updating managed workflows:** Modifying an existing managed workflow (for example, adding `agent-skills` when `documentation-freshness.yml` already exists) requires supplying its current blob SHA in `expected_sha`.

**Exit codes:**
- `0`: Success, `no-op` (already converged), or non-blocking `pending` (e.g., two-pass ruleset waiting for Setup PR merge).
- `1`: Invalid inputs, missing `GITHUB_TOKEN`, schema error, or `--dry-run-result` mismatch.
- `2`: Fatal block, apply failure, or repository state drift since the Dry-Run (`Repository state changed since the accepted Dry-Run; inspect a new Dry-Run before applying.`).

---

## 4. Setup Pull Request & Manual GitHub Steps

### Merging the Setup Pull Request

All workflows, Dependabot config, and agent skills are staged on branch `codex/workflows-<hash>` in one draft PR:

```bash
gh pr list --repo OWNER/REPO --head codex/workflows-
gh pr diff <PR_NUMBER> --repo OWNER/REPO
gh pr ready <PR_NUMBER> --repo OWNER/REPO
gh pr merge <PR_NUMBER> --squash --delete-branch --repo OWNER/REPO
```

**Operational rules when merging:**
1. **Two-pass branch rulesets (`gates` / `issue-state-gate`):** Merge the draft Setup PR first (`apply` reports `status: "pending"` on pass 1), let the required check (`status-check`, `smoke-test`, or `Issue State Gate`) pass on an open PR, then re-run `sciflow-cicd` (`1. Resume existing setup`) or `dry-run` + `apply` to activate the ruleset.
2. **Documentation Freshness tracking issues:** Any PR that modifies `.github/workflows/*.yml` triggers `documentation-freshness` and opens a `Documentation freshness: PR #N` issue. Even after you fix docs and `"Latest audit: clean"` appears, **a clean audit never auto-closes the issue**—close the tracking issue manually (`gh issue close <N>`) to turn the check green.
3. **`pull_request_target` workflows & `UNSTABLE` merge UI:** Workflows using `pull_request_target` (such as `open-code-review.yml`) always run from `main`, not the PR branch. If an advisory check fails, GitHub marks the PR `UNSTABLE` with warning styling, but advisory checks do not block merging in GitHub UI or via `gh pr merge <PR> --squash`.

### Manual GitHub UI Checklist

| Action | GitHub UI Path | When Needed |
| --- | --- | --- |
| **Workflow permissions** | **Settings -> Actions -> General** | If API update is blocked; set read-only token default and disable Actions PR creation/approval. |
| **Rulesets / Branches** | **Settings -> Rules -> Rulesets** (or **Settings -> Branches**) | Two-pass ruleset activation or classic branch protection on private Free repos. |
| **Actions Secrets & Variables** | **Settings -> Secrets and variables -> Actions** | `OCR_LLM_AUTH_TOKEN` + `OCR_LLM_URL` / `OCR_LLM_MODEL` / `OCR_LLM_FALLBACK_MODEL` / `OCR_LLM_USE_ANTHROPIC` (Advisory Review); `POLICY_AUDIT_TOKEN` (policy drift audit); `TRIAGE_PROJECT_TOKEN` + `TRIAGE_PROJECT_ID` (Projects V2). |
| **Code Security toggles** | **Settings -> Code security** | Enable Dependency graph / Vulnerability alerts (needed for SBOM export on private repos) and inspect plan-gated features (CodeQL, Secret protection). |
| **Initialize Wiki (optional)** | **Wiki** tab -> **Create the first page** -> **Save Page** | Required once before `wiki-sync` can push to `<OWNER>/<REPO>.wiki.git` (optional secondary mirror; `README.md` + `docs/` remain primary). |
| **Zenodo DOI (optional)** | Public: Zenodo GitHub toggle + Release; Private: `https://zenodo.org/deposit/new` | Private repos are hidden from Zenodo's GitHub list (`public_repo` scope). Only record a `doi` in `CITATION.cff` / `docs/release-control.json` after it resolves as `issued`. |

---

## 5. Features & Workflows Quick Reference

### Scientific CI Profiles ([Guide](docs/scientific-ci-profiles.md))
- **`python-scientific`** (`.github/workflows/ci.yml`): Runs `lint` and Python version matrix `unit-tests` on PRs under required check `status-check`; runs `full-test-suite` and `coverage` on pushes to `main`. Workload jobs default `runs-on: self-hosted` (with option to select hosted `ubuntu-24.04`), while auxiliary control jobs (`detect-changes`, `status-check`) remain isolated on `ubuntu-24.04`. Injects a Fork Execution Guard on public repositories. Supports `python_setup_mode` (`"setup-python"`, `"uv"`, `"system"`), `install_commands`, push-only `post_merge_commands`, `pip`/`uv` caching, and optional `git_lfs`.
  - **No-manifest guard:** `cache: "pip"` fails if the repo has no root `requirements.txt` or `pyproject.toml`. `sciflow-cicd` auto-detects manifests and defaults `"cache": false` when absent; you can also set `"cache": false` explicitly in `workflows` `python` options and install test runners explicitly in `commands` (e.g., `["pip install pytest pyyaml", "pytest -v"]`), or commit `requirements.txt`.
- **`cmake-mpi` / `cpp-hpc-mpi`**: Builds with `compilers` matrix (`gcc`, `clang`) and `ccache` compiler caching (with `actions/cache` pinned commit SHA); runs oversubscribed OpenMPI (`mpirun --oversubscribe`) + OpenMP smoke tests (`omp_threads`) under required check `smoke-test`; runs `extended-mpi-validation` (with `extended_ranks`) and `post_merge_commands` strictly on pushes to `main` (excluded from `status-check`). Gated by `install_system_deps` (default `false` on `self-hosted` to avoid failing `sudo apt-get` commands, `true` on `ubuntu-24.04`) and emits explicit runner demarcation notices.
- **Hardware note**: GitHub-hosted `ubuntu-24.04` runners are CPU-only VMs—they do not prove GPU kernel execution or numerical reproducibility. Passing CPU smoke checks on self-hosted or hosted runners verifies software compilation and host execution only; use Google Colab or an HPC cluster for real GPU/multi-node validation.

### Proportional Security & Merge Gates ([Guide](docs/proportional-security-and-controls.md))
- **Solo maintainer**: Set reviewer to `none` (`--approvals 0`) for `required_approving_review_count: 0` with required status checks.
- **Team / Collaborators**: Require `--reviewer REVIEWER` (`>= 1` independent approval).
- **Dependabot & supply chain**: Monthly grouped Dependabot updates (`.github/dependabot.yml`, capped at `open_pull_requests_limit: 5` concurrent PRs per ecosystem), SHA-pinned Actions, optional PR Dependency Review gate (`dependency-review` with configurable `fail_on_severity`, `allow_licenses`, `deny_licenses`, `comment_summary_in_pr`, defaulting `runs-on: self-hosted`), and optional `Issue State Gate` (`## Implementation issues`). Merging `.github/dependabot.yml` clears initial backlog up to 5 PRs at once; it does **not** open a new Dependabot PR per user PR.

### Releases, Citation & SBOM ([Guide](docs/release-evidence.md))
- Verify release readiness with `scripts/verify_release.py` and promote explicitly via immutable tag:
  ```bash
  python3 scripts/release_promotion.py --event event.json --output-dir assets/ --tag v<target_version> --profile scientific
  ```
- Profiles: `minimal` (`audit-report.md` + `runtime-fingerprint.txt`), `release` (adds fail-closed SPDX 2.3 `sbom.spdx.json`), `scientific` (adds validated `CITATION.cff`).
- Container publishing (`container-publish` to `ghcr.io`) is opt-in with configurable `triggers`, `registry`, `dockerfile`, `context`, `tags`, non-publishing on PRs, and default `runs-on: self-hosted`.

### Agent Skills & Hooks ([Guide](docs/agent-skills.md))
- Stages 27 pinned Matt Pocock skills (`81` total files including `LICENSE` and `manifest.json`) into `.agents/skills/` as inert files. If `.gitignore` ignores `/.agents/`, update it to `/.agents/*` and `!/.agents/skills/` so Git tracks the bundle. Verify discovery with:
  ```bash
  python3 scripts/verify_agent_skill_discovery.py
  ```
- **Opt-In Hook Contract**: Lifecycle hooks are **disabled by default** (`0` active hooks) and activate only when both Policy and `hook.json` declare `opt_in: true`.

### Advisory Review & Two-Pass OCR ([Guide](docs/advisory-review.md))
Advisory Review supports two non-blocking modes (private repos only):
1. **GitHub Actions mode (`.github/workflows/open-code-review.yml`)**: Nothing is installed on your local VM. Workflows default `runs-on: self-hosted` across both `review` and `report` jobs (with hosted `ubuntu-24.04` option) to eliminate Actions runner minute consumption. Runners install `alibaba/open-code-review` automatically, check `OCR_LLM_AUTH_TOKEN`, `OCR_LLM_URL`, `OCR_LLM_MODEL`, `OCR_LLM_FALLBACK_MODEL`, `OCR_LLM_USE_ANTHROPIC`, and optional `OCR_LLM_EXTRA_BODY`, run the primary review (for example, compact `gemma-4-31b-it` with `max_tokens_budget: "14000"`, optional `llm.extra_body` / `OCR_LLM_EXTRA_BODY` sampling parameters, and `.gitattributes` lockfile pruning), and automatically fall back (`fallback_review`) to `OCR_LLM_FALLBACK_MODEL` (for example, `gemini-3.8-flash` with `"reasoning_effort": "medium"` for OpenAI-compatible endpoints and `max_tokens_budget: "100000"`) if the diff overflows context.
2. **Local operator CLI mode (`sciflow-cicd ocr`)**: Runs locally in `local_direct` mode with a dedicated limited OCR GitHub App (`contents: read`, `pull_requests: write`, `metadata: read`; `administration` forbidden), strictly rejecting self-hosted runner environments, and skipping repos that already run `.github/workflows/open-code-review.yml`:
   ```bash
   sciflow-cicd ocr preflight-app --app-id APP_ID --admin-app-id ADMIN_APP_ID --private-key-path ~/.config/sciflow-cicd/ocr-app.pem --allowlist OWNER/REPO
   sciflow-cicd ocr preflight     --app-id APP_ID --admin-app-id ADMIN_APP_ID --private-key-path ~/.config/sciflow-cicd/ocr-app.pem --allowlist OWNER/REPO
   sciflow-cicd ocr poll          --app-id APP_ID --admin-app-id ADMIN_APP_ID --private-key-path ~/.config/sciflow-cicd/ocr-app.pem --allowlist OWNER/REPO
   sciflow-cicd ocr check-safety  --repo OWNER/REPO --pr 1
   sciflow-cicd ocr run           --app-id APP_ID --admin-app-id ADMIN_APP_ID --private-key-path ~/.config/sciflow-cicd/ocr-app.pem --allowlist OWNER/REPO
   ```
   Missing `ocr` binary or `OCR_LLM_*` credentials exit `0` with `pending_configuration`.

---

## 6. Direct Scripts & Local Verification

Run single-resource commands directly (preview first, add `--apply` to mutate):

| Task | Command |
| --- | --- |
| Live repo inventory | `python3 scripts/setup_github_repo.py --repo OWNER/REPO inventory` |
| Policy dry-run / apply | `python3 scripts/setup_github_repo.py --repo OWNER/REPO policy --file .local/setup-policy.json` |
| Repo features / actions / security / gates | `python3 scripts/configure_github_repo.py --repo OWNER/REPO <subcommand>` |
| Stage workflows | `python3 scripts/install_github_workflows.py --repo OWNER/REPO --policy examples/workflows-policy.json` |
| Reconcile Projects V2 | `python3 scripts/setup_github_projects.py --policy examples/projects-policy.json` |
| Export SPDX 2.3 SBOM | `python3 scripts/export_release_sbom.py --repo OWNER/REPO --output sbom.spdx.json` |
| Publish Wiki mirror | `python3 scripts/publish_github_wiki.py --repo OWNER/REPO --source-dir . --page README.md=Home` |

Run local tests and documentation/architecture audits:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/check_markdown_links.py
python3 scripts/documentation_freshness.py
python3 scripts/lint_workflow_action_pins.py
python3 scripts/audit_module_dependencies.py
```

---

## 7. Common Troubleshooting

| Symptom | Fix |
| --- | --- |
| **`./sciflow-cicd: No such file or directory` or `GITHUB_TOKEN` missing** | Run commands from inside the `sciflow-cicd` repo (or run `./install.sh`) and run `export GITHUB_TOKEN="$(gh auth token)"`. |
| **HTTP 403 / 404 staging workflows or `[INACCESSIBLE]`** | Refresh PAT scopes: `gh auth refresh -h github.com -s repo,workflow,read:org` (or grant `Workflows: write` + `Administration: write`). |
| **`Python CI` fails: `No file ... matched to [**/requirements.txt or **/pyproject.toml]`** | Add a root `requirements.txt` / `pyproject.toml`, or set `"cache": false` in `workflows` `python` options and run `pip install` in `commands`. |
| **`documentation-freshness` fails despite `"Latest audit: clean"`** | Update `docs/*.md` if workflow behavior changed, verify the clean rerun, and **manually close** the open `Documentation freshness: PR #N` issue. |
| **Open Code Review fails (`token_budget_reached` or missing vars)** | Configure `OCR_LLM_AUTH_TOKEN`, `OCR_LLM_URL`, `OCR_LLM_MODEL`, `OCR_LLM_FALLBACK_MODEL`, and `OCR_LLM_USE_ANTHROPIC`. For 16k models, keep primary `max_tokens_budget: "14000"` + `.gitattributes` pruning and rely on `fallback_review`. |
| **GitHub PR merge box warns `UNSTABLE` when OCR fails** | Open Code Review is advisory and non-blocking. Merge via the GitHub UI confirmation or `gh pr merge <PR> --squash --repo OWNER/REPO`. |
| **Apply shows `Pending: 1` for `gates`** | Normal on clean repos: merge the draft Setup PR, let CI pass on an open PR, then re-run `sciflow-cicd`. |
| **Apply exits `2` (state drift)** | Repo changed after Dry-Run. Re-run `sciflow-cicd dry-run ... > .local/setup-dry-run.json` and re-apply. |
| **`codeql` / `secret_scanning` / `branch_rulesets` `[UNSUPPORTED]`** | Feature unavailable on private Free repos without Pro/GHAS. Omit stage from policy or use **Settings -> Branches**. |
| **Repository missing from Zenodo GitHub list** | Zenodo's webhook integration (`public_repo` scope) hides private repos. Use `https://zenodo.org/deposit/new` for private manual deposits, or make the repo public and click **Sync** on Zenodo. |
| **`Wiki sync` fails (`repository not found`)** | Create the first Wiki page manually in GitHub UI (**Wiki** tab -> **Create the first page**). |

For the full symptom matrix, security boundaries, and failure recovery procedures, see [Maintainer Reference — Troubleshooting](docs/maintainer-reference.md#troubleshooting-and-recovery-by-symptom).

---

## 8. Frequently Asked Questions (FAQ)

**1. Why do I need `export GITHUB_TOKEN="$(gh auth token)"` if `gh auth status` is already logged in?**
`gh` stores credentials in its own keyring, whereas `sciflow-cicd` and direct scripts read the `GITHUB_TOKEN` environment variable. Exporting `$(gh auth token)` bridges your active `gh` session cleanly without writing tokens to files.

**2. Why did `sciflow-cicd` open 5 Dependabot PRs and 5 Issues right after I merged `.github/dependabot.yml`?**
When Dependabot first activates, it scans existing workflow Action pins and dependency manifests and opens up to `open_pull_requests_limit` (default `5`) concurrent PRs to clear the backlog. It does **not** open a new Dependabot PR every time you open a PR—it only runs on its configured `cadence` (`monthly` by default) within that cap of `5` open PRs. Each opened PR modifying `.github/workflows/**` also triggers `documentation-freshness`, creating 1 tracking issue per PR until closed.

**3. Why does `documentation-freshness` keep failing when the report says `"Latest audit: clean"`?**
By design ([`docs/documentation-freshness.md`](docs/documentation-freshness.md)), clean reruns refresh an existing `Documentation freshness: PR #N` issue without closing it automatically. If you changed `.github/workflows/*.yml`, confirm `docs/*.md` reflects the behavior, wait for the `"Latest audit: clean"` report, and manually close the tracking issue. The status check immediately switches to green.

**4. Do I have to install `ocr` (OpenCodeReview) on my local machine?**
No, if you use **Review Setup** (`.github/workflows/open-code-review.yml`). GitHub Actions installs and executes `alibaba/open-code-review` inside `ubuntu-24.04` cloud runners. You only need a local `ocr` binary if you run the standalone `sciflow-cicd ocr` CLI daemon.

**5. How do I use a compact-context model (such as Gemma 4 31B with 16k peak input) without failing on large PRs?**
Set the primary step's `max_tokens_budget` to `"14000"`, exclude lockfiles and minified assets in `.gitattributes` (`linguist-generated=true`), set `OCR_LLM_MODEL` to your compact model (`gemma-4-31b-it`), and set `OCR_LLM_FALLBACK_MODEL` to a long-context model (`gemini-3.8-flash`). Small diffs stay on the primary model; diffs exceeding 14k tokens automatically fall back to `fallback_review` (`100000` budget, `"reasoning_effort": "medium"`).

**6. Why are both `OCR_LLM_MODEL` and `OCR_LLM_FALLBACK_MODEL` required repository variables?**
Enforcing both as standardized GitHub Actions variables allows you to swap either the primary or fallback model directly in **Settings -> Secrets and variables -> Actions** without editing YAML or pushing commits.

**7. I modified `.github/workflows/open-code-review.yml` in a PR—why did the PR run the old workflow?**
`open-code-review.yml` uses the `pull_request_target` trigger. For security, GitHub Actions always executes `pull_request_target` workflows from the default branch (`main`), never from the unmerged PR branch. Your edits take effect as soon as the PR is merged into `main`.

**8. Why can't I put root files (`CITATION.cff`, `requirements.txt`) in the Policy `files` stage, and why were `.agents/skills/` ignored by Git?**
The `files` stage intentionally accepts only Issue forms, PR templates, and `docs/*.md` ([`docs/policy-reference.md`](docs/policy-reference.md)); commit root files directly to Git or your Setup PR branch. For `.agents/skills/`, replace any blanket `/.agents/` rule in `.gitignore` with `/.agents/*` and `!/.agents/skills/` so the 81 skill files stay tracked.

**9. Why doesn't my repository appear on Zenodo's GitHub integration page?**
Zenodo's GitHub OAuth app requests only the `public_repo` scope, so private repositories never appear on `https://zenodo.org/account/settings/github/`. Either keep the repo private and perform a manual deposit at `https://zenodo.org/deposit/new`, or make the repo public in GitHub Settings and click **Sync** on Zenodo.

**10. Do I need to configure anything extra for the SPDX 2.3 SBOM?**
Only ensure **Vulnerability alerts / Dependency graph** is enabled under **Settings -> Code security** (verified via `[CONFIGURED]` in `sciflow-cicd doctor`). Release promotion (`scripts/release_promotion.py`) then automatically exports and attaches `sbom.spdx.json` during releases, and you can test it anytime locally with `GITHUB_TOKEN="$(gh auth token)" python3 scripts/export_release_sbom.py --repo OWNER/REPO --output sbom.spdx.json`.

**11. Why isn't the GitHub Wiki initialized automatically, and how do I initialize it manually?**
GitHub's REST and GraphQL APIs cannot create the underlying `<owner>/<repo>.wiki.git` repository—GitHub only creates it when a human creates the first page in the web UI. Furthermore, private repositories under GitHub Free do not support Wikis. To initialize it on supported repositories:
1. Ensure the feature is enabled under **Settings -> General -> Features -> Wikis**.
2. Click the repository **Wiki** tab at the top of the GitHub page.
3. Click **Create the first page**, enter a title/body, and click **Save Page**.
Once that initial page exists, GitHub Actions (`.github/workflows/wiki-sync.yml`) or `scripts/publish_github_wiki.py` will automatically mirror `README.md` and `docs/**/*.md` to the Wiki on pushes to `main`. Per [ADR 0005](docs/adr/0005-canonical-readme-and-wiki-fallback.md), the Wiki is strictly an optional secondary mirror; `README.md` and `docs/` inside Git remain the canonical source of truth.

---

## 9. Documentation Index

- **Reference & Architecture:** [Maintainer Reference](docs/maintainer-reference.md) | [Glossary](GLOSSARY.md) | [Agent Instructions](AGENTS.md) | [Future Works](docs/future-works.md)
- **User & Operator Guides:** [Policy Reference](docs/policy-reference.md) | [Workflows](docs/workflows.md) | [Scientific CI Profiles](docs/scientific-ci-profiles.md) | [Proportional Security & Merge Gates](docs/proportional-security-and-controls.md) | [Dependency Graph Governance](docs/dependency-graph-governance.md) | [Release Evidence](docs/release-evidence.md) | [DOI Guidance](docs/doi-guidance.md) | [Agent Skills](docs/agent-skills.md) | [Documentation Freshness](docs/documentation-freshness.md) | [Advisory Review](docs/advisory-review.md) | [Policy Drift Audit](docs/policy-drift-audit.md) | [Projects V2](docs/projects-v2.md)
- **Agent Conventions:** [Domain & ADRs](docs/agents/domain.md) | [Issue Tracker](docs/agents/issue-tracker.md) | [Triage Labels](docs/agents/triage-labels.md) | [Issue State Gate](docs/agents/issue-state-gate.md) | [Automation](docs/agents/automation.md) | [Wiki Publishing](docs/agents/wiki.md)
- **ADRs:** [0001](docs/adr/0001-unify-repository-and-review-setup.md) | [0002](docs/adr/0002-one-setup-pull-request-for-selected-workflows.md) | [0003](docs/adr/0003-self-contained-release-candidate-verification.md) | [0004](docs/adr/0004-isolate-automated-report-identities.md) | [0005](docs/adr/0005-canonical-readme-and-wiki-fallback.md) | [0006](docs/adr/0006-single-commit-cutover-for-clean-delivery.md) | [0007](docs/adr/0007-self-hosted-default-and-unified-workflow-schema.md)
- **Release Evidence Reports:** [Release Control Report](docs/release-control.md) | [Production Release Audit](docs/production-release-audit.md)
