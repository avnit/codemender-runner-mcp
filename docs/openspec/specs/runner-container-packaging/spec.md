<!--*
freshness: { owner: 'brentcollier', reviewed: '2026-09-30' }
*-->

<!--
sdd_spec_id: SPEC-0003
sdd_implements: None
sdd_extends: None
sdd_level: COMPONENT
sdd_cujspec_product: codemender-runner
-->

# SPEC-0003: Lean Runner Container & Modular BYOI Packaging

**Implements Spec:** None

**Extends Spec:** None

## Overview

This specification defines the container packaging and runtime environment requirements for the CodeMender Runner image (`codemender-runner/Dockerfile`). Governed by [ADR-0001](../../../adr/ADR-0001-machine-readable-output-contract.md), [ADR-0008](../../../adr/ADR-0008-repository-layout-and-package-structure.md), and [ADR-0005](../../../adr/ADR-0005-multi-platform-transit-storage-and-isolation.md), this container executes across GitHub Actions runner matrix jobs and Google Cloud Run Jobs. The image implements a lean container strategy, packaging strictly what is necessary for orchestrator and CodeMender execution, and deferring multi-language toolchains to post-launch modular/BYOI containers.

## Requirements

### REQ-0001: Lean Base Container Image

*   **CUJ UUID:** cuj-container-toolchains

The container image MUST be based on a minimal Linux distribution (`ubuntu:22.04` or `debian:bookworm-slim`) and install only essential utilities (`git`, `curl`, `tar`, `gzip`, `ca-certificates`) and Python 3.11. To ensure fast container pull times and minimize the security attack surface, monolithic multi-language compiler suites (Node.js, Go, OpenJDK) SHALL NOT be pre-installed in the initial base container image.

#### Scenario: Lean Container Startup
-   **WHEN** the runner container is pulled in Cloud Run or GitHub Actions
-   **THEN** the image pulls rapidly without transferring gigabytes of unneeded multi-language compiler toolchains.

---

### REQ-0002: CodeMender CLI Discovery via PATH

*   **CUJ UUID:** cuj-container-cm-binary

The container MUST package the CodeMender native binary (`cm`) with executable permissions (`chmod +x`) located on the system `$PATH` (or configurable via `CM_PATH`), allowing invocation by both the orchestrator and child processes without coupling to rigid hardcoded path locations.

#### Scenario: Discovering cm Binary
-   **WHEN** the orchestrator or runner executes `cm --version`
-   **THEN** the binary is successfully resolved from `$PATH` and executes.

---

### REQ-0003: Isolated Python Virtual Environment

*   **CUJ UUID:** cuj-container-venv-isolation

The container MUST isolate orchestrator Python dependencies inside a dedicated virtual environment at `/opt/codemender/venv`. This isolation MUST protect orchestrator libraries from collisions with scanned repositories or user dependencies.

#### Scenario: Target Repo Installs Conflicting Libraries
-   **WHEN** a scanned repository executes `pip install` with incompatible library versions
-   **THEN** `/opt/codemender/venv` remains unaffected and orchestrator imports continue functioning.

---

### REQ-0004: Complete Orchestrator Module Packaging

*   **CUJ UUID:** cuj-container-modules

The container build MUST copy the full orchestrator package into `/opt/codemender/codemender-runner/`, including `codemender/`, `vcs/`, `cli/`, and `storage.py`. The container entrypoint MUST execute the isolated Python runtime.

#### Scenario: CLI Subcommand Execution in Container
-   **WHEN** the container is launched with command `python3 -m cli version`
-   **THEN** the `cli` package is imported cleanly and executes without `ModuleNotFoundError`.

---

### REQ-0005: Automated Container Build CI Workflow

*   **CUJ UUID:** cuj-container-ci-build

The repository MUST provide a GitHub Actions workflow (`.github/workflows/build_runner_image.yml`) that builds and publishes the lean container image upon changes to `codemender-runner/Dockerfile` or core orchestrator code, supporting dual CLI binary sourcing (Artifact Registry production releases vs. manual custom binary URLs).

#### Scenario: Automated Container Publishing
-   **WHEN** changes to `codemender-runner/` are merged to `main`
-   **THEN** the CI workflow builds the lean image and publishes it to GHCR with layer caching.
