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

import cli
from cli import exit_codes
from cli.events import EventEmitter
from codemender.cm import detect_cm

# The `cm` CLI surface this orchestrator was built and tested against.
#
# Recorded so that version drift is visible rather than mysterious. There is
# precedent: cm 0.7.0 changed `cm report --format json` from PascalCase to
# snake_case keys, which `codemender/cm.py` still carries a workaround for.
# Observed working: cm 0.9.0 on 2026-09-24.
CM_CONTRACT = "cm-0.9.x"


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
