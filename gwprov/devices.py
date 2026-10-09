"""Unified inventory for local GWemu processes and connected physical probes."""
from __future__ import annotations

import json


def device_rows(profile: str | None = None) -> list[dict]:
    from .gwemu_manager import _application_state, _process_scan_restriction, instances
    from .backends import SelectedPyOCDBackend, enumerate_local_probes
    from .target_leases import (DeviceBusyError, adapter_for_probe,
                                external_local_owner, lease_owner)
    symbol_paths = []
    firmware = None
    app_elfs = []
    if profile:
        from .debug_shell import SymbolTable
        from .profiles import DeviceProfile
        device = DeviceProfile.load(profile)
        firmware = device.root / "debug" / "retro-go-debug.elf"
        if firmware.is_file():
            symbol_paths.append(firmware)
        app_elfs = sorted((device.root / "debug" / "apps").rglob("*.elf"))
        symbol_paths.extend(app_elfs)

    rows = []
    vms = instances()
    restriction = _process_scan_restriction()
    if not vms and restriction:
        rows.append({"id": "gwemu:*", "kind": "gwemu", "backend": "process scan",
                     "name": "GWemu process inventory", "status": "unknown",
                     "application": "Unknown",
                     "detail": f"Cannot confirm VM state: process scan restricted ({restriction})."})
    for vm in vms:
        try:
            application = _application_state(vm)
            app_detail = None
        except (OSError, RuntimeError, ValueError) as error:
            application = "Unknown"
            app_detail = str(error)
        rows.append({"id": f"gwemu:{vm['pid']}", "kind": "gwemu", "backend": "qmp",
                     "name": vm.get("profile") or f"GWemu pid {vm['pid']}",
                     "status": vm["status"], "application": application,
                     "pid": vm["pid"], "detail": vm.get("stateDetail") or app_detail})
    try:
        probes = enumerate_local_probes()
        probe_error = None
    except RuntimeError as error:
        probes = []
        probe_error = str(error)
    external_owner = external_local_owner()
    for probe in probes:
        adapter = adapter_for_probe(f"{probe['vendor']} {probe['name']}")
        adapter_lease = lease_owner(f"adapter:{adapter}") if adapter else None
        matching_external = (external_owner if external_owner and
                             (adapter is None or external_owner.get("adapter") is None or
                              external_owner.get("adapter") == adapter) else None)
        owner = lease_owner(f"probe:{probe['id']}") or adapter_lease or matching_external
        if owner:
            pid = owner.get("pid")
            detail = f"Target traffic skipped: {owner.get('operation', 'another session')} owns it"
            if pid:
                detail += f" (pid {pid})"
            rows.append({"id": f"probe:{probe['id']}", "kind": "hardware",
                         "backend": probe["backend"], "name": probe["name"],
                         "probeId": probe["id"], "vendor": probe["vendor"],
                         "status": "busy", "application": "Unknown", "detail": detail,
                         "busyOwner": owner})
            continue
        backend = None
        application = "Unknown"
        busy_owner = None
        try:
            backend = SelectedPyOCDBackend(probe["id"], operation="gwprov ps", lease_wait=0,
                                           probe_adapter=adapter)
            backend.open()
            dhcsr = backend.read_uint32(0xE000EDF0)
            status = "halted" if dhcsr & (1 << 17) else "running"
            target_name = getattr(backend.target, "part_number", None) or probe["name"]
            detail = "Pass --profile with matching firmware and app symbols to classify application state."
            if symbol_paths and firmware is not None:
                was_running = status == "running"
                halted_by_us = False
                try:
                    if was_running:
                        backend.halt()
                        after_halt = backend.read_uint32(0xE000EDF0)
                        if not after_halt & (1 << 17):
                            raise RuntimeError("target did not enter halted state for symbol snapshot")
                        halted_by_us = True
                    symbol_table = SymbolTable()
                    for elf in symbol_paths:
                        symbol_table.load(elf)
                    symbol_table.rebase_from_runtime_pointers(backend.read_memory)
                    registers = {f"r{index}": backend.read_register(f"r{index}")
                                 for index in range(13)}
                    registers.update({name: backend.read_register(name)
                                      for name in ("sp", "lr", "pc")})
                    from .gwemu_manager import _application_state_from_target
                    application = _application_state_from_target(
                        symbol_table, firmware, app_elfs, backend.read_memory, registers)
                    detail = ("Symbolized Retro-Go/app state; target was briefly halted and resumed."
                              if was_running else "Symbolized Retro-Go/app state; target was already halted.")
                except Exception as error:
                    detail = f"CPU state read; application state read failed: {error}"
                finally:
                    if halted_by_us:
                        try:
                            backend.resume()
                            after = backend.read_uint32(0xE000EDF0)
                            status = "halted" if after & (1 << 17) else "running"
                            if status != "running":
                                detail += " Target did not resume; status reflects the final DHCSR read."
                        except Exception as error:
                            status = "unknown"
                            detail += f" Target resume/state verification failed: {error}"
        except DeviceBusyError as error:
            status = "busy"
            detail = str(error)
            target_name = probe["name"]
            busy_owner = error.owner
        except Exception as error:
            status = "unknown"
            detail = f"Probe found, target state could not be read: {error}"
            target_name = probe["name"]
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass
        row = {"id": f"probe:{probe['id']}", "kind": "hardware", "backend": probe["backend"],
               "name": target_name, "probeId": probe["id"], "vendor": probe["vendor"],
               "status": status, "application": application, "detail": detail}
        if busy_owner:
            row["busyOwner"] = busy_owner
        rows.append(row)
    if probe_error:
        rows.append({"id": "probe:*", "kind": "hardware", "backend": "pyocd",
                     "name": "Probe inventory", "status": "unknown", "application": "Unknown",
                     "detail": probe_error})
    return sorted(rows, key=lambda row: (row["kind"], row["name"].casefold(), row["id"]))


def show_devices(*, output: str = "text", profile: str | None = None) -> int:
    rows = device_rows(profile=profile)
    if output == "json":
        print(json.dumps(rows, indent=2))
        return 2 if any(row["status"] == "unknown" for row in rows) else 0
    if not rows:
        print("No GWemu instances or PyOCD probes detected.")
        print("Probe enumeration requires PyOCD with working USB access; use `pyocd list -p` to diagnose probe access.")
        return 0
    print("ID                         KIND       SYSTEM       APPLICATION       NAME")
    for row in rows:
        print(f"{row['id']:<26} {row['kind']:<10} {row['status']:<12} "
              f"{row['application']:<17} {row['name']}")
        if row.get("detail"):
            print(f"  {row['detail']}")
    return 2 if any(row["status"] == "unknown" for row in rows) else 0
