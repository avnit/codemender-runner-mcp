# Design: Lean Runner Container & Modular BYOI Packaging

## Context

CodeMender runs in customer CI pipelines and executes untrusted code, compilers, and test suites. Packaging every potential language runtime (Java, Go, Node.js, Python, Rust) into a single monolithic base container results in massive images (>1.5 GB), slow cold-start pulling in Cloud Run and GitHub Actions, and elevated CVE vulnerability surface.

Governed by [SPEC-0003](spec.md), [ADR-0001](../../../adr/ADR-0001-machine-readable-output-contract.md), and [ADR-0004](../../../adr/ADR-0004-project-tech-stack-and-repo-architecture.md), this design specifies a lean container packaging strategy that strips the base image to core orchestrator essentials and defers specialized toolchains to modular custom images.

## Goals / Non-Goals

### Goals
*   Provide a lean, fast-pulling base container image packaging only what is necessary for runner orchestration (minimal Linux OS, Python 3.11, Git, and `cm`).
*   Isolate the Python orchestrator runtime inside `/opt/codemender/venv`.
*   Package the CodeMender binary (`cm`) on `$PATH` without rigid hardcoded path dependencies.
*   Support modular Bring-Your-Own-Image (BYOI) inheritance for language-specific build environments.
*   Automate image building and publishing via `.github/workflows/build_runner_image.yml`.

### Non-Goals
*   Pre-installing heavy multi-language compiler suites (JDK, Node.js, Go) into the initial base image (evaluated post-launch).
*   Dynamically downloading compilers at runtime over the public internet during CI runs.

## Decisions

### Decision 1: Lean Base Image vs. Monolithic Multi-Language Monolith
*   **Choice**: Package only Python 3.11, Git, essential utilities, and the `cm` binary in the base image.
*   **Rationale**: 
    1. Reduces base image footprint by over 80% (from ~1.5 GB to ~200 MB).
    2. Minimizes cold-start image pull latency in Cloud Run and GitHub Actions runner environments.
    3. Significantly reduces security patching surface by omitting hundreds of unnecessary OS packages.
    4. Custom/multi-language compiler toolchains are easily layered via BYOI or custom container images when required.

### Decision 2: Include `cli/` in Container Build
*   **Choice**: Explicitly copy the `cli/` module into `/opt/codemender/codemender-runner/cli`.
*   **Rationale**: Enables container invocation of `cm-runner` CLI commands (`python3 -m cli version`).

## Architecture

```dot
digraph ContainerLayout {
  rankdir=TB;
  node [shape=box, style="filled,rounded", fontname="Helvetica"];

  Base [label="Base OS: ubuntu:22.04 (minimal)", fillcolor="#E8F0FE"];
  Runtime [label="Lean Core Runtime\nPython 3.11, Git, Core Utils", fillcolor="#CEEAD6"];
  CMBinary [label="CodeMender Binary (cm on PATH)\nGo Binary", fillcolor="#FCE8E6"];
  Venv [label="Isolated Python Venv\n/opt/codemender/venv", fillcolor="#FEF7E0"];
  Modules [label="Orchestrator Package\n/opt/codemender/codemender-runner/\n(codemender, vcs, cli, storage.py)", fillcolor="#FEF7E0"];
  BYOI [label="Custom Toolchain Images (BYOI)\nFROM ghcr.io/<org>/codemender-runner:latest\n(Add Node, Java, Go as needed)", style="dashed", fillcolor="#F1F3F4"];

  Base -> Runtime;
  Base -> CMBinary;
  Base -> Venv;
  Venv -> Modules;
  Modules -> BYOI [style="dotted", label="Extended by"];
}
```

## Risks / Trade-offs

-   **Repositories Requiring Compilers** → Projects needing dynamic compilation or execution during verification must use extended BYOI images. Mitigated by straightforward `FROM` inheritance templates.

## Deployment & Rollback

-   Images published to GHCR with commit SHA and `:latest` tags.
-   Rollback by pinning workflow inputs (`runner_image`) to a previous verified tag.
