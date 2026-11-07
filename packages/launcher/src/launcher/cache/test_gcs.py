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
# Governing: SPEC-ARTIFACT-CACHE/REQ-0003
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0002
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0006
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0002
"""Unit tests for launcher.cache.gcs and open_artifact_cache lazy imports."""

from __future__ import annotations

import errno
from pathlib import Path
import subprocess
import sys
import types
from typing import Any
from uuid import uuid4

import fsspec
import pytest

from launcher.cache import (
    ArtifactNotFoundError,
    CacheConfigurationError,
    CachePermissionError,
    CacheTransientError,
    GcsArtifactCache,
    InvalidCacheKeyError,
    LocalArtifactCache,
    RunManifest,
    open_artifact_cache,
    translate_fs_error,
)
from launcher.cache.gcs import parse_gcs_uri


def _install_fake_gcsfs(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[fsspec.AbstractFileSystem, list[dict[str, Any]]]:
  """Installs a fake `gcsfs` module backed by `MemoryFileSystem`."""
  mem_fs = fsspec.filesystem("memory")
  recorded_kwargs: list[dict[str, Any]] = []
  fake_module = types.ModuleType("gcsfs")

  def _fake_gcs_filesystem(**kwargs: Any) -> fsspec.AbstractFileSystem:
    recorded_kwargs.append(kwargs)
    return mem_fs

  fake_module.GCSFileSystem = _fake_gcs_filesystem  # type: ignore[attr-defined]
  monkeypatch.setitem(sys.modules, "gcsfs", fake_module)
  return mem_fs, recorded_kwargs


@pytest.fixture
def gcs_cache(monkeypatch: pytest.MonkeyPatch) -> GcsArtifactCache:
  _install_fake_gcsfs(monkeypatch)
  unique_prefix = f"org/repo/prs/42/{uuid4().hex[:8]}"
  return GcsArtifactCache(f"gs://test-bucket/{unique_prefix}")


def test_parse_gcs_uri_validation() -> None:
  assert parse_gcs_uri("gs://my-bucket") == ("my-bucket", "")
  assert parse_gcs_uri("gs://my-bucket/") == ("my-bucket", "")
  assert parse_gcs_uri("gs://my-bucket/org/repo/prs/1/sha/") == (
      "my-bucket",
      "org/repo/prs/1/sha",
  )

  for bad_uri in (
      "",
      "file:///tmp/cache",
      "gs://",
      "gs:///prefix",
      "gs://.tmp.bucket/prefix",
      "gs://bad\x00bucket/prefix",
      "gs://my-bucket/bad\x00prefix",
      "gs://my-bucket/org//repo",
      "gs://my-bucket/org/../repo",
      "gs://my-bucket/org/repo?generation=1",
      "gs://my-bucket/org/repo#frag",
  ):
    with pytest.raises(InvalidCacheKeyError):
      parse_gcs_uri(bad_uri)


def test_gcs_cache_roundtrip_and_failed_transfer_rollback(
    gcs_cache: GcsArtifactCache,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  assert gcs_cache.bucket == "test-bucket"
  assert gcs_cache.uri.startswith("gs://test-bucket/org/repo/prs/42/")

  manifest = RunManifest(
      command="find-diff", task_id="find", base_ref="origin/main"
  )
  gcs_cache.put_manifest("find", manifest)
  assert gcs_cache.get_manifest("find") == manifest

  key = "shared/code.tar.gz"
  gcs_cache.put_bytes(key, b"original-gcs")
  src_file = tmp_path / "new.tar.gz"
  src_file.write_bytes(b"new")

  def _failing_put_file(lpath: str, rpath: str, **kwargs: Any) -> None:
    raise OSError(errno.EIO, "simulated network failure")

  monkeypatch.setattr(gcs_cache._fs, "put_file", _failing_put_file)
  with pytest.raises(CacheTransientError):
    gcs_cache.put_file(key, src_file)
  assert gcs_cache.get_bytes(key) == b"original-gcs"


def test_open_artifact_cache_schemes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  _install_fake_gcsfs(monkeypatch)

  c1 = open_artifact_cache(tmp_path / "c1")
  assert isinstance(c1, LocalArtifactCache)

  c2 = open_artifact_cache((tmp_path / "c2").as_uri())
  assert isinstance(c2, LocalArtifactCache)

  c3 = open_artifact_cache("gs://test-bucket/org/repo/prs/1/sha")
  assert isinstance(c3, GcsArtifactCache)

  for bad_uri in (
      "",
      "   ",
      "s3://bucket/prefix",
      "http://example.com",
      "://missing-scheme",
      "/tmp/plain-string-without-scheme",
      "gs://",
      "gs://bucket/prefix?query=1",
      "gs://bucket/prefix#frag",
      "/tmp/bad\x00path",
  ):
    with pytest.raises(InvalidCacheKeyError):
      open_artifact_cache(bad_uri)


def test_local_cache_succeeds_when_cloud_sdks_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  monkeypatch.setitem(sys.modules, "gcsfs", None)
  monkeypatch.setitem(sys.modules, "google.cloud", None)
  monkeypatch.setitem(sys.modules, "google.cloud.storage", None)

  local_cache = open_artifact_cache(f"file://{tmp_path / 'lazy_cache'}")
  local_cache.put_json("tasks/find/input/manifest.json", {"task_id": "find"})
  assert local_cache.get_json("tasks/find/input/manifest.json") == {
      "task_id": "find"
  }

  with pytest.raises(CacheConfigurationError) as exc_info:
    open_artifact_cache("gs://my-bucket/org/repo/prs/1/sha")
  assert "codemender-launcher[cloudrun]" in str(exc_info.value)


def test_importing_launcher_cache_loads_no_cloud_sdks(tmp_path: Path) -> None:
  cache_uri = (tmp_path / "iso_cache").as_uri()
  script = "\n".join([
      "import sys",
      "from launcher.cache import open_artifact_cache",
      f"cache = open_artifact_cache({cache_uri!r})",
      "cache.put_bytes('tasks/find/output/runner.log', b'ok')",
      "assert cache.get_bytes('tasks/find/output/runner.log') == b'ok'",
      "prefixes = ('gcsfs', 'google.cloud', 'google.auth')",
      "loaded = sorted(m for m in sys.modules if m.startswith(prefixes))",
      "print(','.join(loaded))",
  ])
  result = subprocess.run(
      [sys.executable, "-c", script],
      capture_output=True,
      text=True,
      check=False,
      timeout=120,
  )
  assert result.returncode == 0, result.stderr
  assert result.stdout.strip() == ""


def test_gcs_cache_disables_listings_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  _, recorded_kwargs = _install_fake_gcsfs(monkeypatch)
  GcsArtifactCache(
      "gs://test-bucket/org/repo/prs/1/sha",
      token="anon",
      use_listings_cache=True,
  )
  assert recorded_kwargs == [{"token": "anon", "use_listings_cache": False}]


def test_backend_error_translation(monkeypatch: pytest.MonkeyPatch) -> None:
  mem_fs, _ = _install_fake_gcsfs(monkeypatch)
  cache = GcsArtifactCache("gs://test-bucket/org/repo/prs/1/sha")

  class FakeHttpError(Exception):
    def __init__(self, message: str, code: int) -> None:
      super().__init__(message)
      self.code = code

  def _raise_403(*args: Any, **kwargs: Any) -> None:
    raise FakeHttpError("Forbidden", 403)

  monkeypatch.setattr(mem_fs, "pipe_file", _raise_403)
  with pytest.raises(CachePermissionError) as perm_exc:
    cache.put_bytes("a.txt", b"x")
  assert perm_exc.value.op == "put_bytes"
  assert perm_exc.value.uri == "gs://test-bucket/org/repo/prs/1/sha"
  assert perm_exc.value.key == "a.txt"

  def _raise_503(*args: Any, **kwargs: Any) -> bytes:
    raise FakeHttpError("Service Unavailable", 503)

  monkeypatch.setattr(mem_fs, "cat_file", _raise_503)
  with pytest.raises(CacheTransientError) as trans_exc:
    cache.get_bytes("a.txt")
  assert trans_exc.value.op == "get_bytes"
  assert trans_exc.value.uri == "gs://test-bucket/org/repo/prs/1/sha"
  assert trans_exc.value.key == "a.txt"

  def _raise_404(*args: Any, **kwargs: Any) -> bytes:
    raise FakeHttpError("Not Found", 404)

  monkeypatch.setattr(mem_fs, "cat_file", _raise_404)
  with pytest.raises(ArtifactNotFoundError) as nf_exc:
    cache.get_bytes("a.txt")
  assert nf_exc.value.op == "get_bytes"

  class DefaultCredentialsError(Exception):
    pass

  err = translate_fs_error(
      DefaultCredentialsError("ADC not found"),
      op="open_artifact_cache",
      uri="gs://bucket/prefix",
  )
  assert isinstance(err, CacheConfigurationError)
