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

# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0003
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0004
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0007
# Governing: ADR-0008,
# SPEC-GHA/REQ-0004, SPEC-GHA/REQ-0005, SPEC-GHA/REQ-0006, SPEC-GHA/REQ-0008,
# SPEC-GHA-FIX/REQ-0001, SPEC-GHA-VERIFY

"""Entry point for the CodeMender GitHub Action (M1 scan + M2 parallel fixes).

Governing: ADR-0008, docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0003, docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0004, docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0007.

Flow:
  1. Read the GitHub `pull_request` event (base SHA, head SHA, PR number).
  2. Stage an isolated copy of `$GITHUB_WORKSPACE` and run
     `cm-runner find-diff --repo /workspace --base <base_sha> --out /out
     --diff-workers <N>` (which automatically runs `cm-runner init` beforehand).
  3. Parse the SARIF v2.1.0 output via `SarifDocument`, delete earlier
     bot-authored CodeMender review comments on the PR, and post or update the
     sticky summary comment on the PR.
  4. When findings exist and `fix` is enabled, deduplicate identical finding
     fingerprints within the current run, run `cm-runner fix` in parallel
     isolated containers via `launcher.run_parallel(...)`, post each fix as a
     review suggestion or patch comment as it completes, and update the sticky
     comment's Fix column.

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
import datetime
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, TextIO, Tuple
from urllib.parse import quote

from codemender.cm import Finding
from codemender.sarif import SarifDocument
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
from repohost.base import RepoHost
from repohost.github import (
    delete_codemender_review_comments,
    fix_comments,
)
from repohost.github.client import (
    FINDING_TAG_RE,
    VerifyComment,
    get_review_comment,
    get_sticky_comment_body,
    has_other_active_verify_runs,
    is_bot_comment,
    list_verify_comments,
    reply_to_comment,
    update_review_comment,
    verify_tag,
)
from repohost.github.fix_comments import FixCell
from repohost.github.repo_host import GitHubRepoHost
from repohost.github.store import GitHubActionsArtifactStore
from repohost.models import (
    ChangeContext,
    Comment,
    RepoHostError,
    SecurityGate,
    SecurityGateState,
    SuggestedEdit,
)
from repohost.store import ArtifactStore, ArtifactStoreError

logger = logging.getLogger("codemender-github-actions")

# Outer per-fix container timeout (55m). Must sit between cm-runner's inner
# `_FIX_TIMEOUT_SEC` (50m / 3000s in `codemender/runner/commands/fix.py`) and
# the outer Cloud Run Job / workflow timeout (`runner_timeout = "3600s"` / 60m)
# so `cm-runner` can time out cleanly before the container or Cloud Run task is
# terminated.
FIX_TIMEOUT_SECONDS = 55 * 60
DEFAULT_MAX_PARALLEL_FIXES = DEFAULT_MAX_PARALLEL
MAX_CONCURRENT_VERIFIES = 4
VERIFY_LOCK_TTL_SECONDS = 45 * 60
VERIFY_COMMAND = "@cm-runner verify"
ALLOWED_VERIFY_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
_PR_EVENTS = ("pull_request", "pull_request_review_comment")
_GHA_CREDS_PATTERN = "gha-creds-*.json"
_FIX_OFF_VALUES = ("false", "0", "no")
_EXIT_OK = 0
_EXIT_NO_FIX = 3
_VERIFY_CMD_RE = re.compile(
    r"(?i)(?<!\w)@(?:cm-runner|codemender(?:-runner)?)\s+verify(?!\w)"
)
_FENCED_BLOCK_RE = re.compile(r"(?ms)^[ \t]{0,3}(`{3,}|~{3,}).*?^[ \t]{0,3}\1")
_INLINE_CODE_RE = re.compile(r"`+[^`]*`+")
_PARENT_HEADER_RE = re.compile(
    r"\*\*CodeMender fix for finding (\d+):\*\*\s+(.+?)\s+·\s+`([^`]+)`\s+·\s+`([^`]+)`"
)

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
# Entry points (`main` -> `run_preflight` / `run_pr` / `run_verify`)
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
    from launcher.cloudrun.client import CloudRunClient, CloudRunConfig  # pylint: disable=g-import-not-at-top

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


def create_repo_host(
    env: Mapping[str, str],
    event: Optional[Mapping[str, Any]] = None,
) -> GitHubRepoHost:
  """Creates a GitHubRepoHost instance from environment and event."""
  return GitHubRepoHost(env=env, event=event)


def create_artifact_store(
    env: Mapping[str, str],
    event: Optional[Mapping[str, Any]] = None,
    *,
    repo: Optional[str] = None,
    pr_number: Optional[int] = None,
) -> ArtifactStore:
  """Creates an ArtifactStore instance from environment and event."""
  resolved_repo = repo or env.get("GITHUB_REPOSITORY", "")
  if not resolved_repo and event:
    resolved_repo = str(event.get("repository", {}).get("full_name") or "")

  resolved_pr = pr_number
  if resolved_pr is None and event:
    pr_data = event.get("pull_request")
    if isinstance(pr_data, dict) and pr_data.get("number"):
      try:
        resolved_pr = int(pr_data["number"])
      except (ValueError, TypeError):
        pass
    if not resolved_pr and event.get("issue", {}).get("number"):
      try:
        resolved_pr = int(event["issue"]["number"])
      except (ValueError, TypeError):
        pass

  token = env.get("GITHUB_TOKEN", "")
  server_url = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
  api_url = env.get("GITHUB_API_URL") or (
      "https://api.github.com"
      if server_url == "https://github.com"
      else f"{server_url}/api/v3"
  )
  return GitHubActionsArtifactStore(
      repo=resolved_repo,
      pr_number=resolved_pr,
      token=token,
      api_url=api_url,
  )


def main(
    env: Mapping[str, str] = os.environ,
    argv: Optional[Sequence[str]] = None,
    host: Optional[RepoHost] = None,
    store: Optional[ArtifactStore] = None,
) -> int:
  """CLI entry point invoked by `action.yml`."""
  logging.basicConfig(
      level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr
  )
  args = sys.argv[1:] if argv is None else list(argv)
  if "--preflight" in args or env.get("CODEMENDER_PREFLIGHT") == "1":
    return run_preflight(env, host=host)

  creds_path = (
      env.get("GOOGLE_GHA_CREDS_PATH")
      or env.get("GOOGLE_APPLICATION_CREDENTIALS")
      or ""
  ).strip()
  if creds_path and os.path.isfile(creds_path):
    try:
      os.chmod(creds_path, 0o644)
    except OSError:
      pass

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

    if host is None:
      host = create_repo_host(env, event=event)
    if store is None:
      store = create_artifact_store(env, event=event)

    staging_root, workspace_copy, cm_home_dir, out_dir = (
        _stage_host_directories(
            src_workspace=src_workspace,
            temp_root=env.get("RUNNER_TEMP"),
            out_dir_override=env.get("CODEMENDER_OUT_DIR"),
        )
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
      if event_name == "pull_request_review_comment":
        return run_verify(
            event,
            env,
            launcher,
            out_dir=out_dir,
            host=host,
            store=store,
        )
      return run_pr(event, env, launcher, out_dir=out_dir, host=host)
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

    if host is None:
      host = create_repo_host(env, event=event)
    if store is None:
      store = create_artifact_store(env, event=event)

    try:
      launcher = create_launcher(env)
    except (ImportError, ValueError) as e:
      print(f"::error::{e}")
      return 1
    if event_name == "pull_request_review_comment":
      return run_verify(
          event,
          env,
          launcher,
          out_dir=env.get("CODEMENDER_OUT_DIR"),
          host=host,
          store=store,
      )
    return run_pr(
        event, env, launcher, out_dir=env.get("CODEMENDER_OUT_DIR"), host=host
    )

  else:
    print(f"::error::Unsupported CODEMENDER_BACKEND: {backend!r}.")
    return 1


def run_pr(
    event: Dict[str, Any],
    env: Mapping[str, str],
    launcher: Launcher,
    *,
    host: RepoHost,
    out_dir: Optional[str] = None,
) -> int:
  """Runs `find-diff` on a `pull_request` event and orchestrates fixes."""
  pr = event.get("pull_request") or {}
  pr_number = pr.get("number")
  base_sha = (
      ((pr.get("base") or {}).get("sha") or "").strip()
      or ((pr.get("base") or {}).get("ref") or "").strip()
  )
  head_sha = ((pr.get("head") or {}).get("sha") or "").strip()
  fork = is_fork_pr(event)

  try:
    context = host.get_change_context()
    if context.change_id:
      pr_number = int(context.change_id)
    if context.base_sha:
      base_sha = context.base_sha
    if context.head_sha:
      head_sha = context.head_sha
    if context.is_fork is not None:
      fork = context.is_fork
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.debug("host.get_change_context fallback: %s", e)

  if not pr_number:
    print("::error::Event payload has no pull_request.number.")
    return 1

  if not base_sha:
    print("::error::Event payload has no pull_request.base.sha.")
    return 1

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

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0009, SPEC-GHA-FIX
  if (
      delete_codemender_review_comments(
          env.get("GITHUB_REPOSITORY", ""),
          int(pr_number),
          env.get("GITHUB_TOKEN", ""),
      )
      is None
  ):
    print(
        "::warning::Could not delete CodeMender's earlier review comments on"
        " this PR."
    )

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0001, SPEC-GHA-FIX
  if runner_ok and findings and fix_enabled(env):
    if env.get("GITHUB_WORKSPACE"):
      return _run_fixes(
          findings,
          env,
          launcher,
          pr_number=int(pr_number),
          head_sha=head_sha,
          fork=fork,
          out_dir=out_dir,
          render=sticky_body,
          host=host,
      )
    else:
      print(
          "::warning::GITHUB_WORKSPACE is not set, so CodeMender can't fix"
          " the findings."
      )

  if not _post_sticky_comment(
      env, int(pr_number), sticky_body(), fork, host=host
  ):
    return 1

  try:
    if runner_ok and not findings:
      gate = SecurityGate(
          state=SecurityGateState.SUCCESS,
          description="CodeMender: No security findings detected",
      )
    elif runner_ok and findings:
      gate = SecurityGate(
          state=SecurityGateState.FAILURE,
          description=(
              f"CodeMender: {len(findings)} security finding(s) detected"
          ),
      )
    else:
      gate = SecurityGate(
          state=SecurityGateState.ERROR,
          description="CodeMender: Scan execution failed or timed out",
      )
    host.set_security_gate(gate)
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.warning("Could not update security gate: %s", e)

  return 0 if runner_ok else 1


def _set_security_gate_failure(
    host: RepoHost,
    findings: Sequence[Finding],
    *,
    stopped: bool = False,
) -> None:
  """Publishes a FAILURE security gate check for detected findings."""
  try:
    gate = SecurityGate(
        state=SecurityGateState.FAILURE,
        description=(
            f"CodeMender: {len(findings)} security finding(s) detected"
            if not stopped
            else "CodeMender: Remediation run interrupted"
        ),
    )
    host.set_security_gate(gate)
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.warning("Could not update security gate: %s", e)


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
    host: RepoHost,
) -> int:
  """Runs `cm-runner fix` in parallel and posts each fix as it finishes."""
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

  plan = fix_comments.plan_fixes(findings)
  cells: Dict[int, FixCell] = dict(plan.cells)

  if not _post_sticky_comment(
      env, pr_number, render(cells, _fix_status(cells)), fork, host=host
  ):
    return 1
  if not plan.to_fix:
    _set_security_gate_failure(host, findings)
    return 0

  poster = fix_comments.FixPoster(
      host=host,
      repo_dir=workspace,
      headers={finding.index: _fix_header(finding) for finding in plan.to_fix},
      fingerprints=plan.fingerprints,
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
    try:
      c = host.upsert_summary(body)
      return bool(c and (c.comment_id or c.url))
    except Exception as e:  # pylint: disable=broad-exception-caught
      logger.warning("host.upsert_summary update failed: %s", e)
      return False

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

  _set_security_gate_failure(host, findings, stopped=stopped)

  if not final_ok:
    print(
        "::error::Could not update the CodeMender comment with the fix results."
    )
    return 1
  return 1 if stopped else 0


# ---------------------------------------------------------------------------
# On-demand PR verification (`run_preflight` + `run_verify`)
# ---------------------------------------------------------------------------


def _is_valid_verify_request(event: Dict[str, Any]) -> bool:
  """Returns True if `event` is a human collaborator's `@cm-runner verify` reply on a same-repo PR."""
  comment = event.get("comment") or {}
  pr = event.get("pull_request") or {}
  assoc = str(comment.get("author_association") or "").upper()
  return (
      event.get("action") == "created"
      and not is_bot_comment(comment)
      and not is_fork_pr(event)
      and int(pr.get("number") or 0) > 0
      and int(comment.get("id") or 0) > 0
      and int(comment.get("in_reply_to_id") or 0) > 0
      and assoc in ALLOWED_VERIFY_ASSOCIATIONS
      and _has_verify_command(str(comment.get("body") or ""))
  )


def _extract_parent_finding_id(
    parent: Optional[Mapping[str, Any]],
) -> Optional[str]:
  """Returns the finding_id if `parent` is a bot-authored CodeMender fix comment."""
  if not is_bot_comment(parent):
    return None
  match = FINDING_TAG_RE.search(str((parent or {}).get("body") or ""))
  return match.group(1) if match else None


def _verify_pr_base_commit(env: Mapping[str, str]) -> bool:
  """Verifies that the PR base commit is present in the local git clone.

  Attempts a shallow fetch of the base commit if absent. Emits an error notice
  and returns False if the base commit cannot be found or fetched.
  """
  base_sha = env.get("BASE_SHA", "").strip()
  if not base_sha:
    event_path = env.get("GITHUB_EVENT_PATH", "")
    if event_path and os.path.isfile(event_path):
      try:
        with open(event_path, encoding="utf-8") as f:
          event = json.load(f)
          base_sha = str(
              ((event.get("pull_request") or {}).get("base") or {}).get("sha")
              or ""
          ).strip()
      except Exception:  # pylint: disable=broad-exception-caught
        pass

  if not base_sha:
    return True

  try:
    cat_res = subprocess.run(
        ["git", "cat-file", "-e", f"{base_sha}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if cat_res.returncode == 0:
      return True
  except Exception:  # pylint: disable=broad-exception-caught
    return True

  print(
      f"Base commit {base_sha} not in shallow clone; attempting git fetch...",
      file=sys.stderr,
  )
  try:
    fetch_res = subprocess.run(
        ["git", "fetch", "--no-tags", "--depth=1", "origin", base_sha],
        capture_output=True,
        check=False,
    )
    if fetch_res.returncode == 0:
      return True
  except Exception:  # pylint: disable=broad-exception-caught
    pass

  print(
      f"::error::Base commit {base_sha} is not present in the local checkout."
      " Set 'fetch-depth: 0' on actions/checkout."
  )
  return False


def run_preflight(
    env: Mapping[str, str],
    host: Optional[RepoHost] = None,
) -> int:
  """Fast pre-flight gate executed before WIF auth, gVisor, and Docker pull."""
  if host is None:
    host = create_repo_host(env)

  event_name = env.get("GITHUB_EVENT_NAME", "")
  if event_name == "pull_request":
    if not _verify_pr_base_commit(env):
      _write_github_output(env, host=host, should_run="false")
      return 1
    _write_github_output(env, host=host, should_run="true")
    return 0
  if event_name != "pull_request_review_comment":
    _write_github_output(env, host=host, should_run="false")
    return 0

  event_path = env.get("GITHUB_EVENT_PATH", "")
  repo = env.get("GITHUB_REPOSITORY", "")
  token = env.get("GITHUB_TOKEN", "")
  if not (event_path and os.path.isfile(event_path) and repo and token):
    _write_github_output(env, host=host, should_run="false")
    return 0

  with open(event_path, encoding="utf-8") as f:
    event = json.load(f)

  comment = event.get("comment") or {}
  pr = event.get("pull_request") or {}
  pr_number = int(pr.get("number") or 0)
  comment_id = int(comment.get("id") or 0)
  parent_id = int(comment.get("in_reply_to_id") or 0)
  assoc = str(comment.get("author_association") or "").upper()
  user_type = str((comment.get("user") or {}).get("type") or "")

  if not _is_valid_verify_request(event):
    print(
        f"::notice::Pre-flight skip: action={event.get('action')!r},"
        f" user_type={user_type!r}, assoc={assoc!r}, parent_id={parent_id},"
        f" has_cmd={_has_verify_command(str(comment.get('body') or ''))}."
    )
    _write_github_output(env, host=host, should_run="false")
    return 0

  parent = get_review_comment(repo, parent_id, token)
  finding_id = _extract_parent_finding_id(parent)
  if not finding_id:
    print(
        f"::notice::Pre-flight skip: parent comment {parent_id} is not a"
        " CodeMender finding comment."
    )
    _write_github_output(env, host=host, should_run="false")
    return 0

  latest: Dict[str, VerifyComment] = {}
  for vc in sorted(
      list_verify_comments(repo, pr_number, token) or (),
      key=lambda c: c.comment_id,
  ):
    latest[vc.finding_id] = vc

  other_runs_active: Optional[bool] = None

  def _react(cid: int, reaction: str = "eyes") -> None:
    if not cid:
      return
    try:
      host.add_reaction(str(cid), reaction)
    except Exception as exc:  # pylint: disable=broad-except
      logger.warning(
          "Failed to add %r reaction to comment %s: %s",
          reaction,
          cid,
          exc,
      )

  def _is_active_lock(vc: VerifyComment) -> bool:
    nonlocal other_runs_active
    if vc.state != "in-progress" or not _is_fresh_lock(vc.updated_at):
      return False
    if other_runs_active is None:
      other_runs_active = has_other_active_verify_runs(
          repo, token, env.get("GITHUB_RUN_ID", "")
      )
    return other_runs_active

  existing = latest.get(finding_id)
  if existing is not None:
    if _is_active_lock(existing):
      print(
          f"::notice::Verification for finding {finding_id} is already in"
          " progress."
      )
      _write_github_output(env, host=host, should_run="false")
      return 0
    if existing.state == "done":
      _react(comment_id, "eyes")
      status_label = _inline(existing.status or "DONE", 20)
      link = (
          f"[this comment]({existing.url})"
          if existing.url
          else "an earlier comment"
      )
      reply_to_comment(
          repo,
          pr_number,
          token,
          parent_id,
          f"ℹ️ This finding was already verified ({status_label}) in {link}.",
      )
      _write_github_output(env, host=host, should_run="false")
      return 0

  active_count = sum(1 for vc in latest.values() if _is_active_lock(vc))
  if active_count >= MAX_CONCURRENT_VERIFIES:
    _react(comment_id, "eyes")
    reply_to_comment(
        repo,
        pr_number,
        token,
        parent_id,
        f"⚠️ Maximum concurrent verifications ({MAX_CONCURRENT_VERIFIES})"
        " reached on this PR. Please wait for an active verification to finish"
        " and try again.",
    )
    _write_github_output(env, host=host, should_run="false")
    return 0

  _react(comment_id, "eyes")
  in_progress_body = _format_verify_in_progress(finding_id, _run_url(env))
  if (
      existing is not None
      and existing.state == "in-progress"
      and existing.comment_id > 0
  ):
    updated = update_review_comment(
        repo, existing.comment_id, token, in_progress_body
    )
    if updated.ok:
      _write_github_output(
          env,
          host=host,
          should_run="true",
          verify_reply_id=str(existing.comment_id),
      )
      return 0

  reply = reply_to_comment(repo, pr_number, token, parent_id, in_progress_body)
  _write_github_output(
      env,
      host=host,
      should_run="true",
      verify_reply_id=str(reply.comment_id or 0),
  )
  return 0


def run_verify(
    event: Dict[str, Any],
    env: Mapping[str, str],
    launcher: Launcher,
    *,
    host: RepoHost,
    store: Optional[ArtifactStore] = None,
    out_dir: Optional[str] = None,
) -> int:
  """Runs `cm-runner verify` for an on-demand `@cm-runner verify` comment."""
  if store is None:
    store = create_artifact_store(env, event=event)
  repo = env.get("GITHUB_REPOSITORY", "")
  token = env.get("GITHUB_TOKEN", "")
  pr = event.get("pull_request") or {}
  comment = event.get("comment") or {}
  pr_number = int(pr.get("number") or 0)
  parent_id = int(comment.get("in_reply_to_id") or 0)
  if pr_number <= 0 or parent_id <= 0:
    print(
        "::error::Event payload is missing pull_request.number or"
        " comment.in_reply_to_id."
    )
    return 1

  parent = get_review_comment(repo, parent_id, token)
  finding_id = _extract_parent_finding_id(parent)
  if not parent or not finding_id:
    print("::error::Parent comment is not a CodeMender fix comment.")
    return 1

  run_url = _run_url(env)
  try:
    reply_id = int(env.get("CODEMENDER_VERIFY_REPLY_ID") or 0)
  except ValueError:
    reply_id = 0
  if reply_id <= 0:
    try:
      posted_comment = host.reply_to_comment(
          str(parent_id),
          _format_verify_in_progress(finding_id, run_url),
      )
      reply_id = int(posted_comment.comment_id or 0)
    except (RepoHostError, ValueError) as e:
      logger.warning("host.reply_to_comment failed: %s", e)
      reply_id = 0

  def finish_reply(body: str) -> bool:
    if reply_id > 0 and update_review_comment(repo, reply_id, token, body).ok:
      return True
    try:
      host.reply_to_comment(str(parent_id), body)
      return True
    except RepoHostError:
      return False

  if not out_dir:
    out_dir = tempfile.mkdtemp(
        prefix="codemender-verify-out-", dir=env.get("RUNNER_TEMP") or None
    )
  else:
    os.makedirs(out_dir, exist_ok=True)

  launcher = launcher.with_dirs(out_dir=out_dir)

  sarif_doc: Optional[Mapping[str, Any]] = None
  try:
    sarif_doc = store.get_finding_sarif(
        finding_id, change_id=str(pr_number)
    )
  except ArtifactStoreError as e:
    logger.warning("ArtifactStore.get_finding_sarif failed: %s", e)
    sarif_doc = None

  if not sarif_doc:
    sarif_doc = _reconstruct_finding_sarif(
        finding_id, parent, get_sticky_comment_body(repo, pr_number, token)
    )
  finding_file = os.path.join(out_dir, "finding.sarif")
  with open(finding_file, "w", encoding="utf-8") as f:
    json.dump(sarif_doc, f, indent=2)
    f.write("\n")

  verify_result: Optional[RunResult] = None
  error = ""
  with _stop_fixes_on_signal(launcher):
    try:
      verify_result = launcher.run(
          "verify",
          [
              "--repo",
              CONTAINER_WORKSPACE_DIR,
              "--finding",
              f"{CONTAINER_OUT_DIR}/finding.sarif",
              "--out",
              CONTAINER_OUT_DIR,
          ],
      )
    except KeyboardInterrupt:
      finish_reply(
          _format_verify_reply(
              finding_id, "", "", run_url, "The verification run was cancelled or timed out."
          )
      )
      return 1
    except Exception as e:  # pylint: disable=broad-exception-caught
      error = str(e)

  status, dismiss_reason, verified_sarif = _parse_verify_result(verify_result)
  if verified_sarif is not None:
    try:
      verified_dir = os.path.join(out_dir, "verified")
      os.makedirs(verified_dir, exist_ok=True)
      target = os.path.join(verified_dir, f"finding-{finding_id}.sarif")
      if not os.path.islink(target):
        with open(target, "w", encoding="utf-8") as f:
          json.dump(verified_sarif, f, indent=2)
          f.write("\n")
    except OSError as e:
      logger.warning("Could not save verified SARIF: %s", e)
  elif not error:
    error = (
        f"cm-runner verify exited with code {verify_result.exit_code}."
        if verify_result is not None and not verify_result.ok
        else "cm-runner verify did not emit a valid SARIF 2.1.0 document."
    )

  if not finish_reply(
      _format_verify_reply(finding_id, status, dismiss_reason, run_url, error)
  ):
    return 1
  return 0 if status in ("VERIFIED", "DISMISSED", "OPEN") and not error else 1


def _has_verify_command(body: str) -> bool:
  stripped = _FENCED_BLOCK_RE.sub(" ", body or "")
  stripped = _INLINE_CODE_RE.sub(" ", stripped)
  return bool(_VERIFY_CMD_RE.search(stripped))


def _is_fresh_lock(updated_at: str) -> bool:
  if not updated_at:
    return True
  try:
    ts = datetime.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    if ts.tzinfo is None:
      ts = ts.replace(tzinfo=datetime.timezone.utc)
    age = (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds()
    return age < VERIFY_LOCK_TTL_SECONDS
  except ValueError:
    return True


def _write_github_output(
    env: Mapping[str, str],
    *,
    host: RepoHost,
    **items: str,
) -> None:
  del env
  host.emit_ci_outputs(items)


def _format_verify_in_progress(finding_id: str, run_url: str) -> str:
  return "\n".join([
      verify_tag("in-progress", finding_id),
      f"⏳ **Verifying finding `{finding_id}`...**"
      f" ([Workflow run logs]({run_url}))",
  ])


def _format_verify_reply(
    finding_id: str,
    status: str,
    dismiss_reason: str,
    run_url: str,
    error: str = "",
) -> str:
  if error or status not in ("VERIFIED", "DISMISSED", "OPEN"):
    reason = _inline(error or "Verification failed", 200)
    return "\n".join([
        verify_tag("failed", finding_id),
        f"⚠️ **Verification failed** for finding `{finding_id}`: {reason}."
        f" ([Workflow run logs]({run_url}))",
    ])
  tag = verify_tag("done", finding_id, status)
  if status == "VERIFIED":
    msg = (
        f"🚨 **Verified (`VERIFIED`)** — CodeMender confirmed that finding"
        f" `{finding_id}` is exploitable."
    )
  elif status == "DISMISSED":
    detail = f" Reason: {_sanitize_block_text(dismiss_reason, 500)}" if dismiss_reason else ""
    msg = (
        f"✅ **Dismissed (`DISMISSED`)** — CodeMender refuted finding"
        f" `{finding_id}` as not exploitable.{detail}"
    )
  else:
    msg = (
        f"ℹ️ **Inconclusive (`OPEN`)** — CodeMender could neither confirm nor"
        f" refute finding `{finding_id}`."
    )
  return f"{tag}\n{msg}\n\n[Workflow run logs]({run_url})"


def _parse_verify_result(
    verify_result: Optional[RunResult],
) -> Tuple[str, str, Optional[Dict[str, Any]]]:
  if (
      verify_result is None
      or not verify_result.ok
      or not (verify_result.stdout or b"").strip()
  ):
    return ("", "", None)
  try:
    text = (
        verify_result.stdout.decode("utf-8")
        if isinstance(verify_result.stdout, bytes)
        else verify_result.stdout
    )
    doc = SarifDocument.from_json(text)
    if not doc.results:
      return ("", "", None)
    props = doc.results[0].get("properties") or {}
    status = str(props.get("status") or "OPEN").strip().upper()
    reason = str(props.get("dismiss_reason") or "").strip()
    return (status, reason, doc.data)
  except Exception:  # pylint: disable=broad-exception-caught
    return ("", "", None)


def _reconstruct_finding_sarif(
    finding_id: str, parent: Mapping[str, Any], sticky_body: str = ""
) -> Dict[str, Any]:
  """Reconstructs a valid 1-result SARIF 2.1.0 dict from the parent fix comment."""
  body = str(parent.get("body") or "")
  match = _PARENT_HEADER_RE.search(body)
  index = int(match.group(1)) if match else 1
  sev_text = match.group(2) if match else ""
  rule_id = match.group(3) if match else "unknown"
  title = match.group(4) if match else "Vulnerability finding"
  level = "warning"
  for sarif_level, label in _SARIF_LEVEL_LABELS.items():
    if label in sev_text:
      level = sarif_level
      break

  details = ""
  if sticky_body:
    sec_re = re.compile(
        rf"####\s+{index}\.\s+[^\n]+\n(?:- [^\n]+\n)*?- \*\*Details:\*\*\s+([^\n]+)"
    )
    sec_match = sec_re.search(sticky_body)
    if sec_match:
      details = sec_match.group(1).strip()
  message = f"{title}: {details}" if details else title

  path = str(parent.get("path") or "").strip()
  end_line = int(parent.get("line") or parent.get("original_line") or 1)
  start_line = int(
      parent.get("start_line") or parent.get("original_start_line") or end_line
  )
  return {
      "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json",
      "version": "2.1.0",
      "runs": [{
          "tool": {"driver": {"name": "CodeMender", "rules": [{"id": rule_id}]}},
          "results": [{
              "ruleId": rule_id,
              "level": level,
              "message": {"text": message},
              "partialFingerprints": {"primaryLocationLineHash": finding_id},
              "locations": [{
                  "physicalLocation": {
                      "artifactLocation": {"uri": path},
                      "region": {"startLine": start_line, "endLine": end_line},
                  }
              }],
          }],
      }],
  }


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
    env: Mapping[str, str],
    pr_number: int,
    body: str,
    fork: bool,
    *,
    host: RepoHost,
) -> bool:
  del env, pr_number
  try:
    host.upsert_summary(body)
    return True
  except RepoHostError as e:
    print(f"::error::Could not post CodeMender summary comment: {e}")
    if fork:
      print(
          "::error::This is a fork PR. Ask a repository or organization admin"
          " to enable 'Send write tokens to workflows from pull requests'"
          " (Settings > Actions > General > Fork pull request workflows)."
      )
    return False


def is_fork_pr(event: Dict[str, Any]) -> bool:
  pr = event.get("pull_request") or {}
  head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
  base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
  return bool(base_repo) and head_repo != base_repo


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
