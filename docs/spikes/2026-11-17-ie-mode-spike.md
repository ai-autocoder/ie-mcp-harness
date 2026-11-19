# IE-mode spike: results and decisions (plan Task 2)

Run on 2026-11-18 with `scripts/spike_ie_mode.py` against the fixture site
(`ie_mcp_fixtures`), on the development machine:

- Windows 11 Pro 10.0.26200, Microsoft Edge 146 (IE mode, `InternetExplorerIntegrationLevel=1`
  policy set by IT), IEDriverServer 4.14.0 (Win32), selenium 4.52.0, pywin32 312, Python 3.14.
- Default Internet Options: `127.0.0.1` maps to the **Internet** zone, `localhost` to
  **Local intranet** (`MapUrlToZone`, read-only).
- Global proxy settings: `ProxyEnable=0`, `ProxyOverride=<local>`, no PAC.

Every spike session sets `ensure_clean_session=False`, `unhandled_prompt_behavior="ignore"`,
`page_load_strategy="none"`, starts IEDriver with `__COMPAT_LAYER` removed from its
environment (D16), and kills only its own Edge (matched by the IEDriver temp profile in the
command line). The raw output of the final full run is summarised in the Evidence column;
rerun the script to reproduce it.

Three interaction variants were measured where input matters:

| Label | `native_events` | `require_window_focus` | Upstream name |
|---|---|---|---|
| `native` | True | True | `IE_INTERACTION_MODE=native` |
| `background` | False | False | `IE_INTERACTION_MODE=background` |
| `native-nofocus` | True | False | (new; see D17) |

## Questions

| Question | Answer | Evidence |
|---|---|---|
| Q0 Trident | Yes | userAgent `… Trident/7.0; … rv:11.0) like Gecko`; `index.htm` documentMode 11. |
| Q1 Driver click on a `showModalDialog` opener blocks? (Q1R: repeated input per variant, see D17) | **No** (native and native-nofocus): `click()` returns in < 1 s with the dialog open; the driver stays usable. In `background` the driver click does **nothing** (`ElementNotInteractableException`, no dialog). | Q1 lines per variant. |
| Q2 Dialogs/popups in `window_handles`; opener script while a dialog is open | Both the lookup dialog and the `window.open` popup appear as new handles in every variant. `switch_to.window(dialog)` + `execute_script` works (title `Fixture lookup dialog`); a native driver click on `pick_A1` inside it fills `lookup_code=A1`. `execute_script("return 1")` on the opener with the dialog open returns at once (does not block). | Q2 lines. |
| Q3 Win32/COM | Content and dialogs live in the Edge process tree (IE content process, same PID for main IE server, dialog and alerts). Dialog = `Internet Explorer_TridentDlgFrame` owned by an `Alternate Modal Top Most` helper window (same PID, no owner), with one `Internet Explorer_Server` child. `WM_HTML_GETOBJECT` returns the dialog's document (title, body, documentMode 7). `execScript` runs in the documentMode 7 and 11 documents tested (modes 8–10 were not tested). Reading a window expando back: `getattr` works in IE7 mode but **fails in IE11 mode**; `IDispatchEx.GetDispID(name, fdexNameCaseSensitive)` + `InvokeEx` works in both. win32com dynamic dispatch of DOM methods (`getElementById`, `createElement`) also fails in IE11 mode. No `RPC_E_CALL_REJECTED`/`RETRYLATER` was seen: the opener's document was readable while a dialog was open, and the owner's document while an alert was open. A worker thread with its own `CoInitialize` obtained and used its own document. Keys: posted `WM_KEYDOWN`+`WM_KEYUP` → exactly one keypress and `x` (IE7 dialog and IE11 page); `WM_CHAR` only → one keypress; `WM_KEYDOWN`+`WM_CHAR`+`WM_KEYUP` → two; `fireEvent('onkeypress')` → one keypress, but no character in the field. Enter via `WM_KEYDOWN`+`WM_KEYUP` in `search` → `search_count=1`. Navigation `index.htm` → `form.htm`: every hwnd from the `Internet Explorer_Server` up to the top window survived (same hwnds). | Q3 lines; chain `Internet Explorer_Server` → `Shell DocObject View` → `TabWindowClass` → `Frame Tab` → `IEFrame` → `Chrome_WidgetWin_2` → `Chrome_WidgetWin_1` (top). |
| Q4 Per-process proxy | With `noProxy: ["<-loopback>"]` and `use_per_process_proxy=True`, loopback traffic **is** proxied. Seen: later navigations, frames, popup, modal dialog, dialog XMLHTTP, page XMLHTTP (ActiveX and native). **Not seen: the initial URL** (`initial_browser_url`), on `127.0.0.1` and on `127.0.0.2`. Document requests send `Accept: text/html, application/xhtml+xml, image/jxr, */*`; XMLHTTP (ActiveX and native) sends `Accept: */*`. HKCU Internet Settings identical before and after. | Q4 lines (request log with `Accept`). |
| Q5 `console` | documentMode 11 (`index.htm`, and `quirks.htm`, which also runs in mode 11): `object`, `console.log` a function (the F12 tools were never opened in these sessions). documentMode 7 (`form.htm`): `undefined`. Mode 5 was not observed (see Q6). | Q5 line. |
| Q6 Modes and charsets | Every page is measured after a navigation (never as the session's initial URL, which is not proxied), and the proxy's request log confirms each page went through it in runs (b) and (c). Identical in all three runs (no proxy / proxy / proxy+loader): `index` 11 `utf-8`, `form` 7, `quirks` **11 (`BackCompat`)**, `headless` 7, `nomode` 11, `latecharset` 11 `windows-1252`, `utf8` 11 `utf-8`, `/progressive?ms=800` 7; `nomode.htm` on the `localhost` origin (own session, started on its `index.htm`) 11. The decoded `sample_text` (`Müller – café`) was intact on `latecharset`, `utf8` and the others in every run. The loader ran (`window.__inj` set) on every measured page. Reading `frames['foreign'].document` on `xorigin.htm` throws `Permission denied`. Limits: `windows-1252` is also this machine's default charset, so `latecharset.htm` cannot tell a honoured meta from the default (`utf8.htm`, whose UTF-8 meta is early in the head, is the non-default case); no fixture puts a non-default charset meta near IE's prescan limit. The spike's proxy buffers each whole response, so `/progressive` shows the loader keeps the mode, not that streaming insertion is safe (Task 11 tests that). | Q6 comparison lines (`a/b/c`, `text_ok`, `proxied`). |
| Q7 Pop-up blocker vs async click | Not blocked in any variant, on the `127.0.0.1` (Internet zone) origin. IEDriver's Edge command line (seen while diagnosing a hang, not measured by the script) includes `--disable-popup-blocking`. | Q7 lines. |
| Q8 Script dialogs with `ignore` | The alert survives `execute_script` (which raises `UnexpectedAlertPresentException`) and `get_screenshot_as_png` (which succeeds); `switch_to.alert.text == "Hello alert"` afterwards. Shapes below. The driver reaches alert, confirm **and prompt**. An alert raised inside the lookup dialog is owned by the dialog's `Internet Explorer_TridentDlgFrame`; `WM_COMMAND` with its button ID closes it and the page continues (`dialog_alert_result=alerted`). A confirm (TaskDialog) answers `WM_COMMAND` 1 → `true`, 2 → `false`. | Q8 lines. |

### Script-dialog window shapes (Q8)

The script dialogs are top-level `#32770` windows titled as shown (the last row, for comparison, is the lookup dialog), owned by the `Alternate Modal Top Most`
helper of the page (or by the `Internet Explorer_TridentDlgFrame` of a `showModalDialog`
window when raised inside one). Children are (class, control ID, text):

| Kind | Title | Style / ExStyle | Children | Accept | Dismiss |
|---|---|---|---|---|---|
| alert | `Message from webpage` | `0x94c801c5` / `0x10101` | `Button 2 "OK"`, `Static 20 ""`, `Static 65535 <message>` | `WM_COMMAND 2` | (same) |
| confirm | `Message from webpage` | `0x96c80284` / `0x10101` | `DirectUIHWND 0` (a TaskDialog: no button or static controls; the message is not readable through child windows) | `WM_COMMAND 1` | `WM_COMMAND 2` |
| prompt | `<host> needs some information` | `0x94c800c4` / `0x10101` | `Button 1 "OK"`, `Button 2 "Cancel"`, `Button 57089 <don't let this page create more messages>`, `Edit 8133`, `Static 8132 <message>`, `Static 65535 "Script Prompt:"` | driver only | driver only |
| `showModalDialog` | `<title>-- Web page Dialogue` (localised) | `0x96c80000` / `0x101` | `Internet Explorer_Server 0` | — | — |

## Decisions

| Decision | Value | Reason |
|---|---|---|
| D1 | `IE_CLICK_MODE` default `driver` for `native` and for `background` as redefined by D17 | A driver click never blocked on a `showModalDialog` opener in any variant (Q1); with synthetic events (upstream `background`) the driver click does nothing, so `async` would be the only working click there. |
| D2 | `both` | Dialogs are driver handles (Q2) and reachable over COM (Q3). |
| D3 | `proxy` | Q4: the per-process proxy sees everything except the session's initial URL. |
| D4 | `works` | Q4: loopback traffic reached the proxy with `noProxy: ["<-loopback>"]`. |
| D5 | mode 11: yes; mode 7: no; mode 5: not observed | Q5. |
| D6 | `edge-tree` | Q3: content, dialogs and alerts belong to processes in the Edge tree. Script dialogs and `showModalDialog` windows are owned by an `Alternate Modal Top Most` helper (no owner of its own, same PID), so `owner_in_session` must treat a helper window in the session's PIDs as a session window. |
| D7 | Script path: `execScript` (measured in modes 7 and 11). Read-back: `IDispatchEx.GetDispID(name, 1)` + `InvokeEx(DISPATCH_PROPERTYGET)` (works in IE7 and IE11; `getattr` fails in IE11). Do every DOM access in script, never through win32com dynamic dispatch of DOM methods. Worker-thread apartment: works. Rejected calls: none seen (keep the 1 s retry). Key path: `keydown-keyup` in IE7 and IE11 modes. | Q3. |
| D8 | no (not blocked) | Q7 (Internet-zone origin only). |
| D9 | yes | Q2. |
| D10 | yes | Q4: documents send `text/html` in `Accept`; XMLHTTP sends `*/*`. |
| D11 | Proxied: later navigations, frames, popups, modal dialogs, dialog XMLHTTP, page XMLHTTP. **Not proxied: the initial URL** (`initial_browser_url`). | Q4. |
| D12 | no | Q6: `nomode.htm` stays 11 with the proxy on both origins (`127.0.0.1`, Internet zone; `localhost`, Local intranet zone), each reached by a proxied navigation. `IE_CAPTURE` defaults to `1`. |
| D13 | yes | Q6: `Permission denied`. |
| D14 | Class `#32770`; rule: a `#32770` with an `Edit` child (control ID 8133) is a prompt. Alert/confirm have no `Edit`; a `showModalDialog` window is `Internet Explorer_TridentDlgFrame`. Driver reach: **yes**. | Q8. |
| D15 | `ie-server` | Q3: the `Internet Explorer_Server` hwnd (and every ancestor) survived a top-level navigation within one zone. |
| D16 | IEDriver's environment drops `__COMPAT_LAYER` | Recorded before the spike (ledger): with it inherited, Edge relaunches itself and IEDriver cannot attach. |
| D17 | `IE_INTERACTION_MODE=background` becomes `native_events=True`, `require_window_focus=False` | Synthetic events (`native_events=False`) do nothing on this machine: driver `click` raises `ElementNotInteractableException` or silently does nothing, `send_keys` leaves the value empty, in IE7 and IE11 documents. With native events and no focus requirement, click, `send_keys` and form submit work in IE7 and IE11 modes, also while another application has the foreground. Q1R (two runs, another window in front): 5 of 6 clicks opened the lookup dialog each time (one miss, at a different click each run) and 4 of 4 `send_keys` typed exactly. Upstream's `native` (`require_window_focus=True`, `SendInput`) opened it 1 of 6 times, and in one run typed the user's own keystrokes into the field (`'A0ld '`, `'UA1i'`); synthetic `background` 0 of 6 and 0 of 4. `native` stays available as the explicit fallback. A click can still be lost now and then: tools that depend on one should check its effect. |

## Other findings

- **Session creation can hang** inside IEDriver after Edge starts (log ends at "Using existing
  system proxy settings"), seen intermittently in any variant. Selenium's client then waits for
  its 120 s read timeout. The spike creates sessions in a thread with a 60 s join and retries;
  the server's attach retries (and the finite HTTP timeout from Task 4) cover it.
- Selenium's JS atoms fail on these pages (`is_displayed`, `get_attribute` raise
  `JavascriptException`), as CLAUDE.md already says.
- `driver.quit()` can hang when a dialog was left open; kill the session's own IEDriver after a
  bounded wait.
- A `confirm`'s message is not readable through child windows (TaskDialog). Its text comes from
  the driver (`switch_to.alert.text`) or from the instrumentation's `dialog-open` record.
- The alert's only button has ID 2 (`IDCANCEL`), not 1.
- `quirks.htm` (no doctype, no `X-UA-Compatible` meta) runs in documentMode 11 with
  `compatMode == "BackCompat"`, not in mode 5, on this machine (one fixture page). Later tests
  expect 11, 7, 11, 7, 11 for `index`, `form`, `quirks`, `headless`, `nomode`.
- Every decision rests on one machine, one Edge build and one IEDriver build. D17 rests on two
  runs of 6 clicks and 4 typings per variant; about 1 click in 6 was lost, so later tasks should
  check a click's effect where they can (Task 6/9 tests wait for the expected result, not for
  the click call to return).

## Stop rules

None triggered: Q2 and Q3 both reach modal dialogs; Internet Settings were unchanged; the alert
was not dismissed under `ignore`; loopback is proxied (D4 = `works`); the loader changed no
document mode or charset on the fixture pages (within the Q6 limits); the prompt is told apart by its `Edit` child (D14); the IE server
hwnd survives navigation (D15).
