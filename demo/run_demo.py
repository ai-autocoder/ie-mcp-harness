"""Scripted demo: a fixed sequence of real MCP tool calls against the bundled fixture site.

It starts the fixture site and ``python -m ie_mcp`` (over stdio, the way an MCP client does),
then calls the tools in a fixed order and prints each call:

    python demo/run_demo.py                  # run it
    conhost.exe python demo/run_demo.py --record out.mkv
                                             # lay out the windows and record them with ffmpeg

``--record`` needs ffmpeg on PATH (or ``FFMPEG``) and a classic console window (``conhost.exe``):
the script sets a large console font, puts its console on the left and the IE-mode window on the
right (centered on the screen, where IE opens script dialogs), keeps both on top of a black
backdrop, and records only that region (near-lossless). The run ends back on the start page and
the start screen, so the clip loops. demo/README.md has the encoding commands.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ie_mcp_fixtures.server import start_fixture_server  # noqa: E402

PORT = 8765  # fixed, so the URL in the clip is the same every run
PAUSE = 0.8
HEADER = ["ie-mcp: scripted MCP tool calls", "test page on 127.0.0.1, Edge IE mode", ""]
CYAN, GREEN, RED, DIM, RESET = "\x1b[96m", "\x1b[92m", "\x1b[91m", "\x1b[90m", "\x1b[0m"


class Mcp:
    """Newline-delimited JSON-RPC to ``python -m ie_mcp``, one request at a time."""

    def __init__(self, env):
        self.proc = subprocess.Popen([sys.executable, "-m", "ie_mcp"], cwd=str(ROOT), env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self.lines, self.next_id = queue.Queue(), 0
        threading.Thread(target=self._read, daemon=True).start()
        self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "ie-mcp-demo", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self):
        for raw in self.proc.stdout:
            self.lines.put(raw.decode("utf-8"))
        self.lines.put(None)

    def _send(self, message):
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def rpc(self, method, params):
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        while True:
            line = self.lines.get(timeout=240)
            if line is None:
                raise SystemExit(f"the server exited during {method}")
            response = json.loads(line)
            if response.get("id") == self.next_id:
                if "error" in response:
                    raise SystemExit(f"{method} failed: {response['error']}")
                return response["result"]

    def call(self, name, **arguments):
        """(ok, text, ms) of one tools/call."""
        start = time.perf_counter()
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        ms = round((time.perf_counter() - start) * 1000)
        text = result["content"][0]["text"]  # later items are NOTICE lines
        ok = not result.get("isError") and '"ok": false' not in text
        return ok, text, ms

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=60)


def show(tool, detail, ok, ms, note=None):
    status = f"{GREEN}ok{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"-> {CYAN}{tool:<11}{RESET}{detail:<12}{status} {DIM}{ms:>5} ms{RESET}", flush=True)
    if note:
        print(f"   {DIM}{note}{RESET}", flush=True)


def step(mcp, tool, detail, note=None, **arguments):
    ok, text, ms = mcp.call(tool, **arguments)
    show(tool, detail, ok, ms, note(text) if (ok and note) else None)
    if not ok:
        raise SystemExit(f"{tool} failed: {text}")
    time.sleep(PAUSE)
    return text


def start_screen():
    print("\x1b[?25l\x1b[2J\x1b[H", end="")  # hide the cursor, clear the screen
    for line in HEADER:
        print(line)
    sys.stdout.flush()


# --- window layout and recording (Windows only, --record) -------------------------------------

user32, dwmapi, kernel32 = ctypes.windll.user32, ctypes.windll.dwmapi, ctypes.windll.kernel32
HWND_TOPMOST, HWND_NOTOPMOST, SWP_NOACTIVATE = -1, -2, 0x10
# without argtypes ctypes passes -1 as a 32-bit int and SetWindowPos fails silently on 64-bit
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wt.UINT]
kernel32.GetConsoleWindow.restype = wt.HWND


class FontInfo(ctypes.Structure):
    _fields_ = [("cbSize", wt.ULONG), ("nFont", wt.DWORD), ("dwFontSize", wt._COORD),
                ("FontFamily", wt.UINT), ("FontWeight", wt.UINT), ("FaceName", wt.WCHAR * 32)]


def enable_vt():
    handle, mode = kernel32.GetStdHandle(-11), wt.DWORD()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, mode.value | 4)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING


def size_console(font_height, cols, rows):
    """Large font, and a buffer exactly the window's size: no scroll bars in the clip."""
    out = kernel32.GetStdHandle(-11)
    info = FontInfo(cbSize=ctypes.sizeof(FontInfo), dwFontSize=wt._COORD(0, font_height),
                    FontFamily=54, FontWeight=400, FaceName="Consolas")
    kernel32.SetCurrentConsoleFontEx(out, False, ctypes.byref(info))
    # conhost sizes its window in cells and ignores a pixel size: shrink, resize buffer, grow
    kernel32.SetConsoleWindowInfo(out, True, ctypes.byref(wt.SMALL_RECT(0, 0, 0, 0)))
    kernel32.SetConsoleScreenBufferSize(out, wt._COORD(cols, rows))
    kernel32.SetConsoleWindowInfo(out, True, ctypes.byref(wt.SMALL_RECT(0, 0, cols - 1, rows - 1)))
    time.sleep(0.3)  # conhost resizes its window asynchronously


def frames(hwnd):
    """(outer, visible) rects; visible excludes the invisible resize border (DWM frame bounds)."""
    outer, visible = wt.RECT(), wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(outer))
    dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(visible), ctypes.sizeof(visible))
    return outer, visible


def place(hwnd, x, y, w=None, h=None):
    """Put the window's visible frame at x, y (and size w, h); returns its visible w, h."""
    outer, visible = frames(hwnd)
    left, top = visible.left - outer.left, visible.top - outer.top
    right, bottom = outer.right - visible.right, outer.bottom - visible.bottom
    if w is None:
        user32.SetWindowPos(hwnd, HWND_TOPMOST, x - left, y - top, 0, 0, SWP_NOACTIVATE | 1)
    else:
        user32.SetWindowPos(hwnd, HWND_TOPMOST, x - left, y - top, w + left + right,
                            h + top + bottom, SWP_NOACTIVATE)
    _, visible = frames(hwnd)
    return visible.right - visible.left, visible.bottom - visible.top


def main_hwnd(mcp):
    _, text, _ = mcp.call("ie_windows")
    for window in json.loads(text):
        if window.get("kind") == "main":
            return int(window["target"].split(":")[1])
    raise SystemExit("ie_windows listed no main window: " + text)


def backdrop(x, y, w, h):
    """A black top-most window behind the two windows: their rounded corners and the pixels
    around their frames would otherwise show whatever else is on the desktop."""
    import tkinter
    ready = threading.Event()

    def run():  # Tk lives in its own thread so it keeps painting while the demo runs
        root = tkinter.Tk()
        root.overrideredirect(True)
        root.configure(bg="black")
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.attributes("-topmost", True)
        root.update()
        ready.set()
        root.mainloop()

    threading.Thread(target=run, daemon=True).start()
    ready.wait(10)


def start_ffmpeg(path, region):
    x, y, w, h = region
    ffmpeg = os.environ.get("FFMPEG", "ffmpeg")
    return subprocess.Popen(
        [ffmpeg, "-loglevel", "error", "-y", "-f", "gdigrab", "-framerate", "30",
         "-draw_mouse", "0", "-offset_x", str(x), "-offset_y", str(y),
         "-video_size", f"{w}x{h}", "-i", "desktop", "-c:v", "libx264", "-preset", "ultrafast",
         "-crf", "0", "-pix_fmt", "yuv444p", str(path)],
        # its messages would land in the console being recorded
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_ffmpeg(recorder):
    try:
        recorder.communicate(b"q", timeout=30)
    except subprocess.TimeoutExpired:
        recorder.kill()
        recorder.wait()
    if recorder.returncode:
        print(f"ffmpeg exited with {recorder.returncode}: the recording may be incomplete")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--record", metavar="FILE", help="lay out the windows and record them")
    parser.add_argument("--origin", help="x,y of the recorded region in pixels "
                        "(default: the browser part centered on the screen)")
    parser.add_argument("--aspect", type=float, default=4 / 3,
                        help="width/height of the region; the console's height sets the height")
    parser.add_argument("--font", type=int, default=32, help="console font height in pixels")
    parser.add_argument("--cols", type=int, default=40, help="console width in characters")
    parser.add_argument("--rows", type=int, default=29, help="console height in lines")
    args = parser.parse_args()
    region = None

    if args.record:
        from ie_mcp.win32.capture import enable_dpi_awareness
        enable_dpi_awareness()  # physical pixels, the same ones gdigrab records
    enable_vt()
    site = start_fixture_server(port=PORT)
    base = f"http://127.0.0.1:{PORT}/"
    session_root = tempfile.mkdtemp(prefix="ie-mcp-demo-")
    env = dict(os.environ, IE_INTERACTION_MODE="background", IE_SESSION_ROOT=session_root)
    mcp = Mcp(env)
    recorder = edge = None
    try:
        print("starting the IE-mode browser...", flush=True)
        ok, text, _ = mcp.call("ie_open", url=base + "index.htm")
        if not ok:
            raise SystemExit("ie_open failed: " + text)
        # off camera: verifies capture (D11), and leaves Back enabled as it is at the end (loop)
        mcp.call("ie_goto", url=base + "demo.htm", wait=0.5)
        mcp.call("ie_goto", url=base + "index.htm", wait=0.5)
        if args.record:
            size_console(args.font, args.cols, args.rows)
            left, h = place(kernel32.GetConsoleWindow(), 0, 0)
            h -= h % 2
            w = round(h * args.aspect / 2) * 2
            if args.origin:
                x, y = (int(v) for v in args.origin.split(","))
            else:  # IE centers script dialogs on the screen: center the browser part there
                x = (user32.GetSystemMetrics(0) - (w - left)) // 2 - left
                y = (user32.GetSystemMetrics(1) - h) // 2
            backdrop(x - 40, y - 40, w + 80, h + 80)
            kernel32.SetConsoleTitleW("ie-mcp demo")
            edge = main_hwnd(mcp)
            user32.ShowWindow(edge, 9)  # SW_RESTORE: a maximized window snaps back when moved
            place(edge, x + left, y, w - left, h)
            place(kernel32.GetConsoleWindow(), x, y)  # last, so it is above the backdrop
            region = [x, y, w, h]
        start_screen()
        time.sleep(1.0)
        if args.record:
            recorder = start_ffmpeg(args.record, region)
            time.sleep(1.0)  # the first frames of gdigrab arrive late; trimmed when encoding

        step(mcp, "ie_goto", "demo.htm", url=base + "demo.htm", wait=0.3)  # ie_fill auto-waits
        step(mcp, "ie_fill", "#code", by="id", value="code", text="A-1024")
        step(mcp, "ie_fill", "#customer", by="id", value="customer", text="Test customer")
        step(mcp, "ie_click", "#send", by="id", value="send", mode="async", wait=0.3)
        step(mcp, "ie_dialog", "accept",
             note=lambda t: "answered: " + json.loads(t).get("text", ""))
        for _ in range(50):  # the post lands once the dialog is answered
            ok, text, ms = mcp.call("ie_network", method="POST")
            if "/echo" in text:
                break
            time.sleep(0.1)
        posts = [l for l in text.splitlines() if "/echo" in l]
        if not posts:
            raise SystemExit("ie_network never listed the POST to /echo: " + text)
        line = posts[0].split()
        show("ie_network", "POST", ok, ms, note=f"{line[2]} /echo -> {line[3]}, {line[5]} bytes")
        time.sleep(2.0)

        mcp.call("ie_goto", url=base + "index.htm", wait=0.5)  # back to the first frame: loops
        start_screen()
        time.sleep(1.5)
    finally:
        print("\x1b[?25h", end="", flush=True)  # cursor back
        if recorder:
            stop_ffmpeg(recorder)
        if args.record:  # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
            for hwnd in (kernel32.GetConsoleWindow(), edge):
                if hwnd:
                    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, 0x13)
        # each step on its own, so one failure (a dead server) does not skip the rest
        for cleanup in (lambda: mcp.call("ie_close"), mcp.close, site.stop,
                        lambda: shutil.rmtree(session_root, ignore_errors=True)):
            try:
                cleanup()
            except Exception as exc:  # noqa: BLE001 - report and keep cleaning up
                print(f"cleanup: {exc!r}")


if __name__ == "__main__":
    main()
