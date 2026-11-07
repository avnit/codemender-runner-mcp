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

# Governing: SPEC-ARTIFACT-CACHE/REQ-0001, SPEC-ARTIFACT-CACHE/REQ-0002
# Governing: SPEC-ARTIFACT-CACHE/REQ-0003, SPEC-ARTIFACT-CACHE/REQ-0004
# Governing: SPEC-ARTIFACT-CACHE/REQ-0006
# Governing: SPEC-WORKER/REQ-0002, SPEC-WORKER/REQ-0003
# Governing: SPEC-WORKER/REQ-0004, SPEC-WORKER/REQ-0006
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0002
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0003
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0006
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0002
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0003
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0004
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0006
"""ArtifactCache public interface, factory, wire models, and archive helpers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from launcher.cache.archive import (
    flatten_repo_if_nested,
    unpack_tar_gz_preserving_permissions,
)
from launcher.cache.base import (
    ArtifactCache,
    build_gcs_cache_uri,
    normalize_cache_key,
    sanitize_branch_name,
    validate_single_segment,
)
from launcher.cache.errors import (
    ArtifactCacheError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    CacheConfigurationError,
    CachePermissionError,
    CacheTransientError,
    InvalidCacheKeyError,
    translate_fs_error,
)
from launcher.cache.local import LocalArtifactCache
from launcher.cache.models import (
    ExecutionReceipt,
    MAX_DIFF_WORKERS,
    MIN_DIFF_WORKERS,
    RunManifest,
    VALID_RECEIPT_STATUSES,
    VALID_WORKER_COMMANDS,
    ValidationError,
)

if TYPE_CHECKING:
  from launcher.cache.gcs import GcsArtifactCache


# Governing: SPEC-ARTIFACT-CACHE/REQ-0001, SPEC-ARTIFACT-CACHE/REQ-0002, SPEC-ARTIFACT-CACHE/REQ-0003
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0002
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0006
def open_artifact_cache(uri: str | Path, **kwargs: Any) -> ArtifactCache:
  """Opens a `LocalArtifactCache` or `GcsArtifactCache` based on URI scheme.

  Enforces the CodeMender Runner System Design Two-Way Door lazy-import invariant: `GcsArtifactCache`
  (and `gcsfs`) is imported only when `uri` uses the `'gs://'` scheme.

  Args:
    uri: Local directory `Path`, `file://` URI, or `gs://<bucket>[/<prefix>]`
      URI.
    **kwargs: Optional backend keyword arguments forwarded to the cache class.

  Returns:
    Configured `LocalArtifactCache` or `GcsArtifactCache` instance.

  Raises:
    InvalidCacheKeyError: If `uri` is empty, contains NUL bytes, or does not
      use a supported `'file://'` or `'gs://'` scheme.
    CacheConfigurationError: If `uri` is a `gs://` URI and `gcsfs` or cloud
      credentials are unavailable.
  """
  if isinstance(uri, Path):
    return LocalArtifactCache(uri, **kwargs)

  if not isinstance(uri, str) or not uri.strip() or "\x00" in uri:
    raise InvalidCacheKeyError(
        "Cache URI must be a non-empty string or Path without NUL bytes",
        op="open_artifact_cache",
        uri=str(uri),
    )

  raw = uri.strip()
  protocol, sep, _ = raw.lower().partition("://")
  if not sep or not protocol:
    # Missing "://" or nothing before "://" falls through to the default case.
    protocol = ""

  match protocol:
    case "file":
      return LocalArtifactCache(raw, **kwargs)
    case "gs":
      # pylint: disable-next=import-outside-toplevel
      from launcher.cache.gcs import GcsArtifactCache

      return GcsArtifactCache(raw, **kwargs)
    case _:
      raise InvalidCacheKeyError(
          f"Unsupported ArtifactCache URI scheme {protocol!r}; expected"
          " 'file://' or 'gs://'",
          op="open_artifact_cache",
          uri=raw,
      )


def __getattr__(name: str) -> Any:
  """Lazily resolves `GcsArtifactCache` on `launcher.cache` (PEP 562).

  Per the CodeMender Runner System Design Two-Way Door invariant, importing `launcher.cache` or opening
  a local `file://` cache must never import `gcsfs` or Google Cloud libraries
  (which live in the optional `codemender-launcher[cloudrun]` extra). Module
  `__getattr__` defers importing `launcher.cache.gcs` until a caller explicitly
  accesses `launcher.cache.GcsArtifactCache`.
  """
  if name == "GcsArtifactCache":
    # pylint: disable-next=import-outside-toplevel
    from launcher.cache.gcs import GcsArtifactCache

    return GcsArtifactCache
  raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
