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

"""Cloud Run Job transient storage and lifecycle bootstrapper for CodeMender."""

import atexit
from datetime import datetime, timezone
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile

# ==============================================================================
# 1. Standardize Local Workspace & Ephemeral Scratch Directory (Agreement #2)
# ==============================================================================
os.environ.setdefault("EPHEMERAL_SCRATCH_DIR", "/workspace")
WORKSPACE_DIR = os.environ.get(
    "WORKSPACE_DIR", os.environ["EPHEMERAL_SCRATCH_DIR"]
)
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.join(WORKSPACE_DIR, "output"))
REPO_DIR = os.environ.get("REPO_DIR", os.path.join(WORKSPACE_DIR, "repo"))
INPUTS_DIR = os.environ.get("INPUTS_DIR", os.path.join(WORKSPACE_DIR, "inputs"))
LOG_FILE = os.path.join(OUTPUT_DIR, "runner.log")
RECEIPT_FILE = os.path.join(OUTPUT_DIR, "receipt.json")

START_TIME = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
EXIT_CODE = 1
RECEIPT_WRITTEN = False
GCS_SYNC_SUCCEEDED = False
FINALIZED = False
ACTIVE_PROC: subprocess.Popen | None = None
CMD = ""
OUTPUT_URI = ""


def _ensure_workspace_dirs() -> None:
  for d in (WORKSPACE_DIR, OUTPUT_DIR, REPO_DIR, INPUTS_DIR):
    os.makedirs(d, exist_ok=True)
  if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, "a", encoding="utf-8"):
      pass


def log(msg: str) -> None:
  ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
  line = f"[{ts}] [CodeMender-Bootstrapper] {msg}"
  print(line, flush=True)
  try:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
      f.write(line + "\n")
  except OSError:
    pass


# ==============================================================================
# 2. Resilient 4-Tier Transit Drivers (SDK -> gcloud -> gsutil -> file://)
# ==============================================================================
def transit_download(uri: str, dest: str) -> bool:
  """Downloads an object from gs://, file://, or local path into dest."""
  log(f"Downloading from transit storage: {uri} -> {dest}")
  os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)

  if uri.startswith("gs://"):
    gs_path = uri[len("gs://") :]
    bucket_name, _, blob_path = gs_path.partition("/")

    # Tier 1: Python google-cloud-storage SDK
    if bucket_name and blob_path:
      try:
        from google.cloud import storage  # pylint: disable=g-import-not-at-top

        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_path)
        blob.download_to_filename(dest)
        return True
      except Exception as exc:  # pylint: disable=broad-except
        log(f"DEBUG: google-cloud-storage SDK download fallback ({exc})")

    # Tier 2: gcloud storage CLI
    if shutil.which("gcloud"):
      res = subprocess.run(
          ["gcloud", "storage", "cp", uri, dest, "--quiet"],
          stdout=subprocess.PIPE,
          stderr=subprocess.STDOUT,
          text=True,
          check=False,
      )
      if res.returncode == 0:
        return True

    # Tier 3: gsutil CLI
    if shutil.which("gsutil"):
      res = subprocess.run(
          ["gsutil", "-q", "cp", uri, dest],
          stdout=subprocess.PIPE,
          stderr=subprocess.STDOUT,
          text=True,
          check=False,
      )
      if res.returncode == 0:
        return True

    log(f"ERROR: Failed to download {uri} via SDK, gcloud, or gsutil.")
    return False

  # Tier 4: file:// URI or direct local path
  local_src = uri[len("file://") :] if uri.startswith("file://") else uri
  if os.path.isfile(local_src):
    shutil.copy2(local_src, dest)
    return True

  log(f"ERROR: Unsupported or missing transit URI: {uri}")
  return False


def transit_upload_dir(src_dir: str, uri_prefix: str) -> bool:
  """Uploads all files in src_dir to uri_prefix (gs://, file://, or path)."""
  uri_prefix = uri_prefix.rstrip("/")
  log(f"Syncing output folder {src_dir}/ -> {uri_prefix}/")

  if not os.path.isdir(src_dir) or not os.listdir(src_dir):
    log(f"WARNING: Output directory {src_dir} is empty; nothing to upload.")
    return True

  if uri_prefix.startswith("gs://"):
    gs_path = uri_prefix[len("gs://") :]
    bucket_name, _, prefix_path = gs_path.partition("/")
    prefix_path = prefix_path.strip("/")

    # Tier 1: Python google-cloud-storage SDK
    if bucket_name:
      try:
        from google.cloud import storage  # pylint: disable=g-import-not-at-top

        client = storage.Client()
        bucket = client.bucket(bucket_name)
        for root, _, files in os.walk(src_dir):
          for fname in files:
            local_file = os.path.join(root, fname)
            rel_path = os.path.relpath(local_file, src_dir).replace(os.sep, "/")
            blob_name = f"{prefix_path}/{rel_path}" if prefix_path else rel_path
            blob = bucket.blob(blob_name)
            blob.upload_from_filename(local_file)
        return True
      except Exception as exc:  # pylint: disable=broad-except
        log(f"DEBUG: google-cloud-storage SDK upload fallback ({exc})")

    # Tier 2: gcloud storage CLI
    if shutil.which("gcloud"):
      res = subprocess.run(
          [
              "gcloud",
              "storage",
              "cp",
              "-r",
              *[os.path.join(src_dir, p) for p in os.listdir(src_dir)],
              f"{uri_prefix}/",
              "--quiet",
          ],
          stdout=subprocess.PIPE,
          stderr=subprocess.STDOUT,
          text=True,
          check=False,
      )
      if res.returncode == 0:
        return True

    # Tier 3: gsutil CLI
    if shutil.which("gsutil"):
      res = subprocess.run(
          [
              "gsutil",
              "-m",
              "-q",
              "cp",
              "-r",
              *[os.path.join(src_dir, p) for p in os.listdir(src_dir)],
              f"{uri_prefix}/",
          ],
          stdout=subprocess.PIPE,
          stderr=subprocess.STDOUT,
          text=True,
          check=False,
      )
      if res.returncode == 0:
        return True

    log(f"ERROR: Failed to upload {src_dir} to {uri_prefix}")
    return False

  # Tier 4: file:// URI or direct local path
  local_dest = (
      uri_prefix[len("file://") :]
      if uri_prefix.startswith("file://")
      else uri_prefix
  )
  try:
    os.makedirs(local_dest, exist_ok=True)
    for item in os.listdir(src_dir):
      s_item = os.path.join(src_dir, item)
      d_item = os.path.join(local_dest, item)
      if os.path.isdir(s_item):
        shutil.copytree(s_item, d_item, dirs_exist_ok=True)
      else:
        shutil.copy2(s_item, d_item)
    return True
  except OSError as exc:
    log(f"ERROR: Local transit copy failed: {exc}")
    return False


# ==============================================================================
# 3. Receipt Emission & Preemption / Exit Cleanup (Agreement #3 & #4)
# ==============================================================================
def write_receipt(
    status: str, exit_code: int, cmd_run: str, error_msg: str = ""
) -> None:
  """Emits machine-readable receipt.json to OUTPUT_DIR."""
  global RECEIPT_WRITTEN
  end_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
  prefix = OUTPUT_URI.rstrip("/") if OUTPUT_URI else OUTPUT_DIR.rstrip("/")

  artifacts: dict[str, str] = {"log": f"{prefix}/runner.log"}
  finding_count = 0

  sarif_file = os.path.join(OUTPUT_DIR, "report.sarif")
  verified_sarif_file = os.path.join(OUTPUT_DIR, "finding.sarif")
  findings_file = os.path.join(OUTPUT_DIR, "findings.json")

  if os.path.isfile(sarif_file):
    artifacts["sarif"] = f"{prefix}/report.sarif"
  elif os.path.isfile(verified_sarif_file):
    artifacts["sarif"] = f"{prefix}/finding.sarif"
  if os.path.isfile(findings_file):
    artifacts["findings"] = f"{prefix}/findings.json"

  count_source = (
      sarif_file
      if os.path.isfile(sarif_file)
      else (
          verified_sarif_file
          if os.path.isfile(verified_sarif_file)
          else (findings_file if os.path.isfile(findings_file) else "")
      )
  )
  if count_source:
    try:
      with open(count_source, "r", encoding="utf-8") as f:
        data = json.load(f)
      runs = data.get("runs", [])
      if runs and isinstance(runs, list):
        finding_count = len(runs[0].get("results", []))
    except Exception:  # pylint: disable=broad-except
      finding_count = 0

  if os.path.isfile(os.path.join(OUTPUT_DIR, "report.md")):
    artifacts["report"] = f"{prefix}/report.md"

  if os.path.isfile(os.path.join(OUTPUT_DIR, "fix.patch")):
    artifacts["patch"] = f"{prefix}/fix.patch"
  elif os.path.isfile(os.path.join(OUTPUT_DIR, "finding.diff")):
    artifacts["patch"] = f"{prefix}/finding.diff"

  receipt = {
      "status": status,
      "exit_code": int(exit_code),
      "command": cmd_run or "unknown",
      "started_at": START_TIME,
      "completed_at": end_time,
      "finding_count": finding_count,
      "artifacts": artifacts,
      "error": error_msg if error_msg else None,
  }
  os.makedirs(OUTPUT_DIR, exist_ok=True)
  with open(RECEIPT_FILE, "w", encoding="utf-8") as f:
    json.dump(receipt, f, indent=2)
  RECEIPT_WRITTEN = True


def _terminate_children() -> None:
  """Terminates any active child process group and direct child processes."""
  global ACTIVE_PROC
  if ACTIVE_PROC is not None and ACTIVE_PROC.poll() is None:
    try:
      os.killpg(ACTIVE_PROC.pid, signal.SIGTERM)
    except OSError:
      try:
        ACTIVE_PROC.terminate()
      except OSError:
        pass
    try:
      ACTIVE_PROC.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
      try:
        os.killpg(ACTIVE_PROC.pid, signal.SIGKILL)
      except OSError:
        try:
          ACTIVE_PROC.kill()
        except OSError:
          pass
  if shutil.which("pkill"):
    subprocess.run(
        ["pkill", "-TERM", "-P", str(os.getpid())],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def cleanup_and_finalize() -> None:
  """Ensures child termination, receipt emission, transit sync, or stderr spill."""
  global FINALIZED, GCS_SYNC_SUCCEEDED
  if FINALIZED:
    return
  FINALIZED = True

  _terminate_children()

  if not RECEIPT_WRITTEN:
    status = "SUCCESS" if EXIT_CODE == 0 else "FAILURE"
    err = (
        f"Bootstrapper terminated with exit code {EXIT_CODE}"
        if EXIT_CODE != 0
        else ""
    )
    write_receipt(status, EXIT_CODE, CMD or "unknown", err)

  if OUTPUT_URI:
    if transit_upload_dir(OUTPUT_DIR, OUTPUT_URI):
      GCS_SYNC_SUCCEEDED = True
      log("Transit output synchronization completed.")
    else:
      log(f"WARNING: Transit output synchronization failed for {OUTPUT_URI}")

  if EXIT_CODE != 0 and not GCS_SYNC_SUCCEEDED:
    sys.stderr.write(
        "=== [FALLBACK LOG SPILL: Container exited before GCS transit sync] ===\n"
    )
    if os.path.isfile(LOG_FILE):
      try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
          sys.stderr.write(f.read())
      except OSError:
        pass
    sys.stderr.write("=== [END FALLBACK LOG SPILL] ===\n")
    sys.stderr.flush()


def _handle_sigterm(signum, frame) -> None:  # pylint: disable=unused-argument
  global EXIT_CODE
  EXIT_CODE = 143
  log("ERROR: Preemption or SIGTERM (143) received! Terminating child processes...")
  cleanup_and_finalize()
  sys.exit(143)


def _handle_sigint(signum, frame) -> None:  # pylint: disable=unused-argument
  global EXIT_CODE
  EXIT_CODE = 130
  log("ERROR: SIGINT (130) received! Terminating child processes...")
  cleanup_and_finalize()
  sys.exit(130)


# ==============================================================================
# 4. Standardized Tarball Unpacking & Flattening
# ==============================================================================
def unpack_tar_gz_preserving_permissions(
    archive_path: str, dest_dir: str
) -> None:
  """Unpacks a code.tar.gz archive into dest_dir while preserving POSIX modes."""
  os.makedirs(dest_dir, exist_ok=True)
  abs_dest = os.path.realpath(dest_dir)
  with tarfile.open(archive_path, "r:*") as tar:
    members = tar.getmembers()
    for member in members:
      if os.path.isabs(member.name):
        raise ValueError(f"Unsafe absolute tar member path: {member.name}")
      target_path = os.path.realpath(os.path.join(abs_dest, member.name))
      if (
          os.path.commonpath([abs_dest, target_path]) != abs_dest
          or ".." in member.name.split("/")
          or ".." in member.name.split(os.sep)
      ):
        raise ValueError(f"Unsafe path traversal in tar member: {member.name}")
      if member.issym() or member.islnk():
        link_base = (
            os.path.dirname(target_path) if member.issym() else abs_dest
        )
        link_target = os.path.realpath(os.path.join(link_base, member.linkname))
        if (
            os.path.isabs(member.linkname)
            or os.path.commonpath([abs_dest, link_target]) != abs_dest
        ):
          raise ValueError(
              f"Unsafe link target in tar member {member.name}: {member.linkname}"
          )

    try:
      tar.extractall(path=dest_dir, filter="data")
    except TypeError:
      tar.extractall(path=dest_dir)
    for member in members:
      if member.isfile() or member.isdir():
        extracted_path = os.path.join(dest_dir, member.name)
        if os.path.exists(extracted_path) and not os.path.islink(extracted_path):
          if member.mode:
            try:
              os.chmod(extracted_path, member.mode & 0o777)
            except OSError:
              pass

    # Ensure all extracted files and directories are owned by the current process
    # and have user read/write permissions. When running as root in containers
    # (such as Cloud Run Jobs), tar.extractall preserves original archive
    # ownership (e.g. GitHub Actions runner uid:gid 1001:1001). Sandboxed
    # execution engines (like CodeMender's exebox) drop all Linux capabilities
    # (CAP_DAC_OVERRIDE and CAP_FOWNER), causing tool file modifications
    # (write_to_file, search_and_replace) to fail with EACCES (Permission denied).
    current_uid = os.getuid()
    current_gid = os.getgid()
    try:
      os.chown(dest_dir, current_uid, current_gid)
    except OSError:
      pass

    for root_dir, dirs, files in os.walk(dest_dir):
      for name in dirs + files:
        target = os.path.join(root_dir, name)
        if not os.path.islink(target):
          try:
            os.chown(target, current_uid, current_gid)
          except OSError:
            pass
          try:
            mode = os.stat(target).st_mode
            if os.path.isdir(target):
              os.chmod(target, mode | 0o700)
            else:
              os.chmod(target, mode | 0o600)
          except OSError:
            pass


def flatten_repo_if_nested(repo_dir: str) -> None:
  """Flattens single top-level wrapper folder so .git is at repo_dir/.git."""
  if os.path.exists(os.path.join(repo_dir, ".git")):
    return
  entries = os.listdir(repo_dir)
  if len(entries) == 1:
    single_subdir = os.path.join(repo_dir, entries[0])
    if os.path.isdir(single_subdir):
      log(f"Flattening nested repository directory {single_subdir} into {repo_dir}")
      for child in os.listdir(single_subdir):
        shutil.move(
            os.path.join(single_subdir, child),
            os.path.join(repo_dir, child),
        )
      os.rmdir(single_subdir)


def stage_workspace(payload_uri: str, cmd_name: str, finding_uri: str) -> int:
  """Downloads and extracts code.tar.gz and finding.sarif into /workspace."""
  if not payload_uri:
    log(
        f"ERROR: Missing PAYLOAD_URI (or --payload-uri) for command '{cmd_name}'."
    )
    write_receipt(
        "FAILURE", 2, cmd_name, "Missing PAYLOAD_URI for source staging."
    )
    return 2

  os.makedirs(INPUTS_DIR, exist_ok=True)
  shutil.rmtree(REPO_DIR, ignore_errors=True)
  os.makedirs(REPO_DIR, exist_ok=True)

  archive_file = os.path.join(INPUTS_DIR, "code.tar.gz")
  if not transit_download(payload_uri, archive_file):
    write_receipt(
        "FAILURE",
        2,
        cmd_name,
        f"Failed to download source payload from {payload_uri}",
    )
    return 2

  log(f"Extracting {archive_file} to {REPO_DIR} via Python tarfile...")
  try:
    unpack_tar_gz_preserving_permissions(archive_file, REPO_DIR)
  except Exception as exc:  # pylint: disable=broad-except
    log(f"ERROR: Failed to extract {archive_file}: {exc}")
    write_receipt(
        "FAILURE", 2, cmd_name, f"Failed to extract {archive_file}: {exc}"
    )
    return 2

  flatten_repo_if_nested(REPO_DIR)

  if shutil.which("git"):
    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", REPO_DIR],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

  if cmd_name in ("fix", "verify"):
    if not finding_uri:
      log(
          f"ERROR: Command '{cmd_name}' requires FINDING_URI (or --finding-uri)."
      )
      write_receipt(
          "FAILURE", 2, cmd_name, f"Missing FINDING_URI for '{cmd_name}'."
      )
      return 2
    finding_dest = os.path.join(INPUTS_DIR, "finding.sarif")
    if not transit_download(finding_uri, finding_dest):
      write_receipt(
          "FAILURE",
          2,
          cmd_name,
          f"Failed to download finding from {finding_uri}",
      )
      return 2

  return 0


def run_logged(cmd_args: list[str]) -> int:
  """Executes cmd_args in a child process group, teeing output to LOG_FILE."""
  global ACTIVE_PROC
  log(f"Executing cm-runner {CMD}: {' '.join(cmd_args)}")
  ACTIVE_PROC = subprocess.Popen(
      cmd_args,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      text=True,
      start_new_session=True,
  )
  assert ACTIVE_PROC.stdout is not None
  with open(LOG_FILE, "a", encoding="utf-8") as log_f:
    for line in ACTIVE_PROC.stdout:
      sys.stdout.write(line)
      sys.stdout.flush()
      log_f.write(line)
      log_f.flush()
  rc = ACTIVE_PROC.wait()
  ACTIVE_PROC = None
  return rc


# ==============================================================================
# 5. CLI Argument, Environment & Manifest Resolution (Agreement #1)
# ==============================================================================
def main(argv: list[str] | None = None) -> int:
  global EXIT_CODE, CMD, OUTPUT_URI
  EXIT_CODE = 1
  _ensure_workspace_dirs()
  signal.signal(signal.SIGTERM, _handle_sigterm)
  signal.signal(signal.SIGINT, _handle_sigint)
  atexit.register(cleanup_and_finalize)

  log("Initializing CodeMender Cloud Run Job Bootstrapper...")

  args = list(sys.argv[1:] if argv is None else argv)
  if args and args[0] == "cm-runner":
    args.pop(0)

  cli_cmd = ""
  cli_manifest_uri = ""
  cli_output_uri = ""
  cli_finding_uri = ""
  cli_payload_uri = ""
  cli_diff_base = ""
  cli_diff_workers = ""
  cli_log_level = ""
  passthrough_args: list[str] = []

  value_flags = (
      "--manifest",
      "--manifest-uri",
      "--manifest-gcs-uri",
      "--output-uri",
      "--output-prefix",
      "--out-prefix",
      "--output-gcs-prefix",
      "--finding",
      "--finding-uri",
      "--finding-gcs-uri",
      "--payload",
      "--payload-uri",
      "--payload-gcs-uri",
      "--base",
      "--base-ref",
      "--repo",
      "--out",
      "--output-dir",
      "--diff-workers",
      "--log-level",
  )

  i = 0
  while i < len(args):
    arg = args[i]
    flag, has_eq, eq_val = arg.partition("=")
    if arg in ("version", "--version", "-v") and not cli_cmd:
      cli_cmd = "version"
      i += 1
    elif (
        arg in ("init", "find-diff", "find", "find-full", "fix", "verify")
        and not cli_cmd
    ):
      cli_cmd = "find-diff" if arg == "find" else arg
      i += 1
    elif flag in value_flags:
      if has_eq:
        val = eq_val
        i += 1
      elif i + 1 < len(args):
        val = args[i + 1]
        i += 2
      else:
        log(f"ERROR: Flag '{flag}' requires an argument.")
        EXIT_CODE = 2
        write_receipt(
            "FAILURE",
            2,
            cli_cmd or "unknown",
            f"Flag '{flag}' requires an argument.",
        )
        return EXIT_CODE

      if flag in ("--manifest", "--manifest-uri", "--manifest-gcs-uri"):
        cli_manifest_uri = val
      elif flag in (
          "--output-uri",
          "--output-prefix",
          "--out-prefix",
          "--output-gcs-prefix",
      ):
        cli_output_uri = val
      elif flag in ("--finding", "--finding-uri", "--finding-gcs-uri"):
        cli_finding_uri = val
      elif flag in ("--payload", "--payload-uri", "--payload-gcs-uri"):
        cli_payload_uri = val
      elif flag in ("--base", "--base-ref"):
        cli_diff_base = val
      elif flag == "--diff-workers":
        cli_diff_workers = val
      elif flag == "--log-level":
        cli_log_level = val
    else:
      passthrough_args.append(arg)
      i += 1

  manifest_uri = (
      cli_manifest_uri
      or os.environ.get("MANIFEST_URI", "")
      or os.environ.get("MANIFEST_GCS_URI", "")
  )

  manifest_data: dict = {}
  if manifest_uri:
    manifest_path = os.path.join(INPUTS_DIR, "manifest.json")
    log(f"MANIFEST_URI detected ({manifest_uri}). Staging manifest...")
    if not transit_download(manifest_uri, manifest_path):
      EXIT_CODE = 2
      write_receipt(
          "FAILURE", 2, "manifest-sync", f"Failed to download {manifest_uri}"
      )
      return EXIT_CODE
    try:
      with open(manifest_path, "r", encoding="utf-8") as f:
        manifest_data = json.load(f)
    except Exception as exc:  # pylint: disable=broad-except
      EXIT_CODE = 2
      write_receipt(
          "FAILURE", 2, "manifest-parse", f"Invalid JSON in {manifest_uri}: {exc}"
      )
      return EXIT_CODE

  CMD = (
      cli_cmd
      or os.environ.get("CM_COMMAND", "")
      or manifest_data.get("command", "")
      or "find-diff"
  )
  if CMD == "find":
    CMD = "find-diff"

  PAYLOAD_URI = (
      cli_payload_uri
      or os.environ.get("PAYLOAD_URI", "")
      or os.environ.get("PAYLOAD_GCS_URI", "")
      or manifest_data.get("payload_uri", "")
      or manifest_data.get("payload_gcs_uri", "")
  )
  OUTPUT_URI = (
      cli_output_uri
      or os.environ.get("OUTPUT_URI", "")
      or os.environ.get("OUTPUT_GCS_PREFIX", "")
      or os.environ.get("TASK_OUTPUT_PREFIX", "")
      or os.environ.get("OUTPUT_PREFIX", "")
      or manifest_data.get("output_uri", "")
      or manifest_data.get("output_prefix", "")
      or manifest_data.get("output_gcs_prefix", "")
  )
  FINDING_URI = (
      cli_finding_uri
      or os.environ.get("FINDING_URI", "")
      or os.environ.get("FINDING_GCS_URI", "")
      or manifest_data.get("finding_uri", "")
      or manifest_data.get("finding_gcs_uri", "")
  )
  DIFF_BASE = (
      cli_diff_base
      or os.environ.get("DIFF_BASE", "")
      or manifest_data.get("diff_base", "")
      or "HEAD~1"
  )
  DIFF_WORKERS = (
      cli_diff_workers
      or os.environ.get("DIFF_WORKERS", "")
      or str(manifest_data.get("diff_workers", "") or "4")
  )
  LOG_LEVEL = (
      cli_log_level
      or os.environ.get("LOG_LEVEL", "")
      or manifest_data.get("log_level", "")
      or "info"
  )

  log(
      f"Configuration resolved: CMD={CMD}, PAYLOAD_URI={PAYLOAD_URI or '<none>'}, "
      f"OUTPUT_URI={OUTPUT_URI or '<none>'}, FINDING_URI={FINDING_URI or '<none>'}"
  )

  global_opts = ["--log-level", LOG_LEVEL]

  if CMD == "version":
    rc = run_logged(["cm-runner", *global_opts, "version", *passthrough_args])
  elif CMD == "init":
    stage_rc = stage_workspace(PAYLOAD_URI, CMD, FINDING_URI)
    if stage_rc != 0:
      EXIT_CODE = stage_rc
      return EXIT_CODE
    rc = run_logged(
        ["cm-runner", *global_opts, "init", "--repo", REPO_DIR, *passthrough_args]
    )
  elif CMD == "find-diff":
    stage_rc = stage_workspace(PAYLOAD_URI, CMD, FINDING_URI)
    if stage_rc != 0:
      EXIT_CODE = stage_rc
      return EXIT_CODE
    rc = run_logged([
        "cm-runner",
        *global_opts,
        "find-diff",
        "--repo",
        REPO_DIR,
        "--base",
        DIFF_BASE,
        "--out",
        OUTPUT_DIR,
        "--diff-workers",
        DIFF_WORKERS,
        *passthrough_args,
    ])
    # Agreement #3: Ensure dual finding artifacts (report.sarif and findings.json)
    sarif_path = os.path.join(OUTPUT_DIR, "report.sarif")
    findings_path = os.path.join(OUTPUT_DIR, "findings.json")
    if os.path.isfile(sarif_path) and not os.path.isfile(findings_path):
      shutil.copy2(sarif_path, findings_path)
    elif os.path.isfile(findings_path) and not os.path.isfile(sarif_path):
      shutil.copy2(findings_path, sarif_path)
  elif CMD == "find-full":
    # Full-repository scan (no diff base); used by the MCP server's cm_find.
    stage_rc = stage_workspace(PAYLOAD_URI, CMD, FINDING_URI)
    if stage_rc != 0:
      EXIT_CODE = stage_rc
      return EXIT_CODE
    rc = run_logged([
        "cm-runner",
        *global_opts,
        "find-full",
        "--repo",
        REPO_DIR,
        "--out",
        OUTPUT_DIR,
        *passthrough_args,
    ])
  elif CMD == "fix":
    stage_rc = stage_workspace(PAYLOAD_URI, CMD, FINDING_URI)
    if stage_rc != 0:
      EXIT_CODE = stage_rc
      return EXIT_CODE
    rc = run_logged([
        "cm-runner",
        *global_opts,
        "fix",
        "--repo",
        REPO_DIR,
        "--finding",
        os.path.join(INPUTS_DIR, "finding.sarif"),
        "--out",
        OUTPUT_DIR,
        *passthrough_args,
    ])
    finding_diff = os.path.join(OUTPUT_DIR, "finding.diff")
    fix_patch = os.path.join(OUTPUT_DIR, "fix.patch")
    if os.path.isfile(finding_diff) and not os.path.isfile(fix_patch):
      shutil.copy2(finding_diff, fix_patch)
  elif CMD == "verify":
    stage_rc = stage_workspace(PAYLOAD_URI, CMD, FINDING_URI)
    if stage_rc != 0:
      EXIT_CODE = stage_rc
      return EXIT_CODE
    rc = run_logged([
        "cm-runner",
        *global_opts,
        "verify",
        "--repo",
        REPO_DIR,
        "--finding",
        os.path.join(INPUTS_DIR, "finding.sarif"),
        "--out",
        OUTPUT_DIR,
        *passthrough_args,
    ])
  else:
    log(f"ERROR: Unsupported command '{CMD}'.")
    EXIT_CODE = 2
    write_receipt("FAILURE", 2, CMD, f"Unsupported command: {CMD}")
    return EXIT_CODE

  EXIT_CODE = rc
  if EXIT_CODE == 0:
    log(f"Command '{CMD}' completed successfully (exit_code=0).")
    write_receipt("SUCCESS", 0, CMD, "")
  else:
    log(f"ERROR: Command '{CMD}' failed with exit_code={EXIT_CODE}.")
    write_receipt(
        "FAILURE",
        EXIT_CODE,
        CMD,
        f"cm-runner {CMD} failed with exit code {EXIT_CODE}",
    )

  return EXIT_CODE


if __name__ == "__main__":
  sys.exit(main())
