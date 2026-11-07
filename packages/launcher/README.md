# `launcher` Package (Sandbox Backends & Transit Cache)

## Overview

`packages/launcher` provides the sandbox execution backends and artifact transit
storage abstractions that decouple CI orchestration from container execution
([CodeMender Runner System Design](../../docs/openspec/specs/tech/systems/codemender-runner/design.md)):

- **`launcher.base` (`Launcher`):** Abstract interface for executing `find-diff`,
  `fix`, and `verify` stages against an isolated sandbox backend.
- **`launcher.local` (`LocalDockerLauncher`):** Runs the `cm-runner` container
  directly on the CI runner VM under Docker with gVisor (`--runtime=runsc`)
  kernel isolation and read/write workspace bind mounts.
- **`launcher.cloudrun` (`CloudRunLauncher`, `CloudRunClient`):** Stages source
  archives and inputs to Google Cloud Storage (`gs://`) and dispatches isolated
  Google Cloud Run Jobs via container overrides.
- **`launcher.cache` (`ArtifactCache`, `LocalArtifactCache`, `GcsArtifactCache`, `RunManifest`, `ExecutionReceipt`):**
  Multi-platform transit storage interface and wire models supporting local
  filesystem directories (`file://`) and Google Cloud Storage buckets (`gs://`)
  per the [Distributed Execution and Transit System Design](../../docs/openspec/specs/tech/systems/distributed-execution-and-transit/design.md).

## Execution Backends & Setup Guides

- [Google Cloud Run Jobs + GCS Cache](../../docs/user-guides/cloud-run.md)
- [Local Docker + gVisor](../../docs/user-guides/local-gvisor.md)
- [Container Packaging (`docker/README.md`)](../../docker/README.md)

## Development & Testing

```bash
# Run the full test suite via Makefile (sets PYTHONPATH automatically):
make test

# Or after a one-time editable install in your virtualenv:
pip install -e ./packages/codemender -e ./packages/launcher -e "./packages/repohost[github,gitlab]" pytest setuptools
pytest packages/launcher
```

## Specifications & Architecture Decisions

- [SPEC-WORKER](../../docs/openspec/specs/worker/spec.md)
  — Worker container bootstrapper and task lifecycle specification.
- [SPEC-CACHE](../../docs/openspec/specs/artifact-cache/spec.md)
  — `ArtifactCache` transit storage contract across `file://` and `gs://`.
- [Distributed Execution and Transit System
  Design](../../docs/openspec/specs/tech/systems/distributed-execution-and-transit/design.md)
  — Multi-platform archive transit, scratch hydration, and remote job dispatch.
- [CodeMender Runner System
  Design](../../docs/openspec/specs/tech/systems/codemender-runner/design.md)
  — Abstraction layers, dependency injection seams, and asynchronous workflows.
