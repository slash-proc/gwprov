# GWProv local daemon

GWProv starts one per-user service on demand when a GWemu is launched through
the CLI or a hardware backend acquires a target lease. The daemon owns managed
GWemu child processes and their QMP-over-stdio streams. CLI commands from any
terminal send status, screenshot, and control requests to that same owner.
Hardware backend processes register and release their existing cross-process
target leases with the daemon so it can report active operations and stay alive
for the duration of a session.

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
and wait for its protocol handshake. `gwprov ps` does not start an idle daemon;
it queries one if present and still inventories external processes and probes.

The daemon exits after its last managed VM and active target lease are gone and
the short idle grace period expires. A lease is released when the backend closes
or its owning process exits. This lets one daemon serve several terminals while
work is active without leaving a permanent background service.

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

GWemu processes started outside these managed CLI commands remain external
instances. GWProv can inventory them when process visibility and their existing
QMP endpoint permit it, but cannot adopt a process whose QMP stream it does not
own. The explicit `gwprov gwemu run --stdio-gdb` harness mode remains a direct
launch because GDB and QMP cannot both consume the same QEMU stdin/stdout pipe.

## Hardware sessions

Hardware operations continue to use the selected PyOCD, OpenOCD, or
gnwmanager-backed backend. Their existing exclusive target leases register an
operation name, process ID, and target key with the daemon. `gwprov ps` uses
that lease information to report `busy` and skips concurrent target polling.
The daemon never polls a leased target itself. Remote gnwmanager servers remain
separate local sessions, each represented by its own lease.

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
