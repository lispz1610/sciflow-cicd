# Establish canonical README route and Wiki fallback boundary

Status: accepted

## Context

`github-repo-setup` maintains operator guides in `docs/`, domain terminology in `GLOSSARY.md`, architectural decisions in `docs/adr/`, agent conventions in `docs/agents/`, and optional Wiki synchronization via `.github/workflows/wiki-sync.yml` (`scripts/publish_github_wiki.py`). However, GitHub Wikis cannot be initialized through the REST API (`Settings -> General -> Features -> Wikis` and `Create the first page` require manual GitHub UI steps) and are unavailable on private GitHub Free repositories. In addition, scattering maintainer onboarding across unindexed documents or tracking active work in ad-hoc Markdown scratch files creates competing sources of truth and publishes draft notes to the Wiki whenever `docs/**/*.md` is synced.

## Decision

1. **Canonical Primary Route (`README.md` and `docs/`):** `README.md` is the canonical primary entry point for repository setup, supported profiles, architecture and trust boundaries, client installation and setup, manual GitHub UI steps, ongoing operation, symptom-based recovery, and verification evidence. Every retained Markdown document in `docs/`, `GLOSSARY.md`, and `AGENTS.md` is explicitly classified and reachable from `README.md`.
2. **Wiki as an Optional Secondary Mirror:** The GitHub Wiki published by `.github/workflows/wiki-sync.yml` and `scripts/publish_github_wiki.py` is strictly an optional secondary mirror of `README.md` (`Home`) and `docs/**/*.md`. `README.md` and `docs/` in the main Git repository remain complete and self-contained when the Wiki is disabled, uninitialized before its first manual UI page creation, or unavailable on the repository's GitHub plan.
3. **Separation of Stable Documentation, Domain Model, and Evolving Work:**
   - Stable product and operator guidance lives in `README.md` and linked `docs/` guides.
   - Canonical domain language lives in the single-context root `GLOSSARY.md` (devoid of implementation details), and durable architectural decisions live in `docs/adr/`.
   - Evolving specifications, feature requests, bug reports, and implementation tickets are tracked exclusively in **GitHub Issues** (`docs/agents/issue-tracker.md`), never in unindexed scratch files under `docs/`.

## Consequences

Maintainers can onboard and operate `gh-setup` entirely from `README.md` and linked repository docs without depending on Wiki availability. Because `.github/workflows/wiki-sync.yml` publishes all `docs/**/*.md` files when the Wiki is active, retiring unreferenced planning scratch files from `docs/` keeps both the repository tree and the optional Wiki mirror clean and authoritative.
