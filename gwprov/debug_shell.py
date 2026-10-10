"""Persistent structured Python debugger for GWemu and Game & Watch hardware."""

from __future__ import annotations

import code
import errno
import json
import re
import shutil
import socket
import subprocess
import time
import xml.etree.ElementTree as ET
from bisect import bisect_right
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection
from elftools.dwarf.callframe import FDE, RegisterRule
from gnwmanager.ocdbackend.gdb_backend import GDBBackend


class SymbolTable:
    """Resolve local ELF/map symbols and translate mapped section addresses."""

    def __init__(self):
        self._symbols: dict[str, int] = {}
        self._symbol_owners: dict[str, Path] = {}
        self._symbol_sections: dict[str, str] = {}
        self._symbol_types: dict[str, str] = {}
        self._symbol_sizes: dict[str, int] = {}
        self._symbol_file_hints: dict[str, str] = {}
        self._sections: dict[Path, dict[str, tuple[int, int]]] = {}
        self._section_deltas: dict[tuple[Path, str], int] = {}
        self._cfi: dict[Path, list[tuple[int, int, FDE]]] = {}
        self.sources: list[Path] = []
        self._map_sources: set[Path] = set()
        self._function_sources: dict[Path, list[dict]] = {}

    def load(self, path: str | Path) -> int:
        from .debug_types import clear_layout_caches
        clear_layout_caches()
        elf = Path(path).expanduser().resolve()
        if not elf.is_file():
            raise FileNotFoundError(elf)
        with elf.open("rb") as stream:
            image = ELFFile(stream)
            sections: dict[str, tuple[int, int]] = {
                section.name: (int(section["sh_addr"]), int(section["sh_size"]))
                for section in image.iter_sections()
                if section.name
            }
            symtab = image.get_section_by_name(".symtab")
            if not isinstance(symtab, SymbolTableSection):
                symtab = image.get_section_by_name(".dynsym")
            section_names = {
                index: section.name for index, section in enumerate(image.iter_sections())
            }
            count = 0
            function_rows = []
            current_file = None
            for symbol in symtab.iter_symbols() if isinstance(symtab, SymbolTableSection) else ():
                if not symbol.name:
                    continue
                name = symbol.name
                if str(symbol['st_info']['type']) == 'STT_FILE':
                    current_file = name
                if (str(symbol['st_info']['bind']) == 'STB_LOCAL' and current_file
                        and str(symbol['st_info']['type']) != 'STT_FILE'):
                    self._symbol_file_hints[name] = current_file
                else:
                    self._symbol_file_hints.pop(name, None)
                if str(symbol["st_info"]["type"]) == "STT_FUNC":
                    section_index = symbol["st_shndx"]
                    function_rows.append({"name": name, "address": int(symbol["st_value"]),
                                          "size": int(symbol["st_size"]),
                                          "section": section_names.get(section_index),
                                          "elf": str(elf), "type": "STT_FUNC"})
                self._symbols[name] = int(symbol["st_value"])
                self._symbol_owners[name] = elf
                self._symbol_types[name] = str(symbol["st_info"]["type"])
                self._symbol_sizes[name] = int(symbol["st_size"])
                section_index = symbol["st_shndx"]
                if isinstance(section_index, int):
                    section = section_names.get(section_index)
                    if section:
                        self._symbol_sections[name] = section
                    else:
                        self._symbol_sections.pop(name, None)
                else:
                    self._symbol_sections.pop(name, None)
                count += 1
        self._sections[elf] = sections
        if elf not in self.sources:
            self.sources.append(elf)
        self._function_sources[elf] = function_rows
        return count

    def compilation_units(self, *, elf: str | Path | None = None) -> list[dict]:
        """Return original DWARF CU source and compiler producer strings in order."""
        selected = Path(elf).expanduser().resolve() if elf is not None else None
        result = []
        def value(die, name):
            attribute = die.attributes.get(name)
            if attribute is None:
                return None
            raw = attribute.value
            return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        for owner in self.sources:
            if owner in self._map_sources or (selected is not None and owner != selected):
                continue
            with owner.open("rb") as stream:
                dwarf = ELFFile(stream).get_dwarf_info()
                for cu in dwarf.iter_CUs():
                    die = cu.get_top_DIE()
                    result.append({"elf": str(owner), "offset": cu.cu_offset,
                                   "source": value(die, "DW_AT_name"),
                                   "directory": value(die, "DW_AT_comp_dir"),
                                   "producer": value(die, "DW_AT_producer"),
                                   "language": value(die, "DW_AT_language"),
                                   "dwarf_version": cu["version"]})
        if selected is not None and selected not in self.sources:
            raise ValueError(f"ELF is not loaded: {selected}")
        return result

    def compact_debug(self, output: str | Path, *, elf: str | Path | None = None) -> dict:
        """Create a separate compressed-debug ELF without changing loadable data."""
        from .elf_tools import compact_debug
        choices = [source for source in self.sources if source not in self._map_sources]
        if elf is None:
            if len(choices) != 1:
                raise ValueError("specify elf= when more than one ELF is loaded")
            owner = choices[0]
        else:
            owner = Path(elf).expanduser().resolve()
            if owner not in choices:
                raise ValueError("compression requires a loaded ELF, not a symbol map")
        return compact_debug(owner, output)

    def functions(self) -> list[dict]:
        """All ELF/map routines, including repeated static names across ELFs."""
        rows = []
        for owner in self.sources:
            for raw in self._function_sources.get(owner, []):
                row = dict(raw)
                row["address"] += self._section_deltas.get((owner, row.get("section")), 0)
                rows.append(row)
        return rows

    def add_symbol_map(self, source, entries, sections) -> int:
        """Register compact local address/name/size records without an ELF."""
        owner = Path(source).expanduser().resolve()
        self._map_sources.add(owner)
        self._sections[owner] = dict(sections)
        functions = []
        for entry in entries:
            name = entry["name"]
            kind = "STT_FUNC" if entry["kind"] == "function" else "STT_OBJECT"
            self._symbols[name] = entry["address"]
            self._symbol_owners[name] = owner
            self._symbol_types[name] = kind
            self._symbol_sizes[name] = entry["size"]
            self._symbol_sections[name] = entry["section"]
            if kind == "STT_FUNC":
                functions.append({"name": name, "address": entry["address"],
                                  "size": entry["size"], "section": entry["section"],
                                  "type": kind, "elf": str(owner), "source_format": "routine-map"})
        self._function_sources[owner] = functions
        if owner not in self.sources:
            self.sources.append(owner)
        return len(entries)

    def load_config(self, path) -> dict:
        """Read a local developer's ELF/map, relocation and progress description."""
        from .debug_config import load_debug_config
        return load_debug_config(path, self)

    def __getitem__(self, name: str) -> int:
        address = self._symbols[name]
        owner = self._symbol_owners[name]
        section = self._symbol_sections.get(name)
        if section:
            address += self._section_deltas.get((owner, section), 0)
        return address

    def owner(self, name: str) -> Path:
        """Return the ELF that supplied a symbol."""
        return self._symbol_owners[name]

    def rebase(self, section: str, actual_base: int, elf: str | Path | None = None) -> int:
        """Translate symbols in one ELF section to its runtime mapped address.

        ELFs remain immutable and continue to describe their link-time layout.
        If multiple loaded ELFs contain this section, the most recently loaded
        one is selected unless ``elf`` names a specific file.
        """
        if elf is None:
            candidates = [path for path in self.sources if section in self._sections[path]]
            if not candidates:
                raise KeyError(f"section {section!r} not found in loaded ELFs")
            owner = candidates[-1]
        else:
            owner = Path(elf).expanduser().resolve()
            if section not in self._sections.get(owner, {}):
                raise KeyError(f"section {section!r} not found in {owner}")
        linked_base, _ = self._sections[owner][section]
        delta = actual_base - linked_base
        self._section_deltas[(owner, section)] = delta
        return delta

    def rebase_from_runtime_pointers(self, read_memory) -> dict[str, int]:
        """Rebase .xip_<name> sections from matching <name>_xip_runtime_base globals.

        Port code can expose a 32-bit runtime-base global for each relocated XIP
        section. This convention lets local profiling and state inspection
        resolve mapped code without a project-specific symbol list.
        """
        rebased = {}
        for owner in self.sources:
            sections = self._sections.get(owner, {})
            rows = {row["name"]: row for row in self.nm(elf=owner)}
            for section, (_, size) in sections.items():
                if not section.startswith(".xip_"):
                    continue
                stem = section[len(".xip_"):]
                pointer = rows.get(f"{stem}_xip_runtime_base")
                if not pointer or pointer["type"] != "STT_OBJECT" or pointer["size"] < 4:
                    continue
                actual_base = int.from_bytes(read_memory(pointer["address"], 4), "little")
                if not actual_base or actual_base + size > 0x100000000:
                    continue
                self.rebase(section, actual_base, owner)
                rebased[f"{owner}:{section}"] = actual_base
        return rebased

    def find(self, prefix: str) -> dict[str, int]:
        return {name: self[name] for name in self._symbols if prefix in name}

    def source_hint(self, symbol: str) -> str | None:
        """Return the owning ELF's local STT_FILE hint, never a guessed CU."""
        return self._symbol_file_hints.get(symbol)

    def link_address(self, symbol: str) -> int:
        """Original ELF address for DWARF selection, before runtime rebasing."""
        return self._symbols[symbol]

    def type_layout(self, symbol: str) -> dict:
        """Describe a global's C layout from its owning ELF's DWARF."""
        from .debug_types import variable_layout
        if self.owner(symbol) in self._map_sources:
            raise ValueError("compact symbol maps have no DWARF type layouts")
        return variable_layout(self.owner(symbol), symbol, self.source_hint(symbol), self.link_address(symbol))

    def nm(self, query: str = "", elf: str | Path | None = None) -> list[dict]:
        """Return structured nm-style symbol rows, optionally filtered by ELF."""
        selected = Path(elf).expanduser().resolve() if elf is not None else None
        rows = []
        for name, address in self._symbols.items():
            owner = self._symbol_owners[name]
            if query not in name or (selected is not None and owner != selected):
                continue
            rows.append({"name": name, "address": self[name],
                         "size": self._symbol_sizes.get(name, 0),
                         "type": self._symbol_types.get(name, ""),
                         "section": self._symbol_sections.get(name),
                         "elf": str(owner)})
        return sorted(rows, key=lambda row: (row["address"], row["name"]))

    def disassembly_tool(self, symbol: str) -> dict:
        """Report the ELF machine and selected executable/version for a symbol."""
        from .elf_binutils import select_elf_tool
        owner = self.owner(symbol)
        if owner in self._map_sources:
            raise ValueError("ELF disassembly requires an ELF, not a compact symbol map")
        return select_elf_tool(owner, "objdump")

    def disassemble(self, symbol: str, elf: str | Path | None = None) -> str:
        """Disassemble one symbol with binutils selected for its ELF machine."""
        if symbol not in self._symbols:
            raise KeyError(f"symbol {symbol!r} is not loaded")
        owner = (Path(elf).expanduser().resolve() if elf is not None
                 else self._symbol_owners[symbol])
        if self._symbol_owners[symbol] != owner:
            raise ValueError(f"symbol {symbol!r} does not belong to {owner}")
        if owner in self._map_sources:
            raise ValueError("ELF disassembly requires an ELF, not a compact symbol map")
        from .elf_binutils import select_elf_tool
        selection = select_elf_tool(owner, "objdump")
        try:
            result = subprocess.run(
                [selection["path"], "-d", "-C", f"--disassemble={symbol}", str(owner)],
                check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as error:
            raise ValueError(f"{selection['path']} cannot disassemble ELF machine "
                             f"{selection['machine']}: {error.stderr.strip()}") from error
        return result.stdout

    def source_locations(self, addresses) -> list[dict]:
        """Resolve many PCs with one addr2line invocation per owning ELF."""
        addresses = list(addresses)
        results = {}
        groups = {}
        tool = shutil.which("arm-none-eabi-addr2line") or shutil.which("addr2line")
        for address in addresses:
            info = self._owner_for_pc(address)
            if not info or info[0] in self._map_sources or not tool:
                results[address] = self.source_location(address)
                continue
            owner, link = info
            groups.setdefault(owner, {}).setdefault(link, []).append(address)
        for owner, links in groups.items():
            try:
                output = subprocess.run(
                    [tool, "-a", "-f", "-C", "-i", "-e", str(owner),
                     *[hex(link) for link in links]],
                    check=True, capture_output=True, text=True).stdout
                decoded = {}
                current = None
                for line in output.splitlines():
                    if re.fullmatch(r"0x[0-9a-fA-F]+", line):
                        current = int(line, 16)
                        decoded[current] = []
                    elif current is not None:
                        decoded[current].append(line)
                for link, runtime_addresses in links.items():
                    lines = decoded.get(link, [])
                    frames = [{"function": lines[i], "file": lines[i + 1]}
                              for i in range(0, len(lines) - 1, 2)]
                    for address in runtime_addresses:
                        results[address] = {"address": address, "link_address": link,
                            "elf": str(owner), "available": bool(frames), "frames": frames}
            except (OSError, subprocess.SubprocessError) as exc:
                for link, runtime_addresses in links.items():
                    for address in runtime_addresses:
                        results[address] = {"address": address, "link_address": link,
                            "elf": str(owner), "available": False, "reason": str(exc)}
        return [results[address] for address in addresses]

    def source_location(self, address: int) -> dict:
        """Resolve a runtime PC to source lines, accounting for section rebases."""
        owner_info = self._owner_for_pc(address)
        if owner_info is None:
            return {"address": address, "available": False,
                    "reason": "address is outside loaded ELF sections"}
        owner, link_address = owner_info
        if owner in self._map_sources:
            return {"address": address, "link_address": link_address,
                    "symbol_source": str(owner), "available": False,
                    "reason": "compact symbol map names routines but has no source/DWARF"}
        tool = shutil.which("arm-none-eabi-addr2line") or shutil.which("addr2line")
        if not tool:
            return {"address": address, "available": False,
                    "elf": str(owner), "reason": "arm-none-eabi-addr2line or addr2line is required"}
        try:
            result = subprocess.run(
                [tool, "-f", "-C", "-i", "-e", str(owner), hex(link_address)],
                check=True, capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"address": address, "link_address": link_address,
                    "elf": str(owner), "available": False, "reason": str(exc)}
        rows = [row for row in result.stdout.splitlines() if row]
        frames = [{"function": rows[i], "file": rows[i + 1]}
                  for i in range(0, len(rows) - 1, 2)]
        return {"address": address, "link_address": link_address,
                "elf": str(owner), "available": bool(frames), "frames": frames}

    def code_context(self, address: int, radius: int = 4) -> dict:
        """Return nearby disassembly for a sampled PC when a loaded ELF covers it."""
        owner = None
        for path in reversed(self.sources):
            for section, (base, size) in self._sections[path].items():
                delta = self._section_deltas.get((path, section), 0)
                if base + delta <= (address & ~1) < base + delta + size:
                    owner = path
                    break
            if owner:
                break
        if owner is None:
            return {"address": address, "available": False,
                    "reason": "PC is outside loaded ELF sections"}
        symbol = self.nearest(address, elf=owner)
        if not symbol:
            return {"address": address, "available": False,
                    "elf": str(owner), "reason": "no preceding symbol"}
        name = symbol["name"]
        try:
            listing = self.disassemble(name, elf=owner)
        except (KeyError, ValueError, OSError, subprocess.SubprocessError) as exc:
            return {"address": address, "available": False,
                    "symbol": symbol, "elf": str(owner), "reason": str(exc)}
        section = symbol.get("section")
        delta = self._section_deltas.get((owner, section), 0) if section else 0
        link_pc = (address & ~1) - delta
        instructions = []
        for line in listing.splitlines():
            match = re.match(r"\s*([0-9a-fA-F]+):\s", line)
            if match:
                instructions.append((int(match.group(1), 16), line.rstrip()))
        if not instructions:
            return {"address": address, "available": False,
                    "symbol": symbol, "elf": str(owner),
                    "reason": "objdump returned no instructions"}
        index = min(range(len(instructions)),
                    key=lambda i: abs(instructions[i][0] - link_pc))
        selected = instructions[max(0, index - radius):index + radius + 1]
        return {"address": address, "link_address": link_pc,
                "symbol": symbol, "elf": str(owner),
                "instructions": [line for _, line in selected]}

    def sections(self, elf: str | Path | None = None) -> dict[str, tuple[int, int]]:
        """Return section link-time bases and sizes for a loaded ELF."""
        if elf is None:
            if not self.sources:
                return {}
            owner = self.sources[-1]
        else:
            owner = Path(elf).expanduser().resolve()
        return dict(self._sections[owner])

    def nearest(self, address: int, elf: str | Path | None = None,
                max_distance: int | None = None) -> dict | None:
        """Return the closest preceding symbol and its offset from an address."""
        selected = Path(elf).expanduser().resolve() if elf is not None else None
        lookup_address = address & ~1
        candidates = []
        for name, raw in self._symbols.items():
            owner = self._symbol_owners[name]
            if selected is not None and owner != selected:
                continue
            resolved = self[name]
            if self._symbol_types.get(name) == "STT_FUNC":
                resolved &= ~1  # ELF Thumb function values carry the ISA bit.
            if resolved <= lookup_address:
                candidates.append((resolved, self._symbol_types.get(name, ""), name, owner))
        # Preserve local/static routines whose names repeat across objects/ELFs.
        for row in self.functions():
            owner = Path(row["elf"])
            value = row["address"] & ~1
            if (selected is None or owner == selected) and value <= lookup_address:
                candidates.append((value, "STT_FUNC", row["name"], owner))
        if not candidates:
            return None
        value, kind, name, owner = max(candidates,
                                       key=lambda item: (item[0], item[1] == "STT_FUNC"))
        offset = lookup_address - value
        if max_distance is not None and offset > max_distance:
            return None
        return {"name": name, "address": value, "offset": offset,
                "type": kind, "elf": str(owner)}

    def _frame_entries(self, owner: Path) -> list[tuple[int, int, FDE]]:
        if owner in self._map_sources:
            return []
        if owner not in self._cfi:
            with owner.open("rb") as stream:
                image = ELFFile(stream)
                dwarf = image.get_dwarf_info()
                entries = []
                for entry in dwarf.CFI_entries():
                    if isinstance(entry, FDE):
                        start = int(entry["initial_location"])
                        entries.append((start, start + int(entry["address_range"]), entry))
            self._cfi[owner] = sorted(entries, key=lambda row: row[0])
        return self._cfi[owner]

    def _owner_for_pc(self, pc: int) -> tuple[Path, int] | None:
        address = pc & ~1
        for owner in reversed(self.sources):
            for section, (start, size) in self._sections[owner].items():
                runtime_start = start + self._section_deltas.get((owner, section), 0)
                if runtime_start <= address < runtime_start + size:
                    return owner, address - self._section_deltas.get((owner, section), 0)
        return None

    def unwind(self, registers: dict[str, int], read_memory,
               max_frames: int = 32) -> dict:
        """Unwind ARM frames using the loaded ELF .debug_frame CFI."""
        current = {index: registers.get(f"r{index}", 0) for index in range(13)}
        current[13] = registers.get("sp", 0)
        current[14] = registers.get("lr", 0)
        current[15] = registers.get("pc", 0)
        frames = []
        reason = "frame limit reached"
        seen: set[tuple[int, int]] = set()

        for index in range(max_frames):
            pc = current[15] & ~1
            sp = current[13]
            if pc == 0 or (pc, sp) in seen:
                reason = "zero or repeated PC/SP"
                break
            seen.add((pc, sp))
            located = self._owner_for_pc(pc)
            owner = located[0] if located else None
            frame = {"index": index, "pc": pc,
                     "symbol": self.nearest(pc, elf=owner) if owner else None}
            frames.append(frame)
            if owner is None:
                reason = "PC is outside loaded ELF sections"
                break

            link_pc = located[1]
            entries = self._frame_entries(owner)
            starts = [row[0] for row in entries]
            pos = bisect_right(starts, link_pc) - 1
            fde = None
            while pos >= 0:
                start, end, candidate = entries[pos]
                if start <= link_pc < end:
                    fde = candidate
                    break
                if start < link_pc and end <= link_pc:
                    break
                pos -= 1
            if fde is None:
                reason = "no .debug_frame entry for PC"
                break

            table = fde.get_decoded().table
            row = next((candidate for candidate in reversed(table)
                        if candidate["pc"] <= link_pc), None)
            if row is None:
                reason = "no unwind rule for PC"
                break
            cfa_rule = row.get("cfa")
            if cfa_rule is None or cfa_rule.reg is None:
                reason = "unsupported CFA expression"
                break
            cfa = current.get(cfa_rule.reg)
            if cfa is None:
                reason = f"CFA register r{cfa_rule.reg} is unavailable"
                break
            cfa += cfa_rule.offset
            previous = dict(current)
            for regnum, rule in row.items():
                if not isinstance(regnum, int) or not hasattr(rule, "type"):
                    continue
                if rule.type == RegisterRule.UNDEFINED:
                    previous.pop(regnum, None)
                elif rule.type == RegisterRule.SAME_VALUE:
                    pass
                elif rule.type == RegisterRule.OFFSET:
                    previous[regnum] = int.from_bytes(
                        read_memory(cfa + rule.arg, 4), "little")
                elif rule.type == RegisterRule.VAL_OFFSET:
                    previous[regnum] = cfa + rule.arg
                elif rule.type == RegisterRule.REGISTER:
                    if rule.arg in current:
                        previous[regnum] = current[rule.arg]
                    else:
                        previous.pop(regnum, None)
                else:
                    reason = f"unsupported unwind rule {rule.type}"
                    frame["unwind_error"] = reason
                    return {"frames": frames, "stop_reason": reason}
            return_reg = int(fde.cie["return_address_register"])
            return_pc = previous.get(return_reg)
            if return_pc is None or return_pc == 0:
                reason = "return address unavailable"
                break
            previous[13] = cfa
            previous[15] = return_pc
            current = previous
        else:
            reason = "frame limit reached"
        return {"frames": frames, "stop_reason": reason}


class GwemuGDBBackend(GDBBackend):
    """GDBBackend adapter that uses QEMU's target XML register numbering."""

    def __init__(self, host="localhost", port=1234):
        super().__init__(host=host, port=port)
        self.register_numbers: dict[str, int] = {}

    def _feature_xml(self, annex: str) -> bytes:
        payload = bytearray()
        offset = 0
        while True:
            command = f"qXfer:features:read:{annex}:{offset:x},fff".encode()
            reply = self._send_command(command)
            if not reply or reply[:1] not in (b"m", b"l"):
                raise RuntimeError(f"target did not provide GDB feature {annex}")
            payload.extend(reply[1:])
            offset += len(reply) - 1
            if reply[:1] == b"l":
                return bytes(payload)

    def _load_register_numbers(self):
        visited: set[str] = set()
        next_number = 0

        def read_feature(annex: str):
            nonlocal next_number
            if annex in visited:
                return
            visited.add(annex)
            feature_xml = self._feature_xml(annex)
            # GWemu's target XML uses xi:include without declaring the
            # XInclude namespace, which strict XML parsers reject. GDB knows
            # the target-description convention; provide the missing binding.
            if b"xi:" in feature_xml and b"xmlns:xi=" not in feature_xml:
                feature_xml = feature_xml.replace(
                    b"<target>",
                    b'<target xmlns:xi="http://www.w3.org/2001/XInclude">',
                    1,
                )
            root = ET.fromstring(feature_xml)
            for node in root.iter():
                if node.tag.endswith("include"):
                    href = node.attrib.get("href")
                    if href:
                        read_feature(href)
                elif node.tag.endswith("reg"):
                    name = node.attrib.get("name")
                    if not name:
                        continue
                    number = node.attrib.get("regnum")
                    index = int(number, 0) if number else next_number
                    self.register_numbers[name.lower()] = index
                    next_number = max(next_number, index + 1)

        read_feature("target.xml")

    def halt(self):
        # gnwmanager's default halt sends '?' after Ctrl-C. QEMU treats
        # that packet as a fresh attachment and removes all breakpoints.
        # A register-bank read synchronizes with the interrupt while the
        # transport consumes any asynchronous stop packet, preserving hooks.
        from .qmp import record_control_request
        record_control_request(getattr(self, "_control_audit_socket", None), "gdb", "halt")
        self._write(b"\x03")
        reply = self._send_command(b"g")
        self._decode_hex(reply, b"g")
        self._is_running = False

    def resume(self):
        if getattr(self, "_opening_halted", False):
            self._is_running = False
            return
        from .qmp import record_control_request
        record_control_request(getattr(self, "_control_audit_socket", None), "gdb", "resume")
        return super().resume()

    def single_step(self):
        """Consume the step stop packet; the base transport treats it as asynchronous."""
        from .qmp import record_control_request
        record_control_request(getattr(self, "_control_audit_socket", None), "gdb", "step")
        packet = b"$s#73"
        self._write(packet)
        while True:
            character = self._read(1)
            if character == b"+":
                break
            if character == b"-":
                self._write(packet)
            elif character == b"$":
                self._read_packet_data()
        reply = self._wait_for_packet()
        if not reply.startswith((b"T", b"S")):
            raise RuntimeError(f"unexpected single-step reply: {reply!r}")
        self._is_running = False
        return reply

    def open(self, *, halt: bool = False):
        """Attach without releasing a paused target when halt=True."""
        from .qmp import record_control_request
        record_control_request(getattr(self, "_control_audit_socket", None), "gdb",
                               "attach_halted" if halt else "attach_resume")
        self._opening_halted = halt
        try:
            super().open()
        except Exception as exc:
            cause = exc.__cause__
            if isinstance(cause, OSError) and cause.errno in (errno.EACCES, errno.EPERM):
                raise PermissionError(
                    f"GWemu GDB socket {self.host}:{self.port} denied access; "
                    "run gwprov with TCP socket access. NET_ADMIN is not normally "
                    "required for a localhost GDB connection."
                ) from exc
            raise
        finally:
            self._opening_halted = False
        self._load_register_numbers()
        return self

    def _register_index(self, name: str) -> int:
        key = name.lower()
        aliases = {"msp": "sp", "r13": "sp", "r14": "lr", "r15": "pc",
                   "cpsr": "xpsr"}
        key = aliases.get(key, key)
        if key not in self.register_numbers:
            raise ValueError(f"target does not advertise register {name!r}")
        return self.register_numbers[key]

    def read_register(self, name: str) -> int:
        was_running = self._is_running
        if was_running:
            self.halt()
        index = self._register_index(name)
        command = f"p{index:x}".encode("ascii")
        reply = self._send_command(command)
        if was_running:
            self.resume()
        return int.from_bytes(self._decode_hex(reply, command), "little")

    def read_core_registers(self) -> dict[str, int]:
        """Read the ARM core register bank with one GDB remote packet."""
        was_running = self._is_running
        if was_running:
            self.halt()
        try:
            command = b"g"
            reply = self._send_command(command)
            raw = self._decode_hex(reply, command)
            if len(raw) < 16 * 4:
                raise RuntimeError(f"short GDB core-register packet ({len(raw)} bytes)")
            names = [*(f"r{i}" for i in range(13)), "sp", "lr", "pc"]
            values = [int.from_bytes(raw[i * 4:i * 4 + 4], "little")
                      for i in range(16)]
            return dict(zip(names, values))
        finally:
            if was_running:
                self.resume()

    def write_register(self, name: str, value: int):
        was_running = self._is_running
        if was_running:
            self.halt()
        index = self._register_index(name)
        command = f"P{index:x}={value.to_bytes(4, 'little').hex()}".encode("ascii")
        reply = self._send_command(command)
        if was_running:
            self.resume()
        if reply != b"OK":
            raise RuntimeError(f"register write failed: {reply!r}")


class DebugSession:
    """Convenience API exposed as ``dbg`` in the interactive Python console."""

    def __init__(self, backend, transport: str, qmp_socket: str | None = None):
        self.backend = backend
        self.transport = transport
        self.qmp_socket = qmp_socket
        if transport == "gwemu" and backend is not None:
            backend._control_audit_socket = qmp_socket
        self.symbols = SymbolTable()

    def halt(self):
        result = self.backend.halt()
        if self.transport == "gwemu":
            self.backend._is_running = False
        return result

    def resume(self):
        result = self.backend.resume()
        if self.transport == "gwemu":
            self.backend._is_running = True
        return result

    def _target_state(self) -> str:
        if self.transport == "gwemu":
            return "running" if self.backend._is_running else "halted"
        target = getattr(self.backend, "target", None)
        if target is not None:
            return target.get_state().name.lower()
        # OpenOCD and remote gnwmanager both expose memory reads. DHCSR is a
        # non-invasive Cortex-M execution-state query.
        dhcsr = int.from_bytes(self.backend.read_memory(0xE000EDF0, 4), "little")
        if dhcsr & (1 << 17):
            return "halted"
        if dhcsr & (1 << 19):
            return "lockup"
        if dhcsr & (1 << 18):
            return "sleeping"
        if dhcsr & (1 << 25):
            return "reset"
        return "running"

    def _target_is_running(self) -> bool:
        # Cortex-M core registers are not readable while the core is sleeping
        # in WFI. Treat sleep as active execution for helpers that temporarily
        # halt the target to capture coherent register or memory state.
        return self._target_state() in {"running", "sleeping"}

    def wait_stopped(self, timeout: float = 30.0, poll_interval: float = 0.05) -> dict:
        """Wait for a debug stop without interrupting a running target.

        GWemu uses QMP. Hardware polls the Cortex-M state through the selected
        probe/gnwmanager session while its process-shared target lease is held.
        A timeout leaves execution running.
        """
        if self.transport == "gwemu" and not self.qmp_socket:
            raise ValueError("GWemu wait_stopped requires a QMP endpoint")
        if timeout < 0 or poll_interval <= 0:
            raise ValueError("timeout must be nonnegative and poll_interval positive")
        deadline = time.monotonic() + timeout
        while True:
            if self.transport == "gwemu":
                status = self.qmp("query-status").get("return", {})
                running = status.get("running", False)
                state = status.get("status")
            else:
                state = self._target_state()
                running = state in {"running", "sleeping"}
            if not running:
                if self.transport == "gwemu":
                    self.backend._is_running = False
                return {"stopped": True, "status": state,
                        "stop_reply": None, "registers": self.regs()}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"stopped": False, "status": state, "reason": "timeout"}
            time.sleep(min(poll_interval, remaining))

    def run_until(self, location: str | int, timeout: float = 30.0) -> dict:
        """Resume to one temporary breakpoint; report other stops and timeouts."""
        if self.transport == "gwemu" and not self.qmp_socket:
            raise ValueError("GWemu run_until requires a QMP endpoint")
        address = (self.at(location) if isinstance(location, str) else location) & ~1
        response = self.bp(address)
        if response != "OK":
            raise RuntimeError(f"breakpoint installation failed: {response}")
        try:
            self.resume()
            result = self.wait_stopped(timeout)
            result["breakpoint"] = address
            result["hit"] = bool(result["stopped"] and
                                 result["registers"]["pc"] == address)
            return result
        finally:
            try:
                self.clear_bp(address)
            except Exception as cleanup_error:
                # Keep the original run/wait failure visible when the target
                # has reset or disconnected and removed its breakpoints.
                if "result" in locals():
                    result["cleanup_error"] = str(cleanup_error)

    def reset(self, halt: bool = False):
        if halt:
            return self.backend.reset_and_halt()
        return self.backend.reset()

    def step(self):
        """Execute one instruction and leave the target halted."""
        self.backend.halt()
        if self.transport == "gwemu":
            reply = self.backend.single_step()
            self.backend._is_running = False
            return reply.decode("ascii", errors="replace")
        target = getattr(self.backend, "target", None)
        if target is not None:
            target.step()
            return "stepped"
        return self.backend("step", decode=False).decode("utf-8", errors="replace")

    def reg(self, name: str) -> int:
        was_running = self._target_is_running()
        if was_running:
            self.backend.halt()
        try:
            return self.backend.read_register(name)
        finally:
            if was_running:
                self.backend.resume()

    def regs(self) -> dict[str, int]:
        was_running = self._target_is_running()
        if was_running:
            self.backend.halt()
        try:
            names = [*(f"r{i}" for i in range(13)), "sp", "lr", "pc", "xpsr"]
            return {name: self.reg(name) for name in names}
        finally:
            if was_running:
                self.backend.resume()

    def fault_context(self, registers: dict[str, int] | None = None) -> dict:
        """Decode a Cortex-M fault captured at handler entry, before its prologue.

        EXC_RETURN describes the pre-exception stack and FP frame. The stacked
        core registers start at the exception SP; an extended FP frame follows them.
        Read only while halted so the frame and SCB registers stay coherent.
        """
        import struct
        current = registers or self.regs()
        exception = current.get("xpsr", 0) & 0x1ff
        exc_return = current.get("lr", 0)
        if exception not in {3, 4, 5, 6} or exc_return & 0xffffff00 != 0xffffff00:
            return {"available": False, "reason": "not at a Cortex-M fault handler entry"}
        if exc_return & 4:
            frame_address = self.reg("psp")
        else:
            frame_address = current["sp"]
        try:
            handler_entry = self.symbols["common_fault_handler_c"] & ~1
        except KeyError:
            handler_entry = None
        if handler_entry is not None and (current.get("pc", 0) & ~1) == handler_entry:
            # The C fault handler receives the original exception SP in r0.
            # Its current SP is also valid at entry, but r0 makes the contract
            # explicit and keeps this correct if the stub reports a banked SP.
            frame_address = current["r0"]
        extended = not bool(exc_return & (1 << 4))
        core_frame_address = frame_address
        raw = self.read(core_frame_address, 32)
        values = struct.unpack("<8I", raw)
        if not values[7] & (1 << 24):
            return {"available": False, "reason": "stacked xPSR has no Thumb bit",
                    "frame_address": frame_address,
                    "core_frame_address": core_frame_address,
                    "extended_fp_frame": extended, "frame_bytes": raw.hex()}
        names = ["r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr"]
        stacked = dict(zip(names, values))
        padding = 4 if values[7] & (1 << 9) else 0
        recovered = dict(current)
        recovered.update(stacked)
        recovered["sp"] = frame_address + 32 + (72 if extended else 0) + padding
        scb_values = struct.unpack("<6I", self.read(0xe000ed28, 24))
        scb = dict(zip(["CFSR", "HFSR", "DFSR", "MMFAR", "BFAR", "AFSR"], scb_values))
        cfsr = scb["CFSR"]
        bits = {0: "IACCVIOL", 1: "DACCVIOL", 3: "MUNSTKERR", 4: "MSTKERR",
                5: "MLSPERR", 7: "MMARVALID", 8: "IBUSERR", 9: "PRECISERR",
                10: "IMPRECISERR", 11: "UNSTKERR", 12: "STKERR", 13: "LSPERR",
                15: "BFARVALID", 16: "UNDEFINSTR", 17: "INVSTATE", 18: "INVPC",
                19: "NOCP", 24: "UNALIGNED", 25: "DIVBYZERO"}
        result = {"available": True,
                  "exception": {3: "HardFault", 4: "MemManage", 5: "BusFault", 6: "UsageFault"}[exception],
                  "exception_return": exc_return, "frame_address": frame_address,
                  "core_frame_address": core_frame_address,
                  "frame_bytes": raw.hex(), "extended_fp_frame": extended,
                  "registers": recovered, "scb": scb,
                  "fault_flags": [name for bit, name in bits.items() if cfsr & (1 << bit)],
                  "fault_address": scb["MMFAR"] if cfsr & (1 << 7)
                                   else scb["BFAR"] if cfsr & (1 << 15) else None}
        memmanage_status = cfsr & 0x3F
        if result["exception"] == "MemManage" or memmanage_status:
            try:
                result["mpu"] = self._mpu_context(
                    pc=recovered["pc"], fault_address=result["fault_address"],
                    cfsr=cfsr)
            except Exception as exc:
                result["mpu"] = {"available": False, "reason": str(exc)}
        try:
            guard = self.at("_stack_redzone")
            size = self.at("_Stack_Redzone_Size")
            address = result["fault_address"]
            result["stack_guard"] = {"address": guard, "size": size,
                                      "hit": address is not None and guard <= address < guard + size}
        except KeyError:
            pass
        result["source_location"] = self.symbols.source_location(recovered["pc"])
        result["code_context"] = self.symbols.code_context(recovered["pc"])
        return result

    def _read_u32_register(self, address: int) -> int:
        """Read a peripheral register as one aligned word transaction."""
        if address & 3:
            raise ValueError(f"unaligned 32-bit register address 0x{address:08x}")
        backend = self.backend
        if hasattr(backend, "read_uint32"):
            return int(backend.read_uint32(address))
        target = getattr(backend, "target", None)
        if target is not None and hasattr(target, "read32"):
            return int(target.read32(address))
        # QEMU's GDB debug-memory path supports aligned word reads as a
        # single request. OpenOCD bytewise accessors are deliberately excluded
        # because peripheral registers need mdw. Remote access needs an
        # explicit word-operation protocol before it is safe for MPU registers.
        if backend.__class__.__name__ == "GwemuGDBBackend":
            return int.from_bytes(self.read(address, 4), "little")
        raise RuntimeError(f"{backend.__class__.__name__} has no safe 32-bit register read")

    def _write_u32_register(self, address: int, value: int):
        """Write a peripheral register as one aligned word transaction."""
        if address & 3:
            raise ValueError(f"unaligned 32-bit register address 0x{address:08x}")
        backend = self.backend
        if hasattr(backend, "write_uint32"):
            backend.write_uint32(address, value)
            return
        target = getattr(backend, "target", None)
        if target is not None and hasattr(target, "write32"):
            target.write32(address, value)
            return
        if backend.__class__.__name__ == "GwemuGDBBackend":
            self.write(address, value.to_bytes(4, "little"))
            return
        raise RuntimeError(f"{backend.__class__.__name__} has no safe 32-bit register write")

    def _mpu_context(self, *, pc: int, fault_address: int | None,
                     cfsr: int) -> dict:
        """Capture ARMv7-M MPU regions and identify the effective fault region.

        MPU_RNR is temporarily changed to read RBAR/RASR and restored before
        returning. Higher numbered enabled regions take precedence.
        """
        mpu_type_address = 0xE000ED90
        mpu_ctrl_address = 0xE000ED94
        mpu_rnr_address = 0xE000ED98
        mpu_rbar_address = 0xE000ED9C
        mpu_rasr_address = 0xE000EDA0
        mpu_type = self._read_u32_register(mpu_type_address)
        mpu_ctrl = self._read_u32_register(mpu_ctrl_address)
        region_count = (mpu_type >> 8) & 0xFF
        if region_count == 0:
            return {"available": True, "architecture": "ARMv7-M",
                    "type": mpu_type, "control": mpu_ctrl,
                    "enabled": bool(mpu_ctrl & 1), "regions": [], "pc": pc,
                    "fault_address": fault_address}

        original_rnr = self._read_u32_register(mpu_rnr_address)
        regions = []
        restore_error = None
        try:
            for index in range(region_count):
                self._write_u32_register(mpu_rnr_address, index)
                rbar = self._read_u32_register(mpu_rbar_address)
                rasr = self._read_u32_register(mpu_rasr_address)
                enabled = bool(rasr & 1)
                size = (1 << (((rasr >> 1) & 0x1F) + 1)) if enabled else None
                base = ((rbar & 0xFFFFFFE0) & ~(size - 1)) if enabled else None
                subregion_mask = (rasr >> 8) & 0xFF
                def contains(address):
                    if not enabled or not (mpu_ctrl & 1) or address is None:
                        return False, False
                    if not base <= address < base + size:
                        return False, False
                    disabled = False
                    if size >= 256:
                        subregion = (address - base) // (size // 8)
                        disabled = bool(subregion_mask & (1 << subregion))
                    return not disabled, disabled
                pc_covered, pc_subregion_disabled = contains(pc)
                addr_covered, addr_subregion_disabled = contains(fault_address)
                regions.append({"number": index, "enabled": enabled,
                                "base": base, "size_bytes": size,
                                "xn": bool(rasr & (1 << 28)),
                                "access_permission": (rasr >> 24) & 7,
                                "subregion_disable_mask": subregion_mask,
                                "covers_pc": pc_covered,
                                "pc_subregion_disabled": pc_subregion_disabled,
                                "covers_fault_address": addr_covered,
                                "fault_address_subregion_disabled": addr_subregion_disabled,
                                "rbar": rbar, "rasr": rasr})
        finally:
            try:
                self._write_u32_register(mpu_rnr_address, original_rnr)
            except Exception as exc:
                restore_error = str(exc)

        enabled_regions = [r for r in regions if r["enabled"] and (mpu_ctrl & 1)]
        def effective(field):
            matches = [r for r in enabled_regions if r[field]]
            return max(matches, key=lambda r: r["number"]) if matches else None
        result = {"available": True, "architecture": "ARMv7-M",
                  "type": mpu_type, "control": mpu_ctrl,
                  "enabled": bool(mpu_ctrl & 1),
                  "privileged_default": bool(mpu_ctrl & 4), "pc": pc,
                  "fault_address": fault_address,
                  "effective_pc_region": effective("covers_pc"),
                  "effective_fault_address_region": effective("covers_fault_address"),
                  "regions": regions}
        if restore_error:
            result["region_selector_restore_error"] = restore_error
        return result

    def traceback(self, max_frames: int = 32) -> dict:
        """Return symbol-resolved ARM call frames from the current target state."""
        if max_frames < 1:
            raise ValueError("max_frames must be positive")
        was_running = self._target_is_running()
        if was_running:
            self.backend.halt()
        try:
            if self.transport == "gwemu" and hasattr(self.backend, "read_core_registers"):
                registers = self.backend.read_core_registers()
            else:
                registers = {f"r{i}": self.reg(f"r{i}") for i in range(13)}
                registers.update({name: self.reg(name) for name in ("sp", "lr", "pc")})
            registers["xpsr"] = self.reg("xpsr")
            result = self.symbols.unwind(registers, self.read, max_frames)
            result["registers"] = registers
            fault = self.fault_context(registers)
            if fault.get("available"):
                original = self.symbols.unwind(fault["registers"], self.read, max_frames)
                result["handler_frames"] = result["frames"]
                handler = [frame for frame in result["frames"] if frame.get("symbol")]
                result["frames"] = handler + original["frames"]
                for index, frame in enumerate(result["frames"]):
                    frame["index"] = index
                result["stop_reason"] = original["stop_reason"]
                result["fault"] = fault
            return result
        finally:
            if was_running:
                self.backend.resume()

    def where(self) -> dict:
        was_running = self._target_is_running()
        if was_running:
            self.backend.halt()
        try:
            addresses = {name: self.reg(name) for name in ("pc", "lr")}
            result = {}
            for name, address in addresses.items():
                elf = next((str(path) for path in reversed(self.symbols.sources)
                            if any(self._in_section(address, path, section)
                                   for section in self.symbols.sections(path))), None)
                result[name] = {"address": address,
                                "symbol": self.symbols.nearest(address, elf=elf) if elf else None}
            return result
        finally:
            if was_running:
                self.backend.resume()

    def _in_section(self, address: int, elf: str, section: str) -> bool:
        start, size = self.symbols.sections(elf)[section]
        return start <= address < start + size

    def read(self, address: int, size: int = 4) -> bytes:
        return self.backend.read_memory(address, size)

    def read_value(self, symbol: str, *, max_bytes: int = 65536):
        """Decode a scalar, struct, or fixed array using ELF types and live bytes.

        Pointers remain numeric addresses; they are never followed implicitly.
        Unsupported or missing types fail explicitly instead of guessing offsets.
        """
        from .debug_types import decode_value
        layout = self.symbols.type_layout(symbol)
        if layout["size"] > max_bytes:
            raise ValueError(f"{symbol} is {layout['size']} bytes; limit is {max_bytes}")
        return decode_value(layout, self.read(self.at(symbol), layout["size"]))

    def memory_path(self, expression: str) -> dict:
        """Resolve a dotted C global/member path using DWARF and live pointers."""
        from .debug_types import path_layout
        parts = expression.split(".")
        if not parts or any(not part.isidentifier() for part in parts):
            raise ValueError("memory paths use GLOBAL.member identifiers")
        root = parts[0]
        layout = path_layout(self.symbols.owner(root), root, tuple(parts[1:]),
                             self.symbols.source_hint(root), self.symbols.link_address(root))
        address = self.at(root)
        for operation in layout["operations"]:
            if operation["kind"] == "dereference":
                address = int.from_bytes(self.read(address, operation["size"]),
                                         layout["type"]["byteorder"])
                if address == 0:
                    raise ValueError(f"null pointer resolving {expression!r}")
            else:
                address += operation["bytes"]
        return {"expression": expression, "address": address,
                "size": layout["type"]["size"], "type": layout["type"]}

    def read_path(self, expression: str, *, max_bytes: int = 65536):
        """Decode a dotted C global/member path; intermediate pointers are followed."""
        from .debug_types import decode_value
        resolved = self.memory_path(expression)
        if resolved["size"] > max_bytes:
            raise ValueError(f"{expression} is {resolved['size']} bytes; limit is {max_bytes}")
        return decode_value(resolved["type"], self.read(resolved["address"], resolved["size"]))

    def read_paths(self, expressions, *, max_bytes=65536, max_total_bytes=4 * 1024 * 1024,
                   errors="raise") -> dict:
        """Read typed fields together, sharing pointer reads and adjacent ranges.

        Only requested bytes are read: no gap filling or MMIO over-read. Pointer
        values are cached for this call only. Caller owns any required halt.
        Returns explicit values/errors plus transport metrics; no app schema.
        """
        from .debug_types import path_layouts, decode_value
        if errors not in ("raise", "collect"):
            raise ValueError("errors must be raise or collect")
        if max_bytes <= 0 or max_total_bytes <= 0:
            raise ValueError("read limits must be positive")
        if isinstance(expressions, str):
            raise ValueError("read_paths requires an iterable of paths, not one string")
        expressions = tuple(dict.fromkeys(expressions))
        groups, failures, resolved = {}, {}, {}
        pointer_cache = {}
        def fail(expression, error):
            if errors == "raise":
                raise error
            failures[expression] = {"type": type(error).__name__, "message": str(error)}
        for expression in expressions:
            parts = expression.split(".")
            if not parts or any(not part.isidentifier() for part in parts):
                raise ValueError("memory paths use GLOBAL.member identifiers")
            try:
                owner = self.symbols.owner(parts[0])
            except (KeyError, ValueError) as error:
                fail(expression, error)
                continue
            groups.setdefault((owner, parts[0]), []).append((expression, tuple(parts[1:])))
        for (owner, root), fields in groups.items():
            try:
                hint = getattr(self.symbols, 'source_hint', lambda symbol: None)(root)
                options = {}
                if hint:
                    options['source_hint'] = hint
                if hasattr(self.symbols, 'link_address'):
                    options['symbol_address'] = self.symbols.link_address(root)
                plans = path_layouts(owner, root, [members for _, members in fields], **options)
            except (KeyError, ValueError) as error:
                for expression, _ in fields:
                    fail(expression, error)
                continue
            for expression, members in fields:
                plan = plans[members]
                try:
                    if "error" in plan:
                        exception = KeyError if plan["error_type"] == "KeyError" else ValueError
                        raise exception(plan["error"])
                    if plan["type"]["size"] > max_bytes:
                        raise ValueError(f"{expression} exceeds the per-value read limit")
                    address = self.at(root)  # Includes current section rebasing.
                    for operation in plan["operations"]:
                        if operation["kind"] == "dereference":
                            key = (address, operation["size"])
                            if key not in pointer_cache:
                                pointer_cache[key] = self.read(*key)
                            address = int.from_bytes(pointer_cache[key], plan["type"]["byteorder"])
                            if address == 0:
                                raise ValueError(f"null pointer resolving {expression!r}")
                        else:
                            address += operation["bytes"]
                    resolved[expression] = (address, plan["type"])
                except (KeyError, ValueError) as error:
                    fail(expression, error)
        ranges = []
        for address, layout in sorted(resolved.values(), key=lambda item: item[0]):
            end = address + layout["size"]
            if ranges and address <= ranges[-1][1]:
                ranges[-1][1] = max(ranges[-1][1], end)
            else:
                ranges.append([address, end])
        total = sum(end - start for start, end in ranges)
        if total > max_total_bytes:
            raise ValueError(f"requested memory union {total} exceeds total read limit")
        regions = [(start, end, self.read(start, end - start)) for start, end in ranges]
        values = {}
        for expression, (address, layout) in resolved.items():
            start, _, data = next(region for region in regions
                                 if region[0] <= address and address + layout["size"] <= region[1])
            offset = address - start
            values[expression] = decode_value(layout, data[offset:offset + layout["size"]])
        return {"values": {name: values[name] for name in expressions if name in values},
                "errors": {name: failures[name] for name in expressions if name in failures},
                "memory_reads": len(regions), "pointer_reads": len(pointer_cache),
                "requested_union_bytes": total,
                "transport_bytes": total + sum(len(data) for data in pointer_cache.values()),
                "regions": [{"address": start, "size": end - start} for start, end in ranges]}

    def read_ring(self, symbol: str, head_symbol: str, *, max_bytes: int = 65536) -> dict:
        """Read a fixed C array with a monotonic next-write head, oldest first.

        The two globals are sampled under one halt. The head must count all
        writes, rather than only hold a wrapped array index.
        """
        was_running = self._target_is_running()
        if was_running:
            self.halt()
        try:
            head = self.read_value(head_symbol)
            entries = self.read_value(symbol, max_bytes=max_bytes)
            if not isinstance(head, int) or head < 0:
                raise ValueError("ring head must be a nonnegative integer counter")
            if not isinstance(entries, list) or not entries:
                raise ValueError("ring symbol must be a nonempty fixed C array")
            capacity = len(entries)
            count = min(head, capacity)
            ordered = [entries[(head - count + i) % capacity] for i in range(count)]
            return {"symbol": symbol, "head_symbol": head_symbol, "head": head,
                    "capacity": capacity, "overwritten": max(0, head - capacity),
                    "entries": ordered}
        finally:
            if was_running:
                self.resume()

    def u32(self, address: int) -> int:
        return int.from_bytes(self.read(address, 4), "little")

    def write(self, address: int, data: bytes):
        return self.backend.write_memory(address, data)

    def write_u32(self, address: int, value: int):
        return self.write(address, value.to_bytes(4, "little"))

    def configure(self, path) -> dict:
        """Load the shared local port description and apply initialized mappings."""
        settings = self.symbols.load_config(path)
        for spec in settings["rebase_symbols"]:
            section, pointer = spec.split("=", 1)
            self.rebase_from_pointer(section, pointer)
        self.debug_configuration = settings
        return settings

    def profile(self, *, duration=15.0, interval=0.02, progress_symbols=None,
                rebase_symbols=None, stop_event=None, stall_threshold=1.0, sample_gauges=None,
                progress_interval=1.0) -> dict:
        """Sample native function PCs through QMP alongside this debug session."""
        if self.transport != "gwemu" or not self.qmp_socket:
            raise RuntimeError("native PC sampling requires a GWemu QMP socket")
        from .profiling import sample_profile
        settings = getattr(self, "debug_configuration", {})
        if progress_symbols is None:
            progress_symbols = settings.get("progress_symbols") or None
        if rebase_symbols is None:
            rebase_symbols = settings.get("rebase_symbols")
        if sample_gauges is None:
            sample_gauges = settings.get("sample_gauges")
        return sample_profile(self.qmp_socket, self.symbols, duration=duration,
                              interval=interval, progress_symbols=progress_symbols,
                              rebase_symbols=rebase_symbols, stop_event=stop_event,
                              stall_threshold=stall_threshold, sample_gauges=sample_gauges,
                              progress_interval=progress_interval)

    def bp(self, address: int):
        """Set a hardware breakpoint, suitable for flash code addresses."""
        address &= ~1  # RSP breakpoints use the instruction address, not Thumb's ISA bit.
        if self.transport == "gwemu":
            return self.backend._send_command(f"Z1,{address:x},2".encode()).decode()
        target = getattr(self.backend, "target", None)
        if target is not None:
            from pyocd.core.target import Target
            if not target.set_breakpoint(address, Target.BreakpointType.HW):
                raise RuntimeError(f"could not set hardware breakpoint at 0x{address:08x}; "
                                   "the probe may have no free comparator slots")
            return "OK"
        if hasattr(self.backend, "__call__"):
            return self.backend(f"bp 0x{address:08x} 2 hw", decode=False).decode().strip()
        raise NotImplementedError("hardware breakpoints are unavailable for this remote backend")

    def watchpoint(self, address: int, size: int = 4, access: str = "read"):
        """Set a DWT or remote-debug watchpoint on a memory access.

        This is useful for discovering code that reads a known MMIO register
        when firmware symbols are unavailable. It watches memory accesses,
        not the value of a CPU register.
        """
        access = access.strip().lower().replace("-", "_")
        rsp_types = {"write": 2, "read": 3, "read_write": 4, "access": 4}
        if access not in {"read", "write", "read_write", "access"}:
            raise ValueError("access must be 'read', 'write', or 'read_write'")
        if not isinstance(address, int) or address < 0 or address > 0xFFFFFFFF:
            raise ValueError("watchpoint address must be a 32-bit integer")
        if size not in {1, 2, 4, 8}:
            raise ValueError("watchpoint size must be 1, 2, 4, or 8 bytes")
        if self.transport == "gwemu":
            kind = rsp_types[access]
            reply = self.backend._send_command(
                f"Z{kind},{address:x},{size:x}".encode()).decode()
            if reply != "OK":
                raise RuntimeError(f"GWemu watchpoint installation failed: {reply}")
            return "OK"
        target = getattr(self.backend, "target", None)
        if target is not None:
            from pyocd.core.target import Target
            kinds = {"read": Target.WatchpointType.READ,
                     "write": Target.WatchpointType.WRITE,
                     "read_write": Target.WatchpointType.READ_WRITE,
                     "access": Target.WatchpointType.READ_WRITE}
            if not target.set_watchpoint(address, size, kinds[access]):
                raise RuntimeError(f"could not set {access} watchpoint at "
                                   f"0x{address:08x}; the target may have no free DWT comparators")
            return "OK"
        if hasattr(self.backend, "_send_command"):
            kind = rsp_types[access]
            reply = self.backend._send_command(
                f"Z{kind},{address:x},{size:x}".encode()).decode()
            if reply != "OK":
                raise RuntimeError(f"remote watchpoint installation failed: {reply}")
            return "OK"
        raise NotImplementedError("watchpoints are unavailable for this debug backend")

    def clear_watchpoint(self, address: int, size: int = 4, access: str = "read"):
        """Remove a watchpoint previously set with :meth:`watchpoint`."""
        access = access.strip().lower().replace("-", "_")
        if access not in {"read", "write", "read_write", "access"}:
            raise ValueError("access must be 'read', 'write', or 'read_write'")
        if self.transport == "gwemu":
            kind = {"write": 2, "read": 3, "read_write": 4, "access": 4}[access]
            reply = self.backend._send_command(
                f"z{kind},{address:x},{size:x}".encode()).decode()
            if reply != "OK":
                raise RuntimeError(f"GWemu watchpoint removal failed: {reply}")
            return "OK"
        target = getattr(self.backend, "target", None)
        if target is not None:
            from pyocd.core.target import Target
            kinds = {"read": Target.WatchpointType.READ,
                     "write": Target.WatchpointType.WRITE,
                     "read_write": Target.WatchpointType.READ_WRITE,
                     "access": Target.WatchpointType.READ_WRITE}
            target.remove_watchpoint(address, size, kinds[access])
            return "OK"
        if hasattr(self.backend, "_send_command"):
            kind = {"write": 2, "read": 3, "read_write": 4, "access": 4}[access]
            reply = self.backend._send_command(
                f"z{kind},{address:x},{size:x}".encode()).decode()
            if reply != "OK":
                raise RuntimeError(f"remote watchpoint removal failed: {reply}")
            return "OK"
        raise NotImplementedError("watchpoints are unavailable for this debug backend")

    def inject_return(self, return_at: str | int, value: int, *,
                      register: str = "r0", timeout: float = 30.0) -> dict:
        """Override one function result at its return site, then resume.

        ``return_at`` is the address of the instruction reached after the
        function has computed its result (or a symbol resolving to that site).
        On ARM EABI, scalar results normally use r0. This primitive is generic;
        callers must know the target routine's ABI and result meaning.
        """
        if not 0 <= value <= 0xFFFFFFFF:
            raise ValueError("injected value must fit in a 32-bit register")
        if register not in {f"r{index}" for index in range(13)}:
            raise ValueError("return register must be r0 through r12")
        address = (self.at(return_at) if isinstance(return_at, str) else return_at) & ~1
        result = self.run_until(address, timeout=timeout)
        if not result.get("hit"):
            return result
        self.backend.write_register(register, value)
        result["injected"] = {"address": address, "register": register,
                              "value": value}
        self.resume()
        return result

    def press_button(self, return_at: str | int, mask: int, *,
                     release_polls: int = 1, hold_polls: int = 1,
                     release_value: int = 0, register: str = "r0",
                     timeout: float = 30.0) -> dict:
        """Pulse a button bit through a routine returning a button mask.

        This works with any firmware whose button-read routine returns a mask
        in the selected register. It first injects release polls to establish
        a clean edge, injects ``mask`` for ``hold_polls``, then injects a
        release. Supply the return-site symbol/address and firmware's button
        mask; the debugger does not assume a project-specific mapping.
        """
        if release_polls < 0 or hold_polls < 1:
            raise ValueError("release_polls must be nonnegative and hold_polls at least one")
        if not 0 <= mask <= 0xFFFFFFFF or not 0 <= release_value <= 0xFFFFFFFF:
            raise ValueError("button masks must fit in a 32-bit register")
        stages = ([release_value] * release_polls + [mask] * hold_polls + [release_value])
        captures = []
        for value in stages:
            result = self.inject_return(return_at, value, register=register, timeout=timeout)
            captures.append(result)
            if not result.get("hit"):
                return {"pressed": False, "reason": "routine return was not reached",
                        "stages": captures}
        return {"pressed": True, "return_at": (self.at(return_at)
                                                   if isinstance(return_at, str)
                                                   else return_at) & ~1,
                "mask": mask, "release_polls": release_polls,
                "hold_polls": hold_polls, "stages": captures}

    def clear_bp(self, address: int):
        address &= ~1
        if self.transport == "gwemu":
            return self.backend._send_command(f"z1,{address:x},2".encode()).decode()
        target = getattr(self.backend, "target", None)
        if target is not None:
            target.remove_breakpoint(address)
            return "OK"
        if hasattr(self.backend, "__call__"):
            return self.backend(f"rbp 0x{address:08x}", decode=False).decode().strip()
        raise NotImplementedError("hardware breakpoints are unavailable for this remote backend")

    def arm_fault_breakpoints(self) -> list[dict]:
        """Install available Cortex-M fault-entry breakpoints before repro."""
        names = ("common_fault_handler_c", "HardFault_Handler", "MemManage_Handler",
                 "BusFault_Handler", "UsageFault_Handler")
        armed = []
        seen = set()
        for name in names:
            try:
                address = self.at(name) & ~1
            except KeyError:
                continue
            if address in seen:
                continue
            seen.add(address)
            self.bp(address)
            armed.append({"symbol": name, "address": address})
        if not armed:
            raise RuntimeError("loaded symbols contain no recognized Cortex-M fault handlers")
        return armed

    def wait_fault(self, timeout: float = 30.0, poll_interval: float = 0.05) -> dict:
        """Wait for an armed stop and return symbolized stack/fault evidence."""
        stop = self.wait_stopped(timeout, poll_interval)
        if not stop.get("stopped"):
            return {"stop": stop, "triage": None}
        return {"stop": stop, "triage": self.traceback()}

    def watchdog_context(self, registers: dict[str, int] | None = None,
                         max_frames: int = 32) -> dict:
        """Decode the interrupted Cortex-M context at a watchdog IRQ entry.

        Unlike a fault, a watchdog reset does not preserve the faulting stack.
        STM32 WWDG early-wakeup arrives as an ordinary IRQ before reset, so its
        hardware-stacked PC/LR can be recovered while halted at
        ``WWDG_IRQHandler``. The result includes source-resolved interrupted
        frames when symbols and unwind data are available.
        """
        import struct
        current = registers or self.regs()
        exception = current.get("xpsr", 0) & 0x1ff
        exc_return = current.get("lr", 0)
        if exception < 16 or exc_return & 0xffffff00 != 0xffffff00:
            return {"available": False,
                    "reason": "not at a Cortex-M exception entry; halt at WWDG_IRQHandler"}
        frame_address = self.reg("psp") if exc_return & 4 else current["sp"]
        extended = not bool(exc_return & (1 << 4))
        # STM32 Cortex-M exception frames place the core registers at the
        # exception SP; an extended FP frame follows the eight core words.
        core_frame_address = frame_address
        raw = self.read(core_frame_address, 32)
        values = struct.unpack("<8I", raw)
        names = ["r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr"]
        stacked = dict(zip(names, values))
        if not stacked["xpsr"] & (1 << 24):
            return {"available": False, "reason": "stacked xPSR has no Thumb bit",
                    "frame_address": frame_address, "frame_bytes": raw.hex()}
        padding = 4 if stacked["xpsr"] & (1 << 9) else 0
        interrupted = dict(current)
        interrupted.update(stacked)
        interrupted["sp"] = frame_address + 32 + (72 if extended else 0) + padding
        handler_pc = current.get("pc", 0)
        handler = self.symbols.nearest(handler_pc)
        result = {"available": True, "exception_number": exception,
                  "handler": handler, "frame_address": frame_address,
                  "core_frame_address": core_frame_address,
                  "extended_fp_frame": extended, "frame_bytes": raw.hex(),
                  "handler_registers": current, "interrupted_registers": interrupted,
                  "interrupted_pc": self.symbols.source_location(stacked["pc"]),
                  "interrupted_lr": self.symbols.source_location(stacked["lr"]),
                  "interrupted_traceback": self.symbols.unwind(interrupted, self.read, max_frames)}
        result["code_context"] = self.symbols.code_context(stacked["pc"])
        return result

    def arm_watchdog_breakpoints(self) -> list[dict]:
        """Break before an STM32 WWDG reset so the interrupted stack survives."""
        names = ("WWDG_IRQHandler", "WWDG1_IRQHandler")
        armed = []
        seen = set()
        for name in names:
            try:
                address = self.at(name) & ~1
            except KeyError:
                continue
            if address in seen:
                continue
            seen.add(address)
            self.bp(address)
            armed.append({"symbol": name, "address": address})
        if not armed:
            raise RuntimeError("loaded symbols contain no WWDG interrupt handler; "
                               "load firmware symbols or set a breakpoint manually")
        return armed

    def wait_watchdog(self, timeout: float = 30.0,
                      poll_interval: float = 0.05) -> dict:
        """Wait for a WWDG early-wakeup IRQ and report its interrupted code path."""
        stop = self.wait_stopped(timeout, poll_interval)
        if not stop.get("stopped"):
            return {"stop": stop, "watchdog": None}
        registers = stop.get("registers") or self.regs()
        context = self.watchdog_context(registers)
        if not context.get("available"):
            return {"stop": stop, "watchdog": context,
                    "triage": self.traceback()}
        return {"stop": stop, "watchdog": context}

    def at(self, symbol: str) -> int:
        return self.symbols[symbol]

    def nm(self, query: str = "", elf: str | Path | None = None) -> list[dict]:
        """Return structured nm-style symbol rows from loaded ELFs."""
        return self.symbols.nm(query, elf)

    def disasm(self, symbol: str, elf: str | Path | None = None) -> str:
        """Return objdump disassembly for a loaded function symbol."""
        return self.symbols.disassemble(symbol, elf)

    def addr2line(self, address: int) -> dict:
        """Resolve a runtime address to source lines in the loaded ELFs."""
        return self.symbols.source_location(address)

    def rebase_from_pointer(self, section: str, pointer_symbol: str) -> int:
        """Read a target-side base pointer and rebase that ELF section to it."""
        actual_base = self.u32(self.at(pointer_symbol))
        if actual_base == 0:
            raise RuntimeError(f"target symbol {pointer_symbol!r} is still null; has the app initialized?")
        return self.symbols.rebase(section, actual_base)

    def screenshot(self, path: str | Path | None = None) -> dict:
        """Save a QMP PNG and return path, dimensions, and black-screen stats."""
        if not self.qmp_socket:
            raise RuntimeError("pass --qmp-socket to enable GWemu screenshots")
        from .gwemu_manager import screenshot_qmp
        return screenshot_qmp(self.qmp_socket, path)

    def diagnose_qmp(self, path=None, *, max_frames: int = 32) -> dict:
        """Recover halted-target evidence when the GDB connection is unusable."""
        if self.transport != "gwemu" or not self.qmp_socket:
            raise ValueError("QMP diagnosis requires a managed GWemu endpoint")
        from .qmp import QMPConnection
        with QMPConnection(self.qmp_socket) as qmp:
            state = qmp.execute("query-status")["return"]
            if state.get("running"):
                raise ValueError("QMP diagnosis requires an intentionally halted target")
            registers = qmp.registers()
            trace = self.symbols.unwind(registers, qmp.read_memory, max_frames)
            trace["registers"] = registers
            trace["code_context"] = self.symbols.code_context(registers["pc"])
            trace["source_location"] = self.symbols.source_location(registers["pc"])
            raw = qmp.read_memory(0xE000ED28, 20)
            fault_registers = {name: int.from_bytes(raw[index * 4:index * 4 + 4], "little")
                               for index, name in enumerate(("cfsr", "hfsr", "dfsr", "mmfar", "bfar"))}
        return {"method": "qmp-halted-diagnosis", "state": state,
                "traceback": trace, "fault_registers": fault_registers,
                "screenshot": self.screenshot(path)}

    def diagnose(self, path: str | Path | None = None,
                 max_frames: int = 32,
                 inspect_u32: tuple[str, ...] | list[str] = (),
                 inspect_deref: tuple[str, ...] | list[str] = (),
                 inspect_bytes: tuple[str, ...] | list[str] = (),
                 inspect_values: tuple[str, ...] | list[str] = (),
                 inspect_rings: tuple[str, ...] | list[str] = (),
                 screenshot: dict | None = None) -> dict:
        """Capture the framebuffer and a symbol-resolved call stack together."""
        status = self.qmp("query-status").get("return", {})
        screenshot = screenshot or self.screenshot(path)
        trace = self.traceback(max_frames)
        registers = trace.get("registers", {})
        pc = registers.get("pc", 0)
        trace["code_context"] = self.symbols.code_context(pc)
        trace["source_location"] = self.symbols.source_location(pc)
        memory = {}
        for name in inspect_u32:
            address = self.at(name)
            value = self.u32(address)
            memory[name] = {"address": address, "address_hex": hex(address),
                            "value": value, "value_hex": hex(value)}
        for spec in inspect_deref:
            expression, separator, length_text = spec.partition(":")
            if not separator:
                raise ValueError(f"invalid --deref {spec!r}; expected SYMBOL[+OFFSET]:SIZE")
            symbol, plus, offset_text = expression.rpartition("+")
            if not plus:
                symbol, offset_text = expression, "0"
            try:
                offset, length = int(offset_text, 0), int(length_text, 0)
            except ValueError as exc:
                raise ValueError(f"invalid --deref {spec!r}; offsets and sizes use decimal or 0x notation") from exc
            if not symbol or offset < 0 or length <= 0:
                raise ValueError(f"invalid --deref {spec!r}; symbol, nonnegative offset, and positive size required")
            pointer = self.u32(self.at(symbol))
            address = pointer + offset
            data = self.read(address, length)
            key = f"*{symbol}+{offset:#x}:{length}"
            memory[key] = {"symbol": symbol, "pointer": pointer,
                           "pointer_hex": hex(pointer), "address": address,
                           "address_hex": hex(address), "size": length,
                           "bytes_hex": data.hex()}
        for spec in inspect_bytes:
            expression, separator, length_text = spec.partition(":")
            if not separator:
                raise ValueError(f"invalid --bytes {spec!r}; expected SYMBOL[+OFFSET]:SIZE")
            symbol, plus, offset_text = expression.rpartition("+")
            if not plus:
                symbol, offset_text = expression, "0"
            try:
                offset, length = int(offset_text, 0), int(length_text, 0)
            except ValueError as exc:
                raise ValueError(f"invalid --bytes {spec!r}; offsets and sizes use decimal or 0x notation") from exc
            if not symbol or offset < 0 or length <= 0:
                raise ValueError(f"invalid --bytes {spec!r}; symbol, nonnegative offset, and positive size required")
            address = self.at(symbol) + offset
            data = self.read(address, length)
            key = f"{symbol}+{offset:#x}:{length}"
            memory[key] = {"symbol": symbol, "address": address,
                           "address_hex": hex(address), "size": length,
                           "bytes_hex": data.hex()}
        for name in inspect_values:
            try:
                memory[name] = {"address": self.at(name),
                                "value": self.read_value(name),
                                "type": self.symbols.type_layout(name)}
            except (KeyError, ValueError) as exc:
                memory[name] = {"available": False, "reason": str(exc)}
        rings = {}
        for spec in inspect_rings:
            name, separator, head = spec.partition(":")
            if not separator or not name or not head:
                raise ValueError(f"invalid ring {spec!r}; expected ARRAY_SYMBOL:HEAD_SYMBOL")
            try:
                rings[spec] = self.read_ring(name, head)
            except (KeyError, ValueError) as exc:
                rings[spec] = {"available": False, "reason": str(exc)}
        frame_symbols = {frame.get("symbol", {}).get("name")
                         for frame in trace["frames"] if frame.get("symbol")}
        overlay_active = bool(frame_symbols & {
            "open_pause_menu", "odroid_overlay_game_menu", "odroid_overlay_dialog"})
        if screenshot["all_black"]:
            display_state = "all-black"
        elif overlay_active:
            display_state = "retro-go-overlay"
        else:
            display_state = "non-black"
        return {"status": status, "display_state": display_state,
                "screenshot": screenshot, "traceback": trace,
                "memory": memory, "rings": rings}

    def qmp(self, execute: str, arguments: dict | None = None) -> dict:
        """Run one structured QMP command against the attached GWemu."""
        if not self.qmp_socket:
            raise RuntimeError("pass --qmp-socket to enable GWemu QMP controls")
        if self.qmp_socket.startswith("gwprov://"):
            from .qmp import QMPConnection
            with QMPConnection(self.qmp_socket) as qmp:
                return qmp.execute(execute, arguments)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        try:
            sock.connect(self.qmp_socket)
            stream = sock.makefile("rwb", buffering=0)
            stream.readline()
            self._qmp_request(stream, {"execute": "qmp_capabilities"})
            request = {"execute": execute}
            if arguments:
                request["arguments"] = arguments
            return self._qmp_request(stream, request)
        finally:
            sock.close()

    def key_event(self, name: str, down: bool) -> dict:
        """Press/release a GWemu button explicitly for frame-driven schedules."""
        if not isinstance(down, bool):
            raise ValueError("down must be a boolean")
        qcodes = {"A": "x", "B": "z", "GAME": "g", "TIME": "t",
                  "PAUSE": "esc", "POWER": "p", "START": "ret",
                  "SELECT": "shift_r", "UP": "up", "DOWN": "down",
                  "LEFT": "left", "RIGHT": "right"}
        button = name.upper()
        if button not in qcodes:
            raise ValueError(f"unknown Game & Watch button {name!r}")
        return self.qmp("input-send-event", {"events": [{"type": "key",
            "data": {"down": down, "key": {"type": "qcode", "data": qcodes[button]}}}]})

    def key(self, name: str, hold_ms: int = 600) -> dict:
        """Send one Game & Watch button through GWemu's QMP keyboard map."""
        qcodes = {"A": "x", "B": "z", "GAME": "g", "TIME": "t",
                  "PAUSE": "esc", "POWER": "p", "START": "ret",
                  "SELECT": "shift_r", "UP": "up", "DOWN": "down",
                  "LEFT": "left", "RIGHT": "right"}
        button = name.upper()
        if button not in qcodes:
            raise ValueError(f"unknown Game & Watch button {name!r}")
        if hold_ms <= 0:
            raise ValueError("hold_ms must be positive")
        return self.qmp("send-key", {
            "keys": [{"type": "qcode", "data": qcodes[button]}],
            "hold-time": hold_ms,
        })

    @staticmethod
    def _qmp_request(stream, request: dict) -> dict:
        stream.write(json.dumps(request).encode() + b"\r\n")
        while True:
            reply = json.loads(stream.readline())
            if "event" not in reply:
                if "error" in reply:
                    raise RuntimeError(f"QMP {request['execute']} failed: {reply['error']}")
                return reply

    def __repr__(self):
        return (f"DebugSession(transport={self.transport!r}, "
                f"symbols={len(self.symbols._symbols)}, "
                f"sources={[str(p) for p in self.symbols.sources]!r}, "
                f"rebased={self.symbols._section_deltas!r})")


def python_shell(*, target: str, host: str = "127.0.0.1", port: int = 1234,
                 symbols: list[str] | None = None, openocd_port: int = 6666,
                 probe_ids: list[str] | None = None, programmers: list[str] | None = None,
                 remote_urls: list[str] | None = None, remote_origins: list[str] | None = None,
                 qmp_socket: str | None = None, debug_config: str | None = None) -> None:
    """Attach target sessions and expose them together in one Python REPL."""
    backends = {}
    sessions = {}
    if target == "gwemu":
        if probe_ids or programmers or remote_urls:
            raise ValueError("probe/programmer selections apply only to hardware targets")
        backends["gwemu"] = GwemuGDBBackend(host=host, port=port)
    elif target == "hardware":
        from .backends import (AutoOpenOCDBackend, SelectedOpenOCDBackend,
                               SelectedPyOCDBackend, WebSocketBackend)
        if len(programmers or []) != len(set(programmers or [])):
            raise ValueError("select each OpenOCD programmer type at most once; use --probe-id for probe IDs")
        for programmer in programmers or []:
            key = f"openocd:{programmer}"
            backends[key] = SelectedOpenOCDBackend(programmer, operation="gwprov debug session")
        for probe_id in probe_ids or []:
            key = f"probe:{probe_id}"
            backends[key] = SelectedPyOCDBackend(probe_id, operation="gwprov debug session")
        urls = remote_urls or []
        origins = remote_origins or []
        if origins and len(origins) != len(urls):
            raise ValueError("provide one --remote-origin for each --remote-url")
        for index, remote_url in enumerate(urls):
            key = f"remote:{index + 1}"
            origin = origins[index] if origins else None
            backends[key] = WebSocketBackend(remote_url, origin=origin,
                                             operation="gwprov remote debug session")
        if not backends:
            backends["hardware"] = AutoOpenOCDBackend(
                port=openocd_port, operation="gwprov debug session")
    else:
        raise ValueError(f"unknown target {target!r}")

    try:
        for key, backend in backends.items():
            backend.open()
            session = DebugSession(backend, target,
                                   qmp_socket=qmp_socket if target == "gwemu" else None)
            sessions[key] = session
            for elf in symbols or []:
                count = session.symbols.load(elf)
                print(f"[{key}] loaded {count} symbols: {Path(elf).expanduser()}")
            if debug_config:
                print(f"[{key}] loaded local debug config:", session.configure(debug_config))
        first_key = next(iter(sessions))
        session = sessions[first_key]
        backend = backends[first_key]
        print("Connected sessions:", {key: repr(value) for key, value in sessions.items()})
        print("Python debugger: dbg is the first session; sessions[key] accesses each target.")
        print("Use dbg.regs(), dbg.read(address, size), dbg.u32(address), dbg.halt(),")
        print("  dbg.resume(), dbg.step(), dbg.where(), dbg.traceback(), dbg.addr2line(address)")
        print("  dbg.bp(address), dbg.watchpoint(address, size=4, access='read')")
        print("  dbg.inject_return(return_at, value) or dbg.press_button(return_at, mask)")
        if target == "hardware":
            print("  dbg.arm_fault_breakpoints(), dbg.wait_fault() capture Cortex-M faults")
            print("  dbg.arm_watchdog_breakpoints(), dbg.wait_watchdog() capture WWDG pre-reset context")
        namespace = {"dbg": session, "sessions": sessions,
                     "backends": backends, "symbols": session.symbols}
        code.interact(banner="gwprov interactive debug (Ctrl-D disconnects)", local=namespace)
    finally:
        for backend in reversed(list(backends.values())):
            try:
                backend.close()
            except Exception:
                pass
