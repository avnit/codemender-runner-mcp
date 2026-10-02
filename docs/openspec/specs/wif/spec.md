<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-10-01' }
*-->

<!--
sdd_spec_id: SPEC-WIF
sdd_implements: None
sdd_extends: None
sdd_level: COMPONENT
sdd_archetype: INFRASTRUCTURE_TERRAFORM_MODULE
sdd_cujspec_product: codemender-runner
-->

# SPEC-WIF: Composable Mode-Aware Workload Identity Federation Module (`terraform/wif`)

**Implements Spec:** None

**Extends Spec:** None

## Overview

This specification defines the functional requirements, input variables,
validation rules, least-privilege IAM topologies, `terraform/gcp` composability
contract, GitHub repository bootstrap behavior, and verification test suite for
the unified Workload Identity Federation (WIF) Terraform module
(`terraform/wif/`).

Previously named `terraform/gha_wif/` and hardcoded solely for direct Vertex AI
invocation from GitHub Actions runners, `terraform/wif/` is a composable,
mode-aware identity and access module governed by `var.execution_mode`
(`"local"` or `"cloud_run"`):

1. **Local Execution Mode (`execution_mode = "local"`, default):** GitHub
   Actions runs `cm-runner` directly on the runner (`LocalDockerLauncher`) and
   calls Vertex AI Gemini models using the WIF-federated Service Account
   (`codemender-wif-sa`). The module enables `aiplatform.googleapis.com` and
   grants `roles/aiplatform.user` to the WIF Service Account without
   provisioning Cloud Run or GCS transit permissions.
1. **Cloud Run Execution Mode (`execution_mode = "cloud_run"`):** GitHub Actions
   acts purely as an orchestrator client that stages repository snapshots to a
   GCS transit bucket, triggers the remote Cloud Run Job
   (`gcloud run jobs execute --args ... --wait`), and downloads the resulting
   execution receipt and SARIF/patch artifacts. In this mode, the WIF Service
   Account is granted strictly the predefined roles required to stage transit
   objects (`roles/storage.objectUser`), execute and poll Cloud Run Jobs
   (`roles/run.developer`), and act as (`roles/iam.serviceAccountUser`) the
   Cloud Run Job's runtime Service Accounts—while omitting
   `roles/aiplatform.user` on the WIF caller Service Account in accordance with
   [ADR-0005](../../../adr/ADR-0005-multi-platform-transit-storage-and-isolation.md).

This specification is governed by and references:

- [ADR-0001: Machine-Readable Output Contract](../../../adr/ADR-0001-machine-readable-output-contract.md)
- [ADR-0004: Project Tech Stack & Scenario-Decoupled Monorepo Architecture](../../../adr/ADR-0004-project-tech-stack-and-repo-architecture.md)
- [ADR-0005: Multi-Platform Transit Storage & Sandboxed Isolation Model](../../../adr/ADR-0005-multi-platform-transit-storage-and-isolation.md)

## Requirements

### REQ-0001: Module Path Rename & Neutral Resource Naming Defaults {#req-0001}

- **CUJ UUID:** cuj-wif-module-rename

The repository MUST relocate the Workload Identity Federation Terraform module
from `terraform/gha_wif/` to `terraform/wif/` and update its default GCP
resource identifiers to neutral `codemender-wif-*` names:

1. **Module Directory:** All Terraform configuration files (`apis.tf`,
   `github.tf`, `iam.tf`, `outputs.tf`, `provider.tf`, `variables.tf`,
   `terraform.tfvars.example`, `.terraform.lock.hcl`, and
   `tests/wif_test.tftest.hcl`) MUST reside under `terraform/wif/`, and
   `terraform/gha_wif/` MUST be removed.
1. **Default Resource IDs:**
   - `var.gcp_service_account_id` MUST default to `"codemender-wif-sa"`.
   - `var.gcp_wif_pool_id` MUST default to `"codemender-wif-pool"`.
   - `var.gcp_wif_provider_id` MUST default to `"codemender-wif-provider"`.
1. **OIDC Pool, Provider & Attribute Scoping:**
   - The module MUST provision `google_iam_workload_identity_pool.github_pool`
     and `google_iam_workload_identity_pool_provider.github_provider`
     (`issuer_uri = "https://token.actions.githubusercontent.com"`) mapping
     `google.subject`, `attribute.actor`, `attribute.repository`, and
     `attribute.repository_owner`.
   - The module MUST support `var.github_scope_type` values of `"org"`,
     `"user"`, and `"repositories"`:
     - When `var.github_scope_type` is `"org"` or `"user"`, the provider
       `attribute_condition` MUST restrict assertions to
       `assertion.repository_owner == '<github_owner>'` and bind
       `roles/iam.workloadIdentityUser` on `google_service_account.runner_sa` to
       `principalSet://iam.googleapis.com/<pool_name>/attribute.repository_owner/<github_owner>`.
     - When `var.github_scope_type` is `"repositories"`, the provider
       `attribute_condition` MUST restrict assertions to
       `assertion.repository in <jsonencode(var.wif_allowed_repositories)>` and
       bind `roles/iam.workloadIdentityUser` on
       `google_service_account.runner_sa` per repository in
       `var.wif_allowed_repositories`.

#### Scenario: Default Resource Naming in `terraform/wif`

- **GIVEN** a caller initializes `terraform/wif` with only required variables
  (`gcp_project_id = "test-project"`, `github_owner = "test-org"`)
- **WHEN** `terraform plan` is executed
- **THEN** `google_service_account.runner_sa.account_id` is
  `"codemender-wif-sa"`,
  `google_iam_workload_identity_pool.github_pool.workload_identity_pool_id` is
  `"codemender-wif-pool"`, and
  `google_iam_workload_identity_pool_provider.github_provider.workload_identity_pool_provider_id`
  is `"codemender-wif-provider"`.

#### Scenario: Repository List Scoping Perimeter

- **GIVEN** `var.github_scope_type = "repositories"` and
  `var.wif_allowed_repositories = ["test-org/repo-a", "test-org/repo-b"]`
- **WHEN** `terraform plan` is executed
- **THEN** the OIDC provider `attribute_condition` is
  `assertion.repository in ["test-org/repo-a","test-org/repo-b"]`
- **AND** two `google_service_account_iam_member.wif_repo_binding` resources are
  planned while `google_service_account_iam_member.wif_user_org_binding` has
  count `0`.

______________________________________________________________________

### REQ-0002: Execution Mode Selection & Variable Validation {#req-0002}

- **CUJ UUID:** cuj-wif-execution-mode-validation

`terraform/wif/variables.tf` MUST expose a mode selector variable
`var.execution_mode` alongside Cloud Run integration variables with strict input
validation:

1. **`var.execution_mode`:**
   - Type: `string`, default: `"local"`.
   - Validation rule: MUST restrict values to `"local"` or `"cloud_run"`
     (`contains(["local", "cloud_run"], var.execution_mode)`). Any other value
     MUST fail Terraform validation with a descriptive error message.
1. **`var.gcs_transit_bucket_name`:**
   - Type: `string`, default: `""`.
   - Validation rule: When `var.execution_mode == "cloud_run"`,
     `var.gcs_transit_bucket_name` MUST be non-empty
     (`var.execution_mode != "cloud_run" || length(trimspace(var.gcs_transit_bucket_name)) > 0`).
1. **`var.cloud_run_runtime_sa_emails`:**
   - Type: `string` list (`list(string)`), default: `[]`.
   - Description: List of Cloud Run Job runtime Service Account email addresses
     (such as the orchestrator/runner SA and worker SA from `terraform/gcp`)
     that the WIF Service Account is authorized to act as
     (`roles/iam.serviceAccountUser`) when `var.execution_mode == "cloud_run"`.
1. **`var.cloud_run_job_name`:**
   - Type: `string`, default: `""`.
   - Description: Optional Cloud Run Job name (such as `runner_job_name` from
     `terraform/gcp`) to configure on target GitHub repositories when
     `var.execution_mode == "cloud_run"`.

#### Scenario: Rejecting Invalid `execution_mode`

- **GIVEN** `var.execution_mode = "gke"`
- **WHEN** `terraform plan` or `terraform test` is executed
- **THEN** Terraform fails input validation on `var.execution_mode`.

#### Scenario: Rejecting `cloud_run` Mode With Empty `gcs_transit_bucket_name`

- **GIVEN** `var.execution_mode = "cloud_run"` and
  `var.gcs_transit_bucket_name = ""`
- **WHEN** `terraform plan` or `terraform test` is executed
- **THEN** Terraform fails input validation on `var.gcs_transit_bucket_name`
  with an error stating that `gcs_transit_bucket_name` is required when
  `execution_mode` is `"cloud_run"`.

______________________________________________________________________

### REQ-0003: Local Execution Mode APIs & Least-Privilege Vertex AI IAM (`execution_mode = "local"`) {#req-0003}

- **CUJ UUID:** cuj-wif-local-mode-iam

When `var.execution_mode == "local"` (the default), `terraform/wif` MUST
provision only the GCP APIs and IAM bindings required for GitHub Actions runners
executing `cm-runner` locally (`LocalDockerLauncher`) against Vertex AI:

1. **Enabled GCP APIs (`apis.tf`):**
   - MUST organize APIs into function-specific local lists (`baseline_apis`,
     `codemender_apis`, `gcp_cloud_run_apis`) composed via `concat`.
   - MUST enable `cloudresourcemanager.googleapis.com`, `iam.googleapis.com`,
     `iamcredentials.googleapis.com`, and `aiplatform.googleapis.com`.
   - MUST NOT enable `run.googleapis.com` or `storage.googleapis.com`.
1. **Vertex AI IAM Binding (`iam.tf`):**
   - MUST create `google_project_iam_member.vertex_ai_user` granting
     `roles/aiplatform.user` on `var.gcp_project_id` to
     `serviceAccount:${google_service_account.runner_sa.email}`.
1. **Absence of Cloud Run & GCS Bindings (`iam.tf`):**
   - MUST NOT create the Cloud Run Job caller project IAM binding.
   - MUST NOT create the GCS transit bucket IAM binding.
   - MUST NOT create `roles/iam.serviceAccountUser` bindings on
     `var.cloud_run_runtime_sa_emails`.

#### Scenario: Local Mode Enables Vertex AI and Omits Cloud Run / GCS Permissions

- **GIVEN** `var.execution_mode = "local"` (default)
- **WHEN** `terraform plan` is executed
- **THEN** `google_project_service.enabled_apis` contains
  `aiplatform.googleapis.com` and does not contain `run.googleapis.com` or
  `storage.googleapis.com`
- **AND** `google_project_iam_member.vertex_ai_user` is planned with
  `role = "roles/aiplatform.user"`
- **AND** zero Cloud Run caller IAM, GCS transit bucket IAM, or runtime SA
  `actAs` resources are planned.

______________________________________________________________________

### REQ-0004: Cloud Run Execution Mode APIs & Predefined Caller IAM (`execution_mode = "cloud_run"`) {#req-0004}

- **CUJ UUID:** cuj-wif-cloud-run-mode-iam

When `var.execution_mode == "cloud_run"`, `terraform/wif` MUST provision the
exact APIs and predefined IAM bindings required for GitHub Actions to stage
repository snapshots to GCS, trigger and poll Cloud Run Job executions, and read
back receipts—without creating custom roles or granting direct Vertex AI access
to the WIF caller identity:

1. **Enabled GCP APIs (`apis.tf`):**
   - MUST enable `cloudresourcemanager.googleapis.com`, `iam.googleapis.com`,
     `iamcredentials.googleapis.com`, `run.googleapis.com`, and
     `storage.googleapis.com`.
   - MUST NOT enable `aiplatform.googleapis.com` in `terraform/wif`.
1. **No Direct Vertex AI Role on WIF Caller SA (`iam.tf`):**
   - MUST NOT create `google_project_iam_member.vertex_ai_user` (`count = 0`),
     because the Cloud Run Job's own runtime Service Account holds
     `roles/aiplatform.user`.
1. **Cloud Run Job Caller Predefined Role Binding (`iam.tf`):**
   - MUST bind the predefined role `roles/run.developer` to
     `serviceAccount:${google_service_account.runner_sa.email}` via
     `google_project_iam_member.cloud_run_job_caller_binding` so the caller can
     execute jobs with argument/volume overrides (`run.jobs.runWithOverrides`)
     and poll execution status (`run.executions.get`, `run.operations.get`).
1. **GCS Transit Bucket IAM Binding (`iam.tf`):**
   - MUST create `google_storage_bucket_iam_member.transit_bucket_access` on
     `bucket = var.gcs_transit_bucket_name` granting `roles/storage.objectUser`
     to `serviceAccount:${google_service_account.runner_sa.email}`.
1. **Cloud Run Runtime Service Account `actAs` Binding (`iam.tf`):**
   - For each Service Account email in `var.cloud_run_runtime_sa_emails`, MUST
     create `google_service_account_iam_member.cloud_run_runtime_sa_act_as`
     granting `roles/iam.serviceAccountUser` (`iam.serviceAccounts.actAs`) on
     `projects/${var.gcp_project_id}/serviceAccounts/${each.value}` to
     `serviceAccount:${google_service_account.runner_sa.email}`.

#### Scenario: Cloud Run Mode Provisions Predefined Caller Role, Transit Bucket Access, and Runtime SA `actAs` Without Vertex AI Role

- **GIVEN** `var.execution_mode = "cloud_run"`,
  `var.gcs_transit_bucket_name = "cm-transit-bucket"`, and
  `var.cloud_run_runtime_sa_emails = ["codemender-runner-sa@test-project.iam.gserviceaccount.com", "codemender-worker-sa@test-project.iam.gserviceaccount.com"]`
- **WHEN** `terraform plan` is executed
- **THEN** `google_project_service.enabled_apis` includes `run.googleapis.com`
  and `storage.googleapis.com` and excludes `aiplatform.googleapis.com`
- **AND** `google_project_iam_member.vertex_ai_user` has count `0`
- **AND** `google_project_iam_member.cloud_run_job_caller_binding[0]` grants
  `roles/run.developer` to `runner_sa`
- **AND** `google_storage_bucket_iam_member.transit_bucket_access[0]` grants
  `roles/storage.objectUser` on `"cm-transit-bucket"` to `runner_sa`
- **AND** `google_service_account_iam_member.cloud_run_runtime_sa_act_as` grants
  `roles/iam.serviceAccountUser` on both runtime Service Accounts to
  `runner_sa`.

______________________________________________________________________

### REQ-0005: Clean Module Boundary & Composability with `terraform/gcp` {#req-0005}

- **CUJ UUID:** cuj-wif-gcp-composability

`terraform/gcp` and `terraform/wif` MUST maintain a clean, unidirectional
dependency boundary with zero duplicate resources or circular dependencies:

1. **Resource Ownership in `terraform/gcp`:**
   - `terraform/gcp` MUST retain ownership of the Cloud Run Jobs
     (`google_cloud_run_v2_job.runner`), the GCS bucket
     (`google_storage_bucket.reports`), and the Cloud Run runtime Service
     Accounts (`google_service_account.runner_sa` and
     `google_service_account.worker_sa` with `roles/aiplatform.user`).
1. **Exported Runtime Service Account Outputs in `terraform/gcp/outputs.tf`:**
   - `terraform/gcp/outputs.tf` MUST export `runner_service_account_email`
     (`value = google_service_account.runner_sa.email`) and
     `worker_service_account_email`
     (`value = google_service_account.worker_sa.email`) alongside the existing
     `reports_bucket_name`, `runner_job_name`, and `region` outputs.
1. **Unidirectional Composition into `terraform/wif`:**
   - `terraform/wif` MUST consume `reports_bucket_name`,
     `runner_service_account_email`, `worker_service_account_email`, and
     `runner_job_name` as plain string/list input variables
     (`var.gcs_transit_bucket_name`, `var.cloud_run_runtime_sa_emails`,
     `var.cloud_run_job_name`) without requiring `terraform/gcp` to know about
     GitHub WIF pools or repositories.

#### Scenario: Composing `terraform/gcp` Outputs into `terraform/wif`

- **GIVEN** `terraform/gcp` is applied in a GCP project, producing outputs
  `reports_bucket_name`, `runner_service_account_email`,
  `worker_service_account_email`, and `runner_job_name`
- **WHEN** `terraform/wif` is configured with `execution_mode = "cloud_run"`,
  `gcs_transit_bucket_name = <reports_bucket_name>`,
  `cloud_run_runtime_sa_emails = [<runner_service_account_email>, <worker_service_account_email>]`,
  and `cloud_run_job_name = <runner_job_name>`
- **THEN** `terraform/wif` plans cleanly with zero circular dependencies or
  resource ownership conflicts.

______________________________________________________________________

### REQ-0006: Mode-Aware GitHub Repository Secrets, Variables & Label Bootstrap (`github.tf`) {#req-0006}

- **CUJ UUID:** cuj-wif-github-repo-bootstrap

When target GitHub repositories are configured (`local.target_repos`),
`terraform/wif/github.tf` MUST bootstrap the repository labels, secrets, and
mode-specific configuration required by the CodeMender GitHub Action:

1. **Common GitHub Resources (Both `local` and `cloud_run` Modes):**
   - `github_issue_label.codemender_scan`: Creates the `codemender-scan` label
     in each target repository.
   - `github_actions_secret.wif_provider`: Sets `GCP_WORKLOAD_IDENTITY_PROVIDER`
     to `google_iam_workload_identity_pool_provider.github_provider.name`.
   - `github_actions_secret.runner_sa`: Sets `GCP_SERVICE_ACCOUNT` to
     `google_service_account.runner_sa.email`.
   - `github_actions_secret.app_id`: Sets `GH_APP_ID` to `var.github_app_id`
     when `var.github_app_id != ""`.
1. **Cloud Run Mode GitHub Repository Variables
   (`execution_mode == "cloud_run"`):**
   - `github_actions_variable.gcs_transit_bucket`: Sets `GCS_TRANSIT_BUCKET` to
     `var.gcs_transit_bucket_name` in each target repository when
     `var.execution_mode == "cloud_run"`.
   - `github_actions_variable.cloud_run_job_name`: Sets `CLOUD_RUN_JOB_NAME` to
     `var.cloud_run_job_name` in each target repository when
     `var.execution_mode == "cloud_run"` and `var.cloud_run_job_name != ""`.
   - `github_actions_variable.gcp_region`: Sets `GCP_REGION` to `var.gcp_region`
     in each target repository when `var.execution_mode == "cloud_run"` and
     `var.gcp_region != ""` and `var.gcp_region != "global"`.

#### Scenario: Bootstrapping Cloud Run Variables in Target Repositories

- **GIVEN** `var.execution_mode = "cloud_run"`,
  `var.target_github_repositories = ["repo-a", "test-org/repo-b"]`,
  `var.gcs_transit_bucket_name = "cm-transit-bucket"`,
  `var.cloud_run_job_name = "codemender-runner"`, and
  `var.gcp_region = "us-central1"`
- **WHEN** `terraform plan` is executed
- **THEN** `github_actions_variable.gcs_transit_bucket`,
  `github_actions_variable.cloud_run_job_name`, and
  `github_actions_variable.gcp_region` are planned for both `"repo-a"` and
  `"repo-b"` alongside the standard WIF secrets and `codemender-scan` label.

______________________________________________________________________

### REQ-0007: Hermetic Terraform Tests & Repository Documentation Migration {#req-0007}

- **CUJ UUID:** cuj-wif-tests-and-docs

The module rename and multi-mode capabilities MUST be verified by hermetic
`terraform test` suites and reflected across all repository documentation:

1. **Hermetic Terraform Test Suite
   (`terraform/wif/tests/wif_test.tftest.hcl`):**
   - MUST pass `terraform test` offline using `mock_provider "google" {}` and
     `mock_provider "github" {}` covering at least four test runs:
     1. Default `execution_mode = "local"` (verifying `codemender-wif-*` default
        IDs, `roles/aiplatform.user`, org-scoped WIF binding, GitHub
        secrets/label, and zero Cloud Run/GCS bindings).
     1. `execution_mode = "cloud_run"` (verifying `run.googleapis.com` and
        `storage.googleapis.com` enabled, `roles/aiplatform.user` omitted on
        `runner_sa`, predefined `roles/run.developer` caller binding, GCS
        transit bucket `roles/storage.objectUser` binding, runtime SA
        `roles/iam.serviceAccountUser` bindings, and Cloud Run GitHub repository
        variables).
     1. Repository list scoping (`github_scope_type = "repositories"` and
        `wif_allowed_repositories` fallback).
     1. Validation failure (`expect_failures = [var.gcs_transit_bucket_name]`)
        when `execution_mode = "cloud_run"` and `gcs_transit_bucket_name = ""`.
1. **Documentation & Reference Migration:**
   - All references to `terraform/gha_wif` across `codemender-runner/README.md`,
     `docs/adr/ADR-0004-project-tech-stack-and-repo-architecture.md`, and
     `docs/guides/github_actions_guide.md` MUST be updated to `terraform/wif`.

#### Scenario: Running `terraform test` in `terraform/wif`

- **GIVEN** the `terraform/wif` module is initialized with
  `terraform init -backend=false`
- **WHEN** `terraform test` is executed in `terraform/wif`
- **THEN** all test runs in `tests/wif_test.tftest.hcl` pass with 0 failures.
