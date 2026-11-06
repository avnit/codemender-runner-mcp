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

# Governing: ADR-0001, SPEC-ARTIFACT-CACHE/REQ-0004, SPEC-WORKER/REQ-0003, SPEC-WORKER/REQ-0006
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0006
"""Typed exception hierarchy and fsspec error translation for ArtifactCache."""

from __future__ import annotations

import errno
import re


class ArtifactCacheError(Exception):
  """Base exception for all ArtifactCache operations."""

  def __init__(
      self,
      message: str,
      *,
      op: str = "",
      uri: str = "",
      key: str = "",
  ) -> None:
    self.op = op
    self.uri = uri
    self.key = key
    context_parts = [
        f"{name}={val!r}"
        for name, val in (("op", op), ("uri", uri), ("key", key))
        if val
    ]
    suffix = f" ({', '.join(context_parts)})" if context_parts else ""
    super().__init__(f"{message}{suffix}")


class InvalidCacheKeyError(ArtifactCacheError, ValueError):
  """Raised on path traversal, symlink escape, or invalid keys/URIs."""


class ArtifactNotFoundError(ArtifactCacheError, FileNotFoundError):
  """Raised when a requested cache key or local source file does not exist."""


class ArtifactIntegrityError(ArtifactCacheError, ValueError):
  """Raised when cached JSON, UTF-8 content, or wire schema is malformed."""


class CacheConfigurationError(ArtifactCacheError):
  """Raised when a backend lacks required extras, credentials, or host path."""


class CachePermissionError(ArtifactCacheError, PermissionError):
  """Raised on POSIX permission errors or GCS HTTP 401/403 access denials."""


class CacheTransientError(ArtifactCacheError, OSError):
  """Raised on transient network, HTTP 429/5xx, timeout, or OS I/O errors."""


_HTTP_STATUS_RE = re.compile(
    r"(?<![/\w.%-])(401|403|404|408|409|412|429|500|502|503|504)(?![/\w.%-])"
)
# gcsfs reports HTTP 403 as a bare `OSError("Forbidden: ...")` with no errno or
# status attribute, so permission keywords take precedence over status digits
# that may appear inside object paths.
_PERMISSION_HINT_RE = re.compile(r"\b(Forbidden|AccessDenied|Access Denied)\b")
_DIR_COLLISION_ERRNOS = frozenset({errno.EISDIR, errno.ENOTDIR})
_PERMISSION_ERRNOS = frozenset({errno.EACCES, errno.EPERM})
_PERMISSION_STATUSES = frozenset({401, 403})
_CONFLICT_STATUSES = frozenset({409, 412})
_TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_READ_OR_PROBE_OPS = frozenset({
    "exists",
    "get_bytes",
    "get_file",
    "get_finding",
    "get_json",
    "get_manifest",
    "get_receipt",
    "get_repo_archive",
    "get_scan_meta",
    "get_task_output",
    "list_keys",
})
# Credential-loading failures that retrying cannot fix.
_AUTH_ERROR_CLASS_NAMES = frozenset({
    "DefaultCredentialsError",
    "GoogleAuthError",
    "RefreshError",
})
# Network-layer failures surfaced by gcsfs (aiohttp) or google-auth (metadata
# server / token endpoint unreachable) after their own retries. Matched against
# the whole MRO so subclasses such as aiohttp's ServerDisconnectedError count.
_TRANSIENT_CLASS_NAMES = frozenset({
    "ClientConnectionError",
    "ClientPayloadError",
    "ServerTimeoutError",
    "TransportError",
})


# Governing: SPEC-ARTIFACT-CACHE/REQ-0004
def translate_fs_error(
    exc: BaseException,
    *,
    op: str,
    uri: str,
    key: str = "",
) -> ArtifactCacheError:
  """Translates backend exceptions into ArtifactCacheError subclasses.

  Concrete exception types (`PermissionError`, `TimeoutError`, etc.) and
  structured signals (`errno` on `OSError`, HTTP status attributes) are trusted
  first; message text is only parsed when an exception carries none of those, so
  object paths containing digits such as `prs/404/` cannot flip the category.

  Args:
    exc: The raw exception raised by the backend.
    op: The cache operation name for error context.
    uri: The cache root URI for error context.
    key: The logical cache key for error context.

  Returns:
    The translated error. Storage conflicts and unclassified (non-OS,
    non-HTTP) exceptions become the base `ArtifactCacheError` so callers never
    retry deterministic failures as if they were transient.
  """
  if isinstance(exc, ArtifactCacheError):
    return exc

  cls_name = type(exc).__name__
  mro_names = {cls.__name__ for cls in type(exc).__mro__}
  module_name = type(exc).__module__ or ""
  msg = str(exc) or cls_name

  if mro_names & _TRANSIENT_CLASS_NAMES:
    return CacheTransientError(
        f"Transient network error during {op}: {msg}",
        op=op,
        uri=uri,
        key=key,
    )

  if (
      mro_names & _AUTH_ERROR_CLASS_NAMES
      or module_name.startswith("google.auth")
      or "DefaultCredentialsError" in msg
  ):
    return CacheConfigurationError(
        f"Cloud credentials error during {op}: {msg}",
        op=op,
        uri=uri,
        key=key,
    )

  err_no = getattr(exc, "errno", None)
  if isinstance(err_no, bool) or not isinstance(err_no, int):
    err_no = None
  http_status = _structured_http_status(exc)
  is_read_or_probe = _is_read_or_probe_op(op)
  is_dir_collision = (
      isinstance(exc, (IsADirectoryError, NotADirectoryError))
      or err_no in _DIR_COLLISION_ERRNOS
  )

  if (
      isinstance(exc, FileNotFoundError)
      or err_no == errno.ENOENT
      or http_status == 404
      or (is_read_or_probe and is_dir_collision)
  ):
    return ArtifactNotFoundError(
        f"Artifact not found during {op}: {msg}",
        op=op,
        uri=uri,
        key=key,
    )

  if (
      isinstance(exc, PermissionError)
      or err_no in _PERMISSION_ERRNOS
      or http_status in _PERMISSION_STATUSES
  ):
    return CachePermissionError(
        f"Permission denied during {op}: {msg}",
        op=op,
        uri=uri,
        key=key,
    )

  if (
      isinstance(exc, FileExistsError)
      or err_no == errno.EEXIST
      or is_dir_collision
      or http_status in _CONFLICT_STATUSES
  ):
    return ArtifactCacheError(
        f"Storage conflict during {op}: {msg}",
        op=op,
        uri=uri,
        key=key,
    )

  if (
      isinstance(exc, (TimeoutError, ConnectionError))
      or http_status in _TRANSIENT_STATUSES
  ):
    return CacheTransientError(
        f"Transient or storage I/O error during {op}: {msg}",
        op=op,
        uri=uri,
        key=key,
    )

  if http_status is None and err_no is None:
    http_status = _message_http_status(msg)
    if http_status == 404:
      return ArtifactNotFoundError(
          f"Artifact not found during {op}: {msg}",
          op=op,
          uri=uri,
          key=key,
      )
    if http_status in _PERMISSION_STATUSES:
      return CachePermissionError(
          f"Permission denied during {op}: {msg}",
          op=op,
          uri=uri,
          key=key,
      )
    if http_status in _CONFLICT_STATUSES:
      return ArtifactCacheError(
          f"Storage conflict during {op}: {msg}",
          op=op,
          uri=uri,
          key=key,
      )
    if http_status in _TRANSIENT_STATUSES:
      return CacheTransientError(
          f"Transient or storage I/O error during {op}: {msg}",
          op=op,
          uri=uri,
          key=key,
      )

  # Remaining OSError subclasses (e.g. ENOSPC, EIO, or generic OSError).
  if isinstance(exc, OSError):
    return CacheTransientError(
        f"Transient or storage I/O error during {op}: {msg}",
        op=op,
        uri=uri,
        key=key,
    )

  return ArtifactCacheError(
      f"Unexpected storage error during {op}: {cls_name}: {msg}",
      op=op,
      uri=uri,
      key=key,
  )


def _is_read_or_probe_op(op: str) -> bool:
  """Returns True if `op` reads or probes a cache key."""
  return op in _READ_OR_PROBE_OPS or op.startswith("get_")


def _structured_http_status(exc: BaseException) -> int | None:
  """Returns an HTTP status code from well-known exception attributes."""
  for attr in ("code", "status", "status_code", "http_status"):
    val = getattr(exc, attr, None)
    if isinstance(val, bool):
      continue
    if isinstance(val, int) and 100 <= val <= 599:
      return val
    if isinstance(val, str) and val.isdigit():
      code = int(val)
      if 100 <= code <= 599:
        return code
  return None


def _message_http_status(msg: str) -> int | None:
  """Infers an HTTP status from message text (last resort only)."""
  if _PERMISSION_HINT_RE.search(msg):
    return 403
  match = _HTTP_STATUS_RE.search(msg)
  return int(match.group(1)) if match else None
