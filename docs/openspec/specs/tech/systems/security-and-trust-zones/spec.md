<!--
sdd_spec_id: docs/openspec/specs/tech/systems/security-and-trust-zones/spec.md
sdd_level: SYSTEM
sdd_implements:
  - docs/openspec/specs/journeys/pr-automated-remediation/spec.md
  - docs/openspec/specs/journeys/pr-on-demand-verify/spec.md
sdd_extends: docs/openspec/specs/tech/systems/codemender-runner/spec.md
-->

# Security and Trust Zones System Specification

**Implements Spec:** `docs/openspec/specs/journeys/pr-automated-remediation/spec.md`, `docs/openspec/specs/journeys/pr-on-demand-verify/spec.md` (see [User & Operator Journeys Index](../../../journeys/index.md))

**Extends Spec:** [CodeMender Runner System Specification](../codemender-runner/spec.md)

**Governing Journeys:**
- **Change Author (`pr-author`)**: As a developer submitting code changes on any repository hosting site, I want untrusted repository builds and candidate patches isolated so analyzed code cannot steal repository write credentials, pipeline secrets, or cloud tokens.
- **Change Reviewer (`pr-reviewer`)**: As a reviewer triggering on-demand exploitability verification, I want fail-closed caller authorization and unbypassable inner sandboxing when untrusted exploit payloads and repository test suites run.
- **Platform & Security Operator (`ci-operator`)**: As a security or platform operator deploying CodeMender across CI/CD pipelines, scheduled repository audits, fleet-wide batch campaigns, enterprise services, or developer hosts, I want least-privilege keyless workload identities, scrubbed workspace staging, and fail-closed gating before untrusted inputs execute.

## Overview

The Security and Trust Zones system defines the multi-zone privilege separation, launch-boundary environment and workspace hygiene, keyless workload identity isolation, and fail-closed untrusted-trigger gates protecting CodeMender execution across every customer deployment scenario, repository hosting site, and sandbox compute backend. Host-less deployments (such as scheduled audits, batch remediation campaigns, platform services, and standalone CLI runs) omit repository-host credentials while upholding every zone, sanitization, and workload-identity invariant defined below.

## Requirements

### REQ-0001: Three-Zone Credential and Outbound Network Isolation {#req-0001}

Runtime execution MUST be partitioned across three nested trust zones—`Zone 1 (Trusted Outer Host / Orchestrator)`, `Zone 2 (Worker / Sandbox Container)`, and `Zone 3 (CodeMender Native Sandbox)`—so `Zone 1` terminates all repository-host, identity-exchange, and durable-sink credentials without executing the CodeMender analysis engine or target repository code, `Zone 2` receives at most scoped model-inference and ephemeral-transit credentials with zero repository-host or durable-sink access, and `Zone 3` runs target repository compilers, build scripts, and verification tests with zero credentials mounted or inherited and with outbound network access disabled.

#### Scenario: Untrusted target repository build finds zero credentials and blocked egress inside Zone 3

- **GIVEN** a workflow run executing target repository build scripts or verification tests containing credential-discovery and outbound-network probes
- **WHEN** the target build or verification test runs inside `Zone 3 (CodeMender Native Sandbox)` beneath `Zone 2 (Worker / Sandbox Container)`
- **THEN** `Zone 3` MUST expose zero repository-host, durable-sink, transit, or model-inference credentials and MUST block external network connections, while `Zone 2` MUST contain zero repository-host or durable-sink credentials.

### REQ-0002: Launch-Boundary Environment Allowlisting and Out-of-Tree Credential Mounting {#req-0002}

Crossing from `Zone 1` into `Zone 2` MUST enforce an explicit configuration allowlist excluding unenumerated caller environment variables, mount any local workload-identity credential files strictly outside the unpacked repository tree as read-only and non-executable, remove temporary host credential artifacts upon workflow termination, and preserve configured sandbox toolchain and model-auth environment variables for the CodeMender engine while keeping the unpacked repository tree free of runner-created cache files ([ADR-0003](../../../../../adr/ADR-0003-environment-scrubbing.md)).

#### Scenario: Ambient host secrets are excluded from Zone 2 while sandbox toolchains stay intact and the repository stays clean

- **GIVEN** an outer host environment populated with repository-host tokens, CI runtime secrets, and unenumerated caller variables launching a local `Zone 2` sandbox container
- **WHEN** `Zone 1` spawns `Zone 2`, the CodeMender engine runs against the unpacked repository, and the workflow terminates
- **THEN** all non-allowlisted caller environment variables MUST be absent from `Zone 2` while configured sandbox toolchain and model-auth variables reach the CodeMender engine, local workload-identity credentials MUST remain read-only and non-executable strictly outside the unpacked repository tree with zero runner-created cache files inside that tree, and temporary host credential files MUST be deleted upon termination.

### REQ-0003: Untrusted Repository Checkout Sanitization {#req-0003}

Before staging any untrusted repository checkout for `Zone 2`, `Zone 1` MUST strip embedded version-control transport headers, credential helpers, userinfo URLs, executable hooks, and code-executing configuration directives, pin repository-level policy configuration exclusively to a trusted base revision while rejecting unmerged policy overrides, contain symlinks without dereferencing host paths outside the checkout root, and fail closed whenever required repository content relies on unmaterialized submodules or partial-clone remote fetches ([Distributed Execution and Transit System Specification](../distributed-execution-and-transit/spec.md)).

#### Scenario: Adversarial VCS metadata, unmerged policy overrides, and unmaterialized submodules are neutralized pre-launch

- **GIVEN** a target repository checkout containing embedded VCS auth headers, executable repository hooks, an unmerged policy override on the candidate revision, or an unmaterialized submodule dependency
- **WHEN** `Zone 1` prepares the repository snapshot for `Zone 2`
- **THEN** embedded auth headers, hooks, and unmerged policy overrides MUST be stripped before archiving, and any unmaterialized submodule or partial-clone dependency MUST abort snapshot preparation fail-closed before `Zone 2` starts.

### REQ-0004: Keyless Short-Lived Sandbox Authentication {#req-0004}

Cloud and cluster sandbox authentication MUST rely exclusively on short-lived keyless workload identity exchange and runtime-attached workload principals without provisioning long-lived service-account keys or injecting replayable presigned storage URLs or raw bearer tokens into sandbox job override metadata ([Distributed Execution and Transit System Specification](../distributed-execution-and-transit/spec.md)).

#### Scenario: Remote sandbox job execution authenticates without static keys or bearer tokens in override metadata

- **GIVEN** a workflow dispatching sandbox tasks to a remote cloud or cluster job runtime backed by object-storage transit
- **WHEN** the dispatched job override arguments, environment variables, and provisioned identity configuration are inspected
- **THEN** zero long-lived static credential keys, presigned storage URLs, or raw OAuth/bearer tokens MUST appear in provisioned secrets or job override metadata.

### REQ-0005: Coordinator and Worker Identity Separation {#req-0005}

Remote and distributed deployments MUST separate orchestration and sandbox execution across two distinct workload identities—a Coordinator identity bound to `Zone 1` and a Worker identity bound to `Zone 2`—such that neither identity can impersonate or assume the other, and MUST scope each identity to least privilege for its zone: the Coordinator identity MUST be limited to ephemeral transit staging and sandbox job dispatch (and MUST NOT invoke model inference, mutate sandbox job definitions or container images, or impersonate the Worker identity), and the Worker identity MUST be limited to model inference, log emission, and write-once (`create`/`read`) ephemeral transit access (and MUST NOT overwrite or delete existing transit objects or dispatch sandbox jobs). In single-host local runs where no remote dispatcher or remote transit store exists, the single sandbox identity attached to `Zone 2` MUST hold only model-inference permissions.

#### Scenario: Coordinator and Worker identities are denied cross-zone impersonation, escalation, and transit tampering

- **GIVEN** a remote sandbox deployment provisioned with distinct Coordinator (`Zone 1`) and Worker (`Zone 2`) workload identities, or a single-host local run provisioned with a single `Zone 2` sandbox identity
- **WHEN** a caller holding the Coordinator identity attempts to invoke model inference, mutate the sandbox job or container image, or impersonate the Worker, a process holding the Worker identity attempts to overwrite/delete transit objects or dispatch sandbox jobs, or the single-host `Zone 2` identity attempts remote transit or job-dispatch calls
- **THEN** IAM authorization MUST deny every cross-identity impersonation, job/image-mutation, unauthorized model-inference, job-dispatch, and transit overwrite/delete operation.

### REQ-0006: Pre-Execution Caller Authorization for Untrusted Triggers {#req-0006}

Before exchanging cloud workload credentials or dispatching any sandbox container for an untrusted change or interactive trigger on any repository hosting site, `Zone 1` MUST verify caller trust authorization and reject unauthorized external actors or untrusted-fork triggers unless explicitly configured under safe read-only fork policies.

#### Scenario: Unauthorized external trigger is rejected before credential exchange or sandbox launch

- **GIVEN** an interactive verification trigger or untrusted external fork change submitted by an account lacking authorized repository privileges without an explicit read-only fork policy enabled
- **WHEN** `Zone 1` evaluates pre-execution authorization
- **THEN** `Zone 1` MUST reject the request fail-closed before minting cloud workload credentials or spawning any `Zone 2` sandbox container.

### REQ-0007: Trusted-Source Runner Execution and Protected Pipeline Patch Downgrade {#req-0007}

When processing an untrusted change, `Zone 1` MUST execute exclusively trusted base-revision or immutable pinned runner workflow, action, and pre-built sandbox container definitions—MUST NOT execute or build candidate-revision runner workflows, container definitions, or infrastructure configurations from the untrusted change—and MUST downgrade any generated remediation patch touching protected pipeline or workflow configuration paths (or containing code-fence breakout markers) to non-committable patch output rather than offering committable inline edits ([ADR-0006](../../../../../adr/ADR-0006-pr-remediation-routing-and-security-gate.md)).

#### Scenario: Untrusted candidate pipeline or container edits are never executed and pipeline patches are downgraded

- **GIVEN** an untrusted change that modifies runner workflow, container, or infrastructure files or yields a generated remediation patch touching a protected pipeline or workflow configuration path
- **WHEN** `Zone 1` executes the runner workflow and publishes remediation output
- **THEN** `Zone 1` MUST run exclusively trusted base-revision or immutable pinned workflow, action, and pre-built sandbox container definitions without executing or building the candidate revision's workflow, container, or infrastructure files, and MUST publish any remediation patch touching protected pipeline or workflow configuration paths solely as non-committable patch output rather than a committable inline edit.
