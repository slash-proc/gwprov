# gwprov

`gwprov` is the Python provisioning and control library for Game & Watch Retro-Go images, GWemu, and physical devices. It is intended to be consumed as a Python package and as a Git submodule.

The initial package brings reusable target, SD-card, timeline, Retro-Go configuration, and build modules from Doug's own `../tinyemu` project into one package, alongside Retro-Go-SD's `REMOTE_INPUT` transport. Retro-Go build behavior is carried over as a compatibility wrapper; the build system itself remains owned by Retro-Go-SD.

## Install

Run from the gwprov checkout. Keep generated content, profiles and environments in
`dev-local/`, which is ignored by Git. The final launch requires `gwemu` on PATH;
a wrapper named `gwemu` on PATH is also supported.

On macOS, install Python and mtools with Homebrew first (`brew install python@3.11
mtools`). Apple's system Python is older than gwprov's supported Python version.
Create the environment with `python3.11 -m venv dev-local/venv` instead of the
`python3` command below. GWemu must also be available on PATH; gwprov does not
install or build the emulator.

```bash
cd /path/to/gwprov
python3 -m venv dev-local/venv
source dev-local/venv/bin/activate
python -m pip install -r requirements.txt

# Optional: use a local gnwmanager checkout while developing its bindings.
# python -m pip install -e ../gnwmanager

# Optional: enable Bash completion in this shell.
eval "$(gwprov completion bash)"

# In zsh, initialize completion first, then load gwprov's completion function.
# autoload -Uz compinit && compinit
# eval "$(gwprov completion zsh)"
```

`requirements.txt` installs the `dist` and `media` extras from `pyproject.toml`,
including Wasmtime and LittleFS support. gnwmanager is a package dependency;
gwprov reuses its release-download bindings. The Retro-Go source build wrapper
also needs the upstream toolchain and build dependencies; SD composition uses
mtools where applicable. The release workflow below does not compile Retro-Go.

## Complete release-to-profile workflow

These commands download the default `sylverb/game-and-watch-retro-go-sd` release,
install and convert projects, pack both flash filesystems, create a persistent
instance, and boot it. Change the input paths to match your local files. Choose a
new profile directory for each instance; `profile create` refuses to overwrite one.

### Download firmware and provision content

```bash
content=dev-local/content/retro-go-demo
profile=dev-local/profiles/retro-go-demo

gwprov retro-go install --variant flash --output "$content"

gwprov project install smw --variant flash --output "$content" \
  --input-dir "$HOME/Emulation/Roms/Super Nintendo"

gwprov project install zelda3 --variant flash --output "$content" \
  --input-dir "base=$HOME/Emulation/Roms/Super Nintendo" \
  --input-dir "language=$HOME/Emulation/Roms/Super Nintendo"

gwprov project install openlara --variant flash --output "$content" \
  --input-dir "/path/to/tomb-raider-cd/DATA"

# Includes the shipped shareware; no local WAD is required.
gwprov project install doom --variant flash --output "$content"

gwprov project install gba --variant flash --output "$content" \
  --firmware-dir "/path/to/firmware" \
  --game "gba=$HOME/Emulation/Roms/Game Boy Advance/Example Game.zip"
```

SMW requires its supported ROM. Zelda 3 selects the supported US base and available
German/French translation inputs by their declared hashes; absent translation
inputs produce no translated assets. To provide the three files explicitly,
replace the Zelda command above with:

```bash
gwprov project install zelda3 --variant flash --output "$content" \
  --input "base=/path/to/supported-us-rom.sfc" \
  --input "language=/path/to/supported-german-rom.sfc" \
  --input "language=/path/to/supported-french-rom.sfc"
```

OpenLara consumes `.PHD` files directly inside the CD's `DATA` directory. GBA
requires the declared BIOS/firmware and the selected game; `--bios-dir` is an alias
for `--firmware-dir`. Choose your own eligible game instead of the example ZIP.
ZIP inputs must contain exactly one non-directory file. Directory scans are
non-recursive. Actual released converter WASM runs through Wasmtime.

### Stage an unpublished local build

For a project that is not published in the GWRG catalog yet, use
`gwprov project stage-local` after `gwprov retro-go install`. The JSON manifest
names each local source and its destination under `frogfs/` or `littlefs/`;
mapped core artifacts declare their link-time relocation base. Paths in
`source` are relative to the manifest unless absolute. Optional `sha256`
values pin a source file. For `variant: "sd"`, `path` is relative to the SD
card root, such as `cores/example.bin` or `roms/nes/example.nes`; a mapped core
artifact uses that same `cores/` path and includes its `relocBase`.

```json
{
  "schemaVersion": 1,
  "repo": "local/example",
  "tag": "poc",
  "project": "example",
  "variant": "flash",
  "target": {
    "id": "gnw-retro-go",
    "requiresAbi": {"version": 2, "minSize": 888},
    "files": [
      {"source": "build/example.bin", "path": "frogfs/homebrews/Example.bin"},
      {"source": "build/example.xip", "path": "frogfs/cores/example.xip",
       "mapped": {"relocBase": "0xDEC00000"}}
    ],
    "symbols": [{"source": "build/example.elf"}]
  }
}
```

`stage-local` writes the release install ownership marker, checks for path
collisions, removes obsolete files previously owned by the same target, and
stages beside the content root. It does not publish the project or run converters.
An optional `target.symbols` array can name local ELF sources using `source`,
optional `filename`, and optional `sha256` fields. `profile create` carries
those files into the profile's debug directory without copying them onto media.

An SD manifest uses the same shape with `"variant": "sd"` and card-root paths:

```json
{"schemaVersion":1,"repo":"local/example","tag":"poc","project":"example",
 "variant":"sd","target":{"id":"gnw-retro-go","files":[
   {"source":"build/example.bin","path":"cores/example.bin"}]}}
```

### Create the profile and launch GWemu

```bash
gwprov profile create "$profile" --content "$content" \
  --bootloader-version v1.0.8

gwprov profile show "$profile"

# Final command: launch the persistent instance with a window.
gwprov gwemu run --profile "$profile"

# Capture the live display as PNG; the command also checks for an all-black frame.
gwprov gwemu screenshot --profile "$profile"

# Capture the display and an ELF-symbolized ARM call stack in one report.
gwprov gwemu diagnose --profile "$profile" --symbols build/dkc1_core.elf

# Add selected 32-bit target globals to the same report.
gwprov gwemu diagnose --profile "$profile" --symbols build/dkc1_core.elf \
  --u32 snes_frame_counter --deref g_ppu:16
```

`gwprov gwemu diagnose` attaches to the running instance, captures a PNG, checks
whether every pixel is black, and unwinds the current ARM stack using the
firmware and app ELF `.debug_frame` data. It prints JSON with the screenshot,
registers, frames, and any unwind stop reason. The `display_state` field
distinguishes an all-black frame from a Retro-Go pause overlay identified in
the call stack. Repeat `--u32 SYMBOL` to read 32-bit globals from the loaded
ELFs, which is useful for correlating a black frame with runtime state. Repeat
`--deref SYMBOL[+OFFSET]:SIZE` to read bytes through a pointer-valued global,
such as a PPU register block. It then restores the instance's prior running or
paused state.

### Live native function profiling

```bash
gwprov gwemu profile --profile "$profile" --duration 15
# Explicit project probes and runtime relocation when needed:
gwprov gwemu profile --profile "$profile" --duration 30 \
  --progress-symbol frame_counter --rebase .cold_code=runtime_code_base
```

`gwemu profile` samples the running ARM PC through the daemon's QMP channel,
without taking the GDB socket or deliberately halting, resetting, or changing
the app. The daemon serializes monitor requests from the profiler, `ps`,
screenshots, and control commands. It can run beside the fault debugger.
Bundled firmware and app ELFs resolve
functions automatically. `--symbols ELF` adds symbols; `--rebase
SECTION=POINTER_SYMBOL` reads the runtime base of NOR-mapped code/data, using the
same symbol translation as the debugger. No app-specific names are required.

The default interval is 20 ms with jitter to reduce frame/interrupt aliasing.
The console ranks native functions by **percentage of PC samples**, reports
emulator CPU usage and observed progress increments per wall second, and gives
the report path. Auto-discovery looks for 32-bit heartbeat/frame/progress counters;
repeat `--progress-symbol SYMBOL` to select counters explicitly. Counters that
reset are flagged rather than reported as enormous throughput. `--interval`
changes sampling rate; `--top` limits only console rows. JSON retains every
function, unresolved PC, raw sample, counter observation, source location for
the leading functions, rebased section, and symbol ELF checksum. Automatic PNGs
before/after the interval identify the actual scene. `--format json` prints the
full structured report; Ctrl-C retains a partial profile and leaves the VM alone.

The report summarizes each routine's longest and accumulated consecutive
same-function residency observed in PC samples. It also lists individual
routine-stall candidates whose observed span reaches `--stall-threshold`
(default 1 second), including sample count and largest gap between samples.
These durations estimate how long the CPU stayed in a routine; QMP sampling
cannot prove uninterrupted execution or that interrupts and other system work
were blocked between polls. When progress counters are available, the report
also gives intervals where a counter stayed flat and ranks routines sampled
inside each interval. `--progress-interval SECONDS` controls counter polling (default 1 second;
minimum 5 ms). More frequent observations improve freeze localization while
adding monitor traffic. Actual counter spacing is also bounded by the PC/status
poll interval and monitor latency. `longest_no_progress` reports each counter's longest
observed plateau, including episodes below the warning threshold. Use a monotonic
frame or heartbeat counter for this measurement; equal values alone cannot rule
out counter resets or a full wrap.

Timestamped `state_timeline` transitions and `running_epoch` identifiers preserve
observed halts and resumes. Stall and residency spans never cross an observed
halt, even a brief one between counter reads. Progress reports retain wall-time
rates and add rates from counter pairs within the same running epoch. A pause
entirely between status polls remains unobservable; transition boundaries have
polling uncertainty. Residency retains SP/LR context counts but always marks
`single_invocation_proven: false`: repeated calls with identical stack context,
unsampled callees and interrupts cannot be distinguished by PC sampling. Add a project heartbeat or frame
counter with `--progress-symbol` when automatic counter discovery finds none.

**Interpretation:** these are running-state PC snapshot shares, not exact
Cortex-M cycle counts or complete caller stacks. QMP samples at emulator
synchronization points, so MMIO/interrupt boundaries can be overrepresented.
QMP request duration and unresolved samples are always reported. Longer intervals
reduce observer overhead; compare profiles from the same workload. A stopped VM
produces no running samples and a nonzero exit status. Function attribution uses
actual ELF function ranges, so missing symbols are counted as unresolved.

Raw DWT control/CYCCNT observations are included once per second; gwprov never
resets or enables the counter. The structured `dwt_cycle_counter.status` reports
whether `DWT_CTRL.CYCCNTENA` was enabled, along with observed control values and
whether CYCCNT changed between observations. A disabled counter is explicitly
identified and its zero values must not be read as elapsed cycles. Firmware may
reset CYCCNT on each loop. Even when enabled, some GWemu builds advance
CYCCNT from virtual time rather than instruction execution, so it cannot measure
exact instruction costs; consequently gwprov does not distribute that counter
across functions. Host TCG/MMIO profiling answers a separate question. GWemu's
documented
`GNW_UI_FRAME_TRACE`, `GNW_IDLE_PROF`, `GNW_BQL_PROF` and `GNW_MMIO_PROF` hooks
remain useful for emulator-side investigation (MMIO instrumentation can itself
be expensive).

From an existing interactive Python debugger:

```python
report = dbg.profile(duration=15, interval=0.02)
report["functions"][:10]
report["progress"]
```

### Local port debug adapter

This interface is for a developer or AI working on a local port. It requires no
project installation or published release. Give `--debug-config port.debug.json`
to `gwemu profile`, `diagnose`, `watch`, or `debug python` to reuse the same symbol
sources, runtime mappings, and progress probes. File paths are relative to the
descriptor. A build step may generate the JSON; gwprov loads data and does not
execute project code.

```json
{
  "schemaVersion": 1,
  "elfs": ["build/app.elf"],
  "relocations": [{"section": ".cold_code", "basePointer": "cold_code_base"}],
  "progressSymbols": ["frame_count"]
}
```

ELFs provide routine names/ranges and, when present, DWARF source/type/unwind
information. Stripped ELFs retain their section layout; exported dynamic symbols
are used if present, otherwise their PCs stay unnamed. A closed component can instead supply a compact name/address/size
map, without source code or a full debug ELF:

```json
{
  "schemaVersion": 1,
  "symbols": [
    {"name": "render_frame", "kind": "function", "address": "0x24001000", "size": 512},
    {"name": "frame_count", "kind": "object", "address": "0x24010000", "size": 4}
  ],
  "progressSymbols": ["frame_count"]
}
```

Function ranges must be exact; gaps are unresolved rather than attributed to
the preceding name. Numeric fields accept integers or hexadecimal strings. Repeated static function
names retain every address range; named variable probes must be unambiguous.
`symbols` and `elfs` may be combined. Routine maps name routines but cannot
provide DWARF source, types, or unwinding. With no app symbol information,
profiling still records/ranks raw PCs; it does not invent routine names.

For relocatable map records, add `sections` with `name`, linked `address`, and
`size`, and give each symbol its `section`. Declare `relocations` as above;
`basePointer` is a named 32-bit target variable containing that section's live
base. The profiler refreshes mappings as the app initializes. `progressSymbols`
names 32-bit objects; explicit CLI progress selections override the descriptor.
No code or globals with project-specific names are built into gwprov.

In Python, `symbols.load_config(path)` loads the common symbol adapter and
returns its probes/mappings. `dbg.configure(path)` also reads initialized target
base pointers; call it after app initialization. `dbg.profile()`, `dbg.at()`,
`dbg.traceback()`, and `dbg.diagnose()` then use the same symbol table. ELF-only
operations explain missing DWARF/disassembly when only a compact map was supplied.

### Record and replay controller routes

`gwemu start`, `run`, and `debug` accept `--record-timeline FILE.tl` or
`--timeline FILE.tl`. Recording uses GWemu's existing `GNW_TIMELINE_RECORD`
GPIO recorder; playback uses `GNW_TIMELINE`. Timestamps are guest seconds from
machine boot, so debugger pauses do not add delay to the route.

```bash
# Start a visible recorder and perform the route using the window's controls.
gwprov gwemu start --profile "$profile" --record-timeline "$PWD/build/route.tl"
# Close the window cleanly, or use gwprov gwemu stop from another terminal.
# Replay from the same initial firmware/media state.
gwprov gwemu start --profile "$profile" --timeline "$PWD/build/route.tl"
```

Recording requires a visible window and a new output filename. GWemu opens the
file on the first GUI input and flushes each down/release event; a missing file
before the first input is expected. On clean shutdown it appends a `quit` event.
Remove that final event from a **copy** if replay should stay open for fault
triage. Keep the original recording as evidence. Replay ends at that event
otherwise. This controls inputs; it does not reset writable NOR/SD images or
save states. Preserve the initial profile/media before recording when exact
reproduction matters, and replay from a copy of that same baseline.

For unattended homebrew routes, use `gwemu debug --unpause-homebrew
--app-symbols APP.elf --detach-after-app-entry --keep-running` with either
timeline option. GWProv owns QEMU's QMP-over-stdio connection in its per-user
daemon, so `gwemu ps`, screenshots, state polling, and controls remain available
after GDB detaches without exposing a QMP listener. This clears
Retro-Go's autostart pause through the existing launch hook before entering the
app, then frees the debugger for `gwemu watch`. Use the same launch options for
recording and replay.
This needs no wall-clock input player or changes to GWemu.

For automatic symbol lookup of relocated XIP code, export a 32-bit runtime
base global named `<section-name>_xip_runtime_base` alongside an ELF section
named `.xip_<section-name>`. For example, `.xip_game` pairs with
`game_xip_runtime_base`. `gwprov gwemu ps` and the profiler use that pointer to
rebase symbols to the address where the section is mapped at runtime.

For a symbolized debug session, use `gwprov gwemu debug --profile "$profile"`.
The command starts GWemu stopped at reset so it can install breakpoints,
loads the matching `debug/retro-go-debug.elf` from the checksum-verified firmware
release, then continues execution. GWProv owns QMP over stdio in its per-user
daemon; it keeps `gwprov ps`, screenshots, state polling, and controls available
after GDB detaches without creating a QMP socket or TCP listener. This reset halt is a debugger
setup step; the app does not enter Retro-Go's pause menu unless `start_paused`
is left enabled.
Pass `--headless` to use QEMU's `-display none` for scripted or parallel
debugging; GDB stays on loopback while QMP remains private to the daemon.

Retro-Go `/CONFIG` homebrew autostart passes `start_paused=true` to
`run_gwhb_homebrew`. The default debug session breaks on that function only with
`--unpause-homebrew`, clears `r2` at function entry, and then resumes the app.
The app's normal launch behavior remains paused. The interactive debug mode also
sets fault breakpoints on the common Retro-Go and ARM fault handlers. On a hit,
it captures the GDB transcript, including backtrace, registers, stack words,
and nearby instructions, in `runtime/gwprov/fault-triage.txt` and leaves GDB
stopped. Pass `--app-symbols APP.elf` to load app symbols into GDB even when not
using `--detach-after-app-entry`.
Use `gwprov gwemu pause --profile "$profile"` or `resume` to stop or continue
execution through QMP while GDB remains connected.

For an automatic launch-to-watch cycle, provide the app ELF so gwprov can resolve
`app_main`, detach at that entry point, and leave the VM running:

```bash
app_elf="$PWD/build/dkc1_gwrg/homebrew/dkc1_core.elf"
gwprov gwemu debug --profile "$profile" --gdb-port 12345 \
  --unpause-homebrew --app-symbols "$app_elf" \
  --detach-after-app-entry --keep-running

gwprov gwemu watch --profile "$profile" --symbols "$app_elf" \
  --duration 30 --stall-after 3
```

`profile create` copies published target ELF symbols (or local `target.symbols`)
into `debug/apps/`; `diagnose`, `watch`, and `debug python --profile` load them
automatically. `--symbols ELF` adds another ELF when needed. `watch` works with
any core or homebrew ELF. It always samples ARM PC, SP, and
LR; it also auto-detects common 32-bit progress counters (`heartbeat`,
`frame_count`, `frame_counter`, and `progress_count`) and guest-PC globals ending
in `_cur_pc`, `_current_pc`, `_resume_pc`, or `_guest_pc`. Projects can specify
their own symbols with repeatable `--progress-symbol`, `--guest-pc-symbol`, and
`--u32` options, can read structs directly with `--bytes SYMBOL:SIZE`, and can
include pointed-to memory with `--deref SYMBOL:SIZE`.
Fault-handler symbols are discovered from the loaded firmware and app ELFs.
For relocated code or data in NOR, `watch` rebases symbols from a target pointer
using repeatable `--rebase SECTION=POINTER_SYMBOL`. The DKC example above names
its section and runtime-base symbol explicitly; other projects use their own
ELF section and pointer symbol names.

On a fault-handler hit, stagnant guest PC, stagnant progress counters, or a
stable ARM PC/SP/LR location, gwprov writes a JSON report and PNG under
`runtime/gwprov/triage/`. The bundle includes sampled values, a symbolized
traceback, registers, nearby objdump instructions when the PC belongs to a
loaded ELF, and an all-black display result. Fault-handler hits are direct
evidence; loop and stall labels are explicitly heuristic because a project can
legitimately wait at a stable location. `watch` exits 1 when it captures a
fault or suspected stall and 0 when it observes progress through the full
window. If no progress symbols are found, it reports that fact in the JSON
instead of assuming the process is healthy.
Use one GDB owner at a time; after the launch command detaches, `watch` owns the
GDB connection for the observation window.

GDB commands such as `continue`, `stepi`, `info registers`, `x/16wx ADDRESS`,
`bt`, and `monitor system_reset` control an attached interactive session. Use
`--symbols ELF` to override the debug session's firmware ELF or
`--no-break-on-fault` to omit fault breakpoints. `gwprov gwemu ps` exits with an
error when seccomp/no-new-privileges makes an empty process scan inconclusive.
The daemon owns GWemu's QMP stdio stream. CLI processes discover it through a
per-user local endpoint: a mode-restricted Unix socket on Linux/macOS and a
named pipe with an explicit current-user ACL on Windows. The service verifies
the peer UID or SID and uses a versioned JSON protocol. QMP is not exposed on a
network listener. GDB remains a loopback TCP endpoint when requested.

`gwprov gwemu ps` reports CPU execution as `running` or `halted` in `STATE`,
using QMP's boolean `running` field. A debugger stop is `halted`; having a GDB
connection does not imply execution. JSON includes `running`, `halted`, and
`qmpStatus` (the original QEMU reason, such as `debug` or `paused`). If QMP cannot
verify execution, the state is `unknown`, the booleans are null, `stateDetail`
explains why, and `ps` exits with status 2. Socket permission errors remain fatal.
Automation must check `running == true`, rather than process existence or an
application label. Pause/resume commands verify the resulting execution state
before reporting success; an immediate breakpoint stop is a failure to resume.

`gwprov gwemu ps` also polls an `Application` column independently of GWemu's
process `STATE`. It uses the profile's Retro-Go ELF and app ELFs to interpret the
live ARM stack and Retro-Go picker tab, reading registers and memory through
the daemon. This leaves the GDB endpoint available to an interactive debugger. When an
app exports a writable NUL-terminated `char gwprov_application_state[64]`, its
text becomes the application state while that app is executing; projects can
publish values such as `Loading level` or `Boss phase 2` without gwprov-specific
code. Missing symbols produce `Unknown`.

For a persistent Python debugger attached to an already-running target:

```bash
gwprov debug python --target gwemu --port 1234 --profile "$profile" \
  --symbols build/dkc1_core.elf
```

Run one GDB owner at a time; `watch` keeps one connection and briefly halts only
while it samples or captures target state. A DKC-specific invocation can select
its interpreter PC and useful counters explicitly:

```bash
gwprov gwemu watch --profile "$profile" --symbols "$app_elf" \
  --progress-symbol dkc1_gwrg_host_heartbeat \
  --progress-symbol dkc1_gwrg_frame_count \
  --guest-pc-symbol g_interp816_cur_pc \
  --rebase .xip_dkc1=dkc1_xip_runtime_base
```

`--rebase` explicitly connects an ELF section's link-time address to a
target-side pointer that contains its runtime base. The CLI has no project-name
special cases; supply one entry for each relocated section that should be
symbolized.

When captured at a Cortex-M fault handler entry, `dbg.traceback()` now decodes
EXC_RETURN and the stacked CPU registers, then continues the call chain from
the actual faulting instruction. Reports include SCB fault registers, decoded
CFSR flags, the valid fault address, source location, and nearby instructions.
If the firmware exports `_stack_redzone` and `_Stack_Redzone_Size`, the report
also states whether the fault address hits that guard. `dbg.fault_context()`
returns this information separately. It supports basic and extended FP frames;
a capture taken after the handler has changed SP/LR may be too late to recover
the frame, and is reported explicitly as unavailable.

For scripted fault capture, `GwemuGDBBackend.open(halt=True)` attaches without
resuming a paused VM. Load symbols and arm breakpoints before `dbg.resume()`.
The default `open()` keeps the existing behavior of resuming on attach.

Typed memory reads use the owning ELF's DWARF information, so the host need not
know the target compiler's structure padding or enum width. `dbg.read_value("g_cpu")`
returns a Python dictionary; fixed arrays become lists, scalars become numbers,
and pointers remain addresses. `dbg.symbols.type_layout("g_cpu")` describes the
field offsets, sizes, and scalar encoding. Structs, fixed arrays, integer,
boolean, floating point, pointer, and enum globals are supported; missing debug
types, bit fields, and unsupported types produce explicit errors.

`dbg.read_ring("trace_ring", "trace_head")` samples an array and its monotonic
next-write counter under one halt, restores the previous running state, and
returns entries in chronological order with capacity and overwrite counts.
The head must count every write, not merely store a wrapped index. Both methods
limit a single read to 64 KiB by default (`max_bytes` overrides it).
`gwprov gwemu diagnose` and `watch` accept repeatable `--value SYMBOL` and
`--ring ARRAY:HEAD`; `watch` extracts these records when it captures a trigger
report. App variables are selected by the caller; these options contain no
project-specific layouts or names. Unsupported types are recorded with a reason
in the report so they do not conceal the native fault information.

`dbg.run_until("function", timeout=30)` installs a temporary breakpoint,
resumes, and waits through QMP. It returns a dictionary with `hit`, `stopped`,
and registers when stopped. `dbg.wait_stopped(timeout=30)` waits for an already
armed breakpoint. A timeout is explicit and leaves execution running. A debug
stop leaves execution halted for inspection; resume with `dbg.resume()`.
Use a breakpoint address that is not already owned by another routine.
These wait helpers support GWemu through QMP and physical hardware through the
attached debug backend. Hardware waiting polls Cortex-M `DHCSR.S_HALT` (or
PyOCD's target state), so a wait does not halt/resume the running device just to
check it. Hardware breakpoints use the probe's FPB comparators; remote
`gnwmanager serve` breakpoint support depends on the server protocol and may be
unavailable.

For a physical fault capture, load the official firmware and app symbols from a
device profile, then arm breakpoints before reproducing the crash:

```text
gwprov debug python --target hardware --probe-id PROBE_ID --profile PROFILE
>>> dbg.reset(halt=True)
>>> dbg.arm_fault_breakpoints()
>>> dbg.resume()
>>> report = dbg.wait_fault(timeout=180)
>>> report["triage"]
```

`arm_fault_breakpoints()` uses whichever of `common_fault_handler_c`,
`HardFault_Handler`, `MemManage_Handler`, `BusFault_Handler`, and
`UsageFault_Handler` are present in loaded symbols. At a handler-entry stop,
`wait_fault()` collects core registers, the stacked exception frame, SCB fault
registers, the symbolized unwind, and (for MemManage faults) MPU region
permissions with the region covering the faulting PC identified. MPU region
selection is restored after capture. The reset-halt establishes a known boot point before breakpoints are installed.
Use `dbg.run_until("emulator_start")`, `dbg.run_until("run_gwhb_homebrew")`,
and then `dbg.run_until("app_main")` to inspect the Retro-Go launch path when
those symbols are present. A stop remains halted for inspection; resume it
explicitly. This captures and diagnoses a fault automatically, but deterministic
hardware crash bisection still requires a recorded/replayable controller-input
schedule and a reset-to-repro runner. GWemu already supplies that timeline path;
physical input recording/replay is the remaining parity work.

The `dbg` object stays connected for the whole REPL session. It provides
`dbg.halt()`, `dbg.resume()`, `dbg.step()`, `dbg.regs()`, `dbg.where()`,
`dbg.traceback()`, `dbg.diagnose()`, `dbg.read(addr, n)`, `dbg.u32(addr)`, and
hardware breakpoints with `dbg.bp(addr)`. `dbg.addr2line(address)` resolves a
runtime PC directly in the console, including mapped section rebases.
Thumb function symbols are normalized automatically for breakpoints and nearest
symbol lookup. Register and symbol-location reads restore the target's prior
running state.
With a managed GWemu profile, `dbg.screenshot([path])` saves a PNG under the
GWProv runtime directory by default and returns its path, dimensions,
`all_black`, and nonblack pixel count. `dbg.key("START")` sends a mapped Game & Watch button, and `dbg.qmp(command,
arguments)` sends a structured QMP request. ELF symbols can
be queried with `dbg.at("symbol_name")` or `dbg.symbols.find("substring")`.
`dbg.nm("substring")` returns matching symbol rows with address, size, type,
section, and owning ELF; `dbg.disasm("function_name")` uses objdump on the
loaded ELF and returns that function's assembly listing. `dbg.addr2line(address)`
uses `arm-none-eabi-addr2line` (or `addr2line`) to resolve runtime addresses to
source lines, including section rebases. `diagnose` includes source locations
and nearby disassembly when the PC belongs to a loaded ELF.
Use `dbg.watchpoint(address, size=4, access="read")` to stop on a DWT or
GWemu remote-debug memory access. This can reveal which code reads a known
peripheral register even when firmware symbols are missing; the stop PC and
`dbg.addr2line()` identify the access site when symbols or debug data are
available. A watchpoint observes memory accesses, not CPU register values.
For firmware that returns its button mask in a register,
`dbg.press_button(return_at, mask)` injects clean release polls, one or more
pressed polls, then a release at the routine's return site. Supply the firmware-
specific return address or symbol and button mask; the debugger does not assume
an application or button mapping. `dbg.inject_return(return_at, value,
register="r0")` is the single-poll primitive for other scalar-returning
routines. Both helpers resume after each injected value and leave the target
halted if another breakpoint or fault interrupts the operation.
`dbg.symbols.sections()` shows link-time ranges. When a mapped sidecar's runtime
base is known, call `dbg.symbols.rebase(".xip_dkc1", actual_base)`; later
lookups translate symbols in that section while leaving the ELF unchanged.
DKC exports `dkc1_xip_runtime_base`, so after its core has initialized, use
`dbg.rebase_from_pointer(".xip_dkc1", "dkc1_xip_runtime_base")` to read the
actual base from target memory and rebase in one step.
Use `--target hardware` to attach through OpenOCD instead; this attaches without
resetting or flashing the device. The matching firmware ELF is loaded with
`--profile`, while app ELFs are passed with repeatable `--symbols` options.

The variant comes from `.gwprov-firmware.json`, written by `retro-go install`.
`profile create` selects flash or SD assembly from `.gwprov-firmware.json`. Flash
content lives in `flash/frogfs/` and `flash/littlefs/`; SD content lives in `sd/`
and is packed into a bundled FAT32 image. The generated profile contains bank-1
and bank-2 images, extflash, configuration and provenance. Set `--sd-size-mib`
to choose the bundled SD image capacity.

For SD firmware, use an SD content root throughout staging; `profile create`
detects the variant from that root and bundles its files into the profile:

```bash
sd_content=dev-local/content/retro-go-sd
gwprov retro-go install --variant sd --output "$sd_content"
gwprov project install tgb --variant sd --output "$sd_content"
gwprov profile create dev-local/profiles/retro-go-sd --content "$sd_content" \
  --sd-size-mib 256
```

Bank 1 uses the official `gnw_bootloader.bin` linked at `0x08000000`, obtained with
gnwmanager's bindings. Bank 2 contains the released Retro-Go firmware. This is a
standalone Retro-Go instance; patched stock dual boot uses the separate OFW flow
and its `0x08032000` bootloader. No handcrafted bank-1 stub is used in new profiles.
The bootloader cache stays in `.gwprov-cache/` beside the content root, and the
profile records the resolved version and computed hash.

Flash capacity defaults to the smallest of **64, 128 or 256 MiB** that fits the
packed content and LittleFS partition. LittleFS defaults to 2 MiB at the top of
the chip. SD profiles bundle a 128 MiB FAT32 image by default; `--sd-size-mib`
changes its size.
To pin capacity or choose another bootloader, use a new instance directory:

```bash
# Explicit capacity and LittleFS partition size.
gwprov profile create dev-local/profiles/retro-go-128 \
  --content "$content" --extflash-mib 128 --littlefs-mib 2

# Resolve the latest official bootloader release instead of the default v1.0.8.
gwprov profile create dev-local/profiles/retro-go-latest \
  --content "$content" --bootloader-version latest

# Use another release repository.
gwprov profile create dev-local/profiles/retro-go-custom \
  --content "$content" --bootloader-repo OWNER/REPO --bootloader-version TAG

# Use a locally compiled bootloader linked at 0x08000000.
gwprov profile create dev-local/profiles/retro-go-local \
  --content "$content" --bootloader-file /path/to/gnw_bootloader.bin
```

The local GWemu checkout used during development hard-codes 64 MiB; booting a larger
image requires corresponding capacity support in GWemu. Image creation supports
all three capacities. Firmware and project releases can be pinned separately with
`--version TAG` on their install commands.

### Include full Doom WADs

To add the local WADs to the same content tree, reinstall Doom before creating the
profile. Its shipped shareware is retained:

```bash
gwprov project install doom --variant flash --output "$content" \
  --input-dir "/path/to/doom-wads"
```

All OpenLara levels plus these WADs exceed 64 MiB, so that combined profile selects
a larger capacity. For a separate 64 MiB Doom instance:

```bash
gwprov retro-go install --variant flash --output dev-local/content/doom-full

gwprov project install doom --variant flash --output dev-local/content/doom-full \
  --input-dir "/path/to/doom-wads"

gwprov profile create dev-local/profiles/doom-full \
  --content dev-local/content/doom-full --extflash-mib 64

gwprov gwemu run --profile dev-local/profiles/doom-full
```

### Reuse an instance and expose testing controls

After opening a new shell, activate the environment again. The profile remains on
disk and its media mutations persist across clean exits. These are alternative
launch commands; run one instance against a profile at a time:

```bash
source dev-local/venv/bin/activate

gwprov gwemu run --profile dev-local/profiles/retro-go-demo

gwprov gwemu run --profile dev-local/profiles/retro-go-demo --headless

gwprov gwemu run --profile dev-local/profiles/retro-go-demo \
  --gdb-port 3333
```

Profile launches are owned by the per-user GWProv daemon and start the guest
running. The daemon keeps QMP on GWemu's stdin/stdout and exposes only GWProv's
local control operations. It exits shortly after its last VM and active hardware
lease ends. GWemu stderr goes to the instance's `gwemu.log`. See
[the daemon design](docs/DAEMON.md) and [the provisioning guide](docs/PROVISIONING.md).

### Portable reports

Any structured GWProv JSON report can be rendered without external assets:

```sh
gwprov report render --input dev-local/reports/profile.json \
  --format html --output dev-local/reports/profile.html
```

PDF rendering is optional and uses `gwprov[reports]` (ReportLab); the original
JSON remains the machine-readable evidence source.

Hardware profiling emits JSON directly or renders an HTML/PDF companion:

```sh
gwprov perf hardware --probe-id PROBE_ID --profile DEVICE_PROFILE \
  --duration 30 --format html --output dev-local/reports/hardware-profile.html
```

Projects can export the generic `gwprov_trace_header` ring described in
[the trace ABI](docs/TRACE_ABI.md) for routine-level exclusive cycle reports.
For projects that already expose integer counters, repeat `--counter-symbol`
with symbols from the profile ELFs or `--symbols ELF`; `--frame-counter` also
reports the observed logical frame rate, and `--total-cycle-counter` provides
a denominator for cycle-counter percentages. Counter capture is read-only while
the target runs. To measure a reset-to-app run on a local PyOCD probe,
`--start-at SYMBOL` resets the target and starts capture at that function entry; repeat
`--set-register REG=VALUE` to explicitly patch launch arguments at the
breakpoint. The register hook is useful when firmware configuration normally
starts an app paused. The profile JSON records all sampled counters and the
capture window. Hardware profiling also accepts `--programmer` or
`--remote-url` for passive trace-ABI captures.

### Hardware deployment

A hardware deployment plan records every destination offset and SHA-256 before
writing. Bank 1 (OFW or bootloader) and bank 2 are separate selectable regions;
flash profiles write FrogFS and LittleFS at their declared offsets, while stock
profiles can write the complete extflash backup. SD deployment copies files
through `gnwmanager` and overlays the existing card contents. Text deployment
output reports the active phase and per-region progress to stdout; use
`--output json` when a script needs one clean machine-readable result.

```sh
gwprov deploy plan --profile dev-local/profiles/retro-go-demo
gwprov deploy apply --profile dev-local/profiles/retro-go-demo --probe-id PROBE_ID
# Or select one OpenOCD adapter explicitly:
gwprov deploy apply --profile dev-local/profiles/retro-go-demo --programmer stlink
# Keep stdout machine-readable when scripting:
gwprov deploy apply --profile dev-local/profiles/retro-go-demo --probe-id PROBE_ID --output json
```

Apply defaults to all regions present in the profile. Text output shows live
region and byte progress on stdout while it enters gnwmanager's RAM programmer,
writes and verifies each selected region, then starts the bank-1 vector. Use
`--region bank1` or `--region bank2` to select an individual internal bank;
repeat `--region` for exact plans. `--output json` emits only the final structured
result for scripts. A remote server is
selected with `--remote-url ws[s]://host:port/gdb`; `--remote-origin` supplies its
allowed origin when configured. SD deployment is an overlay and does not delete
files omitted from the profile.

Before starting that RAM programmer, GWProv passively checks VTOR, the
gnwmanager mailbox status, and DHCSR. This follows the web-builder's Recovery
Mode heuristic: an idle mailbox alone can be stale, so a live idle stub also
requires the CPU to be running; a non-idle stub is recognized by its SRAM VTOR.
If the RAM service is already resident, GWProv stops before resetting or
replacing it. GWProv also persists a recovery marker in the target lease before
loading the service. If a command exits before verified return to bank 1,
`gwprov ps` reports the device busy and skips all target traffic; new hardware
commands are blocked until recovery. Successful deployments boot bank 1 and
clear the marker. For an interrupted operation, recover after the mailbox
reports `IDLE` (or a terminal error):

```sh
gwprov device recover
```

That command requires the active hardware device. It resets into bank 1 and
verifies the CPU left the stub. If the mailbox reports active `ERASE`, `PROG`,
or `HASH`, or its state cannot be read, GWProv refuses recovery so an active
flash operation is not interrupted. A stub still in `BOOTING` is recoverable
only when VTOR has not moved into the stub's SRAM range yet.

## Local and remote device sessions

`gwprov ps` combines GWemu process/application state with locally visible hardware
probes. A physical row reports `busy` when another GWProv session owns that probe,
or a detected local OpenOCD, PyOCD, or gnwmanager process may be using it. While busy, `ps`
does not open the debug session or read target registers; the `Application` value
is `Unknown` and the row identifies the owner when available. Otherwise it reads
the Cortex-M halt bit. Without `--profile`, this is read-only and application state
is `Unknown`. With matching firmware/app ELFs, it briefly halts each running target
to capture registers, stack, and menu/app state from symbols, resumes it, and
verifies the final run state. It leaves an already halted target halted.
Applications may export the writable SRAM string `gwprov_application_state` for
custom game states. GWProv hardware, deployment, profiling, and remote sessions
hold process-shared leases for their duration; they release automatically on exit.
Install `gwprov[device]` for selected-probe sessions and enumeration.

The text view uses a color-aware, terminal-sized table and pages only when the
rendered output exceeds the interactive screen. Use `--no-pager` to always
write directly to the terminal. `gwprov gwemu ps` supports the same options.
Use `--output json` when consuming inventory from scripts; JSON output remains
structured and separate from the human-readable view.

The `Application` column is separate from VM/CPU `STATE`. Retro-Go symbols can
identify initialization, homebrew startup, picker tabs (Favorites, Homebrew, and
each registered core), the game menu, pause/settings and time menus, and dialogs
over the picker or a running game. An app can report a project-specific state by
exporting the SRAM string `gwprov_application_state`; without usable symbols the
application value is `Unknown`.

A single Python debugger process can keep several hardware targets connected:

```sh
gwprov debug python --target hardware --probe-id PROBE_ID_A --probe-id PROBE_ID_B
```

The REPL exposes `sessions` and `backends` dictionaries keyed by probe ID, and
`dbg` aliases the first session. With no `--probe-id`, GWProv preserves
`gnwmanager`'s OpenOCD autodetection order, which selects one matching programmer.
Use repeatable `--programmer stlink|jlink|cmsis-dap|rpi-gpio` options to start
separate OpenOCD sessions for explicit adapter types; `--probe-id` pins individual
PyOCD probes by unique ID. Deployment and hardware profiling accept the same
single-target selectors.

Each `gnwmanager serve` endpoint represents one target. Multiple server endpoints
can be attached in the same Python session:

```sh
gwprov debug python --target hardware \
  --remote-url ws://host-a:8765/gdb \
  --remote-url wss://host-b:8765/gdb
```

Install `gwprov[remote]` for WebSocket support. If a server restricts browser
origins, pass the matching `--remote-origin` once per URL. The remote protocol
supports memory writes and target control, so expose servers only on loopback or
a trusted network, preferably through an SSH tunnel or TLS. Its reset operation
uses the server's reset-and-halt followed by resume.

## CLI

`gwprov --help` gives a styled overview and common starting points. `gwprov tree`
shows the full command map with short descriptions; use `gwprov COMMAND --help`
to see options for a command. `gwprov tree --format names` provides plain
top-level command names for shell integrations.

```sh
gwprov --help
gwprov show
gwprov show devices
gwprov devices
gwprov adapters list
gwprov adapters add pi-probe ws://10.2.3.122:8765/gdb
gwprov set active DEVICE_ID
gwprov config show
gwprov set profile PROFILE
gwprov apply
gwprov sdcard add /Volumes/RETROGO
gwprov sdcard list
gwprov sdcard create dev-local/sdcard.img --size-mb 128
gwprov sdcard compose dev-local/sdcard.img --content-dir dev-local/content/retro-go-demo
gwprov tree
gwprov projects list
gwprov projects list --output json
gwprov project versions tgb
gwprov project install tgb --variant sd --output dev-local/provisioned
gwprov profile show PATH/TO/GWEMU/PROFILE
gwprov project versions slash-proc/doom-retro-go-sd
gwprov project info slash-proc/openlara-retro-go-sd
gwprov project install slash-proc/zelda3-retro-go-sd --variant sd --output dev-local/provisioned \
  --input base=Zelda3.sfc --input-dir language=translations
gwprov project install slash-proc/openlara-retro-go-sd --variant sd --output dev-local/provisioned \
  --input-dir level=TombRaider/DATA
gwprov project install slash-proc/pce-go-retro-go-sd --variant sd --output dev-local/provisioned \
  --firmware-dir /path/to/firmware --game-dir pcecd=~/Emulation/Roms/PCE-CD
gwprov gwemu run --profile PATH/TO/GWEMU/PROFILE
gwprov gwemu run --bank1 bank1.bin --bank2 bank2.bin --extflash extflash.bin --sdcard sdcard.img
gwprov input tap B
gwprov input tap LEFT+GAME
gwprov fs create frogfs 2 dev-local
gwprov fs create lfs 2 dev-local
gwprov fs create sdcard 128 dev-local
gwprov retro-go config --output build/CONFIG --rom gb Tetris.gb
gwprov retro-go build --path references/game-and-watch-retro-go-sd --dry-run
gwprov media frogfs --retro-go-root references/game-and-watch-retro-go-sd [packer options]
gwprov ofw patch mario --source-tree ../qemu-gnw --backup-dir backup --output-dir build/ofw [patch options]
```

Enable Bash completion in the current shell with `eval "$(gwprov completion bash)"`. In zsh,
run `autoload -Uz compinit && compinit` followed by `eval "$(gwprov completion zsh)"`.
Both modes complete the command tree, managed profiles, devices, SD cards, common options,
and directory arguments. Device IDs are enumerated without opening a target debug session
or polling GWemu. Bash loads curated project names on the first project-argument completion and
reuses them in that shell. Device controls use the selected device from
`gwprov set active DEVICE_ID` unless the command accepts an explicit target.

Profiles and SD cards can be assigned to the active device with `gwprov set profile NAME`
and `gwprov set sdcard NAME`. `gwprov apply` launches the assigned profile on GWemu or
deploys it to hardware. Register an already-mounted card folder or drive with
`gwprov sdcard add PATH [NAME]`; the default name is its folder name on Unix-like systems
or drive letter on Windows. Registering a card does not format or modify it. When a profile
with an SD image is applied to hardware with a card assigned, its files are overlaid onto
the mounted folder and files absent from the profile are retained.
The `sdcard` command also creates and populates raw SD images; `sd` remains a short alias.

The target-neutral APIs are under `gwprov.common`: `Image` and `Target` select media and destination, `sdcard.compose()` describes SD contents, and target-specific SD managers write to an image, a mounted card, or the device. A `.tl` timeline can be replayed by GWemu or injected into compatible firmware through the probe.

## Structure and source provenance

- `gwprov.common.target`, `sdcard`, `timeline`, `retrogo_config`, and `retrogo_build` are carried over from Doug's own `../tinyemu/scripts/common` modules.
- `gwprov.remote_input` is ported from Retro-Go-SD's shared remote-input script. Its bit layout and shadow address must stay in sync with `Core/Inc/gw_buttons.h` in that firmware.
- GWemu profile parsing follows the `profile.toml` format in qemu-gnw. Large firmware and media files remain external profile data; they are not copied into this repository.

See `NOTICE.md` for source and license notes. Keep local device dumps, firmware blobs, and generated images outside Git.

Retro-Go-SD's filesystem image packers are also exposed as `gwprov media frogfs` and `gwprov media littlefs`; pass their normal packer options after the required `--retro-go-root PATH`. They consume a Retro-Go-SD checkout for firmware tools and assets while their Python implementation is vendored here.

## GWRG project releases

`gwprov project` resolves the project's GitHub Pages `dist/versions.json`, selects the newest
release (or `--version TAG`), reads its manifest, and verifies every staged release file by
size and SHA-256. `--variant flash|sd` selects a supported storage variant and writes a
reproducible staging tree under `<output>/<variant>/`. Flash content is separated into
`frogfs/` and `littlefs/`; `retro-go install` supplies the release firmware and bundled
assets, and `profile create` assembles the final images. Artifacts, shipped games, and converted
outputs follow the manifest's homebrew/core, system, `dataDir`, and firmware directory placement rules.

Converter inputs may be files or directories. Use `--input SLOT=FILE` and repeat it for
multiple files, or use `--input-dir SLOT=DIR` for the files directly inside a directory. For a
converter with exactly one input slot, `--input-dir DIR` is shorthand for that slot. Directory
inputs are non-recursive so unrelated nested content is not consumed. Bash and zsh completion suggest
folders after `--input-dir`, `--firmware-dir`, `--game-dir`, and `--bios-dir`. Files are
checked against the declared extension, size, SHA-1 variants, `strict`, `allowMultiple`, and
`maxCount` rules before a converter runs. Converter execution requires `pip install -e '.[dist]'`;
WASM runs are import-free, memory-bounded, fuel-limited, and outputs are checked against the
manifest.

Core firmware files can be supplied as `--firmware ID=FILE` or found by declared filename with
`--firmware-dir DIR`. `--bios` and `--bios-dir` are accepted CLI aliases, and manifests may use
`firmware` or the legacy `bios` system field (`firmwareDir` or `biosDir` for placement). Required firmware is validated by size and published hash; `requiredFor` slots become required when matching games
are staged with `--game SYSTEM=FILE` or `--game-dir SYSTEM=DIR`. Shipped firmware files are
fetched and SHA-256 checked automatically. A project-owned staging marker allows later installs
of the same target to replace its own files, while unrelated existing files are protected from
overwrite. Published `target.symbols` ELFs are also fetched and hash-checked, stored outside the
device filesystem tree, and copied into profiles for automatic symbolication.

Example: Doom's core and shipped shareware game install without a user WAD; optional WADs can
be passed with `--input base=doom.wad`. Zelda 3 can take one base ROM and several recognized
translation ROMs via `--input-dir language=translations`. OpenLara accepts a directory of `.PHD`
files and converts each to a `.PKD` below `homebrews/openlara/`.


`gwprov project list` reads and checksum-verifies the curated `projects.json` published by
`sylverb/game-and-watch-retro-go-sd`, groups entries as GWRG `core` and `homebrew` targets,
and prints a compact list. Pass `--output json` for the source metadata. Listed project names work
with `project versions`, `project info`, and `project install`; both `tgb` and
`sylverb/tgb` resolve to the listed project. Arbitrary `owner/repo` projects remain supported.

## Pristine stock profiles

Create stock media from your own hash-valid backup pair. Protection is stored as
device state in `rdp-state.bin`, independently of the firmware images.

```bash
gwprov profile create dev-local/profiles/stock-locked --stock \
  --backup-dir /path/to/ofw-backups --locked
gwprov profile create dev-local/profiles/stock-unlocked --stock \
  --backup-dir /path/to/ofw-backups
```

`--stock` selects pristine stock profile creation within the normal profile
creation command. Repeat `--backup-dir` for multiple backup sources; use
`--model mario|zelda` to select a device explicitly. Existing profile
directories are never overwritten.

## Named device profiles

GWProv profiles are directory-based device images: each one contains a
`profile.toml` manifest and the flash, extflash, SD, and related state files it
uses. A bare profile name resolves under the managed profile store, so it can
be used with `profile show`, `gwemu`, deployment, and other profile-aware
commands. Explicit paths remain supported.

The default store follows the host OS: `$LOCALAPPDATA/gwprov/profiles` on
Windows, `~/Library/Application Support/gwprov/profiles` on macOS, and
`$XDG_DATA_HOME/gwprov/profiles` on Linux (or `~/.local/share/gwprov/profiles`
when `XDG_DATA_HOME` is unset). Set `GWPROV_PROFILE_DIR` to use a different
store; it takes precedence over those defaults. `profile create --output-dir`
overrides the store for one creation. An explicitly set `XDG_DATA_HOME` is
also honored on any OS.

```sh
gwprov profile create dkc1 --content dev-local/content/dkc1
gwprov profile list
gwprov gwemu run --profile dkc1
gwprov profile duplicate dkc1 dkc1-perf-a
gwprov gwemu run --profile dkc1-perf-a --headless
gwprov profile create dkc1-test --content dev-local/content/dkc1 \
  --output-dir build/test-profiles
```

`profile duplicate SOURCE DESTINATION` creates an independent working copy of
the complete profile package, including firmware, storage media, symbols, debug
metadata, and device state. It never overwrites an existing destination.
Symlinks and hard links are materialized so writes in an experimental run cannot
mutate the baseline through a shared file. On Linux filesystems that support it,
GWProv uses private copy-on-write clones to make large flash images fast and
space efficient; other hosts receive ordinary independent copies. FrogFS edits
on profiles with mapped XiP files require a raw source for every mapped file via
`--mapped-source PATH=FILE`; GWProv then rebuilds and relocates those files from
their declared bases. Use
`--output-dir` to place a named copy in a temporary profile store, and retain
performance reports outside that directory before deleting an experimental
copy.

For repeated use of a custom store, set `GWPROV_PROFILE_DIR` to that directory;
then names such as `dkc1-test` resolve there from any profile-aware command.

## Filesystem inventories and comparisons

Keep small verification records rather than an image copy for each install:

```sh
gwprov media inventory --profile dev-local/profiles/retro-go --output before.json
# Run the web app against the writable profile, then close GWemu.
gwprov media inventory --profile dev-local/profiles/retro-go --output after.json
gwprov media compare before.json after.json

# Inspect a partitioned FAT SD image directly (first partition at 1 MiB here).
gwprov media inventory --image dev-local/sdcard.img --filesystem fatfs \
  --offset 0x100000 --output sd-files.json
```

Inventory reads media without writing or formatting it. It includes sorted
filesystem tables, file sizes/SHA-256, partition hashes, and full-image hashes.
FrogFS hashes cover stored file bytes (with compression recorded); LittleFS and
FAT hashes cover file contents. LittleFS inspection uses Retro-Go's reversed
flash-block order. Explicit LittleFS images need `--size` and optionally
`--block-size`; FrogFS derives its length from its header. Profile inspection
reads the firmware layout and bundled SD partition table.

`media compare` defaults to filesystem contents and ignores timestamps, free
space and allocation order. `--mode image` compares complete images and raw
partition hashes too. A mismatch exits with status 1 and prints both tables.
FAT short names are reported in their actual on-disk spelling, often uppercase.
Pin releases and inputs for repeatable records; no claim about correctness is
implied by recording an observed result. The `media` installation extra includes
the FAT and LittleFS readers.

For a CRC-valid `data/INSTALL` v1 marker, content comparison excludes the
installation timestamp and its dependent CRC. Inventories retain the raw SHA256,
`installedAt`, and a separate `comparisonSha256`. Every other byte remains part
of the comparison; invalid markers receive no normalization. Image comparison
always checks raw image and partition hashes.

Debugger stop polling uses QMP and register reads, without issuing RSP `?`.
QEMU interprets that packet as an initial attachment and clears all breakpoints;
using it on every stop can invalidate launch hooks and fault monitoring.

Python profiling accepts `stop_event=threading.Event()`. A concurrent fault
monitor can set it to finish sampling immediately and save the partial report
(`stopped_early: true`), rather than wait out a capture of a halted target.

`dbg.symbols.source_locations([pc1, pc2, ...])` resolves runtime addresses
(including relocations and inline callers) in a batch. Profiling uses this
interface, loading each ELF through addr2line once instead of once per hotspot.

For frame-driven GWemu input schedules, `dbg.key_event("GAME", True)` presses
a button and `dbg.key_event("GAME", False)` releases it. These use structured
QMP events rather than a wall-time hold; callers must release held keys in
cleanup. `dbg.key("GAME", hold_ms=600)` remains the timed tap interface.


Execution control through the Python debugger and shared QMP interface is
recorded in `runtime/gwprov/control.jsonl` beside the monitor socket. Each halt,
resume, reset or quit request records its transport, process ID, timestamp and
Python caller locations. Received STOP/RESUME events are recorded separately.
This identifies which automation issued a stop; polling memory and registers
adds no control-log traffic. An emulator reporting `running` does not establish
that a guest game's simulation is advancing: pair it with project progress
counters or a project-provided Application state.

For repeatable cohorts, `gwemu start/run --gwemu-bin /absolute/path/to/gwemu`
selects a developer's immutable executable without changing PATH. The daemon
validates it and confirms the running executable's SHA-256. Linux identity is
read from `/proc/PID/exe`, preserving the running inode even when a sibling
build replaces its pathname. A daemon predating this option fails explicitly;
finish its active operations before restarting it.

`dbg.symbols.compact_debug(output, elf=...)` writes a separate ELF using native
`SHF_COMPRESSED` debug sections. It verifies every PT_LOAD byte and address,
records source/output hashes, and never replaces an existing file. GNU
addr2line and pyelftools can read this format directly, including DWARF member
paths, inline source, and CFI. Preserve the original build hash/archive and the
returned provenance when using a compact ELF as a debug artifact.
`dbg.symbols.compilation_units(elf=...)` exposes each CU's ordered source,
compilation directory, DWARF version and original compiler producer string.
Use these strings to audit actual optimization options, including options
introduced by SDK makefiles rather than a project's named flag manifest.

Managed GWemu launches also support `--timing-mode baseline` for the experimental
one-instruction/one-cycle DWT timeline, `--icount SHIFT`, and `--rtc-epoch` for a
repeatable RTC seed. This baseline does not predict Cortex-M7 cache or memory
placement costs; see [timing controls](docs/DAEMON.md#repeatable-gwemu-timing)
before interpreting its cycle values as performance evidence.

The structured debugger's `dbg.memory_path("global.member")` resolves a C
member path through DWARF and live intermediate pointers, returning address,
size, and type. `dbg.read_path("global.member")` decodes that value. Final
pointers remain numeric, null intermediate pointers fail explicitly, and
member offsets are read from the owning ELF rather than supplied manually.
These operations work through the existing hardware or GWemu backend.
Local ELF symbols carry their STT_FILE source hints into type lookup. A hint
selects only a unique matching DWARF compilation unit; missing, ambiguous or
unmatched hints fall back to the original full scan. The cached CU index keeps
primitive offsets rather than live DIEs/streams. Loading an ELF invalidates
these indexes and compiled layouts. This avoids repeated full scans for late
static counters while preserving decoded types and explicit lookup errors.

Global type lookup indexes located DWARF variable definitions once per owning
ELF identity (device, inode, size and modification time). The index retains only
primitive CU/DIE offsets, source names and link-time addresses, never open ELF
streams. Original ELF symbol addresses disambiguate definitions before runtime
rebasing; unique source hints provide a fallback. Standalone declarations and
block-local shadows cannot substitute for a global definition. Located
`DW_AT_specification` definitions may inherit their declaration's name/type;
ambiguous or address-mismatched definitions fail explicitly. Reloading symbols
invalidates these indexes and typed member plans.

`dbg.read_paths(["global.member", "other.field"], errors="collect")` batches
DWARF layout planning and exact adjacent or overlapping memory reads. It returns
ordered `values`, explicit `errors`, read counts, byte counts and covered regions.
Pointers are refreshed for every call; read unions never include unrequested
gaps. `errors="raise"` is the default, and per-value/total byte limits fail loudly.
The caller owns halt/coherence when requesting an atomic snapshot. Loading an
ELF invalidates layout caches; symbol ownership and live rebasing remain explicit.

Debug descriptors may define conditional sampled gauges, independently of
monotonic progress counters:

```json
{"schemaVersion": 1, "sampleGauges": [
  {"symbol": "virtual_pc", "whenFunctions": ["interpreter_*", "dispatch_*"]}
]}
```

`dbg.profile(sample_gauges=[...])` accepts the same descriptors. Gauge values
retain zero and nonmonotonic observations, are counted only when the native
function matches a supplied glob, and are reported as value histograms with
explicit eligible sample counts. Missing or nonscalar symbols fail before
sampling. Empty `whenFunctions` samples the gauge at every running snapshot.

Profile reports retain both native machine-symbol shares (`functions`) and
DWARF source shares (`inline_functions`). The latter assigns each saved PC to
the innermost available inline function, with source-coverage counts. Both are
PC sample shares, not cycle measurements; source attribution adds no VM polls.

Managed instance metadata includes `binaryIdentity`: actual executable path,
SHA-256, byte size, and process arguments captured once at launch. Reports
should retain it with timing mode, workload, and project build identities when
comparing experimental emulator builds.

`dbg.step()` consumes the remote GDB single-step stop reply directly. It does
not leave the target running while waiting for a separate status query.

If a GDB transport failure prevents ordinary diagnosis,
`dbg.diagnose_qmp(path)` captures a halted VM's status, registers, unwind,
source context, SCB fault registers, and PNG through QMP. It requires an
intentionally halted target and does not resume or halt it implicitly.

`--timing-mode experimental-m7` opts into GWemu's provisional Cortex-M7 issue/dependency model (`cortex-m7-arm-cpu.x-gnw-m7-cycle-model=on`), with precise icount shift 0 and DWT timing enabled. This is a separate comparison cohort: fractional issue, limited dependency/latency rules and baseline fallback are modeled; cache, NOR/XIP and full load/store timing remain unvalidated. It is not calibrated hardware cycle timing. Unsupported binaries fail at launch; no test-only scaling knobs are exposed.

Native PC reports classify resolved routines, unresolved addresses and Cortex-M exception-return tokens using the captured PC/LR/xPSR. Corroborated SysTick/PendSV return snapshots are identified as transient control context, never reported as a routine or proof of a fault. No additional target polling is required.

`gwprov.profiling.frame_clock_metrics(frame_count=..., elapsed_cycles=...,
clock_hz=..., excluded_wait_cycles=...)` separates elapsed-clock paced frame
rate from an unpaced work-capacity projection. Supply a stable clock and matched
frame/cycle window. DWT cycles may include virtual timer advancement during
WFI; they are not necessarily retired instruction counts. Removing an explicitly
measured wait does not prove sustained paced FPS or hardware performance. The
summary retains both rates, target cycle budget and required work reduction.

### Architecture-aware ELF inspection

`SymbolTable.disassembly_tool(symbol)` reports the owning ELF machine and the selected binutils executable/version. `disassemble(symbol)` selects a tool for that architecture, including host executables used to audit emulator timing. Unsupported ELF machines, missing tools, and unsupported binutils targets fail explicitly. Native-tool fallback does not assert that every cross target is supported. ELF bytes and runtime rebasing remain unchanged.

### Watchdog pre-reset capture on hardware

For STM32 Retro-Go firmware that includes the WWDG interrupt symbol, capture the
interrupted application before the watchdog reset erases its live stack:

```text
gwprov debug python --target hardware --probe-id PROBE_ID --profile PROFILE
>>> dbg.reset(halt=True)
>>> dbg.arm_watchdog_breakpoints()
>>> dbg.resume()
>>> report = dbg.wait_watchdog(timeout=180)
>>> report["watchdog"]
```

The stop is at `WWDG_IRQHandler`, before its prologue changes the exception
frame. GWProv decodes the Cortex-M stacked PC/LR/xPSR and resolves the interrupted
source and unwind when symbols are available. A timeout leaves the target
running. A captured watchdog stop remains halted for inspection; resume it
explicitly. This is distinct from `arm_fault_breakpoints()`: a WWDG reset is not
a HardFault, and the post-reset BSOD cannot recover the pre-reset call stack.
