# IE-mode Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the forked `ie-mcp` into a harness that gives an AI agent full access to Edge IE-mode pages (DOM, interaction, modal dialogs, windows, network, console, rendering) without changing how those pages behave.

**Architecture:** Keep upstream's Selenium IEDriver session as the main backend. Add:
- a Win32/COM backend (`WM_HTML_GETOBJECT`) for modal dialogs and user-opened tabs;
- a watchdog, so blocked driver calls never freeze the stdio server;
- a per-process, streaming recording proxy for network capture, which also inserts a tiny script loader into HTML documents;
- minimal ES3 page instrumentation for errors, dialogs and console.

Verify everything against a generic fixture site shipped as `ie_mcp_fixtures`.

**Tech Stack:** Python 3.10+, selenium 4.x, IEDriverServer 4.14.0 (Win32), Microsoft Edge IE mode, stdlib `http.server`/`http.client`/`ctypes`/`zlib`, optional `pywin32` (COM), `unittest`.

**Spec:** [docs/superpowers/specs/2026-11-17-ie-mode-harness-design.md](../specs/2026-11-17-ie-mode-harness-design.md). Repository rules: [CLAUDE.md](../../../CLAUDE.md). Read its Fidelity, Trident, Injection, Quirks and Safety sections before any task.

## Revision 3 (2026-11-17)

This revision supersedes revision 2 (commit `c298960`) after a second design review. If you are a session already working from revision 2:

- Task 1 is done (`fe27706`). **Task 1b** adds fixture pages and endpoints that later tasks need; do it before Task 2.
- **Task 2 (spike)** has more questions and decisions D9–D13. If Task 2 already ran under revision 2, run only the added parts and extend the decision record (now D1–D13). The added parts:
  - Q2: popups;
  - Q0: a Trident check that does not clear cookies;
  - Q3: COM threading, rejected calls and key input;
  - Q4: `Accept` headers and proxied paths, plus `127.0.0.2`;
  - Q6: `nomode` (both origins), `headless`, `latecharset`, `/progressive`, the loader, and whether the `localhost` frame is cross-origin;
  - Q8: alert class and button IDs.
- **Renumbering:** revision 2's Task 4 is split into Task 4 (settings, options, guards) and Task 5 (session lifecycle, process safety). Revision 2's Tasks 5–15 are now Tasks 6–16.
- **Main design changes:**
  - the proxy streams responses and inserts a small loader script only into HTML documents requested as pages (Task 11);
  - redaction matches name segments instead of regexes and masks echoed secret values (Task 10);
  - every thread that touches COM (the tool-call worker, each hook sub-thread) enters its own apartment, and no COM object outlives the thread that obtained it (Tasks 6–8);
  - `handle()` goes through the watchdog (Task 6);
  - processes are killed only after an identity check (Task 5).

## Global Constraints

- **Application-agnostic.** No company, product, hostname, credential or captured data from any real application anywhere in the repo. Tests target only `ie_mcp_fixtures/site/`.
- **Fidelity (spec R13).** No change to a page's document mode, charset detection, script behaviour, or the content of non-document responses. Every page modification is in the spec's Fidelity table, listed in `ie_status` and the README, and can be switched off.
- **Platform.** Windows 10/11 only; IE mode is never headless.
- **Python and dependencies.** `requires-python = ">=3.10"`; runtime dependency `selenium>=4.14.0,<5` only; `pywin32>=306` as the optional extra `win32`. The server must start and list its tools without pywin32.
- **Driver.** IEDriverServer 32-bit 4.14.0 is the reference version.
  - `build_options` always sets `unhandled_prompt_behavior="ignore"`.
  - Whenever a `proxy` capability is set, it also sets `use_per_process_proxy=True`, with `noProxy` as a list.
  - The HTTP client timeout to the driver is finite (300 s).
- **Injected scripts** follow CLAUDE.md "Trident rules for injected JavaScript": ES3; no `.apply`/`.call` anywhere (deliberately stricter than needed, so the lint is simple); host methods called directly with the caller's exact argument count; `\x1e`/`\x1f` written as escapes.
- **No system changes.** No registry, system-proxy, firewall, certificate or security-setting changes, except upstream's opt-in `IE_SITE_LIST` policy writer. Reading the registry is allowed.
- **Process killing.** Only PIDs recorded by this server with a matching image name and creation time are killed. System dialogs are never clicked.
- **COM objects.** No COM object outlives the thread that obtained it; every thread that touches COM enters its own apartment (`CALL_CONTEXTS`).
- **Captured data.**
  - It lives in memory. JSONL files exist only with `IE_CAPTURE_JSONL=1` and are deleted on close unless `IE_KEEP_ARTIFACTS=1`.
  - Screenshots saved by name are kept.
  - The server log is redacted.
- **Licence.** The MIT licence and upstream attribution are kept.
- **Commands.** Bash (Git Bash) syntax. Integration tests run only through `python scripts/run_integration.py`, which fails on any skipped test. "Expected: OK" always means no `skipped=`.

**Environment variables this plan introduces (defaults in brackets):**

| Variable | Default |
|---|---|
| `IE_TOOL_TIMEOUT` | `45` |
| `IE_CLICK_MODE` | chosen by D1 in Task 2 |
| `IE_CLEAN_SESSION` | `0` |
| `IE_UPLOAD_DIR` | unset (uploads disabled) |
| `IE_CAPTURE` | `1` (D12 may change it to `0`) |
| `IE_CAPTURE_MAX_BODY` | `262144` |
| `IE_CAPTURE_REDACT` | `1` |
| `IE_CAPTURE_REDACT_FIELDS` | unset: extra field names, comma-separated, added to the built-in list |
| `IE_CAPTURE_REDACT_HEADERS` | unset: extra header names, comma-separated, added to the built-in list |
| `IE_CAPTURE_JSONL` | `0` |
| `IE_CAPTURE_UPSTREAM_PROXY` | unset |
| `IE_CAPTURE_BYPASS` | unset: host patterns, comma-separated, kept off the proxy |
| `IE_CAPTURE_NO_CACHE` | `0` |
| `IE_INSTRUMENT` | `1` |
| `IE_CONSOLE_STUB` | `0` |
| `IE_KEEP_ARTIFACTS` | `0` |
| `IE_FIXTURE_HOST` | `127.0.0.1` |

`IE_INTERACTION_MODE` changes its default from `native` to `background`.

## Review Focus

1. **A click that opens a modal dialog.** The server must answer every later tool call within its budget, every result must say a modal is open, and `ie_close(force=true)` must always recover. Pinned by Task 6 `test_slow_tool_times_out_with_blocked_message` and `test_force_close_works_while_driver_locked`, and Task 9 `test_driver_click_on_modal_opener_does_not_hang_server`.
2. **The proxy changing what the page receives.** Covers the document mode (IE7 meta page stays 7, quirks page stays 5, the page without a mode meta keeps its mode), and XHR responses that are fragments or plain text staying byte-identical. Pinned by Task 11 `test_inject_after_leading_meta_title_base`, `test_headless_document_keeps_xua_meta_first` and `test_fragment_never_injected`, and Task 13 `test_document_mode_unchanged_by_injection` and `test_xhr_fragment_identical_with_capture_on_and_off`.
3. **A script alert open while the agent takes screenshots or runs scripts.** The alert must stay open until the agent handles it. Pinned by Task 4 `test_build_options_ignores_unhandled_prompts` and Task 9 `test_alert_survives_screenshot_and_js`.
4. **Traffic, console text and logs containing passwords, tokens or cookies,** including values echoed back in responses, without masking ordinary fields. Pinned by Task 10 `test_masks_redacted_value_in_response_body`, `test_ordinary_fields_not_redacted` and `test_jsonl_contains_no_secret_values`, and Task 13 `test_form_post_captured_with_redaction`.
5. **Non-ASCII text in windows-1252 and UTF-8 pages** (`Müller – café`). Survives fill/get, network bodies and console without mojibake. Pinned by Task 8 `test_fill_and_get_non_ascii`, and Task 13 `test_request_body_decoded_with_page_charset` and `test_request_body_decoded_with_page_charset_live`.

---

### Task 1: Baseline, fixture package and fixture server — DONE

Done on `harness` in `fe27706` ("test: add legacy fixture site and two-origin fixture server"), against plan revision 2. It shipped:
- **`ie_mcp_fixtures`:** the package with `start_fixture_server(host="127.0.0.1", port=0, alt_port=0) -> FixtureServer`. `base_url` and `alt_base_url` are two listeners on the **same host, different ports**. It has `stop()`, a context manager, and `python -m ie_mcp_fixtures --host/--port/--alt-port`.
- **Endpoints:** `POST /echo`, `GET /api/lookup.xml?q=`, `GET /slow?ms=`, `GET /cookie`.
- **Server behaviour:** `{{ALT_BASE}}` substitution; `Cache-Control: no-store` on every response (errors included); path-traversal refusal.
- **Pages:** `index`, `frameset`, `menu`, `form`, `inner`, `xorigin`, `quirks`, `charset`, `utf8`, `modal_opener`, `modal_dialog`, `modal_confirm`, `modal_nested`, `xhr`, `dialogs`, `popup`, `errors`, `grid`.
- **Packaging:** `pyproject.toml` with Python ≥ 3.10, the `win32` extra and the `ie_mcp_fixtures` package and package data.
- **Tests:** `tests/test_fixture_server.py` with 14 tests; the unit suite is 25 tests.

Task 1b adds what revision 3 of this plan asks for on top.

- [x] **Step 1–6:** done in `fe27706`.

---

### Task 1b: Fixture additions for revision 3

**Files:**
- Modify: `ie_mcp_fixtures/server.py`, `ie_mcp_fixtures/site/modal_dialog.htm`, `ie_mcp_fixtures/site/xhr.htm`, `ie_mcp_fixtures/site/xorigin.htm` (comment), `ie_mcp_fixtures/site/index.htm` (links, `typed` input)
- Create: `ie_mcp_fixtures/site/nomode.htm`, `headless.htm`, `latecharset.htm`
- Test: `tests/test_fixture_server.py`

**Interfaces:**
- Consumes: the Task 1 server.
- Produces:
  - **The other origin uses another host name.** `FixtureServer.alt_base_url` becomes `http://localhost:<alt_port>/`, because IE's same-origin rule ignores ports, so a different port alone is not a second origin for IE. The alt port gets listeners on `127.0.0.1` and, when IPv6 loopback is available, on `::1` (same port). `base_url` is unchanged (`http://<host>:<port>/`).
  - **`GET /fragment`:** `Content-Type: text/html`, body exactly `OK|A1` (no tags).
  - **`GET /big?ms=N`:** a 1,048,576-byte `text/plain` body written in 8 KB writes spread over N ms (with `Content-Length`; the first write happens immediately).
  - **`GET /progressive?ms=N`:** a `text/html` IE7 document whose head is flushed in separate small writes: `<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN" "http://www.w3.org/TR/html4/loose.dtd"><html><head><title>progressive</title>`, then `<meta http-equiv="X-UA-Compatible" content="IE=7">`, then `<link rel="stylesheet" href="data:text/css,">`, with about 50 ms between these writes and `TCP_NODELAY` set, so a reader sees them as separate reads. The rest (`</head><body>progressive ok</body></html>`) follows after N ms.
  - **`FixtureServer.requests`:** a thread-safe list of `RequestRecord(method: str, path: str, headers: dict[str, str], conn_id: int)` for every request either listener received (`conn_id` identifies the client TCP connection), plus `clear_requests() -> None`.
  - **`xorigin.htm`**'s comment no longer says "same host, other port".
  - **New pages:**

| Page | Contents |
|---|---|
| `nomode.htm` | HTML5 doctype, **no** `X-UA-Compatible` meta; text `no mode meta` |
| `headless.htm` | `<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN" "http://www.w3.org/TR/html4/loose.dtd">` then `<html><meta http-equiv="X-UA-Compatible" content="IE=7"><title>headless</title><body>headless ok</body></html>` (no `<head>` tag) |
| `latecharset.htm` | Encoded as `windows-1252`; the head contains `<link rel="stylesheet" href="data:text/css,">` and a short inline `<script>` **before** `<meta http-equiv="Content-Type" content="text/html; charset=windows-1252">`; text `Müller – café` |

  - **`modal_dialog.htm`** gains input `search` (id = name). An `onkeypress` handler, on Enter (keyCode 13), reloads the list from `/api/lookup.xml?q=<search value>` and writes the number of rows into `search_count`. `loadXml()` takes the `q` value as a parameter. It also gains input `typed`, whose `onkeypress` handler increments the number in `keypress_count` (starting at 0) and appends the typed character to `typed_chars`. It also gains button `dialog_alert`, which runs `alert('Dialog alert')` and then writes `alerted` into `dialog_alert_result`.
  - **`xhr.htm`** gains button `load_fragment`: GET `/fragment` via `new ActiveXObject('Microsoft.XMLHTTP')` (fallback `XMLHttpRequest`), writing `responseText` into `fragment_result`.
  - **`index.htm`** links the three new pages and gains the same `typed` input with `keypress_count` and `typed_chars` (an IE11-mode document for the spike's key test).

- [ ] **Step 1: Write the failing tests** (add to `tests/test_fixture_server.py`; update the existing alt-origin test)

```python
def test_fragment_endpoint_is_bare_text(self)     # GET /fragment -> content-type startswith "text/html"; body == b"OK|A1"
def test_big_endpoint_streams(self)               # GET /big?ms=500 -> 1048576 bytes in total; the first chunk is read before 400 ms
def test_alt_origin_uses_other_host(self)         # alt_base_url startswith "http://localhost:"; xorigin.htm contains alt_base_url + "inner.htm";
                                                  # GET http://127.0.0.1:<alt_port>/inner.htm -> 200 (test_alt_origin_placeholder_replaced stays as it is)
def test_new_pages_served(self)                   # nomode.htm contains "no mode meta" and no "X-UA-Compatible"; headless.htm has no "<head";
                                                  # latecharset.htm bytes contain b"M\xfcller" and "<script" occurs before "charset=windows-1252"
def test_dialog_and_xhr_pages_have_new_controls(self)  # modal_dialog.htm contains 'id="search"' and 'id="search_count"'; xhr.htm contains 'id="load_fragment"' and 'id="fragment_result"';
                                                  # modal_dialog.htm and index.htm contain 'id="typed"' and 'id="keypress_count"'; modal_dialog.htm contains 'id="dialog_alert"'
def test_requests_are_recorded(self)               # GET /fragment with header X-Test: 1 -> requests[-1].method == "GET", .path == "/fragment", .headers["X-Test"] == "1"; clear_requests() empties it;
                                                  # two requests on one keep-alive http.client connection share a conn_id; a new connection gets a different one
def test_progressive_endpoint_flushes_head_early(self)  # GET /progressive?ms=800 -> the bytes up to and including the X-UA meta are read before 500 ms; the full body after
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_fixture_server -v`
Expected: the new tests fail.

- [ ] **Step 3: Implement the additions**

- [ ] **Step 4: Run all unit tests**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: `Ran 32 tests` … `OK`.

- [ ] **Step 5: Commit**

```bash
git add ie_mcp_fixtures/ tests/test_fixture_server.py
git commit -m "test: fixture additions for plan revision 3 (fragment, big, mode pages, dialog search)"
```

---

### Task 2: Spike — answer the open questions on a real IE-mode session

**Files:**
- Create: `scripts/spike_ie_mode.py`, `docs/spikes/2026-11-17-ie-mode-spike.md`

**Interfaces:**
- Consumes: `start_fixture_server` and the pages from Tasks 1 and 1b.
- Produces: the decision record. Later tasks read these keys from it:

| Key | Values | What depends on it |
|---|---|---|
| D1 | `IE_CLICK_MODE` default, per interaction mode: `driver` or `async` | Task 6 default; Task 9 tests |
| D2 | Way into modal dialogs: `driver-handles`, `com`, or `both` | Tasks 8, 9 |
| D3 | Network capture: `proxy` or `instrument-only` | Tasks 12, 13 |
| D4 | Loopback proxying with `noProxy: ["<-loopback>"]`: `works` or `bypassed` | Task 13 integration tests |
| D5 | `console` present per document mode (11 / 7 / 5): yes/no each | README; Task 12 tests |
| D6 | Window/process ownership: `edge-tree` (content and dialogs are in Edge's process tree) or `owner-chain` (follow window owners from the session's IE server windows) | Task 7 `session_window_pids` |
| D7 | COM: script execution path per document mode (`execScript`, `eval` or `script-element`); worker-thread apartment works (yes/no); calls rejected while a dialog is open (yes/no, with retry behaviour); key path per document mode: the posted message sequence that gives exactly one keypress (`keydown-keyup`, `char-only` or `keydown-char-keyup`), or `fireEvent` (old document modes only) | Task 7 `com_call`; Task 8 `ComBackend` |
| D8 | Pop-up blocker blocks `window.open` from an async click: yes/no | Task 6 tool description; Task 9 popup test |
| D9 | `window.open` popups appear in `driver.window_handles`: yes/no | Task 9 `ie_switch_window` |
| D10 | Document navigations send `Accept` containing `text/html` while XMLHTTP does not: yes/no | Task 11 `is_document_request`; if no, the proxy never injects and instrumentation comes only from the post-call hook (Task 13) |
| D11 | Proxied paths: which of initial URL, later navigation, popup, modal dialog, dialog XMLHTTP went through the per-process proxy | Task 13 `capture_note`; README |
| D12 | The proxy changes `documentMode` of `nomode.htm` through either origin (`127.0.0.1` or `localhost`): yes/no | Task 13 default of `IE_CAPTURE` (yes → default `0`, README explains) |
| D13 | IE treats the `localhost` frame on `xorigin.htm` as cross-origin (reading `frames['foreign'].document` throws): yes/no | Task 8 cross-origin test |

**Stop rules.** Stop and report to the user before continuing if any of these happen:
- Q2 and Q3 both find no way into a modal dialog.
- Q4 shows that the user's global Internet Settings changed.
- Q8 shows an alert being dismissed despite `ignore`.
- D4 = `bypassed`, and `http://127.0.0.2:<port>/` is bypassed too. The only other route is serving fixtures on a LAN address, which triggers a firewall prompt; the user decides.
- Q6 shows the loader changing any document mode or charset.

The spike script uses raw Selenium plus ctypes/pywin32, not the server. Every spike session sets `ensure_clean_session = False` explicitly (upstream's default clears the user's IE-mode cookies and cache). It prints one `Qn:` line per question with its evidence. It always runs `driver.quit()` and kills its own Edge (by Edge profile) in `finally`. Each question that needs a fresh session quits the previous one first. The proxy it uses for Q4 and Q6 is a minimal stdlib logging proxy that can optionally insert the same tag the product ships, `<script type="text/javascript" src="http://<document host:port>/__iemcp__/i.js"></script>` (and answer that path with `window.__inj=1`), using the placement rule from CLAUDE.md "Injection rules". Q6 (c) must use exactly that tag, because the charset prescan of `latecharset.htm` depends on its length.

- [ ] **Step 1: Prepare the driver**

Ask the user to put `IEDriverServer.exe` 4.14.0 (Win32) in place and set `IE_DRIVER_PATH`. **Do not run upstream's `--selftest` yet**: until Task 4 it forces `ensure_clean_session=True`, which clears the user's IE-mode cookies and cache. The spike's `q0_trident` checks the engine instead.

- [ ] **Step 2: Write `scripts/spike_ie_mode.py`** with one function per question. Run Q1, Q2 and Q7 once with `native_events/require_window_focus` True and once with both False.

  - **`q0_trident`:** open `index.htm`; report the userAgent, which must contain `Trident`.
  - **`q1_driver_click_blocks`:** open `modal_opener.htm`. Run `find_element(By.ID,'open_lookup').click()` in a thread and join it with a 15 s timeout. Report `blocks=True/False` per mode. Then close the dialog with Win32 `PostMessage(WM_CLOSE)` so the session recovers.
  - **`q2_handles`:** click `open_lookup` asynchronously with `execute_script("var b=arguments[0];window.setTimeout(function(){b.click();},0);", el)`.
    - Poll `driver.window_handles` for 10 s. If a new handle appears: `switch_to.window`, `return document.title`, click `pick_A1`, switch back, read `lookup_code`.
    - With a dialog open, run `execute_script("return 1")` on the opener in a thread with a 10 s join, and report whether it blocks.
    - Repeat the handle check for `do_popup` on `dialogs.htm` (D9).
  - **`q3_win32_com`:** keep a dialog open (async click).
    - `EnumWindows` → class, title, owner hwnd and owning PID of every visible window. Compare the PIDs with Edge's process tree.
    - Find the `Internet Explorer_Server` child under the dialog. `WM_HTML_GETOBJECT` → `ObjectFromLresult` → `document.title` and `body.innerText[:80]`.
    - On the dialog's, the opener's and `index.htm`'s documents, test `parentWindow.execScript("window.__t=1+1")` and read `__t` back with `getattr`. If that fails, test `parentWindow.eval`, then appending a `<script>` element.
    - Make one call on the opener's document while the dialog is open; record any `RPC_E_CALL_REJECTED` or `RPC_E_SERVERCALL_RETRYLATER` and whether a retry after 200 ms succeeds. In Q8's session, also try reading the document that owns an open `alert` the same way.
    - Repeat one read from a new thread that calls `pythoncom.CoInitialize()` and obtains its own document.
    - **Key input.** In the dialog (IE7 mode) and on `index.htm` (IE11 mode), focus `typed` and send the character `x` with each candidate. After each, record `keypress_count` and `typed_chars`. The candidates:
      - (a) posted `WM_KEYDOWN` + `WM_KEYUP`, relying on the target thread's `TranslateMessage` to produce `WM_CHAR`;
      - (b) posted `WM_CHAR` only;
      - (c) posted `WM_KEYDOWN` + `WM_CHAR` + `WM_KEYUP`;
      - (d) `fireEvent('onkeypress')` with `keyCode` (old document modes only).

      The right path gives exactly one keypress and the character `x`. Then, in the dialog, set `search` to `B` and send Enter with the chosen path; `search_count` must become 1.
    - **Navigation.** Record the main window's `Internet Explorer_Server` hwnd and its ancestor hwnds up to the top window (with classes), run `get(form.htm)`, and record them again: which of them survived the navigation. Task 8's tab resolution relies on the nearest surviving ancestor.
  - **`q4_per_process_proxy`:** quit the earlier session. Read (read-only) `HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings` `ProxyEnable`/`ProxyServer`/`ProxyOverride`.
    - Start the logging proxy (logs request line and `Accept` header). Create a session with `options.proxy = Proxy({"proxyType": "manual", "httpProxy": "127.0.0.1:<p>", "sslProxy": "127.0.0.1:<p>", "noProxy": ["<-loopback>"]})` and `options.use_per_process_proxy = True`, with `initial_browser_url = <base>index.htm`.
    - Report whether the proxy saw: the initial URL; a later `get(form.htm)`; `popup.htm` from `do_popup`; `modal_dialog.htm` and `/api/lookup.xml` from the modal flow (D11). Report the `Accept` headers of document requests versus XMLHTTP requests (D10).
    - If nothing was seen, repeat with the fixture server reached as `http://127.0.0.2:<port>/` (bind the fixture server to `127.0.0.2` for this run).
    - Read the Internet Settings again and report whether they are unchanged.
  - **`q5_console`:** `return typeof console` on `index.htm`, `form.htm` and `quirks.htm`.
  - **`q6_document_modes`:** `return navigator.userAgent + '|' + document.documentMode` on:
    - `index.htm`, `form.htm`, `quirks.htm`, `headless.htm`, `nomode.htm`;
    - `latecharset.htm` (also read `document.charset` there);
    - `nomode.htm` again through the `localhost` alt origin;
    - the document of `/progressive?ms=800`.

    On `xorigin.htm`, also record whether reading `frames['foreign'].document` throws (D13). Measure each page in three runs:
    - (a) without a proxy;
    - (b) through the proxy without the loader (D12 for `nomode.htm`);
    - (c) through the proxy with the loader, and `window.__inj` must be 1.

    Modes and charset must be identical in (a) and (c), apart from any difference already in (b), which D12 records.
  - **`q7_popup_blocker`:** async click on `do_popup`; report whether a popup window appeared.
  - **`q8_prompt_ignore`:** session with `unhandled_prompt_behavior = "ignore"`. Click `do_alert` asynchronously, then `execute_script("return 1")` and `get_screenshot_as_png()`. Report the error (if any), whether `switch_to.alert.text == "Hello alert"` still works afterwards, and the alert window's class name and the control IDs of its buttons (`GetDlgCtrlID`). Accept it, then do the same for `do_confirm` and `do_prompt` (dismiss each), recording for each window its class, title and the control IDs and classes of its children: the script-dialog shape is an exact set per kind, and a `prompt` may turn out to be an `Internet Explorer_TridentDlgFrame` window rather than `#32770`. Then load `modal_opener.htm`, open the lookup dialog (async click on `open_lookup`), click `dialog_alert` inside it asynchronously (through the dialog's document over COM, or its driver handle if Q3 found COM unusable, queued with `setTimeout` so the spike is not blocked while the alert is open), and record the alert's owner hwnd and that owner's class (expected: the dialog's `Internet Explorer_TridentDlgFrame`).

- [ ] **Step 3: Run the spike on an unlocked desktop**

Run: `python scripts/spike_ie_mode.py`
Expected: lines `Q0:` … `Q8:` (`Q0` showing `Trident`), with no `msedge.exe --ie-mode-force` or `IEDriverServer.exe` left running.

- [ ] **Step 4: Write `docs/spikes/2026-11-17-ie-mode-spike.md`** with a table `Question | Answer | Evidence` and a table `Decision | Value | Reason` for D1–D13. Apply the stop rules above.

- [ ] **Step 5: Commit**

```bash
git add scripts/spike_ie_mode.py docs/spikes/
git commit -m "docs: record IE-mode spike results and decisions"
```

---

### Task 3: Split the single file into the `ie_mcp` package (no behaviour change)

**Files:**
- Create: `ie_mcp/__init__.py`, `ie_mcp/__main__.py`, `ie_mcp/cli.py`, `ie_mcp/config.py`, `ie_mcp/protocol.py`, `ie_mcp/session.py`
- Create: `ie_mcp/tools/__init__.py`, `navigation.py`, `read.py`, `interact.py`, `dialogs.py`, `diagnostics.py`
- Create: `ie_mcp/js/__init__.py`, plus `select.js`, `grid.js`, `ready_probe.js`, moved from `_SELECT_JS`, `_GRID_JS` and `_READY_PROBE_JS`. The raw `0x1E`/`0x1F` bytes in upstream (`ie_mcp.py` lines 1101, 1120–1122, 1187) are rewritten as the escapes `\x1e`/`\x1f`.
- Delete: `ie_mcp.py`
- Modify: `pyproject.toml`:
  - remove both `py-modules = ["ie_mcp"]` and `packages = ["ie_mcp_fixtures"]` from `[tool.setuptools]`;
  - add `[tool.setuptools.packages.find] include = ["ie_mcp*"]` (this matches `ie_mcp_fixtures` too);
  - `[tool.setuptools.package-data]` gains `"ie_mcp.js" = ["*.js"]` (the key must be quoted; a bare dotted key creates a nested table);
  - script `ie-mcp = "ie_mcp.cli:main"`.
- Modify: `tests/test_interaction_mode.py`, `tests/test_session_ownership.py` (import from the new modules)
- Test: `tests/test_protocol.py`, `tests/test_js_resources.py`

**Tool → module mapping:**

| Module | Tools |
|---|---|
| `tools/navigation.py` | `ie_open`, `ie_goto`, `ie_back`, `ie_forward`, `ie_resize`, `ie_close` |
| `tools/read.py` | `ie_text`, `ie_html`, `ie_get`, `ie_frames`, `ie_grid`, `ie_screenshot`, `ie_wait_text`, `ie_wait_ready`, `ie_wait_element` |
| `tools/interact.py` | `ie_click`, `ie_fill`, `ie_select`, `ie_press_key`, `ie_hover`, `ie_scroll`, `ie_upload`, `ie_js` |
| `tools/dialogs.py` | `ie_dialog` |
| `tools/diagnostics.py` | `ie_status`, `ie_browsers`, `ie_kill_orphans`, `ie_sleep` |

**Interfaces:**
- Produces:
  - **`ie_mcp.config.Settings`** (frozen dataclass, one field per env var) and `load_settings(env: Mapping[str, str]) -> Settings`.
    - Module-level `SETTINGS = load_settings(os.environ)`.
    - Code reads `config.SETTINGS` at call time (`from ie_mcp import config` … `config.SETTINGS.x`), never `from ie_mcp.config import SETTINGS`, so tests can replace it.
    - Each later task adds its env vars here.
  - **`ie_mcp.protocol.ToolSpec(name: str, description: str, input_schema: dict, fn: Callable[[dict], dict], needs_driver: Callable[[dict], bool], budget: Callable[[dict], float] | None)`.**
  - **`ie_mcp.protocol.tool(name, description, schema, *, needs_driver: bool | Callable[[dict], bool] = True, budget: Callable[[dict], float] | None = None)`.** A bool is wrapped as `lambda args: value`. It registers the spec in `REGISTRY: dict[str, ToolSpec]` (insertion-ordered).
  - **`ie_mcp.protocol.handle(msg: dict) -> dict | None`**, `serve(stdin, stdout) -> None`, `text_result(s: str) -> dict`, `image_result(png: bytes) -> dict`. In this task `handle` calls the tool function directly, as upstream does; Task 6 routes it through the watchdog.
  - **`ie_mcp.js.load(name: str) -> str`** (`importlib.resources`, cached).
  - **`ie_mcp.session.SESSION: IeSession`**, `LEASE: SessionLease`, plus the upstream functions with unchanged signatures.
  - Tool modules register themselves on import; `ie_mcp/tools/__init__.py` imports all of them.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_protocol.py
UPSTREAM_TOOLS = {"ie_open","ie_goto","ie_status","ie_browsers","ie_kill_orphans","ie_frames","ie_text",
  "ie_html","ie_click","ie_fill","ie_upload","ie_dialog","ie_js","ie_screenshot","ie_wait_text",
  "ie_wait_ready","ie_select","ie_press_key","ie_back","ie_forward","ie_hover","ie_resize","ie_get",
  "ie_wait_element","ie_scroll","ie_grid","ie_sleep","ie_close"}
def test_tools_list_contains_all_upstream_tools(self)   # names from handle({"jsonrpc":"2.0","id":1,"method":"tools/list"}) == UPSTREAM_TOOLS
def test_unknown_tool_returns_jsonrpc_error(self)       # error.code == -32602
def test_tool_exception_becomes_is_error_result(self)   # registered failing tool -> result["isError"] is True, text startswith "ERROR in"
def test_initialize_returns_instructions(self)          # result has "instructions"; result["serverInfo"]["name"] == "ie-mcp"
def test_bool_needs_driver_becomes_callable(self)       # tool(..., needs_driver=False) -> spec.needs_driver({}) is False
def test_settings_read_at_call_time(self)               # replace config.SETTINGS with page_timeout 99 -> a tool reading it sees 99
# tests/test_js_resources.py
def test_js_resources_are_es3(self)
    # for every ie_mcp/js/*.js: none of "=>", "`", r"\blet\b", r"\bconst\b", "JSON.", ".forEach(", ".map(",
    # ".filter(", "Object.keys", ".trim(", ".apply(", ".call(" ; "querySelector" only on lines containing "documentMode"
def test_js_resources_have_no_raw_separator_bytes(self)  # "\x1e" and "\x1f" characters absent from file text
def test_load_returns_script_text(self)                  # load("grid.js") contains "getElementsByTagName('table')"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_protocol tests.test_js_resources -v`
Expected: ImportError.

- [ ] **Step 3: Move the code into the modules above**, with unchanged behaviour. Every env var goes through `Settings`; every tool uses `@tool`.

- [ ] **Step 4: Run the unit suite and the selftest**

Run: `pip install -e ".[win32]"`, then `python -m unittest discover -s tests -p "test_*.py" -v`, then `python -c "import ie_mcp.cli, ie_mcp.tools"`
Expected: all tests OK; the import succeeds. Do not run `--selftest` before Task 4: it still clears the user's IE-mode cookies and cache.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: split server into ie_mcp package with tool registry"
```

---

### Task 4: Settings, driver options and tool guards

**Files:**
- Modify: `ie_mcp/config.py`, `ie_mcp/session.py`, `ie_mcp/protocol.py` (`SERVER_INSTRUCTIONS`), `ie_mcp/tools/interact.py`, `ie_mcp/tools/read.py`
- Create: `ie_mcp/js/frames.js`
- Test: `tests/test_config.py`, `tests/test_interaction_mode.py`, `tests/test_session_options.py`, `tests/test_tool_guards.py`

**Interfaces:**
- Produces:
  - **`config.discover_driver_path(env: Mapping[str,str], home: Path) -> str | None`:**
    - If `IE_DRIVER_PATH` is set: return it if the file exists, else raise `FileNotFoundError` naming `IE_DRIVER_PATH`.
    - Else the highest version under `home/.cache/selenium/IEDriverServer/win32/*/IEDriverServer.exe` (numeric version sort); else the same under `win64`; else `None`.
  - **`session.build_options(settings: Settings, initial_url: str | None, proxy_port: int | None = None, bypass: list[str] | None = None) -> IeOptions`:**
    - the upstream options;
    - `unhandled_prompt_behavior = "ignore"`;
    - `ensure_clean_session = settings.clean_session`;
    - focus/native events from the interaction mode;
    - when `proxy_port` is given: `proxy` with `noProxy = ["<-loopback>"] + (bypass or [])` and `use_per_process_proxy = True`. Proxy wiring is used from Task 13.
  - **`session.DRIVER_HTTP_TIMEOUT = 300`.** The driver is created with that client timeout, using whichever API the installed selenium offers (`ClientConfig(timeout=…)` or `RemoteConnection.set_timeout`). Check the installed version and comment which one is used.
  - **Settings:** `clean_session: bool` (`IE_CLEAN_SESSION`, default `False`); `upload_dir: Path | None` (`IE_UPLOAD_DIR`); `pydeps: Path | None` (set only when `IE_PYDEPS` is given; no implicit `../.pydeps`).
  - **Interaction mode default is `background`.** `SERVER_INSTRUCTIONS` is reworded to match: background is the default and `native` the fallback.
  - **Screenshot `save_path`:** only a relative file name is accepted. It is saved under `<session dir>/screenshots/` and **kept** after close.
  - **`ie_upload`:** `path` must resolve inside `upload_dir`.
  - **`ie_text`:** falls back to `execute_script("return document.body.innerText")` when `.text` fails.
  - **`frames.js`:** reads `name`/`id`/`src` of each `frame`/`iframe` of the current document and returns a `\x1e`/`\x1f` string. `ie_frames` returns `[{"index", "name", "id", "src"}]` (its upstream description already promised this).

- [ ] **Step 1: Write the failing tests**

```python
def test_discover_prefers_highest_win32_version(self)     # dirs 4.8.0 and 4.14.0 -> returned path contains "4.14.0"
def test_discover_honours_explicit_env_path(self)          # IE_DRIVER_PATH=<existing tmp file> -> that path
def test_discover_errors_when_explicit_path_missing(self)  # IE_DRIVER_PATH=<missing> -> FileNotFoundError, "IE_DRIVER_PATH" in str
def test_discover_returns_none_when_nothing_found(self)
def test_background_is_default_interaction_mode(self)     # resolve_interaction_mode({}).name == "background" (replaces test_native_mode_is_the_backward_compatible_default)
def test_instructions_describe_background_default(self)   # the sentence naming "background" in SERVER_INSTRUCTIONS contains "default"
def test_build_options_ignores_unhandled_prompts(self)    # build_options(load_settings({}), None).unhandled_prompt_behavior == "ignore"
def test_clean_session_is_opt_in(self)                    # default -> ensure_clean_session False; IE_CLEAN_SESSION=1 -> True
def test_proxy_always_per_process(self)                   # build_options(..., proxy_port=8123, bypass=["*.example.test"]): use_per_process_proxy True;
                                                          # proxy.noProxy == ["<-loopback>", "*.example.test"]; http and ssl proxy "127.0.0.1:8123"
def test_upload_disabled_without_upload_dir(self)         # ie_upload -> isError, text contains "IE_UPLOAD_DIR"
def test_upload_rejects_path_outside_upload_dir(self)     # path "..\\x.txt" -> isError, text contains "outside IE_UPLOAD_DIR"
def test_screenshot_save_path_must_be_relative(self)      # "C:\\x.png" and "..\\x.png" -> isError; "shot.png" -> file under <session dir>/screenshots
def test_pydeps_not_added_without_env(self)               # load_settings({}).pydeps is None
def test_text_falls_back_to_inner_text(self)              # fake element whose .text raises -> result equals the fake execute_script innerText
def test_frames_lists_name_id_src(self)                   # fake execute_script returns "0\x1fmenu\x1fmenu\x1fmenu.htm\x1e1\x1fmain\x1f\x1fform.htm"
                                                          # -> [{"index":0,"name":"menu","id":"menu","src":"menu.htm"},{"index":1,"name":"main","id":"","src":"form.htm"}]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_config tests.test_interaction_mode tests.test_session_options tests.test_tool_guards -v`
Expected: the new tests fail.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the unit suite**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "fix: safe defaults, prompt handling, driver discovery and tool guards"
```

---

### Task 5: Session lifecycle and process safety

**Files:**
- Create: `ie_mcp/win32/__init__.py`, `ie_mcp/win32/processes.py` (ctypes; this task adds only `process_identity`)
- Modify: `ie_mcp/session.py`, `ie_mcp/tools/navigation.py` (`ie_close`), `ie_mcp/cli.py` (`main()` uses `close()`)
- Test: `tests/test_session_ownership.py`, `tests/test_session_lifecycle.py`

**Interfaces:**
- Produces:
  - **`processes.process_identity(pid: int) -> tuple[str, int] | None`:** image file name (lower case) and creation time (FILETIME as int), via `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`, `QueryFullProcessImageNameW` and `GetProcessTimes`. Returns `None` if the process is gone.
  - **`session.pid_alive(pid: int, run=subprocess.run) -> bool`:** parses `tasklist /FI "PID eq <pid>" /FO CSV /NH` and compares the PID column exactly.
  - **`session.kill_verified(targets: list[tuple[int, tuple[str, int] | None]], identity=process_identity, run=subprocess.run) -> list[int]`:** kills (`taskkill /F /PID`, **without** `/T`) only those PIDs whose current identity equals the recorded one; a recorded identity of `None` never matches. Every kill path uses it. Edge's child processes exit with their browser process, so no tree kill is needed.
  - **Owner records** (`owner.json`) gain `driver_identity` and `edge_identity`. `cleanup_stale_sessions` requires both the Edge profile match (upstream rule) and the identity match.
  - **Stale takeover:** when the lease takes over a dead holder, the old `owner.json` is first renamed to `owner.stale-<oldpid>.json`; `cleanup_stale_sessions` globs `*/owner*.json`.
  - **`IeSession`:**
    - `ensure()` holds the lease across all retries;
    - `quit_browser() -> None` quits the driver and kills its own Edge, but **keeps** the lease;
    - `kill_own_processes() -> list[int]` calls `kill_verified` for `driver_pid`/`edge_pid` with their identities;
    - `cancel: threading.Event` is set by force-close; wait loops (`ie_wait_ready`, `ie_wait_text`, `ie_wait_element`, `find`) check it on every poll and stop with `isError` "cancelled". `ensure()` clears it before building a new driver;
    - `close(timeout_s: float = 10) -> None` sets `cancel`, runs `driver.quit()` in a thread joined for `timeout_s`, then `kill_own_processes()`, then releases the lease.
  - **`ie_close(force: bool = False)`:** `needs_driver = lambda a: not a.get("force")`. Without `force` it calls `quit_browser()`. With `force=True` it sets `cancel`, calls `kill_own_processes()`, drops the driver reference and never touches the driver. `cli.main()`'s `finally` calls `SESSION.close()`.

- [ ] **Step 1: Write the failing tests**

```python
def test_pid_alive_requires_exact_pid_match(self)         # fake tasklist CSV row for PID 123: ask 12 -> False; ask 123 -> True
def test_kill_skips_reused_pid(self)                      # recorded ("msedge.exe", 100), current identity ("notepad.exe", 200) -> not killed; matching -> killed
def test_stale_takeover_keeps_old_record_for_cleanup(self):
    # owner.json {session_id:"s", owner_pid:111, driver_pid:5, edge_pid:6, profile:"p", driver_identity:[...], edge_identity:[...]};
    # lease for pid 222 acquires (111 dead) -> owner.stale-111.json exists;
    # cleanup_stale_sessions(root, browser_rows=[{"pid":6,"kind":"edge-ie","profile":"p"},{"pid":5,"kind":"iedriver","profile":None}],
    #   pid_alive=lambda p: p == 222, identity=<returns recorded identities>, kill=fake) -> fake called with [5, 6]
def test_lease_is_held_across_attach_retries(self)        # fake builder fails twice then succeeds; lock file owner_pid == own pid after ensure()
def test_close_kills_own_processes_when_quit_hangs(self)  # fake driver.quit sleeps 5s; close(timeout_s=0.2) returns < 1s; kill_verified called with own pids
def test_force_close_does_not_touch_driver(self)          # ie_close force=True with a driver whose methods raise -> no exception; own pids killed; cancel set
def test_wait_loop_stops_when_cancelled(self)             # ie_wait_text on a fake that never matches; set SESSION.cancel after 0.2s -> returns within 1s, isError "cancelled"
def test_cancel_cleared_by_next_session(self)             # force close, then ensure() with a fake builder -> cancel not set; ie_wait_text polls normally
def test_unknown_identity_never_killed(self)              # recorded identity None -> taskkill not called
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_session_ownership tests.test_session_lifecycle -v`
Expected: the new tests fail.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the unit suite**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "fix: lease handling, identity-checked process killing, cancellable close"
```

---

### Task 6: Watchdog, driver lock, budgets, async click and modal notices

**Files:**
- Modify: `ie_mcp/protocol.py`, `ie_mcp/session.py`, `ie_mcp/config.py`, the tool modules (budgets, `needs_driver`), `ie_mcp/tools/interact.py`
- Test: `tests/test_watchdog.py`, `tests/test_click_mode.py`

**Interfaces:**
- Consumes: `ToolSpec.needs_driver`/`budget` (Task 3); `IeSession.close`, `kill_own_processes`, `cancel` (Task 5); D1 and D8 (Task 2).
- Produces:
  - **`session.DRIVER_LOCK: threading.Lock`.**
  - **`protocol.call_budget(spec, args) -> float`:** `max(config.SETTINGS.tool_timeout, spec.budget(args))` when the tool declares a budget, else `config.SETTINGS.tool_timeout`. Declared budgets:
    - `ie_open`/`ie_goto`: `attach_retries * 75 + wait + 15` (a 30 s attach plus two process listings of up to 20 s each, per attempt);
    - `ie_wait_ready`, `ie_wait_text`, `ie_wait_element`: `timeout + interval + 15`;
    - locator tools: `timeout + wait + 15`;
    - `ie_sleep`: `seconds + 5`.
  - **`protocol.CALL_CONTEXTS: list[Callable[[], ContextManager]]`:** empty here; Task 7 appends `com_apartment`. The worker enters all of them around the whole call (tool, hooks, notices), and **every sub-thread a hook runs in enters them again for itself**. No COM object outlives the thread that obtained it.
  - **`protocol.run_tool(spec: ToolSpec, args: dict) -> dict`:** runs the tool in a daemon worker. For `spec.needs_driver(args)` it first calls `DRIVER_LOCK.acquire(timeout=budget)`.
    - If the lock cannot be acquired: return `isError` with text `BUSY: <running tool> has held the browser for <n>s`. If `MODAL_PROBE()` is non-empty, return `BLOCKED: modal dialog open …` instead. `MODAL_PROBE` is Win32-only (no COM), so it is safe on any thread.
    - If the worker exceeds the budget: return `isError` with text `BLOCKED: the browser did not answer within <n>s; the action may still complete`, the probe's windows, and the hints `ie_windows` and `ie_close force=true`. The stuck worker keeps the lock until its call returns.
  - **`handle()` routes every `tools/call` through `run_tool`** and waits for its result. **`serve()`** only adds threading: each `tools/call` is handled on its own thread, and responses are written under a stdout lock. Responses may come back out of order, each with its request `id`.
  - **`protocol.MODAL_PROBE: Callable[[], list[dict]]`:** defaults to `lambda: []`; Task 7 replaces it. After every tool call, if the probe returns windows, a text item is appended: `NOTICE: <kind> open: <title> (hwnd <n>). Use ie_windows; act in it with target="hwnd:<n>" or ie_switch_window.`
  - **`protocol.POST_HOOKS: list[Callable[[ToolSpec, dict, dict], None]]`:** empty here, used by Task 12. They run after a successful driver tool, in a sub-thread the worker starts, **while the lock is held**, only when `MODAL_PROBE()` is empty and `args.get("mode") != "async"`.
    - They run in one sub-thread (which enters `CALL_CONTEXTS` itself) and that sub-thread releases `DRIVER_LOCK` when the hooks return. The worker waits for it for at most 3 s, then carries on with `AFTER_CALL` and the notices whether or not the hooks have finished.
    - So the tool's result is returned after the hooks finish or after about 3 s, and the lock stays held until the hooks have returned.
    - Exceptions are logged and swallowed; the tool's own result is never changed.
  - **`protocol.AFTER_CALL: list[Callable[[ToolSpec, dict, dict], None]]`:** empty here; Task 12 adds `dialog_drain`. They run after **every** tool call (driver or not), **without** the driver lock, before the notices are appended. The worker starts each in its own sub-thread, which enters `CALL_CONTEXTS` itself and builds its own `ComBackend`s, and joins it for 2 s; exceptions are logged and swallowed.
  - **`needs_driver=False` tools:** `ie_sleep`, `ie_browsers`, `ie_kill_orphans`. `ie_status` too: it adds `title`/`url` only if `DRIVER_LOCK.acquire(timeout=0.5)` succeeds, else `"driver": "busy"`.
  - **Settings:** `tool_timeout: float` (`IE_TOOL_TIMEOUT`, default 45); `click_mode: str` (`IE_CLICK_MODE`, `driver`|`async`, default from D1 for the active interaction mode).
  - **`ie_click` gains `mode`** (`driver`|`async`, default `config.SETTINGS.click_mode`). `async` runs `execute_script("var e=arguments[0];window.setTimeout(function(){e.click();},0);return 'queued';", el)`. `double`/`right` actions always use the driver. The tool description states that an async click is script-initiated: no mousedown/mouseup/focus, and (per D8) the pop-up blocker may block `window.open`.

- [ ] **Step 1: Write the failing tests**

```python
def test_slow_tool_times_out_with_blocked_message(self)     # needs_driver tool sleeps 2s, IE_TOOL_TIMEOUT 0.5 -> isError; text startswith "BLOCKED:";
                                                            # "may still complete", "ie_windows" and "force=true" in text
def test_declared_budget_extends_timeout(self)              # tool with budget lambda a: 3, sleeps 1s, IE_TOOL_TIMEOUT 0.5 -> not isError
def test_busy_when_lock_held_by_running_tool(self)          # first tool holds lock 2s; second driver tool with budget 0.3 -> text startswith "BUSY:"
def test_non_driver_tool_runs_while_driver_tool_is_stuck(self)  # needs_driver=False tool returns within 0.5s while the lock is held
def test_force_close_works_while_driver_locked(self)        # stuck driver tool holds lock; ie_close force=True returns within 1s; kill_own_processes called
def test_lock_released_after_stuck_tool_finishes(self)
def test_handle_goes_through_watchdog(self)                 # handle() on a slow needs_driver tool with IE_TOOL_TIMEOUT 0.3 returns BLOCKED within 1s
def test_responses_carry_request_ids_when_out_of_order(self)  # serve() with ids 1 (slow) and 2 (fast) -> output line for id 2 precedes id 1; both present
def test_call_contexts_wrap_the_whole_call(self)            # recording context manager entered before fn and exited after hooks
def test_notice_appended_when_modal_open(self)              # MODAL_PROBE -> [{"kind":"html_dialog","title":"Lookup","hwnd":42}]: last content text contains "hwnd 42"
def test_post_hooks_skipped_when_modal_open_or_async(self)  # hook not called when probe non-empty, nor when args mode == "async"
def test_slow_hook_keeps_lock_but_returns_result(self)      # hook sleeps 5s -> result returned within ~3.5s; DRIVER_LOCK still held until the hook ends
def test_post_hook_failure_does_not_change_result(self)
def test_after_call_hooks_run_without_lock(self)          # hook sees DRIVER_LOCK unlocked; runs for needs_driver=False tools too; a 5 s hook -> result within ~2.5 s
def test_hook_threads_enter_call_contexts(self)           # recording context manager is entered in the POST_HOOKS and AFTER_CALL sub-threads, not only in the worker
def test_status_does_not_wait_for_busy_driver(self)         # lock held -> ie_status returns within 1s with "driver": "busy"
def test_async_click_uses_settimeout_script(self)           # fake driver: execute_script script contains "setTimeout"; element.click not called
def test_driver_click_mode_calls_element_click(self)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_watchdog tests.test_click_mode -v`
Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the unit suite**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: tool watchdog with budgets, driver lock, async click and modal notices"
```

---

### Task 7: Win32 layer — processes, windows, HTML documents

**Files:**
- Modify: `ie_mcp/win32/processes.py` (add snapshot and process tree)
- Create: `ie_mcp/win32/windows.py` (ctypes), `ie_mcp/win32/htmldoc.py` (pywin32)
- Modify: `ie_mcp/protocol.py` (install the real `MODAL_PROBE`; append `com_apartment` to `CALL_CONTEXTS` when pywin32 imports), `ie_mcp/session.py` (record `main_hwnd`)
- Test: `tests/test_win32_windows.py`

**Interfaces:**
- Consumes: D6 and D7, plus Q8's alert and confirm shapes (class and button IDs), the prompt's class, and the owner of an alert raised inside a dialog (Task 2); `SESSION.edge_pid`; `CALL_CONTEXTS`, `MODAL_PROBE` (Task 6).
- Produces:
  - **`ProcessRow(pid: int, ppid: int, exe: str)`.**
    - `processes.snapshot() -> list[ProcessRow]` uses `CreateToolhelp32Snapshot`/`Process32FirstW`/`Process32NextW`, cached for 2 s.
    - `processes.process_tree(root_pid: int, rows: list[ProcessRow] | None = None) -> set[int]`. Used for listing only, never for killing.
  - **`WindowInfo` dataclass:** `hwnd: int, pid: int, cls: str, title: str, kind: str, owner_hwnd: int | None, visible: bool, url: str`. `url` is `""` unless the caller asks for it (`with_url=True`, which only `ie_windows` does, in the worker, through `com_call`); `modal_probe` never reads it, so the probe never touches COM. `kind` is one of `"main"`, `"popup"`, `"html_dialog"`, `"script_dialog"`, `"system_dialog"`, `"other"`.
  - **`classify(cls: str, has_ie_server_child: bool, owner_in_session: bool, hwnd: int, main_hwnd: int | None) -> str`:**
    - `Internet Explorer_TridentDlgFrame` → `html_dialog`.
    - The alert class recorded by Q8 (expected `#32770`), owned by a session window (main, popup or `html_dialog`) or by an `ATTACHED_TOPS` window directly or through the owner chain → `script_dialog`. `classify` only sees the `owner_in_session` flag; `enumerate_windows` computes it by walking the owner chain. This is a *candidate*: Task 9 confirms it before acting.
    - Any other `#32770` → `system_dialog`.
    - A top window with an `Internet Explorer_Server` descendant → `main` if `hwnd == main_hwnd`, else `popup`.
    - Apply corrections recorded in D6. If Q8 found that `prompt` opens an `Internet Explorer_TridentDlgFrame`, D6 records what tells it apart from a `showModalDialog` window, and `classify` gains the parameter it needs.
  - **`enumerate_windows(pids: set[int], enum_fn=None, with_url: bool = False, attached_tops: set[int] | None = None) -> list[WindowInfo]`.** With `attached_tops`, it also returns windows whose owner chain reaches one of those hwnds. The default uses `EnumWindows`/`EnumChildWindows`/`GetClassNameW`/`GetWindowTextW`/`GetWindowThreadProcessId`/`GetWindow(GW_OWNER)`. `enum_fn` is injectable for tests.
  - **`session_window_pids() -> set[int]`:** per D6, `process_tree(SESSION.edge_pid)` or the owner-chain PIDs. Empty without a session.
  - **`ATTACHED_TOPS: set[int]`:** top-level hwnds of attached tabs (Task 15 fills it). For an attached tab, only windows whose owner chain reaches one of these hwnds count, never the rest of the user's Edge process tree.
  - **`modal_probe() -> list[dict]`:** the `html_dialog`, `script_dialog` and `system_dialog` windows in `session_window_pids()`, plus those owned (directly or through the owner chain) by an `ATTACHED_TOPS` window, as dicts.
  - **`IeSession.main_hwnd`:** set after attach. Before building the driver, record the hwnds of existing top windows with an IE server descendant. After attach, `main_hwnd` is the new one; if several are new, the one whose title starts with `driver.title`.
  - **`htmldoc.com_apartment()`:** a context manager calling `pythoncom.CoInitialize()`/`CoUninitialize()`.
  - **`htmldoc.ie_server_child(hwnd: int) -> int | None`.**
  - **`htmldoc.document_from_hwnd(hwnd: int, timeout_ms: int = 1000)`:** returns the COM `IHTMLDocument2` via `RegisterWindowMessage("WM_HTML_GETOBJECT")`, `SendMessageTimeout(..., SMTO_ABORTIFHUNG)` and `pythoncom.ObjectFromLresult`. It must be called inside the calling thread's apartment. Without pywin32 it raises `RuntimeError` naming `pip install "ie-mcp[win32]"`.
  - **`htmldoc.com_call(fn, *args)`:** calls `fn(*args)`, retrying `RPC_E_CALL_REJECTED` and `RPC_E_SERVERCALL_RETRYLATER` every 100 ms for up to 1 s (per D7), then re-raising. `document_from_hwnd` and every `ComBackend` COM access go through it.

- [ ] **Step 1: Write the failing tests** (fake enumeration and process data; no real windows)

```python
def test_process_tree_walks_children(self)            # rows 1->2->3, 4 unrelated -> {1,2,3}
def test_classify_trident_dialog_frame_as_html_dialog(self)
def test_classify_owned_alert_class_as_script_dialog(self)
def test_classify_alert_owned_by_html_dialog(self)      # alert class owned by a session html_dialog -> "script_dialog"
def test_enumerate_classifies_alert_owned_through_attached_chain(self)  # fake enum_fn, attached_tops={50}: an alert-class window owned by 50, and one owned by a window owned by 50 -> both listed, kind "script_dialog"
def test_enumerate_includes_attached_top_dialogs(self)  # attached_tops={50}: a dialog owned by 50 is listed although its pid is not a session pid
def test_probe_never_reads_url(self)                    # modal_probe() with a fake COM layer -> no COM call made; every url == ""
def test_failed_url_read_leaves_empty(self)             # with_url=True, COM raises for one window -> that window's url == "", the others are read
def test_classify_unowned_32770_as_system_dialog(self) # owner not in session -> "system_dialog"
def test_main_vs_popup_by_main_hwnd(self)              # hwnd == main_hwnd -> "main"; other IE window -> "popup"
def test_main_hwnd_is_the_new_ie_window(self)          # windows before {10}, after {10, 20} -> main_hwnd 20
def test_enumerate_filters_to_session_pids(self)       # windows of pid 9 excluded when pids={1,2}
def test_modal_probe_empty_without_session(self)
def test_document_from_hwnd_without_pywin32_names_extra(self)  # patch import to fail -> RuntimeError mentioning 'ie-mcp[win32]'
def test_com_call_retries_rejected_calls(self)                  # fake raises RPC_E_CALL_REJECTED, then RPC_E_SERVERCALL_RETRYLATER, then succeeds -> result returned; still failing after 1 s -> raises
def test_com_apartment_registered_in_call_contexts(self)       # with fake pythoncom importable -> com_apartment in CALL_CONTEXTS
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_win32_windows -v`
Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the unit suite**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: Win32 process and window layer, modal probe and HTML document access"
```

---

### Task 8: Backends, the `target` argument and the integration runner

**Files:**
- Create: `ie_mcp/backends.py`, `scripts/run_integration.py`, `tests/integration/__init__.py`, `tests/integration/base.py`
- Modify: `ie_mcp/tools/read.py`, `ie_mcp/tools/interact.py`
- Test: `tests/test_backends.py`, `tests/test_run_integration.py`, `tests/integration/it_read_interact.py`

**Interfaces:**
- Consumes: `document_from_hwnd`, `ie_server_child` (Task 7); `DRIVER_LOCK`, `run_tool`, `CALL_CONTEXTS` (Task 6); `js.load` (Task 3); D2 and D7 (Task 2).
- Produces:
  - **`Backend` protocol:** `html(frame) -> str`, `text(frame) -> str`, `js(script: str, frame, args: list) -> object`, `find(by: str, value: str, frame, timeout: float) -> object`, `click(el, action: str, mode: str) -> None`, `fill(el, text: str, clear: bool) -> None`, `select(el, how: str, val: str) -> str`, `get(el, attr: str) -> str`, `press_key(el | None, key: str, frame) -> None`, `hover(el) -> None`, `scroll(frame, el | None, to: str | None, dx: int, dy: int) -> str`, `grid(frame, table_id, headers, limit) -> dict`, `frames() -> list[dict]`, `refresh() -> None`.
  - **`DriverBackend`** holds the existing Selenium logic.
  - **`ComBackend(hwnd: int, kind: str = "hwnd")`** is created by `resolve_backend` per tool call (or per hook thread). It obtains its document on first use, from the window `server_hwnd()` returns, and caches it on the instance; the instance never leaves the thread that created it, so no COM object outlives that thread.
    - **`refresh()`** drops the cached document. Wait loops (`ie_wait_text`, `ie_wait_element`, and `find` with a timeout) call `backend.refresh()` before every poll, so a navigation during the wait is seen. `DriverBackend.refresh()` does nothing.
    - **`server_hwnd() -> int`:** for `kind="hwnd"`, `ie_server_child(hwnd)`. For `kind="tab"`, `hwnd` itself while it is still an `Internet Explorer_Server` window; otherwise the `Internet Explorer_Server` descendant of the nearest live ancestor in `backends.ATTACHED_CHAINS[hwnd]` (the visible one if there are several). If there is none, it raises `RuntimeError("the attached tab is gone; call ie_tabs and ie_attach again")`.
    - **Frames:** resolved through `doc.frames(i).document`.
    - **Locators:** `id`, `name`, `tag`, plus `css` only when `doc.documentMode >= 8`. `xpath` raises `ValueError("xpath is not supported for COM targets")`.
    - **`js`:** embeds the agent's script as an ES3 string literal `S` and runs `try{window.__iemcp_r=eval('(function(){'+S+'})')();window.__iemcp_e=''}catch(x){window.__iemcp_e=String(x.message||x)}`, so syntax errors are caught too and never reach the page's `onerror`. That wrapper is executed per D7 (`execScript`, `eval` or a script element; a script element is removed again right after it runs, so repeated drains leave no trace in the DOM), and the result is read back with `getattr(win, "__iemcp_r")`/`getattr(win, "__iemcp_e")`. Never use attribute syntax inside a class: Python mangles `__names`. A non-empty `__iemcp_e` raises `RuntimeError`.
    - **`js` arguments:** `args` may contain only str/int/float/bool/None. They are embedded as ES3 literals and exposed as `arguments[i]`; elements raise `ValueError`. Results that are not primitives are converted in-page: arrays via `join('\x1e')`, other objects via `String()`.
    - **`click`:** `action="click"` defaults to a `setTimeout` click. `double`/`right` fire `ondblclick`/`oncontextmenu`.
    - **`fill`:** focuses the element, sets `value`, then fires `onkeydown`, `onkeypress`, `onkeyup`, `onchange`, `onblur` (feature-detect `fireEvent`/`dispatchEvent`).
    - **`press_key`:** focuses the element (or keeps the active element), then uses the D7 key path for the document's mode: the posted message sequence D7 chose, sent to the IE server window, or `fireEvent` with `keyCode` (old document modes only).
    - **`hover`:** fires `onmouseover` then `onmouseenter`.
    - Tool descriptions say COM input is synthetic.
    - Every COM access goes through `htmldoc.com_call` (Task 7).
  - **`backends.DEFAULT_TARGET: str | None = None`** lives in `backends` (no import of `protocol`, so there is no cycle). Task 15 sets it, and code reads `backends.DEFAULT_TARGET` at call time.
  - **`backends.ATTACHED_CHAINS: dict[int, list[int]] = {}`:** for each attached tab's `Internet Explorer_Server` hwnd, its ancestor hwnds, nearest first, ending at the top window. Task 15 fills it.
  - **`backends.target_kind(target: str | None) -> str`:** returns `"driver"`, `"hwnd"` or `"tab"` after applying `DEFAULT_TARGET`. It never touches COM and raises `ValueError` for unknown forms.
  - **`resolve_backend(target: str | None) -> Backend`:** `"driver"` → `DriverBackend`; `"hwnd:<int>"` → `ComBackend(n)`; `"tab:<int>"` → `ComBackend(n, kind="tab")`.
  - **Target-aware tools:** `ie_html`, `ie_text`, `ie_js`, `ie_click`, `ie_fill`, `ie_select`, `ie_get`, `ie_frames`, `ie_wait_element`, `ie_wait_text`, `ie_grid`, `ie_scroll`, `ie_press_key`, `ie_hover` take an optional `target`. They are registered with `needs_driver=lambda a: target_kind(a.get("target")) == "driver"`.
  - **`scripts/run_integration.py`:** `main(argv: list[str] | None = None, start_dir: str = "tests/integration") -> int`. It inserts the repository root into `sys.path`, sets `os.environ["IE_MCP_INTEGRATION"]="1"`, and discovers `start_dir` with pattern `it_*.py` (or `<argv[0]>.py`) and `top_level_dir` = the repository root, so `tests.integration.base` imports. It prints the unittest result and returns 1 if there were failures, errors **or skips**, else 0. The `__main__` guard calls `sys.exit(main())`.
  - **`tests/integration/base.py` `IntegrationTestCase`:**
    - skips unless `IE_MCP_INTEGRATION=1`;
    - `setUpClass` starts the fixture server on `IE_FIXTURE_HOST`;
    - `call(name, **args) -> dict` and `text(name, **args) -> str` go through `protocol.handle`;
    - `restart_session(**env)`: closes the session, sets the given env vars for the rest of the test (restored in `tearDown`) and rebuilds `config.SETTINGS`;
    - `tearDownClass` runs `ie_close`, then asserts that the session's recorded PIDs (with identities) are gone.

- [ ] **Step 1: Write the failing unit tests**

```python
def test_runner_fails_on_skip(self)                 # temp dir with it_x.py whose test calls skipTest -> main([], tmp) == 1; IE_MCP_INTEGRATION restored afterwards
def test_runner_passes_on_success(self)             # passing test -> 0
def test_resolve_backend_default_is_driver(self)
def test_resolve_backend_parses_hwnd_target(self)   # "hwnd:42" -> ComBackend with hwnd 42
def test_unknown_target_rejected(self)              # "frame:1" -> ValueError
def test_needs_driver_false_for_hwnd_target(self)   # REGISTRY["ie_text"].needs_driver({"target":"hwnd:1"}) is False; ({}) is True
def test_com_document_obtained_once_per_call(self)  # two non-waiting operations on one ComBackend -> document_from_hwnd called once
def test_com_wait_reobtains_document_each_poll(self)  # fake document_from_hwnd returns a doc without "Echo", then one with it -> ie_wait_text target="hwnd:1" text="Echo" succeeds; document_from_hwnd called at least twice
def test_tab_target_follows_new_ie_server(self)    # ATTACHED_CHAINS {5: [6, 7]}; hwnd 5 is gone and fake enum gives 6 one IE server child 8 -> ComBackend(5, kind="tab").server_hwnd() == 8; nothing live -> RuntimeError "ie_attach"
def test_com_find_by_id(self)                       # fake doc.getElementById("code") returned
def test_com_xpath_rejected(self)
def test_com_css_requires_document_mode_8(self)     # documentMode 7 -> ValueError mentioning "documentMode"
def test_com_js_embeds_primitive_args(self)         # js("return arguments[0]+arguments[1]", None, ["a", 2]) -> executed text contains '"a"' and '2'
def test_com_js_rejects_element_args(self)
def test_com_js_error_raises(self)                  # fake sets __iemcp_e "boom" -> RuntimeError("boom")
def test_com_js_embeds_script_as_literal(self)      # script containing quotes, backslashes and "</script>" -> executed text contains it only inside one string literal passed to eval
def test_com_fill_fires_event_sequence(self)        # fake element records fired events == ["onkeydown","onkeypress","onkeyup","onchange","onblur"]
def test_com_press_key_uses_d7_path(self)           # D7 keydown-keyup -> PostMessage WM_KEYDOWN then WM_KEYUP (no WM_CHAR); fireEvent path -> fireEvent("onkeypress")
```

- [ ] **Step 2: Write the integration tests** in `tests/integration/it_read_interact.py`

```python
def test_frameset_read_nested_frame(self)           # frameset.htm; ie_text frame="main/inner" contains "Inner frame"
def test_frame_tools_survive_frame_navigation(self) # click link "grid.htm" in frame "menu"; ie_wait_text "Gamma" frame="main"; ie_text frame="main" contains "Gamma"
def test_cross_origin_frame_does_not_break_tools(self)  # xorigin.htm; ie_frames lists 2 frames; ie_text frame="local" contains "Inner frame";
                                                    # frame="foreign" returns either text containing "Inner frame" or isError mentioning the frame; ie_status answers afterwards;
                                                    # ie_wait_ready reports cross_origin_frames >= 1 if D13 = yes, else 0
def test_fill_select_submit_form(self)              # form.htm: fill code=A1, select kind label "B", click send; ie_get id echo_code == "A1"; ie_get id echo_kind == "B"
def test_fill_and_get_non_ascii(self)               # charset.htm and utf8.htm: fill name="Müller – café"; ie_get value equals it exactly
def test_quirks_page_readable(self)                 # quirks.htm text contains "quirks ok"
```

- [ ] **Step 3: Run them to verify they fail**

Run: `python -m unittest tests.test_backends tests.test_run_integration -v`
Expected: FAIL. (The integration module is first run in Step 5, once the runner exists.)

- [ ] **Step 4: Implement** `backends.py`, the runner and the base class, and route the tools through `resolve_backend`.

- [ ] **Step 5: Run unit and integration suites**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`, then `python scripts/run_integration.py`
Expected: OK, no skips; no leftover IE-mode processes.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: driver and COM backends with per-call target; integration runner"
```

---

### Task 9: Window and dialog tools

**Files:**
- Create: `ie_mcp/tools/windows.py`
- Modify: `ie_mcp/tools/dialogs.py`
- Test: `tests/test_window_tools.py`, `tests/integration/it_modal.py`, `tests/integration/it_popups_alerts.py`

**Interfaces:**
- Consumes: `enumerate_windows`, `session_window_pids`, `WindowInfo`, `ATTACHED_TOPS` (Task 7); `resolve_backend` (Task 8); D1, D2, D8, D9 and Q8 (Task 2).
- Produces:
  - **`ie_windows`** (`needs_driver=False`): a JSON list of `WindowInfo` from `enumerate_windows(session_window_pids(), with_url=True, attached_tops=ATTACHED_TOPS)`, each with `target` (`"hwnd:<n>"`). A URL that cannot be read (for example a COM call rejected while an alert is open) is left as `""`; the tool carries on. Only when `MODAL_PROBE()` is empty and the driver lock is free does it add `driver_handle` (matched by title, then URL) and `current: bool`.
  - **`ie_switch_window(handle: str | None, title_contains: str | None, main: bool = False)`** → `driver.switch_to.window(...)`; returns the new title and URL. Implement it if D2 includes `driver-handles` or D9 = yes.
  - **`ie_close_window(target: str)`:**
    - refuses `main` windows ("use ie_close"), the top window of an attached tab ("use ie_detach"), and `system_dialog` windows ("system dialog: ask the user");
    - for `hwnd:` targets: `parentWindow.close()` over COM if D2 includes `com`, else Win32 `PostMessage(WM_CLOSE)`;
    - for driver handles: switch to it, close it, and switch back to main.
  - **`ie_dialog`** gains `target`.
    - **Window path.** For a `script_dialog` window, `action="text"` returns its static text (child `Static` controls); `accept`/`dismiss` send `WM_COMMAND` with the button IDs recorded in Q8. Before any of these, the window is confirmed: its owner is a session window (`main_hwnd`, a popup or an `html_dialog`) or its owner chain reaches an attached tab's top window, **and** its class and its exact set of button IDs match a shape Q8 recorded for `alert` or `confirm`. An alert raised inside a `showModalDialog` window is owned by that dialog, so it qualifies. A `system_dialog` window is refused with `isError` "system dialog: ask the user"; any other window that fails the confirmation (another kind, owner or shape, a `prompt` included) is refused with "hwnd:<n> is not a confirmed script dialog (<kind>)". Nothing is sent in either case, so a `prompt` is answered only through the driver alert. (Task 12 adds a matching `dialog-open` record to the result as extra evidence; it is not required.)
    - **With `target="hwnd:<n>"`** it does not need the driver. `ie_dialog` is registered with `needs_driver=lambda a: not str(a.get("target") or "").startswith("hwnd:")`, which ignores `DEFAULT_TARGET` (unlike Task 8's target-aware tools), and a `tab:` target is refused with `isError`. It skips the driver's alert and takes the window path for that window only. While attached to a tab (Task 15) the no-target form is refused because it needs the driver, so alerts there are handled with the `hwnd:` target that `ie_windows` lists.
    - **Without a target**, it tries the driver alert (`switch_to.alert`) first, then the window path for a `script_dialog` that `MODAL_PROBE` reports. When `MODAL_PROBE` lists an `html_dialog`, it skips the driver alert (that call can block while a modal dialog is open) and goes straight to the window path.
  - **The `NOTICE:` text** (Task 6) recommends the D2 path first: `ie_switch_window` or `target="hwnd:<n>"`.

- [ ] **Step 1: Write the failing unit tests**

```python
def test_windows_tool_lists_dialog_with_target(self)     # fake enumerate -> item kind "html_dialog", target "hwnd:42"
def test_windows_tool_works_while_driver_locked(self)
def test_windows_tool_skips_driver_while_modal_open(self)  # probe non-empty -> driver never called; no driver_handle fields
def test_close_window_hwnd_prefers_com_then_wm_close(self) # COM fails -> PostMessage(WM_CLOSE) called
def test_close_window_refuses_main(self)                 # kind main -> isError containing "ie_close"
def test_system_dialog_never_clicked(self)               # ie_dialog/ie_close_window on system_dialog -> isError containing "ask the user"; no message sent
def test_dialog_hwnd_target_needs_no_driver(self)       # REGISTRY["ie_dialog"].needs_driver({"target": "hwnd:1"}) is False; needs_driver({}) is True
def test_dialog_hwnd_target_refuses_unconfirmed(self)   # hwnd target owned by a window outside the session and attached tabs, or with button IDs unlike Q8's alert/confirm -> isError containing "not a confirmed script dialog"; no message sent
def test_dialog_skips_driver_alert_while_html_dialog_open(self)  # probe lists an html_dialog and an owned script_dialog -> switch_to.alert never called; the script_dialog is answered
```

- [ ] **Step 2: Write the integration tests.** Use the D2 path(s). If D2 is `both`, run the modal tests once per path with `subTest`.

```python
# it_modal.py
def test_lookup_dialog_roundtrip(self)            # click open_lookup (D1 mode) -> ie_windows has html_dialog within 10s -> act in dialog -> click pick_A1 -> dialog gone
                                                  # -> lookup_code == "A1", lookup_name == "Alpha"
def test_lookup_dialog_grid_readable(self)        # with the dialog open: ie_grid on the dialog returns 3 rows with codes A1, B2, C3
def test_lookup_dialog_key_enter(self)            # in the dialog: fill search "B", ie_press_key Enter on search -> search_count == "1"
def test_confirm_dialog_return_value(self)        # ask -> click yes in dialog -> answer == "yes"
def test_nested_modal(self)                       # open_lookup -> nested -> nested_ok -> nested_result == "nested-ok" -> pick_B2 -> lookup_code == "B2"
def test_alert_inside_dialog(self)                # open_lookup -> click dialog_alert -> ie_windows has a script_dialog owned by the html_dialog
                                                  # -> ie_dialog target="hwnd:<alert>" action=text == "Dialog alert" -> ie_dialog target="hwnd:<alert>" accept
                                                  # -> dialog_alert_result == "alerted" (read through the D2 path)
def test_driver_click_on_modal_opener_does_not_hang_server(self)  # ie_click mode="driver" on open_lookup; next ie_windows returns within IE_TOOL_TIMEOUT+5s listing the dialog;
                                                  # ie_close_window closes it; ie_get lookup_code works again
# it_popups_alerts.py
def test_alert_accept_and_text(self)              # do_alert -> ie_dialog action=text == "Hello alert" -> accept -> no script_dialog in ie_windows
def test_alert_survives_screenshot_and_js(self)   # do_alert -> ie_screenshot and ie_js "return 1" (both may report the alert) -> ie_dialog action=text still == "Hello alert"
def test_confirm_dismiss(self)                    # do_confirm -> dismiss -> confirm_result == "false"
def test_popup_window(self)                       # do_popup with mode "driver" (and "async" too if D8 = not blocked) -> ie_windows kind popup -> read "Popup window"
                                                  # via ie_switch_window (D9 = yes) or target hwnd -> close_me -> back to main
```

- [ ] **Step 3: Run them to verify they fail**

Run: `python -m unittest tests.test_window_tools -v`, then `python scripts/run_integration.py it_modal`, then `python scripts/run_integration.py it_popups_alerts`
Expected: FAIL.

- [ ] **Step 4: Implement** the tools.

- [ ] **Step 5: Run the commands from Step 3**

Expected: OK, no skips; no leftover processes.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: window, popup, alert and modal dialog tools"
```

---

### Task 10: Capture store and redaction

**Files:**
- Create: `ie_mcp/capture/__init__.py`, `ie_mcp/capture/redact.py`, `ie_mcp/capture/store.py`
- Modify: `ie_mcp/config.py`, `ie_mcp/session.py` (`log()` passes through the redactor)
- Test: `tests/test_redact.py`, `tests/test_capture_store.py`

**Interfaces:**
- Produces:
  - **`redact.name_segments(name: str) -> list[str]`:** lower-cased word segments, split on non-alphanumerics, camelCase boundaries and letter/digit boundaries. For example, `lastName` → `["last", "name"]`, `X-Auth-Token` → `["x", "auth", "token"]` and `password2` → `["password", "2"]`.
  - **`redact.is_secret_field(name: str, extra: set[str]) -> bool`:** true when any of these holds:
    - the normalised name (lower case, alphanumerics only) **contains** `password`, `passwd`, `passphrase` or `pwd` (catches `newpassword`, `USERPWD`, `TXTPASSWORD`);
    - the whole normalised name is one of `sid session sessionid jsessionid aspsessionid phpsessid auth key apikey csrf xsrf`, or is in `extra`;
    - any segment is one of `pass pin secret token apikey credential credentials otp` (catches `userPass` and `cardPin`; segment equality keeps `compass`, `bypass` and `spinner` visible).
  - **`redact.is_secret_header(name: str, extra: set[str]) -> bool`:** true for `cookie`, `set-cookie`, `authorization`, `proxy-authorization`, `extra`, or any header whose segments include `token`, `auth`, `key`, `csrf`, `xsrf`, `session`, `secret` or `password`.
  - **`Redactor(extra_fields: set[str], extra_headers: set[str], enabled: bool = True)`.** It is thread-safe and keeps `secret_values: set[str]`: redacted values of 4+ characters that are not trivial (not `true/false/null/undefined/on/off/yes/no`, case-insensitive, and not all digits shorter than 8). Values of 8+ characters are masked anywhere. Values of 4–7 characters are masked only as whole tokens (bounded by non-alphanumerics), so `test` inside `latest` stays. Each `Cookie`/`Set-Cookie` pair's value is tracked on its own. Methods:
    - `redact_headers(headers: list[tuple[str,str]]) -> list[tuple[str,str]]`: also applies `redact_url` to `Referer`, `Location` and `Content-Location` values;
    - `redact_url(url: str) -> str`: query-string fields and `;name=value` path parameters such as `;jsessionid=`;
    - `redact_body(content_type: str, body: bytes, charset: str | None) -> bytes`: form-urlencoded, multipart, JSON keys at any depth, XML element/attribute names;
    - `redact_text(text: str) -> str`: `name=value`, `name: value` and `"name":"value"` pairs, then `mask`;
    - `mask(text: str) -> str`: replaces every known secret value with `<redacted>`.

    Redacted values are recorded before masking. With `enabled=False` every method returns its input.
  - **`session.REDACTOR: Redactor`**, built from settings. `log()` passes each message through `REDACTOR.redact_text`.
  - **`NetworkEntry` dataclass:** `id: int, started: float, duration_ms: int, method: str, url: str, status: int | None, req_headers: list[tuple[str,str]], req_body: bytes, resp_headers: list[tuple[str,str]], resp_body: bytes, tunnel: bool, truncated: bool, error: str | None, source: str` (`"proxy"` or `"page"`), `referer_charset: str | None`.
  - **`CaptureStore(redactor: Redactor, max_entries: int = 2000, max_body: int = 262144, jsonl_path: Path | None = None)`** is thread-safe:
    - `add(entry) -> int` redacts the URL, the headers and the request body first (which records secrets), then the response body, then runs `mask` on all of them; it caps bodies and assigns the id;
    - `list(url_contains=None, method=None, status=None, since_id=0, limit=50) -> list[NetworkEntry]`;
    - `get(id) -> NetworkEntry | None`;
    - `clear() -> None`.

    JSONL is written only when `jsonl_path` is set (`IE_CAPTURE_JSONL=1`), after redaction.
  - **Settings:** `capture_redact`, `capture_redact_fields: set[str]`, `capture_redact_headers: set[str]`, `capture_max_body`, `capture_jsonl`.

- [ ] **Step 1: Write the failing tests**

```python
def test_redacts_password_field_in_form_body(self)     # "code=A1&pwd=s3cret" -> b"code=A1&pwd=<redacted>"
def test_redacts_query_string_field(self)              # "/x?user=a&token=abcd1234" -> "/x?user=a&token=<redacted>"
def test_redacts_json_and_xml_keys(self)               # {"a":{"password":"s3cret"}}, <pwd>s3cret</pwd>, <x password="s3cret"/> -> "s3cret" absent
def test_redacts_cookie_auth_and_custom_token_headers(self)  # Cookie, Authorization, X-Auth-Token, X-Api-Key values -> "<redacted>"; Content-Type unchanged
def test_redacts_referer_and_location_query(self)      # Referer "http://h/p?token=abcd1234" -> "token=<redacted>"
def test_ordinary_fields_not_redacted(self)            # lastName=Smith, compassHeading=N, keyword=abc, author=Ann, keyCode=13, bypassCache=1, spinner=on -> unchanged
def test_legacy_password_names_redacted(self)          # newpassword, USERPWD, TXTPASSWORD, password1, password2, userPass, cardPin -> all redacted
def test_short_values_masked_only_as_tokens(self)      # secret "test" recorded -> mask("latest test") == "latest <redacted>"
def test_jsessionid_path_parameter_redacted(self)      # "/app;jsessionid=ABCDEF123456?x=1" -> contains ";jsessionid=<redacted>"
def test_trivial_values_not_tracked(self)              # pwd=true, then mask("true and false") unchanged
def test_masks_redacted_value_in_response_body(self)   # entry with req pwd=s3cret and resp body '<dd id="echo_pwd">s3cret</dd>' -> resp_body has no "s3cret"
def test_redact_text_handles_pairs_and_known_values(self)  # "pwd=xyz9 sCode=A1" -> "pwd=<redacted> sCode=A1"; later "seen xyz9" -> "seen <redacted>"
def test_redaction_can_be_disabled(self)
def test_log_lines_are_redacted(self)                  # log("GET http://h/?token=abcd1234") -> log file text contains "token=<redacted>"
def test_store_ring_buffer_drops_oldest(self)          # max_entries=2, add 3 -> [e.id for e in list()] == [2, 3]
def test_store_filters_by_url_method_status(self)
def test_store_truncates_large_bodies(self)            # max_body=10, body 20 bytes -> len 10, truncated True
def test_jsonl_written_only_when_enabled(self)
def test_jsonl_contains_no_secret_values(self)         # "s3cret" not in jsonl text
def test_store_is_thread_safe(self)                    # 8 threads x 100 adds -> 800 unique ids
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_redact tests.test_capture_store -v`
Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the unit suite**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: capture store with token-based, value-tracking redaction"
```

---

### Task 11: Recording proxy

**Files:**
- Create: `ie_mcp/capture/proxy.py`
- Test: `tests/test_inject.py`, `tests/test_capture_proxy.py`

**Interfaces:**
- Consumes: `CaptureStore`, `NetworkEntry` (Task 10); D10 (Task 2).
- Produces:
  - **`loader_tag(doc_url: str) -> bytes`:** `<script type="text/javascript" src="http://<host:port of doc_url>/__iemcp__/i.js"></script>`. The URL is absolute, so a `<base href>` cannot redirect it. `i.js` first removes its own `<script>` element (the last script in the document while it runs), so the DOM the agent reads matches the original.
  - **`is_document_request(method: str, headers) -> bool`:** `GET`/`POST` with an `Accept` header containing `text/html`. If D10 = no, the proxy never injects (Task 13 then relies on the post-call hook). XHRs that send `Accept: text/html` and receive a full document do get the loader; the Fidelity table and README say so.
  - **`find_insertion_point(buf: bytes, eof: bool) -> int | NEVER | NEED_MORE`** (`NEVER` and `NEED_MORE` are module sentinels). `buf` is everything received so far (at most 64 KB); `eof` says whether the body is complete; the proxy calls it again as bytes arrive. It returns `NEVER` (do not inject) when:
    - the body starts with a UTF-16 BOM;
    - after an optional UTF-8 BOM, whitespace and comments, the first tag is not a doctype or `<html`, `<head`, `<body` or `<frameset` (case-insensitive) — a fragment or plain text;
    - there is no `<head` tag and the next tag after the anchor (below) is `<frameset` — such frameset documents are instrumented after load by the post-hook;
    - nothing is decided within 64 KB.

    It returns `NEED_MORE` while the answer could still change, unless `eof`: the buffer ends before the first tag is complete, inside the anchor tag, or inside or right after the skippable run. An offset is final only once the first tag **after** the skipped run has fully arrived (its name and closing `>`), so a stream split inside the run can never put the loader before an `X-UA-Compatible` meta.

    Otherwise it returns the offset:
    - the **anchor** is the end of the `<head …>` tag, else the end of the `<html …>` tag, else the end of the doctype. A document whose first tag is `<body` (no doctype, `<html` or `<head`) gets the loader right before `<body`; one whose first tag is `<frameset` gets `NEVER`;
    - from the anchor, skip whitespace, comments and `<meta …>`, `<title>…</title>` and `<base …>` elements, case-insensitive;
    - a tag name must be followed by whitespace, `/` or `>` (`<head` does not match `<header`), a `>` inside a quoted attribute value does not end a tag, and tags inside comments are ignored.
  - **`RecordingProxy(store: CaptureStore, host: str = "127.0.0.1", port: int = 0, instrument_js: bytes | None = None, upstream_proxy: tuple[str, int] | None = None, no_cache: bool = False)`** with `start() -> int`, `stop() -> None`, `seen: list[str]` (request URLs and CONNECT targets, in order, capped at 1000), `first_request: threading.Event`, and `doc_charsets` (URL → charset, the 1000 most recent).
    - **Proxy-answered path:** a request whose path starts with `/__iemcp__/` is answered by the proxy itself (`instrument_js`, `Cache-Control: no-store`) on any host. It is never forwarded and never recorded.
    - **Requests:** forwards absolute-form HTTP requests. Each client connection keeps its own upstream `http.client.HTTPConnection` per host:port for its lifetime, so connection-based Windows authentication keeps working.
    - **Headers:** hop-by-hop headers are dropped, except that `Proxy-Authorization` is forwarded when `upstream_proxy` is set, and `Proxy-Authenticate` is passed back. With `no_cache`, `If-Modified-Since`/`If-None-Match` are removed from requests.
    - **CONNECT:** tunnels the bytes and records an entry with `tunnel=True`.
    - **Streaming:** every response is streamed to the client as it arrives, while up to `max_body` bytes are copied into the entry.
    - **Injection** happens when `instrument_js` is set, `is_document_request` is true, and the response is `text/html`, status 200, not `Content-Disposition: attachment`, not HEAD, and not encoded. For such requests, `Accept-Encoding: identity` is sent upstream. The proxy then:
      - reads upstream with `read1()` and calls `find_insertion_point(buf, eof)` after each read, buffering while it returns `NEED_MORE` (at most 64 KB, after which it is `NEVER`);
      - inserts `loader_tag(<request URL>)` at the returned offset;
      - drops `ETag`/`Last-Modified`, sets `Cache-Control: no-store`, and either fixes `Content-Length` or re-chunks;
      - streams the rest.

      Every other response passes through byte-for-byte.
    - **Charsets:** for every `text/html` response, the charset from `Content-Type`, or else from a meta prescan of its first 2 KB, is stored in `doc_charsets[url]`. A later request whose `Referer` is in `doc_charsets` gets `entry.referer_charset`.

- [ ] **Step 1: Write the failing tests.** Proxy tests use `http.client` with absolute-form request lines (`conn.request("GET", "http://127.0.0.1:<p>/index.htm")` on a connection to the proxy), never urllib, so registry `ProxyOverride` and `NO_PROXY` cannot bypass it.

```python
# test_inject.py
def test_inject_after_leading_meta_title_base(self)   # <head><title>t</title><meta http-equiv="X-UA-Compatible" content="IE=7"><link ...> -> offset just before "<link"
def test_headless_document_keeps_xua_meta_first(self) # <!DOCTYPE …><html><meta http-equiv="X-UA-Compatible" content="IE=7"><title>x</title><body> -> offset just before "<body"
def test_never_inject_before_doctype_or_bom(self)     # b"\xef\xbb\xbf<!DOCTYPE html><html><head><script>" -> offset after "<head>", > len(BOM + doctype)
def test_uppercase_tags_recognised(self)              # <HTML><HEAD><TITLE>x</TITLE><SCRIPT> -> offset just before "<SCRIPT"
def test_fragment_never_injected(self)                # b"OK|A1", b"<tr><td>1</td></tr>", b"  <div>x</div>" (eof=True) -> NEVER
def test_utf16_never_injected(self)                   # body starting b"\xff\xfe" -> NEVER
def test_headless_frameset_not_injected(self)         # <html><frameset …> -> NEVER
def test_every_prefix_needs_more_or_final_answer(self)  # for each document above and every prefix length (eof=False): NEED_MORE, or the full-document answer
def test_body_first_and_frameset_first(self)          # b"<body>x</body>" -> offset 0 (before <body); b"<frameset cols='*'>" -> NEVER; both also in the every-prefix test
def test_tag_names_need_a_delimiter(self)             # <!DOCTYPE html><html><header>x</header> has no <head>: anchor after <html>, never inside <header>
def test_quoted_gt_and_comments_ignored(self)         # <meta content="a>b"> skipped as one tag; <!-- <head> --> ignored
# test_capture_proxy.py
def test_proxy_records_post_body_and_response(self)   # POST /echo code=A1 -> entry method "POST", status 200, b"code=A1" in req_body, b"echo_code" in resp_body
def test_proxy_returns_response_unchanged_without_injection(self)  # instrument_js None: body bytes equal to a direct fetch; end-to-end headers equal
def test_fragment_response_unchanged_with_injection_on(self)       # GET /fragment with Accept "text/html" -> body == b"OK|A1"; no Cache-Control change
def test_xhr_request_not_injected(self)               # POST /echo (a full document) with Accept "*/*" -> body unchanged byte-for-byte
def test_document_injected_and_not_cacheable(self)    # GET index.htm with Accept "text/html" -> the loader tag (absolute URL) present once at the expected offset; Cache-Control "no-store";
                                                      # no ETag/Last-Modified; Content-Length == len(body) or chunked
def test_loader_path_served_by_proxy(self)            # GET http://127.0.0.1:<p>/__iemcp__/i.js -> body == instrument_js; fixture server never saw the request; no store entry
def test_attachment_and_head_not_injected(self)
def test_connect_tunnel_recorded_as_tunnel(self)      # local TCP echo server via CONNECT -> entry.tunnel True; echoed bytes match
def test_client_connection_reuses_one_upstream_connection(self)  # two keep-alive requests on one client connection -> fixture server sees one TCP connection
def test_upstream_proxy_chaining_passes_proxy_auth(self)  # second RecordingProxy as upstream; Proxy-Authorization header reaches it; both record the request
def test_no_cache_strips_conditional_headers(self)    # no_cache=True: If-None-Match not received by the fixture server
def test_streams_large_response(self)                 # GET /big?ms=1500 -> first bytes arrive before 1000 ms; full 1 MB delivered; stored body capped at max_body
def test_progressive_head_injected_after_meta(self)    # GET /progressive?ms=800 with Accept text/html -> the head with the loader arrives before 500 ms; the loader sits after the X-UA meta, before <link
def test_referer_charset_recorded(self)               # fetch charset.htm, then POST /echo with Referer charset.htm -> entry.referer_charset == "windows-1252"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_inject tests.test_capture_proxy -v`
Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the unit suite**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: streaming recording proxy with document-only, mode-safe loader injection"
```

---

### Task 12: Page instrumentation and console tools

**Files:**
- Create: `ie_mcp/js/instrument.js`, `ie_mcp/js/hook.js`, `ie_mcp/js/drain.js`, `ie_mcp/capture/console.py`, `ie_mcp/tools/console.py`
- Modify: `ie_mcp/protocol.py` (register the post-hook and the dialog drain), `ie_mcp/tools/dialogs.py`, `ie_mcp/config.py`
- Test: `tests/test_console.py`, `tests/integration/it_console.py`

**Interfaces:**
- Consumes: `POST_HOOKS`, `AFTER_CALL`, `MODAL_PROBE` (Task 6); `ComBackend`, `backends.DEFAULT_TARGET` (Task 8); `Redactor`, `CaptureStore` (Task 10); D3 and D5 (Task 2).
- Produces:
  - **`instrument.js`** is ES3 and idempotent through `window.__iemcp`. It keeps a queue `window.__iemcp.q` of records `kind\x1flevel\x1ftime\x1ftext` (separators inside text escaped as `\\x1e`/`\\x1f`). It records:
    - **`error`:** from `window.onerror`, chained to any existing handler, keeping its return value. The wrapper carries the marker `__iemcp = 1`; if the page later replaces `window.onerror`, the post-hook re-chains it;
    - **`dialog-open`** (pushed before calling the original, so it exists while the dialog is open) and **`dialog`** (pushed after it returns, with the result): `alert`/`confirm`/`prompt` and their text;
    - **`modal`:** `showModalDialog` URL, features, a one-level summary of `dialogArguments` (string/number/boolean own properties, as `name=value` pairs) and the return value;
    - **`open`:** `window.open` URL, name and features;
    - **`console`:** only if `window.console` already exists, wrapping only the methods that exist. With `window.__iemcp_stub` true (`IE_CONSOLE_STUB=1`), it first creates a complete no-op console (`log info warn error debug dir dirxml group groupCollapsed groupEnd time timeEnd count assert trace table clear profile profileEnd`);
    - **`xhr`:** only when `location.protocol == "https:"` or `window.__iemcp_wrap_xhr` is true. The flag is true when capture is off (`IE_CAPTURE=0`, `capture_active is False`, or D3 = `instrument-only`). It wraps native `XMLHttpRequest` `open`/`send`/`onreadystatechange`, recording method, URL, request body, status and response length. ActiveX `XMLHTTP` objects are never wrapped.

    It calls the original host functions directly, branching on `arguments.length`. Popups forward their records to `opener.__iemcp.q` when same-origin.
  - **`hook.js`** takes `arguments[0]` = the `instrument.js` source and `arguments[1]` = flags (`stub`, `wrap_xhr`). Run once from the top document, it walks every same-origin frame in JS. In each document it:
    - sets the flags;
    - injects the source where `typeof window.__iemcp == 'undefined'`, with `window.execScript` where it exists (old modes), else a `script` element with `text` that is removed again right after it runs, so `ie_html` shows the original DOM;
    - re-chains `onerror` if needed;
    - drains the queue.

    It returns all records joined with `\x1e`, each prefixed with its frame path and `\x1f`. `drain.js` drains one document only, for COM targets.
  - **`ConsoleRecord` dataclass** `id: int, kind: str, level: str, time: float, text: str, where: str` (`where` = window target and frame path).
    - `parse_records(raw: str, where: str) -> list[ConsoleRecord]`.
    - `ConsoleStore(redactor: Redactor, max_records: int = 5000)` with `add_many` (applies `redactor.redact_text` to each text), `list(since_id=0, kinds=None, limit=100)`, `clear`, and `find_dialog(text: str, within_s: float = 30) -> ConsoleRecord | None`.
    - `xhr` records are also added to the `CaptureStore` as `NetworkEntry(source="page")`.
  - **`instrument_hook`, registered in `POST_HOOKS`:** a single `execute_script(hook.js, source, flags)` on the top document, then parse into the `ConsoleStore`.
  - **`dialog_drain`, registered in `AFTER_CALL` (Task 6):**
    - for each `html_dialog` that `MODAL_PROBE` reports, it injects (if missing) and drains through `ComBackend(hwnd)`;
    - for each `script_dialog`, it drains the document of the owner window through COM. This is best effort: a rejected call is skipped;
    - while `backends.DEFAULT_TARGET` is a `tab:` target (Task 15), it also injects (if missing) and drains that tab's document.

    Records of a dialog that closes between two calls are lost; the README says so.
  - **`ie_dialog`** (Task 9) adds the matching `dialog-open` record, found with `ConsoleStore.find_dialog(<the window's static text>)`, to its result as extra evidence. Confirmation itself stays as Task 9 defines it.
  - **Settings:** `instrument: bool` (`IE_INSTRUMENT`), `console_stub: bool` (`IE_CONSOLE_STUB`).
  - **Tools** (`needs_driver=False`): `ie_console(since_id=0, kinds=None, limit=100)` and `ie_console_clear()`.
  - **`ie_status`** adds `instrumented`, `console_stub` and `xhr_wrapped`.

- [ ] **Step 1: Write the failing unit tests**

```python
def test_parse_records_splits_fields_and_unescapes(self)  # "0\x1ferror\x1ferror\x1f1.5\x1fboom \\x1f x" -> where "0", text "boom \x1f x"
def test_store_since_id_and_kind_filter(self)
def test_console_text_is_redacted(self)                   # "login pwd=xyz9" -> stored "login pwd=<redacted>"
def test_xhr_records_become_page_network_entries(self)    # xhr record -> CaptureStore entry source "page", method/url parsed
def test_hook_is_one_driver_call(self)                    # fake driver: instrument_hook makes exactly one execute_script call
def test_dialog_drain_uses_com_backend(self)              # probe returns html_dialog hwnd 7 -> ComBackend(7).js called with drain.js
def test_dialog_drain_drains_attached_tab(self)           # backends.DEFAULT_TARGET "tab:9" -> ComBackend(9).js called with drain.js
def test_find_dialog_matches_text_within_window(self)     # record "Hello alert" 5s ago -> found; 60s ago -> None
```

- [ ] **Step 2: Write the integration tests**

```python
def test_late_error_and_console_captured(self)        # errors.htm, click late_error -> error record containing "late-error";
                                                      # console "fixture-log" recorded only if D5 says console exists in that mode
def test_alert_recorded(self)                         # dialogs.htm do_alert -> ie_dialog accept -> ie_console has dialog-open and dialog records for "Hello alert"
def test_modal_call_recorded_with_arguments(self)     # modal flow -> modal record containing "modal_dialog.htm" and "sCode="
def test_dialog_records_drained(self)                 # with lookup dialog open, ie_console contains records with where startswith "hwnd:"
def test_instrumentation_works_in_quirks_and_ie7(self)  # quirks.htm and form.htm -> ie_js "return typeof window.__iemcp" == "object"
def test_no_console_created_by_default(self)          # on a page where D5 says console is absent: ie_js "return typeof window.console" == "undefined"
```

(Load-time errors need the proxy loader; Task 13 tests them.)

- [ ] **Step 3: Run them to verify they fail**

Run: `python -m unittest tests.test_console -v`, then `python scripts/run_integration.py it_console`
Expected: FAIL.

- [ ] **Step 4: Implement.**

- [ ] **Step 5: Run unit and integration suites**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`, then `python scripts/run_integration.py`
Expected: OK, no skips.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: minimal ES3 page instrumentation and console tools"
```

---

### Task 13: Capture wiring, network tools and early injection

**Files:**
- Create: `ie_mcp/tools/network.py`
- Modify: `ie_mcp/session.py` (start the proxy before the driver, verify it, stop it in `close`), `ie_mcp/config.py`, `ie_mcp/tools/diagnostics.py` (`ie_status`), `ie_mcp/tools/navigation.py` (deferred verification)
- Test: `tests/test_network_tools.py`, `tests/integration/it_network.py`

**Interfaces:**
- Consumes: `RecordingProxy` (Task 11); `CaptureStore`, `Redactor` (Task 10); `instrument.js` (Task 12); `build_options(proxy_port=…, bypass=…)` (Task 4); D3, D4, D10, D11, D12 (Task 2).
- Produces:
  - **Defaults:** `IE_CAPTURE` defaults to `1`, or to `0` if D12 = yes. `IE_CAPTURE_BYPASS` (comma-separated host patterns) is passed as `bypass` to `build_options`.
  - **Session start when capture is on and D3 = `proxy`:**
    - start `RecordingProxy`, with `instrument_js` = flags + `instrument.js` when `IE_INSTRUMENT=1` and D10 = yes, plus `upstream_proxy` and `no_cache` from settings;
    - build options with `proxy_port` and `bypass`.
  - **`IeSession.capture_active: bool | None`:** `None` = not verified yet, `True` = verified, `False` = the browser did not use the proxy.
    - **Verification:** after the first `ie_open`/`ie_goto` to an `http(s)` URL whose host is not in `IE_CAPTURE_BYPASS`, wait up to 5 s for `proxy.seen` to contain that URL. Compare after normalising scheme and host case, default ports and the fragment; for https, look for a CONNECT to its host:port.
    - If it doesn't, set `False` and append `NOTICE: network capture is off: the browser did not use the proxy` to that tool result.
    - `about:` and `file:` URLs leave it `None`.
  - **Zone notice.** At start, read (read-only) `ProxyOverride` from `HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings`. After `ie_open`/`ie_goto`, append a notice when all of these hold:
    - capture is active;
    - the host matches a `ProxyOverride` entry, or is dotless while `<local>` is listed;
    - the host is not in `IE_CAPTURE_BYPASS`.

    The notice: `NOTICE: <host> normally bypasses your proxy. Through the capture proxy, IE may put it in another security zone (different document mode, Protected Mode, logon). If the page behaves differently, set IE_CAPTURE_BYPASS=<host>.`
  - **When D3 = `instrument-only`:** no proxy, `capture_active = False`, and instrumentation sets `__iemcp_wrap_xhr = true`.
  - **`ie_status`** adds:
    - `capture` (`"proxy"`, `"page-only"`, `"off"`, `"unverified"`);
    - `capture_note`: the D11 paths not captured (e.g. "popup traffic is not captured"), plus "the proxy can change the IE security zone of intranet hosts; use IE_CAPTURE_BYPASS or IE_CAPTURE=0 if a page behaves differently";
    - `modifications`: the list of active Fidelity-table rows.
  - **Tools** (`needs_driver=False`):
    - `ie_network(url_contains=None, method=None, status=None, since_id=0, limit=50)`: one line per entry, `id source method status duration_ms size url`;
    - `ie_request(id: int, body_limit: int = 20000)`: headers and bodies decoded with the charset from `Content-Type`, then `referer_charset`, then `cp1252`. Binary bodies are summarised as `<binary N bytes>`;
    - `ie_network_clear()`.

    Results say "HTTPS: tunnel only (host, timing)" when tunnel entries are present, and "responses served from the browser cache are not seen (IE_CAPTURE_NO_CACHE=1 turns revalidations into full fetches; fresh cache hits stay invisible)" when the store is empty after a navigation.
  - **`IeSession.close()`** also stops the proxy and deletes the JSONL file unless `IE_KEEP_ARTIFACTS=1`. Screenshots saved by name are kept.

- [ ] **Step 1: Write the failing unit tests**

```python
def test_request_body_decoded_with_page_charset(self)   # entry with Content-Type form, referer_charset "windows-1252", body b"name=M%FCller" -> ie_request text contains "Müller"
def test_utf8_body_decoded(self)                         # body "name=M%C3%BCller", Content-Type charset=utf-8 -> "Müller"
def test_network_tool_line_format(self)                  # one fake entry -> "1 proxy POST 200 12 345 http://…/echo"
def test_capture_unverified_for_about_blank(self)        # ie_open with no URL -> capture_active None, ie_status capture "unverified"
def test_capture_off_when_url_not_seen(self)             # fake proxy.seen empty after http open -> capture_active False; NOTICE in result
def test_status_lists_modifications(self)                # instrument on, capture proxy -> modifications contains the loader row if D10 = yes, and never if D10 = no
def test_jsonl_deleted_on_close_unless_kept(self)
def test_zone_notice_for_proxy_override_host(self)      # ProxyOverride "*.corp.example;<local>" -> ie_goto http://app.corp.example/ and http://intranet/ get the NOTICE; http://example.org/ does not
def test_verification_normalises_urls(self)            # opened "HTTP://Host:80/a#x", proxy saw "http://host/a" -> capture_active True
```

- [ ] **Step 2: Write the integration tests.** If D10 = no, the proxy never injects: `test_load_time_error_captured` becomes `test_late_error_captured_without_loader` (click `late_error`, expect the error record), and `test_document_mode_unchanged_by_injection` checks that modes match Q6 (a). If D3 = `instrument-only`, write only `test_native_xhr_recorded_as_page_entry`, `test_xhr_fragment_identical_with_capture_on_and_off` and `test_document_mode_unchanged_by_injection` (the latter with instrumentation injected after load), and state in the README that form posts and document loads are not captured.

```python
def test_form_post_captured_with_redaction(self)     # form.htm fill code=A1, pwd=s3cret, submit -> ie_network has POST …/echo;
                                                     # ie_request shows "code=A1"; "s3cret" absent from the request AND the echoed response
def test_activex_xhr_captured(self)                  # xhr.htm load -> POST /echo with body "q=ping"
def test_xhr_fragment_identical_with_capture_on_and_off(self)  # xhr.htm load_fragment: fragment_result == "OK|A1" and xhr_result identical,
                                                     # first with capture on, then restart_session(IE_CAPTURE="0")
def test_native_xhr_recorded_as_page_entry(self)     # restart_session(IE_CAPTURE="0") -> xhr.htm load_native -> entry source "page"
def test_dialog_document_requests_captured(self)     # modal flow -> GET modal_dialog.htm and GET /api/lookup.xml?q= present (only the paths D11 says are proxied)
def test_load_time_error_captured(self)              # errors.htm -> ie_console error record containing "undefinedFunctionCall"
def test_document_mode_unchanged_by_injection(self)  # ie_js "return document.documentMode" on index.htm, form.htm, quirks.htm, headless.htm, nomode.htm
                                                     # == the values the spike recorded for Q6 (c) (expected 11, 7, 5, 7, and nomode per D12)
def test_late_charset_page_decodes(self)             # latecharset.htm: ie_text contains "Müller – café"; ie_js "return document.charset" matches Q6
def test_request_body_decoded_with_page_charset_live(self)  # charset.htm submit name="Müller – café" -> ie_request text contains it
```

- [ ] **Step 3: Run them to verify they fail**

Run: `python -m unittest tests.test_network_tools -v`, then `python scripts/run_integration.py it_network`
Expected: FAIL.

- [ ] **Step 4: Implement.**

- [ ] **Step 5: Run unit and integration suites**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`, then `python scripts/run_integration.py`
Expected: OK, no skips.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: verified per-process network capture, network tools and early instrumentation"
```

---

### Task 14: Window screenshots (rendered view including dialogs)

**Files:**
- Create: `ie_mcp/png.py`, `ie_mcp/win32/capture.py`
- Modify: `ie_mcp/tools/read.py` (`ie_screenshot`), `ie_mcp/cli.py` (call `enable_dpi_awareness()` at start-up, before any window call)
- Test: `tests/test_png.py`, `tests/integration/it_screenshot.py`

**Interfaces:**
- Consumes: `enumerate_windows`, `modal_probe`, `SESSION.main_hwnd`, `ATTACHED_TOPS` (Task 7).
- Produces:
  - **`png.encode_png(width: int, height: int, rgba: bytes) -> bytes`:** 8-bit RGBA, filter 0, stdlib `zlib`/`struct` only.
  - **`win32.capture.enable_dpi_awareness() -> bool`:** `SetProcessDpiAwarenessContext` declared with `argtypes = [ctypes.c_void_p]`, called with `ctypes.c_void_p(-4)` (per-monitor v2). It logs and returns `False` on failure.
  - **`win32.capture.capture_window(hwnd: int) -> tuple[int, int, bytes]`:** `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT=2)` into a 32-bit DIB section, converted from BGRA to RGBA.
  - **`ie_screenshot` gains `scope`:**
    - `page` (default, driver);
    - `element` (driver, `by`/`value`);
    - `window` (Win32: `main_hwnd`, or the attached tab's top window while attached, plus every open `html_dialog`/`script_dialog`, one image each, in z-order; `needs_driver=False`).

    A `target` of `hwnd:<n>` captures that window only. The Task 4 `save_path` rules are kept.

- [ ] **Step 1: Write the failing tests**

```python
def test_encode_png_signature_and_ihdr(self)    # startswith b"\x89PNG\r\n\x1a\n"; IHDR width 3, height 2
def test_encode_png_roundtrip_pixels(self)      # 3x2 image; zlib.decompress(IDAT), strip one filter byte per row -> equals input
def test_dpi_awareness_declares_pointer_argtype(self)  # fake user32: argtypes == [c_void_p]; failure -> returns False, no exception
# integration
def test_window_screenshot_includes_open_dialog(self)  # modal open -> scope=window returns 2 image items; the second image's size within 10% of GetWindowRect of the dialog;
                                                       # the main-window image is not one uniform colour (PrintWindow can render the IE child black)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_png -v`, then `python scripts/run_integration.py it_screenshot`
Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the commands from Step 2**

Expected: OK, no skips.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: DPI-aware window screenshots including modal dialogs"
```

---

### Task 15: Attach to an IE-mode tab the user opened

**Files:**
- Create: `ie_mcp/tools/attach.py`
- Modify: `ie_mcp/win32/windows.py`, `ie_mcp/protocol.py` (refuse driver tools while attached). `ie_windows` (Task 9) and `ie_screenshot scope=window` (Task 14) already read `ATTACHED_TOPS`; this task only fills it.
- Test: `tests/test_attach.py`, `tests/integration/it_attach.py`

**Interfaces:**
- Consumes: `ie_server_child`, `ATTACHED_TOPS` (Task 7); `ComBackend`, `resolve_backend`, `backends.DEFAULT_TARGET`, `backends.ATTACHED_CHAINS` (Task 8); `drain.js`, `instrument.js` (Task 12); session owner records (Task 5).
- Produces:
  - **`TabInfo` dataclass:** `hwnd: int` (the `Internet Explorer_Server` window), `top_hwnd: int`, `ancestors: list[int]` (the IE server's ancestor hwnds, nearest first, ending at `top_hwnd`), `pid: int` (the process that owns the `Internet Explorer_Server` window), `title: str`, `url: str`.
  - **`windows.ie_mode_tabs(enum_fn=None) -> list[TabInfo]`:** visible top windows of `msedge.exe` process trees that have an `Internet Explorer_Server` descendant. It **excludes** windows in the process trees of other live ie-mcp sessions (from their owner records). The URL is read through COM when pywin32 is present, else `""`.
  - **Tools** (`needs_driver=False`):
    - `ie_tabs()`;
    - `ie_attach(hwnd: int)` sets `backends.DEFAULT_TARGET = f"tab:{hwnd}"` and `backends.ATTACHED_CHAINS[hwnd] = tab.ancestors`, adds the tab's `top_hwnd` to `ATTACHED_TOPS` (so `MODAL_PROBE` sees the dialogs that window owns, and nothing else of the user's Edge), and injects/drains instrumentation through `ComBackend`. From then on, `dialog_drain` (Task 12) drains the tab after every call, `ie_windows` passes `attached_tops` to `enumerate_windows`, and `ie_screenshot scope=window` captures the tab's top window;
    - `ie_detach()` clears all three and reports that the instrumentation stays in the tab until it is reloaded.
  - **While `backends.DEFAULT_TARGET` is set,** every tool without an explicit target whose `needs_driver(args)` is true is refused by `run_tool` with `isError` "attached to tab <hwnd>: this tool needs the driver session; call ie_detach first". Such a tool never starts a browser.
  - **Documented limits** (README and tool descriptions): on attached tabs there are no driver-only tools (history, page screenshot, upload), no proxy capture, and a page's `prompt` cannot be answered (only `alert` and `confirm` windows pass the `ie_dialog` confirmation). Keys, hover, window screenshots and console work through COM and Win32.

- [ ] **Step 1: Write the failing tests**

```python
def test_ie_mode_tabs_finds_windows_with_ie_server(self)   # fake enum -> one TabInfo with hwnd of the IE server child
def test_ie_mode_tabs_excludes_other_live_sessions(self)   # window in another live session's tree -> excluded
def test_attach_sets_and_detach_clears_default_target(self)  # also ATTACHED_TOPS == {top_hwnd}, then empty; a dialog owned by another Edge window is not in modal_probe()
def test_driver_tools_refused_while_attached(self)         # DEFAULT_TARGET "tab:5"; ie_wait_ready -> isError containing "ie_detach"; SESSION.ensure not called
def test_dialog_without_target_refused_while_attached(self)  # DEFAULT_TARGET "tab:5"; ie_dialog {} -> isError containing "ie_detach"; ie_dialog target="tab:5" -> isError; SESSION.ensure not called
def test_resolve_tab_target_uses_com_backend(self)
# integration
def test_attach_reads_ie_mode_tab(self)                     # restart_session(IE_CAPTURE="0"), so no proxy loader is inserted; ie_open form.htm; ie_tabs lists it; ie_attach(hwnd);
                                                            # ie_text frame "inner" (no target) contains "Inner frame"; click send in the tab; ie_wait_text "Echo" (no target),
                                                            # whose AFTER_CALL drain re-injects instrument.js into the new /echo document; then, with no target,
                                                            # ie_js "window.setTimeout(function(){throw new Error('tab-late-error');},0);return 1", then ie_sleep seconds=1;
                                                            # ie_console has an error record containing "tab-late-error" whose where startswith "tab:"; ie_detach
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_attach -v`, then `python scripts/run_integration.py it_attach`
Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the commands from Step 2**

Expected: OK, no skips.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: attach to user-opened IE-mode tabs over COM"
```

---

### Task 16: Selftest, end-to-end MCP test, docs and release

**Files:**
- Modify: `ie_mcp/cli.py` (`--selftest`), `README.md`, `CLAUDE.md`, the spec's Fidelity table if anything changed, `pyproject.toml` (`version = "0.2.0"`)
- Create: `CHANGELOG.md`, `tests/integration/it_e2e_mcp.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - **`ie-mcp --selftest`** uses `ie_mcp_fixtures` (shipped in the wheel). It prints one `PASS`/`FAIL`/`SKIP` line per check:
    - selenium import, discovered driver path, Edge path, pywin32;
    - fixture server start;
    - IE-mode session (Trident userAgent, `documentMode` 7 on `form.htm`);
    - proxy verified — `SKIP` when capture is not `proxy`; loader kept `documentMode` — `SKIP` when capture is not `proxy` or D10 = no;
    - instrumentation present;
    - a modal dialog detected and closed;
    - no leftover processes.

    The last line is `RESULT: PASS|FAIL`; the exit code is 0/1.
  - **README:**
    - a fork notice crediting upstream;
    - the tool table (all tools, with `target`, `scope`, `mode`, `force`);
    - the env var table (every variable and default);
    - a **Fidelity** section (copy of the spec table);
    - a **Security** section (redaction scope, JSONL deletion, kept screenshots, file confinement, identity-checked killing, system dialogs never clicked, what is never changed);
    - **Limits:**
      - HTTPS is tunnel-only apart from native page XHR, with no load-time instrumentation;
      - cached responses are not seen without `IE_CAPTURE_NO_CACHE=1`;
      - PAC files are unsupported;
      - the proxy may change the IE security zone of intranet hosts;
      - the loopback proxy is reachable by other users on a shared terminal server;
      - dialog records can be lost on fast close;
      - attached tabs keep instrumentation until reloaded;
      - if the spike found D10 = no, the proxy inserts nothing: errors and console output during page load are not captured, only those after the first tool call on the page;
    - troubleshooting rows: driver not found, capture off/unverified, modal not reachable, pywin32 missing, alerts.
  - **`CHANGELOG.md` entry `0.2.0`:** the features, plus every changed behaviour:
    - `IE_INTERACTION_MODE=background`;
    - `IE_CLEAN_SESSION=0`;
    - uploads disabled without `IE_UPLOAD_DIR`;
    - no implicit `../.pydeps`;
    - no hardcoded driver path;
    - `unhandledPromptBehavior` set to `ignore`;
    - capture and instrumentation on by default (or capture off, per D12);
    - `ie_frames` returns name/id/src;
    - screenshots saved by name go to the session's `screenshots` folder;
    - `ie_close` gains `force`;
    - Python ≥ 3.10.

- [ ] **Step 1: Write the end-to-end test** (`tests/integration/it_e2e_mcp.py`). It launches `python -m ie_mcp` as a subprocess and speaks JSON-RPC over stdin/stdout:

```python
def test_full_session_over_stdio(self):
    # initialize -> tools/list includes ie_windows, ie_network, ie_console, ie_tabs, ie_close_window
    # ie_open frameset.htm -> ie_wait_ready loaded -> ie_get frame "main" by id "code" attr "value" succeeds
    # fill + submit -> ie_network has POST /echo (when capture is "proxy")
    # ie_open modal_opener.htm -> click open_lookup -> NOTICE in result or ie_windows lists html_dialog -> pick_A1 via D2 path -> lookup_code == "A1"
    # ie_console has no 'error' records for these pages
    # ie_screenshot scope=window returns >= 1 image
def test_server_exit_leaves_no_browser(self):
    # with a modal dialog open: close stdin -> process exits within 20s -> none of the recorded PIDs (with identities) remain
```

- [ ] **Step 2: Run it and fix any gap it exposes**

Run: `python scripts/run_integration.py it_e2e_mcp`
Expected: OK, no skips. Any failure points at a gap in Tasks 1–15; fix it there, with its own test, before continuing.

- [ ] **Step 3: Implement the selftest checks; write the README, CHANGELOG and CLAUDE.md updates**

- [ ] **Step 4: Run everything**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`, then `python scripts/run_integration.py`, then `pip install . && ie-mcp --selftest` (from a non-editable install, to prove the fixtures ship)
Expected: all OK, no skips; `RESULT: PASS`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "docs: 0.2.0 selftest, end-to-end test, README and changelog"
```
