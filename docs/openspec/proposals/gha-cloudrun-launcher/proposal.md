# Proposal: Optional Cloud Run Execution for GitHub Actions

**Change ID:** `gha-cloudrun-launcher` \
**Status:** Draft \
**Author:** Brent Collier (brentcollier@google.com) \
**Target Spec:** [SPEC-GHA](../../specs/github-actions/spec.md) \
**Bug/Epic:** go/cm-ci-cd-project-plan (Workstream 2)

## Why
The current GitHub Actions integration in `github-actions/` hardcodes local `docker run` execution on the runner VM. While zero-dependency for initial customer evaluations, local execution cannot provide strong microVM kernel sandboxing, is constrained by GitHub Actions runner execution budgets, and incurs unnecessary network/storage overhead by pulling runner images onto the host VM. Extending `github-actions/` to optionally execute CodeMender commands in Cloud Run via the client CLI enables enterprise-grade isolated sandboxing while preserving zero-setup local Docker execution as the default.

## What Changes
- **Core Launcher Extension**: Introduce `CloudRunLauncher` in `github-actions/actions/codemender/src/launcher/cloud_run.py` implementing the `Launcher(abc.ABC)` contract ([ADR-0002](../../../adr/ADR-0002.md)). It executes the Cloud Run client CLI via subprocess, streaming live logs on `stderr` and parsing NDJSON events from `stdout`.
- **Entrypoint Factory Routing**: Update `github-actions/actions/codemender/src/gha_runner.py` to inspect `CODEMENDER_BACKEND` (defaulting to `"local"`, accepting `"cloudrun"`). If `"cloudrun"`, it initializes `CloudRunLauncher` defaulting to the canonical Workstream 3 client script (with optional `CODEMENDER_CLOUDRUN_CLIENT` environment variable override for testing).
- **Simplified Action Inputs & Step Gating**: Extend `github-actions/actions/codemender/action.yml` to support `backend: 'local' | 'cloudrun'`. Cloud details (project, job name, region, client invocation) are handled internally rather than exposed through the action. The `docker pull` and Docker credential cleanup steps are conditionally bypassed when `backend: cloudrun` is selected.
- **Workflow Template**: Update `github-actions/workflows/cm-merge-scan.yml` to document and illustrate both local Docker and Cloud Run invocation configurations.
- **Hermetic Testing**: Add unit test coverage in `github-actions/actions/codemender/tests/test_launcher.py` and `test_gha_runner.py` verifying argument construction, NDJSON parsing, error handling on missing client or execution timeout, and launcher factory routing.

## Capabilities (The Core Contract)
### New Capabilities
- `cloud-run-launcher`: Dispatches CodeMender commands (`version` for M0 walking skeleton parity) to Cloud Run via the client CLI, returning standard NDJSON event streams. Maps to [docs/openspec/specs/github-actions/spec.md#req-0003](../../specs/github-actions/spec.md#req-0003).
- `backend-interface-parity`: Guarantees identical CLI invocation syntax, stream contracts, and workspace file/artifact locations (e.g. `output/receipt.json`) across both local Docker and Cloud Run backends via standardized file-based exchange. Maps to [docs/openspec/specs/github-actions/spec.md#req-0008](../../specs/github-actions/spec.md#req-0008).
### Modified Capabilities
- `workflow-execution-routing`: Extends GitHub Actions composite action and runner entrypoint to dynamically dispatch to either `LocalDockerLauncher` or `CloudRunLauncher` based on user-configured workflow inputs (`backend: 'local' | 'cloudrun'`). Maps to [docs/openspec/specs/github-actions/spec.md#req-0004](../../specs/github-actions/spec.md#req-0004).

## Impact
- **Affected Code & Paths:**
  - `github-actions/actions/codemender/action.yml`: Add input `backend`; add `if: inputs.backend == 'local'` condition to Docker pull step.
  - `github-actions/actions/codemender/src/launcher/cloud_run.py`: New file implementing `CloudRunLauncher`.
  - `github-actions/actions/codemender/src/launcher/__init__.py`: Export `CloudRunLauncher`.
  - `github-actions/actions/codemender/src/gha_runner.py`: Add backend resolution and launcher factory instantiation.
  - `github-actions/workflows/cm-merge-scan.yml`: Document Cloud Run mode usage.
  - `github-actions/actions/codemender/tests/test_launcher.py`: Unit tests for `CloudRunLauncher`.
  - `github-actions/actions/codemender/tests/test_gha_runner.py`: Unit tests for factory selection.
- **Dependencies:**
  - Zero additional external Python packages in `requirements.txt`. The client is invoked as a subprocess CLI and manages its own GCP authentication and cloud configuration internally.
- **Breaking Changes:**
  - None. Default behavior remains `backend: local` with `LocalDockerLauncher`.
