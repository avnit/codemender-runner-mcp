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

"""Unit and integration tests for cloud-run-jobs/entrypoint.py."""

import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import unittest
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_CLOUD_RUN_DIR = os.path.dirname(_TESTS_DIR)
_REPO_ROOT = os.path.dirname(_CLOUD_RUN_DIR)
_ENTRYPOINT_PY = os.path.join(_CLOUD_RUN_DIR, "entrypoint.py")
_CODEMENDER_SRC_DIR = os.path.join(_REPO_ROOT, "packages", "codemender", "src")

_MOCK_CM_SCRIPT = """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
if not args or args[0] in ("--version", "-v"):
    print("cm 0.12.0")
    sys.exit(0)

state_file = os.path.expanduser("~/.codemender/mock_findings.json")
os.makedirs(os.path.dirname(state_file), exist_ok=True)

def load_findings():
    if os.path.exists(state_file):
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_findings(items):
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(items, f)

cmd = args[0]
if cmd == "init":
    if "--verify" in args:
        print("  Results: 4 passed, 0 warnings")
    else:
        cfg = os.path.expanduser("~/.codemender/config.yaml")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write("version: 1\\n")
elif cmd == "find":
    print("Mock CM: scan complete")
elif cmd == "fix":
    items = load_findings()
    for item in items:
        item["Status"] = "FIXED"
        item["status"] = "FIXED"
        target = item.get("file_path") or "sample.txt"
        if os.path.exists(target):
            with open(target, "a", encoding="utf-8") as f:
                f.write("// patched by mock cm\\n")
    save_findings(items)
elif cmd == "verify":
    print("Mock CM: verify complete")
elif cmd == "report":
    if len(args) > 1 and args[1] == "import":
        items = load_findings()
        items.append({
            "ID": "fid-imported-1",
            "finding_id": "fid-imported-1",
            "Type": "CWE-78",
            "vuln_type": "CWE-78",
            "File": "sample.txt",
            "file_path": "sample.txt",
            "Title": "Command injection in sample.txt",
            "title": "Command injection in sample.txt",
            "Status": "OPEN",
            "status": "OPEN",
            "Severity": "HIGH",
            "severity": "HIGH",
            "Analysis": "Mock analysis",
            "analysis": "Mock analysis",
        })
        save_findings(items)
    elif "--format" in args:
        fmt = args[args.index("--format") + 1]
        if fmt == "sarif":
            print(json.dumps({
                "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
                "version": "2.1.0",
                "runs": [{"tool": {"driver": {"name": "CodeMender", "version": "0.12.0", "rules": []}}, "results": []}],
            }))
        elif fmt == "md":
            print("# CodeMender Scan Report\\n\\nNo findings detected.")
        elif fmt == "json":
            print(json.dumps(load_findings()))
"""


def _load_entrypoint_module():
  spec = importlib.util.spec_from_file_location("entrypoint", _ENTRYPOINT_PY)
  if spec is None or spec.loader is None:
    raise ImportError(f"Could not load module from {_ENTRYPOINT_PY}")
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


class TestCloudRunEntrypoint(unittest.TestCase):
  """Tests Cloud Run Job Python entrypoint bootstrapper execution and contracts."""

  def setUp(self):
    self.temp_dir = tempfile.mkdtemp(prefix="cm_cr_test_")
    self.home_dir = os.path.join(self.temp_dir, "home")
    os.makedirs(self.home_dir, exist_ok=True)
    self.mock_bin_dir = os.path.join(self.temp_dir, "mock_bin")
    os.makedirs(self.mock_bin_dir, exist_ok=True)
    mock_cm_path = os.path.join(self.mock_bin_dir, "cm")
    with open(mock_cm_path, "w", encoding="utf-8") as f:
      f.write(_MOCK_CM_SCRIPT)
    os.chmod(mock_cm_path, 0o755)
    mock_cm_runner_path = os.path.join(self.mock_bin_dir, "cm-runner")
    with open(mock_cm_runner_path, "w", encoding="utf-8") as f:
      f.write(
          "#!/usr/bin/env python3\n"
          "import sys\n"
          f"sys.path.insert(0, {_CODEMENDER_SRC_DIR!r})\n"
          "from codemender.runner.__main__ import main\n"
          "sys.exit(main())\n"
      )
    os.chmod(mock_cm_runner_path, 0o755)
    self.workspace_dir = os.path.join(self.temp_dir, "workspace")
    self.output_dir = os.path.join(self.workspace_dir, "output")
    self.repo_dir = os.path.join(self.workspace_dir, "repo")
    self.inputs_dir = os.path.join(self.workspace_dir, "inputs")
    self.gcs_sim_dir = os.path.join(self.temp_dir, "gcs_sim")
    os.makedirs(self.gcs_sim_dir, exist_ok=True)

  def tearDown(self):
    shutil.rmtree(self.temp_dir, ignore_errors=True)

  def _build_env(self, extra_env: dict | None = None) -> dict:
    env = dict(os.environ)
    env["HOME"] = self.home_dir
    env["PATH"] = f"{self.mock_bin_dir}:{env.get('PATH', '')}"
    env["EPHEMERAL_SCRATCH_DIR"] = self.workspace_dir
    env["WORKSPACE_DIR"] = self.workspace_dir
    env["OUTPUT_DIR"] = self.output_dir
    env["REPO_DIR"] = self.repo_dir
    env["INPUTS_DIR"] = self.inputs_dir
    if extra_env:
      env.update(extra_env)
    return env

  def _run_entrypoint(
      self, *args: str, env: dict | None = None, check: bool = False
  ) -> subprocess.CompletedProcess:
    full_env = self._build_env(env)
    return subprocess.run(
        [sys.executable, _ENTRYPOINT_PY, *args],
        env=full_env,
        capture_output=True,
        text=True,
        check=check,
    )

  def _create_mock_repo(self) -> str:
    src_dir = os.path.join(self.temp_dir, "repo_src")
    os.makedirs(src_dir, exist_ok=True)
    git_env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    subprocess.run(
        ["git", "-C", src_dir, "-c", "commit.gpgsign=false", "init", "-q"],
        env=git_env,
        check=True,
    )
    subprocess.run(
        ["git", "-C", src_dir, "config", "user.name", "Test Runner"],
        env=git_env,
        check=True,
    )
    subprocess.run(
        ["git", "-C", src_dir, "config", "user.email", "test@example.com"],
        env=git_env,
        check=True,
    )

    file_path = os.path.join(src_dir, "sample.txt")
    with open(file_path, "w", encoding="utf-8") as f:
      f.write("initial content\n")
    subprocess.run(
        ["git", "-C", src_dir, "-c", "commit.gpgsign=false", "add", "sample.txt"],
        env=git_env,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            src_dir,
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "Initial",
        ],
        env=git_env,
        check=True,
    )

    with open(file_path, "a", encoding="utf-8") as f:
      f.write("modified diff content\n")
    subprocess.run(
        ["git", "-C", src_dir, "-c", "commit.gpgsign=false", "add", "sample.txt"],
        env=git_env,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            src_dir,
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "Modified",
        ],
        env=git_env,
        check=True,
    )

    archive_path = os.path.join(self.temp_dir, "code.tar.gz")
    with tarfile.open(archive_path, "w:gz") as tar:
      for item in os.listdir(src_dir):
        tar.add(os.path.join(src_dir, item), arcname=item)
    return archive_path

  def test_version_command_success(self):
    result = self._run_entrypoint("version")
    self.assertEqual(result.returncode, 0, f"Stderr: {result.stderr}")

    receipt_file = os.path.join(self.output_dir, "receipt.json")
    self.assertTrue(os.path.exists(receipt_file))
    with open(receipt_file, "r", encoding="utf-8") as f:
      receipt = json.load(f)
    self.assertEqual(receipt["status"], "SUCCESS")
    self.assertEqual(receipt["exit_code"], 0)
    self.assertEqual(receipt["command"], "version")
    self.assertIn("log", receipt["artifacts"])
    self.assertIsNone(receipt["error"])

    runner_log = os.path.join(self.output_dir, "runner.log")
    self.assertTrue(os.path.exists(runner_log))
    with open(runner_log, "r", encoding="utf-8") as f:
      content = f.read()
    self.assertIn("CodeMender-Bootstrapper", content)
    self.assertIn("cm-runner version", content)

  def test_workspace_and_ephemeral_scratch_dir_defaults(self):
    with open(_ENTRYPOINT_PY, "r", encoding="utf-8") as f:
      script_text = f.read()
    self.assertIn("EPHEMERAL_SCRATCH_DIR", script_text)

    result = self._run_entrypoint("version")
    self.assertEqual(result.returncode, 0, f"Stderr: {result.stderr}")
    self.assertTrue(os.path.isdir(self.workspace_dir))
    self.assertTrue(os.path.isdir(self.output_dir))
    self.assertTrue(os.path.isdir(self.repo_dir))
    self.assertTrue(os.path.isdir(self.inputs_dir))

  def test_cm_runner_prefix_stripped(self):
    result = self._run_entrypoint("cm-runner", "version")
    self.assertEqual(result.returncode, 0, f"Stderr: {result.stderr}")
    receipt_file = os.path.join(self.output_dir, "receipt.json")
    with open(receipt_file, "r", encoding="utf-8") as f:
      receipt = json.load(f)
    self.assertEqual(receipt["command"], "version")

  def test_options_before_subcommand(self):
    result = self._run_entrypoint("--log-level", "debug", "version")
    self.assertEqual(result.returncode, 0, f"Stderr: {result.stderr}")
    receipt_file = os.path.join(self.output_dir, "receipt.json")
    with open(receipt_file, "r", encoding="utf-8") as f:
      receipt = json.load(f)
    self.assertEqual(receipt["command"], "version")

  def test_unsupported_command_fails_with_code_2(self):
    result = self._run_entrypoint(env={"CM_COMMAND": "unsupported_command"})
    self.assertEqual(result.returncode, 2)

    receipt_file = os.path.join(self.output_dir, "receipt.json")
    with open(receipt_file, "r", encoding="utf-8") as f:
      receipt = json.load(f)
    self.assertEqual(receipt["status"], "FAILURE")
    self.assertEqual(receipt["exit_code"], 2)

  def test_missing_payload_and_missing_finding_exit_code_2(self):
    result = self._run_entrypoint("find-diff")
    self.assertEqual(result.returncode, 2)
    receipt_file = os.path.join(self.output_dir, "receipt.json")
    with open(receipt_file, "r", encoding="utf-8") as f:
      receipt = json.load(f)
    self.assertEqual(receipt["status"], "FAILURE")
    self.assertEqual(receipt["exit_code"], 2)

    archive_path = self._create_mock_repo()
    result_fix = self._run_entrypoint(
        "fix", env={"PAYLOAD_URI": f"file://{archive_path}"}
    )
    self.assertEqual(result_fix.returncode, 2)
    with open(receipt_file, "r", encoding="utf-8") as f:
      receipt_fix = json.load(f)
    self.assertEqual(receipt_fix["status"], "FAILURE")
    self.assertEqual(receipt_fix["exit_code"], 2)

  def test_find_diff_dual_finding_artifacts(self):
    archive_path = self._create_mock_repo()
    gcs_dest = os.path.join(self.gcs_sim_dir, "find-diff")
    result = self._run_entrypoint(
        "find-diff",
        "--base",
        "HEAD~1",
        env={
            "PAYLOAD_URI": f"file://{archive_path}",
            "OUTPUT_URI": f"file://{gcs_dest}",
        },
    )
    self.assertEqual(result.returncode, 0, f"Stderr: {result.stderr}")

    # Check that repo was extracted
    self.assertTrue(os.path.exists(os.path.join(self.repo_dir, ".git")))
    self.assertTrue(os.path.exists(os.path.join(self.repo_dir, "sample.txt")))

    # Verify both report.sarif and findings.json exist in local output_dir
    local_sarif = os.path.join(self.output_dir, "report.sarif")
    local_findings = os.path.join(self.output_dir, "findings.json")
    self.assertTrue(os.path.exists(local_sarif))
    self.assertTrue(os.path.exists(local_findings))
    with open(local_sarif, "r", encoding="utf-8") as f1, open(
        local_findings, "r", encoding="utf-8"
    ) as f2:
      self.assertEqual(json.load(f1), json.load(f2))

    # Check receipt metadata
    receipt_file = os.path.join(self.output_dir, "receipt.json")
    self.assertTrue(os.path.exists(receipt_file))
    with open(receipt_file, "r", encoding="utf-8") as f:
      receipt = json.load(f)
    self.assertEqual(receipt["status"], "SUCCESS")
    self.assertEqual(receipt["exit_code"], 0)
    self.assertEqual(receipt["command"], "find-diff")
    self.assertIn("sarif", receipt["artifacts"])
    self.assertIn("findings", receipt["artifacts"])
    self.assertIn("report", receipt["artifacts"])
    self.assertIn("log", receipt["artifacts"])

    # Check transit destination received all artifacts
    self.assertTrue(os.path.exists(os.path.join(gcs_dest, "receipt.json")))
    self.assertTrue(os.path.exists(os.path.join(gcs_dest, "runner.log")))
    self.assertTrue(os.path.exists(os.path.join(gcs_dest, "report.sarif")))
    self.assertTrue(os.path.exists(os.path.join(gcs_dest, "findings.json")))
    self.assertTrue(os.path.exists(os.path.join(gcs_dest, "report.md")))

  def test_vendor_agnostic_uris_and_manifest_resolution(self):
    archive_path = self._create_mock_repo()
    os.makedirs(self.inputs_dir, exist_ok=True)
    manifest_src = os.path.join(self.temp_dir, "manifest.json")
    out_dest = os.path.join(self.gcs_sim_dir, "manifest_out")
    manifest_data = {
        "command": "find-diff",
        "payload_uri": f"file://{archive_path}",
        "output_uri": f"file://{out_dest}",
        "diff_base": "HEAD~1",
    }
    with open(manifest_src, "w", encoding="utf-8") as f:
      json.dump(manifest_data, f)

    result = self._run_entrypoint(
        env={"MANIFEST_URI": f"file://{manifest_src}"}
    )
    self.assertEqual(result.returncode, 0, f"Stderr: {result.stderr}")
    receipt_file = os.path.join(out_dest, "receipt.json")
    self.assertTrue(os.path.exists(receipt_file))
    with open(receipt_file, "r", encoding="utf-8") as f:
      receipt = json.load(f)
    self.assertEqual(receipt["status"], "SUCCESS")
    self.assertEqual(receipt["command"], "find-diff")
    self.assertTrue(os.path.exists(os.path.join(out_dest, "findings.json")))
    self.assertTrue(os.path.exists(os.path.join(out_dest, "report.sarif")))

  def test_cli_uri_flag_overrides(self):
    archive_path = self._create_mock_repo()
    out_dest = os.path.join(self.gcs_sim_dir, "cli_override_out")
    result = self._run_entrypoint(
        "find-diff",
        "--payload-uri",
        f"file://{archive_path}",
        "--output-uri",
        f"file://{out_dest}",
        "--base",
        "HEAD~1",
    )
    self.assertEqual(result.returncode, 0, f"Stderr: {result.stderr}")
    self.assertTrue(os.path.exists(os.path.join(out_dest, "receipt.json")))
    self.assertTrue(os.path.exists(os.path.join(out_dest, "findings.json")))

  def test_fix_command_stages_finding_uri_and_outputs_patch(self):
    archive_path = self._create_mock_repo()
    finding_src = os.path.join(self.temp_dir, "single_finding.sarif")
    sarif_payload = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "CodeMender", "version": "0.12.0"}},
            "results": [{
                "ruleId": "CWE-78",
                "level": "error",
                "message": {"text": "Command injection in sample.txt"},
                "locations": [{
                    "physicalLocation": {
                        "artifactLocation": {"uri": "sample.txt"},
                        "region": {"startLine": 1, "endLine": 1},
                    }
                }],
            }],
        }],
    }
    with open(finding_src, "w", encoding="utf-8") as f:
      json.dump(sarif_payload, f)

    out_dest = os.path.join(self.gcs_sim_dir, "fix_out")
    result = self._run_entrypoint(
        "fix",
        env={
            "PAYLOAD_URI": f"file://{archive_path}",
            "FINDING_URI": f"file://{finding_src}",
            "OUTPUT_URI": f"file://{out_dest}",
        },
    )
    self.assertEqual(result.returncode, 0, f"Stderr: {result.stderr}")
    self.assertTrue(os.path.exists(os.path.join(self.inputs_dir, "finding.sarif")))
    receipt_file = os.path.join(out_dest, "receipt.json")
    self.assertTrue(os.path.exists(receipt_file))
    with open(receipt_file, "r", encoding="utf-8") as f:
      receipt = json.load(f)
    self.assertEqual(receipt["status"], "SUCCESS")
    self.assertEqual(receipt["command"], "fix")
    self.assertIn("patch", receipt["artifacts"])

  def test_verify_command_stages_finding_uri_and_indexes_sarif_in_receipt(self):
    archive_path = self._create_mock_repo()
    finding_src = os.path.join(self.temp_dir, "single_finding.sarif")
    sarif_payload = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "CodeMender", "version": "0.12.0"}},
            "results": [{
                "ruleId": "CWE-78",
                "level": "error",
                "message": {"text": "Command injection in sample.txt"},
                "locations": [{
                    "physicalLocation": {
                        "artifactLocation": {"uri": "sample.txt"},
                        "region": {"startLine": 1, "endLine": 1},
                    }
                }],
            }],
        }],
    }
    with open(finding_src, "w", encoding="utf-8") as f:
      json.dump(sarif_payload, f)

    out_dest = os.path.join(self.gcs_sim_dir, "verify_out")
    result = self._run_entrypoint(
        "verify",
        env={
            "PAYLOAD_URI": f"file://{archive_path}",
            "FINDING_URI": f"file://{finding_src}",
            "OUTPUT_URI": f"file://{out_dest}",
        },
    )
    self.assertEqual(result.returncode, 0, f"Stderr: {result.stderr}")
    self.assertTrue(os.path.exists(os.path.join(self.inputs_dir, "finding.sarif")))
    self.assertTrue(os.path.exists(os.path.join(out_dest, "finding.sarif")))
    receipt_file = os.path.join(out_dest, "receipt.json")
    self.assertTrue(os.path.exists(receipt_file))
    with open(receipt_file, "r", encoding="utf-8") as f:
      receipt = json.load(f)
    self.assertEqual(receipt["status"], "SUCCESS")
    self.assertEqual(receipt["command"], "verify")
    self.assertEqual(receipt["finding_count"], 1)
    self.assertIn("sarif", receipt["artifacts"])
    self.assertTrue(receipt["artifacts"]["sarif"].endswith("/finding.sarif"))

  def test_init_command_success(self):
    archive_path = self._create_mock_repo()
    result = self._run_entrypoint(
        "init", env={"PAYLOAD_URI": f"file://{archive_path}"}
    )
    self.assertEqual(result.returncode, 0, f"Stderr: {result.stderr}")
    receipt_file = os.path.join(self.output_dir, "receipt.json")
    self.assertTrue(os.path.exists(receipt_file))
    with open(receipt_file, "r", encoding="utf-8") as f:
      receipt = json.load(f)
    self.assertEqual(receipt["status"], "SUCCESS")
    self.assertEqual(receipt["exit_code"], 0)
    self.assertEqual(receipt["command"], "init")

  def test_sigterm_preemption_kills_children_and_writes_receipt(self):
    slow_bin_dir = os.path.join(self.temp_dir, "slow_bin")
    os.makedirs(slow_bin_dir, exist_ok=True)
    child_pid_file = os.path.join(self.temp_dir, "child.pid")
    slow_cm_runner = os.path.join(slow_bin_dir, "cm-runner")
    with open(slow_cm_runner, "w", encoding="utf-8") as f:
      f.write(
          "#!/bin/bash\n"
          f"sleep 60 &\n"
          f"echo $! > {child_pid_file}\n"
          "wait\n"
      )
    os.chmod(slow_cm_runner, 0o755)

    env = self._build_env()
    env["PATH"] = f"{slow_bin_dir}:{env['PATH']}"
    out_dest = os.path.join(self.gcs_sim_dir, "sigterm_out")
    env["OUTPUT_URI"] = f"file://{out_dest}"

    proc = subprocess.Popen(
        [sys.executable, _ENTRYPOINT_PY, "version"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 5.0
    while time.time() < deadline and not os.path.exists(child_pid_file):
      time.sleep(0.05)

    os.kill(proc.pid, signal.SIGTERM)
    _, _ = proc.communicate(timeout=5)
    self.assertIn(proc.returncode, (-15, 143))

    receipt_file = os.path.join(self.output_dir, "receipt.json")
    self.assertTrue(os.path.exists(receipt_file))
    with open(receipt_file, "r", encoding="utf-8") as f:
      receipt = json.load(f)
    self.assertEqual(receipt["status"], "FAILURE")
    self.assertEqual(receipt["exit_code"], 143)

  def test_early_exit_before_sync_spills_runner_log_to_stderr(self):
    result = self._run_entrypoint("find-diff")
    self.assertEqual(result.returncode, 2)
    self.assertIn("FALLBACK LOG SPILL", result.stderr)
    self.assertIn("CodeMender-Bootstrapper", result.stderr)

  def test_tar_gz_flattening_and_posix_permission_preservation(self):
    nested_dir = os.path.join(self.temp_dir, "nested_src")
    wrapper_dir = os.path.join(nested_dir, "my_repo_folder")
    os.makedirs(os.path.join(wrapper_dir, ".git"), exist_ok=True)
    exec_script = os.path.join(wrapper_dir, "build.sh")
    with open(exec_script, "w", encoding="utf-8") as f:
      f.write("#!/bin/sh\necho ok\n")
    os.chmod(exec_script, 0o755)

    archive_path = os.path.join(self.temp_dir, "nested.tar.gz")
    with tarfile.open(archive_path, "w:gz") as tar:
      tar.add(wrapper_dir, arcname="my_repo_folder")

    gcs_dest = os.path.join(self.gcs_sim_dir, "flatten_test")
    result = self._run_entrypoint(
        "init",
        env={
            "PAYLOAD_URI": f"file://{archive_path}",
            "OUTPUT_URI": f"file://{gcs_dest}",
        },
    )
    self.assertEqual(result.returncode, 0, f"Stderr: {result.stderr}")
    self.assertTrue(os.path.exists(os.path.join(self.repo_dir, ".git")))
    extracted_script = os.path.join(self.repo_dir, "build.sh")
    self.assertTrue(os.path.exists(extracted_script))
    mode = stat.S_IMODE(os.stat(extracted_script).st_mode)
    self.assertEqual(mode & 0o111, 0o111, f"Expected executable bits in {oct(mode)}")

  def test_unpack_tar_gz_normalizes_ownership_and_user_write_permissions(self):
    ep = _load_entrypoint_module()
    archive_path = os.path.join(self.temp_dir, "perm_test.tar.gz")
    with tarfile.open(archive_path, "w:gz") as tar:
      ti_dir = tarfile.TarInfo(name="pkg")
      ti_dir.type = tarfile.DIRTYPE
      ti_dir.mode = 0o555
      ti_dir.uid = 1001
      ti_dir.gid = 1001
      tar.addfile(ti_dir)

      ti_file = tarfile.TarInfo(name="pkg/readonly.py")
      ti_file.type = tarfile.REGTYPE
      ti_file.mode = 0o444
      ti_file.size = 12
      ti_file.uid = 1001
      ti_file.gid = 1001
      import io
      tar.addfile(ti_file, io.BytesIO(b"print('ok')\n"))

    extract_dest = os.path.join(self.temp_dir, "extracted_repo")
    with mock.patch.object(ep.os, "chown") as mock_chown:
      ep.unpack_tar_gz_preserving_permissions(archive_path, extract_dest)

      expected_uid = os.getuid()
      expected_gid = os.getgid()
      mock_chown.assert_any_call(extract_dest, expected_uid, expected_gid)
      mock_chown.assert_any_call(
          os.path.join(extract_dest, "pkg"), expected_uid, expected_gid
      )
      mock_chown.assert_any_call(
          os.path.join(extract_dest, "pkg", "readonly.py"),
          expected_uid,
          expected_gid,
      )

    file_mode = stat.S_IMODE(
        os.stat(os.path.join(extract_dest, "pkg", "readonly.py")).st_mode
    )
    self.assertEqual(file_mode & 0o600, 0o600)

    dir_mode = stat.S_IMODE(
        os.stat(os.path.join(extract_dest, "pkg")).st_mode
    )
    self.assertEqual(dir_mode & 0o700, 0o700)

  def test_transit_driver_priority_order(self):
    ep = _load_entrypoint_module()
    dest_file = os.path.join(self.temp_dir, "downloaded.txt")

    # 1. Tier 1: Python google-cloud-storage SDK succeeds first
    fake_blob = mock.MagicMock()
    fake_bucket = mock.MagicMock()
    fake_bucket.blob.return_value = fake_blob
    fake_client = mock.MagicMock()
    fake_client.bucket.return_value = fake_bucket
    fake_storage_mod = types.ModuleType("google.cloud.storage")
    fake_storage_mod.Client = mock.MagicMock(return_value=fake_client)
    fake_cloud_mod = types.ModuleType("google.cloud")
    fake_cloud_mod.storage = fake_storage_mod
    fake_google_mod = types.ModuleType("google")
    fake_google_mod.cloud = fake_cloud_mod

    with mock.patch.dict(
        sys.modules,
        {
            "google": fake_google_mod,
            "google.cloud": fake_cloud_mod,
            "google.cloud.storage": fake_storage_mod,
        },
    ), mock.patch.object(ep.subprocess, "run") as mock_subproc:
      ok = ep.transit_download("gs://my-bucket/path/to/obj.tar.gz", dest_file)
      self.assertTrue(ok)
      fake_client.bucket.assert_called_once_with("my-bucket")
      fake_bucket.blob.assert_called_once_with("path/to/obj.tar.gz")
      fake_blob.download_to_filename.assert_called_once_with(dest_file)
      mock_subproc.assert_not_called()

    # 2. Tier 2 & 3: SDK fails -> falls back to gcloud storage -> falls back to gsutil
    fake_blob.download_to_filename.side_effect = RuntimeError("SDK unavailable")
    with mock.patch.dict(
        sys.modules,
        {
            "google": fake_google_mod,
            "google.cloud": fake_cloud_mod,
            "google.cloud.storage": fake_storage_mod,
        },
    ), mock.patch.object(ep.shutil, "which", return_value="/usr/bin/fake"), mock.patch.object(
        ep.subprocess,
        "run",
        side_effect=[
            subprocess.CompletedProcess(args=["gcloud"], returncode=1),
            subprocess.CompletedProcess(args=["gsutil"], returncode=0),
        ],
    ) as mock_subproc:
      ok = ep.transit_download("gs://my-bucket/path/to/obj.tar.gz", dest_file)
      self.assertTrue(ok)
      self.assertEqual(mock_subproc.call_count, 2)
      self.assertEqual(mock_subproc.call_args_list[0][0][0][:2], ["gcloud", "storage"])
      self.assertEqual(mock_subproc.call_args_list[1][0][0][0], "gsutil")

  def test_cli_flag_equals_syntax_and_missing_arg_bounds_check(self):
    # 1. Verify --flag=value syntax works
    result_eq = self._run_entrypoint("--log-level=debug", "version")
    self.assertEqual(result_eq.returncode, 0, f"Stderr: {result_eq.stderr}")

    # 2. Verify trailing flag without value fails cleanly with code 2 (no IndexError)
    result_missing = self._run_entrypoint("--manifest-uri")
    self.assertEqual(result_missing.returncode, 2)
    self.assertNotIn("IndexError", result_missing.stderr)

    receipt_file = os.path.join(self.output_dir, "receipt.json")
    self.assertTrue(os.path.exists(receipt_file))
    with open(receipt_file, "r", encoding="utf-8") as f:
      receipt = json.load(f)
    self.assertEqual(receipt["status"], "FAILURE")
    self.assertEqual(receipt["exit_code"], 2)
    self.assertIn("requires an argument", receipt["error"])

  def test_unhandled_crash_emits_failure_receipt(self):
    # Trigger an unhandled FileNotFoundError in run_logged by clearing PATH
    result = self._run_entrypoint("version", env={"PATH": ""})
    self.assertNotEqual(result.returncode, 0)

    receipt_file = os.path.join(self.output_dir, "receipt.json")
    self.assertTrue(os.path.exists(receipt_file))
    with open(receipt_file, "r", encoding="utf-8") as f:
      receipt = json.load(f)
    self.assertEqual(receipt["status"], "FAILURE")
    self.assertEqual(receipt["exit_code"], 1)
    self.assertIsNotNone(receipt["error"])
    self.assertIn("FALLBACK LOG SPILL", result.stderr)

  def test_tar_path_traversal_rejected(self):
    import io

    traversal_tar = os.path.join(self.temp_dir, "traversal.tar.gz")
    with tarfile.open(traversal_tar, "w:gz") as tar:
      info = tarfile.TarInfo(name="../escaped.txt")
      payload = b"malicious"
      info.size = len(payload)
      tar.addfile(info, io.BytesIO(payload))

    result = self._run_entrypoint(
        "init", env={"PAYLOAD_URI": f"file://{traversal_tar}"}
    )
    self.assertEqual(result.returncode, 2)
    self.assertFalse(
        os.path.exists(os.path.join(self.workspace_dir, "escaped.txt"))
    )

    receipt_file = os.path.join(self.output_dir, "receipt.json")
    with open(receipt_file, "r", encoding="utf-8") as f:
      receipt = json.load(f)
    self.assertEqual(receipt["status"], "FAILURE")
    self.assertEqual(receipt["exit_code"], 2)
    self.assertIn("Unsafe path traversal", receipt["error"])


if __name__ == "__main__":
  unittest.main()
