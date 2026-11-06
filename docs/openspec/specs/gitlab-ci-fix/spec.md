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

# SPEC-GITLAB-CI-FIX: Fixes & Verification on Merge Requests (GitLab CI)

**Implements Spec:** None

**Extends Spec:** [SPEC-GITLAB-CI](../gitlab-ci/spec.md), [SPEC-GHA-FIX](../github-actions-fix/spec.md), [SPEC-FIX](../fix/spec.md), [SPEC-VERIFY](../verify/spec.md)

**Governing Journeys:**

- **Merge Request Author (`persona-mr-author`)**: As a GitLab developer, I want a fix for each finding on my MR as a one-click suggestion (or a patch when a suggestion can't apply), a `Fix` column in the sticky summary, and on-demand exploitability verification.
- **Enterprise Security & Platform Lead (`persona-security-auditor`)**: As a security architect, I mandate that fixes and verification run on Cloud Run without GitLab tokens, that MR comments use a Reporter token that can't push, and that slow or failed fixes never block the scan's results.

## Overview

This specification extends [SPEC-GHA-FIX](../github-actions-fix/spec.md), [SPEC-GITLAB-CI](../gitlab-ci/spec.md), [SPEC-FIX](../fix/spec.md), and [SPEC-VERIFY](../verify/spec.md) under [ADR-0006](../../../adr/ADR-0006-pr-remediation-routing-and-security-gate.md) and [CodeMender Runner System Design](../tech/systems/codemender-runner/design.md) Phase 1.

### Reused vs. New

- **Reused Unchanged:**
  - Single-finding `fix` and `verify` contracts ([SPEC-FIX](../fix/spec.md), [SPEC-VERIFY](../verify/spec.md)) and Phase 1 bounded-parallel orchestration ([CodeMender Runner System Design](../tech/systems/codemender-runner/design.md)).
  - Per-finding SARIF splitting (`REQ-0004`), 35-minute per-fix timeout and `fixed`/`no_fix`/`failed` classification (`REQ-0003`, `REQ-0005`), progressive sticky `Fix` column states (`REQ-0006`), whole-patch suggestion eligibility (`REQ-0007`), collapsible patch comment layout and dynamic backtick fencing (`REQ-0008.1–2`), `finding_id` deduplication (`REQ-0009`), overlapping-line warning text (`REQ-0010`), and README topics (`REQ-0012`) from [SPEC-GHA-FIX](../github-actions-fix/spec.md), mapped as:

| `SPEC-GHA-FIX` Concept | GitLab CI Equivalent |
| :--- | :--- |
| Action inputs `fix`, `max_parallel_fixes` (`REQ-0001`) | CI/CD variables `CODEMENDER_FIX_ENABLED` (`'true'`), `CODEMENDER_MAX_WORKERS` (`'4'`, also bounds `verify`) |
| `GITHUB_TOKEN` / PR review comments (`REQ-0006`–`REQ-0010`) | `CODEMENDER_GITLAB_TOKEN` (when set) / MR discussions and notes |
| `github-actions/README.md` (`REQ-0012`) | `gitlab-ci/README.md` |

- **Overridden from `SPEC-GHA-FIX` / `SPEC-VERIFY`:**
  - Bot authorship check and GitLab diff-position currency rules for `<!-- codemender-finding:... -->` notes (`REQ-0002`).
  - Post-scan on-demand `cm-gitlab verify` instead of pre-fix verification gating, resolving `latest-scan.json`, replying on findings, and marking `DISMISSED` findings `🚫 Dismissed` in the sticky summary (`REQ-0003`).
  - `< 10 min` pre-fix compute-deadline reserve and `cm-logs/` staging (`REQ-0004`).
  - GitLab ` ```suggestion:-N+0 ` multi-line syntax (with old + new line numbers on context anchors), per-hunk discussion rollback on partial failure, additional `.gitlab-ci.yml`/`.gitlab/`/`templates/`/symlink exclusions, and top-level MR note fallback (`REQ-0005`).
- **Brand New for GitLab CI:**
  - `CODEMENDER_VERIFY_FINDING` and the manual `codemender-verify` job in `templates/codemender.yml` (`REQ-0001`).
  - `- 3 min` MR comment publishing cutoff and discussion replies for overlapping-line notices (`REQ-0006`).

## Requirements

### REQ-0001: Variables & `codemender-verify` Manual Job {#req-0001}

- **CUJ UUID:** `cuj-gitlab-fix-pipeline-config`

Beyond the mapped `CODEMENDER_FIX_ENABLED` and `CODEMENDER_MAX_WORKERS` variables, `cm-gitlab` MUST support `CODEMENDER_VERIFY_FINDING` (default `'all'`, accepting comma-separated `finding_id`s, `finding-NNN` ordinals, or `all`). `templates/codemender.yml` MUST define `codemender-verify` (`when: manual`, `allow_failure: true`) in the MR pipeline sharing `codemender-scan`'s image, `id_tokens`, `inherit`, and MR rules ([SPEC-GITLAB-CI REQ-0008.3](../gitlab-ci/spec.md#req-0008)), with `needs: [{job: codemender-scan, artifacts: false}]`, `GIT_STRATEGY: none`, `timeout: 60 minutes`, `cm-logs/` artifacts, and script `/usr/local/bin/cm-gitlab verify --findings "${CODEMENDER_VERIFY_FINDING:-all}"`.

#### Scenario: Running `codemender-verify` from the MR pipeline

- **GIVEN** `codemender-scan` succeeded in a merge request pipeline
- **WHEN** a developer triggers `codemender-verify` with default variables
- **THEN** it MUST verify every finding of that commit's latest scan.

### REQ-0002: Bot Note Authorship & Diff Note Currency {#req-0002}

- **CUJ UUID:** `cuj-gitlab-deduplication`

**Overrides [SPEC-GHA-FIX REQ-0009.2–3](../github-actions-fix/spec.md#req-0009):** Only MR notes carrying `<!-- codemender-finding:<finding_id> -->` and authored by `CODEMENDER_GITLAB_TOKEN`'s user MUST count for deduplication; a diff note MUST count as current when `position.head_sha` matches the MR head or GitLab still maps it to a current diff line, and a top-level MR note MUST always count as current.

#### Scenario: Ignoring a spoofed finding marker and reusing a current bot note

- **GIVEN** a human posted `<!-- codemender-finding:aaa111 -->` and the bot user posted a diff note with `<!-- codemender-finding:bbb222 -->` still mapped to a current diff line
- **WHEN** `cm-gitlab find` plans fixes
- **THEN** it MUST run `fix` for `aaa111` and skip `bbb222`, linking `bbb222`'s `Fix` cell to the bot note.

### REQ-0003: On-Demand `cm-gitlab verify` {#req-0003}

- **CUJ UUID:** `cuj-gitlab-verify-and-security-gate`

**Overrides [SPEC-VERIFY REQ-0008](../verify/spec.md#req-0008):** Instead of gating `fix` before remediation, `cm-gitlab verify` runs on demand after `codemender-scan`:

1. It MUST resolve the commit's latest scan via `latest-scan.json` ([SPEC-GITLAB-CI REQ-0008.1](../gitlab-ci/spec.md#req-0008)), rejecting pointers outside `<CI_PROJECT_ID>/<head_sha>/`, and exit `1` before starting Cloud Run if the scan's metadata, snapshot, or selected findings are missing.
1. It MUST resolve `--findings` entries as `finding_id` first, then as `finding-NNN` ordinals (skipping unmatched entries and exiting `0` if none match), and run selected findings on Cloud Run per [SPEC-VERIFY](../verify/spec.md) under `CODEMENDER_MAX_WORKERS` and the compute deadline (REQ-0004).
1. When `CODEMENDER_GITLAB_TOKEN` is set, it MUST reply with each outcome on the finding's marked note (or a top-level MR note) and edit only `DISMISSED` findings' `Fix` cells in the sticky summary to `🚫 Dismissed`, leaving other cells, suggestions, and SARIF results unchanged.

#### Scenario: Reporting `verify` outcomes

- **GIVEN** `CODEMENDER_GITLAB_TOKEN` is set and `verify` finds `finding-001` `DISMISSED` and `finding-002` `VERIFIED`
- **WHEN** `cm-gitlab verify` finishes
- **THEN** it MUST reply with both outcomes, set `finding-001`'s `Fix` cell to `🚫 Dismissed`, and leave `finding-002`'s cell unchanged.

### REQ-0004: Fix Deadline Reserve & Artifact Staging {#req-0004}

- **CUJ UUID:** `cuj-gitlab-fix-sharding-and-suggestions`

**Overrides [SPEC-GHA-FIX REQ-0011](../github-actions-fix/spec.md#req-0011)** (with Cloud Run task execution governed by [SPEC-GITLAB-CI REQ-0003](../gitlab-ci/spec.md#req-0003) and [SPEC-GHA-FIX REQ-0003, REQ-0005](../github-actions-fix/spec.md#req-0003)): `cm-gitlab find` MUST NOT start a new fix execution once `< 10 minutes` remain before the compute deadline ([SPEC-GITLAB-CI REQ-0008.2](../gitlab-ci/spec.md#req-0008)), recording unstarted or deadline-canceled fixes as `failed`, and MUST stage each fix's log and non-empty UTF-8 diff in `cm-logs/`.

#### Scenario: Reaching the deadline during fixes

- **GIVEN** `find` succeeded and two fixes are still running at the compute deadline
- **WHEN** `cm-gitlab find` handles the deadline
- **THEN** it MUST cancel both executions, show them as `⚠️ Failed`, and exit `0`.

### REQ-0005: Multi-Line Suggestion Syntax, Per-Hunk Rollback & Patch Fallback {#req-0005}

- **CUJ UUID:** `cuj-gitlab-remediation-routing`

**Overrides [SPEC-GHA-FIX REQ-0007](../github-actions-fix/spec.md#req-0007) and [REQ-0008.3–4](../github-actions-fix/spec.md#req-0008):**

1. Inline suggestions MUST additionally reject symlinks, `.gitlab-ci.yml`, `.gitlab/`, `templates/`, and replacement lines containing ```` ``` ```` or `~~~`.
1. Each hunk MUST be posted as a ` ```suggestion:-N+0 ` MR diff discussion anchored on the last replaced line (`N = replaced_lines - 1`), passing both old and new line numbers when anchored on an unchanged context line; if any part of a multi-hunk fix fails to post, `cm-gitlab` MUST delete already-posted parts before falling back to a patch comment.
1. When the flagged line is outside the MR diff or rejected by GitLab, the patch comment MUST fall back to a top-level MR note (applying [SPEC-GITLAB-CI REQ-0007.2](../gitlab-ci/spec.md#req-0007)'s `/` and `@` neutralization and 1,000,000-character limit).

#### Scenario: Rolling back a partly posted multi-part suggestion

- **GIVEN** a 2-hunk patch inside the MR diff where posting part 2 fails
- **WHEN** `cm-gitlab find` publishes it
- **THEN** it MUST delete part 1 and post a patch comment instead.

### REQ-0006: Publishing Cutoff & Discussion Replies {#req-0006}

- **CUJ UUID:** `cuj-gitlab-summary-and-overlaps`

`cm-gitlab find` MUST stop publishing MR comments at `CI_JOB_STARTED_AT + CI_JOB_TIMEOUT - 3 minutes` so artifacts upload before job timeout, and MUST post overlapping-line notices ([SPEC-GHA-FIX REQ-0010.1](../github-actions-fix/spec.md#req-0010)) as replies on each overlapping fix's MR discussion once all fixes finish.

#### Scenario: Stopping at the publishing cutoff

- **GIVEN** the GitLab API keeps returning HTTP `503` while publishing fixes
- **WHEN** `CI_JOB_STARTED_AT + CI_JOB_TIMEOUT - 3 minutes` is reached
- **THEN** `cm-gitlab find` MUST stop publishing and exit `0`.
