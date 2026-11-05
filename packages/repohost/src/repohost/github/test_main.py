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

# Governing: docs/openspec/specs/distribute/spec.md#req-0005, SPEC-DIST, SPEC-GHA, ADR-0008

"""Unit tests for the cm-github CLI entrypoint (repohost.github.__main__)."""

from pathlib import Path
import tomllib
import unittest
from unittest.mock import MagicMock, patch

from repohost.github import __main__ as github_main
from repohost.github import gha_runner


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
    with patch.object(gha_runner, "main", return_value=0) as mock_main:
      rc = github_main.main()
    self.assertEqual(rc, 0)
    mock_main.assert_called_once_with()

    with patch.object(gha_runner, "main", return_value=3) as mock_main:
      rc_nonzero = github_main.main()
    self.assertEqual(rc_nonzero, 3)
    mock_main.assert_called_once_with()

  def test_main_executes_co_located_gha_runner(self):
    with patch.dict(
        "os.environ",
        {"GITHUB_EVENT_NAME": "push"},
        clear=False,
    ):
      rc = github_main.main()
    self.assertEqual(rc, 0)

  def test_main_passes_host_and_store_to_gha_runner(self):
    mock_host = MagicMock()
    mock_store = MagicMock()
    with patch.object(gha_runner, "main", return_value=0) as mock_main:
      rc = github_main.main(host=mock_host, store=mock_store)
    self.assertEqual(rc, 0)
    mock_main.assert_called_once_with(host=mock_host, store=mock_store)


if __name__ == "__main__":
  unittest.main()
