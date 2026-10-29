// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

# Unit tests for GCP Compute resources (Cloud Run and Workflows)
mock_provider "google" {}

variables {
  project_id      = "test-project-123"
  region          = "us-central1"
  resource_prefix = "test-compute"
}

run "compute_resources_created_correctly" {
  command = plan

  assert {
    condition     = google_cloud_run_v2_job.runner.name == "test-compute-runner"
    error_message = "Cloud Run job name does not match the expected resource_prefix pattern."
  }

  assert {
    condition     = google_cloud_run_v2_job.runner.location == "us-central1"
    error_message = "Cloud Run job should be deployed to the specified region."
  }

  assert {
    condition     = google_cloud_run_v2_job.runner.template[0].template[0].execution_environment == "EXECUTION_ENVIRONMENT_GEN2"
    error_message = "Runner Cloud Run Job must enforce EXECUTION_ENVIRONMENT_GEN2."
  }

  assert {
    condition     = google_cloud_run_v2_job.runner.template[0].template[0].timeout == "3600s"
    error_message = "Runner Cloud Run Job must default to 3600s task timeout."
  }

  assert {
    condition     = google_cloud_run_v2_job.runner.template[0].template[0].max_retries == 0
    error_message = "Runner Cloud Run Job must set max_retries = 0 to prevent retrying timed-out or unfixable tasks."
  }

  assert {
    condition     = google_cloud_run_v2_job.runner.template[0].template[0].containers[0].resources[0].limits["memory"] == "8Gi"
    error_message = "Runner Cloud Run Job must default to 8Gi memory limit to prevent OOM (exit 137)."
  }

  assert {
    condition     = contains([for e in google_cloud_run_v2_job.runner.template[0].template[0].containers[0].env : "${e.name}=${coalesce(e.value, "")}"], "EPHEMERAL_SCRATCH_DIR=/workspace")
    error_message = "Runner Cloud Run Job container must set EPHEMERAL_SCRATCH_DIR=/workspace."
  }

  assert {
    condition     = !contains([for e in google_cloud_run_v2_job.runner.template[0].template[0].containers[0].env : e.name], "GITHUB_APP_TOKEN")
    error_message = "Runner Cloud Run Job container must not inject GITHUB_APP_TOKEN."
  }

  assert {
    condition     = google_cloud_run_v2_job.runner.template[0].template[0].containers[0].image != "us-docker.pkg.dev/cloudrun/container/job:latest"
    error_message = "Runner Cloud Run Job must not use the placeholder container image."
  }

  assert {
    condition     = startswith(google_cloud_run_v2_job.runner.template[0].template[0].containers[0].image, "us-central1-docker.pkg.dev/test-project-123/test-compute-runner/codemender-runner:")
    error_message = "Runner Cloud Run Job must use the deterministic Artifact Registry image URI."
  }

  assert {
    condition     = length(terraform_data.cloud_build_runner_image) == 1
    error_message = "terraform_data.cloud_build_runner_image must be planned by default when auto_build_image = true."
  }

  assert {
    condition     = output.cloud_run_artifact_registry_repository == "us-central1-docker.pkg.dev/test-project-123/test-compute-runner"
    error_message = "cloud_run_artifact_registry_repository output must match the Artifact Registry repository path."
  }

  assert {
    condition     = output.cloud_run_image_uri == google_cloud_run_v2_job.runner.template[0].template[0].containers[0].image
    error_message = "cloud_run_image_uri output must match the Cloud Run Job container image."
  }

  assert {
    condition     = strcontains(output.next_step_cloud_build_command, "gcloud builds submit --config=cloudbuild.yaml --project=test-project-123")
    error_message = "next_step_cloud_build_command output must contain the gcloud builds submit command."
  }
}

run "compute_resources_auto_build_image_disabled" {
  command = plan

  variables {
    auto_build_image = false
  }

  assert {
    condition     = length(terraform_data.cloud_build_runner_image) == 0
    error_message = "terraform_data.cloud_build_runner_image must have count 0 when auto_build_image = false."
  }

  assert {
    condition     = startswith(google_cloud_run_v2_job.runner.template[0].template[0].containers[0].image, "us-central1-docker.pkg.dev/test-project-123/test-compute-runner/codemender-runner:")
    error_message = "Runner Cloud Run Job must still reference the deterministic Artifact Registry image URI when auto_build_image = false."
  }
}

