# IE-mode Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the forked `ie-mcp` into a harness that gives an AI agent full access to Edge IE-mode pages: DOM, interaction, modal dialogs, windows, network, console and rendering.

**Architecture:** Keep upstream's Selenium IEDriver session as the main backend; add a Win32/COM backend (`WM_HTML_GETOBJECT`) for modal dialogs and user-opened tabs, a watchdog so blocked driver calls never freeze the stdio server, a per-process recording proxy for network capture and early script injection, and ES3 page instrumentation for console/errors/dialogs. Verify everything against a generic local fixture site.

**Tech Stack:** Python 3.9+, selenium 4.x, IEDriverServer 4.14.0 (Win32), Microsoft Edge IE mode, stdlib `http.server`/`http.client`/`ctypes`/`zlib`, optional `pywin32` (COM), `unittest`.

**Spec:** [docs/superpowers/specs/2026-11-17-ie-mode-harness-design.md](../specs/2026-11-17-ie-mode-harness-design.md). Repository rules: [CLAUDE.md](../../../CLAUDE.md).

## Global Constraints

- Application-agnostic: no company, product, hostname, credential or captured data from any real application anywhere in the repo. Tests target only `tests/fixtures/legacy_site/`.
- Windows 10/11 only; IE mode is never headless.
- `requires-python = ">=3.9"`; runtime dependency `selenium>=4.14.0,<5` only; `pywin32>=306` as the optional extra `win32`. The server must start and list tools without pywin32.
- IEDriverServer 32-bit 4.14.0 is the reference driver version.
- Every script injected into a page is ES3 (see CLAUDE.md "Trident rules"); host methods (`alert`, `showModalDialog`, `open`) are called directly with explicit arguments, never through `.apply`/`.call` (IE < 9 host objects lack them).
- No registry, system-proxy, certificate or security-setting changes, except upstream's opt-in `IE_SITE_LIST` policy writer.
- Only processes proven to belong to a dead session of this server are ever killed.
- Captured data (network, console, screenshots) lives only under the session directory; secrets are redacted by default.
- MIT license and upstream attribution are kept.
- New env vars introduced by this plan, with defaults: `IE_TOOL_TIMEOUT=45`, `IE_CLICK_MODE` (default chosen in Task 2), `IE_CLEAN_SESSION=0`, `IE_UPLOAD_DIR` (unset = uploads disabled), `IE_CAPTURE=1`, `IE_CAPTURE_MAX_BODY=262144`, `IE_CAPTURE_REDACT=1`, `IE_CAPTURE_REDACT_FIELDS=(?i)pass|pwd|secret|token|auth`, `IE_INSTRUMENT=1`. `IE_INTERACTION_MODE` default changes from `native` to `background`.

## Review Focus

1. **A click that opens a modal dialog.** The server must answer every later tool call within `IE_TOOL_TIMEOUT`, and every result must say a modal is open. Pinned by Task 5 `test_slow_tool_times_out_with_blocked_message` and Task 8 `test_driver_click_on_modal_opener_does_not_hang_server`.
2. **Pages in IE5 quirks or IE7 document mode** (no `JSON`, no `querySelector`). Every injected script must still work. Pinned by Task 3 `test_js_resources_are_es3` and Task 10 `test_instrumentation_works_in_quirks_and_ie7`.
3. **A frame that navigated between two tool calls.** Frame-addressed tools re-resolve the path and succeed. Pinned by Task 7 `test_frame_tools_survive_frame_navigation`.
4. **Traffic containing passwords, cookies or auth headers.** Redacted in tool output and in the JSONL file. Pinned by Task 9 `test_redacts_password_field_and_cookie` and `test_jsonl_contains_no_secret_values`.
5. **Non-ASCII text in windows-1252 and UTF-8 pages** (`Müller – café`). It must survive fill/get, network bodies and console without mojibake. Pinned by Task 7 `test_fill_and_get_non_ascii` and Task 9 `test_request_body_decoded_with_page_charset`.

---

### Task 1: Baseline, fixture site and fixture server

**Files:**
- Modify: `pyproject.toml` (`requires-python = ">=3.9"`, `version = "0.2.0.dev0"`, `[project.optional-dependencies] win32 = ["pywin32>=306"]`)
- Modify: `.gitignore` (add `captures/`, `*.jsonl`, `screenshots/`)
- Create: `tests/__init__.py`, `tests/fixtures/__init__.py`, `tests/fixtures/server.py`
- Create: `tests/fixtures/legacy_site/` pages listed below
- Test: `tests/test_fixture_server.py`

**Interfaces:**
- Produces: `tests.fixtures.server.start_fixture_server(host: str = "127.0.0.1", port: int = 0) -> FixtureServer`; `FixtureServer.base_url: str` (e.g. `http://127.0.0.1:50123/`), `FixtureServer.stop() -> None`, context manager. `python -m tests.fixtures.server [--host H] [--port P]` serves until Ctrl+C and prints the base URL.
- Endpoints: static files from `legacy_site/`; `POST /echo` returns an HTML page (UTF-8) with `<dd id="echo_<field>">value</dd>` per posted field and `<dd id="echo_raw">` holding the raw body; `GET /api/lookup.xml?q=<prefix>` returns `text/xml` `<items><item code="A1" name="Alpha"/><item code="B2" name="Beta"/><item code="C3" name="Gamma"/></items>` filtered by code prefix; `GET /slow?ms=N` waits N ms then returns `slow ok`; `GET /cookie` sets `Set-Cookie: sid=fixture-session; Path=/`.
- Fixture pages (every interactive element has `id` = `name`):

| Page | Contents |
|---|---|
| `index.htm` | Title `Fixture index`; links to every page |
| `frameset.htm` | `<frameset cols="20%,*">` with frame `menu` (`menu.htm`) and frame `main` (`form.htm`) |
| `menu.htm` | Links that target `main`: `form.htm`, `grid.htm` |
| `form.htm` | `X-UA-Compatible IE=7`; POST form to `/echo` with `code`, `pwd` (password), select `kind` (A/B/C), textarea `notes`, checkbox `flag`, submit `send`; `<iframe name="inner" src="inner.htm">` |
| `inner.htm` | Text `Inner frame` and input `inner_field` |
| `quirks.htm` | No doctype; uses `document.all.quirk_target.innerText = 'quirks ok'` on load |
| `charset.htm` | `windows-1252` encoded; text `Müller – café`; POST form to `/echo` with input `name` |
| `modal_opener.htm` | `X-UA-Compatible IE=7`; inputs `lookup_code`, `lookup_name`, `answer`; button `open_lookup` runs `var o={sCode:'',sName:''}; window.showModalDialog('modal_dialog.htm', o, 'dialogWidth:420px;dialogHeight:320px'); if(o.sCode!=''){...fill both inputs...}`; button `ask` sets `answer` to the return value of `showModalDialog('modal_confirm.htm', {question:'Proceed?'}, ...)` |
| `modal_dialog.htm` | Reads `window.dialogArguments`; loads `/api/lookup.xml` synchronously with `new ActiveXObject('Microsoft.XMLHTTP')`; renders rows with links `pick_A1`, `pick_B2`, `pick_C3` that set `dialogArguments.sCode/sName` then `window.close()`; button `nested` opens `modal_nested.htm` and writes its return value into `nested_result` |
| `modal_confirm.htm` | Shows `dialogArguments.question`; buttons `yes`/`no` set `window.returnValue` and close |
| `modal_nested.htm` | Button `nested_ok` sets `window.returnValue='nested-ok'` and closes |
| `xhr.htm` | Button `load` POSTs `q=ping` to `/echo` via `ActiveXObject('Microsoft.XMLHTTP')` (fallback `XMLHttpRequest`); writes `responseText.length` into `xhr_result` |
| `dialogs.htm` | Buttons `do_alert` (`alert('Hello alert')`), `do_confirm` (result into `confirm_result`), `do_prompt` (result into `prompt_result`), `do_popup` (`window.open('popup.htm','pop','width=320,height=200')`) |
| `popup.htm` | Text `Popup window`; button `close_me` |
| `errors.htm` | On load: `try{console.log('fixture-log')}catch(e){}` then `undefinedFunctionCall();` |
| `grid.htm` | `table#grid` with header `Code/Name/Price` and 3 rows, filled by `setTimeout(...,1500)` |

- [ ] **Step 1: Switch to the working branch and record the baseline**

Run: `git switch harness` (created with the commit that added this plan) then `pip install -e ".[win32]"` then `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: `Ran 10 tests` … `OK` (upstream suite).

- [ ] **Step 2: Write the failing fixture-server tests** in `tests/test_fixture_server.py`

```python
class FixtureServerTests(unittest.TestCase):
    def test_serves_static_page(self):            # GET index.htm -> 200, body contains "Fixture index"
    def test_echo_lists_posted_fields(self):      # POST code=A1&pwd=x -> '<dd id="echo_code">A1</dd>' in body
    def test_lookup_xml_filters_by_prefix(self):  # GET /api/lookup.xml?q=B -> content-type starts "text/xml", 'code="B2"' in body, 'code="A1"' not in body
    def test_cookie_endpoint_sets_session_cookie(self):  # Set-Cookie header == "sid=fixture-session; Path=/"
    def test_charset_page_is_windows_1252(self):  # raw bytes contain b"M\xfcller"
```

- [ ] **Step 3: Run them to verify they fail**

Run: `python -m unittest tests.test_fixture_server -v`
Expected: FAIL / ImportError (`tests.fixtures.server` missing).

- [ ] **Step 4: Implement `start_fixture_server` with `http.server.ThreadingHTTPServer`, and write the pages**

- [ ] **Step 5: Run all unit tests**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: 15 tests, OK.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .gitignore tests/
git commit -m "test: add legacy fixture site and server"
```

---

### Task 2: Spike — answer the five open questions on a real IE-mode session

**Files:**
- Create: `scripts/spike_ie_mode.py`
- Create: `docs/spikes/2026-11-17-ie-mode-spike.md`

**Interfaces:**
- Consumes: `start_fixture_server` (Task 1).
- Produces: the decision record. Later tasks read these keys from it: **D1** `IE_CLICK_MODE` default (`driver` or `async`); **D2** primary dialog path (`driver-handles` or `com`); **D3** network capture mode (`proxy` or `instrument-only`); **D4** proxy bypass setting needed for loopback (`<-loopback>` or "serve fixtures on a LAN address"); **D5** whether `console` must be created by instrumentation in each document mode; **D6** Win32 window classes and the process (Edge or a child) that owns the IE content and the dialog windows.

The spike script uses raw Selenium plus `ctypes`/pywin32, not the server. It prints one `Qn: <answer> (evidence)` line per question and never leaves processes behind (`finally: driver.quit()` plus kill by Edge profile).

- [ ] **Step 1: Ask the user to put `IEDriverServer.exe` 4.14.0 (Win32) in place and set `IE_DRIVER_PATH`**

Run: `python ie_mcp.py --selftest`
Expected: `RESULT: PASS` and a userAgent containing `Trident`.

- [ ] **Step 2: Write `scripts/spike_ie_mode.py`** with one function per question:
  - `q1_driver_click_blocks(driver, base)`: open `modal_opener.htm`, run `driver.find_element(By.ID,'open_lookup').click()` in a thread, join with a 15 s timeout, and report `blocks=True/False`. Then close the dialog through Win32 (`WM_CLOSE` to the dialog hwnd) so the session recovers.
  - `q2_dialog_handles(driver, base)`: click asynchronously with `execute_script("var b=arguments[0];window.setTimeout(function(){b.click();},0);", el)`. Poll `driver.window_handles` for 10 s. If a new handle appears, `switch_to.window` to it, run `return document.title`, click `pick_A1`, switch back and read `lookup_code`.
  - `q3_win32_dialog(edge_pid)`: `EnumWindows` → class, title and owning PID of every visible window; find the `Internet Explorer_Server` child under the dialog; `WM_HTML_GETOBJECT` → `ObjectFromLresult` → `document.title` and `body.innerText[:80]`.
  - `q4_per_process_proxy(base)`: start a minimal logging proxy (stdlib, logs request lines). Create a new session with `options.proxy` (manual, http=127.0.0.1:port, noProxy=`<-loopback>`) and `options.use_per_process_proxy = True`. Open `index.htm` and report whether the proxy saw `GET .../index.htm`. Repeat with the fixture server bound to the machine's LAN IP if loopback was bypassed.
  - `q5_console(driver, base)`: run `return typeof console` on `index.htm`, `form.htm` (IE7) and `quirks.htm`.

- [ ] **Step 3: Run the spike on an unlocked desktop**

Run: `python scripts/spike_ie_mode.py`
Expected: five `Qn:` lines; no `msedge.exe --ie-mode-force` or `IEDriverServer.exe` left running.

- [ ] **Step 4: Write `docs/spikes/2026-11-17-ie-mode-spike.md`** with a table `Question | Answer | Evidence` and a table `Decision | Value | Reason` for D1–D6. If Q2 and Q3 both fail (no way into the dialog), stop and report to the user before continuing; Tasks 6–8 depend on one of them working.

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
- Create: `ie_mcp/js/__init__.py`, `ie_mcp/js/select.js`, `ie_mcp/js/grid.js`, `ie_mcp/js/ready_probe.js` (moved verbatim from `_SELECT_JS`, `_GRID_JS`, `_READY_PROBE_JS`)
- Delete: `ie_mcp.py`
- Modify: `pyproject.toml` (`[tool.setuptools.packages.find] include = ["ie_mcp*"]`, package-data `ie_mcp.js = ["*.js"]`, script `ie-mcp = "ie_mcp.cli:main"`)
- Modify: `tests/test_interaction_mode.py`, `tests/test_session_ownership.py` (import from the new modules)
- Test: `tests/test_protocol.py`, `tests/test_js_resources.py`

**Interfaces:**
- Produces:
  - `ie_mcp.config.Settings` (frozen dataclass, one field per env var) and `load_settings(env: Mapping[str, str]) -> Settings`. Module-level `SETTINGS = load_settings(os.environ)`. Every later task adds its env vars here.
  - `ie_mcp.protocol.tool(name: str, description: str, schema: dict, *, needs_driver: bool = True)`: a decorator that registers a `ToolSpec(name, description, input_schema, fn, needs_driver)` in `REGISTRY: dict[str, ToolSpec]` (insertion-ordered).
  - `ie_mcp.protocol.handle(msg: dict) -> dict | None`, `serve(stdin, stdout) -> None`, `text_result(s: str) -> dict`, `image_result(png: bytes) -> dict`.
  - `ie_mcp.js.load(name: str) -> str` (`importlib.resources`, cached).
  - `ie_mcp.session.SESSION: IeSession`, `LEASE: SessionLease`, plus the upstream functions with unchanged signatures.
  - Tool modules register themselves on import; `ie_mcp/tools/__init__.py` imports all of them.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_protocol.py
UPSTREAM_TOOLS = {"ie_open","ie_goto","ie_status","ie_browsers","ie_kill_orphans","ie_frames","ie_text",
  "ie_html","ie_click","ie_fill","ie_upload","ie_dialog","ie_js","ie_screenshot","ie_wait_text",
  "ie_wait_ready","ie_select","ie_press_key","ie_back","ie_forward","ie_hover","ie_resize","ie_get",
  "ie_wait_element","ie_scroll","ie_grid","ie_sleep","ie_close"}
def test_tools_list_contains_all_upstream_tools(self):   # names from handle({"method":"tools/list",...}) ⊇ UPSTREAM_TOOLS
def test_unknown_tool_returns_jsonrpc_error(self):       # error.code == -32602
def test_tool_exception_becomes_is_error_result(self):   # registered failing tool -> result.isError is True, text startswith "ERROR in"
def test_initialize_returns_instructions(self):          # result has "instructions" and serverInfo.name == "ie-mcp"
# tests/test_js_resources.py
def test_js_resources_are_es3(self):   # for every ie_mcp/js/*.js: no "=>", "`", r"\blet\b", r"\bconst\b", "JSON.", ".forEach(", ".map(", ".filter(", "Object.keys", ".trim(", "querySelector" (querySelector allowed only on lines containing "documentMode")
def test_load_returns_script_text(self):  # load("grid.js") contains "getElementsByTagName('table')"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_protocol tests.test_js_resources -v`
Expected: FAIL / ImportError.

- [ ] **Step 3: Move the code into the modules above** with unchanged behaviour. Every env var goes through `Settings`. Every tool uses `@tool`.

- [ ] **Step 4: Run the whole unit suite and the selftest**

Run: `python -m unittest discover -s tests -p "test_*.py" -v` then `pip install -e ".[win32]"` and `ie-mcp --selftest`
Expected: all tests OK; `RESULT: PASS`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: split server into ie_mcp package with tool registry"
```

---

### Task 4: Hardening fixes from the code review

**Files:**
- Modify: `ie_mcp/config.py`, `ie_mcp/session.py`, `ie_mcp/tools/interact.py`, `ie_mcp/tools/read.py`, `ie_mcp/tools/diagnostics.py`
- Create: `ie_mcp/js/frames.js`
- Test: `tests/test_config.py`, `tests/test_session_ownership.py`, `tests/test_interaction_mode.py`, `tests/test_tool_guards.py`

**Interfaces:**
- Produces:
  - `config.discover_driver_path(env: Mapping[str,str], home: Path) -> str | None`. Order: `IE_DRIVER_PATH` if the file exists; else the highest version under `home/.cache/selenium/IEDriverServer/win32/*/IEDriverServer.exe` (numeric version sort); else the same under `win64`.
  - `session.pid_alive(pid: int, run=subprocess.run) -> bool`: parses `tasklist /FI "PID eq <pid>" /FO CSV /NH` and compares the PID column exactly.
  - `IeSession.quit_browser() -> None` (quit driver and kill own Edge, **keep** the lease) and `IeSession.close() -> None` (`quit_browser()` + release the lease). `ensure()` holds the lease across all retries.
  - `Settings.clean_session: bool` (`IE_CLEAN_SESSION`, default `False`) → `options.ensure_clean_session`.
  - `Settings.upload_dir: Path | None` (`IE_UPLOAD_DIR`).
  - `Settings.pydeps: Path | None`: set only when `IE_PYDEPS` is given; no implicit `../.pydeps`.
  - Interaction mode default is `background`.

- [ ] **Step 1: Write the failing tests**

```python
def test_discover_prefers_highest_win32_version(self):   # dirs 4.8.0 and 4.14.0 -> path contains "4.14.0"
def test_discover_honours_explicit_env_path(self):       # IE_DRIVER_PATH=<existing tmp file> -> that path
def test_discover_returns_none_when_nothing_found(self)
def test_pid_alive_requires_exact_pid_match(self):       # fake tasklist CSV for PID 123, ask 12 -> False; ask 123 -> True
def test_stale_takeover_keeps_old_record_for_cleanup(self):
    # owner.json {owner_pid:111, driver_pid:5, edge_pid:6, profile:"p"}; lease for pid 222 acquires (111 dead);
    # then cleanup_stale_sessions(root, browser_rows=[edge 6 profile p, iedriver 5], pid_alive=lambda p: p==222)
    # -> killed == [5, 6]   (old record kept as owner.stale-111.json; cleanup globs */owner*.json)
def test_lease_is_held_across_attach_retries(self):      # fake builder fails twice then succeeds; lock file owner_pid == own pid after ensure()
def test_background_is_default_interaction_mode(self):   # resolve_interaction_mode({}).name == "background"
def test_clean_session_is_opt_in(self):                   # options built with default settings -> ensure_clean_session is False
def test_upload_disabled_without_upload_dir(self):        # ie_upload -> isError, text contains "IE_UPLOAD_DIR"
def test_upload_rejects_path_outside_upload_dir(self):    # path ..\x.txt -> isError, text contains "outside IE_UPLOAD_DIR"
def test_screenshot_save_path_must_be_relative(self):     # save_path "C:\\x.png" or "..\\x.png" -> isError
def test_pydeps_not_added_without_env(self):              # load_settings({}).pydeps is None
def test_text_falls_back_to_inner_text(self):             # fake driver whose element .text raises -> uses execute_script innerText
def test_frames_lists_name_id_src(self):                  # fake execute_script returns "0\x1fmenu\x1fmenu\x1fmenu.htm\x1e1\x1fmain\x1f\x1fform.htm" -> parsed list of dicts
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_config tests.test_session_ownership tests.test_interaction_mode tests.test_tool_guards -v`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement the fixes.** Screenshots saved by name go to `<session dir>/screenshots/<name>`. `frames.js` reads `name`/`id`/`src` of each `frame`/`iframe` element of the current document and returns a `\x1e`/`\x1f` delimited string.

- [ ] **Step 4: Run the unit suite**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: all OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "fix: driver discovery, exact pid check, lease takeover, safe defaults"
```

---

### Task 5: Watchdog, driver lock, async click and modal notices

**Files:**
- Modify: `ie_mcp/protocol.py`, `ie_mcp/session.py`, `ie_mcp/tools/interact.py`, `ie_mcp/config.py`
- Test: `tests/test_watchdog.py`, `tests/test_click_mode.py`

**Interfaces:**
- Consumes: `REGISTRY`/`ToolSpec.needs_driver` (Task 3); D1 (Task 2) for the `IE_CLICK_MODE` default.
- Produces:
  - `session.DRIVER_LOCK: threading.Lock`. Tools with `needs_driver=True` run while holding it.
  - `protocol.run_tool(spec: ToolSpec, args: dict, timeout_s: float) -> dict`. It runs the tool in a daemon worker thread. If the driver lock cannot be acquired, or the call exceeds `timeout_s`, it returns `isError` with text that starts `BLOCKED:` and names `ie_windows`. A worker that is still stuck keeps the lock until its driver call returns.
  - `protocol.MODAL_PROBE: Callable[[], list[dict]]` (default returns `[]`; Task 6 replaces it). After every tool call, if the probe returns windows, append a text item `NOTICE: modal dialog open: <title> (hwnd <n>). Use ie_windows, then target="hwnd:<n>" or ie_switch_window.`
  - `serve()` dispatches each `tools/call` to `run_tool` on its own thread and writes responses under a stdout lock. Responses may come back out of order, each with its request `id`.
  - `Settings.tool_timeout: float` (`IE_TOOL_TIMEOUT`, default 45); `Settings.click_mode: str` (`IE_CLICK_MODE`, `driver`|`async`, default from D1).
  - `ie_click` gains `mode` (`driver`|`async`, default `Settings.click_mode`). `async` runs `execute_script("var e=arguments[0];window.setTimeout(function(){e.click();},0);return 'queued';", el)`. `double`/`right` actions always use the driver.

- [ ] **Step 1: Write the failing tests**

```python
def test_slow_tool_times_out_with_blocked_message(self):  # needs_driver tool sleeps 2s, timeout 0.5 -> isError, text startswith "BLOCKED:", "ie_windows" in text
def test_non_driver_tool_runs_while_driver_tool_is_stuck(self):  # stuck tool holds DRIVER_LOCK; needs_driver=False tool returns within 0.5s
def test_driver_tool_reports_blocked_when_lock_busy(self)
def test_lock_released_after_stuck_tool_finishes(self)
def test_responses_carry_request_ids_when_out_of_order(self):  # serve() with two calls, the first slow -> both ids present in output lines
def test_notice_appended_when_modal_open(self):  # MODAL_PROBE returns [{"title":"Lookup","hwnd":42}] -> last content item text contains "hwnd 42"
def test_async_click_uses_settimeout_script(self):  # fake driver records execute_script; script contains "setTimeout"; element.click not called
def test_driver_click_mode_calls_element_click(self)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_watchdog tests.test_click_mode -v`
Expected: FAIL.

- [ ] **Step 3: Implement** `run_tool`, threaded `serve`, `MODAL_PROBE` notices and the click `mode`.

- [ ] **Step 4: Run the unit suite**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: all OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: tool watchdog, driver lock, async click and modal notices"
```

---

### Task 6: Win32 layer — windows, process tree, HTML documents

**Files:**
- Create: `ie_mcp/win32/__init__.py`, `ie_mcp/win32/windows.py` (ctypes only), `ie_mcp/win32/htmldoc.py` (pywin32)
- Modify: `ie_mcp/protocol.py` (install the real `MODAL_PROBE`)
- Test: `tests/test_win32_windows.py`

**Interfaces:**
- Consumes: D6 (Task 2) for window classes and owning processes; `SESSION.edge_pid`.
- Produces:
  - `WindowInfo` dataclass: `hwnd: int, pid: int, cls: str, title: str, kind: str, owner_hwnd: int | None, visible: bool`. `kind` is one of `"main"`, `"popup"`, `"html_dialog"`, `"alert"`, `"other"`.
  - `classify(cls: str, has_ie_server_child: bool, is_session_main: bool) -> str`. Rules: `Internet Explorer_TridentDlgFrame` → `html_dialog`; `#32770` → `alert`; a top window with an `Internet Explorer_Server` descendant → `main` if it is the session's first window, else `popup`. Apply any corrections recorded in D6.
  - `enumerate_windows(pids: set[int], enum_fn=None) -> list[WindowInfo]`. `enum_fn` is injectable for tests; the default uses `EnumWindows`/`EnumChildWindows`/`GetClassNameW`/`GetWindowTextW`/`GetWindowThreadProcessId`.
  - `browser_pids(edge_pid: int, rows=None) -> set[int]`: the process tree under `edge_pid` (CIM `Win32_Process` ParentProcessId).
  - `modal_probe() -> list[dict]`: `html_dialog`/`alert` windows of the session's process tree, as dicts. Returns `[]` when there is no session.
  - `htmldoc.ie_server_child(hwnd: int) -> int | None`; `htmldoc.document_from_hwnd(hwnd: int, timeout_ms: int = 1000)` returns the COM `IHTMLDocument2` (`RegisterWindowMessage("WM_HTML_GETOBJECT")`, `SendMessageTimeout(..., SMTO_ABORTIFHUNG)`, `pythoncom.ObjectFromLresult`). Without pywin32 it raises `RuntimeError` naming `pip install "ie-mcp[win32]"`.

- [ ] **Step 1: Write the failing tests** (fake enumeration data; no real windows)

```python
def test_classify_trident_dialog_frame_as_html_dialog(self)
def test_classify_32770_as_alert(self)
def test_first_ie_window_is_main_others_popup(self)
def test_enumerate_filters_to_session_pids(self):     # windows of pid 9 excluded when pids={1,2}
def test_browser_pids_walks_process_tree(self):        # rows parent->child 1->2->3, 4 unrelated -> {1,2,3}
def test_modal_probe_empty_without_session(self)
def test_document_from_hwnd_without_pywin32_names_extra(self):  # patch import to fail -> RuntimeError mentioning 'ie-mcp[win32]'
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_win32_windows -v`
Expected: FAIL.

- [ ] **Step 3: Implement** the ctypes enumeration, the process tree, `htmldoc` and the probe wiring.

- [ ] **Step 4: Run the unit suite**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: all OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: Win32 window enumeration, modal probe and HTML document access"
```

---

### Task 7: Backends and the `target` argument

**Files:**
- Create: `ie_mcp/backends.py`
- Modify: `ie_mcp/tools/read.py`, `ie_mcp/tools/interact.py` (route through backends)
- Test: `tests/test_backends.py`, `tests/integration/__init__.py`, `tests/integration/base.py`, `tests/integration/test_read_interact.py`

**Interfaces:**
- Consumes: `document_from_hwnd` (Task 6), `DRIVER_LOCK` (Task 5), `js.load` (Task 3).
- Produces:
  - `Backend` protocol: `html(frame) -> str`, `text(frame) -> str`, `js(script: str, frame, args: list) -> object`, `find(by: str, value: str, frame, timeout: float) -> object`, `click(el, mode: str) -> None`, `fill(el, text: str, clear: bool) -> None`, `select(el, mode: str, val: str) -> str`, `get(el, attr: str) -> str`, `frames() -> list[dict]`, `needs_driver: bool`.
  - `DriverBackend` (the existing Selenium logic) and `ComBackend(doc)`. In `ComBackend`:
    - Frames resolve through `doc.frames(i).document`.
    - Locators are `id`, `name`, `tag`, plus `css` only when `doc.documentMode >= 8`. `xpath` raises `ValueError("xpath is not supported for COM targets")`.
    - `js` wraps the script as `window.__iemcp_r=(function(){<script>})();` via `parentWindow.execScript` and reads back `parentWindow.__iemcp_r`.
    - `click` defaults to `setTimeout` (async) through `execScript`.
    - `fill` sets `value` and fires `onchange`, using `fireEvent` or `dispatchEvent` (feature-detect).
  - `resolve_backend(target: str | None) -> Backend`: `None`/`""` → `DriverBackend`; `"hwnd:<int>"` → `ComBackend(document_from_hwnd(n))`; `"tab:<int>"` is reserved for Task 12. Anything else raises `ValueError`.
  - Tools `ie_html`, `ie_text`, `ie_js`, `ie_click`, `ie_fill`, `ie_select`, `ie_get`, `ie_frames`, `ie_wait_element` accept an optional `target`. Their `needs_driver` is decided per call by the resolved backend.
  - `tests/integration/base.py`: `IntegrationTestCase` (skips unless `IE_MCP_INTEGRATION=1`). It starts the fixture server in `setUpClass`, calls tools through `protocol.handle`, closes the session in `tearDownClass`, and asserts that no own processes remain.

- [ ] **Step 1: Write the failing unit tests** (fake COM document/element objects)

```python
def test_resolve_backend_default_is_driver(self)
def test_resolve_backend_parses_hwnd_target(self)       # patch document_from_hwnd; "hwnd:42" -> ComBackend
def test_unknown_target_rejected(self)                  # "frame:1" -> ValueError
def test_com_find_by_id(self)
def test_com_xpath_rejected(self)
def test_com_css_requires_document_mode_8(self)          # documentMode 7 -> ValueError mentioning "documentMode"
def test_com_js_returns_value_via_expando(self)
def test_com_fill_fires_onchange(self)
```

- [ ] **Step 2: Write the integration tests** (`tests/integration/test_read_interact.py`)

```python
def test_frameset_read_nested_frame(self):          # frameset.htm; ie_text frame="main/inner" contains "Inner frame"
def test_frame_tools_survive_frame_navigation(self):  # click menu link to grid.htm, ie_wait_text "Code" frame="main", then ie_text frame="main" contains "Code"
def test_fill_select_submit_form(self):             # fill code=A1, select kind=B, submit -> echo page shows echo_code A1, echo_kind B
def test_fill_and_get_non_ascii(self):              # charset.htm: fill name="Müller – café"; ie_get value equals it exactly
def test_quirks_page_readable(self):                # quirks.htm text contains "quirks ok"
```

- [ ] **Step 3: Run them to verify they fail**

Run: `python -m unittest tests.test_backends -v` and `set IE_MCP_INTEGRATION=1 && python -m unittest tests.integration.test_read_interact -v`
Expected: FAIL.

- [ ] **Step 4: Implement** `backends.py` and route the tools through it.

- [ ] **Step 5: Run unit and integration suites**

Run: the two commands from Step 3
Expected: all OK; no leftover IE-mode processes.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: driver and COM backends with per-call target"
```

---

### Task 8: Window and dialog tools

**Files:**
- Create: `ie_mcp/tools/windows.py`
- Modify: `ie_mcp/tools/dialogs.py`
- Test: `tests/test_window_tools.py`, `tests/integration/test_modal.py`, `tests/integration/test_popups_alerts.py`

**Interfaces:**
- Consumes: `enumerate_windows`, `browser_pids`, `WindowInfo` (Task 6); `resolve_backend` (Task 7); D2 (Task 2).
- Produces:
  - `ie_windows` (`needs_driver=False`): JSON list of `WindowInfo` plus `target` (`"hwnd:<n>"`). When the driver lock is free, also `driver_handle` (matched by title, then URL) and `current: bool`.
  - `ie_switch_window(handle: str | None, title_contains: str | None, main: bool = False)` → `driver.switch_to.window(...)`; returns the new title and URL.
  - `ie_close_window(target: str)`: for `hwnd:` targets call `parentWindow.close()` through COM; for driver handles switch to it, close it, and switch back to main.
  - `ie_dialog` gains `target`; for `alert` windows found by Win32 without a driver alert, it handles them by sending `BM_CLICK` to the dialog's OK/Cancel button.
  - In the main flow (per D2), after an action opens a modal, tools reach the dialog either by `ie_switch_window` (driver-handles) or by `target="hwnd:<n>"` (com). Both must be supported; D2 decides which one the `NOTICE:` text recommends first.

- [ ] **Step 1: Write the failing unit tests**

```python
def test_windows_tool_lists_dialog_with_target(self)    # fake enumerate -> item kind html_dialog, target "hwnd:42"
def test_windows_tool_works_while_driver_locked(self)
def test_close_window_hwnd_uses_com_close(self)
```

- [ ] **Step 2: Write the integration tests**

```python
# test_modal.py
def test_lookup_dialog_roundtrip(self):            # click open_lookup -> ie_windows has html_dialog within 10s -> act in dialog -> pick_A1 -> dialog gone -> lookup_code=="A1", lookup_name=="Alpha"
def test_confirm_dialog_return_value(self):        # ask -> yes -> answer=="yes"
def test_nested_modal(self):                       # open_lookup -> nested -> nested_ok -> nested_result=="nested-ok" -> pick_B2 -> lookup_code=="B2"
def test_driver_click_on_modal_opener_does_not_hang_server(self):  # ie_click mode="driver" on open_lookup; next ie_windows returns within IE_TOOL_TIMEOUT+5s and lists the dialog; close it via ie_close_window
# test_popups_alerts.py
def test_alert_accept_and_text(self):              # do_alert -> ie_dialog action=text == "Hello alert" -> accept
def test_confirm_dismiss(self):                    # do_confirm -> dismiss -> confirm_result == "false"
def test_popup_window_switch(self):                # do_popup -> ie_windows kind popup -> switch -> text "Popup window" -> close_me -> switch main
```

- [ ] **Step 3: Run them to verify they fail**

Run: `python -m unittest tests.test_window_tools -v` and `set IE_MCP_INTEGRATION=1 && python -m unittest tests.integration.test_modal tests.integration.test_popups_alerts -v`
Expected: FAIL.

- [ ] **Step 4: Implement** the tools.

- [ ] **Step 5: Run the commands from Step 3**

Expected: all OK; no leftover processes.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: window, popup and modal dialog tools"
```

---

### Task 9: Recording proxy and network tools

**Files:**
- Create: `ie_mcp/capture/__init__.py`, `ie_mcp/capture/store.py`, `ie_mcp/capture/redact.py`, `ie_mcp/capture/proxy.py`, `ie_mcp/tools/network.py`
- Modify: `ie_mcp/session.py` (start the proxy before the driver; proxy options), `ie_mcp/config.py`
- Test: `tests/test_capture_store.py`, `tests/test_redact.py`, `tests/test_capture_proxy.py`, `tests/integration/test_network.py`

**Interfaces:**
- Consumes: D3 and D4 (Task 2). If D3 is `instrument-only`, implement the store, redaction and tools, but leave the proxy disabled by default (`IE_CAPTURE=0`) and state that in the README. Network entries then come only from Task 10.
- Produces:
  - `NetworkEntry` dataclass: `id: int, started: float, duration_ms: int, method: str, url: str, status: int | None, req_headers: list[tuple[str,str]], req_body: bytes, resp_headers: list[tuple[str,str]], resp_body: bytes, tunnel: bool, truncated: bool, error: str | None, source: str` (`"proxy"` or `"page"`).
  - `CaptureStore(max_entries: int = 2000, max_body: int = 262144, jsonl_path: Path | None = None)` with `add(entry) -> int`, `list(url_contains=None, method=None, status=None, since_id=0, limit=50) -> list[NetworkEntry]`, `get(id) -> NetworkEntry | None`, `clear() -> None`. JSONL lines are written after redaction.
  - `redact_headers(headers) -> list[tuple[str,str]]`: masks values of `Cookie`, `Set-Cookie`, `Authorization`, `Proxy-Authorization` with `<redacted>`. `redact_body(content_type: str, body: bytes, field_re: str) -> bytes` handles `application/x-www-form-urlencoded` and `multipart/form-data`. Both are no-ops when `IE_CAPTURE_REDACT=0`.
  - `RecordingProxy(store: CaptureStore, host="127.0.0.1", port=0, inject_html: str | None = None)` with `start() -> int` and `stop()`:
    - forwards absolute-form HTTP requests with `http.client`, dropping hop-by-hop headers;
    - tunnels `CONNECT` and records the tunnel entry;
    - when `inject_html` is set and the response is `text/html` 200: sets `Accept-Encoding: identity` upstream, inserts the snippet right after the first `<head...>` tag (case-insensitive; at the start if there is none), and fixes `Content-Length`;
    - bodies are capped at `max_body` with `truncated=True`.
  - Session start (when `IE_CAPTURE=1`): start the proxy, then `options.proxy = Proxy({"proxyType": "manual", "httpProxy": f"127.0.0.1:{port}", "sslProxy": f"127.0.0.1:{port}", "noProxy": <D4 value>})` and `options.use_per_process_proxy = True`. Stop the proxy in `close()`.
  - Tools (`needs_driver=False`):
    - `ie_network(url_contains, method, status, since_id, limit=50)`: one line per entry, `id method status duration_ms size url`.
    - `ie_request(id, body_limit=20000)`: headers and bodies decoded with the charset from `Content-Type`, then the page's `<meta charset>`, then `cp1252`. Binary bodies are summarised as `<binary N bytes>`.
    - `ie_network_clear()`.

- [ ] **Step 1: Write the failing unit tests**

```python
def test_store_ring_buffer_drops_oldest(self)            # max_entries=2, add 3 -> ids listed [2,3]
def test_store_filters_by_url_method_status(self)
def test_redacts_password_field_and_cookie(self)         # body "code=A1&pwd=s3cret" -> "code=A1&pwd=<redacted>"; Cookie value -> "<redacted>"
def test_jsonl_contains_no_secret_values(self)           # "s3cret" not in jsonl file text
def test_redaction_can_be_disabled(self)
def test_proxy_records_post_body_and_response(self)      # urllib ProxyHandler -> fixture POST /echo; entry method POST, status 200, req_body has "code=A1"
def test_proxy_returns_response_unchanged_without_injection(self)  # bytes equal to a direct fetch
def test_proxy_injects_script_after_head(self)           # inject_html="<script>X</script>"; index.htm body has it right after "<head>"
def test_injection_updates_content_length(self)
def test_connect_tunnel_recorded_as_tunnel(self)         # local TCP echo server via CONNECT -> entry.tunnel True, echo works
def test_request_body_decoded_with_page_charset(self)    # cp1252 POST "name=M%FCller" -> ie_request text shows "Müller"
```

- [ ] **Step 2: Write the integration tests**

```python
def test_form_post_captured_with_redaction(self):   # form.htm fill code=A1, pwd=s3cret, submit -> ie_network has POST /echo; ie_request shows code=A1 and pwd=<redacted>
def test_activex_xhr_captured(self):                # xhr.htm load -> POST /echo body q=ping
def test_dialog_document_requests_captured(self):   # modal flow -> GET modal_dialog.htm and GET /api/lookup.xml?q= present
```

- [ ] **Step 3: Run them to verify they fail**

Run: `python -m unittest tests.test_capture_store tests.test_redact tests.test_capture_proxy -v` and `set IE_MCP_INTEGRATION=1 && python -m unittest tests.integration.test_network -v`
Expected: FAIL.

- [ ] **Step 4: Implement** the store, redaction, proxy, session wiring and tools.

- [ ] **Step 5: Run the commands from Step 3**

Expected: all OK.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: per-process recording proxy and network tools"
```

---

### Task 10: Page instrumentation and console tools

**Files:**
- Create: `ie_mcp/js/instrument.js`, `ie_mcp/js/drain.js`, `ie_mcp/capture/console.py`, `ie_mcp/tools/console.py`
- Modify: `ie_mcp/session.py` (proxy `inject_html` = instrument script when `IE_INSTRUMENT=1`), `ie_mcp/protocol.py` (post-call hook)
- Test: `tests/test_console.py`, `tests/integration/test_console.py`

**Interfaces:**
- Consumes: `RecordingProxy(inject_html=...)` (Task 9), `Backend.js`/`frames` (Task 7), D5 (Task 2).
- Produces:
  - `instrument.js` (ES3, idempotent through `window.__iemcp`) keeps a queue `window.__iemcp.q` of records `kind\x1flevel\x1ftime\x1ftext`. Kinds:
    - `console`: wraps `log`/`info`/`warn`/`error`/`debug`, creating `window.console` when missing (per D5);
    - `error`: from `window.onerror`, chained to any existing handler;
    - `dialog`: `alert`/`confirm`/`prompt` with their result;
    - `modal`: `showModalDialog` URL, features, a one-level summary of `dialogArguments` (string/number/boolean own properties) and the return value;
    - `open`: `window.open` URL, name and features.
    It calls the original host functions directly with explicit arguments. With D3 = `proxy` it does **not** wrap XMLHttpRequest or ActiveX objects, because network comes from the proxy. With D3 = `instrument-only` it also wraps native `window.XMLHttpRequest` (`open`/`send`/`readystatechange`, recording an `xhr` record: method, URL, request body, status, response length). The post-call hook copies those records into the `CaptureStore` as `NetworkEntry(source="page")`. ActiveX `XMLHTTP` objects cannot be wrapped without changing their behaviour; in that mode the README lists them as not captured.
  - `drain.js` returns the joined queue and empties it.
  - `ConsoleRecord` dataclass `id: int, kind: str, level: str, time: float, text: str, where: str` (`where` = window target and frame path). `parse_records(raw: str, where: str) -> list[ConsoleRecord]`. `ConsoleStore(max_records=5000)` has `add_many`, `list(since_id=0, kinds=None, limit=100)` and `clear`.
  - Post-call hook in `protocol.run_tool`, after each `needs_driver` tool while it still holds the lock: walk every same-origin frame of the current window; inject `instrument.js` where `typeof window.__iemcp == 'undefined'`; drain into the `ConsoleStore`. All exceptions are logged and swallowed.
  - Tools (`needs_driver=False`): `ie_console(since_id=0, kinds=None, limit=100)` and `ie_console_clear()`.

- [ ] **Step 1: Write the failing unit tests**

```python
def test_parse_records_splits_fields(self)
def test_store_since_id_and_kind_filter(self)
def test_injection_payload_is_single_inline_script(self)  # payload starts "<script" ends "</script>", inner text has no "</script"
def test_post_hook_errors_do_not_fail_tool(self)          # fake backend raising in js -> tool result unchanged
def test_xhr_records_become_page_network_entries(self)    # only when D3 = instrument-only: xhr record -> CaptureStore entry source "page"
```

- [ ] **Step 2: Write the integration tests**

```python
def test_load_time_error_and_console_captured(self):     # errors.htm -> records: console "fixture-log", error containing "undefinedFunctionCall"
def test_alert_recorded(self):                            # dialogs.htm do_alert -> dialog record "Hello alert" (then accept)
def test_modal_call_recorded_with_arguments(self):        # modal flow -> modal record with "modal_dialog.htm" and "sCode"
def test_instrumentation_works_in_quirks_and_ie7(self):   # quirks.htm and form.htm -> ie_js "return typeof window.__iemcp" == "object"
```

- [ ] **Step 3: Run them to verify they fail**

Run: `python -m unittest tests.test_console -v` and `set IE_MCP_INTEGRATION=1 && python -m unittest tests.integration.test_console -v`
Expected: FAIL.

- [ ] **Step 4: Implement** the scripts, store, hook and tools.

- [ ] **Step 5: Run unit and integration suites**

Run: `python -m unittest discover -s tests -p "test_*.py" -v` and `set IE_MCP_INTEGRATION=1 && python -m unittest discover -s tests/integration -p "test_*.py" -v`
Expected: all OK.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: ES3 page instrumentation and console tools"
```

---

### Task 11: Window screenshots (rendered view including dialogs)

**Files:**
- Create: `ie_mcp/png.py`, `ie_mcp/win32/capture.py`
- Modify: `ie_mcp/tools/read.py` (or wherever `ie_screenshot` lives after Task 3)
- Test: `tests/test_png.py`, `tests/integration/test_screenshot.py`

**Interfaces:**
- Consumes: `enumerate_windows`, `modal_probe` (Task 6).
- Produces:
  - `png.encode_png(width: int, height: int, rgba: bytes) -> bytes`: 8-bit RGBA, filter 0, stdlib `zlib`/`struct` only.
  - `win32.capture.capture_window(hwnd: int) -> tuple[int, int, bytes]`: `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT=2)` into a 32-bit DIB section, converted from BGRA to RGBA.
  - `ie_screenshot` gains `scope`: `page` (default, driver), `element` (driver, `by`/`value`), `window` (Win32: the session's main window plus every open `html_dialog`/`alert`, one image each, in z-order). A `target` of `hwnd:<n>` captures that window only. Existing `save_path`/`no_inline` behaviour (Task 4 rules) is kept.

- [ ] **Step 1: Write the failing tests**

```python
def test_encode_png_signature_and_ihdr(self)   # starts b"\x89PNG\r\n\x1a\n"; IHDR width/height == inputs
def test_encode_png_roundtrip_pixels(self)     # 3x2 image; decompress IDAT, strip filter bytes -> equals input
# integration
def test_window_screenshot_includes_open_dialog(self)  # modal open -> scope=window returns 2 image items, both > 1000 bytes
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_png -v` and `set IE_MCP_INTEGRATION=1 && python -m unittest tests.integration.test_screenshot -v`
Expected: FAIL.

- [ ] **Step 3: Implement** the encoder, capture and the `scope` option.

- [ ] **Step 4: Run the commands from Step 2**

Expected: all OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: window screenshots including modal dialogs"
```

---

### Task 12: Attach to an IE-mode tab the user opened

**Files:**
- Create: `ie_mcp/tools/attach.py`
- Modify: `ie_mcp/win32/windows.py`, `ie_mcp/backends.py` (`tab:` targets, session default target)
- Test: `tests/test_attach.py`, `tests/integration/test_attach.py`

**Interfaces:**
- Consumes: `htmldoc.ie_server_child`, `document_from_hwnd` (Task 6); `ComBackend`, `resolve_backend` (Task 7).
- Produces:
  - `TabInfo` dataclass `hwnd: int` (the `Internet Explorer_Server` window), `top_hwnd: int`, `pid: int`, `title: str`, `url: str`.
  - `win32.windows.ie_mode_tabs(enum_fn=None) -> list[TabInfo]`: every visible top window with an `Internet Explorer_Server` descendant, any process. The URL is read through COM when pywin32 is present, else `""`.
  - Tools (`needs_driver=False`):
    - `ie_tabs()` lists them;
    - `ie_attach(hwnd: int)` sets `protocol.DEFAULT_TARGET = f"tab:{hwnd}"`, so target-aware tools without an explicit `target` use the `ComBackend` of that tab;
    - `ie_detach()` clears it.
  - `resolve_backend("tab:<n>")` → `ComBackend(document_from_hwnd(n))`.
  - Documented limits (README and tool descriptions): no driver-only tools (keys, hover, history, page screenshot) and no proxy capture on attached tabs. Console instrumentation works through `ComBackend.js`.

- [ ] **Step 1: Write the failing tests**

```python
def test_ie_mode_tabs_finds_windows_with_ie_server(self)  # fake enum -> one TabInfo with hwnd of the IE server child
def test_attach_sets_default_target(self)
def test_detach_clears_default_target(self)
def test_resolve_tab_target_uses_com_backend(self)
# integration
def test_attach_reads_tab_opened_outside_the_tool_target(self)  # open form.htm via ie_open, then ie_tabs lists it; ie_attach(hwnd); ie_text (no target) contains "Inner" via frame "inner"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_attach -v` and `set IE_MCP_INTEGRATION=1 && python -m unittest tests.integration.test_attach -v`
Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run the commands from Step 2**

Expected: all OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: attach to user-opened IE-mode tabs over COM"
```

---

### Task 13: Selftest, end-to-end MCP test, docs and release

**Files:**
- Modify: `ie_mcp/cli.py` (`--selftest`), `README.md`, `CLAUDE.md`, `pyproject.toml` (`version = "0.2.0"`)
- Create: `CHANGELOG.md`, `tests/integration/test_e2e_mcp.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `ie-mcp --selftest` prints one `PASS`/`FAIL`/`SKIP` line per check: selenium import, driver path (discovered), Edge path, pywin32, fixture server start, IE-mode session (Trident userAgent), proxy saw the request, instrumentation present, modal dialog detected and closed, no leftover processes. Last line `RESULT: PASS|FAIL`; exit code 0/1.
  - README:
    - a fork notice crediting upstream;
    - tool table (all tools, including `target`, `scope`, `mode`);
    - env var table (all vars, defaults);
    - a security section (redaction, file confinement, what is never changed);
    - troubleshooting rows for: driver not found, proxy not used (D4), modal not reachable, pywin32 missing.
  - `CHANGELOG.md` entry `0.2.0` listing the features and the changed defaults (`IE_INTERACTION_MODE=background`, `IE_CLEAN_SESSION=0`, uploads disabled without `IE_UPLOAD_DIR`).

- [ ] **Step 1: Write the end-to-end test** (`tests/integration/test_e2e_mcp.py`). It launches `python -m ie_mcp` as a subprocess and speaks JSON-RPC over stdin/stdout:

```python
def test_full_session_over_stdio(self):
    # initialize -> tools/list includes ie_windows, ie_network, ie_console, ie_tabs
    # ie_open frameset.htm -> ie_wait_ready loaded -> ie_text frame "main" contains "code"
    # fill + submit -> ie_network has POST /echo
    # ie_open modal_opener.htm -> click open_lookup -> NOTICE in result or ie_windows lists html_dialog -> pick_A1 via dialog path -> lookup_code == "A1"
    # ie_console has no 'error' records for these pages
    # ie_screenshot scope=window returns >= 1 image
def test_server_exit_leaves_no_browser(self):
    # close stdin -> process exits within 15s -> no msedge --ie-mode-force / IEDriverServer with this session's profile remains
```

- [ ] **Step 2: Run it to verify the gaps** (selftest checks not implemented yet)

Run: `set IE_MCP_INTEGRATION=1 && python -m unittest tests.integration.test_e2e_mcp -v`
Expected: PASS for tool behaviour if Tasks 1–12 are done; FAIL only where something is still missing. Fix it before continuing.

- [ ] **Step 3: Implement the selftest checks, then write the README, CHANGELOG and CLAUDE.md updates**

- [ ] **Step 4: Run everything**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`, `set IE_MCP_INTEGRATION=1 && python -m unittest discover -s tests/integration -p "test_*.py" -v`, `ie-mcp --selftest`
Expected: all OK; `RESULT: PASS`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "docs: 0.2.0 selftest, end-to-end test, README and changelog"
```
