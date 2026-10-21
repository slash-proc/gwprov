#!/usr/bin/env python3
"""
Sentinel relocation for a mapped (XiP) sidecar.

A blob linked at a sentinel base holds absolute pointers into itself. Whoever
places it at a real address must add the delta to every such pointer. PICO-8's
pico8.ro (0xBEEF0000) was the first; gba.xip (0xDEC00000) is the same shape, and
a project declares its own base -- see "mapped"/"relocBase" in the distribution
manifest. The runtime equivalent is flash_relocate_cb_t (Core/Inc/gw_flash_alloc.h),
which does this per chunk while caching a file from SD.
"""
from __future__ import annotations

import struct

PICO8_CODE_BASE = 0xBEEF0000


def _thumb_mov_immediate(data: bytearray, offset: int, *, top: bool) -> tuple[int, int, int, int]:
    first, second = struct.unpack_from("<HH", data, offset)
    opcode = first & 0xFBF0
    expected = 0xF2C0 if top else 0xF240
    if opcode != expected:
        raise ValueError(f"unexpected Thumb MOV{'T' if top else 'W'} opcode at {offset:#x}")
    immediate = (
        ((first & 0x000F) << 12)
        | (((first >> 10) & 1) << 11)
        | (((second >> 12) & 7) << 8)
        | (second & 0x00FF)
    )
    return immediate, (second >> 8) & 0xF, first, second


def _set_thumb_mov_immediate(
    data: bytearray, offset: int, immediate: int, first: int, second: int
) -> None:
    first = (first & ~0x040F) | ((immediate >> 12) & 0x000F) | (((immediate >> 11) & 1) << 10)
    second = (second & ~0x70FF) | (((immediate >> 8) & 7) << 12) | (immediate & 0x00FF)
    struct.pack_into("<HH", data, offset, first, second)


def patch_mapped_bytes(ro: bytes, target_xip_addr: int, base: int) -> tuple[bytes, int]:
    """Relocate mapped XIP references, preferring an exact GWRL site table.

    GWRL v2 blobs end in ABS32 offsets, MOVW/MOVT instruction-offset pairs,
    and a 24-byte header (magic, version, link base, code size, and both site
    counts). The trailer remains attached so profile rebuilds can reverse a
    prior relocation and relocate again at the new FrogFS address.

    Blobs without a recognized trailer keep the historic aligned-word scan
    used by PICO-8 and other existing mapped artifacts.
    """
    data = bytearray(ro)
    header_size = struct.calcsize("<4sIIIII")
    if len(data) >= header_size:
        magic, version, link_base, code_size, abs_count, mov_count = struct.unpack_from(
            "<4sIIIII", data, len(data) - header_size
        )
        if magic == b"GWRL":
            if version != 2:
                raise ValueError(f"unsupported mapped relocation trailer version {version}")
            abs_size = abs_count * 4
            mov_size = mov_count * 8
            table_start = len(data) - header_size - abs_size - mov_size
            if table_start < 0 or code_size != table_start:
                raise ValueError("invalid GWRL relocation trailer size")
            if not code_size or link_base + code_size > 0x100000000:
                raise ValueError("invalid GWRL link range")
            abs_sites = [
                struct.unpack_from("<I", data, table_start + i * 4)[0]
                for i in range(abs_count)
            ]
            mov_table = table_start + abs_size
            mov_pairs = [
                struct.unpack_from("<II", data, mov_table + i * 8)
                for i in range(mov_count)
            ]
            if abs_sites != sorted(set(abs_sites)):
                raise ValueError("GWRL ABS32 sites must be sorted and unique")
            if mov_pairs != sorted(set(mov_pairs)):
                raise ValueError("GWRL MOVW/MOVT sites must be sorted and unique")
            if any(offset & 3 or offset + 4 > code_size for offset in abs_sites):
                raise ValueError("GWRL ABS32 site is unaligned or outside code")
            if any(
                low & 1 or high & 1 or low + 4 > code_size or high + 4 > code_size or low >= high
                for low, high in mov_pairs
            ):
                raise ValueError("GWRL MOVW/MOVT site is unaligned or outside code")

            delta = (target_xip_addr - base) & 0xFFFFFFFF
            upper = base + code_size
            for offset in abs_sites:
                value = struct.unpack_from("<I", data, offset)[0]
                masked = value & ~1
                if not base <= masked < upper:
                    raise ValueError(
                        f"GWRL ABS32 at {offset:#x} points outside {base:#010x} range"
                    )
                struct.pack_into("<I", data, offset, (value + delta) & 0xFFFFFFFF)
            for low, high in mov_pairs:
                low_value, low_reg, low_first, low_second = _thumb_mov_immediate(
                    data, low, top=False
                )
                high_value, high_reg, high_first, high_second = _thumb_mov_immediate(
                    data, high, top=True
                )
                if low_reg != high_reg:
                    raise ValueError(f"GWRL MOVW/MOVT register mismatch at {low:#x}/{high:#x}")
                value = (high_value << 16) | low_value
                masked = value & ~1
                if not base <= masked < upper:
                    raise ValueError(
                        f"GWRL MOVW/MOVT at {low:#x}/{high:#x} points outside {base:#010x} range"
                    )
                relocated = (value + delta) & 0xFFFFFFFF
                _set_thumb_mov_immediate(data, low, relocated & 0xFFFF, low_first, low_second)
                _set_thumb_mov_immediate(data, high, relocated >> 16, high_first, high_second)
            return bytes(data), len(abs_sites) + len(mov_pairs)

    # Legacy heuristic for artifacts that do not carry a GWRL relocation table.
    code_size = len(data)
    upper = base + code_size
    delta = (target_xip_addr - base) & 0xFFFFFFFF
    patched = 0
    for offset in range(0, (code_size // 4) * 4, 4):
        value = struct.unpack_from("<I", data, offset)[0]
        masked = value & ~1
        if base <= masked < upper:
            struct.pack_into("<I", data, offset, (value + delta) & 0xFFFFFFFF)
            patched += 1
    return bytes(data), patched


def patch_pico8_ro_bytes(ro: bytes, target_xip_addr: int) -> tuple[bytes, int]:
    """Back-compat wrapper: PICO-8's base is 0xBEEF0000."""
    return patch_mapped_bytes(ro, target_xip_addr, PICO8_CODE_BASE)
