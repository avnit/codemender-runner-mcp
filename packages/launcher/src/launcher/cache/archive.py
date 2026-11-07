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

# Governing: SPEC-WORKER/REQ-0004, SPEC-FIND-DIFF/REQ-0002
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0005
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0003
"""Safe repository archive extraction and flattening helpers for ArtifactCache."""

from collections.abc import Sequence
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile


def _validate_tar_member(member: tarfile.TarInfo, abs_dest: Path) -> Path:
  """Validates a single tar member before extraction, returning its target path.

  Args:
    member: Tar member metadata to inspect.
    abs_dest: Canonical resolved path of the destination directory.

  Returns:
    Normalized target `Path` inside `abs_dest`.

  Raises:
    ValueError: If the member has an unsafe path, link target, or file type.
  """
  name = member.name
  if not isinstance(name, str) or not name or "\x00" in name:
    raise ValueError(f"Invalid tar member name: {name!r}")

  posix_name = PurePosixPath(name)
  if (
      posix_name.is_absolute()
      or name.startswith(("/", "\\"))
      or "\\" in name
  ):
    raise ValueError(f"Absolute or backslash path in tar archive: {name!r}")

  if ".." in posix_name.parts or ".." in name.split("/"):
    raise ValueError(f"Path traversal ('..') in tar archive member: {name!r}")

  target_path = abs_dest.joinpath(*posix_name.parts)
  real_target = target_path.resolve()
  if not target_path.is_relative_to(abs_dest) or not real_target.is_relative_to(
      abs_dest
  ):
    raise ValueError(
        f"Tar member {name!r} resolves outside destination directory."
    )
  if target_path == abs_dest and not member.isdir():
    raise ValueError(
        f"Tar member {name!r} targets destination root but is not a directory."
    )

  if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
    raise ValueError(
        f"Special device or FIFO file rejected in tar archive: {name!r}"
    )

  if member.issym() or member.islnk():
    linkname = member.linkname
    if not isinstance(linkname, str) or not linkname or "\x00" in linkname:
      raise ValueError(
          f"Invalid link target for tar member {name!r}: {linkname!r}"
      )
    posix_link = PurePosixPath(linkname)
    if (
        posix_link.is_absolute()
        or linkname.startswith(("/", "\\"))
        or "\\" in linkname
    ):
      raise ValueError(
          f"Absolute link target in tar member {name!r}: {linkname!r}"
      )
    link_base = target_path.parent if member.issym() else abs_dest
    real_link_target = (link_base / linkname).resolve()
    if not real_link_target.is_relative_to(abs_dest):
      raise ValueError(
          f"Link target {linkname!r} for tar member {name!r} escapes"
          " destination directory."
      )
  elif not (member.isfile() or member.isdir()):
    raise ValueError(
        f"Unsupported tar member type {member.type!r} for {name!r}."
    )

  return target_path


def _resolve_archive_path(
    base_dir: Path,
    rel_path: str,
    abs_dest: Path,
    archive_links: dict[Path, tuple[Path, str]],
    active_links: frozenset[Path] = frozenset(),
    depth: int = 0,
) -> Path:
  """Resolves `rel_path` from `base_dir` step-by-step across archive links.

  Args:
    base_dir: Starting directory inside `abs_dest` for resolving `rel_path`.
    rel_path: Relative link target path string.
    abs_dest: Canonical resolved path of the destination root directory.
    archive_links: Mapping of archive link paths to `(link_base, linkname)`.
    active_links: Set of link paths currently being resolved (for cycle checks).
    depth: Current recursion depth across chained archive links.

  Returns:
    Fully resolved target `Path` inside `abs_dest`.

  Raises:
    ValueError: If any intermediate hop escapes `abs_dest`, forms a symlink
      cycle, or exceeds the maximum link recursion depth.
  """
  if depth > 32:
    raise ValueError("Excessive symlink recursion depth in tar archive.")
  current = base_dir
  if not current.is_relative_to(abs_dest):
    raise ValueError("Link base resolves outside destination directory.")

  for part in rel_path.split("/"):
    if not part or part == ".":
      continue
    if part == "..":
      parent = current.parent
      if parent == current or not parent.is_relative_to(abs_dest):
        raise ValueError(
            f"Link target {rel_path!r} escapes destination directory."
        )
      current = parent
      continue

    current = current / part
    real_current = current.resolve()
    if not current.is_relative_to(abs_dest) or not real_current.is_relative_to(
        abs_dest
    ):
      raise ValueError(
          f"Link target {rel_path!r} escapes destination directory."
      )

    if current in archive_links:
      if current in active_links:
        raise ValueError(
            f"Symlink cycle detected in tar archive at {current}."
        )
      target_base, target_linkname = archive_links[current]
      current = _resolve_archive_path(
          target_base,
          target_linkname,
          abs_dest,
          archive_links,
          active_links | {current},
          depth + 1,
      )

  return current


def _validate_archive_members(
    members: Sequence[tarfile.TarInfo], abs_dest: Path
) -> list[tuple[tarfile.TarInfo, Path]]:
  """Validates all tar members and cross-member link topology before extraction.

  Args:
    members: Sequence of `TarInfo` entries read from the archive.
    abs_dest: Canonical resolved destination root directory.

  Returns:
    Ordered list of `(member, target_path)` pairs safe to extract.

  Raises:
    ValueError: If any member or cross-member link chain violates safety checks.
  """
  validated: list[tuple[tarfile.TarInfo, Path]] = []
  archive_links: dict[Path, tuple[Path, str]] = {}

  for member in members:
    target_path = _validate_tar_member(member, abs_dest)
    if member.issym() or member.islnk():
      if target_path in archive_links:
        raise ValueError(
            f"Duplicate link target path in tar archive: {member.name!r}"
        )
      link_base = target_path.parent if member.issym() else abs_dest
      archive_links[target_path] = (link_base, member.linkname)
    validated.append((member, target_path))

  seen_non_links: set[Path] = set()
  for member, target_path in validated:
    if not (member.issym() or member.islnk()):
      if target_path in archive_links:
        raise ValueError(
            f"Tar member {member.name!r} collides with archive link"
            f" {target_path}."
        )
      seen_non_links.add(target_path)
    elif target_path in seen_non_links:
      raise ValueError(
          f"Archive link {member.name!r} collides with member"
          f" {target_path}."
      )

    # Reject any member whose parent directory traverses a symlink or
    # hardlink anywhere in the archive (regardless of member order).
    for ancestor in target_path.parents:
      if ancestor == abs_dest or not ancestor.is_relative_to(abs_dest):
        break
      if ancestor in archive_links:
        raise ValueError(
            f"Tar member {member.name!r} traverses archive link {ancestor}."
        )

    if member.issym() or member.islnk():
      link_base, linkname = archive_links[target_path]
      _resolve_archive_path(
          link_base,
          linkname,
          abs_dest,
          archive_links,
          frozenset({target_path}),
      )

  return validated


def _extract_validated_members(
    tar: tarfile.TarFile,
    validated: Sequence[tuple[tarfile.TarInfo, Path]],
    abs_dest: Path,
) -> None:
  """Extracts pre-validated tar members and restores `0o777` permission bits.

  Args:
    tar: Open `TarFile` handle.
    validated: Pre-validated `(member, target_path)` pairs.
    abs_dest: Canonical resolved destination root directory.

  Raises:
    ValueError: If a parent path resolves outside `abs_dest` or the PEP 706
      tar filter rejects a member.
  """
  dir_modes: list[tuple[Path, int]] = []
  for member, target_path in validated:
    if target_path != abs_dest:
      parent_real = target_path.parent.resolve()
      if not parent_real.is_relative_to(abs_dest):
        raise ValueError(
            f"Tar member {member.name!r} parent resolves outside destination."
        )
    try:
      tar.extract(member, path=abs_dest, set_attrs=False, filter="tar")
    except tarfile.FilterError as exc:
      raise ValueError(
          f"Tar filter rejected member {member.name!r}: {exc}"
      ) from exc

    if member.mode:
      mode = member.mode & 0o777
      if member.isdir():
        dir_modes.append((target_path, mode))
      elif (
          member.isfile()
          and not member.issym()
          and not target_path.is_symlink()
      ):
        target_path.chmod(mode)

  # Apply directory modes in reverse depth order so restrictive directory
  # permissions do not block extracting child files during the loop.
  for dir_path, mode in sorted(
      dir_modes, key=lambda item: len(item[0].parts), reverse=True
  ):
    if dir_path.is_dir() and not dir_path.is_symlink():
      dir_path.chmod(mode)


# Governing: SPEC-WORKER/REQ-0004
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0005
# Governing: docs/openspec/specs/tech/systems/distributed-execution-and-transit/spec.md#req-0003
def unpack_tar_gz_preserving_permissions(
    archive_path: Path, dest_dir: Path
) -> None:
  """Validates and extracts a `.tar.gz` archive while preserving POSIX modes.

  Inspects all archive members before extracting any member so malicious
  archives fail atomically without writing partial files to `dest_dir`.

  Args:
    archive_path: Path to the source tar archive.
    dest_dir: Destination directory to extract into.

  Raises:
    ValueError: If any member fails tar-slip, link-escape, or file-type checks.
    tarfile.TarError: If the archive cannot be read as a valid tar archive.
  """
  archive_path = Path(archive_path)
  dest_dir = Path(dest_dir)
  dest_dir.mkdir(parents=True, exist_ok=True)
  abs_dest = dest_dir.resolve()

  with tarfile.open(archive_path, mode="r:*") as tar:
    validated = _validate_archive_members(tar.getmembers(), abs_dest)
    _extract_validated_members(tar, validated, abs_dest)


def _ensure_symlinks_within(root_dir: Path) -> None:
  """Raises `ValueError` if any symlink under `root_dir` resolves outside it."""
  abs_root = root_dir.resolve()
  stack = [abs_root]
  while stack:
    current = stack.pop()
    for path in current.iterdir():
      if path.is_symlink():
        try:
          resolved = path.resolve()
        except RuntimeError as exc:
          raise ValueError(f"Symlink loop detected at {path}.") from exc
        if not resolved.is_relative_to(abs_root):
          raise ValueError(
              f"Symlink {path} resolves outside boundary {abs_root}."
          )
      elif path.is_dir():
        stack.append(path)


# Governing: SPEC-WORKER/REQ-0004, SPEC-FIND-DIFF/REQ-0002
def flatten_repo_if_nested(repo_dir: Path) -> None:
  """Promotes a single top-level wrapper directory's contents into `repo_dir`.

  If `repo_dir / ".git"` does not exist and `repo_dir` contains exactly one
  entry which is a non-symlink directory, validates that all symlinks inside
  the wrapper resolve strictly within the wrapper boundary, then moves all
  children (including dotfiles like `.git`) directly into `repo_dir` and
  removes the wrapper.

  Args:
    repo_dir: Path to the unpacked repository directory.

  Raises:
    ValueError: If any symlink inside the nested wrapper resolves outside the
      wrapper directory or `repo_dir` after promotion.
  """
  repo_dir = Path(repo_dir)
  if not repo_dir.is_dir():
    return

  git_entry = repo_dir / ".git"
  if git_entry.exists() or git_entry.is_symlink():
    return

  entries = list(repo_dir.iterdir())
  if len(entries) != 1:
    return

  wrapper = entries[0]
  if not wrapper.is_dir() or wrapper.is_symlink():
    return

  # Rename wrapper to a temporary directory first so a child sharing the same
  # name as the wrapper directory (e.g. `project/project`) can be moved up
  # without colliding with the wrapper path itself, and so any symlink
  # referencing `../<wrapper_name>` resolves outside `temp_wrapper`.
  temp_wrapper = Path(tempfile.mkdtemp(prefix=".cm_flatten_", dir=repo_dir))
  temp_wrapper.rmdir()
  wrapper.rename(temp_wrapper)
  try:
    _ensure_symlinks_within(temp_wrapper)
  except ValueError:
    temp_wrapper.rename(wrapper)
    raise

  for child in list(temp_wrapper.iterdir()):
    shutil.move(child, repo_dir / child.name)

  temp_wrapper.rmdir()
