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

# Governing: SPEC-ARTIFACT-CACHE/REQ-0002, SPEC-ARTIFACT-CACHE/REQ-0006
"""Shared, dependency-free path validation predicates for launcher.cache.

`launcher.cache.models` (raising `ValidationError`) and `launcher.cache.base`
(raising `InvalidCacheKeyError`) both delegate to these predicates so segment,
key, and finding-key rules cannot drift apart. This module must not import
anything from `launcher.cache` to keep the dependency graph acyclic.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from launcher.validators import TEMP_PREFIX
from launcher.validators import relative_key_error
from launcher.validators import single_segment_error

FINDINGS_DIR = "findings"
SARIF_SUFFIX = ".sarif"


def canonical_finding_key(value: str) -> str:
  """Maps a bare `<id>` or `<id>.sarif` to `findings/<id>.sarif`.

  Values that already contain a path separator are returned unchanged so the
  caller's validation can accept or reject them as full keys.

  Args:
    value: A bare finding ID, a bare `<id>.sarif` filename, or a full key.

  Returns:
    The canonical `findings/<id>.sarif` key for bare inputs, else `value`.
  """
  if "/" in value or "\\" in value:
    return value
  return f"{FINDINGS_DIR}/{value.removesuffix(SARIF_SUFFIX)}{SARIF_SUFFIX}"


def finding_key_error(value: Any) -> str | None:
  """Returns why `value` is not a `findings/<id>.sarif` key, or None."""
  reason = relative_key_error(value)
  if reason is not None:
    return reason
  parts = PurePosixPath(value).parts
  if (
      len(parts) != 2
      or parts[0] != FINDINGS_DIR
      or not parts[1].endswith(SARIF_SUFFIX)
  ):
    return f"must match '{FINDINGS_DIR}/<id>{SARIF_SUFFIX}'"
  stem_reason = single_segment_error(parts[1].removesuffix(SARIF_SUFFIX))
  if stem_reason is not None:
    return f"finding ID {stem_reason}"
  return None
