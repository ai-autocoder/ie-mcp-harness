"""In-memory store of captured HTTP exchanges (a ring buffer), with optional JSONL copy.

Everything that enters the store goes through the ``Redactor`` first. The store never
imports ``config`` or ``session``: the caller passes the redactor and the settings.
"""
import dataclasses
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ie_mcp.capture.redact import Redactor, parse_content_type


@dataclass
class NetworkEntry:
    id: int = 0  # assigned by CaptureStore.add
    started: float = 0.0
    duration_ms: int = 0
    method: str = ""
    url: str = ""
    status: int | None = None
    req_headers: list[tuple[str, str]] = field(default_factory=list)
    req_body: bytes = b""
    resp_headers: list[tuple[str, str]] = field(default_factory=list)
    resp_body: bytes = b""
    tunnel: bool = False  # a CONNECT tunnel: only the host is visible
    truncated: bool = False  # a body was cut at the store's max_body
    error: str | None = None
    source: str = "proxy"  # "proxy" or "page"
    # Charset of the document that issued the request: a form is posted in it when the
    # request's own Content-Type names none.
    referer_charset: str | None = None


def _header(headers, name):
    for key, value in headers:
        if key.strip().lower() == name:
            return value
    return ""


def _charset(headers):
    return parse_content_type(_header(headers, "content-type"))[1].get("charset") or None


def _text(body, charset):
    """A body as text for JSONL: its charset when known, else UTF-8; never raises."""
    for name in (charset, "utf-8"):
        if not name:
            continue
        try:
            return body.decode(name, "replace")
        except (LookupError, ValueError):
            continue
    return body.decode("utf-8", "replace")


class CaptureStore:
    """Thread-safe ring buffer of ``NetworkEntry`` with redaction applied on the way in.

    Ids are assigned from 1 and keep counting after ``clear()``, so a caller paging with
    ``since_id`` never sees an id twice. When ``jsonl_path`` is given, each added entry is
    also appended there as one JSON object, after redaction. Entries read back are masked
    again with any secret learned after they were stored (the JSONL copy cannot be).
    """

    def __init__(self, redactor: Redactor, max_entries: int = 2000, max_body: int = 262144,
                 jsonl_path: Path | None = None):
        self.redactor = redactor
        self.max_entries = max(1, int(max_entries))
        self.max_body = max(0, int(max_body))
        self.jsonl_path = Path(jsonl_path) if jsonl_path else None
        self.jsonl_error = None  # last JSONL write failure; the store keeps working
        self._lock = threading.Lock()
        self._next_id = 1
        # id -> [entry, redactor.version it was masked with]; insertion order is id order.
        self._entries = {}

    # --- redaction -----------------------------------------------------------
    @staticmethod
    def _charsets(entry):
        return (_charset(entry.req_headers), _charset(entry.resp_headers), entry.referer_charset)

    def _mask(self, entry):
        """``entry`` (a copy) with every known secret value masked."""
        redactor = self.redactor
        return dataclasses.replace(
            entry,
            url=redactor.mask(entry.url),
            req_headers=[(k, redactor.mask(v)) for k, v in entry.req_headers],
            resp_headers=[(k, redactor.mask(v)) for k, v in entry.resp_headers],
            req_body=redactor.mask_bytes(entry.req_body, self._charsets(entry)),
            resp_body=redactor.mask_bytes(entry.resp_body, self._charsets(entry)),
            error=redactor.mask(entry.error) if entry.error else entry.error)

    def _redact(self, entry):
        """Record the secrets in the request, then in the response, then mask everything.
        Redaction sees the full bodies; they are capped afterwards."""
        redactor = self.redactor
        request_charset = _charset(entry.req_headers) or entry.referer_charset
        response_charset = _charset(entry.resp_headers)
        url = redactor.redact_url(entry.url)
        req_headers = redactor.redact_headers(list(entry.req_headers))
        req_body = redactor.redact_body(
            _header(entry.req_headers, "content-type"), entry.req_body, request_charset)
        resp_headers = redactor.redact_headers(list(entry.resp_headers))
        resp_body = redactor.redact_body(
            _header(entry.resp_headers, "content-type"), entry.resp_body, response_charset)
        return self._mask(dataclasses.replace(
            entry, url=url, req_headers=req_headers, req_body=req_body,
            resp_headers=resp_headers, resp_body=resp_body))

    # --- writing -------------------------------------------------------------
    def add(self, entry: NetworkEntry) -> int:
        """Redact, cap the bodies at ``max_body``, assign an id and store; returns the id."""
        # Outside the lock: redaction of a large body must not stall other threads.
        version = self.redactor.version
        stored = self._redact(entry)
        truncated = stored.truncated
        if len(stored.req_body) > self.max_body:
            stored.req_body, truncated = stored.req_body[:self.max_body], True
        if len(stored.resp_body) > self.max_body:
            stored.resp_body, truncated = stored.resp_body[:self.max_body], True
        stored.truncated = truncated
        with self._lock:
            stored.id = self._next_id
            self._next_id += 1
            self._entries[stored.id] = [stored, version]
            while len(self._entries) > self.max_entries:
                del self._entries[next(iter(self._entries))]
            if self.jsonl_path is not None:
                self._write_jsonl(stored)
            return stored.id

    def _write_jsonl(self, entry):
        record = {
            "id": entry.id, "started": entry.started, "duration_ms": entry.duration_ms,
            "method": entry.method, "url": entry.url, "status": entry.status,
            "req_headers": [list(pair) for pair in entry.req_headers],
            "req_body": _text(entry.req_body, _charset(entry.req_headers) or entry.referer_charset),
            "resp_headers": [list(pair) for pair in entry.resp_headers],
            "resp_body": _text(entry.resp_body, _charset(entry.resp_headers)),
            "tunnel": entry.tunnel, "truncated": entry.truncated, "error": entry.error,
            "source": entry.source,
        }
        try:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.jsonl_path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record) + "\n")
        except (OSError, ValueError) as exc:
            self.jsonl_error = str(exc)

    # --- reading -------------------------------------------------------------
    def _current(self, slot):
        """The stored entry, masked again first if a secret was learned since. Lock held."""
        version = self.redactor.version
        if slot[1] != version:
            slot[0] = self._mask(slot[0])
            slot[1] = version
        # The header lists are copied too: a caller must not be able to change the stored entry.
        return dataclasses.replace(slot[0], req_headers=list(slot[0].req_headers),
                                   resp_headers=list(slot[0].resp_headers))

    def list(self, url_contains=None, method=None, status=None, since_id=0, limit=50):
        """Matching entries, oldest first. ``limit`` keeps the oldest ``limit`` of those with
        ``id > since_id``, so a caller can page by passing the last id it saw."""
        wanted_method = method.upper() if method else None
        out = []
        with self._lock:
            for slot in self._entries.values():
                entry = slot[0]
                if entry.id <= since_id:
                    continue
                if wanted_method and entry.method.upper() != wanted_method:
                    continue
                if status is not None and entry.status != status:
                    continue
                if limit is not None and len(out) >= limit:
                    break
                entry = self._current(slot)
                if url_contains and url_contains not in entry.url:
                    continue
                out.append(entry)
        return out

    def get(self, id):
        with self._lock:
            slot = self._entries.get(id)
            return self._current(slot) if slot else None

    def clear(self):
        with self._lock:
            self._entries.clear()
