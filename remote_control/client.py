from __future__ import annotations

import asyncio
import queue
import socket
import threading
from collections.abc import Callable
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .peer import MessageCallback, PeerSession
from .platform_io import platform_name
from .protocol import ProtocolError, decode_message, encode_message

StatusCallback = Callable[[str], None]

KEEPALIVE_INTERVAL = 15
KEEPALIVE_TIMEOUT = 60
RECONNECT_DELAYS = (1, 2, 5, 10, 15)


class RemoteClient:
    """Create and maintain the outbound peer connection.

    This is intentionally resilient for locked-down office PCs. The Windows side
    can establish one outbound WebSocket and, if Wi-Fi or a corporate network
    briefly interrupts it, reconnect automatically without requiring an inbound
    Windows firewall rule.
    """

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        frame_queue: queue.Queue[bytes],
        fps: int = 20,
        jpeg_quality: int = 50,
        max_width: int = 1280,
        message_callback: MessageCallback | None = None,
        status_callback: StatusCallback | None = None,
    ) -> None:
        self.host = host.strip()
        self.port = int(port)
        self.token = token
        self.frame_queue = frame_queue
        self.fps = max(2, min(int(fps), 30))
        self.jpeg_quality = max(25, min(int(jpeg_quality), 90))
        self.max_width = max(640, int(max_width))
        self.message_callback = message_callback or (lambda _message: None)
        self.status_callback = status_callback or (lambda _message: None)

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._started = threading.Event()
        self._error: Exception | None = None
        self._session: PeerSession | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def connected(self) -> bool:
        return bool(self._session and self._session.connected)

    @property
    def role(self) -> str:
        if not self._session:
            return "disconnected"
        return self._session.role

    def start(self) -> None:
        if self.running:
            return
        self._error = None
        self._started.clear()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="remote-client",
            daemon=True,
        )
        self._thread.start()
        self._started.wait(timeout=2)
        if self._error:
            raise self._error

    def stop(self) -> None:
        loop = self._loop
        stop_event = self._stop_event
        if loop and stop_event:
            loop.call_soon_threadsafe(stop_event.set)

        session = self._session
        if session:
            session.close_threadsafe()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def send(self, message_type: str, **payload: Any) -> None:
        session = self._session
        if session:
            session.send_threadsafe(message_type, **payload)

    def request_control(self) -> None:
        session = self._session
        if session:
            session.request_control_threadsafe()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:
            self._error = exc
            self.status_callback(f"Client error: {exc}")
            self._started.set()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        uri = f"ws://{self.host}:{self.port}"
        self.status_callback(f"Connecting to {uri}")
        self._started.set()

        reconnect_index = 0
        ever_connected = False

        try:
            while not self._stop_event.is_set():
                session: PeerSession | None = None
                try:
                    async with connect(
                        uri,
                        max_size=8 * 1024 * 1024,
                        max_queue=4,
                        open_timeout=8,
                        close_timeout=5,
                        ping_interval=KEEPALIVE_INTERVAL,
                        ping_timeout=KEEPALIVE_TIMEOUT,
                        compression=None,
                    ) as websocket:
                        await websocket.send(
                            encode_message(
                                "auth",
                                token=self.token,
                                platform=platform_name(),
                                name=socket.gethostname(),
                                supports_role_switch=True,
                            )
                        )

                        first = await asyncio.wait_for(websocket.recv(), timeout=8)
                        if not isinstance(first, str):
                            raise ProtocolError("Expected server hello")

                        hello = decode_message(first)
                        if hello.get("type") == "auth_result" and not hello.get("ok"):
                            raise PermissionError(
                                str(hello.get("message", "Authentication failed"))
                            )
                        if hello.get("type") != "hello" or not hello.get("ok"):
                            raise ProtocolError("Unexpected server response")

                        session = PeerSession(
                            websocket,
                            initial_role="controller",
                            fps=self.fps,
                            jpeg_quality=self.jpeg_quality,
                            max_width=self.max_width,
                            frame_queue=self.frame_queue,
                            remote_info={
                                key: hello[key]
                                for key in ("platform", "name")
                                if key in hello
                            },
                            message_callback=self.message_callback,
                            status_callback=self.status_callback,
                        )
                        self._session = session
                        session.start_initial_controller(hello)

                        if ever_connected:
                            self.status_callback(
                                f"Reconnected to {self.host}:{self.port}"
                            )
                        else:
                            self.status_callback(
                                f"Connected to {self.host}:{self.port}"
                            )
                        ever_connected = True
                        reconnect_index = 0

                        await session.run()

                except (PermissionError, ProtocolError):
                    # Authentication/protocol errors won't improve by retrying.
                    raise
                except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                    if self._stop_event.is_set():
                        break
                    reason = self._connection_reason(exc)
                    delay = RECONNECT_DELAYS[
                        min(reconnect_index, len(RECONNECT_DELAYS) - 1)
                    ]
                    reconnect_index += 1
                    self.status_callback(
                        f"Connection interrupted ({reason}). Reconnecting in {delay}s…"
                    )
                    self.message_callback(
                        {
                            "type": "session_role",
                            "role": "reconnecting",
                            "retry_seconds": delay,
                        }
                    )
                    if await self._wait_or_stop(delay):
                        break
                finally:
                    if session:
                        await session.close(close_socket=False)
                    if self._session is session:
                        self._session = None

                # session.run() can return cleanly when the network disappears or
                # the peer closes. Retry unless the user explicitly disconnected.
                if (
                    not self._stop_event.is_set()
                    and self._session is None
                    and reconnect_index == 0
                ):
                    delay = RECONNECT_DELAYS[0]
                    reconnect_index = 1
                    self.status_callback(
                        f"Connection ended. Reconnecting in {delay}s…"
                    )
                    self.message_callback(
                        {
                            "type": "session_role",
                            "role": "reconnecting",
                            "retry_seconds": delay,
                        }
                    )
                    if await self._wait_or_stop(delay):
                        break
        finally:
            self._session = None
            self.message_callback({"type": "session_role", "role": "disconnected"})
            self.status_callback("Disconnected")

    async def _wait_or_stop(self, seconds: float) -> bool:
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    @staticmethod
    def _connection_reason(exc: BaseException) -> str:
        if isinstance(exc, ConnectionClosed):
            code = getattr(exc, "code", None)
            reason = getattr(exc, "reason", "")
            if code is not None:
                return f"WebSocket {code}{': ' + reason if reason else ''}"
        text = str(exc).strip()
        return text or exc.__class__.__name__
