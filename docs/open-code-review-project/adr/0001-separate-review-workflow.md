---
status: accepted
---

# Keep the review workflow separate from CI

The target repository runs advisory reviews in a dedicated GitHub Actions workflow. The review needs a provider secret and a token that can comment on pull requests, while CI builds or tests pull request code. Keeping those jobs in separate workflows makes the trust boundary visible and prevents routine CI changes from gaining access to review credentials. The first version triggers reviews when a pull request opens, receives a new commit, or reopens; a newer run for the same pull request cancels the older one.

The installer diagnoses restricted Actions policies without changing them. A separately hosted GitHub App is reconsidered if a real target repository cannot run the workflow or measured volume justifies the additional service. The [cost comparison](../cost-comparison.md) records the assumptions behind this decision.
