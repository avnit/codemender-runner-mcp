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
"""Unit tests for shared `launcher.validators` helpers and validators."""

from __future__ import annotations

from typing import Any
import unittest

import attrs
from launcher import validators


@attrs.define(frozen=True)
class _SampleModel:
  """Synthetic `attrs` model for exercising validators and converters."""

  non_empty: str = attrs.field(validator=validators.validate_non_empty_str)
  segment: str = attrs.field(validator=validators.validate_safe_path_segment)
  optional_text: str | None = attrs.field(
      default=None, validator=validators.validate_optional_non_empty_str
  )
  finding_key: str | None = attrs.field(
      default=None, validator=validators.validate_optional_finding_key
  )
  mode: str = attrs.field(
      default="init", validator=validators.validate_in({"init", "fix"})
  )
  count: int = attrs.field(
      default=0,
      converter=validators.convert_int("count", min_value=0, max_value=10),
  )
  optional_count: int | None = attrs.field(
      default=None,
      converter=attrs.converters.optional(
          validators.convert_int("optional_count", min_value=1, max_value=16)
      ),
  )


class TestValidators(unittest.TestCase):
  """Tests for `launcher.validators` functions and `attrs` validators."""

  def test_constants_and_canonical_finding_key(self) -> None:
    self.assertEqual(validators.FINDINGS_DIR, "findings")
    self.assertEqual(validators.SARIF_SUFFIX, ".sarif")
    cases = (
        ("f-001", "findings/f-001.sarif"),
        ("f-001.sarif", "findings/f-001.sarif"),
        ("findings/f-001.sarif", "findings/f-001.sarif"),
        ("other/f-001.sarif", "other/f-001.sarif"),
        ("findings\\f-001.sarif", "findings\\f-001.sarif"),
    )
    for raw, expected in cases:
      with self.subTest(raw=raw):
        self.assertEqual(validators.canonical_finding_key(raw), expected)

  def test_non_empty_str_error_and_strip_if_str(self) -> None:
    self.assertIsNone(validators.non_empty_str_error("ok"))
    self.assertEqual(validators.non_empty_str_error(123), "must be a string")
    self.assertEqual(
        validators.non_empty_str_error("   "), "must be a non-empty string"
    )
    self.assertEqual(
        validators.non_empty_str_error("a\x00b"), "must not contain NUL bytes"
    )
    self.assertEqual(validators.strip_if_str("  hello  "), "hello")
    for non_str in (None, 0, 123, True, False, ["x"]):
      with self.subTest(non_str=non_str):
        self.assertIs(validators.strip_if_str(non_str), non_str)

  def test_non_empty_str_valid(self) -> None:
    for valid in ("a", "file:///tmp/cache", "  padded  ", "line1\nline2"):
      with self.subTest(valid=valid):
        self.assertEqual(
            validators.check_non_empty_str(valid, field_name="cache_uri"),
            valid,
        )
        model = _SampleModel(
            non_empty=valid, segment="task-1", optional_text=valid
        )
        self.assertEqual(model.non_empty, valid)
        self.assertEqual(model.optional_text, valid)

  def test_optional_non_empty_str_allows_none(self) -> None:
    model = _SampleModel(
        non_empty="ok", segment="task-1", optional_text=None
    )
    self.assertIsNone(model.optional_text)

  def test_non_empty_str_rejects_invalid_values(self) -> None:
    invalid_values: tuple[Any, ...] = (
        "",
        "   ",
        "\t\n",
        "bad\x00nul",
        None,
        123,
        ["not-a-str"],
    )
    for invalid in invalid_values:
      with self.subTest(invalid=invalid):
        with self.assertRaises(validators.ValidationError):
          validators.check_non_empty_str(invalid, field_name="field")
        with self.assertRaises(validators.ValidationError):
          _SampleModel(non_empty=invalid, segment="task-1")
        if invalid is not None:
          with self.assertRaises(validators.ValidationError):
            _SampleModel(
                non_empty="ok", segment="task-1", optional_text=invalid
            )

  def test_safe_path_segment_valid(self) -> None:
    for segment in ("init", "find-diff", "fix_001", "task.v1"):
      with self.subTest(segment=segment):
        self.assertEqual(
            validators.check_safe_path_segment(
                segment, field_name="task_id"
            ),
            segment,
        )
        model = _SampleModel(non_empty="ok", segment=segment)
        self.assertEqual(model.segment, segment)

  def test_safe_path_segment_rejects_invalid_values(self) -> None:
    invalid_segments: tuple[Any, ...] = (
        "",
        "   ",
        "bad\x00nul",
        None,
        42,
        ".",
        "..",
        "../escape",
        "/abs",
        "nested/segment",
        "back\\slash",
        "C:drive",
        "host:port",
        ".tmp.staging",
        "trailing/",
    )
    for invalid in invalid_segments:
      with self.subTest(invalid=invalid):
        with self.assertRaises(validators.ValidationError):
          validators.check_safe_path_segment(invalid, field_name="task_id")
        with self.assertRaises(validators.ValidationError):
          _SampleModel(non_empty="ok", segment=invalid)

  def test_safe_relative_key_valid(self) -> None:
    valid_keys = (
        "report.sarif",
        "findings/finding-001.sarif",
        "tasks/find/output/runner.log",
        "shared/code.tar.gz",
    )
    for key in valid_keys:
      with self.subTest(key=key):
        self.assertEqual(
            validators.check_safe_relative_key(
                key, field_name="artifact_key"
            ),
            key,
        )

  def test_safe_relative_key_rejects_invalid_values(self) -> None:
    invalid_keys: tuple[Any, ...] = (
        "",
        "   ",
        "bad\x00nul",
        None,
        99,
        ".",
        "..",
        "../escape.log",
        "tasks/../../escape.log",
        "tasks/./runner.log",
        "/abs/path.log",
        "tasks\\find\\runner.log",
        "C:tasks/runner.log",
        "tasks/C:runner.log",
        ".tmp.root",
        "tasks/find/output/.tmp.runner.log",
        "tasks//runner.log",
        "tasks/runner.log/",
    )
    for invalid in invalid_keys:
      with self.subTest(invalid=invalid):
        with self.assertRaises(validators.ValidationError):
          validators.check_safe_relative_key(
              invalid, field_name="artifact_key"
          )

  def test_finding_key_validators(self) -> None:
    for valid_key in ("findings/f-1.sarif", "findings/rule_02.sarif"):
      with self.subTest(valid_key=valid_key):
        self.assertIsNone(validators.finding_key_error(valid_key))
        model = _SampleModel(
            non_empty="ok", segment="t1", finding_key=valid_key
        )
        self.assertEqual(model.finding_key, valid_key)

    self.assertIsNone(_SampleModel(non_empty="ok", segment="t1").finding_key)

    invalid_finding_keys: tuple[Any, ...] = (
        "",
        "finding-1",
        "finding-1.sarif",
        "findings/.sarif",
        "findings/.tmp.f1.sarif",
        "findings/f1.json",
        "other/f1.sarif",
        "findings/sub/f1.sarif",
        "../findings/f1.sarif",
        123,
    )
    attr = attrs.fields(_SampleModel).finding_key
    for invalid in invalid_finding_keys:
      with self.subTest(invalid=invalid):
        self.assertIsNotNone(validators.finding_key_error(invalid))
        with self.assertRaises(validators.ValidationError):
          validators.validate_finding_key(None, attr, invalid)
        with self.assertRaises(validators.ValidationError):
          _SampleModel(non_empty="ok", segment="t1", finding_key=invalid)

  def test_validate_in(self) -> None:
    for valid_mode in ("init", "fix"):
      with self.subTest(valid_mode=valid_mode):
        model = _SampleModel(non_empty="ok", segment="t1", mode=valid_mode)
        self.assertEqual(model.mode, valid_mode)

    for invalid_mode in ("unknown", "", "   ", 123, None, True):
      with self.subTest(invalid_mode=invalid_mode):
        with self.assertRaises(validators.ValidationError):
          _SampleModel(
              non_empty="ok",
              segment="t1",
              mode=invalid_mode,  # type: ignore[arg-type]
          )

  def test_convert_int_and_optional_bounds(self) -> None:
    valid_cases = (
        (0, None, 0, None),
        (10, 1, 10, 1),
        ("5", "16", 5, 16),
    )
    for count_in, opt_in, expected_count, expected_opt in valid_cases:
      with self.subTest(count_in=count_in, opt_in=opt_in):
        model = _SampleModel(
            non_empty="ok",
            segment="t1",
            count=count_in,  # type: ignore[arg-type]
            optional_count=opt_in,  # type: ignore[arg-type]
        )
        self.assertEqual(model.count, expected_count)
        self.assertEqual(model.optional_count, expected_opt)

    invalid_counts: tuple[Any, ...] = (
        -1,
        11,
        True,
        False,
        1.5,
        "not-an-int",
        None,
        [],
    )
    for bad_count in invalid_counts:
      with self.subTest(bad_count=bad_count):
        with self.assertRaises(validators.ValidationError):
          _SampleModel(non_empty="ok", segment="t1", count=bad_count)

    for bad_opt in (0, 17, True, 2.5, "bad"):
      with self.subTest(bad_opt=bad_opt):
        with self.assertRaises(validators.ValidationError):
          _SampleModel(
              non_empty="ok",
              segment="t1",
              optional_count=bad_opt,  # type: ignore[arg-type]
          )


if __name__ == "__main__":
  unittest.main()

