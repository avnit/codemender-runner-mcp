# Step 2: Provision Local Docker + gVisor (`runsc`)

> [!WARNING]
> **Work in Progress:** Detailed provisioning commands for Local Docker + gVisor
> execution are being consolidated into this guide from
> [`README.md`](../../README.md) and
> [`.github/README.md`](../../.github/README.md). Follow the three sections
> below and their linked instructions to complete Step 2, then return to
> [Step 3 — Enable CodeMender on Your
> Repositories](../../.github/README.md#step-3).

## Overview

In Local Docker + gVisor mode (`execution_mode = "local"`), the `cm-runner`
container runs directly on the CI runner VM under Docker with gVisor (`runsc`)
kernel sandboxing and a local filesystem artifact cache (`file://`), using
keyless Google Cloud Workload Identity Federation to call Vertex AI.

For architecture specifications, see
[SPEC-GHA](../openspec/specs/github-actions/spec.md),
[SPEC-WIF](../openspec/specs/wif/spec.md), and
[SPEC-CACHE](../openspec/specs/artifact-cache/spec.md).

## 1. Provision Workload Identity Federation (`execution_mode = "local"`)

Use the root [`terraform/`](../../terraform/README.md) configuration with
`execution_mode = "local"` to provision keyless Workload Identity Federation and
a least-privilege Vertex AI service account without creating Cloud Run
resources.

- **Terraform Reference Guides:**
  - [Execution Modes
    (`local`)](../../terraform/README.md#execution-modes)
  - [GitHub Authentication &
    WIF](../../terraform/README.md#github-authentication)
  - [Terraform Inputs and
    Outputs](../../terraform/README.md#inputs-and-outputs)
- **Step-by-Step Commands (in migration):** See
  [Step 1 in `.github/README.md`](../../.github/README.md#step-1--provision-google-cloud--workload-identity-federation-terraform).

## 2. Build & Publish the `cm-runner` Container Image

Build the `cm-runner` container image (or use the published GitHub Container
Registry image `ghcr.io/<owner>/codemender-runner:latest`).

- **Container Packaging Reference Guides:**
  - [Building Images
    (`runner.Dockerfile`)](../../docker/README.md#building-images)
- **Step-by-Step Commands (in migration):** See
  [Step 3 in `.github/README.md`](../../.github/README.md#step-3--build--publish-the-cm-runner-container-image).

## 3. Configure GitHub Actions Secrets & Variables

If you did not populate `github_repositories` in `terraform.tfvars` for
automatic secret/variable bootstrapping, configure the required secrets and
variables in your GitHub Organization or repository settings (see the full table
in
[Step 2 of `.github/README.md`](../../.github/README.md#step-2--configure-github-actions-secrets--variables)).

> [!IMPORTANT]
> **Configuration Outputs Checklist & Next Step**
>
> Before continuing to Step 3, confirm that the following GitHub Actions secrets
> and variables are configured in your organization or repository:
>
> - `CODEMENDER_WORKLOAD_IDENTITY_PROVIDER` (Secret)
> - `CODEMENDER_SERVICE_ACCOUNT` (Secret)
> - `CODEMENDER_GCP_PROJECT_ID` (Variable)
> - `CODEMENDER_RUNNER_IMAGE` (Variable, optional; defaults to
>   `ghcr.io/<action-or-workflow-repo>:latest`)
>
> **Next Step:**
> [Continue to Step 3 — Enable CodeMender on Your
> Repositories →](../../.github/README.md#step-3)
