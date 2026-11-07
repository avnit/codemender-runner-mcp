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
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0003
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0006
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0002
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0006
"""Unit and concurrency tests for launcher.cache.local (LocalArtifactCache)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import os
from pathlib import Path
from typing import Any

import pytest

from launcher.cache import open_artifact_cache
from launcher.cache.errors import (
    ArtifactCacheError,
    ArtifactNotFoundError,
    CachePermissionError,
    CacheTransientError,
    InvalidCacheKeyError,
)
from launcher.cache.local import (
    LocalArtifactCache,
    parse_local_uri_or_path,
)


def test_local_atomic_staging_and_failed_write_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  cache = LocalArtifactCache(tmp_path / "cache_root")
  key = "shared/code.tar.gz"
  cache.put_bytes(key, b"original-cache")
  assert cache.get_bytes(key) == b"original-cache"

  src_file = tmp_path / "new_code.tar.gz"
  src_file.write_bytes(b"new-content")

  def _failing_put_file(lpath: str, rpath: str, **kwargs: Any) -> None:
    Path(rpath).write_bytes(b"partial-staging-file")
    raise OSError(errno.EIO, "simulated disk write failure")

  monkeypatch.setattr(cache._fs, "put_file", _failing_put_file)
  with pytest.raises(CacheTransientError) as exc_info:
    cache.put_file(key, src_file)
  assert exc_info.value.op == "put_file"
  assert cache.get_bytes(key) == b"original-cache"
  leftover = [
      p.name
      for p in (cache.root_dir / "shared").iterdir()
      if p.name.startswith(".tmp.")
  ]
  assert leftover == []

  def _failing_pipe_file(rpath: str, data: bytes, **kwargs: Any) -> None:
    Path(rpath).write_bytes(b"partial-staging-bytes")
    raise OSError(errno.EIO, "simulated disk pipe failure")

  monkeypatch.setattr(cache._fs, "pipe_file", _failing_pipe_file)
  with pytest.raises(CacheTransientError) as pipe_exc:
    cache.put_bytes(key, b"new-bytes")
  assert pipe_exc.value.op == "put_bytes"
  assert cache.get_bytes(key) == b"original-cache"
  leftover = [
      p.name
      for p in (cache.root_dir / "shared").iterdir()
      if p.name.startswith(".tmp.")
  ]
  assert leftover == []


def test_local_directory_collision_on_write_and_symlink_source(
    tmp_path: Path,
) -> None:
  cache = LocalArtifactCache(tmp_path / "dir_collision")
  cache.put_bytes("tasks/find/input/manifest.json", b"{}")

  # Writing to an existing directory path ("tasks/find") or underneath an
  # existing regular file ("tasks/find/input/manifest.json/sub") must raise
  # ArtifactCacheError (not ArtifactNotFoundError).
  for collision_key in ("tasks/find", "tasks/find/input/manifest.json/sub"):
    with pytest.raises(ArtifactCacheError) as exc_info:
      cache.put_bytes(collision_key, b"data")
    assert not isinstance(exc_info.value, ArtifactNotFoundError)

  real_src = tmp_path / "real.txt"
  real_src.write_bytes(b"hello")
  link_src = tmp_path / "link.txt"
  link_src.symlink_to(real_src)
  with pytest.raises(ArtifactNotFoundError):
    cache.put_file("shared/code.tar.gz", link_src)


def test_concurrent_readers_and_writers_observe_only_atomic_snapshots(
    tmp_path: Path,
) -> None:
  local_cache = LocalArtifactCache(tmp_path / "concurrent_cache")
  key = "tasks/fix-1/output/finding.diff"
  payload_a = b"A" * 65536
  payload_b = b"B" * 65536
  src_a = tmp_path / "src_a.diff"
  src_b = tmp_path / "src_b.diff"
  src_a.write_bytes(payload_a)
  src_b.write_bytes(payload_b)

  local_cache.put_bytes(key, payload_a)

  def _writer(idx: int) -> None:
    for i in range(25):
      use_a = (idx + i) % 2 == 0
      if i % 2 == 0:
        local_cache.put_bytes(key, payload_a if use_a else payload_b)
      else:
        local_cache.put_file(key, src_a if use_a else src_b)

  def _reader(idx: int) -> None:
    dest = tmp_path / f"reader_{idx}.diff"
    for i in range(25):
      if i % 2 == 0:
        data = local_cache.get_bytes(key)
      else:
        local_cache.get_file(key, dest)
        data = dest.read_bytes()
      assert data in (payload_a, payload_b)

  with ThreadPoolExecutor(max_workers=8) as pool:
    futures = [pool.submit(_writer, i) for i in range(4)] + [
        pool.submit(_reader, i) for i in range(4)
    ]
    for fut in futures:
      fut.result()

  assert local_cache.get_bytes(key) in (payload_a, payload_b)
  leftovers = sorted(
      p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob(".tmp.*")
  )
  assert leftovers == []


def test_path_traversal_and_symlink_escape_rejection(tmp_path: Path) -> None:
  cache_dir = tmp_path / "local_cache"
  cache = LocalArtifactCache(cache_dir)
  (cache.root_dir / "escape").symlink_to("/etc")

  src_file = tmp_path / "src.txt"
  src_file.write_text("ok", encoding="utf-8")

  invalid_keys = [
      "",
      "../secret",
      "a/../../secret",
      "./file.txt",
      "a/./file.txt",
      "/etc/passwd",
      "C:/Windows/win.ini",
      "a//b.txt",
      "trailing/",
      ".tmp.partial",
      "tasks/.tmp.uuid.manifest.json",
      "bad\x00key.txt",
      "escape/passwd",
  ]
  for bad_key in invalid_keys:
    with pytest.raises(InvalidCacheKeyError):
      cache.get_bytes(bad_key)
    with pytest.raises(InvalidCacheKeyError):
      cache.put_bytes(bad_key, b"x")

  with pytest.raises(InvalidCacheKeyError):
    cache.get_manifest("../escape")
  with pytest.raises(InvalidCacheKeyError):
    cache.get_manifest("sub/task")
  with pytest.raises(InvalidCacheKeyError):
    cache.put_task_output("find", "../escape.diff", src_file)
  with pytest.raises(InvalidCacheKeyError):
    cache.put_task_output("../find", "out.diff", src_file)
  with pytest.raises(InvalidCacheKeyError):
    cache.put_finding("../escape", src_file)


def test_parent_os_symlink_allowed_and_in_cache_symlinks_rejected(
    tmp_path: Path,
) -> None:
  real_base = tmp_path / "real_parent"
  real_base.mkdir()
  symlink_parent = tmp_path / "sym_parent"
  symlink_parent.symlink_to(real_base)

  cache = LocalArtifactCache(symlink_parent / "cache_root")
  cache.put_bytes(r"tasks\find\output\runner.log", b"ok")
  assert cache.get_bytes("tasks/find/output/runner.log") == b"ok"

  outside_file = tmp_path / "outside.txt"
  outside_file.write_text("secret", encoding="utf-8")
  outside_dir = tmp_path / "outside_dir"
  outside_dir.mkdir()
  (outside_dir / "leak.txt").write_text("secret", encoding="utf-8")
  (cache.root_dir / "tasks" / "link.txt").symlink_to(outside_file)
  (cache.root_dir / "linked_dir").symlink_to(
      outside_dir, target_is_directory=True
  )

  with pytest.raises(InvalidCacheKeyError):
    cache.get_bytes("tasks/link.txt")
  with pytest.raises(InvalidCacheKeyError):
    cache.get_bytes("linked_dir/leak.txt")


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="requires POSIX permission checks as a non-root user",
)
def test_local_permission_errors_are_translated(tmp_path: Path) -> None:
  local_cache = LocalArtifactCache(tmp_path / "perm_root")
  key = "tasks/x/output/receipt.json"
  local_cache.put_bytes(key, b"{}")
  locked = local_cache.root_dir / "tasks" / "x"
  locked.chmod(0)
  try:
    with pytest.raises(CachePermissionError):
      local_cache.get_bytes(key)
  finally:
    locked.chmod(0o755)


def test_parse_local_uri_or_path_validation() -> None:
  assert parse_local_uri_or_path("file:///tmp/cache") == Path("/tmp/cache")
  assert parse_local_uri_or_path("file://localhost/tmp/cache") == Path(
      "/tmp/cache"
  )
  assert parse_local_uri_or_path("file:///tmp/a%20b") == Path("/tmp/a b")
  assert parse_local_uri_or_path(Path("/tmp/cache")) == Path("/tmp/cache")

  for bad_local in (
      "",
      "   ",
      "file://",
      "file://remotehost/tmp/cache",
      "/tmp/bad\x00path",
      Path("/tmp/bad\x00path"),
      "file:///tmp/bad%00path",
      "file:///tmp/cache?query=1",
      "file:///tmp/cache#frag",
  ):
    with pytest.raises(InvalidCacheKeyError):
      parse_local_uri_or_path(bad_local)
    with pytest.raises(InvalidCacheKeyError):
      open_artifact_cache(bad_local)
