# Open Code Review setup

This language describes the planned review product, which supplies automated feedback to pull requests in a selected repository.

## Language

**Advisory review**:
Automated feedback posted on a pull request as comments. It does not approve the pull request or decide whether it may merge.
_Avoid_: Automated approval, merge gate

**Target repository**:
The repository whose pull requests receive advisory reviews.
_Avoid_: Setup repository

**Review policy**:
The target repository owner's choices for review provider, model, triggering events, and resource limits.
_Avoid_: Merge policy

**Review run**:
One attempt to review a pull request at a particular commit in response to a selected event.
_Avoid_: Pull request approval

**Review workflow**:
The target repository's automation that starts advisory review runs for selected pull request events.
_Avoid_: CI workflow
