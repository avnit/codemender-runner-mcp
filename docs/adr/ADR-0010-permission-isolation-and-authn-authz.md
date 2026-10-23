# ADR-0010: Three-Zone Permission Isolation and Runtime AuthN/AuthZ Split

- **Status:** proposed
- **Date:** 2026-10-14
- **Builds on:**
  [ADR-0008](ADR-0008-repository-layout-and-package-structure.md) (Repository
  Layout, Three-Package Workspace, and Runtime Architecture),
  [ADR-0009](ADR-0009-abstraction-layers-and-dependency-injection.md)
  (Abstraction Layers, Dependency Injection, and Asynchronous Workflows),
  [ADR-0003](ADR-0003-environment-scrubbing.md),
  [ADR-0005](ADR-0005-multi-platform-transit-storage-and-isolation.md),
  [ADR-0006](ADR-0006-pr-remediation-routing-and-security-gate.md),
  [ADR-0007](ADR-0007-cloud-run-job-invocation-interface-cli-vs-python-library.md)
- **Governs:** [SPEC-WIF](../openspec/specs/wif/spec.md),
  [SPEC-GHA](../openspec/specs/github-actions/spec.md),
  [SPEC-GHA-FIX](../openspec/specs/github-actions-fix/spec.md),
  [SPEC-GITLAB-CI](../openspec/specs/gitlab-ci/spec.md)

## 1. Context and Goals

This ADR defines the permission isolation model and runtime AuthN/AuthZ split
across the packages and classes defined in
[ADR-0008](ADR-0008-repository-layout-and-package-structure.md) and
[ADR-0009](ADR-0009-abstraction-layers-and-dependency-injection.md), guided by
four core goals:

1. **Zero Repository Host Credentials in the Sandbox Container:** The sandbox
   container (`Worker` and `Runner`) must have zero access to repository hosting
   credentials or APIs. If untrusted repository code or a compromised agent
   escapes inside the container, it must not be able to push branches, read
   private repositories, or modify Pull/Merge Requests. All repository host
   credentials and API calls are therefore restricted to `RepoHost` on the outer
   CI host.
1. **Zero Cloud Credentials and Network Access When Executing Target Code:** The
   sandbox where the `cm` agent executes code (for example, running repository
   builds and tests during `fix` or `verify`) must have zero access to cloud
   credentials and no outbound network access. While `cm` itself requires GCP
   credentials to call Vertex AI models, untrusted build scripts and tests run
   by the agent must not be able to read those cloud credentials, query cloud
   metadata servers, or exfiltrate code over the network.
1. **Least-Privilege Cloud Identity Separation Between Orchestration and
   Execution:** Each runtime environment must hold only the GCP permissions
   required for its role, authenticated via short-lived keyless credentials
   (Workload Identity Federation). When compute is offloaded to Cloud Run, the
   outer CI host (`Launcher`) must only have permission to stage artifacts in
   `ArtifactCache` and trigger/monitor the Cloud Run Job (never to call Vertex
   AI directly), while the Cloud Run sandbox container (`Worker`) must only have
   permission to call Vertex AI and read/write `ArtifactCache` (never to trigger
   jobs or manage cloud resources).
1. **Boundary-Enforced Isolation Over In-Process Scrubbing
   ([ADR-0003](ADR-0003-environment-scrubbing.md)):** Credential isolation must
   be enforced at the container boundary before `Worker` starts rather than via
   subprocess environment denylisting inside `Runner`. Per
   [ADR-0003](ADR-0003-environment-scrubbing.md), `CmAdapter` passes its
   container environment directly to `cm` (`env=None`) so `cm` can authenticate
   to Vertex AI and run repository toolchains; `Launcher` is therefore
   responsible for stripping checkout credentials from `shared/code.tar.gz` and
   launching the sandbox container with only an explicit configuration allowlist
   and scoped GCP identity.

---

## 2. Architecture Overview ("How Permission Isolation Works")

### Three-Zone Token & Trust Boundary Map

The diagram below groups the runtime components by **execution environment** and
shows **where credentials live and are used** across **Boundary A** (Container /
MicroVM Sandbox Boundary) and **Boundary B** (`cm` Code Execution Sandbox
Boundary):

```mermaid
classDiagram
    direction TB

    namespace OuterCIHost["Zone 1: Outer CI Host"] {
        class RepoHost {
            +REPO_HOST_TOKEN : e.g. GITHUB_TOKEN
        }
        class Launcher {
            +CI_OIDC_TOKEN
            +GCP_CREDENTIALS
        }
    }

    namespace SandboxContainer["Zone 2: Sandbox Container"] {
        class Worker {
            +GCP_CREDENTIALS
        }
        class Runner {
            +GCP_CREDENTIALS
        }
        class cm {
            +GCP_CREDENTIALS
        }
    }

    namespace CmCodeSandbox["Zone 3: cm Code Execution Sandbox"] {
        class TargetCodeSandbox
    }

    Launcher ..> Worker : Boundary A — Passes only GCP_CREDENTIALS
    Worker --> Runner : Inherits GCP_CREDENTIALS
    Runner --> cm : Inherits GCP_CREDENTIALS
    cm ..> TargetCodeSandbox : Boundary B — Spawns target code with zero tokens
```

> [!IMPORTANT]
> **Foundational Security Invariant:** No single zone possesses both **repository
> host credentials** (`REPO_HOST_TOKEN`) and **target repository code
> execution**. Zone 1 holds `REPO_HOST_TOKEN` (e.g., `GITHUB_TOKEN`) and
> exchanges `CI_OIDC_TOKEN` for `GCP_CREDENTIALS`, but never runs `cm`; Zone 2
> holds only `GCP_CREDENTIALS` for Vertex AI inference (and `ArtifactCache`
> access); Zone 3 (`TargetCodeSandbox`) executes target repository builds and
> tests with zero tokens and a closed network.

1. **Zone 1 — Outer CI Host (`OuterCIHost`):**
   - **Tokens Held:**
     - **`REPO_HOST_TOKEN` (e.g., `GITHUB_TOKEN`):** Held exclusively by
       `RepoHost` to read pull request diffs, post review comments/suggestions,
       and report merge-gate status (see
       [SPEC-GHA](../openspec/specs/github-actions/spec.md) and
       [SPEC-GITLAB-CI](../openspec/specs/gitlab-ci/spec.md) for platform-specific
       token scopes).
     - **`CI_OIDC_TOKEN` & `GCP_CREDENTIALS`:** `Launcher` exchanges the CI
       platform's short-lived `CI_OIDC_TOKEN` via Workload Identity Federation
       for `GCP_CREDENTIALS` (`codemender-wif-sa`).
   - **How Tokens Travel Across Boundary A:** `RepoHost` consumes
     `REPO_HOST_TOKEN` strictly on the outer CI host. Before `Launcher` starts
     the Zone 2 container, it strips checkout credentials from
     `shared/code.tar.gz`, excludes `REPO_HOST_TOKEN` and `CI_OIDC_TOKEN` from
     the container environment, and provides only `GCP_CREDENTIALS` (via a
     read-only `/creds/adc.json` mount in `local` mode or the attached
     `WorkerSa` in `cloud_run` mode).
1. **Zone 2 — Sandbox Container (`SandboxContainer` in
   `docker/runner.Dockerfile`: `Worker` $\rightarrow$ `Runner` $\rightarrow$
   `cm`):**
   - **Tokens Held:** Holds only `GCP_CREDENTIALS` (`packages/repohost` is never
     installed in `docker/runner.Dockerfile`).
   - **How Tokens Travel Inside Zone 2:** `Worker` (`cm-worker`) uses
     `GCP_CREDENTIALS` to read/write `ArtifactCache` (when backed by GCS) and
     spawns `Runner` (`cm-runner`), which passes the container environment
     through to `cm` (`env=None` per
     [ADR-0003](ADR-0003-environment-scrubbing.md)) so `cm` can authenticate to
     Vertex AI Gemini models over gRPC.
1. **Zone 3 — `cm` Code Execution Sandbox (`CmCodeSandbox`:
   `TargetCodeSandbox`):**
   - **Tokens Held:** None (`TargetCodeSandbox` has zero tokens).
   - **How Tokens Are Isolated at Boundary B:** When the `cm` agent executes
     target repository code (such as compiler invocations, build scripts, and
     test suites during `fix` or `verify`), `cm` spawns those commands inside an
     isolated sandbox that mounts only the unpacked repository workspace
     (`/tmp/work/repo`) and scratch directories—omitting `/creds`—and disables
     outbound network access.

---

### Mode-Switched GCP WIF & Service Account Topology (`local` vs. `cloud_run`)

The diagram below groups the GCP identities by execution mode
([SPEC-WIF](../openspec/specs/wif/spec.md)) and shows the IAM roles granted to
each Service Account in `local` mode (single Service Account) versus `cloud_run`
mode (Coordinator SA and Worker SA split):

```mermaid
classDiagram
    direction TB

    namespace OuterCIHost_WIF["Zone 1: Outer CI Host"] {
        class WifPoolProvider {
            +CI_OIDC_TOKEN
        }
    }

    namespace LocalMode["local Mode: Single-SA Topology"] {
        class LocalWifSa {
            <<codemender-wif-sa in Zone 2>>
            +Vertex_AI_User
        }
    }

    namespace CloudRunMode["cloud_run Mode: Dual-SA Split"] {
        class CoordinatorSa {
            <<codemender-wif-sa in Zone 1>>
            +Storage_Object_User
            +Run_Jobs_Executor_With_Overrides
            +Run_Viewer
        }
        class WorkerSa {
            <<codemender-runner-sa in Zone 2>>
            +Vertex_AI_User
            +Storage_Object_Creator
            +Storage_Object_Viewer
            +Logs_Writer
        }
    }

    namespace GCP_Services["Target GCP Services"] {
        class GcsArtifactCache
        class CloudRunJob
        class VertexAI
    }

    WifPoolProvider ..> LocalWifSa : Exchanges CI_OIDC_TOKEN in local mode
    WifPoolProvider ..> CoordinatorSa : Exchanges CI_OIDC_TOKEN in cloud_run mode

    LocalWifSa --> VertexAI : Calls Vertex AI from Zone 2

    CoordinatorSa --> GcsArtifactCache : Stages code archive and reads receipts
    CoordinatorSa --> CloudRunJob : Triggers and polls job
    CloudRunJob --> WorkerSa : Attaches to Zone 2 container
    WorkerSa --> GcsArtifactCache : Reads inputs and writes outputs
    WorkerSa --> VertexAI : Calls Vertex AI from Zone 2
```

1. **`local` Mode (Single-SA Topology):**
   - `LocalDockerLauncher` runs the sandbox container on the CI host using
     `LocalArtifactCache` (`file:///cache`), requiring no GCS bucket or Cloud
     Run Job.
   - `LocalWifSa` (`codemender-wif-sa`) is granted **only
     `roles/aiplatform.user` (`Vertex_AI_User`)**
     ([SPEC-WIF REQ-0003](../openspec/specs/wif/spec.md#req-0003)) and mounted
     read-only into Zone 2 at `/creds/adc.json`.
1. **`cloud_run` Mode (Dual-SA Split):**
   - `CloudRunLauncher` stages artifacts through `GcsArtifactCache` (`gs://`)
     and dispatches remote Cloud Run Job executions, splitting permissions
     across two Service Accounts
     ([SPEC-WIF REQ-0004](../openspec/specs/wif/spec.md#req-0004),
     [REQ-0005](../openspec/specs/wif/spec.md#req-0005)):
     - **`CoordinatorSa` (`codemender-wif-sa` in Zone 1):** Held by `Launcher`
       on the outer CI host. Granted `roles/storage.objectUser`
       (`Storage_Object_User`) on the transit bucket (to stage
       `shared/code.tar.gz`, read output artifacts, and delete staged run
       objects) and job-scoped `roles/run.jobsExecutorWithOverrides`
       (`Run_Jobs_Executor_With_Overrides`) plus `roles/run.viewer`
       (`Run_Viewer`) to trigger (`run.jobs.runWithOverrides`), poll
       (`run.executions.get`), and cancel (`run.executions.cancel`) the
       pre-provisioned Cloud Run Job, without `roles/aiplatform.user`,
       `roles/run.developer`, or `roles/iam.serviceAccountUser` (`actAs`) on
       `WorkerSa` (`WorkerSa` is bound to `google_cloud_run_v2_job` at Terraform
       deploy time, and `RunJob` at runtime neither checks
       `iam.serviceAccounts.actAs` nor permits overriding `service_account`).
     - **`WorkerSa` (`codemender-runner-sa` in Zone 2):** Attached to the Cloud
       Run Job container at deploy time. Granted `roles/aiplatform.user`
       (`Vertex_AI_User`), `roles/storage.objectCreator`
       (`Storage_Object_Creator`) and `roles/storage.objectViewer`
       (`Storage_Object_Viewer`) on the transit bucket, and
       `roles/logging.logWriter` (`Logs_Writer`). Because all Cloud Run Job
       executions run as the same `WorkerSa` principal and access GCS via
       in-container `GcsArtifactCache` SDK calls, static IAM on `WorkerSa` is
       **bucket-wide across the short-lived transit bucket** rather than per-run
       prefix-isolated by IAM (`<run_prefix>` provides namespace partitioning
       against path collisions across runs, while `objectCreator` +
       `objectViewer` denies `storage.objects.update` and
       `storage.objects.delete` so a compromised worker cannot overwrite or
       delete already-staged inputs, sibling task outputs, or receipts).

---

### Runtime AuthN/AuthZ & Credential Lifecycle Sequence

The sequence diagram below traces how credentials are acquired, isolated, used,
and cleaned up across the three zones during a workflow run:

```mermaid
sequenceDiagram
    autonumber
    box Zone 1: Outer CI Host
        participant RH as RepoHost
        participant L as Launcher
    end

    box GCP Services
        participant GCP as WIF / ArtifactCache / Vertex AI
    end

    box Zone 2: Sandbox Container
        participant W as Worker / Runner / cm
    end

    box Zone 3: cm Code Execution Sandbox
        participant CS as TargetCodeSandbox
    end

    RH->>RH: Read PR diff context using REPO_HOST_TOKEN (e.g., GITHUB_TOKEN)
    L->>GCP: Exchange CI_OIDC_TOKEN for GCP_CREDENTIALS
    L->>L: Scrub REPO_HOST_TOKEN & CI_OIDC_TOKEN from env, sanitize .git/config & .codemender.yaml
    L->>GCP: Stage sanitized shared/code.tar.gz & RunManifest to ArtifactCache

    L->>W: Spawn container across Boundary A with explicit env allowlist & GCP_CREDENTIALS
    W->>GCP: Read RunManifest & unpack shared/code.tar.gz into /tmp/work/repo
    W->>GCP: Authenticate to Vertex AI using GCP_CREDENTIALS

    W->>CS: Spawn build/test command across Boundary B (/creds unmounted, network closed)
    CS-->>W: Execute target repo build/test commands & return tool outputs

    W->>GCP: Write receipt.json, runner.log & SARIF/diff artifacts to ArtifactCache
    W-->>L: Container exits

    L->>GCP: Read receipt.json & output artifacts from ArtifactCache
    L->>RH: Return structured outcomes -> Post PR suggestions & status using REPO_HOST_TOKEN
    L->>L: finally: Unlink ephemeral credential files
```

---

## 3. Core Design Decisions & Rationale

### Decision 1: Three-Zone Privilege Separation & Outer-Host VCS Termination

- **Design:** Partition runtime execution into three nested zones—**Zone 1**
  (Outer CI Host running `RepoHost` and `Launcher`), **Zone 2** (Sandbox
  Container running `Worker`, `Runner`, and `cm`), and **Zone 3** (the sandbox
  where the `cm` agent executes target repository builds and tests)—and
  terminate all repository host API calls and `REPO_HOST_TOKEN` credentials
  (e.g., `GITHUB_TOKEN`) exclusively in Zone 1. `docker/runner.Dockerfile`
  installs only `packages/codemender` and `packages/launcher[cloudrun]`,
  omitting `packages/repohost`
  ([ADR-0008](ADR-0008-repository-layout-and-package-structure.md),
  [ADR-0009](ADR-0009-abstraction-layers-and-dependency-injection.md)).
- **Why:**
  - **Zero VCS Blast Radius Inside the Sandbox:** `Worker` and `Runner` only
    transform a staged repository archive (`shared/code.tar.gz`) into output
    artifacts (`report.sarif`, `<finding>.diff`, `receipt.json`) in
    `ArtifactCache`, so Zone 2 and Zone 3 never require `REPO_HOST_TOKEN`.
  - **Defense-in-Depth Across Boundary A and Boundary B:** Even if an untrusted
    build script escapes Zone 3 (Boundary B) or a step in `cm` executes directly
    in Zone 2, **Boundary A** (gVisor container or Cloud Run microVM) confines
    execution to an environment with **zero `REPO_HOST_TOKEN` access**, no
    `packages/repohost` code, and `GCP_CREDENTIALS` restricted to
    `roles/aiplatform.user` (plus write-once/read transit storage access in
    `cloud_run` mode).
- **Alternatives Rejected:**
  - *Monolithic runner with subprocess environment denylisting
    (`SENSITIVE_ENV_VARS` / `get_scrubbed_env()`):* Rejected in
    [ADR-0003](ADR-0003-environment-scrubbing.md); Python denylists miss ambient
    CI tokens, custom repository secrets, and file-based credentials, and
    provide zero protection if a process reads `/proc/<ppid>/environ` without a
    container boundary.
  - *Sandbox-accessible token broker / proxy sidecar:* Exposes whatever VCS or
    cloud operations the proxy permits to any compromised process inside the
    sandbox that can reach the proxy socket, while adding sidecar deployment
    complexity across local containers and Cloud Run Jobs.

### Decision 2: Isolating Cloud Credentials and Network When `cm` Executes Target Code

- **Design:** In `local` mode, `Launcher` writes the `CI_OIDC_TOKEN` and WIF
  `external_account` ADC file (`GCP_CREDENTIALS`) atomically to `/creds/oidc.jwt`
  and `/creds/adc.json` (`0600` permissions inside a `0700` directory) and
  bind-mounts `/creds` read-only (`ro,nosuid,nodev,noexec`) into the Zone 2
  container **outside** the repository workspace. When the `cm` agent executes
  target repository code in Zone 3 across **Boundary B**, that sandbox mounts
  only the unpacked repository workspace, validated relative `project_paths`,
  and isolated scratch directories, omitting `/creds` and disabling outbound
  network access.
- **Why:**
  - **Separates Authenticated Model Inference from Target Code Execution:** `cm`
    requires `GCP_CREDENTIALS` (`/creds/adc.json` in `local` mode or `WorkerSa`
    in `cloud_run` mode) to call Vertex AI over gRPC, whereas untrusted
    repository compilers and test suites run by the agent have no legitimate
    need for cloud credentials or network access.
  - **Blocks Symlink & Metadata Server Exfiltration:** Omitting `/creds` from
    Zone 3's filesystem mounts and closing its network prevents untrusted build
    scripts from reading `/creds` (even via a repository symlink) or querying
    `169.254.169.254`.
- **Alternatives Rejected:**
  - *Mounting WIF credentials inside the repository workspace or `$HOME/.config`:*
    Exposes credentials to target code execution because the workspace and
    selected `$HOME` subdirectories are mounted inside Zone 3.
  - *Enforcing an environment variable allowlist inside `CmAdapter`
    ([ADR-0003](ADR-0003-environment-scrubbing.md)):* `CmAdapter` sits above
    `cm` in Zone 2 and therefore cannot strip `GCP_CREDENTIALS` that `cm` needs
    to call Vertex AI; isolating Zone 3 from Zone 2 must happen at Boundary B
    inside `cm`.

### Decision 3: Mode-Switched Least-Privilege GCP WIF (`local` Single-SA vs. `cloud_run` Coordinator/Worker Dual-SA Split)

- **Design:** Provision keyless OIDC Workload Identity Federation in
  `terraform/` ([SPEC-WIF](../openspec/specs/wif/spec.md)) with two mode-specific
  IAM topologies:
  1. **`local` Mode (`execution_mode = "local"`):** Provisions a single WIF
     Service Account (`LocalWifSa` / `codemender-wif-sa`) granted **only
     `roles/aiplatform.user`**, with zero GCS or Cloud Run permissions.
  1. **`cloud_run` Mode (`execution_mode = "cloud_run"`):** Splits permissions
     across two Service Accounts:
     - **`CoordinatorSa` (`codemender-wif-sa` in Zone 1):** Granted
       `roles/storage.objectUser` on the transit bucket and job-scoped
       `roles/run.jobsExecutorWithOverrides` plus `roles/run.viewer` on the
       Cloud Run Job, while **omitting `roles/aiplatform.user`,
       `roles/run.developer`, and `roles/iam.serviceAccountUser` (`actAs` on
       `WorkerSa`)**.
     - **`WorkerSa` (`codemender-runner-sa` in Zone 2):** Bound to
       `google_cloud_run_v2_job` at Terraform deploy time and granted
       `roles/aiplatform.user`, bucket-wide `roles/storage.objectCreator` +
       `roles/storage.objectViewer` on the short-lived transit bucket, and
       `roles/logging.logWriter`.
- **Why:**
  - **Zero Unnecessary Cloud Services in `local` Mode:** Local deployments never
    enable `run.googleapis.com` or `storage.googleapis.com` and grant zero
    storage or compute roles.
  - **Prevents Job Mutation and Transitive `WorkerSa` Escalation
    (`CoordinatorSa`):** Binding `WorkerSa` to the Cloud Run Job definition at
    Terraform deploy time and granting `CoordinatorSa` only
    `roles/run.jobsExecutorWithOverrides` + `roles/run.viewer` (without
    `run.jobs.update` or `iam.serviceAccounts.actAs`) ensures `Launcher` on the
    CI host can trigger, poll, and cancel executions with per-run `args`/`env`
    overrides, but cannot invoke Vertex AI directly, modify the job's container
    image, or attach `WorkerSa` to new resources to transitively assume
    `WorkerSa`'s permissions.
  - **Write-Once Transit Storage and Bounded Blast Radius (`WorkerSa`):**
    `Worker` inside Cloud Run authenticates via identity-bound IAM (avoiding
    replayable GCS signed URL bearer tokens per
    [ADR-0005](ADR-0005-multi-platform-transit-storage-and-isolation.md)) and
    cannot trigger new Cloud Run Jobs. Because all Cloud Run Job executions
    share `WorkerSa` and call GCS via `GcsArtifactCache`, static IAM on
    `WorkerSa` is bucket-wide across the dedicated transit bucket
    (`<run_prefix>` partitions object paths across runs rather than enforcing a
    per-run IAM boundary). Granting `WorkerSa` `roles/storage.objectCreator` +
    `roles/storage.objectViewer` instead of `roles/storage.objectUser` denies
    `storage.objects.update` and `storage.objects.delete`, preventing a
    compromised worker from overwriting or deleting staged inputs, sibling task
    outputs, or receipts, while the transit bucket's short lifecycle TTL and
    zero stored credentials bound the exposure of objects within that bucket.
- **Alternatives Rejected:**
  - *Sharing a single GCP Service Account between the CI host and the Cloud Run
    Job in `cloud_run` mode:* Violates least privilege by granting the CI host
    unnecessary Vertex AI access and granting the untrusted Cloud Run container
    Cloud Run job-execution permissions.
  - *Granting `CoordinatorSa` project-wide `roles/run.developer` and
    `roles/iam.serviceAccountUser` (`actAs`) on `WorkerSa`:*
    `roles/run.developer` includes `run.jobs.update`, which—combined with
    `iam.serviceAccounts.actAs` on `WorkerSa`—would allow a caller holding
    `CoordinatorSa` (including across repositories when `CoordinatorSa` is
    shared at the organization level) to replace the Cloud Run Job's container
    image and execute arbitrary workloads as `WorkerSa`.
  - *Passing GCS V4 Signed URLs into the Cloud Run container:* Rejected in
    [ADR-0005](ADR-0005-multi-platform-transit-storage-and-isolation.md) because
    signed URLs act as bearer tokens if leaked in logs or environment variables.
  - *Minting per-execution GCP Credential Access Boundary (CAB) downscoped
    OAuth2 tokens for prefix-scoped GCS access:* Rejected for three reasons: (1)
    Cloud Run's metadata server cannot attach a per-execution CAB via
    `RunJobRequest.Overrides`, requiring `Launcher` to pass a raw OAuth2 bearer
    token in job `env`/`args` overrides where it persists in
    `run.executions.get` metadata and Cloud Audit Logs (violating
    [ADR-0005](ADR-0005-multi-platform-transit-storage-and-isolation.md)); (2)
    minting a downscoped token from Zone 1 requires either downscoping
    `CoordinatorSa` itself or granting `CoordinatorSa`
    `roles/iam.serviceAccountTokenCreator` on `WorkerSa` (defeating the
    `CoordinatorSa`/`WorkerSa` privilege split); and (3) downscoped OAuth2
    access tokens have a hard 1-hour lifetime with no metadata-server
    auto-refresh inside the container.

### Decision 4: Boundary-Enforced Workspace, `.git/config`, and Environment Sanitization in `Launcher`

- **Design:** Consistent with [ADR-0003](ADR-0003-environment-scrubbing.md)
  (`Runner` / `CmAdapter` performs zero in-process env scrubbing and passes
  `env=None` to `cm`), `Launcher` enforces four sanitization gates in Zone 1
  before staging `shared/code.tar.gz` or starting the Zone 2 container
  ([SPEC-GHA REQ-0006](../openspec/specs/github-actions/spec.md#req-0006),
  [SPEC-GHA-FIX REQ-0005](../openspec/specs/github-actions-fix/spec.md#req-0005),
  [SPEC-GITLAB-CI REQ-0006](../openspec/specs/gitlab-ci/spec.md#req-0006)):
  1. **Explicit Container Environment Allowlist:** `Launcher` constructs the
     Zone 2 container environment strictly from an allowlist (never copying
     `os.environ`) and pops `REPO_HOST_TOKEN` (e.g., `GITHUB_TOKEN`) and
     `CI_OIDC_TOKEN` from `os.environ` after reading them.
  1. **Clean `.git/config` Replacement & Hook Deletion:** `Launcher` replaces
     `.git/config` in the staged snapshot with a minimal allowlist
     (`core.repositoryformatversion`, `core.bare = false`, and optional
     `extensions.objectFormat` / `extensions.refStorage`), deletes `.git/hooks`,
     and fails closed (`exit 1`) on Git submodules or partial clones.
  1. **Base-Commit `.codemender.yaml` Pinning & Policy Removal:** `Launcher`
     reads `.codemender.yaml` strictly from the diff base commit (ignoring
     unmerged PR changes), retains only `scan` and validated workspace-relative
     `project_paths` (rejecting `..` and symlinks), and deletes
     `.codemender/command_policy.yaml`.
  1. **Symlink Preservation Without Dereferencing:** `Launcher` copies symbolic
     links as links (`symlinks=True`) during workspace staging and rejects
     symlinked mount or credential paths.
- **Why:**
  - **Enforces Isolation at Boundary A While Preserving Toolchain Compatibility
    Inside Zone 2 ([ADR-0003](ADR-0003-environment-scrubbing.md)):** Filtering
    environment variables and stripping checkout secrets when `Launcher` creates
    the container prevents `REPO_HOST_TOKEN` and `CI_OIDC_TOKEN` from entering
    Zone 2, while allowing `Runner` (`CmAdapter`) inside Zone 2 to pass its
    clean container environment straight through to `cm` (`env=None`).
  - **Prevents Git Config Command Execution in Zone 2:** `cm` and
    `codemender.vcs` run `git` commands inside Zone 2 where `/creds` is mounted;
    replacing `.git/config` from a strict allowlist blocks command execution
    hooks such as `diff.<driver>.textconv`, `core.fsmonitor`, and `includeIf`,
    as well as embedded `http.extraheader` checkout tokens.
  - **Prevents PR Self-Modification of Sandbox Rules:** Pinning
    `.codemender.yaml` to the base commit prevents an untrusted PR from adding
    `/creds` to `project_paths` or overriding `.codemender/command_policy.yaml`.
- **Alternatives Rejected:**
  - *In-process env denylisting in `CmAdapter` (`get_scrubbed_env()`):* Rejected
    in [ADR-0003](ADR-0003-environment-scrubbing.md) as incomplete and at the
    wrong architectural boundary.
  - *Denylist-stripping specific `.git/config` keys:* Misses execution hooks
    such as `diff.<driver>.textconv` or `includeIf.<condition>.path`.
  - *Reading `.codemender.yaml` from the PR `HEAD` checkout:* Allows an
    untrusted PR to alter its own scan exclusions or sandbox mount paths before
    merge.
