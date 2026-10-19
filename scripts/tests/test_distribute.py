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

"""Integration tests for scripts/distribute.sh (SPEC-DIST)."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_TESTS_DIR)
_MONOREPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
_DISTRIBUTE_SCRIPT = os.path.join(_SCRIPTS_DIR, "distribute.sh")


def _run_git(repo_dir: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
  return subprocess.run(
      ["git", "-C", repo_dir, *args],
      capture_output=True,
      text=True,
      check=check,
  )


def _init_git_repo(path: str, initial_commit: bool = False) -> str:
  os.makedirs(path, exist_ok=True)
  _run_git(path, "init", "-q")
  _run_git(path, "config", "user.name", "Test User")
  _run_git(path, "config", "user.email", "test@example.com")
  if initial_commit:
    readme_path = os.path.join(path, "INITIAL.txt")
    with open(readme_path, "w", encoding="utf-8") as f:
      f.write("initial target commit\n")
    _run_git(path, "add", "INITIAL.txt")
    _run_git(path, "commit", "-q", "-m", "Initial target commit")
    return _run_git(path, "rev-parse", "HEAD").stdout.strip()
  return ""


class TestDistributeScript(unittest.TestCase):
  """End-to-end integration tests for scripts/distribute.sh (SPEC-DIST)."""

  @classmethod
  def setUpClass(cls):
    super().setUpClass()
    os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
    cls._class_tmp = tempfile.TemporaryDirectory()
    cls.base_source_repo = os.path.join(cls._class_tmp.name, "source_repo")
    shutil.copytree(
        _MONOREPO_ROOT,
        cls.base_source_repo,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".git", ".worktrees", "__pycache__", "*.pyc", ".pytest_cache"
        ),
    )
    _init_git_repo(cls.base_source_repo, initial_commit=False)
    _run_git(cls.base_source_repo, "add", "-A")
    _run_git(
        cls.base_source_repo,
        "commit",
        "-q",
        "-m",
        "Initial monorepo snapshot (C1)",
    )
    cls.c1_sha = _run_git(
        cls.base_source_repo, "rev-parse", "HEAD"
    ).stdout.strip()
    cls.script_path = os.path.join(
        cls.base_source_repo, "scripts", "distribute.sh"
    )

  @classmethod
  def tearDownClass(cls):
    cls._class_tmp.cleanup()
    super().tearDownClass()

  def setUp(self):
    super().setUp()
    self._tmp = tempfile.TemporaryDirectory()
    self.tmp_dir = self._tmp.name

  def tearDown(self):
    self._tmp.cleanup()
    super().tearDown()

  def _clone_source_repo(self, name: str = "src_clone") -> str:
    dest = os.path.join(self.tmp_dir, name)
    _run_git(self.tmp_dir, "clone", "-q", self.base_source_repo, dest)
    _run_git(dest, "config", "user.name", "Test User")
    _run_git(dest, "config", "user.email", "test@example.com")
    return dest

  def _run_distribute(
      self,
      *args: str,
      source_repo: str | None = None,
      script_path: str | None = None,
  ) -> subprocess.CompletedProcess:
    script = script_path or self.script_path
    cmd = [script]
    if source_repo is not None:
      cmd.extend(["--source-repo", source_repo])
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, check=False)

  # ---------------------------------------------------------------------------
  # Task 1: CLI Pre-Flight Validation, --dry-run, and Clean git archive
  # (SPEC-DIST/REQ-0001, REQ-0002)
  # ---------------------------------------------------------------------------

  def test_help_flag_prints_usage_and_exits_zero(self):
    """Verify -h and --help print usage instructions and exit 0 (REQ-0001)."""
    self.assertTrue(os.access(_DISTRIBUTE_SCRIPT, os.X_OK))
    for flag in ("-h", "--help"):
      with self.subTest(flag=flag):
        res = self._run_distribute(flag)
        self.assertEqual(res.returncode, 0, msg=res.stderr)
        self.assertIn("Usage: ./scripts/distribute.sh --target DIR", res.stdout)

  def test_rejects_missing_or_non_git_or_nested_subdir_target_with_exit_2(self):
    """Verify missing --target, non-Git dir, or nested subdir exits 2 (REQ-0001)."""
    # 1. Missing --target flag altogether
    res_no_flag = self._run_distribute()
    self.assertEqual(res_no_flag.returncode, 2)
    self.assertIn("--target", res_no_flag.stderr)

    # 2. Non-existent target directory
    missing_dir = os.path.join(self.tmp_dir, "does_not_exist")
    res_missing = self._run_distribute("--target", missing_dir)
    self.assertEqual(res_missing.returncode, 2)
    self.assertIn("does not exist", res_missing.stderr)

    # 3. Plain non-Git directory
    plain_dir = os.path.join(self.tmp_dir, "plain_dir")
    os.makedirs(plain_dir)
    res_plain = self._run_distribute("--target", plain_dir)
    self.assertEqual(res_plain.returncode, 2)
    self.assertIn(".git not found", res_plain.stderr)

    # 4. Plain subdirectory nested inside an ancestor Git repository
    ancestor_repo = os.path.join(self.tmp_dir, "ancestor_repo")
    _init_git_repo(ancestor_repo, initial_commit=True)
    nested_subdir = os.path.join(ancestor_repo, "nested", "subdir")
    os.makedirs(nested_subdir)
    res_nested = self._run_distribute("--target", nested_subdir)
    self.assertEqual(res_nested.returncode, 2)
    self.assertIn(".git not found", res_nested.stderr)

  def test_rejects_dirty_target_repository_with_exit_3_and_zero_modifications(
      self,
  ):
    """Verify dirty target repo (modified or untracked) exits 3 untouched (REQ-0001)."""
    # Case A: Uncommitted modification to tracked file
    target_mod = os.path.join(self.tmp_dir, "target_modified")
    t1_sha = _init_git_repo(target_mod, initial_commit=True)
    tracked_file = os.path.join(target_mod, "INITIAL.txt")
    with open(tracked_file, "a", encoding="utf-8") as f:
      f.write("uncommitted edit\n")

    res_mod = self._run_distribute("--target", target_mod)
    self.assertEqual(res_mod.returncode, 3)
    self.assertIn("uncommitted changes or untracked files", res_mod.stderr)
    self.assertEqual(
        _run_git(target_mod, "rev-parse", "HEAD").stdout.strip(), t1_sha
    )
    with open(tracked_file, "r", encoding="utf-8") as f:
      self.assertIn("uncommitted edit", f.read())
    self.assertFalse(os.path.exists(os.path.join(target_mod, "README.md")))

    # Case B: Untracked file in target working tree
    target_untracked = os.path.join(self.tmp_dir, "target_untracked")
    t2_sha = _init_git_repo(target_untracked, initial_commit=True)
    untracked_file = os.path.join(target_untracked, "untracked_note.txt")
    with open(untracked_file, "w", encoding="utf-8") as f:
      f.write("work in progress\n")

    res_untracked = self._run_distribute("--target", target_untracked)
    self.assertEqual(res_untracked.returncode, 3)
    self.assertIn(
        "uncommitted changes or untracked files", res_untracked.stderr
    )
    self.assertEqual(
        _run_git(target_untracked, "rev-parse", "HEAD").stdout.strip(), t2_sha
    )
    self.assertTrue(os.path.exists(untracked_file))
    self.assertFalse(
        os.path.exists(os.path.join(target_untracked, "README.md"))
    )

  def test_rejects_invalid_source_ref_with_exit_2(self):
    """Verify invalid --source-ref logs error to stderr and exits 2 (REQ-0001)."""
    target = os.path.join(self.tmp_dir, "target_repo")
    _init_git_repo(target, initial_commit=True)
    res = self._run_distribute(
        "--target", target, "--source-ref", "nonexistent-ref-xyz"
    )
    self.assertEqual(res.returncode, 2)
    self.assertIn("nonexistent-ref-xyz", res.stderr)

  def test_dry_run_prints_diff_summary_without_modifying_target(self):
    """Verify --dry-run prints diff summary to stderr and leaves target untouched (REQ-0001)."""
    target = os.path.join(self.tmp_dir, "target_dry_run")
    t1_sha = _init_git_repo(target, initial_commit=True)
    before_objects = _run_git(
        target, "rev-list", "--all", "--objects"
    ).stdout.strip()

    res = self._run_distribute(
        "--target", target, "--include-top-level", "--dry-run"
    )
    self.assertEqual(res.returncode, 0, msg=res.stderr)
    self.assertIn("Dry-run mode", res.stderr)
    self.assertIn("README.md", res.stderr)
    self.assertIn(".github/actions/codemender/action.yml", res.stderr)

    # Verify HEAD, index, working tree, and object store are unmodified
    self.assertEqual(
        _run_git(target, "rev-parse", "HEAD").stdout.strip(), t1_sha
    )
    self.assertEqual(_run_git(target, "status", "--porcelain").stdout, "")
    self.assertFalse(os.path.exists(os.path.join(target, "README.md")))
    after_objects = _run_git(
        target, "rev-list", "--all", "--objects"
    ).stdout.strip()
    self.assertEqual(before_objects, after_objects)

  def test_clean_git_archive_ignores_untracked_and_uncommitted_source_edits(
      self,
  ):
    """Verify untracked files and uncommitted edits in source are excluded (REQ-0002)."""
    src_clone = self._clone_source_repo("dirty_source")
    secret_file = os.path.join(
        src_clone, "codemender-runner", "secret_notes.txt"
    )
    with open(secret_file, "w", encoding="utf-8") as f:
      f.write("TOP SECRET UNTRACKED NOTE\n")

    cli_main_path = os.path.join(
        src_clone,
        "packages",
        "codemender",
        "src",
        "codemender",
        "runner",
        "__main__.py",
    )
    with open(cli_main_path, "r", encoding="utf-8") as f:
      committed_cli_main = f.read()
    with open(cli_main_path, "a", encoding="utf-8") as f:
      f.write("\n# UNCOMMITTED_DIRTY_MARKER\n")

    target = os.path.join(self.tmp_dir, "target_clean_archive")
    _init_git_repo(target, initial_commit=False)

    res = self._run_distribute(
        "--target",
        target,
        "--source-ref",
        "HEAD",
        source_repo=src_clone,
    )
    self.assertEqual(res.returncode, 0, msg=res.stderr)

    # Untracked file must not exist in target
    self.assertFalse(
        os.path.exists(
            os.path.join(
                target, ".github", "codemender-runner", "secret_notes.txt"
            )
        )
    )
    # Uncommitted edit to runner/__main__.py must not be present in target
    target_cli_main = os.path.join(
        target,
        ".github",
        "codemender-runner",
        "packages",
        "codemender",
        "src",
        "codemender",
        "runner",
        "__main__.py",
    )
    with open(target_cli_main, "r", encoding="utf-8") as f:
      target_content = f.read()
    self.assertNotIn("UNCOMMITTED_DIRTY_MARKER", target_content)
    self.assertEqual(target_content, committed_cli_main)

  # ---------------------------------------------------------------------------
  # Task 2: Target Layout Mapping, Legacy Stripping, Self-Contained Unit Tests,
  # and Documentation Link Verification
  # Governing: docs/openspec/specs/distribute/spec.md#req-0003,
  #            docs/openspec/specs/distribute/spec.md#req-0006
  # ---------------------------------------------------------------------------

  def test_target_layout_legacy_stripping_unit_tests_and_doc_links(self):
    """Verify distributed layout, legacy stripping, self-contained tests, and doc links (REQ-0003, REQ-0006)."""
    target = os.path.join(self.tmp_dir, "distro_repo")
    _init_git_repo(target, initial_commit=False)

    res = self._run_distribute("--target", target, "--include-top-level")
    self.assertEqual(res.returncode, 0, msg=res.stderr)

    # Verify root contains only .git, .github, .gitignore, LICENSE, and README.md
    self.assertEqual(
        set(os.listdir(target)),
        {".git", ".github", ".gitignore", "LICENSE", "README.md"},
    )
    # Verify .github contains only actions, workflows, codemender-runner, and terraform
    self.assertEqual(
        set(os.listdir(os.path.join(target, ".github"))),
        {"actions", "workflows", "codemender-runner", "terraform"},
    )

    # 1. Verify required files and directories exist (SPEC-DIST/REQ-0003)
    required_paths = [
        "LICENSE",
        ".gitignore",
        "README.md",
        ".github/actions/codemender/action.yml",
        ".github/actions/codemender-local/action.yml",
        ".github/actions/codemender/requirements.txt",
        ".github/actions/codemender/src/gha_runner.py",
        ".github/actions/codemender/tests/test_gha_runner.py",
        ".github/workflows/cm-merge-scan.yml",
        ".github/workflows/build_runner_image.yml",
        ".github/terraform/main.tf",
        ".github/terraform/variables.tf",
        ".github/terraform/outputs.tf",
        ".github/terraform/provider.tf",
        ".github/terraform/terraform.tfvars.example",
        ".github/terraform/.terraform.lock.hcl",
        ".github/terraform/tests/root_test.tftest.hcl",
        ".github/terraform/modules/wif/apis.tf",
        ".github/terraform/modules/wif/iam.tf",
        ".github/terraform/modules/wif/github.tf",
        ".github/terraform/modules/wif/outputs.tf",
        ".github/terraform/modules/wif/provider.tf",
        ".github/terraform/modules/wif/variables.tf",
        ".github/terraform/modules/wif/tests/wif_test.tftest.hcl",
        ".github/codemender-runner/Dockerfile",
        ".github/codemender-runner/.dockerignore",
        ".github/codemender-runner/requirements.txt",
        ".github/codemender-runner/__main__.py",
        ".github/codemender-runner/bin/cm-runner",
        ".github/codemender-runner/packages/codemender/pyproject.toml",
        ".github/codemender-runner/packages/codemender/src/codemender/__init__.py",
        ".github/codemender-runner/packages/codemender/src/codemender/cm.py",
        ".github/codemender-runner/packages/codemender/src/codemender/sarif/__init__.py",
        ".github/codemender-runner/packages/codemender/src/codemender/vcs/__init__.py",
        ".github/codemender-runner/packages/codemender/src/codemender/vcs/git.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/__init__.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/__main__.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/config.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/events.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/exit_codes.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/commands/__init__.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/commands/version.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/commands/init.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/commands/find_diff.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/commands/fix.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/commands/verify.py",
        ".github/codemender-runner/packages/launcher/pyproject.toml",
        ".github/codemender-runner/packages/launcher/src/launcher/__init__.py",
        ".github/codemender-runner/packages/launcher/src/launcher/base.py",
        ".github/codemender-runner/packages/launcher/src/launcher/local/__init__.py",
        ".github/codemender-runner/packages/launcher/src/launcher/local/local_docker.py",
        ".github/codemender-runner/packages/launcher/src/launcher/cloudrun/__init__.py",
        ".github/codemender-runner/packages/launcher/src/launcher/cloudrun/cloud_run.py",
        ".github/codemender-runner/packages/repohost/pyproject.toml",
        ".github/codemender-runner/packages/repohost/src/repohost/__init__.py",
        ".github/codemender-runner/packages/repohost/src/repohost/github/__init__.py",
        ".github/codemender-runner/packages/repohost/src/repohost/github/client.py",
        ".github/codemender-runner/packages/repohost/src/repohost/github/fix_comments.py",
        ".github/codemender-runner/packages/repohost/src/repohost/github/branches.py",
        ".github/codemender-runner/packages/repohost/src/repohost/gitlab/__init__.py",
    ]
    for rel_path in required_paths:
      with self.subTest(required_path=rel_path):
        self.assertTrue(
            os.path.exists(os.path.join(target, rel_path)),
            f"Expected required path '{rel_path}' to exist in target",
        )

    # Verify Dockerfile, build_runner_image.yml, and __main__.py transformations
    with open(
        os.path.join(target, ".github", "codemender-runner", "Dockerfile"),
        "r",
        encoding="utf-8",
    ) as f:
      dockerfile_content = f.read()
    self.assertIn('CMD ["version"]', dockerfile_content)
    self.assertNotIn('CMD ["legacy"]', dockerfile_content)
    self.assertNotIn("main.py", dockerfile_content)

    with open(
        os.path.join(target, ".github", "workflows", "build_runner_image.yml"),
        "r",
        encoding="utf-8",
    ) as f:
      build_wf_content = f.read()
    self.assertIn('["version"]', build_wf_content)
    self.assertNotIn('["legacy"]', build_wf_content)
    self.assertIn("context: .github/codemender-runner", build_wf_content)
    self.assertIn("file: .github/codemender-runner/Dockerfile", build_wf_content)
    self.assertNotIn(".github/.github/", build_wf_content)
    self.assertNotIn("context: codemender-runner", build_wf_content)
    self.assertIn("vars.CODEMENDER_RUNNER_IMAGE", build_wf_content)
    self.assertIn("ghcr.io/${OWNER,,}/codemender-runner", build_wf_content)

    with open(
        os.path.join(target, ".github", "workflows", "cm-merge-scan.yml"),
        "r",
        encoding="utf-8",
    ) as f:
      merge_scan_wf_content = f.read()
    self.assertNotIn("<your-registry>", merge_scan_wf_content)
    self.assertIn("vars.CODEMENDER_RUNNER_IMAGE", merge_scan_wf_content)
    self.assertIn(
        "format('ghcr.io/{0}/codemender-runner:latest', github.repository_owner)",
        merge_scan_wf_content,
    )
    self.assertIn("vars.CODEMENDER_GCP_PROJECT_ID", merge_scan_wf_content)
    self.assertIn(
        "secrets.CODEMENDER_WORKLOAD_IDENTITY_PROVIDER", merge_scan_wf_content
    )
    self.assertIn("secrets.CODEMENDER_SERVICE_ACCOUNT", merge_scan_wf_content)

    local_action_path = (
        os.path.join(target, ".github", "actions", "codemender-local", "action.yml")
        if os.path.exists(
            os.path.join(
                target, ".github", "actions", "codemender-local", "action.yml"
            )
        )
        else os.path.join(target, ".github", "actions", "codemender", "action.yml")
    )
    with open(
        local_action_path,
        "r",
        encoding="utf-8",
    ) as f:
      action_yml_content = f.read()
    self.assertIn("id: pull-image", action_yml_content)
    self.assertIn("steps.pull-image.outputs.image", action_yml_content)
    self.assertIn("${repo_part,,}:${tag_part}", action_yml_content)

    with open(
        os.path.join(target, ".github", "codemender-runner", "__main__.py"),
        "r",
        encoding="utf-8",
    ) as f:
      dunder_main_content = f.read()
    self.assertIn("codemender.runner.__main__", dunder_main_content)
    self.assertNotIn("legacy", dunder_main_content)

    # 2. Verify all legacy, Cloud Run, internal, and test/testdata paths are absent (SPEC-DIST/REQ-0003)
    excluded_paths = [
        "scripts",
        ".github/scripts",
        ".github/codemender",
        ".github/workflows/ci.yml",
        "codemender-runner",
        "packages",
        "docker",
        "terraform",
        "docs",
        ".github/docs",
        ".dist.json",
        ".codemender-dist.json",
        ".github/terraform/wif",
        ".github/terraform/gha_wif",
        ".github/terraform/gcp",
        ".github/terraform/modules/gcp",
        ".github/terraform/gcp.tf",
        ".github/terraform/gcp_override.tf",
        ".github/terraform/tests/root_gcp_test.tftest.hcl",
        ".github/codemender-runner/main.py",
        ".github/codemender-runner/utils.py",
        ".github/codemender-runner/storage.py",
        ".github/codemender-runner/runners",
        ".github/codemender-runner/codemender/db.py",
        ".github/codemender-runner/vcs/github.py",
        ".github/codemender-runner/README.md",
        ".github/actions/codemender/src/launcher",
        ".github/actions/codemender/tests/test_launcher.py",
        ".github/codemender-runner/packages/codemender/src/codemender/test_cm.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/test_cli.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/test_config.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/commands/test_init.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/commands/test_find_diff.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/commands/test_fix.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/commands/test_verify.py",
        ".github/codemender-runner/packages/codemender/src/codemender/runner/commands/testdata",
        ".github/codemender-runner/packages/codemender/src/codemender/sarif/test_sarif.py",
        ".github/codemender-runner/packages/codemender/src/codemender/vcs/test_git.py",
        ".github/codemender-runner/packages/launcher/src/launcher/local/test_launcher.py",
        ".github/codemender-runner/packages/repohost/src/repohost/github/test_client.py",
        ".github/codemender-runner/packages/repohost/src/repohost/github/test_fix_comments.py",
        ".github/codemender-runner/packages/repohost/src/repohost/github/test_branches.py",
        "workflows/gcp_parallel_workflow.yaml",
        "cloudbuild.yaml",
    ]
    for rel_path in excluded_paths:
      with self.subTest(excluded_path=rel_path):
        self.assertFalse(
            os.path.exists(os.path.join(target, rel_path)),
            f"Expected excluded path '{rel_path}' to be absent from target",
        )

    # 3. Verify runner entrypoints and GHA unit test suite inside the distributed repository (REQ-0003)
    cmr_help = subprocess.run(
        [sys.executable, ".github/codemender-runner", "--help"],
        cwd=target,
        capture_output=True,
        text=True,
        check=False,
    )
    self.assertEqual(
        cmr_help.returncode,
        0,
        msg=f"python .github/codemender-runner --help failed:\n{cmr_help.stderr}",
    )

    gha_env = os.environ.copy()
    gha_env.pop("GITHUB_WORKSPACE", None)
    staged_pkg_srcs = [
        os.path.join(
            target, ".github", "codemender-runner", "packages", pkg, "src"
        )
        for pkg in ("codemender", "launcher", "repohost")
    ]
    existing_pythonpath = gha_env.get("PYTHONPATH", "")
    gha_env["PYTHONPATH"] = os.pathsep.join(
        staged_pkg_srcs + ([existing_pythonpath] if existing_pythonpath else [])
    )
    gha_tests = subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            ".github/actions/codemender/tests",
        ],
        cwd=target,
        env=gha_env,
        capture_output=True,
        text=True,
        check=False,
    )
    self.assertEqual(
        gha_tests.returncode,
        0,
        msg=f"GHA action tests failed:\n{gha_tests.stderr}",
    )

    with open(_DISTRIBUTE_SCRIPT, "r", encoding="utf-8") as f:
      distribute_script_content = f.read()
    self.assertNotIn("github-actions", distribute_script_content)
    self.assertNotIn("codemender_parallel.yml", distribute_script_content)

  def test_distributed_documentation_links_resolve_validly(self):
    """Verify all relative links and unified Terraform instructions in distributed README.md (REQ-0006)."""
    target = os.path.join(self.tmp_dir, "distro_docs_repo")
    _init_git_repo(target, initial_commit=False)

    res = self._run_distribute("--target", target, "--include-top-level")
    self.assertEqual(res.returncode, 0, msg=res.stderr)

    doc_files = [
        "README.md",
    ]
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

    for rel_doc in doc_files:
      abs_doc = os.path.join(target, rel_doc)
      self.assertTrue(os.path.isfile(abs_doc), f"Missing doc file {rel_doc}")
      with open(abs_doc, "r", encoding="utf-8") as f:
        content = f.read()

      self.assertNotIn("../adr/", content, f"Dangling ../adr/ in {rel_doc}")
      self.assertNotIn(
          "docs/guides/", content, f"Dangling docs/guides/ in {rel_doc}"
      )
      self.assertNotIn(
          ".github/terraform/wif",
          content,
          f"Legacy .github/terraform/wif path in {rel_doc}",
      )
      self.assertNotIn(
          "target_github_repositories",
          content,
          f"Legacy target_github_repositories variable in {rel_doc}",
      )
      self.assertIn(
          "modules/wif",
          content,
          f"Expected modules/wif layout reference in {rel_doc}",
      )
      self.assertIn(
          "cd .github/terraform",
          content,
          f"Expected root 'cd .github/terraform' instruction in {rel_doc}",
      )
      self.assertIn(
          "github_repositories",
          content,
          f"Expected single github_repositories variable in {rel_doc}",
      )
      self.assertIn(
          "(.github/terraform/)",
          content,
          f"Expected link to .github/terraform/ in {rel_doc}",
      )
      self.assertIn(
          "(.github/terraform/terraform.tfvars.example)",
          content,
          f"Expected link to .github/terraform/terraform.tfvars.example in {rel_doc}",
      )
      self.assertIn(
          "CODEMENDER_WORKLOAD_IDENTITY_PROVIDER",
          content,
          f"Expected CODEMENDER_WORKLOAD_IDENTITY_PROVIDER secret in {rel_doc}",
      )
      self.assertIn(
          "CODEMENDER_SERVICE_ACCOUNT",
          content,
          f"Expected CODEMENDER_SERVICE_ACCOUNT secret in {rel_doc}",
      )
      self.assertIn(
          "CODEMENDER_GCP_PROJECT_ID",
          content,
          f"Expected CODEMENDER_GCP_PROJECT_ID variable in {rel_doc}",
      )
      self.assertIn(
          "ghcr.io/<owner>/codemender-runner:<commit-sha>",
          content,
          f"Expected ghcr.io/<owner>/codemender-runner:<commit-sha> in {rel_doc}",
      )

      doc_dir = os.path.dirname(abs_doc)
      for match in link_pattern.finditer(content):
        raw_target = match.group(1).strip()
        if (
            not raw_target
            or raw_target.startswith("#")
            or "://" in raw_target
            or raw_target.startswith("mailto:")
        ):
          continue
        path_part = raw_target.split("#", 1)[0]
        if not path_part:
          continue
        resolved = os.path.normpath(os.path.join(doc_dir, path_part))
        self.assertTrue(
            os.path.exists(resolved),
            f"Broken relative link '{raw_target}' in '{rel_doc}' (resolved to '{resolved}')",
        )

  # ---------------------------------------------------------------------------
  # Task 3: 3-Way Incremental Sync, Conflict Detection, --branch, and
  # --include-top-level Behavior
  # (SPEC-DIST/REQ-0001, REQ-0004, REQ-0005)
  # ---------------------------------------------------------------------------

  def test_initial_sync_records_trailer_and_immediate_repeat_is_noop(self):
    """Verify initial sync commits trailer, creates no .dist.json, and repeat is a no-op (REQ-0004)."""
    target = os.path.join(self.tmp_dir, "target_initial")
    _init_git_repo(target, initial_commit=False)

    res1 = self._run_distribute(
        "--target", target, "--source-ref", self.c1_sha
    )
    self.assertEqual(res1.returncode, 0, msg=res1.stderr)
    self.assertFalse(os.path.exists(os.path.join(target, ".dist.json")))

    commit_msg = _run_git(target, "log", "-n", "1", "--format=%B").stdout
    self.assertIn(f"Codemender-Source-Commit: {self.c1_sha}", commit_msg)
    first_head = _run_git(target, "rev-parse", "HEAD").stdout.strip()

    # Immediate repeat sync at same source ref must be a no-op
    res2 = self._run_distribute(
        "--target", target, "--source-ref", self.c1_sha
    )
    self.assertEqual(res2.returncode, 0, msg=res2.stderr)
    self.assertIn("already up to date", res2.stderr)
    second_head = _run_git(target, "rev-parse", "HEAD").stdout.strip()
    self.assertEqual(first_head, second_head)
    self.assertEqual(_run_git(target, "status", "--porcelain").stdout, "")

  def test_repeat_sync_preserves_target_only_files_and_non_conflicting_edits(
      self,
  ):
    """Verify 3-way sync from C1 to C2 merges upstream changes and keeps target edits (REQ-0004)."""
    src_clone = self._clone_source_repo("src_c1_c2")
    target = os.path.join(self.tmp_dir, "target_3way")
    _init_git_repo(target, initial_commit=False)

    # Sync C1
    res1 = self._run_distribute(
        "--target", target, "--source-ref", "HEAD", source_repo=src_clone
    )
    self.assertEqual(res1.returncode, 0, msg=res1.stderr)

    # In target: add target-only CODEOWNERS and customize runner_image in cm-merge-scan.yml
    codeowners_path = os.path.join(target, "CODEOWNERS")
    with open(codeowners_path, "w", encoding="utf-8") as f:
      f.write("* @customer-security-team\n")

    wf_path = os.path.join(target, ".github", "workflows", "cm-merge-scan.yml")
    with open(wf_path, "r", encoding="utf-8") as f:
      wf_content = f.read()
    self.assertIn("runner_image:", wf_content)
    custom_wf_content = wf_content.replace(
        "ghcr.io/cloud-gtm/codemender-runner:REPLACE_WITH_COMMIT_SHA",
        "us-docker.pkg.dev/customer-proj/cm/codemender-runner:abc1234",
    )
    # Fallback if exact placeholder differs: replace runner_image line
    if custom_wf_content == wf_content:
      custom_wf_content = re.sub(
          r"(runner_image:\s*).*",
          r"\1'us-docker.pkg.dev/customer-proj/cm/codemender-runner:abc1234'",
          wf_content,
          count=1,
      )
    with open(wf_path, "w", encoding="utf-8") as f:
      f.write(custom_wf_content)

    _run_git(target, "add", "CODEOWNERS", ".github/workflows/cm-merge-scan.yml")
    _run_git(
        target, "commit", "-q", "-m", "Customize CODEOWNERS and runner_image"
    )

    # In source: create C2 updating packages/codemender/src/codemender/runner/commands/find_diff.py
    find_diff_src = os.path.join(
        src_clone,
        "packages",
        "codemender",
        "src",
        "codemender",
        "runner",
        "commands",
        "find_diff.py",
    )
    with open(find_diff_src, "a", encoding="utf-8") as f:
      f.write("\n# C2_UPSTREAM_ENHANCEMENT = True\n")
    _run_git(
        src_clone,
        "add",
        "packages/codemender/src/codemender/runner/commands/find_diff.py",
    )
    _run_git(src_clone, "commit", "-q", "-m", "Upstream C2 update to find_diff")
    c2_sha = _run_git(src_clone, "rev-parse", "HEAD").stdout.strip()

    # Sync C2 into target
    res2 = self._run_distribute(
        "--target", target, "--source-ref", c2_sha, source_repo=src_clone
    )
    self.assertEqual(res2.returncode, 0, msg=res2.stderr)

    # Verify find_diff.py has C2 update
    find_diff_target = os.path.join(
        target,
        ".github",
        "codemender-runner",
        "packages",
        "codemender",
        "src",
        "codemender",
        "runner",
        "commands",
        "find_diff.py",
    )
    with open(find_diff_target, "r", encoding="utf-8") as f:
      self.assertIn("C2_UPSTREAM_ENHANCEMENT = True", f.read())

    # Verify CODEOWNERS and customized runner_image in cm-merge-scan.yml remain intact
    self.assertTrue(os.path.isfile(codeowners_path))
    with open(codeowners_path, "r", encoding="utf-8") as f:
      self.assertEqual(f.read(), "* @customer-security-team\n")
    with open(wf_path, "r", encoding="utf-8") as f:
      self.assertIn(
          "us-docker.pkg.dev/customer-proj/cm/codemender-runner:abc1234",
          f.read(),
      )

    # Verify commit trailer records C2
    latest_msg = _run_git(target, "log", "-n", "1", "--format=%B").stdout
    self.assertIn(f"Codemender-Source-Commit: {c2_sha}", latest_msg)

  def test_repeat_sync_propagates_upstream_deletions_without_deleting_target_files(
      self,
  ):
    """Verify upstream file deletions between C1 and C2 delete in target while preserving SECURITY.md (REQ-0004)."""
    src_clone = self._clone_source_repo("src_deletions")
    helper_rel = os.path.join(
        "packages",
        "codemender",
        "src",
        "codemender",
        "runner",
        "commands",
        "temp_helper.py",
    )
    helper_target_rel = os.path.join(
        ".github",
        "codemender-runner",
        "packages",
        "codemender",
        "src",
        "codemender",
        "runner",
        "commands",
        "temp_helper.py",
    )
    helper_src = os.path.join(src_clone, helper_rel)
    with open(helper_src, "w", encoding="utf-8") as f:
      f.write("# temporary distributed helper\n")
    _run_git(src_clone, "add", helper_rel)
    _run_git(src_clone, "commit", "-q", "-m", "Add temp_helper in C1_plus")
    c1_plus_sha = _run_git(src_clone, "rev-parse", "HEAD").stdout.strip()

    target = os.path.join(self.tmp_dir, "target_deletions")
    _init_git_repo(target, initial_commit=False)

    # Initial sync at c1_plus_sha
    res1 = self._run_distribute(
        "--target", target, "--source-ref", c1_plus_sha, source_repo=src_clone
    )
    self.assertEqual(res1.returncode, 0, msg=res1.stderr)
    self.assertTrue(os.path.exists(os.path.join(target, helper_target_rel)))

    # Add target-only SECURITY.md
    security_md = os.path.join(target, "SECURITY.md")
    with open(security_md, "w", encoding="utf-8") as f:
      f.write("# Security Policy\n")
    _run_git(target, "add", "SECURITY.md")
    _run_git(target, "commit", "-q", "-m", "Add target-only SECURITY.md")

    # Remove temp_helper.py upstream in C2
    _run_git(src_clone, "rm", "-q", helper_rel)
    _run_git(src_clone, "commit", "-q", "-m", "Remove temp_helper in C2")
    c2_sha = _run_git(src_clone, "rev-parse", "HEAD").stdout.strip()

    # Repeat sync at C2
    res2 = self._run_distribute(
        "--target", target, "--source-ref", c2_sha, source_repo=src_clone
    )
    self.assertEqual(res2.returncode, 0, msg=res2.stderr)
    self.assertFalse(os.path.exists(os.path.join(target, helper_target_rel)))
    self.assertTrue(os.path.isfile(security_md))

  def test_overlapping_line_conflict_exits_4_and_missing_prev_sha_exits_2(self):
    """Verify overlapping edits leave conflict markers and exit 4, and missing prev_sha exits 2 (REQ-0004)."""
    src_clone = self._clone_source_repo("src_conflict")
    target = os.path.join(self.tmp_dir, "target_conflict")
    _init_git_repo(target, initial_commit=False)

    # Sync C1
    res1 = self._run_distribute(
        "--target", target, "--source-ref", "HEAD", source_repo=src_clone
    )
    self.assertEqual(res1.returncode, 0, msg=res1.stderr)

    # Modify line 1 of .github/workflows/cm-merge-scan.yml in target
    target_wf = os.path.join(
        target, ".github", "workflows", "cm-merge-scan.yml"
    )
    with open(target_wf, "r", encoding="utf-8") as f:
      wf_lines = f.readlines()
    wf_lines[0] = "# TARGET_CUSTOM_HEADER_LINE_1\n"
    with open(target_wf, "w", encoding="utf-8") as f:
      f.writelines(wf_lines)
    _run_git(target, "add", ".github/workflows/cm-merge-scan.yml")
    _run_git(target, "commit", "-q", "-m", "Target edit to line 1")

    # Modify line 1 of .github/workflows/cm-merge-scan.yml in source to a conflicting value
    src_wf = os.path.join(
        src_clone, ".github", "workflows", "cm-merge-scan.yml"
    )
    with open(src_wf, "r", encoding="utf-8") as f:
      src_wf_lines = f.readlines()
    src_wf_lines[0] = "# UPSTREAM_CONFLICTING_HEADER_LINE_1\n"
    with open(src_wf, "w", encoding="utf-8") as f:
      f.writelines(src_wf_lines)
    _run_git(src_clone, "add", ".github/workflows/cm-merge-scan.yml")
    _run_git(src_clone, "commit", "-q", "-m", "Upstream conflicting edit")
    c2_sha = _run_git(src_clone, "rev-parse", "HEAD").stdout.strip()

    # Run distribute.sh -> expect exit code 4 and conflict markers
    res_conflict = self._run_distribute(
        "--target", target, "--source-ref", c2_sha, source_repo=src_clone
    )
    self.assertEqual(res_conflict.returncode, 4)
    self.assertIn(
        ".github/workflows/cm-merge-scan.yml", res_conflict.stderr
    )
    with open(target_wf, "r", encoding="utf-8") as f:
      conflicted_content = f.read()
    self.assertIn("<<<<<<<", conflicted_content)
    self.assertIn("=======", conflicted_content)
    self.assertIn(">>>>>>>", conflicted_content)

    # Also verify missing <prev_source_sha> in source repository exits 2
    target_missing_prev = os.path.join(self.tmp_dir, "target_missing_prev")
    _init_git_repo(target_missing_prev, initial_commit=True)
    fake_sha = "deadbeef" * 5
    _run_git(
        target_missing_prev,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "Sync from vanished commit",
        "-m",
        f"Codemender-Source-Commit: {fake_sha}",
    )
    res_missing_prev = self._run_distribute(
        "--target", target_missing_prev, source_repo=src_clone
    )
    self.assertEqual(res_missing_prev.returncode, 2)
    self.assertIn(fake_sha, res_missing_prev.stderr)

  def test_branch_flag_creates_and_switches_to_target_branch(self):
    """Verify --branch NAME creates or switches to branch in target before committing (REQ-0001, REQ-0004)."""
    target = os.path.join(self.tmp_dir, "target_branch")
    _init_git_repo(target, initial_commit=True)
    _run_git(target, "branch", "existing-sync-branch")

    # 1. Creating a brand-new branch via --branch
    res = self._run_distribute(
        "--target", target, "--branch", "codemender-sync"
    )
    self.assertEqual(res.returncode, 0, msg=res.stderr)
    current_branch = _run_git(
        target, "rev-parse", "--abbrev-ref", "HEAD"
    ).stdout.strip()
    self.assertEqual(current_branch, "codemender-sync")
    commit_msg = _run_git(target, "log", "-n", "1", "--format=%B").stdout
    self.assertIn(f"Codemender-Source-Commit: {self.c1_sha}", commit_msg)

    # 2. Switching to an already-existing branch via --branch
    res_existing = self._run_distribute(
        "--target", target, "--branch", "existing-sync-branch"
    )
    self.assertEqual(res_existing.returncode, 0, msg=res_existing.stderr)
    current_branch_2 = _run_git(
        target, "rev-parse", "--abbrev-ref", "HEAD"
    ).stdout.strip()
    self.assertEqual(current_branch_2, "existing-sync-branch")
    commit_msg_2 = _run_git(target, "log", "-n", "1", "--format=%B").stdout
    self.assertIn(f"Codemender-Source-Commit: {self.c1_sha}", commit_msg_2)

  def test_unknown_cli_flag_exits_2(self):
    """Verify unknown CLI flags log an error to stderr and exit 2 (REQ-0001)."""
    target = os.path.join(self.tmp_dir, "target_unknown_flag")
    _init_git_repo(target, initial_commit=True)
    res = self._run_distribute("--target", target, "--unknown-flag")
    self.assertEqual(res.returncode, 2)
    self.assertIn("--unknown-flag", res.stderr)

  def test_evolving_distribute_script_adds_newly_included_directory_on_repeat_sync(
      self,
  ):
    """Verify reconstructing $base_tree via <prev_source_sha>'s distribute.sh adds newly included Cloud Run components in C2 (REQ-0003, REQ-0004, Decision 5)."""
    src_clone = self._clone_source_repo("src_evolving_dist")
    target = os.path.join(self.tmp_dir, "target_evolving_dist")
    _init_git_repo(target, initial_commit=False)

    # 1. Sync at C1 (where terraform/modules/gcp/ and gcp*.tf exist in src_clone
    #    but are stripped by C1's distribute.sh while root terraform/ + modules/wif/ are included)
    self.assertTrue(
        os.path.isdir(os.path.join(src_clone, "terraform", "modules", "gcp")),
        "Precondition: terraform/modules/gcp exists in monorepo at C1",
    )
    self.assertTrue(
        os.path.isfile(os.path.join(src_clone, "terraform", "gcp.tf")),
        "Precondition: terraform/gcp.tf exists in monorepo at C1",
    )
    res1 = self._run_distribute(
        "--target", target, "--source-ref", "HEAD", source_repo=src_clone
    )
    self.assertEqual(res1.returncode, 0, msg=res1.stderr)
    self.assertTrue(
        os.path.isfile(os.path.join(target, ".github", "terraform", "main.tf"))
    )
    self.assertTrue(
        os.path.isdir(
            os.path.join(target, ".github", "terraform", "modules", "wif")
        )
    )
    self.assertFalse(
        os.path.exists(
            os.path.join(target, ".github", "terraform", "modules", "gcp")
        )
    )
    self.assertFalse(
        os.path.exists(os.path.join(target, ".github", "terraform", "gcp.tf"))
    )

    # 2. In C2, update ONLY scripts/distribute.sh so stage_distribution_tree also retains
    #    terraform/modules/gcp/, terraform/gcp.tf, terraform/gcp_override.tf, and
    #    terraform/tests/root_gcp_test.tftest.hcl
    src_dist_script = os.path.join(src_clone, "scripts", "distribute.sh")
    with open(src_dist_script, "r", encoding="utf-8") as f:
      script_text = f.read()
    anchor = "\n}\n\n# Handle internal --stage-only mode"
    cloud_run_retention_snippet = (
        '\n  if [[ -d "$src_dir/terraform/modules/gcp" ]]; then\n'
        '    mkdir -p "$out_dir/.github/terraform/modules/gcp" "$out_dir/.github/terraform/tests"\n'
        '    cp -R "$src_dir/terraform/modules/gcp/." "$out_dir/.github/terraform/modules/gcp/"\n'
        '    [[ -f "$src_dir/terraform/gcp.tf" ]] && cp "$src_dir/terraform/gcp.tf" "$out_dir/.github/terraform/gcp.tf"\n'
        '    [[ -f "$src_dir/terraform/gcp_override.tf" ]] && cp "$src_dir/terraform/gcp_override.tf" "$out_dir/.github/terraform/gcp_override.tf"\n'
        '    [[ -f "$src_dir/terraform/tests/root_gcp_test.tftest.hcl" ]] && cp "$src_dir/terraform/tests/root_gcp_test.tftest.hcl" "$out_dir/.github/terraform/tests/root_gcp_test.tftest.hcl"\n'
        "  fi"
        + anchor
    )
    self.assertIn(anchor, script_text)
    with open(src_dist_script, "w", encoding="utf-8") as f:
      f.write(script_text.replace(anchor, cloud_run_retention_snippet, 1))
    _run_git(src_clone, "add", "scripts/distribute.sh")
    _run_git(
        src_clone,
        "commit",
        "-q",
        "-m",
        "Enable Cloud Run terraform components in distribute.sh (C2)",
    )
    c2_sha = _run_git(src_clone, "rev-parse", "HEAD").stdout.strip()

    # 3. Run C2's distribute.sh against target -> $base_tree is built using C1's distribute.sh,
    #    so Cloud Run terraform files are absent from $base_tree and present in $new_tree, and are added to target!
    res2 = self._run_distribute(
        "--target",
        target,
        "--source-ref",
        c2_sha,
        source_repo=src_clone,
        script_path=src_dist_script,
    )
    self.assertEqual(res2.returncode, 0, msg=res2.stderr)
    self.assertTrue(
        os.path.isdir(
            os.path.join(target, ".github", "terraform", "modules", "gcp")
        )
    )
    self.assertTrue(
        os.path.isfile(os.path.join(target, ".github", "terraform", "gcp.tf"))
    )
    self.assertTrue(
        os.path.isfile(
            os.path.join(target, ".github", "terraform", "gcp_override.tf")
        )
    )
    self.assertTrue(
        os.path.isfile(
            os.path.join(
                target,
                ".github",
                "terraform",
                "tests",
                "root_gcp_test.tftest.hcl",
            )
        )
    )

  # Governing: docs/openspec/specs/distribute/spec.md#req-0005
  def test_include_top_level_flag_and_default_github_only_sync(self):
    """Verify --include-top-level includes root files while default mode stages only .github/ and preserves existing root files (REQ-0005)."""
    src_clone = self._clone_source_repo("src_for_distro")

    # 1. Default mode (no --include-top-level) on an existing repo with its own README.md:
    #    Only .github/ is staged; existing README.md is untouched and LICENSE is not added.
    existing_app = os.path.join(self.tmp_dir, "existing_app")
    _init_git_repo(existing_app, initial_commit=True)
    app_readme = os.path.join(existing_app, "README.md")
    with open(app_readme, "w", encoding="utf-8") as f:
      f.write("# My Custom Application README\n")
    _run_git(existing_app, "add", "README.md")
    _run_git(existing_app, "commit", "-q", "-m", "Add app README.md")

    res_default = self._run_distribute(
        "--target", existing_app, source_repo=src_clone
    )
    self.assertEqual(res_default.returncode, 0, msg=res_default.stderr)
    with open(app_readme, "r", encoding="utf-8") as f:
      self.assertEqual(f.read(), "# My Custom Application README\n")
    self.assertFalse(os.path.exists(os.path.join(existing_app, "LICENSE")))
    self.assertTrue(
        os.path.isfile(
            os.path.join(
                existing_app, ".github", "actions", "codemender", "action.yml"
            )
        )
    )
    self.assertTrue(
        os.path.isfile(
            os.path.join(
                existing_app,
                ".github",
                "codemender-runner",
                "bin",
                "cm-runner",
            )
        )
    )
    self.assertTrue(
        os.path.isfile(
            os.path.join(existing_app, ".github", "terraform", "main.tf")
        )
    )
    self.assertTrue(
        os.path.isfile(
            os.path.join(
                existing_app,
                ".github",
                "terraform",
                "modules",
                "wif",
                "iam.tf",
            )
        )
    )

    # 2. Maintainer populates distro_repo WITH --include-top-level
    distro_repo = os.path.join(self.tmp_dir, "cm_distro")
    _init_git_repo(distro_repo, initial_commit=False)
    res1 = self._run_distribute(
        "--target",
        distro_repo,
        "--include-top-level",
        source_repo=src_clone,
    )
    self.assertEqual(res1.returncode, 0, msg=res1.stderr)
    for top_file in ("LICENSE", ".gitignore", "README.md"):
      self.assertTrue(
          os.path.isfile(os.path.join(distro_repo, top_file)),
          f"Expected top-level file {top_file} in distro_repo",
      )
    self.assertFalse(os.path.exists(os.path.join(distro_repo, "scripts")))
    self.assertFalse(os.path.exists(os.path.join(distro_repo, "docs")))

    # 3. Maintainer updates dist/README.md in C2 and runs repeat sync WITHOUT passing --include-top-level;
    #    distro_repo previously synced top-level files so README.md must be preserved and updated.
    src_readme = os.path.join(src_clone, "dist", "README.md")
    with open(src_readme, "a", encoding="utf-8") as f:
      f.write("\n<!-- C2_DIST_README_UPDATE -->\n")
    _run_git(src_clone, "add", "dist/README.md")
    _run_git(src_clone, "commit", "-q", "-m", "Update dist/README.md in C2")
    c2_sha = _run_git(src_clone, "rev-parse", "HEAD").stdout.strip()

    res2 = self._run_distribute(
        "--target",
        distro_repo,
        "--source-ref",
        c2_sha,
        source_repo=src_clone,
    )
    self.assertEqual(res2.returncode, 0, msg=res2.stderr)
    with open(os.path.join(distro_repo, "README.md"), "r", encoding="utf-8") as f:
      self.assertIn("<!-- C2_DIST_README_UPDATE -->", f.read())


if __name__ == "__main__":
  unittest.main()
