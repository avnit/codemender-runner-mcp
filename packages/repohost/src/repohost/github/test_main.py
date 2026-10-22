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

# Governing: docs/openspec/specs/distribute/spec.md#req-0005, SPEC-DIST, ADR-0008

"""Unit tests for the cm-github CLI entrypoint shim (repohost.github.__main__)."""

from pathlib import Path
import sys
import tomllib
import types
import unittest
from unittest.mock import MagicMock, patch


_THIS_DIR = Path(__file__).resolve().parent
_REPOHOST_ROOT = _THIS_DIR.parents[2]


class TestCmGithubEntrypoint(unittest.TestCase):
  """Tests for REQ-0005: Host Orchestrator CLI Entrypoint (cm-github)."""

  def test_pyproject_defines_cm_github_console_script(self):
    pyproject_path = _REPOHOST_ROOT / "pyproject.toml"
    self.assertTrue(pyproject_path.is_file(), f"Missing {pyproject_path}")
    with pyproject_path.open("rb") as f:
      data = tomllib.load(f)
    scripts = data.get("project", {}).get("scripts", {})
    self.assertEqual(
        scripts.get("cm-github"),
        "repohost.github.__main__:main",
    )

  def test_main_delegates_to_gha_runner_main_and_returns_exit_code(self):
    from repohost.github import __main__ as github_main  # pylint: disable=g-import-not-at-top

    fake_gha_runner = types.ModuleType("gha_runner")
    fake_gha_runner.main = MagicMock(return_value=0)
    with patch.dict(sys.modules, {"gha_runner": fake_gha_runner}):
      rc = github_main.main()
    self.assertEqual(rc, 0)
    fake_gha_runner.main.assert_called_once_with()

    fake_gha_runner.main.reset_mock()
    fake_gha_runner.main.return_value = 3
    with patch.dict(sys.modules, {"gha_runner": fake_gha_runner}):
      rc_nonzero = github_main.main()
    self.assertEqual(rc_nonzero, 3)
    fake_gha_runner.main.assert_called_once_with()

  def test_main_locates_gha_runner_from_repo_root_when_not_on_sys_path(self):
    from repohost.github import __main__ as github_main  # pylint: disable=g-import-not-at-top

    saved_module = sys.modules.pop("gha_runner", None)
    saved_path = list(sys.path)
    try:
      sys.path = [p for p in sys.path if "actions/codemender" not in p]
      with patch.dict(
          "os.environ",
          {"GITHUB_EVENT_NAME": "push"},
          clear=False,
      ):
        rc = github_main.main()
      self.assertEqual(rc, 0)
    finally:
      sys.path = saved_path
      if saved_module is not None:
        sys.modules["gha_runner"] = saved_module


if __name__ == "__main__":
  unittest.main()
