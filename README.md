# ie-mcp

[![CI](https://github.com/ai-autocoder/ie-mcp-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/ai-autocoder/ie-mcp-harness/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-0078D6.svg)](#requirements)
[![MCP](https://img.shields.io/badge/MCP-stdio-green.svg)](https://modelcontextprotocol.io/)
[![Status: stable](https://img.shields.io/badge/status-stable-brightgreen.svg)](CHANGELOG.md)
[![Version: 1.0.0](https://img.shields.io/badge/version-1.0.0-blue.svg)](CHANGELOG.md)

**Give an LLM the ability to drive a legacy IE-only web application.**

> **This is a fork.** `ie-mcp-harness` is a fork of
> [thomfilg/ie-mcp](https://github.com/thomfilg/ie-mcp) (MIT), the project that created the Edge
> IE-mode / IEDriver MCP server this one grew from; upstream's copyright stays in
> [LICENSE](LICENSE). The fork adds the *harness*: full access to an IE-mode page without
> changing how it behaves (modal dialogs, pop-ups and attached tabs over COM, network capture,
> console and dialog records, window screenshots). The package and the command are still called
> `ie-mcp`. See [CHANGELOG.md](CHANGELOG.md) for what changed.

> **Status: `1.0.0`, the first stable release.** From here on, versions follow
> [semantic versioning](https://semver.org/): a tool, argument or setting is removed or changes
> meaning only in a new major version. See [CHANGELOG.md](CHANGELOG.md). Issues and PRs
> welcome — see [Development](#development) and [Roadmap](#roadmap).

`ie-mcp` is a [Model Context Protocol](https://modelcontextprotocol.io/) server that
automates **Microsoft Edge in IE mode** (the Trident / legacy document-mode engine) via
the Selenium IEDriver, and exposes it over MCP stdio so clients like **Claude Code**,
**Claude Desktop**, or **Codex** can read, click, fill, and scrape any IE-only intranet
app that nothing else can touch anymore.

The server is **application-agnostic**: it knows nothing about any specific site. You point
it at a target through environment variables and the Edge IE-mode site list, and it gives
the model a Playwright-style toolbox (auto-waiting locators, frame navigation, grid
extraction, readiness probing) tuned for the quirks of the Trident engine.

---

## Installation

### Option A — one command, straight from GitHub (recommended)

No clone, no separate install. Requires [`uv`](https://docs.astral.sh/uv/) on PATH
(`pip install uv`, or `winget install astral-sh.uv`). `uvx` fetches the repo, installs
selenium and `pywin32` (the `win32` extra: modal dialogs and attached tabs need it) into an
isolated env, and runs the server on each launch:

```bash
claude mcp add ie-mcp -- uvx --from "ie-mcp[win32] @ git+https://github.com/ai-autocoder/ie-mcp-harness.git" ie-mcp
codex  mcp add ie-mcp -- uvx --from "ie-mcp[win32] @ git+https://github.com/ai-autocoder/ie-mcp-harness.git" ie-mcp
gemini mcp add ie-mcp    uvx --from "ie-mcp[win32] @ git+https://github.com/ai-autocoder/ie-mcp-harness.git" ie-mcp
```

> `claude mcp add` takes `<name> <command…>` — here the name is `ie-mcp` and the command
> is the `uvx …` part. A bare repo URL won't work (it'd be treated as a remote HTTP server).

### Option B — install once, then a short command

Install the `ie-mcp` command on PATH, then let it register itself with every MCP client:

```bash
# 1. install (pipx/uv keep it isolated)
pipx install "ie-mcp[win32] @ git+https://github.com/ai-autocoder/ie-mcp-harness.git"
#   or:  uv tool install "ie-mcp[win32] @ git+https://github.com/ai-autocoder/ie-mcp-harness.git"
#   or from a clone:  pipx install ".[win32]"

# 2. register with every MCP client found on PATH (Claude Code, Codex, Gemini CLI)
ie-mcp --install
```

`ie-mcp --install` runs `claude mcp add` / `codex mcp add` / `gemini mcp add` for you with
the launch path already solved. Remove it again with `ie-mcp --uninstall`.

### Register a client by hand

`ie-mcp --install` (Option B) does this for you. To wire one up manually, use the `ie-mcp`
command (after `pipx install`). If you skipped the install (Option A), replace `ie-mcp`
in the command column with `uvx --from "ie-mcp[win32] @ git+https://github.com/ai-autocoder/ie-mcp-harness.git" ie-mcp`.

| Client | Command |
|--------|---------|
| **Claude Code** | `claude mcp add ie-mcp -- ie-mcp` |
| **Codex CLI** | `codex mcp add ie-mcp -- ie-mcp` |
| **Gemini CLI** | `gemini mcp add ie-mcp ie-mcp` |

Or via a client's config file (all equivalent — `{ "command": "ie-mcp" }`):

- **Claude Desktop** — `claude_desktop_config.json` (Windows: `%APPDATA%\Claude\claude_desktop_config.json`)
- **Gemini CLI** — `~/.gemini/settings.json` (Windows: `%USERPROFILE%\.gemini\settings.json`)

```json
{
  "mcpServers": {
    "ie-mcp": { "command": "ie-mcp" }
  }
}
```

- **Codex CLI** — `~/.codex/config.toml` (Windows: `%USERPROFILE%\.codex\config.toml`)

```toml
[mcp_servers.ie-mcp]
command = "ie-mcp"
```

Any other stdio MCP client works too: set the server command to `ie-mcp`. To override a
default, add the env var to that client's `env` block — see [Configuration](#configuration).

> IEDriver attach can be slow on the first call. If a client reports the server as
> unresponsive on startup, give it a longer init timeout and confirm `ie-mcp --selftest`
> passes standalone first.

Download **IEDriverServer.exe** (the 32-bit `4.14.0` build is the reference) from the
[Selenium downloads](https://www.selenium.dev/downloads/) page if it isn't already in the
per-user selenium cache (`%USERPROFILE%\.cache\selenium\IEDriverServer\`), or set
`IE_DRIVER_PATH`.

### Verify the setup

Before (or after) wiring up a client, run the built-in self-test. It needs an unlocked
desktop: it opens a real, visible IE-mode window on the fixture site that ships with the
package (`ie_mcp_fixtures`, served from `127.0.0.1`), so it works from a plain `pip install`
and touches no site of yours.

```bash
ie-mcp --selftest
```

It prints one `PASS`, `FAIL` or `SKIP` line per check, then `RESULT: PASS` or `RESULT: FAIL`
(exit code 0 or 1):

| Check | What it proves |
|---|---|
| selenium import, IEDriverServer, Edge, pywin32 | The dependencies and paths exist (the driver path is the one `IE_DRIVER_PATH` or discovery gives). |
| fixture server | The bundled fixture site starts. |
| IE-mode session | The session renders in the Trident engine: a Trident `userAgent` and `documentMode` 7 on `form.htm`. |
| proxy verified | The browser's traffic goes through the recording proxy (`SKIP` with `IE_CAPTURE=0`). |
| loader kept documentMode | The loader the proxy inserts runs before the page's own scripts (the load-time error of the `errors.htm` fixture reaches `ie_console`) and leaves `documentMode` 7 on `form.htm` (`SKIP` unless capture is `proxy` and `IE_INSTRUMENT=1`). |
| instrumentation present | The page instrumentation is installed (`SKIP` with `IE_INSTRUMENT=0`). |
| modal dialog | A `showModalDialog` window is detected and closed (`SKIP` without pywin32). |
| no leftover processes | Every Edge and IEDriver process the run started is gone. |

If selenium, the driver or Edge is missing, the browser checks are skipped. A missing pywin32 fails
its own line and skips only the modal check. See [Troubleshooting](#troubleshooting).

---

## Why this exists

Modern browser-automation tools (Playwright, Puppeteer, Selenium-on-Chromium) cannot render
IE-only apps — the ones that depend on ActiveX, `document.all`, framesets, VBScript-era
behaviours, or an enforced legacy document mode. Edge's **IE mode** is the last supported way
to run them, but it is notoriously awkward to automate:

- IEDriver attach is flaky (Protected-Mode boundary crossings, "could not find IE window").
- The legacy engine rejects Selenium's modern JS atoms (`el.text`, `Select`, `get_attribute`).
- Old document modes have no `window.JSON`, so naive scripts throw `'JSON' is undefined`.
- Frame-based apps lose state if the session is recreated.

`ie-mcp` works around all of these so the model gets a stable, high-level interface.

---

## Features

- **One long-lived session** kept alive across tool calls, so frame-based apps keep state.
- **Auto-waiting locators** (`id` / `css` / `xpath` / `name` / `link_text` / `tag` / `class`).
- **Nested frame navigation** — address frames-inside-frames with a path like `"3/0"`.
- **Trident-safe primitives** — click, fill, select, hover, key-press, scroll, upload, dialogs,
  all implemented to survive the legacy JS engine.
- **Readiness probing** (`ie_wait_ready`) that waits for every same-origin frame's
  `readyState` to complete *and* the DOM to stop changing — handles slow AJAX/frameset grids.
- **Grid extraction** (`ie_grid`) that heuristically finds the real data table and returns
  header-keyed rows.
- **Screenshots** (full window or single element), JS execution, history navigation, resize.
- **Network capture**: every request the IE-mode browser makes (navigations, form posts, frames, pop-ups, dialogs, XHR) is recorded by a local proxy that only the browser uses; `ie_network` / `ie_request` show them (see [Network capture](#network-capture)).
- **One browser per MCP process**, with parallel agents isolated by session identity.
- **Atomic per-session leases** prevent two processes from claiming the same explicit identity.
- **Session-scoped logs and ownership records** under `%TEMP%\ie-mcp\sessions\<session-id>`;
  the log is redacted (passwords, tokens, cookies; see `IE_CAPTURE_REDACT`).
- **Safe orphan cleanup** kills only browsers proven to belong to a crashed session; live agents
  and untracked Edge processes are preserved.
- **Vendored selenium** — selenium can be loaded from a deps folder (`IE_PYDEPS`) instead of a pip install.
- **Modal dialogs, pop-ups and attached tabs**, read and driven over COM (works while the driver is blocked).
- **Console, script-error and dialog records** (`ie_console`) and **window screenshots** that include open dialogs.
- **Built-in `--selftest`** to diagnose your setup before wiring up a client.

---

## Requirements

| Component | Notes |
|-----------|-------|
| **Windows** | IE mode only exists on Windows; the process-tracking and policy code use `tasklist`, `taskkill` (never `/T`), PowerShell/CIM, `winreg` and Win32 process APIs (ctypes). |
| **Microsoft Edge** | With IE mode available (Edge ships it on Windows 10/11). |
| **IEDriverServer.exe** | The Selenium IE driver (e.g. `4.14.0`). |
| **Python 3.10+** | Current selenium requires it. |
| **selenium** | `>=4.14.0,<5` — installed with the package, via `requirements.txt`, or vendored (see below). |
| **pywin32** | Optional extra `.[win32]` (`pywin32>=306`): COM access to modal dialogs, pop-ups and attached tabs. The server starts and lists its tools without it; the tools that need it say so. |

---

## Configuration

All configuration is via environment variables — **all optional**:

| Variable | Default | Purpose |
|----------|---------|---------|
| `IE_EDGE_PATH` | `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe` | Path to `msedge.exe`. |
| `IE_DRIVER_PATH` | *(unset)* | Path to `IEDriverServer.exe`. Unset: the highest version under `%USERPROFILE%\.cache\selenium\IEDriverServer\win32\<version>\` (then `win64`); if none is found the first session start fails and tells you to set this. A path that does not exist is an error, never silently replaced. 32-bit `4.14.0` is the reference build. |
| `IE_SITE_LIST` | *(unset)* | Path to an Edge IE-mode site-list XML. If set, the server verifies the HKCU Edge policy points at it so listed sites render in IE mode. If unset, no policy is managed and Edge is assumed already configured. |
| `IE_DEFAULT_URL` | `about:blank` | URL `ie_open` uses when none is given. In IE mode an **http(s) URL is required** to start a session (`about:blank` renders in Chromium and IEDriver finds no IE window), so pass `url` to `ie_open` or set this. |
| `IE_ATTACH_RETRIES` | `3` | IEDriver attach attempts. |
| `IE_PAGE_TIMEOUT` | `30` | Per-page load timeout (seconds). |
| `IE_PYDEPS` | *(unset)* | Extra `sys.path` dir for vendored selenium. Nothing is added unless you set it (there is no implicit `../.pydeps`). |
| `IE_INTERACTION_MODE` | `background` | Input strategy: `background` (default) sends native input as window messages without taking window focus or the physical mouse; `native` requires window focus and is the compatibility fallback (it takes focus and can mix with your own keystrokes and mouse). Selected when the MCP process starts. |
| `IE_TOOL_TIMEOUT` | `45` | Seconds one tool call may take before the server answers `BLOCKED:` (see below). Tools that wait declare a longer budget of their own: `ie_open`/`ie_goto` allow `IE_ATTACH_RETRIES x 75 + wait + 15`, the wait tools `timeout + interval + 15`, tools that locate an element `timeout + wait + 15`, `ie_sleep` `seconds + 5`. |
| `IE_CLICK_MODE` | `driver` | Default of `ie_click`'s `mode`: `driver` (a WebDriver click) or `async` (a script click queued with `setTimeout`; see `ie_click`). |
| `IE_CLEAN_SESSION` | `0` | `1`/`true`/`yes` makes IEDriver clear the **whole** IE-mode cookie store and cache of the current user (not just this session's) when the session starts and ends. Off by default; turning it on logs you out of your own IE-mode sites. |
| `IE_UPLOAD_DIR` | *(unset)* | Directory `ie_upload` may read from. Unset: uploads are disabled. A path must resolve inside it (relative paths are taken relative to it). |
| `IE_CAPTURE_REDACT` | `1` | Redaction of secrets in the server log, in captured traffic and in console records. On unless set to `0`/`false`/`no`/`off` (a typo cannot turn it off). Field and header names are split into word segments (`lastName`, `compass`, `keyword` stay visible); password-like names match as substrings (`newpassword`, `USERPWD`). Every non-trivial value redacted once (4+ characters, not `true`/`on`/... or a short number) is masked as `<redacted>` wherever it appears again; values under 8 characters only as whole tokens. Each `Cookie`/`Set-Cookie` value is tracked, so a harmless cookie such as `theme=dark` also masks the word `dark` elsewhere. In free text, `name: value` lines are redacted only for secret header names (`Authorization`, `Cookie`, hyphenated ones like `X-Auth-Token`) and password-like or configured field names; `name=value` and JSON pairs use the full rules. |
| `IE_CAPTURE_REDACT_FIELDS` | *(unset)* | Extra field names to redact, comma-separated, added to the built-in list (matched case-insensitively, whole name). |
| `IE_CAPTURE_REDACT_HEADERS` | *(unset)* | Extra header names to redact, comma-separated, added to the built-in list. |
| `IE_CAPTURE_MAX_BODY` | `262144` | Bytes kept of each captured request or response body; longer bodies are redacted in full, then cut and marked truncated. |
| `IE_CAPTURE_JSONL` | `0` | `1`/`true`/`yes` also appends every captured exchange, after redaction, to a JSONL file. A value learned as secret only after a line was written is not masked in that earlier line (the in-memory store re-masks on read). |
| `IE_INSTRUMENT` | `1` | Page instrumentation: the server puts a small ES3 script into the pages it drives (after each driver tool call, and into dialogs and attached tabs it reads over COM) that wraps `window.onerror`, `alert`/`confirm`/`prompt`, `showModalDialog`, `window.open` (and the page's own `console`, and native `XMLHttpRequest` on https pages) to record errors, dialogs, calls and console output for `ie_console`. Every wrapper calls the original with the caller's exact arguments and returns its value. `0`/`false`/`no`/`off` turns it all off (nothing is injected, nothing recorded, TaskDialogs are no longer accepted by `ie_dialog`). The full list is in the design spec's Fidelity table. |
| `IE_CAPTURE` | `1` | Network capture: the IE-mode browser (only it; never the system or WinINet proxy settings) is pointed at a recording proxy the server starts on `127.0.0.1`. `ie_network`/`ie_request` read what it saw, and it inserts the instrumentation loader into HTML documents (with `IE_INSTRUMENT=1`). `0`/`false`/`no`/`off` turns the proxy off: no capture of document loads, posts or frames, no loader (only XHR the page instrumentation sees is recorded, as `source: page`). The proxy can move intranet hosts that normally bypass a proxy into another security zone; see [Network capture](#network-capture). |
| `IE_CAPTURE_UPSTREAM_PROXY` | *(unset)* | `host:port` (or `http://host:port`) of a proxy the capture proxy forwards to, for networks that need one. Unset: direct. A malformed value does not stop the server: the next session start fails with an error naming this variable. |
| `IE_CAPTURE_BYPASS` | *(unset)* | Comma-separated host patterns (`*.example.test`, `intranet`) kept off the capture proxy. Those hosts are not captured, get no loader, and their native XHR is not recorded. Use it for hosts that must keep their normal proxy bypass and so their security zone. |
| `IE_CAPTURE_NO_CACHE` | `0` | `1`/`true`/`yes` strips conditional request headers (`If-Modified-Since`, `If-None-Match`) so cache revalidations become full fetches the proxy can see. Fresh cache hits stay invisible either way. |
| `IE_KEEP_ARTIFACTS` | `0` | `1`/`true`/`yes` keeps the JSONL capture file (`IE_CAPTURE_JSONL=1`, `<session dir>\capture.jsonl`) when the server exits; by default it is deleted. Screenshots saved by name are always kept. |
| `IE_CONSOLE_STUB` | `0` | `1`/`true`/`yes` creates a complete no-op `console` where a page has none (IE7 and quirks documents), so its `console.*` calls are recorded. Off by default: nothing is created that the page does not have. |
| `IE_LOG_FILE` | `<session-dir>\ie-mcp.log` | Optional override for this session's log file. |
| `IE_SESSION_ID` | `pid-<MCP process PID>` | Stable agent/session name. Different IDs can run concurrently; the same ID is exclusive. |
| `IE_SESSION_ROOT` | `%TEMP%\ie-mcp\sessions` | Root for session directories containing logs, leases, and ownership records. |
| `IE_LOCK_FILE` | `<session-dir>\session.lock` | Optional override for this session's atomic lease file. |
| `IE_NO_LOCK` | *(unset)* | Development escape hatch that disables the per-session lease. Do not use for normal agents. |


---

## Tools

Arguments that recur: `target` (act on a dialog or tab over COM, see below), `frame` (a
frame path), `scope` (`ie_screenshot`), `mode` (`ie_click`) and `force` (`ie_close`).

| Tool | Description |
|------|-------------|
| `ie_open` | Start (or reuse) an Edge IE-mode session and navigate to a URL. |
| `ie_goto` | Navigate the current session to a new URL. |
| `ie_status` | Session state as JSON (active/alive, title, url, owned PIDs, and the page modifications: `instrumented`, `console_stub`, `xhr_wrapped`; the network capture: `capture` = `proxy` / `page-only` / `off` / `unverified`, `capture_note`, `capture_internal_errors` and `capture_config_error` when there are any; and `modifications`, every change the harness currently makes to pages, requests and input, each with its off switch). Never waits: while another call holds the browser it reports `"driver": "busy"` and leaves out title/url. |
| `ie_browsers` | List all Edge IE-mode windows + IEDriver processes, flagging the one this session owns. |
| `ie_kill_orphans` | Terminate processes recorded for crashed sessions only (Edge profile and recorded image name + creation time must still match); never kills live-agent or unknown browsers. |
| `ie_frames` | List the frames/iframes of the current page (index, name, id, src). |
| `ie_text` | Get visible text of the page or a specific frame. |
| `ie_html` | Get HTML source of the page or a frame. |
| `ie_click` | Click an element (auto-waits); `click` / `double` / `right`. `mode` is `driver` (default; set by `IE_CLICK_MODE`) or `async`: the click is queued on the page's own timer, so the call does not wait for the click's handlers (only the usual `wait` pause follows) and cannot block even if the click opens a dialog that blocks a driver click. An async click is **script-initiated**: no mousedown/mouseup/focus, and the pop-up blocker may block a `window.open` it triggers. `double`/`right` always use the driver. |
| `ie_fill` | Type text into an input/textarea (auto-waits). |
| `ie_upload` | Set a `<input type=file>` by sending a local path; only files inside `IE_UPLOAD_DIR` (disabled when unset). |
| `ie_dialog` | Handle a script dialog (`accept` / `dismiss` / `text` / `sendkeys`). Without `target` it uses the driver's alert, then a script alert/confirm window of the session. With `target="hwnd:<n>"` it needs no driver: it presses that window's button, but only after proving it is the session's own alert or confirm (see below). |
| `ie_windows` | List the session's windows as JSON (`hwnd`, `target`, `kind`: main / popup / html_dialog / script_dialog / system_dialog, title, url, owner, `is_prompt`). When no modal is open and the browser is free it adds each page window's `driver_handle` and `current`. Works while a dialog is open and never waits for a busy browser. |
| `ie_tabs` | List the IE-mode tabs the user opened in Edge (`hwnd`, `target`, `top_hwnd`, `pid`, title, url, `attached`). Only the visible tab of each window is listed. Windows of other live ie-mcp sessions are never listed. |
| `ie_attach` | Attach to one of those tabs by `hwnd` (see [Attaching to a tab you opened](#attaching-to-a-tab-you-opened)). |
| `ie_detach` | Go back to the driver session. The tab keeps the instrumentation until it is reloaded. |
| `ie_switch_window` | Make a driver window (pop-up, `showModalDialog` window) the one the other tools act on: `handle`, `title_contains` or `main=true`. Returns the new title and URL. |
| `ie_close_window` | Close a pop-up or modal dialog by `hwnd:<n>` (`window.close()` over COM, else `WM_CLOSE`) or by driver handle. Refuses the main window (`ie_close`), an attached tab (`ie_detach`) and system dialogs; while the main window is not known (its adoption failed) it closes no page window, only modal dialogs. |
| `ie_console` | Records the page instrumentation made, as JSON `{records, next_since_id}`: `error` (script errors), `dialog-open` / `dialog` (an `alert`/`confirm`/`prompt` when it opens and when it closed, with the result), `modal` (`showModalDialog` URL, features, `dialogArguments` and return value), `open` (`window.open`), `console` and `xhr` (native XHR on https pages). Each has `id`, `kind`, `level`, `time`, `text` and `where` (the window target and frame path). Arguments `since_id`, `kinds`, `limit`. Text is redacted. |
| `ie_console_clear` | Forget every record `ie_console` holds. |
| `ie_network` | List the HTTP requests the browser made (captured by the local proxy; plus native XHR the page instrumentation saw, `source: page`): one line per request, `id source method status duration_ms size url`, oldest first. Filters `url_contains`, `method`, `status`; page with `since_id` and `limit` (default 50). Needs no browser call, so it works while the page is busy. |
| `ie_request` | One captured request in full: method, URL, status, timing, headers and bodies (charset from `Content-Type`, then the charset of the document that made the request, then cp1252; form bodies are percent-decoded the same way; gzip/deflate decoded from a copy; binary bodies as `<binary N bytes>`; `body_limit`, default 20000 characters). Secrets are redacted. An HTTPS entry is only a tunnel. |
| `ie_network_clear` | Forget every captured request (ids keep counting). |
| `ie_js` | Execute JavaScript in the page or a frame and return the result. |
| `ie_screenshot` | PNG screenshots. `scope`: `page` (default; the driver's view of the document), `element` (`by`/`value`; the default when `value` is given) or `window`: the session's windows as rendered on screen, captured with Win32 `PrintWindow` in a per-monitor DPI-aware process, so it includes open `showModalDialog` windows and script `alert`/`confirm`/`prompt` boxes (main window first, then each dialog bottom-most to top-most, one image each, plus a text list). `scope=window` needs no driver, so it works while a dialog blocks the page. `target=hwnd:<n>` (from `ie_windows`) captures that one session window. Only this session's windows are ever captured; system dialogs are listed, not captured; a minimized window is reported, not restored. `save_path` must be a bare file name; it is saved in `<session dir>\screenshots\` (several images: `name`, `name-2`, ...) and kept after `ie_close`. |
| `ie_wait_text` | Poll until a substring appears (or disappears) in the page text. |
| `ie_wait_ready` | Wait until the page (and same-origin frames) finish loading and settle. |
| `ie_select` | Select an `<option>` by label / value / index (Trident-safe). |
| `ie_press_key` | Press a key (Enter/Tab/Escape/arrows/… or a literal char). |
| `ie_back` / `ie_forward` | Navigate browser history. |
| `ie_hover` | Hover over an element (menus/tooltips). |
| `ie_resize` | Resize the browser window. |
| `ie_get` | Read an element's text, `value`, or a named attribute (Trident-safe). |
| `ie_wait_element` | Wait for an element to appear — or, with `gone=true`, disappear. |
| `ie_scroll` | Scroll an element into view, to top/bottom, or by pixels. |
| `ie_grid` | Extract a tabular grid as structured (header-keyed) rows. |
| `ie_sleep` | Sleep N seconds (last resort; prefer `ie_wait_ready` / `ie_wait_text`). |
| `ie_close` | Quit the browser; the session lease stays with this server. `force=true` never talks to the driver: it cancels running waits and kills this session's own Edge and IEDriver after an identity check (use it when the driver hangs). |

### Acting on a dialog or tab over COM (`target`)

`ie_html`, `ie_text`, `ie_js`, `ie_click`, `ie_fill`, `ie_select`, `ie_get`, `ie_frames`,
`ie_wait_element`, `ie_wait_text`, `ie_grid`, `ie_scroll`, `ie_press_key` and `ie_hover` take an
optional `target`. Omitted, the tool uses the session's IE driver. `hwnd:<n>` acts on window
`<n>` (for example a `showModalDialog` window named in a `NOTICE:`) directly over COM, and
`tab:<n>` on an attached tab; these calls do not need the driver, so they work while a modal
dialog blocks it. A target must be a window of this session: `hwnd:<n>` a window `ie_windows`
lists (or one inside it, or the attached tab's window), `tab:<n>` the attached tab, a page
window of the session, or a tab `ie_tabs` lists. Anything else, including a window of another
ie-mcp session or a stale hwnd, is refused (`not a window of this session`) before any COM
call. Using an unattached tab of yours this way leaves the instrumentation in it, like
`ie_attach` (the Fidelity row below then shows). Over COM:

- every DOM access runs as script in the page; the window keeps two expandos,
  `__iemcp_r` and `__iemcp_e`, whose values are cleared after each call;
- input is **synthetic**: script events for fill and hover, a click queued on the page's timer
  (no mousedown/mouseup/focus), posted key messages for keys;
- locators are `id`, `name`, `tag`, `class`, `link_text` and, from documentMode 8, `css`;
  `xpath` is rejected;
- `ie_html` returns the document's `outerHTML` (no doctype).

A `tab:` target never falls back to another tab: when the attached tab is gone the call fails
and asks you to attach again.

### Attaching to a tab you opened

To work in a page you already opened (and logged in to) in Edge IE mode, call `ie_tabs`, then
`ie_attach` with the tab's `hwnd`. From then on the tools above act on that tab over COM without
a `target`, `ie_console` collects its records after every call, `ie_windows` and `ie_dialog`
see the alerts, confirms and modal dialogs the tab opens, and `ie_screenshot scope=window`
captures its window. `ie_detach` goes back to the driver session. Limits:

- No driver-only tools while attached (navigation, history, `scope=page` screenshots, upload).
  A tool that needs the driver and has no explicit `target` is refused with "call ie_detach
  first" and never starts a browser. `ie_close` also needs `ie_detach` first (or `force=true`).
- No proxy capture: the tab's traffic does not go through the recording proxy, so `ie_network`
  sees only what the page instrumentation records.
- A page's `prompt` cannot be answered; only `alert` and `confirm` windows pass the `ie_dialog`
  checks.
- The instrumentation stays in the tab after `ie_detach` until the tab is reloaded (`ie_status`
  lists this under `modifications`).
- An attached tab's alerts are owned by a helper window of the tab's IE process, not by the tab's
  window. The helper is matched by process and by caption (the page title), and only when
  exactly one visible window carries that title. With two same-titled windows the tab's dialogs
  are not shown; give the page a distinct title or close the other window.
- Only the visible tab of each Edge window is listed; activate the tab first. While another
  ie-mcp session is starting its browser, `ie_attach` refuses and asks you to retry.

### Dialogs, pop-ups and system dialogs

Windows a page opens are of four kinds. A **pop-up** (`window.open`) and a **modal HTML dialog**
(`showModalDialog`) are pages: list them with `ie_windows`, then read and act in them either
with `ie_switch_window` (the driver) or with `target="hwnd:<n>"` (COM, no driver needed). Both
ways work; the `hwnd` way also works while the driver is blocked. `ie_close_window` closes
them. A **script dialog** (`alert`, `confirm`, `prompt`) is answered with `ie_dialog`.

- `ie_dialog` without `target` asks the driver's alert first. While a modal HTML dialog is
  open it skips the driver (that call can block) and answers a script dialog window instead.
  Before it accepts the driver alert (`accept`, or `sendkeys`, which also accepts), the open
  script dialogs pass the same checks as the window path: if one is a TaskDialog, the accept
  needs the page's own record (see "Console, errors and dialog records" below), because IE's
  "leave this page?" prompt has that shape; with several dialogs open and one a TaskDialog,
  pick one with `target`. `dismiss` and `text` are not restricted. Seen live: IEDriver exposes
  IE's "leave this page?" prompt (a page's `onbeforeunload`) as an alert, so a plain
  `ie_dialog` would have left the page; it is refused, `action=text` reads the page's message
  and `action=dismiss` stays on the page. That prompt ignores the Cancel message, so
  `ie_close_window` reports `closed: false` for it.
- `ie_dialog target="hwnd:<n>"` sends `WM_COMMAND` to the dialog's own button, but only after
  checking that the window belongs to the session, that its immediate owner is the browser's
  dialog helper or a `showModalDialog` window (a system dialog is owned by the browser window),
  and that its direct controls are exactly those of a script alert or confirm: the classic
  form (icon, message, one OK button for an alert, OK and Cancel for a confirm) or the
  TaskDialog form IE uses for a later dialog of the same page. Anything else is refused and
  nothing is sent.
- A **system dialog** (file open/save, print, a security or credential prompt) is never
  answered or closed: the tools report `system dialog: ask the user`. IE's "leave this page?"
  prompt is never accepted (see above).
- The message of a dialog shown as a TaskDialog is not readable through its window (an alert
  and a confirm look the same there); `ie_dialog action=text` asks the driver alert when exactly
  one script dialog is open, otherwise it says the text is not readable.
- **Limitation:** a `prompt` can only be answered through the driver alert (it has a text box).
  A prompt raised inside a `showModalDialog` window therefore cannot be answered: `ie_dialog`
  says `prompt not reachable: ask the user`. `ie_close_window` can still cancel it
  (the prompt then returns `null`).

### Console, errors and dialog records

`ie_console` shows what the page instrumentation (`IE_INSTRUMENT`, on by default) recorded.
Records are collected after each tool call, from the document the driver is on and its
same-origin frames (one `execute_script`), and over COM from modal HTML dialogs, from the
page windows behind an open script dialog, from a call's explicit `hwnd:`/`tab:` target and from an
attached tab. Know its limits:

- **Load-time errors of the first page are not captured** (spike D11): the session's initial URL
  bypasses the proxy, so nothing is injected before its scripts run. Anything that happens after
  the first tool call, on that page or any later one, is.
- **Records of a dialog that closes between two calls are lost**: nothing reads a page while no
  call is running, and a closed dialog's document is gone.
- `console` is wrapped only where the page already has one (documentMode 11 does, IE7 does not);
  `IE_CONSOLE_STUB=1` creates a no-op console where there is none. Native `XMLHttpRequest` is
  wrapped on https pages (a proxy cannot read inside them), and on every page while capture is off
  or found blind, where the document has an `XMLHttpRequest.prototype` (not documentMode 7); ActiveX
  `XMLHTTP` never is.
- A pop-up's records go to its opener's queue when it is same-origin (marked `"popup": true`).
- A TaskDialog (IE's form for a later dialog of a page; its message cannot be read from the
  window) is accepted by `ie_dialog` only when all of these hold: the owner document, read over
  COM at that moment, reports an `alert`/`confirm`/`prompt` of the page's own wrapper in
  progress; and a `dialog-open` record exists (at most 30 s old) whose page-assigned id no
  `dialog` record has closed. The record is used up: one record, one accept. Otherwise, or if
  the page cannot be read, or with `IE_INSTRUMENT=0`, accept is refused (`ask the user`): IE's
  "leave this page?" prompt has the same shape and nothing in the page's wrappers behind it.
  Dismiss is not restricted. Answers carry the matching record as `evidence`.

### Working with frames

Many legacy apps are framesets. Use `ie_frames` to enumerate them, then pass `frame` to most
tools. A nested path reaches frames-inside-frames — e.g. `"3/0"` is the first frame inside the
fourth frame.

---

## No-pip / vendored selenium

On locked-down or offline machines you can run without a global `pip install`. Drop the
`selenium` package (and its deps) into a folder and point `IE_PYDEPS` at it:

```bash
pip install --target ../.pydeps "selenium>=4.14.0,<5"
```

Only `IE_PYDEPS` adds a folder to the import path; the server no longer looks for `../.pydeps` on its
own. The MCP protocol itself has **zero** pip dependencies (newline-delimited JSON-RPC 2.0 is
implemented inline).

---

## How IE mode is reached

When `IE_SITE_LIST` is set, the server self-heals the HKCU Edge policy
(`SOFTWARE\Policies\Microsoft\Edge`) so the listed sites open in IE mode:

- `InternetExplorerIntegrationLevel = 1`
- `InternetExplorerIntegrationSiteList = file:///…/your-site-list.xml`
- `InternetExplorerIntegrationReloadInIEModeAllowed = 1`

These are written under **HKCU** (no admin needed). If the key isn't writable, the server
logs the exact values to set manually. When `IE_SITE_LIST` is unset, the server manages no
policy and assumes Edge is already configured for IE mode.

> Session creation navigates to the target URL **during** session start — the only reliable
> way to cross the Protected-Mode boundary into IE mode without the driver losing the browser.

---

### Network capture

With `IE_CAPTURE=1` (the default) the server starts a recording proxy on `127.0.0.1` before the
browser and points **only the browser process** at it (IEDriver's per-process proxy; your system
and Internet Options proxy settings are never read or written, except that the Windows
`ProxyOverride` list is read, read-only, for the notice below). `ie_network` lists what it saw and
`ie_request` shows one request with headers and bodies. Captured: navigations, form posts, frames,
pop-ups, `showModalDialog` windows and their XMLHTTP, and page XHR, all with bodies (redacted).

Limits to know:

- **The session's initial URL is not captured** (spike D11): the page `ie_open` starts the browser on
  loads before the proxy applies. Everything the browser loads afterwards is.
- **HTTPS shows only the tunnel** (host and timing), not the exchange.
- **Fresh browser-cache hits are invisible** (no request is made). `IE_CAPTURE_NO_CACHE=1` turns
  revalidations into full fetches, not fresh hits.
- **Hosts in `IE_CAPTURE_BYPASS` are not captured**, get no loader and their native XHR is not recorded.

Verification: `ie_status` reports `capture` as `unverified` until the first `ie_open`/`ie_goto` to an
http(s) URL after the session was created. That call waits up to 5 s for the proxy to show the
request: if it does, `capture` becomes `proxy`; if not the result carries `NOTICE: network capture is off: the browser did not use the proxy` and `capture` is `page-only` (the page instrumentation still records XHR).

The proxy can change how a page behaves. Hosts that normally bypass your proxy (your Windows
`ProxyOverride` list, or any dotless host when it lists `<local>`) are then handled as proxied
hosts, and IE may put them in another security zone: a different document mode, Protected Mode or
automatic logon. When that applies, `ie_open`/`ie_goto` add `NOTICE: <host> normally bypasses your
proxy ...`. If a page behaves differently from your own browser, list its host in
`IE_CAPTURE_BYPASS=<host>` (that host is then not captured) or turn the proxy off with
`IE_CAPTURE=0`. Everything the harness modifies, each with its off switch, is in `ie_status`
`modifications` and in the design spec's Fidelity table.

Documents get a tiny loader `<script>` (so instrumentation runs before the page's own scripts);
only with `IE_INSTRUMENT=1`, only into HTML documents requested as pages, never into fragments or
XHR answers. Modified responses are marked `Cache-Control: no-store`, and requests that accept
HTML are sent with `Accept-Encoding: identity`. `IE_INSTRUMENT=0` turns all of that off.

## Fidelity: what the harness changes

The harness exists to show how a legacy page really behaves, so everything it changes is
listed here, in `ie_status` (`modifications` names the ones active right now) and has an env
var that turns it off. It never changes the document mode, the charset detection or the
content of a response that is not an HTML document. (`D<n>` in the table is a decision of the
design spike, recorded in `docs/spikes/2026-11-17-ie-mode-spike.md`.)

| Change | When | Switch |
|---|---|---|
| Loader `<script>` (absolute `…/__iemcp__/i.js` on the document's host; it removes itself when it runs) in HTML documents, including XHR responses that are full documents fetched with `Accept: text/html`. Only `text/html` 200 responses, not encoded, not attachments, whose first tag is a doctype, `<html`, `<head` or `<body`; never fragments, plain text, UTF-16 bodies, HEAD/204/304, head-less framesets or `<?xml` documents. `Content-Length` is fixed or the body re-chunked | Proxy on, `IE_INSTRUMENT=1` and spike D10 = yes | `IE_INSTRUMENT=0` |
| `Accept-Encoding: identity` on every request whose `Accept` contains `text/html` (documents, and XHRs that ask for HTML), whether or not the response is then modified | Proxy on, `IE_INSTRUMENT=1` and spike D10 = yes | `IE_INSTRUMENT=0` |
| `Cache-Control: no-store`, validators dropped (Back re-fetches the page; Back to a POST result shows "Webpage has expired") | Only on responses the proxy modified | `IE_INSTRUMENT=0` |
| Conditional request headers stripped (pages re-fetched instead of served from cache) | `IE_CAPTURE_NO_CACHE=1` (off by default) | `IE_CAPTURE_NO_CACHE=0` |
| `window.onerror` replaced by a wrapper (marker `__iemcp = 1`) that records the error, then calls the page's own handler with the same arguments and returns its value; put back in front when the page replaces it. Same-error duplicates within 100 ms are recorded once | Every instrumented document (after each driver tool call; COM documents via the dialog drain) | `IE_INSTRUMENT=0` |
| `alert`, `confirm`, `prompt` replaced by wrappers that record (`dialog-open` before, `dialog` after) and call the original with the caller's exact arguments, returning its result. `typeof alert` becomes `function` where it was `object`. The message and result are converted with `String()` (a page object's `toString` may run; guarded). An in-flight counter (`__iemcp.open`, also in a same-origin top window) is kept | Instrumentation active | `IE_INSTRUMENT=0` |
| `showModalDialog` replaced by a wrapper that records URL, features, a one-level summary of `dialogArguments` and the return value; the arguments and `dialogArguments` object are passed through unchanged | Instrumentation active | `IE_INSTRUMENT=0` |
| `window.open` replaced by a wrapper that records URL, name and features and returns the original's window object | Instrumentation active | `IE_INSTRUMENT=0` |
| The page's own `console` methods replaced by recording wrappers that call the original (kept as an extra property `__iemcp_<method>` of the console object, because a detached call can fail in old IE). Arguments are converted with `String()` (a page object's `toString` may run; guarded) and at most 6 are forwarded. Nothing is created or added | Only where `window.console` exists (documentMode 11 yes, 7 no, spike D5) | `IE_INSTRUMENT=0` |
| `XMLHttpRequest.prototype.open`/`send` replaced by recording wrappers (originals kept as `__iemcp_open`/`__iemcp_send` of the prototype; an expando `__iemcp_x` on each request object; a `readystatechange` listener per async request; the page's own handler is untouched). Method, URL and a string body are converted with `String()` (guarded). ActiveX `XMLHTTP` is never touched; not done where the document has no `XMLHttpRequest.prototype` (documentMode 7) | `https:` pages, or the proxy is off (`IE_CAPTURE=0`) or saw no traffic (`capture_active` false) | `IE_INSTRUMENT=0` |
| Window expandos `__iemcp` (record queue and helpers), `__iemcp_stub`, `__iemcp_wrap_xhr` | Instrumentation active | `IE_INSTRUMENT=0` |
| Hook script element: where `window.execScript` is absent (documentMode 11) a `<script>` element with the instrumentation as its `text` is appended to `<head>` (else the root element) and removed again right after it ran, so `ie_html` shows the original DOM; where `execScript` exists it is used instead and no element is created | After each successful driver tool call, and in dialogs and attached tabs over COM | `IE_INSTRUMENT=0` |
| Proxy in the request path. It may move intranet hosts that normally bypass a proxy into another IE security zone, which can change Protected Mode, automatic logon and Compatibility View (and with it the document mode). A host listed in `IE_CAPTURE_BYPASS` never reaches the proxy (no capture, no loader, no native-XHR wrapper) | `IE_CAPTURE=1` and the proxy started. `ie_status` `modifications` leaves this row, the loader, `Accept-Encoding` and `no-store` rows out once verification found the browser blind (`capture_active` false: nothing is modified then) | `IE_CAPTURE=0` for every host, or `IE_CAPTURE_BYPASS` for the hosts that must keep their normal proxy bypass (and their zone) |
| IEDriver native input without window focus (`native_events=True`, `require_window_focus=False`: window messages, no focus, no physical mouse; synthetic events did nothing on the development machine, spike D17) | `IE_INTERACTION_MODE=background` (the default) | `IE_INTERACTION_MODE=native` (takes focus; can mix with the user's keystrokes and mouse) |
| A complete no-op `console` created where the page has none | `IE_CONSOLE_STUB=1` (off by default) | `IE_CONSOLE_STUB=0` |
| Async (script) clicks: `e.click()` queued with `setTimeout`; not user-initiated, no mousedown/mouseup/focus, may be blocked by the pop-up blocker | `ie_click mode="async"` or `IE_CLICK_MODE=async` (default is `driver`, spike D1) | `mode="driver"` / `IE_CLICK_MODE=driver` (the default) |
| COM input on dialogs and attached tabs: synthetic script events (`ie_fill`: focus, value, keydown/keypress/keyup/change/blur; `ie_hover`: mouseover/mouseenter), a timer click (`setTimeout`, no mousedown/mouseup/focus, also for `double`/`right`, which fire `ondblclick`/`oncontextmenu`), and posted `WM_KEYDOWN`/`WM_KEYUP` messages (documentMode 7+; `fireEvent` below 7) | `target="hwnd:…"`/`"tab:…"` | omit `target` (the driver path) when it works |
| COM request wrapper: every COM read or action runs as script and leaves the window expandos `__iemcp_r` (`null`) and `__iemcp_e` (`''`) on the target window (top window for frame requests); their values are cleared after each read-back | `target="hwnd:…"`/`"tab:…"` | omit `target` |
| Instrumentation left in an attached tab after `ie_detach` | `ie_attach` used, or a `tab:<n>` target named an unattached tab of the user's | reload the tab |

## Limits

- **HTTPS is tunnel-only.** The proxy sees the host and timing of an HTTPS exchange, not its
  content, and inserts no loader into HTTPS pages: errors and console output during their load
  are not captured, only those after the first tool call on the page. Native `XMLHttpRequest` on
  HTTPS pages is recorded by the page instrumentation (`source: page`).
- **Cached responses are not seen.** A fresh browser-cache hit makes no request;
  `IE_CAPTURE_NO_CACHE=1` turns revalidations into full fetches, not fresh hits.
- **PAC files are unsupported**.
- **The proxy may change the IE security zone** of intranet hosts that normally bypass a proxy
  (document mode, Protected Mode, automatic logon). `IE_CAPTURE_BYPASS` or `IE_CAPTURE=0`.
- **The loopback proxy is reachable by other users** of the same machine, for example on a
  shared terminal server: it listens on `127.0.0.1` without authentication.
- **The session's initial URL is not captured** and its load-time errors are not recorded.
- **Dialog records can be lost on a fast close**: nothing reads a page while no call is running.
- **Attached tabs keep the instrumentation until they are reloaded**; they have no proxy
  capture and no driver tools (see [Attaching to a tab you opened](#attaching-to-a-tab-you-opened)).
- **A `prompt` is answered only through the driver**: not inside a modal dialog and not on an
  attached tab (`prompt not reachable: ask the user`); `ie_close_window` can cancel it.
- **A TaskDialog raised by a frame whose top document is cross-origin or not instrumented is
  never accepted by `ie_dialog`** (it fails closed; dismiss still works).
- Only the visible tab of each Edge window can be attached.

## Security

- **Redaction is on by default** (`IE_CAPTURE_REDACT`): secret field and header names and every
  value learned from them are masked in the server log, captured requests and responses, console
  records, dialog summaries and URLs. The rules are in the configuration table.
- **Captured traffic and console records stay in memory.** A JSONL copy is written only with
  `IE_CAPTURE_JSONL=1`, and is deleted when the server exits unless `IE_KEEP_ARTIFACTS=1`.
  Screenshots saved by name (`save_path`, a bare file name) go to the session's `screenshots`
  folder and are **kept**. Never commit captured data.
- **File access is confined.** `ie_upload` reads only inside `IE_UPLOAD_DIR` (disabled when
  unset); `save_path` cannot leave the session's screenshots folder.
- **Killing is identity-checked.** The server kills only processes it recorded for its own
  session (or for a dead session, matched by Edge profile), and only if the recorded image name
  and creation time still match, because Windows reuses PIDs. `taskkill` never runs with `/T`;
  a process with no recorded identity is never killed.
- **System dialogs are never clicked.** File open/save, print, security and credential prompts
  are reported (`system dialog: ask the user`) for you to act on. IE's "leave this page?"
  prompt is never accepted (`ask the user`); only "stay" (`ie_dialog action=dismiss`) is sent.
  Only a script `alert`/`confirm` the session can prove is its own is accepted.
- **What is never changed:** the registry (the one exception is the opt-in `IE_SITE_LIST` policy
  writer, under HKCU), Windows, Internet Options, Protected Mode, zones, the system or WinINet
  proxy, the firewall, Edge settings and certificates. The `ProxyOverride` list is only read.
- **No outbound network calls of its own.** The proxy forwards only the browser's traffic (to
  `IE_CAPTURE_UPSTREAM_PROXY` when set).
- `ie_tabs` and `ie_attach` never list or attach to windows owned by another live ie-mcp session.
- `IE_CLEAN_SESSION=1` clears your whole IE-mode cookie store and cache; it is off by default.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `IEDriverServer ... not found`, or the selftest line `FAIL  IEDriverServer` | Put the 32-bit `IEDriverServer.exe` (`4.14.0`) in `%USERPROFILE%\.cache\selenium\IEDriverServer\win32\4.14.0\`, or set `IE_DRIVER_PATH`. A path that does not exist is an error, never silently replaced. |
| `capture` is `off`, `page-only` or `unverified` in `ie_status` | `off`: `IE_CAPTURE=0`. `unverified`: no http(s) navigation since the session started; call `ie_goto`. `page-only`: the browser did not use the proxy; see `capture_note`, check `IE_CAPTURE_UPSTREAM_PROXY`, then `ie_close` and open again. Only XHR the page instrumentation sees is recorded meanwhile. |
| `BLOCKED: modal dialog open`, or a dialog the tools cannot reach | List it with `ie_windows` and use `target="hwnd:<n>"` (COM, needs no driver) or `ie_switch_window`. If a `NOTICE:` names it, follow that. A system dialog (file, print, security, credentials) must be handled by you. `ie_close force=true` ends a hung session. |
| `pywin32 is required to read IE documents over COM` | `pip install "ie-mcp[win32]"` (or `pip install pywin32`) in the environment the MCP client launches; modal dialogs by `hwnd:` and attached tabs need it. |
| An `alert`/`confirm` stays open, or `ie_dialog` says `ask the user` | `ie_dialog` answers only alerts and confirms of this session (and a `prompt` through the driver). A TaskDialog needs a matching `ie_console` record (`IE_INSTRUMENT=1`) and a same-origin, instrumented top document; anything else is left to you. |
| `could not start Edge IE mode after N tries` | Confirm IE mode renders the site manually in Edge; check `IE_SITE_LIST`; try bumping `IE_ATTACH_RETRIES`. If every attempt times out and IEDriver's log stops at "Using UI Automation to set window focus", the desktop is locked: unlock it. |
| `IE session '<id>' is already owned` | Two clients used the same `IE_SESSION_ID`. Give each agent a unique ID, or omit it to use the MCP process PID automatically. |
| `IE engine: NO (Chromium?)` in selftest | The site isn't matching the IE-mode site list; the page rendered in Chromium. Fix the site list / policy. |
| Leftover Edge windows after a crash | Use `ie_browsers` to inspect and `ie_kill_orphans` to clean up. |
| `'JSON' is undefined` from your own `ie_js` | The page is in IE7/quirks mode with no `window.JSON`. Return delimited strings instead of `JSON.stringify`. |

---

## Development

The server is the `ie_mcp` package: a tool registry plus one module per group of tools.
Install it editable so the `ie-mcp` command points at your working copy:

```bash
git clone https://github.com/ai-autocoder/ie-mcp-harness.git
cd ie-mcp-harness
pip install -e ".[win32]"   # or: pipx install --editable ".[win32]"

# diagnose deps/paths and open a real IE-mode session on the bundled fixture site
ie-mcp --selftest
```

Running straight from the clone without installing also works — `python -m ie_mcp
--selftest` (from the repository directory) / `--install` fall back to launching
`ie_mcp/__main__.py` by absolute path.

**Project layout**

- `ie_mcp/config.py` — `Settings`: every env var, read once (`config.SETTINGS`).
- `ie_mcp/session.py` — the IEDriver wrapper (`IeSession`), session leases, process
  tracking, identity-checked killing (`kill_verified`) and orphan cleanup.
- `ie_mcp/win32/processes.py` — `process_identity` (image name + creation time), the process
  snapshot and process tree (ctypes; trees are for listing, never for killing).
- `ie_mcp/win32/windows.py` — window enumeration and classification (main, popup, HTML dialog,
  script dialog, system dialog) and the modal probe behind the `NOTICE:`/`BLOCKED:` answers
  (ctypes only, no COM).
- `ie_mcp/win32/messages.py` — the only code that posts window messages (`WM_COMMAND`, `WM_CLOSE`)
  and reads a control's text (ctypes).
- `ie_mcp/win32/dialogs.py` — the check that a `#32770` window is the session's own script
  dialog before anything is sent to it (owner, owner class and exact controls).
- `ie_mcp/win32/htmldoc.py` — COM access to an IE document through `WM_HTML_GETOBJECT`
  (needs the optional `pywin32` extra; the server runs without it).
- `ie_mcp/protocol.py` — the stdio JSON-RPC loop and the `@tool` registry.
- `ie_mcp/tools/` — one `t_*` function per tool, grouped in `navigation.py`, `read.py`,
  `interact.py`, `windows.py`, `dialogs.py`, `console.py` (the instrumentation hooks and
  `ie_console`), `network.py` (`ie_network`, `ie_request`, `ie_network_clear`) and `diagnostics.py`.
- `ie_mcp/capture/console.py` — console, error and dialog records and their redacting store.
- `ie_mcp/capture/proxy.py`, `store.py`, `redact.py`, `verify.py` — the recording proxy, the captured-request store, redaction, and the helpers that check the browser really uses the proxy.
- `ie_mcp/js/` — the ES3 scripts injected into the page (`select.js`, `grid.js`,
  `ready_probe.js`, `instrument.js`, `hook.js`, `drain.js`).
- `ie_mcp/cli.py` — the `--install`/`--selftest` CLI and the server start-up.
- `ie_mcp/backends.py`, `ie_mcp/png.py`, `ie_mcp/win32/capture.py`, `ie_mcp/tools/attach.py` — the driver and COM backends, the PNG encoder, window capture, attached tabs.
- `ie_mcp_fixtures/` — the generic legacy fixture site (served from `127.0.0.1`), used by the tests and `--selftest`; shipped in the wheel.
- `pyproject.toml` — packaging + the `ie-mcp` console-script entry point (`ie_mcp.cli:main`).
- `requirements.txt` — runtime deps (just selenium; mirrors `pyproject.toml`).

**Adding a tool**

1. In the matching module under `ie_mcp/tools/`, write a `t_<name>(args)` function that
   returns `text_result(...)` (or `image_result(...)`).
2. Register it with the `@tool(name, description, schema)` decorator from
   `ie_mcp.protocol`; `ie_mcp/tools/__init__.py` imports every tool module.
3. Keep it **Trident-safe**: prefer direct `arguments[0].innerText` / `getAttribute` over
   Selenium's JS atoms, and never rely on `window.JSON` inside injected scripts (IE7/quirks
   document modes don't have it — return delimited strings and parse them in Python).
4. Re-run `--selftest`, then exercise the tool from an MCP client against a real IE-mode app.

**Debugging**

- Set `IE_LOG_FILE` to capture the `[ie-mcp]` stderr log to a file.
- Use `ie_browsers` / `ie_kill_orphans` to inspect sessions and clean up browsers left by
  crashed owners. Cleanup deliberately preserves unknown processes and every live agent.
- Set a descriptive `IE_SESSION_ID` when logs need to remain easy to correlate with an agent.
- Set `IE_NO_LOCK=1` only while debugging the lease implementation itself.

### Parallel agents

Every MCP process still owns exactly one long-lived Selenium session and therefore one browser.
Multiple MCP processes can run simultaneously because their leases, logs, owner records, and
IEDriver-created Edge profiles are session-scoped. With no configuration, the MCP process PID is
used as the session ID, which makes separate Codex/Claude agent processes independent automatically.

```toml
[mcp_servers.ie-mcp.env]
IE_SESSION_ID = "qa-agent-country"
```

### Choosing interaction mode (LLM guidance)

`background` is the default, and the right choice when the MCP runs on a person's active desktop.
It sends native input as window messages (IEDriver `native_events` on) without requiring window
focus, so the IE-mode Edge window is not forced to the foreground and your physical mouse is left
alone. (Synthetic, non-native events were tried and did nothing on the development machine.)
Prefer it for navigation, page inspection, ordinary clicks, and form entry:

```toml
[mcp_servers.ie-mcp.env]
IE_INTERACTION_MODE = "background"
```

Use `native` only as a compatibility fallback after a required interaction fails in background
mode—for example, a legacy control, hover menu, file upload, OS modal, or ActiveX component. Native
mode requires window focus, so it takes focus and can mix with the user's own keystrokes and mouse:

```toml
[mcp_servers.ie-mcp.env]
IE_INTERACTION_MODE = "native"
```

The mode is fixed for the lifetime of the MCP process, so restart the MCP after changing it. Neither
mode is headless: Edge still creates a visible IE-mode window. When multiple native-mode agents run
on the same Windows desktop, their focus-sensitive interactions can still conflict. The server also
returns this selection guidance in its MCP `initialize` response so clients can present it to LLMs.

### Busy and blocked browsers

The driver handles one call at a time, and a call can hang (for example while a dialog is open), so
no tool call can hang the server:

- A call that needs the browser while another one is using it waits up to its budget, then answers
  `BUSY: <tool> has held the browser for <n>s`; with a dialog open it answers `BLOCKED: modal dialog
  open`.
- A call that runs past its budget answers `BLOCKED: the browser did not answer within <n>s; the
  action may still complete`. The call itself cannot be stopped and goes on holding the browser until
  it returns (its late result is discarded). `ie_close force=true` works at any time: it does not
  need the browser, cancels running waits and kills only this session's own Edge and IEDriver.
- When a modal window is open after a call, the answer ends with a `NOTICE:` item naming it and
  how to reach it (`ie_switch_window` or `target="hwnd:<n>"`; `ie_windows` lists every window).
- `ie_sleep`, `ie_browsers`, `ie_kill_orphans`, `ie_status`, `ie_windows`, `ie_close force=true` and
  `ie_dialog`/`ie_close_window` with an `hwnd:` target never wait for the browser.

### Other things the server does to the driver

- IEDriverServer is started without the inherited `__COMPAT_LAYER` environment variable (the Claude
  desktop app sets one; with it Edge relaunches itself and IEDriver never finds the IE window).
  Your own environment is not changed.
- IEDriver's `unhandledPromptBehavior` is `ignore`, so a driver command no longer silently cancels
  an open `alert`/`confirm`.
- The HTTP client timeout to the driver is 300 seconds, so a dead driver cannot hang a tool call
  forever.

### Tests

Unit tests use mocked process tables, windows and COM documents and never open Edge:

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

Integration tests (`tests/integration/it_*.py`) open a real, visible IE-mode window against the
fixture site on `127.0.0.1`, so they need an unlocked desktop. Run them only through the runner,
which sets `IE_MCP_INTEGRATION=1` and **fails if any test was skipped**; run one at a time:

```bash
python scripts/run_integration.py                    # all modules
python scripts/run_integration.py it_read_interact   # one module
```

Each module closes its session and checks that no Edge or IEDriver process it started is left.
`it_e2e_mcp` starts `python -m ie_mcp` as a subprocess and drives a whole session over stdio
(frames, a form post, a modal dialog, console, screenshot), then checks that closing stdin with a
dialog open leaves no browser behind. `IE_FIXTURE_HOST` (default `127.0.0.1`) moves the fixture
site to another loopback name for these tests.

CI (`.github/workflows/ci.yml`) runs the unit tests on `windows-latest` with Python 3.10 and
3.14 and checks that the wheel ships the server and the fixture site. It cannot run the
integration tests or `ie-mcp --selftest` (no IE mode on a hosted runner), so run those by hand
before a release.

### Contributing

Bug reports and PRs are welcome. Please:

- Keep the server **dependency-light** (selenium is the only runtime dep).
- Match the existing tool conventions (auto-waiting `find`, `frame` arg, `text_result`).
- Note any IE-mode / Trident quirk you worked around in a code comment — they're rarely obvious.

---

## Roadmap

Ideas for later releases (not promised):

- [ ] Cookie and `localStorage` inspection tools.
- [ ] Richer `ie_grid` (pagination + virtualized-grid scrolling helpers).
- [ ] Optional structured (JSON) tool results alongside text.
- [ ] Configurable per-tool default waits.
- [ ] Cancellable `ie_sleep` and `wait=` pauses.

---

## License

[MIT](LICENSE) © 2026 thomfilg (upstream [thomfilg/ie-mcp](https://github.com/thomfilg/ie-mcp)); the fork's changes are under the same license.
