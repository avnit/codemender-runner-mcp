# Proposal: Cloud Run Jobs Container Bootstrapper & Execution Harness

**Change ID:** `cloudrun-job-bootstrapper` \
**Status:** Superseded / Consolidated into [SPEC-WORKER](../../specs/worker/spec.md) and [SPEC-0003](../../specs/runner-container-packaging/spec.md) \
**Author:** Jonathan Del Toro (jondeltoro@google.com) \
**Target Spec:** [SPEC-WORKER](../../specs/worker/spec.md), [SPEC-0003](../../specs/runner-container-packaging/spec.md) \
**Bug/Epic:** go/cm-ci-cd-project-plan (Workstream 3: Cloud Run Jobs)

## Why

Executing headless CodeMender scans and automated fix syntheses inside Google Cloud Run microVMs requires a resilient container bootstrapper to bridge remote cloud parameters, local workspace directory structures, preemption signal handling, and structured output artifact collection.

While `vcs-cloudrun-comms` defines the external client-to-cloud communications contract, a dedicated Python container bootstrapper specification (`cloud-run-jobs/entrypoint.py`) is required to govern internal container initialization, repository snapshot extraction (`code.tar.gz`), manifest parameter resolution, signal handling, failure-spill log capture, and `receipt.json` generation.

---

## What Changes

- **Multi-Command Container Dispatch**: Replace the shell bootstrapper with a native Python bootstrapper (`cloud-run-jobs/entrypoint.py`, configured in Dockerfile as `ENTRYPOINT ["python3", "/workspace/entrypoint.py"]`) supporting full CLI command dispatch (`version`, `init`, `find-diff`, `fix`, `verify`) and automatically stripping leading `cm-runner` container invocation wrappers.
- **Manifest & Execution Parameter Resolution**: Support `manifest.json` staging (`MANIFEST_URI` / `--manifest`) to populate task defaults (`command`, `payload_uri`, `output_prefix`, `finding_uri`, `diff_base`), while allowing explicit CLI flags and environment variables to act as execution overrides.
- **Standardized Archive Transit Unpacking ([Distributed Execution & Transit](../../specs/tech/systems/distributed-execution-and-transit/spec.md#req-0003))**: Standardize on gzip-compressed tarballs (`code.tar.gz`) as the primary repository transit format, unpacking into `/workspace/repo` with automatic single top-level directory flattening and POSIX permission preservation.
- **Finding Metadata Staging**: Download and stage single-finding SARIF metadata (`FINDING_GCS_URI` / `--finding`) into `/workspace/inputs/finding.sarif` for remediation runs (`fix` and `verify`).
- **Resilient Multi-Driver GCS Transit ([Distributed Execution & Transit](../../specs/tech/systems/distributed-execution-and-transit/design.md))**: Implement automated 4-tier download and upload transit helpers (`google-cloud-storage` SDK -> `gcloud storage` -> `gsutil` -> `file://` local paths) ensuring offline testability and execution fallback.
- **Structured Receipt Generation & Dual Findings Staging ([ADR-0002](../../../adr/ADR-0002.md), [Distributed Execution & Transit](../../specs/tech/systems/distributed-execution-and-transit/spec.md#req-0004))**: Emit deterministic `/workspace/output/receipt.json` cataloging `sarif` (`*.sarif`, `findings.json`), `report` (`*.md`), `patch` (`*.diff`, `*.patch`), and `log` (`runner.log`). Stage `findings.json` alongside `report.sarif` to satisfy both `vcs-cloudrun-comms` and `SPEC-FIND-DIFF` schema consumers.
- **Failure-Spill Logging & Preemption Signal Trapping ([Distributed Execution & Transit](../../specs/tech/systems/distributed-execution-and-transit/spec.md#req-0005))**: Stream stdout/stderr live to stderr while logging to `/workspace/output/runner.log`. Implement native Python signal handlers (`signal.SIGTERM` -> 143, `signal.SIGINT` -> 130) and exit cleanup handlers (`atexit`) to terminate child process groups and upload `/workspace/output` to GCS before exit.

---

## Capabilities (The Core Contract)

### New Capabilities
- `cloudrun-job-bootstrapper`: Production Python container bootstrapper and execution harness managing workspace directory layout (`/workspace`, `/workspace/output`, `/workspace/repo`, `/workspace/inputs`), multi-command dispatch, and GCS artifact syncing.
- `manifest-and-parameter-resolution`: Resolves task parameters from `manifest.json` with CLI flag and environment variable overrides.
- `archive-transit-unpacking`: Unpacks standard `code.tar.gz` repository archives into `/workspace/repo` with POSIX permission preservation.
- `resilient-gcs-transit-drivers`: Resilient 4-tier GCS transfer helpers (`google-cloud-storage` SDK -> `gcloud` -> `gsutil` -> `file://` local mock paths).
- `receipt-and-fail-spill-logging`: Emits structured `receipt.json`, tees container execution logs to `runner.log`, and handles preemption traps (`SIGTERM`).

---

## Impact

- **Proposed Code Changes & Paths:**
  - `cloud-run-jobs/entrypoint.py`: Introduce native Python container bootstrapper handling GCS transit, manifest loading, signal trapping, and receipt generation.
  - `docker/cloudrun.Dockerfile`: Update container layout (`/workspace`, `/workspace/output`, `/workspace/repo`, `/workspace/inputs`) and set `ENTRYPOINT ["python3", "/workspace/entrypoint.py"]`.
  - `cloud-run-jobs/tests/test_entrypoint.py`: Introduce unit and integration test suite validating container bootstrapper functions, manifest parameter resolution, archive extraction, and preemption traps.
  - `codemender-runner/tests/dummy_cm.py`: Enhance mock CLI backend for end-to-end local testing.
- **Dependencies:**
  - `google-cloud-storage` SDK, `gcloud`, or `gsutil` for GCS transit; standard library `tarfile` for archive extraction.
- **Breaking Changes:**
  - None. Backward-compatible with existing Cloud Run container invocations and ADR-0001 output contracts.
