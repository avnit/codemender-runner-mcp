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

"""Abstract Base Class and exceptions for cross-workflow artifact storage."""

import abc
from typing import Any, Mapping, Optional


class ArtifactStoreError(Exception):
  """Base exception for errors encountered in artifact store operations."""


class ArtifactStoreAuthError(ArtifactStoreError):
  """Raised when auth credentials for an artifact store are invalid."""


class ArtifactStoreRateLimitError(ArtifactStoreError):
  """Raised when an artifact store request is rejected due to rate limits."""


class ArtifactStore(abc.ABC):
  """Abstract interface for storing and resolving cross-workflow artifacts.

  ArtifactStore runs exclusively on the outer CI host VM or container.
  It abstracts the retrieval and persistence of scan artifacts (single-finding
  SARIF slices, full scan reports, patches, and verification proofs) across
  asynchronous CI workflow runs and external storage platforms.
  """

  @abc.abstractmethod
  def get_finding_sarif(
      self, finding_id: str, *, change_id: Optional[str] = None
  ) -> Optional[Mapping[str, Any]]:
    """Retrieves SARIF document for a finding from previous scan artifacts.

    Queries the artifact store for scan reports published during earlier
    pull or merge request workflows, extracting the single-finding SARIF
    document matching `finding_id`.

    Args:
      finding_id: Unique identifier/fingerprint of the finding to retrieve.
      change_id: Optional change or pull request identifier. If omitted,
        the store uses its bound change context.

    Returns:
      Mapping representing the single-result SARIF 2.1.0 document if found,
      or None if the artifact is missing, expired, inaccessible, or does not
      contain the requested finding.

    Raises:
      ArtifactStoreAuthError: If authentication fails or token is missing.
      ArtifactStoreRateLimitError: If platform rate limit is exceeded.
      ArtifactStoreError: If the platform API returns an unrecoverable error.
    """
