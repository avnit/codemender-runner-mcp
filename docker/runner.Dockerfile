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

# CodeMender Orchestrator - Universal Multi-Toolchain Base Container for GitHub Actions and Cloud Run
FROM ubuntu:22.04

ARG GO_VERSION=1.22.6
ARG CM_DEFAULT_VERSION=0.16.0
ARG CM_VERSION=""
ARG CM_URL=""

ENV DEBIAN_FRONTEND=noninteractive \
    VIRTUAL_ENV=/opt/codemender/venv \
    PATH="/opt/codemender/venv/bin:/usr/local/go/bin:${PATH}"

# 1. Consolidated multi-toolchain install (Python 3.11, Node.js 22, Java 17, and base build tools)
RUN apt-get -o Acquire::Retries=5 update && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        build-essential dirmngr git gzip jq psmisc tar unzip \
        python3.11 python3.11-dev python3.11-venv python3-pip python-is-python3 \
        nodejs \
        openjdk-17-jdk maven gradle \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && npm install -g yarn pnpm \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /root/.npm

# 2. Go Toolchain
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" | tar -C /usr/local -xz

# 3. CodeMender CLI binary
RUN set -eu && \
    CM_VER="${CM_VERSION:-${CM_DEFAULT_VERSION}}" && \
    URL="${CM_URL:-https://artifactregistry.googleapis.com/download/v1/projects/cmoc-prod/locations/us/repositories/codemender-cli-production/files/cm%3A${CM_VER}%3Acm-linux-amd64.zip:download?alt=media}" && \
    echo "Downloading CodeMender CLI from ${URL}..." && \
    curl -fsSL -o /tmp/cm.bin "${URL}" && \
    if unzip -q -o /tmp/cm.bin cm -d /usr/local/bin/ 2>/dev/null; then \
        echo "Extracted cm from zip archive."; \
    else \
        mv /tmp/cm.bin /usr/local/bin/cm; \
    fi && \
    rm -f /tmp/cm.bin && \
    chmod +x /usr/local/bin/cm

# 4. Isolated Python virtual environment & packages/codemender installation
WORKDIR /opt/codemender
RUN python3.11 -m venv /opt/codemender/venv
COPY packages/codemender ./packages/codemender/
RUN /opt/codemender/venv/bin/pip install --no-cache-dir ./packages/codemender

# 5. Container contract
ENTRYPOINT ["cm-runner"]
CMD ["version"]
