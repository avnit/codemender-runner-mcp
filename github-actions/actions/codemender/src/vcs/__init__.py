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

Extends `sys.path` and `vcs.__path__` to include the shared `codemender-runner`
root in both source (`../../../../codemender-runner`) and distributed
(`../../../codemender-runner`) repository layouts so `vcs.git`, `codemender.cm`,
and `cli.sarif` can be imported directly without duplicating modules.
"""

import os
import pkgutil
import sys

# TODO(ADR-0008): Remove this temporary sys.path and __path__ manipulation once
# the shared package structure from ADR-0008 is in place.
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
  sys.path.insert(0, _SRC_DIR)
for _rel in (
    "../../../../codemender-runner",
    "../../../codemender-runner",
    "../codemender-runner",
):
  _candidate = os.path.normpath(os.path.join(_SRC_DIR, _rel))
  if os.path.isdir(_candidate) and _candidate not in sys.path:
    sys.path.append(_candidate)

__path__ = pkgutil.extend_path(__path__, __name__)


