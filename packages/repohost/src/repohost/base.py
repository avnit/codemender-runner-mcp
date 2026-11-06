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

"""Abstract Base Class for repository hosting platform adapters."""

import abc
from typing import Mapping, Sequence

from repohost.models import (
    ChangeContext,
    Comment,
    SecurityGate,
    SuggestedEdit,
)


class RepoHost(abc.ABC):
  """Abstract interface for repository hosting platform interactions.

  RepoHost runs exclusively on the outer CI host VM or container where platform
  tokens ($GITHUB_TOKEN, $CI_JOB_TOKEN) reside. It abstracts PR/MR diff context
  discovery, review comments and suggestions, security gate status checks,
  summary comments, and CI step outputs.
  """

  @abc.abstractmethod
  def get_change_context(self) -> ChangeContext:
    """Discovers the pull request or merge request change context.

    Returns:
      ChangeContext containing repo identifier, change identifier, base and
      head commit SHAs, and ref names.

    Raises:
      RepoHostError: If the change context cannot be discovered or credentials
        are invalid.
    """

  @abc.abstractmethod
  def list_existing_comments(self) -> Sequence[Comment]:
    """Lists existing CodeMender comments on the pull/merge request.

    Returns:
      Sequence of Comment objects present on the change.

    Raises:
      RepoHostError: If comments cannot be fetched from the host platform API.
    """

  @abc.abstractmethod
  def post_suggestion(self, suggestion: SuggestedEdit) -> Comment:
    """Posts a code remediation suggestion on the pull or merge request.

    Concrete implementations determine the appropriate posting mechanism
    (e.g., inline diff suggestion comment if supported and within diff hunks,
    or fallback patch comment otherwise).

    Args:
      suggestion: SuggestedEdit specifying target file, line, and diff patch.

    Returns:
      Comment representing the created suggestion comment.

    Raises:
      RepoHostError: If the suggestion comment cannot be posted.
    """

  @abc.abstractmethod
  def reply_to_comment(self, comment_id: str, body: str) -> Comment:
    """Replies to an existing review thread or comment on the change.

    Args:
      comment_id: Identifier of the parent comment/thread to reply to.
      body: Comment body text to post as the reply.

    Returns:
      Comment representing the created reply.

    Raises:
      RepoHostError: If the reply cannot be posted.
    """

  @abc.abstractmethod
  def upsert_summary(self, body: str) -> Comment:
    """Creates or updates the summary comment on the change.

    Args:
      body: Full markdown body of the summary comment.

    Returns:
      Comment representing the posted or updated summary comment.

    Raises:
      RepoHostError: If the summary comment cannot be created or updated.
    """

  @abc.abstractmethod
  def set_security_gate(self, gate: SecurityGate) -> None:
    """Publishes a security quality gate evaluation on the head commit.

    Posts the pass/fail Security Gate status to the repository host's commit
    status API (e.g. GitHub Commit Statuses or GitLab Commit Statuses) for
    branch protection rule evaluation per ADR-0006.

    Args:
      gate: SecurityGate specifying state, context, description, and URL.

    Raises:
      RepoHostError: If the security gate status cannot be published.
    """

  @abc.abstractmethod
  def emit_ci_outputs(self, outputs: Mapping[str, str]) -> None:
    """Emits key-value outputs for subsequent CI steps or jobs.

    In GitHub Actions, this writes to $GITHUB_OUTPUT. In GitLab CI, this writes
    to .codemender-cache/ci-outputs.json or dotenv artifacts.

    Args:
      outputs: Mapping of output names to string values.
    """

  @abc.abstractmethod
  def add_reaction(self, comment_id: str, reaction: str) -> None:
    """Adds a reaction to an existing comment on the change.

    Args:
      comment_id: Identifier of the comment to react to.
      reaction: Platform-independent reaction identifier (e.g. "eyes").

    Raises:
      RepoHostError: If the reaction cannot be posted.
    """
