# CodeMender Runner

CodeMender Runner is an orchestrator for
[CodeMender](https://cloud.google.com/security/codemender) for integrating
CodeMender's advanced AI security scanning in headless, scaled environments such
as in CI/CD pipelines. This orchestration provides:

1. **Predefined, customizable Workflows:** Turnkey integration for common use
   cases.
   - Provides default workflows for common use cases such as scanning pull
     requests.
   - Easy setup via provided terraform configurations.
   - See [Supported Workflows](#supported-workflows) for this list of currently
     provided workflows.
   - See [Supported Environments](#supported-environments) for the environments
     currently supported.
1. **Building blocks for custom solutions:** Provide the toolkit for rapid
   adaptation to unique scenarios.
   - Provides modular components that can be adapted to each unique use case.
   - See [Component Overview](#component-overview) and
     [Supported Environments](#supported-environments) for more information.
1. **Stateless, ephemeral execution:** Manage the `cm` state so `find`, `fix`,
   and `verify` can run independently.
   - Supports horizontal scale & long timeouts to allow parallel `fix` or
     `verify`.
   - Enables multi-turn state across ephemeral runs so that `fix` or `verify`
     can be triggered asynchronously hours or even days after `find`.
1. **Defense-in-depth sandboxing:** Contain the powerful agent at multiple
   layers.
   - Utilize CodeMender’s native sandbox as a first line of isolation.
   - Strengthen this sandboxing with architecture support (e.g. VPC, gVisor) and
     environment isolation (dedicated projects, permission isolation).
   - Even if the agent escapes one layer of sandboxing, the impact is contained.

## How It Works

CodeMender Runner separates the lightweight orchestration plane (usually the
CI/CD runner) from sandboxed code execution. When the orchestration starts a
CodeMender job--e.g. on a Pull Request--the orchestrator packages the repository
checkout, dispatches the `cm-runner` container inside an isolated sandbox, and
posts the resulting findings and candidate patches to the appropriate location.

This is illustrated in the [Component Overview](#component-overview) section
below with the currently supported environments and workflows discussed in their
respective sections below.

### Component Overview

![Components](assets/components.svg)

CodeMender Runner uses four main components to keep repository credentials in
your CI control plane while isolating code execution inside an ephemeral
sandbox:

1. **Repo Hosting:** Your source control platform (e.g., GitHub, GitLab,
   Bitbucket). Pull/Merge Request events trigger CI runs, and the host's Status
   & Comments API surfaces scan summaries, commit statuses, and 1-click fix
   suggestions.
1. **Action Runner:** The lightweight orchestrator running inside your CI/CD
   pipeline (e.g., GitHub Actions, GitLab Runners). It checks out code,
   authenticates via Workload Identity Federation (WIF), stages inputs to the
   Artifact Cache, dispatches sandbox jobs, and publishes results back to the
   repository host.
1. **Artifact Cache:** Shared storage (Cloud Storage or local files) that passes
   **PR Diffs**, **Source Snapshots**, **SARIF Reports**, and **Fix Patches**
   between the Action Runner and the sandbox so `find`, `fix`, and `verify` can
   share state across independent runs.
1. **Isolated Execution Environment:** An ephemeral sandbox container (e.g.,
   Cloud Run, GKE, or local Docker + gVisor) that runs with zero repository
   credentials. Inside the container, **CM Runner** reads staged inputs from the
   Artifact Cache, runs the **CodeMender CLI** (`cm find --diff`, `cm verify`,
   `cm fix`), and writes reports and patches back to the cache.

### Supported Environments

CodeMender Runner pairs your CI/CD orchestration platform with your preferred
sandboxed execution environment:

- **Orchestration Platforms (CI/CD):**
  - **[GitHub Actions](.github/SETUP.md):** Turnkey reusable workflows and
    composite actions for pull request scanning, patching, and verification.
  - **[Custom CI & CLI](packages/codemender/README.md):** Modular CLI and
    library building blocks
    ([`packages/launcher/`](packages/launcher/README.md),
    [`packages/repohost/`](packages/repohost/README.md)) for integrating with
    other CI systems or custom automation.
- **Sandboxed Execution Environments:**
  - **[Google Cloud Run Jobs](docs/user-guides/cloud-run.md):** Serverless,
    horizontally scalable container execution backed by Cloud Storage (`gs://`)
    and keyless Workload Identity Federation (WIF) via
    [Terraform](terraform/README.md).
  - **[Local Docker + gVisor](docs/user-guides/local-gvisor.md):** Runs the
    sandbox container directly on a dedicated CI runner VM using the gVisor
    (`runsc`) kernel and a local file cache (`file://`).
  - **[Custom Container Runtimes](docker/README.md):** Standard worker image
    portable to GKE or other container platforms.

### Supported Workflows

CodeMender Runner provides two turnkey Pull/Merge Request workflows out of the
box, along with modular building blocks for custom workflows:

- **Pull Request Scan & Fix (`find --diff` + `fix`):** On a pull request, scans
  the changed code against the base branch (`find --diff`), updates the PR
  summary comment and commit status check, and (when enabled) launches parallel
  sandbox jobs (`fix`)—one per finding—to generate a candidate patch and post a
  1-click PR review suggestion.
- **On-Demand Verification (`@cm-runner verify`):** Triggered asynchronously
  when a reviewer comments `@cm-runner verify` on a finding—even hours or days
  after the initial scan. Runs an exploitability check against the cached
  snapshot in the sandbox, replies on the review thread, and automatically
  dismisses false positives.
- **[MCP Server](packages/mcpserver/README.md) (`cm_find` / `cm_validate` /
  `cm_fix`):** A Cloud Run service that exposes CodeMender to MCP clients. Give
  it a repository name and it runs a full-repository scan in a new Cloud Run Job
  execution, stores every finding in BigQuery, and on request validates
  (`cm verify`) or fixes (`cm fix`) individual findings — updating BigQuery and
  pushing the patch as a branch / pull request.
- **[Custom Workflows](packages/codemender/README.md):** Compose the
  standalone `cm-runner` stages (`find`, `fix`, `verify`) and
  [`packages/launcher/`](packages/launcher/README.md) building blocks to build
  custom scanning, triage, or remediation pipelines.

## Getting Started

### Prerequisites

Before provisioning CodeMender Runner, confirm that you have:

- **Google Cloud Project:** A Google Cloud project with billing enabled and
  Vertex AI API access for CodeMender.
- **Terraform & Google Cloud SDK:** Terraform (`>= 1.5`) and the Google Cloud
  SDK (`gcloud`) authenticated (`gcloud auth application-default login`) for
  one-time infrastructure provisioning.
- **Repository Host Authentication *(Optional — for automated Terraform
  setup)*:** Credentials or CLI authentication for your source control platform
  (such as GitHub or GitLab) when using Terraform to automatically bootstrap
  repository or project CI secrets, variables, and labels.

### Quickstart (Terraform)

All workflows and supported environments are configurable via Terraform. Follow
our [Terraform Instructions](terraform/README.md) to quickly get started.

### Quickstart: CodeMender MCP Server on Cloud Run (`deploy.sh`)

[`deploy.sh`](deploy.sh) deploys the [MCP server](packages/mcpserver/README.md)
and everything it depends on in one command, and rolls out code updates later.

```text
MCP client ──HTTPS /mcp──▶ Cloud Run service  <prefix>-mcp
                             │ clone repo (GitHub token from Secret Manager)
                             │ stage snapshot + manifest ──▶ GCS transit bucket
                             │ run_job(overrides) ──▶ Cloud Run Job <prefix>-runner
                             │                       (new execution per find / validate / fix,
                             │                        no repository credentials)
                             ├──▶ BigQuery  codemender.jobs / codemender.findings
                             └──▶ GitHub    codemender/fix-<id> branch + pull request
```

**Prerequisites:** `gcloud` (logged in with `gcloud auth login` *and*
`gcloud auth application-default login`), Terraform `>= 1.5`, git and Python
`>= 3.8`. Docker and `make` are not needed — images are built with Cloud Build.
Works from Linux, macOS, Cloud Shell, WSL and Git Bash on Windows.

```bash
# 1. Preview — runs terraform plan, changes nothing
./deploy.sh --plan-only --project my-project --region us-central1

# 2. Deploy — prompts for a GitHub token (or Enter to skip) and asks before applying
./deploy.sh --project my-project --region us-central1

# 3. After code changes — rebuild images and roll the job + service only
./deploy.sh --code-only
```

A full deploy:

1. Enables the required APIs (Cloud Run, Cloud Build, Artifact Registry,
   BigQuery, Secret Manager, Cloud Storage, Vertex AI, IAM).
1. Creates Secret Manager secrets if missing: `codemender-github-token` (from
   `$GITHUB_TOKEN`, `--github-token-from-gh`, or a hidden prompt) and
   `codemender-mcp-auth-token` (a random bearer token for MCP clients). Secrets
   are piped over stdin and never written to disk or Terraform files.
1. Writes `terraform/terraform.tfvars` if it does not exist (gitignored), with
   `execution_mode = "cloud_run"`, `enable_mcp_server = true` and
   `runner_timeout = "10800s"` (full-repository scans outlast the 1h default).
1. Bootstraps the Artifact Registry repo, then builds the `codemender-runner`
   and `codemender-mcp` images with Cloud Build. Images are tagged with a
   content hash, so unchanged code is never rebuilt.
1. Runs `terraform plan`, shows it, and applies only after you confirm. It
   creates the runner Cloud Run Job, GCS bucket, BigQuery dataset, service
   accounts/IAM and the `codemender-mcp` Cloud Run service.
1. Prints the MCP endpoint, a health check and the client connection command.

| Option | Purpose |
|---|---|
| `--plan-only` | `terraform plan` only; creates nothing |
| `--code-only` | Rebuild images, `gcloud run jobs/services update` |
| `--project`, `--region`, `--prefix` | Target project, region (default `us-central1`), name prefix (default `codemender`). Default from `terraform.tfvars`, then `gcloud config` |
| `--github-owner` | GitHub user/org that owns the repositories to scan (optional; auto-inferred from `terraform.tfvars`, `gh`, `GITHUB_TOKEN`, or git `origin`) |
| `--private` | No public access: IAM-only for your gcloud account (connect via `gcloud run services proxy`). Default is a public URL that requires the bearer token |
| `--github-token-from-gh` / `--rotate-github-token` | Use `gh auth token`; add a new secret version |
| `-y`, `--yes` | Non-interactive (CI); applies without prompting |

**Connect Claude Code** (printed at the end of the deploy):

```bash
claude mcp add --transport http codemender https://codemender-mcp-XXXX.run.app/mcp \
  --header "Authorization: Bearer $(gcloud secrets versions access latest --secret codemender-mcp-auth-token)"
```

Then ask, e.g. *"Use codemender to scan my-org/my-repo, validate the
high-severity findings and fix the verified ones."* The tools are `cm_find`,
`cm_validate`, `cm_fix`, `cm_apply_patch`, `cm_job_status`, `cm_list_findings`
and `cm_get_finding`. Results are queryable in BigQuery:

```bash
bq query --use_legacy_sql=false \
  'SELECT finding_id, severity, status, file_path, pr_url
   FROM `my-project.codemender.findings` ORDER BY created_at DESC LIMIT 20'
```

#### Scan any repository from GitHub Actions → BigQuery + GitHub issues

The [`CodeMender Repo Scan`](.github/workflows/cm-repo-scan.yml) workflow
(manual `workflow_dispatch`) takes a repository to clone and scan, plus the
repository that should receive the issues. It calls `cm_find` on the deployed
MCP server, which clones the repo, runs a full `cm find` in a new Cloud Run Job
execution and stores every finding in BigQuery. The workflow waits for the scan
and then opens one GitHub issue per finding, labelled `codemender` and
`severity:<level>`. Each issue carries a hidden fingerprint, so re-scans never
duplicate an issue that already exists, open or closed.

One-time setup in the repository that runs the workflow (*Settings → Secrets and
variables → Actions*):

| Name | Kind | Value |
|---|---|---|
| `CODEMENDER_MCP_URL` | variable | `terraform -chdir=terraform output -raw mcp_endpoint` |
| `CODEMENDER_MCP_TOKEN` | secret | `gcloud secrets versions access latest --secret codemender-mcp-auth-token` |
| `CODEMENDER_ISSUES_TOKEN` | secret | Only when issues go to *another* repo: fine-grained PAT with **Issues: read and write** on it |

```bash
gh workflow run cm-repo-scan.yml -R avnit/codemender-runner-mcp \
  -f repo=my-org/my-repo -f issues_repo=my-org/security-tracker -f min_severity=WARNING
```

| Input | Default | Purpose |
|---|---|---|
| `repo` | — (required) | Repository to clone and scan (`owner/repo` or URL). The MCP server's GitHub token must be able to clone it |
| `issues_repo` | `repo` | Repository the issues are created in |
| `ref` | default branch | Branch, tag or commit |
| `min_severity` | `NOTE` | Lowest SARIF level filed as an issue (`NOTE`/`WARNING`/`ERROR`); every finding still goes to BigQuery |
| `max_issues` | `50` | Cap on new issues per run |
| `create_issues` / `dry_run` | `true` / `false` | Scan + BigQuery only, or list the issues without creating them |
| `scan_job_id` | — | Publish an existing scan (e.g. after the 6h runner limit) instead of starting a new one |

The run summary shows the scan job, finding counts by severity and the created
issue links. The same logic runs locally:
`python scripts/cm_scan_issues.py --repo owner/repo --dry-run` (with
`CODEMENDER_MCP_URL` / `CODEMENDER_MCP_TOKEN` / `GITHUB_TOKEN` set).

See [`packages/mcpserver/README.md`](packages/mcpserver/README.md) for the tool
reference, BigQuery schema, finding lifecycle
(`OPEN` → `VERIFIED` / `FALSE_POSITIVE` / `INCONCLUSIVE` → `FIXED` → `PATCHED`)
and configuration.

### Step-by-Step Guided Setup

There are three steps for setting up CodeMender Runner, listed below. Each step
has its own dedicated guide linked to below. To follow the install steps, simply
follow the correct links for your particular environment. The setup steps are:

1. **Setup orchestration platform (CI/CD platform):** First we select and
   configure the control plane for our integration. This is normally your
   existing CI/CD platform.
1. **Provision your sandboxed execution environment:** Then we configure the
   sandboxed environment the `cm` binary executes in. This includes the
   authentication and authorization methods as well as setting up the
   ArtifactCache so that the orchestrator and the execution environment can
   communicate.
1. **Enable workflows on your repositories:** Finally, we tie it all together
   and enable our desired workflows.

### Step 1: Choose Your Orchestration Platform

Select your CI/CD platform or CLI workflow below to begin:

- [**GitHub Actions**](.github/SETUP.md)
- [**Custom CI**](packages/codemender/README.md)

## Component Reference

Looking for standalone component references? Jump directly to:

- **Execution Environment Guides:**
  - [Google Cloud Run Jobs](docs/user-guides/cloud-run.md)
  - [Local Docker + gVisor](docs/user-guides/local-gvisor.md)
- **Infrastructure & Container References:**
  - [Terraform Modules (`terraform/README.md`)](terraform/README.md)
  - [Container Packaging (`docker/README.md`)](docker/README.md)
- **Workspace Package Guides:**
  - [`packages/codemender/README.md`](packages/codemender/README.md)
  - [`packages/launcher/README.md`](packages/launcher/README.md)
  - [`packages/repohost/README.md`](packages/repohost/README.md)
  - [`packages/mcpserver/README.md`](packages/mcpserver/README.md)

## Changes in this fork: MCP Server

| Area | Change |
|---|---|
| [`packages/mcpserver/`](packages/mcpserver/README.md) | New `codemender-mcp` package: FastMCP server (streamable HTTP at `/mcp`, `/healthz`, optional bearer auth) that launches one Cloud Run Job execution per `find` / `validate` / `fix`, stores jobs and findings in BigQuery, and pushes fixes as branches/PRs. |
| `cm-runner find-full` | New runner command: full-repository scan (`cm find` without `--diff`, 3h timeout). Wired into [`cloud-run-jobs/entrypoint.py`](cloud-run-jobs/entrypoint.py). |
| [`docker/mcp.Dockerfile`](docker/mcp.Dockerfile), [`cloudbuild.mcp.yaml`](cloudbuild.mcp.yaml) | MCP server image (pinned Python base, runs as `nonroot`) and its Cloud Build config. |
| [`terraform/modules/mcp/`](terraform/modules/mcp), [`terraform/mcp.tf`](terraform/mcp.tf) | Opt-in (`enable_mcp_server = true`) Cloud Run service, service account with least-privilege IAM (`run.jobsExecutorWithOverrides` on the runner job only), BigQuery dataset, Secret Manager access. New `mcp_*` variables in [`terraform.tfvars.example`](terraform/terraform.tfvars.example). |
| [`deploy.sh`](deploy.sh), [`.gcloudignore`](.gcloudignore) | One-command deploy / plan / code update (above). Terraform state, plans and tfvars are never uploaded to Cloud Build. |
| [`.github/workflows/cm-repo-scan.yml`](.github/workflows/cm-repo-scan.yml), [`scripts/cm_scan_issues.py`](scripts/cm_scan_issues.py) | Manual workflow: scan any repo via the MCP server (findings → BigQuery), then file de-duplicated GitHub issues in a target repo. |
| [`Makefile`](Makefile) | `build-mcp`, `run-mcp` (local container with your ADC), `deploy-mcp` (Cloud Build + `gcloud run services update`). |
| CI | [`.github/workflows/ci.yml`](.github/workflows/ci.yml) installs `packages/mcpserver` so its tests run with `make test`. |
