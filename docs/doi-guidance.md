# DOI Issuance and Deposit Guidance for Scientific Software Releases

This guide defines the lifecycle, governance rules, confirmation surfaces, and deposit workflows for registering Digital Object Identifiers (DOIs) for software releases in `sciflow-cicd` repositories. It covers both public repositories using Zenodo GitHub automation and private-source repositories requiring manual deposit.

---

## 1. Core Governance Principle

> [!IMPORTANT]
> **Verified Issuance Rule:** A DOI string is **never recorded** in citation metadata (`CITATION.cff`), the base-branch **Control Record** (`docs/release-control.json`), or active release evidence (`docs/release-evidence.json`) **before issuance is verified** (`doi_state: "issued"`).

Speculative, placeholder, or pre-allocated DOI strings must never be recorded into release artifacts. Recording an unverified DOI creates false citations, invalidates release evidence, and breaks downstream indexing. Automation in `scripts/doi_guidance.py` (`DoiState`, `DoiStateMachine`, `validate_citation_cff_doi`, `validate_release_doi_metadata`) and `scripts/release_promotion.py` fails closed if an unverified DOI string is declared while the state is `unselected` (not-selected), `requested`, `pending`, or `unavailable`.

---

## 2. The Five Canonical DOI Paths (`DoiState`)

`scripts/doi_guidance.py` defines five mutually exclusive DOI lifecycle states (`DoiState` enum values `"unselected"`, `"requested"`, `"pending"`, `"issued"`, and `"unavailable"`):

```
[ UNSELECTED ] ───────────► [ REQUESTED ] ───────────► [ PENDING ] ───────────► [ ISSUED ]
       │                            │                         │                         │
       │                            ▼                         ▼                         ▼
       └─────────────────────► [ UNAVAILABLE ] ◄──────────────┴─────────────────────────┘
```

1. **Unselected / Not-Selected Path (`UNSELECTED` / `"unselected"`):** The repository or maintainer has not selected DOI registration (`not-selected`). No release DOI is expected, recorded, or tracked.
2. **Requested Path (`REQUESTED` / `"requested"`):** Registration intent has been initiated (such as enabling the `Zenodo -> GitHub` repository toggle or opening a manual deposit draft at `https://zenodo.org/deposit/new`). No DOI has been minted yet, and **no DOI string may be recorded**.
3. **Pending Path (`PENDING` / `"pending"`):** A GitHub Release webhook event has fired or manual deposit files have been uploaded and submitted, awaiting minting, verification, or registry indexing. **No DOI string may be recorded** during this phase (including reserved or pre-registration strings).
4. **Issued Path (`ISSUED` / `"issued"`):** The DOI has been minted by the registration authority, validated against the canonical bare DOI pattern `^10\.\d{4,9}/\S+$` (`DOI_PATTERN`), and verified by the maintainer to resolve at `https://doi.org/<doi>`. **Only in the `issued` state** is a DOI string recorded—and it becomes **mandatory** across `CITATION.cff`, `docs/release-control.json`, and `docs/release-evidence.json`.
5. **Unavailable Path (`UNAVAILABLE` / `"unavailable"`):** Deposit registration was rejected, the external DOI service failed or is unreachable, or the release was retracted. **No DOI string may be recorded.**

| `DoiState` Enum (`value`) | Option / Lifecycle Phase | DOI String Allowed? | Maintainer Confirmation & File State |
| :--- | :--- | :--- | :--- |
| **`UNSELECTED`** (`"unselected"` / `not-selected`) | Not opted-in (`not-selected`) | **No** (must be omitted / `null`) | Default when DOI registration is not needed. Omit `doi` from `CITATION.cff`, `docs/release-control.json`, and `docs/release-evidence.json`. Release notes state: `DOI registration: unselected (this release does not register a DOI).` |
| **`REQUESTED`** (`"requested"`) | Registration initiated | **No** (must be omitted / `null`) | Maintainer toggled `Zenodo -> GitHub` **ON** or created a draft at `https://zenodo.org/deposit/new`. Keep `doi` omitted in all repository files. Release notes state: `DOI registration: requested (deposit registration initiated; no DOI is recorded prior to verification).` |
| **`PENDING`** (`"pending"`) | Deposit submitted / webhook in flight | **No** (must be omitted / `null`) | GitHub Release published or manual archive submitted; Zenodo archive job is running or indexing. Keep `doi` omitted in all repository files. Release notes state: `DOI registration: pending verification (deposit submitted; DOI string will be recorded once minted and verified).` |
| **`ISSUED`** (`"issued"`) | Verified and active | **Yes** (mandatory, matching `^10\.\d{4,9}/\S+$`) | Confirmed on the Zenodo deposit dashboard and via `https://doi.org/<doi>`. Exact matching bare DOI string (`10.xxxx/...`) is recorded in `CITATION.cff`, `docs/release-control.json`, and `docs/release-evidence.json`. Release notes state: `DOI: <doi> (verified release archive: https://doi.org/<doi>).` |
| **`UNAVAILABLE`** (`"unavailable"`) | Registration failed, rejected, or retracted | **No** (must be omitted / `null`) | External registry is unavailable, deposit was rejected, or archive was tombstoned. Keep `doi` omitted/removed (`update_citation_cff_doi` strips any `doi:` line). Release notes state: `DOI registration: unavailable (deposit was rejected, retracted, or service is unavailable).` |

### 2.1 Where Maintainers Confirm and Record DOI State

Maintainers inspect and reconcile DOI state across five concrete surfaces:

1. **Zenodo Deposit Dashboard (`https://zenodo.org/account/settings/github/` or `https://zenodo.org/deposit`):** Confirm whether the repository webhook succeeded (public repos) or the manual deposit is published (private repos), and copy the minted **Version DOI** (`10.5281/zenodo.<record_id>`).
2. **Global Handle Resolver (`https://doi.org/<doi>`):** Verify that `https://doi.org/<doi>` (for example `https://doi.org/10.5281/zenodo.1234567`) resolves via HTTP redirect to the published landing page before marking the DOI as `issued`.
3. **Citation Metadata (`CITATION.cff`):**
   - When `doi_state` is `unselected`, `requested`, `pending`, or `unavailable`: `CITATION.cff` **must not** contain a top-level `doi:` key.
   - When `doi_state` is `issued`: `CITATION.cff` **must** contain `doi: <verified_doi>` matching `^10\.\d{4,9}/\S+$` and matching the Control Record.
4. **Trusted Base Control Record (`docs/release-control.json`):** Declares `"doi_state"` (`"unselected"`, `"requested"`, `"pending"`, `"issued"`, or `"unavailable"`) and, if and only if `"doi_state": "issued"`, declares `"doi": "<verified_doi>"`.
5. **Active Release Evidence (`docs/release-evidence.json`):** Must declare the exact same `"doi_state"` and `"doi"` values as `docs/release-control.json`. Any asymmetry between `docs/release-control.json`, `docs/release-evidence.json`, and `CITATION.cff` causes `scripts/release_promotion.py` to fail closed.

---

## 3. State Machine Transitions and Guard Rules

The state machine implemented in `scripts/doi_guidance.py` (`DoiStateMachine` and `VALID_TRANSITIONS`) enforces the following transition rules:

### Valid Transitions
- **`UNSELECTED` (`"unselected"`)** &rarr; `REQUESTED`, `UNAVAILABLE`, `UNSELECTED`
- **`REQUESTED` (`"requested"`)** &rarr; `PENDING`, `UNAVAILABLE`, `UNSELECTED`, `REQUESTED`
- **`PENDING` (`"pending"`)** &rarr; `ISSUED`, `UNAVAILABLE`, `REQUESTED`, `UNSELECTED`, `PENDING`
- **`ISSUED` (`"issued"`)** &rarr; `ISSUED` (metadata updates retaining verified DOI), `UNAVAILABLE` (retraction/tombstone)
- **`UNAVAILABLE` (`"unavailable"`)** &rarr; `REQUESTED` (retry), `UNSELECTED` (reset), `UNAVAILABLE`

### Forbidden Transitions and Fail-Closed Guards
- Direct jump from `UNSELECTED` to `ISSUED` is strictly forbidden (`DoiError: Invalid transition from 'unselected' to 'issued'`)—registration must proceed through `REQUESTED` and `PENDING` verification.
- Direct jump from `REQUESTED` to `ISSUED` is forbidden—deposit submission (`PENDING`) must occur before issuance.
- Any attempt to attach a non-empty `doi` string when initializing or transitioning to `UNSELECTED`, `REQUESTED`, `PENDING`, or `UNAVAILABLE` raises `DoiError`.
- Any transition to `ISSUED` without a valid, verified bare DOI matching `^10\.\d{4,9}/\S+$` (for example passing a full URL `https://doi.org/...` or a `doi:` prefix instead of `10.5281/zenodo.1234567`) raises `DoiError`.

---

## 4. Public Repository Workflow: Zenodo Integration

For open-source, public GitHub repositories, Zenodo provides automated DOI minting triggered by GitHub Release events.

### 4.1 Concept DOI vs. Version DOI

Zenodo assigns two distinct DOIs:
1. **Concept DOI:** Represents the software project conceptually across all versions. It always resolves to the latest release on Zenodo.
2. **Version DOI:** Represents the exact, immutable snapshot of a specific release version (e.g. `v1.0.0`).

> [!NOTE]
> Specific release evidence (`docs/release-control.json`, `docs/release-evidence.json`) and `CITATION.cff` must record the **Version DOI** for that release once `issued`. Documentation or project README badges may optionally reference the Concept DOI to guide citations to the latest release.

### 4.2 Setup, Automated Minting, and Post-Minting Verification Steps

1. **Prerequisites:** Public GitHub repository and a linked [Zenodo](https://zenodo.org/) account.
2. **Enable Webhook (`Zenodo -> GitHub`):**
   - Log into Zenodo &rarr; **GitHub** settings (`https://zenodo.org/account/settings/github/`).
   - Toggle the switch for the target repository to **ON**.
   - Zenodo installs a repository webhook listening for GitHub Release publication events.
3. **State: `REQUESTED` (`"requested"`):** Once the webhook is enabled (before the GitHub Release is published), the repository DOI state is `requested`. `CITATION.cff` and `docs/release-control.json` **must not** contain a `doi` string.
4. **Promote GitHub Release (`UNSELECTED`, `REQUESTED`, or `PENDING`):** When a Release Candidate PR is merged into `main` and promoted via `.github/workflows/release-promotion.yml` with `--tag v<target_version>` (see [Automated release promotion](release-evidence.md#automated-release-promotion)), the GitHub Release is published. Because Zenodo mints the Version DOI *after* receiving the GitHub Release webhook, initial release promotion runs with `doi_state` set to `"unselected"`, `"requested"`, or `"pending"` and no `doi:` field in `CITATION.cff`.
5. **State: `PENDING` (`"pending"`):** Zenodo receives the release webhook and archives the repository zipball. The minting process is in `pending` state while Zenodo assigns and indexes the identifier.
6. **Verify Issuance (`Zenodo` + `https://doi.org/<doi>`):**
   - Inspect the Zenodo GitHub repository page or deposit dashboard to see the newly minted **Version DOI** (`10.5281/zenodo.<id>`).
   - Confirm that `https://doi.org/<doi>` resolves to the published Zenodo record.
7. **Transition to `ISSUED` (`"issued"`):**
   - Only after resolution is confirmed, update `CITATION.cff` (`update_citation_cff_doi(text, doi_state="issued", doi="<doi>")`) and set `"doi_state": "issued"` and `"doi": "<doi>"` in `docs/release-control.json` and `docs/release-evidence.json`.
8. **Honest `not-selected` / `unavailable` paths:**
   - If the public project does not use Zenodo, leave `doi_state` as `"unselected"` (`not-selected`).
   - If the Zenodo webhook fails or the service is down, record `"doi_state": "unavailable"` without a DOI string and retry via a new release or manual deposit.

---

## 5. Private-Source Repository Workflow: Manual Deposit

When code is proprietary, embargoed, or stored in a private GitHub repository, Zenodo's automated GitHub webhook cannot access private release archives or source trees. Maintainers use the **private-source manual deposit** path at `https://zenodo.org/deposit/new` (or an institutional DataCite repository).

### 5.1 Manual Deposit Lifecycle

1. **State: `REQUESTED` (`"requested"`):**
   - Maintainer opens a new draft upload at `https://zenodo.org/deposit/new` (or institutional DataCite registry).
   - Maintainer selects the upload type (`Software`) and configures access rights appropriate for private-source governance:
     - **Restricted Access:** Metadata is public, while archive access is granted only upon approved request to authorized reviewers or collaborators.
     - **Embargoed Access:** Metadata is public immediately, while uploaded release archives remain restricted until an explicit embargo release date.
     - **Closed Access / Metadata-Only Deposit:** Release evidence (`audit-report.md`, `runtime-fingerprint.txt`, `CITATION.cff`, `sbom.spdx.json`) or a closed archive is preserved without public source download.
   - Keep `doi` omitted from `CITATION.cff`, `docs/release-control.json`, and `docs/release-evidence.json` (even if the Zenodo draft UI previews a reserved DOI button—**never** record a pre-allocated/reserved DOI before publication and resolution verification).
2. **Export Release Archive Tarball & Checksums:**
   - On the frozen release commit (`v<target_version>`), generate the source tarball or collect the promoted release assets:
     ```bash
     git archive --format=tar.gz --prefix=my-project-v1.0.0/ -o my-project-v1.0.0.tar.gz v1.0.0
     sha256sum my-project-v1.0.0.tar.gz > my-project-v1.0.0.tar.gz.sha256
     ```
3. **Upload Assets:** Upload the release archive tarball (or permitted non-source metadata bundle), SHA-256 checksum, `CITATION.cff`, and Software Bill of Materials (`sbom.spdx.json`) to the draft deposit at `https://zenodo.org/deposit/new`.
4. **State: `PENDING` (`"pending"`):** Click **Publish** on the deposit and await registry indexing. While minting/indexing is in flight, `doi_state` is `"pending"` and no DOI string is recorded in repository files.
5. **Verify Issuance:**
   - Inspect the published deposit on Zenodo.
   - Validate that the assigned Version DOI matches `^10\.\d{4,9}/\S+$`.
   - Confirm live resolution at `https://doi.org/<doi>`.
6. **State: `ISSUED` (`"issued"`):**
   - Once verified at `https://doi.org/<doi>`, record `"doi_state": "issued"` and `"doi": "<verified_doi>"` in `CITATION.cff`, `docs/release-control.json`, and `docs/release-evidence.json` using `scripts/doi_guidance.py`.
7. **Honest `not-selected` / `unavailable` paths:**
   - If a private project does not require an external DOI, keep `"doi_state": "unselected"` (`not-selected`).
   - If institutional policy forbids external archival or the deposit is rejected, set `"doi_state": "unavailable"` and omit `doi`.

---

## 6. Metadata Integration & Verification Commands

The helper utilities in `scripts/doi_guidance.py` enforce the five-state contract programmatically:

```python
from scripts.doi_guidance import (
    DoiState,
    DoiStateMachine,
    format_doi_release_notes,
    update_citation_cff_doi,
    validate_citation_cff_doi,
    validate_release_doi_metadata,
)

# 1. State machine transition (UNSELECTED -> REQUESTED -> PENDING -> ISSUED)
tracker = DoiStateMachine(DoiState.UNSELECTED)
tracker = tracker.transition(DoiState.REQUESTED)
tracker = tracker.transition(DoiState.PENDING)

# 2. Strict guard: cannot record DOI on UNSELECTED, REQUESTED, PENDING, or UNAVAILABLE
# tracker.transition(DoiState.PENDING, doi="10.5281/zenodo.1234567") -> raises DoiError!

# 3. Transition to ISSUED with verified DOI matching ^10\.\d{4,9}/\S+$
tracker = tracker.transition(DoiState.ISSUED, doi="10.5281/zenodo.1234567")

# 4. Citation metadata governance
# Strips any unverified doi: line when state is not ISSUED:
clean_cff = update_citation_cff_doi(cff_text, doi_state=DoiState.REQUESTED)

# Injects and validates matching DOI when ISSUED:
issued_cff = update_citation_cff_doi(
    clean_cff,
    doi_state=DoiState.ISSUED,
    doi="10.5281/zenodo.1234567",
)
validate_citation_cff_doi(
    issued_cff,
    doi_state=DoiState.ISSUED,
    expected_doi="10.5281/zenodo.1234567",
)
```

---

## 7. Summary Checklist for Maintainers

- [ ] Has DOI registration been selected? If not, keep `doi_state` as `"unselected"` (`not-selected`) and omit `doi`.
- [ ] While `doi_state` is `"unselected"`, `"requested"`, `"pending"`, or `"unavailable"`, verify `CITATION.cff`, `docs/release-control.json`, and `docs/release-evidence.json` do **not** contain any `doi` string.
- [ ] For public repositories, enable the `Zenodo -> GitHub` toggle before publishing the GitHub Release, then wait for the webhook archive to finish.
- [ ] For private repositories, upload the release archive or restricted/embargoed deposit manually at `https://zenodo.org/deposit/new`.
- [ ] Confirm the minted Version DOI matches `^10\.\d{4,9}/\S+$` on the Zenodo dashboard and resolves at `https://doi.org/<doi>`.
- [ ] Only after resolution is confirmed, transition `doi_state` to `"issued"` and record the exact matching DOI in `CITATION.cff`, `docs/release-control.json`, and `docs/release-evidence.json`.
- [ ] Align the release promotion profile (`minimal`, `release`, or `scientific`) with your project's publication needs (see [Release evidence](release-evidence.md)).
- [ ] If distributing container images alongside releases, explicitly opt into the `container-publish` workflow template (see [Workflow installation](workflows.md)).
