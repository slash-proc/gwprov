"""Unified inventory for local GWemu processes and connected physical probes."""
from __future__ import annotations

import json
from pathlib import Path


def device_rows(profile: str | None = None, *, refresh: bool = False) -> list[dict]:
    from .gwemu_manager import _application_state, _process_scan_restriction, instances

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
                     "profile": vm.get("profile"), "gdbPort": vm.get("gdbPort"),
                     "qmpSocket": vm.get("qmpSocket"),
                     "status": vm["status"], "application": application,
                     "pid": vm["pid"], "display": vm.get("display"),
                     "detail": vm.get("stateDetail") or app_detail})
    from .daemon import device_snapshot
    try:
        hardware = device_snapshot(refresh=refresh)
    except (OSError, RuntimeError, ValueError) as error:
        # The CLI never opens an adapter as an inventory fallback. Report that
        # the daemon could not provide state instead of racing its connection.
        hardware = [{"id": "hardware:*", "kind": "hardware", "backend": "GWProv daemon",
                     "name": "Hardware inventory", "status": "unknown",
                     "application": "Unknown", "installed": "Unknown",
                     "detail": f"Daemon state unavailable: {error}"}]
    rows.extend(hardware)
    if (active_device and active_device.startswith("remote:")
            and not any(row.get("id") == active_device for row in hardware)):
        uri = active_device.removeprefix("remote:")
        try:
            from .adapters import list_remote
            configured = next((row for row in list_remote() if row.get("url") == uri), None)
        except (OSError, RuntimeError, ValueError):
            configured = None
        rows.append({"id": active_device, "kind": "hardware",
                     "backend": "gnwmanager WebSocket",
                     "name": configured.get("name") if configured else uri,
                     "status": "unknown", "application": "Unknown", "installed": "Unknown",
                     "detail": "Selected remote adapter is not present in the daemon inventory",
                     "adapterName": configured.get("name") if configured else uri,
                     "remoteOrigin": (configured.get("origin") if configured
                                      else get_active_origin())})
    from .device_assignments import assignments
    assigned = assignments()
    gwemu_groups: dict[str, list[dict]] = {}
    for row in rows:
        values = assigned.get(row["id"], {})
        if values.get("profile"):
            row["assignedProfile"] = values["profile"]
        if values.get("sdcard"):
            row["sdCard"] = values["sdcard"]
        if row.get("kind") == "gwemu":
            profile = str(row.get("profile") or "")
            gwemu_groups.setdefault(profile, []).append(row)
    for profile, group in gwemu_groups.items():
        base = Path(profile).name if profile else "gwemu"
        for index, row in enumerate(sorted(group, key=lambda item: int(item.get("pid") or 0)), 1):
            default_name = f"{base}-{index:02d}"
            row["displayName"] = assigned.get(row["id"], {}).get("name") or default_name
    for row in rows:
        if row.get("kind") != "gwemu":
            values = assigned.get(row["id"], {})
            profile = values.get("profile")
            default_name = Path(profile).name if profile else row.get("name") or row["id"]
            row["displayName"] = values.get("name") or default_name
    return sorted(rows, key=lambda row: (row["kind"],
                                         str(row.get("displayName", row.get("name", ""))).casefold(),
                                         row["id"]))


def show_devices(*, output: str = "text", profile: str | None = None,
                 refresh: bool = False,
                 no_pager: bool = False) -> int:
    rows = device_rows(profile=profile, refresh=refresh)
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
