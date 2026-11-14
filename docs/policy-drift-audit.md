# Policy drift audit

The scheduled `Policy drift audit` workflow compares this repository with `.github/repository-policy.json`. It opens one `needs-triage` issue when the dry run reports changes. The audit does not apply repository settings. The checked-in policy does not manage a branch ruleset.

It runs every Monday at 08:17 UTC and can also be started with **Actions → Policy drift audit → Run workflow**. The read-only comparison writes a JSON dry-run report and passes it to a separate reporting job. Only that reporting job receives `issues: write` and uses the workflow token to create an issue. A clean report creates no issue. If an open issue whose title starts with `Policy Drift Detected:` already exists, the run suppresses a duplicate. The workflow does not update or close an existing drift issue.

## Configure the audit credential

The workflow's default `GITHUB_TOKEN` permission is `contents: read`. The policy comparison job keeps that read-only permission; the separate reporting job adds `issues: write` so it can list or create the drift issue. The token still cannot inspect every setting in the example policy: GitHub requires `Administration: read` for the repository Actions-permissions endpoint, and reading merge-related settings from the repository endpoint requires Contents read and write permissions.

Create a fine-grained personal access token restricted to this repository, with:

- **Administration: read**
- **Contents: write**

The audit only reads repository settings with this credential. GitHub's documented read requirements for the Actions permissions endpoint and merge settings require these token permissions even though this workflow never applies changes. Do not give this token organization-wide access or use it for issue creation.

Add it as a repository Actions secret named `POLICY_AUDIT_TOKEN`:

1. Open the repository's **Settings → Secrets and variables → Actions**.
2. Select **New repository secret**.
3. Enter `POLICY_AUDIT_TOKEN` as the name and paste the token as the value.
4. Confirm the token owner has access to the repository and that organization policy permits the token.

Issue listing and creation use the reporting job's `GITHUB_TOKEN`, scoped to `contents: read` and `issues: write`. The comparison job has only `contents: read` and uses `POLICY_AUDIT_TOKEN` for settings reads. If the policy token is missing, the audit prints a message naming `POLICY_AUDIT_TOKEN` and the required permissions. If GitHub rejects an API read, the diagnostic asks you to verify those permissions, repository access, and organization policy.

GitHub documents the repository Actions permission endpoint's `Administration: read` requirement in [REST API endpoints for GitHub Actions permissions](https://docs.github.com/en/rest/actions/permissions#get-github-actions-permissions-for-a-repository). The [Get a repository endpoint](https://docs.github.com/en/rest/repos/repos#get-a-repository) requires Contents read and write to view merge-related settings. Issue listing and creation use the workflow token's [Issues read/write permissions](https://docs.github.com/en/rest/issues/issues).

## Troubleshooting and recovery

- **The workflow fails before producing a report:** Confirm `POLICY_AUDIT_TOKEN` exists, has Administration read and Contents write for this repository, and is permitted by organization policy. These permissions are required to read the endpoints even though the command is a dry run. Re-run the workflow after correcting the secret or access.
- **An API read is denied or reported as blocked:** Check that the token is still valid, its owner can access this repository, and the required permissions are granted. Inspect the command diagnostic in the Actions log; it does not print the token. A permissions failure means the comparison is incomplete, so treat the run as failed rather than as a clean audit.
- **A drift issue was not created:** A report with no changes is clean. For nonempty changes, check whether an open issue already starts with `Policy Drift Detected:`; duplicates are intentionally suppressed, even if the older issue is awaiting triage. Review and resolve that issue before expecting a new one.
- **The drift issue reports changes:** Review its JSON changes against the intended repository baseline. This workflow never applies the policy. If the live settings are intended, update the checked-in policy through normal review; if the policy is intended, use the appropriate `gh-setup` command with a separate reviewed apply. After the change, run the workflow again and verify it reports clean. Close the triage issue through the normal issue process once resolved.
- **Issue creation fails:** Verify that Issues are enabled and the reporting job has `issues: write`. The policy secret is only for settings reads; issue creation uses the reporting job's `GITHUB_TOKEN`.

For a local comparison, run `python3 scripts/configure_github_repo.py --repo lispz1610/github-repo-setup --policy .github/repository-policy.json --token-env POLICY_AUDIT_TOKEN` with the token exported in the environment. This command is dry-run only; it cannot be combined with `--apply`.
