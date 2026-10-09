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
                     output_format: str = "text", top: int = 10) -> int:
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
    try:
        table["gwprov_trace_header"]
    except KeyError as exc:
        raise ValueError("loaded symbols do not define gwprov_trace_header; see docs/TRACE_ABI.md") from exc

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
    dropped = 0
    try:
        backend.open()
        transport = backend.probe_name
        table.rebase_from_runtime_pointers(backend.read_memory)
        header_address = table["gwprov_trace_header"]
        initial = read_trace(backend.read_memory, header_address)
        sequence = initial["sequence"]
        dropped_start = initial["dropped"]
        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= duration:
                break
            sample = read_trace(backend.read_memory, header_address, since_sequence=sequence)
            events.extend(sample["events"])
            sequence = sample["sequence"]
            dropped += sample["missing"] + sample["overwritten_since_sequence"]
            time.sleep(min(interval, max(0.0, duration - (time.monotonic() - start))))
        final = read_trace(backend.read_memory, header_address, since_sequence=sequence)
        events.extend(final["events"])
        dropped += final["missing"] + final["overwritten_since_sequence"]
    finally:
        backend.close()

    def resolve(pc):
        info = table.nearest(pc)
        return info["name"] if info else f"0x{pc:08x}"

    summary = summarize(events, resolve)
    total_cycles = sum(row["exclusive_cycles"] for row in summary["functions"])
    for row in summary["functions"]:
        row["percent_of_attributed_cycles"] = (
            100.0 * row["exclusive_cycles"] / total_cycles if total_cycles else 0.0)
    warnings = []
    if not final["flags"] & 1:
        warnings.append("trace timestamps are not marked as DWT CYCCNT")
    if dropped or final["dropped"] > dropped_start:
        warnings.append("trace entries were overwritten or inconsistent during capture")
    if not summary["functions"]:
        warnings.append("no complete routine enter/exit pairs were captured")
    report = {
        "schemaVersion": 1, "target": "hardware", "transport": transport,
        "measurement": "project GWProv trace ABI v1", "durationSeconds": duration,
        "intervalSeconds": interval, "eventCount": len(events),
        "traceSequenceStart": initial["sequence"], "traceSequenceEnd": final["sequence"],
        "traceCapacity": final["capacity"], "traceDropped": final["dropped"],
        "unavailableEvents": dropped, "dwtEnabled": bool(final["flags"] & 1),
        "counterHz": final["counter_hz"], "totalAttributedCycles": total_cycles,
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
    return 0 if events else 2
