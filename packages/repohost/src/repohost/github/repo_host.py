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

# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0001
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0003
# Governing: ADR-0008, ADR-0006,
# SPEC-GHA, SPEC-GHA-FIX

"""GitHub repository host implementation for CodeMender."""

import json
import logging
import os
import re
from typing import Any, Mapping, Optional, Sequence

import requests

from repohost.base import RepoHost
from repohost.github.client import (
    FINDING_TAG_RE,
    GITHUB_API_URL,
    STICKY_SUMMARY_MARKER,
    _terminate_open_fence,
    finding_tag,
    format_patch_comment,
    is_bot_comment,
    truncate_comment_body,
)
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

logger = logging.getLogger("codemender-github-repohost")

_OUTPUT_KEY_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")


class InvalidOutputError(RepoHostError, ValueError):
  """Raised when a CI output key or value is invalid."""


InvalidOutputKeyError = InvalidOutputError


class GitHubRepoHost(RepoHost):
  """GitHub repository hosting platform adapter for CodeMender.

  Runs on the outer CI host VM or container (where GITHUB_TOKEN resides).
  Implements PR context discovery, review comment management, commit status
  checks, and CI step outputs.
  """

  def __init__(
      self,
      *,
      token: Optional[str] = None,
      repo: Optional[str] = None,
      pr_number: Optional[int] = None,
      head_sha: Optional[str] = None,
      event: Optional[Mapping[str, Any]] = None,
      env: Optional[Mapping[str, str]] = None,
      api_url: str = GITHUB_API_URL,
      session: Optional[requests.Session] = None,
  ) -> None:
    self._env: Mapping[str, str] = (
        dict(os.environ) if env is None else dict(env)
    )
    self._token: str = (
        token if token is not None else self._env.get("GITHUB_TOKEN", "")
    ).strip()
    self._repo: Optional[str] = repo
    self._pr_number: Optional[int] = pr_number
    self._head_sha: Optional[str] = head_sha
    self._event: Optional[Mapping[str, Any]] = event
    self._api_url: str = api_url.rstrip("/")
    self._session: Optional[requests.Session] = session
    self._cached_context: Optional[ChangeContext] = None

  def _headers(self) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if self._token:
      headers["Authorization"] = f"Bearer {self._token}"
    return headers

  def _check_response(self, resp: requests.Response, context: str) -> None:
    if resp.status_code == 401:
      raise RepoHostAuthError(
          f"GitHub API authentication failed for {context}: {resp.text}"
      )
    is_rate_limit = resp.status_code == 429 or (
        resp.status_code == 403
        and (
            resp.headers.get("x-ratelimit-remaining") == "0"
            or any(
                m in resp.text.lower()
                for m in ("rate limit", "abuse detection")
            )
        )
    )
    if is_rate_limit:
      raise RepoHostRateLimitError(
          f"GitHub API rate limit exceeded for {context}: {resp.text}"
      )
    if resp.status_code >= 400:
      raise RepoHostError(
          f"GitHub API error {resp.status_code} for {context}: {resp.text}"
      )

  def _get_pages(self, url: str, context: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    curr_url: Optional[str] = url
    headers = self._headers()
    session = self._session or requests
    while curr_url:
      resp = session.get(curr_url, headers=headers, timeout=15)
      self._check_response(resp, context)
      data = resp.json()
      if isinstance(data, list):
        results.extend(data)
      curr_url = resp.links.get("next", {}).get("url")
    return results

  def _ensure_pr_context(self) -> None:
    if self._cached_context is None:
      if self._event is not None or self._env.get("GITHUB_EVENT_PATH"):
        self.get_change_context()
      else:
        if self._repo is None:
          self._repo = self._env.get("GITHUB_REPOSITORY") or None
        if (
            self._repo is None
            or self._pr_number is None
            or self._head_sha is None
        ):
          raise RepoHostError(
              "Incomplete PR context: repo, pr_number, and head_sha are"
              " required"
          )

  def _require_token(self, operation: str) -> None:
    if not self._token:
      raise RepoHostAuthError(
          f"Missing GitHub token (GITHUB_TOKEN) required for {operation}"
      )

  def get_change_context(self) -> ChangeContext:
    """Discovers the pull request change context from event and environment."""
    if self._cached_context is not None:
      return self._cached_context

    event_data: Mapping[str, Any]
    if self._event is not None:
      event_data = self._event
    else:
      event_path = self._env.get("GITHUB_EVENT_PATH", "")
      if not event_path:
        raise RepoHostError(
            "Missing GITHUB_EVENT_PATH and no event payload provided"
        )
      if not os.path.exists(event_path):
        raise RepoHostError(f"GitHub event file not found: {event_path}")
      try:
        with open(event_path, "r", encoding="utf-8") as f:
          event_data = json.load(f)
      except Exception as e:
        raise RepoHostError(f"Failed to parse GitHub event JSON: {e}") from e

    pr = event_data.get("pull_request")
    if not isinstance(pr, dict):
      raise RepoHostError("Event payload does not contain pull_request data")

    pr_num = self._pr_number or pr.get("number")
    if not pr_num:
      raise RepoHostError("Could not determine pull request number from event")

    base_obj = pr.get("base") or {}
    head_obj = pr.get("head") or {}

    base_repo = (base_obj.get("repo") or {}).get("full_name")
    repo_id = (
        self._repo
        or base_repo
        or (event_data.get("repository") or {}).get("full_name")
        or self._env.get("GITHUB_REPOSITORY")
    )
    if not repo_id:
      raise RepoHostError(
          "Could not determine repository ID from event or environment"
      )

    base_sha = str(base_obj.get("sha") or base_obj.get("ref") or "").strip()
    if not base_sha:
      raise RepoHostError(
          "Could not determine base SHA from pull request event"
      )

    head_sha = (
        str(head_obj.get("sha") or "").strip()
        or self._head_sha
        or self._env.get("GITHUB_SHA", "")
    )
    if not head_sha:
      raise RepoHostError(
          "Could not determine head SHA from pull request event"
      )

    base_ref = str(base_obj.get("ref") or "").strip()
    head_ref = str(head_obj.get("ref") or "").strip()

    head_repo = (head_obj.get("repo") or {}).get("full_name")
    is_fork = bool(base_repo) and head_repo != base_repo

    context = ChangeContext(
        repo_id=repo_id,
        change_id=str(pr_num),
        base_sha=base_sha,
        head_sha=head_sha,
        base_ref=base_ref,
        head_ref=head_ref,
        is_fork=is_fork,
    )
    self._cached_context = context
    self._repo = repo_id
    self._pr_number = int(pr_num)
    self._head_sha = head_sha
    return context

  def list_existing_comments(self) -> Sequence[Comment]:
    """Lists existing CodeMender comments on the pull request."""
    self._ensure_pr_context()
    self._require_token("list_existing_comments")

    comments: list[Comment] = []
    seen_comment_ids: set[str] = set()

    # 1. Fetch review comments (inline suggestions and diff patches)
    review_url = (
        f"{self._api_url}/repos/{self._repo}/pulls/{self._pr_number}/comments"
        "?per_page=100"
    )
    review_data = self._get_pages(review_url, "review comments")
    for item in review_data:
      if not isinstance(item, dict):
        continue
      if not is_bot_comment(item):
        continue
      if item.get("in_reply_to_id"):
        continue
      body = str(item.get("body") or "")
      tags = FINDING_TAG_RE.findall(body)
      if not tags:
        continue
      finding_id = tags[0]
      cid = str(item.get("id") or "")
      outdated = (
          False
          if item.get("subject_type") == "file"
          else (item.get("line") is None and item.get("position") is None)
      )
      kind = "suggestion" if "```suggestion" in body else "patch"
      comments.append(
          Comment(
              comment_id=cid,
              url=str(item.get("html_url") or ""),
              finding_id=finding_id,
              outdated=outdated,
              kind=kind,
              body=body,
          )
      )
      seen_comment_ids.add(cid)

    # 2. Fetch issue comments (sticky summary or issue-level fallback patches)
    issue_url = (
        f"{self._api_url}/repos/{self._repo}/issues/{self._pr_number}/comments"
        "?per_page=100"
    )
    issue_data = self._get_pages(issue_url, "issue comments")
    for item in issue_data:
      if not isinstance(item, dict):
        continue
      if not is_bot_comment(item):
        continue
      cid = str(item.get("id") or "")
      if cid in seen_comment_ids:
        continue
      body = str(item.get("body") or "")
      if STICKY_SUMMARY_MARKER in body:
        comments.append(
            Comment(
                comment_id=cid,
                url=str(item.get("html_url") or ""),
                finding_id=None,
                outdated=False,
                kind="summary",
                body=body,
            )
        )
        seen_comment_ids.add(cid)
      else:
        tags = FINDING_TAG_RE.findall(body)
        if tags:
          comments.append(
              Comment(
                  comment_id=cid,
                  url=str(item.get("html_url") or ""),
                  finding_id=tags[0],
                  outdated=False,
                  kind="patch",
                  body=body,
              )
          )
          seen_comment_ids.add(cid)

    return comments

  def post_suggestion(self, suggestion: SuggestedEdit) -> Comment:
    """Posts a suggested edit on the pull request.

    If file_path and line are specified, attempts to post an inline code review
    suggestion on the pull request diff hunk. If posting inline fails with
    HTTP 422 (e.g. line is outside the diff hunks), or if file_path or line are
    omitted, falls back to a patch comment (file-level review comment or
    PR issue comment).
    """
    self._ensure_pr_context()
    self._require_token("post_suggestion")

    session = self._session or requests

    # 1. Attempt inline review suggestion if file_path and line are provided.
    if (
        suggestion.file_path
        and suggestion.line is not None
        and suggestion.line > 0
    ):
      tag = finding_tag(suggestion.finding_id)
      has_fence = suggestion.replacement is not None and (
          "```" in suggestion.replacement or "~~~" in suggestion.replacement
      )
      if suggestion.replacement is not None and not has_fence:
        clean_replacement = suggestion.replacement.rstrip("\n")
        block = f"```suggestion\n{clean_replacement}\n```"
        if suggestion.summary:
          summary = _terminate_open_fence(suggestion.summary)
          inline_body = f"{tag}\n{summary}\n\n{block}"
        else:
          inline_body = f"{tag}\n{block}"
      elif "```suggestion" in suggestion.patch and not has_fence:
        inline_body = (
            suggestion.patch
            if tag in suggestion.patch
            else f"{tag}\n{suggestion.patch}"
        )
      else:
        header = f"Finding `{suggestion.finding_id}`"
        reason = suggestion.summary or "CodeMender generated a suggestion."
        inline_body = format_patch_comment(
            tag=tag,
            header=header,
            reason=reason,
            patch=suggestion.patch,
        )

      payload: dict[str, Any] = {
          "body": truncate_comment_body(inline_body),
          "commit_id": self._head_sha,
          "path": suggestion.file_path,
          "line": suggestion.line,
          "side": suggestion.side or "RIGHT",
      }
      if (
          suggestion.start_line is not None
          and suggestion.start_line < suggestion.line
      ):
        payload["start_line"] = suggestion.start_line
        payload["start_side"] = suggestion.side or "RIGHT"

      url = (
          f"{self._api_url}/repos/{self._repo}/pulls/{self._pr_number}"
          "/comments"
      )
      resp = session.post(
          url, headers=self._headers(), json=payload, timeout=20
      )
      if resp.status_code == 201:
        data = resp.json()
        kind = "suggestion" if "```suggestion" in inline_body else "patch"
        return Comment(
            comment_id=str(data.get("id") or ""),
            url=str(data.get("html_url") or ""),
            finding_id=suggestion.finding_id,
            outdated=False,
            kind=kind,
            body=str(data.get("body") or inline_body),
        )
      if resp.status_code != 422:
        self._check_response(
            resp, f"inline suggestion on {suggestion.file_path}"
        )

    # 2. Render fallback patch comment.
    tag = finding_tag(suggestion.finding_id)
    header = f"Finding `{suggestion.finding_id}`"
    reason = suggestion.summary or "CodeMender generated a fallback patch."
    patch_body = format_patch_comment(
        tag=tag,
        header=header,
        reason=reason,
        patch=suggestion.patch,
    )

    if suggestion.file_path:
      url = (
          f"{self._api_url}/repos/{self._repo}/pulls/{self._pr_number}"
          "/comments"
      )
      file_payload = {
          "body": truncate_comment_body(patch_body),
          "commit_id": self._head_sha,
          "path": suggestion.file_path,
          "subject_type": "file",
      }
      resp = session.post(
          url, headers=self._headers(), json=file_payload, timeout=20
      )
      if resp.status_code == 201:
        data = resp.json()
        return Comment(
            comment_id=str(data.get("id") or ""),
            url=str(data.get("html_url") or ""),
            finding_id=suggestion.finding_id,
            outdated=False,
            kind="patch",
            body=str(data.get("body") or patch_body),
        )
      if resp.status_code != 422:
        self._check_response(
            resp, f"file-level patch on {suggestion.file_path}"
        )

    # 3. Fallback to workflow run artifacts link when review comments cannot
    # be posted (e.g. line or file is entirely outside the PR diff).
    server_url = self._env.get(
        "GITHUB_SERVER_URL", "https://github.com"
    ).rstrip("/")
    run_id = self._env.get("GITHUB_RUN_ID", "")
    artifacts_url = (
        f"{server_url}/{self._repo}/actions/runs/{run_id}#artifacts"
        if run_id
        else f"{server_url}/{self._repo}/pull/{self._pr_number}"
    )
    logger.warning(
        "Could not post review comment on %s:%s for finding %s; linking to"
        " workflow artifacts instead: %s",
        suggestion.file_path,
        suggestion.line,
        suggestion.finding_id,
        artifacts_url,
    )
    return Comment(
        comment_id="",
        url=artifacts_url,
        finding_id=suggestion.finding_id,
        outdated=False,
        kind="patch",
        body=patch_body,
    )

  def reply_to_comment(self, comment_id: str, body: str) -> Comment:
    """Replies to an existing review thread or comment on the change."""
    self._ensure_pr_context()
    self._require_token("reply_to_comment")

    url = (
        f"{self._api_url}/repos/{self._repo}/pulls/{self._pr_number}/comments"
        f"/{comment_id}/replies"
    )
    session = self._session or requests
    payload = {"body": truncate_comment_body(body)}
    resp = session.post(url, headers=self._headers(), json=payload, timeout=20)
    self._check_response(resp, f"reply to comment {comment_id}")
    data = resp.json()
    return Comment(
        comment_id=str(data.get("id") or ""),
        url=str(data.get("html_url") or ""),
        finding_id=None,
        outdated=False,
        kind="reply",
        body=str(data.get("body") or body),
    )

  def upsert_summary(self, body: str) -> Comment:
    """Creates or updates the summary comment on the change."""
    self._ensure_pr_context()
    self._require_token("upsert_summary")

    marked_body = (
        body
        if STICKY_SUMMARY_MARKER in body
        else f"{STICKY_SUMMARY_MARKER}\n{body}"
    )
    truncated_body = truncate_comment_body(
        marked_body, reserved=len(STICKY_SUMMARY_MARKER) + 1
    )

    existing_id: Optional[str] = None
    comments_url = (
        f"{self._api_url}/repos/{self._repo}/issues/{self._pr_number}/comments"
        "?per_page=100"
    )
    issue_comments = self._get_pages(
        comments_url, "issue comments for sticky summary"
    )
    for item in issue_comments:
      if (
          isinstance(item, dict)
          and is_bot_comment(item)
          and STICKY_SUMMARY_MARKER in str(item.get("body") or "")
      ):
        existing_id = str(item.get("id") or "")
        break

    session = self._session or requests
    if existing_id:
      patch_url = (
          f"{self._api_url}/repos/{self._repo}/issues/comments/{existing_id}"
      )
      resp = session.patch(
          patch_url,
          headers=self._headers(),
          json={"body": truncated_body},
          timeout=20,
      )
      self._check_response(
          resp, f"updating sticky summary comment {existing_id}"
      )
    else:
      post_url = (
          f"{self._api_url}/repos/{self._repo}"
          f"/issues/{self._pr_number}/comments"
      )
      resp = session.post(
          post_url,
          headers=self._headers(),
          json={"body": truncated_body},
          timeout=20,
      )
      self._check_response(resp, "creating sticky summary comment")

    data = resp.json()
    return Comment(
        comment_id=str(data.get("id") or ""),
        url=str(data.get("html_url") or ""),
        finding_id=None,
        outdated=False,
        kind="summary",
        body=str(data.get("body") or truncated_body),
    )

  def set_security_gate(self, gate: SecurityGate) -> None:
    """Publishes a security quality gate evaluation on the head commit."""
    self._ensure_pr_context()
    self._require_token("set_security_gate")

    clean_description = (
        (gate.description[:137] + "...")
        if len(gate.description) > 140
        else gate.description
    )
    payload: dict[str, Any] = {
        "state": gate.state.lower(),
        "context": gate.context,
        "description": clean_description,
    }
    if gate.target_url:
      payload["target_url"] = gate.target_url

    url = f"{self._api_url}/repos/{self._repo}/statuses/{self._head_sha}"
    session = self._session or requests
    resp = session.post(url, headers=self._headers(), json=payload, timeout=20)
    self._check_response(resp, f"security gate on {self._head_sha}")

  def emit_ci_outputs(self, outputs: Mapping[str, str]) -> None:
    """Emits key-value outputs for subsequent CI steps or jobs."""
    for key in outputs:
      if not isinstance(key, str) or not _OUTPUT_KEY_RE.match(key):
        raise InvalidOutputError(
            f"Invalid CI output key: {key!r}. Output keys must be non-empty "
            "and contain only alphanumeric characters, underscores, "
            "hyphens, or dots."
        )

    output_path = self._env.get("GITHUB_OUTPUT", "")
    if not output_path:
      logger.debug("GITHUB_OUTPUT not set; skipping CI output emission")
      return

    try:
      with open(output_path, "a", encoding="utf-8") as f:
        for key, val in outputs.items():
          val_str = str(val) if val is not None else ""
          if "\n" in val_str or "\r" in val_str:
            delimiter = f"_EOF_{os.urandom(8).hex()}_"
            while delimiter in val_str:
              delimiter = f"_EOF_{os.urandom(8).hex()}_"
            f.write(f"{key}<<{delimiter}\n{val_str}\n{delimiter}\n")
          else:
            f.write(f"{key}={val_str}\n")
    except OSError as e:
      raise RepoHostError(f"Failed to write to GITHUB_OUTPUT: {e}") from e

  def add_reaction(self, comment_id: str, reaction: str) -> None:
    """Adds a reaction to an existing comment on the change."""
    self._ensure_pr_context()
    self._require_token("add_reaction")

    url = (
        f"{self._api_url}/repos/{self._repo}/pulls/comments/{comment_id}"
        "/reactions"
    )
    session = self._session or requests
    payload = {"content": reaction}
    resp = session.post(url, headers=self._headers(), json=payload, timeout=20)
    if resp.status_code == 404:
      issue_url = (
          f"{self._api_url}/repos/{self._repo}/issues/comments/{comment_id}"
          "/reactions"
      )
      resp = session.post(
          issue_url, headers=self._headers(), json=payload, timeout=20
      )
    self._check_response(
        resp, f"reaction {reaction!r} on comment {comment_id}"
    )
