# Dependency Graph Governance

This document specifies the architecture, policies, and operator guides for this repository's four-pillar Dependency Graph Governance system.

---

## 1. Governance Overview & Architectural Pillars

Dependency Graph Governance ensures end-to-end security, integrity, and predictability across the software supply chain, continuous integration, project orchestration, and codebase architecture.

```
                      ┌──────────────────────────────────────────────┐
                      │        Dependency Graph Governance           │
                      └──────────────────────┬───────────────────────┘
                                             │
      ┌──────────────────────┬───────────────┴──────────────┬────────────────────────┐
      ▼                      ▼                              ▼                        ▼
[1. Supply Chain       [2. PR Dependency             [3. Issue DAG            [4. Architectural
     SBOM Export]          Review Gate]               Frontier Engine]         Import Linter]
  • SPDX 2.3 standard    • CVE threshold: high         • GraphQL & Markdown     • Core vs CLI layering
  • GitHub API export    • Permitted license list        blocker discovery      • AST import extraction
  • Fallback document    • Commit SHA pinned           • 'ready-for-agent'      • DFS cycle detection
  • Release asset          dependency-review-action      frontier transitions   • Zero cycles /
                                                       • DFS cycle protection     layering violations
```

The system is organized into four distinct pillars:

1. [Supply Chain SBOM Export](#supply-chain-sbom-export): Exporting validated SPDX 2.3 Software Bill of Materials documents during release promotion.
2. [PR Dependency Review Security Gate](#pr-dependency-review-security-gate): Enforcing CVE vulnerability thresholds and license compliance on pull requests modifying dependency manifests.
3. [Issue DAG Frontier Orchestration](#issue-dag-frontier-orchestration): Tracking dependency graphs across project issues, automatically transitioning unblocked child issues to `ready-for-agent` when blockers close.
4. [Architectural Module Dependency Linter](#architectural-module-dependency-linter): Static Python AST analysis enforcing architectural layering boundaries between core engine modules and CLI/prompt interfaces, and preventing circular module dependencies.

---

## Supply Chain SBOM Export

The supply chain SBOM export pillar produces standardized Software Bill of Materials (SBOM) documents in SPDX 2.3 format as official release evidence.

### Architecture & API Integration

- **Utility:** `scripts/export_release_sbom.py`
- **Workflow:** `.github/workflows/release-promotion.yml` (`Validate release evidence and prepare assets` step in `promote-release` job)
- **Release Asset:** `sbom.spdx.json`
- **GitHub API Endpoint:** `GET /repos/{owner}/{repo}/dependency-graph/sbom`
  - Header: `Accept: application/vnd.github+json`
  - API Version: `X-GitHub-Api-Version: 2022-11-28`
  - Authentication: `GITHUB_TOKEN` or `GH_TOKEN`

The SBOM exporter queries GitHub's Dependency Graph API to retrieve the repository's dependency graph. The response contains an SPDX 2.3 JSON object representing discovered direct and transitive dependencies. The exporter extracts the `sbom` root payload, validates required SPDX fields (`spdxVersion` starting with `SPDX-` and `SPDXID`), and serializes the document to `sbom.spdx.json`.

### Fallback Document Strategy & Release Promotion Fail-Closed Enforcement

When invoked as a standalone CLI tool without `--no-fallback`, the exporter can generate a valid minimal fallback SPDX 2.3 document (`create_fallback_sbom`) recording the creation timestamp, document namespace, and diagnostic reason.

However, **during automated release promotion** (`scripts/release_promotion.py` and `.github/workflows/release-promotion.yml`), SBOM generation enforces strict fail-closed validation (`allow_fallback=False, require_packages=True`):
- If the dependency graph is disabled (HTTP 404), token permissions are missing (HTTP 401/403), or indexing has not completed (empty `packages: []`), release promotion **fails visibly with exit code 1**.
- Publication is aborted immediately: **no empty or placeholder SBOM is uploaded, no git tag is created, and no release is published**.
- Actionable recovery instructions are output naming the exact remediation steps (e.g., enabling Dependency Graph in settings, adding manifests, or waiting for indexing).
- All credential tokens (`ghp_`, `github_pat_`, OAuth bearer tokens) are sanitized and redacted from stdout/stderr diagnostics.
- See [Release evidence failure and recovery](release-evidence.md#promotion-failure-and-recovery) for detailed procedures.

### Operator Instructions for Inspecting Release SBOMs

Maintainers and operators inspect release SBOMs using the following procedures:

1. **Download the Release SBOM:**
   Using the GitHub CLI, fetch the published SBOM from a release tag:
   ```bash
   gh release download <TAG> -p "sbom.spdx.json"
   ```

2. **Inspect Document Metadata:**
   Verify document version, namespace, and creator metadata:
   ```bash
   jq '{spdxVersion, SPDXID, name, documentNamespace, creationInfo}' sbom.spdx.json
   ```

3. **List Declared Packages:**
   Query package names, version strings, download locations, and concluded licenses:
   ```bash
   jq '.packages[] | {name, versionInfo, downloadLocation, licenseConcluded}' sbom.spdx.json
   ```

4. **Verify Direct & Transitive Relationships:**
   Inspect the package dependency graph relationships:
   ```bash
   jq '.relationships[] | {spdxElementId, relationshipType, relatedSpdxElement}' sbom.spdx.json
   ```

5. **Generate or Validate Locally:**
   Export the SBOM locally using repository credentials:
   ```bash
   python3 scripts/export_release_sbom.py --repo lispz1610/sciflow-cicd --output sbom.spdx.json
   ```

6. **Troubleshooting:**
   - **404 Not Found from Dependency Graph API:** Ensure the dependency graph is enabled in the repository under **Settings → Code security and analysis → Dependency graph**.
   - **Authentication error:** Verify `GITHUB_TOKEN` is exported in the environment and has `contents: read` access.

---

## PR Dependency Review Security Gate

The PR dependency review security gate prevents introducing vulnerable dependencies and unapproved software licenses before code is merged into `main`.

### Workflow Configuration & Policy Thresholds

- **Workflow:** `.github/workflows/dependency-review.yml`
- **Trigger:** `pull_request` on branch `main` (`opened`, `synchronize`, `reopened`) restricted to paths:
  - `requirements.txt`
  - `pyproject.toml`
  - `**/requirements*.txt`
  - `setup.py`
- **Action:** `actions/dependency-review-action@5a2ce3f5b92ee19cbb1541a4984c76d921601d7c` (pinned to a full 40-character commit SHA)
- **Least-Privilege Permissions:**
  - `contents: read`: Required to inspect repository tree and commit diffs.
  - `pull-requests: write`: Required to post diagnostic check review summaries directly on the candidate PR.
- **Enforcement Rules:**
  - `fail-on-severity: 'high'`: The check fails if any newly introduced or bumped dependency has a known High or Critical severity CVE.
  - `allow-licenses: 'MIT, Apache-2.0, BSD-3-Clause, BSD-2-Clause, ISC, Python-2.0'`: Only standard permissive licenses are permitted (non-allowlisted licenses such as copyleft GPL and AGPL are rejected; `allow-licenses` and `deny-licenses` are mutually exclusive in `actions/dependency-review-action`).

### Operator Procedures for Resolving Warnings and Exceptions

When the `Dependency review` workflow fails on a pull request, maintainers follow this triage procedure:

#### Step 1: Identify Offending Packages

Inspect the GitHub Actions workflow run logs or the comment posted on the PR by `dependency-review-action`. The report details:
- **Package name and version**
- **Vulnerability identifier (GHSA / CVE)** and severity score
- **Incompatible license identifier** (e.g. `GPL-3.0-only`)

#### Step 2: Resolving CVE Vulnerability Alerts

1. **Check for Patched Releases:** Inspect the package's upstream repository or PyPI page for a patched release that addresses the CVE.
2. **Update Manifest:** Update the version constraint in `requirements.txt` or `pyproject.toml` to require the patched version:
   ```bash
   # Example: upgrade vulnerable package in requirements.txt
   sed -i 's/vulnerable-pkg==1.0.0/vulnerable-pkg>=1.0.1/' requirements.txt
   ```
3. **Resolve Transitive Dependencies:** If the vulnerability is in a transitive dependency, specify an explicit lower bound in `requirements.txt` to force resolution to a patched release.
4. **Alternative Libraries:** If no patched release is available upstream, replace the library with an actively maintained, secure alternative.
5. **Push Updates:** Commit the manifest updates to the PR branch. GitHub Actions will automatically re-run the dependency review gate.

#### Step 3: Resolving License Compliance Exceptions

1. **Assess License Compatibility:** If a newly introduced library triggers an unapproved license warning, review the library's licensing model. Strong copyleft licenses (GPL/AGPL) cannot be included in this project.
2. **Permissive Dual-Licensing:** Check if the dependency is dual-licensed under a permissible license (e.g. MIT or Apache-2.0). If so, document the chosen license.
3. **Updating the Permitted License List:** If a new library uses an acceptable permissive license not yet in the allowlist (for example, `0BSD` or `Unlicense`):
   - Propose the license addition to maintainers for architectural review.
   - Update `allow-licenses` in `.github/workflows/dependency-review.yml`.
   - Ensure the modified workflow action remains pinned to a 40-character commit SHA.

---

## Issue DAG Frontier Orchestration

The issue DAG frontier orchestration engine automates progress tracking across dependent tasks in GitHub Issues. When a blocker issue is completed and closed, the orchestrator traverses the issue dependency Directed Acyclic Graph (DAG) and transitions unblocked child issues to the active frontier ready for work.

### Architecture & Event Trigger

- **Workflow:** `.github/workflows/issue-frontier-orchestrator.yml`
- **Trigger:** `issues: [closed]`
- **Script:** `scripts/orchestrate_issue_frontier.py`
- **Permissions:**
  - `contents: read`: Repository checkout.
  - `issues: write`: Applying triage labels and posting wayfinder comments.

### Dependency Discovery & Graph Traversal

The orchestrator discovers blocker and blocking dependencies using two complementary mechanisms:

1. **Native GitHub GraphQL API:**
   Queries native `blockedBy` and `blocking` connection nodes on candidate issues.
2. **Markdown Body Parsing:**
   Parses structured Markdown dependencies in issue bodies:
   - Dedicated sections: `## Blocked by` or `### Blocked by` containing bullet lists (`- #123`, `* #123`, `• #123`).
   - Inline declarations: `Blocked by: #123, #124`.
   - Qualified repository references: `owner/repo#123`.
   - Full GitHub URLs: `https://github.com/owner/repo/issues/123`.

### Frontier Transition Invariant

An issue transitions to the active frontier **if and only if all of its declared blockers are in the `closed` state**:

```
        Issue #71 (Closed)       Issue #72 (Closed)
                \                       /
                 \                     /
                  ▼                   ▼
                     Issue #73 (Open)
                            │
               [All blockers evaluated closed]
                            │
                            ▼
          Transition: Add 'ready-for-agent' label
          Comment: Wayfinder unblocked notification
```

- If an issue has multiple blockers and only a subset are closed, the issue remains blocked and does not transition.
- Non-closed issue events and pull request closures are ignored (exit code 0 no-op).

### Transition Action & Wayfinder Notification

Upon confirming all blockers are closed, the engine:
1. Adds the `ready-for-agent` label to the candidate issue (if not already present).
2. Posts a structured wayfinder comment referencing the resolved blockers:
   ```markdown
   🚀 All blockers resolved ([#71, #72]). This ticket is unblocked and now on the active frontier ready for agent implementation.
   ```

### Cycle Protection

The orchestrator builds an adjacency graph of candidates and their blocker chains and runs Depth-First Search (DFS) cycle detection (`detect_cycles`). If cyclic dependencies are detected (e.g. `#10 -> #20 -> #10`):
- The cycle path is logged with a warning: `Cyclic dependency detected: #10 -> #20 -> #10`.
- All nodes participating in the cycle are excluded from transition, preventing deadlocks or invalid status changes.

### Operator Instructions & Local Dry-Run

1. **Run a Dry-Run on an Event Payload:**
   Inspect transition decisions without modifying GitHub Issues:
   ```bash
   python3 scripts/orchestrate_issue_frontier.py --event /path/to/github_event.json --dry-run
   ```

2. **Simulate Issue Closure Event:**
   Operators can create a test event JSON to evaluate transitions:
   ```json
   {
     "action": "closed",
     "issue": {
       "number": 72
     },
     "repository": {
       "full_name": "lispz1610/sciflow-cicd"
     }
   }
   ```
   Run the orchestrator against this event:
   ```bash
   python3 scripts/orchestrate_issue_frontier.py --event event.json --dry-run
   ```

3. **Manual Recovery:**
   If an issue was closed while GitHub Actions was paused or credentials expired:
   - Re-run the failed `Issue frontier orchestrator` workflow run from the GitHub Actions console.
   - Alternatively, maintainers can apply the `ready-for-agent` label manually:
     ```bash
     gh issue edit <NUMBER> --add-label ready-for-agent
     ```

---

## Architectural Module Dependency Linter

The architectural module dependency linter statically analyzes internal Python imports using the Abstract Syntax Tree (AST). It enforces strict architectural layering boundaries and ensures zero circular dependencies exist across internal modules.

### Layering Boundaries & Invariants

Modules in this repository are divided into two architectural tiers:

```
┌────────────────────────────────────────────────────────┐
│             Presentation & Interactive Tier            │
│  (cli.py, interactive_*.py, prompts_*.py, cli_*.py)    │
└───────────────────────────┬────────────────────────────┘
                            │  Allowed: UI imports Core
                            ▼
┌────────────────────────────────────────────────────────┐
│                   Core Domain Tier                     │
│  (configure_github_repo, policy_assembly,              │
│   verify_release, release_promotion,                   │
│   export_release_sbom, orchestrate_issue_frontier,     │
│   audit_module_dependencies, review_policy, ...)       │
└────────────────────────────────────────────────────────┘
```

1. **Core Domain Modules (`CORE_DOMAIN_MODULES`):** Contains automation logic, GitHub API integration, policy assemblers, verification engines, and security linters.
2. **Presentation / Interactive Modules (`is_cli_module`):** Contains interactive terminal prompts, question menus, and CLI dispatchers.

**Architectural Layering Rule:**
- Presentation modules may freely import core domain modules.
- **Core domain modules must NEVER import presentation or interactive prompt modules.** Core logic must remain completely independent of terminal interaction and UI state.
- The linter checks both top-level and function-level deferred imports. (Type-checking blocks guarded by `if TYPE_CHECKING:` are exempted).

### Cycle Detection Algorithm

The linter parses all internal Python modules, resolves imported symbols and aliases to canonical module names, and builds a directed dependency graph. It detects circular dependencies using depth-first search (DFS) with recursive back-edge detection:
- Any circular import (e.g. `module_a -> module_b -> module_a`) triggers a failure.
- The linter outputs the exact cycle chain and exits with code `1`.

### CI Integration

The linter runs as a mandatory CI step in `.github/workflows/python-app.yml` on every push and pull request across the matrix of supported Python versions (`3.10`, `3.11`, `3.12`):
```yaml
- name: Audit architectural module dependencies
  run: |
    python3 scripts/audit_module_dependencies.py
```

### Operator & Developer Commands

1. **Run Standard Architectural Audit:**
   Execute the audit against the current repository checkout:
   ```bash
   python3 scripts/audit_module_dependencies.py
   ```
   *Expected output on clean codebase:*
   ```text
   Architectural module dependency audit passed (30 modules, 47 dependencies, 0 cycles, 0 layering violations).
   ```

2. **Verbose Dependency Graph Display:**
   Display all discovered internal modules and their outgoing dependencies:
   ```bash
   python3 scripts/audit_module_dependencies.py --verbose
   ```

3. **Include Deferred Function-Level Imports in Cycle Check:**
   By default, the cycle graph evaluates top-level imports. Use `--include-deferred` to include deferred imports inside function bodies:
   ```bash
   python3 scripts/audit_module_dependencies.py --include-deferred
   ```

4. **Remediating Violations:**
   - **Layering Violation:** If a core module needs user input or formatting, invert the dependency: let the CLI caller pass parameters, callbacks, or configuration objects into the core module rather than having the core module import prompts.
   - **Circular Dependency:** If two modules depend on each other, extract the shared types, models, or utility functions into a separate leaf module, or pass the required data via arguments.
