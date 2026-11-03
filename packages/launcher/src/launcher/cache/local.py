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

# Governing: ADR-0005, ADR-0009, SPEC-ARTIFACT-CACHE/REQ-0001, SPEC-ARTIFACT-CACHE/REQ-0002
"""LocalArtifactCache implementation backed by fsspec LocalFileSystem."""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

import fsspec

from launcher.cache.base import ArtifactCache, normalize_cache_key
from launcher.cache.errors import (
    ArtifactCacheError,
    ArtifactNotFoundError,
    InvalidCacheKeyError,
    translate_fs_error,
)


def _parse_file_uri(raw: str) -> Path:
  """Parses and validates a `file://` URI into a local filesystem `Path`."""
  parsed = urlparse(raw)
  if parsed.params or parsed.query or parsed.fragment:
    raise InvalidCacheKeyError(
        f"file:// URI must not contain params, query, or fragment: {raw!r}",
        op="open_artifact_cache",
        uri=raw,
    )
  if parsed.netloc not in ("", "localhost"):
    raise InvalidCacheKeyError(
        f"file:// URI authority {parsed.netloc!r} is not supported: {raw!r}",
        op="open_artifact_cache",
        uri=raw,
    )
  if hasattr(Path, "from_uri"):
    if not parsed.path:
      raise InvalidCacheKeyError(
          f"file:// URI must specify an absolute path: {raw!r}",
          op="open_artifact_cache",
          uri=raw,
      )
    try:
      return Path.from_uri(raw)  # type: ignore[attr-defined]
    except ValueError as exc:
      raise InvalidCacheKeyError(
          f"file:// URI must specify a valid path: {raw!r}",
          op="open_artifact_cache",
          uri=raw,
      ) from exc
  path_str = unquote(parsed.path)
  if not path_str.startswith("/"):
    raise InvalidCacheKeyError(
        f"file:// URI must specify an absolute path: {raw!r}",
        op="open_artifact_cache",
        uri=raw,
    )
  return Path(path_str)


# Governing: SPEC-ARTIFACT-CACHE/REQ-0002
def parse_local_uri_or_path(uri_or_path: str | Path) -> Path:
  """Converts a local `Path`, path string, or `file://` URI into a `Path`."""
  if isinstance(uri_or_path, Path):
    raw_path = str(uri_or_path)
    if not raw_path.strip() or "\x00" in raw_path:
      raise InvalidCacheKeyError(
          "Local cache URI or path must be a non-empty path without NUL bytes",
          op="open_artifact_cache",
          uri=raw_path,
      )
    return uri_or_path

  if (
      not isinstance(uri_or_path, str)
      or not uri_or_path.strip()
      or "\x00" in uri_or_path
  ):
    raise InvalidCacheKeyError(
        "Local cache URI or path must be a non-empty string or Path without"
        " NUL bytes",
        op="open_artifact_cache",
        uri=str(uri_or_path),
    )

  raw = uri_or_path.strip()
  path = _parse_file_uri(raw) if raw.startswith("file:") else Path(raw)
  if "\x00" in str(path):
    raise InvalidCacheKeyError(
        f"file:// URI must specify a valid path without NUL bytes: {raw!r}",
        op="open_artifact_cache",
        uri=raw,
    )
  return path


# Governing: SPEC-ARTIFACT-CACHE/REQ-0001, SPEC-ARTIFACT-CACHE/REQ-0002, ADR-0009
class LocalArtifactCache(ArtifactCache):
  """Local filesystem ArtifactCache backed by fsspec LocalFileSystem."""

  def __init__(
      self,
      root_dir: str | Path,
      *,
      uri: str | None = None,
  ) -> None:
    root_path = parse_local_uri_or_path(root_dir)
    try:
      expanded = root_path.expanduser()
      expanded.mkdir(parents=True, exist_ok=True)
      resolved_root = expanded.resolve()
    except Exception as exc:
      raise translate_fs_error(
          exc,
          op="init",
          uri=str(root_dir),
      ) from exc

    self.root_dir = resolved_root
    super().__init__(
        fs=fsspec.filesystem("file"),
        root_path=resolved_root.as_posix(),
        uri=uri or resolved_root.as_uri(),
    )

  def put_bytes(self, key: str, data: bytes) -> None:
    """Atomically writes raw bytes to the local cache via a .tmp sibling."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
      raise TypeError(
          f"put_bytes expected bytes-like object, got {type(data).__name__}"
      )
    payload = bytes(data)
    target_path = Path(self._resolve_full_path(key, op="put_bytes"))
    self._publish_staged(
        target_path,
        key=key,
        op="put_bytes",
        stage=lambda tmp: self._fs.pipe_file(tmp.as_posix(), payload),
    )

  def put_file(self, key: str, src: Path | str) -> None:
    """Atomically copies a local regular file into the cache via .tmp."""
    target_path = Path(self._resolve_full_path(key, op="put_file"))
    src_path = Path(src)
    if src_path.is_symlink() or not src_path.is_file():
      raise ArtifactNotFoundError(
          "Local source file does not exist or is not a regular file:"
          f" {src_path}",
          op="put_file",
          uri=self._uri,
          key=key,
      )
    self._publish_staged(
        target_path,
        key=key,
        op="put_file",
        stage=lambda tmp: self._fs.put_file(
            src_path.as_posix(), tmp.as_posix()
        ),
    )

  def _resolve_full_path(self, key: str, *, op: str) -> str:
    norm_key = normalize_cache_key(
        key,
        uri=self._uri,
        op=op,
    )
    try:
      target_path = self._check_no_symlinks_and_resolve(norm_key, op=op)
    except ArtifactCacheError:
      raise
    except OSError as exc:
      # e.g. EACCES while lstat-ing an intermediate directory.
      raise translate_fs_error(exc, op=op, uri=self._uri, key=key) from exc
    return target_path.as_posix()

  def _check_no_symlinks_and_resolve(self, norm_key: str, *, op: str) -> Path:
    """Verifies no segment inside root_dir is a symlink or escapes root_dir."""
    current = self.root_dir
    for part in PurePosixPath(norm_key).parts:
      current = current / part
      if current.is_symlink():
        raise InvalidCacheKeyError(
            f"Symlink segment {part!r} is not allowed inside"
            " LocalArtifactCache",
            op=op,
            uri=self._uri,
            key=norm_key,
        )

    resolved_target = current.resolve()
    if not resolved_target.is_relative_to(self.root_dir):
      raise InvalidCacheKeyError(
          f"Resolved key {norm_key!r} escapes cache root {self.root_dir}",
          op=op,
          uri=self._uri,
          key=norm_key,
      )
    return current

  def _publish_staged(
      self,
      target_path: Path,
      *,
      key: str,
      op: str,
      stage: Callable[[Path], None],
  ) -> None:
    """Stages into a sibling temp file, then commits via `os.replace()`."""
    tmp_path = self._staging_sibling(target_path)
    try:
      target_path.parent.mkdir(parents=True, exist_ok=True)
      stage(tmp_path)
      if not tmp_path.is_file() or tmp_path.is_symlink():
        raise FileNotFoundError(
            f"Staging write did not materialize file for key: {key}"
        )
      os.replace(tmp_path, target_path)
    except Exception as exc:
      self._discard_staging(tmp_path)
      raise translate_fs_error(exc, op=op, uri=self._uri, key=key) from exc
