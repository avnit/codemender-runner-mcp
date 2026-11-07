# Technical Systems

This directory contains specifications defining the high-level technical systems
(`sdd_level: SYSTEM`) of `codemender-runner`.

## Active Systems

| System | Specification | Design | Primary Focus |
| :--- | :--- | :--- | :--- |
| **CodeMender Runner** | [codemender-runner/spec.md](codemender-runner/spec.md) | [codemender-runner/design.md](codemender-runner/design.md) | Top-level multi-package architecture, scenario- and host-agnostic workflow seams, ephemeral-versus-durable storage separation, progressive completion-order streaming, and split-stream CLI contracts across every customer deployment scenario. |
| **Distributed Execution & Transit** | [distributed-execution-and-transit/spec.md](distributed-execution-and-transit/spec.md) | [distributed-execution-and-transit/design.md](distributed-execution-and-transit/design.md) | Bounded-history snapshot staging, local and object-store transit parity, container-private scratch hydration, typed remote SDK dispatch and cancellation, conflict-free task namespaces, and progressive streaming alongside distributed fan-out/fan-in topologies. |
| **Security & Trust Zones** | [security-and-trust-zones/spec.md](security-and-trust-zones/spec.md) | [security-and-trust-zones/design.md](security-and-trust-zones/design.md) | Three-zone privilege isolation (`Zone 1: Trusted Outer Host / Orchestrator`, `Zone 2: Worker / Sandbox Container`, `Zone 3: CodeMender Native Sandbox`), launch-boundary environment and checkout scrubbing, keyless coordinator-versus-worker identity separation, and fail-closed trigger gating. |
