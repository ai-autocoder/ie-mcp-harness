"""Unit tests for scripts/run_integration.py. No browser: the discovered tests are throwaway
modules in a temporary directory."""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_integration.py"

PASSING = """
import os
import unittest


class T(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(os.environ.get("IE_MCP_INTEGRATION"), "1")
"""

SKIPPING = """
import unittest


class T(unittest.TestCase):
    def test_skipped(self):
        self.skipTest("no desktop")
"""

FAILING = """
import unittest


class T(unittest.TestCase):
    def test_bad(self):
        self.fail("boom")
"""


def load_runner():
    spec = importlib.util.spec_from_file_location("run_integration_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunnerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.runner = load_runner()
        # The runner's own output is the unittest report: keep the test output pristine.
        for stream in ("stdout", "stderr"):
            patcher = contextlib.redirect_stdout if stream == "stdout" else contextlib.redirect_stderr
            ctx = patcher(io.StringIO())
            ctx.__enter__()
            self.addCleanup(ctx.__exit__, None, None, None)
        # Discovery imports it_*.py by bare name: forget those modules and the path entry.
        self.addCleanup(self._forget_modules)
        self.path_before = list(sys.path)
        self.addCleanup(self._restore_path)

    def _restore_path(self):
        sys.path[:] = self.path_before

    def _forget_modules(self):
        for name in [n for n in sys.modules if n.startswith("it_")]:
            del sys.modules[name]

    def write(self, name, source):
        (self.dir / name).write_bytes(source.encode("utf-8"))

    def run_main(self, argv=None):
        return self.runner.main(argv or [], str(self.dir))

    def test_runner_fails_on_skip(self):
        self.write("it_x.py", SKIPPING)
        before = os.environ.get("IE_MCP_INTEGRATION")
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(os.environ.get("IE_MCP_INTEGRATION"), before)

    def test_runner_passes_on_success(self):
        self.write("it_x.py", PASSING)
        self.assertEqual(self.run_main(), 0)

    def test_runner_fails_on_failure(self):
        self.write("it_x.py", FAILING)
        self.assertEqual(self.run_main(), 1)

    def test_runner_sets_and_restores_the_environment_variable(self):
        self.write("it_x.py", PASSING)
        os.environ.pop("IE_MCP_INTEGRATION", None)
        self.addCleanup(os.environ.pop, "IE_MCP_INTEGRATION", None)
        self.assertEqual(self.run_main(), 0)  # the test asserts the variable is "1" while it runs
        self.assertNotIn("IE_MCP_INTEGRATION", os.environ)

    def test_runner_restores_a_previous_value(self):
        self.write("it_x.py", PASSING)
        os.environ["IE_MCP_INTEGRATION"] = "previous"
        self.addCleanup(os.environ.pop, "IE_MCP_INTEGRATION", None)
        self.run_main()
        self.assertEqual(os.environ["IE_MCP_INTEGRATION"], "previous")

    def test_runner_runs_only_the_named_module(self):
        self.write("it_good.py", PASSING)
        self.write("it_bad.py", FAILING)
        self.assertEqual(self.run_main(["it_good"]), 0)

    def test_runner_fails_when_nothing_is_found(self):
        # No tests at all is not "all OK": a typo in the module name must not pass.
        self.assertEqual(self.run_main(["it_missing"]), 1)

    def test_runner_puts_the_repository_root_on_the_path(self):
        self.write("it_x.py", PASSING)
        self.run_main()
        self.assertIn(str(ROOT), sys.path)


if __name__ == "__main__":
    unittest.main()
