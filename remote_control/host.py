from __future__ import annotations

import asyncio
import contextlib
import hmac
import queue
import threading
import time
from collections.abc import Callable

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .platform_io import (
    CapturedFrame,
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


class _CaptureProducer:
    """Capture and encode away from the asyncio/network thread."""

    def __init__(self, fps: int, jpeg_quality: int, max_width: int) -> None:
        self.fps = fps
        self.jpeg_quality = jpeg_quality
        self.max_width = max_width
        self.frames: queue.Queue[CapturedFrame] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._started = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: Exception | None = None

    def start(self) -> None:
        self._stop.clear()
        self._started.clear()
        self.error = None
        self._thread = threading.Thread(
            target=self._run,
            name="screen-capture",
            daemon=True,
        )
        self._thread.start()
        if not self._started.wait(timeout=5):
            raise TimeoutError("Screen capture did not start")
        if self.error:
            raise self.error

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def get(self, timeout: float = 1.0) -> CapturedFrame | None:
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def _publish(self, frame: CapturedFrame) -> None:
        if self.frames.full():
            try:
                self.frames.get_nowait()
            except queue.Empty:
                pass
        try:
            self.frames.put_nowait(frame)
        except queue.Full:
            pass

    def _run(self) -> None:
        capture: ScreenCapture | None = None
        try:
            capture = ScreenCapture(self.jpeg_quality, self.max_width)
            period = 1.0 / self.fps
            while not self._stop.is_set():
                started = time.perf_counter()
                frame = capture.capture()
                self._publish(frame)
                self._started.set()
                remaining = period - (time.perf_counter() - started)
                if remaining > 0:
                    self._stop.wait(remaining)
        except Exception as exc:
            self.error = exc
            self._started.set()
        finally:
            if capture:
                capture.close()


class RemoteHost:
    def __init__(
        self,
        bind: str,
        port: int,
        token: str,
        fps: int = 20,
        jpeg_quality: int = 50,
        max_width: int = 1280,
        status_callback: StatusCallback | None = None,
    ) -> None:
        self.bind = bind
        self.port = port
        self.token = token
        self.fps = max(2, min(fps, 30))
        self.jpeg_quality = max(25, min(jpeg_quality, 90))
        self.max_width = max(640, max_width)
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
        self._thread = threading.Thread(
            target=self._thread_main,
            name="remote-host",
            daemon=True,
        )
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
        async with serve(
            self._handle_client,
            self.bind,
            self.port,
            max_size=8 * 1024 * 1024,
            max_queue=64,
            compression=None,
        ):
            self.status_callback(f"Hosting on {self.bind}:{self.port}")
            self._started.set()
            await self._stop_event.wait()
        self.status_callback("Host stopped")

    async def _handle_client(self, websocket: ServerConnection) -> None:
        peer = websocket.remote_address
        self.status_callback(f"Connection from {peer}")
        producer: _CaptureProducer | None = None
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=5)
            if not isinstance(raw, str):
                await websocket.close(code=4001, reason="Authentication required")
                return

            auth = decode_message(raw)
            if auth.get("type") != "auth" or not hmac.compare_digest(
                str(auth.get("token", "")),
                self.token,
            ):
                await websocket.send(
                    encode_message("auth_result", ok=False, message="Invalid token")
                )
                await websocket.close(code=4003, reason="Invalid token")
                return

            producer = _CaptureProducer(
                fps=self.fps,
                jpeg_quality=self.jpeg_quality,
                max_width=self.max_width,
            )
            producer.start()
            first = await asyncio.to_thread(producer.get, 5.0)
            if first is None:
                raise TimeoutError("No screen frame was captured")

            await websocket.send(
                encode_message(
                    "hello",
                    ok=True,
                    platform=platform_name(),
                    width=first.width,
                    height=first.height,
                    fps=self.fps,
                    stream_width=self.max_width,
                )
            )
            await websocket.send(first.jpeg)
            self.status_callback(f"Controller connected: {peer}")

            receiver = asyncio.create_task(self._control_receiver(websocket))
            sender = asyncio.create_task(self._frame_sender(websocket, producer))
            done, pending = await asyncio.wait(
                {sender, receiver},
                return_when=asyncio.FIRST_COMPLETED,
            )
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
            if producer:
                producer.stop()
            self.status_callback(f"Controller disconnected: {peer}")

    async def _frame_sender(
        self,
        websocket: ServerConnection,
        producer: _CaptureProducer,
    ) -> None:
        while True:
            frame = await asyncio.to_thread(producer.get, 1.0)
            if frame is None:
                if producer.error:
                    raise producer.error
                continue
            await websocket.send(frame.jpeg)

    async def _control_receiver(self, websocket: ServerConnection) -> None:
        async for raw in websocket:
            if not isinstance(raw, str):
                continue

            message = decode_message(raw)
            kind = message["type"]

            if kind == "mouse_move":
                move_mouse(int(message["x"]), int(message["y"]))
            elif kind == "mouse_button":
                if "x" in message and "y" in message:
                    move_mouse(int(message["x"]), int(message["y"]))
                mouse_button(
                    str(message.get("button", "left")),
                    bool(message.get("down")),
                )
            elif kind == "mouse_scroll":
                mouse_scroll(int(message.get("amount", 0)))
            elif kind == "key":
                key = str(message.get("key", ""))
                if key:
                    key_event(key, bool(message.get("down")))
            elif kind == "clipboard_set":
                await asyncio.to_thread(
                    set_clipboard,
                    str(message.get("text", "")),
                )
            elif kind == "clipboard_get":
                text = await asyncio.to_thread(get_clipboard)
                await websocket.send(encode_message("clipboard", text=text))
            elif kind == "ping":
                await websocket.send(encode_message("pong"))
