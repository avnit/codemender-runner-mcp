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

variable "project_id" {
  type        = string
  description = "GCP project hosting the MCP service, the runner job and BigQuery."
}

variable "region" {
  type        = string
  description = "Region for the MCP Cloud Run service (same as the runner job)."
}

variable "resource_prefix" {
  type        = string
  description = "Prefix for resource names."
}

variable "runner_job_name" {
  type        = string
  description = "Name of the sandboxed codemender-runner Cloud Run Job the MCP server executes."
}

variable "reports_bucket_name" {
  type        = string
  description = "GCS transit bucket shared with the runner job."
}

variable "artifact_registry_repository" {
  type        = string
  description = "Artifact Registry repository ID holding the runner and MCP images."
}

variable "image_tag" {
  type        = string
  default     = "latest"
  description = "Tag of the codemender-mcp image to deploy."
}

variable "auto_build_image" {
  type        = bool
  default     = true
  description = "Build and push the codemender-mcp image with Cloud Build if it is missing."
}

variable "bq_dataset_id" {
  type        = string
  default     = "codemender"
  description = "BigQuery dataset for the `jobs` and `findings` tables (created by the server on startup)."
}

variable "bq_location" {
  type        = string
  default     = "US"
  description = "BigQuery dataset location."
}

variable "github_token_secret_id" {
  type        = string
  default     = null
  description = "Secret Manager secret ID (in project_id) holding a GitHub token with repo scope, used to clone repos, push fix branches and open PRs. Never passed to the sandbox job."
}

variable "auth_token_secret_id" {
  type        = string
  default     = null
  description = "Optional Secret Manager secret ID holding a static bearer token required on MCP requests."
}

variable "allow_unauthenticated" {
  type        = bool
  default     = false
  description = "Grant roles/run.invoker to allUsers. Only enable together with auth_token_secret_id."
}

variable "invoker_members" {
  type        = list(string)
  default     = []
  description = "IAM members (e.g. user:alice@example.com) allowed to invoke the MCP service."
}

variable "default_patch_mode" {
  type        = string
  default     = "pr"
  description = "Default cm_fix behaviour: pr, branch or none."

  validation {
    condition     = contains(["pr", "branch", "none"], var.default_patch_mode)
    error_message = "default_patch_mode must be one of: pr, branch, none."
  }
}

variable "min_instances" {
  type        = number
  default     = 1
  description = "Minimum MCP service instances. Keep >= 1 so job watchers are not scaled to zero."
}

variable "max_instances" {
  type        = number
  default     = 3
  description = "Maximum MCP service instances."
}
