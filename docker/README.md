# Container Packaging (`docker/`)

> [!WARNING]
> **Work in Progress:** Detailed container build and registry publication
> instructions are being consolidated into this guide from
> [`README.md`](../README.md) and [`.github/README.md`](../.github/README.md).

## Overview

The `docker/` directory contains the container image definitions for running
CodeMender across Local Docker + gVisor, Google Cloud Run Jobs, and GitLab CI:

- [`runner.Dockerfile`](runner.Dockerfile) — Base `cm-runner` / `cm-worker`
  container image pre-baked with Python, Node.js, OpenJDK, Go, and C/C++
  toolchains.
- [`cloudrun.Dockerfile`](cloudrun.Dockerfile) — Cloud Run Jobs wrapper image.
- **Specification:**
  [SPEC-CONTAINER](../docs/openspec/specs/runner-container-packaging/spec.md).

## Building Images

To build and publish the runner container image manually with Docker, Google
Cloud Build (`make deploy-cloudrun`), or GitHub Container Registry (`ghcr.io`),
refer to:

- [Step 3 in `.github/README.md`](../.github/README.md#step-3--build--publish-the-cm-runner-container-image)

For end-to-end execution environment setup guides, see:

- [Google Cloud Run Jobs + GCS Cache](../docs/user-guides/cloud-run.md)
- [Local Docker + gVisor](../docs/user-guides/local-gvisor.md)
