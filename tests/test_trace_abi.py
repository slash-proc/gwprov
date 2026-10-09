from gwprov.trace_abi import (ENTRY, HEADER, MAGIC, EVENT_ENTER, EVENT_EXIT,
                              read_trace, summarize)


def make_snapshot(capacity, events, *, sequence=None, seqlock=2):
    sequence = len(events) if sequence is None else sequence
    header = HEADER.pack(MAGIC, 1, HEADER.size, ENTRY.size, capacity, seqlock,
                         sequence & 0xFFFFFFFF, sequence >> 32, 0, 280_000_000, 1)
    ring = bytearray(capacity * ENTRY.size)
    for seq, cycles, pc, kind in events:
        entry = ENTRY.pack(seq & 0xFFFFFFFF, seq >> 32, cycles, pc, kind, 0)
        slot = seq % capacity
        ring[slot * ENTRY.size:(slot + 1) * ENTRY.size] = entry
    return header + ring


def test_trace_snapshot_and_wrap_safe_routine_cycles():
    memory = make_snapshot(4, [
        (0, 0xFFFFFFF0, 0x08001234, EVENT_ENTER),
        (1, 0x00000010, 0x08001234, EVENT_EXIT),
    ])
    base = 0x24000000
    result = read_trace(lambda addr, size: memory[addr - base:addr - base + size], base,
                        since_sequence=0)
    assert result["events"][0]["sequence"] == 0
    summary = summarize(result["events"], lambda pc: f"routine@{pc:x}")
    assert summary["functions"] == [{
        "pc": 0x08001234, "calls": 1, "inclusive_cycles": 32,
        "exclusive_cycles": 32, "function": "routine@8001234",
    }]


def test_trace_ring_reports_overwritten_events():
    memory = make_snapshot(2, [
        (3, 30, 0x100, EVENT_ENTER),
        (4, 40, 0x100, EVENT_EXIT),
    ], sequence=5)
    base = 0x20000000
    result = read_trace(lambda addr, size: memory[addr - base:addr - base + size], base,
                        since_sequence=0)
    assert [event["sequence"] for event in result["events"]] == [3, 4]
    assert result["overwritten_since_sequence"] == 3
