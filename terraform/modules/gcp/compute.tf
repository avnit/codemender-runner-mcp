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

# Governing: docs/openspec/specs/wif/spec.md#req-0005, docs/openspec/specs/wif/spec.md#req-0007

locals {
  repo_root = "${path.module}/../../.."
  codemender_source_files = sort([
    for f in fileset("${local.repo_root}/packages/codemender", "**") :
    f if !strcontains(f, "__pycache__/") &&
    !strcontains(f, ".egg-info/") &&
    !startswith(f, "build/") &&
    !strcontains(f, "/testdata/") &&
    !endswith(f, ".pyc") &&
    !startswith(basename(f), "test_") &&
    !endswith(basename(f), "_test.py")
  ])
  runner_image_hash = substr(
    sha256(
      join("", concat(
        [
          filesha256("${local.repo_root}/docker/runner.Dockerfile"),
          filesha256("${local.repo_root}/docker/cloudrun.Dockerfile"),
          filesha256("${local.repo_root}/cloud-run-jobs/entrypoint.py"),
          filesha256("${local.repo_root}/cloudbuild.yaml"),
        ],
        [
          for f in local.codemender_source_files :
          "${f}:${filesha256("${local.repo_root}/packages/codemender/${f}")}"
        ]
      ))
    ),
    0,
    12
  )
  runner_image_uri = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.docker_repo.repository_id}/codemender-runner:${local.runner_image_hash}"
}

resource "time_sleep" "wait_for_apis_and_iam" {
  create_duration = "60s"

  depends_on = [
    google_project_service.enabled_services,
    google_artifact_registry_repository.docker_repo,
    google_artifact_registry_repository_iam_member.cloudbuild_ar_writer,
    google_project_iam_member.cloudbuild_storage_viewer,
    google_project_iam_member.cloudbuild_log_writer,
    google_storage_bucket_iam_member.runner_reports_admin,
    google_project_iam_member.service_accounts_log_writer,
    google_project_iam_member.runner_aiplatform_user,
  ]
}

# Cloud Run validates that the container image exists in Artifact Registry at
# Job creation time. Building and pushing the content-hashed image here in the
# dependency graph (after Artifact Registry + Cloud Build IAM, before the Cloud
# Run Job) avoids dummy placeholder images and `ignore_changes` drift masking.
resource "terraform_data" "cloud_build_runner_image" {
  count = var.auto_build_image ? 1 : 0

  triggers_replace = [
    google_artifact_registry_repository.docker_repo.id,
    local.runner_image_hash,
  ]

  provisioner "local-exec" {
    working_dir = local.repo_root
    command     = "gcloud artifacts docker images describe ${local.runner_image_uri} --project=${var.project_id} >/dev/null 2>&1 || gcloud builds submit --config=cloudbuild.yaml --project=${var.project_id} --substitutions=_REGION=${var.region},_REPO_NAME=${google_artifact_registry_repository.docker_repo.repository_id},_IMAGE_TAG=${local.runner_image_hash} ."
  }

  depends_on = [
    time_sleep.wait_for_apis_and_iam,
    google_artifact_registry_repository.docker_repo,
    google_artifact_registry_repository_iam_member.cloudbuild_ar_writer,
    google_project_iam_member.cloudbuild_storage_viewer,
    google_project_iam_member.cloudbuild_log_writer,
  ]
}

resource "google_cloud_run_v2_job" "runner" {
  name                = "${var.resource_prefix}-runner"
  location            = var.region
  project             = var.project_id
  deletion_protection = false

  template {
    template {
      timeout               = var.runner_timeout
      max_retries           = 0
      execution_environment = "EXECUTION_ENVIRONMENT_GEN2"
      service_account       = google_service_account.runner_sa.email

      containers {
        image = local.runner_image_uri

        resources {
          limits = {
            cpu    = var.runner_cpu
            memory = var.runner_memory
          }
        }

        env {
          name  = "EPHEMERAL_SCRATCH_DIR"
          value = "/workspace"
        }
      }

      dynamic "vpc_access" {
        for_each = local.use_vpc_access ? [1] : []
        content {
          connector = local.vpc_connector_id
          egress    = "ALL_TRAFFIC"
        }
      }
    }
  }

  depends_on = [
    time_sleep.wait_for_apis_and_iam,
    terraform_data.cloud_build_runner_image,
  ]
}
