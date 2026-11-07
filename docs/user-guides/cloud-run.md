# Google Cloud Run Jobs + GCS Cache (`gs://`)

## Overview

In Cloud Run mode (`execution_mode = "cloud_run"`), the CI/CD pipeline acts as a
lightweight orchestrator that never executes untrusted repository build commands
on the CI runner host. Instead, it stages repository snapshots in a Google Cloud
Storage (`gs://`) transit bucket and dispatches isolated Google Cloud Run Jobs
to run `find-diff`, `fix`, and `verify`.

For architecture specifications, see
[SPEC-WIF](../openspec/specs/wif/spec.md),
[SPEC-CACHE](../openspec/specs/artifact-cache/spec.md), and
[Security and Trust Zones](../openspec/specs/tech/systems/security-and-trust-zones/design.md).

## How It Executes

1. **Keyless WIF Authentication:** The CI runner exchanges its OIDC JWT for a
   short-lived Google Cloud token impersonating the WIF caller service account
   (`codemender-wif-sa`).
2. **GCS Transit & Artifact Cache (`gs://`):** Source checkout archives and scan
   artifacts are uploaded to a dedicated Google Cloud Storage transit bucket
   (`gs://<prefix>-reports-<suffix>`) configured with an automatic object
   lifecycle TTL (`reports_bucket_ttl_days = 7` by default).
3. **Dual-SA Isolation Boundary
   ([Security and Trust Zones Decision 3](../openspec/specs/tech/systems/security-and-trust-zones/design.md#decision-3-mode-switched-least-privilege-gcp-wif-local-single-sa-vs-cloud_run-coordinatorworker-dual-sa-split)):**
   - **WIF Caller SA (`codemender-wif-sa`):** Granted only
     `roles/run.developer`, `roles/storage.objectUser` on the transit bucket,
     and `roles/iam.serviceAccountUser` (`actAs`) on the Cloud Run runtime
     service account (`codemender-runner-sa`).
   - **Cloud Run Runtime SA (`codemender-runner-sa`):**
     Attached to the isolated Cloud Run Job container (`codemender-runner`);
     granted `roles/aiplatform.user`, `roles/logging.logWriter`, and
     `roles/storage.objectUser` on the transit bucket to execute `find-diff`,
     `fix`, and `verify` with **zero repository host credentials** inside the
     container environment.

## Terraform Configuration

To provision Workload Identity Federation (`module.wif`), the GCS transit
bucket, Artifact Registry, Cloud Run Jobs, and least-privilege service accounts
(`module.gcp`), set the following in `terraform/terraform.tfvars`:

```hcl
execution_mode = "cloud_run"

# Optional Cloud Run settings (defaults shown)
# reports_bucket_ttl_days = 7
# auto_build_image        = true
# runner_cpu              = "2000m"
# runner_memory           = "8Gi"
# runner_timeout          = "3600s"
# create_vpc_and_nat      = false
```

See the
[Terraform Infrastructure Guide (`../../terraform/README.md`)](../../terraform/README.md)
for provisioning instructions, VPC/Cloud NAT networking options, and the full
input and output reference tables.

## Manual Setup Steps (Under the Hood)

> [!NOTE]
> You do not need to run these steps manually when using the
> [Terraform module (`../../terraform/README.md`)](../../terraform/README.md)
> (`execution_mode = "cloud_run"`). They are documented here so you can
> understand what is provisioned and wired under the hood.

### 1. Dual-SA IAM & Workload Identity Federation

- **WIF Caller Service Account (`codemender-wif-sa`):** Provision a Workload
  Identity Pool and OIDC Provider trusting your CI platform's OIDC issuer, bind
  `roles/iam.workloadIdentityUser` to `codemender-wif-sa`, and grant
  `codemender-wif-sa`:
  - `roles/run.developer` (to trigger and monitor Cloud Run Job executions),
  - `roles/storage.objectUser` on the GCS transit bucket, and
  - `roles/iam.serviceAccountUser` (`iam.serviceAccounts.actAs`) on the Cloud
    Run runtime service account (`codemender-runner-sa`).
- **Cloud Run Runtime Service Account (`codemender-runner-sa`):** Create a
  dedicated runtime service account for the `codemender-runner` Cloud Run Job
  and grant it `roles/aiplatform.user`, `roles/logging.logWriter`, and
  `roles/storage.objectUser` on the GCS transit bucket.

### 2. GCS Transit & Artifact Cache Bucket (`gs://`)

- Create a regional Google Cloud Storage bucket (`<prefix>-reports-<suffix>`)
  with uniform bucket-level access enabled and a lifecycle rule deleting objects
  older than `reports_bucket_ttl_days` (default `7` days).

### 3. Artifact Registry & Cloud Run Jobs

- Create an Artifact Registry Docker repository (`<prefix>-runner`) in your
  target region.
- Build and push the Cloud Run wrapper image
  ([`docker/cloudrun.Dockerfile`](../../docker/cloudrun.Dockerfile) layered on
  [`docker/runner.Dockerfile`](../../docker/runner.Dockerfile)) to Artifact
  Registry via Google Cloud Build (`cloudbuild.yaml` / `make deploy-cloudrun`;
  see
  [Building Images in `../../docker/README.md`](../../docker/README.md#building-images)).
- Create the `codemender-runner` Cloud Run Job attached to
  `codemender-runner-sa` and pointing to the Artifact Registry image.

### 4. Configure Your Orchestration Platform

- Configure your CI/CD orchestration platform with the provisioned Workload
  Identity Provider resource name, caller service account email, Google Cloud
  project ID, GCS transit bucket name, Cloud Run Job name, and Google Cloud
  region.
- Return to your orchestration platform setup guide in
  [Choose Your Orchestration Platform (`../../README.md`)](../../README.md#step-1-choose-your-orchestration-platform)
  to enable workflows on your repositories.
