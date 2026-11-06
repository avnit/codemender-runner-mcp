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

"""Domain models and exceptions for repository host abstractions."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Optional


class SecurityGateState(StrEnum):
  """State of the CodeMender security quality gate."""

  PENDING = "PENDING"
  SUCCESS = "SUCCESS"
  FAILURE = "FAILURE"
  ERROR = "ERROR"


@dataclass(frozen=True)
class ChangeContext:
  """Context of the pull request or merge request being scanned or remediated.

  Attributes:
    repo_id: Repository identifier (e.g. 'owner/repo' or project path/ID).
    change_id: Pull request number or merge request internal ID (IID).
    base_sha: Git commit SHA of the target base branch.
    head_sha: Git commit SHA of the source head branch / PR commit.
    base_ref: Target branch name (e.g. 'main').
    head_ref: Source branch name (e.g. 'feature-branch').
    is_fork: Whether the head branch originates from a fork repository.
  """

  repo_id: str
  change_id: str
  base_sha: str
  head_sha: str
  base_ref: str
  head_ref: str
  is_fork: bool = False


@dataclass(frozen=True)
class SecurityGate:
  """Security quality gate evaluation published on the head commit.

  Represents the pass/fail security status evaluation published to the host
  platform's commit status API (e.g. GitHub Commit Statuses or GitLab Commit
  Statuses) to enforce PR/MR branch protection rules per ADR-0006.

  Attributes:
    state: Security gate evaluation state (pending, success, failure, error).
    context: Check name identifier (defaults to 'CodeMender / Security Gate').
    description: Human-readable summary of gate outcome.
    target_url: Optional link to CI run logs or findings report.
  """

  state: SecurityGateState
  context: str = "CodeMender / Security Gate"
  description: str = ""
  target_url: str = ""


@dataclass(frozen=True)
class SuggestedEdit:
  """A proposed code remediation or patch for a security finding.

  Unifies inline diff review suggestions and fallback patch comments into a
  single model, allowing repository host implementations to determine the
  appropriate rendering (inline review comment or fallback patch comment).

  Attributes:
    finding_id: Deterministic finding identifier associated with this edit.
    patch: Unified diff patch text.
    file_path: Optional repository-relative target file path.
    line: Optional target end line number in the diff (new file line).
    start_line: Optional target starting line number for multi-line edits.
    side: Diff side ('LEFT' for deletions, 'RIGHT' for additions/modifications).
    summary: Optional explanation or reason for the suggested edit.
    replacement: Optional replacement text (without code fence wrapper).
  """

  finding_id: str
  patch: str
  file_path: Optional[str] = None
  line: Optional[int] = None
  start_line: Optional[int] = None
  side: str = "RIGHT"
  summary: str = ""
  replacement: Optional[str] = None


@dataclass(frozen=True)
class Comment:
  """A comment on a pull request or merge request.

  Used to represent existing comments retrieved for deduplication, as well as
  new comments posted by CodeMender (suggestions, patches, replies, summaries).

  Attributes:
    comment_id: Unique identifier of the comment on the host platform.
    url: Web URL linking directly to the comment on the repository host.
    finding_id: Optional finding identifier if tagged in comment body.
    outdated: Whether the comment is considered outdated or resolved.
    kind: Comment classification ('suggestion', 'patch', 'reply', 'summary').
    body: Raw markdown body of the comment.
  """

  comment_id: str
  url: str
  finding_id: Optional[str] = None
  outdated: bool = False
  kind: str = "suggestion"
  body: str = ""


class RepoHostError(Exception):
  """Base exception for repository host operations."""


class RepoHostAuthError(RepoHostError):
  """Authentication or credential failure when contacting the repo host."""


class RepoHostRateLimitError(RepoHostError):
  """API rate limit exceeded on the repository host."""
