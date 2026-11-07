.PHONY: test test-cov build-runner build-cloudrun build-gitlab build-mcp run-mcp check-gcp-project deploy-cloudrun deploy-mcp init-fork

RUNNER_IMAGE ?= codemender-runner:local
CLOUDRUN_IMAGE ?= codemender-cloudrun:local
GITLAB_IMAGE ?= codemender-gitlab:local
MCP_IMAGE ?= codemender-mcp:local
MCP_SERVICE ?= $(RESOURCE_PREFIX)-mcp

TFVARS_FILE ?= terraform/terraform.tfvars
parse_tfvar = $(strip $(shell [ -f "$(TFVARS_FILE)" ] && awk -F= '/^[[:space:]]*$(1)[[:space:]]*=/ { val = $$2; sub(/[[:space:]]*\043.*/, "", val); gsub(/^[[:space:]"'\'']+|[[:space:]"'\'']+$$/, "", val); print val }' "$(TFVARS_FILE)" | tail -n 1))

GCP_PROJECT_ID ?= $(call parse_tfvar,gcp_project_id)
GCP_REGION ?= $(call parse_tfvar,gcp_region)
RESOURCE_PREFIX ?= $(or $(call parse_tfvar,resource_prefix),codemender)

IMAGE_TAG ?= $(shell python3 -c 'import hashlib, pathlib; r=pathlib.Path("."); s=["docker/runner.Dockerfile","docker/cloudrun.Dockerfile","cloud-run-jobs/entrypoint.py","cloudbuild.yaml"]; p=r/"packages/codemender"; fs=sorted(f for x in p.rglob("*") if x.is_file() for f in [x.relative_to(p).as_posix()] if "__pycache__/" not in f and ".egg-info/" not in f and not f.startswith("build/") and "/testdata/" not in f and not x.name.endswith(".pyc") and not x.name.startswith("test_") and not x.name.endswith("_test.py")); h=lambda x: hashlib.sha256(x.read_bytes()).hexdigest(); print(hashlib.sha256("".join([*(h(r/f) for f in s),*(f"{f}:{h(p/f)}" for f in fs)]).encode()).hexdigest()[:12])')
AR_HOST = $(GCP_REGION)-docker.pkg.dev
AR_REPO = $(RESOURCE_PREFIX)-runner
CLOUDRUN_JOB ?= $(RESOURCE_PREFIX)-runner
REMOTE_IMAGE_SHA = $(AR_HOST)/$(GCP_PROJECT_ID)/$(AR_REPO)/codemender-runner:$(IMAGE_TAG)
MCP_IMAGE_TAG ?= $(shell git rev-parse --short=12 HEAD 2>/dev/null || echo dev)
REMOTE_MCP_IMAGE = $(AR_HOST)/$(GCP_PROJECT_ID)/$(AR_REPO)/codemender-mcp:$(MCP_IMAGE_TAG)

empty :=
space := $(empty) $(empty)
# Governing: ADR-0008, SPEC-GHA/REQ-0004
PACKAGE_SRCS := $(subst $(space),:,$(wildcard packages/*/src))
COV_FLAGS := $(foreach pkg,$(notdir $(patsubst %/__init__.py,%,$(wildcard packages/*/src/*/__init__.py))),--cov=$(pkg))

test:
	GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 PYTHONPATH="$(PACKAGE_SRCS)" pytest

# Requires: pip install -e ./packages/artifactstore \
#   -e ./packages/codemender -e ./packages/launcher \
#   -e "./packages/repohost[github,gitlab]" pytest pytest-cov
test-cov:
	GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 PYTHONPATH="$(PACKAGE_SRCS)" pytest $(COV_FLAGS) --cov-report=term-missing

build-runner:
	docker build -f docker/runner.Dockerfile -t $(RUNNER_IMAGE) .

build-cloudrun: build-runner
	docker build -f docker/cloudrun.Dockerfile --build-arg BASE_IMAGE=$(RUNNER_IMAGE) -t $(CLOUDRUN_IMAGE) .

build-gitlab:
	docker build -f docker/gitlab.Dockerfile -t $(GITLAB_IMAGE) .

build-mcp:
	docker build -f docker/mcp.Dockerfile -t $(MCP_IMAGE) .

# Runs the MCP server locally against real GCP resources (uses your ADC).
run-mcp: build-mcp
	docker run --rm -p 8080:8080 -e GOOGLE_CLOUD_PROJECT=$(GCP_PROJECT_ID) -e CLOUD_RUN_REGION=$(GCP_REGION) \n		-e CLOUD_RUN_JOB_NAME=$(CLOUDRUN_JOB) -e GITHUB_TOKEN -e CM_MCP_AUTH_TOKEN \n		-v $(HOME)/.config/gcloud:/home/nonroot/.config/gcloud:ro $(MCP_IMAGE)

check-gcp-project:
	@if [ -z "$(GCP_PROJECT_ID)" ] || [ "$(GCP_PROJECT_ID)" = "TODO" ]; then \
		echo "Error: GCP_PROJECT_ID is not set. Configure gcp_project_id in $(TFVARS_FILE) or pass GCP_PROJECT_ID=<project-id>." >&2; \
		exit 1; \
	fi
	@if [ -z "$(GCP_REGION)" ] || [ "$(GCP_REGION)" = "TODO" ]; then \
		echo "Error: GCP_REGION is not set. Configure gcp_region in $(TFVARS_FILE) or pass GCP_REGION=<region>." >&2; \
		exit 1; \
	fi

# Governing: docs/openspec/specs/wif/spec.md#req-0005, docs/openspec/specs/wif/spec.md#req-0007
deploy-cloudrun: check-gcp-project
	gcloud builds submit --config=cloudbuild.yaml --project=$(GCP_PROJECT_ID) --substitutions=_REGION=$(GCP_REGION),_REPO_NAME=$(AR_REPO),_IMAGE_TAG=$(IMAGE_TAG) .
	gcloud run jobs update $(CLOUDRUN_JOB) --image $(REMOTE_IMAGE_SHA) --region $(GCP_REGION) --project $(GCP_PROJECT_ID)

# Governing: docs/openspec/specs/distribute/spec.md#req-0004, SPEC-DIST
init-fork:
	./scripts/set-fork-owner.sh $(OWNER)

# Builds the MCP server image with Cloud Build and rolls the Cloud Run service.
deploy-mcp: check-gcp-project
	gcloud builds submit --config=cloudbuild.mcp.yaml --project=$(GCP_PROJECT_ID) --substitutions=_REGION=$(GCP_REGION),_REPO_NAME=$(AR_REPO),_IMAGE_TAG=$(MCP_IMAGE_TAG) .
	gcloud run services update $(MCP_SERVICE) --image $(REMOTE_MCP_IMAGE) --region $(GCP_REGION) --project $(GCP_PROJECT_ID)
