<!--
sdd_spec_id: SPEC-LAUNCHER
sdd_implements: None
sdd_extends: None
sdd_level: COMPONENT
sdd_archetype: LIBRARY
sdd_cujspec_product: codemender-runner
-->

<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-11-03' }
*-->

# SPEC-LAUNCHER: Host-Side Client & Container Orchestrator (`launcher.base`)

**Implements Spec:** None

**Extends Spec:** None

**Governing Journeys:**
- **Pull Request Contributor (`persona-pr-author`)**: As a developer submitting pull
  requests or waiting on automated patch suggestions, I want the host-side
  `Launcher` client to orchestrate scan (`find`), repair (`fix`), and verification
  (`verify`) tasks reliably across local Docker/gVisor and Google Cloud Run Jobs,
  streaming repair diffs progressively to my PR as each task finishes, without
  corrupting repository files or leaking sensitive credentials.
- **Security & Compliance Lead (`persona-security-auditor`)**: As a security lead
  governing untrusted code execution in [SPEC-0003](../runner-container-packaging/spec.md),
  the [CodeMender Runner System Specification](../tech/systems/codemender-runner/spec.md),
  and the [Security and Trust Zones System Specification](../tech/systems/security-and-trust-zones/spec.md),
  I need the `Launcher` orchestrator to remain 100% decoupled from repository
  hosting APIs (`packages/repohost`), strictly prevent repository host tokens from
  reaching sandbox containers, enforce bounded worker concurrency, guarantee
  signal-safe container termination on cancellation, and ensure every task
  invocation deterministically returns a valid `ExecutionReceipt`.

---

## Overview

This specification defines the normative interfaces, dependency injection
boundaries, workflow orchestration lifecycle, bounded concurrency engine,
dual selection modes, context management, and concrete backend contracts for
`launcher.base.Launcher` and its concrete implementations (`LocalDockerLauncher`
and `CloudRunLauncher`) in `packages/launcher`.

As established in
the [CodeMender Runner System Design](../tech/systems/codemender-runner/design.md)
(Decisions 1, 2, 3, and 4),
the [Security and Trust Zones System Design](../tech/systems/security-and-trust-zones/design.md)
(Decisions 2 and 4), and
[SPEC-WORKER](../worker/spec.md), `Launcher` (on the trusted outer CI host) and
`Worker` (`cm-worker`, inside the untrusted sandbox container) form the client
and server halves of a unified container execution protocol. Communicating
exclusively through an injected `ArtifactCache` (`SPEC-ARTIFACT-CACHE`:
`launcher.cache.ArtifactCache` / `open_artifact_cache`), `Launcher` implements
the high-level domain workflows:
1. `find`: Packages the repository snapshot with strict credential and symlink
   sanitization, writes the scan manifest, runs the worker, parses the raw
   SARIF output using `codemender.sarif.SarifDocument`, splits individual findings
   into `findings/finding-NNN.sarif`, and records `ScanMetadata`.
2. `fix`: Fans out parallel repair tasks over assigned findings, orchestrates
   bounded worker concurrency (`run_parallel`), and yields `FixOutcome` records
   in completion order via Python generator streaming.
3. `verify`: Fans out on-demand verification tasks over assigned findings, parses
   updated verification SARIF documents, and yields `VerifyOutcome` records via
   generator streaming.

`Launcher` is completely independent of repository hosting platforms
(`packages/repohost`), allowing the exact same orchestrator to power interactive
GitHub Actions and GitLab CI PR workflows as well as headless branch audits,
quarterly security campaigns, and local developer CLI executions.

Detailed component architecture, class diagrams, sequence flows, data models,
and fallback receipt synthesis are specified in [design.md](design.md).

This specification is governed by and references:
- [CodeMender Runner System Design](../tech/systems/codemender-runner/design.md) ([Specification](../tech/systems/codemender-runner/spec.md)):
  Abstraction layers, dependency injection seams, asynchronous workflows, and
  phased CI execution model.
- [Security and Trust Zones System Design](../tech/systems/security-and-trust-zones/design.md) ([Specification](../tech/systems/security-and-trust-zones/spec.md)):
  Three-zone permission isolation, local WIF `/creds` read-only mount, and
  Zone 1 repository snapshot and environment sanitization gates.
- [ADR-0008](../../../adr/ADR-0008-repository-layout-and-package-structure.md):
  Three-package workspace layout (`packages/{codemender,launcher,repohost}`) and
  package dependency rules. Supersedes ADR-0002.
- [Distributed Execution and Transit System Specification](../tech/systems/distributed-execution-and-transit/spec.md)
  and [Design](../tech/systems/distributed-execution-and-transit/design.md):
  Multi-platform archive snapshot transit, container-private scratch hydration,
  typed in-process remote job dispatch, `ExecutionReceipt`/log recovery, and
  cooperative cancellation.
- [ADR-0003](../../../adr/ADR-0003-environment-scrubbing.md):
  Environment scrubbing and container configuration allowlist.
- [ADR-0002](../../../adr/ADR-0002.md):
  Machine-readable exit codes (`0`, `2`, `3`) and cross-boundary artifacts.
- [SPEC-WORKER](../worker/spec.md):
  Unified sandbox container worker (`launcher.worker` / `cm-worker`) server protocol.
- [SPEC-ARTIFACT-CACHE](../artifact-cache/spec.md):
  `ArtifactCache` storage primitives, typed client-worker models (`RunManifest`,
  `ExecutionReceipt`), and Two-Way Door lazy-import isolation.
- [SPEC-FIND-DIFF](../find-diff/spec.md), [SPEC-FIX](../fix/spec.md), and
  [SPEC-VERIFY](../verify/spec.md):
  Underlying `cm-runner` subcommand execution semantics.

---

## Requirements

### REQ-0001: Abstract Base Class & Dependency Injection Seam (`Launcher(cache: ArtifactCache)`) {#req-0001}

- **CUJ UUID:** `cuj-launcher-di-seam`

`packages/launcher/src/launcher/base.py` MUST define `Launcher` as an abstract
base class (`abc.ABC`).

The `Launcher` constructor MUST accept an `ArtifactCache` instance (`SPEC-ARTIFACT-CACHE`)
via constructor dependency injection:
```python
class Launcher(abc.ABC):
  def __init__(self, cache: ArtifactCache) -> None:
    ...
```

`packages/launcher` (including `launcher.base` and all launcher backends) MUST NOT
import `packages/repohost`, and `Launcher` MUST NOT accept a `RepoHost` instance
in its constructor or any of its methods.

Both `Launcher` (client) and `Worker` (server) MUST depend downward on
`launcher.cache` in an acyclic hub-and-spoke hierarchy:
`Launcher --> ArtifactCache <-- Worker`. `Launcher` MUST NOT import `launcher.worker`.

#### Scenario: Hermetic Launcher Instantiation with Local Cache
- **GIVEN** a temporary local cache initialized via `LocalArtifactCache(tmp_path)`
- **WHEN** a concrete `Launcher` subclass is instantiated with the cache
- **THEN** the launcher instance binds the cache to `self.cache`
- **AND** zero imports of `repohost` or `launcher.worker` are executed.

---

### REQ-0002: Repository Snapshot Archiving & Sanitization (`Launcher.ensure_repo_snapshot`) {#req-0002}

- **CUJ UUID:** `cuj-launcher-repo-snapshot`

`Launcher.ensure_repo_snapshot(repo_dir: Path, base_ref: str, force: bool = False) -> str`
MUST package the target Git repository into `shared/code.tar.gz` within the
injected `ArtifactCache` and return the cache key string `"shared/code.tar.gz"`.

`ensure_repo_snapshot` MUST delegate archive packaging to
`launcher.cache.archive.create_repo_archive_excluding_sensitive_files(repo_dir, dest_path, base_ref=base_ref)`.

Per [Security and Trust Zones Decision 4](../tech/systems/security-and-trust-zones/design.md#decision-4-boundary-enforced-workspace-gitconfig-and-environment-sanitization-in-launcher),
the created archive MUST satisfy the following four sanitization gates before upload:
1. **Clean `.git/config` Allowlist Replacement**: The archive MUST replace `.git/config`
   with a minimal format-only allowlist (`core.repositoryformatversion = 0`,
   `core.filemode = true`, `core.bare = false`, and optional `extensions.objectFormat`
   or `extensions.refStorage`). It MUST NOT retain regex-scrubbed original config lines,
   blocking git hooks, `diff.<driver>.textconv`, `fsmonitor`, credential helpers, and
   `http.extraheader` tokens.
2. **Hook Deletion**: The archive MUST NOT contain `.git/hooks` or any hook scripts.
3. **Base-Commit `.codemender.yaml` Pinning**: The archive MUST resolve `.codemender.yaml`
   strictly from `base_ref`, ignoring untrusted PR modifications, and MUST delete
   `.codemender/command_policy.yaml`.
4. **Symlink Boundary Checks**: Symbolic links inside `repo_dir` MUST be archived as
   symlinks without dereferencing, and any symlinks resolving outside `repo_dir`
   MUST be rejected with `ValidationError`.
5. **Path Exclusions**: The archive MUST explicitly exclude `.cm_project`, `.exploit`,
   `gha-creds-*.json`, and `.codemender-cache/`.
6. **Submodule & Partial Clone Guard**: If Git submodules or partial clone filters are
   detected, `ensure_repo_snapshot` MUST fail closed with `ValidationError`.
7. **Idempotency**: If `"shared/code.tar.gz"` already exists in `self.cache` and the
   repository `HEAD` commit SHA matches the cached snapshot, `ensure_repo_snapshot`
   MUST NOT re-package or re-upload the archive unless `force=True`.

#### Scenario: Repository Snapshot Caching
- **GIVEN** a valid local Git repository directory at `/repo`
- **WHEN** `launcher.ensure_repo_snapshot(Path("/repo"), base_ref="origin/main")` is called
- **THEN** the archive contains an allowlisted `.git/config` and zero hooks
- **AND** `self.cache.get_bytes("shared/code.tar.gz")` succeeds
- **AND** calling `ensure_repo_snapshot` a second time with `force=False` reuses the
  existing cache key without creating a new archive.

---

### REQ-0003: Differential Scan Orchestration (`Launcher.find`) {#req-0003}

- **CUJ UUID:** `cuj-launcher-find-orchestration`

`Launcher.find` MUST orchestrate differential vulnerability scanning and return a
structured `FindOutcome`.

`find` MUST support the following signature:
```python
def find(
    self,
    repo_dir: Path,
    base_ref: str,
    *,
    diff_workers: int | None = None,
    timeout_seconds: int | None = None,
) -> FindOutcome:
  ...
```

`find` MUST require a non-empty `base_ref`. If `base_ref` is empty or invalid, `find`
MUST raise `ValidationError`. If `diff_workers` is supplied, it MUST be validated
per `RunManifest` bounds (`1 <= diff_workers <= 64`), raising `ValidationError` if out of bounds.

`find` MUST execute the following sequential stages:
1. **Snapshot Repository**: Call `self.ensure_repo_snapshot(repo_dir, base_ref=base_ref)`.
2. **Stage RunManifest**: Write `tasks/find/input/manifest.json` via
   `self.cache.put_manifest("find", manifest)` where `manifest` specifies
   `command="find-diff"`, `task_id="find"`, `base_ref=base_ref`, and
   `diff_workers=diff_workers`.
3. **Dispatch Worker**: Invoke `receipt = self.run_worker("find", timeout_seconds=timeout_seconds)`.
4. **Evaluate Receipt**: Check `receipt.status` and `receipt.exit_code`.
   - If `receipt.exit_code != 0` or `receipt.status != "SUCCESS"`, `find` MUST raise
     `LauncherError` containing failure details and captured worker diagnostics.
5. **Process Scan Outputs**:
   - Read `tasks/find/output/report.sarif` via `self.cache.get_task_output("find", "report.sarif", dest_path)`.
   - Parse SARIF 2.1.0 JSON using `codemender.sarif.SarifDocument.from_json()` and split
     individual findings into normalized 1-finding SARIF documents via
     `SarifDocument.to_findings()`.
   - Store individual finding SARIF files deterministically under `findings/{finding_id}.sarif`
     in `self.cache`.
   - Construct `ScanMetadata(base_ref=base_ref, head_sha=head_sha, finding_ids=finding_ids, fingerprints=fingerprints)`
     and store it at `shared/scan-meta.json` via `self.cache.put_json("shared/scan-meta.json", scan_meta.to_dict())`.
6. **Return Outcome**: Return `FindOutcome(scan_meta=scan_meta, finding_ids=finding_ids, sarif_report=sarif_dict, receipt=receipt)`.

#### Scenario: Successful `find` Execution Splits SARIF Findings
- **GIVEN** a repository with 2 vulnerable code paths
- **WHEN** `launcher.find(repo_dir, base_ref="origin/main")` is invoked
- **AND** the sandbox worker exits with code `0` and emits `report.sarif`
- **THEN** `launcher.find()` returns a `FindOutcome` with `len(finding_ids) == 2`
- **AND** `findings/finding-001.sarif` and `findings/finding-002.sarif` are staged in `self.cache`
- **AND** `shared/scan-meta.json` is stored in `self.cache`.

---

### REQ-0004: Patch Generation Generator (`Launcher.fix`) {#req-0004}

- **CUJ UUID:** `cuj-launcher-fix-generator`

`Launcher.fix` MUST be implemented as a Python generator yielding `FixOutcome`
instances in completion order.

`fix` MUST support the following signature:
```python
def fix(
    self,
    finding_ids: Sequence[str] | None = None,
    shard: tuple[int, int] | None = None,
    max_workers: int = 4,
    timeout_seconds: int | None = None,
) -> Iterator[FixOutcome]:
  ...
```

`fix` MUST enforce the dual selection modes and validate inputs:
1. **Mode Conflict Validation**: If both `finding_ids is not None` and `shard is not None`,
   `fix` MUST raise `ValidationError("Cannot specify both finding_ids and shard.")`.
2. **Mode A (Explicit IDs / Default All)**:
   - If `finding_ids` is provided, `fix` processes only the specified finding IDs.
   - If both `finding_ids` and `shard` are `None`, `fix` reads `shared/scan-meta.json`
     from `self.cache` via `get_json()` and selects all discovered finding IDs.
3. **Mode B (Deterministic Shard Slicing)**:
   - If `shard=(K, N)` is provided, `K` and `N` MUST be integers where `1 <= K <= N`.
     If `K < 1` or `K > N`, `fix` MUST raise `ValidationError(f"Invalid shard (K, N): {shard}; must have 1 <= K <= N.")`.
   - `fix` reads `shared/scan-meta.json` from `self.cache`, sorts `finding_ids`
     deterministically, and selects the slice `finding_ids[K - 1 :: N]`.
   - If the resulting slice is empty (i.e. `K <= N` but `K - 1 >= len(finding_ids)`),
     `fix` MUST immediately terminate iteration without error (yielding zero items).
4. **Untrusted `finding_id` Sanitization**: Every selected `finding_id` MUST be validated
   as a single-segment alphanumeric string (`^[a-zA-Z0-9_-]+$`, no `.tmp.`, no `/`,
   no `..`, no `:`). If invalid, `fix` MUST raise `ValidationError`.

For each selected finding ID (`finding_id`):
1. Construct task identifier `task_id = f"fix-{finding_id}"`.
2. Stage `tasks/{task_id}/input/manifest.json` with `command="fix"`, `task_id=task_id`,
   and `finding_key=f"findings/{finding_id}.sarif"`.
3. Dispatch execution concurrently via `self.run_parallel(...)`.
4. As each worker completes, read `tasks/{task_id}/output/receipt.json`,
   `tasks/{task_id}/output/runner.log`, and `tasks/{task_id}/output/{finding_id}.diff`
   (if present).
5. Yield `FixOutcome(finding_id=finding_id, diff=diff_text, receipt=receipt, log=log_text, error=receipt.error or "")`.

#### Scenario: Deterministic Shard Slicing in GitLab CI
- **GIVEN** a scan with 3 findings: `["finding-001", "finding-002", "finding-003"]`
- **WHEN** `launcher.fix(shard=(2, 2))` is invoked on node 2 of 2
- **THEN** `launcher.fix()` yields exactly 1 outcome for `"finding-002"`
- **AND** `tasks/fix-finding-002/input/manifest.json` is staged in the cache.

#### Scenario: Empty Shard Terminates Cleanly
- **GIVEN** a scan with 1 finding: `["finding-001"]`
- **WHEN** `launcher.fix(shard=(3, 4))` is invoked on node 3 of 4
- **THEN** `launcher.fix()` yields zero items and exits cleanly without error.

---

### REQ-0005: Patch Verification Generator (`Launcher.verify`) {#req-0005}

- **CUJ UUID:** `cuj-launcher-verify-generator`

`Launcher.verify` MUST be implemented as a Python generator yielding `VerifyOutcome`
instances in completion order.

`verify` MUST support the following signature:
```python
def verify(
    self,
    finding_ids: Sequence[str] | None = None,
    shard: tuple[int, int] | None = None,
    max_workers: int = 4,
    timeout_seconds: int | None = None,
) -> Iterator[VerifyOutcome]:
  ...
```

`verify` MUST enforce the exact same dual selection modes (Mode A, Mode B, conflict
validation, and untrusted `finding_id` sanitization) as `fix`.

For each selected finding ID (`finding_id`):
1. Construct task identifier `task_id = f"verify-{finding_id}"`.
2. Stage `tasks/{task_id}/input/manifest.json` with `command="verify"`, `task_id=task_id`,
   and `finding_key=f"findings/{finding_id}.sarif"`.
3. Dispatch execution concurrently via `self.run_parallel(...)`.
4. As each worker completes:
   - Read `tasks/{task_id}/output/receipt.json` and `tasks/{task_id}/output/runner.log`.
   - Read `tasks/{task_id}/output/verified.sarif` via `self.cache.get_task_output()`
     and parse the verification result status (`"DISMISSED"`, `"VERIFIED"`, `"OPEN"`,
     or `"ERROR"`).
5. Yield `VerifyOutcome(finding_id=finding_id, status=status, verified_sarif=sarif_dict, receipt=receipt, log=log_text, error=receipt.error or "")`.

#### Scenario: On-Demand Verify Yields Dismissed Verification Outcome
- **GIVEN** an existing candidate finding `"finding-001"`
- **WHEN** `launcher.verify(finding_ids=["finding-001"])` is invoked
- **AND** the sandbox worker exits code `0` and emits `verified.sarif` with status `"DISMISSED"`
- **THEN** `launcher.verify()` yields a `VerifyOutcome` where `outcome.status == "DISMISSED"`
- **AND** `outcome.ok` is `True`.

---

### REQ-0006: Bounded Concurrency Engine (`Launcher.run_parallel`) {#req-0006}

- **CUJ UUID:** `cuj-launcher-run-parallel`

`Launcher.run_parallel` MUST execute a collection of worker tasks concurrently
under a bounded thread pool and yield completed results in completion order
(`concurrent.futures.as_completed`).

`run_parallel` MUST support the following signature:
```python
def run_parallel(
    self,
    tasks: Sequence[ParallelTask],
    max_workers: int = 4,
) -> Iterator[ParallelResult]:
  ...
```

`run_parallel` MUST enforce:
1. `max_workers >= 1`. If `max_workers < 1`, `run_parallel` MUST raise `ValidationError`.
2. Bounded worker pool allocation: `workers = min(len(tasks), max_workers)`.
3. Completion-order streaming: Results MUST be yielded as soon as individual workers
   finish, rather than waiting for the slowest task in the batch.
4. Guaranteed resource teardown: If generator iteration is interrupted early
   (e.g., `GeneratorExit`, `KeyboardInterrupt`, `SIGTERM`, unhandled exception),
   `run_parallel` MUST invoke `self.stop()` in a `finally:` block before
   shutting down the thread pool.

#### Scenario: Bounded Parallel Execution Yields in Completion Order
- **GIVEN** 3 tasks submitted to `run_parallel(tasks, max_workers=2)`
- **WHEN** Task B finishes before Task A
- **THEN** Task B is yielded first
- **AND** the thread pool never exceeds 2 active threads concurrently.

---

### REQ-0007: Subclass Dispatch Contract & Guaranteed `ExecutionReceipt` Return (`run_worker`) {#req-0007}

- **CUJ UUID:** `cuj-launcher-run-worker-contract`

`Launcher.run_worker(task_id: str, timeout_seconds: int | None = None) -> ExecutionReceipt`
MUST be a concrete template method on `Launcher`.

`run_worker` MUST delegate container execution to a protected abstract method implemented
by concrete subclasses:
```python
@abc.abstractmethod
def _dispatch_worker(
    self,
    task_id: str,
    timeout_seconds: int | None = None,
) -> int:
  """Executes the container worker and returns the process exit code."""
  ...
```

`run_worker` MUST **always return an `ExecutionReceipt`** across all execution paths:
1. **Clean Container Completion**:
   - Checks for `tasks/{task_id}/output/receipt.json` in `self.cache`.
   - If present and valid, returns the receipt as written by `cm-worker`.
2. **Fail-Closed Fallback Synthesis (Crash, OOM, Missing Receipt)**:
   - If `tasks/{task_id}/output/receipt.json` is missing (regardless of container exit code)
     or if the container exited with non-zero exit code (`137`, `143`, `1`, etc.),
     `run_worker` MUST synthesize an `ExecutionReceipt` with:
     - `status="FAILURE"`.
     - `exit_code`: container process exit code (or `1` if process exited `0` without writing a receipt).
     - `command`: read from `tasks/{task_id}/input/manifest.json` (or `"unknown"` if manifest cannot be read).
     - `task_id=task_id`.
     - `started_at`: UTC ISO timestamp recorded prior to worker dispatch.
     - `completed_at`: current UTC ISO timestamp.
     - `duration_seconds`: elapsed seconds.
     - `finding_count=0`.
     - `artifacts={}`.
     - `error`: non-empty diagnostic message explaining termination.
   - `run_worker` MUST write the synthesized receipt back to
     `tasks/{task_id}/output/receipt.json` via `self.cache.put_json()` so
     downstream CI steps find a valid receipt.

#### Scenario: Synthesized Fail-Closed Receipt on Container OOM Kill
- **GIVEN** a worker container running task `"fix-finding-001"` that gets OOM-killed (exit `137`)
- **WHEN** `run_worker("fix-finding-001")` completes
- **THEN** it returns an `ExecutionReceipt` with `exit_code == 137` and `status == "FAILURE"`
- **AND** `tasks/fix-finding-001/output/receipt.json` is stored in the cache.

---

### REQ-0008: Concrete Local Backend (`LocalDockerLauncher`) & Host Isolation {#req-0008}

- **CUJ UUID:** `cuj-launcher-local-docker`

`packages/launcher/src/launcher/local/local_docker.py` MUST define `LocalDockerLauncher`
as a concrete subclass of `Launcher`.

`LocalDockerLauncher` MUST execute `cm-worker` inside a Docker container:
1. **Entrypoint & Arguments**: Container MUST use default entrypoint `cm-worker` and
   receive arguments `["--cache-uri", "file:///cache", "--task-id", task_id]`.
2. **Bind-Mount Isolation**:
   - Container mounts MUST be limited to `-v <host_cache_dir>:/cache` (read-write) and
     optional `-v <host_creds_file>:/creds/adc.json:ro` (read-only) for `LocalWifSa`
     per [Security and Trust Zones Decision 3](../tech/systems/security-and-trust-zones/design.md#decision-3-mode-switched-least-privilege-gcp-wif-local-single-sa-vs-cloud_run-coordinatorworker-dual-sa-split).
   - Zero repository workspace bind mounts (`-v /repo:/workspace`) MUST be passed.
   - Zero host-side `shutil.copytree` operations MUST be performed.
   - Host mount paths MUST NOT resolve symlinks escaping designated host root directories.
3. **Container Sandboxing & Capability Gating**:
   - Defaults to gVisor `runsc` runtime (`--runtime=runsc`).
   - `--cap-add SYS_ADMIN` MUST NOT be added when `runtime != "runsc"`. Under `runc`,
     zero additional Linux capabilities are granted.
4. **Forbidden Container Flags**:
   - MUST NOT use `--privileged`.
   - MUST NOT use `--security-opt label=disable` or `--security-opt ...=unconfined`.
   - MUST NOT use `--net=host`.
   - MUST NOT use bare `-e VAR` host environment inheritance.
5. **Environment Isolation**:
   - Repository host tokens (`GITHUB_TOKEN`, `CI_JOB_TOKEN`, `ACTIONS_*`) MUST NOT be passed.
   - Pass only explicit `KEY=VALUE` environment variables matching the [ADR-0003](../../../adr/ADR-0003-environment-scrubbing.md)
     allowlist (`GOOGLE_CLOUD_PROJECT`, `CODEMENDER_*`).
6. **Lock-Free Container Tracking & Termination**:
   - Active Docker container IDs MUST be tracked in a thread-safe `set[str]`.
   - `stop()` MUST be lock-free during external command execution: take a snapshot copy
     of active container IDs under lock, release the lock, and invoke `docker stop -t 5 <id>`
     followed by `docker kill <id>` on each tracked container.

#### Scenario: Local Docker Execution with Single Cache Mount
- **GIVEN** a `LocalArtifactCache` rooted at `/tmp/cache`
- **WHEN** `local_docker_launcher.run_worker("find")` is invoked
- **THEN** `docker run` is spawned with `-v /tmp/cache:/cache`
- **AND** zero repository bind mounts are passed.

---

### REQ-0009: Concrete Cloud Backend (`CloudRunLauncher`) & Two-Way Door Lazy Imports {#req-0009}

- **CUJ UUID:** `cuj-launcher-cloud-run`

`packages/launcher/src/launcher/cloudrun/cloud_run.py` MUST define `CloudRunLauncher`
as a concrete subclass of `Launcher`.

`CloudRunLauncher` MUST execute tasks via the Google Cloud Run Jobs API:
1. **Job Invocation**: Overrides container arguments with
   `args=["--cache-uri", cache.uri, "--task-id", task_id]`.
2. **Active Execution Tracking & Cancellation**: Active Cloud Run Job execution names
   MUST be tracked in a thread-safe `set[str]`. `stop()` MUST invoke execution
   cancellation for all tracked active executions without holding locks during RPC calls.
3. **Two-Way Door Lazy Imports**: `packages/launcher/src/launcher/base.py` and
   `LocalDockerLauncher` MUST NOT import `google.cloud` or `google-cloud-run`.
   `CloudRunLauncher` MUST isolate Cloud Run SDK imports so local runs never fail
   due to missing cloud dependencies.

#### Scenario: Lazy Import Protection in Local Environments
- **GIVEN** an environment where `google.cloud` is blocked (`sys.modules["google.cloud"] = None`)
- **WHEN** `launcher.base` and `LocalDockerLauncher` are imported
- **THEN** the imports succeed without raising `ImportError`.

---

### REQ-0010: Context Management, Signal Safety & Lifecycle Management (`Launcher.stop`) {#req-0010}

- **CUJ UUID:** `cuj-launcher-context-management`

`Launcher` MUST implement Python's context manager protocol:
```python
class Launcher(abc.ABC):
  def __enter__(self) -> Self:
    return self

  def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
    self.stop()
```

`Launcher.stop()` MUST:
1. Be thread-safe and signal-safe.
2. Terminate or cancel all in-flight worker containers or cloud executions.
3. Be idempotent: multiple successive calls to `stop()` MUST NOT raise errors.
4. Not deadlock or block indefinitely during shutdown.

#### Scenario: Context Manager Auto-Teardown on Exception
- **GIVEN** a `Launcher` instance used in a `with` block
- **WHEN** an unhandled exception is raised during workflow execution
- **THEN** `__exit__` automatically calls `self.stop()`
- **AND** all active containers are stopped before the exception propagates.

---

## Traceability Matrix

| Requirement | Description | Governing ADR / Spec | Realized Component |
| :--- | :--- | :--- | :--- |
| **REQ-0001** | ABC & Dependency Injection Seam (`ArtifactCache`) | [CodeMender Runner REQ-0001](../tech/systems/codemender-runner/spec.md#req-0001), [REQ-0003](../tech/systems/codemender-runner/spec.md#req-0003), ADR-0008 | `launcher.base.Launcher` |
| **REQ-0002** | Repository Snapshot Archiving & Sanitization (`ensure_repo_snapshot`) | [Security & Trust Zones Dec 4](../tech/systems/security-and-trust-zones/design.md#decision-4-boundary-enforced-workspace-gitconfig-and-environment-sanitization-in-launcher), [CodeMender Runner REQ-0005](../tech/systems/codemender-runner/spec.md#req-0005), [Distributed Execution & Transit REQ-0001](../tech/systems/distributed-execution-and-transit/spec.md#req-0001) | `launcher.base.Launcher.ensure_repo_snapshot` |
| **REQ-0003** | Differential Scan Orchestration (`find`) | [CodeMender Runner REQ-0003](../tech/systems/codemender-runner/spec.md#req-0003), SPEC-FIND-DIFF | `launcher.base.Launcher.find` |
| **REQ-0004** | Patch Generation Generator (`fix`) | [CodeMender Runner REQ-0003](../tech/systems/codemender-runner/spec.md#req-0003), [REQ-0004](../tech/systems/codemender-runner/spec.md#req-0004), SPEC-FIX | `launcher.base.Launcher.fix` |
| **REQ-0005** | Patch Verification Generator (`verify`) | [CodeMender Runner REQ-0003](../tech/systems/codemender-runner/spec.md#req-0003), [REQ-0004](../tech/systems/codemender-runner/spec.md#req-0004), SPEC-VERIFY | `launcher.base.Launcher.verify` |
| **REQ-0006** | Bounded Concurrency Engine (`run_parallel`) | [CodeMender Runner REQ-0004](../tech/systems/codemender-runner/spec.md#req-0004) | `launcher.base.Launcher.run_parallel` |
| **REQ-0007** | Template Dispatch & Guaranteed `ExecutionReceipt` (`run_worker`) | [CodeMender Runner REQ-0006](../tech/systems/codemender-runner/spec.md#req-0006), [Distributed Execution & Transit REQ-0004](../tech/systems/distributed-execution-and-transit/spec.md#req-0004), ADR-0002 | `Launcher.run_worker` & `_synthesize_fallback_receipt` |
| **REQ-0008** | Concrete Local Backend (`LocalDockerLauncher`) | [Security & Trust Zones Dec 3](../tech/systems/security-and-trust-zones/design.md#decision-3-mode-switched-least-privilege-gcp-wif-local-single-sa-vs-cloud_run-coordinatorworker-dual-sa-split), [CodeMender Runner REQ-0006](../tech/systems/codemender-runner/spec.md#req-0006), ADR-0003, SPEC-WORKER | `launcher.local.LocalDockerLauncher` |
| **REQ-0009** | Concrete Cloud Backend (`CloudRunLauncher`) | [Security & Trust Zones Dec 3](../tech/systems/security-and-trust-zones/design.md#decision-3-mode-switched-least-privilege-gcp-wif-local-single-sa-vs-cloud_run-coordinatorworker-dual-sa-split), [CodeMender Runner REQ-0002](../tech/systems/codemender-runner/spec.md#req-0002), [REQ-0006](../tech/systems/codemender-runner/spec.md#req-0006), SPEC-ARTIFACT-CACHE | `launcher.cloudrun.CloudRunLauncher` |
| **REQ-0010** | Context Management & Lifecycle (`stop`) | [CodeMender Runner Design](../tech/systems/codemender-runner/design.md), ADR-0002 | `Launcher.__enter__`, `__exit__`, `stop` |
