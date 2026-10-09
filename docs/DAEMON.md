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
