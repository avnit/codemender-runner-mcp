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

"""Unit tests for the gha_runner entry point (M1 find-diff + M2 parallel fixes)."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
)

import gha_runner  # pylint: disable=g-import-not-at-top
from launcher.base import LauncherError, ParallelResult, RunResult  # pylint: disable=g-import-not-at-top

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
  if error:
    launcher.run.side_effect = error
    return launcher
  launcher.run.return_value = scan_result if scan_result is not None else _scan_result()
  return launcher


@patch.object(gha_runner, "post_or_update_sticky_comment", return_value="https://c")
class TestRunPr(unittest.TestCase):

  def test_clean_scan_posts_no_vulnerabilities_comment(self, mock_post):
    launcher = _launcher()

    self.assertEqual(gha_runner.run_pr(_pr_event(), _ENV, launcher), 0)

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
    token, repo, pr, body = mock_post.call_args.args
    self.assertEqual((token, repo, pr), ("tok", "o/r", 7))
    self.assertIn("✅ **No vulnerabilities found**", body)
    self.assertIn("`111122223333`", body)
    self.assertIn("https://github.com/o/r/actions/runs/123", body)

  def test_custom_diff_workers_is_passed_to_find_diff(self, mock_post):
    launcher = _launcher()
    env = dict(_ENV, CODEMENDER_DIFF_WORKERS="12")

    self.assertEqual(gha_runner.run_pr(_pr_event(), env, launcher), 0)

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

  def test_invalid_diff_workers_in_run_pr_fails_and_posts_error(self, mock_post):
    launcher = _launcher()
    env = dict(_ENV, CODEMENDER_DIFF_WORKERS="0")

    self.assertEqual(gha_runner.run_pr(_pr_event(), env, launcher), 1)
    launcher.run.assert_not_called()
    body = mock_post.call_args.args[3]
    self.assertIn("❌ **CodeMender scan failed.**", body)
    self.assertIn("Invalid CODEMENDER_DIFF_WORKERS", body)

  def test_findings_posts_summary_table_and_details(self, mock_post):
    launcher = _launcher(scan_result=_scan_result(_SARIF_WITH_FINDINGS))

    self.assertEqual(gha_runner.run_pr(_pr_event(), _ENV, launcher), 0)

    body = mock_post.call_args.args[3]
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

  def test_fork_pr_runs_and_handles_comment_failure(self, mock_post):
    launcher = _launcher()
    self.assertEqual(gha_runner.run_pr(_pr_event(head_repo="fork/r"), _ENV, launcher), 0)
    self.assertEqual(launcher.run.call_count, 1)

    mock_post.return_value = None
    with patch("builtins.print") as mock_print:
      rc = gha_runner.run_pr(_pr_event(head_repo="fork/r"), _ENV, launcher)
    self.assertEqual(rc, 1)
    printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list)
    self.assertIn("Send write tokens to workflows from pull requests", printed)

  def test_find_diff_and_launcher_failures_post_error_and_exit_1(self, mock_post):
    launcher = _launcher(scan_result=_scan_result(exit_code=3, raw=b""))
    self.assertEqual(gha_runner.run_pr(_pr_event(), _ENV, launcher), 1)
    self.assertIn("❌ **CodeMender scan failed.**", mock_post.call_args.args[3])
    self.assertIn("`3`", mock_post.call_args.args[3])

    launcher2 = _launcher(error=LauncherError("docker not found"))
    self.assertEqual(gha_runner.run_pr(_pr_event(), _ENV, launcher2), 1)
    self.assertIn("docker not found", mock_post.call_args.args[3])

    launcher3 = _launcher(scan_result=_scan_result(raw=b'{"event": "other"}'))
    self.assertEqual(gha_runner.run_pr(_pr_event(), _ENV, launcher3), 1)
    self.assertIn(
        "did not emit a valid SARIF 2.1.0 document", mock_post.call_args.args[3]
    )


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

    def _capture(event, env, launcher, out_dir=None):
      del event, env
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

  @patch.object(gha_runner, "post_or_update_sticky_comment", return_value="https://c")
  @patch.object(gha_runner, "list_finding_comments", return_value={})
  @patch.object(gha_runner.fix_comments, "checked_out_commit", return_value="aaaabbbbccccdddd")
  @patch.object(gha_runner.fix_comments, "FixPoster")
  def test_run_pr_with_fixes_updates_sticky_comment_in_place(
      self,
      mock_poster_cls,
      _mock_head,
      _mock_earlier,
      mock_post_sticky,
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
      findings = gha_runner.SarifDocument(data=_SARIF_WITH_FINDINGS).to_findings()
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
              gha_runner.fix_comments.SUGGESTED, "https://github.com/o/r/pull/7#review-1"
          ),
          gha_runner.FixCell(gha_runner.fix_comments.NO_FIX),
      ]

      with patch("builtins.print"):
        rc = gha_runner.run_pr(_pr_event(), env, launcher, out_dir=out_dir)

      self.assertEqual(rc, 0)
      self.assertEqual(launcher.run_parallel.call_args.kwargs["max_workers"], 3)
      self.assertTrue(
          os.path.isfile(os.path.join(out_dir, "findings", "finding-001.sarif"))
      )
      self.assertTrue(
          os.path.isfile(os.path.join(out_dir, "fixes", "finding-001.diff"))
      )
      bodies = [c.args[3] for c in mock_post_sticky.call_args_list]
      self.assertGreaterEqual(len(bodies), 2)
      self.assertIn("⏳ Generating fixes for 2 findings.", bodies[0])
      self.assertIn("| ⏳ Working |", bodies[0])
      final_body = bodies[-1]
      self.assertIn("[✅ Suggested](https://github.com/o/r/pull/7#review-1)", final_body)
      self.assertIn("| ❌ No fix |", final_body)
      self.assertIn("**Fixes:** ✅ 1 suggested · ❌ 1 no fix.", final_body)


if __name__ == "__main__":
  unittest.main()
