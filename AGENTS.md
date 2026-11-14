## Agent skills

### Issue tracker

Track issues and specs in GitHub Issues for `lispz1610/github-repo-setup`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the repository's five canonical triage labels. See `docs/agents/triage-labels.md`.

### Domain docs

Use the single-context glossary and ADR layout. See `docs/agents/domain.md`.

### Continuous GitHub Actions automation

Use the operator guides for the repository's scheduled and event-driven workflows:

- Wiki publishing and first-page setup: `docs/agents/wiki.md`.
- Policy drift reporting and its audit credential: `docs/policy-drift-audit.md`.
- Issue triage and optional Projects V2 placement: `docs/workflows.md`.
- Release promotion after a qualified pull request is merged: `docs/release-evidence.md`.
- Markdown link checks, optional managed GitHub link reconciliation, and Workflow Action pin checks: `docs/agents/automation.md`.
- Dependency review security gate: [`docs/dependency-graph-governance.md#pr-dependency-review-security-gate`](docs/dependency-graph-governance.md#pr-dependency-review-security-gate). Scans PR manifest changes for High/Critical CVEs and enforces license allowlists.
- Release SBOM export asset generation: [`docs/dependency-graph-governance.md#supply-chain-sbom-export`](docs/dependency-graph-governance.md#supply-chain-sbom-export). Exports validated SPDX 2.3 `sbom.spdx.json` release assets via GitHub Dependency Graph API during release promotion.
- Issue DAG frontier navigation: [`docs/dependency-graph-governance.md#issue-dag-frontier-orchestration`](docs/dependency-graph-governance.md#issue-dag-frontier-orchestration). Evaluates issue closure events, traverses dependency DAGs, posts wayfinder comments, and transitions unblocked issues to `ready-for-agent`.
- Architectural import linter and layering rules: [`docs/dependency-graph-governance.md#architectural-module-dependency-linter`](docs/dependency-graph-governance.md#architectural-module-dependency-linter). Enforces layering boundaries (core domain modules cannot import CLI/prompts) and verifies zero circular imports.
- Scientific CI profiles, evidence boundaries, and Colab handoff: `docs/scientific-ci-profiles.md`.

Workflow changes must keep their triggers, least-privilege permissions, required secrets or variables, and recovery instructions aligned with these guides.

