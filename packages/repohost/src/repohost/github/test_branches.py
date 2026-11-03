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

# Governing: ADR-0008

"""Unit tests for repohost.github.branches module."""

import re
import unittest
from unittest.mock import MagicMock, patch

import repohost.github as github_pkg
from repohost.github import branches
from repohost.github import client
from repohost.github.branches import (
    FINDING_MARKER_PREFIX,
    MAX_COMMENT_BODY_CHARS,
    STICKY_SUMMARY_MARKER,
    build_review_comment,
    check_remote_branch_exists,
    create_pr_comment,
    create_pr_review_with_suggestions,
    create_pull_request,
    delete_remote_branch,
    finding_marker,
    format_suggestion_body,
    get_default_branch,
    get_pr_diff_line_ranges,
    is_duplicate_pr,
    list_reviewed_finding_ids,
    post_commit_status,
    post_or_update_sticky_comment,
    truncate_comment_body,
)


def _suggestion_is_top_level(markdown: str) -> bool:
  """Reports whether a ```suggestion fence opens at the document's top level.

  Mirrors CommonMark fence pairing: a fence nested inside an already-open code
  block is inert, so GitHub would render it as text instead of a suggestion.
  """
  open_len = 0
  for line in markdown.split("\n"):
    match = re.match(r"^(`{3,})(.*)$", line)
    if not match:
      continue
    run, info = len(match.group(1)), match.group(2).strip()
    if open_len == 0:
      if info == "suggestion":
        return True
      open_len = run
    elif run >= open_len and not info:
      open_len = 0
  return False


def _fence_left_open(markdown: str) -> bool:
  """Reports whether `markdown` ends with an unterminated code fence."""
  open_len = 0
  for line in markdown.split("\n"):
    match = re.match(r"^(`{3,})(.*)$", line)
    if not match:
      continue
    run, info = len(match.group(1)), match.group(2).strip()
    if open_len == 0:
      open_len = run
    elif run >= open_len and not info:
      open_len = 0
  return open_len != 0


class TestReExports(unittest.TestCase):

  def test_github_package_reexports_branch_symbols_and_keeps_client_bindings(self):
    expected_branch_names = (
        "build_review_comment",
        "check_remote_branch_exists",
        "create_pr_comment",
        "create_pr_review_with_suggestions",
        "create_pull_request",
        "delete_remote_branch",
        "format_suggestion_body",
        "get_default_branch",
        "is_duplicate_pr",
        "list_reviewed_finding_ids",
        "post_commit_status",
    )
    for name in expected_branch_names:
      self.assertIs(getattr(github_pkg, name), getattr(branches, name))

    for client_name in (
        "post_or_update_sticky_comment",
        "get_pr_diff_line_ranges",
        "truncate_comment_body",
        "retry_on_exception",
    ):
      self.assertIs(getattr(github_pkg, client_name), getattr(client, client_name))


@patch.object(branches.time, "sleep", lambda _: None)
class TestVcsGithub(unittest.TestCase):

  @patch("repohost.github.branches._get_branch_via_api")
  def test_check_remote_branch_exists_true(self, mock_api):
    """Verify branch check returns True when GitHub API finds the branch."""
    mock_api.return_value = True
    exists = check_remote_branch_exists(
        "https://github.com/org/repo.git", "fake_token", "feature-branch"
    )
    self.assertTrue(exists)
    mock_api.assert_called_once_with(
        "org", "repo", "feature-branch", "fake_token"
    )

  @patch("requests.post")
  def test_create_pull_request_success(self, mock_post):
    """Verify successful Pull Request creation."""
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {
        "html_url": "https://github.com/org/repo/pull/42"
    }
    mock_post.return_value = mock_resp

    pr_url = create_pull_request(
        token="token",
        owner="org",
        repo="repo",
        title="Fix SQLi",
        body="Details",
        head_branch="codemender/fix-sqli",
        base_branch="main",
    )
    self.assertEqual(pr_url, "https://github.com/org/repo/pull/42")

  @patch("requests.get")
  def test_get_default_branch_api_success(self, mock_get):
    """Verify fetching default branch via GitHub REST API."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"default_branch": "development"}
    mock_get.return_value = mock_resp

    branch = get_default_branch("token", "org", "repo")
    self.assertEqual(branch, "development")

  @patch("requests.post")
  def test_create_pr_comment_success(self, mock_post):
    """Verify posting review comment on PRs."""
    from repohost.github.branches import create_pr_comment

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {
        "html_url": "https://github.com/org/repo/pull/42#issuecomment-1"
    }
    mock_post.return_value = mock_resp

    comment_url = create_pr_comment(
        token="valid-token",
        owner="org",
        repo="repo",
        pr_number=42,
        body="## Security Fix Proposal",
    )
    self.assertEqual(
        comment_url, "https://github.com/org/repo/pull/42#issuecomment-1"
    )
    mock_post.assert_called_once()

  @patch("requests.get")
  def test_is_duplicate_pr_with_head_branch(self, mock_get):
    """Verify targeted O(1) duplicate PR lookup with head_branch parameter."""
    from repohost.github.branches import is_duplicate_pr

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"title": "Fix SQL Injection", "state": "open"}
    ]
    mock_get.return_value = mock_resp

    is_dup = is_duplicate_pr(
        repo_url="https://github.com/org/repo.git",
        token="token",
        file_path="db.py",
        vuln_type="SQL_INJECTION",
        start_line=10,
        head_branch="codemender/fix-sql_injection-abc12345",
    )
    self.assertTrue(is_dup)
    mock_get.assert_called_once()
    call_args = mock_get.call_args
    self.assertIn("head=org:codemender/fix-sql_injection-abc12345", call_args[0][0])

  @patch("requests.post")
  def test_post_commit_status_success(self, mock_post):
    """Verify posting a commit status check on a PR commit SHA."""
    from repohost.github.branches import post_commit_status

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"state": "failure"}
    mock_post.return_value = mock_resp

    success = post_commit_status(
        token="valid-token",
        owner="org",
        repo="repo",
        sha="abcdef123456",
        state="failure",
        description="Security Gate FAILED: 1 vulnerability detected",
        context="CodeMender / Security Gate",
        target_url="https://github.com/org/repo/pull/42",
    )
    self.assertTrue(success)
    mock_post.assert_called_once()
    url = mock_post.call_args[0][0]
    payload = mock_post.call_args[1]["json"]
    self.assertIn("/repos/org/repo/statuses/abcdef123456", url)
    self.assertEqual(payload["state"], "failure")
    self.assertEqual(payload["context"], "CodeMender / Security Gate")
    self.assertEqual(payload["target_url"], "https://github.com/org/repo/pull/42")

  def test_post_commit_status_fake_token(self):
    """Verify post_commit_status handles fake-token gracefully in unit tests."""
    from repohost.github.branches import post_commit_status

    success = post_commit_status(
        token="fake-token",
        owner="org",
        repo="repo",
        sha="abcdef123456",
        state="success",
        description="Security Gate PASSED",
    )
    self.assertTrue(success)

  @patch("requests.post")
  def test_post_commit_status_api_error_returns_false(self, mock_post):
    """Verify post_commit_status returns False on network error without throwing."""
    import requests
    from repohost.github.branches import post_commit_status

    mock_post.side_effect = requests.exceptions.RequestException("Connection error")

    success = post_commit_status(
        token="valid-token",
        owner="org",
        repo="repo",
        sha="abcdef123456",
        state="failure",
        description="Security Gate FAILED",
    )
    self.assertFalse(success)

  def test_delete_remote_branch_safety_guard_rejects_main(self):
    """Verify delete_remote_branch strictly rejects non-codemender branches."""
    from repohost.github.branches import delete_remote_branch

    # Refuse to delete protected or non-codemender branches
    self.assertFalse(
        delete_remote_branch("https://github.com/org/repo.git", "token", "main")
    )
    self.assertFalse(
        delete_remote_branch("https://github.com/org/repo.git", "token", "master")
    )
    self.assertFalse(
        delete_remote_branch("https://github.com/org/repo.git", "token", "feature/my-branch")
    )

  @patch("requests.delete")
  def test_delete_remote_branch_api_success(self, mock_delete):
    """Verify deleting branch via GitHub REST API with 204 status."""
    from repohost.github.branches import delete_remote_branch

    mock_resp = MagicMock()
    mock_resp.status_code = 204
    mock_delete.return_value = mock_resp

    success = delete_remote_branch(
        repo_url="https://github.com/org/repo.git",
        token="valid-token",
        branch_name="codemender/fix-sqli-abc12345",
    )
    self.assertTrue(success)
    mock_delete.assert_called_once()
    self.assertIn(
        "/repos/org/repo/git/refs/heads/codemender/fix-sqli-abc12345",
        mock_delete.call_args[0][0],
    )

  @patch("requests.delete")
  def test_delete_remote_branch_api_404_idempotent(self, mock_delete):
    """Verify deleting already-deleted branch returns True idempotently on 404/422."""
    from repohost.github.branches import delete_remote_branch

    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_delete.return_value = mock_resp

    success = delete_remote_branch(
        repo_url="https://github.com/org/repo.git",
        token="valid-token",
        branch_name="codemender/fix-sqli-abc12345",
    )
    self.assertTrue(success)

  @patch("repohost.github.branches.run_command")
  @patch("requests.delete")
  def test_delete_remote_branch_api_fail_falls_back_to_cli(
      self, mock_delete, mock_run_command
  ):
    """Verify falling back to Git CLI push --delete if REST API raises error."""
    import requests
    from repohost.github.branches import delete_remote_branch

    mock_delete.side_effect = requests.exceptions.RequestException("API error")
    mock_cli_res = MagicMock()
    mock_cli_res.returncode = 0
    mock_run_command.return_value = mock_cli_res

    success = delete_remote_branch(
        repo_url="https://github.com/org/repo.git",
        token="valid-token",
        branch_name="codemender/fix-sqli-abc12345",
    )
    self.assertTrue(success)
    mock_run_command.assert_called_once()
    cmd = mock_run_command.call_args[0][0]
    self.assertIn("--delete", cmd)
    self.assertIn("codemender/fix-sqli-abc12345", cmd)


class TestSuggestionBodyFormatting(unittest.TestCase):
  """Validates fence sizing and anchor payload construction."""

  def test_wraps_replacement_in_suggestion_fence(self):
    body = format_suggestion_body(["  return escape(value)"])
    self.assertEqual(body, "```suggestion\n  return escape(value)\n```")

  def test_widens_fence_past_embedded_backticks(self):
    body = format_suggestion_body(['q = "```"'])
    self.assertTrue(body.startswith("````suggestion\n"))
    self.assertTrue(body.endswith("\n````"))

  def test_empty_replacement_renders_deletion_block(self):
    self.assertEqual(format_suggestion_body([]), "```suggestion\n```")

  def test_preamble_precedes_block(self):
    body = format_suggestion_body(["x = 1"], preamble="Heads up")
    self.assertEqual(body, "Heads up\n\n```suggestion\nx = 1\n```")

  def test_unterminated_preamble_fence_is_closed(self):
    """An open fence in the analysis would otherwise swallow the suggestion."""
    body = format_suggestion_body(
        ["safe(x)"], preamble="Vulnerable:\n```python\neval(user_input)"
    )
    self.assertTrue(_suggestion_is_top_level(body), body)

  def test_balanced_preamble_fence_is_left_alone(self):
    preamble = "Vulnerable:\n```python\neval(x)\n```\nUse literal_eval."
    body = format_suggestion_body(["safe(x)"], preamble=preamble)
    self.assertTrue(body.startswith(preamble + "\n\n"))
    self.assertTrue(_suggestion_is_top_level(body), body)

  def test_single_line_anchor_omits_start_line(self):
    comment = build_review_comment("a.py", 7, 7, "body")
    self.assertEqual(comment["line"], 7)
    self.assertEqual(comment["side"], "RIGHT")
    self.assertNotIn("start_line", comment)

  def test_multi_line_anchor_includes_start_line(self):
    comment = build_review_comment("a.py", 7, 9, "body")
    self.assertEqual(comment["start_line"], 7)
    self.assertEqual(comment["start_side"], "RIGHT")
    self.assertEqual(comment["line"], 9)


class TestPrDiffLineRanges(unittest.TestCase):
  """Validates discovery of the lines GitHub will accept as review anchors."""

  @patch("repohost.github.branches._fetch_pr_files")
  def test_expands_hunk_headers_into_line_numbers(self, mock_fetch):
    mock_fetch.return_value = [{
        "filename": "app/views.py",
        "patch": "@@ -1,3 +1,4 @@\n ctx\n+new\n@@ -20,0 +30,2 @@\n+a\n+b",
    }]
    ranges = get_pr_diff_line_ranges("t", "org", "repo", 7)
    self.assertEqual(ranges["app/views.py"], {1, 2, 3, 4, 30, 31})

  @patch("repohost.github.branches._fetch_pr_files")
  def test_file_without_patch_is_not_addressable(self, mock_fetch):
    """Binary or oversized files have no patch, so nothing can anchor to them."""
    mock_fetch.return_value = [{"filename": "logo.png", "status": "added"}]
    self.assertEqual(get_pr_diff_line_ranges("t", "org", "repo", 7), {})

  @patch("repohost.github.branches._fetch_pr_files")
  def test_fetch_failure_yields_empty_map(self, mock_fetch):
    mock_fetch.side_effect = RuntimeError("boom")
    self.assertEqual(get_pr_diff_line_ranges("t", "org", "repo", 7), {})


class TestCreateReviewWithSuggestions(unittest.TestCase):
  """Validates review submission and its fallback signalling."""

  @patch("requests.post")
  def test_posts_review_and_returns_url(self, mock_post):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "html_url": "https://github.com/org/repo/pull/7#pullrequestreview-99"
    }
    mock_post.return_value = resp

    url = create_pr_review_with_suggestions(
        token="t",
        owner="org",
        repo="repo",
        pr_number=7,
        commit_id="deadbeef",
        body="summary",
        comments=[build_review_comment("a.py", 3, 3, "body")],
    )
    self.assertEqual(
        url, "https://github.com/org/repo/pull/7#pullrequestreview-99"
    )
    payload = mock_post.call_args.kwargs["json"]
    self.assertEqual(payload["event"], "COMMENT")
    self.assertEqual(payload["commit_id"], "deadbeef")

  @patch("requests.post")
  def test_omits_commit_id_when_absent(self, mock_post):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"html_url": "https://example.com"}
    mock_post.return_value = resp

    create_pr_review_with_suggestions(
        token="t",
        owner="org",
        repo="repo",
        pr_number=7,
        commit_id=None,
        body="summary",
        comments=[build_review_comment("a.py", 3, 3, "body")],
    )
    self.assertNotIn("commit_id", mock_post.call_args.kwargs["json"])

  @patch("requests.post")
  def test_out_of_diff_rejection_returns_none_for_fallback(self, mock_post):
    resp = MagicMock()
    resp.status_code = 422
    resp.text = '{"message":"line must be part of the diff"}'
    mock_post.return_value = resp

    url = create_pr_review_with_suggestions(
        token="t",
        owner="org",
        repo="repo",
        pr_number=7,
        commit_id="deadbeef",
        body="summary",
        comments=[build_review_comment("a.py", 3, 3, "body")],
    )
    self.assertIsNone(url)
    # A 422 is permanent for this payload and must not be retried.
    self.assertEqual(mock_post.call_count, 1)

  def test_no_comments_short_circuits(self):
    self.assertIsNone(
        create_pr_review_with_suggestions(
            token="t",
            owner="org",
            repo="repo",
            pr_number=7,
            commit_id="sha",
            body="b",
            comments=[],
        )
    )


class TestCommentTruncation(unittest.TestCase):
  """Validates the GitHub 65,536 character comment body cap."""

  def test_short_body_passes_through_untouched(self):
    self.assertEqual(truncate_comment_body("all good"), "all good")

  def test_oversized_body_fits_within_the_cap(self):
    body = "\n".join(f"| finding-{i} | HIGH | XSS |" for i in range(6000))
    self.assertGreater(len(body), MAX_COMMENT_BODY_CHARS)

    result = truncate_comment_body(body)

    self.assertLessEqual(len(result), MAX_COMMENT_BODY_CHARS)
    self.assertIn("truncated", result)
    # The cut must land on a line boundary, never mid-row.
    self.assertTrue(result.split("\n\n---\n")[0].endswith("|"))

  def test_reserved_prefix_counts_against_the_budget(self):
    body = "x" * (MAX_COMMENT_BODY_CHARS + 10)
    result = truncate_comment_body(body, reserved=500)
    self.assertLessEqual(len(result) + 500, MAX_COMMENT_BODY_CHARS)

  def test_cut_inside_a_code_block_closes_the_fence(self):
    body = "intro\n```python\n" + "payload = 1\n" * 12000
    result = truncate_comment_body(body)
    self.assertLessEqual(len(result), MAX_COMMENT_BODY_CHARS)
    # An unclosed fence would swallow the truncation notice.
    self.assertFalse(_fence_left_open(result), result[-200:])

  @patch("requests.post")
  @patch("requests.Session")
  def test_sticky_comment_truncates_and_keeps_marker(
      self, mock_session, mock_post
  ):
    listing = MagicMock()
    listing.json.return_value = []
    listing.links = {}
    mock_session.return_value.__enter__.return_value.get.return_value = listing

    created = MagicMock()
    created.status_code = 201
    created.json.return_value = {"html_url": "https://example.com/c/1"}
    mock_post.return_value = created

    post_or_update_sticky_comment(
        "t", "org", "repo", 7, "y" * (MAX_COMMENT_BODY_CHARS + 1000)
    )

    posted = mock_post.call_args.kwargs["json"]["body"]
    self.assertLessEqual(len(posted), MAX_COMMENT_BODY_CHARS)
    # Losing the marker would orphan the comment on the next run.
    self.assertTrue(posted.startswith(STICKY_SUMMARY_MARKER))


class TestStickyCommentAndDedup(unittest.TestCase):
  """Validates the single summary comment and cross-run finding dedup."""

  @patch("requests.post")
  @patch("requests.patch")
  @patch("requests.Session")
  def test_updates_existing_marked_comment(
      self, mock_session, mock_patch, mock_post
  ):
    listing = MagicMock()
    listing.json.return_value = [
        {"id": 1, "body": "unrelated"},
        {"id": 2, "body": f"{STICKY_SUMMARY_MARKER}\nold summary"},
    ]
    listing.links = {}
    mock_session.return_value.__enter__.return_value.get.return_value = listing

    updated = MagicMock()
    updated.status_code = 200
    updated.json.return_value = {"html_url": "https://example.com/c/2"}
    mock_patch.return_value = updated

    url = post_or_update_sticky_comment("t", "org", "repo", 7, "new summary")
    self.assertEqual(url, "https://example.com/c/2")
    mock_post.assert_not_called()
    self.assertIn("/issues/comments/2", mock_patch.call_args.args[0])

  @patch("requests.post")
  @patch("requests.Session")
  def test_creates_comment_when_marker_absent(self, mock_session, mock_post):
    listing = MagicMock()
    listing.json.return_value = [{"id": 1, "body": "unrelated"}]
    listing.links = {}
    mock_session.return_value.__enter__.return_value.get.return_value = listing

    created = MagicMock()
    created.status_code = 201
    created.json.return_value = {"html_url": "https://example.com/c/9"}
    mock_post.return_value = created

    url = post_or_update_sticky_comment("t", "org", "repo", 7, "summary")
    self.assertEqual(url, "https://example.com/c/9")
    self.assertTrue(
        mock_post.call_args.kwargs["json"]["body"].startswith(
            STICKY_SUMMARY_MARKER
        )
    )

  @patch("requests.Session")
  def test_extracts_finding_ids_from_markers(self, mock_session):
    listing = MagicMock()
    listing.json.return_value = [
        {"body": f"{FINDING_MARKER_PREFIX}CM-1 -->\nsuggestion"},
        {"body": "a human review comment"},
        {"body": finding_marker("CM-2")},
    ]
    listing.links = {}
    mock_session.return_value.__enter__.return_value.get.return_value = listing

    self.assertEqual(
        list_reviewed_finding_ids("t", "org", "repo", 7), {"CM-1", "CM-2"}
    )


if __name__ == "__main__":
  unittest.main()
