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
from typing import Mapping, Optional, Sequence

from repohost.github import gha_runner


def main(
    env: Optional[Mapping[str, str]] = None,
    argv: Optional[Sequence[str]] = None,
) -> int:
  """Dispatches the `cm-github` CLI entrypoint to `gha_runner.main`."""
  if env is None and argv is None:
    return gha_runner.main()
  if argv is None:
    return gha_runner.main(env)  # type: ignore[arg-type]
  if env is None:
    return gha_runner.main(argv=argv)
  return gha_runner.main(env, argv=argv)


if __name__ == "__main__":
  sys.exit(main())
