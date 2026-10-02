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

"""Vanilla launcher: runs cm-runner with `docker run` on the GitHub Actions VM.

Why `docker run` and not a job-level `container:`: with `container:`, every
step of the job (including the ones holding GITHUB_TOKEN) runs inside the same
environment as the agent. With `docker run`, cm-runner is a separate process
that only receives what we explicitly pass in. `docker run` does not inherit
the host environment, so GITHUB_TOKEN never reaches the container.

Container runtime (`runtime`):
  runsc (default)  gVisor. The container gets `--cap-add SYS_ADMIN` so that
                   cm's own sandbox can create its namespaces. Under runsc
                   that capability is granted by gVisor's user-space kernel
                   only, never by the runner VM's real kernel.
  runc             Docker's default runtime, no extra capabilities. cm's
                   sandbox can't start under it (`operation not permitted`),
                   so this is only an explicit opt-out.

Never added, whatever the runtime: `--privileged`, any
`--security-opt ...=unconfined`, or any `-e`/`--env` flag.
"""

import logging
import subprocess
from typing import List, Sequence

from launcher.base import Launcher, LauncherError, RunResult, parse_ndjson

logger = logging.getLogger("codemender-github-actions")

_DEFAULT_TIMEOUT_SECONDS = 60 * 60

RUNTIME_GVISOR = "runsc"
RUNTIME_RUNC = "runc"
SUPPORTED_RUNTIMES = (RUNTIME_GVISOR, RUNTIME_RUNC)

# Extra `docker run` flags per runtime. SYS_ADMIN is safe only under runsc,
# where gVisor's own kernel grants it.
_RUNTIME_FLAGS = {
    RUNTIME_GVISOR: ("--runtime=runsc", "--cap-add", "SYS_ADMIN"),
    RUNTIME_RUNC: (),
}


class LocalDockerLauncher(Launcher):
  """Runs cm-runner in a throwaway container on the local Docker daemon."""

  def __init__(
      self,
      image: str,
      log_level: str = "info",
      timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
      docker_binary: str = "docker",
      runtime: str = RUNTIME_GVISOR,
  ):
    """Initializes the launcher.

    Args:
      image: cm-runner image, pinned to a commit SHA tag.
      log_level: Passed to `cm-runner --log-level`. Logs go to stderr.
      timeout_seconds: Hard limit for one cm-runner command.
      docker_binary: Docker CLI to invoke (overridable for tests).
      runtime: Container runtime, `runsc` (gVisor, default) or `runc`.

    Raises:
      ValueError: if `image` is empty or `runtime` is not supported.
    """
    if not image:
      raise ValueError("A cm-runner image is required.")
    if runtime not in SUPPORTED_RUNTIMES:
      raise ValueError(
          f"Unsupported container runtime {runtime!r}; expected one of"
          f" {', '.join(SUPPORTED_RUNTIMES)}."
      )
    self.image = image
    self.log_level = log_level
    self.timeout_seconds = timeout_seconds
    self.docker_binary = docker_binary
    self.runtime = runtime

  def build_command(self, command: str, args: Sequence[str] = ()) -> List[str]:
    """Builds the `docker run` argv for one cm-runner command.

    The image's ENTRYPOINT is `cm-runner`, so everything after the image name
    is passed to cm-runner. No `-e`/`--env` flags are added: the container
    gets no host environment variables, and in particular no GitHub token.
    The only runtime-dependent flags are the ones in `_RUNTIME_FLAGS`.
    """
    return [
        self.docker_binary,
        "run",
        "--rm",
        *_RUNTIME_FLAGS[self.runtime],
        self.image,
        "--log-level",
        self.log_level,
        command,
        *args,
    ]

  def run(self, command: str, args: Sequence[str] = ()) -> RunResult:
    argv = self.build_command(command, args)
    logger.info("Starting cm-runner: %s", " ".join(argv))
    try:
      # stdout is captured (NDJSON contract). stderr is inherited so cm-runner
      # logs stream live into the GitHub Actions log (ADR-0001).
      proc = subprocess.run(
          argv,
          stdout=subprocess.PIPE,
          stderr=None,
          text=True,
          timeout=self.timeout_seconds,
          check=False,
      )
    except FileNotFoundError as e:
      raise LauncherError(
          f"Docker CLI {self.docker_binary!r} not found on this runner."
      ) from e
    except subprocess.TimeoutExpired as e:
      raise LauncherError(
          f"cm-runner {command!r} timed out after {self.timeout_seconds}s."
      ) from e

    events = parse_ndjson(proc.stdout or "")
    return RunResult(exit_code=proc.returncode, events=events)
