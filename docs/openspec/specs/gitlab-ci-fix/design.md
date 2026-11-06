<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-10-25' }
*-->

<!--
sdd_spec_id: SPEC-GITLAB-CI-FIX
sdd_implements: None
sdd_extends: SPEC-GITLAB-CI, SPEC-GHA-FIX, SPEC-FIX, SPEC-VERIFY
sdd_level: COMPONENT
sdd_archetype: CI_CD_WORKFLOW
sdd_cujspec_product: codemender-runner
-->

# Design: Fixes & Verification on Merge Requests (GitLab CI)

## Context

This design extends [SPEC-GHA-FIX](../github-actions-fix/design.md) and [SPEC-GITLAB-CI](../gitlab-ci/design.md) and records only the GitLab-specific decisions for fixes, MR suggestions, and on-demand verification.

### Reused vs. New Components

- **Reused Unchanged:**
  - `Launcher.fix()`, `Launcher.verify()`, and `Launcher.run_parallel()` generator streaming in `packages/launcher` ([CodeMender Runner System Design](../tech/systems/codemender-runner/design.md)), running over [SPEC-GITLAB-CI Decision 3](../gitlab-ci/design.md#decision-3)'s hardened `CloudRunLauncher`.
  - `SarifDocument.to_findings()`, `parse_patch_to_suggestions`, overlap detection, and sticky `Fix` column state transitions from `packages/codemender` and [SPEC-GHA-FIX](../github-actions-fix/design.md).
- **Brand New:**
  - GitLab MR Discussions publisher in `packages/repohost/src/repohost/gitlab/` (`suggestion:-N+0` formatter, dual old/new context-line positioning, per-hunk rollback, top-level patch fallback, and discussion overlap/verify replies).
  - `cm-gitlab verify` subcommand (`latest-scan.json` resolver and targeted `🚫 Dismissed` cell updater), deadline/cutoff enforcement, `codemender-verify` job in `templates/codemender.yml`, and `gitlab-ci/README.md`.

## Decisions

### Decision 1: Single-Job Cloud Run Fan-Out With Deadline Reserve {#decision-1}

- **Extends:** [SPEC-GHA-FIX Decision 1 & 6](../github-actions-fix/design.md#decision-1) ([CodeMender Runner System Design](../tech/systems/codemender-runner/design.md) Phase 1) over Cloud Run Jobs (`REQ-0004`, `REQ-0006`).
- **Choice:** `cm-gitlab find` dispatches up to `CODEMENDER_MAX_WORKERS` concurrent Cloud Run fix tasks from `codemender-scan` and enforces the `- 5 min` compute and `- 3 min` publishing cutoffs before the 60-minute job and OIDC token timeout.
- **Alternatives Considered:**
  - *Separate `parallel: N` fix jobs plus a fan-in report job ([CodeMender Runner System Design](../tech/systems/codemender-runner/design.md) Phase 2):* Only needed when fixes contend for runner VM CPU/RAM; with Cloud Run, extra CI jobs add queueing and shard coordination without speeding up fixes.

### Decision 2: Per-Hunk Discussion Rollback {#decision-2}

- **Overrides:** [SPEC-GHA-FIX Decision 3](../github-actions-fix/design.md#decision-3)'s single atomic GitHub PR review request (`REQ-0005.2`).
- **Choice:** Because GitLab's MR Discussions API creates one diff discussion per HTTP request and requires both old and new line numbers when a hunk ends on an unchanged context line, `cm-gitlab` deletes already-posted discussion parts if any later hunk of a multi-hunk fix fails, then posts a patch comment.
- **Alternatives Considered:**
  - *Leave posted parts in place:* Leaves a half-applied suggestion alongside a full patch comment.

### Decision 3: On-Demand `verify` Manual Job After `fix` {#decision-3}

- **Overrides:** [SPEC-VERIFY REQ-0008](../verify/spec.md#req-0008)'s pre-fix gating flow (`REQ-0001`, `REQ-0003`).
- **Choice:** `codemender-verify` is a manual job in the MR pipeline that runs after `codemender-scan`, replies with each outcome, and marks `DISMISSED` findings `🚫 Dismissed` in the sticky summary without removing suggestions or persisting dismissals across future scans.
- **Alternatives Considered:**
  - *Standalone pipeline triggered via UI/API:* Fails the WIF `pipeline_source == 'merge_request_event'` condition, whereas a manual job stays in the MR pipeline and accepts `CODEMENDER_VERIFY_FINDING` when triggered.
