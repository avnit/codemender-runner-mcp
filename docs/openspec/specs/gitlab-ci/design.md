<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-10-25' }
*-->

<!--
sdd_spec_id: SPEC-GITLAB-CI
sdd_implements: None
sdd_extends: SPEC-GHA, SPEC-GHA-FIX, SPEC-FIND-DIFF
sdd_level: COMPONENT
sdd_archetype: CI_CD_WORKFLOW
sdd_cujspec_product: codemender-runner
-->

# Design: GitLab CI Scanning on Cloud Run

## Context

`cm-gitlab` (`packages/repohost/src/repohost/gitlab/`, packaged in `docker/gitlab.Dockerfile` and invoked by `templates/codemender.yml`) is the GitLab composition root from the [CodeMender Runner System Design](../tech/systems/codemender-runner/design.md). This design extends [SPEC-GHA](../github-actions/design.md) and [SPEC-GHA-FIX](../github-actions-fix/design.md) and records only the GitLab-specific decisions driven by two platform constraints:

1. GitLab's `script:` runs inside the job container without a Docker daemon, so an on-runner sandbox requires a privileged runner.
1. GitLab Runner injects `CI_JOB_TOKEN`, CI/CD variables, and `id_tokens:` into the job environment and embeds `CI_JOB_TOKEN` in `.git/config`.

**Non-Goals:** On-runner `cm` execution ([Decision 1](#decision-1)); default-branch or scheduled scans (GitLab marks findings absent from the latest default-branch scan as resolved); merge train pipelines; and fork MRs (their ID tokens come from untrusted projects outside the WIF pool).

### Reused vs. New Components

- **Reused Unchanged:**
  - `packages/codemender` (`cm-runner`, SARIF splitting/fingerprinting) and `docker/runner.Dockerfile` (`cm-worker` Cloud Run Job container).
  - `GcsArtifactCache` and `Launcher.find()` in `packages/launcher` ([CodeMender Runner System Design](../tech/systems/codemender-runner/design.md), [SPEC-GHA](../github-actions/design.md)).
  - `terraform/modules/gcp` provisioning the shared Cloud Run Job, runtime service account, and GCS transit bucket ([SPEC-WIF](../wif/design.md)).
- **Overridden in `packages/launcher`:**
  - `CloudRunLauncher`: argument-only `ContainerOverride` (`--cache-uri`, `--task-id`, task-bounded timeout) with zero host environment variables and deadline/`SIGTERM` execution cancellation ([Decision 3](#decision-3)).
  - `Launcher.ensure_repo_snapshot()`: allowlist format-only `.git/config` (replacing legacy denylist credential stripping), base-commit-only reduced `.codemender.yaml`, symlink filtering, and minimal allowlisted `git` environment ([Decision 3](#decision-3)).
- **Brand New:**
  - `packages/repohost/src/repohost/gitlab/`: `cm-gitlab` CLI entrypoint, `GitLabRepoHost` REST adapter (MR notes, bot authorship verification, `/` and `@` neutralization), in-process WIF `external_account` ADC builder, merged-results checkout/fetch helper, and `latest-scan.json` pointer writer.
  - `docker/gitlab.Dockerfile` (slim Python + `git` job image, no `cm`) and `templates/codemender.yml` (`codemender-scan` job).

## Decisions

### Decision 1: Cloud Run Jobs as the Only Execution Backend {#decision-1}

- **Overrides:** [SPEC-GHA](../github-actions/design.md)'s dual `local` / `cloudrun` backends (`REQ-0001`, `REQ-0002`, `REQ-0004`).
- **Choice:** `cm-gitlab` always dispatches `cm` to Cloud Run Jobs.
- **Alternatives Considered:**
  - *On-runner gVisor (`local` backend):* Requires a privileged runner, custom per-task network namespaces/NAT, and host egress filtering.
  - *Runner-level gVisor (`runtime = "runsc"` / GKE Sandbox):* Isolates the host from the job, not the job's GitLab/OIDC tokens from `cm`'s post-fix build running outside `exebox` ([SPEC-GHA-FIX](../github-actions-fix/design.md) fact 12).

### Decision 2: Slim Job Image {#decision-2}

- **Overrides:** [SPEC-GHA](../github-actions/design.md) §4's host-VM composite action (`REQ-0008.3`).
- **Choice:** `docker/gitlab.Dockerfile` is a digest-pinned slim Python image containing only `git` and `codemender`, `launcher[cloudrun]`, and `repohost[gitlab]` in an isolated virtualenv (no `cm`), avoiding pulling the multi-gigabyte runner image on every MR pipeline.

### Decision 3: Merged-Results Checkout & Allowlist Snapshot/Override Hygiene {#decision-3}

- **Overrides:** [SPEC-GHA-FIX Decision 4](../github-actions-fix/design.md#decision-4)'s denylist `.git/config` stripping (`REQ-0003`, `REQ-0006`), aligned with [CodeMender Runner System Design](../tech/systems/codemender-runner/design.md) Decision 2 & Runtime Contracts.
- **Choice:** `cm-gitlab find` checks out `CI_MERGE_REQUEST_SOURCE_BRANCH_SHA` when GitLab Runner checked out a merged-results commit (fetching missing commits with `CI_JOB_TOKEN` as a transient header and trusting `CI_SERVER_TLS_CA_FILE`), delegates `shared/code.tar.gz` packaging to shared `Launcher.ensure_repo_snapshot()` (with a freshly written format-only `.git/config`, filtered symlinks, and only the **base** commit's reduced `.codemender.yaml`, omitting `.codemender/`), runs `git` with a minimal allowlisted environment, and dispatches Cloud Run tasks with `--cache-uri`, `--task-id`, and a task-bounded timeout only (zero environment variables).
- **Alternatives Considered:**
  - *Strip known-dangerous `.git/config` and `.codemender.yaml` keys:* Misses unlisted git execution hooks (such as `diff.<driver>.textconv` on `cm`'s `git diff`) or `.codemender/command_policy.yaml` overrides, and reading `.codemender.yaml` from the MR head would let an MR weaken its own scan or build settings.

### Decision 4: Reporter Bot Token & MR Approval Policies {#decision-4}

- **Overrides:** [SPEC-GHA](../github-actions/design.md)'s `GITHUB_TOKEN`, [ADR-0006](../../../adr/ADR-0006-pr-remediation-routing-and-security-gate.md), and the default `GitLabRepoHost` commit status gate in the [CodeMender Runner System Design](../tech/systems/codemender-runner/design.md) (`REQ-0007`).
- **Choice:** `cm-gitlab` posts MR notes with an optional non-Protected, Masked-and-hidden Reporter token (`CODEMENDER_GITLAB_TOKEN`), verifies note authorship on updates, and neutralizes `/` quick actions and `@` mentions.
- **Rationale:** `CI_JOB_TOKEN` cannot write MR notes and Protected variables do not reach feature-branch MR pipelines; Reporter limits any leak within the project to reading and commenting. Because Reporter cannot set commit statuses and GitLab ingests `reports: sarif:` only from passing jobs, `codemender-scan` exits `0` whenever `find` stages valid SARIF and relies on GitLab MR approval policies to gate merges.

### Decision 5: In-Process WIF Impersonation of Per-Project Service Accounts {#decision-5}

- **Overrides:** [SPEC-GHA](../github-actions/design.md)'s external `google-github-actions/auth` step and shared WIF SA (`REQ-0005`).
- **Choice:** `cm-gitlab` writes `GCP_OIDC_TOKEN` and an `external_account` ADC file (`0600`, outside `$CI_PROJECT_DIR`) impersonating the project's dedicated `GCP_SERVICE_ACCOUNT` ([SPEC-WIF REQ-0004](../wif/spec.md#req-0004)), since Cloud Run does not support federated principals directly.

### Decision 6: Per-Job Scan Prefix with Commit-Level Pointer {#decision-6}

- **Overrides:** [SPEC-GHA](../github-actions/design.md) §6's commit-level prefix (`REQ-0008.1`).
- **Choice:** Each job writes under `<CI_PROJECT_ID>/<head_sha>/<CI_JOB_ID>/` and updates `<CI_PROJECT_ID>/<head_sha>/latest-scan.json` right after `find` succeeds (before fixing), preventing retried scans from colliding with stale task outputs while letting `codemender-verify` locate the newest scan.

## Open Questions

1. **Per-project Cloud Run isolation ([SPEC-WIF REQ-0004](../wif/spec.md#req-0004)):** Onboarded GitLab projects currently share one Cloud Run Job, runtime SA, and transit bucket, so a post-fix build running outside `exebox` could access another project's transit objects until Terraform provisions a per-project Cloud Run Job/runtime SA or bucket prefix IAM condition.
