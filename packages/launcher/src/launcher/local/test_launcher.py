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

# Governing: ADR-0008, SPEC-GHA/REQ-0006
"""Unit tests for launcher interface, LocalDockerLauncher, and CloudRunLauncher."""

import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from launcher.base import LauncherError, ParallelTask, RunResult
from launcher.cloudrun.cloud_run import (
    CloudRunError,
    CloudRunLauncher,
    CloudRunTimeoutError,
)
from launcher.local.local_docker import LocalDockerLauncher

_WAIT_SECONDS = 5

_VERSION_EVENT = {
    "event": "version",
    "orchestrator": "0.1.0",
    "cm": {"found": True, "version": "0.9.0", "path": "/usr/local/bin/cm"},
    "contract": "cm-0.9.x",
}


def _name_flag(argv):
  return argv[argv.index("--name") + 1]


class TestLocalDockerLauncher(unittest.TestCase):

  _ALLOWED_ENV_KEYS = frozenset({
      "GIT_CONFIG_COUNT",
      "GIT_CONFIG_KEY_0",
      "GIT_CONFIG_VALUE_0",
      "GOOGLE_APPLICATION_CREDENTIALS",
      "GOOGLE_CLOUD_PROJECT",
  })

  def assert_no_host_env_or_escalation(self, argv):
    self.assertNotIn("--env", argv)
    self.assertNotIn("--env-file", argv)
    self.assertFalse(any("TOKEN" in a for a in argv))
    for i, token in enumerate(argv):
      if token == "-e":
        self.assertLess(i + 1, len(argv))
        kv = argv[i + 1]
        self.assertIn("=", kv, "Every -e flag must be explicit KEY=VALUE")
        key, _ = kv.split("=", 1)
        self.assertIn(key, self._ALLOWED_ENV_KEYS)
    self.assertFalse(any(a.startswith("--privileged") for a in argv))
    self.assertFalse(any(a.startswith("--security-opt") for a in argv))
    self.assertFalse(any("unconfined" in a for a in argv))

  def test_requires_image_and_valid_runtime(self):
    with self.assertRaises(ValueError):
      LocalDockerLauncher(image="")
    for runtime in ("", "kata", "RUNSC", "runsc --privileged"):
      with self.subTest(runtime=runtime):
        with self.assertRaises(ValueError):
          LocalDockerLauncher(image="img:sha", runtime=runtime)

  def test_build_command_runsc_and_runc(self):
    runsc = LocalDockerLauncher(
        image="img:sha", log_level="debug", runtime="runsc"
    ).build_command("find-diff", container_name="cm-test")
    self.assertEqual(
        runsc,
        [
            "docker", "run", "--rm", "--name", "cm-test",
            "--runtime=runsc", "--cap-add", "SYS_ADMIN",
            "img:sha", "--log-level", "debug", "find-diff",
        ],
    )
    self.assert_no_host_env_or_escalation(runsc)

    runc = LocalDockerLauncher(
        image="img:sha", log_level="debug", runtime="runc"
    ).build_command("find-diff", container_name="cm-test")
    self.assertEqual(
        runc,
        [
            "docker", "run", "--rm", "--name", "cm-test",
            "img:sha", "--log-level", "debug", "find-diff",
        ],
    )
    self.assertNotIn("--cap-add", runc)
    self.assert_no_host_env_or_escalation(runc)

  def test_build_command_with_mounts_and_gcp_auth(self):
    launcher = LocalDockerLauncher(
        image="img:sha",
        log_level="info",
        runtime="runsc",
        workspace_dir="/tmp/work/workspace",
        cm_home_dir="/tmp/work/cm-home",
        out_dir="/tmp/work/out",
        gcp_credentials_file="/tmp/creds/adc.json",
        gcp_project="my-gcp-proj",
    )
    argv = launcher.build_command(
        "find-diff",
        ["--repo", "/workspace", "--base", "abc1234", "--out", "/out"],
        container_name="cm-test",
    )
    image_at = argv.index("img:sha")
    for idx, tok in enumerate(argv):
      if tok in ("-v", "-e", "--name", "--runtime=runsc"):
        self.assertLess(idx, image_at)
    self.assertIn("/tmp/work/workspace:/workspace", argv)
    self.assertIn("/tmp/work/cm-home:/root/.codemender", argv)
    self.assertIn("/tmp/work/out:/out", argv)
    self.assertIn("/tmp/creds/adc.json:/creds/adc.json:ro", argv)
    self.assert_no_host_env_or_escalation(argv)

  @patch("launcher.local.local_docker.subprocess.run")
  def test_run_returns_raw_stdout_bytes_without_parsing(self, mock_run):
    diff = (
        b"diff --git a/app.py b/app.py\r\n"
        b"@@ -1 +1 @@\r\n-print('caf\xe9')\r\n+print('ok')\r\n"
    )
    mock_run.return_value = MagicMock(returncode=0, stdout=diff)

    result = LocalDockerLauncher(image="img:sha").run(
        "fix", ["--repo", "/workspace", "--finding", "/out/finding-001.sarif"]
    )

    self.assertTrue(result.ok)
    self.assertEqual(result.stdout, diff)
    kwargs = mock_run.call_args.kwargs
    for text_mode in ("text", "universal_newlines", "encoding", "errors"):
      self.assertNotIn(text_mode, kwargs)

  @patch("launcher.local.local_docker.subprocess.run")
  def test_log_path_receives_stderr_and_unopenable_raises(self, mock_run):
    def _fake_run(argv, **kwargs):
      del argv
      kwargs["stderr"].write(b"cm-runner log line\n")
      return MagicMock(returncode=0, stdout=b"")

    mock_run.side_effect = _fake_run
    with tempfile.TemporaryDirectory() as temp_dir:
      log_path = os.path.join(temp_dir, "fix-001.log")
      LocalDockerLauncher(image="img:sha").run("fix", log_path=log_path)
      with open(log_path, "rb") as f:
        self.assertEqual(f.read(), b"cm-runner log line\n")

      bad_log = os.path.join(temp_dir, "missing", "fix.log")
      with self.assertRaisesRegex(LauncherError, "log file"):
        LocalDockerLauncher(image="img:sha").run("fix", log_path=bad_log)

  @patch("launcher.local.local_docker.subprocess.run")
  def test_timeout_kills_the_container(self, mock_run):
    mock_run.side_effect = [
        subprocess.TimeoutExpired(cmd="docker", timeout=1),
        MagicMock(returncode=0, stderr=b""),
    ]
    launcher = LocalDockerLauncher(image="img:sha", timeout_seconds=1)
    with self.assertRaises(LauncherError):
      launcher.run("fix")
    run_call, kill_call = mock_run.call_args_list
    self.assertEqual(
        kill_call.args[0], ["docker", "kill", _name_flag(run_call.args[0])]
    )

  @patch("launcher.local.local_docker.subprocess.run")
  def test_stop_kills_running_containers_including_with_dirs_children(
      self, mock_run
  ):
    launcher = LocalDockerLauncher(image="img:sha")
    child = launcher.with_dirs(
        workspace_dir="/tmp/f/w", cm_home_dir="/tmp/f/h", out_dir="/tmp/f/o"
    )
    started = threading.Semaphore(0)
    killed = threading.Event()

    def _fake_run(argv, **kwargs):
      del kwargs
      if argv[1] == "kill":
        killed.set()
        return MagicMock(returncode=0, stderr=b"")
      started.release()
      killed.wait(_WAIT_SECONDS)
      return MagicMock(returncode=137, stdout=b"")

    mock_run.side_effect = _fake_run
    workers = [
        threading.Thread(target=lambda l=l: l.run("fix"), daemon=True)
        for l in (launcher, child)
    ]
    for w in workers:
      w.start()
    for _ in workers:
      self.assertTrue(started.acquire(timeout=_WAIT_SECONDS))

    launcher.stop()
    for w in workers:
      w.join(_WAIT_SECONDS)
      self.assertFalse(w.is_alive())

    calls = [c.args[0] for c in mock_run.call_args_list]
    run_names = sorted(_name_flag(argv) for argv in calls if argv[1] == "run")
    self.assertEqual(
        [argv for argv in calls if argv[1] == "kill"],
        [["docker", "kill", *run_names]],
    )

  @patch("launcher.local.local_docker.subprocess.run")
  def test_run_parallel_stages_isolated_folders_and_runs_tasks(self, mock_run):
    staged_workspaces = []

    def _fake_run(argv, **kwargs):
      for idx, tok in enumerate(argv):
        if tok == "-v" and argv[idx + 1].endswith(":/workspace"):
          ws = argv[idx + 1].split(":")[0]
          staged_workspaces.append(sorted(os.listdir(ws)))
        if tok == "-v" and argv[idx + 1].endswith(":/out"):
          out = argv[idx + 1].split(":")[0]
          for f in os.listdir(out):
            if f.endswith(".sarif"):
              stem = f[:-6]
              with open(os.path.join(out, f"{stem}.diff"), "wb") as df:
                df.write(f"patch-{stem}".encode("utf-8"))
      kwargs["stderr"].write(b"fix log\n")
      return MagicMock(returncode=0, stdout=b"")

    mock_run.side_effect = _fake_run
    temp_dir = tempfile.mkdtemp(prefix="launcher-parallel-test-")
    try:
      src_ws = os.path.join(temp_dir, "src")
      os.makedirs(src_ws)
      for name in ("app.py", "gha-creds-secret.json", ".cm_project"):
        with open(os.path.join(src_ws, name), "w", encoding="utf-8") as f:
          f.write("x")

      launcher = LocalDockerLauncher(image="img:sha")
      tasks = [
          ParallelTask(
              key=f"finding-{i:03d}",
              command="fix",
              args=["--repo", "/workspace"],
              input_files={f"finding-{i:03d}.sarif": b"{}"},
              output_file=f"finding-{i:03d}.diff",
          )
          for i in (1, 2)
      ]
      results = list(
          launcher.run_parallel(
              tasks,
              src_workspace=src_ws,
              temp_root=temp_dir,
              timeout_seconds=60,
              max_workers=2,
          )
      )
      self.assertEqual(sorted(r.key for r in results), ["finding-001", "finding-002"])
      self.assertEqual(staged_workspaces, [["app.py"], ["app.py"]])
      for r in results:
        self.assertTrue(r.ok)
        self.assertEqual(r.output, f"patch-{r.key}".encode("utf-8"))
        self.assertEqual(r.log, "fix log\n")
    finally:
      shutil.rmtree(temp_dir, ignore_errors=True)


class TestCloudRunLauncher(unittest.TestCase):

  def test_requires_client(self):
    for falsey_client in (None, False, "", 0):
      with self.subTest(client=falsey_client):
        with self.assertRaises(ValueError):
          CloudRunLauncher(client=falsey_client)

  def test_run_success(self):
    mock_client = MagicMock()
    mock_receipt = MagicMock(exit_code=0, events=[_VERSION_EVENT])
    mock_client.run.return_value = mock_receipt

    launcher = CloudRunLauncher(client=mock_client)
    result = launcher.run("version")

    self.assertTrue(result.ok)
    self.assertEqual(result.exit_code, 0)
    self.assertEqual(result.events, [_VERSION_EVENT])
    mock_client.run.assert_called_once_with(
        command="version",
        args=[],
        log_level="info",
        timeout_seconds=3600,
    )

  def test_client_error_raises_launcher_error(self):
    mock_client = MagicMock()
    mock_client.run.side_effect = CloudRunError("API failure")

    launcher = CloudRunLauncher(client=mock_client)
    with self.assertRaises(LauncherError) as ctx:
      launcher.run("find-diff", ["--repo", "/workspace"])
    self.assertIn("API failure", str(ctx.exception))

  def test_timeout_raises_launcher_error(self):
    mock_client = MagicMock()
    mock_client.run.side_effect = CloudRunTimeoutError("Timed out")

    launcher = CloudRunLauncher(client=mock_client, timeout_seconds=3600)
    with self.assertRaises(LauncherError) as ctx:
      launcher.run("find-diff", ["--repo", "/workspace"])
    self.assertIn("timed out", str(ctx.exception).lower())

  def test_run_propagates_failure_exit_code(self):
    mock_client = MagicMock()
    mock_receipt = MagicMock(exit_code=2, events=[])
    mock_client.run.return_value = mock_receipt

    launcher = CloudRunLauncher(client=mock_client)
    result = launcher.run("find-diff", ["--repo", "/workspace"])

    self.assertFalse(result.ok)
    self.assertEqual(result.exit_code, 2)
    self.assertEqual(result.events, [])

  def test_run_success_with_bootstrapper_receipt(self):
    mock_client = MagicMock()
    sarif_bytes = b'{"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "CodeMender"}}, "results": []}]}'
    mock_receipt = MagicMock(
        status="SUCCESS",
        exit_code=0,
        command="find-diff",
        finding_count=0,
        stdout=sarif_bytes,
        events=[],
    )
    mock_client.run.return_value = mock_receipt

    launcher = CloudRunLauncher(client=mock_client)
    result = launcher.run("find-diff")

    self.assertTrue(result.ok)
    self.assertEqual(result.exit_code, 0)
    self.assertEqual(result.stdout, sarif_bytes)

  def test_run_bootstrapper_failure_exit_code(self):
    mock_client = MagicMock()
    mock_receipt = MagicMock(
        status="FAILURE",
        exit_code=2,
        error="Extraction failed",
    )
    mock_client.run.return_value = mock_receipt

    launcher = CloudRunLauncher(client=mock_client)
    result = launcher.run("find-diff")

    self.assertFalse(result.ok)
    self.assertEqual(result.exit_code, 2)

  def test_stop_invokes_client_cancel(self):
    mock_client = MagicMock()
    launcher = CloudRunLauncher(client=mock_client)
    launcher.stop()
    mock_client.cancel.assert_called_once()

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0003, SPEC-GHA-FIX
  def test_run_isolated_rewrites_container_paths_and_returns_diff_and_log(self):
    mock_client = MagicMock()
    diff_bytes = b"--- a/f.py\n+++ b/f.py\n"
    observed = {}

    def _fake_client_run(command, args, log_level="info", timeout_seconds=3600):
      observed["command"] = command
      observed["args"] = list(args)
      observed["log_level"] = log_level
      observed["timeout_seconds"] = timeout_seconds
      out_idx = args.index("--out") + 1
      out_dir = args[out_idx]
      finding_idx = args.index("--finding") + 1
      finding_path = args[finding_idx]
      observed["finding_exists"] = os.path.isfile(finding_path)
      with open(os.path.join(out_dir, "finding-001.diff"), "wb") as df:
        df.write(diff_bytes)
      with open(os.path.join(out_dir, "runner.log"), "w", encoding="utf-8") as lf:
        lf.write("cloudrun fix log\n")
      return MagicMock(exit_code=0, stdout=diff_bytes, error=None)

    mock_client.run.side_effect = _fake_client_run
    launcher = CloudRunLauncher(client=mock_client, log_level="debug", timeout_seconds=1800)

    with tempfile.TemporaryDirectory() as temp_dir:
      src_ws = os.path.join(temp_dir, "src-repo")
      os.makedirs(src_ws)
      task = ParallelTask(
          key="finding-001",
          command="fix",
          args=[
              "--repo",
              "/workspace",
              "--finding",
              "/out/finding-001.sarif",
              "--out",
              "/out",
          ],
          input_files={"finding-001.sarif": b"{}"},
          output_file="finding-001.diff",
      )
      result = launcher.run_isolated(
          task,
          src_workspace=src_ws,
          temp_root=temp_dir,
          timeout_seconds=1200,
      )

    self.assertTrue(result.ok)
    self.assertEqual(result.key, "finding-001")
    self.assertEqual(result.exit_code, 0)
    self.assertEqual(result.output, diff_bytes)
    self.assertEqual(result.log, "cloudrun fix log\n")
    self.assertEqual(result.error, "")
    self.assertEqual(observed["command"], "fix")
    self.assertEqual(observed["timeout_seconds"], 1200)
    self.assertTrue(observed["finding_exists"])
    args = observed["args"]
    self.assertEqual(args[args.index("--repo") + 1], src_ws)
    self.assertNotEqual(args[args.index("--out") + 1], "/out")
    self.assertTrue(args[args.index("--finding") + 1].endswith("finding-001.sarif"))
    self.assertNotEqual(args[args.index("--finding") + 1], "/out/finding-001.sarif")

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0003, SPEC-GHA-FIX
  def test_run_isolated_exit_code_3_no_fix(self):
    mock_client = MagicMock()

    def _fake_client_run(command, args, log_level="info", timeout_seconds=3600):
      del command, log_level, timeout_seconds
      out_dir = args[args.index("--out") + 1]
      with open(os.path.join(out_dir, "runner.log"), "w", encoding="utf-8") as lf:
        lf.write("no patch produced\n")
      return MagicMock(exit_code=3, stdout=b"", error="No fix found")

    mock_client.run.side_effect = _fake_client_run
    launcher = CloudRunLauncher(client=mock_client)

    with tempfile.TemporaryDirectory() as temp_dir:
      task = ParallelTask(
          key="finding-001",
          command="fix",
          args=["--repo", "/workspace", "--finding", "/out/finding-001.sarif", "--out", "/out"],
          input_files={"finding-001.sarif": b"{}"},
          output_file="finding-001.diff",
      )
      result = launcher.run_isolated(task, src_workspace=temp_dir, temp_root=temp_dir)

    self.assertFalse(result.ok)
    self.assertEqual(result.key, "finding-001")
    self.assertEqual(result.exit_code, 3)
    self.assertEqual(result.output, b"")
    self.assertEqual(result.log, "no patch produced\n")
    self.assertTrue(bool(result.error))

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0003, SPEC-GHA-FIX
  def test_run_isolated_catches_cloudrun_errors(self):
    for err in (CloudRunError("Job execution failed"), CloudRunTimeoutError("Job timed out")):
      with self.subTest(error=type(err).__name__):
        mock_client = MagicMock()
        mock_client.run.side_effect = err
        launcher = CloudRunLauncher(client=mock_client)
        with tempfile.TemporaryDirectory() as temp_dir:
          task = ParallelTask(
              key="finding-001",
              command="fix",
              args=["--repo", "/workspace", "--finding", "/out/finding-001.sarif", "--out", "/out"],
              input_files={"finding-001.sarif": b"{}"},
              output_file="finding-001.diff",
          )
          result = launcher.run_isolated(task, src_workspace=temp_dir, temp_root=temp_dir)
        self.assertFalse(result.ok)
        self.assertEqual(result.key, "finding-001")
        self.assertIsNone(result.exit_code)
        self.assertTrue(bool(result.error))

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0003, SPEC-GHA-FIX
  def test_run_parallel_executes_multiple_tasks(self):
    mock_client = MagicMock()

    def _fake_client_run(command, args, log_level="info", timeout_seconds=3600):
      del command, log_level, timeout_seconds
      out_dir = args[args.index("--out") + 1]
      finding_path = args[args.index("--finding") + 1]
      stem = os.path.splitext(os.path.basename(finding_path))[0]
      diff_bytes = f"--- a/{stem}.py\n+++ b/{stem}.py\n".encode("utf-8")
      with open(os.path.join(out_dir, f"{stem}.diff"), "wb") as df:
        df.write(diff_bytes)
      with open(os.path.join(out_dir, "runner.log"), "w", encoding="utf-8") as lf:
        lf.write(f"log for {stem}\n")
      return MagicMock(exit_code=0, stdout=diff_bytes, error=None)

    mock_client.run.side_effect = _fake_client_run
    launcher = CloudRunLauncher(client=mock_client)

    with tempfile.TemporaryDirectory() as temp_dir:
      tasks = [
          ParallelTask(
              key=f"finding-{i:03d}",
              command="fix",
              args=[
                  "--repo",
                  "/workspace",
                  "--finding",
                  f"/out/finding-{i:03d}.sarif",
                  "--out",
                  "/out",
              ],
              input_files={f"finding-{i:03d}.sarif": b"{}"},
              output_file=f"finding-{i:03d}.diff",
          )
          for i in (1, 2)
      ]
      results = list(
          launcher.run_parallel(
              tasks,
              src_workspace=temp_dir,
              temp_root=temp_dir,
              max_workers=2,
          )
      )

    self.assertEqual(sorted(r.key for r in results), ["finding-001", "finding-002"])
    for r in results:
      self.assertTrue(r.ok)
      self.assertEqual(r.exit_code, 0)
      self.assertEqual(r.output, f"--- a/{r.key}.py\n+++ b/{r.key}.py\n".encode("utf-8"))
      self.assertEqual(r.log, f"log for {r.key}\n")
      self.assertEqual(r.error, "")

  # Governing: docs/openspec/specs/github-actions-verify/spec.md#req-0006, SPEC-GHA-VERIFY
  def test_run_rewrites_container_paths_for_verify_when_config_dirs_set(self):
    mock_client = MagicMock()
    mock_client.config.workspace_dir = "/host/ws"
    mock_client.config.out_dir = "/host/out"
    sarif_bytes = b'{"version": "2.1.0", "runs": [{"results": [{"properties": {"status": "VERIFIED"}}]}]}'
    mock_client.run.return_value = MagicMock(
        status="SUCCESS", exit_code=0, stdout=sarif_bytes, events=[]
    )

    launcher = CloudRunLauncher(client=mock_client)
    res = launcher.run(
        "verify",
        ["--repo", "/workspace", "--finding", "/out/finding.sarif", "--out", "/out"],
    )

    self.assertTrue(res.ok)
    self.assertEqual(res.stdout, sarif_bytes)
    mock_client.run.assert_called_once_with(
        command="verify",
        args=[
            "--repo",
            "/host/ws",
            "--finding",
            "/host/out/finding.sarif",
            "--out",
            "/host/out",
        ],
        log_level="info",
        timeout_seconds=3600,
    )

  # Governing: docs/openspec/specs/github-actions-verify/spec.md#req-0006, SPEC-GHA-VERIFY
  def test_with_dirs_overrides_client_config_dirs(self):
    mock_client = MagicMock()
    mock_client.config.workspace_dir = "/host/ws"
    mock_client.config.out_dir = "/stale/out"
    sarif_bytes = b'{"version": "2.1.0", "runs": [{"results": [{"properties": {"status": "VERIFIED"}}]}]}'
    mock_client.run.return_value = MagicMock(
        status="SUCCESS", exit_code=0, stdout=sarif_bytes, events=[]
    )

    launcher = CloudRunLauncher(client=mock_client).with_dirs(out_dir="/override/out")
    res = launcher.run(
        "verify",
        ["--repo", "/workspace", "--finding", "/out/finding.sarif", "--out", "/out"],
    )

    self.assertTrue(res.ok)
    self.assertEqual(mock_client.config.out_dir, "/stale/out")
    mock_client.run.assert_called_once_with(
        command="verify",
        args=[
            "--repo",
            "/host/ws",
            "--finding",
            "/override/out/finding.sarif",
            "--out",
            "/override/out",
        ],
        log_level="info",
        timeout_seconds=3600,
    )


if __name__ == "__main__":
  unittest.main()
