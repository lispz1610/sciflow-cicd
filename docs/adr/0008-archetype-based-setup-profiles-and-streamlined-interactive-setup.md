# Archetype-based Setup Profiles and streamlined interactive setup

Status: accepted

## Context

Early iterations of `sciflow-cicd` onboarded repositories through a sequential questionnaire requiring maintainers to answer over fifteen individual prompts across repository features, merge hygiene, Actions permissions, security scanning, workflow templates, and branch rulesets. For maintainers onboarding scientific computing or agent-driven repositories, this created substantial friction and redundant decision-making, often obscuring standard baseline best practices. In addition, greenfield repositories onboarding branch rulesets faced tight coupling to pre-existing pull request checks before any setup pull request had been created or merged.

## Decision

1. **Universal Common Baseline Governance:**
   All repository setup configurations inherit an authoritative common baseline:
   - **Repository Features:** Issues enabled; Wiki disabled in favor of version-controlled `README.md` and `docs/` documentation (per [ADR 0005](0005-canonical-readme-and-wiki-fallback.md)).
   - **Merge Hygiene:** Squash merge enabled (`allow_squash_merge=True`), branch deletion on merge enabled (`delete_branch_on_merge=True`), update branch prompt enabled (`allow_update_branch=True`), and merge commit / rebase merge methods disabled.
   - **Actions & Security:** Default Actions workflow token permissions restricted to read-only (`default_workflow_permissions="read"`), vulnerability alerts enabled, and Dependabot automated security fixes enabled.
   - **Issue Triage:** Provisioning of the five canonical triage labels (`needs-triage`, `bug`, `enhancement`, `documentation`, `ready-for-agent`).

2. **Setup Profile Catalog:**
   Define a catalog of six curated repository archetypes inheriting from the Universal Common Baseline:
   - `scientific-python` ("Scientific Python"): Universal Baseline + Python test matrix with package caching (pip/uv), Git LFS, and CodeQL.
   - `hpc-cmake-mpi` ("HPC C++ / MPI"): Universal Baseline + CMake/MPI with ccache, runner dependencies, rank 1-2 smoke tests, and CodeQL.
   - `agent-ready-governance` ("Agent-Ready Governance"): Universal Baseline + Matt Pocock agent skills bundle, Documentation Freshness Audit, and Issue State Gate.
   - `containerized-scientific` ("Containerized Scientific Application"): Universal Baseline + detected CI + container build and publish workflow (`container-publish.yml` to GHCR) + Dependency Review.
   - `full-suite` ("Full Scientific CI/CD Suite"): Universal Baseline + detected CI + Dependency Review + Container Publish + Agent Skills bundle + Documentation Freshness Audit + Issue State Gate.
   - `minimal-lean` ("Minimal / Lean Governance"): Universal Baseline only (no extra workflows or vendored skills).

3. **Archetype Recommendation Engine:**
   Implement heuristic repository detection (`recommend_profile`) inspecting local file markers (`pyproject.toml`, `setup.py`, `CMakeLists.txt`, `Dockerfile`, `AGENTS.md`, etc.) and live GitHub repository inventory (languages, topics, metadata) to automatically recommend the optimal Setup Profile.

4. **Streamlined 1-Click Interactive Setup:**
   Present the recommended archetype with a 1-click prompt (`[Y/n/c/o]`), allowing immediate acceptance (`Y`), selective stage customization (`c`), alternative profile selection from the catalog (`o`), or clean cancellation (`n`).

5. **Decoupled Greenfield Rulesets:**
   Decouple ruleset onboarding from mandatory immediate `evidence_pr` validation, allowing greenfield repositories without prior pull requests to stage branch rulesets in a pending status until the Setup Pull Request is merged and status checks are verified.

## Consequences

Maintainers can onboard standard scientific repositories in a single click with verified baseline governance and tailored CI/CD profiles. The recommendation engine minimizes manual configuration while preserving granular customization for specialized workflows. Universal baseline inheritance ensures that security, merge hygiene, and issue triage are enforced consistently across all archetypes.

## References

- [Issue #49: Streamline Interactive Setup with archetype-based Setup Profiles and decoupled ruleset onboarding](https://github.com/lispz1610/sciflow-cicd/issues/49)
- [Issue #52: Setup Profile Catalog and Archetype Recommendation Engine](https://github.com/lispz1610/sciflow-cicd/issues/52)
- [ADR 0005: Canonical README route and Wiki fallback boundary](0005-canonical-readme-and-wiki-fallback.md)
