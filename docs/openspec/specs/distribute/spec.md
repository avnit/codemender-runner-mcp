<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-10-20' }
*-->

<!--
sdd_spec_id: SPEC-DIST
sdd_implements: None
sdd_extends: None
sdd_level: COMPONENT
sdd_archetype: CI_CD_WORKFLOW
sdd_cujspec_product: codemender-runner
-->

# SPEC-DIST: Customer Repository Distribution, GitHub Deployment Models & Cross-Repo Action Resolution

**Implements Spec:** None

**Extends Spec:** None

**Governing Journeys:**
- **Platform / Security Administrator (`persona-platform-admin`)**: As an organization administrator, I want to fork `codemender-runner` once into my GitHub Organization and enforce customer-facing workflows in `.github/workflows/*` across repositories via GitHub Organization Repository Rulesets (*"Require workflows to pass before merging"*), or reuse a public organization's composite actions in `.github/actions/*` without copying files into downstream repositories.
- **Individual Repository Owner (`persona-repo-owner`)**: As a personal account user or single-repository owner without Organization Rulesets, I want to call workflows in `.github/workflows/*` as reusable workflows (`workflow_call`) or invoke composite actions in `.github/actions/*` directly in a workflow step.
- **CodeMender Maintainer (`persona-maintainer`)**: As a maintainer developing on `codemender-runner`, I want pull requests opened on `codemender-runner` to dogfood the PR branch's own `packages/{codemender,launcher,repohost}` changes live while using the same `.github/workflows/*` and `.github/actions/*` entrypoints that customers use.

## Overview

This specification defines the customer repository distribution requirements, GitHub deployment models, workspace-vs-action package resolution behavior, cross-repository composite action and container image resolution, and private fork ownership configuration (`scripts/set-fork-owner.sh` / `make init-fork`) for `codemender-runner`.

With the three-package Python workspace (`packages/{codemender,launcher,repohost}`) and root `.github/` entrypoints ([ADR-0008](../../../adr/ADR-0008-repository-layout-and-package-structure.md)), `codemender-runner` is distributed and consumed directly through **native GitHub repository forking, Organization Repository Rulesets, `workflow_call` reusable workflows, and cross-repository composite action invocations** across `.github/workflows/*` and `.github/actions/*`.

### Supported GitHub Deployment Models

1. **Model 1 — Self-Dogfooding (`pull_request` on `codemender-runner`):**
   - Pull requests on `codemender-runner` trigger `.github/workflows/*` directly.
   - Because `packages/` is present in the checked-out repository workspace on `codemender-runner`, composite actions in `.github/actions/*` install `packages/{codemender,launcher,repohost}` from the checked-out workspace so pull requests test their own code changes live.
2. **Model 2 — Organization-Wide Policy Enforcement (Fork + Org Repository Rulesets & Public Cross-Org Reuse):**
   - **Same-Org Ruleset Enforcement & Event Scope (`pull_request` Scanning & Fixes vs. PR-Comment `@cm-runner verify`):** Organizations fork `codemender-runner` into `<org>/codemender-runner` and configure a GitHub Organization Repository Ruleset (*"Require workflows to pass before merging"*) targeting customer-facing workflows in `.github/workflows/*` across selected repositories in `<org>`. Because GitHub Organization Repository Rulesets trigger required workflows on branch/merge-gate events (`pull_request`, `push`, `merge_group`), both **`find-diff` PR scanning** and **automatic `fix` generation (posting inline PR review suggestions/comments and sticky summary comments)** work with **zero workflow files** in target repositories. By contrast, on-demand verification triggered by replying to a PR comment (**`@cm-runner verify`** on `pull_request_review_comment` / `issue_comment`) is ignored by GitHub Organization Repository Rulesets and requires a thin caller workflow in the target repository (Model 3) or an organization-level GitHub App / webhook dispatcher.
   - **Model 2a — Public Cross-Org Consumption (`Org A -> Org B`) & Live `@main` Trade-Off:** Because customer-facing workflows in `.github/workflows/*` invoke composite actions via full `<owner>/codemender-runner/.github/actions/*` coordinates rather than workspace-relative paths, when `<org-A>/codemender-runner` is **Public**, `<org-B>` can fork the repository for Organization Rulesets and reference `<org-A>`'s public composite actions and public GHCR runner image with **zero edits**. However, leaving the action reference as `<org-A>/codemender-runner/.github/actions/*@main` resolves `<org-A>`'s `main` branch live on every run (so breaking changes merged to upstream `main` immediately affect `<org-B>`). Organizations requiring version isolation should either pin `@<ref>` to a release tag/commit SHA or use Model 2b (`scripts/set-fork-owner.sh` / `make init-fork`).
   - **Model 2b — Fork-Isolated & Private/Internal Fork Ownership (`scripts/set-fork-owner.sh`):** When `<org-B>` wants to isolate its workflows from live upstream `@main` changes (updating only when `<org-B>` explicitly syncs its fork), customizes `.github/actions/*`, or operates a Private/Internal fork (with GitHub Actions *"Accessible from repositories in the organization/user"* enabled), operators run the fork ownership helper (`scripts/set-fork-owner.sh` / `make init-fork`) to point the `<owner>/codemender-runner` action references in `.github/workflows/*` at `<org-B>/codemender-runner`.
3. **Model 3 — Personal Account, Single-Repo & Comment-Triggered Adoption (`workflow_call` & Direct Action `uses:`):**
   - Customer-facing workflows in `.github/workflows/*` support both `pull_request` and `workflow_call`, allowing personal repositories (where GitHub Organization Rulesets are unavailable), individual repositories, or repositories requiring PR-comment-triggered workflows (such as **`@cm-runner verify`** on `pull_request_review_comment` / `issue_comment`) to include a thin caller workflow in `.github/workflows/` that calls `.github/workflows/*` via `workflow_call` or invokes `.github/actions/*` directly while keeping `.github/actions/*` and `packages/` centralized in `codemender-runner`.

This specification is governed by and references:
- [SPEC-GHA](../github-actions/spec.md) — GitHub Actions Orchestration & Execution Backends.
- [SPEC-GHA-FIX](../github-actions-fix/spec.md) — Fixes on Pull Requests.
- [SPEC-WIF](../wif/spec.md) — Unified Terraform Root & Composable Mode-Aware WIF Module (`terraform/` & `terraform/modules/{wif,gcp}`).
- [ADR-0002: Machine-Readable Output Contract](../../../adr/ADR-0002.md)
- [ADR-0003: Environment Variable Scrubbing When Running `cm`](../../../adr/ADR-0003-environment-scrubbing.md)
- [ADR-0008: Repository Layout, Three-Package Workspace, and Runtime Architecture](../../../adr/ADR-0008-repository-layout-and-package-structure.md)
- [CodeMender Runner System Design](../tech/systems/codemender-runner/design.md)

---

## Requirements

### REQ-0001: Dual-Mode Python Package Resolution in `.github/actions/*` {#req-0001}

- **CUJ UUID:** cuj-dist-dual-mode-pkg-dir

Composite actions in `.github/actions/*` MUST resolve and install the monorepo Python packages (`codemender`, `launcher`, and `repohost`) across both self-dogfooding runs on `codemender-runner` and runs on external target repositories that do not contain `packages/`:

1. **Workspace Precedence When Self-Dogfooding:**
   - When the checked-out repository workspace contains the CodeMender `packages/` tree (such as during a pull request on `codemender-runner`), the composite action MUST install the Python packages from the checked-out repository workspace so changes in the pull request are exercised directly.
2. **Action-Tree Fallback on External Target Repositories:**
   - When the checked-out repository workspace does not contain the CodeMender `packages/` tree (such as when scanning an external application repository), the composite action MUST resolve and install the Python packages from the action's own repository tree so the scan succeeds without requiring `packages/` to exist in the target repository.

#### Scenario: Pull request on `codemender-runner` installs packages from the checked-out workspace
- **GIVEN** a workflow runs on a repository checkout that contains `packages/`
- **WHEN** a composite action in `.github/actions/*` installs Python dependencies
- **THEN** the action MUST install `codemender`, `launcher`, and `repohost` from the checked-out repository workspace.

#### Scenario: External target repository without `packages/` installs from the action's repository tree
- **GIVEN** a workflow runs on an external application repository (`org/app-repo`) where `packages/` does not exist in the checked-out workspace
- **WHEN** a composite action in `.github/actions/*` installs Python dependencies
- **THEN** the action MUST resolve and install `codemender`, `launcher`, and `repohost` from the action's own repository tree
- **AND** dependency installation MUST succeed without `packages/` existing in `org/app-repo`.

---

### REQ-0002: Reusable Workflow Triggers & Cross-Repository Action Resolution in `.github/workflows/*` {#req-0002}

- **CUJ UUID:** cuj-dist-workflow-triggers-and-uses

Customer-facing workflows in `.github/workflows/*` MUST support all three GitHub deployment models without requiring target repositories to copy `.github/actions/*`:

1. **Dual Event Triggers (`pull_request` & `workflow_call`) and Organization Ruleset Event Scope:**
   - Customer-facing workflows in `.github/workflows/*` MUST support both direct `pull_request` events (for self-dogfooding on `codemender-runner` and Organization Repository Ruleset enforcement of **`find-diff` scanning** and **automatic `fix` review comments** across an organization with zero workflow files in target repositories) and `workflow_call` events (for personal account, single-repository, and caller-workflow adoption).
   - Because GitHub Organization Repository Rulesets only trigger required workflows on branch/merge-gate events (`pull_request`, `push`, `merge_group`) and ignore PR comment events (`pull_request_review_comment`, `issue_comment`), target repositories that require PR-comment-triggered verification (**`@cm-runner verify`**) MUST use a thin caller workflow in the target repository (Model 3) or an organization-level GitHub App / webhook dispatcher while still resolving `.github/actions/*` and `packages/` from the central `codemender-runner` repository.
2. **Cross-Repository Action References & Version Isolation:**
   - Workflow steps in `.github/workflows/*` that invoke composite actions in `.github/actions/*` MUST reference them using full repository coordinates (`<owner>/<repo>/.github/actions/<action>@<ref>`) rather than workspace-relative paths (`./.github/actions/<action>`).
   - This ensures that when an Organization Repository Ruleset or `workflow_call` runs a workflow against a downstream target repository that does not contain `.github/actions/*` in its workspace, the composite action still resolves from `<owner>/<repo>`.
   - Because GitHub resolves `@<ref>` against `<owner>/<repo>` live on every workflow run, operators who want to isolate their organization from unreviewed upstream `@main` changes SHOULD either pin `@<ref>` to a release tag/commit SHA or re-point `<owner>/<repo>` to their own fork via `scripts/set-fork-owner.sh` (`make init-fork`, `REQ-0004`).
3. **Graceful Skip for Unconfigured Repositories:**
   - When required cloud authentication configuration (project ID, Workload Identity Provider, or Service Account) is absent in a target repository, customer-facing workflows in `.github/workflows/*` MUST emit an informational notice, skip checkout and scan execution steps, and exit with status `0` so unconfigured repositories in an organization are not blocked from merging.

#### Scenario: Organization Ruleset runs a workflow on a target repository that lacks `.github/actions/*`
- **GIVEN** an Organization Repository Ruleset enforces a workflow from `<owner>/codemender-runner/.github/workflows/*` on `target-org/service-a`
- **AND** `target-org/service-a` does not contain `.github/actions/*` in its repository tree
- **WHEN** a pull request is opened on `target-org/service-a`
- **THEN** the workflow MUST check out `target-org/service-a`
- **AND** MUST resolve the composite action from `<owner>/codemender-runner/.github/actions/*` without path-not-found errors.

#### Scenario: Personal or single repository invokes a workflow via `workflow_call`
- **GIVEN** a caller workflow in `user/my-app` invokes a workflow in `<owner>/codemender-runner/.github/workflows/*` via `workflow_call`
- **WHEN** a pull request is opened on `user/my-app`
- **THEN** the called workflow MUST execute using the caller repository's configured variables and secrets.

#### Scenario: Unconfigured repository in an organization skips execution cleanly
- **GIVEN** an Organization Repository Ruleset triggers a workflow in `.github/workflows/*` on a repository that has not configured CodeMender cloud credentials
- **WHEN** the workflow runs
- **THEN** the workflow MUST emit a notice indicating CodeMender is not configured, skip the scan steps, and succeed with exit status `0`.

---

### REQ-0003: Automatic Cross-Org Default `runner_image` Resolution {#req-0003}

- **CUJ UUID:** cuj-dist-default-runner-image

When a composite action in `.github/actions/*` runs in local container mode and no runner container image is explicitly provided via action inputs or repository variables:

1. The action MUST default the container image reference to the GHCR image belonging to the repository that hosts the invoked action (falling back to the current workflow repository when invoked locally), normalized to lowercase.
2. When `<org-B>` invokes a public composite action hosted in `<org-A>/codemender-runner`, the default image MUST resolve to `<org-A>`'s published GHCR image without requiring `<org-B>` to rebuild or republish the container image.
3. When `<org-B>` invokes the composite action from its own fork `<org-B>/codemender-runner`, the default image MUST resolve to `<org-B>`'s GHCR image.

#### Scenario: Cross-org caller automatically uses the upstream action repository's GHCR image
- **GIVEN** a workflow in `org-b/app-repo` invokes a local-mode composite action from `org-a/codemender-runner/.github/actions/*` without specifying a custom runner image
- **WHEN** the action resolves the default container image
- **THEN** the image reference MUST resolve to `ghcr.io/org-a/codemender-runner:latest` in lowercase.

---

### REQ-0004: Private Fork Ownership Configuration (`scripts/set-fork-owner.sh` & `make init-fork`) {#req-0004}

- **CUJ UUID:** cuj-dist-set-fork-owner

To support private or internal organization forks and forks that customize `.github/actions/*` while keeping tracked `.github/workflows/*` files free of Terraform state mutations:

1. **Fork Ownership Helper:**
   - The repository MUST provide a fork ownership configuration utility (`scripts/set-fork-owner.sh` and `make init-fork`) that updates `<owner>/<repo>/.github/actions/*` references across `.github/workflows/*` to match the fork's owner and repository name.
   - The utility MUST accept an explicit `<owner>` or `<owner>/<repo>` argument, or automatically detect `<owner>/<repo>` from the local Git `origin` remote when requested, and apply updates idempotently.
   - If the supplied owner/repository argument is malformed, the utility MUST report an error and exit with a non-zero status code without modifying any workflow files.
2. **No Terraform Mutation of Tracked Workflow Files:**
   - Infrastructure automation under `terraform/` MUST NOT overwrite or mutate tracked `.github/workflows/*` files in the repository.

#### Scenario: Updating workflow action references for a private organization fork
- **GIVEN** an administrator forks the repository into `acme-corp/codemender-runner`
- **WHEN** the administrator runs the fork ownership helper for `acme-corp`
- **THEN** all `<owner>/<repo>/.github/actions/*` references in `.github/workflows/*` MUST be updated to `acme-corp/codemender-runner/.github/actions/*`
- **AND** the command MUST succeed with exit status `0`.

#### Scenario: Rejecting a malformed owner argument
- **GIVEN** a caller runs the fork ownership helper with a malformed owner argument containing spaces or invalid path characters
- **WHEN** input validation runs
- **THEN** the utility MUST exit with a non-zero status code and leave `.github/workflows/*` unmodified.

---

### REQ-0005: Host Orchestrator CLI Entrypoint (`cm-github`) {#req-0005}

- **CUJ UUID:** cuj-dist-cm-github-entrypoint

To allow composite actions in `.github/actions/*` to invoke the GitHub orchestrator cleanly from any working directory after installing the workspace packages:

1. `packages/repohost` MUST expose the `cm-github` console script entrypoint upon package installation.
2. Invoking `cm-github` MUST dispatch the GitHub pull request orchestration workflow without requiring callers to reference internal module file paths.

#### Scenario: Installing `packages/repohost` exposes `cm-github` on `PATH`
- **GIVEN** a composite action in `.github/actions/*` installs `packages/codemender`, `packages/launcher`, and `packages/repohost[github]`
- **WHEN** the action's execution step runs
- **THEN** `cm-github` MUST be available as an executable command on `PATH`.
