# Client contract migration for the scientific setup core

The supported repository setup entry point is `gh-setup`. Run it without a
subcommand for Interactive Setup. Use `gh-setup dry-run --policy PATH` to inspect
Inventory and every selected change, save its JSON output, then run
`gh-setup apply --policy PATH --dry-run-result DRY_RUN_JSON` to apply the Policy. Both Headless Mode commands accept `--repo OWNER/REPO` to
override the Policy target. Interactive Setup writes a version 2 Policy.

## Commands and workflows

| Earlier entry point or workflow | Current path |
| --- | --- |
| `gh-setup` Interactive Setup | Retained. Its selections use the same Policy and Dry-Run core as Headless Mode. |
| `gh-setup apply --policy PATH` | Now requires `--dry-run-result DRY_RUN_JSON`, a saved `gh-setup dry-run` result for the same Policy and repository. The former `--plan` spelling remains a hidden compatibility alias. Apply blocks before writes if the inspected change set is stale or any selected preflight is blocked. Generate a fresh Dry-Run for each retry. |
| `gh-setup verify-release` | Retained for Release Candidate evidence; it does not perform repository setup. |
| `scripts/setup_github_repo.py ... inventory` and `... policy --file PATH [--apply]` | Retained as direct compatibility commands. Direct Policy apply now requires `--dry-run-result DRY_RUN_JSON` from its read-only Policy command or `gh-setup dry-run`, and uses the same Headless Mode validation and stale-state guard. The former `--plan` spelling remains a hidden compatibility alias. New setup instructions use `gh-setup` for Interactive Setup and Headless Mode. |
| `scripts/configure_github_repo.py` resource commands | Retained for a single resource and for the read-only policy drift audit. They do not replace the complete `gh-setup` Dry-Run. |
| `scripts/install_github_workflows.py` | Retained for standalone workflow installation. For a combined Policy, select `workflows` so selected files share one Setup Pull Request. |
| `scripts/setup_repository_content.py`, `scripts/setup_github_projects.py`, and `scripts/publish_github_wiki.py` | Retained for their standalone resource workflows. Projects V2 and Wiki page publication are outside the P0 Policy core. |
| `scripts/reconcile_github_links.py` | Retained as the independent managed Issue and pull request links command. The `links` Policy stage is rejected before GitHub API access and directs users here. |
| Policy drift, Wiki, release promotion, link check, and issue triage workflows | Retained with their documented triggers, credentials, permissions, and recovery procedures. This migration does not change those workflows. |

## Policy versions and fields

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
