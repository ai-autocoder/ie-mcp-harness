"""Scripts injected into the page. They run in Trident, so they must stay ES3 (see CLAUDE.md)."""
from functools import lru_cache
from importlib import resources


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """Return the text of ``ie_mcp/js/<name>``."""
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8")
