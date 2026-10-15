# Build, stage, and provision Retro-Go content

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

### Build Retro-Go and run a local homebrew

From a homebrew project checkout, build the Retro-Go base into a staging directory,
build the homebrew with its own build instructions, install its GWRG dist bundle into
the profile, then set Retro-Go to launch it:

```bash
RETRO_GO=/path/to/game-and-watch-retro-go-sd
CONTENT="$PWD/build/retro-go-content"
PROFILE=dev-homebrew

# Use --profile PROFILE instead when an existing profile should choose SD_CARD.
gwprov retro-go build --variant flash --path "$RETRO_GO" \
  --output "$CONTENT" --makevar MY_EXPERIMENT=123

# Build the homebrew using its documented build command.

gwprov profile create "$PROFILE" --content "$CONTENT"
gwprov project install "$PWD/build/homebrew-bundle.zip" profile:"$PROFILE"
gwprov retro-go config profile:"$PROFILE" --auto-start /homebrews/my-app.bin
gwprov start "profile:$PROFILE"
```

`gwprov start` accepts a device name or `profile:NAME`. Bare names resolve as
devices; `profile:` launches a new GWemu instance from that profile. Starting a
physical device is unsupported. Use `gwprov device DEVICE reset` to reset
hardware; `start` does not power-cycle or restart it.

`retro-go build` supplies the firmware, debug ELF, storage artifacts, and local
manifest. The `--makevar` value is passed to Retro-Go's Makefile. GWProv does not
infer or run an arbitrary homebrew's build command: prepare the project bundle using
the GWRG dist manifest and ZIP layout first. `project install` then verifies its
declared files and installs them into the profile. To run on hardware, apply the
profile with `gwprov profile PROFILE apply device:DEVICE --start --bank 2` instead
of starting GWemu.

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

gwprov start profile:dev-local/profiles/retro-go-demo
```

## Inspect and edit device filesystems

`gwprov show` gives a concise resource overview. Use `gwprov show devices` for
the device list and `gwprov show DEVICE` for one device's assignments, detected
bank contents, bootloader, running application and lock state. Hardware details
include the target and adapter; GWemu details include its process, timing, QMP
and GDB endpoints. Use `gwprov ps` to compare live state across all devices.
Device controls always take the device name or stable ID shown by `show devices`:

```bash
gwprov show
gwprov show devices
gwprov show timing-test
gwprov show device:probe:SERIAL --output json
gwprov device gwemu:12345 screenshot --output screenshot.png
```

`gwprov retro-go config device:DEVICE` reads and decodes a device's stored Retro-Go
`/CONFIG`, including its CRC status. GWemu reads the running instance's extflash
image; hardware reads its mapped LittleFS partition using the assigned profile's
layout. Hardware inspection takes a target lease and fails while another session
owns the probe. The command does not write flash or halt the target.

Before a hardware flow loads gnwmanager's RAM service, GWProv reads VTOR,
mailbox status at `0x24025800`, and DHCSR. Recovery Mode requires VTOR to
point into the RAM service; `IDLE` plus a
running core then confirms that it is live and idle. The mailbox can retain a
stale `IDLE` value while application firmware runs, so it cannot identify the
stub by itself. A resident stub blocks an implicit reset/load by flows that
need to start the service. `gwprov profile PROFILE apply device:DEVICE` can attach to an already-running idle
stub.
`gwprov device DEVICE recover` starts the RAM flasher when absent, or attaches and
synchronizes its context counter when already live and idle. It restarts an
idle but halted or terminal-error stub; it refuses a busy or unreadable
resident service. Recovery leaves the device in Recovery Mode ready for commands.
`gwprov ps` reports `BUSY` while a substantial process holds the target lease;
otherwise it reports the observed running, halted, or unknown state. Recovery
state is never represented by a durable host-side marker: the daemon reads the
target's VTOR, mailbox, DHCSR, and firmware signatures on its next poll. The
application column identifies the gnwmanager Recovery stub when
its live state is confirmed. VTOR, mailbox, DHCSR, and firmware signatures
distinguish the stub from a transient SRAM application, and identify stock
Mario/Zelda, bootloader, or Retro-Go version when signatures are readable.
Recovery is refused during `ERASE`, `PROGRAM`, or `HASH`.

Before starting the RAM service, recovery and deployment read the complete
gnwmanager firmware image back from SRAM and verify MSP/PC register writes. A
failed verification leaves the core halted and reports the mismatch instead of
starting an unverified image.

Applying a profile names both resources: `gwprov profile PROFILE apply device:DEVICE`.
For hardware it deploys the profile's flash regions and leaves the RAM flasher
running. Pass `--start` to start bank 1 afterward, or add `--bank 2` to start
bank 2. For GWemu, use `gwprov start profile:PROFILE` to create a managed instance.

```sh
gwprov retro-go config device:probe:PROBE_ID
gwprov retro-go config device:probe:PROBE_ID --output json
gwprov profile dkc1 apply device:probe:PROBE_ID
gwprov media sdcard add /Volumes/RETROGO       # name defaults to RETROGO
```

Register mounted folders or drive roots with `gwprov media sdcard add PATH [NAME]`.
Names default to the folder name on Unix-like systems and the drive letter on
Windows. Registration records the path only; it does not format the card.
Inspect cards with `gwprov media sdcard list` or `gwprov show media`. The same
`media sdcard` group creates and populates card
images with `gwprov media sdcard create` and `gwprov media sdcard compose`; `sd` remains a
short alias for the full command group.

`adapters` and `device ls` are also top-level commands. `adapters` lists detected
local programmers and registered remote servers. Register one remote
`gnwmanager serve` endpoint per adapter; each server represents one device:

```bash
gwprov adapters add pi-probe ws://HOST:PORT/gdb
gwprov adapters list
gwprov device ls
gwprov device pi-probe input tap B
gwprov adapters remove pi-probe
```

Pass `--origin ORIGIN` to `adapters add` when the server requires an Origin
header. The per-user adapter registry uses the platform config directory;
`GWPROV_CONFIG_DIR` overrides it. `show adapters` and `show devices` remain
available as concise dashboard views.

Raw GDB TCP servers use a `gdb://HOST:PORT` endpoint and are registered as
named devices. For example, an OpenOCD GDB server:

```bash
gwprov adapters add test-target gdb://HOST:PORT
gwprov device ls
gwprov debug python test-target
```

The daemon polls registered raw GDB endpoints every five seconds and reports
observed execution and firmware state in `gwprov ps`. Polls read target state
without sending halt, continue, reset, or watchdog-freeze writes; shell
completion remains registry-only. Before an explicit Python debug session, the
daemon hands off its GDB connection. GWProv negotiates the target description
without execution-control writes. A GDB server may have its own connection
behavior that changes execution state. Raw GDB supports Python debugging, but
not GWProv's managed reset-and-project-autostart sequence. GWProv coordinates
its own local clients with a process lease; the GDB server controls access for
clients on other machines.

To let Web Builder or another gnwmanager WebSocket client connect through
GWProv, start a local bridge for the explicit device and pass its printed URL
to that client:

```bash
gwprov serve device:retro-go-vm start
gwprov serve device:retro-go-vm stop
```

The bridge uses the gnwmanager WebSocket protocol on `/gdb`, binds to loopback
by default, and chooses the first free port starting at 8766. GWemu GDB traffic
uses its discovered GDB endpoint; QMP is used separately to read VM state.

Retro-Go has two content layouts. The flash variant stores files in FrogFS and
LittleFS on NOR. The SD variant stores its files together on the FAT filesystem
of the SD card; NOR is used as a cache for contiguous memory-mapped files.
Choose the filesystem that actually stores the target's content.

`gwprov media add TARGET [SOURCE]` accepts a device or profile name, or a
standalone filesystem image. Prefix managed targets with `device:` or `profile:`
when needed. Scoped forms are `gwprov device DEVICE media add [SOURCE]` and
`gwprov profile PROFILE media add [SOURCE]`. The
source defaults to the current directory; `--dest` (also `--dst` or
`--destination`) defaults to `/`. Files are copied into the destination
directory, while a source directory is copied beneath it with its basename
preserved. Missing directories are created.

Storage is inferred for flash-only targets: `/bios`, `/fonts`, and `/homebrews`
go to FrogFS, and other paths go to LittleFS. `--contiguous` forces FrogFS and
stores added files uncompressed. `--sentinel ADDRESS` also uses contiguous
FrogFS storage and relocates sentinel-based pointers in one file to the file's
final extflash address; directory sources are rejected. `--sd` or `--sdcard`
selects SD storage and is mutually exclusive with `--contiguous` and
`--sentinel`. On SD Retro-Go targets, SD storage is selected by default.

Device writes ask before starting the recovery flasher if it is not already
running. `--yes`/`--force` skips that prompt. `ls` and `tree` accept either a
file or directory inside the selected filesystem:

```bash
gwprov profile retro-go-demo media tree /
gwprov profile retro-go-demo media add ./title.png --dest /homebrews
gwprov profile retro-go-demo media add ./my-homebrew --dest /
gwprov profile retro-go-demo media delete /assets/old.png --contiguous
gwprov device DEVICE media tree /CONFIG
gwprov device DEVICE media add ./settings.json --dest /config
gwprov device DEVICE media ls /data/roms/game.sfc --sdcard
```

Standalone filesystem images can be used as top-level media targets. GWProv
scans and validates their contents before listing or editing them. Use an
explicit `blob:TYPE` prefix when the file name does not identify its type:

```console
gwprov media /path/to/frogfs.bin add ./bios
gwprov media blob:frogfs /path/to/frogfs.bin tree
gwprov media /path/to/littlefs.bin add ./CONFIG --dest /
```

`--contiguous` is valid only for FrogFS images. It is rejected for LittleFS and
SD images; SD images also reject `--sentinel`.

FrogFS changes rebuild the image and update its declared size. Rebuilding a
profile with mapped XiP files preserves their raw payloads and relocates them
using their declared `relocBase`. Device FrogFS edits use the assigned profile's
layout and XiP metadata, then write the rebuilt FrogFS and its matching bank
layout. LittleFS edits operate in place. SD-card edits write files into the
card's shared namespace. Profile SD images support recursive directory creation.
The current hardware gnwmanager protocol can write files only into existing SD
directories; nested hardware SD imports need its SD mkdir action. Device changes
show a confirmation; use `--yes` to skip it.

Top-level media file operations treat an unprefixed target as a device. Use
`profile:NAME` to select a profile explicitly. Device-specific actions live under
`gwprov device DEVICE`; profile-specific actions live under
`gwprov profile PROFILE`. `create` formats an empty filesystem and requires
`--force` when replacing existing data.
Formatting a profile that still declares mapped XiP artifacts is rejected:

```bash
gwprov profile retro-go-demo media tree /
gwprov media add device:DEVICE ./title.png --dest /homebrews
gwprov profile retro-go-demo media delete /assets/old.png --contiguous
gwprov media create --profile retro-go-demo --target flash/ext \
  --filesystem frogfs --force
```

Create standalone filesystem images with the concise size-in-MiB form. The
optional output directory defaults to the current directory, and filenames
default to `frogfs.bin`, `lfs.bin`, and `sdcard.bin`. Use `--force` to replace
an existing output:

```bash
gwprov media create frogfs 2 dev-local
gwprov media create lfs 2 dev-local littlefs-test.bin
gwprov media create sdcard 128 dev-local
```

For scripts or custom image layouts, the explicit image form is also available.
FrogFS and LittleFS use a byte size; LittleFS also takes `--offset` and
`--block-size`. SD creation uses `--size-mib`:

```bash
gwprov media create --image dev-local/frogfs.bin \
  --filesystem frogfs --size 0x200000
gwprov media create --image dev-local/littlefs.bin \
  --filesystem littlefs --size 0x200000 --block-size 4096
gwprov media create --image dev-local/card.img \
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

A profile contains the named component blobs and a `flash-map.yaml`. The map is
the hardware write plan: `bank1` and `bank2` are internal flash, while `bank0`
is external flash. Each entry has a display `name`, profile-relative `file`, and
offset within that bank; the blob's file length is the write length. For example:

```yaml
bank1:
  - name: Bootloader
    file: blobs/bootloader.bin
    offset: 0
bank2: []
bank0:
  - name: Mario Assets
    file: blobs/mario-assets.bin
    offset: 0
    size: 1048576
    sha256: "<64-character SHA-256 digest>"
```

Every entry records `size` in bytes and the full SHA-256 digest. Apply checks
both values against the blob before using the map.

Edit a profile's flash components with `profile edit`. Adding copies the input
file into the profile's `blobs/` directory, records its name, bank-relative
offset, byte size, and SHA-256 in `flash-map.yaml`, then rebuilds the padded
bank images used by GWemu. Existing parts with the same name require
`--replace`; removal names the bank and component shown in the map:

```sh
gwprov profile basic-test edit add bank0 "Mario Assets" mario-assets.bin --offset 0x0
gwprov profile basic-test edit add bank1 "Bootloader" bootloader.bin --offset 0 --replace
gwprov profile basic-test edit remove bank0 "Mario Assets"
```

`bank0` is external flash; `bank1` and `bank2` are internal flash. Blob ranges
must fit the existing composed bank image and cannot overlap another mapped
component. `-o json` provides structured edit results.

GWemu instances use composed bank images, with `0xFF` in gaps between mapped
blobs. The profile remains the reusable source; each emulator instance gets its
own composed and mutable images. Bank 1 contains the official `sylverb/game-and-watch-bootloader` base-address
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
patch/bootloader provisioning flow. Stock profile creation uses the backup registry;
see [Profiles: creation, editing, and deployment](PROFILES.md).

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
gwprov start profile:dev-local/profiles/doom-full
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
