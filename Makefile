.PHONY: test test-cov build-runner build-cloudrun build-gitlab

RUNNER_IMAGE ?= codemender-runner:local
CLOUDRUN_IMAGE ?= codemender-cloudrun:local
GITLAB_IMAGE ?= codemender-gitlab:local

empty :=
space := $(empty) $(empty)
# Governing: ADR-0008, SPEC-GHA/REQ-0004
PACKAGE_SRCS := $(subst $(space),:,$(wildcard packages/*/src))
COV_FLAGS := $(foreach pkg,$(notdir $(patsubst %/__init__.py,%,$(wildcard packages/*/src/*/__init__.py))),--cov=$(pkg))

test:
	GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 PYTHONPATH="$(PACKAGE_SRCS)" pytest

# Requires: pip install -e ./packages/codemender -e ./packages/launcher -e "./packages/repohost[github,gitlab]" pytest pytest-cov
test-cov:
	GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 PYTHONPATH="$(PACKAGE_SRCS)" pytest $(COV_FLAGS) --cov-report=term-missing

build-runner:
	docker build -f docker/runner.Dockerfile -t $(RUNNER_IMAGE) .

build-cloudrun:
	docker build -f docker/cloudrun.Dockerfile -t $(CLOUDRUN_IMAGE) .

build-gitlab:
	docker build -f docker/gitlab.Dockerfile -t $(GITLAB_IMAGE) .
