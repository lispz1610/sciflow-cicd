# Repository setup policy reference

A Policy is a JSON document with `version: 1` or `version: 2` and a nonempty `stages` array that declares the desired end-state of repository configuration. It may optionally specify a target `repository` (in `OWNER/REPO` format). Stages are opt-in, may occur once each, and run in the order written.

- **Policy version 1**: Accepted with its existing document format and standard repository configuration stages. The `files` stage now proposes an owner-reviewed Setup Pull Request instead of writing to the default branch during apply.
- **Policy version 2**: Assembled by Interactive Setup and supported in Headless Mode. Adds support for opt-in Agent Skills, Scientific CI Profiles, Hooks, and Dependabot.

The example in [`examples/setup-policy.json`](../examples/setup-policy.json) demonstrates a Policy version 1 configuration. The example in [`examples/setup-policy-v2-agent-skills.json`](../examples/setup-policy-v2-agent-skills.json) demonstrates a Policy version 2 configuration.

See the [Client contract migration](#client-contract-migration-for-the-scientific-setup-core) section below for the command, Policy
field, version, and workflow decisions in the scientific setup core.

## Applying policies

### Public client contract

`sciflow-cicd` is the client entry point. Interactive Setup (`sciflow-cicd`) gathers choices, shows Inventory and a Dry-Run, saves a version 2 Policy, and asks before apply. Headless Mode uses the same Policy planner through `sciflow-cicd dry-run --policy PATH` and `sciflow-cicd apply --policy PATH --dry-run-result DRY_RUN_JSON`; both accept `--repo OWNER/REPO` to override the Policy target. Save the Dry-Run JSON and pass it to apply. The Dry-Run returns observed `inventory`, `mode: "dry-run"`, `repository`, `policy_sha256`, and `changes`. Each proposed setting change includes its stage, scope, operation, path, current observation, desired state, reason, and `planned` status. An empty `changes` array has `status: "no-op"` when no Setup Pull Request remains pending. Dry-Run does not issue mutating GitHub requests. Security endpoints that expose an `enabled` value report the observed and desired boolean state rather than only the HTTP response code.

Headless apply returns JSON with `mode: "apply"`, `repository`, and `changes`. It checks the saved Dry-Run against the Policy and repository, recomputes the selected changes from live state, and blocks before writes if any selected preflight is blocked or the inspected change set changed. It writes only operations selected by the accepted Dry-Run and verifies direct settings afterward. Exit code `0` means no blocking or unverified change, including no-op; `1` means invalid input, authentication, or an API error that prevents execution; `2` means a reported `blocked`, `failed`, or `unverified` change. A `pending` change requires owner action or later verification and remains visible in the result. Generate and inspect a fresh Dry-Run after a stale result or before retrying an apply.

The first complete science-focused path is the repository `features` stage, including `issues`: Interactive Setup and Headless Mode produce the same planned repository setting change. Existing version 1 Policies remain accepted, as required by the integration ADR. Interactive Setup continues to write version 2. Selected authored files now follow the owner-reviewed Setup Pull Request decision. Scientific CI profiles, proportional security controls, citation, releases, skills, hooks, and worker capabilities are added through this Policy and Dry-Run boundary in their owning issues.

### Headless Mode via `sciflow-cicd`

Apply a Policy in Headless Mode using the `sciflow-cicd` CLI:

```bash
# Apply a policy file (uses target repository declared in the policy document)
sciflow-cicd dry-run --policy .local/setup-policy.json > .local/setup-dry-run.json
sciflow-cicd apply --policy .local/setup-policy.json --dry-run-result .local/setup-dry-run.json

# Explicitly specify or override the target repository
sciflow-cicd dry-run --policy examples/setup-policy.json --repo OWNER/REPO > .local/setup-dry-run.json
sciflow-cicd apply --policy examples/setup-policy.json --repo OWNER/REPO --dry-run-result .local/setup-dry-run.json
```

When you configure a repository using Interactive Setup (`sciflow-cicd`), your selections are automatically assembled and saved to `.local/setup-policy.json`. This Policy document can be reviewed, version-controlled, or reapplied repeatedly in Headless Mode.

### Direct script invocation

Alternatively, you can query repository Inventory and execute Policy commands using the underlying script directly:

```bash
python3 scripts/setup_github_repo.py --repo OWNER/REPO inventory
python3 scripts/setup_github_repo.py --repo OWNER/REPO policy --file examples/setup-policy.json
python3 scripts/setup_github_repo.py --repo OWNER/REPO policy --file examples/setup-policy.json --apply
```

The policy command reads and calculates changes for every selected stage before the first write. Dry-Run is the default when using the script. It never rolls back successful writes after a later API failure; resolve the error and rerun. A repeat apply should have an empty `changes` array. A blocked or unverified outcome exits with code 2. File sources are resolved relative to the policy file when used through this CLI.

| Stage | Required data | Effect |
| --- | --- | --- |
| `features` | `enable`: nonempty list of `issues`, `wiki`, `projects` | Enables only those repository features. |
| `actions` | None | Enables Actions, sets read-only default workflow tokens, and disables workflow PR approvals. |
| `merge` | `settings`: selected boolean keys | Sets `allow_merge_commit`, `allow_squash_merge`, `allow_rebase_merge`, `delete_branch_on_merge`, and/or `allow_update_branch`. At least one merge method must remain enabled. |
| `actions-policy` | `settings`: selected keys | Sets `allowed_actions` (`all`, `local_only`, or `selected`) and/or `sha_pinning_required`. For `selected`, supply all of `github_owned_allowed`, `verified_allowed`, and `patterns_allowed`. GitHub documents `patterns_allowed` for public repositories only. |
| `security` | `enable`: nonempty list | Enables selected `vulnerability-alerts`, `automated-security-fixes`, `secret-scanning`, `push-protection`, or `private-vulnerability-reporting` where supported. |
| `dependency-graph` | None | No change for public repositories. For private/internal repositories, enables vulnerability alerts and the dependency graph together through GitHub's documented alerts endpoint. This coupling appears in the Dry-Run. |
| `codeql` | None | Requests CodeQL default setup. Put `actions` before this stage if Actions is currently disabled. A `202` response is pending, not proof of scan completion. |
| `labels` | `items`: label objects | Reconciles only supplied `name`, six-digit `color`, and `description`. Unlisted labels are untouched. |
| `files` | `items`: file objects | Stages caller-authored Issue forms, PR templates, or Markdown under `docs/` in the owner-reviewed Setup Pull Request. Each item needs `path` and `source`; updating changed remote content also needs the current `expected_sha`. Existing changes are never silently overwritten. |
| `workflows` | `workflows`: nonempty list of workflow objects | Stages caller-selected versioned workflow templates (`pr-ci`, `profile-ci`, or `dependency-review`) in a single draft pull request on a dedicated branch (`codex/workflows-*`). Supports CI profiles (such as `python` and `cmake-mpi`) with concurrency controls (`cancel-in-progress: true`), profile-specific options, and a stable status check reporting anchor (`status-check`) that never hangs in pending when jobs, matrices, or paths are skipped. Verifies pinned action commit existence and guards against workflow drift. See [Scientific CI profiles](scientific-ci-profiles.md). |
| `ci-profile` | `profile`, options (`python` or `cmake_mpi`), optional `filename`, `triggers`, `paths`, `checkout_sha` | Stages a focused scientific CI profile (`python` or `cmake-mpi`) in the unified draft Setup Pull Request. Configures profile-specific matrix strategies, package manager caching, build/test commands, PR concurrency cancellation, and stable status check reporting. See [Scientific CI profiles](scientific-ci-profiles.md). |
| `agent-skills` | `bundle`: `mattpocock` | Policy version 2 only. Copies the repository-pinned Matt Pocock skill snapshot as inert files under `.agents/skills/` and stages them with selected workflows in the same Setup Pull Request. The target manifest records source commit and file hashes; repeat apply converges, upgrades replace only files matching the prior manifest, and diverged files block staging. |
| `review` | `workflow`, `llm`, `limits` objects | Policy version 2 only. Stages the Advisory Review workflow (`.github/workflows/open-code-review.yml`) and trusted reporter (`scripts/review_issue_reporter.js`) in the unified draft Setup Pull Request. Optional `workflow.expected_sha` and `workflow.reporter_expected_sha` acknowledge managed updates. Supports optional `llm.extra_body` for custom sampling and reasoning parameters. Never contains provider keys or tokens. Its findings reporter owns its own managed issue and PR link identity, separate from Documentation Freshness. See the [Advisory Review guide](advisory-review.md). |
| `issue-state-gate` | `name` only | Optional in Policy versions 1 and 2. Stages the trusted Issue State Gate, issue close/reopen refresh, and reconciliation workflows together in the unified draft Setup Pull Request. It then requires pull requests and the exact `Issue State Gate` check on `main`, with zero required reviews and no branch synchronization requirement, after a successful check run proves the source. Apply may report pending until the Setup Pull Request is merged and successful check evidence is observable. See [proportional security and controls](proportional-security-and-controls.md#62-optional-issue-state-gate) for recovery. |
| `gates` | `evidence_pr`: int, `reviewer`: string, `checks`: dict or list; optional `ruleset_name`: string (default `code-review`) | Creates or updates a branch ruleset protecting the default branch after verifying an open evidence PR, reviewer approval on the current PR head, active installed workflows, and successful workflow check runs pinned by GitHub App ID. Deferred when dependent on newly staged CI. |

An opt-in agent-skill Policy example is available at
[`examples/setup-policy-v2-agent-skills.json`](../examples/setup-policy-v2-agent-skills.json).
Replace `OWNER/REPO` before applying it.

Example of the two content stages:

```json
{
  "version": 1,
  "stages": [
    {"name": "labels", "items": [
      {"name": "spec", "color": "123abc", "description": "Authored specification"}
    ]},
    {"name": "files", "items": [
      {"path": ".github/ISSUE_TEMPLATE/feature.yml", "source": "./authored/feature.yml"}
    ]}
  ]
}
```

The `files` stage only accepts Issue forms (`.github/ISSUE_TEMPLATE/*`), PR templates (`.github/PULL_REQUEST_TEMPLATE*`), and authored Markdown under `docs/*.md`. Root files such as `CITATION.cff`, `.gitattributes`, or `requirements.txt` are intentionally rejected by `files` and must be committed directly to Git or onto the Setup Pull Request branch. The `files` stage uses GitHub's blob SHA as a compare-and-swap guard. To update a file after it changes remotely, read its current blob SHA and put it in `expected_sha`. The Dry-Run shows a SHA-256 digest of file bytes rather than the file body. Apply stages the selected files in a draft Setup Pull Request; the owner merges it to complete installation. Secrets must never be placed in a policy, template, comment, or authored document.

### Scientific CI profile configuration

Scientific repositories can declare the `profile-ci` template in the `workflows` stage or through a dedicated `ci-profile` stage. Supported profiles are:

- **`python`**: Focuses on scientific Python environments. Supports multi-version matrix execution (`versions`: e.g. `["3.11", "3.12"]`), dependency caching (`package_manager: "pip"` with `actions/setup-python` cache, or `package_manager: "uv"` with `astral-sh/setup-uv` cache; set `"cache": false` when the repository has no root `requirements.txt` or `pyproject.toml`), Git LFS data retrieval (`lfs: true`), and test/lint commands.
- **`cmake-mpi`**: Focuses on compiled CMake/MPI scientific codes. Supports bounded MPI rank smoke checks (`ranks`: e.g. `[1, 2]`), compiler caching (`ccache: true`), system runner dependencies (`runner_dependencies`: e.g. `["libopenmpi-dev", "openmpi-bin"]`), and CPU OpenCL emulation (`opencl: true` via POCL).

Every profile workflow includes:
- **Concurrency control**: `cancel-in-progress: true` on PR branches to conserve runner capacity.
- **Stable status check anchor**: An unmatrixed `status-check` terminal job running `if: always()` that inspects all upstream job results, ensuring branch rulesets never hang when paths or matrix jobs are skipped.
- **Explicit evidence boundaries**: Workflows clearly document and emit annotations that virtual CPU runs (including POCL OpenCL and multi-rank OpenMPI) verify host compilation only and do not prove execution on real GPUs, accelerators, or multi-node physical clusters. Heavy simulations and GPU benchmarks must be delegated to external compute environments (e.g. Google Colab).

See the [Scientific CI profiles guide](scientific-ci-profiles.md) for full configuration schemas, operational boundaries, and delegation workflows.

### Opt-in hook package contract and validator

Agent hooks extend agent lifecycle events with custom script execution. To guarantee repository security, hook packages are governed by an **Opt-In Hook Contract** implemented in [`scripts/hook_contracts.py`](../scripts/hook_contracts.py):

- **Safe defaults**: Hooks are never invented, never enabled by default, and have no active execution path when no hooks are supplied (`None` or empty sequence).
- **Package schema**: A valid hook package contains a manifest (`hook.json` or `manifest.json`) declaring:
  - `schema_version`: Must be integer `1`.
  - `name`: Alphanumeric/hyphen string (`[a-z0-9][a-z0-9-]*`).
  - `description`: Non-empty description string.
  - `event`: Lifecycle event (`pre-tool-call`, `post-tool-call`, `session-start`, or `session-stop`).
  - `runtime`: Supported script runner (`bash` or `python3`).
  - `entrypoint`: Canonical relative POSIX path to the package script or executable.
  - `files`: Non-empty mapping of relative file paths to their 64-character lowercase hexadecimal SHA-256 digests.
  - `permissions`: List of required permission identifiers or empty list (`[]`).
  - `opt_in`: Explicit boolean (`true` or `false`). Hooks with `opt_in: false` remain in packaged (dormant) state and are omitted from active execution.
- **Strict safety validation**: The validator rejects invalid packages before any filesystem write or staging operation:
  - Manifest schema validation (all required fields, no unrecognized fields, valid events and runtimes).
  - Path traversal defense: Prohibits absolute paths, parent traversal (`..`), empty segments, backslashes, null bytes, and non-canonical paths in entrypoints and file maps.
  - Symlink prohibition: Any symlink within the package directory, manifest file, or target components is strictly rejected.
  - Integrity verification: Every file on disk is checked against its expected SHA-256 digest. Content mismatches, missing declared files, or unindexed files in the package directory fail validation.
  - Ambiguity guards: Multiple manifests (both `hook.json` and `manifest.json`) or duplicate hook names across packages are rejected.
- **Setup Pull Request staging**: In Policy v2, validated hook packages are staged via the `hooks` stage:
  ```json
  {
    "name": "hooks",
    "opt_in": true,
    "packages": ["path/to/hook-package"]
  }
  ```
  When `opt_in` is `true`, valid packages are staged under `.agents/hooks/<name>/...` within the unified draft Setup Pull Request. Omitted or opted-out hooks remain unwritten. Preflight target conflict detection prevents overwriting local modifications if existing hook files diverged from prior manifests.
- **Double opt-in semantics**: Staging a hook package requires agreement between both the Policy configuration and the individual hook package manifest:
  1. The Policy `hooks` stage must declare `opt_in: true`.
  2. The individual hook package manifest must also declare `opt_in: true`.
  If the Policy `hooks` stage opts in (`opt_in: true`), but an individual package manifest declares `opt_in: false`, the package manifest acts as a veto: the package remains dormant, zero files are staged from it, and Setup emits a visible warning in the dry-run output naming the ignored package (e.g. `hooks: package '<name>' declares opt_in=false in its manifest and was ignored; set opt_in=true in manifest.json to stage it`).
  If the Policy `hooks` stage declares `opt_in: false` (or is omitted), no hook packages are staged regardless of package manifest values, and no veto warnings are generated.


Managed Issue and PR link comments use an independent optional command. Its
input is a JSON array of entries with `issue`, `key`, and a nonempty `links`
array; each link has a `label` and an HTTPS `url`. Edit
[`examples/links.json`](../examples/links.json) is a placeholder for manual
use. Replace every example issue number and URL before running it; never apply
the example as checked in. The documentation link workflow reads only the
optional repository-specific `.github/managed-links.json`. Add that file with
real issue or pull request numbers and repository URLs to enable automatic
reconciliation on pushes to `main`. If it is absent, the workflow reports a
successful skip.

Run the command without `--apply` to inspect a Dry-Run result. Apply only a
configuration written for the intended repository:

```bash
python3 scripts/reconcile_github_links.py --repo OWNER/REPO --file examples/links.json
python3 scripts/reconcile_github_links.py --repo OWNER/REPO --file examples/links.json --apply
```

Dry-Run is the default. The command updates only comments owned by the
authenticated actor with the matching managed marker; unrelated comments
remain untouched. Ambiguous duplicate managed comments block the operation.
The output shows a body digest instead of comment text. A repeated apply
has an empty `changes` array; an unverified apply exits with code 2. A
`links` stage in the core policy is rejected before any API write.

To configure automatic reconciliation, copy the JSON array format into
`.github/managed-links.json` and replace its sample issue number and URL with
actual values. On `main`, the workflow applies this managed configuration with
the repository's `GITHUB_TOKEN`; pull request runs validate Markdown links but
do not write comments.

Use `scripts/publish_github_wiki.py` separately for caller-authored Wiki pages. The GitHub Wiki Git repository must have its initial page before it can be cloned. GitHub documents first-page creation and collaborator-only editing in its UI; the repository REST API does not expose a supported field for the latter. The inventory reports these as unknown or unsupported, and the publisher fails clearly when the first page is absent. The tools do not pretend to have verified either condition.

When GitHub returns HTTP 202 and a `validation_run_id`, setup remains
`pending`; the command has not verified a completed scan. Inspect
[GitHub's code scanning tool status](https://docs.github.com/en/code-security/how-tos/find-and-fix-code-vulnerabilities/manage-your-configuration/use-the-tools-status-page-for-code-scanning)
for scan health, errors, languages, and file coverage. The inventory command
reports the observable default-setup configuration and `unknown` when it
cannot read that configuration.

## API and permission notes

- Repository features, Actions, merge settings, CodeQL default setup, and most security settings require repository Administration permissions.
- Labels and issue/PR link comments require Issues or Pull requests permissions as described by their endpoints. Authored repository files require Contents write permission. Use the least permissions necessary for the selected stages.
- Account plans and organization policy can block individual stages. A successful write response is followed by a read-based convergence check; an unchanged setting remains `unverified`.
- Wiki publication uses Git credentials for the separate Wiki repository. The API token used by the policy CLI does not initialize a Wiki.

References: [repository settings](https://docs.github.com/en/rest/repos/repos), [Actions permissions](https://docs.github.com/en/rest/actions/permissions), [CodeQL API](https://docs.github.com/en/rest/code-scanning/code-scanning), [labels](https://docs.github.com/en/rest/issues/labels), [repository files](https://docs.github.com/en/rest/repos/contents), [issue comments](https://docs.github.com/en/rest/issues/comments), [Wiki setup](https://docs.github.com/en/communities/documenting-your-project-with-wikis/adding-or-editing-wiki-pages), and [Wiki permissions](https://docs.github.com/en/communities/documenting-your-project-with-wikis/changing-access-permissions-for-wikis).

---

## Client contract migration for the scientific setup core

The supported repository setup entry point is `sciflow-cicd`. Run it without a
subcommand for Interactive Setup. Use `sciflow-cicd dry-run --policy PATH` to inspect
Inventory and every selected change, save its JSON output, then run
`sciflow-cicd apply --policy PATH --dry-run-result DRY_RUN_JSON` to apply the Policy. Both Headless Mode commands accept `--repo OWNER/REPO` to
override the Policy target. Interactive Setup writes a version 2 Policy.

### Commands and workflows

| Earlier entry point or workflow | Current path |
| --- | --- |
| `sciflow-cicd` Interactive Setup | Retained. Its selections use the same Policy and Dry-Run core as Headless Mode. |
| `sciflow-cicd apply --policy PATH` | Now requires `--dry-run-result DRY_RUN_JSON`, a saved `sciflow-cicd dry-run` result for the same Policy and repository. The former `--plan` spelling remains a hidden compatibility alias. Apply blocks before writes if the inspected change set is stale or any selected preflight is blocked. Generate a fresh Dry-Run for each retry. |
| `sciflow-cicd verify-release` | Retained for Release Candidate evidence; it does not perform repository setup. |
| `scripts/setup_github_repo.py ... inventory` and `... policy --file PATH [--apply]` | Retained as direct compatibility commands. Direct Policy apply now requires `--dry-run-result DRY_RUN_JSON` from its read-only Policy command or `sciflow-cicd dry-run`, and uses the same Headless Mode validation and stale-state guard. The former `--plan` spelling remains a hidden compatibility alias. New setup instructions use `sciflow-cicd` for Interactive Setup and Headless Mode. |
| `scripts/configure_github_repo.py` resource commands | Retained for a single resource and for the read-only policy drift audit. They do not replace the complete `sciflow-cicd` Dry-Run. |
| `scripts/install_github_workflows.py` | Retained for standalone workflow installation. For a combined Policy, select `workflows` so selected files share one Setup Pull Request. |
| `scripts/setup_repository_content.py`, `scripts/setup_github_projects.py`, and `scripts/publish_github_wiki.py` | Retained for their standalone resource workflows. Projects V2 and Wiki page publication are outside the P0 Policy core. |
| `scripts/reconcile_github_links.py` | Retained as the independent managed Issue and pull request links command. The `links` Policy stage is rejected before GitHub API access and directs users here. |
| Policy drift, Wiki, release promotion, link check, and issue triage workflows | Retained with their documented triggers, credentials, permissions, and recovery procedures. This migration does not change those workflows. |

### Policy versions and fields

Policy versions 1 and 2 remain accepted, including the optional `repository`
target and nonempty `stages` array. Version 2 adds `review` and `agent-skills`.
The existing version 1 Policy schema is retained under the integration ADR;
version 1 Policies do not need a version bump for repository settings.

| Stage or field | Migration decision |
| --- | --- |
| `features.enable`, `actions`, `merge.settings`, `actions-policy.settings`, `security.enable`, `dependency-graph`, and `codeql` | Retained. Each selected setting is visible in Dry-Run and uses the common apply path. |
| `labels.items` and `gates` fields (`evidence_pr`, `reviewer`, `checks`, optional `ruleset_name`) | Retained. Existing stage validation and resource prerequisites still apply. |
| `files.items` with `path`, `source`, and optional `expected_sha` | Retained in both Policy versions. File content is proposed in the owner-reviewed Setup Pull Request instead of being written to the default branch during apply. Review and merge the pull request to complete installation. The expected SHA continues to guard updates. |
| `workflows.workflows` | Retained. Selected workflows join the same Setup Pull Request as selected authored files and other managed assets. |
| Version 2 `review.workflow`, `review.llm`, `review.limits`, and `agent-skills.bundle` | Retained. `review` stays private-repository only; `agent-skills.bundle` accepts `mattpocock`. |
| `issue-state-gate.name` | Retained. The gate can remain pending until its Setup Pull Request is merged and a successful check exists. |
| `links` stage | Unsupported. Use `scripts/reconcile_github_links.py` with a separate managed links document. |
| Any unknown stage, unknown stage key, or unsupported Policy version | Rejected as invalid Policy before any GitHub write. Do not silently discard fields. |

The target contract remains open for science-specific CI, security, citation,
release, skill, and worker capabilities in their owning issues. Those future
stages are not accepted by P0 until implemented and documented. A `pending`
result represents owner action or later verification, not completed setup.

The earlier direct-file apply behavior changes delivery, but not the Policy v1
document format. This distinction is recorded in the architecture decision
for the unified Setup Pull Request.
