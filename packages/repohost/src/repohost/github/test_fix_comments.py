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

# Governing: ADR-0008, SPEC-GHA-FIX REQ-0006, REQ-0007, REQ-0008,
# REQ-0009, REQ-0010

"""Unit tests for repohost.github.fix_comments."""

import os
import shutil
import subprocess
import tempfile
from typing import Optional
import unittest
from unittest import mock

from codemender.cm import Finding
from codemender.vcs.git import SuggestionHunk
import repohost.github as github_pkg
from repohost.github import fix_comments
from repohost.models import SuggestedEdit
from repohost.testing import InMemoryRepoHost

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


def _patch(
    path="src/app.py",
    old_start=2,
    old=("line2", "line3", "line4"),
    new=None,
):
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
      [
          "git",
          "-C",
          repo,
          "-c",
          "user.name=T",
          "-c",
          "user.email=t@e",
          "commit",
          "-q",
          "-m",
          "t",
      ],
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


class TestReExports(unittest.TestCase):

  def test_github_package_reexports_fix_comments_symbols(self):
    expected_names = (
        "FAILED",
        "FIXED",
        "FixCell",
        "FixPlan",
        "FixPoster",
        "NO_FIX",
        "PATCH",
        "SUGGESTED",
        "UNTESTED_NOTE",
        "WORKING",
        "checked_out_commit",
        "plan_fixes",
        "same_lines_note",
    )
    for name in expected_names:
      self.assertIs(getattr(github_pkg, name), getattr(fix_comments, name))


class TestPlanFixes(unittest.TestCase):

  def test_all_distinct_findings_are_planned_as_working(self):
    findings = [_finding(1, start=1, end=1), _finding(2, start=3, end=3)]
    plan = fix_comments.plan_fixes(findings)
    self.assertEqual([f.index for f in plan.to_fix], [1, 2])
    self.assertEqual(
        plan.cells,
        {
            1: fix_comments.FixCell("working"),
            2: fix_comments.FixCell("working"),
        },
    )
    self.assertEqual(plan.shared, {1: [1], 2: [2]})
    self.assertEqual(plan.fingerprints[2], findings[1].finding_id)

  def test_same_fingerprint_is_fixed_once_and_shared(self):
    findings = [
        _finding(1, fp="dup"),
        _finding(2, fp="dup"),
        _finding(3, fp="other"),
    ]
    plan = fix_comments.plan_fixes(findings)
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


class TestFixPoster(unittest.TestCase):

  def setUp(self):
    super().setUp()
    self._repo, self._head = _make_repo(
        {"src/app.py": _APP, "src/other.py": _APP}
    )

  def tearDown(self):
    shutil.rmtree(self._repo, ignore_errors=True)
    super().tearDown()

  def _poster(
      self,
      host: Optional[InMemoryRepoHost] = None,
      head_checked_out: bool = True,
      indexes=(1, 2, 3),
  ):
    return fix_comments.FixPoster(
        host=host or InMemoryRepoHost(),
        repo_dir=self._repo,
        headers={i: f"**Fix {i}**" for i in indexes},
        fingerprints={i: f"{i:012x}" for i in indexes},
        head_checked_out=head_checked_out,
    )

  def test_no_fix_and_failed_post_nothing(self):
    host = InMemoryRepoHost()
    poster = self._poster(host=host)
    self.assertEqual(
        poster.post(_finding(1), fix_comments.NO_FIX),
        fix_comments.FixCell("no_fix"),
    )
    self.assertEqual(
        poster.post(_finding(2), fix_comments.FAILED),
        fix_comments.FixCell("failed"),
    )
    self.assertEqual(len(host.posted_suggestions), 0)
    self.assertEqual(len(host.replies), 0)

  def test_fix_posts_suggestion_via_host(self):
    host = InMemoryRepoHost()
    cell = self._poster(host=host).post(
        _finding(1), fix_comments.FIXED, _patch()
    )
    self.assertEqual(cell.status, "suggested")
    self.assertTrue(cell.url.startswith("https://inmemory.repo/"))
    self.assertEqual(len(host.posted_suggestions), 1)
    s = host.posted_suggestions[0]
    self.assertEqual(s.finding_id, "000000000001")
    self.assertEqual(s.file_path, "src/app.py")
    self.assertEqual(s.line, 3)
    self.assertIn("**Fix 1**", s.summary or "")
    self.assertIsNone(s.start_line)

  def test_multiline_fix_sets_start_line_and_end_line(self):
    host = InMemoryRepoHost()
    multiline_patch = _patch(
        old_start=2,
        old=("line2", "line3", "line4", "line5"),
        new=("line2", "fixed3", "fixed4", "line5"),
    )
    cell = self._poster(host=host).post(
        _finding(1), fix_comments.FIXED, multiline_patch
    )
    self.assertEqual(cell.status, "suggested")
    self.assertEqual(len(host.posted_suggestions), 1)
    s = host.posted_suggestions[0]
    self.assertEqual(s.start_line, 3)
    self.assertEqual(s.line, 4)
    self.assertEqual(s.replacement, "fixed3\nfixed4\n")

  def test_multi_hunk_fix_falls_back_to_patch(self):
    host = InMemoryRepoHost()
    multi_hunk_patch = (
        b"diff --git a/src/app.py b/src/app.py\n"
        b"--- a/src/app.py\n"
        b"+++ b/src/app.py\n"
        b"@@ -1,3 +1,3 @@\n"
        b" line1\n"
        b"-line2\n"
        b"+fixed2\n"
        b" line3\n"
        b"@@ -4,2 +4,2 @@\n"
        b" line4\n"
        b"-line5\n"
        b"+fixed5\n"
    )
    cell = self._poster(host=host).post(
        _finding(1), fix_comments.FIXED, multi_hunk_patch
    )
    self.assertEqual(cell.status, "patch")
    self.assertEqual(len(host.posted_suggestions), 1)
    s = host.posted_suggestions[0]
    self.assertIsNone(s.replacement)
    self.assertIsNone(s.start_line)
    self.assertEqual(s.line, 3)
    self.assertIn("multiple diff hunks", s.summary)

  def test_github_workflow_modification_is_blocked_from_suggestions(self):
    host = InMemoryRepoHost()
    patch = _patch(path=".github/workflows/ci.yml")
    cell = self._poster(host=host).post(
        _finding(1, path=".github/workflows/ci.yml"),
        fix_comments.FIXED,
        patch,
    )
    self.assertEqual(cell.status, "patch")
    self.assertEqual(len(host.posted_suggestions), 1)
    s = host.posted_suggestions[0]
    self.assertIsNone(s.replacement)
    self.assertIsNone(s.start_line)
    self.assertEqual(s.line, 3)
    self.assertIn("modifies workflow or GitHub configuration", s.summary)

  def test_head_not_checked_out_falls_back_to_patch(self):
    host = InMemoryRepoHost()
    cell = self._poster(host=host, head_checked_out=False).post(
        _finding(1), fix_comments.FIXED, _patch()
    )
    self.assertEqual(cell.status, "patch")
    self.assertEqual(len(host.posted_suggestions), 1)
    s = host.posted_suggestions[0]
    self.assertIsNone(s.replacement)
    self.assertIsNone(s.start_line)
    self.assertIsNone(s.line)
    self.assertIn("didn't check out the PR's head commit", s.summary)

  def test_fix_with_code_fences_in_replacement_omits_inline_suggestion(self):
    host = InMemoryRepoHost()
    patch_with_backticks = _patch(new=("line2", "```python", "print(1)", "```"))
    cell = self._poster(host=host).post(
        _finding(1), fix_comments.FIXED, patch_with_backticks
    )
    self.assertEqual(cell.status, "patch")
    self.assertEqual(len(host.posted_suggestions), 1)
    s = host.posted_suggestions[0]
    self.assertIsNone(s.replacement)
    self.assertIsNone(s.start_line)
    self.assertEqual(s.line, 3)
    self.assertIn("code fence", s.summary)

  def test_host_failure_returns_failed_cell(self):
    class FailingRepoHost(InMemoryRepoHost):

      def post_suggestion(self, suggestion):
        raise RuntimeError("boom")

    poster = self._poster(host=FailingRepoHost())
    cell = poster.post(_finding(1), fix_comments.FIXED, _patch())
    self.assertEqual(cell, fix_comments.FixCell("failed"))

  def test_overlapping_fixes_both_get_the_note(self):
    host = InMemoryRepoHost()
    poster = self._poster(host=host)

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

    self.assertEqual(len(host.posted_suggestions), 3)
    self.assertEqual(len(host.replies), 1)
    reply_id, reply_body = host.replies[0]
    self.assertEqual(reply_id, "inmem-sugg-1")
    self.assertIn("the fix for [finding 2]", reply_body)
    self.assertIn(
        "the fix for [finding 1]",
        host.posted_suggestions[1].summary,
    )


if __name__ == "__main__":
  unittest.main()
