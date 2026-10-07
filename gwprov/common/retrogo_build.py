"""
Build the Retro-Go SD firmware, and report what it produced.

One function, both targets. gwemu and the device boot the SAME image -- that is
retro-go's own guiding rule for its emulator integration -- so building is a
single operation and the target only decides where the bytes go afterwards.

Defaults mirror the documented release build (retro-go's CLAUDE.md):

    COVERFLOW=1 CHEAT_CODES=1 SHARED_HIBERNATE_SAVESTATE=1
    DISABLE_SPLASH_SCREEN=1 ZH_CN=1 ZH_TW=1 KO_KR=1 JA_JP=1

with ONE deliberate deviation, INTFLASH_BANK:

  * retro-go's release default is bank 2, which is the productive install:
    stock firmware stays in bank 1 and retro-go lives alongside it at
    0x08100000 via a dual-boot patch.
  * WE default to bank 1. The device boots straight into bank 1, so a bank-1
    image runs immediately with no bootloader patching and no stock-firmware
    handling -- which is what you want when the thing under test is an
    emulator core, not the install story. Pass --bank 2 for a productive build.
    A bank-2 image placed in bank 1 boots nothing, so this must be deliberate.

COVERFLOW / CHEAT_CODES matter for a different reason: they must match what the
CORE is compiled with. The COVERFLOW fields sit before cheat_* in
retro_emulator_file_t, so a firmware/core disagreement misaligns pointers rather
than failing to build. Makefile.core sets the same pair; keep them in step.

We build the `gwemu_release` target rather than `release`, because it depends on
`release` and additionally lays down the emulator media (qemu_bank1.bin,
qemu_bank2.bin, extflash.bin, sdcard.img) using the SAME parameters. Retro-go's
comment on that dependency is explicit: otherwise "the emulator silently boots a
different firmware than the device and any comparison between them is worthless."
Consuming those bank images directly is also why nothing here re-implements bank
placement or padding.
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

# Hardcoded relative default by explicit exception: this is the one reference
# tree we build from. Nothing links against it.
DEFAULT_FIRMWARE_PATH = "references/game-and-watch-retro-go-sd"

# From retro-go's CLAUDE.md: "The Docker release build enables ...".
RELEASE_DEFAULTS = {
    "COVERFLOW": 1,
    "CHEAT_CODES": 1,
    "SHARED_HIBERNATE_SAVESTATE": 1,
    "DISABLE_SPLASH_SCREEN": 1,
    "ZH_CN": 1,
    "ZH_TW": 1,
    "KO_KR": 1,
    "JA_JP": 1,
    "CHECK_DIRTY_SUBMODULE": 0,
    # ALWAYS ON in our builds, and deliberately not a switch.
    #
    # REMOTE_INPUT makes buttons_get() OR a shadow word at 0x30001FF4 into the
    # live button state, which is the ONLY way `--timeline` can mean the same
    # thing on hardware as it does under gwemu. Making it optional would mean a
    # timeline that silently presses nothing on a firmware someone built
    # without it -- a test that passes while testing nothing. The cost is one
    # uncached load and twelve bit tests per input poll (~60 Hz) and four bytes
    # of the AHB .persistent pad, which is nothing next to that failure mode.
    # It is a development firmware; the "backdoor" only exists for a debugger
    # that is already attached.
    "REMOTE_INPUT": 1,
}

# Deliberately NOT retro-go's release value of 2. See the module docstring:
# bank 1 boots directly, which is what a development loop wants.
DEV_INTFLASH_BANK = 1

# External flash sizing. Specify the PHYSICAL PART and the OFFSET; the size is
# derived. EXTFLASH_SIZE_MB is the USABLE region after the offset, not the part
# size, because retro-go computes EXTFLASH_END = EXTFLASH_OFFSET + EXTFLASH_SIZE
# and that end must land on the part boundary.
#
#   64 MB part, 4 MB offset (typical: stock Zelda ROM kept at the bottom)
#       -> EXTFLASH_OFFSET=4194304, EXTFLASH_SIZE_MB=60      (NOT 64)
#
# Getting this wrong runs the region off the end of the part. Callers give the
# part and the offset so the subtraction is never done by hand.
DEV_EXTFLASH_PART_MB = 64
DEV_EXTFLASH_OFFSET_MB = 0
# The PART is a power of two; the usable size derived from it usually is not.
VALID_EXTFLASH_PART_MB = (1, 2, 4, 8, 16, 32, 64, 128, 256)
MB = 1024 * 1024

# gwemu requires internal-flash bank images of EXACTLY this size and refuses a
# short backing store ("too small for 'size' option 0x40000") even though it
# offers to extend it. Both banks are padded to it.
BANK_SIZE = 256 * 1024


@dataclass
class FirmwareBuild:
    """Where a completed build put things."""
    path: str
    intflash_bank: int
    bank1: str = ""
    bank2: str = ""
    extflash: str = ""
    sdcard: str = ""
    elf: str = ""
    intflash_bin: str = ""
    build_info: str = ""
    params: dict = field(default_factory=dict)

    def missing(self):
        return [n for n in ("bank1", "bank2", "elf")
                if not getattr(self, n) or not os.path.exists(getattr(self, n))]


def _artifacts(path, bank, params):
    b = os.path.join(path, "build")
    fb = FirmwareBuild(path=path, intflash_bank=bank, params=params)
    for attr, name in (("bank1", "qemu_bank1.bin"),
                       ("bank2", "qemu_bank2.bin"),
                       ("extflash", "extflash.bin"),
                       ("sdcard", "sdcard.img"),
                       ("elf", "gw_retro_go.elf"),
                       ("intflash_bin", "gw_retro_go_intflash.bin"),
                       ("build_info", "gwemu_build_info.txt")):
        p = os.path.join(b, name)
        setattr(fb, attr, p if os.path.exists(p) else "")
    return fb


def build(path=DEFAULT_FIRMWARE_PATH, clean=False, target="gwemu_release",
          intflash_bank=None, extflash_part_mb=None, extflash_offset_mb=None,
          jobs=None, docker=False, extra=None, verbose=True, dry_run=False):
    """Build the firmware. Returns FirmwareBuild, or the command list if
    dry_run. Raises on failure."""
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"no firmware checkout at {path}")
    if not os.path.exists(os.path.join(path, "Makefile.common")):
        raise FileNotFoundError(f"{path} does not look like a retro-go checkout")

    params = dict(RELEASE_DEFAULTS)
    params["INTFLASH_BANK"] = (DEV_INTFLASH_BANK if intflash_bank is None
                               else intflash_bank)

    part_mb = DEV_EXTFLASH_PART_MB if extflash_part_mb is None else extflash_part_mb
    if part_mb not in VALID_EXTFLASH_PART_MB:
        raise ValueError(
            f"extflash part size {part_mb} MB is not valid; the physical part "
            f"is a power of two, so use one of {list(VALID_EXTFLASH_PART_MB)}")

    off_mb = DEV_EXTFLASH_OFFSET_MB if extflash_offset_mb is None else extflash_offset_mb
    if off_mb < 0 or off_mb >= part_mb:
        raise ValueError(
            f"extflash offset {off_mb} MB does not fit inside a {part_mb} MB part")

    # The usable region is what is LEFT after the offset. retro-go computes
    # EXTFLASH_END = OFFSET + SIZE and that must equal the part size.
    params["EXTFLASH_OFFSET"] = off_mb * MB
    params["EXTFLASH_SIZE_MB"] = part_mb - off_mb
    if extra:
        params.update(extra)

    jobs = jobs or (os.cpu_count() or 4)
    base = ["make", f"-j{jobs}"] + [f"{k}={v}" for k, v in sorted(params.items())]
    if docker:
        base.append("DOCKER=1")

    cmd = base + [target]

    if dry_run:
        if clean:
            print(f"cd {path} && {' '.join(base + ['clean'])}")
        print(f"cd {path} && {' '.join(cmd)}")
        return cmd

    if clean:
        # Build flags are NOT dependencies in this build system -- changing one
        # recompiles nothing, so a stale object silently boots the wrong config.
        if verbose:
            print(f"[build_retrogo] clean in {path}")
        subprocess.run(base + ["clean"], cwd=path, check=False)
    if verbose:
        print(f"[build_retrogo] {' '.join(cmd)}")
        print(f"[build_retrogo] cwd={path}")
    res = subprocess.run(cmd, cwd=path)
    if res.returncode != 0:
        raise RuntimeError(f"firmware build failed (exit {res.returncode})")

    fb = _artifacts(path, params["INTFLASH_BANK"], params)
    missing = fb.missing()
    if missing:
        raise RuntimeError(
            f"build reported success but these artifacts are missing: {missing}. "
            f"Target '{target}' may not produce emulator media -- use gwemu_release.")
    _refresh_bank_images(fb, verbose=verbose)

    # The build flags are not dependencies here, so "I passed REMOTE_INPUT=1"
    # is not evidence that the image has it. Check the image.
    if params.get("REMOTE_INPUT") == 1:
        got = verify_remote_input(fb.elf)
        if got is False:
            raise RuntimeError(
                "REMOTE_INPUT=1 was passed but buttons_get() in the built image "
                "does not read the shadow cell -- a stale object. Rebuild with "
                "--clean; --timeline on hardware would press nothing.")
        if verbose:
            print(f"[build_retrogo] REMOTE_INPUT shadow cell "
                  f"0x{REMOTE_INPUT_ADDR:08X} in image: "
                  + ("yes" if got else "UNVERIFIED (no arm objdump)"))

    if verbose:
        print(f"[build_retrogo] bank{fb.intflash_bank} firmware ready:")
        for n in ("bank1", "bank2", "extflash", "sdcard", "elf"):
            p = getattr(fb, n)
            if p:
                print(f"    {n:9} {p} ({os.path.getsize(p)} bytes)")
    return fb


def _refresh_bank_images(fb, verbose=True):
    """Re-cut qemu_bank{1,2}.bin from the intflash image this build produced.

    THE EMULATOR IMAGES ARE NOT REBUILT BY `gwemu_release`. Its prep block is
    guarded by `if [ ! -f build/sdcard.img ]`, so once any gwemu media exists
    the bank images are never cut again -- every later build updates
    gw_retro_go_intflash.bin and leaves qemu_bank1.bin at whatever the FIRST
    build produced. Tier 2 then boots firmware that can be hours old while the
    build log says success, which is exactly how a REMOTE_INPUT=1 firmware came
    up with no remote input in it. Same failure shape as the stale SD image in
    docs/04, one layer down.

    Only the bank the firmware was LINKED for is touched: on a bank-2 build,
    bank 1 holds the stock-firmware backup and is not ours to overwrite.
    """
    src = fb.intflash_bin
    if not src or not os.path.exists(src):
        return
    dst = os.path.join(fb.path, "build",
                       "qemu_bank2.bin" if fb.intflash_bank == 2
                       else "qemu_bank1.bin")
    stale = (not os.path.exists(dst)
             or os.path.getmtime(dst) < os.path.getmtime(src))
    pad_bank(src, dst)
    setattr(fb, "bank2" if fb.intflash_bank == 2 else "bank1", dst)
    if verbose and stale:
        print(f"[build_retrogo] refreshed {os.path.basename(dst)} from "
              f"{os.path.basename(src)} (gwemu_release does NOT)")


# The shadow cell a REMOTE_INPUT firmware reads. Keep in sync with
# scripts/common/timeline.py and the firmware's Core/Inc/gw_buttons.h.
REMOTE_INPUT_ADDR = 0x30001FF4


def verify_remote_input(elf, addr=REMOTE_INPUT_ADDR, objdump=None):
    """True if buttons_get() actually reads the shadow cell in THIS image.

    Do NOT test this by grepping the .bin for the address: the compiler does
    not put 0x30001FF4 in the literal pool. It emits the page base
    (`.word 0x30001000`) and reaches the cell with `ldr.w rX,[rY,#4084]`, so a
    byte search for the full address finds ZERO hits in an image that has the
    feature -- which reads exactly like the define not taking.

    So: disassemble the function, collect its literals and its register-plus-
    offset loads, and see whether any pair sums to the cell.
    """
    tool = objdump or os.environ.get("OBJDUMP", "arm-none-eabi-objdump")
    try:
        out = subprocess.run([tool, "-d", elf, "--disassemble=buttons_get"],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None                      # cannot tell; caller decides
    import re
    words = [int(m, 16) for m in re.findall(r"\.word\s+0x([0-9a-f]+)", out)]
    offs = [int(m) for m in re.findall(r"ldr(?:\.w)?\s+\w+,\s*\[\w+,\s*#(\d+)\]", out)]
    return any(w + o == addr for w in words for o in offs + [0])


def pad_bank(src, dst, size=BANK_SIZE):
    """Copy a bank image padded to exactly `size`. gwemu rejects anything else."""
    with open(src, "rb") as fh:
        data = fh.read()
    if len(data) > size:
        raise RuntimeError(
            f"{src} is {len(data)} bytes, larger than a {size}-byte bank")
    with open(dst, "wb") as fh:
        fh.write(data.ljust(size, b"\x00"))
    return dst


def stage(fb, dest, sd_image=None, verbose=True):
    """Copy a build's media into `dest` under the names gwemu expects.

    Bank images are padded to exactly 256 KB -- gwemu refuses a short backing
    store. The SD image and extflash are copied as-is.

    sd_image overrides the firmware's own sdcard.img -- that is how our core and
    its /CONFIG get onto the card without touching the firmware tree.
    """
    os.makedirs(dest, exist_ok=True)
    out = {}
    for attr, name in (("bank1", "qemu_bank1.bin"), ("bank2", "qemu_bank2.bin")):
        src = getattr(fb, attr)
        if src and os.path.exists(src):
            out[attr] = pad_bank(src, os.path.join(dest, name))
    for attr, name in (("extflash", "extflash.bin"), ("sdcard", "sdcard.img")):
        src = sd_image if (attr == "sdcard" and sd_image) else getattr(fb, attr)
        if src and os.path.exists(src):
            dst = os.path.join(dest, name)
            shutil.copy(src, dst)
            out[attr] = dst
    if verbose:
        sizes = ", ".join(f"{k}={os.path.getsize(v)}" for k, v in out.items())
        print(f"[build_retrogo] staged -> {dest} ({sizes})")
    return out
