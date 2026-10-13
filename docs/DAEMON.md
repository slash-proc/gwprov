# GWProv local daemon

GWProv starts one per-user service on demand when a CLI command needs device
state or a managed GWemu instance. The daemon owns managed GWemu child processes
and their QMP-over-stdio streams. It also refreshes local probe and configured
remote-adapter inventory, keeps device connections while it owns them, and
polls attached devices every five seconds. CLI commands from any terminal use
its cached state or ask for a live refresh.

## Discovery and startup

Clients connect to a deterministic per-user endpoint; they do not scan process
tables or select a port:

| Platform | Endpoint |
| --- | --- |
| Linux | `$XDG_RUNTIME_DIR/gwprov/daemon.sock`, falling back to `~/.cache/gwprov/run/daemon.sock` |
| macOS | `~/Library/Caches/gwprov/run/daemon.sock` |
| Windows | `\\.\pipe\gwprov-<user-SID-hash>` |

Commands that need the daemon first try that endpoint. If it is absent, they
serialize startup with a per-user lock, launch the service in the background,
register configured adapters, and wait for its protocol handshake. `gwprov ps`
starts the daemon when needed and returns its current device snapshot; `--live`
waits for an immediate poll and internal-flash scan. Background polls refresh
runtime state every five seconds and rescan bank contents at least every 90
seconds; GWProv hardware sessions invalidate the bank cache when they release
their lease.

CLI and scripted requests reset a ten-minute idle timer. Periodic device polls
and registered devices do not. Once no managed GWemu instance or substantial
hardware operation blocks shutdown, the daemon exits after ten minutes without
client activity. The next CLI command starts it again.

## Local authentication and protocol

Linux and macOS use a Unix-domain socket inside a private per-user directory;
the socket is mode `0600`, and Linux peer credentials are checked against the
daemon UID. Windows uses a named pipe whose explicit DACL grants access to the
current user's SID and SYSTEM, rejects remote clients, and verifies the
connecting process token SID. The pipe name is an address, not a secret.

Each request includes protocol version 1, and each response repeats the
version. The OS authenticates the local peer; there is no password stored in
the CLI and no loopback TCP QMP endpoint. Requests use structured JSON and a
fixed set of GWProv operations. The QMP bridge allows status, screenshots,
execution controls, input events, and bounded register/memory reads. It does
not expose arbitrary QEMU monitor commands.

The daemon's `ping` response reports its package version, source directory,
and SHA-256 of the Python source files captured when the process started. The
daemon also checks that identity every five seconds. If the installed source
changes, it stops accepting new managed VM and target sessions and drains
automatically while passive device polling continues. A stale daemon that owns
a managed GWemu waits for that instance to stop. Substantial hardware
operations can finish normally, or be canceled with
`gwprov daemon restart --force`. The next CLI request starts the current daemon
after the old process exits. Older daemons that predate self-monitoring are
detected by `ping`; a client replaces them when no managed VM, child process, or
substantial operation lease prevents shutdown.

The source identity includes Python files throughout the package, including
subpackages. An idle stale daemon exits on its own; a RAM-flasher startup holds
a process-visible operation lease, so draining waits until that transition
ends. For an older or unresponsive daemon, GWProv verifies the process and its
children before replacing it.

This boundary separates different OS users. It does not protect a user's
session from malicious software running as that same user, which already has
the user's filesystem and process permissions.

## Managed GWemu

`gwprov gwemu start`, `gwprov gwemu run` (profile or raw image), and
`gwprov gwemu debug` launch GWemu through the daemon. QEMU receives `-qmp stdio`;
its QMP stream is held only by the daemon. Existing `ps`, screenshot, profiling, diagnosis,
pause, resume, stop, and Python debugger flows use the daemon handle reported
in `qmpSocket` instead of opening a QEMU QMP socket. GDB remains a separate
loopback endpoint when requested.

`gwprov gwemu start` returns after the daemon has launched and queried the VM.
Use `gwprov gwemu ps` in any terminal to inspect its actual running or halted
state. `gwprov gwemu stop` requests a graceful QMP quit and waits for the child.
Debugger launches that need reset-time breakpoints request an initial halt from
the daemon; ordinary launches start running.

Managed GDB listeners are loopback-only. When a command does not provide
`--gdb-port`, GWProv starts at the conventional base of 1234 plus one (1235)
and walks upward until the OS reports a free port. The lower-level direct
GWemu target uses the same rule for GDB and for its TCP QMP listener (base
4444, first automatic candidate 4445); both ports are kept distinct.

GWemu processes started outside these managed CLI commands remain external
instances. GWProv can inventory them when process visibility and their existing
QMP endpoint permit it, but cannot adopt a process whose QMP stream it does not
own. The explicit `gwprov gwemu run --stdio-gdb` harness mode remains a direct
launch because GDB and QMP cannot both consume the same QEMU stdin/stdout pipe.

## Hardware sessions

The daemon owns each selected PyOCD or remote gnwmanager connection while it is
polling a device. Local PyOCD and registered remote `gwprov debug python`
sessions use a daemon proxy: the daemon keeps the same adapter connection,
serializes debugger calls with its five-second state poll, and continues
refreshing running and installed firmware between debugger commands. The
session remains `BUSY` in `gwprov ps`, and an independent heartbeat keeps an
idle interactive console alive.

Standalone device operations take the process-shared target lease and hand the
monitor connection off before connecting directly to the adapter. The daemon
skips target polling during writes, erases, programming, and reset transitions;
it resumes polling when that lease ends. It also watches for an external
gnwmanager, OpenOCD, or PyOCD process and closes its monitor connection so that
tool can acquire the adapter. The device row remains in the daemon inventory
with an `externalSession` record containing the process, PID, command, adapter,
and handoff time. The last live firmware observation is retained separately as
`lastObserved`; the current status becomes `unknown` while target reads are
suspended. When the process exits, the daemon clears that record and performs a
fresh live poll. Explicit OpenOCD debug sessions that cannot use the shared
PyOCD connection also hold a lease and pause the monitor for the session.

The CLI sends lease heartbeats on a separate thread every two seconds, so a
long flash write does not delay them. Releasing the lease returns the device
to daemon polling. If the owner process disappears, the operating-system lease
is released and the daemon resumes polling. If heartbeats stop for twelve
seconds while the owner process is still alive, the daemon requests
cancellation, then force-kills an unresponsive owner after a short grace
period. Monitor handoffs carry per-process tokens, so overlapping clients do
not resume polling until the last handoff ends; abandoned handoffs expire if a
client dies before acquiring its lease. BUSY is cleared on lease release while
the daemon schedules a fresh state read. An explicit `gwprov daemon restart --force` requests immediate
cancellation of substantial device operations before restart. Failed or
interrupted writes are surfaced as operation failures; the daemon does not
assume the device write completed.

The daemon does not persist a host-side recovery or interrupted-operation
marker. After an operation ends, the next poll reads VTOR, DHCSR, mailbox state,
and firmware signatures from the target and reports what is actually present.
An idle RAM flasher is therefore observable through the same live polling path.

For coordination with tools that already use gnwmanager, run `gnwmanager serve`
and register its `ws://.../gdb` endpoint. GWProv's WebSocket backend passes
target operations through the server. That server currently admits one client;
multi-client sharing would need to be added to gnwmanager's server rather than
being inferred from GWProv's local leases.

## Repeatable GWemu timing

Managed `gwemu start` and `gwemu run` accept `--timing-mode baseline`,
`--icount SHIFT`, and `--rtc-epoch UNIX_SECONDS`. For a deterministic experimental
cycle timeline and a fixed RTC seed:

```sh
gwprov gwemu start --profile NAME --headless --timing-mode baseline --rtc-epoch 1735689600
```

Baseline selects `-icount shift=0,align=off,sleep=off` and
`gnw-h7b0-soc.timing-mode=on`. It counts each retired instruction as one cycle
and drives DWT and virtual-time peripherals from the same live core-clock
rate. A nonzero `--icount` is rejected with baseline. The default mode preserves
ordinary GWemu timing; an explicit `--icount` in default mode uses QEMU's fixed
instruction-to-virtual-time shift without enabling the H7 timing baseline.

The current baseline does **not** model Cortex-M7 instruction latency, cache
misses, memory wait states, or ITCM/DTCM placement advantages. Its DWT values
are experimental instruction-based work measurements, not measured hardware
cycles, and cannot establish physical-device 60 fps. Firmware can disable or
reset DWT; retain the control register and raw values when interpreting it.
A GWemu build without the timing-mode property fails loudly during startup.

`gwprov gwemu ps --output json` records `timing.mode`, `icountShift`, `rtcEpoch`,
`cycleSemantics`, and `hardwareCycleAccurate` for daemon-managed instances.
The direct `--stdio-gdb` harness does not support the new timing-mode or RTC
controls; use the managed path for those controls. Profiles and their files
remain unchanged by these launch options.

The QMP profiler reads explicit progress symbols using their ELF object sizes
(1, 2, 4, or 8 bytes). Its raw observations and wrap-safe deltas therefore
preserve 64-bit cumulative work counters. Symbol maps without sizes retain
the historical 32-bit default. These are sampled values; programs should
publish coherent counters if concurrent multiword updates are possible.

For managed instances, `binaryIdentity` pins the actual executable path,
SHA-256, byte size, process arguments, and startup-reported GWemu version when
available. It is captured once after startup rather than hashing on every
`ps` poll. Retain this identity alongside workload and firmware/application
identities when comparing timing measurements.

Profiler `inline_functions` attributes saved PCs through DWARF inline chains.
`inline_attribution` records source coverage. This reuses saved samples without
additional device traffic; these percentages remain PC sample shares and must
not be described as exact routine cycles.

`--gwemu-bin PATH` selects an explicit executable for a managed start/run.
The daemon validates the path and checks the actual running SHA-256 against
the selected file. Its ping capabilities advertise support, so older running
daemons cannot silently ignore this option. Finish active VM/hardware sessions
before restarting an older daemon. On Linux, `binaryIdentity.identitySource`
is `live-process-inode`, hashing `/proc/PID/exe` instead of a mutable pathname.
Other systems verify the resolved executable file identity during the read.

`--timing-mode experimental-m7` opts into GWemu's provisional Cortex-M7 issue/dependency model (`cortex-m7-arm-cpu.x-gnw-m7-cycle-model=on`), with precise icount shift 0 and DWT timing enabled. This is a separate comparison cohort: fractional issue, limited dependency/latency rules and baseline fallback are modeled; cache, NOR/XIP and full load/store timing remain unvalidated. It is not calibrated hardware cycle timing. Unsupported binaries fail at launch; no test-only scaling knobs are exposed.


### Evolving experimental model capabilities

DWT availability and enabled timing mode are separate from hardware calibration.
`baseline` remains a deterministic retired-instruction timeline. A newer binary
must form its own immutable comparison cohort; record its executable SHA and
actual launch properties before comparing modeled results.

Static ELF inspection on 2026-10-10 observed executable SHA
`3de9bc88d780e53295bcbaddc9637aa660f103a7617fa61b5dbb6dc5d22f0433`
(146,780,440 bytes) exposing `x-gnw-m7-cycle-model` and
`x-gnw-m7-dcache-policy`. The corresponding source describes model v5 with
zero-cost 16 KiB / 32-byte-line / four-way AXI SRAM data-cache shadows:
`tags-no-replacement`, `round-robin-hypothesis`, `tree-plru-hypothesis`, and
`true-lru-hypothesis`. These diagnostic hypotheses do not charge cache miss
costs, provide a complete instruction-cache model, or establish calibrated
NOR/bus timing. Their presence is capability evidence, not a hardware timing
accuracy claim. Current GWProv exposes the timing mode; it does not yet expose
the optional cache-shadow policy selector.
