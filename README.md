# Remote Control

A small cross-platform LAN remote-control prototype written in Python. The same Tkinter application can either **host this computer** or **control another computer**.

> This is deliberately a prototype, not an Internet-facing remote administration product. Use it only on networks and computers you are authorised to control.

## Current features

- Single Tkinter GUI for host and controller modes.
- Windows and macOS can both act as the host or controller.
- JPEG screen streaming over WebSockets.
- Low-latency latest-frame buffering and coalesced mouse movement.
- Native DPI-aware Windows mouse/keyboard injection for ordinary desktop apps.
- Mouse movement, left/middle/right buttons and wheel.
- Keyboard forwarding.
- Clipboard send/get.
- Shared access-token authentication.
- Remote screen automatically scaled to fit the viewer.
- **Control direction can be reversed over the same existing WebSocket connection.**
- One listening host accepts one active peer at a time.

## Requirements

- Python 3.11+ recommended.
- Tkinter (normally included with the standard Windows Python installer; on macOS use a Python distribution that includes Tk).
- Both computers on the same LAN for this prototype.

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

If Windows Firewall asks whether Python may accept connections, allow it on **Private networks** only when Windows is being used as the listening host.

### macOS

```bash
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

When using the Mac as the machine being controlled, macOS requires permissions for the Python application/terminal that starts it:

- System Settings → Privacy & Security → **Screen Recording**
- System Settings → Privacy & Security → **Accessibility**

Restart the application after granting permissions if macOS requests it.

## Normal use

### On the computer being controlled first

1. Open **Host this computer**.
2. Note the LAN IP, port and generated access token.
3. Click **Start hosting**.

### On the controlling computer

1. Open **Control another computer**.
2. Enter the host LAN IP, port and token.
3. Click **Connect**.
4. Click inside the remote screen before typing.

## Locked-down Windows / office-firewall layout

If the Windows PC cannot accept inbound connections but it can make the outbound connection to the Mac, use this layout:

```text
Office Windows PC                    MacBook
(outbound connector)                 (listening host)
        |                                  |
        +========= WebSocket ==============+
                    full duplex
```

1. Start **Host this computer** on the Mac.
2. From Windows, open **Control another computer** and connect outbound to the Mac.
3. Initially Windows controls the Mac.
4. On the Mac, press **Take control of connected computer**.
5. The Mac stops its current screen stream, sends a `control_request` over the existing WebSocket, and Windows begins streaming its own desktop back over that same connection.
6. The Mac automatically switches to the controller view and can control Windows.
7. On Windows, press **Take control** to reverse the direction again.

No second connection is opened and Windows does not need to become a listening server for the role reversal. This does not override organisational firewall or remote-access policy; it only reuses an already-established permitted connection.

## Role-switch protocol

Exactly one machine is the controller at a time:

```text
Initial:
Windows controller  --->  Mac controlled

Mac presses Take control:
Mac stops old stream
Mac --- control_request ---> Windows
Windows starts capture
Windows --- control_granted + frames ---> Mac

Result:
Mac controller  --->  Windows controlled
```

The peer state is one of:

- `controller` – displays remote frames and sends input.
- `controlled` – streams the local screen and accepts remote input.
- `requesting` – local stream has stopped and the peer is waiting for the direction switch to complete.

WebSocket ordering is used so frames from the old direction are sent before the role-change request. This avoids mixing stale Mac frames with the new Windows stream.

## Architecture

```text
Peer A                                         Peer B
+-------------------------+                   +-------------------------+
| Tk viewer               |                   | Tk viewer               |
| Screen capture producer |<-- same socket -->| Screen capture producer |
| Mouse / keyboard input  |                   | Mouse / keyboard input  |
| Clipboard               |                   | Clipboard               |
+-------------------------+                   +-------------------------+

      controller / controlled roles can swap without reconnecting
```

Binary WebSocket messages are JPEG frames. Text messages are compact JSON packets for authentication, input, clipboard, status and role switching.

## Known prototype limitations

- No TLS; intended for trusted LAN use only.
- No Windows secure-desktop/UAC/login-screen control.
- macOS permissions must be granted manually.
- No H.264 hardware encoding yet, so JPEG streaming uses more bandwidth than VNC/RDP.
- Primary monitor only.
- Keyboard mapping is intentionally basic; unusual international layouts may need additional mappings.
- One active peer per listening host.
- Role-change requests are automatically accepted in v0.3.0; an approve/deny UI can be added later.

## Next sensible improvements

- Optional approval prompt for control-direction changes.
- LAN discovery using mDNS/Bonjour.
- Monitor selector.
- H.264/FFmpeg streaming.
- Full-screen controller mode.
- Saved known hosts.
- Optional TLS and certificate pairing.
- Better cross-platform keyboard translation.

## Version

`0.3.0` – persistent full-duplex peer session with reversible control over the existing connection.
