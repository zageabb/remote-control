from __future__ import annotations

import asyncio
import queue
import socket
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

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
MessageCallback = Callable[[dict[str, Any]], None]


class CaptureProducer:
    """Capture and JPEG-encode on a dedicated thread.

    Only the newest frame is retained, so a slow network never creates a stale
    video backlog.
    """

    def __init__(self, fps: int, jpeg_quality: int, max_width: int) -> None:
        self.fps = max(2, min(int(fps), 30))
        self.jpeg_quality = max(25, min(int(jpeg_quality), 90))
        self.max_width = max(640, int(max_width))
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


class PeerSession:
    """A full-duplex remote-control session over one existing WebSocket.

    Exactly one peer is the controller at a time. The controlled peer streams
    its screen and accepts input. The controlled peer can request control; its
    current stream is stopped before the request is sent, then the other peer
    starts streaming back over the same WebSocket.
    """

    def __init__(
        self,
        websocket: Any,
        *,
        initial_role: str,
        fps: int = 20,
        jpeg_quality: int = 50,
        max_width: int = 1280,
        frame_queue: queue.Queue[bytes] | None = None,
        remote_info: dict[str, Any] | None = None,
        message_callback: MessageCallback | None = None,
        status_callback: StatusCallback | None = None,
    ) -> None:
        if initial_role not in {"controller", "controlled"}:
            raise ValueError("initial_role must be 'controller' or 'controlled'")

        self.websocket = websocket
        self.role = initial_role
        self.fps = max(2, min(int(fps), 30))
        self.jpeg_quality = max(25, min(int(jpeg_quality), 90))
        self.max_width = max(640, int(max_width))
        self.frame_queue = frame_queue or queue.Queue(maxsize=1)
        self.remote_info = dict(remote_info or {})
        self.message_callback = message_callback or (lambda _message: None)
        self.status_callback = status_callback or (lambda _message: None)

        self._loop = asyncio.get_running_loop()
        self._send_lock = asyncio.Lock()
        self._switch_lock = asyncio.Lock()
        self._producer: CaptureProducer | None = None
        self._frame_task: asyncio.Task[None] | None = None
        self._outbound_task: asyncio.Task[None] | None = None
        self._send_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=512)
        self._send_wakeup = asyncio.Event()
        self._latest_mouse: tuple[int, int] | None = None
        self._pending_request_id: str | None = None
        self._closed = False

    @property
    def connected(self) -> bool:
        return not self._closed

    def send_threadsafe(self, message_type: str, **payload: Any) -> None:
        if self._closed or self.role != "controller":
            return
        self._loop.call_soon_threadsafe(
            self._queue_outbound,
            message_type,
            payload,
        )

    def request_control_threadsafe(self) -> None:
        if self._closed or self.role != "controlled":
            return

        def schedule() -> None:
            asyncio.create_task(self.request_control())

        self._loop.call_soon_threadsafe(schedule)

    def close_threadsafe(self) -> None:
        if self._closed:
            return

        def schedule() -> None:
            asyncio.create_task(self.close())

        self._loop.call_soon_threadsafe(schedule)

    async def start_initial_controlled(self) -> None:
        await self._start_streaming(announcement="hello")
        self.status_callback("Remote peer is controlling this computer")

    def start_initial_controller(self, hello: dict[str, Any]) -> None:
        self.role = "controller"
        self.remote_info = self._extract_remote_info(hello)
        self._clear_frames()
        self._emit_role("controller", hello)
        self.status_callback("Controlling remote computer")

    async def run(self) -> None:
        self._outbound_task = asyncio.create_task(
            self._outbound_sender(),
            name="peer-outbound",
        )
        try:
            async for raw in self.websocket:
                if isinstance(raw, bytes):
                    self._handle_frame(raw)
                elif isinstance(raw, str):
                    await self._handle_message(decode_message(raw))
        except ConnectionClosed:
            pass
        finally:
            await self.close(close_socket=False)

    async def close(self, *, close_socket: bool = True) -> None:
        if self._closed:
            return
        self._closed = True

        current = asyncio.current_task()
        if self._outbound_task and self._outbound_task is not current:
            self._outbound_task.cancel()
            try:
                await self._outbound_task
            except asyncio.CancelledError:
                pass
        self._outbound_task = None

        await self._stop_streaming()

        if close_socket:
            try:
                await self.websocket.close()
            except Exception:
                pass

    async def request_control(self) -> None:
        async with self._switch_lock:
            if self._closed or self.role != "controlled":
                return

            request_id = uuid.uuid4().hex[:12]
            self._pending_request_id = request_id
            self.role = "requesting"
            self._clear_outbound()
            self._emit_role("requesting")
            self.status_callback("Requesting control of remote computer…")

            # Stop our current stream before the request enters the WebSocket.
            # WebSocket message ordering guarantees the remote receives all prior
            # screen frames before it sees control_request.
            await self._stop_streaming()
            await self._send_text(
                encode_message("control_request", request_id=request_id)
            )

    async def _handle_message(self, message: dict[str, Any]) -> None:
        kind = message["type"]

        if kind == "control_request":
            await self._handle_control_request(message)
            return
        if kind == "control_granted":
            await self._handle_control_granted(message)
            return
        if kind == "control_denied":
            await self._handle_control_denied(message)
            return

        if kind == "clipboard":
            if self.role == "controller":
                self.message_callback(message)
            return
        if kind == "error":
            self.message_callback(message)
            return
        if kind == "ping":
            await self._send_text(encode_message("pong"))
            return
        if kind == "pong":
            return

        # Remote input is only honoured while this machine is the controlled peer.
        if self.role != "controlled":
            return

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
            await self._send_text(encode_message("clipboard", text=text))

    async def _handle_control_request(self, message: dict[str, Any]) -> None:
        request_id = str(message.get("request_id", ""))
        if not request_id:
            return

        async with self._switch_lock:
            if self.role != "controller":
                await self._send_text(
                    encode_message(
                        "control_denied",
                        request_id=request_id,
                        reason="This peer is not currently the controller",
                    )
                )
                return

            self.status_callback("Remote peer requested control; switching direction…")
            try:
                await self._start_streaming(
                    announcement="control_granted",
                    request_id=request_id,
                )
            except Exception as exc:
                # Remain the controller if local capture cannot start.
                self.role = "controller"
                self._emit_role("controller", self.remote_info)
                await self._send_text(
                    encode_message(
                        "control_denied",
                        request_id=request_id,
                        reason=f"Unable to start local screen capture: {exc}",
                    )
                )
                self.status_callback(f"Control switch failed: {exc}")

    async def _handle_control_granted(self, message: dict[str, Any]) -> None:
        request_id = str(message.get("request_id", ""))
        if not request_id or request_id != self._pending_request_id:
            return

        self._pending_request_id = None
        self.role = "controller"
        self.remote_info = self._extract_remote_info(message)
        self._clear_frames()
        self._emit_role("controller", message)
        self.status_callback("Control transferred — now controlling remote computer")

    async def _handle_control_denied(self, message: dict[str, Any]) -> None:
        request_id = str(message.get("request_id", ""))
        if not request_id or request_id != self._pending_request_id:
            return

        self._pending_request_id = None
        reason = str(message.get("reason", "Control request was denied"))
        self.status_callback(reason)

        # Resume the screen stream we stopped before making the request.
        try:
            await self._start_streaming(announcement=None)
        except Exception as exc:
            self.message_callback(
                {"type": "error", "message": f"Unable to resume local stream: {exc}"}
            )

    async def _start_streaming(
        self,
        *,
        announcement: str | None,
        request_id: str | None = None,
    ) -> None:
        await self._stop_streaming()

        producer = CaptureProducer(
            self.fps,
            self.jpeg_quality,
            self.max_width,
        )
        try:
            await asyncio.to_thread(producer.start)
            first = await asyncio.to_thread(producer.get, 5.0)
            if first is None:
                if producer.error:
                    raise producer.error
                raise TimeoutError("No screen frame was captured")
        except Exception:
            await asyncio.to_thread(producer.stop)
            raise

        self._producer = producer
        self.role = "controlled"
        self._clear_outbound()
        info: dict[str, Any] = {
            "ok": True,
            "platform": platform_name(),
            "name": socket.gethostname(),
            "width": first.width,
            "height": first.height,
            "fps": self.fps,
            "stream_width": self.max_width,
            "supports_role_switch": True,
        }
        if request_id:
            info["request_id"] = request_id

        if announcement:
            await self._send_text(encode_message(announcement, **info))

        await self._send_bytes(first.jpeg)
        self._frame_task = asyncio.create_task(
            self._frame_sender(producer),
            name="peer-screen-output",
        )
        self._emit_role("controlled")
        self.status_callback("Remote peer is controlling this computer")

    async def _stop_streaming(self) -> None:
        task = self._frame_task
        self._frame_task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        producer = self._producer
        self._producer = None
        if producer:
            await asyncio.to_thread(producer.stop)

    async def _frame_sender(self, producer: CaptureProducer) -> None:
        while True:
            frame = await asyncio.to_thread(producer.get, 1.0)
            if frame is None:
                if producer.error:
                    raise producer.error
                continue
            if self.role != "controlled":
                return
            await self._send_bytes(frame.jpeg)

    def _handle_frame(self, frame: bytes) -> None:
        if self.role != "controller":
            return
        if self.frame_queue.full():
            try:
                self.frame_queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self.frame_queue.put_nowait(frame)
        except queue.Full:
            pass

    def _queue_outbound(self, message_type: str, payload: dict[str, Any]) -> None:
        if self._closed or self.role != "controller":
            return

        if message_type == "mouse_move":
            self._latest_mouse = (
                int(payload.get("x", 0)),
                int(payload.get("y", 0)),
            )
            self._send_wakeup.set()
            return

        if message_type == "mouse_button" and self._latest_mouse is not None:
            payload = dict(payload)
            payload.setdefault("x", self._latest_mouse[0])
            payload.setdefault("y", self._latest_mouse[1])

        raw = encode_message(message_type, **payload)
        try:
            self._send_queue.put_nowait(raw)
        except asyncio.QueueFull:
            self.status_callback("Input queue full; dropping one control event")
        self._send_wakeup.set()

    async def _outbound_sender(self) -> None:
        mouse_period = 1.0 / 60.0

        while True:
            try:
                raw = self._send_queue.get_nowait()
            except asyncio.QueueEmpty:
                raw = None

            if raw is not None:
                await self._send_text(raw)
                await asyncio.sleep(0)
                continue

            if self._latest_mouse is not None and self.role == "controller":
                x, y = self._latest_mouse
                self._latest_mouse = None
                await self._send_text(
                    encode_message("mouse_move", x=x, y=y)
                )
                await asyncio.sleep(mouse_period)
                continue

            self._send_wakeup.clear()
            if not self._send_queue.empty() or self._latest_mouse is not None:
                continue
            await self._send_wakeup.wait()

    async def _send_text(self, raw: str) -> None:
        async with self._send_lock:
            await self.websocket.send(raw)

    async def _send_bytes(self, raw: bytes) -> None:
        async with self._send_lock:
            await self.websocket.send(raw)

    def _clear_frames(self) -> None:
        try:
            while True:
                self.frame_queue.get_nowait()
        except queue.Empty:
            pass

    def _clear_outbound(self) -> None:
        self._latest_mouse = None
        try:
            while True:
                self._send_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

    def _emit_role(
        self,
        role: str,
        remote_message: dict[str, Any] | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "type": "session_role",
            "role": role,
            "remote_platform": self.remote_info.get("platform", "remote"),
            "remote_name": self.remote_info.get("name", "remote computer"),
        }

        source = remote_message or {}
        if role == "controller":
            for key in ("platform", "name", "width", "height", "fps", "stream_width"):
                if key in source:
                    event[f"remote_{key}" if key in {"platform", "name"} else key] = source[key]
            if "platform" in source:
                event["remote_platform"] = source["platform"]
            if "name" in source:
                event["remote_name"] = source["name"]

        self.message_callback(event)

    @staticmethod
    def _extract_remote_info(message: dict[str, Any]) -> dict[str, Any]:
        return {
            key: message[key]
            for key in ("platform", "name", "width", "height", "fps", "stream_width")
            if key in message
        }
