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

"""Unit and contract tests for InMemoryRepoHost test double."""

import unittest

from repohost.base import RepoHost
from repohost.models import (
    ChangeContext,
    Comment,
    SecurityGate,
    SecurityGateState,
    SuggestedEdit,
)
from repohost.testing import InMemoryRepoHost


class TestInMemoryRepoHost(unittest.TestCase):
  """Tests for InMemoryRepoHost state tracking and simulation."""

  def setUp(self):
    self.context = ChangeContext(
        repo_id="google/codemender-runner",
        change_id="42",
        base_sha="1111aaa",
        head_sha="2222bbb",
        base_ref="main",
        head_ref="fix/feature",
    )
    self.host = InMemoryRepoHost(change_context=self.context)

  def test_is_instance_of_repohost_abc(self):
    self.assertIsInstance(self.host, RepoHost)

  def test_get_change_context(self):
    self.assertEqual(self.host.get_change_context(), self.context)

  def test_default_context_when_not_provided(self):
    default_host = InMemoryRepoHost()
    ctx = default_host.get_change_context()
    self.assertEqual(ctx.repo_id, "default/repo")
    self.assertEqual(ctx.change_id, "1")

  def test_list_existing_comments_seeded(self):
    seed_comment = Comment(
        comment_id="seed-1",
        url="https://example.com/seed-1",
        finding_id="f0",
    )
    host = InMemoryRepoHost(
        change_context=self.context, existing_comments=[seed_comment]
    )
    comments = host.list_existing_comments()
    self.assertEqual(len(comments), 1)
    self.assertEqual(comments[0].comment_id, "seed-1")

  def test_post_suggestion_inline(self):
    suggestion = SuggestedEdit(
        finding_id="finding-101",
        patch="diff --git a/b b/b",
        file_path="src/app.py",
        line=15,
        summary="Use safe helper",
        replacement="safe_helper()",
    )
    created = self.host.post_suggestion(suggestion)
    self.assertEqual(created.finding_id, "finding-101")
    self.assertEqual(created.kind, "suggestion")
    self.assertEqual(len(self.host.posted_suggestions), 1)
    self.assertEqual(self.host.posted_suggestions[0], suggestion)
    self.assertIn(created, self.host.list_existing_comments())

  def test_post_suggestion_fallback_patch_with_line_returns_patch_kind(self):
    suggestion = SuggestedEdit(
        finding_id="finding-102",
        patch="diff --git a/b b/b",
        file_path="src/app.py",
        line=15,
        summary="Fallback patch with line",
    )
    created = self.host.post_suggestion(suggestion)
    self.assertEqual(created.finding_id, "finding-102")
    self.assertEqual(created.kind, "patch")
    self.assertEqual(len(self.host.posted_suggestions), 1)

  def test_post_suggestion_fallback_patch(self):
    suggestion = SuggestedEdit(
        finding_id="finding-202",
        patch="diff --git a/b b/b",
        summary="Fallback diff outside hunk",
    )
    created = self.host.post_suggestion(suggestion)
    self.assertEqual(created.finding_id, "finding-202")
    self.assertEqual(created.kind, "patch")
    self.assertEqual(len(self.host.posted_suggestions), 1)
    self.assertEqual(self.host.posted_suggestions[0], suggestion)
    self.assertIn(created, self.host.list_existing_comments())

  def test_reply_to_comment(self):
    reply = self.host.reply_to_comment("seed-1", "Verification passed!")
    self.assertEqual(reply.body, "Verification passed!")
    self.assertEqual(len(self.host.replies), 1)
    self.assertEqual(self.host.replies[0], ("seed-1", "Verification passed!"))

  def test_upsert_summary(self):
    first = self.host.upsert_summary("Initial summary")
    self.assertEqual(first.kind, "summary")
    self.assertEqual(first.body, "Initial summary")
    self.assertEqual(self.host.current_summary, "Initial summary")

    second = self.host.upsert_summary("Updated summary")
    self.assertEqual(second.comment_id, first.comment_id)
    self.assertEqual(second.body, "Updated summary")
    self.assertEqual(self.host.current_summary, "Updated summary")
    self.assertEqual(len(self.host.summaries), 2)

  def test_upsert_summary_with_seeded_summary_comment(self):
    seeded_summary = Comment(
        comment_id="seeded-summary-99",
        url="https://example.com/summary",
        kind="summary",
        body="Seeded summary",
    )
    host = InMemoryRepoHost(
        change_context=self.context, existing_comments=[seeded_summary]
    )
    self.assertEqual(host.current_summary, "Seeded summary")

    updated = host.upsert_summary("New summary text")
    self.assertEqual(updated.comment_id, "seeded-summary-99")
    self.assertEqual(updated.body, "New summary text")
    self.assertEqual(len(host.list_existing_comments()), 1)
    self.assertEqual(host.list_existing_comments()[0].body, "New summary text")

  def test_set_security_gate(self):
    self.assertIsNone(self.host.latest_security_gate)
    gate_pending = SecurityGate(state=SecurityGateState.PENDING)
    self.host.set_security_gate(gate_pending)
    self.assertEqual(self.host.latest_security_gate, gate_pending)

    gate_success = SecurityGate(
        state=SecurityGateState.SUCCESS, description="All checks passed"
    )
    self.host.set_security_gate(gate_success)
    self.assertEqual(self.host.latest_security_gate, gate_success)
    self.assertEqual(len(self.host.security_gates), 2)

  def test_emit_ci_outputs(self):
    self.host.emit_ci_outputs({"has_findings": "true", "count": "3"})
    self.assertEqual(
        self.host.emitted_outputs, {"has_findings": "true", "count": "3"}
    )
    self.host.emit_ci_outputs({"batches_json": '["1", "2"]'})
    self.assertEqual(
        self.host.emitted_outputs,
        {"has_findings": "true", "count": "3", "batches_json": '["1", "2"]'},
    )

  def test_add_reaction(self):
    self.host.add_reaction("comment-123", "eyes")
    self.assertEqual(len(self.host.reactions), 1)
    self.assertEqual(self.host.reactions[0], ("comment-123", "eyes"))


if __name__ == "__main__":
  unittest.main()
