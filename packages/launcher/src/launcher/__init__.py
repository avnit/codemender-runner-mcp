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

# Governing: ADR-0008, SPEC-GHA/REQ-0004
"""CodeMender sandbox execution backends and launcher interface (ADR-0008).

Exports only shared base interfaces and data models from `launcher.base`.
Target subpackages (`launcher.local`, `launcher.cloudrun`) are imported lazily
by callers per ADR-0008 Decision 3 so optional extras never trigger
`ModuleNotFoundError`.
"""

from launcher.base import (
    DEFAULT_MAX_PARALLEL,
    EXIT_EXECUTION_FAILURE,
    EXIT_OK,
    EXIT_USAGE_ERROR,
    Launcher,
    LauncherError,
    ParallelResult,
    ParallelTask,
    RunResult,
)
