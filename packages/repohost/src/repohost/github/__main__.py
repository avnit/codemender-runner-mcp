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

# Governing: ADR-0008, SPEC-GHA/REQ-0004, SPEC-DIST/REQ-0005

"""CLI entrypoint for the `cm-github` console script."""

import sys
from typing import Any, Dict, Mapping, Optional, Sequence

from repohost.base import RepoHost
from repohost.github import gha_runner
from repohost.store import ArtifactStore


def main(
    env: Optional[Mapping[str, str]] = None,
    argv: Optional[Sequence[str]] = None,
    host: Optional[RepoHost] = None,
    store: Optional[ArtifactStore] = None,
) -> int:
  """Dispatches the `cm-github` CLI entrypoint to `gha_runner.main`."""
  kwargs: Dict[str, Any] = {}
  if host is not None:
    kwargs["host"] = host
  if store is not None:
    kwargs["store"] = store
  if argv is not None:
    kwargs["argv"] = argv
  if env is not None:
    return gha_runner.main(env, **kwargs)
  return gha_runner.main(**kwargs)


if __name__ == "__main__":
  sys.exit(main())
