// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

# Unit tests for GCP IAM and Service Accounts
mock_provider "google" {}

override_resource {
  target          = google_service_account.runner_sa
  override_during = plan
  values = {
    name  = "projects/test-project-123/serviceAccounts/test-iam-runner-sa@test-project-123.iam.gserviceaccount.com"
    email = "test-iam-runner-sa@test-project-123.iam.gserviceaccount.com"
  }
}

variables {
  project_id      = "test-project-123"
  region          = "us-central1"
  resource_prefix = "test-iam"
}

run "iam_resources_created_correctly" {
  command = plan

  # Governing: SPEC-WIF, REQ-0005
  assert {
    condition     = google_service_account.runner_sa.account_id == "test-iam-runner-sa"
    error_message = "Runner Service Account ID does not match expected prefix."
  }

  assert {
    condition     = output.runner_service_account_email == "test-iam-runner-sa@test-project-123.iam.gserviceaccount.com"
    error_message = "runner_service_account_email output does not match expected runner SA email."
  }

  assert {
    condition     = google_storage_bucket_iam_member.runner_reports_admin.role == "roles/storage.objectUser" && google_storage_bucket_iam_member.runner_reports_admin.member == "serviceAccount:test-iam-runner-sa@test-project-123.iam.gserviceaccount.com"
    error_message = "Runner SA must be granted roles/storage.objectUser on the transit/reports bucket."
  }

  assert {
    condition     = google_project_iam_member.runner_aiplatform_user.role == "roles/aiplatform.user" && google_project_iam_member.runner_aiplatform_user.member == "serviceAccount:test-iam-runner-sa@test-project-123.iam.gserviceaccount.com"
    error_message = "Runner SA must be granted roles/aiplatform.user for Vertex AI interactions."
  }
}

