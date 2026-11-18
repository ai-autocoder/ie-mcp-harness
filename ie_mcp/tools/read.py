"""Tools that read the page: text, html, element values, frames, grids, screenshots, waits."""
import json
import re
import time
from pathlib import PureWindowsPath

from ie_mcp import config, js
from ie_mcp.protocol import image_result, text_result, tool
from ie_mcp.session import SESSION, log


def _parse_ready(raw):
    """Parse the \x1f-delimited readiness string into a dict."""
    parts = (raw or "").split("\x1f")
    parts += [""] * (6 - len(parts))
    to_int = lambda v: int(v) if str(v).isdigit() else 0
    return {
        "ready": parts[0] == "true",
        "len": to_int(parts[1]),
        "els": to_int(parts[2]),
        "docs": to_int(parts[3]),
        "crossOrigin": to_int(parts[4]),
        "sample": parts[5],
    }


@tool(
    "ie_text",
    "Get visible text of the page or of a specific frame.",
    {
        "type": "object",
        "properties": {
            "frame": {"type": ["string", "integer"], "description": "frame index or name (optional)"},
            "limit": {"type": "integer"},
        },
    },
)
def t_text(args):
    from selenium.webdriver.common.by import By
    d = SESSION.ensure()
    SESSION._apply_frame(args.get("frame"))
    try:
        txt = d.find_element(By.TAG_NAME, "body").text
    except Exception:
        # Selenium's text atom fails on Trident; plain innerText still works.
        txt = d.execute_script("return document.body.innerText")
    limit = args.get("limit", 8000)
    return text_result(("" if txt is None else str(txt))[:limit])


@tool(
    "ie_html",
    "Get the HTML source of the page or of a specific frame.",
    {
        "type": "object",
        "properties": {
            "frame": {"type": ["string", "integer"]},
            "limit": {"type": "integer"},
        },
    },
)
def t_html(args):
    d = SESSION.ensure()
    SESSION._apply_frame(args.get("frame"))
    html = d.page_source
    limit = args.get("limit", 12000)
    return text_result(html[:limit])


@tool(
    "ie_get",
    "Read an element's text (default), its 'value', or a named attribute/property. "
    "Set attr=text|value|<attribute name>.",
    {
        "type": "object",
        "properties": {
            "by": {"type": "string"},
            "value": {"type": "string"},
            "attr": {"type": "string", "description": "text (default) | value | any attribute name"},
            "frame": {"type": ["string", "integer"]},
            "limit": {"type": "integer"},
        },
        "required": ["by", "value"],
    },
)
def t_get(args):
    """Read an element's text, value, or a named attribute via direct JS.

    (el.text / el.get_attribute use IEDriver's JS atoms, which fail on the legacy
    engine; arguments[0].innerText/value/getAttribute is simple enough to work.)
    """
    d = SESSION.ensure()
    el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
    what = args.get("attr", "text")
    try:
        if what == "text":
            out = d.execute_script("return arguments[0].innerText;", el)
        elif what == "value":
            out = d.execute_script("return arguments[0].value;", el)
        else:
            out = d.execute_script("return arguments[0].getAttribute(arguments[1]);", el, what)
    except Exception:
        out = el.text if what == "text" else el.get_attribute(what)
    return text_result("" if out is None else str(out)[: args.get("limit", 4000)])


@tool(
    "ie_frames",
    "List the frames/iframes of the current page (index, name, id, src). "
    "Use the index or name with the 'frame' arg of other tools.",
    {"type": "object", "properties": {}},
)
def t_frames(args):
    d = SESSION.ensure()
    d.switch_to.default_content()
    # frames.js reads name/id/src from the frame elements of the top document (Selenium's
    # get_attribute atom fails on Trident) and returns a delimited string.
    raw = d.execute_script(js.load("frames.js"))
    out = []
    for record in (raw or "").split("\x1e"):
        if record == "":
            continue
        fields = record.split("\x1f") + [""] * 4
        out.append({"index": int(fields[0]), "name": fields[1], "id": fields[2],
                    "src": fields[3]})
    return text_result(json.dumps(out, indent=2))


@tool(
    "ie_grid",
    "Extract a tabular grid as structured rows. Auto-picks the table with the "
    "most multi-cell rows (or pass table_id). Returns headers + rows (header-keyed "
    "dicts when the header row is detected/given). App-agnostic.",
    {
        "type": "object",
        "properties": {
            "frame": {"type": ["string", "integer"], "description": "frame/path holding the grid (e.g. '3/0')"},
            "table_id": {"type": "string", "description": "specific table element id (optional)"},
            "headers": {"type": "array", "items": {"type": "string"}, "description": "explicit column names (optional)"},
            "limit": {"type": "integer", "description": "max rows (default 500)"},
            "out_limit": {"type": "integer", "description": "max output chars (default 12000)"},
        },
    },
)
def t_grid(args):
    """Extract a tabular grid as structured rows (header-keyed dicts when possible).

    Heuristic + app-agnostic: picks the table with the most multi-cell rows. Pass a
    table element id via `table_id` to target a specific grid. Returns headers + rows;
    if `headers` is given (or detected) and matches the column count, rows become dicts.
    """
    d = SESSION.ensure()
    SESSION._apply_frame(args.get("frame"))
    raw = d.execute_script(js.load("grid.js"), args.get("table_id"))
    if not raw:
        return text_result(json.dumps({"rows": 0, "note": "no table found"}))
    grid = [r.split("\x1f") for r in raw.split("\x1e") if r != ""]
    grid = [r for r in grid if any(c.strip() for c in r)]  # drop spacer/empty rows
    headers = args.get("headers")
    if not headers and grid:
        # detect header row: first row whose cells are all non-empty & non-numeric-ish
        first = grid[0]
        if first and all(c.strip() and not c.strip().isdigit() for c in first):
            headers = first
            grid = grid[1:]
    limit = args.get("limit", 500)
    grid = grid[:limit]
    if headers and all(len(r) == len(headers) for r in grid):
        rows = [dict(zip(headers, r)) for r in grid]
    else:
        rows = grid  # fall back to arrays
    return text_result(json.dumps(
        {"columns": len(headers) if headers else (len(grid[0]) if grid else 0),
         "headers": headers, "row_count": len(rows), "rows": rows},
        ensure_ascii=False, indent=2)[: args.get("out_limit", 12000)])


# Windows opens these names as devices whatever the extension ("nul.png" is NUL).
# The superscript digits (COM¹ ...) are reserved too.
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                   *(f"{base}{digit}" for base in ("COM", "LPT") for digit in "123456789¹²³")}
_FORBIDDEN_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _screenshot_target(name):
    """Where a screenshot named ``name`` is saved: ``<session dir>/screenshots/<name>``.

    Only a bare file name is accepted: no drive (``C:x.png`` is drive-relative), no root,
    no directory separators, no ``..``, no ``:`` (NTFS alternate data streams), no wildcard
    characters, no device names. The folder is created here and this server never deletes
    it (screenshots saved by name are kept after ie_close).
    """
    text = "" if name is None else str(name)
    parsed = PureWindowsPath(text)
    stem = text.split(".")[0].strip().upper()
    # Windows strips trailing dots and spaces ("shot." would be saved as "shot").
    if (text.strip(". ") == "" or text != text.strip() or text.endswith(".")
            or parsed.drive or parsed.root or len(parsed.parts) != 1
            or _FORBIDDEN_NAME_CHARS.search(text) or stem in _RESERVED_NAMES):
        raise ValueError(
            f"save_path {text!r} must be a bare file name (no folders, drive or ..); "
            "it is saved in the session's screenshots folder")
    folder = config.SETTINGS.session_paths.directory / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / text


@tool(
    "ie_screenshot",
    "Capture a PNG screenshot of the IE-mode window, or of a single element if "
    "by/value are given (returned as an image). save_path, a bare file name, also saves it "
    "in the session's screenshots folder; saved files are kept after ie_close.",
    {
        "type": "object",
        "properties": {
            "save_path": {"type": "string", "description": "bare file name, e.g. shot.png; omit it to not save (an empty string is an error)"},
            "no_inline": {"type": "boolean", "description": "return confirmation text instead of the image blob"},
            "by": {"type": "string", "description": "element locator type (for element screenshot)"},
            "value": {"type": "string", "description": "element locator value (for element screenshot)"},
            "frame": {"type": ["string", "integer"]},
        },
    },
)
def t_screenshot(args):
    # Validate before touching the browser: a bad name must fail without side effects.
    target = _screenshot_target(args["save_path"]) if "save_path" in args else None
    d = SESSION.ensure()
    if args.get("value"):
        # element-scoped screenshot
        el = SESSION.find(args["by"], args["value"], args.get("frame"), args.get("timeout", 10))
        log("screenshot: capturing element")
        png = el.screenshot_as_png
    else:
        try:
            d.switch_to.default_content()  # window screenshots from the top doc
        except Exception:
            pass
        log("screenshot: capturing")
        png = d.get_screenshot_as_png()
    log(f"screenshot: got {len(png)} bytes")
    if target is not None:
        with open(target, "wb") as fh:
            fh.write(png)
        log(f"screenshot: saved {target}")
    if args.get("no_inline"):
        # avoid returning a giant base64 blob; just confirm + path
        return text_result(f"screenshot saved ({len(png)} bytes)"
                           + (f" -> {target}" if target is not None else ""))
    return image_result(png)


@tool(
    "ie_wait_text",
    "Poll until a specific substring appears in the page text (for slow async "
    "content). Scans all frames unless 'frame' is given; returns which frame "
    "matched + a preview. For 'is the page loaded?' use ie_wait_ready instead.",
    {
        "type": "object",
        "properties": {
            "contains": {"type": "string"},
            "frame": {"type": ["string", "integer"]},
            "timeout": {"type": "number", "description": "max seconds (default 45)"},
            "interval": {"type": "number", "description": "poll seconds (default 2)"},
            "case_insensitive": {"type": "boolean"},
            "gone": {"type": "boolean", "description": "wait for the substring to DISAPPEAR instead of appear (e.g. a spinner)"},
        },
        "required": ["contains"],
    },
)
def t_wait_text(args):
    """Poll until `contains` appears (or, with gone=True, disappears) in a frame.

    If no frame is given, all frames are scanned each round. With gone=False the
    success condition is the substring present in some frame; with gone=True it is
    the substring absent from every scanned frame (e.g. a spinner disappearing).
    """
    from selenium.webdriver.common.by import By
    d = SESSION.ensure()
    needle = args["contains"]
    ci = args.get("case_insensitive", True)
    if ci:
        needle = needle.lower()
    timeout = args.get("timeout", 45)
    interval = args.get("interval", 2)
    frame = args.get("frame")
    gone = args.get("gone", False)

    elapsed = 0
    last = ""
    while True:
        SESSION.check_cancel()  # outside the try below: not a transient failure
        # A whole round can fail transiently while the app navigates (IEDriver
        # briefly reports "Unable to get browser"); treat that as "not yet".
        found_in = None
        round_ok = False
        try:
            d.switch_to.default_content()
            if frame is not None and frame != "":
                candidates = [frame]
            else:
                n = len(d.find_elements(By.TAG_NAME, "frame") + d.find_elements(By.TAG_NAME, "iframe"))
                candidates = list(range(n)) if n else [None]
            for cand in candidates:
                d.switch_to.default_content()
                try:
                    if cand is not None:
                        SESSION._apply_frame(cand)
                    txt = d.find_element(By.TAG_NAME, "body").text
                except Exception:
                    continue
                hay = txt.lower() if ci else txt
                if needle in hay:
                    found_in = cand
                    last = txt
                    break
                last = txt
            round_ok = True
        except Exception as exc:
            last = f"(transient: {str(exc).splitlines()[0]})"

        if round_ok:
            if not gone and found_in is not None:
                d.switch_to.default_content()
                return text_result(json.dumps({
                    "ok": True, "found": True, "frame": found_in,
                    "after_seconds": elapsed, "preview": last[:600],
                }, indent=2))
            if gone and found_in is None:
                d.switch_to.default_content()
                return text_result(json.dumps({
                    "ok": True, "gone": True, "after_seconds": elapsed,
                }, indent=2))
        if elapsed >= timeout:
            try:
                d.switch_to.default_content()
            except Exception:
                pass
            return text_result(json.dumps({
                "ok": False, "found": found_in is not None, "after_seconds": elapsed,
                "timed_out": True, "last_preview": last[:600],
            }, indent=2))
        SESSION.pause(interval)
        elapsed += interval


@tool(
    "ie_wait_ready",
    "Wait until the page finishes loading: every same-origin frame's "
    "document.readyState is 'complete' AND the DOM stops changing for a few "
    "polls (content settled). Handles slow async/frameset content. Use THIS to "
    "know content is ready, then search the data separately. Do NOT wait for a "
    "specific value to detect load — if it's absent you can't tell 'still "
    "loading' from 'not present'. Returns loaded=true/false with frame count, "
    "text length, element count and a content sample.",
    {
        "type": "object",
        "properties": {
            "timeout": {"type": "number", "description": "max seconds (default 60)"},
            "interval": {"type": "number", "description": "poll seconds (default 2)"},
            "stable_rounds": {"type": "integer", "description": "consecutive unchanged polls to call it settled (default 2)"},
            "min_chars": {"type": "integer", "description": "minimum visible text length to count as loaded (default 1)"},
            "settle": {"type": "boolean", "description": "require DOM to stop changing (default true); set false to only require readyState complete"},
        },
    },
)
def t_wait_ready(args):
    """Wait until the page (and all same-origin frames) finish loading — app-agnostic.

    "Loaded" = every document.readyState is 'complete' AND the DOM has stopped
    changing for `stable_rounds` consecutive polls (content settled), with at least
    `min_chars` of visible text. This handles slow async/AJAX/frameset content
    (e.g. ~30s grids) without knowing anything about the specific application.
    """
    d = SESSION.ensure()
    timeout = args.get("timeout", 60)
    interval = args.get("interval", 2)
    stable_rounds = args.get("stable_rounds", 2)
    min_chars = args.get("min_chars", 1)
    require_settle = args.get("settle", True)

    elapsed = 0
    prev_sig = None
    stable = 0
    last = {"loaded": False, "stage": "init"}
    while True:
        SESSION.check_cancel()
        try:
            d.switch_to.default_content()
            raw = d.execute_script(js.load("ready_probe.js"))
            s = _parse_ready(raw)
            sig = (s.get("len", 0), s.get("els", 0), s.get("docs", 0))
            ready = bool(s.get("ready")) and s.get("len", 0) >= min_chars
            if prev_sig is not None and sig == prev_sig:
                stable += 1
            else:
                stable = 0
            prev_sig = sig
            settled = (not require_settle) or (stable >= stable_rounds)
            last = {
                "loaded": bool(ready and settled),
                "stage": "ready" if (ready and settled) else ("settling" if ready else "loading"),
                "readyState_all_complete": bool(s.get("ready")),
                "frames": s.get("docs", 0),
                "cross_origin_frames": s.get("crossOrigin", 0),
                "text_len": s.get("len", 0),
                "elements": s.get("els", 0),
                "stable_rounds": stable,
                "sample": s.get("sample", ""),
                "after_seconds": elapsed,
            }
            if last["loaded"]:
                return text_result(json.dumps(last, indent=2))
        except Exception as exc:
            stable = 0
            last = {"loaded": False, "stage": "transient",
                    "note": str(exc).splitlines()[0], "after_seconds": elapsed}
        if elapsed >= timeout:
            last["timed_out"] = True
            return text_result(json.dumps(last, indent=2))
        SESSION.pause(interval)
        elapsed += interval


@tool(
    "ie_wait_element",
    "Wait for an element (by/value) to appear — or, with gone=true, to disappear. "
    "Use before acting on slow-rendering content.",
    {
        "type": "object",
        "properties": {
            "by": {"type": "string"},
            "value": {"type": "string"},
            "frame": {"type": ["string", "integer"]},
            "timeout": {"type": "number", "description": "max seconds (default 20)"},
            "interval": {"type": "number"},
            "gone": {"type": "boolean", "description": "wait for it to disappear instead"},
        },
        "required": ["by", "value"],
    },
)
def t_wait_element(args):
    """Wait for an element (by/value) to appear — or, with gone=True, to disappear."""
    by = args["by"]
    value = args["value"]
    frame = args.get("frame")
    timeout = args.get("timeout", 20)
    interval = args.get("interval", 1)
    gone = args.get("gone", False)
    d = SESSION.ensure()
    elapsed = 0
    while True:
        SESSION.check_cancel()
        present = False
        try:
            SESSION._apply_frame(frame)
            present = len(d.find_elements(SESSION._by(by), value)) > 0
        except Exception:
            present = False
        if (not gone and present) or (gone and not present):
            try:
                d.switch_to.default_content()
            except Exception:
                pass
            return text_result(json.dumps({
                "ok": True, "present": present, "after_seconds": elapsed,
            }))
        if elapsed >= timeout:
            return text_result(json.dumps({
                "ok": False, "present": present, "after_seconds": elapsed, "timed_out": True,
            }))
        SESSION.pause(interval)
        elapsed += interval
