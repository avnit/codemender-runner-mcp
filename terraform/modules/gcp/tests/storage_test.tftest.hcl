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

# Unit tests for GCP Storage and Artifact Registry
mock_provider "google" {}

variables {
  project_id      = "test-project-123"
  region          = "us-central1"
  resource_prefix = "test-storage"
}

run "storage_resources_default_names" {
  command = plan

  variables {
    reports_bucket_name = ""
  }

  assert {
    condition     = google_storage_bucket.reports.name == "test-storage-reports-test-project-123"
    error_message = "Reports bucket default name does not match expected prefix pattern."
  }

  assert {
    condition     = google_artifact_registry_repository.docker_repo.repository_id == "test-storage-runner"
    error_message = "Artifact Registry repository ID does not match expected prefix pattern."
  }

  assert {
    condition     = google_storage_bucket.reports.uniform_bucket_level_access == true
    error_message = "Reports/transit bucket must enforce uniform_bucket_level_access."
  }

  assert {
    condition     = google_storage_bucket.reports.public_access_prevention == "enforced"
    error_message = "Reports/transit bucket must enforce public_access_prevention."
  }

  assert {
    condition     = tolist(google_storage_bucket.reports.versioning)[0].enabled == false
    error_message = "Reports/transit bucket must disable object versioning for immediate transit expiration."
  }

  assert {
    condition     = tolist(google_storage_bucket.reports.soft_delete_policy)[0].retention_duration_seconds == 0
    error_message = "Reports/transit bucket must set soft_delete_policy retention_duration_seconds to 0."
  }

  assert {
    condition     = tolist(tolist(google_storage_bucket.reports.lifecycle_rule)[0].condition)[0].age == 7 && tolist(tolist(google_storage_bucket.reports.lifecycle_rule)[0].action)[0].type == "Delete"
    error_message = "Reports/transit bucket lifecycle rule must enforce a 7-day Delete TTL by default."
  }

  assert {
    condition     = tolist(tolist(google_storage_bucket.reports.lifecycle_rule)[1].condition)[0].age == 1 && tolist(tolist(google_storage_bucket.reports.lifecycle_rule)[1].action)[0].type == "AbortIncompleteMultipartUpload"
    error_message = "Reports/transit bucket must abort incomplete multipart uploads after 1 day."
  }
}

run "storage_resources_custom_names" {
  command = plan

  variables {
    reports_bucket_name     = "custom-reports-123"
    reports_bucket_ttl_days = 14
  }

  assert {
    condition     = google_storage_bucket.reports.name == "custom-reports-123"
    error_message = "Reports bucket should use provided variable when specified."
  }

  assert {
    condition     = tolist(tolist(google_storage_bucket.reports.lifecycle_rule)[0].condition)[0].age == 14
    error_message = "Reports/transit bucket lifecycle rule should use custom reports_bucket_ttl_days when specified."
  }
}

run "storage_iam_bindings" {
  command = plan

  assert {
    condition     = length(google_project_iam_member.cloudbuild_run_developer) > 0
    error_message = "Cloud Build SAs must be granted run.developer role on the project."
  }

  assert {
    condition     = length(google_service_account_iam_member.cloudbuild_runner_sa_user) > 0
    error_message = "Cloud Build SAs must be granted iam.serviceAccountUser role on the runner SA."
  }
}
