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

# SPEC-GITLAB-CI: GitLab CI Scanning on Cloud Run

**Implements Spec:** None

**Extends Spec:** [SPEC-GHA](../github-actions/spec.md), [SPEC-GHA-FIX](../github-actions-fix/spec.md), [SPEC-FIND-DIFF](../find-diff/spec.md)

**Governing Journeys:**

- **Merge Request Author (`persona-mr-author`)**: As a GitLab developer, I want each merge request scanned automatically, with findings in one sticky MR comment and in GitLab's Security tab, without managing Google Cloud keys.
- **Enterprise Security & Platform Lead (`persona-security-auditor`)**: As a security architect, I mandate that untrusted repository code runs only in Cloud Run microVMs, never on the GitLab Runner, and that GitLab tokens never reach those microVMs.

## Overview

`templates/codemender.yml` adds `codemender-scan` (`cm-gitlab find`) to merge request pipelines. Fixes and `verify` are in [SPEC-GITLAB-CI-FIX](../gitlab-ci-fix/spec.md); shared Cloud Run and GCS transit resources follow [SPEC-WIF REQ-0004–REQ-0005](../wif/spec.md#req-0004).

### Reused vs. New

- **Reused Unchanged:**
  - Differential scanning contract and SARIF 2.1.0 output ([SPEC-FIND-DIFF](../find-diff/spec.md)).
  - Cloud Run `cm-worker` task/manifest/receipt contract and transit cache layout ([SPEC-GHA REQ-0003, REQ-0008](../github-actions/spec.md#req-0003), [Distributed Execution & Transit](../tech/systems/distributed-execution-and-transit/design.md), [CodeMender Runner System Design](../tech/systems/codemender-runner/design.md)).
  - Host-token isolation boundary ([SPEC-GHA REQ-0006](../github-actions/spec.md#req-0006), [ADR-0003](../../../adr/ADR-0003-environment-scrubbing.md), [ADR-0008](../../../adr/ADR-0008-repository-layout-and-package-structure.md)).
  - SARIF splitting, `finding_id` fingerprinting, and sticky summary table structure/fencing ([SPEC-GHA-FIX REQ-0004, REQ-0008.2, REQ-0009.1](../github-actions-fix/spec.md#req-0004)).
  - Shared Cloud Run Job, GCS transit bucket, and Cloud Run caller IAM roles ([SPEC-WIF REQ-0004](../wif/spec.md#req-0004)).
- **Overridden from `SPEC-GHA` / `SPEC-GHA-FIX` / [CodeMender Runner System Design](../tech/systems/codemender-runner/design.md):**
  - Cloud Run-only execution (`local` backend removed) and hardened task overrides: `--cache-uri` and `--task-id` arguments plus a task-bounded timeout only, zero host env vars, no `CODEMENDER_SANDBOX_*` override, and deadline/`SIGTERM` cancellation (`exit_code=124`) (`REQ-0001`–`REQ-0004`).
  - Allowlist snapshot and runner `git` hygiene: fresh format-only `.git/config` (replacing legacy denylist credential stripping), base-commit-only reduced `.codemender.yaml`, symlink filtering, and minimal allowlisted `git` environment (`REQ-0006.2`).
  - Passing-job SARIF ingestion exit code (`0` iff `find` stages valid `report.sarif`, gated by GitLab MR approval policies rather than [ADR-0006](../../../adr/ADR-0006-pr-remediation-routing-and-security-gate.md) / [CodeMender Runner System Design](../tech/systems/codemender-runner/design.md)'s default commit status) (`REQ-0007.1`).
- **Brand New for GitLab CI:**
  - `cm-gitlab` CLI (`find`, `verify`), `docker/gitlab.Dockerfile`, and `templates/codemender.yml` (`REQ-0001`, `REQ-0004`, `REQ-0008.3`).
  - In-process WIF exchange of `GCP_OIDC_TOKEN` (`id_tokens:`) to impersonate `GCP_SERVICE_ACCOUNT` (`REQ-0005`).
  - Merged-results source-branch checkout (`CI_MERGE_REQUEST_SOURCE_BRANCH_SHA`), shallow-clone fetch, and `CI_SERVER_TLS_CA_FILE` trust (`REQ-0006.1`).
  - Sticky MR note `CODEMENDER_GITLAB_TOKEN` authorship check, `/` and `@` neutralization, and 1,000,000-char limit (`REQ-0007.2`).
  - Per-job `<CI_PROJECT_ID>/<head_sha>/<CI_JOB_ID>/` scan prefix, `latest-scan.json` pointer, and `- 5 min` compute deadline (`REQ-0008.1–2`).

## Requirements

### REQ-0001: Pipeline Configuration & Platform Mapping {#req-0001}

- **CUJ UUID:** `cuj-gitlab-backend-config`

**Overrides [SPEC-GHA REQ-0001](../github-actions/spec.md#req-0001) and [REQ-0004](../github-actions/spec.md#req-0004):** GitLab has no `local` backend (`backend`, `runner_image`, and `registry_token` are omitted) and no ambient Cloud Run defaults. `cm-gitlab` MUST validate the variables below and exit `1` naming any missing or invalid variable before starting Cloud Run:

| GitLab CI Variable | GitHub Actions Equivalent | Required / Default | Notes |
| :--- | :--- | :--- | :--- |
| `GCP_PROJECT_ID`, `GCP_WIF_PROVIDER`, `GCP_SERVICE_ACCOUNT`, `CODEMENDER_GCS_TRANSIT_BUCKET`, `CODEMENDER_CLOUD_RUN_JOB_NAME`, `CODEMENDER_GCP_REGION` | `gcp_project_id`, `gcp_workload_identity_provider`, `gcp_service_account`, `gcs_transit_bucket`, `cloud_run_job_name`, `cloud_run_region` | Yes (no defaults) | Cloud Run caller IAM and transit bucket match [SPEC-WIF REQ-0004](../wif/spec.md#req-0004); `'global'` is invalid for region. |
| `GCP_OIDC_TOKEN` | Ambient `ACTIONS_ID_TOKEN_REQUEST_*` | Required (`id_tokens:`) | Injected by `templates/codemender.yml` (REQ-0005, REQ-0008.3). |
| `CODEMENDER_GITLAB_TOKEN` | `GITHUB_TOKEN` | Optional (`''`) | Reporter-role token for MR notes ([design Decision 4](design.md#decision-4)); scan and SARIF run without it. |
| `CODEMENDER_LOG_LEVEL` | `log_level` | `'info'` | Passed to `cm-runner` via `manifest.json`. |
| `CODEMENDER_SCAN_ON_MR`, `GIT_DEPTH`, `HTTPS_PROXY` / `NO_PROXY` | Workflow `if:`, `fetch-depth`, runner proxy | `'true'`, `'1'`, unset | `'false'` skips jobs; shallow clones fetch needed commits (REQ-0006). |

### REQ-0002: Local gVisor OCI Sandbox Execution (Removed) {#req-0002}

**Overrides [SPEC-GHA REQ-0002](../github-actions/spec.md#req-0002) (Removed):** GitLab CI MUST run `cm` only on Cloud Run Jobs ([design Decision 1](design.md#decision-1)).

### REQ-0003: Cloud Run Execution & Override Hardening {#req-0003}

- **CUJ UUID:** `cuj-gitlab-cloudrun-launcher`

**Overrides [SPEC-GHA REQ-0003](../github-actions/spec.md#req-0003) and [CodeMender Runner System Design](../tech/systems/codemender-runner/design.md) Decision 2:** `cm-gitlab` MUST dispatch every task as a Cloud Run Job execution whose override passes only `--cache-uri` and `--task-id` arguments and a task-bounded timeout, with zero environment variables from the GitLab job (`log_level` travels in `manifest.json`, `CODEMENDER_SANDBOX_*` MUST NOT be set or overridden, and GitLab/OIDC/WIF credentials MUST NOT be sent to Cloud Run). All task I/O MUST stay under the scan's GCS prefix (REQ-0008.1), and `cm-gitlab` MUST cancel running executions at the compute deadline (REQ-0008.2) or on `SIGTERM`/`SIGINT`, recording them as timed out (`exit_code=124`).

#### Scenario: Dispatching tasks with argument-only overrides

- **GIVEN** valid Cloud Run settings and host GitLab/OIDC tokens
- **WHEN** `cm-gitlab` starts a Cloud Run task and later receives `SIGTERM`
- **THEN** the execution override MUST carry only `--cache-uri`, `--task-id`, and a task-bounded timeout with no environment variables, and `cm-gitlab` MUST cancel the execution (`exit_code=124`).

### REQ-0004: `cm-gitlab` Subcommands {#req-0004}

- **CUJ UUID:** `cuj-gitlab-launcher-factory`

**Overrides [SPEC-GHA REQ-0004](../github-actions/spec.md#req-0004):** `cm-gitlab` accepts no `CODEMENDER_BACKEND` variable, always dispatches to Cloud Run Jobs, and MUST provide `find` and `verify` ([SPEC-GITLAB-CI-FIX](../gitlab-ci-fix/spec.md)).

### REQ-0005: In-Process WIF Impersonation {#req-0005}

- **CUJ UUID:** `cuj-gitlab-wif-auth`

**Overrides [SPEC-GHA REQ-0006.1](../github-actions/spec.md#req-0006):** `cm-gitlab` MUST exchange `GCP_OIDC_TOKEN` via WIF in-process to impersonate the project's `GCP_SERVICE_ACCOUNT` ([SPEC-WIF REQ-0004](../wif/spec.md#req-0004)), writing `0600` credential files outside `$CI_PROJECT_DIR` and deleting them on exit. It MUST ignore any pre-existing `GOOGLE_APPLICATION_CREDENTIALS` (logging that it did), never fall back to the runner VM's identity, never log `GCP_OIDC_TOKEN`, and exit `1` before starting Cloud Run if `GCP_OIDC_TOKEN` is missing.

#### Scenario: Failing without an ID token

- **GIVEN** no `GCP_OIDC_TOKEN` and `GOOGLE_APPLICATION_CREDENTIALS` pointing to a service account key
- **WHEN** `cm-gitlab find` runs
- **THEN** it MUST exit `1` without using the key or starting Cloud Run.

### REQ-0006: Merged-Results Checkout, Allowlist Snapshot Hygiene & Custom CA {#req-0006}

- **CUJ UUID:** `cuj-gitlab-security-boundary`

**Overrides [SPEC-GHA REQ-0006](../github-actions/spec.md#req-0006) and [SPEC-GHA-FIX REQ-0002](../github-actions-fix/spec.md#req-0002) (aligned with [CodeMender Runner System Design](../tech/systems/codemender-runner/design.md) `shared/code.tar.gz`):**

1. **Merged-results checkout, diff range & custom CA:** The source head MUST be `CI_MERGE_REQUEST_SOURCE_BRANCH_SHA` when set (else `CI_COMMIT_SHA`), and the base MUST be `CI_MERGE_REQUEST_DIFF_BASE_SHA` (exiting `1` if unset, never `HEAD~1`). When GitLab Runner checks out a merged-results commit (`CI_COMMIT_SHA != head`), `cm-gitlab find` MUST check out `head` before building `shared/code.tar.gz`. With a shallow clone (`GIT_DEPTH`), it MUST fetch missing head/base commits using `CI_JOB_TOKEN` as a transient HTTP header (exiting `1` saying to raise `GIT_DEPTH` if still unresolved), and MUST honor `HTTPS_PROXY`/`NO_PROXY` and `CI_SERVER_TLS_CA_FILE` (`GIT_SSL_CAINFO`) on `git fetch` and GitLab/Google API calls.
1. **Allowlist snapshot & `git` hygiene:** `shared/code.tar.gz` MUST replace `.git/config` with a freshly written format-only config (omitting `.git/config.worktree`, `.git/FETCH_HEAD`, `.git/hooks`, `.cm_project`, `.exploit`, and `.codemender/`), keep in-repo relative symlinks as links while omitting out-of-repo symlinks (never following symlinks), and include at most the **base** commit's root `.codemender.yaml` (`codemender.yaml`, `.codemender.yml`, `codemender.yml`) reduced to `scan`, `build`, and in-repo `project_paths` (exiting `1` before uploading on multiple, symlinked, or unparseable configs, submodules, or partial clones). Host-side `git` MUST run with only `PATH`, `HOME`, `LANG=C.UTF-8`, `GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_NOSYSTEM=1`, and `GIT_CONFIG_GLOBAL=/dev/null` (plus the transient `git fetch` settings above), never passing GitLab or OIDC tokens in any subprocess environment.

#### Scenario: Scanning a merged-results pipeline with allowlist snapshot sanitization

- **GIVEN** `CI_MERGE_REQUEST_SOURCE_BRANCH_SHA="f00baa1"`, `CI_COMMIT_SHA="merged99"`, `CI_MERGE_REQUEST_DIFF_BASE_SHA="a1b2c3d"`, `CI_SERVER_TLS_CA_FILE` set, `CI_JOB_TOKEN` embedded in `.git/config`, `leak -> /creds/oidc.jwt`, and an MR-added `codemender.yml` with a `sandbox` block
- **WHEN** `cm-gitlab find` fetches missing commits and builds `shared/code.tar.gz`
- **THEN** it MUST trust `CI_SERVER_TLS_CA_FILE`, scan `f00baa1` against `a1b2c3d`, package `f00baa1`'s tree with a format-only `.git/config`, omit `leak` and the MR's `codemender.yml`, and include only `a1b2c3d`'s reduced `.codemender.yaml`.

### REQ-0007: SARIF Ingestion Exit Code & Sticky MR Note {#req-0007}

- **CUJ UUID:** `cuj-gitlab-reporting`

**Overrides [SPEC-GHA REQ-0005](../github-actions/spec.md#req-0005), [SPEC-GHA-FIX REQ-0008.3, REQ-0011](../github-actions-fix/spec.md#req-0008), [ADR-0006](../../../adr/ADR-0006-pr-remediation-routing-and-security-gate.md), and [CodeMender Runner System Design](../tech/systems/codemender-runner/design.md) (`GitLabRepoHost` status gate):**

1. **Exit code & merge gating:** Because GitLab ingests `reports: sarif:` only from passing jobs, `codemender-scan` MUST stage `report.sarif`, `report.md`, and `cm-logs/` (at most 100 regular files and 50 MiB) in `$CI_PROJECT_DIR` and exit `0` iff `find` succeeds and stages a valid `report.sarif` (non-empty `runs`), and `1` otherwise. Unlike GitHub Actions, a failed `find` MUST NOT post or update the MR note, fixes/publishing MUST NOT change the exit code, and `cm-gitlab` MUST NOT set a commit status (merges are gated via GitLab MR approval policies).
1. **Sticky MR note:** When `CODEMENDER_GITLAB_TOKEN` is set, `cm-gitlab find` MUST post or update `<!-- codemender-summary -->`, updating only a note authored by the token's own user, neutralizing `/` quick actions and `@` mentions outside code fences, and truncating at GitLab's 1,000,000-character limit on a line boundary with open fences or `<details>` closed. Note post failures MUST be logged without changing the exit code.

#### Scenario: Failing when `find` fails and ignoring spoofed notes

- **GIVEN** a human posted a note with `<!-- codemender-summary -->`
- **WHEN** `find` exits non-zero after writing `report.sarif`
- **THEN** `cm-gitlab find` MUST stage the artifacts, post no MR note, and exit `1`; when a later run succeeds with `/label ~x` and `@all` in `report.md`, it MUST post a new bot note with `/` and `@all` neutralized outside fences.

### REQ-0008: Scan Prefix, Deadlines, Job Image & Template {#req-0008}

- **CUJ UUID:** `cuj-gitlab-backend-parity-and-template`

**Overrides [SPEC-GHA REQ-0007](../github-actions/spec.md#req-0007):**

1. **Per-job prefix & pointer:** Each job MUST write under `gs://<bucket>/<CI_PROJECT_ID>/<head_sha>/<CI_JOB_ID>/` and, as soon as `find` succeeds and results are split (before fixing), write `gs://<bucket>/<CI_PROJECT_ID>/<head_sha>/latest-scan.json` naming that prefix (never writing it after a failed `find`).
1. **Compute deadline:** The compute deadline is `CI_JOB_STARTED_AT + CI_JOB_TIMEOUT - 5 minutes`; `cm-gitlab` MUST exit `1` if `< 10 minutes` remain before starting `find` or if `find` hits the deadline, and MUST cancel running Cloud Run executions at the deadline.
1. **Job image & template:** `docker/gitlab.Dockerfile` MUST provide `/usr/local/bin/cm-gitlab` and `git` in an isolated virtualenv (`python3 -I`). `templates/codemender.yml` (inputs `image`, `stage: test`, `git_depth: "1"`) MUST define `codemender-scan` pinned by digest with `entrypoint: [""]`, no `tags:`, `inherit: default: false`, `needs: []`, `dependencies: []`, `interruptible: true`, `GIT_STRATEGY: clone`, `GIT_SUBMODULE_STRATEGY: none`, `timeout: 60 minutes`, `id_tokens: GCP_OIDC_TOKEN` (`aud: https://iam.googleapis.com/${GCP_WIF_PROVIDER}`), and `report.sarif`/`report.md`/`cm-logs/` artifacts (`access: developer`, `when: always`, `expire_in: 7 days`, `reports: sarif: report.sarif`), running only in non-fork, non-merge-train MR pipelines when `CODEMENDER_SCAN_ON_MR != "false"`.

#### Scenario: Writing the pointer only after a successful scan

- **GIVEN** `find` fails in job `200` and succeeds in job `201` before job `201` is killed while fixing
- **WHEN** both jobs exit
- **THEN** `<project>/<head_sha>/latest-scan.json` MUST remain untouched after job `200` and name job `201`'s prefix after job `201`.
