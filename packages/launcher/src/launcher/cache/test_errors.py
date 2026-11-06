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
"""Unit tests for launcher.cache.errors and translate_fs_error."""

from __future__ import annotations

import errno

import pytest

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


class _StatusError(Exception):
  """Minimal stand-in for gcsfs.retry.HttpError (exposes `.code`)."""

  def __init__(self, message: str, code: int) -> None:
    super().__init__(message)
    self.code = code


def _third_party_exc(
    module: str, name: str, *bases: type[BaseException]
) -> type[BaseException]:
  """Builds an exception type whose module/name mimic a third-party class."""
  return type(name, bases or (Exception,), {"__module__": module})


_AuthTransportError = _third_party_exc(
    "google.auth.exceptions", "TransportError"
)
_AuthRefreshError = _third_party_exc("google.auth.exceptions", "RefreshError")
_AiohttpConnectionError = _third_party_exc(
    "aiohttp.client_exceptions", "ClientConnectionError"
)
_AiohttpServerDisconnected = _third_party_exc(
    "aiohttp.client_exceptions",
    "ServerDisconnectedError",
    _AiohttpConnectionError,
)


def test_exception_hierarchy_and_context_formatting() -> None:
  err = ArtifactNotFoundError(
      "Missing object",
      op="get_bytes",
      uri="gs://bucket/prefix",
      key="shared/code.tar.gz",
  )
  assert isinstance(err, ArtifactCacheError)
  assert isinstance(err, FileNotFoundError)
  assert err.op == "get_bytes"
  assert err.uri == "gs://bucket/prefix"
  assert err.key == "shared/code.tar.gz"
  assert "op='get_bytes'" in str(err)
  assert "uri='gs://bucket/prefix'" in str(err)
  assert "key='shared/code.tar.gz'" in str(err)

  assert issubclass(InvalidCacheKeyError, (ArtifactCacheError, ValueError))
  assert issubclass(ArtifactIntegrityError, (ArtifactCacheError, ValueError))
  assert issubclass(CacheConfigurationError, ArtifactCacheError)
  assert issubclass(CachePermissionError, (ArtifactCacheError, PermissionError))
  assert issubclass(CacheTransientError, (ArtifactCacheError, OSError))
  assert translate_fs_error(err, op="other", uri="gs://b") is err


@pytest.mark.parametrize(
    ("exc", "op", "expected"),
    [
        # errno wins over status-like digits inside paths (PR #404, task 500).
        (
            PermissionError(errno.EACCES, "Permission denied", "/c/prs/404/x"),
            "get_bytes",
            CachePermissionError,
        ),
        (
            OSError(errno.ENOSPC, "No space left on device", "/c/tasks/403/o"),
            "put_bytes",
            CacheTransientError,
        ),
        (OSError(errno.EIO, "I/O error"), "get_bytes", CacheTransientError),
        (
            FileNotFoundError(errno.ENOENT, "No such file", "/c/tasks/503/x"),
            "get_bytes",
            ArtifactNotFoundError,
        ),
        # 1-arg concrete exception types (errno=None) with PR #404 in path.
        (
            PermissionError("Permission denied: gs://b/o/r/prs/404/sha/x"),
            "get_bytes",
            CachePermissionError,
        ),
        (
            TimeoutError("Timed out reading gs://b/o/r/prs/404/sha/x"),
            "get_bytes",
            CacheTransientError,
        ),
        (
            ConnectionResetError("Connection reset on /prs/404/sha"),
            "get_bytes",
            CacheTransientError,
        ),
        # Bare OSError with /404/ path segment must not match HTTP 404.
        (
            OSError("I/O failure on /cache/org/repo/prs/404/sha/code.tar.gz"),
            "get_bytes",
            CacheTransientError,
        ),
        # EISDIR and ENOTDIR map to ArtifactNotFoundError on read/probe ops,
        # but map to ArtifactCacheError (storage conflict) on write/init ops.
        (
            IsADirectoryError(errno.EISDIR, "Is a directory", "/c/tasks/find"),
            "get_bytes",
            ArtifactNotFoundError,
        ),
        (
            NotADirectoryError(errno.ENOTDIR, "Not a directory", "/c/a/b"),
            "exists",
            ArtifactNotFoundError,
        ),
        (
            IsADirectoryError(errno.EISDIR, "Is a directory", "/c/tasks/find"),
            "put_bytes",
            ArtifactCacheError,
        ),
        (
            NotADirectoryError(errno.ENOTDIR, "Not a directory", "/c/a/b"),
            "put_file",
            ArtifactCacheError,
        ),
        (
            NotADirectoryError(errno.ENOTDIR, "Not a directory", "/c/root"),
            "init",
            ArtifactCacheError,
        ),
        # Structured HTTP status attributes.
        (
            _StatusError("Too Many Requests", 429),
            "get_bytes",
            CacheTransientError,
        ),
        (_StatusError("Unauthorized", 401), "get_bytes", CachePermissionError),
        (_StatusError("Not Found", 404), "get_bytes", ArtifactNotFoundError),
        (_StatusError("Conflict", 409), "put_bytes", ArtifactCacheError),
        # gcsfs: bare OSError for 403, FileExistsError for 412.
        (
            OSError("Forbidden: b/bucket/o/owner%2Frepo%2Fprs%2F404%2Fx"),
            "get_bytes",
            CachePermissionError,
        ),
        (
            Exception("HTTP 404 Not Found for prs/503/manifest.json"),
            "get_bytes",
            ArtifactNotFoundError,
        ),
        (FileExistsError("b/bucket/o/key"), "put_bytes", ArtifactCacheError),
        (
            FileExistsError(
                errno.EEXIST, "File exists", "/c/shared/code.tar.gz"
            ),
            "put_bytes",
            ArtifactCacheError,
        ),
        # Deterministic, non-OS failures must not look retryable.
        (
            TypeError("unexpected keyword argument 'bogus'"),
            "open_artifact_cache",
            ArtifactCacheError,
        ),
        (
            ValueError("Bad Request: b/bucket/o/key\ninvalid"),
            "put_bytes",
            ArtifactCacheError,
        ),
        # Network and auth failures after backend retries.
        (TimeoutError("timed out"), "get_bytes", CacheTransientError),
        (
            ConnectionResetError(errno.ECONNRESET, "reset"),
            "get_bytes",
            CacheTransientError,
        ),
        (
            _AiohttpServerDisconnected("Server disconnected"),
            "get_bytes",
            CacheTransientError,
        ),
        (
            _AuthTransportError("metadata server unreachable"),
            "get_bytes",
            CacheTransientError,
        ),
        (
            _AuthRefreshError("invalid_grant"),
            "open_artifact_cache",
            CacheConfigurationError,
        ),
    ],
)
def test_translate_fs_error_prefers_structured_signals(
    exc: BaseException,
    op: str,
    expected: type[ArtifactCacheError],
) -> None:
  translated = translate_fs_error(exc, op=op, uri="gs://b/p", key="k")
  assert type(translated) is expected
  assert (translated.op, translated.uri, translated.key) == (
      op,
      "gs://b/p",
      "k",
  )
