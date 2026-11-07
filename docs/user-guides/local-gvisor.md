# Local Docker + gVisor (`runsc`)

## Overview

In Local Docker + gVisor mode (`execution_mode = "local"`), the `cm-runner`
container executes directly on the CI runner VM under Docker using the gVisor
(`runsc`) user-space kernel runtime for sandbox isolation and a local filesystem
artifact cache (`file://`).

For architecture specifications, see
[SPEC-WIF](../openspec/specs/wif/spec.md),
[SPEC-CACHE](../openspec/specs/artifact-cache/spec.md), and the
[Distributed Execution and Transit System Design](../openspec/specs/tech/systems/distributed-execution-and-transit/design.md).

## How It Executes

1. **Single-SA WIF Authentication:** The CI runner exchanges its OIDC JWT for a
   short-lived Google Cloud credential impersonating `codemender-wif-sa`, which
   holds `roles/aiplatform.user` so `cm-runner` can call Vertex AI directly from
   the container. No Cloud Run Jobs or GCS transit buckets are used.
2. **gVisor (`runsc`) Kernel Isolation:** The CI orchestrator launches the
   [`runner.Dockerfile`](../../docker/runner.Dockerfile) container under Docker
   with `--runtime=runsc`, isolating candidate builds and verification inside
   gVisor's user-space kernel alongside CodeMender's inner `permissive-closed`
   sandbox.
3. **Local Filesystem Artifact Cache (`file://`):** Intermediate SARIF findings
   (`findings/*.sarif`), candidate patches (`fixes/*.diff`), and verification
   reports (`verified/*.sarif`) are staged on the runner host's local disk and
   persisted as CI pipeline artifacts across `find-diff`, `fix`, and `verify`
   runs.

## Terraform Configuration

To provision the Google Cloud identity resources for Local Docker + gVisor mode,
set the following in `terraform/terraform.tfvars`:

```hcl
execution_mode = "local"
```

When `execution_mode = "local"`, Terraform provisions only `module.wif` (the
Workload Identity Pool, OIDC Provider, `codemender-wif-sa` with
`roles/aiplatform.user`, and optional repository CI secret/variable
bootstrapping) and skips `module.gcp`.

See the
[Terraform Infrastructure Guide (`../../terraform/README.md`)](../../terraform/README.md)
for provisioning instructions, authentication options, and the full input and
output reference tables.

## Manual Setup Steps (Under the Hood)

> [!NOTE]
> You do not need to run these steps manually when using the
> [Terraform module (`../../terraform/README.md`)](../../terraform/README.md)
> and turnkey CI integrations. They are documented here so you can understand
> what is configured under the hood or pre-configure custom self-hosted runner
> VMs.

### 1. Workload Identity Federation & IAM (`roles/aiplatform.user`)

- Provision a Google Cloud Workload Identity Pool and OIDC Provider configured
  to trust your CI platform's OIDC issuer and repository/project claims.
- Create a dedicated service account (`codemender-wif-sa`), grant it
  `roles/aiplatform.user` on the target Google Cloud project, and bind
  `roles/iam.workloadIdentityUser` to the Workload Identity Pool principal set.

### 2. Docker & gVisor (`runsc`) Runtime on the Runner Host

- Install Docker and the gVisor `runsc` binary on the Linux CI runner VM (ephemeral hosted runners can install `runsc` dynamically during job setup; persistent self-hosted runner VMs can pre-install `runsc`).
- Register `runsc` in `/etc/docker/daemon.json` and restart Docker:

  ```json
  {
    "runtimes": {
      "runsc": {
        "path": "/usr/local/bin/runsc"
      }
    }
  }
  ```

  ```bash
  sudo systemctl restart docker
  docker run --rm --runtime=runsc hello-world
  ```

### 3. Runner Container Image & Execution

- Build or pull the [`runner.Dockerfile`](../../docker/runner.Dockerfile) image
  (see
  [Building Images in `../../docker/README.md`](../../docker/README.md#building-images)).
- Invoke `cm-runner` (`find-diff`, `fix`, or `verify`) under `--runtime=runsc`
  with the checked-out repository and local cache directory mounted into the
  container.

### 4. Configure Your Orchestration Platform

- Configure your CI/CD orchestration platform with the provisioned Workload
  Identity Provider resource name, service account email, Google Cloud project
  ID, and optional custom runner image URI.
- Return to your orchestration platform setup guide in
  [Choose Your Orchestration Platform (`../../README.md`)](../../README.md#step-1-choose-your-orchestration-platform)
  to enable workflows on your repositories.
