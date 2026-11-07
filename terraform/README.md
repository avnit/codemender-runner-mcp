# Infrastructure Provisioning (`terraform/`)

## Overview

The root `terraform/` configuration provisions keyless Google Cloud Workload
Identity Federation (WIF), least-privilege IAM service accounts, optional Google
Cloud Run Jobs and GCS transit storage, and optional GitHub Actions repository
secrets, variables, and labels.

- **Specifications:**
  [SPEC-WIF](../docs/openspec/specs/wif/spec.md) and
  [Security and Trust Zones](../docs/openspec/specs/tech/systems/security-and-trust-zones/design.md).
- **Execution Environment Guides:**
  - [Google Cloud Run Jobs + GCS Cache (`gs://`)](../docs/user-guides/cloud-run.md)
  - [Local Docker + gVisor (`runsc`)](../docs/user-guides/local-gvisor.md)

## QuickStart

Copy [`terraform.tfvars.example`](terraform.tfvars.example) to
`terraform.tfvars`, populate the required variables for your environment, and
apply the root module:

- **`gcp_project_id`:** Target Google Cloud Project ID where WIF and/or Cloud
  Run resources reside.
- **`execution_mode`:** `"cloud_run"` (recommended in
  [`terraform.tfvars.example`](terraform.tfvars.example); provisions WIF plus
  Cloud Run Jobs and a GCS transit bucket) or `"local"` (default in
  [`variables.tf`](variables.tf); provisions WIF only for containers running on
  the CI runner host).
- **`github_scope_type`:** OIDC trust scope (`"org"`, `"user"`, or
  `"repositories"`).
- **`github_owner`:** GitHub organization or username (or `""` when
  `github_scope_type = "repositories"`).
- **`github_repositories`:** Repositories (`"repo"` or `"owner/repo"`) to
  authorize and/or bootstrap with GitHub Actions secrets, variables, and the
  `codemender-scan` label.

## Details

### Module Architecture

The root configuration ([`main.tf`](main.tf), [`gcp.tf`](gcp.tf),
[`gcp_override.tf`](gcp_override.tf), and [`provider.tf`](provider.tf)) composes
two child modules:

1. **`module.wif` (`./modules/wif`) — Always Provisioned:**
   Creates the Google Cloud Workload Identity Pool (`gcp_wif_pool_id`), the
   GitHub Actions OIDC Provider (`gcp_wif_provider_id`), and the caller service
   account (`gcp_service_account_id`, default `codemender-wif-sa`). When
   `github_repositories` is non-empty, `module.wif` also bootstraps target
   GitHub repositories with Actions secrets, variables, and the
   `codemender-scan` issue label ([`modules/wif/github.tf`](modules/wif/github.tf)).
2. **`module.gcp` (`./modules/gcp`) — Provisioned When `execution_mode == "cloud_run"`:**
   Enables required Google Cloud APIs, creates the GCS reports/transit bucket
   with lifecycle TTL deletion (`reports_bucket_ttl_days`), provisions the
   Artifact Registry repository (`<resource_prefix>-runner`), builds and pushes
   the two-stage Cloud Run worker image via Google Cloud Build
   ([`../cloudbuild.yaml`](../cloudbuild.yaml)) when `auto_build_image = true`,
   and deploys the `codemender-runner` Cloud Run Job with a dedicated runtime
   service account (`codemender-runner-sa`).
   - **Automatic Cross-Module Wiring ([`gcp_override.tf`](gcp_override.tf)):**
     When `execution_mode = "cloud_run"`, the root module automatically passes
     `module.gcp[0].reports_bucket_name`, `module.gcp[0].runner_job_name`, and
     `[module.gcp[0].runner_service_account_email]` into `module.wif` unless
     explicitly overridden via `gcs_transit_bucket_name`, `cloud_run_job_name`,
     or `cloud_run_runtime_sa_emails`.

### Execution Modes

The `execution_mode` variable controls which infrastructure and IAM boundaries
are provisioned:

| Feature / Resource | `execution_mode = "cloud_run"` (Recommended in `terraform.tfvars.example`) | `execution_mode = "local"` (Default in `variables.tf`) |
| :--- | :--- | :--- |
| **Child Modules Provisioned** | `module.wif` + `module.gcp` | `module.wif` only |
| **Service Account Model** | **Dual-SA Least-Privilege Split ([Security and Trust Zones Decision 3](../docs/openspec/specs/tech/systems/security-and-trust-zones/design.md#decision-3-mode-switched-least-privilege-gcp-wif-local-single-sa-vs-cloud_run-coordinatorworker-dual-sa-split)):**<br>1. **WIF Caller SA (`codemender-wif-sa`):** Impersonated by CI OIDC; granted `roles/run.developer`, `roles/storage.objectUser` on the transit bucket, and `roles/iam.serviceAccountUser` (`iam.serviceAccounts.actAs`) on the Cloud Run runtime SA (`codemender-runner-sa`).<br>2. **Cloud Run Runtime SA (`codemender-runner-sa`):** Attached to the `codemender-runner` Cloud Run Job container; granted `roles/aiplatform.user`, `roles/logging.logWriter`, and object access on the transit bucket. Holds zero repository host tokens. | **Single-SA Direct Model:**<br>1. **WIF Caller SA (`codemender-wif-sa`):** Impersonated by CI OIDC on the runner VM; granted `roles/aiplatform.user` directly to call Vertex AI from the local `cm-runner` container. |
| **Artifact & Source Staging** | GCS transit bucket (`gs://<prefix>-reports-<suffix>`) with automatic object deletion after `reports_bucket_ttl_days` (default `7` days). | Local filesystem cache (`file://`) on the CI runner host. |
| **Container Build & Registry** | Artifact Registry (`<prefix>-runner`) + automatic Cloud Build submission ([`../cloudbuild.yaml`](../cloudbuild.yaml)) during apply when `auto_build_image = true`. | Uses `ghcr.io/<owner>/<repo>:latest` or a locally built `codemender-runner:local` image. |

### GitHub Authentication

#### 1. GCP WIF OIDC Trust Perimeter (`github_scope_type`)

The Workload Identity Pool Provider validates incoming GitHub Actions OIDC JWTs
against `https://token.actions.githubusercontent.com` using the trust condition
selected by `github_scope_type`:

- **`github_scope_type = "org"` (default):**
  - **GCP WIF Trust Condition:** `assertion.repository_owner == "<github_owner>"`
    (trusts any repository inside the GitHub organization).
  - **`github_repositories` Behavior:** Lists the specific repositories where
    Terraform should automatically create GitHub Actions secrets, variables, and
    the `codemender-scan` label. Set `github_repositories = []` to provision
    org-wide GCP WIF without making any GitHub API calls.
- **`github_scope_type = "user"`:**
  - **GCP WIF Trust Condition:** `assertion.repository_owner == "<github_owner>"`
    (trusts any repository owned by the personal GitHub user account).
  - **`github_repositories` Behavior:** Lists repositories to bootstrap with
    secrets, variables, and labels (or `[]` to skip GitHub API calls).
- **`github_scope_type = "repositories"`:**
  - **GCP WIF Trust Condition:**
    `assertion.repository in ['owner/repo-a', 'owner/repo-b', ...]` (strictly
    restricts GCP WIF token exchange to the explicitly listed repositories).
  - **`github_repositories` Behavior:** At least one repository is required.
    Defines both the GCP WIF OIDC allowlist and the repositories to bootstrap on
    GitHub.

#### 2. GitHub Provider Authentication (`provider "github"`)

When `github_repositories` is non-empty, [`provider.tf`](provider.tf) supports
two authentication methods for the `integrations/github` provider:

1. **Ambient CLI / Environment Credentials (Default):**
   When `github_mgmt_token` is omitted (`null`), the GitHub provider
   automatically uses `GITHUB_TOKEN` from the environment or your active
   `gh auth login` session.
2. **Explicit Personal Access Token (`github_mgmt_token`):**
   Set `github_mgmt_token` to a classic PAT (`repo` scope) or fine-grained PAT
   (Actions, Administration/Issues, Variables, and Secrets read/write
   permissions on target repositories) to override ambient CLI credentials.

### Inputs and Outputs

#### Inputs (`terraform/variables.tf`)

| Name | Type | Default | Required | Description |
| :--- | :--- | :--- | :--- | :--- |
| `gcp_project_id` | `string` | — | Yes | The GCP Project ID where resources will be deployed and Vertex AI / Cloud Run Jobs reside. |
| `gcp_region` | `string` | `"us-central1"` | No | The GCP region for resource deployment. |
| `gcp_service_account_id` | `string` | `"codemender-wif-sa"` | No | ID of the dedicated Service Account to create for CodeMender WIF authentication in GCP. |
| `gcp_wif_pool_id` | `string` | `"codemender-wif-pool"` | No | ID of the Workload Identity Pool to create in GCP. |
| `gcp_wif_provider_id` | `string` | `"codemender-wif-provider"` | No | ID of the Workload Identity Provider to create in GCP. |
| `execution_mode` | `string` | `"local"` | No | Execution mode for GitHub Actions workflows: `"local"` (runs `cm-runner` on runner with Vertex AI access) or `"cloud_run"` (stages repository to GCS transit bucket and invokes Cloud Run Job). Note: `terraform.tfvars.example` sets `"cloud_run"`. |
| `gcs_transit_bucket_name` | `string` | `""` | No | Optional override for the GCS transit bucket name. In `"cloud_run"` mode, automatically defaults to `reports_bucket_name` provisioned by `module.gcp`. |
| `cloud_run_job_name` | `string` | `""` | No | Optional override for the Cloud Run Job name. In `"cloud_run"` mode, automatically defaults to `runner_job_name` provisioned by `module.gcp`. |
| `cloud_run_runtime_sa_emails` | `list(string)` | `[]` | No | Optional override for Cloud Run Job runtime Service Account email addresses authorized for `actAs`. In `"cloud_run"` mode, automatically defaults to runtime SAs provisioned by `module.gcp`. |
| `resource_prefix` | `string` | `"codemender"` | No | Prefix used for naming provisioned GCP resources to prevent multi-deployment collisions. |
| `reports_bucket_name` | `string` | `""` | No | Name of the GCS bucket for scan reports and source snapshot staging (auto-generated with a random suffix when empty). |
| `reports_bucket_ttl_days` | `number` | `7` | No | Number of days (`>= 1`) to retain objects in the GCS reports and transit bucket before lifecycle deletion. |
| `runner_cpu` | `string` | `"2000m"` | No | CPU limit for Cloud Run Job worker tasks (e.g. `"2000m"`, `"2"`, `"4"`). |
| `runner_memory` | `string` | `"8Gi"` | No | Memory limit for Cloud Run Job worker tasks (e.g. `"2Gi"`, `"4Gi"`, `"8Gi"`). |
| `runner_timeout` | `string` | `"3600s"` | No | Task timeout for Cloud Run Job worker tasks (e.g. `"600s"`, `"1800s"`, `"3600s"`). |
| `create_vpc_and_nat` | `bool` | `false` | No | Whether to create a dedicated VPC network, subnet, connector, and Cloud NAT for private egress. |
| `existing_vpc_connector_id` | `string` | `null` | No | ID of an existing Serverless VPC Access Connector if `create_vpc_and_nat` is `false`. |
| `vpc_connector_cidr` | `string` | `"10.8.0.0/28"` | No | IPv4 CIDR range (`/28`) for the Serverless VPC Access Connector. |
| `vpc_connector_min_instances` | `number` | `2` | No | Minimum number of instances for the Serverless VPC Access Connector. |
| `vpc_connector_max_instances` | `number` | `3` | No | Maximum number of instances for the Serverless VPC Access Connector. |
| `vpc_connector_machine_type` | `string` | `"e2-micro"` | No | Machine type for the Serverless VPC Access Connector. |
| `auto_build_image` | `bool` | `true` | No | Whether Terraform should automatically build and push the Cloud Run Job container image via Cloud Build (`cloudbuild.yaml`) before creating or updating the Cloud Run Job. |
| `github_owner` | `string` | `""` | Conditional | GitHub organization name (e.g. `"my-org"`) or username (e.g. `"octocat"`). Required if `github_scope_type` is `"org"` or `"user"`. |
| `github_scope_type` | `string` | `"org"` | No | Scoping type for WIF authentication: `"org"` (all repos in org), `"user"` (all repos under user), or `"repositories"` (specific repo allowlist). |
| `github_repositories` | `list(string)` | `[]` | Conditional | List of target GitHub repositories in `"repo"`, `"owner/repo"`, or `"https://github.com/owner/repo"` format. At least one required when `github_scope_type = "repositories"`. |
| `github_mgmt_token` | `string` (sensitive) | `null` | No | Personal access token (classic or fine-grained) for GitHub provider authentication when overriding ambient CLI/environment auth. |

#### Outputs (`terraform/outputs.tf` & `terraform/gcp.tf`)

| Output Name | Defined In | Description |
| :--- | :--- | :--- |
| `gcp_workload_identity_provider` | [`outputs.tf`](outputs.tf) | Full resource name of the Workload Identity Provider for GitHub Actions secret `CODEMENDER_WORKLOAD_IDENTITY_PROVIDER`. |
| `gcp_service_account` | [`outputs.tf`](outputs.tf) | Service Account email for GitHub Actions secret `CODEMENDER_SERVICE_ACCOUNT`. |
| `execution_mode` | [`outputs.tf`](outputs.tf) | Configured CodeMender execution mode (`"local"` or `"cloud_run"`). |
| `cloud_run_reports_bucket_name` | [`gcp.tf`](gcp.tf) | GCS bucket name for scan reports and transit archives when `execution_mode` is `"cloud_run"` (`null` in `"local"` mode). |
| `cloud_run_job_name` | [`gcp.tf`](gcp.tf) | Cloud Run Job name (`<resource_prefix>-runner`) when `execution_mode` is `"cloud_run"` (`null` in `"local"` mode). |
| `cloud_run_artifact_registry_repository` | [`gcp.tf`](gcp.tf) | Artifact Registry Docker repository path when `execution_mode` is `"cloud_run"` (`null` in `"local"` mode). |
| `cloud_run_image_uri` | [`gcp.tf`](gcp.tf) | Deterministic Artifact Registry image URI for the Cloud Run Job when `execution_mode` is `"cloud_run"` (`null` in `"local"` mode). |
| `next_step_cloud_build_command` | [`gcp.tf`](gcp.tf) | Standalone `gcloud builds submit` command to manually rebuild and push the Cloud Run Job container image when `execution_mode` is `"cloud_run"` (`null` in `"local"` mode). |

#### Bootstrapped GitHub Repository Resources (`terraform/modules/wif/github.tf`)

When `github_repositories` is non-empty, `module.wif` automatically provisions
the following resources on each target repository:

| Resource Name | Type | Provisioned When | Value / Purpose |
| :--- | :--- | :--- | :--- |
| `CODEMENDER_WORKLOAD_IDENTITY_PROVIDER` | GitHub Actions Secret | Always (`for_each` repo) | Full resource name of the GCP Workload Identity Pool Provider (`google_iam_workload_identity_pool_provider.github_provider.name`). |
| `CODEMENDER_SERVICE_ACCOUNT` | GitHub Actions Secret | Always (`for_each` repo) | Email of the WIF caller service account (`google_service_account.runner_sa.email`). |
| `CODEMENDER_GCP_PROJECT_ID` | GitHub Actions Variable | Always (`for_each` repo) | Target Google Cloud Project ID (`var.gcp_project_id`). |
| `CODEMENDER_GCS_TRANSIT_BUCKET` | GitHub Actions Variable | `execution_mode == "cloud_run"` | GCS transit and reports bucket name (`var.gcs_transit_bucket_name`). |
| `CODEMENDER_CLOUD_RUN_JOB_NAME` | GitHub Actions Variable | `execution_mode == "cloud_run"` and `cloud_run_job_name != ""` | Cloud Run Job name (`var.cloud_run_job_name`, default `codemender-runner`). |
| `CODEMENDER_GCP_REGION` | GitHub Actions Variable | `execution_mode == "cloud_run"` and `gcp_region` is non-empty / non-`"global"` | Regional deployment location (`var.gcp_region`, default `us-central1`). |
| `codemender-scan` | GitHub Issue Label | Always (`for_each` repo) | Issue/PR trigger label (`#0E8A16`) for CodeMender automated security scan and remediation. |
