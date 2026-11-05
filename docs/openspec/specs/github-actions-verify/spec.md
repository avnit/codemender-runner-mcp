<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-10-16' }
*-->

<!--
sdd_spec_id: SPEC-GHA-VERIFY
sdd_implements: None
sdd_extends: SPEC-GHA, SPEC-GHA-FIX, SPEC-VERIFY
sdd_level: COMPONENT
sdd_archetype: CI_CD_WORKFLOW
sdd_cujspec_product: codemender-runner
-->

# SPEC-GHA-VERIFY: On-Demand Verification on Pull Requests (GitHub Action, M3)

**Implements Spec:** None

**Extends Spec:** [SPEC-GHA](../github-actions/spec.md),
[SPEC-GHA-FIX](../github-actions-fix/spec.md), [SPEC-VERIFY](../verify/spec.md)

**Governing Journeys:**
- **Pull Request Reviewer / Developer (`persona-pr-author`)**: As a developer reviewing a CodeMender fix comment on my pull request, I want to comment `@cm-runner verify` under the fix to verify on demand whether the vulnerability is actually exploitable, see immediate feedback that verification has started, and receive the verification outcome directly in that review thread.
- **Security & Platform Lead (`persona-security-auditor`)**: As a security lead, I require that on-demand verification can only be triggered by repository collaborators/members, caps concurrent runs per PR (`MAX_CONCURRENT_VERIFIES = 4`), blocks duplicate runs on the same finding, keeps `GITHUB_TOKEN` out of the container, and blocks untrusted fork execution.

## Overview

In M1 ([SPEC-GHA](../github-actions/spec.md)) and M2
([SPEC-GHA-FIX](../github-actions-fix/spec.md)), opening or pushing to a pull
request runs `cm-runner find-diff` (posting a sticky summary comment) followed
by parallel `cm-runner fix` containers (posting an inline suggestion review or
a fallback patch comment for each finding, tagged with
`<!-- codemender-finding:<finding_id> -->`).

Because dynamic exploit verification (`cm-runner verify`,
[SPEC-VERIFY](../verify/spec.md)) takes 10–30 minutes per finding, running it
automatically for every finding before `fix` would double PR turnaround times.
Instead, M3 runs `cm-runner verify` **on demand** when an authorized reviewer
or developer replies `@cm-runner verify` inside a CodeMender fix review comment
thread (`pull_request_review_comment`).

The rationale and trade-offs behind each requirement are in
[design.md](design.md).

This specification is governed by:
- [SPEC-VERIFY](../verify/spec.md): `cm-runner verify` CLI contract and SARIF status metadata (`VERIFIED`, `DISMISSED`, `OPEN`).
- [SPEC-GHA-FIX](../github-actions-fix/spec.md): PR fix comments and `<!-- codemender-finding:<finding_id> -->` fingerprinting.
- [ADR-0002](../../../adr/ADR-0002.md): machine-readable `stdout`, human logs on `stderr`, and coarse exit codes (`0`, `2`, `3`).
- [ADR-0003](../../../adr/ADR-0003-environment-scrubbing.md): explicit environment allowlisting into the gVisor container.
- [ADR-0008](../../../adr/ADR-0008-repository-layout-and-package-structure.md): `GITHUB_TOKEN` remains on the CI host and never enters the container.

---

## Requirements

### REQ-0001: Workflow Trigger, Fast Job Gate & Permissions {#req-0001}

- **CUJ UUID:** `cuj-gha-verify-workflow-trigger`

1. The workflow template (`.github/workflows/cm-merge-scan.yml`) MUST
   trigger on both `pull_request` (`types: [opened, synchronize, reopened]`) and
   `pull_request_review_comment` (`types: [created]`).
2. To prevent normal human review comments, top-level comments, unauthorized
   users' comments, bot comments, or untrusted cross-repo fork PR comments from
   provisioning a GitHub Actions runner VM, the workflow job MUST include a
   job-level `if:` condition that requires
   `github.event.pull_request.head.repo.full_name == github.repository` and
   allows VM provisioning and execution only when:
   - `github.event_name == 'pull_request'`, OR
   - `github.event.comment.in_reply_to_id != null`,
     `contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.comment.author_association)`,
     and `contains(github.event.comment.body, '@cm-runner verify')`.
3. The workflow's `actions/checkout` step MUST check out
   `ref: ${{ github.event.comment.commit_id || github.event.pull_request.head.sha }}`
   with `fetch-depth: 0` and `persist-credentials: false` so on-demand
   verification runs against the exact commit snapshot the fix comment targets.
4. The workflow's `permissions:` block MUST include `actions: read` (in addition
   to `contents: read`, `id-token: write`, `packages: read`, and
   `pull-requests: write`) so the action can download the PR's scan artifacts
   via the GitHub REST API.

#### Scenario: Normal or unauthorized review comment does not start a runner VM
- **GIVEN** a pull request with the CodeMender workflow enabled
- **WHEN** a user posts a normal review comment `"Looks good to me"` OR an external contributor (`author_association` not in `["OWNER", "MEMBER", "COLLABORATOR"]`) comments `@cm-runner verify`
- **THEN** the workflow job MUST be skipped by the job-level `if:` condition before a runner VM is ever provisioned.

#### Scenario: Fork PR comment is blocked from running with base repo secrets
- **GIVEN** a pull request opened from an external fork (`head.repo.full_name != github.repository`)
- **WHEN** a user comments `@cm-runner verify` on the fork PR
- **THEN** the job-level `if:` condition (and the action's pre-flight check) MUST skip execution so untrusted fork code is never checked out and run in a privileged `pull_request_review_comment` context.

---

### REQ-0002: Fast Pre-Flight Validation & Defense-in-Depth Gate {#req-0002}

- **CUJ UUID:** `cuj-gha-verify-preflight`

1. Once the workflow's job-level `if:` condition (`REQ-0001`) passes and a runner
   VM is provisioned for `GITHUB_EVENT_NAME == 'pull_request_review_comment'`,
   the composite action (`.github/actions/codemender/action.yml` or
   `.github/actions/codemender-local/action.yml`) MUST
   execute a lightweight pre-flight validation step (`cm-github --preflight`)
   **before** authenticating to Google Cloud (WIF), installing gVisor (`runsc`),
   or pulling the `cm-runner` Docker image.
2. If the pre-flight step determines that verification should not run, it MUST
   write `should_run=false` to `$GITHUB_OUTPUT`, and all subsequent heavy steps
   (WIF authentication, gVisor installation, Docker image pull, and container
   execution) MUST be skipped with exit code `0`.
3. The pre-flight check MUST verify all of the following before allowing a
   verification run (re-checking the workflow `if:` conditions as
   defense-in-depth in case a customer customizes their workflow YAML, and
   inspecting the parent comment via the GitHub API):
   - The event is `pull_request_review_comment` with `action == "created"`.
   - The comment author is not a bot (`comment.user.type != "Bot"`).
   - The comment is not on a cross-repository fork PR (`is_fork_pr(event)` is
     `False`).
   - The comment body contains `@cm-runner verify` (or `@codemender-runner verify`
     / `@codemender verify`) outside fenced code blocks and inline code spans.
   - The comment is a reply in a review comment thread (`comment.in_reply_to_id`
     is a positive integer).
   - The comment author's `author_association` is in
     `{"OWNER", "MEMBER", "COLLABORATOR"}` (case-insensitive). If the author is
     not authorized, the action MUST log a `::warning::` and exit `0` without
     invoking `cm-runner` or posting to the PR.
   - The parent root review comment (`GET /repos/{repo}/pulls/comments/{in_reply_to_id}`)
     was posted by a bot and carries a valid CodeMender finding marker
     `<!-- codemender-finding:<finding_id> -->`. If the parent comment is not a
     CodeMender finding comment, the action MUST log a `::notice::` and exit `0`.

#### Scenario: Defense-in-depth check when workflow `if:` is bypassed
- **GIVEN** an external contributor with `author_association: "NONE"` or `"CONTRIBUTOR"` triggers the action in a repository whose workflow YAML omitted the `author_association` `if:` filter
- **WHEN** the pre-flight step runs
- **THEN** the pre-flight check MUST set `should_run=false`, log a warning, and exit `0` without authenticating to GCP, installing gVisor, or pulling the runner image.

#### Scenario: Replying `@cm-runner verify` to a human comment
- **GIVEN** a collaborator replies `@cm-runner verify` to another human's review comment (no `<!-- codemender-finding:... -->` tag on the parent comment)
- **WHEN** the pre-flight step runs
- **THEN** it MUST set `should_run=false`, log a notice, and exit `0`.

---

### REQ-0003: Concurrency Model, Per-Finding Deduplication, Concurrency Cap (`MAX_CONCURRENT_VERIFIES = 4`) & Stale Lock Recovery {#req-0003}

- **CUJ UUID:** `cuj-gha-verify-concurrency`

1. **Per-Thread Trigger & Workflow Concurrency Isolation:**
   - On-demand verification is triggered per finding review thread (one
     `@cm-runner verify` reply per finding thread, rather than listing multiple
     finding IDs in a single comment).
   - The workflow's `concurrency:` configuration MUST isolate PR scan runs from
     per-thread verification runs:
     - `group: codemender-${{ github.event.pull_request.number }}-${{ github.event.comment.in_reply_to_id || github.event.comment.id || 'scan' }}`
     - `cancel-in-progress: ${{ github.event_name == 'pull_request' }}`
   - This ensures that:
     - Pushing a new commit cancels a stale `scan` run without cancelling active
       on-demand verifications.
     - Replying `@cm-runner verify` on multiple different finding threads (e.g.
       three verify comments in a row on Findings A, B, and C) launches parallel
       workflow runs on separate runner VMs because each thread has a distinct
       `in_reply_to_id`.
     - Concurrent verification runs do not collide when posting or updating
       comments: each run creates its own reply comment inside its own finding
       thread and only updates (`PATCH`) that specific `reply_comment_id`, never
       modifying the shared sticky PR summary comment or other threads.
     - Rapid duplicate `@cm-runner verify` comments in the **same** review
       thread share the same `in_reply_to_id` concurrency group and are
       serialized so the second run observes the first run's `in-progress` lock.
2. The action MUST define the maximum number of concurrent on-demand
   verifications per pull request as a constant `MAX_CONCURRENT_VERIFIES = 4`,
   and the stale in-progress lock timeout as `VERIFY_LOCK_TTL_SECONDS = 45 * 60`
   (45 minutes).
3. Every verification comment posted by CodeMender MUST embed a machine-readable
   HTML marker of the form:
   `<!-- codemender-verify:<state> finding:<finding_id> [status:<status>] -->`
   where `<state>` is one of `in-progress`, `done`, or `failed`, and
   `<finding_id>` is the finding fingerprint from the parent fix comment's
   `<!-- codemender-finding:<finding_id> -->` tag.
4. During pre-flight, the action MUST list the PR's review comments
   (`GET /repos/{repo}/pulls/{pr_number}/comments`) and inspect all existing
   bot comments carrying `<!-- codemender-verify:... -->`:
   - An `in-progress` comment whose `updated_at` (or `created_at`) timestamp is
     within `VERIFY_LOCK_TTL_SECONDS` (45 minutes) of the current UTC time is
     considered **active**. An `in-progress` comment older than 45 minutes is
     considered **stale** (e.g. from a hard-crashed runner VM) and treated as
     `failed`.
5. Based on the existing verification comments on the PR:
   - **Already active (`in-progress`) for `<finding_id>`:** If an active
     `in-progress` comment already exists for `<finding_id>` (even on another
     hunk comment of a multi-part suggestion for the same finding), the action
     MUST log a notice, set `should_run=false`, and exit `0` without starting a
     new verification run or posting a duplicate comment.
   - **Already completed (`done`) for `<finding_id>`:** If a `done` comment
     (`VERIFIED`, `DISMISSED`, or `OPEN`) already exists for `<finding_id>`, the
     action MUST react with 👀 (`eyes`) on the user's comment, post a reply in
     the thread linking to the existing verification result comment, set
     `should_run=false`, and exit `0` without re-running `cm-runner verify`.
   - **Previously `failed` (or stale `in-progress`) for `<finding_id>`:** The
     action MUST allow a new verification run to proceed.
   - **Concurrency cap reached (`active_count >= MAX_CONCURRENT_VERIFIES`):** If
     `4` or more active `in-progress` verifications are already running across
     other findings on the PR, the action MUST react with 👀 (`eyes`) on the
     user's comment, post a reply in the thread stating that the maximum of `4`
     concurrent verifications on this pull request is already in progress and to
     comment `@cm-runner verify` again once one finishes, set
     `should_run=false`, and exit `0`.

#### Scenario: Three `@cm-runner verify` comments on different findings run in parallel without collision
- **GIVEN** a pull request with CodeMender fix comments for Finding A, Finding B, and Finding C on three separate review threads
- **WHEN** a collaborator replies `@cm-runner verify` in each of the three threads in quick succession
- **THEN** all three verifications MUST run concurrently on separate runner VMs (distinct `in_reply_to_id` concurrency groups, within `MAX_CONCURRENT_VERIFIES = 4`)
- **AND** each run MUST create and `PATCH` only its own reply comment inside its respective review thread without any comment update collision.

#### Scenario: User comments `@cm-runner verify` twice while verification is running
- **GIVEN** Finding A already has an active `<!-- codemender-verify:in-progress finding:<fp> -->` comment updated 2 minutes ago
- **WHEN** a user comments `@cm-runner verify` again on Finding A's thread
- **THEN** the pre-flight step MUST detect the active `in-progress` marker, set `should_run=false`, and exit `0` without starting a second verification container.

#### Scenario: User comments `@cm-runner verify` on an already-verified finding
- **GIVEN** Finding A already has a `<!-- codemender-verify:done finding:<fp> status:DISMISSED -->` comment on the PR
- **WHEN** a collaborator comments `@cm-runner verify` on Finding A
- **THEN** the action MUST NOT run `cm-runner verify` again
- **AND** it MUST post a reply linking to the existing verification result comment.

#### Scenario: 5th concurrent verification request on the same PR is capped
- **GIVEN** 4 findings on the PR currently have active `in-progress` verification comments
- **WHEN** a collaborator comments `@cm-runner verify` on a 5th finding
- **THEN** the action MUST reply that the limit of `4` concurrent verifications is reached, set `should_run=false`, and exit `0`.

---

### REQ-0004: Immediate Acknowledgement (👀 Reaction & In-Progress Reply) {#req-0004}

- **CUJ UUID:** `cuj-gha-verify-ack`

1. As soon as pre-flight validation and concurrency checks pass, the pre-flight
   step MUST immediately:
   - Add an `eyes` (👀) reaction to the user's `@cm-runner verify` comment via
     `POST /repos/{repo}/pulls/comments/{user_comment_id}/reactions` with
     `{"content": "eyes"}` (best-effort; a reaction API failure MUST NOT abort
     verification).
   - Post a reply in the fix comment thread via
     `POST /repos/{repo}/pulls/{pr_number}/comments/{in_reply_to_id}/replies`
     containing `<!-- codemender-verify:in-progress finding:<finding_id> -->`
     and a progress message (`⏳ **Verifying finding...** CodeMender is attempting to reproduce and verify this vulnerability. This usually takes 10–20 minutes.`).
2. The pre-flight step MUST pass the created reply's `comment_id` (and the
   resolved `<finding_id>` and parent comment metadata) to the main execution
   step so the main step can update (`PATCH`) that exact reply in place once
   `cm-runner verify` finishes.

#### Scenario: User gets immediate feedback before Docker image pull
- **GIVEN** an authorized collaborator replies `@cm-runner verify` to a valid fix comment
- **WHEN** the pre-flight step completes
- **THEN** the user's comment MUST have an `eyes` reaction and the thread MUST contain the `⏳ **Verifying finding...**` reply before WIF auth, gVisor installation, or `docker pull` begins.

---

### REQ-0005: Single-Finding SARIF Input Resolution (Artifact Primary, Reconstruction Fallback) {#req-0005}

- **CUJ UUID:** `cuj-gha-verify-sarif-input`

Because on-demand verification executes in a separate workflow run from the
initial PR scan, the action MUST resolve the single-result SARIF 2.1.0 input
document required for verification using a two-tier strategy:

1. **Tier 1 (Primary — Durable Artifact Storage):**
   - The action MUST attempt to retrieve the original single-result SARIF
     document for `<finding_id>` from durable artifact storage associated with
     the pull request.
2. **Tier 2 (Fallback — Reconstruct from Parent Fix Comment & Sticky Comment):**
   - If the finding SARIF document cannot be retrieved from artifact storage
     (the artifact is missing, expired, inaccessible, or does not contain
     `<finding_id>`), the action MUST reconstruct a valid single-result SARIF
     2.1.0 document from the parent fix comment and the PR sticky summary
     comment:
     - `ruleId`: extracted from the parent fix comment header, defaulting to
       `"codemender-finding"`.
     - `level`: mapped from the severity label in the parent fix comment header
       (`error`, `warning`, `note`).
     - `message.text`: the finding title from the parent fix comment header
       (enriched with finding details from the sticky summary comment when
       available).
     - `locations[0].physicalLocation`: `artifactLocation.uri` set to the
       parent comment repo-relative `path`, and line coordinates set from the
       parent review comment.
     - `partialFingerprints.primaryLocationLineHash`: set to `<finding_id>`.

#### Scenario: Resolving SARIF from the PR artifact
- **GIVEN** a non-expired `codemender-report-pr-42` artifact containing `findings/finding-001.sarif` with `finding_id == "abc123def456"`
- **WHEN** `@cm-runner verify` runs for a fix comment tagged `<!-- codemender-finding:abc123def456 -->`
- **THEN** the action MUST extract and pass that exact single-result SARIF document to `cm-runner verify`.

#### Scenario: Falling back to comment reconstruction when the artifact has expired
- **GIVEN** the `codemender-report-pr-42` artifact has expired or cannot be downloaded
- **WHEN** `@cm-runner verify` runs for a fix comment on `src/db.py` line `25` with header `**CodeMender fix for finding 1:** 🔴 High / Critical · CWE-89 · SQL Injection`
- **THEN** the action MUST construct a valid 1-result SARIF 2.1.0 document with `ruleId: "CWE-89"`, `uri: "src/db.py"`, `startLine: 25`, and `partialFingerprints.primaryLocationLineHash` set to the finding ID, and run `cm-runner verify` with it.

---

### REQ-0006: Isolated Container Execution & Signal Handling (`cm-runner verify`) {#req-0006}

- **CUJ UUID:** `cuj-gha-verify-execution`

1. The action MUST stage an isolated temporary directory containing:
   - `workspace/`: a clean copy of `$GITHUB_WORKSPACE` (excluding
     `gha-creds-*.json`, `.cm_project`, and `.exploit`).
   - `cm-home/`: an empty directory for `~/.codemender`.
   - `out/`: a directory containing the resolved single-finding SARIF file
     (`finding.sarif`).
2. The action MUST configure the launcher's output directory via
   `launcher.with_dirs(out_dir=out_dir)` and run
   `cm-runner verify --repo /workspace --finding /out/finding.sarif --out /out`
   via the configured `Launcher` (`LocalDockerLauncher` under gVisor `runsc` for
   `codemender-local`, or `CloudRunLauncher` via `CloudRunClient` for
   `codemender`) with an outer launcher timeout exceeding `cm-runner verify`'s
   internal `_VERIFY_TIMEOUT_SEC` ceiling.
3. When executing via `CloudRunLauncher`, the launcher MUST:
   - Rewrite container path arguments (`/workspace`, `/out`, and
     `/out/finding.sarif`) to the staged host directory paths (configured via
     `with_dirs` or `CloudRunConfig`) so `CloudRunClient` uploads the
     single-finding SARIF file to GCS transit storage (`input/finding.sarif`).
   - Download the verified output SARIF (`output/finding.sarif`) from GCS
     transit storage back to `out_dir/finding.sarif` on the host runner and
     populate `RunResult.stdout` with its raw bytes so downstream result
     handling (`REQ-0007`) operates identically across both backends.
4. On `SIGINT` or `SIGTERM` (workflow cancellation or job timeout), the action
   MUST stop the running execution (`launcher.stop()`) and update (`PATCH`) the
   in-progress reply comment to `<!-- codemender-verify:failed finding:<finding_id> -->`
   with a cancellation notice so the thread is never left permanently locked in
   `in-progress`.

#### Scenario: On-demand verify executes via CloudRunLauncher
- **GIVEN** `CODEMENDER_BACKEND="cloudrun"` and a resolved single-finding SARIF file staged at `out_dir/finding.sarif`
- **WHEN** `run_verify` invokes `launcher.run("verify", ["--repo", "/workspace", "--finding", "/out/finding.sarif", "--out", "/out"])`
- **THEN** `CloudRunLauncher` MUST translate `/out/finding.sarif` to the staged host path, upload `input/finding.sarif` to GCS transit storage, execute the Cloud Run job with `CMD="verify"`, download `output/finding.sarif` back to `out_dir/finding.sarif` (and `RunResult.stdout`), and update the PR review reply in place.

#### Scenario: Cancelled job releases the in-progress lock
- **GIVEN** a running `cm-runner verify` execution and an `in-progress` reply comment on the PR
- **WHEN** the process receives `SIGTERM`
- **THEN** the action MUST stop the launcher and update the reply comment's marker to `<!-- codemender-verify:failed finding:<finding_id> -->`.

---

### REQ-0007: In-Place Verification Result Reply & Artifact Upload {#req-0007}

- **CUJ UUID:** `cuj-gha-verify-result-reply`

1. When `cm-runner verify` finishes, the action MUST inspect its exit code and
   parse the emitted SARIF 2.1.0 JSON from `RunResult.stdout` (populated from
   container `stdout` in local mode or the downloaded `output/finding.sarif`
   artifact in Cloud Run mode), reading `runs[0].results[0].properties.status`
   and `runs[0].results[0].properties.dismiss_reason`.
2. The action MUST update (`PATCH /repos/{repo}/pulls/comments/{reply_comment_id}`)
   the in-progress reply comment in place (falling back to posting a new thread
   reply if `PATCH` fails) with one of the following outcomes:
   - **`VERIFIED` (exit `0` and `status == "VERIFIED"`):**
     - Marker: `<!-- codemender-verify:done finding:<finding_id> status:VERIFIED -->`
     - Body: `🔴 **Verified (Exploitable):** CodeMender confirmed that this vulnerability is exploitable. Please review and apply the suggested fix.`
   - **`DISMISSED` (exit `0` and `status in {"DISMISSED", "FALSE_POSITIVE"}`):**
     - Marker: `<!-- codemender-verify:done finding:<finding_id> status:DISMISSED -->`
     - Body: `🟢 **Dismissed (Not Exploitable):** CodeMender demonstrated that this finding is not exploitable (` + sanitized `dismiss_reason` + `). You can disregard this fix suggestion.`
   - **`OPEN` / Inconclusive (exit `0` and any other status):**
     - Marker: `<!-- codemender-verify:done finding:<finding_id> status:OPEN -->`
     - Body: `🟡 **Inconclusive:** CodeMender could neither confirm nor refute exploitability for this finding. Please review the suggested fix manually.`
   - **`FAILED` (non-zero exit code, launcher error, timeout, or invalid SARIF output):**
     - Marker: `<!-- codemender-verify:failed finding:<finding_id> -->`
     - Body: `⚠️ **Verification failed:** CodeMender could not complete verification for this finding. You can comment \`@cm-runner verify\` to try again. See the [workflow run logs](<run_url>).`
3. Every reply MUST include a link to the workflow run logs and keep its body
   within GitHub's 65,536-character limit (`truncate_comment_body`).
4. When `cm-runner verify` produces an updated SARIF file, the action MUST save
   it under `out_dir/verified/<finding-name>.sarif` and upload it in the
   workflow's artifact upload step.
5. Consistent with ADR-0006 and SPEC-GHA-FIX REQ-0005, an inconclusive (`OPEN`)
   or `FAILED` verification outcome MUST NOT fail the GitHub Actions workflow
   job itself (exit `0` when the reply is posted).

#### Scenario: Confirmed exploitable finding (`VERIFIED`)
- **GIVEN** `cm-runner verify` exits `0` and writes a SARIF file with `properties.status = "VERIFIED"`
- **WHEN** the action processes the result
- **THEN** it MUST update the in-progress comment in place with `<!-- codemender-verify:done finding:<fp> status:VERIFIED -->` and the `🔴 **Verified (Exploitable)**` message.

#### Scenario: Refuted false-positive finding (`DISMISSED`)
- **GIVEN** `cm-runner verify` exits `0` and writes a SARIF file with `properties.status = "DISMISSED"` and `properties.dismiss_reason = "Input is sanitized as an integer"`
- **WHEN** the action processes the result
- **THEN** it MUST update the in-progress comment in place with `<!-- codemender-verify:done finding:<fp> status:DISMISSED -->`, the `🟢 **Dismissed (Not Exploitable)**` message, and the sanitized dismissal reason.

---

### REQ-0008: Security & Documentation {#req-0008}

- **CUJ UUID:** `cuj-gha-verify-security-docs`

1. `GITHUB_TOKEN` MUST NOT be passed into the `cm-runner verify` container
   (ADR-0003, ADR-0008).
2. All strings extracted from `cm-runner verify` output (including
   `dismiss_reason`) and from PR comments MUST be sanitized before rendering
   into Markdown replies.
3. `.github/SETUP.md` MUST document how to trigger on-demand
   verification (`@cm-runner verify` in a fix comment thread), who can trigger
   it, the concurrency cap (`4`), and what each verification status means.
