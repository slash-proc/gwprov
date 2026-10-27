"""Versioned exact ABS32 relocation for mapped distribution artifacts."""

from __future__ import annotations

import struct
from pathlib import PurePosixPath


def mapped_declaration(spec: dict, path: str, *, source_size: int | None = None) -> dict | None:
    mapped = spec.get("mapped", False)
    if type(mapped) is not bool:
        raise ValueError("mapped must be a boolean")
    if not mapped:
        if "relocBase" in spec or "relocations" in spec:
            raise ValueError("relocation declarations require mapped=true")
        return None
    result = {"path": path}
    if "relocBase" in spec:
        base = spec["relocBase"]
        if isinstance(base, str):
            base = int(base, 0)  # Historic local-project formatting.
        if type(base) is not int or not 0 <= base <= 0xFFFFFFFF:
            raise ValueError("mapped relocBase must fit in uint32")
        result["relocBase"] = base
    if "relocations" in spec:
        result["relocations"] = validate_exact_relocations(
            spec["relocations"], source_size=source_size
        )
    return result


def project_relocation_tables(projects: dict) -> dict:
    tables = {}
    for project in projects.values():
        for item in project.get("mapped", []):
            if "relocations" in item:
                path = item["path"]
                if path in tables:
                    raise ValueError("duplicate mapped exact relocation source")
                tables[path] = validate_exact_relocations(item["relocations"])
    return tables


def validate_exact_relocations(value: object, *, source_size: int | None = None) -> dict:
    if not isinstance(value, dict) or set(value) != {"method", "version", "groups"}:
        raise ValueError("exact relocations require method, version, and groups")
    if (
        value["method"] != "exact-relocation-sites"
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ValueError("unsupported exact relocation method or version")
    groups = value["groups"]
    if not isinstance(groups, list) or not groups:
        raise ValueError("exact relocation groups must be a non-empty list")
    seen_targets, seen_sites = set(), set()
    normalized = []
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"target", "relocBase", "offsets"}:
            raise ValueError("relocation groups require target, relocBase, and offsets")
        target = group["target"]
        if (
            not isinstance(target, str)
            or not target
            or target in {".", ".."}
            or "/" in target
            or "\\" in target
            or any(ord(c) < 32 or ord(c) == 127 for c in target)
        ):
            raise ValueError("relocation target must be a plain filename")
        folded = target.casefold()
        if folded in seen_targets:
            raise ValueError("duplicate exact relocation target")
        seen_targets.add(folded)
        base = group["relocBase"]
        if type(base) is not int or not 0 <= base <= 0xFFFFFFFF or base & 3:
            raise ValueError("exact relocation base must be an aligned uint32")
        offsets = group["offsets"]
        if not isinstance(offsets, list) or not offsets:
            raise ValueError("exact relocation offsets must be a non-empty list")
        previous = -1
        for offset in offsets:
            if (
                type(offset) is not int
                or not 0 <= offset <= 0xFFFFFFFC
                or offset & 3
                or offset <= previous
                or offset in seen_sites
                or (source_size is not None and offset + 4 > source_size)
            ):
                raise ValueError(
                    "exact relocation sites must be sorted, unique, aligned, and inside the source"
                )
            seen_sites.add(offset)
            previous = offset
        normalized.append({"target": target, "relocBase": base, "offsets": list(offsets)})
    return {"method": "exact-relocation-sites", "version": 1, "groups": normalized}


def relocate_exact_bytes(
    source: bytes,
    table: dict,
    placements: dict[str, tuple[int, int]],
    *,
    source_offset: int = 0,
    source_size: int | None = None,
) -> tuple[bytes, int]:
    source_size = len(source) if source_size is None else source_size
    if source_offset < 0 or source_offset & 3 or source_offset + len(source) > source_size:
        raise ValueError("invalid exact relocation chunk range")
    table = validate_exact_relocations(table, source_size=source_size)
    by_name = {}
    for name, placement in placements.items():
        folded = name.casefold()
        if folded in by_name:
            raise ValueError("ambiguous mapped relocation target")
        by_name[folded] = placement
    data = bytearray(source)
    patched = 0
    for group in table["groups"]:
        placement = by_name.get(group["target"].casefold())
        if placement is None:
            raise ValueError(f"missing mapped relocation target: {group['target']}")
        address, size = placement
        base = group["relocBase"]
        if (
            type(address) is not int
            or address < 0
            or address & 3
            or type(size) is not int
            or size <= 0
            or base + size > 0x100000000
            or address + size > 0x100000000
        ):
            raise ValueError("mapped relocation target range is invalid")
        for offset in group["offsets"]:
            if offset < source_offset or offset >= source_offset + len(source):
                continue
            local = offset - source_offset
            if local + 4 > len(source):
                raise ValueError("exact relocation chunk splits a pointer word")
            original = struct.unpack_from("<I", source, local)[0]
            if not base <= (original & ~1) < base + size:
                raise ValueError(
                    f"exact relocation pointer at +{offset:#x} is outside {group['target']}"
                )
            struct.pack_into("<I", data, local, (original + address - base) & 0xFFFFFFFF)
            patched += 1
    return bytes(data), patched


def patch_frogfs_exact(
    image_path, *, mapped_artifacts, tables, extflash_offset=0, extflash_base=0x90000000
):
    """Resolve all placements before patching; publish only after every check."""
    from pathlib import Path
    import zlib
    from .vendor.retrogo_sd.scripts.frogfs_pico8_ro import find_uncompressed_file_payload

    if not tables:
        return
    path = Path(image_path)
    image = path.read_bytes()
    declarations = {}
    for name, _base in mapped_artifacts:
        folded = name.casefold()
        if folded in declarations:
            raise ValueError("ambiguous mapped artifact path")
        found = find_uncompressed_file_payload(image, name)
        if found is None:
            raise ValueError(f"missing or compressed mapped artifact: {name}")
        declarations[folded] = (name, *found)
    replacements = []
    for name, table in tables.items():
        entry = declarations.get(name.casefold())
        if entry is None:
            raise ValueError(f"exact relocation source is not mapped: {name}")
        _, offset, size = entry
        placements = {}
        for group in table["groups"]:
            target_path = str(PurePosixPath(name).parent / group["target"])
            target = declarations.get(target_path.casefold())
            if target is None:
                raise ValueError(f"missing mapped sibling relocation target: {target_path}")
            _, target_offset, target_size = target
            placements[group["target"]] = (
                extflash_base + extflash_offset + target_offset,
                target_size,
            )
        patched, _ = relocate_exact_bytes(image[offset : offset + size], table, placements)
        replacements.append((offset, patched))
    updated = bytearray(image)
    for offset, payload in replacements:
        updated[offset : offset + len(payload)] = payload
    struct.pack_into("<I", updated, len(updated) - 4, zlib.crc32(updated[:-4]))
    path.write_bytes(updated)
