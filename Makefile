.PHONY: test test-cov build-runner build-cloudrun build-gitlab

RUNNER_IMAGE ?= codemender-runner:local
CLOUDRUN_IMAGE ?= codemender-cloudrun:local
GITLAB_IMAGE ?= codemender-gitlab:local

test:
	GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 PYTHONPATH=packages/codemender/src pytest

# Requires: pip install -e "./packages/codemender[dev]"
test-cov:
	GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 PYTHONPATH=packages/codemender/src pytest --cov=codemender --cov-report=term-missing

build-runner:
	docker build -f docker/runner.Dockerfile -t $(RUNNER_IMAGE) .

build-cloudrun:
	docker build -f docker/cloudrun.Dockerfile -t $(CLOUDRUN_IMAGE) .

build-gitlab:
	docker build -f docker/gitlab.Dockerfile -t $(GITLAB_IMAGE) .
