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

# Governing: ADR-0007, ADR-0009, SPEC-WORKER/REQ-0002, SPEC-WORKER/REQ-0003, SPEC-WORKER/REQ-0004, SPEC-WORKER/REQ-0006
"""ArtifactCache wire models and safe archive unpacking utilities."""

from launcher.cache.archive import (
    flatten_repo_if_nested,
    unpack_tar_gz_preserving_permissions,
)
from launcher.cache.models import (
    ExecutionReceipt,
    MAX_DIFF_WORKERS,
    MIN_DIFF_WORKERS,
    RunManifest,
    VALID_RECEIPT_STATUSES,
    VALID_WORKER_COMMANDS,
    ValidationError,
)

__all__ = [
    "ExecutionReceipt",
    "MAX_DIFF_WORKERS",
    "MIN_DIFF_WORKERS",
    "RunManifest",
    "VALID_RECEIPT_STATUSES",
    "VALID_WORKER_COMMANDS",
    "ValidationError",
    "flatten_repo_if_nested",
    "unpack_tar_gz_preserving_permissions",
]
