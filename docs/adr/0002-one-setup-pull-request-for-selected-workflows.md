# Use one Setup Pull Request for selected workflows

The existing generic workflow installer and Open Code Review Setup stage workflows through separate draft pull requests. The integrated `sciflow-cicd` proposes every selected workflow, its trusted reporter runtime, and the optional reviewed Matt Pocock agent-skill bundle through one Setup Pull Request, giving the owner one diff and one merge decision. The shared staging process preserves per-file ownership and drift checks, verifies that the branch contains only selected files, and avoids overwriting unrelated changes. Advisory Review stages `scripts/review_issue_reporter.js` with its workflow and requires a separate blob SHA acknowledgement when updating that managed runtime. Agent skills are copied as inert files under `.agents/skills/`; a source manifest records their pinned upstream revision and hashes. A later bundle update may replace a file only when its current bytes match the prior generated manifest. Diverged files block the whole staged bundle and remain untouched.

Selected caller-authored Policy `files` also join this Setup Pull Request.
That changes the delivery of the version 1 `files` stage from a direct
default-branch write to an owner-reviewed change without changing the version
1 Policy document format. The owner must merge the Setup Pull Request before
those files take effect. See the client migration note for this behavior and
the retained direct resource commands.
