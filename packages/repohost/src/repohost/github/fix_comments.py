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
# Governing: ADR-0008,
# SPEC-GHA-FIX REQ-0006, REQ-0007, REQ-0008, REQ-0009, REQ-0010

"""Plans and posts Pull Request fix comments (SPEC-GHA-FIX REQ-0006-0010).

Governing: ADR-0008, docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0001, docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0003.

Public entry points:
  - `plan_fixes`: Plans which findings to fix, deduplicating findings that share
    the same fingerprint (`finding.finding_id`) within a single run.
  - `FixPoster`: Posts each completed fix via `RepoHost.post_suggestion`.
"""

import dataclasses
import logging
import re
import subprocess
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from codemender.cm import Finding
from codemender.vcs.git import (
    SuggestionHunk,
    normalize_repo_relative_path,
    parse_patch_to_suggestions,
)
from repohost.base import RepoHost
from repohost.models import SuggestedEdit

logger = logging.getLogger("codemender-github-actions")

# Fix outcomes and Sticky-comment Fix column statuses
# (SPEC-GHA-FIX REQ-0005, REQ-0006).
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
) -> FixPlan:
  """Decides which findings need a fix run, deduplicating identical
  fingerprints in one run (SPEC-GHA-FIX REQ-0009).
  """
  to_fix: List[Finding] = []
  cells: Dict[int, FixCell] = {}
  fingerprints: Dict[int, str] = {}
  shared: Dict[int, List[int]] = {}
  first_with: Dict[str, int] = {}

  for finding in findings:
    fp = finding.finding_id
    fingerprints[finding.index] = fp
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
  url: str
  comment_id: str


class FixPoster:
  """Posts each finished fix on the PR and returns its FixCell."""

  def __init__(
      self,
      *,
      host: RepoHost,
      repo_dir: str,
      headers: Mapping[int, str],
      fingerprints: Mapping[int, str],
      head_checked_out: bool,
  ):
    self._host = host
    self._repo_dir = repo_dir
    self._headers = headers
    self._fingerprints = fingerprints
    self._head_checked_out = head_checked_out
    self._posted: List[_PostedFix] = []

  def post(
      self, finding: Finding, outcome: str, patch_bytes: bytes = b""
  ) -> FixCell:
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
    overlap_note = (
        same_lines_note([(p.index, p.url) for p in overlapping])
        if overlapping
        else ""
    )

    cell, comment_url, comment_id = self._post_patch(
        finding,
        patch,
        hunks,
        lines_by_file,
        blockers=blockers,
        overlap_note=overlap_note,
        exact=exact,
    )

    for prev in overlapping:
      if prev.comment_id:
        note_text = same_lines_note([(finding.index, comment_url)])
        try:
          self._host.reply_to_comment(prev.comment_id, note_text)
        except Exception as e:  # pylint: disable=broad-exception-caught
          logger.warning(
              "Could not add same-lines note to fix for finding %d: %s",
              prev.index,
              e,
          )

    self._posted.append(
        _PostedFix(finding.index, lines_by_file, comment_url, comment_id)
    )
    return cell

  def _post_patch(
      self,
      finding: Finding,
      patch: str,
      hunks: List[SuggestionHunk],
      lines_by_file: Dict[str, Set[int]],
      *,
      blockers: Sequence[str] = (),
      overlap_note: str = "",
      exact: bool = True,
  ) -> Tuple[FixCell, str, str]:
    header = self._headers[finding.index]
    path = finding.path or (next(iter(lines_by_file)) if lines_by_file else "")
    replacement = None
    start_line = None
    line = None
    reason = ""

    if not exact:
      reason = (
          "This fix isn't valid UTF-8 text, so it can't be a suggestion and"
          " the copy below may not apply. The exact patch is in the run's"
          " artifacts."
      )
    elif not self._head_checked_out:
      reason = (
          "This fix can't be a suggestion because the workflow didn't check"
          " out the PR's head commit, so its line numbers may not match the PR."
      )
    elif blockers:
      reason = f"This fix can't be a suggestion because it {blockers[0]}."
    elif not hunks:
      reason = (
          "This fix can't be a suggestion because it contains no applicable"
          " changes."
      )
    elif len(hunks) > 1:
      reason = (
          "This fix can't be a suggestion because it spans multiple diff"
          " hunks."
      )
    else:
      hunk = hunks[0]
      has_fence = any(
          "```" in l or "~~~" in l for l in hunk.replacement_lines
      )
      if has_fence:
        reason = (
            "This fix includes a Markdown code fence (` ``` ` or `~~~`), which"
            " would break a GitHub suggestion block."
        )
      else:
        replacement = (
            "\n".join(hunk.replacement_lines)
            + ("\n" if hunk.replacement_lines else "")
        )
        if hunk.start_line < hunk.end_line:
          start_line = hunk.start_line
        line = hunk.end_line
        if hunk.path:
          path = hunk.path

    if replacement is None:
      if (
          self._head_checked_out
          and (finding.path or path)
          and finding.start_line > 0
      ):
        line = finding.start_line

    summary_parts = [header]
    if reason:
      summary_parts.append(reason)
    summary_parts.append(UNTESTED_NOTE)
    if overlap_note:
      summary_parts.append(overlap_note)
    summary = "\n\n".join(summary_parts)

    suggestion = SuggestedEdit(
        file_path=path or None,
        line=line,
        replacement=replacement,
        finding_id=self._fingerprints[finding.index],
        start_line=start_line,
        patch=patch,
        summary=summary,
    )
    try:
      posted = self._host.post_suggestion(suggestion)
      if posted and posted.url:
        cell_status = (
            SUGGESTED if posted.kind == "suggestion" else PATCH
        )
        return (
            FixCell(cell_status, posted.url),
            posted.url,
            posted.comment_id or "",
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
      logger.warning(
          "host.post_suggestion failed for finding %d: %s",
          finding.index,
          e,
      )

    return FixCell(FAILED), "", ""


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
  refs = [
      f"[finding {idx}]({url})" if url else f"finding {idx}"
      for idx, url in others
  ]
  if len(refs) == 1:
    which, rest = f"the fix for {refs[0]}", "the other finding"
  else:
    which = f"the fixes for {', '.join(refs[:-1])} and {refs[-1]}"
    rest = "the other findings"
  return (
      f"This fix changes the same lines as {which}. Commit only one of them;"
      f" CodeMender checks {rest} again on the next push."
  )


def _patch_changed_lines(
    patch: str, hunks: Sequence[SuggestionHunk]
) -> Dict[str, Set[int]]:
  """Extracts 1-based HEAD lines per file for conflict detection."""
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
        lines = range(max(start, 1), max(end, 1) + 1)
        by_file.setdefault(current_path, set()).update(lines)
  return {k: v for k, v in by_file.items() if k}


def _overlaps(first: Dict[str, Set[int]], second: Dict[str, Set[int]]) -> bool:
  return any(
      bool(lines & second.get(path, set())) for path, lines in first.items()
  )
