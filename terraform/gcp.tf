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

# Governing: docs/openspec/specs/wif/spec.md#req-0001, docs/openspec/specs/wif/spec.md#req-0005

module "gcp" {
  source = "./modules/gcp"
  count  = var.execution_mode == "cloud_run" ? 1 : 0

  project_id                  = var.gcp_project_id
  region                      = var.gcp_region
  resource_prefix             = var.resource_prefix
  reports_bucket_name         = var.reports_bucket_name
  reports_bucket_ttl_days     = var.reports_bucket_ttl_days
  runner_cpu                  = var.runner_cpu
  runner_memory               = var.runner_memory
  runner_timeout              = var.runner_timeout
  create_vpc_and_nat          = var.create_vpc_and_nat
  existing_vpc_connector_id   = var.existing_vpc_connector_id
  vpc_connector_cidr          = var.vpc_connector_cidr
  vpc_connector_min_instances = var.vpc_connector_min_instances
  vpc_connector_max_instances = var.vpc_connector_max_instances
  vpc_connector_machine_type  = var.vpc_connector_machine_type
  auto_build_image            = var.auto_build_image
}

output "cloud_run_reports_bucket_name" {
  description = "GCS bucket name for scan reports when execution_mode is 'cloud_run'."
  value       = var.execution_mode == "cloud_run" ? module.gcp[0].reports_bucket_name : null
}

output "cloud_run_job_name" {
  description = "Cloud Run Job name when execution_mode is 'cloud_run'."
  value       = var.execution_mode == "cloud_run" ? module.gcp[0].runner_job_name : null
}

output "cloud_run_artifact_registry_repository" {
  description = "Artifact Registry Docker repository path when execution_mode is 'cloud_run'."
  value       = var.execution_mode == "cloud_run" ? module.gcp[0].cloud_run_artifact_registry_repository : null
}

output "cloud_run_image_uri" {
  description = "Deterministic Artifact Registry image URI for the Cloud Run Job when execution_mode is 'cloud_run'."
  value       = var.execution_mode == "cloud_run" ? module.gcp[0].cloud_run_image_uri : null
}

output "next_step_cloud_build_command" {
  description = "Standalone Cloud Build command to manually rebuild and push the Cloud Run Job container image when execution_mode is 'cloud_run'."
  value       = var.execution_mode == "cloud_run" ? module.gcp[0].next_step_cloud_build_command : null
}
