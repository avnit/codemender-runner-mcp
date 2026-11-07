#!/usr/bin/env python3
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
"""Scan a repository through the CodeMender MCP server and file GitHub issues.

1. `cm_find(repo, ref)` on the MCP server: clones the repo, runs a full scan in
   a new Cloud Run Job execution and stores every finding in BigQuery.
2. Polls `cm_job_status` until the scan finishes.
3. `cm_list_findings(scan_job_id)` and opens one GitHub issue per finding in the
   issues repository. Issues carry a hidden fingerprint marker so re-running a
   scan never duplicates an issue that already exists (open or closed).

Used by .github/workflows/cm-repo-scan.yml; also runnable locally:

  CODEMENDER_MCP_URL=https://codemender-mcp-xxx.run.app/mcp \
  CODEMENDER_MCP_TOKEN=... GITHUB_TOKEN=... \
  python scripts/cm_scan_issues.py --repo owner/repo --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

LABEL = "codemender"
LABEL_COLOR = "B60205"
SEVERITY_RANK = {"ERROR": 3, "WARNING": 2, "NOTE": 1, "NONE": 0}
_MARKER_RE = re.compile(r"<!--\s*codemender:fingerprint=([^\s>]+)\s*-->")
_TERMINAL = ("SUCCEEDED", "FAILED")


class ScanError(RuntimeError):
  """A scan or publishing step failed."""


# --------------------------------------------------------------------- MCP
class McpClient:
  """Calls tools on the CodeMender MCP server (streamable HTTP, stateless).

  A fresh session per call keeps multi-hour polling resilient to dropped
  connections and server restarts.
  """

  def __init__(self, url: str, token: str = "") -> None:
    self.url = url.rstrip("/")
    if not self.url.endswith("/mcp"):
      self.url += "/mcp"
    self.headers = {"Authorization": f"Bearer {token}"} if token else {}

  def call(self, tool: str, args: dict[str, Any], retries: int = 3) -> dict[str, Any]:
    for attempt in range(retries + 1):
      try:
        result = asyncio.run(self._call(tool, args))
        break
      except ScanError:
        raise
      except Exception as e:  # pylint: disable=broad-except  # incl. ExceptionGroup
        cause = _root_cause(e)
        fatal = any(code in cause for code in ("401", "403", "404"))
        if fatal or attempt == retries:
          hint = " (check CODEMENDER_MCP_TOKEN)" if ("401" in cause or "403" in cause) else ""
          raise ScanError(f"MCP call {tool} to {self.url} failed: {cause}{hint}") from None
        time.sleep(min(60, 5 * 2 ** attempt))
    if isinstance(result, dict) and result.get("error"):
      raise ScanError(f"{tool} failed: {result['error']}")
    return result

  async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    from mcp import ClientSession  # pylint: disable=import-outside-toplevel
    from mcp.client.streamable_http import streamablehttp_client  # pylint: disable=import-outside-toplevel

    async with streamablehttp_client(self.url, headers=self.headers,
                                     timeout=60, sse_read_timeout=900) as (read, write, _):
      async with ClientSession(read, write) as session:
        await session.initialize()
        res = await session.call_tool(tool, args)
    return parse_tool_result(res)


def _root_cause(e: BaseException) -> str:
  while getattr(e, "exceptions", None):  # unwrap anyio/asyncio ExceptionGroups
    e = e.exceptions[0]
  return f"{type(e).__name__}: {e}".splitlines()[0]


def parse_tool_result(res: Any) -> dict[str, Any]:
  """Extracts the dict a tool returned (structured content or JSON text)."""
  structured = getattr(res, "structuredContent", None)
  if isinstance(structured, dict):
    # FastMCP wraps non-object return types as {"result": ...}.
    return structured.get("result", structured) if set(structured) == {"result"} else structured
  for item in getattr(res, "content", None) or []:
    text = getattr(item, "text", None)
    if text:
      try:
        data = json.loads(text)
      except ValueError:
        raise ScanError(f"Unexpected tool output: {text[:500]}") from None
      if isinstance(data, dict):
        return data
  if getattr(res, "isError", False):
    raise ScanError("Tool call failed without details.")
  return {}


def run_scan(mcp: Any, repo: str, ref: str, timeout_s: int, poll_s: int = 600,
             log: Callable[[str], None] = print,
             clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
  """Starts cm_find and waits for it; returns the final job view."""
  job = mcp.call("cm_find", {"repo": repo, "ref": ref})
  job_id = job.get("job_id") or ""
  if not job_id:
    raise ScanError(f"cm_find returned no job_id: {job}")
  log(f"Started scan {job_id} of {repo}{'@' + ref if ref else ''}")
  return wait_for_job(mcp, job_id, timeout_s, poll_s, log, clock)


def wait_for_job(mcp: Any, job_id: str, timeout_s: int, poll_s: int = 600,
                 log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
  deadline = clock() + timeout_s
  while True:
    remaining = int(deadline - clock())
    view = mcp.call("cm_job_status", {"job_id": job_id,
                                      "wait_seconds": max(1, min(poll_s, remaining))})
    status = view.get("status", "")
    log(f"  {job_id}: {status}")
    if status in _TERMINAL:
      return view
    if clock() >= deadline:
      raise ScanError(f"Timed out waiting for {job_id} (still {status}). "
                      f"Re-run with scan_job_id={job_id} to publish once it finishes.")


# ------------------------------------------------------------------ GitHub
class GitHubIssues:
  """Minimal GitHub REST client for the issues of one repository."""

  def __init__(self, repo: str, token: str, api_url: str = "https://api.github.com",
               session: Any = None) -> None:
    self.repo = repo
    self.api = api_url.rstrip("/")
    if session is None:
      import requests  # pylint: disable=import-outside-toplevel
      session = requests.Session()
    self.http = session
    self.http.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })

  def _req(self, method: str, path: str, **kw: Any) -> Any:
    for attempt in range(5):
      r = self.http.request(method, f"{self.api}/repos/{self.repo}{path}", timeout=30, **kw)
      # Secondary rate limit / abuse detection: back off and retry.
      if r.status_code in (403, 429) and ("rate limit" in r.text.lower() or "retry-after" in r.headers):
        time.sleep(int(r.headers.get("retry-after", 10 * (attempt + 1))))
        continue
      if r.status_code >= 400:
        raise ScanError(f"GitHub {method} {path}: {r.status_code} {r.text[:300]}")
      return r.json() if r.content else None
    raise ScanError(f"GitHub {method} {path}: rate limited")

  def ensure_label(self) -> None:
    try:
      self._req("GET", f"/labels/{LABEL}")
    except ScanError:
      self._req("POST", "/labels", json={"name": LABEL, "color": LABEL_COLOR,
                                         "description": "Security finding from CodeMender"})

  def existing_fingerprints(self) -> dict[str, str]:
    """fingerprint -> issue URL for every codemender issue (open and closed)."""
    found: dict[str, str] = {}
    page = 1
    while True:
      items = self._req("GET", "/issues", params={"labels": LABEL, "state": "all",
                                                  "per_page": 100, "page": page}) or []
      for it in items:
        for fp in _MARKER_RE.findall(it.get("body") or ""):
          found.setdefault(fp, it.get("html_url", ""))
      if len(items) < 100:
        return found
      page += 1

  def create(self, title: str, body: str, labels: list[str]) -> str:
    return self._req("POST", "/issues", json={"title": title, "body": body,
                                              "labels": labels}).get("html_url", "")


# ----------------------------------------------------------------- publish
def fingerprint_of(f: dict[str, Any]) -> str:
  """Stable identity of a finding across scans."""
  fp = (f.get("fingerprint") or "").strip()
  if fp:
    return re.sub(r"[^A-Za-z0-9._:/=-]", "_", fp)
  return f"{f.get('rule_id', '')}:{f.get('file_path', '')}:{f.get('start_line', '')}".replace(" ", "_")


def issue_title(f: dict[str, Any]) -> str:
  loc = f.get("file_path") or "unknown file"
  if f.get("start_line"):
    loc += f":{f['start_line']}"
  rule = f.get("rule_id") or "finding"
  return f"[CodeMender] {(f.get('severity') or 'UNKNOWN').upper()}: {rule} in {loc}"[:250]


def issue_body(f: dict[str, Any], scanned_repo: str, server_url: str = "https://github.com") -> str:
  path, line, sha = f.get("file_path") or "", f.get("start_line"), f.get("commit_sha") or ""
  loc = f"`{path}{':' + str(line) if line else ''}`"
  if path and sha:
    anchor = f"#L{line}" + (f"-L{f['end_line']}" if f.get("end_line") and f.get("end_line") != line else "") if line else ""
    loc = f"[{loc}]({server_url}/{scanned_repo}/blob/{sha}/{path}{anchor})"
  msg = (f.get("message") or "").strip() or "_No description._"
  return "\n".join([
      f"CodeMender found a potential security issue in **{scanned_repo}**.",
      "",
      "| | |",
      "|---|---|",
      f"| **Rule** | `{f.get('rule_id') or '-'}` |",
      f"| **Severity** | {(f.get('severity') or 'UNKNOWN').upper()} |",
      f"| **Location** | {loc} |",
      f"| **Commit** | `{sha[:12] or '-'}` |",
      f"| **Finding ID** | `{f.get('finding_id', '')}` |",
      f"| **Scan job** | `{f.get('scan_job_id', '')}` |",
      "",
      "### Description",
      "",
      msg,
      "",
      "### Next steps",
      "",
      "Ask your MCP client (e.g. Claude Code connected to the CodeMender server):",
      f"- *\"Validate CodeMender finding `{f.get('finding_id', '')}`\"* (`cm_validate`) — exploitability check",
      f"- *\"Fix CodeMender finding `{f.get('finding_id', '')}`\"* (`cm_fix`) — opens a pull request with a patch",
      "",
      f"<!-- codemender:fingerprint={fingerprint_of(f)} -->",
  ])


@dataclass
class PublishResult:
  created: list[str] = field(default_factory=list)
  skipped: list[str] = field(default_factory=list)
  filtered: int = 0


def publish(findings: list[dict[str, Any]], issues: Any, scanned_repo: str,
            min_severity: str = "NOTE", max_issues: int = 50, dry_run: bool = False,
            log: Callable[[str], None] = print) -> PublishResult:
  out = PublishResult()
  floor = SEVERITY_RANK.get(min_severity.upper(), 0)
  selected = [f for f in findings
              if SEVERITY_RANK.get((f.get("severity") or "").upper(), 0) >= floor]
  out.filtered = len(findings) - len(selected)
  selected.sort(key=lambda f: (-SEVERITY_RANK.get((f.get("severity") or "").upper(), 0),
                               f.get("file_path") or "", f.get("start_line") or 0))
  existing = issues.existing_fingerprints()
  if not dry_run and selected:
    issues.ensure_label()
  seen: set[str] = set()
  for f in selected:
    fp = fingerprint_of(f)
    if fp in existing or fp in seen:
      out.skipped.append(existing.get(fp, fp))
      continue
    seen.add(fp)
    if len(out.created) >= max_issues:
      log(f"Reached max_issues={max_issues}; remaining findings stay in BigQuery only.")
      break
    sev = (f.get("severity") or "unknown").lower()
    title = issue_title(f)
    if dry_run:
      log(f"[dry-run] would create: {title}")
      out.created.append(title)
      continue
    url = issues.create(title, issue_body(f, scanned_repo), [LABEL, f"severity:{sev}"])
    log(f"Created {url}  {title}")
    out.created.append(url)
  return out


def write_summary(path: str, repo: str, job: dict[str, Any], findings: list[dict[str, Any]],
                  res: PublishResult, issues_repo: str, dry_run: bool) -> None:
  by_sev: dict[str, int] = {}
  for f in findings:
    k = (f.get("severity") or "UNKNOWN").upper()
    by_sev[k] = by_sev.get(k, 0) + 1
  lines = [
      f"## CodeMender scan: `{repo}`",
      "",
      f"- Scan job: `{job.get('job_id', '')}` — **{job.get('status', '')}**",
      f"- Commit: `{(job.get('commit_sha') or '')[:12]}`",
      f"- Findings stored in BigQuery: **{len(findings)}** "
      + (" ".join(f"{k}={v}" for k, v in sorted(by_sev.items())) if by_sev else ""),
      f"- Issues {'that would be ' if dry_run else ''}created in `{issues_repo}`: **{len(res.created)}**",
      f"- Already tracked (skipped): {len(res.skipped)}; below severity threshold: {res.filtered}",
  ]
  if res.created and not dry_run:
    lines += ["", *[f"- {u}" for u in res.created[:100]]]
  with open(path, "a", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")


# -------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
  p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
  p.add_argument("--repo", required=True, help="Repository to clone and scan (owner/repo or URL)")
  p.add_argument("--ref", default="", help="Branch, tag or commit (default: default branch)")
  p.add_argument("--issues-repo", default="", help="Repository to file issues in (default: --repo)")
  p.add_argument("--scan-job-id", default="", help="Publish an existing scan instead of starting one")
  p.add_argument("--min-severity", default="NOTE", choices=["ERROR", "WARNING", "NOTE", "NONE"],
                 type=str.upper)
  p.add_argument("--max-issues", type=int, default=50)
  p.add_argument("--timeout-minutes", type=int, default=330)
  p.add_argument("--no-issues", action="store_true", help="Scan + BigQuery only")
  p.add_argument("--dry-run", action="store_true", help="Print issues instead of creating them")
  a = p.parse_args(argv)

  url = os.environ.get("CODEMENDER_MCP_URL", "").strip()
  if not url:
    print("CODEMENDER_MCP_URL is not set.", file=sys.stderr)
    return 2
  mcp = McpClient(url, os.environ.get("CODEMENDER_MCP_TOKEN", "").strip())
  issues_repo = (a.issues_repo or a.repo).strip()
  issues_repo = re.sub(r"^(https?://[^/]+/|git@[^:]+:)", "", issues_repo).removesuffix(".git").strip("/")

  try:
    if a.scan_job_id:
      job = wait_for_job(mcp, a.scan_job_id.strip(), a.timeout_minutes * 60)
    else:
      job = run_scan(mcp, a.repo, a.ref, a.timeout_minutes * 60)
    if job.get("status") != "SUCCEEDED":
      raise ScanError(f"Scan {job.get('job_id')} {job.get('status')}: {job.get('error', '')}")
    findings = mcp.call("cm_list_findings", {"scan_job_id": job["job_id"],
                                             "limit": 1000}).get("findings", [])
    print(f"Scan {job['job_id']} stored {len(findings)} finding(s) in BigQuery.")

    res = PublishResult()
    if not a.no_issues and findings:
      token = os.environ.get("GITHUB_TOKEN", "").strip()
      if not token:
        raise ScanError("GITHUB_TOKEN is required to create issues (or pass --no-issues).")
      gh = GitHubIssues(issues_repo, token, os.environ.get("GITHUB_API_URL", "https://api.github.com"))
      res = publish(findings, gh, job.get("repo") or a.repo, a.min_severity, a.max_issues, a.dry_run)
      print(f"Issues created: {len(res.created)}, already tracked: {len(res.skipped)}, "
            f"below threshold: {res.filtered}")
  except ScanError as e:
    print(f"::error::{e}")
    return 1

  if os.environ.get("GITHUB_STEP_SUMMARY"):
    write_summary(os.environ["GITHUB_STEP_SUMMARY"], a.repo, job, findings, res, issues_repo, a.dry_run)
  if os.environ.get("GITHUB_OUTPUT"):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
      fh.write(f"scan_job_id={job['job_id']}\nfinding_count={len(findings)}\n"
               f"issues_created={len(res.created)}\n")
  return 0


if __name__ == "__main__":
  sys.exit(main())
