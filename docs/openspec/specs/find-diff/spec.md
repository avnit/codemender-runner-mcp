<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-09-30' }
*-->

<!--
sdd_spec_id: SPEC-FIND-DIFF
sdd_implements: None
sdd_extends: None
sdd_level: COMPONENT
sdd_archetype: CLI_TOOL_BATCH_JOB
sdd_cujspec_product: codemender-runner
-->

# SPEC-FIND-DIFF: Differential Vulnerability Scanning (`cm-runner find-diff`)

**Implements Spec:** None

**Extends Spec:** None

## Overview

This specification defines the functional requirements, command-line interface,
SARIF `stdout` output contract, and `--out` artifact contract for the
`cm-runner find-diff` subcommand
(`packages/codemender/src/codemender/runner/commands/find_diff.py`).

`cm-runner find-diff` provides a streamlined CLI command built on CodeMender's
native impact-aware differential scanner
(`cm find <repo> -y --bypass-warning --diff=<base> --diff-workers=<N> --fail-on NONE`) followed
by report extraction (`cm report --format sarif`, and `cm report --format md`
when `--out` is specified). It relies on `cm`'s built-in defaults for diff
filtering and impact resolution while configuring `--diff-workers` (default
`8`, configurable from `1` to `16`) for parallelization, leaves the `cm` sandbox
intact, writes a single compact (non-pretty-printed)
[OASIS SARIF v2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html)
JSON document to `stdout`, and optionally writes `report.sarif` and `report.md`
to a caller-specified `--out` directory.

This specification is governed by and references:

- [CodeMender Find Modes (`cm find`, `--diff`, `--deep`)](https://docs.cloud.google.com/gemini-enterprise-agent-platform/agents/codemender/find-modes)
  — Official Google Cloud documentation for `cm find` scanning modes and flags.
- [Scaling CodeMender Scan Quality V2 (`go/codemender-rfc-30`)](https://docs.google.com/document/d/1_bB1A-dkDjaILxjohu7rKAndvOSWbR6dHcTghCS6NZ0/edit?tab=t.0#heading=h.rfwep4caowad)
  — Tier 0 heuristic non-production file pruning and Tier 1 impact-aware pull
  request delta scanning (`cm find --diff`).
- [ADR-0002: Machine-Readable Output Contract (`stdout` JSON, `stderr` Logs, Coarse Exit Codes)](../../../adr/ADR-0002.md)
- [ADR-0008: Repository Layout, Three-Package Workspace, and Runtime Architecture](../../../adr/ADR-0008-repository-layout-and-package-structure.md)
- [ADR-0003: Environment Variable Scrubbing When Running `cm`](../../../adr/ADR-0003-environment-scrubbing.md)

## Requirements

### REQ-0001: CLI Subcommand Registration & Minimal Argument Surface {#req-0001}

- **CUJ UUID:** cuj-find-diff-cli-args

The orchestrator CLI (`packages/codemender/src/codemender/runner/__main__.py`)
MUST register the `find-diff` subcommand and dispatch it to
`packages/codemender/src/codemender/runner/commands/find_diff.py` via
`find_diff.run(cfg, adapter, emitter)`.

To keep the CLI surface lean and rely on `cm`'s built-in defaults (and
repository settings written to `~/.codemender/config.yaml` by `cm-runner init`),
`find-diff` MUST accept only the following command-specific flags:

- `--repo PATH`: Path to the target Git repository to scan (default: `.`).
- `--base REF`: Target Git reference (branch, tag, or commit SHA, such as
  `origin/main`) to diff against (default: `HEAD`).
- `--out DIR`: Optional directory path where report artifacts (`report.sarif`
  and `report.md`) are written.
- `--diff-workers N`: Optional number of concurrent workers for `cm find --diff`
  (default: `8`, valid range: `1..16`).

All user-supplied filesystem paths (`--repo` and `--out`) MUST be normalized via
`Path(...).expanduser().resolve()` so user home directory prefixes (`~`),
relative segments (`..`), and symlinks are deterministically resolved.

#### Scenario: Default Differential Scan Invocation

- **GIVEN** a valid Git repository in the current working directory
- **WHEN** `cm-runner find-diff` is invoked with no optional flags
- **THEN** the command resolves `--repo` to the current working directory,
  targets `--base HEAD`, leaves `--out` unset (`None`), and defaults
  `--diff-workers` to `8`.

#### Scenario: Tilde Expansion on Repository and Output Paths

- **GIVEN** a caller invokes `cm-runner find-diff --repo ~/project --out ~/out`
- **WHEN** `FindDiffConfig` resolves the arguments
- **THEN** both `repo` and `out` are expanded with `.expanduser().resolve()` to
  absolute paths under the user's home directory rather than treated as literal
  relative `~` directories.

______________________________________________________________________

### REQ-0002: Input Validation & Pre-Flight Checks {#req-0002}

- **CUJ UUID:** cuj-find-diff-input-validation

The `find-diff` command MUST validate all CLI options, target paths, and the
target Git reference prior to invoking `cm find`:

1. **Command-Line Argument Validation (`FindDiffConfig.validate()`, Exit Code
   `2`):**
   - If `--repo` does not exist or is not a directory (matching
     `InitConfig.validate()` in `codemender/runner/commands/init.py`),
     `validate()` MUST log the reason to `stderr`, write nothing to `stdout`,
     and return exit code `2` (`exit_codes.USAGE`).
   - If `--base` is empty or contains only whitespace, `validate()` MUST log the
     reason to `stderr`, write nothing to `stdout`, and return exit code `2`
     (`exit_codes.USAGE`).
   - If `--out` points to an existing regular file rather than a directory,
     `validate()` MUST log the reason to `stderr`, write nothing to `stdout`,
     and return exit code `2` (`exit_codes.USAGE`).
   - If `--diff-workers` is not an integer between `1` and `16`, `validate()`
     MUST log the reason to `stderr`, write nothing to `stdout`, and return exit
     code `2` (`exit_codes.USAGE`).
1. **Git Repository & Base Reference Pre-Flight Checks (Exit Code `3`):**
   - Before invoking `cm find --diff`, `find-diff` MUST verify that `--repo`
     resides inside a valid Git working tree
     (`codemender.vcs.git.is_git_repository`). If not, the command MUST exit
     early, logging the error to `stderr`, writing nothing to `stdout`, and
     returning exit code `3` (`exit_codes.FAILED`).
   - Before invoking `cm find --diff`, `find-diff` MUST verify that `--base` is
     a valid, existing Git commit reference in `--repo`
     (`codemender.vcs.git.ref_exists`, e.g.,
     `git rev-parse --verify --quiet --end-of-options <base>^{commit}`). If
     `--base` is invalidly formatted or does not resolve to an existing
     reference in `--repo`, the command MUST exit early without spawning `cm`,
     log the error to `stderr`, write nothing to `stdout`, and return exit code
     `3` (`exit_codes.FAILED`).

#### Scenario: Rejecting Non-Directory `--repo` Argument

- **GIVEN** `/workspace/src/app.py` is a regular file (or `/nonexistent` does
  not exist)
- **WHEN** `cm-runner find-diff --repo /workspace/src/app.py` is executed
- **THEN** `FindDiffConfig.validate()` logs an error to `stderr`, emits zero
  bytes on `stdout`, and exits with code `2`.

#### Scenario: Rejecting Empty Base Reference

- **GIVEN** a caller supplies an empty or whitespace-only `--base` argument
- **WHEN** `cm-runner find-diff --base ""` or `cm-runner find-diff --base "   "`
  is executed
- **THEN** `FindDiffConfig.validate()` exits early before running Git or `cm`,
  writes a usage error to `stderr`, emits zero bytes on `stdout`, and exits with
  code `2`.

#### Scenario: Rejecting Out-of-Range `--diff-workers`

- **GIVEN** a caller supplies `--diff-workers 0` or `--diff-workers 17`
- **WHEN** `cm-runner find-diff --diff-workers 17` is executed
- **THEN** `FindDiffConfig.validate()` exits early before running Git or `cm`,
  writes a usage error naming `--diff-workers` and the `1..16` range to
  `stderr`, emits zero bytes on `stdout`, and exits with code `2`. A
  non-integer value (e.g., `--diff-workers abc`) is rejected by the argument
  parser, also with exit code `2` and nothing on `stdout`.

#### Scenario: Non-Git Directory Target

- **GIVEN** `/tmp/not-a-repo` is an existing directory that is not a Git working
  tree
- **WHEN** `cm-runner find-diff --repo /tmp/not-a-repo` is executed
- **THEN** the Git pre-flight check fails fast, logs the error to `stderr`,
  emits zero bytes on `stdout`, and exits with code `3`.

#### Scenario: Exiting Early When `--base` Is Invalidly Formatted or Does Not Exist

- **GIVEN** `--base` is invalidly formatted (e.g., `origin/..bad..ref`) or does
  not exist in `--repo` (e.g., `origin/nonexistent`)
- **WHEN** `cm-runner find-diff --base origin/nonexistent` is executed
- **THEN** `codemender.vcs.git.ref_exists` fails during pre-flight before
  `CmAdapter.run()` is called, logs the invalid/missing reference error to
  `stderr`, emits zero bytes on `stdout`, and exits with code `3`.

______________________________________________________________________

### REQ-0003: Two-Step Adapter-Isolated Execution (`cm find --diff` + `cm report --format sarif`) {#req-0003}

- **CUJ UUID:** cuj-find-diff-adapter-execution

In accordance with [ADR-0008](../../../adr/ADR-0008-repository-layout-and-package-structure.md),
`codemender/runner/commands/find_diff.py` MUST NOT import `subprocess`, `utils`,
or `vcs.github`, and MUST NOT invoke `run_command` or `build_cm_command`. All
`cm` binary executions MUST go through the `CmAdapter` instance verified by
`codemender/runner/__main__.py:main()`, and any local Git pre-flight checks MUST
go through `codemender.vcs.git`.

`cm-runner find-diff` MUST execute the differential scan and report extraction
in two steps via `CmAdapter.run()`, relying on `cm find --diff` to filter
findings to those relevant to the diff:

1. **Step 1 — Differential Scan (`cm find --diff`):** Invoke `CmAdapter.run()`
   using `cm`'s built-in defaults for diff filtering, neighbor depth, and
   sandbox isolation, with `--diff-workers=<cfg.diff_workers>`:
   - Positional target `<repo_path>` and non-interactive confirmation flag `-y`.
   - `--bypass-warning` to bypass the interactive responsibility confirmation in non-interactive CI environments (matching `cm fix` and `cm verify`).
   - `--diff=<base_ref>` (where `<base_ref>` defaults to `HEAD`).
   - `--diff-workers=<cfg.diff_workers>` (`DEFAULT_DIFF_WORKERS = 8` unless the
     caller passes `--diff-workers`; valid range `1..16`, as supported by `cm`).
   - `--fail-on NONE` so that `cm find --diff` does not exit with code `1` when
     vulnerabilities are discovered, allowing `cm-runner` to distinguish clean
     scan completion (exit `0`) from genuine execution errors (exit `3`).
   - Fixed outer circuit-breaker timeout `_FIND_DIFF_TIMEOUT_SEC = 1800.0` passed
     as `timeout_sec=1800.0` (providing a 30-minute outer ceiling above
     `cm find --diff`'s per-file `--diff-job-timeout` of 10–20 minutes).
   - All human-readable progress output produced by `cm find --diff` on
     `CmResult`'s `stdout` and `stderr` MUST be forwarded to `cm-runner`'s
     `stderr`.
1. **Step 2 — SARIF Report Extraction (`cm report --format sarif`):** Once
   `cm find --diff` completes with exit code `0`, invoke
   `CmAdapter.run(["report", "--format", "sarif"], cwd=str(cfg.repo), timeout_sec=60.0)`
   (`_CM_REPORT_TIMEOUT_SEC = 60.0`) to extract the SARIF 2.1.0 report from
   `CmResult.stdout` while forwarding any `CmResult.stderr` diagnostic lines to
   `cm-runner`'s `stderr`.

#### Scenario: Running `cm find --diff` Followed by `cm report --format sarif`

- **GIVEN** `--repo /workspace` and `--base origin/main`
- **WHEN** `cm-runner find-diff --repo /workspace --base origin/main` executes
- **THEN** `CmAdapter.run()` is first called with
  `["find", "/workspace", "-y", "--diff=origin/main", "--diff-workers=8", "--fail-on", "NONE"]`,
  `cwd="/workspace"`, and `timeout_sec=1800.0`, and upon exit code `0` is next
  called with `["report", "--format", "sarif"]`, `cwd="/workspace"`, and
  `timeout_sec=60.0`.

#### Scenario: Passing a Caller-Selected `--diff-workers` to `cm find --diff`

- **GIVEN** `--repo /workspace`, `--base origin/main`, and `--diff-workers 16`
- **WHEN** `cm-runner find-diff --repo /workspace --base origin/main --diff-workers 16`
  executes
- **THEN** `CmAdapter.run()` is first called with
  `["find", "/workspace", "-y", "--diff=origin/main", "--diff-workers=16", "--fail-on", "NONE"]`.

#### Scenario: Forwarding `cm` Progress Streams to `stderr` While Preserving `stdout` Purity

- **GIVEN** `cm find --diff` writes progress banners
  (`"Impact-Aware PR Delta Scan"`) to its subprocess `stdout` and `stderr`, and
  `cm report --format sarif` writes session diagnostics to its subprocess
  `stderr` and SARIF JSON to its subprocess `stdout`
- **WHEN** `cm-runner find-diff` executes
- **THEN** all `cm find --diff` output and `cm report` `stderr` lines are routed
  to `cm-runner`'s `stderr`, and `cm-runner`'s `stdout` contains only the
  single-line SARIF 2.1.0 JSON output.

#### Scenario: Enforcing Outer Timeout Bounds

- **GIVEN** `cm find --diff` hangs beyond `_FIND_DIFF_TIMEOUT_SEC` (`1800.0`
  seconds)
- **WHEN** `CmAdapter.run()` returns
  `CmResult(exit_code=None, error="timed out after 1800.0 s")`
- **THEN** `cm-runner find-diff` logs a timeout diagnostic to `stderr`, does not
  call `cm report`, emits zero bytes to `stdout`, and exits with code `3`.

______________________________________________________________________

### REQ-0004: Subprocess Environment Passthrough & Working Tree Cleanliness {#req-0004}

- **CUJ UUID:** cuj-find-diff-environment-passthrough

In accordance with [ADR-0003](../../../adr/ADR-0003-environment-scrubbing.md),
`codemender/runner/commands/find_diff.py` and `CmAdapter` MUST NOT import or
call `config.get_scrubbed_env()` or reference `SENSITIVE_ENV_VARS`.
`cm-runner find-diff` MUST invoke `CmAdapter.run()` with `env=None` (the
default), inheriting the caller's environment so that parent `cm` can
authenticate to the CodeMender backend via GCP Application Default Credentials
while `cm`'s native `exebox` sandbox (`workerCmdConfig` and
`sandboxDeniedPaths`) manages the sandboxed worker environment and `TMPDIR`.
`cm-runner find-diff` MUST NOT create a `.codemender_cache/` directory or any
other untracked files inside `--repo`.

#### Scenario: Passing Inherited Environment Without `.codemender_cache` Pollution

- **GIVEN** a clean Git repository at `/workspace`
- **WHEN** `cm-runner find-diff --repo /workspace` invokes `CmAdapter.run()`
- **THEN** `codemender/runner/commands/find_diff.py` does not import
  `get_scrubbed_env` or `SENSITIVE_ENV_VARS`, `CmAdapter.run()` is called with
  `env=None`, and no `/workspace/.codemender_cache` directory is created.

______________________________________________________________________

### REQ-0005: Compact Single-Line SARIF 2.1.0 Output on `stdout` {#req-0005}

- **CUJ UUID:** cuj-find-diff-sarif-stdout

On successful scan completion (exit code `0`), `cm-runner find-diff` MUST parse
the
[OASIS SARIF v2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html)
JSON document produced by `cm report --format sarif` and write it to `stdout` as
a **single, compact (non-pretty-printed) UTF-8 JSON line** terminated by ` `:

1. **Single-Line Compact SARIF Document:** `stdout` MUST contain exactly 1 line
   consisting of the serialized
   [OASIS SARIF v2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html)
   JSON object with no embedded newlines (`json.dumps(sarif_doc)`).
1. **Clean Runs (`0` Findings):** When zero findings are discovered, `stdout`
   MUST still emit the valid single-line SARIF 2.1.0 JSON document with
   `"results": []` inside `runs[0]`.

#### Concrete `stdout` Examples

**Example 1 — Raw `stdout` when 2 findings are discovered (exact 1-line compact
SARIF 2.1.0 output):**

```json
{"$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json", "version": "2.1.0", "runs": [{"tool": {"driver": {"name": "CodeMender", "version": "0.16.0"}}, "results": [{"ruleId": "CWE-78", "level": "error", "message": {"text": "OS Command Injection in archive extractor: Untrusted filename user_archive is passed directly to os.system('/bin/sh -c'), allowing shell metacharacter injection."}, "locations": [{"physicalLocation": {"artifactLocation": {"uri": "src/archive/unpack.py"}, "region": {"startLine": 18, "endLine": 19}}}]}, {"ruleId": "CWE-89", "level": "error", "message": {"text": "SQL Injection in user lookup query: User-supplied user_id is interpolated directly into the SQL query string without parameterization."}, "locations": [{"physicalLocation": {"artifactLocation": {"uri": "src/db/users.py"}, "region": {"startLine": 42, "endLine": 45}}}]}]}]}
```

**Example 2 — Raw `stdout` when 0 findings are discovered (exact 1-line compact
SARIF 2.1.0 output with empty `results`):**

```json
{"$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json", "version": "2.1.0", "runs": [{"tool": {"driver": {"name": "CodeMender", "version": "0.16.0"}}, "results": []}]}
```

#### Scenario: Emitting Compact Single-Line SARIF on `stdout`

- **GIVEN** `cm report --format sarif` returns a multi-line SARIF 2.1.0 JSON
  document with 2 findings
- **WHEN** `cm-runner find-diff` completes
- **THEN** `stdout` contains exactly 1 line (no pretty-printing) that parses as
  a valid SARIF 2.1.0 JSON object with 2 entries in `runs[0].results`, and the
  command exits with code `0`.

#### Scenario: Clean Diff with Zero Findings

- **GIVEN** `cm report --format sarif` returns a SARIF 2.1.0 document with zero
  findings (`"results": []`)
- **WHEN** `cm-runner find-diff` completes
- **THEN** `stdout` contains 1 compact SARIF 2.1.0 JSON line with
  `"results": []`, a human-readable clean summary is logged to `stderr`, and the
  command exits with code `0`.

______________________________________________________________________

### REQ-0006: Report Artifact Generation (`--out`) {#req-0006}

- **CUJ UUID:** cuj-find-diff-out-artifacts

When `--out DIR` is specified, `cm-runner find-diff` MUST create `DIR` if it
does not exist and write the following two artifacts inside `DIR` before writing
the SARIF document to `stdout`:

1. `report.sarif`: The
   [OASIS SARIF v2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html)
   JSON document retrieved from `cm report --format sarif`.
1. `report.md`: The GitHub-Flavored Markdown report retrieved by invoking
   `CmAdapter.run(["report", "--format", "md"], cwd=str(cfg.repo), timeout_sec=60.0)`
   (`_CM_REPORT_TIMEOUT_SEC = 60.0`).

When `--out` is omitted, `cm-runner find-diff` MUST NOT invoke
`cm report --format md` and MUST NOT write report files to the repository
working tree or any other directory outside `~/.codemender`.

#### Scenario: Writing `report.sarif` and `report.md` to `--out` Directory

- **GIVEN** `--out /tmp/cm-artifacts` is passed and 1 finding is discovered
- **WHEN** `cm-runner find-diff --out /tmp/cm-artifacts` completes
- **THEN** `CmAdapter.run()` is called with `["report", "--format", "md"]` after
  `["report", "--format", "sarif"]`, `/tmp/cm-artifacts/report.sarif` and
  `/tmp/cm-artifacts/report.md` are created with valid contents, and the
  repository working tree remains clean (`git status --porcelain` is unchanged).

______________________________________________________________________

### REQ-0007: Coarse Exit Codes & Failure Atomicity {#req-0007}

- **CUJ UUID:** cuj-find-diff-exit-codes

`cm-runner find-diff` MUST enforce the coarse exit code contract defined in
[ADR-0002](../../../adr/ADR-0002.md):

- `0` (`exit_codes.OK`): The differential scan and report extraction completed
  without execution errors, regardless of whether `0` or $N$ findings were
  discovered.
- `2` (`exit_codes.USAGE`): Invalid CLI arguments (e.g., unknown flags,
  non-directory `--repo`, empty `--base` reference, `--diff-workers` outside
  `1..16`, or `--out` pointing to an existing regular file).
- `3` (`exit_codes.FAILED`): Any execution failure, including a missing or
  broken `cm` binary, non-Git `--repo` workspace, invalidly formatted or
  non-existent `--base` Git reference, non-zero exit from `cm find --diff` or
  `cm report`, outer timeout expiration (`_FIND_DIFF_TIMEOUT_SEC` or
  `_CM_REPORT_TIMEOUT_SEC`), unreadable/corrupt SARIF JSON output from
  `cm report --format sarif`, or filesystem write failure in `--out`.

On any exit code `2` or `3`, `cm-runner find-diff` MUST NOT emit any bytes to
`stdout`, guaranteeing that failed runs never produce corrupt or misleading
output for downstream CI stages.

#### Scenario: Malformed or Empty SARIF Output from `cm report --format sarif`

- **GIVEN** `cm find --diff` and `cm report --format sarif` exit with code `0`,
  but `cm report --format sarif` writes truncated or invalid JSON to `stdout`
- **WHEN** `cm-runner find-diff` attempts to parse the SARIF output
- **THEN** the command logs a parse failure diagnostic to `stderr`, emits zero
  bytes to `stdout`, and exits with code `3`.

#### Scenario: Non-Zero `cm` Exit Code on Backend or Scan Failure

- **GIVEN** `cm find --diff` encounters a backend gRPC error or hits its
  per-file `--diff-job-timeout` session limit
- **WHEN** `cm find --diff` exits with return code `1` and writes an error to
  `stderr`
- **THEN** `cm-runner find-diff` forwards the error details to `stderr`, skips
  `cm report`, emits zero bytes to `stdout`, and exits with code `3`.

#### Scenario: Atomic Failure When `--out` Directory Cannot Be Written

- **GIVEN** `cm report --format sarif` returns 2 findings, but
  `--out /dev/null/invalid` cannot be created or written
- **WHEN** `cm-runner find-diff --out /dev/null/invalid` attempts to write
  `report.sarif` and `report.md`
- **THEN** artifact generation runs before `stdout` emission, so `stdout`
  remains completely empty (`0` bytes), the error is logged to `stderr`, and the
  command exits with code `3`.
