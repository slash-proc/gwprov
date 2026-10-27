"""Assemble provisioned content into a self-contained, mutable GWemu instance."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
import struct
import tempfile
import zlib

import yaml

MIB = 1024 * 1024
BANK_SIZE = 256 * 1024


def _flash_map_entry(name: str, relative_file: str, offset: int, path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "name": name,
        "file": relative_file,
        "offset": offset,
        "size": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def patch_layout(
    firmware: bytes,
    declaration: dict,
    *,
    frogfs_length: int,
    extflash_size: int,
    littlefs_length: int,
    frogfs_offset: int = 0,
    reserved_offset: int = 0,
) -> bytes:
    if declaration != {"magic": "GWLB", "version": 2, "structSize": 36}:
        raise ValueError(f"unsupported firmware layout declaration: {declaration}")
    candidates = [
        offset
        for offset in range(0, len(firmware) - 35, 4)
        if firmware[offset : offset + 8] == b"GWLB\x02\x00\x24\x00"
    ]
    if len(candidates) != 1:
        raise ValueError(f"expected one firmware layout superblock, found {len(candidates)}")
    result = bytearray(firmware)
    offset = candidates[0]
    flags = struct.unpack_from("<I", result, offset + 28)[0] | 15
    struct.pack_into(
        "<6I",
        result,
        offset + 8,
        frogfs_offset,
        frogfs_length,
        extflash_size,
        reserved_offset,
        littlefs_length,
        flags,
    )
    struct.pack_into("<I", result, offset + 32, zlib.crc32(result[offset : offset + 32]))
    return bytes(result)


def select_extflash_mib(required_bytes: int, requested: int | None = None) -> int:
    """Choose the smallest supported chip that fits, defaulting to at least 64 MiB."""
    sizes = (64, 128, 256)
    if requested is not None and requested not in sizes:
        raise ValueError("extflash size must be 64, 128 or 256 MiB")
    for size in (requested,) if requested is not None else sizes:
        if required_bytes <= size * MIB:
            return size
    raise ValueError(
        f"content needs {required_bytes} bytes; exceeds "
        + (f"the selected {requested} MiB chip" if requested else "the maximum 256 MiB chip")
    )


def create_profile(
    directory: str | Path,
    *,
    content: str | Path,
    name: str | None = None,
    littlefs_mib: int = 2,
    extflash_mib: int | None = None,
    sd_size_mib: int = 128,
    sd_label: str = "RETROGO",
    bootloader_repo: str = "sylverb/game-and-watch-bootloader",
    bootloader_version: str = "v1.0.8",
    bootloader_file: str | Path | None = None,
    base_extflash: str | Path | None = None,
    stock_model: str | None = None,
    erase_block_size: int = 64 * 1024,
) -> dict:
    source = Path(content).expanduser().resolve()
    metadata = json.loads((source / ".gwprov-firmware.json").read_text())
    variant = metadata.get("variant")
    if variant not in {"flash", "sd"}:
        raise ValueError(
            "firmware metadata variant must be flash or sd; run gwprov retro-go install first"
        )
    if sd_size_mib < 2:
        raise ValueError("SD image must be at least 2 MiB")
    declared_files = metadata.get("files")
    if not isinstance(declared_files, list) or not declared_files:
        raise ValueError(
            "firmware metadata has no declared content files; run gwprov retro-go install first"
        )
    missing_files = []
    for relative in declared_files:
        if not isinstance(relative, str):
            raise ValueError("firmware metadata contains a non-path file entry")
        parts = Path(relative).parts
        if not parts or parts[0] not in {"firmware", variant} or ".." in parts:
            raise ValueError(f"invalid firmware content path: {relative!r}")
        if not (source / relative).is_file():
            missing_files.append(relative)
    if missing_files:
        preview = ", ".join(missing_files[:4])
        suffix = " ..." if len(missing_files) > 4 else ""
        raise ValueError(
            f"firmware content is incomplete; missing {preview}{suffix}; rerun gwprov retro-go install"
        )
    projects_path = source / variant / ".gwprov-projects.json"
    projects = json.loads(projects_path.read_text()) if projects_path.is_file() else {}
    abi = metadata["firmware"]["providesAbi"]
    mapped = []
    for project in projects.values():
        required = project.get("requiresAbi") or {}
        if required and (
            required.get("version") != abi["version"] or required.get("minSize", 0) > abi["size"]
        ):
            raise ValueError(f"project ABI is incompatible with firmware: {project['repo']}")
        mapped.extend(project.get("mapped", []))
    root = Path(directory).expanduser().resolve()
    if root.exists():
        raise ValueError(f"profile already exists; select a new instance directory: {root}")
    select_extflash_mib(0, extflash_mib)
    if stock_model not in {None, "mario", "zelda"}:
        raise ValueError("stock model must be mario or zelda")
    if variant == "flash" and not 1 <= littlefs_mib < (extflash_mib or 256):
        raise ValueError("LittleFS must be at least 1 MiB and smaller than the extflash chip")
    from .bootloader import resolve_bootloader

    bootloader, boot_info = resolve_bootloader(
        cache=source.parent / ".gwprov-cache",
        repo=bootloader_repo,
        version=bootloader_version,
        local=bootloader_file,
    )
    root.parent.mkdir(parents=True, exist_ok=True)
    if variant == "sd":
        chip_mib = select_extflash_mib(0, extflash_mib or 64)
        total = chip_mib * MIB
        reserved_offset = _profile_reserved_offset(base_extflash, erase_block_size, stock_model)
        firmware = patch_layout(
            (source / "firmware/intflash.bin").read_bytes(),
            metadata["firmware"]["superblock"],
            frogfs_length=0,
            extflash_size=total,
            littlefs_length=0,
            reserved_offset=reserved_offset,
        )
        if len(firmware) > BANK_SIZE:
            raise ValueError("firmware is larger than bank 2")
        with tempfile.TemporaryDirectory(prefix=".gwprov-profile-", dir=root.parent) as tmp:
            work = Path(tmp)
            instance = work / "instance"
            instance.mkdir()
            _write_profile_images(
                instance, source, metadata, bootloader, firmware, extflash_bytes=total
            )
            blobs = instance / "blobs"
            blobs.mkdir()
            (blobs / "bootloader.bin").write_bytes(bootloader)
            (blobs / "retro-go-firmware.bin").write_bytes(firmware)
            flash_map = {
                "bank1": [
                    _flash_map_entry(
                        "Bootloader", "blobs/bootloader.bin", 0, blobs / "bootloader.bin"
                    )
                ],
                "bank2": [
                    _flash_map_entry(
                        f"Retro-Go {metadata['firmware'].get('version', '')}".strip(),
                        "blobs/retro-go-firmware.bin",
                        0,
                        blobs / "retro-go-firmware.bin",
                    )
                ],
                "bank0": [],
            }
            (instance / "flash-map.yaml").write_text(
                yaml.safe_dump(flash_map, sort_keys=False), encoding="utf-8"
            )
            project_symbols = _copy_project_symbols(source, projects, instance)
            _copy_mapped_project_sources(source, projects, instance, variant)
            sd_image = instance / "sdcard.img"
            from .common.sdcard import create_image, push_tree, QemuSDCardManager

            create_image(str(sd_image), size_mb=sd_size_mib, label=sd_label)
            push_tree(
                QemuSDCardManager(str(sd_image)),
                source / "sd",
                exclude_names={".gwprov-projects.json"},
            )
            display_name = name or root.name
            _write_profile_config(instance, display_name, sd_mode="bundled")
            report = {
                "firmware": metadata,
                "projects": projects,
                "debugSymbols": (
                    "debug/retro-go-debug.elf" if metadata.get("debugSymbols") else None
                ),
                "projectSymbols": project_symbols,
                "layout": {
                    "extflashBytes": total,
                    "sdImageBytes": sd_image.stat().st_size,
                    "sdLabel": sd_label,
                    "reservedOffset": reserved_offset,
                },
                "boot": "official bootloader at 0x08000000",
                "bootloader": boot_info,
                "variant": "sd",
            }
            (instance / "provision.json").write_text(json.dumps(report, indent=2) + "\n")
            (instance / "gwemu.toml").write_text("[general]\nshow_welcome = false\n")
            instance.rename(root)
        return report

    prebuilt_frogfs = source / "flash/frogfs.bin"
    prebuilt_littlefs = source / "flash/littlefs.bin"
    declared_layout = metadata.get("layout")
    if prebuilt_frogfs.is_file() or prebuilt_littlefs.is_file():
        if not (prebuilt_frogfs.is_file() and prebuilt_littlefs.is_file()):
            raise ValueError("a prebuilt flash build must include both FrogFS and LittleFS images")
        if (source / "flash/.gwprov-projects.json").is_file():
            raise ValueError(
                "prebuilt filesystem images cannot include later project edits; rebuild Retro-Go "
                "after staging projects"
            )
        if not isinstance(declared_layout, dict):
            raise ValueError("prebuilt flash images have no declared firmware layout")
        total = int(declared_layout.get("extflashBytes", 0))
        frog_offset = int(declared_layout.get("frogfsOffset", -1))
        frog_size = prebuilt_frogfs.stat().st_size
        little_size = prebuilt_littlefs.stat().st_size
        little_offset = int(declared_layout.get("littlefsOffset", -1))
        if (
            total <= 0
            or frog_offset < 0
            or little_offset < 0
            or int(declared_layout.get("frogfsBytes", -1)) != frog_size
            or int(declared_layout.get("littlefsBytes", -1)) != little_size
            or frog_offset + frog_size > little_offset
            or little_offset + little_size > total
        ):
            raise ValueError(
                "prebuilt filesystem image sizes do not fit their declared extflash layout"
            )
        firmware = (source / "firmware/intflash.bin").read_bytes()
        if len(firmware) > BANK_SIZE:
            raise ValueError("firmware is larger than bank 2")
        instance = root
        instance.mkdir(parents=True)
        _write_profile_images(instance, source, metadata, bootloader, firmware)
        blobs = instance / "blobs"
        blobs.mkdir()
        (blobs / "bootloader.bin").write_bytes(bootloader)
        (blobs / "retro-go-firmware.bin").write_bytes(firmware)
        shutil.copyfile(prebuilt_frogfs, blobs / "frogfs.bin")
        shutil.copyfile(prebuilt_littlefs, blobs / "littlefs.bin")
        with (instance / "extflash.bin").open("wb") as output:
            chunk = b"\xff" * MIB
            remaining = total
            while remaining:
                part = min(remaining, MIB)
                output.write(chunk[:part])
                remaining -= part
            output.seek(frog_offset)
            with prebuilt_frogfs.open("rb") as image:
                shutil.copyfileobj(image, output)
            output.seek(little_offset)
            with prebuilt_littlefs.open("rb") as image:
                shutil.copyfileobj(image, output)
        _write_profile_config(instance, name or root.name, sd_mode="none")
        report = {
            "firmware": metadata,
            "projects": {},
            "debugSymbols": "debug/retro-go-debug.elf" if metadata.get("debugSymbols") else None,
            "projectSymbols": [],
            "layout": {
                "extflashBytes": total,
                "frogfsOffset": frog_offset,
                "frogfsBytes": frog_size,
                "littlefsOffset": little_offset,
                "littlefsBytes": little_size,
            },
            "boot": "official bootloader at 0x08000000",
            "bootloader": boot_info,
            "variant": "flash",
        }
        flash_map = {
            "bank1": [
                _flash_map_entry("Bootloader", "blobs/bootloader.bin", 0, blobs / "bootloader.bin")
            ],
            "bank2": [
                _flash_map_entry(
                    f"Retro-Go {metadata['firmware'].get('version', '')}".strip(),
                    "blobs/retro-go-firmware.bin",
                    0,
                    blobs / "retro-go-firmware.bin",
                )
            ],
            "bank0": [
                _flash_map_entry(
                    "Retro-Go FrogFS", "blobs/frogfs.bin", frog_offset, blobs / "frogfs.bin"
                ),
                _flash_map_entry(
                    "Retro-Go LittleFS", "blobs/littlefs.bin", little_offset, blobs / "littlefs.bin"
                ),
            ],
        }
        (instance / "flash-map.yaml").write_text(
            yaml.safe_dump(flash_map, sort_keys=False), encoding="utf-8"
        )
        (instance / "provision.json").write_text(json.dumps(report, indent=2) + "\n")
        (instance / "gwemu.toml").write_text("[general]\nshow_welcome = false\n")
        return report

    from .vendor.retrogo_sd.scripts import gen_frogfs_image, gen_littlefs_image

    total = (extflash_mib or 256) * MIB
    reserved_offset = _profile_reserved_offset(base_extflash, erase_block_size, stock_model)
    scanned = []
    if base_extflash is not None:
        from .extflash_scan import scan_extflash

        scanned = scan_extflash(base_extflash)
    existing_frogfs = next((part for part in scanned if part.get("fs") == "frogfs"), None)
    frogfs_offset = (
        existing_frogfs["offset"]
        if existing_frogfs
        and existing_frogfs["offset"] % erase_block_size == 0
        and existing_frogfs["offset"] >= reserved_offset
        else reserved_offset
    )
    reserved_offset = max(reserved_offset, frogfs_offset)
    littlefs_size = littlefs_mib * MIB
    if total <= frogfs_offset + littlefs_size:
        raise ValueError(
            "extflash must leave space for FrogFS after detected reserved data and LittleFS"
        )
    with tempfile.TemporaryDirectory(prefix=".gwprov-profile-", dir=root.parent) as tmp:
        work = Path(tmp)
        frogfs_tree = source / "flash/frogfs"
        littlefs_tree = work / "littlefs-content"
        if (source / "flash/littlefs").is_dir():
            shutil.copytree(source / "flash/littlefs", littlefs_tree)
        common = ["--retro-go-root", str(work), "--roms-dir", str(work / "no-extra-roms")]
        frog_args = [
            *common,
            "--mkfrogfs",
            str(Path(__file__).parent / "vendor/frogfs/mkfrogfs.py"),
            "--sd-content",
            str(frogfs_tree),
            "--output",
            str(work / "frogfs.bin"),
            "--build-dir",
            str(work / "frogfs-build"),
            "--extflash-offset",
            str(frogfs_offset),
            "--reserve-size",
            str(total - frogfs_offset - littlefs_size),
            "--no-gencovers",
        ]
        for item in ("bios", "covers", "fonts", "roms"):
            frog_args.extend(["--include", item])
        # Raw ROM extensions are filtered from /homebrews by default. Preserve
        # only paths explicitly owned by a GWRG homebrew install manifest.
        raw_rom_extensions = {".sfc", ".smc", ".fig", ".swc"}
        for project in projects.values():
            for relative in project.get("files", []):
                parts = Path(relative).parts
                if (
                    len(parts) >= 3
                    and parts[0] == "frogfs"
                    and parts[1] == "homebrews"
                    and Path(parts[-1]).suffix.casefold() in raw_rom_extensions
                ):
                    rom_path = Path(*parts[2:])
                    if rom_path.is_absolute() or ".." in rom_path.parts:
                        raise ValueError(f"invalid project-owned homebrew ROM path: {relative!r}")
                    frog_args.extend(["--include-homebrew-rom", rom_path.as_posix()])
        for item in mapped:
            path = Path(item["path"])
            if (
                not path.parts
                or path.parts[0] not in {"cores", "homebrews"}
                or ".." in path.parts
                or path.is_absolute()
            ):
                raise ValueError(f"invalid mapped artifact path: {path}")
            base = item.get("relocBase")
            if isinstance(base, str):
                base = int(base, 0)
            if base is not None and (type(base) is not int or not 0 <= base <= 0xFFFFFFFF):
                raise ValueError("mapped artifact has an invalid relocation base")
            # An empty base still requests contiguous storage, with no scan.
            base_arg = "" if base is None or "relocations" in item else str(base)
            frog_args.extend(["--mapped-artifact", f"{frogfs_tree / path}:{path}:{base_arg}"])
        if gen_frogfs_image.main(frog_args):
            raise ValueError("FrogFS build failed")
        from .mapped_relocations import patch_frogfs_exact, project_relocation_tables

        patch_frogfs_exact(
            work / "frogfs.bin",
            mapped_artifacts=[(item["path"], item.get("relocBase")) for item in mapped],
            tables=project_relocation_tables(projects),
            extflash_offset=frogfs_offset,
        )
        littlefs_tree.mkdir(parents=True, exist_ok=True)
        (littlefs_tree / "data").mkdir(exist_ok=True)
        lfs_args = [
            *common,
            "--sd-content",
            str(littlefs_tree),
            "--output",
            str(work / "littlefs.bin"),
            "--build-dir",
            str(work / "littlefs-build"),
            "--size",
            str(littlefs_size),
            "--block-size",
            str(metadata["littlefsBlockSize"]),
            "--no-cores-filter",
        ]
        for item in ("cores", "lang", "data"):
            lfs_args.extend(["--include", item])
        config_file = littlefs_tree / "CONFIG"
        if config_file.is_file():
            lfs_args.extend(["--include-root-file", "CONFIG"])
        if gen_littlefs_image.main(lfs_args):
            raise ValueError("LittleFS build failed")
        frogfs = (work / "frogfs.bin").read_bytes()
        chip_mib = select_extflash_mib(frogfs_offset + len(frogfs) + littlefs_size, extflash_mib)
        total = chip_mib * MIB
        if frogfs_offset + len(frogfs) > total - littlefs_size:
            raise ValueError(
                f"content needs {frogfs_offset + len(frogfs)} bytes; only {total - littlefs_size} fit before LittleFS"
            )
        firmware = patch_layout(
            (source / "firmware/intflash.bin").read_bytes(),
            metadata["firmware"]["superblock"],
            frogfs_length=len(frogfs),
            extflash_size=total,
            littlefs_length=littlefs_size,
            frogfs_offset=frogfs_offset,
            reserved_offset=reserved_offset,
        )
        if len(firmware) > BANK_SIZE:
            raise ValueError("firmware is larger than bank 2")
        instance = work / "instance"
        instance.mkdir()
        _write_profile_images(instance, source, metadata, bootloader, firmware)
        blobs = instance / "blobs"
        blobs.mkdir()
        (blobs / "bootloader.bin").write_bytes(bootloader)
        (blobs / "retro-go-firmware.bin").write_bytes(firmware)
        (blobs / "frogfs.bin").write_bytes(frogfs)
        shutil.copyfile(work / "littlefs.bin", blobs / "littlefs.bin")
        project_symbols = _copy_project_symbols(source, projects, instance)
        _copy_mapped_project_sources(source, projects, instance, variant)
        with (instance / "extflash.bin").open("wb") as output:
            for _ in range(chip_mib):
                output.write(b"\xff" * MIB)
            output.seek(frogfs_offset)
            output.write(frogfs)
            output.seek(total - littlefs_size)
            with (work / "littlefs.bin").open("rb") as packed:
                shutil.copyfileobj(packed, output)
        display_name = name or root.name
        _write_profile_config(instance, display_name, sd_mode="none")
        report = {
            "firmware": metadata,
            "projects": projects,
            "debugSymbols": "debug/retro-go-debug.elf" if metadata.get("debugSymbols") else None,
            "projectSymbols": project_symbols,
            "layout": {
                "extflashBytes": total,
                "frogfsOffset": frogfs_offset,
                "reservedOffset": reserved_offset,
                "frogfsBytes": len(frogfs),
                "littlefsOffset": total - littlefs_size,
                "littlefsBytes": littlefs_size,
            },
            "boot": "official bootloader at 0x08000000",
            "bootloader": boot_info,
        }
        flash_map = {
            "bank1": [
                _flash_map_entry("Bootloader", "blobs/bootloader.bin", 0, blobs / "bootloader.bin")
            ],
            "bank2": [
                _flash_map_entry(
                    f"Retro-Go {metadata['firmware'].get('version', '')}".strip(),
                    "blobs/retro-go-firmware.bin",
                    0,
                    blobs / "retro-go-firmware.bin",
                )
            ],
            "bank0": [
                _flash_map_entry(
                    "Retro-Go FrogFS", "blobs/frogfs.bin", frogfs_offset, blobs / "frogfs.bin"
                ),
                _flash_map_entry(
                    "Retro-Go LittleFS",
                    "blobs/littlefs.bin",
                    total - littlefs_size,
                    blobs / "littlefs.bin",
                ),
            ],
        }
        (instance / "flash-map.yaml").write_text(
            yaml.safe_dump(flash_map, sort_keys=False), encoding="utf-8"
        )
        (instance / "provision.json").write_text(json.dumps(report, indent=2) + "\n")
        (instance / "gwemu.toml").write_text("[general]\nshow_welcome = false\n")
        instance.rename(root)
    return report


def _profile_reserved_offset(
    base_extflash: str | Path | None,
    erase_block_size: int,
    stock_model: str | None,
) -> int:
    from .extflash_scan import next_free_offset, scan_extflash

    if erase_block_size <= 0:
        raise ValueError("external-flash erase block size must be positive")
    partitions = scan_extflash(base_extflash) if base_extflash else []
    return next_free_offset(partitions, block_size=erase_block_size, stock_model=stock_model)


def _write_profile_images(
    instance: Path,
    source: Path,
    metadata: dict,
    bootloader: bytes,
    firmware: bytes,
    *,
    extflash_bytes: int | None = None,
) -> None:
    symbol_relative = metadata.get("debugSymbols")
    if symbol_relative:
        symbols = (source / symbol_relative).resolve()
        if source not in symbols.parents or not symbols.is_file():
            raise ValueError(
                f"firmware debug symbols are missing or escape the content root: {symbol_relative}"
            )
        debug_dir = instance / "debug"
        debug_dir.mkdir()
        shutil.copyfile(symbols, debug_dir / "retro-go-debug.elf")
    (instance / "bank1.bin").write_bytes(bootloader.ljust(BANK_SIZE, b"\xff"))
    (instance / "bank2.bin").write_bytes(firmware.ljust(BANK_SIZE, b"\xff"))
    (instance / "rdp-state.bin").write_bytes(b"\xaa")
    if extflash_bytes is not None:
        with (instance / "extflash.bin").open("wb") as output:
            chunk = b"\xff" * MIB
            remaining = extflash_bytes
            while remaining:
                amount = min(remaining, MIB)
                output.write(chunk[:amount])
                remaining -= amount


def _copy_project_symbols(source: Path, projects: dict, instance: Path) -> list[str]:
    copied: list[str] = []
    for project in projects.values():
        if not isinstance(project, dict):
            continue
        for raw in project.get("symbols", []):
            relative = Path(raw)
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                raise ValueError(f"invalid project symbol path in ownership marker: {raw!r}")
            symbol = (source / "debug" / relative).resolve()
            debug_root = (source / "debug" / "projects").resolve()
            if debug_root not in symbol.parents or not symbol.is_file():
                raise ValueError(f"project debug symbols are missing or escape content root: {raw}")
            destination = instance / "debug" / "apps" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(symbol, destination)
            copied.append(destination.relative_to(instance).as_posix())
    return sorted(copied)


def _copy_mapped_project_sources(
    source: Path, projects: dict, instance: Path, variant: str
) -> None:
    """Keep raw XiP sidecars so later profile edits can safely recompose FrogFS."""
    for project_key, project in projects.items():
        if not isinstance(project, dict):
            continue
        for index, item in enumerate(project.get("mapped", [])):
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ValueError(f"invalid mapped project entry in {project_key!r}")
            mapped_path = Path(item["path"])
            if mapped_path.is_absolute() or ".." in mapped_path.parts:
                raise ValueError(f"invalid mapped project path: {mapped_path}")
            content_path = (
                source / variant / "frogfs" / mapped_path
                if variant == "flash"
                else source / variant / mapped_path
            )
            if not content_path.is_file():
                raise ValueError(f"mapped project source is missing: {content_path}")
            identity = hashlib.sha256(project_key.encode("utf-8")).hexdigest()[:16]
            relative = Path("blobs/project-mapped") / identity / f"{index}-{mapped_path.name}"
            destination = instance / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(content_path, destination)
            item["source"] = relative.as_posix()


def _write_profile_config(instance: Path, display_name: str, *, sd_mode: str) -> None:
    sd_config = (
        '[sd]\nmode = "bundled"\nimage = "sdcard.img"\n'
        if sd_mode == "bundled"
        else '[sd]\nmode = "none"\n'
    )
    (instance / "profile.toml").write_text(
        f"version = 1\ndisplay_name = {json.dumps(display_name)}\n"
        '[flash]\nbank1 = "bank1.bin"\nbank2 = "bank2.bin"\nextflash = "extflash.bin"\n' + sd_config
    )
