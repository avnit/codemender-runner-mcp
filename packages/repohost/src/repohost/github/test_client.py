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

# Governing: ADR-0008, SPEC-GHA, SPEC-GHA-FIX

"""Unit tests for repohost.github.client."""

import unittest
from unittest.mock import MagicMock, patch

from codemender.vcs.git import SuggestionHunk
import repohost.github as github_pkg
from repohost.github import client as github
from repohost.github.client import (
    FINDING_TAG_RE,
    FindingComment,
    GITHUB_API_URL,
    MAX_COMMENT_BODY_CHARS,
    ReviewResult,
    STICKY_SUMMARY_MARKER,
    code_fence,
    create_review_with_suggestions,
    finding_tag,
    format_patch_comment,
    get_pr_diff_line_ranges,
    list_finding_comments,
    post_or_update_sticky_comment,
    post_patch_comment,
    reply_to_comment,
    retry_on_exception,
    truncate_comment_body,
)


def _response(json_body, status_code=200, links=None):
  resp = MagicMock()
  resp.status_code = status_code
  resp.json.return_value = json_body
  resp.links = links or {}
  resp.raise_for_status.return_value = None
  return resp


class TestReExports(unittest.TestCase):

  def test_github_package_reexports_client_symbols(self):
    expected_symbols = {
        "FINDING_TAG_RE": FINDING_TAG_RE,
        "FindingComment": FindingComment,
        "GITHUB_API_URL": GITHUB_API_URL,
        "MAX_COMMENT_BODY_CHARS": MAX_COMMENT_BODY_CHARS,
        "ReviewResult": ReviewResult,
        "STICKY_SUMMARY_MARKER": STICKY_SUMMARY_MARKER,
        "code_fence": code_fence,
        "create_review_with_suggestions": create_review_with_suggestions,
        "finding_tag": finding_tag,
        "format_patch_comment": format_patch_comment,
        "get_pr_diff_line_ranges": get_pr_diff_line_ranges,
        "list_finding_comments": list_finding_comments,
        "post_or_update_sticky_comment": post_or_update_sticky_comment,
        "post_patch_comment": post_patch_comment,
        "reply_to_comment": reply_to_comment,
        "retry_on_exception": retry_on_exception,
        "truncate_comment_body": truncate_comment_body,
    }
    for name, obj in expected_symbols.items():
      self.assertIs(getattr(github_pkg, name), obj)
      self.assertIn(name, github_pkg.__all__)


class TestTruncateAndFormatting(unittest.TestCase):

  def test_truncate_comment_body(self):
    self.assertEqual(truncate_comment_body("hello"), "hello")
    body = "```\n" + ("x\n" * MAX_COMMENT_BODY_CHARS)
    result = truncate_comment_body(body)
    self.assertLessEqual(len(result), MAX_COMMENT_BODY_CHARS)
    self.assertIn("truncated", result)
    before_notice = result.split("\n\n---\n")[0]
    self.assertEqual(before_notice.count("```") % 2, 0)

  def test_format_patch_comment_fences_embedded_backticks(self):
    body = format_patch_comment(
        tag="<!-- codemender-finding:001122334455 -->",
        header="**Fix 1**",
        reason="Outside diff.",
        patch="diff --git a/x b/x\n+```injected```\n",
        notes=["Untested note."],
    )
    self.assertIn("````diff", body)
    self.assertIn("git apply", body)


@patch.object(github.time, "sleep", lambda _: None)
class TestGitHubApi(unittest.TestCase):

  @patch.object(github.requests, "Session")
  def test_posts_and_updates_sticky_comment_with_pagination(self, mock_session_cls):
    session = mock_session_cls.return_value.__enter__.return_value
    session.get.return_value = _response([{"id": 1, "body": "unrelated"}])
    session.post.return_value = _response({"html_url": "https://x/c/2"}, 201)

    self.assertEqual(
        post_or_update_sticky_comment("tok", "o/r", 7, "hi"), "https://x/c/2"
    )
    self.assertTrue(
        session.post.call_args.kwargs["json"]["body"].startswith(STICKY_SUMMARY_MARKER)
    )

    session.get.side_effect = [
        _response([{"id": 1, "body": "a"}], links={"next": {"url": "page2"}}),
        _response([{"id": 99, "body": STICKY_SUMMARY_MARKER}]),
    ]
    session.patch.return_value = _response({"html_url": "https://x/c/99"})
    self.assertEqual(
        post_or_update_sticky_comment("tok", "o/r", 7, "b"), "https://x/c/99"
    )

  @patch.object(github.requests, "Session")
  def test_list_finding_comments_and_pr_diff_ranges(self, mock_session_cls):
    session = mock_session_cls.return_value.__enter__.return_value
    session.get.return_value = _response([
        {
            "id": 1,
            "user": {"type": "Bot"},
            "body": "<!-- codemender-finding:aabbcc112233 -->\n```suggestion\nx\n```",
            "html_url": "https://c/1",
            "line": 10,
            "subject_type": "line",
        },
        {
            "id": 2,
            "user": {"type": "Bot"},
            "body": "<!-- codemender-finding:ddeeff445566 -->\n```diff\nx\n```",
            "html_url": "https://c/2",
            "line": None,
            "subject_type": "line",
        },
    ])
    comments = list_finding_comments("o/r", 7, "tok")
    self.assertFalse(comments["aabbcc112233"].outdated)
    self.assertEqual(comments["aabbcc112233"].kind, "suggestion")
    self.assertTrue(comments["ddeeff445566"].outdated)
    self.assertEqual(comments["ddeeff445566"].kind, "patch")

    session.get.return_value = _response([
        {
            "filename": "src/app.py",
            "status": "modified",
            "patch": "@@ -1,2 +10,3 @@\n line10\n+line11\n line12",
        }
    ])
    ranges = get_pr_diff_line_ranges("o/r", 7, "tok")
    self.assertEqual(ranges, {"src/app.py": {10, 11, 12}})

  @patch.object(github.requests, "Session")
  def test_create_review_and_post_patch_comment_fallback(self, mock_session_cls):
    session = mock_session_cls.return_value.__enter__.return_value
    session.post.return_value = _response({"html_url": "https://r/1", "id": 5}, 200)
    session.get.return_value = _response([{"id": 11}])
    res = create_review_with_suggestions(
        "o/r",
        7,
        "tok",
        "deadbeef",
        [SuggestionHunk("src/app.py", 2, 3, ["fixed"])],
        "**Fix 1**",
        "<!-- codemender-finding:aabbcc112233 -->",
    )
    self.assertTrue(res.ok)
    self.assertEqual(res.url, "https://r/1")
    self.assertEqual(res.comment_id, 11)

    # Line comment fails with 422 -> falls back to file-level comment.
    session.post.reset_mock()
    session.post.side_effect = [
        _response({}, 422),
        _response({"html_url": "https://c/file", "id": 9}, 201),
    ]
    patched = post_patch_comment(
        "o/r", 7, "tok", "deadbeef", "src/app.py", 42, "body"
    )
    self.assertTrue(patched.ok)
    self.assertEqual(patched.url, "https://c/file")
    self.assertEqual(
        session.post.call_args_list[1].kwargs["json"]["subject_type"], "file"
    )


if __name__ == "__main__":
  unittest.main()
