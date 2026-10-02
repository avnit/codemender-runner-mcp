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

# Design: GitHub Actions Orchestration & Execution Backends

## Context

The CodeMender GitHub Actions integration (`github-actions/`) provides automated scanning and remediation workflows for pull requests. Currently, M0 implements local execution using `docker run` on the Actions VM via `LocalDockerLauncher`.

However, enterprise environments require strong microVM isolation, execution durations exceeding standard CI runner timeouts, and isolation against malicious exploits during dynamic verification. Workstream 3 of the [CM CI/CD Project Plan](https://docs.google.com/document/d/1Vv2o5C2UD3wYY9RQpq00dYst_5apT9-PO0CVoAV5wLU/edit) is building the Google Cloud infrastructure (Cloud Run Jobs, GCS transit, WIF) and providing a local client CLI to manage job lifecycle.

Governed by [SPEC-GHA](spec.md), [ADR-0001](../../../adr/ADR-0001-machine-readable-output-contract.md), [ADR-0002](../../../adr/ADR-0002-runner-layout-and-build.md), and [ADR-0004](../../../adr/ADR-0004-project-tech-stack-and-repo-architecture.md), this design details how `github-actions/` is extended to support Cloud Run execution via the client CLI while preserving 100% backward compatibility for local Docker runs.

To keep the GitHub Action interface minimal and maintain a clean separation of concerns, the action input schema accepts only `backend: 'local' | 'cloudrun'` (and `runner_image` when running locally). All cloud-specific execution details (GCP project, Cloud Run Job name, region, IAM, transit buckets, and client invocation) are delegated entirely to the canonical Cloud Run client CLI and ambient environment.

---

## Goals / Non-Goals

### Goals
- Introduce `CloudRunLauncher` implementing `Launcher(abc.ABC)` to execute commands via the Cloud Run client CLI.
- Keep the action interface clean: simplify backend selection to `backend: 'local' | 'cloudrun'` and delegate all cloud configuration to the client CLI.
- Provide a clean launcher factory in `gha_runner.py` that selects between `LocalDockerLauncher` and `CloudRunLauncher` based on `CODEMENDER_BACKEND`.
- Bypass Docker pull and registry operations completely when running in Cloud Run mode.
- Maintain full compatibility with ADR-0001 NDJSON output contract and existing PR sticky comment reporting.
- Enforce strict host token isolation ([ADR-0002](../../../adr/ADR-0002-runner-layout-and-build.md)) by scrubbing `GITHUB_TOKEN` from the subprocess environment.
- Enforce symmetric interface and workspace file parity between local Docker and Cloud Run backends, standardizing on file-based communication so downstream actions remain backend-agnostic.
- Ensure 100% hermetic unit testing with mock subprocesses without needing live cloud infrastructure.

### Non-Goals
- Implementing or configuring the Cloud Run client CLI itself (owned by Workstream 3).
- Repository snapshot packaging or GCS transit uploading for `find-diff` (deferred to subsequent milestone M1/M2).
- GKE or Bitbucket launcher implementations (deferred to future milestones).

---

## Architecture & Data Flow

```mermaid
sequenceDiagram
    autonumber
    participant GitHub as GitHub Actions VM
    participant Action as actions/codemender
    participant Runner as gha_runner.py
    participant Launcher as CloudRunLauncher
    participant Client as Cloud Run Client CLI
    participant CloudRun as GCP Cloud Run Job
    participant PR as GitHub PR (API)

    GitHub->>Action: Trigger workflow (cm-merge-scan.yml)
    Action->>Action: Check backend == 'cloudrun' (Skip Docker pull)
    Action->>Runner: python src/gha_runner.py (CODEMENDER_BACKEND=cloudrun)
    Runner->>Runner: Launcher Factory: instantiate CloudRunLauncher
    Runner->>Launcher: launcher.run("version")
    Launcher->>Launcher: Scrub GITHUB_TOKEN from subprocess environment
    Launcher->>Client: subprocess.run([*client, "version", "--output", "json"], env=clean_env)
    Client->>Client: Resolve GCP Auth, Project, Job, & Region
    Client->>CloudRun: Dispatch Cloud Run Job execution
    CloudRun-->>Client: Stream execution logs & emit version receipt
    Client-->>Launcher: stdout: NDJSON event stream; stderr: live logs
    Launcher->>Launcher: parse_ndjson(stdout) -> RunResult
    Launcher-->>Runner: Return RunResult(exit_code=0, events=[version])
    Runner->>Runner: format_version_comment(result)
    Runner->>PR: post_or_update_sticky_comment(GITHUB_TOKEN)
```

---

## Detailed Design

### 1. `CloudRunLauncher` (`launcher/cloud_run.py`)

`CloudRunLauncher` bridges the abstract `Launcher` interface to the external Cloud Run client CLI.

Critical Security Control: Unlike `LocalDockerLauncher` (which invokes `docker run` on a separate container without environment inheritance), `CloudRunLauncher` executes a subprocess directly on the GitHub Actions host VM.

```python
import logging
import os
import shlex
import subprocess
from typing import List, Sequence, Union

from launcher.base import Launcher, LauncherError, RunResult, parse_ndjson

logger = logging.getLogger("codemender-github-actions")

_DEFAULT_TIMEOUT_SECONDS = 60 * 60


class CloudRunLauncher(Launcher):
  """Starts cm-runner inside Google Cloud Run via the client CLI."""

  def __init__(
      self,
      client_cmd: Union[str, Sequence[str]],
      log_level: str = "info",
      timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
  ):
    if not client_cmd:
      raise ValueError("A client command or path is required for Cloud Run.")
    if isinstance(client_cmd, str):
      self.client_cmd = shlex.split(client_cmd)
    else:
      self.client_cmd = list(client_cmd)
    if not self.client_cmd:
      raise ValueError("A client command or path is required for Cloud Run.")

    self.log_level = log_level
    self.timeout_seconds = timeout_seconds

  # This assumes cloud run interaction will be done through a cli, as opposed to
  # a python library/functions, but that is still to be determined.
  def build_command(self, command: str, args: Sequence[str] = ()) -> List[str]:
    return [
        *self.client_cmd,
        command,
        *args,
        "--output",
        "json",
        "--log-level",
        self.log_level,
    ]

  def run(self, command: str, args: Sequence[str] = ()) -> RunResult:
    argv = self.build_command(command, args)
    logger.info("Executing Cloud Run client: %s", " ".join(argv))

    # Security boundary (REQ-0006 & ADR-0002): explicitly scrub GITHUB_TOKEN
    # so host repository write credentials are never forwarded to the client CLI.
    clean_env = {k: v for k, v in os.environ.items() if k != "GITHUB_TOKEN"}

    try:
      proc = subprocess.run(
          argv,
          stdout=subprocess.PIPE,
          stderr=None,  # stderr streams live to GitHub Actions log
          text=True,
          timeout=self.timeout_seconds,
          check=False,
          env=clean_env,
      )
    except FileNotFoundError as e:
      raise LauncherError(
          f"Cloud Run client command {self.client_cmd[0]!r} not found."
      ) from e
    except subprocess.TimeoutExpired as e:
      raise LauncherError(
          f"Cloud Run command {command!r} timed out after {self.timeout_seconds}s."
      ) from e

    events = parse_ndjson(proc.stdout or "")
    return RunResult(exit_code=proc.returncode, events=events)
```

---

### 2. Launcher Factory & Entrypoint (`gha_runner.py`)

In `gha_runner.py`, `main()` inspects `CODEMENDER_BACKEND` with normalization and truthy fallback:

```python
# Updated top-level required variable check (runner image check moved to factory)
for required in ("GITHUB_EVENT_PATH", "GITHUB_REPOSITORY", "GITHUB_TOKEN"):
  if not env.get(required):
    print(f"::error::Missing required environment variable {required}.")
    return 1

# Robust backend resolution with truthy fallback and normalization
backend = (env.get("CODEMENDER_BACKEND") or "local").strip().lower()
if backend == "cloud_run":
  backend = "cloudrun"

if backend == "local":
  image = env.get("CODEMENDER_RUNNER_IMAGE", "").strip()
  if not image:
    print("::error::Missing required environment variable CODEMENDER_RUNNER_IMAGE.")
    return 1
  launcher = LocalDockerLauncher(
      image=image,
      log_level=env.get("CODEMENDER_LOG_LEVEL", "info"),
  )
elif backend == "cloudrun":
  client = env.get("CODEMENDER_CLOUDRUN_CLIENT", "").strip()
  if not client:
    # Default to canonical Workstream 3 client script
    client = "python3 gcp-sandboxing/scripts/client.py"
  launcher = CloudRunLauncher(
      client_cmd=client,
      log_level=env.get("CODEMENDER_LOG_LEVEL", "info"),
  )
else:
  print(f"::error::Unsupported CODEMENDER_BACKEND: {backend!r}.")
  return 1
```

---

### 3. Simplified Composite Action Metadata (`action.yml`)

The `inputs:` block in `action.yml` is streamlined to only what is necessary, removing all cloud infrastructure configuration inputs:

```yaml
inputs:
  backend:
    description: >-
      Target execution backend: 'local' (Docker on runner VM) or 'cloudrun'
      (Cloud Run via canonical client CLI). Defaults to 'local'.
    required: false
    default: 'local'
  runner_image:
    description: >-
      cm-runner container image pinned to a commit SHA tag. Required when
      backend is 'local'.
    required: false
    default: ''
  registry_token:
    description: >-
      Optional token to pull runner_image from GHCR. Used only when backend is 'local'.
    required: false
    default: ''
  log_level:
    description: cm-runner log level (debug, info, warning, error).
    required: false
    default: info
```

The composite steps conditionally pull the image and pass inputs to `gha_runner.py`:

```yaml
    # Pull step executes ONLY when running locally with Docker
    - name: Pull cm-runner image
      if: inputs.backend == 'local'
      shell: bash
      env:
        IMAGE: ${{ inputs.runner_image }}
        TOKEN: ${{ inputs.registry_token || github.token }}
        ACTOR: ${{ github.actor }}
      run: |
        if [[ -z "$IMAGE" ]]; then
          echo "::error::The 'runner_image' input is required when backend is 'local'."
          exit 1
        fi
        if [[ "$IMAGE" == ghcr.io/* ]]; then
          trap 'docker logout ghcr.io >/dev/null 2>&1 || true' EXIT
          echo "$TOKEN" | docker login ghcr.io -u "$ACTOR" --password-stdin
        fi
        docker pull "$IMAGE"

    - name: Run CodeMender
      shell: bash
      env:
        CODEMENDER_BACKEND: ${{ inputs.backend || 'local' }}
        CODEMENDER_RUNNER_IMAGE: ${{ inputs.runner_image }}
        CODEMENDER_LOG_LEVEL: ${{ inputs.log_level }}
        GITHUB_TOKEN: ${{ github.token }}
      run: python "${{ github.action_path }}/src/gha_runner.py"
```

---

### 4. Reference Workflow Template (`cm-merge-scan.yml`)

The template demonstrates dual configuration with documented comments:

```yaml
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false

      # Mode 1: Local Docker execution (default)
      - uses: ./.github/actions/codemender
        with:
          runner_image: <your-registry>/codemender-runner:<commit-sha>

      # Mode 2: Cloud Run execution (uncomment to enable)
      # - uses: ./.github/actions/codemender
      #   with:
      #     backend: cloudrun
```

---

### 5. Backend Interface Symmetry & Workspace Artifact Parity

To ensure seamless interchangeability between local and remote execution, interactions across the runner boundary are constrained to mechanisms supportable by both local containers and remote microVMs:

1. **Standardized Communication via Files**:
   Passing files across the boundary is established as the standard exchange medium for all complex data:
   - **Local Docker Mode**: Files are shared directly via bind mounts (`-v "$GITHUB_WORKSPACE:/workspace"`).
   - **Cloud Run Mode**: The client CLI manages input staging to GCS transit before job dispatch and materializes all output files (such as `output/receipt.json`, generated diff patches, and report files) from GCS transit back into the identical relative workspace paths on the Actions runner VM before exiting.

2. **Backend-Agnostic Downstream Consumers**:
   Because files are placed in identical locations and NDJSON events conform to ADR-0001 regardless of backend:
   - Comment formatters (`format_version_comment`) and PR sticky comment posters operate on the parsed `RunResult` without knowing which backend ran.
   - Any future steps reading output artifacts (e.g. `output/receipt.json` or patch diffs) read the exact same path without conditional branching on `inputs.backend`.

---

## Security Model

1. **Host-Only GitHub Token & Explicit Environment Scrubbing**:
   - `GITHUB_TOKEN` is used strictly on the host runner by `vcs/github.py` to create/update PR comments.
   - `CloudRunLauncher` explicitly scrubs `GITHUB_TOKEN` from `os.environ` when launching the subprocess, eliminating accidental token leakage to the client CLI or remote Cloud Run containers.
2. **Client-Managed Authentication & Cloud Config**:
   - The Cloud Run client manages all GCP authentication (ambient ADC/WIF) and cloud resource resolution internally without exposing cloud parameters to the action interface.
3. **Escaped Feedback Display**:
   - Output fields from Cloud Run events are sanitized with Markdown inline escaping (`_inline()`) to prevent Markdown injection in PR comments.

---

## Testing & Verification Plan

1. **`test_launcher.py`**:
   - `test_requires_client_cmd`: Verify `ValueError` when `client_cmd` is empty or whitespace.
   - `test_build_command`: Verify command argv construction (`[*client_cmd, "version", "--output", "json", "--log-level", "info"]`).
   - `test_run_success`: Mock `subprocess.run` returning valid version NDJSON, asserting `RunResult.ok == True` and event parsing.
   - `test_subprocess_scrubs_github_token`: Assert `GITHUB_TOKEN` is stripped from `subprocess.run`'s `env` kwargs.
   - `test_missing_client_raises_launcher_error`: Verify `FileNotFoundError` maps to `LauncherError`.
   - `test_timeout_raises_launcher_error`: Verify `subprocess.TimeoutExpired` maps to `LauncherError`.
   - `test_run_propagates_failure_exit_code`: Verify non-zero client exit codes propagate to `RunResult`.
2. **`test_gha_runner.py`**:
   - `test_launcher_factory_local_default`: Verify unset backend selects `LocalDockerLauncher`.
   - `test_launcher_factory_cloudrun_canonical_default`: Verify `CODEMENDER_BACKEND="cloudrun"` defaults to canonical client `python3 gcp-sandboxing/scripts/client.py`.
   - `test_launcher_factory_cloudrun_custom_client`: Verify `CODEMENDER_CLOUDRUN_CLIENT` environment variable overrides the client command.
   - `test_launcher_factory_cloud_run_alias`: Verify `CODEMENDER_BACKEND="cloud_run"` normalizes to `CloudRunLauncher`.
   - `test_missing_runner_image_fails_in_local_mode`: Verify exit code `1` when `CODEMENDER_RUNNER_IMAGE` is omitted in `local` mode.
   - `test_unsupported_backend_fails`: Verify exit code `1` on unknown backend.
