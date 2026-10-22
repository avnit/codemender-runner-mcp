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

# Governing: docs/openspec/specs/distribute/spec.md#req-0004, SPEC-DIST, ADR-0008
#
# scripts/set-fork-owner.sh — Configure <owner>/<repo>/.github/actions/*
# references across .github/workflows/*.yml for private or customized forks.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
  cat <<'EOF'
Usage: ./scripts/set-fork-owner.sh [<owner> | <owner>/<repo> | --auto]

Updates composite action references (<owner>/<repo>/.github/actions/*) across
.github/workflows/*.yml to match your fork's GitHub owner and repository name.

Arguments:
  <owner>           GitHub organization or user name (defaults repo to codemender-runner).
  <owner>/<repo>    Explicit GitHub owner and repository name.
  --auto            Detect <owner>/<repo> from `git remote get-url origin` (default when
                    no positional argument is supplied).
  -h, --help        Print this usage message and exit 0.

Note for Private / Internal Forks:
  In GitHub, navigate to Settings -> Actions -> General -> Access and select
  "Accessible from repositories in the organization" so downstream repositories
  can invoke composite actions hosted in this fork.
EOF
}

log_err() {
  printf '[set-fork-owner] ERROR: %s\n' "$*" >&2
}

is_valid_segment() {
  local seg="$1"
  if [[ -z "$seg" || "$seg" == "." || "$seg" == ".." ]]; then
    return 1
  fi
  [[ "$seg" =~ ^[A-Za-z0-9_.-]+$ ]]
}

detect_from_origin() {
  local url
  if ! url="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null)"; then
    log_err "Could not read 'origin' remote URL. Pass <owner> or <owner>/<repo> explicitly."
    exit 2
  fi
  url="${url%.git}"
  url="${url%/}"
  if [[ "$url" =~ [:\/]([^:\/]+)\/([^:\/]+)$ ]]; then
    printf '%s/%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
    return 0
  fi
  log_err "Could not parse <owner>/<repo> from origin URL: '$url'."
  exit 2
}

ARG=""
if [[ $# -gt 1 ]]; then
  log_err "Too many arguments. Run with --help for usage."
  exit 2
elif [[ $# -eq 1 ]]; then
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --auto)
      ARG=""
      ;;
    -*)
      log_err "Unknown option: '$1'. Run with --help for usage."
      exit 2
      ;;
    *)
      ARG="$1"
      ;;
  esac
fi

if [[ -z "$ARG" ]]; then
  ARG="$(detect_from_origin)"
fi

OWNER=""
REPO="codemender-runner"

if [[ "$ARG" == */* ]]; then
  # Reject more than one slash
  if [[ "${ARG#*/}" == */* ]]; then
    log_err "Invalid argument '$ARG': expected <owner> or <owner>/<repo>."
    exit 2
  fi
  OWNER="${ARG%%/*}"
  REPO="${ARG#*/}"
else
  OWNER="$ARG"
fi

if ! is_valid_segment "$OWNER" || ! is_valid_segment "$REPO"; then
  log_err "Invalid owner/repository '$OWNER/$REPO': each segment must match ^[A-Za-z0-9_.-]+$."
  exit 2
fi

WORKFLOWS_DIR="${REPO_ROOT}/.github/workflows"
if [[ ! -d "$WORKFLOWS_DIR" ]]; then
  log_err "Workflows directory not found at '$WORKFLOWS_DIR'."
  exit 2
fi

for wf in "$WORKFLOWS_DIR"/*.yml; do
  [[ -f "$wf" ]] || continue
  sed -i -E \
    -e "s|(uses:[[:space:]]+)[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(\.github/actions/)|\1${OWNER}/${REPO}/\2|g" \
    -e "s|(uses:[[:space:]]+)\./(\.github/actions/[^@[[:space:]]+)$|\1${OWNER}/${REPO}/\2@main|g" \
    "$wf"
done

printf '[set-fork-owner] Updated .github/workflows/*.yml action references to %s/%s.\n' "$OWNER" "$REPO"
printf '[set-fork-owner] Reminder for private/internal forks: enable Settings -> Actions -> General -> Access -> "Accessible from repositories in the organization".\n'
