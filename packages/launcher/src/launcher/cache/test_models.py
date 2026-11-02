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

# Governing: SPEC-ARTIFACT-CACHE/REQ-0006, SPEC-WORKER/REQ-0002, SPEC-WORKER/REQ-0003, SPEC-WORKER/REQ-0006
"""Unit tests for ArtifactCache wire models."""

import unittest

import attrs
from launcher.cache.models import (
    ExecutionReceipt,
    RunManifest,
    ValidationError,
)


class TestRunManifest(unittest.TestCase):
  """Invariant and round-trip tests for RunManifest."""

  def test_roundtrip_dict_and_json(self):
    cases = [
        RunManifest(command="init", task_id="init-1"),
        RunManifest(
            command="find-diff",
            task_id="find",
            base_ref="origin/main",
            diff_workers="4",  # type: ignore[arg-type]
            log_level="debug",
            extra_args=["--foo", "bar"],  # type: ignore[arg-type]
        ),
        RunManifest(
            command="fix",
            task_id="fix-finding-001",
            finding_key="findings/finding-001.sarif",
        ),
        RunManifest(
            command="verify",
            task_id="verify-finding-001",
            finding_key="findings/finding-001.sarif",
        ),
    ]
    for manifest in cases:
      with self.subTest(command=manifest.command):
        self.assertEqual(RunManifest.from_dict(manifest.to_dict()), manifest)
        self.assertEqual(RunManifest.from_json(manifest.to_json()), manifest)

  def test_frozen_immutability(self):
    manifest = RunManifest(command="init", task_id="init")
    with self.assertRaises(attrs.exceptions.FrozenInstanceError):
      manifest.command = "fix"  # type: ignore[misc]

  def test_validation_invariants(self):
    invalid_inputs = [
        {"command": "unknown", "task_id": "t1"},
        {"command": "find", "task_id": "t1", "base_ref": "main"},
        {"command": "init", "task_id": ""},
        {"command": "init", "task_id": "../escape"},
        {"command": "init", "task_id": "sub/task"},
        {"command": "init", "task_id": "."},
        {"command": "init", "task_id": ".tmp.task"},
        {"command": "init", "task_id": "C:task"},
        {"command": "find-diff", "task_id": "find"},
        {"command": "fix", "task_id": "fix-1"},
        {"command": "verify", "task_id": "verify-1"},
        {
            "command": "fix",
            "task_id": "fix-1",
            "finding_key": "../escape.sarif",
        },
        {
            "command": "fix",
            "task_id": "fix-1",
            "finding_key": "/abs/finding.sarif",
        },
        {
            "command": "fix",
            "task_id": "fix-1",
            "finding_key": "findings/.tmp.f1.sarif",
        },
        {
            "command": "fix",
            "task_id": "fix-1",
            "finding_key": "C:findings/f1.sarif",
        },
        {
            "command": "fix",
            "task_id": "fix-1",
            "finding_key": "shared/code.tar.gz",
        },
        {
            "command": "fix",
            "task_id": "fix-1",
            "finding_key": "findings/finding-001.json",
        },
        {
            "command": "fix",
            "task_id": "fix-1",
            "finding_key": "finding-001",
        },
        {
            "command": "find-diff",
            "task_id": "find",
            "base_ref": "main",
            "diff_workers": 0,
        },
        {
            "command": "find-diff",
            "task_id": "find",
            "base_ref": "main",
            "diff_workers": 17,
        },
        {
            "command": "find-diff",
            "task_id": "find",
            "base_ref": "main",
            "diff_workers": True,
        },
        {
            "command": "find-diff",
            "task_id": "find",
            "base_ref": "main",
            "diff_workers": 2.5,
        },
        {
            "command": "find-diff",
            "task_id": "find",
            "base_ref": "main",
            "diff_workers": "not-an-int",
        },
        {"command": "init", "task_id": "init", "extra_args": "--bare-str"},
    ]
    for payload in invalid_inputs:
      with self.subTest(payload=payload):
        with self.assertRaises(ValidationError):
          RunManifest.from_dict(payload)


class TestExecutionReceipt(unittest.TestCase):
  """Invariant and round-trip tests for ExecutionReceipt."""

  def _base_payload(self, **overrides):
    data = {
        "status": "SUCCESS",
        "exit_code": 0,
        "command": "find-diff",
        "task_id": "find",
        "started_at": "2026-10-20T21:00:00Z",
        "completed_at": "2026-10-20T21:00:42Z",
        "duration_seconds": 42.125,
        "finding_count": 2,
        "artifacts": {
            "log": "tasks/find/output/runner.log",
            "sarif": "tasks/find/output/report.sarif",
        },
        "error": None,
    }
    data.update(overrides)
    return data

  def test_roundtrip_dict_and_json(self):
    cases = [
        ExecutionReceipt.from_dict(self._base_payload()),
        ExecutionReceipt.from_dict(
            self._base_payload(status="FAILURE", exit_code=3, error=None)
        ),
        ExecutionReceipt.from_dict(
            self._base_payload(
                status="FAILURE", exit_code=2, error="Validation failed"
            )
        ),
    ]
    for receipt in cases:
      with self.subTest(status=receipt.status, exit_code=receipt.exit_code):
        self.assertEqual(
            ExecutionReceipt.from_dict(receipt.to_dict()), receipt
        )
        self.assertEqual(
            ExecutionReceipt.from_json(receipt.to_json()), receipt
        )

  def test_validation_invariants(self):
    invalid_overrides = [
        {"status": "FAILURE", "exit_code": 0, "error": None},
        {"status": "SUCCESS", "exit_code": 2, "error": "err"},
        {"status": "SUCCESS", "exit_code": 0, "error": "unexpected"},
        {"status": "FAILURE", "exit_code": 2, "error": None},
        {"status": "FAILURE", "exit_code": 143, "error": ""},
        {"status": "UNKNOWN"},
        {"command": ""},
        {"task_id": "../escape"},
        {"task_id": ".tmp.find"},
        {"task_id": "C:find"},
        {"exit_code": 1.5, "status": "FAILURE", "error": "err"},
        {"duration_seconds": -1.0},
        {"finding_count": -1},
        {"finding_count": 1.5},
        {"artifacts": {"log": "../escape.log"}},
        {"artifacts": {"log": "tasks/find/output/.tmp.runner.log"}},
    ]
    for overrides in invalid_overrides:
      with self.subTest(overrides=overrides):
        with self.assertRaises(ValidationError):
          ExecutionReceipt.from_dict(self._base_payload(**overrides))

  def test_hashable_despite_artifacts_mapping(self):
    first = ExecutionReceipt.from_dict(self._base_payload())
    second = ExecutionReceipt.from_dict(self._base_payload())
    self.assertEqual(hash(first), hash(second))
    self.assertEqual(len({first, second}), 1)


if __name__ == "__main__":
  unittest.main()

