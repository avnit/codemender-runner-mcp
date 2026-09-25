# Calling cm-runner

How to run the CodeMender runner from CI, Cloud Run, or your own machine, and
what it gives back. The design reasoning is in
[ADR-0001](../adr/ADR-0001-machine-readable-output-contract.md) (output) and
[ADR-0002](../adr/ADR-0002-runner-layout-and-build.md) (layout and build).

> **Status:** Only `version` works today. `init`, `find-diff`, `fix`, and
> `verify` are in progress. Each will be added to this guide when it lands.

## The command

```
cm-runner [--output json|text] [--cm-binary PATH] [--log-level LEVEL] <command> [flags]
```

| Command | What it does | Status |
| :--- | :--- | :--- |
| `version` | Reports the runner's version and the `cm` binary it found. | Available |
| `init` | Checks the environment and fails fast if something is missing. | Planned |
| `find-diff` | Scans a pull request's changes. | Planned |
| `fix` | Produces a patch for one finding. | Planned |
| `verify` | Checks whether a finding is real, or whether a patch fixes it. | Planned |

## Where to run it

The runner ships as a container image:

```
ghcr.io/cloud-gtm/codemender-runner:<commit-sha>
```

Pin a commit tag. Don't use `latest`.

> [!NOTE]
> The container image above is the upstream development reference built by this repository.
>
> **TODO (Workstreams 2 & 3):** Document customer-managed build and deployment pipelines (building from source and pushing to customer private registries such as Google Cloud Artifact Registry, AWS ECR, or private GHCR).

| Where | How |
| :--- | :--- |
| Docker | `docker run --rm ghcr.io/cloud-gtm/codemender-runner:<sha> version` |
| GitHub Actions | Set `container: ghcr.io/cloud-gtm/codemender-runner:<sha>` on the job, then `run: cm-runner version`. |
| Cloud Run | To be defined. |
| From source | `python3 codemender-runner version` (needs Python 3.11+ and the packages in `requirements.txt`). |

Running the image with **no arguments** starts the older pipeline, configured
by `CODEMENDER_RUN_MODE`. Existing Cloud Run jobs depend on this. New
integrations should always name a command.

## What you get back

**stdout: one JSON object per line.** Each command writes one kind of line, so
every line is one unit of work. Ignore fields you don't recognise; new ones may
be added.

```json
{"ts": "2026-09-25T00:43:23Z", "event": "version", "orchestrator": "0.1.0", "cm": {"found": true, "version": "0.9.0", "path": "/usr/local/bin/cm"}, "contract": "cm-0.9.x"}
```

**stderr: logs.** Progress, warnings, errors, and all output from `cm`, `git`,
and other programs. Show it to people; don't parse it.

**Exit code:**

| Code | Meaning |
| :-- | :--- |
| `0` | The command did its job. |
| `2` | Bad command line. |
| `3` | The command failed. The reason is on stderr. |

**Files.** Commands that write files put them in the folder you pass with
`--out`.
