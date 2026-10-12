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

"""Unit tests for fix_comments (SPEC-GHA-FIX REQ-0006 to REQ-0010)."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

import fix_comments  # pylint: disable=g-import-not-at-top
from codemender.cm import Finding  # pylint: disable=g-import-not-at-top
from vcs import github  # pylint: disable=g-import-not-at-top
from vcs.git import SuggestionHunk  # pylint: disable=g-import-not-at-top

_APP = b"line1\nline2\nline3\nline4\nline5\n"
_ARTIFACTS = "https://github.com/o/r/actions/runs/123#artifacts"
_GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _finding(index, path="src/app.py", start=3, end=3, rule="CWE-78", fp=None):
  return Finding(
      finding_id=fp or f"{rule}:{path}:{start}-{end}",
      file_path=path,
      index=index,
      start_line=start,
      end_line=end,
      rule_id=rule,
      level="error",
      message="Title: details",
      document={"runs": [{"results": [{}]}]},
  )


def _patch(path="src/app.py", old_start=2, old=("line2", "line3", "line4"), new=None):
  new = new if new is not None else (old[0], "fixed3", old[2])
  lines = [
      f"diff --git a/{path} b/{path}",
      "index 1111111..2222222 100644",
      f"--- a/{path}",
      f"+++ b/{path}",
      f"@@ -{old_start},{len(old)} +{old_start},{len(new)} @@",
      f" {old[0]}",
      *[f"-{line}" for line in old[1:-1]],
      *[f"+{line}" for line in new[1:-1]],
      f" {old[-1]}",
  ]
  return ("\n".join(lines) + "\n").encode("utf-8")


def _make_repo(files):
  repo = tempfile.mkdtemp(prefix="fix-comments-test-")
  env = {**os.environ, **_GIT_ENV}
  subprocess.run(["git", "-C", repo, "init", "-q"], check=True, env=env)
  for path, content in files.items():
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
      f.write(content)
  subprocess.run(["git", "-C", repo, "add", "-A"], check=True, env=env)
  subprocess.run(
      ["git", "-C", repo, "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-q", "-m", "t"],
      check=True,
      env=env,
  )
  head = subprocess.run(
      ["git", "-C", repo, "rev-parse", "HEAD"],
      check=True,
      capture_output=True,
      text=True,
      env=env,
  ).stdout.strip()
  return repo, head


class TestPlanFixes(unittest.TestCase):

  def test_findings_without_earlier_comments_are_all_fixed(self):
    findings = [_finding(1, start=1, end=1), _finding(2, start=3, end=3)]
    plan = fix_comments.plan_fixes(findings, {})
    self.assertEqual([f.index for f in plan.to_fix], [1, 2])
    self.assertEqual(
        plan.cells,
        {1: fix_comments.FixCell("working"), 2: fix_comments.FixCell("working")},
    )
    self.assertEqual(plan.shared, {1: [1], 2: [2]})
    self.assertEqual(plan.fingerprints[2], findings[1].finding_id)

  def test_current_earlier_comment_is_linked_and_outdated_is_fixed_again(self):
    f1, f2, f3 = _finding(1, start=1), _finding(2, start=2), _finding(3, start=3)
    earlier = {
        f1.finding_id: github.FindingComment(url="https://c/1", outdated=False, kind="suggestion"),
        f2.finding_id: github.FindingComment(url="https://c/2", outdated=False, kind="patch"),
        f3.finding_id: github.FindingComment(url="https://c/3", outdated=True, kind="suggestion"),
    }
    plan = fix_comments.plan_fixes([f1, f2, f3], earlier)
    self.assertEqual([f.index for f in plan.to_fix], [3])
    self.assertEqual(plan.cells[1], fix_comments.FixCell("suggested", "https://c/1"))
    self.assertEqual(plan.cells[2], fix_comments.FixCell("patch", "https://c/2"))
    self.assertEqual(plan.cells[3], fix_comments.FixCell("working"))

  def test_same_fingerprint_is_fixed_once_and_shared(self):
    findings = [_finding(1, fp="dup"), _finding(2, fp="dup"), _finding(3, fp="other")]
    plan = fix_comments.plan_fixes(findings, {})
    self.assertEqual([f.index for f in plan.to_fix], [1, 3])
    self.assertEqual(plan.shared, {1: [1, 2], 3: [3]})
    self.assertEqual(plan.cells[2], fix_comments.FixCell("working"))


class TestHelpers(unittest.TestCase):

  def test_checked_out_commit(self):
    repo, commit = _make_repo({"src/app.py": _APP})
    outside = tempfile.mkdtemp(prefix="fix-comments-outside-")
    try:
      self.assertEqual(fix_comments.checked_out_commit(repo), commit)
      self.assertEqual(fix_comments.checked_out_commit(outside), "")
    finally:
      shutil.rmtree(repo, ignore_errors=True)
      shutil.rmtree(outside, ignore_errors=True)

  def test_same_lines_note(self):
    self.assertEqual(
        fix_comments.same_lines_note([(3, "https://c/3")]),
        "This fix changes the same lines as the fix for"
        " [finding 3](https://c/3). Commit only one of them; CodeMender checks"
        " the other finding again on the next push.",
    )
    note = fix_comments.same_lines_note([(1, ""), (2, "https://c/2"), (4, "")])
    self.assertIn(
        "the fixes for finding 1, [finding 2](https://c/2) and finding 4.", note
    )
    self.assertNotIn("#", note.replace("https://c/2", ""))


@mock.patch.object(github, "reply_to_comment")
@mock.patch.object(github, "post_patch_comment")
@mock.patch.object(github, "create_review_with_suggestions")
@mock.patch.object(github, "get_pr_diff_line_ranges")
class TestFixPoster(unittest.TestCase):

  def setUp(self):
    super().setUp()
    self._repo, self._head = _make_repo({"src/app.py": _APP, "src/other.py": _APP})

  def tearDown(self):
    shutil.rmtree(self._repo, ignore_errors=True)
    super().tearDown()

  def _poster(self, head_checked_out=True, indexes=(1, 2, 3)):
    return fix_comments.FixPoster(
        repo="o/r",
        pr_number=7,
        token="tok",
        head_sha=self._head,
        repo_dir=self._repo,
        headers={i: f"**Fix {i}**" for i in indexes},
        fingerprints={i: f"{i:012x}" for i in indexes},
        artifacts_url=_ARTIFACTS,
        head_checked_out=head_checked_out,
    )

  def test_no_fix_and_failed_post_nothing(self, ranges, review, comment, reply):
    poster = self._poster()
    self.assertEqual(
        poster.post(_finding(1), fix_comments.NO_FIX),
        fix_comments.FixCell("no_fix"),
    )
    self.assertEqual(
        poster.post(_finding(2), fix_comments.FAILED),
        fix_comments.FixCell("failed"),
    )
    for api in (ranges, review, comment, reply):
      api.assert_not_called()

  def test_fix_inside_pr_changes_becomes_suggestions(self, ranges, review, comment, reply):
    ranges.return_value = {"src/app.py": {1, 2, 3, 4, 5}}
    review.return_value = github.ReviewResult(True, "https://r/1", 200, 11)

    cell = self._poster().post(_finding(1), fix_comments.FIXED, _patch())

    self.assertEqual(cell, fix_comments.FixCell("suggested", "https://r/1"))
    review.assert_called_once_with(
        "o/r",
        7,
        "tok",
        self._head,
        [SuggestionHunk("src/app.py", 3, 3, ["fixed3"])],
        "**Fix 1**",
        "<!-- codemender-finding:000000000001 -->",
        notes=[fix_comments.UNTESTED_NOTE],
    )
    comment.assert_not_called()
    reply.assert_not_called()

  def test_fix_outside_pr_changes_becomes_patch_comment(self, ranges, review, comment, reply):
    del reply
    ranges.return_value = {"src/app.py": {5}}
    comment.return_value = github.ReviewResult(True, "https://c/1", 201, 21)

    cell = self._poster().post(_finding(1), fix_comments.FIXED, _patch())

    self.assertEqual(cell, fix_comments.FixCell("patch", "https://c/1"))
    review.assert_not_called()
    repo, pr, token, head, path, line, body = comment.call_args.args
    self.assertEqual(
        (repo, pr, token, head, path, line),
        ("o/r", 7, "tok", self._head, "src/app.py", 3),
    )
    self.assertIn("<!-- codemender-finding:000000000001 -->", body)
    self.assertIn("outside the pull request's diff", body)
    self.assertIn("+fixed3", body)

  def test_github_workflow_modification_is_blocked_from_suggestions(
      self, ranges, review, comment, reply
  ):
    del reply
    ranges.return_value = {".github/workflows/ci.yml": {1, 2, 3, 4, 5}}
    comment.return_value = github.ReviewResult(True, "https://c/1", 201, 21)

    cell = self._poster().post(
        _finding(1, path=".github/workflows/ci.yml"),
        fix_comments.FIXED,
        _patch(path=".github/workflows/ci.yml"),
    )

    self.assertEqual(cell, fix_comments.FixCell("patch", "https://c/1"))
    review.assert_not_called()
    self.assertIn(".github/workflows/ci.yml", comment.call_args.args[6])

  def test_rejected_review_and_missing_checkout_fallbacks(
      self, ranges, review, comment, reply
  ):
    del reply
    ranges.return_value = {"src/app.py": {1, 2, 3, 4, 5}}
    review.return_value = github.ReviewResult(False, "", 422)
    comment.return_value = github.ReviewResult(True, "https://c/1", 201, 21)

    cell = self._poster().post(_finding(1), fix_comments.FIXED, _patch())
    self.assertEqual(cell, fix_comments.FixCell("patch", "https://c/1"))
    self.assertIn("accept this fix as a suggestion", comment.call_args.args[6])

    # When GitHub rejects the patch comment too, fall back to artifacts link.
    comment.return_value = github.ReviewResult(False, "", 422)
    cell2 = self._poster(head_checked_out=False).post(
        _finding(1), fix_comments.FIXED, _patch()
    )
    self.assertEqual(cell2, fix_comments.FixCell("patch", _ARTIFACTS))

  def test_overlapping_fixes_both_get_the_note(self, ranges, review, comment, reply):
    ranges.return_value = {"src/app.py": {5}}
    comment.side_effect = [
        github.ReviewResult(True, "https://c/1", 201, 21),
        github.ReviewResult(True, "https://c/2", 201, 22),
        github.ReviewResult(True, "https://c/3", 201, 23),
    ]
    reply.return_value = github.ReviewResult(True, "https://c/r", 201, 31)
    poster = self._poster()

    poster.post(_finding(1), fix_comments.FIXED, _patch())
    poster.post(
        _finding(2),
        fix_comments.FIXED,
        _patch(new=("line2", "other3", "line4")),
    )
    poster.post(
        _finding(3, path="src/other.py"),
        fix_comments.FIXED,
        _patch(path="src/other.py"),
    )

    review.assert_not_called()
    self.assertNotIn("changes the same lines", comment.call_args_list[0].args[6])
    self.assertIn("the fix for [finding 1](https://c/1)", comment.call_args_list[1].args[6])
    self.assertNotIn("changes the same lines", comment.call_args_list[2].args[6])
    reply.assert_called_once_with(
        "o/r", 7, "tok", 21, fix_comments.same_lines_note([(2, "https://c/2")])
    )


if __name__ == "__main__":
  unittest.main()
