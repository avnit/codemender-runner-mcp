<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-10-01' }
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
1. **Customer Propagation Kit:** When the maintainer populates the distribution
   repository with `--include-distribute`, `scripts/distribute.sh` is included
   in the distribution repository alongside the GCP Workload Identity Federation
   (WIF) Terraform module (`terraform/gha_wif/`) and customer-focused
   documentation (`README.md`, `docs/github_actions_setup.md`,
   `docs/calling_cm_runner.md`). Customers can then run
   `./scripts/distribute.sh --target /path/to/repo` from the distribution
   repository (where `--include-distribute` is off by default) to set up
   additional repositories without copying `scripts/distribute.sh` into those
   repositories.

This specification is governed by and references:

- [SPEC-FIND-DIFF](../find-diff/spec.md) — Differential vulnerability scanning
  subcommand (`cm-runner find-diff`).
- [SPEC-FIX](../fix/spec.md) — Automated patch synthesis and remediation
  subcommand (`cm-runner fix`).
- [ADR-0001: Machine-Readable Output Contract](../../adr/ADR-0001-machine-readable-output-contract.md)
- [ADR-0002: Runner Layout and Build](../../adr/ADR-0002-runner-layout-and-build.md)
- [ADR-0003: Environment Variable Scrubbing When Running `cm`](../../adr/ADR-0003-environment-scrubbing.md)
- [ADR-0004: Project Tech Stack & Scenario-Decoupled Monorepo Architecture](../../adr/ADR-0004-project-tech-stack-and-repo-architecture.md)

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
- `--include-distribute`: Optional flag (**off by default**) controlling whether
  `scripts/distribute.sh` itself is copied into `--target` at
  `scripts/distribute.sh` (`chmod +x`). When omitted (and not already tracked on
  the target repository's `HEAD`), `scripts/distribute.sh` MUST NOT be copied
  into `--target`.
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

### REQ-0003: Dual-Purpose Target Layout & File Filtering {#req-0003}

- **CUJ UUID:** cuj-dist-layout-mapping

When building the staged distribution tree, `scripts/distribute.sh` MUST
transform the archive into the target repository layout so that the target
repository both operates as a live GitHub Actions example repository and
provides all assets needed to set up the workflow in additional customer
repositories:

1. **Root & Base Files:**
   - Copy `LICENSE` and `.gitignore` to the target root.
   - Map `dist/README.md` (when running from the internal monorepo) or
     `README.md` (when running from an already-distributed repository) to
     `README.md` at the target root.
   - Exclude `scripts/distribute.sh` by default; include `scripts/distribute.sh`
     (`chmod +x`) at `scripts/distribute.sh` only when `--include-distribute` is
     set (or when `scripts/distribute.sh` is already tracked at `HEAD` in
     `--target`).
1. **Live GitHub Actions Configuration (`.github/`):**
   - Map `github-actions/actions/codemender/` (or `.github/actions/codemender/`
     when running from a distributed repository) to
     `.github/actions/codemender/` (including `action.yml`, `requirements.txt`,
     `src/`, and `tests/`).
   - Map `github-actions/workflows/*.yml` (or `.github/workflows/*.yml` when
     running from a distributed repository; currently `cm-merge-scan.yml`, plus
     any additional workflows added under `github-actions/workflows/`) to
     `.github/workflows/`.
   - Copy `.github/workflows/build_runner_image.yml` to
     `.github/workflows/build_runner_image.yml` (with the
     `Verify Image Contract` step updated to assert `CMD ["version"]` rather
     than `CMD ["legacy"]`).
   - Explicitly exclude `.github/workflows/codemender_parallel.yml`.
1. **Stripped `codemender-runner/` Package (New `cm-runner` Pipeline Only):**
   - Include the `codemender-runner/` package files required to build, run, and
     unit-test the `cm-runner` CLI (`cli/`, including `version`, `init`,
     `find-diff`, and additional subcommands such as `fix` as they are added to
     `cli/commands/`):
     - `codemender-runner/Dockerfile` (with the legacy `main.py` symlink removed
       and default `CMD ["version"]`), `codemender-runner/.dockerignore`, and
       `codemender-runner/requirements.txt`
     - `codemender-runner/__main__.py` (dispatching directly to `cli.__main__`)
       and `codemender-runner/bin/cm-runner`
     - `codemender-runner/cli/` (all modules and subcommands under `cli/` and
       `cli/commands/`)
     - `codemender-runner/codemender/__init__.py` and
       `codemender-runner/codemender/cm.py`
     - `codemender-runner/config.py`
     - `codemender-runner/vcs/__init__.py` and `codemender-runner/vcs/git.py`
       (with legacy `utils` imports deferred inside legacy-only functions)
     - Unit tests for the included modules:
       `codemender-runner/tests/__init__.py`, `tests/test_cli*.py`,
       `tests/test_codemender_cm.py`, `tests/test_vcs_git.py`,
       `tests/test_config.py`, and `tests/testdata/` (with legacy-only test
       cases in `test_cli.py` and `test_vcs_git.py` guarded via
       `@unittest.skipUnless` when `main.py` or `utils.py` is absent and using
       inline `_write_fake_cm` stubs instead of `tests/cm`)
   - **Explicitly exclude all legacy pipeline files:**
     - `codemender-runner/main.py`, `codemender-runner/runners/`,
       `codemender-runner/storage.py`, `codemender-runner/utils.py`,
       `codemender-runner/codemender/db.py`, `codemender-runner/vcs/github.py`,
       `codemender-runner/README.md`, and all legacy test files
       (`test_runners_*.py`, `test_orchestrator.py`,
       `test_pr_review_suggestions.py`, `test_storage.py`, `test_utils.py`,
       `test_vcs_github.py`, `test_codemender_db.py`, `test_command_builder.py`,
       `e2e_test_local.py`, `dummy_cm.py`, `tests/cm`).
1. **GCP Workload Identity Federation Module (`terraform/gha_wif/`):**
   - Copy `terraform/gha_wif/` to `terraform/gha_wif/`.
   - Exclude in-progress Cloud Run files (`terraform/gcp/`,
     `workflows/gcp_parallel_workflow.yaml`, `cloudbuild.yaml`) until Cloud Run
     support is ready to be added to `scripts/distribute.sh` in a future commit.
1. **Customer Documentation (`docs/`):**
   - Map `docs/guides/calling_cm_runner.md` (or `docs/calling_cm_runner.md` when
     running from a distributed repository) to `docs/calling_cm_runner.md` and
     rewrite or strip internal relative links to `../adr/` so no broken relative
     links remain.
   - Map `github-actions/README.md` (or `docs/github_actions_setup.md` when
     running from a distributed repository) to `docs/github_actions_setup.md`
     and rewrite `docs/guides/calling_cm_runner.md` references to
     `docs/calling_cm_runner.md`.
   - Exclude all internal SDD specifications (`docs/openspec/`), ADRs
     (`docs/adr/`), internal architecture docs (`docs/architecture/`), roadmap
     notes (`docs/future_work.md`), and legacy guides
     (`docs/guides/github_actions_guide.md`, `docs/guides/local_run.md`,
     `docs/guides/production_run.md`,
     `docs/guides/terraform_deployment_guide.md`,
     `docs/guides/configuration_reference.md`).

#### Scenario: Distribution Produces a Self-Contained Repo With Zero Legacy Files

- **GIVEN** an initialized target Git repository `/tmp/cm-distro`
- **WHEN** `./scripts/distribute.sh --target /tmp/cm-distro` completes
- **THEN** `/tmp/cm-distro` contains `.github/actions/codemender/action.yml`,
  `.github/workflows/cm-merge-scan.yml`,
  `.github/workflows/build_runner_image.yml`, `terraform/gha_wif/`, `README.md`,
  `docs/calling_cm_runner.md`, and `docs/github_actions_setup.md`
- **AND** `/tmp/cm-distro` contains no files under `codemender-runner/runners/`,
  `docs/openspec/`, `docs/adr/`, or `terraform/gcp/`, nor
  `codemender-runner/main.py`, `codemender-runner/utils.py`, or
  `scripts/distribute.sh`
- **AND** running
  `python3 -m unittest discover -s codemender-runner/tests -t codemender-runner`
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
- **THEN** `codemender-runner/cli/commands/find_diff.py` is updated to the `C2`
  version, `CODEOWNERS` and the customized `runner_image:` line in
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

### REQ-0005: Off-by-Default Distribution Script Self-Copy (`--include-distribute`) & Downstream Repo Setup {#req-0005}

- **CUJ UUID:** cuj-dist-include-distribute-flag

`scripts/distribute.sh` MUST serve as the single tool for both maintainer
distribution (from `cloud-gtm/codemender-runner` into the customer distribution
repository) and downstream customer repository setup (from the distribution
repository into additional customer repositories), governed by the
`--include-distribute` flag:

1. **Off by Default:**
   - When `--include-distribute` is NOT passed (and `scripts/distribute.sh` is
     not already tracked on the target repository's `HEAD`),
     `scripts/distribute.sh` MUST NOT copy `scripts/distribute.sh` into
     `--target`.
1. **Explicit Self-Copy (`--include-distribute`):**
   - When `--include-distribute` IS passed (or `scripts/distribute.sh` is
     already tracked on the target repository's `HEAD` from a prior
     `--include-distribute` sync), `scripts/distribute.sh` MUST include
     `scripts/distribute.sh` (`chmod +x`) in both `$base_tree` and `$new_tree`
     so the distribution repository receives and maintains an up-to-date copy of
     `scripts/distribute.sh`.
1. **Downstream Execution from Distributed Repository:**
   - When `scripts/distribute.sh` is executed from inside a distributed
     repository (where files already reside at `.github/actions/codemender/`,
     `.github/workflows/`, `README.md`, and `docs/`) without
     `--include-distribute`, it MUST stage and commit those assets into
     `--target` and MUST NOT copy `scripts/distribute.sh` into `--target`.

#### Scenario: Maintainer Syncs With `--include-distribute` and Customer Sets Up Downstream Repo Without Copying `distribute.sh`

- **GIVEN** the maintainer runs
  `./scripts/distribute.sh --target /tmp/cm-distro --include-distribute` to
  populate the customer distribution repository `/tmp/cm-distro`
- **WHEN** a customer inside `/tmp/cm-distro` runs
  `./scripts/distribute.sh --target /tmp/customer-app` (without
  `--include-distribute`)
- **THEN** `/tmp/cm-distro/scripts/distribute.sh` exists and is executable
- **AND** `/tmp/customer-app` receives `.github/actions/codemender/action.yml`,
  `.github/workflows/cm-merge-scan.yml`, and the distributed assets
- **AND** `/tmp/customer-app/scripts/distribute.sh` does NOT exist.

#### Scenario: Repeat Maintainer Sync on a Repo Tracking `scripts/distribute.sh` Preserves and Updates It

- **GIVEN** `/tmp/cm-distro` was initialized with `--include-distribute` at
  commit `C1` so `scripts/distribute.sh` is tracked in `/tmp/cm-distro`
- **WHEN** the maintainer runs
  `./scripts/distribute.sh --target /tmp/cm-distro --source-ref C2`
- **THEN** `scripts/distribute.sh` in `/tmp/cm-distro` is preserved and updated
  to the `C2` version rather than deleted.

______________________________________________________________________

### REQ-0006: Customer-Facing Documentation Suite (`dist/README.md` & `docs/`) {#req-0006}

- **CUJ UUID:** cuj-dist-customer-docs

The source repository MUST maintain a customer-facing root documentation
template at `dist/README.md` (mapped to `README.md` in the distribution
repository) that documents:

1. **Repository Overview & Dual Role:** How the distribution repository works
   immediately as a live example repository (`.github/actions/codemender/` and
   `.github/workflows/cm-merge-scan.yml`) and as a source for setting up
   additional repositories.
1. **Step 1 — Building & Publishing the `cm-runner` Image:** How
   `.github/workflows/build_runner_image.yml` (or
   `docker build codemender-runner`) builds and publishes the `cm-runner`
   container image.
1. **Step 2 — Setting Up GCP Workload Identity Federation (WIF):** How to use
   `terraform/gha_wif/` so `cm-runner` can authenticate to Google Cloud.
1. **Step 3 — Setting Up Additional Repositories:** How to run
   `./scripts/distribute.sh --target /path/to/repo` from the distribution
   repository to set up another repository without copying the distribution
   script itself.
1. **Documentation Links:** Relative links to `docs/github_actions_setup.md` and
   `docs/calling_cm_runner.md` that resolve validly inside the distribution
   repository.

#### Scenario: Relative Links in Distributed Documentation Resolve Validly

- **GIVEN** `/tmp/cm-distro` has been populated by
  `./scripts/distribute.sh --target /tmp/cm-distro --include-distribute`
- **WHEN** all relative Markdown links in `/tmp/cm-distro/README.md` are
  inspected
- **THEN** every linked target (`docs/github_actions_setup.md`,
  `docs/calling_cm_runner.md`, `.github/workflows/cm-merge-scan.yml`,
  `.github/workflows/build_runner_image.yml`, `scripts/distribute.sh`,
  `terraform/gha_wif/`) exists at its resolved path inside `/tmp/cm-distro`.
