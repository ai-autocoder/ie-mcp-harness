"""Generic legacy fixture site used by the ie-mcp tests and ``--selftest``.

The site is application-agnostic: it only reproduces legacy IE-only patterns.
"""

from .server import FixtureServer, start_fixture_server

__all__ = ["FixtureServer", "start_fixture_server"]
