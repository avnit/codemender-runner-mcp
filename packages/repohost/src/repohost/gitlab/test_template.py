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

# Governing: docs/openspec/specs/gitlab-ci/spec.md#req-0008, docs/openspec/specs/gitlab-ci-fix/spec.md#req-0001, SPEC-GITLAB-CI, SPEC-GITLAB-CI-FIX, ADR-0008
"""Unit tests for docker/gitlab.Dockerfile and templates/codemender.yml."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re
from typing import Any
import unittest

import yaml

REPO_ROOT = Path(__file__).resolve().parents[5]
DOCKERFILE_PATH = REPO_ROOT / "docker" / "gitlab.Dockerfile"
TEMPLATE_PATH = REPO_ROOT / "templates" / "codemender.yml"

DIGEST_PINNED_IMAGE_RE = re.compile(r"^[^\s]+@sha256:[0-9a-f]{64}$")
RULE_EXPR_RE = re.compile(
    r"^\$([A-Z0-9_]+)\s*(==|!=)\s*(?:\"([^\"]*)\"|\$([A-Z0-9_]+))$"
)


def _load_template_documents() -> tuple[dict[str, Any], dict[str, Any]]:
  """Loads the spec header and job definitions from templates/codemender.yml."""
  raw = TEMPLATE_PATH.read_text(encoding="utf-8")
  docs = list(yaml.safe_load_all(raw))
  if len(docs) != 2:
    raise AssertionError(
        f"Expected 2 YAML documents in {TEMPLATE_PATH}, found {len(docs)}"
    )
  header_doc, jobs_doc = docs
  if not isinstance(header_doc, dict) or not isinstance(jobs_doc, dict):
    raise AssertionError(
        "Both YAML documents in templates/codemender.yml must be mappings"
    )
  return header_doc, jobs_doc


def evaluate_gitlab_rules(
    job: Mapping[str, Any], env: Mapping[str, str]
) -> dict[str, Any]:
  """Simulates GitLab CI rule evaluation for a job definition and CI env."""
  effective_env: dict[str, str] = {}
  job_vars = job.get("variables")
  if isinstance(job_vars, Mapping):
    for key, value in job_vars.items():
      effective_env[str(key)] = str(value)
  effective_env.update(env)

  rules = job.get("rules", [])
  for rule in rules:
    condition = rule.get("if")
    if condition is not None:
      match = RULE_EXPR_RE.match(condition.strip())
      if not match:
        raise ValueError(f"Unsupported GitLab rule condition: {condition!r}")
      lhs_var, operator, rhs_literal, rhs_var = match.groups()
      lhs_val = effective_env.get(lhs_var, "")
      rhs_val = (
          rhs_literal
          if rhs_literal is not None
          else effective_env.get(rhs_var, "")
      )
      matched = (
          (lhs_val == rhs_val) if operator == "==" else (lhs_val != rhs_val)
      )
      if not matched:
        continue

    when = rule.get("when", job.get("when", "on_success"))
    allow_failure = rule.get(
        "allow_failure", job.get("allow_failure", False)
    )
    return {
        "triggered": when != "never",
        "when": when,
        "allow_failure": allow_failure,
    }

  return {
      "triggered": False,
      "when": "never",
      "allow_failure": False,
  }


class TestGitlabDockerfile(unittest.TestCase):
  """Tests for REQ-GL-0221 slim docker/gitlab.Dockerfile contract."""

  def test_dockerfile_exists_and_has_governing_header(self) -> None:
    self.assertTrue(
        DOCKERFILE_PATH.is_file(),
        f"Missing Dockerfile at {DOCKERFILE_PATH}",
    )
    content = DOCKERFILE_PATH.read_text(encoding="utf-8")
    self.assertIn("Copyright 2026 Google LLC", content)
    self.assertIn("Licensed under the Apache License, Version 2.0", content)
    self.assertIn("docs/openspec/specs/gitlab-ci/spec.md#req-0008", content)
    self.assertIn("docs/openspec/specs/gitlab-ci-fix/spec.md#req-0001", content)
    self.assertIn("SPEC-GITLAB-CI", content)
    self.assertIn("SPEC-GITLAB-CI-FIX", content)
    self.assertIn("ADR-0008", content)

  def test_dockerfile_uses_digest_pinned_slim_python_base_image(self) -> None:
    content = DOCKERFILE_PATH.read_text(encoding="utf-8")
    from_lines = [
        line.strip()
        for line in content.splitlines()
        if line.strip().upper().startswith("FROM ")
    ]
    self.assertEqual(
        len(from_lines), 1, "Expected exactly one FROM instruction"
    )
    base_image = from_lines[0].split()[1]
    self.assertIn("python:3.11-slim", base_image)
    self.assertRegex(base_image, DIGEST_PINNED_IMAGE_RE)

  def test_dockerfile_installs_git_and_ca_certificates_without_cm_or_runsc(
      self,
  ) -> None:
    content = DOCKERFILE_PATH.read_text(encoding="utf-8")
    self.assertIn("apt-get", content)
    self.assertIn("--no-install-recommends", content)
    self.assertIn("ca-certificates", content)
    self.assertIn("git", content)
    self.assertIn("/var/lib/apt/lists/*", content)
    self.assertNotRegex(content, r"/usr/local/bin/cm(?![-\w])")
    self.assertNotIn("cm-linux-amd64", content)
    self.assertNotIn("runsc", content)

  def test_dockerfile_sets_isolated_virtualenv_and_packages(self) -> None:
    content = DOCKERFILE_PATH.read_text(encoding="utf-8")
    self.assertIn("python3 -m venv /opt/codemender/venv", content)
    self.assertIn("VIRTUAL_ENV=/opt/codemender/venv", content)
    self.assertIn(
        'PATH="/usr/local/bin:/opt/codemender/venv/bin:${PATH}"', content
    )
    self.assertIn("PYTHONSAFEPATH=1", content)
    self.assertIn("PYTHONNOUSERSITE=1", content)
    self.assertIn("PYTHONDONTWRITEBYTECODE=1", content)
    self.assertIn("PYTHONUNBUFFERED=1", content)
    self.assertIn("--no-cache-dir", content)
    self.assertIn("./packages/artifactstore", content)
    self.assertIn("./packages/codemender", content)
    self.assertIn("./packages/launcher[cloudrun]", content)
    self.assertIn("./packages/repohost[gitlab]", content)

  def test_dockerfile_creates_isolated_cm_gitlab_entrypoint(self) -> None:
    content = DOCKERFILE_PATH.read_text(encoding="utf-8")
    self.assertIn("/usr/local/bin/cm-gitlab", content)
    self.assertIn("chmod 0755 /usr/local/bin/cm-gitlab", content)
    self.assertIn(
        'exec /opt/codemender/venv/bin/python3 -I -B -u -m repohost.gitlab "$@"',
        content,
    )
    self.assertIn('ENTRYPOINT ["/usr/local/bin/cm-gitlab"]', content)


class TestCodemenderTemplateHeaderAndScan(unittest.TestCase):
  """Tests for REQ-GL-0222 component inputs and codemender-scan job."""

  def test_template_exists_and_has_governing_header(self) -> None:
    self.assertTrue(
        TEMPLATE_PATH.is_file(),
        f"Missing GitLab CI template at {TEMPLATE_PATH}",
    )
    content = TEMPLATE_PATH.read_text(encoding="utf-8")
    self.assertIn("Copyright 2026 Google LLC", content)
    self.assertIn("Licensed under the Apache License, Version 2.0", content)
    self.assertIn("docs/openspec/specs/gitlab-ci/spec.md#req-0008", content)
    self.assertIn("docs/openspec/specs/gitlab-ci-fix/spec.md#req-0001", content)
    self.assertIn("SPEC-GITLAB-CI", content)
    self.assertIn("SPEC-GITLAB-CI-FIX", content)
    self.assertIn("ADR-0008", content)

  def test_component_spec_inputs(self) -> None:
    header_doc, _ = _load_template_documents()
    self.assertIn("spec", header_doc)
    inputs = header_doc["spec"].get("inputs", {})
    self.assertEqual(set(inputs.keys()), {"image", "stage", "git_depth"})

    self.assertNotIn("default", inputs["image"])
    self.assertEqual(
        inputs["image"]["regex"], r"^[^\s@]+@sha256:[0-9a-f]{64}$"
    )
    self.assertEqual(inputs["stage"]["default"], "test")
    self.assertEqual(inputs["git_depth"]["default"], "1")
    self.assertEqual(inputs["git_depth"]["regex"], r"^[0-9]+$")

  def test_codemender_scan_job_contract(self) -> None:
    _, jobs_doc = _load_template_documents()
    self.assertIn("codemender-scan", jobs_doc)
    scan = jobs_doc["codemender-scan"]

    self.assertEqual(scan["stage"], "$[[ inputs.stage ]]")
    self.assertEqual(
        scan["image"],
        {"name": "$[[ inputs.image ]]", "entrypoint": [""]},
    )
    self.assertNotIn("tags", scan)
    self.assertEqual(scan["inherit"], {"default": False})
    self.assertEqual(scan["needs"], [])
    self.assertEqual(scan["dependencies"], [])
    self.assertIs(scan["interruptible"], True)
    self.assertEqual(scan["timeout"], "60 minutes")
    self.assertEqual(
        scan["id_tokens"],
        {
            "GCP_OIDC_TOKEN": {
                "aud": "https://iam.googleapis.com/${GCP_WIF_PROVIDER}",
            }
        },
    )
    self.assertEqual(
        scan["variables"],
        {
            "GIT_STRATEGY": "clone",
            "GIT_SUBMODULE_STRATEGY": "none",
            "GIT_DEPTH": "$[[ inputs.git_depth ]]",
        },
    )
    self.assertEqual(scan["script"], ["/usr/local/bin/cm-gitlab find"])
    self.assertEqual(
        scan["artifacts"],
        {
            "access": "developer",
            "when": "always",
            "expire_in": "7 days",
            "paths": ["report.sarif", "report.md", "cm-logs/"],
            "reports": {"sarif": "report.sarif"},
        },
    )


class TestCodemenderVerifyJob(unittest.TestCase):
  """Tests for REQ-GL-0223 codemender-verify manual job contract."""

  def test_codemender_verify_job_contract(self) -> None:
    _, jobs_doc = _load_template_documents()
    self.assertIn("codemender-verify", jobs_doc)
    verify = jobs_doc["codemender-verify"]

    self.assertEqual(verify["stage"], "$[[ inputs.stage ]]")
    self.assertEqual(
        verify["image"],
        {"name": "$[[ inputs.image ]]", "entrypoint": [""]},
    )
    self.assertNotIn("tags", verify)
    self.assertEqual(verify["inherit"], {"default": False})
    self.assertEqual(verify["when"], "manual")
    self.assertIs(verify["allow_failure"], True)
    self.assertIs(verify["interruptible"], True)
    self.assertEqual(verify["timeout"], "60 minutes")
    self.assertEqual(
        verify["needs"],
        [{"job": "codemender-scan", "artifacts": False}],
    )
    self.assertEqual(verify["dependencies"], [])
    self.assertEqual(
        verify["id_tokens"],
        {
            "GCP_OIDC_TOKEN": {
                "aud": "https://iam.googleapis.com/${GCP_WIF_PROVIDER}",
            }
        },
    )
    self.assertEqual(
        verify["variables"],
        {
            "GIT_STRATEGY": "none",
            "GIT_SUBMODULE_STRATEGY": "none",
        },
    )
    self.assertEqual(
        verify["script"],
        [
            "/usr/local/bin/cm-gitlab verify --findings"
            ' "${CODEMENDER_VERIFY_FINDING:-all}"'
        ],
    )
    self.assertEqual(
        verify["artifacts"],
        {
            "access": "developer",
            "when": "always",
            "expire_in": "7 days",
            "paths": ["cm-logs/"],
        },
    )


class TestCodemenderRules(unittest.TestCase):
  """Tests for REQ-GL-0224 MR pipeline gating rules on scan and verify."""

  def _valid_mr_env(self, **overrides: str) -> dict[str, str]:
    env = {
        "CI_PIPELINE_SOURCE": "merge_request_event",
        "CI_MERGE_REQUEST_EVENT_TYPE": "merged_result",
        "CI_MERGE_REQUEST_SOURCE_PROJECT_ID": "100",
        "CI_MERGE_REQUEST_PROJECT_ID": "100",
    }
    env.update(overrides)
    return env

  def test_rules_structure_on_scan_and_verify(self) -> None:
    _, jobs_doc = _load_template_documents()
    scan_rules = jobs_doc["codemender-scan"]["rules"]
    verify_rules = jobs_doc["codemender-verify"]["rules"]

    expected_never_rules = [
        {"if": '$CODEMENDER_SCAN_ON_MR == "false"', "when": "never"},
        {"if": '$CI_MERGE_REQUEST_EVENT_TYPE == "merge_train"', "when": "never"},
        {
            "if": (
                "$CI_MERGE_REQUEST_SOURCE_PROJECT_ID !="
                " $CI_MERGE_REQUEST_PROJECT_ID"
            ),
            "when": "never",
        },
    ]
    self.assertEqual(scan_rules[:3], expected_never_rules)
    self.assertEqual(verify_rules[:3], expected_never_rules)
    self.assertEqual(
        scan_rules[3],
        {"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'},
    )
    self.assertEqual(
        verify_rules[3],
        {
            "if": '$CI_PIPELINE_SOURCE == "merge_request_event"',
            "when": "manual",
            "allow_failure": True,
        },
    )

  def test_valid_mr_pipeline_triggers_scan_and_manual_verify(self) -> None:
    _, jobs_doc = _load_template_documents()
    env = self._valid_mr_env()

    scan_eval = evaluate_gitlab_rules(jobs_doc["codemender-scan"], env)
    self.assertTrue(scan_eval["triggered"])
    self.assertEqual(scan_eval["when"], "on_success")
    self.assertFalse(scan_eval["allow_failure"])

    verify_eval = evaluate_gitlab_rules(jobs_doc["codemender-verify"], env)
    self.assertTrue(verify_eval["triggered"])
    self.assertEqual(verify_eval["when"], "manual")
    self.assertTrue(verify_eval["allow_failure"])

  def test_merge_train_pipeline_never_triggers(self) -> None:
    _, jobs_doc = _load_template_documents()
    env = self._valid_mr_env(CI_MERGE_REQUEST_EVENT_TYPE="merge_train")

    self.assertFalse(
        evaluate_gitlab_rules(jobs_doc["codemender-scan"], env)["triggered"]
    )
    self.assertFalse(
        evaluate_gitlab_rules(jobs_doc["codemender-verify"], env)["triggered"]
    )

  def test_fork_mr_pipeline_never_triggers(self) -> None:
    _, jobs_doc = _load_template_documents()
    env = self._valid_mr_env(
        CI_MERGE_REQUEST_SOURCE_PROJECT_ID="999",
        CI_MERGE_REQUEST_PROJECT_ID="100",
    )

    self.assertFalse(
        evaluate_gitlab_rules(jobs_doc["codemender-scan"], env)["triggered"]
    )
    self.assertFalse(
        evaluate_gitlab_rules(jobs_doc["codemender-verify"], env)["triggered"]
    )

  def test_scan_on_mr_false_never_triggers(self) -> None:
    _, jobs_doc = _load_template_documents()
    env = self._valid_mr_env(CODEMENDER_SCAN_ON_MR="false")

    self.assertFalse(
        evaluate_gitlab_rules(jobs_doc["codemender-scan"], env)["triggered"]
    )
    self.assertFalse(
        evaluate_gitlab_rules(jobs_doc["codemender-verify"], env)["triggered"]
    )

  def test_non_mr_pipelines_never_trigger(self) -> None:
    _, jobs_doc = _load_template_documents()
    for source in ("push", "schedule", "web", "api", "trigger"):
      with self.subTest(pipeline_source=source):
        env = self._valid_mr_env(CI_PIPELINE_SOURCE=source)
        self.assertFalse(
            evaluate_gitlab_rules(jobs_doc["codemender-scan"], env)["triggered"]
        )
        self.assertFalse(
            evaluate_gitlab_rules(jobs_doc["codemender-verify"], env)[
                "triggered"
            ]
        )


if __name__ == "__main__":
  unittest.main()
