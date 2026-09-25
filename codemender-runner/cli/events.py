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

"""Structured output for cm-runner.

The output contract, which every command must honour:

  1. stdout carries NDJSON and nothing else. One JSON object per line.
  2. Every human-readable word -- logs, progress, diagnostics -- goes to stderr.
  3. Each command emits one event type, so every stdout line is one unit of
     work. There is no trailing summary event: success or failure comes from
     the exit code, and error messages go to stderr.
  4. `--output text` swaps only the renderer. The underlying events are
     identical.

Events are emitted as work happens rather than batched at the end: `init`
shells out to `gcloud` and `cm` and can take tens of seconds, and a silent CI
log looks like a hang.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from datetime import timezone
from typing import Any, Dict, Optional, TextIO

# Output format identifiers.
JSON = "json"
TEXT = "text"


def utc_now() -> str:
  """Returns an ISO-8601 UTC timestamp, second resolution."""
  return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EventEmitter:
  """Writes structured events to a stream.

  Attributes:
    stream: Destination for events. Always stdout in normal operation; tests
      inject a StringIO.
    output_format: Either JSON (NDJSON, one object per line) or TEXT
      (human-readable). The event data is the same either way.
  """

  def __init__(
      self,
      stream: Optional[TextIO] = None,
      output_format: str = JSON,
  ) -> None:
    self.stream = stream if stream is not None else sys.stdout
    self.output_format = output_format

  def emit(self, event: str, **fields: Any) -> Dict[str, Any]:
    """Writes a single event and returns the payload that was written.

    Args:
      event: The event type, e.g. "version", "check", "finding".
      **fields: Arbitrary event-specific data.

    Returns:
      The full payload including the injected timestamp, so callers can
      inspect or accumulate what was emitted.
    """
    payload: Dict[str, Any] = {"ts": utc_now(), "event": event}
    payload.update(fields)

    if self.output_format == TEXT:
      line = self._render_text(payload)
    else:
      line = json.dumps(payload)

    self.stream.write(line + "\n")
    # Flush per event: CI log tailing is the whole point of streaming.
    self.stream.flush()
    return payload

  @staticmethod
  def _render_text(payload: Dict[str, Any]) -> str:
    """Renders an event for human eyes."""
    event = payload.get("event")

    if event == "version":
      cm = payload.get("cm") or {}
      cm_line = cm.get("version") if cm.get("found") else "not found"
      return (
          f"cm-runner  {payload.get('orchestrator')}\n"
          f"cm         {cm_line}\n"
          f"contract   {payload.get('contract')}"
      )

    # Generic fallback: every field except the timestamp, space separated.
    return " ".join(f"{k}={v}" for k, v in payload.items() if k != "ts")
