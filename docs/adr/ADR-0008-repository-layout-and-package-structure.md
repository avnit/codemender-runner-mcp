# ADR-0008: Single Multi-Platform Repository with Independently Installable Packages

*   **Status:** accepted
*   **Date:** 2026-10-08
*   **Decision Makers:** CodeMender Maintainers

## Context and Problem Statement

CodeMender integrates automated code analysis, patch synthesis, and exploitability verification across diverse customer deployment scenarios—automated change reviews in CI/CD pipelines, scheduled repository audits, fleet-wide batch remediation campaigns, enterprise platform services, and standalone CLI runs—spanning any repository hosting site and pluggable sandbox execution backends across organization-wide, single-repository, self-hosted, and self-dogfooding adoption models. How should we structure repository distribution and internal package boundaries so customers can adopt one coherent release across scenarios and platforms without pulling privileged repository-host communication code or unconfigured provider dependencies into the sandboxed CodeMender runtime?

## Decision Drivers

*   **Sandbox Runtime Minimality and Trust Isolation:** Avoiding pulling repository-host communication code, credentialed host clients, or unused platform dependencies into the sandboxed CodeMender runtime environment where untrusted target code executes.
*   **Unified Multi-Scenario and Multi-Platform Distribution:** Giving adopters across CI/CD providers, repository hosting sites, batch schedulers, and sandbox compute backends a single canonical repository and release tag to fork, mirror, pin, or reference without cross-repository version skew.
*   **Scoped Installation Footprint:** Allowing each deployment scenario—including host-less batch, service, and standalone runs—to install only the internal distributions and third-party SDKs required for its execution role.
*   **Structural Evolvability:** Supporting new repository hosting adapters, durable reporting sinks, compute backends, or package splits and renames without locking the architecture into a fixed package count or multiplying separate repositories.

## Considered Options

*   Single multi-platform repository with multiple independently installable workspace packages
*   Separate repositories per hosting platform and core runtime
*   Single repository with a single monolithic package

## Decision Outcome

Chosen option: "Single multi-platform repository with multiple independently installable workspace packages", because a single repository eliminates cross-platform version skew across customer adoption models while decomposing the workspace into independently installable packages keeps repository-host communication code and unconfigured provider dependencies out of the sandboxed CodeMender runtime and host-less deployments. Concrete package definitions, optional-dependency boundaries, and repository layout are specified in the [CodeMender Runner System Design](../openspec/specs/tech/systems/codemender-runner/design.md).

### Consequences

*   Good, because the sandboxed CodeMender runtime installs only the packages required for isolated analysis and remediation, keeping repository-host communication code absent from untrusted execution environments.
*   Good, because customers fork, mirror, or pin a single repository and release tag across CI/CD workflows, enterprise automation, container definitions, and infrastructure provisioning definitions.
*   Good, because workspace packages can be added, split, or renamed—such as introducing dedicated persistence sinks or new host integrations—without changing customer repository topology.
*   Bad, because cross-cutting interface changes can span multiple workspace package manifests within the repository and require coordinated updates.

### Confirmation

Compliance is confirmed through container image builds, isolated single-scenario CI and standalone execution runs, and design and code reviews verifying that the sandboxed CodeMender runtime environment and individual customer deployment targets build and run with only their required workspace packages—without pulling in repository-host communication code or unconfigured hosting, storage, and cloud dependencies.

## Pros and Cons of the Options

### Single multi-platform repository with multiple independently installable workspace packages

Distribute all platform entrypoints, container definitions, infrastructure provisioning definitions, and independently installable workspace packages from one repository (see [CodeMender Runner System Design](../openspec/specs/tech/systems/codemender-runner/design.md)).

*   Good, because excludes repository-host communication clients and unused platform or cloud SDKs from both the sandboxed CodeMender runtime and host-less deployments.
*   Good, because atomic changes across core runtime logic, container definitions, provisioning definitions, and outer platform integrations land and release together without cross-repository synchronization.
*   Bad, because requires maintaining separate workspace package metadata and dependency declarations across internal distributions.

### Separate repositories per hosting platform and core runtime

Split platform integrations, infrastructure provisioning definitions, and core runtime code into independent per-platform repositories pinned to published core releases.

*   Good, because isolates platform-specific workflow definitions and release cadences into dedicated repositories.
*   Bad, because introduces cross-repository version skew and release coordination overhead whenever runtime contracts, container configurations, or provisioning inputs evolve together.
*   Bad, because forces enterprise customers spanning multiple platforms or deployment scenarios to fork, mirror, and govern multiple repositories instead of one canonical source.

### Single repository with a single monolithic package

Ship all runtime execution logic, transit coordination, durable persistence adapters, and repository-host communication clients inside one shared package in a single repository.

*   Good, because minimizes workspace packaging metadata to a single package manifest.
*   Bad, because installing the monolithic package into the sandboxed CodeMender runtime pulls repository-host communication code into the untrusted execution environment and couples unrelated hosting, storage, and cloud dependencies into one distribution.

## More Information

*   [CodeMender Runner System Specification](../openspec/specs/tech/systems/codemender-runner/spec.md) — observable system behavioral contracts for isolation and scenario-extensible execution.
*   [CodeMender Runner System Design](../openspec/specs/tech/systems/codemender-runner/design.md) — concrete workspace package map, optional-extra boundaries, and repository layout.
*   [ADR-0001: Foundational Technical Stack, Build Tooling, and Engineering Conventions](ADR-0001.md) — foundational Python workspace tooling and engineering conventions.
*   [Security and Trust Zones System Specification](../openspec/specs/tech/systems/security-and-trust-zones/spec.md) ([Design](../openspec/specs/tech/systems/security-and-trust-zones/design.md)) — privilege separation and credential boundaries reinforced by excluding repository-host communication code from sandboxed environments.
