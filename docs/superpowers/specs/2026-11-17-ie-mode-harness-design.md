# IE-mode harness: design

Date: 2026-11-17. Status: approved direction, implementation not started. Revised twice the
same day after design reviews.

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
  recorded as tunnels (host, port, timing) only. On HTTPS pages, native `XMLHttpRequest` calls
  are recorded by page instrumentation, but ActiveX `XMLHTTP` calls are not, and there is no
  early (load-time) instrumentation.
- Recording or replaying test scripts; the agent drives the tools interactively.
- Supporting non-Windows platforms or Python below 3.10.

## Requirements

| ID | Requirement |
|---|---|
| R1 | Open any URL in IE mode and keep one long-lived session per MCP process (upstream behaviour, kept). |
| R2 | Read HTML (live DOM), visible text and element values/attributes of the page, any frame (nested paths), any popup window and any `showModalDialog` window. |
| R3 | Click, double/right click, type, select, press keys, hover and scroll in all of the above. Where an input can only be synthesised (COM targets, async click), the tool description says so. |
| R4 | A tool call never blocks the server indefinitely. When a modal dialog is open, the agent is told so in every tool result and can act inside it. A hung driver can always be force-closed. |
| R5 | List all windows of the session (main, popups, modal dialogs, script alerts, other system dialogs) with kind, title, URL; switch between them; close them. System dialogs (file, print, security, credential prompts) are reported but never clicked by the harness. The main window is never closed through `ie_close_window`. |
| R6 | Record every HTTP request/response of the IE-mode browser that goes over the network: method, URL, headers, request body, status, response headers, response body (size-capped), timing. Filter, list and fetch single entries. Responses served from the browser cache are not seen; `IE_CAPTURE_NO_CACHE=1` turns revalidations into full fetches, but fresh cache hits stay invisible. |
| R7 | Capture script errors, console output where the page has a console, alert/confirm/prompt texts, `showModalDialog`/`window.open` calls (URL, features, arguments, return value), and native page XHR on HTTPS pages or when the proxy is off, as early in page load as possible. |
| R8 | Screenshots of the page, of an element, and of the whole browser window including open modal dialogs, correct on scaled (high-DPI) displays. |
| R9 | Run arbitrary JavaScript in any window/frame/dialog and return the result. |
| R10 | Optional: attach to an IE-mode tab the user opened by hand (keeps the user's login). |
| R11 | Safe by default: passwords, cookies, tokens and auth headers redacted in every captured artefact (URLs incl. Referer/Location, bodies, headers, console text, dialog summaries, the server log), without corrupting ordinary data; JSONL capture files only on request and deleted on close unless kept; screenshots saved by name are kept; no registry/system/security changes; file access confined; only own processes killed, after checking their identity. |
| R12 | Verified against a generic local fixture site that reproduces legacy patterns (framesets, cross-origin frames, old document modes, a page without a mode meta, `showModalDialog` with `dialogArguments`/`returnValue`, ActiveX XMLHTTP returning documents and fragments, form posts, alerts, popups, load-time script errors, windows-1252 and UTF-8 pages). |
| R13 | **Fidelity.** The harness must not change a page's document mode, charset detection, script behaviour or the content of non-document responses. Every modification it makes is listed in `ie_status` and the README and can be switched off with an env var. |

## Architecture decisions

| Decision | Choice | Reason |
|---|---|---|
| Engine access | Selenium IEDriverServer, Edge with `attach_to_edge_chrome`, `unhandled_prompt_behavior="ignore"`, a finite HTTP client timeout to the driver | Official, maintained route into IE mode. The driver's default prompt behaviour (dismiss and notify) would silently cancel alerts the agent needs to see. |
| Second access path | Win32 `WM_HTML_GETOBJECT` → `IHTMLDocument2` over COM (pywin32). The tool-call worker thread, and every hook sub-thread, enters its own COM apartment; no COM object outlives the thread that obtained it. Keys reach COM targets as the posted key-message sequence or `fireEvent` the spike selects; every COM call retries rejected calls for up to 1 s | Reaches a modal dialog or a user-opened tab when the driver cannot; independent of the driver's blocked state. |
| Tool timeouts | Watchdog thread per tool call; budget = max(`IE_TOOL_TIMEOUT`, the tool's own timeout + wait + margin); driver access serialised by a lock acquired with that budget; `handle()` always goes through the watchdog; force-close by own PIDs after an identity check, which also cancels running wait loops | A blocking click must not freeze the stdio loop; long waits are not mistaken for blocks; a hung driver is always recoverable. |
| Network capture | Local recording HTTP proxy on `127.0.0.1`, applied only to the browser process (`use_per_process_proxy=True`, `noProxy` a list, plus `IE_CAPTURE_BYPASS` hosts); verified by matching the first http(s) navigation; reported as unverified/off otherwise | Captures navigations, form posts, frames, dialogs and XHR alike, with bodies, without touching the system proxy. |
| Proxy connection model | Streams every response; one upstream connection per client connection; optional upstream proxy chaining (`IE_CAPTURE_UPSTREAM_PROXY`) with proxy-auth headers passed through | Keeps progressive rendering and long-lived responses working; keeps connection-based Windows authentication working; works behind a corporate proxy. PAC files are not supported (documented). |
| Early instrumentation | The proxy inserts a tiny loader `<script>`, whose absolute `src` is `/__iemcp__/i.js` on the document's own host, only into **documents** (a doctype or `<html`/`<head`/`<body`/`<frameset` as the first tag) requested as documents (per the spike's `Accept` check), after the leading `<meta>`/`<title>`/`<base>`/comment run at the insertion point, decided incrementally on the streamed bytes and never before the first tag after that run has fully arrived; never into fragments, attachments, UTF-16 bodies, HEAD/204/304 responses, or head-less framesets. The proxy answers `/__iemcp__/` itself (never forwarded, never recorded). Modified responses get `Cache-Control: no-store` without validators | IE has no "document start" hook. Inserting a few bytes at a safe point keeps the document mode and charset detection unchanged; excluding non-documents keeps `responseText` of XHR endpoints unchanged. |
| Instrumentation scope | Records errors, dialogs, `showModalDialog`/`open` calls; wraps `console` only if the page already has one (a complete no-op stub only with `IE_CONSOLE_STUB=1`); wraps native `XMLHttpRequest` only on HTTPS pages or when the proxy is off; calls host functions with the caller's exact argument count; re-chains `onerror` when a page replaces it | Minimal wrapping keeps page behaviour unchanged. |
| Post-call instrumentation | One `execute_script` from the top document walks the same-origin frames in JS (inject where missing, drain), holding the driver lock until it returns; skipped while a dialog is open or after async clicks | No per-frame round trips; never interleaves with the next tool's frame switching. |
| Redaction | Field and header names are split into word segments and matched against built-in lists, plus substring matches for password-like names (plus `IE_CAPTURE_REDACT_FIELDS`/`_HEADERS`); applied to form bodies, query strings (incl. Referer/Location), JSON keys, XML names, `name=value` pairs in console text and dialog summaries. Every non-trivial value redacted once is masked everywhere afterwards. JSONL only with `IE_CAPTURE_JSONL=1`, deleted on close unless `IE_KEEP_ARTIFACTS=1` | Secrets echo back in responses and logs; segment matching avoids corrupting fields like `lastName`, `compass` or `keyword`; short values are masked only as whole tokens. |
| Screenshots of dialogs | Win32 `PrintWindow(PW_RENDERFULLCONTENT)` of the window handle in a per-monitor DPI-aware process, PNG encoded with stdlib `zlib` | Driver screenshots show only the document, not OS-level dialog windows. |
| Process safety | Every PID the session may kill is recorded with its image name and creation time and re-checked before killing | PIDs are reused; a stale PID must never kill an unrelated process. |
| Package layout | Split upstream's single file into the `ie_mcp/` package; ship the fixture site as `ie_mcp_fixtures/` (used by tests and `--selftest`) | Each concern testable on its own; selftest works from a pip install. |
| Dependencies | Python 3.10+; `selenium` required; `pywin32` optional extra | Current selenium needs 3.10; the server still starts without pywin32 and Win32/COM tools say what is missing. |

## Fidelity: what the harness changes, and how to switch it off

| Change | When | Switch |
|---|---|---|
| Loader `<script>` (absolute `…/__iemcp__/i.js` on the document's host; it removes itself when it runs) in HTML documents, including XHR responses that are full documents fetched with `Accept: text/html` | Proxy on, `IE_INSTRUMENT=1` and spike D10 = yes | `IE_INSTRUMENT=0` |
| `Accept-Encoding: identity` on requests for documents the proxy may modify | Proxy on, `IE_INSTRUMENT=1` and spike D10 = yes | `IE_INSTRUMENT=0` |
| `Cache-Control: no-store`, validators dropped (Back re-fetches the page; Back to a POST result shows "Webpage has expired") | Only on responses the proxy modified | `IE_INSTRUMENT=0` |
| Conditional request headers stripped (pages re-fetched instead of served from cache) | `IE_CAPTURE_NO_CACHE=1` (off by default) | `IE_CAPTURE_NO_CACHE=0` |
| Wrappers around `onerror`, `alert`/`confirm`/`prompt`, `showModalDialog`, `open` (and `console`, native `XMLHttpRequest` as above) | Instrumentation active | `IE_INSTRUMENT=0` |
| Proxy in the request path. It may move intranet hosts that normally bypass a proxy into another IE security zone, which can change Protected Mode, automatic logon and Compatibility View (and with it the document mode) | `IE_CAPTURE=1` | `IE_CAPTURE=0`, or list those hosts in `IE_CAPTURE_BYPASS` |
| IEDriver native input without window focus (`native_events=True`, `require_window_focus=False`: window messages, no focus, no physical mouse; synthetic events did nothing on the development machine, spike D17) | `IE_INTERACTION_MODE=background` (the default) | `IE_INTERACTION_MODE=native` (takes focus; can mix with the user's keystrokes and mouse) |
| A complete no-op `console` created where the page has none | `IE_CONSOLE_STUB=1` (off by default) | `IE_CONSOLE_STUB=0` |
| Async (script) clicks: `e.click()` queued with `setTimeout`; not user-initiated, no mousedown/mouseup/focus, may be blocked by the pop-up blocker | `ie_click mode="async"` or `IE_CLICK_MODE=async` (default is `driver`, spike D1) | `mode="driver"` / `IE_CLICK_MODE=driver` (the default) |
| COM input on dialogs and attached tabs: synthetic script events (`ie_fill`: focus, value, keydown/keypress/keyup/change/blur; `ie_hover`: mouseover/mouseenter), a timer click (`setTimeout`, no mousedown/mouseup/focus, also for `double`/`right`, which fire `ondblclick`/`oncontextmenu`), and posted `WM_KEYDOWN`/`WM_KEYUP` messages (documentMode 7+; `fireEvent` below 7) | `target="hwnd:…"`/`"tab:…"` | omit `target` (the driver path) when it works |
| COM request wrapper: every COM read or action runs as script and leaves the window expandos `__iemcp_r` (`null`) and `__iemcp_e` (`''`) on the target window (top window for frame requests); their values are cleared after each read-back | `target="hwnd:…"`/`"tab:…"` | omit `target` |
| Instrumentation left in an attached tab after `ie_detach` | `ie_attach` used | reload the tab |

## Open questions answered by the spike (plan Task 2)

1. Does a WebDriver click that opens `showModalDialog` block the driver, in `native` and in `background` interaction mode?
2. Do modal dialogs and `window.open` popups appear in `driver.window_handles`; can the driver switch to them and run script; does `execute_script` on the opener block while a modal is open (both modes)?
3. What Win32 window classes and processes host the content and the dialog; does `WM_HTML_GETOBJECT` return the dialog's document; does `execScript` over COM work on IE7 and IE11-mode documents; does COM work from a worker thread in its own apartment; are COM calls rejected while a dialog or alert is open; which posted key-message sequence (or `fireEvent`) produces exactly one keypress, in IE7 and IE11 mode?
4. Does `use_per_process_proxy` with `noProxy: ["<-loopback>"]` route the initial URL, later navigations, popups and modal dialogs through a local proxy, without changing the user's global Internet Settings; what `Accept` headers do document requests and XMLHTTP requests send?
5. Does `console` exist in IE-mode documents when the F12 tools are closed, per document mode?
6. Do the fixture pages render with the expected userAgent and `document.documentMode`, with and without the proxy and the loader (including a head that arrives in pieces); does the proxy change the mode of a page without a mode meta, through either origin; does IE treat the `localhost` frame as cross-origin?
7. Does the pop-up blocker block `window.open` triggered by an async (`setTimeout`) click?
8. With `unhandled_prompt_behavior="ignore"`, does an alert stay open across `execute_script` and screenshot calls; what window class and button IDs does the alert have?

Each answer is recorded in `docs/spikes/2026-11-17-ie-mode-spike.md` with the evidence, and the
plan names which answer selects which variant.
