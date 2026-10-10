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
    from .active_device import get_active
    from .active_device import get_active_origin
    active_device = get_active()
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
        rows.append({"id": f"gwemu:{vm['pid']}", "kind": "gwemu",
                     "backend": "GWProv daemon" if str(vm.get("qmpSocket", "")).startswith("gwprov://") else "QMP",
                     "name": vm.get("profile") or f"GWemu pid {vm['pid']}",
                     "status": vm["status"], "application": application,
                     "pid": vm["pid"], "display": vm.get("display"),
                     "detail": vm.get("stateDetail") or app_detail})
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
            if owner.get("recovery_required"):
                if owner.get("lease_active"):
                    detail = ("Target traffic skipped: "
                              f"{owner.get('phase', 'hardware operation')} is in progress")
                else:
                    detail = ("Target traffic skipped: the previous session left the "
                              f"{owner.get('phase', 'hardware operation')} active; "
                              "run `gwprov device recover` after it is idle")
            else:
                detail = f"Target traffic skipped: {owner.get('operation', 'another session')} owns it"
            if pid:
                active_owner = owner.get("lease_active", not owner.get("recovery_required"))
                detail += (f" (pid {pid})" if active_owner else
                           f" (last session pid {pid})")
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
    from .adapters import list_remote
    remotes = list_remote()
    remote_by_url = {row["url"]: row for row in remotes}
    if active_device and active_device.startswith("remote:"):
        uri = active_device.removeprefix("remote:")
        remote_by_url.setdefault(uri, {"name": uri.split("/", 3)[2],
                                       "url": uri, "origin": get_active_origin()})
    for remote in remote_by_url.values():
        uri = remote["url"]
        remote_id = f"remote:{uri}"
        owner = lease_owner(f"remote:{uri}")
        if owner:
            detail = (("gnwmanager RAM service operation is in progress"
                       if owner.get("lease_active") else
                       "Previous session left the gnwmanager RAM service active; "
                       "run `gwprov device recover` after its mailbox is idle")
                      if owner.get("recovery_required") else
                      f"Target traffic skipped: {owner.get('operation', 'another session')} owns it")
            rows.append({"id": remote_id, "kind": "hardware", "backend": "gnwmanager WebSocket",
                         "name": remote["name"], "status": "busy", "application": "Unknown",
                         "detail": detail,
                         "adapterName": remote["name"], "remoteOrigin": remote["origin"]})
        else:
            backend = None
            try:
                from .backends import WebSocketBackend
                backend = WebSocketBackend(uri, origin=remote["origin"],
                                           operation="gwprov device inventory", lease_wait=0)
                backend.open()
                dhcsr = backend.read_uint32(0xE000EDF0)
                status = "halted" if dhcsr & (1 << 17) else "running"
                detail = "Remote device; pass a matching profile to resolve its Application state."
            except Exception as error:
                status = "unknown"
                detail = f"Remote device state could not be read: {error}"
            finally:
                if backend is not None:
                    try: backend.close()
                    except Exception: pass
            rows.append({"id": remote_id, "kind": "hardware", "backend": "gnwmanager WebSocket",
                         "name": remote["name"], "status": status, "application": "Unknown",
                         "detail": detail, "adapterName": remote["name"], "remoteOrigin": remote["origin"]})
    if probe_error:
        rows.append({"id": "probe:*", "kind": "hardware", "backend": "pyocd",
                     "name": "Probe inventory", "status": "unknown", "application": "Unknown",
                     "detail": probe_error})
    from .device_assignments import assignments
    assigned = assignments()
    for row in rows:
        values = assigned.get(row["id"], {})
        if values.get("profile"):
            row["assignedProfile"] = values["profile"]
        if values.get("sdcard"):
            row["sdCard"] = values["sdcard"]
    return sorted(rows, key=lambda row: (row["kind"], row["name"].casefold(), row["id"]))


def show_devices(*, output: str = "text", profile: str | None = None,
                 no_pager: bool = False) -> int:
    rows = device_rows(profile=profile)
    if output == "json":
        print(json.dumps(rows, indent=2))
        return 2 if any(row["status"] == "unknown" for row in rows) else 0
    if not rows:
        print("No GWemu instances or PyOCD probes detected.")
        print("Probe enumeration requires PyOCD with working USB access; use `pyocd list -p` to diagnose probe access.")
        return 0
    from .cli.text import print_process_list
    print_process_list(rows, title="GWProv devices", no_pager=no_pager)
    return 2 if any(row["status"] == "unknown" for row in rows) else 0


def device_identifiers() -> list[str]:
    """Return completion-safe device selectors without QMP or target traffic."""
    import psutil
    from pathlib import Path
    from .adapters import list_remote

    identifiers = set()
    for process in psutil.process_iter(["pid", "cmdline"]):
        args = process.info.get("cmdline") or []
        executable = Path(args[0]).name.lower() if args else ""
        if ((executable in {"gwemu", "gwemu.exe"} or executable.startswith("qemu-system"))
                and "gnw-h7b0" in " ".join(args)):
            pid = process.info.get("pid")
            if pid:
                identifiers.add(f"gwemu:{pid}")
    try:
        from .backends import enumerate_local_probes
        identifiers.update(f"probe:{row['id']}" for row in enumerate_local_probes())
    except (RuntimeError, OSError):
        pass
    for row in list_remote():
        identifiers.add(f"remote:{row['url']}")
        identifiers.add(row["name"])
    return sorted(identifiers, key=str.casefold)
