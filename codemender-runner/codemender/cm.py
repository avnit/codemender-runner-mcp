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

"""Code for running the `cm` binary and reading its output.

Two audiences, kept apart on purpose:

*   **New commands (`cli/`)** run `cm` only through `CmAdapter`. It starts `cm`
    itself, with a timeout and with output captured, and returns a `CmResult`
    this module owns. It never touches the legacy `utils` module.
*   **The legacy pipeline (`runners/`)** keeps using `parse_findings_json`,
    `extract_session_id` and `log_cm_version`, which still go through `utils`.
    Those imports happen inside the functions, so importing this module does
    not load `utils` (and through it, `requests`). `cm-runner version` has to
    work before `requirements.txt` is installed.
"""

import dataclasses
import logging
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


logger = logging.getLogger("codemender-orchestrator")


# ---------------------------------------------------------------------------
# New path: CmAdapter
# ---------------------------------------------------------------------------

# How long `cm --version` may take before we give up on it.
_VERSION_TIMEOUT_SEC = 30

# cm prints one of these lines per model call it makes, e.g.
# "Tokens: 41.5k in / 1.2k out / 42.7k total".
_TOKENS_RE = re.compile(
    r"Tokens:\s*([0-9.kMgG]+)\s*in\s*/\s*([0-9.kMgG]+)\s*out\s*/\s*([0-9.kMgG]+)\s*total"
)
_TOKEN_SUFFIXES = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}


@dataclasses.dataclass(frozen=True)
class TokenUsage:
  """Model token counts reported by one `cm` call, summed across its lines."""

  in_tokens: int = 0
  out_tokens: int = 0
  total_tokens: int = 0


@dataclasses.dataclass(frozen=True)
class CmResult:
  """The outcome of one `cm` call, already processed.

  Attributes:
    args: The full command that was run, binary first.
    exit_code: `cm`'s exit code, or None if it never finished (see `error`).
    stdout: Everything `cm` wrote to stdout.
    stderr: Everything `cm` wrote to stderr. Kept separate from stdout.
    tokens: Token counts parsed from the output.
    error: Why `cm` could not be started or did not finish, or None.
  """

  args: Tuple[str, ...]
  exit_code: Optional[int]
  stdout: str
  stderr: str
  tokens: TokenUsage
  error: Optional[str] = None

  @property
  def ok(self) -> bool:
    """True when `cm` ran to completion and exited 0."""
    return self.error is None and self.exit_code == 0


def _parse_token_count(text: str) -> int:
  """Converts a token count such as "561", "41.5k" or "1.2M" to an integer.

  Raises:
    ValueError: If `text` is not a number with an optional k/M/G suffix.
  """
  text = text.strip()
  if not text:
    raise ValueError("Empty token count.")
  multiplier = _TOKEN_SUFFIXES.get(text[-1].lower())
  if multiplier:
    return int(float(text[:-1]) * multiplier)
  return int(float(text))


def parse_token_usage(output: str) -> TokenUsage:
  """Sums every `Tokens: N in / N out / N total` line in `output`.

  Every current `cm` release prints these lines, so this always parses them.
  Output with no such lines yields zero counts.

  Args:
    output: Text written by `cm`.

  Returns:
    The summed token counts.
  """
  in_tokens = out_tokens = total_tokens = 0
  for match in _TOKENS_RE.finditer(output or ""):
    try:
      counts = [_parse_token_count(group) for group in match.groups()]
    except ValueError:
      continue
    in_tokens += counts[0]
    out_tokens += counts[1]
    total_tokens += counts[2]
  return TokenUsage(in_tokens, out_tokens, total_tokens)


def _as_text(output: Union[str, bytes, None]) -> str:
  """Returns captured output as text.

  `subprocess.TimeoutExpired` can carry bytes even when the call used
  `text=True`, so partial output from a timed-out call is decoded here.
  """
  if output is None:
    return ""
  if isinstance(output, bytes):
    return output.decode("utf-8", errors="replace")
  return output


class CmAdapter:
  """Runs one `cm` binary for the new commands and processes what it prints.

  Every call:

  *   closes `cm`'s stdin, so `cm` can never read the runner's own input;
  *   captures stdout and stderr separately, so nothing `cm` prints can reach
      the runner's stdout, which carries only JSON events (ADR-0001);
  *   honours a timeout, when one is given;
  *   never raises for a `cm` failure. A binary that cannot be started, a
      timeout and a non-zero exit all come back as a `CmResult`.
  """

  def __init__(self, binary: str):
    """Initialises the adapter.

    Args:
      binary: Path to the `cm` executable, or a name to look up on PATH.
    """
    self.binary = binary

  @classmethod
  def locate(cls, binary: Optional[str] = None) -> Optional["CmAdapter"]:
    """Returns an adapter for `binary`, or for the first `cm` on PATH.

    Args:
      binary: Explicit path to `cm`. When None, `cm` is looked up on PATH.

    Returns:
      An adapter, or None when no `cm` binary could be found.
    """
    path = binary or shutil.which("cm")
    return cls(path) if path else None

  def run(
      self,
      args: Sequence[str],
      cwd: Optional[str] = None,
      env: Optional[Dict[str, str]] = None,
      timeout_sec: Optional[float] = None,
  ) -> CmResult:
    """Runs `cm` with `args` and returns the processed result.

    Args:
      args: Arguments after the binary name, e.g. `["find", "."]`.
      cwd: Working directory for `cm`.
      env: Full environment for `cm`. None inherits the runner's environment.
      timeout_sec: Seconds to wait before stopping `cm`. None waits forever.

    Returns:
      A `CmResult`. Check `ok` before trusting `stdout`.
    """
    cmd = (self.binary, *args)
    logger.debug("Running: %s", " ".join(cmd))
    try:
      completed = subprocess.run(
          list(cmd),
          cwd=cwd,
          env=env,
          stdin=subprocess.DEVNULL,
          capture_output=True,
          text=True,
          timeout=timeout_sec,
          check=False,
      )
    except subprocess.TimeoutExpired as exc:
      stdout, stderr = _as_text(exc.stdout), _as_text(exc.stderr)
      return CmResult(
          args=cmd,
          exit_code=None,
          stdout=stdout,
          stderr=stderr,
          tokens=parse_token_usage(stdout + "\n" + stderr),
          error=f"timed out after {timeout_sec} s",
      )
    except (OSError, subprocess.SubprocessError) as exc:
      return CmResult(
          args=cmd,
          exit_code=None,
          stdout="",
          stderr="",
          tokens=TokenUsage(),
          error=f"could not run: {exc}",
      )

    stdout, stderr = completed.stdout or "", completed.stderr or ""
    return CmResult(
        args=cmd,
        exit_code=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        tokens=parse_token_usage(stdout + "\n" + stderr),
    )

  def version(self, timeout_sec: float = _VERSION_TIMEOUT_SEC) -> Optional[str]:
    """Returns the bare `cm` version, e.g. "0.9.0", or None if unreadable.

    `cm` has no `version` subcommand, only the `--version` flag. Only stdout
    is parsed, so a notice `cm` prints on stderr is never taken for the
    version.

    Args:
      timeout_sec: Seconds to wait for `cm --version`.
    """
    result = self.run(["--version"], timeout_sec=timeout_sec)
    if result.error:
      logger.warning(
          "Could not read cm version from %s: %s", self.binary, result.error
      )
      return None
    if result.exit_code != 0:
      logger.warning(
          "cm --version exited %d: %s", result.exit_code, result.stderr.strip()
      )
      return None
    return _normalise_version(result.stdout)


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


def detect_cm(
    cm_binary: Optional[str] = None,
    timeout_sec: float = _VERSION_TIMEOUT_SEC,
) -> Dict[str, Any]:
  """Locates the cm binary and reads its version without ever raising.

  Args:
    cm_binary: Explicit path to cm. When None, cm is looked up on PATH.
    timeout_sec: How long to wait for `cm --version` before giving up.

  Returns:
    A dict with `found`, `version` and `path` keys. `found` is False when no
    binary could be located; `version` may be None even when found, if the
    binary exists but could not be executed, timed out, or could not be parsed.
  """
  adapter = CmAdapter.locate(cm_binary)
  if adapter is None:
    return {"found": False, "version": None, "path": None}
  return {
      "found": True,
      "version": adapter.version(timeout_sec=timeout_sec),
      "path": adapter.binary,
  }


# ---------------------------------------------------------------------------
# Legacy pipeline helpers (runners/). Kept working, not extended.
# ---------------------------------------------------------------------------

# Canonical PascalCase finding schema per docs/architecture/guardrails.md Section 6.
# cm CLI 0.7.0 (cl/974628022) switched `cm report --format json` to snake_case keys,
# so both casings are normalized to the canonical form for version-agnostic parsing.
_FINDING_KEY_ALIASES = {
    "finding_id": "FindingID",
    "session_id": "SessionID",
    "title": "Title",
    "file_path": "FilePath",
    "severity": "Severity",
    "confidence": "Confidence",
    "analysis": "Analysis",
    "snippet": "Snippet",
    "vuln_type": "VulnType",
    "vuln_id": "VulnID",
    "fingerprint": "Fingerprint",
    "status": "Status",
    "source_stage": "SourceStage",
    "finding_json": "FindingJSON",
    "updated_at": "UpdatedAt",
    "start_line": "StartLine",
    "end_line": "EndLine",
    "dismiss_reason": "DismissReason",
    "confidence_level": "ConfidenceLevel",
}


def parse_findings_json(json_str: str) -> List[Dict[str, Any]]:
  """Parses `cm report --format json` output normalizing keys to PascalCase."""
  # Imported here, not at the top, so that importing this module for
  # `CmAdapter` never loads `utils` (see the module docstring).
  from utils import extract_json_from_output  # pylint: disable=g-import-not-at-top

  data = extract_json_from_output(json_str)
  if data is None:
    logger.error("No valid JSON array or object found in report.")
    return []

  if isinstance(data, dict):
    findings = data.get("findings", data.get("items", []))
  elif isinstance(data, list):
    findings = data
  else:
    findings = []

  cleaned_findings = []
  for item in findings:
    if not isinstance(item, dict):
      continue
    cleaned = {}
    for k, v in item.items():
      if v == "":
        cleaned[k] = None
      else:
        cleaned[k] = v
      # Mirror snake_case keys onto the canonical PascalCase name. The original
      # key is retained so downstream snake_case readers keep working, and an
      # explicit PascalCase key already present in the payload always wins.
      canonical = _FINDING_KEY_ALIASES.get(k)
      if canonical and canonical not in item:
        cleaned[canonical] = cleaned[k]
    cleaned_findings.append(cleaned)

  return cleaned_findings


def extract_session_id(find_stdout: str) -> Optional[str]:
  """Extracts the CodeMender session ID from 'cm find' output."""
  # Match UUID format session ID (e.g. Session: f7f7b492-3564-4dc0-bc8f-2020554ebe24)
  match = re.search(
      r"Session:\s*([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
      find_stdout,
      re.IGNORECASE,
  )
  if match:
    return match.group(1)
  return None


def log_cm_version(
    cm_binary: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
) -> Optional[str]:
  """Runs `cm --version` and logs the CodeMender CLI binary version.

  Args:
    cm_binary: Path or name of the cm executable.
    env: Environment variables for the subprocess.
    cwd: Working directory for running the command.

  Returns:
    The output version string if successfully retrieved, or None.
  """
  # Imported here, not at the top, so that importing this module for
  # `CmAdapter` never loads `utils` (see the module docstring).
  from utils import run_command  # pylint: disable=g-import-not-at-top

  bin_path = cm_binary or shutil.which("cm") or "cm"
  try:
    res = run_command(
        [bin_path, "--version"],
        cwd=cwd,
        env=env,
        check=False,
        capture_stderr=True,
    )
    if res.returncode == 0:
      version_str = res.stdout.strip()
      if version_str:
        logger.info("CodeMender CLI version: %s", version_str)
        return version_str
      logger.warning("CodeMender CLI returned empty version output.")
    else:
      logger.warning(
          "Failed to retrieve CodeMender CLI version (exit code %d): %s",
          res.returncode,
          res.stderr.strip() if getattr(res, "stderr", None) else "",
      )
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.warning("Error checking CodeMender CLI version: %s", e)
  return None
