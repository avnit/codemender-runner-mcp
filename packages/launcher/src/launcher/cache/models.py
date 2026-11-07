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

# Governing: SPEC-ARTIFACT-CACHE/REQ-0006
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0003
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0006
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0004
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0006
# Governing: SPEC-WORKER/REQ-0002, SPEC-WORKER/REQ-0003, SPEC-WORKER/REQ-0006
"""Shared wire models for Launcher and Worker over ArtifactCache."""

from collections.abc import Iterable, Mapping
import json
import math
from typing import Any, Self

import attrs
from codemender.runner.commands.find_diff import MAX_DIFF_WORKERS
from codemender.runner.commands.find_diff import MIN_DIFF_WORKERS
from launcher import validators
from launcher.validators import ValidationError

VALID_WORKER_COMMANDS = frozenset({"init", "find-diff", "fix", "verify"})
VALID_RECEIPT_STATUSES = frozenset({"SUCCESS", "FAILURE"})


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
    validators.check_non_empty_str(
        role, field_name="ExecutionReceipt.artifacts role"
    )
    validators.check_safe_relative_key(
        key, field_name=f"ExecutionReceipt.artifacts[{role!r}]"
    )
    validated[role] = key
  return validated


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


# Governing: SPEC-ARTIFACT-CACHE/REQ-0006
# Governing: SPEC-WORKER/REQ-0002, SPEC-WORKER/REQ-0003
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0003
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0006
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0006
@attrs.define(frozen=True)
class RunManifest(_JsonWireModel):
  """Task input manifest (`tasks/<task_id>/input/manifest.json`)."""

  command: str = attrs.field(
      validator=validators.validate_in(VALID_WORKER_COMMANDS)
  )
  task_id: str = attrs.field(validator=validators.validate_safe_path_segment)
  base_ref: str | None = attrs.field(
      default=None, validator=validators.validate_optional_non_empty_str
  )
  finding_key: str | None = attrs.field(
      default=None, validator=validators.validate_optional_finding_key
  )
  diff_workers: int | None = attrs.field(
      default=None,
      converter=attrs.converters.optional(
          validators.convert_int(
              "RunManifest.diff_workers",
              min_value=MIN_DIFF_WORKERS,
              max_value=MAX_DIFF_WORKERS,
          )
      ),
  )
  log_level: str | None = attrs.field(
      default=None, validator=validators.validate_optional_non_empty_str
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


# Governing: SPEC-ARTIFACT-CACHE/REQ-0006
# Governing: SPEC-WORKER/REQ-0002, SPEC-WORKER/REQ-0006, ADR-0001
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0003
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0006
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0004
@attrs.define(frozen=True)
class ExecutionReceipt(_JsonWireModel):
  """Task completion receipt (`tasks/<task_id>/output/receipt.json`)."""

  status: str = attrs.field(
      validator=validators.validate_in(VALID_RECEIPT_STATUSES)
  )
  exit_code: int = attrs.field(
      converter=validators.convert_int("ExecutionReceipt.exit_code")
  )
  command: str = attrs.field(validator=validators.validate_non_empty_str)
  task_id: str = attrs.field(validator=validators.validate_safe_path_segment)
  started_at: str = attrs.field(validator=validators.validate_non_empty_str)
  completed_at: str = attrs.field(validator=validators.validate_non_empty_str)
  duration_seconds: float = attrs.field(
      default=0.0, converter=_convert_duration_seconds
  )
  finding_count: int = attrs.field(
      default=0,
      converter=validators.convert_int(
          "ExecutionReceipt.finding_count", min_value=0
      ),
  )
  artifacts: dict[str, str] = attrs.field(
      factory=dict, converter=_convert_artifacts, hash=False
  )
  error: str | None = attrs.field(
      default=None, validator=validators.validate_optional_non_empty_str
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

