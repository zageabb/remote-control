from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class ProtocolError(ValueError):
    pass


def encode_message(message_type: str, **payload: Any) -> str:
    return json.dumps({"type": message_type, **payload}, separators=(",", ":"))


def decode_message(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError("Invalid JSON message") from exc
    if not isinstance(data, dict) or not isinstance(data.get("type"), str):
        raise ProtocolError("Message must be an object containing a string 'type'")
    return data


@dataclass(slots=True)
class RemoteGeometry:
    width: int
    height: int

    def map_from_view(self, x: int, y: int, view_width: int, view_height: int) -> tuple[int, int]:
        if view_width <= 0 or view_height <= 0:
            return 0, 0
        rx = round(x * self.width / view_width)
        ry = round(y * self.height / view_height)
        rx = min(max(rx, 0), max(self.width - 1, 0))
        ry = min(max(ry, 0), max(self.height - 1, 0))
        return rx, ry
