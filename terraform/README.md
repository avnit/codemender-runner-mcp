# Infrastructure Provisioning (`terraform/`)

> [!WARNING]
> **Work in Progress:** Detailed Terraform variable reference tables, module
> architecture, and authentication instructions are being consolidated into this
> guide from [`README.md`](../README.md) and
> [`.github/README.md`](../.github/README.md).

## Overview

The root `terraform/` configuration and child modules (`modules/wif`,
`modules/gcp`) provision keyless Workload Identity Federation, Google Cloud Run
Jobs, a GCS transit artifact bucket, least-privilege service accounts, and
optional GitHub repository secret/variable bootstrapping.

- **Specification:**
  [SPEC-WIF](../docs/openspec/specs/wif/spec.md).
- **Example Configuration:**
  [`terraform.tfvars.example`](terraform.tfvars.example).

## Execution Modes

The root module supports two execution modes via the `execution_mode` variable:

- `execution_mode = "cloud_run"` (default): Provisions Workload Identity
  Federation (`modules/wif`) plus Google Cloud Run Jobs and a GCS transit bucket
  (`modules/gcp`). See the
  [Cloud Run Setup Guide](../docs/user-guides/cloud-run.md).
- `execution_mode = "local"`: Provisions Workload Identity Federation and a
  least-privilege Vertex AI service account (`modules/wif`) for containers
  running on the CI runner VM under Docker + gVisor. See the
  [Local Docker + gVisor Setup Guide](../docs/user-guides/local-gvisor.md).

For step-by-step `terraform init` / `plan` / `apply` instructions while this
section is being consolidated, see
[Step 1 in `.github/README.md`](../.github/README.md#step-1--provision-google-cloud--workload-identity-federation-terraform).

## GitHub Authentication

Workload Identity Federation trust conditions are configured via
`github_scope_type` (`"org"`, `"user"`, or `"repositories"`), `github_owner`,
and `github_repositories`. For full authentication details, see
[Step 1 in `.github/README.md`](../.github/README.md#step-1--provision-google-cloud--workload-identity-federation-terraform)
and [`terraform.tfvars.example`](terraform.tfvars.example).

## Inputs and Outputs

See [`variables.tf`](variables.tf), [`outputs.tf`](outputs.tf), and
[Step 1](../.github/README.md#step-1--provision-google-cloud--workload-identity-federation-terraform)
and
[Step 2](../.github/README.md#step-2--configure-github-actions-secrets--variables)
in `.github/README.md` for input variable definitions, output values, and GitHub
Actions secret and variable mappings.
