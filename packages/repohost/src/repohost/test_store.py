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

"""Unit tests for ArtifactStore base class and InMemoryArtifactStore."""

from typing import Any, Mapping, Optional
import unittest

from repohost.store import (
    ArtifactStore,
    ArtifactStoreAuthError,
    ArtifactStoreError,
    ArtifactStoreRateLimitError,
)
from repohost.testing import InMemoryArtifactStore


class IncompleteStore(ArtifactStore):
  """Subclass missing abstract methods."""


class CompleteStore(ArtifactStore):
  """Subclass implementing all abstract methods."""

  def get_finding_sarif(
      self, finding_id: str, *, change_id: Optional[str] = None
  ) -> Optional[Mapping[str, Any]]:
    if finding_id == "f-found":
      return {"version": "2.1.0", "runs": []}
    return None


class TestArtifactStoreBase(unittest.TestCase):
  """Tests for ArtifactStore ABC contract."""

  def test_cannot_instantiate_abc_directly(self):
    with self.assertRaises(TypeError):
      ArtifactStore()  # type: ignore[abstract]

  def test_cannot_instantiate_incomplete_subclass(self):
    with self.assertRaises(TypeError) as ctx:
      IncompleteStore()  # type: ignore[abstract]
    self.assertIn("abstract method", str(ctx.exception).lower())

  def test_can_instantiate_complete_subclass(self):
    store = CompleteStore()
    self.assertIsInstance(store, ArtifactStore)
    self.assertIsNotNone(store.get_finding_sarif("f-found"))
    self.assertIsNone(store.get_finding_sarif("f-missing"))

  def test_exception_hierarchy(self):
    self.assertTrue(issubclass(ArtifactStoreAuthError, ArtifactStoreError))
    self.assertTrue(
        issubclass(ArtifactStoreRateLimitError, ArtifactStoreError)
    )


class TestInMemoryArtifactStore(unittest.TestCase):
  """Tests for InMemoryArtifactStore test double."""

  def test_seeded_findings(self):
    sarif = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "cm"}}}]}
    store = InMemoryArtifactStore(
        findings_sarif={"f-101": sarif},
        default_change_id="42",
    )
    self.assertEqual(store.get_finding_sarif("f-101"), sarif)
    self.assertEqual(store.get_finding_sarif("f-101", change_id="42"), sarif)
    self.assertIsNone(store.get_finding_sarif("f-999"))

  def test_seed_dynamically(self):
    store = InMemoryArtifactStore()
    sarif = {"version": "2.1.0", "runs": []}
    self.assertIsNone(store.get_finding_sarif("f-202"))
    store.seed_finding_sarif("f-202", sarif)
    self.assertEqual(store.get_finding_sarif("f-202"), sarif)

  def test_seed_with_specific_change_id(self):
    store = InMemoryArtifactStore(default_change_id="10")
    sarif_pr_10 = {"version": "2.1.0", "id": "pr10"}
    sarif_pr_20 = {"version": "2.1.0", "id": "pr20"}
    store.seed_finding_sarif("f-1", sarif_pr_10, change_id="10")
    store.seed_finding_sarif("f-1", sarif_pr_20, change_id="20")

    self.assertEqual(store.get_finding_sarif("f-1"), sarif_pr_10)
    self.assertEqual(
        store.get_finding_sarif("f-1", change_id="20"), sarif_pr_20
    )


if __name__ == "__main__":
  unittest.main()
