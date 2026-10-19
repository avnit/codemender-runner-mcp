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

# scripts/distribute.sh — Customer Repository Distribution & 3-Way Incremental Sync
# Governed by SPEC-DIST (docs/openspec/specs/distribute/spec.md).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_REPO="${CODEMENDER_SOURCE_REPO:-$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || (cd "$SCRIPT_DIR/.." && pwd))}"
EMPTY_TREE_SHA="4b825dc642cb6eb9a060e54bf8d69288fbee4904"

log_info() {
  printf '[distribute] %s\n' "$*" >&2
}

log_err() {
  printf '[distribute] ERROR: %s\n' "$*" >&2
}

usage() {
  cat <<'EOF'
Usage: ./scripts/distribute.sh --target DIR [options]

Extracts a curated, customer-ready subset of CodeMender Runner from Git and
synchronizes it into a target Git repository using stateless 3-way Git merges.

Required Flags:
  --target DIR            Path to the local checkout of the target Git repository.

Optional Flags:
  --source-ref REF        Git reference in the source repository to package from
                          (default: HEAD).
  --include-top-level     Include top-level repository files (LICENSE, .gitignore,
                          and dist/README.md -> README.md) at the root of --target
                          (off by default; automatically enabled on repeat syncs if
                          the target repository previously synced top-level files).
  --branch NAME           Create or switch to branch NAME in the target repository
                          before applying and committing the update.
  --dry-run               Materialize the filtered distribution tree, compute the
                          file-level diff against the target repository, print the
                          summary to stderr, and exit 0 without modifying --target.
  -h, --help              Print this usage message and exit 0.

Exit Codes:
  0  Sync (or --dry-run / --help) completed successfully.
  2  Invalid CLI arguments, invalid --target directory/Git repo, invalid
     --source-ref, or missing previous source commit in source repo.
  3  Target repository has uncommitted changes or untracked files.
  4  3-way merge encountered overlapping line conflicts; conflict markers are
     left in the target working tree for manual resolution.
EOF
}

TARGET=""
SOURCE_REF="HEAD"
INCLUDE_TOP_LEVEL=0
BRANCH=""
DRY_RUN=0
STAGE_ONLY_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        log_err "Flag --target requires a directory argument."
        exit 2
      fi
      TARGET="$2"
      shift 2
      ;;
    --target=*)
      TARGET="${1#*=}"
      if [[ -z "$TARGET" ]]; then
        log_err "Flag --target requires a non-empty directory argument."
        exit 2
      fi
      shift
      ;;
    --source-ref)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        log_err "Flag --source-ref requires a Git ref argument."
        exit 2
      fi
      SOURCE_REF="$2"
      shift 2
      ;;
    --source-ref=*)
      SOURCE_REF="${1#*=}"
      if [[ -z "$SOURCE_REF" ]]; then
        log_err "Flag --source-ref requires a non-empty Git ref argument."
        exit 2
      fi
      shift
      ;;
    --source-repo)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        log_err "Flag --source-repo requires a directory argument."
        exit 2
      fi
      SOURCE_REPO="$2"
      shift 2
      ;;
    --source-repo=*)
      SOURCE_REPO="${1#*=}"
      if [[ -z "$SOURCE_REPO" ]]; then
        log_err "Flag --source-repo requires a non-empty directory argument."
        exit 2
      fi
      shift
      ;;
    --include-top-level)
      INCLUDE_TOP_LEVEL=1
      shift
      ;;
    --branch)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        log_err "Flag --branch requires a branch name argument."
        exit 2
      fi
      BRANCH="$2"
      shift 2
      ;;
    --branch=*)
      BRANCH="${1#*=}"
      if [[ -z "$BRANCH" ]]; then
        log_err "Flag --branch requires a non-empty branch name argument."
        exit 2
      fi
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --stage-only)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        log_err "Flag --stage-only requires a directory argument."
        exit 2
      fi
      STAGE_ONLY_DIR="$2"
      shift 2
      ;;
    --stage-only=*)
      STAGE_ONLY_DIR="${1#*=}"
      if [[ -z "$STAGE_ONLY_DIR" ]]; then
        log_err "Flag --stage-only requires a non-empty directory argument."
        exit 2
      fi
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      log_err "Unknown or invalid argument: '$1'. Run with --help for usage."
      exit 2
      ;;
  esac
done

# Verify source repository and --source-ref
# Governing: docs/openspec/specs/distribute/spec.md#req-0001
if ! git -C "$SOURCE_REPO" rev-parse --git-dir >/dev/null 2>&1; then
  log_err "Source repository '$SOURCE_REPO' is not a valid Git repository."
  exit 2
fi

if ! SOURCE_SHA="$(git -C "$SOURCE_REPO" rev-parse --verify "${SOURCE_REF}^{commit}" 2>/dev/null)"; then
  log_err "Source reference '$SOURCE_REF' does not resolve to a valid commit in '$SOURCE_REPO'."
  exit 2
fi

# Pre-flight validation for normal mode (when not running internal --stage-only)
if [[ -z "$STAGE_ONLY_DIR" ]]; then
  if [[ -z "$TARGET" ]]; then
    log_err "Missing required flag: --target DIR. Run with --help for usage."
    exit 2
  fi

  if [[ ! -d "$TARGET" ]]; then
    log_err "Target path '$TARGET' does not exist or is not a directory."
    exit 2
  fi

  if [[ ! -e "$TARGET/.git" ]]; then
    log_err "Target directory '$TARGET' is not the root of a Git repository (.git not found)."
    exit 2
  fi

  if [[ "$(git -C "$TARGET" rev-parse --is-inside-work-tree 2>/dev/null || true)" != "true" ]]; then
    log_err "Target directory '$TARGET' is not a valid Git working tree."
    exit 2
  fi

  if [[ -n "$(git -C "$TARGET" status --porcelain 2>/dev/null)" ]]; then
    log_err "Target repository '$TARGET' has uncommitted changes or untracked files. Refusing to modify a dirty working tree."
    exit 3
  fi
fi

TMP_ROOT="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

# Governing: docs/openspec/specs/distribute/spec.md#req-0002
extract_source_archive() {
  local ref="$1"
  local dest="$2"
  mkdir -p "$dest"
  git -C "$SOURCE_REPO" archive "$ref" | tar -x -C "$dest"
}

write_stripped_main_py() {
  local dest_file="$1"
  cat >"$dest_file" <<'EOF'
#!/usr/bin/env python3
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

"""CodeMender Runner top-level package entrypoint.

Enables native directory execution (e.g. `python3 .github/codemender-runner <command>`).
Dispatches to the CLI subcommands (`codemender.runner.__main__`).
"""

from pathlib import Path
import sys
from typing import List, Optional

_RUNNER_DIR = Path(__file__).resolve().parent
for _candidate in (
    _RUNNER_DIR / "packages" / "codemender" / "src",
    _RUNNER_DIR.parent / "packages" / "codemender" / "src",
):
  if _candidate.is_dir() and str(_candidate) not in sys.path:
    sys.path.insert(0, str(_candidate))


def main(argv: Optional[List[str]] = None) -> int:
  args = list(sys.argv[1:] if argv is None else argv)
  from codemender.runner.__main__ import main as cli_main  # pylint: disable=import-outside-toplevel

  return cli_main(args)


if __name__ == "__main__":
  sys.exit(main())
EOF
}

# Governing: docs/openspec/specs/distribute/spec.md#req-0003,
#            docs/openspec/specs/distribute/spec.md#req-0005,
#            docs/openspec/specs/distribute/spec.md#req-0006
stage_distribution_tree() {
  local src_dir="$1"
  local out_dir="$2"
  local inc_top="$3"

  mkdir -p "$out_dir"

  # 1. Top-Level Files (only when --include-top-level is enabled)
  if [[ "$inc_top" == "1" ]]; then
    if [[ -f "$src_dir/LICENSE" ]]; then
      cp "$src_dir/LICENSE" "$out_dir/LICENSE"
    fi
    if [[ -f "$src_dir/.gitignore" ]]; then
      cp "$src_dir/.gitignore" "$out_dir/.gitignore"
    fi
    if [[ -f "$src_dir/dist/README.md" ]]; then
      cp "$src_dir/dist/README.md" "$out_dir/README.md"
    elif [[ -f "$src_dir/README.md" ]]; then
      cp "$src_dir/README.md" "$out_dir/README.md"
    fi
  fi

  # 2. Live GitHub Actions Configuration (.github/actions/ & .github/workflows/)
  if [[ -d "$src_dir/github-actions/actions/codemender" ]]; then
    mkdir -p "$out_dir/.github/actions/codemender"
    cp -R "$src_dir/github-actions/actions/codemender/." "$out_dir/.github/actions/codemender/"
  elif [[ -d "$src_dir/.github/actions/codemender" ]]; then
    mkdir -p "$out_dir/.github/actions/codemender"
    cp -R "$src_dir/.github/actions/codemender/." "$out_dir/.github/actions/codemender/"
  fi

  if [[ -d "$src_dir/github-actions/actions/codemender-local" ]]; then
    mkdir -p "$out_dir/.github/actions/codemender-local"
    cp -R "$src_dir/github-actions/actions/codemender-local/." "$out_dir/.github/actions/codemender-local/"
  elif [[ -d "$src_dir/.github/actions/codemender-local" ]]; then
    mkdir -p "$out_dir/.github/actions/codemender-local"
    cp -R "$src_dir/.github/actions/codemender-local/." "$out_dir/.github/actions/codemender-local/"
  fi

  mkdir -p "$out_dir/.github/workflows"
  if [[ -d "$src_dir/github-actions/workflows" ]]; then
    for wf in "$src_dir/github-actions/workflows"/*.yml; do
      [[ -f "$wf" ]] || continue
      cp "$wf" "$out_dir/.github/workflows/$(basename "$wf")"
    done
  elif [[ -d "$src_dir/.github/workflows" ]]; then
    for wf in "$src_dir/.github/workflows"/*.yml; do
      [[ -f "$wf" ]] || continue
      local wf_name
      wf_name="$(basename "$wf")"
      if [[ "$wf_name" != "codemender_parallel.yml" && "$wf_name" != "build_runner_image.yml" ]]; then
        cp "$wf" "$out_dir/.github/workflows/$wf_name"
      fi
    done
  fi

  if [[ -f "$src_dir/.github/workflows/build_runner_image.yml" ]]; then
    sed -e "s|'packages/codemender/\*\*'|'.github/codemender-runner/**'|g" \
        -e "s|'docker/runner.Dockerfile'|'.github/codemender-runner/Dockerfile'|g" \
        -e "s|'\.dockerignore'|'.github/codemender-runner/.dockerignore'|g" \
        -e "s|'codemender-runner/\*\*'|'.github/codemender-runner/**'|g" \
        -e 's|context: \.|context: .github/codemender-runner|g' \
        -e 's|file: docker/runner\.Dockerfile|file: .github/codemender-runner/Dockerfile|g' \
        -e 's|context: codemender-runner|context: .github/codemender-runner|g' \
        -e 's|codemender-runner/Dockerfile|.github/codemender-runner/Dockerfile|g' \
        -e 's/\["legacy"\]/["version"]/g' \
      "$src_dir/.github/workflows/build_runner_image.yml" \
      >"$out_dir/.github/workflows/build_runner_image.yml"
  fi
  rm -f "$out_dir/.github/workflows/codemender_parallel.yml"

  # 3. Stripped codemender-runner/ Package (.github/codemender-runner/)
  local runner_src=""
  if [[ -d "$src_dir/codemender-runner" ]]; then
    runner_src="$src_dir/codemender-runner"
  elif [[ -d "$src_dir/.github/codemender-runner" ]]; then
    runner_src="$src_dir/.github/codemender-runner"
  fi

  if [[ -n "$runner_src" || -d "$src_dir/packages/codemender" ]]; then
    local runner_out="$out_dir/.github/codemender-runner"
    mkdir -p "$runner_out"

    if [[ -f "$src_dir/docker/runner.Dockerfile" ]]; then
      cp "$src_dir/docker/runner.Dockerfile" "$runner_out/Dockerfile"
    elif [[ -n "$runner_src" && -f "$runner_src/Dockerfile" ]]; then
      sed -e '/ln -s \/opt\/codemender\/codemender-runner\/main\.py/d' \
          -e 's|\(ln -s /opt/codemender/codemender-runner/bin/cm-runner /usr/local/bin/cm-runner\) && \\|\1|' \
          -e 's/CMD \["legacy"\]/CMD ["version"]/g' \
          "$runner_src/Dockerfile" \
          >"$runner_out/Dockerfile"
    fi

    if [[ -n "$runner_src" && -f "$runner_src/.dockerignore" ]]; then
      cp "$runner_src/.dockerignore" "$runner_out/.dockerignore"
    elif [[ -f "$src_dir/.dockerignore" ]]; then
      cp "$src_dir/.dockerignore" "$runner_out/.dockerignore"
    fi
    if [[ -n "$runner_src" && -f "$runner_src/requirements.txt" ]]; then
      cp "$runner_src/requirements.txt" "$runner_out/requirements.txt"
    fi

    write_stripped_main_py "$runner_out/__main__.py"

    if [[ -n "$runner_src" && -f "$runner_src/bin/cm-runner" ]]; then
      mkdir -p "$runner_out/bin"
      cp "$runner_src/bin/cm-runner" "$runner_out/bin/cm-runner"
      chmod +x "$runner_out/bin/cm-runner"
    fi

    if [[ -d "$src_dir/packages/codemender" ]]; then
      mkdir -p "$runner_out/packages/codemender"
      cp -R "$src_dir/packages/codemender/." "$runner_out/packages/codemender/"
    elif [[ -n "$runner_src" && -d "$runner_src/packages/codemender" ]]; then
      mkdir -p "$runner_out/packages/codemender"
      cp -R "$runner_src/packages/codemender/." "$runner_out/packages/codemender/"
    fi
    if [[ -d "$runner_out/packages/codemender" ]]; then
      find "$runner_out/packages/codemender" \( -name 'test_*.py' -o -name '*_test.py' \) -type f -delete
      find "$runner_out/packages/codemender" \( -name 'testdata' -o -name '__pycache__' \) -type d -prune -exec rm -rf {} +
    fi
  fi

  # 4. GCP Workload Identity Federation Terraform Configuration (.github/terraform/ & .github/terraform/modules/wif/)
  # Governing: docs/openspec/specs/distribute/spec.md#req-0003
  if [[ -f "$src_dir/terraform/main.tf" || -d "$src_dir/terraform/modules/wif" ]]; then
    mkdir -p "$out_dir/.github/terraform"
    cp -R "$src_dir/terraform/." "$out_dir/.github/terraform/"
    rm -rf \
      "$out_dir/.github/terraform/modules/gcp" \
      "$out_dir/.github/terraform/gcp" \
      "$out_dir/.github/terraform/wif" \
      "$out_dir/.github/terraform/gha_wif" \
      "$out_dir/.github/terraform/gcp.tf" \
      "$out_dir/.github/terraform/gcp_override.tf" \
      "$out_dir/.github/terraform/tests/root_gcp_test.tftest.hcl"
  elif [[ -f "$src_dir/.github/terraform/main.tf" || -d "$src_dir/.github/terraform/modules/wif" ]]; then
    mkdir -p "$out_dir/.github/terraform"
    cp -R "$src_dir/.github/terraform/." "$out_dir/.github/terraform/"
    rm -rf \
      "$out_dir/.github/terraform/modules/gcp" \
      "$out_dir/.github/terraform/gcp" \
      "$out_dir/.github/terraform/wif" \
      "$out_dir/.github/terraform/gha_wif" \
      "$out_dir/.github/terraform/gcp.tf" \
      "$out_dir/.github/terraform/gcp_override.tf" \
      "$out_dir/.github/terraform/tests/root_gcp_test.tftest.hcl"
  elif [[ -d "$src_dir/terraform/wif" ]]; then
    mkdir -p "$out_dir/.github/terraform/wif"
    cp -R "$src_dir/terraform/wif/." "$out_dir/.github/terraform/wif/"
  elif [[ -d "$src_dir/.github/terraform/wif" ]]; then
    mkdir -p "$out_dir/.github/terraform/wif"
    cp -R "$src_dir/.github/terraform/wif/." "$out_dir/.github/terraform/wif/"
  elif [[ -d "$src_dir/terraform/gha_wif" ]]; then
    mkdir -p "$out_dir/.github/terraform/gha_wif"
    cp -R "$src_dir/terraform/gha_wif/." "$out_dir/.github/terraform/gha_wif/"
  elif [[ -d "$src_dir/.github/terraform/gha_wif" ]]; then
    mkdir -p "$out_dir/.github/terraform/gha_wif"
    cp -R "$src_dir/.github/terraform/gha_wif/." "$out_dir/.github/terraform/gha_wif/"
  fi
}

# Handle internal --stage-only mode (used when reconstructing historical $base_tree)
if [[ -n "$STAGE_ONLY_DIR" ]]; then
  extract_source_archive "$SOURCE_SHA" "$TMP_ROOT/stage_only_src"
  stage_distribution_tree "$TMP_ROOT/stage_only_src" "$STAGE_ONLY_DIR" "$INCLUDE_TOP_LEVEL"
  exit 0
fi

TARGET_ABS="$(cd "$TARGET" && pwd)"

target_check_ref="HEAD"
if [[ -n "$BRANCH" ]] && git -C "$TARGET_ABS" show-ref --verify --quiet "refs/heads/$BRANCH"; then
  target_check_ref="refs/heads/$BRANCH"
fi

target_has_head=0
if git -C "$TARGET_ABS" rev-parse --verify "$target_check_ref" >/dev/null 2>&1; then
  target_has_head=1
fi

# Resolve previous synced source commit from target git log trailer (REQ-0004)
# Governing: docs/openspec/specs/distribute/spec.md#req-0004
prev_source_sha=""
last_sync_commit=""
if [[ "$target_has_head" == "1" ]]; then
  last_sync_commit="$(
    git -C "$TARGET_ABS" log "$target_check_ref" --grep="^Codemender-Source-Commit:" -n 1 --format=%H 2>/dev/null || true
  )"
  if [[ -n "$last_sync_commit" ]]; then
    prev_source_sha="$(
      git -C "$TARGET_ABS" log "$last_sync_commit" -n 1 --format=%B 2>/dev/null \
        | sed -n 's/^Codemender-Source-Commit:[[:space:]]*\([^[:space:]]\{1,\}\)[[:space:]]*$/\1/p' \
        | tail -n 1
    )"
  fi
fi

if [[ -n "$prev_source_sha" ]]; then
  if ! git -C "$SOURCE_REPO" cat-file -e "${prev_source_sha}^{commit}" 2>/dev/null; then
    log_err "Previous synced source commit '$prev_source_sha' (from Codemender-Source-Commit trailer in '$TARGET') does not exist in source repository '$SOURCE_REPO'."
    exit 2
  fi
fi

# Determine effective --include-top-level setting (REQ-0005)
# Governing: docs/openspec/specs/distribute/spec.md#req-0005
effective_include_top_level="$INCLUDE_TOP_LEVEL"
if [[ "$effective_include_top_level" == "0" && -n "$last_sync_commit" && -n "$prev_source_sha" ]]; then
  target_readme_blob="$(git -C "$TARGET_ABS" rev-parse --verify "${last_sync_commit}:README.md" 2>/dev/null || true)"
  prev_dist_readme_blob="$(git -C "$SOURCE_REPO" rev-parse --verify "${prev_source_sha}:dist/README.md" 2>/dev/null || true)"
  if [[ -n "$target_readme_blob" && -n "$prev_dist_readme_blob" && "$target_readme_blob" == "$prev_dist_readme_blob" ]]; then
    effective_include_top_level=1
  fi
fi

# Stage the new distribution tree from $SOURCE_SHA
NEW_SRC_DIR="$TMP_ROOT/new_src"
NEW_STAGE_DIR="$TMP_ROOT/new_stage"
extract_source_archive "$SOURCE_SHA" "$NEW_SRC_DIR"
stage_distribution_tree "$NEW_SRC_DIR" "$NEW_STAGE_DIR" "$effective_include_top_level"

# Stage the base distribution tree if $prev_source_sha exists
BASE_STAGE_DIR=""
if [[ -n "$prev_source_sha" ]]; then
  BASE_SRC_DIR="$TMP_ROOT/base_src"
  BASE_STAGE_DIR="$TMP_ROOT/base_stage"
  extract_source_archive "$prev_source_sha" "$BASE_SRC_DIR"
  base_dist_script=""
  if [[ -f "$BASE_SRC_DIR/scripts/distribute.sh" ]]; then
    base_dist_script="$BASE_SRC_DIR/scripts/distribute.sh"
  fi
  if [[ -n "$base_dist_script" ]] && grep -q -- "--stage-only" "$base_dist_script" 2>/dev/null; then
    stage_args=(--source-ref "$prev_source_sha" --stage-only "$BASE_STAGE_DIR")
    if [[ "$effective_include_top_level" == "1" ]]; then
      stage_args+=(--include-top-level)
    fi
    if ! CODEMENDER_SOURCE_REPO="$SOURCE_REPO" bash "$base_dist_script" "${stage_args[@]}"; then
      rm -rf "$BASE_STAGE_DIR"
      stage_distribution_tree "$BASE_SRC_DIR" "$BASE_STAGE_DIR" "$effective_include_top_level"
    fi
  else
    stage_distribution_tree "$BASE_SRC_DIR" "$BASE_STAGE_DIR" "$effective_include_top_level"
  fi
fi

write_dir_to_git_tree() {
  local git_dir="$1"
  local work_dir="$2"
  local idx_file="$TMP_ROOT/git_index.$RANDOM.$$"
  rm -f "$idx_file"
  GIT_INDEX_FILE="$idx_file" git --git-dir="$git_dir" --work-tree="$work_dir" add -f -A
  local tree_sha
  tree_sha="$(GIT_INDEX_FILE="$idx_file" git --git-dir="$git_dir" write-tree)"
  rm -f "$idx_file"
  printf '%s\n' "$tree_sha"
}

TARGET_GIT_DIR="$(git -C "$TARGET_ABS" rev-parse --absolute-git-dir)"

# Handle --dry-run using an ephemeral object store so --target is 100% untouched
if [[ "$DRY_RUN" == "1" ]]; then
  DRY_GIT_DIR="$TMP_ROOT/dryrun.git"
  git init --bare -q "$DRY_GIT_DIR"
  mkdir -p "$DRY_GIT_DIR/objects/info"
  printf '%s\n' "$TARGET_GIT_DIR/objects" >"$DRY_GIT_DIR/objects/info/alternates"

  new_tree="$(write_dir_to_git_tree "$DRY_GIT_DIR" "$NEW_STAGE_DIR")"
  if [[ -n "$BASE_STAGE_DIR" ]]; then
    base_tree="$(write_dir_to_git_tree "$DRY_GIT_DIR" "$BASE_STAGE_DIR")"
  else
    base_tree="$EMPTY_TREE_SHA"
  fi

  log_info "Dry-run mode (--target '$TARGET' will not be modified)."
  log_info "Source commit: $SOURCE_SHA"
  if [[ "$target_has_head" == "1" && "$new_tree" == "$base_tree" ]]; then
    log_info "No distribution changes detected (already up to date)."
  else
    log_info "Staged file-level changes (base -> source-ref):"
    git --git-dir="$DRY_GIT_DIR" diff --name-status "$base_tree" "$new_tree" >&2
  fi
  exit 0
fi

# Switch or create --branch now that pre-flight validation and --dry-run have passed
if [[ -n "$BRANCH" ]]; then
  if git -C "$TARGET_ABS" show-ref --verify --quiet "refs/heads/$BRANCH"; then
    git -C "$TARGET_ABS" checkout -q "$BRANCH"
  else
    git -C "$TARGET_ABS" checkout -q -b "$BRANCH"
  fi
fi

# Write trees into the target repository's object store
new_tree="$(write_dir_to_git_tree "$TARGET_GIT_DIR" "$NEW_STAGE_DIR")"
if [[ -n "$BASE_STAGE_DIR" ]]; then
  base_tree="$(write_dir_to_git_tree "$TARGET_GIT_DIR" "$BASE_STAGE_DIR")"
else
  base_tree="$EMPTY_TREE_SHA"
fi

# Ensure git author/committer identity is available in --target
if ! git -C "$TARGET_ABS" config user.name >/dev/null 2>&1; then
  export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-CodeMender Distribute}"
  export GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-CodeMender Distribute}"
fi
if ! git -C "$TARGET_ABS" config user.email >/dev/null 2>&1; then
  export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-codemender@google.com}"
  export GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-codemender@google.com}"
fi

COMMIT_SUBJECT="chore(distribute): sync CodeMender distribution (${SOURCE_SHA:0:12})"
COMMIT_TRAILER="Codemender-Source-Commit: $SOURCE_SHA"

if [[ "$target_has_head" == "0" ]]; then
  git -C "$TARGET_ABS" read-tree --reset -u "$new_tree"
  git -C "$TARGET_ABS" commit -q -m "$COMMIT_SUBJECT" -m "$COMMIT_TRAILER"
  log_info "Initialized target repository '$TARGET' at source commit $SOURCE_SHA."
  exit 0
fi

if [[ "$new_tree" == "$base_tree" ]]; then
  log_info "Target repository '$TARGET' is already up to date with source commit $SOURCE_SHA."
  exit 0
fi

base_commit="$(git -C "$TARGET_ABS" commit-tree "$base_tree" -m "dist-base")"
new_commit="$(git -C "$TARGET_ABS" commit-tree "$new_tree" -p "$base_commit" -m "dist-new")"

if ! git -C "$TARGET_ABS" cherry-pick --no-commit "$new_commit" >/dev/null 2>&1; then
  conflicted_files="$(git -C "$TARGET_ABS" diff --name-only --diff-filter=U || true)"
  log_err "3-way merge conflict detected while syncing into '$TARGET'."
  if [[ -n "$conflicted_files" ]]; then
    log_err "Conflicting files:"
    while IFS= read -r cfile; do
      [[ -n "$cfile" ]] && log_err "  $cfile"
    done <<<"$conflicted_files"
  fi
  exit 4
fi

git -C "$TARGET_ABS" commit -q --allow-empty -m "$COMMIT_SUBJECT" -m "$COMMIT_TRAILER"
log_info "Synced target repository '$TARGET' to source commit $SOURCE_SHA."
exit 0
