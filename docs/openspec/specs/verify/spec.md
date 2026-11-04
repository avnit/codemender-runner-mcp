<!--
sdd_spec_id: SPEC-VERIFY
sdd_implements: None
sdd_extends: None
sdd_level: COMPONENT
sdd_archetype: CLI_TOOL_BATCH_JOB
sdd_cujspec_product: codemender-runner
-->

# SPEC-VERIFY: Verify One Finding (`cm-runner verify`)

**Implements Spec:** None

**Extends Spec:** None

## Overview

This specification defines the command-line interface, input contract, SARIF
metadata update contract, `stdout` output contract, and `--out` artifact
contract for the `cm-runner verify` subcommand
(`packages/codemender/src/codemender/runner/commands/verify.py`).

`cm-runner verify` is a generic, storage-agnostic local CLI runner that takes
**one finding as a local SARIF file** (or on `stdin` with `--finding -`)—the
exact same single-finding SARIF input that `cm-runner fix` consumes (see
[SPEC-FIX](../fix/spec.md))—imports it into `cm` with `cm report import`, runs
`cm verify` on the newly assigned finding ID, resets any working-tree changes
outside `.cm_project` and `.exploit/`, and inspects the finding's post-verify
status in `cm`'s database. Rather than overwriting the input file in place or
interacting with cloud storage directly, `cm-runner verify` emits the updated
single-finding SARIF 2.1.0 document on `stdout` and (when `--out DIR` is passed)
writes a **new** file `DIR/<finding-name>.sarif`.

Downstream orchestration (GitHub Actions, GitLab CI, or Cloud Run) inspects the
emitted SARIF JSON and **skips `cm-runner fix` only when `verify` successfully
demonstrates the finding is not exploitable (`status == "DISMISSED"`)**. If
`verify` confirms the finding (`VERIFIED`), is inconclusive (`OPEN`), or fails
for any reason (exit `3`), the orchestrator continues to `cm-runner fix` (see
[design.md — Verification Outcomes & Expected Downstream Behaviour](design.md#verification-outcomes--expected-downstream-fix-behaviour)).

The `cm` behaviour this spec relies on was checked against **`cm` 0.11.0** (see
[design.md](design.md#verified-cm-behaviour)).

This specification is governed by and references:

- [SPEC-FIND-DIFF](../find-diff/spec.md) — produces the SARIF findings this
  command consumes.
- [SPEC-FIX](../fix/spec.md) — shares `FixConfig`, pre-flight, and import logic,
  and consumes the finding SARIF input when `verify` does not dismiss it.
- [ADR-0002: Machine-Readable Output Contract](../../../adr/ADR-0002.md)
- [ADR-0008: Repository Layout, Three-Package Workspace, and Runtime Architecture](../../../adr/ADR-0008-repository-layout-and-package-structure.md)
- [ADR-0003: Environment Variable Scrubbing When Running `cm`](../../../adr/ADR-0003-environment-scrubbing.md)

## Requirements

### REQ-0001: CLI Subcommand Registration & Minimal Argument Surface {#req-0001}

- **CUJ UUID:** cuj-verify-cli-args

The orchestrator CLI (`packages/codemender/src/codemender/runner/__main__.py`)
MUST register the `verify` subcommand and dispatch it to
`packages/codemender/src/codemender/runner/commands/verify.py` via
`verify.run(verify.config_from_args(args), adapter, emitter)`.

`VerifyConfig` MUST inherit `finding`, `repo`, `out`, path normalisation, and
`validate()` from `fix.FixConfig`, and provide `sarif_file_name` returning
`<stem>.sarif` (or `finding.sarif` when `finding` is `None`).

`verify` MUST accept only the following command-specific flags:

- `--finding FILE`: **Required.** Path to a local SARIF 2.1.0 file holding
  exactly one finding, or `-` to read it from `stdin`—the same input flag and
  semantics as `cm-runner fix`.
- `--repo PATH`: Path to the Git repository to verify against (default: `.`).
- `--out DIR`: Optional directory where a new updated SARIF file
  (`DIR/<finding-name>.sarif`) is written.

All user-supplied filesystem paths (`--repo`, `--out`, and `--finding` when it
is not `-`) MUST be normalised with `Path(...).expanduser().resolve()`.

`verify` MUST NOT expose `--model`, `--context`, `--skip-exploit-verification`,
retry flags, or cloud storage (`gs://`) flags. Fetching findings from and
syncing results to remote storage is the responsibility of the outer
orchestration layer.

#### Scenario: Verifying a Local Finding SARIF File in the Current Directory

- **GIVEN** a Git repository in the current working directory and a local
  finding SARIF file `findings/finding-00.sarif`
- **WHEN** `cm-runner verify --finding findings/finding-00.sarif` is invoked
- **THEN** `repo` resolves to the current working directory, `finding` resolves
  to the absolute path of `findings/finding-00.sarif`, `out` is `None`, and
  `sarif_file_name` is `finding-00.sarif`.

#### Scenario: Reading the Finding from `stdin` and Writing to `--out`

- **GIVEN** a Git repository at `/workspace`
- **WHEN** `cm-runner verify --finding - --repo /workspace --out /tmp/verified`
  is invoked
- **THEN** `finding` is `None` (indicating `stdin`), `repo` resolves to
  `/workspace`, `out` resolves to `/tmp/verified`, and `sarif_file_name` is
  `finding.sarif`.

______________________________________________________________________

### REQ-0002: Input Validation (Exit Code `2`) {#req-0002}

- **CUJ UUID:** cuj-verify-input-validation

`VerifyConfig.validate()` (inherited from `fix.FixConfig`) MUST check the
command line before anything is run. On any validation failure it MUST log the
reason to `stderr`, write nothing to `stdout`, and return exit code `2`
(`exit_codes.USAGE`):

1. `--repo` does not exist or is not a directory.
1. `--finding` is not `-` and is not an existing, readable regular file.
1. `--out` points to an existing regular file.

The finding content MUST then be read and checked via
`fix._read_finding(cfg, command="verify")` and `sarif.SarifDocument.from_json()`,
also exiting `2` (`exit_codes.USAGE`) on failure:

4. The content is not valid JSON (`SarifJsonError`), or is not a SARIF document
   with a `runs` list (`SarifSchemaError`).
1. The document does not hold **exactly one** result across all `runs`
   (`doc.result_count != 1`).

A SARIF file with zero or multiple results is rejected so one `verify` run
always corresponds to one finding.

#### Scenario: Rejecting a Missing Finding File

- **GIVEN** `missing.sarif` does not exist on disk
- **WHEN** `cm-runner verify --finding missing.sarif` is invoked
- **THEN** the command logs that `--finding` is not a readable file, emits zero
  bytes on `stdout`, and exits `2` without calling Git or `cm`.

#### Scenario: Rejecting a SARIF File with Two Findings

- **GIVEN** `report.sarif` holds two results in `runs[0].results`
- **WHEN** `cm-runner verify --finding report.sarif` is invoked
- **THEN** the command logs that `verify` takes exactly one finding, emits zero
  bytes on `stdout`, and exits `2` without calling Git or `cm`.

______________________________________________________________________

### REQ-0003: Repository Pre-Flight Checks (Exit Code `3`) {#req-0003}

- **CUJ UUID:** cuj-verify-preflight

Before calling `cm`, `verify` MUST check the repository via
`fix._check_repository(cfg.repo, command="verify")`
(`packages/codemender/src/codemender/vcs/git.py`). On failure it MUST log the
reason to `stderr`, write nothing to `stdout`, and exit `3`
(`exit_codes.FAILED`):

1. `--repo` is not inside a Git working tree (`vcs.git.is_git_repository`,
   shared with SPEC-FIND-DIFF and SPEC-FIX).
1. `--repo` has uncommitted changes: modified, staged, or untracked files
   (`vcs.git.list_uncommitted_paths`). The files `cm` itself writes into a
   repository, **`.cm_project` and `.exploit/` (`fix._CM_OWN_PATHS`), MUST be
   ignored** by this check.

The second check exists because **`cm verify` discards uncommitted work before
it starts** (`git checkout HEAD -- . && git clean -fd`). Refusing to run
protects work that would otherwise be lost without warning. Ignoring `cm`'s own
files lets `verify` run straight after `find-diff` or a previous `verify` run.

#### Scenario: Refusing to Run with Uncommitted Work

- **GIVEN** `app.py` has an uncommitted edit in `--repo`
- **WHEN** `cm-runner verify --finding f.sarif` is invoked
- **THEN** the command lists `app.py` on `stderr` noting `'cm verify' would
  discard it`, does not call `cm`, emits zero bytes on `stdout`, and exits `3`.
  `app.py` is unchanged.

#### Scenario: Running After `find-diff` or `verify` Left `.cm_project` and `.exploit/`

- **GIVEN** the only untracked entries in the repository are `.cm_project` and
  `.exploit/poc.sh`
- **WHEN** `cm-runner verify --finding f.sarif` is invoked
- **THEN** the pre-flight check passes and verification proceeds.

______________________________________________________________________

### REQ-0004: Import, Verify, Reset Working Tree, and Read Status Through `CmAdapter` {#req-0004}

- **CUJ UUID:** cuj-verify-adapter-execution

In accordance with [ADR-0008](../../../adr/ADR-0008-repository-layout-and-package-structure.md),
`packages/codemender/src/codemender/runner/commands/verify.py` MUST NOT import
`subprocess`, `utils`, `vcs.github`, or `storage`. All `cm` calls MUST go
through the `CmAdapter` checked by
`packages/codemender/src/codemender/runner/__main__.py`, with
`cwd=cfg.repo.as_posix()`. Git calls MUST go through
`packages/codemender/src/codemender/vcs/git.py`.

`verify` MUST run these steps in order, stopping at the first execution failure:

1. **List existing findings (`fix._import`):** `cm report --format json`
   (`_CM_REPORT_TIMEOUT_SEC = 60.0`) and record their `finding_id`s.
1. **Import the finding (`fix._import`):**
   `cm report import -f <sarif_file> -p <repo>` (`_CM_REPORT_TIMEOUT_SEC`),
   where `<sarif_file>` is the resolved `--finding` path, or a temporary file
   outside `--repo` when `--finding -` was given.
1. **Find the new ID (`fix._import`):** run `cm report --format json` again.
   There MUST be exactly one `finding_id` that was not there in step 1 (any
   `finding_id` embedded in the input SARIF properties MUST NOT be used
   directly).
1. **Verify (`_run_verify`):** `cm verify -y --bypass-warning <finding_id>`
   (`_VERIFY_TIMEOUT_SEC`). All of `cm`'s `stdout` and `stderr` MUST be
   logged to `cm-runner`'s `stderr`.
1. **Reset working tree (`_run_verify` `finally` block):** call
   `git.reset_working_tree(repo.as_posix(), exclude=fix._CM_OWN_PATHS)` to
   revert any tracked file modifications (`git checkout HEAD -- .`) and clean
   any untracked files (`git clean -fd`) outside `.cm_project` and `.exploit/`
   that the verification agent made in `--repo`.
1. **Read status:** call `adapter.get_finding(finding.finding_id,
   timeout_sec=fix._CM_REPORT_TIMEOUT_SEC)` (`cm report --format json`) and read
   the finding's `status` and `dismiss_reason`.

If `cm verify` fails to start, exits with a non-zero code, or times out
(`not result.ok`), or if `git.reset_working_tree` fails, `verify` MUST log the
failure to `stderr`, emit zero bytes on `stdout`, write nothing to `--out`, and
exit `3`. `verify` MUST NOT make more than one `cm verify` attempt.

#### Scenario: Successful Verification Run

- **GIVEN** a clean repository at `/workspace` and a finding at
  `/tmp/finding-00.sarif`
- **WHEN** `cm-runner verify --repo /workspace --finding /tmp/finding-00.sarif`
  executes
- **THEN** `CmAdapter.run()` is called with, in order:
  `["report", "--format", "json"]`,
  `["report", "import", "-f", "/tmp/finding-00.sarif", "-p", "/workspace"]`,
  `["report", "--format", "json"]`,
  `["verify", "-y", "--bypass-warning", "<new id>"]` with `timeout_sec=_VERIFY_TIMEOUT_SEC`,
  and `["report", "--format", "json"]`; all with `cwd="/workspace"` and
  `env=None`.

#### Scenario: `cm verify` Times Out

- **GIVEN** `cm verify` runs past `_VERIFY_TIMEOUT_SEC`
- **WHEN** `CmAdapter.run()` returns a timeout
  `CmResult(exit_code=None, error=...)`
- **THEN** the command logs the timeout, resets the repository working tree in
  `finally`, writes nothing to `--out`, emits zero bytes on `stdout`, and exits
  `3`.

______________________________________________________________________

### REQ-0005: Mapping Verification Outcomes into the Output SARIF {#req-0005}

- **CUJ UUID:** cuj-verify-sarif-update

When `_run_verify` succeeds and `adapter.get_finding` returns the post-verify
`Finding`, `_apply_verification_result` and `_update_sarif_status` MUST update a
copy of the input `SarifDocument` in memory without modifying the input
`--finding` file in place:

1. **Demonstrated Not Exploitable (`verified.is_dismissed` is `True`, i.e.
   `status` in `cm report --format json` is `DISMISSED` or `FALSE_POSITIVE`):**
   - Set `result["properties"]["status"] = "DISMISSED"`.
   - Set `result["properties"]["dismiss_reason"]` to `verified.dismiss_reason`
     if non-empty, or `_DEFAULT_DISMISS_REASON`
     (`"Finding was demonstrated not exploitable by cm verify"`) if `cm` did not
     provide one.
   - Set `result["properties"]["updated_at"]` to the current ISO-8601 UTC
     timestamp (`YYYY-MM-DDTHH:MM:SSZ`).
   - Append a SARIF 2.1.0 external suppression entry to `result["suppressions"]`:
     ```json
     {
       "kind": "external",
       "status": "accepted",
       "justification": "<dismiss_reason>"
     }
     ```
1. **Confirmed Exploitable (`verified.is_verified` is `True`, i.e.
   `status == "VERIFIED"`):**
   - Set `result["properties"]["status"] = "VERIFIED"`.
   - Set `result["properties"]["updated_at"]` to the current ISO-8601 UTC
     timestamp.
1. **Inconclusive (any other status, e.g. `status == "OPEN"` or empty):**
   - Set `result["properties"]["status"] = verified.status or "OPEN"` (MUST NOT
     mark as `DISMISSED`, so the downstream orchestrator continues to
     `cm-runner fix`).
   - Set `result["properties"]["updated_at"]` to the current ISO-8601 UTC
     timestamp.

All other fields of the SARIF document (`$schema`, `version`, `runs`, `tool`,
`ruleId`, `message`, `locations`, and other keys in `properties` such as the
original `finding_id`) MUST be preserved unchanged.

______________________________________________________________________

### REQ-0006: `stdout` SARIF Output and New `--out` File {#req-0006}

- **CUJ UUID:** cuj-verify-output

1. **`--out DIR` (`_write_out_sarif`):** When `--out DIR` is given, `verify`
   MUST create `DIR` if needed and write `sarif_doc.to_json() + "\n"` to a new
   file `DIR/<name>.sarif` **before** writing to `stdout`, where `<name>` is the
   stem of `--finding` (or `finding` when `--finding -` was used). The input
   `--finding` file MUST NOT be overwritten unless the caller explicitly pointed
   `--out` at the same directory and filename. A write failure MUST exit `3`
   with zero bytes on `stdout`. When `--out` is omitted, `verify` MUST NOT write
   any local output file outside what `cm` itself writes.
1. **`stdout`:** After any `--out` write succeeds, `verify` MUST write the
   updated SARIF 2.1.0 document as a single compact JSON line
   (`sarif_doc.to_json() + "\n"`) to `stdout` and flush the stream, matching
   `cm-runner find-diff`.

#### Scenario: Writing a New Updated SARIF File to `--out`

- **GIVEN** `--finding /workspace/in/cwe-94.sarif --out /workspace/verified`
- **WHEN** `verify` finishes and refutes the finding
- **THEN** `/workspace/in/cwe-94.sarif` is untouched,
  `/workspace/verified/cwe-94.sarif` is created with
  `runs[0].results[0].properties.status == "DISMISSED"`, and `stdout` contains
  the same SARIF JSON on a single line.

______________________________________________________________________

### REQ-0007: Environment, Working Tree, and Exit Codes {#req-0007}

- **CUJ UUID:** cuj-verify-exit-codes

In accordance with [ADR-0003](../../../adr/ADR-0003-environment-scrubbing.md),
`verify` MUST call `CmAdapter.run()` with `env=None` and MUST NOT call
`config.get_scrubbed_env()` or create `.codemender_cache/`.

After `cm verify` runs (whether it succeeds or fails), `verify` MUST reset any
tracked file edits and remove any untracked files outside `.cm_project` and
`.exploit/` in `--repo` (`git.reset_working_tree`), and MUST delete any
temporary SARIF file created for `stdin`, so `cm-runner fix` can run immediately
afterwards on the same repository checkout.

Exit codes, per [ADR-0002](../../../adr/ADR-0002.md):

- `0` (`exit_codes.OK`): `cm verify` ran to completion, the working tree was
  reset, and the updated SARIF document (with `status` set to `DISMISSED`,
  `VERIFIED`, or `OPEN`) was written to `--out` (if given) and `stdout`.
- `2` (`exit_codes.USAGE`): bad flags or invalid SARIF input (REQ-0002).
- `3` (`exit_codes.FAILED`): anything else, including a missing or broken `cm`,
  a failed repository pre-flight check, a failed `cm report import`, no single
  new finding ID, a `cm verify` process error or timeout, a failed working-tree
  reset, or a failed `--out` write.

On exit `2` or `3`, `verify` MUST NOT write any bytes to `stdout`.

______________________________________________________________________

### REQ-0008: Caller Guide — From `find-diff` Through `verify` to `fix` {#req-0008}

- **CUJ UUID:** cuj-verify-caller-guide

`packages/codemender/README.md` MUST show how an orchestrator chains
`find-diff`, `verify`, and `fix` using local SARIF files outside the repository
and only skips `fix` when `verify` outputs `status == "DISMISSED"`:

```bash
set -euo pipefail
shopt -s nullglob
work="$(mktemp -d)"   # Outside the repository, so runner doesn't see these files.

# 1. Scan the diff and split into one local SARIF file per finding
cm-runner find-diff --repo . --base origin/main > "$work/report.sarif"
mkdir -p "$work/findings" "$work/verified" "$work/patches"
jq -c '.runs[0].results[] as $r | .runs[0].results = [$r]' "$work/report.sarif" \
  | split -l 1 -d --additional-suffix=.sarif - "$work/findings/finding-"

# 2. Verify each finding and write a new SARIF to verified/<name>.sarif.
#    Only skip fix when verify successfully demonstrated the finding is not
#    exploitable (status == "DISMISSED"). If verify confirmed it (VERIFIED),
#    was inconclusive (OPEN), or failed (exit 3), continue to fix.
for f in "$work"/findings/finding-*.sarif; do
  name=$(basename "$f")
  cm-runner verify --repo . --finding "$f" --out "$work/verified" || true

  status=$(jq -r '.runs[0].results[0].properties.status // "OPEN"' "$work/verified/${name}" 2>/dev/null || echo "OPEN")
  if [ "$status" != "DISMISSED" ]; then
    cm-runner fix --repo . --finding "$f" --out "$work/patches" >/dev/null || echo "No fix for $f" >&2
    git reset -q --hard && git clean -fdq
  fi
done
ls "$work/patches" 2>/dev/null || echo "No patches."
```
