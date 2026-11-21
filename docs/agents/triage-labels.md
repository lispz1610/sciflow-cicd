# Triage labels

| Role | GitHub label | Meaning |
| --- | --- | --- |
| `needs-triage` | `needs-triage` | Needs maintainer evaluation |
| `needs-info` | `needs-info` | Waiting for reporter details |
| `ready-for-agent` | `ready-for-agent` | Ready for an agent to implement |
| `ready-for-human` | `ready-for-human` | Requires human implementation |
| `wontfix` | `wontfix` | Will not be implemented |

Use the GitHub label corresponding to the role named by a skill.

## Repository Classification vs. Lifecycle Triage Labels

Setup Profiles and Universal Common Baseline Governance synchronize the five canonical labels (`needs-triage`, `bug`, `enhancement`, `documentation`, `ready-for-agent`) to ensure standardized classification and automation readiness across repositories.

- **Issue Lifecycle Triage Labels** (listed in the table above: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`) represent the state of an issue throughout triage and execution workflows.
- **Repository Classification Labels** (`bug`, `enhancement`, `documentation`) categorize incoming issues by nature and work type.
- Shared labels like `needs-triage` and `ready-for-agent` bridge classification and lifecycle states to signal both maintainer evaluation needs and autonomous agent dispatch eligibility.
