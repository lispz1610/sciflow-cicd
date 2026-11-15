# sciflow-cicd User Manual

`sciflow-cicd` configures, audits, and maintains scientific and software GitHub repositories using declarative policies and safe dry-run previews. All file changes (workflows, Dependabot config, and agent skills) are staged in a single draft **Setup Pull Request** for owner review and merge.

> Looking for architecture diagrams, trust boundaries, functional requirements (`FR-1`–`FR-7`), or detailed verification evidence? See the [Maintainer Reference](docs/maintainer-reference.md).

---

## 1. Prerequisites & Installation

### Requirements
- **Python 3.10+**
- **`git`** and **GitHub CLI (`gh`)**
- **`GITHUB_TOKEN`** with only the scopes your setup needs:
  - **Classic PAT / `gh` token:** `repo`, `workflow`, `read:org` (add `project` if managing Projects V2).
  - **Fine-grained PAT:** `Metadata` (read), `Administration`, `Contents`, `Workflows`, `Pull requests`, and `Issues` (read/write).

### Install

```bash
# Option A: Install via script (defaults: ~/.local/share/sciflow-cicd and ~/.local/bin/sciflow-cicd)
./install.sh

# Custom install paths:
# SCIFLOW_CICD_INSTALL_DIR=/opt/sciflow-cicd SCIFLOW_CICD_BIN_DIR=~/.local/bin ./install.sh

# Option B: Run from a local clone
git clone https://github.com/lispz1610/sciflow-cicd.git
cd sciflow-cicd
chmod +x sciflow-cicd
```

Authenticate and export your token before running setup commands:

```bash
gh auth status
export GITHUB_TOKEN="$(gh auth token)"
```

---

## 2. Quick Start

### Step 1: Check your environment and target repository

```bash
# Check local runtime (Python, git, gh auth, PATH)
sciflow-cicd doctor

# Audit repository capabilities and controls (read-only)
sciflow-cicd doctor --repo OWNER/REPO
sciflow-cicd doctor --repo OWNER/REPO --json
```

`doctor` classifies each control as `[CONFIGURED]`, `[MISSING]`, `[UNSUPPORTED]` (unavailable on current plan/visibility), or `[INACCESSIBLE]` (missing token permissions).

### Step 2: Run Interactive Setup

```bash
sciflow-cicd
```

Interactive Setup guides you through:
1. Entering `Target repository (OWNER/REPO): ` and inspecting current configuration (**Resume State**: `1. Resume existing setup` or `2. Start from scratch`).
2. Selecting **Stage 1–5** options (repo features, merge rules, read-only Actions & security, labels/files, CI profiles, Advisory Review, branch rulesets, Issue State Gate, and Matt Pocock agent skills).
3. Saving your choices to `.local/setup-policy.json` and displaying a read-only **Dry-Run** preview.
4. Prompting `Apply these changes? [y/N]: ` before touching any remote settings or opening a draft Setup PR.

---

## 3. Headless Mode (Reusable Policies & CI)

Use Headless Mode to preview and apply a checked-in or template policy non-interactively:

```bash
# 1. Generate and inspect a read-only Dry-Run snapshot
sciflow-cicd dry-run --policy .local/setup-policy.json > .local/setup-dry-run.json

# 2. Apply using the verified Dry-Run snapshot (fails if repo state drifted)
sciflow-cicd apply --policy .local/setup-policy.json --dry-run-result .local/setup-dry-run.json

# Or apply a template policy to a specific repo:
sciflow-cicd dry-run --policy examples/setup-policy.json --repo OWNER/REPO > .local/setup-dry-run.json
sciflow-cicd apply --policy examples/setup-policy.json --repo OWNER/REPO --dry-run-result .local/setup-dry-run.json
```

**Exit codes:**
- `0`: Success, `no-op` (already converged), or non-blocking `pending` (e.g., two-pass ruleset waiting for Setup PR merge).
- `1`: Invalid inputs, missing `GITHUB_TOKEN`, schema error, or `--dry-run-result` mismatch.
- `2`: Fatal block, apply failure, or repository state drift since the Dry-Run (`Repository state changed since the accepted Dry-Run; inspect a new Dry-Run before applying.`).

---

## 4. Setup Pull Request & Manual GitHub Steps

### Merging the Setup Pull Request

Workflows, Dependabot config, and agent skills are committed to branch `codex/workflows-<hash>` in one draft PR:

```bash
gh pr list --repo OWNER/REPO --head codex/workflows-
gh pr diff <PR_NUMBER> --repo OWNER/REPO
gh pr ready <PR_NUMBER> --repo OWNER/REPO
gh pr merge <PR_NUMBER> --squash --delete-branch --repo OWNER/REPO
```

If you enabled branch rulesets (`gates` or `issue-state-gate`) alongside new workflows:
1. Merge the draft Setup PR first (`apply` reports `status: "pending"` on pass 1).
2. Let the required check (`status-check`, `smoke-test`, or `Issue State Gate`) pass on an open PR.
3. Re-run `sciflow-cicd` (`1. Resume existing setup`) or `dry-run` + `apply` to activate the ruleset.

### Manual GitHub UI Checklist

| Action | GitHub UI Path | When Needed |
| --- | --- | --- |
| **Workflow permissions** | **Settings -> Actions -> General** | If API update is blocked; set read-only token default and disable Actions PR creation/approval. |
| **Rulesets / Branches** | **Settings -> Rules -> Rulesets** (or **Settings -> Branches**) | Two-pass ruleset activation or classic branch protection on private Free repos. |
| **Actions Secrets & Variables** | **Settings -> Secrets and variables -> Actions** | `OCR_LLM_AUTH_TOKEN` + `OCR_LLM_URL` / `OCR_LLM_MODEL` / `OCR_LLM_USE_ANTHROPIC` (Advisory Review); `POLICY_AUDIT_TOKEN` (policy drift audit); `TRIAGE_PROJECT_TOKEN` + `TRIAGE_PROJECT_ID` (Projects V2). |
| **Code Security toggles** | **Settings -> Code security** | Inspect plan-gated features (Dependency graph, Dependabot, CodeQL, Secret protection). |
| **Initialize Wiki (optional)** | **Wiki** tab -> **Create the first page** -> **Save Page** | Required once before `wiki-sync` can push to `<OWNER>/<REPO>.wiki.git` (optional secondary mirror; `README.md` + `docs/` remain primary). |
| **Zenodo DOI (optional)** | Zenodo GitHub webhook or `https://zenodo.org/deposit/new` | Only record a `doi` in `CITATION.cff` / `docs/release-control.json` after it resolves as `issued`. |

---

## 5. Features & Workflows Quick Reference

### Scientific CI Profiles ([Guide](docs/scientific-ci-profiles.md))
- **`python-scientific`**: Runs `lint` and Python version matrix `unit-tests` on PRs under required check `status-check`; runs `full-test-suite` and `coverage` on pushes to `main`. Supports `pip`/`uv` caching and optional `git_lfs`.
- **`cmake-mpi` / `cpp-hpc-mpi`**: Builds with `gcc`/`clang` (`ccache`) and runs oversubscribed OpenMPI (`mpirun --oversubscribe`) + OpenMP smoke tests under required check `smoke-test`; runs `extended-mpi-validation` on pushes to `main`.
- **Hardware note**: GitHub-hosted `ubuntu-24.04` runners are CPU-only VMs—they do not prove GPU kernel execution or numerical reproducibility. Use Google Colab or an HPC cluster for real GPU/multi-node validation.

### Proportional Security & Merge Gates ([Guide](docs/proportional-security-and-controls.md))
- **Solo maintainer**: Set reviewer to `none` (`--approvals 0`) for `required_approving_review_count: 0` with required status checks.
- **Team / Collaborators**: Require `--reviewer REVIEWER` (`>= 1` independent approval).
- **Supply chain**: Monthly grouped Dependabot updates (`.github/dependabot.yml`), SHA-pinned Actions, optional PR Dependency Review gate, and optional `Issue State Gate` (`## Implementation issues`).

### Releases, Citation & SBOM ([Guide](docs/release-evidence.md))
- Verify release readiness with `scripts/verify_release.py` and promote explicitly via immutable tag:
  ```bash
  python3 scripts/release_promotion.py --event event.json --output-dir assets/ --tag v<target_version> --profile scientific
  ```
- Profiles: `minimal` (`audit-report.md` + `runtime-fingerprint.txt`), `release` (adds fail-closed SPDX 2.3 `sbom.spdx.json`), `scientific` (adds validated `CITATION.cff`).
- Container publishing (`container-publish` to `ghcr.io`) is opt-in.

### Agent Skills & Hooks ([Guide](docs/agent-skills.md))
- Stages 27 pinned Matt Pocock skills into `.agents/skills/` as inert files. Verify discovery with:
  ```bash
  python3 scripts/verify_agent_skill_discovery.py
  ```
- **Opt-In Hook Contract**: Lifecycle hooks are **disabled by default** (`0` active hooks) and activate only when both Policy and `hook.json` declare `opt_in: true`.

### Automated OCR Pipeline (`sciflow-cicd ocr`) ([Guide](docs/advisory-review.md))
Local operator pipeline for non-blocking PR advisory reviews on private repos (skips forks and repos running `.github/workflows/open-code-review.yml`):

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
| **HTTP 403 / 404 staging workflows or `[INACCESSIBLE]`** | Refresh PAT scopes: `gh auth refresh -h github.com -s repo,workflow,read:org` (or grant `Workflows: write` + `Administration: write`). |
| **Apply shows `Pending: 1` for `gates`** | Normal on clean repos: merge the draft Setup PR, let CI pass on an open PR, then re-run `sciflow-cicd`. |
| **Apply exits `2` (state drift)** | Repo changed after Dry-Run. Re-run `sciflow-cicd dry-run ... > .local/setup-dry-run.json` and re-apply. |
| **`codeql` / `secret_scanning` / `branch_rulesets` `[UNSUPPORTED]`** | Feature unavailable on private Free repos without Pro/GHAS. Omit stage from policy or use **Settings -> Branches**. |
| **`Wiki sync` fails (`repository not found`)** | Create the first Wiki page manually in GitHub UI (**Wiki** tab -> **Create the first page**). |

For the full symptom matrix, security boundaries, and failure recovery procedures, see [Maintainer Reference — Troubleshooting](docs/maintainer-reference.md#troubleshooting-and-recovery-by-symptom).

---

## 8. Documentation Index

- **Reference & Architecture:** [Maintainer Reference](docs/maintainer-reference.md) | [Glossary](GLOSSARY.md) | [Agent Instructions](AGENTS.md) | [Future Works](docs/future-works.md)
- **User & Operator Guides:** [Policy Reference](docs/policy-reference.md) | [Workflows](docs/workflows.md) | [Scientific CI Profiles](docs/scientific-ci-profiles.md) | [Proportional Security & Merge Gates](docs/proportional-security-and-controls.md) | [Dependency Graph Governance](docs/dependency-graph-governance.md) | [Release Evidence](docs/release-evidence.md) | [DOI Guidance](docs/doi-guidance.md) | [Agent Skills](docs/agent-skills.md) | [Documentation Freshness](docs/documentation-freshness.md) | [Advisory Review](docs/advisory-review.md) | [Policy Drift Audit](docs/policy-drift-audit.md) | [Projects V2](docs/projects-v2.md)
- **Agent Conventions:** [Domain & ADRs](docs/agents/domain.md) | [Issue Tracker](docs/agents/issue-tracker.md) | [Triage Labels](docs/agents/triage-labels.md) | [Issue State Gate](docs/agents/issue-state-gate.md) | [Automation](docs/agents/automation.md) | [Wiki Publishing](docs/agents/wiki.md)
- **ADRs:** [0001](docs/adr/0001-unify-repository-and-review-setup.md) | [0002](docs/adr/0002-one-setup-pull-request-for-selected-workflows.md) | [0003](docs/adr/0003-self-contained-release-candidate-verification.md) | [0004](docs/adr/0004-isolate-automated-report-identities.md) | [0005](docs/adr/0005-canonical-readme-and-wiki-fallback.md) | [0006](docs/adr/0006-single-commit-cutover-for-clean-delivery.md)
- **Release Evidence Reports:** [Release Control Report](docs/release-control.md) | [Production Release Audit](docs/production-release-audit.md)
