"""Non-halting hardware profiler for projects exporting the GWProv trace ABI."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .trace_abi import read_trace, summarize


def _symbols(profile: str | None, symbols: list[str]):
    from .debug_shell import SymbolTable
    table = SymbolTable()
    inputs = [Path(item).expanduser().resolve() for item in symbols]
    if profile:
        from .profiles import DeviceProfile
        device = DeviceProfile.load(profile)
        firmware = device.root / "debug" / "retro-go-debug.elf"
        if firmware.is_file():
            inputs.insert(0, firmware)
        apps = sorted((device.root / "debug" / "apps").rglob("*.elf"))
        inputs.extend(apps)
    if not inputs:
        raise ValueError("provide --profile or at least one --symbols ELF")
    for path in inputs:
        if not path.is_file():
            raise FileNotFoundError(f"symbols not found: {path}")
        table.load(path)
    return table


def profile_hardware(*, probe_id: str | None = None, programmer: str | None = None,
                     remote_url: str | None = None, remote_origin: str | None = None, profile: str | None = None,
                     symbols: list[str] | None = None, duration: float = 10.0,
                     interval: float = 0.05, output: str | None = None,
                     output_format: str = "text", top: int = 10,
                     counter_symbols: list[str] | None = None,
                     frame_counter: str | None = None, total_cycle_counter: str | None = None,
                     start_at: str | None = None,
                     set_registers: list[str] | None = None,
                     startup_timeout: float = 120.0) -> int:
    if duration <= 0 or interval <= 0 or top <= 0:
        raise ValueError("duration, interval, and top must be positive")
    selected = sum(bool(value) for value in (probe_id, programmer, remote_url))
    if selected > 1:
        raise ValueError("select one of --probe-id, --programmer, or --remote-url")
    if output_format not in {"text", "json", "html", "pdf"}:
        raise ValueError("format must be text, json, html, or pdf")
    if remote_origin and not remote_url:
        raise ValueError("--remote-origin requires --remote-url")
    table = _symbols(profile, symbols or [])
    counter_symbols = list(dict.fromkeys(counter_symbols or []))
    set_registers = set_registers or []
    if not counter_symbols:
        try:
            table["gwprov_trace_header"]
        except KeyError as exc:
            raise ValueError("loaded symbols do not define gwprov_trace_header; provide --counter-symbol for project counters") from exc
    counter_rows = {}
    if counter_symbols:
        for name in counter_symbols:
            rows = [row for row in table.nm(name) if row["name"] == name]
            if not rows or rows[-1]["type"] != "STT_OBJECT" or rows[-1]["size"] not in (1, 2, 4, 8):
                raise ValueError(f"counter {name!r} must be a sized 1, 2, 4, or 8 byte ELF object")
            counter_rows[name] = rows[-1]
        if frame_counter and frame_counter not in counter_rows:
            raise ValueError("--frame-counter must also be listed with --counter-symbol")
        if total_cycle_counter and total_cycle_counter not in counter_rows:
            raise ValueError("--total-cycle-counter must also be listed with --counter-symbol")
    if start_at:
        if not probe_id:
            raise ValueError("--start-at currently requires --probe-id (local PyOCD hardware)")
        if not counter_symbols:
            raise ValueError("--start-at requires at least one --counter-symbol")
        if startup_timeout <= 0:
            raise ValueError("startup timeout must be positive")
    elif set_registers:
        raise ValueError("--set-register requires --start-at")
    parsed_registers = []
    for assignment in set_registers:
        register, separator, value = assignment.partition("=")
        register = register.strip().lower()
        if not separator or register not in {*(f"r{i}" for i in range(13)), "sp", "lr", "xpsr"}:
            raise ValueError(f"invalid register assignment {assignment!r}; use r0-r12, sp, lr, or xpsr")
        try:
            parsed_registers.append((register, int(value.strip(), 0)))
        except ValueError as exc:
            raise ValueError(f"invalid register value in {assignment!r}") from exc

    if remote_url:
        from .backends import WebSocketBackend
        backend = WebSocketBackend(remote_url, origin=remote_origin,
                                   operation="gwprov hardware profile")
    elif probe_id:
        from .backends import SelectedPyOCDBackend
        backend = SelectedPyOCDBackend(probe_id, operation="gwprov hardware profile")
    elif programmer:
        from .backends import SelectedOpenOCDBackend
        backend = SelectedOpenOCDBackend(programmer, operation="gwprov hardware profile")
    else:
        from .backends import AutoOpenOCDBackend
        backend = AutoOpenOCDBackend(operation="gwprov hardware profile")

    events = []
    counter_observations = []
    dropped = 0
    profiler_halted_target = False
    startup_breakpoint = None

    def read_counters():
        snapshot = {}
        for name, row in counter_rows.items():
            address, size = row["address"], row["size"]
            raw = backend.read_memory(address, size)
            if len(raw) != size:
                raise RuntimeError(f"short hardware read for {name}: expected {size} bytes, got {len(raw)}")
            snapshot[name] = int.from_bytes(raw, "little")
        return snapshot

    try:
        backend.open()
        transport = backend.probe_name
        if start_at:
            from pyocd.core.target import Target
            from .hw_lifecycle import assert_application_mode
            assert_application_mode(backend, operation="reset hardware for profiling")
            backend.reset_and_halt()
            profiler_halted_target = True
            address = table[start_at] & ~1
            if not backend.target.set_breakpoint(address, Target.BreakpointType.HW):
                raise RuntimeError(f"could not set startup breakpoint at {start_at} (0x{address:08x})")
            startup_breakpoint = address
            backend.resume()
            profiler_halted_target = False
            deadline = time.monotonic() + startup_timeout
            while backend.target.get_state() != Target.State.HALTED:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"hardware did not reach startup symbol {start_at!r} within {startup_timeout:g}s")
                time.sleep(0.01)
            profiler_halted_target = True
            pc = int(backend.target.read_core_register("pc")) & ~1
            if pc != address:
                raise RuntimeError(f"startup stopped at 0x{pc:08x}, expected {start_at} at 0x{address:08x}")
            for register, value in parsed_registers:
                backend.write_register(register, value)
            backend.target.remove_breakpoint(address)
            startup_breakpoint = None
            counter_initial = read_counters()
            counter_observations.append({"elapsedSeconds": 0.0, "values": counter_initial})
            start = time.monotonic()
            backend.resume()
            profiler_halted_target = False
            initial = None
            sequence = dropped_start = None
        else:
            table.rebase_from_runtime_pointers(backend.read_memory)
            if counter_symbols:
                initial = None
                sequence = dropped_start = None
            else:
                header_address = table["gwprov_trace_header"]
                initial = read_trace(backend.read_memory, header_address)
                sequence = initial["sequence"]
                dropped_start = initial["dropped"]
            start = time.monotonic()

        if counter_symbols and not start_at:
            counter_initial = read_counters()
            counter_observations.append({"elapsedSeconds": 0.0, "values": counter_initial})
        elif not counter_symbols:
            counter_initial = None
        if not start_at:
            start = time.monotonic()
        next_progress = 5.0
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= duration:
                break
            if counter_symbols:
                values = read_counters()
                counter_observations.append({"elapsedSeconds": elapsed, "values": values})
                if elapsed >= next_progress:
                    progress = f"; {values[frame_counter]} frames" if frame_counter else ""
                    print(f"Hardware profile {elapsed:.0f}/{duration:.0f}s{progress}", flush=True)
                    next_progress = elapsed + 5.0
            else:
                sample = read_trace(backend.read_memory, header_address, since_sequence=sequence)
                events.extend(sample["events"])
                sequence = sample["sequence"]
                dropped += sample["missing"] + sample["overwritten_since_sequence"]
            time.sleep(min(interval, max(0.0, duration - (time.monotonic() - start))))
        if counter_symbols:
            counter_observations.append({"elapsedSeconds": time.monotonic() - start, "values": read_counters()})
            final = None
        else:
            final = read_trace(backend.read_memory, header_address, since_sequence=sequence)
            events.extend(final["events"])
            dropped += final["missing"] + final["overwritten_since_sequence"]
    finally:
        if startup_breakpoint is not None:
            try:
                backend.target.remove_breakpoint(startup_breakpoint)
            except Exception:
                pass
        if profiler_halted_target:
            try:
                backend.resume()
            except Exception:
                pass
        backend.close()

    def resolve(pc):
        info = table.nearest(pc)
        return info["name"] if info else f"0x{pc:08x}"

    summary = summarize(events, resolve) if not counter_symbols else {"functions": [], "unclosed_entries": 0,
                                                                      "unmatched_exits": 0}
    total_cycles = sum(row["exclusive_cycles"] for row in summary["functions"])
    for row in summary["functions"]:
        row["percent_of_attributed_cycles"] = (
            100.0 * row["exclusive_cycles"] / total_cycles if total_cycles else 0.0)
    counter_report = []
    if counter_symbols:
        elapsed = counter_observations[-1]["elapsedSeconds"] if counter_observations else 0.0
        start_values = counter_observations[0]["values"]
        end_values = counter_observations[-1]["values"]
        for name in counter_symbols:
            width = counter_rows[name]["size"] * 8
            delta = (end_values[name] - start_values[name]) & ((1 << width) - 1)
            counter_report.append({"name": name, "widthBits": width,
                                   "start": start_values[name], "end": end_values[name],
                                   "delta": delta,
                                   "perSecond": delta / elapsed if elapsed > 0 else None})
        total_delta = next((row["delta"] for row in counter_report
                            if row["name"] == total_cycle_counter), None)
        if total_delta:
            for row in counter_report:
                if row["name"].endswith("_cycles"):
                    row["percentOfTotal"] = 100.0 * row["delta"] / total_delta
        counter_report.sort(key=lambda row: row["delta"], reverse=True)
    warnings = []
    if final is not None:
        if not final["flags"] & 1:
            warnings.append("trace timestamps are not marked as DWT CYCCNT")
        if dropped or final["dropped"] > dropped_start:
            warnings.append("trace entries were overwritten or inconsistent during capture")
        if not summary["functions"]:
            warnings.append("no complete routine enter/exit pairs were captured")
    elif frame_counter and not next((row["delta"] for row in counter_report if row["name"] == frame_counter), 0):
        warnings.append("frame counter did not advance during capture")
    report = {
        "schemaVersion": 2 if counter_symbols else 1, "target": "hardware", "transport": transport,
        "measurement": "project counters" if counter_symbols else "project GWProv trace ABI v1",
        "durationSeconds": duration, "actualDurationSeconds": (counter_observations[-1]["elapsedSeconds"] if counter_symbols else duration),
        "intervalSeconds": interval, "eventCount": len(events),
        "traceSequenceStart": initial["sequence"] if final is not None else None,
        "traceSequenceEnd": final["sequence"] if final is not None else None,
        "traceCapacity": final["capacity"] if final is not None else None,
        "traceDropped": final["dropped"] if final is not None else None,
        "unavailableEvents": dropped,
        "dwtEnabled": bool(final["flags"] & 1) if final is not None else None,
        "counterHz": final["counter_hz"] if final is not None else None,
        "totalAttributedCycles": total_cycles, "counters": counter_report,
        "frameCounter": frame_counter, "totalCycleCounter": total_cycle_counter,
        "startAt": start_at,
        "counterObservations": counter_observations,
        **summary, "warnings": warnings,
    }
    if output:
        requested = Path(output).expanduser().resolve()
        json_path = requested if output_format in {"json", "text"} else requested.with_suffix(".json")
        artifact_path = requested
    else:
        directory = Path("dev-local/reports")
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        json_path = directory / f"hardware-profile-{stamp}.json"
        artifact_path = (directory / f"hardware-profile-{stamp}.{output_format}"
                         if output_format in {"html", "pdf"} else json_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if output_format in {"html", "pdf"}:
        from .reports import render_report
        render_report(json_path, artifact_path, format=output_format)

    if output_format == "json":
        print(json.dumps(report, indent=2))
    elif counter_symbols:
        print(f"Hardware profile: {report['actualDurationSeconds']:.2f}s; {len(counter_observations)} counter snapshots")
        print("Counter                                  Delta           Rate/s     % total")
        for row in counter_report[:top]:
            rate = f"{row['perSecond']:.2f}" if row["perSecond"] is not None else "n/a"
            percent = f"{row['percentOfTotal']:.2f}%" if "percentOfTotal" in row else ""
            print(f"{row['name']:<40} {row['delta']:>12} {rate:>16} {percent:>10}")
        if frame_counter:
            frame_row = next(row for row in counter_report if row["name"] == frame_counter)
            elapsed = report["actualDurationSeconds"]
            print(f"Frame rate: {frame_row['delta'] / elapsed:.2f} logical frames/s" if elapsed else "Frame rate: n/a")
        for warning in warnings:
            print(f"Warning: {warning}")
        if output_format in {"html", "pdf"}:
            print(f"Rendered {output_format.upper()}: {artifact_path}")
        print(f"JSON report: {json_path}")
    else:
        print(f"Hardware profile: {len(events)} trace events; "
              f"{total_cycles} attributed exclusive cycles")
        print(f"DWT: {'enabled' if report['dwtEnabled'] else 'not marked enabled'}; "
              f"counter rate {report['counterHz'] or 'unknown'} Hz")
        for row in summary["functions"][:top]:
            print(f"{row['function']:<40} {row['exclusive_cycles']:>12} cycles "
                  f"{row['percent_of_attributed_cycles']:6.2f}%")
        for warning in warnings:
            print(f"Warning: {warning}")
        if output_format in {"html", "pdf"}:
            print(f"Rendered {output_format.upper()}: {artifact_path}")
        print(f"JSON report: {json_path}")
    return 0 if events or counter_report else 2
