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
  description = "The GCP Project ID where resources will be deployed."
}

variable "region" {
  type        = string
  description = "The GCP region for resource deployment."
  default     = "us-central1"
}

variable "resource_prefix" {
  type        = string
  description = "Prefix used for naming provisioned GCP resources to prevent multi-deployment collisions."
  default     = "codemender"
}

variable "reports_bucket_name" {
  type        = string
  description = "Name of the GCS bucket for scan reports."
  default     = ""
}

variable "reports_bucket_ttl_days" {
  type        = number
  description = "Number of days to retain objects in the GCS reports and transit bucket before lifecycle deletion."
  default     = 7

  validation {
    condition     = var.reports_bucket_ttl_days >= 1 && floor(var.reports_bucket_ttl_days) == var.reports_bucket_ttl_days
    error_message = "reports_bucket_ttl_days must be a whole number greater than or equal to 1."
  }
}


variable "runner_cpu" {
  type        = string
  description = "CPU limit for Cloud Run Job worker tasks (e.g. '1', '2', '4', '8')."
  default     = "2"
}

variable "runner_memory" {
  type        = string
  description = "Memory limit for Cloud Run Job worker tasks (e.g. '2Gi', '4Gi', '8Gi', '16Gi')."
  default     = "8Gi"
}

variable "runner_timeout" {
  type        = string
  description = "Task timeout for Cloud Run Job worker tasks (e.g. '600s', '1800s', '3600s')."
  default     = "3600s"
}

variable "create_vpc_and_nat" {
  type        = bool
  description = "Whether to create a dedicated VPC network, subnet, connector, and Cloud NAT for private egress."
  default     = false
}

variable "existing_vpc_connector_id" {
  type        = string
  description = "ID of an existing Serverless VPC Access Connector if create_vpc_and_nat is false."
  default     = null
}

variable "vpc_connector_cidr" {
  type        = string
  description = "CIDR range (/28 or /26) for the Serverless VPC Access Connector."
  default     = "10.0.0.0/26"

  validation {
    condition     = can(regex("^([0-9]{1,3}\\.){3}[0-9]{1,3}/([0-9]|[1-2][0-9]|3[0-2])$", var.vpc_connector_cidr))
    error_message = "vpc_connector_cidr must be a valid IPv4 CIDR string (e.g., 10.0.0.0/26)."
  }
}

variable "vpc_connector_min_instances" {
  type        = number
  description = "Minimum number of instances for the Serverless VPC Access Connector."
  default     = 2
}

variable "vpc_connector_max_instances" {
  type        = number
  description = "Maximum number of instances for the Serverless VPC Access Connector."
  default     = 3
}

variable "vpc_connector_machine_type" {
  type        = string
  description = "Machine type for the Serverless VPC Access Connector."
  default     = "e2-micro"
}

variable "auto_build_image" {
  type        = bool
  description = "Whether Terraform should automatically build and push the Cloud Run Job container image via Cloud Build (cloudbuild.yaml) before creating or updating the Cloud Run Job."
  default     = true
}
