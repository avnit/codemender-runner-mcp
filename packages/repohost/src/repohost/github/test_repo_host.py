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

"""Unit tests for repohost.github.repo_host (GitHubRepoHost)."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from repohost.base import RepoHost
from repohost.github.repo_host import GitHubRepoHost
from repohost.models import (
    ChangeContext,
    RepoHostAuthError,
    RepoHostError,
    RepoHostRateLimitError,
    SecurityGate,
    SecurityGateState,
    SuggestedEdit,
)


def _mock_response(
    json_body, status_code=200, links=None, text="", headers=None
):
  resp = MagicMock()
  resp.status_code = status_code
  resp.json.return_value = json_body
  resp.links = links or {}
  resp.headers = headers or {}
  resp.text = text or json.dumps(json_body) if json_body is not None else text
  return resp


class TestGitHubRepoHost(unittest.TestCase):
  """Comprehensive test suite for GitHubRepoHost implementation."""

  def setUp(self):
    self.temp_dir = tempfile.TemporaryDirectory()
    self.temp_path = Path(self.temp_dir.name)
    self.default_event = {
        "pull_request": {
            "number": 42,
            "base": {
                "sha": "base1234567890",
                "ref": "main",
                "repo": {"full_name": "owner/repo"},
            },
            "head": {
                "sha": "headabcdef1234",
                "ref": "feat-branch",
                "repo": {"full_name": "owner/repo"},
            },
        }
    }
    self.mock_session = MagicMock()

  def tearDown(self):
    self.temp_dir.cleanup()

  def test_implements_repo_host_interface(self):
    host = GitHubRepoHost(token="dummy-token")
    self.assertIsInstance(host, RepoHost)

  def test_get_change_context_from_injected_event(self):
    host = GitHubRepoHost(
        event=self.default_event,
        token="dummy-token",
    )
    context = host.get_change_context()
    self.assertEqual(
        context,
        ChangeContext(
            repo_id="owner/repo",
            change_id="42",
            base_sha="base1234567890",
            head_sha="headabcdef1234",
            base_ref="main",
            head_ref="feat-branch",
            is_fork=False,
        ),
    )
    # Verify cached context returns the same object without re-parsing
    self.assertIs(host.get_change_context(), context)

  def test_get_change_context_detects_fork_pr(self):
    fork_event = {
        "pull_request": {
            "number": 99,
            "base": {
                "sha": "base111",
                "ref": "main",
                "repo": {"full_name": "upstream/repo"},
            },
            "head": {
                "sha": "head222",
                "ref": "contrib-branch",
                "repo": {"full_name": "contributor/repo"},
            },
        }
    }
    host = GitHubRepoHost(event=fork_event, token="dummy-token")
    context = host.get_change_context()
    self.assertTrue(context.is_fork)
    self.assertEqual(context.repo_id, "upstream/repo")
    self.assertEqual(context.change_id, "99")

  def test_get_change_context_detects_deleted_fork_repo(self):
    deleted_fork_event = {
        "pull_request": {
            "number": 101,
            "base": {
                "sha": "base111",
                "ref": "main",
                "repo": {"full_name": "upstream/repo"},
            },
            "head": {
                "sha": "head222",
                "ref": "contrib-branch",
                "repo": None,
            },
        }
    }
    host = GitHubRepoHost(event=deleted_fork_event, token="dummy-token")
    context = host.get_change_context()
    self.assertTrue(context.is_fork)
    self.assertEqual(context.repo_id, "upstream/repo")
    self.assertEqual(context.change_id, "101")

  def test_get_change_context_from_event_file(self):
    event_file = self.temp_path / "event.json"
    event_file.write_text(json.dumps(self.default_event), encoding="utf-8")
    env = {
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_TOKEN": "token-from-env",
    }
    host = GitHubRepoHost(env=env)
    context = host.get_change_context()
    self.assertEqual(context.change_id, "42")
    self.assertEqual(context.repo_id, "owner/repo")

  def test_get_change_context_prefers_event_base_repo_over_ambient_env(self):
    env = {"GITHUB_REPOSITORY": "host/ambient-repo"}
    host = GitHubRepoHost(
        event=self.default_event,
        env=env,
        token="dummy-token",
    )
    context = host.get_change_context()
    self.assertEqual(context.repo_id, "owner/repo")

  def test_get_change_context_falls_back_to_ambient_env_when_repo_not_in_event(
      self,
  ):
    event_without_repo = {
        "pull_request": {
            "number": 42,
            "base": {"sha": "base123", "ref": "main"},
            "head": {"sha": "head123", "ref": "feat"},
        }
    }
    env = {"GITHUB_REPOSITORY": "fallback/repo"}
    host = GitHubRepoHost(
        event=event_without_repo,
        env=env,
        token="dummy-token",
    )
    context = host.get_change_context()
    self.assertEqual(context.repo_id, "fallback/repo")

  def test_ensure_pr_context_without_event_falls_back_to_ambient_env(self):
    env = {"GITHUB_REPOSITORY": "ambient/repo"}
    host = GitHubRepoHost(
        env=env,
        token="dummy-token",
        pr_number=77,
        head_sha="head123",
    )
    host._ensure_pr_context()
    self.assertEqual(host._repo, "ambient/repo")

  def test_get_change_context_missing_event_path_raises_error(self):
    host = GitHubRepoHost(env={}, token="dummy-token")
    with self.assertRaises(RepoHostError) as ctx:
      host.get_change_context()
    self.assertIn("Missing GITHUB_EVENT_PATH", str(ctx.exception))

  def test_get_change_context_corrupt_json_raises_error(self):
    event_file = self.temp_path / "corrupt.json"
    event_file.write_text("invalid json contents", encoding="utf-8")
    host = GitHubRepoHost(
        env={"GITHUB_EVENT_PATH": str(event_file)},
        token="dummy-token",
    )
    with self.assertRaises(RepoHostError) as ctx:
      host.get_change_context()
    self.assertIn("Failed to parse GitHub event JSON", str(ctx.exception))

  def test_get_change_context_missing_pull_request_raises_error(self):
    event_file = self.temp_path / "no_pr.json"
    event_file.write_text(json.dumps({"zen": "keep it simple"}), "utf-8")
    host = GitHubRepoHost(
        env={"GITHUB_EVENT_PATH": str(event_file)},
        token="dummy-token",
    )
    with self.assertRaises(RepoHostError) as ctx:
      host.get_change_context()
    self.assertIn("does not contain pull_request data", str(ctx.exception))

  def test_list_existing_comments_missing_token_raises_auth_error(self):
    host = GitHubRepoHost(event=self.default_event, token="")
    with self.assertRaises(RepoHostAuthError) as ctx:
      host.list_existing_comments()
    self.assertIn("Missing GitHub token", str(ctx.exception))

  def test_list_existing_comments_discovers_suggestions_and_sticky(self):
    review_comments = [
        {
            "id": 1001,
            "html_url": "https://github.com/owner/repo/pull/42#r1001",
            "body": (
                "<!-- codemender-finding:finding_1 -->\n"
                "```suggestion\nfix\n```"
            ),
            "line": 15,
            "position": 3,
            "user": {"type": "Bot"},
        },
        {
            "id": 1002,
            "html_url": "https://github.com/owner/repo/pull/42#r1002",
            "body": "<!-- codemender-finding:finding_2 -->\nFallback diff",
            "line": None,
            "position": None,
            "user": {"type": "Bot"},
        },
    ]
    issue_comments = [
        {
            "id": 2001,
            "html_url": (
                "https://github.com/owner/repo/issues/42#issuecomment-2001"
            ),
            "body": "<!-- codemender-summary -->\n# Summary\nAll clean.",
            "user": {"type": "Bot"},
        }
    ]

    self.mock_session.get.side_effect = [
        _mock_response(review_comments),
        _mock_response(issue_comments),
    ]

    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
    )
    comments = host.list_existing_comments()
    self.assertEqual(len(comments), 3)

    # First comment: inline suggestion
    c1 = comments[0]
    self.assertEqual(c1.comment_id, "1001")
    self.assertEqual(c1.finding_id, "finding_1")
    self.assertEqual(c1.kind, "suggestion")
    self.assertFalse(c1.outdated)

    # Second comment: outdated patch review comment
    c2 = comments[1]
    self.assertEqual(c2.comment_id, "1002")
    self.assertEqual(c2.finding_id, "finding_2")
    self.assertEqual(c2.kind, "patch")
    self.assertTrue(c2.outdated)

    # Third comment: sticky summary
    c3 = comments[2]
    self.assertEqual(c3.comment_id, "2001")
    self.assertIsNone(c3.finding_id)
    self.assertEqual(c3.kind, "summary")
    self.assertFalse(c3.outdated)

  def test_list_existing_comments_handles_auth_error_401(self):
    self.mock_session.get.return_value = _mock_response(
        None, status_code=401, text="Bad credentials"
    )
    host = GitHubRepoHost(
        event=self.default_event,
        token="bad-token",
        session=self.mock_session,
    )
    with self.assertRaises(RepoHostAuthError):
      host.list_existing_comments()

  def test_list_existing_comments_handles_rate_limit_403(self):
    self.mock_session.get.return_value = _mock_response(
        None, status_code=403, text="API rate limit exceeded for user"
    )
    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
    )
    with self.assertRaises(RepoHostRateLimitError):
      host.list_existing_comments()

  def test_list_existing_comments_handles_rate_limit_429(self):
    self.mock_session.get.return_value = _mock_response(
        None, status_code=429, text="Too Many Requests"
    )
    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
    )
    with self.assertRaises(RepoHostRateLimitError):
      host.list_existing_comments()

  def test_list_existing_comments_handles_rate_limit_header(self):
    self.mock_session.get.return_value = _mock_response(
        None,
        status_code=403,
        text="Resource blocked",
        headers={"x-ratelimit-remaining": "0"},
    )
    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
    )
    with self.assertRaises(RepoHostRateLimitError):
      host.list_existing_comments()

  def test_list_existing_comments_ignores_non_bot_comments_with_tags(self):
    review_comments = [
        {
            "id": 8888,
            "html_url": "https://github.com/owner/repo/pull/42#r8888",
            "body": "<!-- codemender-finding:f_spoofed -->\nSpoofed tag",
            "line": 10,
            "position": 1,
            "user": {"type": "User", "login": "external-author"},
        },
    ]
    self.mock_session.get.side_effect = [
        _mock_response(review_comments),
        _mock_response([]),
    ]
    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
    )
    comments = host.list_existing_comments()
    self.assertEqual(comments, [])

  def test_list_existing_comments_ignores_unrelated_bot_comments(self):
    review_comments = [
        {
            "id": 9999,
            "html_url": "https://github.com/owner/repo/pull/42#r9999",
            "body": "Dependabot comment with no codemender finding tag",
            "line": 10,
            "position": 1,
            "user": {"type": "Bot", "login": "dependabot[bot]"},
        },
    ]
    self.mock_session.get.side_effect = [
        _mock_response(review_comments),
        _mock_response([]),
    ]
    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
    )
    comments = host.list_existing_comments()
    self.assertEqual(comments, [])

  def test_list_existing_comments_ignores_thread_replies(self):
    review_comments = [
        {
            "id": 1001,
            "html_url": "https://github.com/owner/repo/pull/42#r1001",
            "body": "<!-- codemender-finding:f1 -->\nroot comment",
            "line": 10,
            "position": 1,
            "user": {"type": "Bot"},
        },
        {
            "id": 1002,
            "in_reply_to_id": 1001,
            "html_url": "https://github.com/owner/repo/pull/42#r1002",
            "body": "<!-- codemender-finding:f1 -->\nreply in thread",
            "line": 10,
            "position": 1,
            "user": {"type": "Bot"},
        },
    ]
    self.mock_session.get.side_effect = [
        _mock_response(review_comments),
        _mock_response([]),
    ]
    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
    )
    comments = host.list_existing_comments()
    self.assertEqual(len(comments), 1)
    self.assertEqual(comments[0].comment_id, "1001")

  def test_post_suggestion_inline_success(self):
    self.mock_session.post.return_value = _mock_response(
        {
            "id": 5001,
            "html_url": "https://github.com/owner/repo/pull/42#r5001",
            "body": (
                "<!-- codemender-finding:vuln1 -->\n"
                "```suggestion\nnew code\n```"
            ),
        },
        status_code=201,
    )
    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
    )
    suggestion = SuggestedEdit(
        file_path="src/main.py",
        line=42,
        replacement="new code\n",
        finding_id="vuln1",
        start_line=40,
        side="RIGHT",
        patch="",
    )
    result = host.post_suggestion(suggestion)
    self.assertEqual(result.comment_id, "5001")
    self.assertEqual(result.kind, "suggestion")
    self.assertEqual(result.finding_id, "vuln1")
    self.assertEqual(
        result.url, "https://github.com/owner/repo/pull/42#r5001"
    )

    # Verify request payload
    called_args, called_kwargs = self.mock_session.post.call_args
    self.assertIn("owner/repo/pulls/42/comments", called_args[0])
    payload = called_kwargs["json"]
    self.assertEqual(payload["path"], "src/main.py")
    self.assertEqual(payload["line"], 42)
    self.assertEqual(payload["start_line"], 40)
    self.assertEqual(payload["commit_id"], "headabcdef1234")
    self.assertIn("<!-- codemender-finding:vuln1 -->", payload["body"])

  def test_post_suggestion_inline_with_code_fences_falls_back_to_patch(self):
    self.mock_session.post.return_value = _mock_response(
        {
            "id": 5002,
            "html_url": "https://github.com/owner/repo/pull/42#r5002",
            "body": "patch comment",
        },
        status_code=201,
    )
    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
    )
    suggestion = SuggestedEdit(
        file_path="src/main.py",
        line=42,
        replacement="```python\nprint(1)\n```",
        finding_id="vuln1",
        patch="diff --git a/src/main.py b/src/main.py\n...",
    )
    result = host.post_suggestion(suggestion)
    self.assertEqual(result.comment_id, "5002")
    self.assertEqual(result.kind, "patch")

    called_args, called_kwargs = self.mock_session.post.call_args
    payload = called_kwargs["json"]
    # Ensure raw ```suggestion block was not emitted
    self.assertNotIn("```suggestion", payload["body"])
    self.assertIn("```diff", payload["body"])

  def test_post_suggestion_inline_422_falls_back_to_artifacts_url(self):
    self.mock_session.post.side_effect = [
        _mock_response(
            None,
            status_code=422,
            text="Pull request review comment line must be part of the diff",
        ),
        _mock_response(
            None,
            status_code=422,
            text="File is not part of the diff",
        ),
    ]
    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
        env={
            "GITHUB_RUN_ID": "5002",
            "GITHUB_SERVER_URL": "https://github.com",
        },
    )
    suggestion = SuggestedEdit(
        file_path="src/other.py",
        line=10,
        patch="--- a/src/other.py\n+++ b/src/other.py\n",
        finding_id="vuln2",
    )
    result = host.post_suggestion(suggestion)
    self.assertEqual(result.comment_id, "")
    self.assertEqual(
        result.url,
        "https://github.com/owner/repo/actions/runs/5002#artifacts",
    )
    self.assertEqual(result.kind, "patch")

  def test_post_suggestion_server_error_raises_repo_host_error(self):
    self.mock_session.post.return_value = _mock_response(
        None,
        status_code=500,
        text="Internal Server Error",
    )
    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
    )
    suggestion = SuggestedEdit(
        file_path="src/other.py",
        line=10,
        patch="",
        finding_id="vuln2",
    )
    with self.assertRaises(RepoHostError) as ctx:
      host.post_suggestion(suggestion)
    self.assertIn("500", str(ctx.exception))

  def test_ensure_pr_context_does_not_use_merge_sha_when_event_available(self):
    self.mock_session.post.return_value = _mock_response(
        {
            "id": 5001,
            "html_url": "https://github.com/owner/repo/pull/42#r5001",
            "body": "<!-- codemender-finding:f1 -->\n```suggestion\nfix\n```",
        },
        status_code=201,
    )
    host = GitHubRepoHost(
        pr_number=42,
        event=self.default_event,
        token="test-token",
        env={
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_SHA": "synthetic_merge_commit_sha",
        },
        session=self.mock_session,
    )
    suggestion = SuggestedEdit(
        file_path="src/main.py",
        line=42,
        patch="```suggestion\nfix\n```",
        finding_id="f1",
    )
    host.post_suggestion(suggestion)

    # Verify that the commit_id used is the PR head SHA, NOT synthetic merge SHA
    _, kwargs = self.mock_session.post.call_args
    self.assertEqual(kwargs["json"]["commit_id"], "headabcdef1234")

  def test_ensure_pr_context_missing_context_raises_repo_host_error(self):
    host = GitHubRepoHost(token="test-token", env={})
    suggestion = SuggestedEdit(
        file_path="src/main.py",
        line=10,
        patch="suggestion",
        finding_id="f1",
    )
    with self.assertRaises(RepoHostError) as ctx:
      host.post_suggestion(suggestion)
    self.assertIn("Incomplete PR context", str(ctx.exception))

  def test_post_suggestion_with_file_path(self):
    self.mock_session.post.return_value = _mock_response(
        {
            "id": 6001,
            "html_url": "https://github.com/owner/repo/pull/42#r6001",
            "body": "Fallback patch body",
        },
        status_code=201,
    )
    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
    )
    patch = SuggestedEdit(
        finding_id="vuln3",
        patch="--- a/lib.py\n+++ b/lib.py\n@@ -1 +1 @@\n-bad\n+good\n",
        file_path="lib.py",
        summary="Diff outside hunk",
    )
    result = host.post_suggestion(patch)
    self.assertEqual(result.comment_id, "6001")
    self.assertEqual(result.kind, "patch")
    self.assertEqual(result.finding_id, "vuln3")

  def test_post_suggestion_without_file_path_falls_back_to_artifacts_url(self):
    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
        env={
            "GITHUB_RUN_ID": "7001",
            "GITHUB_SERVER_URL": "https://github.com",
        },
    )
    patch = SuggestedEdit(
        finding_id="vuln4",
        patch="--- a/all.py\n+++ b/all.py\n@@ -1 +1 @@\n-old\n+new\n",
        file_path=None,
    )
    result = host.post_suggestion(patch)
    self.assertEqual(result.comment_id, "")
    self.assertEqual(
        result.url,
        "https://github.com/owner/repo/actions/runs/7001#artifacts",
    )
    self.assertEqual(result.kind, "patch")
    self.mock_session.post.assert_not_called()

  def test_reply_to_comment_success(self):
    self.mock_session.post.return_value = _mock_response(
        {
            "id": 8001,
            "html_url": "https://github.com/owner/repo/pull/42#r8001",
            "body": "Reply comment body",
        },
        status_code=201,
    )
    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
    )
    result = host.reply_to_comment(
        comment_id="5001", body="Verified fix successfully."
    )
    self.assertEqual(result.comment_id, "8001")
    self.assertEqual(result.kind, "reply")
    called_args, called_kwargs = self.mock_session.post.call_args
    self.assertIn("pulls/42/comments/5001/replies", called_args[0])
    self.assertEqual(
        called_kwargs["json"]["body"], "Verified fix successfully."
    )

  def test_upsert_summary_creates_when_not_existing(self):
    # GET returns no existing sticky comment
    self.mock_session.get.return_value = _mock_response([])
    # POST creates new issue comment
    self.mock_session.post.return_value = _mock_response(
        {
            "id": 9001,
            "html_url": (
                "https://github.com/owner/repo/issues/42#issuecomment-9001"
            ),
            "body": "<!-- codemender-summary -->\n# Scan Results",
        },
        status_code=201,
    )
    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
    )
    result = host.upsert_summary("# Scan Results")
    self.assertEqual(result.comment_id, "9001")
    self.assertEqual(result.kind, "summary")
    self.mock_session.post.assert_called_once()
    self.mock_session.patch.assert_not_called()

  def test_upsert_summary_updates_when_existing(self):
    # GET returns existing sticky comment with id 9001
    self.mock_session.get.return_value = _mock_response(
        [
            {
                "id": 9001,
                "body": "<!-- codemender-summary -->\n# Old Results",
                "html_url": (
                    "https://github.com/owner/repo/issues/42#issuecomment-9001"
                ),
                "user": {"type": "Bot"},
            }
        ]
    )
    # PATCH updates existing issue comment
    self.mock_session.patch.return_value = _mock_response(
        {
            "id": 9001,
            "html_url": (
                "https://github.com/owner/repo/issues/42#issuecomment-9001"
            ),
            "body": "<!-- codemender-summary -->\n# Updated Results",
        },
        status_code=200,
    )
    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
    )
    result = host.upsert_summary("# Updated Results")
    self.assertEqual(result.comment_id, "9001")
    self.assertEqual(result.kind, "summary")
    self.mock_session.patch.assert_called_once()
    self.mock_session.post.assert_not_called()

  def test_upsert_summary_ignores_non_bot_comment(self):
    # GET returns spoofed sticky comment from regular user
    self.mock_session.get.return_value = _mock_response(
        [
            {
                "id": 9001,
                "body": "<!-- codemender-summary -->\n# Spoofed Results",
                "html_url": (
                    "https://github.com/owner/repo/issues/42#issuecomment-9001"
                ),
                "user": {"type": "User"},
            }
        ]
    )
    self.mock_session.post.return_value = _mock_response(
        {
            "id": 9002,
            "html_url": (
                "https://github.com/owner/repo/issues/42#issuecomment-9002"
            ),
            "body": "<!-- codemender-summary -->\n# Fresh Results",
        },
        status_code=201,
    )
    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
    )
    result = host.upsert_summary("# Fresh Results")
    self.assertEqual(result.comment_id, "9002")
    self.assertEqual(result.kind, "summary")
    self.mock_session.post.assert_called_once()
    self.mock_session.patch.assert_not_called()

  def test_set_security_gate_success(self):
    self.mock_session.post.return_value = _mock_response(
        {"state": "success"},
        status_code=201,
    )
    host = GitHubRepoHost(
        event=self.default_event,
        token="test-token",
        session=self.mock_session,
    )
    gate = SecurityGate(
        state=SecurityGateState.SUCCESS,
        context="CodeMender / Security Gate",
        description="All security checks passed",
        target_url="https://ci.example.com/build/123",
    )
    host.set_security_gate(gate)

    called_args, called_kwargs = self.mock_session.post.call_args
    self.assertIn("statuses/headabcdef1234", called_args[0])
    payload = called_kwargs["json"]
    self.assertEqual(payload["state"], "success")
    self.assertEqual(payload["context"], "CodeMender / Security Gate")
    self.assertEqual(payload["description"], "All security checks passed")
    self.assertEqual(payload["target_url"], "https://ci.example.com/build/123")

  def test_emit_ci_outputs_writes_single_and_multiline_to_file(self):
    gh_output_file = self.temp_path / "github_output.txt"
    host = GitHubRepoHost(
        event=self.default_event,
        env={"GITHUB_OUTPUT": str(gh_output_file)},
    )
    host.emit_ci_outputs({
        "status": "success",
        "findings_count": "3",
        "multiline_summary": "Line 1\nLine 2\nLine 3",
    })

    content = gh_output_file.read_text(encoding="utf-8")
    self.assertIn("status=success\n", content)
    self.assertIn("findings_count=3\n", content)
    self.assertIn("multiline_summary<<_EOF_", content)
    self.assertIn("Line 1\nLine 2\nLine 3", content)

  def test_emit_ci_outputs_multiline_with_carriage_return(self):
    gh_output_file = self.temp_path / "github_output_cr.txt"
    host = GitHubRepoHost(
        event=self.default_event,
        env={"GITHUB_OUTPUT": str(gh_output_file)},
    )
    host.emit_ci_outputs({
        "finding_status": "clean\rshould_run=true",
    })

    raw_bytes = gh_output_file.read_bytes()
    self.assertIn(b"finding_status<<_EOF_", raw_bytes)
    self.assertIn(b"clean\rshould_run=true", raw_bytes)
    self.assertNotIn(b"finding_status=clean\rshould_run=true\n", raw_bytes)

  def test_emit_ci_outputs_rejects_crlf_in_keys(self):
    gh_output_file = self.temp_path / "github_output_crlf_key.txt"
    host = GitHubRepoHost(
        event=self.default_event,
        env={"GITHUB_OUTPUT": str(gh_output_file)},
    )
    with self.assertRaises((RepoHostError, ValueError)):
      host.emit_ci_outputs({
          "status\noverride_gate=bypassed": "true",
      })

    with self.assertRaises((RepoHostError, ValueError)):
      host.emit_ci_outputs({
          "status\roverride_gate=bypassed": "true",
      })

    with self.assertRaises((RepoHostError, ValueError)):
      host.emit_ci_outputs({
          "status\r\noverride_gate=bypassed": "true",
      })

  def test_emit_ci_outputs_rejects_invalid_key_characters(self):
    host = GitHubRepoHost(
        event=self.default_event,
        env={"GITHUB_OUTPUT": str(self.temp_path / "out.txt")},
    )
    invalid_keys = ["", "bad key", "key=val", "key<<delim", "key:name"]
    for bad_key in invalid_keys:
      with self.subTest(key=bad_key):
        with self.assertRaises((RepoHostError, ValueError)):
          host.emit_ci_outputs({bad_key: "value"})

  def test_add_reaction_success_on_review_comment(self):
    self.mock_session.post.return_value = _mock_response({"id": 1}, 201)
    host = GitHubRepoHost(
        event=self.default_event,
        token="dummy-token",
        session=self.mock_session,
    )
    host.add_reaction("123", "eyes")
    self.mock_session.post.assert_called_once()
    call_args = self.mock_session.post.call_args
    self.assertIn("/pulls/comments/123/reactions", call_args.args[0])
    self.assertEqual(call_args.kwargs["json"], {"content": "eyes"})

  def test_add_reaction_fallback_to_issue_comment_on_404(self):
    self.mock_session.post.side_effect = [
        _mock_response({}, 404),
        _mock_response({"id": 1}, 201),
    ]
    host = GitHubRepoHost(
        event=self.default_event,
        token="dummy-token",
        session=self.mock_session,
    )
    host.add_reaction("123", "eyes")
    self.assertEqual(self.mock_session.post.call_count, 2)
    first_url = self.mock_session.post.call_args_list[0].args[0]
    second_url = self.mock_session.post.call_args_list[1].args[0]
    self.assertIn("/pulls/comments/123/reactions", first_url)
    self.assertIn("/issues/comments/123/reactions", second_url)

  def test_add_reaction_auth_error_raises_repo_host_auth_error(self):
    self.mock_session.post.return_value = _mock_response(
        {"message": "Bad credentials"}, 401
    )
    host = GitHubRepoHost(
        event=self.default_event,
        token="dummy-token",
        session=self.mock_session,
    )
    with self.assertRaises(RepoHostAuthError):
      host.add_reaction("123", "eyes")

  def test_add_reaction_rate_limit_error_raises_repo_host_rate_limit(self):
    self.mock_session.post.return_value = _mock_response(
        {"message": "API rate limit exceeded"},
        403,
        headers={"x-ratelimit-remaining": "0"},
    )
    host = GitHubRepoHost(
        event=self.default_event,
        token="dummy-token",
        session=self.mock_session,
    )
    with self.assertRaises(RepoHostRateLimitError):
      host.add_reaction("123", "eyes")

  def test_add_reaction_missing_token_raises_repo_host_auth_error(self):
    host = GitHubRepoHost(event=self.default_event, token="")
    with self.assertRaises(RepoHostAuthError):
      host.add_reaction("123", "eyes")


if __name__ == "__main__":
  unittest.main()
