# CodeMender GitHub Actions — Setup & Usage Guide

Scans each pull request with CodeMender (`cm-runner find-diff`) and generates
candidate patches (`cm-runner fix`), uploads SARIF and Markdown reports as
workflow artifacts, and posts a summary comment and inline review suggestions on
the pull request.

See [SPEC-GHA](../docs/openspec/specs/github-actions/spec.md),
[SPEC-GHA-FIX](../docs/openspec/specs/github-actions-fix/spec.md), and
[SPEC-DIST](../docs/openspec/specs/distribute/spec.md) for architecture and
security model specifications.

## Prerequisites

1. **Google Cloud Project** with billing enabled and Vertex AI API access for
   CodeMender.
2. **Terraform** (`>= 1.5`) and **Google Cloud SDK (`gcloud`)** authenticated
   (`gcloud auth application-default login`) for one-time infrastructure setup.
3. **GitHub Authentication** (`gh auth login` or `GITHUB_TOKEN` /
   `github_mgmt_token` when bootstrapping repository secrets and variables via
   Terraform).

## Step 1 — Provision Google Cloud & Workload Identity Federation (Terraform)

Use the root [`terraform/`](../terraform/) configuration to provision keyless
Workload Identity Federation between GitHub Actions and Google Cloud (and, when
`execution_mode = "cloud_run"`, Google Cloud Run Jobs and a GCS transit bucket):

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` (see
[`terraform/terraform.tfvars.example`](../terraform/terraform.tfvars.example))
with your environment values:

- `gcp_project_id`: Your Google Cloud project ID.
- `gcp_region`: Target GCP region (default: `"us-central1"`).
- `execution_mode`: `"cloud_run"` (dispatches isolated Cloud Run Jobs; default
  for `.github/actions/codemender`) or `"local"` (runs the `cm-runner` container
  under Docker + gVisor on the GitHub Actions runner VM via
  `.github/actions/codemender-local`).
- `github_scope_type`: `"org"`, `"user"`, or `"repositories"`.
- `github_owner`: Your GitHub organization or username (or `""` when
  `github_scope_type = "repositories"`).
- `github_repositories`: List of repository names (`"repo"` or `"owner/repo"`)
  to authorize and/or bootstrap with GitHub Actions secrets, variables, and
  labels.

Initialize and apply the Terraform configuration:

```bash
# Authenticate to GitHub first (`gh auth login` or export GITHUB_TOKEN="...")
terraform init
terraform plan
terraform apply

# Inspect the provisioned WIF provider and service account outputs
terraform output
```

## Step 2 — Configure GitHub Actions Secrets & Variables

When `github_repositories` is populated in `terraform.tfvars`, `terraform apply`
automatically configures the target repositories. If you manage settings
manually (at the **Organization** or **Repository** level under **Settings →
Secrets and variables → Actions**), configure:

| Name | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `CODEMENDER_WORKLOAD_IDENTITY_PROVIDER` | Secret | Yes | Full resource name of the GCP Workload Identity Provider (`terraform output gcp_workload_identity_provider`). |
| `CODEMENDER_SERVICE_ACCOUNT` | Secret | Yes | Email of the GCP Service Account (`terraform output gcp_service_account`). |
| `CODEMENDER_GCP_PROJECT_ID` | Variable | Yes | Google Cloud project ID hosting Vertex AI. |
| `CODEMENDER_GCS_TRANSIT_BUCKET` | Variable | Optional (Cloud Run) | GCS bucket for staging source archives and reports (`codemender` action). Defaults to `codemender-reports-<project>`. |
| `CODEMENDER_CLOUD_RUN_JOB_NAME` | Variable | Optional (Cloud Run) | Cloud Run Job name (`codemender` action). Defaults to `codemender-runner`. |
| `CODEMENDER_GCP_REGION` | Variable | Optional (Cloud Run) | GCP region for the Cloud Run Job (`codemender` action). Defaults to `us-central1`. |
| `CODEMENDER_RUNNER_IMAGE` | Variable | Optional (Local Docker) | Container image URI for `.github/actions/codemender-local`. Defaults to `ghcr.io/<action-or-workflow-repo>:latest` in lowercase. |

## Step 3 — Build & Publish the `cm-runner` Container Image

CodeMender executes inside a container image built from the root
[`Dockerfile`](../Dockerfile) (`ENTRYPOINT ["cm-runner"]`, `CMD ["version"]`),
which comes pre-baked with Python 3.11, Node.js 22 (`npm`/`yarn`/`pnpm`),
OpenJDK 17 (`mvn`/`gradle`), Go 1.22, and C/C++ (`gcc`/`g++`/`make`/`cmake`).

### Option A: Build Automatically in GitHub Actions (GHCR)

Trigger
[`.github/workflows/build_runner_image.yml`](workflows/build_runner_image.yml)
from the **Actions** tab (**Build & Publish CodeMender Runner Image → Run
workflow**) or push to `main`. This builds the root `Dockerfile`, verifies the
`cm-runner` entrypoint contract, and publishes
`ghcr.io/<owner>/codemender-runner:latest` and `:<commit-sha>`.

### Option B: Build & Push Manually with Docker (Artifact Registry or GHCR)

```bash
COMMIT_SHA="$(git rev-parse HEAD)"
IMAGE_URI="us-docker.pkg.dev/<GCP_PROJECT_ID>/<AR_REPO>/codemender-runner:${COMMIT_SHA}"

# 1. Build the image from the repository root
docker build -t "${IMAGE_URI}" .

# 2. Verify the container entrypoint contract
docker run --rm "${IMAGE_URI}" version

# 3. Push to your container registry
gcloud auth configure-docker us-docker.pkg.dev
docker push "${IMAGE_URI}"
```

### Customizing Project Dependencies in the Image

Because `cm-runner fix` runs your project's build/test command inside an offline
sandbox (`permissive-closed`) to verify candidate patches, if your repositories
require third-party libraries (such as `pytest`, `flask`, or `jest`) or
additional toolchains, edit the root [`Dockerfile`](../Dockerfile) directly to
install those dependencies, and then build and publish your custom image using
[Option A](#option-a-build-automatically-in-github-actions-ghcr) or
[Option B](#option-b-build--push-manually-with-docker-artifact-registry-or-ghcr)
above.

## Step 4 — Enable CodeMender on Your Repositories

You can roll out CodeMender across an entire GitHub Organization or in
individual repositories without copying action files into target repositories:

| Rollout Option | Files in Target Repo | Supported Event Triggers | Action & Package Version Isolation |
| :--- | :--- | :--- | :--- |
| **Option 1: Organization Repository Ruleset** | **0 files** | `pull_request` / `push` / `merge_group` (**PR scan `find-diff` + automatic inline `fix` suggestions**) | Isolated to `<your-org>/codemender-runner` after running `make init-fork` (or pin `@<tag/sha>`) |
| **Option 2: Caller Workflow in Target Repo** (`workflow_call` or `uses:`) | 1 thin `.github/workflows/codemender.yml` file (`.github/actions/*` and `packages/` stay centralized) | `pull_request` (**scan + fix**) **and** `pull_request_review_comment` / `issue_comment` (**on-demand `@cm-runner verify`**) | Pin `@<tag/sha>` or reference `<your-org>/codemender-runner@main` |

### Option 1: Organization-Wide Rollout (Fork + Repository Rulesets)

To enforce CodeMender pull request scanning (`find-diff`) and automatic fix
suggestions (`fix`) across multiple repositories in a GitHub Organization with
**zero workflow files** added to target repositories:

1. **Fork `codemender-runner` into your organization (`<your-org>/codemender-runner`) and update workflow action references:**
   Run the fork ownership helper in your fork clone so
   `.github/workflows/cm-merge-scan.yml` references the actions in
   `<your-org>/codemender-runner` instead of the upstream repository you forked
   from:
   ```bash
   ./scripts/set-fork-owner.sh <your-org>
   # or auto-detect <owner>/<repo> from `git remote get-url origin`:
   make init-fork
   ```
   Commit and push the updated workflow file to your fork's default branch.
   - **Why this matters for change isolation:** If you leave
     `uses: cloud-gtm/codemender-runner/.github/actions/*@main` unedited, GitHub
     resolves `@main` against the upstream repository live on every workflow
     run—so breaking changes merged to upstream `main` immediately affect your
     runs even if you have not synced your fork. Running `make init-fork` (or
     pinning `@<ref>` to a release tag/commit SHA) ensures your workflows only
     update when you explicitly sync your fork.
   - **Private / Internal forks:** If your fork is **Private** or **Internal**,
     also navigate in your fork's GitHub settings to **Settings → Actions →
     General → Access** and select **Accessible from repositories in the
     organization** so other repositories in `<your-org>` can invoke your fork's
     workflows and composite actions.
2. **Configure Google Cloud credentials** at the organization or repository
   level as shown in [Step 2](#step-2--configure-github-actions-secrets--variables).
   *(Repositories in the organization that have not configured these credentials
   automatically emit a `::notice::` and exit `0` so their pull requests are not
   blocked.)*
3. **Enable the Organization Repository Ruleset:**
   In your GitHub Organization settings, navigate to **Settings → Rules →
   Rulesets → New branch ruleset**:
   - Target your desired repositories and default branch (`main` / `master`).
   - Enable **Require workflows to pass before merging** and select
     `.github/workflows/cm-merge-scan.yml` from `<your-org>/codemender-runner`.

> [!NOTE]
> **Organization Ruleset event scope vs. PR comment triggers (`@cm-runner verify`):**
> GitHub Organization Repository Rulesets only trigger required workflows on
> branch/merge-gate events (`pull_request`, `push`, `merge_group`). Both **PR
> diff scanning (`find-diff`)** and **automatic inline fix suggestions (`fix`)**
> run during `pull_request` events and work with **zero workflow files** in
> target repositories. However, because GitHub Organization Rulesets ignore
> `pull_request_review_comment` and `issue_comment` events, on-demand PR comment
> commands (such as `@cm-runner verify`) require adding a thin caller workflow
> in the target repository (see [Option 2](#option-2-single-repository-or-comment-triggered-rollout-caller-workflow-in-target-repo))
> or an organization-level GitHub App / webhook dispatcher. Even when a thin
> caller workflow is used in the target repository, `.github/actions/*` and
> `packages/` remain centralized in `codemender-runner` and are never copied.

### Option 2: Single-Repository or Comment-Triggered Rollout (Caller Workflow in Target Repo)

Use a thin caller workflow in the target repository when:
- You are on a **personal GitHub account** (where Organization Rulesets are unavailable) or rolling out to a **single repository**, or
- Your repository needs **PR-comment-triggered commands** (such as `@cm-runner verify` on `pull_request_review_comment` / `issue_comment`, which GitHub Organization Rulesets do not trigger).

Configure the secrets and variables from
[Step 2](#step-2--configure-github-actions-secrets--variables) in your
repository, then add a workflow using either a reusable workflow call or a
direct composite action step (pinning `@<ref>` to a release tag/commit SHA or
referencing `<your-org>/codemender-runner` if you want to isolate from live
upstream `@main` changes):

#### 2A. Call the Reusable Workflow (`workflow_call`)

Create `.github/workflows/codemender.yml` in your repository:

```yaml
name: CodeMender

on:
  pull_request:
    branches: [main, master]

permissions:
  contents: read
  id-token: write
  packages: read
  pull-requests: write

jobs:
  codemender:
    uses: cloud-gtm/codemender-runner/.github/workflows/cm-merge-scan.yml@main
    secrets: inherit
```

#### 2B. Invoke the Composite Action Directly (`uses:`)

To run the composite action as a step inside your own workflow job (using
`codemender` for Cloud Run mode or `codemender-local` for Local Docker + gVisor
mode):

```yaml
name: CodeMender

on:
  pull_request:
    branches: [main, master]

permissions:
  contents: read
  id-token: write
  packages: read
  pull-requests: write

jobs:
  codemender:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0
          persist-credentials: false

      # Cloud Run mode (default):
      - uses: cloud-gtm/codemender-runner/.github/actions/codemender@main
        with:
          gcp_project_id: ${{ vars.CODEMENDER_GCP_PROJECT_ID }}
          gcp_workload_identity_provider: ${{ secrets.CODEMENDER_WORKLOAD_IDENTITY_PROVIDER }}
          gcp_service_account: ${{ secrets.CODEMENDER_SERVICE_ACCOUNT }}

      # Or Local Docker + gVisor mode:
      # - uses: cloud-gtm/codemender-runner/.github/actions/codemender-local@main
      #   with:
      #     gcp_project_id: ${{ vars.CODEMENDER_GCP_PROJECT_ID }}
      #     gcp_workload_identity_provider: ${{ secrets.CODEMENDER_WORKLOAD_IDENTITY_PROVIDER }}
      #     gcp_service_account: ${{ secrets.CODEMENDER_SERVICE_ACCOUNT }}
      #     # Optional: defaults to ghcr.io/<action-repo>:latest when omitted
      #     # runner_image: ${{ vars.CODEMENDER_RUNNER_IMAGE }}
```

## Configuring Fixes & Action Reference

### Automatic Fixes (`fix: 'true'`)

- **Enabled by default:** After `find-diff` reports findings, the action runs
  `cm-runner fix` for each finding and updates the PR summary table's `Fix`
  column. Set `fix: 'false'` in the action `with:` block to run only the scan.
- **How fixes are posted on the PR:**
  - **One-click suggestions (`✅ Suggested`):** When all lines modified by a
    patch fall inside the PR diff's hunks (and do not touch `.github/`), the
    action posts an inline PR review with ```` ```suggestion ```` blocks that
    can be committed directly in GitHub.
  - **Patch comments (`📎 Patch`):** When a patch modifies lines outside the PR
    diff or touches `.github/`, the action posts a collapsible unified `diff`
    comment with `git apply` instructions.
- **`Fix` column status values:**

  | Status | Meaning |
  | :--- | :--- |
  | `⏳ Working` | `cm-runner fix` is currently running for this finding. |
  | `✅ Suggested` | Links to a PR review with one-click ```` ```suggestion ```` block(s). |
  | `📎 Patch` | Links to a review comment with a unified diff (or to the workflow run artifacts). |
  | `❌ No fix` | `cm-runner fix` exited with code 3 (could not produce or verify a patch). Does not fail the job. |
  | `⚠️ Failed` | The fix run failed or timed out. Does not fail the job; see the collapsible `::group::` in the job log. |

- **Customizing the build/test command (`.codemender.yaml`):** You can override
  the verification command that `cm-runner init` configures by setting
  `build.command` in `.codemender.yaml` at the root of your repository.

### On-Demand Verification (`@cm-runner verify`)

- **Trigger:** Reply `@cm-runner verify` inside any CodeMender fix review
  comment thread on a pull request.
- **Authorization:** Only repository owners, organization members, and
  collaborators (`OWNER`, `MEMBER`, `COLLABORATOR`) can trigger verification.
  Comments from bots, external contributors, or cross-repository fork PRs are
  ignored.
- **Deduplication & concurrency:**
  - Each `@cm-runner verify` comment runs in its own GitHub Actions workflow run
    (up to `MAX_CONCURRENT_VERIFIES = 4` concurrent verifications per PR).
  - If verification is already `in-progress` for that finding, duplicate
    requests are skipped immediately before installing gVisor or pulling the
    runner image.
  - If the finding was already verified (`VERIFIED`, `DISMISSED`, or `OPEN`),
    CodeMender replies with a link to the existing verification result without
    re-running the container.
- **Outcomes:** CodeMender reacts with 👀 (`eyes`), posts an in-progress reply,
  runs `cm-runner verify` under gVisor, and updates the reply in place with:
  - `🚨 Verified (VERIFIED)` — confirmed exploitable true positive.
  - `✅ Dismissed (DISMISSED)` — refuted as not exploitable (with dismissal reason).
  - `ℹ️ Inconclusive (OPEN)` — could neither confirm nor refute.
  - `⚠️ Verification failed` — container failure or cancellation (retryable).

### Action Inputs

| Input | Action | Default | Description |
| :--- | :--- | :--- | :--- |
| `gcp_project_id` | Both | — (**required**) | Google Cloud project ID used by CodeMender for Vertex AI API calls. |
| `gcp_workload_identity_provider` | Both | — (**required**) | Full Workload Identity Provider resource name (`projects/<num>/locations/global/workloadIdentityPools/<pool>/providers/<provider>`). |
| `gcp_service_account` | Both | — (**required**) | Service account email impersonated via Workload Identity Federation. |
| `fix` | Both | `'true'` | Run `cm-runner fix` for each finding and post suggestions/patches on the PR (`'false'` to scan only). |
| `max_parallel_fixes` | Both | `'4'` | Maximum number of `cm-runner fix` jobs/containers to run concurrently. |
| `diff_workers` | Both | `'8'` | Number of concurrent workers for `cm-runner find-diff` (`1`..`16`). |
| `log_level` | Both | `'info'` | `cm-runner` log level (`debug`, `info`, `warning`, `error`). |
| `artifact_retention_days` | Both | `'3'` | Days to retain the uploaded `codemender-report-pr-<N>` artifact (`report.sarif`, `report.md`, `findings/*.sarif`, `fixes/*.diff`, `verified/*.sarif`). |
| `gcs_transit_bucket` | `codemender` | `''` | GCS bucket used for transit storage and report staging in Cloud Run mode (defaults to `CODEMENDER_GCS_TRANSIT_BUCKET` or `codemender-reports-<project>`). |
| `cloud_run_job_name` | `codemender` | `'codemender-runner'` | Cloud Run Job name to execute in Cloud Run mode. |
| `cloud_run_region` | `codemender` | `'us-central1'` | GCP region for the Cloud Run Job. |
| `runner_image` | `codemender-local` | `ghcr.io/<action-or-workflow-repo>:latest` | Container image for Local Docker mode (built from the root `Dockerfile`). |
| `registry_token` | `codemender-local` | *(workflow token)* | Optional token used only to pull `runner_image` from GHCR when the workflow token cannot read the package. |
| `container_runtime` | `codemender-local` | `'runsc'` | `'runsc'` installs gVisor (`runsc`) required by `cm`'s inner sandbox; `'runc'` opts out to plain Docker. |
| `gvisor_release` | `codemender-local` | `'20260921.0'` | Pinned gVisor release (`yyyymmdd` or `yyyymmdd.N`) installed when `container_runtime` is `'runsc'`. |

### Required Workflow Permissions

| Permission | Purpose |
| :--- | :--- |
| `actions: read` | Download prior PR scan artifacts (`findings/*.sarif`) during on-demand verification. |
| `contents: read` | Check out the repository (`fetch-depth: 0` so the PR base commit is present for `find-diff`). |
| `id-token: write` | Request a GitHub OIDC token for Google Cloud Workload Identity Federation. |
| `packages: read` | Pull the `cm-runner` image from GHCR with the workflow token (`codemender-local`). |
| `pull-requests: write` | Post and update the CodeMender summary comment, inline review suggestions, and verification replies. |

### Fork Pull Requests (Private and Internal Repositories)

By default, GitHub grants fork pull request workflows a read-only token and no
secrets. For **private or internal** repositories where forks come from trusted
organization members, an administrator can enable fork PR scans under
**Settings → Actions → General → Fork pull request workflows in private
repositories**:

- Enable **Run workflows from fork pull requests**
- Enable **Send write tokens to workflows from pull requests**
- Enable **Send secrets and variables to workflows from pull requests**
- *(Optional)* Enable **Require approval for fork pull request workflows**

*(For public repositories, do not enable write tokens or secrets for untrusted
forks, and never use `pull_request_target` with a checkout of untrusted fork
code.)*

## Running the Unit Tests

```bash
pip install -e ./packages/codemender -e "./packages/launcher[cloudrun]" -e "./packages/repohost[github,gitlab]"
make test
```
