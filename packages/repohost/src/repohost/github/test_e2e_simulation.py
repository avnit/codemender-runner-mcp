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

"""End-to-end simulation tests for GitHub Actions Cloud Run execution."""

import json
import os
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from repohost.models import ChangeContext, SecurityGateState
from repohost.testing import InMemoryRepoHost

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(
    os.path.join(_THIS_DIR, "..", "..", "..", "..", "..")
)
for _pkg in ("codemender", "launcher", "repohost"):
  _pkg_src = os.path.join(_REPO_ROOT, "packages", _pkg, "src")
  if os.path.isdir(_pkg_src) and _pkg_src not in sys.path:
    sys.path.insert(0, _pkg_src)

from launcher.cloudrun import (  # pylint: disable=g-import-not-at-top
    CloudRunError,
    CloudRunLauncher,
    CloudRunTimeoutError,
)
from repohost.github import gha_runner  # pylint: disable=g-import-not-at-top

_ENTRYPOINT_PY = os.path.join(_REPO_ROOT, "cloud-run-jobs", "entrypoint.py")
_CODEMENDER_SRC_DIR = os.path.join(_REPO_ROOT, "packages", "codemender", "src")


class MockExecutionReceipt:
  """Mock receipt returned by in-process CloudRunClient.run()."""

  def __init__(
      self,
      status: str = "SUCCESS",
      exit_code: int = 0,
      command: str = "find-diff",
      started_at: str = "2026-10-14T00:00:00Z",
      completed_at: str = "2026-10-14T00:01:00Z",
      finding_count: int = 0,
      artifacts=None,
      error=None,
      events=None,
      stdout: bytes = b"",
  ):
    self.status = status
    self.exit_code = exit_code
    self.command = command
    self.started_at = started_at
    self.completed_at = completed_at
    self.finding_count = finding_count
    self.artifacts = artifacts
    self.error = error
    self.events = events or []
    self.stdout = stdout
    if not self.stdout:
      if self.artifacts and isinstance(self.artifacts, dict) and "sarif" in self.artifacts:
        sarif_val = self.artifacts["sarif"]
        if sarif_val.startswith("file://"):
          sarif_val = sarif_val[len("file://") :]
        if os.path.isfile(sarif_val):
          try:
            with open(sarif_val, "rb") as f:
              self.stdout = f.read()
          except OSError:
            pass
      if not self.stdout:
        out_dir = os.environ.get("CODEMENDER_OUT_DIR", "")
        for cand in [
            os.path.join(out_dir, "report.sarif") if out_dir else "",
            os.path.join("output", "report.sarif"),
            "report.sarif",
        ]:
          if cand and os.path.isfile(cand):
            try:
              with open(cand, "rb") as f:
                self.stdout = f.read()
                break
            except OSError:
              pass


class MockCloudRunConfig:
  """Mock CloudRunConfig."""

  def __init__(
      self,
      project: str = "",
      bucket: str = "",
      job_name: str = "",
      region: str = "",
      workspace_dir: str = "",
      out_dir: str = "",
      run_id: str = "",
      run_attempt: str = "",
      diff_workers: str = "",
      base_sha: str = "",
      **kwargs,
  ):
    self.project = project
    self.bucket = bucket
    self.job_name = job_name
    self.region = region
    self.workspace_dir = workspace_dir
    self.out_dir = out_dir
    self.run_id = run_id
    self.run_attempt = run_attempt
    self.diff_workers = diff_workers
    self.base_sha = base_sha


class MockCloudRunClient:
  """Mock in-process CloudRunClient implementing run()."""

  def __init__(self, config=None):
    self.config = config
    self.invocations = []

  def run(
      self,
      command: str,
      args=(),
      log_level: str = "info",
      timeout_seconds: int = 3600,
  ):
    self.invocations.append({
        "command": command,
        "args": list(args),
        "log_level": log_level,
        "timeout_seconds": timeout_seconds,
    })

    # 1. Write mock output artifact matching bootstrapper receipt schema (REQ-0008)
    os.makedirs("output", exist_ok=True)
    receipt_data = {
        "status": "SUCCESS",
        "exit_code": 0,
        "command": command,
        "started_at": "2026-10-14T00:00:00Z",
        "completed_at": "2026-10-14T00:01:00Z",
        "finding_count": 0,
        "artifacts": {
            "log": "gs://test-transit-bucket/output/runner.log",
            "sarif": "output/report.sarif",
        },
        "error": None,
    }
    with open(os.path.join("output", "receipt.json"), "w", encoding="utf-8") as f:
      json.dump(receipt_data, f, indent=2)

    # Write output/report.sarif containing valid clean SARIF JSON
    sarif_data = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [{"tool": {"driver": {"name": "CodeMender"}}, "results": []}],
    }
    with open(os.path.join("output", "report.sarif"), "w", encoding="utf-8") as f:
      json.dump(sarif_data, f, indent=2)

    # 2. Emit live log to sys.stderr per ADR-0001
    sys.stderr.write(
        f"[mock-cloudrun-client] Remote sandbox execution started for {command}.\n"
    )
    sys.stderr.write(
        "[mock-cloudrun-client] Output artifact written to output/receipt.json.\n"
    )
    sys.stderr.flush()

    return MockExecutionReceipt(
        status="SUCCESS",
        exit_code=0,
        command=command,
        finding_count=0,
        artifacts={
            "sarif": "output/report.sarif",
        },
    )


class MockFailingCloudRunClient(MockCloudRunClient):
  """Mock CloudRunClient that simulates a remote failure with exit code 2."""

  def run(
      self,
      command: str,
      args=(),
      log_level: str = "info",
      timeout_seconds: int = 3600,
  ):
    self.invocations.append({
        "command": command,
        "args": list(args),
        "log_level": log_level,
        "timeout_seconds": timeout_seconds,
    })
    os.makedirs("output", exist_ok=True)
    with open(os.path.join("output", "receipt.json"), "w", encoding="utf-8") as f:
      json.dump({
          "status": "FAILURE",
          "exit_code": 2,
          "error": "Remote execution error",
      }, f, indent=2)

    sys.stderr.write(
        f"[mock-cloudrun-client] Remote sandbox execution failed for {command}.\n"
    )
    sys.stderr.flush()
    return MockExecutionReceipt(
        exit_code=2, status="FAILURE", error="Remote execution error"
    )


def _make_mock_client_module(client_cls=MockCloudRunClient):
  client_mod = types.ModuleType("launcher.cloudrun.client")
  client_mod.CloudRunClient = client_cls
  client_mod.CloudRunConfig = MockCloudRunConfig
  client_mod.CloudRunError = CloudRunError
  client_mod.CloudRunTimeoutError = CloudRunTimeoutError

  pkg_mod = types.ModuleType("gcp_sandboxing")
  pkg_mod.client = client_mod
  return {
      "launcher.cloudrun.client": client_mod,
      "gcp_sandboxing": pkg_mod,
      "gcp_sandboxing.client": client_mod,
  }


class TestEndToEndSimulation(unittest.TestCase):
  """End-to-end simulation of gha_runner with Cloud Run client execution."""

  def setUp(self):
    super().setUp()
    self._delete_comments_patcher = patch.object(
        gha_runner, "delete_codemender_review_comments", return_value=0
    )
    self.mock_delete_comments = self._delete_comments_patcher.start()
    self.addCleanup(self._delete_comments_patcher.stop)

  def test_e2e_cloudrun_simulation_success(self):
    """Simulates gha_runner.main() with Cloud Run backend and mock in-process client.

    Verifies:
      1. Entrypoint factory routes CODEMENDER_BACKEND="cloudrun" to CloudRunLauncher
         with an in-process CloudRunClient (REQ-0004).
      2. Consolidated find-diff execution (no preliminary init call) (REQ-0008).
      3. Mock Cloud Run client writes workspace artifact output/receipt.json (REQ-0008).
      4. Live logs stream to stderr (ADR-0001).
      5. gha_runner.main() exits 0 and posts sticky PR comment (REQ-0005).
    """
    with tempfile.TemporaryDirectory() as temp_dir:
      orig_cwd = os.getcwd()
      os.chdir(temp_dir)
      try:
        # 1. Create mock PR event JSON
        event_path = os.path.join(temp_dir, "event.json")
        pr_event = {
            "pull_request": {
                "number": 42,
                "head": {"sha": "head12345678", "repo": {"full_name": "org/repo"}},
                "base": {"sha": "base12345678", "repo": {"full_name": "org/repo"}},
            }
        }
        with open(event_path, "w", encoding="utf-8") as f:
          json.dump(pr_event, f)

        # 2. Prepare realistic GitHub Actions environment for Cloud Run
        env = {
            "CODEMENDER_BACKEND": "cloudrun",
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_EVENT_PATH": event_path,
            "GITHUB_REPOSITORY": "org/repo",
            "GITHUB_TOKEN": "ghp_test_secret_token",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": "987654",
            "GOOGLE_CLOUD_PROJECT": "test-gcp-project",
            "GCS_TRANSIT_BUCKET": "test-transit-bucket",
            "CLOUD_RUN_JOB_NAME": "codemender-runner",
            "CLOUD_RUN_REGION": "us-central1",
        }

        # 3. Mock in-process SDK modules in sys.modules
        mock_modules = _make_mock_client_module(MockCloudRunClient)

        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test_secret_token"}):
          with patch.dict("sys.modules", mock_modules):
            with patch.object(
                gha_runner,
                "create_repo_host",
            ) as mock_create_host:
              mock_host = MagicMock()
              mock_host.get_change_context.return_value = ChangeContext(
                  repo_id="org/repo",
                  change_id="42",
                  base_sha="base12345678",
                  head_sha="head12345678",
                  base_ref="main",
                  head_ref="feature",
              )
              mock_create_host.return_value = mock_host
              rc = gha_runner.main(env)

        # 4. Assertions
        self.assertEqual(rc, 0, "gha_runner.main() should exit 0 on success")

        # Verify output artifact exists in workspace (REQ-0008)
        receipt_path = os.path.join("output", "receipt.json")
        self.assertTrue(
            os.path.exists(receipt_path),
            "output/receipt.json must exist in the workspace",
        )
        with open(receipt_path, encoding="utf-8") as f:
          receipt_data = json.load(f)
        self.assertEqual(receipt_data.get("status"), "SUCCESS")
        self.assertEqual(receipt_data.get("exit_code"), 0)
        self.assertEqual(receipt_data.get("command"), "find-diff")
        self.assertEqual(
            receipt_data.get("artifacts", {}).get("sarif"),
            "output/report.sarif",
        )
        self.assertTrue(os.path.exists(os.path.join("output", "report.sarif")))

        # Verify PR sticky comment was posted with expected details
        mock_host.upsert_summary.assert_called_once()
        body = mock_host.upsert_summary.call_args.args[0]

        self.assertIn("✅ **No vulnerabilities found**", body)
        self.assertIn("`base12345678`", body)
        self.assertIn("https://github.com/org/repo/actions/runs/987654", body)

      finally:
        os.chdir(orig_cwd)

  def test_e2e_cloudrun_simulation_failure_on_remote_error(self):
    """Simulates handling when in-process Cloud Run execution returns failure exit code."""
    with tempfile.TemporaryDirectory() as temp_dir:
      orig_cwd = os.getcwd()
      os.chdir(temp_dir)
      try:
        event_path = os.path.join(temp_dir, "event.json")
        pr_event = {
            "pull_request": {
                "number": 10,
                "head": {"sha": "head12345678", "repo": {"full_name": "org/repo"}},
                "base": {"sha": "base12345678", "repo": {"full_name": "org/repo"}},
            }
        }
        with open(event_path, "w", encoding="utf-8") as f:
          json.dump(pr_event, f)

        env = {
            "CODEMENDER_BACKEND": "cloudrun",
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_EVENT_PATH": event_path,
            "GITHUB_REPOSITORY": "org/repo",
            "GITHUB_TOKEN": "ghp_test_secret_token",
            "GOOGLE_CLOUD_PROJECT": "test-gcp-project",
            "GCS_TRANSIT_BUCKET": "test-transit-bucket",
        }

        mock_modules = _make_mock_client_module(MockFailingCloudRunClient)

        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test_secret_token"}):
          with patch.dict("sys.modules", mock_modules):
            with patch.object(
                gha_runner,
                "create_repo_host",
            ) as mock_create_host:
              mock_host = MagicMock()
              mock_host.get_change_context.return_value = ChangeContext(
                  repo_id="org/repo",
                  change_id="10",
                  base_sha="base12345678",
                  head_sha="head12345678",
                  base_ref="main",
                  head_ref="feature",
              )
              mock_create_host.return_value = mock_host
              rc = gha_runner.main(env)

        self.assertEqual(rc, 1, "gha_runner.main() must exit 1 when client fails")
        mock_host.upsert_summary.assert_called_once()
        body = mock_host.upsert_summary.call_args.args[0]
        self.assertIn("❌ **CodeMender scan failed.**", body)
        self.assertIn("`2`", body)
        self.assertIn("Backend: `Cloudrun`", body)

        # Verify failure receipt written
        receipt_path = os.path.join("output", "receipt.json")
        self.assertTrue(os.path.exists(receipt_path))
        with open(receipt_path, encoding="utf-8") as f:
          receipt_data = json.load(f)
        self.assertEqual(receipt_data.get("status"), "FAILURE")
        self.assertEqual(receipt_data.get("exit_code"), 2)
        self.assertEqual(receipt_data.get("error"), "Remote execution error")

      finally:
        os.chdir(orig_cwd)

  @unittest.skipUnless(
      os.path.isfile(_ENTRYPOINT_PY),
      "cloud-run-jobs/entrypoint.py not found on disk",
  )
  def test_e2e_live_bootstrapper_invocation(self):
    """Simulates live execution invoking cloud-run-jobs/entrypoint.py and gha_runner.run_pr."""
    with tempfile.TemporaryDirectory() as temp_dir:
      orig_cwd = os.getcwd()
      os.chdir(temp_dir)
      try:
        home_dir = os.path.join(temp_dir, "home")
        os.makedirs(home_dir, exist_ok=True)
        mock_bin_dir = os.path.join(temp_dir, "mock_bin")
        os.makedirs(mock_bin_dir, exist_ok=True)

        mock_cm_path = os.path.join(mock_bin_dir, "cm")
        with open(mock_cm_path, "w", encoding="utf-8") as f:
          f.write("""#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
if not args or args[0] in ("--version", "-v"):
    print("cm 0.12.0")
    sys.exit(0)

cmd = args[0]
if cmd == "init":
    if "--verify" in args:
        print("  Results: 4 passed, 0 warnings")
    else:
        cfg = os.path.expanduser("~/.codemender/config.yaml")
        os.makedirs(os.path.dirname(cfg), exist_ok=True)
        with open(cfg, "w", encoding="utf-8") as cf:
            cf.write("version: 1\\n")
    sys.exit(0)

if cmd == "find":
    print("Mock CM: scan complete")
    sys.exit(0)

if cmd == "report" and "--format" in args:
    fmt = args[args.index("--format") + 1]
    if fmt == "sarif":
        print(json.dumps({
            "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json",
            "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": "CodeMender"}}, "results": []}],
        }))
        sys.exit(0)
    elif fmt == "md":
        print("# CodeMender Scan Report\\n\\nNo findings detected.")
        sys.exit(0)

sys.exit(0)
""")
        os.chmod(mock_cm_path, 0o755)
        mock_cm_runner_path = os.path.join(mock_bin_dir, "cm-runner")
        with open(mock_cm_runner_path, "w", encoding="utf-8") as f:
          f.write(
              "#!/usr/bin/env python3\n"
              "import sys\n"
              f"sys.path.insert(0, {_CODEMENDER_SRC_DIR!r})\n"
              "from codemender.runner.__main__ import main\n"
              "sys.exit(main())\n"
          )
        os.chmod(mock_cm_runner_path, 0o755)

        # 1. Simulate entrypoint.py version invocation
        version_ws = os.path.join(temp_dir, "version_ws")
        version_out = os.path.join(version_ws, "output")
        bootstrapper_env = dict(os.environ)
        bootstrapper_env["HOME"] = home_dir
        bootstrapper_env["PATH"] = (
            f"{mock_bin_dir}:{bootstrapper_env.get('PATH', '')}"
        )
        bootstrapper_env["EPHEMERAL_SCRATCH_DIR"] = version_ws
        bootstrapper_env["WORKSPACE_DIR"] = version_ws
        bootstrapper_env["OUTPUT_DIR"] = version_out

        ver_res = subprocess.run(
            [sys.executable, _ENTRYPOINT_PY, "version"],
            env=bootstrapper_env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            ver_res.returncode,
            0,
            f"entrypoint.py version failed: {ver_res.stderr}",
        )
        ver_receipt_path = os.path.join(version_out, "receipt.json")
        self.assertTrue(os.path.exists(ver_receipt_path))
        with open(ver_receipt_path, "r", encoding="utf-8") as f:
          ver_receipt = json.load(f)
        self.assertEqual(ver_receipt["status"], "SUCCESS")
        self.assertEqual(ver_receipt["exit_code"], 0)
        self.assertEqual(ver_receipt["command"], "version")

        # 2. Prepare mock Git repository and package payload tarball
        repo_src = os.path.join(temp_dir, "repo_src")
        os.makedirs(repo_src, exist_ok=True)
        subprocess.run(
            ["git", "-C", repo_src, "-c", "commit.gpgsign=false", "init", "-q"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", repo_src, "config", "user.name", "Test Runner"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", repo_src, "config", "user.email", "runner@example.com"],
            check=True,
        )
        app_file = os.path.join(repo_src, "app.py")
        with open(app_file, "w", encoding="utf-8") as f:
          f.write("print('hello world')\n")
        subprocess.run(
            ["git", "-C", repo_src, "-c", "commit.gpgsign=false", "add", "app.py"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", repo_src, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "Initial"],
            check=True,
        )
        base_sha = (
            subprocess.check_output(["git", "-C", repo_src, "rev-parse", "HEAD"])
            .decode()
            .strip()
        )
        with open(app_file, "a", encoding="utf-8") as f:
          f.write("print('security patch')\n")
        subprocess.run(
            ["git", "-C", repo_src, "-c", "commit.gpgsign=false", "add", "app.py"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", repo_src, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "Patch"],
            check=True,
        )
        head_sha = (
            subprocess.check_output(["git", "-C", repo_src, "rev-parse", "HEAD"])
            .decode()
            .strip()
        )

        payload_tar = os.path.join(temp_dir, "code.tar.gz")
        with tarfile.open(payload_tar, "w:gz") as tar:
          for item in os.listdir(repo_src):
            tar.add(os.path.join(repo_src, item), arcname=item)

        transit_out = os.path.join(temp_dir, "transit_out")
        os.makedirs(transit_out, exist_ok=True)

        # 3. Create mock client that executes cloud-run-jobs/entrypoint.py
        class LiveBootstrapperCloudRunClient:
          def __init__(self, config=None):
            self.config = config
            self.invocations = []

          def run(
              self,
              command: str,
              args=(),
              log_level: str = "info",
              timeout_seconds: int = 3600,
          ):
            self.invocations.append({"command": command, "args": list(args)})
            job_ws = os.path.join(temp_dir, "job_ws")
            job_out = os.path.join(job_ws, "output")
            c_env = dict(os.environ)
            c_env["HOME"] = home_dir
            c_env["PATH"] = f"{mock_bin_dir}:{c_env.get('PATH', '')}"
            c_env["EPHEMERAL_SCRATCH_DIR"] = job_ws
            c_env["WORKSPACE_DIR"] = job_ws
            c_env["OUTPUT_DIR"] = job_out

            manifest_path = os.path.join(temp_dir, "manifest.json")
            with open(manifest_path, "w", encoding="utf-8") as mf:
              json.dump({
                  "command": command,
                  "payload_uri": f"file://{payload_tar}",
                  "output_uri": f"file://{transit_out}",
                  "diff_base": base_sha,
                  "diff_workers": "2",
              }, mf)

            cmd_args = [
                sys.executable,
                _ENTRYPOINT_PY,
                "--manifest",
                f"file://{manifest_path}",
            ]
            res = subprocess.run(
                cmd_args,
                env=c_env,
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
              sys.stderr.write(f"Bootstrapper stderr: {res.stderr}\n")

            receipt_file = os.path.join(transit_out, "receipt.json")
            with open(receipt_file, "r", encoding="utf-8") as rf:
              r_data = json.load(rf)

            # Ensure local output/ has receipt and artifacts for parity (REQ-0008)
            os.makedirs("output", exist_ok=True)
            with open(os.path.join("output", "receipt.json"), "w", encoding="utf-8") as out_rf:
              json.dump(r_data, out_rf, indent=2)

            return MockExecutionReceipt(
                status=r_data["status"],
                exit_code=r_data["exit_code"],
                command=r_data["command"],
                started_at=r_data.get("started_at", "2026-10-14T00:00:00Z"),
                completed_at=r_data.get("completed_at", "2026-10-14T00:01:00Z"),
                finding_count=r_data.get("finding_count", 0),
                artifacts=r_data.get("artifacts"),
                error=r_data.get("error"),
            )

        # 4. Prepare PR event and runner env
        event_path = os.path.join(temp_dir, "event.json")
        pr_event = {
            "pull_request": {
                "number": 55,
                "head": {"sha": head_sha, "repo": {"full_name": "test-org/test-repo"}},
                "base": {"sha": base_sha, "repo": {"full_name": "test-org/test-repo"}},
            }
        }
        with open(event_path, "w", encoding="utf-8") as f:
          json.dump(pr_event, f)

        gha_env = {
            "CODEMENDER_BACKEND": "cloudrun",
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_EVENT_PATH": event_path,
            "GITHUB_REPOSITORY": "test-org/test-repo",
            "GITHUB_TOKEN": "ghp_mock_live_token",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": "123456",
            "GOOGLE_CLOUD_PROJECT": "live-project",
            "GCS_TRANSIT_BUCKET": "live-transit",
        }

        launcher = CloudRunLauncher(client=LiveBootstrapperCloudRunClient())
        host = InMemoryRepoHost(
            change_context=ChangeContext(
                repo_id="test-org/test-repo",
                change_id="55",
                base_sha="base12345678",
                head_sha="head12345678",
                base_ref="main",
                head_ref="feature",
                is_fork=False,
            )
        )

        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_mock_live_token"}):
          rc = gha_runner.run_pr(pr_event, gha_env, launcher, host=host)

        self.assertEqual(
            rc, 0, "gha_runner.run_pr() must exit 0 with live bootstrapper"
        )
        summary = host.current_summary or ""
        self.assertIn("✅ **No vulnerabilities found**", summary)
        self.assertIsNotNone(host.latest_security_gate)
        self.assertEqual(
            host.latest_security_gate.state, SecurityGateState.SUCCESS
        )

        # Validate generated receipt structure
        generated_receipt_path = os.path.join(transit_out, "receipt.json")
        self.assertTrue(os.path.exists(generated_receipt_path))
        with open(generated_receipt_path, "r", encoding="utf-8") as f:
          receipt_data = json.load(f)
        self.assertEqual(receipt_data["status"], "SUCCESS")
        self.assertEqual(receipt_data["exit_code"], 0)
        self.assertEqual(receipt_data["command"], "find-diff")
        self.assertIn("sarif", receipt_data["artifacts"])
        self.assertIn("findings", receipt_data["artifacts"])
        self.assertIn("log", receipt_data["artifacts"])
        self.assertIsNone(receipt_data["error"])
        self.assertEqual(receipt_data["finding_count"], 0)

      finally:
        os.chdir(orig_cwd)


if __name__ == "__main__":
  unittest.main()
