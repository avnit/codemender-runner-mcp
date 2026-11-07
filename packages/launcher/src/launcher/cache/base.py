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
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0001
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0002
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0004
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0006
# Governing: SPEC-ARTIFACT-CACHE/REQ-0002, SPEC-ARTIFACT-CACHE/REQ-0006
"""Abstract base class, protocol methods, and URI helpers for ArtifactCache."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any
from uuid import uuid4

import fsspec
from launcher import validators
from launcher.cache.errors import (
    ArtifactCacheError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    InvalidCacheKeyError,
    translate_fs_error,
)
from launcher.cache.models import (
    ExecutionReceipt,
    RunManifest,
    ValidationError,
)

_BRANCH_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9_.-]")


# Governing: SPEC-ARTIFACT-CACHE/REQ-0002
def normalize_cache_key(
    key: str,
    *,
    uri: str = "",
    op: str = "",
) -> str:
  """Validates and normalizes a logical cache key to relative POSIX format.

  Args:
    key: The logical key provided by the caller.
    uri: The cache root URI for error context.
    op: The operation name for error context.

  Returns:
    A normalized forward-slash-separated relative key.

  Raises:
    InvalidCacheKeyError: If the key is empty, absolute, contains traversal
      segments (`'.'` or `'..'`), null bytes, Windows drive prefixes, or
      reserved `'.tmp.'` staging segments.
  """
  if not isinstance(key, str):
    raise InvalidCacheKeyError(
        f"Cache key must be a str, got {type(key).__name__}",
        op=op,
        uri=uri,
        key=str(key),
    )

  normalized = key.replace("\\", "/")
  reason = validators.relative_key_error(normalized)
  if reason is not None:
    raise InvalidCacheKeyError(
        f"Cache key {reason}",
        op=op,
        uri=uri,
        key=key,
    )
  return normalized


# Governing: SPEC-ARTIFACT-CACHE/REQ-0002
def validate_single_segment(
    value: Any,
    *,
    field: str = "segment",
    uri: str = "",
    op: str = "",
) -> str:
  """Validates that `value` is a single safe path segment without separators."""
  reason = validators.single_segment_error(value)
  if reason is not None:
    raise InvalidCacheKeyError(
        f"{field} must be a single safe path segment ({reason}): {value!r}",
        op=op,
        uri=uri,
        key=str(value),
    )
  return value


# Governing: SPEC-ARTIFACT-CACHE/REQ-0002
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0002
def sanitize_branch_name(branch: str) -> str:
  """Sanitizes a Git branch ref into a deterministic single path segment.

  Strips an optional leading `refs/heads/` prefix, replaces characters outside
  `[a-zA-Z0-9_.-]` with `-`, and returns
  `f"{slug[:40]}-{sha256(raw_branch.encode('utf-8')).hexdigest()[:8]}"`.

  Args:
    branch: Raw Git branch name or `refs/heads/<branch>` ref.

  Returns:
    Deterministic single-segment branch identifier.

  Raises:
    InvalidCacheKeyError: If `branch` is empty or contains null bytes.
  """
  if not isinstance(branch, str) or not branch.strip() or "\x00" in branch:
    raise InvalidCacheKeyError(
        "Branch name must be a non-empty string",
        op="sanitize_branch_name",
        key=str(branch),
    )
  raw_branch = branch.strip().removeprefix("refs/heads/").strip()
  if not raw_branch:
    raise InvalidCacheKeyError(
        "Branch name must not be empty after stripping refs/heads/:"
        f" {branch!r}",
        op="sanitize_branch_name",
        key=branch,
    )
  slug = _BRANCH_UNSAFE_RE.sub("-", raw_branch).strip("-.")
  if not slug:
    slug = "branch"
  prefix = slug[:40].rstrip("-.") or "branch"
  digest = hashlib.sha256(raw_branch.encode("utf-8")).hexdigest()[:8]
  return f"{prefix}-{digest}"


# Governing: SPEC-ARTIFACT-CACHE/REQ-0002
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0003
def build_gcs_cache_uri(
    bucket: str,
    owner: str,
    repo: str,
    commit_sha: str,
    *,
    pr_number: int | None = None,
    branch: str | None = None,
) -> str:
  """Constructs the canonical 4-tier GCS `<cache_root>` URI for a PR or branch.

  Args:
    bucket: GCS transit bucket name (single safe segment).
    owner: Repository owner/organization/namespace (single safe segment).
    repo: Repository name (single safe segment).
    commit_sha: Commit SHA (single safe segment).
    pr_number: Pull/Merge Request number (`>= 1`). Mutually exclusive with
      `branch`.
    branch: Git branch name or ref. Mutually exclusive with `pr_number`.

  Returns:
    Canonical `gs://<bucket>/<owner>/<repo>/prs/<pr_number>/<commit_sha>` or
    `gs://<bucket>/<owner>/<repo>/branches/<sanitized_branch>/<commit_sha>`.

  Raises:
    InvalidCacheKeyError: If any segment is invalid or if neither/both of
      `pr_number` and `branch` are provided.
  """
  op = "build_gcs_cache_uri"
  safe_bucket = validate_single_segment(bucket, field="bucket", op=op)
  safe_owner = validate_single_segment(owner, field="owner", op=op)
  safe_repo = validate_single_segment(repo, field="repo", op=op)
  safe_sha = validate_single_segment(commit_sha, field="commit_sha", op=op)

  if (pr_number is None) == (branch is None):
    raise InvalidCacheKeyError(
        "Exactly one of pr_number or branch must be provided",
        op=op,
    )

  if pr_number is not None:
    if (
        isinstance(pr_number, bool)
        or not isinstance(pr_number, int)
        or pr_number < 1
    ):
      raise InvalidCacheKeyError(
          f"pr_number must be an integer >= 1, got {pr_number!r}",
          op=op,
          key=str(pr_number),
      )
    return (
        f"gs://{safe_bucket}/{safe_owner}/{safe_repo}/prs/"
        f"{pr_number}/{safe_sha}"
    )

  assert branch is not None
  safe_branch = sanitize_branch_name(branch)
  return (
      f"gs://{safe_bucket}/{safe_owner}/{safe_repo}/branches/"
      f"{safe_branch}/{safe_sha}"
  )


# Governing: SPEC-ARTIFACT-CACHE/REQ-0001
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0003
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0006
# Governing: SPEC-ARTIFACT-CACHE/REQ-0006
class ArtifactCache(ABC):
  """Root-scoped transit cache and client-worker protocol over fsspec.

  Subclasses implement `_resolve_full_path` to map validated logical keys onto
  backend paths (`LocalArtifactCache` adds symlink-escape checks).
  """

  REPO_ARCHIVE_KEY = "shared/code.tar.gz"

  def __init__(
      self,
      *,
      fs: fsspec.AbstractFileSystem,
      root_path: str,
      uri: str,
  ) -> None:
    self._fs = fs
    self._root_path = root_path.replace("\\", "/").rstrip("/")
    self._uri = uri

  @property
  def uri(self) -> str:
    """Canonical root URI for this ArtifactCache."""
    return self._uri

  # ---------------------------------------------------------------------------
  # Atomic Low-Level Storage Primitives (REQ-0001)
  # ---------------------------------------------------------------------------

  def put_bytes(self, key: str, data: bytes) -> None:
    """Atomically writes raw bytes to the cache at the given relative key."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
      raise TypeError(
          f"put_bytes expected bytes-like object, got {type(data).__name__}"
      )
    full_path = self._resolve_full_path(key, op="put_bytes")
    try:
      self._fs.pipe_file(full_path, bytes(data))
    except Exception as exc:
      raise translate_fs_error(
          exc, op="put_bytes", uri=self._uri, key=key
      ) from exc

  def get_bytes(self, key: str) -> bytes:
    """Atomically reads and returns raw bytes stored at the relative key."""
    full_path = self._resolve_full_path(key, op="get_bytes")
    try:
      data = self._fs.cat_file(full_path)
      if isinstance(data, str):
        return data.encode("utf-8")
      return bytes(data)
    except Exception as exc:
      raise translate_fs_error(
          exc, op="get_bytes", uri=self._uri, key=key
      ) from exc

  def put_file(self, key: str, src: Path | str) -> None:
    """Atomically uploads a local regular file into the cache at `key`."""
    full_path = self._resolve_full_path(key, op="put_file")
    src_path = Path(src)
    if src_path.is_symlink() or not src_path.is_file():
      raise ArtifactNotFoundError(
          "Local source file does not exist or is not a regular file:"
          f" {src_path}",
          op="put_file",
          uri=self._uri,
          key=key,
      )
    try:
      self._fs.put_file(src_path.as_posix(), full_path)
    except Exception as exc:
      raise translate_fs_error(
          exc, op="put_file", uri=self._uri, key=key
      ) from exc

  def get_file(self, key: str, dest: Path | str) -> Path:
    """Atomically downloads `key` to `dest` via a sibling temporary file."""
    full_path = self._resolve_full_path(key, op="get_file")
    dest_path = Path(dest)
    if dest_path.is_dir():
      raise ArtifactCacheError(
          f"Destination path is an existing directory: {dest_path}",
          op="get_file",
          uri=self._uri,
          key=key,
      )
    try:
      dest_path.parent.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, IsADirectoryError, NotADirectoryError) as exc:
      raise ArtifactCacheError(
          f"Destination parent is not a directory: {dest_path.parent}: {exc}",
          op="get_file",
          uri=self._uri,
          key=key,
      ) from exc
    except Exception as exc:
      raise translate_fs_error(
          exc, op="get_file", uri=self._uri, key=key
      ) from exc

    tmp_path = self._staging_sibling(dest_path)
    try:
      self._fs.get_file(full_path, tmp_path.as_posix())
      if not tmp_path.is_file() or tmp_path.is_symlink():
        raise ArtifactNotFoundError(
            f"Cached key does not exist or is a directory: {key}",
            op="get_file",
            uri=self._uri,
            key=key,
        )
      os.replace(tmp_path, dest_path)
      return dest_path
    except Exception as exc:
      self._discard_staging(tmp_path)
      raise translate_fs_error(
          exc, op="get_file", uri=self._uri, key=key
      ) from exc

  def put_json(self, key: str, data: Mapping[str, Any]) -> None:
    """Serializes `data` as deterministic UTF-8 JSON and writes it to `key`."""
    self._resolve_full_path(key, op="put_json")
    if not isinstance(data, Mapping):
      raise ArtifactIntegrityError(
          f"put_json expected a mapping (dict), got {type(data).__name__}",
          op="put_json",
          uri=self._uri,
          key=key,
      )
    try:
      payload_str = (
          json.dumps(dict(data), indent=2, sort_keys=True, allow_nan=False)
          + "\n"
      )
      payload_bytes = payload_str.encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
      raise ArtifactIntegrityError(
          f"Failed to serialize object to JSON for key {key!r}: {exc}",
          op="put_json",
          uri=self._uri,
          key=key,
      ) from exc
    self.put_bytes(key, payload_bytes)

  def get_json(self, key: str) -> dict[str, Any]:
    """Reads and parses a UTF-8 JSON object (`dict`) from `key`."""
    raw_bytes = self.get_bytes(key)
    try:
      text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
      raise ArtifactIntegrityError(
          f"Invalid UTF-8 bytes in JSON artifact {key!r}: {exc}",
          op="get_json",
          uri=self._uri,
          key=key,
      ) from exc
    try:
      parsed = json.loads(text)
    except json.JSONDecodeError as exc:
      raise ArtifactIntegrityError(
          f"Malformed JSON in artifact {key!r}: {exc}",
          op="get_json",
          uri=self._uri,
          key=key,
      ) from exc
    if not isinstance(parsed, dict):
      raise ArtifactIntegrityError(
          f"Expected JSON object (dict) in artifact {key!r}, got"
          f" {type(parsed).__name__}",
          op="get_json",
          uri=self._uri,
          key=key,
      )
    return parsed

  # ---------------------------------------------------------------------------
  # Typed vcs-cloudrun-comms Protocol Methods (REQ-0006)
  # ---------------------------------------------------------------------------

  def get_manifest(self, task_id: str) -> RunManifest:
    """Atomically reads and validates `RunManifest` for `task_id`."""
    safe_id, key = self._manifest_key(task_id, op="get_manifest")
    raw = dict(self.get_json(key))
    if raw.get("task_id") is None:
      raw["task_id"] = safe_id
    try:
      manifest = RunManifest.from_dict(raw)
      if manifest.task_id != safe_id:
        raise ValidationError(
            f"Manifest task_id {manifest.task_id!r} does not match requested"
            f" task_id {safe_id!r}."
        )
      return manifest
    except (ValidationError, ValueError, TypeError) as exc:
      raise ArtifactIntegrityError(
          f"Invalid RunManifest at {key!r}: {exc}",
          op="get_manifest",
          uri=self._uri,
          key=key,
      ) from exc

  def put_manifest(self, task_id: str, manifest: RunManifest) -> None:
    """Atomically writes `RunManifest` for `task_id`."""
    safe_id, key = self._manifest_key(task_id, op="put_manifest")
    if not isinstance(manifest, RunManifest):
      raise ArtifactIntegrityError(
          f"put_manifest expected RunManifest, got {type(manifest).__name__}",
          op="put_manifest",
          uri=self._uri,
          key=key,
      )
    if manifest.task_id != safe_id:
      raise InvalidCacheKeyError(
          f"manifest.task_id {manifest.task_id!r} does not match task_id"
          f" {safe_id!r}",
          op="put_manifest",
          uri=self._uri,
          key=key,
      )
    self.put_json(key, manifest.to_dict())

  def get_repo_archive(self, dest: Path | str) -> Path:
    """Atomically downloads `shared/code.tar.gz` to `dest`."""
    return self.get_file(self.REPO_ARCHIVE_KEY, dest)

  def put_repo_archive(self, src: Path | str) -> None:
    """Atomically uploads `src` to `shared/code.tar.gz`."""
    self.put_file(self.REPO_ARCHIVE_KEY, src)

  def get_finding(self, finding_key: str, dest: Path | str) -> Path:
    """Atomically downloads a single-finding SARIF to `dest`.

    Accepts the full key, a bare finding ID, or a bare `<id>.sarif` filename.
    """
    if not isinstance(finding_key, str) or not finding_key.strip():
      raise InvalidCacheKeyError(
          "finding_key must be a non-empty string",
          op="get_finding",
          uri=self._uri,
          key=str(finding_key),
      )
    resolved_key = validators.canonical_finding_key(
        finding_key.replace("\\", "/")
    )
    reason = validators.finding_key_error(resolved_key)
    if reason is not None:
      raise InvalidCacheKeyError(
          f"finding_key {reason}, got {finding_key!r}",
          op="get_finding",
          uri=self._uri,
          key=finding_key,
      )
    return self.get_file(resolved_key, dest)

  def put_finding(self, finding_id: str, src: Path | str) -> str:
    """Atomically uploads a single-finding SARIF to `findings/<id>.sarif`."""
    if not isinstance(finding_id, str) or not finding_id.strip():
      raise InvalidCacheKeyError(
          "finding_id must be a non-empty string",
          op="put_finding",
          uri=self._uri,
          key=str(finding_id),
      )
    safe_id = validate_single_segment(
        finding_id.removesuffix(validators.SARIF_SUFFIX),
        field="finding_id",
        uri=self._uri,
        op="put_finding",
    )
    key = validators.canonical_finding_key(safe_id)
    self.put_file(key, src)
    return key

  def get_task_output(
      self, task_id: str, filename: str, dest: Path | str
  ) -> Path:
    """Atomically downloads `tasks/<task_id>/output/<filename>` to `dest`."""
    key = self._task_output_key(task_id, filename, op="get_task_output")
    return self.get_file(key, dest)

  def put_task_output(
      self, task_id: str, filename: str, src: Path | str
  ) -> str:
    """Atomically uploads `src` to `tasks/<task_id>/output/<filename>`."""
    key = self._task_output_key(task_id, filename, op="put_task_output")
    self.put_file(key, src)
    return key

  def get_receipt(self, task_id: str) -> ExecutionReceipt:
    """Atomically reads and validates `ExecutionReceipt` for `task_id`."""
    safe_id, key = self._receipt_key(task_id, op="get_receipt")
    raw = self.get_json(key)
    try:
      receipt = ExecutionReceipt.from_dict(raw)
      if receipt.task_id != safe_id:
        raise ValidationError(
            f"Receipt task_id {receipt.task_id!r} does not match requested"
            f" task_id {safe_id!r}."
        )
      return receipt
    except (ValidationError, ValueError, TypeError) as exc:
      raise ArtifactIntegrityError(
          f"Invalid ExecutionReceipt at {key!r}: {exc}",
          op="get_receipt",
          uri=self._uri,
          key=key,
      ) from exc

  def put_receipt(self, task_id: str, receipt: ExecutionReceipt) -> None:
    """Atomically writes `ExecutionReceipt` for `task_id`."""
    safe_id, key = self._receipt_key(task_id, op="put_receipt")
    if not isinstance(receipt, ExecutionReceipt):
      raise ArtifactIntegrityError(
          f"put_receipt expected ExecutionReceipt, got"
          f" {type(receipt).__name__}",
          op="put_receipt",
          uri=self._uri,
          key=key,
      )
    if receipt.task_id != safe_id:
      raise InvalidCacheKeyError(
          f"receipt.task_id {receipt.task_id!r} does not match task_id"
          f" {safe_id!r}",
          op="put_receipt",
          uri=self._uri,
          key=key,
      )
    self.put_json(key, receipt.to_dict())

  @abstractmethod
  def _resolve_full_path(self, key: str, *, op: str) -> str:
    """Validates a relative key and returns the backend path under the root."""

  def _manifest_key(self, task_id: str, *, op: str) -> tuple[str, str]:
    """Validates `task_id` and returns `(safe_id, manifest_key)`."""
    safe_id = validate_single_segment(
        task_id, field="task_id", uri=self._uri, op=op
    )
    return safe_id, f"tasks/{safe_id}/input/manifest.json"

  def _task_output_key(
      self, task_id: str, filename: str, *, op: str
  ) -> str:
    """Validates `task_id` and `filename` and returns the task output key."""
    safe_task = validate_single_segment(
        task_id, field="task_id", uri=self._uri, op=op
    )
    safe_file = validate_single_segment(
        filename, field="filename", uri=self._uri, op=op
    )
    return f"tasks/{safe_task}/output/{safe_file}"

  def _receipt_key(self, task_id: str, *, op: str) -> tuple[str, str]:
    """Validates `task_id` and returns `(safe_id, receipt_key)`."""
    safe_id = validate_single_segment(
        task_id, field="task_id", uri=self._uri, op=op
    )
    return safe_id, f"tasks/{safe_id}/output/receipt.json"

  def _join_root(self, norm_key: str) -> str:
    """Joins an already-normalized relative key onto the backend root path."""
    if not norm_key:
      return self._root_path
    return f"{self._root_path}/{norm_key}"

  @staticmethod
  def _staging_sibling(target: Path) -> Path:
    """Returns a unique `.tmp.<uuid>.<name>` staging path next to `target`."""
    name = f"{validators.TEMP_PREFIX}{uuid4().hex}.{target.name}"
    return target.parent / name

  @staticmethod
  def _discard_staging(tmp_path: Path) -> None:
    """Best-effort removal of a staging file or directory after a failure."""
    try:
      if tmp_path.is_symlink() or tmp_path.is_file():
        tmp_path.unlink(missing_ok=True)
      elif tmp_path.is_dir():
        shutil.rmtree(tmp_path, ignore_errors=True)
    except OSError:
      pass
