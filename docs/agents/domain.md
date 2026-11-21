# Domain docs

This repository uses one domain context. Before working in an area, read the root `GLOSSARY.md` and relevant decisions in `docs/adr/`.

Use the glossary's canonical terms in issues, plans, code, and documentation. Surface any conflict with an ADR instead of silently changing the decision. If a domain document is absent, proceed; create terms and decisions when they are resolved.

Keep Documentation Freshness and Advisory Review as distinct `Managed Report Issue` identities. Their issue markers, PR link comments, and status contexts belong to the reporter that created them. A shared `needs-triage` label does not make the reports one lifecycle.

The accepted ownership boundary is recorded in [ADR 0004](../adr/0004-isolate-automated-report-identities.md). The canonical `README.md` entry route, GitHub Issues work-tracking boundary, and optional Wiki secondary-mirror fallback are recorded in [ADR 0005](../adr/0005-canonical-readme-and-wiki-fallback.md).

## Architecture Decision Records

- [ADR 0001: Unify repository and review setup](../adr/0001-unify-repository-and-review-setup.md)
- [ADR 0002: Use one Setup Pull Request for selected workflows](../adr/0002-one-setup-pull-request-for-selected-workflows.md)
- [ADR 0003: Self-contained release candidate verification](../adr/0003-self-contained-release-candidate-verification.md)
- [ADR 0004: Isolate automated report identities](../adr/0004-isolate-automated-report-identities.md)
- [ADR 0005: Establish canonical README route and Wiki fallback boundary](../adr/0005-canonical-readme-and-wiki-fallback.md)
- [ADR 0006: Single-commit cutover for clean product delivery](../adr/0006-single-commit-cutover-for-clean-delivery.md)
- [ADR 0007: Archetype-based Setup Profiles and streamlined interactive setup](../adr/0007-archetype-based-setup-profiles-and-streamlined-interactive-setup.md)
