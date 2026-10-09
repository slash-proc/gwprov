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
                   progress_symbols=None, rebase_symbols=None, process=None, stop_event=None,
                   stall_threshold=1.0) -> dict:
    """Return raw snapshots and a function histogram. No target writes or halts.

    Percentages count running-state PC snapshots, NOT measured Cortex-M cycles.
    QEMU exposes the PC at monitor synchronization points, so this is a hotspot
    locator with sampling bias, not an instruction-accurate cycle profiler.
    """
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("profile duration must be finite and positive")
    if not math.isfinite(interval) or interval < 0.005:
        raise ValueError("profile interval must be finite and at least 0.005 seconds")
    if not math.isfinite(stall_threshold) or stall_threshold < 0:
        raise ValueError("stall threshold must be finite and nonnegative")
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
        while time.monotonic() - started < duration and not (stop_event and stop_event.is_set()):
            sample_started = time.monotonic()
            with QMPConnection(qmp_socket) as qmp:
                status = qmp.execute("query-status")["return"]
                state = status.get("status", "unknown")
                states[state] += 1
                now = time.monotonic()
                if status.get("running"):
                    if now >= next_counters:
                        for key, base in symbols.rebase_from_runtime_pointers(qmp.read_memory).items():
                            if resolved_rebases.get(key) != base:
                                resolved_rebases[key] = base
                                functions, starts = make_index()
                                addresses = {name: symbols[name] for name in selected}
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
    named = [row for row in ranked[:20] if row["function"]]
    sources = symbols.source_locations([row["representative_pc"] for row in named])
    for row, source in zip(named, sources):
        row["source"] = source

    # Consecutive PC snapshots in one symbol are an estimate of routine
    # residency, not proof that the CPU remained in the routine continuously.
    # QMP latency and intervening unsampled execution limit the precision.
    max_sample_gap = max(interval * 3.0, 0.1)
    residency = []
    ordered_samples = sorted(samples, key=lambda sample: sample["elapsed"])
    run = []
    run_key = None
    for sample in ordered_samples:
        key = sample_key(sample)
        if run and (key != run_key or sample["elapsed"] - run[-1]["elapsed"] > max_sample_gap):
            if len(run) > 1:
                residency.append((run[-1]["elapsed"] - run[0]["elapsed"], run))
            run = []
        if not run:
            run_key = key
        run.append(sample)
    if len(run) > 1:
        residency.append((run[-1]["elapsed"] - run[0]["elapsed"], run))
    def residency_record(span, run):
        return {
            "function": run[0]["function"], "symbol_source": run[0]["symbol_source"],
            "pc": run[0]["pc"], "start_elapsed": run[0]["elapsed"],
            "end_elapsed": run[-1]["elapsed"], "observed_span_seconds": span,
            "sample_count": len(run), "max_sample_gap_seconds": max(
                (b["elapsed"] - a["elapsed"] for a, b in zip(run, run[1:])), default=0.0)}

    routine_residency = [residency_record(span, run) for span, run in
                         sorted(residency, key=lambda item: (item[0], len(item[1])), reverse=True)[:20]]
    # Summarize both the longest observed same-function interval and the sum
    # of all such intervals per function. This helps attribute apparent freezes
    # while keeping the sampling limitation explicit: time between snapshots
    # is observed residency, not proof of uninterrupted execution.
    residency_by_function = {}
    for span, run in residency:
        first = run[0]
        key = sample_key(first)
        summary = residency_by_function.setdefault(key, {
            "function": first["function"], "symbol_source": first["symbol_source"],
            "label": first["function"] or f"0x{first['pc']:08x}",
            "max_observed_span_seconds": 0.0, "total_observed_span_seconds": 0.0,
            "observed_runs": 0, "samples_in_runs": 0,
        })
        summary["max_observed_span_seconds"] = max(summary["max_observed_span_seconds"], span)
        summary["total_observed_span_seconds"] += span
        summary["observed_runs"] += 1
        summary["samples_in_runs"] += len(run)
    routine_residency_by_function = sorted(
        residency_by_function.values(),
        key=lambda item: (item["max_observed_span_seconds"], item["total_observed_span_seconds"]),
        reverse=True)
    for item in routine_residency_by_function:
        item["max_observed_span_percent_of_profile"] = (
            item["max_observed_span_seconds"] / elapsed * 100 if elapsed > 0 else 0.0)
        item["total_observed_span_percent_of_profile"] = (
            item["total_observed_span_seconds"] / elapsed * 100 if elapsed > 0 else 0.0)
    # A routine-stall candidate is a long same-function run in sampled PCs.
    # This estimates how long execution stayed in that function; it cannot
    # establish that interrupts or unobserved calls did not run between polls.
    routine_stalls = [residency_record(span, run) for span, run in
                      sorted((item for item in residency if item[0] >= stall_threshold),
                             key=lambda item: (item[0], len(item[1])), reverse=True)]

    # Counters are intentionally sampled once per second to keep QMP overhead
    # bounded. A zero-delta interval is therefore a coarse stall indication.
    stalls = []
    if counters and selected:
        for name in selected:
            segments = []
            active = []
            for previous, current in zip(counters, counters[1:]):
                gap = current["elapsed"] - previous["elapsed"]
                delta = (current["values"][name] - previous["values"][name]) & 0xffffffff
                if gap <= 0 or gap > 2.5:
                    if active:
                        segments.append(active)
                        active = []
                elif delta == 0:
                    if not active:
                        active = [previous]
                    active.append(current)
                elif active:
                    segments.append(active)
                    active = []
            if active:
                segments.append(active)
            for segment in segments:
                span = segment[-1]["elapsed"] - segment[0]["elapsed"]
                if span < stall_threshold:
                    continue
                nearby = [sample for sample in ordered_samples
                          if segment[0]["elapsed"] <= sample["elapsed"] <= segment[-1]["elapsed"]]
                routines = Counter(sample["function"] or f"0x{sample['pc']:08x}" for sample in nearby)
                stalls.append({"counter": name, "start_elapsed": segment[0]["elapsed"],
                               "end_elapsed": segment[-1]["elapsed"], "duration_seconds": span,
                               "counter_resolution_seconds": max(
                                   (b["elapsed"] - a["elapsed"] for a, b in zip(segment, segment[1:])),
                                   default=None),
                               "sample_count": len(nearby),
                               "dominant_routines": [{"function": function, "samples": count,
                                                      "percent": count * 100 / len(nearby)}
                                                     for function, count in routines.most_common(5)]
                               if nearby else []})
    stalls.sort(key=lambda item: item["duration_seconds"], reverse=True)
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
        "Routine residency and progress stalls are sampled estimates; counter stalls have roughly one-second resolution and do not prove a routine blocked the system.",
    ]
    dwt_ctrl_values = sorted({item["dwt_ctrl"] for item in counters})
    dwt_enable_states = {bool(value & 1) for value in dwt_ctrl_values}
    if not counters:
        dwt_status = "unobserved"
    elif dwt_enable_states == {True}:
        dwt_status = "enabled"
    elif dwt_enable_states == {False}:
        dwt_status = "disabled"
    else:
        dwt_status = "mixed"
    dwt_values = [item["dwt_cyccnt"] for item in counters]
    dwt_cycle_counter = {
        "status": dwt_status,
        "ctrl_values": dwt_ctrl_values,
        "counter_changed_between_observations": (
            any(a != b for a, b in zip(dwt_values, dwt_values[1:]))
            if len(dwt_values) > 1 else None),
    }
    if dwt_status == "disabled":
        warnings.append("DWT CYCCNT is disabled (DWT_CTRL.CYCCNTENA=0); raw CYCCNT values are not elapsed cycle measurements.")
    if any(state != "running" for state in states):
        warnings.append("Paused/stopped states were observed; hotspot samples cover running snapshots only.")
    if io_seconds / elapsed > 0.1:
        warnings.append("QMP requests consumed over 10% of sampling wall time; use a longer interval.")
    unresolved = sum(sample["function"] is None for sample in samples)
    if unresolved:
        warnings.append(f"{unresolved} samples are outside loaded function ranges; check symbols/rebasing.")
    return {"schema_version": 2, "method": "qmp-native-pc-sampling",
            "started_at": started_at, "duration_seconds": elapsed,
            "requested_interval_seconds": interval, "interrupted": interrupted,
            "sample_count": len(samples), "unresolved_samples": unresolved,
            "observed_samples_per_second": len(samples) / elapsed,
            "qmp_request_seconds": io_seconds,
            "qmp_request_wall_percent": io_seconds / elapsed * 100,
            "emulator_cpu_seconds": cpu_seconds,
            "emulator_cpu_percent": cpu_seconds / elapsed * 100 if cpu_seconds is not None else None,
            "states": dict(states), "rebased_sections": resolved_rebases,
            "stopped_early": bool(stop_event and stop_event.is_set()),
            "dwt_cycle_counter": dwt_cycle_counter,
            "progress": progress, "routine_residency": routine_residency,
            "routine_residency_by_function": routine_residency_by_function,
            "routine_stalls": routine_stalls,
            "progress_stalls": stalls, "stall_threshold_seconds": stall_threshold,
            "functions": ranked, "raw_samples": samples,
            "counter_observations": counters, "warnings": warnings}


def profile_instance(profile, *, duration=15.0, interval=0.02, symbols=None,
                     progress_symbols=None, rebase_symbols=None, output=None,
                     output_format="text", top=20, debug_config=None,
                     stall_threshold=1.0) -> int:
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
                            process=psutil.Process(row["pid"]),
                            stall_threshold=stall_threshold)
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
        dwt = report["dwt_cycle_counter"]
        ctrl = ", ".join(f"0x{value:08x}" for value in dwt["ctrl_values"]) or "unobserved"
        print(f"DWT CYCCNT: {dwt['status']} (DWT_CTRL {ctrl})")
        print("  SAMPLE %   HITS  FUNCTION")
        for item in report["functions"][:top]:
            print(f"  {item['percent']:8.2f} {item['samples']:6d}  {item['label']}")
        print("Longest per-routine observed residency (estimated from same-function samples):")
        for item in report["routine_residency_by_function"][:10]:
            print(f"  max {item['max_observed_span_seconds']:7.3f}s; "
                  f"total {item['total_observed_span_seconds']:7.3f}s across "
                  f"{item['observed_runs']} runs ({item['total_observed_span_percent_of_profile']:.1f}% of profile)  "
                  f"{item['label']}")
        if report["routine_stalls"]:
            print(f"Routine-stall candidates (same function across samples for >= {stall_threshold:.2f}s):")
            for item in report["routine_stalls"][:10]:
                label = item["function"] or f"0x{item['pc']:08x}"
                print(f"  {item['observed_span_seconds']:7.3f}s {item['sample_count']:5d} samples  "
                      f"max gap {item['max_sample_gap_seconds']:.3f}s  {label}")
        else:
            print(f"No routine-stall candidates >= {stall_threshold:.2f}s observed.")
        if report["progress_stalls"]:
            print("Progress counter stalls (coarse; routine samples are correlated by time):")
            for item in report["progress_stalls"][:10]:
                dominant = ", ".join(f"{row['function']} {row['percent']:.0f}%"
                                     for row in item["dominant_routines"][:3]) or "no PC samples"
                print(f"  {item['duration_seconds']:7.3f}s {item['counter']}: {dominant}")
        else:
            print(f"No progress counter stalls >= {stall_threshold:.2f}s observed.")
        for name, value in report["progress"].items():
            rate = value.get("per_wall_second")
            if rate is not None:
                print(f"{name}: {rate:.2f} increments / wall second")
        print(f"Report and PNGs: {destination}")
        for warning in report["warnings"]:
            print(f"Note: {warning}")
    return 0 if report["sample_count"] else 2
