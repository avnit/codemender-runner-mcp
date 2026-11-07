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

output "nat_ip" {
  description = "Static IP address allocated for Cloud NAT (if VPC/NAT was created)."
  value       = var.create_vpc_and_nat ? google_compute_address.nat_ip[0].address : null
}

output "region" {
  description = "GCP Region for all resources."
  value       = var.region
}

output "reports_bucket_url" {
  description = "GCS bucket URL for scan reports."
  value       = google_storage_bucket.reports.url
}

output "reports_bucket_name" {
  description = "GCS bucket name for scan reports."
  value       = google_storage_bucket.reports.name
}


output "runner_job_name" {
  description = "Cloud Run Job name."
  value       = google_cloud_run_v2_job.runner.name
}

# Governing: SPEC-WIF, REQ-0005
output "runner_service_account_email" {
  description = "Email address of the Cloud Run orchestrator/runner Service Account."
  value       = google_service_account.runner_sa.email
}

output "artifact_registry_repository" {
  description = "Artifact Registry Docker repository path."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.docker_repo.repository_id}"
}

output "cloud_run_artifact_registry_repository" {
  description = "Artifact Registry Docker repository path for Cloud Run Job images."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.docker_repo.repository_id}"
}

output "runner_image_uri" {
  description = "Deterministic Artifact Registry image URI for the Cloud Run Job."
  value       = local.runner_image_uri
}

output "cloud_run_image_uri" {
  description = "Deterministic Artifact Registry image URI for the Cloud Run Job."
  value       = local.runner_image_uri
}

output "next_step_cloud_build_command" {
  description = "Standalone Cloud Build command to manually rebuild and push the Cloud Run Job container image."
  value       = "gcloud builds submit --config=cloudbuild.yaml --project=${var.project_id} --substitutions=_REGION=${var.region},_REPO_NAME=${google_artifact_registry_repository.docker_repo.repository_id},_IMAGE_TAG=${local.runner_image_hash} ."
}

output "artifact_registry_repository_id" {
  description = "Artifact Registry repository ID (used for the MCP server image)."
  value       = google_artifact_registry_repository.docker_repo.repository_id
}
