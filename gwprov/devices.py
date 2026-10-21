"""Unified inventory for local GWemu processes and connected physical probes."""

from __future__ import annotations

import json
from pathlib import Path
import time


def device_rows(profile: str | None = None, *, refresh: bool = False) -> list[dict]:
    from .gwemu_manager import _application_state, _process_scan_restriction, instances

    rows = []
    vms = instances()
    restriction = _process_scan_restriction()
    if not vms and restriction:
        rows.append(
            {
                "id": "gwemu:*",
                "kind": "gwemu",
                "backend": "process scan",
                "name": "GWemu process inventory",
                "status": "unknown",
                "application": "Unknown",
                "detail": f"Cannot confirm VM state: process scan restricted ({restriction}).",
            }
        )
    for vm in vms:
        try:
            application = _application_state(vm)
            app_detail = None
        except (OSError, RuntimeError, ValueError) as error:
            application = "Unknown"
            app_detail = str(error)
        adapter_endpoints = []
        if vm.get("gdbEndpoint"):
            adapter_endpoints.append(
                {
                    "protocol": "gdb",
                    "transport": vm.get("gdbTransport"),
                    "endpoint": vm["gdbEndpoint"],
                    "pid": vm.get("gdbPid"),
                    "listening": vm.get("gdbListener"),
                }
            )
        if vm.get("qmpSocket") and not str(vm["qmpSocket"]).startswith("gwprov://"):
            adapter_endpoints.append(
                {
                    "protocol": "qmp",
                    "transport": vm.get("qmpTransport"),
                    "endpoint": vm["qmpSocket"],
                    "pid": vm.get("qmpPid"),
                    "listening": vm.get("qmpListener"),
                }
            )
        rows.append(
            {
                "id": f"gwemu:{vm['pid']}",
                "kind": "gwemu",
                "backend": (
                    "GWProv daemon"
                    if str(vm.get("qmpSocket", "")).startswith("gwprov://")
                    else "QMP"
                ),
                "name": vm.get("profile") or f"GWemu pid {vm['pid']}",
                "profile": vm.get("profile"),
                "gdbPort": vm.get("gdbPort"),
                "gdbEndpoint": vm.get("gdbEndpoint"),
                "gdbTransport": vm.get("gdbTransport"),
                "gdbPid": vm.get("gdbPid"),
                "gdbListener": vm.get("gdbListener"),
                "qmpSocket": vm.get("qmpSocket"),
                "qmpTransport": vm.get("qmpTransport"),
                "qmpPid": vm.get("qmpPid"),
                "qmpListener": vm.get("qmpListener"),
                "tcpListeners": vm.get("tcpListeners"),
                "unixListeners": vm.get("unixListeners"),
                "adapterEndpoints": adapter_endpoints,
                "status": vm["status"],
                "application": application,
                "pid": vm["pid"],
                "display": vm.get("display"),
                "qmpStatus": vm.get("qmpStatus"),
                "timing": vm.get("timing"),
                "binaryIdentity": vm.get("binaryIdentity"),
                "created": vm.get("created"),
                # GWemu state was queried from QMP while building this row.
                "polledAt": time.time(),
                "detail": vm.get("stateDetail") or app_detail,
            }
        )
    from .daemon import device_snapshot

    daemon_error = None
    try:
        hardware = device_snapshot(refresh=refresh)
    except (OSError, RuntimeError, ValueError) as error:
        # The CLI never opens an adapter as an inventory fallback. Report that
        # the daemon could not provide state instead of racing its connection.
        hardware = []
        daemon_error = f"Daemon state unavailable: {error}"
    rows.extend(hardware)
    # The daemon owns periodic raw-GDB polling. Keep this registry fallback for
    # cases where daemon discovery is unavailable, but don't duplicate rows.
    from .adapters import list_gdb
    from .target_leases import lease_owner, operation_is_busy

    fallback_gdb_rows = 0
    for adapter in list_gdb():
        endpoint = f"{adapter['host']}:{adapter['port']}"
        device_id = f"gdb:{endpoint}"
        if any(row.get("id") == device_id for row in rows):
            continue
        remote_url = str(adapter["url"])
        owner = lease_owner(f"remote-gdb:{remote_url}")
        status = "busy" if owner and operation_is_busy(owner) else "unknown"
        if daemon_error:
            detail = daemon_error
            if status == "busy":
                detail = f"GWProv owns target through {owner['operation']}; {detail}"
        elif status == "busy":
            detail = f"GWProv owns target through {owner['operation']}"
        else:
            detail = "Waiting for daemon poll"
        rows.append(
            {
                "id": device_id,
                "kind": "remote-gdb",
                "backend": "Remote GDB",
                "name": adapter["name"],
                "displayName": adapter["name"],
                "gdbEndpoint": endpoint,
                "gdbHost": adapter["host"],
                "gdbPort": adapter["port"],
                "gdbUrl": remote_url,
                "remoteGdb": True,
                "targetProfile": adapter.get("targetProfile"),
                "status": status,
                "application": "Unknown",
                "installed": "Unknown",
                "detail": detail,
            }
        )
        fallback_gdb_rows += 1
    if daemon_error and not fallback_gdb_rows:
        rows.append(
            {
                "id": "hardware:*",
                "kind": "hardware",
                "backend": "GWProv daemon",
                "name": "Hardware inventory",
                "status": "unknown",
                "application": "Unknown",
                "installed": "Unknown",
                "detail": daemon_error,
            }
        )
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
    return sorted(
        rows,
        key=lambda row: (
            row["kind"],
            str(row.get("displayName", row.get("name", ""))).casefold(),
            row["id"],
        ),
    )


def show_devices(
    *,
    output: str = "text",
    profile: str | None = None,
    refresh: bool = False,
    no_pager: bool = False,
) -> int:
    rows = device_rows(profile=profile, refresh=refresh)
    if output == "json":
        print(json.dumps(rows, indent=2))
        return 2 if any(row["status"] == "unknown" for row in rows) else 0
    if not rows:
        print("No GWemu instances or PyOCD probes detected.")
        print(
            "Probe enumeration requires PyOCD with working USB access; use `pyocd list -p` to diagnose probe access."
        )
        return 0
    from .cli.text import print_process_list

    print_process_list(rows, title="GWProv devices", no_pager=no_pager)
    return 2 if any(row["status"] == "unknown" for row in rows) else 0


def device_identifiers(*, include_names: bool = False) -> list[str]:
    """Return completion-safe device selectors without QMP or target traffic."""
    import psutil
    from pathlib import Path
    from .adapters import list_gdb, list_remote

    identifiers = set()
    for process in psutil.process_iter(["pid", "cmdline"]):
        args = process.info.get("cmdline") or []
        executable = Path(args[0]).name.lower() if args else ""
        if (
            executable in {"gwemu", "gwemu.exe"} or executable.startswith("qemu-system")
        ) and "gnw-h7b0" in " ".join(args):
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
    for row in list_gdb():
        identifiers.add(f"gdb:{row['host']}:{row['port']}")
        identifiers.add(row["name"])
    if include_names:
        # Configured display names are completion-safe aliases. Reading this
        # registry does not poll QMP or open a hardware debug session.
        from .device_assignments import assignments

        identifiers.update(
            values["name"] for values in assignments().values() if values.get("name")
        )
    return sorted(identifiers, key=str.casefold)
