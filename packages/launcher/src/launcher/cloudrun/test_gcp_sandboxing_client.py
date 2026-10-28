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

"""Unit tests for launcher.cloudrun.client module."""

import io
import json
import os
import subprocess
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from launcher.cloudrun.client import (
    CloudRunClient,
    CloudRunConfig,
    CloudRunError,
    CloudRunTimeoutError,
    ExecutionReceipt,
    _package_workspace,
)


def _fake_popen(returncode=0, stdout="", stderr="", timeout=False):
  mock_proc = MagicMock()
  mock_proc.returncode = returncode
  mock_proc.poll.return_value = None if timeout else returncode
  mock_proc.stdout = io.StringIO(stdout)
  mock_proc.stderr = io.StringIO(stderr)
  if timeout:
    mock_proc.wait.side_effect = subprocess.TimeoutExpired(
        cmd="gcloud", timeout=10
    )
  else:
    mock_proc.wait.return_value = returncode
  return mock_proc


class TestCloudRunConfig(unittest.TestCase):

  def test_defaults_from_env(self):
    env = {
        "GOOGLE_CLOUD_PROJECT": "proj-123",
        "GCS_TRANSIT_BUCKET": "bucket-abc",
        "CLOUD_RUN_JOB_NAME": "my-job",
        "CLOUD_RUN_REGION": "us-east1",
    }
    with patch.dict(os.environ, env, clear=True):
      cfg = CloudRunConfig()
      self.assertEqual(cfg.project, "proj-123")
      self.assertEqual(cfg.bucket, "bucket-abc")
      self.assertEqual(cfg.job_name, "my-job")
      self.assertEqual(cfg.region, "us-east1")

  def test_bucket_fallback_from_project(self):
    env = {"GOOGLE_CLOUD_PROJECT": "proj-xyz"}
    with patch.dict(os.environ, env, clear=True):
      cfg = CloudRunConfig()
      self.assertEqual(cfg.bucket, "codemender-reports-proj-xyz")


class TestExecutionReceipt(unittest.TestCase):

  def test_receipt_fields_and_get(self):
    r = ExecutionReceipt(
        status="SUCCESS",
        exit_code=0,
        command="find-diff",
        finding_count=3,
        artifacts={"sarif": "output/report.sarif"},
    )
    self.assertEqual(r.get("status"), "SUCCESS")
    self.assertEqual(r.get("exit_code"), 0)
    self.assertEqual(r.get("finding_count"), 3)
    self.assertEqual(r.get("nonexistent", "fallback"), "fallback")


class TestCloudRunClient(unittest.TestCase):

  def test_run_missing_project_raises_error(self):
    cfg = CloudRunConfig(project="")
    client = CloudRunClient(cfg)
    with patch.dict(os.environ, {}, clear=True):
      with self.assertRaises(CloudRunError) as ctx:
        client.run("version")
      self.assertIn("GOOGLE_CLOUD_PROJECT", str(ctx.exception))

  @patch("launcher.cloudrun.client.subprocess.Popen")
  @patch("launcher.cloudrun.client._package_workspace")
  @patch("launcher.cloudrun.client._gcloud_transfer")
  def test_run_success(self, mock_transfer, mock_package, mock_popen):
    cfg = CloudRunConfig(
        project="test-project",
        bucket="test-bucket",
        job_name="codemender-runner",
        region="us-central1",
    )
    client = CloudRunClient(cfg)

    # Simulate gcloud run jobs execute succeeding
    mock_popen.return_value = _fake_popen(returncode=0)

    def fake_transfer(src, dest, check=True):
      if dest.endswith("receipt.json"):
        with open(dest, "w", encoding="utf-8") as f:
          json.dump(
              {
                  "status": "SUCCESS",
                  "exit_code": 0,
                  "command": "find-diff",
                  "finding_count": 0,
                  "artifacts": {"sarif": "output/report.sarif"},
              },
              f,
          )
        return True
      if dest.endswith("report.sarif"):
        with open(dest, "wb") as f:
          f.write(b'{"version": "2.1.0", "runs": []}')
        return True
      return True

    mock_transfer.side_effect = fake_transfer

    # When --repo is passed as container path '/workspace', it must fall back to host workspace
    receipt = client.run(
        "find-diff",
        ["--repo", "/workspace", "--base", "sha123", "--out", "/out"],
    )
    self.assertEqual(receipt.status, "SUCCESS")
    self.assertEqual(receipt.exit_code, 0)
    self.assertEqual(receipt.command, "find-diff")
    self.assertEqual(receipt.finding_count, 0)
    self.assertEqual(receipt.stdout, b'{"version": "2.1.0", "runs": []}')
    mock_package.assert_called_once_with(os.getcwd(), unittest.mock.ANY)

  @patch("launcher.cloudrun.client.subprocess.Popen")
  @patch("launcher.cloudrun.client._package_workspace")
  @patch("launcher.cloudrun.client._gcloud_transfer")
  def test_run_hermetic_config_no_environ(
      self, mock_transfer, mock_package, mock_popen
  ):
    with tempfile.TemporaryDirectory() as ws_dir, tempfile.TemporaryDirectory() as out_dir:
      cfg = CloudRunConfig(
          project="hermetic-proj",
          bucket="hermetic-bucket",
          job_name="codemender-runner",
          region="us-central1",
          workspace_dir=ws_dir,
          out_dir=out_dir,
          run_id="run-456",
          run_attempt="2",
          diff_workers="4",
          base_sha="base-abc",
      )
      client = CloudRunClient(cfg)
      mock_popen.return_value = _fake_popen(returncode=0)

      def fake_transfer(src, dest, check=True):
        if dest.endswith("receipt.json"):
          with open(dest, "w", encoding="utf-8") as f:
            json.dump(
                {"status": "SUCCESS", "exit_code": 0, "command": "find-diff"},
                f,
            )
        return True

      mock_transfer.side_effect = fake_transfer

      # Empty environment to prove hermetic execution without os.environ
      with patch.dict(os.environ, {}, clear=True):
        receipt = client.run(
            "find-diff", ["--repo", "/workspace", "--out", "/out"]
        )

      self.assertEqual(receipt.status, "SUCCESS")
      mock_package.assert_called_once_with(ws_dir, unittest.mock.ANY)

  @patch("launcher.cloudrun.client.subprocess.Popen")
  @patch("launcher.cloudrun.client._package_workspace")
  @patch("launcher.cloudrun.client._gcloud_transfer")
  def test_run_timeout_raises_timeout_error(
      self, mock_transfer, mock_package, mock_popen
  ):
    cfg = CloudRunConfig(project="p", bucket="b")
    client = CloudRunClient(cfg)

    mock_popen.return_value = _fake_popen(timeout=True)
    with self.assertRaises(CloudRunTimeoutError):
      client.run("find-diff", timeout_seconds=10)

  @patch("launcher.cloudrun.client.subprocess.Popen")
  @patch("launcher.cloudrun.client._package_workspace")
  @patch("launcher.cloudrun.client._gcloud_transfer")
  def test_run_timeout_triggers_remote_cancel(
      self, mock_transfer, mock_package, mock_popen
  ):
    cfg = CloudRunConfig(
        project="test-proj",
        bucket="test-bucket",
        job_name="codemender-runner",
        region="us-central1",
    )
    client = CloudRunClient(cfg)

    mock_popen.return_value = _fake_popen(
        stderr="Execution [codemender-runner-ab12cd34] has successfully started running.\n",
        timeout=True,
    )

    with patch.object(client, "cancel") as mock_cancel:
      with self.assertRaises(CloudRunTimeoutError):
        client.run("find-diff", timeout_seconds=10)

      mock_cancel.assert_called_once_with("codemender-runner-ab12cd34")
      self.assertEqual(client.current_execution, "codemender-runner-ab12cd34")

  @patch("launcher.cloudrun.client.subprocess.Popen")
  @patch("launcher.cloudrun.client._package_workspace")
  @patch("launcher.cloudrun.client._gcloud_transfer")
  def test_run_captures_execution_name_up_front_and_cancel_terminates_process(
      self, mock_transfer, mock_package, mock_popen
  ):
    cfg = CloudRunConfig(
        project="test-proj",
        bucket="test-bucket",
        job_name="codemender-runner",
        region="us-central1",
    )
    client = CloudRunClient(cfg)
    mock_proc = _fake_popen(
        stderr="Execution [codemender-runner-early99] has successfully started running.\n"
    )
    mock_popen.return_value = mock_proc

    name_observed_during_wait = []

    def _wait_hook(timeout=None):
      # Give reader thread time to process the stream line
      import time
      for _ in range(50):
        if client.current_execution:
          name_observed_during_wait.append(client.current_execution)
          break
        time.sleep(0.01)
      return 0

    mock_proc.wait.side_effect = _wait_hook

    def fake_transfer(src, dest, check=True):
      if dest.endswith("receipt.json"):
        with open(dest, "w", encoding="utf-8") as f:
          json.dump(
              {"status": "SUCCESS", "exit_code": 0, "command": "find-diff"},
              f,
          )
      return True

    mock_transfer.side_effect = fake_transfer
    receipt = client.run("find-diff", ["--repo", "/workspace", "--out", "/out"])

    self.assertEqual(name_observed_during_wait, ["codemender-runner-early99"])
    self.assertEqual(client.current_execution, "codemender-runner-early99")
    self.assertEqual(receipt.status, "SUCCESS")

  @patch("launcher.cloudrun.client.subprocess.run")
  def test_cancel_executes_gcloud_executions_cancel(self, mock_subproc):
    cfg = CloudRunConfig(
        project="test-proj",
        bucket="test-bucket",
        job_name="codemender-runner",
        region="us-central1",
    )
    client = CloudRunClient(cfg)
    client.current_execution = "codemender-runner-ab12cd34"

    mock_subproc.return_value = MagicMock(returncode=0, stdout="", stderr="")

    res = client.cancel()
    self.assertTrue(res)
    mock_subproc.assert_called_once_with(
        [
            "gcloud",
            "run",
            "jobs",
            "executions",
            "cancel",
            "codemender-runner-ab12cd34",
            "--region=us-central1",
            "--project=test-proj",
            "--quiet",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

  def test_cancel_without_execution_name_returns_false(self):
    cfg = CloudRunConfig(project="test-proj", bucket="test-bucket")
    client = CloudRunClient(cfg)
    self.assertFalse(client.cancel())

  @patch("launcher.cloudrun.client.subprocess.run")
  @patch("launcher.cloudrun.client._package_workspace")
  @patch("launcher.cloudrun.client._gcloud_transfer")
  def test_run_failed_without_receipt_raises_error(
      self, mock_transfer, mock_package, mock_subproc
  ):
    cfg = CloudRunConfig(project="p", bucket="b")
    client = CloudRunClient(cfg)

    mock_subproc.return_value = MagicMock(
        returncode=1, stdout="", stderr="Cloud Run API container error"
    )
    mock_transfer.return_value = False

    with self.assertRaises(CloudRunError) as ctx:
      client.run("find-diff")
    self.assertIn("Cloud Run Job execution failed", str(ctx.exception))

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0003, SPEC-GHA-FIX
  @patch("launcher.cloudrun.client.subprocess.Popen")
  @patch("launcher.cloudrun.client._package_workspace")
  @patch("launcher.cloudrun.client._gcloud_transfer")
  def test_run_fix_stages_finding_and_downloads_diff_and_log(
      self, mock_transfer, mock_package, mock_popen
  ):
    with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as out_dir:
      finding_path = os.path.join(out_dir, "finding-001.sarif")
      with open(finding_path, "wb") as f:
        f.write(b'{"version": "2.1.0", "runs": []}')

      cfg = CloudRunConfig(
          project="test-project",
          bucket="test-bucket",
          job_name="codemender-runner",
          region="us-central1",
      )
      client = CloudRunClient(cfg)
      mock_popen.return_value = _fake_popen(returncode=0)

      uploaded_manifests = []
      transfer_calls = []
      diff_bytes = b"--- a/src/app.py\n+++ b/src/app.py\n"

      def fake_transfer(src, dest, check=True):
        del check
        transfer_calls.append((src, dest))
        if src.endswith("manifest.json"):
          with open(src, "r", encoding="utf-8") as mf:
            uploaded_manifests.append(json.load(mf))
          return True
        if dest.endswith("receipt.json"):
          with open(dest, "w", encoding="utf-8") as rf:
            json.dump(
                {
                    "status": "SUCCESS",
                    "exit_code": 0,
                    "command": "fix",
                    "artifacts": {"diff": "output/finding.diff"},
                },
                rf,
            )
          return True
        if src.endswith("/output/finding.diff"):
          os.makedirs(os.path.dirname(dest), exist_ok=True)
          with open(dest, "wb") as df:
            df.write(diff_bytes)
          return True
        if src.endswith("/output/runner.log"):
          os.makedirs(os.path.dirname(dest), exist_ok=True)
          with open(dest, "w", encoding="utf-8") as lf:
            lf.write("fix runner log\n")
          return True
        return True

      mock_transfer.side_effect = fake_transfer

      receipt = client.run(
          "fix",
          [
              "--repo",
              repo_dir,
              "--finding",
              finding_path,
              "--out",
              out_dir,
          ],
      )

      self.assertEqual(receipt.status, "SUCCESS")
      self.assertEqual(receipt.exit_code, 0)
      self.assertEqual(receipt.command, "fix")
      self.assertEqual(len(uploaded_manifests), 1)
      manifest = uploaded_manifests[0]
      self.assertEqual(manifest.get("command"), "fix")
      self.assertIn("finding_uri", manifest)
      self.assertTrue(
          manifest["finding_uri"].startswith("gs://test-bucket/"),
          f"Unexpected finding_uri: {manifest.get('finding_uri')}",
      )
      self.assertTrue(
          manifest["finding_uri"].endswith("/input/finding.sarif"),
          f"Unexpected finding_uri: {manifest.get('finding_uri')}",
      )
      self.assertTrue(
          any(
              src == finding_path and dest == manifest["finding_uri"]
              for src, dest in transfer_calls
          ),
          f"Expected finding_path upload to {manifest['finding_uri']}, got {transfer_calls}",
      )
      diff_out_path = os.path.join(out_dir, "finding-001.diff")
      self.assertTrue(os.path.isfile(diff_out_path))
      with open(diff_out_path, "rb") as df:
        self.assertEqual(df.read(), diff_bytes)
      log_out_path = os.path.join(out_dir, "runner.log")
      self.assertTrue(os.path.isfile(log_out_path))
      with open(log_out_path, "r", encoding="utf-8") as lf:
        self.assertEqual(lf.read(), "fix runner log\n")

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0003, SPEC-GHA-FIX
  @patch("launcher.cloudrun.client.subprocess.Popen")
  @patch("launcher.cloudrun.client._package_workspace")
  @patch("launcher.cloudrun.client._gcloud_transfer")
  def test_workspace_tarball_reused_across_find_diff_and_fix_runs(
      self, mock_transfer, mock_package, mock_popen
  ):
    with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as out_dir:
      finding_path = os.path.join(out_dir, "finding-001.sarif")
      with open(finding_path, "wb") as f:
        f.write(b"{}")

      cfg = CloudRunConfig(
          project="test-project",
          bucket="test-bucket",
          job_name="codemender-runner",
          region="us-central1",
      )
      client = CloudRunClient(cfg)
      mock_popen.side_effect = [
          _fake_popen(returncode=0),
          _fake_popen(returncode=0),
      ]

      uploaded_manifests = []
      code_tarball_uploads = []

      def fake_transfer(src, dest, check=True):
        del check
        if dest.endswith("/input/code.tar.gz"):
          code_tarball_uploads.append(dest)
          return True
        if src.endswith("manifest.json"):
          with open(src, "r", encoding="utf-8") as mf:
            uploaded_manifests.append(json.load(mf))
          return True
        if dest.endswith("receipt.json"):
          with open(dest, "w", encoding="utf-8") as rf:
            json.dump({"status": "SUCCESS", "exit_code": 0}, rf)
          return True
        return True

      mock_transfer.side_effect = fake_transfer

      client.run("find-diff", ["--repo", repo_dir, "--out", out_dir])
      client.run(
          "fix",
          ["--repo", repo_dir, "--finding", finding_path, "--out", out_dir],
      )

      mock_package.assert_called_once_with(repo_dir, unittest.mock.ANY)
      self.assertEqual(len(code_tarball_uploads), 1)
      self.assertEqual(len(uploaded_manifests), 2)
      self.assertEqual(
          uploaded_manifests[0]["payload_uri"],
          uploaded_manifests[1]["payload_uri"],
      )
      self.assertNotEqual(
          uploaded_manifests[0]["output_prefix"],
          uploaded_manifests[1]["output_prefix"],
      )

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0003, SPEC-GHA-FIX
  @patch("launcher.cloudrun.client._gcloud_cancel_execution", return_value=True)
  def test_cancel_cancels_all_active_executions(self, mock_cancel_exec):
    cfg = CloudRunConfig(
        project="test-proj",
        bucket="test-bucket",
        job_name="codemender-runner",
        region="us-central1",
    )
    client = CloudRunClient(cfg)
    client._active_executions = {  # pylint: disable=protected-access
        "codemender-runner-exec-1",
        "codemender-runner-exec-2",
    }

    res = client.cancel()
    self.assertTrue(res)
    cancelled_names = {c.args[0] for c in mock_cancel_exec.call_args_list}
    self.assertEqual(
        cancelled_names,
        {"codemender-runner-exec-1", "codemender-runner-exec-2"},
    )

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0003, SPEC-GHA-FIX
  @patch("launcher.cloudrun.client._gcloud_cancel_execution", return_value=True)
  @patch("launcher.cloudrun.client.subprocess.Popen")
  @patch("launcher.cloudrun.client._package_workspace")
  @patch("launcher.cloudrun.client._gcloud_transfer")
  def test_run_timeout_without_own_exec_name_does_not_cancel_other_thread_execution(
      self, mock_transfer, mock_package, mock_popen, mock_cancel_exec
  ):
    cfg = CloudRunConfig(
        project="test-proj",
        bucket="test-bucket",
        job_name="codemender-runner",
        region="us-central1",
    )
    client = CloudRunClient(cfg)
    client.current_execution = "codemender-runner-other-thread"

    timed_out_proc = _fake_popen(stderr="", stdout="", timeout=True)
    timed_out_proc.poll.return_value = None
    mock_popen.return_value = timed_out_proc

    with self.assertRaises(CloudRunTimeoutError):
      client.run("fix", timeout_seconds=1)

    mock_cancel_exec.assert_not_called()
    timed_out_proc.kill.assert_called_once()

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0003, SPEC-GHA-FIX
  @patch("launcher.cloudrun.client._gcloud_cancel_execution", return_value=True)
  @patch("launcher.cloudrun.client.subprocess.Popen")
  @patch("launcher.cloudrun.client._package_workspace")
  @patch("launcher.cloudrun.client._gcloud_transfer")
  def test_run_timeout_with_own_exec_name_kills_own_proc_without_terminating_other_current_process(
      self, mock_transfer, mock_package, mock_popen, mock_cancel_exec
  ):
    cfg = CloudRunConfig(
        project="test-proj",
        bucket="test-bucket",
        job_name="codemender-runner",
        region="us-central1",
    )
    client = CloudRunClient(cfg)

    timed_out_proc = _fake_popen(
        stderr="Execution [codemender-runner-own123] has successfully started running.\n",
        timeout=True,
    )
    timed_out_proc.poll.return_value = None
    other_thread_proc = MagicMock()
    other_thread_proc.poll.return_value = None

    def wait_and_simulate_other_thread_popen(timeout=None):
      client.current_process = other_thread_proc
      raise subprocess.TimeoutExpired(cmd="gcloud", timeout=timeout or 1)

    timed_out_proc.wait.side_effect = wait_and_simulate_other_thread_popen
    mock_popen.return_value = timed_out_proc

    with self.assertRaises(CloudRunTimeoutError):
      client.run("fix", timeout_seconds=1)

    mock_cancel_exec.assert_called_once_with(
        "codemender-runner-own123",
        region="us-central1",
        project="test-proj",
    )
    timed_out_proc.kill.assert_called_once()
    other_thread_proc.terminate.assert_not_called()

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0003, SPEC-GHA-FIX
  @patch("launcher.cloudrun.client._gcloud_cancel_execution", return_value=True)
  def test_cancel_does_not_block_on_payload_lock(self, mock_cancel_exec):
    cfg = CloudRunConfig(
        project="test-proj",
        bucket="test-bucket",
        job_name="codemender-runner",
        region="us-central1",
    )
    client = CloudRunClient(cfg)
    client._active_executions = {"codemender-runner-exec-1"}  # pylint: disable=protected-access

    # Simulate another thread holding _payload_lock while packaging/uploading
    payload_lock_held = threading.Event()
    release_payload_lock = threading.Event()

    def hold_payload_lock():
      with client._payload_lock:  # pylint: disable=protected-access
        payload_lock_held.set()
        release_payload_lock.wait(timeout=5)

    holder = threading.Thread(target=hold_payload_lock, daemon=True)
    holder.start()
    self.assertTrue(payload_lock_held.wait(timeout=2))
    try:
      res = client.cancel()
      self.assertTrue(res)
      mock_cancel_exec.assert_called_once()
    finally:
      release_payload_lock.set()
      holder.join(timeout=2)

  # Governing: docs/openspec/specs/github-actions-verify/spec.md#req-0006, SPEC-GHA-VERIFY
  @patch("launcher.cloudrun.client.subprocess.Popen")
  @patch("launcher.cloudrun.client._package_workspace")
  @patch("launcher.cloudrun.client._gcloud_transfer")
  def test_run_verify_uploads_finding_and_downloads_verified_sarif(
      self, mock_transfer, mock_package, mock_popen
  ):
    with tempfile.TemporaryDirectory() as ws_dir, tempfile.TemporaryDirectory() as out_dir:
      input_sarif_path = os.path.join(out_dir, "finding.sarif")
      with open(input_sarif_path, "wb") as f:
        f.write(b'{"version": "2.1.0", "runs": [{"results": [{"ruleId": "R1"}]}]}')

      verified_sarif_bytes = (
          b'{"version": "2.1.0", "runs": [{"results": [{"ruleId": "R1",'
          b' "properties": {"status": "DISMISSED", "dismiss_reason": "safe"}}]}]}'
      )

      cfg = CloudRunConfig(
          project="test-proj",
          bucket="test-bucket",
          job_name="codemender-runner",
          region="us-central1",
          workspace_dir=ws_dir,
          out_dir=out_dir,
      )
      client = CloudRunClient(cfg)
      mock_popen.return_value = _fake_popen(returncode=0)

      uploaded_finding_uris = []
      uploaded_manifests = []

      def fake_transfer(src, dest, check=True):
        del check
        if dest.endswith("/input/finding.sarif"):
          uploaded_finding_uris.append((src, dest))
          return True
        if src.endswith("manifest.json"):
          with open(src, "r", encoding="utf-8") as mf:
            uploaded_manifests.append(json.load(mf))
          return True
        if dest.endswith("receipt.json"):
          with open(dest, "w", encoding="utf-8") as rf:
            json.dump(
                {"status": "SUCCESS", "exit_code": 0, "command": "verify"}, rf
            )
          return True
        if src.endswith("/output/finding.sarif") and dest.endswith("finding.sarif"):
          with open(dest, "wb") as sf:
            sf.write(verified_sarif_bytes)
          return True
        return True

      mock_transfer.side_effect = fake_transfer

      receipt = client.run(
          "verify",
          ["--repo", "/workspace", "--finding", "/out/finding.sarif", "--out", "/out"],
      )

      self.assertEqual(receipt.status, "SUCCESS")
      self.assertEqual(receipt.exit_code, 0)
      self.assertEqual(receipt.command, "verify")
      self.assertEqual(receipt.stdout, verified_sarif_bytes)
      self.assertEqual(len(uploaded_finding_uris), 1)
      self.assertEqual(uploaded_finding_uris[0][0], input_sarif_path)
      self.assertEqual(len(uploaded_manifests), 1)
      self.assertIn("finding_uri", uploaded_manifests[0])
      with open(os.path.join(out_dir, "finding.sarif"), "rb") as f:
        self.assertEqual(f.read(), verified_sarif_bytes)
      mock_package.assert_called_once_with(ws_dir, unittest.mock.ANY)

  # Governing: docs/openspec/specs/github-actions-verify/spec.md#req-0006, SPEC-GHA/REQ-0008
  @patch("launcher.cloudrun.client.subprocess.Popen")
  @patch("launcher.cloudrun.client._package_workspace")
  @patch("launcher.cloudrun.client._gcloud_transfer")
  def test_run_verify_with_custom_finding_name_writes_canonical_and_custom_sarif(
      self, mock_transfer, mock_package, mock_popen
  ):
    with tempfile.TemporaryDirectory() as ws_dir, tempfile.TemporaryDirectory() as out_dir:
      input_sarif_path = os.path.join(out_dir, "custom-finding.sarif")
      with open(input_sarif_path, "wb") as f:
        f.write(b'{"version": "2.1.0", "runs": [{"results": [{"ruleId": "R1"}]}]}')

      verified_sarif_bytes = (
          b'{"version": "2.1.0", "runs": [{"results": [{"ruleId": "R1",'
          b' "properties": {"status": "VERIFIED"}}]}]}'
      )

      cfg = CloudRunConfig(
          project="test-proj",
          bucket="test-bucket",
          job_name="codemender-runner",
          region="us-central1",
          workspace_dir=ws_dir,
          out_dir=out_dir,
      )
      client = CloudRunClient(cfg)
      mock_popen.return_value = _fake_popen(returncode=0)

      def fake_transfer(src, dest, check=True):
        del check
        if dest.endswith("receipt.json"):
          with open(dest, "w", encoding="utf-8") as rf:
            json.dump(
                {"status": "SUCCESS", "exit_code": 0, "command": "verify"}, rf
            )
          return True
        if src.endswith("/output/finding.sarif") and dest.endswith("finding.sarif"):
          with open(dest, "wb") as sf:
            sf.write(verified_sarif_bytes)
          return True
        return True

      mock_transfer.side_effect = fake_transfer

      client.run(
          "verify",
          [
              "--repo",
              "/workspace",
              "--finding",
              "/out/custom-finding.sarif",
              "--out",
              "/out",
          ],
      )

      for filename in ("finding.sarif", "custom-finding.sarif"):
        with open(os.path.join(out_dir, filename), "rb") as f:
          self.assertEqual(f.read(), verified_sarif_bytes)
      mock_package.assert_called_once_with(ws_dir, unittest.mock.ANY)

  def test_execution_receipt_resolve_stdout_falls_back_to_finding_sarif(self):
    from launcher.cloudrun.client import ExecutionReceipt  # pylint: disable=g-import-not-at-top

    with tempfile.TemporaryDirectory() as out_dir:
      finding_path = os.path.join(out_dir, "finding.sarif")
      payload = b'{"version": "2.1.0", "runs": []}'
      with open(finding_path, "wb") as f:
        f.write(payload)

      with patch.dict(os.environ, {"CODEMENDER_OUT_DIR": out_dir}, clear=False):
        receipt = ExecutionReceipt(command="verify")
        self.assertEqual(receipt.stdout, payload)


class TestPackageWorkspace(unittest.TestCase):

  def setUp(self):
    self.tmp_dir = tempfile.mkdtemp()
    self.repo_dir = os.path.join(self.tmp_dir, "repo")
    os.makedirs(os.path.join(self.repo_dir, "src"), exist_ok=True)
    os.makedirs(os.path.join(self.repo_dir, ".cm_project"), exist_ok=True)
    os.makedirs(os.path.join(self.repo_dir, ".exploit"), exist_ok=True)
    os.makedirs(os.path.join(self.repo_dir, "__pycache__"), exist_ok=True)

    with open(os.path.join(self.repo_dir, "src", "app.py"), "w") as f:
      f.write("print('hello')\n")
    with open(os.path.join(self.repo_dir, "gha-creds-12345.json"), "w") as f:
      f.write('{"type": "service_account"}\n')
    with open(os.path.join(self.repo_dir, ".cm_project", "state.json"), "w") as f:
      f.write('{"project": "test"}\n')
    with open(os.path.join(self.repo_dir, ".exploit", "poc.sh"), "w") as f:
      f.write('#!/bin/sh\n')
    with open(os.path.join(self.repo_dir, "__pycache__", "app.cpython-311.pyc"), "w") as f:
      f.write('binarydata\n')

    self.tarball_path = os.path.join(self.tmp_dir, "out", "code.tar.gz")

  def tearDown(self):
    import shutil
    shutil.rmtree(self.tmp_dir, ignore_errors=True)

  def _assert_excluded(self, members):
    names = [m.replace("\\", "/").lstrip("./") for m in members]
    self.assertTrue(any("src/app.py" in n for n in names), f"src/app.py missing from archive: {names}")
    for n in names:
      self.assertFalse(n.startswith("gha-creds-"), f"Found credential file in tarball: {n}")
      self.assertFalse(n.startswith(".cm_project"), f"Found .cm_project in tarball: {n}")
      self.assertFalse(n.startswith(".exploit"), f"Found .exploit in tarball: {n}")
      self.assertFalse("__pycache__" in n, f"Found __pycache__ in tarball: {n}")

  def test_package_workspace_tar_command_excludes_sensitive_files(self):
    _package_workspace(self.repo_dir, self.tarball_path)
    self.assertTrue(os.path.isfile(self.tarball_path))
    with tarfile.open(self.tarball_path, "r:gz") as tar:
      members = [m.name for m in tar.getmembers()]
    self._assert_excluded(members)

  def test_package_workspace_python_fallback_excludes_sensitive_files(self):
    with patch("shutil.which", return_value=None):
      _package_workspace(self.repo_dir, self.tarball_path)
    self.assertTrue(os.path.isfile(self.tarball_path))
    with tarfile.open(self.tarball_path, "r:gz") as tar:
      members = [m.name for m in tar.getmembers()]
    self._assert_excluded(members)


if __name__ == "__main__":
  unittest.main()
