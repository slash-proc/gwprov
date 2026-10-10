# Release content to a bootable GWemu instance

Run these commands from the gwprov checkout. Generated files belong in the ignored
`dev-local/` directory. Source ROMs, firmware and CD files are read as inputs.
The commands download released artifacts and execute the published WASM converters
using Wasmtime's Python bindings; they do not require a Retro-Go source build.
On macOS, install Python and mtools with Homebrew (`brew install python@3.11
mtools`) and create the environment with `python3.11 -m venv dev-local/venv`
instead of `python3` below; Apple's system Python is too old. GWemu must be
installed separately and available on PATH.

## Install the Python tools

```bash
python3 -m venv dev-local/venv
source dev-local/venv/bin/activate
python -m pip install -r requirements.txt
```

`requirements.txt` selects the `dist` and `media` extras from `pyproject.toml`.
The final launch also needs `gwemu` on PATH; a wrapper named `gwemu`
is supported. The package depends on gnwmanager and reuses its bootloader release bindings.

## Download firmware and install projects

The default firmware repository is `sylverb/game-and-watch-retro-go-sd`.
`--variant flash` selects the published bank-2 firmware. `--version TAG` can
pin a firmware or project release; resolved versions and inputs are recorded in
local metadata. Use a new content root when choosing a different firmware/variant.

```bash
gwprov retro-go install --variant flash --output dev-local/content/retro-go-demo

gwprov project install smw --variant flash --output dev-local/content/retro-go-demo \
  --input-dir "$HOME/Emulation/Roms/Super Nintendo"

gwprov project install zelda3 --variant flash --output dev-local/content/retro-go-demo \
  --input-dir "base=$HOME/Emulation/Roms/Super Nintendo" \
  --input-dir "language=$HOME/Emulation/Roms/Super Nintendo"

gwprov project install openlara --variant flash --output dev-local/content/retro-go-demo \
  --input-dir "/path/to/tomb-raider-cd/DATA"

gwprov project install doom --variant flash --output dev-local/content/retro-go-demo

gwprov project install gba --variant flash --output dev-local/content/retro-go-demo \
  --firmware-dir "/path/to/firmware" \
  --game "gba=$HOME/Emulation/Roms/Game Boy Advance/Example Game.zip"
```

SMW finds its known US ROM in the directory. Zelda 3 independently selects its US
base and supported translation variants by SHA-1. Translated files are optional;
no translated ROM means only the base assets are produced. To supply German and
French explicitly, repeat `--input language=FILE` for the two recognized dumps,
alongside `--input base=FILE`. The supplied directory used for the verified run
contained no recognized German or French variants, so those conversions are not
yet verified against real inputs.

OpenLara needs the CD's `DATA` directory, where its `.PHD` levels live. Doom includes
release-verified shareware without a user WAD. GBA finds and validates `gba_bios.bin`
and unpacks the selected single-ROM ZIP into the game's inner filename.

ZIP inputs follow the web builder's rules: exactly one non-directory entry; stored
or deflate compression; no encryption; use the inner basename; validate unpacked
size and extension. A ZIP with multiple files is refused for explicit inputs and
skipped with a warning during directory scans. If a core declares `.zip` as a
native extension, the ZIP itself is retained. Directory scans ignore unrelated
extensions and deduplicate identical converter inputs. Directories are not recursive.

For flash, release and project content is staged into `flash/frogfs/` and
`flash/littlefs/`. ROMs, homebrew, firmware assets and mapped core artifacts go to
FrogFS; ordinary cores, language resources and writable data go to LittleFS.
The ownership catalog retains every installed project instead of replacing the
previous project's metadata. You may reinstall a project's owned files;
conflicting unrelated files are refused.

Raw SNES ROM extensions under `/homebrews/` are normally filtered from FrogFS
to avoid treating arbitrary ROMs as homebrew payloads. When a GWRG homebrew
converter explicitly emits a raw ROM there, `profile create` preserves only
that project-owned path from `.gwprov-projects.json`; unowned `.sfc`, `.smc`,
`.fig` and `.swc` files remain filtered. SD content keeps the declared path
directly.

## Assemble and boot

```bash
gwprov profile create dev-local/profiles/retro-go-demo \
  --content dev-local/content/retro-go-demo

gwprov gwemu run --profile dev-local/profiles/retro-go-demo
```

## Inspect and edit device filesystems

`gwprov show` gives a concise resource overview. Use `gwprov show devices` for
the short device list and `gwprov ps` for detailed runtime state. Set a device
focus with the ID shown by `show devices`; single-device controls use it when
no explicit target is supplied:

```bash
gwprov show
gwprov show devices
gwprov set active gwemu:12345
gwprov gwemu screenshot
```

`gwprov config show` reads and decodes the active device's stored Retro-Go
`/CONFIG`, including its CRC status. GWemu reads the running instance's extflash
image; hardware reads its mapped LittleFS partition using the assigned profile's
layout. Hardware inspection takes a target lease and fails while another session
owns the probe. The command does not write flash or halt the target.

Before a hardware flow loads gnwmanager's RAM service, GWProv reads VTOR,
mailbox status at `0x24025800`, and DHCSR. It follows gnw-web-builder's
Recovery Mode signals: `IDLE` plus a running core confirms a live idle stub,
while an SRAM VTOR identifies a stub that is busy or otherwise not confirmed
idle. A resident stub blocks another implicit reset/load. Once the mailbox is
idle, run `gwprov device recover` to reset and boot bank 1. GWProv refuses this
recovery while the mailbox is busy or unreadable.
The target lease also keeps a durable recovery marker from the moment the RAM
service is loaded until return to bank 1 is verified. If a process exits in
between, `gwprov ps` skips target reads and later hardware commands fail closed
until the explicit recovery succeeds. Recovery is refused during `ERASE`,
`PROG`, or `HASH`; it is allowed at `IDLE`, after a terminal service error, or
during `BOOTING` only before VTOR enters the stub's SRAM range.

Assigning a profile is separate from selecting the active device. `gwprov apply`
uses the active device's saved assignment: for GWemu it starts the assigned
profile and replaces the active VM when its profile differs; for hardware it
deploys the profile's flash regions. A registered SD card can be associated with
hardware so the profile's FAT files are overlaid onto that mounted folder as
part of apply.

```sh
gwprov set active probe:PROBE_ID
gwprov config show
gwprov config show --output json
gwprov set profile dkc1
gwprov sdcard add /Volumes/RETROGO       # name defaults to RETROGO
gwprov set sdcard RETROGO
gwprov apply
```

Register mounted folders or drive roots with `gwprov sdcard add PATH [NAME]`.
Names default to the folder name on Unix-like systems and the drive letter on
Windows. Registration records the path only; it does not format the card.
Inspect cards with `gwprov sdcard list` or `gwprov show sdcard`. Clear an
assignment with `gwprov set profile none` or `gwprov set sdcard none` before
removing a resource. The same `sdcard` command creates and populates raw card
images with `gwprov sdcard create` and `gwprov sdcard compose`; `sd` remains a
short alias for the full command group.

`adapters` and `devices` are also top-level commands. `adapters` lists detected
local programmers and registered remote servers. Register one remote
`gnwmanager serve` endpoint per adapter; each server represents one device:

```bash
gwprov adapters add pi-probe ws://10.2.3.122:8765/gdb
gwprov adapters list
gwprov devices
gwprov set active pi-probe
gwprov input tap B
gwprov adapters remove pi-probe
```

Pass `--origin ORIGIN` to `adapters add` when the server requires an Origin
header. The per-user adapter registry uses the platform config directory;
`GWPROV_CONFIG_DIR` overrides it. `show adapters` and `show devices` remain
available as concise dashboard views.

Filesystem commands accept a managed profile or an explicit image. FrogFS
changes rebuild the filesystem and update a profile's declared FrogFS size;
direct images are rebuilt within their existing image or explicitly sized region.
LittleFS and SD changes are applied in place. `create` formats an empty
filesystem and requires `--force` when replacing existing data:

```bash
gwprov filesystem ls --profile retro-go-demo --target flash/ext
gwprov filesystem add assets/title.png --source ./title.png \
  --profile retro-go-demo --target flash/ext
gwprov filesystem remove assets/old.png --profile retro-go-demo --target flash/ext
gwprov filesystem create --profile retro-go-demo --target flash/ext \
  --filesystem frogfs --force
gwprov filesystem ls --profile retro-go-demo --target sdcard
```

Create standalone filesystem images with the concise size-in-MiB form. The
optional output directory defaults to the current directory, and filenames
default to `frogfs.bin`, `lfs.bin`, and `sdcard.bin`. Use `--force` to replace
an existing output:

```bash
gwprov fs create frogfs 2 dev-local
gwprov fs create lfs 2 dev-local littlefs-test.bin
gwprov fs create sdcard 128 dev-local
```

For scripts or custom image layouts, the explicit image form is also available.
FrogFS and LittleFS use a byte size; LittleFS also takes `--offset` and
`--block-size`. SD creation uses `--size-mib`:

```bash
gwprov filesystem create --image dev-local/frogfs.bin \
  --filesystem frogfs --size 0x200000
gwprov filesystem create --image dev-local/littlefs.bin \
  --filesystem littlefs --size 0x200000 --block-size 4096
gwprov filesystem create --image dev-local/card.img \
  --filesystem sd --size-mib 128
```

For direct filesystem images, preserve any other data in the containing image
when choosing offsets and sizes. The `media`, `sdcard` (also available as `sd`), and `profile` commands
handle packing, card composition, and profile lifecycle tasks.

`profile create` checks project ABI requirements, builds both filesystems, relocates
mapped artifacts such as `gba.xip`, patches the released firmware's GWLB layout
with a fresh CRC, and assembles an extflash blob sized automatically to the smallest 64, 128 or 256 MiB
chip that fits. Use `--extflash-mib 128` (or 64/256) to pin the capacity; overflow
is refused. LittleFS stays at the top of the selected chip. LittleFS defaults to 2 MiB;
`--littlefs-mib` changes that partition. Its blocks are stored in the reverse order
expected by the firmware. Profile directories must be new, so creating an instance
cannot overwrite a previous test session.

A generated profile contains `bank1.bin`, `bank2.bin`, `extflash.bin`, `profile.toml`,
`provision.json`, and an isolated GWemu configuration. Bank 1 contains the official `sylverb/game-and-watch-bootloader` base-address
release asset, `gnw_bootloader.bin`, padded to the bank size. Version `v1.0.8`
is the default. `--bootloader-version latest` resolves the current release; an
explicit tag reuses its cached binary offline. `--bootloader-repo`
selects another repository, and `--bootloader-file PATH` accepts a local compiled
image linked at `0x08000000`. Release resolution and cached downloads use gnwmanager's shared Python bindings.
Gwprov validates the base-address vectors and bank size and records the resolved
version and computed SHA-256 in the profile. The download binding's default cache
check is non-empty content; the recorded hash is provenance, not an upstream
checksum verification. The cache is
`.gwprov-cache/` beside the content root. No source checkout or compiler is needed.
The bootloader starts valid bank-2 firmware when no updater or diagnostic action
is selected. This is a
standalone Retro-Go instance; a stock dual-boot image requires the separate stock
patch/bootloader provisioning flow. To create a pristine stock device profile,
use `gwprov profile create PATH --stock --backup-dir BACKUPS`.

Profiles are named directory-based device images. Bare names resolve under the
OS data directory: `%LOCALAPPDATA%\gwprov\profiles` on Windows,
`~/Library/Application Support/gwprov/profiles` on macOS, or
`$XDG_DATA_HOME/gwprov/profiles` on Linux (defaulting to
`~/.local/share/gwprov/profiles`). `GWPROV_PROFILE_DIR` overrides the default;
an explicitly set `XDG_DATA_HOME` is honored on any OS. `profile create
--output-dir` overrides the store for that creation. Explicit profile paths
remain supported.

Profile launches use the GWemu executable from PATH through the per-user GWProv
daemon. They start the guest running, use the profile's actual images, and
retain mutated media and RDP sidecar across clean exits. They do not attach a
probe or issue debug halts or resets. A config file under the profile disables
the first-run wizard; GWemu settings and runtime data stay under that instance.
Add `--headless` to omit the window or `--gdb-port PORT` for an optional
loopback debugger. The daemon keeps QMP private over stdio for state polling and
controls. GWemu stderr is saved in `gwemu.log`; see [the daemon design](DAEMON.md).

## Full Doom inputs

All OpenLara levels plus the full Doom WADs exceed 64 MiB; automatic sizing now
selects a larger chip when these are combined. The current local GWemu binary
hard-codes 64 MiB, so larger images require emulator capacity support before boot.
For a 64 MiB instance dedicated to the full Doom collection:

```bash
gwprov retro-go install --variant flash --output dev-local/content/doom-full
gwprov project install doom --variant flash --output dev-local/content/doom-full \
  --input-dir "/path/to/doom-wads"
gwprov profile create dev-local/profiles/doom-full --content dev-local/content/doom-full
gwprov gwemu run --profile dev-local/profiles/doom-full
```

This includes the shipped shareware alongside converted `doom.wad` and `doom2.wad`.
Select games explicitly for flash-sized profiles. A whole GBA library can be larger
than the device's storage; `--game-dir gba=DIR` handles eligible loose and single-ROM
ZIP files but does not choose a subset that will fit automatically.

## Verification

```bash
python -m unittest discover -s tests -v
```

The focused tests cover ZIP identity and refusals, size checks, layout CRC and
ambiguity, real FrogFS/LittleFS image creation, mounted LittleFS contents, preserved
source files, and existing-profile protection. Real release conversion and emulator
boot checks are additional integration checks; generated reports and screenshots
remain in `dev-local/`.

The local integration run also booted `retro-go-all` (SMW, Zelda 3 US, OpenLara,
Doom shareware, GBA) and `doom-full` (shareware, Ultimate Doom, Doom II) to the
Retro-Go menu without a debugger pause. Screenshots are stored as `boot.png`
inside those local profile instances. This verifies provisioning and menu boot;
it does not establish gameplay correctness for every title.
