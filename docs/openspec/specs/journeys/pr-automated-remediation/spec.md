<!--
sdd_spec_id: docs/openspec/specs/journeys/pr-automated-remediation/spec.md
sdd_level: JOURNEY
sdd_implements: NONE
sdd_extends: NONE
-->

# Pull Request Automated Remediation

## Overview

When a developer opens or updates a pull request (PR)—meaning any change-review
request on a supported repository host, such as a GitHub Pull Request or a
GitLab Merge Request—CodeMender scans the changes against the target base branch
to identify newly introduced or activated security findings and automatically
synthesizes candidate remediation patches in the background. The primary
personas are:

- **Pull Request Author**: Wants actionable security findings reported directly
  in the pull request in a single consolidated summary and wants fixes delivered
  next to the changed code—as a directly applicable inline suggested edit
  whenever the repository host supports it or as a complete standalone patch
  otherwise—without cluttering the review conversation across commits.
- **Security & CI Operator**: Requires best-effort fix generation that never
  cancels sibling fixes or fails the pull request's CI check when an individual
  patch cannot be synthesized.

## Requirements

### REQ-0001: In-Review Finding Notification & Remediation Status Tracking {#req-0001}

1. Upon completing a scan on a pull request, the system MUST notify developers
   and reviewers directly in the pull request with a consolidated summary of
   actionable findings, and SHOULD update that existing summary comment in place
   as fixes complete and across subsequent commits (when the repository host
   supports updating comments in place) rather than posting duplicate summary
   notifications.
2. When actionable findings are present, the consolidated summary MUST display
   each finding's severity, rule or category identifier, file and line location,
   finding explanation and details, and remediation status.
3. As automated remediation progresses, the system MUST communicate each
   finding's current remediation status to the user—and SHOULD reflect and
   update that status in place within the summary comment when the repository
   host supports it—covering the following user-observable lifecycle states:
   - **In progress / generating**: Remediation is actively running for the
     finding;
   - **Directly applicable suggestion**: A fix has been posted as an inline
     suggested edit linked from the summary;
   - **Standalone patch available**: A complete fallback patch (or link to the
     generated patch in build artifacts) has been posted and linked from the
     summary;
   - **No automated fix produced**: The remediation engine determined that no
     automated patch could be synthesized;
   - **Fix generation failed**: Fix synthesis encountered an execution error or
     exceeded its time budget;
   - **Dismissed by verification**: Subsequent on-demand verification refuted
     exploitability for the finding.

#### Scenario: Communicating remediation status updates across fix completions and subsequent commits

- **GIVEN** a pull request where a scan detects two actionable findings and
  posts the initial consolidated summary with both findings in an in-progress
  remediation state
- **WHEN** the second finding's fix completes as an inline suggested edit while
  the first finding's fix fails, and the developer later pushes another commit
- **THEN** the system MUST communicate the updated remediation status for both
  findings (showing that the first finding's fix generation failed and linking
  the second finding to its inline suggested edit), and SHOULD update the
  existing summary comment in place across fix completions and subsequent
  commits when supported by the repository host.

### REQ-0002: Direct In-Review Fix Application & Complete-Patch Fallback {#req-0002}

1. **Tier 1 — Direct In-Review Fix Application**: When the repository host
   allows creating inline suggested edits for the generated fix, the system MUST
   deliver the entire fix as a directly applicable inline suggested edit
   anchored to the affected lines. Inline suggested edit delivery MUST be atomic
   (all-or-nothing) per finding: the system MUST NOT post part of a fix as an
   inline suggested edit and omit or split the remainder.
2. **Tier 2 — Complete-Patch Fallback**: When inline suggestions are not
   supported for a fix (for example, the patch is touching files or lines
   outside the current PR scope and so inline suggestions are blocked), the
   system MUST gracefully fall back to providing the complete patch in the PR
   review (or linking to the build artifacts when the patch exceeds host display
   limits). Instructions on how to apply the patch MUST be provided.
3. **Overlapping-Line Notice**: When two posted fixes modify overlapping line
   ranges of the same file, the system MUST notify the developer on both fixes
   to apply one fix first and allow the next commit scan to re-evaluate the
   other.

#### Scenario: Fix delivered as an inline suggested edit when supported by the repository host

- **GIVEN** a generated fix for which the repository host supports creating
  inline suggested edits
- **WHEN** the system publishes the remediation for that finding
- **THEN** it MUST deliver the entire fix atomically as a directly applicable
  inline suggested edit anchored to the affected lines and communicate that the
  suggestion is available.

#### Scenario: Fix delivered via complete-patch fallback when inline suggestions are not supported for the fix

- **GIVEN** a generated fix for which inline suggestions are not supported (for
  example, the patch is touching files or lines outside the current PR scope and
  so inline suggestions are blocked)
- **WHEN** the system publishes the remediation for that finding
- **THEN** it MUST NOT post a partial inline suggested edit, MUST instead
  provide the complete patch in the PR review along with instructions on how to
  apply the patch, and MUST communicate that the standalone patch is available.

#### Scenario: Overlapping fixes warn the developer on both review threads

- **GIVEN** two findings in the same scan whose generated fixes modify
  overlapping line ranges of the same file
- **WHEN** both fixes are published to the pull request
- **THEN** both review threads MUST advise the developer to apply one fix first
  and let the next push re-evaluate the other.

### REQ-0003: Non-Blocking Remediation {#req-0003}

1. Automated fix generation is best-effort developer assistance. Inability or
   failure to generate a fix for an individual finding (including when no fix is
   found, an error occurs, or a timeout is reached) MUST NOT cancel other
   concurrent fix attempts and MUST NOT cause the pull request's CI check to
   fail, ensuring that the finding summary, completed fixes, and diagnostic
   build artifacts are always published.

#### Scenario: A failed or timed-out fix does not fail CI or abort sibling fixes

- **GIVEN** a pull request scan with three actionable findings where one fix
  attempt times out or fails while the other two produce valid fixes
- **WHEN** the remediation workflow finishes
- **THEN** the two valid fixes MUST be published to the pull request, the failed
  finding MUST be reported as failed, diagnostic artifacts MUST be published,
  and the CI check MUST succeed.

### REQ-0004: Finding Deduplication & Cross-Commit Conversation Hygiene {#req-0004}

1. Within a single scan run, identical findings targeting the same underlying
   defect MUST be remediated once and share the resulting outcome.
2. Across subsequent commits on the same pull request, the system MUST remove,
   supersede, or update outdated automated review feedback from earlier commits
   so the pull request conversation does not accumulate stale bot comments.

#### Scenario: Subsequent commits keep the pull request conversation free of stale bot feedback

- **GIVEN** a pull request where an earlier commit's scan posted automated fix
  suggestions or patches
- **WHEN** the developer pushes a new commit and a new scan runs
- **THEN** the system MUST clear, supersede, or reuse outdated automated review
  comments from the previous commit and MUST NOT leave duplicate automated fix
  comments for the same finding.
