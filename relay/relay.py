#!/usr/bin/env python3
"""
Browser Relay — 把一台机器上 headed Chrome 的画面推给手机网页，手机上的触摸/键盘操作传回去。

Mac 本地和 Linux 服务器（xvfb）都能跑；Playwright 等自动化工具可以通过
CDP 端口共享同一个浏览器实例，人和脚本看到的是同一个现场。
"""

import asyncio
import ctypes
import ctypes.util
import hashlib
import hmac
import json
import os
import signal
import sys
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from shutil import which

try:
    from websockets.asyncio.server import serve
    from websockets.asyncio.client import connect as ws_connect
    from websockets import Response
    from websockets.datastructures import Headers
except ImportError:
    print("需要安装: pip3 install websockets")
    sys.exit(1)

DEFAULT_PORT = int(os.environ.get("BROWSER_RELAY_PORT", "8271"))
DEFAULT_HOST = os.environ.get("BROWSER_RELAY_HOST", "127.0.0.1")
CDP_PORT = int(os.environ.get("BROWSER_RELAY_CDP_PORT", "9333"))
DEFAULT_WIDTH = 390
DEFAULT_HEIGHT = 844
LINUX_CHROME_WINDOW_WIDTH = 500
LINUX_CHROME_WINDOW_HEIGHT = 960
PROFILE_DIR = Path(os.environ.get("BROWSER_RELAY_PROFILE", str(Path.home() / ".browser-relay-profile"))).expanduser()
SCREENCAST_QUALITY = 85
_PASS = os.environ.get("BROWSER_RELAY_PASS", "")
# 登录凭证：客户端发 sha256(密码)；会话 cookie 是另一个派生值，由服务器以 HttpOnly 下发，
# 页面里的 JS 拿不到，也推不回密码哈希。
AUTH_TOKEN = hashlib.sha256(_PASS.encode()).hexdigest()
COOKIE_TOKEN = hashlib.sha256(f"relay-cookie-v1:{_PASS}".encode()).hexdigest()
COOKIE_NAME = "relay_auth"
COOKIE_MAX_AGE = 30 * 24 * 3600


class X11Input:
    """Inject the phone operator's input through the X11 server, not CDP Input.*."""

    BUTTON_LEFT = 1
    BUTTON_SCROLL_UP = 4
    BUTTON_SCROLL_DOWN = 5
    BUTTON_SCROLL_LEFT = 6
    BUTTON_SCROLL_RIGHT = 7
    WHEEL_PIXELS_PER_TICK = 36

    KEY_NAMES = {
        "Backspace": "BackSpace",
        "Enter": "Return",
        "Tab": "Tab",
        "Escape": "Escape",
        "ArrowLeft": "Left",
        "ArrowRight": "Right",
        "ArrowUp": "Up",
        "ArrowDown": "Down",
    }

    SHIFTED_ASCII = {
        "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
        "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
        "_": "minus", "+": "equal", "{": "bracketleft",
        "}": "bracketright", "|": "backslash", ":": "semicolon",
        '"': "apostrophe", "<": "comma", ">": "period",
        "?": "slash", "~": "grave",
    }

    ASCII_KEY_NAMES = {
        " ": "space", "-": "minus", "=": "equal",
        "[": "bracketleft", "]": "bracketright", "\\": "backslash",
        ";": "semicolon", "'": "apostrophe", ",": "comma",
        ".": "period", "/": "slash", "`": "grave",
    }

    def __init__(self):
        if not os.environ.get("DISPLAY"):
            raise RuntimeError("DISPLAY 未设置")

        x11_path = ctypes.util.find_library("X11")
        xtst_path = ctypes.util.find_library("Xtst")
        if not x11_path or not xtst_path:
            raise RuntimeError("缺少 libX11 或 libXtst")

        self.x11 = ctypes.CDLL(x11_path)
        self.xtst = ctypes.CDLL(xtst_path)
        self._configure_ffi()
        self.display = self.x11.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError(f"无法打开 X11 display {os.environ.get('DISPLAY')}")

        event_base = ctypes.c_int()
        error_base = ctypes.c_int()
        major = ctypes.c_int()
        minor = ctypes.c_int()
        if not self.xtst.XTestQueryExtension(
            self.display,
            ctypes.byref(event_base), ctypes.byref(error_base),
            ctypes.byref(major), ctypes.byref(minor),
        ):
            self.close()
            raise RuntimeError("当前 X server 不支持 XTEST")

        self.screen = self.x11.XDefaultScreen(self.display)
        self.root = self.x11.XRootWindow(self.display, self.screen)
        self._scroll_x_remainder = 0.0
        self._scroll_y_remainder = 0.0

    def _configure_ffi(self):
        c_display = ctypes.c_void_p
        c_window = ctypes.c_ulong

        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XOpenDisplay.restype = c_display
        self.x11.XCloseDisplay.argtypes = [c_display]
        self.x11.XDefaultScreen.argtypes = [c_display]
        self.x11.XDefaultScreen.restype = ctypes.c_int
        self.x11.XRootWindow.argtypes = [c_display, ctypes.c_int]
        self.x11.XRootWindow.restype = c_window
        self.x11.XQueryTree.argtypes = [
            c_display, c_window,
            ctypes.POINTER(c_window), ctypes.POINTER(c_window),
            ctypes.POINTER(ctypes.POINTER(c_window)), ctypes.POINTER(ctypes.c_uint),
        ]
        self.x11.XQueryTree.restype = ctypes.c_int
        self.x11.XGetGeometry.argtypes = [
            c_display, c_window, ctypes.POINTER(c_window),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
        ]
        self.x11.XGetGeometry.restype = ctypes.c_int
        self.x11.XFree.argtypes = [ctypes.c_void_p]
        self.x11.XRaiseWindow.argtypes = [c_display, c_window]
        self.x11.XSetInputFocus.argtypes = [c_display, c_window, ctypes.c_int, ctypes.c_ulong]
        self.x11.XFlush.argtypes = [c_display]
        self.x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
        self.x11.XStringToKeysym.restype = ctypes.c_ulong
        self.x11.XKeysymToKeycode.argtypes = [c_display, ctypes.c_ulong]
        self.x11.XKeysymToKeycode.restype = ctypes.c_uint

        self.xtst.XTestQueryExtension.argtypes = [
            c_display,
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
        ]
        self.xtst.XTestQueryExtension.restype = ctypes.c_int
        self.xtst.XTestFakeMotionEvent.argtypes = [
            c_display, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong,
        ]
        self.xtst.XTestFakeMotionEvent.restype = ctypes.c_int
        self.xtst.XTestFakeButtonEvent.argtypes = [
            c_display, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong,
        ]
        self.xtst.XTestFakeButtonEvent.restype = ctypes.c_int
        self.xtst.XTestFakeKeyEvent.argtypes = [
            c_display, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong,
        ]
        self.xtst.XTestFakeKeyEvent.restype = ctypes.c_int

    def close(self):
        if getattr(self, "display", None):
            self.x11.XCloseDisplay(self.display)
            self.display = None

    def browser_window_geometry(self):
        root_return = ctypes.c_ulong()
        parent_return = ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        child_count = ctypes.c_uint()
        ok = self.x11.XQueryTree(
            self.display, self.root,
            ctypes.byref(root_return), ctypes.byref(parent_return),
            ctypes.byref(children), ctypes.byref(child_count),
        )
        if not ok:
            raise RuntimeError("XQueryTree 失败")

        best = None
        try:
            for i in range(child_count.value):
                window = children[i]
                root = ctypes.c_ulong()
                x = ctypes.c_int()
                y = ctypes.c_int()
                width = ctypes.c_uint()
                height = ctypes.c_uint()
                border = ctypes.c_uint()
                depth = ctypes.c_uint()
                if not self.x11.XGetGeometry(
                    self.display, window, ctypes.byref(root),
                    ctypes.byref(x), ctypes.byref(y),
                    ctypes.byref(width), ctypes.byref(height),
                    ctypes.byref(border), ctypes.byref(depth),
                ):
                    continue
                area = width.value * height.value
                if width.value < 200 or height.value < 200:
                    continue
                if best is None or area > best[0]:
                    best = (area, window, x.value, y.value, width.value, height.value)
        finally:
            if children:
                self.x11.XFree(children)

        if best is None:
            raise RuntimeError("找不到 Chrome 的 X11 窗口")
        _, window, x, y, width, height = best
        return window, x, y, width, height

    def focus_browser(self):
        window, x, y, width, height = self.browser_window_geometry()
        self.x11.XRaiseWindow(self.display, window)
        self.x11.XSetInputFocus(self.display, window, 2, 0)
        self.x11.XFlush(self.display)
        return x, y, width, height

    def motion(self, x, y):
        if not self.xtst.XTestFakeMotionEvent(
            self.display, self.screen, int(x), int(y), 0,
        ):
            raise RuntimeError("XTEST 鼠标移动失败")
        self.x11.XFlush(self.display)

    def button(self, button, pressed):
        if not self.xtst.XTestFakeButtonEvent(
            self.display, int(button), 1 if pressed else 0, 0,
        ):
            raise RuntimeError("XTEST 鼠标按键失败")
        self.x11.XFlush(self.display)

    def reset_scroll(self):
        self._scroll_x_remainder = 0.0
        self._scroll_y_remainder = 0.0

    def scroll(self, delta_x, delta_y):
        self._scroll_x_remainder += float(delta_x)
        self._scroll_y_remainder += float(delta_y)
        emitted = 0

        while abs(self._scroll_y_remainder) >= self.WHEEL_PIXELS_PER_TICK and emitted < 8:
            if self._scroll_y_remainder > 0:
                button = self.BUTTON_SCROLL_DOWN
                self._scroll_y_remainder -= self.WHEEL_PIXELS_PER_TICK
            else:
                button = self.BUTTON_SCROLL_UP
                self._scroll_y_remainder += self.WHEEL_PIXELS_PER_TICK
            self.button(button, True)
            self.button(button, False)
            emitted += 1

        while abs(self._scroll_x_remainder) >= self.WHEEL_PIXELS_PER_TICK and emitted < 8:
            if self._scroll_x_remainder > 0:
                button = self.BUTTON_SCROLL_RIGHT
                self._scroll_x_remainder -= self.WHEEL_PIXELS_PER_TICK
            else:
                button = self.BUTTON_SCROLL_LEFT
                self._scroll_x_remainder += self.WHEEL_PIXELS_PER_TICK
            self.button(button, True)
            self.button(button, False)
            emitted += 1

    def _keycode(self, keysym_name):
        keysym = self.x11.XStringToKeysym(keysym_name.encode("ascii"))
        if not keysym:
            raise RuntimeError(f"未知 X11 keysym: {keysym_name}")
        keycode = self.x11.XKeysymToKeycode(self.display, keysym)
        if not keycode:
            raise RuntimeError(f"没有对应的 X11 keycode: {keysym_name}")
        return keycode

    def _key(self, keysym_name, pressed):
        keycode = self._keycode(keysym_name)
        if not self.xtst.XTestFakeKeyEvent(
            self.display, keycode, 1 if pressed else 0, 0,
        ):
            raise RuntimeError(f"XTEST 键盘事件失败: {keysym_name}")
        self.x11.XFlush(self.display)

    def press_key(self, key):
        keysym_name = self.KEY_NAMES.get(key, key)
        self._key(keysym_name, True)
        self._key(keysym_name, False)

    def shortcut(self, modifiers, key):
        modifier_names = {
            "Control": "Control_L",
            "Shift": "Shift_L",
            "Alt": "Alt_L",
        }
        pressed = [modifier_names.get(modifier, modifier) for modifier in modifiers]
        for modifier in pressed:
            self._key(modifier, True)
        try:
            self.press_key(key)
        finally:
            for modifier in reversed(pressed):
                self._key(modifier, False)

    def _type_ascii(self, char):
        if char == "\n":
            self.press_key("Enter")
            return
        if char in self.SHIFTED_ASCII:
            self.shortcut(["Shift"], self.SHIFTED_ASCII[char])
            return
        if char.isalpha() and char.isupper():
            self.shortcut(["Shift"], char.lower())
            return
        keysym_name = self.ASCII_KEY_NAMES.get(char, char)
        self.press_key(keysym_name)

    def _type_unicode_compose(self, char):
        self._key("Control_L", True)
        self._key("Shift_L", True)
        self.press_key("u")
        self._key("Shift_L", False)
        self._key("Control_L", False)
        for digit in f"{ord(char):x}":
            self.press_key(digit)
        self.press_key("Enter")

    def type_text(self, text):
        for char in text:
            if ord(char) < 128:
                self._type_ascii(char)
            else:
                self._type_unicode_compose(char)


def find_chrome():
    env_bin = os.environ.get("CHROME_BIN")
    if env_bin and Path(env_bin).exists():
        return env_bin

    normal = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if normal.exists():
        return str(normal)

    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = which(name)
        if path:
            return path

    pw_cache = Path.home() / "Library/Caches/ms-playwright"
    for d in sorted(pw_cache.glob("chromium-*"), reverse=True):
        app = d / "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
        if app.exists():
            return str(app)
    return None


class BrowserRelay:
    def __init__(self, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT):
        self.width = width
        self.height = height
        self.clients = set()
        self.chrome_proc = None
        self.cdp_ws = None
        self._msg_id = 0
        self._pending = {}  # msg_id -> Future
        self._running = True
        self._last_frame = None
        self._send_lock = asyncio.Lock()
        self._reader_task = None
        self._cdp_alive = asyncio.Event()
        self._switching = False
        self.x11 = None
        self._input_lock = asyncio.Lock()
        self._current_target_id = None
        self._chrome_top_inset = None
        self._chrome_left_inset = 0
        self._input_origin_x = None
        self._input_origin_y = None

    def _next_id(self):
        self._msg_id += 1
        return self._msg_id

    async def _cdp_send(self, method, params=None):
        """Send a CDP message (fire-and-forget)."""
        async with self._send_lock:
            mid = self._next_id()
            msg = {"id": mid, "method": method}
            if params:
                msg["params"] = params
            await self.cdp_ws.send(json.dumps(msg))
            return mid

    async def cdp_call(self, method, params=None):
        """Send CDP command and wait for response."""
        fut = asyncio.get_event_loop().create_future()
        mid = await self._cdp_send(method, params)
        self._pending[mid] = fut
        try:
            return await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise

    async def cdp_fire(self, method, params=None):
        """Fire-and-forget CDP command."""
        await self._cdp_send(method, params)

    async def _cdp_reader(self):
        """Continuously read CDP WebSocket, dispatch responses and events."""
        try:
            async for raw in self.cdp_ws:
                data = json.loads(raw)

                msg_id = data.get("id")
                if msg_id and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        if "error" in data:
                            fut.set_result(data["error"])
                        else:
                            fut.set_result(data.get("result", {}))
                    continue

                if data.get("method") == "Page.screencastFrame":
                    params = data["params"]
                    await self.cdp_fire("Page.screencastFrameAck", {
                        "sessionId": params.get("sessionId", 0),
                    })
                    frame_data = json.dumps({
                        "type": "frame",
                        "data": params["data"],
                    })
                    self._last_frame = frame_data
                    if self.clients:
                        await asyncio.gather(
                            *[self._safe_send(c, frame_data) for c in self.clients],
                            return_exceptions=True,
                        )
        except Exception as e:
            if self._running:
                print(f"CDP 读取断开: {e}")
        finally:
            self._cdp_alive.clear()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("CDP disconnected"))
            self._pending.clear()
            if self._running and not self._switching:
                asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        """CDP 断了以后持续尝试重连。"""
        while self._running:
            await asyncio.sleep(2)
            pages = self._get_pages()
            if pages:
                ws_url = pages[0]["webSocketDebuggerUrl"]
                try:
                    await self._connect_cdp(ws_url)
                    print(f"自动重连成功: {pages[0].get('title', '')}")
                    return
                except Exception as e:
                    print(f"重连失败: {e}")

    async def _connect_cdp(self, ws_url):
        """Connect to a CDP page WebSocket and start reader + screencast."""
        self._switching = True
        try:
            if self.cdp_ws:
                await self.cdp_ws.close()
        except Exception:
            pass

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass

        self.cdp_ws = await ws_connect(ws_url, max_size=50 * 1024 * 1024, ping_interval=None)
        self._switching = False
        self._cdp_alive.set()
        self._reader_task = asyncio.create_task(self._cdp_reader())

        await self.cdp_call("Page.enable")
        self._current_target_id = ws_url.rstrip("/").rsplit("/", 1)[-1]

        if self._chrome_top_inset is None:
            metrics = await self.cdp_call("Runtime.evaluate", {
                "expression": (
                    "({top:Math.max(0,outerHeight-innerHeight),"
                    "left:Math.max(0,outerWidth-innerWidth)})"
                ),
                "returnByValue": True,
            })
            native_metrics = ((metrics.get("result") or {}).get("value") or {})
            self._chrome_top_inset = int(native_metrics.get("top", 0))
            self._chrome_left_inset = int(native_metrics.get("left", 0))

        await self.cdp_call("Emulation.setDeviceMetricsOverride", {
            "width": self.width, "height": self.height,
            "deviceScaleFactor": 2, "mobile": True,
        })

        await self.cdp_fire("Page.startScreencast", {
            "format": "jpeg",
            "quality": SCREENCAST_QUALITY,
            "maxWidth": self.width * 2,
            "maxHeight": self.height * 2,
            "everyNthFrame": 1,
        })

        result = await self.cdp_call("Page.captureScreenshot", {
            "format": "jpeg", "quality": 60,
        })
        if "data" in result:
            self._last_frame = json.dumps({"type": "frame", "data": result["data"]})

    async def start_browser(self):
        chrome_bin = find_chrome()
        if not chrome_bin:
            print("找不到 Chrome")
            sys.exit(1)

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        subprocess.run(["pkill", "-f", f"--user-data-dir={PROFILE_DIR}"], capture_output=True)
        await asyncio.sleep(1)

        chrome_window_width = self.width
        chrome_window_height = self.height
        if sys.platform.startswith("linux"):
            chrome_window_width = max(LINUX_CHROME_WINDOW_WIDTH, self.width)
            chrome_window_height = LINUX_CHROME_WINDOW_HEIGHT

        args = [
            chrome_bin,
            f"--remote-debugging-port={CDP_PORT}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={PROFILE_DIR}",
            f"--window-size={chrome_window_width},{chrome_window_height}",
            "--disable-blink-features=AutomationControlled",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
            "--no-first-run",
            "--no-default-browser-check",
            "--noerrdialogs",
            "--disable-session-crashed-bubble",
            "--restore-last-session=false",
            "--profile-directory=Default",
            "about:blank",
        ]
        if sys.platform.startswith("linux"):
            args.insert(-1, "--window-position=0,0")
            args.insert(-1, "--no-sandbox")
            args.insert(-1, "--disable-dev-shm-usage")

        self.chrome_proc = subprocess.Popen(args)

        if sys.platform == "darwin":
            asyncio.get_event_loop().call_later(2, lambda: subprocess.Popen([
                "osascript", "-e",
                'tell application "Google Chrome" to set miniaturized of front window to true',
            ]))

        page_ws_url = None
        for _ in range(30):
            try:
                resp = urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json")
                pages = json.loads(resp.read())
                for p in pages:
                    if p.get("type") == "page":
                        page_ws_url = p["webSocketDebuggerUrl"]
                        break
                if page_ws_url:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.3)

        if not page_ws_url:
            print("Chrome 启动超时")
            sys.exit(1)

        await self._connect_cdp(page_ws_url)

        try:
            self.x11 = X11Input()
            initial_target_id = self._current_target_id
            calibration_target = self._open_target("about:blank")
            await self._connect_cdp(calibration_target["webSocketDebuggerUrl"])
            if initial_target_id and initial_target_id != self._current_target_id:
                try:
                    self._close_target(initial_target_id)
                except Exception as e:
                    print(f"警告: 初始空白标签关闭失败: {e}")
            await self._calibrate_x11_input()
            geometry = self.x11.focus_browser()
            effective_width, effective_height = self._fit_viewport_to_window(
                self.width, self.height, geometry,
            )
            if (effective_width, effective_height) != (self.width, self.height):
                self.width, self.height = effective_width, effective_height
                await self.cdp_call("Emulation.setDeviceMetricsOverride", {
                    "width": self.width, "height": self.height,
                    "deviceScaleFactor": 2, "mobile": True,
                })
                print(f"viewport 已限制到 X11 可触达范围: {self.width}x{self.height}")
            print(
                "X11 人工输入已启用 "
                f"display={os.environ.get('DISPLAY')} "
                f"input_origin={self._input_origin_x},{self._input_origin_y}"
            )
        except Exception as e:
            if self.x11:
                self.x11.close()
            self.x11 = None
            print(f"警告: X11 人工输入不可用，手机仍可看画面但不能操作: {e}")

        await self.cdp_call("Page.navigate", {"url": "https://www.google.com"})
        print(f"浏览器已启动 viewport={self.width}x{self.height}")

    async def _safe_send(self, ws, data):
        try:
            await ws.send(data)
        except Exception:
            self.clients.discard(ws)

    async def handle_client(self, ws):
        self.clients.add(ws)
        remote = ws.remote_address
        print(f"客户端连接: {remote}")

        await ws.send(json.dumps({
            "type": "init",
            "width": self.width,
            "height": self.height,
        }))

        if self._last_frame:
            await self._safe_send(ws, self._last_frame)

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    reply = await self._handle_input(msg)
                    if reply:
                        await self._safe_send(ws, reply)
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass
        finally:
            self.clients.discard(ws)
            print(f"客户端断开: {remote}")

    def _get_pages(self):
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json")
            return [p for p in json.loads(resp.read()) if p.get("type") == "page"]
        except Exception:
            return []

    def _open_target(self, url):
        request = urllib.request.Request(
            f"http://127.0.0.1:{CDP_PORT}/json/new?"
            + urllib.parse.quote(url, safe=""),
            method="PUT",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read())

    def _close_target(self, target_id):
        with urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json/close/{target_id}", timeout=2,
        ) as response:
            response.read()

    def _activate_target(self, target_id):
        if not target_id:
            raise RuntimeError("没有可激活的标签页")
        url = f"http://127.0.0.1:{CDP_PORT}/json/activate/{target_id}"
        with urllib.request.urlopen(url, timeout=2) as resp:
            resp.read()

    async def _prepare_human_input(self):
        if self.x11 is None:
            raise RuntimeError("X11 人工输入未初始化")
        if not self._current_target_id:
            raise RuntimeError("当前没有 relay 标签页")
        self._activate_target(self._current_target_id)
        await asyncio.sleep(0.03)
        return self.x11.focus_browser()

    async def _calibrate_x11_input(self):
        """Measure the emulated page's real X11 origin on a throwaway blank page."""
        calibration_html = (
            '<!doctype html><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>relay-input-calibration</title>'
        )
        calibration_url = "data:text/html;charset=utf-8," + urllib.parse.quote(
            calibration_html,
        )
        await self.cdp_call("Page.navigate", {"url": calibration_url})
        await asyncio.sleep(0.15)

        geometry = self.x11.focus_browser()
        window_x, window_y, window_width, window_height = geometry
        self.x11.motion(window_x + 8, window_y + 8)
        await self.cdp_call("Runtime.evaluate", {
            "expression": (
                "window.__relayInputCalibration=null;"
                "addEventListener('mousemove',e=>{"
                "window.__relayInputCalibration={x:e.clientX,y:e.clientY,"
                "trusted:e.isTrusted}}, {once:true})"
            ),
        })

        sample_root_x = window_x + window_width // 2
        sample_root_y = window_y + window_height // 2
        self.x11.motion(sample_root_x, sample_root_y)
        await asyncio.sleep(0.08)
        result = await self.cdp_call("Runtime.evaluate", {
            "expression": "window.__relayInputCalibration",
            "returnByValue": True,
        })
        point = ((result.get("result") or {}).get("value") or {})
        if not point or not point.get("trusted"):
            raise RuntimeError("X11 坐标校准没有收到可信鼠标事件")

        origin_x = round(sample_root_x - window_x - float(point["x"]))
        origin_y = round(sample_root_y - window_y - float(point["y"]))
        if not (0 <= origin_x < window_width and 0 <= origin_y < window_height):
            raise RuntimeError(f"X11 坐标校准结果越界: {origin_x},{origin_y}")
        self._input_origin_x = origin_x
        self._input_origin_y = origin_y

    def _fit_viewport_to_window(self, width, height, window_geometry):
        _, _, window_width, window_height = window_geometry
        top = max(0, min(int(self._chrome_top_inset or 0), window_height - 1))
        left = max(0, min(int(self._chrome_left_inset or 0), window_width - 1))
        origin_x = int(self._input_origin_x if self._input_origin_x is not None else left)
        origin_y = int(self._input_origin_y if self._input_origin_y is not None else top)
        max_width = max(1, window_width - origin_x)
        max_height = max(1, window_height - origin_y)
        return (
            max(1, min(int(width), max_width)),
            max(1, min(int(height), max_height)),
        )

    def _page_to_root(self, x, y, window_geometry):
        window_x, window_y, window_width, window_height = window_geometry
        top = max(0, min(int(self._chrome_top_inset or 0), window_height - 1))
        left = max(0, min(int(self._chrome_left_inset or 0), window_width - 1))
        origin_x = int(self._input_origin_x if self._input_origin_x is not None else left)
        origin_y = int(self._input_origin_y if self._input_origin_y is not None else top)
        page_x = max(0.0, min(float(x), float(self.width)))
        page_y = max(0.0, min(float(y), float(self.height)))
        root_x = window_x + origin_x + round(page_x)
        root_y = window_y + origin_y + round(page_y)
        root_x = max(window_x, min(root_x, window_x + window_width - 1))
        root_y = max(window_y, min(root_y, window_y + window_height - 1))
        return root_x, root_y

    async def _human_input(self, action):
        async with self._input_lock:
            geometry = await self._prepare_human_input()
            return await action(geometry)

    async def _handle_input(self, msg):
        t = msg.get("type")

        if t == "list_tabs":
            pages = self._get_pages()
            tabs = [{"id": p["id"], "title": p.get("title", ""), "url": p.get("url", "")} for p in pages]
            return json.dumps({"type": "tabs", "tabs": tabs})

        if not self._cdp_alive.is_set():
            return

        if t == "switch_tab":
            target_id = msg.get("id", "")
            pages = self._get_pages()
            for p in pages:
                if p["id"] == target_id:
                    self._activate_target(target_id)
                    await self._connect_cdp(p["webSocketDebuggerUrl"])
                    print(f"切换到: {p.get('title', '')}")
                    break

        elif t == "tap":
            x, y = msg["x"], msg["y"]
            duration_ms = max(1, min(int(msg.get("durationMs", 80)), 1000))

            async def tap(geometry):
                root_x, root_y = self._page_to_root(x, y, geometry)
                self.x11.motion(root_x, root_y)
                self.x11.button(X11Input.BUTTON_LEFT, True)
                await asyncio.sleep(duration_ms / 1000)
                self.x11.button(X11Input.BUTTON_LEFT, False)

            try:
                await self._human_input(tap)
            except Exception as e:
                return json.dumps({"type": "input_error", "message": str(e)})

        elif t == "scroll":
            x, y = msg.get("x", self.width // 2), msg.get("y", self.height // 2)
            dx, dy = msg.get("deltaX", 0), msg.get("deltaY", 0)

            async def scroll(geometry):
                root_x, root_y = self._page_to_root(x, y, geometry)
                self.x11.motion(root_x, root_y)
                if msg.get("phase") == "start":
                    self.x11.reset_scroll()
                self.x11.scroll(dx, dy)

            try:
                await self._human_input(scroll)
            except Exception as e:
                return json.dumps({"type": "input_error", "message": str(e)})

        elif t == "scroll_end":
            if self.x11:
                self.x11.reset_scroll()

        elif t == "keypress":
            key = msg.get("key", "")

            async def keypress(_geometry):
                self.x11.press_key(key)

            try:
                await self._human_input(keypress)
            except Exception as e:
                return json.dumps({"type": "input_error", "message": str(e)})

        elif t == "type":
            text = msg.get("text", "")

            async def type_text(_geometry):
                self.x11.type_text(text)

            try:
                await self._human_input(type_text)
            except Exception as e:
                return json.dumps({"type": "input_error", "message": str(e)})

        elif t == "navigate":
            url = msg.get("url", "")
            if url:
                async def navigate(_geometry):
                    self.x11.shortcut(["Control"], "l")
                    self.x11.type_text(url)
                    self.x11.press_key("Enter")

                try:
                    await self._human_input(navigate)
                except Exception as e:
                    return json.dumps({"type": "input_error", "message": str(e)})

        elif t == "resize":
            w, h = msg.get("width", self.width), msg.get("height", self.height)
            if self.x11:
                w, h = self._fit_viewport_to_window(
                    w, h, self.x11.browser_window_geometry()[1:],
                )
            else:
                w = max(1, min(int(w), 4096))
                h = max(1, min(int(h), 4096))
            self.width, self.height = w, h
            await self.cdp_call("Emulation.setDeviceMetricsOverride", {
                "width": w, "height": h, "deviceScaleFactor": 2, "mobile": True,
            })
            return json.dumps({"type": "viewport", "width": w, "height": h})

        elif t == "back":
            async def back(_geometry):
                self.x11.shortcut(["Alt"], "Left")

            try:
                await self._human_input(back)
            except Exception as e:
                return json.dumps({"type": "input_error", "message": str(e)})

        elif t == "forward":
            async def forward(_geometry):
                self.x11.shortcut(["Alt"], "Right")

            try:
                await self._human_input(forward)
            except Exception as e:
                return json.dumps({"type": "input_error", "message": str(e)})

        elif t == "refresh":
            async def refresh(_geometry):
                self.x11.shortcut(["Control"], "r")

            try:
                await self._human_input(refresh)
            except Exception as e:
                return json.dumps({"type": "input_error", "message": str(e)})

    async def stop(self):
        self._running = False
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self.cdp_ws:
            await self.cdp_ws.close()
        if self.x11:
            self.x11.close()
        if self.chrome_proc:
            self.chrome_proc.terminate()
            self.chrome_proc.wait()
        print("浏览器已关闭")


CLIENT_HTML = Path(__file__).parent / "client.html"
LOGIN_HTML = Path(__file__).parent / "login.html"


def _safe_equal(a, b):
    try:
        return hmac.compare_digest(a, b)
    except TypeError:
        return False


def _check_cookie(request):
    cookie_header = request.headers.get("Cookie", "")
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(f"{COOKIE_NAME}="):
            return _safe_equal(part.split("=", 1)[1], COOKIE_TOKEN)
    return False


def process_request(connection, request):
    if request.headers.get("Upgrade", "").lower() == "websocket":
        if not _check_cookie(request):
            return Response(403, "Forbidden", Headers(), b"unauthorized")
        return

    auth_header = request.headers.get("X-Relay-Auth")
    if auth_header is not None:
        if _safe_equal(auth_header, AUTH_TOKEN):
            headers = Headers([(
                "Set-Cookie",
                f"{COOKIE_NAME}={COOKIE_TOKEN}; Path=/; Max-Age={COOKIE_MAX_AGE}; HttpOnly; SameSite=Strict",
            )])
            return Response(204, "No Content", headers, b"")
        return Response(403, "Forbidden", Headers(), b"wrong password")

    if not _check_cookie(request):
        try:
            html = LOGIN_HTML.read_bytes()
            return Response(200, "OK", Headers([("Content-Type", "text/html; charset=utf-8")]), html)
        except FileNotFoundError:
            return Response(500, "Error", Headers(), b"login.html not found")

    try:
        html = CLIENT_HTML.read_bytes()
        return Response(200, "OK", Headers([("Content-Type", "text/html; charset=utf-8")]), html)
    except FileNotFoundError:
        return Response(404, "Not Found", Headers(), b"client.html not found")


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Browser Relay")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    args = parser.parse_args()

    if not _PASS:
        print("警告: 未设置 BROWSER_RELAY_PASS，空密码即可登录 — 只可用于本机调试，绝不要这样暴露到公网")

    relay = BrowserRelay(width=args.width, height=args.height)
    await relay.start_browser()

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    async with serve(
        relay.handle_client, args.host, args.port,
        process_request=process_request,
    ):
        print(f"服务已启动: http://{args.host}:{args.port}")
        await stop_event.wait()

    await relay.stop()


if __name__ == "__main__":
    asyncio.run(main())
