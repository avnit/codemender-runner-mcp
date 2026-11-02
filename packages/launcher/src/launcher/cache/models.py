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

# Governing: ADR-0007, ADR-0009, SPEC-ARTIFACT-CACHE/REQ-0006, SPEC-WORKER/REQ-0002, SPEC-WORKER/REQ-0003, SPEC-WORKER/REQ-0006
"""Shared wire models for Launcher and Worker over ArtifactCache."""

from collections.abc import Iterable, Mapping
import json
import math
from typing import Any, Self

import attrs
from codemender.runner.commands.find_diff import (
    MAX_DIFF_WORKERS,
    MIN_DIFF_WORKERS,
)

from launcher.cache._validation import (
    finding_key_error,
    relative_key_error,
    single_segment_error,
)

VALID_WORKER_COMMANDS = frozenset({"init", "find-diff", "fix", "verify"})
VALID_RECEIPT_STATUSES = frozenset({"SUCCESS", "FAILURE"})


class ValidationError(Exception):
  """Raised when a cache wire model fails schema or field validation."""


def _check_non_empty_str(value: Any, field_name: str) -> str:
  """Validates that `value` is a non-empty string without NUL bytes."""
  if not isinstance(value, str) or not value.strip() or "\x00" in value:
    raise ValidationError(f"{field_name} must be a non-empty string.")
  return value


def _validate_non_empty_str(
    instance: Any, attribute: attrs.Attribute, value: str
) -> None:
  del instance
  _check_non_empty_str(value, attribute.name)


def _validate_optional_non_empty_str(
    instance: Any, attribute: attrs.Attribute, value: str | None
) -> None:
  del instance
  if value is not None:
    _check_non_empty_str(value, attribute.name)


def _check_safe_path_segment(value: Any, field_name: str) -> str:
  """Validates that `value` is a single safe path segment."""
  reason = single_segment_error(value)
  if reason is not None:
    raise ValidationError(
        f"{field_name} must be a single safe path segment: {value!r}"
    )
  return value


def _validate_safe_path_segment(
    instance: Any, attribute: attrs.Attribute, value: str
) -> None:
  del instance
  _check_safe_path_segment(value, attribute.name)


def _check_safe_relative_key(value: Any, field_name: str) -> str:
  """Validates that `value` is a safe relative POSIX key."""
  reason = relative_key_error(value)
  if reason is not None:
    raise ValidationError(
        f"{field_name} must be a normalized relative POSIX key: {value!r}"
    )
  return value


def _validate_finding_key(
    instance: Any, attribute: attrs.Attribute, value: str | None
) -> None:
  del instance
  if value is not None:
    reason = finding_key_error(value)
    if reason is not None:
      raise ValidationError(f"{attribute.name} {reason}: {value!r}")


def _validate_command(
    instance: Any, attribute: attrs.Attribute, value: str
) -> None:
  del instance
  _check_non_empty_str(value, attribute.name)
  if value not in VALID_WORKER_COMMANDS:
    raise ValidationError(
        f"Unsupported RunManifest.command {value!r}; must be one of"
        f" {sorted(VALID_WORKER_COMMANDS)}."
    )


def _convert_int(value: Any, field_name: str) -> int:
  """Converts `value` to an int, rejecting booleans, floats, and non-ints."""
  if isinstance(value, (bool, float)):
    raise ValidationError(f'{field_name} must be an int, got "{value}".')
  try:
    return int(value)
  except (ValueError, TypeError) as exc:
    raise ValidationError(
        f'{field_name} must be an int, got "{value}".'
    ) from exc


def _convert_optional_int(value: Any) -> int | None:
  if value is None:
    return None
  return _convert_int(value, "RunManifest.diff_workers")


def _validate_diff_workers(
    instance: Any, attribute: attrs.Attribute, value: int | None
) -> None:
  del instance, attribute
  if value is not None and not (MIN_DIFF_WORKERS <= value <= MAX_DIFF_WORKERS):
    raise ValidationError(
        f"RunManifest.diff_workers must be in"
        f" {MIN_DIFF_WORKERS}..{MAX_DIFF_WORKERS}, got {value!r}."
    )


def _convert_extra_args(value: Any) -> tuple[str, ...]:
  if value is None:
    return ()
  if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
    raise ValidationError(
        "RunManifest.extra_args must be a sequence of strings, not a bare"
        " string or non-iterable."
    )
  coerced: list[str] = []
  for idx, arg in enumerate(value):
    if not isinstance(arg, str) or "\x00" in arg:
      raise ValidationError(
          f"RunManifest.extra_args[{idx}] must be a string without NUL"
          f" bytes, got {arg!r}."
      )
    coerced.append(arg)
  return tuple(coerced)


def _validate_non_negative_int(
    instance: Any, attribute: attrs.Attribute, value: int
) -> None:
  del instance
  if value < 0:
    raise ValidationError(f"{attribute.name} must be >= 0, got {value!r}.")


def _convert_duration_seconds(value: Any) -> float:
  if isinstance(value, bool):
    raise ValidationError(
        f'ExecutionReceipt.duration_seconds must be a number, got "{value}".'
    )
  try:
    duration = float(value)
  except (ValueError, TypeError) as exc:
    raise ValidationError(
        f'ExecutionReceipt.duration_seconds must be a number, got "{value}".'
    ) from exc
  if not math.isfinite(duration) or duration < 0.0:
    raise ValidationError(
        "ExecutionReceipt.duration_seconds must be a finite number >= 0.0,"
        f" got {value!r}."
    )
  return round(duration, 3)


def _convert_artifacts(value: Any) -> dict[str, str]:
  if value is None:
    return {}
  if not isinstance(value, Mapping):
    raise ValidationError(
        "ExecutionReceipt.artifacts must be a mapping of role -> relative"
        f" cache key, got {type(value).__name__}."
    )
  validated: dict[str, str] = {}
  for role, key in value.items():
    _check_non_empty_str(role, "ExecutionReceipt.artifacts role")
    _check_safe_relative_key(key, f"ExecutionReceipt.artifacts[{role!r}]")
    validated[role] = key
  return validated


def _validate_receipt_status(
    instance: Any, attribute: attrs.Attribute, value: str
) -> None:
  del instance, attribute
  if value not in VALID_RECEIPT_STATUSES:
    raise ValidationError(
        f"ExecutionReceipt.status must be one of"
        f" {sorted(VALID_RECEIPT_STATUSES)}, got {value!r}."
    )


class _JsonWireModel:
  """Shared JSON/dict serialization mixin for frozen `attrs` wire models."""

  @classmethod
  def from_dict(cls, data: Mapping[str, Any]) -> Self:
    """Deserializes and validates an instance of `cls` from a mapping."""
    if not isinstance(data, Mapping):
      raise ValidationError(
          f"{cls.__name__} data must be a mapping, got {type(data).__name__}."
      )
    try:
      return cls(**data)
    except TypeError as exc:
      raise ValidationError(f"Invalid {cls.__name__} fields: {exc}") from exc

  @classmethod
  def from_json(cls, raw_json: str | bytes) -> Self:
    """Deserializes and validates an instance of `cls` from UTF-8 JSON."""
    try:
      parsed = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
      raise ValidationError(f"Invalid {cls.__name__} JSON: {exc}") from exc
    return cls.from_dict(parsed)

  def to_dict(self) -> dict[str, Any]:
    """Serializes this model into a dictionary."""
    return attrs.asdict(self)  # type: ignore[arg-type]

  def to_json(self) -> str:
    """Serializes this model into a deterministic JSON string."""
    return json.dumps(self.to_dict(), sort_keys=True)


# Governing: SPEC-WORKER/REQ-0002, SPEC-WORKER/REQ-0003, ADR-0007, ADR-0009
@attrs.define(frozen=True)
class RunManifest(_JsonWireModel):
  """Task input manifest (`tasks/<task_id>/input/manifest.json`)."""

  command: str = attrs.field(validator=_validate_command)
  task_id: str = attrs.field(validator=_validate_safe_path_segment)
  base_ref: str | None = attrs.field(
      default=None, validator=_validate_optional_non_empty_str
  )
  finding_key: str | None = attrs.field(
      default=None, validator=_validate_finding_key
  )
  diff_workers: int | None = attrs.field(
      default=None,
      converter=_convert_optional_int,
      validator=_validate_diff_workers,
  )
  log_level: str | None = attrs.field(
      default=None, validator=_validate_optional_non_empty_str
  )
  extra_args: tuple[str, ...] = attrs.field(
      default=(), converter=_convert_extra_args
  )

  def __attrs_post_init__(self) -> None:
    if self.command == "find-diff" and self.base_ref is None:
      raise ValidationError(
          "RunManifest.base_ref is required when command is 'find-diff'."
      )
    if self.command in ("fix", "verify") and self.finding_key is None:
      raise ValidationError(
          f"RunManifest.finding_key is required when command is"
          f" {self.command!r}."
      )

  def to_dict(self) -> dict[str, Any]:
    """Serializes this manifest into a dictionary, omitting `None` fields."""
    raw = attrs.asdict(self)
    raw["extra_args"] = list(self.extra_args) if self.extra_args else None
    return {key: val for key, val in raw.items() if val is not None}


# Governing: SPEC-WORKER/REQ-0002, SPEC-WORKER/REQ-0006, ADR-0001, ADR-0007, ADR-0009
@attrs.define(frozen=True)
class ExecutionReceipt(_JsonWireModel):
  """Task completion receipt (`tasks/<task_id>/output/receipt.json`)."""

  status: str = attrs.field(validator=_validate_receipt_status)
  exit_code: int = attrs.field(
      converter=lambda v: _convert_int(v, "ExecutionReceipt.exit_code")
  )
  command: str = attrs.field(validator=_validate_non_empty_str)
  task_id: str = attrs.field(validator=_validate_safe_path_segment)
  started_at: str = attrs.field(validator=_validate_non_empty_str)
  completed_at: str = attrs.field(validator=_validate_non_empty_str)
  duration_seconds: float = attrs.field(
      default=0.0, converter=_convert_duration_seconds
  )
  finding_count: int = attrs.field(
      default=0,
      converter=lambda v: _convert_int(v, "ExecutionReceipt.finding_count"),
      validator=_validate_non_negative_int,
  )
  artifacts: dict[str, str] = attrs.field(
      factory=dict, converter=_convert_artifacts, hash=False
  )
  error: str | None = attrs.field(
      default=None, validator=_validate_optional_non_empty_str
  )

  def __attrs_post_init__(self) -> None:
    if self.exit_code == 0 and self.status != "SUCCESS":
      raise ValidationError(
          "ExecutionReceipt.status must be 'SUCCESS' when exit_code == 0."
      )
    if self.exit_code != 0 and self.status != "FAILURE":
      raise ValidationError(
          "ExecutionReceipt.status must be 'FAILURE' when exit_code != 0."
      )
    if self.status == "SUCCESS" and self.error is not None:
      raise ValidationError(
          "ExecutionReceipt.error must be None when status == 'SUCCESS'."
      )
    if self.exit_code not in (0, 3) and self.error is None:
      raise ValidationError(
          "ExecutionReceipt.error must be a non-empty string when exit_code is"
          f" {self.exit_code} (not 0 or 3)."
      )

