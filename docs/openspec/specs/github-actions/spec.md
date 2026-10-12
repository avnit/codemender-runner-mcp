<!--
sdd_spec_id: SPEC-GHA
sdd_implements: None
sdd_extends: None
sdd_level: COMPONENT
sdd_archetype: CI_CD_WORKFLOW
sdd_cujspec_product: codemender-runner
-->

<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-10-01' }
*-->

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
2. **Cloud Run Mode (`cloudrun`)**: Delegates command execution to Google Cloud Run microVMs via a local client CLI developed in Workstream 3 of the [CM CI/CD Project Plan](https://docs.google.com/document/d/1Vv2o5C2UD3wYY9RQpq00dYst_5apT9-PO0CVoAV5wLU/edit). All Cloud Run infrastructure configuration details (GCP project, job name, region, transit storage) are managed directly by the Cloud Run client.

Both backends adhere to an identical, symmetric execution interface: CLI arguments, NDJSON stream contracts, and file-based workspace inputs and outputs (e.g. `output/receipt.json`, diff patches, reports) are placed in identical workspace locations. This minimizes operational differences and ensures downstream action steps operate transparently without backend branching.

This specification is governed by:
- [ADR-0001: Machine-Readable Output Contract](../../../adr/ADR-0001-machine-readable-output-contract.md) (NDJSON events on `stdout`, logs on `stderr`, coarse exit codes)
- [ADR-0002: Runner Layout and Build](../../../adr/ADR-0002-runner-layout-and-build.md) (Host token boundary; credentials never enter the container)
- [ADR-0004: Project Tech Stack & Scenario-Decoupled Monorepo Architecture](../../../adr/ADR-0004-project-tech-stack-and-repo-architecture.md)
- [ADR-0005: Multi-Platform Transit Storage & Sandboxed Isolation Model](../../../adr/ADR-0005-multi-platform-transit-storage-and-isolation.md)
- [Proposal: gha-cloudrun-launcher](../../proposals/gha-cloudrun-launcher/proposal.md)

---

## Requirements

### REQ-0001: Simplified Dual Backend Action Inputs {#req-0001}

- **CUJ UUID:** `cuj-gha-backend-config`

The GitHub Action metadata (`github-actions/actions/codemender/action.yml`) MUST support configurable execution backends through a minimal input schema:

| Input | Type | Required | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `backend` | string | No | `'local'` | Target execution backend: `'local'` (Docker on runner VM) or `'cloudrun'` (Cloud Run via canonical client CLI). |
| `runner_image` | string | Conditional | `''` | Container image tag. Required when `backend == 'local'`; optional when `backend == 'cloudrun'`. |
| `registry_token` | string | No | `''` | Token to authenticate to container registry for `docker pull`. Used only when `backend == 'local'`. |
| `log_level` | string | No | `'info'` | Log verbosity level passed to the orchestrator. |

All cloud-specific execution parameters (including GCP project, Cloud Run Job name, region, transit storage, and client executable location) MUST be managed internally by the action and the canonical Cloud Run client, and MUST NOT be exposed as action inputs.

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

- **CUJ UUID:** `cuj-gha-skip-docker-pull`

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
- **AND** `docker pull "$IMAGE"` is invoked.

---

### REQ-0003: CloudRunLauncher Execution & Subprocess Contract {#req-0003}

- **CUJ UUID:** `cuj-cloudrun-launcher-contract`

The orchestrator MUST provide a launcher component (`CloudRunLauncher`) that realizes the abstract launcher interface to dispatch commands to Cloud Run:

1. The launcher MUST accept:
   - `client_cmd`: Command string or sequence of arguments to invoke the Cloud Run client CLI.
   - `log_level`: Log verbosity string (default `'info'`).
   - `timeout_seconds`: Hard execution ceiling (default 3600 seconds).

2. When executing a command:
   - The launcher MUST construct an argv invoking the client CLI with the specified command, positional arguments, and flags:
     `[*client_cmd, command, *args, "--output", "json", "--log-level", log_level]`
   - Subprocess invocation MUST capture `stdout` (`stdout=subprocess.PIPE`) and inherit `stderr` (`stderr=None`) so that remote job logs stream live to the GitHub Actions job log.
   - `stdout` MUST be parsed into a list of event dictionaries via `parse_ndjson` per ADR-0001.
   - The launcher MUST return a result containing the exit code and parsed events.

3. Error handling:
   - If the client CLI binary or script is not found on the host, the launcher MUST raise a launcher error indicating the client command was not found.
   - If the subprocess exceeds `timeout_seconds`, the launcher MUST raise a launcher error indicating a timeout occurred.

#### Scenario: Successful version check via CloudRunLauncher
- **GIVEN** a configured `CloudRunLauncher` targeting a valid Cloud Run client
- **WHEN** `launcher.run("version")` is called
- **THEN** the client subprocess MUST be executed with `version --output json`
- **AND** valid NDJSON emitted on `stdout` MUST be parsed into `RunResult.events`
- **AND** `RunResult.ok` MUST be `True` with exit code `0`.

#### Scenario: Client command not found
- **GIVEN** `CloudRunLauncher` initialized with a non-existent client binary `nonexistent-client`
- **WHEN** `launcher.run("version")` is called
- **THEN** a launcher error MUST be raised with a descriptive error message
- **AND** the execution MUST fail cleanly without unhandled stack traces.

#### Scenario: Subprocess timeout raises LauncherError
- **GIVEN** a `CloudRunLauncher` configured with `timeout_seconds=5`
- **WHEN** the client subprocess execution exceeds 5 seconds
- **THEN** a launcher error MUST be raised indicating execution timed out
- **AND** the process MUST fail cleanly without unhandled stack traces.

#### Scenario: Client subprocess non-zero exit code propagation
- **GIVEN** a configured `CloudRunLauncher` targeting the Cloud Run client
- **WHEN** the client subprocess exits with non-zero exit code `2`
- **THEN** `launcher.run("version")` MUST return `RunResult` with `exit_code == 2`
- **AND** `RunResult.ok` MUST be `False`.

#### Scenario: Missing client command parameter raises ValueError
- **GIVEN** `CloudRunLauncher` is instantiated with an empty `client_cmd` (`""` or `[]` or whitespace-only)
- **WHEN** initialization executes
- **THEN** a `ValueError` MUST be raised requiring a valid client command.

---

### REQ-0004: Entrypoint Launcher Factory Routing {#req-0004}

- **CUJ UUID:** `cuj-gha-launcher-factory`

The action entrypoint (`gha_runner.py`) MUST implement dynamic launcher factory routing based on environment variables:

1. Inspect `CODEMENDER_BACKEND` using truthy defaulting to `"local"`. Normalization MUST accept `"local"` and `"cloudrun"` (with `"cloud_run"` accepted as a normalized alias).
2. If normalized backend is `"local"`:
   - Validate that `CODEMENDER_RUNNER_IMAGE` is set; exit with code `1` and a GitHub error notice if missing.
   - Instantiate `LocalDockerLauncher`.
3. If normalized backend is `"cloudrun"`:
   - Resolve the client command: inspect `CODEMENDER_CLOUDRUN_CLIENT`. If set and non-empty, use it (supporting mock injection and test overrides); otherwise default to the canonical client invocation `["python3", "gcp-sandboxing/scripts/client.py"]`.
   - Instantiate `CloudRunLauncher` configured with the resolved client command and log level.
4. If `CODEMENDER_BACKEND` has an unsupported value:
   - Exit with code `1` and print an error notice naming the unsupported backend.

#### Scenario: Launcher factory selects CloudRunLauncher with canonical client default
- **GIVEN** environment variable `CODEMENDER_BACKEND="cloudrun"` and `CODEMENDER_CLOUDRUN_CLIENT` is unset
- **WHEN** `gha_runner.main()` executes
- **THEN** `CloudRunLauncher` MUST be instantiated targeting the canonical client command `python3 gcp-sandboxing/scripts/client.py`
- **AND** dispatched to `run_pr`.

#### Scenario: Launcher factory respects CODEMENDER_CLOUDRUN_CLIENT override
- **GIVEN** environment variables `CODEMENDER_BACKEND="cloudrun"` and `CODEMENDER_CLOUDRUN_CLIENT="python3 mock_client.py"`
- **WHEN** `gha_runner.main()` executes
- **THEN** `CloudRunLauncher` MUST be instantiated with the overridden client command `"python3 mock_client.py"`
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

- **CUJ UUID:** `cuj-pr-comment-continuity`

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
- **GIVEN** a launcher raises a launcher error with reason `"client command not found"`
- **WHEN** PR handling executes
- **THEN** the sticky comment MUST be created or updated with a body containing `❌ cm-runner check failed.` and `Reason: `client command not found``
- **AND** the handling function MUST return exit code `1`.

#### Scenario: Subprocess non-zero exit code reports failure in sticky comment
- **GIVEN** a launcher returns an execution failure with exit code `2`
- **WHEN** PR handling executes
- **THEN** the sticky comment MUST be created or updated with a body containing `❌ cm-runner check failed.` and `cm-runner exited with code `2``
- **AND** the handling function MUST return exit code `1`.

---

### REQ-0006: Security & Credential Boundary Isolation {#req-0006}

- **CUJ UUID:** `cuj-security-boundary`

In compliance with [ADR-0002](../../../adr/ADR-0002-runner-layout-and-build.md) and [ADR-0004](../../../adr/ADR-0004-project-tech-stack-and-repo-architecture.md):
1. `GITHUB_TOKEN` MUST NOT be forwarded to the Cloud Run client CLI or passed as an environment variable to Cloud Run. The launcher MUST scrub `GITHUB_TOKEN` from environment variables passed to the subprocess.
2. The Cloud Run client CLI MUST handle GCP authentication internally using ambient Application Default Credentials (ADC) or workload identity credentials present on the host.
3. Untrusted container outputs from Cloud Run MUST be safely escaped by comment formatters using inline Markdown sanitization before posting to GitHub.

#### Scenario: Subprocess execution isolates host GitHub token
- **GIVEN** `GITHUB_TOKEN="ghp_super_secret_token"` is present in the host environment
- **WHEN** `CloudRunLauncher.run("version")` executes
- **THEN** the argv passed to `subprocess.run` MUST NOT contain the token value
- **AND** `subprocess.run` MUST NOT be passed an `env` dictionary containing `GITHUB_TOKEN`.

#### Scenario: Untrusted container output markdown injection sanitization
- **GIVEN** a version event containing malicious markdown in fields (such as backticks or newlines `0.1.0`\n## Injected Heading`)
- **WHEN** the comment formatter renders the sticky comment body
- **THEN** backticks and newlines MUST be replaced with safe characters
- **AND** the rendered comment body MUST NOT contain unescaped injected markdown headers or breakout code spans.

---

### REQ-0007: Reference Workflow Template Configuration {#req-0007}

- **CUJ UUID:** `cuj-workflow-template-config`

The workflow template (`github-actions/workflows/cm-merge-scan.yml`) MUST provide documented reference configurations for both `local` and `cloudrun` execution:
1. Provide the default configuration targeting local Docker execution with `runner_image`.
2. Provide commented configuration examples illustrating Cloud Run invocation via `backend: cloudrun`.

#### Scenario: Template contains documented Cloud Run configuration
- **GIVEN** a user inspects `github-actions/workflows/cm-merge-scan.yml`
- **WHEN** reviewing the configuration options
- **THEN** the file MUST contain documented comments explaining how to enable `backend: cloudrun` without requiring runner images or client paths.

---

### REQ-0008: Symmetric Backend Interface & Workspace File Parity {#req-0008}

- **CUJ UUID:** `cuj-backend-interface-parity`

To minimize differences between actions that execute via Cloud Run and those that execute locally, the interface between the orchestrator action and the execution backends MUST be symmetric, identical, and standardized around file-based communication:

1. **Invocation Argument & CLI Parity**:
   - Both `local` and `cloudrun` launchers MUST accept identical command-line invocations, subcommands, positional arguments, and CLI flags (e.g. `version`, `find-diff`, `fix`, `verify`, `--output json`, `--log-level`).
   - Neither backend may introduce backend-specific command flags, positional arguments, or alter parameter semantics.

2. **Stream & Output Contract Parity**:
   - Both backends MUST strictly satisfy [ADR-0001](../../../adr/ADR-0001-machine-readable-output-contract.md) by streaming live operational logs on `stderr` and emitting each command's result in its standard format on `stdout` (NDJSON events for `version`/`init`, SARIF for `find-diff`, and raw unified diff bytes for `fix`, which the launcher keeps as raw bytes per [SPEC-GHA-FIX REQ-0003](../github-actions-fix/spec.md#req-0003)).

3. **Workspace Filesystem & Artifact Symmetry**:
   - File exchange across the runner boundary is the standardized communication medium. All interactions MUST be constrained to interfaces that are supportable by both local containers and remote microVMs.
   - Any input files, configuration manifests, or repository files required by CodeMender MUST be consumed from identical relative workspace paths regardless of backend.
   - Any output files, diff patches, report summaries, or receipts (e.g. `output/receipt.json`) produced by CodeMender MUST be placed in identical relative workspace paths.
   - If the local Docker version puts an output file in a specific location (such as `<workspace>/output/receipt.json`) and the action reads that file to publish a comment or status, the Cloud Run version MUST also put that same file in that exact same location on the host runner before the launcher invocation completes.

4. **Downstream Step Agnosticism**:
   - Downstream action steps and orchestrator logic (including PR comment posting, GitHub Step Summary rendering, commit status checks, and artifact archiving) MUST NOT branch on `backend`. They MUST consume outputs and files uniformly regardless of which backend executed the command.

#### Scenario: Identical output file path materialized across backends
- **GIVEN** a CodeMender command that outputs an execution artifact to `output/receipt.json`
- **WHEN** executed with `backend: local` or `backend: cloudrun`
- **THEN** upon command completion, `output/receipt.json` MUST exist at the identical relative workspace path
- **AND** the schema and contents of the artifact MUST be indistinguishable between backends.

#### Scenario: Downstream reporting functions agnostically of backend
- **GIVEN** an output file produced by either backend at a standard workspace path
- **WHEN** downstream reporting logic formats a PR comment or Step Summary
- **THEN** the reporting logic MUST read the file from the standard path without inspecting or branching on `backend`.

#### Scenario: Symmetric CLI argument handling across backends
- **GIVEN** a command invocation with arguments `["find-diff", "--output", "json", "--log-level", "info"]`
- **WHEN** dispatched to either `LocalDockerLauncher` or `CloudRunLauncher`
- **THEN** both launchers MUST dispatch equivalent command structures and flags to their respective execution environments.
