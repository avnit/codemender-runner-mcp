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

# Governing: ADR-0002
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0001
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0005
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0006
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0007
# Governing: SPEC-ARTIFACT-CACHE/REQ-0001, SPEC-ARTIFACT-CACHE/REQ-0002
# Governing: SPEC-ARTIFACT-CACHE/REQ-0004, SPEC-ARTIFACT-CACHE/REQ-0006
# Governing: SPEC-WORKER/REQ-0001, SPEC-WORKER/REQ-0002, SPEC-WORKER/REQ-0003
# Governing: SPEC-WORKER/REQ-0004, SPEC-WORKER/REQ-0005, SPEC-WORKER/REQ-0006
# Governing: SPEC-WORKER/REQ-0007
"""Unit tests for Worker (`launcher.worker`) and `cm-worker` CLI entrypoint."""

import io
import json
import logging
import os
from pathlib import Path
import signal
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock

from codemender.cm import CmAdapter
from codemender.runner.commands import find_diff
from codemender.runner.commands import fix
from codemender.runner.commands import init
from codemender.runner.commands import verify
from launcher.base import EXIT_EXECUTION_FAILURE
from launcher.base import EXIT_OK
from launcher.base import EXIT_USAGE_ERROR
from launcher.cache import CachePermissionError
from launcher.cache import CacheTransientError
from launcher.cache import LocalArtifactCache
from launcher.cache import RunManifest
from launcher.cache import ValidationError
from launcher.worker import DEFAULT_WORK_DIR
from launcher.worker import EXIT_SIGINT
from launcher.worker import EXIT_SIGTERM
from launcher.worker import Worker
from launcher.worker import WorkerConfig
from launcher.worker import main


def _create_repo_tar_gz(
    archive_path: Path,
    *,
    nested_wrapper: str | None = None,
    traversal_member: str | None = None,
) -> Path:
  """Creates a synthetic repository `.tar.gz` archive for testing."""
  prefix = f"{nested_wrapper}/" if nested_wrapper else ""
  with tarfile.open(archive_path, "w:gz") as tf:
    for dir_name in (f"{prefix}.git", f"{prefix}scripts"):
      d_info = tarfile.TarInfo(name=dir_name)
      d_info.type = tarfile.DIRTYPE
      d_info.mode = 0o755
      tf.addfile(d_info)

    head_bytes = b"ref: refs/heads/main\n"
    head_info = tarfile.TarInfo(name=f"{prefix}.git/HEAD")
    head_info.size = len(head_bytes)
    head_info.mode = 0o644
    tf.addfile(head_info, io.BytesIO(head_bytes))

    script_bytes = b"#!/bin/sh\necho build-ok\n"
    script_info = tarfile.TarInfo(name=f"{prefix}scripts/build.sh")
    script_info.size = len(script_bytes)
    script_info.mode = 0o755
    tf.addfile(script_info, io.BytesIO(script_bytes))

    if traversal_member is not None:
      bad_bytes = b"evil\n"
      bad_info = tarfile.TarInfo(name=traversal_member)
      bad_info.size = len(bad_bytes)
      bad_info.mode = 0o644
      tf.addfile(bad_info, io.BytesIO(bad_bytes))

  return archive_path


class TestWorkerCliAndConfig(unittest.TestCase):
  """Tests for WorkerConfig.from_cli and cm-worker CLI entrypoint."""

  def test_from_cli_and_validators(self):
    cfg = WorkerConfig.from_cli(
        ["--cache-uri", "  file:///tmp/cache  ", "--task-id", "  find  "]
    )
    self.assertEqual(cfg.cache_uri, "file:///tmp/cache")
    self.assertEqual(cfg.task_id, "find")
    self.assertEqual(cfg.work_dir, DEFAULT_WORK_DIR)

    with tempfile.TemporaryDirectory() as tmp:
      tmp_path = Path(tmp)
      real_dir = tmp_path / "real_work"
      real_dir.mkdir()
      sym_dir = tmp_path / "sym_work"
      sym_dir.symlink_to(real_dir)
      regular_file = tmp_path / "regular_file"
      regular_file.write_text("file", encoding="utf-8")
      cache = LocalArtifactCache(tmp_path / "cache")

      w = Path("/tmp/work")
      u = "file:///cache"
      invalid_kwargs = [
          {"cache_uri": "", "task_id": "find", "work_dir": w},
          {"cache_uri": 123, "task_id": "find", "work_dir": w},
          {"cache_uri": None, "task_id": "find", "work_dir": w},
          {"cache_uri": True, "task_id": "find", "work_dir": w},
          {"cache_uri": u, "task_id": "", "work_dir": w},
          {"cache_uri": u, "task_id": 123, "work_dir": w},
          {"cache_uri": u, "task_id": None, "work_dir": w},
          {"cache_uri": u, "task_id": True, "work_dir": w},
          {"cache_uri": u, "task_id": ".", "work_dir": w},
          {"cache_uri": u, "task_id": "..", "work_dir": w},
          {"cache_uri": u, "task_id": "../escape", "work_dir": w},
          {"cache_uri": u, "task_id": "a/b", "work_dir": w},
          {"cache_uri": u, "task_id": "a\\b", "work_dir": w},
          {"cache_uri": u, "task_id": ".tmp.staging", "work_dir": w},
          {"cache_uri": u, "task_id": "find", "work_dir": sym_dir},
          {"cache_uri": u, "task_id": "find", "work_dir": regular_file},
          {"cache_uri": u, "task_id": "find", "work_dir": ""},
          {"cache_uri": u, "task_id": "find", "work_dir": "   "},
          {"cache_uri": u, "task_id": "find", "work_dir": "/tmp/work\x00bad"},
          {"cache_uri": u, "task_id": "find", "work_dir": 123},
      ]
      for kwargs in invalid_kwargs:
        with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
          WorkerConfig(**kwargs)  # type: ignore[arg-type]

      for bad_work_dir in (
          "",
          "   ",
          "/tmp/work\x00bad",
          sym_dir,
          regular_file,
          123,
      ):
        with (
            self.subTest(bad_work_dir=bad_work_dir),
            self.assertRaises(ValidationError),
        ):
          Worker(cache, work_dir=bad_work_dir)  # type: ignore[arg-type]

  def test_cli_missing_or_invalid_args_return_2_with_clean_stdout(self):
    bad_argvs = [
        [],
        ["--cache-uri", "file:///tmp/cache"],
        ["--task-id", "find"],
        ["--cache-uri", "file:///tmp/cache", "--task-id", "../escape"],
        ["--cache-uri", "file://remotehost/tmp/cache", "--task-id", "find"],
        ["--cache-uri", "file:///tmp/cache", "--task-id", "find", "--unknown"],
    ]
    for argv in bad_argvs:
      with self.subTest(argv=argv):
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with (
            mock.patch.object(sys, "stdout", stdout_buf),
            mock.patch.object(sys, "stderr", stderr_buf),
        ):
          rc = main(argv)
        self.assertEqual(rc, EXIT_USAGE_ERROR)
        self.assertEqual(stdout_buf.getvalue(), "")
        self.assertNotEqual(stderr_buf.getvalue().strip(), "")

  def test_cli_cache_open_errors_map_to_exit_codes(self):
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with (
        mock.patch.object(sys, "stdout", stdout_buf),
        mock.patch.object(sys, "stderr", stderr_buf),
        mock.patch.dict(sys.modules, {"gcsfs": None}),
    ):
      rc = main(
          ["--cache-uri", "gs://missing-gcsfs/prefix", "--task-id", "find"]
      )
    self.assertEqual(rc, EXIT_EXECUTION_FAILURE)
    self.assertEqual(stdout_buf.getvalue(), "")

  def test_cli_end_to_end_local_file_uri_execution(self):
    with tempfile.TemporaryDirectory() as tmp:
      cache_dir, work_dir = Path(tmp) / "cache", Path(tmp) / "work"
      cache = LocalArtifactCache(cache_dir)
      cache.put_repo_archive(_create_repo_tar_gz(Path(tmp) / "code.tar.gz"))
      cache.put_manifest("find", RunManifest(
          command="find-diff", task_id="find",
          base_ref="origin/main", extra_args=("--no-init",),
      ))
      adapter = CmAdapter("/fake/bin/cm")
      adapter._version = "0.9.0"

      def _fake_find(cfg, cm, emitter=None) -> int:
        del emitter
        self.assertIs(cm, adapter)
        assert cfg.out is not None
        sarif = '{"version":"2.1.0","runs":[{"results":[{"ruleId":"R1"}]}]}'
        (cfg.out / "report.sarif").write_text(sarif, encoding="utf-8")
        (cfg.out / "report.md").write_text("# Summary\n", encoding="utf-8")
        return EXIT_OK

      stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
      argv = ["--cache-uri", cache_dir.as_uri(), "--task-id", "find",
              "--work-dir", str(work_dir)]
      with (
          mock.patch.object(sys, "stdout", stdout_buf),
          mock.patch.object(sys, "stderr", stderr_buf),
          mock.patch.object(CmAdapter, "locate", return_value=adapter),
          mock.patch.object(find_diff, "run", side_effect=_fake_find),
      ):
        self.assertEqual(main(argv), EXIT_OK)
      self.assertEqual(stdout_buf.getvalue(), "")
      receipt = cache.get_receipt("find")
      self.assertEqual((receipt.status, receipt.finding_count), ("SUCCESS", 1))
      self.assertEqual(receipt.artifacts, {
          "log": "tasks/find/output/runner.log",
          "sarif": "tasks/find/output/report.sarif",
          "report": "tasks/find/output/report.md",
      })


class TestWorkerTaskExecution(unittest.TestCase):
  """Behavioral tests for Worker 4-step execution, stream isolation, signals."""

  def setUp(self):
    super().setUp()
    self._tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self._tmp.cleanup)
    self.tmp_path = Path(self._tmp.name)
    self.cache_dir = self.tmp_path / "cache"
    self.work_dir = self.tmp_path / "work"
    self.cache = LocalArtifactCache(self.cache_dir)
    self.adapter = CmAdapter("/fake/bin/cm")

    self.archive_src = _create_repo_tar_gz(
        self.tmp_path / "code.tar.gz", nested_wrapper="my-repo"
    )
    self.cache.put_repo_archive(self.archive_src)

  def test_local_file_execution_without_cloud_or_repohost_imports(self):
    self.cache.put_manifest("init", RunManifest(command="init", task_id="init"))
    self.adapter._version = "0.9.0"

    def _fake_init(
        cfg: init.InitConfig, adapter: CmAdapter, emitter=None
    ) -> int:
      self.assertIs(adapter, self.adapter)
      self.assertIsNone(emitter)
      self.assertTrue((cfg.repo / ".git" / "HEAD").is_file())
      logging.getLogger("codemender.runner.commands.init").info(
          "init check [cm_binary] pass: init diagnostics"
      )
      return EXIT_OK

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with (
        mock.patch.dict(
            sys.modules,
            {"gcsfs": None, "google.cloud": None, "repohost": None},
        ),
        mock.patch.object(sys, "stdout", stdout_buf),
        mock.patch.object(sys, "stderr", stderr_buf),
        mock.patch.object(
            CmAdapter, "locate", return_value=self.adapter
        ) as mock_locate,
        mock.patch.object(init, "run", side_effect=_fake_init) as mock_init_run,
    ):
      worker = Worker(self.cache, work_dir=self.work_dir)
      rc = worker.run_task("init")
      self.assertIs(worker.adapter, self.adapter)
      rc2 = worker.run_task("init")

    mock_locate.assert_called_once()
    self.assertEqual(mock_init_run.call_count, 2)
    self.assertEqual(rc, EXIT_OK)
    self.assertEqual(rc2, EXIT_OK)
    self.assertEqual(stdout_buf.getvalue(), "")
    receipt = self.cache.get_receipt("init")
    self.assertEqual(receipt.status, "SUCCESS")
    self.assertEqual(receipt.exit_code, EXIT_OK)
    self.assertIsNone(receipt.error)
    log_text = self.cache.get_bytes(receipt.artifacts["log"]).decode("utf-8")
    self.assertIn("init check [cm_binary] pass", log_text)
    self.assertIn("init diagnostics", log_text)
    self.assertFalse(self.work_dir.exists())

  def test_find_diff_command_construction_and_stream_isolation(self):
    manifest = RunManifest(
        command="find-diff",
        task_id="find",
        base_ref="origin/main",
        diff_workers=4,
        log_level="debug",
        extra_args=("--no-init",),
    )
    self.cache.put_manifest("find", manifest)

    # Pre-populate stale file in work_dir to verify Step 1 cleanup.
    self.work_dir.mkdir(parents=True, exist_ok=True)
    (self.work_dir / "stale.txt").write_text("stale", encoding="utf-8")

    observed_workers: list[int] = []

    def _fake_find_diff(
        cfg: find_diff.FindDiffConfig, adapter: CmAdapter, emitter=None
    ) -> int:
      self.assertIs(adapter, self.adapter)
      self.assertEqual(cfg.base, "origin/main")
      assert cfg.diff_workers is not None
      observed_workers.append(cfg.diff_workers)
      self.assertIsNotNone(cfg.out)
      assert cfg.out is not None
      self.assertTrue((cfg.repo / ".git" / "HEAD").is_file())
      build_sh = cfg.repo / "scripts" / "build.sh"
      self.assertEqual(build_sh.stat().st_mode & 0o777, 0o755)
      self.assertFalse((cfg.repo / "inputs").exists())
      self.assertFalse((cfg.repo / "out").exists())

      sarif_doc = {
          "version": "2.1.0",
          "runs": [{
              "results": [
                  {"ruleId": "R1", "message": {"text": "active 1"}},
                  {"ruleId": "R2", "message": {"text": "active 2"}},
                  {"ruleId": "R3", "properties": {"status": "DISMISSED"}},
              ]
          }],
      }
      (cfg.out / "report.sarif").write_text(
          json.dumps(sarif_doc), encoding="utf-8"
      )
      (cfg.out / "report.md").write_text("# Scan Summary\n", encoding="utf-8")
      if emitter is not None:
        emitter.stream.write("DUPLICATE_STDOUT_SARIF_PAYLOAD\n")
        emitter.stream.flush()
      logging.getLogger("codemender.runner.commands.find_diff").debug(
          "find-diff base=%s diff_workers=%d", cfg.base, cfg.diff_workers
      )
      return EXIT_OK

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with (
        mock.patch.object(sys, "stdout", stdout_buf),
        mock.patch.object(sys, "stderr", stderr_buf),
        mock.patch.object(init, "run") as mock_init_run,
        mock.patch.object(
            find_diff, "run", side_effect=_fake_find_diff
        ) as mock_find_run,
    ):
      worker = Worker(self.cache, work_dir=self.work_dir, adapter=self.adapter)
      rc = worker.run_task("find")
      self.cache.put_manifest(
          "find-default",
          RunManifest(
              command="find-diff",
              task_id="find-default",
              base_ref="origin/main",
              diff_workers=None,
              extra_args=("--no-init",),
          ),
      )
      rc_default = worker.run_task("find-default")

    mock_init_run.assert_not_called()
    self.assertEqual(mock_find_run.call_count, 2)
    self.assertEqual(
        observed_workers, [4, find_diff.DEFAULT_DIFF_WORKERS]
    )
    self.assertEqual(rc, EXIT_OK)
    self.assertEqual(rc_default, EXIT_OK)
    self.assertEqual(stdout_buf.getvalue(), "")
    receipt = self.cache.get_receipt("find")
    self.assertEqual(receipt.status, "SUCCESS")
    self.assertEqual(receipt.finding_count, 2)
    self.assertEqual(
        receipt.artifacts,
        {
            "log": "tasks/find/output/runner.log",
            "sarif": "tasks/find/output/report.sarif",
            "report": "tasks/find/output/report.md",
        },
    )
    log_text = self.cache.get_bytes(receipt.artifacts["log"]).decode("utf-8")
    self.assertIn("find-diff base=origin/main diff_workers=4", log_text)
    self.assertNotIn("DUPLICATE_STDOUT_SARIF_PAYLOAD", log_text)

  def test_fix_and_verify_tasks_and_no_fix_exit_3_disambiguation(self):
    sarif_file = self.tmp_path / "finding-001.sarif"
    sarif_file.write_text(
        '{"version": "2.1.0", "runs": [{"results": [{"ruleId": "R1"}]}]}',
        encoding="utf-8",
    )
    finding_key = self.cache.put_finding("finding-001", sarif_file)

    # 1. Successful fix task with CRLF binary diff bytes.
    raw_diff = b"diff --git a/a.c b/a.c\r\n--- a/a.c\r\n+++ b/a.c\r\n"

    def _fake_fix(cfg: fix.FixConfig, adapter: CmAdapter, emitter=None) -> int:
      self.assertIs(adapter, self.adapter)
      self.assertIsNotNone(cfg.finding)
      self.assertIsNotNone(cfg.out)
      assert cfg.finding is not None and cfg.out is not None
      self.assertEqual(cfg.finding.name, "finding-001.sarif")
      (cfg.out / f"{cfg.finding.stem}.diff").write_bytes(raw_diff)
      logging.getLogger("codemender.runner.commands.fix").info("fix succeeded")
      return EXIT_OK

    self.cache.put_manifest(
        "fix-001",
        RunManifest(command="fix", task_id="fix-001", finding_key=finding_key),
    )
    worker = Worker(self.cache, work_dir=self.work_dir, adapter=self.adapter)
    with (
        mock.patch.object(init, "run", return_value=EXIT_OK),
        mock.patch.object(fix, "run", side_effect=_fake_fix),
    ):
      self.assertEqual(worker.run_task("fix-001"), EXIT_OK)
    fix_receipt = self.cache.get_receipt("fix-001")
    self.assertEqual(fix_receipt.status, "SUCCESS")
    self.assertIsNone(fix_receipt.error)
    self.assertEqual(
        self.cache.get_bytes(fix_receipt.artifacts["patch"]), raw_diff
    )

    # 2. Normal `no_fix` (fix.run returns 3): status="FAILURE", error=None.
    def _fake_nofix(
        cfg: fix.FixConfig, adapter: CmAdapter, emitter=None
    ) -> int:
      del cfg, adapter, emitter
      logging.getLogger("codemender.runner.commands.fix").error(
          "No patch could be synthesized."
      )
      return EXIT_EXECUTION_FAILURE

    self.cache.put_manifest(
        "fix-002",
        RunManifest(command="fix", task_id="fix-002", finding_key=finding_key),
    )
    with (
        mock.patch.object(init, "run", return_value=EXIT_OK),
        mock.patch.object(fix, "run", side_effect=_fake_nofix),
    ):
      self.assertEqual(worker.run_task("fix-002"), EXIT_EXECUTION_FAILURE)
    nofix_receipt = self.cache.get_receipt("fix-002")
    self.assertEqual(nofix_receipt.status, "FAILURE")
    self.assertEqual(nofix_receipt.exit_code, EXIT_EXECUTION_FAILURE)
    self.assertIsNone(nofix_receipt.error)
    self.assertEqual(
        nofix_receipt.artifacts, {"log": "tasks/fix-002/output/runner.log"}
    )

    # 3. Verify task dismissing a finding sets finding_count == 0.
    def _fake_verify(
        cfg: verify.VerifyConfig, adapter: CmAdapter, emitter=None
    ) -> int:
      self.assertIs(adapter, self.adapter)
      assert cfg.finding is not None and cfg.out is not None
      dismissed = {
          "version": "2.1.0",
          "runs": [{
              "results": [
                  {"ruleId": "R1", "properties": {"status": "DISMISSED"}}
              ]
          }],
      }
      (cfg.out / cfg.sarif_file_name).write_text(
          json.dumps(dismissed), encoding="utf-8"
      )
      return EXIT_OK

    self.cache.put_manifest(
        "verify-001",
        RunManifest(
            command="verify", task_id="verify-001", finding_key=finding_key
        ),
    )
    with (
        mock.patch.object(init, "run", return_value=EXIT_OK),
        mock.patch.object(verify, "run", side_effect=_fake_verify),
    ):
      self.assertEqual(worker.run_task("verify-001"), EXIT_OK)
    verify_receipt = self.cache.get_receipt("verify-001")
    self.assertEqual(verify_receipt.finding_count, 0)
    self.assertEqual(
        verify_receipt.artifacts["sarif"],
        "tasks/verify-001/output/finding-001.sarif",
    )

  def test_validation_and_tar_slip_errors_publish_exit_2_receipt(self):
    # Case 1: Invalid manifest missing finding_key for fix.
    self.cache.put_json(
        "tasks/bad-fix/input/manifest.json",
        {"command": "fix", "task_id": "bad-fix"},
    )
    worker = Worker(self.cache, work_dir=self.work_dir, adapter=self.adapter)
    self.assertEqual(worker.run_task("bad-fix"), EXIT_USAGE_ERROR)
    receipt = self.cache.get_receipt("bad-fix")
    self.assertEqual(receipt.status, "FAILURE")
    self.assertEqual(receipt.exit_code, EXIT_USAGE_ERROR)
    self.assertTrue(receipt.error)

    # Case 2: Tar archive with path traversal member.
    bad_tar = _create_repo_tar_gz(
        self.tmp_path / "bad.tar.gz", traversal_member="../../etc/passwd"
    )
    self.cache.put_repo_archive(bad_tar)
    self.cache.put_manifest("init", RunManifest(command="init", task_id="init"))
    self.assertEqual(worker.run_task("init"), EXIT_USAGE_ERROR)
    tar_receipt = self.cache.get_receipt("init")
    self.assertEqual(tar_receipt.status, "FAILURE")
    self.assertEqual(tar_receipt.exit_code, EXIT_USAGE_ERROR)
    self.assertTrue(tar_receipt.error)

  def test_infrastructure_errors_and_fallback_log_spill(self):
    self.cache.put_manifest("init", RunManifest(command="init", task_id="init"))
    # Missing cm binary -> exit code 3 with non-empty error (non-main thread).
    worker = Worker(self.cache, work_dir=self.work_dir)
    thread_rc: list[int] = []
    with mock.patch.object(CmAdapter, "locate", return_value=None):
      bg = threading.Thread(
          target=lambda: thread_rc.append(worker.run_task("init"))
      )
      bg.start()
      bg.join(timeout=5.0)
    self.assertEqual(thread_rc, [EXIT_EXECUTION_FAILURE])
    receipt = self.cache.get_receipt("init")
    self.assertEqual(receipt.exit_code, EXIT_EXECUTION_FAILURE)
    self.assertTrue(receipt.error)

    # Cache error on Step 1 + output/receipt upload failure -> single log spill.
    stderr_buf = io.StringIO()
    with (
        mock.patch.object(sys, "stderr", stderr_buf),
        mock.patch.object(
            self.cache,
            "get_manifest",
            side_effect=CachePermissionError("403 forbidden"),
        ),
        mock.patch.object(
            self.cache,
            "put_task_output",
            side_effect=CacheTransientError("503 output"),
        ),
        mock.patch.object(
            self.cache,
            "put_receipt",
            side_effect=CacheTransientError("503 receipt"),
        ),
    ):
      rc = worker.run_task("init")

    self.assertEqual(rc, EXIT_EXECUTION_FAILURE)
    self.assertEqual(
        stderr_buf.getvalue().count("=== [FALLBACK LOG SPILL:"), 1
    )
    self.assertIn("=== [END FALLBACK LOG SPILL] ===", stderr_buf.getvalue())

  def test_invalid_task_id_returns_2_and_spills_unpublishable_receipt(self):
    worker = Worker(self.cache, work_dir=self.work_dir, adapter=self.adapter)
    for bad_task_id in ("", "../escape", "nested/task", ".tmp.staging"):
      with self.subTest(task_id=bad_task_id):
        stdout_buf, stderr_buf = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(sys, "stdout", stdout_buf),
            mock.patch.object(sys, "stderr", stderr_buf),
        ):
          rc = worker.run_task(bad_task_id)
        self.assertEqual(rc, EXIT_USAGE_ERROR)
        self.assertEqual(stdout_buf.getvalue(), "")
        stderr_text = stderr_buf.getvalue()
        self.assertEqual(stderr_text.count("=== [FALLBACK LOG SPILL:"), 1)
        self.assertIn("Validation error:", stderr_text)
        self.assertIn("=== [END FALLBACK LOG SPILL] ===", stderr_text)
        self.assertFalse(self.work_dir.exists())

  def test_preemption_signals_terminate_child_group_and_flush_receipt(self):
    for sig, expected_rc in (
        (signal.SIGTERM, EXIT_SIGTERM),
        (signal.SIGINT, EXIT_SIGINT),
    ):
      task_id = f"sig-{sig.value}"
      with self.subTest(signal=sig, expected_rc=expected_rc):
        ready_event = threading.Event()

        def _slow_init(
            cfg: init.InitConfig,
            adapter: CmAdapter,
            emitter=None,
            *,
            event: threading.Event = ready_event,
        ) -> int:
          del cfg, adapter, emitter
          logging.getLogger("codemender.runner.commands.init").info(
              "starting slow task"
          )
          event.set()
          time.sleep(30)
          return EXIT_OK

        self.cache.put_manifest(
            task_id, RunManifest(command="init", task_id=task_id)
        )
        worker = Worker(
            self.cache, work_dir=self.work_dir, adapter=self.adapter
        )

        def _trigger_signal(
            event: threading.Event = ready_event,
            signum: signal.Signals = sig,
        ) -> None:
          event.wait(timeout=5.0)
          os.kill(os.getpid(), signum)

        sender = threading.Thread(target=_trigger_signal, daemon=True)
        with mock.patch.object(init, "run", side_effect=_slow_init):
          sender.start()
          rc = worker.run_task(task_id)
          sender.join(timeout=2.0)

        self.assertEqual(rc, expected_rc)
        receipt = self.cache.get_receipt(task_id)
        self.assertEqual(receipt.status, "FAILURE")
        self.assertEqual(receipt.exit_code, expected_rc)
        self.assertTrue(receipt.error)
        self.assertFalse(self.work_dir.exists())


if __name__ == "__main__":
  unittest.main()
