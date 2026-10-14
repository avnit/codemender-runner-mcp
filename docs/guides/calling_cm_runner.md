# Calling cm-runner

How to run the CodeMender runner from CI, Cloud Run, or your own machine, and
what it gives back. The design reasoning is in
[ADR-0001](../adr/ADR-0001-machine-readable-output-contract.md) (output) and
[ADR-0008](../adr/ADR-0008-repository-layout-and-package-structure.md) (layout and build).

> **Status:** `version`, `init`, `find-diff`, `fix`, and `verify` work today.

## The command

```
cm-runner [--cm-binary PATH] [--log-level LEVEL] [--no-init] <command> [flags]
```

| Command     | What it does                                                                                 | Status    |
| :---------- | :------------------------------------------------------------------------------------------- | :-------- |
| `version`   | Reports the runner's version and the `cm` binary it found.                                   | Available |
| `init`      | Sets up `cm` for a repository and checks the result. See [`init`](#init).                    | Available |
| `find-diff` | Scans a pull request's changes and outputs SARIF v2.1.0 JSON. See [`find-diff`](#find-diff). | Available |
| `fix`       | Produces a patch for one finding, as a unified diff. See [`fix`](#fix).                      | Available |
| `verify`    | Verifies a finding with `cm verify` and emits updated SARIF 2.1.0. See [`verify`](#verify).   | Available |

## Where to run it

The runner ships as a container image:

```
ghcr.io/cloud-gtm/codemender-runner:<commit-sha>
```

Pin a commit tag. Don't use `latest`.

> [!NOTE] The container image above is the upstream development reference built
> by this repository.
>
> **TODO (Workstreams 2 & 3):** Document customer-managed build and deployment
> pipelines (building from source and pushing to customer private registries
> such as Google Cloud Artifact Registry, AWS ECR, or private GHCR).

| Where          | How                                                                                                                 |
| :------------- | :------------------------------------------------------------------------------------------------------------------ |
| Docker         | `docker run --rm ghcr.io/cloud-gtm/codemender-runner:<sha> version`                                                 |
| GitHub Actions | Set `container: ghcr.io/cloud-gtm/codemender-runner:<sha>` on the job, then `run: cm-runner version`.               |
| Cloud Run      | To be defined.                                                                                                      |
| From source    | `uvx --from ./packages/codemender cm-runner version` or `pip install -e ./packages/codemender && cm-runner version` |

## What you get back

**stdout: one JSON object per line.** Each command writes one kind of line, so
every line is one unit of work. Ignore fields you don't recognise; new ones may
be added. There is no text mode; to read it by hand, pipe it through
`python3 -m json.tool --json-lines` or `jq`.

```json
{"ts": "2026-10-07T01:15:00Z", "event": "version", "orchestrator": "0.1.0", "cm": {"found": true, "version": "0.12.0", "path": "/usr/local/bin/cm"}, "contract": "cm-0.12.x"}
```

`find-diff` and `fix` are the exceptions: stdout carries their result in its
standard format, and nothing else. For `find-diff` that is one SARIF document;
for `fix` it is a unified diff.

**stderr: logs.** Progress, warnings, errors, and all output from `cm`, `git`,
and other programs. Show it to people; don't parse it.

**Exit code:**

| Code | Meaning                                      |
| :--- | :------------------------------------------- |
| `0`  | The command did its job.                     |
| `2`  | Bad command line.                            |
| `3`  | The command failed. The reason is on stderr. |

**`cm` is checked first.** Before any command starts, the runner finds `cm`
(from `--cm-binary`, or the first `cm` on `PATH`) and runs `cm --version`. If
`cm` is missing or won't run, the runner exits `3` with the reason on stderr and
writes nothing to stdout. This applies to `version` too.

**`init` runs automatically.** Before running any command other than `version`
or `init`, `cm-runner` automatically runs [`init`](#init) for `--repo` and logs
the check results to `stderr` (writing nothing to `stdout` so `stdout` holds
only the command's own output). This makes every command self-contained in a
single `docker run --rm` or Cloud Run container invocation. Pass `--no-init` to
skip automatic initialization when `~/.codemender/` is already set up.

**Files.** Commands that write files put them in the folder you pass with
`--out`.

## `init`

```
cm-runner init [--repo PATH]
```

Sets up `cm` for one repository, then checks the result. `cm-runner` runs `init`
automatically before `find-diff`, `fix`, and `verify` (logging to `stderr`
unless `--no-init` is passed), and you can also run `cm-runner init` on its own
to inspect the setup checks as `check` events on `stdout`.

`--repo` defaults to the current folder. In GitHub Actions and GitLab the
checkout already is the current folder, so you rarely need it.

**What it does, in order.** The run stops at the first step that fails.

| #   | Step                                                                                                                                                                                                                                     | `name` of the check line |
| :-- | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :----------------------- |
| 1   | Reports which `cm` is in use and its version. (If `cm` is missing or won't run, the run has already stopped; see [above](#what-you-get-back).)                                                                                           | `cm_binary`              |
| 2   | Runs `cm init`, **only if** `~/.codemender/config.yaml` doesn't exist. An existing config is never replaced.                                                                                                                             | `cm_workspace`           |
| 3   | Writes this repository's settings into that config, then reads the file back to confirm.                                                                                                                                                 | `cm_config`              |
| 4   | Runs `cm init --verify` and reads its summary line, e.g. `Results: 4 passed, 4 warnings`. Fails if it lists any failed checks, or if there is no summary line. Warnings don't fail it. `cm`'s full report, item by item, goes to stderr. | `cm_verify`              |

**Output: one `check` line per step.**

```json
{"ts": "2026-09-29T01:02:30Z", "event": "check", "name": "cm_workspace", "status": "recovered", "message": "created /root/.codemender/config.yaml with 'cm init'"}
```

| `status`    | Meaning                                    | Fails the run? |
| :---------- | :----------------------------------------- | :------------- |
| `pass`      | Fine as it was.                            | No             |
| `recovered` | Something was missing and `init` fixed it. | No             |
| `fail`      | Scans can't run until this is fixed.       | Yes: exit `3`  |

**Exit code:** `0` if nothing failed, `3` if any check failed, `2` if `--repo`
isn't a folder.

**Settings.** Step 3 uses the same environment variables as the older pipeline:

| Variable                             | Effect                                                                                                                                                                                                                                                                   |
| :----------------------------------- | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `CODEMENDER_BUILD_COMMAND`           | The command `cm` runs to build and test a fix. If unset, it's taken from the repository's `.codemender.yaml`, or guessed from project files (e.g. `package.json` → `npm test`). If none is found, `cm init --verify` warns about it (on stderr) and fixes aren't tested. |
| `CODEMENDER_MODEL`                   | The model `cm` uses.                                                                                                                                                                                                                                                     |
| `CODEMENDER_SANDBOX_ENABLED`         | Whether `cm` runs its agent in a sandbox.                                                                                                                                                                                                                                |
| `CODEMENDER_SANDBOX_NETWORK_PROFILE` | The sandbox's network access. Defaults to `permissive-closed` (no network for the agent's commands); set `permissive-open` to allow full network access, e.g. when the build or tests download dependencies. Only affects `fix` and `verify`.                                    |

`cm init --verify` also needs a GCP project ID, from `GOOGLE_CLOUD_PROJECT`,
application default credentials, or the gcloud config. Without one, `cm_verify`
fails.

> [!IMPORTANT] **A passing `init` doesn't prove `cm` can reach Google Cloud.**
> Once `cm init --verify` finds a project ID, it reports server connectivity as
> "Skipped" without contacting the server. That counts as a warning, so
> `cm_verify` passes. Wrong credentials or a missing permission only show up at
> the first scan.

**Running it on your own machine.** `init` changes your real
`~/.codemender/config.yaml`: it keeps your existing file but writes this
repository's settings into it. If no build command can be found and you're at a
terminal, `init` asks you for one; press Enter to skip. In CI it never asks.

## `find-diff`

```
cm-runner find-diff [--repo PATH] [--base REF] [--out DIR] [--diff-workers N]
```

Scans the changes in a repository relative to a base Git reference (see
[CodeMender find modes](https://docs.cloud.google.com/gemini-enterprise-agent-platform/agents/codemender/find-modes#diff-scan))
and reports findings in OASIS SARIF v2.1.0 format. `cm-runner` automatically
runs [`init`](#init) for `--repo` first (logging `init` checks to `stderr` only)
unless `--no-init` is passed.

**Flags:**

| Flag               | Default | Meaning                                                                                         |
| :----------------- | :------ | :---------------------------------------------------------------------------------------------- |
| `--repo PATH`      | `.`     | Path to the Git working tree to scan.                                                           |
| `--base REF`       | `HEAD`  | Base Git reference to diff against (e.g. `origin/main` or a commit SHA).                        |
| `--out DIR`        | None    | Optional folder where `report.sarif` and `report.md` are written. Created if it does not exist. |
| `--diff-workers N` | `8`     | Number of concurrent workers for `cm find --diff` (1..16 supported by `cm`).                    |

**Pre-flight & shallow clones.** Before starting `cm`, `find-diff` checks that
`--repo` is a valid Git working tree and that `--base` resolves to a commit
object in that repository (`git rev-parse --verify <base>^{commit}`).

> [!IMPORTANT] **CI shallow clones:** Most CI checkout steps default to a
> shallow clone (`fetch-depth: 1`), where base branch references like
> `origin/main` are not present locally. Fetch the base branch (e.g.
> `git fetch origin main`) or check out with `fetch-depth: 0` before running
> `cm-runner find-diff --base <ref>`.

**What it runs.** `find-diff` runs:

1. `cm find <repo> -y --diff=<base> --diff-workers=<N> --fail-on NONE`
1. `cm report --format sarif` (written to a temporary file and validated as
   JSON)
1. `cm report --format md` (only when `--out` is passed)

All `cm` progress and diagnostic output streams directly to `stderr`.

**Output: one compact SARIF v2.1.0 JSON line on `stdout`.** Whether `0` or more
findings are discovered, `find-diff` prints exactly one single-line OASIS SARIF
v2.1.0 JSON object to `stdout` (with `"results": []` when zero findings are
found):

```json
{"$schema":"https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json","version":"2.1.0","runs":[{"tool":{"driver":{"name":"CodeMender"}},"results":[]}]}
```

**Files (`--out`).** When `--out DIR` is passed, `find-diff` writes both
`DIR/report.sarif` (formatted SARIF JSON) and `DIR/report.md` (Markdown summary
report) before printing the compact SARIF line to `stdout`.

**Exit code:**

| Code | Meaning                                                                                                                                                                           |
| :--- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `0`  | Scan and report succeeded (`0` or more findings; `--fail-on NONE` ensures findings do not cause a non-zero exit).                                                                 |
| `2`  | Invalid flags (`--repo` is not a directory, `--base` is empty/whitespace, `--diff-workers` outside 1..16, or `--out` points to an existing file). Nothing is written to `stdout`. |
| `3`  | Execution failed (`--repo` is not a Git working tree, `--base` cannot be resolved, `cm` failed or timed out, or writing to `--out` failed). Nothing is written to `stdout`.       |

## `fix`

```
cm-runner fix --finding FILE [--repo PATH] [--out DIR]
```

Runs `cm fix` on one finding and prints the patch as a unified diff. `cm-runner`
automatically runs [`init`](#init) for `--repo` first (logging `init` checks to
`stderr` only) unless `--no-init` is passed.

**Flags:**

| Flag             | Default  | Meaning                                                                                   |
| :--------------- | :------- | :---------------------------------------------------------------------------------------- |
| `--finding FILE` | Required | A SARIF file holding exactly one finding. `-` reads it from stdin.                        |
| `--repo PATH`    | `.`      | The Git working tree to fix.                                                              |
| `--out DIR`      | None     | Folder to also write the patch to, as `<finding file name>.diff`. Created if it is missing. |

There is no `--model` or retry flag. The model comes from `cm`'s default or
`~/.codemender/config.yaml`. To retry, run the command again.

**Before it starts.** `fix` checks two things and changes nothing if either
fails:

1. The finding is a SARIF document holding **exactly one** result. A
   `find-diff` report with several findings is rejected; split it first (see
   [From `find-diff` to `fix`](#from-find-diff-to-fix)). Exit `2`.
1. `--repo` is a Git working tree with **no uncommitted work**: no modified,
   staged, or untracked files, apart from `.cm_project` and `.exploit/`, which
   `cm` writes itself. `cm fix` starts by running
   `git checkout HEAD -- . && git clean -fd`, which would delete that work
   without asking. Exit `3`, with the files listed on stderr. A fresh CI
   checkout always passes.

**What it runs.**

1. `cm report --format json`, to list the findings already in `cm`'s database.
1. `cm report import -f <finding> -p <repo>`. A finding from stdin is first
   written to a temporary file outside the repository.
1. `cm report --format json` again. Exactly one finding must be new; that is
   the ID `cm` gave the import.
1. `cm fix -y --bypass-warning <id>`, with a 30-minute limit. `--bypass-warning`
   accepts `cm`'s responsibility warning for you, as the older pipeline does.
1. `cm report --format json`, to check that the finding's status is now
   `FIXED`. `cm fix` exits `0` even when it did not produce a patch.

> [!IMPORTANT]
> **`fix` is slow.** A one-line fix in a 10-line file took 8 to 9 minutes on
> `cm` 0.11.0. `cm fix` prints nothing until it finishes, so its output appears
> on stderr only at the end. To fix several findings faster, run one `fix` per
> finding in parallel, each in its own checkout or container.

**Output: the patch on stdout.** stdout holds the patch exactly as
`git diff --binary HEAD` produced it, and nothing else. It includes files the
fix created and leaves out `.cm_project` and `.exploit/`. Apply it with
`git apply`:

```bash
cm-runner fix --finding finding.sarif > fix.diff && git apply fix.diff
```

The patch is also left applied in `--repo`. It is not committed or pushed;
that is up to your CI job. Two runs on the same finding can produce different
patches that are both correct, so don't compare patches by text.

**Files (`--out`).** When `--out DIR` is passed, `fix` writes the same bytes to
`DIR/<name>.diff` before printing anything, where `<name>` is the finding
file's name without its extension (`finding` when the finding came from
stdin). Without `--out` it writes no files of its own.

**Exit code:**

| Code | Meaning                                                                                                                                                                                              |
| :--- | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `0`  | The finding is `FIXED` and the patch was written.                                                                                                                                                    |
| `2`  | Bad flags (`--repo` is not a folder, `--finding` is not a readable file, `--out` is a file) or a bad finding (not SARIF, or not exactly one result). Nothing is written to `stdout`.                 |
| `3`  | Anything else: not a Git working tree, uncommitted work, `cm` not set up, import failed, `cm fix` failed or timed out, status not `FIXED`, empty patch, or writing `--out` failed. Nothing on `stdout`. |

## `verify`

```
cm-runner verify --finding FILE [--repo PATH] [--out DIR]
```

Verifies one SARIF 2.1.0 finding using `cm verify` and emits an updated SARIF
2.1.0 document. `cm-runner` automatically runs [`init`](#init) for `--repo`
first (logging `init` checks to `stderr` only) unless `--no-init` is passed.

**Flags:**

| Flag             | Default  | Meaning                                                                                               |
| :--------------- | :------- | :---------------------------------------------------------------------------------------------------- |
| `--finding FILE` | Required | A SARIF file holding exactly one finding. `-` reads it from stdin.                                    |
| `--repo PATH`    | `.`      | The Git working tree to verify.                                                                       |
| `--out DIR`      | None     | Folder to also write the updated SARIF document to, as `<finding file name>.sarif`. Created if needed.|

**Before it starts.** `verify` checks two things and changes nothing if either
fails:

1. The finding is a SARIF document holding **exactly one** result. A
   `find-diff` report with several findings is rejected; split it first. Exit `2`.
2. `--repo` is a Git working tree with **no uncommitted work**: no modified,
   staged, or untracked files, apart from `.cm_project` and `.exploit/`, which
   `cm` writes itself. Exit `3`.

**What it runs.**

1. `cm report --format json`, to inspect findings already in `cm`'s database.
2. `cm report import -f <finding> -p <repo>`, discovering the newly assigned
   finding ID.
3. `cm verify -y --bypass-warning <id>`, with a 30-minute limit.
4. Resets the Git working tree (tracked modifications and untracked files
   outside `.cm_project` and `.exploit/`) via `codemender.vcs.git`.
5. `cm report --format json`, reading the post-verify status and `dismiss_reason`.

**Output: updated SARIF 2.1.0 on stdout.**
stdout holds the updated SARIF document as a single compact JSON line.
- When `cm verify` refutes the finding (`DISMISSED` or `FALSE_POSITIVE`):
  `properties.status` is set to `"DISMISSED"`, `properties.dismiss_reason` is
  populated, `properties.updated_at` is set to the current ISO-8601 UTC
  timestamp, and a SARIF 2.1.0 external suppression entry is added to
  `suppressions`.
- When confirmed exploitable (`VERIFIED`): `properties.status` is set to
  `"VERIFIED"` and `properties.updated_at` is updated.
- When inconclusive (`OPEN`): `properties.status` remains `"OPEN"` and
  `properties.updated_at` is updated.

The input `--finding` file is **never modified in place**.

**Files (`--out`).** When `--out DIR` is passed, `verify` writes the updated
SARIF document to `DIR/<name>.sarif` before writing to `stdout`.

**Exit code:**

| Code | Meaning                                                                                                                                            |
| :--- | :------------------------------------------------------------------------------------------------------------------------------------------------- |
| `0`  | Verification completed (`DISMISSED`, `VERIFIED`, or `OPEN`) and the updated SARIF document was written.                                             |
| `2`  | Bad flags or invalid SARIF finding (not SARIF or not exactly one result). Nothing on `stdout`.                                                     |
| `3`  | Execution failed: not a Git repo, uncommitted work, `cm` not initialized, import failed, `cm verify` failed or timed out, or `--out` write failed. |

### From `find-diff` through `verify` to `fix`

`find-diff` prints one SARIF document for all findings; `verify` and `fix` take
one finding at a time. This script splits the report, verifies each finding, and
only attempts `fix` when verification does not refute the finding:

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

Each patch in `$work/patches` applies to the commit `find-diff` scanned. The
patches were made independently, so two of them can conflict if they touch the
same lines.
