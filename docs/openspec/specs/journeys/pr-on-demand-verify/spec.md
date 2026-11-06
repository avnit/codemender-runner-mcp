<!--
sdd_spec_id: docs/openspec/specs/journeys/pr-on-demand-verify/spec.md
sdd_level: JOURNEY
sdd_implements: NONE
sdd_extends: NONE
-->

# Pull Request On-Demand Finding Verification

## Overview

Dynamic exploitability verification executes candidate reproducers inside an
isolated sandbox, taking significantly longer than static scanning and making it
impractical to run automatically on every finding before posting pull request
fixes. Instead, developers and reviewers can request exploitability verification
on demand for specific findings directly from a pull request (PR)—meaning any
change-review request on a supported repository host, such as a GitHub Pull
Request or a GitLab Merge Request. The primary personas are:

- **Pull Request Reviewer or Author**: Wants to request on-demand exploitability
  verification directly from a finding's review thread (or via a host-supported
  verification trigger), receive immediate visible confirmation that
  verification has started, and receive a clear verdict (`Verified`,
  `Dismissed`, `Inconclusive`, or `Failed`) in the same review thread.
- **Security & Platform Lead**: Requires strict collaborator and fork
  authorization before provisioning verification compute or cloud credentials,
  per-finding deduplication, and per-pull-request concurrency bounds so
  unauthorized users or repeated requests cannot exhaust CI resources.

## Requirements

### REQ-0001: In-Review On-Demand Verification Trigger & Authorization Gate {#req-0001}

1. Authorized developers and reviewers MUST be able to trigger on-demand
   exploitability verification for a specific reported finding directly from the
   pull request review interface (for example, by replying with a verification
   command such as `@codemender verify` outside code blocks in the finding's
   review thread or invoking a host-supported verification action).
2. **Collaborator and Fork Authorization Gate**: Only users with repository
   collaborator or write permissions acting on trusted (non-fork) pull requests
   in reference to a CodeMender finding thread MAY trigger on-demand
   verification.
3. Ordinary review comments without the verification trigger, requests from
   unauthorized external users, comments authored by automated bots, replies to
   human comments unrelated to a reported finding, and requests on
   cross-repository fork pull requests MUST be ignored and rejected during fast
   pre-flight validation before provisioning verification compute, pulling
   sandbox images, or minting cloud credentials.

#### Scenario: Authorized collaborator triggers verification on a finding thread

- **GIVEN** a trusted (non-fork) pull request containing a reported CodeMender
  finding review thread
- **WHEN** an authorized repository collaborator requests on-demand verification
  (for example, by commenting `@codemender verify`) in that finding thread
- **THEN** pre-flight validation MUST authorize the request for that finding and
  proceed to acknowledge and execute verification.

#### Scenario: Unauthorized user or untrusted fork pull request is rejected before heavy compute

- **GIVEN** an external contributor without collaborator permissions requests
  verification on a pull request, OR any user requests verification on a
  cross-repository fork pull request
- **WHEN** the system evaluates the verification trigger event
- **THEN** it MUST reject the request during pre-flight validation without
  minting cloud credentials, pulling sandbox images, or starting a verification
  environment, and without failing the pull request's CI checks.

#### Scenario: Unrelated comment or non-finding thread is ignored without starting verification

- **GIVEN** a pull request with the verification workflow enabled
- **WHEN** a collaborator posts an ordinary review comment without a
  verification command, OR replies with a verification command in a general
  human discussion thread unrelated to a CodeMender finding
- **THEN** the system MUST ignore and skip the comment without starting
  verification and without failing the pull request's CI checks.

### REQ-0002: Immediate In-Review Acknowledgment {#req-0002}

1. Immediately upon passing pre-flight validation and concurrency checks—and
   before provisioning isolated verification environments, pulling container
   images, or running dynamic exploitability analysis—the system MUST post
   visible in-review feedback acknowledging the user's request and indicating
   that verification is in progress in the finding's review thread.

#### Scenario: Requester sees immediate in-progress acknowledgment before sandbox provisioning

- **GIVEN** an authorized on-demand verification request on a valid finding
  thread that passes concurrency checks
- **WHEN** pre-flight validation completes
- **THEN** the finding's review thread MUST display an immediate in-progress
  acknowledgment before cloud credential exchange, image pulling, or dynamic
  verification execution begins.

### REQ-0003: Actionable Verification Verdict, Non-Blocking Execution & Concurrency Protection {#req-0003}

1. **Verdict Reporting**: When dynamic verification finishes (or is cancelled or
   terminated), the system MUST report the final verdict in the finding's review
   thread (including a link to the diagnostic workflow run logs)—and SHOULD
   update the in-progress acknowledgment in place when the repository host
   supports updating comments in place—with one of four actionable outcomes:
   - **`Verified` (Exploitable Confirmed)**: Confirms that the finding is
     exploitable and advises the developer to review and apply the proposed fix.
   - **`Dismissed` (Not Exploitable / False Positive Refuted)**: Confirms that
     the finding is not exploitable with a sanitized explanation of why it was
     refuted, states that the proposed fix can be disregarded, and SHOULD update
     the finding's status in the pull request summary to dismissed when
     supported by the repository host.
   - **`Inconclusive` (`Open`)**: States that exploitability could neither be
     confirmed nor refuted and advises manual review of the finding and proposed
     fix.
   - **`Failed`**: States that verification could not complete (including on
     timeout or job cancellation) and informs the user that they can retry
     verification.
2. **Non-Blocking Execution Contract**: An `Inconclusive` or `Failed`
   verification outcome MUST NOT cause the pull request's CI check to fail.
3. **Per-Finding Deduplication & Pull Request Concurrency Protection**:
   - **Active verification deduplication**: While verification is actively
     running for a finding, duplicate verification requests for that same
     finding MUST be ignored without starting a redundant verification run.
   - **Completed verification reuse**: If a finding has already been verified to
     a completed verdict (`Verified`, `Dismissed`, or `Inconclusive`), a
     subsequent verification request MUST direct the user to the existing
     verdict in the review thread without re-running dynamic verification.
   - **Retry after failure or expired state**: If a prior verification attempt
     failed, was cancelled, or exceeded the active-run expiration window after
     an unexpected runner termination, the system MUST release the active state
     and allow a new verification request to proceed.
   - **Per-pull-request concurrency bound**: When the maximum number of
     concurrent active verifications on a pull request has been reached, any
     additional verification request MUST inform the user in the review thread
     that the concurrency limit is currently reached and advise them to retry
     once an active verification finishes.

#### Scenario: Refuted finding reports Dismissed verdict in the review thread and updates the summary

- **GIVEN** an active on-demand verification for a reported finding with an
  in-progress acknowledgment in the review thread
- **WHEN** dynamic verification finishes and refutes exploitability with a
  dismissal explanation
- **THEN** the system MUST report the `Dismissed` verdict in the review thread
  with the sanitized explanation and run log link (updating the in-progress
  acknowledgment in place when supported by the repository host), and SHOULD
  update the finding's status in the pull request summary to dismissed when
  supported by the repository host.

#### Scenario: Cancelled or failed verification allows retry without failing CI

- **GIVEN** an active on-demand verification with an in-progress acknowledgment
  in the review thread
- **WHEN** the verification execution fails, times out, or is cancelled
- **THEN** the system MUST report the `Failed` status in the review thread
  informing the user that verification can be retried (updating the
  acknowledgment in place when supported by the repository host), release the
  active verification state for that finding, and complete without failing the
  pull request's CI check.

#### Scenario: Duplicate, already-completed, and excess concurrent requests are bounded

- **GIVEN** a pull request where Finding A is actively being verified, Finding B
  already has a completed verification verdict, and the pull request has reached
  its maximum concurrent active verifications
- **WHEN** an authorized collaborator requests verification on Finding A,
  Finding B, or an additional unverified Finding C
- **THEN** the system MUST ignore the duplicate request on Finding A without
  starting a second run, reply on Finding B linking to the existing verdict
  without re-running verification, and reply on Finding C that the concurrent
  verification limit is reached and to retry once an active run finishes.
