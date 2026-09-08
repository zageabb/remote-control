from __future__ import annotations

import asyncio
import contextlib
import hmac
import threading
import time
from collections.abc import Callable

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .platform_io import (
    ScreenCapture,
    get_clipboard,
    key_event,
    mouse_button,
    mouse_scroll,
    move_mouse,
    platform_name,
    set_clipboard,
)
from .protocol import ProtocolError, decode_message, encode_message

StatusCallback = Callable[[str], None]


class RemoteHost:
    def __init__(
        self,
        bind: str,
        port: int,
        token: str,
        fps: int = 12,
        jpeg_quality: int = 60,
        max_width: int = 1600,
        status_callback: StatusCallback | None = None,
    ) -> None:
        self.bind = bind
        self.port = port
        self.token = token
        self.fps = max(2, min(fps, 30))
        self.jpeg_quality = jpeg_quality
        self.max_width = max_width
        self.status_callback = status_callback or (lambda _message: None)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._started = threading.Event()
        self._error: Exception | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._error = None
        self._started.clear()
        self._thread = threading.Thread(target=self._thread_main, name="remote-host", daemon=True)
        self._thread.start()
        self._started.wait(timeout=3)
        if self._error:
            raise self._error

    def stop(self) -> None:
        loop = self._loop
        stop_event = self._stop_event
        if loop and stop_event:
            loop.call_soon_threadsafe(stop_event.set)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:
            self._error = exc
            self.status_callback(f"Host error: {exc}")
            self._started.set()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        async with serve(self._handle_client, self.bind, self.port, max_size=8 * 1024 * 1024):
            self.status_callback(f"Hosting on {self.bind}:{self.port}")
            self._started.set()
            await self._stop_event.wait()
        self.status_callback("Host stopped")

    async def _handle_client(self, websocket: ServerConnection) -> None:
        peer = websocket.remote_address
        self.status_callback(f"Connection from {peer}")
        capture: ScreenCapture | None = None
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=5)
            if not isinstance(raw, str):
                await websocket.close(code=4001, reason="Authentication required")
                return
            auth = decode_message(raw)
            if auth.get("type") != "auth" or not hmac.compare_digest(str(auth.get("token", "")), self.token):
                await websocket.send(encode_message("auth_result", ok=False, message="Invalid token"))
                await websocket.close(code=4003, reason="Invalid token")
                return

            capture = ScreenCapture(self.jpeg_quality, self.max_width)
            first = capture.capture()
            await websocket.send(
                encode_message(
                    "hello",
                    ok=True,
                    platform=platform_name(),
                    width=first.width,
                    height=first.height,
                    fps=self.fps,
                )
            )
            await websocket.send(first.jpeg)
            self.status_callback(f"Controller connected: {peer}")

            # Run screen output and control input as independent tasks.  The frame
            # sender explicitly yields on every iteration because websocket.send()
            # isn't guaranteed to suspend when socket buffers have room.  Without
            # this, a fast capture/send loop can starve _control_receiver entirely.
            receiver = asyncio.create_task(self._control_receiver(websocket))
            sender = asyncio.create_task(self._frame_sender(websocket, capture))
            done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, ConnectionClosed):
                    raise exc
        except (ConnectionClosed, asyncio.TimeoutError):
            pass
        except ProtocolError as exc:
            with contextlib.suppress(Exception):
                await websocket.send(encode_message("error", message=str(exc)))
        except Exception as exc:
            self.status_callback(f"Client error: {exc}")
        finally:
            if capture:
                capture.close()
            self.status_callback(f"Controller disconnected: {peer}")

    async def _frame_sender(self, websocket: ServerConnection, capture: ScreenCapture) -> None:
        delay = 1 / self.fps
        next_frame_at = time.monotonic()

        while True:
            # Always yield before doing synchronous capture/JPEG work.  This is
            # important even though websocket.send() is awaited: send() may finish
            # immediately and therefore doesn't guarantee fairness to the receiver.
            await asyncio.sleep(0)

            frame = capture.capture()
            await websocket.send(frame.jpeg)

            next_frame_at += delay
            remaining = next_frame_at - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
            else:
                # If capture/compression is slower than the requested frame rate,
                # don't busy-loop trying to catch up.  Reset the schedule and yield.
                next_frame_at = time.monotonic()
                await asyncio.sleep(0)

    async def _control_receiver(self, websocket: ServerConnection) -> None:
        async for raw in websocket:
            if not isinstance(raw, str):
                continue
            message = decode_message(raw)
            kind = message["type"]
            if kind == "mouse_move":
                await asyncio.to_thread(move_mouse, int(message["x"]), int(message["y"]))
            elif kind == "mouse_button":
                await asyncio.to_thread(mouse_button, str(message.get("button", "left")), bool(message.get("down")))
            elif kind == "mouse_scroll":
                await asyncio.to_thread(mouse_scroll, int(message.get("amount", 0)))
            elif kind == "key":
                key = str(message.get("key", ""))
                if key:
                    await asyncio.to_thread(key_event, key, bool(message.get("down")))
            elif kind == "clipboard_set":
                await asyncio.to_thread(set_clipboard, str(message.get("text", "")))
            elif kind == "clipboard_get":
                text = await asyncio.to_thread(get_clipboard)
                await websocket.send(encode_message("clipboard", text=text))
            elif kind == "ping":
                await websocket.send(encode_message("pong"))
