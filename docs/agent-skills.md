# Vendored agent skill snapshot and Opt-In Hook Contract

The repository carries an inert, source-pinned snapshot of the supported Matt Pocock skills from [`mattpocock/skills`](https://github.com/mattpocock/skills). The reviewed source is pinned in [`data/agent-skills/mattpocock/manifest.json`](../data/agent-skills/mattpocock/manifest.json), recording the exact upstream commit (`49dd158d1076134a641b33efb035946536778336`), published plugin version (`1.3.1`), upstream plugin metadata ([`data/agent-skills/mattpocock/source-plugin.json`](../data/agent-skills/mattpocock/source-plugin.json)), MIT license ([`data/agent-skills/mattpocock/LICENSE`](../data/agent-skills/mattpocock/LICENSE)), and SHA-256 digest for every copied file. Skill text and helper files are strictly inert repository data; `sciflow-cicd` and GitHub Actions never execute or import them.

**Interactive Setup** and **Policy** version 2 (`{"name": "agent-skills", "bundle": "mattpocock"}`) can optionally select the `mattpocock` bundle. `sciflow-cicd` stages the snapshot under the target repository's `.agents/skills/` directory in the single draft **Setup Pull Request** (`codex/workflows-<repo>`, per [ADR 0002](adr/0002-one-setup-pull-request-for-selected-workflows.md)) alongside any selected workflows and the companion **Documentation Freshness Audit** suite. Installing the skill catalog stages **zero** active hooks: under the **Opt-In Hook Contract** ([`scripts/hook_contracts.py`](../scripts/hook_contracts.py)), no real maintainer hook is enabled or staged until a cryptographically validated **Hook Package** is explicitly supplied and opted in (`opt_in: true` in both the Policy `hooks` stage and the package's `hook.json` or `manifest.json`).

---

## Pinned catalog provenance, attribution, and staged layout

Maintainers can inspect catalog provenance and attribution both in the `sciflow-cicd` source tree and in any target repository where the bundle has been staged or merged:

| Provenance Field / Artifact | Source Catalog (`data/agent-skills/mattpocock/`) | Target Repository (`.agents/skills/` & `.github/`) | Value / Contract |
| --- | --- | --- | --- |
| `schema_version` | `data/agent-skills/mattpocock/manifest.json` | `.agents/skills/manifest.json` | `1` |
| `managed_by` / `bundle` | Implied by catalog directory | `.agents/skills/manifest.json` | `"managed_by": "sciflow-cicd"`, `"bundle": "mattpocock"` |
| `source_repository` | `data/agent-skills/mattpocock/manifest.json` | `.agents/skills/manifest.json` | `"https://github.com/mattpocock/skills"` |
| `source_commit` | `data/agent-skills/mattpocock/manifest.json` | `.agents/skills/manifest.json` | `"49dd158d1076134a641b33efb035946536778336"` |
| `source_version` | `data/agent-skills/mattpocock/manifest.json` | `.agents/skills/manifest.json` | `"1.3.1"` |
| `license` & Attribution | `data/agent-skills/mattpocock/LICENSE` | `.agents/skills/LICENSE` | `"MIT"` (`Copyright (c) 2026 Matt Pocock`, verified non-empty MIT license text) |
| Supported skills (`27` total) | `supported_skills` in `manifest.json` | `.agents/skills/<category>/<skill>/SKILL.md` | `20` `engineering/*` skills + `7` `productivity/*` skills = **`27` skills** (`73` Markdown/script/YAML files + `6` additional skill files = **`79` skill files** in `snapshot/skills/**`) |
| Target manifest `files` map | `files` list (`79` entries) | `files` dict (`80` entries) | Maps `"LICENSE"` + all `79` relative skill file paths (`80` tracked entries) to 64-char lowercase hex SHA-256 digests (`81` total files staged under `.agents/skills/` including `.agents/skills/manifest.json`) |
| Companion **Documentation Freshness Audit** | `scripts/install_github_workflows.py` (`render_documentation_suite`) | Staged in the same draft **Setup Pull Request** | `.github/workflows/documentation-freshness.yml` (`DOCS_TARGET_MODE: "true"`), `.github/documentation-freshness.json`, `.github/documentation-skill-inventory.json`, `docs/documentation-freshness.md`, `scripts/documentation_freshness.py`, `scripts/documentation_audit_runner.py`, `scripts/documentation_issue_reporter.py`, and `scripts/check_markdown_links.py` ([guide](documentation-freshness.md)) |

### Supported 27-skill inventory

- **`engineering/*` (`20` skills)**: `engineering/ask-matt`, `engineering/code-review`, `engineering/codebase-design`, `engineering/diagnosing-bugs`, `engineering/domain-modeling`, `engineering/grill-with-docs`, `engineering/implement`, `engineering/implement-spec`, `engineering/improve-codebase-architecture`, `engineering/pr`, `engineering/prototype`, `engineering/research`, `engineering/retro`, `engineering/setup-matt-pocock-skills`, `engineering/tdd`, `engineering/to-spec`, `engineering/to-tickets`, `engineering/triage`, `engineering/wayfinder`, `engineering/wizard`.
- **`productivity/*` (`7` skills)**: `productivity/grill-me`, `productivity/grilling`, `productivity/handoff`, `productivity/teach`, `productivity/to-questionnaire`, `productivity/wait-what`, `productivity/writing-for-agents`.

---

## Verified lifecycle behavior

The installation lifecycle ([`scripts/provision_agent_skills.py`](../scripts/provision_agent_skills.py), [`scripts/agent_skill_upgrade.py`](../scripts/agent_skill_upgrade.py), [`scripts/install_github_workflows.py`](../scripts/install_github_workflows.py)) enforces strict idempotency, safe atomic upgrades, explicit removal acknowledgment, and zero-partial-write protection across skills and workflow bundles:

1. **Initial installation**:
   Stages all `27` verified skills (`79` skill files) from the reviewed catalog snapshot into `.agents/skills/`, alongside `.agents/skills/LICENSE` (`80` hash-tracked entries) and generated `.agents/skills/manifest.json` (`81` total files under `.agents/skills/`). When combined with workflow stages (`pr-ci`, `profile-ci`, `dependency-review`, `issue-state-gate`, `review`, `dependabot`, or `hooks`), all workflows, runtime scripts, companion **Documentation Freshness Audit** files (`8` files), and skill files are staged together in one guarded draft **Setup Pull Request** on branch `codex/workflows-<digest>`.

2. **Repeat installation (idempotency)**:
   - When merged to the default branch, reapplying the same Policy or running `provision_skills` produces a convergent `noop` (`changes: []`, `status: "verified"`, `0` writes), creating no branch, no commit, and no new pull request. File iteration order does not affect convergence.
   - When an open draft **Setup Pull Request** is already pending review with the identical files staged, reapplying returns `status: "pending_review"` with the existing pull request URL and performs `0` writes.

3. **Controlled catalog upgrade, removals, and renames (`classify_catalog_upgrade`)**:
   - When the pinned snapshot advances to a new upstream commit or version, [`classify_catalog_upgrade`](../scripts/agent_skill_upgrade.py) compares the installed `.agents/skills/manifest.json` against the new target manifest and classifies `added_skills`, `removed_skills`, `renamed_skills` (where a removed skill's relative filename-to-SHA-256 map matches an added skill's map), `added_files`, `removed_files`, `changed_files`, `unchanged`, and `revision_changed`.
   - `sciflow-cicd` surfaces the revision transition in `agent_skill_upgrade` (`from` and `to` `source_commit` / `source_version`, `removed_skills`, and `renamed_skills`).
   - **Default removal block**: If a catalog upgrade drops or renames any installed skill, `validate_removal_acknowledgment` blocks the **entire** upgrade before any write unless `allow_removed_skills` (in the Policy `agent-skills` stage) or `allow_removals` (in `provision_skills`) lists **exact** dropped skill IDs (for example `{"name": "agent-skills", "bundle": "mattpocock", "allow_removed_skills": ["engineering/tdd"]}`).
   - **Atomic rollback (`_rollback_destination`)**: During local upgrades and removals in `provision_skills`, every mutated or deleted file is backed up prior to writes. If any file write or removal `unlink` fails mid-operation, `_rollback_destination` restores all modified files, unlinks newly created files, and recreates pruned directories so the destination never remains half-upgraded.

4. **Local-edit conflict detection & atomicity**:
   - If any target skill file has diverged from its prior `.agents/skills/manifest.json` (modified locally by the repository owner, unmanaged, or deleted while `manifest.json` remains), the installer stops the **entire** workflow-and-skill bundle before any write occurs.
   - In local provisioning (`provision_skills`), it raises `ValueError("destination has local changes: <path> (installed sha256 <prior>, local sha256 <actual>, proposed sha256 <expected>)")`.
   - In remote staging (`install_github_workflows.py`), it raises `ValueError("agent skill conflict: existing file diverged from its manifest: <path>")` (or marks the stage `status: "blocked"` in `run_policy` Dry-Run) with `0` API writes, preserving the owner's edits untouched. Even when a skill is listed in `allow_removed_skills`, a locally modified file inside that skill is never deleted.

5. **Conflict resolution**:
   - Inspect the reported path and preserve desired local customizations in repository documentation or custom paths outside `.agents/skills/`.
   - Revert the managed file under `.agents/skills/` back to the hash recorded in `.agents/skills/manifest.json` to allow the clean upgrade to proceed, or remove the entire `.agents/skills/` directory if performing a fresh reinstall.
   - Do not delete individual conflicting skill files while retaining `.agents/skills/manifest.json`; the prior-manifest check detects missing tracked files as local changes and blocks staging.

---

## Readiness and compatibility reporting

Before staging, `run_policy` evaluates two read-only diagnostic seams and attaches non-blocking warnings when setup prerequisites are incomplete:

1. **Repository convention readiness ([`scripts/agent_skills_readiness.py`](../scripts/agent_skills_readiness.py))**:
   `evaluate_agent_skills_readiness` checks three convention prerequisites expected by the Matt Pocock skills:
   - `issue-tracker`: `docs/agents/issue-tracker.md`
   - `triage-labels`: `docs/agents/triage-labels.md`
   - `domain-docs`: `docs/agents/domain.md` and `GLOSSARY.md`
   Staging `.agents/skills/` never auto-creates domain documents; when any prerequisite is missing, `sciflow-cicd` advises running `/setup-matt-pocock-skills` after merging the Setup Pull Request.

2. **Per-skill compatibility audit ([`scripts/agent_skills_compatibility.py`](../scripts/agent_skills_compatibility.py))**:
   `audit_agent_skills_compatibility` inspects the staged skill texts for backticked CLI commands (`gh`, `glab`, `git`), literal convention paths (`docs/agents/issue-tracker.md`, `docs/agents/triage-labels.md`, `docs/agents/domain.md`, `GLOSSARY.md`, `docs/adr`), and backticked sibling skill references (`` `/skill-name` ``), classifying each of the `27` skills as `ready`, `requires-manual-setup`, or `incompatible`.

## After the Setup Pull Request is merged

Confirm the merged `.agents/skills/manifest.json` records the expected upstream commit (`49dd158d1076134a641b33efb035946536778336`), version (`1.3.1`), license (`MIT`), and `80` file hashes. Derive installed skill names from its `files` paths ending in `SKILL.md` (`27` skills) and compare them with `data/agent-skills/mattpocock/manifest.json`.

If the target repository's `.gitignore` contains a blanket `/.agents/` rule, local tools and manual commits will ignore `.agents/skills/`. Replace blanket ignores with `/.agents/*` followed by `!/.agents/skills/` so the skill bundle stays tracked while ephemeral local agent state remains ignored. When staging `agent-skills` on a repository that already carries a managed `.github/workflows/documentation-freshness.yml`, supply `"expected_sha": "<current_blob_sha>"` in the Policy `agent-skills` stage to acknowledge the companion workflow update.

If the same upstream plugin is also installed globally for an agent, avoid copying duplicate same-named skills into the repository: duplicate definitions can make that agent load competing versions. Choose one route per agent—either its global plugin or the repository-local `.agents/skills/` snapshot.

---

## Agent runtime skill discovery

Agent runtimes (such as Antigravity, Claude Code, or Gemini CLI) discover project-specific workflows by inspecting workspace skill directories. Skills installed by `sciflow-cicd` under `.agents/skills/` satisfy standard workspace discovery contracts:

1. **Discovery location**: Skills reside under `.agents/skills/<skill-name>/` or categorized directories `.agents/skills/<category>/<skill-name>/`.
2. **Entrypoint integrity**: Each skill directory contains a regular, non-empty `SKILL.md` entrypoint encoded in valid UTF-8 (with zero symlinks in the path or ancestors).
3. **YAML frontmatter contract**: Every `SKILL.md` begins with YAML frontmatter delimited by `---` containing:
   - `name`: A unique lowercase, hyphenated identifier matching `^[a-z0-9][a-z0-9-]*$`.
   - `description`: A non-empty string explaining what the skill accomplishes and when an agent should activate it.
4. **Progressive disclosure**: Agent runtimes index skill `name` and `description` frontmatter into the model's available tools or slash commands without loading the full instruction body into context until the skill is activated.

### Verification command

The discovery verifier [`scripts/verify_agent_skill_discovery.py`](../scripts/verify_agent_skill_discovery.py) proves that installed skills meet runtime discovery contracts:

```bash
# Verify skill discovery in a disposable temporary target directory
python3 scripts/verify_agent_skill_discovery.py

# Verify skill discovery in an existing target directory
python3 scripts/verify_agent_skill_discovery.py --target-dir /path/to/target

# Output machine-readable JSON summary
python3 scripts/verify_agent_skill_discovery.py --format json
```

When run without `--target-dir`, the script creates an isolated disposable temporary directory, provisions the full catalog snapshot with target manifests via `provision_skills(..., target_manifest=True)`, audits every `SKILL.md` entrypoint against UTF-8 and YAML frontmatter contracts, checks 27/27 parity against `data/agent-skills/mattpocock/manifest.json`, probes `PATH` for candidate agent CLI binaries (`antigravity`, `agy`, `claude`, `gemini`), and tears down the disposable workspace.

---

## Opt-In Hook Contract (`scripts/hook_contracts.py`)

Agent lifecycle hooks provide event-driven scripts around tool calls and sessions, but carry execution risk if enabled implicitly. Under the repository's **Opt-In Hook Contract** ([`scripts/hook_contracts.py`](../scripts/hook_contracts.py) and [`scripts/install_github_workflows.py`](../scripts/install_github_workflows.py)):

1. **Disabled by default (zero implicit hooks)**:
   - Installing the Matt Pocock `agent-skills` catalog installs **zero** hooks.
   - Default Policy documents contain no `hooks` stage.
   - Calling `validate_hook_packages(None)`, `filter_enabled_hooks(None)`, or `resolve_active_hooks(None)` returns an empty tuple `()`, and `render_hook_packages(None, opt_in=True)` returns `{}`.

2. **Double opt-in requirement & manifest veto**:
   - A **Hook Package** is staged under `.agents/hooks/<name>/` only when **both** the Policy stage (`{"name": "hooks", "opt_in": true, "packages": [...]}` in Policy version 2) **and** the package's own manifest (`hook.json` or `manifest.json`) explicitly set `"opt_in": true`.
   - If the Policy `hooks` stage sets `"opt_in": false`, zero hook files are staged.
   - If the Policy `hooks` stage sets `"opt_in": true` but a supplied package manifest declares `"opt_in": false`, the package manifest acts as a **veto**: zero files from that package are staged, and `run_policy` emits an explicit Dry-Run warning:
     `hooks: package '<name>' declares opt_in=false in its manifest and was ignored; set opt_in=true in <manifest> to stage it`.

3. **Hook Package schema (`MANIFEST_REQUIRED_FIELDS`)**:
   Every **Hook Package** directory must contain exactly one manifest file named `hook.json` or `manifest.json` (having both raises `HookValidationError: Ambiguous hook package manifests`) with exactly nine required fields:
   - `schema_version`: integer `1`
   - `name`: lowercase alphanumeric/hyphen identifier matching `^[a-z0-9][a-z0-9-]*$`
   - `description`: non-empty string
   - `event`: one of `["post-tool-call", "pre-tool-call", "session-start", "session-stop"]`
   - `runtime`: one of `["bash", "python3"]`
   - `entrypoint`: safe relative POSIX path that must also be listed in `files`
   - `files`: non-empty mapping of every relative file path in the package (excluding the manifest itself) to its 64-character lowercase hexadecimal SHA-256 digest
   - `permissions`: list of non-empty permission strings (or `[]`)
   - `opt_in`: explicit boolean (`true` or `false`)

4. **Fail-closed pre-staging validation (`HookValidationError`)**:
   Before any branch ref or file is written to the **Setup Pull Request**, `validate_hook_package` and `validate_hook_packages` fail closed with `HookValidationError` on:
   - Missing or extra manifest fields, non-boolean `opt_in`, invalid `schema_version`, unsupported `event` or `runtime`, or invalid `name`.
   - Any symlink on the package path, ancestor directories, manifest, or internal files/directories (`Symlinks are prohibited in hook packages`).
   - Path traversal (`..`, leading `/`, backslashes, or null bytes) in `entrypoint` or `files`.
   - SHA-256 content hash mismatch or missing declared file (`Content hash mismatch for <file>`).
   - Unindexed files present in the package directory on disk (`Unindexed file found in hook package: <file>`).
   - Duplicate hook `name` values across supplied packages (`Duplicate hook name '<name>' in package sequence`).
   - Local target conflicts in `.agents/hooks/<name>/` (unmanaged files or files whose content diverged from the previously installed hook manifest).

---

## Disposable-target verification evidence (Ticket `#186`)

In accordance with the repository's Evidence and Claims standard, the following disposable-target verification exercises were executed and recorded in this workspace:

### Observed

1. **Disposable-target skill discovery & manifest parity**:
   - **Command**: `python3 scripts/verify_agent_skill_discovery.py`
   - **Input**: Default pinned catalog (`data/agent-skills/mattpocock/manifest.json` and `data/agent-skills/mattpocock/snapshot/`) provisioned into an isolated disposable temporary directory.
   - **Environment**: Python 3.12.3 on `linux` (`x86_64`).
   - **Exit status**: `0` (`Static audit result: clean`).
   - **Output**:
     - Provisioned `81` files under `.agents/skills/` (`79` skill files across `20` `engineering/*` and `7` `productivity/*` skills, plus `.agents/skills/LICENSE` and `.agents/skills/manifest.json` tracking `80` SHA-256 file entries).
     - Discovered `27` skill entrypoints matching `SKILL.md`; all `27` validated for UTF-8 encoding, non-empty content, and YAML frontmatter (`name` and `description`).
     - Catalog manifest parity: `Verified` (`27/27` expected skills matched discovered skills with `0` missing and `0` unexpected).
     - Live agent runtime CLI probe in container `PATH`: `Unavailable` (no standalone `antigravity`, `agy`, `claude`, or `gemini` binary installed in container `PATH`).
   - **Current Antigravity session workspace skill discovery**:
     - Within this active Antigravity agent session, the host harness natively indexes and exposes the workspace skill catalog (including `/tdd`, `/domain-modeling`, `/codebase-design`, `/code-review`, `/diagnosing-bugs`, `/grilling`, `/pr`, `/prototype`, `/research`, `/wizard`, and `/writing-for-agents`), and `discover_workspace_skills("data/agent-skills/mattpocock/snapshot")` verified `27/27` skills with `0` errors.

2. **Disposable-target lifecycle, upgrade/removal, conflict preservation, and Opt-In Hook Contract suite**:
   - **Command**: `python3 -m unittest tests/test_p6_186_skills_hooks_guidance.py tests/test_provision_agent_skills.py tests/test_skill_lifecycle.py tests/test_agent_skill_discovery.py tests/test_hook_contracts.py tests/test_optin_hook_staging.py -v`
   - **Input**: Disposable temporary directories (`tempfile.TemporaryDirectory`) and simulated GitHub Contents/Pulls API targets (`WorkflowApi`).
   - **Environment**: Python 3.12.3 on `linux` (`x86_64`).
   - **Exit status**: `0`.
   - **Output**:
     - **Initial & repeat installation**: Confirmed `provision_skills(..., target_manifest=True)` writes `81` files on initial install and converges on repeat install; confirmed `install_selected` opens one draft **Setup Pull Request** (`codex/workflows-<digest>`) bundling `.github/workflows/ci.yml`, `.github/workflows/dependency-review.yml`, `.agents/skills/**`, and the 8-file **Documentation Freshness Audit** suite, and performs `0` writes (`status: "verified"`, `changes: []`) on repeat apply after merge.
     - **Catalog upgrade, removal acknowledgment, & atomic rollback**: Confirmed `classify_catalog_upgrade` detects removed and renamed skills; unacknowledged removals raise `ValueError("agent skill upgrade requires an explicit removal decision: ...")` with `0` writes; exact `allow_removed_skills` / `allow_removals` stages deletions of unmodified prior files; and simulated mid-upgrade write/unlink failures trigger `_rollback_destination` to restore the pre-upgrade state cleanly.
     - **Local-edit conflict detection**: Confirmed modifying an installed skill file under `.agents/skills/` raises `ValueError("destination has local changes: ... (installed sha256 ..., local sha256 ..., proposed sha256 ...)")` locally and `ValueError("agent skill conflict: existing file diverged from its manifest: ...")` remotely before any file or branch is written, preserving the maintainer's local edit.
     - **Opt-In Hook Contract**: Confirmed `validate_hook_packages(None) == ()` and `resolve_active_hooks(None) == ()`; confirmed `opt_in: false` in a hook manifest vetoes staging with a Dry-Run warning and `0` staged hook files; and confirmed invalid hook packages (missing fields, symlinks, path traversal, SHA-256 mismatch, unindexed files, ambiguous `hook.json` + `manifest.json`, and duplicate hook names) raise `HookValidationError` with `0` API writes.

### Inferred

- **Reasoning**: Because every staged `SKILL.md` file is verified for UTF-8 encoding, non-symlink regular-file status, YAML frontmatter syntax, and SHA-256 parity against `data/agent-skills/mattpocock/manifest.json` (`49dd158d1076134a641b33efb035946536778336`, `v1.3.1`, MIT), compliant agent runtimes that scan `.agents/skills/**/SKILL.md` will index all `27` skills without schema errors, while no lifecycle hook can execute unless both the repository Policy and the cryptographically verified **Hook Package** manifest set `opt_in: true`.
- **Uncertainty**: Runtime progressive-disclosure token budgets, model-specific skill activation heuristics, and external CLI subcommand flags (`<bin> skills list`) depend on the host agent runtime version.

### Unverified

- **Live standalone CLI binary invocation (`<bin> skills list`) in the test container**: `Unverified` because no standalone `antigravity`, `agy`, `claude`, or `gemini` CLI binary is installed in the container's `PATH`.
- **Missing check & required environment**: Running `python3 scripts/verify_agent_skill_discovery.py --runtime-bin <path-to-agent-cli>` on a workstation with a standalone agent CLI binary in `PATH` that supports the `skills list` subcommand.

---

## Intentional snapshot update

Update the vendored snapshot in `data/agent-skills/mattpocock/` only when deliberately changing the supported skill set or refreshing its upstream content:

1. Review the desired immutable commit in `mattpocock/skills`; do not use a moving branch as the recorded source. Read its `.claude-plugin/plugin.json` and `LICENSE` at that exact commit.
2. Copy every file from each path in the plugin's `skills` list into `data/agent-skills/mattpocock/snapshot/skills/`, preserving its relative path. Remove files for skills no longer in the supported plugin list.
3. Update `source_commit`, `source_version`, `supported_skills`, and every file's source path, destination path, and SHA-256 in `data/agent-skills/mattpocock/manifest.json` (and update `DOCUMENTATION_INVENTORY_SHA256` in `scripts/install_github_workflows.py` if the catalog manifest digest changes). Refresh `source-plugin.json` and `LICENSE` from the same commit.
4. Inspect the diff for unexpected instructions, helper executables, or unrelated upstream material. The provisioner copies files as inert data and rejects symlinks, unsafe paths, duplicate destinations, missing files, unindexed files, and hash mismatches before writing any destination.
5. Run `python3 -m unittest discover -s tests -p 'test_*.py'`, `python3 scripts/verify_agent_skill_discovery.py`, `python3 scripts/documentation_freshness.py`, and `python3 scripts/check_markdown_links.py`. Confirm the inventory matches the upstream plugin list and the MIT license remains present.

Do not hand-edit a vendored file without making a deliberate upstream refresh and updating its hash. Local policy or operating instructions belong in repository documentation, not as silent edits to the upstream snapshot.
