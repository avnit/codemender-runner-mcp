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

# Governing: docs/openspec/specs/gitlab-ci/spec.md#req-0008, docs/openspec/specs/gitlab-ci-fix/spec.md#req-0001, SPEC-GITLAB-CI, SPEC-GITLAB-CI-FIX, ADR-0008
FROM python:3.11-slim-bookworm@sha256:94a8fee35252d06ab75856f15f12c7bae67b10e8a6da93c7fded82b2610926cd

ENV DEBIAN_FRONTEND=noninteractive \
    VIRTUAL_ENV=/opt/codemender/venv \
    PATH="/usr/local/bin:/opt/codemender/venv/bin:${PATH}" \
    PYTHONSAFEPATH=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get -o Acquire::Retries=5 update && \
    apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        ca-certificates \
        git && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/*

WORKDIR /opt/codemender
RUN python3 -m venv /opt/codemender/venv

COPY packages/artifactstore ./packages/artifactstore/
COPY packages/codemender ./packages/codemender/
COPY packages/launcher ./packages/launcher/
COPY packages/repohost ./packages/repohost/

RUN /opt/codemender/venv/bin/pip install --no-cache-dir \
    ./packages/artifactstore \
    ./packages/codemender \
    "./packages/launcher[cloudrun]" \
    "./packages/repohost[gitlab]"

RUN printf '%s\n' \
    '#!/bin/sh' \
    'set -eu' \
    'exec /opt/codemender/venv/bin/python3 -I -B -u -m repohost.gitlab "$@"' \
    > /usr/local/bin/cm-gitlab && \
    chmod 0755 /usr/local/bin/cm-gitlab

ENTRYPOINT ["/usr/local/bin/cm-gitlab"]
