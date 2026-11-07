# ADR-0003: Environment Variable Scrubbing When Running `cm`

- **Status:** accepted
- **Date:** 2026-09-30
- **Decision Makers:** CodeMender Maintainers

## Context and Problem Statement

The legacy orchestrator defines a 9-entry
denylist and an environment-scrubbing helper that
does two things before the legacy runners spawn `cm`:

1. **Credential Denylisting:** Strips nine specific environment variables from a
   copy of the process environment (`GITHUB_APP_TOKEN`, `GITHUB_PAT`, `GITHUB_TOKEN`,
   `GH_TOKEN`, `GITHUB_SECRET`, `INPUT_GITHUB_TOKEN`, `GCP_SA_KEY`,
   `GOOGLE_APPLICATION_CREDENTIALS_JSON`, and `GOOGLE_CREDENTIALS`).
1. **Temp and Cache Redirection:** Creates
   temporary and package-cache directories inside the checkout on disk and overrides
   `XDG_CACHE_HOME`, `npm_config_cache`, `TMPDIR`, `TEMP`, `TMP`, and
   `PIP_CACHE_DIR` to point inside the target repository.

As we build the new `cm-runner` subcommands (`init`, `find-diff`, `fix`,
`verify`) on top of the `cm` subprocess adapter (per
[ADR-0008](ADR-0008-repository-layout-and-package-structure.md)), that adapter currently
passes the process environment through unmodified, inheriting the caller's environment without calling
the legacy environment-scrubbing helper.

Should `cm-runner` carry forward or expand environment
variable scrubbing when spawning `cm`, or should we omit environment scrubbing
from the new CLI and retire the legacy environment-scrubbing helper along with the legacy pipeline?

## Decision Drivers

- **Permission Split Between Orchestration and `cm` Execution
  ([ADR-0008](ADR-0008-repository-layout-and-package-structure.md)):** Unlike the legacy orchestrator
  (which both ran `cm` and called the GitHub API to open PRs from the same
  process, forcing that process to hold `GITHUB_TOKEN` in its own
  environment), the new `cm-runner` CLI never talks to the repository host.
- **Security Across All Three Target Execution Modes:** We must evaluate how
  environment variables and sandbox escapes behave across all three ways
  `cm-runner` can be deployed:
  1. **Cloud Run Jobs:** Off-runner microVM fed via GCS, with zero repo-host
     credentials in the container environment.
  1. **gVisor (`runsc`) Container on the Repo Runner (Current On-Runner Plan):**
     Invoked on the CI runner via `docker run --runtime=runsc` (GitHub Actions),
     which isolates both the kernel syscall surface and the container
     environment (only explicitly allowlisted variables enter the container).
  1. **Direct Execution on the Repo Runner:** Invoked directly as a bare CI
     runner step (or a standard GitHub Actions `container:` job) without a
     separate `docker run` / gVisor boundary.
- **Native `cm` Sandboxing:** The `cm` binary
  uses a split parent-worker architecture: the parent `cm` process authenticates
  to the CodeMender backend over gRPC, while agent tool execution (`bash`, file
  edits, build/test commands) runs inside a Linux namespace and
  seccomp-bpf sandbox (`CLONE_NEWNS`, `CLONE_NEWUSER`, closed network by
  default, restricted writable mounts).
- **Avoiding False Security ("Security Theater"):** A hardcoded 9-variable
  denylist gives a false sense of security while missing real
  credential vectors (`ACTIONS_RUNTIME_TOKEN`, `ACTIONS_ID_TOKEN_REQUEST_TOKEN`,
  `GITLAB_TOKEN`, `CI_JOB_TOKEN`, `AWS_*`, custom repository secrets, and
  file-based credentials like `.git/config` auth headers or
  `GOOGLE_APPLICATION_CREDENTIALS` files).
- **Toolchain Compatibility During `fix` and `verify`:** When `cm` runs build
  and test commands inside the target repository (`npm`, `mvn`, `gradle`,
  `cargo`, `go`, `pytest`, `bazel`), those toolchains rely on standard host
  environment variables (`PATH`, `HOME`, `JAVA_HOME`, language SDK paths,
  compiler flags).
- **Simplicity and Low Maintenance
  ([ADR-0008](ADR-0008-repository-layout-and-package-structure.md)):** Avoid carrying over
  fragile legacy code and tests into the `cm` subprocess adapter when it
  neither solves bare-runner isolation nor fits our container/sandbox
  boundaries.

## Considered Options

1. **Option 1: Do not scrub environment variables in `cm-runner`; leave legacy
   environment scrubbing restricted to the legacy orchestrator only
   and delete it when legacy is retired.**
1. **Option 2: Port and expand denylist environment scrubbing
   into `cm-runner` for all `cm` invocations.**
1. **Option 3: Enforce a strict environment variable allowlist in `cm-runner`
   when spawning `cm`.**

## Decision Outcome

Chosen option: **"Option 1: Do not scrub environment variables in `cm-runner`;
leave legacy environment scrubbing restricted to the legacy orchestrator only
and delete it when legacy is retired."**

### Why Environment Scrubbing in `codemender-runner` Is Not Worth Keeping

Evaluating the legacy environment-scrubbing helper against our CI/CD execution modes
and the `cm` sandbox design shows why
the legacy orchestrator had this behavior and why both halves of that scrubbing helper are
either redundant, ineffective, or actively counterproductive for `cm-runner`:

1. **Why the legacy orchestrator needed environment scrubbing, and why `cm-runner` does
   not:**
   - The legacy orchestrator was a monolithic script that both executed `cm`
     *and* called the GitHub REST API and `git push` from the
     same process. Because that process required `GITHUB_TOKEN` in its own
     environment, it had to strip `GITHUB_TOKEN` when spawning `cm`.
   - Under [ADR-0008](ADR-0008-repository-layout-and-package-structure.md), `cm-runner` (`init`,
     `find-diff`, `fix`, `verify`) never talks to the repository host. Even when
     running on the repo runner, the step or container invoking `cm-runner` has
     no reason to receive `GITHUB_TOKEN` at all; only the subsequent publishing
     step receives `GITHUB_TOKEN`.
1. **How environment isolation works across all three execution modes:**
   - **Mode A — Cloud Run Jobs:** The CI runner stages source to GCS and starts
     a Cloud Run Job. The Cloud Run container has zero repo-host tokens or CI
     runner tokens in its environment.
   - **Mode B — gVisor (`runsc`) Container on the Repo Runner (Current On-Runner
     Plan):**
     - In **GitHub Actions**, `docker run --runtime=runsc` starts a separate
       container from the host VM and natively drops the host's environment—only
       variables explicitly passed via `-e` (such as `GOOGLE_CLOUD_PROJECT`,
       `GOOGLE_APPLICATION_CREDENTIALS`, and explicit runner configuration variables) enter the
       container, blocking ambient runner tokens (`ACTIONS_RUNTIME_TOKEN`,
       `ACTIONS_ID_TOKEN_REQUEST_TOKEN`, `GITHUB_ENV`) at the `docker run`
       boundary.
   - **Mode C — Direct Execution on the Repo Runner (Bare Step or GHA
     `container:` Job):** What if a customer runs `cm-runner` directly on the
     repo runner without a `docker run --runtime=runsc` boundary?
     - *Against a `cm` inner sandbox escape:* Scrubbing the process environment in
       the runner provides **zero protection**. On a bare runner, `cm-runner`
       and `cm` run as the same OS user (`runner`); an agent that escapes
       `cm`'s inner sandbox can read `/proc/<ppid>/environ`, runner temp files,
       or `.git/config` directly from the host.
       Protecting the runner against sandbox escape requires an external
       container/VM boundary (Mode A or Mode B), not in-process variable filtering.
     - *Inside `cm`'s inner sandbox (no escape):* The inner sandbox isolates the
       filesystem (`CLONE_NEWNS` only mounts the workspace, an external temporary scratch directory, and
       the `cm` binary directory, blocking writes to `GITHUB_ENV` /
       `GITHUB_PATH` runner command files), *but* the parent `cm` process
       currently passes its inherited process environment into the sandboxed child worker process. On a bare GitHub
       Actions step, even when `GITHUB_TOKEN` is omitted from the step's `env:`,
       GitHub Actions automatically injects `ACTIONS_RUNTIME_TOKEN` and
       `ACTIONS_ID_TOKEN_REQUEST_TOKEN` into every step—neither of which is in
       the legacy denylist.
1. **`cm` requires GCP credentials in its parent process (wrong layer for
   parent-to-worker filtering):**
   - The legacy denylist strips `GCP_SA_KEY`,
     `GOOGLE_APPLICATION_CREDENTIALS_JSON`, and `GOOGLE_CREDENTIALS` (raw JSON
     strings used by older GitHub Actions auth setups), but leaves
     `GOOGLE_APPLICATION_CREDENTIALS` (the file path to the active Workload
     Identity Federation / Service Account credential) intact because the parent
     `cm` binary needs it to authenticate to the CodeMender cloud backend over
     gRPC.
   - Because `cm` is the process boundary between the authenticated gRPC client
     (parent `cm`) and the untrusted agent tool worker (the sandboxed child worker
     process inside `cm`'s inner sandbox), `codemender-runner` sits *above* parent `cm` and cannot strip
     the credentials `cm` needs to operate. Today, `cm` isolates its sandboxed child worker process
     via Linux filesystem mount namespaces (explicit read-only and
     read-write mount lists) and a closed network namespace by default; if
     parent-to-worker environment variable scrubbing (for example, stripping
     `ACTIONS_*`, `GITHUB_*`, `GITLAB_*`, and `GOOGLE_APPLICATION_CREDENTIALS`)
     is desired for bare-runner defense-in-depth, `cm`
     is the only architectural boundary positioned to enforce it.
1. **`TMPDIR` and in-repository cache redirection in the legacy helper
   actively conflicts with `cm`'s inner sandbox:**
   - The legacy environment-scrubbing helper creates an in-repository temporary cache directory and
     sets `TMPDIR`, `TMP`, and `TEMP` to that path.
   - In `cm`,
     sandbox initialization reads the system temporary directory—which resolves to the caller's
     `$TMPDIR`—and adds it to the inner sandbox's denied-path list so the sandboxed worker
     cannot access the host's temp directory.
   - Immediately afterward, `cm` strips `TMPDIR` and `TMP` from
     the worker environment and
     resets `TMPDIR` and `TMP` to a `tmp` subdirectory inside
     `cm`'s external temporary scratch directory, while leaving a dangling
     `TEMP` variable pointing directly at a directory
     that `cm` just added to the inner sandbox's denied-path list!
   - Similarly, redirecting `XDG_CACHE_HOME`, `npm_config_cache`, and
     `PIP_CACHE_DIR` into in-repository cache subdirectories pollutes the
     checked-out repository working tree with untracked files during `fix` and
     `verify` (risking dirty `git status` or accidental inclusion in generated
     patches). If sandboxed toolchains need writable cache directories inside
     the inner sandbox, `cm` can point them at its external temporary scratch directory
     (which the inner sandbox mounts read-write outside the repo worktree) rather than
     `codemender-runner` mutating the target repository.

### Normative Rules

- The `cm` subprocess adapter and `cm-runner` commands **MUST NOT** invoke
  legacy environment-scrubbing helpers or maintain an environment variable
  denylist.
- The `cm` subprocess adapter **MUST** pass the process environment through to
  `cm` unmodified by default, allowing `cm` to authenticate to the CodeMender backend and
  manage its own inner sandbox worker environment and `TMPDIR`.
- In accordance with [ADR-0008](ADR-0008-repository-layout-and-package-structure.md) ("Kept
  working, not extended"), the legacy denylist and environment-scrubbing helper
  **SHOULD** remain untouched while the legacy runner pipeline
  is still active, and **MUST** be deleted alongside the legacy runner pipeline
  once the legacy pipeline is retired.
- Outer CI adapters **MUST** enforce workspace, environment, and sandbox
  isolation before invoking `cm-runner` (see
  [SPEC-GHA](../openspec/specs/github-actions/spec.md) and
  [SPEC-GITLAB-CI](../openspec/specs/gitlab-ci/spec.md)):

| Boundary Responsibility | GitHub Actions | GitLab CI |
| :--- | :--- | :--- |
| **Runner Starting Point** | Host VM with step-scoped `env:` blocks and local `dockerd`. | Inside a slim GitLab CI job container with no `dockerd` and globally injected CI variables. |
| **Checkout & Workspace Staging** | Set `persist-credentials: false` on `actions/checkout` and stage `$GITHUB_WORKSPACE` to an isolated directory. | Stage `$CI_PROJECT_DIR` to an isolated directory and strip embedded `.git/config` credentials, `.git/hooks`, and `.codemender.yaml` overrides (fail-closed). |
| **Repo-Host & Runner Tokens** | Scope `GITHUB_TOKEN` to outer steps; pass only an explicit `-e` allowlist into the launcher. | Scrub ambient `CI_JOB_TOKEN`, GitLab API tokens, and OIDC secrets from the process environment, pass only an explicit allowlist to local `git` commands, and pass zero repo-host or CI environment variables to Cloud Run Job executions (only explicit non-secret task settings are passed). |
| **On-Runner Sandbox & WIF Mount** | `docker run --runtime=runsc` with WIF credentials mounted read-only at `/creds/`. | None: `cm` runs only in Cloud Run Jobs. |

### Consequences

- **Good**, because `cm-runner` and its `cm` subprocess adapter remain simple, dependency-free, and
  aligned with [ADR-0008](ADR-0008-repository-layout-and-package-structure.md) without carrying
  forward brittle denylist and in-repository cache setup logic and
  dedicated scrubbing unit tests.
- **Good**, because `cm-runner` no longer creates untracked
  in-repository cache directories inside the target repository working tree or
  causes `cm`'s denied-path isolation to block `TEMP` inside `cm`'s inner sandbox.
- **Good**, because security responsibilities are placed at the boundaries that
  can actually enforce them: Cloud Run Jobs or `docker run --runtime=runsc`
  (gVisor) for container/environment isolation on the
  runner, and `cm`'s inner sandbox for agent tool execution.
- **Bad**, because if a caller runs `cm-runner` directly on a bare CI runner
  (without a `docker run --runtime=runsc` container boundary) or in a local
  developer shell, the parent `cm` process currently passes inherited environment
  variables other than `TMPDIR` and `TMP` through to
  its sandboxed child worker process. Any ambient secrets in that bare shell
  (`ACTIONS_RUNTIME_TOKEN`, or job-level `GITHUB_TOKEN`) are visible to
  the sandboxed child worker process inside `cm`'s inner sandbox and could be leaked via generated patches, build
  logs, or permissive outbound network calls. We mitigate this by running
  on-runner jobs inside a gVisor container with explicit allowlisted pass-through,
  scoping repo-host tokens to publishing steps only, and recommending that any
  future worker environment filtering be added directly at `cm`'s
  parent-to-worker sandbox boundary.

### Confirmation

- **Architecture Boundary Check:** Automated architecture tests fail if the
  runner CLI or `cm` subprocess adapter imports or references legacy
  environment-scrubbing helpers or a static secret-variable denylist.
- **Working Tree Cleanliness:** Running `cm-runner init`, `find-diff`, `fix`, or
  `verify` against a target repository does not create untracked cache
  directories inside the target repository.
- **Workflow Audit:** Official GitHub Actions, GitLab CI, and Cloud Run Job
  definitions run `cm-runner` without passing repo-host tokens
  (`GITHUB_TOKEN` / `GH_TOKEN` / `GITLAB_TOKEN` / `CI_JOB_TOKEN`),
  use an explicit allowlist when invoking gVisor (`docker run --runtime=runsc`),
  and strip checkout credentials from the staged
  workspace.

## Pros and Cons of the Options

### Option 1: Do not scrub environment variables in `cm-runner`; retire legacy environment scrubbing with the legacy orchestrator

- **Good**, because it eliminates security theater and avoids maintaining an
  ever-growing denylist of secret variable names across CI providers.
- **Good**, because it avoids polluting the target repo with
  in-repository cache directories and conflicting with `cm`'s native inner sandbox denied-path
  and `TMPDIR` isolation.
- **Good**, because leaving legacy environment scrubbing untouched until
  the legacy runner pipeline is removed satisfies
  [ADR-0008](ADR-0008-repository-layout-and-package-structure.md)'s "do not break what runs
  today; small diffs" rule.
- **Bad**, because the legacy orchestrator retains dead-end code until the legacy
  pipeline is deleted.

### Option 2: Port and expand denylist environment scrubbing into `cm-runner`

- **Good**, because on bare-runner or local-shell executions (without a
  `docker run --runtime=runsc` boundary), it catches the specific variable names
  in the denylist before they reach the sandboxed child worker process.
- **Bad**, because a denylist is inherently incomplete: it misses unknown custom
  repository secrets (`SONAR_TOKEN`, `NPM_TOKEN`, `AWS_*`), file-based
  credentials (`GOOGLE_APPLICATION_CREDENTIALS`, `.git/config`), and provides
  zero protection if the agent escapes `cm`'s inner sandbox on a bare runner (where
  `/proc/<ppid>/environ` is readable).
- **Bad**, because creating in-repository cache directories dirties the target
  repository (affecting `git diff` / patch generation) and conflicts with `cm`'s
  inner sandbox denied-path and `TMPDIR` handling.

### Option 3: Enforce a strict environment variable allowlist in `cm-runner`

- **Good**, because an allowlist is the only sound way to prevent unknown
  ambient secret environment variables from reaching a subprocess on a bare
  runner.
- **Bad**, because `codemender-runner` sits *above* the parent `cm` process
  (which needs GCP auth variables, proxy configs, and system certs) and *above*
  arbitrary repository build/test toolchains (`JAVA_HOME`, `GOPATH`,
  `CARGO_HOME`, `NVM_DIR`, `CC`, `CXX`, `PYTHONPATH`, custom project env vars)
  invoked during `fix` and `verify`. An allowlist in `cm-runner` would cause
  frequent false-positive build breakages across diverse repositories, whereas
  `docker run --runtime=runsc` already provides container-level env isolation
  when running on the CI runner.
- **Bad**, because if parent-to-worker environment allowlisting/filtering is
  needed for bare-runner execution, it belongs inside `cm`
  between the authenticated `cm` parent and the sandboxed child worker process, not
  above the `cm` parent in `codemender-runner`.

## Architecture Diagram

```mermaid
flowchart TD
    subgraph CI["Repo CI Runner (GitHub Actions / GitLab CI)"]
        step_checkout["1. Checkout & Stage Clean Workspace\n(GHA: persist-credentials: false;\nGitLab: strip .git/config & .codemender.yaml)"]
        step_invoke["2. Invoke Execution Boundary\n- Cloud Run Job (off-runner microVM), OR\n- gVisor Sandbox on Runner (docker run --runtime=runsc)\n(Explicit env allowlist + /creds/:ro;\nNo GITHUB_TOKEN / CI_JOB_TOKEN / GitLab API tokens)"]
        step_publish["3. Publish PR / MR Comment Step\n(Outer-scoped GITHUB_TOKEN / GitLab API tokens)"]
    end

    subgraph Runner["codemender-runner (New CLI inside Container)"]
        cli["cm-runner CLI subcommands\n(init, find-diff, fix, verify)"]
        adapter["cm Subprocess Adapter\n(Pass-through environment; no denylist scrubbing)"]
    end

    subgraph CM["CodeMender CLI (cm Binary)"]
        cm_parent["cm Parent Process\n(Authenticates to CodeMender Backend via GCP ADC)"]
        cm_worker["Sandboxed Child Worker Process\n(CLONE_NEWNS, CLONE_NEWUSER, seccomp-bpf,\nClosed network, resets TMPDIR/TMP to external scratch tmp)"]
    end

    step_checkout -->|"Clean workspace"| step_invoke
    step_invoke --> cli
    cli --> adapter
    adapter -->|"Spawns cm with inherited container env"| cm_parent
    cm_parent -->|"Resets TMPDIR/TMP + inner namespace sandbox"| cm_worker
    step_invoke -->|"Structured reports + patches"| step_publish
```

## More Information

- Related ADRs:
  - [ADR-0002: Split-Stream Machine-Readable CLI Output and Exit Code Contract](ADR-0002.md)
  - [ADR-0008: Single Multi-Platform Repository with Independently Installable Packages](ADR-0008-repository-layout-and-package-structure.md)
- System & Sandbox Design Documents:
  - [Security and Trust Zones System Specification](../openspec/specs/tech/systems/security-and-trust-zones/spec.md)
  - [Security and Trust Zones System Design](../openspec/specs/tech/systems/security-and-trust-zones/design.md)
  - [Distributed Execution and Transit System Design](../openspec/specs/tech/systems/distributed-execution-and-transit/design.md)
