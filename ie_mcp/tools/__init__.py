"""Importing this package registers every tool with ``ie_mcp.protocol.REGISTRY``.

The import order is the order tools/list reports them in.
"""
# isort: skip_file
from ie_mcp.tools import navigation  # noqa: F401
from ie_mcp.tools import read  # noqa: F401
from ie_mcp.tools import interact  # noqa: F401
from ie_mcp.tools import windows  # noqa: F401
from ie_mcp.tools import dialogs  # noqa: F401
from ie_mcp.tools import console  # noqa: F401
from ie_mcp.tools import network  # noqa: F401
from ie_mcp.tools import attach  # noqa: F401
from ie_mcp.tools import diagnostics  # noqa: F401
