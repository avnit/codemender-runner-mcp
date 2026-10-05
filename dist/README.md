# CodeMender for CI/CD — Setup & Usage Guide

This repository is a reference implementation and starter kit for
running Google CodeMender automated security scanning and patching on
pull requests. It contains the necessary GitHub actions workflows,
runner scripts, and terraform configurations needed to run CodeMender in
this repository or set it up in additional repositories.

## Repository Layout

```text
.
├── LICENSE
├── .gitignore
├── README.md
└── .github/
    ├── actions/
    │   └── codemender/                         # Composite GitHub Action
    ├── workflows/
    │   ├── build_runner_image.yml              # Builds & pushes cm-runner image
    │   └── cm-merge-scan.yml                   # Runs CodeMender on Pull Requests
    ├── codemender-runner/                      # Source for the cm-runner container image
    └── terraform/                              # Root Terraform configuration
```

## Prerequisites

1. **Google Cloud Project** with billing enabled and Vertex AI API
   access for CodeMender.
1. **Terraform** (`>= 1.5`) and **Google Cloud SDK (`gcloud`)**
   authenticated (`gcloud auth application-default login`) for one-time
   infrastructure setup.
1. **GitHub Authentication** (ambient `gh auth login` / `GITHUB_TOKEN`
   or `github_mgmt_token` when bootstrapping repository secrets and
   variables via Terraform).

## Step 1 — Provision GCP Workload Identity Federation (Terraform)

Use the [`.github/terraform/`](.github/terraform/) configuration (which invokes
the `.github/terraform/modules/wif` child module) to configure keyless
authentication between GitHub Actions and Google Cloud:

```bash
cd .github/terraform

# 1. Copy the example variables file
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` (see
[`.github/terraform/terraform.tfvars.example`](.github/terraform/terraform.tfvars.example))
with your environment values:

- `gcp_project_id`: Your Google Cloud project ID.
- `gcp_region`: Target GCP region (default: `"us-central1"`).
- `github_scope_type`: `"org"`, `"user"`, or `"repositories"`.
- `github_owner`: Your GitHub organization or username.
- `github_repositories`: List of repository names (`"repo"` or `"owner/repo"`)
  to authorize and/or bootstrap with GitHub Actions secrets, variables, and
  labels.
- `execution_mode`: `"local"` (runs the `cm-runner` container directly on the
  GitHub Actions runner) or `"cloud_run"` (dispatches isolated Cloud Run Jobs).

Initialize and apply the Terraform configuration:

```bash
# Authenticate to GitHub first.
# Either `gh auth loging` or export GITHUB_TOKEN="ghp_your_github_pat"

terraform init
terraform plan
terraform apply

# Inspect the provisioned WIF provider and service account outputs
terraform output
```

## Step 2 — Configure GitHub Actions Secrets & Variables

When `github_repositories` is populated in `terraform.tfvars`, `terraform apply`
automatically configures the repository secrets and variables below. If you
manage repository settings manually, configure the following in **Settings →
Secrets and variables → Actions**:

| Name                             | Type     | Description                                                                                                   |
| :------------------------------- | :------- | :------------------------------------------------------------------------------------------------------------ |
| `CODEMENDER_WORKLOAD_IDENTITY_PROVIDER` | Secret   | Full resource name of the GCP Workload Identity Provider (`terraform output gcp_workload_identity_provider`). |
| `CODEMENDER_SERVICE_ACCOUNT`            | Secret   | Email of the GCP Service Account (`terraform output gcp_service_account`).                                    |
| `CODEMENDER_GCP_PROJECT_ID`                 | Variable | Google Cloud project ID hosting Vertex AI.                                                                    |

## Step 3 — Build & Publish the `cm-runner` Container Image

The composite GitHub Action executes `cm-runner` inside a container image built
from
[`.github/codemender-runner/Dockerfile`](.github/codemender-runner/Dockerfile)
(`ENTRYPOINT ["cm-runner"]`, `CMD ["version"]`).

### Option A: Build Automatically in GitHub Actions

Trigger
[`.github/workflows/build_runner_image.yml`](.github/workflows/build_runner_image.yml)
from the **Actions** tab (**Build & Publish CodeMender Runner Image → Run
workflow**) or push to `main`. This builds `.github/codemender-runner`, verifies
the `cm-runner` entrypoint contract, and publishes
`ghcr.io/<owner>/codemender-runner:<commit-sha>`.

### Option B: Build & Push Manually with Docker (Artifact Registry or GHCR)

```bash
COMMIT_SHA="$(git rev-parse HEAD)"
IMAGE_URI="us-docker.pkg.dev/<GCP_PROJECT_ID>/<AR_REPO>/codemender-runner:${COMMIT_SHA}"

# 1. Build the image from .github/codemender-runner
docker build \
    --build-arg CM_VERSION="0.11.0" \
    -t "${IMAGE_URI}" \
    .github/codemender-runner

# 2. Verify the container entrypoint contract
docker run --rm "${IMAGE_URI}" version

# 3. Push to your container registry
gcloud auth configure-docker us-docker.pkg.dev
docker push "${IMAGE_URI}"
```

After publishing the image, set `runner_image` in
[`.github/workflows/cm-merge-scan.yml`](.github/workflows/cm-merge-scan.yml) to
your pinned `<commit-sha>` image tag.

## Step 4 — Run CodeMender on Pull Requests & via CLI

### Automated Pull Request Scans

Once `runner_image` is configured in
[`.github/workflows/cm-merge-scan.yml`](.github/workflows/cm-merge-scan.yml):

1. Open or update any pull request targeting `main` or `master`.
1. The `CodeMender` workflow runs
   [`.github/actions/codemender/action.yml`](.github/actions/codemender/action.yml),
   which pulls your `cm-runner` image, scans the pull request diff, and posts or
   updates the CodeMender report comment on the PR.

## Setting Up CodeMender in a Separate Repository

To enable CodeMender pull request scanning in another repository:

1. **Copy the composite action and workflow into the target repository:**

   ```bash
   mkdir -p /path/to/your-repo/.github/actions /path/to/your-repo/.github/workflows
   cp -R .github/actions/codemender /path/to/your-repo/.github/actions/
   cp .github/workflows/cm-merge-scan.yml /path/to/your-repo/.github/workflows/
   ```

1. **Point the workflow at your `cm-runner` container image:** Edit
   `/path/to/your-repo/.github/workflows/cm-merge-scan.yml` and set
   `runner_image` to your published `cm-runner` image URI (pinned by commit
   SHA), then commit and push the changes to your repository.

1. **Authorize the new repository in GCP Workload Identity Federation:** In
   `.github/terraform/terraform.tfvars` (see
   [`.github/terraform/terraform.tfvars.example`](.github/terraform/terraform.tfvars.example)),
   add the new repository name to `github_repositories` and run:

   ```bash
   cd .github/terraform
   terraform apply
   ```

   *(Or manually configure the secrets and variables from
   [Step 2](#step-2--configure-github-actions-secrets--variables) in the new
   repository's **Settings → Secrets and variables → Actions**.)*

