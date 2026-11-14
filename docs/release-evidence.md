# Release Candidate Lifecycle, Control Record Contract & Verification

This document specifies the operational lifecycle, schema constraints, optional publication features (explicit-tag releases, `CITATION.cff`, five-state DOI governance, SPDX 2.3 SBOM export, and GHCR container publication), and verification contracts for `sciflow-cicd` Release Candidates and Release Gates.

## 1. Overview & Canonical Terminology

Verification of releases adheres to the following canonical concepts defined in [GLOSSARY.md](../GLOSSARY.md):

- **Release Candidate:** A pull request explicitly designated by maintainers for release qualification and bound to mandatory gate verification.
- **Release Gate:** An automated verification checkpoint that evaluates release candidate identity, CI matrix checks, and runtime evidence before permitting release qualification.
- **Control Record:** A trusted document on the base branch declaring the expected identity, target version, and evidence criteria for an active release candidate.
- **Runtime Fingerprint:** A cryptographic hash of the repository's executable assets and policies at a specific commit, verifying that runtime code matches declared release evidence.

Per [ADR 0003](adr/0003-self-contained-release-candidate-verification.md), release verification is entirely self-contained within this repository. The Release Gate evaluates candidate identity, trusted base Control Records, required CI matrix runs, and the Runtime Fingerprint using only the default read-only workflow permissions (`GITHUB_TOKEN`), without dependencies on disposable test repositories or private token secrets.

Human prose reports must not be the sole authority for release qualification. The release verifier evaluates the Control Record and repository evidence deterministically via read-only GitHub API checks and Git repository state.

---

## 2. Six-Stage Maintainer Operational Lifecycle

```
[1. Designation]      Apply 'release-candidate' label to the PR
       │
       ▼
[2. Freeze]           Freeze candidate head commit SHA (all changes pushed)
       │
       ▼
[3. Author & Merge]   Compute Runtime Fingerprint; author & commit docs/release-control.json
                      and docs/release-control.md directly to base branch 'main'
       │
       ▼
[4. Gate Execution]   CI workflow runs verify-release job with PR labels;
                      evaluates fail-closed Release Gate from base commit
       │
       ▼
[5. Decision]         Inspect gate verdict: VERDICT: GO (exit code 0);
                      merge Release Candidate into 'main'
       │
       ▼
[6. Archival]         Set Control Record status to "archived" on 'main'
```

### Stage 1: Designation
Maintainers explicitly mark a candidate pull request for release qualification:
1. Apply the canonical `release-candidate` label to the pull request in GitHub.
2. **Gating Semantics:**
   - **Unmarked Pull Requests:** PRs lacking the `release-candidate` label cleanly bypass the Release Gate (`"Pull request is not marked as a release candidate; skipping release gate"` with exit code 0). Unmarked PRs do not load or evaluate base Control Records, ensuring routine development is never blocked.
   - **Marked Release Candidates:** Pull requests carrying the `release-candidate` label activate the Release Gate in fail-closed mode. Any missing, mismatched, or stale evidence halts CI with exit code 1.

### Stage 2: Freeze
Maintainers stabilize the candidate codebase before declaring evidence:
1. Ensure all features, fixes, dependency updates, and documentation changes are committed and pushed to the candidate branch.
2. Freeze the candidate head commit SHA. Capture the exact 40-character hex commit SHA:
   ```bash
   git rev-parse HEAD
   ```
3. *Freeze Guarantee:* Any subsequent commit pushed to the candidate branch changes its head SHA, invalidating the freeze and causing the Release Gate to fail closed until maintainers explicitly refresh the Control Record.

### Stage 3: Authoring & Base Merge
Maintainers author and publish the trusted Control Record outside the candidate branch tree:
1. **Compute Runtime Fingerprint:** On the frozen candidate head commit, compute the cryptographic fingerprint:
   ```bash
   python3 -c "import scripts.verify_release as vr; print(vr.compute_runtime_fingerprint('.', commit_sha='<CANDIDATE_HEAD_SHA>'))"
   ```
2. **Author Control Record (`docs/release-control.json`):** Declare the candidate's exact identity, optional release `profile` (`minimal`, `release`, or `scientific`), optional `doi_state`, and evidence:
   ```json
   {
     "version": 1,
     "status": "candidate",
     "candidate": {
       "repository": "lispz1610/sciflow-cicd",
       "pull_request": <PR_NUMBER>,
       "target_version": "<TARGET_VERSION>",
       "head_sha": "<CANDIDATE_HEAD_SHA>",
       "profile": "scientific"
     },
     "doi_state": "unselected",
     "required_gates": {
       "python_versions": ["3.10", "3.11", "3.12"]
     },
     "live_evidence": {
       "runtime_fingerprint": "<COMPUTED_RUNTIME_FINGERPRINT>"
     },
     "operational_constraints": [],
     "unverified_scenarios": []
   }
   ```
3. **Author Human Release Report (`docs/release-control.md`):** Draft the accompanying audit report declaring:
   - Matching Pull Request number (`Pull Request: [#<PR_NUMBER>](...)`).
   - Matching Candidate Head SHA (`**Candidate Head SHA:** `<CANDIDATE_HEAD_SHA>``).
   - Matching Target Version (`**Target Version:** `<TARGET_VERSION>``).
   - Matching Formal Verdict (`Current Release Verdict: **READY**` or `**READY WITH DECLARED OPERATIONAL CONSTRAINTS**`).
4. **Publish Directly to Trusted Base Branch (`main`):**
   ```bash
   git checkout main
   git pull origin main
   git add docs/release-control.json docs/release-control.md
   git commit -m "docs(release): publish release control for candidate PR #<PR_NUMBER>"
   git push origin main
   ```
   *Base Branch Isolation Invariant:* The Control Record must be committed on the base branch outside the candidate head. A record committed inside the candidate pull request cannot declare that pull request's own final commit SHA (modifying the record changes the SHA again), and candidate-controlled files cannot serve as their own release authority.

### Stage 4: Gate Execution
The GitHub Actions workflow runs the automated Release Gate:
1. The CI workflow (`.github/workflows/python-app.yml`) triggers on pull request synchronization or labeling.
2. In the `verify-release` job, CI fetches the base branch commit (`$GITHUB_BASE_REF`):
   ```bash
   git fetch origin "$GITHUB_BASE_REF" --depth=1
   control_ref="$(git rev-parse FETCH_HEAD)"
   ```
3. CI collects pull request labels into `PR_LABELS="${{ join(github.event.pull_request.labels.*.name, ',') }}"` and executes:
   ```bash
   python3 scripts/verify_release.py --record docs/release-control.json --report docs/release-control.md --record-ref "$control_ref" --labels "$PR_LABELS" --ci
   ```
4. The Release Gate reads the Control Record exclusively from `$control_ref` on the base branch.

### Stage 5: Decision
Maintainers evaluate Release Gate results:
1. The Release Gate validates four pillars:
   - **Identity Alignment:** Verifies candidate PR number and head commit SHA against the live GitHub PR.
   - **Required CI Gates:** Confirms `build (3.10)`, `build (3.11)`, and `build (3.12)` matrix jobs completed with conclusion `success` on the exact candidate head SHA.
   - **Runtime Fingerprint:** Computes fingerprint across runtime assets (`sciflow-cicd`, `scripts/*.py`, `.github/workflows/*.yml`, dependency files) at the candidate head commit and verifies exact match with the declared hash.
   - **Report Agreement:** Verifies `docs/release-control.md` citations match candidate identity and verdict.
2. Upon passing all checks, the verifier emits:
   ```text
   Verdict: READY
   VERDICT: GO
   ```
   and exits with code `0`.
3. Maintainers can now merge the Release Candidate PR into `main` and tag the release version.

### Automated Release Promotion

After a qualifying `release-candidate` PR is merged into `main`, `.github/workflows/release-promotion.yml` creates the version tag and GitHub Release. It runs only for merged PRs carrying that label and targeting `main`, and uses `pull_request.merge_commit_sha` as the tag target. Unmerged and unmarked PRs bypass promotion.

Promotion fails closed unless `docs/release-control.json` and `docs/release-evidence.json` both parse as supported Control Records with active or candidate status, matching candidate repository, PR number, target version, candidate head SHA, and Runtime Fingerprint. Those identities must match the merged PR event, and the fingerprint is recomputed at the actual merged commit before a release is prepared. Archived or stale evidence cannot publish a release. The workflow runs promotion and Gate verification code from the single common ancestor of the candidate head and the pre-merge main parent, while reading release records and runtime files from the merged checkout. A branch created before this verifier version existed must be refreshed from `main` before it can qualify for promotion. It fetches the candidate commit if necessary for squash or rebase merges and checks the fetched SHA. Before creating a tag, promotion checks the PR file list to ensure the candidate did not change the trusted Control Record, release evidence, or audit report (`TRUSTED_RELEASE_FILES = {"docs/release-control.json", "docs/release-control.md", "docs/release-evidence.json"}`), then re-runs the Release Gate against those trusted records and the candidate head. The recheck requires a GO verdict; ambiguous ancestry, missing trusted code, incomplete PR file lists, and denied API reads fail closed. The workflow token therefore has `pull-requests: read` and `checks: read` alongside `contents: write` for tag and Release publication. The workflow definition is a deployment trust root. `.github/CODEOWNERS` identifies its maintainer, but no branch ruleset enforces code-owner review; the CODEOWNERS entry alone does not block changes to the workflow.

#### Explicit-Tag Release Contract and Immutable Tag Creation

- **Explicit `--tag v<target_version>` Requirement:** `scripts/release_promotion.py` (`prepare_release_payload`) refuses to publish unless `--tag v<target_version>` is explicitly passed and equals `v` + `candidate.target_version` in `docs/release-control.json`. If `--tag` is omitted, blank, or mismatches `v<target_version>`, `prepare_release_payload` raises `ReleasePromotionError` before writing any output files.
- **Annotated Immutable Tag Creation in `.github/workflows/release-promotion.yml`:** In step `Create an annotated immutable tag at the merged commit`, the workflow first checks `gh api "repos/$GITHUB_REPOSITORY/git/ref/tags/$TAG"` and exits `1` with `::error::Refusing to replace existing release tag $TAG` if the tag already exists. Otherwise, it creates an annotated Git tag object (`POST /repos/$GITHUB_REPOSITORY/git/tags` with `"type": "commit"` and `"object": target_sha` pointing to `merge_commit_sha` and `"message"` containing `release-notes.md`), binds `refs/tags/$TAG` via `POST /repos/$GITHUB_REPOSITORY/git/refs` (never using `--force`), and runs `gh release create "$TAG" --verify-tag`.

#### Optional Release Profiles, Assets, Citation Metadata, DOI Governance, SBOM, and Containers

`--profile` (or `"profile"` in `docs/release-control.json`) selects which optional publication assets are validated and attached alongside the mandatory evidence assets (`audit-report.md`, `runtime-fingerprint.txt`, and `--notes-file release-notes.md`):

| Profile | Mandatory Assets | Optional Assets (`optional_assets`) | Pre-Publication Validation Before Any Asset or Tag Is Created |
| --- | --- | --- | --- |
| **`minimal`** | `audit-report.md`, `runtime-fingerprint.txt`, `release-notes.md` | none (`()`) | Control Record & evidence alignment, Runtime Fingerprint match, audit report agreement, and DOI state consistency. (`CITATION.cff` and `sbom.spdx.json` are not required or attached.) |
| **`release`** (default) | `audit-report.md`, `runtime-fingerprint.txt`, `release-notes.md` | `sbom.spdx.json` | All `minimal` checks **plus** fail-closed SPDX 2.3 SBOM export via `scripts/export_release_sbom.py` (`allow_fallback=False, require_packages=True`). |
| **`scientific`** | `audit-report.md`, `runtime-fingerprint.txt`, `release-notes.md` | `CITATION.cff`, `sbom.spdx.json` | All `release` checks **plus** `CITATION.cff` structural & DOI state validation (`validate_citation_metadata`). |

- **Citation Metadata (`CITATION.cff`) Contract (`validate_citation_metadata`):**
  - Required top-level non-empty scalar fields: `cff-version`, `message`, and `title`.
  - Required top-level `authors:` block listing at least one `- ` author entry.
  - Optional `version`: if present, must exactly equal `candidate.target_version` in `docs/release-control.json`.
  - Optional `date-released`: if present, must match `YYYY-MM-DD`.
  - Optional `doi`: governed strictly by the five-state DOI state machine in `scripts/doi_guidance.py` (see [DOI guidance](doi-guidance.md)):
    - **`issued` (`DoiState.ISSUED`):** A bare DOI string matching `^10\.\d{4,9}/\S+$` and verified to resolve at `https://doi.org/<doi>` **must** be present and identical across `CITATION.cff`, `docs/release-control.json`, and `docs/release-evidence.json`, after confirming minting on the Zenodo dashboard (or institutional DataCite registry) and checking resolution at `https://doi.org/<doi>`.
    - **`requested`, `pending`, `unavailable`, or `unselected` (`not-selected`):** A DOI string is **never recorded** in `CITATION.cff`, `docs/release-control.json`, or `docs/release-evidence.json` before `issued` verification. Declaring any `doi` string while `doi_state` is `unselected`, `requested`, `pending`, or `unavailable` aborts release promotion immediately.
- **Public Zenodo vs. Private-Source Manual Deposit:**
  - **Public repositories:** Enable the `Zenodo -> GitHub` repository toggle before publishing the GitHub Release. Because Zenodo mints the Version DOI via webhook *after* the GitHub Release is published, promote the initial release with `doi_state` set to `"unselected"`, `"requested"`, or `"pending"` (no `doi` field), verify minting on the Zenodo dashboard and resolution at `https://doi.org/<doi>`, and only then transition to `doi_state: "issued"`.
  - **Private-source repositories:** Zenodo's automatic GitHub webhook cannot archive private repositories. Maintainers upload the release archive or a restricted/embargoed/metadata-only deposit manually at `https://zenodo.org/deposit/new`, verify the minted Version DOI resolves at `https://doi.org/<doi>`, and only then record `doi_state: "issued"` (see [DOI guidance](doi-guidance.md)).
- **SPDX 2.3 SBOM Export (`scripts/export_release_sbom.py`):**
  - Queries `GET /repos/<OWNER>/<REPO>/dependency-graph/sbom`, validates `spdxVersion` (`SPDX-2.3` / `SPDX-*`) and `SPDXID` (`SPDXRef-DOCUMENT`), and writes `sbom.spdx.json`.
  - During release promotion (`release` and `scientific` profiles), `scripts/release_promotion.py` invokes `export_release_sbom(..., allow_fallback=False, require_packages=True)`. If SBOM generation fails, promotion **aborts before creating the Git tag or GitHub Release**.
- **Opt-In Container Publication (`container-publish` workflow template):**
  - Default repository setups install zero container workflows. Maintainers distributing container images to GitHub Container Registry (`ghcr.io/<owner>/<repo>`) explicitly select the `container-publish` workflow template (`.github/workflows/container-publish.yml`, marker `# sciflow-cicd workflow-template=container-publish version=1`) via Interactive Setup or Policy `workflows` (see [Versioned workflow installation](workflows.md)).
  - The generated workflow runs with least-privilege permissions (`contents: read`, `packages: write`) and 40-character SHA-pinned Actions (`actions/checkout`, `docker/login-action`, `docker/build-push-action`). On `pull_request`, it verifies image build without logging in or pushing (`if: github.event_name != 'pull_request'`, `push: ${{ github.event_name != 'pull_request' }}`); on non-PR triggers such as `workflow_dispatch` (or release/push events), it logs into `ghcr.io` using `secrets.GITHUB_TOKEN` and pushes `ghcr.io/${{ github.repository }}:${{ github.sha }}`.
- **Scientific Reproducibility Disclaimer:** Release notes always record: *"This release records verified evidence for the selected commit. It does not prove scientific reproducibility."* Neither the release evidence, `CITATION.cff`, `sbom.spdx.json`, nor a published container image proves numerical or domain reproducibility.

### Promotion failure and recovery

Inspect the failed `Release promotion` run before retrying. The prepare step reports which identity, status, report, or fingerprint check failed; the tag and release steps do not run until every check passes. Unmerged pull requests and merged pull requests without the `release-candidate` label are intentional skips, not failures.

- **Evidence, identity, or fingerprint validation failed:** Promotion validates the control record, release evidence, audit report, and recomputed fingerprint from the actual merge commit. Correct evidence on the candidate before merge and rerun the Release Gate. If the PR has already merged with mismatched or missing evidence, this workflow cannot make that merge commit eligible by editing later files on `main`; prepare a new qualified candidate with matching records and repeat the reviewed release process.
- **Trusted record or Gate recheck failed:** The candidate PR must not change `docs/release-control.json`, `docs/release-control.md`, or `docs/release-evidence.json`; publish those records directly to `main` before qualifying the PR. Inspect the failed Gate diagnostic and its required CI runs on the candidate head. Confirm the workflow token has Pull requests and Checks read access. Do not bypass the recheck or tag the merge commit after a failed verdict.
- **Trusted verifier checkout failed:** The candidate must share one unambiguous ancestor with the pre-merge main history, and that ancestor must contain the promotion and Gate scripts. A candidate branched before these automations were installed cannot be promoted by this workflow; create a new candidate from updated `main` and rerun its checks.
- **SBOM generation or Dependency Graph failure (Fail-Closed Abort Before Tag or Release Creation):**
  When running under `--profile release` or `--profile scientific`, `scripts/release_promotion.py` calls `export_release_sbom(..., allow_fallback=False, require_packages=True)`. If SBOM generation fails because the GitHub Dependency Graph is disabled (`HTTP 404`), the token lacks access (`HTTP 401/403`), or the returned SPDX payload contains **zero packages** (`packages: []`), the documented and implemented behavior is to **abort the release before creating the Git tag or GitHub Release**, writing zero files to the release output directory and uploading no placeholder/fallback SBOM.
  **Step-by-step maintainer recovery:**
  1. **If Dependency Graph is disabled (`HTTP 404`):** Open **Settings -> Code security -> Dependency graph** (or **Settings -> Code security and analysis -> Dependency graph**), enable **Dependency graph**, and wait for GitHub to index the repository's manifests.
  2. **If token permissions are rejected (`HTTP 401/403`):** Verify that `GITHUB_TOKEN` (or `GH_TOKEN`) has `contents: read` permission and can read `GET /repos/<OWNER>/<REPO>/dependency-graph/sbom`.
  3. **If the SBOM has zero packages (`packages: []`):**
     - *Path A (Repository has dependencies):* Commit a supported dependency manifest (such as `requirements.txt`, `pyproject.toml`, `Pipfile.lock`, or `poetry.lock`) to `main`, confirm packages appear under **Insights -> Dependency graph**, and verify with `python3 scripts/export_release_sbom.py --repo <OWNER>/<REPO> --no-fallback --require-packages`.
     - *Path B (Repository intentionally has zero third-party package dependencies, e.g., pure standard-library or standalone C++/MPI code):* Switch the release profile to `"minimal"` by setting `"profile": "minimal"` inside `candidate` in `docs/release-control.json` and `docs/release-evidence.json` on `main` so SBOM export is not required.
  4. Re-run the failed `Release promotion` workflow job (if Dependency Graph was enabled/indexed for the same merge commit) or qualify a new release candidate PR.
- **Citation validation and DOI governance failure:** When running under `--profile scientific`, `CITATION.cff` is structurally validated at the merged commit. Missing required fields (`cff-version`, `message`, `title`, `authors`), empty values, target version mismatch, or malformed DOI strings abort publication before creating the Git tag or GitHub Release. Furthermore, per [DOI guidance](doi-guidance.md), a DOI is **never recorded** in `CITATION.cff`, `docs/release-control.json`, or `docs/release-evidence.json` before issuance is verified (`doi_state: "issued"`). Pre-release drafts and unminted records must remain in `unselected` (`not-selected`), `requested`, `pending`, or `unavailable` states without a `doi` string.
- **Container workflows and distribution boundaries:** Default repository installations contain zero container workflows. Maintainers requiring container image publication to GitHub Container Registry (`ghcr.io/<owner>/<repo>`) must explicitly opt into the `container-publish` template (`.github/workflows/container-publish.yml`; see [Versioned workflow installation](workflows.md)). Container publication is separate from the release promotion job and operates with least-privilege permissions (`packages: write`, `contents: read`) and SHA-pinned Actions.
- **A transient GitHub API or release creation error occurred:** Once the cause is corrected, use **Re-run jobs** for the failed run. The workflow rechecks the same merged commit and evidence before attempting publication.
- **The run reports an existing tag:** Do not move or overwrite it (`Refusing to replace existing release tag`). Inspect the tag target and any existing GitHub Release to determine whether this version was already promoted or a prior run stopped partway through. If the tag points to the expected merge commit and the release is missing, have a maintainer follow the repository's release recovery process; this workflow intentionally fails closed rather than replacing the immutable tag. If it points elsewhere, stop and resolve the version/tag conflict with maintainers before promoting another candidate.
- **Tag creation succeeded but release creation failed:** Confirm the annotated tag still points to the expected merge commit and inspect whether the Release exists. A rerun will stop at the pre-existing-tag guard. Preserve the tag; have a maintainer complete or reconcile the GitHub Release using the approved release process and retained audit evidence. Do not delete or force-move the tag to make automation rerun.

The workflow does not upload intermediate files as run artifacts. Keep the reviewed release control/evidence and audit report in the repository as the authoritative records; the workflow generates release notes, fingerprint, and SBOM assets from those records when it runs.

### Stage 6: Post-Release Archival
After merging the Release Candidate and completing the release:
1. Update `docs/release-control.json` on `main` to set `"status": "archived"`:
   ```bash
   git checkout main
   git pull origin main
   # Edit docs/release-control.json to set "status": "archived"
   git add docs/release-control.json
   git commit -m "docs(release): archive release control record for PR #<PR_NUMBER>"
   git push origin main
   ```
2. In CI mode, archived records automatically skip gating (`Control Record is archived for historical candidate PR #...; skipping release gate in CI`), ensuring future development branches targeting `main` remain unblocked.

---

## 3. Control Record Schema (Version 1)

Every Control Record JSON document must strictly conform to schema version 1. Unexpected keys, duplicate keys, or secret tokens are rejected immediately.

### Canonical Schema

```json
{
  "version": 1,
  "status": "candidate",
  "candidate": {
    "repository": "OWNER/REPO",
    "pull_request": 28,
    "target_version": "0.1.0-rc.1",
    "head_sha": "70e261a657f347277da065397266461ddeda4487"
  },
  "required_gates": {
    "python_versions": ["3.10", "3.11", "3.12"]
  },
  "live_evidence": {
    "runtime_fingerprint": "4db1179c7eb581c0fdd5c891a801636b75f6ef42540e420e7a05f3e5034e34e7"
  },
  "operational_constraints": [
    "Private Repository Constraint for Advisory Review",
    "GitHub Token OAuth Scope for Workflow Staging"
  ],
  "unverified_scenarios": [
    "Live execution of corrected direct-command exit code for failed workflow staging"
  ]
}
```

### Schema Rules & Boundaries

1. **Version:** Must be the integer `1`. Any other version is rejected.
2. **Status:** Optional string, one of `"active"`, `"candidate"`, or `"archived"`. Defaults to `"active"`. When marked `"archived"`, CI skips Release Gate checks.
3. **Candidate Object:**
   - `repository`: Repository slug in `OWNER/REPO` format.
   - `pull_request`: Positive integer identifying the Release Candidate PR.
   - `target_version`: Semantic version string.
   - `head_sha`: Full 40-character commit hex SHA.
   - `profile` (optional): `"minimal"`, `"release"`, or `"scientific"` (defaults to `"release"`).
4. **Optional DOI Governance Fields (`doi_state` and `doi`):**
   - `doi_state`: Optional string, one of `"unselected"`, `"requested"`, `"pending"`, `"issued"`, or `"unavailable"` (defaults to `"unselected"`).
   - `doi`: Bare DOI string matching `^10\.\d{4,9}/\S+$`. Allowed and required **only** when `doi_state` is `"issued"`; forbidden in all other states.
5. **Required Gates Object:**
   - `python_versions`: Supported Python matrix versions exactly once (`["3.10", "3.11", "3.12"]`).
6. **Live Evidence Object:**
   - `runtime_fingerprint`: Deterministic 64-character lowercase hex SHA-256 hash.
   - Historical live validation fields (`target_repository`, `setup_pr`, `review_run`) are supported for backward compatibility with earlier validation runs.
7. **No Secret Tokens:** Any pattern resembling a personal access token (`ghp_`, `github_pat_`), OAuth token (`gho_`), Bearer token, private key, or API key is forbidden. If detected, parsing fails closed with a redaction diagnostic.
8. **No Duplicate Keys:** Raw JSON with duplicate keys is rejected.

---

## 4. Release Gate Verification Commands

### CLI Usage

Maintainers and CI run the Release Gate using the `verify-release` command:

```bash
# Verify using local workspace files:
./sciflow-cicd verify-release --record docs/release-control.json --report docs/release-control.md

# Verify in CI mode extracting Control Record from a trusted base commit SHA:
python3 scripts/verify_release.py \
  --record docs/release-control.json \
  --report docs/release-control.md \
  --record-ref <BASE_COMMIT_SHA> \
  --labels "release-candidate" \
  --ci
```

### CLI Arguments

- `--record <PATH>`: Path to Control Record JSON document (default: `docs/release-evidence.json`).
- `--record-ref <COMMIT_SHA>`: Exact 40-character Git commit SHA to extract the Control Record and report from outside the candidate tree.
- `--report <PATH>`: Path to human release audit report Markdown file (default: `docs/production-release-audit.md`).
- `--labels <LABELS>`: Pull request labels passed as a comma-separated string (e.g. `--labels "bug,release-candidate"`).
- `--repo <OWNER/REPO>`: Target repository slug override.
- `--token <TOKEN>`: GitHub API token (falls back to `GITHUB_TOKEN` environment variable or `gh auth token`).
- `--format {text,json}`: Output format (default: `text`).
- `--ci`: Run in continuous integration mode.

### Exit Codes & Verdicts

- **Exit Code `0`:**
  - `VERDICT: GO` (text mode) or `"verdict": "GO"` (JSON mode): All Release Gate requirements are met.
  - Gate bypass: Pull request is unmarked (missing `release-candidate` label), event is not a pull request, or Control Record is archived.
- **Exit Code `1`:**
  - `Verdict: NOT READY`: Release Gate failed closed due to missing records, identity mismatch, unpassed CI matrix checks, runtime fingerprint mismatch, or report contradictions.

---

## 5. Troubleshooting & Maintainer Recovery Procedures

When the Release Gate encounters missing, mismatched, or stale evidence for a Release Candidate, it fails closed with exit code 1, sanitizes all tokens, and prints actionable recovery commands.

### Diagnostic Summary Table

| Failure Mode | Diagnostic Symptom | Root Cause | Maintainer Recovery |
| :--- | :--- | :--- | :--- |
| **Missing Control Record** | `Release control file docs/release-control.json is unavailable at commit <SHA>` | PR is labeled `release-candidate`, but base branch lacks `docs/release-control.json` | Author and push Control Record and report directly to `main` |
| **Candidate PR Mismatch** | `Candidate PR mismatch: expected PR #<A>, observed PR #<B>` | Base Control Record declares a different PR number than current candidate | Update `pull_request` in `docs/release-control.json` on `main` |
| **Head SHA Mismatch** | `Candidate head commit SHA mismatch: expected <SHA_A>, observed <SHA_B>` | Candidate branch received additional commits after freeze | Update `head_sha` and `runtime_fingerprint` on `main`, or reset branch |
| **CI Matrix Failure** | `Required CI job build (3.x) failed with conclusion 'failure'` | Python matrix check failed or has not completed on candidate head | Re-run CI checks or fix defect and update candidate head |
| **Fingerprint Discrepancy** | `Runtime fingerprint mismatch (recorded <A>, computed <B>)` | Runtime source or workflow files modified since fingerprint was computed | Recompute fingerprint on candidate head and update `docs/release-control.json` on `main` |
| **Report Misalignment** | `Verdict mismatch` or `Contradictory release claims` | `docs/release-control.md` claims `READY` while verifier determined `NOT READY` | Align report verdict and citations with verified candidate state |
| **SBOM Export Failure** | `Release promotion failed: Cannot generate release SBOM` | Dependency Graph disabled (`HTTP 404`), token denied (`HTTP 401/403`), or zero packages (`packages: []`) on `release`/`scientific` profile | Enable `Settings -> Code security -> Dependency graph` and commit a manifest, or set `"profile": "minimal"` in `docs/release-control.json` |
| **Unverified DOI or Invalid `CITATION.cff`** | `Invalid CITATION.cff: ...` or `A DOI string cannot be recorded when state is ...` | `doi` recorded while `doi_state` is not `"issued"`, or `CITATION.cff` missing `cff-version`/`message`/`title`/`authors` or mismatched `version` | Strip `doi` until verified at `https://doi.org/<doi>`, or fix `CITATION.cff` fields before promotion |

---

### Recovery Procedures & Exact Commands

#### 1. Missing Base Control Record at `record_ref`
- **Observed Diagnostic:**
  ```text
  Error: Invalid Control Record: Release control file docs/release-control.json is unavailable at commit <BASE_COMMIT_SHA>
  Recovery: Publish the trusted release control record on the base branch:
    git checkout main
    git pull origin main
    git add docs/release-control.json docs/release-control.md
    git commit -m "docs(release): publish release control for candidate PR #<PR_NUMBER>"
    git push origin main
  Verdict: NOT READY
  ```
- **Recovery Steps:**
  Execute the exact commands formatted by the verifier to publish the Control Record on the base branch:
  ```bash
  git checkout main
  git pull origin main
  git add docs/release-control.json docs/release-control.md
  git commit -m "docs(release): publish release control for candidate PR #<PR_NUMBER>"
  git push origin main
  ```
  Once pushed to `main`, re-run the failed `verify-release` job on the candidate PR.

#### 2. Candidate PR Number Mismatch
- **Observed Diagnostic:**
  ```text
  Release Candidate: lispz1610/sciflow-cicd PR #28
  Verdict: NOT READY
  Reason: Candidate PR mismatch: expected PR #28, observed PR #42
  Recovery: Update docs/release-control.json on the base branch to declare candidate pull_request 42.
  ```
- **Recovery Steps:**
  Update the base branch Control Record with the current candidate PR number:
  ```bash
  git checkout main
  git pull origin main
  # Edit docs/release-control.json to set "pull_request": 42
  # Update PR references in docs/release-control.md
  git add docs/release-control.json docs/release-control.md
  git commit -m "docs(release): update release control for candidate PR #42"
  git push origin main
  ```

#### 3. Candidate Head Commit SHA Mismatch (Stale Candidate Head)
- **Observed Diagnostic:**
  ```text
  Release Candidate: lispz1610/sciflow-cicd PR #42
  Verdict: NOT READY
  Reason: Candidate head commit SHA mismatch: expected commit SHA 70e261a657f347277da065397266461ddeda4487, observed commit SHA abc1234def56789012345678901234567890abcd
  Recovery: Align candidate branch head with declared head_sha or update docs/release-control.json on base branch with observed commit SHA abc1234def56789012345678901234567890abcd.
  ```
- **Recovery Steps:**
  - **Option A (If commits on candidate branch are intentional):**
    1. Confirm all required CI matrix checks pass on the observed commit SHA (`abc1234...`).
    2. Recompute the Runtime Fingerprint for the new commit:
       ```bash
       python3 -c "import scripts.verify_release as vr; print(vr.compute_runtime_fingerprint('.', commit_sha='abc1234def56789012345678901234567890abcd'))"
       ```
    3. Publish the updated head SHA and Runtime Fingerprint to `main`:
       ```bash
       git checkout main
       git pull origin main
       # Update "head_sha" and "runtime_fingerprint" in docs/release-control.json
       # Update "**Candidate Head SHA:**" in docs/release-control.md
       git add docs/release-control.json docs/release-control.md
       git commit -m "docs(release): refresh candidate head SHA and fingerprint for PR #42"
       git push origin main
       ```
  - **Option B (If accidental commits were pushed to candidate branch):**
    Reset the candidate branch back to the declared frozen head SHA:
    ```bash
    git checkout <CANDIDATE_BRANCH>
    git reset --hard 70e261a657f347277da065397266461ddeda4487
    git push origin <CANDIDATE_BRANCH> --force-with-lease
    ```

#### 4. CI Matrix Check Failures
- **Observed Diagnostic:**
  ```text
  Release Candidate: lispz1610/sciflow-cicd PR #42
  Verdict: NOT READY
  Reason: Required CI job build (3.11) failed with conclusion 'failure' on candidate head <HEAD_SHA>
  Recovery: Fix test or build failure in CI job build (3.11) and re-run on candidate head <HEAD_SHA>.
  ```
- **Recovery Steps:**
  - If a job failed due to a transient error, re-trigger the check via GitHub CLI:
    ```bash
    gh run rerun <RUN_ID> --failed
    ```
  - If a job is still in progress (`in_progress`), wait for GitHub Actions to complete before running verification.
  - If tests failed due to a genuine bug, fix the defect on the candidate branch, push the fix, and follow Procedure #3 (Option A) to refresh the Control Record on `main`.

#### 5. Runtime Fingerprint Discrepancy
- **Observed Diagnostic:**
  ```text
  Release Candidate: lispz1610/sciflow-cicd PR #42
  Verdict: NOT READY
  Reason: Runtime fingerprint mismatch (recorded 4db1179c7eb581c0fdd5c891a801636b75f6ef42540e420e7a05f3e5034e34e7, computed a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef0): runtime product code or templates were modified since live validation was recorded.
  Recovery: Recompute runtime fingerprint for candidate commit and refresh the control record.
  ```
- **Recovery Steps:**
  1. Inspect changes to runtime-relevant paths:
     ```bash
     git diff main...<HEAD_SHA> -- sciflow-cicd scripts/ .github/workflows/ requirements*.txt pyproject.toml
     ```
  2. Recompute the fingerprint:
     ```bash
     python3 -c "import scripts.verify_release as vr; print(vr.compute_runtime_fingerprint('.', commit_sha='<HEAD_SHA>'))"
     ```
  3. Publish the updated fingerprint on `main`:
     ```bash
     git checkout main
     git pull origin main
     # Update "runtime_fingerprint" in docs/release-control.json
     git add docs/release-control.json
     git commit -m "docs(release): update runtime fingerprint for candidate PR #42"
     git push origin main
     ```

#### 6. Human Report Misalignment
- **Observed Diagnostic:**
  ```text
  Release Candidate: lispz1610/sciflow-cicd PR #42
  Verdict: NOT READY
  Reason: Contradictory release claims: human report claims 'READY' but verification engine determined 'not ready'.
  Recovery: Update human report verdict to 'NOT READY' until all release gates pass.
  ```
- **Recovery Steps:**
  Ensure `docs/release-control.md` accurately reflects verified state:
  ```bash
  git checkout main
  git pull origin main
  # Ensure docs/release-control.md states "Current Release Verdict: **READY**"
  # (or "READY WITH DECLARED OPERATIONAL CONSTRAINTS" if constraints are declared)
  # only when all candidate identity, CI, and fingerprint checks pass.
  git add docs/release-control.md
  git commit -m "docs(release): align human report verdict with verified state"
  git push origin main
  ```

---

## 6. Publication Feature Selection, Verification Steps, and Unavailable / Not-Selected Paths

Maintainers of scientific repositories select only the release and citation features needed for their project milestone. Every option has a concrete verification step and an honest `unavailable` or `not-selected` (`unselected`) path:

| Publication Option | When to Select | How to Configure | Concrete Verification Step | Honest `Unavailable` or `Not-Selected` Path |
| :--- | :--- | :--- | :--- | :--- |
| **1. `minimal` release profile** | Internal milestones or standalone C++/Python codebases without indexed third-party package manifests or `CITATION.cff`. | Set `"profile": "minimal"` in `docs/release-control.json` (`candidate.profile`) or pass `--profile minimal` to `scripts/release_promotion.py --tag v<target_version>`. | Run `scripts/release_promotion.py --profile minimal --tag v1.0.0` and confirm `optional_assets` is `[]` and only `audit-report.md`, `runtime-fingerprint.txt`, and `release-notes.md` are generated. | **Not-selected:** Superseded when `"profile": "release"` (default) or `"profile": "scientific"` is selected. |
| **2. `release` profile (with SPDX 2.3 SBOM)** | Software releases requiring supply-chain dependency provenance (`sbom.spdx.json`) without formal citation metadata. | Default profile (`"profile": "release"` in `docs/release-control.json` or `--profile release`). Requires GitHub Dependency Graph enabled and at least one indexed package. | Run `python3 scripts/export_release_sbom.py --repo OWNER/REPO --no-fallback --require-packages` and verify `sbom.spdx.json` contains `"spdxVersion": "SPDX-2.3"`, `"SPDXID": "SPDXRef-DOCUMENT"`, and non-empty `"packages"`. | **Unavailable:** If Dependency Graph is disabled (`HTTP 404`) or repository has zero packages (`packages: []`), `release_promotion.py` aborts before creating the Git tag or GitHub Release. Switch to `"profile": "minimal"` or enable **Settings -> Code security -> Dependency graph**. |
| **3. `scientific` profile (`CITATION.cff` + SBOM)** | Citable scientific software releases accompanying a manuscript, archive deposit, or benchmark study. | Set `"profile": "scientific"` in `docs/release-control.json` (`candidate.profile`) and commit a valid `CITATION.cff` (`cff-version`, `message`, `title`, `authors`, matching `version`, and `YYYY-MM-DD` `date-released`). | Run `scripts/release_promotion.py --profile scientific --tag v<target_version>` and verify `optional_assets` equals `["CITATION.cff", "sbom.spdx.json"]` and both files are written to the release asset directory. | **Not-selected / Unavailable:** Use `"profile": "minimal"` or `"profile": "release"` when no `CITATION.cff` is maintained. If `CITATION.cff` is missing/invalid or SBOM export fails, promotion aborts fail-closed before tag creation. |
| **4. Public Zenodo integration** | Public open-source scientific repositories minting automated Version DOIs on GitHub Release publication. | Toggle `Zenodo -> GitHub` **ON** for the public repository, promote the release with `doi_state` set to `"requested"` or `"pending"` (no `doi` string), then update to `"issued"` after minting. | Confirm the Version DOI (`^10\.\d{4,9}/\S+$`) on the Zenodo dashboard and verify HTTP resolution at `https://doi.org/<doi>`, then run `validate_citation_cff_doi` and `validate_release_doi_metadata` across `CITATION.cff`, `docs/release-control.json`, and `docs/release-evidence.json`. | **Not-selected:** Keep `"doi_state": "unselected"` and omit `doi`.<br>**Unavailable:** Automatic Zenodo GitHub webhooks are **unavailable** for private repositories (use private-source manual deposit instead) or if the external Zenodo service fails (`"doi_state": "unavailable"`). |
| **5. Private-source manual deposit** | Private, proprietary, or embargoed scientific repositories that require a persistent DOI without exposing a public GitHub repository. | Create a manual upload at `https://zenodo.org/deposit/new` with **Restricted Access**, **Embargoed Access**, or closed/metadata-only access; upload the release archive, `.sha256`, `CITATION.cff`, and `sbom.spdx.json`. | After publishing the deposit on Zenodo, verify the minted Version DOI resolves at `https://doi.org/<doi>`, then transition `DoiState` from `PENDING` to `ISSUED` and record the verified DOI in `CITATION.cff`, `docs/release-control.json`, and `docs/release-evidence.json`. | **Not-selected:** Keep `"doi_state": "unselected"` with no `doi` field when external archival is not required.<br>**Unavailable:** Record `"doi_state": "unavailable"` (with no `doi` string) if institutional policy prohibits external metadata deposit or the registry rejects the submission. |
| **6. `container-publish` workflow (`ghcr.io`)** | Projects distributing prebuilt container images (`Dockerfile`) to GitHub Container Registry (`ghcr.io/<owner>/<repo>`). | Select `Container build and publish (container-publish.yml)` in `sciflow-cicd` Interactive Setup or include `{"template": "container-publish", "template_version": 1, "filename": "container-publish.yml", "triggers": ["pull_request", "workflow_dispatch"], ...}` in Policy `workflows`. | Inspect `.github/workflows/container-publish.yml` in the Setup Pull Request (`# sciflow-cicd workflow-template=container-publish version=1`), verify `permissions: contents: read, packages: write` and SHA-pinned Docker Actions, and confirm PR runs build without pushing while non-PR runs push to `ghcr.io/<owner>/<repo>`. | **Not-selected (Default):** Default `sciflow-cicd` installations stage **zero** container workflows.<br>**Unavailable:** Live push to `ghcr.io` is unavailable on `pull_request` events by design (`push: ${{ github.event_name != 'pull_request' }}`) and requires GitHub Packages permissions on the target account. |

---

## 7. Disposable Target Environment Validation Exercise (`Observed` / `Inferred` / `Unverified`)

To verify the optional release, citation, DOI, SBOM, and container workflows end-to-end without mutating production tags, a disposable target environment (`octo/disposable-sci-release`, target version `1.0.0`, explicit tag `v1.0.0`) was exercised using `scripts/verify_release.py`, `scripts/release_promotion.py`, `scripts/doi_guidance.py`, `scripts/export_release_sbom.py`, and `scripts/install_github_workflows.py` (reproducible via `python3 -m unittest tests/test_p6_185_release_citation_guidance.py -v` and `python3 -m unittest tests/test_disposable_target_release_validation.py -v`).

### 7.1 Recorded Disposable Target Parameters and Artifacts

- **Disposable Target Repository:** `octo/disposable-sci-release` (Pull Request `#42`, base branch `main`).
- **Explicit Release Tag:** `v1.0.0` (derived from `candidate.target_version: "1.0.0"` in `docs/release-control.json`).
- **Candidate Head Commit SHA:** `ddfc2482a2f8e744872329af97374cd9c8b04d8a`
- **Merged Commit SHA (`target_sha`):** `4d2113af9af535024cd6e2e482691cb41097d0b1`
- **Computed Runtime Fingerprint (`compute_runtime_fingerprint`):** `04c59e4a30dcf2e78b0f055c9b1f805bbc16de057b891f393b4227405fbe3b7f`
- **Validated Citation Metadata (`CITATION.cff`):**
  - `cff-version: 1.2.0`
  - `message: "If you use this software, please cite it as below."`
  - `title: "Disposable Scientific Solver"`
  - `version: 1.0.0` (matches `candidate.target_version`)
  - `date-released: 2026-11-11` (matches `YYYY-MM-DD`)
  - `authors:` `Noether, Emmy`
- **Release Payload Returned by `prepare_release_payload` (`scientific` profile):**
  ```json
  {
    "optional_assets": ["CITATION.cff", "sbom.spdx.json"],
    "runtime_fingerprint": "04c59e4a30dcf2e78b0f055c9b1f805bbc16de057b891f393b4227405fbe3b7f",
    "tag": "v1.0.0",
    "target_sha": "4d2113af9af535024cd6e2e482691cb41097d0b1",
    "version": "1.0.0"
  }
  ```
- **Generated Assets in Output Directory:**
  - `minimal` profile: `audit-report.md`, `runtime-fingerprint.txt`, `release-notes.md` (`optional_assets: []`).
  - `release` profile: `audit-report.md`, `runtime-fingerprint.txt`, `release-notes.md`, `sbom.spdx.json` (`optional_assets: ["sbom.spdx.json"]`).
  - `scientific` profile: `audit-report.md`, `runtime-fingerprint.txt`, `release-notes.md`, `CITATION.cff`, `sbom.spdx.json` (`optional_assets: ["CITATION.cff", "sbom.spdx.json"]`, with `sbom.spdx.json` validated as `SPDX-2.3` / `SPDXRef-DOCUMENT` containing indexed `numpy` and `scipy` packages).

### 7.2 Evidence Demarcation (`Observed` / `Inferred` / `Unverified`)

- **Observed** —
  - **Command:** `python3 -m unittest tests/test_p6_185_release_citation_guidance.py tests/test_disposable_target_release_validation.py -v`
  - **Environment:** Local Linux worktree (`Python 3`, `git`), temporary disposable Git repository (`octo/disposable-sci-release`), exit status `0`.
  - **Explicit-tag & profile execution:** `prepare_release_payload` refused missing or mismatched tags (`--tag v9.9.9`) with `ReleasePromotionError` and wrote zero files. With `--tag v1.0.0`, it produced exact asset sets for `minimal` (`audit-report.md`, `runtime-fingerprint.txt`, `release-notes.md`), `release` (adding `sbom.spdx.json`), and `scientific` (adding `CITATION.cff` and `sbom.spdx.json`), including the mandatory reproducibility disclaimer in `release-notes.md`.
  - **Fail-closed SBOM abort:** When the Dependency Graph API returned `HTTP 404` (disabled) or `HTTP 200` with `packages: []` (zero packages) under `allow_fallback=False, require_packages=True`, `prepare_release_payload` raised `ReleasePromotionError` with step-by-step recovery guidance before creating any output directory, Git tag, or GitHub Release.
  - **DOI state transitions & guards:** `DoiStateMachine` verified `unselected -> requested -> pending -> issued` (`10.5281/zenodo.1234567`) and `unavailable` transitions, rejected `unselected -> issued` direct jumps, and rejected recording any DOI string in `CITATION.cff`, `docs/release-control.json`, or `docs/release-evidence.json` prior to `issued`.
  - **Container workflow rendering:** `install_github_workflows.render` produced `.github/workflows/container-publish.yml` (`# sciflow-cicd workflow-template=container-publish version=1`) targeting `ghcr.io/${{ github.repository }}:${{ github.sha }}` with `contents: read`, `packages: write`, and 40-character commit SHA pins (`actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683`, `docker/login-action@9780b0c442fbb1117ed29e0efdff1e18412f7567`, `docker/build-push-action@471d1dc4e07e5cdedd4c2171150001c434f0b7a4`), while default workflow installation staged zero container workflows.
- **Inferred** —
  - Because `.github/workflows/release-promotion.yml` executes `scripts/release_promotion.py` in step `Validate release evidence and prepare assets` prior to step `Create an annotated immutable tag at the merged commit` and step `Publish GitHub Release with audit evidence`, any `CITATION.cff`, DOI state, or SBOM validation error halts the GitHub Actions job before `POST /repos/$GITHUB_REPOSITORY/git/tags` or `gh release create` can run.
- **Unverified** —
  - **External Zenodo webhook minting and manual deposit UI (`https://zenodo.org` / `https://zenodo.org/deposit/new`):** Live OAuth account linking between GitHub and Zenodo, live Zenodo webhook archive ingestion, and live DOI handle registration on `https://doi.org/<doi>` require an external Zenodo account and were not executed in the local test environment.
  - **Live container registry push to `ghcr.io`:** While `.github/workflows/container-publish.yml` rendering, SHA-pin verification, and Setup Pull Request staging were verified, live Docker daemon image building and pushing to `ghcr.io/<owner>/<repo>` on a GitHub-hosted runner were not executed in this local run.
