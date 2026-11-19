"""Network tools (plan Task 13): ``ie_network``, ``ie_request`` and ``ie_network_clear``.

They read the session's ``CaptureStore`` (filled by the recording proxy and by the XHR records
of the page instrumentation) and never need the driver, so they work while the browser is busy
or stuck in a dialog. Everything in the store is already redacted.
"""
import codecs
import urllib.parse
import zlib
from datetime import datetime, timezone

from ie_mcp import config
from ie_mcp.capture.redact import parse_content_type
from ie_mcp.protocol import text_result, tool
from ie_mcp.session import SESSION
from ie_mcp.tools.windows import tool_error

DEFAULT_LIMIT = 50
MAX_LIMIT = 1000
DEFAULT_BODY_LIMIT = 20000
# The most a decompressed body may grow to: a few bytes of gzip can expand without bound.
MAX_DECODED = 4 << 20

HTTPS_NOTE = "HTTPS: tunnel only (host, timing)"
CACHE_NOTE = ("responses served from the browser cache are not seen (IE_CAPTURE_NO_CACHE=1 turns "
              "revalidations into full fetches; fresh cache hits stay invisible)")
CAPTURE_OFF_NOTE = ("network capture is off (IE_CAPTURE=0): only XHR recorded by the page "
                    "instrumentation (source page) is listed")
CAPTURE_BLIND_NOTE = ("network capture is off: the browser did not use the proxy; only XHR "
                      "recorded by the page instrumentation (source page) is listed")

BINARY_TYPES = frozenset((
    "application/octet-stream", "application/zip", "application/pdf", "application/gzip",
    "application/x-gzip", "application/x-zip-compressed", "application/x-shockwave-flash",
    "application/msword", "application/vnd.ms-excel", "application/vnd.ms-powerpoint"))
BINARY_PREFIXES = ("image/", "audio/", "video/", "font/")
WIDE_CODECS = ("utf-16", "utf-32")
FORM_TYPE = "application/x-www-form-urlencoded"


# --- arguments -----------------------------------------------------------------------------------
def _int_arg(args, name, default, minimum):
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def _store():
    return SESSION.store


# --- ie_network ----------------------------------------------------------------------------------
def _header(headers, name):
    for key, value in headers:
        if key.strip().lower() == name:
            return value
    return ""


def _size(entry):
    """Response body size for the listing: the stored length, or Content-Length when the
    stored body was cut. ``-`` where there is no response body to speak of."""
    if entry.tunnel or entry.source == "page":
        return "-"
    if entry.truncated:
        declared = _header(entry.resp_headers, "content-length").strip()
        if declared.isdigit():
            return declared
    return str(len(entry.resp_body))


def entry_line(entry):
    """``id source method status duration_ms size url``."""
    status = "-" if entry.status is None else str(entry.status)
    return (f"{entry.id} {entry.source} {entry.method} {status} {entry.duration_ms} "
            f"{_size(entry)} {entry.url}")


@tool(
    "ie_network",
    "List the HTTP requests the browser made, captured by the local recording proxy (and the "
    "XHR calls the page instrumentation saw, source page): one line per request, "
    "'id source method status duration_ms size url', oldest first. Filter with url_contains, "
    "method and status; pass the last id seen as since_id to page (limit, default 50). "
    "Ie_request shows one entry's headers and bodies. HTTPS shows only the tunnel (host, "
    "timing). The initial URL of a session and responses served from the browser cache are "
    "not seen. Needs no browser call: it works while the page is busy or blocked. "
    "IE_CAPTURE=0 turns the proxy off.",
    {
        "type": "object",
        "properties": {
            "url_contains": {"type": "string"},
            "method": {"type": "string"},
            "status": {"type": "integer"},
            "since_id": {"type": "integer", "description": "only entries with a higher id (default 0)"},
            "limit": {"type": "integer",
                      "description": f"at most this many, oldest first (default {DEFAULT_LIMIT})"},
        },
    },
    needs_driver=False,
)
def t_network(args):
    try:
        since_id = _int_arg(args, "since_id", 0, 0)
        limit = min(_int_arg(args, "limit", DEFAULT_LIMIT, 1), MAX_LIMIT)
        status = args.get("status")
        if status is not None:
            status = _int_arg(args, "status", None, 0)
        url_contains = args.get("url_contains") or None
        method = args.get("method") or None
        for name, value in (("url_contains", url_contains), ("method", method)):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
    except ValueError as exc:
        return tool_error(str(exc))
    store = _store()
    entries = [] if store is None else store.list(
        url_contains=url_contains, method=method, status=status, since_id=since_id, limit=limit)
    lines = [entry_line(e) for e in entries]
    notes = []
    if not entries:
        everything = [] if store is None else store.list(limit=1)
        lines.append("no matching entries" if everything else "no entries")
        if not everything:
            notes.append(CACHE_NOTE)
    elif len(entries) >= limit and store.list(
            url_contains=url_contains, method=method, status=status,
            since_id=entries[-1].id, limit=1):
        notes.append(f"{limit} entries shown; pass since_id={entries[-1].id} for the next ones")
    if any(e.tunnel for e in entries):
        notes.append(HTTPS_NOTE)
    if not config.SETTINGS.capture:
        notes.append(CAPTURE_OFF_NOTE)
    elif SESSION.capture_active is False:
        notes.append(CAPTURE_BLIND_NOTE)
    return text_result("\n".join(lines + notes))


# --- ie_request ----------------------------------------------------------------------------------
def _decompress(data, encodings):
    """``(bytes, None, cut)`` with every Content-Encoding undone (a copy), or
    ``(None, reason, False)``. ``cut``: the output hit ``MAX_DECODED`` and was cut there."""
    cut = False
    for name in reversed(encodings):
        if name in ("gzip", "x-gzip"):
            attempts = (31,)
        elif name == "deflate":
            attempts = (15, -15)  # zlib-wrapped (the standard), then raw (what some servers send)
        else:
            return None, f"{name} is not supported", False
        last = None
        for wbits in attempts:
            try:
                # A stored body may be cut at IE_CAPTURE_MAX_BODY: a truncated stream still
                # yields what it holds so far, only corrupt data raises.
                data = zlib.decompressobj(wbits).decompress(data, MAX_DECODED)
                cut = cut or len(data) >= MAX_DECODED
                break
            except zlib.error as exc:
                last = exc
        else:
            return None, str(last), False
    return data, None, cut


def _is_binary(content_type, data, charset):
    if content_type in BINARY_TYPES or content_type.startswith(BINARY_PREFIXES):
        return True
    if charset and charset.startswith(WIDE_CODECS):
        return False  # UTF-16 text is full of NUL bytes
    return b"\x00" in data[:8192]


def _codec(charset):
    """The Python codec name for a charset label, or None when absent or unknown."""
    try:
        return codecs.lookup(charset).name if charset else None
    except (LookupError, ValueError):
        return None


def _charset_for(headers, referer_charset):
    """The codec of a body: Content-Type's charset, then the requesting document's, then
    cp1252 (the legacy default of IE on Western Windows)."""
    declared = parse_content_type(_header(headers, "content-type"))[1].get("charset")
    for candidate in (declared, referer_charset):
        name = _codec(candidate)
        if name:
            return name
    return "cp1252"


def _form_text(data, codec):
    """A urlencoded form body as text: each key and value percent-decoded in ``codec`` (IE posts
    a form in the document's charset, so ``M%FCller`` is only readable with that charset)."""
    pairs = []
    for part in data.split(b"&"):
        key, sep, value = part.partition(b"=")
        decoded = [urllib.parse.unquote_to_bytes(item.replace(b"+", b" ")).decode(codec, "replace")
                   for item in (key, value)]
        pairs.append(decoded[0] + (sep.decode("ascii") + decoded[1] if sep else ""))
    return "&".join(pairs)


def decode_body(headers, body, referer_charset, limit):
    """``(text, notes)`` of a stored body: decompressed (a copy), summarised when binary, else
    decoded with the charset from Content-Type, then ``referer_charset``, then cp1252."""
    if not body:
        return "", []
    notes = []
    data = body
    encodings = [t.strip().lower() for t in _header(headers, "content-encoding").split(",")
                 if t.strip() and t.strip().lower() != "identity"]
    if encodings:
        data, reason, cut = _decompress(body, encodings)
        if cut:
            notes.append(f"the decoded body was cut at {MAX_DECODED} bytes")
        if data is None:
            return (f"<{', '.join(encodings)} body of {len(body)} bytes could not be decoded: "
                    f"{reason}>", notes)
    content_type = parse_content_type(_header(headers, "content-type"))[0]
    codec = _charset_for(headers, referer_charset)
    if _is_binary(content_type, data, codec):
        return f"<binary {len(data)} bytes>", notes
    text = _form_text(data, codec) if content_type == FORM_TYPE else data.decode(codec, "replace")
    if len(text) > limit:
        notes.append(f"{len(text) - limit} more characters not shown (raise body_limit)")
        text = text[:limit]
    return text, notes


def _stamp(seconds):
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"
    except (OverflowError, OSError, ValueError):
        return "?"


def _headers_block(title, headers):
    out = [f"== {title} =="]
    out.extend(f"{key}: {value}" for key, value in headers)
    if not headers:
        out.append("(none)")
    return out


def _body_block(title, headers, body, referer_charset, limit, truncated):
    text, notes = decode_body(headers, body, referer_charset, limit)
    out = [f"== {title} ({len(body)} bytes stored{', cut at IE_CAPTURE_MAX_BODY' if truncated else ''}) =="]
    out.append(text if body else "(empty)")
    out.extend(f"({note})" for note in notes)
    return out


@tool(
    "ie_request",
    "Show one captured request (id from ie_network): method, URL, status, timing, request and "
    "response headers and bodies. Bodies are decoded with the charset from Content-Type, then "
    "the charset of the document that made the request, then cp1252; a form body is "
    "percent-decoded the same way; gzip/deflate responses are decoded from a copy; binary "
    "bodies are summarised as <binary N bytes>. body_limit (default 20000) caps the characters "
    "shown per body. Secrets are redacted. An HTTPS entry is only a tunnel (host, timing).",
    {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "body_limit": {"type": "integer",
                           "description": f"characters shown per body (default {DEFAULT_BODY_LIMIT})"},
        },
        "required": ["id"],
    },
    needs_driver=False,
)
def t_request(args):
    try:
        entry_id = _int_arg(args, "id", None, 1)
        limit = _int_arg(args, "body_limit", DEFAULT_BODY_LIMIT, 0)
    except ValueError as exc:
        return tool_error(str(exc))
    store = _store()
    entry = None if store is None else store.get(entry_id)
    if entry is None:
        return tool_error(f"no captured request with id {entry_id} (ie_network lists them; "
                          "ie_network_clear forgets them)")
    status = "-" if entry.status is None else entry.status
    lines = [f"{entry.method} {entry.url}",
             f"id: {entry.id}  source: {entry.source}  status: {status}  "
             f"duration: {entry.duration_ms} ms  started: {_stamp(entry.started)}"]
    if entry.error:
        lines.append(f"error: {entry.error}")
    if entry.tunnel:
        lines.append(HTTPS_NOTE + ": the content of this connection is encrypted and not captured")
    lines += _headers_block("request headers", entry.req_headers)
    if not entry.tunnel:
        lines += _body_block("request body", entry.req_headers, entry.req_body,
                             entry.referer_charset, limit, entry.truncated and bool(entry.req_body))
        if entry.source == "page":
            lines.append("== response == (not captured: seen by the page instrumentation only)")
        else:
            lines += _headers_block("response headers", entry.resp_headers)
            lines += _body_block("response body", entry.resp_headers, entry.resp_body,
                                 entry.referer_charset, limit,
                                 entry.truncated and bool(entry.resp_body))
    return text_result("\n".join(lines))


# --- ie_network_clear ---------------------------------------------------------------------------
@tool(
    "ie_network_clear",
    "Forget every captured request ie_network holds (ids keep counting).",
    {"type": "object", "properties": {}},
    needs_driver=False,
)
def t_network_clear(args):
    store = _store()
    count = 0
    if store is not None:
        count = len(store.list(limit=None))
        store.clear()
    return text_result(f"cleared {count} captured requests")
