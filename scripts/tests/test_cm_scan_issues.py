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
"""Unit tests for scripts/cm_scan_issues.py (no network)."""

import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

_SCRIPT = Path(__file__).resolve().parents[1] / "cm_scan_issues.py"
_spec = importlib.util.spec_from_file_location("cm_scan_issues", _SCRIPT)
csi = importlib.util.module_from_spec(_spec)
sys.modules["cm_scan_issues"] = csi
_spec.loader.exec_module(csi)


def _finding(i, severity="ERROR", fp=None, **kw):
  f = {"finding_id": f"abc-{i:03d}", "scan_job_id": "find-abc", "repo": "acme/widgets",
       "commit_sha": "0123456789abcdef0123", "fingerprint": fp if fp is not None else f"fp{i}",
       "rule_id": f"CWE-{i}", "severity": severity, "message": f"bad thing {i}",
       "file_path": f"src/f{i}.py", "start_line": 10 + i, "end_line": 12 + i}
  f.update(kw)
  return f


class FakeIssues:
  def __init__(self, existing=None):
    self.existing = existing or {}
    self.created = []
    self.label_ensured = False

  def existing_fingerprints(self):
    return dict(self.existing)

  def ensure_label(self):
    self.label_ensured = True

  def create(self, title, body, labels):
    self.created.append((title, body, labels))
    return f"https://github.com/acme/widgets/issues/{len(self.created)}"


class FakeMcp:
  def __init__(self, statuses):
    self.statuses = list(statuses)
    self.calls = []

  def call(self, tool, args):
    self.calls.append((tool, args))
    if tool == "cm_find":
      return {"job_id": "find-abc", "status": "STAGING"}
    if tool == "cm_job_status":
      return {"job_id": args["job_id"], "status": self.statuses.pop(0)}
    raise AssertionError(tool)


class PublishTest(unittest.TestCase):

  def test_creates_one_issue_per_finding_with_marker_and_labels(self):
    gh = FakeIssues()
    res = csi.publish([_finding(1), _finding(2, "WARNING")], gh, "acme/widgets", log=lambda _: None)
    self.assertEqual(len(res.created), 2)
    self.assertTrue(gh.label_ensured)
    title, body, labels = gh.created[0]
    self.assertEqual(title, "[CodeMender] ERROR: CWE-1 in src/f1.py:11")
    self.assertIn("<!-- codemender:fingerprint=fp1 -->", body)
    self.assertIn("https://github.com/acme/widgets/blob/0123456789abcdef0123/src/f1.py#L11-L13", body)
    self.assertIn("`abc-001`", body)
    self.assertEqual(labels, ["codemender", "severity:error"])
    self.assertEqual(gh.created[1][2], ["codemender", "severity:warning"])

  def test_skips_findings_already_filed_and_duplicates_within_scan(self):
    gh = FakeIssues(existing={"fp1": "https://github.com/acme/widgets/issues/7"})
    res = csi.publish([_finding(1), _finding(2), _finding(3, fp="fp2")], gh, "acme/widgets",
                      log=lambda _: None)
    self.assertEqual(len(gh.created), 1)
    self.assertEqual(res.skipped, ["https://github.com/acme/widgets/issues/7", "fp2"])

  def test_marker_round_trips_through_existing_fingerprint_parser(self):
    body = csi.issue_body(_finding(5, fp="sha256:ab/cd e"), "acme/widgets")
    self.assertEqual(csi._MARKER_RE.findall(body), ["sha256:ab/cd_e"])

  def test_severity_threshold_and_ordering(self):
    gh = FakeIssues()
    res = csi.publish([_finding(1, "NOTE"), _finding(2, "WARNING"), _finding(3, "ERROR")], gh,
                      "acme/widgets", min_severity="WARNING", log=lambda _: None)
    self.assertEqual(res.filtered, 1)
    self.assertEqual([t for t, _, _ in gh.created],
                     ["[CodeMender] ERROR: CWE-3 in src/f3.py:13",
                      "[CodeMender] WARNING: CWE-2 in src/f2.py:12"])

  def test_max_issues_caps_creation(self):
    gh = FakeIssues()
    res = csi.publish([_finding(i) for i in range(5)], gh, "acme/widgets", max_issues=2,
                      log=lambda _: None)
    self.assertEqual(len(res.created), 2)

  def test_dry_run_creates_nothing(self):
    gh = FakeIssues()
    res = csi.publish([_finding(1)], gh, "acme/widgets", dry_run=True, log=lambda _: None)
    self.assertEqual(gh.created, [])
    self.assertFalse(gh.label_ensured)
    self.assertEqual(len(res.created), 1)

  def test_fingerprint_fallback_without_fingerprint(self):
    self.assertEqual(csi.fingerprint_of(_finding(1, fp="")), "CWE-1:src/f1.py:11")


class ScanTest(unittest.TestCase):

  def test_run_scan_polls_until_terminal(self):
    mcp = FakeMcp(["RUNNING", "RUNNING", "SUCCEEDED"])
    view = csi.run_scan(mcp, "acme/widgets", "main", timeout_s=10_000, log=lambda _: None)
    self.assertEqual(view["status"], "SUCCEEDED")
    self.assertEqual(mcp.calls[0], ("cm_find", {"repo": "acme/widgets", "ref": "main"}))
    self.assertEqual([c[0] for c in mcp.calls[1:]], ["cm_job_status"] * 3)
    self.assertEqual(mcp.calls[1][1]["wait_seconds"], 600)

  def test_wait_for_job_times_out_with_resume_hint(self):
    now = [0.0]

    def clock():
      now[0] += 400
      return now[0]

    mcp = FakeMcp(["RUNNING"] * 10)
    with self.assertRaisesRegex(csi.ScanError, "scan_job_id=find-abc"):
      csi.wait_for_job(mcp, "find-abc", timeout_s=1000, log=lambda _: None, clock=clock)


class ParseToolResultTest(unittest.TestCase):

  def test_structured_content(self):
    res = types.SimpleNamespace(structuredContent={"job_id": "x"}, content=[], isError=False)
    self.assertEqual(csi.parse_tool_result(res), {"job_id": "x"})

  def test_wrapped_result(self):
    res = types.SimpleNamespace(structuredContent={"result": {"a": 1}}, content=[], isError=False)
    self.assertEqual(csi.parse_tool_result(res), {"a": 1})

  def test_text_content_json(self):
    res = types.SimpleNamespace(structuredContent=None,
                                content=[types.SimpleNamespace(text=json.dumps({"findings": []}))],
                                isError=False)
    self.assertEqual(csi.parse_tool_result(res), {"findings": []})

  def test_error_payload_raises_in_client(self):
    client = csi.McpClient("https://x.run.app")
    self.assertEqual(client.url, "https://x.run.app/mcp")
    client._call = lambda tool, args: _coro({"error": "boom"})  # pylint: disable=protected-access
    with self.assertRaisesRegex(csi.ScanError, "boom"):
      client.call("cm_find", {})


  def test_auth_failure_is_not_retried_and_is_readable(self):
    client = csi.McpClient("https://x.run.app/mcp")
    calls = []

    async def boom(tool, args):
      calls.append(tool)
      raise ExceptionGroup("tg", [RuntimeError("Client error '401 Unauthorized' for url")])

    client._call = boom  # pylint: disable=protected-access
    with self.assertRaisesRegex(csi.ScanError, "401 Unauthorized.*CODEMENDER_MCP_TOKEN"):
      client.call("cm_find", {})
    self.assertEqual(calls, ["cm_find"])

  def test_transient_failure_is_retried(self):
    client = csi.McpClient("https://x.run.app/mcp")
    attempts = []

    async def flaky(tool, args):
      attempts.append(1)
      if len(attempts) < 2:
        raise ConnectionError("reset by peer")
      return {"job_id": "j"}

    client._call = flaky  # pylint: disable=protected-access
    orig_sleep, csi.time.sleep = csi.time.sleep, lambda _: None
    try:
      self.assertEqual(client.call("cm_job_status", {}), {"job_id": "j"})
    finally:
      csi.time.sleep = orig_sleep
    self.assertEqual(len(attempts), 2)


async def _coro(value):
  return value


if __name__ == "__main__":
  unittest.main()
