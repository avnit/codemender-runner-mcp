# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Entry point for the CodeMender GitHub Action (M1 scan + M2 parallel fixes).

Flow:
  1. Read the GitHub `pull_request` event (base SHA, head SHA, PR number).
  2. Stage an isolated copy of `$GITHUB_WORKSPACE` and run
     `cm-runner find-diff --repo /workspace --base <base_sha> --out /out
     --diff-workers <N>` (which automatically runs `cm-runner init` beforehand).
  3. Parse the SARIF v2.1.0 output via `SarifDocument` and post or update the
     sticky summary comment on the PR.
  4. When findings exist and `fix` is enabled, deduplicate against earlier PR
     comments, run `cm-runner fix` in parallel isolated containers via
     `launcher.run_parallel(...)`, post each fix as a review suggestion or patch
     comment as it completes, and update the sticky comment's Fix column.

Configuration comes from environment variables set by `action.yml`:
  CODEMENDER_BACKEND             `cloudrun` (default) or `local`.
  CODEMENDER_RUNNER_IMAGE        cm-runner image (pinned SHA tag). Required for local.
  CODEMENDER_LOG_LEVEL           cm-runner --log-level. Default: info.
  CODEMENDER_CONTAINER_RUNTIME   `runsc` (gVisor, default) or `runc`.
  CODEMENDER_DIFF_WORKERS        Number of concurrent diff workers (1..16).
                                 Default: 8.
  CODEMENDER_OUT_DIR             Host path where `report.sarif`, `report.md`,
                                 `findings/*.sarif`, and `fixes/*.diff` are
                                 written for artifact upload. Optional.
  CODEMENDER_FIX                 Enable/disable `cm-runner fix` (`true` default).
  CODEMENDER_MAX_PARALLEL_FIXES  Max concurrent fix containers (`4` default).
  GOOGLE_CLOUD_PROJECT           GCP project ID for Vertex AI / CodeMender API.
  GOOGLE_GHA_CREDS_PATH          Host path to WIF ADC JSON file (or
                                 GOOGLE_APPLICATION_CREDENTIALS).
  GCS_TRANSIT_BUCKET             GCS bucket for Cloud Run transit storage.
  CLOUD_RUN_JOB_NAME             Cloud Run Job name (default: codemender-runner).
  CLOUD_RUN_REGION               Cloud Run region (default: us-central1).
  GITHUB_WORKSPACE               Checked-out repository on the runner host.
  GITHUB_TOKEN                   Token used only here, to post PR comments.
  GITHUB_EVENT_NAME, GITHUB_EVENT_PATH, GITHUB_REPOSITORY,
  GITHUB_SERVER_URL, GITHUB_RUN_ID, RUNNER_TEMP   Standard GitHub Actions vars.
"""

import collections
import contextlib
import json
import logging
import os
import secrets
import shutil
import signal
import sys
import tempfile
import threading
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, TextIO, Tuple
from urllib.parse import quote

# Configure sys.path so 'codemender' and local action subpackages are importable
# in both the monorepo source layout and customer standalone .github/actions/codemender/ layout.
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_ACTIONS_DIR = os.path.dirname(_CURRENT_DIR) if os.path.basename(_CURRENT_DIR) in ("src", "vcs") else _CURRENT_DIR

for _candidate in (
    _CURRENT_DIR,
    os.path.join(_ACTIONS_DIR, "src"),
    os.path.join(_ACTIONS_DIR, "packages", "codemender", "src"),
    os.path.normpath(os.path.join(_ACTIONS_DIR, "../codemender-runner/packages/codemender/src")),
    os.path.normpath(os.path.join(_ACTIONS_DIR, "../../packages/codemender/src")),
    os.path.normpath(os.path.join(_ACTIONS_DIR, "../../../packages/codemender/src")),
    os.path.normpath(os.path.join(_ACTIONS_DIR, "../../../../packages/codemender/src")),
):
  if os.path.isdir(_candidate) and _candidate not in sys.path:
    sys.path.insert(0, _candidate)

from vcs.github import list_finding_comments, post_or_update_sticky_comment
from codemender.sarif import SarifDocument
from codemender.cm import Finding
import fix_comments
from fix_comments import FixCell
# Governing: ADR-0008, SPEC-GHA/REQ-0004
from launcher.base import (
    DEFAULT_MAX_PARALLEL,
    Launcher,
    LauncherError,
    ParallelResult,
    ParallelTask,
    RunResult,
)
from launcher.cloudrun import CloudRunLauncher
from launcher.local import (
    CONTAINER_OUT_DIR,
    CONTAINER_WORKSPACE_DIR,
    RUNTIME_GVISOR,
    LocalDockerLauncher,
    make_world_writable,
)

logger = logging.getLogger("codemender-github-actions")

FIX_TIMEOUT_SECONDS = 35 * 60
DEFAULT_MAX_PARALLEL_FIXES = DEFAULT_MAX_PARALLEL
_PR_EVENTS = ("pull_request",)
_GHA_CREDS_PATTERN = "gha-creds-*.json"
_FIX_OFF_VALUES = ("false", "0", "no")
_EXIT_OK = 0
_EXIT_NO_FIX = 3

# Default and bounds for `cm-runner find-diff --diff-workers`. These mirror
# `cli/commands/find_diff.py` in cm-runner, which this action can't import
# (it ships separately); cm-runner validates the value again.
DEFAULT_DIFF_WORKERS = 8
MIN_DIFF_WORKERS = 1
MAX_DIFF_WORKERS = 16


def _parse_diff_workers(env: Mapping[str, str]) -> int:
  """Parses and validates `CODEMENDER_DIFF_WORKERS` (1..16, default 8)."""
  raw = (env.get("CODEMENDER_DIFF_WORKERS") or "").strip()
  if not raw:
    return DEFAULT_DIFF_WORKERS
  try:
    workers = int(raw)
  except ValueError as e:
    raise ValueError(
        f"Invalid CODEMENDER_DIFF_WORKERS {raw!r}; expected an integer between"
        f" {MIN_DIFF_WORKERS} and {MAX_DIFF_WORKERS}."
    ) from e
  if not (MIN_DIFF_WORKERS <= workers <= MAX_DIFF_WORKERS):
    raise ValueError(
        f"Invalid CODEMENDER_DIFF_WORKERS {workers}; expected an integer"
        f" between {MIN_DIFF_WORKERS} and {MAX_DIFF_WORKERS}."
    )
  return workers


_SARIF_LEVEL_LABELS = {
    "error": "🔴 High / Critical",
    "warning": "🟠 Medium",
    "note": "🟡 Low",
}
_FIX_LABELS = {
    fix_comments.WORKING: "⏳ Working",
    fix_comments.SUGGESTED: "✅ Suggested",
    fix_comments.PATCH: "📎 Patch",
    fix_comments.NO_FIX: "❌ No fix",
    fix_comments.FAILED: "⚠️ Failed",
}
_OUTCOME_LABELS = {
    fix_comments.FIXED: "fixed",
    fix_comments.NO_FIX: "no fix",
    fix_comments.FAILED: "failed",
}


# ---------------------------------------------------------------------------
# Entry points (`main` -> `run_pr` -> `_run_fixes`)
# ---------------------------------------------------------------------------


def create_launcher(
    env: Mapping[str, str],
    *,
    workspace_dir: Optional[str] = None,
    cm_home_dir: Optional[str] = None,
    out_dir: Optional[str] = None,
    creds_file: Optional[str] = None,
) -> Launcher:
  """Launcher factory selecting between LocalDockerLauncher and CloudRunLauncher."""
  backend = (env.get("CODEMENDER_BACKEND") or "cloudrun").strip().lower()
  if backend == "cloud_run":
    backend = "cloudrun"

  if backend == "cloudrun":
    from gcp_sandboxing.client import CloudRunClient, CloudRunConfig  # type: ignore

    project = env.get("GOOGLE_CLOUD_PROJECT", "").strip()
    bucket = (
        env.get("GCS_TRANSIT_BUCKET", "").strip()
        or env.get("CODEMENDER_GCS_TRANSIT_BUCKET", "").strip()
    )
    if not bucket and project:
      bucket = f"codemender-reports-{project}"

    diff_workers = ""
    try:
      diff_workers = str(_parse_diff_workers(env))
    except ValueError:
      pass

    config = CloudRunConfig(
        project=project,
        bucket=bucket,
        job_name=(
            env.get("CLOUD_RUN_JOB_NAME", "").strip()
            or env.get("CODEMENDER_CLOUD_RUN_JOB_NAME", "").strip()
            or "codemender-runner"
        ),
        region=(
            env.get("CLOUD_RUN_REGION", "").strip()
            or env.get("CODEMENDER_GCP_REGION", "").strip()
            or "us-central1"
        ),
        workspace_dir=workspace_dir or env.get("GITHUB_WORKSPACE", "").strip(),
        out_dir=out_dir or env.get("CODEMENDER_OUT_DIR", "").strip(),
        run_id=env.get("GITHUB_RUN_ID", "manual").strip(),
        run_attempt=env.get("GITHUB_RUN_ATTEMPT", "1").strip(),
        diff_workers=diff_workers,
        base_sha=env.get("BASE_SHA", "").strip(),
    )
    client = CloudRunClient(config)
    return CloudRunLauncher(
        client=client,
        log_level=env.get("CODEMENDER_LOG_LEVEL", "info"),
    )

  if backend == "local":
    return LocalDockerLauncher(
        image=env.get("CODEMENDER_RUNNER_IMAGE", ""),
        log_level=env.get("CODEMENDER_LOG_LEVEL", "info"),
        runtime=env.get("CODEMENDER_CONTAINER_RUNTIME") or RUNTIME_GVISOR,
        workspace_dir=workspace_dir or "",
        cm_home_dir=cm_home_dir or "",
        out_dir=out_dir or "",
        gcp_credentials_file=creds_file or "",
        gcp_project=env.get("GOOGLE_CLOUD_PROJECT", ""),
    )

  raise ValueError(f"Unsupported CODEMENDER_BACKEND: {backend!r}.")


def main(env: Mapping[str, str] = os.environ) -> int:
  """CLI entry point invoked by `action.yml`."""
  logging.basicConfig(
      level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr
  )

  event_name = env.get("GITHUB_EVENT_NAME", "")
  if event_name not in _PR_EVENTS:
    print(f"::notice::CodeMender: event {event_name!r} is not handled yet; skipping.")
    return 0

  for required in ("GITHUB_EVENT_PATH", "GITHUB_REPOSITORY", "GITHUB_TOKEN"):
    if not env.get(required):
      print(f"::error::Missing required environment variable {required}.")
      return 1

  backend = (env.get("CODEMENDER_BACKEND") or "cloudrun").strip().lower()
  if backend == "cloud_run":
    backend = "cloudrun"

  if backend == "local":
    for required in (
        "CODEMENDER_RUNNER_IMAGE",
        "GITHUB_WORKSPACE",
        "GOOGLE_CLOUD_PROJECT",
    ):
      if not env.get(required):
        print(f"::error::Missing required environment variable {required}.")
        return 1

    creds_file = (
        env.get("GOOGLE_GHA_CREDS_PATH")
        or env.get("GOOGLE_APPLICATION_CREDENTIALS")
        or ""
    )
    if not creds_file:
      print(
          "::error::Missing GCP credentials file"
          " (GOOGLE_GHA_CREDS_PATH or GOOGLE_APPLICATION_CREDENTIALS)."
      )
      return 1

    src_workspace = env["GITHUB_WORKSPACE"]
    if not os.path.isdir(src_workspace):
      print(
          f"::error::GITHUB_WORKSPACE {src_workspace!r} does not exist or is not"
          " a directory."
      )
      return 1

    with open(env["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
      event = json.load(f)

    staging_root, workspace_copy, cm_home_dir, out_dir = _stage_host_directories(
        src_workspace=src_workspace,
        temp_root=env.get("RUNNER_TEMP"),
        out_dir_override=env.get("CODEMENDER_OUT_DIR"),
    )
    try:
      try:
        _parse_diff_workers(env)
        launcher = create_launcher(
            env,
            workspace_dir=workspace_copy,
            cm_home_dir=cm_home_dir,
            out_dir=out_dir,
            creds_file=creds_file,
        )
      except ValueError as e:
        print(f"::error::{e}")
        return 1
      print(f"::notice::cm-runner container runtime: {launcher.runtime}")
      return run_pr(event, env, launcher, out_dir=out_dir)
    finally:
      shutil.rmtree(staging_root, ignore_errors=True)

  elif backend == "cloudrun":
    # Cloud Run Backend (REQ-0004)
    # Governing: docs/openspec/specs/github-actions/spec.md#req-0004
    try:
      _parse_diff_workers(env)
    except ValueError as e:
      print(f"::error::{e}")
      return 1

    with open(env["GITHUB_EVENT_PATH"], encoding="utf-8") as f:
      event = json.load(f)

    try:
      launcher = create_launcher(env)
    except (ImportError, ValueError) as e:
      print(f"::error::{e}")
      return 1
    return run_pr(event, env, launcher, out_dir=env.get("CODEMENDER_OUT_DIR"))
    return run_pr(event, env, launcher, out_dir=env.get("CODEMENDER_OUT_DIR"))

  else:
    print(f"::error::Unsupported CODEMENDER_BACKEND: {backend!r}.")
    return 1


def run_pr(
    event: Dict[str, Any],
    env: Mapping[str, str],
    launcher: Launcher,
    *,
    out_dir: Optional[str] = None,
) -> int:
  """Runs `find-diff` on a `pull_request` event and orchestrates fixes."""
  pr = event.get("pull_request") or {}
  pr_number = pr.get("number")
  if not pr_number:
    print("::error::Event payload has no pull_request.number.")
    return 1

  base_sha = (
      ((pr.get("base") or {}).get("sha") or "").strip()
      or ((pr.get("base") or {}).get("ref") or "").strip()
  )
  if not base_sha:
    print("::error::Event payload has no pull_request.base.sha.")
    return 1
  head_sha = ((pr.get("head") or {}).get("sha") or "").strip()

  fork = is_fork_pr(event)
  if fork:
    print("::notice::Fork PR: running with the repository's fork-PR settings.")

  backend = (env.get("CODEMENDER_BACKEND") or "cloudrun").strip().lower()
  if backend == "cloud_run":
    backend = "cloudrun"
  image = env.get("CODEMENDER_RUNNER_IMAGE", "") if backend == "local" else ""
  scan_result: Optional[RunResult] = None
  error: Optional[str] = None
  try:
    diff_workers = _parse_diff_workers(env)
  except ValueError as e:
    error = str(e)
    diff_workers = DEFAULT_DIFF_WORKERS

  if error is None:
    with _stop_on_signal(launcher):
      try:
        scan_result = launcher.run(
            "find-diff",
            [
                "--repo",
                CONTAINER_WORKSPACE_DIR,
                "--base",
                base_sha,
                "--out",
                CONTAINER_OUT_DIR,
                "--diff-workers",
                str(diff_workers),
            ],
        )
      except KeyboardInterrupt:
        launcher.stop()
        error = "The run was cancelled or timed out."
        print(
            "::warning::The run was cancelled or timed out. CodeMender stopped"
            " the scan that was still running."
        )
      except LauncherError as e:
        error = str(e)
      except Exception:
        launcher.stop()
        raise

  findings = _parse_scan_findings(scan_result)
  runner_ok = (
      error is None
      and scan_result is not None
      and scan_result.ok
      and findings is not None
  )
  if not runner_ok:
    print(f"::error::cm-runner scan failed: {error or scan_result}")

  def sticky_body(
      fix_cells: Optional[Mapping[int, FixCell]] = None, fix_status: str = ""
  ) -> str:
    return format_find_diff_comment(
        scan_result=scan_result,
        image=image,
        run_url=_run_url(env),
        base_sha=base_sha,
        head_sha=head_sha,
        repo=env.get("GITHUB_REPOSITORY", ""),
        server_url=env.get("GITHUB_SERVER_URL", "https://github.com"),
        error=error,
        fix_cells=(
            None
            if fix_cells is None
            else {index: _fix_cell(cell) for index, cell in fix_cells.items()}
        ),
        fix_status=fix_status,
        backend=backend,
    )

  if runner_ok and findings and fix_enabled(env):
    if backend == "cloudrun":
      print("::notice::Automatic fixes in Cloud Run mode are not yet supported; skipping.")
    elif env.get("GITHUB_WORKSPACE"):
      return _run_fixes(
          findings,
          env,
          launcher,
          pr_number=int(pr_number),
          head_sha=head_sha,
          fork=fork,
          out_dir=out_dir,
          render=sticky_body,
      )
    else:
      print(
          "::warning::GITHUB_WORKSPACE is not set, so CodeMender can't fix the findings."
      )

  if not _post_sticky_comment(env, int(pr_number), sticky_body(), fork):
    return 1
  return 0 if runner_ok else 1


def _run_fixes(
    findings: Sequence[Finding],
    env: Mapping[str, str],
    launcher: Launcher,
    *,
    pr_number: int,
    head_sha: str,
    fork: bool,
    out_dir: Optional[str],
    render: Callable[[Mapping[int, FixCell], str], str],
) -> int:
  """Runs `cm-runner fix` in parallel and posts each fix as it finishes."""
  repo = env.get("GITHUB_REPOSITORY", "")
  token = env.get("GITHUB_TOKEN", "")
  workspace = env["GITHUB_WORKSPACE"]
  temp_root = env.get("RUNNER_TEMP") or None
  if out_dir is None:
    out_dir = tempfile.mkdtemp(prefix="codemender-out-", dir=temp_root)

  _save_findings_artifact(findings, out_dir)

  head_checked_out = (
      bool(head_sha) and fix_comments.checked_out_commit(workspace) == head_sha
  )
  if not head_checked_out:
    print(
        "::warning::The checkout isn't the PR's head commit, so CodeMender"
        " posts its fixes as file comments instead of suggestions. Add"
        " 'ref: ${{ github.event.pull_request.head.sha }}' to the"
        " actions/checkout step."
    )

  earlier = list_finding_comments(repo, pr_number, token)
  if earlier is None:
    print(
        "::warning::Could not read CodeMender's earlier comments on this PR,"
        " so every finding gets a new fix."
    )
    earlier = {}
  plan = fix_comments.plan_fixes(findings, earlier)
  cells: Dict[int, FixCell] = dict(plan.cells)

  if not _post_sticky_comment(env, pr_number, render(cells, _fix_status(cells)), fork):
    return 1
  if not plan.to_fix:
    return 0

  poster = fix_comments.FixPoster(
      repo=repo,
      pr_number=pr_number,
      token=token,
      head_sha=head_sha,
      repo_dir=workspace,
      headers={finding.index: _fix_header(finding) for finding in plan.to_fix},
      fingerprints=plan.fingerprints,
      artifacts_url=f"{_run_url(env)}#artifacts",
      head_checked_out=head_checked_out,
  )
  max_workers = max_parallel_fixes(env)
  noun = "finding" if len(plan.to_fix) == 1 else "findings"
  print(
      f"::notice::CodeMender is fixing {len(plan.to_fix)} {noun}"
      f" (up to {min(len(plan.to_fix), max_workers)} in parallel)."
  )

  tasks = [
      ParallelTask(
          key=finding,
          command="fix",
          args=[
              "--repo",
              CONTAINER_WORKSPACE_DIR,
              "--finding",
              f"{CONTAINER_OUT_DIR}/{finding.name}.sarif",
              "--out",
              CONTAINER_OUT_DIR,
          ],
          input_files={
              f"{finding.name}.sarif": (
                  json.dumps(finding.document, indent=2) + "\n"
              ).encode("utf-8")
          },
          output_file=f"{finding.name}.diff",
      )
      for finding in plan.to_fix
  ]
  fixes_dir = os.path.join(out_dir, "fixes")

  def update_sticky(stopped: bool = False) -> bool:
    body = render(cells, _fix_status(cells, stopped))
    return post_or_update_sticky_comment(token, repo, pr_number, body) is not None

  stopped = False
  with _stop_on_signal(launcher):
    try:
      with contextlib.closing(
          launcher.run_parallel(
              tasks,
              src_workspace=workspace,
              temp_root=temp_root,
              timeout_seconds=FIX_TIMEOUT_SECONDS,
              max_workers=max_workers,
          )
      ) as results:
        for res in results:
          finding = res.key
          outcome, patch, error = _classify_fix_result(finding, res, fixes_dir)
          _print_fix_log(finding, outcome, error, res.log)
          try:
            cell = poster.post(finding, outcome, patch)
          except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("Could not post fix for finding %d.", finding.index)
            cell = FixCell(fix_comments.FAILED)
          for index in plan.shared[finding.index]:
            cells[index] = cell
          if any(c.status == fix_comments.WORKING for c in cells.values()):
            update_sticky()
    except KeyboardInterrupt:
      stopped = True
      print(
          "::warning::The run was cancelled or timed out. CodeMender stopped"
          " the fixes that were still running."
      )
      for index, cell in cells.items():
        if cell.status == fix_comments.WORKING:
          cells[index] = FixCell(fix_comments.FAILED)
    final_ok = update_sticky(stopped)

  if not final_ok:
    print("::error::Could not update the CodeMender comment with the fix results.")
    return 1
  return 1 if stopped else 0


# ---------------------------------------------------------------------------
# Sticky PR comment formatting
# ---------------------------------------------------------------------------


def format_find_diff_comment(
    scan_result: Optional[RunResult],
    image: str,
    run_url: str,
    base_sha: str = "",
    head_sha: str = "",
    repo: str = "",
    server_url: str = "https://github.com",
    error: Optional[str] = None,
    fix_cells: Optional[Mapping[int, str]] = None,
    fix_status: str = "",
    backend: str = "local",
    init_result: Optional[RunResult] = None,
) -> str:
  """Builds the sticky PR comment body for a `cm-runner find-diff` run."""
  lines = ["### CodeMender Security Scan", ""]
  short_base = base_sha[:12] if base_sha else "HEAD"
  findings = _parse_scan_findings(scan_result) if error is None else None

  if error is None and findings is not None:
    count = len(findings)
    if count == 0:
      lines.append(
          "✅ **No vulnerabilities found** in the changes for this pull request."
      )
    else:
      noun = "vulnerability" if count == 1 else "vulnerabilities"
      lines.append(
          f"⚠️ **Found {count} {noun}** in the changes for this pull request."
      )
      if fix_status:
        lines += ["", fix_status]
      fix_header = " Fix |" if fix_cells is not None else ""
      fix_align = " :--- |" if fix_cells is not None else ""
      lines += [
          "",
          f"| # | Severity | Rule | Location | Finding |{fix_header}",
          f"| :--- | :--- | :--- | :--- | :--- |{fix_align}",
      ]
      detail_blocks: List[str] = []
      for f in findings:
        rule_id = f.rule_id or "unknown"
        level = (f.level or "warning").lower()
        sev_label = _SARIF_LEVEL_LABELS.get(level, f"⚪ {level}")
        title, analysis = _split_title_and_analysis(f.message)
        loc_md = _format_finding_location(f, server_url, repo, head_sha)
        fix_md = (
            f" {fix_cells.get(f.index, '—')} |" if fix_cells is not None else ""
        )

        lines.append(
            f"| {f.index} | {sev_label} | {_inline(rule_id, 60)} | {loc_md} |"
            f" {_inline(title, 120)} |{fix_md}"
        )

        detail_lines = [
            f"#### {f.index}. {_inline(rule_id, 60)} — {_inline(title, 120)}",
            f"- **Severity:** {sev_label} ({_inline(level, 20)})",
            f"- **Location:** {loc_md}",
        ]
        if analysis:
          detail_lines.append(f"- **Details:** {_sanitize_block_text(analysis)}")
        detail_blocks.append("\n".join(detail_lines))

      lines += [
          "",
          "<details>",
          f"<summary><strong>Finding details ({count})</strong></summary>",
          "",
          "\n\n".join(detail_blocks),
          "",
          "</details>",
      ]
    lines += [
        "",
        f"Scanned diff against base {_inline(short_base, 40)}"
        f" · [Workflow run logs]({run_url})",
    ]
    return "\n".join(lines)

  lines.append("❌ **CodeMender scan failed.**")
  lines.append("")
  if error:
    lines.append(f"Reason: {_inline(error)}")
  elif scan_result is not None and not scan_result.ok:
    lines.append(
        f"`cm-runner find-diff` exited with code {_inline(scan_result.exit_code)}."
    )
  else:
    lines.append(
        "`cm-runner find-diff` did not emit a valid SARIF 2.1.0 document."
    )
  if image:
    lines.append(f"Image: {_inline(image)}")
  else:
    backend_name = backend.capitalize() if backend else "Cloud Run"
    lines.append(f"Backend: {_inline(backend_name)}")
  lines += ["", f"[Workflow run logs]({run_url})"]
  return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_scan_findings(scan_result: Optional[RunResult]) -> Optional[List[Finding]]:
  """Parses `scan_result.stdout` into `Finding` objects, or None if invalid SARIF."""
  if scan_result is None or not scan_result.ok:
    return None
  raw_bytes = scan_result.stdout or b""
  if not raw_bytes and scan_result.events:
    raw_bytes = json.dumps(scan_result.events[0]).encode("utf-8")
  if not (raw_bytes or b"").strip():
    return None
  try:
    text = (
        scan_result.stdout.decode("utf-8")
        if isinstance(scan_result.stdout, bytes)
        else scan_result.stdout
    )
    raw = json.loads(text)
    if not isinstance(raw, dict) or not isinstance(raw.get("runs"), list):
      return None
    for run_entry in raw["runs"]:
      if not isinstance(run_entry, dict) or not isinstance(run_entry.get("results"), list):
        return None
    return SarifDocument(data=raw).to_findings()
  except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
    return None


def _classify_fix_result(
    finding: Finding, res: ParallelResult, fixes_dir: str
) -> Tuple[str, bytes, str]:
  """Classifies a `ParallelResult` into `FIXED`, `NO_FIX`, or `FAILED`."""
  if res.exit_code == _EXIT_OK and res.output.strip():
    try:
      os.makedirs(fixes_dir, exist_ok=True)
      out_patch = os.path.join(fixes_dir, f"{finding.name}.diff")
      if not os.path.islink(out_patch):
        with open(out_patch, "wb") as f:
          f.write(res.output)
    except OSError as e:
      logger.warning("Could not save patch for %s: %s", finding.name, e)
    return fix_comments.FIXED, res.output, ""

  if res.exit_code == _EXIT_NO_FIX:
    return fix_comments.NO_FIX, b"", "cm-runner produced no patch (exit code 3)."
  return (
      fix_comments.FAILED,
      b"",
      res.error or f"cm-runner exited with code {res.exit_code}.",
  )


def _save_findings_artifact(findings: Sequence[Finding], out_dir: str) -> None:
  """Writes `findings/finding-NNN.sarif` files under `out_dir` for artifact upload."""
  try:
    findings_dir = os.path.join(out_dir, "findings")
    os.makedirs(findings_dir, exist_ok=True)
    for f in findings:
      if f.document:
        target = os.path.join(findings_dir, f"{f.name}.sarif")
        if not os.path.islink(target):
          with open(target, "w", encoding="utf-8") as fh:
            json.dump(f.document, fh, indent=2)
            fh.write("\n")
  except OSError as e:
    print(f"::warning::Could not save the findings for the artifact upload: {e}")


def _print_fix_log(
    finding: Finding,
    outcome: str,
    error: str,
    log_text: str,
    stream: Optional[TextIO] = None,
) -> None:
  """Prints one fix's container stderr log inside a collapsed GitHub Actions group."""
  stream = stream or sys.stdout
  where = (
      f"{finding.path}:{finding.start_line}"
      if (finding.path and finding.start_line)
      else (finding.path or "no location")
  )
  desc = f"finding {finding.index} ({where})"
  title = f"CodeMender fix for {desc}: {_OUTCOME_LABELS[outcome]}"
  token = secrets.token_hex(16)
  esc = lambda s: s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

  stream.write(f"::group::{esc(title)}\n")
  stream.write(f"::stop-commands::{token}\n")
  if error:
    stream.write(f"{error}\n")
  if log_text:
    stream.write(log_text)
    if not log_text.endswith("\n"):
      stream.write("\n")
  stream.write(f"::{token}::\n")
  stream.write("::endgroup::\n")
  if outcome == fix_comments.FAILED:
    msg = f"CodeMender could not fix {desc}: {error} See the log group above."
    stream.write(f"::warning::{esc(msg)}\n")
  stream.flush()


@contextlib.contextmanager
def _stop_on_signal(launcher: Launcher) -> Iterator[None]:
  """Stops running containers or remote jobs on SIGINT/SIGTERM and raises KeyboardInterrupt once."""
  if threading.current_thread() is not threading.main_thread():
    yield
    return
  signalled: List[int] = []

  def handle(signum: int, frame: Any) -> None:
    del frame
    launcher.stop()
    if not signalled:
      signalled.append(signum)
      raise KeyboardInterrupt

  previous = {
      sig: signal.signal(sig, handle) for sig in (signal.SIGINT, signal.SIGTERM)
  }
  try:
    yield
  finally:
    for sig, handler in previous.items():
      if handler is not None:
        signal.signal(sig, handler)


_stop_fixes_on_signal = _stop_on_signal


def _stage_host_directories(
    src_workspace: str,
    temp_root: Optional[str],
    out_dir_override: Optional[str],
) -> Tuple[str, str, str, str]:
  staging_root = tempfile.mkdtemp(
      prefix="codemender-", dir=temp_root if temp_root else None
  )
  workspace_copy = os.path.join(staging_root, "workspace")
  shutil.copytree(
      src_workspace,
      workspace_copy,
      symlinks=True,
      ignore=shutil.ignore_patterns(_GHA_CREDS_PATTERN),
  )
  make_world_writable(workspace_copy)
  cm_home_dir = os.path.join(staging_root, "cm-home")
  os.makedirs(cm_home_dir, exist_ok=True)
  out_dir = out_dir_override or os.path.join(staging_root, "out")
  os.makedirs(out_dir, exist_ok=True)
  return staging_root, workspace_copy, cm_home_dir, out_dir


def _post_sticky_comment(
    env: Mapping[str, str], pr_number: int, body: str, fork: bool
) -> bool:
  comment_url = post_or_update_sticky_comment(
      env.get("GITHUB_TOKEN", ""),
      env.get("GITHUB_REPOSITORY", ""),
      pr_number,
      body,
  )
  if comment_url is None:
    print("::error::Could not post the CodeMender comment to the pull request.")
    if fork:
      print(
          "::error::This is a fork PR. Ask a repository or organization admin"
          " to enable 'Send write tokens to workflows from pull requests'"
          " (Settings > Actions > General > Fork pull request workflows)."
      )
    return False
  return True


def is_fork_pr(event: Dict[str, Any]) -> bool:
  pr = event.get("pull_request") or {}
  head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
  base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
  return bool(head_repo) and head_repo != base_repo


def fix_enabled(env: Mapping[str, str]) -> bool:
  return env.get("CODEMENDER_FIX", "").strip().lower() not in _FIX_OFF_VALUES


def max_parallel_fixes(env: Mapping[str, str]) -> int:
  raw = env.get("CODEMENDER_MAX_PARALLEL_FIXES", "").strip()
  if not raw:
    return DEFAULT_MAX_PARALLEL_FIXES
  try:
    value = int(raw)
  except ValueError:
    value = 0
  if value < 1:
    print(
        f"::warning::Invalid max_parallel_fixes {raw!r}; using default"
        f" {DEFAULT_MAX_PARALLEL_FIXES}."
    )
    return DEFAULT_MAX_PARALLEL_FIXES
  return value


def _fix_cell(cell: FixCell) -> str:
  label = _FIX_LABELS[cell.status]
  return f"[{label}]({cell.url})" if cell.url else label


def _fix_status(cells: Mapping[int, FixCell], stopped: bool = False) -> str:
  counts = collections.Counter(cell.status for cell in cells.values())
  working = counts[fix_comments.WORKING]
  if working and not stopped:
    noun = "finding" if working == 1 else "findings"
    return (
        f"⏳ Generating fixes for {working} {noun}. This usually takes 10"
        " minutes or more."
    )
  parts = []
  if counts[fix_comments.SUGGESTED]:
    parts.append(f"✅ {counts[fix_comments.SUGGESTED]} suggested")
  if counts[fix_comments.PATCH]:
    patches = counts[fix_comments.PATCH]
    parts.append(f"📎 {patches} {'patch' if patches == 1 else 'patches'}")
  if counts[fix_comments.NO_FIX]:
    parts.append(f"❌ {counts[fix_comments.NO_FIX]} no fix")
  if counts[fix_comments.FAILED]:
    parts.append(f"⚠️ {counts[fix_comments.FAILED]} failed")
  line = f"**Fixes:** {' · '.join(parts)}."
  if counts[fix_comments.SUGGESTED] or counts[fix_comments.PATCH]:
    line += " Open a fix from the Fix column."
  if stopped:
    line += " The run was cancelled or timed out before every fix finished."
  return line


def _fix_header(finding: Finding) -> str:
  severity = (
      _SARIF_LEVEL_LABELS.get(finding.level)
      or f"⚪ {_code_span(finding.level, 20)}"
  )
  title, _ = _split_title_and_analysis(finding.message)
  return (
      f"**CodeMender fix for finding {finding.index}:** {severity}"
      f" · {_code_span(finding.rule_id or 'unknown', 60)}"
      f" · {_code_span(title, 120)}"
  )


def _format_finding_location(
    finding: Finding, server_url: str, repo: str, head_sha: str
) -> str:
  uri = (finding.file_path or "").strip().lstrip("/")
  if not uri:
    return "—"
  label = uri
  anchor = ""
  if finding.start_line > 0:
    if finding.end_line > finding.start_line:
      label = f"{uri}:{finding.start_line}-{finding.end_line}"
      anchor = f"#L{finding.start_line}-L{finding.end_line}"
    else:
      label = f"{uri}:{finding.start_line}"
      anchor = f"#L{finding.start_line}"
  safe_label = _inline(label)
  if repo and head_sha:
    encoded_path = "/".join(quote(part, safe="") for part in uri.split("/"))
    url = f"{server_url.rstrip('/')}/{repo}/blob/{quote(head_sha, safe='')}/{encoded_path}{anchor}"
    return f"[{safe_label}]({url})"
  return safe_label


def _split_title_and_analysis(message_text: str) -> Tuple[str, str]:
  text = (message_text or "").strip()
  if not text:
    return ("Vulnerability finding", "")
  first_line, _, rest = text.partition("\n")
  if ": " in first_line:
    prefix, after = first_line.split(": ", 1)
    if 0 < len(prefix) <= 120:
      analysis = f"{after}\n{rest}".strip() if rest else after.strip()
      return (prefix.strip(), analysis)
  return (first_line[:120].strip(), text)


def _inline(value: Any, max_len: int = 200) -> str:
  text = str(value).replace("`", "'").replace("\n", " ").replace("\r", " ")
  text = text.replace("|", "\\|")
  return f"`{text[:max_len]}`"


def _code_span(value: Any, max_len: int = 200) -> str:
  text = str(value).replace("`", "'").replace("\n", " ").replace("\r", " ")
  return f"`{text[:max_len]}`"


def _sanitize_block_text(value: Any, max_len: int = 2000) -> str:
  text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
  text = text.replace("```", "'''").replace("~~~", "'''")
  text = text.replace("<", "&lt;").replace(">", "&gt;")
  return text[:max_len] + "…" if len(text) > max_len else text


def _run_url(env: Mapping[str, str]) -> str:
  server = env.get("GITHUB_SERVER_URL", "https://github.com")
  repo = env.get("GITHUB_REPOSITORY", "")
  run_id = env.get("GITHUB_RUN_ID", "")
  return f"{server}/{repo}/actions/runs/{run_id}"


def format_version_comment(
    result: Optional[RunResult],
    image: str,
    run_url: str,
    error: Optional[str] = None,
    backend: str = "local",
) -> str:
  """Builds the PR comment body for the M0 `cm-runner version` check."""
  lines = ["### CodeMender", ""]
  version = result.find_event("version") if result else None

  backend_name = backend.capitalize() if backend else "Cloud Run"
  target_row = (
      f"| Image | {_inline(image)} |"
      if image
      else f"| Backend | {_inline(backend_name)} |"
  )

  if result is not None and result.ok and version:
    cm = version.get("cm") or {}
    cm_ok = bool(cm.get("found"))
    lines.append(
        "✅ cm-runner is reachable."
        if cm_ok
        else "⚠️ cm-runner is reachable, but the `cm` binary was not found."
    )
    lines += [
        "",
        "| | |",
        "| :--- | :--- |",
        f"| cm-runner | {_inline(version.get('orchestrator', 'unknown'))} |",
        f"| cm | {_inline(cm.get('version', 'not found'))} |",
        f"| Contract | {_inline(version.get('contract', 'unknown'))} |",
        target_row,
    ]
  else:
    lines.append("❌ cm-runner check failed.")
    lines.append("")
    if error:
      lines.append(f"Reason: {_inline(error)}")
    elif result is not None and not result.ok:
      lines.append(f"cm-runner exited with code {_inline(result.exit_code)}.")
    else:
      lines.append("cm-runner did not emit a `version` event.")
    if image:
      lines.append(f"Image: {_inline(image)}")
    else:
      lines.append(f"Backend: {_inline(backend_name)}")

  lines += ["", f"[Workflow run logs]({run_url})"]
  return "\n".join(lines)


if __name__ == "__main__":
  sys.exit(main())
