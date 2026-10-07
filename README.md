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

### Create the profile and launch GWemu

```bash
gwprov profile create "$profile" --content "$content" \
  --bootloader-version v1.0.8

gwprov profile show "$profile"

# Final command: launch the persistent instance with a window.
gwprov gwemu run --profile "$profile"
```

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
