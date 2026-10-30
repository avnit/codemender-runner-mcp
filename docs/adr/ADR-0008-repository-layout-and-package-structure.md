# ADR-0008: Repository Layout, Three-Package Workspace, and Runtime Architecture

- **Status:** proposed
- **Date:** 2026-10-08
- **Supersedes:** `ADR-0002` (Runner Layout, Build, and How to Call It),
  `ADR-0004` (Project Tech Stack & Scenario-Decoupled Monorepo Architecture)

## 1. Context and Goals

We are designing `codemender-runner` as an extensible, reusable system for
running CodeMender (`cm`) across diverse CI/CD platforms, version control
systems, and sandbox execution environments.

The repository layout and Python package architecture are designed to accomplish
four core goals:

1. **Single Shared Customer Repository:** Provide one repository that customers
   can adopt directly for both **organization-level controls** (cloning or
   forking into a GitHub Organization or GitLab Group as a central reusable
   workflow, action, or CI/CD component catalog repository) and
   **single-repository adoption** (referencing or copying the entrypoints into
   an individual project).
1. **Extensible Multi-Host, Multi-VCS & Multi-Backend Architecture:** Support
   multiple repository hosting platforms (such as GitHub and GitLab), multiple
   version control systems (such as Git, and future Mercurial or Perforce
   integrations), and multiple sandbox execution backends (such as local
   Docker/gVisor and Google Cloud Run Jobs) with clean domain boundaries.
1. **Reusable Pipeline Coordination & Domain Primitives:** Share SARIF
   processing, local VCS/diff operations, and the multi-step scan-verify-fix
   pipeline coordinator across all CI/CD integrations without duplicating logic.
1. **Modular Packaging & Scoped Dependencies:** Organize Python code into a
   three-package workspace (`packages/{codemender,launcher,repohost}`) built on
   the foundational technical stack and engineering conventions in
   [ADR-0001](ADR-0001.md), with minimal base dependencies for the sandbox
   runner and scoped optional dependency extras (`cloudrun`, `github`,
   `gitlab`).

______________________________________________________________________

## 2. Repository Layout & Package Architecture ("What Goes Where")

### Roadmap Directory Structure

The tree below shows the high-level layout of the repository and the three
Python packages under `packages/`:

```text
codemender-runner/
├── .github/                        # GitHub workflows and composite actions
├── templates/                      # GitLab CI/CD component templates
├── docker/                         # Per-target Dockerfiles and container entrypoints
├── terraform/                      # Root Terraform configuration
│   └── modules/                    # Composable Terraform modules
├── scripts/                        # Repository maintenance and distribution utilities
├── docs/                           # Architecture decisions, specifications, and guides
├── Makefile                        # Standard build, test, and container targets
├── pyproject.toml                  # Root workspace configuration
└── packages/                       # Python workspace packages (with co-located test_*.py unit tests)
    ├── codemender/                 # Core sandbox runner & shared primitives (`import codemender`)
    │   ├── pyproject.toml
    │   └── src/
    │       └── codemender/
    │           ├── sarif/          # SARIF parsing, splitting, fingerprinting, and normalization
    │           ├── vcs/            # Version Control System operations (local tree and diff parsing)
    │           └── runner/         # Generic CLI wrapper around the `cm` binary (`cm-runner`)
    ├── launcher/                   # Sandbox execution client (`Launcher`), worker (`Worker`/`cm-worker`), & `ArtifactCache` (`import launcher`)
    │   ├── pyproject.toml
    │   └── src/
    │       └── launcher/
    │           ├── cache/          # Short-lived run-scoped `ArtifactCache` (`LocalArtifactCache`, `GcsArtifactCache`)
    │           ├── local/          # Local Docker / gVisor container execution backend (`LocalDockerLauncher`)
    │           └── cloudrun/       # Google Cloud Run Jobs execution backend (`CloudRunLauncher`)
    └── repohost/                   # Repository hosting platform integrations (`import repohost`)
        ├── pyproject.toml
        └── src/
            └── repohost/
                ├── github/         # GitHub PR review, inline suggestions, API integration, and `cm-github` CLI
                └── gitlab/         # GitLab MR discussion, inline suggestions, API integration, and `cm-gitlab` CLI
```

> **Note on `src/<package>/` nesting:** In standard Python `src/`-layout
> packaging, `src/` is the directory added to `sys.path` (corresponding to
> `site-packages/` when installed), so the directory immediately inside `src/`
> defines the top-level import namespace (`import codemender`,
> `import launcher`, `import repohost`). Placing `sarif/` directly at
> `packages/codemender/src/sarif/` would expose a bare `import sarif` global
> module instead of `from codemender import sarif`.

### Responsibility & Boundary Table

The table below mirrors the roadmap structure above:

| Path                                | Responsibility                                                                                                                                                                                                                | Boundary Rules                                                                                                                     |
| :---------------------------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :--------------------------------------------------------------------------------------------------------------------------------- |
| `.github/`                          | Native GitHub Actions workflows (`.github/workflows/`) and pure-YAML composite actions (`.github/actions/codemender` for Cloud Run and `.github/actions/codemender-local` for Local Docker + gVisor).                         | Referenced directly at the repository root by org-level or single-repo GitHub callers. Contains no Python source or test files.    |
| `templates/`                        | Native GitLab CI/CD Component Catalog templates.                                                                                                                                                                              | Referenced directly at the repository root via GitLab `include: component:` or `include: project:`.                                |
| `docker/`                           | Target-specific container definitions (`*.Dockerfile`) and entrypoint scripts.                                                                                                                                                | Built with the repository root (`.`) as the Docker build context via `Makefile` targets.                                           |
| `terraform/` & `terraform/modules/` | Root Terraform configuration and composable child modules for cloud infrastructure.                                                                                                                                           | Root `terraform/` composes child modules under `terraform/modules/`.                                                               |
| `scripts/`                          | Standalone maintenance and distribution utilities.                                                                                                                                                                            | Never imported by runtime Python packages.                                                                                         |
| `docs/`                             | Architecture Decision Records (ADRs), OpenSpecs, and user guides.                                                                                                                                                             | Documentation only.                                                                                                                |
| `Makefile`                          | Developer and CI entrypoints for running unit tests, linters, and container builds.                                                                                                                                           | Wraps test discovery and `docker build -f docker/...` commands into ergonomic targets.                                             |
| `pyproject.toml`                    | Root Python workspace configuration (`[tool.uv.workspace]`) and shared tool settings.                                                                                                                                         | Links `packages/*` for unified local development and testing.                                                                      |
| `packages/codemender/`              | Core package running inside the sandbox (`cm-runner`) plus shared `sarif/` and local `vcs/` primitives.                                                                                                                       | Minimal base dependencies. **Never imports `launcher` or `repohost`.** Executes `cm` exclusively through its internal `CmAdapter`. |
| `packages/launcher/`                | Provides the host-side `Launcher` client (`local/`, `cloudrun/`), the inside-container `Worker` server (`cm-worker`), and the shared `ArtifactCache` (`cache/`) coordinating `find`, `verify`, and parallel `fix` workflows. | Depends on `codemender`. **Never imports `repohost`.** Isolates Cloud Run SDKs in the `cloudrun` optional extra.                   |
| `packages/repohost/`                | Repository hosting platform integrations (`github/`, `gitlab/`), dedicated per-host CLIs (`cm-github`, `cm-gitlab`), and shared PR/MR Markdown formatting.                                                                    | Runs in the outer CI step (outside the `cm` sandbox) where repo-host tokens reside. Depends on `codemender` and `launcher`.        |

______________________________________________________________________

## 3. Core Design Decisions & Rationale

### Decision 1: Single Shared Customer Repository & Native Root Entrypoints (`.github/`, `templates/`)

- **Design:** Place GitHub workflows and actions in `.github/` and GitLab CI/CD
  component templates in `templates/` at the root of the repository (governed
  for GitHub distribution and deployment by
  [SPEC-DIST](../openspec/specs/distribute/spec.md)).
- **Why:** Enterprise and individual customers adopt CodeMender across three
  native deployment patterns ([SPEC-DIST](../openspec/specs/distribute/spec.md)):
  1. **Self-Dogfooding (`codemender-runner`):** Running `.github/workflows/*`
     and `.github/actions/*` directly on pull requests in this repository,
     resolving Python workspace packages from the checked-out repository
     workspace so pull requests test their own code changes.
  1. **Org-Level Controls (Fork + Rulesets / Cross-Org Action Reuse):** Cloning
     or forking the repository into a customer's GitHub Organization (enforcing
     workflows in `.github/workflows/*` via GitHub Organization Repository
     Rulesets, referencing public cross-org actions in `.github/actions/*`, or
     running `scripts/set-fork-owner.sh` for private forks) or GitLab Group as a
     central reusable workflow, action, or component catalog repository.
  1. **Single-Repo / Personal Account Adoption:** Calling workflows in
     `.github/workflows/*` via `workflow_call` or referencing composite actions
     in `.github/actions/*` directly from an individual application repository.
     Both GitHub (`.github/`) and GitLab (`templates/`) require fixed root paths
     for reusable workflows, composite actions, and CI/CD Catalog components.
     Because `.github/` and `templates/` do not collide, placing both at the
     root allows a single repository to serve both platforms and all adoption
     models out of the box.
- **Alternatives Rejected:**
  - *Platform-specific subdirectories (e.g., `github-actions/`, `gitlab-ci/`):*
    Prevents native GitHub cross-repo references and GitLab CI/CD Component
    Catalog discovery unless files are copied and rewritten into another
    repository.
  - *Separate repositories per CI platform:* Duplicates shared Python packages,
    Dockerfiles, and Terraform modules across multiple repositories.

### Decision 2: Three-Package Workspace (`packages/{codemender,launcher,repohost}`), Minimal Base Dependencies & Scoped Optional Extras

- **Design:** Split the Python codebase along process and security boundaries
  into three PEP 621 packages under `packages/`, linked by a root workspace
  `pyproject.toml`:

  1. **`packages/codemender` (`codemender`):** Installed inside the sandbox
     container (`docker/runner.Dockerfile`) and imported by the outer packages.
     Contains `sarif/`, `vcs/`, and `runner/` (which exposes the `cm-runner`
     console script entrypoint). Maintains **minimal base dependencies** so the
     sandbox runner stays lightweight and self-contained.
  1. **`packages/launcher` (`launcher`):** Provides both the host-side
     `Launcher` client (`local/` for local Docker/gVisor, `cloudrun/` for Google
     Cloud Run Jobs), the inside-container `Worker` server (`cm-worker`), and
     the shared `ArtifactCache` (`cache/`) that coordinates the repo-host-agnostic
     `find`, `verify`, and parallel `fix` workflows (see
     [ADR-0009](ADR-0009-abstraction-layers-and-dependency-injection.md)).
     Isolates GCP libraries under `[project.optional-dependencies] cloudrun = [...]`.
  1. **`packages/repohost` (`repohost`):** Runs on the CI host where repository
     host credentials (`GITHUB_TOKEN`, `CI_JOB_TOKEN`) live. Exposes dedicated
     per-host CLI entrypoints (`cm-github` and `cm-gitlab`) that invoke
     `launcher` to execute the scan/fix/verify workflows and translate the
     resulting SARIF findings and patches into GitHub PR reviews (`github/`) or
     GitLab MR discussions (`gitlab/`). Isolates platform libraries under
     `[project.optional-dependencies] github = [...]` and `gitlab = [...]`.

  The snippets below are **illustrative examples** showing how the root
  workspace `pyproject.toml` and per-package `pyproject.toml` files fit together
  (exact dependency versions and scripts may evolve in code without updating
  this ADR):

  ```toml
  # ILLUSTRATIVE EXAMPLE: Root pyproject.toml
  [tool.uv.workspace]
  members = ["packages/*"]

  [tool.pytest.ini_options]
  testpaths = ["packages"]
  python_files = ["test_*.py"]
  ```

  ```toml
  # ILLUSTRATIVE EXAMPLE: packages/codemender/pyproject.toml
  [build-system]
  requires = ["setuptools>=68.0"]
  build-backend = "setuptools.build_meta"

  [project]
  name = "codemender"
  version = "0.1.0"
  requires-python = ">=3.11"
  # Keep base dependencies minimal: only what core `runner`, `sarif`, and `vcs` need.
  dependencies = []

  # Console entrypoint installed on $PATH by `pip install` and `uvx`
  # (e.g., `uvx --from ./packages/codemender cm-runner <cmd>`):
  [project.scripts]
  cm-runner = "codemender.runner.__main__:main"

  # Exclude co-located unit tests and test fixtures from built wheels and installs.
  [tool.setuptools.packages.find]
  where = ["src"]
  include = ["codemender*"]
  exclude = ["test_*", "*_test", "testdata*"]
  ```

  In `packages/launcher/pyproject.toml` and `packages/repohost/pyproject.toml`,
  each package's base `dependencies` list declares only its internal workspace
  dependencies (such as `codemender`) and any lightweight libraries required
  across all execution modes. Target-specific third-party libraries—such as
  Google Cloud SDKs for Cloud Run execution or platform-specific HTTP/API
  clients for GitHub and GitLab—are isolated inside
  `[project.optional-dependencies]` extras (`cloudrun`, `github`, `gitlab`).
  When building a container image or installing via `uvx` / `pip`, the caller
  requests only the target extra it needs (for example,
  `pip install "./packages/launcher[cloudrun]"` or
  `pip install "./packages/repohost[github]"`), ensuring that a GitHub + Local
  Docker deployment never downloads or installs GitLab or Cloud Run dependencies
  (and vice versa):

  ```toml
  # ILLUSTRATIVE EXAMPLE: packages/launcher/pyproject.toml
  [build-system]
  requires = ["setuptools>=68.0"]
  build-backend = "setuptools.build_meta"

  [project]
  name = "codemender-launcher"
  version = "0.1.0"
  description = "CodeMender sandbox execution client (Launcher), container worker (Worker), and ArtifactCache"
  requires-python = ">=3.11"
  # Base install requires only the core `codemender` workspace package
  # (sufficient for running the `local` Docker/gVisor backend and local `cm-worker`).
  dependencies = ["codemender"]

  # Cloud Run SDKs are isolated in the `cloudrun` extra so local-only installs
  # do not need to install Google Cloud libraries.
  [project.optional-dependencies]
  cloudrun = [
      "google-cloud-storage>=2.14.0",
      "google-cloud-run>=0.10.0",
  ]

  # Inside-container sandbox worker entrypoint invoked by Launcher.run_worker()
  # (on the host, `launcher` is imported as a library by cm-github / cm-gitlab):
  [project.scripts]
  cm-worker = "launcher.worker:main"

  # Resolve `codemender` from the local monorepo workspace during development:
  [tool.uv.sources]
  codemender = { workspace = true }

  # Exclude co-located unit tests and test fixtures from built wheels and installs.
  [tool.setuptools.packages.find]
  where = ["src"]
  include = ["launcher*"]
  exclude = ["test_*", "*_test", "testdata*"]
  ```

  ```toml
  # ILLUSTRATIVE EXAMPLE: packages/repohost/pyproject.toml
  [build-system]
  requires = ["setuptools>=68.0"]
  build-backend = "setuptools.build_meta"

  [project]
  name = "codemender-repohost"
  version = "0.1.0"
  description = "CodeMender repository hosting platform integrations (GitHub, GitLab)"
  requires-python = ">=3.11"
  # Base install depends on the workspace packages (`codemender` and `codemender-launcher`).
  dependencies = [
      "codemender",
      "codemender-launcher",
  ]

  # Platform-specific HTTP/API libraries are scoped to per-host extras so a
  # GitHub runner only installs `[github]` and a GitLab image only installs `[gitlab]`.
  [project.optional-dependencies]
  github = ["requests>=2.31.0"]
  gitlab = ["requests>=2.31.0"]

  # Dedicated per-host CLI console entrypoints for outer CI execution:
  [project.scripts]
  cm-github = "repohost.github.__main__:main"
  cm-gitlab = "repohost.gitlab.__main__:main"

  # Resolve internal dependencies from the local monorepo workspace:
  [tool.uv.sources]
  codemender = { workspace = true }
  codemender-launcher = { workspace = true }

  # Exclude co-located unit tests and test fixtures from built wheels and installs.
  [tool.setuptools.packages.find]
  where = ["src"]
  include = ["repohost*"]
  exclude = ["test_*", "*_test", "testdata*"]
  ```

- **Why:**

  - **Enforces Security Boundaries at the Package Level:**
    `docker/runner.Dockerfile` installs only `packages/codemender` and
    `packages/launcher[cloudrun]` (see
    [ADR-0009](ADR-0009-abstraction-layers-and-dependency-injection.md)
    Decision 2), guaranteeing that repository host API clients (`packages/repohost`)
    are never present inside the `cm` sandbox image.
  - **Eliminates Orchestration Duplication:** Placing the multi-step `find`,
    `verify`, and `fix` workflow methods on `Launcher` and the inside-container
    task execution harness on `Worker` (`cm-worker`) in `packages/launcher`
    means GitHub (`repohost/github`) and GitLab (`repohost/gitlab`) share one
    pipeline engine instead of re-implementing workflow orchestration per repo
    host.
  - **Scoped Installation:** Isolating `[cloudrun]` in `packages/launcher`
    (with lazy `gs://` imports in `open_artifact_cache`) and `[github]` /
    `[gitlab]` in `packages/repohost` keeps dependencies strictly scoped.

- **Alternatives Rejected:**

  - *A single monolithic package for sandbox code, launcher, and repo hosts:*
    Blurs the boundary between code that runs inside the untrusted `cm` sandbox
    and code that talks to repository hosting APIs (`repohost`).
  - *Splitting every target into 6+ micro-packages immediately
    (`launcher-local`, `launcher-cloudrun`, `repohost-github`,
    `repohost-gitlab`):* Multiplies `pyproject.toml` boilerplate before
    individual targets are large enough to warrant separate distributions.

### Decision 3: Domain-Driven Subpackages (`vcs` vs. `repohost`, No `common/` or `utils/`) & Extensibility Conventions

- **Design:** Organize subpackages by domain cohesion with no generic `common/`
  or `utils/` directory (see [ADR-0001](ADR-0001.md)), strictly separate
  `codemender.vcs` from `repohost`, and follow three conventions in `launcher`
  and `repohost` so new execution backends and repo hosts can be added—or later
  split into standalone packages—with zero caller churn:
  1. **1-to-1 Subpackage-to-Extra Mapping:** Each execution backend
     (`launcher/local/`, `launcher/cloudrun/`) and each repo host
     (`repohost/github/`, `repohost/gitlab/`) lives in its own dedicated
     subpackage matching its `[project.optional-dependencies]` extra key.
  1. **Lazy Target Imports:** Package root `__init__.py` files
     (`launcher/__init__.py` and `repohost/__init__.py`) export only shared base
     interfaces and data models; they never eagerly import target subpackages
     (`cloudrun`, `github`, `gitlab`), ensuring omitted optional extras never
     trigger `ModuleNotFoundError`.
  1. **Future Package-Split Escape Hatch (PEP 420 Namespaces):** Callers always
     import concrete targets via their subpackage path
     (`from launcher.cloudrun import ...`, `from repohost.github import ...`).
     If any target subpackage later grows large enough to require its own
     `pyproject.toml` under `packages/`, it can be extracted as a PEP 420
     namespace distribution while keeping all `import` statements unchanged.
- **Why:**
  - **VCS vs. Repo Host Separation:** Version Control Systems (such as Git, and
    future Mercurial or Perforce backends) inspect local source trees, commits,
    and unified diffs. Repository Hosting Platforms (such as GitHub and GitLab)
    manage pull/merge requests, review threads, and remote HTTP APIs. Keeping
    `codemender.vcs` distinct from `repohost` allows `codemender.runner` and
    `launcher` to use local VCS and diff helpers without depending on repo-host
    APIs.
  - **Simple Now, Extensible Later:** Keeping 3 packages today avoids workspace
    sprawl while the 1-to-1 subpackage convention preserves a zero-refactor
    upgrade path as new platforms (e.g., Bitbucket, Gerrit, Kubernetes) are
    added.
- **Alternatives Rejected:**
  - *A top-level `common/` or `utils/` package:* Quickly becomes a dumping
    ground for unrelated helpers and obscures dependency direction.
  - *Placing GitHub and GitLab API clients inside `vcs/`:* Conflates local
    version control operations with remote repository hosting platforms.

### Decision 4: Co-Located Unit Tests (`test_<name>.py`) & Packaging Exclusions

Extracted to [ADR-0001](ADR-0001.md) (*Foundational Technical Stack, Build
Tooling, and Engineering Conventions*). Unit tests live beside the modules they
test as `test_*.py` inside `packages/*/src/` and are excluded from production
wheels and container images via `[tool.setuptools.packages.find]` `exclude` and
`.dockerignore`.

### Decision 5: Per-Target Dockerfiles (`docker/*.Dockerfile`) & Root `Makefile` Targets (Two-Way Door)

- **Design:** Place target Dockerfiles in `docker/` (`docker/runner.Dockerfile`
  for the unified sandbox container across local gVisor and Cloud Run, and
  `docker/gitlab.Dockerfile` for the outer GitLab CI job container), build all
  images using the repository root (`.`) as the Docker build context, and
  provide root `Makefile` targets (`make test`, `make build-runner`,
  `make build-cloudrun`, `make build-gitlab`). Per
  [ADR-0009](ADR-0009-abstraction-layers-and-dependency-injection.md)
  Decision 2, `make build-runner` and `make build-cloudrun` initially build the
  same unified `docker/runner.Dockerfile` image as a **two-way door**, preserving
  the ability to split `docker/cloudrun.Dockerfile` later with zero changes to
  Python code or CI callers.
- **Why:** Using the repository root (`.`) as the build context gives every
  Dockerfile direct access to `packages/codemender`, `packages/launcher`, and
  `packages/repohost` so each image installs strictly the package(s) and extras
  it requires. Wrapping these commands in a root `Makefile` (`make test`,
  `make build-runner`, `make build-cloudrun`, `make build-gitlab`) gives
  developers and CI pipelines simple, memorable commands without typing long
  `docker build -f ...` or multi-package test discovery invocations.
- **Alternatives Rejected:**
  - *Single root multi-stage `Dockerfile`:* Couples unrelated outer-CI and
    inner-sandbox images (`gitlab.Dockerfile` vs. `runner.Dockerfile`) into a
    single file.
  - *Subdirectory Docker build contexts:* Cannot access workspace packages under
    `packages/` cleanly.

______________________________________________________________________

## 4. `cm-runner` Runtime Contract (Carried Forward from `ADR-0002` & `ADR-0004`)

### Runtime Entrypoints & Container Packaging

Foundational Python (`>= 3.11`), PEP 621 packaging, `Makefile`, `pytest`, and
Terraform (`>= 1.5`) standards are defined in [ADR-0001](ADR-0001.md).

- **Console Entrypoint (`cm-runner` vs. `cm`):**
  - `cm` is the underlying native Go binary that performs CodeMender analysis
    and patch synthesis.
  - `cm-runner` is the Python CLI entrypoint generated from `[project.scripts]`
    in `packages/codemender/pyproject.toml` (pointing to
    `codemender.runner.__main__:main`) that wraps `cm` with structured
    [ADR-0002](ADR-0002.md) output and pre-flight checks.
  - Inside containers, `packages/codemender` and `packages/launcher[cloudrun]`
    are installed into `/opt/codemender/venv` on `$PATH` (see
    [ADR-0001](ADR-0001.md)).
- **Lean Base Runner Container (`docker/runner.Dockerfile`):** Packages OS
  essentials (`git`, `curl`, `tar`, `gzip`, `ca-certificates`), Python 3.11,
  `cm`, `packages/codemender` (`cm-runner`), and `packages/launcher[cloudrun]`
  (`cm-worker`, with `ENTRYPOINT ["cm-worker"]` as the default container
  entrypoint so `Launcher.run_worker()` only passes `--cache-uri` and
  `--task-id` arguments across Cloud Run Jobs and local Docker, while standalone
  local callers can invoke `docker run --entrypoint cm-runner`; see
  [ADR-0009](ADR-0009-abstraction-layers-and-dependency-injection.md)
  Decision 2), omitting `packages/repohost` and heavy compiler suites (see
  [SPEC-0003](../openspec/specs/runner-container-packaging/spec.md)).

### Security & Process Isolation Boundary

- **The process that runs `cm` never talks to the repo host.**
  `codemender.runner` runs inside the sandbox without repository host tokens and
  never imports `launcher` or `repohost`. Communicating with the repository host
  (posting PR/MR comments, pushing branches, uploading reports) is performed
  exclusively by the outer CI job (`packages/repohost` or native CI steps)
  outside the `cm` sandbox.
- **All `cm` calls go through `CmAdapter`.** `codemender.runner` invokes the
  `cm` binary exclusively through `CmAdapter`, which runs `cm` with closed
  `stdin`, separate `stdout`/`stderr` capture, and an explicit timeout,
  returning a structured `CmResult` instead of raising on non-zero `cm` exits.
  Per [ADR-0003](ADR-0003-environment-scrubbing.md), `CmAdapter` inherits the
  caller environment (`env=None`) so parent `cm` can authenticate to Vertex AI
  while `cm`'s built-in `exebox` sandbox isolates tool workers.

### CLI Invocation & Self-Contained Subcommands

```text
cm-runner [--cm-binary PATH] [--log-level LEVEL] [--no-init] <command> [flags]
```

- **Inputs & Outputs:** Inputs are CLI flags (or `-` for `stdin`). Outputs
  follow [ADR-0002](ADR-0002.md): one compact JSON object per line on `stdout`,
  human logs on `stderr`, and coarse exit codes (`0` success, `2` usage error,
  `3` execution failure). File artifacts are written to the directory specified
  by `--out`.
- **Stdout Purity by Construction:** `codemender.runner` gives the NDJSON event
  emitter the real `sys.stdout` stream and runs subcommands with Python's
  `sys.stdout` redirected to `sys.stderr`
  (`contextlib.redirect_stdout(sys.stderr)`), preventing stray `print()` calls
  in helpers from polluting `stdout`.
- **Self-Contained Commands via Automatic `init`:** Commands do not rely on
  state left behind by an earlier command. To make every command self-contained
  in ephemeral containers (`docker run --rm`, Cloud Run Jobs), the runner CLI
  automatically executes `init` before dispatching any command other than
  `version` or `init` unless `--no-init` is passed. When `init` runs
  automatically before another command, its check diagnostics route only to
  `stderr` so `stdout` carries exclusively the dispatched command's output.

### How to Build, Test, and Run

Standard `Makefile`, `pytest`, `uvx`, and container build workflows follow
[ADR-0001](ADR-0001.md) (`make test`, `make build-runner`,
`make build-cloudrun`, `make build-gitlab`).
