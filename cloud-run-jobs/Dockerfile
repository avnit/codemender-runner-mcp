# ==============================================================================
# Cloud Run Jobs - Local Source Build 
# Path: cloud-run-jobs/Dockerfile
# Base: Locally built Workstream #1 image
# ==============================================================================
FROM codemender-runner:local

# 1. Copy our Cloud Run transient bootstrapper directly into the execution path
COPY cloud-run-jobs/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# 2. Establish our runtime entrypoint
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
