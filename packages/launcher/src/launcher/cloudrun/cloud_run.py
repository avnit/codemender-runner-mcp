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

# Governing: docs/openspec/specs/github-actions/spec.md#req-0003

"""Cloud Run launcher: runs cm-runner in Google Cloud Run via in-process SDK."""

import json
import logging
import os
from typing import Any, Optional, Sequence

try:
  from gcp_sandboxing.client import (  # type: ignore
      CloudRunClient,
      CloudRunError,
      CloudRunTimeoutError,
  )
except ImportError:
  # Fallback definitions for environments where gcp_sandboxing is not installed
  class CloudRunError(Exception):
    """Exception raised when Cloud Run execution fails."""

  class CloudRunTimeoutError(CloudRunError):
    """Exception raised when Cloud Run execution times out."""

  CloudRunClient = Any  # type: ignore

from launcher.base import Launcher, LauncherError, RunResult

logger = logging.getLogger("codemender-github-actions")

_DEFAULT_TIMEOUT_SECONDS = 60 * 60


class CloudRunLauncher(Launcher):
  """Executes cm-runner tasks in Google Cloud Run via in-process CloudRunClient."""

  def __init__(
      self,
      client: Any,
      log_level: str = "info",
      timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
  ):
    """Initializes the launcher.

    Args:
      client: CloudRunClient instance or mock implementing run().
      log_level: Log verbosity string passed to the client.
      timeout_seconds: Hard limit for command execution.

    Raises:
      ValueError: if client is None or not provided.
    """
    super().__init__()
    if not client:
      raise ValueError("A CloudRunClient instance is required for Cloud Run.")

    self.client = client
    self.log_level = log_level
    self.timeout_seconds = timeout_seconds

  def stop(self) -> None:
    """Stops any in-flight Cloud Run Job execution."""
    if hasattr(self.client, "cancel"):
      try:
        self.client.cancel()
      except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("Error cancelling Cloud Run client: %s", e)

  def run(self, command: str, args: Sequence[str] = ()) -> RunResult:
    """Runs a command via the in-process CloudRunClient and waits for completion."""
    # Governing: docs/openspec/specs/github-actions/spec.md#req-0003
    logger.info(
        "Executing Cloud Run task in-process: command=%s args=%s",
        command,
        list(args),
    )

    try:
      receipt = self.client.run(
          command=command,
          args=list(args),
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
