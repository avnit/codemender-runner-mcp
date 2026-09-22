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

"""Unit tests for main.py entrypoint."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


class TestOrchestratorEntrypoint(unittest.TestCase):

  @patch("main.run_scan_pipeline")
  def test_main_scan_mode(self, mock_scan):
    """Verify CODEMENDER_RUN_MODE=scan dispatches to run_scan_pipeline."""
    with patch.dict(os.environ, {"CODEMENDER_RUN_MODE": "scan"}):
      main.main()
      mock_scan.assert_called_once()

  @patch("main.run_worker_pipeline")
  def test_main_worker_mode(self, mock_worker):
    """Verify CODEMENDER_RUN_MODE=worker dispatches to run_worker_pipeline."""
    with patch.dict(os.environ, {"CODEMENDER_RUN_MODE": "worker"}):
      main.main()
      mock_worker.assert_called_once()

  @patch("main.run_aggregate_pipeline")
  def test_main_aggregate_mode(self, mock_aggregate):
    """Verify CODEMENDER_RUN_MODE=aggregate dispatches to run_aggregate_pipeline."""
    with patch.dict(os.environ, {"CODEMENDER_RUN_MODE": "aggregate"}):
      main.main()
      mock_aggregate.assert_called_once()

  @patch("main.run_sequential_pipeline")
  def test_main_sequential_mode(self, mock_sequential):
    """Verify CODEMENDER_RUN_MODE=sequential dispatches to run_sequential_pipeline."""
    with patch.dict(os.environ, {"CODEMENDER_RUN_MODE": "sequential"}):
      main.main()
      mock_sequential.assert_called_once()

  @patch("main.run_sequential_pipeline")
  def test_main_fallback_mode(self, mock_sequential):
    """Verify unknown CODEMENDER_RUN_MODE falls back to sequential pipeline."""
    with patch.dict(os.environ, {"CODEMENDER_RUN_MODE": "unknown_mode"}):
      main.main()
      mock_sequential.assert_called_once()


if __name__ == "__main__":
  unittest.main()
