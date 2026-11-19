"""Redaction of secrets in captured traffic, console records and the server log.

Two mechanisms work together:

* **Structure.** A field (form, query string, ``;name=value`` path parameter, JSON key, XML
  element or attribute, multipart part) or header whose *name* looks like a secret has its
  value replaced by ``MARKER``. Names are matched by word segment, so ``lastName``,
  ``compass`` and ``keyword`` stay visible while ``userPass``, ``cardPin``, ``newpassword``
  and ``USERPWD`` are caught.
* **Value tracking.** Every value redacted this way is remembered (``secret_values``, which
  only grows) and ``mask`` replaces it wherever it shows up again, for instance a password
  echoed in a response page. A value is always recorded before anything is masked, so nothing
  that passes through ``mask`` afterwards can carry it.

This module imports neither ``config`` nor ``session``.
"""
import base64
import binascii
import codecs
import html
import json
import re
import threading
from urllib.parse import unquote, unquote_plus

MARKER = "<redacted>"

_PASSWORD_PARTS = ("password", "passwd", "passphrase", "pwd")
_FIELD_NAMES = frozenset((
    "sid", "session", "sessionid", "jsessionid", "aspsessionid", "phpsessid", "auth", "key",
    "apikey", "csrf", "xsrf"))
_FIELD_SEGMENTS = frozenset((
    "pass", "pin", "secret", "token", "apikey", "credential", "credentials", "otp"))
_HEADER_NAMES = frozenset(("cookie", "set-cookie", "authorization", "proxy-authorization"))
_HEADER_SEGMENTS = frozenset((
    "token", "auth", "key", "csrf", "xsrf", "session", "secret", "password"))
_URL_HEADERS = frozenset(("referer", "location", "content-location"))
_TRIVIAL_VALUES = frozenset(("true", "false", "null", "undefined", "on", "off", "yes", "no"))
# A recorded value this long is not a credential; recording it would only slow every mask.
_MAX_SECRET_LENGTH = 4096

# camelCase and letter/digit boundaries: "lastName" -> last Name, "USERPWD" -> USERPWD,
# "XMLHttp" -> XML Http, "password2" -> password 2.
_SEGMENT = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")


def name_segments(name):
    """Lower-cased word segments of a field or header name."""
    return [part.lower() for part in _SEGMENT.findall(name or "")]


def _normalise(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def is_secret_field(name, extra=()):
    """True when a form/query/JSON/XML field called ``name`` holds a secret."""
    norm = _normalise(name)
    if not norm:
        return False
    for part in _PASSWORD_PARTS:
        if part in norm:
            return True
    if norm in _FIELD_NAMES:
        return True
    if extra and norm in {_normalise(item) for item in extra}:
        return True
    for segment in name_segments(name):
        if segment in _FIELD_SEGMENTS:
            return True
    return False


def is_secret_header(name, extra=()):
    """True when the value of the HTTP header ``name`` is a secret."""
    lowered = (name or "").strip().lower()
    if not lowered:
        return False
    if lowered in _HEADER_NAMES:
        return True
    if extra:
        wanted = {_normalise(item) for item in extra}
        if _normalise(lowered) in wanted:
            return True
    for segment in name_segments(name):
        if segment in _HEADER_SEGMENTS:
            return True
    return False


def parse_content_type(value):
    """``("text/html", {"charset": "utf-8"})`` for a Content-Type header value."""
    parts = (value or "").split(";")
    params = {}
    for part in parts[1:]:
        key, sep, val = part.partition("=")
        if sep:
            params[key.strip().lower()] = val.strip().strip('"').strip("'")
    return parts[0].strip().lower(), params


def _codec(charset):
    """The Python codec name for ``charset``, or None when absent or unknown."""
    if not charset:
        return None
    try:
        return codecs.lookup(charset).name
    except (LookupError, ValueError):
        return None


def _encodings(charsets):
    """Codecs to encode a secret with so its bytes match a body: UTF-8 plus the given charsets.
    A BOM-less pair stands in for plain utf-16/utf-32, whose encoder would add a BOM."""
    names = ["utf-8"]
    for charset in charsets:
        name = _codec(charset)
        if name in ("utf-16", "utf-32"):
            names.extend((name + "-le", name + "-be"))
        elif name:
            names.append(name)
    return tuple(dict.fromkeys(names))


# --- JSON, lenient and format-preserving -------------------------------------
# Strings (an unterminated one runs to the end), structural characters, bare tokens.
_JSON_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"?|[{}\[\]:,]|[^\s{}\[\]:,"]+', re.S)


def _json_string(token):
    try:
        value = json.loads(token)
        if isinstance(value, str):
            return value
    except ValueError:
        pass
    return token[1:-1] if token.endswith('"') and len(token) > 1 else token[1:]


# --- XML, lenient ------------------------------------------------------------
_XML_ELEMENT = re.compile(
    r"<(?P<n>[A-Za-z_][\w.\-:]*)(?P<a>(?:\s[^<>]*?)?)(?<!/)>"
    r"(?P<t><!\[CDATA\[.*?\]\]>|[^<]*)</(?P=n)\s*>", re.S)
_XML_TAG = re.compile(r"<[A-Za-z_][^<>]*>")
_XML_ATTRIBUTE = re.compile(
    r"(?P<pre>\s)(?P<n>[A-Za-z_][\w.\-:]*)(?P<eq>\s*=\s*)(?P<q>[\"'])(?P<v>.*?)(?P=q)", re.S)

# --- text (log lines) and URLs -----------------------------------------------
_TEXT_JSON_PAIR = re.compile(
    r"""(?P<kq>["'])(?P<n>[A-Za-z0-9_.\-]+)(?P=kq)(?P<sep>\s*:\s*)"""
    r"""(?P<v>"(?:[^"\\]|\\.)*"|'[^']*'|[^\s,}\]]+)""")
_TEXT_EQUALS_PAIR = re.compile(r"""(?<![A-Za-z0-9_.\-])(?P<n>[A-Za-z0-9_.\-]+)=(?P<v>[^\s&"']*)""")
_TEXT_COLON_PAIR = re.compile(
    r"(?<![A-Za-z0-9_.\-/])(?P<n>[A-Za-z][A-Za-z0-9_\-]*)(?P<sep>[ \t]*:[ \t]*)(?P<v>[^\r\n]*)")
_URL_USERINFO = re.compile(r"^(?P<head>[A-Za-z][A-Za-z0-9+.\-]*://[^/:@?#]*):(?P<pw>[^/@?#]*)@")
_PATH_PARAM = re.compile(r";(?P<n>[^;/?#=]+)=(?P<v>[^;/?#]*)")
_PART_NAME = re.compile(r'(?i)content-disposition:[^\r\n]*?[;\s]name=(?:"([^"]*)"|([^;\s"]+))')
_PART_TYPE = re.compile(r"(?i)^content-type:[ \t]*([^\r\n]*)", re.M)


def _alternation(longs, shorts, before, after):
    """Regex source matching any of ``longs`` anywhere and any of ``shorts`` only as a whole
    token (between ``before`` and ``after``), longest first. Works for str and for bytes."""
    empty = before[:0]
    bar, open_group, close_group = ((b"|", b"(?:", b")") if isinstance(before, bytes)
                                    else ("|", "(?:", ")"))

    def group(values):
        alternatives = bar.join(re.escape(v) for v in sorted(values, key=len, reverse=True))
        return open_group + alternatives + close_group

    sources = []
    if longs:
        sources.append(group(longs))
    if shorts:
        sources.append(before + group(shorts) + after)
    return bar.join(sources) if sources else empty


class Redactor:
    """Thread-safe secret redactor; see the module docstring.

    ``secret_values`` holds every redacted value of 4+ characters that is not trivial
    (``true``/``null``/... or a number shorter than 8 digits). Values of 8+ characters are
    masked anywhere; shorter ones only as whole tokens, so ``test`` inside ``latest`` stays.
    With ``enabled=False`` every method returns its input and records nothing.
    """

    def __init__(self, extra_fields=(), extra_headers=(), enabled=True):
        self.extra_fields = frozenset(str(x).strip().lower() for x in extra_fields if str(x).strip())
        self.extra_headers = frozenset(
            str(x).strip().lower() for x in extra_headers if str(x).strip())
        self.enabled = enabled
        self._extra_field_names = frozenset(_normalise(x) for x in self.extra_fields)
        self._extra_header_names = frozenset(_normalise(x) for x in self.extra_headers)
        self.secret_values = set()
        self._lock = threading.Lock()
        # Compiled maskers, rebuilt when secret_values has grown (it never shrinks).
        self._text_regex = None
        self._text_size = -1
        self._byte_regexes = {}
        self._byte_size = -1

    # --- recording -----------------------------------------------------------
    @property
    def version(self):
        """Changes whenever ``secret_values`` grows, so a holder of masked data can tell that
        a value it was not masked with is known now."""
        return len(self.secret_values)

    def record(self, value):
        """Remember ``value`` as a secret if it is long enough and not trivial."""
        if not self.enabled or not isinstance(value, str):
            return
        value = value.strip()
        if len(value) < 4 or len(value) > _MAX_SECRET_LENGTH:
            return
        if value.lower() in _TRIVIAL_VALUES or (value.isdigit() and len(value) < 8):
            return
        if value in MARKER:  # never let a value corrupt the marker itself
            return
        with self._lock:
            self.secret_values.add(value)

    def _record_variants(self, raw, encoding="utf-8"):
        """Record a value as seen (possibly percent-encoded) and decoded."""
        self.record(raw)
        for decode in (unquote, unquote_plus):
            try:
                self.record(decode(raw, encoding=encoding, errors="replace"))
            except LookupError:
                self.record(decode(raw, errors="replace"))

    # --- masking -------------------------------------------------------------
    def mask(self, text):
        """Replace every known secret value in ``text`` with ``MARKER``."""
        if not self.enabled or not text:
            return text
        with self._lock:
            size = len(self.secret_values)
            if self._text_size != size:
                source = _alternation(
                    [v for v in self.secret_values if len(v) >= 8],
                    [v for v in self.secret_values if len(v) < 8],
                    # ASCII alphanumerics bound a token, exactly as in mask_bytes: the two must
                    # agree, or the same secret would be masked in a body but not in a log line.
                    r"(?<![A-Za-z0-9])", r"(?![A-Za-z0-9])")
                self._text_regex = re.compile(source) if source else None
                self._text_size = size
            regex = self._text_regex
        return regex.sub(MARKER, text) if regex else text

    def mask_bytes(self, data, charsets=()):
        """``mask`` for a body: each secret is matched in UTF-8 and in the given charsets, so
        a non-ASCII value still matches. Never raises."""
        if not self.enabled or not data:
            return data
        try:
            encodings = _encodings(charsets)
            with self._lock:
                size = len(self.secret_values)
                if self._byte_size != size:
                    self._byte_regexes = {}
                    self._byte_size = size
                if encodings not in self._byte_regexes:
                    longs, shorts = set(), set()
                    for value in self.secret_values:
                        for encoding in encodings:
                            try:
                                encoded = value.encode(encoding)
                            except (UnicodeError, LookupError):
                                continue
                            (longs if len(value) >= 8 else shorts).add(encoded)
                    source = _alternation(longs, shorts, b"(?<![A-Za-z0-9])", b"(?![A-Za-z0-9])")
                    self._byte_regexes[encodings] = re.compile(source) if source else None
                regex = self._byte_regexes[encodings]
            return regex.sub(MARKER.encode("ascii"), data) if regex else data
        except Exception:
            return data

    # --- pairs (query string, form body) -------------------------------------
    def _redact_pairs(self, text, encoding="utf-8"):
        pieces = text.split("&")
        for index, piece in enumerate(pieces):
            name, equals, value = piece.partition("=")
            if not equals or not value or value == MARKER:
                continue
            try:
                decoded_name = unquote_plus(name, encoding=encoding, errors="replace")
            except LookupError:
                decoded_name = unquote_plus(name, errors="replace")
            if is_secret_field(decoded_name, self.extra_fields):
                self._record_variants(value, encoding)
                pieces[index] = name + "=" + MARKER
        return "&".join(pieces)

    # --- URLs and headers ----------------------------------------------------
    def redact_url(self, url):
        """Redact query-string fields, fragment fields, ``;name=value`` path parameters
        (``;jsessionid=``) and the password of ``user:password@host``."""
        if not self.enabled or not url:
            return url
        try:
            return self._redact_url(url)
        except Exception:
            return self.mask(url)

    def _redact_url(self, url):
        match = re.match(r"^(?P<base>[^?#]*)(?P<query>\?[^#]*)?(?P<frag>#.*)?$", url, re.S)
        base, query, fragment = match.group("base"), match.group("query"), match.group("frag")

        def userinfo(m):
            if m.group("pw") and m.group("pw") != MARKER:
                self._record_variants(m.group("pw"))
                return m.group("head") + ":" + MARKER + "@"
            return m.group(0)

        def path_param(m):
            if (is_secret_field(unquote(m.group("n")), self.extra_fields)
                    and m.group("v") not in ("", MARKER)):
                self._record_variants(m.group("v"))
                return ";" + m.group("n") + "=" + MARKER
            return m.group(0)

        out = _PATH_PARAM.sub(path_param, _URL_USERINFO.sub(userinfo, base))
        if query is not None:
            out += "?" + self._redact_pairs(query[1:])
        if fragment is not None:
            out += "#" + (self._redact_pairs(fragment[1:]) if "=" in fragment else fragment[1:])
        return out

    def _track_header(self, lowered, value):
        """Record the secret values inside one secret header's value."""
        value = value.strip()
        if lowered == "cookie":
            for pair in value.split(";"):
                name, equals, cookie = pair.partition("=")
                if equals:
                    self._record_variants(cookie.strip().strip('"'))
        elif lowered == "set-cookie":
            # Only the first pair is the cookie; Path, Expires, Domain... are attributes.
            name, equals, cookie = value.split(";", 1)[0].partition("=")
            if equals:
                self._record_variants(cookie.strip().strip('"'))
        else:
            self._record_variants(value)
            if lowered in ("authorization", "proxy-authorization"):
                scheme, _, credentials = value.partition(" ")
                credentials = credentials.strip()
                if credentials:
                    self._record_variants(credentials)
                    if scheme.lower() == "basic":
                        try:
                            decoded = base64.b64decode(credentials).decode("utf-8", "replace")
                        except (binascii.Error, ValueError):
                            decoded = ""
                        user, colon, password = decoded.partition(":")
                        if colon:
                            self.record(password)

    def redact_headers(self, headers):
        """``headers`` with secret values (Cookie, Authorization, ...) replaced by ``MARKER``
        and the query of ``Referer``/``Location``/``Content-Location`` redacted."""
        if not self.enabled:
            return headers
        out = []
        for name, value in headers:
            try:
                lowered = (name or "").strip().lower()
                if is_secret_header(name, self.extra_headers):
                    if value and value.strip() and value != MARKER:
                        self._track_header(lowered, value)
                        value = MARKER
                elif lowered in _URL_HEADERS:
                    value = self.redact_url(value)
            except Exception:
                value = self.mask(value)
            out.append((name, value))
        return out

    # --- bodies --------------------------------------------------------------
    def redact_body(self, content_type, body, charset):
        """Redact secret fields in a form-urlencoded, multipart, JSON or XML body. A body that
        does not parse is returned as it was (``mask`` is the store's second line)."""
        if not self.enabled or not body:
            return body
        try:
            ctype, params = parse_content_type(content_type)
            return self._structured(ctype, params, body, charset or params.get("charset"), 0)
        except Exception:
            return body

    def _structured(self, ctype, params, body, charset, depth):
        if ctype.startswith("multipart/"):
            return self._redact_multipart(params, body, charset, depth)
        if not ctype or ctype == "text/plain":
            # Legacy pages post forms and JSON with no or a wrong type; sniff.
            head = body.lstrip()[:1]
            if head in (b"{", b"["):
                ctype = "application/json"
            elif head == b"<":
                ctype = "application/xml"
            elif re.match(rb"^[^\s=&<>]+=", body):
                ctype = "application/x-www-form-urlencoded"
        if ctype == "application/x-www-form-urlencoded":
            # latin-1 maps bytes 1:1, so everything not redacted comes back unchanged.
            text = self._redact_pairs(body.decode("latin-1"), _codec(charset) or "utf-8")
            return text.encode("latin-1")
        if "json" in ctype:
            return self._via_text(body, charset, self._redact_json)
        if "xml" in ctype:
            return self._via_text(body, charset, self._redact_xml)
        return body

    @staticmethod
    def _via_text(body, charset, transform):
        codec = _codec(charset) or "utf-8"
        try:
            text = body.decode(codec)
        except UnicodeError:
            return body
        redacted = transform(text)
        return body if redacted == text else redacted.encode(codec, "replace")

    def _redact_json(self, text):
        """Replace the value of every secret key, at any depth, keeping the layout as it was."""
        tokens = [(m.group(), m.start(), m.end()) for m in _JSON_TOKEN.finditer(text)]
        out, position, i = [], 0, 0
        while i < len(tokens):
            token = tokens[i][0]
            if not (token[0] == '"' and i + 2 < len(tokens) and tokens[i + 1][0] == ":"
                    and is_secret_field(_json_string(token), self.extra_fields)):
                i += 1
                continue
            first = last = i + 2
            leaves = []
            if tokens[first][0] in ("{", "["):
                depth = 0
                for last in range(first, len(tokens)):
                    value = tokens[last][0]
                    if value in ("{", "["):
                        depth += 1
                    elif value in ("}", "]"):
                        depth -= 1
                        if depth <= 0:
                            break
                    elif value not in (":", ","):
                        leaves.append(value)
            elif tokens[first][0] not in ("}", "]", ":", ",", "null", "true", "false"):
                leaves.append(tokens[first][0])
            if not leaves:
                i += 1
                continue
            for leaf in leaves:
                if leaf[0] == '"':
                    self.record(_json_string(leaf))
                    self.record(leaf[1:-1] if leaf.endswith('"') and len(leaf) > 1 else leaf[1:])
                else:
                    self.record(leaf)
            out.append(text[position:tokens[first][1]])
            out.append('"%s"' % MARKER)
            position = tokens[last][2]
            i = last + 1
        out.append(text[position:])
        return "".join(out)

    def _redact_xml(self, text):
        def element(m):
            content = m.group("t")
            if (not content.strip() or content == MARKER
                    or not is_secret_field(m.group("n").rsplit(":", 1)[-1], self.extra_fields)):
                return m.group(0)
            inner = content[9:-3] if content.startswith("<![CDATA[") else content
            self.record(inner)
            self.record(html.unescape(inner))
            return "<%s%s>%s</%s>" % (m.group("n"), m.group("a"), MARKER, m.group("n"))

        def attribute(m):
            if (is_secret_field(m.group("n").rsplit(":", 1)[-1], self.extra_fields)
                    and m.group("v") not in ("", MARKER)):
                self.record(m.group("v"))
                self.record(html.unescape(m.group("v")))
                return "%s%s%s%s%s%s" % (m.group("pre"), m.group("n"), m.group("eq"),
                                         m.group("q"), MARKER, m.group("q"))
            return m.group(0)

        # Elements first: a redacted attribute value would put "<" inside a tag.
        text = _XML_ELEMENT.sub(element, text)
        return _XML_TAG.sub(lambda m: _XML_ATTRIBUTE.sub(attribute, m.group(0)), text)

    def _redact_multipart(self, params, body, charset, depth):
        boundary = params.get("boundary")
        if not boundary or depth > 2:
            return body
        delimiter = b"--" + boundary.encode("latin-1", "replace")
        pieces = body.split(delimiter)
        out = [pieces[0]]
        for piece in pieces[1:]:
            if piece.startswith(b"--") or not piece.strip():
                out.append(piece)  # the closing "--"
            else:
                out.append(self._redact_part(piece, charset, depth))
        return delimiter.join(out)

    def _redact_part(self, piece, charset, depth):
        separator = b"\r\n\r\n"
        index = piece.find(separator)
        if index < 0:
            separator = b"\n\n"
            index = piece.find(separator)
        if index < 0:
            return piece
        head = piece[:index + len(separator)]
        content = piece[index + len(separator):]
        trailer = b""
        for ending in (b"\r\n", b"\n"):
            if content.endswith(ending):
                trailer, content = ending, content[:-len(ending)]
                break
        headers = head.decode("latin-1")
        named = _PART_NAME.search(headers)
        name = (named.group(1) or named.group(2) or "") if named else ""
        if is_secret_field(name, self.extra_fields):
            if content and content != MARKER.encode("ascii"):
                if len(content) <= _MAX_SECRET_LENGTH:
                    self._record_variants(content.decode(_codec(charset) or "utf-8", "replace"))
                content = MARKER.encode("ascii")
        else:
            typed = _PART_TYPE.search(headers)
            if typed:
                ctype, params = parse_content_type(typed.group(1))
                content = self._structured(
                    ctype, params, content, params.get("charset") or charset, depth + 1)
        return head + content + trailer

    # --- free text -----------------------------------------------------------
    def redact_text(self, text):
        """Redact ``name=value``, ``name: value`` and ``"name":"value"`` pairs whose name is
        secret, then ``mask`` every known secret value."""
        if not self.enabled or not text:
            return text
        try:
            text = self._redact_text_pairs(text)
        except Exception:
            pass
        return self.mask(text)

    def _redact_text_pairs(self, text):
        def json_pair(m):
            value = m.group("v")
            if not is_secret_field(m.group("n"), self.extra_fields):
                return m.group(0)
            quote = value[0] if value[0] in "\"'" else ""
            inner = value[1:-1] if quote and len(value) > 1 and value[-1] == quote else value
            if not inner or inner == MARKER or inner in ("null", "true", "false"):
                return m.group(0)
            self.record(inner)
            return "%s%s%s%s%s%s%s" % (m.group("kq"), m.group("n"), m.group("kq"), m.group("sep"),
                                       quote, MARKER, quote)

        def equals_pair(m):
            value = m.group("v")
            if not value or value == MARKER or not is_secret_field(m.group("n"), self.extra_fields):
                return m.group(0)
            self._record_variants(value)
            return m.group("n") + "=" + MARKER

        def colon_pair(m):
            # "name: value" is also ordinary prose ("KeyError: 'x'", "no such session: ..."), so
            # it is redacted only for names that are unambiguously secret: exact secret headers
            # (rest of the line), hyphenated header names such as X-Auth-Token (rest of the
            # line), and password-like or configured field names (first token only). The looser
            # segment rules (token, session, key...) apply to "=" and JSON pairs only.
            name, value = m.group("n"), m.group("v")
            if not value.strip() or value.strip() == MARKER:
                return m.group(0)
            lowered = name.lower()
            if (lowered in _HEADER_NAMES or _normalise(name) in self._extra_header_names
                    or ("-" in name and is_secret_header(name, self.extra_headers))):
                self._track_header(lowered, value)
                return name + m.group("sep") + MARKER
            normalised = _normalise(name)
            if (any(part in normalised for part in _PASSWORD_PARTS)
                    or normalised in self._extra_field_names):
                token = re.match(r"\"[^\"]*\"|'[^']*'|[^\s,;&]+", value)
                if token and token.group(0) != MARKER:
                    raw = token.group(0)
                    quote = raw[0] if raw[0] in "\"'" and len(raw) > 1 and raw[-1] == raw[0] else ""
                    self._record_variants(raw[1:-1] if quote else raw)
                    return (name + m.group("sep") + quote + MARKER + quote
                            + value[token.end():])
            return m.group(0)

        text = _TEXT_JSON_PAIR.sub(json_pair, text)
        text = _TEXT_EQUALS_PAIR.sub(equals_pair, text)
        return _TEXT_COLON_PAIR.sub(colon_pair, text)
