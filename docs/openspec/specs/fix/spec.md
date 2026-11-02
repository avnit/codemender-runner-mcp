<!--*
freshness: { owner: 'codemender-maintainers', reviewed: '2026-09-30' }
*-->

<!--
sdd_spec_id: SPEC-FIX
sdd_implements: None
sdd_extends: None
sdd_level: COMPONENT
sdd_archetype: CLI_TOOL_BATCH_JOB
sdd_cujspec_product: codemender-runner
-->

# SPEC-FIX: Patch One Finding (`cm-runner fix`)

**Implements Spec:** None

**Extends Spec:** None

## Overview

This specification defines the command-line interface, input contract,
`stdout` output contract, and `--out` artifact contract for the
`cm-runner fix` subcommand
(`packages/codemender/src/codemender/runner/commands/fix.py`).

`cm-runner fix` takes **one finding as a SARIF file** (the format
`cm-runner find-diff` produces, see SPEC-FIND-DIFF), imports it into `cm` with
`cm report import`, runs `cm fix` on it, and writes the resulting patch to
`stdout` as a **unified diff**, exactly as `git diff` produced it. It leaves the
`cm` sandbox intact and relies on `cm`'s defaults and on
`~/.codemender/config.yaml` (written by `cm-runner init`) for everything else.

The `stdout` format, a raw unified diff (REQ-0005), was agreed on PR #23
(cledoux@, 2026-10-01). The alternatives considered are in
[design.md](design.md#decision-2).

The `cm` behaviour this spec relies on was checked against **`cm` 0.11.0** in a
live run on 2026-09-30 (see [design.md](design.md#verified-cm-behaviour)).

This specification is governed by and references:

- [SPEC-FIND-DIFF](../find-diff/spec.md) — produces the SARIF this command
  consumes.
- [ADR-0002: Machine-Readable Output Contract](../../../adr/ADR-0002.md)
- [ADR-0008: Repository Layout, Three-Package Workspace, and Runtime Architecture](../../../adr/ADR-0008-repository-layout-and-package-structure.md)
- [ADR-0003: Environment Variable Scrubbing When Running `cm`](../../../adr/ADR-0003-environment-scrubbing.md)

## Requirements

### REQ-0001: CLI Subcommand Registration & Minimal Argument Surface {#req-0001}

- **CUJ UUID:** cuj-fix-cli-args

The orchestrator CLI (`packages/codemender/src/codemender/runner/__main__.py`)
MUST register the `fix` subcommand and dispatch it to
`packages/codemender/src/codemender/runner/commands/fix.py` via
`fix.run(cfg, adapter, emitter)`.

`fix` MUST accept only the following command-specific flags:

- `--finding FILE`: **Required.** Path to a SARIF 2.1.0 file holding exactly
  one finding. `-` reads the SARIF document from standard input instead.
- `--repo PATH`: Path to the Git repository to fix (default: `.`).
- `--out DIR`: Optional directory where the diff file is written.

All user-supplied filesystem paths (`--finding` unless `-`, `--repo`, `--out`)
MUST be normalised with `Path(...).expanduser().resolve()`.

`fix` MUST NOT expose `--model`, `--context`, or retry flags. The model comes
from `cm`'s default or from `~/.codemender/config.yaml`.

#### Scenario: Fixing a Finding File in the Current Directory

- **GIVEN** a Git repository in the current working directory and a finding
  file `findings/cwe-78.sarif`
- **WHEN** `cm-runner fix --finding findings/cwe-78.sarif` is invoked
- **THEN** `repo` resolves to the current working directory and `out` is
  `None`.

#### Scenario: Reading the Finding from Standard Input

- **GIVEN** a caller pipes one SARIF document into the command
- **WHEN** `cm-runner fix --finding - --repo /workspace` is invoked
- **THEN** the finding is read from `stdin`, and the command otherwise behaves
  as if it had been given a file.

______________________________________________________________________

### REQ-0002: Input Validation (Exit Code `2`) {#req-0002}

- **CUJ UUID:** cuj-fix-input-validation

`FixConfig.validate()` MUST check the command line before anything is run. On
any failure it MUST log the reason to `stderr`, write nothing to `stdout`, and
return exit code `2` (`exit_codes.USAGE`):

1. `--repo` does not exist or is not a directory.
1. `--finding` is not `-` and is not a readable file.
1. `--out` points to an existing regular file.

The finding itself MUST then be read and checked, also exiting `2` on failure:

4. The content is not valid JSON, or is not a SARIF document (no `runs`
   list).
1. The document does not hold **exactly one** result across all `runs`.

A finding file with several results is rejected rather than fixed in part, so
one `fix` run always means one finding and one patch.

#### Scenario: Rejecting a SARIF File with Two Findings

- **GIVEN** `report.sarif` from `find-diff` holds two results
- **WHEN** `cm-runner fix --finding report.sarif` is invoked
- **THEN** the command logs that exactly one finding is required, suggests
  splitting the file (REQ-0008), emits zero bytes on `stdout`, and exits `2`
  without running Git or `cm`.

#### Scenario: Rejecting a Non-SARIF File

- **GIVEN** `finding.json` is valid JSON but has no `runs` list
- **WHEN** `cm-runner fix --finding finding.json` is invoked
- **THEN** the command emits zero bytes on `stdout`, and exits `2`.

______________________________________________________________________

### REQ-0003: Repository Pre-Flight Checks (Exit Code `3`) {#req-0003}

- **CUJ UUID:** cuj-fix-preflight

Before calling `cm`, `fix` MUST check the repository through
`codemender.vcs.git`. On failure it MUST log the reason to `stderr`, write
nothing to `stdout`, and exit `3` (`exit_codes.FAILED`):

1. `--repo` is not inside a Git working tree
   (`codemender.vcs.git.is_git_repository`, shared with SPEC-FIND-DIFF).
1. `--repo` has uncommitted changes: modified, staged, or untracked files.
   The files `cm` itself writes into a repository, **`.cm_project` and
   `.exploit/`, MUST be ignored** by this check.

The second check exists because **`cm fix` discards uncommitted work before it
starts** (`git checkout HEAD -- . && git clean -fd`). Refusing to run protects
work that would otherwise be lost without warning. Ignoring `cm`'s own files
lets `fix` run straight after `find-diff`, which leaves `.cm_project` behind.

#### Scenario: Refusing to Run with Uncommitted Work

- **GIVEN** `util.py` has an uncommitted edit and `notes.txt` is untracked
- **WHEN** `cm-runner fix --finding f.sarif` is invoked
- **THEN** the command lists the uncommitted files on `stderr`, does not call
  `cm`, emits zero bytes on `stdout`, and exits `3`. Both files are unchanged.

#### Scenario: Running After `find-diff` Left `.cm_project`

- **GIVEN** the only untracked entry in the repository is `.cm_project`
- **WHEN** `cm-runner fix --finding f.sarif` is invoked
- **THEN** the pre-flight check passes and the fix proceeds.

______________________________________________________________________

### REQ-0004: Import, Fix, and Confirm Through `CmAdapter` {#req-0004}

- **CUJ UUID:** cuj-fix-adapter-execution

In accordance with [ADR-0008](../../../adr/ADR-0008-repository-layout-and-package-structure.md),
`codemender/runner/commands/fix.py` MUST NOT import `subprocess`, `utils`, or
`vcs.github`. All `cm` calls MUST go through the `CmAdapter` checked by
`codemender/runner/__main__.py`, with `cwd=str(cfg.repo)`. Git calls MUST go
through `codemender.vcs.git`.

`fix` MUST run these steps in order, stopping at the first failure:

1. **List existing findings:** `cm report --format json`
   (`_CM_REPORT_TIMEOUT_SEC = 60.0`) and record their `finding_id`s.
1. **Import the finding:** `cm report import -f <file> -p <repo>`
   (`_CM_REPORT_TIMEOUT_SEC`). When the finding came from `stdin`, it is first
   written to a temporary file **outside** `--repo`, deleted afterwards.
1. **Find the new ID:** run `cm report --format json` again. There MUST be
   exactly one `finding_id` that was not there in step 1. `cm report import`
   does not print the ID it assigns, and every import assigns a new one.
1. **Fix:** `cm fix -y --bypass-warning <finding_id>`
   (`_FIX_TIMEOUT_SEC`). `fix` MUST log a line to `stderr` before
   `cm fix` starts, saying it usually takes 10 minutes or more. `CmAdapter`
   captures `cm`'s output, so all of `cm`'s `stdout` and `stderr` MUST be logged
   to `cm-runner`'s `stderr` when `cm fix` exits, whatever the outcome.
1. **Confirm:** run `cm report --format json` and read the finding's `status`.
   It MUST be `FIXED`. `cm fix` exits `0` whether or not it produced a patch,
   so its exit code alone is not trusted.

`--bypass-warning` accepts `cm`'s responsibility warning on the caller's behalf,
as the existing pipeline does. `fix` MUST NOT make more than one `cm fix`
attempt. Retrying is left to the caller.

#### Scenario: Successful Fix

- **GIVEN** a clean repository at `/workspace` and a one-finding SARIF file
- **WHEN** `cm-runner fix --repo /workspace --finding f.sarif` executes
- **THEN** `CmAdapter.run()` is called with, in order:
  `["report", "--format", "json"]`,
  `["report", "import", "-f", "<abs path of f.sarif>", "-p", "/workspace"]`,
  `["report", "--format", "json"]`,
  `["fix", "-y", "--bypass-warning", "<new id>"]` with `timeout_sec=_FIX_TIMEOUT_SEC`,
  and `["report", "--format", "json"]`; all with `cwd="/workspace"`.

#### Scenario: `cm fix` Finishes Without Fixing

- **GIVEN** `cm fix` exits `0` but the finding's status is still `OPEN`
- **WHEN** `cm-runner fix` reads the status
- **THEN** it logs the status and `cm`'s output to `stderr`, emits zero bytes on
  `stdout`, and exits `3`.

#### Scenario: `cm fix` Times Out

- **GIVEN** `cm fix` runs past `_FIX_TIMEOUT_SEC`
- **WHEN** `CmAdapter.run()` returns a timeout `CmResult(exit_code=None, error=...)`
- **THEN** the command logs the timeout, does not read the status or the diff,
  emits zero bytes on `stdout`, and exits `3`.

______________________________________________________________________

### REQ-0005: Unified Diff on `stdout` {#req-0005}

- **CUJ UUID:** cuj-fix-diff-stdout

See [design.md, Decision 2](design.md#decision-2) for the alternatives
considered.

When the finding is `FIXED`, `fix` MUST capture the patch with Git and write it
to `stdout` **unchanged**:

1. The patch is `git diff --binary HEAD` over the whole working tree,
   **excluding `.cm_project` and `.exploit/`**, and **including files the fix
   created** (untracked files are marked intent-to-add, `git add -N`, before
   diffing).
1. Git's output MUST be read as **raw bytes**, with Git's `stderr` kept
   separate, and written to `stdout` byte for byte. Reading it as text would
   turn Windows line endings (`\r\n`) into `\n`, and the patch would then fail
   `git apply`. Mixing in `stderr` would put Git's warnings inside the patch.
1. The diff command MUST set colour, prefixes, external diff tools, text
   conversion and relative paths explicitly (`--no-color`, `--src-prefix=a/`,
   `--dst-prefix=b/`, `--no-ext-diff`, `--no-textconv`, `--no-relative`), so a
   caller's Git configuration cannot change the format.
1. `stdout` MUST contain the diff and nothing else, so that
   `cm-runner fix ... > fix.diff && git apply fix.diff` works.
1. An empty diff MUST be treated as a failure (exit `3`, zero bytes on
   `stdout`), even when `cm` reports `FIXED`.

The patch is taken from Git rather than from `cm`, because `cm`'s own copies are
either mixed with progress text (`cm fix`'s `stdout`), printed in a display
format (`cm report --patches`), or not available (`cm fix --export` is not
implemented in 0.11.0).

#### Concrete `stdout` Example

```diff
diff --git a/app.py b/app.py
index 49e349b..71e5662 100644
--- a/app.py
+++ b/app.py
@@ -1,10 +1,11 @@
 import os
+import shlex
 import sys


 def ping(host):
     """Pings a host once and returns the exit status."""
-    return os.system("ping -c 1 " + host)
+    return os.system("ping -c 1 " + shlex.quote(str(host)))
```

#### Scenario: Patch Excludes `cm`'s Own Files

- **GIVEN** after `cm fix` the working tree shows `M app.py` and an untracked
  `.cm_project`
- **WHEN** `fix` captures the diff
- **THEN** `stdout` holds the change to `app.py` only.

#### Scenario: Patch Includes a File the Fix Created

- **GIVEN** the fix adds a new file `sanitize.py` and edits `app.py`
- **WHEN** `fix` captures the diff
- **THEN** `stdout` holds both the new file and the edit.

______________________________________________________________________

### REQ-0006: Diff File (`--out`) {#req-0006}

- **CUJ UUID:** cuj-fix-out-artifact

When `--out DIR` is given, `fix` MUST create `DIR` if needed and write the same
diff to `DIR/<name>.diff` **before** writing to `stdout`, where `<name>` is the
finding file's name without its extension, or `finding` when the finding came
from `stdin`. A write failure MUST exit `3` with zero bytes on `stdout`.

When `--out` is omitted, `fix` MUST NOT write any file other than those `cm`
itself writes.

#### Scenario: Diff File Named After the Finding File

- **GIVEN** `--finding findings/cwe-78-app-py.sarif --out /tmp/out`
- **WHEN** the fix succeeds
- **THEN** `/tmp/out/cwe-78-app-py.diff` holds the same bytes as `stdout`.

______________________________________________________________________

### REQ-0007: Environment, Working Tree, and Exit Codes {#req-0007}

- **CUJ UUID:** cuj-fix-exit-codes

In accordance with [ADR-0003](../../../adr/ADR-0003-environment-scrubbing.md),
`fix` MUST call `CmAdapter.run()` with `env=None` and MUST NOT call
`config.get_scrubbed_env()` or create `.codemender_cache/`.

After a successful run, `fix` MUST leave the patch applied in `--repo` and MUST
NOT commit it. `.cm_project` remains as `cm` left it.

Exit codes, per [ADR-0002](../../../adr/ADR-0002.md):

- `0` (`exit_codes.OK`): the finding is `FIXED` and a non-empty diff was
  written.
- `2` (`exit_codes.USAGE`): bad flags or a bad finding file (REQ-0002).
- `3` (`exit_codes.FAILED`): anything else, including a missing or broken `cm`,
  a failed pre-flight check, a failed import, no new finding ID, a `cm` error
  or timeout, a status other than `FIXED`, an empty diff, or a failed `--out`
  write.

On exit `2` or `3`, `fix` MUST NOT write any bytes to `stdout`.

______________________________________________________________________

### REQ-0008: Caller Guide — From `find-diff` to `fix` {#req-0008}

- **CUJ UUID:** cuj-fix-caller-guide

`docs/guides/calling_cm_runner.md` MUST show how to turn `find-diff`'s single
SARIF document into one file per finding and fix each one. Because `fix`
refuses to run on uncommitted work (REQ-0003) and leaves its patch applied
(REQ-0007), the example MUST:

1. Keep the report, the split finding files, and the patches **outside** the
   repository, so they don't count as uncommitted work.
1. Check that the working tree is clean (ignoring `.cm_project` and
   `.exploit/`) **before** the loop, and stop if it isn't.
1. Reset the working tree (`git reset --hard && git clean -fd`) **after every**
   `fix`, so the next one starts clean. The first check guarantees that this
   only discards `cm`'s changes, which `--out` has already saved.

The split uses this `jq` expression, checked with `jq` 1.6: each output line is
a complete SARIF document holding one result.

```bash
jq -c '.runs[0].results[] as $r | .runs[0].results = [$r]' "$work/report.sarif" \
  | split -l 1 -d --additional-suffix=.sarif - "$work/finding-"
```

The loop assumes `find-diff`'s report holds only that scan's findings. If it
also held findings from earlier runs, the loop would spend a `cm fix` run on
each of them.
