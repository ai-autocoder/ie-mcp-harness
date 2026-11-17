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
Work through the plan in order. Task 2 is a spike whose recorded decisions (D1–D8) select
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
| IEDriverServer | Official Selenium IE driver, **32-bit (Win32) 4.14.0** recommended. Set `IE_DRIVER_PATH`. Automatic discovery in the per-user Selenium cache arrives in plan Task 4; until then upstream hardcodes a path that will not exist on your machine |
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
# FAILS if any test was skipped (from plan Task 7).
python scripts/run_integration.py            # all
python scripts/run_integration.py it_modal   # one module

# serve the fixture site by hand; prints both origins (--host, --port, --alt-port)
python -m ie_mcp_fixtures

# check the setup end to end
ie-mcp --selftest            # after plan Task 3; before that: python ie_mcp.py --selftest

# register a dev copy with Claude Code for manual testing (after plan Task 3)
claude mcp add ie-mcp-dev -e IE_INTERACTION_MODE=background -- python -m ie_mcp
```

PowerShell: set an env var with `$env:NAME = 'value'; <command>`.

Integration tests must leave no Edge or IEDriver process behind; the integration base class
asserts it, and `ie_browsers` shows what is running.

## Architecture

Upstream is a single file, `ie_mcp.py`. Plan Task 3 splits it into the package below; until
then the single file is the code. Keep this section in sync with the plan and the code.

```
ie_mcp/
  __main__.py        python -m ie_mcp -> cli.main()
  cli.py             --selftest / --install / --uninstall / stdio server start
  config.py          Settings (every env var, read once), discover_driver_path
  protocol.py        JSON-RPC loop, @tool registry, run_tool watchdog, notices, post-call hooks
  session.py         IeSession, build_options, SessionLease, process tracking/cleanup, DRIVER_LOCK
  backends.py        Backend interface; DriverBackend (Selenium), ComBackend (COM); resolve_backend
  png.py             encode_png (stdlib zlib)
  win32/
    processes.py     Toolhelp32 process snapshot (ctypes), process tree
    windows.py       window enumeration and classification, modal_probe, ie_mode_tabs
    htmldoc.py       com_apartment, ie_server_child, document_from_hwnd (pywin32)
    capture.py       PrintWindow capture, DPI awareness
  capture/
    store.py         NetworkEntry, CaptureStore
    redact.py        Redactor (field/header regexes, value masking)
    proxy.py         RecordingProxy, inject_into_html
    console.py       ConsoleRecord, ConsoleStore, parse_records
  js/                ES3 scripts: select.js, grid.js, ready_probe.js, frames.js, instrument.js, drain.js
  tools/             navigation.py, read.py, interact.py, dialogs.py, windows.py, network.py,
                     console.py, attach.py, diagnostics.py
ie_mcp_fixtures/
  server.py          start_fixture_server (two origins), python -m ie_mcp_fixtures
  site/              legacy fixture pages
scripts/
  spike_ie_mode.py   plan Task 2 diagnostic
  run_integration.py integration runner (sets env, fails on skips)
tests/
  test_*.py          unit tests (mocked, no browser)
  integration/       it_*.py real IE-mode tests against the fixture site
```

Key design points:

- **One MCP process owns one long-lived Selenium session** (frame-based apps lose state if the
  session is recreated). Parallel agents use different `IE_SESSION_ID`s.
- **Two backends.** `DriverBackend` (Selenium) is the default. `ComBackend` reaches an IE
  document directly through `WM_HTML_GETOBJECT` on its `Internet Explorer_Server` window. It
  serves modal dialogs the driver cannot reach and tabs the user opened. Every COM call runs
  inside `com_apartment()`; never keep a COM object across tool calls or threads.
- **No tool call may hang the server.** Every tool runs under a watchdog with a per-call
  budget; the driver lock is acquired with that budget; `ie_close(force=true)` kills only this
  session's own Edge and IEDriver by recorded PID. Window and dialog tools work without the
  driver.
- **Network capture** comes from a local recording proxy that only the IE-mode browser process
  uses. It is checked at session start and reported as off when the browser did not use it.

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

- Insert the script **after** the leading run of `<meta>`, `<title>` and `<base>` elements
  inside `<head>`. IE ignores an `X-UA-Compatible` meta that comes after a script, which would
  change the document mode.
- Without a `<head>`, insert after the `<html …>` tag; without that, after the doctype. Never
  insert before the doctype or before a byte-order mark.
- On every modified response, drop `ETag`/`Last-Modified` and set `Cache-Control: no-store`,
  so injected copies never land in the per-user WinINet cache that the user's own IE-mode
  browsing shares.

## Known IE-mode / IEDriver quirks

- Navigate **during session creation** (`initial_browser_url`). Navigating afterwards across
  the Protected-Mode boundary can make the driver lose the browser.
- `page_load_strategy = "none"`; readiness is detected with `ie_wait_ready`.
- "Unable to get browser" is often transient while a page or frame navigates; retry.
- Re-apply the frame path on every call; frames reload and references go stale.
- IEDriver's default `unhandledPromptBehavior` is "dismiss and notify": any driver command
  silently cancels an open alert/confirm. Plan Task 4 sets `ignore`.
- A WebDriver click whose handler opens `alert`/`confirm`/`showModalDialog` can block until the
  dialog closes.
- The `proxy` capability's `noProxy` must be a **list** (`["<-loopback>"]`); a string fails
  session creation. Without `use_per_process_proxy=True` IEDriver writes the user's global
  WinINet proxy settings; never build a proxy capability without it.
- If the per-process proxy hook fails, IEDriver only logs "Proxy will not be set!"; the session
  must verify that the proxy saw traffic.
- `ensure_clean_session=True` clears the user's whole IE-mode cookie store and cache, not only
  the session's. Upstream forces it on (`ie_mcp.py`, `IeSession._build`); plan Task 4 makes it
  opt-in (`IE_CLEAN_SESSION=1`).

Write a code comment for every new quirk you work around; they are rarely obvious.

## Safety rules

- Never write the registry, change system, proxy, firewall or Edge settings, or install
  certificates. The only exception is upstream's opt-in `IE_SITE_LIST` policy writer.
- Only kill processes proven to belong to this server: this session's own recorded PIDs, or a
  dead session's PIDs matched by Edge profile.
- Never click, accept or dismiss a **system dialog** (file open/save, print, security or
  ActiveX prompts, credential prompts, "leave this page?"). Report it and let the user act.
  Only script alerts the session can attribute to itself are handled.
- No outbound network calls of the server's own. The proxy only forwards the browser's traffic
  (directly, or to `IE_CAPTURE_UPSTREAM_PROXY` when set).
- Captured traffic, console records and screenshots stay in memory or in the session directory
  under `%TEMP%\ie-mcp\sessions\<id>`; disk copies are deleted on close unless
  `IE_KEEP_ARTIFACTS=1`. Redaction is on by default and masks every value it has redacted once,
  wherever it appears again. Never commit captured data.
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
