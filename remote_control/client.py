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


class RemoteClient:
    """Create the outbound connection to a listening peer.

    This is ideal for a locked-down office PC: it only needs to establish the
    outbound WebSocket once. Control can then switch in either direction without
    opening an inbound port on this computer.
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
        uri = f"ws://{self.host}:{self.port}"
        self.status_callback(f"Connecting to {uri}")
        self._started.set()

        try:
            async with connect(
                uri,
                max_size=8 * 1024 * 1024,
                max_queue=4,
                open_timeout=5,
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

                first = await asyncio.wait_for(websocket.recv(), timeout=5)
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
                self.status_callback(f"Connected to {self.host}:{self.port}")
                await session.run()

        except ConnectionClosed:
            pass
        finally:
            session = self._session
            if session:
                await session.close(close_socket=False)
            self._session = None
            self.message_callback({"type": "session_role", "role": "disconnected"})
            self.status_callback("Disconnected")
