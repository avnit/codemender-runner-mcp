# `repohost` Package (Repository Hosting & `cm-github` CLI)

> [!WARNING]
> **Work in Progress:** Detailed `RepoHost` adapter interfaces, pull/merge
> request sticky summary comment and inline suggestion routing rules, and
> `cm-github` CLI usage are being consolidated into this guide.

## Overview

`packages/repohost` implements the repository hosting integrations
(`repohost.github` and `repohost.gitlab`) and exposes the `cm-github` console
script entrypoint for orchestrating pull request diff scans, inline fix
suggestions, patch comments, and `@cm-runner verify` comment workflows.

## Platform Setup Guides

- [GitHub Actions Setup & Usage Guide](../../.github/README.md)
- [GitLab CI Setup & Usage Guide](../../gitlab-ci/README.md)

## Specifications & Architecture Decisions

- [SPEC-GHA](../../docs/openspec/specs/github-actions/spec.md)
  — GitHub Actions pull request scan orchestration.
- [SPEC-GHA-FIX](../../docs/openspec/specs/github-actions-fix/spec.md)
  — Automated pull request fix generation and suggestion routing.
- [SPEC-GHA-VERIFY](../../docs/openspec/specs/github-actions-verify/spec.md)
  — On-demand `@cm-runner verify` pull request comment workflow.
- [SPEC-GL](../../docs/openspec/specs/gitlab-ci/spec.md)
  — GitLab CI merge request integration.
- [ADR-0009: Abstraction Layers &
  DI](../../docs/adr/ADR-0009-abstraction-layers-and-dependency-injection.md)
  — Abstraction layers, orchestration ownership, and dependency injection.
- [ADR-0006: PR Remediation
  Routing](../../docs/adr/ADR-0006-pr-remediation-routing-and-security-gate.md)
  — Pull request remediation routing and security gate.
