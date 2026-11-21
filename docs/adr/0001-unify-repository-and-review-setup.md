# Unify repository and review setup

Open Code Review Setup was previously extracted as an independent private product. The owner approved bringing its source and maintenance into `sciflow-cicd` so one installation and one Interactive Setup can guide repository configuration and optional Advisory Review. We chose this over orchestrating a separately installed package to give users one coherent setup flow. The integrated product will expose only the `sciflow-cicd` command, accept existing version 1 Policies, and write new version 2 Policies; review installation will retain its guarded draft pull request and owner-controlled merge. The first integrated release will support Review Setup only for private repositories; public repository support is deferred.


> [!NOTE]
> **Superseding note (Issue #50):** Advisory Review has been decoupled from the Setup Pull Request and in-repo Actions workflows. Advisory Review runs exclusively via an external systemd OCR runner (`sciflow-cicd ocr`) per Issue #50 and [Advisory Review guide](../advisory-review.md).
