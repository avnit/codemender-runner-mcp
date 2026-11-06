# `repohost` Package (Repository Hosting & `cm-github` CLI)

## Overview

`packages/repohost` implements the repository hosting integrations and CI/CD
control-plane entrypoints that connect pull/merge requests to sandboxed
`cm-runner` execution:

- **`repohost.github` (`cm-github` console script):** Orchestrates GitHub
  Actions pull request pre-flight checks (`cm-github --preflight`), diff scans,
  sticky PR summary comment updates, inline ```` ```suggestion ```` vs. unified
  diff patch comment routing
  ([ADR-0006](../../docs/adr/ADR-0006-pr-remediation-routing-and-security-gate.md)),
  and on-demand `@cm-runner verify` review comment workflows.
- **`repohost.base`, `repohost.models`, `repohost.testing` (`RepoHost`, `InMemoryRepoHost`):**
  Defines the [CodeMender Runner System Design](../../docs/openspec/specs/tech/systems/codemender-runner/design.md)
  `RepoHost` abstraction boundary, domain models (`ChangeContext`, `SuggestedEdit`,
  `SecurityGate`), and hermetic test fake, keeping VCS credentials (`GITHUB_TOKEN`,
  PR comments, and workflow artifacts) out of `packages/launcher` containers.
- **`repohost.gitlab` *(In Development)*:** Package namespace for GitLab CI merge
  request integration ([SPEC-GL](../../docs/openspec/specs/gitlab-ci/spec.md)).

## Platform Setup Guides

- [GitHub Actions Setup & Usage Guide](../../.github/SETUP.md)
- [GitLab CI Specification *(In Development)*](../../docs/openspec/specs/gitlab-ci/spec.md)

## Development & Testing

```bash
# Run the full test suite via Makefile (sets PYTHONPATH automatically):
make test

# Or after a one-time editable install in your virtualenv:
pip install -e ./packages/codemender -e ./packages/launcher -e "./packages/repohost[github,gitlab]" pytest setuptools
pytest packages/repohost
```

## Specifications & Architecture Decisions

- [SPEC-GHA](../../docs/openspec/specs/github-actions/spec.md)
  — GitHub Actions pull request scan orchestration.
- [SPEC-GHA-FIX](../../docs/openspec/specs/github-actions-fix/spec.md)
  — Automated pull request fix generation and suggestion routing.
- [SPEC-GHA-VERIFY](../../docs/openspec/specs/github-actions-verify/spec.md)
  — On-demand `@cm-runner verify` pull request comment workflow.
- [SPEC-GL](../../docs/openspec/specs/gitlab-ci/spec.md)
  — GitLab CI merge request integration.
- [ADR-0006: PR Remediation
  Routing](../../docs/adr/ADR-0006-pr-remediation-routing-and-security-gate.md)
  — Pull request remediation routing and security gate.
- [CodeMender Runner System
  Design](../../docs/openspec/specs/tech/systems/codemender-runner/design.md)
  — Abstraction layers, dependency injection seams, and per-host CLI wiring.
