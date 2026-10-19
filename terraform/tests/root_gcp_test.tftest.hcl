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

# Governing: docs/openspec/specs/wif/spec.md#req-0001, docs/openspec/specs/wif/spec.md#req-0005, docs/openspec/specs/wif/spec.md#req-0007

mock_provider "google" {}
mock_provider "google-beta" {}
mock_provider "github" {}

variables {
  gcp_project_id = "test-codemender-project"
  gcp_region     = "us-central1"
  github_owner   = "test-org"
  execution_mode = "cloud_run"
}

run "cloud_run_mode_composition" {
  command = plan

  override_resource {
    target          = module.gcp[0].google_service_account.runner_sa
    override_during = plan
    values = {
      name  = "projects/test-codemender-project/serviceAccounts/codemender-runner-sa@test-codemender-project.iam.gserviceaccount.com"
      email = "codemender-runner-sa@test-codemender-project.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = module.gcp[0].google_service_account.worker_sa
    override_during = plan
    values = {
      name  = "projects/test-codemender-project/serviceAccounts/codemender-worker-sa@test-codemender-project.iam.gserviceaccount.com"
      email = "codemender-worker-sa@test-codemender-project.iam.gserviceaccount.com"
    }
  }

  override_resource {
    target          = module.wif.google_service_account.runner_sa
    override_during = plan
    values = {
      name  = "projects/test-codemender-project/serviceAccounts/codemender-wif-sa@test-codemender-project.iam.gserviceaccount.com"
      email = "codemender-wif-sa@test-codemender-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = length(module.gcp) == 1
    error_message = "Expected module.gcp to have count 1 in cloud_run execution mode."
  }

  assert {
    condition     = output.execution_mode == "cloud_run"
    error_message = "Expected output.execution_mode to be 'cloud_run'."
  }

  assert {
    condition     = output.cloud_run_reports_bucket_name != null
    error_message = "Expected cloud_run_reports_bucket_name output to be non-null in cloud_run mode."
  }

  assert {
    condition     = output.cloud_run_job_name != null
    error_message = "Expected cloud_run_job_name output to be non-null in cloud_run mode."
  }

  assert {
    condition     = var.runner_memory == "8Gi"
    error_message = "Expected default runner_memory in root module to be '8Gi'."
  }

  assert {
    condition     = var.runner_timeout == "3600s"
    error_message = "Expected default runner_timeout in root module to be '3600s'."
  }
}

