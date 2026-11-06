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
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0003
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0006
# Governing: SPEC-ARTIFACT-CACHE/REQ-0002, SPEC-ARTIFACT-CACHE/REQ-0006
"""Unit and contract tests for `launcher.cache.base`."""

from __future__ import annotations

import errno
import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

import fsspec
import pytest

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
    CacheTransientError,
    InvalidCacheKeyError,
)
from launcher.cache.models import (
    ExecutionReceipt,
    RunManifest,
)


class _MemoryArtifactCache(ArtifactCache):
  """Concrete in-memory ArtifactCache subclass for testing base.py directly."""

  def __init__(self, root_path: str) -> None:
    super().__init__(
        fs=fsspec.filesystem("memory"),
        root_path=root_path,
        uri=f"memory://{root_path}",
    )

  def _resolve_full_path(self, key: str, *, op: str) -> str:
    norm_key = normalize_cache_key(
        key,
        uri=self._uri,
        op=op,
    )
    return self._join_root(norm_key)


@pytest.fixture
def cache() -> ArtifactCache:
  return _MemoryArtifactCache(f"test-bucket/org/repo/prs/42/{uuid4().hex[:8]}")


def test_artifact_cache_base_class_is_abstract() -> None:
  with pytest.raises(TypeError):
    ArtifactCache(  # type: ignore[abstract]
        fs=fsspec.filesystem("memory"),
        root_path="bucket",
        uri="gs://bucket",
    )


def test_atomic_bytes_roundtrip_and_overwrite(cache: ArtifactCache) -> None:
  key = "tasks/find/output/runner.log"
  with pytest.raises(ArtifactNotFoundError):
    cache.get_bytes(key)

  payload1 = b"line 1\r\nline 2\x00binary\n"
  cache.put_bytes(key, payload1)
  assert cache.get_bytes(key) == payload1

  payload2 = b"updated payload"
  cache.put_bytes(key, payload2)
  assert cache.get_bytes(key) == payload2


def test_atomic_file_roundtrip_and_parent_creation(
    cache: ArtifactCache, tmp_path: Path
) -> None:
  src_file = tmp_path / "source.tar.gz"
  raw_content = b"\x1f\x8b\x08\x00fake-tar-gz-bytes\r\n"
  src_file.write_bytes(raw_content)

  key = "shared/code.tar.gz"
  cache.put_file(key, src_file)

  dest_file = tmp_path / "nested" / "dir" / "downloaded.tar.gz"
  returned_path = cache.get_file(key, dest_file)
  assert returned_path == dest_file
  assert dest_file.read_bytes() == raw_content


def test_json_roundtrip_and_deterministic_formatting(
    cache: ArtifactCache,
) -> None:
  key = "tasks/find/input/manifest.json"
  payload = {"b": 2, "a": 1, "nested": {"k": "v"}}
  cache.put_json(key, payload)
  assert cache.get_json(key) == payload

  raw_text = cache.get_bytes(key).decode("utf-8")
  assert raw_text.endswith("\n")
  assert raw_text.index('"a"') < raw_text.index('"b"')


def test_failed_get_preserves_prior_dest_and_leaves_no_tmp(
    cache: ArtifactCache,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  key = "shared/code.tar.gz"
  cache.put_bytes(key, b"original-cache")

  dest_file = tmp_path / "dest" / "code.tar.gz"
  dest_file.parent.mkdir(parents=True, exist_ok=True)
  dest_file.write_bytes(b"original-dest")

  def _failing_get_file(rpath: str, lpath: str, **kwargs: Any) -> None:
    Path(lpath).write_bytes(b"truncated-partial")
    raise OSError(errno.EIO, "simulated network drop during download")

  monkeypatch.setattr(cache._fs, "get_file", _failing_get_file)
  with pytest.raises(CacheTransientError) as get_exc:
    cache.get_file(key, dest_file)
  assert get_exc.value.op == "get_file"
  assert dest_file.read_bytes() == b"original-dest"
  leftover_dest = [
      p.name for p in dest_file.parent.iterdir() if p.name.startswith(".tmp.")
  ]
  assert leftover_dest == []


def test_missing_or_directory_keys_and_symlink_sources_raise_expected_errors(
    cache: ArtifactCache, tmp_path: Path
) -> None:
  cache.put_json("tasks/find/input/manifest.json", {"task_id": "find"})
  dest_file = tmp_path / "out.json"

  with pytest.raises(ArtifactNotFoundError):
    cache.get_bytes("missing.txt")
  with pytest.raises(ArtifactNotFoundError):
    cache.get_file("missing.txt", dest_file)

  with pytest.raises(ArtifactNotFoundError):
    cache.get_bytes("tasks/find")
  with pytest.raises(ArtifactNotFoundError):
    cache.get_file("tasks/find", dest_file)
  assert not dest_file.exists()

  with pytest.raises(ArtifactNotFoundError):
    cache.put_file("shared/code.tar.gz", tmp_path / "does_not_exist.tar.gz")
  with pytest.raises(ArtifactNotFoundError):
    cache.put_file("shared/code.tar.gz", tmp_path)

  # Symlink source file must be rejected by put_file.
  real_src = tmp_path / "real.tar.gz"
  real_src.write_bytes(b"real")
  symlink_src = tmp_path / "symlink.tar.gz"
  symlink_src.symlink_to(real_src)
  with pytest.raises(ArtifactNotFoundError):
    cache.put_file("shared/code.tar.gz", symlink_src)

  # Existing directory or non-directory parent passed as dest to get_file
  # must raise ArtifactCacheError (not ArtifactNotFoundError).
  existing_dir = tmp_path / "existing_dir"
  existing_dir.mkdir()
  with pytest.raises(ArtifactCacheError) as dir_exc:
    cache.get_file("tasks/find/input/manifest.json", existing_dir)
  assert not isinstance(dir_exc.value, ArtifactNotFoundError)

  file_as_parent = tmp_path / "regular_file"
  file_as_parent.write_bytes(b"file")
  with pytest.raises(ArtifactCacheError) as parent_exc:
    cache.get_file(
        "tasks/find/input/manifest.json", file_as_parent / "child.json"
    )
  assert not isinstance(parent_exc.value, ArtifactNotFoundError)


def test_json_integrity_and_non_finite_rejection(cache: ArtifactCache) -> None:
  with pytest.raises(ArtifactIntegrityError):
    cache.put_json("bad/list.json", [1, 2, 3])  # type: ignore[arg-type]

  with pytest.raises(ArtifactIntegrityError):
    cache.put_json("bad/set.json", {"unserializable": {1, 2}})

  for bad_value in (float("nan"), float("inf"), float("-inf")):
    with pytest.raises(ArtifactIntegrityError):
      cache.put_json("bad/non-finite.json", {"value": bad_value})
  with pytest.raises(ArtifactNotFoundError):
    cache.get_bytes("bad/non-finite.json")

  cache.put_bytes("bad/utf8.json", b"\xff\xfe\xfa")
  with pytest.raises(ArtifactIntegrityError):
    cache.get_json("bad/utf8.json")

  cache.put_bytes("bad/truncated.json", b'{"task_id": "find"')
  with pytest.raises(ArtifactIntegrityError):
    cache.get_json("bad/truncated.json")

  cache.put_bytes("bad/array_root.json", b"[1, 2, 3]")
  with pytest.raises(ArtifactIntegrityError):
    cache.get_json("bad/array_root.json")


def test_end_to_end_typed_protocol_flow(
    cache: ArtifactCache, tmp_path: Path
) -> None:
  tar_src = tmp_path / "code.tar.gz"
  tar_src.write_bytes(b"fake-repo-tar-gz")
  cache.put_repo_archive(tar_src)

  sarif_src = tmp_path / "finding-001.sarif"
  sarif_src.write_text('{"version": "2.1.0", "runs": []}\n', encoding="utf-8")
  finding_key = cache.put_finding("finding-001", sarif_src)
  assert finding_key == "findings/finding-001.sarif"

  manifest_in = RunManifest(
      command="fix",
      task_id="fix-finding-001",
      finding_key=finding_key,
      log_level="info",
  )
  cache.put_manifest("fix-finding-001", manifest_in)

  manifest_out = cache.get_manifest("fix-finding-001")
  assert manifest_out == manifest_in

  work_inputs = tmp_path / "work" / "inputs"
  repo_dest = cache.get_repo_archive(work_inputs / "code.tar.gz")
  assert repo_dest.read_bytes() == b"fake-repo-tar-gz"

  assert manifest_out.finding_key is not None
  finding_dest = cache.get_finding(
      manifest_out.finding_key, work_inputs / "finding-001.sarif"
  )
  assert finding_dest.read_text(encoding="utf-8") == sarif_src.read_text(
      encoding="utf-8"
  )

  finding_dest_bare = cache.get_finding(
      "finding-001", work_inputs / "finding-001-bare.sarif"
  )
  assert finding_dest_bare.read_text(encoding="utf-8") == sarif_src.read_text(
      encoding="utf-8"
  )

  diff_bytes = b"diff --git a/f.py b/f.py\r\n--- a/f.py\r\n+++ b/f.py\r\n"
  diff_src = tmp_path / "finding-001.diff"
  diff_src.write_bytes(diff_bytes)
  diff_key = cache.put_task_output(
      "fix-finding-001", "finding-001.diff", diff_src
  )
  assert diff_key == "tasks/fix-finding-001/output/finding-001.diff"

  log_src = tmp_path / "runner.log"
  log_src.write_text("cm-runner fix completed\n", encoding="utf-8")
  log_key = cache.put_task_output("fix-finding-001", "runner.log", log_src)
  assert log_key == "tasks/fix-finding-001/output/runner.log"

  receipt_in = ExecutionReceipt(
      status="SUCCESS",
      exit_code=0,
      command="fix",
      task_id="fix-finding-001",
      started_at="2026-10-22T10:00:00Z",
      completed_at="2026-10-22T10:01:00Z",
      duration_seconds=60.0,
      finding_count=0,
      artifacts={"patch": diff_key, "log": log_key},
      error=None,
  )
  cache.put_receipt("fix-finding-001", receipt_in)

  receipt_out = cache.get_receipt("fix-finding-001")
  assert receipt_out == receipt_in

  out_diff = tmp_path / "retrieved" / "finding-001.diff"
  cache.get_task_output("fix-finding-001", "finding-001.diff", out_diff)
  assert out_diff.read_bytes() == diff_bytes


def test_typed_protocol_mismatch_and_integrity_errors(
    cache: ArtifactCache, tmp_path: Path
) -> None:
  manifest = RunManifest(command="init", task_id="init-1")
  with pytest.raises(InvalidCacheKeyError):
    cache.put_manifest("other-task", manifest)

  receipt = ExecutionReceipt(
      status="FAILURE",
      exit_code=3,
      command="fix",
      task_id="fix-1",
      started_at="2026-10-22T10:00:00Z",
      completed_at="2026-10-22T10:00:05Z",
      duration_seconds=5.0,
      error=None,
  )
  with pytest.raises(InvalidCacheKeyError):
    cache.put_receipt("fix-2", receipt)

  cache.put_json(
      "tasks/find/input/manifest.json",
      {"command": "init", "task_id": "wrong-id"},
  )
  with pytest.raises(ArtifactIntegrityError):
    cache.get_manifest("find")

  cache.put_json("tasks/fix-2/output/receipt.json", receipt.to_dict())
  with pytest.raises(ArtifactIntegrityError):
    cache.get_receipt("fix-2")

  for bad_finding_key in (
      "shared/code.tar.gz",
      "findings/finding-001.json",
      "findings/sub/finding-001.sarif",
      "../escape.sarif",
  ):
    with pytest.raises(InvalidCacheKeyError):
      cache.get_finding(bad_finding_key, tmp_path / "out.sarif")


def test_finding_key_forms_and_omitted_manifest_task_id(
    cache: ArtifactCache, tmp_path: Path
) -> None:
  src = tmp_path / "finding.sarif"
  src.write_text('{"runs": []}', encoding="utf-8")
  assert cache.put_finding("finding-007.sarif", src) == (
      "findings/finding-007.sarif"
  )
  for form in (
      "finding-007",
      "finding-007.sarif",
      "findings/finding-007.sarif",
      r"findings\finding-007.sarif",
  ):
    dest = cache.get_finding(form, tmp_path / "out" / "finding.sarif")
    assert dest.read_text(encoding="utf-8") == '{"runs": []}'

  for bad_form in (
      ".sarif",
      "findings/.sarif",
      "findings/C:x.sarif",
      "findings/finding-007.sarif.bak",
  ):
    with pytest.raises(InvalidCacheKeyError):
      cache.get_finding(bad_form, tmp_path / "out" / "bad.sarif")

  cache.put_json(
      "tasks/find/input/manifest.json",
      {"command": "find-diff", "base_ref": "origin/main", "diff_workers": 4},
  )
  assert cache.get_manifest("find") == RunManifest(
      command="find-diff",
      task_id="find",
      base_ref="origin/main",
      diff_workers=4,
  )


def test_temp_prefix_and_uri_branch_helpers(cache: ArtifactCache) -> None:
  key = "tasks/find/output/build.tmp.log"
  cache.put_bytes(key, b"ok")
  assert cache.get_bytes(key) == b"ok"
  assert validate_single_segment("build.tmp.log") == "build.tmp.log"
  for bad_key in (".tmp.partial", "tasks/.tmp.abc.receipt.json"):
    with pytest.raises(InvalidCacheKeyError):
      cache.put_bytes(bad_key, b"x")

  expected_hash = hashlib.sha256(b"feat/login").hexdigest()[:8]
  assert sanitize_branch_name("feat/login") == f"feat-login-{expected_hash}"
  assert (
      sanitize_branch_name("refs/heads/feat/login")
      == f"feat-login-{expected_hash}"
  )
  assert sanitize_branch_name("feat/login") != sanitize_branch_name(
      "feat-login"
  )

  long_branch = "feature/" + ("a" * 60)
  sanitized_long = sanitize_branch_name(long_branch)
  assert len(sanitized_long) <= 49
  assert validate_single_segment(sanitized_long) == sanitized_long

  for bad_branch in ("", "   ", "refs/heads/", "bad\x00branch"):
    with pytest.raises(InvalidCacheKeyError):
      sanitize_branch_name(bad_branch)

  pr_uri = build_gcs_cache_uri(
      "my-bucket", "my-org", "my-repo", "abc1234", pr_number=42
  )
  assert pr_uri == "gs://my-bucket/my-org/my-repo/prs/42/abc1234"

  branch_uri = build_gcs_cache_uri(
      "my-bucket", "my-org", "my-repo", "abc1234", branch="feat/login"
  )
  assert (
      branch_uri
      == f"gs://my-bucket/my-org/my-repo/branches/"
      f"feat-login-{expected_hash}/abc1234"
  )

  for bad_kwargs in (
      {},
      {"pr_number": 1, "branch": "main"},
      {"pr_number": 0},
      {"pr_number": True},
  ):
    with pytest.raises(InvalidCacheKeyError):
      build_gcs_cache_uri(
          "my-bucket",
          "my-org",
          "my-repo",
          "abc1234",
          **bad_kwargs,  # type: ignore[arg-type]
      )
  with pytest.raises(InvalidCacheKeyError):
    build_gcs_cache_uri(
        "my-bucket", "my-org/sub", "my-repo", "abc1234", pr_number=1
    )
