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

# Governing: ADR-0008, SPEC-GHA/REQ-0001, SPEC-GHA/REQ-0002, SPEC-GHA/REQ-0004, SPEC-GHA/REQ-0007, SPEC-GHA-FIX/REQ-0001, SPEC-GHA-VERIFY

"""Unit tests for the gha_runner entry point (M1 find-diff + M2 parallel fixes + M3 verify)."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import MagicMock, call, patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(
    os.path.join(_THIS_DIR, "..", "..", "..", "..", "..")
)
for _pkg in ("codemender", "launcher", "repohost"):
  _pkg_src = os.path.join(_REPO_ROOT, "packages", _pkg, "src")
  if os.path.isdir(_pkg_src) and _pkg_src not in sys.path:
    sys.path.insert(0, _pkg_src)

from launcher.base import LauncherError, ParallelResult, RunResult  # pylint: disable=g-import-not-at-top
from launcher.cloudrun import CloudRunLauncher  # pylint: disable=g-import-not-at-top
from launcher.local import LocalDockerLauncher  # pylint: disable=g-import-not-at-top
from repohost.github import __main__ as github_main  # pylint: disable=g-import-not-at-top
# pylint: disable=g-import-not-at-top
from repohost.github import client as github_client
from repohost.github import gha_runner
from repohost.models import (  # pylint: disable=g-import-not-at-top
    ChangeContext,
    Comment,
    RepoHostError,
    SecurityGate,
    SecurityGateState,
    SuggestedEdit,
)
# pylint: disable=g-import-not-at-top
from repohost.store import ArtifactStoreError
from repohost.testing import InMemoryArtifactStore, InMemoryRepoHost

_VERSION_EVENT = {
    "event": "version",
    "orchestrator": "0.1.0",
    "cm": {"found": True, "version": "0.9.0", "path": "/usr/local/bin/cm"},
    "contract": "cm-0.9.x",
}

_INIT_OK_EVENTS = [
    {"event": "check", "name": "cm_binary", "status": "pass", "message": "ok"},
    {"event": "check", "name": "cm_workspace", "status": "recovered", "message": "created"},
    {"event": "check", "name": "cm_config", "status": "pass", "message": "ok"},
    {"event": "check", "name": "cm_verify", "status": "pass", "message": "ok"},
]

_SARIF_CLEAN = {
    "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json",
    "version": "2.1.0",
    "runs": [{"tool": {"driver": {"name": "CodeMender"}}, "results": []}],
}

_SARIF_WITH_FINDINGS = {
    "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json",
    "version": "2.1.0",
    "runs": [
        {
            "tool": {"driver": {"name": "CodeMender"}},
            "results": [
                {
                    "ruleId": "CWE-78",
                    "level": "error",
                    "message": {
                        "text": (
                            "OS Command Injection in archive extractor:"
                            " Untrusted filename user_archive is passed"
                            " directly to os.system('/bin/sh -c')."
                        )
                    },
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "src/archive/unpack.py"},
                                "region": {"startLine": 18, "endLine": 19},
                            }
                        }
                    ],
                },
                {
                    "ruleId": "CWE-89",
                    "level": "warning",
                    "message": {
                        "text": (
                            "SQL Injection in user lookup query:"
                            " User-supplied user_id is interpolated directly."
                        )
                    },
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "src/db/users.py"},
                                "region": {"startLine": 42, "endLine": 42},
                            }
                        }
                    ],
                },
            ],
        }
    ],
}

_ENV = {
    "GITHUB_EVENT_NAME": "pull_request",
    "GITHUB_REPOSITORY": "o/r",
    "GITHUB_TOKEN": "tok",
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_RUN_ID": "123",
    "CODEMENDER_BACKEND": "local",
    "CODEMENDER_RUNNER_IMAGE": "img:sha",
    "GOOGLE_CLOUD_PROJECT": "my-gcp-project",
    "GOOGLE_GHA_CREDS_PATH": "/tmp/adc.json",
    "CODEMENDER_FIX": "false",
}


def _pr_event(
    head_repo="o/r",
    base_repo="o/r",
    number=7,
    base_sha="1111222233334444",
    head_sha="aaaabbbbccccdddd",
):
  return {
      "pull_request": {
          "number": number,
          "head": {"sha": head_sha, "repo": {"full_name": head_repo}},
          "base": {"sha": base_sha, "repo": {"full_name": base_repo}},
      }
  }


def _scan_result(sarif_dict=None, exit_code=0, raw=None):
  if raw is not None:
    return RunResult(exit_code, raw)
  doc = sarif_dict if sarif_dict is not None else _SARIF_CLEAN
  return RunResult(exit_code, json.dumps(doc).encode("utf-8"))


def _launcher(scan_result=None, error=None):
  launcher = MagicMock()
  launcher.with_dirs.return_value = launcher
  if error:
    launcher.run.side_effect = error
    return launcher
  launcher.run.return_value = (
      scan_result if scan_result is not None else _scan_result()
  )
  return launcher


def _in_memory_host(event=None, **kwargs):
  event_data = event if event is not None else _pr_event()
  pr = event_data.get("pull_request") or {}
  ctx = ChangeContext(
      repo_id="o/r",
      change_id=str(pr.get("number") or 7),
      base_sha=(
          ((pr.get("base") or {}).get("sha") or "").strip()
          or ((pr.get("base") or {}).get("ref") or "").strip()
          or "1111222233334444"
      ),
      head_sha=(
          ((pr.get("head") or {}).get("sha") or "").strip()
          or "aaaabbbbccccdddd"
      ),
      base_ref="main",
      head_ref="feature",
      is_fork=gha_runner.is_fork_pr(event_data),
  )
  return InMemoryRepoHost(change_context=ctx, **kwargs)


class TestRunPr(unittest.TestCase):

  def setUp(self):
    super().setUp()
    self._host = _in_memory_host()
    self._delete_comments_patcher = patch.object(
        gha_runner, "delete_codemender_review_comments", return_value=0
    )
    self.mock_delete_comments = self._delete_comments_patcher.start()
    self.addCleanup(self._delete_comments_patcher.stop)

  def test_clean_scan_posts_no_vulnerabilities_comment(self):
    launcher = _launcher()

    self.assertEqual(
        gha_runner.run_pr(_pr_event(), _ENV, launcher, host=self._host), 0
    )

    self.mock_delete_comments.assert_called_once_with("o/r", 7, "tok")
    self.assertEqual(
        launcher.run.call_args_list,
        [
            call(
                "find-diff",
                [
                    "--repo",
                    "/workspace",
                    "--base",
                    "1111222233334444",
                    "--out",
                    "/out",
                    "--diff-workers",
                    "8",
                ],
            )
        ],
    )
    body = self._host.current_summary or ""
    self.assertIn("✅ **No vulnerabilities found**", body)
    self.assertIn("`111122223333`", body)
    self.assertIn("https://github.com/o/r/actions/runs/123", body)
    self.assertIsNotNone(self._host.latest_security_gate)
    self.assertEqual(
        self._host.latest_security_gate.state, SecurityGateState.SUCCESS
    )

  def test_custom_diff_workers_is_passed_to_find_diff(self):
    launcher = _launcher()
    env = dict(_ENV, CODEMENDER_DIFF_WORKERS="12")

    self.assertEqual(
        gha_runner.run_pr(_pr_event(), env, launcher, host=self._host), 0
    )

    self.assertEqual(
        launcher.run.call_args_list[0],
        call(
            "find-diff",
            [
                "--repo",
                "/workspace",
                "--base",
                "1111222233334444",
                "--out",
                "/out",
                "--diff-workers",
                "12",
            ],
        ),
    )

  def test_invalid_diff_workers_in_run_pr_fails_and_posts_error(self):
    launcher = _launcher()
    env = dict(_ENV, CODEMENDER_DIFF_WORKERS="0")

    self.assertEqual(
        gha_runner.run_pr(_pr_event(), env, launcher, host=self._host), 1
    )
    launcher.run.assert_not_called()
    body = self._host.current_summary or ""
    self.assertIn("❌ **CodeMender scan failed.**", body)
    self.assertIn("Invalid CODEMENDER_DIFF_WORKERS", body)

  def test_findings_posts_summary_table_and_details(self):
    launcher = _launcher(scan_result=_scan_result(_SARIF_WITH_FINDINGS))

    self.assertEqual(
        gha_runner.run_pr(_pr_event(), _ENV, launcher, host=self._host), 0
    )

    body = self._host.current_summary or ""
    self.assertIn("⚠️ **Found 2 vulnerabilities**", body)
    self.assertIn("`CWE-78`", body)
    self.assertIn("`CWE-89`", body)
    self.assertIn("🔴 High / Critical", body)
    self.assertIn("🟠 Medium", body)
    self.assertIn(
        "[`src/archive/unpack.py:18-19`](https://github.com/o/r/blob/aaaabbbbccccdddd/src/archive/unpack.py#L18-L19)",
        body,
    )
    self.assertIn(
        "[`src/db/users.py:42`](https://github.com/o/r/blob/aaaabbbbccccdddd/src/db/users.py#L42)",
        body,
    )
    self.assertIn("<details>", body)
    self.assertIn("Untrusted filename user_archive", body)
    self.assertIsNotNone(self._host.latest_security_gate)
    self.assertEqual(
        self._host.latest_security_gate.state, SecurityGateState.FAILURE
    )

  def test_delete_review_comments_failure_warns_without_failing_run(self):
    self.mock_delete_comments.return_value = None
    launcher = _launcher()
    with patch("builtins.print") as mock_print:
      self.assertEqual(
          gha_runner.run_pr(_pr_event(), _ENV, launcher, host=self._host), 0
      )
    printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list)
    self.assertIn(
        "::warning::Could not delete CodeMender's earlier review comments",
        printed,
    )

  def test_fork_pr_runs_and_handles_comment_failure(self):
    launcher = _launcher()
    fork_host = _in_memory_host(event=_pr_event(head_repo="fork/r"))
    self.assertEqual(
        gha_runner.run_pr(
            _pr_event(head_repo="fork/r"), _ENV, launcher, host=fork_host
        ),
        0,
    )
    self.assertEqual(launcher.run.call_count, 1)

    class FailingHost(InMemoryRepoHost):

      def upsert_summary(self, body: str) -> Comment:
        del body
        raise RepoHostError("permission denied on fork")

    failing_fork_host = FailingHost(
        change_context=fork_host.get_change_context()
    )
    with patch("builtins.print") as mock_print:
      rc = gha_runner.run_pr(
          _pr_event(head_repo="fork/r"), _ENV, launcher, host=failing_fork_host
      )
    self.assertEqual(rc, 1)
    printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list)
    self.assertIn("Send write tokens to workflows from pull requests", printed)

  def test_scan_signal_interruption_stops_launcher_and_updates_sticky(self):
    launcher = _launcher()
    launcher.run.side_effect = KeyboardInterrupt

    self.assertEqual(
        gha_runner.run_pr(_pr_event(), _ENV, launcher, host=self._host), 1
    )
    launcher.stop.assert_called()
    body = self._host.current_summary or ""
    self.assertIn("❌ **CodeMender scan failed.**", body)
    self.assertIn("The run was cancelled or timed out.", body)

  def test_stop_on_signal_invokes_stop_and_raises(self):
    launcher = MagicMock()
    with self.assertRaises(KeyboardInterrupt):
      with gha_runner._stop_on_signal(launcher):
        import signal
        signal.raise_signal(signal.SIGTERM)
    launcher.stop.assert_called_once()

  def test_find_diff_and_launcher_failures_post_error_and_exit_1(self):
    host1 = _in_memory_host()
    launcher = _launcher(scan_result=_scan_result(exit_code=3, raw=b""))
    self.assertEqual(
        gha_runner.run_pr(_pr_event(), _ENV, launcher, host=host1), 1
    )
    self.assertIn("❌ **CodeMender scan failed.**", host1.current_summary or "")
    self.assertIn("`3`", host1.current_summary or "")

    host2 = _in_memory_host()
    launcher2 = _launcher(error=LauncherError("docker not found"))
    self.assertEqual(
        gha_runner.run_pr(_pr_event(), _ENV, launcher2, host=host2), 1
    )
    self.assertIn("docker not found", host2.current_summary or "")

    host3 = _in_memory_host()
    launcher3 = _launcher(scan_result=_scan_result(raw=b'{"event": "other"}'))
    self.assertEqual(
        gha_runner.run_pr(_pr_event(), _ENV, launcher3, host=host3), 1
    )
    self.assertIn(
        "did not emit a valid SARIF 2.1.0 document",
        host3.current_summary or "",
    )

  def test_comment_failure_exits_1(self):
    class FailingHost(InMemoryRepoHost):

      def upsert_summary(self, body: str) -> Comment:
        del body
        raise RepoHostError("simulated comment failure")

    failing_host = FailingHost(change_context=self._host.get_change_context())
    launcher = _launcher()
    self.assertEqual(
        gha_runner.run_pr(_pr_event(), _ENV, launcher, host=failing_host), 1
    )

  def test_cloudrun_consolidated_find_diff(self):
    launcher = MagicMock()
    scan_res = _scan_result()
    launcher.run.return_value = scan_res

    env = dict(_ENV, CODEMENDER_BACKEND="cloudrun")
    rc = gha_runner.run_pr(_pr_event(), env, launcher, host=self._host)

    self.assertEqual(rc, 0)
    launcher.run.assert_called_once_with(
        "find-diff",
        [
            "--repo",
            "/workspace",
            "--base",
            "1111222233334444",
            "--out",
            "/out",
            "--diff-workers",
            "8",
        ],
    )
    body = self._host.current_summary or ""
    self.assertIn("✅ **No vulnerabilities found**", body)

  def test_cloudrun_comment_generation_with_no_init(self):
    # Success scenario: format_find_diff_comment with init_result is None
    scan_ok = _scan_result()
    comment_ok = gha_runner.format_find_diff_comment(
        scan_result=scan_ok,
        image="",
        run_url="https://run",
        base_sha="1111222233334444",
        init_result=None,
        backend="cloudrun",
    )
    self.assertIn("✅ **No vulnerabilities found**", comment_ok)
    self.assertIn("`111122223333`", comment_ok)

    # Failure scenario: format_find_diff_comment with init_result is None
    scan_fail = RunResult(2, b"")
    comment_fail = gha_runner.format_find_diff_comment(
        scan_result=scan_fail,
        image="",
        run_url="https://run",
        base_sha="1111222233334444",
        init_result=None,
        backend="cloudrun",
    )
    self.assertIn("❌ **CodeMender scan failed.**", comment_fail)
    self.assertIn("`2`", comment_fail)
    self.assertIn("Backend: `Cloudrun`", comment_fail)

    # run_pr execution when scan fails with init_result is None:
    host_fail = _in_memory_host()
    launcher_fail = MagicMock()
    launcher_fail.run.return_value = scan_fail
    env = dict(_ENV, CODEMENDER_BACKEND="cloudrun")
    rc_fail = gha_runner.run_pr(
        _pr_event(), env, launcher_fail, host=host_fail
    )
    self.assertEqual(rc_fail, 1)
    body_posted = host_fail.current_summary or ""
    self.assertIn("❌ **CodeMender scan failed.**", body_posted)
    self.assertIn("Backend: `Cloudrun`", body_posted)

    # run_pr execution when scan succeeds with init_result is None:
    host_ok = _in_memory_host()
    launcher_ok = MagicMock()
    launcher_ok.run.return_value = scan_ok
    rc_ok = gha_runner.run_pr(_pr_event(), env, launcher_ok, host=host_ok)
    self.assertEqual(rc_ok, 0)
    body_posted_ok = host_ok.current_summary or ""
    self.assertIn("✅ **No vulnerabilities found**", body_posted_ok)

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0001, SPEC-GHA-FIX
  @patch.object(gha_runner.fix_comments, "checked_out_commit", return_value="aaaabbbbccccdddd")
  @patch.object(gha_runner.fix_comments, "FixPoster")
  def test_M2CloudRunParallelFixes(
      self,
      mock_poster_cls,
      _mock_head,

  ):
    with tempfile.TemporaryDirectory() as temp_dir:
      workspace = os.path.join(temp_dir, "workspace")
      os.makedirs(workspace)
      out_dir = os.path.join(temp_dir, "out")

      env = dict(
          _ENV,
          CODEMENDER_BACKEND="cloudrun",
          CODEMENDER_FIX="true",
          CODEMENDER_MAX_PARALLEL_FIXES="2",
          GITHUB_WORKSPACE=workspace,
          RUNNER_TEMP=temp_dir,
      )
      launcher = _launcher(scan_result=_scan_result(_SARIF_WITH_FINDINGS))
      findings = gha_runner.SarifDocument(data=_SARIF_WITH_FINDINGS).to_findings()
      launcher.run_parallel.return_value = (
          r
          for r in (
              ParallelResult(
                  key=findings[0],
                  exit_code=0,
                  output=b"diff --git a/x b/x\n",
                  log="cloudrun fix 1 log\n",
              ),
              ParallelResult(
                  key=findings[1],
                  exit_code=3,
                  output=b"",
                  log="cloudrun fix 2 log\n",
                  error="cm-runner exited with code 3.",
              ),
          )
      )
      mock_poster_cls.return_value.post.side_effect = [
          gha_runner.FixCell(
              gha_runner.fix_comments.SUGGESTED,
              "https://github.com/o/r/pull/7#review-1",
          ),
          gha_runner.FixCell(gha_runner.fix_comments.NO_FIX),
      ]

      host = _in_memory_host()
      with patch("builtins.print"):
        rc = gha_runner.run_pr(
            _pr_event(), env, launcher, out_dir=out_dir, host=host
        )

      self.assertEqual(rc, 0)
      self.mock_delete_comments.assert_called_once_with("o/r", 7, "tok")
      launcher.run_parallel.assert_called_once()
      self.assertEqual(launcher.run_parallel.call_args.kwargs["max_workers"], 2)
      self.assertTrue(
          os.path.isfile(os.path.join(out_dir, "findings", "finding-001.sarif"))
      )
      self.assertTrue(
          os.path.isfile(os.path.join(out_dir, "fixes", "finding-001.diff"))
      )
      bodies = host.summaries
      self.assertGreaterEqual(len(bodies), 2)
      self.assertIn("⏳ Generating fixes for 2 findings.", bodies[0])
      self.assertIn("| ⏳ Working |", bodies[0])
      final_body = bodies[-1]
      self.assertIn(
          "[✅ Suggested](https://github.com/o/r/pull/7#review-1)", final_body
      )
      self.assertIn("| ❌ No fix |", final_body)
      self.assertIn("**Fixes:** ✅ 1 suggested · ❌ 1 no fix.", final_body)

  # Governing: docs/openspec/specs/github-actions-fix/spec.md#req-0001, SPEC-GHA-FIX
  def test_cloudrun_fixes_skipped_without_workspace_warning(self):
    launcher = _launcher(scan_result=_scan_result(_SARIF_WITH_FINDINGS))
    env = dict(
        _ENV,
        CODEMENDER_BACKEND="cloudrun",
        CODEMENDER_FIX="true",
        GITHUB_WORKSPACE="",
    )

    with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
      rc = gha_runner.run_pr(_pr_event(), env, launcher, host=self._host)

    self.assertEqual(rc, 0)
    launcher.run_parallel.assert_not_called()
    stdout_val = mock_stdout.getvalue()
    self.assertIn("GITHUB_WORKSPACE is not set", stdout_val)


class TestFormatting(unittest.TestCase):

  def test_untrusted_sarif_text_cannot_break_details_or_fences(self):
    sarif = {
        "version": "2.1.0",
        "runs": [
            {
                "results": [
                    {
                        "ruleId": "CWE-79|`bad",
                        "level": "error",
                        "message": {
                            "text": (
                                "XSS|Title: Body with ```fence``` and"
                                " </details><script>alert(1)</script>"
                            )
                        },
                        "locations": [],
                    }
                ]
            }
        ],
    }
    body = gha_runner.format_find_diff_comment(
        _scan_result(sarif),
        image="img:sha",
        run_url="https://run",
        base_sha="1234567890ab",
    )
    self.assertNotIn("```", body)
    self.assertNotIn("</details><script>", body)
    self.assertIn("&lt;/details&gt;&lt;script&gt;", body)

  def test_cm_not_found_is_flagged(self):
    event = dict(_VERSION_EVENT, cm={"found": False})
    body = gha_runner.format_version_comment(
        RunResult(0, [event]), "img", "https://run"
    )
    self.assertIn("⚠️", body)

  def test_format_version_comment_cloudrun_displays_backend(self):
    event = dict(_VERSION_EVENT)
    body = gha_runner.format_version_comment(
        RunResult(0, [event]), "", "https://run", backend="cloudrun"
    )
    self.assertIn("| Backend |", body)
    self.assertIn("Cloud", body)
    self.assertNotIn("| Image |", body)

    fail_body = gha_runner.format_version_comment(
        RunResult(1, []), "", "https://run", backend="cloudrun"
    )
    self.assertIn("Backend:", fail_body)
    self.assertNotIn("Image:", fail_body)

class TestMain(unittest.TestCase):

  def setUp(self):
    super().setUp()
    self._temp_dir = tempfile.mkdtemp(prefix="gha-test-")
    self._workspace = os.path.join(self._temp_dir, "src-repo")
    os.makedirs(os.path.join(self._workspace, ".git"))
    with open(os.path.join(self._workspace, "app.py"), "w", encoding="utf-8") as f:
      f.write("print('hi')\n")
    self._event_file = os.path.join(self._temp_dir, "event.json")
    with open(self._event_file, "w", encoding="utf-8") as f:
      json.dump(_pr_event(), f)

  def tearDown(self):
    shutil.rmtree(self._temp_dir, ignore_errors=True)
    super().tearDown()

  def _env(self, **overrides):
    base = dict(
        _ENV,
        GITHUB_EVENT_PATH=self._event_file,
        GITHUB_WORKSPACE=self._workspace,
        RUNNER_TEMP=self._temp_dir,
    )
    base.update(overrides)
    return base

  def test_validation_and_non_pr_skip(self):
    self.assertEqual(gha_runner.main(self._env(GITHUB_EVENT_NAME="push")), 0)
    env = self._env()
    del env["CODEMENDER_RUNNER_IMAGE"]
    self.assertEqual(gha_runner.main(env), 1)
    env2 = self._env(GOOGLE_GHA_CREDS_PATH="", GOOGLE_APPLICATION_CREDENTIALS="")
    self.assertEqual(gha_runner.main(env2), 1)

  @patch.object(gha_runner, "run_pr", return_value=0)
  def test_stages_workspace_excludes_creds_and_builds_launcher(self, mock_run_pr):
    creds_name = "gha-creds-0123456789abcdef.json"
    with open(os.path.join(self._workspace, creds_name), "w", encoding="utf-8") as f:
      f.write('{"type": "external_account"}')
    observed = {}

    def _capture(event, env, launcher, out_dir=None, **kwargs):
      del event, env, kwargs
      observed["files"] = sorted(os.listdir(launcher.workspace_dir))
      observed["out_dir"] = out_dir
      return 0

    mock_run_pr.side_effect = _capture
    out_dir = os.path.join(self._temp_dir, "custom-out")
    rc = gha_runner.main(
        self._env(CODEMENDER_LOG_LEVEL="debug", CODEMENDER_OUT_DIR=out_dir)
    )
    self.assertEqual(rc, 0)
    self.assertNotIn(creds_name, observed["files"])
    self.assertIn("app.py", observed["files"])
    self.assertEqual(observed["out_dir"], out_dir)
    launcher = mock_run_pr.call_args.args[2]
    self.assertFalse(os.path.exists(launcher.workspace_dir))
    self.assertTrue(os.path.isdir(out_dir))

  @patch.object(gha_runner, "run_pr")
  def test_invalid_diff_workers_fails_before_running(self, mock_run_pr):
    self.assertEqual(
        gha_runner.main(self._env(CODEMENDER_DIFF_WORKERS="99")), 1
    )
    mock_run_pr.assert_not_called()

  @patch.object(gha_runner, "run_pr", return_value=0)
  def test_launcher_factory_cloudrun_default(self, mock_run_pr):
    env = self._env()
    env.pop("CODEMENDER_BACKEND", None)
    self.assertEqual(gha_runner.main(env), 0)

    launcher = mock_run_pr.call_args.args[2]
    self.assertIsInstance(launcher, CloudRunLauncher)

  @patch.object(gha_runner, "run_pr", return_value=0)
  def test_launcher_factory_local_explicit(self, mock_run_pr):
    env = self._env(CODEMENDER_BACKEND="local")
    self.assertEqual(gha_runner.main(env), 0)

    launcher = mock_run_pr.call_args.args[2]
    self.assertIsInstance(launcher, LocalDockerLauncher)
    self.assertEqual(launcher.image, "img:sha")

  @patch.object(gha_runner, "run_pr")
  def test_launcher_factory_missing_runner_image_in_local_mode_fails(
      self, mock_run_pr
  ):
    env = dict(_ENV)
    del env["CODEMENDER_RUNNER_IMAGE"]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
      json.dump(_pr_event(), f)
    try:
      env["GITHUB_EVENT_PATH"] = f.name
      with patch("builtins.print") as mock_print:
        rc = gha_runner.main(env)
    finally:
      os.unlink(f.name)

    self.assertEqual(rc, 1)
    mock_run_pr.assert_not_called()
    printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list)
    self.assertIn(
        "Missing required environment variable CODEMENDER_RUNNER_IMAGE", printed
    )

  @patch.object(gha_runner, "run_pr", return_value=0)
  def test_launcher_factory_cloudrun(self, mock_run_pr):
    mock_client_module = MagicMock()
    mock_cloud_run_client_cls = MagicMock(name="CloudRunClient")
    mock_cloud_run_config_cls = MagicMock(name="CloudRunConfig")
    mock_client_module.CloudRunClient = mock_cloud_run_client_cls
    mock_client_module.CloudRunConfig = mock_cloud_run_config_cls

    env = dict(
        _ENV,
        CODEMENDER_BACKEND="cloudrun",
        GOOGLE_CLOUD_PROJECT="my-gcp-project",
        GCS_TRANSIT_BUCKET="my-transit-bucket",
        CLOUD_RUN_JOB_NAME="codemender-runner",
        CLOUD_RUN_REGION="us-central1",
    )
    env.pop("CODEMENDER_RUNNER_IMAGE", None)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
      json.dump(_pr_event(), f)
    try:
      env["GITHUB_EVENT_PATH"] = f.name
      with patch.dict(
          "sys.modules",
          {
              "launcher.cloudrun.client": mock_client_module,
          },
      ):
        rc = gha_runner.main(env)
    finally:
      os.unlink(f.name)

    self.assertEqual(rc, 0)
    mock_run_pr.assert_called_once()
    launcher = mock_run_pr.call_args.args[2]
    self.assertIsInstance(launcher, CloudRunLauncher)
    self.assertEqual(launcher.client, mock_cloud_run_client_cls.return_value)
    mock_cloud_run_config_cls.assert_called_once_with(
        project="my-gcp-project",
        bucket="my-transit-bucket",
        job_name="codemender-runner",
        region="us-central1",
        workspace_dir="",
        out_dir="",
        run_id="123",
        run_attempt="1",
        diff_workers="8",
        base_sha="",
    )
    mock_cloud_run_client_cls.assert_called_once_with(
        mock_cloud_run_config_cls.return_value
    )

  @patch.object(gha_runner, "run_pr", return_value=0)
  def test_launcher_factory_cloud_run_alias(self, mock_run_pr):
    mock_client_module = MagicMock()
    mock_cloud_run_client_cls = MagicMock(name="CloudRunClient")
    mock_cloud_run_config_cls = MagicMock(name="CloudRunConfig")
    mock_client_module.CloudRunClient = mock_cloud_run_client_cls
    mock_client_module.CloudRunConfig = mock_cloud_run_config_cls

    env = dict(
        _ENV,
        CODEMENDER_BACKEND="cloud_run",
        GOOGLE_CLOUD_PROJECT="my-gcp-project",
        GCS_TRANSIT_BUCKET="my-transit-bucket",
    )
    env.pop("CODEMENDER_RUNNER_IMAGE", None)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
      json.dump(_pr_event(), f)
    try:
      env["GITHUB_EVENT_PATH"] = f.name
      with patch.dict(
          "sys.modules",
          {
              "launcher.cloudrun.client": mock_client_module,
          },
      ):
        rc = gha_runner.main(env)
    finally:
      os.unlink(f.name)

    self.assertEqual(rc, 0)
    mock_run_pr.assert_called_once()
    launcher = mock_run_pr.call_args.args[2]
    self.assertIsInstance(launcher, CloudRunLauncher)
    self.assertEqual(launcher.client, mock_cloud_run_client_cls.return_value)

  @patch.object(gha_runner, "run_pr")
  def test_launcher_factory_unsupported_backend_fails(self, mock_run_pr):
    env = dict(_ENV, CODEMENDER_BACKEND="unsupported_engine")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
      json.dump(_pr_event(), f)
    try:
      env["GITHUB_EVENT_PATH"] = f.name
      with patch("builtins.print") as mock_print:
        rc = gha_runner.main(env)
    finally:
      os.unlink(f.name)

    self.assertEqual(rc, 1)
    mock_run_pr.assert_not_called()
    printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list)
    self.assertIn("Unsupported CODEMENDER_BACKEND: 'unsupported_engine'", printed)

  def test_create_launcher_cloudrun_explicit_config(self):
    env = {
        "CODEMENDER_BACKEND": "cloudrun",
        "GOOGLE_CLOUD_PROJECT": "p",
        "GCS_TRANSIT_BUCKET": "b",
        "GITHUB_WORKSPACE": "/ws",
        "CODEMENDER_OUT_DIR": "/out",
        "GITHUB_RUN_ID": "100",
        "GITHUB_RUN_ATTEMPT": "3",
        "CODEMENDER_DIFF_WORKERS": "4",
        "BASE_SHA": "base123",
        "CODEMENDER_LOG_LEVEL": "debug",
    }
    launcher = gha_runner.create_launcher(env)
    self.assertIsInstance(launcher, CloudRunLauncher)
    self.assertEqual(launcher.client.config.workspace_dir, "/ws")
    self.assertEqual(launcher.client.config.out_dir, "/out")
    self.assertEqual(launcher.client.config.run_id, "100")
    self.assertEqual(launcher.client.config.run_attempt, "3")
    self.assertEqual(launcher.client.config.diff_workers, "4")
    self.assertEqual(launcher.client.config.base_sha, "base123")


class TestM2FixConfigAndOrchestration(unittest.TestCase):

  def test_fix_enabled_and_max_parallel_fixes_parsing(self):
    for off in ("false", "FALSE", " 0 ", "No", "no"):
      self.assertFalse(gha_runner.fix_enabled({"CODEMENDER_FIX": off}))
    for on in ("", "true", "TRUE", "1", "yes"):
      self.assertTrue(gha_runner.fix_enabled({"CODEMENDER_FIX": on}))
    self.assertEqual(gha_runner.max_parallel_fixes({}), 4)
    self.assertEqual(
        gha_runner.max_parallel_fixes({"CODEMENDER_MAX_PARALLEL_FIXES": " 8 "}), 8
    )
    with patch("builtins.print"):
      self.assertEqual(
          gha_runner.max_parallel_fixes({"CODEMENDER_MAX_PARALLEL_FIXES": "0"}), 4
      )

  @patch.object(gha_runner, "delete_codemender_review_comments", return_value=2)
  @patch.object(
      gha_runner.fix_comments,
      "checked_out_commit",
      return_value="aaaabbbbccccdddd",
  )
  @patch.object(gha_runner.fix_comments, "FixPoster")
  def test_run_pr_with_fixes_updates_sticky_comment_in_place(
      self,
      mock_poster_cls,
      _mock_head,
      mock_delete_comments,
  ):
    with tempfile.TemporaryDirectory() as temp_dir:
      workspace = os.path.join(temp_dir, "workspace")
      os.makedirs(workspace)
      out_dir = os.path.join(temp_dir, "out")

      env = dict(
          _ENV,
          CODEMENDER_FIX="true",
          CODEMENDER_MAX_PARALLEL_FIXES="3",
          GITHUB_WORKSPACE=workspace,
          RUNNER_TEMP=temp_dir,
      )
      launcher = _launcher(scan_result=_scan_result(_SARIF_WITH_FINDINGS))
      findings = (
          gha_runner.SarifDocument(data=_SARIF_WITH_FINDINGS).to_findings()
      )
      launcher.run_parallel.return_value = (
          r
          for r in (
              ParallelResult(
                  key=findings[0],
                  exit_code=0,
                  output=b"diff --git a/x b/x\n",
                  log="fix 1 log\n",
              ),
              ParallelResult(
                  key=findings[1],
                  exit_code=3,
                  output=b"",
                  log="fix 2 log\n",
                  error="cm-runner exited with code 3.",
              ),
          )
      )
      mock_poster_cls.return_value.post.side_effect = [
          gha_runner.FixCell(
              gha_runner.fix_comments.SUGGESTED,
              "https://github.com/o/r/pull/7#review-1",
          ),
          gha_runner.FixCell(gha_runner.fix_comments.NO_FIX),
      ]

      host = _in_memory_host()
      with patch("builtins.print"):
        rc = gha_runner.run_pr(
            _pr_event(), env, launcher, out_dir=out_dir, host=host
        )

      self.assertEqual(rc, 0)
      mock_delete_comments.assert_called_once_with("o/r", 7, "tok")
      self.assertEqual(
          launcher.run_parallel.call_args.kwargs["max_workers"], 3
      )
      self.assertTrue(
          os.path.isfile(os.path.join(out_dir, "findings", "finding-001.sarif"))
      )
      self.assertTrue(
          os.path.isfile(os.path.join(out_dir, "fixes", "finding-001.diff"))
      )
      bodies = host.summaries
      self.assertGreaterEqual(len(bodies), 2)
      self.assertIn("⏳ Generating fixes for 2 findings.", bodies[0])
      self.assertIn("| ⏳ Working |", bodies[0])
      final_body = bodies[-1]
      self.assertIn(
          "[✅ Suggested](https://github.com/o/r/pull/7#review-1)", final_body
      )
      self.assertIn("| ❌ No fix |", final_body)
      self.assertIn("**Fixes:** ✅ 1 suggested · ❌ 1 no fix.", final_body)
      self.assertIsNotNone(host.latest_security_gate)
      self.assertEqual(
          host.latest_security_gate.state, SecurityGateState.FAILURE
      )
      self.assertEqual(
          host.latest_security_gate.description,
          "CodeMender: 2 security finding(s) detected",
      )

  def test_run_pr_fixes_disabled_sets_security_gate_failure(self):
    findings = (
        gha_runner.SarifDocument(data=_SARIF_WITH_FINDINGS).to_findings()
    )
    host = InMemoryRepoHost(
        change_context=ChangeContext(
            repo_id="o/r",
            change_id="7",
            base_sha="1111222233334444",
            head_sha="5555666677778888",
            base_ref="main",
            head_ref="feat",
        ),
    )
    launcher = _launcher(scan_result=_scan_result(_SARIF_WITH_FINDINGS))
    with tempfile.TemporaryDirectory() as temp_dir:
      env = dict(
          _ENV,
          CODEMENDER_FIX="false",
          GITHUB_WORKSPACE=temp_dir,
          RUNNER_TEMP=temp_dir,
      )
      with patch("builtins.print"):
        rc = gha_runner.run_pr(_pr_event(), env, launcher, host=host)

    self.assertEqual(rc, 0)
    launcher.run_parallel.assert_not_called()
    self.assertIsNotNone(host.latest_security_gate)
    self.assertEqual(
        host.latest_security_gate.state, SecurityGateState.FAILURE
    )
    self.assertEqual(
        host.latest_security_gate.description,
        f"CodeMender: {len(findings)} security finding(s) detected",
    )


# Governing: docs/openspec/specs/distribute/spec.md#req-0002, SPEC-DIST, SPEC-GHA
class TestWorkflowConfigGuard(unittest.TestCase):

  def test_cm_merge_scan_check_config_behavior(self):
    workflow_path = os.path.join(
        _REPO_ROOT, ".github", "workflows", "cm-merge-scan.yml"
    )
    with open(workflow_path, "r", encoding="utf-8") as f:
      content = f.read()

    self.assertIn(
        "github.event.pull_request.head.repo.full_name == github.repository",
        content,
    )
    self.assertIn(
        "github.event_name == 'workflow_call'",
        content,
    )

    import re
    import subprocess
    import textwrap

    match = re.search(
        r"- name: Check CodeMender configuration\n(.*?)(?=\n      - |\Z)",
        content,
        re.DOTALL,
    )
    self.assertIsNotNone(match, "Check CodeMender configuration step not found")
    run_match = re.search(
        r"        run: \|\n((?:          .*\n?)+)", match.group(1)
    )
    self.assertIsNotNone(run_match, "run: | block not found in check-config")
    run_script = textwrap.dedent(run_match.group(1))

    with tempfile.TemporaryDirectory() as tmp:
      # 1. Unconfigured repository (missing credentials): exits 0, emits notice, sets configured=false
      for missing_case in (
          {"GCP_PROJECT_ID": "", "GCP_WIF_PROVIDER": "p", "GCP_SERVICE_ACCOUNT": "sa"},
          {"GCP_PROJECT_ID": "proj", "GCP_WIF_PROVIDER": "   ", "GCP_SERVICE_ACCOUNT": "sa"},
          {"GCP_PROJECT_ID": "proj", "GCP_WIF_PROVIDER": "p", "GCP_SERVICE_ACCOUNT": ""},
      ):
        with self.subTest(env_vars=missing_case):
          gh_out = os.path.join(tmp, "gh_out_unconfigured.txt")
          with open(gh_out, "w", encoding="utf-8"):
            pass
          env = dict(os.environ, GITHUB_OUTPUT=gh_out, **missing_case)
          res = subprocess.run(
              ["bash", "-c", run_script],
              env=env,
              capture_output=True,
              text=True,
              check=False,
          )
          self.assertEqual(res.returncode, 0, msg=res.stderr)
          self.assertIn("::notice::", res.stdout)
          with open(gh_out, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "configured=false")

      # 2. Configured repository (all credentials present): exits 0, sets configured=true
      gh_out_ok = os.path.join(tmp, "gh_out_configured.txt")
      with open(gh_out_ok, "w", encoding="utf-8"):
        pass
      env_ok = dict(
          os.environ,
          GITHUB_OUTPUT=gh_out_ok,
          GCP_PROJECT_ID="my-project",
          GCP_WIF_PROVIDER="projects/123/locations/global/workloadIdentityPools/p/providers/prov",
          GCP_SERVICE_ACCOUNT="cm@my-project.iam.gserviceaccount.com",
      )
      res_ok = subprocess.run(
          ["bash", "-c", run_script],
          env=env_ok,
          capture_output=True,
          text=True,
          check=False,
      )
      self.assertEqual(res_ok.returncode, 0, msg=res_ok.stderr)
      with open(gh_out_ok, "r", encoding="utf-8") as f:
        self.assertEqual(f.read().strip(), "configured=true")

  def test_cm_github_entrypoint_and_pyproject_contract(self):
    """Verify repohost.github.__main__.main delegates to gha_runner.main and pyproject.toml contract (SPEC-GHA REQ-0001, REQ-0004)."""
    with patch.object(gha_runner, "main", return_value=0) as mock_main:
      rc = github_main.main({"GITHUB_EVENT_NAME": "push"})
    self.assertEqual(rc, 0)
    mock_main.assert_called_once_with({"GITHUB_EVENT_NAME": "push"})

    pyproject_path = os.path.join(_REPO_ROOT, "packages", "repohost", "pyproject.toml")
    with open(pyproject_path, "rb") as f:
      pyproject = tomllib.load(f)
    deps = pyproject.get("project", {}).get("dependencies", [])
    self.assertIn("codemender-launcher", deps)
    scripts = pyproject.get("project", {}).get("scripts", {})
    self.assertEqual(scripts.get("cm-github"), "repohost.github.__main__:main")

  def test_composite_actions_and_workflow_contracts(self):
    """Verify composite action manifests and workflow template contracts (SPEC-GHA REQ-0001, REQ-0002, REQ-0007; SPEC-GHA-FIX REQ-0001)."""
    cloudrun_action_path = os.path.join(
        _REPO_ROOT, ".github", "actions", "codemender", "action.yml"
    )
    local_action_path = os.path.join(
        _REPO_ROOT, ".github", "actions", "codemender-local", "action.yml"
    )
    workflow_path = os.path.join(
        _REPO_ROOT, ".github", "workflows", "cm-merge-scan.yml"
    )

    with open(cloudrun_action_path, "r", encoding="utf-8") as f:
      cloudrun_yml = f.read()
    with open(local_action_path, "r", encoding="utf-8") as f:
      local_yml = f.read()
    with open(workflow_path, "r", encoding="utf-8") as f:
      workflow_yml = f.read()

    # Neither action defines a `backend:` input under `inputs:`
    self.assertNotRegex(cloudrun_yml, r"(?m)^  backend\s*:")
    self.assertNotRegex(local_yml, r"(?m)^  backend\s*:")

    # .github/actions/codemender/action.yml contract
    self.assertRegex(cloudrun_yml, r"(?m)^  max_parallel_fixes\s*:")
    self.assertIn("CODEMENDER_MAX_PARALLEL_FIXES", cloudrun_yml)
    self.assertRegex(
        cloudrun_yml, r'CODEMENDER_BACKEND:\s*["\']cloudrun["\']'
    )
    self.assertIn('"${PKG_DIR}/launcher[cloudrun]"', cloudrun_yml)
    self.assertIn("cm-github", cloudrun_yml)
    self.assertNotIn("gha_runner.py", cloudrun_yml)
    self.assertNotIn("requirements.txt", cloudrun_yml)
    self.assertNotIn("docker pull", cloudrun_yml)
    self.assertNotIn("runsc", cloudrun_yml)

    # .github/actions/codemender-local/action.yml contract
    self.assertRegex(local_yml, r"(?m)^  runner_image\s*:")
    self.assertRegex(local_yml, r"(?m)^  max_parallel_fixes\s*:")
    self.assertRegex(local_yml, r'CODEMENDER_BACKEND:\s*["\']local["\']')
    self.assertIn('"${PKG_DIR}/launcher"', local_yml)
    self.assertNotIn("launcher[cloudrun]", local_yml)
    self.assertIn("cm-github", local_yml)
    self.assertNotIn("gha_runner.py", local_yml)
    self.assertNotIn("requirements.txt", local_yml)

    # Neither action directory contains a requirements.txt
    self.assertFalse(
        os.path.exists(
            os.path.join(
                _REPO_ROOT, ".github", "actions", "codemender", "requirements.txt"
            )
        )
    )
    self.assertFalse(
        os.path.exists(
            os.path.join(
                _REPO_ROOT,
                ".github",
                "actions",
                "codemender-local",
                "requirements.txt",
            )
        )
    )

    # .github/workflows/cm-merge-scan.yml does not pass `backend:` in `with:`
    self.assertNotRegex(workflow_yml, r"(?m)^\s*#?\s*backend\s*:")


class TestM3OnDemandVerify(unittest.TestCase):

  def setUp(self):
    super().setUp()
    self._host = InMemoryRepoHost()
    self._temp_dir = tempfile.mkdtemp(prefix="gha-verify-test-")
    self._out_file = os.path.join(self._temp_dir, "github_output.txt")
    self._event_file = os.path.join(self._temp_dir, "event.json")

  def tearDown(self):
    shutil.rmtree(self._temp_dir, ignore_errors=True)
    super().tearDown()

  def _write_event(self, body="@cm-runner verify", assoc="MEMBER", parent_id=100):
    event = {
        "action": "created",
        "pull_request": {
            "number": 7,
            "head": {"sha": "aaaabbbb", "repo": {"full_name": "o/r"}},
            "base": {"sha": "11112222", "repo": {"full_name": "o/r"}},
        },
        "comment": {
            "id": 200,
            "in_reply_to_id": parent_id,
            "body": body,
            "author_association": assoc,
            "user": {"type": "User"},
        },
    }
    with open(self._event_file, "w", encoding="utf-8") as f:
      json.dump(event, f)
    return event

  def _env(self, **overrides):
    env = dict(
        _ENV,
        GITHUB_EVENT_NAME="pull_request_review_comment",
        GITHUB_EVENT_PATH=self._event_file,
        GITHUB_OUTPUT=self._out_file,
        RUNNER_TEMP=self._temp_dir,
    )
    env.update(overrides)
    return env

  def _read_outputs(self):
    if not os.path.isfile(self._out_file):
      return {}
    out = {}
    with open(self._out_file, encoding="utf-8") as f:
      for line in f:
        if "=" in line:
          k, v = line.rstrip("\n").split("=", 1)
          out[k] = v
    return out

  def test_has_verify_command_ignores_code_fences_and_spans(self):
    self.assertTrue(gha_runner._has_verify_command("please @cm-runner verify this"))
    self.assertFalse(gha_runner._has_verify_command("use `@cm-runner verify` here"))
    self.assertFalse(
        gha_runner._has_verify_command("```\n@cm-runner verify\n```")
    )

  def test_is_fork_pr_handles_fork_and_deleted_fork_repo(self):
    self.assertFalse(
        gha_runner.is_fork_pr({
            "pull_request": {
                "head": {"repo": {"full_name": "o/r"}},
                "base": {"repo": {"full_name": "o/r"}},
            }
        })
    )
    self.assertTrue(
        gha_runner.is_fork_pr({
            "pull_request": {
                "head": {"repo": {"full_name": "fork/r"}},
                "base": {"repo": {"full_name": "o/r"}},
            }
        })
    )
    self.assertTrue(
        gha_runner.is_fork_pr({
            "pull_request": {
                "head": {"repo": None},
                "base": {"repo": {"full_name": "o/r"}},
            }
        })
    )

  @patch.object(gha_runner, "has_other_active_verify_runs", return_value=True)
  @patch.object(gha_runner, "reply_to_comment")
  @patch.object(gha_runner, "list_verify_comments", return_value=[])
  @patch.object(gha_runner, "get_review_comment")
  def test_preflight_gates_auth_dedup_and_concurrency_cap(
      self, mock_get_parent, mock_list_verifies, mock_reply, _mock_active
  ):
    parent_body = (
        "<!-- codemender-finding:aabbcc112233 -->\n"
        "**CodeMender fix for finding 1:** 🔴 High / Critical · `CWE-78` · `Cmd Inj`"
    )
    mock_get_parent.return_value = {
        "id": 100,
        "user": {"type": "Bot"},
        "body": parent_body,
    }
    mock_reply.return_value = github_client.ReviewResult(
        ok=True, comment_id=301
    )

    # 1. Unauthorized contributor is skipped silently.
    self._write_event(assoc="CONTRIBUTOR")
    self.assertEqual(gha_runner.run_preflight(self._env(), host=self._host), 0)
    self.assertEqual(self._host.emitted_outputs.get("should_run"), "false")
    self.assertEqual(self._host.reactions, ())

    # 1b. Non-bot parent comment with spoofed finding tag is skipped.
    self._host = InMemoryRepoHost()
    self._write_event(assoc="MEMBER")
    mock_get_parent.return_value = {
        "id": 100,
        "user": {"type": "User"},
        "body": parent_body,
    }
    self.assertEqual(gha_runner.run_preflight(self._env(), host=self._host), 0)
    self.assertEqual(self._host.emitted_outputs.get("should_run"), "false")
    self.assertEqual(self._host.reactions, ())

    # 2. Authorized member on bot parent comment starts verification.
    self._host = InMemoryRepoHost()
    mock_get_parent.return_value = {
        "id": 100,
        "user": {"type": "Bot"},
        "body": parent_body,
    }
    self.assertEqual(gha_runner.run_preflight(self._env(), host=self._host), 0)
    self.assertEqual(
        self._host.emitted_outputs,
        {"should_run": "true", "verify_reply_id": "301"},
    )
    self.assertEqual(self._host.reactions, (("200", "eyes"),))

    # 3. Duplicate in-progress finding is skipped without reacting or replying.
    mock_reply.reset_mock()
    self._host = InMemoryRepoHost()
    mock_list_verifies.return_value = [
        gha_runner.VerifyComment("aabbcc112233", "in-progress", "", "", "", 301)
    ]
    with patch("builtins.print"):
      self.assertEqual(
          gha_runner.run_preflight(self._env(), host=self._host), 0
      )
    self.assertEqual(self._host.emitted_outputs.get("should_run"), "false")
    mock_reply.assert_not_called()
    self.assertEqual(self._host.reactions, ())

    # 4. Already-done finding replies with link to earlier verification.
    self._host = InMemoryRepoHost()
    mock_list_verifies.return_value = [
        gha_runner.VerifyComment(
            "aabbcc112233", "done", "DISMISSED", "https://c/301", "", 301
        )
    ]
    self.assertEqual(gha_runner.run_preflight(self._env(), host=self._host), 0)
    self.assertEqual(self._host.emitted_outputs.get("should_run"), "false")
    self.assertIn("already verified", mock_reply.call_args.args[4])
    self.assertIn("https://c/301", mock_reply.call_args.args[4])
    self.assertEqual(self._host.reactions, (("200", "eyes"),))

    # 5. Concurrency cap (MAX_CONCURRENT_VERIFIES = 4) blocks 5th active verify.
    mock_reply.reset_mock()
    self._host = InMemoryRepoHost()
    mock_list_verifies.return_value = [
        gha_runner.VerifyComment(f"f{i:011d}", "in-progress", "", "", "", 400 + i)
        for i in range(gha_runner.MAX_CONCURRENT_VERIFIES)
    ]
    self.assertEqual(gha_runner.run_preflight(self._env(), host=self._host), 0)
    self.assertEqual(self._host.emitted_outputs.get("should_run"), "false")
    self.assertIn(
        "Maximum concurrent verifications (4)", mock_reply.call_args.args[4]
    )
    self.assertEqual(self._host.reactions, (("200", "eyes"),))

  @patch.object(gha_runner, "update_review_comment")
  @patch.object(gha_runner, "get_sticky_comment_body", return_value="")
  @patch.object(gha_runner, "get_review_comment")
  def test_run_verify_reconstructs_sarif_and_updates_reply(
      self, mock_get_parent, _mock_sticky, mock_update
  ):
    event = self._write_event()
    mock_get_parent.return_value = {
        "id": 100,
        "user": {"type": "Bot"},
        "path": "src/archive/unpack.py",
        "line": 18,
        "body": (
            "<!-- codemender-finding:aabbcc112233 -->\n"
            "**CodeMender fix for finding 1:** 🔴 High / Critical · `CWE-78` · `Cmd Inj`"
        ),
    }
    mock_update.return_value = github_client.ReviewResult(
        ok=True, comment_id=301
    )
    verified_doc = {
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "CodeMender"}},
            "results": [{
                "ruleId": "CWE-78",
                "level": "error",
                "message": {"text": "Cmd Inj"},
                "properties": {
                    "status": "DISMISSED",
                    "dismiss_reason": "Input is sanitized upstream.",
                },
            }],
        }],
    }
    launcher = _launcher(scan_result=_scan_result(verified_doc))
    out_dir = os.path.join(self._temp_dir, "out")
    os.makedirs(out_dir)

    rc = gha_runner.run_verify(
        event,
        self._env(CODEMENDER_VERIFY_REPLY_ID="301"),
        launcher,
        out_dir=out_dir,
        host=self._host,
    )
    self.assertEqual(rc, 0)
    self.assertEqual(launcher.run.call_args.args[0], "verify")
    self.assertTrue(
        os.path.isfile(os.path.join(out_dir, "verified", "finding-aabbcc112233.sarif"))
    )
    updated_body = mock_update.call_args.args[3]
    self.assertIn("<!-- codemender-verify:done finding:aabbcc112233 status:DISMISSED -->", updated_body)
    self.assertIn("Input is sanitized upstream.", updated_body)

  @patch.object(gha_runner, "update_review_comment")
  @patch.object(gha_runner, "get_sticky_comment_body", return_value="")
  @patch.object(gha_runner, "get_review_comment")
  def test_run_verify_with_cloud_run_launcher(
      self, mock_get_parent, _mock_sticky, mock_update
  ):
    event = self._write_event()
    mock_get_parent.return_value = {
        "id": 100,
        "user": {"type": "Bot"},
        "path": "src/archive/unpack.py",
        "line": 18,
        "body": (
            "<!-- codemender-finding:aabbcc112233 -->\n"
            "**CodeMender fix for finding 1:** 🔴 High / Critical · `CWE-78` · `Cmd Inj`"
        ),
    }
    mock_update.return_value = github_client.ReviewResult(
        ok=True, comment_id=301
    )
    verified_doc = {
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "CodeMender"}},
            "results": [{
                "ruleId": "CWE-78",
                "level": "error",
                "message": {"text": "Cmd Inj"},
                "properties": {
                    "status": "VERIFIED",
                },
            }],
        }],
    }
    verified_bytes = json.dumps(verified_doc).encode("utf-8")
    out_dir = os.path.join(self._temp_dir, "cloudrun-verify-out")
    observed_args = {}

    def _fake_cr_run(command, args, log_level="info", timeout_seconds=3600):
      del log_level, timeout_seconds
      observed_args["command"] = command
      observed_args["args"] = list(args)
      finding_arg = args[args.index("--finding") + 1]
      observed_args["finding_exists"] = os.path.isfile(finding_arg)
      return MagicMock(status="SUCCESS", exit_code=0, stdout=verified_bytes, events=[])

    mock_client = MagicMock()
    mock_client.config.workspace_dir = self._temp_dir
    mock_client.config.out_dir = "/stale/prepopulated/out"
    mock_client.run.side_effect = _fake_cr_run
    launcher = CloudRunLauncher(client=mock_client)

    rc = gha_runner.run_verify(
        event,
        self._env(CODEMENDER_VERIFY_REPLY_ID="301"),
        launcher,
        out_dir=out_dir,
        host=self._host,
    )
    self.assertEqual(rc, 0)
    self.assertEqual(mock_client.config.out_dir, "/stale/prepopulated/out")
    self.assertEqual(observed_args["command"], "verify")
    self.assertTrue(observed_args["finding_exists"])
    self.assertEqual(
        observed_args["args"],
        [
            "--repo",
            self._temp_dir,
            "--finding",
            os.path.join(out_dir, "finding.sarif"),
            "--out",
            out_dir,
        ],
    )
    self.assertTrue(
        os.path.isfile(os.path.join(out_dir, "verified", "finding-aabbcc112233.sarif"))
    )
    updated_body = mock_update.call_args.args[3]
    self.assertIn("<!-- codemender-verify:done finding:aabbcc112233 status:VERIFIED -->", updated_body)

  @patch("subprocess.run")
  def test_preflight_pull_request_verifies_base_commit_present(self, mock_run):
    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
    env = self._env(GITHUB_EVENT_NAME="pull_request", BASE_SHA="abc123commit")
    rc = gha_runner.run_preflight(env)
    self.assertEqual(rc, 0)
    self.assertEqual(self._read_outputs().get("should_run"), "true")
    mock_run.assert_called_once_with(
        ["git", "cat-file", "-e", "abc123commit^{commit}"],
        capture_output=True,
        check=False,
    )

  @patch("subprocess.run")
  def test_preflight_pull_request_fetches_missing_base_commit(self, mock_run):
    # First cat-file fails, subsequent fetch succeeds
    mock_run.side_effect = [
        subprocess.CompletedProcess(args=[], returncode=1),
        subprocess.CompletedProcess(args=[], returncode=0),
    ]
    env = self._env(GITHUB_EVENT_NAME="pull_request", BASE_SHA="abc123commit")
    rc = gha_runner.run_preflight(env)
    self.assertEqual(rc, 0)
    self.assertEqual(self._read_outputs().get("should_run"), "true")
    self.assertEqual(mock_run.call_count, 2)
    mock_run.assert_any_call(
        ["git", "fetch", "--no-tags", "--depth=1", "origin", "abc123commit"],
        capture_output=True,
        check=False,
    )

  @patch("subprocess.run")
  def test_preflight_pull_request_fails_when_base_commit_unreachable(self, mock_run):
    # Both cat-file and fetch fail
    mock_run.side_effect = [
        subprocess.CompletedProcess(args=[], returncode=1),
        subprocess.CompletedProcess(args=[], returncode=1),
    ]
    env = self._env(GITHUB_EVENT_NAME="pull_request", BASE_SHA="abc123commit")
    rc = gha_runner.run_preflight(env)
    self.assertEqual(rc, 1)
    self.assertEqual(self._read_outputs().get("should_run"), "false")

  def test_preflight_pull_request_no_base_sha_succeeds(self):
    env = self._env(GITHUB_EVENT_NAME="pull_request")
    rc = gha_runner.run_preflight(env)
    self.assertEqual(rc, 0)
    self.assertEqual(self._read_outputs().get("should_run"), "true")

  def test_main_chmods_credentials_file_if_present(self):
    creds_file = os.path.join(self._temp_dir, "fake_creds.json")
    with open(creds_file, "w", encoding="utf-8") as f:
      f.write("{}")
    os.chmod(creds_file, 0o600)
    env = self._env(
        GITHUB_EVENT_NAME="other_event",
        GOOGLE_GHA_CREDS_PATH=creds_file,
    )
    gha_runner.main(env)
    self.assertEqual(os.stat(creds_file).st_mode & 0o777, 0o644)


class TestGhaRunnerRepoHostWiring(unittest.TestCase):
  """Unit tests for RepoHost dependency injection into gha_runner."""

  def setUp(self):
    super().setUp()
    self._temp_dir = tempfile.mkdtemp(prefix="repohost-wiring-test-")
    self._workspace = os.path.join(self._temp_dir, "workspace")
    os.makedirs(self._workspace)
    self._output_file = os.path.join(self._temp_dir, "outputs")
    with open(self._output_file, "w", encoding="utf-8") as f:
      f.write("")
    self._event_file = os.path.join(self._temp_dir, "event.json")
    with open(self._event_file, "w", encoding="utf-8") as f:
      json.dump(_pr_event(), f)
    self._env = dict(
        _ENV,
        GITHUB_EVENT_PATH=self._event_file,
        GITHUB_WORKSPACE=self._workspace,
        GITHUB_OUTPUT=self._output_file,
        RUNNER_TEMP=self._temp_dir,
    )

  def tearDown(self):
    shutil.rmtree(self._temp_dir, ignore_errors=True)
    super().tearDown()

  def test_run_pr_clean_scan_wires_repohost_summary_and_gate(self):
    host = InMemoryRepoHost(
        change_context=ChangeContext(
            repo_id="o/r",
            change_id="7",
            base_sha="1111222233334444",
            head_sha="5555666677778888",
            base_ref="main",
            head_ref="feat",
        )
    )
    launcher = _launcher()
    rc = gha_runner.run_pr(_pr_event(), self._env, launcher, host=host)
    self.assertEqual(rc, 0)
    self.assertIsNotNone(host.current_summary)
    self.assertIn("✅ **No vulnerabilities found**", host.current_summary or "")
    self.assertIsNotNone(host.latest_security_gate)
    self.assertEqual(
        host.latest_security_gate.state, SecurityGateState.SUCCESS
    )

  def test_run_pr_findings_fix_disabled_wires_security_gate_failure(self):
    host = InMemoryRepoHost(
        change_context=ChangeContext(
            repo_id="o/r",
            change_id="7",
            base_sha="1111222233334444",
            head_sha="5555666677778888",
            base_ref="main",
            head_ref="feat",
        )
    )
    launcher = _launcher(scan_result=_scan_result(_SARIF_WITH_FINDINGS))
    env = dict(self._env, CODEMENDER_FIX="false")
    rc = gha_runner.run_pr(_pr_event(), env, launcher, host=host)
    self.assertEqual(rc, 0)
    self.assertIsNotNone(host.current_summary)
    self.assertIn("Found 2 vulnerabilities", host.current_summary or "")
    self.assertIsNotNone(host.latest_security_gate)
    self.assertEqual(
        host.latest_security_gate.state, SecurityGateState.FAILURE
    )

  def test_run_pr_wires_change_context_shas_to_launcher(self):
    host = InMemoryRepoHost(
        change_context=ChangeContext(
            repo_id="custom/repo",
            change_id="99",
            base_sha="base_from_host_sha_1234",
            head_sha="head_from_host_sha_5678",
            base_ref="main",
            head_ref="feature",
        )
    )
    launcher = _launcher()
    rc = gha_runner.run_pr(_pr_event(), self._env, launcher, host=host)
    self.assertEqual(rc, 0)
    cmd = launcher.run.call_args_list[0].args
    self.assertIn("base_from_host_sha_1234", cmd[1])

  def test_security_gate_failure_logs_warning_and_does_not_crash(self):
    class GateFailingRepoHost(InMemoryRepoHost):

      def set_security_gate(self, gate):
        raise RuntimeError("permission denied: statuses: write required")

    host = GateFailingRepoHost()
    launcher = _launcher()
    with self.assertLogs("codemender-github-actions", level="WARNING") as cm:
      rc = gha_runner.run_pr(_pr_event(), self._env, launcher, host=host)
    self.assertEqual(rc, 0)
    self.assertTrue(
        any("Could not update security gate" in log for log in cm.output)
    )

  @patch("subprocess.run")
  def test_run_preflight_wires_repohost_emit_ci_outputs(self, mock_run):
    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
    host = InMemoryRepoHost()
    env = dict(self._env, GITHUB_EVENT_NAME="pull_request")
    rc = gha_runner.run_preflight(env, host=host)
    self.assertEqual(rc, 0)
    self.assertEqual(host.emitted_outputs.get("should_run"), "true")

  def test_run_preflight_skips_non_pr_and_emits_output(self):
    host = InMemoryRepoHost()
    env = dict(self._env, GITHUB_EVENT_NAME="push")
    rc = gha_runner.run_preflight(env, host=host)
    self.assertEqual(rc, 0)
    self.assertEqual(host.emitted_outputs.get("should_run"), "false")

  @patch.object(gha_runner, "get_review_comment")
  @patch.object(gha_runner, "reply_to_comment")
  def test_run_preflight_wires_repohost_add_reaction(
      self, mock_reply, mock_get_parent
  ):
    mock_reply.return_value = github_client.ReviewResult(
        ok=True, comment_id=301
    )
    mock_get_parent.return_value = {
        "id": 100,
        "user": {"type": "Bot"},
        "body": "<!-- codemender-finding:aabbcc112233 -->\nFix",
    }
    temp_dir = tempfile.mkdtemp(prefix="preflight-react-")
    event_file = os.path.join(temp_dir, "event.json")
    event = {
        "action": "created",
        "pull_request": {"number": 7},
        "comment": {
            "id": 555,
            "in_reply_to_id": 100,
            "body": "@cm-runner verify",
            "author_association": "MEMBER",
            "user": {"type": "User"},
        },
    }
    with open(event_file, "w", encoding="utf-8") as f:
      json.dump(event, f)
    host = InMemoryRepoHost()
    env = dict(
        self._env,
        GITHUB_EVENT_NAME="pull_request_review_comment",
        GITHUB_EVENT_PATH=event_file,
    )
    try:
      rc = gha_runner.run_preflight(env, host=host)
      self.assertEqual(rc, 0)
      self.assertIn(("555", "eyes"), host.reactions)
    finally:
      shutil.rmtree(temp_dir, ignore_errors=True)

  @patch.object(gha_runner, "run_pr", return_value=0)
  def test_main_cli_passes_injected_host_to_run_pr(self, mock_run_pr):
    host = InMemoryRepoHost()
    rc = gha_runner.main(self._env, host=host)
    self.assertEqual(rc, 0)
    mock_run_pr.assert_called_once()
    self.assertIs(mock_run_pr.call_args.kwargs.get("host"), host)

  @patch.object(gha_runner, "main", return_value=0)
  def test_github_main_forwards_host_to_gha_runner(self, mock_main):
    host = InMemoryRepoHost()
    rc = github_main.main(self._env, host=host)
    self.assertEqual(rc, 0)
    mock_main.assert_called_once_with(self._env, host=host)

  @patch.object(gha_runner, "get_sticky_comment_body", return_value="")
  @patch.object(gha_runner, "get_review_comment")
  def test_run_verify_wires_repohost_reply_to_comment(
      self, mock_get_parent, _mock_sticky
  ):
    host = InMemoryRepoHost()
    event = {
        "action": "created",
        "pull_request": {"number": 7},
        "comment": {
            "id": 200,
            "in_reply_to_id": 100,
            "body": "@cm-runner verify",
        },
    }
    mock_get_parent.return_value = {
        "id": 100,
        "user": {"type": "Bot"},
        "path": "src/archive/unpack.py",
        "line": 18,
        "body": (
            "<!-- codemender-finding:aabbcc112233 -->\n"
            "**CodeMender fix for finding 1:** 🔴 High · `CWE-78`"
        ),
    }
    verified_doc = {
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "cm-runner"}},
            "results": [{
                "ruleId": "CWE-78",
                "level": "error",
                "message": {"text": "Cmd Inj"},
                "properties": {
                    "status": "VERIFIED",
                    "dismiss_reason": "",
                },
            }],
        }],
    }
    launcher = _launcher(scan_result=_scan_result(verified_doc))
    out_dir = os.path.join(self._temp_dir, "verify_out")
    os.makedirs(out_dir, exist_ok=True)
    rc = gha_runner.run_verify(
        event, self._env, launcher, out_dir=out_dir, host=host
    )
    self.assertEqual(rc, 0)
    self.assertGreaterEqual(len(host.replies), 1)
    self.assertEqual(host.replies[0][0], "100")
    self.assertIn("Verifying finding `aabbcc112233`", host.replies[0][1])

  @patch.object(gha_runner, "get_sticky_comment_body", return_value="")
  @patch.object(gha_runner, "get_review_comment")
  def test_run_verify_uses_artifact_store_when_available(
      self, mock_get_parent, _mock_sticky
  ):
    host = InMemoryRepoHost()
    store = InMemoryArtifactStore()
    canonical_sarif = {
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "CodeMenderCanonical"}},
            "results": [{
                "ruleId": "CWE-78",
                "level": "error",
                "message": {"text": "Canonical SARIF finding"},
                "partialFingerprints": {
                    "primaryLocationLineHash": "aabbcc112233"
                },
            }],
        }],
    }
    store.seed_finding_sarif("aabbcc112233", canonical_sarif, change_id="7")

    event = {
        "action": "created",
        "pull_request": {"number": 7},
        "comment": {
            "id": 200,
            "in_reply_to_id": 100,
            "body": "@cm-runner verify",
        },
    }
    mock_get_parent.return_value = {
        "id": 100,
        "user": {"type": "Bot"},
        "path": "src/archive/unpack.py",
        "line": 18,
        "body": (
            "<!-- codemender-finding:aabbcc112233 -->\n"
            "**CodeMender fix for finding 1:** 🔴 High · `CWE-78`"
        ),
    }
    verified_doc = {
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "cm-runner"}},
            "results": [{
                "ruleId": "CWE-78",
                "level": "error",
                "message": {"text": "Cmd Inj"},
                "properties": {"status": "VERIFIED"},
            }],
        }],
    }
    launcher = _launcher(scan_result=_scan_result(verified_doc))
    out_dir = os.path.join(self._temp_dir, "verify_store_out")
    os.makedirs(out_dir, exist_ok=True)
    rc = gha_runner.run_verify(
        event,
        self._env,
        launcher,
        out_dir=out_dir,
        host=host,
        store=store,
    )
    self.assertEqual(rc, 0)
    finding_path = os.path.join(out_dir, "finding.sarif")
    with open(finding_path, "r", encoding="utf-8") as f:
      written_finding = json.load(f)
    self.assertEqual(
        written_finding["runs"][0]["tool"]["driver"]["name"],
        "CodeMenderCanonical",
    )

  @patch.object(gha_runner, "get_sticky_comment_body", return_value="")
  @patch.object(gha_runner, "get_review_comment")
  def test_run_verify_falls_back_when_artifact_store_fails(
      self, mock_get_parent, _mock_sticky
  ):
    host = InMemoryRepoHost()
    mock_store = MagicMock()
    mock_store.get_finding_sarif.side_effect = ArtifactStoreError(
        "Simulated download failure"
    )

    event = {
        "action": "created",
        "pull_request": {"number": 7},
        "comment": {
            "id": 200,
            "in_reply_to_id": 100,
            "body": "@cm-runner verify",
        },
    }
    mock_get_parent.return_value = {
        "id": 100,
        "user": {"type": "Bot"},
        "path": "src/archive/unpack.py",
        "line": 18,
        "body": (
            "<!-- codemender-finding:aabbcc112233 -->\n"
            "**CodeMender fix for finding 1:** 🔴 High · `CWE-78`"
        ),
    }
    verified_doc = {
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "cm-runner"}},
            "results": [{
                "ruleId": "CWE-78",
                "level": "error",
                "message": {"text": "Cmd Inj"},
                "properties": {"status": "VERIFIED"},
            }],
        }],
    }
    launcher = _launcher(scan_result=_scan_result(verified_doc))
    out_dir = os.path.join(self._temp_dir, "verify_store_fallback_out")
    os.makedirs(out_dir, exist_ok=True)
    rc = gha_runner.run_verify(
        event,
        self._env,
        launcher,
        out_dir=out_dir,
        host=host,
        store=mock_store,
    )
    self.assertEqual(rc, 0)
    finding_path = os.path.join(out_dir, "finding.sarif")
    with open(finding_path, "r", encoding="utf-8") as f:
      written_finding = json.load(f)
    self.assertEqual(
        written_finding["runs"][0]["tool"]["driver"]["name"],
        "CodeMender",
    )

  def test_create_artifact_store_returns_github_actions_store(self):
    store = gha_runner.create_artifact_store(
        {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "tok"}
    )
    self.assertIsInstance(store, gha_runner.GitHubActionsArtifactStore)
    self.assertEqual(store._repo, "o/r")
    self.assertEqual(store._token, "tok")

  def test_create_artifact_store_extracts_context_from_event(self):
    event = {
        "repository": {"full_name": "event-owner/event-repo"},
        "pull_request": {"number": 99},
    }
    store = gha_runner.create_artifact_store(
        {"GITHUB_TOKEN": "tok"}, event=event
    )
    self.assertIsInstance(store, gha_runner.GitHubActionsArtifactStore)
    self.assertEqual(store._repo, "event-owner/event-repo")
    self.assertEqual(store._pr_number, 99)

  def test_create_artifact_store_respects_explicit_overrides(self):
    event = {
        "repository": {"full_name": "event-owner/event-repo"},
        "pull_request": {"number": 99},
    }
    store = gha_runner.create_artifact_store(
        {"GITHUB_REPOSITORY": "env-owner/env-repo", "GITHUB_TOKEN": "tok"},
        event=event,
        repo="custom-owner/custom-repo",
        pr_number=123,
    )
    self.assertIsInstance(store, gha_runner.GitHubActionsArtifactStore)
    self.assertEqual(store._repo, "custom-owner/custom-repo")
    self.assertEqual(store._pr_number, 123)


if __name__ == "__main__":
  unittest.main()
