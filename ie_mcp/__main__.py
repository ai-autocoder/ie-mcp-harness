"""``python -m ie_mcp``, or ``python path/to/ie_mcp/__main__.py`` straight from a clone."""
import os
import sys

if not __package__:
    # Run as a plain script: the package directory is on sys.path instead of its parent.
    # Put the parent first and drop the package directory, so the package's own module
    # names (session, protocol, ...) cannot shadow top-level modules.
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]
    sys.path.insert(0, os.path.dirname(_here))

from ie_mcp.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
