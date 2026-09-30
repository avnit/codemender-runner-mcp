# Calling cm-runner

How to run the CodeMender runner from CI, Cloud Run, or your own machine, and
what it gives back. The design reasoning is in
[ADR-0001](../adr/ADR-0001-machine-readable-output-contract.md) (output) and
[ADR-0002](../adr/ADR-0002-runner-layout-and-build.md) (layout and build).

> **Status:** `version`, `init`, and `find-diff` work today. `fix` and `verify`
> are in progress. Each will be added to this guide when it lands.

## The command

```
cm-runner [--cm-binary PATH] [--log-level LEVEL] <command> [flags]
```

| Command     | What it does                                                                                 | Status    |
| :---------- | :------------------------------------------------------------------------------------------- | :-------- |
| `version`   | Reports the runner's version and the `cm` binary it found.                                   | Available |
| `init`      | Sets up `cm` for a repository and checks the result. See [`init`](#init).                    | Available |
| `find-diff` | Scans a pull request's changes and outputs SARIF v2.1.0 JSON. See [`find-diff`](#find-diff). | Available |
| `fix`       | Produces a patch for one finding.                                                            | Planned   |
| `verify`    | Checks whether a finding is real, or whether a patch fixes it.                               | Planned   |

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

| Where          | How                                                                                                   |
| :------------- | :---------------------------------------------------------------------------------------------------- |
| Docker         | `docker run --rm ghcr.io/cloud-gtm/codemender-runner:<sha> version`                                   |
| GitHub Actions | Set `container: ghcr.io/cloud-gtm/codemender-runner:<sha>` on the job, then `run: cm-runner version`. |
| Cloud Run      | To be defined.                                                                                        |
| From source    | `python3 codemender-runner version` (needs Python 3.11+ and the packages in `requirements.txt`).      |

Running the image with **no arguments** starts the older pipeline, configured by
`CODEMENDER_RUN_MODE`. Existing Cloud Run jobs depend on this. New integrations
should always name a command.

## What you get back

**stdout: one JSON object per line.** Each command writes one kind of line, so
every line is one unit of work. Ignore fields you don't recognise; new ones may
be added. There is no text mode; to read it by hand, pipe it through
`python3 -m json.tool --json-lines` or `jq`.

```json
{"ts": "2026-09-30T23:41:12Z", "event": "version", "orchestrator": "0.1.0", "cm": {"found": true, "version": "0.11.0", "path": "/usr/local/bin/cm"}, "contract": "cm-0.11.x"}
```

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

**Files.** Commands that write files put them in the folder you pass with
`--out`.

## `init`

```
cm-runner init [--repo PATH]
```

Sets up `cm` for one repository, then checks the result. Run it before any scan,
**in the same container as the scan**: it writes `~/.codemender/`, and a fresh
container starts without it.

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
| `CODEMENDER_SANDBOX_NETWORK_PROFILE` | The sandbox's network access. Defaults to `permissive-open`. Only affects `fix` and `verify`; the right default is still to be decided.                                                                                                                                  |

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
cm-runner find-diff [--repo PATH] [--base REF] [--out DIR]
```

Scans the changes in a repository relative to a base Git reference and reports
findings in OASIS SARIF v2.1.0 format. Run [`init`](#init) first in the same
container so `~/.codemender/` is configured.

**Flags:**

| Flag          | Default | Meaning                                                                                         |
| :------------ | :------ | :---------------------------------------------------------------------------------------------- |
| `--repo PATH` | `.`     | Path to the Git working tree to scan.                                                           |
| `--base REF`  | `HEAD`  | Base Git reference to diff against (e.g. `origin/main` or a commit SHA).                        |
| `--out DIR`   | None    | Optional folder where `report.sarif` and `report.md` are written. Created if it does not exist. |

**Pre-flight & shallow clones.** Before starting `cm`, `find-diff` checks that
`--repo` is a valid Git working tree and that `--base` resolves to a commit
object in that repository (`git rev-parse --verify <base>^{commit}`).

> [!IMPORTANT] **CI shallow clones:** Most CI checkout steps default to a
> shallow clone (`fetch-depth: 1`), where base branch references like
> `origin/main` are not present locally. Fetch the base branch (e.g.
> `git fetch origin main`) or check out with `fetch-depth: 0` before running
> `cm-runner find-diff --base <ref>`.

**What it runs.** `find-diff` runs:

1. `cm find <repo> -y --diff=<base> --fail-on NONE`
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

| Code | Meaning                                                                                                                                                                     |
| :--- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `0`  | Scan and report succeeded (`0` or more findings; `--fail-on NONE` ensures findings do not cause a non-zero exit).                                                           |
| `2`  | Invalid flags (`--repo` is not a directory, `--base` is empty/whitespace, or `--out` points to an existing file). Nothing is written to `stdout`.                           |
| `3`  | Execution failed (`--repo` is not a Git working tree, `--base` cannot be resolved, `cm` failed or timed out, or writing to `--out` failed). Nothing is written to `stdout`. |
