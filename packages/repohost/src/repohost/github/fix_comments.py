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

# Governing: ADR-0008, SPEC-GHA-FIX REQ-0006, REQ-0007, REQ-0008, REQ-0009, REQ-0010

"""Plans and posts Pull Request fix comments (SPEC-GHA-FIX REQ-0006 to REQ-0010).

Public entry points:
  - `plan_fixes`: Deduplicates findings by fingerprint (`finding.finding_id`) and
    skips findings that already have an active (non-outdated) CodeMender comment.
  - `FixPoster`: Posts each completed fix as a one-click GitHub suggestion review
    when all modified lines fall within the PR diff, or falls back to a line/file
    patch comment.
"""

import dataclasses
import logging
import re
import subprocess
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from codemender.cm import Finding
from codemender.vcs.git import SuggestionHunk, normalize_repo_relative_path, parse_patch_to_suggestions
from repohost.github import client as github

logger = logging.getLogger("codemender-github-actions")

# Fix outcomes and Sticky-comment Fix column statuses (SPEC-GHA-FIX REQ-0005, REQ-0006).
FIXED = "fixed"
WORKING = "working"
SUGGESTED = "suggested"
PATCH = "patch"
NO_FIX = "no_fix"
FAILED = "failed"

# Added to every comment with a fix (SPEC-GHA-FIX REQ-0010).
UNTESTED_NOTE = (
    "CodeMender made this fix on its own, without the other fixes on this PR,"
    " and may not have built or tested it. Review it before you commit it."
)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


@dataclasses.dataclass(frozen=True)
class FixCell:
  """What the sticky comment's Fix column shows for one finding."""

  status: str
  url: str = ""


@dataclasses.dataclass(frozen=True)
class FixPlan:
  """Which findings to fix, and initial Fix column state."""

  to_fix: List[Finding]
  cells: Dict[int, FixCell]
  fingerprints: Dict[int, str]
  shared: Dict[int, List[int]]


def plan_fixes(
    findings: Sequence[Finding],
    earlier: Mapping[str, github.FindingComment],
) -> FixPlan:
  """Decides which findings need a new fix run (SPEC-GHA-FIX REQ-0009)."""
  to_fix: List[Finding] = []
  cells: Dict[int, FixCell] = {}
  fingerprints: Dict[int, str] = {}
  shared: Dict[int, List[int]] = {}
  first_with: Dict[str, int] = {}

  for finding in findings:
    fp = finding.finding_id
    fingerprints[finding.index] = fp
    comment = earlier.get(fp)
    if comment is not None and not comment.outdated:
      status = SUGGESTED if comment.kind == "suggestion" else PATCH
      cells[finding.index] = FixCell(status, comment.url)
      continue
    cells[finding.index] = FixCell(WORKING)
    first = first_with.get(fp)
    if first is None:
      first_with[fp] = finding.index
      shared[finding.index] = [finding.index]
      to_fix.append(finding)
    else:
      shared[first].append(finding.index)
  return FixPlan(to_fix, cells, fingerprints, shared)


@dataclasses.dataclass(frozen=True)
class _PostedFix:
  index: int
  lines_by_file: Dict[str, Set[int]]
  comment: github.ReviewResult


class FixPoster:
  """Posts each finished fix on the PR and returns its FixCell (main thread only)."""

  def __init__(
      self,
      *,
      repo: str,
      pr_number: int,
      token: str,
      head_sha: str,
      repo_dir: str,
      headers: Mapping[int, str],
      fingerprints: Mapping[int, str],
      artifacts_url: str,
      head_checked_out: bool,
  ):
    self._repo = repo
    self._pr_number = pr_number
    self._token = token
    self._head_sha = head_sha
    self._repo_dir = repo_dir
    self._headers = headers
    self._fingerprints = fingerprints
    self._artifacts_url = artifacts_url
    self._head_checked_out = head_checked_out
    self._pr_ranges: Optional[Dict[str, Set[int]]] = None
    self._pr_ranges_read = False
    self._posted: List[_PostedFix] = []

  def post(self, finding: Finding, outcome: str, patch_bytes: bytes = b"") -> FixCell:
    """Posts one finished fix to the PR and returns the updated FixCell."""
    if outcome == NO_FIX:
      return FixCell(NO_FIX)
    if outcome != FIXED:
      return FixCell(FAILED)

    try:
      patch = patch_bytes.decode("utf-8")
      exact = True
    except UnicodeDecodeError:
      patch = patch_bytes.decode("utf-8", errors="replace")
      exact = False

    hunks, blockers = (
        parse_patch_to_suggestions(self._repo_dir, patch)
        if exact
        else ([], ["patch is not valid UTF-8 text"])
    )
    lines_by_file = _patch_changed_lines(patch, hunks)
    overlapping = [
        p for p in self._posted if _overlaps(lines_by_file, p.lines_by_file)
    ]
    notes = [UNTESTED_NOTE]
    if overlapping:
      notes.append(same_lines_note([(p.index, p.comment.url) for p in overlapping]))

    cell, comment = self._post_patch(
        finding, patch, exact, hunks, blockers, lines_by_file, notes
    )

    for prev in overlapping:
      if prev.comment.comment_id:
        reply = github.reply_to_comment(
            self._repo,
            self._pr_number,
            self._token,
            prev.comment.comment_id,
            same_lines_note([(finding.index, comment.url)]),
        )
        if not reply.ok:
          logger.warning(
              "Could not add same-lines note to fix for finding %d.", prev.index
          )

    self._posted.append(_PostedFix(finding.index, lines_by_file, comment))
    return cell

  def _post_patch(
      self,
      finding: Finding,
      patch: str,
      exact: bool,
      hunks: List[SuggestionHunk],
      blockers: List[str],
      lines_by_file: Dict[str, Set[int]],
      notes: Sequence[str],
  ) -> Tuple[FixCell, github.ReviewResult]:
    tag = github.finding_tag(self._fingerprints[finding.index])
    header = self._headers[finding.index]

    valid_hunks, reason = self._verify_suggestions(exact, hunks, blockers)
    if valid_hunks:
      review = github.create_review_with_suggestions(
          self._repo,
          self._pr_number,
          self._token,
          self._head_sha,
          valid_hunks,
          header,
          tag,
          notes=notes,
      )
      if review.ok:
        return FixCell(SUGGESTED, review.url), review
      reason = "GitHub didn't accept this fix as a suggestion."

    body = github.format_patch_comment(
        tag=tag,
        header=header,
        reason=reason,
        patch=patch,
        notes=notes,
        artifacts_url=self._artifacts_url,
    )
    path = finding.path or next(iter(lines_by_file), "")
    line = finding.start_line if (self._head_checked_out and finding.path) else 0
    if path:
      posted = github.post_patch_comment(
          self._repo, self._pr_number, self._token, self._head_sha, path, line, body
      )
      if posted.ok:
        return FixCell(PATCH, posted.url), posted

    logger.warning(
        "GitHub took no comment for finding %d; linking to artifacts.", finding.index
    )
    return FixCell(PATCH, self._artifacts_url), github.ReviewResult(ok=False)

  def _verify_suggestions(
      self,
      exact: bool,
      hunks: List[SuggestionHunk],
      blockers: List[str],
  ) -> Tuple[List[SuggestionHunk], str]:
    if not exact:
      return [], (
          "This fix isn't valid UTF-8 text, so it can't be a suggestion and"
          " the copy below may not apply. The exact patch is in the run's"
          " artifacts."
      )
    if not self._head_checked_out:
      return [], (
          "This fix can't be a suggestion because the workflow didn't check"
          " out the PR's head commit, so its line numbers may not match the PR."
      )
    pr_ranges = self._read_pr_ranges()
    if pr_ranges is None:
      return [], (
          "This fix can't be a suggestion because GitHub didn't return the"
          " PR's changes."
      )
    if blockers or not hunks:
      detail = blockers[0] if blockers else "contains no applicable changes"
      return [], f"This fix can't be a suggestion because it {detail}."

    last_end_by_file: Dict[str, int] = {}
    for hunk in hunks:
      if any("```" in line or "~~~" in line for line in hunk.replacement_lines):
        return [], (
            "This fix includes a Markdown code fence (` ``` ` or `~~~`),"
            " which would break a GitHub suggestion block."
        )
      prev_end = last_end_by_file.get(hunk.path, 0)
      if hunk.start_line <= prev_end:
        return [], "This fix has overlapping diff hunks in the same file."
      last_end_by_file[hunk.path] = hunk.end_line

      in_diff = pr_ranges.get(hunk.path, set())
      if not all(line in in_diff for line in range(hunk.start_line, hunk.end_line + 1)):
        return [], (
            "This fix changes lines outside the pull request's diff, where"
            " GitHub doesn't allow suggestions."
        )
    return hunks, ""

  def _read_pr_ranges(self) -> Optional[Dict[str, Set[int]]]:
    if not self._pr_ranges_read:
      self._pr_ranges_read = True
      self._pr_ranges = github.get_pr_diff_line_ranges(
          self._repo, self._pr_number, self._token
      )
      if self._pr_ranges is None:
        logger.warning(
            "Could not read PR diff ranges from GitHub; posting patch comments."
        )
    return self._pr_ranges


def checked_out_commit(repo_dir: str) -> str:
  """Returns the SHA of HEAD in `repo_dir`, or '' if unknown."""
  try:
    proc = subprocess.run(
        ["git", "-C", repo_dir, "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
  except (OSError, subprocess.SubprocessError) as e:
    logger.warning("Could not read checked-out commit: %s", e)
    return ""
  return proc.stdout.strip() if proc.returncode == 0 else ""


def same_lines_note(others: Sequence[Tuple[int, str]]) -> str:
  """Builds the conflict warning note when two fixes touch overlapping lines."""
  refs = [f"[finding {idx}]({url})" if url else f"finding {idx}" for idx, url in others]
  if len(refs) == 1:
    which, rest = f"the fix for {refs[0]}", "the other finding"
  else:
    which, rest = f"the fixes for {', '.join(refs[:-1])} and {refs[-1]}", "the other findings"
  return (
      f"This fix changes the same lines as {which}. Commit only one of them;"
      f" CodeMender checks {rest} again on the next push."
  )


def _patch_changed_lines(
    patch: str, hunks: Sequence[SuggestionHunk]
) -> Dict[str, Set[int]]:
  """Extracts 1-based HEAD line numbers touched per file for conflict detection."""
  by_file: Dict[str, Set[int]] = {}
  for hunk in hunks:
    by_file.setdefault(hunk.path, set()).update(
        range(hunk.start_line, hunk.end_line + 1)
    )
  current_path = ""
  for line in patch.splitlines():
    if line.startswith("diff --git "):
      parts = line.split(" ")
      if len(parts) >= 4:
        current_path = normalize_repo_relative_path(parts[-1][2:])
        by_file.setdefault(current_path, set())
    elif line.startswith("+++ b/"):
      current_path = normalize_repo_relative_path(line[6:].strip())
      if current_path:
        by_file.setdefault(current_path, set())
    elif line.startswith("@@ ") and current_path:
      m = _HUNK_RE.match(line)
      if m:
        start = int(m.group(1))
        count = int(m.group(2)) if m.group(2) is not None else 1
        end = start if count == 0 else start + count - 1
        by_file.setdefault(current_path, set()).update(range(max(start, 1), max(end, 1) + 1))
  return {k: v for k, v in by_file.items() if k}


def _overlaps(first: Dict[str, Set[int]], second: Dict[str, Set[int]]) -> bool:
  return any(bool(lines & second.get(path, set())) for path, lines in first.items())
