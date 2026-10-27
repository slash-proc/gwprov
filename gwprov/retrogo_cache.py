"""Seed Retro-Go SD's file cache for a profile's autostart application.

The cache index is the on-device ``flashcachedata.bin`` structure used by
Core/Src/gw_flash_alloc.c.  GWemu uses a stable synthetic STM32 UID, which lets
GWProv prepare an exact cache hit before boot instead of making the virtual
device copy a large startup application at runtime.
"""

from __future__ import annotations

import calendar
import json
import os
import struct
import tempfile
import zlib
from pathlib import Path

from .profiles import DeviceProfile

EXTFLASH_BASE = 0x90000000
ERASE_BLOCK_SIZE = 4096
CACHE_VERSION = 2
CACHE_SLOTS = 256
CACHE_METADATA_PATH = "/data/flashcachedata.bin"
GWEMU_UID = (0x47575145, 0x4D55004D, 1)
METADATA_SIZE = 4124


def _file_crc32(path: str, mtime: int, size: int) -> int:
    crc = zlib.crc32(path.encode("utf-8"))
    # The current STM32 Retro-Go toolchain uses 64-bit time_t in struct stat;
    # file size remains the 32-bit field used by this firmware's cache format.
    crc = zlib.crc32(struct.pack("<Q", mtime & 0xFFFFFFFFFFFFFFFF), crc)
    return zlib.crc32(struct.pack("<I", size), crc) & 0xFFFFFFFF


def _empty_metadata_blob(base: int) -> bytes:
    blob = bytearray(METADATA_SIZE)
    struct.pack_into("<I3I", blob, 0, CACHE_VERSION, *GWEMU_UID)
    struct.pack_into("<IIH", blob, 4112, base, base, 0)
    return bytes(blob)


def _metadata_blob(*, entries: list[tuple[int, int, int]], base: int) -> bytes:
    if len(entries) > CACHE_SLOTS:
        raise ValueError(f"cache index supports at most {CACHE_SLOTS} entries")
    blob = bytearray(_empty_metadata_blob(base))
    pointer = base
    for index, (key, address, size) in enumerate(entries):
        struct.pack_into("<IIIB3x", blob, 16 + index * 16, key, address, size, 1)
        pointer = max(pointer, address + size)
    pointer = (pointer + ERASE_BLOCK_SIZE - 1) & ~(ERASE_BLOCK_SIZE - 1)
    struct.pack_into("<IIH", blob, 4112, pointer, base, max(0, len(entries) - 1))
    return bytes(blob)


def _partition_offset(image: Path) -> int:
    with image.open("rb") as stream:
        mbr = stream.read(512)
    if mbr[510:512] != b"\x55\xaa":
        return 0
    for index in range(4):
        entry = mbr[446 + index * 16 : 462 + index * 16]
        if entry[4] in (1, 4, 6, 11, 12, 14):
            return struct.unpack_from("<I", entry, 8)[0] * 512
    return 0


def _fatfs_entry_name(entry) -> str:
    """Return the spelling FatFs exposes through FILINFO.fname.

    Long names keep their stored spelling. 8.3 entries use the NTRes lower-case
    flags for the base and extension; PyFatFS's short-name helper leaves those
    uppercase, which can make Retro-Go's strcmp-based autostart miss the file.
    """
    if entry.lfn_entry is not None:
        return entry.get_long_name()
    short_name = entry.get_short_name()
    base, dot, extension = short_name.partition(".")
    if entry.ntres & 0x08:
        base = base.lower()
    if dot and entry.ntres & 0x10:
        extension = extension.lower()
    return base + (dot + extension if dot else "")


def _fat_file_details(image: Path, path: str):
    try:
        from pyfatfs.PyFatFS import PyFatFS
    except ImportError as exc:
        raise RuntimeError("autostart pre-cache requires the pyfatfs package") from exc

    offset = _partition_offset(image)
    with PyFatFS(str(image), offset=offset, read_only=True) as fs:
        parts = [part for part in path.split("/") if part]
        if not parts:
            raise ValueError("autostart path must name a file")
        lookup_parent = "/"
        firmware_parts = []
        source_parts = []
        entry = None
        for part in parts:
            names = fs.listdir(lookup_parent)
            match = next((name for name in names if name.casefold() == part.casefold()), None)
            if match is None:
                raise FileNotFoundError(
                    f"autostart file is missing from the profile SD image: {path}"
                )
            candidate = f"{lookup_parent.rstrip('/')}/{match}"
            entry = fs.fs.root_dir.get_entry(candidate)
            firmware_parts.append(_fatfs_entry_name(entry))
            source_parts.append(match)
            lookup_parent = candidate
        canonical_path = "/" + "/".join(firmware_parts)
        source_path = "/" + "/".join(source_parts)
        if entry.is_directory():
            raise ValueError(f"autostart path is a directory: {path}")
        # Use the decoded FAT fields directly. Firmware converts its local-time
        # fields with newlib mktime (UTC by default), so avoid PyFatFS's
        # host-local datetime.timestamp() conversion here.
        modified = entry.get_mtime()
        mtime = calendar.timegm(modified.timetuple())
        size = int(entry.filesize)
        if not 0 < size <= 0xFFFFFFFF:
            raise ValueError(f"autostart file size is unsupported by Retro-Go's cache: {size}")
        return offset, mtime, size, canonical_path, source_path


def _fat_metadata_destination(image: Path, offset: int) -> tuple[str, bool]:
    from pyfatfs.PyFatFS import PyFatFS

    with PyFatFS(str(image), offset=offset, read_only=True) as fs:
        match = next((name for name in fs.listdir("/") if name.casefold() == "data"), None)
    if match is None:
        return CACHE_METADATA_PATH, False
    return f"/{match}/flashcachedata.bin", True


def _validated_extflash_layout(bank2: bytes, layout: dict) -> tuple[int, int]:
    """Return the declared cache boundary and capacity after matching firmware.

    GWProv must seed at the exact boundary Retro-Go will use. Missing or stale
    host metadata is not evidence that the beginning of extflash is free.
    """
    records = []
    for pos in range(0, len(bank2) - 35, 4):
        if bank2[pos : pos + 8] != b"GWLB\x02\x00\x24\x00":
            continue
        stored_crc = struct.unpack_from("<I", bank2, pos + 32)[0]
        if zlib.crc32(bank2[pos : pos + 32]) != stored_crc:
            continue
        records.append(struct.unpack_from("<6I", bank2, pos + 8))
    if len(records) != 1:
        raise ValueError(
            "cannot safely pre-cache autostart: profile bank 2 has no unique valid "
            "GWLB layout record"
        )

    _, _, firmware_flash_size, firmware_reserved, _, flags = records[0]
    if not flags & 0x4:
        raise ValueError(
            "cannot safely pre-cache autostart: firmware GWLB record does not declare "
            "a reserved extflash boundary"
        )
    declared_reserved = layout.get("reservedOffset")
    if declared_reserved is not None and int(declared_reserved) != firmware_reserved:
        raise ValueError(
            "cannot safely pre-cache autostart: provision.json reservedOffset does not "
            "match the firmware GWLB record"
        )
    flash_size = int(layout.get("extflashBytes", 0))
    if firmware_flash_size != flash_size or flash_size <= 0:
        raise ValueError(
            "cannot safely pre-cache autostart: provision.json extflashBytes does not "
            "match the firmware GWLB record"
        )
    if firmware_reserved >= flash_size:
        raise ValueError(
            "cannot safely pre-cache autostart: firmware GWLB cache boundary lies "
            "outside the declared extflash"
        )
    # For older profile manifests the patched GWLB record remains authoritative;
    # reservedOffset is optional metadata, never a fallback to zero.
    return firmware_reserved, flash_size


def _push_metadata(profile_root: Path, destination: str, blob: bytes, manager) -> None:
    fd, metadata_temp = tempfile.mkstemp(prefix=".gwprov-flashcache-", dir=profile_root)
    metadata_path = Path(metadata_temp)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(blob)
        manager.push_file(str(metadata_path), destination)
    finally:
        metadata_path.unlink(missing_ok=True)


def _elf32_xip_relocations(path: Path) -> dict:
    """Read SMG's XIP sections and exact asset-pointer sites from its ELF symbols."""
    elf = path.read_bytes()
    if len(elf) < 52 or elf[:6] != b"\x7fELF\x01\x01":
        raise ValueError(f"project symbols are not a 32-bit little-endian ELF: {path}")
    header = struct.unpack_from("<16sHHIIIIIHHHHHH", elf)
    if header[2] != 40:
        raise ValueError(f"project symbols are not an ARM ELF: {path}")
    shoff, shentsize, shnum, shstrndx = header[6], header[11], header[12], header[13]
    if shentsize != 40 or not shnum or shstrndx >= shnum:
        raise ValueError(f"unsupported ELF section table in {path}")
    if shoff + shentsize * shnum > len(elf):
        raise ValueError(f"ELF section table extends beyond {path}")
    sections = [struct.unpack_from("<IIIIIIIIII", elf, shoff + i * shentsize) for i in range(shnum)]

    def c_string(blob: bytes, offset: int) -> str:
        end = blob.find(b"\0", offset)
        if offset < 0 or end < 0:
            raise ValueError("malformed ELF string table")
        return blob[offset:end].decode("ascii", errors="strict")

    names_section = sections[shstrndx]
    names_blob = elf[names_section[4] : names_section[4] + names_section[5]]
    section_names = [c_string(names_blob, section[0]) for section in sections]
    section_by_name = {name: sections[i] for i, name in enumerate(section_names)}
    try:
        asset_section = section_by_name[".xip_smg_assets"]
        code_section = section_by_name[".xip_smg_code"]
    except KeyError as exc:
        raise ValueError("project ELF has no SMG asset/code XIP sections") from exc

    symbols = {}
    for section in sections:
        if section[1] != 2:  # SHT_SYMTAB
            continue
        if section[9] != 16 or section[5] % 16 or section[6] >= shnum:
            raise ValueError("malformed ELF32 symbol table")
        if section[4] + section[5] > len(elf):
            raise ValueError("ELF symbol table extends beyond file")
        string_section = sections[section[6]]
        strings = elf[string_section[4] : string_section[4] + string_section[5]]
        for offset in range(section[4], section[4] + section[5], 16):
            name, value, _, _, _, shndx = struct.unpack_from("<IIIBBH", elf, offset)
            if name:
                symbols[c_string(strings, name)] = (value, shndx)

    def relocation_sites(start_name: str, end_name: str, target: tuple) -> tuple[int, ...]:
        try:
            start, start_section = symbols[start_name]
            end, end_section = symbols[end_name]
        except KeyError as exc:
            raise ValueError(
                f"project ELF is missing relocation table symbol {exc.args[0]}"
            ) from exc
        if start_section != end_section or start_section >= shnum:
            raise ValueError(f"project ELF has an invalid relocation table {start_name}")
        table_section = sections[start_section]
        byte_start = start - table_section[3]
        byte_end = end - table_section[3]
        if (
            byte_start < 0
            or byte_end < byte_start
            or byte_end > table_section[5]
            or (byte_end - byte_start) % 4
        ):
            raise ValueError(f"project ELF has a malformed relocation table {start_name}")
        absolute = table_section[4] + byte_start
        count = (byte_end - byte_start) // 4
        sites = struct.unpack_from(f"<{count}I", elf, absolute)
        base, size = target[3], target[5]
        if any(site & 3 or site < base or site + 4 > base + size for site in sites):
            raise ValueError(f"project ELF has out-of-range relocation sites in {start_name}")
        return tuple(sites)

    data_sites = relocation_sites(
        "__smg_data_relocs_start__", "__smg_data_relocs_end__", asset_section
    )
    code_asset_sites = relocation_sites(
        "__smg_code_relocs_start__", "__smg_code_relocs_end__", code_section
    )
    result = {
        "asset_base": asset_section[3],
        "asset_size": asset_section[5],
        "asset_sites": data_sites,
        "code_base": code_section[3],
        "code_size": code_section[5],
        "code_asset_sites": code_asset_sites,
    }
    # New SMG builds record code self references too: their rodata contains
    # numeric values in the code sentinel range that must remain unchanged.
    self_bounds = ("__smg_code_self_relocs_start__", "__smg_code_self_relocs_end__")
    if any(name in symbols for name in self_bounds):
        result["code_sites"] = relocation_sites(*self_bounds, code_section)
    return result


def _elf_symbol_path(profile: DeviceProfile, project: dict) -> Path | None:
    for relative in project.get("symbols", []):
        if not isinstance(relative, str):
            continue
        candidates = (profile.root / "debug" / relative, profile.root / relative)
        match = next((candidate for candidate in candidates if candidate.is_file()), None)
        if match is not None:
            return match
    return None


def _autostart_xip_paths(provision: dict, startup_file: str) -> tuple[dict, list[dict]] | None:
    wanted = startup_file.replace("\\", "/").lstrip("/").casefold()
    for project in provision.get("projects", {}).values():
        files = project.get("files", [])
        if not any(
            isinstance(item, str) and item.replace("\\", "/").lstrip("/").casefold() == wanted
            for item in files
        ):
            continue
        mapped = {
            item["path"].replace("\\", "/").lstrip("/").casefold(): item
            for item in project.get("mapped", [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
        paths = []
        for project_file in files:
            if not isinstance(project_file, str) or not project_file.casefold().endswith(".xip"):
                continue
            normalized = project_file.replace("\\", "/").lstrip("/").casefold()
            paths.append({**mapped.get(normalized, {}), "path": project_file})
        names = {Path(item["path"]).name.casefold() for item in paths}
        if {"smg_assets.xip", "smg_code.xip"}.issubset(names):
            return project, paths
    return None


def _mapped_cache_sources(
    profile: DeviceProfile, provision: dict, startup_file: str, image: Path
) -> list[dict] | None:
    """Generic manifest-driven sidecars; old SMG bundles retain their ELF fallback."""
    from .mapped_relocations import mapped_declaration

    wanted = startup_file.replace("\\", "/").lstrip("/").casefold()
    for project in provision.get("projects", {}).values():
        if not any(
            isinstance(name, str) and name.lstrip("/").casefold() == wanted
            for name in project.get("files", [])
        ):
            continue
        mapped = project.get("mapped", [])
        if not mapped:
            return None
        filenames = {Path(name).name.casefold() for name in project.get("files", [])}
        if {"smg_code.xip", "smg_assets.xip"} <= filenames and not any(
            "relocations" in item for item in mapped
        ):
            return None
        result = []
        for item in mapped:
            path = "/" + item["path"].replace("\\", "/").lstrip("/")
            _, mtime, size, firmware_path, source_path = _fat_file_details(image, path)
            declaration = mapped_declaration(
                {**item, "mapped": True}, firmware_path, source_size=size
            )
            result.append(
                {
                    **declaration,
                    "path": firmware_path,
                    "source_path": source_path,
                    "size": size,
                    "cache_key": _file_crc32(firmware_path, mtime, size),
                    "kind": "mapped",
                }
            )
        return result
    return None


def _relocate_mapped_cache_chunk(chunk: bytes, chunk_offset: int, source: dict) -> bytes:
    from .mapped_relocations import relocate_exact_bytes

    if "relocations" in source:
        return relocate_exact_bytes(
            chunk,
            source["relocations"],
            source["placements"],
            source_offset=chunk_offset,
            source_size=source["size"],
        )[0]
    base = source.get("relocBase")
    if base is None:
        return chunk
    data = bytearray(chunk)
    for offset in range(0, len(data) & ~3, 4):
        value = struct.unpack_from("<I", data, offset)[0]
        if base <= (value & ~1) < base + source["size"]:
            struct.pack_into("<I", data, offset, (value + source["address"] - base) & 0xFFFFFFFF)
    return bytes(data)


def _smg_xip_sources(
    profile: DeviceProfile, provision: dict, startup_file: str, image: Path
) -> list[dict] | None:
    match = _autostart_xip_paths(provision, startup_file)
    if match is None:
        return None
    project, mapped = match
    symbols_path = _elf_symbol_path(profile, project)
    if symbols_path is None:
        raise ValueError("cannot pre-cache mapped XIP sidecars: project ELF symbols are missing")
    relocations = _elf32_xip_relocations(symbols_path)

    code_items = [
        item for item in mapped if int(item.get("relocBase", -1)) == relocations["code_base"]
    ]
    asset_items = [item for item in mapped if "asset" in Path(item["path"]).name.casefold()]
    if len(code_items) != 1 or len(asset_items) != 1 or code_items[0] == asset_items[0]:
        raise ValueError("cannot match SMG's code and asset XIP files to the project manifest")

    sources = []
    # This order matches smg_xip_load(): assets are loaded before code. The
    # address is recorded per entry, so correctness does not depend on order.
    for item, base_key, size_key, sites_key in (
        (asset_items[0], "asset_base", "asset_size", "asset_sites"),
        (code_items[0], "code_base", "code_size", "code_asset_sites"),
    ):
        path = "/" + item["path"].replace("\\", "/").lstrip("/")
        _, mtime, size, firmware_path, source_path = _fat_file_details(image, path)
        if size != relocations[size_key]:
            raise ValueError(
                f"mapped XIP size does not match its project ELF section: {path} "
                f"({size} != {relocations[size_key]})"
            )
        sources.append(
            {
                "path": firmware_path,
                "source_path": source_path,
                "size": size,
                "cache_key": _file_crc32(firmware_path, mtime, size),
                "link_base": relocations[base_key],
                "sites": relocations[sites_key],
                "kind": "assets" if base_key == "asset_base" else "code",
                "relocations": relocations,
            }
        )
    return sources


def _relocate_xip_chunk(
    chunk: bytes, chunk_offset: int, source: dict, runtime_address: int
) -> bytes:
    data = bytearray(chunk)
    relocs = source["relocations"]
    link_base = source["link_base"]
    file_size = source["size"]

    def rebase(offset: int, old_base: int, new_base: int, target_size: int) -> None:
        if offset < chunk_offset or offset + 4 > chunk_offset + len(data):
            return
        local = offset - chunk_offset
        value = struct.unpack_from("<I", data, local)[0]
        bare = value & ~1
        if old_base <= bare < old_base + target_size:
            struct.pack_into("<I", data, local, (value + new_base - old_base) & 0xFFFFFFFF)

    if source["kind"] == "assets":
        for site in source["sites"]:
            rebase(site - link_base, link_base, runtime_address, file_size)
    else:
        code_sites = relocs.get("code_sites")
        if code_sites is None:
            # Compatibility with older ELF symbols lacking an exact table.
            code_offsets = range(chunk_offset, chunk_offset + (len(data) & ~3), 4)
        else:
            code_offsets = (site - link_base for site in code_sites)
        for offset in code_offsets:
            rebase(offset, link_base, runtime_address, file_size)
        asset_base = relocs["asset_base"]
        asset_address = source["asset_address"]
        asset_size = relocs["asset_size"]
        for site in source["sites"]:
            rebase(site - link_base, asset_base, asset_address, asset_size)
    return bytes(data)


def prepare_profile_autostart_cache(profile_name: str | Path, startup_file: str) -> dict:
    """Pre-cache an autostart file and mapped XIP sidecars in virtual extflash.

    This currently supports bundled SD profiles, the storage model used by
    GWemu. It streams directly between image files and never materializes the
    application or full extflash image in host RAM.
    """
    if not startup_file:
        return {"cached": False, "reason": "autostart disabled"}
    profile = DeviceProfile.load(profile_name)
    if profile.sd_mode != "bundled" or profile.resolved_sd is None:
        raise ValueError("autostart pre-cache requires a profile with bundled SD storage")

    image = profile.resolved_sd
    offset, mtime, size, firmware_path, source_path = _fat_file_details(image, startup_file)
    # Retro-Go compares autostart paths with paths returned by its FAT scan
    # using strcmp. Preserve the FAT directory-entry spelling so both that
    # lookup and the cache key use the exact path the firmware will enumerate.
    startup_file = firmware_path
    provision_path = profile.root / "provision.json"
    provision = json.loads(provision_path.read_text(encoding="utf-8"))
    layout = provision.get("layout", {})
    reserved, flash_size = _validated_extflash_layout(profile.bank2.read_bytes(), layout)
    base = EXTFLASH_BASE + ((reserved + ERASE_BLOCK_SIZE - 1) & ~(ERASE_BLOCK_SIZE - 1))
    flash_offset = base - EXTFLASH_BASE

    sources = [
        {
            "path": startup_file,
            "source_path": source_path,
            "size": size,
            "cache_key": _file_crc32(startup_file, mtime, size),
            "kind": "file",
        }
    ]
    xip_sources = _mapped_cache_sources(profile, provision, startup_file, image)
    if xip_sources is None:
        xip_sources = _smg_xip_sources(profile, provision, startup_file, image)
    if xip_sources:
        sources.extend(xip_sources)

    # The cache allocator advances in allocation order. Keep the startup
    # executable first, then the XIPs in smg_xip_load() order (assets, code),
    # and put each entry at the address its matching cache-index lookup returns.
    cursor = flash_offset
    entries = []
    for source in sources:
        source["flash_offset"] = cursor
        source["address"] = EXTFLASH_BASE + cursor
        if source.get("kind") == "code":
            asset_source = next(item for item in sources if item.get("kind") == "assets")
            source["asset_address"] = asset_source["address"]
        entries.append((source["cache_key"], source["address"], source["size"]))
        cursor += (source["size"] + ERASE_BLOCK_SIZE - 1) & ~(ERASE_BLOCK_SIZE - 1)
    mapped_by_path = {item["path"].casefold(): item for item in sources if item["kind"] == "mapped"}
    for source in sources:
        if source["kind"] != "mapped" or "relocations" not in source:
            continue
        placements = {}
        for group in source["relocations"]["groups"]:
            target_path = (Path(source["path"]).parent / group["target"]).as_posix().casefold()
            target = mapped_by_path.get(target_path)
            if target is None:
                raise ValueError(f"missing mapped cache relocation target: {group['target']}")
            placements[group["target"]] = (target["address"], target["size"])
        source["placements"] = placements
    if flash_offset < 0 or cursor > flash_size:
        total_bytes = sum(source["size"] for source in sources)
        raise ValueError(
            f"autostart and mapped XIP files ({total_bytes} bytes) do not fit the "
            "profile's writable extflash cache"
        )
    if profile.extflash.stat().st_size < flash_size:
        raise ValueError("profile extflash image is smaller than its declared capacity")

    # Preflight exact pointers with bounded reads before modifying either image.
    if any(item["kind"] == "mapped" and "relocations" in item for item in sources):
        from pyfatfs.PyFatFS import PyFatFS

        with PyFatFS(str(image), offset=offset, read_only=True) as fs:
            for source in sources:
                if source["kind"] != "mapped" or "relocations" not in source:
                    continue
                position = 0
                with fs.openbin(source["source_path"], "r") as stream:
                    while position < source["size"]:
                        chunk = stream.read(min(1024 * 1024, source["size"] - position))
                        if not chunk:
                            raise ValueError("truncated mapped cache source")
                        _relocate_mapped_cache_chunk(chunk, position, source)
                        position += len(chunk)
    metadata_destination, data_dir_exists = _fat_metadata_destination(image, offset)
    from .common.sdcard import QemuSDCardManager

    manager = QemuSDCardManager(str(image))
    if not data_dir_exists:
        manager.mkdir("/data")
    # Invalidate the old index before overwriting its flash range. If the host
    # stops or an I/O operation fails mid-copy, firmware sees an empty cache
    # instead of stale entries that still point into modified NOR bytes.
    _push_metadata(profile.root, metadata_destination, _empty_metadata_blob(base), manager)
    with profile.extflash.open("r+b") as flash:
        # PyFatFS streams source files while the virtual NOR is written
        # sequentially. Relocate XIP data per chunk so the converter never
        # needs to hold the 31 MiB assets sidecar in host memory.
        from pyfatfs.PyFatFS import PyFatFS

        with PyFatFS(str(image), offset=offset, read_only=True) as fs:
            for cached in sources:
                source_offset = 0
                left = cached["size"]
                flash.seek(cached["flash_offset"])
                with fs.openbin(cached["source_path"], "r") as source:
                    while left:
                        chunk = source.read(min(left, 1024 * 1024))
                        if not chunk:
                            raise ValueError(f"truncated cached file in SD image: {cached['path']}")
                        if cached["kind"] in {"assets", "code"}:
                            chunk = _relocate_xip_chunk(
                                chunk, source_offset, cached, cached["address"]
                            )
                        elif cached["kind"] == "mapped":
                            chunk = _relocate_mapped_cache_chunk(chunk, source_offset, cached)
                        flash.write(chunk)
                        source_offset += len(chunk)
                        left -= len(chunk)
                erased_size = (cached["size"] + ERASE_BLOCK_SIZE - 1) & ~(ERASE_BLOCK_SIZE - 1)
                if erased_size > cached["size"]:
                    flash.write(b"\xff" * (erased_size - cached["size"]))
        flash.flush()
        os.fsync(flash.fileno())

    _push_metadata(
        profile.root,
        metadata_destination,
        _metadata_blob(entries=entries, base=base),
        manager,
    )

    return {
        "cached": True,
        "path": startup_file,
        "bytes": sum(source["size"] for source in sources),
        "address": base,
        "flash_offset": flash_offset,
        "metadata_path": CACHE_METADATA_PATH,
        "files": [
            {"path": source["path"], "bytes": source["size"], "address": source["address"]}
            for source in sources
        ],
        "cache_key": sources[0]["cache_key"],
    }
