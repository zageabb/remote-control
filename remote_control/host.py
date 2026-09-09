from __future__ import annotations

import asyncio
import hmac
import queue
import threading
from collections.abc import Callable
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .peer import MessageCallback, PeerSession
from .protocol import ProtocolError, decode_message, encode_message

StatusCallback = Callable[[str], None]

KEEPALIVE_INTERVAL = 15
KEEPALIVE_TIMEOUT = 60


class RemoteHost:
    """Listen for one outbound peer connection.

    The socket remains server-side for the entire session, but control can switch
    direction repeatedly over that same full-duplex WebSocket.
    """

    def __init__(
        self,
        bind: str,
        port: int,
        token: str,
        fps: int = 20,
        jpeg_quality: int = 50,
        max_width: int = 1280,
        frame_queue: queue.Queue[bytes] | None = None,
        message_callback: MessageCallback | None = None,
        status_callback: StatusCallback | None = None,
    ) -> None:
        self.bind = bind
        self.port = port
        self.token = token
        self.fps = max(2, min(int(fps), 30))
        self.jpeg_quality = max(25, min(int(jpeg_quality), 90))
        self.max_width = max(640, int(max_width))
        self.frame_queue = frame_queue or queue.Queue(maxsize=1)
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
            name="remote-host",
            daemon=True,
        )
        self._thread.start()
        self._started.wait(timeout=3)
        if self._error:
            raise self._error

    def stop(self) -> None:
        session = self._session
        if session:
            session.close_threadsafe()

        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def disconnect_peer(self) -> None:
        session = self._session
        if session:
            session.close_threadsafe()

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
            close_timeout=5,
            ping_interval=KEEPALIVE_INTERVAL,
            ping_timeout=KEEPALIVE_TIMEOUT,
            compression=None,
        ):
            self.status_callback(f"Hosting on {self.bind}:{self.port}")
            self._started.set()
            await self._stop_event.wait()

        self.status_callback("Host stopped")

    async def _handle_client(self, websocket: ServerConnection) -> None:
        peer = websocket.remote_address

        if self._session and self._session.connected:
            await websocket.close(code=4009, reason="Another peer is already connected")
            return

        self.status_callback(f"Connection from {peer}")
        session: PeerSession | None = None

        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=8)
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

            remote_info = {
                key: auth[key]
                for key in ("platform", "name")
                if key in auth
            }

            session = PeerSession(
                websocket,
                initial_role="controlled",
                fps=self.fps,
                jpeg_quality=self.jpeg_quality,
                max_width=self.max_width,
                frame_queue=self.frame_queue,
                remote_info=remote_info,
                message_callback=self.message_callback,
                status_callback=self.status_callback,
            )
            self._session = session

            await session.start_initial_controlled()
            self.status_callback(f"Peer connected: {peer}")
            await session.run()

        except (ConnectionClosed, asyncio.TimeoutError):
            pass
        except ProtocolError as exc:
            try:
                await websocket.send(encode_message("error", message=str(exc)))
            except Exception:
                pass
        except Exception as exc:
            self.status_callback(f"Peer error: {exc}")
        finally:
            if session:
                await session.close(close_socket=False)
            if self._session is session:
                self._session = None
            self.message_callback({"type": "session_role", "role": "disconnected"})
            self.status_callback(f"Peer disconnected: {peer}")
