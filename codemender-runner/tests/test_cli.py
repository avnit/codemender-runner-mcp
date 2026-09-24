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

"""Unit tests for the cm-runner command-line interface.

Most of these tests assert the *output contract* rather than any particular
command's behaviour:

  1. stdout carries NDJSON and nothing else.
  2. Everything human-readable goes to stderr.
  3. Exit codes are 0 / 2 / 3.

The contract is the part other workstreams depend on, and it is the part most
easily broken by an innocent-looking change elsewhere in the repo -- see
`test_importing_cli_does_not_import_main` for the specific hazard.

The contract tests run the CLI in a subprocess rather than calling `main()`
in-process. That is deliberate: an in-process test cannot see output written
by a child process or by a C-level write to fd 1, and both of those are
exactly how the contract has been broken before.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from io import StringIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli import exit_codes
from cli.commands.version import _normalise_version
from cli.events import EventEmitter
from cli.events import JSON
from cli.events import TEXT

# Directory containing the `cli` package, used as cwd so `python -m cli` works.
_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The repo's pre-existing fake cm. It implements init/find/fix/verify/report
# but *not* `--version`, on which it exits non-zero. That makes it a ready-made
# fixture for the "binary is present but will not report a version" case.
_FAKE_CM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cm")


class CLIContractTestBase(unittest.TestCase):
  """Runs the CLI in a subprocess with a controlled, empty PATH."""

  def setUp(self):
    super().setUp()
    # An empty PATH means a real `cm` installed on the developer's machine can
    # never leak into a test result. Without this, these tests would pass or
    # fail depending on whose laptop they run on.
    self._empty_dir = tempfile.mkdtemp(prefix="cm_runner_test_")
    self.addCleanup(shutil.rmtree, self._empty_dir, True)

  def run_cli(self, *args, path=None):
    """Invokes `python -m cli` and captures both streams separately.

    Args:
      *args: Arguments passed after `-m cli`.
      path: Value for PATH in the child. Defaults to an empty directory.

    Returns:
      The completed process, with text-mode stdout and stderr.
    """
    env = dict(os.environ)
    env["PATH"] = self._empty_dir if path is None else path
    # Keep the child from writing .pyc files into the source tree, and stop
    # any inherited PYTHONPATH from changing what `cli` resolves to.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONPATH", None)

    return subprocess.run(
        [sys.executable, "-m", "cli", *args],
        cwd=_PKG_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

  def assert_stdout_is_one_json_object(self, completed):
    """Asserts stdout is exactly one line and that line is a JSON object."""
    lines = completed.stdout.splitlines()
    self.assertEqual(
        len(lines),
        1,
        f"stdout must be exactly one line, got {len(lines)}:\n"
        f"{completed.stdout}",
    )
    payload = json.loads(lines[0])
    self.assertIsInstance(payload, dict)
    return payload


class TestStdoutPurity(CLIContractTestBase):
  """The output contract: stdout is machine-readable and nothing else."""

  def test_version_emits_exactly_one_json_object(self):
    """The happy path writes one NDJSON line and exits 0."""
    result = self.run_cli("version")

    self.assertEqual(result.returncode, exit_codes.OK, result.stderr)
    payload = self.assert_stdout_is_one_json_object(result)
    self.assertEqual(payload["event"], "version")
    self.assertIn("ts", payload)
    self.assertIn("contract", payload)

  def test_missing_cm_still_succeeds_with_clean_stdout(self):
    """A missing cm is reported in the payload, not as a failure.

    Hard-failing on a missing binary is `init`'s job. `version` must stay
    usable for diagnosing the very environment that is missing cm.
    """
    result = self.run_cli("version")

    self.assertEqual(result.returncode, exit_codes.OK, result.stderr)
    payload = self.assert_stdout_is_one_json_object(result)
    self.assertFalse(payload["cm"]["found"])
    self.assertIsNone(payload["cm"]["version"])
    self.assertIsNone(payload["cm"]["path"])

  def test_usage_error_exits_two_and_leaves_stdout_empty(self):
    """argparse must never write to stdout, on any error path."""
    cases = {
        "no subcommand": [],
        "unknown subcommand": ["nonsuch"],
        "unknown flag": ["version", "--nonsuch"],
        "invalid flag value": ["version", "--output", "yaml"],
    }

    for label, argv in cases.items():
      with self.subTest(case=label):
        result = self.run_cli(*argv)

        self.assertEqual(result.returncode, exit_codes.USAGE)
        self.assertEqual(
            result.stdout,
            "",
            f"{label}: stdout must stay empty, got: {result.stdout!r}",
        )
        self.assertNotEqual(result.stderr.strip(), "")

  def test_debug_logging_goes_to_stderr_not_stdout(self):
    """Raising log verbosity must not add anything to stdout.

    This is the regression test for the hazard described in
    `cli/__main__.configure_logging`: a stdout log handler installed anywhere
    in the process would surface here.
    """
    result = self.run_cli("--log-level", "debug", "version")

    self.assertEqual(result.returncode, exit_codes.OK, result.stderr)
    self.assert_stdout_is_one_json_object(result)

  def test_text_output_writes_no_json(self):
    """`--output text` swaps the renderer without polluting stdout."""
    result = self.run_cli("--output", "text", "version")

    self.assertEqual(result.returncode, exit_codes.OK, result.stderr)
    self.assertIn("cm-runner", result.stdout)
    with self.assertRaises(json.JSONDecodeError):
      json.loads(result.stdout.splitlines()[0])

  def test_global_flags_accepted_before_and_after_subcommand(self):
    """Both orderings must behave identically.

    argparse silently ignores the pre-subcommand form unless the shared flags
    use SUPPRESS, which is a failure mode that looks like success.
    """
    before = self.run_cli("--output", "text", "version")
    after = self.run_cli("version", "--output", "text")

    self.assertEqual(before.returncode, exit_codes.OK, before.stderr)
    self.assertEqual(after.returncode, exit_codes.OK, after.stderr)
    self.assertIn("cm-runner", before.stdout)
    self.assertIn("cm-runner", after.stdout)


class TestCmDetection(CLIContractTestBase):
  """Locating cm and reading its version."""

  def _write_fake_cm(self, body):
    """Creates an executable stub and returns its path."""
    directory = tempfile.mkdtemp(prefix="cm_runner_fake_")
    self.addCleanup(shutil.rmtree, directory, True)

    path = os.path.join(directory, "cm")
    with open(path, "w", encoding="utf-8") as handle:
      handle.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP)
    return path

  def test_reports_version_from_explicit_binary(self):
    """`--cm-binary` is honoured and the bare version is extracted."""
    fake = self._write_fake_cm('#!/bin/bash\necho "cm version 1.2.3"\n')

    result = self.run_cli("version", "--cm-binary", fake)

    payload = self.assert_stdout_is_one_json_object(result)
    self.assertTrue(payload["cm"]["found"])
    self.assertEqual(payload["cm"]["version"], "1.2.3")
    self.assertEqual(payload["cm"]["path"], fake)

  def test_finds_cm_on_path(self):
    """With no explicit path, cm is discovered via PATH."""
    fake = self._write_fake_cm('#!/bin/bash\necho "cm version 4.5.6"\n')

    result = self.run_cli("version", path=os.path.dirname(fake))

    payload = self.assert_stdout_is_one_json_object(result)
    self.assertTrue(payload["cm"]["found"])
    self.assertEqual(payload["cm"]["version"], "4.5.6")

  def test_binary_present_but_version_unreadable(self):
    """A cm that fails `--version` is found, but reports no version.

    Uses the repo's existing dummy cm, which rejects `--version`. The command
    must still succeed and must not leak the child's error text to stdout.
    """
    result = self.run_cli("version", "--cm-binary", _FAKE_CM)

    self.assertEqual(result.returncode, exit_codes.OK)
    payload = self.assert_stdout_is_one_json_object(result)
    self.assertTrue(payload["cm"]["found"])
    self.assertIsNone(payload["cm"]["version"])

  def test_noisy_cm_cannot_pollute_stdout(self):
    """Child process chatter is captured, never forwarded to our stdout.

    This is the failure that `detect_cm` exists to avoid: the first
    implementation reused a helper that streams child output to sys.stdout,
    producing six lines where the contract allows one.
    """
    fake = self._write_fake_cm(
        "#!/bin/bash\n"
        'echo "banner line one"\n'
        'echo "banner line two"\n'
        'echo "cm version 9.9.9"\n'
    )

    result = self.run_cli("version", "--cm-binary", fake)

    payload = self.assert_stdout_is_one_json_object(result)
    self.assertEqual(payload["cm"]["version"], "9.9.9")


class TestImportIsolation(unittest.TestCase):
  """The cli package must stay independent of the env-var pipeline."""

  def test_importing_cli_does_not_import_main(self):
    """Importing cli must not pull in main.py.

    `main.py` calls logging.basicConfig() with a StreamHandler(sys.stdout) at
    module scope, so merely importing it attaches a log handler to the event
    stream. Nothing in cli/ imports it today; this test makes sure a future
    refactor cannot quietly change that.
    """
    probe = (
        "import sys;"
        "import cli, cli.__main__, cli.events, cli.exit_codes,"
        " cli.commands.version;"
        "print('main' in sys.modules)"
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_PKG_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(
        result.stdout.strip(),
        "False",
        "cli must not import main.py; it configures logging on stdout at "
        "import time.",
    )


class TestEventEmitter(unittest.TestCase):
  """The emitter itself, exercised in-process against a StringIO."""

  def test_emit_writes_one_json_line_per_event(self):
    """Each event is a single self-contained line of JSON."""
    stream = StringIO()
    emitter = EventEmitter(stream=stream, output_format=JSON)

    emitter.emit("first", value=1)
    emitter.emit("second", value=2)

    lines = stream.getvalue().splitlines()
    self.assertEqual(len(lines), 2)
    self.assertEqual(json.loads(lines[0])["event"], "first")
    self.assertEqual(json.loads(lines[1])["value"], 2)

  def test_emit_returns_payload_including_timestamp(self):
    """The returned payload is what was written, so callers can inspect it."""
    emitter = EventEmitter(stream=StringIO(), output_format=JSON)

    payload = emitter.emit("version", orchestrator="0.1.0")

    self.assertEqual(payload["event"], "version")
    self.assertEqual(payload["orchestrator"], "0.1.0")
    self.assertRegex(payload["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

  def test_text_renderer_does_not_change_the_event(self):
    """--output text swaps only the rendering, never the data."""
    json_stream, text_stream = StringIO(), StringIO()

    from_json = EventEmitter(stream=json_stream, output_format=JSON).emit(
        "version", orchestrator="0.1.0", cm={"found": False}, contract="cm-0.9.x"
    )
    from_text = EventEmitter(stream=text_stream, output_format=TEXT).emit(
        "version", orchestrator="0.1.0", cm={"found": False}, contract="cm-0.9.x"
    )

    self.assertEqual(
        {k: v for k, v in from_json.items() if k != "ts"},
        {k: v for k, v in from_text.items() if k != "ts"},
    )
    self.assertIn("not found", text_stream.getvalue())

  def test_unknown_event_falls_back_to_key_value_text(self):
    """Text rendering must not crash on an event it has no template for."""
    stream = StringIO()
    emitter = EventEmitter(stream=stream, output_format=TEXT)

    emitter.emit("something_new", alpha=1, beta="two")

    rendered = stream.getvalue()
    self.assertIn("alpha=1", rendered)
    self.assertIn("beta=two", rendered)


class TestVersionParsing(unittest.TestCase):
  """Parsing the version string out of `cm --version` output."""

  def test_strips_the_cm_version_prefix(self):
    self.assertEqual(_normalise_version("cm version 0.9.0"), "0.9.0")

  def test_tolerates_surrounding_whitespace(self):
    self.assertEqual(_normalise_version("  cm version 0.9.0 \n"), "0.9.0")

  def test_bare_version_passes_through(self):
    self.assertEqual(_normalise_version("0.9.0"), "0.9.0")

  def test_empty_and_none_yield_none(self):
    self.assertIsNone(_normalise_version(""))
    self.assertIsNone(_normalise_version("   "))
    self.assertIsNone(_normalise_version(None))


class TestExitCodes(unittest.TestCase):
  """The exit codes are a published contract; pin their values."""

  def test_values_are_stable(self):
    """These may be added to, but never changed: CI configs depend on them."""
    self.assertEqual(exit_codes.OK, 0)
    self.assertEqual(exit_codes.USAGE, 2)
    self.assertEqual(exit_codes.FAILED, 3)


if __name__ == "__main__":
  unittest.main()
