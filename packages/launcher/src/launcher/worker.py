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

# Governing: ADR-0001, ADR-0002, ADR-0003, ADR-0008
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0001
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0005
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0006
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0007
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0003
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0004
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0005
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0006
# Governing: SPEC-ARTIFACT-CACHE/REQ-0001, SPEC-ARTIFACT-CACHE/REQ-0002
# Governing: SPEC-ARTIFACT-CACHE/REQ-0004, SPEC-ARTIFACT-CACHE/REQ-0006
# Governing: SPEC-WORKER/REQ-0001, SPEC-WORKER/REQ-0002, SPEC-WORKER/REQ-0003
# Governing: SPEC-WORKER/REQ-0004, SPEC-WORKER/REQ-0005, SPEC-WORKER/REQ-0006
# Governing: SPEC-WORKER/REQ-0007
"""Sandbox container worker (`cm-worker`) executing tasks over ArtifactCache."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from collections.abc import Sequence
import contextlib
from datetime import UTC
from datetime import datetime
import logging
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
from typing import Any
from typing import Self

import attrs
from codemender import sarif
from codemender.cm import CmAdapter
from codemender.runner import __main__ as runner_cli
from codemender.runner import exit_codes
from launcher import cache
from launcher import validators

logger = logging.getLogger(__name__)

DEFAULT_WORK_DIR = Path("/tmp/work")
EXIT_SIGINT = 130
EXIT_SIGTERM = 143
_GIT_SAFE_DIR_TIMEOUT_SECONDS = 10.0
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_RUNNER_LOG = "runner.log"
_ARTIFACT_ROLES = {
    _RUNNER_LOG: "log",
    ".sarif": "sarif",
    ".md": "report",
    ".diff": "patch",
}
_VALIDATION_ERRORS = (
    cache.ArtifactNotFoundError,
    cache.ArtifactIntegrityError,
    cache.InvalidCacheKeyError,
    validators.ValidationError,
    ValueError,
    tarfile.TarError,
)


class _PreemptionSignal(Exception):
  """Internal exception raised when a preemption signal interrupts a task."""

  def __init__(self, signum: int) -> None:
    self.exit_code = EXIT_SIGTERM if signum == signal.SIGTERM else EXIT_SIGINT
    sig_name = signal.Signals(signum).name
    super().__init__(f"Task preempted by signal {sig_name} ({signum}).")


def _convert_work_dir(value: Path | str | None) -> Path:
  """Normalizes `work_dir` and rejects invalid, symlink, or file paths.

  Args:
    value: Candidate scratch directory path, string, or `None`.

  Returns:
    Expanded `Path` for the worker scratch directory.

  Raises:
    validators.ValidationError: If `value` is not a `Path`, non-empty `str`,
      or `None`, or if the path is a symlink or existing non-directory.
  """
  if value is None:
    path = DEFAULT_WORK_DIR
  elif isinstance(value, (str, Path)):
    raw = validators.check_non_empty_str(str(value), field_name="work_dir")
    path = (
        value.expanduser()
        if isinstance(value, Path)
        else Path(raw.strip()).expanduser()
    )
  else:
    raise validators.ValidationError(
        f"work_dir must be a Path or non-empty string, got {value!r}."
    )
  if path.is_symlink() or (path.exists() and not path.is_dir()):
    raise validators.ValidationError(
        f"work_dir {path} must not be a symlink or existing non-directory."
    )
  return path


# Governing: SPEC-WORKER/REQ-0001, ADR-0001
@attrs.define(frozen=True)
class WorkerConfig:
  """Validated command-line configuration for `cm-worker`."""

  cache_uri: str = attrs.field(
      converter=validators.strip_if_str,
      validator=validators.validate_non_empty_str,
  )
  task_id: str = attrs.field(
      converter=validators.strip_if_str,
      validator=validators.validate_safe_path_segment,
  )
  work_dir: Path = attrs.field(
      default=DEFAULT_WORK_DIR, converter=_convert_work_dir
  )

  @classmethod
  def from_cli(cls, args: Sequence[str] | None = None) -> Self:
    """Parses CLI `args` and returns a validated `WorkerConfig`."""
    return cls._from_args(cls._build_parser().parse_args(args))

  @classmethod
  def _from_args(cls, args: argparse.Namespace) -> Self:
    """Builds a validated `WorkerConfig` from parsed CLI arguments."""
    return cls(args.cache_uri, args.task_id, args.work_dir)

  @staticmethod
  def _build_parser() -> argparse.ArgumentParser:
    """Constructs the argument parser for `cm-worker`."""
    parser = argparse.ArgumentParser(
        prog="cm-worker",
        description=(
            "Sandbox container worker executing tasks via ArtifactCache."
        ),
    )
    parser.add_argument("--cache-uri", required=True, metavar="URI")
    parser.add_argument("--task-id", required=True, metavar="ID")
    parser.add_argument("--work-dir", default=DEFAULT_WORK_DIR, metavar="DIR")
    return parser


def _fmt_iso(ts: float) -> str:
  """Formats a POSIX timestamp as an ISO 8601 UTC string."""
  return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# Governing: SPEC-WORKER/REQ-0002, SPEC-WORKER/REQ-0003, SPEC-WORKER/REQ-0004
# Governing: SPEC-WORKER/REQ-0005, SPEC-WORKER/REQ-0006, SPEC-WORKER/REQ-0007
class Worker:
  """Inside-container worker executing tasks over an `ArtifactCache`."""

  def __init__(
      self,
      cache: cache.ArtifactCache,
      *,
      work_dir: Path | str = DEFAULT_WORK_DIR,
      adapter: CmAdapter | None = None,
  ) -> None:
    self.cache = cache
    self.work_dir = _convert_work_dir(work_dir)
    self.adapter = adapter

  def run_task(self, task_id: str) -> int:
    """Runs `task_id` in an isolated scratch workspace and publishes receipt.

    Stages the task manifest, repository archive, and optional finding SARIF
    from `ArtifactCache` to execute the command via
    `codemender.runner.__main__.dispatch`, then uploads all output artifacts and
    the terminal `ExecutionReceipt` back to `ArtifactCache`.

    Args:
      task_id: Logical task identifier to execute.

    Returns:
      Numeric process exit code (`0`, `2`, `3`, `130`, or `143`).
    """
    started_at, started_mono = _fmt_iso(time.time()), time.monotonic()
    with (
        self._preemption_signal_handlers(),
        self._scratch_workspace() as workspace,
    ):
      outcome = self._stage_and_run_command(task_id, workspace)
      duration = max(0.0, time.monotonic() - started_mono)
      return self._publish_results(
          task_id, outcome, started_at, duration, workspace / "out"
      )

  @contextlib.contextmanager
  def _scratch_workspace(self) -> Iterator[Path]:
    self._clean_work_dir()
    self.work_dir.mkdir(parents=True, exist_ok=True)
    try:
      if self.work_dir.is_symlink():
        raise ValueError(f"work_dir {self.work_dir} must not be a symlink.")
      root = self.work_dir.resolve()
      for subdir in ("repo", "inputs", "out"):
        (root / subdir).mkdir(parents=True, exist_ok=True)
      (root / "out" / _RUNNER_LOG).write_text("", encoding="utf-8")
      yield root
    finally:
      self._clean_work_dir()

  def _clean_work_dir(self) -> None:
    if self.work_dir.is_symlink() or self.work_dir.is_file():
      self.work_dir.unlink(missing_ok=True)
    elif self.work_dir.exists():
      shutil.rmtree(self.work_dir, ignore_errors=True)

  def _prepare_workspace(
      self, task_id: str, workspace: Path
  ) -> tuple[cache.RunManifest, Path | None]:
    repo_dir, inputs_dir = workspace / "repo", workspace / "inputs"
    manifest = self.cache.get_manifest(task_id)
    archive_dest = inputs_dir / "code.tar.gz"
    self.cache.get_repo_archive(archive_dest)
    cache.unpack_tar_gz_preserving_permissions(archive_dest, repo_dir)
    cache.flatten_repo_if_nested(repo_dir)
    git_cmd = ["git", "config", "--global", "--add", "safe.directory"]
    with contextlib.suppress(OSError, subprocess.SubprocessError):
      subprocess.run(
          [*git_cmd, repo_dir.as_posix()],
          stdin=subprocess.DEVNULL,
          capture_output=True,
          timeout=_GIT_SAFE_DIR_TIMEOUT_SECONDS,
          check=False,
      )
    if manifest.command not in ("fix", "verify") or not manifest.finding_key:
      return manifest, None
    staged_finding = inputs_dir / PurePosixPath(manifest.finding_key).name
    self.cache.get_finding(manifest.finding_key, staged_finding)
    return manifest, staged_finding

  def _stage_and_run_command(
      self, task_id: str, workspace: Path
  ) -> tuple[str, int, str | None]:
    """Stages workspace inputs and runs the manifest's `codemender` command."""
    command_name = "unknown"
    log_path = workspace / "out" / _RUNNER_LOG
    try:
      manifest, staged_finding = self._prepare_workspace(task_id, workspace)
      command_name = manifest.command
      runner_rc = self._run_codemender(manifest, workspace, staged_finding)
      if runner_rc in (exit_codes.OK, exit_codes.FAILED):
        return command_name, runner_rc, None
      if runner_rc == exit_codes.USAGE:
        err = f"codemender {command_name} failed with usage error (exit 2)."
        return command_name, exit_codes.USAGE, err
      err = f"codemender {command_name} exited with code {runner_rc}."
      return command_name, exit_codes.FAILED, err
    except _PreemptionSignal as sig_exc:
      self._append_log(log_path, f"{sig_exc}\n")
      return command_name, sig_exc.exit_code, str(sig_exc)
    except _VALIDATION_ERRORS as val_exc:
      msg = str(val_exc) or type(val_exc).__name__
      self._append_log(log_path, f"Validation error: {msg}\n")
      return command_name, exit_codes.USAGE, msg
    except (cache.ArtifactCacheError, OSError) as infra_exc:
      msg = str(infra_exc) or type(infra_exc).__name__
      self._append_log(log_path, f"Execution error: {msg}\n")
      return command_name, exit_codes.FAILED, msg

  def _run_codemender(
      self, manifest: cache.RunManifest, workspace: Path, finding: Path | None
  ) -> int:
    """Executes the manifest's `codemender` command via shared `dispatch`.

    Args:
      manifest: Validated task manifest from `ArtifactCache`.
      workspace: Scratch workspace root containing `repo` and `out`.
      finding: Optional staged SARIF finding path for `fix` or `verify`.

    Returns:
      Process exit code returned by `codemender.runner.__main__.dispatch`.
    """
    out_dir = workspace / "out"
    with self._capture_runner_log(out_dir / _RUNNER_LOG, manifest.log_level):
      return runner_cli.dispatch(
          manifest.command,
          self._resolve_adapter(),
          repo=workspace / "repo",
          base=manifest.base_ref or "HEAD",
          out=out_dir,
          finding=finding,
          diff_workers=manifest.diff_workers,
          auto_init="--no-init" not in manifest.extra_args,
      )

  def _resolve_adapter(self) -> CmAdapter:
    """Returns the cached `CmAdapter` or locates and verifies `cm` on PATH.

    Returns:
      Verified `CmAdapter` instance cached on `self.adapter`.

    Raises:
      FileNotFoundError: If `cm` is not on PATH or `cm --version` fails.
    """
    if self.adapter is not None:
      return self.adapter
    adapter = CmAdapter.locate()
    if adapter is None or adapter.version() is None:
      raise FileNotFoundError(
          "cm binary not found on PATH or failed 'cm --version'."
      )
    self.adapter = adapter
    return adapter

  @contextlib.contextmanager
  def _capture_runner_log(
      self, log_path: Path, log_level: str | None
  ) -> Iterator[None]:
    root_logger = logging.getLogger()
    prev_level = root_logger.level
    if log_level is not None:
      root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root_logger.addHandler(file_handler)
    try:
      yield
    finally:
      root_logger.removeHandler(file_handler)
      file_handler.close()
      root_logger.setLevel(prev_level)

  def _upload_outputs(
      self, task_id: str, out_dir: Path
  ) -> tuple[dict[str, str], int]:
    if out_dir.is_symlink() or not out_dir.is_dir():
      return {}, 0
    artifacts: dict[str, str] = {}
    finding_count = 0
    for path in sorted(out_dir.iterdir()):
      if path.is_symlink() or not path.is_file() or path.name == "receipt.json":
        continue
      role = _ARTIFACT_ROLES.get(path.name) or _ARTIFACT_ROLES.get(path.suffix)
      if role == "sarif":
        with contextlib.suppress(OSError, UnicodeDecodeError, sarif.SarifError):
          doc = sarif.SarifDocument.from_json(path.read_text(encoding="utf-8"))
          finding_count = doc.active_result_count
      key = self.cache.put_task_output(task_id, path.name, path)
      if role is not None:
        artifacts[role] = key
    return artifacts, finding_count

  def _publish_results(
      self,
      task_id: str,
      outcome: tuple[str, int, str | None],
      started_at: str,
      duration: float,
      out_dir: Path,
  ) -> int:
    command_name, exit_code, error_message = outcome
    artifacts, finding_count, upload_failures = {}, 0, []
    try:
      artifacts, finding_count = self._upload_outputs(task_id, out_dir)
    except (cache.ArtifactCacheError, OSError) as exc:
      exit_code = exit_code or exit_codes.FAILED
      error_message = error_message or f"Output artifact upload failed: {exc}"
      upload_failures.append(str(exc))
    try:
      receipt = cache.ExecutionReceipt(
          status="SUCCESS" if exit_code == exit_codes.OK else "FAILURE",
          exit_code=exit_code,
          command=command_name,
          task_id=task_id,
          started_at=started_at,
          completed_at=_fmt_iso(time.time()),
          duration_seconds=duration,
          finding_count=finding_count,
          artifacts=artifacts,
          error=error_message,
      )
      self.cache.put_receipt(task_id, receipt)
    except (
        cache.ArtifactCacheError,
        OSError,
        validators.ValidationError,
    ) as exc:
      exit_code = exit_code or exit_codes.FAILED
      upload_failures.append(str(exc))
    if exit_code != exit_codes.OK and upload_failures:
      self._spill_fallback_log(out_dir / _RUNNER_LOG, upload_failures)
    return exit_code

  def _spill_fallback_log(
      self, log_path: Path, failures: Sequence[str]
  ) -> None:
    log_content = "<runner.log unavailable>\n"
    if not log_path.is_symlink() and log_path.is_file():
      with contextlib.suppress(OSError):
        log_content = log_path.read_text(encoding="utf-8", errors="replace")
    if log_content and not log_content.endswith("\n"):
      log_content += "\n"
    reason = "; ".join(failures)
    sys.stderr.write(
        f"=== [FALLBACK LOG SPILL: ArtifactCache sync failed ({reason})] ===\n"
        f"{log_content}=== [END FALLBACK LOG SPILL] ===\n"
    )
    sys.stderr.flush()

  def _append_log(self, log_path: Path, message: str) -> None:
    sys.stderr.write(message)
    sys.stderr.flush()
    with contextlib.suppress(OSError):
      if not log_path.parent.is_symlink() and not log_path.is_symlink():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
          handle.write(message)

  @contextlib.contextmanager
  def _preemption_signal_handlers(self) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
      yield
      return

    def _raise_preemption(signum: int, _frame: Any) -> None:
      raise _PreemptionSignal(signum)

    prev = {
        sig: signal.signal(sig, _raise_preemption)
        for sig in (signal.SIGTERM, signal.SIGINT)
    }
    try:
      yield
    finally:
      for sig, handler in prev.items():
        signal.signal(sig, handler)


# Governing: SPEC-WORKER/REQ-0001, ADR-0001
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0002
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0004
def main(argv: Sequence[str] | None = None) -> int:
  """CLI entrypoint for `cm-worker` enforcing zero-stdout purity."""
  with contextlib.redirect_stdout(sys.stderr):
    runner_cli.configure_logging("info")
    try:
      cfg = WorkerConfig.from_cli(argv)
      store = cache.open_artifact_cache(cfg.cache_uri)
      return Worker(store, work_dir=cfg.work_dir).run_task(cfg.task_id)
    except SystemExit as exc:
      return exit_codes.OK if exc.code == 0 else exit_codes.USAGE
    except (validators.ValidationError, cache.InvalidCacheKeyError) as exc:
      logger.error("Invalid cm-worker arguments: %s", exc)
      return exit_codes.USAGE
    except cache.ArtifactCacheError as exc:
      logger.error("Failed to open ArtifactCache: %s", exc)
      return exit_codes.FAILED
    except Exception:  # pylint: disable=broad-exception-caught
      logger.exception("Unexpected error in cm-worker.")
      return exit_codes.FAILED


if __name__ == "__main__":
  sys.exit(main())
