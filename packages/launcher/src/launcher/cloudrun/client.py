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

# Governing: ADR-0008, SPEC-GHA/REQ-0003
# Governing: docs/openspec/specs/github-actions/spec.md#req-0003
# Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0003, SPEC-GHA-FIX
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0001
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0004
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0005

"""In-process Cloud Run client SDK for GitHub Actions and CI platforms.

Implements typed in-process job dispatch, transit staging, receipt/log
recovery, and cooperative cancellation per
docs/openspec/specs/tech/systems/distributed-execution-and-transit/design.md,
ADR-0008, and SPEC-GHA REQ-0003.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("codemender-github-actions")


class CloudRunError(Exception):
  """Exception raised when Cloud Run job invocation or transit transfer fails."""


class CloudRunTimeoutError(CloudRunError):
  """Exception raised when Cloud Run execution exceeds the configured timeout."""


@dataclasses.dataclass
class CloudRunConfig:
  """Configuration settings for Cloud Run Job invocation."""

  project: str = ""
  bucket: str = ""
  job_name: str = ""
  region: str = ""
  workspace_dir: str = ""
  out_dir: str = ""
  run_id: str = ""
  run_attempt: str = ""
  diff_workers: str = ""
  base_sha: str = ""

  def __post_init__(self) -> None:
    if not self.project:
      self.project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if not self.bucket:
      self.bucket = (
          os.environ.get("GCS_TRANSIT_BUCKET", "").strip()
          or os.environ.get("CODEMENDER_GCS_TRANSIT_BUCKET", "").strip()
      )
    if not self.bucket and self.project:
      self.bucket = f"codemender-reports-{self.project}"
    if not self.job_name:
      self.job_name = (
          os.environ.get("CLOUD_RUN_JOB_NAME", "").strip()
          or os.environ.get("CODEMENDER_CLOUD_RUN_JOB_NAME", "").strip()
          or "codemender-runner"
      )
    if not self.region:
      self.region = (
          os.environ.get("CLOUD_RUN_REGION", "").strip()
          or os.environ.get("CODEMENDER_GCP_REGION", "").strip()
          or "us-central1"
      )
    if not self.workspace_dir:
      self.workspace_dir = os.environ.get("GITHUB_WORKSPACE", "").strip()
    if not self.out_dir:
      self.out_dir = os.environ.get("CODEMENDER_OUT_DIR", "").strip()
    if not self.run_id:
      self.run_id = os.environ.get("GITHUB_RUN_ID", "manual").strip()
    if not self.run_attempt:
      self.run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1").strip()
    if not self.diff_workers:
      self.diff_workers = os.environ.get("CODEMENDER_DIFF_WORKERS", "8").strip()
    if not self.base_sha:
      self.base_sha = os.environ.get("BASE_SHA", "HEAD~1").strip()


@dataclasses.dataclass
class ExecutionReceipt:
  """Structured machine-readable execution receipt matching ADR-0001."""

  status: str = "SUCCESS"
  exit_code: int = 0
  command: str = ""
  started_at: str = ""
  completed_at: str = ""
  finding_count: int = 0
  artifacts: Dict[str, str] = dataclasses.field(default_factory=dict)
  error: Optional[str] = None
  stdout: bytes = b""
  events: List[Dict[str, Any]] = dataclasses.field(default_factory=list)

  def resolve_stdout(self) -> bytes:
    """Resolves and returns stdout bytes, discovering from artifacts or local output if needed."""
    if self.stdout:
      return self.stdout
    if self.artifacts and isinstance(self.artifacts, dict) and "sarif" in self.artifacts:
      sarif_path = self.artifacts["sarif"]
      if sarif_path.startswith("file://"):
        sarif_path = sarif_path[len("file://") :]
      if os.path.isfile(sarif_path):
        try:
          with open(sarif_path, "rb") as f:
            return f.read()
        except OSError:
          pass
    out_dir = os.environ.get("CODEMENDER_OUT_DIR", "")
    for cand in [
        os.path.join(out_dir, "report.sarif") if out_dir else "",
        os.path.join("output", "report.sarif"),
        "report.sarif",
        os.path.join(out_dir, "finding.sarif") if out_dir else "",
        os.path.join("output", "finding.sarif"),
        "finding.sarif",
    ]:
      if cand and os.path.isfile(cand):
        try:
          with open(cand, "rb") as f:
            return f.read()
        except OSError:
          pass
    return b""

  def __post_init__(self):
    """Populates stdout from local SARIF artifact if stdout was not directly provided."""
    if not self.stdout:
      self.stdout = self.resolve_stdout()

  def get(self, key: str, default: Any = None) -> Any:
    """Provides dictionary-like key access for compatibility."""
    return getattr(self, key, default)


def _parse_cli_arg(args: Sequence[str], flag: str, default: str = "") -> str:
  """Extracts flag value from a sequence of CLI arguments."""
  flag_prefix = f"{flag}="
  i = 0
  while i < len(args):
    arg = args[i]
    if arg == flag and i + 1 < len(args):
      return args[i + 1]
    if arg.startswith(flag_prefix):
      return arg[len(flag_prefix) :]
    i += 1
  return default


def _gcloud_transfer(src: str, dest: str, check: bool = True) -> bool:
  """Transfers a file or prefix between local and GCS using gcloud storage or gsutil."""
  cmd = ["gcloud", "storage", "cp", src, dest, "--quiet"]
  res = subprocess.run(
      cmd,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
      check=False,
  )
  if res.returncode == 0:
    return True

  # Fallback to gsutil
  gsutil_cmd = ["gsutil", "cp", src, dest]
  res_gs = subprocess.run(
      gsutil_cmd,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
      check=False,
  )
  if res_gs.returncode == 0:
    return True

  err_msg = res.stderr.strip() or res_gs.stderr.strip()
  if check:
    raise CloudRunError(f"Failed to transfer {src} -> {dest}: {err_msg}")
  logger.debug("Optional transfer %s -> %s failed: %s", src, dest, err_msg)
  return False


def _package_workspace(repo_dir: str, tarball_path: str) -> None:
  """Creates a diff-capable repository tarball including .git history."""
  os.makedirs(os.path.dirname(os.path.abspath(tarball_path)), exist_ok=True)
  tar_bin = shutil.which("tar")
  if tar_bin:
    cmd = [
        tar_bin,
        "-czf",
        tarball_path,
        "--exclude=.git/objects/pack/*.tmp",
        "--exclude=__pycache__",
        "--exclude=gha-creds-*.json",
        "--exclude=.cm_project",
        "--exclude=.exploit",
        "-C",
        repo_dir,
        ".",
    ]
    res = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if res.returncode == 0 and os.path.isfile(tarball_path):
      return
    logger.warning(
        "tar command exited with %d: %s; falling back to python tarfile",
        res.returncode,
        res.stderr.decode("utf-8", "replace"),
    )

  def _tar_filter(member: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
    parts = member.name.replace("\\", "/").split("/")
    for part in parts:
      if part in ("__pycache__", ".cm_project", ".exploit"):
        return None
      if fnmatch.fnmatch(part, "gha-creds-*.json"):
        return None
    return member

  # Fallback to pure Python tarfile
  with tarfile.open(tarball_path, "w:gz") as tar:
    tar.add(
        repo_dir,
        arcname=".",
        filter=_tar_filter,
    )


def _extract_execution_name(text: Optional[Any]) -> Optional[str]:
  """Extracts the Cloud Run execution name from gcloud CLI output."""
  if not text:
    return None
  if isinstance(text, bytes):
    text = text.decode("utf-8", "replace")
  match = re.search(r"Execution\s+\[([a-zA-Z0-9_-]+)\]", text, re.IGNORECASE)
  if match:
    return match.group(1)
  match = re.search(r"executions/(?:details/[^/]+/)?([a-zA-Z0-9_-]+)", text)
  if match:
    return match.group(1)
  match = re.search(
      r"(?:execution|executions)\s+([a-zA-Z0-9_-]+)", text, re.IGNORECASE
  )
  if match:
    return match.group(1)
  return None


def _gcloud_cancel_execution(
    execution_name: str, region: str, project: str
) -> bool:
  """Cancels an active Cloud Run execution via gcloud CLI."""
  cmd = [
      "gcloud",
      "run",
      "jobs",
      "executions",
      "cancel",
      execution_name,
      f"--region={region}",
      f"--project={project}",
      "--quiet",
  ]
  logger.info("Cancelling Cloud Run execution %s...", execution_name)
  res = subprocess.run(
      cmd,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
      check=False,
  )
  if res.returncode == 0:
    logger.info(
        "Successfully cancelled Cloud Run execution %s.", execution_name
    )
    return True
  logger.warning(
      "Failed to cancel Cloud Run execution %s (exit %d): %s",
      execution_name,
      res.returncode,
      res.stderr.strip() if res.stderr else "",
  )
  return False


class CloudRunClient:
  """Orchestrates CodeMender execution inside Google Cloud Run Jobs."""

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0003, SPEC-GHA-FIX
  def __init__(self, config: Optional[CloudRunConfig] = None):
    self.config = config or CloudRunConfig()
    self.current_execution: Optional[str] = None
    self.current_process: Optional[Any] = None
    self._lock = threading.RLock()
    self._payload_lock = threading.RLock()
    self._staged_payloads: Dict[str, str] = {}
    self._active_executions: set[str] = set()
    self._active_processes: set[Any] = set()

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0003, SPEC-GHA-FIX
  def cancel(self, execution_name: Optional[str] = None) -> bool:
    """Cancels the active or specified Cloud Run Job execution(s)."""
    if execution_name:
      with self._lock:
        self._active_executions.discard(execution_name)
      return _gcloud_cancel_execution(
          execution_name,
          region=self.config.region,
          project=self.config.project,
      )

    if not self._active_executions and not self.current_execution:
      has_running_proc = (
          (self.current_process and self.current_process.poll() is None)
          or any(p.poll() is None for p in list(self._active_processes))
      )
      if has_running_proc:
        # If a process is still starting, wait briefly for the execution name to appear
        for _ in range(25):
          if self._active_executions or self.current_execution:
            break
          time.sleep(0.1)

    with self._lock:
      target_names = list(self._active_executions)
      if not target_names and self.current_execution:
        target_names = [self.current_execution]
      self._active_executions.clear()

    cancelled = False
    if target_names:
      for target_name in target_names:
        if _gcloud_cancel_execution(
            target_name,
            region=self.config.region,
            project=self.config.project,
        ):
          cancelled = True
    else:
      logger.warning("No Cloud Run execution name available to cancel.")

    with self._lock:
      procs = list(self._active_processes)
      if self.current_process and self.current_process not in procs:
        procs.append(self.current_process)
    for proc in procs:
      if proc and proc.poll() is None:
        try:
          proc.terminate()
        except OSError:
          pass
    return cancelled

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0003, SPEC-GHA-FIX
  def run(
      self,
      command: str,
      args: Sequence[str] = (),
      log_level: str = "info",
      timeout_seconds: int = 3600,
  ) -> ExecutionReceipt:
    """Executes a command on Cloud Run Jobs, awaiting completion and receipt."""
    project = self.config.project
    bucket = self.config.bucket
    job_name = self.config.job_name
    region = self.config.region

    if not project:
      raise CloudRunError(
          "GOOGLE_CLOUD_PROJECT must be configured for Cloud Run execution."
      )
    if not bucket:
      bucket = f"codemender-reports-{project}"

    # Parse arguments
    raw_repo = _parse_cli_arg(args, "--repo", "")
    if raw_repo and os.path.isdir(raw_repo) and raw_repo != "/workspace":
      repo_dir = raw_repo
    elif self.config.workspace_dir and os.path.isdir(self.config.workspace_dir):
      repo_dir = self.config.workspace_dir
    else:
      repo_dir = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()

    if not os.path.isdir(repo_dir):
      raise CloudRunError(
          f"Repository directory to package does not exist: {repo_dir}"
      )

    diff_base = _parse_cli_arg(
        args, "--base", self.config.base_sha or os.environ.get("BASE_SHA", "HEAD~1")
    )
    diff_workers = _parse_cli_arg(
        args,
        "--diff-workers",
        self.config.diff_workers or os.environ.get("CODEMENDER_DIFF_WORKERS", "8"),
    )

    raw_out = _parse_cli_arg(args, "--out", "")
    if (
        raw_out
        and raw_out != "/out"
        and os.path.exists(os.path.dirname(os.path.abspath(raw_out)))
    ):
      out_dir = raw_out
    else:
      out_dir = self.config.out_dir or os.environ.get("CODEMENDER_OUT_DIR", "").strip()

    raw_finding = _parse_cli_arg(args, "--finding", "")
    if raw_finding.startswith("/out/") and out_dir:
      raw_finding = os.path.join(out_dir, raw_finding[len("/out/") :])

    run_id = self.config.run_id or os.environ.get("GITHUB_RUN_ID", "manual")
    attempt = self.config.run_attempt or os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    salt = secrets.token_hex(4)
    run_tag = f"{run_id}-{attempt}-{salt}"

    gcs_prefix = f"gs://{bucket}/{run_tag}"
    payload_uri = f"{gcs_prefix}/input/code.tar.gz"
    output_uri = f"{gcs_prefix}/output"
    receipt_gcs_uri = f"{output_uri}/receipt.json"
    sarif_gcs_uri = f"{output_uri}/report.sarif"
    verified_sarif_gcs_uri = f"{output_uri}/finding.sarif"
    diff_gcs_uri = f"{output_uri}/finding.diff"
    log_gcs_uri = f"{output_uri}/runner.log"
    manifest_gcs_uri = f"{gcs_prefix}/input/manifest.json"

    with tempfile.TemporaryDirectory(prefix="cm-cr-") as temp_dir:
      # Stage source payload once per repository directory across find-diff and parallel fixes
      if command not in ("version",):
        repo_key = os.path.abspath(repo_dir)
        with self._payload_lock:
          cached_payload_uri = self._staged_payloads.get(repo_key)
          if cached_payload_uri:
            payload_uri = cached_payload_uri
          else:
            tarball_path = os.path.join(temp_dir, "code.tar.gz")
            logger.info("Packaging repository snapshot from %s...", repo_dir)
            _package_workspace(repo_dir, tarball_path)
            logger.info("Uploading repository snapshot to %s...", payload_uri)
            _gcloud_transfer(tarball_path, payload_uri)
            self._staged_payloads[repo_key] = payload_uri

      finding_gcs_uri = ""
      if (
          command in ("fix", "verify")
          and raw_finding
          and os.path.isfile(raw_finding)
      ):
        finding_gcs_uri = f"{gcs_prefix}/input/finding.sarif"
        logger.info(
            "Uploading finding SARIF %s to %s...", raw_finding, finding_gcs_uri
        )
        _gcloud_transfer(raw_finding, finding_gcs_uri)

      # Stage manifest.json
      manifest_data: Dict[str, Any] = {
          "command": command,
          "payload_uri": payload_uri if command != "version" else "",
          "output_prefix": output_uri,
          "output_uri": output_uri,
          "diff_base": diff_base,
          "diff_workers": diff_workers,
          "log_level": log_level,
      }
      if finding_gcs_uri:
        manifest_data["finding_uri"] = finding_gcs_uri

      manifest_path = os.path.join(temp_dir, "manifest.json")
      with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=2)
      logger.info("Staging execution manifest to %s...", manifest_gcs_uri)
      _gcloud_transfer(manifest_path, manifest_gcs_uri)

      # Build gcloud run jobs execute invocation
      exec_cmd = [
          "gcloud",
          "run",
          "jobs",
          "execute",
          job_name,
          f"--region={region}",
          f"--project={project}",
          f"--args={command},--manifest={manifest_gcs_uri}",
          "--wait",
      ]

      logger.info(
          "Dispatching Cloud Run Job execution: %s",
          " ".join(exec_cmd),
      )

      try:
        proc = subprocess.Popen(
            exec_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
      except FileNotFoundError as exc:
        raise CloudRunError(f"gcloud CLI not found: {exc}") from exc

      with self._lock:
        self.current_process = proc
        self._active_processes.add(proc)
      run_exec_name: Optional[str] = None
      stdout_lines: List[str] = []
      stderr_lines: List[str] = []

      def _stream_output(stream: Any, storage: List[str]):
        nonlocal run_exec_name
        if not stream:
          return
        for line in iter(stream.readline, ""):
          storage.append(line)
          if not run_exec_name:
            exec_name = _extract_execution_name(line)
            if exec_name:
              run_exec_name = exec_name
              with self._lock:
                self.current_execution = exec_name
                self._active_executions.add(exec_name)
        try:
          stream.close()
        except Exception:
          pass

      err_thread = threading.Thread(
          target=_stream_output, args=(proc.stderr, stderr_lines), daemon=True
      )
      out_thread = threading.Thread(
          target=_stream_output, args=(proc.stdout, stdout_lines), daemon=True
      )
      err_thread.start()
      out_thread.start()

      try:
        proc.wait(timeout=timeout_seconds)
      except subprocess.TimeoutExpired as exc:
        exec_name = (
            run_exec_name
            or _extract_execution_name("".join(stderr_lines))
            or _extract_execution_name("".join(stdout_lines))
        )
        if exec_name:
          run_exec_name = exec_name
          with self._lock:
            self.current_execution = exec_name
          try:
            self.cancel(exec_name)
          except Exception as cancel_exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "Error cancelling execution %s on timeout: %s",
                exec_name,
                cancel_exc,
            )
        if proc.poll() is None:
          try:
            proc.kill()
          except OSError:
            pass
        raise CloudRunTimeoutError(
            f"Cloud Run Job execution timed out after {timeout_seconds} seconds."
        ) from exc
      finally:
        err_thread.join(timeout=5)
        out_thread.join(timeout=5)
        with self._lock:
          self._active_processes.discard(proc)
          if self.current_process is proc:
            self.current_process = None
          if run_exec_name:
            self._active_executions.discard(run_exec_name)

      stderr_text = "".join(stderr_lines)
      stdout_text = "".join(stdout_lines)
      if not run_exec_name:
        exec_name = (
            _extract_execution_name(stderr_text)
            or _extract_execution_name(stdout_text)
        )
        if exec_name:
          run_exec_name = exec_name
          with self._lock:
            self.current_execution = exec_name
            self._active_executions.discard(exec_name)

      logger.info(
          "Cloud Run Job dispatch finished with returncode %d.", proc.returncode
      )

      # Retrieve receipt.json
      local_receipt = os.path.join(temp_dir, "receipt.json")
      receipt_ok = _gcloud_transfer(receipt_gcs_uri, local_receipt, check=False)

      output_bytes = b""
      if command == "fix":
        local_diff = os.path.join(temp_dir, "finding.diff")
        _gcloud_transfer(diff_gcs_uri, local_diff, check=False)
        if os.path.isfile(local_diff):
          with open(local_diff, "rb") as f:
            output_bytes = f.read()
        if out_dir:
          os.makedirs(out_dir, exist_ok=True)
          if os.path.isfile(local_receipt):
            shutil.copy2(local_receipt, os.path.join(out_dir, "receipt.json"))
          if os.path.isfile(local_diff):
            if raw_finding:
              stem = os.path.splitext(os.path.basename(raw_finding))[0]
              diff_name = f"{stem}.diff"
            else:
              diff_name = "finding.diff"
            shutil.copy2(local_diff, os.path.join(out_dir, diff_name))
          _gcloud_transfer(
              log_gcs_uri, os.path.join(out_dir, "runner.log"), check=False
          )
      elif command == "verify":
        # Governing: docs/openspec/specs/github-actions-verify/spec.md#req-0006
        local_verified = os.path.join(temp_dir, "finding.sarif")
        _gcloud_transfer(verified_sarif_gcs_uri, local_verified, check=False)
        if os.path.isfile(local_verified):
          with open(local_verified, "rb") as f:
            output_bytes = f.read()
        if out_dir:
          os.makedirs(out_dir, exist_ok=True)
          if os.path.isfile(local_receipt):
            shutil.copy2(local_receipt, os.path.join(out_dir, "receipt.json"))
          if os.path.isfile(local_verified):
            shutil.copy2(local_verified, os.path.join(out_dir, "finding.sarif"))
            if raw_finding:
              stem = os.path.splitext(os.path.basename(raw_finding))[0]
              sarif_name = f"{stem}.sarif"
              if sarif_name != "finding.sarif":
                shutil.copy2(local_verified, os.path.join(out_dir, sarif_name))
          _gcloud_transfer(
              log_gcs_uri, os.path.join(out_dir, "runner.log"), check=False
          )
      else:
        # Retrieve SARIF report
        local_sarif = os.path.join(temp_dir, "report.sarif")
        _gcloud_transfer(sarif_gcs_uri, local_sarif, check=False)
        if os.path.isfile(local_sarif):
          with open(local_sarif, "rb") as f:
            output_bytes = f.read()

        # Stage artifacts into CODEMENDER_OUT_DIR if configured
        if out_dir:
          os.makedirs(out_dir, exist_ok=True)
          if os.path.isfile(local_receipt):
            shutil.copy2(local_receipt, os.path.join(out_dir, "receipt.json"))
          if os.path.isfile(local_sarif):
            shutil.copy2(local_sarif, os.path.join(out_dir, "report.sarif"))
          _gcloud_transfer(
              log_gcs_uri, os.path.join(out_dir, "runner.log"), check=False
          )

      if not receipt_ok or not os.path.isfile(local_receipt):
        if proc.returncode != 0:
          raise CloudRunError(
              f"Cloud Run Job execution failed (exit code {proc.returncode}) "
              f"and produced no receipt.json: {stderr_text.strip()}"
          )
        raise CloudRunError(
            f"Cloud Run Job completed but receipt.json was not found at {receipt_gcs_uri}"
        )

      with open(local_receipt, "r", encoding="utf-8") as f:
        receipt_data = json.load(f)

      # Log spill if non-zero exit code
      raw_exit = receipt_data.get("exit_code", proc.returncode)
      if raw_exit != 0:
        local_log = (
            os.path.join(out_dir, "runner.log")
            if out_dir and os.path.isfile(os.path.join(out_dir, "runner.log"))
            else os.path.join(temp_dir, "runner.log")
        )
        if not os.path.isfile(local_log):
          _gcloud_transfer(log_gcs_uri, local_log, check=False)
        if os.path.isfile(local_log):
          with open(local_log, "r", encoding="utf-8", errors="replace") as f:
            sys.stderr.write(
                f"\n--- [Cloud Run Job runner.log] ---\n{f.read()}\n--- [End runner.log] ---\n"
            )
            sys.stderr.flush()

      return ExecutionReceipt(
          status=receipt_data.get("status", "SUCCESS" if proc.returncode == 0 else "FAILURE"),
          exit_code=int(raw_exit),
          command=receipt_data.get("command", command),
          started_at=receipt_data.get("started_at", ""),
          completed_at=receipt_data.get("completed_at", ""),
          finding_count=int(receipt_data.get("finding_count", 0)),
          artifacts=receipt_data.get("artifacts", {}),
          error=receipt_data.get("error"),
          stdout=output_bytes,
          events=receipt_data.get("events", []),
      )
