#!/usr/bin/env bash
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
#
# deploy.sh -- deploy the CodeMender MCP server and everything it depends on
# (runner Cloud Run Job, GCS bucket, Artifact Registry, BigQuery dataset,
# service accounts/IAM, Secret Manager secrets), or roll out new code.
#
#   ./deploy.sh --plan-only          # preview: terraform plan, changes nothing
#   ./deploy.sh                      # full deploy (asks before applying)
#   ./deploy.sh --code-only          # rebuild images + update job/service only
#
# Works from Linux, macOS, Cloud Shell, WSL and Git Bash on Windows. Requires
# gcloud, terraform (>= 1.5), git and python. `make` and Docker are NOT needed:
# images are built remotely with Cloud Build.

set -euo pipefail

# ----------------------------------------------------------------- defaults
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${REPO_ROOT}/terraform"
TFVARS="${TF_DIR}/terraform.tfvars"

PROJECT_ID="${GCP_PROJECT_ID:-}"
REGION="${GCP_REGION:-}"
PREFIX="${RESOURCE_PREFIX:-}"
GITHUB_OWNER="${GITHUB_OWNER:-}"
GH_SECRET="${MCP_GITHUB_TOKEN_SECRET:-codemender-github-token}"
AUTH_SECRET="${MCP_AUTH_TOKEN_SECRET:-codemender-mcp-auth-token}"
RUNNER_TIMEOUT="10800s"
ACCESS_MODE="token"        # token | private
MODE="full"                # full | plan | code
ASSUME_YES=false
ROTATE_GH=false
GH_FROM_CLI=false

# ------------------------------------------------------------------ helpers
c_bold=$'\033[1m'; c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_off=$'\033[0m'
[[ -t 1 ]] || { c_bold=""; c_red=""; c_grn=""; c_ylw=""; c_off=""; }
step() { printf '\n%s==> %s%s\n' "$c_bold" "$*" "$c_off"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s✓%s %s\n' "$c_grn" "$c_off" "$*"; }
warn() { printf '    %s!%s %s\n' "$c_ylw" "$c_off" "$*" >&2; }
die()  { printf '%sERROR:%s %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }

confirm() {
  $ASSUME_YES && return 0
  [[ -t 0 ]] || die "Confirmation required for: $1 (re-run interactively or pass --yes)."
  local reply
  read -r -p "    $1 [y/N] " reply
  [[ "$reply" =~ ^[Yy]([Ee][Ss])?$ ]]
}

usage() {
  cat <<'EOF'
Usage: ./deploy.sh [options]

Modes (default: full deploy)
  --plan-only               Run terraform plan only. Creates/changes nothing.
  --code-only               Rebuild images and roll the runner job + MCP service.
                            Use after code changes once infrastructure exists.

Options
  --project ID              GCP project (default: terraform.tfvars, then gcloud config)
  --region REGION           GCP region (default: terraform.tfvars, then us-central1)
  --prefix PREFIX           Resource name prefix (default: codemender)
  --github-owner OWNER      GitHub user/org that owns the repos (default: tfvars or `gh`)
  --private                 No public URL: IAM-only access for your gcloud account
                            (connect with `gcloud run services proxy`). Default is a
                            public URL protected by a bearer token.
  --github-token-from-gh    Store the token from `gh auth token` as the GitHub secret
  --rotate-github-token     Add a new version to the GitHub token secret
  --runner-timeout DUR      Runner job timeout if not set in tfvars (default: 10800s)
  -y, --yes                 Do not prompt (CI). Applies without confirmation.
  -h, --help                Show this help

Environment
  GITHUB_TOKEN              GitHub token (repo scope) stored in Secret Manager on
                            first deploy; prompted for if unset.
EOF
}

# ------------------------------------------------------------------ args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan-only) MODE="plan" ;;
    --code-only) MODE="code" ;;
    --project) PROJECT_ID="${2:?}"; shift ;;
    --region) REGION="${2:?}"; shift ;;
    --prefix) PREFIX="${2:?}"; shift ;;
    --github-owner) GITHUB_OWNER="${2:?}"; shift ;;
    --private) ACCESS_MODE="private" ;;
    --github-token-from-gh) GH_FROM_CLI=true ;;
    --rotate-github-token) ROTATE_GH=true ;;
    --runner-timeout) RUNNER_TIMEOUT="${2:?}"; shift ;;
    -y|--yes) ASSUME_YES=true ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "Unknown option: $1" ;;
  esac
  shift
done

# --------------------------------------------------------------- preflight
step "Checking prerequisites"
for bin in gcloud git; do
  command -v "$bin" >/dev/null 2>&1 || die "'$bin' is not installed."
done
if [[ "$MODE" != "code" ]]; then
  command -v terraform >/dev/null 2>&1 || die "terraform is not installed (https://developer.hashicorp.com/terraform/install; Windows: winget install Hashicorp.Terraform)."
fi
PYTHON=""
for cand in python3 python py; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; then
    PYTHON="$cand"; break
  fi
done
[[ -n "$PYTHON" ]] || die "Python >= 3.8 is required (used to compute image tags)."
gcloud auth print-access-token >/dev/null 2>&1 || die "gcloud is not logged in. Run: gcloud auth login"
if [[ "$MODE" != "code" ]] && ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
  die "Terraform needs Application Default Credentials. Run: gcloud auth application-default login"
fi
ok "tools found (python: ${PYTHON})"

# Run terraform inside its directory (avoids -chdir path mangling on Git Bash).
tf() { (cd "$TF_DIR" && terraform "$@"); }

tfvar() {  # tfvar NAME -> value from terraform.tfvars (simple string/bool assignments)
  [[ -f "$TFVARS" ]] || return 0
  sed -n -E "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"?([^\"#]*)\"?.*$/\1/p" "$TFVARS" | tail -n 1 | sed -E 's/[[:space:]]+$//'
}

TF_PROJECT="$(tfvar gcp_project_id)"; TF_REGION="$(tfvar gcp_region)"
if [[ -n "$TF_PROJECT" && "$TF_PROJECT" != "TODO" && -n "$PROJECT_ID" && "$PROJECT_ID" != "$TF_PROJECT" ]]; then
  die "--project ($PROJECT_ID) differs from gcp_project_id in terraform.tfvars ($TF_PROJECT). Edit tfvars or use a separate checkout per project."
fi
[[ -n "$PROJECT_ID" ]] || { [[ "$TF_PROJECT" != "TODO" ]] && PROJECT_ID="$TF_PROJECT"; } || true
[[ -n "$PROJECT_ID" ]] || PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
[[ -n "$PROJECT_ID" ]] || die "No GCP project. Pass --project or run: gcloud config set project ID"
REGION="${REGION:-${TF_REGION:-us-central1}}"
PREFIX="${PREFIX:-$(tfvar resource_prefix)}"; PREFIX="${PREFIX:-codemender}"

AR_REPO="${PREFIX}-runner"
JOB_NAME="${PREFIX}-runner"
SERVICE_NAME="${PREFIX}-mcp"
AR_HOST="${REGION}-docker.pkg.dev"
ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"

info "project=${PROJECT_ID}  region=${REGION}  prefix=${PREFIX}  mode=${MODE}  access=${ACCESS_MODE}"
gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null 2>&1 \
  || die "Project '$PROJECT_ID' not found or not accessible as ${ACCOUNT:-current account}."

# ---------------------------------------------------------- image tags
# Runner tag: identical formula to terraform/modules/gcp/compute.tf
# (runner_image_hash) so Terraform and this script agree on the image URI.
RUNNER_TAG="$(cd "$REPO_ROOT" && "$PYTHON" - <<'PY'
import hashlib, pathlib
r = pathlib.Path(".")
s = ["docker/runner.Dockerfile", "docker/cloudrun.Dockerfile", "cloud-run-jobs/entrypoint.py", "cloudbuild.yaml"]
p = r / "packages/codemender"
fs = sorted(
    f for x in p.rglob("*") if x.is_file()
    for f in [x.relative_to(p).as_posix()]
    if "__pycache__/" not in f and ".egg-info/" not in f and not f.startswith("build/")
    and "/testdata/" not in f and not x.name.endswith(".pyc")
    and not x.name.startswith("test_") and not x.name.endswith("_test.py")
)
h = lambda x: hashlib.sha256(x.read_bytes()).hexdigest()
print(hashlib.sha256("".join([*(h(r / f) for f in s), *(f"{f}:{h(p / f)}" for f in fs)]).encode()).hexdigest()[:12])
PY
)"
# MCP tag: content hash of everything baked into docker/mcp.Dockerfile, so
# unchanged code is never rebuilt and changed code always is.
MCP_TAG="$(cd "$REPO_ROOT" && "$PYTHON" - <<'PY'
import hashlib, pathlib
files = [pathlib.Path("docker/mcp.Dockerfile"), pathlib.Path("cloudbuild.mcp.yaml")]
for root in ("packages/codemender", "packages/mcpserver"):
    files += [x for x in pathlib.Path(root).rglob("*") if x.is_file()
              and "__pycache__" not in x.parts and not any(p.endswith(".egg-info") for p in x.parts)
              and not x.name.startswith("test_") and "testdata" not in x.parts]
d = hashlib.sha256()
for f in sorted(files, key=lambda x: x.as_posix()):
    d.update(f.as_posix().encode()); d.update(hashlib.sha256(f.read_bytes()).digest())
print("m" + d.hexdigest()[:12])
PY
)"
RUNNER_IMAGE="${AR_HOST}/${PROJECT_ID}/${AR_REPO}/codemender-runner:${RUNNER_TAG}"
MCP_IMAGE="${AR_HOST}/${PROJECT_ID}/${AR_REPO}/codemender-mcp:${MCP_TAG}"
info "runner image: ${RUNNER_IMAGE}"
info "mcp image:    ${MCP_IMAGE}"

image_exists() { gcloud artifacts docker images describe "$1" --project="$PROJECT_ID" >/dev/null 2>&1; }

build_images() {
  step "Building container images with Cloud Build"
  if image_exists "$RUNNER_IMAGE"; then
    ok "runner image up to date"
  else
    info "building runner image (first build takes ~10-15 min)..."
    (cd "$REPO_ROOT" && gcloud builds submit --config=cloudbuild.yaml --project="$PROJECT_ID" \
      --substitutions="_REGION=${REGION},_REPO_NAME=${AR_REPO},_IMAGE_TAG=${RUNNER_TAG}" .)
    ok "runner image pushed"
  fi
  if image_exists "$MCP_IMAGE"; then
    ok "mcp image up to date"
  else
    info "building mcp image..."
    (cd "$REPO_ROOT" && gcloud builds submit --config=cloudbuild.mcp.yaml --project="$PROJECT_ID" \
      --substitutions="_REGION=${REGION},_REPO_NAME=${AR_REPO},_IMAGE_TAG=${MCP_TAG}" .)
    ok "mcp image pushed"
  fi
}

print_connect_help() {
  local url="$1"
  step "Done"
  if [[ -z "$url" ]]; then
    warn "Could not determine the service URL; check: gcloud run services describe ${SERVICE_NAME} --region ${REGION}"
    return
  fi
  ok "MCP endpoint: ${url}/mcp"
  if [[ "$ACCESS_MODE" == "private" ]]; then
    cat <<EOF

    Connect (private / IAM):
      gcloud run services proxy ${SERVICE_NAME} --region ${REGION} --project ${PROJECT_ID} --port 8080
      claude mcp add --transport http codemender http://localhost:8080/mcp
EOF
  else
    if curl -fsS --max-time 20 "${url}/healthz" >/dev/null 2>&1; then ok "health check passed"; else warn "health check did not pass yet (service may still be starting)"; fi
    cat <<EOF

    Connect Claude Code:
      claude mcp add --transport http codemender ${url}/mcp \\
        --header "Authorization: Bearer \$(gcloud secrets versions access latest --secret ${AUTH_SECRET} --project ${PROJECT_ID})"
EOF
  fi
  cat <<EOF

    Then ask, e.g.: "Use codemender to scan ${GITHUB_OWNER:-owner}/<repo>"
    Findings:  bq query --use_legacy_sql=false 'SELECT finding_id, severity, status, file_path FROM \`${PROJECT_ID}.codemender.findings\` ORDER BY created_at DESC LIMIT 20'
EOF
}

# ================================================================ code-only
if [[ "$MODE" == "code" ]]; then
  gcloud run services describe "$SERVICE_NAME" --region "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1 \
    || die "Service ${SERVICE_NAME} does not exist yet. Run a full deploy first: ./deploy.sh"
  confirm "Build images and roll ${JOB_NAME} (job) and ${SERVICE_NAME} (service) to the new code?" || die "Aborted."
  build_images
  step "Rolling out"
  gcloud run jobs update "$JOB_NAME" --image "$RUNNER_IMAGE" --region "$REGION" --project "$PROJECT_ID" --quiet >/dev/null
  ok "job ${JOB_NAME} -> ${RUNNER_TAG}"
  gcloud run services update "$SERVICE_NAME" --image "$MCP_IMAGE" --region "$REGION" --project "$PROJECT_ID" --quiet >/dev/null
  ok "service ${SERVICE_NAME} -> ${MCP_TAG}"
  warn "If terraform.tfvars pins mcp_image_tag, the next ./deploy.sh overrides it with ${MCP_TAG} (expected)."
  print_connect_help "$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --project "$PROJECT_ID" --format='value(status.url)')"
  exit 0
fi

# ======================================================= full / plan-only
# --- GitHub owner -----------------------------------------------------------
[[ -n "$GITHUB_OWNER" ]] || GITHUB_OWNER="$(tfvar github_owner)"
if [[ -z "$GITHUB_OWNER" ]] && command -v gh >/dev/null 2>&1; then
  GITHUB_OWNER="$(gh api user -q .login 2>/dev/null || true)"
fi
[[ -n "$GITHUB_OWNER" ]] || die "Pass --github-owner (the GitHub user or org that owns the repositories)."

# --- terraform.tfvars -------------------------------------------------------
step "Terraform variables"
if [[ ! -f "$TFVARS" ]]; then
  if [[ "$MODE" == "plan" ]]; then
    info "terraform.tfvars not found; the plan uses command-line variables only."
  else
    cat > "$TFVARS" <<EOF
# Generated by deploy.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Safe to edit; gitignored.
gcp_project_id    = "${PROJECT_ID}"
gcp_region        = "${REGION}"
resource_prefix   = "${PREFIX}"
execution_mode    = "cloud_run"
github_owner      = "${GITHUB_OWNER}"
github_scope_type = "user"
runner_timeout    = "${RUNNER_TIMEOUT}"
enable_mcp_server = true
EOF
    ok "wrote terraform/terraform.tfvars"
  fi
else
  ok "using existing terraform/terraform.tfvars (MCP settings below override it)"
fi

TF_ARGS=(
  -var "gcp_project_id=${PROJECT_ID}"
  -var "gcp_region=${REGION}"
  -var "resource_prefix=${PREFIX}"
  -var "execution_mode=cloud_run"
  -var "enable_mcp_server=true"
  -var "auto_build_image=false"
  -var "mcp_image_tag=${MCP_TAG}"
  -var "mcp_github_token_secret_id=${GH_SECRET}"
)
[[ -n "$(tfvar github_owner)" ]] || TF_ARGS+=(-var "github_owner=${GITHUB_OWNER}" -var "github_scope_type=user")
[[ -n "$(tfvar runner_timeout)" ]] || TF_ARGS+=(-var "runner_timeout=${RUNNER_TIMEOUT}")
if [[ "$ACCESS_MODE" == "private" ]]; then
  TF_ARGS+=(-var "mcp_allow_unauthenticated=false" -var "mcp_invoker_members=[\"user:${ACCOUNT}\"]")
else
  TF_ARGS+=(-var "mcp_auth_token_secret_id=${AUTH_SECRET}" -var "mcp_allow_unauthenticated=true")
fi
RT="$(tfvar runner_timeout)"; RT="${RT:-$RUNNER_TIMEOUT}"
if [[ "${RT%s}" =~ ^[0-9]+$ ]] && (( ${RT%s} < 10800 )); then
  warn "runner_timeout=${RT} is below 10800s; full-repository scans may be cut off."
fi

secret_exists() { gcloud secrets describe "$1" --project "$PROJECT_ID" >/dev/null 2>&1; }

# ================================================================ plan-only
if [[ "$MODE" == "plan" ]]; then
  step "terraform plan (no changes will be made)"
  secret_exists "$GH_SECRET" || warn "secret ${GH_SECRET} does not exist yet; a full deploy will create it."
  image_exists "$RUNNER_IMAGE" || warn "runner image not built yet; a full deploy builds it before applying."
  tf init -input=false -upgrade=false >/dev/null
  tf plan -input=false "${TF_ARGS[@]}"
  exit 0
fi

# ================================================================ full deploy
cat <<EOF

    This will, in project ${c_bold}${PROJECT_ID}${c_off}:
      1. enable required APIs and create Secret Manager secrets (if missing)
      2. create the Artifact Registry repo (if missing) and build images with Cloud Build
      3. terraform apply: runner Cloud Run Job, GCS bucket, BigQuery dataset,
         service accounts/IAM and the ${SERVICE_NAME} Cloud Run service
         (you will see the plan and confirm before anything is applied)
EOF
confirm "Continue?" || die "Aborted."

step "Enabling bootstrap APIs"
gcloud services enable \
  serviceusage.googleapis.com cloudresourcemanager.googleapis.com iam.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  run.googleapis.com bigquery.googleapis.com storage.googleapis.com aiplatform.googleapis.com \
  --project "$PROJECT_ID"
ok "APIs enabled"

step "Secret Manager"
if secret_exists "$GH_SECRET" && ! $ROTATE_GH; then
  ok "${GH_SECRET} exists (use --rotate-github-token to replace)"
else
  token="${GITHUB_TOKEN:-}"
  if [[ -z "$token" ]] && $GH_FROM_CLI; then
    token="$(gh auth token 2>/dev/null || true)"
    [[ -n "$token" ]] && warn "using your gh CLI token; prefer a fine-grained token scoped to the target repos."
  fi
  if [[ -z "$token" ]]; then
    [[ -t 0 ]] || die "Set GITHUB_TOKEN (repo scope) or pass --github-token-from-gh."
    read -r -s -p "    GitHub token (repo scope; used to clone, push fix branches and open PRs): " token; echo
  fi
  [[ -n "$token" ]] || die "Empty GitHub token."
  if secret_exists "$GH_SECRET"; then
    printf '%s' "$token" | gcloud secrets versions add "$GH_SECRET" --data-file=- --project "$PROJECT_ID" >/dev/null
    ok "added new version to ${GH_SECRET}"
  else
    printf '%s' "$token" | gcloud secrets create "$GH_SECRET" --data-file=- --replication-policy=automatic --project "$PROJECT_ID" >/dev/null
    ok "created ${GH_SECRET}"
  fi
  unset token
fi
if [[ "$ACCESS_MODE" == "token" ]]; then
  if secret_exists "$AUTH_SECRET"; then
    ok "${AUTH_SECRET} exists"
  else
    "$PYTHON" -c 'import secrets, sys; sys.stdout.write(secrets.token_hex(32))' \
      | gcloud secrets create "$AUTH_SECRET" --data-file=- --replication-policy=automatic --project "$PROJECT_ID" >/dev/null
    ok "created ${AUTH_SECRET} (random bearer token for MCP clients)"
  fi
fi

step "Terraform init"
tf init -input=false >/dev/null
ok "initialized"

# Images must exist before Cloud Run resources are created, and images need the
# Artifact Registry repo (managed by Terraform): bootstrap it with a targeted apply.
if ! gcloud artifacts repositories describe "$AR_REPO" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  step "Bootstrapping Artifact Registry repo + Cloud Build permissions (targeted apply)"
  BOOT=(
    -target='module.gcp[0].google_artifact_registry_repository.docker_repo'
    -target='module.gcp[0].google_artifact_registry_repository_iam_member.cloudbuild_ar_writer'
    -target='module.gcp[0].google_project_iam_member.cloudbuild_storage_viewer'
    -target='module.gcp[0].google_project_iam_member.cloudbuild_log_writer'
  )
  # Interactive: terraform shows the targeted plan and asks for "yes" itself.
  APPROVE=(); if $ASSUME_YES; then APPROVE=(-input=false -auto-approve); fi
  tf apply "${APPROVE[@]}" "${TF_ARGS[@]}" "${BOOT[@]}"
  ok "repo ${AR_REPO} ready"
fi

build_images

step "Terraform plan"
tf plan -input=false -out=tfplan "${TF_ARGS[@]}"
confirm "Apply this plan?" || { rm -f "${TF_DIR}/tfplan"; die "Aborted; nothing applied."; }

step "Terraform apply"
tf apply -input=false tfplan
rm -f "${TF_DIR}/tfplan"
ok "infrastructure applied"

URL="$(tf output -raw mcp_endpoint 2>/dev/null || true)"
print_connect_help "${URL%/mcp}"
