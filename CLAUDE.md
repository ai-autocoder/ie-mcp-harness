# CLAUDE.md

Guidance for coding agents working in this repository.

## What this is

`ie-mcp` is an MCP (Model Context Protocol) stdio server that lets an AI agent drive **legacy,
IE-only web applications** running in **Microsoft Edge IE mode** (the Trident engine), through
the official Selenium **IEDriverServer**.

This repository is a **fork** of [thomfilg/ie-mcp](https://github.com/thomfilg/ie-mcp) (MIT),
cloned at upstream commit `e0937ff6f1bfb51b13c72310084af3fd17d062b0`. The upstream remote is
named `upstream`; there is no `origin` until a fork is created on a hosting service. Development
happens on the `harness` branch.

The fork's goal: give an agent **full access** to an IE-mode page, on par with what a
Chromium automation tool gives on a modern page, **without changing how the page behaves**:

- read the DOM, text and HTML of every window, frame and modal dialog;
- interact (click, type, select, keys, hover), including inside `showModalDialog` windows;
- see every HTTP request and response the page makes;
- read script errors, console output and dialog calls;
- see the page as rendered (screenshots, including open modal dialogs);
- run arbitrary JavaScript in any window or frame.

**Start here:** the design is in
[docs/superpowers/specs/2026-11-17-ie-mode-harness-design.md](docs/superpowers/specs/2026-11-17-ie-mode-harness-design.md)
and the task-by-task implementation plan is in
[docs/superpowers/plans/2026-11-17-ie-mode-harness.md](docs/superpowers/plans/2026-11-17-ie-mode-harness.md).
Work through the plan in order. Task 2 is a spike whose recorded decisions (D1–D15) select
variants in later tasks; do not skip it.

## Golden rule: keep it generic

This project must stay **application-agnostic**. Never add anything specific to one company,
product, customer, intranet or site:

- no real hostnames, URLs, screen names, field names, credentials, or captured data;
- no tool, default or heuristic tuned to one application;
- examples and tests use only the fixture site in `ie_mcp_fixtures/site/`, served from
  `127.0.0.1`.

If a feature is only useful for one application, it belongs in that application's own
tooling, not here.

## Fidelity rule: observe, don't alter

The harness exists to show how the legacy page really behaves. Anything that changes the page
(script injection, wrappers, the recording proxy, synthetic input) must:

- keep the document mode, charset detection and caching behaviour unchanged (see the injection
  rules below);
- be listed in `ie_status` and the README;
- have an env var that turns it off.

The spec's "Fidelity" table is the list. Add a row whenever you add a modification.

## Environment

| Component | Requirement |
|---|---|
| OS | Windows 10/11 with an unlocked, interactive desktop (IE mode is never headless) |
| Edge | Microsoft Edge with IE mode available |
| IEDriverServer | Official Selenium IE driver, **32-bit (Win32) 4.14.0** recommended. Set `IE_DRIVER_PATH`, or leave it unset: the server then uses the highest version in the per-user Selenium cache (`~/.cache/selenium/IEDriverServer/win32/<version>/`, then `win64`) and tells you to set `IE_DRIVER_PATH` if it finds none |
| Python | **3.10+** (current selenium requires it; the development machine runs 3.14). |
| Runtime deps | `selenium>=4.14.0,<5` only. Optional extra `.[win32]` adds `pywin32>=306` (COM backend); the server runs without it |

IEDriver launches Edge with `--ie-mode-force`. Plan Task 2 confirms on this machine that the
fixture pages then render in IE mode (Trident userAgent and the expected `documentMode`). Until
then, treat that as unverified.

Installing the dependencies declared in `pyproject.toml` (`pip install -e ".[win32]"`) is fine.
Ask the user before downloading `IEDriverServer.exe`, any other binary, or any package not
declared there. Never change Windows, Edge or Internet Options settings yourself (Protected
Mode, zones, registry, system proxy, firewall, certificates); tell the user what to set.

## Commands

Claude Code's Bash tool is Git Bash on Windows. Commands below are for bash; PowerShell
equivalents are given where they differ. Never use `set VAR=1 && …` (that is cmd syntax: in
bash it sets positional parameters, in Windows PowerShell 5.1 `&&` is a parse error).

```bash
# install editable, with the optional Win32 extra
pip install -e ".[win32]"

# unit tests: no browser, run anywhere on Windows. Files are tests/test_*.py
python -m unittest discover -s tests -p "test_*.py" -v

# integration tests: open a real, visible IE-mode window; need an unlocked desktop.
# Files are tests/integration/it_*.py. The runner sets IE_MCP_INTEGRATION=1 itself and
# FAILS if any test was skipped (from plan Task 8).
python scripts/run_integration.py            # all
python scripts/run_integration.py it_modal   # one module

# serve the fixture site by hand; prints both origins (--host, --port, --alt-port)
python -m ie_mcp_fixtures

# check the setup end to end: opens and closes one real IE-mode session on IE_DEFAULT_URL
# (which must be an http(s) URL). It does not clear your cookies or cache unless you set
# IE_CLEAN_SESSION=1.
ie-mcp --selftest            # needs `pip install -e .`; or: python -m ie_mcp --selftest

# register a dev copy with Claude Code for manual testing
claude mcp add ie-mcp-dev -e IE_INTERACTION_MODE=background -- python -m ie_mcp
```

PowerShell: set an env var with `$env:NAME = 'value'; <command>`.

Integration tests must leave no Edge or IEDriver process behind; the integration base class
asserts it, and `ie_browsers` shows what is running.

## Architecture

Upstream was a single file, `ie_mcp.py`; plan Task 3 (done) split it into the package below.
Modules the later tasks add (further `tools/` modules and `js/` scripts) are listed here as the target; `win32/processes.py`,
`win32/windows.py` and `win32/htmldoc.py` exist (Task 7); `backends.py`, the `com_*.js`
scripts, `scripts/run_integration.py` and `tests/integration/` exist (Task 8);
`win32/messages.py`, `win32/dialogs.py`, `tools/windows.py` and the rewritten `tools/dialogs.py`
exist (Task 9); `capture/store.py` and `capture/redact.py` exist, and `session.log()` goes
through `session.REDACTOR` (Task 10); `capture/proxy.py` exists (Task 11);
`capture/console.py`, `tools/console.py` and `js/instrument.js`, `hook.js`, `drain.js` exist
(Task 12); `capture/verify.py` and `tools/network.py` exist, and the proxy is wired into the
session (Task 13); `png.py` and `win32/capture.py` exist, and `cli.main()` makes the process
per-monitor DPI aware first (Task 14). Keep this section in sync with the plan and the code.

Settings live in `config.Settings`; code reads `config.SETTINGS` at call time (never
`from ie_mcp.config import SETTINGS`), so tests can replace it. Tools register themselves with
`@tool` from `ie_mcp.protocol`, and `ie_mcp/tools/__init__.py` imports every tool module.

```
ie_mcp/
  __main__.py        python -m ie_mcp -> cli.main()
  cli.py             --selftest / --install / --uninstall / stdio server start
  config.py          Settings (every env var, read once), discover_driver_path
  protocol.py        JSON-RPC loop (one thread per tools/call), @tool registry, run_tool watchdog
                     (call_budget, BUSY/BLOCKED, CALL_CONTEXTS, MODAL_PROBE notices, POST_HOOKS, AFTER_CALL)
  session.py         IeSession, build_options, SessionLease, process tracking/cleanup, DRIVER_LOCK (plain Lock);
                     owner record `starting_since` while a browser starts (ie_attach refuses meanwhile)
  backends.py        Backend interface; DriverBackend (Selenium), ComBackend (COM); target_kind, resolve_backend,
                     DEFAULT_TARGET, ATTACHED_CHAINS, TAB_ANCESTOR_CLASS (None: D15 = ie-server), js_literal;
                     attach state: ATTACH_LOCK (held only for the swap, never across COM), attached_tab,
                     attach_tab, detach_tab, prune_attached
  png.py             encode_png (stdlib zlib/struct, 8-bit RGBA, filter 0)
  win32/
    processes.py     process_identity (image + creation time), snapshot (2 s cache; fresh=True skips it), process_tree
    windows.py       ctypes only: enumerate_windows, classify, modal_probe (visible windows only, no COM), session_window_pids,
                     ATTACHED_TOPS / ATTACHED_PIDS (rebound, never mutated), pick_main_hwnd, TabInfo,
                     ie_mode_tabs (excludes other live sessions' trees), ancestor_chain
    htmldoc.py       com_apartment, com_call (retries rejected calls), ie_server_child, document_from_hwnd, document_url (pywin32, lazy)
    messages.py      the only module that posts window messages (post_message, WM_COMMAND/WM_CLOSE) + is_window, get_text (ctypes)
    dialogs.py       the safety gate: confirm_script_dialog (session window + helper/dialog-frame owner + exact direct-child
                     shape), the Q8 shape tables, post_command, wait_gone; review this file alone (no tool code)
    capture.py       enable_dpi_awareness (SetProcessDpiAwarenessContext(-4), once in cli.main), capture_window
                     (PrintWindow PW_RENDERFULLCONTENT into a 32-bit DIB section, BGRA->RGBA, GDI freed in finally)
  capture/
    store.py         NetworkEntry, CaptureStore
    redact.py        Redactor (name-segment matching, value masking)
    proxy.py         RecordingProxy, loader_tag, is_document_request, find_insertion_point
    console.py       ConsoleRecord, ConsoleStore, parse_records
    verify.py        pure capture helpers: http_target, url_seen (normalised URL / CONNECT matching), host_matches,
                     override_matches, read_proxy_override (winreg, read-only), parse_bypass, parse_upstream_proxy
  js/                ES3 scripts: select.js, grid.js, ready_probe.js, frames.js, com_run.js, com_frame.js,
                     com_prelude.js (COM request runner, frame resolution, locator/event helpers),
                     instrument.js, hook.js, drain.js
  tools/             navigation.py, read.py, interact.py, windows.py (ie_windows, ie_switch_window, ie_close_window),
                     dialogs.py (ie_dialog), network.py (ie_network, ie_request, ie_network_clear), console.py,
                     attach.py, diagnostics.py (ie_status incl. capture/modifications)
ie_mcp_fixtures/
  server.py          start_fixture_server (origins 127.0.0.1 and localhost), requests log, python -m ie_mcp_fixtures
  site/              legacy fixture pages
scripts/
  spike_ie_mode.py   plan Task 2 diagnostic
  run_integration.py integration runner (sets env, fails on skips)
tests/
  test_*.py          unit tests (mocked, no browser)
  integration/       base.py (IntegrationTestCase: fixture server, call/text via protocol.handle,
                     restart_session, asserts recorded PIDs gone); it_*.py real IE-mode tests
```

Key design points. These describe the **target state** the plan builds; until a task lands,
the upstream code behaves as before:

- **One MCP process owns one long-lived Selenium session** (frame-based apps lose state if the
  session is recreated). Parallel agents use different `IE_SESSION_ID`s.
- **Two backends.** `DriverBackend` (Selenium) is the default. `ComBackend` reaches an IE
  document directly through `WM_HTML_GETOBJECT` on its `Internet Explorer_Server` window. It
  serves modal dialogs the driver cannot reach and tabs the user opened. Wait loops call
  `backend.refresh()` before every poll and treat a missing document as not ready until the
  timeout. A `tab:` target resolves its server window again on every call and never falls back
  to the top window (it raises "call ie_tabs and ie_attach again").
- **The `target` argument (plan Task 8, done).** `ie_html`, `ie_text`, `ie_js`, `ie_click`,
  `ie_fill`, `ie_select`, `ie_get`, `ie_frames`, `ie_wait_element`, `ie_wait_text`, `ie_grid`,
  `ie_scroll`, `ie_press_key` and `ie_hover` take an optional `target`: omitted (or
  `backends.DEFAULT_TARGET`, set by Task 15) is the driver; `hwnd:<n>` and `tab:<n>` act over
  COM and have `needs_driver` False. Over COM every DOM access is script (D7): the request runs
  as `new Function(S)` inside `execScript`, a frame path is resolved in script and run in the
  frame window, results come back through the window expandos `__iemcp_r`/`__iemcp_e` (cleared
  again after each read), element handles carry their locator and are re-resolved in each
  script, and input is synthetic (script events, a timer click, posted `WM_KEYDOWN`/`WM_KEYUP`
  in documentMode 7+). Locators: id, name, tag, class, link_text, css (documentMode 8+); no
  xpath. `ie_html` over COM returns `outerHTML` (no doctype).
- **COM lifetime.** The tool-call worker thread enters one COM apartment (`com_apartment()`)
  for the whole call, and every hook sub-thread enters its own. A `ComBackend` is created per
  call (or per hook thread), and **no COM object outlives the thread that obtained it**. The
  modal probe is Win32-only and never touches COM. Read COM expandos with
  `IDispatchEx.GetDispID(name, 1)` + `InvokeEx` (D7: `getattr` fails in documentMode 11), and
  never with attribute syntax inside a class: Python mangles `__names`. `ComBackend` refuses
  use from a thread other than the one that created it.
- **No tool call may hang the server (plan Task 6, done).** `handle()` routes every
  `tools/call` through `protocol.run_tool`; `serve()` only adds threading (one thread per
  `tools/call`, responses written under a lock, possibly out of order; at EOF it waits for the
  calls still running). `call_budget` = `max(IE_TOOL_TIMEOUT, spec.budget(args))`; tools declare
  budgets with `locator_budget`, `wait_budget` and `attach_budget`, built from the same
  defaults the tool uses (`number_arg` ignores bad values). A `needs_driver(args)` call takes
  `session.DRIVER_LOCK` (a plain `Lock`: the POST_HOOKS sub-thread releases it) with
  `acquire(timeout=budget)` and answers `BUSY: <tool> has held the browser for <n>s` (`BLOCKED:
  modal dialog open` if `MODAL_PROBE()` finds windows). The tool then runs in a daemon worker;
  past the budget the caller answers `BLOCKED: ... the action may still complete`, the worker
  keeps the lock until it returns, then discards its result and runs no hooks. The lock is
  released exactly once on every path (`_Hold`). The worker enters every `CALL_CONTEXTS`
  context around the whole call, and the POST_HOOKS thread (3 s wait, releases the lock) and
  each AFTER_CALL thread (2 s wait each, no lock) enter them again. Every result gets a
  `NOTICE:` item per open window. `ie_close(force=true)`, `ie_sleep`, `ie_browsers`,
  `ie_kill_orphans` and `ie_status` have `needs_driver=False` (`ie_status` takes the lock
  itself for 0.5 s, through `protocol.driver_hold` so BUSY names it, and reports `"driver": "busy"` otherwise); `ie_close(force=true)` cancels
  running wait loops and kills only this session's own Edge and IEDriver, after checking
  their identity. Window and dialog tools work without the driver. `ie_sleep` and the `wait=`
  pauses are not cancellable yet.
- **Async click (plan Task 6).** `ie_click mode=async` (default `IE_CLICK_MODE`, `driver` per
  spike D1) queues `e.click()` with `setTimeout`, so it cannot block on a dialog; it is
  script-initiated (no mousedown/mouseup/focus, the pop-up blocker may block `window.open`,
  spike D8). `double`/`right` always use the driver. The post-call hooks are skipped for a
  call that queued its effect: `ToolSpec.defers_effect` (set by `ie_click` from the click's
  effective mode, explicit `mode` else `IE_CLICK_MODE`; default `args["mode"] == "async"`).
- **Window and dialog tools (plan Task 9, done).** `ie_windows` (`needs_driver=False`) lists
  `enumerate_windows(..., with_url=True)` with an `hwnd:<n>` target for every page window and
  dialog; only when `MODAL_PROBE()` is empty and `driver_hold` gets the lock within 0.5 s does it
  add `driver_handle`/`current` (matched by title prefix, then URL; it switches through every
  handle and always back). `ie_switch_window` (handle | title_contains | main) retries for 5 s,
  because IEDriver lists a new window late. `ie_close_window`: `hwnd:<n>` closes over COM
  (`ComBackend.close_window`, `parentWindow.close()`), else `WM_CLOSE`; a driver handle is
  switched to, closed, and the driver goes back to main; main, attached tops and system dialogs
  are refused. **Never send a message to a `#32770` that `win32/dialogs.confirm_script_dialog` has
  not proven:** it must be a `script_dialog` of the session (owner chain), visible, a `#32770`,
  have an IMMEDIATE owner of class `Alternate Modal Top Most` (in the session PIDs) or
  `Internet Explorer_TridentDlgFrame` (D6/Q8; a system dialog is owned by the browser's top window),
  and have exactly the control set (DIRECT children, `windows.enum_children`) of a classic alert
  (Button 2, Static 20, Static 65535), a classic confirm (Button 1, Button 2, Static 20,
  Static 65535) or a TaskDialog (DirectUIHWND 0); a prompt's sets (with and without the Button
  57089 check box) pass only for closing. An `onbeforeunload` TaskDialog may be helper-owned
  with the same shape, so (Task 12) **accepting a TaskDialog** (`WM_COMMAND` 1; window path, `hwnd:`
  target or the no-target owned-window path) needs (1) a COM drain of the owner document that
  reports an alert/confirm/prompt in flight in the page's own wrapper (`__iemcp.open` > 0; a failed
  drain or no reachable document refuses) and (2) a pending `dialog-open` record
  (`ConsoleStore.take_pending_dialog`: at most 30 s old, its page-assigned `dialog_id` (level
  `confirm#<id>`) not closed by a `dialog` record from ANY `where`, and not already used: one
  record, one accept); with `IE_INSTRUMENT=0`, or when either is missing, it is refused with
  nothing sent. `where` is the drain route, never a key for pairing. Dismiss and the
  classic alert/confirm paths are unchanged; answers carry the matching record as `evidence`.
  Messages are posted (never sent) through `win32/messages.py`. `ie_dialog` is `needs_driver=lambda a: a.get("target") is None`;
  a target other than `hwnd:<n>` is refused before the lock. Without a target it tries the
  driver alert, but skips it while a modal HTML dialog is open (a driver call can block) and
  answers an owned alert/confirm window instead; a prompt is answered only by the driver alert
  (`dialogs.PROMPT_DRIVER_REACH` records D14), so a prompt inside a modal dialog gets "prompt not
  reachable: ask the user". Without a session `ie_dialog` no longer starts a browser.
- **Post-call instrumentation** is one `execute_script` (`js/hook.js`, `tools/console.py`
  `instrument_hook`) from the top document that walks the same-origin frames in JS. It holds the
  driver lock until it returns, and it is skipped while a dialog is open or after an async click.
- **Page instrumentation (plan Task 12, done).** `js/instrument.js` (ES3, idempotent through
  `window.__iemcp`) wraps `onerror` (chained, re-chained by the hook when the page replaces it),
  `alert`/`confirm`/`prompt` (`dialog-open` pushed before the call), `showModalDialog`, `open`,
  existing `console` methods (all of them with `IE_CONSOLE_STUB=1`) and, on https pages or when
  the proxy is off or blind, native `XMLHttpRequest`. Originals of console methods and of XHR
  `open`/`send` stay under `__iemcp_<name>` on their owner, because a detached call can fail in
  old IE. `hook.js`/`drain.js` inject with `execScript` where it exists, else a script element
  that is removed at once, then drain; `dialog_drain` (`AFTER_CALL`, no lock) does the same over
  COM for `html_dialog`s, the page windows behind a script dialog, an explicit `hwnd:`/`tab:`
  target (POST_HOOKS stay driver-only) and a `tab:` `DEFAULT_TARGET`. `tools/console.py`
  `ingest` stores records in `STORE` (redacted); `xhr` records also go to `NETWORK_STORE` as
  `source="page"` entries (into `SESSION.store`, the session's CaptureStore, unless a test sets
  `NETWORK_STORE`). `IE_INSTRUMENT=0`: both hooks return at once.
  Records of a dialog that closes between two calls are lost; load-time errors of the first page
  are not captured (D11).
- **Network capture wiring (plan Task 13, done).** `IE_CAPTURE` defaults to 1 (D12 = no).
  `IeSession._build` starts a `RecordingProxy` BEFORE the driver, bound to `127.0.0.1` (port 0)
  and `build_options(proxy_port=..., bypass=IE_CAPTURE_BYPASS)` points only the browser at it
  (`use_per_process_proxy=True`, `noProxy` a list). `instrument_js` is flags
  (`__iemcp_stub`, `__iemcp_wrap_xhr=false`) + `instrument.js`, None with `IE_INSTRUMENT=0`;
  `upstream_proxy`/`no_cache` come from `IE_CAPTURE_UPSTREAM_PROXY`/`IE_CAPTURE_NO_CACHE`. One
  proxy per browser instance, one `CaptureStore` per `IeSession` (created at the first build,
  also with `IE_CAPTURE=0`, so page-source XHR entries have a home; kept across `ie_close`;
  rebuilt only when `IE_CAPTURE_MAX_BODY`/`IE_CAPTURE_JSONL` change). The proxy is stopped on
  every teardown path: `close()`, `quit_browser()` (so the `ensure` retry loop too),
  `force_kill()` and a failed `_build`. `IE_CAPTURE_JSONL=1` writes
  `<session dir>/capture.jsonl`, deleted by `close()` (not `ie_close`) unless
  `IE_KEEP_ARTIFACTS=1`. A proxy that cannot start leaves `capture_active` False and the
  session works (page-level XHR wrapper). A malformed `IE_CAPTURE_UPSTREAM_PROXY` is NOT raised
  at settings load (the server must still start and list its tools): it is kept in
  `Settings.capture_upstream_proxy_error` and raised as a one-line `RuntimeError` naming the
  variable by `ensure()`/`_start_capture` (and shown as `capture_config_error`).
  `IeSession.capture_active` is `None` (not verified), `True` (the proxy saw a navigation) or
  `False` (it did not). The session's initial URL is never proxied (D11), so an `ie_open` that
  creates the browser leaves it `None` and says nothing; the first `ie_open`/`ie_goto` to an
  http(s) URL (host not in `IE_CAPTURE_BYPASS`) after creation polls `proxy.seen` for 5 s
  (`session.VERIFY_WAIT`, added to both tools' budgets), comparing normalised URLs (scheme and
  host case, default port, fragment; for https a CONNECT to `host:port`), and sets True, or False
  plus `NOTICE: network capture is off: the browser did not use the proxy`. `about:`/`file:`
  leave it `None`; a miss for the browser's own initial URL concludes nothing (browser-cache
  hit); a False verdict is rechecked without waiting on later navigations. The zone notice
  (`ZONE_NOTICE`) is added when capture is verified and the host matches a `ProxyOverride`
  entry (read once at proxy start with `winreg`, `KEY_READ` only) or is dotless with `<local>`
  listed, and is not in `IE_CAPTURE_BYPASS`.
- **ie_status capture fields and the network tools (Task 13).** `ie_status` adds `capture`
  (`off`, `proxy`, `page-only`, `unverified`), `capture_note` (what is not captured: the initial
  URL, fresh cache hits, HTTPS content; the bypass hosts when `IE_CAPTURE_BYPASS` is set; the
  zone sentence), `capture_internal_errors` (`{count, last}`, redacted, only when the proxy
  recorded bugs), `capture_config_error`, and `modifications`: every Fidelity-table row whose
  switch is on, each ending `(off: ...)`. The loader, Accept-Encoding and no-store rows need
  capture AND `IE_INSTRUMENT=1`; the proxy rows disappear when `capture_active` is False; the
  async-click, COM-input and COM-wrapper rows are always listed with their trigger.
  `diagnostics.status_info()` builds the browser-free part. `ie_network`, `ie_request` and
  `ie_network_clear` (`tools/network.py`, `needs_driver=False`) read `SESSION.store`;
  `ie_request` decodes bodies with the Content-Type charset, then `referer_charset`, then
  cp1252 (form bodies are percent-decoded the same way), decodes gzip/deflate from a copy
  (4 MiB cap, said when hit) and summarises binary bodies as `<binary N bytes>`.
- **Network capture** comes from a streaming local recording proxy that only the IE-mode
  browser process uses (wiring: the previous point). **`capture/proxy.py` (plan Task 11, done):**
  `RecordingProxy(store, host, port, instrument_js, upstream_proxy, no_cache)` forwards only
  absolute-form http requests to their own target (or to `upstream_proxy`), keeps one upstream
  `HTTPConnection` per client connection and host:port (one per client connection when chained),
  tunnels CONNECT (entry `tunnel=True`, recorded at tunnel-up), answers `/__iemcp__/` itself
  (`instrument_js`, else 404; never forwarded or recorded), refuses origin-form/non-http requests
  with 400 (not recorded) and requests to itself with 508 (recorded). It streams every response,
  copies at most `store.max_body` bytes of each body into the entry, and adds the entry after the
  last byte reaches the client. Injection runs only when `instrument_js` is set and
  `is_document_request` holds (D10: `Accept` contains `text/html`; such requests are sent with
  `Accept-Encoding: identity`); `find_insertion_point` decides on at most 64 KB. Bodies are stored
  as received (a gzip response stays compressed). `seen` (1000), `first_request` and
  `doc_charsets` (1000) feed the D11 verification and `referer_charset`; `internal_errors`
  (bugs only) is shown by `ie_status`.
  `stop()` wakes handler threads blocked in `recv`/`select` by closing their sockets: on Windows
  `shutdown()` alone does not wake them.
- **Settings and tool guards (plan Task 4, done).** Interaction mode defaults to `background`
  (native input, no window focus). `IE_CLEAN_SESSION` is opt-in. `IE_PYDEPS` is the only way a
  path is added to `sys.path` (there is no implicit `../.pydeps`). `IE_DRIVER_PATH` unset means
  discovery in the per-user Selenium cache (`config.discover_driver_path`, win32 first). `ie_upload`
  works only with `IE_UPLOAD_DIR` set and only on files that `Path.resolve()` places inside it.
  `ie_screenshot` `save_path` is a bare file name saved in `<session dir>/screenshots/` and kept.
- **Window screenshots (plan Task 14, done).** `ie_screenshot scope=window` (`needs_driver=False`,
  also for any `target`; default scope: window with a target, element with `value`, else page)
  captures with `win32/capture.capture_window`: the main window (`SESSION.main_hwnd`, or the
  root of the `tab:` `DEFAULT_TARGET` through `backends.default_tab_hwnd`), then every visible
  session `html_dialog`/`script_dialog` bottom-most to top-most, one image each, plus a text item
  listing them. Main and attached tops are captured only while `IsWindow` holds and their PID is
  a session PID (attached tops: in `ATTACHED_TOPS`), so a recycled stale hwnd is refused.
  `target` accepts only `hwnd:<n>` of a window `enumerate_windows` lists for the session. System
  dialogs are listed, never captured; a minimized window is reported, never restored; nothing
  is posted to any window. Several images save as `name`, `name-2`, ... (each name validated
  before the first capture); a failed file write is reported and the image still returned.
  `ie_text` falls back to `document.body.innerText`; `ie_frames` reads `name`/`id`/`src` with
  `js/frames.js`. `ie_open` needs an http(s) URL in IE mode.
- **Attached tabs (plan Task 15, done).** `tools/attach.py`: `ie_tabs`, `ie_attach`, `ie_detach`
  (`needs_driver=False`). `ie_attach` looks the tab up and probes it over COM without any lock,
  then swaps `DEFAULT_TARGET`, `ATTACHED_CHAINS`, `ATTACHED_TOPS` and `ATTACHED_PIDS` under
  `backends.ATTACH_LOCK` (re-checking the windows still exist); a failed re-attach keeps the old
  tab. While attached, `run_tool` refuses any tool whose `needs_driver(args)` is true and that has
  no explicit `target` ("call ie_detach first"); it prunes a dead tab first, as does
  `protocol._probe`. The attach drains the tab once (`instrumented` = the drain answered).
  `ie_status modifications` lists the "instrumentation left after ie_detach" row once attached.
- **Session lifecycle (plan Task 5, done).** `session.kill_verified` is the only kill path
  (`taskkill /F /PID`, no `/T`, only when `process_identity` still equals the recorded image
  name + creation time; a recorded `None` or malformed identity never kills). `owner.json`
  records `driver_identity`/`edge_identity`; a dead holder's record is kept as
  `owner.stale-<pid>.json` and `cleanup_stale_sessions` (globs `*/owner*.json`) needs both the
  profile and the identity match. Our Edge is adopted only if it is the single new IE-mode
  row with a new `IEDriver-<uuid>` profile whose parent PID is our IEDriver (verified on the
  development machine). `webdriver.Ie` creation runs in a thread joined for 60 s
  (`SESSION_CREATE_TIMEOUT`); on timeout our own IEDriver and new Edge are reaped and
  `ensure()` retries while holding the lease. `quit_browser()` keeps the lease, `close()`
  (cli exit) releases it, unless a process survived the kill. `SESSION.cancel` stops
  `ie_wait_ready`/`ie_wait_text`/`ie_wait_element`/`find` with `Cancelled`; `ensure()` clears
  it. `ie_close(force=true)` never calls the driver. `ie_sleep` and `wait=` sleeps are not
  cancellable yet.

## Trident rules for injected JavaScript

Pages may run in IE5 quirks, IE7, IE8 or later document modes. Every script the server injects
must be **ES3**:

- no `JSON` (absent in IE7/quirks); return delimited strings and parse in Python. Write the
  separators as the escapes `\x1e` (record) and `\x1f` (field), never as raw bytes, and escape
  them inside data;
- no `let`/`const`, arrow functions, template literals, `Array.prototype.forEach/map/filter`,
  `Object.keys`, `String.prototype.trim`, getters/setters;
- no `querySelector(All)` unless `document.documentMode >= 8`; prefer `getElementById`,
  `getElementsByTagName`, `document.all`;
- events: `attachEvent`/`fireEvent` exist only in old modes; feature-detect both models;
- host methods (`alert`, `showModalDialog`, `open`, `console.*`) have no `.apply`/`.call` in
  IE < 9: call them directly, and pass exactly as many arguments as the caller passed (branch
  on `arguments.length`), so no explicit `undefined` reaches them;
- wrap every probe in `try/catch`; cross-origin frames throw on access.

`tests/test_js_resources.py` (plan Task 3) lints every file in `ie_mcp/js/` for these rules.

Selenium's JS "atoms" fail on Trident: `el.text`, `get_attribute`, `Select`. Use
`execute_script("return arguments[0].innerText")`, `getAttribute`, and the JS select helper.

## Injection rules (proxy)

- **What gets inserted.** Only a tiny loader:
  `<script type="text/javascript" src="http://<document host:port>/__iemcp__/i.js"></script>`.
  The URL is absolute, so a `<base href>` cannot redirect it. The proxy answers `/__iemcp__/`
  itself, on any host; it is never forwarded and never recorded. `i.js` removes its own
  `<script>` element when it runs, so the DOM matches the original. Keeping the insert small
  keeps a late `<meta charset>` inside IE's charset prescan.
- **Only into documents requested as pages.** Inject only when both hold:
  - the request is a document request (per spike D10, an `Accept` header containing
    `text/html`);
  - the response is `text/html` 200 whose first tag (after an optional UTF-8 BOM, whitespace
    and comments) is a doctype or `<html`, `<head`, `<body` or `<frameset`.

  Never inject into fragments or plain text, even with a `text/html` content type: legacy XHR
  endpoints often return those, and their `responseText` must stay byte-identical. Also skip
  UTF-16 bodies, attachments, HEAD/204/304 responses, and head-less framesets (the post-call
  hook instruments those after load).
- **Where.** Find the anchor: the end of `<head …>`, else the end of `<html …>`, else the end
  of the doctype. From the anchor, skip whitespace, comments, and `<meta>`, `<title>` and
  `<base>` elements, then insert. A document whose first tag is `<body` gets the loader right
  before `<body`; one whose first tag is `<frameset` gets nothing. Match tags
  case-insensitively. A tag name needs a delimiter after it (`<head` is not `<header`), a `>`
  inside a quoted attribute does not end a tag, and tags inside comments are ignored.
  - IE ignores an `X-UA-Compatible` meta that comes after a script, which would change the
    document mode.
  - Never insert before the doctype or a byte-order mark.
- **Caching.** On every modified response, drop `ETag`/`Last-Modified` and set
  `Cache-Control: no-store`, so injected copies never land in the per-user WinINet cache that
  the user's own IE-mode browsing shares.
- **Streaming.** Stream every response. For a document that may get the loader, buffer while
  `find_insertion_point` says `NEED_MORE` (at most 64 KB). An insertion point is final only
  once the first tag after the skipped run has fully arrived, so a head split across packets
  can never put the loader before an `X-UA-Compatible` meta.

## Known IE-mode / IEDriver quirks

- In a tab the user opened, script dialogs and `showModalDialog` windows are owned by an
  ownerless `Alternate Modal Top Most` helper of the tab's IE process, not by the Edge window.
  `enumerate_windows` takes such a helper as the attached tab's only when its pid is in
  `ATTACHED_PIDS` and exactly one visible window carries its (non-empty) caption, an attached
  top.
- Navigate **during session creation** (`initial_browser_url`). Navigating afterwards across
  the Protected-Mode boundary can make the driver lose the browser.
- `page_load_strategy = "none"`; readiness is detected with `ie_wait_ready`.
- "Unable to get browser" is often transient while a page or frame navigates; retry.
- Re-apply the frame path on every call; frames reload and references go stale.
- IEDriver's default `unhandledPromptBehavior` is "dismiss and notify": any driver command
  silently cancels an open alert/confirm. `session.build_options` sets `ignore`.
- A WebDriver click whose handler opens `alert`/`confirm`/`showModalDialog` can block until the
  dialog closes. (On the development machine, Edge 146 + IEDriver 4.14, the spike saw it return
  at once with the dialog open; keep the watchdog anyway.)
- Synthetic input (`native_events=False`) did nothing on the development machine: `click`
  raises `ElementNotInteractableException` or silently no-ops and `send_keys` leaves the field
  empty. Native events with `require_window_focus=False` worked there while another window had
  focus (spike D17, one machine; about 1 click in 6 was lost, so check a click's effect). So
  `IE_INTERACTION_MODE=background` (the default) is `native_events=True`,
  `require_window_focus=False`: native input sent as window messages without taking focus or the
  user's mouse. `native` (`require_window_focus=True`) is the fallback; it takes focus and can mix
  with the user's own keystrokes and mouse.
- An inherited `__COMPAT_LAYER` environment variable (the Claude desktop app sets one) makes
  Edge relaunch itself, so IEDriver cannot find the IE window. Start IEDriver with that variable
  removed from its environment (spike D16; `session.driver_environment`, passed as the
  `Service` `env`). Do not use `ie.ignoreprocessmatch`.
- With `--ie-mode-force`, `about:blank` renders in Chromium and IEDriver finds no IE window:
  always start a session on an http(s) URL.
- Session creation sometimes hangs inside IEDriver after Edge starts; bound it and retry.
- The per-process proxy does not see the session's **initial URL** (`initial_browser_url`);
  everything after it is proxied (spike D11).
- Over COM, a documentMode 11 document hides window expandos and DOM methods from win32com's
  dynamic dispatch (`getattr` fails; mode 7 works; modes 8–10 untested). Run everything as
  script through `execScript` and read results
  with `IDispatchEx.GetDispID(name, fdexNameCaseSensitive)` + `InvokeEx` (spike D7). Reach
  `parentWindow` and `execScript` through the raw dispatch (`GetIDsOfNames` + `Invoke`). JScript
  numbers come back as doubles (`11.0`).
- In a documentMode 7 document `eval('(function(){...})')` returns `undefined`, so COM requests
  run their body with `new Function`. The frame windows of a mode-7 top expose no `eval` or
  `Function` to script but do expose `execScript`; in mode 11 `eval` exists and `execScript` is
  not visible from script (it is on the COM `IHTMLWindow2`). Task 8 measured both.
- Posted key messages for extended keys (arrows, Home, End, PageUp/PageDown, Insert, Delete)
  need bit 24 set in `lParam`.
- `ie_frames` index order (document order of frame + iframe elements) equals the driver's
  `switch_to.frame(index)` order and COM `window.frames[index]` (Task 8, frameset.htm).
- IE shows a script dialog in one of two forms. The first dialog of a page is a classic
  MessageBox (alert: Button 2; confirm: Buttons 1 and 2; both with Static 20 and Static 65535,
  so the message is readable); a later dialog of the same page is a TaskDialog with the "don't
  let this page create more messages" check box (one `DirectUIHWND` direct child with about a
  dozen hidden descendants; no readable message; a prompt then also has Button 57089). Spike Q8
  recorded one of each; an alert can be either. Always compare the direct children, never all
  descendants. OK is control 1 and Cancel 2 in a confirm and a TaskDialog; a classic alert
  closes with 2.
- With `unhandledPromptBehavior=ignore`, driver commands raise `UnexpectedAlertPresentException`
  while a dialog is open. `IeSession.ensure()` treats that as a live browser; before, its liveness
  probe took it for a dead session and recreated the browser, destroying the open dialog.
- Integration tests must never leave a browser behind: `tests/integration/base.py` remembers every
  (pid, identity) the session ever had (it can be recreated mid-class) and `finish_session` kills
  any survivor by recorded identity and fails the class. Never end a run by killing the runner
  (`timeout`, TaskStop): that is how unrecorded processes were left. Unit tests must never reach
  `SESSION.ensure()` (it starts a real browser): patch it to raise, as `test_window_tools` does.
- IEDriver lists a new pop-up or `showModalDialog` window in `window_handles` about a second
  after the window exists (the first read after the click shows one handle, later reads two).
  Any "find the new window" step must retry (`tools/windows.SWITCH_WAIT`).
- A COM click is queued on the page's timer; when it closes its own window (a dialog's OK/Cancel
  button) the window can be gone before the result is read back, and reading `__iemcp_e` fails
  with "Member not found". `ComBackend._run` checks whether the IE server window is gone and
  raises `WindowClosed`; `click` treats that as success. The same race exists for a COM click on
  a form's submit button: the page can navigate before the result is read back (seen once in
  Task 13 as `it_read_interact.test_com_form_round_trip` failing with "Member not found", passing
  on rerun; not fixed).
- The capture proxy listens on `127.0.0.1` only (never a routable address) and the browser is
  pointed at it with `<-loopback>` in `noProxy`; without that IE bypasses any proxy for loopback
  hosts and the fixture site would never be captured. The browser's initial URL is the only
  navigation it does not carry (D11), and a page the browser has fresh in its cache never
  reaches it, so a navigation the proxy did not see proves nothing for the initial URL.
- Native input without window focus loses about 1 click in 6 (D17): integration tests whose
  next step needs a click's effect click again until the effect shows
  (`it_network.click_until`).
- A second driver `clear()` on the same field can fail with "JavaScript error clearing the
  element" (seen in Task 8; not yet explained).
- Script dialogs are `#32770` windows owned by an `Alternate Modal Top Most` helper window (no
  owner of its own, same process), not by the browser window; inside a `showModalDialog`
  window they are owned by its `Internet Explorer_TridentDlgFrame`. A `confirm` is a TaskDialog
  (`DirectUIHWND` child only): its text is not readable through child windows, but
  `WM_COMMAND` 1/2 answers it. The alert's OK button has control ID 2. A `prompt` has an `Edit`
  child (spike Q8/D14).
- `windows.modal_probe` reports only *visible* windows (Task 7 ruling): Edge processes may own
  hidden `#32770` windows (assumed, not measured), which would otherwise put a false NOTICE on
  every tool result.
- `processes.snapshot()` is cached for 2 s. A before/after comparison (the `main_hwnd` capture
  around driver creation) must call it with `fresh=True`.
- The fixture's page without a doctype (`quirks.htm`) runs in documentMode 11 with
  `compatMode == "BackCompat"`, not in mode 5 (spike Q6).
- Pass the `proxy` capability's `noProxy` as a **list** (`["<-loopback>"]`). IEDriver rejects a
  string; selenium's `Proxy` class converts strings, but raw capability dicts do not. Without
  `use_per_process_proxy=True`, IEDriver writes the user's global WinINet proxy settings; never
  build a proxy capability without it.
- Selenium's HTTP client to the driver must have a finite timeout, or a dead driver hangs the
  calling thread forever. `webdriver.Ie` builds its own `ClientConfig(timeout=120)` and takes no
  `client_config`, so `session.apply_driver_timeout` raises it to `DRIVER_HTTP_TIMEOUT` (300 s)
  on the created driver through `command_executor.client_config` (selenium 4.52;
  `RemoteConnection.set_timeout` is deprecated there). Session creation runs under the 120 s.
- If the per-process proxy hook fails, IEDriver only logs "Proxy will not be set!"; the session
  must verify that the proxy saw traffic.
- `ensure_clean_session=True` clears the user's whole IE-mode cookie store and cache, not only
  the session's. Upstream forced it on; it is opt-in (`IE_CLEAN_SESSION=1`) and `build_options`
  takes it from `settings.clean_session`.
- With default Internet Options IE maps `127.0.0.1` to the Internet zone (Protected Mode on)
  and the dotless `localhost` to the Local intranet zone (Protected Mode off, intranet
  Compatibility View possible). The fixture's two origins therefore sit on opposite sides of
  the Protected-Mode boundary: open alt-origin pages in their own session, and record the zone
  (`MapUrlToZone`, read-only) next to cross-origin results.

Write a code comment for every new quirk you work around; they are rarely obvious.

## Safety rules

- Never write the registry, change system, proxy, firewall or Edge settings, or install
  certificates. The only exception is upstream's opt-in `IE_SITE_LIST` policy writer.
- Only kill processes proven to belong to this server: this session's own recorded PIDs, or a
  dead session's PIDs matched by Edge profile. Either way, the recorded image name and creation
  time must still match (`kill_verified`), because Windows reuses PIDs. A process with no
  recorded identity is never killed, and `taskkill` runs without `/T`. Process trees built
  from parent PIDs are fine for listing, never for killing.
- Never click, accept or dismiss a **system dialog** (file open/save, print, security or
  ActiveX prompts, credential prompts, "leave this page?"). Report it and let the user act.
  Only script alerts the session can attribute to itself are handled.
- No outbound network calls of the server's own. The proxy only forwards the browser's traffic
  (directly, or to `IE_CAPTURE_UPSTREAM_PROXY` when set).
- Captured traffic and console records stay in memory. A JSONL copy is written only with
  `IE_CAPTURE_JSONL=1`, and is deleted on close unless `IE_KEEP_ARTIFACTS=1`. Screenshots saved
  by name go to the session's `screenshots` folder and are kept. Never commit captured data.
- Redaction is on by default. It matches field and header names by word segment, plus
  substrings for password-like names (so `lastName`, `compass` and `keyword` stay visible, while
  `newpassword` and `USERPWD` are caught). It masks every non-trivial value it has redacted
  once, wherever it appears again (short values only as whole tokens): bodies, URLs, console
  text, dialog summaries and the server log. In free text (`redact_text`), `name: value` lines
  count only for secret header names (exact, or hyphenated like `X-Auth-Token`) and
  password-like or configured field names, so log prose such as `no such session: ...` is never
  learned as a secret; `name=value` and JSON pairs use the full rules. Secrets are recorded
  before anything is masked or written.
- File access from tools (upload, `save_path`) is confined to configured directories.
- `ie_tabs`/`ie_attach` never list or attach to windows owned by another live ie-mcp session.

## Working rules

- Follow the plan; TDD where a test can run without a browser, integration tests (through
  `scripts/run_integration.py`) for the rest. "All OK" means **no skipped tests**.
- After every change, have a fresh reviewer (code-review subagent) check it against the plan
  task before moving on; fix Critical and Important findings first.
- One commit per plan task on the `harness` branch; conventional commit messages (`feat:`,
  `fix:`, `docs:`, `test:`, `refactor:`). Do not push, open PRs, or contact upstream without
  asking the user.
- Keep upstream attribution in `LICENSE` and the README. Note in the README that this is a fork.
- Update this file, the spec's Fidelity table and the README whenever a tool, env var, command
  or page modification changes.
