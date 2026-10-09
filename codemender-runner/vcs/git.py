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

"""Git URL and repository manipulation utilities for CodeMender Agent."""

import base64
import logging
import os
import re
import subprocess
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger("codemender-orchestrator")


def enforce_https_url(url: str) -> str:
  """Converts SSH git URLs to HTTPS format to support token-based authentication."""
  url = url.strip()
  if url.startswith("git@") or url.startswith("ssh://"):
    # Match git@host:owner/repo.git or ssh://git@host/owner/repo.git
    match = re.search(r"(?:ssh://)?git@([^:/]+)[:/](.+)$", url)
    if match:
      host = match.group(1)
      path = match.group(2)
      return f"https://{host}/{path}"
  return url


def sanitize_git_url(url: str) -> str:
  """Removes embedded credentials and query parameters from Git URLs."""
  # Enforce HTTPS format
  cleaned = enforce_https_url(url)
  # Strip username/password credentials
  cleaned = re.sub(r"(https?://)[^@]+@", r"\1", cleaned)
  # Strip query parameters or fragment identifiers (?branch=main or #readme)
  cleaned = cleaned.split("?")[0].split("#")[0].strip()
  return cleaned


def get_git_auth_header(token: str) -> str:
  """Generates a Basic authentication header value for GitHub Git transactions."""
  auth_str = f"x-access-token:{token}"
  auth_b64 = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
  return f"http.extraheader=AUTHORIZATION: Basic {auth_b64}"


def parse_repo_owner_and_name(repo_url: str) -> Tuple[str, str]:
  """Extracts (owner, repo_name) from a GitHub repository URL."""
  clean_url = sanitize_git_url(repo_url).rstrip("/")
  if clean_url.endswith(".git"):
    clean_url = clean_url[:-4]

  match = re.search(r"[:/]([^/]+)/([^/]+)$", clean_url)
  if match:
    return match.group(1), match.group(2)
  raise ValueError(f"Could not parse owner and repo name from URL: {repo_url}")


def is_git_repository(repo_dir: str) -> bool:
  """Verifies whether repo_dir is inside a valid Git working tree."""
  if not repo_dir or not os.path.isdir(repo_dir):
    return False

  try:
    res = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_dir,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return res.returncode == 0 and (res.stdout or "").strip() == "true"
  except (OSError, subprocess.SubprocessError):
    return False


def ref_exists(repo_dir: str, ref: str) -> bool:
  """Verifies that ref is a valid, existing Git commit reference in repo_dir."""
  if not repo_dir or not os.path.isdir(repo_dir):
    return False
  clean_ref = (ref or "").strip()
  if not clean_ref or clean_ref.startswith("-"):
    return False

  try:
    res = subprocess.run(
        [
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{clean_ref}^{{commit}}",
        ],
        cwd=repo_dir,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return res.returncode == 0
  except (OSError, subprocess.SubprocessError):
    return False


# How long the working-tree helpers below wait for one Git call.
_WORKTREE_GIT_TIMEOUT_SEC = 120


def _pathspecs_excluding(exclude: Sequence[str]) -> List[str]:
  """Returns pathspecs for everything under the current folder except `exclude`."""
  return ["--", ".", *(f":(exclude){path}" for path in exclude)]


def _run_git_for_bytes(repo_dir: str, args: Sequence[str]) -> Optional[bytes]:
  """Runs one Git command and returns its stdout exactly as Git wrote it.

  stderr is captured separately, so a Git warning can never end up in the
  returned bytes.

  Args:
    repo_dir: Folder to run Git in.
    args: Arguments after `git`.

  Returns:
    stdout as bytes, or None (with the reason logged) if Git could not be run,
    timed out, or exited non-zero.
  """
  try:
    res = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=_WORKTREE_GIT_TIMEOUT_SEC,
        check=False,
    )
  except (OSError, subprocess.SubprocessError) as exc:
    logger.error("Could not run 'git %s' in %s: %s", args[0], repo_dir, exc)
    return None
  stderr = res.stderr.decode("utf-8", errors="replace").strip()
  if res.returncode != 0:
    logger.error(
        "'git %s' in %s exited %d: %s", args[0], repo_dir, res.returncode, stderr
    )
    return None
  if stderr:
    logger.debug("'git %s' stderr: %s", args[0], stderr)
  return res.stdout


def list_uncommitted_paths(
    repo_dir: str, exclude: Sequence[str] = ()
) -> Optional[List[str]]:
  """Lists modified, staged and untracked files under `repo_dir`.

  Files ignored by `.gitignore` are not listed. Nor is anything in `exclude`
  (paths relative to `repo_dir`) or beneath it.

  Args:
    repo_dir: A folder inside a Git working tree.
    exclude: Paths to leave out, such as files another tool writes.

  Returns:
    The paths, as `git status` reports them, in Git's order. Empty when the
    working tree is clean. None if Git failed.
  """
  out = _run_git_for_bytes(
      repo_dir,
      [
          "status",
          "--porcelain=v1",
          "-z",
          "--untracked-files=all",
          *_pathspecs_excluding(exclude),
      ],
  )
  if out is None:
    return None
  entries = out.decode("utf-8", errors="replace").split("\0")
  paths = []
  index = 0
  while index < len(entries):
    entry = entries[index]
    index += 1
    if len(entry) < 4:
      continue
    paths.append(entry[3:])
    # With -z, a rename or copy entry is followed by the original path.
    if any(code in "RC" for code in entry[:2]):
      index += 1
  return paths


def diff_working_tree(
    repo_dir: str, exclude: Sequence[str] = ()
) -> Optional[bytes]:
  """Returns every change under `repo_dir` since HEAD, as a patch.

  Untracked files are first marked intent-to-add (`git add -N`) so that files
  created since HEAD appear in the patch. This records only their names in the
  index, not their contents. Ignored files, and anything in `exclude`, are left
  out.

  The patch is returned as bytes, exactly as Git wrote it, so Windows line
  endings and binary changes survive and `git apply` accepts it. Colour,
  prefixes, external diff tools and text conversion are set explicitly, so a
  caller's Git configuration cannot change the format.

  Args:
    repo_dir: A folder inside a Git working tree.
    exclude: Paths to leave out, such as files another tool writes.

  Returns:
    The patch, empty when nothing changed, or None if Git failed.
  """
  pathspecs = _pathspecs_excluding(exclude)
  if _run_git_for_bytes(repo_dir, ["add", "--intent-to-add", *pathspecs]) is None:
    return None
  return _run_git_for_bytes(
      repo_dir,
      [
          "diff",
          "--binary",
          "--no-color",
          "--no-ext-diff",
          "--no-textconv",
          "--no-relative",
          "--src-prefix=a/",
          "--dst-prefix=b/",
          "HEAD",
          *pathspecs,
      ],
  )


def reset_working_tree(
    repo_dir: str, exclude: Sequence[str] = ()
) -> bool:
  """Resets tracked file modifications and removes untracked files outside `exclude`.

  Runs `git checkout HEAD -- .` (excluding `exclude`) and `git clean -fd`
  (excluding `exclude`).

  Args:
    repo_dir: A folder inside a Git working tree.
    exclude: Paths to leave untouched, such as metadata directories.

  Returns:
    True on success, False if Git failed.
  """
  pathspecs = _pathspecs_excluding(exclude)
  if _run_git_for_bytes(repo_dir, ["checkout", "HEAD", *pathspecs]) is None:
    return False

  clean_cmd = ["clean", "-fd"]
  for ex in exclude:
    clean_cmd.extend(["-e", ex])
  if _run_git_for_bytes(repo_dir, clean_cmd) is None:
    return False

  return True

