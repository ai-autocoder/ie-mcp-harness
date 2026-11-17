# IE-mode Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the forked `ie-mcp` into a harness that gives an AI agent full access to Edge IE-mode pages (DOM, interaction, modal dialogs, windows, network, console, rendering) without changing how those pages behave.

**Architecture:** Keep upstream's Selenium IEDriver session as the main backend. Add:
- a Win32/COM backend (`WM_HTML_GETOBJECT`) for modal dialogs and user-opened tabs;
- a watchdog, so blocked driver calls never freeze the stdio server;
- a per-process recording proxy for network capture and early script injection;
- minimal ES3 page instrumentation for errors, dialogs and console.

Verify everything against a generic fixture site shipped as `ie_mcp_fixtures`.

**Tech Stack:** Python 3.10+, selenium 4.x, IEDriverServer 4.14.0 (Win32), Microsoft Edge IE mode, stdlib `http.server`/`http.client`/`ctypes`/`zlib`, optional `pywin32` (COM), `unittest`.

**Spec:** [docs/superpowers/specs/2026-11-17-ie-mode-harness-design.md](../specs/2026-11-17-ie-mode-harness-design.md). Repository rules: [CLAUDE.md](../../../CLAUDE.md) (read its Fidelity, Trident, Injection, Quirks and Safety sections before any task).

## Global Constraints

- **Application-agnostic.** No company, product, hostname, credential or captured data from any real application anywhere in the repo. Tests target only `ie_mcp_fixtures/site/`.
- **Fidelity.** No change to a page's document mode, charset detection, script behaviour or cached content. Every page modification is listed in `ie_status` and the README, and can be switched off (spec "Fidelity" table).
- **Platform.** Windows 10/11 only; IE mode is never headless.
- **Python and dependencies.** `requires-python = ">=3.10"`; runtime dependency `selenium>=4.14.0,<5` only; `pywin32>=306` as the optional extra `win32`. The server must start and list its tools without pywin32.
- **Driver.** IEDriverServer 32-bit 4.14.0 is the reference version. `build_options` always sets `unhandled_prompt_behavior="ignore"`. Whenever a `proxy` capability is set, it also sets `use_per_process_proxy=True`, with `noProxy` as a list.
- **Injected scripts** follow CLAUDE.md "Trident rules for injected JavaScript" (ES3; host methods called directly with the caller's exact argument count; `\x1e`/`\x1f` written as escapes).
- **No system changes.** No registry, system-proxy, firewall, certificate or security-setting changes, except upstream's opt-in `IE_SITE_LIST` policy writer.
- **Process killing.** Only this session's recorded PIDs, or a dead session's PIDs matched by Edge profile, are ever killed. System dialogs are never clicked.
- **Captured data.** Lives in memory, or under the session directory only when `IE_CAPTURE_JSONL=1` or for screenshots. Deleted on close unless `IE_KEEP_ARTIFACTS=1`. Redaction is on by default.
- **Licence.** The MIT licence and upstream attribution are kept.
- **Commands.** Bash (Git Bash) syntax. Integration tests run only through `python scripts/run_integration.py`, which fails on any skipped test. "Expected: OK" always means no `skipped=`.

**Environment variables this plan introduces (defaults in brackets):**

| Variable | Default |
|---|---|
| `IE_TOOL_TIMEOUT` | `45` |
| `IE_CLICK_MODE` | chosen by D1 in Task 2 |
| `IE_CLEAN_SESSION` | `0` |
| `IE_UPLOAD_DIR` | unset (uploads disabled) |
| `IE_CAPTURE` | `1` |
| `IE_CAPTURE_MAX_BODY` | `262144` |
| `IE_CAPTURE_REDACT` | `1` |
| `IE_CAPTURE_REDACT_FIELDS` | `(?i)pass\|pwd\|secret\|token\|auth\|session\|key` |
| `IE_CAPTURE_REDACT_HEADERS` | `(?i)^(cookie\|set-cookie\|authorization\|proxy-authorization\|x-[a-z0-9-]*(token\|auth\|key\|csrf\|session)[a-z0-9-]*)$` |
| `IE_CAPTURE_JSONL` | `0` |
| `IE_CAPTURE_UPSTREAM_PROXY` | unset |
| `IE_INSTRUMENT` | `1` |
| `IE_CONSOLE_STUB` | `0` |
| `IE_KEEP_ARTIFACTS` | `0` |
| `IE_FIXTURE_HOST` | `127.0.0.1` |

In the table, `\|` is an escaped `|`; the real defaults contain plain `|`.

`IE_INTERACTION_MODE` changes its default from `native` to `background`.

## Review Focus

1. **A click that opens a modal dialog.** The server must answer every later tool call within its budget, every result must say a modal is open, and `ie_close(force=true)` must always recover. Pinned by Task 5 `test_slow_tool_times_out_with_blocked_message`, `test_force_close_works_while_driver_locked`, and Task 8 `test_driver_click_on_modal_opener_does_not_hang_server`.
2. **The proxy injecting the instrumentation script.** The page's document mode must stay the same (IE7 meta page stays 7, quirks page stays 5). Pinned by Task 10 `test_inject_after_leading_meta_title_base` and `test_never_inject_before_doctype_or_bom`, and Task 12 `test_document_mode_unchanged_by_injection`.
3. **A script alert open while the agent takes screenshots or runs scripts.** The alert must stay open until the agent handles it. Pinned by Task 4 `test_build_options_ignores_unhandled_prompts` and Task 8 `test_alert_survives_screenshot_and_js`.
4. **Traffic and console text containing passwords, tokens or cookies**, including values echoed back in responses. Masked in every tool output and on disk. Pinned by Task 9 `test_masks_redacted_value_in_response_body` and `test_jsonl_contains_no_secret_values`, and Task 12 `test_form_post_captured_with_redaction`.
5. **Non-ASCII text in windows-1252 and UTF-8 pages** (`Müller – café`). Survives fill/get, network bodies and console without mojibake. Pinned by Task 7 `test_fill_and_get_non_ascii` and Task 12 `test_request_body_decoded_with_page_charset`.

---

### Task 1: Baseline, fixture package and fixture server

**Files:**
- Modify: `pyproject.toml`:
  - `requires-python = ">=3.10"`, `version = "0.2.0.dev0"`;
  - `[project.optional-dependencies] win32 = ["pywin32>=306"]`;
  - keep `[tool.setuptools] py-modules = ["ie_mcp"]` and add `packages = ["ie_mcp_fixtures"]`;
  - package-data `ie_mcp_fixtures = ["site/*"]`.
- Modify: `.gitignore` (add `captures/`, `*.jsonl`, `screenshots/`)
- Create: `tests/__init__.py`, `ie_mcp_fixtures/__init__.py`, `ie_mcp_fixtures/__main__.py`, `ie_mcp_fixtures/server.py`, the pages under `ie_mcp_fixtures/site/`
- Test: `tests/test_fixture_server.py`

**Interfaces:**
- Produces:
  - `ie_mcp_fixtures.server.start_fixture_server(host: str = "127.0.0.1", port: int = 0, alt_port: int = 0) -> FixtureServer`.
  - `FixtureServer.base_url: str` (e.g. `http://127.0.0.1:50123/`), `FixtureServer.alt_base_url: str` (a second listener on another port, so a second origin), `FixtureServer.stop() -> None`; it is a context manager.
  - `python -m ie_mcp_fixtures [--host H]` serves until Ctrl+C and prints both URLs.
- Server behaviour:
  - Every response carries `Cache-Control: no-store` and no validators.
  - Static `.htm` files are served with `{{ALT_BASE}}` replaced by `alt_base_url`.
  - `POST /echo` returns UTF-8 HTML with `<dd id="echo_<field>">value</dd>` per posted field and `<dd id="echo_raw">` holding the raw body.
  - `GET /api/lookup.xml?q=<prefix>` returns `text/xml` `<items><item code="A1" name="Alpha"/><item code="B2" name="Beta"/><item code="C3" name="Gamma"/></items>`, filtered by code prefix (an empty `q` returns all).
  - `GET /slow?ms=N` waits N ms, then returns `slow ok`.
  - `GET /cookie` sets `Set-Cookie: sid=fixture-session; Path=/`.
- Fixture pages: every interactive element has `id` equal to `name`. Pages marked IE7 start their head with `<head><title>…</title><meta http-equiv="X-UA-Compatible" content="IE=7">` and use doctype `<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN" "http://www.w3.org/TR/html4/loose.dtd">`.

| Page | Contents |
|---|---|
| `index.htm` | HTML5 doctype, `X-UA-Compatible IE=edge`; title `Fixture index`; links to every page |
| `frameset.htm` | `<frameset cols="20%,*">` with frame `menu` (`menu.htm`) and frame `main` (`form.htm`) |
| `menu.htm` | Links that target `main`: `form.htm`, `grid.htm` |
| `form.htm` | IE7. POST form to `/echo` with `code`, `pwd` (password), select `kind` (A/B/C), textarea `notes`, checkbox `flag`, submit `send`; `<iframe name="inner" src="inner.htm">` |
| `inner.htm` | Text `Inner frame` and input `inner_field` |
| `xorigin.htm` | Iframe `foreign` with `src="{{ALT_BASE}}inner.htm"` (cross-origin) and iframe `local` with `src="inner.htm"` |
| `quirks.htm` | No doctype; on load `document.all.quirk_target.innerText = 'quirks ok'` |
| `charset.htm` | Encoded as `windows-1252` with meta charset; text `Müller – café`; POST form to `/echo` with input `name` |
| `utf8.htm` | UTF-8 with meta charset; same text and form as `charset.htm` |
| `modal_opener.htm` | IE7. Inputs `lookup_code`, `lookup_name`, `answer`. Button `open_lookup` runs `var o={sCode:'',sName:''}; window.showModalDialog('modal_dialog.htm', o, 'dialogWidth:420px;dialogHeight:320px'); if(o.sCode!=''){...fill both inputs...}`. Button `ask` sets `answer` to the return value of `showModalDialog('modal_confirm.htm', {question:'Proceed?'}, ...)` |
| `modal_dialog.htm` | IE7. Reads `window.dialogArguments`; loads `/api/lookup.xml?q=` synchronously with `new ActiveXObject('Microsoft.XMLHTTP')`; renders `table#lookup_grid` with links `pick_A1`, `pick_B2`, `pick_C3` that set `dialogArguments.sCode/sName` then `window.close()`; button `nested` opens `modal_nested.htm` and writes its return value into `nested_result` |
| `modal_confirm.htm` | Shows `dialogArguments.question`; buttons `yes`/`no` set `window.returnValue` and close |
| `modal_nested.htm` | Button `nested_ok` sets `window.returnValue='nested-ok'` and closes |
| `xhr.htm` | Button `load` POSTs `q=ping` to `/echo` via `ActiveXObject('Microsoft.XMLHTTP')` (fallback `XMLHttpRequest`) and writes `responseText.length` into `xhr_result`; button `load_native` does the same with native `XMLHttpRequest` only |
| `dialogs.htm` | Buttons `do_alert` (`alert('Hello alert')`), `do_confirm` (result into `confirm_result`), `do_prompt` (result into `prompt_result`), `do_popup` (`window.open('popup.htm','pop','width=320,height=200')`) |
| `popup.htm` | Text `Popup window`; button `close_me` |
| `errors.htm` | On load: `if(window.console){console.log('fixture-log')}` then `undefinedFunctionCall();`; button `late_error` throws `new Error('late-error')` |
| `grid.htm` | `table#grid` with header `Code/Name/Price` and 3 rows, filled by `setTimeout(...,1500)` |

- [ ] **Step 1: Switch to the working branch and record the baseline**

Run: `git switch harness` (the branch was created by the commit that added this plan), then `pip install -e ".[win32]" || pip install -e .`, then `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: `Ran 11 tests` … `OK` (the upstream suite: 5 interaction-mode tests and 6 ownership tests).

- [ ] **Step 2: Write the failing tests** in `tests/test_fixture_server.py`

```python
class FixtureServerTests(unittest.TestCase):
    def test_serves_static_page(self):                    # GET index.htm -> 200, body contains "Fixture index"
    def test_echo_lists_posted_fields(self):              # POST code=A1&pwd=x -> '<dd id="echo_code">A1</dd>' in body
    def test_lookup_xml_filters_by_prefix(self):          # GET /api/lookup.xml?q=B -> content-type startswith "text/xml"; 'code="B2"' in body; 'code="A1"' not in body
    def test_lookup_xml_empty_query_returns_all(self):    # q= -> body contains A1, B2 and C3
    def test_cookie_endpoint_sets_session_cookie(self):   # Set-Cookie header == "sid=fixture-session; Path=/"
    def test_charset_page_is_windows_1252(self):          # raw bytes of charset.htm contain b"M\xfcller"
    def test_responses_are_not_cacheable(self):           # Cache-Control == "no-store"; no ETag and no Last-Modified headers
    def test_alt_origin_placeholder_replaced(self):       # xorigin.htm body contains alt_base_url + "inner.htm"; "{{ALT_BASE}}" absent; alt_base_url port != base_url port
```

- [ ] **Step 3: Run them to verify they fail**

Run: `python -m unittest tests.test_fixture_server -v`
Expected: ImportError (`ie_mcp_fixtures` missing).

- [ ] **Step 4: Implement `start_fixture_server`** (two `ThreadingHTTPServer`s sharing one handler class) **and write the pages**

- [ ] **Step 5: Run all unit tests**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: `Ran 19 tests` … `OK`.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .gitignore tests/ ie_mcp_fixtures/
git commit -m "test: add legacy fixture site and two-origin fixture server"
```

---

### Task 2: Spike — answer the open questions on a real IE-mode session

**Files:**
- Create: `scripts/spike_ie_mode.py`, `docs/spikes/2026-11-17-ie-mode-spike.md`

**Interfaces:**
- Consumes: `start_fixture_server` (Task 1).
- Produces: the decision record. Later tasks read these keys from it:

| Key | Values | What depends on it |
|---|---|---|
| D1 | `IE_CLICK_MODE` default, per interaction mode: `driver` or `async` | Task 5 default; Task 8 tests |
| D2 | Way into modal dialogs: `driver-handles`, `com`, or `both` | Tasks 7 and 8 |
| D3 | Network capture: `proxy` or `instrument-only` | Tasks 11 and 12 |
| D4 | Loopback proxying with `noProxy: ["<-loopback>"]`: `works` or `bypassed` | Task 12 integration tests |
| D5 | `console` present per document mode (11 / 7 / 5): yes/no each | README; Task 11 tests |
| D6 | Window/process ownership: `edge-tree` (content and dialogs are in Edge's process tree) or `owner-chain` (follow window owners from the session's IE server windows) | Task 6 `session_window_pids` |
| D7 | COM script execution: `execScript`, `eval`, or `script-element` per document mode; COM from a worker thread inside its own apartment: yes/no | Task 7 `ComBackend.js` |
| D8 | Pop-up blocker blocks `window.open` from an async click: yes/no | Task 5 tool description; Task 8 popup test mode |

**Stop rules.** Stop and report to the user before continuing if any of these happen:
- Q2 and Q3 both find no way into a modal dialog.
- Q4 shows that the user's global Internet Settings changed.
- Q8 shows an alert being dismissed despite `ignore`.
- D4 = `bypassed`. The only other route is serving fixtures on a LAN address, which triggers a firewall prompt; the user decides.

The spike script uses raw Selenium plus ctypes/pywin32, not the server. It prints one `Qn:` line per question with its evidence. It always runs `driver.quit()` and kills its own Edge (by Edge profile) in `finally`. Each question that needs a fresh session quits the previous one first.

- [ ] **Step 1: Prepare the driver**

Ask the user to put `IEDriverServer.exe` 4.14.0 (Win32) in place and set `IE_DRIVER_PATH`.
Run: `python ie_mcp.py --selftest`
Expected: `RESULT: PASS` and a userAgent containing `Trident`.

- [ ] **Step 2: Write `scripts/spike_ie_mode.py`** with one function per question. Run Q1, Q2 and Q7 once with `native_events/require_window_focus` True and once with both False.

  - **`q1_driver_click_blocks`:** open `modal_opener.htm`. Run `find_element(By.ID,'open_lookup').click()` in a thread and join it with a 15 s timeout. Report `blocks=True/False` per mode. Then close the dialog with Win32 `PostMessage(WM_CLOSE)` so the session recovers.
  - **`q2_dialog_handles`:** click asynchronously with `execute_script("var b=arguments[0];window.setTimeout(function(){b.click();},0);", el)`.
    - Poll `driver.window_handles` for 10 s.
    - If a new handle appears: `switch_to.window`, `return document.title`, click `pick_A1`, switch back, read `lookup_code`.
    - Separately, with a dialog open, run `execute_script("return 1")` on the opener in a thread with a 10 s join, and report whether it blocks.
  - **`q3_win32_dialog`:** keep a dialog open (async click).
    - `EnumWindows` → class, title, owner hwnd and owning PID of every visible window.
    - Compare the PIDs with Edge's process tree (Toolhelp32).
    - Find the `Internet Explorer_Server` child under the dialog.
    - `WM_HTML_GETOBJECT` → `ObjectFromLresult` → `document.title` and `body.innerText[:80]`.
    - Then on the opener's and on `index.htm`'s IE server window, test `parentWindow.execScript("window.__t=1+1")` and read `__t` back. If that fails, test `parentWindow.eval`, then appending a `<script>` element.
    - Repeat one read from a new thread that calls `pythoncom.CoInitialize()` and gets its own document.
  - **`q4_per_process_proxy`:** quit the earlier session. Read (read-only) `HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings` `ProxyEnable`/`ProxyServer`/`ProxyOverride`.
    - Start a minimal logging proxy (stdlib; logs request lines).
    - Create a session with `options.proxy = Proxy({"proxyType": "manual", "httpProxy": "127.0.0.1:<p>", "sslProxy": "127.0.0.1:<p>", "noProxy": ["<-loopback>"]})` and `options.use_per_process_proxy = True`, with `initial_browser_url = <base>index.htm`.
    - Report whether the proxy saw: the initial URL; a later `get(form.htm)`; `popup.htm` from `do_popup`; `modal_dialog.htm` and `/api/lookup.xml` from the modal flow.
    - Read the Internet Settings again and report whether they are unchanged.
  - **`q5_console`:** `return typeof console` on `index.htm`, `form.htm` and `quirks.htm`.
  - **`q6_document_modes`:** `return navigator.userAgent + '|' + document.documentMode` on the same three pages.
    - Without a proxy.
    - Then through a spike proxy that inserts `<script>window.__inj=1</script>` after the leading meta/title/base run.
    - The modes must be identical, and `__inj` must be 1.
  - **`q7_popup_blocker`:** async click on `do_popup`; report whether a popup window appeared.
  - **`q8_prompt_ignore`:** session with `unhandled_prompt_behavior = "ignore"`. Click `do_alert` asynchronously, then `execute_script("return 1")` and `get_screenshot_as_png()`. Report the error (if any) and whether `switch_to.alert.text == "Hello alert"` still works afterwards.

- [ ] **Step 3: Run the spike on an unlocked desktop**

Run: `python scripts/spike_ie_mode.py`
Expected: lines `Q1:` … `Q8:`, with no `msedge.exe --ie-mode-force` or `IEDriverServer.exe` left running.

- [ ] **Step 4: Write `docs/spikes/2026-11-17-ie-mode-spike.md`** with a table `Question | Answer | Evidence` and a table `Decision | Value | Reason` for D1–D8. Apply the stop rules above.

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
  - remove `py-modules`;
  - `[tool.setuptools.packages.find] include = ["ie_mcp*"]` (this matches `ie_mcp_fixtures` too);
  - package-data `ie_mcp.js = ["*.js"]`;
  - script `ie-mcp = "ie_mcp.cli:main"`.
- Modify: `tests/test_interaction_mode.py`, `tests/test_session_ownership.py` (import from the new modules)
- Test: `tests/test_protocol.py`, `tests/test_js_resources.py`

**Interfaces:**
- Produces:
  - `ie_mcp.config.Settings` (frozen dataclass, one field per env var) and `load_settings(env: Mapping[str, str]) -> Settings`. Module-level `SETTINGS = load_settings(os.environ)`. Each later task adds its env vars here.
  - `ie_mcp.protocol.ToolSpec(name: str, description: str, input_schema: dict, fn: Callable[[dict], dict], needs_driver: Callable[[dict], bool], budget: Callable[[dict], float] | None)`.
  - `ie_mcp.protocol.tool(name, description, schema, *, needs_driver: bool | Callable[[dict], bool] = True, budget: Callable[[dict], float] | None = None)`. A bool is wrapped as `lambda args: value`. It registers the spec in `REGISTRY: dict[str, ToolSpec]` (insertion-ordered).
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
def test_tools_list_contains_all_upstream_tools(self):   # names from handle({"jsonrpc":"2.0","id":1,"method":"tools/list"}) == UPSTREAM_TOOLS
def test_unknown_tool_returns_jsonrpc_error(self):       # error.code == -32602
def test_tool_exception_becomes_is_error_result(self):   # registered failing tool -> result["isError"] is True, text startswith "ERROR in"
def test_initialize_returns_instructions(self):          # result has "instructions"; result["serverInfo"]["name"] == "ie-mcp"
def test_bool_needs_driver_becomes_callable(self):       # tool(..., needs_driver=False) -> spec.needs_driver({}) is False
# tests/test_js_resources.py
def test_js_resources_are_es3(self):
    # for every ie_mcp/js/*.js: none of "=>", "`", r"\blet\b", r"\bconst\b", "JSON.", ".forEach(", ".map(",
    # ".filter(", "Object.keys", ".trim(", ".apply(", ".call(" ; "querySelector" only on lines containing "documentMode"
def test_js_resources_have_no_raw_separator_bytes(self):  # "\x1e" and "\x1f" characters absent from file text
def test_load_returns_script_text(self):                  # load("grid.js") contains "getElementsByTagName('table')"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_protocol tests.test_js_resources -v`
Expected: ImportError.

- [ ] **Step 3: Move the code into the modules above**, with unchanged behaviour. Every env var goes through `Settings`; every tool uses `@tool`.

- [ ] **Step 4: Run the unit suite and the selftest**

Run: `pip install -e ".[win32]"`, then `python -m unittest discover -s tests -p "test_*.py" -v`, then `ie-mcp --selftest`
Expected: all tests OK; `RESULT: PASS`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: split server into ie_mcp package with tool registry"
```

---

### Task 4: Hardening fixes and safe defaults

**Files:**
- Modify: `ie_mcp/config.py`, `ie_mcp/session.py`, `ie_mcp/protocol.py` (`SERVER_INSTRUCTIONS`), `ie_mcp/tools/interact.py`, `ie_mcp/tools/read.py`, `ie_mcp/tools/diagnostics.py`, `ie_mcp/cli.py`
- Create: `ie_mcp/js/frames.js`
- Test: `tests/test_config.py`, `tests/test_session_ownership.py`, `tests/test_interaction_mode.py`, `tests/test_session_options.py`, `tests/test_tool_guards.py`

**Interfaces:**
- Produces:
  - `config.discover_driver_path(env: Mapping[str,str], home: Path) -> str | None`:
    - If `IE_DRIVER_PATH` is set: return it if the file exists, else raise `FileNotFoundError` naming `IE_DRIVER_PATH`.
    - Else the highest version under `home/.cache/selenium/IEDriverServer/win32/*/IEDriverServer.exe` (numeric version sort); else the same under `win64`; else `None`.
  - `session.pid_alive(pid: int, run=subprocess.run) -> bool`: parses `tasklist /FI "PID eq <pid>" /FO CSV /NH` and compares the PID column exactly.
  - `session.build_options(settings: Settings, initial_url: str | None, proxy_port: int | None = None) -> IeOptions`:
    - the upstream options;
    - `unhandled_prompt_behavior = "ignore"`;
    - `ensure_clean_session = settings.clean_session`;
    - focus/native events from the interaction mode;
    - when `proxy_port` is given: the proxy capability (`noProxy` a list) with `use_per_process_proxy = True`. Proxy wiring is used from Task 12.
  - `IeSession.main_hwnd: int | None`: the top-level window of the session's first IE-mode browser, recorded after attach (Task 6 fills in the lookup; until then `None`).
  - `IeSession.quit_browser() -> None` quits the driver and kills its own Edge, but **keeps** the lease. `ensure()` holds the lease across all retries.
  - `IeSession.kill_own_processes() -> list[int]` kills only `driver_pid` and `edge_pid` recorded for this session.
  - `IeSession.close(timeout_s: float = 10) -> None` runs `driver.quit()` in a thread joined for `timeout_s`, then `kill_own_processes()`, then releases the lease. `main()`'s `finally` calls `close()`.
  - `ie_close(force: bool = False)`: `needs_driver = not force`. Without `force` it calls `quit_browser()`; with `force=True` it calls `kill_own_processes()`, drops the driver reference and does not touch the driver.
  - `Settings.clean_session: bool` (`IE_CLEAN_SESSION`, default `False`); `Settings.upload_dir: Path | None` (`IE_UPLOAD_DIR`); `Settings.pydeps: Path | None` (set only when `IE_PYDEPS` is given; no implicit `../.pydeps`).
  - Interaction mode default is `background`. `SERVER_INSTRUCTIONS` is reworded to match (background is the default; `native` is the fallback).
  - Screenshots saved by name go to `<session dir>/screenshots/<name>`.
  - `frames.js` reads `name`/`id`/`src` of each `frame`/`iframe` of the current document and returns a `\x1e`/`\x1f` string.

- [ ] **Step 1: Write the failing tests**

```python
def test_discover_prefers_highest_win32_version(self)       # dirs 4.8.0 and 4.14.0 -> returned path contains "4.14.0"
def test_discover_honours_explicit_env_path(self)            # IE_DRIVER_PATH=<existing tmp file> -> that path
def test_discover_errors_when_explicit_path_missing(self)    # IE_DRIVER_PATH=<missing> -> FileNotFoundError, "IE_DRIVER_PATH" in str
def test_discover_returns_none_when_nothing_found(self)
def test_pid_alive_requires_exact_pid_match(self)           # fake tasklist CSV row for PID 123: ask 12 -> False; ask 123 -> True
def test_stale_takeover_keeps_old_record_for_cleanup(self):
    # owner.json {session_id:"s", owner_pid:111, driver_pid:5, edge_pid:6, profile:"p"}; lease for pid 222 acquires (111 dead);
    # old record renamed to owner.stale-111.json; cleanup_stale_sessions globs */owner*.json;
    # cleanup_stale_sessions(root, browser_rows=[{"pid":6,"kind":"edge-ie","profile":"p"},{"pid":5,"kind":"iedriver","profile":None}],
    #   pid_alive=lambda p: p == 222, kill=fake) -> fake called with [5, 6]
def test_lease_is_held_across_attach_retries(self)          # fake builder fails twice then succeeds; lock file owner_pid == own pid after ensure()
def test_background_is_default_interaction_mode(self)       # resolve_interaction_mode({}).name == "background" (replaces test_native_mode_is_the_backward_compatible_default)
def test_instructions_describe_background_default(self)     # "default" appears in SERVER_INSTRUCTIONS within the sentence naming background
def test_build_options_ignores_unhandled_prompts(self)      # build_options(load_settings({}), None).unhandled_prompt_behavior == "ignore"
def test_clean_session_is_opt_in(self)                      # default -> ensure_clean_session False; IE_CLEAN_SESSION=1 -> True
def test_proxy_always_per_process(self)                     # build_options(..., proxy_port=8123): use_per_process_proxy True; proxy noProxy == ["<-loopback>"]
def test_close_kills_own_processes_when_quit_hangs(self)    # fake driver.quit sleeps 5s, close(timeout_s=0.2) returns < 1s, kill called with [driver_pid, edge_pid]
def test_force_close_does_not_touch_driver(self)            # ie_close force=True with a driver whose methods raise -> no exception; own pids killed
def test_upload_disabled_without_upload_dir(self)           # ie_upload -> isError, text contains "IE_UPLOAD_DIR"
def test_upload_rejects_path_outside_upload_dir(self)       # path "..\\x.txt" -> isError, text contains "outside IE_UPLOAD_DIR"
def test_screenshot_save_path_must_be_relative(self)        # "C:\\x.png" and "..\\x.png" -> isError; "shot.png" -> written under session dir/screenshots
def test_pydeps_not_added_without_env(self)                 # load_settings({}).pydeps is None
def test_text_falls_back_to_inner_text(self)                # fake element whose .text raises -> result equals the fake execute_script innerText
def test_frames_lists_name_id_src(self)                     # fake execute_script returns "0\x1fmenu\x1fmenu\x1fmenu.htm\x1e1\x1fmain\x1f\x1fform.htm"
                                                            # -> [{"index":0,"name":"menu","id":"menu","src":"menu.htm"},{"index":1,"name":"main","id":"","src":"form.htm"}]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_config tests.test_session_ownership tests.test_interaction_mode tests.test_session_options tests.test_tool_guards -v`
Expected: the new tests fail.

- [ ] **Step 3: Implement the fixes**

- [ ] **Step 4: Run the unit suite**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "fix: safe defaults, prompt handling, driver discovery, lease and process handling"
```

---

### Task 5: Watchdog, driver lock, budgets, async click and modal notices

**Files:**
- Modify: `ie_mcp/protocol.py`, `ie_mcp/session.py`, `ie_mcp/config.py`, tool modules (budgets), `ie_mcp/tools/interact.py`
- Test: `tests/test_watchdog.py`, `tests/test_click_mode.py`

**Interfaces:**
- Consumes: `ToolSpec.needs_driver`/`budget` (Task 3); `IeSession.close`, `kill_own_processes` (Task 4); D1 and D8 (Task 2).
- Produces:
  - **`session.DRIVER_LOCK: threading.Lock`.**
  - **`protocol.call_budget(spec, args) -> float`:** `max(Settings.tool_timeout, spec.budget(args))` when the tool declares a budget, else `Settings.tool_timeout`. Declared budgets:
    - `ie_open`/`ie_goto`: `attach_retries * 32 + wait + 15`;
    - `ie_wait_ready`, `ie_wait_text`, `ie_wait_element`: `timeout + interval + 15`;
    - locator tools: `timeout + wait + 15`;
    - `ie_sleep`: `seconds + 5`.
  - **`protocol.run_tool(spec: ToolSpec, args: dict) -> dict`:** runs the tool in a daemon worker. For `spec.needs_driver(args)` it first calls `DRIVER_LOCK.acquire(timeout=budget)`.
    - If the lock can't be acquired: return `isError` with text `BUSY: <running tool> has held the browser for <n>s`. If `MODAL_PROBE()` is non-empty, return `BLOCKED: modal dialog open …` instead.
    - If the worker exceeds the budget: return `isError` with text `BLOCKED: the browser did not answer within <n>s`, the probe's windows, and the hints `ie_windows` and `ie_close force=true`. The stuck worker keeps the lock until its call returns.
  - **`protocol.MODAL_PROBE: Callable[[], list[dict]]`:** defaults to `lambda: []`; Task 6 replaces it. After every tool call, if the probe returns windows, a text item is appended: `NOTICE: <kind> open: <title> (hwnd <n>). Use ie_windows; act in it with target="hwnd:<n>" or ie_switch_window.`
  - **`protocol.POST_HOOKS: list[Callable[[ToolSpec, dict, dict], None]]`:** empty here, used in Task 11. They run after a successful driver tool, inside the worker, while the lock is still held, only when `MODAL_PROBE()` is empty and `args.get("mode") != "async"`. Each hook gets its own 3 s budget (a sub-thread joined for 3 s). Exceptions are logged and swallowed. The tool's own result is always returned unchanged.
  - **`serve()`:** dispatches each `tools/call` to `run_tool` on its own thread and writes responses under a stdout lock. Responses may come back out of order, each with its request `id`.
  - **Settings:** `tool_timeout: float` (`IE_TOOL_TIMEOUT`, default 45); `click_mode: str` (`IE_CLICK_MODE`, `driver`|`async`, default from D1 for the active interaction mode).
  - **`ie_click` gains `mode`** (`driver`|`async`, default `Settings.click_mode`). `async` runs `execute_script("var e=arguments[0];window.setTimeout(function(){e.click();},0);return 'queued';", el)`. `double`/`right` actions always use the driver. The tool description states that an async click is script-initiated: no mousedown/mouseup/focus, and (per D8) the pop-up blocker may block `window.open`.

- [ ] **Step 1: Write the failing tests**

```python
def test_slow_tool_times_out_with_blocked_message(self)     # needs_driver tool sleeps 2s, IE_TOOL_TIMEOUT 0.5 -> isError; text startswith "BLOCKED:"; "ie_windows" and "force=true" in text
def test_declared_budget_extends_timeout(self)              # tool with budget lambda a: 3, sleeps 1s, IE_TOOL_TIMEOUT 0.5 -> not isError
def test_busy_when_lock_held_by_running_tool(self)          # first tool holds lock 2s; second driver tool with budget 0.3 -> text startswith "BUSY:"
def test_non_driver_tool_runs_while_driver_tool_is_stuck(self)  # needs_driver=False tool returns within 0.5s while the lock is held
def test_force_close_works_while_driver_locked(self)        # stuck driver tool holds lock; ie_close force=True returns within 1s, kill_own_processes called
def test_lock_released_after_stuck_tool_finishes(self)
def test_responses_carry_request_ids_when_out_of_order(self)  # serve() with ids 1 (slow) and 2 (fast) -> output line for id 2 precedes id 1; both present
def test_notice_appended_when_modal_open(self)              # MODAL_PROBE -> [{"kind":"html_dialog","title":"Lookup","hwnd":42}]: last content text contains "hwnd 42"
def test_post_hooks_skipped_when_modal_open_or_async(self)  # hook not called when probe non-empty, nor when args mode == "async"
def test_post_hook_failure_does_not_change_result(self)
def test_async_click_uses_settimeout_script(self)           # fake driver: execute_script script contains "setTimeout"; element.click not called
def test_driver_click_mode_calls_element_click(self)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_watchdog tests.test_click_mode -v`
Expected: FAIL.

- [ ] **Step 3: Implement** budgets, `run_tool`, threaded `serve`, probe notices, post-hooks and click `mode`.

- [ ] **Step 4: Run the unit suite**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: tool watchdog with budgets, driver lock, async click and modal notices"
```

---

### Task 6: Win32 layer — processes, windows, HTML documents

**Files:**
- Create: `ie_mcp/win32/__init__.py`, `ie_mcp/win32/processes.py` (ctypes), `ie_mcp/win32/windows.py` (ctypes), `ie_mcp/win32/htmldoc.py` (pywin32)
- Modify: `ie_mcp/protocol.py` (install the real `MODAL_PROBE`), `ie_mcp/session.py` (record `main_hwnd` after attach)
- Test: `tests/test_win32_windows.py`

**Interfaces:**
- Consumes: D6 and D7 (Task 2); `SESSION.edge_pid`, `SESSION.main_hwnd`.
- Produces:
  - **`ProcessRow(pid: int, ppid: int, exe: str)`.**
    - `processes.snapshot() -> list[ProcessRow]` uses `CreateToolhelp32Snapshot`/`Process32FirstW`/`Process32NextW`, cached for 2 s.
    - `processes.process_tree(root_pid: int, rows: list[ProcessRow] | None = None) -> set[int]`.
  - **`WindowInfo` dataclass:** `hwnd: int, pid: int, cls: str, title: str, kind: str, owner_hwnd: int | None, visible: bool`. `kind` is one of `"main"`, `"popup"`, `"html_dialog"`, `"script_dialog"`, `"system_dialog"`, `"other"`.
  - **`classify(cls: str, has_ie_server_child: bool, owner_in_session: bool, hwnd: int, main_hwnd: int | None) -> str`:**
    - `Internet Explorer_TridentDlgFrame` → `html_dialog`.
    - `#32770` owned by a session IE window → `script_dialog`. This is a *candidate*: Task 8 confirms it through the driver alert or an instrumentation record before acting.
    - Any other `#32770` → `system_dialog`.
    - A top window with an `Internet Explorer_Server` descendant → `main` if `hwnd == main_hwnd`, else `popup`.
    - Apply corrections recorded in D6.
  - **`enumerate_windows(pids: set[int], enum_fn=None) -> list[WindowInfo]`.** The default uses `EnumWindows`/`EnumChildWindows`/`GetClassNameW`/`GetWindowTextW`/`GetWindowThreadProcessId`/`GetWindow(GW_OWNER)`. `enum_fn` is injectable for tests.
  - **`session_window_pids() -> set[int]`:** per D6, either `process_tree(SESSION.edge_pid)`, or the PIDs found by following owners from the session's IE server windows. Empty without a session.
  - **`modal_probe() -> list[dict]`:** `html_dialog`, `script_dialog` and `system_dialog` windows of the session, as dicts. Returns `[]` when there is no session.
  - **`htmldoc.com_apartment()`:** a context manager calling `pythoncom.CoInitialize()`/`CoUninitialize()`.
  - **`htmldoc.ie_server_child(hwnd: int) -> int | None`.**
  - **`htmldoc.document_from_hwnd(hwnd: int, timeout_ms: int = 1000)`:** must be called inside `com_apartment()`. It returns the COM `IHTMLDocument2` via `RegisterWindowMessage("WM_HTML_GETOBJECT")`, `SendMessageTimeout(..., SMTO_ABORTIFHUNG)` and `pythoncom.ObjectFromLresult`. Without pywin32 it raises `RuntimeError` naming `pip install "ie-mcp[win32]"`.

- [ ] **Step 1: Write the failing tests** (fake enumeration and process data; no real windows)

```python
def test_process_tree_walks_children(self)            # rows 1->2->3, 4 unrelated -> {1,2,3}
def test_classify_trident_dialog_frame_as_html_dialog(self)
def test_classify_owned_32770_as_script_dialog(self)
def test_classify_unowned_32770_as_system_dialog(self) # owner not in session -> "system_dialog"
def test_main_vs_popup_by_main_hwnd(self)              # hwnd == main_hwnd -> "main"; other IE window -> "popup"
def test_enumerate_filters_to_session_pids(self)       # windows of pid 9 excluded when pids={1,2}
def test_modal_probe_empty_without_session(self)
def test_document_from_hwnd_without_pywin32_names_extra(self)  # patch import to fail -> RuntimeError mentioning 'ie-mcp[win32]'
def test_com_apartment_initialises_and_uninitialises(self)     # fake pythoncom records calls in order
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_win32_windows -v`
Expected: FAIL.

- [ ] **Step 3: Implement** processes, enumeration, classification, `htmldoc`, probe wiring and `main_hwnd` recording.

- [ ] **Step 4: Run the unit suite**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: Win32 process and window layer, modal probe, HTML document access"
```

---

### Task 7: Backends, the `target` argument and the integration runner

**Files:**
- Create: `ie_mcp/backends.py`, `scripts/run_integration.py`, `tests/integration/__init__.py`, `tests/integration/base.py`
- Modify: `ie_mcp/tools/read.py`, `ie_mcp/tools/interact.py`, `ie_mcp/protocol.py` (`DEFAULT_TARGET`)
- Test: `tests/test_backends.py`, `tests/test_run_integration.py`, `tests/integration/it_read_interact.py`

**Interfaces:**
- Consumes: `com_apartment`, `document_from_hwnd` (Task 6); `DRIVER_LOCK`, `run_tool` (Task 5); `js.load` (Task 3); D7 (Task 2).
- Produces:
  - **`Backend` protocol:** `html(frame) -> str`, `text(frame) -> str`, `js(script: str, frame, args: list) -> object`, `find(by: str, value: str, frame, timeout: float) -> object`, `click(el, action: str, mode: str) -> None`, `fill(el, text: str, clear: bool) -> None`, `select(el, how: str, val: str) -> str`, `get(el, attr: str) -> str`, `frames() -> list[dict]`, `scroll(...)`, `grid(frame, table_id, headers, limit) -> dict`.
  - **`DriverBackend`** holds the existing Selenium logic.
  - **`ComBackend(hwnd: int)`** resolves its document inside `com_apartment()` on every call and never caches COM objects.
    - **Frames:** resolved through `doc.frames(i).document`.
    - **Locators:** `id`, `name`, `tag`, plus `css` only when `doc.documentMode >= 8`. `xpath` raises `ValueError("xpath is not supported for COM targets")`.
    - **`js`:** wraps the script as `window.__iemcp_r=(function(){<script>})();`, executes it per D7 (`execScript`, `eval` or a script element), and reads back `parentWindow.__iemcp_r`. `args` may contain only str/int/float/bool/None; they are embedded as ES3 literals and exposed as `arguments[i]`. Elements raise `ValueError`. Results that are not primitives are converted in-page: arrays via `join('\x1e')`, other objects via `String()`.
    - **`click`:** `action="click"` defaults to a `setTimeout` click. `double`/`right` fire `ondblclick`/`oncontextmenu` (synthetic).
    - **`fill`:** focuses the element, sets `value`, then fires `onkeydown`, `onkeypress`, `onkeyup`, `onchange`, `onblur` (feature-detect `fireEvent`/`dispatchEvent`).
    - Tool descriptions say COM input is synthetic.
  - **`protocol.DEFAULT_TARGET: str | None = None`** (set by Task 14).
  - **`resolve_backend(target: str | None) -> Backend`:** an empty target uses `DEFAULT_TARGET`; `None`/`""` → `DriverBackend`; `"hwnd:<int>"` and `"tab:<int>"` → `ComBackend(n)`. Anything else raises `ValueError`.
  - **`backends.target_kind(target: str | None) -> str`:** returns `"driver"`, `"hwnd"` or `"tab"` after applying `DEFAULT_TARGET`. It never touches COM and raises `ValueError` for unknown forms.
  - **Target-aware tools:** `ie_html`, `ie_text`, `ie_js`, `ie_click`, `ie_fill`, `ie_select`, `ie_get`, `ie_frames`, `ie_wait_element`, `ie_wait_text`, `ie_grid`, `ie_scroll` take an optional `target`. They are registered with `needs_driver=lambda a: target_kind(a.get("target")) == "driver"`.
  - **`scripts/run_integration.py [pattern]`:** sets `os.environ["IE_MCP_INTEGRATION"]="1"` and discovers `tests/integration` with pattern `it_*.py` (or `<pattern>.py`). It prints the unittest result and exits 1 if there were failures, errors **or skips**.
  - **`tests/integration/base.py` `IntegrationTestCase`:**
    - skips unless `IE_MCP_INTEGRATION=1`;
    - `setUpClass` starts the fixture server on `IE_FIXTURE_HOST`;
    - `call(name, **args) -> dict` and `text(name, **args) -> str` go through `protocol.handle`;
    - `tearDownClass` runs `ie_close`, then asserts that the session's recorded PIDs are gone (`processes.snapshot()`).

- [ ] **Step 1: Write the failing unit tests**

```python
def test_runner_fails_on_skip(self)                 # temp dir with a test that calls skipTest -> run_integration main() returns 1
def test_runner_passes_on_success(self)             # passing test -> 0
def test_resolve_backend_default_is_driver(self)
def test_resolve_backend_parses_hwnd_target(self)   # "hwnd:42" -> ComBackend with hwnd 42
def test_unknown_target_rejected(self)              # "frame:1" -> ValueError
def test_needs_driver_false_for_hwnd_target(self)   # REGISTRY["ie_text"].needs_driver({"target":"hwnd:1"}) is False; ({}) is True
def test_com_find_by_id(self)                       # fake doc.getElementById("code") returned
def test_com_xpath_rejected(self)
def test_com_css_requires_document_mode_8(self)     # documentMode 7 -> ValueError mentioning "documentMode"
def test_com_js_embeds_primitive_args(self)         # js("return arguments[0]+arguments[1]", None, ["a", 2]) -> executed text contains '"a"' and '2'
def test_com_js_rejects_element_args(self)
def test_com_fill_fires_event_sequence(self)        # fake element records fired events == ["onkeydown","onkeypress","onkeyup","onchange","onblur"]
```

- [ ] **Step 2: Write the integration tests** in `tests/integration/it_read_interact.py`

```python
def test_frameset_read_nested_frame(self)           # frameset.htm; ie_text frame="main/inner" contains "Inner frame"
def test_frame_tools_survive_frame_navigation(self) # click menu link "grid.htm" in frame "menu"; ie_wait_text "Code" frame="main"; ie_text frame="main" contains "Gamma"
def test_cross_origin_frame_reported_not_crashing(self)  # xorigin.htm; ie_frames lists 2 frames; ie_text frame="local" contains "Inner frame"; frame="foreign" -> isError mentioning cross-origin, server still answers
def test_fill_select_submit_form(self)              # form.htm: fill code=A1, select kind label "B", click send; ie_text contains "A1"; ie_get id echo_kind == "B"
def test_fill_and_get_non_ascii(self)               # charset.htm and utf8.htm: fill name="Müller – café"; ie_get value equals it exactly
def test_quirks_page_readable(self)                 # quirks.htm text contains "quirks ok"
```

- [ ] **Step 3: Run them to verify they fail**

Run: `python -m unittest tests.test_backends tests.test_run_integration -v`, then `python scripts/run_integration.py it_read_interact`
Expected: FAIL.

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

### Task 8: Window and dialog tools

**Files:**
- Create: `ie_mcp/tools/windows.py`
- Modify: `ie_mcp/tools/dialogs.py`
- Test: `tests/test_window_tools.py`, `tests/integration/it_modal.py`, `tests/integration/it_popups_alerts.py`

**Interfaces:**
- Consumes: `enumerate_windows`, `session_window_pids`, `WindowInfo` (Task 6); `resolve_backend` (Task 7); D1, D2 and D8 (Task 2).
- Produces:
  - **`ie_windows`** (`needs_driver=False`): a JSON list of `WindowInfo`, each with `target` (`"hwnd:<n>"`). When the driver lock is free it adds `driver_handle` (matched by title, then URL) and `current: bool`.
  - **`ie_switch_window(handle: str | None, title_contains: str | None, main: bool = False)`** → `driver.switch_to.window(...)`; returns the new title and URL. Implement it only if D2 is `driver-handles` or `both`.
  - **`ie_close_window(target: str)`**, tried in this order:
    - for `hwnd:` targets, `parentWindow.close()` over COM if D2 includes `com`, else Win32 `PostMessage(WM_CLOSE)`;
    - for driver handles, switch to it, close it, and switch back to main.

    Never used on `system_dialog` windows: those return `isError` "system dialog: ask the user".
  - **`ie_dialog`** gains `target`. Order of attempts:
    - the driver alert (`switch_to.alert`);
    - else, for a `script_dialog` window confirmed by a matching instrumentation `dialog` record from the last 30 s (or, before Task 11 exists, by its owner being a session IE window and `action="text"` only), send `WM_COMMAND(IDOK)` / `WM_COMMAND(IDCANCEL)`;
    - `system_dialog` windows are refused.
  - **The `NOTICE:` text** (Task 5) recommends the D2 path first: `ie_switch_window` or `target="hwnd:<n>"`.

- [ ] **Step 1: Write the failing unit tests**

```python
def test_windows_tool_lists_dialog_with_target(self)     # fake enumerate -> item kind "html_dialog", target "hwnd:42"
def test_windows_tool_works_while_driver_locked(self)
def test_close_window_hwnd_prefers_com_then_wm_close(self)  # COM fails -> PostMessage(WM_CLOSE) called
def test_system_dialog_never_clicked(self)               # ie_dialog/ie_close_window on system_dialog -> isError containing "ask the user"; no message sent
```

- [ ] **Step 2: Write the integration tests.** Use the D2 path(s). If D2 is `both`, run the modal tests once per path with `subTest`.

```python
# it_modal.py
def test_lookup_dialog_roundtrip(self)            # click open_lookup (D1 mode) -> ie_windows has html_dialog within 10s -> act in dialog -> click pick_A1 -> dialog gone -> lookup_code == "A1", lookup_name == "Alpha"
def test_lookup_dialog_grid_readable(self)        # with the dialog open: ie_grid (dialog target or switched window) returns 3 rows with codes A1, B2, C3
def test_confirm_dialog_return_value(self)        # ask -> click yes in dialog -> answer == "yes"
def test_nested_modal(self)                       # open_lookup -> nested -> nested_ok -> nested_result == "nested-ok" -> pick_B2 -> lookup_code == "B2"
def test_driver_click_on_modal_opener_does_not_hang_server(self)  # ie_click mode="driver" on open_lookup; next ie_windows returns within IE_TOOL_TIMEOUT+5s listing the dialog; ie_close_window closes it; ie_get lookup_code works again
# it_popups_alerts.py
def test_alert_accept_and_text(self)              # do_alert -> ie_dialog action=text == "Hello alert" -> accept -> no script_dialog in ie_windows
def test_alert_survives_screenshot_and_js(self)   # do_alert -> ie_screenshot and ie_js "return 1" (both may report the alert) -> ie_dialog action=text still == "Hello alert"
def test_confirm_dismiss(self)                    # do_confirm -> dismiss -> confirm_result == "false"
def test_popup_window(self)                       # do_popup clicked with mode "driver" (and "async" too if D8 = not blocked) -> ie_windows kind popup -> read "Popup window" via D2 path -> close_me -> back to main
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

### Task 9: Capture store and redaction

**Files:**
- Create: `ie_mcp/capture/__init__.py`, `ie_mcp/capture/redact.py`, `ie_mcp/capture/store.py`
- Modify: `ie_mcp/config.py`
- Test: `tests/test_redact.py`, `tests/test_capture_store.py`

**Interfaces:**
- Produces:
  - **`Redactor(field_re: str, header_re: str, enabled: bool = True)`.** It is thread-safe and keeps `secret_values: set[str]` (values ≥ 4 characters it has redacted). Methods:
    - `redact_headers(headers: list[tuple[str,str]]) -> list[tuple[str,str]]`;
    - `redact_url(url: str) -> str` (query-string fields);
    - `redact_body(content_type: str, body: bytes, charset: str | None) -> bytes` (form-urlencoded, multipart, JSON keys at any depth, XML element/attribute names);
    - `mask(text: str) -> str` (replaces every known secret value with `<redacted>`).

    Redacted values are added to `secret_values` before masking. With `enabled=False` every method returns its input.
  - **`NetworkEntry` dataclass:** `id: int, started: float, duration_ms: int, method: str, url: str, status: int | None, req_headers: list[tuple[str,str]], req_body: bytes, resp_headers: list[tuple[str,str]], resp_body: bytes, tunnel: bool, truncated: bool, error: str | None, source: str` (`"proxy"` or `"page"`), `referer_charset: str | None`.
  - **`CaptureStore(redactor: Redactor, max_entries: int = 2000, max_body: int = 262144, jsonl_path: Path | None = None)`** is thread-safe:
    - `add(entry) -> int` redacts in this order: URL, headers, request body, response body, then `mask` on all of them; it caps bodies and assigns the id;
    - `list(url_contains=None, method=None, status=None, since_id=0, limit=50) -> list[NetworkEntry]`;
    - `get(id) -> NetworkEntry | None`;
    - `clear() -> None`.

    JSONL is written only when `jsonl_path` is set (`IE_CAPTURE_JSONL=1`), after redaction.
  - **Settings:** `capture_redact`, `capture_redact_fields`, `capture_redact_headers`, `capture_max_body`, `capture_jsonl`.

- [ ] **Step 1: Write the failing tests**

```python
def test_redacts_password_field_in_form_body(self)     # "code=A1&pwd=s3cret" -> b"code=A1&pwd=<redacted>"
def test_redacts_query_string_field(self)              # "/x?user=a&token=abcd1234" -> "/x?user=a&token=<redacted>"
def test_redacts_json_and_xml_keys(self)               # {"a":{"password":"s3cret"}} and <pwd>s3cret</pwd>, <x password="s3cret"/> -> "s3cret" absent
def test_redacts_cookie_auth_and_custom_token_headers(self)  # Cookie, Authorization, X-Auth-Token values -> "<redacted>"; Content-Type unchanged
def test_masks_redacted_value_in_response_body(self)   # add entry with req pwd=s3cret and resp body '<dd id="echo_pwd">s3cret</dd>' -> resp_body has no "s3cret"
def test_mask_applies_to_later_entries_and_text(self)  # after the above, redactor.mask("value s3cret here") == "value <redacted> here"
def test_redaction_can_be_disabled(self)
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

- [ ] **Step 3: Implement** `redact.py` and `store.py`.

- [ ] **Step 4: Run the unit suite**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: capture store with value-tracking redaction"
```

---

### Task 10: Recording proxy

**Files:**
- Create: `ie_mcp/capture/proxy.py`
- Test: `tests/test_capture_proxy.py`, `tests/test_inject.py`

**Interfaces:**
- Consumes: `CaptureStore`, `NetworkEntry` (Task 9).
- Produces:
  - **`inject_into_html(body: bytes, snippet: bytes) -> bytes`.** The placement rule from CLAUDE.md "Injection rules":
    - skip a leading BOM and doctype;
    - inside `<head>`, insert after the leading run of `<meta …>`, `<title>…</title>` and `<base …>` elements (and whitespace/comments between them);
    - with no `<head>`, insert after `<html …>`;
    - with neither, insert right after the doctype, or at offset 0 when there is no doctype and no BOM.
  - **`RecordingProxy(store: CaptureStore, host: str = "127.0.0.1", port: int = 0, inject_html: bytes | None = None, upstream_proxy: tuple[str, int] | None = None)`** with `start() -> int`, `stop() -> None`, `requests_seen: int` and `first_request: threading.Event`.
    - **Requests:** forwards absolute-form HTTP requests and drops hop-by-hop headers. Each client connection keeps its own upstream `http.client.HTTPConnection` per host:port for its lifetime, so connection-based Windows authentication keeps working. With `upstream_proxy` set, it connects through it.
    - **CONNECT:** tunnels the bytes and records an entry with `tunnel=True`.
    - **Injection:** when `inject_html` is set and the response is `text/html`, status 200, it requests `Accept-Encoding: identity` upstream, applies `inject_into_html`, fixes `Content-Length`, drops `ETag`/`Last-Modified`, and sets `Cache-Control: no-store`. Responses it does not modify pass through byte-for-byte.
    - **Body caps:** bodies are capped by the store.

- [ ] **Step 1: Write the failing tests.** Proxy tests use `http.client` with absolute-form request lines (`conn.request("GET", "http://127.0.0.1:<p>/index.htm")` on a connection to the proxy), never urllib, so registry `ProxyOverride` and `NO_PROXY` cannot bypass it.

```python
# test_inject.py
def test_inject_after_leading_meta_title_base(self)   # <head><title>t</title><meta http-equiv="X-UA-Compatible" content="IE=7"><link ...> -> snippet right before "<link"
def test_never_inject_before_doctype_or_bom(self)     # b"\xef\xbb\xbf<!DOCTYPE html><html><head><script>" -> output starts with BOM + doctype; snippet after "<head>"
def test_inject_without_head_goes_after_html_tag(self)
def test_inject_without_html_goes_after_doctype(self)
def test_inject_into_bare_fragment_at_start(self)
# test_capture_proxy.py
def test_proxy_records_post_body_and_response(self)   # POST /echo code=A1 -> entry method "POST", status 200, b"code=A1" in req_body, b"echo_code" in resp_body
def test_proxy_returns_response_unchanged_without_injection(self)  # body bytes equal to a direct fetch; headers except hop-by-hop equal
def test_injected_response_is_not_cacheable(self)     # inject on -> Cache-Control "no-store"; no ETag/Last-Modified; Content-Length == len(body)
def test_non_html_never_injected(self)                # /api/lookup.xml unchanged
def test_connect_tunnel_recorded_as_tunnel(self)      # local TCP echo server via CONNECT -> entry.tunnel True; echoed bytes match
def test_client_connection_reuses_one_upstream_connection(self)  # two keep-alive requests on one client connection -> fixture server sees one TCP connection
def test_upstream_proxy_chaining(self)                # second RecordingProxy as upstream -> both record the request
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_inject tests.test_capture_proxy -v`
Expected: FAIL.

- [ ] **Step 3: Implement** `inject_into_html` and `RecordingProxy`.

- [ ] **Step 4: Run the unit suite**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: recording proxy with document-mode-safe injection"
```

---

### Task 11: Page instrumentation and console tools

**Files:**
- Create: `ie_mcp/js/instrument.js`, `ie_mcp/js/drain.js`, `ie_mcp/capture/console.py`, `ie_mcp/tools/console.py`
- Modify: `ie_mcp/protocol.py` (register the post-hook and the dialog drain), `ie_mcp/config.py`
- Test: `tests/test_console.py`, `tests/integration/it_console.py`

**Interfaces:**
- Consumes: `POST_HOOKS`, `MODAL_PROBE` (Task 5); `Backend.js`/`frames`, `ComBackend` (Task 7); `Redactor.mask`, `CaptureStore` (Task 9); D3 and D5 (Task 2).
- Produces:
  - **`instrument.js`** is ES3 and idempotent through `window.__iemcp`. It keeps a queue `window.__iemcp.q` of records `kind\x1flevel\x1ftime\x1ftext` (separators inside text escaped as `\\x1e`/`\\x1f`). It records:
    - **`error`:** from `window.onerror`, chained to any existing handler, keeping its return value;
    - **`dialog`:** `alert`/`confirm`/`prompt` with their result;
    - **`modal`:** `showModalDialog` URL, features, a one-level summary of `dialogArguments` (string/number/boolean own properties) and the return value;
    - **`open`:** `window.open` URL, name and features;
    - **`console`:** only if `window.console` already exists, wrapping only the methods that exist. When `window.__iemcp_stub` is true (`IE_CONSOLE_STUB=1`), it first creates a complete no-op console (`log info warn error debug dir dirxml group groupCollapsed groupEnd time timeEnd count assert trace table clear profile profileEnd`);
    - **`xhr`:** only when `location.protocol == "https:"` or `window.__iemcp_wrap_xhr` is true (proxy off, or D3 = `instrument-only`). It wraps native `XMLHttpRequest` `open`/`send`/`onreadystatechange`, recording method, URL, request body, status and response length. ActiveX `XMLHTTP` objects are never wrapped.

    It calls the original host functions directly, branching on `arguments.length`. Popup windows forward their records to `opener.__iemcp.q` when same-origin.
  - **`drain.js`** returns the joined queue and empties it.
  - **`ConsoleRecord` dataclass** `id: int, kind: str, level: str, time: float, text: str, where: str` (`where` = window target and frame path).
    - `parse_records(raw: str, where: str) -> list[ConsoleRecord]`.
    - `ConsoleStore(redactor: Redactor, max_records: int = 5000)` with `add_many` (applies `redactor.mask`), `list(since_id=0, kinds=None, limit=100)` and `clear`.
    - `xhr` records are also added to the `CaptureStore` as `NetworkEntry(source="page")`.
  - **`instrument_hook`, registered in `POST_HOOKS`:** for every same-origin frame of the current driver window, injects `instrument.js` (with the stub/xhr flags set first) where `typeof window.__iemcp == 'undefined'`, then drains into the `ConsoleStore`.
  - **`dialog_drain`:** runs after every tool call without the driver lock. For each `html_dialog` that `MODAL_PROBE` reports, it drains through `ComBackend(hwnd).js` (and injects if missing). Records of a dialog that closes between two calls are lost; the README says so.
  - **Settings:** `instrument: bool` (`IE_INSTRUMENT`), `console_stub: bool` (`IE_CONSOLE_STUB`).
  - **Tools** (`needs_driver=False`): `ie_console(since_id=0, kinds=None, limit=100)` and `ie_console_clear()`.
  - **`ie_status`** adds `instrumented`, `console_stub` and `xhr_wrapped`.
  - **`ie_dialog`'s Win32 fallback** (Task 8) now confirms a `script_dialog` window against a `dialog` record in the `ConsoleStore` from the last 30 s before sending `WM_COMMAND`.

- [ ] **Step 1: Write the failing unit tests**

```python
def test_parse_records_splits_fields_and_unescapes(self)  # "error\x1ferror\x1f1.5\x1fboom \\x1f x" -> text "boom \x1f x"
def test_store_since_id_and_kind_filter(self)
def test_console_text_is_masked(self)                     # redactor knows "s3cret" -> stored text has "<redacted>"
def test_xhr_records_become_page_network_entries(self)    # xhr record -> CaptureStore entry source "page", method/url parsed
def test_hook_injects_only_where_missing(self)            # fake backend: frame A has __iemcp, frame B not -> inject called for B only
def test_dialog_drain_uses_com_backend(self)              # probe returns html_dialog hwnd 7 -> ComBackend(7).js called with drain.js
```

- [ ] **Step 2: Write the integration tests**

```python
def test_late_error_and_console_captured(self)        # errors.htm, click late_error -> error record containing "late-error";
                                                      # console "fixture-log" recorded only if D5 says console exists in that mode
def test_alert_recorded(self)                         # dialogs.htm do_alert -> dialog record "Hello alert" (then ie_dialog accept)
def test_modal_call_recorded_with_arguments(self)     # modal flow -> modal record containing "modal_dialog.htm" and "sCode"
def test_dialog_records_drained(self)                 # with lookup dialog open, ie_console contains records with where startswith "hwnd:"
def test_instrumentation_works_in_quirks_and_ie7(self)  # quirks.htm and form.htm -> ie_js "return typeof window.__iemcp" == "object"
def test_no_console_created_by_default(self)          # on a page where D5 says console is absent: ie_js "return typeof window.console" == "undefined"
```

(Load-time errors need the proxy injection; Task 12 tests them.)

- [ ] **Step 3: Run them to verify they fail**

Run: `python -m unittest tests.test_console -v`, then `python scripts/run_integration.py it_console`
Expected: FAIL.

- [ ] **Step 4: Implement** the scripts, store, hooks and tools.

- [ ] **Step 5: Run unit and integration suites**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`, then `python scripts/run_integration.py`
Expected: OK, no skips.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: minimal ES3 page instrumentation and console tools"
```

---

### Task 12: Capture wiring, network tools and early injection

**Files:**
- Create: `ie_mcp/tools/network.py`
- Modify: `ie_mcp/session.py` (start the proxy before the driver, verify it, stop it in `close`), `ie_mcp/config.py`, `ie_mcp/tools/diagnostics.py` (`ie_status`)
- Test: `tests/test_network_tools.py`, `tests/integration/it_network.py`

**Interfaces:**
- Consumes: `RecordingProxy`, `inject_into_html` (Task 10); `CaptureStore`, `Redactor` (Task 9); `instrument.js` (Task 11); `build_options(proxy_port=…)` (Task 4); D3 and D4 (Task 2).
- Produces:
  - **Session start when `IE_CAPTURE=1` and D3 = `proxy`:**
    - start `RecordingProxy` with `inject_html = <script>` + flags + `instrument.js` + `</script>` when `IE_INSTRUMENT=1`, and `upstream_proxy` from `IE_CAPTURE_UPSTREAM_PROXY`;
    - build options with `proxy_port`;
    - after creation, wait up to 5 s for `proxy.first_request`. If no request was seen, set `IeSession.capture_active = False` and add a `NOTICE: network capture is off: the browser did not use the proxy` to the `ie_open` result.
  - **When D3 = `instrument-only`:** the proxy is not started, `capture_active = False`, and instrumentation sets `__iemcp_wrap_xhr = true`.
  - **`ie_status`** adds `capture` (`"proxy"`, `"page-only"`, `"off"`) and `capture_note`.
  - **Tools** (`needs_driver=False`):
    - `ie_network(url_contains=None, method=None, status=None, since_id=0, limit=50)`: one line per entry, `id source method status duration_ms size url`;
    - `ie_request(id: int, body_limit: int = 20000)`: headers and bodies decoded with the charset from `Content-Type`, then `referer_charset` (the charset captured for the Referer document), then `cp1252`. Binary bodies are summarised as `<binary N bytes>`;
    - `ie_network_clear()`.

    Every result says "HTTPS: tunnel only (host, timing)" when tunnel entries are present.
  - **`IeSession.close()`** stops the proxy and deletes session artefacts (JSONL, screenshots) unless `IE_KEEP_ARTIFACTS=1`.

- [ ] **Step 1: Write the failing unit tests**

```python
def test_request_body_decoded_with_page_charset(self)   # entry with Content-Type form, referer_charset "windows-1252", body b"name=M%FCller" -> ie_request text contains "Müller"
def test_utf8_body_decoded(self)                         # body "name=M%C3%BCller", Content-Type charset=utf-8 -> "Müller"
def test_network_tool_line_format(self)                  # one fake entry -> "1 proxy POST 200 12 345 http://…/echo"
def test_status_reports_capture_off_when_proxy_unused(self)  # session with capture_active False -> ie_status capture "off" and capture_note set
def test_artifacts_deleted_on_close(self)                # temp session dir with screenshots/x.png -> close() removes it unless IE_KEEP_ARTIFACTS=1
```

- [ ] **Step 2: Write the integration tests.** If D3 = `instrument-only`, write only `test_native_xhr_recorded_as_page_entry` and `test_document_mode_unchanged_by_injection` (the latter with instrumentation injected after load), and state in the README that form posts and document loads are not captured.

```python
def test_form_post_captured_with_redaction(self)     # form.htm fill code=A1, pwd=s3cret, submit -> ie_network has POST …/echo;
                                                     # ie_request shows "code=A1"; "s3cret" absent from the request AND the echoed response
def test_activex_xhr_captured(self)                  # xhr.htm load -> POST /echo with body "q=ping"
def test_native_xhr_recorded_as_page_entry(self)     # proxy off (IE_CAPTURE=0 session) -> xhr.htm load_native -> entry source "page"
def test_dialog_document_requests_captured(self)     # modal flow -> GET modal_dialog.htm and GET /api/lookup.xml?q= present
def test_load_time_error_captured(self)              # errors.htm -> ie_console error record containing "undefinedFunctionCall"
def test_document_mode_unchanged_by_injection(self)  # ie_js "return document.documentMode" on index.htm, form.htm, quirks.htm
                                                     # == the values the spike recorded for Q6 without injection (expected 11, 7, 5)
def test_request_body_decoded_with_page_charset_live(self)  # charset.htm submit name="Müller – café" -> ie_request text contains it
```

- [ ] **Step 3: Run them to verify they fail**

Run: `python -m unittest tests.test_network_tools -v`, then `python scripts/run_integration.py it_network`
Expected: FAIL.

- [ ] **Step 4: Implement** the wiring and tools.

- [ ] **Step 5: Run unit and integration suites**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`, then `python scripts/run_integration.py`
Expected: OK, no skips.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: per-process network capture, network tools and early instrumentation"
```

---

### Task 13: Window screenshots (rendered view including dialogs)

**Files:**
- Create: `ie_mcp/png.py`, `ie_mcp/win32/capture.py`
- Modify: the module holding `ie_screenshot` (Task 3 puts it in `ie_mcp/tools/read.py`), `ie_mcp/cli.py` (call `enable_dpi_awareness()` at start-up, before any window call)
- Test: `tests/test_png.py`, `tests/integration/it_screenshot.py`

**Interfaces:**
- Consumes: `enumerate_windows`, `modal_probe`, `SESSION.main_hwnd` (Task 6).
- Produces:
  - **`png.encode_png(width: int, height: int, rgba: bytes) -> bytes`:** 8-bit RGBA, filter 0, stdlib `zlib`/`struct` only.
  - **`win32.capture.enable_dpi_awareness() -> None`:** `SetProcessDpiAwarenessContext(-4)` (per-monitor v2). Failures are ignored.
  - **`win32.capture.capture_window(hwnd: int) -> tuple[int, int, bytes]`:** `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT=2)` into a 32-bit DIB section, converted from BGRA to RGBA.
  - **`ie_screenshot` gains `scope`:**
    - `page` (default, driver);
    - `element` (driver, `by`/`value`);
    - `window` (Win32: `main_hwnd` plus every open `html_dialog`/`script_dialog`, one image each, in z-order; `needs_driver=False`).

    A `target` of `hwnd:<n>` captures that window only. The Task 4 rules for `save_path`/`no_inline` are kept.

- [ ] **Step 1: Write the failing tests**

```python
def test_encode_png_signature_and_ihdr(self)    # startswith b"\x89PNG\r\n\x1a\n"; IHDR width 3, height 2
def test_encode_png_roundtrip_pixels(self)      # 3x2 image; zlib.decompress(IDAT), strip one filter byte per row -> equals input
# integration
def test_window_screenshot_includes_open_dialog(self)  # modal open -> scope=window returns 2 image items; second image size within 10% of the dialog window rect
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m unittest tests.test_png -v`, then `python scripts/run_integration.py it_screenshot`
Expected: FAIL.

- [ ] **Step 3: Implement** the encoder, DPI awareness, capture and `scope`.

- [ ] **Step 4: Run the commands from Step 2**

Expected: OK, no skips.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: DPI-aware window screenshots including modal dialogs"
```

---

### Task 14: Attach to an IE-mode tab the user opened

**Files:**
- Create: `ie_mcp/tools/attach.py`
- Modify: `ie_mcp/win32/windows.py`, `ie_mcp/protocol.py` (refuse driver tools while attached)
- Test: `tests/test_attach.py`, `tests/integration/it_attach.py`

**Interfaces:**
- Consumes: `ie_server_child`, `com_apartment` (Task 6); `ComBackend`, `resolve_backend`, `DEFAULT_TARGET` (Task 7); `instrument_hook` logic (Task 11); session owner records (Task 4).
- Produces:
  - **`TabInfo` dataclass:** `hwnd: int` (the `Internet Explorer_Server` window), `top_hwnd: int`, `pid: int`, `title: str`, `url: str`.
  - **`windows.ie_mode_tabs(enum_fn=None) -> list[TabInfo]`:** visible top windows of `msedge.exe` process trees that have an `Internet Explorer_Server` descendant. It **excludes** windows in the process trees of other live ie-mcp sessions (from their owner records). The URL is read through COM when pywin32 is present, else `""`.
  - **Tools** (`needs_driver=False`):
    - `ie_tabs()`;
    - `ie_attach(hwnd: int)` sets `protocol.DEFAULT_TARGET = f"tab:{hwnd}"` and injects/drains instrumentation through `ComBackend`;
    - `ie_detach()` clears it.
  - **While `DEFAULT_TARGET` is set,** every tool without an explicit target whose `needs_driver(args)` is true is refused with `isError` "attached to tab <hwnd>: this tool needs the driver session; call ie_detach first". Such a tool never starts a browser.
  - **Documented limits** (README and tool descriptions): on attached tabs there are no keys, hover, history or page screenshot, and no proxy capture. Window screenshots and console work.

- [ ] **Step 1: Write the failing tests**

```python
def test_ie_mode_tabs_finds_windows_with_ie_server(self)   # fake enum -> one TabInfo with hwnd of the IE server child
def test_ie_mode_tabs_excludes_other_live_sessions(self)   # window in another live session's tree -> excluded
def test_attach_sets_and_detach_clears_default_target(self)
def test_driver_tools_refused_while_attached(self)         # DEFAULT_TARGET "tab:5"; ie_wait_ready -> isError containing "ie_detach"; SESSION.ensure not called
def test_resolve_tab_target_uses_com_backend(self)
# integration
def test_attach_reads_ie_mode_tab(self)                     # ie_open form.htm; ie_tabs lists it; ie_attach(hwnd); ie_text frame "inner" (no target) contains "Inner frame"; ie_detach
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

### Task 15: Selftest, end-to-end MCP test, docs and release

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
    - proxy saw the request and injection kept `documentMode`;
    - instrumentation present;
    - a modal dialog detected and closed;
    - no leftover processes.

    The last line is `RESULT: PASS|FAIL`; the exit code is 0/1.
  - **README:**
    - a fork notice crediting upstream;
    - the tool table (all tools, with `target`, `scope`, `mode`, `force`);
    - the env var table (every variable and default);
    - a **Fidelity** section (copy of the spec table);
    - a **Security** section (redaction scope, artefact deletion, file confinement, system dialogs never clicked, what is never changed);
    - **Limits:** HTTPS is tunnel-only apart from page XHR; PAC files are unsupported; the proxy may change the IE security zone of intranet hosts; dialog records can be lost on fast close; attached tabs have no driver features;
    - troubleshooting rows: driver not found, capture off, modal not reachable, pywin32 missing, alerts.
  - **`CHANGELOG.md` entry `0.2.0`:** the features, plus every changed default:
    - `IE_INTERACTION_MODE=background`;
    - `IE_CLEAN_SESSION=0`;
    - uploads disabled without `IE_UPLOAD_DIR`;
    - no implicit `../.pydeps`;
    - no hardcoded driver path;
    - `unhandledPromptBehavior` set to `ignore`;
    - Python ≥ 3.10.

- [ ] **Step 1: Write the end-to-end test** (`tests/integration/it_e2e_mcp.py`). It launches `python -m ie_mcp` as a subprocess and speaks JSON-RPC over stdin/stdout:

```python
def test_full_session_over_stdio(self):
    # initialize -> tools/list includes ie_windows, ie_network, ie_console, ie_tabs, ie_close_window
    # ie_open frameset.htm -> ie_wait_ready loaded -> ie_text frame "main" contains "code"
    # fill + submit -> ie_network has POST /echo
    # ie_open modal_opener.htm -> click open_lookup -> NOTICE in result or ie_windows lists html_dialog -> pick_A1 via D2 path -> lookup_code == "A1"
    # ie_console has no 'error' records for these pages
    # ie_screenshot scope=window returns >= 1 image
def test_server_exit_leaves_no_browser(self):
    # with a modal dialog open: close stdin -> process exits within 20s -> none of the recorded PIDs remain
```

- [ ] **Step 2: Run it and fix any gap it exposes**

Run: `python scripts/run_integration.py it_e2e_mcp`
Expected: OK, no skips. Any failure points at a gap in Tasks 1–14; fix it there, with its own test, before continuing.

- [ ] **Step 3: Implement the selftest checks; write the README, CHANGELOG and CLAUDE.md updates**

- [ ] **Step 4: Run everything**

Run: `python -m unittest discover -s tests -p "test_*.py" -v`, then `python scripts/run_integration.py`, then `pip install . && ie-mcp --selftest` (from a non-editable install, to prove the fixtures ship)
Expected: all OK, no skips; `RESULT: PASS`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "docs: 0.2.0 selftest, end-to-end test, README and changelog"
```
