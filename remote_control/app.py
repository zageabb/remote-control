from __future__ import annotations

import io
import queue
import secrets
import tkinter as tk
from tkinter import messagebox, ttk

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
        self.frame_queue: queue.Queue[bytes] = queue.Queue(maxsize=2)
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.remote_geometry: RemoteGeometry | None = None
        self.remote_photo: ImageTk.PhotoImage | None = None
        self.remote_image_size = (1, 1)
        self.last_mouse_sent: tuple[int, int] | None = None

        self._build_ui()
        self.after(30, self._drain_ui_queue)
        self.after(30, self._refresh_frame)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(header, text="LAN Remote Control", font=("TkDefaultFont", 18, "bold")).pack(side="left")
        ttk.Label(header, text=f"v{__version__}").pack(side="right")

        notebook = ttk.Notebook(outer)
        notebook.grid(row=1, column=0, sticky="nsew")

        self.host_tab = ttk.Frame(notebook, padding=14)
        self.control_tab = ttk.Frame(notebook, padding=10)
        notebook.add(self.host_tab, text="Host this computer")
        notebook.add(self.control_tab, text="Control another computer")

        self._build_host_tab()
        self._build_control_tab()

        self.global_status = tk.StringVar(value="Ready")
        ttk.Label(outer, textvariable=self.global_status, relief="sunken", anchor="w").grid(
            row=2, column=0, sticky="ew", pady=(8, 0)
        )

    def _build_host_tab(self) -> None:
        frame = self.host_tab
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="This machine", font=("TkDefaultFont", 14, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 12)
        )

        self.host_ip = tk.StringVar(value=local_ip())
        self.host_port = tk.StringVar(value=str(DEFAULT_PORT))
        self.host_token = tk.StringVar(value=secrets.token_urlsafe(9))
        self.host_fps = tk.IntVar(value=12)
        self.host_quality = tk.IntVar(value=60)
        self.host_status = tk.StringVar(value="Stopped")

        labels = [
            ("LAN IP", self.host_ip),
            ("Port", self.host_port),
            ("Access token", self.host_token),
        ]
        for row, (label, variable) in enumerate(labels, start=1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
            entry = ttk.Entry(frame, textvariable=variable, width=42)
            entry.grid(row=row, column=1, sticky="ew", pady=5)
            if label == "LAN IP":
                entry.state(["readonly"])

        ttk.Button(frame, text="New token", command=self._new_token).grid(row=3, column=2, padx=(8, 0), pady=5)

        ttk.Label(frame, text="Frame rate").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Spinbox(frame, from_=2, to=30, textvariable=self.host_fps, width=8).grid(row=4, column=1, sticky="w", pady=5)

        ttk.Label(frame, text="JPEG quality").grid(row=5, column=0, sticky="w", pady=5)
        ttk.Spinbox(frame, from_=25, to=90, textvariable=self.host_quality, width=8).grid(row=5, column=1, sticky="w", pady=5)

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=3, sticky="w", pady=(16, 8))
        self.start_host_btn = ttk.Button(buttons, text="Start hosting", command=self._start_host)
        self.start_host_btn.pack(side="left")
        self.stop_host_btn = ttk.Button(buttons, text="Stop", command=self._stop_host, state="disabled")
        self.stop_host_btn.pack(side="left", padx=(8, 0))

        ttk.Separator(frame).grid(row=7, column=0, columnspan=3, sticky="ew", pady=12)
        ttk.Label(frame, text="Status:").grid(row=8, column=0, sticky="nw")
        ttk.Label(frame, textvariable=self.host_status, wraplength=700, justify="left").grid(
            row=8, column=1, columnspan=2, sticky="w"
        )

        help_text = (
            "Run this same app on the other computer, open 'Control another computer', "
            "enter this LAN IP, port and access token, then connect. macOS hosting requires "
            "Screen Recording and Accessibility permission."
        )
        ttk.Label(frame, text=help_text, wraplength=760, justify="left").grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(18, 0)
        )

    def _build_control_tab(self) -> None:
        frame = self.control_tab
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(1, weight=1)

        self.connect_host = tk.StringVar(value="")
        self.connect_port = tk.StringVar(value=str(DEFAULT_PORT))
        self.connect_token = tk.StringVar(value="")
        self.client_status = tk.StringVar(value="Not connected")

        ttk.Label(toolbar, text="Host").grid(row=0, column=0, padx=(0, 4))
        ttk.Entry(toolbar, textvariable=self.connect_host, width=18).grid(row=0, column=1, sticky="ew", padx=(0, 8))
        ttk.Label(toolbar, text="Port").grid(row=0, column=2, padx=(0, 4))
        ttk.Entry(toolbar, textvariable=self.connect_port, width=7).grid(row=0, column=3, padx=(0, 8))
        ttk.Label(toolbar, text="Token").grid(row=0, column=4, padx=(0, 4))
        ttk.Entry(toolbar, textvariable=self.connect_token, width=20, show="•").grid(row=0, column=5, padx=(0, 8))
        self.connect_btn = ttk.Button(toolbar, text="Connect", command=self._connect)
        self.connect_btn.grid(row=0, column=6)
        self.disconnect_btn = ttk.Button(toolbar, text="Disconnect", command=self._disconnect, state="disabled")
        self.disconnect_btn.grid(row=0, column=7, padx=(8, 0))

        viewer_border = ttk.Frame(frame, relief="sunken", borderwidth=1)
        viewer_border.grid(row=1, column=0, sticky="nsew")
        viewer_border.rowconfigure(0, weight=1)
        viewer_border.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(viewer_border, background="black", highlightthickness=0, takefocus=True)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.create_text(20, 20, anchor="nw", fill="white", text="Connect to a host to view its screen.", tags="placeholder")

        self.canvas.bind("<Configure>", lambda _e: self._redraw_last_frame())
        self.canvas.bind("<Motion>", self._mouse_move)
        self.canvas.bind("<ButtonPress-1>", lambda e: self._mouse_button(e, "left", True))
        self.canvas.bind("<ButtonRelease-1>", lambda e: self._mouse_button(e, "left", False))
        self.canvas.bind("<ButtonPress-2>", lambda e: self._mouse_button(e, "middle", True))
        self.canvas.bind("<ButtonRelease-2>", lambda e: self._mouse_button(e, "middle", False))
        self.canvas.bind("<ButtonPress-3>", lambda e: self._mouse_button(e, "right", True))
        self.canvas.bind("<ButtonRelease-3>", lambda e: self._mouse_button(e, "right", False))
        self.canvas.bind("<MouseWheel>", self._mouse_wheel)
        self.canvas.bind("<Button-4>", lambda _e: self._send_scroll(1))
        self.canvas.bind("<Button-5>", lambda _e: self._send_scroll(-1))
        self.canvas.bind("<KeyPress>", self._key_down)
        self.canvas.bind("<KeyRelease>", self._key_up)

        footer = ttk.Frame(frame)
        footer.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(footer, textvariable=self.client_status).pack(side="left")
        ttk.Button(footer, text="Send clipboard", command=self._send_clipboard).pack(side="right")
        ttk.Button(footer, text="Get clipboard", command=self._get_clipboard).pack(side="right", padx=(0, 8))

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
                status_callback=lambda message: self.ui_queue.put(("host_status", message)),
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
        self.start_host_btn.config(state="normal")
        self.stop_host_btn.config(state="disabled")
        self.host_status.set("Stopped")
        self.global_status.set("Ready")

    def _connect(self) -> None:
        host = self.connect_host.get().strip()
        token = self.connect_token.get().strip()
        if not host or not token:
            messagebox.showwarning("Missing connection details", "Enter the host IP/name and access token.")
            return
        try:
            port = int(self.connect_port.get())
            self.client_service = RemoteClient(
                host=host,
                port=port,
                token=token,
                frame_queue=self.frame_queue,
                message_callback=lambda message: self.ui_queue.put(("client_message", message)),
                status_callback=lambda message: self.ui_queue.put(("client_status", message)),
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
        if self.client_service:
            self.client_service.stop()
            self.client_service = None
        self.remote_geometry = None
        self.connect_btn.config(state="normal")
        self.disconnect_btn.config(state="disabled")
        self.client_status.set("Not connected")
        self.canvas.delete("remote_frame")
        self.canvas.itemconfigure("placeholder", state="normal")

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
                elif kind == "client_message":
                    self._handle_client_message(payload)
        except queue.Empty:
            pass
        self.after(30, self._drain_ui_queue)

    def _handle_client_message(self, message: object) -> None:
        if not isinstance(message, dict):
            return
        if message.get("type") == "hello":
            self.remote_geometry = RemoteGeometry(int(message["width"]), int(message["height"]))
            self.client_status.set(
                f"Connected • {message.get('platform', 'remote')} • {message['width']}×{message['height']}"
            )
            self.canvas.itemconfigure("placeholder", state="hidden")
        elif message.get("type") == "clipboard":
            text = str(message.get("text", ""))
            self.clipboard_clear()
            self.clipboard_append(text)
            self.global_status.set("Remote clipboard copied locally")
        elif message.get("type") == "error":
            self.global_status.set(str(message.get("message", "Remote error")))

    def _refresh_frame(self) -> None:
        latest: bytes | None = None
        try:
            while True:
                latest = self.frame_queue.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            self._display_frame(latest)
        self.after(30, self._refresh_frame)

    def _display_frame(self, jpeg: bytes) -> None:
        try:
            image = Image.open(io.BytesIO(jpeg)).convert("RGB")
            self._last_frame = image
            self._draw_image(image)
        except Exception:
            return

    def _redraw_last_frame(self) -> None:
        image = getattr(self, "_last_frame", None)
        if image is not None:
            self._draw_image(image)

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
        self.canvas.create_image(x, y, anchor="nw", image=self.remote_photo, tags="remote_frame")
        self.canvas.tag_lower("remote_frame")

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
        coords = self._view_coordinates(event)
        if coords and coords != self.last_mouse_sent and self.client_service:
            self.last_mouse_sent = coords
            self.client_service.send("mouse_move", x=coords[0], y=coords[1])

    def _mouse_button(self, event: tk.Event, button: str, down: bool) -> None:
        self.canvas.focus_set()
        coords = self._view_coordinates(event)
        if coords and self.client_service:
            self.client_service.send("mouse_move", x=coords[0], y=coords[1])
            self.client_service.send("mouse_button", button=button, down=down)

    def _mouse_wheel(self, event: tk.Event) -> None:
        amount = 1 if event.delta > 0 else -1
        if abs(event.delta) >= 120:
            amount = int(event.delta / 120)
        self._send_scroll(amount)

    def _send_scroll(self, amount: int) -> None:
        if self.client_service:
            self.client_service.send("mouse_scroll", amount=amount)

    def _key_down(self, event: tk.Event) -> str | None:
        key = normalise_key(event.keysym, event.char)
        if key and self.client_service:
            self.client_service.send("key", key=key, down=True)
            return "break"
        return None

    def _key_up(self, event: tk.Event) -> str | None:
        key = normalise_key(event.keysym, event.char)
        if key and self.client_service:
            self.client_service.send("key", key=key, down=False)
            return "break"
        return None

    def _send_clipboard(self) -> None:
        if not self.client_service:
            return
        try:
            text = self.clipboard_get()
        except tk.TclError:
            text = ""
        self.client_service.send("clipboard_set", text=text)
        self.global_status.set("Local clipboard sent to remote")

    def _get_clipboard(self) -> None:
        if self.client_service:
            self.client_service.send("clipboard_get")

    def _on_close(self) -> None:
        try:
            self._disconnect()
            self._stop_host()
        finally:
            self.destroy()


def main() -> None:
    app = RemoteControlApp()
    app.mainloop()


if __name__ == "__main__":
    main()
