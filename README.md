# CodeMender Runner

> [!NOTE]
> While we hold this code to a high standard, these are canonical reference
> implementations and should not be blindly applied in a production repository.
> Review and adapt the workflows, container configurations, and Terraform
> modules to fit your organization's security and infrastructure requirements.

`codemender-runner` provides reference CI/CD integrations, sandbox launchers
(Google Cloud Run Jobs and Local Docker + gVisor), and Terraform modules for
running Google CodeMender automated security scanning (`find-diff`) and
automated patching (`fix`) on pull and merge requests.

## Quickstart by Deployment Platform

Choose the setup guide for your CI/CD platform or execution environment:

| Platform / Use Case | Setup & Usage Guide | Execution Modes Supported |
| :--- | :--- | :--- |
| **GitHub Actions** (Organization Rulesets, `workflow_call`, or Composite Action) | [**GitHub Actions Setup & Usage Guide (`.github/README.md`)**](.github/README.md) | Cloud Run Jobs (default) or Local Docker + gVisor |
| **GitLab CI** (Merge Request Pipelines) | [**GitLab CI Setup & Usage Guide (`gitlab-ci/README.md`)**](gitlab-ci/README.md) | Local Docker + gVisor on a dedicated runner |
| **Standalone CLI / Custom CI** (`cm-runner`) | [**Calling `cm-runner` Directly (`docs/guides/calling_cm_runner.md`)**](docs/guides/calling_cm_runner.md) | Container (`Dockerfile`) or local Python CLI |

- **GitHub Organization Repository Rulesets (0 files in target repos):** Enforces `pull_request` diff scanning (`find-diff`) and automatic inline fix suggestions (`fix`) across an organization. Run `make init-fork` in your fork (or pin `@<ref>` to a tag/SHA) to isolate your workflows from live upstream `@main` changes.
- **GitHub Per-Repository Caller Workflows (`workflow_call` / composite action `uses:`):** Used for personal accounts, single-repository rollouts, or PR-comment-triggered verification (`@cm-runner verify` on `pull_request_review_comment`, which GitHub Organization Rulesets do not trigger) while keeping `.github/actions/*` and `packages/` centralized in `codemender-runner`.

## Shared Infrastructure & Container Packaging

### 1. Provisioning Google Cloud & Workload Identity Federation (`terraform/`)

The root [`terraform/`](terraform/) configuration provisions keyless Workload
Identity Federation (`terraform/modules/wif`) and, when `execution_mode = "cloud_run"`,
Google Cloud Run Jobs, a GCS transit bucket, and least-privilege service
accounts (`terraform/modules/gcp`):

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your gcp_project_id, github_scope_type, github_owner, and execution_mode
terraform init
terraform plan
terraform apply
```

See the [GitHub Actions Setup & Usage Guide (`.github/README.md`)](.github/README.md#step-1--provision-google-cloud--workload-identity-federation-terraform)
for full variable documentation and manual secret/variable setup.

### 2. Building the `cm-runner` Container Image (`Dockerfile`)

The root [`Dockerfile`](Dockerfile) builds the universal `cm-runner` container
image (`ENTRYPOINT ["cm-runner"]`, `CMD ["version"]`) pre-baked with Python 3.11,
Node.js 22, OpenJDK 17, Go 1.22, and C/C++ toolchains:

```bash
COMMIT_SHA="$(git rev-parse HEAD)"
IMAGE_URI="us-docker.pkg.dev/<GCP_PROJECT_ID>/<AR_REPO>/codemender-runner:${COMMIT_SHA}"

docker build -t "${IMAGE_URI}" .
docker run --rm "${IMAGE_URI}" version
```

For automated GitHub Container Registry (`ghcr.io`) builds or customizing
project dependencies in the [`Dockerfile`](Dockerfile), see
[`.github/README.md`](.github/README.md#step-3--build--publish-the-cm-runner-container-image).

## Repository Layout

| Path | Description |
| :--- | :--- |
| [`.github/`](.github/README.md) | GitHub Actions workflows (`.github/workflows/cm-merge-scan.yml`, `.github/workflows/build_runner_image.yml`) and composite actions (`.github/actions/codemender`, `.github/actions/codemender-local`). |
| [`gitlab-ci/`](gitlab-ci/README.md) | GitLab CI merge request pipeline template (`gitlab-ci/templates/codemender-scan.yml`) and setup guide. |
| [`terraform/`](terraform/) | Root Terraform module and child modules (`terraform/modules/wif`, `terraform/modules/gcp`) for GCP Workload Identity Federation, Cloud Run Jobs, and GitHub repository secret/variable bootstrapping. |
| [`packages/codemender/`](packages/codemender/) | Scenario-independent `cm-runner` CLI orchestrator (`codemender.runner`, `codemender.cm`, `codemender.sarif`, `codemender.vcs`). See [`docs/guides/calling_cm_runner.md`](docs/guides/calling_cm_runner.md). |
| [`packages/launcher/`](packages/launcher/) | Sandbox execution backends (`launcher.local` for Docker + gVisor, `launcher.cloudrun` for Google Cloud Run Jobs) and parallel execution interface (`launcher.base`). |
| [`packages/repohost/`](packages/repohost/) | Repository hosting integrations (`repohost.github`, `repohost.gitlab`) and the `cm-github` CLI entrypoint for PR sticky comments, inline review suggestions, and patch routing. |
| [`scripts/`](scripts/) | Operational helper scripts, including `scripts/set-fork-owner.sh` (`make init-fork`) for re-pointing workflow action references in forks. |
| [`docs/`](docs/) | Architecture Decision Records (`docs/adr/`), OpenSpec specifications (`docs/openspec/specs/`), and CLI guides (`docs/guides/`). |

## Development & Testing

Run the full unit test suite across all workspace packages:

```bash
make test
```

When operating a fork of this repository, re-point workflow action references
to your fork owner (so workflows resolve actions from your fork rather than
upstream `@main` live):

```bash
make init-fork
# or explicitly:
./scripts/set-fork-owner.sh <your-org>
```

## Contributing

1. Reach out to an existing administrator to be added as a contributor.
2. Open a pull request and request review from a maintainer or administrator.
3. All code must be reviewed by a maintainer or administrator and maintain 90%+
   test coverage across the test suites.
