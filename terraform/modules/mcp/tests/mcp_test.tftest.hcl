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

mock_provider "google" {}

variables {
  project_id                   = "test-project"
  region                       = "us-central1"
  resource_prefix              = "codemender"
  runner_job_name              = "codemender-runner"
  reports_bucket_name          = "codemender-reports-test-project"
  artifact_registry_repository = "codemender-runner"
  auto_build_image             = false
  github_token_secret_id       = "gh-token"
  auth_token_secret_id         = "mcp-token"
}

run "service_iam_and_secrets" {
  command = plan

  assert {
    condition     = length(google_secret_manager_secret_iam_member.mcp_secret_access) == 2
    error_message = "Expected secret access for the GitHub and auth token secrets."
  }

  assert {
    condition     = google_cloud_run_v2_job_iam_member.mcp_job_executor.role == "roles/run.jobsExecutorWithOverrides"
    error_message = "MCP SA must be able to execute the runner job with overrides."
  }

  assert {
    condition     = google_cloud_run_v2_job_iam_member.mcp_job_executor.name == "codemender-runner"
    error_message = "Job execution permission must be scoped to the runner job."
  }

  assert {
    condition     = length(google_cloud_run_v2_service_iam_member.public_invoker) == 0
    error_message = "The MCP service must not be public by default."
  }

  assert {
    condition     = google_cloud_run_v2_service.mcp.template[0].containers[0].resources[0].cpu_idle == false
    error_message = "CPU must stay allocated so background job watchers keep running."
  }

  assert {
    condition     = google_cloud_run_v2_service.mcp.template[0].containers[0].image == "us-central1-docker.pkg.dev/test-project/codemender-runner/codemender-mcp:latest"
    error_message = "Unexpected MCP image URI."
  }

  assert {
    condition     = google_bigquery_dataset.codemender.dataset_id == "codemender"
    error_message = "Unexpected BigQuery dataset ID."
  }
}

run "public_requires_opt_in" {
  command = plan

  variables {
    allow_unauthenticated = true
  }

  assert {
    condition     = length(google_cloud_run_v2_service_iam_member.public_invoker) == 1
    error_message = "allow_unauthenticated should grant allUsers run.invoker."
  }
}

run "rejects_bad_patch_mode" {
  command = plan

  variables {
    default_patch_mode = "direct"
  }

  expect_failures = [var.default_patch_mode]
}
