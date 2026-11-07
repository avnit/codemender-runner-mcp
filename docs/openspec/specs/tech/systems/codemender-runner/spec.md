<!--
sdd_spec_id: docs/openspec/specs/tech/systems/codemender-runner/spec.md
sdd_level: SYSTEM
sdd_implements:
  - docs/openspec/specs/journeys/pr-automated-remediation/spec.md
  - docs/openspec/specs/journeys/pr-on-demand-verify/spec.md
sdd_extends: NONE
-->

# CodeMender Runner System Specification

**Implements Spec:** `docs/openspec/specs/journeys/pr-automated-remediation/spec.md`, `docs/openspec/specs/journeys/pr-on-demand-verify/spec.md` (see [User & Operator Journeys Index](../../../journeys/index.md))

**Extends Spec:** None

**Governing Journeys:**
- **Change Author (`pr-author`)**: As a developer opening or updating a proposed change on any repository hosting site, I want automated differential vulnerability discovery and remediation patches delivered without exposing repository credentials or mutating my working checkout during untrusted code execution.
- **Change Reviewer (`pr-reviewer`)**: As a reviewer or security engineer, I want on-demand or delayed exploitability verification backed by isolated sandbox execution and standard machine-readable triage outcomes across repository review threads and security workflows.
- **Platform & Security Operator (`ci-operator`)**: As a platform or security operator, I want one extensible orchestration and sandboxing system that starts with CI/CD change reviews and runs identically across scheduled audits, fleet-wide batch remediation campaigns, enterprise platform services, and standalone headless invocations across pluggable compute, transit, and durable storage environments.

## Overview

`codemender-runner` is the top-level orchestration and sandboxed execution system integrating the CodeMender (`cm`) code analysis, patch-synthesis, and exploitability-verification engine across customer deployment scenarios. While automated change-review workflows (pull requests, merge requests, and changelists) in CI/CD pipelines form the initial starting point, the system supports any repository hosting site alongside scheduled full-repository audits, fleet-wide batch remediation campaigns, delayed verification runs, custom enterprise services, and standalone local or headless invocations across organization-wide, single-repository, self-hosted, and self-dogfooding adoption models.

This specification defines the observable system-level contracts for execution-plane credential and callback isolation, scenario-scoped dependency isolation, host-neutral stateless workflow composition, progressive completion-order outcome delivery, caller checkout non-mutation, cross-runtime sandbox behavioral equivalence, and split-stream CLI contracts.

## Requirements

### REQ-0001: Execution-Plane Credential and Callback Isolation {#req-0001}

Sandboxed code analysis, patch synthesis, verification, and target repository build/test execution MUST operate in an isolated execution plane that never installs, loads, or requires repository-host communication clients or external hosting/durable-sink credentials, and MUST complete with zero outbound callbacks from the sandbox to any repository hosting platform or external findings database ([ADR-0008](../../../../../adr/ADR-0008-repository-layout-and-package-structure.md), [Security and Trust Zones](../security-and-trust-zones/spec.md#req-0001)).

#### Scenario: Sandboxed task execution completes without repository-host clients, credentials, or callbacks

- **GIVEN** the isolated sandbox runtime executing vulnerability discovery, patch synthesis, or exploitability verification against untrusted repository code
- **WHEN** the sandbox runtime environment is inspected and executes a task to completion
- **THEN** repository-host communication clients and external hosting or findings-sink credentials MUST be absent from the sandbox, and the task MUST complete without making outbound requests to any repository hosting platform or external findings database.

### REQ-0002: Scenario-Scoped Dependency Isolation {#req-0002}

Deploying or invoking any single selected customer scenario—a chosen repository hosting site, sandbox compute/transit runtime, durable findings sink, or host-less standalone/batch run—MUST NOT require installing SDKs, provisioning infrastructure, or supplying credentials for unconfigured repository hosts, compute backends, or storage sinks ([ADR-0008](../../../../../adr/ADR-0008-repository-layout-and-package-structure.md)).

#### Scenario: Single-scenario or host-less deployment runs without unused provider dependencies

- **GIVEN** a deployment configured either for one specific repository hosting site and sandbox runtime or for host-less standalone/batch execution with no repository host configured
- **WHEN** an operator or automated caller runs a workflow in that environment
- **THEN** execution MUST succeed without installing SDKs, provisioning services, or presenting credentials belonging to unconfigured repository hosts, compute backends, or durable storage sinks.

### REQ-0003: Host-Neutral Stateless Workflow Composition {#req-0003}

Vulnerability discovery (`find`), automated patch synthesis (`fix`), and exploitability verification (`verify`) workflows MUST operate on repository snapshots, exchange exclusively host-neutral SARIF 2.1.0 analysis reports, updated SARIF 2.1.0 verification-status reports, unified diff patches, and structured execution receipts via an ephemeral run-scoped transit store, and remain memory-stateless across invocations so interactive change reviews, multi-stage pipeline shards, delayed outer-host-hydrated runs, scheduled audits, batch campaigns, enterprise services, and headless runs compose identically.

#### Scenario: Stateless workflows compose identically across interactive, delayed, and host-less runs

- **GIVEN** a repository snapshot and a configured ephemeral run-scoped transit store
- **WHEN** discovery, remediation, or verification executes either inside an interactive repository-host change review, across separate multi-stage or delayed runs hydrated on the outer host from prior scan artifacts, or in a host-less scheduled, batch, service, or headless run
- **THEN** core workflow execution MUST consume and emit identical host-neutral SARIF 2.1.0 reports, unified diff patches, and structured execution receipts via the transit store without requiring in-memory coordinator state or repository-host metadata inside sandbox execution.

### REQ-0004: Progressive Completion-Order Outcome Delivery {#req-0004}

During concurrent multi-finding remediation (`fix`) and verification (`verify`) runs, the orchestration layer MUST deliver each completed finding's structured outcome in completion order without blocking callers on slower in-flight tasks.

#### Scenario: Fast concurrent remediation or verification outcome streams ahead of slower tasks

- **GIVEN** a concurrent multi-finding remediation or verification run in which one sandbox task completes early while another sandbox task remains in flight
- **WHEN** the fast task finishes recording its output artifact and structured execution receipt
- **THEN** the orchestration layer MUST deliver that finding's structured outcome immediately to the caller before the slower in-flight task terminates.

### REQ-0005: Caller Checkout Non-Mutation {#req-0005}

Executing `find`, `fix`, or `verify` MUST leave all tracked and untracked files in the caller's live repository checkout unmodified, performing any prerequisite sandbox workspace initialization transparently inside isolated scratch storage.

#### Scenario: Sandbox initialization and workflow execution leave the caller checkout clean

- **GIVEN** a caller repository checkout containing tracked files and local untracked files prior to running `find`, `fix`, or `verify`
- **WHEN** the workflow performs prerequisite workspace initialization and executes inside the sandbox runtime
- **THEN** all tracked and untracked files in the caller's live repository checkout MUST remain byte-for-byte unmodified after the run completes or fails.

### REQ-0006: Cross-Runtime Sandbox Behavioral Equivalence {#req-0006}

Executing identical workflow inputs across local container sandboxes and remote cloud or cluster job runtimes MUST produce schema-equivalent SARIF 2.1.0 reports, unified diff patches, structured execution receipts, and exit behavior.

#### Scenario: Identical inputs yield schema-equivalent artifacts and receipts across local and remote runtimes

- **GIVEN** an identical repository snapshot and workflow request executed once on a local container sandbox runtime and once on a remote cloud or cluster job runtime
- **WHEN** both executions finish
- **THEN** both runtimes MUST emit schema-equivalent SARIF 2.1.0 or unified diff artifacts, matching structured execution receipt fields, and equivalent exit status semantics.

### REQ-0007: Split-Stream CLI Output and Exit Code Contract {#req-0007}

Every CLI surface exposed by the system MUST reserve `stdout` exclusively for machine-readable payloads, route all human-readable diagnostics, progress logs, and usage output to `stderr`, and exit with coarse status codes `0` (completed execution, including when findings are present), `2` (invalid CLI arguments or inputs), or `3` (runtime or subprocess failure) per [ADR-0002](../../../../../adr/ADR-0002.md).

#### Scenario: CLI stdout remains machine-pure under debug logging and noisy subprocess output

- **GIVEN** any system CLI invoked with verbose diagnostics enabled while underlying analysis or repository build subprocesses emit banners, warnings, and progress messages
- **WHEN** the CLI terminates
- **THEN** `stdout` MUST contain exclusively the command's machine-readable payload with zero human prose leakage, all diagnostics and subprocess chatter MUST be routed only to `stderr` (or isolated task log artifacts), and the process exit code MUST conform to `0`, `2`, or `3` per [ADR-0002](../../../../../adr/ADR-0002.md).
