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

"""VCS and GitHub integration subpackage.

Extends `sys.path` and `vcs.__path__` to include `packages/codemender/src` and
the legacy `codemender-runner` root in both source and distributed repository
layouts so `codemender.vcs.git` (and `vcs.git`), `codemender.cm`, and
`codemender.sarif` can be imported directly without duplicating modules.
"""

import os
import pkgutil
import sys

# TODO(ADR-0008): Remove this temporary sys.path and __path__ manipulation once
# the full three-package workspace (repohost / launcher) from ADR-0008 is complete.
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
  sys.path.insert(0, _SRC_DIR)

for _rel in (
    "../packages/codemender/src",
    "../../packages/codemender/src",
    "../../../../packages/codemender/src",
    "../../../codemender-runner/packages/codemender/src",
    "../../../packages/codemender/src",
    "../../../../codemender-runner",
    "../../../codemender-runner",
    "../codemender-runner",
):
  _candidate = os.path.normpath(os.path.join(_SRC_DIR, _rel))
  if os.path.isdir(_candidate) and _candidate not in sys.path:
    sys.path.append(_candidate)

__path__ = pkgutil.extend_path(__path__, __name__)
for _pkg_rel in (
    "../packages/codemender/src/codemender/vcs",
    "../../packages/codemender/src/codemender/vcs",
    "../../../../packages/codemender/src/codemender/vcs",
    "../../../codemender-runner/packages/codemender/src/codemender/vcs",
    "../../../packages/codemender/src/codemender/vcs",
):
  _vcs_candidate = os.path.normpath(os.path.join(_SRC_DIR, _pkg_rel))
  if os.path.isdir(_vcs_candidate) and _vcs_candidate not in __path__:
    __path__.append(_vcs_candidate)


