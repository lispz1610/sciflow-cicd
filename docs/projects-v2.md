# Owner-scoped Projects V2 setup

`scripts/setup_github_projects.py` reconciles one explicitly selected project for a GitHub user or organization. It is separate from repository feature setup. It never authors Issue or PR text, changes document bodies, or discovers items to add. The caller supplies GraphQL node IDs for the exact Issues and PRs to link.

## Run

Set `GITHUB_TOKEN` in the environment. A classic personal access token needs `read:project` for a dry run or `project` for writes; access to the selected Issues and PRs is also required. GitHub App and fine-grained token permissions depend on the owner and content resources. The command reports API permission failures without printing the token.

```bash
python3 scripts/setup_github_projects.py --policy examples/projects-policy.json
python3 scripts/setup_github_projects.py --policy examples/projects-policy.json --apply
python3 scripts/setup_github_projects.py --policy examples/projects-policy.json --apply
```

Replace the example owner and node IDs before running. The first command only prints JSON planned changes. The first apply prints `project_id`; copy that ID into a `project_id` key in the policy to pin the managed project. The second apply should print `"changes": []`. The project ID is the stable identity after creation; the title is checked as a guard against targeting a renamed or different project. If the title needs to change, handle that intentionally outside this stage and update the policy together.

The policy uses `version: 1`, `owner_type` (`user` or `organization`), `owner` login, `title`, optional `project_id`, optional `fields`, and optional `items`. Supported custom field types are `TEXT`, `NUMBER`, `DATE`, and `SINGLE_SELECT`. Single-select options require a name, color, and description. Item `values` use field names declared in `fields`; dates use `YYYY-MM-DD`. The tool does not delete fields, options, items, projects, or any unlisted resource. It preserves IDs of existing single-select options when updating their properties and keeps unlisted options.

Project, field, item, and value connections are paginated. Before any mutation, the command checks owner/project identity, all selected Issue or PR node IDs and types, existing fields, and existing items. Duplicate project titles, fields, options, or items stop the run rather than choosing arbitrarily. It creates missing fields, links missing items, and updates selected item values in separate calls, then reads back the resulting state. A run can partially apply if a later API call fails; rerun after fixing permissions or rate limits. Concurrent modifications by other actors can require another run.

The script currently targets GitHub.com (`https://api.github.com/graphql`). It does not install workflows, configure project views, set collaborators, or change repository Projects feature flags.

## Sources

- [GitHub's Projects API guide](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects)
- [GitHub GraphQL Projects reference](https://docs.github.com/en/graphql/reference/projects)
