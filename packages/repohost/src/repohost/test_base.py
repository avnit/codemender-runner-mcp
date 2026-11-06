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

"""Unit tests for RepoHost abstract base class."""

from typing import Mapping, Sequence
import unittest

from repohost.base import RepoHost
from repohost.models import (
    ChangeContext,
    Comment,
    SecurityGate,
    SecurityGateState,
    SuggestedEdit,
)


class DummyIncompleteRepoHost(RepoHost):
  """Subclass missing abstract methods."""

  def get_change_context(self) -> ChangeContext:
    return ChangeContext(
        repo_id="owner/repo",
        change_id="1",
        base_sha="a",
        head_sha="b",
        base_ref="main",
        head_ref="feat",
    )


class DummyCompleteRepoHost(RepoHost):
  """Subclass implementing all abstract methods."""

  def get_change_context(self) -> ChangeContext:
    return ChangeContext(
        repo_id="owner/repo",
        change_id="1",
        base_sha="a",
        head_sha="b",
        base_ref="main",
        head_ref="feat",
    )

  def list_existing_comments(self) -> Sequence[Comment]:
    return []

  def post_suggestion(self, suggestion: SuggestedEdit) -> Comment:
    return Comment(
        comment_id="c1",
        url="https://example.com/c1",
        finding_id=suggestion.finding_id,
    )

  def reply_to_comment(self, comment_id: str, body: str) -> Comment:
    return Comment(
        comment_id=f"reply-{comment_id}",
        url="https://example.com/reply",
        finding_id=None,
        body=body,
    )

  def upsert_summary(self, body: str) -> Comment:
    return Comment(
        comment_id="summary-1",
        url="https://example.com/summary",
        finding_id=None,
        kind="summary",
        body=body,
    )

  def set_security_gate(self, gate: SecurityGate) -> None:
    pass

  def emit_ci_outputs(self, outputs: Mapping[str, str]) -> None:
    pass

  def add_reaction(self, comment_id: str, reaction: str) -> None:
    pass


class TestRepoHostBase(unittest.TestCase):
  """Tests for RepoHost ABC instantiation and interface contract."""

  def test_cannot_instantiate_abc_directly(self):
    with self.assertRaises(TypeError):
      RepoHost()  # type: ignore[abstract]

  def test_cannot_instantiate_incomplete_subclass(self):
    with self.assertRaises(TypeError) as ctx:
      DummyIncompleteRepoHost()  # type: ignore[abstract]
    self.assertIn("abstract method", str(ctx.exception).lower())

  def test_can_instantiate_complete_subclass(self):
    host = DummyCompleteRepoHost()
    self.assertIsInstance(host, RepoHost)

    ctx = host.get_change_context()
    self.assertEqual(ctx.repo_id, "owner/repo")

    comments = host.list_existing_comments()
    self.assertEqual(comments, [])

    sugg = SuggestedEdit(
        file_path="foo.py",
        line=10,
        patch="diff",
        finding_id="f1",
    )
    sugg_res = host.post_suggestion(sugg)
    self.assertEqual(sugg_res.comment_id, "c1")

    reply = host.reply_to_comment("c1", "verified")
    self.assertEqual(reply.comment_id, "reply-c1")

    summary = host.upsert_summary("summary")
    self.assertEqual(summary.comment_id, "summary-1")

    gate = SecurityGate(state=SecurityGateState.SUCCESS)
    host.set_security_gate(gate)

    host.emit_ci_outputs({"key": "value"})

    host.add_reaction("c1", "eyes")


if __name__ == "__main__":
  unittest.main()
