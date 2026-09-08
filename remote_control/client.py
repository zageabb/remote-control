from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .protocol import ProtocolError, decode_message, encode_message

StatusCallback = Callable[[str], None]
MessageCallback = Callable[[dict], None]


class RemoteClient:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        frame_queue: queue.Queue[bytes],
        message_callback: MessageCallback | None = None,
        status_callback: StatusCallback | None = None,
    ) -> None:
        self.host = host.strip()
        self.port = port
        self.token = token
        self.frame_queue = frame_queue
        self.message_callback = message_callback or (lambda _message: None)
        self.status_callback = status_callback or (lambda _message: None)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._send_queue: asyncio.Queue[str] | None = None
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
        self._thread = threading.Thread(target=self._thread_main, name="remote-client", daemon=True)
        self._thread.start()
        self._started.wait(timeout=2)
        if self._error:
            raise self._error

    def stop(self) -> None:
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def send(self, message_type: str, **payload) -> None:
        if not self._loop or not self._send_queue or not self.running:
            return
        raw = encode_message(message_type, **payload)
        self._loop.call_soon_threadsafe(self._queue_outbound, raw)

    def _queue_outbound(self, raw: str) -> None:
        """Queue a control message without ever blocking the Tk thread.

        If the queue is saturated by mouse motion, discard the oldest queued event
        rather than allowing input latency to grow without bound.
        """
        if self._send_queue is None:
            return
        if self._send_queue.full():
            try:
                self._send_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._send_queue.put_nowait(raw)
        except asyncio.QueueFull:
            pass

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:
            self._error = exc
            self.status_callback(f"Client error: {exc}")
            self._started.set()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._send_queue = asyncio.Queue(maxsize=250)
        self._stop_event = asyncio.Event()
        uri = f"ws://{self.host}:{self.port}"
        self.status_callback(f"Connecting to {uri}")
        self._started.set()
        try:
            async with connect(uri, max_size=8 * 1024 * 1024, open_timeout=5) as websocket:
                await websocket.send(encode_message("auth", token=self.token))
                first = await asyncio.wait_for(websocket.recv(), timeout=5)
                if not isinstance(first, str):
                    raise ProtocolError("Expected server hello")
                hello = decode_message(first)
                if hello.get("type") == "auth_result" and not hello.get("ok"):
                    raise PermissionError(str(hello.get("message", "Authentication failed")))
                if hello.get("type") != "hello" or not hello.get("ok"):
                    raise ProtocolError("Unexpected server response")
                self.message_callback(hello)
                self.status_callback(f"Connected to {self.host}:{self.port}")

                receiver = asyncio.create_task(self._receiver(websocket))
                sender = asyncio.create_task(self._sender(websocket))
                stopper = asyncio.create_task(self._stop_event.wait())
                done, pending = await asyncio.wait({receiver, sender, stopper}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in pending:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                for task in done:
                    if not task.cancelled():
                        exc = task.exception()
                        if exc and not isinstance(exc, ConnectionClosed):
                            raise exc
        except ConnectionClosed:
            pass
        finally:
            self.status_callback("Disconnected")

    async def _receiver(self, websocket) -> None:
        async for raw in websocket:
            if isinstance(raw, bytes):
                if self.frame_queue.full():
                    try:
                        self.frame_queue.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    self.frame_queue.put_nowait(raw)
                except queue.Full:
                    pass
            else:
                self.message_callback(decode_message(raw))

    async def _sender(self, websocket) -> None:
        assert self._send_queue is not None
        while True:
            raw = await self._send_queue.get()
            await websocket.send(raw)
            # websocket.send() may complete synchronously while buffers have space.
            # Yield explicitly so the frame receiver remains responsive even during
            # continuous mouse motion or rapid key input.
            await asyncio.sleep(0)
