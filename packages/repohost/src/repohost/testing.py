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

"""In-memory test double for RepoHost."""

from typing import Any, Mapping, Optional, Sequence

from repohost.base import RepoHost
from repohost.models import (
    ChangeContext,
    Comment,
    SecurityGate,
    SuggestedEdit,
)
from artifactstore.base import ArtifactStore
from artifactstore.testing import InMemoryArtifactStore


class InMemoryRepoHost(RepoHost):
  """In-memory RepoHost simulator for hermetic unit and integration testing."""

  def __init__(
      self,
      change_context: Optional[ChangeContext] = None,
      existing_comments: Optional[Sequence[Comment]] = None,
  ) -> None:
    self._context = change_context or ChangeContext(
        repo_id="default/repo",
        change_id="1",
        base_sha="0000000",
        head_sha="1111111",
        base_ref="main",
        head_ref="feature",
    )
    self._comments: list[Comment] = list(existing_comments or [])
    self._posted_suggestions: list[SuggestedEdit] = []
    self._replies: list[tuple[str, str]] = []
    self._summaries: list[str] = []
    self._security_gates: list[SecurityGate] = []
    self._emitted_outputs: dict[str, str] = {}
    self._reactions: list[tuple[str, str]] = []
    self._comment_counter = len(self._comments)

    # Detect pre-existing summary comment from seeded comments
    self._summary_comment: Optional[Comment] = None
    for comment in self._comments:
      if comment.kind in ("summary", "sticky"):
        self._summary_comment = comment
        if comment.body:
          self._summaries.append(comment.body)
        break

  def _comment_url(self, fragment: str) -> str:
    """Helper to generate in-memory comment URLs under 80 chars."""
    return (
        f"https://inmemory.repo/{self._context.repo_id}/"
        f"pull/{self._context.change_id}#{fragment}"
    )

  @property
  def posted_suggestions(self) -> Sequence[SuggestedEdit]:
    return tuple(self._posted_suggestions)

  @property
  def replies(self) -> Sequence[tuple[str, str]]:
    return tuple(self._replies)

  @property
  def summaries(self) -> Sequence[str]:
    return tuple(self._summaries)

  @property
  def current_summary(self) -> Optional[str]:
    return self._summaries[-1] if self._summaries else None

  @property
  def security_gates(self) -> Sequence[SecurityGate]:
    return tuple(self._security_gates)

  @property
  def latest_security_gate(self) -> Optional[SecurityGate]:
    return self._security_gates[-1] if self._security_gates else None

  @property
  def emitted_outputs(self) -> Mapping[str, str]:
    return dict(self._emitted_outputs)

  @property
  def reactions(self) -> Sequence[tuple[str, str]]:
    return tuple(self._reactions)

  def get_change_context(self) -> ChangeContext:
    return self._context

  def list_existing_comments(self) -> Sequence[Comment]:
    return tuple(self._comments)

  def post_suggestion(self, suggestion: SuggestedEdit) -> Comment:
    self._posted_suggestions.append(suggestion)
    self._comment_counter += 1
    kind = (
        "suggestion"
        if (
            suggestion.file_path
            and suggestion.line
            and suggestion.replacement is not None
        )
        else "patch"
    )
    comment = Comment(
        comment_id=f"inmem-sugg-{self._comment_counter}",
        url=self._comment_url(f"comment-{self._comment_counter}"),
        finding_id=suggestion.finding_id,
        kind=kind,
        body=suggestion.summary or suggestion.patch,
    )
    self._comments.append(comment)
    return comment

  def reply_to_comment(self, comment_id: str, body: str) -> Comment:
    self._replies.append((comment_id, body))
    self._comment_counter += 1
    comment = Comment(
        comment_id=f"inmem-reply-{self._comment_counter}",
        url=self._comment_url(f"comment-{self._comment_counter}"),
        finding_id=None,
        kind="reply",
        body=body,
    )
    self._comments.append(comment)
    return comment

  def upsert_summary(self, body: str) -> Comment:
    self._summaries.append(body)
    if self._summary_comment is None:
      self._comment_counter += 1
      self._summary_comment = Comment(
          comment_id=f"inmem-summary-{self._comment_counter}",
          url=self._comment_url("summary"),
          finding_id=None,
          kind="summary",
          body=body,
      )
      self._comments.append(self._summary_comment)
    else:
      self._summary_comment = Comment(
          comment_id=self._summary_comment.comment_id,
          url=self._summary_comment.url,
          finding_id=None,
          kind="summary",
          body=body,
      )
      for i, c in enumerate(self._comments):
        if c.comment_id == self._summary_comment.comment_id:
          self._comments[i] = self._summary_comment
          break
    return self._summary_comment

  def set_security_gate(self, gate: SecurityGate) -> None:
    self._security_gates.append(gate)

  def emit_ci_outputs(self, outputs: Mapping[str, str]) -> None:
    self._emitted_outputs.update(outputs)

  def add_reaction(self, comment_id: str, reaction: str) -> None:
    self._reactions.append((comment_id, reaction))

