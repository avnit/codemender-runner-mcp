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

# Governing: ADR-0001, ADR-0003, ADR-0005, ADR-0007, ADR-0008, ADR-0009, SPEC-WORKER/REQ-0001, SPEC-WORKER/REQ-0002, SPEC-WORKER/REQ-0003, SPEC-WORKER/REQ-0004, SPEC-WORKER/REQ-0005, SPEC-WORKER/REQ-0006, SPEC-WORKER/REQ-0007
"""Sandbox container worker (`cm-worker`) executing tasks over ArtifactCache."""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Sequence
import contextlib
from datetime import datetime, timezone
import logging
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
from typing import Any, Self

import attrs
from codemender.cm import CmAdapter
from codemender.runner.__main__ import configure_logging
from codemender.runner.__main__ import dispatch
from codemender.sarif import SarifDocument
from codemender.sarif import SarifError

from launcher.base import EXIT_EXECUTION_FAILURE
from launcher.base import EXIT_OK
from launcher.base import EXIT_USAGE_ERROR
from launcher.cache import ArtifactCache
from launcher.cache import ArtifactCacheError
from launcher.cache import ArtifactIntegrityError
from launcher.cache import ArtifactNotFoundError
from launcher.cache import ExecutionReceipt
from launcher.cache import flatten_repo_if_nested
from launcher.cache import InvalidCacheKeyError
from launcher.cache import open_artifact_cache
from launcher.cache import RunManifest
from launcher.cache import unpack_tar_gz_preserving_permissions
from launcher.cache import ValidationError
from launcher.cache.models import _check_safe_path_segment
from launcher.cache.models import _validate_non_empty_str
from launcher.cache.models import _validate_safe_path_segment

logger = logging.getLogger(__name__)

DEFAULT_WORK_DIR = Path("/tmp/work")
EXIT_SIGINT = 130
EXIT_SIGTERM = 143
_GIT_SAFE_DIR_TIMEOUT_SECONDS = 10.0
_ROLE_BY_SUFFIX = {".sarif": "sarif", ".md": "report", ".diff": "patch"}
_VALIDATION_ERRORS = (
    ArtifactNotFoundError,
    ArtifactIntegrityError,
    InvalidCacheKeyError,
    ValidationError,
    ValueError,
    tarfile.TarError,
)


class _PreemptionSignal(Exception):
  """Internal exception raised when a preemption signal interrupts a task."""

  def __init__(self, signum: int, exit_code: int) -> None:
    self.signum, self.exit_code = signum, exit_code
    sig_name = (
        signal.Signals(signum).name
        if signum in signal.Signals._value2member_map_
        else f"SIG{signum}"
    )
    super().__init__(f"Task preempted by signal {sig_name} ({signum}).")


def _validate_work_dir(
    instance: Any, attribute: attrs.Attribute[Path], value: Path
) -> None:
  """Attrs validator ensuring `work_dir` is not a symlink or existing file."""
  del instance, attribute
  if value.is_symlink() or (value.exists() and not value.is_dir()):
    raise ValidationError(
        f"work_dir {value} must not be a symlink or existing non-directory."
    )


def _strip_str(value: Any) -> str:
  """Normalizes a CLI or config string value by stripping whitespace."""
  return value.strip() if isinstance(value, str) else str(value or "").strip()


def _normalize_work_dir(value: Path | str | None) -> Path:
  """Normalizes `work_dir`, defaulting `None` to `DEFAULT_WORK_DIR`."""
  return DEFAULT_WORK_DIR if value is None else Path(value).expanduser()


# Governing: SPEC-WORKER/REQ-0001, ADR-0001
@attrs.define(frozen=True)
class WorkerConfig:
  """Validated command-line configuration for `cm-worker`."""

  cache_uri: str = attrs.field(
      converter=_strip_str,
      validator=_validate_non_empty_str,
  )
  task_id: str = attrs.field(
      converter=_strip_str,
      validator=_validate_safe_path_segment,
  )
  work_dir: Path = attrs.field(
      default=DEFAULT_WORK_DIR,
      converter=_normalize_work_dir,
      validator=_validate_work_dir,
  )

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

  @classmethod
  def _from_args(cls, args: argparse.Namespace) -> Self:
    """Builds a validated `WorkerConfig` from parsed CLI arguments."""
    return cls(
        cache_uri=getattr(args, "cache_uri", ""),
        task_id=getattr(args, "task_id", ""),
        work_dir=getattr(args, "work_dir", DEFAULT_WORK_DIR),
    )

  @classmethod
  def from_cli(cls, args: Sequence[str] | None = None) -> Self:
    """Parses CLI `args` and returns a validated `WorkerConfig`."""
    parser = cls._build_parser()
    return cls._from_args(parser.parse_args(args))


def _fmt_iso(ts: float) -> str:
  """Formats a POSIX timestamp as an ISO 8601 UTC string."""
  return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
      "%Y-%m-%dT%H:%M:%SZ"
  )


# Governing: SPEC-WORKER/REQ-0002, SPEC-WORKER/REQ-0003, SPEC-WORKER/REQ-0004, SPEC-WORKER/REQ-0005, SPEC-WORKER/REQ-0006, SPEC-WORKER/REQ-0007
class Worker:
  """Inside-container worker executing tasks over an `ArtifactCache`."""

  def __init__(
      self,
      cache: ArtifactCache,
      *,
      work_dir: Path | str = DEFAULT_WORK_DIR,
      adapter: CmAdapter | None = None,
  ) -> None:
    self.cache = cache
    self.work_dir = Path(work_dir).expanduser()
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
    started_ts, started_monotonic = time.time(), time.monotonic()
    with (
        self._preemption_signal_handlers(),
        self._scratch_workspace() as (repo_dir, inputs_dir, out_dir, log_path),
    ):
      command_name, exit_code, error_message = self._stage_and_run_command(
          task_id, repo_dir, inputs_dir, out_dir, log_path
      )
      duration = max(0.0, round(time.monotonic() - started_monotonic, 3))
      return self._publish_results(
          task_id=task_id,
          command_name=command_name,
          exit_code=exit_code,
          error_message=error_message,
          started_ts=started_ts,
          duration_seconds=duration,
          out_dir=out_dir,
          log_path=log_path,
      )

  @contextlib.contextmanager
  def _preemption_signal_handlers(self) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
      yield
      return
    prev_sigterm = signal.getsignal(signal.SIGTERM)
    prev_sigint = signal.getsignal(signal.SIGINT)

    def _handle_signal(signum: int, _frame: Any) -> None:
      code = EXIT_SIGTERM if signum == signal.SIGTERM else EXIT_SIGINT
      raise _PreemptionSignal(signum, code)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
      yield
    finally:
      signal.signal(signal.SIGTERM, prev_sigterm)
      signal.signal(signal.SIGINT, prev_sigint)

  def _clean_work_dir(self) -> None:
    if self.work_dir.is_symlink() or self.work_dir.is_file():
      self.work_dir.unlink(missing_ok=True)
    elif self.work_dir.exists():
      shutil.rmtree(self.work_dir, ignore_errors=True)

  @contextlib.contextmanager
  def _scratch_workspace(self) -> Iterator[tuple[Path, Path, Path, Path]]:
    self._clean_work_dir()
    self.work_dir.mkdir(parents=True, exist_ok=True)
    try:
      if self.work_dir.is_symlink():
        raise ValueError(f"work_dir {self.work_dir} must not be a symlink.")
      root = self.work_dir.resolve()
      dirs = (root / "repo", root / "inputs", root / "out")
      for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)
      log_path = dirs[2] / "runner.log"
      log_path.write_text("", encoding="utf-8")
      yield dirs[0], dirs[1], dirs[2], log_path
    finally:
      self._clean_work_dir()

  def _prepare_workspace(
      self, task_id: str, repo_dir: Path, inputs_dir: Path
  ) -> tuple[RunManifest, Path | None]:
    manifest = self.cache.get_manifest(task_id)
    archive_dest = inputs_dir / "code.tar.gz"
    self.cache.get_repo_archive(archive_dest)
    unpack_tar_gz_preserving_permissions(archive_dest, repo_dir)
    flatten_repo_if_nested(repo_dir)
    if shutil.which("git") is not None:
      cmd = [
          "git",
          "config",
          "--global",
          "--add",
          "safe.directory",
          repo_dir.as_posix(),
      ]
      with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_GIT_SAFE_DIR_TIMEOUT_SECONDS,
            check=False,
        )
    staged_finding: Path | None = None
    if manifest.command in ("fix", "verify"):
      assert manifest.finding_key is not None
      staged_finding = inputs_dir / PurePosixPath(manifest.finding_key).name
      self.cache.get_finding(manifest.finding_key, staged_finding)
    return manifest, staged_finding

  def _stage_and_run_command(
      self,
      task_id: str,
      repo_dir: Path,
      inputs_dir: Path,
      out_dir: Path,
      log_path: Path,
  ) -> tuple[str, int, str | None]:
    """Stages workspace inputs and runs the manifest's `codemender` command."""
    command_name = "unknown"
    try:
      manifest, staged_finding = self._prepare_workspace(
          task_id, repo_dir, inputs_dir
      )
      command_name = manifest.command
      runner_rc = self._run_codemender(
          manifest, repo_dir, out_dir, log_path, staged_finding
      )
      if runner_rc in (EXIT_OK, EXIT_EXECUTION_FAILURE):
        return command_name, runner_rc, None
      if runner_rc == EXIT_USAGE_ERROR:
        msg = (
            f"codemender {command_name} failed with usage error (exit code 2)."
        )
        return command_name, EXIT_USAGE_ERROR, msg
      msg = (
          f"codemender {command_name} exited with unexpected code {runner_rc}."
      )
      return command_name, EXIT_EXECUTION_FAILURE, msg
    except _PreemptionSignal as sig_exc:
      self._append_log(log_path, f"{sig_exc}\n")
      return command_name, sig_exc.exit_code, str(sig_exc)
    except _VALIDATION_ERRORS as val_exc:
      self._append_log(log_path, f"Validation error: {val_exc}\n")
      return command_name, EXIT_USAGE_ERROR, str(val_exc)
    except (ArtifactCacheError, OSError) as infra_exc:
      self._append_log(log_path, f"Execution error: {infra_exc}\n")
      return command_name, EXIT_EXECUTION_FAILURE, str(infra_exc)

  def _upload_outputs(
      self, task_id: str, out_dir: Path
  ) -> tuple[dict[str, str], int]:
    artifacts: dict[str, str] = {}
    finding_count = 0
    if out_dir.is_symlink() or not out_dir.is_dir():
      return artifacts, finding_count
    for file_path in sorted(out_dir.iterdir()):
      if (
          file_path.is_symlink()
          or not file_path.is_file()
          or file_path.name == "receipt.json"
      ):
        continue
      role = (
          "log"
          if file_path.name == "runner.log"
          else _ROLE_BY_SUFFIX.get(PurePosixPath(file_path.name).suffix)
      )
      if role == "sarif":
        with contextlib.suppress(OSError, UnicodeDecodeError, SarifError):
          finding_count = SarifDocument.from_json(
              file_path.read_text(encoding="utf-8")
          ).active_result_count
      key = self.cache.put_task_output(task_id, file_path.name, file_path)
      if role is not None:
        artifacts[role] = key
    return artifacts, finding_count

  def _spill_fallback_log(
      self, log_path: Path, upload_failures: Sequence[str]
  ) -> None:
    log_content = "<runner.log unavailable>\n"
    if not log_path.is_symlink() and log_path.is_file():
      with contextlib.suppress(OSError):
        log_content = log_path.read_text(encoding="utf-8", errors="replace")
    if log_content and not log_content.endswith("\n"):
      log_content += "\n"
    reason = "; ".join(upload_failures)
    sys.stderr.write(
        f"=== [FALLBACK LOG SPILL: ArtifactCache sync failed ({reason})] ===\n"
        f"{log_content}=== [END FALLBACK LOG SPILL] ===\n"
    )
    sys.stderr.flush()

  def _build_receipt(
      self,
      *,
      task_id: str,
      command_name: str,
      exit_code: int,
      error_message: str | None,
      started_ts: float,
      duration_seconds: float,
      finding_count: int,
      artifacts: dict[str, str],
  ) -> ExecutionReceipt:
    if exit_code not in (EXIT_OK, EXIT_EXECUTION_FAILURE) and not error_message:
      error_message = f"Worker exited with code {exit_code}."
    try:
      safe_task_id = _check_safe_path_segment(task_id, "task_id")
    except ValidationError:
      safe_task_id = "invalid-task-id"
    return ExecutionReceipt(
        status="SUCCESS" if exit_code == EXIT_OK else "FAILURE",
        exit_code=exit_code,
        command=command_name,
        task_id=safe_task_id,
        started_at=_fmt_iso(started_ts),
        completed_at=_fmt_iso(time.time()),
        duration_seconds=duration_seconds,
        finding_count=finding_count,
        artifacts=artifacts,
        error=error_message,
    )

  def _publish_results(
      self,
      *,
      task_id: str,
      command_name: str,
      exit_code: int,
      error_message: str | None,
      started_ts: float,
      duration_seconds: float,
      out_dir: Path,
      log_path: Path,
  ) -> int:
    artifacts, finding_count, upload_failures = {}, 0, []
    try:
      artifacts, finding_count = self._upload_outputs(task_id, out_dir)
    except (ArtifactCacheError, OSError) as upload_exc:
      exit_code = EXIT_EXECUTION_FAILURE if exit_code == EXIT_OK else exit_code
      error_message = (
          error_message or f"Output artifact upload failed: {upload_exc}"
      )
      upload_failures.append(str(upload_exc))
    receipt = self._build_receipt(
        task_id=task_id,
        command_name=command_name,
        exit_code=exit_code,
        error_message=error_message,
        started_ts=started_ts,
        duration_seconds=duration_seconds,
        finding_count=finding_count,
        artifacts=artifacts,
    )
    try:
      self.cache.put_receipt(task_id, receipt)
    except (ArtifactCacheError, OSError) as receipt_exc:
      exit_code = EXIT_EXECUTION_FAILURE if exit_code == EXIT_OK else exit_code
      upload_failures.append(str(receipt_exc))
    if exit_code != EXIT_OK and upload_failures:
      self._spill_fallback_log(log_path, upload_failures)
    return exit_code

  def _append_log(self, log_path: Path, message: str) -> None:
    sys.stderr.write(message)
    sys.stderr.flush()
    with contextlib.suppress(OSError):
      if not log_path.parent.is_symlink():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if not log_path.is_symlink():
          with log_path.open("a", encoding="utf-8") as handle:
            handle.write(message)

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
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    root_logger.addHandler(file_handler)
    try:
      yield
    finally:
      root_logger.removeHandler(file_handler)
      file_handler.close()
      root_logger.setLevel(prev_level)

  def _run_codemender(
      self,
      manifest: RunManifest,
      repo_dir: Path,
      out_dir: Path,
      log_path: Path,
      finding: Path | None,
  ) -> int:
    """Executes the manifest's `codemender` command via shared `dispatch`.

    Args:
      manifest: Validated task manifest from `ArtifactCache`.
      repo_dir: Unpacked target Git repository directory.
      out_dir: Output directory for command artifacts and `runner.log`.
      log_path: File path to `runner.log` for capturing log records.
      finding: Optional staged SARIF finding path for `fix` or `verify`.

    Returns:
      Process exit code returned by `codemender.runner.__main__.dispatch`.
    """
    with self._capture_runner_log(log_path, manifest.log_level):
      return dispatch(
          manifest.command,
          self._resolve_adapter(),
          repo=repo_dir,
          base=manifest.base_ref or "HEAD",
          out=out_dir,
          finding=finding,
          diff_workers=manifest.diff_workers,
          auto_init="--no-init" not in manifest.extra_args,
      )


# Governing: SPEC-WORKER/REQ-0001, ADR-0001, ADR-0007
def main(argv: Sequence[str] | None = None) -> int:
  """CLI entrypoint for `cm-worker` enforcing zero-stdout purity."""
  with contextlib.redirect_stdout(sys.stderr):
    configure_logging("info")
    try:
      cfg = WorkerConfig.from_cli(argv)
      cache = open_artifact_cache(cfg.cache_uri)
      return Worker(cache, work_dir=cfg.work_dir).run_task(cfg.task_id)
    except SystemExit as exc:
      return EXIT_OK if exc.code == 0 else EXIT_USAGE_ERROR
    except (ValidationError, InvalidCacheKeyError) as exc:
      logger.error("Invalid cm-worker arguments: %s", exc)
      return EXIT_USAGE_ERROR
    except ArtifactCacheError as exc:
      logger.error("Failed to open ArtifactCache: %s", exc)
      return EXIT_EXECUTION_FAILURE
    except Exception as exc:  # pylint: disable=broad-exception-caught
      logger.exception("Unexpected error in cm-worker: %s", exc)
      return EXIT_EXECUTION_FAILURE


if __name__ == "__main__":
  sys.exit(main())
