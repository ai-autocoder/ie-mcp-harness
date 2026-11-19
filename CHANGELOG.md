# Changelog

`ie-mcp-harness` started from [thomfilg/ie-mcp](https://github.com/thomfilg/ie-mcp) (MIT).
`0.1.0` is that project's last release, the one this project was built on.

## 1.0.0 — 2026-11-19

First release of ie-mcp-harness.

The harness: full access to an IE-mode page without changing how it behaves.

### Added

- **Package layout.** The single `ie_mcp.py` is the `ie_mcp` package (`python -m ie_mcp`, console
  script `ie-mcp`); `ie_mcp_fixtures` (the generic legacy fixture site) ships with it.
- **Modal dialogs, pop-ups and attached tabs over COM.** `target="hwnd:<n>"` / `"tab:<n>"` on the
  read and interact tools; `ie_windows`, `ie_switch_window`, `ie_close_window`; `ie_tabs`,
  `ie_attach`, `ie_detach` for tabs you opened yourself. Optional extra `ie-mcp[win32]` (pywin32).
- **Network capture.** A local recording proxy that only the IE-mode browser uses
  (`IE_CAPTURE`, on by default): `ie_network`, `ie_request`, `ie_network_clear`, redacted bodies,
  verified on the first http(s) navigation (`ie_status` `capture`).
- **Page instrumentation** (`IE_INSTRUMENT`, on by default): script errors, `alert`/`confirm`/
  `prompt`, `showModalDialog`, `window.open`, `console` and XHR records for `ie_console`
  (`ie_console_clear`); a tiny loader the proxy inserts so it runs before the page's scripts.
- **`ie_dialog`** answers script dialogs by window handle, after proving they are the
  session's own, including the TaskDialog form (needs a matching console record).
- **`ie_screenshot scope=window`**: DPI-aware PrintWindow capture of the session's windows,
  open dialogs included; `target=hwnd:<n>` for one window.
- **`ie_status`** reports `capture`, `capture_note`, `instrumented` and `modifications`: every
  change the harness makes to pages, requests and input, each with its off switch.
- **Tool-call watchdog**: no call can hang the server (`BUSY:`/`BLOCKED:` answers,
  `IE_TOOL_TIMEOUT`); async clicks (`ie_click mode=async`, `IE_CLICK_MODE`).
- **Identity-checked process cleanup**: only processes recorded with image name and creation time
  are killed.
- **`ie-mcp --selftest`** now runs against the bundled fixture site: dependencies and paths, an
  IE-mode session (Trident, `documentMode` 7), proxy and loader, instrumentation, a modal
  dialog and a leftover-process check; one PASS/FAIL/SKIP line each and `RESULT: PASS|FAIL`.
- End-to-end test over stdio (`tests/integration/it_e2e_mcp.py`) and an integration runner that
  fails on skipped tests (`scripts/run_integration.py`).
- README: origin and credits note, Fidelity, Limits and Security sections, troubleshooting rows.

### Changed behaviour (versus 0.1.0)

- `IE_INTERACTION_MODE` defaults to `background` (native input without taking window focus or the
  physical mouse); `native` is the compatibility fallback.
- `IE_CLEAN_SESSION` defaults to `0`: the user's IE-mode cookie store and cache are no longer
  cleared when a session starts and ends. `IE_CLEAN_SESSION=1` brings it back.
- Uploads are disabled unless `IE_UPLOAD_DIR` is set, and only files inside it can be sent.
- No implicit `../.pydeps`: only `IE_PYDEPS` adds a vendored-selenium folder to the import path.
- No hardcoded IEDriver path: `IE_DRIVER_PATH`, else the highest version in the per-user selenium
  cache; a path that does not exist is an error.
- IEDriver's `unhandledPromptBehavior` is `ignore`, so a driver command no longer silently
  cancels an open `alert`/`confirm`.
- Network capture and page instrumentation are **on by default** (`IE_CAPTURE=1`,
  `IE_INSTRUMENT=1`): the browser is pointed at a loopback recording proxy and pages get a small
  loader script. `IE_CAPTURE=0` and `IE_INSTRUMENT=0` turn them off; every change is listed in
  the README's Fidelity section.
- `ie_frames` returns each frame's index, name, id and src.
- Screenshots saved by name (`save_path`, a bare file name) go to the session's `screenshots`
  folder and are kept after `ie_close`.
- `ie_close` gains `force`: it never talks to the driver, cancels running waits and kills only
  this session's own Edge and IEDriver after an identity check.
- The HTTP client timeout to the driver is 300 seconds (a dead driver can no longer hang a call
  forever).
- Python 3.10 or newer (current selenium requires it).
- The logs redact secrets by default (`IE_CAPTURE_REDACT`).
