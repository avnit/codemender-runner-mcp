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

import io
import json
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from codemender.vcs.git import SuggestionHunk
import repohost.github as github_pkg
from repohost.github import client as github
from repohost.github.client import (
    FINDING_TAG_RE,
    GITHUB_API_URL,
    GitHubAuthError,
    GitHubClientError,
    GitHubRateLimitError,
    MAX_COMMENT_BODY_CHARS,
    ReviewResult,
    STICKY_SUMMARY_MARKER,
    code_fence,
    create_review_with_suggestions,
    delete_codemender_review_comments,
    delete_review_comment,
    fetch_finding_sarif_from_artifact,
    finding_tag,
    format_patch_comment,
    get_pr_diff_line_ranges,
    get_review_comment,
    get_sticky_comment_body,
    list_verify_comments,
    post_or_update_sticky_comment,
    post_patch_comment,
    react_to_review_comment,
    reply_to_comment,
    retry_on_exception,
    truncate_comment_body,
    update_review_comment,
    verify_tag,
)


def _response(json_body, status_code=200, links=None, text=None):
  resp = MagicMock()
  resp.status_code = status_code
  resp.json.return_value = json_body
  resp.links = links or {}
  resp.raise_for_status.return_value = None
  resp.text = text if text is not None else ""
  return resp


class TestReExports(unittest.TestCase):

  def test_github_package_reexports_client_symbols(self):
    expected_symbols = {
        "FINDING_TAG_RE": FINDING_TAG_RE,
        "GITHUB_API_URL": GITHUB_API_URL,
        "MAX_COMMENT_BODY_CHARS": MAX_COMMENT_BODY_CHARS,
        "ReviewResult": ReviewResult,
        "STICKY_SUMMARY_MARKER": STICKY_SUMMARY_MARKER,
        "code_fence": code_fence,
        "create_review_with_suggestions": create_review_with_suggestions,
        "delete_codemender_review_comments": delete_codemender_review_comments,
        "delete_review_comment": delete_review_comment,
        "finding_tag": finding_tag,
        "format_patch_comment": format_patch_comment,
        "get_pr_diff_line_ranges": get_pr_diff_line_ranges,
        "post_or_update_sticky_comment": post_or_update_sticky_comment,
        "post_patch_comment": post_patch_comment,
        "reply_to_comment": reply_to_comment,
        "retry_on_exception": retry_on_exception,
        "truncate_comment_body": truncate_comment_body,
    }
    for name, obj in expected_symbols.items():
      self.assertIs(getattr(github_pkg, name), obj)


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
        _response(
            [{"id": 1, "user": {"type": "User"}, "body": STICKY_SUMMARY_MARKER}],
            links={"next": {"url": "page2"}},
        ),
        _response(
            [{"id": 99, "user": {"type": "Bot"}, "body": STICKY_SUMMARY_MARKER}]
        ),
    ]
    session.patch.return_value = _response({"html_url": "https://x/c/99"})
    self.assertEqual(
        post_or_update_sticky_comment("tok", "o/r", 7, "b"), "https://x/c/99"
    )

  @patch.object(github.requests, "Session")
  def test_delete_codemender_review_comments_and_pr_diff_ranges(
      self, mock_session_cls
  ):
    session = mock_session_cls.return_value.__enter__.return_value
    session.get.return_value = _response([
        {
            "id": 1,
            "user": {"type": "Bot"},
            "body": "<!-- codemender-finding:aabbcc112233 -->\n```suggestion\nx\n```",
            "html_url": "https://c/1",
        },
        {
            "id": 2,
            "user": {"type": "Bot"},
            "in_reply_to_id": 1,
            "body": "This fix changes the same lines as the fix for finding 2.",
            "html_url": "https://c/2",
        },
        {
            "id": 3,
            "user": {"type": "Bot"},
            "in_reply_to_id": 1,
            "body": verify_tag("done", "aabbcc112233", "VERIFIED") + "\nVerified",
            "html_url": "https://c/3",
        },
        {
            "id": 4,
            "user": {"type": "User"},
            "in_reply_to_id": 1,
            "body": "@cm-runner verify",
            "html_url": "https://c/4",
        },
        {
            "id": 5,
            "user": {"type": "Bot"},
            "body": "Unrelated bot comment from another integration",
            "html_url": "https://c/5",
        },
    ])
    session.delete.return_value = _response({}, 204)

    deleted = delete_codemender_review_comments("o/r", 7, "tok")
    self.assertEqual(deleted, 3)
    deleted_urls = [c.args[0] for c in session.delete.call_args_list]
    # Replies (2, 3) are deleted before root comment (1); user/unrelated comments (4, 5) are untouched.
    self.assertEqual(
        deleted_urls,
        [
            f"{GITHUB_API_URL}/repos/o/r/pulls/comments/2",
            f"{GITHUB_API_URL}/repos/o/r/pulls/comments/3",
            f"{GITHUB_API_URL}/repos/o/r/pulls/comments/1",
        ],
    )
    self.assertFalse(delete_review_comment("o/r", 0, "tok"))

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
    self.assertEqual(session.post.call_args.kwargs["json"]["body"], "")
    self.assertIn(
        "<!-- codemender-finding:aabbcc112233 -->",
        session.post.call_args.kwargs["json"]["comments"][0]["body"],
    )

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

  @patch.object(github.requests, "Session")
  def test_verify_comment_helpers(self, mock_session_cls):
    session = mock_session_cls.return_value.__enter__.return_value
    self.assertEqual(
        verify_tag("done", "aabbcc112233", "VERIFIED"),
        "<!-- codemender-verify:done finding:aabbcc112233 status:VERIFIED -->",
    )
    with self.assertRaises(ValueError):
      finding_tag("../evil")

    session.patch.return_value = _response({"html_url": "https://c/55", "id": 55})
    updated = update_review_comment("o/r", 55, "tok", "updated")
    self.assertTrue(updated.ok)
    self.assertEqual(updated.comment_id, 55)

    session.post.return_value = _response({"id": 1}, 201)
    self.assertTrue(react_to_review_comment("o/r", 55, "tok", "eyes"))

    session.get.return_value = _response({"id": 55, "body": "parent"})
    self.assertEqual(get_review_comment("o/r", 55, "tok"), {"id": 55, "body": "parent"})

    session.get.return_value = _response([
        {
            "id": 88,
            "user": {"type": "Bot"},
            "body": verify_tag("done", "aabbcc112233", "DISMISSED") + "\nDismissed",
            "html_url": "https://c/88",
            "updated_at": "2026-10-16T12:00:00Z",
        },
        {
            "id": 89,
            "user": {"type": "User"},
            "body": verify_tag("in-progress", "aabbcc112233"),
        },
    ])
    verifies = list_verify_comments("o/r", 7, "tok")
    self.assertEqual(len(verifies), 1)
    self.assertEqual(verifies[0].finding_id, "aabbcc112233")
    self.assertEqual(verifies[0].state, "done")
    self.assertEqual(verifies[0].status, "DISMISSED")

    session.get.return_value = _response([
        {"id": 8, "user": {"type": "User"}, "body": f"{STICKY_SUMMARY_MARKER}\n### Spoofed"},
        {"id": 9, "user": {"type": "Bot"}, "body": f"{STICKY_SUMMARY_MARKER}\n### Summary"},
    ])
    self.assertIn("### Summary", get_sticky_comment_body("o/r", 7, "tok"))

  @patch.object(github.requests, "Session")
  def test_fetch_finding_sarif_from_artifact(self, mock_session_cls):
    session = mock_session_cls.return_value.__enter__.return_value
    sarif_doc = {
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "cm", "rules": [{"id": "R1"}]}},
            "results": [{
                "ruleId": "R1",
                "level": "error",
                "message": {"text": "boom"},
                "partialFingerprints": {"primaryLocationLineHash": "aabbcc112233"},
                "locations": [{
                    "physicalLocation": {
                        "artifactLocation": {"uri": "src/a.py"},
                        "region": {"startLine": 5},
                    }
                }],
            }],
        }],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
      zf.writestr("findings/finding_1.sarif", json.dumps(sarif_doc))
    zip_resp = _response({}, 200)
    zip_resp.content = buf.getvalue()
    session.get.side_effect = [
        _response({"artifacts": [{"id": 101, "expired": False}]}),
        zip_resp,
    ]
    found = fetch_finding_sarif_from_artifact("o/r", 7, "tok", "aabbcc112233")
    self.assertIsNotNone(found)
    self.assertEqual(found["runs"][0]["results"][0]["ruleId"], "R1")

  @patch.object(github.requests, "Session")
  def test_fetch_finding_sarif_auth_error_401(self, mock_session_cls):
    session = mock_session_cls.return_value.__enter__.return_value
    session.get.return_value = _response(None, 401, text="Bad credentials")
    with self.assertRaises(GitHubAuthError):
      fetch_finding_sarif_from_artifact("o/r", 7, "tok", "f1")

  @patch.object(github.requests, "Session")
  def test_fetch_finding_sarif_rate_limit_403(self, mock_session_cls):
    session = mock_session_cls.return_value.__enter__.return_value
    session.get.return_value = _response(
        None, 403, text="API rate limit exceeded"
    )
    with self.assertRaises(GitHubRateLimitError):
      fetch_finding_sarif_from_artifact("o/r", 7, "tok", "f1")

  @patch.object(github.requests, "Session")
  def test_fetch_finding_sarif_server_error_500(self, mock_session_cls):
    session = mock_session_cls.return_value.__enter__.return_value
    session.get.return_value = _response(
        None, 500, text="Internal Server Error"
    )
    with self.assertRaises(GitHubClientError):
      fetch_finding_sarif_from_artifact("o/r", 7, "tok", "f1")



if __name__ == "__main__":
  unittest.main()
