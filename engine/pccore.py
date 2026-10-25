"""pc-control core — Windows automation primitives.

Zero new dependencies: ctypes (Win32 SendInput/PostMessage/UIA-free control),
pywin32 (win32gui, already present), psutil, Pillow.

Two delivery paths, on purpose:

* FOREGROUND  -> SendInput: real mouse/keyboard, works with every app, moves the
  real cursor and needs the target to have focus.
* BACKGROUND  -> PostMessage / WM_SETTEXT / BM_CLICK straight to an HWND: does not
  move the cursor, does not steal focus, works while the user is typing elsewhere.
  Classic Win32 controls honour it; Chromium/Electron/UWP often ignore it — that
  is a property of those apps, not a bug here, and the caller escalates to
  foreground or to the cua-driver ``computer_use`` tool (UIA) instead.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import difflib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# DLLs and prototypes (restype/argtypes matter: a truncated HWND silently
# breaks every window call on 64-bit)
# --------------------------------------------------------------------------- #

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindow.restype = wt.HWND
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.GetDesktopWindow.restype = wt.HWND
user32.WindowFromPoint.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowEnabled.argtypes = [wt.HWND]
user32.IsIconic.argtypes = [wt.HWND]
user32.IsZoomed.argtypes = [wt.HWND]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetDlgCtrlID.argtypes = [wt.HWND]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = wt.LPARAM
user32.SendMessageTimeoutW.argtypes = [
    wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT, ctypes.POINTER(ULONG_PTR)
]
user32.SendMessageTimeoutW.restype = wt.LPARAM
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.SetFocus.argtypes = [wt.HWND]
user32.SetActiveWindow.argtypes = [wt.HWND]
user32.SetActiveWindow.restype = wt.HWND
user32.AllowSetForegroundWindow.argtypes = [wt.DWORD]
user32.AllowSetForegroundWindow.restype = wt.BOOL
user32.LockSetForegroundWindow.argtypes = [wt.UINT]
user32.LockSetForegroundWindow.restype = wt.BOOL
user32.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT]
user32.SystemParametersInfoW.restype = wt.BOOL
user32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_void_p]
user32.keybd_event.restype = None
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.MoveWindow.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.BOOL]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.MapWindowPoints.argtypes = [wt.HWND, wt.HWND, ctypes.POINTER(wt.POINT), wt.UINT]
user32.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
user32.OpenInputDesktop.restype = wt.HANDLE
user32.CloseDesktop.argtypes = [wt.HANDLE]
user32.LockWorkStation.restype = wt.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
user32.MapVirtualKeyW.restype = wt.UINT
user32.SendInput.argtypes = [wt.UINT, ctypes.c_void_p, ctypes.c_int]
user32.SendInput.restype = wt.UINT
user32.MessageBoxTimeoutW = getattr(user32, "MessageBoxTimeoutW", None)

EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, wt.LPARAM]
user32.EnumChildWindows.argtypes = [wt.HWND, EnumWindowsProc, wt.LPARAM]

MonitorEnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
user32.EnumDisplayMonitors.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), MonitorEnumProc, wt.LPARAM]
user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.c_void_p]

dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]

kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GetCurrentThreadId.restype = wt.DWORD
kernel32.GetConsoleWindow.restype = wt.HWND

# --------------------------------------------------------------------------- #
# DPI awareness: without it every coordinate is scaled and every click misses
# --------------------------------------------------------------------------- #

def _set_dpi_aware() -> None:
    try:
        # PER_MONITOR_AWARE_V2 == -4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def ensure_foreground_lock_disabled() -> bool:
    """Ensure Windows focus-stealing prevention does not force newly opened windows to background."""
    try:
        user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.c_void_p(0), SPIF_UPDATEINIFILE | SPIF_SENDCHANGE)
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "ForegroundLockTimeout", 0, winreg.REG_DWORD, 0)
        return True
    except Exception:
        return False


_set_dpi_aware()
ensure_foreground_lock_disabled()

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SM_CXSCREEN, SM_CYSCREEN = 0, 1

GW_CHILD, GW_HWNDNEXT = 5, 2
GA_ROOT = 2

WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E
BM_CLICK = 0x00F5
SMTO_ABORTIFHUNG, SMTO_BLOCK = 0x0002, 0x0001

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x0001, 0x8000, 0x4000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_EXTENDEDKEY = 0x0002, 0x0004, 0x0001

SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02
ASFW_ANY = 0xFFFFFFFF
LSFW_UNLOCK = 2
VK_MENU = 0x12

SW_RESTORE, SW_MINIMIZE, SW_MAXIMIZE, SW_SHOW = 9, 6, 3, 5
HWND_TOP, HWND_TOPMOST, HWND_NOTOPMOST = 0, -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0004, 0x0040

DESKTOP_READOBJECTS, DESKTOP_SWITCHDESKTOP = 0x0001, 0x0100


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
        ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


VK = {
    "backspace": 0x08, "back": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "pause": 0x13,
    "capslock": 0x14, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "pageup": 0x21,
    "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "printscreen": 0x2C,
    "prtsc": 0x2C, "insert": 0x2D, "ins": 0x2D, "delete": 0x2E, "del": 0x2E,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D, "menu": 0x5D,
    "numlock": 0x90, "scrolllock": 0x91, "semicolon": 0xBA, "equals": 0xBB,
    "comma": 0xBC, "minus": 0xBD, "period": 0xBE, "slash": 0xBF, "grave": 0xC0,
    "lbracket": 0xDB, "backslash": 0xDC, "rbracket": 0xDD, "apostrophe": 0xDE,
    "lshift": 0xA0, "rshift": 0xA1, "lctrl": 0xA2, "rctrl": 0xA3, "lalt": 0xA4, "ralt": 0xA5,
}
for _i in range(1, 25):
    VK[f"f{_i}"] = 0x6F + _i


def vk_of(name: str) -> int:
    key = (name or "").strip().lower()
    if not key:
        raise ValueError("empty key name")
    if key in VK:
        return VK[key]
    if len(key) == 1:
        ch = key.upper()
        if ch.isalnum():
            return ord(ch)
        if key in VK:
            return VK[key]
    if key.startswith("vk") and key[2:].isdigit():
        return int(key[2:])
    raise ValueError(f"unknown key {name!r}")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def window_text(hwnd: int, timeout_ms: int = 120) -> str:
    """Window text, cross-process safe (WM_GETTEXT with a timeout)."""
    length = user32.SendMessageTimeoutW(hwnd, WM_GETTEXTLENGTH, 0, 0, SMTO_ABORTIFHUNG | SMTO_BLOCK,
                                        timeout_ms, None) or 0
    length = min(int(length), 4096)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 2)
    res = ULONG_PTR()
    user32.SendMessageTimeoutW(hwnd, WM_GETTEXT, length + 1, ctypes.cast(buf, ctypes.c_void_p).value,
                               SMTO_ABORTIFHUNG | SMTO_BLOCK, timeout_ms, ctypes.byref(res))
    text = buf.value
    if not text:
        n = user32.GetWindowTextLengthW(hwnd)
        if n > 0:
            b2 = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, b2, n + 1)
            text = b2.value
    return text


def class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def rect_of(hwnd: int) -> tuple[int, int, int, int]:
    r = wt.RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return r.left, r.top, r.right, r.bottom
    return 0, 0, 0, 0


def client_rect_of(hwnd: int) -> tuple[int, int, int, int]:
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def frame_bounds(hwnd: int) -> tuple[int, int, int, int]:
    """DWM extended frame bounds — the real on-screen box (no invisible borders)."""
    r = wt.RECT()
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    try:
        hr = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                                          ctypes.byref(r), ctypes.sizeof(r))
        if hr == 0 and (r.right - r.left) > 0:
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return rect_of(hwnd)


def is_cloaked(hwnd: int) -> bool:
    DWMWA_CLOAKED = 14
    val = wt.DWORD(0)
    try:
        dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
        return bool(val.value)
    except Exception:
        return False


def window_pid(hwnd: int) -> int:
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


_PROC_NAME_CACHE: dict[int, str] = {}


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]


def _process_name_ctypes(pid: int) -> str:
    """Executable name without psutil — QueryFullProcessImageName."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(handle)
    return ""


def process_name(pid: int) -> str:
    if not pid:
        return ""
    if pid in _PROC_NAME_CACHE:
        return _PROC_NAME_CACHE[pid]
    name = ""
    try:
        import psutil
        p = psutil.Process(pid)
        name = p.name()
    except Exception:
        name = _process_name_ctypes(pid)
    if len(_PROC_NAME_CACHE) > 4096:
        _PROC_NAME_CACHE.clear()
    _PROC_NAME_CACHE[pid] = name
    return name


def resolve_hwnd(hwnd: int | None) -> int:
    h = int(hwnd or 0)
    if not h:
        h = int(user32.GetForegroundWindow() or 0)
    if not h or not user32.IsWindow(h):
        raise ValueError(f"invalid window handle: {hwnd}")
    return h


# --------------------------------------------------------------------------- #
# Session / display state
# --------------------------------------------------------------------------- #

def virtual_screen() -> tuple[int, int, int, int]:
    return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
            user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))


def input_desktop_available() -> bool:
    """False when the secure desktop owns input (locked screen / logon / UAC)."""
    h = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS | DESKTOP_SWITCHDESKTOP)
    if h:
        user32.CloseDesktop(h)
        return True
    return False


def monitors() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def cb(hmon, hdc, lprc, data):
        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                        ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            m = mi.rcMonitor
            wk = mi.rcWork
            out.append({
                "index": len(out),
                "primary": bool(mi.dwFlags & 1),
                "rect": [m.left, m.top, m.right, m.bottom],
                "work_area": [wk.left, wk.top, wk.right, wk.bottom],
                "size": [m.right - m.left, m.bottom - m.top],
            })
        return True

    user32.EnumDisplayMonitors(None, None, MonitorEnumProc(cb), 0)
    return out


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def session_state() -> dict[str, Any]:
    interactive = input_desktop_available()
    fg = int(user32.GetForegroundWindow() or 0)
    vx, vy, vw, vh = virtual_screen()
    return {
        "interactive_desktop": interactive,
        "locked": (not interactive) or fg == 0,
        "foreground_window": fg,
        "foreground_title": (window_text(fg) if fg else ""),
        "cursor": list(cursor_pos()),
        "virtual_screen": [vx, vy, vw, vh],
        "monitors": monitors(),
        "elevated": is_admin(),
        "session_id": _session_id(),
        "console_session_id": int(kernel32.WTSGetActiveConsoleSessionId()) if hasattr(kernel32, "WTSGetActiveConsoleSessionId") else None,
        "python": sys.executable,
        "pid": os.getpid(),
    }


def _session_id() -> int | None:
    try:
        sid = wt.DWORD(0)
        kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(sid))
        return int(sid.value)
    except Exception:
        return None


def cursor_pos() -> tuple[int, int]:
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


# --------------------------------------------------------------------------- #
# Window inventory
# --------------------------------------------------------------------------- #

def list_windows(include_hidden: bool = False, only_pid: int | None = None,
                 title_filter: str | None = None, limit: int = 400) -> list[dict[str, Any]]:
    fg = int(user32.GetForegroundWindow() or 0)
    out: list[dict[str, Any]] = []
    needle = (title_filter or "").lower()

    def cb(hwnd, lparam):
        if len(out) >= limit:
            return False
        pid = window_pid(hwnd)
        if only_pid and pid != int(only_pid):
            return True
        visible = bool(user32.IsWindowVisible(hwnd))
        if not visible and not include_hidden:
            return True
        title = window_text(hwnd)
        if not title and not include_hidden:
            return True
        if is_cloaked(hwnd):
            return True
        if needle and needle not in title.lower():
            return True
        l, t, r, b = frame_bounds(hwnd)
        out.append({
            "hwnd": int(hwnd),
            "title": title,
            "class": class_name(hwnd),
            "pid": pid,
            "process": process_name(pid),
            "rect": [l, t, r, b],
            "visible": visible,
            "foreground": int(hwnd) == fg,
            "minimized": bool(user32.IsIconic(hwnd)),
            "maximized": bool(user32.IsZoomed(hwnd)),
            "enabled": bool(user32.IsWindowEnabled(hwnd)),
        })
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    out.sort(key=lambda w: (not w["foreground"], w["process"], w["title"]))
    return out


SKIP_CLASSES = {
    "tooltips_class32", "IME", "MSCTFIME UI", "GDI+ Hook Window Class",
    "SysShadow", "SystemResourceNotifyWindow", "TaskListThumbnailWnd",
}


def find_window(title: str | None = None, process: str | None = None,
                class_: str | None = None, hwnd: int | None = None) -> dict[str, Any]:
    """Best matching top-level window: prefers visible + titled + largest.

    A process owns several top-level windows (tooltips, IME, shadow helpers); the
    naive "first match" lands on an invisible helper and every later action fails.
    """
    if hwnd:
        h = resolve_hwnd(hwnd)
        l, t, r, b = frame_bounds(h)
        return {"hwnd": h, "title": window_text(h), "class": class_name(h),
                "pid": window_pid(h), "process": process_name(window_pid(h)), "rect": [l, t, r, b]}
    wn = (title or "").lower()
    pn = (process or "").lower()
    cn = (class_ or "").lower()
    best: tuple[float, dict[str, Any]] | None = None
    for w in list_windows(include_hidden=True):
        if (w.get("class") or "") in SKIP_CLASSES:
            continue
        if wn and wn not in (w["title"] or "").lower():
            continue
        if pn and pn not in (w["process"] or "").lower():
            continue
        if cn and cn not in (w["class"] or "").lower():
            continue
        l, t, r, b = w["rect"]
        area = max((r - l) * (b - t), 0) / 1_000_000.0
        score = (100.0 if w["title"] else 0.0) + (50.0 if w.get("visible") else 0.0) \
            + (10.0 if w.get("enabled") else 0.0) + min(area, 9.0)
        if best is None or score > best[0]:
            best = (score, w)
    if best is None:
        raise LookupError(f"no window matched title={title!r} process={process!r} class={class_!r}")
    return best[1]


def window_action(hwnd: int, action: str, x: int | None = None, y: int | None = None,
                  width: int | None = None, height: int | None = None) -> dict[str, Any]:
    h = resolve_hwnd(hwnd)
    a = (action or "").lower()
    if a in ("focus", "activate", "front"):
        _force_foreground(h)
    elif a in ("minimize", "min"):
        user32.ShowWindow(h, SW_MINIMIZE)
    elif a in ("maximize", "max"):
        user32.ShowWindow(h, SW_MAXIMIZE)
        _force_foreground(h)
    elif a in ("restore", "show"):
        user32.ShowWindow(h, SW_RESTORE)
        _force_foreground(h)
    elif a in ("close",):
        user32.PostMessageW(h, WM_CLOSE, 0, 0)
    elif a in ("topmost",):
        user32.SetWindowPos(h, wt.HWND(HWND_TOPMOST), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
    elif a in ("untopmost",):
        user32.SetWindowPos(h, wt.HWND(HWND_NOTOPMOST), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
    elif a in ("move", "resize", "bounds"):
        l, t, r, b = rect_of(h)
        nx = l if x is None else int(x)
        ny = t if y is None else int(y)
        nw = (r - l) if width is None else int(width)
        nh = (b - t) if height is None else int(height)
        user32.MoveWindow(h, nx, ny, nw, nh, True)
    else:
        raise ValueError(f"unknown window action {action!r}")
    l, t, r, b = frame_bounds(h)
    return {"hwnd": h, "action": a, "title": window_text(h),
            "rect": [l, t, r, b], "foreground": int(user32.GetForegroundWindow() or 0) == h}


def _force_foreground(hwnd: int) -> None:
    """Bring a window to the front, defeating the foreground-lock heuristic."""
    if not hwnd or not user32.IsWindow(hwnd):
        return
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    fg = int(user32.GetForegroundWindow() or 0)
    if fg == hwnd:
        return

    try:
        user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.c_void_p(0), SPIF_SENDCHANGE)
    except Exception:
        pass
    try:
        user32.AllowSetForegroundWindow(ASFW_ANY)
    except Exception:
        pass
    try:
        user32.LockSetForegroundWindow(LSFW_UNLOCK)
    except Exception:
        pass

    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)
    attached = []
    try:
        for tid in {fg_tid, tgt_tid}:
            if tid and tid != cur_tid and user32.AttachThreadInput(cur_tid, tid, True):
                attached.append(tid)

        try:
            user32.keybd_event(VK_MENU, 0, 0, None)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, None)
        except Exception:
            pass

        user32.SetWindowPos(hwnd, wt.HWND(HWND_TOP), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        for tid in attached:
            user32.AttachThreadInput(cur_tid, tid, False)


# --------------------------------------------------------------------------- #
# Control tree (Win32 child windows) — background targeting without UIA/COM
# --------------------------------------------------------------------------- #

def elements(hwnd: int, depth: int = 4, max_items: int = 300,
             include_empty: bool = False) -> list[dict[str, Any]]:
    root = resolve_hwnd(hwnd)
    out: list[dict[str, Any]] = []

    def walk(parent: int, level: int) -> None:
        if level > depth or len(out) >= max_items:
            return
        child = user32.GetWindow(root if level == 0 else parent, GW_CHILD) if level == 0 else user32.GetWindow(parent, GW_CHILD)
        while child:
            if len(out) >= max_items:
                return
            h = int(child)
            text = window_text(h)
            cls = class_name(h)
            if text or include_empty:
                l, t, r, b = rect_of(h)
                out.append({
                    "hwnd": h,
                    "class": cls,
                    "text": text[:400],
                    "id": int(user32.GetDlgCtrlID(h)),
                    "rect": [l, t, r, b],
                    "depth": level,
                    "visible": bool(user32.IsWindowVisible(h)),
                    "enabled": bool(user32.IsWindowEnabled(h)),
                    "clickable": cls in ("Button", "SysButton", "ToolbarWindow32", "SysTabControl32",
                                         "ListBox", "ComboBox", "MenuItem"),
                    "editable": is_editable_class(cls),
                })
            walk(h, level + 1)
            child = user32.GetWindow(h, GW_HWNDNEXT)

    walk(root, 0)
    return out


def find_element(hwnd: int, text: str | None = None, class_: str | None = None,
                 control_id: int | None = None, depth: int = 6) -> dict[str, Any]:
    tn = (text or "").lower()
    cn = (class_ or "").lower()
    for el in elements(hwnd, depth=depth, include_empty=True):
        if control_id is not None and el["id"] != int(control_id):
            continue
        if tn and tn not in (el["text"] or "").lower():
            continue
        if cn and cn != (el["class"] or "").lower():
            continue
        return el
    raise LookupError(f"no control matched text={text!r} class={class_!r} id={control_id}")


# --------------------------------------------------------------------------- #
# FOREGROUND input (SendInput) — real events
# --------------------------------------------------------------------------- #

def _send_inputs(inputs: list[INPUT]) -> int:
    n = len(inputs)
    if not n:
        return 0
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, ctypes.cast(arr, ctypes.c_void_p), ctypes.sizeof(INPUT))
    if sent != n:
        err = ctypes.get_last_error()
        raise OSError(f"SendInput inserted {sent}/{n} events (WinError {err})")
    return sent


def _abs(x: int, y: int) -> tuple[int, int]:
    vx, vy, vw, vh = virtual_screen()
    ax = int(round((int(x) - vx) * 65535.0 / max(vw - 1, 1)))
    ay = int(round((int(y) - vy) * 65535.0 / max(vh - 1, 1)))
    return max(0, min(65535, ax)), max(0, min(65535, ay))


def _mouse_event(flags: int, x: int | None = None, y: int | None = None, data: int = 0) -> INPUT:
    mi = MOUSEINPUT()
    if x is not None and y is not None:
        mi.dx, mi.dy = _abs(x, y)
        flags |= MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    mi.dwFlags = flags
    mi.mouseData = data & 0xFFFFFFFF
    return INPUT(type=INPUT_MOUSE, mi=mi)


def _key_event(vk: int, up: bool = False, unicode_scan: int | None = None) -> INPUT:
    ki = KEYBDINPUT()
    if unicode_scan is not None:
        ki.wVk = 0
        ki.wScan = unicode_scan
        ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    else:
        ki.wVk = vk
        ki.wScan = user32.MapVirtualKeyW(vk, 0)
        ki.dwFlags = (KEYEVENTF_KEYUP if up else 0)
        if vk in (0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x90):
            ki.dwFlags |= KEYEVENTF_EXTENDEDKEY
    return INPUT(type=INPUT_KEYBOARD, ki=ki)


_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def fg_move(x: int, y: int) -> None:
    _send_inputs([_mouse_event(MOUSEEVENTF_MOVE, x, y)])


def fg_click(x: int | None = None, y: int | None = None, button: str = "left",
             double: bool = False, hold_ms: int = 0) -> None:
    down, up = _BUTTON_FLAGS.get((button or "left").lower(), _BUTTON_FLAGS["left"])
    seq: list[INPUT] = []
    if x is not None and y is not None:
        seq.append(_mouse_event(MOUSEEVENTF_MOVE, x, y))
    seq.append(_mouse_event(down))
    if hold_ms > 0:
        _send_inputs(seq)
        time.sleep(hold_ms / 1000.0)
        _send_inputs([_mouse_event(up)])
        if double:
            time.sleep(0.05)
            _send_inputs([_mouse_event(down)])
            time.sleep(0.05)
            _send_inputs([_mouse_event(up)])
        return
    seq.append(_mouse_event(up))
    if double:
        seq.append(_mouse_event(down))
        seq.append(_mouse_event(up))
    _send_inputs(seq)


def fg_drag(x1: int, y1: int, x2: int, y2: int, button: str = "left",
            steps: int = 12, duration_ms: int = 260) -> None:
    down, up = _BUTTON_FLAGS.get((button or "left").lower(), _BUTTON_FLAGS["left"])
    _send_inputs([_mouse_event(MOUSEEVENTF_MOVE, x1, y1)])
    time.sleep(0.03)
    _send_inputs([_mouse_event(down)])
    step_delay = max(duration_ms, 40) / 1000.0 / max(steps, 1)
    for i in range(1, steps + 1):
        nx = int(round(x1 + (x2 - x1) * i / steps))
        ny = int(round(y1 + (y2 - y1) * i / steps))
        _send_inputs([_mouse_event(MOUSEEVENTF_MOVE, nx, ny)])
        time.sleep(step_delay)
    time.sleep(0.04)
    _send_inputs([_mouse_event(up)])


def fg_scroll(amount: int, horizontal: bool = False) -> None:
    delta = int(amount) * 120
    flags = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    _send_inputs([_mouse_event(flags, data=delta)])


def fg_type(text: str) -> int:
    if not text:
        return 0
    seq: list[INPUT] = []
    data = text.encode("utf-16-le")
    for i in range(0, len(data), 2):
        unit = data[i] | (data[i + 1] << 8)
        seq.append(_key_event(0, False, unicode_scan=unit))
        seq.append(_key_event(0, True, unicode_scan=unit))
        if len(seq) >= 400:
            _send_inputs(seq)
            seq = []
            time.sleep(0.01)
    if seq:
        _send_inputs(seq)
    return len(text)


def fg_key(combo: str, hold_ms: int = 0) -> list[str]:
    parts = [p for p in str(combo or "").replace(" ", "").split("+") if p]
    if not parts:
        raise ValueError("empty key combo")
    vks = [vk_of(p) for p in parts]
    mods, main = vks[:-1], vks[-1]
    for vk in mods:
        _send_inputs([_key_event(vk, False)])
        time.sleep(0.012)
    time.sleep(0.012)
    _send_inputs([_key_event(main, False)])
    if hold_ms:
        time.sleep(hold_ms / 1000.0)
    time.sleep(0.012)
    _send_inputs([_key_event(main, True)])
    for vk in reversed(mods):
        _send_inputs([_key_event(vk, True)])
        time.sleep(0.012)
    return parts


# --------------------------------------------------------------------------- #
# BACKGROUND input (PostMessage) — no focus, no cursor
# --------------------------------------------------------------------------- #

def _pt_lparam(x: int, y: int) -> int:
    return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)


def _coords_for(hwnd: int, x: int, y: int, coords: str) -> tuple[int, int]:
    if (coords or "screen").lower() in ("screen", "global"):
        p = wt.POINT(int(x), int(y))
        user32.ScreenToClient(hwnd, ctypes.byref(p))
        return p.x, p.y
    return int(x), int(y)


def bg_click(hwnd: int, x: int, y: int, button: str = "left", double: bool = False,
             coords: str = "screen") -> dict[str, Any]:
    h = resolve_hwnd(hwnd)
    cx, cy = _coords_for(h, x, y, coords)
    lp = _pt_lparam(cx, cy)
    b = (button or "left").lower()
    d, u = _BUTTON_FLAGS.get(b, _BUTTON_FLAGS["left"])
    wparam = {MOUSEEVENTF_LEFTDOWN: 0x0001, MOUSEEVENTF_RIGHTDOWN: 0x0002,
              MOUSEEVENTF_MIDDLEDOWN: 0x0010}.get(d, 0)
    user32.PostMessageW(h, WM_MOUSEMOVE, 0, lp)
    ok = bool(user32.PostMessageW(h, d, wparam, lp))
    time.sleep(0.02)
    ok = bool(user32.PostMessageW(h, u, 0, lp)) and ok
    if double:
        time.sleep(0.04)
        user32.PostMessageW(h, WM_LBUTTONDBLCLK, wparam, lp)
        time.sleep(0.02)
        ok = bool(user32.PostMessageW(h, u, 0, lp)) and ok
    return {"hwnd": h, "client": [cx, cy], "delivered": ok, "mode": "background"}


def bg_key(hwnd: int, combo: str) -> dict[str, Any]:
    h = resolve_hwnd(hwnd)
    parts = [p for p in str(combo or "").replace(" ", "").split("+") if p]
    if not parts:
        raise ValueError("empty key combo")
    vks = [vk_of(p) for p in parts]
    mods, main = vks[:-1], vks[-1]
    for vk in mods:
        _post_key(h, vk, up=False)
    _post_key(h, main, up=False)
    time.sleep(0.02)
    _post_key(h, main, up=True)
    for vk in reversed(mods):
        _post_key(h, vk, up=True)
    return {"hwnd": h, "keys": parts, "delivered": True, "mode": "background",
            "class": class_name(h), "hint": BG_HINT}


def _post_key(h: int, vk: int, up: bool) -> bool:
    scan = user32.MapVirtualKeyW(vk, 0) & 0xFF
    lp = 1 | (scan << 16) | (0xC0000000 if up else 0)
    return bool(user32.PostMessageW(h, WM_KEYUP if up else WM_KEYDOWN, vk, wt.LPARAM(lp)))


BG_HINT = ("entregue ao HWND em segundo plano; Win32 clássico honra (confirme com pc_screenshot), "
           "apps XAML/Chromium/Electron/UWP costumam ignorar — escale para mode=foreground "
           "ou use computer_use (UIA)")

EDITABLE_CLASSES = ("Edit", "RichEdit", "RichEdit20W", "RichEdit20A", "RichEdit50W",
                    "RICHEDIT60W", "Scintilla", "TEdit", "TMemo")


def is_editable_class(cls: str) -> bool:
    c = cls or ""
    return c in EDITABLE_CLASSES or c.startswith("RichEdit") or c.startswith("WindowsForms10.EDIT")


def bg_type(hwnd: int, text: str) -> dict[str, Any]:
    h = resolve_hwnd(hwnd)
    cls = class_name(h)
    sent = 0
    for ch in text or "":
        if ch == "\n":
            _post_key(h, 0x0D, False)
            _post_key(h, 0x0D, True)
            sent += 1
            continue
        code = ord(ch)
        if code > 0xFFFF:  # astral plane: send the surrogate pair as two WM_CHARs
            v = code - 0x10000
            for unit in (0xD800 + (v >> 10), 0xDC00 + (v & 0x3FF)):
                if user32.PostMessageW(h, WM_CHAR, unit, wt.LPARAM(1 | (unit << 16))):
                    sent += 1
            continue
        if user32.PostMessageW(h, WM_CHAR, code, wt.LPARAM(1 | (code << 16))):
            sent += 1
    out = {"hwnd": h, "class": cls, "chars_sent": sent, "requested": len(text or ""),
           "delivered": sent > 0, "mode": "background", "hint": BG_HINT}
    if not is_editable_class(cls):
        out["warning"] = (f"classe {cls!r} não é um controle de edição: WM_CHAR no HWND da janela "
                          "normalmente não insere texto. Use pc_elements para achar o Edit/Custom "
                          "ou mande mode=foreground.")
    return out


def bg_settext(hwnd: int, text: str) -> dict[str, Any]:
    """WM_SETTEXT on a control: the most reliable background "typing" on a known Edit."""
    h = resolve_hwnd(hwnd)
    cls = class_name(h)
    res = user32.SendMessageW(h, WM_SETTEXT, 0, ctypes.cast(ctypes.c_wchar_p(text or ""), ctypes.c_void_p).value)
    out = {"hwnd": h, "class": cls, "ok": bool(res), "readback": window_text(h),
           "mode": "background"}
    if not is_editable_class(cls):
        out["warning"] = (f"classe {cls!r} não é Edit: WM_SETTEXT pode alterar o título da janela "
                          "em vez de um campo de texto. Confirme o readback.")
    return out


def bg_press_button(hwnd: int) -> dict[str, Any]:
    """BM_CLICK on a real button — background, no focus."""
    h = resolve_hwnd(hwnd)
    ok = bool(user32.PostMessageW(h, BM_CLICK, 0, 0))
    return {"hwnd": h, "class": class_name(h), "text": window_text(h), "delivered": ok,
            "mode": "background"}


def bg_wheel(hwnd: int, amount: int, x: int | None = None, y: int | None = None,
             coords: str = "screen") -> dict[str, Any]:
    h = resolve_hwnd(hwnd)
    cx, cy = _coords_for(h, int(x or 0), int(y or 0), coords)
    delta = (int(amount) * 120) & 0xFFFF
    user32.PostMessageW(h, WM_MOUSEWHEEL, (delta << 16), _pt_lparam(cx, cy))
    return {"hwnd": h, "amount": amount, "delivered": True, "mode": "background"}


# --------------------------------------------------------------------------- #
# Screenshot
# --------------------------------------------------------------------------- #

def shots_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    d = base / "hermes" / "plugin-data" / "pc-control" / "shots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _shot_path(name: str | None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    fname = name or f"shot-{stamp}-{int((time.time() % 1) * 1000):03d}.png"
    if not fname.lower().endswith(".png"):
        fname += ".png"
    return shots_dir() / fname


def capture_screen(path: str | None = None, monitor: int | None = None,
                   bbox: list[int] | None = None) -> dict[str, Any]:
    box = None
    if monitor is not None:
        ms = monitors()
        if 0 <= int(monitor) < len(ms):
            box = tuple(ms[int(monitor)]["rect"])
        else:
            raise ValueError(f"monitor {monitor} out of range (0..{len(ms) - 1})")
    if bbox:
        box = tuple(int(v) for v in bbox[:4])
    out = _shot_path(path)
    pil_err = ""
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=box, all_screens=True)
        img.save(out)
        return {"path": str(out), "size": list(img.size), "mode": "screen"}
    except Exception as exc:
        pil_err = f"{type(exc).__name__}: {exc}"

    x, y, w, h = (box[0], box[1], box[2] - box[0], box[3] - box[1]) if box else (0, 0, 0, 0)
    script = "\n".join([
        "$ErrorActionPreference='Stop'",
        "Add-Type -AssemblyName System.Drawing",
        "Add-Type -AssemblyName System.Windows.Forms",
        f"$X={x}; $Y={y}; $W={w}; $H={h}",
        "if ($W -le 0 -or $H -le 0) { $vs=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
        " $X=$vs.X; $Y=$vs.Y; $W=$vs.Width; $H=$vs.Height }",
        "$bmp = New-Object System.Drawing.Bitmap $W, $H",
        "$g = [System.Drawing.Graphics]::FromImage($bmp)",
        "$g.CopyFromScreen($X, $Y, 0, 0, (New-Object System.Drawing.Size $W, $H))",
        f"$bmp.Save('{_ps_quote(str(out))}', [System.Drawing.Imaging.ImageFormat]::Png)",
        "$g.Dispose(); $bmp.Dispose()",
        "'{\"ok\":true}'",
    ])
    _run_ps_inline(script, timeout=60)
    if out.exists() and out.stat().st_size > 0:
        return {"path": str(out), "mode": "powershell", "reason": pil_err}
    return {"ok": False, "error": f"captura falhou (Pillow: {pil_err})", "path": str(out)}


def _ps_quote(value: str) -> str:
    return str(value).replace("'", "''")


def capture_window(hwnd: int, path: str | None = None) -> dict[str, Any]:
    """PrintWindow(PW_RENDERFULLCONTENT) — captures a window even when occluded."""
    h = resolve_hwnd(hwnd)
    l, t, r, b = frame_bounds(h)
    w_, h_ = max(r - l, 1), max(b - t, 1)
    out = _shot_path(path)
    try:
        import win32gui
        import win32ui
        from PIL import Image

        hwnd_dc = win32gui.GetWindowDC(h)
        try:
            mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(mfc_dc, w_, h_)
            save_dc.SelectObject(bmp)
            ok = user32.PrintWindow(h, save_dc.GetSafeHdc(), 2)
            info = bmp.GetInfo()
            raw = bmp.GetBitmapBits(True)
            img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), raw, "raw", "BGRX", 0, 1)
            img.save(out)
            win32gui.DeleteObject(bmp.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            if ok:
                return {"path": str(out), "size": list(img.size),
                        "rect": [l, t, r, b], "mode": "printwindow", "hwnd": h}
        finally:
            win32gui.ReleaseDC(h, hwnd_dc)
    except Exception as exc:  # pywin32/Pillow absent or PrintWindow refused
        last_error = f"{type(exc).__name__}: {exc}"
    else:
        last_error = "PrintWindow returned 0"

    ps = _printwindow_ps(h, out)
    if ps.get("ok") and out.exists():
        return {"path": str(out), "rect": [l, t, r, b], "mode": "printwindow-powershell",
                "reason": last_error, "hwnd": h}

    crop = capture_screen(path=str(out), bbox=[l, t, r, b])
    if crop.get("ok", True) and out.exists():
        return {"path": str(out), "rect": [l, t, r, b], "mode": "crop-fallback",
                "reason": last_error, "hwnd": h}
    return {"ok": False, "error": f"captura da janela falhou: {last_error}", "hwnd": h}


_PRINTWINDOW_CS = r"""
using System;
using System.Runtime.InteropServices;
using System.Drawing;
using System.Drawing.Imaging;
public class PcPrint {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
  public static bool Grab(long handle, string path) {
    RECT r; GetWindowRect(new IntPtr(handle), out r);
    int w = Math.Max(r.Right - r.Left, 1), h = Math.Max(r.Bottom - r.Top, 1);
    using (Bitmap bmp = new Bitmap(w, h, PixelFormat.Format32bppArgb)) {
      bool ok;
      using (Graphics g = Graphics.FromImage(bmp)) {
        IntPtr hdc = g.GetHdc();
        ok = PrintWindow(new IntPtr(handle), hdc, 2);
        g.ReleaseHdc(hdc);
      }
      if (!ok) return false;
      bmp.Save(path, ImageFormat.Png);
    }
    return true;
  }
}
"""


def _printwindow_ps(hwnd: int, out: Path) -> dict[str, Any]:
    """PrintWindow through .NET — no pywin32 needed (MCP/plain-python installs)."""
    script = "\n".join([
        "$ErrorActionPreference='Stop'",
        "Add-Type -AssemblyName System.Drawing",
        "$cs = @'",
        _PRINTWINDOW_CS.strip(),
        "'@",
        "Add-Type -TypeDefinition $cs -ReferencedAssemblies System.Drawing",
        f"if ([PcPrint]::Grab({int(hwnd)}, '{_ps_quote(str(out))}')) {{ '{{\"ok\":true}}' }}"
        " else { '{\"ok\":false}' }",
    ])
    raw = _run_ps_inline(script, timeout=60)
    return {"ok": '"ok":true' in raw.replace(" ", ""), "raw": raw.strip()[-200:]}


# --------------------------------------------------------------------------- #
# Clipboard
# --------------------------------------------------------------------------- #

def clipboard_get() -> dict[str, Any]:
    try:
        import win32clipboard as cb
    except Exception:
        return _clipboard_get_ps()
    for _ in range(8):
        try:
            cb.OpenClipboard()
            break
        except Exception:
            time.sleep(0.05)
    else:
        return _clipboard_get_ps()
    try:
        if cb.IsClipboardFormatAvailable(cb.CF_UNICODETEXT):
            return {"ok": True, "text": cb.GetClipboardData(cb.CF_UNICODETEXT)}
        if cb.IsClipboardFormatAvailable(cb.CF_TEXT):
            return {"ok": True, "text": cb.GetClipboardData(cb.CF_TEXT)}
        if cb.IsClipboardFormatAvailable(cb.CF_HDROP):
            return {"ok": True, "files": list(cb.GetClipboardData(cb.CF_HDROP))}
        return {"ok": True, "text": "", "note": "no text on clipboard"}
    finally:
        cb.CloseClipboard()


def clipboard_set(text: str) -> dict[str, Any]:
    try:
        import win32clipboard as cb
    except Exception:
        return _clipboard_set_ps(text)
    for _ in range(8):
        try:
            cb.OpenClipboard()
            break
        except Exception:
            time.sleep(0.05)
    else:
        return _clipboard_set_ps(text)
    try:
        cb.EmptyClipboard()
        cb.SetClipboardData(cb.CF_UNICODETEXT, str(text or ""))
        return {"ok": True, "chars": len(text or "")}
    finally:
        cb.CloseClipboard()


# --- clipboard without pywin32 (plain Python install, MCP server) -------------

def _clipboard_get_ps() -> dict[str, Any]:
    out = _run_ps_inline(
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8\n"
        "$t = Get-Clipboard -Raw\n"
        "ConvertTo-Json -Compress -Depth 3 @{ ok=$true; text=$t }\n"
    )
    return _json_or_error(out, "clipboard")


def _clipboard_set_ps(text: str) -> dict[str, Any]:
    import base64 as _b64
    payload = _b64.b64encode(str(text or "").encode("utf-8")).decode()
    script = (
        "$t = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s'))\n"
        "Set-Clipboard -Value $t\n"
        "ConvertTo-Json -Compress @{ ok=$true; chars=$t.Length }\n" % payload
    )
    return _json_or_error(_run_ps_inline(script), "clipboard")


def _json_or_error(raw: str, what: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                continue
    return {"ok": False, "error": f"{what}: resposta inesperada {raw[:300]!r}"}


# --------------------------------------------------------------------------- #
# Processes
# --------------------------------------------------------------------------- #

def process_list(name: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
    import psutil
    needle = (name or "").lower()
    out = []
    for p in psutil.process_iter(["pid", "name", "username", "cpu_percent", "memory_info", "status"]):
        try:
            info = p.info
            if needle and needle not in (info.get("name") or "").lower():
                continue
            mem = info.get("memory_info")
            out.append({
                "pid": info["pid"],
                "name": info.get("name"),
                "status": info.get("status"),
                "user": info.get("username"),
                "rss_mb": round((mem.rss / 1048576.0), 1) if mem else None,
            })
        except Exception:
            continue
        if len(out) >= limit:
            break
    out.sort(key=lambda x: (x["name"] or "", x["pid"]))
    return out


def process_start(command: str, cwd: str | None = None, detached: bool = True,
                  shell: bool = True) -> dict[str, Any]:
    flags = 0
    if detached:
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(command, cwd=cwd, shell=shell, creationflags=flags,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, close_fds=True)
    time.sleep(0.4)
    return {"pid": proc.pid, "command": command, "alive": proc.poll() is None,
            "exit_code": proc.poll()}


def process_stop(pid: int | str | None = None, name: str | None = None,
                 tree: bool = True, force: bool = False) -> dict[str, Any]:
    import psutil
    targets = []
    if pid:
        targets = [psutil.Process(int(pid))]
    elif name:
        targets = [p for p in psutil.process_iter(["name"]) if (p.info.get("name") or "").lower() == name.lower()]
    if not targets:
        raise LookupError("no process matched")
    killed = []
    for proc in targets:
        try:
            victims = ([proc] + proc.children(recursive=True)) if tree else [proc]
            for v in victims:
                try:
                    v.kill() if force else v.terminate()
                except Exception:
                    pass
            gone, alive = psutil.wait_procs(victims, timeout=4 if force else 2)
            killed.append({"pid": proc.pid, "name": proc.name(),
                           "terminated": len(gone), "still_alive": [a.pid for a in alive]})
        except Exception as exc:
            killed.append({"pid": getattr(proc, "pid", None), "error": str(exc)})
    return {"ok": True, "results": killed}


# --------------------------------------------------------------------------- #
# Services (sc.exe)
# --------------------------------------------------------------------------- #

def _run(cmd: list[str], timeout: int = 25) -> tuple[int, str]:
    """Run a console tool decoding its output safely.

    sc.exe/net.exe write OEM-codepage bytes (0x80 shows up in pt-BR output), so
    ``text=True`` blows up the reader thread with UnicodeDecodeError.
    """
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=0x08000000)  # CREATE_NO_WINDOW
    except Exception as exc:
        return 1, str(exc)
    raw = (p.stdout or b"") + (p.stderr or b"")
    bad = chr(0xFFFD)
    out = raw.decode("utf-8", errors="replace")
    if bad in out:
        for enc in ("cp850", "cp1252", "latin-1"):
            try:
                candidate = raw.decode(enc)
            except Exception:
                continue
            if bad not in candidate:
                out = candidate
                break
    return p.returncode, out


def service(action: str, name: str) -> dict[str, Any]:
    a = (action or "status").lower()
    STATE_TOKENS = ("RUNNING", "STOPPED", "PAUSED", "START_PENDING", "STOP_PENDING",
                    "CONTINUE_PENDING", "PAUSE_PENDING")
    if a == "status":
        rc, out = _run(["sc", "query", name])
        upper = out.upper()
        state = next((tok for tok in STATE_TOKENS if tok in upper), "")
        if not state and "1060" in upper:
            state = "NOT_FOUND"
        return {"ok": rc == 0, "service": name, "state": state, "raw": out.strip()[:900]}
    if a in ("start", "stop", "restart", "pause", "continue"):
        if a == "restart":
            _run(["sc", "stop", name])
            time.sleep(1.5)
            rc, out = _run(["sc", "start", name])
        else:
            rc, out = _run(["sc", a, name])
        upper = out.upper()
        state = next((tok for tok in STATE_TOKENS if tok in upper), "")
        return {"ok": rc == 0, "service": name, "action": a, "state": state,
                "raw": out.strip()[:900],
                "hint": "" if rc == 0 else "sc start/stop exige Hermes elevado (administrador)"}
    raise ValueError(f"unknown service action {action!r}")


# --------------------------------------------------------------------------- #
# Power
# --------------------------------------------------------------------------- #

def power(action: str, delay_s: int = 30) -> dict[str, Any]:
    a = (action or "").lower()
    if a == "lock":
        return {"ok": bool(user32.LockWorkStation()), "action": "lock"}
    if a in ("sleep", "suspend", "hibernate"):
        hibernate = a == "hibernate"
        try:
            ok = bool(ctypes.windll.powrprof.SetSuspendState(1 if hibernate else 0, 1, 0))
        except Exception:
            ok = bool(ctypes.windll.powrprof.SetSuspendState(hibernate, True, False))
        return {"ok": ok, "action": a}
    if a in ("shutdown", "restart", "reboot", "logoff"):
        flags = {"shutdown": "/s", "restart": "/r", "reboot": "/r", "logoff": "/l"}[a]
        args = ["shutdown", flags, "/t", str(int(delay_s))]
        if a in ("shutdown", "restart", "reboot"):
            args.append("/f")
        rc, out = _run(args)
        return {"ok": rc == 0, "action": a, "delay_s": delay_s, "raw": out.strip()[:400],
                "cancel": "pc_power action=cancel para abortar"}
    if a == "cancel":
        rc, out = _run(["shutdown", "/a"])
        return {"ok": rc == 0, "action": "cancel", "raw": out.strip()[:400]}
    raise ValueError(f"unknown power action {action!r}")


# --------------------------------------------------------------------------- #
# UI Automation (background, no focus, works on WinUI/XAML/Chromium/Electron)
#
# Driven through Windows PowerShell's .NET UIAutomationClient: no pip dependency
# and no COM vtable work in ctypes. ~0.4-1.2 s per call.
# --------------------------------------------------------------------------- #

_PS_SCRIPT = Path(__file__).resolve().parent / "uia.ps1"
_PS_CACHE: dict[str, Any] = {}


def _run_ps(args: list[str], timeout: int = 60, script: Path | None = None) -> dict[str, Any]:
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
           "-File", str(script or _PS_SCRIPT)] + [str(a) for a in args]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=0x08000000)  # CREATE_NO_WINDOW
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"UIA timeout ({timeout}s) — reduza limit"}
    out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    if not out:
        return {"ok": False, "error": f"powershell sem saída (rc={proc.returncode}): {err[:400]}"}
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                continue
    return {"ok": False, "error": f"saída não-JSON: {out[:300]} {err[:200]}"}


def _run_ps_inline(script: str, timeout: int = 60) -> str:
    """Run an inline PowerShell script (base64/EncodedCommand: no quoting hell).

    Used by the dependency-free paths (clipboard, screenshots) so pc-control also
    works on a bare python without pywin32/Pillow — e.g. under Antigravity's MCP.
    """
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", enc],
            capture_output=True, timeout=timeout, creationflags=0x08000000)
    except subprocess.TimeoutExpired:
        return ""
    raw = (proc.stdout or b"") + (proc.stderr or b"")
    return raw.decode("utf-8", errors="replace")


def uia_available() -> bool:
    if "avail" not in _PS_CACHE:
        try:
            _PS_CACHE["avail"] = bool(shutil.which("powershell.exe") or
                                      Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists())
        except Exception:
            _PS_CACHE["avail"] = False
    return _PS_CACHE["avail"]


def _uia_args(hwnd: int | None, name: str | None, automation_id: str | None,
              type_: str | None, index: int, value: str | None, contains: bool,
              extra: dict[str, Any] | None = None, self_mode: bool = False) -> list[str]:
    args: list[str] = ["-Hwnd", int(hwnd or 0)]
    if name:
        args += ["-Name", name]
    if automation_id:
        args += ["-AutomationId", automation_id]
    if type_:
        args += ["-Type", type_]
    if index:
        args += ["-Index", int(index)]
    if value is not None:
        args += ["-Value", value]
    if contains:
        args.append("-Contains")
    if self_mode:
        args.append("-Self")
    for key, val in (extra or {}).items():
        if val is None or val == "":
            continue
        args += [f"-{key}", str(val)]
    return args


def uia_dump(hwnd: int | None = None, limit: int = 200) -> dict[str, Any]:
    if not uia_available():
        raise RuntimeError("PowerShell não encontrado: camada UIA indisponível")
    res = _run_ps(["-Action", "dump", "-Hwnd", int(hwnd or 0), "-Limit", int(limit)])
    if res.get("ok"):
        res["hwnd"] = int(hwnd or 0)
    return res


def uia_do(action: str, hwnd: int | None = None, name: str | None = None,
           automation_id: str | None = None, type_: str | None = None,
           index: int = 0, value: str | None = None, contains: bool = False,
           x: int | None = None, y: int | None = None, w: int | None = None,
           h: int | None = None, direction: str | None = None,
           timeout: int = 60, self_mode: bool = False) -> dict[str, Any]:
    if not uia_available():
        raise RuntimeError("PowerShell não encontrado: camada UIA indisponível")
    extra = {"X": x, "Y": y, "W": w, "H": h, "Direction": direction}
    res = _run_ps(["-Action", action]
                  + _uia_args(hwnd, name, automation_id, type_, index, value, contains,
                              extra, self_mode),
                  timeout=timeout)
    if not res.get("ok") and "nenhum elemento casou" in str(res.get("error") or ""):
        res["suggestions"] = _uia_suggestions(hwnd, name, type_)
    return res


def _uia_suggestions(hwnd: int | None, name: str | None, type_: str | None) -> list[str]:
    """Names close to the queried one — saves a blind re-dump round trip."""
    try:
        dump = uia_dump(hwnd, limit=400)
        rows = [(e.get("name") or "", e.get("type") or "", e.get("automation_id") or "")
                for e in (dump.get("elements") or [])]
    except Exception:
        return []
    rows = [r for r in rows if r[0]]
    needle = (name or "").lower()
    tips: list[str] = []
    for n, t, a in rows:
        if type_ and t != type_:
            continue
        if needle and (needle in n.lower() or n.lower() in needle):
            tips.append(f"{t}: {n!r}" + (f" (automation_id={a})" if a else ""))
    if not tips:
        for close in difflib.get_close_matches(name or "", [r[0] for r in rows], n=6, cutoff=0.4):
            row = next(r for r in rows if r[0] == close)
            tips.append(f"~ {row[1]}: {close!r}")
    return tips[:8]


def uia_find(hwnd: int | None = None, name: str | None = None,
             automation_id: str | None = None, type_: str | None = None,
             index: int = 0, contains: bool = False) -> dict[str, Any]:
    return uia_do("find", hwnd=hwnd, name=name, automation_id=automation_id,
                  type_=type_, index=index, contains=contains)


def uia_click_element(hwnd: int | None = None, name: str | None = None,
                      automation_id: str | None = None, type_: str | None = None,
                      index: int = 0, contains: bool = False) -> dict[str, Any]:
    """Click by accessible name, background-first.

    Ladder: UIA InvokePattern -> Win32 BM_CLICK on the matching Button child ->
    foreground click at the element center. Each rung reports what it used, so the
    caller never assumes background was possible when it wasn't.
    """
    res = uia_do("invoke", hwnd=hwnd, name=name, automation_id=automation_id,
                 type_=type_, index=index, contains=contains)
    if res.get("ok"):
        res["method"] = "uia-invoke"
        return res

    found = uia_find(hwnd=hwnd, name=name, automation_id=automation_id, type_=type_,
                     index=index, contains=contains)
    if not found.get("ok"):
        res["method"] = "none"
        return res

    el = found.get("element") or {}
    rect = el.get("rect") or [0, 0, 0, 0]
    cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    root = int(hwnd or user32.GetForegroundWindow() or 0)

    child = _button_child_at(root, cx, cy)
    if child:
        pressed = bg_press_button(child["hwnd"])
        if pressed.get("delivered"):
            return {"ok": True, "method": "bm-click", "control": child,
                    "element": el, "invoke_error": res.get("error")}

    fg_click(cx, cy)
    return {"ok": True, "method": "foreground-click", "point": [cx, cy], "element": el,
            "invoke_error": res.get("error")}


def _button_child_at(root: int, x: int, y: int) -> dict[str, Any] | None:
    """Win32 Button child covering (x, y) — lets BM_CLICK work where UIA has no pattern."""
    try:
        for el in elements(root, depth=6, include_empty=True):
            if el["class"] not in ("Button", "SysButton"):
                continue
            l, t, r, b = el["rect"]
            if l <= x <= r and t <= y <= b:
                return el
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------- #
# COM / IDispatch — command the app's own object model ("from inside its code")
# --------------------------------------------------------------------------- #

_COM_SCRIPT = Path(__file__).resolve().parent / "com.ps1"


def com_do(action: str, progid: str, member: str = "", args: list | None = None,
           value: Any = None, create: bool = False, limit: int = 200) -> dict[str, Any]:
    """attach|create|members|get|set|call on any registered COM ProgID.

    ``attach`` first: Marshal.GetActiveObject grabs the instance that is already
    running, so the agent drives the user's session instead of spawning a second
    invisible one. Falls back to New-Object when nothing is running.
    """
    if not progid:
        raise ValueError("progid é obrigatório (ex: 'Word.Application', 'Shell.Application')")
    argv = ["-Action", action, "-ProgId", progid, "-Limit", int(limit)]
    if member:
        argv += ["-Member", member]
    if args is not None:
        argv += ["-ArgsJson", json.dumps(args, ensure_ascii=False)]
    if value is not None:
        argv += ["-ValueJson", json.dumps(value, ensure_ascii=False)]
    if create:
        argv.append("-Create")
    res = _run_ps(argv, timeout=90, script=_COM_SCRIPT)
    res.setdefault("progid", progid)
    return res


# --------------------------------------------------------------------------- #
# Raw Win32 messages — the app's own internal command path
# --------------------------------------------------------------------------- #

def win32_send(hwnd: int, msg: int, wparam: int = 0, lparam: int = 0,
               post: bool = True, timeout_ms: int = 2000) -> dict[str, Any]:
    h = resolve_hwnd(hwnd)
    if post:
        ok = bool(user32.PostMessageW(h, int(msg), wt.WPARAM(int(wparam)), wt.LPARAM(int(lparam))))
        mode = "post"
    else:
        result = ULONG_PTR()
        r = user32.SendMessageTimeoutW(h, int(msg), wt.WPARAM(int(wparam)), wt.LPARAM(int(lparam)),
                                       SMTO_ABORTIFHUNG | SMTO_BLOCK, timeout_ms, ctypes.byref(result))
        ok = bool(r)
        mode = f"send result={result.value}"
    return {"ok": ok, "hwnd": h, "msg": hex(int(msg)), "wparam": int(wparam),
            "lparam": int(lparam), "mode": mode, "class": class_name(h)}


def wm_command(hwnd: int, control_id: int, notify: int = 0, post: bool = True,
               child_hwnd: int = 0) -> dict[str, Any]:
    """WM_COMMAND to the parent — the same path a real menu/button click takes."""
    h = resolve_hwnd(hwnd)
    parent = int(user32.GetParent(h) or 0) or h
    wparam = (int(notify) << 16) | (int(control_id) & 0xFFFF)
    lparam = int(child_hwnd) or 0
    out = win32_send(parent, WM_COMMAND, wparam, lparam, post=post)
    out.update({"target_parent": parent, "control_id": int(control_id), "notify": int(notify)})
    return out


def find_control(root: int, control_id: int | None = None, text: str | None = None,
                 class_: str | None = None) -> dict[str, Any]:
    tn = (text or "").lower()
    cn = (class_ or "").lower()
    for el in elements(root, depth=8, include_empty=True):
        if control_id is not None and el["id"] != int(control_id):
            continue
        if tn and tn not in (el["text"] or "").lower():
            continue
        if cn and cn != (el["class"] or "").lower():
            continue
        return el
    raise LookupError(f"nenhum controle casou id={control_id} text={text!r} class={class_!r}")


# --------------------------------------------------------------------------- #
# pc_act — the universal ladder: background first, foreground only as last rung
# --------------------------------------------------------------------------- #

def _probe(h: int) -> tuple:
    """Cheap state fingerprint used to tell whether an unverifiable click did something."""
    try:
        return (int(user32.GetForegroundWindow() or 0), window_text(h), bool(user32.IsWindow(h)),
                bool(user32.IsIconic(h)))
    except Exception:
        return (0, "", False, False)


def _uia_value_of(h: int, name: str | None, automation_id: str | None,
                  type_: str | None, index: int, contains: bool) -> str | None:
    try:
        res = uia_do("get_value", hwnd=h, name=name, automation_id=automation_id,
                     type_=type_, index=index, contains=contains)
        if res.get("ok"):
            return res.get("value")
    except Exception:
        return None
    return None


def act(action: str, hwnd: int | None = None, name: str | None = None,
        automation_id: str | None = None, type_: str | None = None, text: str | None = None,
        x: int | None = None, y: int | None = None, coords: str = "screen",
        allow_foreground: bool = True, index: int = 0, contains: bool = False,
        direction: str | None = None, value: Any = None) -> dict[str, Any]:
    """Do *action* on an app element, walking every layer until one works.

    Order is always background -> foreground:
      uia-pattern -> uia-legacy(MSAA) -> win32-message -> foreground(SendInput)

    Returns ``resolved_by``, ``foreground_used`` and ``verified``; the caller never
    has to guess which mechanism was needed.
    """
    h = resolve_hwnd(hwnd)
    a = (action or "click").lower()
    layers: list[dict[str, Any]] = []
    before = _probe(h)

    def run(layer: str, fn) -> dict[str, Any] | None:
        try:
            out = fn()
        except Exception as exc:
            layers.append({"layer": layer, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return None
        ok = bool(out.get("ok", True)) if isinstance(out, dict) else bool(out)
        entry: dict[str, Any] = {"layer": layer, "ok": ok}
        if isinstance(out, dict):
            for key in ("detail", "method", "value", "error", "reason", "hint"):
                if out.get(key):
                    entry[key] = out[key]
        layers.append(entry)
        return out if ok else None

    def done(layer: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        after = _probe(h)
        verified = None
        if a in ("set", "settext", "type", "write"):
            got = _uia_value_of(h, name, automation_id, type_, index, contains)
            verified = bool(got and text and str(text).strip()[:20] in str(got))
        elif a in ("click", "invoke", "toggle", "legacy", "press"):
            verified = after != before
        out = {"ok": True, "action": a, "resolved_by": layer,
               "foreground_used": layer.startswith("foreground"),
               "verified": verified, "layers": layers, "hwnd": h}
        if not out["foreground_used"]:
            out["invisible"] = True
        if extra:
            out.update(extra)
        if verified is False:
            out["hint"] = ("a resposta do mecanismo foi ok mas o estado não mudou visivelmente: "
                           "confirme com pc_screenshot target=window ou tente action=click com verify")
        return out

    def element_rect() -> tuple[int, int]:
        found = uia_find(hwnd=h, name=name, automation_id=automation_id, type_=type_,
                         index=index, contains=contains)
        if not found.get("ok"):
            raise LookupError(found.get("error") or "elemento não encontrado")
        rect = (found.get("element") or {}).get("rect") or [0, 0, 0, 0]
        return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2

    # ---------- click-like ----------
    if a in ("click", "invoke", "toggle", "legacy", "press"):
        if run("uia-pattern", lambda: uia_do("invoke" if a != "toggle" else "toggle", hwnd=h, name=name,
                                             automation_id=automation_id, type_=type_, index=index,
                                             contains=contains)):
            return done("uia-pattern")
        if run("uia-legacy", lambda: uia_do("legacy", hwnd=h, name=name, automation_id=automation_id,
                                            type_=type_, index=index, contains=contains)):
            return done("uia-legacy")
        if run("uia-select", lambda: uia_do("select", hwnd=h, name=name, automation_id=automation_id,
                                            type_=type_, index=index, contains=contains)):
            return done("uia-select")

            if run("uia-child-self", lambda: uia_do("invoke" if a != "toggle" else "toggle", hwnd=h, name=name,
                                             automation_id=automation_id, type_=type_, index=index,
                                             contains=contains, self_mode=True)):
                return done("uia-child-self")

        try:
            cx, cy = (int(x), int(y)) if (x is not None and y is not None) else element_rect()
        except Exception as exc:
            layers.append({"layer": "resolve-point", "ok": False, "error": str(exc)})
            cx = cy = None

        if cx is not None:
            child = _button_child_at(h, cx, cy)
            if child and run("win32-bm-click", lambda: bg_press_button(child["hwnd"])):
                return done("win32-bm-click", {"control": child})
            if a != "press" and run("win32-message", lambda: bg_click(h, cx, cy, coords="screen")):
                res = done("win32-message", {"point": [cx, cy]})
                if res["verified"]:
                    return res
                layers[-1]["note"] = "entregue, sem confirmação visível"
            if allow_foreground:
                window_action(h, "focus")
                time.sleep(0.12)
                if run("foreground-click", lambda: (fg_click(cx, cy), {"ok": True, "point": [cx, cy]})[1]):
                    return done("foreground-click", {"point": [cx, cy]})
        return {"ok": False, "action": a, "layers": layers,
                "error": "nenhuma camada conseguiu (e foreground desabilitado)" if not allow_foreground
                else "todas as camadas falharam"}

    # ---------- text ----------
    if a in ("type", "set", "settext", "write"):
        body = str(text if text is not None else (value if value is not None else ""))
        if not body:
            return {"ok": False, "error": "texto vazio"}

        def verify_text() -> dict[str, Any] | None:
            got = _uia_value_of(h, name, automation_id, type_, index, contains)
            if got is not None and body.strip()[:20] in str(got):
                return {"ok": True, "value": str(got)[:200]}
            return None

        if run("uia-value", lambda: uia_do("set_value", hwnd=h, name=name, automation_id=automation_id,
                                           type_=type_, index=index, value=body, contains=contains)):
            if verify_text():
                return done("uia-value", {"readback": True})
            layers[-1]["note"] = "SetValue aceito, read-back não confirmou"
        if run("uia-legacy-value", lambda: uia_do("legacy_value", hwnd=h, name=name,
                                                 automation_id=automation_id, type_=type_,
                                                 index=index, value=body, contains=contains)):
            if verify_text():
                return done("uia-legacy-value", {"readback": True})

        try:
            target = _edit_child_near(h, name, automation_id, type_, index, contains)
        except Exception:
            target = None
        if target and run("win32-settext", lambda: bg_settext(target["hwnd"], body)):
            if verify_text() or window_text(target["hwnd"]).strip():
                return done("win32-settext", {"control": target})
        if run("win32-wmchar", lambda: bg_type(target["hwnd"] if target else h, body)):
            if verify_text():
                return done("win32-wmchar", {"control": target})
        if allow_foreground:
            window_action(h, "focus")
            time.sleep(0.12)
            if run("foreground-type", lambda: (fg_type(body), {"ok": True, "chars": len(body)})[1]):
                return done("foreground-type", {"chars": len(body)})
        return {"ok": False, "action": a, "layers": layers, "error": "todas as camadas falharam"}

    # ---------- keys ----------
    if a in ("key", "hotkey", "keys"):
        combo = str(text or value or "")
        if not combo:
            return {"ok": False, "error": "combo vazio (ex: 'ctrl+s')"}
        if run("win32-key", lambda: bg_key(h, combo)):
            res = done("win32-key")
            if res["verified"]:
                return res
        if allow_foreground:
            window_action(h, "focus")
            time.sleep(0.12)
            if run("foreground-key", lambda: (fg_key(combo), {"ok": True})[1]):
                return done("foreground-key")
        return {"ok": False, "action": a, "layers": layers, "error": "todas as camadas falharam"}

    # ---------- read-only ----------
    if a in ("read", "get", "get_value", "text"):
        got = _uia_value_of(h, name, automation_id, type_, index, contains)
        if got is not None:
            return {"ok": True, "action": "read", "resolved_by": "uia-value", "value": got,
                    "foreground_used": False, "verified": True, "layers": layers}
        el = find_control(h, text=text or name)
        return {"ok": True, "action": "read", "resolved_by": "win32-text", "value": el.get("text"),
                "foreground_used": False, "verified": True, "layers": layers}

    # ---------- window-level ----------
    if a in ("minimize", "maximize", "restore", "close", "focus", "move", "resize"):
        if a in ("minimize", "maximize", "restore"):
            if run("uia-window-state", lambda: uia_do("window_state", hwnd=h, direction=a)):
                return done("uia-window-state")
        if a == "close" and run("uia-window-close", lambda: uia_do("window_close", hwnd=h)):
            return done("uia-window-close")
        if a in ("move", "resize") and run("uia-transform",
                                           lambda: uia_do("move" if a == "move" else "resize",
                                                          hwnd=h, x=x, y=y,
                                                          w=value if a == "resize" else None,
                                                          h=None)):
            return done("uia-transform")
        if run("win32-window", lambda: window_action(h, a, x=x, y=y)):
            return done("win32-window")
        return {"ok": False, "action": a, "layers": layers, "error": "nenhuma camada conseguiu"}

    return {"ok": False, "error": f"action desconhecida {action!r}",
            "actions": ["click", "invoke", "toggle", "type", "set", "key", "read",
                        "minimize", "maximize", "restore", "close", "move", "resize"]}


def _edit_child_near(h: int, name: str | None, automation_id: str | None,
                     type_: str | None, index: int, contains: bool) -> dict[str, Any] | None:
    """Win32 Edit control matching the UIA element's box — for WM_SETTEXT fallback."""
    try:
        rect = None
        found = uia_find(hwnd=h, name=name, automation_id=automation_id, type_=type_,
                         index=index, contains=contains)
        if found.get("ok"):
            rect = (found.get("element") or {}).get("rect")
        for el in elements(h, depth=8, include_empty=True):
            if not is_editable_class(el["class"]):
                continue
            if rect:
                l, t, r, b = el["rect"]
                if not (l <= rect[0] <= r and t <= rect[1] <= b):
                    continue
            return el
    except Exception:
        return None
    return None
