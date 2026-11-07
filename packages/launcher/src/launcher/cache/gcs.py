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

# Governing: SPEC-ARTIFACT-CACHE/REQ-0001
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0002
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0006
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0002
# Governing: SPEC-ARTIFACT-CACHE/REQ-0002, SPEC-ARTIFACT-CACHE/REQ-0003
"""GcsArtifactCache implementation backed by gcsfs.GCSFileSystem."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from launcher import validators
from launcher.cache.base import ArtifactCache, normalize_cache_key
from launcher.cache.errors import (
    CacheConfigurationError,
    InvalidCacheKeyError,
    translate_fs_error,
)


# Governing: SPEC-ARTIFACT-CACHE/REQ-0002
def parse_gcs_uri(uri: str) -> tuple[str, str]:
  """Parses a `gs://<bucket>[/<prefix>]` URI into `(bucket, prefix)`."""
  if (
      not isinstance(uri, str)
      or "\x00" in uri
      or not uri.startswith("gs://")
  ):
    raise InvalidCacheKeyError(
        f"GCS cache URI must start with 'gs://' without NUL bytes, got {uri!r}",
        op="open_artifact_cache",
        uri=str(uri),
    )

  parsed = urlparse(uri)
  if parsed.params or parsed.query or parsed.fragment:
    raise InvalidCacheKeyError(
        "GCS cache URI must not contain params, query, or fragment:"
        f" {uri!r}",
        op="open_artifact_cache",
        uri=uri,
    )

  bucket = parsed.netloc.strip()
  bucket_reason = validators.single_segment_error(bucket)
  if bucket_reason is not None:
    raise InvalidCacheKeyError(
        f"GCS cache URI has an invalid bucket name ({bucket_reason}): {uri!r}",
        op="open_artifact_cache",
        uri=uri,
    )

  raw_path = parsed.path
  if "//" in raw_path.strip("/"):
    raise InvalidCacheKeyError(
        f"GCS cache URI prefix must not contain empty segments ('//'): {uri!r}",
        op="open_artifact_cache",
        uri=uri,
    )
  raw_prefix = raw_path.lstrip("/").rstrip("/")
  if not raw_prefix:
    return bucket, ""

  norm_prefix = normalize_cache_key(
      raw_prefix,
      uri=uri,
      op="open_artifact_cache",
  )
  return bucket, norm_prefix


# Governing: SPEC-ARTIFACT-CACHE/REQ-0001
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0002
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0006
# Governing: SPEC-ARTIFACT-CACHE/REQ-0003
class GcsArtifactCache(ArtifactCache):
  """Google Cloud Storage ArtifactCache implementation using gcsfs."""

  def __init__(
      self,
      uri: str,
      **fs_kwargs: Any,
  ) -> None:
    """Initializes the cache for `uri`.

    Args:
      uri: `gs://<bucket>[/<prefix>]` cache root URI.
      **fs_kwargs: Extra keyword arguments for `gcsfs.GCSFileSystem` (such as
        `project` or `token`). The listings cache is always disabled so
        objects written by other processes are visible immediately; transient
        HTTP 429/5xx retries come from gcsfs's built-in retry policy
        (`GCSFileSystem.retries`).

    Raises:
      InvalidCacheKeyError: If `uri` is not a valid `gs://` URI.
      CacheConfigurationError: If `gcsfs` is not installed or credentials
        cannot be loaded.
    """
    bucket, prefix = parse_gcs_uri(uri)
    self.bucket = bucket
    self.prefix = prefix

    root_path = f"{bucket}/{prefix}" if prefix else bucket
    canonical_uri = f"gs://{root_path}"

    try:
      # pylint: disable-next=import-outside-toplevel
      import gcsfs  # type: ignore[import-untyped]
    except ImportError as exc:
      raise CacheConfigurationError(
          "GcsArtifactCache requires 'gcsfs'. Install the cloudrun extra"
          " with: pip install 'codemender-launcher[cloudrun]'",
          op="open_artifact_cache",
          uri=canonical_uri,
      ) from exc

    try:
      # `use_listings_cache=False` (rather than `cache_timeout=0`) because
      # older gcsfs releases treat a zero timeout as "never expire".
      fs = gcsfs.GCSFileSystem(**{**fs_kwargs, "use_listings_cache": False})
    except Exception as exc:
      raise translate_fs_error(
          exc,
          op="open_artifact_cache",
          uri=canonical_uri,
      ) from exc

    super().__init__(
        fs=fs,
        root_path=root_path,
        uri=canonical_uri,
    )

  def _resolve_full_path(self, key: str, *, op: str) -> str:
    """Normalizes a relative key and joins it onto `<bucket>/<prefix>`."""
    norm_key = normalize_cache_key(
        key,
        uri=self._uri,
        op=op,
    )
    return self._join_root(norm_key)
