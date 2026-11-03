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
"""Unit tests for shared `launcher.validators` helpers and validators."""

from __future__ import annotations

from typing import Any
import unittest

import attrs
from launcher.validators import ValidationError
from launcher.validators import check_non_empty_str
from launcher.validators import check_safe_path_segment
from launcher.validators import check_safe_relative_key
from launcher.validators import validate_non_empty_str
from launcher.validators import validate_optional_non_empty_str
from launcher.validators import validate_safe_path_segment


@attrs.define(frozen=True)
class _SampleModel:
  """Synthetic `attrs` model for exercising validator callables."""

  non_empty: str = attrs.field(validator=validate_non_empty_str)
  segment: str = attrs.field(validator=validate_safe_path_segment)
  optional_text: str | None = attrs.field(
      default=None, validator=validate_optional_non_empty_str
  )


class TestValidators(unittest.TestCase):
  """Tests for `launcher.validators` functions and `attrs` validators."""

  def test_non_empty_str_valid(self) -> None:
    for valid in ("a", "file:///tmp/cache", "  padded  ", "line1\nline2"):
      with self.subTest(valid=valid):
        self.assertEqual(
            check_non_empty_str(valid, field_name="cache_uri"), valid
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
        with self.assertRaises(ValidationError):
          check_non_empty_str(invalid, field_name="field")
        with self.assertRaises(ValidationError):
          _SampleModel(non_empty=invalid, segment="task-1")
        if invalid is not None:
          with self.assertRaises(ValidationError):
            _SampleModel(
                non_empty="ok", segment="task-1", optional_text=invalid
            )

  def test_safe_path_segment_valid(self) -> None:
    for segment in ("init", "find-diff", "fix_001", "task.v1"):
      with self.subTest(segment=segment):
        self.assertEqual(
            check_safe_path_segment(segment, field_name="task_id"), segment
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
        with self.assertRaises(ValidationError):
          check_safe_path_segment(invalid, field_name="task_id")
        with self.assertRaises(ValidationError):
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
            check_safe_relative_key(key, field_name="artifact_key"), key
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
        with self.assertRaises(ValidationError):
          check_safe_relative_key(invalid, field_name="artifact_key")


if __name__ == "__main__":
  unittest.main()
