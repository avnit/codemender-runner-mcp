<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-10-06' }
*-->

<!--
sdd_spec_id: SPEC-GITLAB-CI
sdd_implements: None
sdd_extends: SPEC-FIND-DIFF, SPEC-GITLAB-SARIF
sdd_level: COMPONENT
sdd_archetype: CI_CD_WORKFLOW
sdd_cujspec_product: codemender-runner
-->

# SPEC-GITLAB-CI: GitLab CI Orchestration & Execution Backends

**Implements Spec:** None

**Extends Spec:** [SPEC-FIND-DIFF](../find-diff/spec.md),
[SPEC-GITLAB-SARIF](../gitlab-sarif-report/spec.md)

**Governing Journeys:**

- **Merge Request Author (`persona-mr-author`)**: As a GitLab developer
  submitting Merge Requests, I want fast, automated differential security
  feedback posted as a single sticky MR comment and ingested natively into
  GitLab's Security tab (`artifacts:reports:sarif`) without managing static GCP
  keys or committing runner scripts into my repository.
- **Enterprise Security & Platform Lead (`persona-security-auditor`)**: As an
  enterprise security architect, I mandate that code analysis and verification
  occur inside a strong sandbox boundary—either a standalone gVisor (`runsc`)
  sandbox on ephemeral/privileged GitLab Runners or an isolated Google Cloud Run
  microVM—with zero repository token leakage into the sandbox, strict isolation
  of Workload Identity Federation (WIF) credentials from untrusted agent
  workers, and fail-closed execution if the configured backend fails.

## Overview

This specification defines the functional requirements, configuration interface,
launcher abstraction, credential boundary isolation, and reporting workflow for
the CodeMender GitLab CI integration (`gitlab-ci/`).

Repositories configure their pipeline to use one of two execution backends via
`CODEMENDER_BACKEND`:

1. **Local gVisor Mode (`local`)**: Executes `cm-runner` on the GitLab Runner
   inside an ephemeral gVisor (`runsc`) OCI sandbox with `cm`'s internal
   `exebox` sandbox enabled, requiring no Docker-in-Docker (`dockerd`) daemon or
   external cloud compute infrastructure.
1. **Cloud Run Mode (`cloudrun`, planned)**: Offloads `cm-runner` execution to
   isolated, serverless Google Cloud Run microVMs via the canonical Cloud Run
   client CLI (`cm-cloudrun-client`)—used when teams want zero compute/memory
   overhead on their CI runners, centralized cloud execution in GCP, or are
   running on unprivileged Kubernetes/shared runners.

Both backends take the same `cm-runner` arguments and write the same output
artifacts (`report.sarif`, `report.md`, `cm-logs/`) to the same paths, so SARIF
normalization and Merge Request reporting never branch on the backend
(REQ-0008).

This specification is governed by:

- [ADR-0001: Machine-Readable Output Contract](../../../adr/ADR-0001-machine-readable-output-contract.md)
- [ADR-0002: Runner Layout, Build, and How to Call It](../../../adr/ADR-0002-runner-layout-and-build.md)
- [ADR-0003: Environment Variable Scrubbing When Running `cm`](../../../adr/ADR-0003-environment-scrubbing.md)
- [ADR-0004: Project Tech Stack & Scenario-Decoupled Monorepo Architecture](../../../adr/ADR-0004-project-tech-stack-and-repo-architecture.md)
- [ADR-0005: Multi-Platform Transit Storage & Sandboxed Isolation Model](../../../adr/ADR-0005-multi-platform-transit-storage-and-isolation.md)
- [SPEC-GITLAB-SARIF: GitLab Native SARIF 2.1.0 Normalization](../gitlab-sarif-report/spec.md)
- [SPEC-GHA: GitHub Actions Orchestration & Execution Backends](../github-actions/spec.md)

______________________________________________________________________

## Requirements

### REQ-0001: Pipeline Configuration & Backend Selection

- **CUJ UUID:** `cuj-gitlab-backend-config`

The GitLab CI integration (`gitlab-ci/templates/codemender-scan.yml` and
`gitlab-ci/src/gitlab_runner.py`) MUST be configured through a minimal set of
CI/CD Variables and `id_tokens`:

| Variable | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `CODEMENDER_BACKEND` | No | `'local'` | Target execution backend: `'local'` (gVisor `runsc` on the runner) or `'cloudrun'` (Cloud Run via canonical client CLI; planned, REQ-0003). |
| `CODEMENDER_CLOUDRUN_CLIENT` | No | `'cm-cloudrun-client'` | Cloud Run client command, used only with `'cloudrun'` (REQ-0004). |
| `GCP_PROJECT_ID` | Yes | — | Google Cloud project ID hosting Vertex AI / CodeMender APIs. |
| `GCP_WIF_PROVIDER` | Yes | — | Workload identity pool provider resource name (`projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL_ID/providers/PROVIDER_ID`). |
| `GCP_SERVICE_ACCOUNT` | Yes | — | Email of the Google Cloud service account impersonated through WIF. |
| `GCP_OIDC_TOKEN` | Set by the template | — | GitLab OIDC JWT, requested by the template's `id_tokens:`. Don't define it as a CI/CD variable. |
| `CODEMENDER_GITLAB_TOKEN` | No | `''` | Token for the sticky MR comment: a project access token or a service account's token, with the Reporter role, `api` scope and an expiry date. Masked, not Protected. Without it, the scan and the SARIF report still run. A v1 choice, to revisit (design Decision 4). |
| `CODEMENDER_SCAN_ON_MR` | No | `'true'` | Set to `'false'` to skip the scan in merge request pipelines, the only pipelines it runs in (REQ-0008). |
| `CODEMENDER_DIFF_BASE` | No | Auto | Explicit Git base reference override for `cm-runner find-diff --base` (REQ-0007). |
| `CODEMENDER_LOG_LEVEL` | No | `'info'` | Log verbosity: `debug`, `info`, `warning` or `error`. Any other value exits with code `1`. |
| `CODEMENDER_MODEL` | No | `''` | Optional Gemini model identifier, passed to `cm-runner` after a format check (REQ-0006). |
| `GIT_DEPTH` | No | `'0'` | Clone depth. The template sets `"0"` (full history) so the base commit is in the clone. For large repositories, override it in your `codemender-scan` job's `variables:`; if the base commit is then missing, the job exits with code `1` and says to raise it (REQ-0007). A project or group CI/CD variable named `GIT_DEPTH` also overrides the template's value. |

#### Scenario: Rejecting an unsupported backend value

- **GIVEN** `CODEMENDER_BACKEND="kubernetes"`
- **WHEN** the orchestrator starts
- **THEN** it MUST log an error naming `'kubernetes'` and exit with code `1`
  without invoking `cm-runner`.

______________________________________________________________________

### REQ-0002: Local gVisor OCI Sandbox Execution (`GvisorLauncher`)

- **CUJ UUID:** `cuj-gitlab-gvisor-sandbox`

When `CODEMENDER_BACKEND` is `'local'`, `GvisorLauncher`
(`gitlab-ci/src/launcher/gvisor.py`) MUST execute `cm-runner` inside a
standalone gVisor (`runsc`) OCI Runtime Specification v1.0.2 sandbox without
requiring a Docker-in-Docker (`dockerd`) daemon:

1. **Read-Only Root & Least-Privilege Mounts:**
   - Construct an ephemeral `0700` OCI bundle with `root.readonly = True`.
   - Mount fresh virtual filesystems (`/proc`, `/dev`, and an isolated `/tmp`
     `tmpfs`).
   - Bind-mount host system directories (`/bin`, `/etc`, `/lib*`, `/opt`,
     `/sbin`, `/usr`, `/var`) read-only (`ro,nosuid,nodev`).
   - Bind-mount only the staged workspace, output directory, and an isolated
     `$HOME` directory, rejecting symbolic links, `/`, `/root`, or system
     directories as mount targets.
   - Bind-mount the `/creds` directory (REQ-0005) read-only
     (`ro,nosuid,nodev,noexec`) at the same path, outside the staged workspace,
     so the parent `cm` process can authenticate to Google Cloud.
   - The inner `exebox` worker (`cm __worker`) can't see `/creds`. `exebox`
     blocks every path it isn't given. `cm` gives the worker the repository, its
     `project_paths`, its artifacts directory, the `cm` binary's directory and
     `security.protected_files`, and `exebox` adds system directories such as
     `/etc`, `/usr` and `/var`, plus some directories under `$HOME` such as
     `~/.cache` and `~/.config`. None of these include `/creds`, and the
     credential directory MUST NOT be moved under any of them. REQ-0006 keeps
     repositories from adding paths and keeps the sandbox on.
1. **Two-Layer Sandbox Enforcement (`runsc` + `exebox`):**
   - Grant virtualized `CAP_SYS_ADMIN` inside gVisor's user-space `Sentry`
     kernel (while excluding `CAP_SYS_PTRACE` and `CAP_NET_RAW`) and
     unconditionally set `CODEMENDER_SANDBOX_ENABLED="true"` and
     `CODEMENDER_SANDBOX_NETWORK_PROFILE="permissive-closed"` (REQ-0006), so
     `cm`'s internal `exebox` sandbox is always active and its worker has no
     network.
1. **Network Egress:**
   - Traffic from the job, including the sandbox, MUST reach only the GitLab
     instance, `aiplatform.googleapis.com`, `sts.googleapis.com` and
     `iamcredentials.googleapis.com`. If a test run shows that `cm` needs
     another host, it is added here.
   - The metadata server (`169.254.169.254`) MUST be blocked.
   - The runner's network, not the orchestrator, enforces this with a proxy or
     a firewall ([README](../../../../gitlab-ci/README.md)). In the simplest
     setup, sandbox traffic leaves through the job container's network.
1. **Deterministic Cleanup & Error Handling:**
   - Unconditionally destroy the `runsc` container (`runsc delete -force`) and
     clean up the ephemeral bundle directory in a `finally:` block, even when
     the deadline (REQ-0008) is reached (`exit_code=124`).

#### Scenario: `GvisorLauncher` enforces read-only root, virtualized `CAP_SYS_ADMIN`, a closed network, and `/creds` isolation

- **GIVEN** `GvisorLauncher` configured for a local scan
- **WHEN** the OCI bundle configuration is generated for `cm-runner find-diff`
- **THEN** `root.readonly` MUST be `True`
- **AND** `CAP_SYS_ADMIN` MUST be enabled (with `CAP_SYS_PTRACE` and
  `CAP_NET_RAW` omitted)
- **AND** `CODEMENDER_SANDBOX_ENABLED=true` and
  `CODEMENDER_SANDBOX_NETWORK_PROFILE=permissive-closed` MUST be set in the
  container environment
- **AND** `/creds` MUST be bind-mounted read-only at `/creds`, outside the
  staged workspace.

#### Scenario: `GvisorLauncher` rejects symlinked or system-root mount targets

- **GIVEN** an output path, home directory, or credential path that is a
  symbolic link or points to `/etc` or `/root`
- **WHEN** `GvisorLauncher` validates mount targets
- **THEN** it MUST raise `LauncherError` and refuse to start the container.

#### Scenario: The worker can't read `/creds`

- **GIVEN** a local scan of a repository that contains `leak`, a symbolic link
  to `/creds/oidc.jwt`
- **WHEN** the worker tries to read `/creds/oidc.jwt` and `leak` with its tools
  (`read_file`, `list_dir`, `grep_search`, and `run_command` running, for
  example, `cat /creds/oidc.jwt`)
- **THEN** every attempt MUST fail
- **AND** the output files and the job log MUST NOT contain the token's value
- **AND** the scan MUST still complete.

#### Scenario: Egress is limited to the allowlist

- **GIVEN** a `codemender-gvisor` runner set up as the README describes
- **WHEN** a process in the job or the sandbox connects to a host that item 3
  doesn't list, or to `169.254.169.254`
- **THEN** the connection MUST fail
- **AND** a scan that needs only the listed hosts MUST complete.

#### Scenario: `GvisorLauncher` cleans up container state at the deadline

- **GIVEN** `runsc` is still running at the deadline (REQ-0008)
- **WHEN** `subprocess.TimeoutExpired` is raised
- **THEN** `GvisorLauncher` MUST invoke `runsc delete -force`, remove the
  temporary bundle directory, and raise `LauncherError` with `exit_code == 124`.

______________________________________________________________________

### REQ-0003: Cloud Run Backend Execution (`CloudRunLauncher`)

- **CUJ UUID:** `cuj-gitlab-cloudrun-launcher`
- **Status:** Planned. `local` ships first; revisit this requirement before
  `cloudrun` ships.

When `CODEMENDER_BACKEND` is `'cloudrun'`, `CloudRunLauncher`
(`gitlab-ci/src/launcher/cloud_run.py`) MUST implement the abstract `Launcher`
interface to dispatch `cm-runner` commands to Google Cloud Run via the client
CLI (modeled on [SPEC-GHA](../github-actions/spec.md) REQ-0003):

1. **Client Invocation & Validation:**
   - Accept `client_cmd` (defaulting to `cm-cloudrun-client`) and `log_level`,
     rejecting empty or repository-relative executable/script paths
     (`ValueError`). The time limit is the deadline (REQ-0008).
   - Construct `argv` as `[*client_cmd, command, *args, "--log-level", log_level]`.
1. **Stream & Result Contract:**
   - Follow REQ-0008: the client's `stderr` goes to the job log, and results
     come only from the files in `--out`.
1. **Error Mapping:**
   - Raise `LauncherError(exit_code=127)` when the client CLI is not found,
     `LauncherError(exit_code=124)` at the deadline, and `LauncherError` on OS
     execution errors.

#### Scenario: Successful command dispatch via `CloudRunLauncher`

- **GIVEN** a configured `CloudRunLauncher` targeting `cm-cloudrun-client` with
  `log_level="info"`
- **WHEN** `launcher.run("find-diff", ["--repo", "/workspace", "--base", "9fceb02", "--out", "/out"])` is called
- **THEN** the client subprocess MUST be invoked with
  `["cm-cloudrun-client", "find-diff", "--repo", "/workspace", "--base", "9fceb02", "--out", "/out", "--log-level", "info"]`
- **AND** `RunResult.exit_code` MUST be `0` when the client exits with code `0`.

#### Scenario: Missing client binary or a run still going at the deadline raises `LauncherError`

- **GIVEN** `CloudRunLauncher` configured with a missing client binary, or a
  command still running at the deadline
- **WHEN** `launcher.run("find-diff", ...)` executes
- **THEN** `CloudRunLauncher` MUST raise `LauncherError` (`exit_code == 127` for
  the missing binary, `exit_code == 124` at the deadline) without unhandled
  stack traces.

______________________________________________________________________

### REQ-0004: Entrypoint Launcher Factory Routing

- **CUJ UUID:** `cuj-gitlab-launcher-factory`

The entrypoint (`gitlab_runner.py`) MUST route execution through a single
launcher factory (`build_launcher`) based on `CODEMENDER_BACKEND`:

1. Normalize `CODEMENDER_BACKEND` case-insensitively, defaulting to `'local'`
   when unset and accepting `'cloud_run'` as an alias for `'cloudrun'`.
1. When `'local'`, instantiate `GvisorLauncher`.
1. When `'cloudrun'`, instantiate `CloudRunLauncher` using
   `CODEMENDER_CLOUDRUN_CLIENT` (or default `cm-cloudrun-client`).
1. When `CODEMENDER_BACKEND` has any other value, raise `ValueError` and exit
   with code `1`.

#### Scenario: Launcher factory selects `GvisorLauncher` by default

- **GIVEN** `CODEMENDER_BACKEND` is unset (or `'local'`)
- **WHEN** `build_launcher` is called
- **THEN** it MUST return a `GvisorLauncher` instance.

#### Scenario: Launcher factory selects `CloudRunLauncher` in Cloud Run mode

- **GIVEN** `CODEMENDER_BACKEND="cloudrun"` (or `"cloud_run"`)
- **WHEN** `build_launcher` is called
- **THEN** it MUST return a `CloudRunLauncher` instance.

______________________________________________________________________

### REQ-0005: Workload Identity Federation (`auth/wif.py`) & Credential Lifecycle

- **CUJ UUID:** `cuj-gitlab-wif-auth`

`setup_wif_credentials` (`gitlab-ci/src/auth/wif.py`) MUST write GitLab's
`id_tokens:` OIDC JWT (`$GCP_OIDC_TOKEN`) to a file, plus an Application Default
Credentials (ADC) file of type `external_account` that points to it. It doesn't
call Google itself: `cm` exchanges the token for Google credentials through ADC.

1. **Immediate Token Removal & Validation:**
   - Unconditionally remove `GCP_OIDC_TOKEN` from the environment upon entry.
   - Validate `GCP_PROJECT_ID`, `GCP_WIF_PROVIDER`, `GCP_SERVICE_ACCOUNT`, and
     `GCP_OIDC_TOKEN` against strict format rules (rejecting path traversal,
     whitespace, and quotes) and raise `WifAuthError` without echoing raw secret
     values on failure.
1. **`/creds` Files & `finally:` Cleanup:**
   - Write `oidc.jwt` and `adc.json` atomically with `0600` permissions in
     `/creds`, a `0700` directory created in the image, rejecting symbolic links
     on the files and on `/creds`.
   - `adc.json` MUST set:
     - `type`: `external_account`
     - `audience`: `//iam.googleapis.com/${GCP_WIF_PROVIDER}`
     - `subject_token_type`: `urn:ietf:params:oauth:token-type:jwt`
     - `token_url`: `https://sts.googleapis.com/v1/token`
     - `credential_source.file`: `/creds/oidc.jwt`
     - `service_account_impersonation_url`:
       `https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/${GCP_SERVICE_ACCOUNT}:generateAccessToken`
   - The sandbox gets `GOOGLE_APPLICATION_CREDENTIALS=/creds/adc.json`
     (REQ-0006). The paths are the same inside and outside `runsc` because
     `GvisorLauncher` mounts `/creds` at `/creds` (REQ-0002).
   - Unconditionally unlink both files in a `finally:` block via
     `cleanup_wif_credentials` when the pipeline completes.

#### Scenario: Writing the WIF files to `/creds` and cleaning them up on exit

- **GIVEN** valid `GCP_PROJECT_ID`, `GCP_WIF_PROVIDER`, `GCP_SERVICE_ACCOUNT`,
  and `GCP_OIDC_TOKEN`
- **WHEN** `run_gitlab_pipeline` executes and completes
- **THEN** `setup_wif_credentials` MUST pop `GCP_OIDC_TOKEN` from `env` and
  atomically create `0600` files `/creds/oidc.jwt` and `/creds/adc.json`
- **AND** `adc.json` MUST set `credential_source.file` to `/creds/oidc.jwt`
- **AND** `cleanup_wif_credentials` MUST remove both files in `finally:`.

#### Scenario: Rejecting malformed WIF inputs or symlinked credential paths without leaking secrets

- **GIVEN** `GCP_OIDC_TOKEN="secret-jwt"` and a malformed `GCP_WIF_PROVIDER` or
  symlinked credential path
- **WHEN** `setup_wif_credentials` is called
- **THEN** `GCP_OIDC_TOKEN` MUST be removed from `env`, credential files
  MUST NOT remain on disk, and `WifAuthError` MUST NOT contain `"secret-jwt"`.

______________________________________________________________________

### REQ-0006: Security & Credential Boundary Isolation

- **CUJ UUID:** `cuj-gitlab-security-boundary`

In compliance with [ADR-0002](../../../adr/ADR-0002-runner-layout-and-build.md)
and [ADR-0003](../../../adr/ADR-0003-environment-scrubbing.md):

1. **Allowlisted Environments:**
   - The orchestrator MUST build the environment of every subprocess it starts
     (`git`, `runsc`, the Cloud Run client) from an allowlist, never from a copy
     of `os.environ`. As a backstop, it MUST also remove
     `CODEMENDER_GITLAB_TOKEN`, `CI_JOB_TOKEN` and `GCP_OIDC_TOKEN` from
     `os.environ` once it has read them.
   - The sandbox environment MUST contain only:
     - `PATH`, and `HOME` set to the isolated home directory (REQ-0002).
     - `LANG=C.UTF-8`.
     - `GOOGLE_APPLICATION_CREDENTIALS=/creds/adc.json` (REQ-0005).
     - `GOOGLE_CLOUD_PROJECT`, copied from `GCP_PROJECT_ID` after a format
       check.
     - `CODEMENDER_SANDBOX_ENABLED=true` and
       `CODEMENDER_SANDBOX_NETWORK_PROFILE=permissive-closed`. Both values are
       fixed: no CI/CD variable or repository config can change them (item 4).
     - `CODEMENDER_MODEL`, if set, after a format check.
     - `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=safe.directory` and
       `GIT_CONFIG_VALUE_0` set to the staged workspace path, only if the
       staged workspace's owner differs from the sandbox user.
     - `HTTPS_PROXY` and `NO_PROXY`, only when egress goes through a proxy
       (REQ-0002).
   - A name is added to this list only when a test run shows that `cm-runner`
     or `cm` needs it.
   - The Cloud Run client (REQ-0003) gets only `PATH`, `HOME`,
     `GOOGLE_APPLICATION_CREDENTIALS` and `GOOGLE_CLOUD_PROJECT`, plus the proxy
     variables when they are set.
1. **Isolated Workspace Staging:**
   - Before invoking the launcher, the orchestrator MUST copy `$CI_PROJECT_DIR`
     into an isolated temporary workspace directory, so the sandboxed process
     never changes the job's checkout. `$CI_PROJECT_DIR` itself MUST NOT be
     mounted into the sandbox.
   - The copy MUST keep symbolic links as links and never follow them, so a
     link in the repository can't copy a file from outside it (such as
     `/proc/self/environ`) into the staged workspace.
1. **Fresh `.git/config`:**
   - The orchestrator MUST replace the staged `.git/config` with a new file that
     sets only `core.repositoryformatversion` (copied from the clone),
     `core.bare = false`, and `extensions.objectFormat` and
     `extensions.refStorage` if the clone sets them. It MUST delete the staged
     `.git/hooks`.
   - A list of keys to strip isn't enough. `cm` runs `git` in its own process,
     where `/creds` is readable, and such a list can miss keys that make `git`
     run commands, like `diff.<driver>.textconv` (which `git diff` runs by
     default), or `includeIf.<condition>.path`, which pulls in another config
     file.
   - Partial clones and submodules aren't supported: the orchestrator MUST exit
     with code `1` if the clone uses either.
1. **`.codemender.yaml` Allowlist:**
   - `cm-runner` uses the first of `.codemender.yaml`, `codemender.yaml`,
     `.codemender.yml` and `codemender.yml` that parses, and skips the ones
     that don't. The orchestrator MUST read these files from the diff base
     commit (REQ-0007), not from the MR's changes, so an MR can't change the
     settings its own scan uses (for example `scan.exclude_dirs`). A change
     takes effect once it's merged.
   - The orchestrator MUST exit with code `1` if the base commit has more than
     one of these files, or if any of them is a symbolic link or doesn't parse.
   - From the file, it MUST keep only the `scan` block and the `project_paths`
     entries that are relative and stay inside the staged workspace without
     passing through a symbolic link, and it MUST log the keys it drops.
   - It MUST delete all four file names from the staged workspace and write the
     result as `.codemender.yaml`, the only config file there. If the base
     commit has none of these files, the staged workspace has none either.
   - It MUST delete the staged `.codemender/command_policy.yaml`. When there's
     no global command policy, `cm` uses a repository's copy instead of its
     built-in list of blocked commands.

#### Scenario: Building the sandbox environment and `.git/config` from allowlists

- **GIVEN** the job environment has `CI_JOB_TOKEN="glcbt-secret"`,
  `CODEMENDER_GITLAB_TOKEN="glpat-secret"`, `MY_COPY="glpat-secret"`,
  `CM_DISABLE_SANDBOX="true"` and
  `CODEMENDER_SANDBOX_NETWORK_PROFILE="permissive-open"`
- **AND** the clone's `.git/config` embeds the job token and sets
  `diff.x.textconv`
- **WHEN** the orchestrator prepares the workspace and starts the sandbox
- **THEN** the sandbox environment MUST contain only names from item 1, with
  `CODEMENDER_SANDBOX_ENABLED="true"` and
  `CODEMENDER_SANDBOX_NETWORK_PROFILE="permissive-closed"`
- **AND** the sandbox environment MUST NOT contain either token's value
- **AND** the staged `.git/config` MUST contain only the keys listed in item 3,
  and the staged `.git/hooks` MUST NOT exist
- **AND** `$CI_PROJECT_DIR` MUST NOT be mounted into the sandbox.

#### Scenario: Copying symbolic links as links

- **GIVEN** the clone contains `leak`, a symbolic link to `/proc/self/environ`
- **WHEN** workspace staging runs
- **THEN** the staged `leak` MUST be a symbolic link to `/proc/self/environ`
- **AND** the staged workspace MUST NOT contain the value of
  `CODEMENDER_GITLAB_TOKEN` in any file.

#### Scenario: Reducing `.codemender.yaml` to the allowlist

- **GIVEN** the base commit's `.codemender.yaml` has
  `project_paths: ["/creds", "../other", "src"]`, a `scan` block, and
  `security` and `build` blocks
- **AND** the MR adds a `codemender.yml` with a `sandbox` block
- **WHEN** workspace staging runs
- **THEN** the staged `.codemender.yaml` MUST contain only the `scan` block and
  `project_paths: ["src"]`
- **AND** the staged workspace MUST NOT contain `codemender.yml`
- **AND** the orchestrator MUST log the dropped keys and run the scan.

#### Scenario: Failing closed on ambiguous configs, symbolic links and unsupported clones

- **GIVEN** a base commit with a malformed `.codemender.yaml` next to a
  `codemender.yml` or with a config file that is a symbolic link, or a clone
  whose `.git/config` is a symbolic link, or a clone with submodules
- **WHEN** workspace staging runs
- **THEN** the orchestrator MUST exit with code `1` before invoking `cm-runner`.

______________________________________________________________________

### REQ-0007: Sticky Merge Request Feedback & Native SARIF Reporting

- **CUJ UUID:** `cuj-gitlab-reporting`

Merge Request comment formatting, native SARIF reporting, and quality gating
MUST function identically across both execution backends:

1. **Diff Base Resolution (`resolve_diff_base`):**
   - Use the first that applies:
     1. `CODEMENDER_DIFF_BASE`, if set.
     1. In a merged results pipeline
        (`CI_MERGE_REQUEST_EVENT_TYPE == "merged_result"`):
        `CI_MERGE_REQUEST_TARGET_BRANCH_SHA`. The pipeline tests the MR merged
        into the target branch's head, so diffing against that head covers only
        the MR's changes.
     1. In any other merge request pipeline: `CI_MERGE_REQUEST_DIFF_BASE_SHA`.
     1. Otherwise, exit with code `1`. Never fall back to `HEAD~1`.
   - Reject refs starting with `"-"` or containing `".."`, whitespace, or
     control characters.
   - Before launching, check that the base resolves to a commit in the clone. If
     it doesn't, exit with code `1` with a message saying to raise `GIT_DEPTH`
     (REQ-0001).
1. **Native SARIF Normalization & Bounded Artifact Staging:**
   - Require `cm-runner find-diff` to produce a valid SARIF 2.1.0 document with
     a non-empty `runs` array (failing closed with exit code `1` if missing or
     corrupt).
   - Normalize `report.sarif` via `normalize_sarif_file` per
     [SPEC-GITLAB-SARIF](../gitlab-sarif-report/spec.md) and stage
     `report.sarif`, `report.md`, and bounded `cm-logs/` into `$CI_PROJECT_DIR`
     for GitLab artifact collection.
1. **Bot-Verified Sticky MR Commenting (`vcs/gitlab.py`):**
   - Comment only when `CODEMENDER_GITLAB_TOKEN` is set; otherwise, log that the
     comment is skipped.
   - Escape untrusted Markdown content (`format_mr_summary`) and post or update
     a single sticky MR note containing `<!-- codemender-summary -->`, updating
     an existing note only when its author is the token's own user and
     enforcing same-origin pagination.
   - The author check proves only that the note was written with the bot's
     token, not that this pipeline wrote it: any job in the project can read
     the token (design Decision 4).
   - A failure to post the comment is logged and doesn't change the exit code.
1. **Exit Codes & Blocking Merges:**
   - GitLab ingests SARIF findings only when the job that produced them
     succeeds, and `allow_failure` doesn't change that
     ([SARIF exit codes](https://docs.gitlab.com/user/application_security/detect/sarif/#scanner-exit-codes)).
     So `codemender-scan` MUST exit with code `0` whenever it staged a valid
     `report.sarif`, with or without findings, and with code `1` when the scan
     or the report is broken.
   - The template MUST NOT fail a job because of findings. To block merges on
     findings, customers use a GitLab
     [merge request approval policy](https://docs.gitlab.com/user/application_security/policies/merge_request_approval_policies/),
     which requires an approval first. How a policy behaves when the target
     branch has no CodeMender scan (the template scans only merge requests) is
     still to be confirmed in a test run.

#### Scenario: Failing closed when `cm-runner` produces no valid SARIF report

- **GIVEN** `launcher.run("find-diff", ...)` exits `0` without writing a valid
  `report.sarif` containing a non-empty `runs` list
- **WHEN** post-scan processing runs
- **THEN** the orchestrator MUST log an error and return exit code `1` without
  posting a false zero-findings MR comment.

#### Scenario: Ignoring a spoofed comment and exiting `0` with a finding

- **GIVEN** `find-diff` reports 1 finding, `CODEMENDER_GITLAB_TOKEN` is set, and
  an untrusted human user has posted a spoofed comment containing
  `<!-- codemender-summary -->`
- **WHEN** the pipeline runs
- **THEN** `codemender-scan` MUST normalize and stage `report.sarif` to
  `$CI_PROJECT_DIR`
- **AND** `post_or_update_sticky_mr_note` MUST ignore the human comment and post
  a new bot comment
- **AND** `codemender-scan` MUST exit with code `0`, so GitLab ingests the
  finding.

#### Scenario: Scanning without a GitLab token

- **GIVEN** `CODEMENDER_GITLAB_TOKEN` is unset and `find-diff` reports 1 finding
- **WHEN** the pipeline runs
- **THEN** `codemender-scan` MUST stage `report.sarif`, post no comment, log
  that the comment was skipped, and exit with code `0`.

#### Scenario: Using the target branch head in a merged results pipeline

- **GIVEN** `CI_MERGE_REQUEST_EVENT_TYPE="merged_result"`,
  `CI_MERGE_REQUEST_TARGET_BRANCH_SHA="d4e5f6a"`,
  `CI_MERGE_REQUEST_DIFF_BASE_SHA="a1b2c3d"`, and no `CODEMENDER_DIFF_BASE`
- **WHEN** `resolve_diff_base` runs
- **THEN** it MUST return `"d4e5f6a"`.

#### Scenario: Failing when no usable base exists

- **GIVEN** a pipeline where none of the base sources applies, or a base commit
  missing from the clone because `GIT_DEPTH` is too small
- **WHEN** the orchestrator resolves and checks the base
- **THEN** it MUST exit with code `1` before launching, without falling back to
  `HEAD~1`
- **AND** for a missing commit, the message MUST say to raise `GIT_DEPTH`.

______________________________________________________________________

### REQ-0008: Symmetric Backend Interface & Reference Pipeline Template

- **CUJ UUID:** `cuj-gitlab-backend-parity-and-template`

To keep `local` and `cloudrun` interchangeable (modeled on
[SPEC-GHA](../github-actions/spec.md) REQ-0008, per
[ADR-0002](../../../adr/ADR-0002-runner-layout-and-build.md)):

1. **Single Self-Contained `find-diff` Invocation & Symmetric Artifact Interface:**
   - Per [ADR-0002](../../../adr/ADR-0002-runner-layout-and-build.md), `cm-runner`
     commands are self-contained and automatically run `init` before dispatching
     `find-diff`. The orchestrator MUST invoke `find-diff` in a single ephemeral
     sandbox/Cloud Run execution rather than spawning separate `version` or
     `init` containers.
   - Both `GvisorLauncher` and `CloudRunLauncher` MUST take the same
     `cm-runner` arguments (`find-diff` with `--repo`, `--base`, `--out` and
     `--log-level`), send live logs to `stderr` (the job log), and write
     `report.sarif` and `report.md` to the same `--out` path. Results come only
     from those files; `stdout` isn't parsed. Downstream normalization and MR
     commenting never branch on `CODEMENDER_BACKEND`.
1. **Deadline:**
   - The orchestrator MUST compute
     `deadline = CI_JOB_STARTED_AT + CI_JOB_TIMEOUT - 5 minutes`. The 5 minutes
     are kept for staging artifacts, reporting and cleanup.
   - The launcher gets whatever time is left before the deadline. If that is
     under 10 minutes, the orchestrator MUST exit with code `1` before
     launching, with a message saying to raise the job's `timeout:`.
   - At the deadline, the launcher stops the scan and raises `LauncherError`
     with `exit_code == 124` (REQ-0002, REQ-0003).
1. **Hardened Container & Reference Pipeline Template
   (`gitlab-ci/templates/codemender-scan.yml`):**
   - The container image (`gitlab-ci/Dockerfile`) and wrapper
     (`/usr/local/bin/cm-gitlab-runner`) MUST execute `gitlab_runner.py` inside
      the isolated `/opt/codemender/venv` Python runtime (`python3 -s`,
     `PYTHONNOUSERSITE=1`) without allowing repository files to override runner
     code.
   - `gitlab-ci/templates/codemender-scan.yml` MUST define a `codemender-scan`
     job with:
     - `image:` the image built from `gitlab-ci/Dockerfile`, pinned by digest,
       with `entrypoint: [""]`.
     - `tags: [codemender-gvisor]`.
     - `id_tokens:` `GCP_OIDC_TOKEN` with
       `aud: https://iam.googleapis.com/${GCP_WIF_PROVIDER}`.
     - `variables:` `GIT_DEPTH: "0"`, so the base commit is in the clone
       (REQ-0001 explains how to override it), and `GIT_STRATEGY: clone`, so a
       working copy left by an earlier job is never reused.
     - `timeout: 30 minutes`. GitLab also uses the job timeout as the ID token's
       lifetime; without one, the token lasts 5 minutes
       ([ID tokens](https://docs.gitlab.com/ci/secrets/id_token_authentication/)).
     - `artifacts:` `when: always`, `paths: [report.sarif, report.md, cm-logs/]`
       and `reports: sarif: report.sarif`. Listing `report.sarif` under `paths`
       too lets people browse it in the job's artifacts.
     - `rules:` run only in merge request pipelines
       (`CI_PIPELINE_SOURCE == "merge_request_event"`), and never when
       `CODEMENDER_SCAN_ON_MR` is `"false"`, in merge train pipelines
       (`CI_MERGE_REQUEST_EVENT_TYPE == "merge_train"`), or for merge requests
       from forks
       (`CI_MERGE_REQUEST_SOURCE_PROJECT_ID != CI_MERGE_REQUEST_PROJECT_ID`).
       The design's Non-Goals explain why.

#### Scenario: Downstream reporting operates identically across `local` and `cloudrun` backends

- **GIVEN** `report.sarif` and `report.md` produced in `out_dir` by either
  `GvisorLauncher` or `CloudRunLauncher`
- **WHEN** downstream artifact normalization and MR summary formatting execute
- **THEN** they MUST consume `out_dir / "report.sarif"` and
  `out_dir / "report.md"` identically without branching on `CODEMENDER_BACKEND`.

#### Scenario: Reference template configures the scan job and its triggers

- **GIVEN** `gitlab-ci/templates/codemender-scan.yml`
- **WHEN** it is included in a GitLab project's pipeline
- **THEN** `codemender-scan` MUST invoke `cm-gitlab-runner` and have every
  setting listed in item 3.
