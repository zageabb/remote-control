from __future__ import annotations

import io
import queue
import secrets
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from PIL import Image, ImageTk

from . import __version__
from .client import RemoteClient
from .host import RemoteHost
from .platform_io import local_ip, normalise_key
from .protocol import RemoteGeometry

DEFAULT_PORT = 8765


class RemoteControlApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"Remote Control v{__version__}")
        self.geometry("1180x760")
        self.minsize(900, 600)

        self.host_service: RemoteHost | None = None
        self.client_service: RemoteClient | None = None
        self.frame_queue: queue.Queue[bytes] = queue.Queue(maxsize=1)
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        self.active_source: str | None = None
        self.remote_geometry: RemoteGeometry | None = None
        self.remote_photo: ImageTk.PhotoImage | None = None
        self.remote_image_size = (1, 1)
        self.last_mouse_sent: tuple[int, int] | None = None
        self._last_frame: Image.Image | None = None

        self._build_ui()
        self.after(30, self._drain_ui_queue)
        self.after(15, self._refresh_frame)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(
            header,
            text="LAN Remote Control",
            font=("TkDefaultFont", 18, "bold"),
        ).pack(side="left")
        ttk.Label(header, text=f"v{__version__}").pack(side="right")

        self.notebook = ttk.Notebook(outer)
        self.notebook.grid(row=1, column=0, sticky="nsew")

        self.host_tab = ttk.Frame(self.notebook, padding=14)
        self.control_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.host_tab, text="Host this computer")
        self.notebook.add(self.control_tab, text="Control another computer")

        self._build_host_tab()
        self._build_control_tab()

        self.global_status = tk.StringVar(value="Ready")
        ttk.Label(
            outer,
            textvariable=self.global_status,
            relief="sunken",
            anchor="w",
        ).grid(row=2, column=0, sticky="ew", pady=(8, 0))

    def _build_host_tab(self) -> None:
        frame = self.host_tab
        frame.columnconfigure(1, weight=1)

        ttk.Label(
            frame,
            text="This machine",
            font=("TkDefaultFont", 14, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

        self.host_ip = tk.StringVar(value=local_ip())
        self.host_port = tk.StringVar(value=str(DEFAULT_PORT))
        self.host_token = tk.StringVar(value=secrets.token_urlsafe(9))
        self.host_fps = tk.IntVar(value=20)
        self.host_quality = tk.IntVar(value=50)
        self.host_status = tk.StringVar(value="Stopped")
        self.host_role = tk.StringVar(value="No peer connected")

        labels = [
            ("LAN IP", self.host_ip),
            ("Port", self.host_port),
            ("Access token", self.host_token),
        ]
        for row, (label, variable) in enumerate(labels, start=1):
            ttk.Label(frame, text=label).grid(
                row=row,
                column=0,
                sticky="w",
                padx=(0, 10),
                pady=5,
            )
            entry = ttk.Entry(frame, textvariable=variable, width=42)
            entry.grid(row=row, column=1, sticky="ew", pady=5)
            if label == "LAN IP":
                entry.state(["readonly"])

        ttk.Button(
            frame,
            text="New token",
            command=self._new_token,
        ).grid(row=3, column=2, padx=(8, 0), pady=5)

        ttk.Label(frame, text="Frame rate").grid(
            row=4, column=0, sticky="w", pady=5
        )
        ttk.Spinbox(
            frame,
            from_=2,
            to=30,
            textvariable=self.host_fps,
            width=8,
        ).grid(row=4, column=1, sticky="w", pady=5)

        ttk.Label(frame, text="JPEG quality").grid(
            row=5, column=0, sticky="w", pady=5
        )
        ttk.Spinbox(
            frame,
            from_=25,
            to=90,
            textvariable=self.host_quality,
            width=8,
        ).grid(row=5, column=1, sticky="w", pady=5)

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=3, sticky="w", pady=(16, 8))

        self.start_host_btn = ttk.Button(
            buttons,
            text="Start hosting",
            command=self._start_host,
        )
        self.start_host_btn.pack(side="left")

        self.stop_host_btn = ttk.Button(
            buttons,
            text="Stop",
            command=self._stop_host,
            state="disabled",
        )
        self.stop_host_btn.pack(side="left", padx=(8, 0))

        self.host_take_control_btn = ttk.Button(
            buttons,
            text="Take control of connected computer",
            command=lambda: self._request_control("host"),
            state="disabled",
        )
        self.host_take_control_btn.pack(side="left", padx=(18, 0))

        ttk.Separator(frame).grid(
            row=7,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=12,
        )

        ttk.Label(frame, text="Connection:").grid(row=8, column=0, sticky="nw")
        ttk.Label(
            frame,
            textvariable=self.host_status,
            wraplength=760,
            justify="left",
        ).grid(row=8, column=1, columnspan=2, sticky="w")

        ttk.Label(frame, text="Control direction:").grid(
            row=9, column=0, sticky="nw", pady=(8, 0)
        )
        ttk.Label(
            frame,
            textvariable=self.host_role,
            wraplength=760,
            justify="left",
        ).grid(row=9, column=1, columnspan=2, sticky="w", pady=(8, 0))

        help_text = (
            "For a locked-down office PC, start hosting on the Mac and make the "
            "Windows PC connect outbound to it. The WebSocket remains open. While "
            "Windows is controlling the Mac, press 'Take control of connected computer' "
            "here to reverse the screen and input direction without opening an inbound "
            "Windows firewall port."
        )
        ttk.Label(
            frame,
            text=help_text,
            wraplength=820,
            justify="left",
        ).grid(row=10, column=0, columnspan=3, sticky="w", pady=(18, 0))

    def _build_control_tab(self) -> None:
        frame = self.control_tab
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(1, weight=1)

        self.connect_host = tk.StringVar(value="")
        self.connect_port = tk.StringVar(value=str(DEFAULT_PORT))
        self.connect_token = tk.StringVar(value="")
        self.client_status = tk.StringVar(value="Not connected")
        self.control_direction = tk.StringVar(value="No active remote-control session")

        ttk.Label(toolbar, text="Host").grid(row=0, column=0, padx=(0, 4))
        ttk.Entry(
            toolbar,
            textvariable=self.connect_host,
            width=18,
        ).grid(row=0, column=1, sticky="ew", padx=(0, 8))

        ttk.Label(toolbar, text="Port").grid(row=0, column=2, padx=(0, 4))
        ttk.Entry(
            toolbar,
            textvariable=self.connect_port,
            width=7,
        ).grid(row=0, column=3, padx=(0, 8))

        ttk.Label(toolbar, text="Token").grid(row=0, column=4, padx=(0, 4))
        ttk.Entry(
            toolbar,
            textvariable=self.connect_token,
            width=20,
            show="•",
        ).grid(row=0, column=5, padx=(0, 8))

        self.connect_btn = ttk.Button(
            toolbar,
            text="Connect",
            command=self._connect,
        )
        self.connect_btn.grid(row=0, column=6)

        self.disconnect_btn = ttk.Button(
            toolbar,
            text="Disconnect",
            command=self._disconnect,
            state="disabled",
        )
        self.disconnect_btn.grid(row=0, column=7, padx=(8, 0))

        direction_bar = ttk.Frame(frame)
        direction_bar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(
            direction_bar,
            textvariable=self.control_direction,
            font=("TkDefaultFont", 10, "bold"),
        ).pack(side="left")

        self.take_control_btn = ttk.Button(
            direction_bar,
            text="Take control",
            command=self._request_control_active,
            state="disabled",
        )
        self.take_control_btn.pack(side="right")

        viewer_border = ttk.Frame(frame, relief="sunken", borderwidth=1)
        viewer_border.grid(row=2, column=0, sticky="nsew")
        viewer_border.rowconfigure(0, weight=1)
        viewer_border.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(
            viewer_border,
            background="black",
            highlightthickness=0,
            takefocus=True,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.create_text(
            20,
            20,
            anchor="nw",
            fill="white",
            text="Connect to a host to view its screen.",
            tags="placeholder",
        )

        self.canvas.bind("<Configure>", lambda _e: self._redraw_last_frame())
        self.canvas.bind("<Motion>", self._mouse_move)
        self.canvas.bind(
            "<ButtonPress-1>",
            lambda e: self._mouse_button(e, "left", True),
        )
        self.canvas.bind(
            "<ButtonRelease-1>",
            lambda e: self._mouse_button(e, "left", False),
        )
        self.canvas.bind(
            "<ButtonPress-2>",
            lambda e: self._mouse_button(e, "middle", True),
        )
        self.canvas.bind(
            "<ButtonRelease-2>",
            lambda e: self._mouse_button(e, "middle", False),
        )
        self.canvas.bind(
            "<ButtonPress-3>",
            lambda e: self._mouse_button(e, "right", True),
        )
        self.canvas.bind(
            "<ButtonRelease-3>",
            lambda e: self._mouse_button(e, "right", False),
        )
        self.canvas.bind("<MouseWheel>", self._mouse_wheel)
        self.canvas.bind("<Button-4>", lambda _e: self._send_scroll(1))
        self.canvas.bind("<Button-5>", lambda _e: self._send_scroll(-1))
        self.canvas.bind("<KeyPress>", self._key_down)
        self.canvas.bind("<KeyRelease>", self._key_up)

        footer = ttk.Frame(frame)
        footer.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(footer, textvariable=self.client_status).pack(side="left")

        ttk.Button(
            footer,
            text="Send clipboard",
            command=self._send_clipboard,
        ).pack(side="right")

        ttk.Button(
            footer,
            text="Get clipboard",
            command=self._get_clipboard,
        ).pack(side="right", padx=(0, 8))

    def _new_token(self) -> None:
        self.host_token.set(secrets.token_urlsafe(9))

    def _start_host(self) -> None:
        try:
            port = int(self.host_port.get())
            token = self.host_token.get().strip()
            if not token:
                raise ValueError("Access token cannot be blank")

            self.host_service = RemoteHost(
                bind="0.0.0.0",
                port=port,
                token=token,
                fps=self.host_fps.get(),
                jpeg_quality=self.host_quality.get(),
                frame_queue=self.frame_queue,
                message_callback=lambda message: self.ui_queue.put(
                    ("session_message", ("host", message))
                ),
                status_callback=lambda message: self.ui_queue.put(
                    ("host_status", message)
                ),
            )
            self.host_service.start()
        except Exception as exc:
            messagebox.showerror("Unable to start host", str(exc))
            self.host_service = None
            return

        self.start_host_btn.config(state="disabled")
        self.stop_host_btn.config(state="normal")
        self.host_status.set(f"Hosting at {self.host_ip.get()}:{port}")
        self.global_status.set("Hosting enabled")

    def _stop_host(self) -> None:
        if self.host_service:
            self.host_service.stop()
            self.host_service = None

        if self.active_source == "host":
            self._clear_remote_view("Host stopped")

        self.start_host_btn.config(state="normal")
        self.stop_host_btn.config(state="disabled")
        self.host_take_control_btn.config(state="disabled")
        self.host_status.set("Stopped")
        self.host_role.set("No peer connected")
        self.global_status.set("Ready")
        self._update_role_controls()

    def _connect(self) -> None:
        host = self.connect_host.get().strip()
        token = self.connect_token.get().strip()
        if not host or not token:
            messagebox.showwarning(
                "Missing connection details",
                "Enter the host IP/name and access token.",
            )
            return

        try:
            port = int(self.connect_port.get())
            self.client_service = RemoteClient(
                host=host,
                port=port,
                token=token,
                frame_queue=self.frame_queue,
                fps=self.host_fps.get(),
                jpeg_quality=self.host_quality.get(),
                message_callback=lambda message: self.ui_queue.put(
                    ("session_message", ("client", message))
                ),
                status_callback=lambda message: self.ui_queue.put(
                    ("client_status", message)
                ),
            )
            self.client_service.start()
        except Exception as exc:
            messagebox.showerror("Unable to connect", str(exc))
            self.client_service = None
            return

        self.connect_btn.config(state="disabled")
        self.disconnect_btn.config(state="normal")
        self.client_status.set("Connecting…")
        self.canvas.focus_set()

    def _disconnect(self) -> None:
        if self.client_service and (
            self.client_service.running or self.client_service.connected
        ):
            self.client_service.stop()
            self.client_service = None
        elif self.host_service and self.host_service.connected:
            self.host_service.disconnect_peer()

        self._clear_remote_view("Not connected")
        self.connect_btn.config(state="normal")
        self.disconnect_btn.config(state="disabled")
        self.client_status.set("Not connected")
        self.control_direction.set("No active remote-control session")
        self._update_role_controls()

    def _request_control(self, source: str) -> None:
        service = self._service_for_source(source)
        if not service or not service.connected or service.role != "controlled":
            return
        service.request_control()
        self.global_status.set("Requesting control over the existing connection…")

    def _request_control_active(self) -> None:
        for source in ("client", "host"):
            service = self._service_for_source(source)
            if service and service.connected and service.role == "controlled":
                self._request_control(source)
                return

    def _service_for_source(self, source: str):
        if source == "host":
            return self.host_service
        if source == "client":
            return self.client_service
        return None

    def _controller_service(self):
        if self.active_source:
            service = self._service_for_source(self.active_source)
            if service and service.connected and service.role == "controller":
                return service

        for source in ("client", "host"):
            service = self._service_for_source(source)
            if service and service.connected and service.role == "controller":
                self.active_source = source
                return service
        return None

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()

                if kind == "host_status":
                    self.host_status.set(str(payload))
                elif kind == "client_status":
                    text = str(payload)
                    self.client_status.set(text)
                    self.global_status.set(text)
                    if text == "Disconnected" or text.startswith("Client error:"):
                        self.connect_btn.config(state="normal")
                        self.disconnect_btn.config(state="disabled")
                elif kind == "session_message":
                    source, message = payload
                    self._handle_session_message(str(source), message)
        except queue.Empty:
            pass

        self.after(30, self._drain_ui_queue)

    def _handle_session_message(self, source: str, message: object) -> None:
        if not isinstance(message, dict):
            return

        kind = message.get("type")
        if kind == "session_role":
            self._handle_role_event(source, message)
        elif kind == "clipboard":
            text = str(message.get("text", ""))
            self.clipboard_clear()
            self.clipboard_append(text)
            self.global_status.set("Remote clipboard copied locally")
        elif kind == "error":
            self.global_status.set(str(message.get("message", "Remote error")))

    def _handle_role_event(self, source: str, message: dict[str, Any]) -> None:
        role = str(message.get("role", "disconnected"))
        remote_name = str(message.get("remote_name", "remote computer"))
        remote_platform = str(message.get("remote_platform", "remote"))

        if role == "controller":
            try:
                width = int(message["width"])
                height = int(message["height"])
            except (KeyError, TypeError, ValueError):
                self.global_status.set("Role switched, but remote geometry is missing")
                return

            self.active_source = source
            self.remote_geometry = RemoteGeometry(width, height)
            self.last_mouse_sent = None
            self._last_frame = None
            self._flush_frames()
            self._show_placeholder("Waiting for first frame…")

            direction = f"This computer → {remote_name} ({remote_platform})"
            self.control_direction.set(direction)
            self.client_status.set(
                f"Controlling {remote_name} • {width}×{height} • "
                f"{message.get('fps', '?')} fps"
            )
            self.global_status.set(f"Control direction: {direction}")

            if source == "host":
                self.host_role.set(f"This computer is controlling {remote_name}")
                self.notebook.select(self.control_tab)

        elif role == "controlled":
            if self.active_source == source:
                self.active_source = None
                self.remote_geometry = None
                self._last_frame = None
                self._flush_frames()

            direction = f"{remote_name} ({remote_platform}) → this computer"
            self.control_direction.set(direction)
            self.client_status.set(
                f"{remote_name} is controlling this computer over the existing connection"
            )
            self._show_placeholder(
                "Remote peer is controlling this computer.\n"
                "Press 'Take control' to reverse the same connection."
            )
            self.global_status.set(f"Control direction: {direction}")

            if source == "host":
                self.host_role.set(f"{remote_name} is controlling this computer")

        elif role == "requesting":
            self.control_direction.set("Switching control direction…")
            self.client_status.set("Requesting control of remote computer…")
            if source == "host":
                self.host_role.set("Requesting control of connected computer…")

        elif role == "disconnected":
            if self.active_source == source:
                self._clear_remote_view("Peer disconnected")
            if source == "host":
                self.host_role.set("No peer connected")
                self.host_take_control_btn.config(state="disabled")
            elif source == "client":
                self.connect_btn.config(state="normal")
                self.disconnect_btn.config(state="disabled")

        self._update_role_controls()

    def _update_role_controls(self) -> None:
        host_role = self.host_service.role if self.host_service else "disconnected"
        if self.host_service and self.host_service.connected and host_role == "controlled":
            self.host_take_control_btn.config(
                state="normal",
                text="Take control of connected computer",
            )
        elif self.host_service and self.host_service.connected and host_role == "requesting":
            self.host_take_control_btn.config(
                state="disabled",
                text="Requesting control…",
            )
        elif self.host_service and self.host_service.connected and host_role == "controller":
            self.host_take_control_btn.config(
                state="disabled",
                text="Controlling connected computer",
            )
        else:
            self.host_take_control_btn.config(
                state="disabled",
                text="Take control of connected computer",
            )

        controlled_service = None
        for source in ("client", "host"):
            service = self._service_for_source(source)
            if service and service.connected and service.role == "controlled":
                controlled_service = service
                break

        if controlled_service:
            self.take_control_btn.config(
                state="normal",
                text="Take control",
            )
        else:
            self.take_control_btn.config(
                state="disabled",
                text="Take control",
            )

        if (
            (self.client_service and self.client_service.connected)
            or (self.host_service and self.host_service.connected)
        ):
            self.disconnect_btn.config(state="normal")

    def _refresh_frame(self) -> None:
        latest: bytes | None = None
        try:
            while True:
                latest = self.frame_queue.get_nowait()
        except queue.Empty:
            pass

        if latest is not None and self._controller_service():
            self._display_frame(latest)

        self.after(15, self._refresh_frame)

    def _display_frame(self, jpeg: bytes) -> None:
        try:
            image = Image.open(io.BytesIO(jpeg)).convert("RGB")
            self._last_frame = image
            self._draw_image(image)
        except Exception:
            return

    def _redraw_last_frame(self) -> None:
        if self._last_frame is not None:
            self._draw_image(self._last_frame)

    def _draw_image(self, image: Image.Image) -> None:
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        ratio = min(cw / image.width, ch / image.height)
        width = max(1, round(image.width * ratio))
        height = max(1, round(image.height * ratio))

        resized = image.resize((width, height), Image.Resampling.BILINEAR)
        self.remote_photo = ImageTk.PhotoImage(resized)
        self.remote_image_size = (width, height)

        x = (cw - width) // 2
        y = (ch - height) // 2
        self.canvas.delete("remote_frame")
        self.canvas.create_image(
            x,
            y,
            anchor="nw",
            image=self.remote_photo,
            tags="remote_frame",
        )
        self.canvas.itemconfigure("placeholder", state="hidden")
        self.canvas.tag_lower("remote_frame")

    def _show_placeholder(self, text: str) -> None:
        self.canvas.delete("remote_frame")
        self.canvas.itemconfigure("placeholder", text=text, state="normal")

    def _clear_remote_view(self, text: str) -> None:
        self.active_source = None
        self.remote_geometry = None
        self.last_mouse_sent = None
        self._last_frame = None
        self._flush_frames()
        self._show_placeholder(text)

    def _flush_frames(self) -> None:
        try:
            while True:
                self.frame_queue.get_nowait()
        except queue.Empty:
            pass

    def _view_coordinates(self, event: tk.Event) -> tuple[int, int] | None:
        if not self.remote_geometry:
            return None

        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        iw, ih = self.remote_image_size
        ox = (cw - iw) // 2
        oy = (ch - ih) // 2

        x = int(event.x) - ox
        y = int(event.y) - oy
        if x < 0 or y < 0 or x >= iw or y >= ih:
            return None

        return self.remote_geometry.map_from_view(x, y, iw, ih)

    def _mouse_move(self, event: tk.Event) -> None:
        service = self._controller_service()
        coords = self._view_coordinates(event)
        if coords and coords != self.last_mouse_sent and service:
            self.last_mouse_sent = coords
            service.send("mouse_move", x=coords[0], y=coords[1])

    def _mouse_button(
        self,
        event: tk.Event,
        button: str,
        down: bool,
    ) -> None:
        self.canvas.focus_set()
        service = self._controller_service()
        coords = self._view_coordinates(event)
        if coords and service:
            service.send(
                "mouse_button",
                button=button,
                down=down,
                x=coords[0],
                y=coords[1],
            )

    def _mouse_wheel(self, event: tk.Event) -> None:
        amount = 1 if event.delta > 0 else -1
        if abs(event.delta) >= 120:
            amount = int(event.delta / 120)
        self._send_scroll(amount)

    def _send_scroll(self, amount: int) -> None:
        service = self._controller_service()
        if service:
            service.send("mouse_scroll", amount=amount)

    def _key_down(self, event: tk.Event) -> str | None:
        service = self._controller_service()
        key = normalise_key(event.keysym, event.char)
        if key and service:
            service.send("key", key=key, down=True)
            return "break"
        return None

    def _key_up(self, event: tk.Event) -> str | None:
        service = self._controller_service()
        key = normalise_key(event.keysym, event.char)
        if key and service:
            service.send("key", key=key, down=False)
            return "break"
        return None

    def _send_clipboard(self) -> None:
        service = self._controller_service()
        if not service:
            return

        try:
            text = self.clipboard_get()
        except tk.TclError:
            text = ""

        service.send("clipboard_set", text=text)
        self.global_status.set("Local clipboard sent to remote")

    def _get_clipboard(self) -> None:
        service = self._controller_service()
        if service:
            service.send("clipboard_get")

    def _on_close(self) -> None:
        try:
            if self.client_service:
                self.client_service.stop()
            if self.host_service:
                self.host_service.stop()
        finally:
            self.destroy()


def main() -> None:
    app = RemoteControlApp()
    app.mainloop()


if __name__ == "__main__":
    main()
