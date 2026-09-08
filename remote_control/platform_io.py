from __future__ import annotations

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


@dataclass(slots=True)
class CapturedFrame:
    jpeg: bytes
    width: int
    height: int


class ScreenCapture:
    def __init__(self, jpeg_quality: int = 60, max_width: int = 1600) -> None:
        self.jpeg_quality = max(25, min(jpeg_quality, 90))
        self.max_width = max(640, max_width)
        self._sct = mss.mss()

    def close(self) -> None:
        self._sct.close()

    def capture(self) -> CapturedFrame:
        monitor = self._sct.monitors[1]
        shot = self._sct.grab(monitor)
        image = Image.frombytes("RGB", shot.size, shot.rgb)
        if image.width > self.max_width:
            ratio = self.max_width / image.width
            image = image.resize((self.max_width, max(1, round(image.height * ratio))), Image.Resampling.BILINEAR)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=self.jpeg_quality, optimize=False)
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
    pyautogui.moveTo(x, y)


def mouse_button(button: str, down: bool) -> None:
    button = button if button in {"left", "middle", "right"} else "left"
    if down:
        pyautogui.mouseDown(button=button)
    else:
        pyautogui.mouseUp(button=button)


def mouse_scroll(amount: int) -> None:
    pyautogui.scroll(amount)


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


def key_event(key: str, down: bool) -> None:
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
