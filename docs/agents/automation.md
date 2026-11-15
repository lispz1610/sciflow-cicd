# Continuous automation guide

This repository's continuous GitHub Actions jobs are documented next to their operational settings:

- [Wiki publishing](wiki.md) covers the `Wiki sync` workflow, first-page setup, dry runs, and recovery.
- [Policy drift audit](../policy-drift-audit.md) covers its read-only policy comparison, `POLICY_AUDIT_TOKEN`, and issue reporting.
- [Workflow guide](../workflows.md#issue-triage-workflow) covers issue triage and the optional `TRIAGE_PROJECT_ID` variable and `TRIAGE_PROJECT_TOKEN` secret for Projects V2.
- [Release evidence](../release-evidence.md#automated-release-promotion) covers release promotion and fail-closed recovery.
- [Dependency graph governance](../dependency-graph-governance.md) covers PR dependency review, release SBOM export, issue DAG frontier orchestration, and architectural import linting.
- [Issue tracker delivery and closure](issue-tracker.md#implementation-delivery-and-closure) covers the `issues: [closed]` enforcement workflow, its live label, Parent link, and pull request declaration scope, token permissions, and recovery after a failed close event. The optional Issue State Gate Setup Pull Request stages the trusted workflows and their Python runtime modules together.
- [Documentation Freshness Audit](../documentation-freshness.md) covers audit scope, its independent managed issue identity and `documentation-freshness` status, maintainer closure/reopen behavior, required Actions permissions, and recovery.
- [Advisory Review](../advisory-review.md#recovery-and-idempotency) covers its separate managed issue and PR link identity, finding triage, required workflow-token permissions, and recovery. Documentation Freshness and Advisory Review must keep separate issues, link comments, and status contexts even when they report the same pull request.
- [Pinned agent skill bundle](../agent-skills.md) covers reviewed upstream provenance, target manifest hashes, duplicate installation, and conflict recovery.
- [Maintainer reference notes](../maintainer-reference.md#evidence-and-documentation-boundary) describe documentation link checks, documentation boundary rules, and optional managed issue-link reconciliation.
- `.github/workflows/workflow-security-audit.yml` runs `scripts/lint_workflow_action_pins.py` on pull requests that change workflow files. The check accepts local actions and external Actions pinned to full commit SHAs. Update a dependency by reviewing the upstream commit and setting `uses:` to the verified 40-character commit SHA; then rerun the linter.

Before changing an automation, keep its event filter, permissions, credentials, and operator instructions in sync. Local workflow YAML and behavior checks are part of the test suite; they do not replace a live run in a repository where the required GitHub settings and credentials are available.
