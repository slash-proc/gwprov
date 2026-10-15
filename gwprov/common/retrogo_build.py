"""Build Retro-Go release artifacts for GWProv's profile staging pipeline.

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

SD builds use Retro-Go's `release` target and its generated `sd_content` tree.
Flash builds use `frogfs_image` and `littlefs_image`. GWProv stages the resulting
files with the same metadata contract used by downloaded Page-dist releases.
"""

import os
import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

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

# Retro-Go's release distribution is linked for bank 2.
RELEASE_INTFLASH_BANK = 2

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


@dataclass
class FirmwareBuild:
    """Where a completed build put things."""

    path: str
    intflash_bank: int
    elf: str = ""
    intflash_bin: str = ""
    build_info: str = ""
    params: dict = field(default_factory=dict)
    variant: str = "sd"
    frogfs: str = ""
    littlefs: str = ""
    sd_content: str = ""


def _artifacts(path, bank, params):
    b = os.path.join(path, "build")
    fb = FirmwareBuild(path=path, intflash_bank=bank, params=params)
    for attr, name in (
        ("elf", "gw_retro_go.elf"),
        ("intflash_bin", "gw_retro_go_intflash.bin"),
        ("build_info", "gwemu_build_info.txt"),
    ):
        p = os.path.join(b, name)
        setattr(fb, attr, p if os.path.exists(p) else "")
    return fb


def build(
    path=DEFAULT_FIRMWARE_PATH,
    clean=False,
    variant="sd",
    extflash_part_mb=None,
    extflash_offset_mb=None,
    jobs=None,
    docker=False,
    extra=None,
    verbose=True,
    dry_run=False,
):
    """Build the firmware. Returns FirmwareBuild, or the command list if
    dry_run. Raises on failure."""
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise FileNotFoundError(f"no firmware checkout at {path}")
    if not os.path.exists(os.path.join(path, "Makefile.common")):
        raise FileNotFoundError(f"{path} does not look like a retro-go checkout")

    if variant not in {"sd", "flash"}:
        raise ValueError("variant must be sd or flash")
    targets = ["release"] if variant == "sd" else ["all", "frogfs_image", "littlefs_image"]

    params = dict(RELEASE_DEFAULTS)
    params["SD_CARD"] = 1 if variant == "sd" else 0
    params["INTFLASH_BANK"] = RELEASE_INTFLASH_BANK

    part_mb = extflash_part_mb
    off_mb = extflash_offset_mb
    if part_mb is not None or off_mb is not None:
        part_mb = DEV_EXTFLASH_PART_MB if part_mb is None else part_mb
        off_mb = DEV_EXTFLASH_OFFSET_MB if off_mb is None else off_mb
    if part_mb is not None and part_mb not in VALID_EXTFLASH_PART_MB:
        raise ValueError(
            f"extflash part size {part_mb} MB is not valid; the physical part "
            f"is a power of two, so use one of {list(VALID_EXTFLASH_PART_MB)}"
        )

    if part_mb is not None and (off_mb < 0 or off_mb >= part_mb):
        raise ValueError(f"extflash offset {off_mb} MB does not fit inside a {part_mb} MB part")

    # The usable region is what is LEFT after the offset. retro-go computes
    # EXTFLASH_END = OFFSET + SIZE and that must equal the part size.
    if part_mb is not None:
        params["EXTFLASH_OFFSET"] = off_mb * MB
        params["EXTFLASH_SIZE_MB"] = part_mb - off_mb
    if extra:
        params.update(extra)
    params["INTFLASH_BANK"] = RELEASE_INTFLASH_BANK
    params["SD_CARD"] = 1 if variant == "sd" else 0

    jobs = jobs or (os.cpu_count() or 4)
    base = ["make", f"-j{jobs}"] + [f"{k}={v}" for k, v in sorted(params.items())]
    if docker:
        base.append("DOCKER=1")

    cmd = base + targets

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
    for item in targets:
        res = subprocess.run(base + [item], cwd=path)
        if res.returncode != 0:
            raise RuntimeError(f"firmware build target {item!r} failed (exit {res.returncode})")

    fb = _artifacts(path, params["INTFLASH_BANK"], params)
    if int(params["INTFLASH_BANK"]) != 2:
        raise ValueError(
            "GWProv profile staging currently requires Retro-Go's bank-2 release layout"
        )
    fb.variant = variant
    fb.frogfs = str(Path(path) / "build/frogfs.bin")
    fb.littlefs = str(Path(path) / "build/littlefs.bin")
    fb.sd_content = str(Path(path) / "sd_content")
    required = ["elf", "intflash_bin"]
    required += ["sd_content"] if variant == "sd" else ["frogfs", "littlefs"]
    missing = [
        name for name in required if not getattr(fb, name) or not Path(getattr(fb, name)).exists()
    ]
    if missing:
        raise RuntimeError(
            f"build completed but required {variant} artifacts are missing: {missing}"
        )

    # The build flags are not dependencies here, so "I passed REMOTE_INPUT=1"
    # is not evidence that the image has it. Check the image.
    if params.get("REMOTE_INPUT") == 1:
        got = verify_remote_input(fb.elf)
        if got is False:
            raise RuntimeError(
                "REMOTE_INPUT=1 was passed but buttons_get() in the built image "
                "does not read the shadow cell -- a stale object. Rebuild with "
                "--clean; --timeline on hardware would press nothing."
            )
        if verbose:
            print(
                f"[build_retrogo] REMOTE_INPUT shadow cell "
                f"0x{REMOTE_INPUT_ADDR:08X} in image: "
                + ("yes" if got else "UNVERIFIED (no arm objdump)")
            )

    if verbose:
        print(f"[build_retrogo] bank{fb.intflash_bank} firmware ready:")
        for n in ("intflash_bin", "frogfs", "littlefs", "sd_content", "elf"):
            p = getattr(fb, n)
            if p:
                print(f"    {n:9} {p} ({os.path.getsize(p)} bytes)")
    return fb


def stage_build(fb: FirmwareBuild, output: str | Path) -> dict:
    """Stage local build files in the same tree consumed by profile creation."""
    root = Path(output).expanduser().resolve()
    marker = root / ".gwprov-firmware.json"
    if marker.exists():
        old = json.loads(marker.read_text(encoding="utf-8"))
        if old.get("origin") != "local-build":
            raise ValueError(f"refusing to replace a non-build firmware content root: {root}")
        for rel in old.get("files", []):
            path = root / rel
            if path.is_file():
                path.unlink()
    elif root.exists() and any(root.iterdir()):
        raise ValueError(f"refusing to stage over existing unrelated content: {root}")
    root.mkdir(parents=True, exist_ok=True)
    sources = {
        "firmware/intflash.bin": Path(fb.intflash_bin),
        "firmware/retro-go-debug.elf": Path(fb.elf),
    }
    if fb.variant == "flash":
        sources["flash/frogfs.bin"] = Path(fb.frogfs)
        sources["flash/littlefs.bin"] = Path(fb.littlefs)
    else:
        tree = Path(fb.sd_content)
        if not tree.is_dir():
            raise ValueError(f"Retro-Go release did not produce its sd_content directory: {tree}")
        for source in tree.rglob("*"):
            if source.is_file():
                sources[(Path("sd") / source.relative_to(tree)).as_posix()] = source
    entries = []
    for relative, source in sorted(sources.items()):
        if not source.is_file():
            raise ValueError(f"build artifact is missing: {source}")
        data = source.read_bytes()
        dest = root / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        entries.append(
            {"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    abi_version, abi_size = _abi_metadata(fb.elf)
    source_info = _git_source(fb.path)
    firmware = {
        "version": source_info["version"],
        "providesAbi": {"version": abi_version, "size": abi_size},
        "superblock": {"magic": "GWLB", "version": 2, "structSize": 36},
    }
    metadata = {
        "origin": "local-build",
        "repo": source_info["repo"],
        "version": firmware["version"],
        "source": source_info,
        "variant": fb.variant,
        "build": "make " + ("release" if fb.variant == "sd" else "frogfs_image littlefs_image"),
        "firmware": firmware,
        "debugSymbols": "firmware/retro-go-debug.elf",
        "littlefsBlockSize": int(fb.params.get("LITTLEFS_BLOCK_SIZE", 4096)),
        "layout": _build_layout(fb),
        "params": fb.params,
        "files": [entry["path"] for entry in entries],
        "artifacts": entries,
    }
    marker.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps({"schemaVersion": 1, "firmware": metadata, "files": entries}, indent=2) + "\n",
        encoding="utf-8",
    )
    return metadata


def _git_source(path: str) -> dict:
    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments], cwd=path, capture_output=True, text=True, check=False
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    version = git("describe", "--tags", "--always", "--dirty") or "unknown"
    commit = git("rev-parse", "HEAD") or "unknown"
    remote = git("remote", "get-url", "origin")
    import re

    match = re.search(r"(?:github\.com[:/])([^/]+/[^/]+?)(?:\.git)?$", remote)
    repo = match.group(1).lower() if match else "local/retro-go"
    return {"repo": repo, "version": version, "commit": commit, "dirty": version.endswith("-dirty")}


def _abi_metadata(elf: str) -> tuple[int, int]:
    """Read the ABI version and struct size at the start of .firmware_abi."""
    result = subprocess.run(
        [os.environ.get("OBJDUMP", "arm-none-eabi-objdump"), "-s", "-j", ".firmware_abi", elf],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "cannot read .firmware_abi from built ELF; install arm-none-eabi-objdump"
        )
    import re

    words = []
    for line in result.stdout.splitlines():
        match = re.match(r"\s*[0-9a-fA-F]+\s+((?:[0-9a-fA-F]{8}\s*)+)", line)
        if match:
            words.extend(re.findall(r"[0-9a-fA-F]{8}", match.group(1)))
    if not words:
        raise RuntimeError("built ELF has no readable .firmware_abi data")
    raw = bytes.fromhex("".join(words))
    version, size = int.from_bytes(raw[:4], "little"), int.from_bytes(raw[4:8], "little")
    if version < 1 or size < 8:
        raise RuntimeError("built ELF has invalid firmware ABI metadata")
    return version, size


def _build_layout(fb: FirmwareBuild) -> dict:
    if fb.variant == "sd":
        return {"sd": True}
    firmware = Path(fb.intflash_bin).read_bytes()
    locations = [
        i for i in range(len(firmware) - 36) if firmware[i : i + 8] == b"GWLB\x02\x00\x24\x00"
    ]
    if len(locations) != 1:
        raise RuntimeError(f"expected one GWLB firmware layout record, found {len(locations)}")
    import struct

    values = struct.unpack_from("<6I", firmware, locations[0] + 8)
    frogfs_offset, frogfs_bytes, extflash_bytes, _, littlefs_bytes, _ = values
    if (
        extflash_bytes <= 0
        or frogfs_bytes != Path(fb.frogfs).stat().st_size
        or littlefs_bytes != Path(fb.littlefs).stat().st_size
        or frogfs_offset + frogfs_bytes > extflash_bytes - littlefs_bytes
    ):
        raise RuntimeError("built filesystem images do not match the firmware layout record")
    return {
        "extflashBytes": extflash_bytes,
        "frogfsOffset": frogfs_offset,
        "frogfsBytes": frogfs_bytes,
        "littlefsBytes": littlefs_bytes,
        "littlefsOffset": extflash_bytes - littlefs_bytes,
    }


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
        out = subprocess.run(
            [tool, "-d", elf, "--disassemble=buttons_get"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None  # cannot tell; caller decides
    import re

    words = [int(m, 16) for m in re.findall(r"\.word\s+0x([0-9a-f]+)", out)]
    offs = [int(m) for m in re.findall(r"ldr(?:\.w)?\s+\w+,\s*\[\w+,\s*#(\d+)\]", out)]
    return any(w + o == addr for w in words for o in offs + [0])
