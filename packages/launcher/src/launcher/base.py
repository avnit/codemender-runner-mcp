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

"""Launcher interface and parallel execution support for cm-runner (ADR-0001)."""

import abc
import concurrent.futures
from dataclasses import dataclass, field
import logging
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from codemender.runner import exit_codes

logger = logging.getLogger("codemender-github-actions")

# cm-runner exit codes (ADR-0001). Frozen by the contract.
EXIT_OK = exit_codes.OK
EXIT_USAGE_ERROR = exit_codes.USAGE
EXIT_EXECUTION_FAILURE = exit_codes.FAILED

DEFAULT_MAX_PARALLEL = 4


class LauncherError(Exception):
  """cm-runner could not be started or timed out."""


@dataclass
class RunResult:
  """Outcome of one cm-runner command.

  Attributes:
    exit_code: cm-runner's exit code (0 ok, 2 usage error, 3 failure).
    stdout: cm-runner's stdout, byte for byte (e.g. SARIF JSON from `find-diff`
      or unified diff from `fix`).
    events: Optional parsed NDJSON events from stdout.
  """

  exit_code: int
  stdout: bytes = field(default=b"", repr=False)
  events: List[Dict[str, Any]] = field(default_factory=list)

  def __post_init__(self):
    if isinstance(self.stdout, list):
      if not self.events:
        self.events = self.stdout
      self.stdout = b""

  @property
  def ok(self) -> bool:
    return self.exit_code == EXIT_OK

  def find_event(self, event_type: str) -> Optional[Dict[str, Any]]:
    """Returns the first stdout event whose `event` field is `event_type`."""
    for event in self.events:
      if event.get("event") == event_type:
        return event
    return None


@dataclass(frozen=True)
class ParallelTask:
  """One isolated cm-runner command to execute in `Launcher.run_parallel`.

  Attributes:
    key: Caller identifier for this task (e.g. a `Finding` or task name).
    command: cm-runner subcommand (e.g. `"fix"`).
    args: Subcommand arguments.
    input_files: Mapping of relative file paths inside `/out` to raw bytes
      written before the container starts (e.g. `{"finding-001.sarif": ...}`).
    output_file: Optional relative file path inside `/out` to read back after
      the container exits (e.g. `"finding-001.diff"`).
  """

  key: Any
  command: str
  args: Sequence[str] = ()
  input_files: Dict[str, bytes] = field(default_factory=dict, repr=False)
  output_file: Optional[str] = None


@dataclass(frozen=True)
class ParallelResult:
  """Outcome of one `ParallelTask` from `Launcher.run_parallel`.

  Attributes:
    key: The task's `key`.
    exit_code: cm-runner's exit code, or None if the container failed to start
      or timed out.
    output: Bytes read from `task.output_file` (or `b""` if absent).
    log: Captured stderr log text from the container run.
    error: Error message when the task failed or produced no output.
  """

  key: Any
  exit_code: Optional[int] = None
  output: bytes = field(default=b"", repr=False)
  log: str = field(default="", repr=False)
  error: str = ""

  @property
  def ok(self) -> bool:
    return self.exit_code == EXIT_OK and not self.error


class Launcher(abc.ABC):
  """Starts cm-runner and returns its result (single or parallel).

  Implementations decide *where* cm-runner runs (local docker, Cloud Run,
  GKE, ...) and what is passed into that environment. They must never pass
  the GitHub token to cm-runner (ADR-0002).
  """

  @abc.abstractmethod
  def run(
      self,
      command: str,
      args: Sequence[str] = (),
      log_path: Optional[str] = None,
  ) -> RunResult:
    """Runs `cm-runner <command> [args]` and waits for it to finish.

    Args:
      command: cm-runner subcommand, e.g. `"find-diff"` or `"fix"`.
      args: Extra flags for the subcommand.
      log_path: Optional file path to redirect container `stderr` logs to
        instead of streaming directly to the job log.

    Returns:
      `RunResult` with the process exit code and raw `stdout` bytes.

    Raises:
      LauncherError: If `cm-runner` could not be started or timed out.
    """

  def run_isolated(
      self,
      task: ParallelTask,
      src_workspace: str,
      temp_root: Optional[str] = None,
      timeout_seconds: Optional[int] = None,
  ) -> ParallelResult:
    """Runs one `ParallelTask` in its own isolated workspace and `/out` folder.

    Intentionally not marked `@abc.abstractmethod` so launcher implementations
    (and test doubles) that only need single-command execution via `run()` do
    not have to implement parallel container isolation.

    Args:
      task: Task descriptor with subcommand, arguments, and `/out` files.
      src_workspace: Host directory of the clean repository checkout to copy.
      temp_root: Optional parent directory for the task's temporary folder.
      timeout_seconds: Optional timeout override for this task's container run.

    Returns:
      `ParallelResult` describing the task's outcome, output bytes, and logs.

    Raises:
      NotImplementedError: If the launcher subclass does not support isolated
        parallel execution.
    """
    raise NotImplementedError

  def with_dirs(
      self,
      *,
      workspace_dir: Optional[str] = None,
      cm_home_dir: Optional[str] = None,
      out_dir: Optional[str] = None,
      timeout_seconds: Optional[int] = None,
  ) -> "Launcher":
    """Returns a launcher configured with the given host directories.

    Intentionally returns `self` by default rather than `@abc.abstractmethod`
    so test doubles that only stub `run()` do not need to implement directory
    reconfiguration.
    """
    del workspace_dir, cm_home_dir, out_dir, timeout_seconds
    return self

  def stop(self) -> None:
    """Stops any in-flight containers started by this launcher.

    Intentionally a no-op by default rather than `@abc.abstractmethod` because
    only launchers that track live background containers or remote jobs need
    explicit teardown on early cancellation.
    """

  def run_parallel(
      self,
      tasks: Sequence[ParallelTask],
      *,
      src_workspace: str,
      temp_root: Optional[str] = None,
      timeout_seconds: Optional[int] = None,
      max_workers: int = DEFAULT_MAX_PARALLEL,
      on_Log: Optional[Callable[[ParallelResult], None]] = None,
  ) -> Iterator[ParallelResult]:
    """Runs `tasks` in parallel (up to `max_workers`) in isolated containers.

    Yields each `ParallelResult` in completion order. If iteration stops early
    (e.g. on `KeyboardInterrupt` / `SIGTERM`), in-flight containers are killed
    via `self.stop()`.

    Args:
      tasks: Sequence of `ParallelTask` items to run concurrently.
      src_workspace: Host directory of the clean repository checkout to copy
        into each task's isolated workspace.
      temp_root: Optional parent directory for per-task temporary directories.
      timeout_seconds: Optional per-task container timeout in seconds.
      max_workers: Maximum number of concurrent worker threads/containers.
      on_Log: Optional callback invoked on the caller thread for each completed
        `ParallelResult` before yielding it.

    Yields:
      `ParallelResult` for each task in the order tasks complete.

    Raises:
      ValueError: If `max_workers < 1`.
    """
    if max_workers < 1:
      raise ValueError(f"max_workers must be >= 1, got {max_workers!r}.")
    if not tasks:
      return

    # Workers are I/O-bound (staging files and waiting on external container
    # subprocesses, which releases the GIL), so threads avoid multiprocessing
    # pickling overhead and allow shared container lifecycle tracking in stop().
    workers = min(len(tasks), max_workers)
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="codemender-parallel"
    )
    # ParallelTask descriptors only store lightweight strings/paths; expensive
    # workspace copying happens lazily inside run_isolated() when a worker starts.
    futures = {
        pool.submit(
            self.run_isolated,
            task,
            src_workspace,
            temp_root,
            timeout_seconds,
        ): task
        for task in tasks
    }
    finished = 0
    try:
      for future in concurrent.futures.as_completed(futures):
        finished += 1
        task = futures[future]
        try:
          result = future.result()
        except Exception as e:  # pylint: disable=broad-exception-caught
          logger.exception("Unexpected error in parallel task %r", task.key)
          result = ParallelResult(key=task.key, error=f"Unexpected error: {e}")
        if on_Log is not None:
          on_Log(result)
        yield result
    finally:
      if finished < len(futures):
        self.stop()
      pool.shutdown(wait=True)
