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
"""ArtifactCache protocol seam, wire models, and safe archive unpacking utilities."""

from pathlib import Path
from typing import Protocol

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


# TODO(b/575622914): Replace protocol/error stubs once SPEC-ARTIFACT-CACHE lands in launcher.cache.
class ArtifactCacheError(Exception):
  """Base exception for ArtifactCache storage and protocol errors."""


class InvalidCacheKeyError(ArtifactCacheError, ValueError):
  """Raised when a cache key, segment identifier, or URI is invalid."""


class ArtifactNotFoundError(ArtifactCacheError, FileNotFoundError):
  """Raised when a requested cache key or local source file does not exist."""


class ArtifactIntegrityError(ArtifactCacheError, ValueError):
  """Raised when a cached JSON or wire model payload is corrupt or invalid."""


class ArtifactCache(Protocol):
  """Protocol seam for ArtifactCache operations required by `Worker`."""

  def get_manifest(self, task_id: str) -> RunManifest: ...
  def get_repo_archive(self, dest: Path | str) -> Path: ...
  def get_finding(self, finding_key: str, dest: Path | str) -> Path: ...
  def put_task_output(
      self, task_id: str, filename: str, src: Path | str
  ) -> str: ...
  def put_receipt(self, task_id: str, receipt: ExecutionReceipt) -> None: ...


def open_artifact_cache(uri: str | Path) -> ArtifactCache:
  """Opens an `ArtifactCache` instance for `uri`."""
  if not isinstance(uri, Path) and (
      not isinstance(uri, str) or not uri.strip() or "\x00" in uri
  ):
    raise InvalidCacheKeyError(f"Invalid cache URI: {uri!r}")
  raise ArtifactCacheError(
      f"ArtifactCache backend for {uri!r} is not yet wired"
      " (pending SPEC-ARTIFACT-CACHE)."
  )


__all__ = [
    "ArtifactCache",
    "ArtifactCacheError",
    "ArtifactIntegrityError",
    "ArtifactNotFoundError",
    "ExecutionReceipt",
    "InvalidCacheKeyError",
    "MAX_DIFF_WORKERS",
    "MIN_DIFF_WORKERS",
    "RunManifest",
    "VALID_RECEIPT_STATUSES",
    "VALID_WORKER_COMMANDS",
    "ValidationError",
    "flatten_repo_if_nested",
    "open_artifact_cache",
    "unpack_tar_gz_preserving_permissions",
]
