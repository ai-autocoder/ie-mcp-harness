# IE-mode harness: design

Date: 2026-11-17. Status: approved direction, implementation not started.

## Problem

AI coding agents can inspect and drive modern pages through Chromium tooling (DevTools
protocol, browser extensions). None of that reaches a tab running in **Edge IE mode**: the
Trident engine is invisible to Chromium extensions and to the DevTools protocol. Teams that
still maintain or migrate away from IE-only applications therefore cannot let an agent look
at the legacy UI, compare it with a replacement, or reproduce its behaviour.

Two routes were evaluated: polyfilling IE behaviour into Chromium, and driving the real IE
engine through the official IEDriverServer. The IEDriver route was chosen because findings
come from the real engine (no false differences from an imitation), and because the engine
is frozen, so a working harness stays working. Microsoft supports IE mode through at least
2029.

## Goal

An MCP server that gives an agent **full access** to IE-mode pages: read, interact, observe
network and console, and see the rendering, across windows, frames and modal dialogs.

## Non-goals

- Anything specific to one application (see CLAUDE.md, golden rule).
- Headless operation (IE mode cannot run headless).
- Decrypting HTTPS traffic (would require installing a root certificate). HTTPS requests are
  recorded as tunnels (host, port, timing) only.
- Recording or replaying test scripts; the agent drives the tools interactively.
- Supporting non-Windows platforms.

## Requirements

| ID | Requirement |
|---|---|
| R1 | Open any URL in IE mode and keep one long-lived session per MCP process (upstream behaviour, kept). |
| R2 | Read HTML (live DOM), visible text and element values/attributes of the page, any frame (nested paths), any popup window and any `showModalDialog` window. |
| R3 | Click, double/right click, type, select, press keys, hover and scroll in all of the above. |
| R4 | A tool call never blocks the server indefinitely. When a modal dialog is open, the agent is told so in every tool result and can act inside it. |
| R5 | List all windows of the session (main, popups, modal dialogs, alerts) with kind, title, URL; switch between them; close them. |
| R6 | Record every HTTP request/response of the IE-mode browser: method, URL, headers, request body, status, response headers, response body (size-capped), timing. Filter, list and fetch single entries. |
| R7 | Capture console output, script errors (`window.onerror`), alert/confirm/prompt texts, `showModalDialog`/`window.open` calls (URL, features, arguments, return value), and page XHR calls, as early in page load as possible. |
| R8 | Screenshots of the page, of an element, and of the whole browser window including open modal dialogs. |
| R9 | Run arbitrary JavaScript in any window/frame/dialog and return the result. |
| R10 | Optional: attach to an IE-mode tab the user opened by hand (keeps the user's login). |
| R11 | Safe by default: passwords, cookies and auth headers redacted in captures; no registry/system changes; file access confined; only own processes killed. |
| R12 | Verified against a generic local fixture site that reproduces legacy patterns (framesets, old document modes, `showModalDialog` with `dialogArguments`/`returnValue`, ActiveX XMLHTTP, form posts, alerts, popups, load-time script errors). |

## Architecture decisions

| Decision | Choice | Reason |
|---|---|---|
| Engine access | Selenium IEDriverServer, Edge with `attach_to_edge_chrome` | Official, maintained route into IE mode. |
| Second access path | Win32 `WM_HTML_GETOBJECT` → `IHTMLDocument2` over COM (pywin32) | Reaches a modal dialog or a user-opened tab when the driver cannot; independent of the driver's blocked state. |
| Tool timeouts | Watchdog thread per tool call, driver access serialised by a lock | A blocking click must not freeze the stdio loop; Win32-only tools do not need the driver lock. |
| Network capture | Local recording HTTP proxy on `127.0.0.1`, set only for the browser process (`use_per_process_proxy`) | Captures navigations, form posts, frames, dialogs and XHR alike, with bodies, without touching the system proxy. |
| Early instrumentation | Proxy injects one inline ES3 `<script>` at the top of `<head>` of HTML responses (opt-in flag, on by default in the harness); re-injected after each tool call when missing | IE has no "document start" hook; injection through the proxy is the only way to see load-time errors and console output. |
| Screenshots of dialogs | Win32 `PrintWindow` of the window handle, PNG encoded with stdlib `zlib` | Driver screenshots show only the document, not OS-level dialog windows. |
| Package layout | Split upstream's single file into the `ie_mcp/` package | Each concern (protocol, session, backends, capture, Win32, tools) testable on its own. |
| Dependencies | `selenium` required; `pywin32` optional extra | Server still starts without pywin32; Win32/COM tools report what is missing. |

## Open questions answered by the spike (plan Task 2)

1. Does a WebDriver click that opens `showModalDialog` block the driver in Edge IE mode?
2. Do modal dialogs appear in `driver.window_handles`, and can the driver switch to them and run
   script while the opener is blocked?
3. What Win32 window classes and process host the dialog, and does `WM_HTML_GETOBJECT` return
   its document?
4. Does `use_per_process_proxy` route Edge IE-mode traffic through a local proxy?
5. Does `console` exist in IE-mode documents when the F12 tools are closed, per document mode?

Each answer is recorded in `docs/spikes/2026-11-17-ie-mode-spike.md` with the evidence, and the
plan's later tasks name which answer selects which variant.
