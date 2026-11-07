<!--
sdd_spec_id: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md
sdd_level: SYSTEM
sdd_implements:
  - docs/openspec/specs/journeys/pr-automated-remediation/spec.md
  - docs/openspec/specs/journeys/pr-on-demand-verify/spec.md
sdd_extends: docs/openspec/specs/tech/systems/codemender-runner/spec.md
-->

# Distributed Execution and Transit System Specification

**Implements Spec:** `docs/openspec/specs/journeys/pr-automated-remediation/spec.md`, `docs/openspec/specs/journeys/pr-on-demand-verify/spec.md` (see [User & Operator Journeys Index](../../../journeys/index.md))

**Extends Spec:** [CodeMender Runner System Specification](../codemender-runner/spec.md)

**Governing Journeys:**
- **Change Author (`pr-author`)**: As a developer awaiting scan findings and remediation patches, I want concurrent sandbox tasks to run quickly and reliably without corrupting one another's outputs or dropping diagnostics when a worker fails or times out.
- **Change Reviewer (`pr-reviewer`)**: As a reviewer triggering delayed or on-demand exploitability verification on an isolated execution host, I want prior finding context hydrated cleanly into a disposable sandbox that emits deterministic SARIF 2.1.0 verification outcomes.
- **Platform & Security Operator (`ci-operator`)**: As an operator deploying CodeMender across single-node pipelines, multi-node CI matrices, static worker pools, scheduled repository audits, or fleet-wide batch schedulers, I want symmetric transit semantics across local and remote object stores, safe archive extraction, and race-free fan-out/fan-in execution.

## Overview

The Distributed Execution and Transit system defines how repository snapshots, finding specifications, execution receipts, diagnostic logs, and workflow outputs move between outer orchestration hosts and isolated sandbox workers—and across distributed pipeline stages—spanning local container sandboxes, serverless cloud jobs, cluster runtimes, and batch schedulers across any repository hosting site or host-less deployment scenario.

This specification establishes the observable behavioral contracts for single-pass immutable repository snapshot staging, fail-fast transit backend parity, safe container-private scratch hydration, structured execution receipts and failure log recovery, cooperative sandbox cancellation, conflict-free per-task transit output namespaces, credential-less distributed sharding, and single-aggregator fan-in serialization.

## Requirements

### REQ-0001: Single-Pass Immutable Repository Snapshot Staging {#req-0001}

For a given workflow run or commit context, the orchestration layer MUST package and stage a sanitized, bounded-history repository archive into the ephemeral transit store at most once and reuse that immutable snapshot across subsequent discovery, concurrent remediation, and verification tasks within the same transit namespace.

#### Scenario: Subsequent concurrent tasks reuse a single staged repository archive

- **GIVEN** a discovery scan followed by multiple concurrent remediation or verification tasks sharing one run-scoped transit namespace
- **WHEN** the discovery scan and subsequent concurrent tasks execute
- **THEN** the orchestration layer MUST package and upload the repository snapshot once and every subsequent task MUST consume that staged snapshot read-only without re-archiving the repository.

### REQ-0002: Fail-Fast Transit Backend Parity {#req-0002}

All transit backends across local filesystem stores and remote object or shared stores MUST uphold identical relative key addressing, atomic completion visibility, and structured error reporting, and MUST fail fast with an explicit error on unsupported transit schemes, authentication failures, or unreachable storage without silently falling back to an alternate backend or stale cached artifacts.

#### Scenario: Misconfigured or unreachable transit backend fails immediately without fallback

- **GIVEN** a workflow run configured with an unsupported transit URI scheme, expired storage credentials, or an unreachable remote transit store
- **WHEN** the orchestration layer or sandbox worker attempts to read or write transit state
- **THEN** execution MUST terminate immediately with a structured error and non-zero failure status rather than silently falling back to another storage backend or stale local files.

### REQ-0003: Safe Container-Private Scratch Hydration {#req-0003}

Every sandbox worker invocation MUST validate incoming repository archives against path-traversal and out-of-root link escapes before extraction, unpack task inputs exclusively onto isolated container-private scratch storage rather than compiling or executing directly against a live caller checkout or network object-storage FUSE mount, preserve executable file modes required by repository build scripts, and isolate scratch mutations from sibling tasks.

#### Scenario: Archive containing path-escaping entries is rejected before extraction

- **GIVEN** a staged repository archive containing a relative `..` path, an absolute path, or a symbolic/hard link targeting a location outside the sandbox scratch directory
- **WHEN** a sandbox worker validates the archive prior to task execution
- **THEN** the worker MUST reject the archive with a structured failure receipt before writing any file outside its isolated container-private scratch directory.

### REQ-0004: Structured Execution Receipts and Failure Log Recovery {#req-0004}

Every sandbox worker execution MUST record a terminal structured execution receipt alongside captured diagnostic task logs when transit storage remains reachable, and when a worker crashes or is terminated before writing a receipt, the outer dispatcher MUST surface actionable failure diagnostics recovered from transit logs or runtime execution logs.

#### Scenario: Crashed sandbox worker without a receipt still yields captured failure diagnostics

- **GIVEN** a sandbox worker that crashes or is killed by an out-of-memory or runtime fault before writing its structured execution receipt
- **WHEN** the outer dispatcher observes the abnormal worker termination
- **THEN** the dispatcher MUST return a terminal failure outcome populated with actionable diagnostic output recovered from captured task or runtime execution logs.

### REQ-0005: Cooperative Sandbox Cancellation {#req-0005}

Upon task timeout or caller interrupt, the outer dispatcher MUST propagate cooperative cancellation to all active local sandbox containers and remote job executions so interrupted tasks stop explicitly instead of continuing silently in the background.

#### Scenario: Caller interrupt cancels in-flight local and remote sandbox executions

- **GIVEN** one or more active sandbox tasks running in local containers or remote cloud/cluster job executions
- **WHEN** the caller interrupts the orchestration run or a task exceeds its configured execution timeout
- **THEN** the dispatcher MUST issue explicit stop or cancel operations to the affected active sandbox executions before returning control to the caller.

### REQ-0006: Conflict-Free Per-Task Transit Output Namespaces {#req-0006}

Shared snapshot and finding inputs within a run transit store MUST remain read-only across concurrent remediation and verification workers, and every worker task MUST write its structured receipt, diagnostic log, and output artifacts strictly within a unique task-scoped output prefix so concurrent tasks and cross-machine directory merges produce zero key collisions or lost updates ([CodeMender Runner REQ-0003](../codemender-runner/spec.md#req-0003), [System Design](../codemender-runner/design.md)).

#### Scenario: Parallel workers and multi-node artifact merges preserve every task output intact

- **GIVEN** concurrent remediation and verification tasks executing in parallel on one host or across separate pipeline machines whose transit trees are subsequently merged into one directory or prefix
- **WHEN** all tasks finish writing their receipts, logs, unified diff patches, and updated SARIF 2.1.0 verification reports
- **THEN** shared snapshot and finding inputs MUST remain unmutated and every task's output artifacts MUST coexist without overwriting any sibling task's files.

### REQ-0007: Credential-Less Distributed Sharding with Clean Empty-Slice Exit {#req-0007}

Multi-stage distributed execution MUST support partitioning remediation or verification work across parallel compute shards—via either explicit finding batches or deterministic static shard indices (`1..N`)—that execute without repository-host write credentials and exit cleanly (`0`, no-op) whenever assigned an empty finding slice ([CodeMender Runner REQ-0001](../codemender-runner/spec.md#req-0001), [REQ-0003](../codemender-runner/spec.md#req-0003)).

#### Scenario: Static shard pool larger than the finding count exits cleanly on empty slices without host credentials

- **GIVEN** a multi-stage workflow configured with $N$ parallel compute shards without repository-host write credentials over fewer than $N$ findings (including zero findings)
- **WHEN** all $N$ compute shards execute their assigned slices
- **THEN** non-empty shards MUST write their remediation or verification outputs solely to the transit store without calling repository-host APIs, and shards assigned zero findings MUST exit with status `0` as a clean no-op.

### REQ-0008: Single-Aggregator Fan-In Serialization {#req-0008}

Whenever a repository-integrated workflow distributes remediation or verification across multiple parallel compute shards, external repository-host summary updates and aggregate merge/quality gate evaluations MUST be serialized through a single downstream fan-in aggregation stage that executes even when one or more upstream compute shards fail ([CodeMender Runner System Design](../codemender-runner/design.md)).

#### Scenario: Fan-in aggregator publishes partial shard results and records fail-closed gate status

- **GIVEN** a distributed change-review workflow in which one parallel compute shard succeeds with a valid remediation patch while another parallel compute shard fails
- **WHEN** the parallel shard stage completes and triggers the downstream fan-in aggregation stage
- **THEN** the single fan-in aggregator MUST still execute, publish the succeeded shard's results without concurrent summary races, and record a fail-closed aggregate quality gate status reflecting the failed shard.
