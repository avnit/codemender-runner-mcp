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

# Optional CodeMender MCP server (Cloud Run service) that drives the runner
# Cloud Run Job and stores findings in BigQuery. See packages/mcpserver/README.md.

module "mcp" {
  source = "./modules/mcp"
  count  = var.execution_mode == "cloud_run" && var.enable_mcp_server ? 1 : 0

  project_id                   = var.gcp_project_id
  region                       = var.gcp_region
  resource_prefix              = var.resource_prefix
  runner_job_name              = module.gcp[0].runner_job_name
  reports_bucket_name          = module.gcp[0].reports_bucket_name
  artifact_registry_repository = module.gcp[0].artifact_registry_repository_id
  auto_build_image             = var.auto_build_image
  image_tag                    = var.mcp_image_tag
  bq_dataset_id                = var.mcp_bq_dataset_id
  bq_location                  = var.mcp_bq_location
  github_token_secret_id       = var.mcp_github_token_secret_id
  auth_token_secret_id         = var.mcp_auth_token_secret_id
  allow_unauthenticated        = var.mcp_allow_unauthenticated
  invoker_members              = var.mcp_invoker_members
  default_patch_mode           = var.mcp_default_patch_mode
}

output "mcp_endpoint" {
  description = "Streamable-HTTP MCP endpoint (when enable_mcp_server = true)."
  value       = length(module.mcp) > 0 ? module.mcp[0].mcp_endpoint : null
}

output "mcp_bq_dataset" {
  description = "BigQuery dataset with the CodeMender jobs and findings tables."
  value       = length(module.mcp) > 0 ? module.mcp[0].bq_dataset : null
}
