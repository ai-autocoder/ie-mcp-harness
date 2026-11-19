"""Run the integration tests (tests/integration/it_*.py) against a real IE-mode window.

    python scripts/run_integration.py            # every it_*.py module
    python scripts/run_integration.py it_modal   # one module

The tests open a real, visible Edge IE-mode window and need an unlocked desktop. This runner
sets ``IE_MCP_INTEGRATION=1`` itself (the tests skip without it) and exits 1 on any failure,
any error, any skipped test or when it found no test at all: "all OK" must mean that every
test really ran.
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_VAR = "IE_MCP_INTEGRATION"


def _top_level_dir(start_dir):
    """The repository root when ``start_dir`` is inside it (so ``tests.integration.base``
    imports), else ``start_dir`` itself (unittest cannot discover outside its top level)."""
    try:
        start_dir.relative_to(ROOT)
    except ValueError:
        return start_dir
    return ROOT


def main(argv=None, start_dir="tests/integration"):
    """Discover and run the integration tests; 0 only if every one ran and passed."""
    argv = sys.argv[1:] if argv is None else list(argv)
    start = Path(start_dir)
    start = (start if start.is_absolute() else ROOT / start).resolve()
    top = _top_level_dir(start)
    # Put the root first so the repository's own package is the one imported, not an installed copy.
    for path in (str(ROOT),) if top == ROOT else (str(ROOT), str(top)):
        if path not in sys.path:
            sys.path.insert(0, path)
    pattern = f"{argv[0]}.py" if argv else "it_*.py"
    previous = os.environ.get(ENV_VAR)
    os.environ[ENV_VAR] = "1"
    try:
        suite = unittest.defaultTestLoader.discover(
            str(start), pattern=pattern, top_level_dir=str(top))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        if previous is None:
            os.environ.pop(ENV_VAR, None)
        else:
            os.environ[ENV_VAR] = previous
    if result.testsRun == 0:
        print(f"FAILED: no integration test matched {pattern!r} in {start}", file=sys.stderr)
        return 1
    if result.skipped:
        print(f"FAILED: {len(result.skipped)} test(s) skipped; a skip is not a pass",
              file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
