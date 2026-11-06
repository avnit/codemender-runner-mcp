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

# Governing: ADR-0008, ADR-0006
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0001
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0003

"""Unit tests for RepoHost domain models and exceptions."""

import dataclasses
import unittest

from repohost.models import (
    ChangeContext,
    Comment,
    RepoHostAuthError,
    RepoHostError,
    RepoHostRateLimitError,
    SecurityGate,
    SecurityGateState,
    SuggestedEdit,
)


class TestRepoHostModels(unittest.TestCase):
  """Tests asserting key invariants on domain models and exceptions."""

  def test_security_gate_state_values(self):
    self.assertEqual(SecurityGateState.PENDING, "PENDING")
    self.assertEqual(SecurityGateState.SUCCESS, "SUCCESS")
    self.assertEqual(SecurityGateState.FAILURE, "FAILURE")
    self.assertEqual(SecurityGateState.ERROR, "ERROR")

  def test_models_are_frozen_immutable(self):
    ctx = ChangeContext(
        repo_id="owner/repo",
        change_id="123",
        base_sha="abc1234",
        head_sha="def5678",
        base_ref="main",
        head_ref="feature",
    )
    with self.assertRaises(dataclasses.FrozenInstanceError):
      ctx.base_sha = "new_sha"  # type: ignore[misc]

    gate = SecurityGate(state=SecurityGateState.SUCCESS)
    with self.assertRaises(dataclasses.FrozenInstanceError):
      gate.description = "Mutated"  # type: ignore[misc]

    suggestion = SuggestedEdit(
        finding_id="finding-001",
        patch="unified diff",
    )
    with self.assertRaises(dataclasses.FrozenInstanceError):
      suggestion.line = 43  # type: ignore[misc]

    comment = Comment(
        comment_id="comment-101",
        url="https://example.com/c1",
    )
    with self.assertRaises(dataclasses.FrozenInstanceError):
      comment.outdated = True  # type: ignore[misc]

  def test_exception_hierarchy(self):
    auth_err = RepoHostAuthError("Token expired")
    rate_err = RepoHostRateLimitError("Rate limit exceeded")

    self.assertIsInstance(auth_err, RepoHostError)
    self.assertIsInstance(rate_err, RepoHostError)
    self.assertIsInstance(auth_err, Exception)


if __name__ == "__main__":
  unittest.main()
