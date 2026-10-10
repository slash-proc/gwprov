"""A local developer's symbol/configuration adapter, independent of releases."""
from __future__ import annotations

import json
from pathlib import Path


def number(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer or hexadecimal string")
    try:
        result = int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer or hexadecimal string") from exc
    if not isinstance(value, (int, str)) or not 0 <= result <= 0xffffffff:
        raise ValueError(f"{label} is outside the 32-bit address/size range")
    return result


def load_debug_config(path, table) -> dict:
    """Load local ELF paths and/or a compact named routine/variable map.

    Only data is loaded. No project code is imported or executed. A build step
    can generate this descriptor, or an AI/developer can provide it directly.
    """
    config_path = Path(path).expanduser().resolve()
    data = json.loads(config_path.read_text())
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        raise ValueError("debug config must use schemaVersion 1")
    files = data.get("elfs", [])
    if not isinstance(files, list) or any(not isinstance(item, str) for item in files):
        raise ValueError("debug config elfs must be a list of local file paths")
    elf_paths = []
    for raw in files:
        elf = Path(raw).expanduser()
        if not elf.is_absolute():
            elf = config_path.parent / elf
        elf = elf.resolve()
        if not elf.is_file():
            raise FileNotFoundError(f"debug config ELF is missing: {elf}")
        elf_paths.append(elf)
    progress = data.get("progressSymbols", [])
    if not isinstance(progress, list) or any(not isinstance(name, str) or not name for name in progress):
        raise ValueError("progressSymbols must be a list of symbol names")
    raw_sections = data.get("sections", [])
    if not isinstance(raw_sections, list):
        raise ValueError("sections must be a list")
    sections = {}
    for section in raw_sections:
        if not isinstance(section, dict) or not isinstance(section.get("name"), str) or not section["name"]:
            raise ValueError("each section needs a name")
        base = number(section.get("address"), "section address")
        size = number(section.get("size"), "section size")
        if not size or base + size > 0x100000000:
            raise ValueError("section size must be positive and fit the address space")
        if section["name"] in sections:
            raise ValueError(f"duplicate debug section: {section['name']}")
        sections[section["name"]] = (base, size)
    entries = data.get("symbols", [])
    if not isinstance(entries, list):
        raise ValueError("debug config symbols must be a list")
    normalized = []
    names = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"]:
            raise ValueError("each map symbol needs a name")
        kind = entry.get("kind", "function")
        if entry["name"] in names and (kind != "function" or names[entry["name"]] != "function"):
            raise ValueError(f"ambiguous map object symbol: {entry['name']}")
        names[entry["name"]] = kind
        address = number(entry.get("address"), "symbol address")
        size = number(entry.get("size"), "symbol size")
        if kind not in {"function", "object"} or not size or address + size > 0x100000000:
            raise ValueError("map symbols need function/object kind and positive size within address space")
        section = entry.get("section")
        if section is None:
            section = f".gwprov_map_{len(normalized)}"
            sections[section] = (address & ~1 if kind == "function" else address, size)
        elif not isinstance(section, str) or section not in sections:
            raise ValueError(f"map symbol {entry['name']} names an undeclared section")
        base, extent = sections[section]
        actual_address = address & ~1 if kind == "function" else address
        if not base <= actual_address < actual_address + size <= base + extent:
            raise ValueError(f"map symbol {entry['name']} lies outside its section")
        normalized.append({"name": entry["name"], "address": address, "size": size,
                           "kind": kind, "section": section})
    raw_relocations = data.get("relocations", [])
    if not isinstance(raw_relocations, list):
        raise ValueError("relocations must be a list")
    relocations = []
    for entry in raw_relocations:
        if not isinstance(entry, dict) or any(not isinstance(entry.get(key), str) or not entry[key]
                                             for key in ("section", "basePointer")):
            raise ValueError("each relocation needs section and basePointer names")
        relocations.append(f"{entry['section']}={entry['basePointer']}")
    for elf in elf_paths:
        table.load(elf)
    if normalized:
        table.add_symbol_map(config_path, normalized, sections)
    gauges = data.get("sampleGauges", [])
    if not isinstance(gauges, list):
        raise ValueError("sampleGauges must be a list")
    gauge_names = set()
    for gauge in gauges:
        if not isinstance(gauge, dict) or not isinstance(gauge.get("symbol"), str) or not gauge["symbol"]:
            raise ValueError("sampleGauges require symbol names")
        name = gauge["symbol"]
        patterns = gauge.get("whenFunctions", [])
        if not isinstance(patterns, list) or any(not isinstance(item, str) or not item for item in patterns):
            raise ValueError("sampleGauges whenFunctions must be a list of function patterns")
        if name in gauge_names:
            raise ValueError(f"duplicate sample gauge: {name}")
        gauge_names.add(name)
        if not any(row["name"] == name and row["size"] in (1, 2, 4, 8) and row["type"] == "STT_OBJECT"
                   for row in table.nm(name)):
            raise ValueError(f"sample gauge {name!r} must be a named scalar object")
    for name in progress:
        rows = table.nm(name)
        if not any(row["name"] == name and row["size"] in (1, 2, 4, 8) and row["type"] == "STT_OBJECT"
                   for row in rows):
            raise ValueError(f"progress symbol {name!r} must be a named 1, 2, 4, or 8-byte object")
    for spec in relocations:
        section, pointer = spec.split("=", 1)
        if not any(row["name"] == pointer and row["size"] == 4 and row["type"] == "STT_OBJECT"
                   for row in table.nm(pointer)):
            raise ValueError(f"base pointer {pointer!r} must be a named 1, 2, 4, or 8-byte object")
        if not any(section in table.sections(elf) for elf in table.sources):
            raise ValueError(f"relocated section {section!r} is absent from symbol sources")
    return {"path": str(config_path), "elfs": [str(elf) for elf in elf_paths],
            "progress_symbols": progress, "sample_gauges": gauges, "rebase_symbols": relocations,
            "mapped_symbols": len(normalized)}
