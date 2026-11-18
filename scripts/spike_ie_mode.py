#!/usr/bin/env python3
"""Plan Task 2 spike: answer the open questions on a real Edge IE-mode session.

Raw Selenium plus ctypes/pywin32; it does not use the ie_mcp server code.

Usage:
    python scripts/spike_ie_mode.py            # every question
    python scripts/spike_ie_mode.py q3 q8      # some questions

Every session sets ``ensure_clean_session = False`` (IEDriver's clean session
clears the user's whole IE-mode cookie store and cache) and
``unhandled_prompt_behavior = "ignore"``. Each question quits its sessions and
kills only the Edge it started (matched by the IEDriver temp profile in the
command line) in ``finally``.

Output: one ``Qn:`` summary line per question (and per variant), preceded by
indented evidence lines.
"""
from __future__ import annotations

import http.client
import os
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ie_mcp_fixtures import start_fixture_server  # noqa: E402

EDGE = os.environ.get(
    "IE_EDGE_PATH", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")


def _driver_path() -> str:
    explicit = os.environ.get("IE_DRIVER_PATH")
    if explicit:
        return explicit
    base = os.path.join(os.path.expanduser("~"), ".cache", "selenium", "IEDriverServer")
    for arch in ("win32", "win64"):
        d = os.path.join(base, arch)
        if not os.path.isdir(d):
            continue
        versions = sorted(os.listdir(d),
                          key=lambda v: [int(x) if x.isdigit() else 0 for x in v.split(".")])
        for v in reversed(versions):
            p = os.path.join(d, v, "IEDriverServer.exe")
            if os.path.isfile(p):
                return p
    raise SystemExit("IEDriverServer.exe not found; set IE_DRIVER_PATH")


DRIVER = _driver_path()


def say(*parts):
    print(" ".join(str(p) for p in parts), flush=True)


def ev(*parts):
    """An evidence line (indented)."""
    say("   ", *parts)


# --------------------------------------------------------------------------
# Processes
# --------------------------------------------------------------------------

def list_processes(names=("msedge.exe", "IEDriverServer.exe")):
    """[(name, pid, ppid, cmdline)] via CIM (read-only)."""
    flt = " or ".join(f"Name='{n}'" for n in names)
    ps = ("$ErrorActionPreference='SilentlyContinue';"
          f"Get-CimInstance Win32_Process -Filter \"{flt}\" | ForEach-Object {{"
          " $_.Name + '|' + $_.ProcessId + '|' + $_.ParentProcessId + '|' +"
          " ($_.CommandLine -replace '[\\r\\n]',' ') }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, timeout=30).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) < 3 or not parts[1].strip().isdigit():
            continue
        rows.append((parts[0].strip(), int(parts[1]), int(parts[2] or 0),
                     parts[3] if len(parts) > 3 else ""))
    return rows


def ie_mode_edges(with_parent=False):
    """{pid: profile} (or {pid: (profile, ppid)}) of main IE-mode Edge
    processes (``--ie-mode-force``)."""
    found = {}
    for name, pid, ppid, cmd in list_processes(("msedge.exe",)):
        if "--ie-mode-force" in cmd and "--type=" not in cmd:
            m = re.search(r"IEDriver-([0-9a-fA-F-]+)", cmd)
            prof = m.group(1) if m else None
            found[pid] = (prof, ppid) if with_parent else prof
    return found


def process_tree(root_pid, rows=None):
    rows = rows if rows is not None else list_processes(("msedge.exe", "IEDriverServer.exe",
                                                         "iexplore.exe", "ie_to_edge_stub.exe"))
    children = {}
    for _n, pid, ppid, _c in rows:
        children.setdefault(ppid, []).append(pid)
    tree, todo = set(), [root_pid]
    while todo:
        p = todo.pop()
        if p in tree:
            continue
        tree.add(p)
        todo.extend(children.get(p, []))
    return tree


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

ACTIVE = []  # live Spike sessions; window helpers only touch their windows


def session_pids():
    """PIDs of the active spike sessions' Edge process trees (empty if none)."""
    pids = set()
    rows = None
    for sp in ACTIVE:
        if sp.edge_pid:
            if rows is None:
                rows = list_processes(("msedge.exe", "IEDriverServer.exe", "iexplore.exe"))
            pids |= process_tree(sp.edge_pid, rows)
    return pids


class Spike:
    """One IEDriver session that cleans up only what it started."""

    def __init__(self, url, native=False, proxy_port=None, prompt="ignore", focus=None):
        from selenium import webdriver
        from selenium.webdriver.common.proxy import Proxy
        from selenium.webdriver.ie.service import Service

        self.driver = None
        self.edge_pid = None
        self.profile = None
        before = set(ie_mode_edges())
        o = webdriver.IeOptions()
        o.attach_to_edge_chrome = True
        o.edge_executable_path = EDGE
        o.ignore_zoom_level = True
        o.ignore_protected_mode_settings = True
        # native=True: native events; focus defaults to native (upstream's
        # "native" mode). native=False: synthetic events (upstream "background").
        o.require_window_focus = native if focus is None else focus
        o.native_events = native
        o.ensure_clean_session = False
        o.browser_attach_timeout = 30_000
        o.page_load_strategy = "none"
        o.unhandled_prompt_behavior = prompt
        o.initial_browser_url = url
        if proxy_port:
            o.proxy = Proxy({
                "proxyType": "manual",
                "httpProxy": f"127.0.0.1:{proxy_port}",
                "sslProxy": f"127.0.0.1:{proxy_port}",
                "noProxy": ["<-loopback>"],
            })
            o.use_per_process_proxy = True
        # Quirk: the parent environment may carry __COMPAT_LAYER (the Claude
        # desktop app sets DetectorsAppHealth). Edge relaunches itself without
        # it, the launcher PID exits, and IEDriver cannot find the IE window.
        env = {k: v for k, v in os.environ.items() if k.upper() != "__COMPAT_LAYER"}
        log_dir = os.environ.get("SPIKE_DRIVER_LOG_DIR")
        extra = {}
        if log_dir:
            extra = {"log_output": os.path.join(log_dir, f"iedriver-{int(time.time())}.log"),
                     "service_args": ["--log-level=DEBUG"]}
        # Session creation sometimes hangs inside IEDriver after Edge starts
        # (seen once per few runs, any interaction mode): watchdog + retry.
        last = None
        for attempt in range(3):
            service = Service(executable_path=DRIVER, env=env, **extra)
            done, res, _t = in_thread(lambda: webdriver.Ie(service=service, options=o), 60)
            if done and not isinstance(res, BaseException):
                self.driver = res
                self.driver_pid = self.driver.service.process.pid
                self._find_edge(before, self.driver_pid)
                ACTIVE.append(self)
                if attempt:
                    ev(f"(session created on attempt {attempt + 1})")
                return
            last = res if done else TimeoutError("session creation hung for 60 s")
            ev(f"(session attempt {attempt + 1} failed: {str(last)[:120]})")
            try:
                driver_pid = service.process.pid
                service.process.kill()
            except Exception:
                driver_pid = None
            self._find_edge(before, driver_pid)
            self.quit()
            self.edge_pid = self.profile = None
            before = set(ie_mode_edges())
        raise RuntimeError(f"could not create a session: {last}")

    def _find_edge(self, before, driver_pid):
        """Adopt the new IE-mode Edge started by OUR IEDriver (its parent),
        so a concurrent IE-mode launch is never adopted (or later killed)."""
        if not driver_pid:
            return
        for pid, (prof, ppid) in ie_mode_edges(with_parent=True).items():
            if pid not in before and ppid == driver_pid and prof:
                self.edge_pid, self.profile = pid, prof
                return
        ev(f"(no new IE-mode Edge whose parent is driver {driver_pid}; nothing adopted)")

    def quit(self):
        if self in ACTIVE:
            close_dialogs()  # only this run's dialogs (session_pids)
            ACTIVE.remove(self)
        if self.driver is not None:
            t = threading.Thread(target=self._quit_driver, daemon=True)
            t.start()
            t.join(20)
            try:
                # quit() can hang (a dialog left open); kill our own driver.
                if self.driver.service.process.poll() is None:
                    self.driver.service.process.kill()
            except Exception:
                pass
            self.driver = None
        if self.edge_pid and self.profile:
            # Kill our Edge only if the PID still carries our profile.
            if ie_mode_edges().get(self.edge_pid) == self.profile:
                subprocess.run(["taskkill", "/F", "/PID", str(self.edge_pid)],
                               capture_output=True, timeout=15)
        elif self.edge_pid:
            ev(f"(not killing edge {self.edge_pid}: no profile recorded)")

    def _quit_driver(self):
        try:
            self.driver.quit()
        except Exception:
            pass

    def wait_title(self, needle, timeout=15):
        end = time.time() + timeout
        last = ""
        while time.time() < end:
            try:
                last = self.driver.title
                if needle.lower() in last.lower():
                    state = self.driver.execute_script("return document.readyState")
                    if state == "complete":
                        return last
            except Exception:
                pass
            time.sleep(0.3)
        raise TimeoutError(f"title {needle!r} not reached (last {last!r})")

    def goto(self, url, title):
        self.driver.get(url)
        return self.wait_title(title)


def in_thread(fn, timeout):
    """Run fn in a daemon thread; return (finished, result_or_exception)."""
    box = {}

    def run():
        try:
            box["r"] = fn()
        except BaseException as exc:  # noqa: BLE001 - report everything
            box["r"] = exc
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return (not t.is_alive()), box.get("r"), t


# --------------------------------------------------------------------------
# Win32 / COM
# --------------------------------------------------------------------------

import win32api  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402

IE_SERVER = "Internet Explorer_Server"
DLG_FRAME = "Internet Explorer_TridentDlgFrame"


def win_info(hwnd):
    try:
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
    except Exception:
        pid = 0
    return {
        "hwnd": hwnd,
        "cls": win32gui.GetClassName(hwnd),
        "title": win32gui.GetWindowText(hwnd),
        "owner": win32gui.GetWindow(hwnd, win32con.GW_OWNER),
        "pid": pid,
        "visible": bool(win32gui.IsWindowVisible(hwnd)),
        "style": win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE) & 0xFFFFFFFF,
        "exstyle": win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & 0xFFFFFFFF,
    }


def top_windows(visible_only=True):
    out = []

    def cb(h, _):
        if not visible_only or win32gui.IsWindowVisible(h):
            out.append(h)
        return True
    win32gui.EnumWindows(cb, None)
    return out


def children(hwnd):
    out = []

    def cb(h, _):
        out.append(h)
        return True
    try:
        win32gui.EnumChildWindows(hwnd, cb, None)
    except Exception:
        pass
    return out


def direct_children(hwnd):
    return [h for h in children(hwnd) if win32gui.GetParent(h) == hwnd]


def ie_servers(hwnd):
    return [h for h in children(hwnd) if win32gui.GetClassName(h) == IE_SERVER]


def child_shape(hwnd):
    """Sorted (class, control id) of the direct child controls."""
    shape = []
    for h in direct_children(hwnd):
        try:
            cid = win32gui.GetDlgCtrlID(h)
        except Exception:
            cid = None
        shape.append((win32gui.GetClassName(h), cid, win32gui.GetWindowText(h)))
    return sorted(shape, key=lambda s: (s[0], s[1] or 0))


def ancestors(hwnd):
    chain = []
    h = win32gui.GetParent(hwnd)
    while h:
        chain.append((h, win32gui.GetClassName(h)))
        h = win32gui.GetParent(h)
    return chain


def windows_of(pids):
    return [win_info(h) for h in top_windows() if win_info(h)["pid"] in pids]


def find_top(cls=None, title_part=None, pids=None, timeout=10.0, exclude=()):
    """A visible top window of the active spike sessions (``pids`` defaults
    to their Edge process trees, so the user's own windows never match)."""
    if pids is None:
        pids = session_pids()
    end = time.time() + timeout
    while True:
        for h in top_windows():
            if h in exclude:
                continue
            i = win_info(h)
            if cls and i["cls"] != cls:
                continue
            if title_part and title_part.lower() not in i["title"].lower():
                continue
            if pids is not None and i["pid"] not in pids:
                continue
            return i
        if time.time() >= end:
            return None
        time.sleep(0.2)


def post_close(hwnd):
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)


RPC_REJECTED = {-2147418111, -2147417846}  # RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER


def document_from_hwnd(hwnd, timeout_ms=1000):
    import pythoncom
    import win32com.client
    msg = win32gui.RegisterWindowMessage("WM_HTML_GETOBJECT")
    _ok, lres = win32gui.SendMessageTimeout(hwnd, msg, 0, 0, win32con.SMTO_ABORTIFHUNG,
                                            timeout_ms)
    if not lres:
        raise RuntimeError(f"WM_HTML_GETOBJECT returned 0 for hwnd {hwnd}")
    ob = pythoncom.ObjectFromLresult(lres, pythoncom.IID_IDispatch, 0)
    return win32com.client.dynamic.Dispatch(ob)


def com_retry(fn, total=2.0):
    """Call fn, retrying rejected COM calls; return (result, rejections)."""
    import pywintypes
    rejected = []
    end = time.time() + total
    while True:
        try:
            return fn(), rejected
        except pywintypes.com_error as exc:
            if exc.hresult in RPC_REJECTED and time.time() < end:
                rejected.append(hex(exc.hresult & 0xFFFFFFFF))
                time.sleep(0.1)
                continue
            raise


def com_js(doc, script, method="execScript"):
    """Run script in doc's window by the given method; return nothing."""
    win = doc.parentWindow
    if method == "execScript":
        win.execScript(script)
    elif method == "eval":
        win.eval(script)
    elif method == "script-element":
        el = doc.createElement("script")
        el.text = script
        head = doc.getElementsByTagName("head").item(0) or doc.body
        head.appendChild(el)
        head.removeChild(el)
    else:
        raise ValueError(method)


# --------------------------------------------------------------------------
# Logging proxy (Q4, Q6)
# --------------------------------------------------------------------------

LOADER_PATH = "/__iemcp__/i.js"
LOADER_JS = b"window.__inj=1;"


def loader_tag(doc_url):
    p = urllib.parse.urlsplit(doc_url)
    return (f'<script type="text/javascript" src="http://{p.netloc}{LOADER_PATH}">'
            '</script>').encode("ascii")


_TAG = re.compile(rb"<(!doctype|/?[a-zA-Z][a-zA-Z0-9]*)", re.I)


def _tag_end(buf, i):
    """Index after the '>' closing the tag starting at i (quotes respected)."""
    q = None
    j = i + 1
    while j < len(buf):
        c = buf[j:j + 1]
        if q:
            if c == q:
                q = None
        elif c in (b'"', b"'"):
            q = c
        elif c == b">":
            return j + 1
        j += 1
    return -1


def _skip_ws_comments(buf, i):
    while True:
        while i < len(buf) and buf[i:i + 1] in b" \t\r\n\f":
            i += 1
        if buf.startswith(b"<!--", i):
            e = buf.find(b"-->", i + 4)
            if e < 0:
                return len(buf)
            i = e + 3
            continue
        return i


def _tag_at(buf, i):
    """(lower name, end) of the tag at i, or (None, i)."""
    m = _TAG.match(buf, i)
    if not m:
        return None, i
    name = m.group(1).lower()
    after = buf[m.end():m.end() + 1]
    if name != b"!doctype" and after not in (b" ", b"\t", b"\r", b"\n", b"\f", b"/", b">"):
        return None, i
    return name, _tag_end(buf, i)


def find_insertion_point(body):
    """The spike's version of the CLAUDE.md placement rule (whole body known)."""
    if body.startswith(b"\xff\xfe") or body.startswith(b"\xfe\xff"):
        return None
    i = 3 if body.startswith(b"\xef\xbb\xbf") else 0
    i = _skip_ws_comments(body, i)
    first, first_end = _tag_at(body, i)
    if first not in (b"!doctype", b"html", b"head", b"body", b"frameset"):
        return None
    if first == b"body":
        return i
    if first == b"frameset":
        return None
    # Walk the tags (ignoring comments) to find <head and <html.
    anchor = None
    j = i
    html_end = None
    doctype_end = first_end if first == b"!doctype" else None
    while j < len(body):
        j = _skip_ws_comments(body, j)
        if j >= len(body):
            break
        if body[j:j + 1] != b"<":
            break
        name, end = _tag_at(body, j)
        if name is None or end < 0:
            break
        if name == b"head":
            anchor = end
            break
        if name == b"html":
            html_end = end
        elif name != b"!doctype":
            break
        j = end
    if anchor is None:
        anchor = html_end if html_end is not None else doctype_end
    if anchor is None:
        return None
    k = anchor
    while True:
        k = _skip_ws_comments(body, k)
        name, end = _tag_at(body, k)
        if name in (b"meta", b"base") and end > 0:
            k = end
            continue
        if name == b"title" and end > 0:
            close = re.compile(rb"</title\s*>", re.I).search(body, end)
            if not close:
                return None
            k = close.end()
            continue
        if name == b"frameset":
            return None
        return k


class LoggingProxy:
    """Minimal forward proxy: logs request line + Accept; optional loader."""

    def __init__(self, inject=False):
        self.inject = inject
        self.log = []  # (method, url, accept)
        self.lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_CONNECT(self):
                with proxy.lock:
                    proxy.log.append(("CONNECT", self.path, self.headers.get("Accept", "")))
                host, _, port = self.path.partition(":")
                try:
                    up = socket.create_connection((host, int(port or 443)), timeout=10)
                except OSError:
                    self.send_error(502)
                    return
                self.send_response(200, "Connection established")
                self.end_headers()
                self.wfile.flush()
                cl = self.connection

                def pump(a, b):
                    try:
                        while True:
                            d = a.recv(65536)
                            if not d:
                                break
                            b.sendall(d)
                    except OSError:
                        pass
                    finally:
                        try:
                            b.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                t = threading.Thread(target=pump, args=(up, cl), daemon=True)
                t.start()
                pump(cl, up)
                t.join(10)
                up.close()

            def _forward(self):
                url = self.path
                p = urllib.parse.urlsplit(url)
                if p.path.startswith("/__iemcp__/"):
                    body = LOADER_JS
                    self.send_response(200)
                    self.send_header("Content-Type", "application/javascript")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                with proxy.lock:
                    proxy.log.append((self.command, url, self.headers.get("Accept", "")))
                n = int(self.headers.get("Content-Length") or 0)
                req_body = self.rfile.read(n) if n else None
                hop = {"connection", "proxy-connection", "keep-alive", "te", "trailer",
                       "transfer-encoding", "upgrade", "proxy-authorization"}
                headers = {k: v for k, v in self.headers.items() if k.lower() not in hop}
                if proxy.inject:
                    headers["Accept-Encoding"] = "identity"
                conn = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=30)
                path = p.path + (("?" + p.query) if p.query else "")
                try:
                    conn.request(self.command, path or "/", body=req_body, headers=headers)
                    resp = conn.getresponse()
                    data = resp.read()
                except OSError as exc:
                    self.send_error(502, str(exc))
                    return
                finally:
                    conn.close()
                rh = [(k, v) for k, v in resp.getheaders()
                      if k.lower() not in hop and k.lower() != "content-length"]
                ctype = resp.getheader("Content-Type", "")
                accept = self.headers.get("Accept", "")
                if (proxy.inject and resp.status == 200 and "text/html" in accept
                        and ctype.lower().startswith("text/html")):
                    off = find_insertion_point(data)
                    if off is not None:
                        data = data[:off] + loader_tag(url) + data[off:]
                        rh = [(k, v) for k, v in rh
                              if k.lower() not in ("etag", "last-modified", "cache-control")]
                        rh.append(("Cache-Control", "no-store"))
                self.send_response(resp.status, resp.reason)
                for k, v in rh:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            do_GET = do_POST = do_HEAD = _forward

        class Server(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # browsers reset idle proxy connections; not evidence

        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def seen(self, needle):
        with self.lock:
            return [e for e in self.log if needle in e[1]]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def read_inet_settings():
    import winreg
    out = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
        for name in ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL"):
            try:
                out[name] = winreg.QueryValueEx(k, name)[0]
            except FileNotFoundError:
                out[name] = None
    return out


def zone_of(url):
    """IE security zone of url (read-only; .NET Framework's Zone.CreateFromUrl
    calls IInternetSecurityManager::MapUrlToZone)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[System.Security.Policy.Zone]::CreateFromUrl('%s').SecurityZone" % url],
            capture_output=True, text=True, timeout=20).stdout.strip()
        return out
    except Exception as exc:
        return f"? ({exc})"


# --------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------

def q0_trident(fx):
    s = Spike(fx.base_url + "index.htm")
    try:
        s.wait_title("Fixture index")
        ua = s.driver.execute_script("return navigator.userAgent")
        mode = s.driver.execute_script("return document.documentMode")
        ev("edge pid", s.edge_pid, "profile", s.profile, "driver pid", s.driver_pid)
        ev("userAgent", ua)
        ev("documentMode", mode)
        say(f"Q0: Trident={'Trident' in ua} documentMode={mode}")
    finally:
        s.quit()


ASYNC_CLICK = "var b=arguments[0];window.setTimeout(function(){b.click();},0);return 'queued';"


MODES = {
    # label: (native_events, require_window_focus)
    "native": (True, True),          # upstream IE_INTERACTION_MODE=native
    "background": (False, False),    # upstream IE_INTERACTION_MODE=background
    "native-nofocus": (True, False),  # native events posted as window messages
}


def modes():
    return tuple((label, ne) for label, (ne, _f) in MODES.items())


def spike_for(mode, url, **kw):
    ne, focus = MODES[mode]
    return Spike(url, native=ne, focus=focus, **kw)


def wait_dialog(timeout=10.0, exclude=()):
    return find_top(cls=DLG_FRAME, timeout=timeout, exclude=exclude)


def close_dialogs():
    """WM_CLOSE the visible IE dialog frames of the active spike sessions."""
    pids = session_pids()
    for h in top_windows():
        if win32gui.GetClassName(h) == DLG_FRAME and win_info(h)["pid"] in pids:
            post_close(h)


def q1_driver_click_blocks(fx):
    for mode, native in modes():
        s = spike_for(mode, fx.base_url + "modal_opener.htm")
        try:
            s.wait_title("modal opener")
            el = s.driver.find_element("id", "open_lookup")
            t0 = time.time()
            done, res, th = in_thread(el.click, 15)
            dlg = wait_dialog(1)
            ev(f"[{mode}] click returned within 15s: {done} after {time.time() - t0:.1f}s;"
               f" result {res!r}; dialog open: {bool(dlg)}")
            if dlg:
                post_close(dlg["hwnd"])
            th.join(15)
            ev(f"[{mode}] click thread alive after WM_CLOSE: {th.is_alive()}")
            ok, title, _ = in_thread(lambda: s.driver.title, 10)
            ev(f"[{mode}] driver usable afterwards: {ok} title={title!r}")
            say(f"Q1[{mode}]: blocks={not done} click_opened_dialog={bool(dlg)}")
        finally:
            close_dialogs()
            s.quit()


def q1r_input_reliability(fx):
    """D17 evidence: repeated driver clicks and typing per variant, with
    whatever window the user has in front (focus is not forced)."""
    for mode in ("native-nofocus", "native", "background"):
        s = spike_for(mode, fx.base_url + "modal_opener.htm")
        try:
            d = s.driver
            s.wait_title("modal opener")
            opened = []
            for _ in range(6):
                fg = win_info(win32gui.GetForegroundWindow())["title"][:20]
                el = d.find_element("id", "open_lookup")
                _ok, _r, th = in_thread(el.click, 15)
                dlg = wait_dialog(5)
                opened.append((bool(dlg), fg))
                if dlg:
                    post_close(dlg["hwnd"])
                    th.join(10)
                time.sleep(1)
            s.goto(fx.base_url + "form.htm", "Fixture form")
            typed = []
            for i in range(4):
                el = d.find_element("id", "code")
                d.execute_script("arguments[0].value=''", el)
                in_thread(lambda: el.send_keys("A%d" % i), 15)
                time.sleep(0.3)
                typed.append(d.execute_script("return arguments[0].value", el))
            ev(f"[{mode}] dialog opened per click (foreground window): {opened}")
            ev(f"[{mode}] send_keys 'A0'..'A3' gave: {typed}")
            say(f"Q1R[{mode}]: clicks_opened={sum(o for o, _ in opened)}/6"
                f" exact_typing={sum(t == 'A%d' % i for i, t in enumerate(typed))}/4")
        finally:
            close_dialogs()
            s.quit()


def q2_handles(fx):
    for mode, native in modes():
        s = spike_for(mode, fx.base_url + "modal_opener.htm")
        try:
            d = s.driver
            s.wait_title("modal opener")
            main = d.current_window_handle
            d.execute_script(ASYNC_CLICK, d.find_element("id", "open_lookup"))
            dlg = wait_dialog(10)
            ev(f"[{mode}] dialog window: {dlg and (dlg['hwnd'], dlg['title'])}")
            new = None
            end = time.time() + 10
            while time.time() < end and new is None:
                ok, hs, _ = in_thread(lambda: d.window_handles, 10)
                if not ok:
                    ev(f"[{mode}] window_handles blocked")
                    break
                if isinstance(hs, Exception):
                    ev(f"[{mode}] window_handles error {hs!r}")
                    break
                extra = [h for h in hs if h != main]
                if extra:
                    new = extra[0]
                    ev(f"[{mode}] handles {hs}")
                time.sleep(0.5)
            if new:
                d.switch_to.window(new)
                ev(f"[{mode}] dialog title via driver: {d.execute_script('return document.title')!r}")
                ok, res, _ = in_thread(lambda: d.find_element("id", "pick_A1").click(), 10)
                ev(f"[{mode}] pick_A1 click finished {ok} {res!r}")
                d.switch_to.window(main)
                code = d.execute_script("return document.getElementById('lookup_code').value")
                ev(f"[{mode}] lookup_code after pick: {code!r}")
                if wait_dialog(1):
                    ev(f"[{mode}] dialog still open after the driver click; closing it")
                    close_dialogs()
                    time.sleep(1)
            else:
                close_dialogs()
                time.sleep(1)
            # execute_script on the opener while the dialog is open
            d.switch_to.window(main)
            d.execute_script(ASYNC_CLICK, d.find_element("id", "open_lookup"))
            dlg = wait_dialog(10)
            done, res, th = in_thread(lambda: d.execute_script("return 1"), 10)
            ev(f"[{mode}] execute_script on opener with dialog open: finished={done} result={res!r}")
            close_dialogs()
            th.join(15)
            time.sleep(1)
            say(f"Q2[{mode}]: dialog_in_handles={bool(new)} opener_script_blocks={not done}")

            # D9: window.open popup (driver click)
            s.goto(fx.base_url + "dialogs.htm", "Fixture dialogs")
            before = set(d.window_handles)
            if native:
                d.find_element("id", "do_popup").click()
            else:  # background driver clicks do nothing here (see Q1)
                d.execute_script(ASYNC_CLICK, d.find_element("id", "do_popup"))
            pop = None
            end = time.time() + 10
            while time.time() < end and pop is None:
                extra = [h for h in d.window_handles if h not in before]
                pop = extra[0] if extra else None
                time.sleep(0.5)
            w = find_top(title_part="Fixture popup", timeout=2)
            ev(f"[{mode}] popup Win32 window: {w and (w['hwnd'], w['cls'], w['title'])}")
            if pop:
                d.switch_to.window(pop)
                ev(f"[{mode}] popup title via driver: {d.title!r}")
                d.close()
                d.switch_to.window(main)
            elif w:
                post_close(w["hwnd"])
            say(f"Q2[{mode}]: popup_in_handles={bool(pop)}")
        finally:
            close_dialogs()
            s.quit()


def q7_popup_blocker(fx):
    for mode, native in modes():
        s = spike_for(mode, fx.base_url + "dialogs.htm")
        try:
            d = s.driver
            s.wait_title("Fixture dialogs")
            before = set(d.window_handles)
            d.execute_script(ASYNC_CLICK, d.find_element("id", "do_popup"))
            w = find_top(title_part="Fixture popup", timeout=8)
            extra = [h for h in d.window_handles if h not in before]
            ev(f"[{mode}] async popup: Win32 {w and (w['hwnd'], w['title'])}; new handles {extra}")
            bar = [i for i in (win_info(h) for h in top_windows())
                   if "pop-up" in i["title"].lower() or "popup" in i["title"].lower()]
            ev(f"[{mode}] windows mentioning pop-ups: {[(b['cls'], b['title']) for b in bar]}")
            if w:
                post_close(w["hwnd"])
            say(f"Q7[{mode}]: async_popup_blocked={not (w or extra)}")
        finally:
            s.quit()


VK_X, VK_RETURN = 0x58, 0x0D


def _key_lparams(vk):
    scan = win32api.MapVirtualKey(vk, 0)
    down = 1 | (scan << 16)
    up = down | (1 << 30) | (1 << 31)
    return down, up


def post_keys(hwnd, variant, vk, ch):
    down, up = _key_lparams(vk)
    if variant == "keydown-keyup":
        win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, down)
        win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk, up)
    elif variant == "char-only":
        win32gui.PostMessage(hwnd, win32con.WM_CHAR, ord(ch), down)
    elif variant == "keydown-char-keyup":
        win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, down)
        win32gui.PostMessage(hwnd, win32con.WM_CHAR, ord(ch), down)
        win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk, up)
    else:
        raise ValueError(variant)


def win_get(win, name):
    """Read a window expando. getattr (GetIDsOfNames) fails in IE9+ document
    modes; IDispatchEx.GetDispID(fdexNameCaseSensitive) + InvokeEx works."""
    import pythoncom
    dex = win._oleobj_.QueryInterface(pythoncom.IID_IDispatchEx)
    dispid = dex.GetDispID(name, 1)
    return dex.InvokeEx(dispid, 0, pythoncom.DISPATCH_PROPERTYGET, ())


def js_value(doc, expr, method):
    com_js(doc, "window.__v=String(" + expr + ")", method)
    return win_get(doc.parentWindow, "__v")


def text_of(doc, el_id, method="execScript"):
    return js_value(doc, "document.getElementById('%s').innerText" % el_id, method)


def find_script_method(doc, label):
    """Try execScript, eval, script element; return the first that works."""
    win = doc.parentWindow
    for method in ("execScript", "eval", "script-element"):
        try:
            com_js(doc, "window.__t=1+1", method)
            try:
                val = getattr(win, "__t")
                ev(f"{label}: {method} -> getattr __t={val!r}")
            except Exception as exc:
                ev(f"{label}: {method} getattr failed ({str(exc)[:60]}); IDispatchEx read")
                val = win_get(win, "__t")
                ev(f"{label}: {method} -> IDispatchEx __t={val!r}")
            if val == 2:
                return method
        except Exception as exc:
            ev(f"{label}: {method} failed: {str(exc)[:160]}")
    return None


def key_tests(doc, server_hwnd, method, label, fire_event_ok):
    """Send 'x' to #typed by each candidate; return {variant: (count, chars)}."""
    results = {}
    variants = ["keydown-keyup", "char-only", "keydown-char-keyup"]
    if fire_event_ok:
        variants.append("fireEvent")
    for variant in variants:
        com_js(doc, "resetTyped(); document.getElementById('typed').value='';"
                    " document.getElementById('typed').focus();", method)
        time.sleep(0.3)
        if variant == "fireEvent":
            try:
                com_js(doc, "var e=document.createEventObject(); e.keyCode=120;"
                            " document.getElementById('typed').fireEvent('onkeypress', e);", method)
            except Exception as exc:
                ev(f"{label} fireEvent failed: {str(exc)[:120]}")
        else:
            post_keys(server_hwnd, variant, VK_X, "x")
        time.sleep(0.8)
        count = js_value(doc, "typedCount", method)
        chars = js_value(doc, "typedChars", method)
        value = js_value(doc, "document.getElementById('typed').value", method)
        results[variant] = (count, chars, value)
        ev(f"{label} {variant}: keypress_count={count!r} typed_chars={chars!r} value={value!r}")
    return results


def pick_key_path(results):
    for variant, (count, chars, _v) in results.items():
        if count == "1" and chars == "x":
            return variant
    return None


def q3_win32_com(fx):
    import pythoncom
    s = Spike(fx.base_url + "modal_opener.htm", native=False)
    try:
        d = s.driver
        s.wait_title("modal opener")
        rows = list_processes(("msedge.exe", "IEDriverServer.exe", "iexplore.exe"))
        tree = process_tree(s.edge_pid, rows)
        ev("edge tree:", sorted(tree))
        main_top = [w for w in windows_of(tree) if ie_servers(w["hwnd"])]
        ev("main top windows with IE server:",
           [(w["hwnd"], w["cls"], w["title"][:40], w["pid"]) for w in main_top])
        main_srv = ie_servers(main_top[0]["hwnd"])[0] if main_top else None
        if main_srv:
            ev("main IE server", main_srv, "pid", win_info(main_srv)["pid"],
               "ancestors", ancestors(main_srv))

        d.execute_script(ASYNC_CLICK, d.find_element("id", "open_lookup"))
        dlg = wait_dialog(10)
        if not dlg:
            say("Q3: no dialog appeared")
            return
        owner = dlg["owner"]
        ev("dialog", dlg, "owner class", owner and win32gui.GetClassName(owner),
           "owner pid", owner and win_info(owner)["pid"])
        ev("dialog pid in edge tree:", dlg["pid"] in tree)
        # every visible top window whose owner chain reaches an edge window
        for h in top_windows():
            i = win_info(h)
            if i["pid"] in tree:
                ev("  top", h, i["cls"], repr(i["title"][:40]), "owner", i["owner"], "pid", i["pid"])
        srv = ie_servers(dlg["hwnd"])
        ev("dialog IE servers:", srv, [ancestors(h) for h in srv])
        doc = document_from_hwnd(srv[0])
        ev("dialog document title:", doc.title, "| body:", doc.body.innerText[:80].replace("\r\n", " "))
        ev("dialog documentMode:", doc.documentMode)
        dmethod = find_script_method(doc, "dialog(IE7)")

        # the opener's document while the dialog is open
        try:
            (title, rej) = com_retry(lambda: document_from_hwnd(main_srv).title)
            ev(f"opener title over COM with dialog open: {title!r}; rejections {rej}")
            odoc = document_from_hwnd(main_srv)
            omethod, rej = com_retry(lambda: find_script_method(odoc, "opener(IE7)"))
            ev("opener script method:", omethod, "rejections", rej)
        except Exception as exc:
            ev("opener COM call failed:", repr(exc)[:200])

        # worker thread with its own apartment
        box = {}

        def worker():
            pythoncom.CoInitialize()
            try:
                wdoc = document_from_hwnd(srv[0])
                box["title"] = wdoc.title
                com_js(wdoc, "window.__w=3", dmethod or "execScript")
                box["w"] = win_get(wdoc.parentWindow, "__w")
                del wdoc
            except Exception as exc:
                box["err"] = repr(exc)
            finally:
                pythoncom.CoUninitialize()
        t = threading.Thread(target=worker)
        t.start()
        t.join(15)
        ev("worker thread COM:", box)

        # keys in the dialog (IE7)
        dres = key_tests(doc, srv[0], dmethod, "dialog(IE7)", fire_event_ok=True)
        dpath = pick_key_path(dres)
        enter_ok = None
        if dpath and dpath != "fireEvent":
            com_js(doc, "var s=document.getElementById('search'); s.value='B'; s.focus();", dmethod)
            time.sleep(0.3)
            post_keys(srv[0], dpath, VK_RETURN, "\r")
            time.sleep(1.5)
            enter_ok = text_of(doc, "search_count", dmethod)
            ev(f"dialog Enter via {dpath}: search_count={enter_ok!r}")
        elif dpath == "fireEvent":
            com_js(doc, "var s=document.getElementById('search'); s.value='B';"
                        " var e=document.createEventObject(); e.keyCode=13; s.fireEvent('onkeypress', e);",
                   dmethod)
            time.sleep(1.5)
            enter_ok = text_of(doc, "search_count", dmethod)
            ev(f"dialog Enter via fireEvent: search_count={enter_ok!r}")
        del doc
        post_close(dlg["hwnd"])
        time.sleep(1.5)

        # index.htm (IE11)
        s.goto(fx.base_url + "index.htm", "Fixture index")
        top = [w for w in windows_of(tree | process_tree(s.edge_pid)) if ie_servers(w["hwnd"])]
        isrv = ie_servers(top[0]["hwnd"])[0]
        idoc = document_from_hwnd(isrv)
        ev("index documentMode:", idoc.documentMode)
        imethod = find_script_method(idoc, "index(IE11)")
        ires = key_tests(idoc, isrv, imethod, "index(IE11)", fire_event_ok=False)
        ipath = pick_key_path(ires)
        del idoc

        # navigation: which hwnds survive
        chain_before = [(isrv, IE_SERVER)] + ancestors(isrv)
        s.goto(fx.base_url + "form.htm", "Fixture form")
        time.sleep(1)
        top = [w for w in windows_of(process_tree(s.edge_pid)) if ie_servers(w["hwnd"])]
        nsrv = ie_servers(top[0]["hwnd"])[0]
        chain_after = [(nsrv, IE_SERVER)] + ancestors(nsrv)
        ev("chain before navigation:", chain_before)
        ev("chain after navigation: ", chain_after)
        survived = [c for c in chain_before if c in chain_after]
        ev("survived:", survived)
        say(f"Q3: dialog_doc_via_com=True dialog_script={dmethod} opener_script=see-evidence"
            f" index_script={imethod} worker_apartment={'err' not in box}"
            f" key_path_ie7={dpath} enter_search_count={enter_ok!r} key_path_ie11={ipath}"
            f" ie_server_survives_nav={nsrv == isrv}")
    finally:
        close_dialogs()
        s.quit()


def wait_until(fn, timeout=8.0, step=0.3):
    end = time.time() + timeout
    while True:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        if time.time() >= end:
            return None
        time.sleep(step)


def q4_per_process_proxy(fx):
    before_settings = read_inet_settings()
    ev("Internet Settings before:", before_settings)
    proxy = LoggingProxy(inject=False)
    seen_any = False
    try:
        for label, server in (("127.0.0.1", fx), ("127.0.0.2", None)):
            srv = server
            if srv is None:
                if seen_any:
                    break
                srv = start_fixture_server(host="127.0.0.2")
            base = srv.base_url
            s = Spike(base + "index.htm", native=False, proxy_port=proxy.port)
            try:
                d = s.driver
                s.wait_title("Fixture index")
                time.sleep(1)
                initial = proxy.seen(base + "index.htm")
                s.goto(base + "form.htm", "Fixture form")
                later = proxy.seen(base + "form.htm")
                s.goto(base + "xhr.htm", "Fixture")
                for b in ("load", "load_native", "load_fragment"):
                    d.execute_script(ASYNC_CLICK, d.find_element("id", b))
                    time.sleep(1.5)
                s.goto(base + "dialogs.htm", "Fixture dialogs")
                d.execute_script(ASYNC_CLICK, d.find_element("id", "do_popup"))
                w = find_top(title_part="Fixture popup", timeout=8)
                time.sleep(1)
                popup = proxy.seen(base + "popup.htm")
                if w:
                    post_close(w["hwnd"])
                    time.sleep(1)
                s.goto(base + "modal_opener.htm", "modal opener")
                d.execute_script(ASYNC_CLICK, d.find_element("id", "open_lookup"))
                dlg = wait_dialog(10)
                time.sleep(2)
                modal = proxy.seen(base + "modal_dialog.htm")
                dxhr = proxy.seen("/api/lookup.xml")
                close_dialogs()
                time.sleep(1)
                ev(f"[{label}] initial {bool(initial)} later {bool(later)} popup {bool(popup)}"
                   f" modal {bool(modal)} dialog-xmlhttp {bool(dxhr)} (dialog opened {bool(dlg)})")
                with proxy.lock:
                    for m, u, a in proxy.log:
                        ev(f"[{label}]   {m} {u} Accept={a!r}")
                seen_any = seen_any or bool(initial)
                say(f"Q4[{label}]: proxied initial={bool(initial)} later_nav={bool(later)}"
                    f" popup={bool(popup)} modal_dialog={bool(modal)} dialog_xmlhttp={bool(dxhr)}")
            finally:
                close_dialogs()
                s.quit()
                if server is None:
                    srv.stop()
    finally:
        proxy.stop()
    after_settings = read_inet_settings()
    ev("Internet Settings after:", after_settings)
    say(f"Q4: internet_settings_unchanged={before_settings == after_settings}")


def q5_console(fx):
    s = Spike(fx.base_url + "index.htm", native=False)
    try:
        out = []
        for page, title in (("index.htm", "Fixture index"), ("form.htm", "Fixture form"),
                            ("quirks.htm", "quirks")):
            s.goto(fx.base_url + page, title)
            r = s.driver.execute_script(
                "return document.documentMode + '|' + typeof console + '|' +"
                " (typeof console != 'undefined' ? typeof console.log : '-')")
            ev(page, r)
            out.append(f"{page}={r}")
        say("Q5:", " ".join(out))
    finally:
        s.quit()


PROBE = ("var e = document.getElementById('sample_text');"
         " var r = navigator.userAgent + '|' + document.documentMode + '/' + document.compatMode"
         " + '|' + (document.charset || document.characterSet) + '|' + (typeof window.__inj)"
         " + '|' + (e ? e.innerText : '-');"
         " return r;")
XORIGIN = ("try { var t = frames['foreign'].document.title; return 'readable:' + t; }"
           " catch (e) { return 'throws:' + (e.message || e); }")
SAMPLE = "Müller – café"
# utf8.htm: a non-default charset (UTF-8 meta), so a lost meta would show in
# the decoded sample text. latecharset.htm: windows-1252 meta after a script;
# windows-1252 is also this machine's default, so its charset cannot tell a
# honoured meta from the default.
Q6_PAGES = ("index.htm", "form.htm", "quirks.htm", "headless.htm", "nomode.htm",
            "latecharset.htm", "utf8.htm", "progressive?ms=800")


def _q6_measure(s, base, page, label, key, proxy, res):
    s.driver.get(base + page)
    time.sleep(1)
    wait_until(lambda: s.driver.execute_script("return document.readyState") == "complete", 15)
    if page.startswith("progressive"):
        time.sleep(1)
    ua, mode, cs, inj, text = s.driver.execute_script(PROBE).split("|", 4)
    proxied = bool(proxy.seen(base + page)) if proxy else None
    text_ok = text == "-" or text == SAMPLE
    res[key] = (mode, cs, inj, text_ok, proxied)
    ev(f"[{label}] {key}: mode={mode} charset={cs} sample_text_ok={text_ok} __inj={inj}"
       f" proxied={proxied} trident={'Trident' in ua}")


def _q6_run(fx, label, proxy):
    """Measure every page after a navigation (never as the session's initial
    URL, which the per-process proxy does not see: D11)."""
    res = {}
    port = proxy.port if proxy else None
    s = Spike(fx.base_url + "index.htm", native=False, proxy_port=port)
    try:
        s.wait_title("Fixture index")
        for page in Q6_PAGES:
            _q6_measure(s, fx.base_url, page, label, page, proxy, res)
        s.goto(fx.base_url + "xorigin.htm", "cross-origin")
        time.sleep(2)
        xo = s.driver.execute_script(XORIGIN)
        res["xorigin"] = xo
        ev(f"[{label}] xorigin foreign frame: {xo}")
    finally:
        s.quit()
    # The localhost origin in its own session (other side of the zone
    # boundary): start on its index.htm, then navigate to nomode.htm.
    s = Spike(fx.alt_base_url + "index.htm", native=False, proxy_port=port)
    try:
        s.wait_title("Fixture index")
        _q6_measure(s, fx.alt_base_url, "nomode.htm", label, "alt:nomode.htm", proxy, res)
    finally:
        s.quit()
    return res


def q6_document_modes(fx):
    ev("zones:", fx.base_url, zone_of(fx.base_url), "|", fx.alt_base_url, zone_of(fx.alt_base_url))
    a = _q6_run(fx, "a:no-proxy", None)
    p = LoggingProxy(inject=False)
    try:
        b = _q6_run(fx, "b:proxy", p)
    finally:
        p.stop()
    p = LoggingProxy(inject=True)
    try:
        c = _q6_run(fx, "c:proxy+loader", p)
    finally:
        p.stop()
    keys = [k for k in a if k != "xorigin"]
    for key in keys:
        ev(f"{key}: a={a[key][:2]} b={b[key][:2]} c={c[key][:2]}"
           f" text_ok a/b/c={a[key][3]}/{b[key][3]}/{c[key][3]} loader_ran={c[key][2]}")

    def same(x, y):
        return x[0] == y[0] and x[1] == y[1] and x[3] == y[3]
    d12 = [k for k in ("nomode.htm", "alt:nomode.htm") if a[k][0] != b[k][0]]
    proxy_changes = [k for k in keys if not same(a[k], b[k])]
    loader_changes = [k for k in keys if not same(a[k], c[k])]
    not_proxied = [f"{run}:{k}" for run, r in (("b", b), ("c", c)) for k in keys if not r[k][4]]
    loader_missing = [k for k in keys if c[k][2] != "number"]
    say(f"Q6: a={ {k: v[:2] for k, v in a.items() if k != 'xorigin'} }")
    say(f"Q6: D12_proxy_changes_nomode={d12 or 'none'} proxy_changes={proxy_changes or 'none'}"
        f" loader_changes_vs_a={loader_changes or 'none'} not_proxied={not_proxied or 'none'}"
        f" loader_not_run={loader_missing or 'none'}"
        f" D13_xorigin_throws={a['xorigin'].startswith('throws')}")


HELPER = "Alternate Modal Top Most"


def new_top_windows(tree, before, timeout=8.0):
    """New visible top windows in the Edge tree, real dialogs first. IE also
    creates an "Alternate Modal Top Most" helper window for every modal."""
    def find():
        return [win_info(h) for h in top_windows()
                if h not in before and win_info(h)["pid"] in tree
                and win_info(h)["cls"] != HELPER]
    found = wait_until(find, timeout) or []
    time.sleep(1)
    all_new = [win_info(h) for h in top_windows()
               if h not in before and win_info(h)["pid"] in tree]
    for i in all_new:
        ev("    new window:", i["hwnd"], i["cls"], repr(i["title"][:50]), "owner", i["owner"],
           i["owner"] and win32gui.GetClassName(i["owner"]))
    return [i for i in all_new if i["cls"] != HELPER] or found


def describe(i):
    return {k: (hex(v) if k in ("style", "exstyle") else v) for k, v in i.items()} | {
        "children": child_shape(i["hwnd"]),
        "owner_cls": i["owner"] and win32gui.GetClassName(i["owner"])}


def q8_prompt_ignore(fx):
    s = Spike(fx.base_url + "dialogs.htm", native=False, prompt="ignore")
    try:
        d = s.driver
        s.wait_title("Fixture dialogs")
        tree = process_tree(s.edge_pid)
        shapes = {}
        for kind, btn in (("alert", "do_alert"), ("confirm", "do_confirm"),
                          ("prompt", "do_prompt")):
            before = set(top_windows())
            d.execute_script(ASYNC_CLICK, d.find_element("id", btn))
            wins = new_top_windows(tree, before)
            if not wins:
                ev(f"{kind}: no new window")
                continue
            info = wins[0]
            shapes[kind] = describe(info)
            ev(f"{kind} window: {shapes[kind]}")
            if kind == "alert":
                ok, r, _ = in_thread(lambda: d.execute_script("return 1"), 10)
                ev(f"alert: execute_script finished={ok} result={r!r}"[:300])
                ok, r, _ = in_thread(d.get_screenshot_as_png, 15)
                ev(f"alert: screenshot finished={ok} result="
                   f"{(len(r), 'bytes') if isinstance(r, bytes) else repr(r)[:200]}")
                still = win32gui.IsWindow(info["hwnd"])
                ok, txt, _ = in_thread(lambda: d.switch_to.alert.text, 10)
                ev(f"alert still open={still}; switch_to.alert.text={txt!r}")
                shapes["alert_survived"] = still and txt == "Hello alert"
                # Reading the document that owns an open alert over COM
                srv = ie_servers(info["owner"]) if info["owner"] else []
                main = [w for w in windows_of(tree) if ie_servers(w["hwnd"])]
                msrv = ie_servers(main[0]["hwnd"])[0] if main else None
                try:
                    (t, rej) = com_retry(lambda: document_from_hwnd(msrv).title)
                    ev(f"alert: owner document over COM title={t!r} rejections={rej}")
                except Exception as exc:
                    ev(f"alert: owner document over COM failed {exc!r}"[:300])
                _ = srv
                ok, r, _ = in_thread(lambda: d.switch_to.alert.accept(), 10)
                ev(f"alert accept via driver finished={ok} {r!r}")
            elif kind == "confirm":
                ok, r, _ = in_thread(lambda: d.switch_to.alert.text, 10)
                ev(f"confirm: driver reaches it: finished={ok} text={r!r}")
                # A confirm is a TaskDialog (DirectUIHWND child): try WM_COMMAND
                # IDOK, then on a second confirm IDCANCEL.
                for attempt, (cmd, want) in enumerate(((1, "true"), (2, "false"))):
                    if attempt:
                        before = set(top_windows())
                        d.execute_script(ASYNC_CLICK, d.find_element("id", btn))
                        wins = new_top_windows(tree, before)
                        info = wins[0]
                    win32gui.PostMessage(info["hwnd"], win32con.WM_COMMAND, cmd, 0)
                    time.sleep(1.5)
                    closed = not win32gui.IsWindow(info["hwnd"])
                    res = d.execute_script(
                        "return document.getElementById('confirm_result').innerText")
                    ev(f"confirm: WM_COMMAND {cmd} closed={closed} result={res!r} (want {want})")
                    shapes[f"confirm_wm_command_{cmd}"] = closed and res == want
                    if not closed:
                        in_thread(lambda: d.switch_to.alert.dismiss(), 10)
                        time.sleep(1)
            else:
                ok, r, _ = in_thread(lambda: d.switch_to.alert.text, 10)
                ev(f"{kind}: driver reaches it: finished={ok} text={r!r}")
                shapes[kind + "_driver"] = ok and isinstance(r, str)
                if kind == "prompt":
                    # compare with a showModalDialog window, via Win32 only
                    ev("prompt Edit controls:",
                       [c for c in children(info["hwnd"]) if win32gui.GetClassName(c) == "Edit"])
                    ev("prompt IE servers:", ie_servers(info["hwnd"]))
                    if ie_servers(info["hwnd"]):
                        try:
                            pdoc = document_from_hwnd(ie_servers(info["hwnd"])[0])
                            ev("prompt document: title", repr(pdoc.title), "url", pdoc.url)
                            del pdoc
                        except Exception as exc:
                            ev("prompt document failed:", repr(exc)[:200])
                if shapes[kind + "_driver"]:
                    ok, r, _ = in_thread(lambda: d.switch_to.alert.dismiss(), 10)
                    ev(f"{kind}: dismiss via driver finished={ok} {r!r}")
                    time.sleep(1)
                if win32gui.IsWindow(info["hwnd"]):
                    post_close(info["hwnd"])
                    ev(f"{kind}: closed with WM_CLOSE")
                time.sleep(1)
                res = d.execute_script(
                    "return document.getElementById('%s_result').innerText" % kind)
                ev(f"{kind}: result after dismiss: {res!r}")
            time.sleep(1)

        # A prompt and a showModalDialog window side by side
        s.goto(fx.base_url + "modal_opener.htm", "modal opener")
        d.execute_script(ASYNC_CLICK, d.find_element("id", "open_lookup"))
        dlg = wait_dialog(10)
        ev("lookup dialog window:", describe(dlg) if dlg else None)
        # An alert inside the dialog
        srv = ie_servers(dlg["hwnd"])[0]
        doc = document_from_hwnd(srv)
        before = set(top_windows())
        com_js(doc, "window.setTimeout(function(){document.getElementById('dialog_alert')"
                    ".click();},0);")
        del doc
        wins = new_top_windows(tree, before)
        alert_in_dialog = None
        if wins:
            a = wins[0]
            alert_in_dialog = describe(a)
            ev("alert inside dialog:", alert_in_dialog)
            ev("owner is the dialog:", a["owner"] == dlg["hwnd"])
            try:
                (t, rej) = com_retry(lambda: document_from_hwnd(srv).title)
                ev(f"dialog document over COM while its alert is open: {t!r}, rejections {rej}")
            except Exception as exc:
                ev(f"dialog document over COM while its alert is open failed: {exc!r}"[:300])
            # close it the way ie_dialog would: WM_COMMAND with the OK button id
            ok_ids = [c[1] for c in child_shape(a["hwnd"]) if c[0] == "Button"]
            ev("alert button ids:", ok_ids)
            if ok_ids:
                win32gui.PostMessage(a["hwnd"], win32con.WM_COMMAND, ok_ids[0], 0)
                time.sleep(1.5)
            ev("alert closed by WM_COMMAND:", not win32gui.IsWindow(a["hwnd"]))
            doc = document_from_hwnd(srv)
            ev("dialog_alert_result:", js_value(doc, "document.getElementById("
                                                "'dialog_alert_result').innerText", "execScript"))
            del doc
        close_dialogs()
        say(f"Q8: alert_survives_script_and_screenshot={shapes.get('alert_survived')}"
            f" alert_cls={shapes.get('alert', {}).get('cls')}"
            f" confirm_cls={shapes.get('confirm', {}).get('cls')}"
            f" prompt_cls={shapes.get('prompt', {}).get('cls')}"
            f" prompt_driver_reach={shapes.get('prompt_driver')}"
            f" confirm_wm_command_ok={shapes.get('confirm_wm_command_1')}"
            f" confirm_wm_command_cancel={shapes.get('confirm_wm_command_2')}"
            f" alert_in_dialog_owner_cls={alert_in_dialog and alert_in_dialog['owner_cls']}")
    finally:
        close_dialogs()
        s.quit()


QUESTIONS = {
    "q0": q0_trident,
    "q1": q1_driver_click_blocks,
    "q1r": q1r_input_reliability,
    "q2": q2_handles,
    "q3": q3_win32_com,
    "q4": q4_per_process_proxy,
    "q5": q5_console,
    "q6": q6_document_modes,
    "q7": q7_popup_blocker,
    "q8": q8_prompt_ignore,
}


def leftovers():
    rows = list_processes()
    return [(n, p) for n, p, _pp, c in rows
            if n.lower() == "iedriverserver.exe" or ("--ie-mode-force" in c)]


def main(argv):
    names = [a.lower() for a in argv] or list(QUESTIONS)
    pre = leftovers()
    if pre:
        say("WARNING: IE-mode processes already running before the spike:", pre)
    fx = start_fixture_server()
    say("fixture", fx.base_url, fx.alt_base_url, "driver", DRIVER)
    try:
        for name in names:
            say(f"--- {name}")
            try:
                QUESTIONS[name](fx)
            except Exception:
                say(f"{name.upper()}: ERROR")
                traceback.print_exc()
    finally:
        fx.stop()
    time.sleep(2)
    left = [p for p in leftovers() if p not in pre]
    say("leftover IE-mode processes:", left or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
