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

# Governing: ADR-0008

"""GitHub repository hosting platform integration."""

from repohost.github.branches import (
    build_review_comment,
    check_remote_branch_exists,
    create_pr_comment,
    create_pr_review_with_suggestions,
    create_pull_request,
    delete_remote_branch,
    format_suggestion_body,
    get_default_branch,
    is_duplicate_pr,
    list_reviewed_finding_ids,
    post_commit_status,
)
from repohost.github.client import (
    FINDING_TAG_RE,
    GITHUB_API_URL,
    MAX_COMMENT_BODY_CHARS,
    ReviewResult,
    STICKY_SUMMARY_MARKER,
    code_fence,
    create_review_with_suggestions,
    delete_codemender_review_comments,
    delete_review_comment,
    finding_tag,
    format_patch_comment,
    get_pr_diff_line_ranges,
    post_or_update_sticky_comment,
    post_patch_comment,
    reply_to_comment,
    retry_on_exception,
    truncate_comment_body,
)
from repohost.github.fix_comments import (
    FAILED,
    FIXED,
    FixCell,
    FixPlan,
    FixPoster,
    NO_FIX,
    PATCH,
    SUGGESTED,
    UNTESTED_NOTE,
    WORKING,
    checked_out_commit,
    plan_fixes,
    same_lines_note,
)
from repohost.github.repo_host import GitHubRepoHost
from repohost.github.store import GitHubActionsArtifactStore
