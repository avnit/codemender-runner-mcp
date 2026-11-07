<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-10-21' }
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

This specification defines the functional requirements, configuration interface, launcher abstraction, and execution routing for the CodeMender GitHub Actions integration (`.github/actions/`, `.github/workflows/`, `packages/repohost`, and `packages/launcher`; repository distribution and GitHub deployment models are governed by [SPEC-DIST](../distribute/spec.md)).

The integration allows repositories to run CodeMender during pull request workflows via two dedicated composite actions paired with a unified `cm-github` CLI orchestrator (`packages/repohost`):
1. **Cloud Run Action (`.github/actions/codemender/action.yml`, `CODEMENDER_BACKEND="cloudrun"`)**: Delegates command execution to Google Cloud Run microVMs via in-process integration with the `launcher.cloudrun.client.CloudRunClient` Python SDK in `packages/launcher[cloudrun]` (using typed in-process SDK dispatch per the [Distributed Execution and Transit System Design](../tech/systems/distributed-execution-and-transit/design.md) and [ADR-0008](../../../adr/ADR-0008-repository-layout-and-package-structure.md)). All Cloud Run infrastructure configuration details (GCP project, job name, region, transit storage) are resolved from action inputs or ambient environment variables and authenticated via Workload Identity Federation (WIF).
2. **Local Docker Action (`.github/actions/codemender-local/action.yml`, `CODEMENDER_BACKEND="local"`)**: Runs `cm-runner` natively on the GitHub Actions runner VM inside a throwaway gVisor (`runsc`) Docker container via `LocalDockerLauncher` in `packages/launcher` with zero external cloud job dependencies.

Both actions install the required workspace packages (`packages/codemender`, `packages/launcher[cloudrun]` or `packages/launcher`, and `packages/repohost[github]`) and invoke the `cm-github` CLI entrypoint (`repohost.github.__main__:main`). Both backends adhere to an identical, symmetric execution interface: CLI arguments, NDJSON stream contracts, and file-based workspace inputs and outputs (e.g. `output/receipt.json`, diff patches, reports) are placed in identical workspace locations. This minimizes operational differences and ensures downstream orchestrator steps operate transparently without backend branching.

> [!NOTE]
> **Action Granularity & Workflow Evolution:** Packaging these execution modes into two composite actions (`.github/actions/codemender` and `.github/actions/codemender-local`) represents the current design choice to cleanly isolate host provisioning dependencies (gVisor/Docker vs. GCP client SDKs). As workflow requirements mature—particularly when introducing more granular lifecycle phase actions (such as separate scan, fix, and verify actions)—this action split may be revisited. The normative requirements in this specification govern the underlying execution contracts, dependency isolation, and backend routing rather than cementing the two-action split as a permanent invariant.

This specification is governed by:
- [SPEC-DIST: Customer Repository Distribution, GitHub Deployment Models & Cross-Repo Action Resolution](../distribute/spec.md)
- [Distributed Execution and Transit System Specification](../tech/systems/distributed-execution-and-transit/spec.md) and [Design](../tech/systems/distributed-execution-and-transit/design.md)
- [ADR-0002: Machine-Readable Output Contract](../../../adr/ADR-0002.md) (NDJSON events on `stdout`, logs on `stderr`, coarse exit codes)
- [ADR-0003: Environment Variable Scrubbing](../../../adr/ADR-0003-environment-scrubbing.md)
- [ADR-0008: Repository Layout, Three-Package Workspace, and Runtime Architecture](../../../adr/ADR-0008-repository-layout-and-package-structure.md) (Three-package workspace, `cm-github` CLI entrypoint, and host token boundary)
- [Proposal: gha-cloudrun-launcher](../../proposals/gha-cloudrun-launcher/proposal.md)
- [Proposal: Cloud Run Jobs Container Bootstrapper & Execution Harness](../../proposals/cloudrun-job-bootstrapper/proposal.md)

---

## Requirements

### REQ-0001: Declarative Action Boundaries & Scoped Dependencies (`codemender` vs. `codemender-local`) {#req-0001}

- **CUJ UUID:** cuj-gha-backend-config

The GitHub Actions integration MUST provide declarative composite action entrypoints containing only YAML step definitions without embedded Python source files or `requirements.txt` files (governed by [ADR-0008](../../../adr/ADR-0008-repository-layout-and-package-structure.md)):

1. **Pure-YAML Action Boundary:**
   - Both `.github/actions/codemender/action.yml` and `.github/actions/codemender-local/action.yml` MUST execute as pure composite actions that install workspace packages and delegate execution to the `cm-github` CLI orchestrator (`packages/repohost`).
   - Neither action MUST expose a redundant `backend` input (`codemender` hardcodes `CODEMENDER_BACKEND: "cloudrun"`; `codemender-local` hardcodes `CODEMENDER_BACKEND: "local"`).

2. **Scoped Dependency Isolation:**
   - `.github/actions/codemender/action.yml` MUST install `codemender`, `launcher[cloudrun]`, and `repohost[github]` to provide Google Cloud SDKs for remote Cloud Run Job dispatch without installing local Docker or gVisor tools.
   - `.github/actions/codemender-local/action.yml` MUST install `codemender`, `launcher` (omitting `[cloudrun]`), and `repohost[github]`, avoiding unused GCP SDK dependencies during local Docker execution.

3. **Pre-Flight Base Commit Verification:**
   - Before executing compute-intensive tasks, WIF authentication, or container provisioning, `cm-github --preflight` MUST verify that the pull request's base commit is present in the local git clone (attempting a shallow fetch of the base commit if absent), exiting with code `1` and an error notice if the base commit cannot be resolved.

4. **Action Input Contracts:**
   - Both actions MUST expose shared configuration for GCP project and Workload Identity Federation authentication (`gcp_project_id`, `gcp_workload_identity_provider`, `gcp_service_account`), log verbosity (`log_level`), diff parallelism (`diff_workers`), remediation controls (`fix`, `max_parallel_fixes`), and artifact retention (`artifact_retention_days`).
   - `.github/actions/codemender/action.yml` MUST support Cloud Run execution overrides (`cloud_run_region`, `gcs_transit_bucket`, `cloud_run_job_name`).
   - `.github/actions/codemender-local/action.yml` MUST support local container runtime configuration (`runner_image`, `registry_token`, `container_runtime`, `gvisor_release`).

#### Scenario: Cloud Run composite action installs cloudrun extra and sets cloudrun backend
- **GIVEN** a workflow step invokes `./.github/actions/codemender`
- **WHEN** the composite action executes
- **THEN** it MUST install `codemender`, `launcher[cloudrun]`, and `repohost[github]`
- **AND** it MUST invoke `cm-github` with `CODEMENDER_BACKEND="cloudrun"` without requiring `runner_image`.

#### Scenario: Local composite action installs base launcher and resolves runner_image
- **GIVEN** a workflow step invokes `./.github/actions/codemender-local`
- **WHEN** the composite action executes
- **THEN** it MUST install `codemender`, `launcher` (without `[cloudrun]`), and `repohost[github]`
- **AND** it MUST invoke `cm-github` with `CODEMENDER_BACKEND="local"` and `CODEMENDER_RUNNER_IMAGE` set to the resolved runner image.

#### Scenario: Base commit verification succeeds or fetches missing base commit
- **GIVEN** a pull request event where the base commit is absent from a shallow checkout
- **WHEN** `cm-github --preflight` executes
- **THEN** it MUST attempt a shallow fetch of the base commit
- **AND** emit an error notice and exit with code `1` if the base commit cannot be retrieved.

---

### REQ-0002: Backend-Isolated Host Provisioning & Docker Pull Bypass {#req-0002}

- **CUJ UUID:** cuj-gha-skip-docker-pull

To prevent unnecessary bandwidth usage, image download latency, gVisor installation overhead, and runner disk consumption:

1. `.github/actions/codemender-local/action.yml` MUST install the gVisor `runsc` runtime on the host runner VM (when `container_runtime == 'runsc'`), resolve `runner_image` (defaulting to the distributed repository image per [SPEC-DIST](../distribute/spec.md)), normalize image repository names to lowercase, and pull the container image before executing `cm-github`.
2. `.github/actions/codemender/action.yml` MUST NOT install gVisor or execute any Docker CLI login, pull, or logout commands on the host runner.

#### Scenario: Cloud Run action bypasses gVisor and Docker pull
- **GIVEN** a workflow step invokes `./.github/actions/codemender`
- **WHEN** the action executes
- **THEN** no gVisor installation or `docker pull` step MUST be executed on the host runner.

#### Scenario: Local action installs gVisor and pulls runner image
- **GIVEN** a workflow step invokes `./.github/actions/codemender-local` with `inputs.runner_image` specified
- **WHEN** the action executes
- **THEN** the `Install gVisor (runsc)` and `Pull cm-runner image` steps MUST execute
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
   - This ensures downstream consumers (such as `_parse_scan_findings` in `repohost.github`) receive valid SARIF bytes identically across both local Docker and Cloud Run backends.

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

### REQ-0004: Entrypoint Launcher Factory Routing (`cm-github`) {#req-0004}

- **CUJ UUID:** cuj-gha-launcher-factory

The GitHub orchestrator CLI entrypoint (`cm-github`, mapped to `repohost.github.__main__:main` in `packages/repohost`) MUST implement dynamic launcher factory routing based on environment variables:

1. Inspect `CODEMENDER_BACKEND` using truthy defaulting to `"cloudrun"` (with `"local"` supported for local runner execution). Normalization MUST accept `"local"` and `"cloudrun"` (with `"cloud_run"` accepted as a normalized alias).
2. If normalized backend is `"local"`:
   - Validate that `CODEMENDER_RUNNER_IMAGE` is set; exit with code `1` and a GitHub error notice if missing.
   - Instantiate the local Docker launcher (`LocalDockerLauncher`).
3. If normalized backend is `"cloudrun"`:
   - Resolve Cloud Run configuration from ambient environment variables (`GOOGLE_CLOUD_PROJECT`, `GCS_TRANSIT_BUCKET`, `CLOUD_RUN_JOB_NAME`, `CLOUD_RUN_REGION`) and ambient credentials.
   - Instantiate the Cloud Run client (`CloudRunClient`) in-process and pass it to `CloudRunLauncher`.
4. If `CODEMENDER_BACKEND` has an unsupported value:
   - Exit with code `1` and print an error notice naming the unsupported backend.

#### Scenario: Launcher factory selects CloudRunLauncher with in-process client
- **GIVEN** environment variable `CODEMENDER_BACKEND="cloudrun"`
- **WHEN** `cm-github` (`repohost.github.__main__.main()`) executes
- **THEN** `CloudRunLauncher` MUST be instantiated with an in-process `CloudRunClient`
- **AND** dispatched to `run_pr`.

#### Scenario: Normalized backend alias selects CloudRunLauncher
- **GIVEN** environment variable `CODEMENDER_BACKEND="cloud_run"`
- **WHEN** `cm-github` (`repohost.github.__main__.main()`) executes
- **THEN** `CloudRunLauncher` MUST be instantiated with an in-process `CloudRunClient`
- **AND** dispatched to `run_pr`.

#### Scenario: Unsupported backend exits with error
- **GIVEN** environment variable `CODEMENDER_BACKEND="unsupported_engine"`
- **WHEN** `cm-github` (`repohost.github.__main__.main()`) executes
- **THEN** the entrypoint MUST exit with code `1`
- **AND** output a GitHub Actions error notice naming `'unsupported_engine'`.

#### Scenario: Missing runner image in Local mode exits with error
- **GIVEN** environment variable `CODEMENDER_BACKEND="local"`
- **AND** `CODEMENDER_RUNNER_IMAGE` is unset or empty
- **WHEN** `cm-github` (`repohost.github.__main__.main()`) executes
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

The workflow template (`.github/workflows/cm-merge-scan.yml`) MUST provide documented reference configurations for both `.github/actions/codemender` (Cloud Run) and `.github/actions/codemender-local` (Local Docker + gVisor) execution without passing a `backend` input:
1. Provide the active Mode 1 configuration targeting Cloud Run execution via `uses: ./.github/actions/codemender`.
2. Provide commented Mode 2 configuration examples illustrating local Docker + gVisor invocation via `uses: ./.github/actions/codemender-local` with `runner_image`.

#### Scenario: Template contains documented Cloud Run and Local action configurations
- **GIVEN** a user inspects `.github/workflows/cm-merge-scan.yml`
- **WHEN** reviewing the configuration options
- **THEN** the file MUST contain documented steps for both `./.github/actions/codemender` (Cloud Run) and `./.github/actions/codemender-local` (Local Docker + gVisor) without passing a redundant `backend` input.

---

### REQ-0008: Symmetric Backend Interface & Workspace File Parity {#req-0008}

- **CUJ UUID:** cuj-backend-interface-parity

To minimize differences between actions that execute via Cloud Run and those that execute locally, the interface between the orchestrator action and the execution backends MUST be symmetric, identical, and standardized around file-based communication:

1. **Invocation Argument & CLI Parity**:
   - Both `local` and `cloudrun` launchers MUST accept identical command-line invocations, subcommands, positional arguments, and CLI flags (e.g. `version`, `find-diff`, `fix`, `verify`, `--output json`, `--log-level`).
   - Backends MUST NOT introduce backend-specific command flags, positional arguments, or alter parameter semantics.

2. **Stream & Output Contract Parity**:
   - Both backends MUST strictly satisfy [ADR-0002](../../../adr/ADR-0002.md) by streaming live operational logs on `stderr` and emitting each command's result in its standard format on `stdout` (NDJSON events for `version`/`init`, SARIF for `find-diff` and `verify`, and raw unified diff bytes for `fix`, which the launcher keeps as raw bytes per [SPEC-GHA-FIX REQ-0003](../github-actions-fix/spec.md#req-0003)).

3. **Workspace Filesystem & Artifact Symmetry**:
   - File exchange across the runner boundary is the standardized communication medium. All interactions MUST be constrained to interfaces that are supportable by both local containers and remote microVMs.
   - Any input files, configuration manifests, or repository files required by CodeMender MUST be consumed from identical relative workspace paths regardless of backend.
   - Any output files, diff patches, verified SARIF files, report summaries, or receipts (e.g. `output/receipt.json`) produced by CodeMender MUST be placed in identical relative workspace paths.
   - The Cloud Run Job container bootstrapper (`cloud-run-jobs/entrypoint.py`) stages the canonical command artifact in the output directory (`output/report.sarif` for `find-diff`, `output/finding.diff` for `fix`, and `output/finding.sarif` for `verify`), alongside `output/receipt.json` detailing status, exit code, command, timestamps, finding count, error, and an artifacts mapping (`sarif`, `log`, `report`, `patch`).
   - `CloudRunLauncher` and downstream action steps MUST support structured receipt ingestion and artifact discovery across these standard output files (`output/report.sarif`, `output/finding.sarif`, and `output/receipt.json`), guaranteeing workspace artifact parity.
   - If the local Docker version puts an output file in a specific location (such as `<workspace>/output/receipt.json` or `<out_dir>/finding.sarif`) and the action reads that file to publish a comment or status, the Cloud Run version MUST also put that same file in that exact same location on the host runner before the launcher invocation completes.

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
- **GIVEN** `CODEMENDER_BACKEND == 'cloudrun'`
- **WHEN** `cm-github` (`repohost.github`) executes PR analysis
- **THEN** the orchestrator MUST invoke `launcher.run("find-diff", ...)` as a single consolidated remote task
- **AND** MUST NOT issue a preliminary standalone remote `init` job invocation.
