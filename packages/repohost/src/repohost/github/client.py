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

"""GitHub REST API client and PR comment formatters for CodeMender.

Posting to the repo host is the CI job's responsibility (ADR-0008, SPEC-GHA,
SPEC-GHA-FIX); cm-runner never receives a GitHub token.

Public entry points are at the top of this module; internal HTTP and Markdown
helpers follow below.
"""

import dataclasses
import functools
import io
import logging
import re
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple
import zipfile

from codemender.sarif import SarifDocument
import requests

logger = logging.getLogger("codemender-github-actions")

GITHUB_API_URL = "https://api.github.com"

# Hidden HTML marker that identifies the comment owned by CodeMender, so it
# can be found and updated instead of posting a new comment on every run.
STICKY_SUMMARY_MARKER = "<!-- codemender-summary -->"
FINDING_TAG_RE = re.compile(r"<!-- codemender-finding:([A-Za-z0-9_.:]+) -->")
VERIFY_TAG_RE = re.compile(
    r"<!-- codemender-verify:(in-progress|done|failed)"
    r" finding:([A-Za-z0-9_.:]+)(?: status:([^\s>]+))? -->"
)
_MAX_ARTIFACT_FILE_BYTES = 20 * 1024 * 1024

# GitHub rejects issue/review comment bodies longer than this.
MAX_COMMENT_BODY_CHARS = 65536

_TRUNCATION_NOTICE = (
    "\n\n---\n"
    "*⚠️ This comment was truncated because it exceeded GitHub's 65,536"
    " character limit. See the workflow run logs for the full output.*"
)
_TRUNCATION_SLACK = 16
_MD_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_BACKTICK_RUN_RE = re.compile(r"`+")
_PR_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_REQUEST_TIMEOUT_SECONDS = 15
_POST_TIMEOUT_SECONDS = 30
_MAX_PAGES = 100


# ---------------------------------------------------------------------------
# Public Data Models & Entry Points
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ReviewResult:
  """Outcome of posting a PR review or review comment."""

  ok: bool
  url: str = ""
  status: int = 0
  comment_id: int = 0


@dataclasses.dataclass(frozen=True)
class VerifyComment:
  """An existing CodeMender verification reply comment on the PR."""

  finding_id: str
  state: str
  status: str = ""
  url: str = ""
  updated_at: str = ""
  comment_id: int = 0


class GitHubClientError(Exception):
  """Base exception for GitHub API client errors."""


class GitHubAuthError(GitHubClientError):
  """Raised when authentication with the GitHub API fails (401)."""


class GitHubRateLimitError(GitHubClientError):
  """Raised when GitHub API rate limits are exceeded (403/429)."""


def is_bot_comment(comment: Optional[Mapping[str, Any]]) -> bool:
  """Returns True if `comment` was authored by a GitHub bot."""
  if not isinstance(comment, Mapping):
    return False
  user = comment.get("user")
  return isinstance(user, Mapping) and user.get("type") == "Bot"


def finding_tag(finding_id: str) -> str:
  """Returns the hidden HTML marker that tags a comment for `finding_id`.

  Args:
    finding_id: Non-empty finding fingerprint or identifier without whitespace
      or HTML comment delimiter characters.

  Returns:
    HTML comment marker `<!-- codemender-finding:<finding_id> -->`.

  Raises:
    ValueError: If `finding_id` is empty or contains unsafe characters.
  """
  clean = (finding_id or "").strip()
  if (
      not clean
      or ".." in clean
      or any(c in clean for c in (" ", "-", "/", "\\", "<", ">", "\n", "\r"))
  ):
    raise ValueError(f"Invalid finding_id for tag: {finding_id!r}")
  return f"<!-- codemender-finding:{clean} -->"


def verify_tag(state: str, finding_id: str, status: str = "") -> str:
  """Returns the hidden HTML marker that tags a verification reply comment."""
  finding_tag(finding_id)
  if state not in ("in-progress", "done", "failed"):
    raise ValueError(f"Invalid verify state: {state!r}")
  clean_status = (status or "").strip()
  if clean_status and any(c in clean_status for c in (" ", "-", "<", ">", "\n", "\r")):
    raise ValueError(f"Invalid verify status: {status!r}")
  suffix = f" status:{clean_status}" if clean_status else ""
  return f"<!-- codemender-verify:{state} finding:{finding_id.strip()}{suffix} -->"


def post_or_update_sticky_comment(
    token: str, repo: str, pr_number: int, body: str
) -> Optional[str]:
  """Posts or updates the CodeMender PR comment. Never raises.

  Args:
    token: GitHub token with `pull-requests: write`.
    repo: Repository in `owner/name` form (e.g. `$GITHUB_REPOSITORY`).
    pr_number: Pull request number.
    body: Markdown body. Truncated to GitHub's limit if needed.

  Returns:
    The comment's HTML URL, or None if posting failed (failure is logged).
  """
  try:
    return _post_or_update_sticky_comment_api(token, repo, pr_number, body)
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.warning("Failed to post CodeMender comment on PR #%d: %s", pr_number, e)
    return None


def delete_review_comment(repo: str, comment_id: int, token: str) -> bool:
  """Deletes a single PR review comment (`DELETE /repos/{repo}/pulls/comments/{comment_id}`).

  Args:
    repo: Repository in `owner/name` form.
    comment_id: Positive review comment ID to delete.
    token: GitHub token with `pull-requests: write`.

  Returns:
    `True` if the comment was deleted or already absent (`404`), `False` otherwise.
  """
  if not _positive_id(comment_id):
    return False
  return _delete(
      f"{GITHUB_API_URL}/repos/{repo}/pulls/comments/{comment_id}",
      token,
      f"review comment {comment_id} in {repo}",
  )


def delete_codemender_review_comments(
    repo: str, pr_number: int, token: str
) -> Optional[int]:
  """Deletes all existing bot-authored CodeMender review comments on the PR.

  Identifies bot-authored review comments carrying a finding tag
  (`<!-- codemender-finding:... -->`), a verification tag
  (`<!-- codemender-verify:... -->`), or replying to a root CodeMender review
  comment (such as a same-lines overlap note), and deletes replies before root
  comments.

  Args:
    repo: Repository in `owner/name` form.
    pr_number: Pull request number.
    token: GitHub token with `pull-requests: write`.

  Returns:
    Number of review comments deleted, or `None` if listing or any deletion
    failed.
  """
  comments = _get_pages(
      f"{GITHUB_API_URL}/repos/{repo}/pulls/{pr_number}/comments?per_page=100",
      token,
      f"the review comments of PR #{pr_number}",
  )
  if comments is None:
    return None

  root_cm_ids: Set[int] = set()
  for comment in comments:
    if not is_bot_comment(comment):
      continue
    cid = _positive_id(comment.get("id"))
    body = str(comment.get("body") or "")
    if cid and (FINDING_TAG_RE.search(body) or VERIFY_TAG_RE.search(body)):
      root_cm_ids.add(cid)

  reply_ids: List[int] = []
  root_ids: List[int] = []
  for comment in comments:
    if not is_bot_comment(comment):
      continue
    cid = _positive_id(comment.get("id"))
    if not cid:
      continue
    body = str(comment.get("body") or "")
    parent_id = _positive_id(comment.get("in_reply_to_id"))
    is_cm = bool(
        FINDING_TAG_RE.search(body)
        or VERIFY_TAG_RE.search(body)
        or (parent_id and parent_id in root_cm_ids)
    )
    if not is_cm:
      continue
    if parent_id:
      reply_ids.append(cid)
    else:
      root_ids.append(cid)

  deleted = 0
  all_ok = True
  for cid in (*reply_ids, *root_ids):
    if delete_review_comment(repo, cid, token):
      deleted += 1
    else:
      all_ok = False
  return deleted if all_ok else None


def get_pr_diff_line_ranges(
    repo: str, pr_number: int, token: str
) -> Optional[Dict[str, Set[int]]]:
  """Maps each file in the PR diff to the set of 1-based RIGHT-side line numbers.

  Args:
    repo: Repository in `owner/name` form.
    pr_number: Pull request number.
    token: GitHub token with `pull-requests: read`.

  Returns:
    Mapping of repo-relative file path to 1-based RIGHT-side line numbers in the
    PR diff hunks, or `None` if the API request failed.
  """
  files = _get_pages(
      f"{GITHUB_API_URL}/repos/{repo}/pulls/{pr_number}/files?per_page=100",
      token,
      f"the files of PR #{pr_number}",
  )
  if files is None:
    return None
  ranges: Dict[str, Set[int]] = {}
  for entry in files:
    if not isinstance(entry, dict) or not entry.get("filename"):
      continue
    lines: Set[int] = ranges.setdefault(str(entry["filename"]), set())
    for line in str(entry.get("patch") or "").splitlines():
      match = _PR_HUNK_HEADER_RE.match(line)
      if not match:
        continue
      start = int(match.group(1))
      count = int(match.group(2)) if match.group(2) is not None else 1
      if count > 0:
        lines.update(range(start, start + count))
  return ranges


def create_review_with_suggestions(
    repo: str,
    pr_number: int,
    token: str,
    head_sha: str,
    hunks: Sequence[Any],
    header: str,
    tag: str,
    *,
    notes: Sequence[str] = (),
) -> ReviewResult:
  """Posts a fix as a PR review containing one-click suggestion comments.

  Args:
    repo: Repository in `owner/name` form.
    pr_number: Pull request number.
    token: GitHub token with `pull-requests: write`.
    head_sha: PR head commit SHA (`commit_id`).
    hunks: Sequence of `SuggestionHunk` objects to render as suggestion blocks.
    header: Finding summary header line.
    tag: Hidden HTML finding marker (`finding_tag(finding_id)`).
    notes: Optional note lines to include above each suggestion block.

  Returns:
    `ReviewResult` with the review URL, HTTP status, and first review comment ID.
  """
  hunks = list(hunks or ())
  if not hunks:
    return ReviewResult(ok=False)

  note_lines = [_sanitize_line(n) for n in (notes or ()) if _sanitize_line(n)]
  comments: List[Dict[str, Any]] = []
  for number, hunk in enumerate(hunks, start=1):
    paragraphs: List[str] = []
    if len(hunks) > 1:
      everything = "both parts" if len(hunks) == 2 else f"all {len(hunks)} parts"
      paragraphs.append(
          f"Part {number} of {len(hunks)}. To commit {everything} at once,"
          " click *Add suggestion to batch* on each part, then *Commit"
          " suggestions*."
      )
    paragraphs.extend(note_lines)
    replacement_lines = getattr(hunk, "replacement_lines", None)
    if replacement_lines is not None:
      rep_text = "\n".join(replacement_lines)
    else:
      rep_text = getattr(hunk, "replacement", "").rstrip("\n")
    paragraphs.append(_suggestion_block(rep_text))

    body = _join_blocks(tag, header, paragraphs)
    if len(body) > MAX_COMMENT_BODY_CHARS:
      return ReviewResult(ok=False)

    comment: Dict[str, Any] = {
        "path": hunk.path,
        "line": hunk.end_line,
        "side": "RIGHT",
        "body": body,
    }
    if hunk.start_line < hunk.end_line:
      comment["start_line"] = hunk.start_line
      comment["start_side"] = "RIGHT"
    comments.append(comment)

  payload: Dict[str, Any] = {
      "event": "COMMENT",
      "body": "",
      "comments": comments,
  }
  if head_sha:
    payload["commit_id"] = head_sha

  result, response = _post(
      f"{GITHUB_API_URL}/repos/{repo}/pulls/{pr_number}/reviews",
      token,
      payload,
      f"a review of {len(hunks)} suggestion(s) on PR #{pr_number}",
  )
  if not result.ok:
    return result
  review_id = _positive_id(response.get("id"))
  return dataclasses.replace(
      result,
      comment_id=_first_review_comment_id(repo, pr_number, token, review_id),
  )


def post_patch_comment(
    repo: str,
    pr_number: int,
    token: str,
    head_sha: str,
    path: str,
    line: int,
    body: str,
) -> ReviewResult:
  """Posts a fallback patch comment on `path:line`, falling back to file-level on 422.

  Args:
    repo: Repository in `owner/name` form.
    pr_number: Pull request number.
    token: GitHub token with `pull-requests: write`.
    head_sha: PR head commit SHA (`commit_id`).
    path: Repo-relative file path to attach the review comment to.
    line: 1-based RIGHT-side line number (or `<= 0` to post directly on the file).
    body: Markdown comment body.

  Returns:
    `ReviewResult` with the posted comment's URL, status, and comment ID.
  """
  url = f"{GITHUB_API_URL}/repos/{repo}/pulls/{pr_number}/comments"
  base: Dict[str, Any] = {"body": truncate_comment_body(body), "path": path}
  if head_sha:
    base["commit_id"] = head_sha
  if line > 0:
    result, response = _post(
        url,
        token,
        {**base, "line": line, "side": "RIGHT"},
        f"a patch comment on {path}:{line} of PR #{pr_number}",
    )
    if result.ok or result.status != 422:
      return _with_comment_id(result, response)
  result, response = _post(
      url,
      token,
      {**base, "subject_type": "file"},
      f"a file-level patch comment on {path} of PR #{pr_number}",
  )
  return _with_comment_id(result, response)


def reply_to_comment(
    repo: str, pr_number: int, token: str, comment_id: int, body: str
) -> ReviewResult:
  """Replies to a review comment thread.

  Args:
    repo: Repository in `owner/name` form.
    pr_number: Pull request number.
    token: GitHub token with `pull-requests: write`.
    comment_id: ID of the top-level review comment to reply to.
    body: Markdown reply body.

  Returns:
    `ReviewResult` indicating whether the reply succeeded.
  """
  if not _positive_id(comment_id):
    return ReviewResult(ok=False)
  result, response = _post(
      f"{GITHUB_API_URL}/repos/{repo}/pulls/{pr_number}/comments/{comment_id}/replies",
      token,
      {"body": truncate_comment_body(body)},
      f"a reply to review comment {comment_id} on PR #{pr_number}",
  )
  return _with_comment_id(result, response)


def update_review_comment(
    repo: str, comment_id: int, token: str, body: str
) -> ReviewResult:
  """Updates an existing PR review comment in place (`PATCH`)."""
  if not _positive_id(comment_id):
    return ReviewResult(ok=False)
  result, response = _post(
      f"{GITHUB_API_URL}/repos/{repo}/pulls/comments/{comment_id}",
      token,
      {"body": truncate_comment_body(body)},
      f"an update to review comment {comment_id}",
      method="PATCH",
  )
  return _with_comment_id(result, response)


def react_to_review_comment(
    repo: str, comment_id: int, token: str, content: str = "eyes"
) -> bool:
  """Adds a reaction (default: `eyes`) to a PR review comment."""
  if not _positive_id(comment_id):
    return False
  result, _ = _post(
      f"{GITHUB_API_URL}/repos/{repo}/pulls/comments/{comment_id}/reactions",
      token,
      {"content": content},
      f"a {content!r} reaction to review comment {comment_id}",
  )
  return result.ok


def get_review_comment(
    repo: str, comment_id: int, token: str
) -> Optional[Dict[str, Any]]:
  """Fetches a single PR review comment by ID, or `None` if unavailable."""
  if not _positive_id(comment_id):
    return None
  data = _get_object(
      f"{GITHUB_API_URL}/repos/{repo}/pulls/comments/{comment_id}",
      token,
      f"review comment {comment_id}",
  )
  return data if isinstance(data, dict) else None


def list_verify_comments(
    repo: str, pr_number: int, token: str
) -> Optional[List[VerifyComment]]:
  """Lists existing bot verification comments on the PR."""
  comments = _get_pages(
      f"{GITHUB_API_URL}/repos/{repo}/pulls/{pr_number}/comments?per_page=100",
      token,
      f"the review comments of PR #{pr_number}",
  )
  if comments is None:
    return None
  found: List[VerifyComment] = []
  for comment in comments:
    if not is_bot_comment(comment):
      continue
    body = str(comment.get("body") or "")
    match = VERIFY_TAG_RE.search(body)
    if not match:
      continue
    found.append(
        VerifyComment(
            finding_id=match.group(2),
            state=match.group(1),
            status=match.group(3) or "",
            url=str(comment.get("html_url") or ""),
            updated_at=str(
                comment.get("updated_at") or comment.get("created_at") or ""
            ),
            comment_id=_positive_id(comment.get("id")),
        )
    )
  return found


def has_other_active_verify_runs(
    repo: str, token: str, current_run_id: str
) -> bool:
  """Returns True if another `pull_request_review_comment` workflow run is active."""
  data = _get_object(
      f"{GITHUB_API_URL}/repos/{repo}/actions/runs"
      "?event=pull_request_review_comment&per_page=20",
      token,
      f"active verify workflow runs for {repo}",
  )
  runs = (data or {}).get("workflow_runs") if isinstance(data, dict) else None
  if not isinstance(runs, list):
    return True
  return any(
      isinstance(r, dict)
      and str(r.get("id") or "") != str(current_run_id or "")
      and r.get("status")
      in ("in_progress", "queued", "waiting", "pending", "requested")
      for r in runs
  )


def get_sticky_comment_body(repo: str, pr_number: int, token: str) -> str:
  """Returns the body of CodeMender's sticky summary comment on the PR, or ''."""
  comments = _get_pages(
      f"{GITHUB_API_URL}/repos/{repo}/issues/{pr_number}/comments?per_page=100",
      token,
      f"the issue comments of PR #{pr_number}",
  )
  for comment in comments or ():
    if is_bot_comment(comment) and STICKY_SUMMARY_MARKER in str(
        comment.get("body") or ""
    ):
      return str(comment.get("body") or "")
  return ""


def _check_client_response(resp: requests.Response, context: str) -> None:
  if resp.status_code == 401:
    raise GitHubAuthError(
        f"GitHub API authentication failed for {context}: {resp.text}"
    )
  is_rate_limit = resp.status_code == 429 or (
      resp.status_code == 403
      and (
          (resp.headers or {}).get("x-ratelimit-remaining") == "0"
          or any(
              m in (resp.text or "").lower()
              for m in ("rate limit", "abuse detection")
          )
      )
  )
  if is_rate_limit:
    raise GitHubRateLimitError(
        f"GitHub API rate limit exceeded for {context}: {resp.text}"
    )
  if resp.status_code == 404:
    return
  if resp.status_code >= 400:
    raise GitHubClientError(
        f"GitHub API error {resp.status_code} for {context}: {resp.text}"
    )


def _fetch_finding_sarif_with_session(
    sess: Any,
    repo: str,
    pr_number: int,
    token: str,
    finding_id: str,
    *,
    api_url: str = GITHUB_API_URL,
) -> Optional[Dict[str, Any]]:
  base_url = api_url.rstrip("/")
  headers = _headers(token)
  url = (
      f"{base_url}/repos/{repo}/actions/artifacts"
      f"?name=codemender-report-pr-{pr_number}&per_page=30"
  )
  try:
    resp = sess.get(url, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS)
  except requests.RequestException as e:
    raise GitHubClientError(
        f"Failed to query GitHub Actions artifacts for PR #{pr_number}: {e}"
    ) from e

  _check_client_response(resp, f"artifacts for PR #{pr_number}")
  if resp.status_code == 404:
    return None

  try:
    data = resp.json()
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.warning(
        "Invalid JSON response querying artifacts for PR #%d: %s",
        pr_number,
        e,
    )
    return None

  artifacts = data.get("artifacts") if isinstance(data, dict) else None
  if not isinstance(artifacts, list):
    return None

  candidates = [
      a
      for a in artifacts
      if isinstance(a, dict)
      and not a.get("expired")
      and _positive_id(a.get("id"))
  ]
  candidates.sort(key=lambda a: int(a["id"]), reverse=True)

  for artifact in candidates:
    art_id = artifact["id"]
    zip_url = f"{base_url}/repos/{repo}/actions/artifacts/{art_id}/zip"
    try:
      zip_resp = sess.get(
          zip_url, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS
      )
    except requests.RequestException as e:
      logger.warning("Failed to download artifact %s: %s", art_id, e)
      continue

    _check_client_response(zip_resp, f"artifact {art_id} zip")
    if zip_resp.status_code == 404:
      continue
    if not zip_resp.content:
      continue

    try:
      with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
        sarif_names = sorted(
            (n for n in zf.namelist() if n.endswith(".sarif")),
            key=lambda n: (0 if n.startswith("findings/") else 1, n),
        )
        for name in sarif_names:
          info = zf.getinfo(name)
          if info.file_size > _MAX_ARTIFACT_FILE_BYTES:
            continue
          doc = SarifDocument.from_json(
              zf.read(name).decode("utf-8", errors="replace")
          )
          for finding in doc.to_findings():
            if finding.finding_id == finding_id and finding.document:
              return finding.document
    except Exception as e:  # pylint: disable=broad-exception-caught
      logger.warning("Could not read artifact %s zip: %s", art_id, e)
  return None


def fetch_finding_sarif_from_artifact(
    repo: str,
    pr_number: int,
    token: str,
    finding_id: str,
    *,
    api_url: str = GITHUB_API_URL,
    session: Optional[requests.Session] = None,
) -> Optional[Dict[str, Any]]:
  """Downloads the PR's scan artifact and returns 1-result SARIF for finding.
  """
  if session is not None:
    return _fetch_finding_sarif_with_session(
        session, repo, pr_number, token, finding_id, api_url=api_url
    )
  with requests.Session() as s:
    return _fetch_finding_sarif_with_session(
        s, repo, pr_number, token, finding_id, api_url=api_url
    )


def format_patch_comment(
    *,
    tag: str,
    header: str,
    reason: str,
    patch: str,
    notes: Sequence[str] = (),
    artifacts_url: str = "",
) -> str:
  """Formats the body of a fallback patch comment with a collapsible diff.

  Args:
    tag: Hidden HTML finding marker (`finding_tag(finding_id)`).
    header: Finding summary header line.
    reason: One-sentence explanation of why the fix is not a suggestion.
    patch: Unified diff string.
    notes: Optional note lines to include above the collapsible diff.
    artifacts_url: Optional URL to the workflow run's artifacts.

  Returns:
    Formatted Markdown comment body within `MAX_COMMENT_BODY_CHARS`.
  """
  patch = "" if patch is None else str(patch)
  paragraphs = [_sanitize_line(reason)] + [
      _sanitize_line(n) for n in (notes or ())
  ]
  intro = _join_blocks(tag, header, [p for p in paragraphs if p])
  files, added, deleted = _patch_stats(patch)
  noun = "file" if files == 1 else "files"
  summary = (
      f"<details><summary>Show the fix ({files} {noun}, +{added}"
      f" \u2212{deleted})</summary>"
  )
  link = (
      f"[workflow run's artifacts]({artifacts_url})"
      if artifacts_url.startswith(("http://", "https://"))
      else "workflow run's artifacts"
  )
  apply_steps = (
      "To apply it, save the diff above as `fix.diff` at the root of a"
      " checkout of this PR's branch, then run `git apply fix.diff`."
  )
  if "\r" in patch:
    apply_steps += (
        " It has Windows line endings, which copying from this page loses, so"
        f" if `git apply` rejects it, use the patch in the {link}."
    )
  cut_note = (
      "This fix is too long for a comment, so only its start is shown. To"
      f" apply it, download the full patch from the {link}, save it as"
      " `fix.diff` at the root of a checkout of this PR's branch, then run"
      " `git apply fix.diff`."
  )

  def _render(shown: str, cut: bool) -> str:
    fence = code_fence(shown)
    newline = "" if not shown or shown.endswith("\n") else "\n"
    diff_block = f"{fence}diff\n{shown}{newline}{fence}"
    details = [cut_note, diff_block] if cut else [diff_block, apply_steps]
    return "\n\n".join(b for b in [intro, summary, *details, "</details>"] if b)

  body = _render(patch, cut=False)
  if len(body) <= MAX_COMMENT_BODY_CHARS:
    return body
  shown = patch
  for _ in range(5):
    body = _render(shown, cut=True)
    excess = len(body) - MAX_COMMENT_BODY_CHARS
    if excess <= 0:
      return body
    keep = max(len(shown) - excess, 0)
    last_nl = shown.rfind("\n", 0, keep)
    shown = shown[: last_nl + 1] if last_nl >= 0 else shown[:keep]
  return truncate_comment_body(body)


def code_fence(text: str) -> str:
  """Returns a backtick fence wider than any backtick run in `text`."""
  longest = max((len(run) for run in _BACKTICK_RUN_RE.findall(text)), default=0)
  return "`" * max(3, longest + 1)


def truncate_comment_body(body: str, reserved: int = 0) -> str:
  """Trims a comment body to GitHub's maximum length, appending a notice.

  Runner output is untrusted and unbounded, so every body is passed through
  here before posting. Truncation happens on a line boundary and any code
  fence left open by the cut is closed so the rest of the comment renders.

  Args:
    body: Markdown comment body to truncate if needed.
    reserved: Characters to reserve for outer prefixes (such as a sticky marker).

  Returns:
    Truncated Markdown body within `MAX_COMMENT_BODY_CHARS - reserved`.
  """
  limit = MAX_COMMENT_BODY_CHARS - reserved
  if len(body) <= limit:
    return body
  head = body[: limit - len(_TRUNCATION_NOTICE) - _TRUNCATION_SLACK]
  last_newline = head.rfind("\n")
  if last_newline > 0:
    head = head[:last_newline]
  result = _terminate_open_fence(head) + _TRUNCATION_NOTICE
  return result if len(result) <= limit else result[:limit]


# ---------------------------------------------------------------------------
# Internal HTTP & Formatting Helpers
# ---------------------------------------------------------------------------


def retry_on_exception(
    max_tries: int = 3,
    initial_delay: float = 2.0,
    backoff_factor: float = 2.0,
) -> Callable:
  """Retries a function with exponential backoff when an exception occurs."""

  def decorator(func: Callable) -> Callable:
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
      delay = initial_delay
      for attempt in range(1, max_tries + 1):
        try:
          return func(*args, **kwargs)
        except Exception as e:  # pylint: disable=broad-exception-caught
          if attempt == max_tries:
            raise
          logger.warning(
              "Attempt %d failed for %s: %s. Retrying in %s seconds...",
              attempt,
              func.__name__,
              e,
              delay,
          )
          time.sleep(delay)
          delay *= backoff_factor

    return wrapper

  return decorator


class _RetryableHTTPError(Exception):

  def __init__(self, status: int, message: str):
    super().__init__(f"HTTP {status}: {message}")
    self.status = status


def _headers(token: str) -> Dict[str, str]:
  return {
      "Authorization": f"Bearer {token}",
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
  }


def _raise_if_retryable(resp: Any) -> None:
  status = resp.status_code
  if status >= 500 or status == 429:
    raise _RetryableHTTPError(status, str(getattr(resp, "text", ""))[:300])
  if status == 403:
    headers = getattr(resp, "headers", None) or {}
    text = str(getattr(resp, "text", ""))
    if headers.get("x-ratelimit-remaining") == "0" or "rate limit" in text.lower():
      raise _RetryableHTTPError(status, text[:300])


@retry_on_exception(max_tries=3, initial_delay=3, backoff_factor=2)
def _post_with_retries(
    url: str, token: str, payload: Dict[str, Any], method: str = "POST"
) -> Any:
  with requests.Session() as session:
    fn = session.patch if method == "PATCH" else session.post
    resp = fn(
        url, headers=_headers(token), json=payload, timeout=_POST_TIMEOUT_SECONDS
    )
  _raise_if_retryable(resp)
  return resp


def _post(
    url: str,
    token: str,
    payload: Dict[str, Any],
    what: str,
    method: str = "POST",
) -> Tuple[ReviewResult, Dict[str, Any]]:
  try:
    resp = _post_with_retries(url, token, payload, method=method)
  except _RetryableHTTPError as e:
    logger.warning("Failed to post %s: %s", what, e)
    return ReviewResult(ok=False, status=e.status), {}
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.warning("Failed to post %s: %s", what, e)
    return ReviewResult(ok=False), {}
  status = resp.status_code
  if not 200 <= status < 300:
    logger.warning("Failed to post %s: HTTP %d", what, status)
    return ReviewResult(ok=False, status=status), {}
  try:
    data = resp.json()
  except Exception:  # pylint: disable=broad-exception-caught
    data = {}
  if not isinstance(data, dict):
    data = {}
  return ReviewResult(ok=True, url=str(data.get("html_url") or ""), status=status), data


@retry_on_exception(max_tries=3, initial_delay=2, backoff_factor=2)
def _get_with_retries(
    url: str, token: str, session: Optional[requests.Session] = None
) -> Any:
  if session is not None:
    resp = session.get(
        url, headers=_headers(token), timeout=_REQUEST_TIMEOUT_SECONDS
    )
  else:
    with requests.Session() as s:
      resp = s.get(
          url, headers=_headers(token), timeout=_REQUEST_TIMEOUT_SECONDS
      )
  _raise_if_retryable(resp)
  return resp


def _get_object(
    url: str,
    token: str,
    what: str,
    session: Optional[requests.Session] = None,
) -> Optional[Any]:
  try:
    resp = _get_with_retries(url, token, session=session)
    return resp.json() if 200 <= resp.status_code < 300 else None
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.warning("Failed to get %s: %s", what, e)
    return None


def _get_bytes(
    url: str,
    token: str,
    what: str,
    session: Optional[requests.Session] = None,
) -> Optional[bytes]:
  try:
    resp = _get_with_retries(url, token, session=session)
    return bytes(resp.content) if 200 <= resp.status_code < 300 else None
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.warning("Failed to download %s: %s", what, e)
    return None


@retry_on_exception(max_tries=3, initial_delay=2, backoff_factor=2)
def _get_pages_with_retries(url: str, token: str) -> Optional[List[Any]]:
  items: List[Any] = []
  next_url: Optional[str] = url
  with requests.Session() as session:
    for _ in range(_MAX_PAGES):
      if not next_url:
        return items
      resp = session.get(
          next_url, headers=_headers(token), timeout=_REQUEST_TIMEOUT_SECONDS
      )
      _raise_if_retryable(resp)
      if not 200 <= resp.status_code < 300:
        return None
      page = resp.json()
      if not isinstance(page, list):
        return None
      items.extend(page)
      next_url = ((resp.links or {}).get("next") or {}).get("url")
  return None


def _get_pages(url: str, token: str, what: str) -> Optional[List[Any]]:
  try:
    return _get_pages_with_retries(url, token)
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.warning("Failed to get %s: %s", what, e)
    return None


@retry_on_exception(max_tries=3, initial_delay=3, backoff_factor=2)
def _post_or_update_sticky_comment_api(
    token: str, repo: str, pr_number: int, body: str
) -> Optional[str]:
  marked_body = f"{STICKY_SUMMARY_MARKER}\n" + truncate_comment_body(
      body, reserved=len(STICKY_SUMMARY_MARKER) + 1
  )
  with requests.Session() as session:
    existing_id = _find_sticky_comment_id(session, token, repo, pr_number)
    if existing_id is not None:
      resp = session.patch(
          f"{GITHUB_API_URL}/repos/{repo}/issues/comments/{existing_id}",
          headers=_headers(token),
          json={"body": marked_body},
          timeout=_REQUEST_TIMEOUT_SECONDS,
      )
    else:
      resp = session.post(
          f"{GITHUB_API_URL}/repos/{repo}/issues/{pr_number}/comments",
          headers=_headers(token),
          json={"body": marked_body},
          timeout=_REQUEST_TIMEOUT_SECONDS,
      )
    resp.raise_for_status()
  return resp.json().get("html_url")


def _find_sticky_comment_id(
    session: requests.Session, token: str, repo: str, pr_number: int
) -> Optional[int]:
  url: Optional[str] = (
      f"{GITHUB_API_URL}/repos/{repo}/issues/{pr_number}/comments?per_page=100"
  )
  while url:
    resp = session.get(
        url, headers=_headers(token), timeout=_REQUEST_TIMEOUT_SECONDS
    )
    resp.raise_for_status()
    comments = resp.json()
    if not isinstance(comments, list):
      return None
    for comment in comments:
      if is_bot_comment(comment) and STICKY_SUMMARY_MARKER in (
          comment.get("body") or ""
      ):
        return comment.get("id")
    url = resp.links.get("next", {}).get("url")
  return None


def _first_review_comment_id(
    repo: str, pr_number: int, token: str, review_id: int
) -> int:
  if not review_id:
    return 0
  comments = _get_pages(
      f"{GITHUB_API_URL}/repos/{repo}/pulls/{pr_number}/reviews/{review_id}/comments?per_page=100",
      token,
      f"review {review_id} comments on PR #{pr_number}",
  )
  if not comments:
    return 0
  ids = [
      _positive_id(c.get("id")) for c in comments if isinstance(c, dict)
  ]
  valid = [cid for cid in ids if cid]
  return min(valid) if valid else 0


def _positive_id(value: Any) -> int:
  return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _with_comment_id(
    result: ReviewResult, response: Dict[str, Any]
) -> ReviewResult:
  if not result.ok:
    return result
  return dataclasses.replace(result, comment_id=_positive_id(response.get("id")))


def _sanitize_line(value: Any, limit: int = 1000) -> str:
  text = " ".join(("" if value is None else str(value)).split())
  if len(text) > limit:
    text = text[: limit - 1] + "…"
  return text


def _join_blocks(tag: str, header: str, paragraphs: Sequence[str]) -> str:
  top = "\n".join(
      line for line in ((tag or "").strip(), _sanitize_line(header)) if line
  )
  return "\n\n".join(b for b in (top, *paragraphs) if b)


def _suggestion_block(replacement: str) -> str:
  if replacement and not replacement.endswith("\n"):
    replacement += "\n"
  fence = code_fence(replacement)
  return f"{fence}suggestion\n{replacement}{fence}"


def _terminate_open_fence(text: str) -> str:
  open_delim = ""
  for line in text.split("\n"):
    match = _MD_FENCE_RE.match(line)
    if not match:
      continue
    delim, info = match.group(1), match.group(2)
    if not open_delim:
      open_delim = delim
    elif (
        delim[0] == open_delim[0]
        and len(delim) >= len(open_delim)
        and not info.strip()
    ):
      open_delim = ""
  return f"{text}\n{open_delim}" if open_delim else text


@retry_on_exception(max_tries=3, initial_delay=3, backoff_factor=2)
def _delete_with_retries(url: str, token: str) -> Any:
  with requests.Session() as session:
    resp = session.delete(
        url, headers=_headers(token), timeout=_POST_TIMEOUT_SECONDS
    )
  _raise_if_retryable(resp)
  return resp


def _delete(url: str, token: str, what: str) -> bool:
  try:
    resp = _delete_with_retries(url, token)
  except Exception as e:  # pylint: disable=broad-exception-caught
    logger.warning("Failed to delete %s: %s", what, e)
    return False
  status = resp.status_code
  if 200 <= status < 300 or status == 404:
    return True
  logger.warning("Failed to delete %s: HTTP %d", what, status)
  return False


def _patch_stats(patch: str) -> Tuple[int, int, int]:
  files = added = deleted = 0
  in_hunk = False
  for line in (patch or "").splitlines():
    if line.startswith("diff --git "):
      files += 1
      in_hunk = False
    elif line.startswith("@@ "):
      in_hunk = True
    elif in_hunk:
      if line.startswith("+"):
        added += 1
      elif line.startswith("-"):
        deleted += 1
  return files, added, deleted
