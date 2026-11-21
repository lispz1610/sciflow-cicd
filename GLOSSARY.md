# sciflow-cicd

This context covers the domain concepts and terminology for configuring, auditing, and governing scientific and software GitHub repositories using `sciflow-cicd`.

## Language

**Interactive Setup**:
A guided command-line session that inspects a repository, asks the maintainer configuration questions, previews changes, and applies them upon confirmation.
_Avoid_: TUI, GUI, wizard

**Headless Mode**:
Non-interactive execution of repository inspection, dry-run calculation, or configuration application driven entirely by a saved policy document.
_Avoid_: Automated mode, silent mode, script mode

**Policy**:
A versioned document that declares the desired end-state of a repository's features, security controls, merge rules, workflows, and agent skills.
_Avoid_: Config file, manifest, setup file

**Setup Profile**:
A curated repository archetype configuration that defines recommended features, merge controls, security policies, CI/CD workflows, and agent skills inheriting from Universal Common Baseline Governance.
_Avoid_: Template, preset, bundle

**Universal Common Baseline Governance**:
The foundational repository configuration applied universally across all Setup Profiles before archetype specializations are evaluated. Enforces baseline governance including Issues enabled, Wiki disabled, squash merge with automatic branch deletion, read-only Actions token permissions, automated vulnerability alerts, and the canonical triage label taxonomy.
_Avoid_: Default config, base template, generic setup

**Inventory**:
A read-only snapshot of a repository's live configuration and plan-gated capabilities queried before proposing or applying changes.
_Avoid_: State check, read-only mode, fetch

**Resume State**:
The behavior where Interactive Setup pre-fills prompts from the live Inventory so maintainers can adjust existing configuration without starting from scratch.
_Avoid_: Update mode, patch mode

**Dry-Run**:
A read-only calculation of the exact changes required to bring a repository's live Inventory into alignment with a Policy.
_Avoid_: Plan, preview, simulation

**Setup Pull Request**:
A single draft pull request that stages all selected workflows, trusted reporter runtimes, and vendored agent skills for the repository owner to review and merge.
_Avoid_: Review run, code review pull request, direct push

**Scientific CI Profile**:
A curated continuous integration template tailored to a scientific software stack (such as Python or C++/MPI) that separates fast pull-request checks from heavier default-branch or external validation and demarcates runner hardware evidence.
_Avoid_: Generic runner, GPU proof, reproducibility guarantee

**Self-Hosted Runner**:
A maintainer-operated GitHub Actions execution machine or node targeted via `runs-on` labels, serving as the default execution target for configurable workflows to avoid hosted Actions quota consumption.
_Avoid_: Local CLI worker, personal workstation daemon, Colab runtime

**Hosted Runner**:
An ephemeral GitHub-managed virtual execution environment (`ubuntu-24.04`) used for auxiliary control jobs (`detect-changes`, `status-check`), fixed trusted-suite workflows (`issue-state-gate`, `documentation-freshness`), or as an opt-out alternative for configurable workflows.
_Avoid_: Default runner, dedicated node

**Fork Execution Guard**:
A workflow job condition restricting `pull_request` execution on a Self-Hosted Runner to same-repository branches in public repositories so untrusted fork code cannot execute on maintainer infrastructure.
_Avoid_: Fork bypass, manual approval gate

**Stable Status Check**:
A single unmatrixed gate check reported on every pull request so branch rulesets never deadlock on skipped path filters or changed matrix dimensions.
_Avoid_: Matrix job check, push-only check

**Advisory Review**:
An automated pull request review posted by the external OCR systemd runner on allowlisted repositories that provides non-blocking findings without approving pull requests or acting as a required merge check.
_Avoid_: In-repo workflow, Actions reviewer, required check

**Review Setup**:
The external VM runner and allowlist configuration for Advisory Review across allowlisted private repositories.
_Avoid_: In-repo workflow, Actions setup, merge gate setup

**Review Run**:
One execution cycle of the external Automated OCR Pipeline against an eligible pull request HEAD commit SHA.
_Avoid_: Actions run, setup session

**Documentation Freshness Audit**:
An automated check of declared Markdown links, canonical documentation presence, and pinned skill provenance that reports findings for maintainer review.
_Avoid_: Documentation auto-fixer, complete repository scan

**Managed Report Issue**:
A GitHub issue owned and updated by a single automated reporter (such as Documentation Freshness Audit) through a distinct identity marker.
_Avoid_: Shared report issue

**Issue State Gate**:
A pull request check that verifies every implementation issue declared by the pull request is closed with verified delivery evidence before merge.
_Avoid_: Triage workflow, closing keyword

**Release Candidate**:
A pull request explicitly designated by maintainers for release qualification and bound to mandatory gate verification.
_Avoid_: Release PR, version branch, candidate build

**Release Gate**:
An automated fail-closed checkpoint that evaluates release candidate identity, CI matrix checks, and runtime evidence before permitting release qualification or promotion.
_Avoid_: Merge gate, check run, deploy gate

**Control Record**:
A trusted document on the base branch declaring the expected identity, target version, and evidence criteria for an active Release Candidate.
_Avoid_: Release record, evidence file, manifest

**Runtime Fingerprint**:
A cryptographic hash of the repository's executable assets and workflow templates at a specific commit, verifying that runtime code matches declared release evidence.
_Avoid_: Code hash, build hash, version digest

**Hook Package**:
A self-contained, cryptographically verified directory declaring a hook manifest and script assets that run at specified agent lifecycle events.
_Avoid_: Hook script, hook bundle, action hook

**Opt-In Hook Contract**:
The governance policy requiring agent hooks to be disabled by default and staged or executed only when both the repository Policy and the individual Hook Package explicitly opt in.
_Avoid_: Auto-hook, default hook, implicit hook

**Doctor Diagnostic**:
An on-demand, read-only inspection of repository settings, permissions, and security controls comparing live state against declared Policy.
_Avoid_: Health check, policy auditor, auto-fixer

**Automated OCR Pipeline**:
An external systemd service running on a dedicated VM (`sciflow-cicd ocr`) that polls allowlisted repositories and publishes strictly advisory reviews bound to each pull request HEAD commit SHA without repository-staged Actions workflows.
_Avoid_: In-repo workflow, Actions reviewer, required check

**Single-Commit Cutover**:
The procedure of transferring a finalized, verified repository tree to a fresh destination repository as a single canonical root commit without historical branch clutter.
_Avoid_: Squash merge, repo clone, migration branch
