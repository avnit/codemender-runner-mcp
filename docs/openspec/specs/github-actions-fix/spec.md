<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-10-31' }
*-->

<!--
sdd_spec_id: SPEC-GHA-FIX
sdd_implements: None
sdd_extends: SPEC-GHA
sdd_level: COMPONENT
sdd_archetype: CI_CD_WORKFLOW
sdd_cujspec_product: codemender-runner
-->

# SPEC-GHA-FIX: Fixes on Pull Requests (GitHub Action, M2)

**Implements Spec:** None

**Extends Spec:** [SPEC-GHA](../github-actions/spec.md)

**Governing Journeys:**
- **Pull Request Contributor (`persona-pr-author`)**: As a developer, I want CodeMender's fix for each finding next to the code it changes, as a one-click suggestion when possible, without the PR conversation filling up.
- **Security & Compliance Lead (`persona-security-auditor`)**: As a security lead, I need the agent to run isolated, the PR's GitHub token kept out of every container, and nothing committed until a person commits it.

## Overview

After `find-diff` (SPEC-GHA, M1), the action runs `cm-runner fix`
([SPEC-FIX](../fix/spec.md)) once per finding. All fixes run in parallel.
The action posts each patch on the PR next to the code it changes, and adds
a Fix column to the sticky comment.

The reasons behind each requirement are in [design.md](design.md).

This specification is governed by:
- [ADR-0002](../../../adr/ADR-0002.md): stdout carries each command's result in its standard format.
- [ADR-0008](../../../adr/ADR-0008-repository-layout-and-package-structure.md): the GitHub token stays on the host (`packages/repohost`).
- [ADR-0003](../../../adr/ADR-0003-environment-scrubbing.md): containers get only explicit environment variables.
- [ADR-0006](../../../adr/ADR-0006-pr-remediation-routing-and-security-gate.md): remediation routing (inline suggestions and collapsible patch comment fallback).

---

## Requirements

### REQ-0001: Action Inputs (`fix`, `max_parallel_fixes`) {#req-0001}

- **CUJ UUID:** `cuj-gha-fix-toggle`

1. Both `.github/actions/codemender/action.yml` and `.github/actions/codemender-local/action.yml` MUST define two optional inputs:
   - `fix`: default `'true'`.
   - `max_parallel_fixes`: default `'4'`.
2. For `fix`, the action MUST treat `false`, `0` and `no` as off, ignoring case
   and surrounding spaces. Every other value, including an empty one, MUST mean
   on.
3. With fixes off, the action MUST behave as in M1: only scanning and posting
   the sticky comment without the Fix column.
4. For `max_parallel_fixes`, the action MUST accept a positive integer (`>= 1`)
   to bound the number of concurrent fix workers. If unset,
   empty, or invalid (`<= 0` or non-integer), it MUST fall back to `4` (emitting
   a `::warning::` when an explicit non-empty value is invalid) so a
   misconfigured workflow still runs safely with bounded concurrency.
5. The workflow template (`.github/workflows/cm-merge-scan.yml`) MUST show `# fix: false` and `# max_parallel_fixes: '4'`
   as commented-out examples in the action's `with:` block.

#### Scenario: Fixes run by default
- **GIVEN** a workflow that doesn't set `fix`
- **WHEN** `find-diff` reports findings
- **THEN** the action MUST run `fix` for them.

#### Scenario: Turning fixes off
- **GIVEN** a workflow with `fix: false`
- **WHEN** the action runs
- **THEN** it MUST NOT run `fix`
- **AND** the sticky comment MUST be identical to M1's.

---

### REQ-0002: Check Out the PR Head {#req-0002}

- **CUJ UUID:** `cuj-gha-fix-head-checkout`

The workflow template's `actions/checkout` step MUST set
`ref: ${{ github.event.pull_request.head.sha }}` and keep `fetch-depth: 0` and
`persist-credentials: false`.

#### Scenario: Line numbers match the PR
- **GIVEN** a PR whose base branch gained 10 lines at the top of `app.py` after the PR branched
- **WHEN** `find-diff` reports a finding at line 50 of the PR's version of `app.py`
- **THEN** the finding, its fix and its suggestion MUST all refer to line 50 of the PR head.

---

### REQ-0003: Launcher Raw Output, Parallel Execution and Clean-Up {#req-0003}

- **CUJ UUID:** `cuj-gha-fix-launcher`

This extends SPEC-GHA REQ-0003 and REQ-0008 for parallel isolated execution and
raw stdout capture.

1. The launcher MUST capture container `stdout` and `stderr` as raw, unmodified
   bytes without text decoding or command-specific parsing, so binary patches
   and Windows line endings (`\r\n`) survive intact.
2. The launcher MUST support both single-command execution and bounded-parallel
   task execution with optional per-task log redirection.
3. When per-task log redirection is enabled (used during parallel execution so
   concurrent container logs do not interleave in the GitHub Actions job log),
   the container's operational output (`stderr`) MUST be written to that task's
   log file instead of streamed live to the job log.
4. When a container exceeds its execution timeout, the launcher MUST forcefully
   terminate that container (`docker kill`) before failing the task so timed-out
   containers do not continue consuming runner resources in the background.
5. The launcher MUST support terminating all currently running containers it
   started (including containers spawned during parallel execution) and MUST be
   safe to invoke from a signal handler or another thread during cancellation.
6. Each parallel container execution MUST mount its own isolated workspace, `cm`
   home and output directories so concurrent runs cannot mutate shared working
   trees or `cm` session state, while sharing the parent launcher's image,
   configuration and active-container lifecycle tracking.
7. For `fix`, the action MUST read the generated patch from the `--out` artifact
   file rather than `stdout`. Reading from `--out` guarantees that any
   unexpected wrapper or runtime output emitted on `stdout` cannot corrupt the
   patch, and reuses the exact diff file materialized on disk for workflow
   artifact upload (REQ-0011).

#### Scenario: Windows line endings survive
- **GIVEN** a container that writes `a\r\nb\r\n` to stdout
- **WHEN** it runs via the launcher
- **THEN** the captured `stdout` MUST equal `b"a\r\nb\r\n"`.

#### Scenario: A diff on stdout doesn't raise
- **GIVEN** a container that writes a unified diff to stdout
- **WHEN** it runs via the launcher
- **THEN** the launcher MUST NOT fail trying to parse `stdout` as JSON.

#### Scenario: A timed-out container is killed
- **GIVEN** a container that runs past the launcher's timeout
- **WHEN** the timeout fires
- **THEN** the launcher MUST run `docker kill` on that container
- **AND** report a launcher timeout error.

---

### REQ-0004: One SARIF File per Finding {#req-0004}

- **CUJ UUID:** `cuj-gha-fix-split`

Because `cm-runner fix` ([SPEC-FIX REQ-0002](../fix/spec.md#req-0002)) requires
a SARIF input containing **exactly one finding** per invocation so each finding
can be remediated independently in parallel, and because `cm report import`
resolves each result's `ruleIndex` against the run's `tool.driver.rules` array,
the action MUST materialize a standalone, valid single-result SARIF document for
each result in `find-diff`'s report. Each per-finding SARIF document MUST
preserve the full run metadata (`tool`, `rules`, `artifacts` and every other
run-level field) with `results` replaced by that single result, written to
`out_dir/findings/finding-NNN.sarif` (where `NNN` is the result's 1-based
position, zero-padded to at least three digits).

#### Scenario: Rule references stay valid
- **GIVEN** a report with three results, the third using `ruleIndex: 2`
- **WHEN** the action splits it into per-finding SARIF files
- **THEN** it MUST write `finding-001.sarif`, `finding-002.sarif` and `finding-003.sarif`, each with exactly one result
- **AND** `finding-003.sarif` MUST still contain all of the run's rules, so `ruleIndex: 2` points at the same rule.

---

### REQ-0005: Isolated, Bounded-Parallel Fix Runs {#req-0005}

- **CUJ UUID:** `cuj-gha-fix-parallel`

1. The action MUST run one container per finding to fix using a bounded worker
   pool of size `min(len(findings), max_parallel_fixes)` (default `4`).
2. Each fix task MUST run in its own isolated temporary directory on the runner
   containing:
   - `workspace/`: a clean copy of `$GITHUB_WORKSPACE` without
     `gha-creds-*.json`, `.cm_project` and `.exploit`. Because `cm fix` resets
     and mutates the working tree and `.cm_project` holds single-session state,
     it MUST be copied from the original checkout (never from the scan's copy)
     only when the worker starts.
   - `cm-home/`: an empty directory so concurrent `cm report import` calls do
     not collide.
   - `out/`: a directory containing only that finding's SARIF file.
3. Each container MUST run
   `fix --repo /workspace --finding /out/finding-NNN.sarif --out /out` with
   its own isolated log file.
4. Each fix container MUST enforce a bounded execution timeout (exceeding
   `cm fix`'s internal timeout so a hung container cannot stall the job
   indefinitely), and the workflow template MUST set a job-level timeout ceiling
   (see [design.md, Decision 6](design.md#decision-6)).
5. The action MUST classify each run as:
   - `fixed`: exit code 0 and a non-empty `out/finding-NNN.diff`
   - `no_fix`: exit code 3
   - `failed`: anything else, including exit code 0 without a patch, exit
     code 2, a launcher error or a timeout
6. Fix generation is best-effort assistance: a failed fix MUST NOT stop the
   other fixes in progress, and `no_fix` or `failed` outcomes MUST NOT fail the
   GitHub Actions job (from the user's perspective, the workflow job still
   completes with a green passing status while the sticky PR comment's `Fix`
   column reports `❌ No fix` or `⚠️ Failed` for that finding).
7. Only the main thread MUST call GitHub, and it MUST handle results in the
   order the fixes finish.
8. On SIGTERM, the action MUST stop the fix containers still running.
9. When a fix ends, the action MUST print its log inside one `::group::` named
   after the finding and the result. A `failed` result MUST also produce a
   `::warning::` line.
10. The action MUST copy each patch to `out_dir/fixes/finding-NNN.diff`. It
    SHOULD then delete that fix's temporary directory before moving on to
    conserve runner disk space.

#### Scenario: Fixes run in parallel up to `max_parallel_fixes`
- **GIVEN** three findings to fix and `max_parallel_fixes: 4`
- **WHEN** the action starts fixing
- **THEN** all three containers MUST have started before any of them finishes.

#### Scenario: One failure doesn't stop the others
- **GIVEN** three fixes, where the second fails with a launcher error
- **WHEN** the others finish with patches
- **THEN** the action MUST post the first and third patches
- **AND** the second finding's Fix cell MUST say `⚠️ Failed`
- **AND** the GitHub Actions job MUST pass (exit code `0`).

---

### REQ-0006: The Sticky Comment's Fix Column {#req-0006}

- **CUJ UUID:** `cuj-gha-fix-sticky`

With fixes on, the sticky comment MUST:

1. be posted right after `find-diff`, with the status line
   `⏳ Generating fixes for N findings. This usually takes 10 minutes or more.`
   (`1 finding` when there is one);
2. have a Fix column holding exactly one of: `⏳ Working`, `✅ Suggested`
   linking to the review, `📎 Patch` linking to the patch comment or to the run,
   `❌ No fix` or `⚠️ Failed`;
3. be updated each time a fix finishes, and once more when all have finished,
   when the status line MUST be replaced by a summary of the results;
4. contain no patches. M1's *Finding details* drill-down MUST stay as it is.

#### Scenario: Results arrive out of order
- **GIVEN** two findings whose fixes finish in reverse order
- **WHEN** both have finished
- **THEN** each row MUST show its own finding's result
- **AND** no row MUST still say `⏳ Working`.

---

### REQ-0007: One-Click Suggestions {#req-0007}

- **CUJ UUID:** `cuj-gha-fix-suggestion`

1. The action MUST post a `fixed` patch as one review per finding, with
   `event: COMMENT`, an empty review `body` (`""`), `commit_id` set to the PR
   head SHA and one ` ```suggestion ` comment per contiguous change, but only if
   all of these hold:
   - every file in the patch is modified, not added, deleted, renamed or binary;
   - every line the patch changes is inside the new side of the PR diff's
     hunks;
   - no file in the patch is under `.github/`.
2. Otherwise, or if GitHub rejects the review with HTTP 422, the finding MUST
   get a patch comment (REQ-0008). The action MUST NOT post part of a patch as
   suggestions.
3. Each suggestion MUST state the finding's severity, rule and title, `part i of
   n` when there are several, and that *Add suggestion to batch* commits them
   all at once.

#### Scenario: A fix inside the PR's changes
- **GIVEN** a patch that changes only lines the PR added
- **WHEN** the action posts it
- **THEN** it MUST post one review with suggestions, an empty review `body` (`""`), and `commit_id` equal to the PR head SHA.

#### Scenario: A fix that adds an import at the top of the file
- **GIVEN** a patch that also changes line 3, which the PR didn't touch
- **WHEN** the action posts it
- **THEN** it MUST post a patch comment instead of suggestions.

---

### REQ-0008: Patch Comments {#req-0008}

- **CUJ UUID:** `cuj-gha-fix-patch-comment`

1. A patch comment MUST be a review comment on the finding's flagged line at the
   PR head. Its body MUST give the reason in one sentence, then
   `<details><summary>Show the fix (F files, +A −D)</summary>` with the patch
   in a fenced `diff` block and the steps to apply it with `git apply`.
2. The fence MUST be one backtick longer than the longest run of backticks in
   the patch, and at least three.
3. The body MUST fit GitHub's comment limit (65,536 characters). A longer patch
   MUST be cut, with a link to the run's artifacts.
4. If GitHub rejects the line comment with HTTP 422, the action MUST post the
   same body as a file-level comment (`subject_type: file`). If GitHub rejects
   that too, the action MUST NOT post, and the Fix cell MUST link to the run's
   artifacts.

#### Scenario: A patch can't break out of its fence
- **GIVEN** a patch whose text contains a run of five backticks
- **WHEN** the action posts it
- **THEN** the fence MUST be six backticks long.

#### Scenario: Falling back to a file-level comment
- **GIVEN** a flagged line that isn't in the PR diff
- **WHEN** GitHub rejects the line comment with HTTP 422
- **THEN** the action MUST post a file-level comment on that file.

---

### REQ-0009: Fresh PR Review Comments per Run and Intra-Run Deduplication {#req-0009}

- **CUJ UUID:** `cuj-gha-fix-dedup`

1. To correlate findings for intra-run deduplication and on-demand verification
   (`@cm-runner verify`, [SPEC-GHA-VERIFY](../github-actions-verify/spec.md)),
   every comment CodeMender posts with a fix MUST contain a deterministic
   finding marker `<!-- codemender-finding:<finding_id> -->`, where
   `<finding_id>` is derived from the SARIF result's fingerprint metadata
   (`fingerprints` or `partialFingerprints`, falling back to a deterministic
   hash of the file path, rule ID, start line and message when no SARIF
   fingerprint is present).
2. On every PR scan run (`run_pr`), the action MUST list the PR's review
   comments and delete all existing bot-authored CodeMender review comments
   (`DELETE /repos/{repo}/pulls/comments/{comment_id}` for comments carrying a
   `<!-- codemender-finding:... -->` or `<!-- codemender-verify:... -->` marker
   or replying to a CodeMender finding comment), even when `find-diff` reports
   `0` findings or `fix: false`, while updating the sticky summary comment in
   place (`<!-- codemender-summary -->`).
3. Every finding reported by `find-diff` in the current run MUST be fixed fresh
   (never skipped because an earlier run posted a comment).
4. Findings with the same `finding_id` within a single run MUST be fixed once
   and share the result.

#### Scenario: A second push deletes earlier CodeMender review comments and fixes fresh
- **GIVEN** a PR where an earlier run posted a suggestion or patch comment for a finding
- **AND** the developer pushes a new commit to the PR
- **WHEN** the workflow runs again and `find-diff` reports findings
- **THEN** the action MUST delete the earlier bot-authored CodeMender review comments on the PR
- **AND** it MUST run `fix` and post fresh comments for the current run's findings.

#### Scenario: A clean re-run wipes earlier CodeMender review comments
- **GIVEN** a PR where an earlier run posted CodeMender review comments
- **WHEN** a subsequent PR scan run reports `0` findings (or runs with `fix: false`)
- **THEN** the action MUST still delete all earlier bot-authored CodeMender review comments on the PR
- **AND** update the sticky summary comment in place.

---

### REQ-0010: Fixes That Change the Same Lines {#req-0010}

- **CUJ UUID:** `cuj-gha-fix-overlap`

1. If two posted patches change overlapping lines of the same file, both
   comments MUST say: "This fix changes the same lines as the fix for #N.
   Commit one; CodeMender checks the other again on the next push."
2. Every fix comment SHOULD say that each fix was made on its own.

---

### REQ-0011: Artifacts {#req-0011}

- **CUJ UUID:** `cuj-gha-fix-artifacts`

The action's artifact upload MUST include `findings/*.sarif` and `fixes/*.diff`
along with M1's reports.

---

### REQ-0012: Documentation {#req-0012}

- **CUJ UUID:** `cuj-gha-fix-docs`

The action's README MUST say, in plain language:

1. that fixes are on by default, roughly how long they take, and how to turn
   them off (`fix: false`) or tune concurrency (`max_parallel_fixes`);
2. how builds and dependencies work: the Universal Runner Image pre-installs
   standard toolchains, customers can pass a Bring-Your-Own-Image (BYOI) built
   `FROM codemender-runner` with their project's dependencies pre-installed (so
   they don't need to open the sandbox network or pre-install dependencies on
   the GHA host), or override the command via `.codemender.yaml`;
3. that cm-runner accepts `cm`'s responsibility warning on the client's behalf
   (`--bypass-warning`), what the warning says, and that nothing is committed
   until a person commits it;
4. what each value in the Fix column means.

---

### REQ-0013: Security {#req-0013}

- **CUJ UUID:** `cuj-gha-fix-security`

1. `GITHUB_TOKEN` MUST NOT be passed into any container.
2. Containers MUST receive only explicit, allowlisted `-e KEY=VALUE` variables
   (ADR-0003), and MUST NOT run with `--privileged`.
3. The action MUST treat patches and all `cm` output as untrusted: escape them
   in table cells, fence them as in REQ-0008, and keep comment bodies within
   GitHub's limit.
4. The action MUST NOT run project code on the host.

#### Scenario: The GitHub token stays on the host
- **GIVEN** `GITHUB_TOKEN` is set in the action's environment
- **WHEN** the action starts fix containers
- **THEN** no `docker run` argv MUST contain the token or a `GITHUB_TOKEN` variable.
