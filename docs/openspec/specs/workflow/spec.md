<!--
sdd_spec_id: docs/openspec/specs/workflow/spec.md
sdd_legacy_id: SPEC-WORKFLOW
sdd_implements: None
sdd_extends: docs/openspec/specs/find-diff/spec.md
sdd_level: COMPONENT
sdd_archetype: CI_CD_WORKFLOW
sdd_cujspec_product: codemender-runner
-->

<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-11-03' }
*-->

# SPEC-WORKFLOW: Generic CI Workflow Orchestrator & Dependency Injection Engine

**Implements Spec:** None

**Extends Spec:** [SPEC-FIND-DIFF](../find-diff/spec.md)

**Governing Journeys:**
- **Pull Request / Merge Request Contributor (`persona-pr-author`)**: As a developer submitting code changes on GitHub or GitLab, I want fast, automated, and deterministic security scanning, inline remediation suggestions, and on-demand verification directly on my review threads without platform-specific inconsistencies.
- **Security & Compliance Lead (`persona-security-auditor`)**: As an enterprise security lead, I mandate that vulnerability scans, parallel remediations, and verification runs execute under strict isolation, enforce fail-closed security gate status checks, and maintain identical orchestration guarantees regardless of whether the pipeline runs on GitHub Actions or GitLab CI.

## Overview

This specification defines the functional requirements, lifecycle state machine, error handling, cancellation semantics, and dependency injection boundaries for the CodeMender Generic CI Workflow Engine.

Historically, outer CI workflow orchestration was implemented in a monolithic script (`gha_runner.py`), tightly coupling differential vulnerability scanning, parallel fix execution, and verification to GitHub Actions runtime APIs, environment variables, and comment formatting. This specification decouples the core CI workflow into a host-agnostic orchestration engine parameterized by two core dependency injection seams:
1. **`RepoHost`**: Abstracts platform-specific PR/MR metadata discovery, comment posting, review suggestion placement, commit status updates, and CI step output writing.
2. **`Launcher`**: Abstracts container and compute execution backends (`LocalDockerLauncher`, `CloudRunLauncher`), managing workspace isolation, diff workers, and bounded parallel task dispatch.

This specification is governed by:
- [SPEC-FIND-DIFF: Native Impact-Aware Differential Vulnerability Scanning](../find-diff/spec.md)
- [SPEC-GHA-FIX: Fixes on Pull Requests (GitHub Action, M2)](../github-actions-fix/spec.md)
- [SPEC-GITLAB-CI-FIX: Fixes & Verification on Merge Requests (GitLab CI)](../gitlab-ci-fix/spec.md)
- [SPEC-VERIFY: Verify One Finding (`cm-runner verify`)](../verify/spec.md)
- [SPEC-LAUNCHER: Sandbox Container Dispatch & Execution Engine (PR #93)](https://github.com/cloud-gtm/codemender-runner/pull/93)
- [ADR-0002: Machine-Readable Output Contract](../../../adr/ADR-0002.md)
- [ADR-0003: Environment Variable Scrubbing When Running `cm`](../../../adr/ADR-0003-environment-scrubbing.md)
- [Distributed Execution and Transit System Specification](../tech/systems/distributed-execution-and-transit/spec.md)
- [ADR-0006: PR Remediation Routing and Security Gate](../../../adr/ADR-0006-pr-remediation-routing-and-security-gate.md)
- [ADR-0008: Repository Layout, Three-Package Workspace, and Runtime Architecture](../../../adr/ADR-0008-repository-layout-and-package-structure.md)
- [CodeMender Runner System Design](../tech/systems/codemender-runner/design.md) ([Specification](../tech/systems/codemender-runner/spec.md))
- [Security and Trust Zones System Specification](../tech/systems/security-and-trust-zones/spec.md) ([Design](../tech/systems/security-and-trust-zones/design.md))

---

## Requirements

### REQ-0001: Host and Launcher Independence via Dependency Injection {#req-0001}

- **CUJ UUID:** `cuj-workflow-di`

The generic `Workflow` engine MUST accept an injected `RepoHost` instance, an injected `Launcher` instance, and a validated `WorkflowConfig` upon initialization.

The `Workflow` implementation:
1. MUST NOT import platform-specific modules (such as `repohost.github` or `repohost.gitlab`).
2. MUST NOT read platform-specific environment variables directly (such as `GITHUB_*` or `CI_*`).
3. MUST interact with the repository hosting platform exclusively through the methods declared on `RepoHost`.
4. MUST interact with sandbox containers and runner processes exclusively through `Launcher` (`find`, `fix`, `verify`, `stop`).
5. MUST enforce [Security and Trust Zones](../tech/systems/security-and-trust-zones/spec.md#req-0001) trust zone boundaries: Zone 1 host tokens (`$GITHUB_TOKEN`, `$CI_JOB_TOKEN`, GitLab API tokens) MUST NOT be passed into `Launcher` methods or placed inside the staged repository workspace. Archive sanitization and Zone 2 WIF credentials are governed exclusively by [SPEC-LAUNCHER (PR #93)](https://github.com/cloud-gtm/codemender-runner/pull/93) REQ-0002 and REQ-0008.

#### Scenario: Workflow initialization with mock adapters
- **WHEN** the `Workflow` engine is instantiated with a test implementation of `RepoHost` and `Launcher`
- **THEN** it MUST execute all workflow lifecycle stages without network access or host-specific environment variables.

---

### REQ-0002: Differential Vulnerability Scanning Lifecycle (`run_scan`) {#req-0002}

- **CUJ UUID:** `cuj-workflow-scan`

The workflow MUST provide a `run_scan()` entrypoint that orchestrates differential vulnerability scanning on a proposed code change and returns a typed `WorkflowScanOutcome`:
1. It MUST resolve the change context (`base_sha`, `head_sha`, `change_id`) by invoking `RepoHost.get_change_context()`.
2. It MUST validate that `base_sha` and `head_sha` are valid hex commit hashes matching `^[0-9a-fA-F]{40,64}$`, rejecting empty strings or leading-dash values to prevent Git argument injection.
3. It MUST verify that the local repository checkout matches `head_sha`; if mismatched, inline review suggestions MUST be downgraded to non-committable file comments.
4. If `config.fail_on_findings` is enabled, it MUST immediately publish `SecurityGateState.PENDING` via `RepoHost.set_security_gate()` before container dispatch so untrappable runner crashes or OOM events fail closed.
5. It MUST dispatch differential scanning to `Launcher.find(repo_dir=config.workspace_dir, base_ref=base_sha, diff_workers=config.diff_workers)` and consume the returned `FindOutcome` (including `finding_ids`, `sarif_report`, and `scan_meta`).
6. It MUST post or update the initial summary comment on the change via `RepoHost.upsert_summary()`.
7. If the scan command fails or times out (e.g. `LauncherError` or non-zero worker exit), the workflow MUST update the security gate to `ERROR` and return a `WorkflowScanOutcome` indicating failure.

#### Scenario: Clean differential scan detects no findings
- **WHEN** `Launcher.find()` completes the scan with exit code 0 and reports an empty `finding_ids` list
- **THEN** `Workflow.run_scan()` MUST publish `SecurityGateState.SUCCESS`, upsert the clean summary comment, and return `WorkflowScanOutcome(status=ScanStatus.CLEAN)`.

#### Scenario: Differential scan command fails or times out
- **WHEN** `Launcher.find()` raises `LauncherError` or the underlying worker exits with non-zero status
- **THEN** `Workflow.run_scan()` MUST record the error, set `SecurityGateState.ERROR` via `RepoHost.set_security_gate()`, and return `WorkflowScanOutcome(status=ScanStatus.ERROR)`.

---

### REQ-0003: Deduplication and Parallel Remediation Lifecycle {#req-0003}

- **CUJ UUID:** `cuj-workflow-fix`

When differential scanning detects findings and remediation is enabled (`config.fix_enabled == True`):
1. **Scope:** Implements the Phase 1 Single-Job "Find-Then-Fix" lifecycle, with task outputs structured for Phase 2 multi-job fan-out compatibility.
2. **Bot-Only Deduplication:** The workflow MUST query `RepoHost.list_existing_comments()` and filter exclusively for comments authored by the trusted CodeMender bot identity (`is_bot_comment`). Unauthenticated or third-party comments containing fingerprint tokens MUST be ignored to prevent fix suppression spoofing.
3. **Safe Path & Identifier Validation:** The workflow MUST filter for novel finding IDs not already remediated. Sanitization of `finding_id` is governed by [SPEC-LAUNCHER (PR #93)](https://github.com/cloud-gtm/codemender-runner/pull/93) REQ-0004. Before calling `RepoHost.post_suggestion()`, all modified target file paths MUST be checked against symlink traversal (`os.path.islink` / `Path.is_symlink()`) and rejected if pointing outside the repository root.
4. **Protected File Downgrade:** If a remediation patch touches protected CI configuration files (e.g. `.github/workflows/*`, `.gitlab-ci.yml`) or contains Markdown code fence markers, it MUST NOT be posted as a committable review suggestion; it MUST be posted as a plain non-committable patch comment.
5. **Bounded Concurrency Dispatch:** For all novel actionable findings, the workflow MUST call `Launcher.fix(finding_ids=novel_ids, max_workers=config.max_parallel)` and iterate over yielded `FixOutcome` instances.
6. **Progressive Comment Updates:** As each `FixOutcome` is yielded by `Launcher.fix()`, the workflow MUST post the remediation via `RepoHost.post_suggestion()` and progressively update the sticky summary comment.
7. **Clean Exit When Already Commented:** If all detected findings have active bot comments from prior runs, the workflow MUST exit cleanly without dispatching fix tasks to `Launcher`.

#### Scenario: Deduplicating against trusted bot comments
- **WHEN** 3 findings are detected during differential scan, but 2 have identical active bot comments from a prior run
- **THEN** the workflow MUST invoke `Launcher.fix(finding_ids=[novel_id])` with exactly 1 novel finding ID, leaving the other 2 cells marked with their existing status.

#### Scenario: Ignoring spoofed third-party fingerprint comments
- **WHEN** an untrusted user posts a PR comment containing a CodeMender finding fingerprint tag
- **THEN** the workflow MUST disregard the third-party comment and dispatch the fix task for that finding.

---

### REQ-0004: On-Demand Verification Lifecycle (`run_verify`) {#req-0004}

- **CUJ UUID:** `cuj-workflow-verify`

The workflow MUST provide a `run_verify(comment_id: str, finding_id: str)` entrypoint that executes on-demand verification and returns a typed `WorkflowVerifyOutcome`:
1. **Pre-Conditions & Input Validation:** It MUST require a non-empty `comment_id` and a validated `finding_id` matching `^[a-zA-Z0-9_-]+$` (per [SPEC-LAUNCHER (PR #93)](https://github.com/cloud-gtm/codemender-runner/pull/93) REQ-0004).
2. **In-Progress Acknowledgment:** It MUST post an immediate in-progress acknowledgment reply to the initiating comment thread via `RepoHost.reply_to_comment(comment_id, ...)`.
3. **Execution Delegation:** It MUST invoke `Launcher.verify(finding_ids=[finding_id], max_workers=1)` and consume the yielded `VerifyOutcome`.
4. **SPEC-VERIFY Semantic Alignment:** Verification outcomes MUST adhere to `SPEC-VERIFY` and `SPEC-LAUNCHER` REQ-0005:
   - **Remediated (`DISMISSED`)**: Finding is no longer exploitable on the head commit. The workflow MUST post a success confirmation reply and emit `verified=true`.
   - **Unremediated (`VERIFIED`)**: Finding remains verified and exploitable. The workflow MUST post an unremediated notification reply and emit `verified=false`.
   - **Inconclusive (`OPEN`)**: Verification could not decisively reproduce or dismiss the exploit. The workflow MUST post an inconclusive notification reply and emit `verified=false`.
5. **Thread-Scoped Lock Release:** Verification execution MUST NOT mutate the commit-level `SecurityGate` status. In the event of cancellation or error, the workflow MUST reply to `comment_id` to release the thread's active verification state.

#### Scenario: Verification demonstrates finding is remediated (DISMISSED)
- **WHEN** `Launcher.verify()` yields a `VerifyOutcome` with status `DISMISSED`
- **THEN** the workflow MUST post a success reply to `comment_id`, emit `verified=true` via `RepoHost.emit_ci_outputs()`, and return `WorkflowVerifyOutcome(status=VerifyStatus.REMEDIATED, verified=True)`.

#### Scenario: Verification confirms finding remains exploitable (VERIFIED)
- **WHEN** `Launcher.verify()` yields a `VerifyOutcome` with status `VERIFIED`
- **THEN** the workflow MUST post an unremediated reply to `comment_id`, emit `verified=false`, and return `WorkflowVerifyOutcome(status=VerifyStatus.EXPLOITABLE, verified=False)`.

---

### REQ-0005: Deterministic Security Gate Evaluation & Exit-Code Mapping {#req-0005}

- **CUJ UUID:** `cuj-workflow-security-gate`

When `config.fail_on_findings` is enabled, the workflow MUST publish a `SecurityGate` status on the change context via `RepoHost.set_security_gate()`. The relationship between scan outcomes, security gate states, and CLI exit codes MUST adhere to the following contract:

| Scan Outcome | Findings State | Security Gate State | Security Gate Description | CLI Exit Code |
| :--- | :--- | :--- | :--- | :--- |
| **Clean Scan** | 0 actionable findings | `SUCCESS` | `CodeMender: No security findings detected` | `0` |
| **Remediated Scan** | $\ge 1$ findings, all fixes posted | `FAILURE` | `CodeMender: N security finding(s) detected (remediations suggested)` | `0` |
| **Unremediated Scan**| $\ge 1$ findings, fixes disabled/failed | `FAILURE` | `CodeMender: N security finding(s) detected` | `0` |
| **Execution Error** | Scan command failed or crashed | `ERROR` | `CodeMender: Scan execution failed: <error>` | `1` (or `3`) |
| **Interrupted / Cancel**| Cancelled by signal or timeout | `ERROR` | `CodeMender: Scan interrupted or cancelled` | `1` |

When `config.fail_on_findings == False`, the security gate check MUST be skipped and not posted to `RepoHost`.

#### Scenario: Actionable findings present with suggested fixes posted
- **WHEN** differential scan detects 2 vulnerabilities and fixes are successfully posted to review comments
- **THEN** `RepoHost.set_security_gate()` MUST be set to `FAILURE` (retaining branch protection requirements) while the CLI exits `0` to allow the CI pipeline to complete review presentation.

---

### REQ-0006: Signal Trapping, Cancellation & Resource Cleanup {#req-0006}

- **CUJ UUID:** `cuj-workflow-cancellation`

1. **Process Boundary:** Process signal traps (`SIGINT`, `SIGTERM`) MUST be registered at the CLI composition root (`main()`) rather than inside library classes, ensuring thread safety and non-invasive embedding.
2. **Cancellation Hook:** `Workflow` MUST provide a `cancel()` method that invokes `Launcher.stop()` to terminate active containers, marks pending tasks as `FAILED` in the summary table, and updates `SecurityGate` to `ERROR`.
3. **Guaranteed Cleanup:** Ephemeral directories and container mounts MUST be cleaned up via `finally` blocks in `main()`, ensuring no dangling resources even during unhandled exceptions.

#### Scenario: Graceful cancellation during parallel fixes
- **WHEN** the CLI process receives `SIGTERM` while parallel fix tasks are running
- **THEN** the composition root MUST call `workflow.cancel()`, update the summary table marking in-flight tasks failed, set `SecurityGateState.ERROR`, and clean up scratch storage.

---

### REQ-0007: Standardized Review Presentation & Content Sanitization {#req-0007}

- **CUJ UUID:** `cuj-workflow-presentation`

All review summaries, comments, and replies posted to `RepoHost` MUST adhere to a portable, injection-safe Markdown presentation contract:
1. **Markup Sanitization:** Pipe characters (`|`) inside finding titles or paths MUST be escaped (`\|`) to prevent Markdown table formatting breaks.
2. **Command & Tag Injection Defense:** Leading slashes (`/`) and at-signs (`@`) in rule messages MUST be escaped (`\/`, `\@`) to prevent inadvertent GitLab quick action execution or unwanted user mentions.
3. **Dynamic Code Fence Lengths:** Diffs embedded in Markdown MUST use dynamic code fence delimiters with a backtick count greater than any backtick sequence in the diff content (minimum 4 backticks: ```` ````).
4. **Length Bounds:** Finding titles MUST be truncated to 120 characters and descriptions to 1,000 characters before table rendering.

#### Scenario: Escaping table injection characters
- **WHEN** a SARIF finding message contains `foo | bar` and begins with `/close`
- **THEN** the rendered Markdown table cell MUST render `foo \| bar` and `\/close`, preserving table structure and preventing quick actions.

---

### REQ-0008: Preflight Trigger Evaluation (`run_preflight`) {#req-0008}

- **CUJ UUID:** `cuj-workflow-preflight`

The workflow MUST provide a lightweight `run_preflight(event: TriggerEvent) -> PreflightOutcome` entrypoint for review comment webhooks:
1. **Actor Authorization:** The actor MUST be verified as an authorized collaborator (`OWNER`, `MEMBER`, `COLLABORATOR` on GitHub; `Reporter`, `Developer`, `Maintainer` on GitLab). Requests from untrusted actors MUST be rejected.
2. **Fork Guard:** Requests originating from untrusted forks MUST be rejected unless fork evaluation is explicitly configured.
3. **Bot Guard:** Comments authored by bot identities MUST be rejected to prevent recursive trigger loops.
4. **Concurrency & Lock Guard:** The workflow MUST verify that no active verify runs are already in progress on the change thread before emitting `should_run=True`.
5. **Output Injection Defense:** Output key names emitted via `RepoHost.emit_ci_outputs()` MUST match `^[a-zA-Z0-9_]+$` and values MUST NOT contain unescaped newline characters.

#### Scenario: Unauthorized user attempts to trigger verify
- **WHEN** an external contributor with author association `NONE` comments `@cm-runner verify`
- **THEN** `run_preflight` MUST return `PreflightOutcome(should_run=False, reason="Unauthorized actor")` and emit `run_verify=false`.
