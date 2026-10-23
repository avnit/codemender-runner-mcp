# ==============================================================================
# Cloud Run Jobs - Container Bootstrapper Layer
# Path: docker/cloudrun.Dockerfile
# Base: Locally built runner image from docker/runner.Dockerfile (make build-runner; configurable via --build-arg)
# ==============================================================================
ARG BASE_IMAGE=codemender-runner:local
FROM ${BASE_IMAGE}

# 1. Standardize ephemeral workspace scratch directory under /workspace
ENV EPHEMERAL_SCRATCH_DIR="/workspace"
RUN mkdir -p /workspace /workspace/output /workspace/repo /workspace/inputs

# 2. Install google-cloud-storage client library for transit transfers
RUN pip install --no-cache-dir google-cloud-storage

# 3. Copy our Cloud Run transient bootstrapper directly into the execution path
COPY cloud-run-jobs/entrypoint.py /workspace/entrypoint.py
RUN chmod +x /workspace/entrypoint.py

# 3. Establish runtime working directory and entrypoint
WORKDIR /workspace
ENTRYPOINT ["python3", "/workspace/entrypoint.py"]

