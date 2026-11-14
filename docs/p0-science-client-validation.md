# P0 scientific client validation

This is the recorded exercise for issue #110, spec #103, and P6.4 (#182), observed on
2026-10-20 UTC and verified against the current client tutorial contract. The target is the private, disposable scientific repository
[`lispz1610/gh-setup-science-p0-20261020-2308`](https://github.com/lispz1610/gh-setup-science-p0-20261020-2308).
It remains available for owner inspection. No secret was stored in the target,
Policy, or this record.

## Selected state and Dry-Run parity

The Policy in `examples/p0-science-validation/policy.json` selects Wiki and a
small reproducibility methods document. The document's SHA-256 is
`8392d5370a9e55743e7af1356bc41eacdf5ffccf8b38dcfe86c4043a41c6f5a9`.
The target started with Wiki disabled, no `docs/methods.md` on `main`, and no
open pull requests.

`./gh-setup dry-run --policy examples/p0-science-validation/policy.json`
returned exit 0 and exactly these proposed operations:

| Stage | Operation | Target | Status |
| --- | --- | --- | --- |
| features | PATCH `has_wiki=true` | repository settings | planned |
| files | create | `docs/methods.md` | planned |

Interactive Setup was run against the same target. Its actual prompts selected
Wiki and the same local methods document, with every other capability declined.
The generated Policy had the same two stages and destination. Its Dry-Run
displayed exactly the same two operations. Apply was declined at the confirmation
prompt (`Apply these changes? [y/N]: `), which returned exit 0. A fresh GitHub read still showed Wiki disabled,
no open pull requests, and no methods file on `main`. This establishes zero
observable mutations for both Dry-Runs and the declined interactive apply.

## Apply and owner review

The first Headless Mode apply was interrupted by the local command timeout after
GitHub accepted the Wiki setting, before the command produced a result. The
remote read showed `has_wiki=true` and no pull request at that point. Retrying
the same Policy returned exit 0 and created one draft
[`Setup Pull Request #1`](https://github.com/lispz1610/gh-setup-science-p0-20261020-2308/pull/1)
on `codex/workflows-c7e7c9b72417`. Its diff contains only the selected
`docs/methods.md` (five added lines). The file is present on that branch and
still absent from `main`. The direct Wiki setting is verified by a GitHub read;
the file remains pending owner review and merge. The interrupted first command
has no complete client result, so its intermediate status is inferred from
these remote reads, not reported as a successful client response.

A further Headless Mode apply of the identical Policy returned exit 0 with zero
changes and the existing Setup Pull Request URL. GitHub still showed exactly
one open draft pull request. No pull request was merged.

## Unavailable capability

An additional read-only Policy selecting CodeQL returned exit 2. The Dry-Run
reported one blocked `codeql` inspection with availability `inaccessible`:
GitHub returned HTTP 403 for the default setup endpoint because code scanning
was not enabled for this private repository. The client did not infer a disabled
value or attempt a CodeQL write. Interactive Inventory exposed the same optional
read as unavailable after the import consistency fix covered by the public
client regression test.

## Evidence classification (`Observed` / `Inferred` / `Unverified`)

- **Observed**:
  - `./gh-setup dry-run --policy examples/p0-science-validation/policy.json` exited `0` with 2 planned changes (`features` PATCH `has_wiki=true` and `files` create `docs/methods.md`) and zero remote mutations.
  - Interactive Setup (`./gh-setup`) declined at `Apply these changes? [y/N]: ` exited `0` with zero remote mutations.
  - Headless apply retry exited `0`, verified `has_wiki=true`, and opened draft [`Setup Pull Request #1`](https://github.com/lispz1610/gh-setup-science-p0-20261020-2308/pull/1) on `codex/workflows-c7e7c9b72417`.
  - Repeat Headless apply exited `0` with `changes: []` and reused the open draft Setup Pull Request without creating duplicates.
  - Read-only Policy selecting `codeql` on the private repository without GHAS exited `2` with `status: "blocked"` and `availability: "inaccessible"` (HTTP 403).
- **Inferred**:
  - The intermediate state of the timed-out first apply (`has_wiki=true` applied before PR creation) is inferred from subsequent `GET /repos/lispz1610/gh-setup-science-p0-20261020-2308` and `GET /pulls` reads rather than a completed client stdout payload.
- **Unverified**:
  - Per-request GitHub server-side audit log inspection for the timed-out first apply (requires an Enterprise audit log stream unavailable on the personal account).

## Reproduction and evidence limits

Use the authenticated `GITHUB_TOKEN` in the environment; never paste it into a
Policy or command log. Run the Policy Dry-Run, then inspect the two selected
operations. Under the current Headless Mode contract, save that result and
pass it to apply:

```bash
./gh-setup dry-run --policy examples/p0-science-validation/policy.json > .local/setup-dry-run.json
./gh-setup apply --policy examples/p0-science-validation/policy.json --dry-run-result .local/setup-dry-run.json
```

The live exercise above predates the required `--dry-run-result` argument; its recorded
GitHub evidence and outcomes are historical. Interactive Setup can be run with
the same selections and apply declined. The public client regression test runs
with `python3 -m unittest discover -s tests -p test_science_client_exercise.py -v`.

GitHub repository state and the Setup Pull Request diff verify the final state.
The available account did not provide a per-request server audit log, so this
record does not claim that every HTTP request in the interrupted first apply
was independently audited. The fake API tests verify mutation-free Dry-Run and
planned-operation restrictions at the client boundary.
