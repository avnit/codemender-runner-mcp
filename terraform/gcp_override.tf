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

locals {
  gcp_module_bucket_name = var.execution_mode == "cloud_run" ? module.gcp[0].reports_bucket_name : ""
  gcp_module_job_name    = var.execution_mode == "cloud_run" ? module.gcp[0].runner_job_name : ""
  gcp_module_runtime_sa_emails = var.execution_mode == "cloud_run" ? [
    module.gcp[0].runner_service_account_email,
  ] : []
}
