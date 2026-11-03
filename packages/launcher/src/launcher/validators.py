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

# Governing: ADR-0002, ADR-0007, ADR-0009, SPEC-ARTIFACT-CACHE/REQ-0002, SPEC-ARTIFACT-CACHE/REQ-0006, SPEC-WORKER/REQ-0001, SPEC-WORKER/REQ-0002
"""Shared `attrs` field validators and path predicates for `launcher`."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

import attrs

TEMP_PREFIX = ".tmp."


class ValidationError(Exception):
  """Raised when a model field, CLI argument, or path fails validation."""


def single_segment_error(value: Any) -> str | None:
  """Returns why `value` is not a single safe path segment, or `None`.

  Args:
    value: Candidate path segment to inspect.

  Returns:
    Human-readable reason string if `value` is invalid, or `None` when `value`
    is a valid single safe path segment.
  """
  if not isinstance(value, str) or not value.strip() or "\x00" in value:
    return "must be a non-empty string without NUL bytes"
  path = PurePosixPath(value)
  if (
      path.is_absolute()
      or "\\" in value
      or ":" in value
      or value.startswith(TEMP_PREFIX)
      or len(path.parts) != 1
      or path.parts[0] in (".", "..")
      or path.as_posix() != value
  ):
    return (
        "must be a single safe path segment without '/', '\\', ':', or '.tmp.'"
    )
  return None


def relative_key_error(value: Any) -> str | None:
  """Returns why `value` is not a normalized relative POSIX key, or `None`.

  Args:
    value: Candidate relative POSIX key to inspect.

  Returns:
    Human-readable reason string if `value` is invalid, or `None` when `value`
    is a normalized relative POSIX key.
  """
  if not isinstance(value, str) or not value.strip() or "\x00" in value:
    return "must be a non-empty string without NUL bytes"
  path = PurePosixPath(value)
  if (
      path.is_absolute()
      or "\\" in value
      or not path.parts
      or path.as_posix() != value
      or any(
          part in (".", "..")
          or part.startswith(TEMP_PREFIX)
          or ":" in part
          for part in path.parts
      )
  ):
    return (
        "must be a normalized relative POSIX key without '..', ':', or '.tmp.'"
    )
  return None


def check_non_empty_str(value: Any, *, field_name: str) -> str:
  """Validates that `value` is a non-empty string without NUL bytes.

  Args:
    value: Candidate value to validate.
    field_name: Field or parameter name included in `ValidationError`.

  Returns:
    The validated non-empty string `value`.

  Raises:
    ValidationError: If `value` is not a `str`, is blank/whitespace-only, or
      contains NUL bytes.
  """
  if not isinstance(value, str) or not value.strip() or "\x00" in value:
    raise ValidationError(f"{field_name} must be a non-empty string.")
  return value


def validate_non_empty_str(
    instance: Any, attribute: attrs.Attribute[Any], value: Any
) -> None:
  """`attrs` validator ensuring `value` is a non-empty string without NULs.

  Args:
    instance: The `attrs` model instance being validated (unused).
    attribute: The `attrs` attribute descriptor supplying the field name.
    value: Candidate field value to validate.

  Raises:
    ValidationError: If `value` is not a non-empty string without NUL bytes.
  """
  del instance
  check_non_empty_str(value, field_name=attribute.name)


def validate_optional_non_empty_str(
    instance: Any, attribute: attrs.Attribute[Any], value: Any
) -> None:
  """`attrs` validator ensuring `value` is `None` or a non-empty string.

  Args:
    instance: The `attrs` model instance being validated (unused).
    attribute: The `attrs` attribute descriptor supplying the field name.
    value: Candidate optional field value to validate.

  Raises:
    ValidationError: If `value` is not `None` and fails `check_non_empty_str`.
  """
  del instance
  if value is not None:
    check_non_empty_str(value, field_name=attribute.name)


def check_safe_path_segment(value: Any, *, field_name: str) -> str:
  """Validates that `value` is a single safe path segment.

  Args:
    value: Candidate path segment to validate.
    field_name: Field or parameter name included in `ValidationError`.

  Returns:
    The validated path segment `value`.

  Raises:
    ValidationError: If `value` is empty, contains path separators, traversal
      segments, colons, NUL bytes, or a reserved `.tmp.` prefix.
  """
  if single_segment_error(value) is not None:
    raise ValidationError(
        f"{field_name} must be a single safe path segment: {value!r}"
    )
  return value


def validate_safe_path_segment(
    instance: Any, attribute: attrs.Attribute[Any], value: Any
) -> None:
  """`attrs` validator ensuring `value` is a single safe path segment.

  Args:
    instance: The `attrs` model instance being validated (unused).
    attribute: The `attrs` attribute descriptor supplying the field name.
    value: Candidate field value to validate.

  Raises:
    ValidationError: If `value` is not a single safe path segment.
  """
  del instance
  check_safe_path_segment(value, field_name=attribute.name)


def check_safe_relative_key(value: Any, *, field_name: str) -> str:
  """Validates that `value` is a normalized relative POSIX key.

  Args:
    value: Candidate relative key to validate.
    field_name: Field or parameter name included in `ValidationError`.

  Returns:
    The validated relative POSIX key `value`.

  Raises:
    ValidationError: If `value` is empty, absolute, unnormalized, or contains
      backslashes, colons, traversal segments, or `.tmp.` segments.
  """
  if relative_key_error(value) is not None:
    raise ValidationError(
        f"{field_name} must be a normalized relative POSIX key: {value!r}"
    )
  return value
