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

# Governing: docs/openspec/specs/distribute/spec.md#req-0005, SPEC-DIST, ADR-0008

"""CLI entrypoint shim for the `cm-github` console script."""

from pathlib import Path
import sys


def main() -> int:
  """Locates and delegates to `gha_runner.main()`."""
  # TODO(b/575573750): Remove sys.path manipulation once gha_runner.py is migrated to packages/repohost per ADR-0008.
  action_src = (
      Path(__file__).resolve().parents[5]
      / ".github"
      / "actions"
      / "codemender"
      / "src"
  )
  if action_src.is_dir() and str(action_src) not in sys.path:
    sys.path.insert(0, str(action_src))
  import gha_runner  # pylint: disable=g-import-not-at-top

  return gha_runner.main()


if __name__ == "__main__":
  sys.exit(main())
