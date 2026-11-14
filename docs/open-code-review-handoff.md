# Open Code Review project handoff

> [!NOTE] Historical Document
> This file is preserved as historical evidence of the 2026-10-09 handoff. The review integration has since been merged into `github-repo-setup` under the unified `gh-setup` product. See [Integrated Review Setup specification](integrated-review-setup-spec.md).

**Status:** The source was removed from `github-repo-setup` after the 2026-10-09
handoff. The independent, private [Open Code Review Setup](https://github.com/lispz1610/open-code-review-setup)
repository now owns the review integration. Access requires permission from
`lispz1610`. Its limited [v1.0.0 tag](https://github.com/lispz1610/open-code-review-setup/tree/v1.0.0)
identifies the released code. The [release record](https://github.com/lispz1610/open-code-review-setup/blob/v1.0.0/docs/release-v1.0.0.md)
and [live validation report](https://github.com/lispz1610/open-code-review-setup/blob/v1.0.0/docs/live-validation-2026-10-09.md)
show a real advisory review using unchanged project context, repeat setup
without writes, and same-PR cancellation. The owner accepted T09 with a
deferred successful `opened` and `synchronize` review, provider timeout tuning,
and live fork exercise. These cases are not represented as passed.

This file preserves the historical source snapshot and transfer boundary; it
is not an instruction to deploy a workflow. The [v1 specification](open-code-review-project/spec.md),
[implementation plan](open-code-review-project/implementation-plan.md), and
[sequential task list](open-code-review-project/tasks.md) record the original
design and acceptance criteria. The owner-accepted exception is documented
above and in the destination release record.

## Product boundary

Open Code Review is a caller-selected, bring-your-own-key GitHub Actions
integration for advisory pull-request comments. It uses GitHub as an execution
platform, but it is independent of this repository's setup product. Its
workflow does not approve PRs, merge them, create rulesets, or serve as a
required status check. The owner of a target repository chooses the LLM
provider, model, events, budget, and whether to merge the staged workflow PR.

The new project should own all Open Code Review-specific policy validation,
workflow rendering, preflight, installation, tests, examples, documentation,
and live acceptance evidence. `github-repo-setup` should retain its generic
workflow installer and merge-gate behavior without Open Code Review-specific
branches. Existing workflows already installed in target repositories are
separate remote state; extracting this code does not uninstall them.

## Source snapshot

The paths below refer to historical commit `e97c022`. Obtain a clean copy of
that commit or use `git show e97c022:PATH`; the Open Code Review-specific
files and branches are no longer present at the current tip. The source
snapshot is also available at
[commit e97c022](https://github.com/lispz1610/github-repo-setup/tree/e97c022).

| Area | Source at `e97c022` | Transfer action |
| --- | --- | --- |
| Direct CLI and preflight | `scripts/setup_open_code_review.py` | Move and remove its imports from this repository. |
| Policy example | `examples/open-code-review-policy.json` | Move, then replace placeholder provider values in the new project's examples as appropriate. Never copy credentials. |
| Dedicated guide | `docs/open-code-review.md` | Move and update paths and ownership. |
| Dedicated tests | `tests/test_setup_open_code_review.py` | Move; replace the imported `WorkflowApi` fixture with a fixture owned by the new project. |
| Rendering and installation | Open Code Review branches in `scripts/install_github_workflows.py` | Extract `_render_open_code_review` and its dispatch, pinned-commit selection, and `_review_ready` preflight guard. Keep generic PR CI and dependency-review templates here. |
| Shared API client | `GitHubApi`, `ApiError`, and `get_object` from `scripts/configure_github_repo.py` | Copy a small, independently tested client into the new project or define a deliberate shared package. Do not import the old CLI across repositories. |
| Shared workflow staging | `_file`, `install`, `render`, and policy helpers from `scripts/install_github_workflows.py` | Implement an owned staging module in the new project, retaining the current drift and readback safeguards. |
| Generic installer fixtures | Open Code Review cases in `tests/test_install_github_workflows.py` | Move relevant cases; keep tests for generic templates here. |
| Merge-gate special case | Open Code Review path/name rejection in `scripts/configure_github_repo.py` and `tests/test_configure_github_repo.py` | Remove the product-specific case after the new project clearly documents its advisory status. Preserve generic check-source validation. |
| References | `README.md`, `docs/workflows.md`, `docs/merge-gates.md`, `docs/release-verification.md`, `docs/setup-tasks.md`, `docs/scope-reduction-*.md`, and historical validation records | Remove active setup/support claims from this project. Keep historical records labeled as such and link to the new project once its location exists. |

Run `rg -n -i 'open.code.review|open_code_review|OCR_LLM' README.md docs
scripts tests examples` in the source snapshot to find references. Ignore
untracked root scratch files and `.agents/`; they are not part of the
supported implementation. `scripts/run_setup.py` and its plan format were
already retired and must not be resurrected for this migration.

## Historical contract to preserve

The entry point takes `--repo OWNER/REPO`, `--policy PATH`, and optional
`--apply`. Without `--apply`, it plans changes without writing. Apply stages
only the selected `.github/workflows/open-code-review.yml` in a draft PR;
the caller reviews and merges it. A repeat against an unchanged open PR or
merged workflow makes no new write. Existing unmanaged workflow files are
rejected. Updating a managed file requires its current blob SHA as
`expected_sha`. The installer verifies the pinned Action commit, selected
branch diff, staged content, and resulting PR. A failed write may leave a
partial branch, so recovery must remain explicit.

Policy version 1 selects a pinned `alibaba/open-code-review` Action commit
and CLI version. The current example uses Action commit
`182898cf522da3d04157b422752d028417974e19` and CLI `1.12.12`; these
are historical tested pins, not a request to upgrade. The workflow uses
`pull_request_target`, accepts `opened`, `synchronize`, or `reopened` events,
and skips fork PRs. It grants `contents: read` and `pull-requests: write`,
limits job time and requested tokens, disables raw result artifacts, and
sets `llm_extra_body` to `{}` for the tested OpenAI-compatible provider.
Optional `rule_path` must resolve to a JSON object on the trusted default
branch, never to a PR-supplied file.

Preflight reads repository Actions settings, the configured retention period,
secret metadata for `OCR_LLM_AUTH_TOKEN`, and exact values of repository
variables `OCR_LLM_URL`, `OCR_LLM_MODEL`, and `OCR_LLM_USE_ANTHROPIC`. It
requires an HTTPS endpoint URL without embedded credentials, query, or
fragment and an Actions policy that permits the pinned Action and its
dependencies. It does not read or validate the LLM key's value. The setup
token is provided through `GITHUB_TOKEN`; it needs repository/Actions
metadata read access plus Contents, Workflows, and Pull requests write for
the draft PR. Keep tokens and provider keys out of policies, logs, test
fixtures, and Git history. Logs and posted review comments can still contain
review content; the configured token budget is not a hard billing cap.

## Evidence and limits

The [historical guide](https://github.com/lispz1610/github-repo-setup/blob/e97c022/docs/open-code-review.md) and [live validation
report](https://github.com/lispz1610/github-repo-setup/blob/e97c022/docs/live-validation-2026-10-08.md) record the disposable public test
repository and PRs. In PR
[#12](https://github.com/lispz1610/codex-setup-validation-public-20261008003856/pull/12),
the review cited `UNSET-739` from unchanged `docs/quality-contract.md`; the
PR diff did not contain that value. This establishes use of that unchanged
file in one live run. The intentionally incorrect PR was closed. A later
provider HTTP 429 affected a second review round, although the first-round
finding posted. Earlier preflight rejected a missing secret and excessive
retention. Local tests cover dry run, apply, repeat, preflight failures, fork
skip, pinned rendering, and the generic-installer bypass guard.

The 2026-10-09 scope-reduction pass ran the retained suite successfully,
but a fresh Open Code Review API dry run could not be performed because this
environment lacked a suitable GitHub token. Fork behavior was tested with
fixtures, not a live fork PR. Treat the historical provider and Action pins
as evidence for the observed run only; recheck upstream behavior before a
new release.

The historical local baseline can be checked at `e97c022` with
`python3 -m unittest discover -s tests -p 'test_*.py' -q`. In the new
project, replace this with its own isolated suite and document the exact
command in its README.

## Historical transfer checklist and current ownership

1. **Establish the new project.** Record its repository URL, maintainer,
   supported Python version, license, release process, and ownership of
   security reports. Acceptance: a clean checkout has a README explaining
   the advisory contract and a runnable CLI without this repository on
   `PYTHONPATH`.
2. **Extract the implementation.** Move the direct CLI, policy schema,
   renderer, preflight, API client, and guarded draft-PR staging into the
   new project. Preserve dry run, explicit apply, drift acknowledgement,
   pinned Action verification, readback, and repeat behavior. Acceptance:
   dedicated tests run in isolation and prove no write on preflight failure
   or dry run; apply stages only the selected workflow; repeat makes no
   additional write.
3. **Transfer the safety contract and evidence.** Move the example and
   guide, document token permissions and data exposure, and reproduce
   local tests for secret/variable mismatch, retention, fork skip, trusted
   guidance, and advisory status. Acceptance: documentation contains no
   credential values; the tests have no fixture import from
   `github-repo-setup`; evidence distinguishes local fixtures from live
   observations.
4. **Validate independently.** On a disposable repository with an
   authorized provider key, run dry run, apply, inspect and merge the draft
   PR, observe one review that uses an unchanged project file, then repeat
   setup and confirm no write. Record sanitized PR/run URLs, provider quota
   limits, and the selected Action/CLI pins. Acceptance: evidence is
   reproducible and any unavailable live case is marked unverified, not
   passed. Never use the review comment as proof of a blocking merge gate.
5. **Publish the new project location.** Once the new project is ready,
   replace this source-snapshot handoff with a link to its repository from
   `github-repo-setup`. Acceptance: the new project has passed its own tests
   and this repository's retained suite still passes without importing its
   Open Code Review code.

The transfer is complete for the owner-accepted limited release. The destination
owns its CLI, policy, workflow renderer, preflight, installer, tests, operator
guide, and future review integration changes. This source repository retains no
runtime or test import from that project. The unresolved live cases remain in
the destination validation report; a later release should resolve them before
claiming all event paths are reliable.

The destination's `v1.0.0` tag points to commit
`51dd0423e43b5af33bc2e11bc3acb5cafda07ab9`. A fresh clone of that commit
passed `env PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py' -q`
(70 tests) and `env PYTHONPATH=src python3 -m open_code_review_setup --help`.
The source repository passed `python3 -m unittest discover -s tests -p 'test_*.py' -q`
(96 tests), and a scan of `scripts/` and `tests/` found no import of the
destination package. The destination release record notes that an installed
console-script smoke check was unavailable on this host because `pip` and
`ensurepip` are absent.
