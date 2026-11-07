# Architecture Decision Records (ADRs)

This directory contains the Architecture Decision Records (ADRs) governing the
CodeMender Orchestrator and its deployment scenarios.

| ADR | Title | Status | Date |
| :--- | :--- | :--- | :--- |
| [ADR-0001](ADR-0001.md) | Foundational Technical Stack, Build Tooling, and Engineering Conventions | Accepted | 2026-10-08 |
| [ADR-0002](ADR-0002.md) | Split-Stream Machine-Readable CLI Output and Exit Code Contract | Accepted | 2026-09-24 |
| [ADR-0003](ADR-0003-environment-scrubbing.md) | Environment Variable Scrubbing When Running `cm` | Accepted | 2026-09-30 |
| ADR-0004 | Project Tech Stack & Scenario-Decoupled Monorepo Architecture | Superseded by [ADR-0008](ADR-0008-repository-layout-and-package-structure.md) | 2026-09-26 |
| ADR-0005 | Ephemeral Archive Snapshot Transit and Container-Private Scratch Isolation | Superseded by [Distributed Execution & Transit](../openspec/specs/tech/systems/distributed-execution-and-transit/design.md) | 2026-09-26 |
| [ADR-0006](ADR-0006-pr-remediation-routing-and-security-gate.md) | Pull Request Remediation Routing & Security Quality Gate | Proposed | 2026-09-26 |
| ADR-0007 | Typed In-Process SDK Dispatch for Remote Sandbox Jobs | Superseded by [Distributed Execution & Transit](../openspec/specs/tech/systems/distributed-execution-and-transit/design.md) | 2026-10-02 |
| [ADR-0008](ADR-0008-repository-layout-and-package-structure.md) | Single Multi-Platform Repository with Independently Installable Packages | Accepted | 2026-10-08 |
| ADR-0009 | Decoupled Client/Server Sandbox Orchestration and Host-Agnostic Workflow Seams | Superseded by [CodeMender Runner](../openspec/specs/tech/systems/codemender-runner/design.md) | 2026-10-09 |
| ADR-0010 | Three-Zone Permission Isolation and Runtime AuthN/AuthZ Split | Superseded by [Security & Trust Zones](../openspec/specs/tech/systems/security-and-trust-zones/design.md) | 2026-10-14 |
