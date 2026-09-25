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

"""`cm-runner version` -- report orchestrator, cm, and contract versions.

This command must **never fail because `cm` is missing**. It is the command you
run precisely when something is broken, so a missing binary is reported as data
(`"found": false`) and the exit code stays 0.

That is the opposite of `cm-runner init`, where a missing `cm` is a hard
failure. The asymmetry is intentional: `version` diagnoses, `init` gates.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
from typing import Any, Dict, Optional

import cli
from cli import exit_codes
from cli.events import EventEmitter

logger = logging.getLogger("cm-runner")

# The `cm` CLI surface this orchestrator was built and tested against.
#
# Recorded so that version drift is visible rather than mysterious. There is
# precedent: cm 0.7.0 changed `cm report --format json` from PascalCase to
# snake_case keys, which `codemender/cli.py` still carries a workaround for.
# Observed working: cm 0.9.0 on 2026-09-24.
CM_CONTRACT = "cm-0.9.x"


def _normalise_version(raw: Optional[str]) -> Optional[str]:
  """Extracts the bare version from `cm --version` output.

  `cm --version` prints "cm version 0.9.0"; we want "0.9.0".

  Args:
    raw: Raw stdout from the version call, or None.

  Returns:
    The trailing version token, or None if there was nothing to parse.
  """
  if not raw:
    return None
  lines = [line.strip() for line in raw.splitlines() if line.strip()]
  if not lines:
    return None
  for line in lines:
    if "version" in line.lower():
      parts = line.split()
      if parts:
        return parts[-1]
  parts = lines[0].split()
  return parts[-1] if parts else None


def detect_cm(cm_binary: Optional[str] = None,
              timeout_sec: int = 30) -> Dict[str, Any]:
  """Locates the cm binary and reads its version without ever raising.

  Note on why this does not reuse `codemender.cli.log_cm_version`:
  that helper goes through `utils.run_command`, which writes a subprocess
  banner and every line of child output directly to **sys.stdout**
  (utils.py lines 295-314). That is fine for the env-var pipelines, whose
  stdout is just a log, but it would corrupt this CLI's NDJSON event stream.
  Until there is a shared subprocess helper that streams to stderr, commands
  here call subprocess directly with output captured.

  Args:
    cm_binary: Explicit path to cm. When None, cm is looked up on PATH.
    timeout_sec: How long to wait for `cm --version` before giving up.

  Returns:
    A dict with `found`, `version` and `path` keys. `found` is False when no
    binary could be located; `version` may be None even when found, if the
    binary exists but could not be executed or parsed.
  """
  path = cm_binary or shutil.which("cm")
  if not path:
    return {"found": False, "version": None, "path": None}

  version: Optional[str] = None
  try:
    result = subprocess.run(
        [path, "--version"],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    if result.returncode == 0:
      version = _normalise_version(result.stdout)
    else:
      logger.warning(
          "cm --version exited %d: %s",
          result.returncode,
          (result.stderr or "").strip(),
      )
  except (OSError, subprocess.SubprocessError) as exc:
    # A binary that exists but will not report its version is worth surfacing,
    # but it is not a reason for `version` itself to fail.
    logger.warning("Could not read cm version from %s: %s", path, exc)

  return {"found": True, "version": version, "path": path}


def run(args: argparse.Namespace, emitter: EventEmitter) -> int:
  """Emits a single `version` event.

  Args:
    args: Parsed CLI arguments; only `cm_binary` is consulted.
    emitter: Destination for the event.

  Returns:
    Always `exit_codes.OK`.
  """
  emitter.emit(
      "version",
      orchestrator=cli.__version__,
      cm=detect_cm(getattr(args, "cm_binary", None)),
      contract=CM_CONTRACT,
  )
  return exit_codes.OK
