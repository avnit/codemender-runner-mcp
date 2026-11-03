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
"""Local Docker / gVisor container execution backend (ADR-0008)."""

from launcher.local.local_docker import (
    CONTAINER_CM_HOME_DIR,
    CONTAINER_GCP_CREDENTIALS_PATH,
    CONTAINER_OUT_DIR,
    CONTAINER_WORKSPACE_DIR,
    LocalDockerLauncher,
    RUNTIME_GVISOR,
    RUNTIME_RUNC,
    SUPPORTED_RUNTIMES,
    make_world_writable,
)
