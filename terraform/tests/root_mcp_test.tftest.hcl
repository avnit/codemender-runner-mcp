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
mock_provider "google-beta" {}
mock_provider "github" {}

variables {
  gcp_project_id   = "test-codemender-project"
  gcp_region       = "us-central1"
  github_owner     = "test-org"
  execution_mode   = "cloud_run"
  auto_build_image = false
}

override_resource {
  target          = module.gcp[0].google_service_account.runner_sa
  override_during = plan
  values = {
    name  = "projects/test-codemender-project/serviceAccounts/codemender-runner-sa@test-codemender-project.iam.gserviceaccount.com"
    email = "codemender-runner-sa@test-codemender-project.iam.gserviceaccount.com"
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

run "mcp_disabled_by_default" {
  command = plan

  assert {
    condition     = length(module.mcp) == 0
    error_message = "The MCP server must be opt-in."
  }

  assert {
    condition     = output.mcp_endpoint == null
    error_message = "mcp_endpoint must be null when the MCP server is disabled."
  }
}

run "mcp_enabled" {
  command = plan

  variables {
    enable_mcp_server          = true
    mcp_github_token_secret_id = "gh-token"
    mcp_auth_token_secret_id   = "mcp-token"
    mcp_invoker_members        = ["user:dev@example.com"]
  }

  override_resource {
    target          = module.mcp[0].google_service_account.mcp_sa
    override_during = plan
    values = {
      email = "codemender-mcp-sa@test-codemender-project.iam.gserviceaccount.com"
    }
  }

  assert {
    condition     = module.mcp[0].bq_dataset == "test-codemender-project.codemender"
    error_message = "Unexpected BigQuery dataset."
  }

  assert {
    condition     = output.mcp_bq_dataset == "test-codemender-project.codemender"
    error_message = "Expected the mcp_bq_dataset root output."
  }
}
