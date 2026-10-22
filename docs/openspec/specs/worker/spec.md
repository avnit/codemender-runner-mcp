<!--
sdd_spec_id: SPEC-WORKER
sdd_implements: SPEC-0003
sdd_extends: None
sdd_level: COMPONENT
sdd_archetype: CLI_TOOL_BATCH_JOB
sdd_cujspec_product: codemender-runner
-->

<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-10-20' }
*-->

# SPEC-WORKER: Sandbox Container Worker (`launcher.worker` / `cm-worker`)

**Implements Spec:** [SPEC-0003](../runner-container-packaging/spec.md)

**Extends Spec:** None

**Governing Journeys:**
- **Pull Request Contributor (`persona-pr-author`)**: As a developer waiting on
  automated security scans and patch suggestions, I want the sandbox container
  worker (`cm-worker`) to execute `find-diff`, `fix`, and `verify` tasks
  deterministically across local Docker/gVisor and Google Cloud Run Jobs,
  preserving executable file modes in my repository and capturing clear failure
  logs when a run fails or is preempted.
- **Security & Compliance Lead (`persona-security-auditor`)**: As a security
  lead governing untrusted code execution in
  [SPEC-0003](../runner-container-packaging/spec.md), I need the
  inside-container worker to run with zero repository host credentials
  (`packages/repohost` never imported or installed), validate archives against
  tar-slip path traversal before extraction, isolate each task in
  container-private scratch storage (`/tmp/work`), and communicate exclusively
  through a scoped `ArtifactCache`.

## Overview

This specification defines the normative command-line interface, dependency
injection boundaries, 4-step task execution protocol, archive extraction
security controls, structured receipt contract, and preemption signal handling
for `launcher.worker.Worker` and the `cm-worker` console script entrypoint in
`packages/launcher`.

As established in
[ADR-0009](../../../adr/ADR-0009-abstraction-layers-and-dependency-injection.md)
(Decision 2 and Section 4), `Launcher` (on the trusted outer CI host) and
`Worker` (inside the untrusted sandbox container) form the client and server
halves of a unified container execution protocol. Whether a container is started
locally under Docker/gVisor (`runsc`) or remotely on Google Cloud Run Jobs or
GKE, the same container image (`docker/runner.Dockerfile`, governed by
[SPEC-0003](../runner-container-packaging/spec.md)) sets
`ENTRYPOINT ["cm-worker"]` and invokes `Worker.run_task(task_id)` against an
injected `ArtifactCache` (`SPEC-ARTIFACT-CACHE`:
`launcher.cache.ArtifactCache` / `open_artifact_cache`). Standalone local runs
outside of a CI/CD pipeline can still use the same container image directly by
overriding the entrypoint (`docker run --entrypoint cm-runner`).

Detailed component architecture, data models (`RunManifest` and
`ExecutionReceipt`), sequence flows, and error/exit-code mappings are specified
in [design.md](design.md).

This specification is governed by and references:
- [ADR-0009](../../../adr/ADR-0009-abstraction-layers-and-dependency-injection.md)
  (Decision 2 and Section 4): Unified `Worker` (`cm-worker`) 4-step execution
  protocol, `ArtifactCache` dependency injection, and Two-Way Door container
  invariants.
- [ADR-0008](../../../adr/ADR-0008-repository-layout-and-package-structure.md):
  Three-package workspace layout (`packages/{codemender,launcher,repohost}`),
  co-located unit tests, and `cm-worker = "launcher.worker:main"` entrypoint.
- [ADR-0007](../../../adr/ADR-0007-cloud-run-job-invocation-interface-cli-vs-python-library.md):
  `RunManifest` and `ExecutionReceipt` models, failure-spill logging, and exit
  code mapping (`0`, `2`, `3`, `130`, `143`).
- [ADR-0005](../../../adr/ADR-0005-multi-platform-transit-storage-and-isolation.md):
  Bounded-history archive transit (`shared/code.tar.gz`) and container-private
  ephemeral scratch isolation.
- [ADR-0003](../../../adr/ADR-0003-environment-scrubbing.md): Subprocess
  environment pass-through (`env=None`) without denylist scrubbing or
  `<repo_dir>/.codemender_cache` redirection.
- [ADR-0001](../../../adr/ADR-0001-machine-readable-output-contract.md):
  Split-stream output purity (`stderr` for human diagnostics, coarse exit codes
  `0`, `2`, `3`, and cross-boundary `--out` artifacts).
- [SPEC-0003](../runner-container-packaging/spec.md): Lean runner container
  packaging (`docker/runner.Dockerfile`) with `ENTRYPOINT ["cm-worker"]`.
- [SPEC-FIND-DIFF](../find-diff/spec.md), [SPEC-FIX](../fix/spec.md), and
  [SPEC-VERIFY](../verify/spec.md): Downstream `cm-runner` subcommand contracts
  (bounded Git history, clean working tree invariants, SARIF `properties.status`,
  and raw binary diff output).
- [SPEC-GHA](../github-actions/spec.md),
  [SPEC-GHA-FIX](../github-actions-fix/spec.md), and
  [SPEC-GITLAB-CI](../gitlab-ci/spec.md): Outer CI/CD orchestration workflows,
  `no_fix` vs. infrastructure failure reporting, and sandbox container mount/env
  contracts.
- [Proposal: cloudrun-job-bootstrapper](../../proposals/cloudrun-job-bootstrapper/proposal.md):
  Container bootstrapper capabilities consolidated into `launcher.worker`.
- [Proposal: vcs-cloudrun-comms](../../proposals/vcs-cloudrun-comms/proposal.md):
  5-phase client-to-worker transit storage and execution lifecycle protocol
  realized jointly by `Launcher`, `ArtifactCache`, and `Worker`.
- `SPEC-ARTIFACT-CACHE` (`launcher.cache.ArtifactCache`, `open_artifact_cache`,
  `RunManifest`, `ExecutionReceipt`, `ScanMetadata`, and
  `launcher.cache.errors.ArtifactCacheError`): Typed client-worker communication
  protocol (`vcs-cloudrun-comms`) and short-lived `file://` / `gs://` artifact
  cache abstraction seam over `fsspec`.

______________________________________________________________________

## Requirements

### REQ-0001: `cm-worker` CLI Entrypoint & Argument Validation {#req-0001}

- **CUJ UUID:** `cuj-worker-cli-entrypoint`

`packages/launcher/pyproject.toml` MUST register the console script entrypoint
`cm-worker = "launcher.worker:main"` under `[project.scripts]`.

The `cm-worker` CLI entrypoint MUST accept the following command-line flags:
1. `--cache-uri <URI>`: **Required.** Non-empty `file://` or `gs://` URI (or
   local directory path) identifying the scoped `ArtifactCache` root.
2. `--task-id <ID>`: **Required.** Non-empty logical task identifier matching a
   single path segment (e.g., `find`, `fix-finding-001`, `verify-finding-001`).
   To prevent cache path traversal, `--task-id` MUST NOT be empty, `.`, `..`, or
   contain path separators (`/` or `\`).
3. `--work-dir <DIR>`: **Optional.** Local container-private scratch directory
   path (default: `/tmp/work`).

`cm-worker` MUST emit zero bytes to `sys.stdout` across all execution paths
(both success and failure) per
[ADR-0007](../../../adr/ADR-0007-cloud-run-job-invocation-interface-cli-vs-python-library.md).
If required flags are missing, empty, or invalid, or if unknown flags are
supplied, `cm-worker` MUST write a diagnostic error message to `sys.stderr`,
emit zero bytes to `sys.stdout`, and exit with code `2`
([ADR-0001](../../../adr/ADR-0001-machine-readable-output-contract.md)). If
opening the `ArtifactCache` for `--cache-uri` raises `CacheConfigurationError`,
`CachePermissionError`, or `CacheTransientError` (`SPEC-ARTIFACT-CACHE`),
`cm-worker` MUST log the failure to `sys.stderr`, emit zero bytes to
`sys.stdout`, and exit with code `3`.

#### Scenario: Valid CLI Invocation Dispatches Worker Task

- **GIVEN** a populated `ArtifactCache` at `file:///cache` with task `find`
- **WHEN** `cm-worker --cache-uri file:///cache --task-id find` is invoked
- **THEN** `cm-worker` connects to the `ArtifactCache` at `file:///cache`,
  executes task `find` using the default `/tmp/work` scratch directory, emits
  zero bytes to `stdout`, and exits with the task's resulting exit code.

#### Scenario: Missing or Invalid CLI Arguments Return Exit Code 2

- **GIVEN** a caller invokes `cm-worker` without `--task-id` or with an invalid
  traversal task ID `--task-id ../escape`
- **WHEN** argument validation runs
- **THEN** `cm-worker` logs a usage error to `stderr`, writes nothing to
  `stdout`, and exits with code `2`.

______________________________________________________________________

### REQ-0002: DI, Package Isolation & Lazy Imports {#req-0002}

- **CUJ UUID:** `cuj-worker-di-isolation`

`Worker` MUST accept its `ArtifactCache` backend, local scratch directory path
(defaulting to `/tmp/work`), and `cm-runner` executable name/path (defaulting to
`"cm-runner"`) via dependency injection so that unit and integration tests can
supply local cache backends and stub executables without network or cloud
dependencies.

To preserve the security and packaging invariants of
[ADR-0008](../../../adr/ADR-0008-repository-layout-and-package-structure.md) and
[ADR-0009](../../../adr/ADR-0009-abstraction-layers-and-dependency-injection.md)
(Decision 2):
1. `Worker` MUST perform all transit reads and writes exclusively through the
   injected `ArtifactCache` protocol interface (`SPEC-ARTIFACT-CACHE`:
   `get_manifest`, `get_repo_archive`, `get_finding`, `put_task_output`,
   `put_receipt` and their underlying `ArtifactCache` primitives) and MUST NOT
   directly invoke `google.cloud.storage`, `gcloud storage`, or `gsutil`.
2. Both `Launcher` (client) and `Worker` (server) MUST depend downward on
   `launcher.cache` (`Launcher --> ArtifactCache <-- Worker`) for shared wire
   models (`RunManifest`, `ExecutionReceipt`, `ScanMetadata`) so neither peer
   imports the other.
3. `packages/launcher` (including `launcher.worker`) MUST contain zero imports
   of `packages/repohost` (`repohost`).
4. When `--cache-uri` uses the `file://` scheme (or a local path), `cm-worker`
   MUST NOT import `google.cloud` or `gcsfs`, satisfying the Two-Way Door
   lazy-import invariant.

#### Scenario: Local `file://` Execution Without Cloud SDK Installed

- **GIVEN** optional cloud storage packages (`gcsfs` and `google.cloud`) are
  unavailable in the Python environment and `--cache-uri file:///tmp/cache` is
  provided
- **WHEN** `cm-worker --cache-uri file:///tmp/cache --task-id find` runs
- **THEN** `cm-worker` resolves the local `ArtifactCache` and completes the task
  without raising `ImportError` or `ModuleNotFoundError`.

#### Scenario: Zero `repohost` Imports in `launcher.worker`

- **GIVEN** all Python modules under `packages/launcher/src/launcher/`
- **WHEN** module imports are inspected
- **THEN** no module imports `repohost` or any submodule of `repohost`.

______________________________________________________________________

### REQ-0003: Step 1 — Scratch Initialization & Task Manifest Resolution (`RunManifest`) {#req-0003}

- **CUJ UUID:** `cuj-worker-manifest-validation`

At the start of a task execution (before reading `manifest.json`), `Worker` MUST
clean any pre-existing contents under `work_dir` (default `/tmp/work`) and
create three separate sibling directories: `work_dir / "repo"`,
`work_dir / "inputs"`, and `work_dir / "out"`, initializing
`work_dir / "out" / "runner.log"`. Keeping `inputs/` and `out/` strictly outside
`work_dir / "repo"` guarantees that staged inputs and logs never dirty the
repository working tree (`SPEC-FIX` `REQ-0003`, `SPEC-VERIFY` `REQ-0003`) and
that `work_dir / "out" / "runner.log"` is ready to capture Step 1 validation
errors. `Worker` MUST also clean `work_dir` in a `finally` block after Step 4
output publication completes.

In Step 1, `Worker` MUST read and validate `tasks/<task_id>/input/manifest.json`
from `ArtifactCache` and deserialize it into a `RunManifest`:

1. **Supported Commands:** `RunManifest.command` MUST be one of `init`,
   `find-diff` (normalizing the alias `find` to `find-diff`), `fix`, or
   `verify`.
2. **Command-Specific & Optional Field Validation:**
   - For `find-diff`: `base_ref` (or alias `diff_base`) MUST be a non-empty
     string specifying the Git base ref or SHA for differential scanning.
   - For `fix` and `verify`: `finding_key` MUST be a non-empty relative cache
     key (e.g., `findings/finding-001.sarif`) without leading slashes or `..`
     segments.
   - When `diff_workers` is provided, it MUST be an integer in the inclusive
     range `1..16` ([SPEC-FIND-DIFF](../find-diff/spec.md) `REQ-0002`).
3. **Failure Handling (`ArtifactCacheError` & Validation Mapping):**
   - **Exit Code `2` (`EXIT_USAGE_ERROR`):** If reading `manifest.json` raises
     `ArtifactNotFoundError`, `ArtifactIntegrityError`, or
     `InvalidCacheKeyError`, or if `manifest.json` specifies an unknown
     `command`, omits required command-specific fields, or provides an
     out-of-range field (`ValueError`), `Worker` MUST log the validation error
     to `stderr` and `work_dir / "out" / "runner.log"`, publish an
     `ExecutionReceipt` with `status="FAILURE"`, `exit_code=2`, and a non-empty
     `error` string to `tasks/<task_id>/output/receipt.json` (along with
     `tasks/<task_id>/output/runner.log`), and return exit code `2`.
   - **Exit Code `3` (`EXIT_EXECUTION_FAILURE`):** If reading `manifest.json`
     raises `CacheConfigurationError`, `CachePermissionError`, or
     `CacheTransientError`, `Worker` MUST log the error to `stderr` and
     `work_dir / "out" / "runner.log"`, attempt to publish a failure
     `ExecutionReceipt` (`status="FAILURE"`, `exit_code=3`, and a non-empty
     `error` string), spill `runner.log` to `sys.stderr` if cache upload fails,
     and return exit code `3`.

#### Scenario: Valid `fix` Manifest Resolves Finding Key

- **GIVEN** `tasks/fix-finding-001/input/manifest.json` sets `command="fix"`,
  `task_id="fix-finding-001"`, and `finding_key="findings/finding-001.sarif"`
- **WHEN** `Worker` executes Step 1 for task `fix-finding-001`
- **THEN** `work_dir / "repo"`, `work_dir / "inputs"`, and `work_dir / "out"`
  are initialized cleanly and `RunManifest` is parsed with `command="fix"` and
  `finding_key="findings/finding-001.sarif"`.

#### Scenario: Missing `finding_key` for `fix` Task Fails With Exit Code 2

- **GIVEN** `tasks/fix-001/input/manifest.json` specifies `{"command": "fix"}`
  without `finding_key`
- **WHEN** `Worker` executes task `fix-001`
- **THEN** `Worker` writes the validation error to
  `work_dir / "out" / "runner.log"`, publishes `tasks/fix-001/output/receipt.json`
  with `status="FAILURE"`, `exit_code=2`, and a non-empty `error` message,
  uploads `tasks/fix-001/output/runner.log`, and returns `2`.

______________________________________________________________________

### REQ-0004: Step 2 — Scratch Unpacking & Tar-Slip Defense {#req-0004}

- **CUJ UUID:** `cuj-worker-scratch-unpacking`

In Step 2, for all repository-backed commands (`init`, `find-diff`, `fix`,
`verify`), `Worker` MUST stage inputs into `work_dir / "inputs"` and materialize
the target repository into `work_dir / "repo"` while keeping `work_dir / "repo"`
free of untracked staging or output files ([SPEC-FIX](../fix/spec.md) `REQ-0003`,
[SPEC-VERIFY](../verify/spec.md) `REQ-0003`):

1. Download `shared/code.tar.gz` from `ArtifactCache` to
   `work_dir / "inputs" / "code.tar.gz"`.
2. Validate and extract the archive into `work_dir / "repo"`:
   - **Tar-Slip, Link & Device File Validation:** Before extracting any archive
     member, `Worker` MUST inspect all members and reject (failing with exit
     code `2` and a failure `ExecutionReceipt`) any member that:
     - has an absolute path or contains `..` path traversal segments;
     - resolves outside the canonical destination directory `work_dir / "repo"`;
     - is a symbolic link or hard link whose target is absolute or resolves
       outside the canonical destination directory `work_dir / "repo"`; or
     - is a character device, block device, or FIFO special file.
   - **POSIX Permission Preservation:** `Worker` MUST preserve each extracted
     regular file or directory's POSIX permission bits (`mode & 0o777`) so
     executable build scripts and test harnesses (e.g., `./gradlew`, configure
     scripts) remain executable.
3. **Single-Directory Wrapper Normalization & Bounded Git History:** If
   `work_dir / "repo" / ".git"` does not exist at the top level and extraction
   yields a single top-level subdirectory, `Worker` MUST promote all entries
   from that subdirectory directly into `work_dir / "repo"` and remove the empty
   wrapper directory so `.git` (and its bounded commit history required by
   `cm-runner find-diff` per [SPEC-FIND-DIFF](../find-diff/spec.md)
   `REQ-0002.2`) resides directly at `work_dir / "repo" / ".git"`.
4. **Git Safe Directory Registration:** `Worker` MUST mark the unpacked
   repository path `work_dir / "repo"` as a trusted Git `safe.directory` when
   `git` is available on `$PATH`.
5. **Single-Finding SARIF Staging (`fix` and `verify`):** For `fix` and `verify`
   commands, `Worker` MUST download `manifest.finding_key` from `ArtifactCache`
   into `work_dir / "inputs" / <original_filename>` (outside `work_dir / "repo"`)
   so the original finding filename stem (e.g., `finding-001.sarif`) is
   preserved when `cm-runner` writes `<stem>.diff` or `<stem>.sarif` into
   `work_dir / "out"`.

If fetching `shared/code.tar.gz` or `manifest.finding_key` from `ArtifactCache`
raises `ArtifactNotFoundError`, `ArtifactIntegrityError`, or
`InvalidCacheKeyError`, or if archive extraction fails tar-slip or archive
format validation, `Worker` MUST record an `ExecutionReceipt` with
`status="FAILURE"`, `exit_code=2`, and a non-empty `error` string and return
`2`. If `ArtifactCache` raises `CacheConfigurationError`,
`CachePermissionError`, or `CacheTransientError` during workspace staging,
`Worker` MUST record a failure receipt with `exit_code=3` and a non-empty
`error` string (or spill `runner.log` to `sys.stderr` if cache upload fails) and
return `3`.

#### Scenario: Extracting a Nested Archive With Executable Script Permissions

- **GIVEN** `shared/code.tar.gz` contains a top-level wrapper folder
  `my-repo/.git` and an executable script `my-repo/scripts/build.sh` with mode
  `0o755`
- **WHEN** `Worker` materializes the workspace into `/tmp/work/repo`
- **THEN** `/tmp/work/repo/.git` exists at the root of `/tmp/work/repo`,
  `/tmp/work/repo/scripts/build.sh` retains executable permissions (`0o755`),
  and `/tmp/work/repo` contains no untracked staging files from
  `/tmp/work/inputs`.

#### Scenario: Rejecting a Tar Archive With Path Traversal or Escaping Symlink

- **GIVEN** `shared/code.tar.gz` contains a member `../../etc/passwd` or a
  symlink pointing to `/etc/shadow`
- **WHEN** `Worker` attempts to extract `shared/code.tar.gz` for task `find`
- **THEN** `Worker` rejects the archive before writing any file outside
  `/tmp/work/repo`, publishes `receipt.json` with `status="FAILURE"`,
  `exit_code=2`, and a non-empty `error` message, and returns `2`.

______________________________________________________________________

### REQ-0005: Step 3 — `cm-runner` Execution & Stream Isolation {#req-0005}

- **CUJ UUID:** `cuj-worker-subprocess-exec`

In Step 3, `Worker` MUST construct and execute the `cm-runner` command line in a
dedicated child process group:

1. **Command Construction:**
   - Top-level flags: `[cm_runner_binary]` followed by
     `["--log-level", manifest.log_level]` when `manifest.log_level` is set.
   - `init`: passes `init` and `--repo <repo_dir>`.
   - `find-diff`: passes `find-diff`, `--repo <repo_dir>`,
     `--base <manifest.base_ref>`, `--out <out_dir>`, plus
     `--diff-workers <manifest.diff_workers>` when `manifest.diff_workers` is
     set.
   - `fix`: passes `fix`, `--repo <repo_dir>`,
     `--finding <staged_finding_path>`, and `--out <out_dir>`.
   - `verify`: passes `verify`, `--repo <repo_dir>`,
     `--finding <staged_finding_path>`, and `--out <out_dir>`.
   - Any additional `manifest.extra_args` are appended to the command list.
2. **Process Group & Environment Pass-Through Contract
   ([ADR-0003](../../../adr/ADR-0003-environment-scrubbing.md),
   [SPEC-GHA](../github-actions/spec.md),
   [SPEC-GITLAB-CI](../gitlab-ci/spec.md)):**
   - `Worker` MUST start `cm-runner` in a new process group/session (so the
     entire child process tree can be signaled as a unit on preemption) and
     inherit the container process environment without denylist scrubbing or
     `<repo_dir>/.codemender_cache` redirection (so mounted credentials such as
     `GOOGLE_APPLICATION_CREDENTIALS` and `CODEMENDER_SANDBOX_*` settings flow
     through to `cm-runner` and `cm`).
3. **Stream Isolation & Live Log Teeing
   ([ADR-0001](../../../adr/ADR-0001-machine-readable-output-contract.md),
   [SPEC-FIND-DIFF](../find-diff/spec.md), [SPEC-FIX](../fix/spec.md),
   [SPEC-VERIFY](../verify/spec.md)):**
   - `cm-worker` itself MUST emit zero bytes to its own `sys.stdout`.
   - For artifact-producing repository commands (`find-diff`, `fix`, `verify`),
     where `--out <out_dir>` writes the canonical artifacts (`report.sarif`,
     `report.md`, `<finding>.diff`, `<finding>.sarif`) to disk and `cm-runner`'s
     `stdout` carries duplicate SARIF JSON or raw binary diff bytes, `Worker`
     MUST discard `cm-runner`'s `stdout` and tee `cm-runner`'s `stderr` live to
     `sys.stderr` and `work_dir / "out" / "runner.log"`.
   - For `init` (which takes no `--out` flag and emits UTF-8 NDJSON `check`
     events on `cm-runner`'s `stdout` alongside diagnostics on `stderr`),
     `Worker` MUST capture both `cm-runner`'s `stdout` and `stderr` into
     `work_dir / "out" / "runner.log"` and `sys.stderr` while still writing
     nothing to `cm-worker`'s own `sys.stdout`.

#### Scenario: Running `cm-runner find-diff` Isolates `stderr` Logs From `stdout` SARIF Payload

- **GIVEN** a `RunManifest` with `command="find-diff"`,
  `base_ref="origin/main"`, and `diff_workers=4`
- **WHEN** `Worker` executes Step 3
- **THEN** `Worker` invokes `cm-runner find-diff --repo /tmp/work/repo --base origin/main --out /tmp/work/out --diff-workers 4`
  in a new process group with the inherited container environment, discards
  `cm-runner`'s duplicate `stdout` SARIF stream, and tees `cm-runner`'s `stderr`
  diagnostics live to `sys.stderr` and `/tmp/work/out/runner.log`.

#### Scenario: Running `cm-runner init` Captures `check` Events Into `runner.log`

- **GIVEN** a `RunManifest` with `command="init"`
- **WHEN** `Worker` executes Step 3
- **THEN** `Worker` invokes `cm-runner init --repo /tmp/work/repo`, captures
  both `cm-runner`'s `stdout` (`check` NDJSON events) and `stderr` diagnostics
  into `/tmp/work/out/runner.log` and `sys.stderr`, and emits zero bytes to
  `cm-worker`'s `sys.stdout`.

______________________________________________________________________

### REQ-0006: Step 4 — `ExecutionReceipt` & Artifact Publication {#req-0006}

- **CUJ UUID:** `cuj-worker-receipt-publish`

In Step 4, after `cm-runner` finishes (or upon an earlier validation/execution
error), `Worker` MUST generate an `ExecutionReceipt` and publish all task
outputs to `tasks/<task_id>/output/` in `ArtifactCache`:

1. **Raw-Byte Output Artifact Upload & Role Mapping:**
   - Upload every file produced in `work_dir / "out"` (such as `report.sarif`,
     `report.md`, `<finding>.diff`, `<finding>.sarif`, and `runner.log`) to
     `tasks/<task_id>/output/<filename>` as raw bytes (without text re-encoding
     or line-ending normalization, preserving `\r\n` and binary diff hunks per
     [SPEC-GHA-FIX](../github-actions-fix/spec.md) `REQ-0003.7`).
   - Record uploaded files in `receipt.artifacts` using the deterministic role
     mapping: `runner.log` -> `"log"`, `*.sarif` -> `"sarif"`, `*.md` ->
     `"report"`, and `*.diff` -> `"patch"`.
2. **Active Finding Count Extraction:**
   - When a SARIF artifact (`report.sarif` or `<finding>.sarif`) is present in
     `work_dir / "out"`, parse `runs[0].results` and set `receipt.finding_count`
     to the count of results that are not dismissed (`properties.status != "DISMISSED"`
     and no suppression entry, per [SPEC-VERIFY](../verify/spec.md) `REQ-0005`),
     defaulting to `0` if no SARIF file exists or if parsing fails.
3. **Receipt Serialization & Error Disambiguation (`no_fix` vs. Infrastructure Failure):**
   - Write `ExecutionReceipt` to `tasks/<task_id>/output/receipt.json`
     containing `status` (`"SUCCESS"` when `exit_code == 0`, `"FAILURE"`
     otherwise), `exit_code`, `command`, `task_id`, `started_at`,
     `completed_at`, `duration_seconds`, `finding_count`, `artifacts`, and
     `error`:
     - When `exit_code == 0`, `error` MUST be `None` (`null`).
     - When `cm-runner` itself runs to completion and returns non-zero exit code
       `3` (for example, `cm-runner fix` exiting `3` when no valid patch could
       be synthesized per [SPEC-FIX](../fix/spec.md) `REQ-0007`), `Worker` MUST
       still complete Step 4 and publish `runner.log` and `receipt.json` with
       `status="FAILURE"`, `exit_code=3`, and `error=None` (`null`), allowing
       downstream `Launcher` and [SPEC-GHA-FIX](../github-actions-fix/spec.md)
       (`REQ-0005.5`) to distinguish a normal `no_fix` outcome (`error is None`,
       diagnostics in `runner.log`) from a worker/infrastructure failure.
     - When `Worker` itself encounters a manifest/archive validation error
       (`exit_code == 2`), `cm-runner` usage error (`exit_code == 2`), missing
       `cm-runner` executable (`FileNotFoundError`), `ArtifactCacheError` /
       storage I/O exception, or preemption signal (`130` / `143`), `error` MUST
       be a non-empty human-readable string describing the failure.
4. **Exit Code Propagation:**
   - If `cm-runner` exits with `0`, `Worker` MUST return `0` regardless of
     `finding_count`.
   - If `cm-runner` exits with `2` (usage error), `Worker` MUST return `2`.
   - If `cm-runner` exits with any other non-zero code (or fails to start), or
     if `ArtifactCache` raises `CacheConfigurationError`,
     `CachePermissionError`, `CacheTransientError`, or any `ArtifactCacheError`
     during Step 4 publication, `Worker` MUST map the return code to `3`
     (execution failure, unless preserving an earlier exit code `2`, `130`, or
     `143`) while recording the failure details in `receipt.json`.
5. **Fallback Log Spill on Cache Upload Failure:**
   - If `exit_code != 0` and uploading `runner.log` or `receipt.json` to
     `ArtifactCache` raises `CacheConfigurationError`, `CachePermissionError`,
     `CacheTransientError`, or any `ArtifactCacheError` / `OSError`, `Worker`
     MUST spill the contents of `work_dir / "out" / "runner.log"` directly to
     `sys.stderr` framed by `=== [FALLBACK LOG SPILL: ...] ===` and
     `=== [END FALLBACK LOG SPILL] ===` so failure diagnostics are preserved in
     container stderr logs.

#### Scenario: Successful `fix` Task Publishes Diff, Log, and Receipt

- **GIVEN** `cm-runner fix` succeeds (`exit_code == 0`) and writes
  `/tmp/work/out/finding-001.diff`
- **WHEN** `Worker` executes Step 4 for task `fix-finding-001`
- **THEN** `tasks/fix-finding-001/output/finding-001.diff`,
  `tasks/fix-finding-001/output/runner.log`, and
  `tasks/fix-finding-001/output/receipt.json` exist in `ArtifactCache`,
  `receipt.json` has `status="SUCCESS"`, `exit_code=0`, `error=None`, and
  `artifacts["patch"] == "tasks/fix-finding-001/output/finding-001.diff"`, and
  `Worker` returns `0`.

#### Scenario: `cm-runner fix` Returning Exit Code 3 ("No Fix") Still Publishes Receipt With `error=None`

- **GIVEN** `cm-runner fix` finishes with exit code `3` without producing a
  `.diff` file in `/tmp/work/out`
- **WHEN** `Worker` executes Step 4 for task `fix-finding-001`
- **THEN** `Worker` uploads `tasks/fix-finding-001/output/runner.log` and
  `tasks/fix-finding-001/output/receipt.json` with `status="FAILURE"`,
  `exit_code=3`, `error=None`, and `artifacts == {"log": "tasks/fix-finding-001/output/runner.log"}`,
  and returns `3`.

#### Scenario: `cm-runner verify` Dismissing a Finding Sets `finding_count` to 0

- **GIVEN** `cm-runner verify` succeeds (`exit_code == 0`) and writes
  `/tmp/work/out/finding-001.sarif` whose single result has
  `properties.status == "DISMISSED"`
- **WHEN** `Worker` executes Step 4 for task `verify-finding-001`
- **THEN** `Worker` uploads `tasks/verify-finding-001/output/finding-001.sarif`,
  sets `receipt.finding_count == 0` and
  `receipt.artifacts["sarif"] == "tasks/verify-finding-001/output/finding-001.sarif"`,
  and returns `0`.

#### Scenario: Cache Upload Failure on Non-Zero Exit Triggers Fallback Log Spill

- **GIVEN** `cm-runner` fails with exit code `3` and `ArtifactCache` raises
  `CacheTransientError` (or `OSError`) during output synchronization
- **WHEN** `Worker` finalizes the failed task
- **THEN** `Worker` writes the framed `FALLBACK LOG SPILL` block containing
  `runner.log` to `sys.stderr` and returns exit code `3`.

______________________________________________________________________

### REQ-0007: Preemption Signal Handling (`SIGTERM` & `SIGINT`) {#req-0007}

- **CUJ UUID:** `cuj-worker-preemption-signals`

When `cm-worker` runs a task, the worker MUST install signal handlers for
`SIGTERM` and `SIGINT` to handle container preemption and CI cancellation
gracefully
([ADR-0007](../../../adr/ADR-0007-cloud-run-job-invocation-interface-cli-vs-python-library.md)):

1. **Child Process Group Termination:** Upon receiving `SIGTERM` or `SIGINT`,
   if a `cm-runner` subprocess is active, `Worker` MUST forward `SIGTERM` to the
   entire child process group, wait up to a bounded grace period for the child
   group to exit, and escalate to `SIGKILL` for the process group if it remains
   alive.
2. **Preemption Receipt & Log Flush:** Before terminating, `Worker` MUST write
   and upload a failure `ExecutionReceipt` (`status="FAILURE"`,
   `exit_code=143` for `SIGTERM` or `exit_code=130` for `SIGINT`, with a
   non-empty `error` string indicating signal preemption) and `runner.log` to
   `tasks/<task_id>/output/` in `ArtifactCache` (falling back to `stderr` log
   spill if cache upload fails).
3. **Standard Exit Codes:** `cm-worker` MUST exit with code `143` on `SIGTERM`
   and code `130` on `SIGINT`.

#### Scenario: `SIGTERM` Terminates Child Group and Flushes Failure Receipt

- **GIVEN** `Worker` is executing a long-running `cm-runner fix` child process
  group
- **WHEN** the container receives `SIGTERM`
- **THEN** `Worker` terminates the `cm-runner` child process group, uploads
  `tasks/<task_id>/output/runner.log` and `tasks/<task_id>/output/receipt.json`
  with `status="FAILURE"`, `exit_code=143`, and a non-empty `error` string,
  cleans `work_dir`, and exits with code `143`.

#### Scenario: Interactive Abort via `SIGINT` Returns Exit Code 130

- **GIVEN** `Worker` is executing a task when `SIGINT` is delivered
- **WHEN** the signal handler runs
- **THEN** `Worker` terminates the active child process group, flushes
  `receipt.json` with `status="FAILURE"`, `exit_code=130`, and a non-empty
  `error` string, and exits with code `130`.
