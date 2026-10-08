# gwprov

`gwprov` is the Python provisioning and control library for Game & Watch Retro-Go images, GWemu, and physical devices. It is intended to be consumed as a Python package and as a Git submodule.

The initial package brings reusable target, SD-card, timeline, Retro-Go configuration, and build modules from Doug's own `../tinyemu` project into one package, alongside Retro-Go-SD's `REMOTE_INPUT` transport. Retro-Go build behavior is carried over as a compatibility wrapper; the build system itself remains owned by Retro-Go-SD.

## Install

Run from the gwprov checkout. Keep generated content, profiles and environments in
`dev-local/`, which is ignored by Git. The final launch requires `gwemu` on PATH;
a wrapper named `gwemu` on PATH is also supported.

```bash
cd /path/to/gwprov
python3 -m venv dev-local/venv
source dev-local/venv/bin/activate
python -m pip install -r requirements.txt

# Optional: use a local gnwmanager checkout while developing its bindings.
# python -m pip install -e ../gnwmanager

# Optional: enable Bash completion in this shell.
eval "$(gwprov completion bash)"
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
mapped FrogFS artifacts also declare their relocation base. Paths in `source`
are relative to the manifest unless absolute. Optional `sha256` values pin a
source file.

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
    ]
  }
}
```

`stage-local` writes the release install ownership marker, checks for path
collisions, and stages beside the content root. It does not publish the project
or run converters.

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

For a symbolized debug session, use `gwprov gwemu debug --profile "$profile"`.
The command starts visible GWemu stopped at reset so it can install breakpoints,
loads the matching `debug/retro-go-debug.elf` from the checksum-verified firmware
release, then continues execution. This reset halt is a debugger setup step; the
app does not enter Retro-Go's pause menu unless `start_paused` is left enabled.

Retro-Go `/CONFIG` homebrew autostart passes `start_paused=true` to
`run_gwhb_homebrew`. The default debug session breaks on that function only with
`--unpause-homebrew`, clears `r2` at function entry, and then resumes the app.
The app's normal launch behavior remains paused. The interactive debug mode also
sets fault breakpoints on the common Retro-Go and ARM fault handlers. On a hit,
it saves a text report containing the backtrace, registers, stack words, and
nearby instructions to `runtime/gwprov/fault-triage.txt` and leaves GDB stopped.
Use `gwprov gwemu pause --profile "$profile"` or `resume` to stop or continue
execution through QMP while GDB remains connected.

For an automatic launch-to-watch cycle, provide the app ELF so gwprov can resolve
`app_main`, detach at that entry point, and leave the VM running:

```bash
app_elf="$PWD/build/dkc1_gwrg/homebrew/dkc1_core.elf"
gwprov gwemu debug --profile "$profile" --gdb-port 12345 \
  --qmp-socket "$profile/runtime/gwprov/qmp.sock" \
  --unpause-homebrew --app-symbols "$app_elf" \
  --detach-after-app-entry --keep-running

gwprov gwemu watch --profile "$profile" --symbols "$app_elf" \
  --duration 30 --stall-after 3
```

`watch` works with any core or homebrew ELF. It always samples ARM PC, SP, and
LR; it also auto-detects common 32-bit progress counters (`heartbeat`,
`frame_count`, `frame_counter`, and `progress_count`) and guest-PC globals ending
in `_cur_pc`, `_current_pc`, `_resume_pc`, or `_guest_pc`. Projects can specify
their own symbols with repeatable `--progress-symbol`, `--guest-pc-symbol`, and
`--u32` options, and can include pointed-to memory with `--deref SYMBOL:SIZE`.
Fault-handler symbols are discovered from the loaded firmware and app ELFs.

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
Process management needs process-namespace visibility; QMP and GDB need AF_UNIX
and TCP connect access. Those are not inherently `NET_ADMIN` requirements. A
denied operation reports the socket and access type instead of appearing to be
an absent emulator.

For a persistent Python debugger attached to an already-running target:

```bash
gwprov debug python --target gwemu --port 1234 --profile "$profile" \
  --qmp-socket "$profile/runtime/gwprov/qmp.sock" \
  --symbols build/dkc1_core.elf
```

Run one GDB owner at a time; `watch` keeps one connection and briefly halts only
while it samples or captures target state. A DKC-specific invocation can select
its interpreter PC and useful counters explicitly:

```bash
gwprov gwemu watch --profile "$profile" --symbols "$app_elf" \
  --progress-symbol dkc1_gwrg_host_heartbeat \
  --progress-symbol dkc1_gwrg_frame_count \
  --guest-pc-symbol g_interp816_cur_pc
```

The `dbg` object stays connected for the whole REPL session. It provides
`dbg.halt()`, `dbg.resume()`, `dbg.step()`, `dbg.regs()`, `dbg.where()`,
`dbg.traceback()`, `dbg.diagnose()`, `dbg.read(addr, n)`, `dbg.u32(addr)`, and
hardware breakpoints with `dbg.bp(addr)`.
Thumb function symbols are normalized automatically for breakpoints and nearest
symbol lookup. Register and symbol-location reads restore the target's prior
running state.
With `--qmp-socket`, `dbg.screenshot([path])` saves a PNG beside the QMP socket
by default and returns its path, dimensions, `all_black`, and nonblack pixel
count. `dbg.key("START")` sends a mapped Game & Watch button, and `dbg.qmp(command,
arguments)` sends a structured QMP request. ELF symbols can
be queried with `dbg.at("symbol_name")` or `dbg.symbols.find("substring")`.
`dbg.nm("substring")` returns matching symbol rows with address, size, type,
section, and owning ELF; `dbg.disasm("function_name")` uses objdump on the
loaded ELF and returns that function's assembly listing.
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
`profile create` currently supports **flash** assembly; SD content can be staged
with `--variant sd`, but this profile-assembly workflow does not yet pack SD images.
Flash content lives in `flash/frogfs/` and `flash/littlefs/`. The generated profile
contains bank-1 and bank-2 images, an extflash image, configuration and provenance.

Bank 1 uses the official `gnw_bootloader.bin` linked at `0x08000000`, obtained with
gnwmanager's bindings. Bank 2 contains the released Retro-Go firmware. This is a
standalone Retro-Go instance; patched stock dual boot uses the separate OFW flow
and its `0x08032000` bootloader. No handcrafted bank-1 stub is used in new profiles.
The bootloader cache stays in `.gwprov-cache/` beside the content root, and the
profile records the resolved version and computed hash.

Capacity defaults to the smallest of **64, 128 or 256 MiB** that fits the packed
content and LittleFS partition. LittleFS defaults to 2 MiB at the top of the chip.
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
  --gdb-port 3333 \
  --qmp-socket dev-local/profiles/retro-go-demo/qmp.sock
```

The launcher uses `gwemu` from PATH, starts the guest running, and does not attach a
probe or pause it. GWemu stderr goes to the instance's `gwemu.log`. For file formats,
input constraints and verification details, see [the provisioning guide](docs/PROVISIONING.md).

## CLI

`gwprov tree` prints the command hierarchy and each subcommand's short help text. Use `-h` on
any listed command to see its full usage.

```sh
gwprov --help
gwprov tree
gwprov project list
gwprov project list --output json
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
gwprov retro-go config --output build/CONFIG --rom gb Tetris.gb
gwprov retro-go build --path references/game-and-watch-retro-go-sd --dry-run
gwprov media frogfs --retro-go-root references/game-and-watch-retro-go-sd [packer options]
gwprov ofw patch mario --source-tree ../qemu-gnw --backup-dir backup --output-dir build/ofw [patch options]
```

Enable Bash completion in the current shell with `eval "$(gwprov completion bash)"`. It loads
curated project names on the first project-argument completion and reuses them in that shell.

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
inputs are non-recursive so unrelated nested content is not consumed. Bash completion suggests
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
overwrite.

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
gwprov profile stock dev-local/profiles/stock-locked \
  --backup-dir /path/to/ofw-backups --locked
gwprov profile stock dev-local/profiles/stock-unlocked \
  --backup-dir /path/to/ofw-backups
```

Repeat `--backup-dir` for multiple backup sources; use `--model mario|zelda` to
select a device explicitly. Existing profile directories are never overwritten.

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
