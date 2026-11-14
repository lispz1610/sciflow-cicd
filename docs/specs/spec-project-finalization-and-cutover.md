## Problem Statement

The repository has reached milestone completion for its core release, delivering capabilities across P0 through P7: an interactive and headless science-focused client core, unified draft setup pull requests, scientific Python and CMake/MPI CI profiles, proportional security and merge controls for solo maintainers, verifiable scientific release promotion with SBOM and citation metadata, agent skills and hook contracts, and an automated advisory Open Code Review (OCR) pipeline.

However, two critical barriers prevent closing the active development phase and presenting the project cleanly:

1. **Stale Disposable Repository Couplings & Outdated Documentation:** Documentation, evidence records, and example policies across the repository still reference temporary, disposable sandbox repositories created during development (e.g. `gh-setup-science-p0-*`, `gh-setup-unified-p1-sandbox`, `gh-setup-python-p2-sandbox`, `gh-setup-cmake-p2-sandbox`). Deleting these sandboxes would leave stale references and dead links if documentation is not made fully self-contained. Furthermore, remaining open backlog issues past P7 (P8 audit and P9 autonomous VM worker with issue queue dispatch) create confusion about what is implemented versus what is future expansion.
2. **Cluttered Exploratory History for Final Delivery:** The development repository currently carries hundreds of iterative exploratory commits, temporary topic branches, and PR review cycles. To deliver a clean, professional, and audit-ready product to a new target repository for presentation and evaluation, maintainers require a safe, deterministic procedure to cut over the entire finalized codebase into a brand-new repository in a single canonical initial commit without carrying historical noise, credentials, or transient files.

## Solution

Deliver a unified project finalization and cutover specification that addresses both needs:

1. **Documentation Decoupling & Sanitization:** Update all documentation, example configurations, and validation guides to be completely self-contained in accordance with ADR 0003. Replace references to disposable sandboxes with generic placeholders and local reproducible test commands.
2. **Consolidated Future Works Roadmap:** Synthesize all post-P7 roadmap items (P8 specification conformance audit, P9 autonomous VM worker, native issue queue intake, and durable task execution) into a canonical `Future Works` document. Retire the loose post-P7 tracking issues with references to this roadmap, establishing a clean milestone boundary.
3. **Single-Commit Repository Cutover Runbook:** Define and execute a verified migration process that audits workspace hygiene, strips temporary artifacts and local scratch data, generates an orphan clean root commit with a comprehensive release message, and verifies full functional parity (test suite pass, CLI entrypoint sanity, link integrity) in the new destination repository.

## User Stories

1. As a project maintainer, I want documentation references to disposable sandbox repositories replaced with self-contained examples, so that deleting ephemeral test repositories does not leave dead links or broken guides.
2. As an evaluator or reviewer, I want validation documents to explain reproduction steps using local loopback and offline fixtures, so that I can independently verify product capabilities without needing access to deleted private sandboxes.
3. As a developer, I want example configuration policies sanitized of specific disposable repository names, so that I can copy and adapt templates without accidental references to obsolete test targets.
4. As a maintainer, I want the requirements and architecture of post-P7 features (P8 audit and P9 VM worker) synthesized into a dedicated Future Works document, so that long-term vision is preserved without cluttering the active milestone queue.
5. As a project stakeholder, I want open post-P7 issues cleanly reconciled and referenced from the Future Works document, so that the repository presents an unambiguous completed milestone for academic evaluation.
6. As a researcher, I want the Future Works roadmap to detail the planned autonomous issue queue dispatch contract, so that future contributors understand how sub-issues, blockers, and `ready-for-agent` labels will interact with VM workers.
7. As an operator, I want the Future Works roadmap to specify the security boundary and service user model for the future Linux VM worker, so that future deployment requirements are clearly understood in advance.
8. As a maintainer, I want a single-commit migration plan that audits the working tree against accidental credentials, logs, and transient files, so that no sensitive data or clutter is leaked to the new repository.
9. As a repository owner, I want the new repository to start with a clean Git history containing exactly one commit representing the finalized v1.0 release, so that presentation and publication history is concise and professional.
10. As a developer, I want all unit, end-to-end, and architectural linter tests to run and pass identically in the new single-commit repository, so that functional integrity is verified immediately upon migration.
11. As a maintainer, I want the primary CLI entrypoint (`gh-setup`) and all subcommands to remain immediately installable and functional in the new repository, so that end-user workflows are uninterrupted.
12. As a security auditor, I want all GitHub Actions workflows to retain their exact SHA-pinned Actions and least-privilege token permissions across the migration, so that security posture is uncompromised.
13. As a documentation reader, I want relative markdown links and cross-references to pass automated verification in the new repository, so that navigation remains fully intact.
14. As an academic presenter, I want the repository state to cleanly demarcate completed capabilities (P0-P7) from future roadmap items, so that professors and evaluators clearly see what was accomplished and verified.
15. As a future contributor, I want git remote setup instructions and cutover verification checklists documented step-by-step, so that repository cutover is repeatable and leaves no ambiguity.

## Implementation Decisions

### Documentation Decoupling & Sanitization
- Adhere strictly to ADR 0003 (*Self-contained release candidate verification*): remove all assumptions of persistent live access to disposable sandboxes.
- Replace hardcoded references to disposable validation repositories with neutral placeholders (`OWNER/REPO` or generic test descriptors) across documentation, guides, and example configuration files.
- Update validation reports to explicitly demarcate verified local/loopback test runs from optional live demonstrations, documenting exact local command lines for reproducer verification.
- Verify that repository-relative markdown link checking passes with zero broken targets or orphaned anchors.

### Future Works Roadmap Consolidation
- Author a canonical, version-controlled Future Works document in the documentation tree and link it directly from the primary `README.md`.
- Consolidate the architectural specifications and user stories of:
  - **P8 (Product Conformance Audit):** Full traceability matrix across P0-P7, adversarial edge-case stress testing, and documentation drift reconciliation.
  - **P9 (Autonomous VM Worker & Issue Queue):** Native GitHub issue intake contract, parent/child issue dispatching, durable task reservation with checkpointing, isolated VM task workspaces, automated Antigravity execution loops, draft PR management, and dedicated Linux service user isolation.
- Retire or update open tracking issues associated with post-P7 scope (#167, #168, #169, #170, #171) by linking them to the newly established Future Works document, signaling milestone closure.

### Single-Commit Repository Cutover Protocol
- **Governed by ADR 0006:** Formalized under [ADR 0006](../../docs/adr/0006-single-commit-cutover-for-clean-delivery.md) (*Single-commit cutover for clean product delivery*), establishing that the new destination repository begins with an audit-ready, single canonical commit representing the v1.0 release while the development repository remains an immutable historical record.
- **Hygiene & Sanitation Audit:** Before cutover, verify clean working tree state:
  - Confirm `.gitignore` excludes all virtual environments, cache directories, OS artifacts, coverage reports, test output, and scratch data.
  - Verify that no secrets, tokens, private keys, or credentials exist in tracked files or examples.
- **Tree Synthesis & Canonical Commit:**
  - Create a clean orphan root commit representing the finalized state of `main`.
  - Structure the single commit message to provide a comprehensive, structured record of the entire system architecture, features delivered across P0 through P7, and references to project ADRs.
- **Remote Configuration & Target Initialization:**
  - Provide a clear, step-by-step sequence of git commands to configure the remote for the user's newly created target repository.
  - Push the single-commit default branch (`main`) to the new origin.
- **Post-Cutover Integrity Verification Gate:**
  - Execute the complete test suite against the fresh target checkout to verify zero regressions.
  - Validate the CLI entrypoint and help outputs.
  - Verify that all GitHub Actions workflow files and configurations remain intact and valid.

## Testing Decisions

- **Black-box behavioral testing:** Verify behavior at the public boundaries (CLI invocation, markdown link integrity, and test suite execution) rather than internal git plumbing.
- **Pre-cutover test gate:** Execute the complete local test suite, the OCR end-to-end loopback suite, the architectural dependency linter, and markdown link verification before initiating cutover.
- **Post-cutover test gate:** Clone or check out the new repository in an isolated workspace, rerun the exact same test matrix, and verify 100% passing tests with identical exit codes.
- **Prior art:** Existing test suites in `tests/`, `scripts/audit_module_dependencies.py`, and `scripts/check_markdown_links.py`.

## Out of Scope

- Implementing the autonomous VM worker daemon or active background polling of GitHub issues (documented as Future Works).
- Modifying the core domain logic, algorithms, or CLI interfaces developed in P0 through P7.
- Altering the upstream source-of-truth specification repository (`sciflow-cicd-setup`).
- Deleting or modifying non-test repositories under the user's account.

## Further Notes

- Relates to parent issues #102 (P7), #167 (P9), #168/#169 (P8), #170 (P9.0), and #171 (P9.1).
- Complies with ADR 0001, ADR 0002, ADR 0003, ADR 0004, ADR 0005, and ADR 0006.
