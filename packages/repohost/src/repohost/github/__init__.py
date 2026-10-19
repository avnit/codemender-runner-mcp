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
    FindingComment,
    GITHUB_API_URL,
    MAX_COMMENT_BODY_CHARS,
    ReviewResult,
    STICKY_SUMMARY_MARKER,
    code_fence,
    create_review_with_suggestions,
    finding_tag,
    format_patch_comment,
    get_pr_diff_line_ranges,
    list_finding_comments,
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

__all__: list[str] = [
    "FAILED",
    "FINDING_TAG_RE",
    "FIXED",
    "FindingComment",
    "FixCell",
    "FixPlan",
    "FixPoster",
    "GITHUB_API_URL",
    "MAX_COMMENT_BODY_CHARS",
    "NO_FIX",
    "PATCH",
    "ReviewResult",
    "STICKY_SUMMARY_MARKER",
    "SUGGESTED",
    "UNTESTED_NOTE",
    "WORKING",
    "build_review_comment",
    "check_remote_branch_exists",
    "checked_out_commit",
    "code_fence",
    "create_pr_comment",
    "create_pr_review_with_suggestions",
    "create_pull_request",
    "create_review_with_suggestions",
    "delete_remote_branch",
    "finding_tag",
    "format_patch_comment",
    "format_suggestion_body",
    "get_default_branch",
    "get_pr_diff_line_ranges",
    "is_duplicate_pr",
    "list_finding_comments",
    "list_reviewed_finding_ids",
    "plan_fixes",
    "post_commit_status",
    "post_or_update_sticky_comment",
    "post_patch_comment",
    "reply_to_comment",
    "retry_on_exception",
    "same_lines_note",
    "truncate_comment_body",
]
