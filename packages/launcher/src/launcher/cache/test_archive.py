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
"""Unit tests for safe archive extraction and nested directory flattening."""

import io
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest

from launcher.cache import (
    flatten_repo_if_nested,
    unpack_tar_gz_preserving_permissions,
)


class TestUnpackTarGzPreservingPermissions(unittest.TestCase):
  """Tests for unpack_tar_gz_preserving_permissions and tar-slip defenses."""

  def _build_tar_gz(self, archive_path: Path, build_fn) -> Path:
    with tarfile.open(archive_path, "w:gz") as tf:
      build_fn(tf)
    return archive_path

  def test_extracts_valid_archive_and_preserves_permissions(self):
    with tempfile.TemporaryDirectory() as tmp:
      tmp_path = Path(tmp)
      archive = tmp_path / "code.tar.gz"
      dest = tmp_path / "repo"
      dest.mkdir()

      def _populate(tf: tarfile.TarFile):
        dir_info = tarfile.TarInfo(name="scripts")
        dir_info.type = tarfile.DIRTYPE
        dir_info.mode = 0o755
        tf.addfile(dir_info)

        script_bytes = b"#!/bin/sh\necho ok\n"
        script_info = tarfile.TarInfo(name="scripts/build.sh")
        script_info.type = tarfile.REGTYPE
        script_info.size = len(script_bytes)
        script_info.mode = 0o755
        tf.addfile(script_info, io.BytesIO(script_bytes))

        ro_bytes = b"secret=false\n"
        ro_info = tarfile.TarInfo(name="config.ini")
        ro_info.type = tarfile.REGTYPE
        ro_info.size = len(ro_bytes)
        ro_info.mode = 0o640
        tf.addfile(ro_info, io.BytesIO(ro_bytes))

        sym_info = tarfile.TarInfo(name="scripts/run.sh")
        sym_info.type = tarfile.SYMTYPE
        sym_info.linkname = "build.sh"
        tf.addfile(sym_info)

      self._build_tar_gz(archive, _populate)
      unpack_tar_gz_preserving_permissions(archive, dest)

      script_path = dest / "scripts" / "build.sh"
      self.assertTrue(script_path.is_file())
      self.assertEqual(stat.S_IMODE(script_path.stat().st_mode), 0o755)
      ro_path = dest / "config.ini"
      self.assertEqual(stat.S_IMODE(ro_path.stat().st_mode), 0o640)
      sym_path = dest / "scripts" / "run.sh"
      self.assertTrue(sym_path.is_symlink())
      self.assertEqual(sym_path.readlink(), Path("build.sh"))

  def test_extracts_dot_prefixed_archive_and_rejects_non_dir_dot(self):
    with tempfile.TemporaryDirectory() as tmp:
      tmp_path = Path(tmp)
      archive = tmp_path / "dot_repo.tar.gz"
      dest = tmp_path / "repo"

      def _populate_dot(tf: tarfile.TarFile):
        root_dir = tarfile.TarInfo(name=".")
        root_dir.type = tarfile.DIRTYPE
        root_dir.mode = 0o755
        tf.addfile(root_dir)

        file_bytes = b"hello\n"
        file_info = tarfile.TarInfo(name="./hello.txt")
        file_info.type = tarfile.REGTYPE
        file_info.size = len(file_bytes)
        file_info.mode = 0o644
        tf.addfile(file_info, io.BytesIO(file_bytes))

      self._build_tar_gz(archive, _populate_dot)
      unpack_tar_gz_preserving_permissions(archive, dest)
      self.assertEqual(
          (dest / "hello.txt").read_text(encoding="utf-8"), "hello\n"
      )

    def _add_file_dot(tf: tarfile.TarFile):
      bad_dot = tarfile.TarInfo(name=".")
      bad_dot.type = tarfile.REGTYPE
      bad_dot.size = 4
      tf.addfile(bad_dot, io.BytesIO(b"evil"))

    self._assert_rejected_without_partial_extraction(_add_file_dot)

  def _assert_rejected_without_partial_extraction(self, build_bad_member_fn):
    with tempfile.TemporaryDirectory() as tmp:
      tmp_path = Path(tmp)
      archive = tmp_path / "bad.tar.gz"
      dest = tmp_path / "repo"
      dest.mkdir()

      def _populate(tf: tarfile.TarFile):
        ok_data = b"innocent\n"
        ok_info = tarfile.TarInfo(name="innocent.txt")
        ok_info.size = len(ok_data)
        ok_info.mode = 0o644
        tf.addfile(ok_info, io.BytesIO(ok_data))
        build_bad_member_fn(tf)

      self._build_tar_gz(archive, _populate)
      with self.assertRaises(ValueError):
        unpack_tar_gz_preserving_permissions(archive, dest)

      self.assertEqual(list(dest.iterdir()), [])

  def test_rejects_absolute_member_paths(self):
    for abs_name in ("/etc/passwd", "\\evil.txt"):
      with self.subTest(name=abs_name):

        def _add(tf: tarfile.TarFile, name=abs_name):
          info = tarfile.TarInfo(name=name)
          info.size = 4
          tf.addfile(info, io.BytesIO(b"evil"))

        self._assert_rejected_without_partial_extraction(_add)

  def test_rejects_dotdot_traversal_members(self):
    for traversal_name in ("../escape.txt", "a/../../escape.txt", ".."):
      with self.subTest(name=traversal_name):

        def _add(tf: tarfile.TarFile, name=traversal_name):
          info = tarfile.TarInfo(name=name)
          info.size = 4
          tf.addfile(info, io.BytesIO(b"evil"))

        self._assert_rejected_without_partial_extraction(_add)

  def test_rejects_escaping_symlinks_and_hardlinks(self):
    cases = [
        (tarfile.SYMTYPE, "abs_sym", "/etc/shadow"),
        (tarfile.SYMTYPE, "sub/rel_sym", "../../outside.txt"),
        (tarfile.LNKTYPE, "abs_lnk", "/etc/passwd"),
        (tarfile.LNKTYPE, "rel_lnk", "../outside.txt"),
    ]
    for link_type, name, target in cases:
      with self.subTest(type=link_type, name=name, target=target):

        def _add(tf: tarfile.TarFile, lt=link_type, nm=name, tg=target):
          info = tarfile.TarInfo(name=nm)
          info.type = lt
          info.linkname = tg
          tf.addfile(info)

        self._assert_rejected_without_partial_extraction(_add)

  def test_rejects_symlink_chain_traversal(self):
    def _add_ancestor_traversal(tf: tarfile.TarFile):
      sym_dir = tarfile.TarInfo(name="sub/alias")
      sym_dir.type = tarfile.SYMTYPE
      sym_dir.linkname = ".."
      tf.addfile(sym_dir)

      chained = tarfile.TarInfo(name="sub/alias/escape")
      chained.type = tarfile.SYMTYPE
      chained.linkname = "../outside.txt"
      tf.addfile(chained)

    def _add_reverse_order_traversal(tf: tarfile.TarFile):
      payload = tarfile.TarInfo(name="sub/alias/file.txt")
      payload.type = tarfile.REGTYPE
      payload.size = 4
      tf.addfile(payload, io.BytesIO(b"evil"))

      sym_dir = tarfile.TarInfo(name="sub/alias")
      sym_dir.type = tarfile.SYMTYPE
      sym_dir.linkname = ".."
      tf.addfile(sym_dir)

    def _add_linkname_hop_escape(tf: tarfile.TarFile):
      sym_dir = tarfile.TarInfo(name="sub/alias")
      sym_dir.type = tarfile.SYMTYPE
      sym_dir.linkname = ".."
      tf.addfile(sym_dir)

      hop_escape = tarfile.TarInfo(name="sub/escape")
      hop_escape.type = tarfile.SYMTYPE
      hop_escape.linkname = "alias/.."
      tf.addfile(hop_escape)

    def _add_symlink_cycle(tf: tarfile.TarFile):
      sym_a = tarfile.TarInfo(name="a")
      sym_a.type = tarfile.SYMTYPE
      sym_a.linkname = "b"
      tf.addfile(sym_a)

      sym_b = tarfile.TarInfo(name="b")
      sym_b.type = tarfile.SYMTYPE
      sym_b.linkname = "a"
      tf.addfile(sym_b)

    for builder in (
        _add_ancestor_traversal,
        _add_reverse_order_traversal,
        _add_linkname_hop_escape,
        _add_symlink_cycle,
    ):
      with self.subTest(case=builder.__name__):
        self._assert_rejected_without_partial_extraction(builder)

  def test_rejects_device_and_fifo_special_files(self):
    for special_type in (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
      with self.subTest(special_type=special_type):

        def _add(tf: tarfile.TarFile, st=special_type):
          info = tarfile.TarInfo(name="devnode")
          info.type = st
          tf.addfile(info)

        self._assert_rejected_without_partial_extraction(_add)


class TestFlattenRepoIfNested(unittest.TestCase):
  """Tests for flatten_repo_if_nested single-directory wrapper normalization."""

  def test_flattens_single_wrapper_directory_with_dotfiles(self):
    with tempfile.TemporaryDirectory() as tmp:
      repo_dir = Path(tmp) / "repo"
      wrapper = repo_dir / "my-repo-abc123"
      (wrapper / ".git").mkdir(parents=True)
      (wrapper / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
      (wrapper / ".gitignore").write_text("*.pyc\n")
      (wrapper / "src").mkdir()
      (wrapper / "src" / "app.py").write_text("print('hi')\n")

      flatten_repo_if_nested(repo_dir)

      self.assertTrue((repo_dir / ".git" / "HEAD").is_file())
      self.assertTrue((repo_dir / ".gitignore").is_file())
      self.assertTrue((repo_dir / "src" / "app.py").is_file())
      self.assertFalse(wrapper.exists())

  def test_flattens_when_child_shares_wrapper_directory_name(self):
    with tempfile.TemporaryDirectory() as tmp:
      repo_dir = Path(tmp) / "repo"
      wrapper = repo_dir / "project"
      (wrapper / ".git").mkdir(parents=True)
      (wrapper / "project").mkdir()
      (wrapper / "project" / "__init__.py").write_text("# pkg\n")

      flatten_repo_if_nested(repo_dir)

      self.assertTrue((repo_dir / ".git").is_dir())
      self.assertTrue((repo_dir / "project" / "__init__.py").is_file())

  def test_noop_when_top_level_git_already_exists(self):
    with tempfile.TemporaryDirectory() as tmp:
      repo_dir = Path(tmp) / "repo"
      (repo_dir / ".git").mkdir(parents=True)
      (repo_dir / "sub").mkdir()
      (repo_dir / "sub" / "file.txt").write_text("hello\n")

      flatten_repo_if_nested(repo_dir)

      self.assertTrue((repo_dir / ".git").is_dir())
      self.assertTrue((repo_dir / "sub" / "file.txt").is_file())

  def test_noop_when_multiple_entries_or_single_file(self):
    with tempfile.TemporaryDirectory() as tmp:
      repo_dir = Path(tmp) / "repo"
      (repo_dir / "dir_a").mkdir(parents=True)
      (repo_dir / "dir_b").mkdir(parents=True)

      flatten_repo_if_nested(repo_dir)
      self.assertTrue((repo_dir / "dir_a").is_dir())
      self.assertTrue((repo_dir / "dir_b").is_dir())

  def test_flattens_valid_internal_symlinks(self):
    with tempfile.TemporaryDirectory() as tmp:
      repo_dir = Path(tmp) / "repo"
      wrapper = repo_dir / "archive_wrapper"
      (wrapper / ".git").mkdir(parents=True)
      (wrapper / "scripts").mkdir(parents=True)
      (wrapper / "README.md").write_text("# readme\n")
      (wrapper / "scripts" / "readme_link").symlink_to("../README.md")

      flatten_repo_if_nested(repo_dir)

      promoted_link = repo_dir / "scripts" / "readme_link"
      self.assertTrue(promoted_link.is_symlink())
      self.assertEqual(promoted_link.read_text(), "# readme\n")

  def test_rejects_wrapper_symlink_escaping_on_promotion(self):
    escape_cases = (
        ("archive_wrapper/escape", ".."),
        ("archive_wrapper/escape", "../inputs"),
        ("archive_wrapper/escape", "../archive_wrapper/sub"),
        ("archive_wrapper/escape", "sub/../.."),
        ("archive_wrapper/.escape", ".."),
        ("archive_wrapper/.git/escape", "../.."),
    )
    for link_path, target in escape_cases:
      with self.subTest(link_path=link_path, target=target):
        with tempfile.TemporaryDirectory() as tmp:
          tmp_path = Path(tmp)
          archive = tmp_path / "code.tar.gz"
          repo_dir = tmp_path / "repo"
          repo_dir.mkdir()

          with tarfile.open(archive, "w:gz") as tf:
            for dir_name in (
                "archive_wrapper",
                "archive_wrapper/sub",
                "archive_wrapper/.git",
            ):
              dir_info = tarfile.TarInfo(name=dir_name)
              dir_info.type = tarfile.DIRTYPE
              dir_info.mode = 0o755
              tf.addfile(dir_info)

            sym = tarfile.TarInfo(name=link_path)
            sym.type = tarfile.SYMTYPE
            sym.linkname = target
            tf.addfile(sym)

          unpack_tar_gz_preserving_permissions(archive, repo_dir)
          with self.assertRaises(ValueError):
            flatten_repo_if_nested(repo_dir)

          self.assertFalse((repo_dir / "escape").exists())
          self.assertFalse((repo_dir / "escape").is_symlink())
          self.assertFalse((repo_dir / ".escape").exists())
          self.assertFalse((repo_dir / ".escape").is_symlink())
          self.assertFalse((repo_dir / ".git" / "escape").exists())
          self.assertFalse((repo_dir / ".git" / "escape").is_symlink())


if __name__ == "__main__":
  unittest.main()
