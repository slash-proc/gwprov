from gwprov.common.retrogo_config import (
    AUTOSTART_CFG_MARKER,
    AUTOSTART_MODE_OFF,
    AUTOSTART_MODE_ON,
    build,
    build_from_values,
)
from gwprov.device_config import decode_config


def _reserved(blob):
    values = decode_config(blob)
    return bytes.fromhex(values["values"].get("reserved_app_hex", ""))


def test_autostart_modes_are_written_to_reserved_config_bytes():
    enabled = _reserved(build(startup_file="/homebrews/demo.bin", autostart_mode=AUTOSTART_MODE_ON))
    disabled = _reserved(build(startup_file="", autostart_mode=AUTOSTART_MODE_OFF))

    assert enabled[:2] == bytes((AUTOSTART_CFG_MARKER, AUTOSTART_MODE_ON))
    assert disabled[:2] == bytes((AUTOSTART_CFG_MARKER, AUTOSTART_MODE_OFF))
    assert decode_config(
        build(startup_file="/homebrews/demo.bin", autostart_mode=AUTOSTART_MODE_ON)
    )["integrity"]["crc_valid"]


def test_read_modify_write_preserves_explicit_autostart_mode():
    original = build(startup_file="/homebrews/demo.bin", autostart_mode=AUTOSTART_MODE_ON)
    values = decode_config(original)["values"]

    updated = build_from_values(values, menu_timeout_s=0)

    assert _reserved(updated)[:2] == bytes((AUTOSTART_CFG_MARKER, AUTOSTART_MODE_ON))


def test_read_modify_write_can_enable_and_disable_autostart():
    values = decode_config(build())["values"]

    enabled = build_from_values(
        values,
        startup_file="/homebrews/demo.bin",
        autostart_mode=AUTOSTART_MODE_ON,
    )
    disabled = build_from_values(
        decode_config(enabled)["values"],
        startup_file="",
        autostart_mode=AUTOSTART_MODE_OFF,
    )

    assert _reserved(enabled)[:2] == bytes((AUTOSTART_CFG_MARKER, AUTOSTART_MODE_ON))
    assert _reserved(disabled)[:2] == bytes((AUTOSTART_CFG_MARKER, AUTOSTART_MODE_OFF))
    assert decode_config(disabled)["values"]["startup_file"] == ""
