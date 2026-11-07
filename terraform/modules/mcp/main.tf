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

# CodeMender MCP server: a Cloud Run *service* that launches a new execution of
# the sandboxed runner Cloud Run *Job* for every find / validate / fix call and
# records results in BigQuery.

terraform {
  required_providers {
    google = {
      source = "hashicorp/google"
    }
  }
}

locals {
  repo_root = "${path.module}/../../.."
  image_uri = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_registry_repository}/codemender-mcp:${var.image_tag}"
  secrets = merge(
    var.github_token_secret_id == null ? {} : { GITHUB_TOKEN = var.github_token_secret_id },
    var.auth_token_secret_id == null ? {} : { CM_MCP_AUTH_TOKEN = var.auth_token_secret_id },
  )
}

resource "google_project_service" "mcp_services" {
  for_each = toset([
    "bigquery.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
  ])
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "mcp_sa" {
  project      = var.project_id
  account_id   = "${var.resource_prefix}-mcp-sa"
  display_name = "CodeMender MCP server (${var.resource_prefix})"
}

# --- BigQuery ---------------------------------------------------------------
# Tables (`jobs`, `findings`) are created by the server at startup so the
# schema lives next to the code (packages/mcpserver/src/cm_mcp/store.py).
resource "google_bigquery_dataset" "codemender" {
  project                    = var.project_id
  dataset_id                 = var.bq_dataset_id
  location                   = var.bq_location
  description                = "CodeMender findings, verification verdicts and patches (written by the MCP server)."
  delete_contents_on_destroy = false

  depends_on = [google_project_service.mcp_services]
}

resource "google_bigquery_dataset_iam_member" "mcp_data_editor" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.codemender.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.mcp_sa.email}"
}

resource "google_project_iam_member" "mcp_bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.mcp_sa.email}"
}

# --- Runner job + transit bucket ---------------------------------------------
resource "google_storage_bucket_iam_member" "mcp_transit" {
  bucket = var.reports_bucket_name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.mcp_sa.email}"
}

# run.jobs.runWithOverrides + reading executions of the runner job only.
resource "google_cloud_run_v2_job_iam_member" "mcp_job_executor" {
  project  = var.project_id
  location = var.region
  name     = var.runner_job_name
  role     = "roles/run.jobsExecutorWithOverrides"
  member   = "serviceAccount:${google_service_account.mcp_sa.email}"
}

resource "google_project_iam_member" "mcp_run_viewer" {
  project = var.project_id
  role    = "roles/run.viewer"
  member  = "serviceAccount:${google_service_account.mcp_sa.email}"
}

resource "google_project_iam_member" "mcp_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.mcp_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "mcp_secret_access" {
  for_each  = local.secrets
  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.mcp_sa.email}"

  depends_on = [google_project_service.mcp_services]
}

# --- Image -------------------------------------------------------------------
resource "terraform_data" "cloud_build_mcp_image" {
  count            = var.auto_build_image ? 1 : 0
  triggers_replace = [local.image_uri]

  provisioner "local-exec" {
    working_dir = local.repo_root
    command     = "gcloud artifacts docker images describe ${local.image_uri} --project=${var.project_id} >/dev/null 2>&1 || gcloud builds submit --config=cloudbuild.mcp.yaml --project=${var.project_id} --substitutions=_REGION=${var.region},_REPO_NAME=${var.artifact_registry_repository},_IMAGE_TAG=${var.image_tag} ."
  }
}

# --- Service -----------------------------------------------------------------
resource "google_cloud_run_v2_service" "mcp" {
  project             = var.project_id
  name                = "${var.resource_prefix}-mcp"
  location            = var.region
  deletion_protection = false
  ingress             = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = google_service_account.mcp_sa.email
    timeout                          = "3600s"
    max_instance_request_concurrency = 40

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = local.image_uri

      resources {
        limits = {
          cpu    = "1"
          memory = "2Gi"
        }
        # Keep CPU allocated outside requests: job watchers and repo staging
        # run in background threads after the tool call has returned.
        cpu_idle          = false
        startup_cpu_boost = true
      }

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "CLOUD_RUN_REGION"
        value = var.region
      }
      env {
        name  = "CLOUD_RUN_JOB_NAME"
        value = var.runner_job_name
      }
      env {
        name  = "GCS_TRANSIT_BUCKET"
        value = var.reports_bucket_name
      }
      env {
        name  = "BQ_DATASET"
        value = google_bigquery_dataset.codemender.dataset_id
      }
      env {
        name  = "BQ_LOCATION"
        value = var.bq_location
      }
      env {
        name  = "CM_MCP_DEFAULT_PATCH_MODE"
        value = var.default_patch_mode
      }

      dynamic "env" {
        for_each = local.secrets
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
        period_seconds    = 5
        failure_threshold = 12
      }
    }
  }

  depends_on = [
    google_project_service.mcp_services,
    google_secret_manager_secret_iam_member.mcp_secret_access,
    google_bigquery_dataset_iam_member.mcp_data_editor,
    google_project_iam_member.mcp_bq_job_user,
    terraform_data.cloud_build_mcp_image,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  count    = var.allow_unauthenticated ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.mcp.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "invokers" {
  for_each = toset(var.invoker_members)
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.mcp.name
  role     = "roles/run.invoker"
  member   = each.value
}
