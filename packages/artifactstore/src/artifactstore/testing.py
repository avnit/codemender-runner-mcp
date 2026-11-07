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

"""In-memory test double for ArtifactStore."""

from typing import Any, Mapping, Optional

from artifactstore.base import ArtifactStore


class InMemoryArtifactStore(ArtifactStore):
  """In-memory test double for ArtifactStore."""

  def __init__(
      self,
      findings_sarif: Optional[Mapping[str, Mapping[str, Any]]] = None,
      default_change_id: Optional[str] = None,
  ) -> None:
    self._default_change_id = default_change_id
    self._findings: dict[tuple[Optional[str], str], Mapping[str, Any]] = {}
    if findings_sarif:
      for finding_id, sarif in findings_sarif.items():
        self._findings[(default_change_id, finding_id)] = sarif

  def get_finding_sarif(
      self, finding_id: str, *, change_id: Optional[str] = None
  ) -> Optional[Mapping[str, Any]]:
    cid = change_id if change_id is not None else self._default_change_id
    if (cid, finding_id) in self._findings:
      return self._findings[(cid, finding_id)]
    return self._findings.get((None, finding_id))

  def seed_finding_sarif(
      self,
      finding_id: str,
      sarif: Mapping[str, Any],
      *,
      change_id: Optional[str] = None,
  ) -> None:
    """Seeds a single-finding SARIF document into the store."""
    cid = change_id if change_id is not None else self._default_change_id
    self._findings[(cid, finding_id)] = sarif
