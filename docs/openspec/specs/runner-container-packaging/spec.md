<!--*
freshness: { owner: 'brentcollier', reviewed: '2026-10-16' }
*-->

<!--
sdd_spec_id: SPEC-0003
sdd_implements: None
sdd_extends: None
sdd_level: COMPONENT
sdd_archetype: CLI_TOOL_BATCH_JOB
sdd_cujspec_product: codemender-runner
-->

# SPEC-0003: Lean Runner Container & Modular BYOI Packaging

**Implements Spec:** None

**Extends Spec:** None

## Overview

This specification defines the container packaging and runtime environment
requirements for the CodeMender Runner image (`docker/runner.Dockerfile`), built
from the repository root context (`.`). Governed by
[ADR-0002](../../../adr/ADR-0002.md),
the [Distributed Execution and Transit System Specification](../tech/systems/distributed-execution-and-transit/spec.md),
and
[ADR-0008](../../../adr/ADR-0008-repository-layout-and-package-structure.md),
this container executes across GitHub Actions runner matrix jobs, local
Docker/gVisor sandboxes, and Google Cloud Run Jobs. The image implements a lean
container strategy, packaging strictly what is necessary for
`packages/codemender` (`cm-runner`), `packages/launcher[cloudrun]`
(`cm-worker`), and CodeMender (`cm`) execution—while never installing
`packages/repohost`—and deferring multi-language toolchains to post-launch
modular/BYOI containers. Because the orchestrated worker flow (`Launcher` ->
`Worker` / `cm-worker`) across local Docker/gVisor and Google Cloud Run Jobs is
the most common execution case, `ENTRYPOINT ["cm-worker"]` is the default
container entrypoint, while standalone `cm-runner` invocations remain supported
via `docker run --entrypoint cm-runner`.

## Requirements

### REQ-0001: Lean Base Container Image {#req-0001}

- **CUJ UUID:** cuj-container-toolchains

The container image MUST be based on a minimal Linux distribution
(`ubuntu:22.04` or `debian:bookworm-slim`) and install only essential utilities
(`git`, `curl`, `tar`, `gzip`, `ca-certificates`) and Python 3.11. To ensure
fast container pull times and minimize the security attack surface, monolithic
multi-language compiler suites (Node.js, Go, OpenJDK) SHALL NOT be pre-installed
in the initial base container image.

#### Scenario: Lean Container Startup

- **WHEN** the runner container is pulled in Cloud Run or GitHub Actions
- **THEN** the image pulls rapidly without transferring gigabytes of unneeded
  multi-language compiler toolchains.

______________________________________________________________________

### REQ-0002: CodeMender CLI Discovery via PATH {#req-0002}

- **CUJ UUID:** cuj-container-cm-binary

The container MUST package the CodeMender native binary (`cm`) with executable
permissions (`chmod +x`) located on the system `$PATH` (or configurable via
`CM_PATH`), allowing invocation by both the orchestrator and child processes
without coupling to rigid hardcoded path locations.

#### Scenario: Discovering cm Binary

- **WHEN** the orchestrator or runner executes `cm --version`
- **THEN** the binary is successfully resolved from `$PATH` and executes.

______________________________________________________________________

### REQ-0003: Isolated Python Virtual Environment {#req-0003}

- **CUJ UUID:** cuj-container-venv-isolation

The container MUST isolate orchestrator Python dependencies inside a dedicated
virtual environment at `/opt/codemender/venv`. This isolation MUST protect
orchestrator libraries from collisions with scanned repositories or user
dependencies.

#### Scenario: Target Repo Installs Conflicting Libraries

- **WHEN** a scanned repository executes `pip install` with incompatible library
  versions
- **THEN** `/opt/codemender/venv` remains unaffected and orchestrator imports
  continue functioning.

______________________________________________________________________

### REQ-0004: Complete Orchestrator Module Packaging {#req-0004}

- **CUJ UUID:** cuj-container-modules

The container build (`docker/runner.Dockerfile` built with the repository root
`.` as context) MUST copy `packages/codemender` and `packages/launcher` into
`/opt/codemender/packages/` and install them into the isolated virtual
environment via
`/opt/codemender/venv/bin/pip install --no-cache-dir ./packages/codemender "./packages/launcher[cloudrun]"`,
installing both the `cm-worker` console script
(`/opt/codemender/venv/bin/cm-worker`) and the `cm-runner` console script
(`/opt/codemender/venv/bin/cm-runner`) on `$PATH` alongside `codemender`
(`codemender.runner`, `codemender.runner.commands`, `codemender.sarif`,
`codemender.vcs`) and `launcher`. The container build MUST NOT copy or install
`packages/repohost`. Because the orchestrated worker flow (`Launcher` ->
`cm-worker` -> `cm-runner`) is the most common execution path across local
Docker/gVisor and Google Cloud Run Jobs, the container MUST configure
`ENTRYPOINT ["cm-worker"]` as the default entrypoint (without a default `CMD`,
since `cm-worker` requires `--cache-uri` and `--task-id`), while keeping
`cm-runner` on `$PATH` for subprocess invocation by `cm-worker` and standalone
CLI execution via `--entrypoint cm-runner`.

#### Scenario: Default Worker Flow Execution in Container

- **WHEN** the container is launched with `--cache-uri <uri> --task-id <id>`
  (without overriding the entrypoint, as invoked by `LocalDockerLauncher` or
  `CloudRunLauncher`)
- **THEN** the default `cm-worker` entrypoint executes from
  `/opt/codemender/venv/bin/cm-worker`, imports `launcher` and `codemender`
  cleanly without `ModuleNotFoundError`, and invokes `cm-runner` on `$PATH`.

#### Scenario: Standalone CLI Subcommand Execution in Container

- **WHEN** the container is launched with `--entrypoint cm-runner` and a
  subcommand (e.g., `docker run --rm --entrypoint cm-runner <image> version`)
- **THEN** `cm-runner` resolves from `/opt/codemender/venv/bin/cm-runner`,
  imports `codemender.runner` cleanly from `/opt/codemender/venv`, and executes
  the subcommand without `ModuleNotFoundError`.

______________________________________________________________________

### REQ-0005: Automated Container Build CI Workflow {#req-0005}

- **CUJ UUID:** cuj-container-ci-build

The repository MUST provide a GitHub Actions workflow
(`.github/workflows/build_runner_image.yml`) that builds (`context: .`,
`file: docker/runner.Dockerfile`), verifies, and publishes the lean container
image upon changes to `docker/runner.Dockerfile`, `.dockerignore`,
`packages/codemender/**`, or `packages/launcher/**`, supporting dual CLI binary
sourcing (Artifact Registry production releases vs. manual custom binary URLs).
The workflow's image contract verification step MUST assert that
`ENTRYPOINT ["cm-worker"]` is configured, that `codemender` and `launcher`
import cleanly inside `/opt/codemender/venv`, and that
`docker run --rm --entrypoint cm-runner <image> version` succeeds.

#### Scenario: Automated Container Verification and Publishing

- **WHEN** changes to `packages/codemender/`, `packages/launcher/`,
  `.dockerignore`, or `docker/runner.Dockerfile` are merged to `main`
- **THEN** the CI workflow builds the lean image from the repository root
  context, verifies the `ENTRYPOINT ["cm-worker"]` and `cm-runner` contracts,
  and publishes the image to GHCR with layer caching.
