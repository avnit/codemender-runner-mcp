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

output "service_url" {
  description = "Base URL of the MCP Cloud Run service."
  value       = google_cloud_run_v2_service.mcp.uri
}

output "mcp_endpoint" {
  description = "Streamable-HTTP MCP endpoint to register in MCP clients."
  value       = "${google_cloud_run_v2_service.mcp.uri}/mcp"
}

output "service_account_email" {
  description = "Service account the MCP server runs as."
  value       = google_service_account.mcp_sa.email
}

output "bq_dataset" {
  description = "BigQuery dataset holding the jobs and findings tables."
  value       = "${var.project_id}.${google_bigquery_dataset.codemender.dataset_id}"
}
