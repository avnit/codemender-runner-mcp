<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-10-14' }
*-->

<!--
sdd_spec_id: SPEC-GHA
sdd_implements: None
sdd_extends: None
sdd_level: COMPONENT
sdd_archetype: CI_CD_WORKFLOW
sdd_cujspec_product: codemender-runner
-->

# SPEC-GHA: GitHub Actions Orchestration & Execution Backends

**Implements Spec:** None

**Extends Spec:** None

**Governing Journeys:**
- **Pull Request Contributor (`persona-pr-author`)**: As a developer submitting pull requests, I want fast, automated security feedback directly on my PR comments without needing local GCP credentials or waiting for unneeded container image downloads.
- **Security & Compliance Lead (`persona-security-auditor`)**: As an enterprise security auditor, I mandate that code analysis and verification occur in isolated microVM sandboxes (Cloud Run Jobs) with strict perimeter isolation, zero token exfiltration risk, and complete host credential segregation.

## Overview

This specification defines the functional requirements, configuration interface, launcher abstraction, and execution routing for the CodeMender GitHub Actions integration (`github-actions/`).

The integration allows repositories to run CodeMender during pull request workflows. It supports dual execution backends:
1. **Local Docker Mode (`local`)**: Runs `cm-runner` natively on the GitHub Actions runner VM inside a throwaway Docker container with zero external cloud dependencies.
2. **Cloud Run Mode (`cloudrun`)**: Delegates command execution to Google Cloud Run microVMs via in-process integration with the `gcp_sandboxing.client.CloudRunClient` Python SDK (governed by [ADR-0007](../../../adr/ADR-0007-cloud-run-job-invocation-interface-cli-vs-python-library.md) Option 3). All Cloud Run infrastructure configuration details (GCP project, job name, region, transit storage) are resolved from ambient environment variables and authenticated via Workload Identity Federation (WIF).

Both backends adhere to an identical, symmetric execution interface: CLI arguments, NDJSON stream contracts, and file-based workspace inputs and outputs (e.g. `output/receipt.json`, diff patches, reports) are placed in identical workspace locations. This minimizes operational differences and ensures downstream action steps operate transparently without backend branching.

This specification is governed by:
- [ADR-0001: Machine-Readable Output Contract](../../../adr/ADR-0001-machine-readable-output-contract.md) (NDJSON events on `stdout`, logs on `stderr`, coarse exit codes)
- [ADR-0003: Environment Variable Scrubbing](../../../adr/ADR-0003-environment-scrubbing.md)
- [ADR-0005: Multi-Platform Transit Storage & Sandboxed Isolation Model](../../../adr/ADR-0005-multi-platform-transit-storage-and-isolation.md)
- [ADR-0007: Cloud Run Job Invocation Interface: CLI vs. Python Library](../../../adr/ADR-0007-cloud-run-job-invocation-interface-cli-vs-python-library.md)
- [ADR-0008: Repository Layout, Three-Package Workspace, and Runtime Architecture](../../../adr/ADR-0008-repository-layout-and-package-structure.md) (Host token boundary; credentials never enter the container)
- [Proposal: gha-cloudrun-launcher](../../proposals/gha-cloudrun-launcher/proposal.md)
- [Proposal: Cloud Run Jobs Container Bootstrapper & Execution Harness](../../proposals/cloudrun-job-bootstrapper/proposal.md)

---

## Requirements

### REQ-0001: Simplified Dual Backend Action Inputs {#req-0001}

- **CUJ UUID:** cuj-gha-backend-config

The GitHub Action metadata (`github-actions/actions/codemender/action.yml`) MUST support configurable execution backends through a unified input schema:

| Input | Type | Required | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `backend` | string | No | `'local'` | Target execution backend: `'local'` (Docker on runner VM) or `'cloudrun'` (Cloud Run via in-process CloudRunClient SDK). |
| `runner_image` | string | Conditional | `''` | Container image tag. Required when `backend == 'local'`; optional when `backend == 'cloudrun'`. |
| `registry_token` | string | No | `''` | Token to authenticate to container registry for `docker pull`. Used only when `backend == 'local'`. |
| `log_level` | string | No | `'info'` | Log verbosity level passed to the orchestrator. |
| `gcp_project_id` | string | Yes | — | Google Cloud project ID used by CodeMender (`GOOGLE_CLOUD_PROJECT`). |
| `gcp_workload_identity_provider` | string | Yes | — | Full GCP Workload Identity Provider resource name for WIF authentication. |
| `gcp_service_account` | string | Yes | — | Google Cloud service account email for Workload Identity Federation. |
| `gcs_transit_bucket` | string | No | `''` | GCS transit storage bucket. Defaults to `codemender-reports-<project>`. |
| `cloud_run_job_name` | string | No | `'codemender-runner'` | Target Cloud Run Job name in Cloud Run mode. |
| `cloud_run_region` | string | No | `'us-central1'` | Target GCP region for the Cloud Run Job. |

Cloud-specific execution parameters MAY be passed explicitly via action inputs or resolved from ambient environment variables on the runner. When inputs are omitted, the action and CloudRunClient SDK MUST resolve defaults automatically from ambient environment variables and ADC credentials.

#### Scenario: Defaulting to local Docker backend
- **GIVEN** a workflow step invokes `.github/actions/codemender` without specifying `backend`
- **WHEN** the action executes
- **THEN** `backend` MUST default to `'local'`
- **AND** `runner_image` MUST be validated as present and non-empty.

#### Scenario: Missing runner_image in local mode
- **GIVEN** a workflow step specifies `backend: 'local'` (or omits `backend`)
- **WHEN** `runner_image` is empty or not provided
- **THEN** the action MUST fail with exit code `1` and an error message requiring `runner_image`.

#### Scenario: Successful validation with Cloud Run backend
- **GIVEN** a workflow step specifies `backend: 'cloudrun'`
- **WHEN** `runner_image` is empty or omitted
- **THEN** input validation MUST succeed without requiring `runner_image` or external client input parameters.

---

### REQ-0002: Conditional Docker Pull Bypass {#req-0002}

- **CUJ UUID:** cuj-gha-skip-docker-pull

To prevent unnecessary bandwidth usage, image download latency, and runner disk consumption:

1. The composite step pulling the container image (`docker pull "$IMAGE"`) and any associated Docker registry login/logout commands MUST execute **ONLY IF** `inputs.backend == 'local'`.
2. When `inputs.backend == 'cloudrun'`, the action MUST bypass all Docker CLI commands on the host runner.

#### Scenario: Bypassing Docker pull in Cloud Run mode
- **GIVEN** `inputs.backend` is set to `'cloudrun'`
- **WHEN** the action executes
- **THEN** the `Pull cm-runner image` step MUST be skipped
- **AND** Docker MUST NOT attempt to authenticate or pull any image.

#### Scenario: Executing Docker pull in local mode
- **GIVEN** `inputs.backend` is `'local'` (or omitted) and `inputs.runner_image` is specified
- **WHEN** the action executes
- **THEN** the `Pull cm-runner image` step MUST execute
- **AND** `docker pull "$IMAGE"` MUST be invoked.

---

### REQ-0003: CloudRunLauncher In-Process SDK Integration & Execution Contract {#req-0003}

- **CUJ UUID:** cuj-cloudrun-launcher-contract

The orchestrator MUST provide a launcher component (`CloudRunLauncher`) that realizes the abstract launcher interface (`Launcher`) to dispatch commands to Cloud Run via direct in-process Python SDK integration:

1. The launcher MUST accept:
   - `client`: An object implementing the `CloudRunClient` interface (or a factory/callable producing it), enabling direct in-process invocation and dependency injection of mock clients during hermetic unit testing.
   - `log_level`: Log verbosity string (default `'info'`).
   - `timeout_seconds`: Hard execution ceiling (default 3600 seconds).

2. When executing a command (`run(command, args)`):
   - The launcher MUST invoke the client's execution engine in-process (e.g. `client.run(...)`).
   - Operational logs and heartbeat progress updates emitted by the remote job or client MUST stream live to `sys.stderr` so that remote progress is immediately visible in the GitHub Actions runner console.
   - The launcher MUST map the returned `ExecutionReceipt` directly into a `RunResult` containing:
     - `exit_code`: The integer exit code reported by `receipt.exit_code` (or `0` when `receipt.status == "SUCCESS"`).
     - `events`: The list of structured event dictionaries parsed from `receipt.event` or `receipt.events` conforming to ADR-0001.
   - The launcher MUST NOT invoke external CLI subprocess wrappers or parse stdout JSON text in Python execution environments.

3. Error handling:
   - If the client raises a timeout exception (e.g. `CloudRunTimeoutError`) or execution exceeds `timeout_seconds`, the launcher MUST raise a `LauncherError` indicating an execution timeout.
   - If the client raises an infrastructure or execution error (e.g. `CloudRunError`), the launcher MUST raise a `LauncherError` containing the underlying failure diagnostic.
   - If the execution completes with a non-zero exit code (`receipt.exit_code != 0`), the launcher MUST return a `RunResult` with `exit_code == receipt.exit_code` and `ok == False`.

4. Structured Receipt Ingestion & Artifact Discovery Contract:
   - The launcher MUST support the structured execution receipt contract (`receipt.json`) produced by the Cloud Run Job bootstrapper (`cloud-run-jobs/entrypoint.py`).
   - The launcher MUST parse receipt attributes or dictionary fields:
     - `status`: Execution status string (`"SUCCESS"` or `"FAILURE"`).
     - `exit_code`: Integer exit code reported by the inner CodeMender command execution.
     - `command`: Dispatched command string.
     - `started_at` & `completed_at`: ISO 8601 UTC execution timestamps.
     - `finding_count`: Non-negative integer count of security findings discovered.
     - `artifacts`: Dictionary mapping output artifact categories (`sarif`, `log`, `report`, `patch`) to relative file paths or storage URIs.
     - `error`: Diagnostic error string, or `None`.
   - When `receipt.stdout` is absent or empty, the launcher MUST perform artifact discovery:
     - Inspect `receipt.artifacts` for `"sarif"` paths/URIs.
     - Inspect standard candidate locations including `CODEMENDER_OUT_DIR`, `output/report.sarif`, and `report.sarif`.
     - Read the staged artifact bytes and populate `RunResult.stdout`.
   - This ensures downstream consumers (such as `_parse_scan_findings` in `gha_runner.py`) receive valid SARIF bytes identically across both local Docker and Cloud Run backends.

#### Scenario: Successful execution via CloudRunLauncher
- **GIVEN** a configured `CloudRunLauncher` with a valid `CloudRunClient`
- **WHEN** `launcher.run("version")` is called
- **THEN** `client.run` MUST be invoked with `command="version"`
- **AND** the returned receipt events MUST be populated in `RunResult.events`
- **AND** `RunResult.ok` MUST be `True` with exit code `0`.

#### Scenario: Artifact discovery populates RunResult stdout from bootstrapper receipt
- **GIVEN** a configured `CloudRunLauncher` whose client returns an execution receipt with `artifacts={"sarif": "output/report.sarif"}` and no direct `stdout`
- **AND** `output/report.sarif` exists on disk containing valid SARIF JSON
- **WHEN** `launcher.run("find-diff", args)` is called
- **THEN** `launcher.run` MUST discover and read `output/report.sarif`
- **AND** populate `RunResult.stdout` with the SARIF file contents
- **AND** `RunResult.ok` MUST be `True` with exit code `0`.

#### Scenario: Bootstrapper failure exit code propagation
- **GIVEN** a configured `CloudRunLauncher` whose client returns a bootstrapper receipt with `status="FAILURE"`, `exit_code=2`, and `error="Extraction failed"`
- **WHEN** `launcher.run("find-diff", args)` is called
- **THEN** `launcher.run(...)` MUST return a `RunResult` with `exit_code == 2`
- **AND** `RunResult.ok` MUST be `False`.

#### Scenario: Client error raises LauncherError
- **GIVEN** a `CloudRunLauncher` whose `CloudRunClient` encounters an API or transit storage failure
- **WHEN** `launcher.run("find-diff", args)` is called
- **THEN** a `LauncherError` MUST be raised with the failure message
- **AND** the execution MUST fail cleanly without unhandled stack traces.

#### Scenario: Client timeout raises LauncherError
- **GIVEN** a `CloudRunLauncher` whose execution exceeds the configured timeout
- **WHEN** `launcher.run("find-diff", args)` is called
- **THEN** a `LauncherError` MUST be raised indicating execution timed out
- **AND** the process MUST fail cleanly without unhandled stack traces.

#### Scenario: Client non-zero exit code propagation
- **GIVEN** a configured `CloudRunLauncher` whose client returns an `ExecutionReceipt` with `exit_code == 2`
- **WHEN** `launcher.run("find-diff", args)` is called
- **THEN** `launcher.run(...)` MUST return a `RunResult` with `exit_code == 2`
- **AND** `RunResult.ok` MUST be `False`.

#### Scenario: Missing client parameter raises ValueError
- **GIVEN** `CloudRunLauncher` is instantiated with an empty or `None` client
- **WHEN** initialization executes
- **THEN** a `ValueError` MUST be raised requiring a valid client instance.

---

### REQ-0004: Entrypoint Launcher Factory Routing {#req-0004}

- **CUJ UUID:** cuj-gha-launcher-factory

The action entrypoint (`gha_runner.py`) MUST implement dynamic launcher factory routing based on environment variables:

1. Inspect `CODEMENDER_BACKEND` using truthy defaulting to `"local"`. Normalization MUST accept `"local"` and `"cloudrun"` (with `"cloud_run"` accepted as a normalized alias).
2. If normalized backend is `"local"`:
   - Validate that `CODEMENDER_RUNNER_IMAGE` is set; exit with code `1` and a GitHub error notice if missing.
   - Instantiate `LocalDockerLauncher`.
3. If normalized backend is `"cloudrun"`:
   - Resolve Cloud Run configuration from ambient environment variables (`GOOGLE_CLOUD_PROJECT`, `GCS_TRANSIT_BUCKET`, `CLOUD_RUN_JOB_NAME`, `CLOUD_RUN_REGION`) and ambient credentials.
   - Instantiate `CloudRunClient` in-process (or use an injected client instance) and pass it to `CloudRunLauncher`.
4. If `CODEMENDER_BACKEND` has an unsupported value:
   - Exit with code `1` and print an error notice naming the unsupported backend.

#### Scenario: Launcher factory selects CloudRunLauncher with in-process client
- **GIVEN** environment variable `CODEMENDER_BACKEND="cloudrun"`
- **WHEN** `gha_runner.main()` executes
- **THEN** `CloudRunLauncher` MUST be instantiated with an in-process `CloudRunClient`
- **AND** dispatched to `run_pr`.

#### Scenario: Normalized backend alias selects CloudRunLauncher
- **GIVEN** environment variable `CODEMENDER_BACKEND="cloud_run"`
- **WHEN** `gha_runner.main()` executes
- **THEN** `CloudRunLauncher` MUST be instantiated with an in-process `CloudRunClient`
- **AND** dispatched to `run_pr`.

#### Scenario: Unsupported backend exits with error
- **GIVEN** environment variable `CODEMENDER_BACKEND="unsupported_engine"`
- **WHEN** `gha_runner.main()` executes
- **THEN** the entrypoint MUST exit with code `1`
- **AND** output a GitHub Actions error notice naming `'unsupported_engine'`.

#### Scenario: Default backend selects LocalDockerLauncher
- **GIVEN** environment variable `CODEMENDER_BACKEND` is unset (or set to `"local"`)
- **AND** `CODEMENDER_RUNNER_IMAGE="img:sha"` is set
- **WHEN** `gha_runner.main()` executes
- **THEN** `LocalDockerLauncher` MUST be instantiated and dispatched to `run_pr`.

#### Scenario: Missing runner image in Local mode exits with error
- **GIVEN** environment variable `CODEMENDER_BACKEND="local"` (or unset)
- **AND** `CODEMENDER_RUNNER_IMAGE` is unset or empty
- **WHEN** `gha_runner.main()` executes
- **THEN** the entrypoint MUST exit with code `1`
- **AND** output a GitHub Actions error notice `::error::Missing required environment variable CODEMENDER_RUNNER_IMAGE.`.

---

### REQ-0005: Feedback Reporting Continuity {#req-0005}

- **CUJ UUID:** cuj-pr-comment-continuity

PR comment formatting and GitHub REST API reporting MUST function identically regardless of which launcher backend was selected:

1. If the launcher result is successful and contains a valid `version` event, the sticky comment MUST display the version table with a green checkmark (`✅ cm-runner is reachable.`).
2. If the launcher failed or raised a launcher error, the sticky comment MUST report the failure reason (`❌ cm-runner check failed.`).
3. Exit codes from `run_pr` MUST return `0` on success and `1` on failure.

#### Scenario: Successful execution reports version status table
- **GIVEN** a launcher returns a successful `RunResult` containing a valid version event
- **WHEN** PR handling executes
- **THEN** the sticky comment MUST be created or updated with a body containing `✅ cm-runner is reachable.` and version table details
- **AND** the handling function MUST return exit code `0`.

#### Scenario: Launcher failure reports error in sticky comment
- **GIVEN** a launcher raises a launcher error with reason `"Cloud Run API unavailable"`
- **WHEN** PR handling executes
- **THEN** the sticky comment MUST be created or updated with a body containing `❌ cm-runner check failed.` and `Reason: `Cloud Run API unavailable``
- **AND** the handling function MUST return exit code `1`.

#### Scenario: Remote execution non-zero exit code reports failure in sticky comment
- **GIVEN** a launcher returns an execution failure with exit code `2`
- **WHEN** PR handling executes
- **THEN** the sticky comment MUST be created or updated with a body containing `❌ cm-runner check failed.` and `cm-runner exited with code `2``
- **AND** the handling function MUST return exit code `1`.

---

### REQ-0006: Security & Credential Boundary Isolation {#req-0006}

- **CUJ UUID:** cuj-security-boundary

In compliance with [ADR-0003](../../../adr/ADR-0003-environment-scrubbing.md) and [ADR-0008](../../../adr/ADR-0008-repository-layout-and-package-structure.md):
1. The CloudRunClient SDK / client MUST handle GCP authentication internally using ambient Application Default Credentials (ADC) or workload identity credentials present on the host. Per ADR-0003, no denylist environment variable scrubbing is performed by the launcher.
2. Untrusted container outputs from Cloud Run MUST be safely escaped by comment formatters using inline Markdown sanitization before posting to GitHub.

#### Scenario: Untrusted container output markdown injection sanitization
- **GIVEN** a version event containing malicious markdown in fields (such as backticks or newlines `0.1.0`\n## Injected Heading`)
- **WHEN** the comment formatter renders the sticky comment body
- **THEN** backticks and newlines MUST be replaced with safe characters
- **AND** the rendered comment body MUST NOT contain unescaped injected markdown headers or breakout code spans.

---

### REQ-0007: Reference Workflow Template Configuration {#req-0007}

- **CUJ UUID:** cuj-workflow-template-config

The workflow template (`github-actions/workflows/cm-merge-scan.yml`) MUST provide documented reference configurations for both `local` and `cloudrun` execution:
1. Provide the default configuration targeting local Docker execution with `runner_image`.
2. Provide commented configuration examples illustrating Cloud Run invocation via `backend: cloudrun`.

#### Scenario: Template contains documented Cloud Run configuration
- **GIVEN** a user inspects `github-actions/workflows/cm-merge-scan.yml`
- **WHEN** reviewing the configuration options
- **THEN** the file MUST contain documented comments explaining how to enable `backend: cloudrun` without requiring runner images or client paths.

---

### REQ-0008: Symmetric Backend Interface & Workspace File Parity {#req-0008}

- **CUJ UUID:** cuj-backend-interface-parity

To minimize differences between actions that execute via Cloud Run and those that execute locally, the interface between the orchestrator action and the execution backends MUST be symmetric, identical, and standardized around file-based communication:

1. **Invocation Argument & CLI Parity**:
   - Both `local` and `cloudrun` launchers MUST accept identical command-line invocations, subcommands, positional arguments, and CLI flags (e.g. `version`, `find-diff`, `fix`, `verify`, `--output json`, `--log-level`).
   - Backends MUST NOT introduce backend-specific command flags, positional arguments, or alter parameter semantics.

2. **Stream & Output Contract Parity**:
   - Both backends MUST strictly satisfy [ADR-0001](../../../adr/ADR-0001-machine-readable-output-contract.md) by streaming live operational logs on `stderr` and emitting each command's result in its standard format on `stdout` (NDJSON events for `version`/`init`, SARIF for `find-diff`, and raw unified diff bytes for `fix`, which the launcher keeps as raw bytes per [SPEC-GHA-FIX REQ-0003](../github-actions-fix/spec.md#req-0003)).

3. **Workspace Filesystem & Artifact Symmetry**:
   - File exchange across the runner boundary is the standardized communication medium. All interactions MUST be constrained to interfaces that are supportable by both local containers and remote microVMs.
   - Any input files, configuration manifests, or repository files required by CodeMender MUST be consumed from identical relative workspace paths regardless of backend.
   - Any output files, diff patches, report summaries, or receipts (e.g. `output/receipt.json`) produced by CodeMender MUST be placed in identical relative workspace paths.
   - The Cloud Run Job container bootstrapper (`cloud-run-jobs/entrypoint.py`) stages the canonical finding artifact in the output directory: `output/report.sarif` (SARIF schema for `SPEC-FIND-DIFF`), alongside `output/receipt.json` detailing status, exit code, command, timestamps, finding count, error, and an artifacts mapping (`sarif`, `log`, `report`, `patch`).
   - `CloudRunLauncher` and downstream action steps MUST support structured receipt ingestion and artifact discovery across these standard output files (`output/report.sarif` and `output/receipt.json`), guaranteeing workspace artifact parity.
   - If the local Docker version puts an output file in a specific location (such as `<workspace>/output/receipt.json`) and the action reads that file to publish a comment or status, the Cloud Run version MUST also put that same file in that exact same location on the host runner before the launcher invocation completes.

4. **Downstream Step Agnosticism**:
   - Downstream action steps and orchestrator logic (including PR comment posting, GitHub Step Summary rendering, commit status checks, and artifact archiving) MUST NOT branch on `backend`. They MUST consume outputs and files uniformly regardless of which backend executed the command.

5. **Consolidated Remote Invocation**:
   - In Cloud Run mode, the orchestrator MUST dispatch a single consolidated `find-diff` remote task instead of issuing separate `init` and `find-diff` remote runs. The remote Cloud Run Job container automatically initializes the repository workspace from the staged snapshot archive prior to executing differential scanning.

#### Scenario: Identical output file path materialized across backends
- **GIVEN** a CodeMender command that outputs an execution artifact to `output/receipt.json`
- **WHEN** executed with `backend: local` or `backend: cloudrun`
- **THEN** upon command completion, `output/receipt.json` MUST exist at the identical relative workspace path
- **AND** the schema and contents of the artifact MUST be indistinguishable between backends.

#### Scenario: Canonical finding artifact and receipt staged by Cloud Run bootstrapper
- **GIVEN** a `find-diff` scan executes via `CloudRunLauncher` backed by `cloud-run-jobs/entrypoint.py`
- **WHEN** execution completes
- **THEN** `output/receipt.json` MUST exist detailing `status`, `exit_code`, `command`, `finding_count`, and `artifacts`
- **AND** the canonical finding artifact (`output/report.sarif`) MUST be materialized in the output directory
- **AND** downstream reporting steps MUST consume findings from `RunResult.stdout` or `output/report.sarif` identically to local Docker execution.

#### Scenario: Downstream reporting functions agnostically of backend
- **GIVEN** an output file produced by either backend at a standard workspace path
- **WHEN** downstream reporting logic formats a PR comment or Step Summary
- **THEN** the reporting logic MUST read the file from the standard path without inspecting or branching on `backend`.

#### Scenario: Symmetric CLI argument handling across backends
- **GIVEN** a launcher invocation `launcher.run("find-diff", ["--output", "json"])`
- **WHEN** dispatched to either `LocalDockerLauncher` or `CloudRunLauncher`
- **THEN** both launchers MUST dispatch equivalent command structures and flags to their respective execution environments.

#### Scenario: Single consolidated remote command dispatched in Cloud Run mode
- **GIVEN** `backend == 'cloudrun'`
- **WHEN** `gha_runner.py` executes PR analysis
- **THEN** the orchestrator MUST invoke `launcher.run("find-diff", ...)` as a single consolidated remote task
- **AND** MUST NOT issue a preliminary standalone remote `init` job invocation.
