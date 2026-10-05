<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-10-02' }
*-->

<!--
sdd_spec_id: SPEC-DIST
sdd_implements: None
sdd_extends: None
sdd_level: COMPONENT
sdd_archetype: CLI_TOOL_BATCH_JOB
sdd_cujspec_product: codemender-runner
-->

# SPEC-DIST: Customer Repository Distribution & Incremental Sync (`scripts/distribute.sh`)

**Implements Spec:** None

**Extends Spec:** None

## Overview

This specification defines the functional requirements, command-line interface,
file mapping and filtering rules, Git 3-way incremental sync protocol, and
customer-facing repository layout for the unified distribution script
(`scripts/distribute.sh`).

`scripts/distribute.sh` is a standalone Bash utility (`#!/usr/bin/env bash`,
`set -euo pipefail`) that extracts a curated, customer-ready subset of the
codebase from Git and synchronizes it into a target Git repository—without
requiring shared Git commit history or checking a tracking manifest file (such
as `.dist.json`) into the target repository.

The resulting distribution repository serves a **dual purpose**:

1. **Live Example Repository:** It places the CodeMender composite GitHub Action
   directly at `.github/actions/codemender/` and the pull request differential
   scan workflow at `.github/workflows/cm-merge-scan.yml` alongside
   `.github/workflows/build_runner_image.yml`, so the distribution repository
   functions out-of-the-box as a working reference implementation.
1. **Live Example Repository (`--include-top-level`):** When the maintainer
   populates the standalone distribution repository with `--include-top-level`,
   `LICENSE`, `.gitignore`, and `dist/README.md` (mapped to `README.md`) are
   placed at the repository root alongside `.github/actions/codemender/`,
   `.github/workflows/cm-merge-scan.yml`,
   `.github/workflows/build_runner_image.yml`, `.github/codemender-runner/`, and
   the root GCP Workload Identity Federation (WIF) Terraform configuration
   (`.github/terraform/` and `.github/terraform/modules/wif/`), so the
   distribution repository functions out-of-the-box as a self-contained working
   reference implementation with zero extra top-level directories.
1. **Default `.github/`-Only Sync:** When `--include-top-level` is omitted (the
   default), `scripts/distribute.sh` stages only `.github/`, allowing a target
   repository's existing root `README.md`, `LICENSE`, and `.gitignore` to remain
   untouched while syncing `.github/`. `scripts/distribute.sh` itself is never
   copied into `--target`; `README.md` in the distribution repository includes
   step-by-step instructions for manually copying `.github/actions/codemender/`
   and `.github/workflows/cm-merge-scan.yml` into additional repositories.

This specification is governed by and references:

- [SPEC-FIND-DIFF](../find-diff/spec.md) — Differential vulnerability scanning
  subcommand (`cm-runner find-diff`).
- [SPEC-FIX](../fix/spec.md) — Automated patch synthesis and remediation
  subcommand (`cm-runner fix`).
- [SPEC-WIF](../wif/spec.md) — Unified Terraform root and composable mode-aware
  Workload Identity Federation module (`terraform/` &
  `terraform/modules/{wif,gcp}`).
- [ADR-0001: Machine-Readable Output Contract](../../../adr/ADR-0001-machine-readable-output-contract.md)
- [ADR-0002: Runner Layout and Build](../../../adr/ADR-0002-runner-layout-and-build.md)
- [ADR-0003: Environment Variable Scrubbing When Running `cm`](../../../adr/ADR-0003-environment-scrubbing.md)
- [ADR-0004: Project Tech Stack & Scenario-Decoupled Monorepo Architecture](../../../adr/ADR-0004-project-tech-stack-and-repo-architecture.md)

## Requirements

### REQ-0001: Distribution CLI Surface & Pre-Flight Validation {#req-0001}

- **CUJ UUID:** cuj-dist-cli-surface

The repository MUST provide an executable Bash script at `scripts/distribute.sh`
(`chmod +x`, `#!/usr/bin/env bash`, `set -euo pipefail`) that accepts the
following command-line flags:

- `--target DIR`: **Required.** Path to the local checkout of the target Git
  repository.
- `--source-ref REF`: Optional Git reference in the source repository to package
  from (default: `HEAD`).
- `--include-top-level`: Optional flag (**off by default**) controlling whether
  the top-level files (`LICENSE`, `.gitignore`, and `dist/README.md` mapped to
  `README.md`) are staged at the root of `--target` in addition to `.github/`.
  When omitted (and not already synced in the target repository's previous
  `Codemender-Source-Commit`), only `.github/` is staged.
- `--branch NAME`: Optional branch name that the script MUST create or switch to
  in the target repository before applying and committing the update.
- `--dry-run`: Optional flag instructing the script that it MUST materialize the
  filtered distribution tree, compute the file-level diff against the target
  repository, print the summary of changes to `stderr`, and exit `0` without
  modifying or committing to the target repository (`--target` MUST NOT be
  modified).
- `-h` / `--help`: Optional flag that MUST print usage instructions and exit
  `0`.

Before modifying the target repository, `scripts/distribute.sh` MUST validate:

1. `--target` is provided, exists, is a directory, and is the root of a valid
   Git working tree (`[ -e "$TARGET/.git" ]` and
   `git -C "$TARGET" rev-parse --is-inside-work-tree`, preventing false
   positives when `--target` is a plain subdirectory nested inside an ancestor
   Git repository). If not, the script MUST log an error to `stderr` and exit
   with code `2`.
1. The target repository working tree and index have no uncommitted
   modifications or untracked files (`git -C "$TARGET" status --porcelain` is
   empty), so an in-progress edit in the target repository is never clobbered.
   If the target working tree is dirty, the script MUST log an error to `stderr`
   and exit with code `3`.
1. `--source-ref` resolves to a valid commit in the source repository
   (`git rev-parse --verify "$SOURCE_REF^{commit}"`). If not, the script MUST
   log an error to `stderr` and exit with code `2`.

#### Scenario: Rejecting a Dirty Target Repository

- **GIVEN** the target repository `/tmp/cm-distro` has an uncommitted change in
  `README.md`
- **WHEN** `./scripts/distribute.sh --target /tmp/cm-distro` is executed
- **THEN** the script logs an error stating that `/tmp/cm-distro` has
  uncommitted changes, makes zero modifications to `/tmp/cm-distro`, and exits
  with code `3`.

#### Scenario: Rejecting a Non-Git Target Directory

- **GIVEN** `/tmp/plain-dir` is an existing directory that is not a Git
  repository (even if nested beneath an ancestor Git checkout)
- **WHEN** `./scripts/distribute.sh --target /tmp/plain-dir` is executed
- **THEN** the script logs an error to `stderr` and exits with code `2`.

#### Scenario: Dry-Run Prints Diff Summary Without Modifying Target Repository

- **GIVEN** an initialized target Git repository `/tmp/cm-distro` at commit `T1`
- **WHEN** `./scripts/distribute.sh --target /tmp/cm-distro --dry-run` is
  executed
- **THEN** the script prints the summary of staged file changes to `stderr`,
  leaves `/tmp/cm-distro` working tree, index, and `HEAD` at `T1` with zero
  modifications, and exits with code `0`.

#### Scenario: Rejecting an Invalid Source Ref

- **GIVEN** a clean target Git repository `/tmp/cm-distro`
- **WHEN**
  `./scripts/distribute.sh --target /tmp/cm-distro --source-ref nonexistent-ref`
  is executed
- **THEN** the script logs an error to `stderr` and exits with code `2`.

______________________________________________________________________

### REQ-0002: Clean Source Extraction via `git archive` {#req-0002}

- **CUJ UUID:** cuj-dist-clean-source-extraction

To guarantee that untracked local files, temporary build caches, `__pycache__`
directories, `.worktrees/`, or uncommitted developer edits in the source
workspace never leak into a customer distribution, `scripts/distribute.sh` MUST
extract source files exclusively from the source repository's Git object
database using `git archive "$SOURCE_REF"` into a temporary staging directory
(`mktemp -d`) cleaned up on `EXIT`.

#### Scenario: Ignoring Untracked and Uncommitted Files in Source Workspace

- **GIVEN** the maintainer's local `codemender-runner` checkout contains an
  untracked file `codemender-runner/secret_notes.txt` and an uncommitted edit to
  `codemender-runner/cli/__main__.py`
- **WHEN** `./scripts/distribute.sh --target /tmp/cm-distro --source-ref HEAD`
  runs
- **THEN** the staged distribution contains the committed `HEAD` version of
  `codemender-runner/cli/__main__.py` and does not include `secret_notes.txt`.

______________________________________________________________________

### REQ-0003: Target Layout & File Filtering {#req-0003}

- **CUJ UUID:** cuj-dist-layout-mapping

When building the staged distribution tree, `scripts/distribute.sh` MUST
transform the archive into the target repository layout:

1. **Optional Top-Level Files (`--include-top-level`):**
   - Only when `--include-top-level` is passed (or when the target repository's
     previous `Codemender-Source-Commit` already synced top-level files):
     - Copy `LICENSE` and `.gitignore` to the target root.
     - Map `dist/README.md` to `README.md` at the target root.
   - Never copy `scripts/distribute.sh` or any `docs/` directory into
     `--target`.
1. **Live GitHub Actions Configuration (`.github/actions/` & `.github/workflows/`):**
   - Map `github-actions/actions/codemender/` to `.github/actions/codemender/`
     (including `action.yml`, which normalizes `ghcr.io/` repository names to
     lowercase and passes the resolved image via `steps.pull-image.outputs.image`
     to `CODEMENDER_RUNNER_IMAGE`, `requirements.txt`, `src/`, and `tests/`).
   - Map `github-actions/workflows/*.yml` (currently `cm-merge-scan.yml`, which
     defaults `runner_image` to
     `${{ vars.CODEMENDER_RUNNER_IMAGE || format('ghcr.io/{0}/codemender-runner:latest', github.repository_owner) }}`
     and passes `vars.CODEMENDER_GCP_PROJECT_ID`,
     `secrets.CODEMENDER_WORKLOAD_IDENTITY_PROVIDER`, and
     `secrets.CODEMENDER_SERVICE_ACCOUNT`, plus any additional workflows added
     under `github-actions/workflows/`) to `.github/workflows/`.
   - Copy `.github/workflows/build_runner_image.yml` to
     `.github/workflows/build_runner_image.yml` (with the build context updated
     to `.github/codemender-runner`, the `Verify Image Contract` step updated to
     assert `CMD ["version"]` rather than `CMD ["legacy"]`, and the `Push Runner Image`
     step honoring `vars.CODEMENDER_RUNNER_IMAGE` while publishing lowercase
     `ghcr.io/${OWNER,,}/codemender-runner:latest` and `:${SHA}`).
   - Explicitly exclude `.github/workflows/codemender_parallel.yml`.
1. **Stripped `codemender-runner/` Package (`.github/codemender-runner/`):**
   - Map the `codemender-runner/` package files required to build, run, and
     unit-test the `cm-runner` CLI (`cli/`, including `version`, `init`,
     `find-diff`, and `fix`) into `.github/codemender-runner/`:
     - `Dockerfile` (with the legacy `main.py` symlink removed and default
       `CMD ["version"]`), `.dockerignore`, and `requirements.txt`
     - `__main__.py` (dispatching directly to `cli.__main__`) and `bin/cm-runner`
     - `cli/` (all modules and subcommands under `cli/` and `cli/commands/`)
     - `codemender/__init__.py` and `codemender/cm.py`
     - `config.py`
     - `vcs/__init__.py` and `vcs/git.py` (with legacy `utils` imports deferred
       inside legacy-only functions)
     - Unit tests for the included modules under `.github/codemender-runner/tests/`:
       `tests/__init__.py`, `tests/test_cli*.py`, `tests/test_codemender_cm.py`,
       `tests/test_vcs_git.py`, `tests/test_config.py`, and `tests/testdata/`
   - **Explicitly exclude all legacy pipeline files:**
     - `main.py`, `runners/`, `storage.py`, `utils.py`, `codemender/db.py`,
       `vcs/github.py`, `codemender-runner/README.md`, and all legacy test files
       (`test_runners_*.py`, `test_orchestrator.py`,
       `test_pr_review_suggestions.py`, `test_storage.py`, `test_utils.py`,
       `test_vcs_github.py`, `test_codemender_db.py`, `test_command_builder.py`,
       `e2e_test_local.py`, `dummy_cm.py`, `tests/cm`).
1. **GCP Workload Identity Federation Terraform Configuration (`.github/terraform/` &
   `.github/terraform/modules/wif/`):**
   - Copy the root `terraform/` files (`main.tf`, `variables.tf`, `outputs.tf`,
     `provider.tf`, `terraform.tfvars.example`, `.terraform.lock.hcl`, and
     `tests/root_test.tftest.hcl`) and `terraform/modules/wif/` to
     `.github/terraform/` (and `.github/terraform/modules/wif/`).
   - Explicitly exclude/strip `terraform/modules/gcp/`, `terraform/gcp.tf`,
     `terraform/gcp_override.tf`, `terraform/tests/root_gcp_test.tftest.hcl`,
     `workflows/gcp_parallel_workflow.yaml`, `cloudbuild.yaml`, and all `docs/`
     files (`docs/openspec/`, `docs/adr/`, `docs/architecture/`, `docs/guides/`)
     until Cloud Run support is ready to be added to `scripts/distribute.sh` in
     a future commit, ensuring `terraform init`, `terraform validate`, and
     `terraform test` succeed directly in `.github/terraform/` inside the
     distributed repository with zero dangling local, module, or output
     references.

#### Scenario: Distribution With `--include-top-level` Produces a Self-Contained Repo With Zero Legacy Files

- **GIVEN** an initialized target Git repository `/tmp/cm-distro`
- **WHEN**
  `./scripts/distribute.sh --target /tmp/cm-distro --include-top-level`
  completes
- **THEN** `/tmp/cm-distro` contains only `LICENSE`, `.gitignore`, `README.md`,
  and `.github/` at the repository root (with `.github/actions/codemender/`,
  `.github/workflows/cm-merge-scan.yml`,
  `.github/workflows/build_runner_image.yml`, `.github/codemender-runner/`,
  `.github/terraform/main.tf`, `.github/terraform/terraform.tfvars.example`,
  `.github/terraform/tests/root_test.tftest.hcl`, and
  `.github/terraform/modules/wif/`)
- **AND** `/tmp/cm-distro` contains no `docs/` directory, no `distribute.sh`
  script, and no files under `.github/codemender-runner/runners/` or
  `.github/terraform/modules/gcp/`, nor `.github/terraform/gcp.tf`,
  `.github/terraform/gcp_override.tf`,
  `.github/terraform/tests/root_gcp_test.tftest.hcl`,
  `.github/codemender-runner/main.py`, or `.github/codemender-runner/utils.py`
- **AND** running
  `python3 -m unittest discover -s .github/codemender-runner/tests -t .github/codemender-runner`
  and `python3 -m unittest discover -s .github/actions/codemender/tests` inside
  `/tmp/cm-distro` both pass with exit code `0`.

______________________________________________________________________

### REQ-0004: Stateless 3-Way Incremental Sync via Git Commit Trailer (No `.dist.json`) {#req-0004}

- **CUJ UUID:** cuj-dist-git-three-way-sync

`scripts/distribute.sh` MUST NOT write a `.dist.json` or any other distribution
tracking manifest file into the target repository working tree. Instead, it MUST
use a Git commit trailer (`Codemender-Source-Commit`) and Git's native 3-way
merge machinery so repeat runs update the target repository without stomping on
target-side edits, even when the source and target repositories have no shared
commit history:

1. **Automatic Commit With Provenance Trailer:**
   - Every completed sync (when `--dry-run` is not passed and `$new_tree`
     differs from `$base_tree`) MUST automatically create a Git commit in the
     target repository whose commit message contains the trailer:
     ```text
     Codemender-Source-Commit: <full-40-char-source-commit-sha>
     ```
1. **Base Tree Resolution from Commit Trailer:**
   - On every run against a target repository that has at least one commit on
     `HEAD`, `scripts/distribute.sh` MUST search the target repository's commit
     history (`git -C "$TARGET" log --grep="^Codemender-Source-Commit:" -n 1`)
     to extract the most recent `Codemender-Source-Commit` SHA
     (`<prev_source_sha>`).
   - If `<prev_source_sha>` is found, `scripts/distribute.sh` MUST verify that
     `<prev_source_sha>` exists in the source repository
     (`git cat-file -e "${prev_source_sha}^{commit}"`); if `<prev_source_sha>`
     is missing in the source repository, the script MUST log an error to
     `stderr` and exit with code `2`. Otherwise, `scripts/distribute.sh` MUST
     reconstruct the filtered distribution tree for `<prev_source_sha>` (using
     `<prev_source_sha>`'s own `scripts/distribute.sh` staging rules if present
     at `<prev_source_sha>`, so directories added to `scripts/distribute.sh` in
     later commits—such as Cloud Run—are accurately absent from `$base_tree` and
     added in `$new_tree`) and write it into the target repository's Git object
     store as `$base_tree` (`GIT_INDEX_FILE` + `git write-tree`).
   - If no `Codemender-Source-Commit` trailer exists in the target repository's
     history (initial sync), `$base_tree` MUST be set to Git's empty tree SHA
     (`4b825dc642cb6eb9a060e54bf8d69288fbee4904`).
1. **3-Way Merge & Commit Execution (`git commit-tree` +
   `git cherry-pick --no-commit` + `git commit`):**
   - `scripts/distribute.sh` MUST write the new filtered distribution tree for
     `--source-ref` into the target repository's Git object store as
     `$new_tree`.
   - If the target repository has no commits yet, the script MUST populate the
     index and working tree via
     `git -C "$TARGET" read-tree --reset -u "$new_tree"` and commit the staged
     tree with the `Codemender-Source-Commit` trailer.
   - Otherwise, if `$new_tree` equals `$base_tree`, the script MUST report that
     the target repository is already up to date and exit `0` without creating
     an empty commit.
   - Otherwise, the script MUST synthesize two ephemeral commits in the target
     repository's object store:
     - `base_commit=$(git -C "$TARGET" commit-tree "$base_tree" -m "dist-base")`
     - `new_commit=$(git -C "$TARGET" commit-tree "$new_tree" -p "$base_commit" -m "dist-new")`
       and apply the delta onto the target repository's `HEAD` via
       `git -C "$TARGET" cherry-pick --no-commit "$new_commit"`.
   - If the 3-way merge completes cleanly, all upstream additions,
     modifications, and deletions between `<prev_source_sha>` and `<source_ref>`
     MUST be committed in the target repository with the
     `Codemender-Source-Commit` trailer while preserving any target-only files
     or non-conflicting target edits.
   - If a file was modified on conflicting lines in both the source and target
     repositories, `git cherry-pick --no-commit` leaves standard 3-way conflict
     markers in the target working tree without creating a commit, and
     `scripts/distribute.sh` MUST log the conflicting file paths to `stderr` and
     exit with code `4` so the user can resolve them without data loss.

#### Scenario: Initial Sync to a New Target Repository

- **GIVEN** a newly initialized target Git repository `/tmp/cm-distro` with no
  prior `Codemender-Source-Commit` trailer
- **WHEN** `./scripts/distribute.sh --target /tmp/cm-distro --source-ref C1` is
  executed
- **THEN** a commit is created on `/tmp/cm-distro` whose commit message contains
  `Codemender-Source-Commit: <full-40-char-C1-sha>`
- **AND** no `.dist.json` or tracking manifest file exists anywhere in
  `/tmp/cm-distro`
- **AND** immediately re-running
  `./scripts/distribute.sh --target /tmp/cm-distro --source-ref C1` reports that
  the target repository is already up to date and exits `0` with a clean working
  tree.

#### Scenario: Repeat Sync Preserves Target-Only Files and Non-Conflicting Target Edits

- **GIVEN** `/tmp/cm-distro` was previously synced at source commit `C1`
  (recorded in `Codemender-Source-Commit: <C1>`), and in `/tmp/cm-distro` a
  custom file `CODEOWNERS` was added and `.github/workflows/cm-merge-scan.yml`
  had its `runner_image:` line customized
- **WHEN** source commit `C2` updates
  `codemender-runner/cli/commands/find_diff.py` and
  `./scripts/distribute.sh --target /tmp/cm-distro --source-ref C2` is executed
- **THEN** `.github/codemender-runner/cli/commands/find_diff.py` is updated to
  the `C2` version, `CODEOWNERS` and the customized `runner_image:` line in
  `.github/workflows/cm-merge-scan.yml` remain intact, and the new commit on
  `/tmp/cm-distro` records `Codemender-Source-Commit: <C2>`.

#### Scenario: Repeat Sync Propagates Upstream File Deletions Without Deleting Target-Only Files

- **GIVEN** `/tmp/cm-distro` was synced at `C1`, and in `C2` a distributed
  helper file is removed upstream while `/tmp/cm-distro` also contains a
  target-only file `SECURITY.md`
- **WHEN** `./scripts/distribute.sh --target /tmp/cm-distro --source-ref C2`
  runs
- **THEN** the file removed between `C1` and `C2` is deleted from
  `/tmp/cm-distro` in the new commit, while `SECURITY.md` is untouched.

#### Scenario: Detecting Overlapping Line Conflicts Without Stomping

- **GIVEN** `/tmp/cm-distro` modified line 45 of
  `.github/workflows/cm-merge-scan.yml` after syncing `C1`, and upstream commit
  `C2` also modified line 45 of `github-actions/workflows/cm-merge-scan.yml` to
  a different value
- **WHEN** `./scripts/distribute.sh --target /tmp/cm-distro --source-ref C2`
  runs
- **THEN** the script does not silently overwrite the target file, leaves
  standard Git conflict markers in `.github/workflows/cm-merge-scan.yml`, prints
  the conflicted file path to `stderr`, and exits with code `4`.

______________________________________________________________________

### REQ-0005: Off-by-Default Top-Level File Staging (`--include-top-level`) {#req-0005}

- **CUJ UUID:** cuj-dist-include-top-level-flag

`scripts/distribute.sh` MUST control top-level repository file staging
(`LICENSE`, `.gitignore`, `README.md`) via the off-by-default
`--include-top-level` flag:

1. **Off by Default (`.github/` Only):**
   - When `--include-top-level` is NOT passed (and the target repository's
     previous `Codemender-Source-Commit` did not sync top-level `README.md` from
     `dist/README.md`), `scripts/distribute.sh` MUST stage ONLY `.github/` and
     MUST NOT create, modify, or overwrite `LICENSE`, `.gitignore`, or
     `README.md` at the root of `--target`.
1. **Explicit Top-Level Staging (`--include-top-level`):**
   - When `--include-top-level` IS passed (or the target repository's previous
     `Codemender-Source-Commit` already synced top-level `README.md` from
     `dist/README.md`), `scripts/distribute.sh` MUST include `LICENSE`,
     `.gitignore`, and `dist/README.md` (mapped to `README.md`) in both
     `$base_tree` and `$new_tree`.
1. **No Self-Copy of `scripts/distribute.sh`:**
   - `scripts/distribute.sh` MUST NOT copy itself into `--target` regardless of
     whether `--include-top-level` is passed.

#### Scenario: Default Sync Stages Only `.github/` While `--include-top-level` Stages Top-Level Files

- **GIVEN** `/tmp/existing-app` has its own `README.md` (`# My Existing App`),
  and `/tmp/cm-distro` is an empty Git repository for the standalone
  distribution
- **WHEN** `./scripts/distribute.sh --target /tmp/existing-app` runs without
  `--include-top-level` and
  `./scripts/distribute.sh --target /tmp/cm-distro --include-top-level` runs
  with `--include-top-level`
- **THEN** `/tmp/existing-app/README.md` remains `# My Existing App` and
  `/tmp/existing-app/.github/` is populated with `actions/codemender/`,
  `workflows/`, `codemender-runner/`, and `terraform/` (including
  `terraform/modules/wif/`)
- **AND** `/tmp/cm-distro` receives `LICENSE`, `.gitignore`, `README.md` (from
  `dist/README.md`), and `.github/`
- **AND** neither repository contains `distribute.sh`.

#### Scenario: Repeat Sync on a Repo Initialized With `--include-top-level` Preserves and Updates Top-Level Files

- **GIVEN** `/tmp/cm-distro` was initialized with `--include-top-level` at
  commit `C1`
- **WHEN** the maintainer runs
  `./scripts/distribute.sh --target /tmp/cm-distro --source-ref C2`
- **THEN** `README.md`, `LICENSE`, and `.gitignore` in `/tmp/cm-distro` are
  preserved and updated to the `C2` version rather than deleted.

______________________________________________________________________

### REQ-0006: Customer-Facing Root Documentation (`dist/README.md`) {#req-0006}

- **CUJ UUID:** cuj-dist-customer-docs

The source repository MUST maintain a comprehensive customer-facing root
documentation file at `dist/README.md` (mapped to `README.md` in the
distribution repository when `--include-top-level` is passed) that documents:

1. **Repository Overview & Layout:** A summary of what the repository is and a
   directory tree illustrating `LICENSE`, `.gitignore`, `README.md`, and
   `.github/{actions/codemender,workflows,codemender-runner,terraform}`
   (including `.github/terraform/modules/wif`).
1. **Step-by-Step Setup & Usage Instructions:**
   - **Step 1 — Provisioning GCP Workload Identity Federation (Terraform):** How
     to use `.github/terraform/` (`cd .github/terraform && cp terraform.tfvars.example terraform.tfvars && terraform init && terraform apply`)
     so GitHub Actions can authenticate to Google Cloud.
   - **Step 2 — Configuring GitHub Actions Secrets & Variables:** Required and
     optional repository secrets and variables
     (`CODEMENDER_WORKLOAD_IDENTITY_PROVIDER`, `CODEMENDER_SERVICE_ACCOUNT`,
     `GH_APP_ID`, `CODEMENDER_GCP_PROJECT_ID`, `CODEMENDER_GCP_REGION`, and
     optional `CODEMENDER_RUNNER_IMAGE` to override the default
     `ghcr.io/<owner>/codemender-runner:latest` image or pin a `<commit-sha>`
     tag).
   - **Step 3 — Building & Publishing the `cm-runner` Image:** How
     `.github/workflows/build_runner_image.yml` (or
     `docker build .github/codemender-runner`) builds and publishes the
     `cm-runner` container image (`ghcr.io/<owner>/codemender-runner:latest` and
     `:<commit-sha>`), which `.github/workflows/cm-merge-scan.yml` uses
     automatically out of the box without editing the workflow file.
   - **Step 4 — Running CodeMender on Pull Requests & via CLI:** How
     `.github/workflows/cm-merge-scan.yml` runs automatically on PRs and how to
     invoke `cm-runner` (`find-diff`, `fix`) directly via CLI.
1. **Setting Up CodeMender in a Separate Repository:** Step-by-step instructions
   for manually onboarding another repository by copying
   `.github/actions/codemender/` and `.github/workflows/cm-merge-scan.yml`,
   optionally setting the `CODEMENDER_RUNNER_IMAGE` repository/organization
   variable (if the image is hosted under a different owner/registry or pinned
   to a commit SHA), adding the new repository to `github_repositories` in
   `.github/terraform/terraform.tfvars`, and configuring its GitHub Actions
   variables.

#### Scenario: Relative Links in Distributed `README.md` Resolve Validly

- **GIVEN** `/tmp/cm-distro` has been populated by
  `./scripts/distribute.sh --target /tmp/cm-distro --include-top-level`
- **WHEN** all relative Markdown links in `/tmp/cm-distro/README.md` are
  inspected
- **THEN** every linked target (`LICENSE`, `.github/actions/codemender/`,
  `.github/actions/codemender/action.yml`,
  `.github/workflows/cm-merge-scan.yml`,
  `.github/workflows/build_runner_image.yml`, `.github/codemender-runner/`,
  `.github/terraform/`, `.github/terraform/terraform.tfvars.example`)
  exists at its resolved path inside `/tmp/cm-distro`.
