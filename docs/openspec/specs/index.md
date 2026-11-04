# OpenSpec Specifications Index

This directory contains the formal OpenSpec specification (`spec.md`) and design
(`design.md`) documents governing the CodeMender Orchestrator (`cm-runner`)
capabilities.

| Spec ID                                         | Name                                                               | Level     | Status      | Primary Focus                                                                       |
| :---------------------------------------------- | :----------------------------------------------------------------- | :-------- | :---------- | :---------------------------------------------------------------------------------- |
| [SPEC-FIND-DIFF](find-diff/spec.md)             | [find-diff](find-diff/design.md)                                   | Component | Implemented | Native Impact-Aware Differential Vulnerability Scanning (`cm-runner find-diff`)     |
| [SPEC-VERIFY](verify/spec.md)                   | [verify](verify/design.md)                                         | Component | Implemented | Verify Single Finding & Emit Updated SARIF (`cm-runner verify`)                      |
| [SPEC-FIX](fix/spec.md)                         | [fix](fix/design.md)                                               | Component | Implemented | Patch One Finding and Print the Diff (`cm-runner fix`)                              |
| [SPEC-0003](runner-container-packaging/spec.md) | [runner-container-packaging](runner-container-packaging/design.md) | Component | Approved    | Lean Runner Base Container & Modular BYOI Packaging                                 |
| [SPEC-WIF](wif/spec.md)                         | [wif](wif/design.md)                                               | Component | Approved    | Unified Terraform Root & Composable Mode-Aware WIF Module (`terraform/` & `terraform/modules/{wif,gcp}`) |
| [SPEC-DIST](distribute/spec.md)                 | [distribute](distribute/design.md)                                 | Component | Implemented | Customer Repository Distribution, GitHub Deployment Models & Cross-Repo Action Resolution |
| [SPEC-GHA](github-actions/spec.md)              | [github-actions](github-actions/design.md)                         | Component | Draft       | GitHub Actions Orchestration & Dual Execution Backends (`LocalDocker` & `CloudRun`) |
| [SPEC-GHA-FIX](github-actions-fix/spec.md)      | [github-actions-fix](github-actions-fix/design.md)                 | Component | Draft       | Fixes on Pull Requests: parallel `cm-runner fix` runs, suggestions & patch comments (GitHub Action M2) |
| [SPEC-GHA-VERIFY](github-actions-verify/spec.md) | [github-actions-verify](github-actions-verify/design.md)           | Component | Draft       | On-Demand Verification on Pull Requests: `@cm-runner verify` in fix review threads (GitHub Action M3) |
| [SPEC-GITLAB-CI](gitlab-ci/spec.md)             | [gitlab-ci](gitlab-ci/design.md)                                   | Component | Draft       | GitLab CI Scanning on Cloud Run: `cm-gitlab find`, WIF credentials, snapshot sanitization, sticky MR comment & SARIF (`templates/`, `docker/gitlab.Dockerfile`, `packages/repohost/src/repohost/gitlab`) |
| [SPEC-GITLAB-CI-FIX](gitlab-ci-fix/spec.md)     | [gitlab-ci-fix](gitlab-ci-fix/design.md)                           | Component | Draft       | Fixes & Verification on Merge Requests (GitLab CI): fixes on Cloud Run posted as they finish, inline suggestions (`suggestion:-N+0`) & patch comments, on-demand `cm-gitlab verify` |
| [SPEC-WORKER](worker/spec.md)                   | [worker](worker/design.md)                                         | Component | Draft       | Unified Sandbox Container Worker Entrypoint & 4-Step Task Execution (`launcher.worker` / `cm-worker`) |
| [SPEC-ARTIFACT-CACHE](artifact-cache/spec.md)   | [artifact-cache](artifact-cache/design.md)                         | Component | Draft       | `fsspec`-Backed Atomic `ArtifactCache` & Typed `vcs-cloudrun-comms` Protocol (`launcher.cache`) |
| [SPEC-WORKFLOW](workflow/spec.md)               | [workflow](workflow/design.md)                                     | Component | Draft       | Generic CI Workflow Orchestrator & Dependency Injection Engine (`repohost.workflow`) |
| [SPEC-LAUNCHER](launcher/spec.md)               | [launcher](launcher/design.md)                                     | Component | Draft       | Host-Side Client, Workflow Orchestration & Dual Execution Backends (`launcher.base`) |

## Process & Governance

- [SDD Process Governance and Configuration (`SPEC-0000`)](process/spec.md)
- [SDD Writing Standard](process/writing_standard.md)
- [Domain Review Instructions](process/domain-review-instructions.md)
- [User Journeys Index](journeys/index.md)
- [Technical Systems Index](tech/systems/index.md)
- [Technical Components Index](tech/components/index.md)
