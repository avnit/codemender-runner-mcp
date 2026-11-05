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

# Governing: docs/openspec/specs/distribute/spec.md#req-0004, SPEC-DIST, ADR-0008

"""Unit and integration tests for scripts/set-fork-owner.sh and make init-fork."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent
_SET_FORK_OWNER_SH = _SCRIPTS_DIR / "set-fork-owner.sh"


def _run_git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
  env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
  return subprocess.run(
      ["git", "-C", str(repo_dir), *args],
      env=env,
      capture_output=True,
      text=True,
      check=True,
  )


class TestSetForkOwner(unittest.TestCase):
  """Tests for REQ-0004: Private Fork Ownership Configuration."""

  def setUp(self):
    super().setUp()
    self._tmp = tempfile.TemporaryDirectory()
    self.repo_dir = Path(self._tmp.name) / "fork_repo"
    (self.repo_dir / ".github" / "workflows").mkdir(parents=True)
    (self.repo_dir / "scripts").mkdir(parents=True)

    shutil.copy2(
        _REPO_ROOT / ".github" / "workflows" / "cm-merge-scan.yml",
        self.repo_dir / ".github" / "workflows" / "cm-merge-scan.yml",
    )
    if _SET_FORK_OWNER_SH.is_file():
      target_script = self.repo_dir / "scripts" / "set-fork-owner.sh"
      shutil.copy2(_SET_FORK_OWNER_SH, target_script)
      target_script.chmod(0o755)

    _run_git(self.repo_dir, "init", "-q")

  def tearDown(self):
    self._tmp.cleanup()
    super().tearDown()

  def _run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
    script_path = self.repo_dir / "scripts" / "set-fork-owner.sh"
    if not script_path.is_file():
      self.fail(f"Expected script at {_SET_FORK_OWNER_SH}")
    env = dict(
        os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1"
    )
    return subprocess.run(
        [str(script_path), *args],
        cwd=str(self.repo_dir),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

  def _workflow_text(self) -> str:
    return (
        self.repo_dir / ".github" / "workflows" / "cm-merge-scan.yml"
    ).read_text(encoding="utf-8")

  def _expected_ref(self) -> str:
    content = (
        _REPO_ROOT / ".github" / "workflows" / "cm-merge-scan.yml"
    ).read_text(encoding="utf-8")
    m = re.search(r"actions/codemender@([a-f0-9]+|\w+)", content)
    return m.group(1) if m else "main"

  def test_help_flag_prints_usage_and_exits_zero(self):
    self.assertTrue(
        _SET_FORK_OWNER_SH.is_file() and os.access(_SET_FORK_OWNER_SH, os.X_OK),
        f"Expected executable {_SET_FORK_OWNER_SH}",
    )
    for flag in ("-h", "--help"):
      with self.subTest(flag=flag):
        res = self._run_script(flag)
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("Usage:", res.stdout)

  def test_explicit_owner_updates_workflow_action_references(self):
    res = self._run_script("acme-corp")
    self.assertEqual(res.returncode, 0, msg=res.stderr)
    wf = self._workflow_text()
    ref = self._expected_ref()
    self.assertIn(
        f"- uses: acme-corp/codemender-runner/.github/actions/codemender@{ref}",
        wf,
    )
    self.assertIn(
        "# - uses:"
        f" acme-corp/codemender-runner/.github/actions/codemender-local@{ref}",
        wf,
    )
    self.assertNotIn("cloud-gtm/codemender-runner/.github/actions/", wf)

  def test_explicit_owner_and_repo_updates_workflow_action_references(self):
    res = self._run_script("acme-corp/custom-runner")
    self.assertEqual(res.returncode, 0, msg=res.stderr)
    wf = self._workflow_text()
    ref = self._expected_ref()
    self.assertIn(
        f"- uses: acme-corp/custom-runner/.github/actions/codemender@{ref}",
        wf,
    )
    self.assertIn(
        "# - uses:"
        f" acme-corp/custom-runner/.github/actions/codemender-local@{ref}",
        wf,
    )

  def test_auto_detects_owner_and_repo_from_https_and_ssh_origin_remotes(self):
    # 1. HTTPS remote with --auto and with no args
    _run_git(
        self.repo_dir,
        "remote",
        "add",
        "origin",
        "https://github.com/https-org/https-runner.git",
    )
    res_https = self._run_script("--auto")
    self.assertEqual(res_https.returncode, 0, msg=res_https.stderr)
    ref = self._expected_ref()
    self.assertIn(
        f"- uses: https-org/https-runner/.github/actions/codemender@{ref}",
        self._workflow_text(),
    )

    # 2. SSH remote with no args
    _run_git(
        self.repo_dir,
        "remote",
        "set-url",
        "origin",
        "git@github.com:ssh-org/ssh-runner.git",
    )
    res_ssh = self._run_script()
    self.assertEqual(res_ssh.returncode, 0, msg=res_ssh.stderr)
    self.assertIn(
        f"- uses: ssh-org/ssh-runner/.github/actions/codemender@{ref}",
        self._workflow_text(),
    )

  def test_idempotent_across_repeat_runs(self):
    res1 = self._run_script("acme-corp")
    self.assertEqual(res1.returncode, 0, msg=res1.stderr)
    after_first = self._workflow_text()

    res2 = self._run_script("acme-corp")
    self.assertEqual(res2.returncode, 0, msg=res2.stderr)
    after_second = self._workflow_text()
    self.assertEqual(after_first, after_second)

    res3 = self._run_script("second-org")
    self.assertEqual(res3.returncode, 0, msg=res3.stderr)
    ref = self._expected_ref()
    self.assertIn(
        "- uses:"
        f" second-org/codemender-runner/.github/actions/codemender@{ref}",
        self._workflow_text(),
    )

  def test_rejects_malformed_owner_with_exit_code_2_without_modifying_files(self):
    original = self._workflow_text()
    malformed_inputs = (
        "bad owner",
        "../traversal",
        "org/repo/extra",
        "org/$bad",
        "org/",
        "/repo",
        "..",
    )
    for bad in malformed_inputs:
      with self.subTest(bad_input=bad):
        res = self._run_script(bad)
        self.assertEqual(res.returncode, 2, msg=f"Input {bad!r} should exit 2")
        self.assertEqual(self._workflow_text(), original)

  def test_makefile_defines_init_fork_target(self):
    makefile_text = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    self.assertIn("init-fork", makefile_text)
    self.assertIn("./scripts/set-fork-owner.sh", makefile_text)

  def test_no_terraform_mutation_of_tracked_workflows(self):
    tf_root = _REPO_ROOT / "terraform"
    for tf_file in tf_root.rglob("*.tf"):
      content = tf_file.read_text(encoding="utf-8")
      if "github_repository_file" in content:
        self.assertNotIn(
            ".github/workflows/",
            content,
            f"Terraform file {tf_file} must not mutate .github/workflows/*",
        )


if __name__ == "__main__":
  unittest.main()
