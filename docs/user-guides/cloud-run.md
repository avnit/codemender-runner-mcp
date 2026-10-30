# Step 2: Provision Google Cloud Run Jobs + GCS Cache (`gs://`)

> [!WARNING]
> **Work in Progress:** Detailed provisioning commands for Google Cloud Run Jobs
> are being consolidated into this guide from [`README.md`](../../README.md) and
> [`.github/README.md`](../../.github/README.md). Follow the three sections
> below and their linked instructions to complete Step 2, then return to
> [Step 3 — Enable CodeMender on Your
> Repositories](../../.github/README.md#step-3).

## Overview

In Cloud Run mode (`execution_mode = "cloud_run"`), the GitHub Actions workflow
authenticates to Google Cloud via keyless Workload Identity Federation, stages
the repository checkout archive in a Google Cloud Storage (`gs://`) transit
bucket, and dispatches isolated Google Cloud Run Jobs (`cm-worker` /
`cm-runner`) to run diff scans (`find-diff`), generate patches (`fix`), and
verify findings (`verify`).

For architecture specifications, see
[SPEC-GHA](../openspec/specs/github-actions/spec.md),
[SPEC-WIF](../openspec/specs/wif/spec.md), and
[SPEC-CACHE](../openspec/specs/artifact-cache/spec.md).

## 1. Provision GCP & Workload Identity (`execution_mode = "cloud_run"`)

Use the root [`terraform/`](../../terraform/README.md) configuration to
provision Workload Identity Federation, the GCS transit bucket, Cloud Run Jobs,
and least-privilege service accounts.

- **Terraform Reference Guides:**
  - [Execution Modes
    (`cloud_run`)](../../terraform/README.md#execution-modes)
  - [GitHub Authentication &
    WIF](../../terraform/README.md#github-authentication)
  - [Terraform Inputs and
    Outputs](../../terraform/README.md#inputs-and-outputs)
- **Step-by-Step Commands (in migration):** See
  [Step 1 in `.github/README.md`](../../.github/README.md#step-1--provision-google-cloud--workload-identity-federation-terraform).

## 2. Build & Publish the Cloud Run Worker Container Image

Build and push the `cm-runner` / `cm-worker` container image to Google Artifact
Registry so your Cloud Run Jobs can execute scans and fixes.

- **Container Packaging Reference Guides:**
  - [Building Images (`runner.Dockerfile` &
    `cloudrun.Dockerfile`)](../../docker/README.md#building-images)
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
> - `CODEMENDER_GCS_TRANSIT_BUCKET` (Variable, optional)
> - `CODEMENDER_CLOUD_RUN_JOB_NAME` (Variable, optional)
> - `CODEMENDER_GCP_REGION` (Variable, optional)
>
> **Next Step:**
> [Continue to Step 3 — Enable CodeMender on Your
> Repositories →](../../.github/README.md#step-3)
