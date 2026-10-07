#!/bin/bash
# ==============================================================================
# Job entry point
# Path: cloud-run-jobs/entrypoint.sh
# ==============================================================================
set -Eeuo pipefail

log() {
  echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] 🤖 [Local-Test] $1"
}

CM_COMMAND="${CM_COMMAND:-version}"
WORKSPACE_DIR="/workspace"
OUTPUT_DIR="/workspace/output"
mkdir -p "${WORKSPACE_DIR}" "${OUTPUT_DIR}"

cd "${WORKSPACE_DIR}"

# 1. Zero-Trust Security Check: Verify credential unsetting works cleanly
log "Securing environment: Scrubbing credentials..."
unset GOOGLE_APPLICATION_CREDENTIALS GCP_TOKEN CLOUD_RUN_TOKEN

# 2. Command Router: Invoke the orchestrator
log "Invoking command: [${CM_COMMAND}]"
case "${CM_COMMAND}" in
  version)
    cm-runner version
    ;;
  *)
    log "ERROR: Command '${CM_COMMAND}' is not supported."
    exit 1
    ;;
esac

log "Complete!"
