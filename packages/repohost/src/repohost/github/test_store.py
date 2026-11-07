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

"""Unit tests for repohost.github.store (GitHubActionsArtifactStore)."""

import io
import json
from unittest.mock import MagicMock
import unittest
import zipfile

from artifactstore import (
    ArtifactStore,
    ArtifactStoreAuthError,
    ArtifactStoreError,
    ArtifactStoreRateLimitError,
)
from repohost.github.store import GitHubActionsArtifactStore


def _mock_response(json_body, status_code=200, headers=None, text=None):
  resp = MagicMock()
  resp.status_code = status_code
  resp.json.return_value = json_body
  resp.headers = headers or {}
  if text is not None:
    resp.text = text
  else:
    resp.text = json.dumps(json_body) if json_body is not None else ""
  return resp


class TestGitHubActionsArtifactStore(unittest.TestCase):
  """Tests for GitHubActionsArtifactStore implementation."""

  def setUp(self):
    self.mock_session = MagicMock()

  def test_is_instance_of_artifact_store(self):
    store = GitHubActionsArtifactStore(
        repo="owner/repo", pr_number=42, token="tok"
    )
    self.assertIsInstance(store, ArtifactStore)

  def test_init_empty_repo_raises_error(self):
    with self.assertRaises(ArtifactStoreError):
      GitHubActionsArtifactStore(repo="", pr_number=42, token="tok")
    with self.assertRaises(ArtifactStoreError):
      GitHubActionsArtifactStore(repo="   ", pr_number=42, token="tok")

  def test_get_finding_sarif_success(self):
    sarif_doc = {
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "cm", "rules": [{"id": "R1"}]}},
            "results": [{
                "ruleId": "R1",
                "level": "error",
                "message": {"text": "vulnerability"},
                "partialFingerprints": {
                    "primaryLocationLineHash": "finding_abc"
                },
                "locations": [{
                    "physicalLocation": {
                        "artifactLocation": {"uri": "src/app.py"},
                        "region": {"startLine": 10},
                    }
                }],
            }],
        }],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
      zf.writestr("findings/finding-1.sarif", json.dumps(sarif_doc))

    zip_resp = _mock_response({}, 200)
    zip_resp.content = buf.getvalue()
    self.mock_session.get.side_effect = [
        _mock_response({"artifacts": [{"id": 101, "expired": False}]}),
        zip_resp,
    ]

    store = GitHubActionsArtifactStore(
        repo="owner/repo",
        pr_number=42,
        token="tok123",
        session=self.mock_session,
    )
    res = store.get_finding_sarif("finding_abc")
    self.assertIsNotNone(res)
    self.assertEqual(res["runs"][0]["results"][0]["ruleId"], "R1")

  def test_get_finding_sarif_with_explicit_change_id(self):
    sarif_doc = {
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "cm"}},
            "results": [{
                "ruleId": "R2",
                "partialFingerprints": {"primaryLocationLineHash": "f-99"},
            }],
        }],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
      zf.writestr("report.sarif", json.dumps(sarif_doc))

    zip_resp = _mock_response({}, 200)
    zip_resp.content = buf.getvalue()
    self.mock_session.get.side_effect = [
        _mock_response({"artifacts": [{"id": 202, "expired": False}]}),
        zip_resp,
    ]

    store = GitHubActionsArtifactStore(
        repo="owner/repo",
        token="tok123",
        session=self.mock_session,
    )
    res = store.get_finding_sarif("f-99", change_id="88")
    self.assertIsNotNone(res)
    self.assertEqual(res["runs"][0]["results"][0]["ruleId"], "R2")

  def test_get_finding_sarif_missing_pr_number_raises_error(self):
    store = GitHubActionsArtifactStore(
        repo="owner/repo",
        token="tok123",
        session=self.mock_session,
    )
    with self.assertRaises(ArtifactStoreError) as ctx:
      store.get_finding_sarif("f1")
    self.assertIn("requires a pull request number", str(ctx.exception))

  def test_get_finding_sarif_missing_artifact_returns_none(self):
    self.mock_session.get.return_value = _mock_response({"artifacts": []})
    store = GitHubActionsArtifactStore(
        repo="owner/repo",
        pr_number=42,
        token="tok123",
        session=self.mock_session,
    )
    res = store.get_finding_sarif("finding_abc")
    self.assertIsNone(res)

  def test_get_finding_sarif_not_found_in_artifact_returns_none(self):
    sarif_doc = {
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "cm"}},
            "results": [{
                "ruleId": "R1",
                "partialFingerprints": {"primaryLocationLineHash": "other"},
            }],
        }],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
      zf.writestr("findings/finding-2.sarif", json.dumps(sarif_doc))

    zip_resp = _mock_response({}, 200)
    zip_resp.content = buf.getvalue()
    self.mock_session.get.side_effect = [
        _mock_response({"artifacts": [{"id": 102, "expired": False}]}),
        zip_resp,
    ]
    store = GitHubActionsArtifactStore(
        repo="owner/repo",
        pr_number=42,
        token="tok123",
        session=self.mock_session,
    )
    res = store.get_finding_sarif("finding_abc")
    self.assertIsNone(res)

  def test_get_finding_sarif_missing_token_raises_auth_error(self):
    store = GitHubActionsArtifactStore(
        repo="owner/repo",
        pr_number=42,
        token="",
        session=self.mock_session,
    )
    with self.assertRaises(ArtifactStoreAuthError):
      store.get_finding_sarif("finding_abc")

  def test_get_finding_sarif_skips_oversized_file(self):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
      info = zipfile.ZipInfo("findings/huge.sarif")
      info.file_size = 60 * 1024 * 1024  # > 50 MB limit
      zf.writestr(info, "{}")

    zip_resp = _mock_response({}, 200)
    zip_resp.content = buf.getvalue()
    self.mock_session.get.side_effect = [
        _mock_response({"artifacts": [{"id": 103, "expired": False}]}),
        zip_resp,
    ]
    store = GitHubActionsArtifactStore(
        repo="owner/repo",
        pr_number=42,
        token="tok123",
        session=self.mock_session,
    )
    res = store.get_finding_sarif("finding_abc")
    self.assertIsNone(res)

  def test_get_finding_sarif_with_direct_constructor_context(self):
    sarif_doc = {
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "cm"}},
            "results": [{
                "ruleId": "R1",
                "partialFingerprints": {"primaryLocationLineHash": "f-dir"},
            }],
        }],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
      zf.writestr("findings/finding-1.sarif", json.dumps(sarif_doc))

    zip_resp = _mock_response({}, 200)
    zip_resp.content = buf.getvalue()
    self.mock_session.get.side_effect = [
        _mock_response({"artifacts": [{"id": 501, "expired": False}]}),
        zip_resp,
    ]
    store = GitHubActionsArtifactStore(
        repo="direct/repo",
        pr_number=99,
        token="tok123",
        session=self.mock_session,
    )
    res = store.get_finding_sarif("f-dir")
    self.assertIsNotNone(res)
    self.assertEqual(res["runs"][0]["results"][0]["ruleId"], "R1")
    first_call_url = self.mock_session.get.call_args_list[0][0][0]
    self.assertIn("direct/repo/actions/artifacts", first_call_url)
    self.assertIn("codemender-report-pr-99", first_call_url)

  def test_get_finding_sarif_invalid_change_id_raises_error(self):
    store = GitHubActionsArtifactStore(
        repo="owner/repo",
        pr_number=42,
        token="tok123",
        session=self.mock_session,
    )
    with self.assertRaises(ArtifactStoreError) as ctx:
      store.get_finding_sarif("f1", change_id="not-a-number")
    self.assertIn("Invalid change_id", str(ctx.exception))

    with self.assertRaises(ArtifactStoreError):
      store.get_finding_sarif("f1", change_id="-5")

    with self.assertRaises(ArtifactStoreError):
      store.get_finding_sarif("f1", change_id="0")

  def test_get_finding_sarif_handles_auth_error_401(self):
    self.mock_session.get.return_value = _mock_response(
        None, status_code=401, text="Bad credentials"
    )
    store = GitHubActionsArtifactStore(
        repo="owner/repo",
        pr_number=42,
        token="bad-token",
        session=self.mock_session,
    )
    with self.assertRaises(ArtifactStoreAuthError):
      store.get_finding_sarif("finding_abc")

  def test_get_finding_sarif_handles_rate_limit_403(self):
    self.mock_session.get.return_value = _mock_response(
        None,
        status_code=403,
        headers={"x-ratelimit-remaining": "0"},
        text="API rate limit exceeded",
    )
    store = GitHubActionsArtifactStore(
        repo="owner/repo",
        pr_number=42,
        token="test-token",
        session=self.mock_session,
    )
    with self.assertRaises(ArtifactStoreRateLimitError):
      store.get_finding_sarif("finding_abc")

  def test_get_finding_sarif_handles_rate_limit_429(self):
    self.mock_session.get.return_value = _mock_response(
        None, status_code=429, text="Too Many Requests"
    )
    store = GitHubActionsArtifactStore(
        repo="owner/repo",
        pr_number=42,
        token="test-token",
        session=self.mock_session,
    )
    with self.assertRaises(ArtifactStoreRateLimitError):
      store.get_finding_sarif("finding_abc")

  def test_get_finding_sarif_handles_server_error_500(self):
    self.mock_session.get.return_value = _mock_response(
        None, status_code=500, text="Internal Server Error"
    )
    store = GitHubActionsArtifactStore(
        repo="owner/repo",
        pr_number=42,
        token="test-token",
        session=self.mock_session,
    )
    with self.assertRaises(ArtifactStoreError):
      store.get_finding_sarif("finding_abc")

  def test_get_finding_sarif_zip_download_auth_error_401(self):
    self.mock_session.get.side_effect = [
        _mock_response({"artifacts": [{"id": 105, "expired": False}]}),
        _mock_response(None, status_code=401, text="Bad credentials"),
    ]
    store = GitHubActionsArtifactStore(
        repo="owner/repo",
        pr_number=42,
        token="tok123",
        session=self.mock_session,
    )
    with self.assertRaises(ArtifactStoreAuthError):
      store.get_finding_sarif("finding_abc")


if __name__ == "__main__":
  unittest.main()
