"""Find, launch, and gracefully stop local GWemu instances."""

from __future__ import annotations

import errno
import json
import socket
import struct
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any

import psutil



def _process_scan_restriction() -> str | None:
    """Describe Linux sandbox controls that make an empty process scan inconclusive."""
    if not Path("/proc/self/status").is_file():
        return None
    fields = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key] = value.strip()
    try:
        seccomp = int(fields.get("Seccomp", "0"))
    except ValueError:
        seccomp = 0
    restricted = fields.get("NoNewPrivs") == "1" or seccomp != 0
    if not restricted:
        return None
    details = []
    if seccomp:
        details.append(f"seccomp mode {seccomp}")
    if fields.get("NoNewPrivs") == "1":
        details.append("NoNewPrivs=1")
    return ", ".join(details)


def _arguments(pid: int) -> list[str]:
    try:
        return psutil.Process(pid).cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []


def _option(args: list[str], name: str) -> str | None:
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return None


def _instance(pid: int, args: list[str]) -> dict[str, Any] | None:
    joined = " ".join(args)
    executable = Path(args[0]).name.lower() if args else ""
    is_emulator = executable == "gwemu" or executable == "gwemu.exe" \
        or executable.startswith("qemu-system")
    if "gnw-h7b0" not in joined or not is_emulator:
        return None
    bank1 = next((arg.split("=", 1)[1] for arg in args
                  if "gnw-h7b0-soc.bank1-image=" in arg), None)
    profile = str(Path(bank1).resolve().parent) if bank1 else None
    qmp_arg = _option(args, "-qmp")
    qmp = None
    if qmp_arg and qmp_arg.startswith("unix:"):
        qmp = qmp_arg[5:].split(",", 1)[0]
    gdb_arg = _option(args, "-gdb")
    gdb_port = None
    if gdb_arg and gdb_arg.startswith("tcp:"):
        try:
            gdb_port = int(gdb_arg.rsplit(":", 1)[1])
        except ValueError:
            pass
    process = psutil.Process(pid)
    try:
        created = process.create_time()
    except psutil.Error:
        created = None
    execution = _qmp_execution(qmp)
    running = execution.get("running")
    return {
        "pid": pid,
        "profile": profile,
        "status": "running" if running is True else "halted" if running is False else "unknown",
        "running": running,
        "halted": not running if isinstance(running, bool) else None,
        "qmpStatus": execution.get("status", "unknown"),
        **({"stateDetail": execution["detail"]} if "detail" in execution else {}),
        "display": "visible" if _option(args, "-display") == "gwemu" else "headless",
        "gdbPort": gdb_port,
        "qmpSocket": qmp,
        "qmpHandle": qmp,
        "qmpTransport": "unix" if qmp else "unavailable",
        "created": created,
    }


def _qmp_execution(path: str | None) -> dict:
    """Read actual CPU execution state; a debugger connection is not a state."""
    if not path:
        return {"status": "unavailable", "running": None,
                "detail": "QMP endpoint is missing; execution state cannot be verified"}
    if path.startswith("gwprov://"):
        from .qmp import QMPConnection
        try:
            with QMPConnection(path, timeout=0.5) as qmp:
                state = qmp.execute("query-status")["return"]
            if not isinstance(state.get("running"), bool):
                raise RuntimeError("query-status omitted the boolean running field")
            return state
        except (OSError, RuntimeError, ValueError) as exc:
            return {"status": "unavailable", "running": None, "detail": str(exc)}
    try:
        endpoint_exists = Path(path).exists()
    except PermissionError:
        raise
    except OSError as exc:
        return {"status": "unavailable", "running": None,
                "detail": f"QMP endpoint cannot be checked: {exc}"}
    if not endpoint_exists:
        return {"status": "unavailable", "running": None,
                "detail": "QMP endpoint is missing; execution state cannot be verified"}
    from .qmp import QMPConnection
    try:
        with QMPConnection(path, timeout=0.5) as qmp:
            state = qmp.execute("query-status")["return"]
        if not isinstance(state.get("running"), bool):
            raise RuntimeError("query-status omitted the boolean running field")
        return state
    except PermissionError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        return {"status": "unavailable", "running": None, "detail": str(exc)}


def _qmp_status(path: str | None) -> str:
    return str(_qmp_execution(path).get("status", "unavailable"))


def _qmp_execute(path: str, command: str, arguments: dict[str, Any] | None = None) -> dict:
    from .qmp import QMPConnection
    with QMPConnection(path) as qmp:
        return qmp.execute(command, arguments)


def _read_ppm(ppm: bytes) -> tuple[int, int, bytes]:
    """Parse QEMU's binary P6 screendump and return normalized RGB pixels."""
    offset = 0

    def token() -> bytes:
        nonlocal offset
        while offset < len(ppm):
            if ppm[offset] in b" \t\r\n":
                offset += 1
            elif ppm[offset] == ord("#"):
                newline = ppm.find(b"\n", offset)
                if newline < 0:
                    raise ValueError("invalid PPM header comment")
                offset = newline + 1
            else:
                break
        start = offset
        while offset < len(ppm) and ppm[offset] not in b" \t\r\n#":
            offset += 1
        value = ppm[start:offset]
        if not value:
            raise ValueError("incomplete PPM header")
        if offset >= len(ppm):
            raise ValueError("PPM header has no pixel-data delimiter")
        if ppm[offset:offset + 2] == b"\r\n":
            offset += 2
        else:
            offset += 1
        return value

    if token() != b"P6":
        raise ValueError("GWemu screenshot is not a binary P6 PPM")
    width, height, maximum = (int(token()) for _ in range(3))
    if width <= 0 or height <= 0 or not 1 <= maximum <= 65535:
        raise ValueError("invalid PPM dimensions or maximum channel value")
    sample_bytes = 1 if maximum < 256 else 2
    expected = width * height * 3 * sample_bytes
    pixels = ppm[offset:]
    if len(pixels) != expected:
        raise ValueError(f"PPM pixel data has {len(pixels)} bytes; expected {expected}")
    if sample_bytes == 2 or maximum != 255:
        converted = bytearray(width * height * 3)
        if sample_bytes == 1:
            for index, value in enumerate(pixels):
                converted[index] = (value * 255 + maximum // 2) // maximum
        else:
            for index in range(width * height * 3):
                value = int.from_bytes(pixels[index * 2:index * 2 + 2], "big")
                converted[index] = (value * 255 + maximum // 2) // maximum
        pixels = bytes(converted)

    return width, height, pixels


def _rgb_to_png(width: int, height: int, pixels: bytes) -> bytes:
    scanlines = b"".join(
        b"\x00" + pixels[y * width * 3:(y + 1) * width * 3]
        for y in range(height)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(scanlines))
            + chunk(b"IEND", b""))


def screenshot_qmp(qmp_socket: str, output: str | Path | None = None) -> dict[str, Any]:
    """Capture GWemu as PNG and report exact black-screen pixel statistics."""
    if qmp_socket.startswith("gwprov://"):
        from .daemon_ipc import runtime_directory
        qmp_path = runtime_directory() / f"gwemu-{qmp_socket.removeprefix('gwprov://')}"
    else:
        qmp_path = Path(qmp_socket).expanduser().resolve()
    if output is None:
        directory = qmp_path.parent / "screenshots"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        output_path = directory / f"gwemu-{stamp}-{time.time_ns() % 1_000_000_000:09d}.png"
    else:
        output_path = Path(output).expanduser().resolve()
        if not output_path.suffix:
            output_path = output_path.with_suffix(".png")
        if output_path.suffix.lower() != ".png":
            raise ValueError("GWemu screenshots are saved as PNG; output must use a .png extension")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
                prefix=".gwprov-screenshot-", suffix=".ppm",
                dir=output_path.parent, delete=False) as temporary:
            temp_path = Path(temporary.name)
        _qmp_execute(str(qmp_path), "screendump", {"filename": str(temp_path)})
        width, height, pixels = _read_ppm(temp_path.read_bytes())
        output_path.write_bytes(_rgb_to_png(width, height, pixels))
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    non_black = sum(
        1 for index in range(0, len(pixels), 3)
        if pixels[index] or pixels[index + 1] or pixels[index + 2]
    )
    return {
        "path": str(output_path),
        "width": width,
        "height": height,
        "all_black": non_black == 0,
        "non_black_pixels": non_black,
    }


def screenshot_instance(*, pid: int | None = None, profile: str | None = None,
                        output: str | Path | None = None) -> int:
    if (pid is None) == (profile is None):
        raise ValueError("select exactly one instance with --pid or --profile")
    rows = instances()
    if profile:
        from .profiles import resolve_profile_path
        root = str(resolve_profile_path(profile))
        rows = [row for row in rows if row["profile"] == root]
    else:
        rows = [row for row in rows if row["pid"] == pid]
    if not rows:
        raise ValueError("no matching GWemu instance is running")
    if len(rows) != 1:
        raise ValueError("profile matches multiple GWemu instances; select one by --pid")
    qmp_path = rows[0]["qmpSocket"]
    if not qmp_path:
        raise ValueError("instance has no QMP control channel; it cannot be screenshotted by gwprov")
    result = screenshot_qmp(qmp_path, output)
    print(f"Saved PNG screenshot: {result['path']}")
    print(f"Frame {result['width']}x{result['height']}; all black: "
          f"{'yes' if result['all_black'] else 'no'}; "
          f"nonblack pixels: {result['non_black_pixels']}")
    return 0


def diagnose_instance(profile: str, *, symbols: list[str] | None = None,
                      output: str | Path | None = None,
                      max_frames: int = 32,
                      inspect_u32: list[str] | None = None,
                      inspect_deref: list[str] | None = None,
                      inspect_bytes: list[str] | None = None,
                      inspect_values: list[str] | None = None,
                      inspect_rings: list[str] | None = None,
                      debug_config: str | None = None) -> int:
    """Capture one running profile's screen and symbol-resolved ARM stack."""
    from .debug_shell import DebugSession, GwemuGDBBackend
    from .profiles import DeviceProfile

    device = DeviceProfile.load(profile)
    root = str(device.root)
    matches = [row for row in instances() if row["profile"] == root]
    if len(matches) != 1:
        if not matches:
            raise ValueError(f"no GWemu instance is running for profile {root}")
        raise ValueError(f"profile has {len(matches)} GWemu instances; select one before diagnosing")
    row = matches[0]
    if not row["gdbPort"]:
        raise ValueError("GWemu instance has no GDB endpoint")
    if not row["qmpSocket"]:
        raise ValueError("GWemu instance has no QMP control channel for framebuffer capture")

    firmware = device.root / "debug" / "retro-go-debug.elf"
    if not firmware.is_file():
        raise ValueError(f"profile has no bundled firmware symbols: {firmware}")
    app_symbols = sorted((device.root / "debug" / "apps").rglob("*.elf"))
    symbol_paths = list(dict.fromkeys([
        firmware, *app_symbols,
        *(Path(path).expanduser().resolve() for path in symbols or []),
    ]))
    for symbol_path in symbol_paths:
        if not symbol_path.is_file():
            raise FileNotFoundError(symbol_path)

    before = _qmp_execute(row["qmpSocket"], "query-status").get("return", {})
    was_running = bool(before.get("running"))
    backend = GwemuGDBBackend(host="127.0.0.1", port=row["gdbPort"])
    try:
        backend.open()  # GDBBackend resumes QEMU after its attach halt.
        if not was_running:
            backend.halt()
        session = DebugSession(backend, "gwemu", qmp_socket=row["qmpSocket"])
        for symbol_path in symbol_paths:
            session.symbols.load(symbol_path)
        if debug_config:
            session.configure(debug_config)
        report = session.diagnose(output, max_frames=max_frames,
                                  inspect_u32=inspect_u32 or [],
                                  inspect_deref=inspect_deref or [],
                                  inspect_bytes=inspect_bytes or [],
                                  inspect_values=inspect_values or [],
                                  inspect_rings=inspect_rings or [])
        report.update({"pid": row["pid"], "profile": root,
                       "symbols": [str(path) for path in symbol_paths]})
        print(json.dumps(report, indent=2))
        return 0
    finally:
        # Preserve whether the user had GWemu running or intentionally paused.
        if getattr(backend, "_socket", None) is not None:
            if was_running and not backend._is_running:
                backend.resume()
            elif not was_running and backend._is_running:
                backend.halt()
            backend.close()



def watch_instance(profile: str, *, symbols: list[str] | None = None,
                   progress_symbols: list[str] | None = None,
                   guest_pc_symbols: list[str] | None = None,
                   rebase_symbols: list[str] | None = None,
                   watch_u32: list[str] | None = None,
                   watch_deref: list[str] | None = None,
                   watch_bytes: list[str] | None = None,
                   watch_values: list[str] | None = None,
                   watch_rings: list[str] | None = None,
                   heartbeat_symbol: str | None = None,
                   frame_symbol: str | None = None,
                   guest_pc_symbol: str | None = None,
                   interval: float = 0.5, stall_after: float = 3.0,
                   duration: float = 30.0,
                   output: str | Path | None = None, debug_config: str | None = None) -> int:
    """Watch a target and save a generic, symbolized triage bundle on suspicion."""
    from datetime import datetime, timezone
    from .debug_shell import DebugSession, GwemuGDBBackend
    from .profiles import DeviceProfile

    if interval <= 0 or stall_after <= 0 or duration <= 0:
        raise ValueError("interval, stall-after, and duration must be positive")
    device = DeviceProfile.load(profile)
    root = str(device.root)
    matches = [row for row in instances() if row["profile"] == root]
    if len(matches) != 1:
        raise ValueError("watch requires exactly one visible GWemu instance for profile; "
                         "ensure gwprov has process visibility and the instance has GDB/QMP")
    row = matches[0]
    if not row["gdbPort"] or not row["qmpSocket"]:
        raise ValueError("watch requires GDB and QMP endpoints; start with gwprov gwemu start")
    firmware = device.root / "debug" / "retro-go-debug.elf"
    if not firmware.is_file():
        raise ValueError(f"profile has no bundled firmware symbols: {firmware}")
    app_symbols = sorted((device.root / "debug" / "apps").rglob("*.elf"))
    symbol_paths = list(dict.fromkeys([
        firmware, *app_symbols,
        *(Path(path).expanduser().resolve() for path in symbols or []),
    ]))
    for path in symbol_paths:
        if not path.is_file():
            raise FileNotFoundError(path)
    before = _qmp_execute(row["qmpSocket"], "query-status").get("return", {})
    was_running = bool(before.get("running"))
    backend = GwemuGDBBackend(host="127.0.0.1", port=row["gdbPort"])
    started = time.monotonic()
    # Retain the old named options as aliases, while allowing any project to
    # describe its own progress and guest-PC probes. Probe discovery is only a
    # convenience; the report records exactly what was selected.
    progress_names = list(progress_symbols or [])
    for name in (heartbeat_symbol, frame_symbol):
        if name and name not in progress_names:
            progress_names.append(name)
    guest_names = list(guest_pc_symbols or [])
    if guest_pc_symbol and guest_pc_symbol not in guest_names:
        guest_names.append(guest_pc_symbol)
    watched_names = list(dict.fromkeys([*progress_names, *guest_names,
                                        *(watch_u32 or [])]))
    deref_specs = list(watch_deref or [])
    byte_specs = list(watch_bytes or [])
    rebase_specs: list[tuple[str, str]] = []
    for spec in rebase_symbols or []:
        section, separator, pointer_symbol = spec.partition("=")
        if not separator or not section or not pointer_symbol:
            raise ValueError(f"invalid --rebase {spec!r}; expected SECTION=POINTER_SYMBOL")
        rebase_specs.append((section, pointer_symbol))
    value_history: dict[str, int] = {}
    value_stable_since: dict[str, float] = {}
    last_progress = started
    samples = []
    previous_cpu_signature = None
    cpu_stable_since = started
    last_guest_screen_probe = 0.0
    report_path = (Path(output).expanduser().resolve() if output else
                   device.root / "runtime" / "gwprov" / "triage" /
                   f"watch-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
    image_path = report_path.with_suffix(".png")
    try:
        backend.open()
        if not was_running:
            backend.halt()
        session = DebugSession(backend, "gwemu", qmp_socket=row["qmpSocket"])
        for path in symbol_paths:
            session.symbols.load(path)
        if debug_config:
            settings = session.symbols.load_config(debug_config)
            if not progress_names:
                progress_names = list(settings["progress_symbols"])
            for spec in settings["rebase_symbols"]:
                section, pointer = spec.split("=", 1)
                if (section, pointer) not in rebase_specs:
                    rebase_specs.append((section, pointer))
        # Discover common instrumentation names across cores and homebrews.
        # Explicit options always win and projects need not follow this naming.
        symbols_found = session.nm()
        if not progress_names:
            for item in symbols_found:
                name = item["name"].lower()
                if item["size"] == 4 and item["type"] == "STT_OBJECT" and any(
                        token in name for token in
                        ("heartbeat", "frame_count", "frame_counter", "progress_count")):
                    progress_names.append(item["name"])
        if not guest_names:
            for item in symbols_found:
                name = item["name"].lower()
                if item["size"] == 4 and item["type"] == "STT_OBJECT" and (
                        name.endswith(("_cur_pc", "_current_pc", "_resume_pc", "_guest_pc"))):
                    guest_names.append(item["name"])
        watched_names = list(dict.fromkeys([*progress_names, *guest_names,
                                            *(watch_u32 or [])]))
        for section, pointer_symbol in rebase_specs:
            try:
                session.at(pointer_symbol)
                session.symbols.owner(pointer_symbol)
            except KeyError as exc:
                raise ValueError(f"rebase pointer symbol {pointer_symbol!r} is not loaded") from exc
            if not any(section in session.symbols.sections(path) for path in session.symbols.sources):
                raise ValueError(
                    f"ELF section {section!r} is not present in the ELF containing "
                    f"pointer symbol {pointer_symbol!r}")
        fault_ranges = []
        for item in symbols_found:
            name = item["name"]
            lowered = name.lower()
            if item["size"] and any(token in lowered for token in
                                     ("hardfault", "busfault", "usagefault",
                                      "memmanagefault", "fault_handler", "error_handler",
                                      "panic", "assert_fail", "abort")):
                fault_ranges.append((name, item["address"] & ~1,
                                     (item["address"] & ~1) + item["size"]))
        saw_progress = False
        while time.monotonic() - started < duration:
            status = _qmp_execute(row["qmpSocket"], "query-status").get("return", {})
            now = time.monotonic()
            if not status.get("running"):
                samples.append({"elapsed_seconds": round(now - started, 3),
                                "state": status.get("status", "paused")})
                previous_cpu_signature = None
                value_history.clear()
                value_stable_since.clear()
                last_progress = now
                time.sleep(interval)
                continue
            for section, pointer_symbol in rebase_specs:
                try:
                    session.rebase_from_pointer(section, pointer_symbol)
                except (KeyError, RuntimeError):
                    pass
            addresses = {name: session.at(name) for name in watched_names}
            values = {name: session.u32(address) for name, address in addresses.items()}
            if hasattr(backend, "read_core_registers"):
                regs = backend.read_core_registers()
            else:
                regs = {name: session.reg(name) for name in ("pc", "sp", "lr")}
            pc = regs["pc"] & ~1
            signature = (pc, regs.get("sp"), regs.get("lr"))
            if signature != previous_cpu_signature:
                cpu_stable_since = now
            changed_progress = False
            for name in watched_names:
                value = values[name]
                if name not in value_history:
                    value_stable_since[name] = now
                elif value != value_history[name]:
                    value_stable_since[name] = now
                    if name in progress_names:
                        changed_progress = True
                value_history[name] = value
            if changed_progress:
                saw_progress = True
                last_progress = now
            fault_name = next((name for name, start, end in fault_ranges
                               if start <= pc < end), None)
            guest_stagnant = next((name for name in guest_names
                                   if now - value_stable_since.get(name, now) >= stall_after), None)
            cpu_stagnant = (previous_cpu_signature == signature and
                            now - cpu_stable_since >= stall_after and
                            (not progress_names or now - last_progress >= stall_after))
            guest_stagnant_trigger = False
            screen_probe = None
            if guest_stagnant and now - last_guest_screen_probe >= min(stall_after, 2.0):
                last_guest_screen_probe = now
                screen_probe = session.screenshot(image_path)
                guest_stagnant_trigger = (
                    screen_probe["all_black"] or not progress_names or
                    now - last_progress >= stall_after)
                if not guest_stagnant_trigger:
                    image_path.unlink(missing_ok=True)
                    screen_probe = None
            samples.append({"elapsed_seconds": round(now - started, 3),
                            "values": values,
                            "pc": pc, "pc_hex": hex(pc), "fault_handler": fault_name,
                            "sp": regs.get("sp"), "lr": regs.get("lr"),
                            "guest_pc_stagnant": guest_stagnant,
                            "cpu_location_stagnant": cpu_stagnant,
                            "state": status.get("status", "running")})
            previous_cpu_signature = signature
            if fault_name or guest_stagnant_trigger or cpu_stagnant or (
                    progress_names and now - last_progress >= stall_after):
                diagnosis = session.diagnose(image_path, inspect_u32=watched_names,
                                             inspect_deref=deref_specs,
                                             inspect_bytes=byte_specs,
                                             inspect_values=watch_values or [],
                                             inspect_rings=watch_rings or [],
                                             screenshot=screen_probe)
                screenshot = diagnosis.get("screenshot", {})
                traceback_frames = diagnosis.get("traceback", {}).get("frames", [])
                traceback_symbols = {
                    frame.get("symbol", {}).get("name", "")
                    for frame in traceback_frames if frame.get("symbol")
                }
                fatal_symbols = sorted(name for name in traceback_symbols
                                       if name.lower() in {
                                           "abort", "bsod", "hardfault_handler",
                                           "busfault_handler", "usagefault_handler",
                                           "memmanage_handler", "error_handler",
                                           "common_fault_handler_c", "panic",
                                       })
                if fault_name:
                    classification = "fault-handler"
                    reason = f"CPU PC is inside {fault_name}"
                elif fatal_symbols:
                    classification = "firmware-fatal-path"
                    reason = "traceback passes through " + ", ".join(fatal_symbols)
                elif guest_stagnant_trigger:
                    classification = "suspected-guest-loop"
                    reason = f"{guest_stagnant} remained unchanged for {stall_after:g} seconds"
                elif progress_names and now - last_progress >= stall_after:
                    classification = "suspected-progress-stall"
                    reason = f"none of the selected progress symbols changed for {stall_after:g} seconds"
                else:
                    classification = "suspected-cpu-loop"
                    reason = f"ARM PC, SP, and LR stayed unchanged for {stall_after:g} seconds"
                context = session.where()
                if screenshot.get("all_black") and classification.startswith("suspected-"):
                    classification += "-black-display"
                report = {
                    "classification": classification,
                    "reason": reason,
                    "confidence": "direct" if fault_name or fatal_symbols else "heuristic",
                    "observed_at_utc": datetime.now(timezone.utc).isoformat(),
                    "profile": root, "pid": row["pid"],
                    "symbols": [str(path) for path in symbol_paths],
                    "progress_symbols": progress_names,
                    "guest_pc_symbols": guest_names,
                    "rebased_sections": [
                        {"section": section, "pointer_symbol": pointer}
                        for section, pointer in rebase_specs],
                    "watch_u32": watch_u32 or [],
                    "watch_deref": deref_specs,
                    "watch_bytes": byte_specs,
                    "runtime_error_symbols": fatal_symbols,
                    "last_values": values,
                    "stable_seconds": round(now - (value_stable_since.get(guest_stagnant, cpu_stable_since)
                                                    if guest_stagnant_trigger else cpu_stable_since), 3),
                    "cpu_location": {"pc": pc, "sp": regs.get("sp"), "lr": regs.get("lr"),
                                     "symbolized": context},
                    "display": screenshot,
                    "screenshot": str(image_path), "diagnosis": diagnosis,
                    "samples": samples,
                }
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(json.dumps(report, indent=2) + "\n")
                print(json.dumps({"classification": classification,
                                  "reason": reason,
                                  "confidence": report["confidence"],
                                  "values": values,
                                  "display": screenshot,
                                  "report": str(report_path),
                                  "screenshot": str(image_path),
                                  "cpu_location": context,
                                  "traceback": diagnosis["traceback"]}, indent=2))
                return 1
            time.sleep(interval)
        classification = "progressing" if saw_progress else "no-progress-symbols-detected"
        report = {
            "classification": classification,
            "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "profile": root, "pid": row["pid"],
            "symbols": [str(path) for path in symbol_paths],
            "progress_symbols": progress_names,
            "guest_pc_symbols": guest_names,
            "rebased_sections": [
                {"section": section, "pointer_symbol": pointer}
                for section, pointer in rebase_specs],
            "samples": samples,
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"classification": report["classification"],
                          "report": str(report_path),
                          "samples": len(samples)}, indent=2))
        return 0 if saw_progress else 2
    finally:
        if getattr(backend, "_socket", None) is not None:
            if was_running and not backend._is_running:
                backend.resume()
            elif not was_running and backend._is_running:
                backend.halt()
            backend.close()

def instances() -> list[dict[str, Any]]:
    from .daemon import managed_instances
    result = managed_instances()
    managed_pids = {row["pid"] for row in result}
    for proc in psutil.process_iter(["pid", "cmdline"]):
        pid = proc.info.get("pid")
        args = proc.info.get("cmdline") or []
        if pid and pid not in managed_pids:
            found = _instance(pid, args)
            if found:
                result.append(found)
    return sorted(result, key=lambda row: row["pid"])


def _qmp_read_memory(path: str, address: int, size: int) -> bytes:
    """Read memory through QMP without taking the GDB endpoint."""
    if size <= 0:
        return b""
    from .qmp import QMPConnection
    with QMPConnection(path) as qmp:
        return qmp.read_memory(address, size)


def _dwarf_struct_members(elf_path: Path, variable_name: str) -> dict[str, int]:
    """Return member byte offsets for a global struct, using the ELF's DWARF types."""
    from elftools.elf.elffile import ELFFile

    def attr_name(die, key):
        attr = die.attributes.get(key)
        return attr.value.decode(errors="replace") if attr and isinstance(attr.value, bytes) else None

    def referenced_type(cu, die):
        attr = die.attributes.get("DW_AT_type")
        if not attr:
            return None
        offset = attr.value if attr.form == "DW_FORM_ref_addr" else cu.cu_offset + attr.value
        target = cu.get_DIE_from_refaddr(offset)
        while target and target.tag in {
            "DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type",
            "DW_TAG_restrict_type", "DW_TAG_atomic_type",
        }:
            attr = target.attributes.get("DW_AT_type")
            if not attr:
                return None
            offset = attr.value if attr.form == "DW_FORM_ref_addr" else cu.cu_offset + attr.value
            target = cu.get_DIE_from_refaddr(offset)
        return target

    with elf_path.open("rb") as stream:
        dwarf = ELFFile(stream).get_dwarf_info()
        for cu in dwarf.iter_CUs():
            for die in cu.iter_DIEs():
                if die.tag != "DW_TAG_variable" or attr_name(die, "DW_AT_name") != variable_name:
                    continue
                struct = referenced_type(cu, die)
                if not struct or struct.tag != "DW_TAG_structure_type":
                    continue
                members = {}
                for member in struct.iter_children():
                    if member.tag != "DW_TAG_member":
                        continue
                    name = attr_name(member, "DW_AT_name")
                    location = member.attributes.get("DW_AT_data_member_location")
                    if name and location and isinstance(location.value, int):
                        members[name] = location.value
                return members
    return {}


def _dwarf_typedef_members(elf_path: Path, typedef_name: str) -> dict[str, int]:
    """Return the first complete struct layout for a named typedef in an ELF."""
    from elftools.elf.elffile import ELFFile

    with elf_path.open("rb") as stream:
        dwarf = ELFFile(stream).get_dwarf_info()
        for cu in dwarf.iter_CUs():
            for die in cu.iter_DIEs():
                name = die.attributes.get("DW_AT_name")
                if die.tag != "DW_TAG_typedef" or not name or name.value != typedef_name.encode():
                    continue
                attr = die.attributes.get("DW_AT_type")
                if not attr:
                    continue
                offset = attr.value if attr.form == "DW_FORM_ref_addr" else cu.cu_offset + attr.value
                target = cu.get_DIE_from_refaddr(offset)
                while target and target.tag in {
                    "DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type",
                    "DW_TAG_restrict_type", "DW_TAG_atomic_type",
                }:
                    attr = target.attributes.get("DW_AT_type")
                    if not attr:
                        break
                    offset = attr.value if attr.form == "DW_FORM_ref_addr" else cu.cu_offset + attr.value
                    target = cu.get_DIE_from_refaddr(offset)
                if not target or target.tag != "DW_TAG_structure_type":
                    continue
                members = {}
                for member in target.iter_children():
                    if member.tag != "DW_TAG_member":
                        continue
                    mname = member.attributes.get("DW_AT_name")
                    location = member.attributes.get("DW_AT_data_member_location")
                    if mname and location and isinstance(location.value, int):
                        members[mname.value.decode(errors="replace")] = location.value
                if members:
                    return members
    return {}


def _qmp_registers(path: str) -> dict[str, int]:
    from .qmp import QMPConnection
    with QMPConnection(path) as qmp:
        return qmp.registers()


def _application_state_from_target(symbol_table, firmware: Path, app_elfs: list[Path],
                                   read_memory, registers: dict[str, int]) -> str:
    """Classify a paused target from shared Retro-Go and project ELF symbols."""
    pc = registers["pc"]
    frames = []
    trace = {"frames": []}
    try:
        trace = symbol_table.unwind(registers, lambda addr, size: read_memory(addr, size), 16)
        for frame in trace.get("frames", []):
            symbol = frame.get("symbol")
            if symbol:
                frames.append(symbol.get("name", ""))
    except (OSError, RuntimeError, ValueError):
        pass
    current = symbol_table.nearest(pc)
    current_name = current.get("name", "") if current else ""
    active_names = set(frames) | {current_name}
    lowered_names = {name.casefold() for name in active_names}

    fatal_names = sorted(name for name in active_names if name.casefold() in {
        "bsod", "abort", "hardfault_handler", "busfault_handler",
        "usagefault_handler", "memmanage_handler", "common_fault_handler_c",
        "error_handler", "panic",
    })
    if fatal_names:
        return "Fault: " + ", ".join(fatal_names)

    if "handle_time_menu" in active_names:
        return "Time settings menu"
    if "odroid_overlay_game_menu" in active_names:
        return "Game menu"
    if "odroid_overlay_game_settings_menu" in active_names:
        return "Pause/settings menu"
    if "odroid_overlay_settings_menu" in active_names:
        if any("clock" in name or "time" in name for name in lowered_names):
            return "Time settings menu"
        return "Settings menu"
    if "odroid_overlay_dialog" in active_names:
        if any(name.startswith(("retro_loop", "gui_")) for name in active_names):
            return "Overlay open in picker"
        return "Overlay open in game"

    # App projects may export a writable NUL-terminated char array with this
    # name. It is read directly from the app ELF, so custom state text needs no
    # per-project code in gwprov and does not depend on relocated pointers.
    for elf in reversed(app_elfs):
        state_symbol = next((item for item in symbol_table.nm("gwprov_application_state", elf)
                             if item["name"] == "gwprov_application_state"), None)
        if state_symbol and state_symbol["size"] >= 2:
            # The state array lives in data/BSS, not executable code. Prove
            # the app is active from the current PC or its caller frames.
            app_active = (current and current.get("elf") == str(elf)) or any(
                (frame.get("symbol") or {}).get("elf") == str(elf)
                for frame in trace.get("frames", []))
            if app_active:
                raw = read_memory(state_symbol["address"], min(state_symbol["size"], 64))
                custom = raw.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()
                if custom and all(char.isprintable() for char in custom):
                    return custom

    # Retro-Go's GUI is a runtime tab registry. DWARF keeps this compatible
    # with firmware releases that change the layout of gui and tab_t.
    gui_address = next((item["address"] for item in symbol_table.nm()
                        if item["name"] == "gui" and item["elf"] == str(firmware)), None)
    gui_members = _dwarf_struct_members(firmware, "gui")
    tab_members = _dwarf_typedef_members(firmware, "tab_t")
    in_picker = any(name == "retro_loop" or name.startswith("gui_") for name in active_names)
    if in_picker and gui_address is not None and {"tabs", "selected"}.issubset(gui_members) and "name" in tab_members:
        gui_head = read_memory(gui_address, max(gui_members.values()) + 4)
        tabs_ptr = int.from_bytes(gui_head[gui_members["tabs"]:gui_members["tabs"] + 4], "little")
        selected = int.from_bytes(gui_head[gui_members["selected"]:gui_members["selected"] + 4], "little", signed=True)
        if tabs_ptr and 0 <= selected < 32:
            tab_ptr = int.from_bytes(read_memory(tabs_ptr + selected * 4, 4), "little")
            if tab_ptr:
                tab_name = read_memory(tab_ptr + tab_members["name"], 64).split(b"\0", 1)[0]
                label = tab_name.decode("utf-8", errors="replace").strip()
            else:
                label = ""
            if label:
                canonical = {"favorites": "Favorites", "homebrew": "Homebrew"}
                label = canonical.get(label.casefold(), label)
                prefix = "Core: " if label.casefold() not in {"favorites", "homebrew"} else ""
                return f"Picker: {prefix}{label}"

    function_names = {name for name in active_names if name}
    if current and current["elf"] != str(firmware):
        return "Running"
    if function_names & {"run_gwhb_homebrew", "run_homebrew"}:
        return "Starting"
    if function_names & {"app_main", "main"}:
        return "Running"
    if current or function_names:
        return "Initializing"
    return "Unknown"


def _application_state(row: dict[str, Any]) -> str:
    """Poll Retro-Go or app state using QMP and the profile's official ELF symbols."""
    from .debug_shell import SymbolTable

    qmp = row.get("qmpSocket")
    if not qmp:
        raise RuntimeError("GWemu has no QMP control channel; application state cannot be polled")
    root = Path(row["profile"]) if row.get("profile") else None
    firmware = root / "debug" / "retro-go-debug.elf" if root else None
    if not firmware or not firmware.is_file():
        return "Unknown"

    symbol_table = SymbolTable()
    symbol_table.load(firmware)
    app_elfs = sorted((root / "debug" / "apps").rglob("*.elf"))
    for elf in app_elfs:
        symbol_table.load(elf)
    def read_memory(address, size):
        return _qmp_read_memory(qmp, address, size)
    symbol_table.rebase_from_runtime_pointers(read_memory)
    registers = _qmp_registers(qmp)
    return _application_state_from_target(symbol_table, firmware, app_elfs,
                                          read_memory, registers)



def show_instances(*, output: str = "text", no_pager: bool = False) -> int:
    rows = instances()
    if not rows:
        restriction = _process_scan_restriction()
        if restriction:
            raise RuntimeError("cannot confirm whether GWemu is running: process scan is "
                               f"restricted ({restriction}); grant PID namespace visibility")
    for row in rows:
        try:
            row["application"] = _application_state(row)
        except (OSError, RuntimeError, ValueError) as error:
            row["application"] = "Unknown"
            row["applicationDetail"] = str(error)
    if output == "json":
        print(json.dumps(rows, indent=2))
    elif not rows:
        restriction = _process_scan_restriction()
        if restriction:
            print("Cannot confirm whether GWemu is running: this process scan is "
                  f"restricted ({restriction}). gwprov requires visibility of the "
                  "GWemu PID namespace. Daemon IPC uses the per-user local endpoint; "
                  "GDB may require loopback TCP connect access. This is not inherently "
                  "a NET_ADMIN requirement; an empty scan is inconclusive.")
            return 2
        print("No GWemu instances running in the current process namespace.")
    else:
        from .cli.text import print_process_list
        print_process_list(rows, title="GWemu instances", no_pager=no_pager)
    return 2 if any(row["running"] is None for row in rows) else 0


def stop_instance(*, pid: int | None = None, profile: str | None = None,
                  timeout: float = 10.0) -> int:
    if (pid is None) == (profile is None):
        raise ValueError("select exactly one instance with --pid or --profile")
    rows = instances()
    if profile:
        from .profiles import resolve_profile_path
        root = str(resolve_profile_path(profile))
        rows = [row for row in rows if row["profile"] == root]
    else:
        rows = [row for row in rows if row["pid"] == pid]
    if not rows:
        raise ValueError("no matching GWemu instance is running")
    if len(rows) != 1:
        raise ValueError("profile matches multiple GWemu instances; stop one by --pid")
    row = rows[0]
    qmp_path = row["qmpSocket"]
    process = psutil.Process(row["pid"])
    qmp_missing = not qmp_path
    if qmp_path and qmp_path.startswith("gwprov://"):
        try:
            _qmp_execute(qmp_path, "quit")
        except (OSError, RuntimeError):
            qmp_missing = True
    elif qmp_path:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        try:
            sock.connect(qmp_path)
            stream = sock.makefile("rwb", buffering=0)
            stream.readline()
            stream.write(b'{"execute":"qmp_capabilities"}\r\n')
            stream.readline()
            stream.write(b'{"execute":"quit"}\r\n')
            # QMP may close immediately after accepting quit.
            try:
                stream.readline()
            except OSError:
                pass
        except OSError as exc:
            path_too_long = exc.errno == errno.ENAMETOOLONG or \
                "AF_UNIX path too long" in str(exc)
            if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED) and not path_too_long:
                raise
            # QMP may be missing, refused, or impossible to address because
            # its Unix path exceeded the platform limit. SIGTERM is a graceful
            # fallback; never force-kill here.
            qmp_missing = True
        finally:
            sock.close()
    if qmp_missing:
        process.terminate()
    try:
        process.wait(timeout=timeout)
    except psutil.TimeoutExpired as exc:
        raise RuntimeError(f"GWemu pid {row['pid']} did not exit after QMP quit") from exc
    print(f"Stopped GWemu pid {row['pid']} ({row['profile']}).")
    return 0



def set_instance_running(profile: str | None = None, *, pid: int | None = None,
                         running: bool) -> int:
    """Pause or resume one visible instance through its QMP endpoint."""
    if (profile is None) == (pid is None):
        raise ValueError("select exactly one GWemu instance with profile or pid")
    if profile:
        from .profiles import DeviceProfile
        root = str(DeviceProfile.load(profile).root)
        matches = [row for row in instances() if row["profile"] == root]
        selector = root
    else:
        matches = [row for row in instances() if row["pid"] == pid]
        selector = f"pid {pid}"
    if len(matches) != 1:
        if _process_scan_restriction() and not matches:
            raise RuntimeError(
                "cannot verify the GWemu process in this restricted process view; "
                "grant PID namespace visibility before sending QMP commands")
        raise ValueError(f"expected one GWemu instance for {selector}, found {len(matches)}")
    row = matches[0]
    if not row["qmpSocket"]:
        raise ValueError("GWemu instance has no QMP control channel")
    status = _qmp_execute(row["qmpSocket"], "query-status").get("return", {})
    if bool(status.get("running")) == running:
        print(f"GWemu pid {row['pid']} already "
              f"{'running' if running else 'paused'}.")
        return 0
    command = "cont" if running else "stop"
    _qmp_execute(row["qmpSocket"], command)
    deadline = time.monotonic() + 2.0
    while True:
        observed = _qmp_execution(row["qmpSocket"])
        if observed.get("running") is running:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f"QMP {command} did not establish the requested execution state: {observed}")
        time.sleep(0.05)
    print(f"GWemu pid {row['pid']} {'resumed' if running else 'paused'}.")
    return 0


def start_instance(profile: str, *, audio: bool = False,
                   gdb_port: int | None = None, qmp_socket: str | None = None,
                   headless: bool = False, timeline: str | None = None,
                   record_timeline: str | None = None) -> int:
    from .profiles import DeviceProfile

    device = DeviceProfile.load(profile)
    root = device.root
    matches = [row for row in instances() if row["profile"] == str(root)]
    if matches:
        row = matches[0]
        raise ValueError(f"profile already has GWemu pid {row['pid']} ({row['status']}); "
                         "use `gwprov gwemu ps` or `gwprov gwemu stop`")
    if qmp_socket:
        raise ValueError("managed GWemu uses QMP over the private GWProv daemon channel; "
                         "--qmp-socket is not available for `gwprov gwemu start`")
    if gdb_port is None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            gdb_port = probe.getsockname()[1]
    from .daemon import start_instance as daemon_start
    row = daemon_start(str(root), audio=audio, gdb_port=gdb_port, headless=headless,
                      timeline=timeline, record_timeline=record_timeline)
    display = "headless" if headless else "visible"
    print(f"Started {display} GWemu for {root} (pid {row['pid']}); "
          f"GDB :{gdb_port}; QMP managed by GWProv daemon.", flush=True)
    return 0
