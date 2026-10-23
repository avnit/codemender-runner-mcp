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

# Governing: ADR-0007, ADR-0009, SPEC-WORKER/REQ-0002, SPEC-WORKER/REQ-0003, SPEC-WORKER/REQ-0006
"""Shared wire models for Launcher and Worker over ArtifactCache."""

from collections.abc import Iterable, Mapping
import json
import math
from pathlib import PurePosixPath
from typing import Any, Self

import attrs

VALID_WORKER_COMMANDS = frozenset({"init", "find-diff", "fix", "verify"})
VALID_RECEIPT_STATUSES = frozenset({"SUCCESS", "FAILURE"})
MIN_DIFF_WORKERS = 1
MAX_DIFF_WORKERS = 16


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
  """Attrs validator ensuring a string attribute is non-empty without NULs."""
  del instance
  _check_non_empty_str(value, attribute.name)


def _validate_optional_non_empty_str(
    instance: Any, attribute: attrs.Attribute, value: str | None
) -> None:
  """Attrs validator ensuring an optional string attribute is non-empty."""
  del instance
  if value is not None:
    _check_non_empty_str(value, attribute.name)


def _check_safe_path_segment(value: Any, field_name: str) -> str:
  """Validates that `value` is a single safe path segment using `pathlib`."""
  _check_non_empty_str(value, field_name)
  path = PurePosixPath(value)
  if (
      path.is_absolute()
      or "\\" in value
      or len(path.parts) != 1
      or path.parts[0] in (".", "..")
      or path.as_posix() != value
  ):
    raise ValidationError(
        f"{field_name} must be a single safe path segment: {value!r}"
    )
  return value


def _validate_safe_path_segment(
    instance: Any, attribute: attrs.Attribute, value: str
) -> None:
  """Attrs validator ensuring `attribute` is a single safe path segment."""
  del instance
  _check_safe_path_segment(value, attribute.name)


def _check_safe_relative_key(value: Any, field_name: str) -> str:
  """Validates that `value` is a safe relative POSIX key using `pathlib`."""
  _check_non_empty_str(value, field_name)
  path = PurePosixPath(value)
  if (
      path.is_absolute()
      or "\\" in value
      or not path.parts
      or ".." in path.parts
      or path.as_posix() != value
  ):
    raise ValidationError(
        f"{field_name} must be a normalized relative POSIX key: {value!r}"
    )
  return value


def _validate_safe_relative_key(
    instance: Any, attribute: attrs.Attribute, value: str | None
) -> None:
  """Attrs validator ensuring an optional attribute is a safe relative key."""
  del instance
  if value is not None:
    _check_safe_relative_key(value, attribute.name)


def _validate_command(
    instance: Any, attribute: attrs.Attribute, value: str
) -> None:
  """Validates that `RunManifest.command` is a supported worker command."""
  del instance
  _check_non_empty_str(value, attribute.name)
  if value not in VALID_WORKER_COMMANDS:
    raise ValidationError(
        f"Unsupported RunManifest.command {value!r}; must be one of"
        f" {sorted(VALID_WORKER_COMMANDS)}."
    )


def _convert_optional_int(value: Any) -> int | None:
  """Converts `value` to `int` when not `None`, raising `ValidationError`."""
  if value is None:
    return None
  if isinstance(value, bool):
    raise ValidationError(
        f'RunManifest.diff_workers must be an int, got "{value}".'
    )
  try:
    return int(value)
  except (ValueError, TypeError) as exc:
    raise ValidationError(
        f'RunManifest.diff_workers must be an int, got "{value}".'
    ) from exc


def _validate_diff_workers(
    instance: Any,
    attribute: attrs.Attribute,
    value: int | None,
) -> None:
  """Validates `RunManifest.diff_workers` is within `1..16` when set."""
  del instance, attribute
  if value is not None and not (MIN_DIFF_WORKERS <= value <= MAX_DIFF_WORKERS):
    raise ValidationError(
        f"RunManifest.diff_workers must be in"
        f" {MIN_DIFF_WORKERS}..{MAX_DIFF_WORKERS}, got {value!r}."
    )


def _convert_extra_args(value: Any) -> tuple[str, ...]:
  """Validates and converts `RunManifest.extra_args` into a tuple of strings."""
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


def _convert_int(value: Any, field_name: str) -> int:
  """Converts `value` to an integer, rejecting booleans and non-integers."""
  if isinstance(value, bool):
    raise ValidationError(f'{field_name} must be an int, got "{value}".')
  try:
    return int(value)
  except (ValueError, TypeError) as exc:
    raise ValidationError(
        f'{field_name} must be an int, got "{value}".'
    ) from exc


def _convert_exit_code(value: Any) -> int:
  """Converts `ExecutionReceipt.exit_code` to an integer."""
  return _convert_int(value, "ExecutionReceipt.exit_code")


def _convert_finding_count(value: Any) -> int:
  """Converts `ExecutionReceipt.finding_count` to an integer."""
  return _convert_int(value, "ExecutionReceipt.finding_count")


def _validate_non_negative_int(
    instance: Any, attribute: attrs.Attribute, value: int
) -> None:
  """Validates that an integer attribute is non-negative."""
  del instance
  if value < 0:
    raise ValidationError(f"{attribute.name} must be >= 0, got {value!r}.")


def _convert_duration_seconds(value: Any) -> float:
  """Converts `ExecutionReceipt.duration_seconds` to a finite float."""
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
  """Validates and copies `ExecutionReceipt.artifacts`."""
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
  """Validates `ExecutionReceipt.status` is `'SUCCESS'` or `'FAILURE'`."""
  del instance, attribute
  if value not in VALID_RECEIPT_STATUSES:
    raise ValidationError(
        f"ExecutionReceipt.status must be one of"
        f" {sorted(VALID_RECEIPT_STATUSES)}, got {value!r}."
    )


# Governing: SPEC-WORKER/REQ-0002, SPEC-WORKER/REQ-0003, ADR-0007, ADR-0009
@attrs.define(frozen=True)
class RunManifest:
  """Task input manifest (`tasks/<task_id>/input/manifest.json`).

  Attributes:
    command: Canonical `cm-runner` subcommand (`"init"`, `"find-diff"`,
      `"fix"`, or `"verify"`).
    task_id: Single-segment logical task identifier.
    base_ref: Target Git base ref or commit SHA to diff `HEAD` against
      (required when `command == "find-diff"`; `None` for `"init"`, `"fix"`,
      and `"verify"`).
    finding_key: Relative `ArtifactCache` key for the single-finding SARIF
      input (required when `command` is `"fix"` or `"verify"`).
    diff_workers: Optional worker concurrency in `1..16` for `"find-diff"`.
    log_level: Optional log verbosity passed as `--log-level <level>`.
    extra_args: Optional tuple of additional CLI arguments for `cm-runner`.
  """

  command: str = attrs.field(validator=_validate_command)
  task_id: str = attrs.field(validator=_validate_safe_path_segment)
  base_ref: str | None = attrs.field(
      default=None, validator=_validate_optional_non_empty_str
  )
  finding_key: str | None = attrs.field(
      default=None, validator=_validate_safe_relative_key
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

  @classmethod
  def from_dict(cls, data: Mapping[str, Any]) -> Self:
    """Deserializes and validates a `RunManifest` from a dictionary.

    Args:
      data: Mapping containing manifest fields.

    Returns:
      Validated `RunManifest` instance.

    Raises:
      ValidationError: If `data` is not a mapping or fails validation.
    """
    if not isinstance(data, Mapping):
      raise ValidationError(
          f"RunManifest data must be a mapping, got {type(data).__name__}."
      )
    try:
      return cls(**data)
    except TypeError as exc:
      raise ValidationError(f"Invalid RunManifest fields: {exc}") from exc

  @classmethod
  def from_json(cls, raw_json: str | bytes) -> Self:
    """Deserializes and validates a `RunManifest` from a JSON string.

    Args:
      raw_json: UTF-8 JSON string or bytes representing a manifest object.

    Returns:
      Validated `RunManifest` instance.

    Raises:
      ValidationError: If `raw_json` is not valid JSON or fails validation.
    """
    try:
      parsed = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
      raise ValidationError(f"Invalid RunManifest JSON: {exc}") from exc
    return cls.from_dict(parsed)

  def to_dict(self) -> dict[str, Any]:
    """Serializes this manifest into a dictionary, omitting `None` fields."""
    raw = attrs.asdict(self)
    raw["extra_args"] = list(self.extra_args) if self.extra_args else None
    return {key: val for key, val in raw.items() if val is not None}

  def to_json(self) -> str:
    """Serializes this manifest into a deterministic JSON string."""
    return json.dumps(self.to_dict(), sort_keys=True)


# Governing: SPEC-WORKER/REQ-0002, SPEC-WORKER/REQ-0006, ADR-0001, ADR-0007, ADR-0009
@attrs.define(frozen=True)
class ExecutionReceipt:
  """Task completion receipt (`tasks/<task_id>/output/receipt.json`).

  Attributes:
    status: `"SUCCESS"` when `exit_code == 0`, `"FAILURE"` otherwise.
    exit_code: Numeric worker exit code (`0`, `2`, `3`, `130`, `143`).
    command: Executed `cm-runner` command (or `"unknown"` on manifest error).
    task_id: Single-segment task identifier.
    started_at: ISO 8601 UTC start timestamp.
    completed_at: ISO 8601 UTC completion timestamp.
    duration_seconds: Non-negative elapsed wall-clock seconds.
    finding_count: Count of non-dismissed findings in the output SARIF.
    artifacts: Mapping of logical artifact roles to relative cache keys.
    error: `None` on success or normal `cm-runner` exit `3` (`no_fix`);
      non-empty string on worker, validation, infrastructure, or signal error.
  """

  status: str = attrs.field(validator=_validate_receipt_status)
  exit_code: int = attrs.field(converter=_convert_exit_code)
  command: str = attrs.field(validator=_validate_non_empty_str)
  task_id: str = attrs.field(validator=_validate_safe_path_segment)
  started_at: str = attrs.field(validator=_validate_non_empty_str)
  completed_at: str = attrs.field(validator=_validate_non_empty_str)
  duration_seconds: float = attrs.field(
      default=0.0, converter=_convert_duration_seconds
  )
  finding_count: int = attrs.field(
      default=0,
      converter=_convert_finding_count,
      validator=_validate_non_negative_int,
  )
  artifacts: dict[str, str] = attrs.field(
      factory=dict, converter=_convert_artifacts
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

  @classmethod
  def from_dict(cls, data: Mapping[str, Any]) -> Self:
    """Deserializes and validates an `ExecutionReceipt` from a dictionary.

    Args:
      data: Mapping containing receipt fields.

    Returns:
      Validated `ExecutionReceipt` instance.

    Raises:
      ValidationError: If `data` is not a mapping or fails validation.
    """
    if not isinstance(data, Mapping):
      raise ValidationError(
          f"ExecutionReceipt data must be a mapping, got {type(data).__name__}."
      )
    try:
      return cls(**data)
    except TypeError as exc:
      raise ValidationError(f"Invalid ExecutionReceipt fields: {exc}") from exc

  @classmethod
  def from_json(cls, raw_json: str | bytes) -> Self:
    """Deserializes and validates an `ExecutionReceipt` from a JSON string.

    Args:
      raw_json: UTF-8 JSON string or bytes representing a receipt object.

    Returns:
      Validated `ExecutionReceipt` instance.

    Raises:
      ValidationError: If `raw_json` is not valid JSON or fails validation.
    """
    try:
      parsed = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
      raise ValidationError(f"Invalid ExecutionReceipt JSON: {exc}") from exc
    return cls.from_dict(parsed)

  def to_dict(self) -> dict[str, Any]:
    """Serializes this receipt into a dictionary."""
    return attrs.asdict(self)

  def to_json(self) -> str:
    """Serializes this receipt into a deterministic JSON string."""
    return json.dumps(self.to_dict(), sort_keys=True)
