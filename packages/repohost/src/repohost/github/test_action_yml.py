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

# Governing: docs/openspec/specs/distribute/spec.md#req-0001, docs/openspec/specs/distribute/spec.md#req-0003, SPEC-DIST, ADR-0008

"""Unit tests for dual-mode PKG_DIR and default runner_image in composite actions."""

from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parents[4]
_ACTIONS_DIR = _REPO_ROOT / ".github" / "actions"
_CODEMENDER_ACTION_DIR = _ACTIONS_DIR / "codemender"
_CODEMENDER_LOCAL_ACTION_DIR = _ACTIONS_DIR / "codemender-local"
_CODEMENDER_YML = _CODEMENDER_ACTION_DIR / "action.yml"
_CODEMENDER_LOCAL_YML = _CODEMENDER_LOCAL_ACTION_DIR / "action.yml"


def _extract_step_run_block(yaml_text: str, step_name: str) -> str:
  """Extracts the shell `run: |` script for a named composite action step."""
  pattern = re.compile(
      rf"- name: {re.escape(step_name)}\n(.*?)(?=\n    - |\Z)",
      re.DOTALL,
  )
  match = pattern.search(yaml_text)
  if not match:
    raise AssertionError(f"Step {step_name!r} not found in action.yml")
  step_body = match.group(1)
  run_match = re.search(r"      run: \|\n((?:        .*\n?)+)", step_body)
  if not run_match:
    raise AssertionError(f"run: | block not found in step {step_name!r}")
  return textwrap.dedent(run_match.group(1))


class TestCompositeActionsDualModeAndDefaultImage(unittest.TestCase):
  """Tests REQ-0001 (dual-mode PKG_DIR) and REQ-0003 (default runner_image)."""

  def test_both_actions_define_dual_mode_pkg_dir_resolution_in_yaml(self):
    for action_yml in (_CODEMENDER_YML, _CODEMENDER_LOCAL_YML):
      with self.subTest(action=str(action_yml)):
        content = action_yml.read_text(encoding="utf-8")
        self.assertIn("GITHUB_WORKSPACE: ${{ github.workspace }}", content)
        run_script = _extract_step_run_block(
            content, "Install action dependencies"
        )
        self.assertIn(
            'if [[ -d "${GITHUB_WORKSPACE:-}/packages/repohost" ]]; then',
            run_script,
        )
        self.assertIn('PKG_DIR="${GITHUB_WORKSPACE}/packages"', run_script)
        self.assertIn(
            'PKG_DIR="$(cd "${ACTION_PATH}/../../../packages" && pwd)"',
            run_script,
        )

  def test_install_dependencies_pkg_dir_behavioral_execution(self):
    for action_yml in (_CODEMENDER_YML, _CODEMENDER_LOCAL_YML):
      content = action_yml.read_text(encoding="utf-8")
      run_script = _extract_step_run_block(
          content, "Install action dependencies"
      )

      with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        action_repo = tmp_path / "action_repo"
        action_path = action_repo / ".github" / "actions" / "codemender"
        action_pkgs = action_repo / "packages"
        for pkg in ("artifactstore", "codemender", "launcher", "repohost"):
          (action_pkgs / pkg).mkdir(parents=True, exist_ok=True)
        action_path.mkdir(parents=True, exist_ok=True)
        (action_path / "requirements.txt").write_text("", encoding="utf-8")

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        pip_log = tmp_path / "pip_args.txt"
        fake_python = fake_bin / "python"
        fake_python.write_text(
            f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{pip_log}"\n',
            encoding="utf-8",
        )
        fake_python.chmod(0o755)

        # Case 1: Self-dogfooding workspace WITH packages/repohost
        dogfood_ws = tmp_path / "dogfood_workspace"
        (dogfood_ws / "packages" / "repohost").mkdir(parents=True)
        env_dogfood = {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "ACTION_PATH": str(action_path),
            "GITHUB_WORKSPACE": str(dogfood_ws),
        }
        res_dogfood = subprocess.run(
            ["bash", "-c", run_script],
            env=env_dogfood,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res_dogfood.returncode, 0, msg=res_dogfood.stderr)
        pip_args_dogfood = pip_log.read_text(encoding="utf-8")
        self.assertIn(
            f"{dogfood_ws}/packages/artifactstore", pip_args_dogfood
        )
        self.assertIn(f"{dogfood_ws}/packages/codemender", pip_args_dogfood)
        self.assertIn(
            f"{dogfood_ws}/packages/repohost[github]", pip_args_dogfood
        )

        # Case 2: External target repo WITHOUT packages/repohost
        external_ws = tmp_path / "external_app_repo"
        external_ws.mkdir(parents=True)
        env_external = {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "ACTION_PATH": str(action_path),
            "GITHUB_WORKSPACE": str(external_ws),
        }
        res_external = subprocess.run(
            ["bash", "-c", run_script],
            env=env_external,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res_external.returncode, 0, msg=res_external.stderr)
        pip_args_external = pip_log.read_text(encoding="utf-8")
        self.assertIn(
            f"{action_pkgs.resolve()}/artifactstore", pip_args_external
        )
        self.assertIn(
            f"{action_pkgs.resolve()}/codemender", pip_args_external
        )
        self.assertIn(
            f"{action_pkgs.resolve()}/repohost[github]", pip_args_external
        )

  def test_codemender_cloud_run_inputs_optional_and_blank_default_in_yaml(self):
    content = _CODEMENDER_YML.read_text(encoding="utf-8")
    for input_name in ("cloud_run_job_name", "cloud_run_region", "gcs_transit_bucket"):
      with self.subTest(input_name=input_name):
        match = re.search(rf"  {input_name}:\n((?:    .*\n)+)", content)
        self.assertIsNotNone(match, f"{input_name} input not found in action.yml")
        block = match.group(1)
        self.assertIn("required: false", block)
        self.assertIn("default: ''", block)

  def test_codemender_local_runner_image_optional_and_default_in_yaml(self):
    content = _CODEMENDER_LOCAL_YML.read_text(encoding="utf-8")
    runner_image_match = re.search(
        r"  runner_image:\n((?:    .*\n)+)", content
    )
    self.assertIsNotNone(runner_image_match, "runner_image input not found")
    runner_image_block = runner_image_match.group(1)
    self.assertIn("required: false", runner_image_block)
    self.assertIn("default: ''", runner_image_block)

    self.assertIn(
        "DEFAULT_IMAGE: ghcr.io/${{ github.action_repository || github.repository }}:latest",
        content,
    )

  def test_codemender_local_pull_image_step_behavioral_execution(self):
    content = _CODEMENDER_LOCAL_YML.read_text(encoding="utf-8")
    pull_script = _extract_step_run_block(content, "Pull cm-runner image")

    with tempfile.TemporaryDirectory() as tmp:
      tmp_path = Path(tmp)
      fake_bin = tmp_path / "bin"
      fake_bin.mkdir()
      docker_log = tmp_path / "docker.log"
      fake_docker = fake_bin / "docker"
      fake_docker.write_text(
          f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{docker_log}"\n',
          encoding="utf-8",
      )
      fake_docker.chmod(0o755)

      # Case 1: Blank IMAGE falls back to DEFAULT_IMAGE normalized to lowercase
      gh_out_1 = tmp_path / "gh_output_1.txt"
      gh_out_1.write_text("", encoding="utf-8")
      env_default = {
          "PATH": f"{fake_bin}:/usr/bin:/bin",
          "IMAGE": "",
          "DEFAULT_IMAGE": "ghcr.io/Org-A/CodeMender-Runner:latest",
          "TOKEN": "fake-token",
          "ACTOR": "octocat",
          "GITHUB_OUTPUT": str(gh_out_1),
      }
      res_default = subprocess.run(
          ["bash", "-c", pull_script],
          env=env_default,
          capture_output=True,
          text=True,
          check=False,
      )
      self.assertEqual(res_default.returncode, 0, msg=res_default.stderr)
      self.assertIn(
          "pull ghcr.io/org-a/codemender-runner:latest",
          docker_log.read_text(encoding="utf-8"),
      )
      self.assertIn(
          "image=ghcr.io/org-a/codemender-runner:latest",
          gh_out_1.read_text(encoding="utf-8"),
      )

      # Case 2: Explicit mixed-case IMAGE overrides DEFAULT_IMAGE and lowercases repo
      docker_log.write_text("", encoding="utf-8")
      gh_out_2 = tmp_path / "gh_output_2.txt"
      gh_out_2.write_text("", encoding="utf-8")
      env_custom = {
          "PATH": f"{fake_bin}:/usr/bin:/bin",
          "IMAGE": "ghcr.io/Custom-Org/My-Runner:SHA123",
          "DEFAULT_IMAGE": "ghcr.io/Org-A/CodeMender-Runner:latest",
          "TOKEN": "fake-token",
          "ACTOR": "octocat",
          "GITHUB_OUTPUT": str(gh_out_2),
      }
      res_custom = subprocess.run(
          ["bash", "-c", pull_script],
          env=env_custom,
          capture_output=True,
          text=True,
          check=False,
      )
      self.assertEqual(res_custom.returncode, 0, msg=res_custom.stderr)
      self.assertIn(
          "pull ghcr.io/custom-org/my-runner:SHA123",
          docker_log.read_text(encoding="utf-8"),
      )
      self.assertIn(
          "image=ghcr.io/custom-org/my-runner:SHA123",
          gh_out_2.read_text(encoding="utf-8"),
      )

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0001, SPEC-GHA-FIX
  def test_codemender_cloudrun_action_max_parallel_fixes_input_and_env(self):
    content = _CODEMENDER_YML.read_text(encoding="utf-8")
    input_match = re.search(
        r"  max_parallel_fixes:\n((?:    .*\n)+)", content
    )
    self.assertIsNotNone(
        input_match, "max_parallel_fixes input not found in .github/actions/codemender/action.yml"
    )
    input_block = input_match.group(1)
    self.assertIn("required: false", input_block)
    self.assertIn("default: '4'", input_block)
    self.assertIn(
        "CODEMENDER_MAX_PARALLEL_FIXES: ${{ inputs.max_parallel_fixes }}",
        content,
    )


if __name__ == "__main__":
  unittest.main()

