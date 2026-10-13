# Profiles: creation, editing, and deployment

Profiles are reusable descriptions of firmware and data written to the device's
flash banks. A profile keeps the source files and a `flash-map.yaml`; GWProv uses
the map to describe each named component, its destination offset, size, and SHA-256.
GWemu receives composed bank images with erased (`0xFF`) padding. Hardware receives
each component at its mapped offset.

## Find stock backups

```console
$ gwprov backups search ~/Downloads
Found verified Mario backup pair: /home/me/Downloads/ofw-backup
$ gwprov backups list
```

`backups search [DIRECTORY]` searches recursively, defaulting to the current
directory. `backups list` (also `ls`) shows previously discovered valid backup
pairs and their locations.

## Create stock profiles

```console
gwprov profile create mario-stock -m
gwprov profile create zelda-stock -z
gwprov profile create mario-patched -m -p
gwprov profile create zelda-patched -z -p
gwprov profile create mario-special -m -p -b
```

`-m/--mario` and `-z/--zelda` select the stock variant. `-p/--patched` applies
the standard stock patch. Adding `-b/--bootloader` also maps the SD bootloader
at bank 1 offset `0x32000`; this is the image stock firmware uses for the
Game+Left SD boot path. The apply summary names both the patched stock image
and the bootloader as separate components. The default bootloader is the
vendored `sylverb/game-and-watch-bootloader` v1.0.8 asset linked at
`0x08032000`; `--sd-bootloader-file` accepts a replacement compiled for that
address. `--bootloader-file` is for the separate base-address bootloader used
by content-based profiles. Backup pairs discovered with `gwprov backups
search` supply the original images. Choose either `-m` or `-z`; when neither
is specified, the profile contains no stock firmware.

## Add Retro-Go

```console
gwprov retro-go source add slash-proc/game-and-watch-retro-go-sd
gwprov retro-go source add https://repo.link/dist-parent/versions.json
gwprov retro-go source list
gwprov retro-go source default slash-proc/game-and-watch-retro-go-sd
gwprov retro-go list --sort version
gwprov profile create dualboot-rg -z -p -b -r slash-proc:v2.0.0-rc15 --littlefs 4
```

Sources may be GitHub `owner/repo` names or HTTPS URLs to a `versions.json`
index. A release argument must exactly match a registered entry from
`gwprov retro-go list`, in `SOURCE:VERSION` form. `retro-go list` also accepts
`ls`; `--sort` selects the displayed ordering. `--littlefs SIZE_MB` chooses the
LittleFS capacity for flash builds.

To use a locally staged content root (for example one populated by
`gwprov retro-go install` and `gwprov project install`), pass `--content DIR`.
The staging directory contains `.gwprov-firmware.json`, which identifies the
release, variant, and owned files. It is not a profile and can be reused while
assembling multiple profiles.

Projects can also be installed directly into an existing profile. This updates
the profile files only; it does not connect to or flash hardware. Use
`deploy apply` afterward to write the updated profile:

```console
gwprov project install doom --profile dualboot-rg
gwprov deploy plan dualboot-rg
gwprov deploy apply dualboot-rg

# Preview detected files and destinations without changing the profile
gwprov project install doom --profile dualboot-rg --dry-run
```

`--variant` is optional. GWProv infers it from the profile's provisioning
metadata or SD-image configuration, from the active device's assigned profile,
or from a target directory's firmware metadata or existing `flash/` and `sd/`
subdirectories. A release that publishes only one storage variant also supplies
an unambiguous default. If these clues do not identify one choice, pass
`--variant flash` or `--variant sd`.

For SD projects, GWProv selects `sd` from a profile configured with a bundled
SD image; `--variant sd` remains an explicit override. The target profile must contain a
bundled SD image. Projects with mapped XiP artifacts must be included while
creating the profile so GWProv can compose their relocation data together.

### Install from an offline bundle ZIP

The ZIP must contain the contents of one published `dist/<tag>/` directory at
its root. It may contain `versions.json` plus the release manifest, or a lone
root `manifest.json`. GWProv reads the newest index entry when `versions.json`
is present, resolves the manifest's filenames beside that manifest, and checks
the size and SHA-256 of every declared payload before installing. Unnamed ZIP
entries are ignored and never extracted. Unsafe ZIP paths, missing files,
duplicate names, malformed metadata, or checksum mismatches reject the bundle.

```console
gwprov project install ./doom-v1.2.0.zip --profile dualboot-rg
gwprov deploy plan dualboot-rg
gwprov deploy apply dualboot-rg

# Install and write to the active device's assigned profile in one prompted action
gwprov project install ./doom-v1.2.0.zip --device
```

`--device DEVICE` can name a device; with no value it uses the active device.
That device must have a managed profile assigned. `--yes` accepts the write
without prompting. For an SD project, the profile needs a bundled SD image; a
direct device install also requires an assigned registered SD card, which gets
the project files overlaid while retaining unrelated files.

`--dry-run` shows the resolved project and storage variant, each file's size,
and where it would be placed in the staging directory, profile, or assigned SD
card. With `--device`, it reads the saved assignment but does not poll or
connect to hardware. Add `--format json` for structured output.

Projects with mapped XiP artifacts, such as GBA cores, are placed in FrogFS
uncompressed and relocated when that filesystem is composed. Newly created
profiles keep the raw mapped sidecars so a later project install can safely rebuild FrogFS;
hardware deployment uses the updated extflash component map. ROM inputs still
stay local and must be supplied to the converter, including for an offline
bundle:

```console
gwprov project install ./project-bundle.zip --profile dualboot-rg \
  --input base=./supported-rom.sfc --dry-run
gwprov project install ./project-bundle.zip --profile dualboot-rg \
  --input base=./supported-rom.sfc
gwprov deploy plan dualboot-rg
gwprov deploy apply dualboot-rg
```

Mapped payload placement depends on the final FrogFS composition, so the dry-run
reports the relocation base and that the runtime XiP address is assigned during
composition. It also shows the pointer range and relocation rule: aligned
32-bit words whose Thumb-bit-cleared values point inside the link-time payload
range receive the delta `runtime address - relocBase`, preserving the Thumb bit.
The runtime address is `0x90000000 + FrogFS offset + payload offset`; the exact
address and number of adjusted pointers require the composed filesystem. When
an older profile has no retained sidecar, GWProv reconstructs
the original mapped bytes from the existing uncompressed FrogFS payload before
rebuilding it.

Bundle checksums prove the archive is internally consistent, but they do not
authenticate the publisher. The install result labels this source accordingly.
Use `--format json` for a structured result suitable for scripts.

## Import custom images

```console
gwprov profile create custom-profile ./images
```

Without `flash-map.yaml`, the import recognizes `bank0.bin`, `bank1.bin`,
optional `bank2.bin`, and `sdcard.img` or `sdcard.qcow2`. `bank0` is external
flash (extflash). Offset filenames such as `bank0@0.bin`,
`bank0@0x400000.bin`, and `bank0@1M.bin` set the component's byte offset. A
plain `bank0.bin` starts at offset zero. When supplied, `flash-map.yaml` is the
authoritative mapping and uses this form:

```yaml
bank1:
  - name: Stock Mario
    file: files/mario-bank1.bin
    offset: 0
    size: 262144
    sha256: 0123456789abcdef...
bank2: []
bank0:
  - name: Mario Assets
    file: files/mario-assets.bin
    offset: 0
    size: 1048576
    sha256: fedcba9876543210...
```

The map uses `bank1`, `bank2`, then `bank0` (extflash). Each entry names the
component and its source file, byte offset, byte size, and SHA-256. `bank0` and
`extflash` are aliases and cannot both appear. Components in a bank may not
overlap. Bank 1 and bank 2 are each limited to 256 KiB. Extflash components must
fit the profile's declared chip size, the detected hardware capacity at apply
time, and the 256 MiB addressable limit.

## Edit a profile

```console
gwprov profile edit mario-stock add bank0 "Mario Assets" ./assets.bin --offset 0
gwprov profile edit mario-stock add bank2 "Retro-Go v2.0.0-rc15" ./retro-go.bin --offset 0
gwprov profile edit mario-stock remove bank0 "Mario Assets"
gwprov profile edit mario-stock --sdcard ./sdcard.img
gwprov profile edit mario-stock --no-sdcard
gwprov profile edit mario-stock --sdcard-size 256
```

Adding or replacing a flash component updates its map and composed images. An
overlap or bank-size violation is rejected before the profile is changed. SD
card settings are independent of flash banks; no SD card is included unless
requested. `--sdcard-size SIZE_MB` creates/resizes the bundled card and implies
`--sdcard`; it cannot be combined with `--no-sdcard`.

Retro-Go's `/CONFIG` is changed inline; there is no separate config editor or
config build step:

```console
gwprov retro-go config PROFILE
gwprov retro-go config --profile PROFILE --auto-start /homebrews/demo.bin --timeout 0
gwprov retro-go config --profile PROFILE --no-auto-start
```

For hardware, use its device name or stable ID instead of `PROFILE`. GWProv
updates the assigned profile's LittleFS image and deploys only that region after
the normal write confirmation. Add `--yes` to skip the confirmation. Use
`--output FILE` to save the resulting 416-byte CONFIG blob.

## Plan and flash

```console
gwprov set active probe:PROBE_ID
gwprov deploy plan mario-stock
gwprov deploy apply mario-stock
```

Review the plan's target bank, component names, offsets, and human-readable sizes
before applying. Hardware writes each component directly at its mapped offset;
GWemu uses the corresponding composed bank images.
