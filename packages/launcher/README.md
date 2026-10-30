# `launcher` Package (Sandbox Backends & Worker Bootstrapper)

> [!WARNING]
> **Work in Progress:** Detailed `Launcher` backend interfaces, `ArtifactCache`
> transit storage contracts, and `cm-worker` container entrypoint documentation
> are being consolidated into this guide.

## Overview

`packages/launcher` provides the sandbox execution backends (`launcher.local`
for Local Docker + gVisor and `launcher.cloudrun` for Google Cloud Run Jobs),
the `ArtifactCache` transit storage abstraction (`file://` and `gs://`), and the
`cm-worker` container entrypoint bootstrapper.

## Execution Backends & Setup Guides

- [Google Cloud Run Jobs + GCS Cache](../../docs/user-guides/cloud-run.md)
- [Local Docker + gVisor](../../docs/user-guides/local-gvisor.md)
- [Calling `cm-runner`](../../docs/guides/calling_cm_runner.md#where-to-run-it)

## Specifications & Architecture Decisions

- [SPEC-WORKER](../../docs/openspec/specs/worker/spec.md)
  — `cm-worker` container bootstrapper and task lifecycle.
- [SPEC-CACHE](../../docs/openspec/specs/artifact-cache/spec.md)
  — `ArtifactCache` transit storage contract across `file://` and `gs://`.
- [ADR-0009: Abstraction Layers &
  DI](../../docs/adr/ADR-0009-abstraction-layers-and-dependency-injection.md)
  — Abstraction layers, orchestration ownership, and dependency injection.
- [ADR-0005: Multi-Platform Transit
  Storage](../../docs/adr/ADR-0005-multi-platform-transit-storage-and-isolation.md)
  — Multi-platform transit storage and isolation.
