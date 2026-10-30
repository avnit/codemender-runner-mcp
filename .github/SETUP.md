# CodeMender GitHub Actions — Setup & Usage Guide

Scans each pull request with CodeMender (`cm-runner find-diff`) and generates
candidate patches (`cm-runner fix`), uploads SARIF and Markdown reports as
workflow artifacts, and posts a summary comment and inline review suggestions on
the pull request.

See [SPEC-GHA](../docs/openspec/specs/github-actions/spec.md),
[SPEC-GHA-FIX](../docs/openspec/specs/github-actions-fix/spec.md), and
[SPEC-DIST](../docs/openspec/specs/distribute/spec.md) for architecture and
security model specifications.

Follow the four steps below to set up CodeMender on GitHub Actions:

1. [**Step 1 — Verify Prerequisites**](#step-1--verify-prerequisites)
2. [**Step 2 — Provision Your Execution
   Environment**](#step-2--provision-your-execution-environment)
   ([Google Cloud Run Jobs](../docs/user-guides/cloud-run.md) or
   [Local Docker + gVisor](../docs/user-guides/local-gvisor.md))
3. [**Step 3 — Enable CodeMender on Your Repositories**](#step-3)
   (Organization-Wide Repository Rulesets or Per-Repository Caller Workflows)
4. [**Step 4 — Configure Automated Fixes & On-Demand
   Verification**](#step-4--configure-automated-fixes--on-demand-verification)

<a id="prerequisites"></a>

## Step 1 — Verify Prerequisites

Before provisioning infrastructure for GitHub Actions, confirm that you have:

- [**General CodeMender Prerequisites**](../README.md#prerequisites): Confirm
  your Google Cloud project, Vertex AI API access, Terraform (`>= 1.5`), and
  `gcloud` authentication are set up.
- **GitHub Repository or Organization Permissions:** Admin access to the target
  GitHub repository (or GitHub Organization) with GitHub Actions enabled so you
  can configure Actions secrets/variables and enable caller workflows or
  Organization Repository Rulesets.
- **GitHub Authentication for Automated Terraform Bootstrap *(Optional)*:** If
  you populate `github_repositories` in `terraform.tfvars` so Terraform
  automatically provisions GitHub Actions secrets, variables, and the
  `codemender-scan` label, authenticate to GitHub using one of the supported
  methods in
  [`../terraform/README.md#github-authentication`](../terraform/README.md#github-authentication):
  - An authenticated GitHub CLI session (`gh auth login`) or `GITHUB_TOKEN`
    environment variable, or
  - A Personal Access Token (`github_mgmt_token` with `repo` scope or
    fine-grained repository Secrets/Variables/Issues permissions).

## Step 2 — Provision Your Execution Environment

CodeMender executes candidate patch builds and verification inside an isolated
sandbox container. Choose **one** of the two execution environments below to
provision Workload Identity Federation, build or configure your runner container
image, and set up your GitHub Actions secrets and variables, then return to
[Step 3](#step-3):

| Google Cloud Run Jobs + GCS Cache (Recommended) | Local Docker + gVisor on the Runner VM |
| :--- | :--- |
| [**Open the Cloud Run Setup Guide (`../docs/user-guides/cloud-run.md`) →**](../docs/user-guides/cloud-run.md) | [**Open the Local Docker + gVisor Setup Guide (`../docs/user-guides/local-gvisor.md`) →**](../docs/user-guides/local-gvisor.md) |
| Dispatches isolated serverless Google Cloud Run Jobs (`execution_mode = "cloud_run"`) and stages source archives and reports in a GCS bucket (`gs://`). Default backend for `.github/actions/codemender`. | Runs the `cm-runner` container directly on the GitHub Actions runner VM under Docker with gVisor (`runsc`) kernel isolation (`execution_mode = "local"`, `.github/actions/codemender-local`). |

- **GitHub Actions Secrets & Variables:** When `github_repositories` is
  populated in `terraform.tfvars`, Terraform automatically creates the required
  repository secrets, variables, and the `codemender-scan` label. For manual
  setup or organization-level configurations (`github_scope_type = "org"` with
  `github_repositories = []`), configure the secrets and variables listed in
  [Bootstrapped GitHub Repository Resources (`../terraform/README.md`)](../terraform/README.md#bootstrapped-github-repository-resources-terraformmoduleswifgithubtf)
  (and optionally set the `CODEMENDER_RUNNER_IMAGE` variable when using a custom
  container image with `.github/actions/codemender-local`).
- **Standalone Infrastructure & Container Reference:** See
  [`../terraform/README.md`](../terraform/README.md) and
  [`../docker/README.md`](../docker/README.md).

<a id="step-3"></a>

## Step 3 — Enable CodeMender on Your Repositories

Once your execution environment, secrets, and variables are configured in Step
2, choose how to roll out CodeMender workflows to your repositories:

| Rollout Approach | Files in Target Repo | Supported Event Triggers | Action & Package Version Isolation |
| :--- | :--- | :--- | :--- |
| **Organization-Wide Rollout (Fork + Repository Rulesets)** | **0 files** | `pull_request` / `push` / `merge_group` (**PR scan `find-diff` + automatic inline `fix` suggestions**) | Isolated to `<your-org>/codemender-runner` after running `make init-fork` (or pin `@<tag/sha>`) |
| **Per-Repository Caller Workflow (`workflow_call` or Composite Action `uses:`)** | 1 thin `.github/workflows/codemender.yml` file (`.github/actions/*` and `packages/` stay centralized) | `pull_request` (**scan + fix**) **and** `pull_request_review_comment` (**on-demand `@cm-runner verify`**) | Pin `@<tag/sha>` or reference `<your-org>/codemender-runner@main` |

### Organization-Wide Rollout (Fork + Repository Rulesets)

To enforce CodeMender pull request scanning (`find-diff`) and automatic fix
suggestions (`fix`) across multiple repositories in a GitHub Organization with
**zero workflow files** added to target repositories:

1. **Fork `codemender-runner` into your organization
   (`<your-org>/codemender-runner`) and update workflow action references:** Run
   the fork ownership helper in your fork clone so
   `.github/workflows/cm-merge-scan.yml` references the actions in
   `<your-org>/codemender-runner` instead of the upstream repository:
   ```bash
   ./scripts/set-fork-owner.sh <your-org>
   # or auto-detect <owner>/<repo> from `git remote get-url origin`:
   make init-fork
   ```
   Commit and push the updated `.github/workflows/cm-merge-scan.yml` file to
   your fork's default branch.
   - **Why this matters for change isolation:** If you leave
     `uses: cloud-gtm/codemender-runner/.github/actions/*@main` unedited, GitHub
     resolves `@main` against the upstream repository live on every workflow
     run—so upstream changes immediately affect your runs even if you have not
     synced your fork. Running `make init-fork` (or pinning `@<ref>` to a
     release tag/commit SHA) ensures your workflows only update when you
     explicitly sync your fork.
   - **Private / Internal forks:** If your fork is **Private** or **Internal**,
     navigate in your fork's GitHub settings to **Settings → Actions → General →
     Access** and select **Accessible from repositories in the organization** so
     other repositories in `<your-org>` can invoke your fork's workflows and
     composite actions.
   - **Local Docker + gVisor users:** `.github/workflows/cm-merge-scan.yml`
     defaults to Mode 1 (`codemender` on Google Cloud Run Jobs). To run Mode 2
     (`codemender-local` on the GitHub Actions runner VM with gVisor), uncomment
     the `codemender-local` step and comment out the `codemender` step in
     `.github/workflows/cm-merge-scan.yml` before committing.
2. **Configure Google Cloud credentials** at the organization or repository
   level as shown in
   [Step 2](#step-2--provision-your-execution-environment). *(Repositories in
   the organization that have not configured these credentials automatically
   emit a `::notice::` and exit `0` so their pull requests are not blocked.)*
3. **Enable the Organization Repository Ruleset:** In your GitHub Organization
   settings, navigate to **Settings → Rules → Rulesets → New branch ruleset**:
   - Target your desired repositories and default branch (`main` / `master`).
   - Enable **Require workflows to pass before merging** and select
     `.github/workflows/cm-merge-scan.yml` from `<your-org>/codemender-runner`.

> [!NOTE]
> **Organization Ruleset event scope vs. PR review comment triggers
> (`@cm-runner verify`):** GitHub Organization Repository Rulesets only trigger
> required workflows on branch/merge-gate events (`pull_request`, `push`,
> `merge_group`). Both **PR diff scanning (`find-diff`)** and **automatic inline
> fix suggestions (`fix`)** run during `pull_request` events and work with
> **zero workflow files** in target repositories. Because GitHub Organization
> Rulesets ignore `pull_request_review_comment` events, on-demand
> `@cm-runner verify` replies require adding a thin caller workflow in the
> target repository (see
> [Per-Repository Caller Workflow](#per-repository-caller-workflow)). Even when
> a caller workflow is used in the target repository, `.github/actions/*` and
> `packages/` remain centralized in `codemender-runner` and are never copied.

### Per-Repository Caller Workflow

Use a thin caller workflow in the target repository when:

- You are on a **personal GitHub account** (where Organization Rulesets are
  unavailable) or rolling out to a **single repository**, or
- Your repository needs **on-demand `@cm-runner verify` commands** triggered on
  `pull_request_review_comment` events.

Configure the secrets and variables from
[Step 2](#step-2--provision-your-execution-environment) in your repository, then
add `.github/workflows/codemender.yml` using either a reusable workflow call or
a direct composite action step (pinning `@<ref>` to a release tag/commit SHA or
referencing `<your-org>/codemender-runner` to isolate from live upstream `@main`
changes):

#### Call the Reusable Workflow (`workflow_call`)

Because GitHub Actions only permits `secrets: inherit` when calling a reusable
workflow inside the same GitHub organization or personal account, fork
`codemender-runner` into `<your-org-or-user>/codemender-runner` (and run
`make init-fork`), then create `.github/workflows/codemender.yml` in your
repository (or, to reference `cloud-gtm/codemender-runner` directly without a
fork, use
[Invoke the Composite Action Directly (`uses:`)](#invoke-the-composite-action-directly-uses)
below):

```yaml
name: CodeMender

on:
  pull_request:
    branches: [main, master]
  # Optional: uncomment to enable on-demand `@cm-runner verify` replies
  # pull_request_review_comment:
  #   types: [created]

permissions:
  actions: read
  contents: read
  id-token: write
  packages: read
  pull-requests: write

jobs:
  codemender:
    uses: <your-org-or-user>/codemender-runner/.github/workflows/cm-merge-scan.yml@main
    secrets: inherit
```

#### Invoke the Composite Action Directly (`uses:`)

To run the composite action as a step inside your own workflow job (using
`codemender` for Cloud Run mode or `codemender-local` for Local Docker + gVisor
mode):

```yaml
name: CodeMender

on:
  pull_request:
    branches: [main, master]
  pull_request_review_comment:
    types: [created]

permissions:
  actions: read
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
          ref: ${{ github.event.comment.commit_id || github.event.pull_request.head.sha }}
          fetch-depth: 0
          persist-credentials: false

      # Cloud Run mode (default):
      - uses: cloud-gtm/codemender-runner/.github/actions/codemender@main
        with:
          gcp_project_id: ${{ vars.CODEMENDER_GCP_PROJECT_ID }}
          gcp_workload_identity_provider: ${{ secrets.CODEMENDER_WORKLOAD_IDENTITY_PROVIDER }}
          gcp_service_account: ${{ secrets.CODEMENDER_SERVICE_ACCOUNT }}
          # Optional: defaults to repo variables or standard names when omitted
          # gcs_transit_bucket: ${{ vars.CODEMENDER_GCS_TRANSIT_BUCKET }}
          # cloud_run_job_name: ${{ vars.CODEMENDER_CLOUD_RUN_JOB_NAME }}
          # cloud_run_region: ${{ vars.CODEMENDER_GCP_REGION }}

      # Or Local Docker + gVisor mode:
      # - uses: cloud-gtm/codemender-runner/.github/actions/codemender-local@main
      #   with:
      #     gcp_project_id: ${{ vars.CODEMENDER_GCP_PROJECT_ID }}
      #     gcp_workload_identity_provider: ${{ secrets.CODEMENDER_WORKLOAD_IDENTITY_PROVIDER }}
      #     gcp_service_account: ${{ secrets.CODEMENDER_SERVICE_ACCOUNT }}
      #     # Optional: defaults to ghcr.io/<action-repo>:latest when omitted
      #     # runner_image: ${{ vars.CODEMENDER_RUNNER_IMAGE }}
```

## Step 4 — Configure Automated Fixes & On-Demand Verification

After enabling workflows on your repositories, you can customize how CodeMender
verifies candidate patches (`.codemender.yaml`), trigger on-demand
exploitability verification on pull request review comments (`@cm-runner
verify`), and tune action inputs and permissions.

### Automatic Fixes (`fix: 'true'`)

- **Enabled by default:** After `find-diff` reports findings, the action runs
  `cm-runner fix` for each finding and updates the PR summary table's `Fix`
  column. Set `fix: 'false'` in the action `with:` block to run only the scan.
- **How fixes are posted on the PR** (see
  [ADR-0006](../docs/adr/ADR-0006-pr-remediation-routing-and-security-gate.md)):
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
  | `❌ No fix` | `cm-runner fix` exited with code `3` (could not produce or verify a patch). Does not fail the job. |
  | `⚠️ Failed` | The fix run failed or timed out. Does not fail the job; see the collapsible `::group::` in the job log. |

### Repository Configuration (`.codemender.yaml`)

When `cm-runner init` prepares the container environment, it merges any
repository-root `.codemender.yaml` (or `codemender.yaml`, `.codemender.yml`,
`codemender.yml`) into `~/.codemender/config.yaml`:

- **`build.command`:** Shell command used by `cm-runner fix` to compile and test
  candidate patches inside the offline (`permissive-closed`) sandbox. Setting
  the `CODEMENDER_BUILD_COMMAND` environment variable overrides `build.command`.
- **Additional supported sections:** `project_paths` (normalized to absolute
  paths inside the checkout), `vcs` (deep-merged with `vcs.commands`), `scan`,
  `output`, `tools`, `sandbox`, and `security`.

Example `.codemender.yaml`:

```yaml
build:
  command: "pytest -q"
project_paths:
  - "."
```

### On-Demand Verification (`@cm-runner verify`)

- **Trigger (`pull_request_review_comment` only):** Reply `@cm-runner verify`
  inside any CodeMender fix review comment thread on a pull request. On-demand
  verification triggers exclusively on `pull_request_review_comment` events on
  PR review threads.
- **Authorization:** Only repository owners, organization members, and
  collaborators (`OWNER`, `MEMBER`, `COLLABORATOR`) can trigger verification.
  Comments from bots, external contributors, or cross-repository fork PRs are
  ignored.
- **Deduplication & concurrency:**
  - Each `@cm-runner verify` comment runs in its own GitHub Actions workflow run
    (up to `MAX_CONCURRENT_VERIFIES = 4` concurrent verifications per PR).
  - If verification is already `in-progress` for that finding, duplicate
    requests are skipped during pre-flight (`cm-github --preflight`) before WIF
    authentication, gVisor installation, or image pull.
  - If the finding was already verified (`VERIFIED`, `DISMISSED`, or `OPEN`),
    CodeMender replies with a link to the existing verification result without
    re-running the container.
- **Outcomes:** CodeMender reacts with 👀 (`eyes`), posts an in-progress reply,
  runs `cm-runner verify` inside the isolated sandbox, and updates the reply in
  place with:
  - `🚨 Verified (VERIFIED)` — confirmed exploitable true positive.
  - `✅ Dismissed (DISMISSED)` — refuted as not exploitable (with dismissal
    reason).
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
| `gcs_transit_bucket` | `codemender` | `''` | GCS bucket used for transit storage and report staging in Cloud Run mode (defaults to `vars.CODEMENDER_GCS_TRANSIT_BUCKET` or `codemender-reports-<project>`). |
| `cloud_run_job_name` | `codemender` | `''` | Cloud Run Job name to execute in Cloud Run mode (defaults to `vars.CODEMENDER_CLOUD_RUN_JOB_NAME` or `'codemender-runner'`). |
| `cloud_run_region` | `codemender` | `''` | GCP region for the Cloud Run Job (defaults to `vars.CODEMENDER_GCP_REGION` or `'us-central1'`). |
| `runner_image` | `codemender-local` | `ghcr.io/<action-or-workflow-repo>:latest` | Container image for Local Docker mode (built from [`../docker/runner.Dockerfile`](../docker/runner.Dockerfile); see [`../docker/README.md`](../docker/README.md)). |
| `registry_token` | `codemender-local` | *(workflow token)* | Optional token used only to pull `runner_image` from GHCR when the workflow token cannot read the package. |
| `container_runtime` | `codemender-local` | `'runsc'` | `'runsc'` installs gVisor (`runsc`) required by `cm`'s inner sandbox; `'runc'` opts out to plain Docker. |
| `gvisor_release` | `codemender-local` | `'20260921.0'` | Pinned gVisor release (`yyyymmdd` or `yyyymmdd.N`) installed when `container_runtime` is `'runsc'`. |

### Permissions & Fork Pull Request Security

<details>
<summary><strong>Required Workflow Permissions</strong></summary>

| Permission | Purpose |
| :--- | :--- |
| `actions: read` | Download prior PR scan artifacts (`findings/*.sarif`) during on-demand verification. |
| `contents: read` | Check out the repository (`fetch-depth: 0` so the PR base commit is present for `find-diff`). |
| `id-token: write` | Request a GitHub OIDC token for Google Cloud Workload Identity Federation. |
| `packages: read` | Pull the `cm-runner` image from GHCR with the workflow token (`codemender-local`). |
| `pull-requests: write` | Post and update the CodeMender summary comment, inline review suggestions, and verification replies. |

</details>

<details>
<summary>
<strong>Fork Pull Requests (Private and Internal Repositories)</strong>
</summary>

By default, GitHub grants fork pull request workflows a read-only token and no
secrets. For **private or internal** repositories where forks come from trusted
organization members, an administrator can enable fork PR scans under **Settings
→ Actions → General → Fork pull request workflows in private repositories**:

- Enable **Run workflows from fork pull requests**
- Enable **Send write tokens to workflows from pull requests**
- Enable **Send secrets and variables to workflows from pull requests**
- *(Optional)* Enable **Require approval for fork pull request workflows**

*(For public repositories, do not enable write tokens or secrets for untrusted
forks, and never use `pull_request_target` with a checkout of untrusted fork
code.)*

</details>

