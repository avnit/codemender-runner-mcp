# Container Packaging (`docker/`)

## Overview

The `docker/` directory contains the container image definitions used by
CodeMender Runner across supported execution environments:

| Dockerfile | Purpose | Used By |
| :--- | :--- | :--- |
| [`runner.Dockerfile`](runner.Dockerfile) | Standalone `cm-runner` execution image packaging the CodeMender CLI (`cm`), language build toolchains, and the [`packages/codemender`](../packages/codemender/README.md) runner package. | [Local Docker + gVisor](../docs/user-guides/local-gvisor.md), standalone CLI runs, and as the base image (`BASE_IMAGE`) for layered runtime images. |
| [`cloudrun.Dockerfile`](cloudrun.Dockerfile) | Cloud Run Jobs wrapper image layered on top of `runner.Dockerfile` that stages inputs and outputs between Google Cloud Storage (`gs://`) and `cm-runner` via [`../cloud-run-jobs/entrypoint.py`](../cloud-run-jobs/entrypoint.py). | [Google Cloud Run Jobs](../docs/user-guides/cloud-run.md). |

For more information, see:

- **Architecture Specification:**
  [SPEC-CONTAINER](../docs/openspec/specs/runner-container-packaging/spec.md)
- **Execution Environment Guides:**
  - [Google Cloud Run Jobs + GCS Cache (`gs://`)](../docs/user-guides/cloud-run.md)
  - [Local Docker + gVisor (`runsc`)](../docs/user-guides/local-gvisor.md)

## Building Images

All Docker builds use the **repository root** (`.`) as the build context so
workspace packages and entrypoints resolve cleanly.

### 1. Local Builds via `Makefile`

Run the following targets from the repository root (see
[`../Makefile`](../Makefile)):

```bash
# Build base runner image (tagged codemender-runner:local)
make build-runner

# Build Cloud Run wrapper image (tagged codemender-cloudrun:local)
make build-cloudrun
```

Verify the container contract locally:

```bash
# Verify default entrypoint (cm-runner) and default command (version)
docker run --rm codemender-runner:local version

# Run a diff scan locally under the gVisor (runsc) runtime
docker run --rm --runtime=runsc --cap-add SYS_ADMIN \
  -v "$(pwd):/workspace" \
  -v "$(pwd)/out:/out" \
  -v "$HOME/.config/gcloud/application_default_credentials.json:/creds/adc.json:ro" \
  -e GOOGLE_APPLICATION_CREDENTIALS=/creds/adc.json \
  -e GOOGLE_CLOUD_PROJECT="$GOOGLE_CLOUD_PROJECT" \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0=/workspace \
  codemender-runner:local find-diff \
  --repo /workspace \
  --base HEAD~1 \
  --out /out
```

### 2. Google Cloud Build & Cloud Run Deployment (`make deploy-cloudrun`)

When `execution_mode = "cloud_run"` and `auto_build_image = true` (default),
Terraform automatically submits [`../cloudbuild.yaml`](../cloudbuild.yaml) to
Google Cloud Build and pushes both `:latest` and a deterministic 12-character
SHA-256 content tag (`IMAGE_TAG`) to Artifact Registry.

To rebuild and update the `codemender-runner` Cloud Run Job after modifying
`packages/codemender` or `docker/`:

```bash
# Reads gcp_project_id, gcp_region, and resource_prefix from terraform/terraform.tfvars
make deploy-cloudrun

# Or pass project and region explicitly:
make deploy-cloudrun GCP_PROJECT_ID=my-gcp-project GCP_REGION=us-central1
```

### 3. Automated GitHub Container Registry Publishing (`ghcr.io`)

[`../.github/workflows/build_runner_image.yml`](../.github/workflows/build_runner_image.yml)
builds [`runner.Dockerfile`](runner.Dockerfile) on pushes to `main`, `v*` tags,
or manual `workflow_dispatch` runs, verifies the `["cm-runner"]` entrypoint and
Python package imports, and publishes:

- `ghcr.io/<owner>/<repo>:latest` (e.g. `ghcr.io/cloud-gtm/codemender-runner`;
  in a fork, `ghcr.io/<you>/<fork-name>`), the default image of the
  `codemender-local` action
- `ghcr.io/<owner>/<repo>:<commit-sha>`
- Optional custom target URI when the `CODEMENDER_RUNNER_IMAGE` repository
  variable is set.

The workflow has `packages: write`, so the repository's own `GITHUB_TOKEN`
creates and updates `ghcr.io/<owner>/<repo>` with no setup. To push to a
package that already exists and is linked to **another** repository (the push
fails with `permission_denied: write_package`), either grant this repository
**Write** under the package's *Settings → Manage Actions access*, or add a
`CODEMENDER_GHCR_TOKEN` repository secret: a classic PAT of the package owner
with the `write:packages` scope. The workflow then logs in with that token.
