<!--
sdd_spec_id: SPEC-ARTIFACT-CACHE
sdd_implements: None
sdd_extends: None
sdd_level: COMPONENT
sdd_archetype: LIBRARY
sdd_cujspec_product: codemender-runner
-->

<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-10-22' }
*-->

# SPEC-ARTIFACT-CACHE: `ArtifactCache` Implementation Using `fsspec` (`launcher.cache`)

**Implements Spec:** None

**Extends Spec:** None

**Governing Journeys:**
- **Pull Request Contributor (`persona-pr-author`)**: As a developer waiting on
  automated security scans, patch synthesis, and exploit verification, I need
  `Launcher` and `Worker` (`cm-worker`) to exchange repository snapshots, task
  manifests, single-finding SARIF inputs, diffs, logs, and execution receipts
  atomically and deterministically across local Docker/gVisor (`file://`),
  Google Cloud Run Jobs (`gs://`), and GKE (`gs://`) without partial reads,
  stale caches, or corrupted line endings.
- **Security & Compliance Lead (`persona-security-auditor`)**: As a security
  lead governing untrusted code execution in
  [SPEC-0003](../runner-container-packaging/spec.md) and
  [SPEC-WORKER (PR #73)](https://github.com/cloud-gtm/codemender-runner/pull/73), I need `ArtifactCache` to strictly confine
  all cache keys within `<cache_root>`, block path-traversal (`..`) and symlink
  escapes, enforce the Two-Way Door lazy-import invariant so local `file://`
  runs never import cloud SDKs, and automatically expire short-lived GCS transit
  objects under least-privilege IAM.

## Overview

This specification defines the normative functional requirements, atomic
low-level storage primitives, typed client–worker protocol methods
(`vcs-cloudrun-comms`), shared wire schemas (`RunManifest`,
`ExecutionReceipt`), `<cache_root>` URI and branch-sanitization helpers
(`sanitize_branch_name`, `build_gcs_cache_uri`), path-traversal and symlink
security invariants, Two-Way Door lazy-import boundary, typed exception
hierarchy, and GCS transit bucket lifecycle/IAM configuration for
`packages/launcher/src/launcher/cache/` (`launcher.cache`).

As established in
the [CodeMender Runner System Design](../tech/systems/codemender-runner/design.md)
(Decision 2 and Runtime Contracts) and
[SPEC-WORKER (PR #73)](https://github.com/cloud-gtm/codemender-runner/pull/73), the outer
host `Launcher` client (`LocalDockerLauncher` / `CloudRunLauncher`) and the
inside-container `Worker` server (`cm-worker`) communicate exclusively through
`ArtifactCache` in an acyclic hub-and-spoke dependency hierarchy
(`Launcher --> ArtifactCache <-- Worker`). Implemented over
[`fsspec`](https://filesystem-spec.readthedocs.io/) (`filesystem-spec` +
`gcsfs`), `ArtifactCache` unifies `LocalArtifactCache` (`file://`) and
`GcsArtifactCache` (`gs://`) behind a single root-scoped protocol interface
where all read and write operations are atomic. This file/object-based protocol
enables a single universal worker container image (`docker/runner.Dockerfile`
with `ENTRYPOINT ["cm-worker"]`) to run identically across local Docker/gVisor
(`file://`), Google Cloud Run Jobs (`gs://`), and Google Kubernetes Engine / GKE
(`gs://`), while standalone local runs outside CI/CD can still invoke
`cm-runner` directly by overriding `--entrypoint cm-runner`.

This specification is governed by and references:

- [design.md — `ArtifactCache` Implementation Using `fsspec`](design.md)
- [SPEC-WORKER: Sandbox Container Worker (`launcher.worker` / `cm-worker`) — PR #73](https://github.com/cloud-gtm/codemender-runner/pull/73)
- [SPEC-FIND-DIFF: Differential Scan (`cm-runner find-diff`)](../find-diff/spec.md)
- [SPEC-FIX: Patch Synthesis (`cm-runner fix`)](../fix/spec.md)
- [SPEC-VERIFY: Finding Verification (`cm-runner verify`)](../verify/spec.md)
- [SPEC-GHA: GitHub Actions Differential Scan Workflow](../github-actions/spec.md)
- [SPEC-GHA-FIX: GitHub Actions Interactive `/codemender` Remediation](../github-actions-fix/spec.md)
- [SPEC-GITLAB-CI: GitLab CI Integration](../gitlab-ci/spec.md)
- [SPEC-0003: Lean Runner Base Container & Modular BYOI Packaging](../runner-container-packaging/spec.md)
- [SPEC-WIF: Unified Terraform Root & Composable Mode-Aware WIF Module](../wif/spec.md)
- [Proposal: VCS to Cloud Run Communication Architecture (`vcs-cloudrun-comms`)](../../proposals/vcs-cloudrun-comms/proposal.md)
- [Proposal: Cloud Run Jobs Container Bootstrapper (`cloudrun-job-bootstrapper`)](../../proposals/cloudrun-job-bootstrapper/proposal.md)
- [ADR-0002: Machine-Readable Output Contract](../../../adr/ADR-0002.md)
- [Distributed Execution and Transit System Specification](../tech/systems/distributed-execution-and-transit/spec.md)
- [Distributed Execution and Transit System Design](../tech/systems/distributed-execution-and-transit/design.md)
- [ADR-0008: Repository Layout, Three-Package Workspace, and Runtime Architecture](../../../adr/ADR-0008-repository-layout-and-package-structure.md)
- [CodeMender Runner System Design](../tech/systems/codemender-runner/design.md)

## Requirements

### REQ-0001: Atomic Low-Level Storage Primitives & `fsspec` Backend Contract {#req-0001}

- **CUJ UUID:** `cuj-artifact-cache-contract`

`launcher.cache` MUST export `ArtifactCache`, `LocalArtifactCache`,
`GcsArtifactCache`, and `open_artifact_cache(uri)` providing root-scoped,
atomic read and write primitives backed by `fsspec`:

1. **`put_bytes(key: str, data: bytes) -> None` (Atomic Byte Write):**
   - MUST validate `key` and atomically write `data` to the resolved location
     under `<cache_root>` so concurrent readers or interrupted writes never
     expose a partial or truncated artifact, and no temporary staging files
     remain if the write fails.
2. **`get_bytes(key: str) -> bytes` (Single-Operation Atomic Byte Read):**
   - MUST validate `key` and read the complete byte payload of `key` in a single
     atomic read operation without a separate pre-flight existence check that
     could introduce Time-Of-Check to Time-Of-Use (TOCTOU) races.
   - MUST raise `ArtifactNotFoundError` if `key` does not exist or refers to a
     directory or virtual prefix.
3. **`put_file(key: str, src: Path | str) -> None` (Atomic File Upload):**
   - MUST validate `key` and verify that `src` exists on the local filesystem
     as a regular file, raising `ArtifactNotFoundError` if `src` does not exist
     or is a directory.
   - MUST atomically publish `src` to `key` under `<cache_root>` as raw bytes
     (preserving exact byte content including `\r\n` and binary sequences) so
     no partial file is visible during or after an interrupted transfer, any
     prior cached artifact at `key` remains intact on error, and no temporary
     staging files remain on error.
4. **`get_file(key: str, dest: Path | str) -> Path` (Atomic Local File Materialization):**
   - MUST validate `key` and verify that `dest` is not an existing directory on
     the local filesystem (raising `ArtifactCacheError` if `dest` is an existing
     directory).
   - MUST create any missing parent directories for `dest`, atomically
     materialize `key` to `dest` so an interrupted or failed download never
     leaves a truncated file at `dest` or leftover temporary staging files, and
     return `Path(dest)`.
   - MUST raise `ArtifactNotFoundError` if `key` does not exist or refers to a
     directory or virtual prefix.
5. **`put_json(key: str, data: dict[str, Any]) -> None` &
   `get_json(key: str) -> dict[str, Any]` (Atomic JSON Object Read/Write):**
   - `put_json` MUST verify that `data` is a mapping, serialize it
     deterministically as UTF-8 JSON with sorted keys and a trailing newline,
     and atomically write the payload via `put_bytes`, raising
     `ArtifactIntegrityError` if `data` is not a mapping or is not
     JSON-serializable.
   - `get_json` MUST atomically read bytes via `get_bytes`, decode as UTF-8,
     parse the JSON document, and verify that the root JSON value is a `dict`,
     raising `ArtifactIntegrityError` if UTF-8 decoding fails, the JSON is
     malformed or truncated, or the root JSON value is not a `dict`.

#### Scenario: Atomic Round-Trip and Directory-Prefix Rejection Across Backends

- **GIVEN** a `LocalArtifactCache` or `GcsArtifactCache` instance bound to a
  `<cache_root>`
- **WHEN** `cache.put_json("tasks/find/input/manifest.json", {"task_id": "find"})`
  is called
- **THEN** `cache.get_json("tasks/find/input/manifest.json")` returns
  `{"task_id": "find"}`, while `cache.get_bytes("tasks/find")` raises
  `ArtifactNotFoundError`.

#### Scenario: Failed Write or Download Preserves Prior File and Leaves Zero Temporary Files

- **GIVEN** a cache containing `"shared/code.tar.gz"` with content `b"original"`
  and an existing local destination file `dest_file` with content `b"original"`
- **WHEN** a subsequent `put_file` or `get_file` fails mid-transfer due to an
  underlying storage error
- **THEN** the prior cached artifact and `dest_file` still contain
  `b"original"` and no temporary staging files remain in the cache or
  `dest_file.parent`.

#### Scenario: Missing or Directory Key/Path in `get_file` and `put_file`

- **GIVEN** a cache instance where `"tasks/find/input/manifest.json"` is stored
- **WHEN** `cache.get_file("tasks/find", dest_file)` or
  `cache.get_file("missing.txt", dest_file)` or
  `cache.put_file("shared/code.tar.gz", directory_path)` is called
- **THEN** each call raises `ArtifactNotFoundError`.

---

### REQ-0002: Key Normalization, `<cache_root>` URI Helpers, Branch Sanitization & Path-Traversal Security {#req-0002}

- **CUJ UUID:** `cuj-artifact-cache-security`

`ArtifactCache` MUST enforce a strict boundary between host filesystem paths,
multi-tenant `<cache_root>` URIs, and logical cache keys:

1. **`<cache_root>` URI Helpers (`open_artifact_cache`, `sanitize_branch_name`, `build_gcs_cache_uri`):**
   - `open_artifact_cache(uri)` MUST accept `pathlib.Path` objects and `file://`
     URIs (`file:///path/to/cache`) to construct a `LocalArtifactCache`, and
     `gs://<bucket>[/<prefix>]` URIs to construct a `GcsArtifactCache`. Empty
     URIs, bare string paths without a scheme, or unsupported URI schemes MUST
     raise `InvalidCacheKeyError`.
   - `sanitize_branch_name(branch: str) -> str` MUST strip an optional leading
     `refs/heads/` prefix, replace characters outside `[a-zA-Z0-9_.-]` with `-`,
     and return `f"{slug[:40]}-{sha256(raw_branch.encode('utf-8')).hexdigest()[:8]}"`
     ([Proposal: `vcs-cloudrun-comms`](../../proposals/vcs-cloudrun-comms/proposal.md)),
     raising `InvalidCacheKeyError` if `branch` is empty.
   - `build_gcs_cache_uri(bucket, owner, repo, commit_sha, *, pr_number=None, branch=None) -> str`
     MUST validate `bucket`, `owner`, `repo`, and `commit_sha` as single safe
     segments, require exactly one of `pr_number` ($\ge 1$) or `branch`, and
     return `gs://<bucket>/<owner>/<repo>/prs/<pr_number>/<commit_sha>` or
     `gs://<bucket>/<owner>/<repo>/branches/<sanitize_branch_name(branch)>/<commit_sha>`.
2. **Logical Cache Key & Single-Segment Identifier Validation:**
   - Logical cache keys MUST normalize Windows backslashes (`\`) to POSIX
     forward slashes (`/`).
   - Logical cache keys MUST be non-empty relative strings and MUST NOT start
     or end with `/`, contain empty segments (`//`), traversal segments (`.` or
     `..`), null bytes (`\x00`), Windows drive prefixes (e.g., `C:`), or
     reserved temporary staging prefixes (`.tmp.`). Any violation MUST raise
     `InvalidCacheKeyError`.
   - Domain identifiers (`task_id`, `finding_id`, `filename`) passed to typed
     protocol methods MUST each be a single non-empty path segment containing no
     `/` or `\` separators, `.`, `..`, null bytes, or `.tmp.` prefix; any
     violation MUST raise `InvalidCacheKeyError`.
3. **Local Root Canonicalization & In-Cache Symlink Rejection:**
   - `LocalArtifactCache` MUST canonicalize its `root_dir` once at
     initialization (permitting OS-level symbolic links in parent directories
     *above* `root_dir`, such as macOS `/tmp` $\rightarrow$ `/private/tmp`),
     and MUST reject any symbolic link segment *inside* `root_dir` along the
     resolved cache path or any path that resolves outside `root_dir`, raising
     `InvalidCacheKeyError`.

#### Scenario: Path Traversal and Symlink Escape Rejection

- **GIVEN** a `LocalArtifactCache` whose root directory contains a symbolic link
  `"escape"` pointing to `/etc`
- **WHEN** a caller invokes `cache.get_bytes("../secret")`,
  `cache.get_bytes("/etc/passwd")`, `cache.get_bytes("C:/Windows/win.ini")`,
  `cache.get_bytes(".tmp.partial")`, `cache.get_bytes("escape/passwd")`,
  `cache.get_manifest("../escape")`, or
  `cache.put_task_output("find", "../escape.diff", src_file)`
- **THEN** each call raises `InvalidCacheKeyError`.

---

### REQ-0003: Two-Way Door Lazy-Import Isolation & `GcsArtifactCache` Consistency {#req-0003}

- **CUJ UUID:** `cuj-artifact-cache-lazy-import`

`packages/launcher` MUST isolate Google Cloud dependencies so local `file://`
executions never import cloud storage SDKs:

1. **Dependency Packaging (`packages/launcher/pyproject.toml`):**
   - Base `dependencies` MUST include `"codemender"` and `"fsspec>=2024.2.0"`
     with zero cloud SDK dependencies.
   - `[project.optional-dependencies] cloudrun` MUST include
     `"gcsfs>=2024.2.0"`, `"google-cloud-run>=0.10.0"`, and (until legacy
     callers are migrated) `"google-cloud-storage>=2.14.0"`.
2. **Lazy Import in `open_artifact_cache(uri)`:**
   - Importing `launcher.cache` and opening or operating on a `file://` or
     local-path `ArtifactCache` MUST NOT import `gcsfs` or `google.cloud`.
   - When `uri` uses the `gs://` scheme, `open_artifact_cache(uri)` MUST lazily
     load the GCS backend and raise `CacheConfigurationError` with an actionable
     `pip install 'codemender-launcher[cloudrun]'` message if `gcsfs` is not
     installed.
3. **Cross-Process GCS Read-After-Write Consistency & Retries:**
   - `GcsArtifactCache` MUST disable local directory metadata caching so
     `Launcher` immediately observes objects written by `Worker` containers
     without stale cache misses, and MUST automatically retry transient HTTP
     `429`/`5xx` responses (see [design.md Section 3.3](design.md#33-atomic-reading-writing-and-path-resolution-design)).

#### Scenario: Local Cache Operations Succeed Without Cloud Storage Packages Installed

- **GIVEN** optional cloud storage packages (`gcsfs` and `google.cloud`) are
  unavailable in the Python environment
- **WHEN** `open_artifact_cache("file:///tmp/cache")` is used to write and read
  artifacts
- **THEN** all local cache operations succeed without importing `gcsfs` or
  `google.cloud`, whereas `open_artifact_cache("gs://bucket/prefix")` raises
  `CacheConfigurationError`.

---

### REQ-0004: Typed Exception Hierarchy {#req-0004}

- **CUJ UUID:** `cuj-artifact-cache-errors`

`launcher.cache` (`launcher.cache.errors`) MUST translate backend-specific
filesystem, POSIX, and cloud storage errors into a single typed hierarchy
rooted at `ArtifactCacheError` (carrying `op`, `uri`, and `key` context; see
[design.md Section 5.3](design.md#53-artifactcacheerror-hierarchy--exit-code-mapping-launchercacheerrors)
and [SPEC-WORKER (PR #73)](https://github.com/cloud-gtm/codemender-runner/pull/73)
for `Worker` and `Launcher` process exit-code mapping):

1. **`InvalidCacheKeyError(ArtifactCacheError, ValueError)`:** Raised on invalid
   keys, multi-segment `task_id`/`finding_id`/`filename` arguments, caller
   `task_id` argument mismatch against `manifest.task_id` in `put_manifest` or
   `receipt.task_id` in `put_receipt`, unsupported URI schemes, or symlink
   escapes inside `root_dir`.
2. **`ArtifactNotFoundError(ArtifactCacheError, FileNotFoundError)`:** Raised
   when a requested cache key or local source file (`put_file`,
   `put_repo_archive`, `put_finding`, `put_task_output`) does not exist or is a
   directory.
3. **`ArtifactIntegrityError(ArtifactCacheError, ValueError)`:** Raised when
   `get_json` encounters invalid UTF-8, malformed JSON, or a non-`dict` root,
   `put_json` receives a non-mapping or unserializable value, or `get_manifest`
   / `get_receipt` encounters a payload that fails wire schema validation or
   whose stored `task_id` does not match the requested `task_id`.
4. **`CacheConfigurationError(ArtifactCacheError)`:** Raised when `gcsfs` is
   missing for a `gs://` URI or cloud credentials cannot be loaded.
5. **`CachePermissionError(ArtifactCacheError, PermissionError)`:** Raised on
   cloud HTTP `401`/`403` or POSIX `EACCES`/`EPERM`.
6. **`CacheTransientError(ArtifactCacheError, OSError)`:** Raised on cloud HTTP
   `408`/`429`/`500`/`502`/`503`/`504`, `TimeoutError`, `ConnectionError`
   (including `aiohttp` connection errors and `google.auth` `TransportError`)
   after retries, or other OS-level I/O failures such as local `ENOSPC`.
7. **Base `ArtifactCacheError`:** Storage conflicts (`FileExistsError`, HTTP
   `409`/`412`) and unclassified backend exceptions MUST surface as the base
   `ArtifactCacheError` (never `CacheTransientError`) so callers do not retry
   deterministic failures. Translation MUST prefer structured signals
   (`errno`, HTTP status attributes) over message text.

#### Scenario: Translating Backend Permission and Transient Failures

- **GIVEN** an `ArtifactCache` whose underlying storage backend raises an HTTP
  `403` permission failure on write and an HTTP `503` service unavailable error
  on read
- **WHEN** `cache.put_bytes("a.txt", b"x")` and `cache.get_bytes("a.txt")` are
  called
- **THEN** `put_bytes` raises `CachePermissionError` and `get_bytes` raises
  `CacheTransientError`, each populated with `op`, `uri`, and `key`.

---

### REQ-0005: GCS Transit Bucket Lifecycle (`google_storage_bucket.reports`) & Scoped IAM {#req-0005}

- **CUJ UUID:** `cuj-artifact-cache-terraform`

`terraform/modules/gcp` MUST configure `google_storage_bucket.reports` (wired
into `module.wif` via [SPEC-WIF](../wif/spec.md)) for short-lived
`GcsArtifactCache` transit hygiene and least-privilege access across `Launcher`
and `Worker`:

1. **Configurable TTL (`var.reports_bucket_ttl_days`):**
   - `terraform/modules/gcp` MUST define `var.reports_bucket_ttl_days`
     (`number`, `default = 7`, validated as an integer $\ge 1$).
   - `google_storage_bucket.reports` MUST configure a `Delete` lifecycle rule
     with `condition { age = var.reports_bucket_ttl_days }` and an
     `AbortIncompleteMultipartUpload` lifecycle rule with `condition { age = 1 }`.
2. **Immediate Expiration Without Noncurrent/Soft-Delete Retention:**
   - `google_storage_bucket.reports` MUST enforce
     `uniform_bucket_level_access = true`,
     `public_access_prevention = "enforced"`, `versioning { enabled = false }`,
     and `soft_delete_policy { retention_duration_seconds = 0 }`.
3. **Scoped Bucket IAM Across `Launcher` (WIF SA) and `Worker` (Cloud Run Runtime SA):**
   - Host-side `Launcher` authenticates via the WIF caller Service Account in
     `terraform/modules/wif` (`google_service_account.runner_sa`), which
     receives `roles/storage.objectUser` on `google_storage_bucket.reports` via
     `google_storage_bucket_iam_member.transit_bucket_access`
     ([SPEC-WIF](../wif/spec.md) `REQ-0004`).
   - Inside-container `Worker` (`cm-worker`) runs under the Cloud Run runtime
     Service Account in `terraform/modules/gcp`
     (`google_service_account.runner_sa`), which MUST be granted
     `roles/storage.objectUser` on `google_storage_bucket.reports`.

#### Scenario: Default and Custom TTL Plan Verification in Terraform Tests

- **GIVEN** `terraform/modules/gcp` is planned with default variables and with
  `reports_bucket_ttl_days = 14`
- **WHEN** `terraform test` validates the module plan
- **THEN** `google_storage_bucket.reports` enforces disabled versioning,
  zero-second soft-delete retention, enforced public access prevention, a
  `Delete` lifecycle rule with `age == 7` by default (and `age == 14` when
  overridden), a 1-day `AbortIncompleteMultipartUpload` rule, and a
  `roles/storage.objectUser` binding for
  `google_service_account.runner_sa`.

---

### REQ-0006: Typed `vcs-cloudrun-comms` Wire Schemas & Protocol Methods for `Worker` and `Launcher` {#req-0006}

- **CUJ UUID:** `cuj-artifact-cache-typed-protocol`

`launcher.cache` MUST export the shared wire schemas (`RunManifest`,
`ExecutionReceipt`) and expose all typed protocol methods required by `Worker`
([SPEC-WORKER / PR #73](https://github.com/cloud-gtm/codemender-runner/pull/73))
and `Launcher` on `ArtifactCache` so neither peer hardcodes internal cache paths
or imports the other (`Launcher --> ArtifactCache <-- Worker`):

1. **Wire Schemas (`RunManifest`, `ExecutionReceipt`):**
   - **`RunManifest` ([design.md Section 5.1](design.md#51-runmanifest-taskstask_idinputmanifestjson)):**
     Immutable value object (`command`, `task_id`, `base_ref`, `finding_key`,
     `diff_workers`, `log_level`, `extra_args`) validating `command` in
     `{"init", "find-diff", "fix", "verify"}`, requiring `base_ref` when
     `command == "find-diff"`, requiring a canonical `findings/<id>.sarif`
     `finding_key` when `command in {"fix", "verify"}`, and validating
     `diff_workers` in `1..16` ([SPEC-FIND-DIFF `REQ-0002`](../find-diff/spec.md)).
   - **`ExecutionReceipt` ([design.md Section 5.2](design.md#52-executionreceipt-taskstask_idoutputreceiptjson)):**
     Immutable value object (`status`, `exit_code`, `command`, `task_id`,
     `started_at`, `completed_at`, `duration_seconds`, `finding_count`,
     `artifacts`, `error`) validating `status` in `{"SUCCESS", "FAILURE"}` and
     `command` as a non-empty string (canonical commands `"init"`,
     `"find-diff"`, `"fix"`, `"verify"`, or `"unknown"` when `Worker` writes a
     failure receipt after a manifest load error), preserving `error=None` on
     `exit_code == 0` and normal `cm-runner` exit `3`
     ([SPEC-FIX `REQ-0007`](../fix/spec.md),
     [SPEC-GHA-FIX `REQ-0005.5`](../github-actions-fix/spec.md)), and
     preserving non-dismissed `finding_count` ([SPEC-VERIFY `REQ-0005`](../verify/spec.md)).
2. **Typed Protocol Methods on `ArtifactCache`:**
   - **`get_manifest(task_id: str) -> RunManifest` &
     `put_manifest(task_id: str, manifest: RunManifest) -> None`:** Atomically
     read and write `RunManifest` at `"tasks/<task_id>/input/manifest.json"`,
     validating `task_id` as a single safe segment, raising
     `InvalidCacheKeyError` if `put_manifest` receives
     `manifest.task_id != task_id`, and translating schema or stored `task_id`
     mismatch errors in `get_manifest` into `ArtifactIntegrityError`.
   - **`get_repo_archive(dest: Path) -> Path` &
     `put_repo_archive(src: Path) -> None`:** Atomically download and upload the
     bounded-history repository archive ([SPEC-FIND-DIFF `REQ-0002.2`](../find-diff/spec.md))
     at `"shared/code.tar.gz"` via `get_file` / `put_file`.
   - **`get_finding(finding_key: str, dest: Path) -> Path` &
     `put_finding(finding_id: str, src: Path) -> str`:** Atomically download and
     upload single-finding SARIF 2.1.0 files at `"findings/<finding_id>.sarif"`,
     validating `finding_id` as a single safe segment (`put_finding` returns
     `"findings/<finding_id>.sarif"`; `get_finding` accepts either
     `"findings/<id>.sarif"` or bare `"<id>"` and rejects keys outside
     `"findings/"` or not ending in `".sarif"` with `InvalidCacheKeyError`).
   - **`get_task_output(task_id: str, filename: str, dest: Path) -> Path` &
     `put_task_output(task_id: str, filename: str, src: Path) -> str`:**
     Atomically download and upload raw-byte task output files (`runner.log`,
     `report.sarif`, `report.md`, `<finding>.diff`, `<finding>.sarif`) at
     `"tasks/<task_id>/output/<filename>"`, validating both `task_id` and
     `filename` as single safe segments (`put_task_output` returns
     `"tasks/<task_id>/output/<filename>"`).
   - **`get_receipt(task_id: str) -> ExecutionReceipt` &
     `put_receipt(task_id: str, receipt: ExecutionReceipt) -> None`:**
     Atomically read and write `ExecutionReceipt` at
     `"tasks/<task_id>/output/receipt.json"`, validating `task_id` as a single
     safe segment, raising `InvalidCacheKeyError` if `put_receipt` receives
     `receipt.task_id != task_id`, and translating schema or stored `task_id`
     mismatch errors in `get_receipt` into `ArtifactIntegrityError`.

#### Scenario: End-to-End Typed Protocol Flow Between `Launcher` and `Worker`

- **GIVEN** a `LocalArtifactCache` or `GcsArtifactCache` instance
- **WHEN** `Launcher` calls `cache.put_repo_archive(tar_src)`,
  `cache.put_finding("finding-001", sarif_src)`, and
  `cache.put_manifest("fix-finding-001", RunManifest(command="fix", task_id="fix-finding-001", finding_key="findings/finding-001.sarif"))`,
  and `Worker` calls `cache.get_manifest("fix-finding-001")`,
  `cache.get_repo_archive(work_dir / "inputs" / "code.tar.gz")`,
  `cache.get_finding(manifest.finding_key, work_dir / "inputs" / "finding-001.sarif")`,
  `cache.put_task_output("fix-finding-001", "finding-001.diff", diff_src)`,
  `cache.put_task_output("fix-finding-001", "runner.log", log_src)`, and
  `cache.put_receipt("fix-finding-001", receipt)`
- **THEN** `cache.get_receipt("fix-finding-001")` returns the matching
  `ExecutionReceipt` and
  `cache.get_task_output("fix-finding-001", "finding-001.diff", out_diff)`
  atomically materializes the exact raw diff bytes without either caller
  hardcoding `"shared/"`, `"findings/"`, or `"tasks/"` paths.
