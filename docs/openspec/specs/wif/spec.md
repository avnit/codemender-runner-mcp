<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-10-02' }
*-->

<!--
sdd_spec_id: SPEC-WIF
sdd_implements: None
sdd_extends: None
sdd_level: COMPONENT
sdd_archetype: INFRASTRUCTURE_TERRAFORM_MODULE
sdd_cujspec_product: codemender-runner
-->

# SPEC-WIF: Unified Terraform Root & Composable Mode-Aware WIF Module (`terraform/` & `terraform/modules/{wif,gcp}`)

**Implements Spec:** None

**Extends Spec:** None

## Overview

This specification defines the functional requirements, root-and-child module
hierarchy, input variables, validation rules, least-privilege IAM topologies,
automatic `terraform/modules/gcp` composability contract, GitHub provider
authentication modes, repository bootstrap behavior, and verification test
suites for the unified Terraform root module (`terraform/`) and its child
modules (`terraform/modules/wif/` and `terraform/modules/gcp/`).

Summary: `terraform/` serves as the single root entrypoint where operators run
`terraform init`, `terraform plan`, `terraform apply`, and `terraform test`
with a single `terraform/terraform.tfvars` file. Under the hood, root
`terraform/` composes `terraform/modules/wif/` (Workload Identity Federation
and optional GitHub repository secret/variable/label bootstrap) and, when
`var.execution_mode == "cloud_run"`, `terraform/modules/gcp/` (Cloud Run Jobs,
GCS transit/reports bucket, and runtime Service Accounts).

Governed by `var.execution_mode` (`"local"` or `"cloud_run"`):

1. **Local Execution Mode (`execution_mode = "local"`, default):** GitHub
   Actions runs `cm-runner` directly on the runner (`LocalDockerLauncher`) and
   calls Vertex AI Gemini models using the WIF-federated Service Account
   (`codemender-wif-sa`). `terraform/modules/wif` enables
   `aiplatform.googleapis.com` and grants `roles/aiplatform.user` to the WIF
   Service Account without provisioning Cloud Run infrastructure or GCS transit
   permissions.
1. **Cloud Run Execution Mode (`execution_mode = "cloud_run"`):** Root
   `terraform/` automatically provisions `module.gcp[0]`
   (`terraform/modules/gcp`) and wires its outputs (`reports_bucket_name`,
   `runner_job_name`, `runner_service_account_email`) into `module.wif` (`terraform/modules/wif`).
   GitHub Actions acts purely as an orchestrator client that stages repository
   snapshots to the GCS transit bucket, triggers the remote Cloud Run Job
   (`gcloud run jobs execute --args ... --wait`), and downloads the resulting
   execution receipt and SARIF/patch artifacts. In this mode, the WIF Service
   Account is granted strictly the predefined roles required to stage transit
   objects (`roles/storage.objectUser`), execute and poll Cloud Run Jobs
   (`roles/run.developer`), and act as (`roles/iam.serviceAccountUser`) the
   Cloud Run Job's runtime Service Account—while omitting
   `roles/aiplatform.user` on the WIF caller Service Account in accordance with
   the [Distributed Execution and Transit System Design](../tech/systems/distributed-execution-and-transit/design.md).

This specification is governed by and references:

- [ADR-0002: Machine-Readable Output Contract](../../../adr/ADR-0002.md)
- [ADR-0008: Repository Layout, Three-Package Workspace, and Runtime Architecture](../../../adr/ADR-0008-repository-layout-and-package-structure.md)
- [Distributed Execution and Transit System Design](../tech/systems/distributed-execution-and-transit/design.md)

## Requirements

### REQ-0001: Root `terraform/` Entrypoint, `terraform/modules/{wif,gcp}` Child Hierarchy & Single `github_repositories` List {#req-0001}

- **CUJ UUID:** cuj-wif-module-rename

The repository MUST organize its Terraform configurations into a single root
entrypoint at `terraform/` and reusable child modules under
`terraform/modules/wif/` and `terraform/modules/gcp/`, with neutral
`codemender-wif-*` default GCP resource identifiers and a single
`github_repositories` list variable:

1. **Root & Child Module Directory Hierarchy:**
   - `terraform/` MUST be the single root Terraform module containing
     `main.tf`, `gcp.tf`, `gcp_override.tf`, `variables.tf`, `outputs.tf`,
     `provider.tf`, `terraform.tfvars.example`, `.terraform.lock.hcl`,
     `tests/root_test.tftest.hcl`, and `tests/root_gcp_test.tftest.hcl`.
   - Child modules MUST reside at `terraform/modules/wif/` (`apis.tf`,
     `github.tf`, `iam.tf`, `outputs.tf`, `provider.tf`, `variables.tf`,
     `tests/wif_test.tftest.hcl`) and `terraform/modules/gcp/` (`apis.tf`,
     `compute.tf`, `iam.tf`, `outputs.tf`, `provider.tf`,
     `storage.tf`, `variables.tf`, `vpc.tf`, `tests/*.tftest.hcl`). Legacy
     top-level `terraform/gha_wif/`, `terraform/wif/`, and `terraform/gcp/`
     directories MUST NOT exist.
   - Child modules (`terraform/modules/wif/` and `terraform/modules/gcp/`) MUST
     NOT contain concrete `provider "..." {}` blocks; they MUST only declare
     `terraform { required_version = ">= 1.5.0" required_providers { ... } }`
     so the root module can compose them using `count` and `for_each`. Concrete
     `provider "google"`, `provider "google-beta"`, and `provider "github"`
     configuration blocks MUST reside exclusively in root
     `terraform/provider.tf`.
1. **Default Resource IDs:**
   - `var.gcp_service_account_id` MUST default to `"codemender-wif-sa"`.
   - `var.gcp_wif_pool_id` MUST default to `"codemender-wif-pool"`.
   - `var.gcp_wif_provider_id` MUST default to `"codemender-wif-provider"`.
1. **Single Repository List (`var.github_repositories`) With Zero Fallbacks &
   Automatic Normalization:**
   - Both `terraform/variables.tf` and `terraform/modules/wif/variables.tf`
     MUST define a single `var.github_repositories` (`list(string)`,
     `default = []`) variable and MUST NOT define `wif_allowed_repositories` or
     `target_github_repositories` or any fallback logic between multiple list
     variables.
   - Entries in `var.github_repositories` MAY be formatted as either `"repo"`
     or `"owner/repo"`. `terraform/modules/wif` MUST normalize each entry `r`
     using `var.github_owner` into:
     - `local.full_repo_names`
       (`[for r in var.github_repositories : strcontains(trimspace(r), "/") ? trimspace(r) : "${var.github_owner}/${trimspace(r)}"]`)
       for GCP WIF OIDC `attribute_condition` (`assertion.repository in ...`)
       and per-repository `roles/iam.workloadIdentityUser` bindings
       (`google_service_account_iam_member.wif_repo_binding`).
     - `local.short_repo_names`
       (`[for r in var.github_repositories : element(reverse(split("/", trimspace(r))), 0)]`)
       for GitHub repository secrets, variables, and labels (`github.tf`).
1. **OIDC Pool, Provider & Attribute Scoping:**
   - `terraform/modules/wif` MUST provision
     `google_iam_workload_identity_pool.github_pool` and
     `google_iam_workload_identity_pool_provider.github_provider`
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
       `assertion.repository in <jsonencode(local.full_repo_names)>` and bind
       `roles/iam.workloadIdentityUser` on `google_service_account.runner_sa`
       per repository in `local.full_repo_names`.

#### Scenario: Default Resource Naming in Root `terraform/` and `terraform/modules/wif`

- **GIVEN** a caller initializes `terraform/` (or `terraform/modules/wif`) with
  only required variables (`gcp_project_id = "test-project"`,
  `github_owner = "test-org"`)
- **WHEN** `terraform plan` is executed
- **THEN** `google_service_account.runner_sa.account_id` is
  `"codemender-wif-sa"`,
  `google_iam_workload_identity_pool.github_pool.workload_identity_pool_id` is
  `"codemender-wif-pool"`, and
  `google_iam_workload_identity_pool_provider.github_provider.workload_identity_pool_provider_id`
  is `"codemender-wif-provider"`.

#### Scenario: Single `github_repositories` List Normalizes Mixed `"repo"` and `"owner/repo"` Entries

- **GIVEN** `var.github_owner = "test-org"`,
  `var.github_scope_type = "repositories"`, and
  `var.github_repositories = ["repo-a", "test-org/repo-b"]`
- **WHEN** `terraform plan` is executed
- **THEN** the OIDC provider `attribute_condition` is
  `assertion.repository in ["test-org/repo-a","test-org/repo-b"]`
- **AND** two `google_service_account_iam_member.wif_repo_binding` resources
  are planned for `"test-org/repo-a"` and `"test-org/repo-b"` while
  `google_service_account_iam_member.wif_user_org_binding` has count `0`
- **AND** GitHub repository resources (`github.tf`) are keyed by the short
  names `"repo-a"` and `"repo-b"`.

______________________________________________________________________

### REQ-0002: Execution Mode Selection, Single-List Validation & GitHub Provider Authentication {#req-0002}

- **CUJ UUID:** cuj-wif-execution-mode-validation

Root `terraform/variables.tf` and child `terraform/modules/wif/variables.tf`
MUST expose `var.execution_mode`, repository scoping validation, Cloud Run
integration inputs, and flexible GitHub provider authentication variables:

1. **`var.execution_mode`:**
   - Type: `string`, default: `"local"`.
   - Validation rule: MUST restrict values to `"local"` or `"cloud_run"`
     (`contains(["local", "cloud_run"], var.execution_mode)`). Any other value
     MUST fail Terraform validation with a descriptive error message.
1. **`var.github_repositories` Validation:**
   - Type: `list(string)`, default: `[]`.
   - Validation rule (in both `terraform/variables.tf` and
     `terraform/modules/wif/variables.tf`): When
     `var.github_scope_type == "repositories"`, `var.github_repositories` MUST
     contain at least one repository
     (`var.github_scope_type != "repositories" || length(var.github_repositories) > 0`).
     When `var.github_scope_type` is `"org"` or `"user"`,
     `var.github_repositories` MAY be empty (`[]`).
1. **Cloud Run Integration Variables & Child Validation:**
   - In root `terraform/variables.tf`:
     - `var.gcp_region` MUST default to `"us-central1"`.
     - `var.gcs_transit_bucket_name` (`string`, `default = ""`),
       `var.cloud_run_job_name` (`string`, `default = ""`), and
       `var.cloud_run_runtime_sa_emails` (`list(string)`, `default = []`) MUST
       be optional overrides so operators running `execution_mode = "cloud_run"`
       from root `terraform/` do not have to set them manually in
       `terraform.tfvars` when `module.gcp[0]` is present.
   - In child `terraform/modules/wif/variables.tf`:
     - `var.gcs_transit_bucket_name` (`string`, `default = ""`) MUST validate
       that when `var.execution_mode == "cloud_run"`,
       `var.gcs_transit_bucket_name` is non-empty
       (`var.execution_mode != "cloud_run" || length(trimspace(var.gcs_transit_bucket_name)) > 0`).
     - `var.cloud_run_runtime_sa_emails` (`list(string)`, `default = []`) and
       `var.cloud_run_job_name` (`string`, `default = ""`) MUST be defined for
       Cloud Run IAM and GitHub repository variable provisioning.
1. **Root GitHub Provider Authentication (`terraform/variables.tf` &
   `terraform/provider.tf`):**
   - Root `terraform/variables.tf` MUST define:
     - `var.github_app_id` (`string`, `default = ""`)
     - `var.github_app_installation_id` (`string`, `default = ""`)
     - `var.github_app_pem_file` (`string`, `default = ""`, `sensitive = true`)
     - `var.github_mgmt_token` (`string`, `default = null`, `sensitive = true`)
   - Root `terraform/provider.tf` MUST compute
     `local.use_github_app_auth = (var.github_app_id != "" && var.github_app_installation_id != "" && var.github_app_pem_file != "")`
     and configure `provider "github"` with:
     - `owner = var.github_owner != "" ? var.github_owner : null`
     - `token = !local.use_github_app_auth && var.github_mgmt_token != null && var.github_mgmt_token != "" ? var.github_mgmt_token : null`
     - A conditional `dynamic "app_auth"` block
       (`for_each = local.use_github_app_auth ? [1] : []`) setting
       `id = var.github_app_id`,
       `installation_id = var.github_app_installation_id`, and
       `pem_file = fileexists(pathexpand(trimspace(var.github_app_pem_file))) ? file(pathexpand(trimspace(var.github_app_pem_file))) : var.github_app_pem_file`.
   - Authentication precedence MUST be:
     1. GitHub App `app_auth` when `github_app_id`,
        `github_app_installation_id`, and `github_app_pem_file` are all
        non-empty.
     1. Explicit `github_mgmt_token` when `local.use_github_app_auth` is
        `false` and `github_mgmt_token` is non-empty.
     1. Ambient `gh auth login` session or `GITHUB_TOKEN` environment variable
        when both are omitted.

#### Scenario: Rejecting Invalid `execution_mode`

- **GIVEN** `var.execution_mode = "gke"`
- **WHEN** `terraform plan` or `terraform test` is executed
- **THEN** Terraform fails input validation on `var.execution_mode`.

#### Scenario: Rejecting Empty `github_repositories` When `github_scope_type = "repositories"`

- **GIVEN** `var.github_scope_type = "repositories"` and
  `var.github_repositories = []`
- **WHEN** `terraform plan` or `terraform test` is executed
- **THEN** Terraform fails input validation on `var.github_repositories`.

#### Scenario: Rejecting `cloud_run` Mode With Empty `gcs_transit_bucket_name` in `terraform/modules/wif`

- **GIVEN** `var.execution_mode = "cloud_run"` and
  `var.gcs_transit_bucket_name = ""` in `terraform/modules/wif`
- **WHEN** `terraform plan` or `terraform test` is executed
- **THEN** Terraform fails input validation on `var.gcs_transit_bucket_name`
  with an error stating that `gcs_transit_bucket_name` is required when
  `execution_mode` is `"cloud_run"`.

#### Scenario: Selecting GitHub App Provider Authentication

- **GIVEN** `var.github_app_id = "12345"`,
  `var.github_app_installation_id = "67890"`, and
  `var.github_app_pem_file = "MOCK_PEM"` in root `terraform/`
- **WHEN** `terraform plan` or `terraform test` evaluates root locals
- **THEN** `local.use_github_app_auth` evaluates to `true` and
  `provider "github"` configures the `app_auth` block while setting
  `token = null`.

______________________________________________________________________

### REQ-0003: Local Execution Mode APIs & Least-Privilege Vertex AI IAM (`execution_mode = "local"`) {#req-0003}

- **CUJ UUID:** cuj-wif-local-mode-iam

When `var.execution_mode == "local"` (the default), `terraform/modules/wif`
MUST provision only the GCP APIs and IAM bindings required for GitHub Actions
runners executing `cm-runner` locally (`LocalDockerLauncher`) against Vertex AI:

1. **Enabled GCP APIs (`terraform/modules/wif/apis.tf`):**
   - MUST organize APIs into function-specific local lists (`baseline_apis`,
     `codemender_apis`, `gcp_cloud_run_apis`) composed via `concat`.
   - MUST enable `cloudresourcemanager.googleapis.com`, `iam.googleapis.com`,
     `iamcredentials.googleapis.com`, and `aiplatform.googleapis.com`.
   - MUST NOT enable `run.googleapis.com` or `storage.googleapis.com`.
1. **Vertex AI IAM Binding (`terraform/modules/wif/iam.tf`):**
   - MUST create `google_project_iam_member.vertex_ai_user` granting
     `roles/aiplatform.user` on `var.gcp_project_id` to
     `serviceAccount:${google_service_account.runner_sa.email}`.
1. **Absence of Cloud Run & GCS Bindings (`terraform/modules/wif/iam.tf`):**
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

When `var.execution_mode == "cloud_run"`, `terraform/modules/wif` MUST
provision the exact APIs and predefined IAM bindings required for GitHub
Actions to stage repository snapshots to GCS, trigger and poll Cloud Run Job
executions, and read back receipts—without creating custom roles or granting
direct Vertex AI access to the WIF caller identity:

1. **Enabled GCP APIs (`terraform/modules/wif/apis.tf`):**
   - MUST enable `cloudresourcemanager.googleapis.com`, `iam.googleapis.com`,
     `iamcredentials.googleapis.com`, `run.googleapis.com`, and
     `storage.googleapis.com`.
   - MUST NOT enable `aiplatform.googleapis.com` in `terraform/modules/wif`.
1. **No Direct Vertex AI Role on WIF Caller SA (`terraform/modules/wif/iam.tf`):**
   - MUST NOT create `google_project_iam_member.vertex_ai_user` (`count = 0`),
     because the Cloud Run Job's own runtime Service Account holds
     `roles/aiplatform.user`.
1. **Cloud Run Job Caller Predefined Role Binding (`terraform/modules/wif/iam.tf`):**
   - MUST bind the predefined role `roles/run.developer` to
     `serviceAccount:${google_service_account.runner_sa.email}` via
     `google_project_iam_member.cloud_run_job_caller_binding` so the caller can
     execute jobs with argument/volume overrides (`run.jobs.runWithOverrides`)
     and poll execution status (`run.executions.get`, `run.operations.get`).
1. **GCS Transit Bucket IAM Binding (`terraform/modules/wif/iam.tf`):**
   - MUST create `google_storage_bucket_iam_member.transit_bucket_access` on
     `bucket = var.gcs_transit_bucket_name` granting `roles/storage.objectUser`
     to `serviceAccount:${google_service_account.runner_sa.email}`.
1. **Cloud Run Runtime Service Account `actAs` Binding (`terraform/modules/wif/iam.tf`):**
   - For each Service Account email in `var.cloud_run_runtime_sa_emails`, MUST
     create `google_service_account_iam_member.cloud_run_runtime_sa_act_as`
     granting `roles/iam.serviceAccountUser` (`iam.serviceAccounts.actAs`) on
     `projects/${var.gcp_project_id}/serviceAccounts/${each.value}` to
     `serviceAccount:${google_service_account.runner_sa.email}`.

#### Scenario: Cloud Run Mode Provisions Predefined Caller Role, Transit Bucket Access, and Runtime SA `actAs` Without Vertex AI Role

- **GIVEN** `var.execution_mode = "cloud_run"`,
  `var.gcs_transit_bucket_name = "cm-transit-bucket"`, and
  `var.cloud_run_runtime_sa_emails = ["codemender-runner-sa@test-project.iam.gserviceaccount.com"]`
- **WHEN** `terraform plan` is executed
- **THEN** `google_project_service.enabled_apis` includes `run.googleapis.com`
  and `storage.googleapis.com` and excludes `aiplatform.googleapis.com`
- **AND** `google_project_iam_member.vertex_ai_user` has count `0`
- **AND** `google_project_iam_member.cloud_run_job_caller_binding[0]` grants
  `roles/run.developer` to `runner_sa`
- **AND** `google_storage_bucket_iam_member.transit_bucket_access[0]` grants
  `roles/storage.objectUser` on `"cm-transit-bucket"` to `runner_sa`
- **AND** `google_service_account_iam_member.cloud_run_runtime_sa_act_as` grants
  `roles/iam.serviceAccountUser` on the runtime Service Account to
  `runner_sa`.

______________________________________________________________________

### REQ-0005: Automatic Root Composition & Distribution-Friendly `terraform/gcp.tf` / `terraform/gcp_override.tf` Isolation {#req-0005}

- **CUJ UUID:** cuj-wif-gcp-composability

Root `terraform/`, `terraform/modules/gcp/`, and `terraform/modules/wif/` MUST
maintain a clean, unidirectional dependency boundary where root `terraform/`
automatically wires `module.gcp[0]` outputs into `module.wif` when
`execution_mode == "cloud_run"`, while isolating `module "gcp"`, its local
overrides, and its outputs inside `terraform/gcp.tf` and
`terraform/gcp_override.tf` so `scripts/distribute.sh` (`SPEC-DIST`) can strip
Cloud Run files cleanly without leaving dangling references:

1. **Resource Ownership & Outputs in `terraform/modules/gcp`:**
   - `terraform/modules/gcp` MUST retain ownership of the Cloud Run Job
     (`google_cloud_run_v2_job.runner`), the GCS bucket
     (`google_storage_bucket.reports`), and the Cloud Run runtime Service
     Account (`google_service_account.runner_sa` with `roles/aiplatform.user`).
   - `terraform/modules/gcp/outputs.tf` MUST export
     `runner_service_account_email`
     (`value = google_service_account.runner_sa.email`) alongside
     `reports_bucket_name`, `runner_job_name`, and `region`.
1. **Automatic Root Composition & Native Override Wiring (`terraform/main.tf`,
   `terraform/outputs.tf`, `terraform/gcp.tf`, & `terraform/gcp_override.tf`):**
   - `terraform/main.tf` MUST declare default empty hook locals
     (`gcp_module_bucket_name = ""`, `gcp_module_job_name = ""`,
     `gcp_module_runtime_sa_emails = []`) and instantiate `module "wif"`
     (`source = "./modules/wif"`) passing:
     - `gcs_transit_bucket_name = var.gcs_transit_bucket_name != "" ? var.gcs_transit_bucket_name : local.gcp_module_bucket_name`
     - `cloud_run_job_name = var.cloud_run_job_name != "" ? var.cloud_run_job_name : local.gcp_module_job_name`
     - `cloud_run_runtime_sa_emails = length(var.cloud_run_runtime_sa_emails) > 0 ? var.cloud_run_runtime_sa_emails : local.gcp_module_runtime_sa_emails`
   - `terraform/outputs.tf` MUST export the WIF outputs
     (`gcp_workload_identity_provider`, `gcp_service_account`,
     `execution_mode`, and `next_step_private_key_injection`) without
     referencing `module.gcp`.
   - `terraform/gcp.tf` MUST instantiate `module "gcp"`
     (`source = "./modules/gcp"`,
     `count = var.execution_mode == "cloud_run" ? 1 : 0`) and export the
     conditional Cloud Run outputs (`cloud_run_reports_bucket_name`,
     `cloud_run_job_name`), while `terraform/gcp_override.tf` MUST use
     Terraform's native `_override.tf` merging to override
     `local.gcp_module_bucket_name`, `local.gcp_module_job_name`, and
     `local.gcp_module_runtime_sa_emails` with `module.gcp[0]`'s outputs when
     `var.execution_mode == "cloud_run"`.
   - When `terraform/modules/gcp/`, `terraform/gcp.tf`,
     `terraform/gcp_override.tf`, and
     `terraform/tests/root_gcp_test.tftest.hcl` are removed (by
     `scripts/distribute.sh` for local-mode customer distribution),
     `terraform/main.tf`, `terraform/outputs.tf`, and
     `terraform/tests/root_test.tftest.hcl` MUST contain zero dangling local,
     module, or output references and MUST pass `terraform init`,
     `terraform validate`, and `terraform test` without modification.

#### Scenario: Automatic Root Wiring From `module.gcp[0]` Into `module.wif`

- **GIVEN** an operator configures root `terraform/` with
  `gcp_project_id = "test-project"`, `github_owner = "test-org"`, and
  `execution_mode = "cloud_run"` (without manually specifying
  `gcs_transit_bucket_name`, `cloud_run_job_name`, or
  `cloud_run_runtime_sa_emails`)
- **WHEN** `terraform plan` is executed in `terraform/`
- **THEN** `module.gcp[0]` is planned and its outputs
  (`reports_bucket_name`, `runner_job_name`, and
  `[runner_service_account_email]`) are
  automatically wired into `module.wif` with zero circular dependencies.

______________________________________________________________________

### REQ-0006: Mode-Aware GitHub Repository Bootstrap (`github.tf`) & Self-Documenting `terraform/terraform.tfvars.example` {#req-0006}

- **CUJ UUID:** cuj-wif-github-repo-bootstrap

When `var.github_repositories` is non-empty, `terraform/modules/wif/github.tf`
MUST bootstrap the repository labels, secrets, and mode-specific configuration
required by the CodeMender GitHub Action across `local.short_repo_names`, and
root `terraform/terraform.tfvars.example` MUST document all configuration
options across four structured sections:

1. **Zero GitHub API Calls When `var.github_repositories = []`:**
   - When `var.github_scope_type` is `"org"` or `"user"` and
     `var.github_repositories = []`, `local.short_repo_names` is empty and
     `terraform/modules/wif/github.tf` MUST create zero `github_*` resources.
1. **Common GitHub Resources (`var.github_repositories` non-empty, Both `local`
   and `cloud_run` Modes):**
   - `github_issue_label.codemender_scan`: Creates the `codemender-scan` label
     in each repository in `local.short_repo_names`.
   - `github_actions_secret.wif_provider`: Sets `CODEMENDER_WORKLOAD_IDENTITY_PROVIDER`
     to `google_iam_workload_identity_pool_provider.github_provider.name`.
   - `github_actions_secret.runner_sa`: Sets `CODEMENDER_SERVICE_ACCOUNT` to
     `google_service_account.runner_sa.email`.
   - `github_actions_variable.gcp_project_id`: Sets `CODEMENDER_GCP_PROJECT_ID`
     to `var.gcp_project_id`.
   - `github_actions_secret.app_id`: Sets `GH_APP_ID` to `var.github_app_id`
     when `var.github_app_id != ""`.
1. **Cloud Run Mode GitHub Repository Variables
   (`execution_mode == "cloud_run"`):**
   - `github_actions_variable.gcs_transit_bucket`: Sets `CODEMENDER_GCS_TRANSIT_BUCKET` to
     `var.gcs_transit_bucket_name` in each repository in
     `local.short_repo_names` when `var.execution_mode == "cloud_run"`.
   - `github_actions_variable.cloud_run_job_name`: Sets `CODEMENDER_CLOUD_RUN_JOB_NAME` to
     `var.cloud_run_job_name` in each repository in `local.short_repo_names`
     when `var.execution_mode == "cloud_run"` and `var.cloud_run_job_name != ""`.
   - `github_actions_variable.gcp_region`: Sets `CODEMENDER_GCP_REGION` to `var.gcp_region`
     in each repository in `local.short_repo_names` when
     `var.execution_mode == "cloud_run"` and `var.gcp_region != ""` and
     `var.gcp_region != "global"`.
1. **Self-Documenting Root `terraform/terraform.tfvars.example`:**
   - `terraform/terraform.tfvars.example` MUST be organized into 4 clearly
     commented sections:
     1. **Section 1: GCP Project & Identity Naming** (`gcp_project_id`,
        `gcp_region`, `gcp_service_account_id`, `gcp_wif_pool_id`,
        `gcp_wif_provider_id`).
     1. **Section 2: Execution Mode & Cloud Run Infrastructure**
        (`execution_mode = "local" | "cloud_run"`, explaining that
        `"cloud_run"` automatically provisions `terraform/modules/gcp` and wires
        its outputs into `terraform/modules/wif`, plus optional overrides and
        Cloud Run sizing/image settings).
     1. **Section 3: GCP WIF OIDC Trust Perimeter & Target Repositories**
        (`github_owner`, `github_scope_type = "org" | "user" | "repositories"`,
        and the single `github_repositories` list accepting `"repo"` or
        `"owner/repo"`).
     1. **Section 4: Optional GitHub Repository Bootstrap & Provider Auth**
        (explaining how `github_repositories = []` skips GitHub API calls in
        `"org"`/`"user"` mode, and documenting the 3 authentication options:
        ambient `gh auth login` / `GITHUB_TOKEN`, GitHub App `app_auth` via
        `github_app_id` + `github_app_installation_id` + `github_app_pem_file`,
        or explicit `github_mgmt_token`).

#### Scenario: Bootstrapping Cloud Run Variables in Target Repositories

- **GIVEN** `var.execution_mode = "cloud_run"`,
  `var.github_repositories = ["repo-a", "test-org/repo-b"]`,
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

The unified root `terraform/` module, `terraform/modules/wif/`, and
`terraform/modules/gcp/` MUST be verified by hermetic `terraform test` suites
and reflected across all repository documentation:

1. **Root Hermetic Test Suites (`terraform/tests/root_test.tftest.hcl` &
   `terraform/tests/root_gcp_test.tftest.hcl`):**
   - `terraform/tests/root_test.tftest.hcl` (retained in distributed repos)
     MUST pass `terraform test` offline using `mock_provider "google"`,
     `mock_provider "google-beta"`, and `mock_provider "github"` without
     referencing `module.gcp`, covering:
     1. Default `execution_mode = "local"` (`module.wif` provisions
        `roles/aiplatform.user` and `local.use_github_app_auth == false`).
     1. GitHub App provider authentication (`local.use_github_app_auth == true`
        when `github_app_id`, `github_app_installation_id`, and
        `github_app_pem_file` are provided).
   - `terraform/tests/root_gcp_test.tftest.hcl` (excluded by
     `scripts/distribute.sh` alongside `terraform/modules/gcp/`,
     `terraform/gcp.tf`, and `terraform/gcp_override.tf`) MUST pass
     `terraform test` offline covering:
     1. `execution_mode = "cloud_run"` (`module.gcp` has count `1` and its
        bucket, job name, and runtime SA outputs are automatically wired into
        `module.wif`).
1. **WIF Child Module Hermetic Test Suite
   (`terraform/modules/wif/tests/wif_test.tftest.hcl`):**
   - MUST pass `terraform test` offline covering:
     1. Default `execution_mode = "local"` (verifying `codemender-wif-*` default
        IDs, `roles/aiplatform.user`, org-scoped WIF binding, GitHub
        secrets/label from `var.github_repositories`, and zero Cloud Run/GCS
        bindings).
     1. `execution_mode = "cloud_run"` (verifying `run.googleapis.com` and
        `storage.googleapis.com` enabled, `roles/aiplatform.user` omitted on
        `runner_sa`, predefined `roles/run.developer` caller binding, GCS
        transit bucket `roles/storage.objectUser` binding, runtime SA
        `roles/iam.serviceAccountUser` bindings, and Cloud Run GitHub repository
        variables).
     1. Repository list scoping (`github_scope_type = "repositories"` and
        `github_repositories` normalization of mixed `"repo"` and
        `"owner/repo"` entries).
     1. Validation failures (`expect_failures = [var.gcs_transit_bucket_name]`
        when `execution_mode = "cloud_run"` and `gcs_transit_bucket_name = ""`,
        and `expect_failures = [var.github_repositories]` when
        `github_scope_type = "repositories"` and `github_repositories = []`).
1. **GCP Child Module Hermetic Test Suites
   (`terraform/modules/gcp/tests/*.tftest.hcl`):**
   - MUST pass `terraform test` offline in `terraform/modules/gcp`.
1. **Documentation & Reference Migration:**
   - All references to `terraform/gha_wif`, standalone `terraform/wif`, or
     standalone `terraform/gcp` in
     `docs/adr/ADR-0008-repository-layout-and-package-structure.md` MUST be
     updated to reflect the root `terraform/` entrypoint and
     `terraform/modules/{wif,gcp}` child hierarchy.

#### Scenario: Running `terraform test` Across Root and Child Modules

- **GIVEN** `terraform/`, `terraform/modules/wif`, and `terraform/modules/gcp`
  are initialized with `terraform init -backend=false`
- **WHEN** `terraform test` is executed in `terraform/`,
  `terraform/modules/wif`, and `terraform/modules/gcp`
- **THEN** all test suites pass with 0 failures.
