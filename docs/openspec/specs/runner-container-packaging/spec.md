<!--*
freshness: { owner: 'brentcollier', reviewed: '2026-10-13' }
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
[ADR-0001](../../adr/ADR-0001-machine-readable-output-contract.md),
[ADR-0008](../../adr/ADR-0008-repository-layout-and-package-structure.md), and
[ADR-0005](../../adr/ADR-0005-multi-platform-transit-storage-and-isolation.md),
this container executes across GitHub Actions runner matrix jobs and Google
Cloud Run Jobs. The image implements a lean container strategy, packaging
strictly what is necessary for `packages/codemender` (`cm-runner`) and
CodeMender (`cm`) execution, and deferring multi-language toolchains to
post-launch modular/BYOI containers.

## Requirements

### REQ-0001: Lean Base Container Image {#req-0001}

*   **CUJ UUID:** cuj-container-toolchains

The container image MUST be based on a minimal Linux distribution
(`ubuntu:22.04` or `debian:bookworm-slim`) and install only essential utilities
(`git`, `curl`, `tar`, `gzip`, `ca-certificates`) and Python 3.11. To ensure
fast container pull times and minimize the security attack surface, monolithic
multi-language compiler suites (Node.js, Go, OpenJDK) SHALL NOT be pre-installed
in the initial base container image.

#### Scenario: Lean Container Startup
-   **WHEN** the runner container is pulled in Cloud Run or GitHub Actions
-   **THEN** the image pulls rapidly without transferring gigabytes of unneeded
    multi-language compiler toolchains.

---

### REQ-0002: CodeMender CLI Discovery via PATH {#req-0002}

*   **CUJ UUID:** cuj-container-cm-binary

The container MUST package the CodeMender native binary (`cm`) with executable
permissions (`chmod +x`) located on the system `$PATH` (or configurable via
`CM_PATH`), allowing invocation by both the orchestrator and child processes
without coupling to rigid hardcoded path locations.

#### Scenario: Discovering cm Binary
-   **WHEN** the orchestrator or runner executes `cm --version`
-   **THEN** the binary is successfully resolved from `$PATH` and executes.

---

### REQ-0003: Isolated Python Virtual Environment {#req-0003}

*   **CUJ UUID:** cuj-container-venv-isolation

The container MUST isolate orchestrator Python dependencies inside a dedicated
virtual environment at `/opt/codemender/venv`. This isolation MUST protect
orchestrator libraries from collisions with scanned repositories or user
dependencies.

#### Scenario: Target Repo Installs Conflicting Libraries
-   **WHEN** a scanned repository executes `pip install` with incompatible
    library versions
-   **THEN** `/opt/codemender/venv` remains unaffected and orchestrator imports
    continue functioning.

---

### REQ-0004: Complete Orchestrator Module Packaging {#req-0004}

*   **CUJ UUID:** cuj-container-modules

The container build (`docker/runner.Dockerfile` built with the repository root
`.` as context) MUST copy `packages/codemender` into
`/opt/codemender/packages/codemender` and install it into the isolated virtual
environment via
`/opt/codemender/venv/bin/pip install --no-cache-dir ./packages/codemender`,
installing the `cm-runner` console script at `/opt/codemender/venv/bin/cm-runner`
alongside `codemender`, `codemender.runner`, `codemender.runner.commands`,
`codemender.sarif`, and `codemender.vcs`. The container MUST configure
`ENTRYPOINT ["cm-runner"]` and `CMD ["version"]`.

#### Scenario: CLI Subcommand Execution in Container
-   **WHEN** the container is launched with `cm-runner version` (or default
    `CMD ["version"]`)
-   **THEN** the `codemender.runner` package is imported cleanly from
    `/opt/codemender/venv` and executes without `ModuleNotFoundError`.

---

### REQ-0005: Automated Container Build CI Workflow {#req-0005}

*   **CUJ UUID:** cuj-container-ci-build

The repository MUST provide a GitHub Actions workflow
(`.github/workflows/build_runner_image.yml`) that builds (`context: .`,
`file: docker/runner.Dockerfile`) and publishes the lean container image upon
changes to `docker/runner.Dockerfile`, `.dockerignore`, or
`packages/codemender/**`, supporting dual CLI binary sourcing (Artifact Registry
production releases vs. manual custom binary URLs).

#### Scenario: Automated Container Publishing
-   **WHEN** changes to `packages/codemender/` or `docker/runner.Dockerfile` are
    merged to `main`
-   **THEN** the CI workflow builds the lean image from the repository root
    context and publishes it to GHCR with layer caching.
