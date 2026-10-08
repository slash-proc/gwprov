"""Persistent structured Python debugger for GWemu and Game & Watch hardware."""

from __future__ import annotations

import code
import errno
import json
import shutil
import socket
import subprocess
import xml.etree.ElementTree as ET
from bisect import bisect_right
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection
from elftools.dwarf.callframe import FDE, RegisterRule
from gnwmanager.ocdbackend.gdb_backend import GDBBackend


class SymbolTable:
    """Resolve ELF symbols with the matching GNU ARM nm tool."""

    def __init__(self):
        self._symbols: dict[str, int] = {}
        self._symbol_owners: dict[str, Path] = {}
        self._symbol_sections: dict[str, str] = {}
        self._symbol_types: dict[str, str] = {}
        self._symbol_sizes: dict[str, int] = {}
        self._sections: dict[Path, dict[str, tuple[int, int]]] = {}
        self._section_deltas: dict[tuple[Path, str], int] = {}
        self._cfi: dict[Path, list[tuple[int, int, FDE]]] = {}
        self.sources: list[Path] = []

    def load(self, path: str | Path) -> int:
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
                raise ValueError(f"ELF symbol table not found: {elf}")
            section_names = {
                index: section.name for index, section in enumerate(image.iter_sections())
            }
            count = 0
            for symbol in symtab.iter_symbols():
                if not symbol.name:
                    continue
                name = symbol.name
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
        self.sources.append(elf)
        return count

    def __getitem__(self, name: str) -> int:
        address = self._symbols[name]
        owner = self._symbol_owners[name]
        section = self._symbol_sections.get(name)
        if section:
            address += self._section_deltas.get((owner, section), 0)
        return address

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

    def find(self, prefix: str) -> dict[str, int]:
        return {name: self[name] for name in self._symbols if prefix in name}

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

    def disassemble(self, symbol: str, elf: str | Path | None = None) -> str:
        """Disassemble one ELF symbol with arm-none-eabi-objdump or objdump."""
        if symbol not in self._symbols:
            raise KeyError(f"symbol {symbol!r} is not loaded")
        owner = (Path(elf).expanduser().resolve() if elf is not None
                 else self._symbol_owners[symbol])
        if self._symbol_owners[symbol] != owner:
            raise ValueError(f"symbol {symbol!r} does not belong to {owner}")
        tool = shutil.which("arm-none-eabi-objdump") or shutil.which("objdump")
        if not tool:
            raise FileNotFoundError("arm-none-eabi-objdump or objdump is required")
        result = subprocess.run(
            [tool, "-d", "-C", f"--disassemble={symbol}", str(owner)],
            check=True, capture_output=True, text=True)
        return result.stdout

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

    def open(self):
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
        self.symbols = SymbolTable()

    def halt(self):
        return self.backend.halt()

    def resume(self):
        return self.backend.resume()

    def reset(self, halt: bool = False):
        if halt:
            return self.backend.reset_and_halt()
        return self.backend.reset()

    def step(self):
        """Execute one instruction and leave the target halted."""
        self.backend.halt()
        if self.transport == "gwemu":
            reply = self.backend._send_command(b"s")
            self.backend._is_running = False
            return reply.decode("ascii", errors="replace")
        return self.backend("step", decode=False).decode("utf-8", errors="replace")

    def reg(self, name: str) -> int:
        return self.backend.read_register(name)

    def regs(self) -> dict[str, int]:
        was_running = self.backend._is_running
        if was_running:
            self.backend.halt()
        try:
            names = [*(f"r{i}" for i in range(13)), "sp", "lr", "pc", "xpsr"]
            return {name: self.reg(name) for name in names}
        finally:
            if was_running:
                self.backend.resume()

    def traceback(self, max_frames: int = 32) -> dict:
        """Return symbol-resolved ARM call frames from the current target state."""
        if max_frames < 1:
            raise ValueError("max_frames must be positive")
        was_running = self.backend._is_running
        if was_running:
            self.backend.halt()
        try:
            if self.transport == "gwemu" and hasattr(self.backend, "read_core_registers"):
                registers = self.backend.read_core_registers()
            else:
                registers = {f"r{i}": self.reg(f"r{i}") for i in range(13)}
                registers.update({name: self.reg(name) for name in ("sp", "lr", "pc")})
            result = self.symbols.unwind(registers, self.read, max_frames)
            result["registers"] = registers
            return result
        finally:
            if was_running:
                self.backend.resume()

    def where(self) -> dict:
        was_running = self.backend._is_running
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

    def u32(self, address: int) -> int:
        return int.from_bytes(self.read(address, 4), "little")

    def write(self, address: int, data: bytes):
        return self.backend.write_memory(address, data)

    def write_u32(self, address: int, value: int):
        return self.write(address, value.to_bytes(4, "little"))

    def bp(self, address: int):
        """Set a hardware breakpoint, suitable for flash code addresses."""
        address &= ~1  # RSP breakpoints use the instruction address, not Thumb's ISA bit.
        if self.transport == "gwemu":
            return self.backend._send_command(f"Z1,{address:x},2".encode()).decode()
        return self.backend(f"bp 0x{address:08x} 2 hw", decode=False).decode().strip()

    def clear_bp(self, address: int):
        address &= ~1
        if self.transport == "gwemu":
            return self.backend._send_command(f"z1,{address:x},2".encode()).decode()
        return self.backend(f"rbp 0x{address:08x}", decode=False).decode().strip()

    def at(self, symbol: str) -> int:
        return self.symbols[symbol]

    def nm(self, query: str = "", elf: str | Path | None = None) -> list[dict]:
        """Return structured nm-style symbol rows from loaded ELFs."""
        return self.symbols.nm(query, elf)

    def disasm(self, symbol: str, elf: str | Path | None = None) -> str:
        """Return objdump disassembly for a loaded function symbol."""
        return self.symbols.disassemble(symbol, elf)

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

    def diagnose(self, path: str | Path | None = None,
                 max_frames: int = 32,
                 inspect_u32: tuple[str, ...] | list[str] = (),
                 inspect_deref: tuple[str, ...] | list[str] = ()) -> dict:
        """Capture the framebuffer and a symbol-resolved call stack together."""
        status = self.qmp("query-status").get("return", {})
        screenshot = self.screenshot(path)
        trace = self.traceback(max_frames)
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
                "memory": memory}

    def qmp(self, execute: str, arguments: dict | None = None) -> dict:
        """Run one structured QMP command against the attached GWemu."""
        if not self.qmp_socket:
            raise RuntimeError("pass --qmp-socket to enable GWemu QMP controls")
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
                 qmp_socket: str | None = None) -> None:
    """Attach once, then keep a Python REPL and target connection alive."""
    from gnwmanager.ocdbackend.openocd_backend import OpenOCDBackend

    if target == "gwemu":
        backend = GwemuGDBBackend(host=host, port=port)
    elif target == "hardware":
        backend = OpenOCDBackend(port=openocd_port)
    else:
        raise ValueError(f"unknown target {target!r}")
    session = DebugSession(backend, target, qmp_socket=qmp_socket)
    try:
        backend.open()
        for elf in symbols or []:
            count = session.symbols.load(elf)
            print(f"Loaded {count} symbols: {Path(elf).expanduser()}")
        print("Connected:", session)
        print("Python debugger: dbg.regs(), dbg.read(address, size), dbg.u32(address),")
        print("  dbg.halt(), dbg.resume(), dbg.step(), dbg.where(), dbg.traceback()")
        print("  dbg.screenshot([path]), dbg.diagnose([path])")
        print("  dbg.bp(address), dbg.at('symbol'), dbg.rebase_from_pointer(section, symbol)")
        print("  dbg.symbols.find('prefix') lists matching symbols")
        print("  dbg.nm('pattern') returns structured symbols; dbg.disasm('function')")
        code.interact(banner="gwprov interactive debug (Ctrl-D disconnects)",
                      local={"dbg": session, "symbols": session.symbols, "backend": backend})
    finally:
        backend.close()
