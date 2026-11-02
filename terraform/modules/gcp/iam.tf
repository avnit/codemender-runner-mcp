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

resource "google_service_account" "runner_sa" {
  account_id   = "${var.resource_prefix}-runner-sa"
  display_name = "CodeMender Orchestrator Service Account (${var.resource_prefix})"
  project      = var.project_id
  depends_on   = [google_project_service.enabled_services["iam.googleapis.com"]]
}

# Governing: SPEC-ARTIFACT-CACHE/REQ-0005, SPEC-WIF/REQ-0004
# Bucket-level IAM for Runner SA
resource "google_storage_bucket_iam_member" "runner_reports_admin" {
  bucket = google_storage_bucket.reports.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.runner_sa.email}"
}

locals {
  log_writer_service_accounts = {
    "runner" = "serviceAccount:${google_service_account.runner_sa.email}"
  }
}

# Logging Writer IAM for Runner Service Account
resource "google_project_iam_member" "service_accounts_log_writer" {
  for_each = local.log_writer_service_accounts
  project  = var.project_id
  role     = "roles/logging.logWriter"
  member   = each.value
}

# Agent Platform / Vertex AI IAM for Runner SA (required for CodeMender LLM interactions)
resource "google_project_iam_member" "runner_aiplatform_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.runner_sa.email}"
}

# Grant Cloud Run Developer to Cloud Build SAs so they can update the Cloud Run Job image
resource "google_project_iam_member" "cloudbuild_run_developer" {
  for_each = local.cloudbuild_service_accounts
  project  = var.project_id
  role     = "roles/run.developer"
  member   = each.value
  depends_on = [
    google_project_service.enabled_services["iam.googleapis.com"],
    google_project_service.enabled_services["cloudbuild.googleapis.com"],
  ]
}

# Grant Service Account User to Cloud Build SAs on the Runner SA
resource "google_service_account_iam_member" "cloudbuild_runner_sa_user" {
  for_each           = local.cloudbuild_service_accounts
  service_account_id = google_service_account.runner_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}
