import ctypes
from ctypes import wintypes
from pathlib import PureWindowsPath
import struct
from time import monotonic

from PIL import Image, ImageGrab


class ThumbnailProperties(ctypes.Structure):
    _fields_ = [
        ("flags", wintypes.DWORD),
        ("destination", wintypes.RECT),
        ("source", wintypes.RECT),
        ("opacity", wintypes.BYTE),
        ("visible", wintypes.BOOL),
        ("client_only", wintypes.BOOL),
    ]


class WindowThumbnail:
    def __init__(self, destination):
        self.dwm = ctypes.windll.dwmapi
        self.destination = destination
        self.source = None
        self.handle = wintypes.HANDLE()
        self.last_rect = None

    def show(self, source, area):
        if source != self.source:
            self.close()
            if self.dwm.DwmRegisterThumbnail(wintypes.HWND(self.destination), wintypes.HWND(source),
                                             ctypes.byref(self.handle)) != 0:
                return False
            self.source = source
        if area != self.last_rect:
            properties = ThumbnailProperties(flags=0x01 | 0x08 | 0x10, destination=wintypes.RECT(*area),
                                             visible=True, client_only=True)
            if self.dwm.DwmUpdateThumbnailProperties(self.handle, ctypes.byref(properties)) != 0:
                self.close()
                return False
            self.last_rect = area
        return True

    def close(self):
        if self.handle:
            self.dwm.DwmUnregisterThumbnail(self.handle)
        self.handle = wintypes.HANDLE()
        self.source = None
        self.last_rect = None


def client_size(hwnd):
    user = ctypes.windll.user32
    handle = wintypes.HWND(hwnd)
    rect = wintypes.RECT()
    if not user.IsIconic(handle) and user.GetClientRect(handle, ctypes.byref(rect)):
        return rect.right, rect.bottom
    return 0, 0


def find_game_window(executable="arknights.exe"):
    user = ctypes.windll.user32
    user.GetForegroundWindow.restype = wintypes.HWND
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    )
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    foreground = user.GetForegroundWindow()
    matches = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def inspect(hwnd, _):
        if not user.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(256)
        user.GetWindowTextW(hwnd, title, len(title))
        exact_game_title = title.value.strip() == "明日方舟" and executable == "arknights.exe"
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = kernel.OpenProcess(0x1000, False, pid.value)
        matches_executable = False
        if process:
            path = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(path))
            try:
                if kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(length)):
                    matches_executable = PureWindowsPath(path.value).name.casefold() == executable
            finally:
                kernel.CloseHandle(process)
        if not (matches_executable or exact_game_title):
            return True
        rect = wintypes.RECT()
        if user.GetClientRect(hwnd, ctypes.byref(rect)) and rect.right >= 320 and rect.bottom >= 180:
            matches.append((hwnd == foreground, not user.IsIconic(hwnd), rect.right * rect.bottom, hwnd, title.value))
        return True

    user.EnumWindows(inspect, 0)
    if matches:
        _, _, _, hwnd, title = max(matches)
        return hwnd, title or "明日方舟"
    return None


def foreground_client_box(hwnd):
    user = ctypes.windll.user32
    user.GetForegroundWindow.restype = wintypes.HWND
    if user.GetForegroundWindow() != hwnd or user.IsIconic(hwnd):
        return None
    rect = wintypes.RECT()
    origin = wintypes.POINT()
    if not user.GetClientRect(hwnd, ctypes.byref(rect)) or not user.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    left, top = origin.x, origin.y
    box = (left, top, left + rect.right, top + rect.bottom)
    screen_left, screen_top, width, height = (user.GetSystemMetrics(index) for index in (76, 77, 78, 79))
    if (left < screen_left or top < screen_top or box[2] > screen_left + width
            or box[3] > screen_top + height or rect.right < 1 or rect.bottom < 1):
        return None
    return box


def capture_window_client(hwnd):
    user = ctypes.windll.user32
    handle = wintypes.HWND(hwnd)
    width, height = client_size(hwnd)
    if width < 1 or height < 1 or user.IsHungAppWindow(handle):
        return None

    gdi = ctypes.windll.gdi32
    gdi.CreateCompatibleDC.argtypes = (wintypes.HDC,)
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateDIBSection.argtypes = (wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                     ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD)
    gdi.CreateDIBSection.restype = wintypes.HANDLE
    gdi.SelectObject.argtypes = (wintypes.HDC, wintypes.HANDLE)
    gdi.SelectObject.restype = wintypes.HANDLE
    gdi.DeleteObject.argtypes = (wintypes.HANDLE,)
    gdi.DeleteDC.argtypes = (wintypes.HDC,)
    user.PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
    user.PrintWindow.restype = wintypes.BOOL

    dc = gdi.CreateCompatibleDC(None)
    if not dc:
        return None
    bitmap = previous = None
    try:
        # A negative height puts the first row at the top of the image.
        header = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        pixels = ctypes.c_void_p()
        bitmap = gdi.CreateDIBSection(dc, header, 0, ctypes.byref(pixels), None, 0)
        if not bitmap or not pixels:
            return None
        previous = gdi.SelectObject(dc, bitmap)
        if not previous:
            return None
        # PW_CLIENTONLY | PW_RENDERFULLCONTENT
        if not user.PrintWindow(handle, dc, 0x03):
            return None
        image = Image.frombytes("RGB", (width, height), ctypes.string_at(pixels, width * height * 4),
                                "raw", "BGRX")
        sample = image.resize((64, 36))
        if all(high - low <= 2 for low, high in sample.getextrema()):
            return None
        return image
    finally:
        if previous:
            gdi.SelectObject(dc, previous)
        if bitmap:
            gdi.DeleteObject(bitmap)
        gdi.DeleteDC(dc)


class BackgroundCapture:
    """Latest frame from Windows Graphics Capture for one game window."""

    def __init__(self, hwnd):
        self.hwnd = hwnd
        self.control = None
        self.latest = None

    def __enter__(self):
        try:
            from windows_capture import WindowsCapture

            capture = WindowsCapture(window_hwnd=self.hwnd, cursor_capture=False)

            @capture.event
            def on_frame_arrived(frame, capture_control):
                now = monotonic()
                if self.latest and now - self.latest[0] < 0.10:
                    return
                image = Image.frombytes("RGB", (frame.width, frame.height),
                                        frame.frame_buffer.tobytes(), "raw", "BGRX")
                self.latest = (now, image)

            @capture.event
            def on_closed():
                self.latest = None

            self.control = capture.start_free_threaded()
        except (ImportError, OSError, RuntimeError, TypeError):
            pass
        return self

    def __exit__(self, *_):
        if self.control:
            try:
                self.control.stop()
            except RuntimeError:
                pass

    def grab(self):
        latest = self.latest
        size = client_size(self.hwnd)
        if not latest or not all(size) or monotonic() - latest[0] > 3:
            return None
        image = latest[1]
        if image.size == size:
            return image
        hwnd = wintypes.HWND(self.hwnd)
        origin = wintypes.POINT()
        user = ctypes.windll.user32
        if not user.ClientToScreen(hwnd, ctypes.byref(origin)):
            return None
        dwm = ctypes.windll.dwmapi
        visible_frame = wintypes.RECT()
        outer_frame = wintypes.RECT()
        has_visible_frame = dwm.DwmGetWindowAttribute(
            hwnd, 9, ctypes.byref(visible_frame), ctypes.sizeof(visible_frame)) == 0
        has_outer_frame = user.GetWindowRect(hwnd, ctypes.byref(outer_frame))
        for valid, frame in ((has_visible_frame, visible_frame), (has_outer_frame, outer_frame)):
            if valid and image.size == (frame.right - frame.left, frame.bottom - frame.top):
                x, y = origin.x - frame.left, origin.y - frame.top
                if 0 <= x <= image.width - size[0] and 0 <= y <= image.height - size[1]:
                    return image.crop((x, y, x + size[0], y + size[1]))
        return None


def capture_game(hwnd, background=None):
    box = foreground_client_box(hwnd)
    if box is not None:
        image = ImageGrab.grab(bbox=box, all_screens=True, include_layered_windows=False)
        if ctypes.windll.user32.GetForegroundWindow() == hwnd:
            return image
    if background is not None:
        image = background.grab()
        if image is not None:
            return image
    return capture_window_client(hwnd)
