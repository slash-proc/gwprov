"""Live native PC sampling through shared QMP access, without taking GDB."""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import time

from .qmp import QMPConnection


def sample_profile(qmp_socket, symbols, *, duration=15.0, interval=0.02,
                   progress_symbols=None, rebase_symbols=None, process=None) -> dict:
    """Return raw snapshots and a function histogram. No target writes or halts.

    Percentages count running-state PC snapshots, NOT measured Cortex-M cycles.
    QEMU exposes the PC at monitor synchronization points, so this is a hotspot
    locator with sampling bias, not an instruction-accurate cycle profiler.
    """
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("profile duration must be finite and positive")
    if not math.isfinite(interval) or interval < 0.005:
        raise ValueError("profile interval must be finite and at least 0.005 seconds")
    selected = list(dict.fromkeys(progress_symbols or []))
    if progress_symbols is None:
        selected = [row["name"] for row in symbols.nm() if
                    row["type"] == "STT_OBJECT" and row["size"] == 4 and
                    any(term in row["name"].lower() for term in
                        ("heartbeat", "frame_count", "frame_counter", "progress_count"))]
    addresses = {name: symbols[name] for name in selected}
    rebases = []
    for spec in rebase_symbols or []:
        section, separator, pointer = spec.partition("=")
        if not separator or not section or not pointer:
            raise ValueError(f"invalid rebase {spec!r}; expected SECTION=POINTER_SYMBOL")
        # Reject a misspelled symbol before sampling.
        symbols[pointer]
        if not any(section in symbols.sections(owner) for owner in symbols.sources):
            raise ValueError(f"section {section!r} is absent from pointer's owning ELF")
        rebases.append((section, pointer))

    def make_index():
        rows = sorted((row for row in symbols.functions() if
                       row["type"] == "STT_FUNC" and row["size"] > 0),
                      key=lambda row: (row["address"] & ~1, row["name"]))
        return rows, [row["address"] & ~1 for row in rows]

    def resolve(pc):
        index = bisect_right(starts, pc & ~1) - 1
        if index >= 0:
            row = functions[index]
            if (pc & ~1) < (row["address"] & ~1) + row["size"]:
                return row
        return None

    samples = []
    counters = []
    states = Counter()
    functions, starts = make_index()
    resolved_rebases = {}
    io_seconds = 0.0
    interrupted = False
    cpu_before = process.cpu_times() if process else None
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    next_counters = started
    rng = random.Random()
    try:
        while time.monotonic() - started < duration:
            sample_started = time.monotonic()
            with QMPConnection(qmp_socket) as qmp:
                status = qmp.execute("query-status")["return"]
                state = status.get("status", "unknown")
                states[state] += 1
                now = time.monotonic()
                if status.get("running"):
                    if now >= next_counters:
                        for section, pointer in rebases:
                            base = qmp.u32(symbols[pointer])
                            if base and resolved_rebases.get(section) != base:
                                symbols.rebase(section, base)
                                resolved_rebases[section] = base
                                functions, starts = make_index()
                                addresses = {name: symbols[name] for name in selected}
                        values = {name: qmp.u32(address) for name, address in addresses.items()}
                        # CYCCNT is read-only here. Firmware may reset it each loop;
                        # retain raw values instead of inventing a cumulative count.
                        dwt = qmp.read_memory(0xE0001000, 8)
                        cpu_now = process.cpu_times() if process else None
                        counters.append({"elapsed": time.monotonic() - started,
                                         "values": values,
                                         "emulator_cpu_seconds": cpu_now.user + cpu_now.system if cpu_now else None,
                                         "dwt_ctrl": int.from_bytes(dwt[:4], "little"),
                                         "dwt_cyccnt": int.from_bytes(dwt[4:], "little")})
                        next_counters = time.monotonic() + 1.0
                    regs = qmp.registers()
                    pc = regs["pc"] & ~1
                    function = resolve(pc)
                    samples.append({"elapsed": time.monotonic() - started,
                                    "pc": pc, "sp": regs["sp"], "lr": regs["lr"],
                                    "xpsr": regs.get("xpsr"),
                                    "function": function["name"] if function else None,
                                    "symbol_source": function["elf"] if function else None,
                                    "source_format": function.get("source_format", "elf") if function else None,
                                    "elf": function["elf"] if function and function.get("source_format", "elf") == "elf" else None})
            io_seconds += time.monotonic() - sample_started
            # Avoid locking samples to a frame/interrupt period. Never catch
            # up missed polls with a burst of reads that stalls the emulator.
            remaining = duration - (time.monotonic() - started)
            delay = interval * rng.uniform(0.8, 1.2) - (time.monotonic() - sample_started)
            if remaining > 0 and delay > 0:
                time.sleep(min(delay, remaining))
    except KeyboardInterrupt:
        interrupted = True
    elapsed = time.monotonic() - started
    cpu_after = process.cpu_times() if process else None
    cpu_seconds = ((cpu_after.user + cpu_after.system) -
                   (cpu_before.user + cpu_before.system)) if cpu_before and cpu_after else None
    def sample_key(sample):
        return (sample["symbol_source"], sample["function"] or f"0x{sample['pc']:08x}", sample["source_format"])
    groups = Counter(sample_key(sample) for sample in samples)
    pcs = {}
    for sample in samples:
        pcs.setdefault(sample_key(sample), Counter())[sample["pc"]] += 1
    ranked = []
    for key, count in groups.most_common():
        pc = pcs[key].most_common(1)[0][0]
        ranked.append({"elf": key[0] if key[2] == "elf" else None,
                       "symbol_source": key[0], "source_format": key[2],
                       "function": key[1] if key[0] else None,
                       "label": key[1], "samples": count,
                       "percent": 100.0 * count / len(samples), "representative_pc": pc,
                       "sampled_pcs": [{"pc": address, "samples": hits}
                                       for address, hits in pcs[key].most_common()]})
    for row in ranked[:20]:
        if row["function"]:
            row["source"] = symbols.source_location(row["representative_pc"])
    progress = {}
    for name in selected:
        if len(counters) < 2:
            progress[name] = {"observations": len(counters)}
            continue
        first, last = counters[0], counters[-1]
        values = [item["values"][name] for item in counters]
        resets = sum(b < a and not (a > 0xf0000000 and b < 0x10000000)
                     for a, b in zip(values, values[1:]))
        delta = sum((b - a) & 0xffffffff for a, b in zip(values, values[1:]))
        span = last["elapsed"] - first["elapsed"]
        counter_cpu = (last["emulator_cpu_seconds"] - first["emulator_cpu_seconds"]
                       if last["emulator_cpu_seconds"] is not None else None)
        progress[name] = {"first": values[0], "last": values[-1], "resets": resets,
                          "delta": delta if not resets else None,
                          "per_wall_second": delta / span if span > 0 and not resets else None,
                          "emulator_cpu_ms_per_increment":
                              counter_cpu * 1000 / delta
                              if counter_cpu is not None and delta and not resets else None}
    warnings = [
        "Function percentages are PC sample shares, not exact cycle counts.",
        "QMP synchronizes with QEMU; samples can overrepresent MMIO and interrupt boundaries.",
        "DWT observations are raw counter values; firmware may reset them and GWemu timing depends on its build.",
    ]
    if any(state != "running" for state in states):
        warnings.append("Paused/stopped states were observed; hotspot samples cover running snapshots only.")
    if io_seconds / elapsed > 0.1:
        warnings.append("QMP requests consumed over 10% of sampling wall time; use a longer interval.")
    unresolved = sum(sample["function"] is None for sample in samples)
    if unresolved:
        warnings.append(f"{unresolved} samples are outside loaded function ranges; check symbols/rebasing.")
    return {"schema_version": 1, "method": "qmp-native-pc-sampling",
            "started_at": started_at, "duration_seconds": elapsed,
            "requested_interval_seconds": interval, "interrupted": interrupted,
            "sample_count": len(samples), "unresolved_samples": unresolved,
            "observed_samples_per_second": len(samples) / elapsed,
            "qmp_request_seconds": io_seconds,
            "qmp_request_wall_percent": io_seconds / elapsed * 100,
            "emulator_cpu_seconds": cpu_seconds,
            "emulator_cpu_percent": cpu_seconds / elapsed * 100 if cpu_seconds is not None else None,
            "states": dict(states), "rebased_sections": resolved_rebases,
            "progress": progress, "functions": ranked, "raw_samples": samples,
            "counter_observations": counters, "warnings": warnings}


def profile_instance(profile, *, duration=15.0, interval=0.02, symbols=None,
                     progress_symbols=None, rebase_symbols=None, output=None,
                     output_format="text", top=20, debug_config=None) -> int:
    from .debug_shell import SymbolTable
    from .gwemu_manager import instances, screenshot_qmp, _process_scan_restriction
    from .profiles import DeviceProfile
    import psutil

    if top < 1:
        raise ValueError("--top must be positive")
    device = DeviceProfile.load(profile)
    matches = [row for row in instances() if row["profile"] == str(device.root)]
    if len(matches) != 1:
        restriction = _process_scan_restriction()
        raise RuntimeError(f"expected one running GWemu for {device.root}; found {len(matches)}"
                           + (f"; {restriction}" if restriction else ""))
    row = matches[0]
    if not row.get("qmpSocket"):
        raise RuntimeError("GWemu needs a QMP socket for live profiling")
    table = SymbolTable()
    firmware = device.root / "debug/retro-go-debug.elf"
    paths = [firmware] if firmware.is_file() else []
    paths += sorted((device.root / "debug/apps").rglob("*.elf"))
    paths += [Path(path).expanduser().resolve() for path in symbols or []]
    for path in dict.fromkeys(paths):
        if not path.is_file():
            raise FileNotFoundError(f"profiling symbols not found: {path}")
        table.load(path)
    settings = table.load_config(debug_config) if debug_config else {}
    if progress_symbols is None:
        progress_symbols = settings.get("progress_symbols") or None
    rebase_symbols = list(dict.fromkeys([*settings.get("rebase_symbols", []), *(rebase_symbols or [])]))
    paths = table.sources + ([Path(settings["path"])] if settings else [])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    destination = (Path(output).expanduser().resolve() if output else
                   device.root / "runtime/gwprov/profiles" / f"profile-{stamp}-{time.time_ns()}.json")
    if destination.exists():
        raise ValueError(f"profile report already exists: {destination}; choose another output")
    destination.parent.mkdir(parents=True, exist_ok=True)
    screenshot_before = screenshot_qmp(row["qmpSocket"], destination.with_name(destination.stem + "-before.png"))
    report = sample_profile(row["qmpSocket"], table, duration=duration, interval=interval,
                            progress_symbols=progress_symbols, rebase_symbols=rebase_symbols,
                            process=psutil.Process(row["pid"]))
    report.update({"pid": row["pid"], "profile": str(device.root), "debug_configuration": settings,
                   "symbols": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                               for path in dict.fromkeys(paths)],
                   "screenshot_before": screenshot_before,
                   "screenshot_after": screenshot_qmp(row["qmpSocket"], destination.with_name(destination.stem + "-after.png"))})
    destination.write_text(json.dumps(report, indent=2) + "\n")
    if output_format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(f"Native PC profile: {report['sample_count']} samples in {report['duration_seconds']:.2f}s")
        if report['emulator_cpu_percent'] is not None:
            print(f"GWemu CPU: {report['emulator_cpu_percent']:.1f}%; "
                  f"QMP request wall time: {report['qmp_request_wall_percent']:.1f}%")
        print("VM states:", dict(report["states"]))
        print("  SAMPLE %   HITS  FUNCTION")
        for item in report["functions"][:top]:
            print(f"  {item['percent']:8.2f} {item['samples']:6d}  {item['label']}")
        for name, value in report["progress"].items():
            rate = value.get("per_wall_second")
            if rate is not None:
                print(f"{name}: {rate:.2f} increments / wall second")
        print(f"Report and PNGs: {destination}")
        for warning in report["warnings"]:
            print(f"Note: {warning}")
    return 0 if report["sample_count"] else 2
