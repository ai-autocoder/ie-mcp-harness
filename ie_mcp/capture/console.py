"""Console, error and dialog records drained from the pages (plan Task 12).

``instrument.js`` keeps a queue of records in each page; ``hook.js`` and ``drain.js`` hand it
over as one delimited string (JSON does not exist in IE7 or quirks mode). A raw record is
``path FS kind FS level FS time FS text``, records joined by RS (``\\x1e``/``\\x1f``). Inside
the text the page escapes the separators and the backslash: ``\\x1e``, ``\\x1f``, ``\\\\``.

Everything stored here goes through the ``Redactor`` first. This module imports neither
``config`` nor ``session``: the caller passes the redactor.
"""
import re
import threading
import time
from dataclasses import dataclass

RECORD_SEP = "\x1e"
FIELD_SEP = "\x1f"
DEFAULT_WINDOW_S = 30.0  # how old a dialog-open record may be and still describe an open dialog

_UNESCAPE = re.compile(r"\\(x1e|x1f|\\)")
_UNESCAPED = {"x1e": RECORD_SEP, "x1f": FIELD_SEP, "\\": "\\"}
_BLANKS = re.compile(r"\s+")
_LEVEL = re.compile(r"([^#@]*)(?:#([^@]*))?(@popup)?\Z")


@dataclass
class ConsoleRecord:
    id: int  # assigned by ConsoleStore.add_many
    kind: str  # error | dialog-open | dialog | modal | open | console | xhr
    level: str  # the console method, the dialog function, or "error"; "" otherwise
    time: float  # seconds since the epoch, by the page's clock
    text: str
    where: str  # the window target and the frame path: "driver", "hwnd:7", "tab:9/0/1"
    # The page-assigned id of an alert/confirm/prompt, shared by its dialog-open and dialog
    # records, whatever route each was drained by (the level field carries it as "confirm#<id>").
    dialog_id: str = ""
    popup: bool = False  # the record was handed to its opener by a pop-up (level "...@popup")


def _unescape(text):
    return _UNESCAPE.sub(lambda m: _UNESCAPED[m.group(1)], text)


def _number(text):
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_records(raw, where):
    """The ``ConsoleRecord`` list (``id`` 0) for a drained string.

    ``where`` is the window target; each record's frame path is appended to it. A malformed
    record is dropped: the page controls these bytes, and one bad record must not hide the rest.
    """
    out = []
    for item in (raw or "").split(RECORD_SEP):
        if item == "":
            continue
        fields = item.split(FIELD_SEP, 4)
        if len(fields) != 5:
            continue
        path, kind, level, stamp, text = fields
        parts = _LEVEL.match(level)
        dialog_id, popup = (parts.group(2) or "", bool(parts.group(3))) if parts else ("", False)
        out.append(ConsoleRecord(
            0, kind, parts.group(1) if parts else level, _number(stamp), _unescape(text),
            "/".join(part for part in (where, path) if part), dialog_id, popup))
    return out


def _normal(text):
    """A dialog's text without the whitespace differences between a script and a Static control."""
    return _BLANKS.sub(" ", text or "").strip()


class ConsoleStore:
    """Thread-safe ring buffer of ``ConsoleRecord`` with redaction applied on the way in.

    Ids start at 1 and keep counting after ``clear()``, so a caller paging with ``since_id``
    never sees an id twice. Records read back are masked again with any secret learned after
    they were stored.
    """

    def __init__(self, redactor, max_records: int = 5000):
        self.redactor = redactor
        self.max_records = max(1, int(max_records))
        self._lock = threading.Lock()
        self._next_id = 1
        self._records = []
        self._consumed = set()  # dialog ids that have already authorised an accept

    def add_many(self, records):
        """Redact and store ``records`` (their ids are assigned here); the stored copies."""
        stored = []
        for rec in records:
            # Outside the lock: redaction must not stall other threads.
            text = self.redactor.redact_text(rec.text)
            stored.append(ConsoleRecord(0, rec.kind, rec.level, rec.time, text, rec.where,
                                        rec.dialog_id, rec.popup))
        with self._lock:
            for rec in stored:
                rec.id = self._next_id
                self._next_id += 1
                self._records.append(rec)
            del self._records[:max(0, len(self._records) - self.max_records)]
        return stored

    def _masked(self, rec):
        mask = getattr(self.redactor, "mask", None)
        text = mask(rec.text) if mask else rec.text
        return ConsoleRecord(rec.id, rec.kind, rec.level, rec.time, text, rec.where,
                             rec.dialog_id, rec.popup)

    def list(self, since_id=0, kinds=None, limit=100):
        """Matching records with ``id > since_id``, oldest first; ``limit`` keeps the oldest
        ``limit`` of them, so a caller pages by passing the last id it saw."""
        wanted = set(kinds) if kinds else None
        with self._lock:
            snapshot = [r for r in self._records
                        if r.id > since_id and (wanted is None or r.kind in wanted)]
        if limit is not None:
            snapshot = snapshot[:limit]
        return [self._masked(r) for r in snapshot]

    def clear(self):
        """Forget every record; the number removed."""
        with self._lock:
            removed = len(self._records)
            self._records.clear()
            # _consumed is kept: a cleared record must not become usable again if it is drained
            # a second time (ids are unique per page, so it cannot collide with a new dialog).
        return removed

    def _recent(self, within_s):
        cutoff = time.time() - within_s
        with self._lock:
            return [r for r in self._records if r.time >= cutoff]

    def find_dialog(self, text, within_s=DEFAULT_WINDOW_S):
        """The newest ``dialog-open`` record whose text is ``text`` (whitespace-insensitive)
        and at most ``within_s`` old, or None. Evidence for ``ie_dialog``."""
        wanted = _normal(text)
        for rec in reversed(self._recent(within_s)):
            if rec.kind == "dialog-open" and _normal(rec.text) == wanted:
                return self._masked(rec)
        return None

    def _pending(self, within_s):
        """The newest live ``dialog-open`` (stored record; lock held by the caller)."""
        cutoff = time.time() - within_s
        closed = {r.dialog_id for r in self._records if r.kind == "dialog" and r.dialog_id}
        live = [r for r in self._records
                if r.kind == "dialog-open" and r.dialog_id and r.time >= cutoff
                and r.dialog_id not in closed and r.dialog_id not in self._consumed]
        return max(live, key=lambda r: r.id) if live else None

    def pending_dialog(self, within_s=DEFAULT_WINDOW_S):
        """The newest ``dialog-open`` record at most ``within_s`` old that nothing has closed
        or used, or None: a dialog the page says is still open.

        A dialog is closed by a ``dialog`` record with the same page-assigned ``dialog_id``,
        whichever window or route (``where``) each record was drained by: the open record is
        usually drained over COM and the close by the next driver call. A record without an id
        never counts. A TaskDialog's message cannot be read from its window, so this record is
        what ties it to the page's own call.
        """
        with self._lock:
            rec = self._pending(within_s)
        return self._masked(rec) if rec else None

    def take_pending_dialog(self, within_s=DEFAULT_WINDOW_S):
        """``pending_dialog``, and the record is used up: one record authorises one accept."""
        with self._lock:
            rec = self._pending(within_s)
            if rec is not None:
                self._consumed.add(rec.dialog_id)
        return self._masked(rec) if rec else None
