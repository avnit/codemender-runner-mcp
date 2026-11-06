# User Journeys

This directory contains specifications defining the end-to-end user and operator
journeys (`sdd_level: JOURNEY`) for CodeMender.

## Active Journeys

- **Pull Request Automated Remediation (`pr-automated-remediation`)**:
  [spec.md](pr-automated-remediation/spec.md) ·
  [design.md](pr-automated-remediation/design.md) — End-to-end developer journey
  for pull request scanning, consolidated in-review finding notification and
  remediation status communication, direct inline suggested edit delivery with
  complete-patch fallback, non-blocking remediation, and cross-commit
  conversation hygiene across supported repository hosts.
- **Pull Request On-Demand Finding Verification (`pr-on-demand-verify`)**:
  [spec.md](pr-on-demand-verify/spec.md) ·
  [design.md](pr-on-demand-verify/design.md) — End-to-end developer and reviewer
  journey for triggering authorized in-review on-demand exploitability
  verification (such as `@codemender verify`), immediate in-progress
  acknowledgment, and actionable verification verdicts with non-blocking
  execution and concurrency protection.
