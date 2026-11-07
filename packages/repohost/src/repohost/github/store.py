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
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0002
# Governing: docs/openspec/specs/tech/systems/codemender-runner/spec.md#req-0003
# SPEC-GHA-VERIFY/REQ-0005

"""GitHub Actions implementation of ArtifactStore."""

import logging
import os
from typing import Any, Mapping, Optional

import requests

from artifactstore import (
    ArtifactStore,
    ArtifactStoreAuthError,
    ArtifactStoreError,
    ArtifactStoreRateLimitError,
)
from repohost.github.client import (
    GITHUB_API_URL,
    GitHubAuthError,
    GitHubClientError,
    GitHubRateLimitError,
    fetch_finding_sarif_from_artifact,
)

logger = logging.getLogger("codemender-github-artifactstore")


class GitHubActionsArtifactStore(ArtifactStore):
  """ArtifactStore implementation backed by GitHub Actions workflow artifacts.

  Retrieves scan reports and single-finding SARIF slices published as GitHub
  Actions run artifacts (e.g. `codemender-report-pr-{pr_number}`).
  """

  def __init__(
      self,
      *,
      repo: str,
      pr_number: Optional[int] = None,
      token: Optional[str] = None,
      api_url: str = GITHUB_API_URL,
      session: Optional[requests.Session] = None,
  ) -> None:
    if not repo or not repo.strip():
      raise ArtifactStoreError(
          "GitHubActionsArtifactStore requires a non-empty repository name"
          " (e.g. 'owner/repo')."
      )
    self._repo: str = repo.strip()
    self._pr_number: Optional[int] = pr_number
    self._token: str = (
        token if token is not None else os.environ.get("GITHUB_TOKEN", "")
    ).strip()
    self._api_url: str = api_url.rstrip("/")
    self._session: Optional[requests.Session] = session

  def get_finding_sarif(
      self, finding_id: str, *, change_id: Optional[str] = None
  ) -> Optional[Mapping[str, Any]]:
    """Retrieves finding SARIF from GitHub Actions run artifacts."""
    if not self._token:
      raise ArtifactStoreAuthError(
          "GitHubActionsArtifactStore requires a non-empty GitHub token."
      )

    if change_id is not None:
      try:
        pr_number = int(change_id)
        if pr_number <= 0:
          raise ValueError()
      except (ValueError, TypeError) as e:
        raise ArtifactStoreError(
            f"Invalid change_id: {change_id!r}. Must be a positive integer."
        ) from e
    else:
      pr_number = self._pr_number
      if not pr_number or pr_number <= 0:
        raise ArtifactStoreError(
            "GitHubActionsArtifactStore requires a pull request number."
        )

    try:
      return fetch_finding_sarif_from_artifact(
          self._repo,
          pr_number,
          self._token,
          finding_id,
          api_url=self._api_url,
          session=self._session,
      )
    except GitHubAuthError as e:
      raise ArtifactStoreAuthError(str(e)) from e
    except GitHubRateLimitError as e:
      raise ArtifactStoreRateLimitError(str(e)) from e
    except GitHubClientError as e:
      raise ArtifactStoreError(str(e)) from e
