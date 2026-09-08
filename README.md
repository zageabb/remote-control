# Remote Control

A small cross-platform LAN remote-control prototype written in Python. The same Tkinter application can either **host this computer** or **control another computer**.

> This is deliberately a prototype, not an Internet-facing remote administration product. Use it only on networks and computers you control.

## Current features

- Single Tkinter GUI for host and controller modes.
- Windows and macOS can both act as the host or controller.
- JPEG screen streaming over WebSockets.
- Mouse movement, left/middle/right buttons and wheel.
- Keyboard forwarding.
- Clipboard send/get.
- Shared access-token authentication.
- Remote screen automatically scaled to fit the viewer.
- Host status and controller connection status.

## Requirements

- Python 3.11+ recommended.
- Tkinter (normally included with the standard Windows Python installer; on macOS use a Python distribution that includes Tk).
- Both computers on the same LAN for this first version.

## Install

```bash
python -m venv .venv
```

### Windows

```powershell
.venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

If Windows Firewall asks whether Python may accept connections, allow it on **Private networks** only.

### macOS

```bash
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

When using the Mac as the **host**, macOS will require permissions for the Python application/terminal that starts it:

- System Settings → Privacy & Security → **Screen Recording**
- System Settings → Privacy & Security → **Accessibility**

Restart the application after granting permissions if macOS requests it.

## Use

### On the computer being controlled

1. Open **Host this computer**.
2. Note the LAN IP, port and generated access token.
3. Click **Start hosting**.

### On the controlling computer

1. Open **Control another computer**.
2. Enter the host LAN IP, port and token.
3. Click **Connect**.
4. Click inside the remote screen before typing.

The same steps work in either direction, e.g. Mac → Windows or Windows → Mac.

## Architecture

```text
Tkinter Controller                        Tkinter Host
+-------------------+                    +-------------------+
| Remote screen     | <--- JPEG frames -| mss screen grab   |
| Mouse / keyboard  | --- JSON events ->| pyautogui         |
| Clipboard         | <---- JSON ------>| pyperclip         |
+-------------------+    WebSocket       +-------------------+
```

The screen stream and control messages share one authenticated WebSocket connection. Binary messages are JPEG frames; text messages are compact JSON control packets.

## Known prototype limitations

- No TLS; intended for trusted LAN use only.
- No Windows secure-desktop/UAC/login-screen control.
- macOS permissions must be granted manually.
- No H.264 hardware encoding yet, so JPEG streaming uses more bandwidth than VNC/RDP.
- Primary monitor only in v0.1.0.
- Keyboard mapping is intentionally basic; unusual international layouts may need additional key mappings.
- One controller per WebSocket connection; multiple simultaneous controllers are not a design target yet.

## Next sensible improvements

- LAN discovery using mDNS/Bonjour.
- Monitor selector.
- H.264/FFmpeg streaming.
- Full-screen controller mode.
- Saved known hosts.
- Optional TLS and certificate pairing.
- Better cross-platform keyboard translation.

## Version

`0.1.0` – first working Tkinter host/controller prototype.
