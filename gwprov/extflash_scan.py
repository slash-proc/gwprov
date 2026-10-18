"""Recognize occupied regions in a raw Game & Watch external-flash image.

This mirrors the signature probes used by gnw-web-builder's extflash scanner so
profile assembly can keep stock assets, relocated firmware backups, and FAT
cache partitions below newly installed filesystems.
"""

from __future__ import annotations

import struct
from pathlib import Path

MIB = 1024 * 1024

_MARIO_INT = bytes((0x30, 0x13, 0x01, 0x20))
_ZELDA_INT = bytes((0x20, 0xB6, 0x01, 0x20))
_ZELDA_STOCK = bytes((0x3C, 0x13, 0x96, 0xC5, 0x79, 0x38, 0x71, 0xD6))
_ZELDA_PATCHED = bytes((0x22, 0x21, 0x23, 0x22, 0x22, 0x22, 0x22, 0x22))
_MARIO_STOCK = bytes((0xFE, 0x6E, 0xF8, 0x01, 0x30, 0x77, 0x2D, 0x3A))
_MARIO_PATCHED = bytes((0x78, 0xD8, 0xA9, 0x10, 0x8D, 0x00, 0x20, 0xA2))
_MARIO_PATCHED_GIF = b"GIF89a\x00\x01\xc0\x00"


def scan_extflash(path: str | Path, flash_size: int | None = None) -> list[dict]:
    """Return recognized partitions as ``{offset, size, type, fs?}`` rows."""
    path = Path(path)
    actual_size = path.stat().st_size
    size = actual_size if flash_size is None else flash_size
    if size <= 0 or actual_size < size:
        raise ValueError(f"external-flash image is shorter than its declared size: {path}")
    found: list[dict] = []
    seen: set[tuple[int, int]] = set()
    stream = path.open("rb")

    def read(offset: int, count: int) -> bytes:
        if offset < 0 or offset + count > size:
            return b""
        stream.seek(offset)
        return stream.read(count)

    def add(offset: int, length: int, kind: str, fs: str | None = None) -> None:
        if length <= 0 or offset < 0 or offset + length > size or (offset, length) in seen:
            return
        row = {"offset": offset, "size": length, "type": kind}
        if fs:
            row["fs"] = fs
        found.append(row)
        seen.add((offset, length))

    strides = (MIB, MIB // 2, MIB // 4, MIB // 8)
    for stride in strides:
        for offset in range(0, size, stride):
            if stride < MIB and offset % (stride * 2) == 0:
                continue
            data = read(offset, 512)
            if len(data) < 512:
                continue

            # FAT boot sector / partition start, as in gnw-web-builder's scanner.
            if data[510:512] == b"\x55\xaa" and data[0] in (0xEB, 0xE9):
                bps = struct.unpack_from("<H", data, 0x0B)[0]
                sectors = struct.unpack_from("<H", data, 0x13)[0] or struct.unpack_from("<I", data, 0x20)[0]
                if 512 <= bps <= 4096 and sectors:
                    add(offset, bps * sectors, "FAT", "fat")
                    continue

            if data[:4] == b"FROG":
                length = struct.unpack_from("<I", data, 8)[0]
                add(offset, length, "FrogFS", "frogfs")
                continue

            # A relocated bank-1 image is 128 KiB; the trailing flag distinguishes
            # an OFW dump from a patched image in the reference scanner.
            if data[:4] in (_MARIO_INT, _ZELDA_INT) and offset + 131072 <= size:
                tail = read(offset + 131068, 4)
                model = "Mario" if data[:4] == _MARIO_INT else "Zelda"
                patched = len(tail) == 4 and tail[3] != 0xFF
                add(offset, 131072, f"{model} {'Pat(Int)' if patched else 'OFW (Int)'}")
                continue

            if data.startswith(_ZELDA_STOCK) and offset + 4 * MIB <= size:
                add(offset, 4 * MIB, "Zelda OFW")
            elif data.startswith(_ZELDA_PATCHED) and offset >= 0x20000:
                add(offset - 0x20000, 4 * MIB, "Zelda Assets")
            elif data.startswith(_MARIO_STOCK) and offset + MIB <= size:
                add(offset, MIB, "Mario OFW")
            elif data.startswith(_MARIO_PATCHED) or data.startswith(_MARIO_PATCHED_GIF):
                if offset + MIB <= size:
                    add(offset, MIB, "Mario Assets")

            # LittleFS grows down from the top; locate its superblock anchors
            # during the same walk and derive the partition start from geometry.
            if data[8:16] == b"littlefs" and len(data) >= 32:
                version, block_size, block_count = struct.unpack_from("<III", data, 20)
                if version >> 16 == 2 and 128 <= block_size <= 8192 and block_count:
                    length = block_size * block_count
                    start = offset + block_size - length
                    if start >= 0 and start % 4096 == 0:
                        add(start, length, "LittleFS", "littlefs")

    # Match the web-builder's final near-end LittleFS probe (block zero is
    # normally the last block, with a few older/custom block sizes supported).
    for block_size in (4096, 8192, 2048, 1024, 512, 256, 128):
        offset = size - block_size
        data = read(offset, 32)
        if len(data) == 32 and data[8:16] == b"littlefs":
            version, bsize, count = struct.unpack_from("<III", data, 20)
            if version >> 16 == 2 and 128 <= bsize <= 8192 and count:
                length = bsize * count
                start = offset + bsize - length
                if start >= 0 and start % 4096 == 0:
                    add(start, length, "LittleFS", "littlefs")
                    break
    stream.close()
    return sorted(found, key=lambda part: (part["offset"], part["type"]))


def next_free_offset(
    partitions: list[dict], *, block_size: int, stock_model: str | None = None
) -> int:
    """Find the 1 MiB-aligned end of reserved partitions, excluding filesystems."""
    reserved_end = max(
        (part["offset"] + part["size"] for part in partitions if part.get("fs") not in {"frogfs", "littlefs"}),
        default=0,
    )
    model_floor = {"mario": MIB, "zelda": 4 * MIB}.get(stock_model or "", 0)
    reserved_end = max(reserved_end, model_floor)
    if block_size <= 0:
        raise ValueError("external-flash erase block size must be positive")
    alignment = max(block_size, MIB)
    return ((reserved_end + alignment - 1) // alignment) * alignment
