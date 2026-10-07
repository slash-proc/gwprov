"""
Generate retro-go's global /CONFIG blob.

Why this is project code and not a one-off: two settings in it are what make
automated retro-go testing possible at all.

  startup_file        Set non-empty and retro-go AUTO-LAUNCHES it at boot,
                      bypassing the launcher UI entirely (rg_main.c:1057-1069:
                      `emulator_get_file(startup_file)` then
                      `emulator_start(file, true, true, -1)`). No menu
                      navigation, no button choreography, no screenshots.
  main_menu_timeout_s Idle standby. Defaults to 600s; set 0 for test runs so a
                      long measurement is never cut short by the device
                      deciding to sleep.

Also sets welcome_prompt=1 so the first-boot message cannot block startup.

Layout mirrors persistent_config_t in the firmware's
Core/Src/porting/odroid_settings.c. It is validated by a CRC the firmware
recomputes on load, so a layout mistake is loud: the firmware logs
"Config: CRC32 mismatch" and falls back to defaults. Read the device log
(scripts/common/rglog.py) to see it.

CRC is standard CRC-32: crc32_le(0, ...) inverts in and out, matching zlib.

Usage:
    python3 scripts/make_retrogo_config.py --out build/CONFIG \\
        --rom dos ALLEYCAT.dsk --menu-timeout 0
"""

import argparse
import struct
import zlib

CONFIG_MAGIC = 0xCAFEF00D
CONFIG_VERSION = 9

# Firmware path constants (Core/Inc/retro-go/rg_storage.h).
ROMS = "/roms"
HOMEBREWS = "/homebrews"

# odroid_settings.h
START_ACTION_RESUME = 0
START_ACTION_NEWGAME = 1

STARTUP_FILE_LEN = 256
BROWSE_SUBPATH_LEN = 96
RESERVED_APP_LEN = 32
CONFIG_SIZE = 416   # asserted below


def rom_path(dirname, filename):
    """Path in the form emulator_get_file() matches (rg_emulators.c:2139)."""
    if dirname == "homebrew":
        return f"{HOMEBREWS}/{filename}"
    return f"{ROMS}/{dirname}/{filename}"


def build(startup_file="", menu_timeout_s=0, backlight=6, volume=4,
          font_size=8, theme=2, lang=0, startup_app=0, cpu_oc_level=0,
          start_action=START_ACTION_RESUME, welcome_prompt=1,
          selected_tab=0, cursor=0, browse_subpath=""):
    """Return the packed /CONFIG bytes, CRC included."""
    if len(startup_file) >= STARTUP_FILE_LEN:
        raise ValueError("startup_file too long")

    blob = struct.pack(
        "<I"          # magic
        "12B"         # version, backlight, start_action, volume, font_size,
                      # theme, colors, turbo_buttons, font, lang, startup_app,
                      # cpu_oc_level
        f"{STARTUP_FILE_LEN}s"
        "3H"          # main_menu_timeout_s, selected_tab, cursor
        f"{BROWSE_SUBPATH_LEN}s"
        "B"           # debug_clock_always_on (bool)
        "x"           # pad to 4-byte alignment for welcome_prompt
        "I"           # welcome_prompt
        f"{RESERVED_APP_LEN}s"
        "I",          # crc32 (zero while computing)
        CONFIG_MAGIC,
        CONFIG_VERSION, backlight, start_action, volume, font_size,
        theme, 0, 0, 0, lang, startup_app, cpu_oc_level,
        startup_file.encode() ,
        menu_timeout_s, selected_tab, cursor,
        browse_subpath.encode(),
        0,
        welcome_prompt,
        b"",
        0,
    )
    if len(blob) != CONFIG_SIZE:
        raise AssertionError(
            f"packed /CONFIG is {len(blob)} bytes, expected {CONFIG_SIZE} -- "
            "persistent_config_t layout changed; re-check odroid_settings.c")

    crc = zlib.crc32(blob) & 0xFFFFFFFF
    return blob[:-4] + struct.pack("<I", crc)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="CONFIG", help="output path")
    p.add_argument("--startup-file", default="",
                   help="auto-launch this path at boot, e.g. /roms/dos/ALLEYCAT.dsk. "
                        "Empty means show the launcher.")
    p.add_argument("--rom", nargs=2, metavar=("DIRNAME", "FILENAME"),
                   help="build the startup path from a core dirname + filename")
    p.add_argument("--menu-timeout", type=int, default=0,
                   help="idle standby seconds; 0 disables (default for tests)")
    p.add_argument("--backlight", type=int, default=6)
    p.add_argument("--volume", type=int, default=4)
    args = p.parse_args()

    startup = args.startup_file
    if args.rom:
        startup = rom_path(*args.rom)

    blob = build(startup_file=startup, menu_timeout_s=args.menu_timeout,
                 backlight=args.backlight, volume=args.volume)
    with open(args.out, "wb") as fh:
        fh.write(blob)
    print(f"wrote {args.out} ({len(blob)} bytes)")
    print(f"  startup_file        {startup or '(launcher)'}")
    print(f"  main_menu_timeout_s {args.menu_timeout}"
          f"{'  (standby disabled)' if args.menu_timeout == 0 else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
