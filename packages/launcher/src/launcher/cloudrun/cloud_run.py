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

"""Cloud Run launcher: runs cm-runner in Google Cloud Run via in-process SDK."""

import json
import logging
import os
import shutil
import tempfile
from typing import Any, Optional, Sequence

from launcher.base import (
    Launcher,
    LauncherError,
    ParallelResult,
    ParallelTask,
    RunResult,
)
from launcher.cloudrun.client import (
    CloudRunClient,
    CloudRunConfig,
    CloudRunError,
    CloudRunTimeoutError,
    ExecutionReceipt,
)

logger = logging.getLogger("codemender-github-actions")

_DEFAULT_TIMEOUT_SECONDS = 60 * 60


def _read_bytes(path: str) -> bytes:
  try:
    with open(path, "rb") as f:
      return f.read()
  except OSError:
    return b""


def _read_text(path: str) -> str:
  try:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
      return f.read()
  except OSError:
    return ""


class CloudRunLauncher(Launcher):
  """Executes cm-runner tasks in Google Cloud Run via in-process CloudRunClient."""

  def __init__(
      self,
      client: Any,
      log_level: str = "info",
      timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
      workspace_dir: Optional[str] = None,
      out_dir: Optional[str] = None,
  ):
    """Initializes the launcher.

    Args:
      client: CloudRunClient instance or mock implementing run().
      log_level: Log verbosity string passed to the client.
      timeout_seconds: Hard limit for command execution.
      workspace_dir: Optional host directory mapped from `/workspace`.
      out_dir: Optional host directory mapped from `/out`.

    Raises:
      ValueError: if client is None or not provided.
    """
    super().__init__()
    if not client:
      raise ValueError("A CloudRunClient instance is required for Cloud Run.")

    self.client = client
    self.log_level = log_level
    self.timeout_seconds = timeout_seconds
    self.workspace_dir = workspace_dir
    self.out_dir = out_dir
    self._stopped = False

  def with_dirs(
      self,
      *,
      workspace_dir: Optional[str] = None,
      cm_home_dir: Optional[str] = None,
      out_dir: Optional[str] = None,
      timeout_seconds: Optional[int] = None,
  ) -> "CloudRunLauncher":
    """Returns a launcher with the same client and settings but different directories."""
    del cm_home_dir
    return CloudRunLauncher(
        client=self.client,
        log_level=self.log_level,
        timeout_seconds=(
            self.timeout_seconds if timeout_seconds is None else timeout_seconds
        ),
        workspace_dir=(
            self.workspace_dir if workspace_dir is None else workspace_dir
        ),
        out_dir=self.out_dir if out_dir is None else out_dir,
    )

  def stop(self) -> None:
    """Stops any in-flight Cloud Run Job execution."""
    self._stopped = True
    if hasattr(self.client, "cancel"):
      try:
        self.client.cancel()
      except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("Error cancelling Cloud Run client: %s", e)

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0003, SPEC-GHA-FIX
  def run_isolated(
      self,
      task: ParallelTask,
      src_workspace: str,
      temp_root: Optional[str] = None,
      timeout_seconds: Optional[int] = None,
  ) -> ParallelResult:
    """Runs one task in an isolated Cloud Run Job execution."""
    if self._stopped:
      return ParallelResult(key=task.key, error="Launcher was stopped.")
    if not os.path.isdir(src_workspace):
      return ParallelResult(
          key=task.key,
          error=f"Source workspace does not exist: {src_workspace!r}",
      )

    limit = (
        self.timeout_seconds if timeout_seconds is None else timeout_seconds
    )
    folder = tempfile.mkdtemp(
        prefix="codemender-cr-task-", dir=temp_root or None
    )
    try:
      out_dir = os.path.join(folder, "out")
      os.makedirs(out_dir, exist_ok=True)
      for name, data in task.input_files.items():
        path = os.path.join(out_dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
          f.write(data)

      rewritten_args = []
      for arg in task.args:
        if arg == "/workspace":
          rewritten_args.append(src_workspace)
        elif arg == "/out":
          rewritten_args.append(out_dir)
        elif arg.startswith("/out/"):
          rewritten_args.append(os.path.join(out_dir, arg[len("/out/") :]))
        else:
          rewritten_args.append(arg)

      try:
        receipt = self.client.run(
            command=task.command,
            args=rewritten_args,
            log_level=self.log_level,
            timeout_seconds=limit,
        )
      except (CloudRunTimeoutError, CloudRunError, LauncherError) as e:
        return ParallelResult(
            key=task.key,
            exit_code=None,
            log=_read_text(os.path.join(out_dir, "runner.log")),
            error=str(e),
        )

      raw_exit_code = getattr(receipt, "exit_code", None)
      status = getattr(receipt, "status", None)
      if isinstance(raw_exit_code, int):
        exit_code = raw_exit_code
      elif isinstance(raw_exit_code, (str, float)):
        try:
          exit_code = int(raw_exit_code)
        except (ValueError, TypeError):
          exit_code = 0 if status == "SUCCESS" else 1
      elif status == "FAILURE":
        exit_code = 1
      else:
        exit_code = 0

      log_text = _read_text(os.path.join(out_dir, "runner.log"))
      output = b""
      if task.output_file:
        output = _read_bytes(os.path.join(out_dir, task.output_file))
      if not output:
        raw_stdout = getattr(receipt, "stdout", None)
        if isinstance(raw_stdout, bytes) and raw_stdout:
          output = raw_stdout

      if exit_code == 0 and (not task.output_file or output.strip()):
        error = ""
      elif exit_code == 0:
        error = "cm-runner exited with code 0 but wrote no output."
      else:
        receipt_err = getattr(receipt, "error", None)
        error = (
            str(receipt_err)
            if receipt_err
            else f"cm-runner exited with code {exit_code}."
        )

      return ParallelResult(
          key=task.key,
          exit_code=exit_code,
          output=output,
          log=log_text,
          error=error,
      )
    finally:
      shutil.rmtree(folder, ignore_errors=True)

  def run(
      self,
      command: str,
      args: Sequence[str] = (),
      log_path: Optional[str] = None,
  ) -> RunResult:
    """Runs a command via the in-process CloudRunClient and waits for completion."""
    del log_path
    # Governing: docs/openspec/specs/github-actions/spec.md#req-0003
    # Governing: docs/openspec/specs/github-actions-verify/spec.md#req-0006
    client_cfg = getattr(self.client, "config", None)
    cfg_ws = (
        self.workspace_dir
        if self.workspace_dir is not None
        else getattr(client_cfg, "workspace_dir", None)
    )
    ws_dir = cfg_ws.strip() if isinstance(cfg_ws, str) else ""
    cfg_out = (
        self.out_dir
        if self.out_dir is not None
        else getattr(client_cfg, "out_dir", None)
    )
    out_dir = cfg_out.strip() if isinstance(cfg_out, str) else ""

    rewritten_args = []
    for arg in args:
      if arg == "/workspace" and ws_dir:
        rewritten_args.append(ws_dir)
      elif arg == "/out" and out_dir:
        rewritten_args.append(out_dir)
      elif arg.startswith("/out/") and out_dir:
        rewritten_args.append(os.path.join(out_dir, arg[len("/out/") :]))
      else:
        rewritten_args.append(arg)

    logger.info(
        "Executing Cloud Run task in-process: command=%s args=%s",
        command,
        rewritten_args,
    )

    try:
      receipt = self.client.run(
          command=command,
          args=rewritten_args,
          log_level=self.log_level,
          timeout_seconds=self.timeout_seconds,
      )
    except CloudRunTimeoutError as e:
      raise LauncherError(
          f"Cloud Run command {command!r} timed out after {self.timeout_seconds}s."
      ) from e
    except CloudRunError as e:
      raise LauncherError(
          f"Cloud Run execution failed: {e}"
      ) from e

    # Support receipt attributes per ADR-0001 and bootstrapper contract
    raw_exit_code = getattr(receipt, "exit_code", None)
    status = getattr(receipt, "status", None)
    finding_count = getattr(receipt, "finding_count", None)
    error = getattr(receipt, "error", None)

    if isinstance(raw_exit_code, int):
      exit_code = raw_exit_code
    elif isinstance(raw_exit_code, (str, float)):
      try:
        exit_code = int(raw_exit_code)
      except (ValueError, TypeError):
        exit_code = 0 if status == "SUCCESS" else 1
    elif status == "SUCCESS":
      exit_code = 0
    elif status == "FAILURE":
      exit_code = 1
    else:
      exit_code = 0

    if finding_count is not None and exit_code == 0:
      exit_code = 0

    if error and isinstance(error, str):
      logger.info("Cloud Run execution reported error: %s", error)

    # Extract parsed events from receipt per ADR-0001
    raw_events = getattr(receipt, "events", None)
    if isinstance(raw_events, (list, tuple)):
      events = list(raw_events)
    else:
      raw_event = getattr(receipt, "event", None)
      if isinstance(raw_event, dict):
        events = [raw_event]
      else:
        events = []

    # Extract raw stdout or fall back to event JSON
    raw_stdout = getattr(receipt, "stdout", None)
    stdout = b""
    if isinstance(raw_stdout, bytes) and raw_stdout:
      stdout = raw_stdout
    elif isinstance(raw_stdout, str) and raw_stdout:
      stdout = raw_stdout.encode("utf-8")
    elif events:
      try:
        stdout = json.dumps(events[0]).encode("utf-8")
      except (TypeError, ValueError):
        stdout = b""
    elif getattr(receipt, "event", None) and isinstance(receipt.event, dict):
      try:
        stdout = json.dumps(receipt.event).encode("utf-8")
      except (TypeError, ValueError):
        stdout = b""

    return RunResult(exit_code=exit_code, stdout=stdout, events=events)
