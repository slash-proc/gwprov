# GWProv trace ABI, version 1

The optional trace ABI lets a project expose a lock-free-to-read event ring in
SRAM. GWProv reads it through QMP or a debug probe without stopping the CPU,
then resolves recorded PCs with any matching ELF/map symbols. Projects can use
it for generic routine timing, application states, and fault/loop timelines.

## Exported symbols and layout

Export a global `gwprov_trace_header` in non-cacheable/coherent SRAM, followed
immediately by `capacity` entries. Header and entry fields are little-endian.
The current reader accepts a larger `header_bytes` or `entry_bytes` so future
versions can append fields without changing the prefix.

| Header offset | Type | Meaning |
| ---: | --- | --- |
| 0 | `u32` | magic `0x54505747` (`GWPT`) |
| 4 | `u16` | ABI version, currently `1` |
| 6 | `u16` | header bytes, at least 36 |
| 8 | `u16` | entry bytes, at least 20 |
| 10 | `u16` | ring capacity |
| 12 | `u32` | seqlock; odd during a write, even when stable |
| 16 | `u32` | next sequence, low word |
| 20 | `u32` | next sequence, high word |
| 24 | `u32` | cumulative overwritten entry count |
| 28 | `u32` | counter frequency in Hz, or zero if unknown |
| 32 | `u32` | flags; bit 0 means timestamps use enabled DWT CYCCNT |

Each 20-byte entry has `sequence_low:u32`, `sequence_high:u32`,
`cycles:u32`, `pc:u32`, `event:u16`, and `flags:u16`. The ring slot is
`sequence % capacity`. Event values are `1=enter`, `2=exit`, `3=marker`, and
`4=fault`. Enter/exit records carry the same routine PC. `cycles` is the raw
32-bit DWT cycle count; consumers compute wrap-safe deltas. Event sequence is
64-bit and monotonically increasing.

Writers serialize updates: increment the header seqlock to odd, write the
entry, advance the 64-bit next-sequence and overwritten count, then increment
the seqlock to even. The reader snapshots the header before and after the ring
read and retries if the sequence changed. Do not call tracing hooks from
concurrent interrupt contexts unless the project serializes those writers.

## App state and instrumentation

A project may additionally export `gwprov_application_state`, a writable,
NUL-terminated printable string in SRAM. Update it at meaningful state
transitions and clear or replace it when leaving that state. This lets `ps`
report custom game states without project-specific GWProv code.

Routine events are optional instrumentation and add execution cost. Keep the
trace build flag off for release builds, measure the overhead, and compare
instrumented and uninstrumented behavior. A trace overflow or a missing entry
is reported explicitly; it is never presented as a complete call history.
Probe-side PC sampling remains a separate fallback because reading core
registers may briefly halt the target. The ring reader itself does not halt,
reset, or resume the device.
