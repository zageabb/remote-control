from remote_control.protocol import RemoteGeometry, decode_message, encode_message
from remote_control.platform_io import normalise_key


def test_protocol_round_trip():
    raw = encode_message("mouse_move", x=12, y=34)
    assert decode_message(raw) == {"type": "mouse_move", "x": 12, "y": 34}


def test_geometry_mapping():
    geometry = RemoteGeometry(1920, 1080)
    assert geometry.map_from_view(640, 360, 1280, 720) == (960, 540)


def test_key_mapping():
    assert normalise_key("Return", "\r") == "enter"
    assert normalise_key("a", "a") == "a"
