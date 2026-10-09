"""Reader and summarizer for the optional, target-neutral GWProv trace ABI."""
from __future__ import annotations

import struct

MAGIC = int.from_bytes(b"GWPT", "little")
VERSION = 1
HEADER = struct.Struct("<IHHHHIIIIII")
ENTRY = struct.Struct("<IIIIHH")
EVENT_ENTER = 1
EVENT_EXIT = 2
EVENT_MARK = 3
EVENT_FAULT = 4
MAX_CAPACITY = 65536


def _decode_header(raw: bytes) -> dict:
    if len(raw) < HEADER.size:
        raise ValueError("truncated GWProv trace header")
    (magic, version, header_bytes, entry_bytes, capacity, lock, low, high,
     dropped, counter_hz, flags) = HEADER.unpack_from(raw)
    if magic != MAGIC:
        raise ValueError(f"GWProv trace magic mismatch: 0x{magic:08x}")
    if version != VERSION:
        raise ValueError(f"unsupported GWProv trace ABI version {version}")
    if header_bytes < HEADER.size or entry_bytes < ENTRY.size:
        raise ValueError("invalid GWProv trace header/entry size")
    if not 1 <= capacity <= MAX_CAPACITY:
        raise ValueError(f"invalid GWProv trace capacity {capacity}")
    if lock & 1:
        raise RuntimeError("GWProv trace header is being updated")
    return {"version": version, "header_bytes": header_bytes,
            "entry_bytes": entry_bytes, "capacity": capacity,
            "sequence": (high << 32) | low, "dropped": dropped,
            "counter_hz": counter_hz, "flags": flags, "seqlock": lock}


def read_trace(read_memory, address: int, *, since_sequence: int | None = None,
               retries: int = 3) -> dict:
    """Read a consistent ring snapshot; never halts or controls the target."""
    if address < 0 or address > 0xFFFFFFFF:
        raise ValueError("invalid GWProv trace header address")
    for _ in range(retries):
        first_raw = read_memory(address, HEADER.size)
        first = _decode_header(first_raw)
        head = first["sequence"]
        ring_start = address + first["header_bytes"]
        earliest = max(0, head - first["capacity"])
        requested = earliest if since_sequence is None else max(earliest, since_sequence)
        start = min(requested, head)
        count = head - start
        index = start % first["capacity"]
        contiguous = min(count, first["capacity"] - index)
        record_size = first["entry_bytes"]
        raw = bytearray()
        if contiguous:
            raw.extend(read_memory(ring_start + index * record_size,
                                   contiguous * record_size))
        remaining = count - contiguous
        if remaining:
            raw.extend(read_memory(ring_start, remaining * record_size))
        second_raw = read_memory(address, HEADER.size)
        second = _decode_header(second_raw)
        if first["seqlock"] != second["seqlock"] or first["sequence"] != second["sequence"]:
            continue
        events = []
        missing = 0
        for index in range(count):
            chunk = raw[index * record_size:(index + 1) * record_size]
            seq_low, seq_high, cycles, pc, event, flags = ENTRY.unpack_from(chunk)
            sequence = (seq_high << 32) | seq_low
            expected = start + index
            if sequence != expected:
                missing += 1
                continue
            events.append({"sequence": sequence, "cycles": cycles, "pc": pc,
                           "event": event, "flags": flags})
        overflow = max(0, earliest - (earliest if since_sequence is None else since_sequence))
        return {**first, "events": events, "missing": missing,
                "overwritten_since_sequence": overflow}
    raise RuntimeError("GWProv trace ring changed during each read attempt")


def summarize(events: list[dict], symbol_name=None) -> dict:
    """Compute inclusive/exclusive routine cycles from entry/exit records."""
    stack = []
    totals: dict[int, dict] = {}
    unmatched_exits = 0
    for event in events:
        kind = event["event"]
        pc = event["pc"]
        if kind == EVENT_ENTER:
            stack.append({"pc": pc, "cycles": event["cycles"], "children": 0})
        elif kind == EVENT_EXIT:
            if not stack or stack[-1]["pc"] != pc:
                unmatched_exits += 1
                continue
            frame = stack.pop()
            elapsed = (event["cycles"] - frame["cycles"]) & 0xFFFFFFFF
            exclusive = max(0, elapsed - frame["children"])
            row = totals.setdefault(pc, {"pc": pc, "calls": 0,
                                         "inclusive_cycles": 0, "exclusive_cycles": 0})
            row["calls"] += 1
            row["inclusive_cycles"] += elapsed
            row["exclusive_cycles"] += exclusive
            if stack:
                stack[-1]["children"] = (stack[-1]["children"] + elapsed) & 0xFFFFFFFF
    functions = []
    for row in totals.values():
        if symbol_name:
            row["function"] = symbol_name(row["pc"])
        functions.append(row)
    functions.sort(key=lambda row: row["exclusive_cycles"], reverse=True)
    return {"functions": functions, "unclosed_entries": len(stack),
            "unmatched_exits": unmatched_exits}
