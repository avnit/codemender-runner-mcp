# ==============================================================================
# Cloud Run Jobs - Container Bootstrapper Layer
# Path: cloud-run-jobs/Dockerfile
# Base: Workstream #1 codemender-runner image (configurable via --build-arg)
# ==============================================================================
ARG BASE_IMAGE=codemender-runner:local
FROM ${BASE_IMAGE}

# 1. Standardize ephemeral workspace scratch directory under /workspace
ENV EPHEMERAL_SCRATCH_DIR="/workspace"
RUN mkdir -p /workspace /workspace/output /workspace/repo /workspace/inputs

# 2. Copy our Cloud Run transient bootstrapper directly into the execution path
COPY cloud-run-jobs/entrypoint.py /workspace/entrypoint.py
RUN chmod +x /workspace/entrypoint.py

# 3. Establish runtime working directory and entrypoint
WORKDIR /workspace
ENTRYPOINT ["python3", "/workspace/entrypoint.py"]

