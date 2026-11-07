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

# CodeMender MCP server (Cloud Run service). It does not run `cm` itself: every
# find/validate/fix call launches a new execution of the sandboxed
# codemender-runner Cloud Run Job (docker/cloudrun.Dockerfile).
FROM python:3.11.9-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    CM_MCP_WORK_DIR=/tmp/cm-mcp

RUN apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/codemender
COPY packages/codemender ./packages/codemender/
COPY packages/mcpserver ./packages/mcpserver/
RUN pip install ./packages/codemender "./packages/mcpserver[gcp]"

RUN useradd --create-home --uid 10001 nonroot
USER nonroot

EXPOSE 8080
ENTRYPOINT ["cm-mcp"]
