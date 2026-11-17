# CLAUDE.md

Guidance for coding agents working in this repository.

## What this is

`ie-mcp` is an MCP (Model Context Protocol) stdio server that lets an AI agent drive **legacy,
IE-only web applications** running in **Microsoft Edge IE mode** (the Trident engine), through
the official Selenium **IEDriverServer**.

This repository is a **fork** of [thomfilg/ie-mcp](https://github.com/thomfilg/ie-mcp) (MIT),
cloned at upstream commit `e0937ff6f1bfb51b13c72310084af3fd17d062b0`. The upstream remote is
named `upstream`; there is no `origin` until a fork is created on a hosting service.

The fork's goal: give an agent **full access** to an IE-mode page, on par with what a
Chromium automation tool gives on a modern page:

- read the DOM, text and HTML of every window, frame and modal dialog;
- interact (click, type, select, keys, hover), including inside `showModalDialog` windows;
- see every HTTP request and response the page makes;
- read console output and script errors;
- see the page as rendered (screenshots, including open modal dialogs);
- run arbitrary JavaScript in any window or frame.

**Start here:** the design is in
[docs/superpowers/specs/2026-11-17-ie-mode-harness-design.md](docs/superpowers/specs/2026-11-17-ie-mode-harness-design.md)
and the task-by-task implementation plan is in
[docs/superpowers/plans/2026-11-17-ie-mode-harness.md](docs/superpowers/plans/2026-11-17-ie-mode-harness.md).
Work through the plan in order. Task 2 is a spike whose recorded results decide details of
later tasks; do not skip it.

## Golden rule: keep it generic

This project must stay **application-agnostic**. Never add anything specific to one company,
product, customer, intranet or site:

- no real hostnames, URLs, screen names, field names, credentials, or captured data;
- no tool, default or heuristic tuned to one application;
- examples and tests use only the local fixture site in `tests/fixtures/legacy_site/`
  served from `127.0.0.1`.

If a feature is only useful for one application, it belongs in that application's own
tooling, not here.

## Environment

| Component | Requirement |
|---|---|
| OS | Windows 10/11 with an unlocked, interactive desktop (IE mode is never headless) |
| Edge | Microsoft Edge with IE mode available |
| IEDriverServer | Official Selenium IE driver, **32-bit (Win32) 4.14.0** recommended. Path via `IE_DRIVER_PATH` or the per-user Selenium cache (`%USERPROFILE%\.cache\selenium\IEDriverServer\win32\<version>\IEDriverServer.exe`) |
| Python | 3.9+ (development machine runs 3.14) |
| Runtime deps | `selenium>=4.14.0,<5` only. `pywin32` is an optional extra (`.[win32]`) for the Win32/COM features |

IEDriver launches Edge with `--ie-mode-force`, so **every URL it opens renders in IE mode**,
including `http://127.0.0.1` fixture pages. No site list or policy change is needed for tests.

Ask the user before downloading `IEDriverServer.exe` or any other binary. Never change Windows,
Edge or Internet Options security settings yourself (Protected Mode, zones, registry, system
proxy); tell the user what to set.

## Commands

```bash
# install editable, with the optional Win32 extra
pip install -e ".[win32]"

# unit tests (no browser; run anywhere on Windows)
python -m unittest discover -s tests -p "test_*.py" -v

# integration tests (open a real visible IE-mode window; need an unlocked desktop)
set IE_MCP_INTEGRATION=1
python -m unittest discover -s tests/integration -p "test_*.py" -v

# serve the fixture site by hand (prints the URL)
python -m tests.fixtures.server

# check the setup end to end
ie-mcp --selftest

# register a dev copy with Claude Code for manual testing
claude mcp add ie-mcp-dev -e IE_INTERACTION_MODE=background -- python -m ie_mcp
```

Integration tests skip themselves unless `IE_MCP_INTEGRATION=1`. They must leave no Edge or
IEDriver process behind; check with the `ie_browsers` tool or Task Manager after a run.

## Architecture

Upstream is a single file, `ie_mcp.py`. Plan Task 3 splits it into the `ie_mcp/` package below;
until then the single file is the code. Keep this section in sync when the layout changes.

```
ie_mcp/
  __main__.py        python -m ie_mcp -> cli.main()
  cli.py             --selftest / --install / --uninstall / stdio server start
  config.py          every env var, read once; driver/edge path discovery
  protocol.py        newline-delimited JSON-RPC 2.0 loop, tool registry (@tool), watchdog
  session.py         IeSession (Selenium driver), SessionLease, process tracking/cleanup
  backends.py        Backend interface; DriverBackend (Selenium), ComBackend (Win32/COM)
  win32/             ctypes/pywin32 helpers: window enumeration, WM_HTML_GETOBJECT, PrintWindow, PNG
  capture/           recording HTTP proxy, network log store, redaction
  js/                ES3 scripts injected into pages (instrument.js, ready_probe.js, grid.js, select.js)
  tools/             one module per tool group: navigation, read, interact, windows, network,
                     console, screenshot, diagnostics
tests/
  fixtures/          legacy_site/ (static pages) + server.py (local test server)
  test_*.py          unit tests (mocked, no browser)
  integration/       real IE-mode tests against the fixture site
```

Key design points:

- **One MCP process owns one long-lived Selenium session** (frame-based apps lose state if the
  session is recreated). Parallel agents use different `IE_SESSION_ID`s.
- **Two backends.** `DriverBackend` (Selenium) is the default. `ComBackend` reaches an IE
  document directly through `WM_HTML_GETOBJECT` on its `Internet Explorer_Server` window. It
  is used for modal dialogs the driver cannot reach and for attaching to a tab the user opened.
- **No tool call may hang the server.** Every tool runs under a watchdog. A click that opens a
  modal dialog blocks the opener's script thread, so window/dialog tools must keep working while
  the driver is blocked.
- **Network capture** comes from a local recording proxy that only the IE-mode browser process
  uses (per-process proxy). Page-level instrumentation adds console, errors, dialogs and XHR.

## Trident rules for injected JavaScript

Pages may run in IE5 quirks, IE7, IE8 or later document modes. Every script the server injects
must be **ES3**:

- no `JSON` (absent in IE7/quirks); return delimited strings (`\x1e` rows, `\x1f` fields) and
  parse in Python;
- no `let`/`const`, arrow functions, template literals, `Array.prototype.forEach/map/filter`,
  `Object.keys`, `String.prototype.trim`, getters/setters;
- no `querySelector(All)` unless the document mode is 8 or higher; prefer `getElementById`,
  `getElementsByTagName`, `document.all`;
- events: `attachEvent`/`fireEvent` exist only in old modes; feature-detect both models;
- wrap every probe in `try/catch`; cross-origin frames throw on access.

Selenium's JS "atoms" fail on Trident: `el.text`, `get_attribute`, `Select`. Use
`execute_script("return arguments[0].innerText")`, `getAttribute`, and the JS select helper.

## Known IE-mode / IEDriver quirks

- Navigate **during session creation** (`initial_browser_url`). Navigating afterwards across
  the Protected-Mode boundary can make the driver lose the browser.
- `page_load_strategy = "none"`; readiness is detected with `ie_wait_ready`.
- "Unable to get browser" is often transient while a page or frame navigates; retry.
- Re-apply the frame path on every call; frames reload and references go stale.
- A WebDriver click whose handler opens `alert`/`confirm`/`showModalDialog` can block until the
  dialog closes.
- `ensure_clean_session` clears the user's whole IE-mode cookie store and cache, not only the
  session's. It is opt-in (`IE_CLEAN_SESSION=1`).

Write a code comment for every new quirk you work around; they are rarely obvious.

## Safety rules

- Never write the registry, change system or Edge settings, or install certificates. The only
  exception is the existing opt-in `IE_SITE_LIST` policy writer.
- Only kill processes proven to belong to a dead session of this server (Edge profile match).
- No outbound network calls of the server's own. The proxy only forwards the browser's traffic.
- Captured traffic, console logs and screenshots stay in the session directory under
  `%TEMP%\ie-mcp\sessions\<id>`. Redact passwords, cookies and auth headers by default.
  Never commit captured data.
- File access from tools (upload, `save_path`) is confined to configured directories.

## Working rules

- Follow the plan; TDD where a test can run without a browser, integration tests for the rest.
- One commit per plan task on a feature branch; conventional commit messages (`feat:`, `fix:`,
  `docs:`, `test:`, `refactor:`). Do not push, open PRs, or contact upstream without asking the user.
- Keep upstream attribution in `LICENSE` and the README. Note in the README that this is a fork.
- Update this file and the README whenever a tool, env var or command changes.
