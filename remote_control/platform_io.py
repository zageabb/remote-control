from __future__ import annotations

import ctypes
import io
import platform
import socket
from dataclasses import dataclass

import mss
from PIL import Image
import pyautogui
import pyperclip

pyautogui.PAUSE = 0
pyautogui.FAILSAFE = False

_IS_WINDOWS = platform.system() == "Windows"

if _IS_WINDOWS:
    _user32 = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            _user32.SetProcessDPIAware()
        except Exception:
            pass

    _user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    _user32.SetCursorPos.restype = ctypes.c_bool
    _user32.mouse_event.argtypes = [
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    _user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
    _user32.VkKeyScanW.restype = ctypes.c_short

    _MOUSE_FLAGS = {
        ("left", True): 0x0002,
        ("left", False): 0x0004,
        ("right", True): 0x0008,
        ("right", False): 0x0010,
        ("middle", True): 0x0020,
        ("middle", False): 0x0040,
    }
    _MOUSEEVENTF_WHEEL = 0x0800
    _WHEEL_DELTA = 120
    _KEYEVENTF_EXTENDEDKEY = 0x0001
    _KEYEVENTF_KEYUP = 0x0002
    _VK = {
        "backspace": 0x08,
        "tab": 0x09,
        "enter": 0x0D,
        "shift": 0x10,
        "ctrl": 0x11,
        "alt": 0x12,
        "esc": 0x1B,
        "space": 0x20,
        "pageup": 0x21,
        "pagedown": 0x22,
        "end": 0x23,
        "home": 0x24,
        "left": 0x25,
        "up": 0x26,
        "right": 0x27,
        "down": 0x28,
        "delete": 0x2E,
        "win": 0x5B,
        "command": 0x5B,
    }
    for _n in range(1, 25):
        _VK[f"f{_n}"] = 0x6F + _n
    _EXTENDED_KEYS = {
        "pageup", "pagedown", "end", "home", "left", "up",
        "right", "down", "delete", "win", "command",
    }


@dataclass(slots=True)
class CapturedFrame:
    jpeg: bytes
    width: int
    height: int


class ScreenCapture:
    def __init__(self, jpeg_quality: int = 50, max_width: int = 1280) -> None:
        self.jpeg_quality = max(25, min(jpeg_quality, 90))
        self.max_width = max(640, max_width)
        self._sct = mss.mss()

    def close(self) -> None:
        self._sct.close()

    def capture(self) -> CapturedFrame:
        monitor = self._sct.monitors[1]
        shot = self._sct.grab(monitor)
        image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        if image.width > self.max_width:
            ratio = self.max_width / image.width
            image = image.resize(
                (self.max_width, max(1, round(image.height * ratio))),
                Image.Resampling.BILINEAR,
            )

        output = io.BytesIO()
        image.save(
            output,
            format="JPEG",
            quality=self.jpeg_quality,
            optimize=False,
            subsampling=2,
        )
        return CapturedFrame(output.getvalue(), shot.width, shot.height)


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def platform_name() -> str:
    return platform.system()


def move_mouse(x: int, y: int) -> None:
    if _IS_WINDOWS:
        _user32.SetCursorPos(int(x), int(y))
        return
    pyautogui.moveTo(int(x), int(y))


def mouse_button(button: str, down: bool) -> None:
    button = button if button in {"left", "middle", "right"} else "left"
    if _IS_WINDOWS:
        _user32.mouse_event(_MOUSE_FLAGS[(button, bool(down))], 0, 0, 0, 0)
        return
    if down:
        pyautogui.mouseDown(button=button)
    else:
        pyautogui.mouseUp(button=button)


def mouse_scroll(amount: int) -> None:
    if _IS_WINDOWS:
        delta = int(amount) * _WHEEL_DELTA
        _user32.mouse_event(_MOUSEEVENTF_WHEEL, 0, 0, delta & 0xFFFFFFFF, 0)
        return
    pyautogui.scroll(int(amount))


_KEY_MAP = {
    "Return": "enter",
    "Escape": "esc",
    "BackSpace": "backspace",
    "Delete": "delete",
    "Tab": "tab",
    "space": "space",
    "Left": "left",
    "Right": "right",
    "Up": "up",
    "Down": "down",
    "Home": "home",
    "End": "end",
    "Prior": "pageup",
    "Next": "pagedown",
    "Control_L": "ctrl",
    "Control_R": "ctrl",
    "Shift_L": "shift",
    "Shift_R": "shift",
    "Alt_L": "alt",
    "Alt_R": "alt",
    "Meta_L": "command",
    "Meta_R": "command",
    "Command": "command",
    "Super_L": "win",
    "Super_R": "win",
}


def normalise_key(keysym: str, char: str = "") -> str | None:
    if keysym in _KEY_MAP:
        return _KEY_MAP[keysym]
    if keysym.startswith("F") and keysym[1:].isdigit():
        return keysym.lower()
    if len(char) == 1 and char.isprintable():
        return char
    if len(keysym) == 1:
        return keysym.lower()
    return None


def _windows_virtual_key(key: str) -> tuple[int, bool] | None:
    name = key.lower()
    if name in _VK:
        return _VK[name], name in _EXTENDED_KEYS
    if len(key) == 1:
        result = int(_user32.VkKeyScanW(key))
        if result != -1:
            return result & 0xFF, False
    return None


def key_event(key: str, down: bool) -> None:
    if _IS_WINDOWS:
        resolved = _windows_virtual_key(key)
        if resolved is not None:
            vk, extended = resolved
            flags = _KEYEVENTF_EXTENDEDKEY if extended else 0
            if not down:
                flags |= _KEYEVENTF_KEYUP
            _user32.keybd_event(vk, 0, flags, 0)
            return

    if down:
        pyautogui.keyDown(key)
    else:
        pyautogui.keyUp(key)


def get_clipboard() -> str:
    try:
        return pyperclip.paste()
    except Exception:
        return ""


def set_clipboard(text: str) -> None:
    pyperclip.copy(text)
