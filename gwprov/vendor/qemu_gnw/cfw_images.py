#!/usr/bin/env python3
"""
make_cfw_images.py -- produce the patched bank1/extflash images
scripts/boot_qemu.sh --patched expects, by running gnwmanager's real
firmware-patch pipeline offline (no device, no flashing) against the
user's own stock firmware dumps in backup/.

This replaces a previous approach of manually extracting these images
from an archived gnwmanager CFW-patch output -- it now reproduces that
same output directly, reading whatever stock firmware dumps the user
already has in backup/ (same inputs make_boot_images.py uses).

Uses gnwmanager's flash-patch defaults exactly as `gnwmanager flash-patch
<game> --bootloader` would produce them (bootloader=True, every other
flag at its CLI default) -- deliberately not customized, so the image
matches what gnwmanager naturally produces, not a bespoke variant. The
`--bootloader` space reservation is used for its internal-firmware-layout
side effect only; no SD bootloader binary is fetched or written anywhere
(gnwmanager's own flash_bootloader() is a real-hardware/network step,
not applicable here).

Requires gnwmanager importable (pip install gnwmanager, or set
GNWMANAGER_PATH to a local checkout) -- see the same pattern already used
in scripts/state_transplant.py / scripts/hotloop_compare.py. Only
gnwmanager's offline firmware-patch object model is used
(gnwmanager.cli.gnw_patch.*); no device/GDB/OpenOCD backend is touched.

--retro-go: retro-go is a separate firmware with TWO independent
components, not one combined file (stock/CFW firmware -- "OFW" -- only
ever runs from bank1, so retro-go never touches bank1):
  1. bank2 (internal flash, 0x08100000): the retro-go firmware/launcher
     binary itself. Written verbatim via --retro-go-bank2.
  2. extflash: game ROM/asset data that retro-go's non-SD builds embed
     directly into external flash at a build-fixed offset -- 0x100000
     (1MiB) for mario builds, 0x400000 (4MiB) for zelda builds, per
     project convention (retro-go's own build parameters aren't recorded
     anywhere retrievable, so this is a by-convention assumption, not
     something verified from the file itself). Written via
     --retro-go-extflash [--retro-go-extflash-offset].

Exception: game-and-watch-retro-go-sd (the SD-card variant) loads ROMs
from an SD card at runtime instead of baking them into extflash, so that
variant has bank2 only -- omit --retro-go-extflash entirely for it.

Usage:
    ./scripts/make_cfw_images.py mario
    ./scripts/make_cfw_images.py zelda --retro-go-bank2 /path/to/retro-go.bin
    ./scripts/make_cfw_images.py mario --retro-go-bank2 retro-go.bin \\
        --retro-go-extflash roms.bin --retro-go-extflash-offset 0x100000

Output: backup/qemu-images/<game>-bank1-patched.bin,
        backup/qemu-images/<game>-extflash-patched.bin,
        and (only if the corresponding flag is given)
        backup/qemu-images/<game>-bank2.bin
"""
import argparse
import importlib.resources
import os
import re
import sys
from pathlib import Path


GNWMANAGER_PATH = os.environ.get("GNWMANAGER_PATH")
if GNWMANAGER_PATH:
    sys.path.insert(0, GNWMANAGER_PATH)
try:
    from gnwmanager.cli.gnw_patch.mario import MarioGnW
    from gnwmanager.cli.gnw_patch.zelda import ZeldaGnW
except ImportError:
    sys.exit(
        "error: gnwmanager not importable. Install it (pip install gnwmanager) "
        "or set GNWMANAGER_PATH to a local checkout."
    )

DEVICE_CLASSES = {"mario": MarioGnW, "zelda": ZeldaGnW}

# Per-game "where does retro-go's own payload start" convention -- see
# module docstring. Not derived from anything in the file itself.
RETRO_GO_OFFSETS = {"mario": 0x100000, "zelda": 0x400000}


def read_define(name: str, soc_h: Path) -> int:
    text = soc_h.read_text()
    m = re.search(rf"#define\s+{name}\s+\(?([^)\n]+)\)?", text)
    if not m:
        raise ValueError(f"could not find {name} in {soc_h}")
    expr = m.group(1).strip()
    return eval(expr, {"__builtins__": {}})


def pad_with_ff(data: bytes, size: int, label: str) -> bytes:
    if len(data) > size:
        raise ValueError(f"{label}: source is {len(data)} bytes, larger than target size {size}")
    if len(data) < size:
        print(f"  {label}: padding {len(data)} -> {size} bytes with 0xFF")
    return data + b"\xff" * (size - len(data))


def common_prepare(cls, internal_path: Path, external_path: Path, bootloader: bool):
    """Mirrors gnwmanager/cli/_patch.py::_common_prepare() exactly, minus
    anything device-related. Reimplemented locally (rather than imported)
    to avoid pulling in gnwmanager.cli._patch's import-time CLI
    registration / cyclopts dependency, which we don't need here."""
    version = "0x08032000" if bootloader else "default"
    patch_data = (
        importlib.resources.files(f"gnwmanager.cli.gnw_patch.binaries.{cls.name}") / f"{version}.bin"
    ).read_bytes()
    elf = importlib.resources.files(f"gnwmanager.cli.gnw_patch.binaries.{cls.name}") / f"{version}.elf"
    device = cls(internal_path, elf, external_path)
    device.crypt()  # Decrypt external firmware.

    novel_code_start = device.internal.STOCK_ROM_END
    device.internal[novel_code_start:] = patch_data[novel_code_start:]
    if bootloader:
        device.internal.extend(b"\x00" * ((200 * (1 << 10)) - 0x20000))
    else:
        device.internal.extend(b"\x00" * 0x20000)

    return device


def default_args(game: str) -> Namespace:
    """Exactly the CLI defaults `gnwmanager flash-patch <game> --bootloader`
    would use with no other flags -- see gnwmanager/cli/_patch.py's
    mario()/zelda() command signatures."""
    if game == "mario":
        return Namespace(
            disable_sleep=False,
            sleep_time=None,
            no_save=False,
            no_mario_song=False,
            no_sleep_images=False,
            no_smb2=False,
            compression_ratio=1.4,
        )
    return Namespace(
        no_la=False,
        no_sleep_images=False,
        no_second_beep=False,
        no_hour_tune=False,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("game", choices=sorted(DEVICE_CLASSES))
    parser.add_argument("--source-tree", type=Path, required=True,
                        help="qemu-gnw source tree containing the SoC geometry header")
    parser.add_argument("--backup-dir", type=Path, required=True,
                        help="directory containing internal_flash_backup_<game>.bin and flash_backup_<game>.bin")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="where patched bank and extflash images are written")
    parser.add_argument("--retro-go-bank2", type=Path, metavar="PATH",
                         help="retro-go firmware/launcher binary, written verbatim as bank2")
    parser.add_argument("--retro-go-extflash", type=Path, metavar="PATH",
                         help="retro-go ROM/asset data to splice into extflash "
                              "(non-SD retro-go builds only -- omit for the SD variant)")
    parser.add_argument("--retro-go-extflash-offset", type=lambda s: int(s, 0), default=None,
                         help="byte offset within extflash where --retro-go-extflash is written "
                              "(default: 0x100000 for mario, 0x400000 for zelda)")
    args = parser.parse_args(argv)
    game = args.game

    soc_h = args.source_tree.expanduser().resolve() / "include/hw/arm/gnw_h7b0_soc.h"
    bank_size = read_define("FLASH_BANK_SIZE", soc_h)
    extflash_size = read_define("EXTFLASH_SIZE", soc_h)
    print(f"FLASH_BANK_SIZE={bank_size:#x}  EXTFLASH_SIZE={extflash_size:#x}")

    backup_dir = args.backup_dir.expanduser().resolve()
    out_dir = args.output_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    internal_src = backup_dir / f"internal_flash_backup_{game}.bin"
    extflash_src = backup_dir / f"flash_backup_{game}.bin"
    if not internal_src.exists():
        sys.exit(f"missing {internal_src}")
    if not extflash_src.exists():
        sys.exit(f"missing {extflash_src}")

    cls = DEVICE_CLASSES[game]
    device = common_prepare(cls, internal_src, extflash_src, bootloader=True)
    device.args = default_args(game)

    print(f"Running gnwmanager's {game} patch pipeline (bootloader=True, all other flags default)...")
    internal_remaining_free, compressed_memory_remaining_free = device()
    print(f"  internal free: {internal_remaining_free} bytes, "
          f"compressed_memory free: {compressed_memory_remaining_free} bytes")

    bank1 = pad_with_ff(bytes(device.internal), bank_size, "bank1-patched")
    (out_dir / f"{game}-bank1-patched.bin").write_bytes(bank1)

    if device.external:
        extflash = bytearray(pad_with_ff(bytes(device.external), extflash_size, "extflash-patched"))
    else:
        extflash = bytearray(b"\xff" * extflash_size)
        print("  extflash-patched: no external firmware produced, writing blank (0xFF)")

    if args.retro_go_extflash:
        offset = args.retro_go_extflash_offset
        if offset is None:
            offset = RETRO_GO_OFFSETS[game]
        retro_go_data = args.retro_go_extflash.read_bytes()
        end = offset + len(retro_go_data)
        if end > extflash_size:
            sys.exit(f"--retro-go-extflash {args.retro_go_extflash} ({len(retro_go_data)} bytes) "
                      f"at offset {offset:#x} runs past extflash size {extflash_size:#x}")
        extflash[offset:end] = retro_go_data
        print(f"  extflash-patched: spliced in {args.retro_go_extflash} "
              f"({len(retro_go_data)} bytes) @ offset {offset:#x}")

    (out_dir / f"{game}-extflash-patched.bin").write_bytes(bytes(extflash))

    print(f"Wrote {out_dir}/{game}-bank1-patched.bin, {out_dir}/{game}-extflash-patched.bin")

    if args.retro_go_bank2:
        bank2 = pad_with_ff(args.retro_go_bank2.read_bytes(), bank_size, "bank2 (retro-go)")
        (out_dir / f"{game}-bank2.bin").write_bytes(bank2)
        print(f"Wrote {out_dir}/{game}-bank2.bin (from {args.retro_go_bank2}, verbatim)")


if __name__ == "__main__":
    main()
