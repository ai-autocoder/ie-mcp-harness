# IE-mode harness: design

Date: 2026-11-17. Status: approved direction, implementation not started. Revised the same day
after the first design review.

## Problem

AI coding agents can inspect and drive modern pages through Chromium tooling (DevTools
protocol, browser extensions). None of that reaches a tab running in **Edge IE mode**: the
Trident engine is invisible to Chromium extensions and to the DevTools protocol. Teams that
still maintain, or are migrating away from, IE-only applications therefore cannot let an agent
look at the legacy UI, compare it with a replacement, or reproduce its behaviour.

Two routes were evaluated: polyfilling IE behaviour into Chromium, and driving the real IE
engine through the official IEDriverServer. The IEDriver route was chosen because findings come
from the real engine (no false differences from an imitation), and because the engine is frozen,
so a working harness stays working. Microsoft supports IE mode through at least 2029.

## Goal

An MCP server that gives an agent **full access** to IE-mode pages: read, interact, observe
network and console, and see the rendering, across windows, frames and modal dialogs, **without
changing how the page behaves**.

## Non-goals

- Anything specific to one application (see CLAUDE.md, golden rule).
- Headless operation (IE mode cannot run headless).
- Decrypting HTTPS traffic (would require installing a root certificate). HTTPS requests are
  recorded as tunnels (host, port, timing) only; page-level XHR recording partly covers HTTPS
  (R7).
- Recording or replaying test scripts; the agent drives the tools interactively.
- Supporting non-Windows platforms or Python below 3.10.

## Requirements

| ID | Requirement |
|---|---|
| R1 | Open any URL in IE mode and keep one long-lived session per MCP process (upstream behaviour, kept). |
| R2 | Read HTML (live DOM), visible text and element values/attributes of the page, any frame (nested paths), any popup window and any `showModalDialog` window. |
| R3 | Click, double/right click, type, select, press keys, hover and scroll in all of the above. Where an input can only be synthesised (COM targets, async click), the tool description says so. |
| R4 | A tool call never blocks the server indefinitely. When a modal dialog is open, the agent is told so in every tool result and can act inside it. A hung driver can always be force-closed. |
| R5 | List all windows of the session (main, popups, modal dialogs, script alerts, other system dialogs) with kind, title, URL; switch between them; close them. System dialogs (file, print, security, credential prompts) are reported but never clicked by the harness. |
| R6 | Record every HTTP request/response of the IE-mode browser: method, URL, headers, request body, status, response headers, response body (size-capped), timing. Filter, list and fetch single entries. |
| R7 | Capture script errors (`window.onerror`), console output where the page has a console, alert/confirm/prompt texts, `showModalDialog`/`window.open` calls (URL, features, arguments, return value), and page XHR on HTTPS pages or when the proxy is off, as early in page load as possible. |
| R8 | Screenshots of the page, of an element, and of the whole browser window including open modal dialogs, correct on scaled (high-DPI) displays. |
| R9 | Run arbitrary JavaScript in any window/frame/dialog and return the result. |
| R10 | Optional: attach to an IE-mode tab the user opened by hand (keeps the user's login). |
| R11 | Safe by default: passwords, cookies, tokens and auth headers redacted in every captured artefact (URLs, bodies, headers, console text, dialog summaries); captures deleted when the session closes unless kept explicitly; no registry/system/security changes; file access confined; only own processes killed. |
| R12 | Verified against a generic local fixture site that reproduces legacy patterns (framesets, cross-origin frames, old document modes, `showModalDialog` with `dialogArguments`/`returnValue`, ActiveX XMLHTTP, form posts, alerts, popups, load-time script errors, windows-1252 and UTF-8 pages). |
| R13 | **Fidelity.** The harness must not change a page's document mode, charset detection, script behaviour or cached content. Every modification it makes (script injection, wrappers, proxy) is listed in `ie_status` and the README and can be switched off with an env var. |

## Architecture decisions

| Decision | Choice | Reason |
|---|---|---|
| Engine access | Selenium IEDriverServer, Edge with `attach_to_edge_chrome`, `unhandled_prompt_behavior="ignore"` | Official, maintained route into IE mode. The driver's default prompt behaviour (dismiss and notify) would silently cancel alerts the agent needs to see. |
| Second access path | Win32 `WM_HTML_GETOBJECT` → `IHTMLDocument2` over COM (pywin32), each call inside its own COM apartment, no COM objects cached across calls | Reaches a modal dialog or a user-opened tab when the driver cannot; independent of the driver's blocked state. |
| Tool timeouts | Watchdog thread per tool call; budget = max(`IE_TOOL_TIMEOUT`, the tool's own timeout + wait + margin); driver access serialised by a lock acquired with that budget; force-close by own PIDs | A blocking click must not freeze the stdio loop; long waits are not mistaken for blocks; a hung driver is always recoverable. |
| Network capture | Local recording HTTP proxy on `127.0.0.1`, applied only to the browser process (`use_per_process_proxy=True`, `noProxy` as a list); verified at session start, reported as off if unused | Captures navigations, form posts, frames, dialogs and XHR alike, with bodies, without touching the system proxy. |
| Proxy connection model | One upstream connection per client connection; optional upstream proxy chaining (`IE_CAPTURE_UPSTREAM_PROXY`) | Keeps connection-based Windows authentication (NTLM/Negotiate) working; works behind a corporate proxy. PAC files are not supported (documented). |
| Early instrumentation | Proxy injects one inline ES3 `<script>` into HTML responses (`IE_INSTRUMENT`, on by default), placed **after** the leading `<meta>`/`<title>`/`<base>` run of `<head>` (never before the doctype, BOM or an `X-UA-Compatible` meta), and marks modified responses `Cache-Control: no-store` without validators; re-injected after tool calls when missing | IE has no "document start" hook. The placement rule keeps the document mode unchanged; `no-store` keeps injected copies out of the per-user cache. |
| Instrumentation scope | Records errors, dialogs, `showModalDialog`/`open` calls; wraps `console` only if the page already has one (a complete no-op stub only with `IE_CONSOLE_STUB=1`); wraps native `XMLHttpRequest` only on HTTPS pages or when the proxy is off; calls host functions with the caller's exact argument count | Minimal wrapping keeps page behaviour unchanged. |
| Redaction | Field-name regex on form bodies, query strings, JSON keys and XML names; header-name regex; every value redacted once is masked everywhere afterwards (bodies, URLs, console, dialog summaries). Captures written to disk only with `IE_CAPTURE_JSONL=1`; session artefacts deleted on close unless `IE_KEEP_ARTIFACTS=1` | Secrets echo back in responses and logs; masking by value catches those. |
| Screenshots of dialogs | Win32 `PrintWindow(PW_RENDERFULLCONTENT)` of the window handle in a per-monitor DPI-aware process, PNG encoded with stdlib `zlib` | Driver screenshots show only the document, not OS-level dialog windows. |
| Package layout | Split upstream's single file into the `ie_mcp/` package; ship the fixture site as `ie_mcp_fixtures/` (used by tests and `--selftest`) | Each concern testable on its own; selftest works from a pip install. |
| Dependencies | Python 3.10+; `selenium` required; `pywin32` optional extra | Current selenium needs 3.10; the server still starts without pywin32 and Win32/COM tools say what is missing. |

## Fidelity: what the harness changes, and how to switch it off

| Change | When | Switch |
|---|---|---|
| Inline script in HTML responses | Proxy on and `IE_INSTRUMENT=1` | `IE_INSTRUMENT=0` |
| `Cache-Control: no-store`, validators dropped | Only on responses the proxy modified | follows `IE_INSTRUMENT` |
| Wrappers around `onerror`, `alert`/`confirm`/`prompt`, `showModalDialog`, `open` (and `console`, native `XMLHttpRequest` as above) | Instrumentation active | `IE_INSTRUMENT=0` |
| Proxy in the request path (may change the IE security zone of intranet hosts that normally bypass a proxy, and with it Protected Mode, automatic logon and Compatibility View) | `IE_CAPTURE=1` | `IE_CAPTURE=0` |
| Async (script) clicks: not user-initiated, no mousedown/mouseup/focus, may be blocked by the pop-up blocker | `mode="async"` or `IE_CLICK_MODE=async` | `mode="driver"` |
| COM fill/click on dialogs and attached tabs: synthetic event sequence | `target="hwnd:…"`/`"tab:…"` | use the driver path when it works |

## Open questions answered by the spike (plan Task 2)

1. Does a WebDriver click that opens `showModalDialog` block the driver, in `native` and in `background` interaction mode?
2. Do modal dialogs appear in `driver.window_handles`; can the driver switch to them and run script; does `execute_script` on the opener block while a modal is open (both modes)?
3. What Win32 window classes and processes host the content and the dialog; does `WM_HTML_GETOBJECT` return the dialog's document; does `execScript` over COM work on IE7 and IE11-mode documents; does COM work from a worker thread with its own apartment?
4. Does `use_per_process_proxy` with `noProxy: ["<-loopback>"]` route the initial URL, later navigations, popups and modal dialogs through a local proxy, without changing the user's global Internet Settings?
5. Does `console` exist in IE-mode documents when the F12 tools are closed, per document mode?
6. Do the fixture pages render with the expected userAgent and `document.documentMode` (IE11 / 7 / 5), with and without a script injected by the placement rule?
7. Does the pop-up blocker block `window.open` triggered by an async (`setTimeout`) click?
8. With `unhandled_prompt_behavior="ignore"`, does an alert stay open across `execute_script` and screenshot calls?

Each answer is recorded in `docs/spikes/2026-11-17-ie-mode-spike.md` with the evidence, and the
plan names which answer selects which variant.
