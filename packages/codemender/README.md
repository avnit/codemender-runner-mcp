# `codemender` Package (`cm-runner` CLI Orchestrator)

> [!WARNING]
> **Work in Progress:** Detailed `cm-runner` subcommand flag tables, JSON Lines
> and SARIF v2.1.0 output schemas, and standalone CI pipeline examples are being
> consolidated into this guide from
> [`docs/guides/calling_cm_runner.md`](../../docs/guides/calling_cm_runner.md).

## Overview

`packages/codemender` provides the scenario-independent `cm-runner` CLI
orchestrator (`version`, `init`, `find-diff`, `fix`, `verify`), `cm` binary
subprocess execution with environment variable scrubbing, SARIF 2.1.0
normalization, and Git working tree pre-flight validation.

## CLI Subcommands & Usage

Until the CLI reference finishes migrating to this file, see
[`docs/guides/calling_cm_runner.md`](../../docs/guides/calling_cm_runner.md) for
complete flag tables, exit codes (`0`, `2`, `3`), and end-to-end shell examples
for `version`, `init`, `find-diff`, `fix`, and `verify`.

## Specifications & Architecture Decisions

- [SPEC-FIND-DIFF](../../docs/openspec/specs/find-diff/spec.md)
  — Diff scanning (`cm-runner find-diff`) and SARIF v2.1.0 output contract.
- [SPEC-FIX](../../docs/openspec/specs/fix/spec.md)
  — Single-finding patch generation (`cm-runner fix`).
- [SPEC-VERIFY](../../docs/openspec/specs/verify/spec.md)
  — Single-finding exploitability verification (`cm-runner verify`).
- [ADR-0001](../../docs/adr/ADR-0001-machine-readable-output-contract.md)
  — Machine-readable `stdout` / `stderr` and exit-code contract.
- [ADR-0003](../../docs/adr/ADR-0003-environment-scrubbing.md)
  — Environment variable scrubbing when invoking `cm`.
- [ADR-0008: Repository
  Layout](../../docs/adr/ADR-0008-repository-layout-and-package-structure.md)
  — Three-package workspace layout.
