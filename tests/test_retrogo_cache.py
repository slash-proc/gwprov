import struct
import zlib
from types import SimpleNamespace

from gwprov.retrogo_cache import (
    CACHE_SLOTS,
    CACHE_VERSION,
    ERASE_BLOCK_SIZE,
    GWEMU_UID,
    METADATA_SIZE,
    _empty_metadata_blob,
    _file_crc32,
    _fatfs_entry_name,
    _metadata_blob,
    _relocate_xip_chunk,
    _autostart_xip_paths,
    _validated_extflash_layout,
)


def test_autostart_cache_metadata_matches_firmware_layout():
    key, address, size, base = 0x12345678, 0x90001000, 0x2345, 0x90001000
    blob = _metadata_blob(entries=[(key, address, size)], base=base)

    assert len(blob) == METADATA_SIZE == 4124
    assert struct.unpack_from("<I3I", blob, 0) == (CACHE_VERSION, *GWEMU_UID)
    assert struct.unpack_from("<IIIB3x", blob, 16) == (key, address, size, 1)
    assert blob[32 : 16 + CACHE_SLOTS * 16] == bytes(16 * (CACHE_SLOTS - 1))
    assert struct.unpack_from("<IIH", blob, 4112) == (
        (address + size + ERASE_BLOCK_SIZE - 1) & ~(ERASE_BLOCK_SIZE - 1),
        base,
        0,
    )


def test_autostart_cache_metadata_records_multiple_sidecars_in_order():
    entries = [
        (0x11111111, 0x90004000, 47_109),
        (0x22222222, 0x90010000, 31_084_812),
        (0x33333333, 0x91DB6000, 1_746_884),
    ]
    blob = _metadata_blob(entries=entries, base=0x90004000)

    assert [struct.unpack_from("<IIIB3x", blob, 16 + 16 * i) for i in range(3)] == [
        (*entry, 1) for entry in entries
    ]
    assert struct.unpack_from("<IIH", blob, 4112) == (
        (entries[-1][1] + entries[-1][2] + ERASE_BLOCK_SIZE - 1) & ~(ERASE_BLOCK_SIZE - 1),
        0x90004000,
        2,
    )


def test_xip_chunk_relocation_rebases_asset_and_code_references():
    relocations = {"asset_base": 0xBEEF0000, "asset_size": 0x1000}
    asset_source = {
        "kind": "assets",
        "size": 8,
        "link_base": 0xBEEF0000,
        "sites": (0xBEEF0000, 0xBEEF0004),
        "relocations": relocations,
    }
    asset = struct.pack("<II", 0xBEEF0005, 0xDEAD0000)
    relocated_asset = _relocate_xip_chunk(asset, 0, asset_source, 0x90004000)
    assert struct.unpack("<II", relocated_asset) == (0x90004005, 0xDEAD0000)

    code_source = {
        "kind": "code",
        "size": 12,
        "link_base": 0xDEC00000,
        "sites": (0xDEC00004,),
        "asset_address": 0x90004000,
        "relocations": {**relocations, "code_base": 0xDEC00000},
    }
    code = struct.pack("<III", 0xDEC00009, 0xBEEF0009, 0x12345678)
    relocated_code = _relocate_xip_chunk(code, 0, code_source, 0x90008000)
    assert struct.unpack("<III", relocated_code) == (0x90008009, 0x90004009, 0x12345678)


def test_smg_sidecar_discovery_includes_unmapped_raw_assets_file():
    provision = {
        "projects": {
            "smg": {
                "files": [
                    "homebrews/SMG.bin",
                    "homebrews/smg_code.xip",
                    "homebrews/smg_assets.xip",
                ],
                "mapped": [{"path": "homebrews/smg_code.xip", "relocBase": 0xDEC00000}],
            }
        }
    }

    project, paths = _autostart_xip_paths(provision, "/homebrews/SMG.bin")
    assert project is provision["projects"]["smg"]
    assert [item["path"] for item in paths] == [
        "homebrews/smg_code.xip",
        "homebrews/smg_assets.xip",
    ]
    assert paths[0]["relocBase"] == 0xDEC00000
    assert "relocBase" not in paths[1]


def test_autostart_cache_key_matches_firmware_crc_order():
    path, mtime, size = "/homebrews/demo.bin", 1_792_716_594, 47_109
    expected = zlib.crc32(path.encode("utf-8"))
    expected = zlib.crc32(struct.pack("<Q", mtime), expected)
    expected = zlib.crc32(struct.pack("<I", size), expected) & 0xFFFFFFFF

    assert _file_crc32(path, mtime, size) == expected


def test_autostart_cache_key_matches_stm32_64_bit_time_t():
    # Captured from the running Retro-Go firmware under GWemu with GDB:
    # path=/homebrews/smg_assets.xip, st_mtime=0x6adaaf32, size=0x1da510c.
    assert _file_crc32("/homebrews/smg_assets.xip", 0x6ADA_AF32, 0x01DA_510C) == 0xC3D8_EB4D


def test_fatfs_entry_name_applies_short_name_lowercase_flags():
    entry = SimpleNamespace(
        lfn_entry=None,
        ntres=0x18,
        get_short_name=lambda: "SMG_CODE.XIP",
    )

    assert _fatfs_entry_name(entry) == "smg_code.xip"


def test_fatfs_entry_name_preserves_long_name_spelling():
    entry = SimpleNamespace(
        lfn_entry=object(),
        ntres=0,
        get_short_name=lambda: "SMG_AS~1.XIP",
        get_long_name=lambda: "smg_assets.xip",
    )

    assert _fatfs_entry_name(entry) == "smg_assets.xip"


def _gw_layout_record(*, extflash_size=64 * 1024 * 1024, reserved=0x12345, flags=0xF):
    record = bytearray(36)
    record[:8] = b"GWLB\x02\x00\x24\x00"
    struct.pack_into("<6I", record, 8, 0, 0, extflash_size, reserved, 0, flags)
    struct.pack_into("<I", record, 32, zlib.crc32(record[:32]))
    return bytes(record)


def test_autostart_cache_requires_matching_firmware_reserved_boundary():
    reserved, capacity = _validated_extflash_layout(
        _gw_layout_record(),
        {"reservedOffset": 0x12345, "extflashBytes": 64 * 1024 * 1024},
    )
    assert (reserved, capacity) == (0x12345, 64 * 1024 * 1024)


def test_autostart_cache_reads_boundary_from_superblock_for_older_profiles():
    assert _validated_extflash_layout(_gw_layout_record(), {"extflashBytes": 64 * 1024 * 1024}) == (
        0x12345,
        64 * 1024 * 1024,
    )


def test_autostart_cache_rejects_mismatched_or_invalid_firmware_layout():
    import pytest

    with pytest.raises(ValueError, match="does not match the firmware GWLB record"):
        _validated_extflash_layout(
            _gw_layout_record(),
            {"reservedOffset": 0, "extflashBytes": 64 * 1024 * 1024},
        )
    corrupt = bytearray(_gw_layout_record())
    corrupt[20] ^= 0x01
    with pytest.raises(ValueError, match="no unique valid GWLB"):
        _validated_extflash_layout(
            bytes(corrupt),
            {"reservedOffset": 0x12345, "extflashBytes": 64 * 1024 * 1024},
        )


def test_empty_cache_index_invalidates_old_entries_before_flash_write():
    base = 0x90004000
    blob = _empty_metadata_blob(base)

    assert len(blob) == METADATA_SIZE
    assert struct.unpack_from("<I3I", blob, 0) == (CACHE_VERSION, *GWEMU_UID)
    assert blob[16:4112] == bytes(4096)
    assert struct.unpack_from("<IIH", blob, 4112) == (base, base, 0)


def test_exact_code_relocation_preserves_numeric_sentinel_collisions():
    base = 0xDEC00000
    source = {
        "kind": "code",
        "size": 12,
        "link_base": base,
        "sites": (base + 4,),
        "asset_address": 0x90004000,
        "relocations": {
            "asset_base": 0xBEEF0000,
            "asset_size": 0x1000,
            "code_sites": (base,),
        },
    }
    # The last word is numeric table data, even though it resembles a pointer.
    raw = struct.pack("<III", base + 9, 0xBEEF0009, base + 5)
    expected = struct.pack("<III", 0x90008009, 0x90004009, base + 5)
    assert _relocate_xip_chunk(raw, 0, source, 0x90008000) == expected
    # Independent chunks must produce the same result as one contiguous pass.
    assert (
        b"".join(
            _relocate_xip_chunk(raw[i : i + 4], i, source, 0x90008000)
            for i in range(0, len(raw), 4)
        )
        == expected
    )
