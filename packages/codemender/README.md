# `codemender` Package (`cm-runner` CLI Orchestrator)

## Overview

`packages/codemender` provides the scenario-independent `cm-runner` CLI
orchestrator that executes inside the isolated sandbox container:

- **`codemender.runner` (`__main__.py`, `config.py`, `events.py`, `exit_codes.py`, `commands/`):**
  Implements the `cm-runner` subcommands (`version`, `init`, `find-diff`, `fix`,
  `verify`) and merges repository-level `.codemender.yaml` settings into
  `~/.codemender/config.yaml`.
- **`codemender.cm`:** Wraps the bundled `cm` binary (`find`, `fix`, `verify`),
  inheriting the container environment without denylist scrubbing
  ([ADR-0003](../../docs/adr/ADR-0003-environment-scrubbing.md)); CI/VCS tokens
  must be excluded at the outer host/container boundary.
- **`codemender.sarif`:** Validates SARIF v2.1.0 documents and splits
  multi-finding `find-diff` reports into single-finding SARIF files.
- **`codemender.vcs`:** Performs Git working tree pre-flight checks (verifying a
  clean checkout and resolving base/head commit revisions).

## CLI Subcommands (`cm-runner`)

| Subcommand | Purpose | Primary Inputs & Outputs |
| :--- | :--- | :--- |
| `version` | Prints `cm-runner` and `cm` version metadata. | Writes a single JSON `version` event to `stdout`. |
| `init` | Initializes `~/.codemender/config.yaml` and merges `.codemender.yaml`. | Accepts `--repo <path>` and optional `CODEMENDER_BUILD_COMMAND`; streams JSON Lines `check` events to `stdout`. |
| `find-diff` | Scans working-tree changes against `--base <ref>` (default `HEAD`). | Accepts `--repo <path>`, `--base <ref>`, `--diff-workers <1..16>` (`8`), `--out <dir>`; writes SARIF to `stdout` (and `report.sarif` + `report.md` to `--out`). |
| `fix` | Generates and verifies a candidate patch for a single finding. | Accepts `--finding <file>` (or `-` for `stdin`), `--repo <path>`, `--out <dir>`; writes unified diff to `stdout` (and `<stem>.diff` to `--out`). |
| `verify` | Evaluates exploitability of a single finding in the sandbox. | Accepts `--finding <file>` (or `-` for `stdin`), `--repo <path>`, `--out <dir>`; writes verified SARIF to `stdout` (and `<stem>.sarif` to `--out`). |

- **Global flags:** All subcommands accept `--cm-binary <path>` (override the
  `cm` binary path), `--log-level <DEBUG|INFO|WARNING|ERROR>`, and `--no-init`
  (skip automatic `cm-runner init` before `find-diff`, `fix`, or `verify`).
- **Exit codes ([ADR-0002](../../docs/adr/ADR-0002.md)):**
  `0` on success, `2` on CLI usage or input/schema validation errors, and `3` on
  operational failure or when `cm-runner fix` cannot produce a verified patch.
- **From `find-diff` to `fix` / `verify`:** Both `cm-runner fix` and
  `cm-runner verify` require a 1-result SARIF document (`result_count == 1`) and
  a clean Git working tree. Stage `--out` and split finding files **outside**
  `--repo` (e.g., via `SarifDocument.from_json(text).to_findings()` where each
  `finding.document` is a 1-result SARIF dict, or `jq -c '.runs[0].results[] as
  $r | .runs[0].results = [$r]' "$work/report.sarif"`), and run
  `git -C "$REPO" reset --hard && git -C "$REPO" clean -fd` between sequential
  `fix` / `verify` runs because `fix` leaves its changes applied in `--repo`.
- **Container execution & timeouts:** Production runner images set
  `ENTRYPOINT ["cm-runner"]` (see [`../../docker/README.md`](../../docker/README.md)).
  `cm-runner find-diff` defaults to a 30-minute (`1800s`) timeout, while
  `cm-runner fix` and `cm-runner verify` default to a 50-minute (`3000s`)
  timeout and run candidate build/test verification via `build.command` in
  `.codemender.yaml` (or `CODEMENDER_BUILD_COMMAND`).

## Development & Testing

```bash
# Run the full test suite via Makefile (sets PYTHONPATH automatically):
make test

# Or after a one-time editable install in your virtualenv:
pip install -e ./packages/codemender -e ./packages/launcher -e "./packages/repohost[github,gitlab]" pytest setuptools
pytest packages/codemender
```

## Specifications & Architecture Decisions

- [SPEC-FIND-DIFF](../../docs/openspec/specs/find-diff/spec.md)
  — Diff scanning (`cm-runner find-diff`) and SARIF v2.1.0 output contract.
- [SPEC-FIX](../../docs/openspec/specs/fix/spec.md)
  — Single-finding patch generation (`cm-runner fix`).
- [SPEC-VERIFY](../../docs/openspec/specs/verify/spec.md)
  — Single-finding exploitability verification (`cm-runner verify`).
- [ADR-0002](../../docs/adr/ADR-0002.md)
  — Machine-readable `stdout` / `stderr` and exit-code contract.
- [ADR-0003](../../docs/adr/ADR-0003-environment-scrubbing.md)
  — Subprocess environment inheritance in `CmAdapter` and secret isolation at
  the outer host/container boundary.
- [ADR-0008: Repository
  Layout](../../docs/adr/ADR-0008-repository-layout-and-package-structure.md)
  — Three-package workspace layout.
