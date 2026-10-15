# GWProv Agent Working Traits

Every AI agent working in this repository should bring these perspectives to
its changes:

- **CLI and data-processing expert:** Treat terminal output as a user interface.
  Every command should have the same thoughtful design quality as `gwprov ps`:
  concise, scannable, readable at ordinary and narrow terminal widths, and
  explicit about state and next actions. Use Rich for adaptive tables, useful
  color, progress, hierarchy, and paging; use Rich-Argparse for help. Honor
  `NO_COLOR`, avoid raw ANSI codes, and provide a `--no-pager` path for pageable
  commands. Keep machine-readable output (especially JSON) structured, stable,
  and free of human-oriented decoration.
- **State and hardware steward:** Report observed device/process state faithfully.
  Distinguish running, halted, busy, and unknown; never turn missing visibility
  or symbols into a confident guess. Do not poll a target while an operation
  lease says its interface is busy or carries an interrupted-operation marker.
  Before any flow calls `GnW.start_gnwmanager()`, passively inspect the
  gnwmanager mailbox, DHCSR, and VTOR. Mark the lease as requiring recovery
  before entering the RAM service, and clear that marker only after verified
  return to application firmware. `gwprov device DEVICE recover` is the explicit
  transition out of an idle Recovery Mode stub.
- **Systems integrator:** Keep local hardware, remote gnwmanager, and GWemu
  workflows coherent while respecting the different transports and side
  effects each backend requires.
- **Evidence-minded engineer:** Prefer structured data and reproducible checks.
  Surface actionable errors in the normal output and reserve raw implementation
  detail for diagnostics, verbose output, or saved reports.

For text commands, lead with the result, group related items, use consistent
labels and state names, wrap long values, and avoid repeating generic guidance
for every row. Put details next to the item they explain. Preserve command exit
codes and JSON schemas when improving presentation.

Before finishing a CLI change, inspect its actual help/output at a normal
terminal width and a narrow width, and check that redirected output remains
clean and script-safe. Do not accept default `argparse` formatting or a plain
dump of internal structure as finished user-facing help. Help should explain
what commands do, group related operations, show useful examples, and point to
the next level of help. A command map should describe the real parser hierarchy;
any machine-facing representation used by shell completion must remain
deterministic and free of color or layout glyphs.

## Command scope and target selection

- Keep three scopes clear in the command tree: GWProv-wide inventory and
  configuration, all-device inventory through `gwprov device ls`, and operations on one explicitly named
  device or profile. Never store or infer a current device selection.
- Put device actions under `gwprov device DEVICE ACTION`; put profile actions
  under `gwprov profile PROFILE ACTION`. Commands that accept either resource
  use one positional target and accept `device:NAME` or `profile:NAME` prefixes.
  Reject ambiguous bare names instead of guessing.
- `gwprov show` is the concise overview; `adapters`, `device ls`, `project`,
  `profile`, and `media` are first-class top-level resource commands. Their
  `show CATEGORY` forms remain concise dashboard views. `gwprov ps` remains the
  detailed live-state view. Do not duplicate its technical columns in `device ls`.
- Keep SD-card registration and image operations under `gwprov media sdcard`
  (`add/list/remove/create/compose`). Preserve `sd` as a short subcommand alias
  rather than presenting a second SD concept in help or the tree.
- Register remote `gnwmanager serve` endpoints under `gwprov adapters`; one
  endpoint represents one device. Store per-user adapter configuration in the
  platform config directory and honor `GWPROV_CONFIG_DIR`.
- Device operations require an explicit device name or stable ID. Profile
  assignments remain metadata attached to a device, and profile deployment
  names both the profile and destination device. A registered SD-card path is
  an already-mounted folder/drive; registration never formats media. Applying
  its profile overlays files and retains extras.
- Shell completion must query local profile, device, and SD-card registries
  through script-friendly output modes. Device completion must not poll QMP or
  open a physical debug session.
- Keep device-interacting commands grouped under the explicit `device DEVICE`
  noun. Label remaining directory-driven firmware tasks
  (`retro-go`, `ofw`) as file workflows that are not yet device-scoped; keep
  project-catalog commands in their own group.
- Filesystem edits must make the target and side effect clear. FrogFS edits
  rebuild the image and update its declared size; LittleFS edits operate in
  place; SD edits operate in place. Filesystem creation/formatting must state
  the required size and type and explicitly confirm replacement of existing
  data.
- Keep profile-name workflows distinct from direct image-file workflows, but
  expose them through the same filesystem verbs where their behavior is the
  same. Do not retain superseded command spellings as compatibility aliases.

## Pre-reset hardware diagnosis

When firmware reports a watchdog reset, do not treat the later BSOD screen as
the original fault context: the reset has already destroyed that stack. If
firmware symbols expose an STM32 `WWDG_IRQHandler`, use
`dbg.arm_watchdog_breakpoints()` and `dbg.wait_watchdog()` before reproducing.
The handler-entry capture decodes the hardware-stacked interrupted PC/LR/xPSR
and resolves its source and unwind. Keep this generic across projects; do not
hard-code an application name or assume a WWDG capture proves the interrupted
routine caused the stall without timing/evidence. Fault handlers and watchdog
interrupts are different mechanisms and need separate reports.

## Interactive input and symbol-free tracing

- Keep `dbg.addr2line(address)` available in the Python console and advertise
  it in the console's startup help; address resolution should not require a
  separate shell command.
- For firmware without symbols, prefer DWT or remote-debug data watchpoints on
  known MMIO input registers, then use the captured PC/LR and disassembly to
  identify the read path. A watchpoint observes memory access, not a CPU
  register value.
- Keep input injection generic. Accept the firmware's explicit return-site
  address or symbol and button mask; do not bake in an application, firmware,
  button mapping, or pause-state variable. For scalar-returning input routines,
  offer release/press/release polls through the ABI return register and clearly
  report when another breakpoint, timeout, or fault interrupts the sequence.
