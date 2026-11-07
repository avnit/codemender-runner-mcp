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

# Governing: ADR-0002
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0003
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0007
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0002
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0004
# Governing: SPEC-ARTIFACT-CACHE/REQ-0002, SPEC-ARTIFACT-CACHE/REQ-0006
# Governing: SPEC-WORKER/REQ-0001, SPEC-WORKER/REQ-0002
"""Shared `attrs` field validators, converters, and path predicates."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Collection
from pathlib import PurePosixPath
from typing import Any

import attrs

TEMP_PREFIX = ".tmp."
FINDINGS_DIR = "findings"
SARIF_SUFFIX = ".sarif"


class ValidationError(Exception):
  """Raised when a model field, CLI argument, or path fails validation."""


def non_empty_str_error(value: Any) -> str | None:
  """Returns why `value` is not a non-empty string without NULs, or `None`.

  Args:
    value: Candidate value to inspect.

  Returns:
    Human-readable reason string if `value` is invalid, or `None` when `value`
    is a non-empty `str` without NUL bytes.
  """
  if not isinstance(value, str):
    return "must be a string"
  if not value.strip():
    return "must be a non-empty string"
  if "\x00" in value:
    return "must not contain NUL bytes"
  return None


def strip_if_str(value: Any) -> Any:
  """Returns `value.strip()` when `value` is a `str`, else `value` unchanged.

  Args:
    value: Candidate field value to normalize.

  Returns:
    Stripped `str` when `value` is a `str`, or the original `value` so
    downstream validators can reject non-string types.
  """
  return value.strip() if isinstance(value, str) else value


def single_segment_error(value: Any) -> str | None:
  """Returns why `value` is not a single safe path segment, or `None`.

  Args:
    value: Candidate path segment to inspect.

  Returns:
    Human-readable reason string if `value` is invalid, or `None` when `value`
    is a valid single safe path segment.
  """
  if non_empty_str_error(value) is not None:
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
  if non_empty_str_error(value) is not None:
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
  """Returns why `value` is not a `findings/<id>.sarif` key, or `None`.

  Args:
    value: Candidate finding key to inspect.

  Returns:
    Human-readable reason string if `value` is invalid, or `None` when `value`
    is a canonical `findings/<id>.sarif` key.
  """
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
  reason = non_empty_str_error(value)
  if reason is not None:
    raise ValidationError(f"{field_name} {reason}.")
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


validate_optional_non_empty_str = attrs.validators.optional(
    validate_non_empty_str
)


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


def validate_finding_key(
    instance: Any, attribute: attrs.Attribute[Any], value: Any
) -> None:
  """`attrs` validator ensuring `value` is a `findings/<id>.sarif` key.

  Args:
    instance: The `attrs` model instance being validated (unused).
    attribute: The `attrs` attribute descriptor supplying the field name.
    value: Candidate finding key to validate.

  Raises:
    ValidationError: If `value` is not a valid `findings/<id>.sarif` key.
  """
  del instance
  reason = finding_key_error(value)
  if reason is not None:
    raise ValidationError(f"{attribute.name} {reason}: {value!r}")


validate_optional_finding_key = attrs.validators.optional(validate_finding_key)


def validate_in(
    allowed: Collection[str],
) -> Callable[[Any, Any, Any], None]:
  """Returns an `attrs` validator checking membership in `allowed` strings.

  Args:
    allowed: Collection of permitted string values.

  Returns:
    An `attrs` validator callable `(instance, attribute, value)` that raises
    `ValidationError` if `value` is not a `str` in `allowed`.
  """
  allowed_set = frozenset(allowed)
  allowed_list = sorted(allowed_set)

  def _validator(
      instance: Any, attribute: attrs.Attribute[Any], value: Any
  ) -> None:
    del instance
    check_non_empty_str(value, field_name=attribute.name)
    if value not in allowed_set:
      raise ValidationError(
          f"{attribute.name} must be one of {allowed_list}, got {value!r}."
      )

  return _validator


def convert_int(
    field_name: str,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> Callable[[Any], int]:
  """Returns an `attrs` converter coercing `value` to a bounded `int`.

  Args:
    field_name: Field name included in `ValidationError` messages.
    min_value: Optional inclusive lower bound.
    max_value: Optional inclusive upper bound.

  Returns:
    An `attrs` converter callable `(value) -> int` that rejects `bool` and
    `float` instances, converts via `int(value)`, and enforces bounds.
  """

  def _converter(value: Any) -> int:
    if isinstance(value, (bool, float)):
      raise ValidationError(f'{field_name} must be an int, got "{value}".')
    try:
      coerced = int(value)
    except (ValueError, TypeError) as exc:
      raise ValidationError(
          f'{field_name} must be an int, got "{value}".'
      ) from exc
    if min_value is not None and coerced < min_value:
      raise ValidationError(
          f"{field_name} must be >= {min_value}, got {coerced!r}."
      )
    if max_value is not None and coerced > max_value:
      raise ValidationError(
          f"{field_name} must be <= {max_value}, got {coerced!r}."
      )
    return coerced

  return _converter

