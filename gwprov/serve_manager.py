"""Lifecycle management for local gnwmanager-compatible WebSocket bridges."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil

from .daemon_ipc import runtime_directory
from .ports import find_free_port, port_is_available, validate_requested_port


def _registry_path() -> Path:
    return runtime_directory() / "served-devices.json"


def _read_registry() -> dict[str, dict]:
    try:
        value = json.loads(_registry_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise RuntimeError(f"cannot read serve registry: {error}") from error
    if not isinstance(value, dict) or not isinstance(value.get("devices", {}), dict):
        raise RuntimeError(f"invalid serve registry: {_registry_path()}")
    return value.get("devices", {})


def _write_registry(rows: dict[str, dict]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"schemaVersion": 1, "devices": rows}, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _worker_alive(row: dict) -> bool:
    return _worker_state(row) != "stale"


def _worker_state(row: dict) -> str:
    pid = row.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return "stale"
    try:
        process = psutil.Process(pid)
        return "serving" if "gwprov.serve_worker" in " ".join(process.cmdline()) else "stale"
    except psutil.NoSuchProcess:
        return "stale"
    except (psutil.AccessDenied, OSError):
        return "unknown"


def status() -> list[dict]:
    """List registered bridge workers and report their current process state."""
    rows = _read_registry()
    return [
        {
            "device": device_id,
            "name": row.get("name") or device_id,
            "kind": row.get("kind", "unknown"),
            "state": _worker_state(row),
            "url": row.get("url"),
            "bind": row.get("bind"),
            "port": row.get("port"),
            "pid": row.get("pid"),
            "started": row.get("started"),
            "log": row.get("log"),
        }
        for device_id, row in sorted(rows.items())
    ]


def start(
    device: dict,
    *,
    bind: str = "127.0.0.1",
    port: int | None = None,
    gdb_endpoint: str | None = None,
) -> dict:
    device_id = str(device.get("id", ""))
    if not device_id:
        raise ValueError("device has no stable ID")
    rows = _read_registry()
    existing = rows.get(device_id)
    if existing and _worker_alive(existing):
        raise ValueError(
            f"device is already served at ws://{existing['bind']}:{existing['port']}/gdb"
        )
    if existing:
        rows.pop(device_id, None)
    selected_port = (
        find_free_port(8765, host=bind)
        if port is None
        else validate_requested_port(port, host=bind)
    )
    if device.get("kind") == "gwemu":
        endpoint = gdb_endpoint or device.get("gdbEndpoint")
        transport = device.get("gdbTransport", "tcp")
        host, gdb_port = "127.0.0.1", device.get("gdbPort")
        if not endpoint and gdb_port:
            endpoint = f"127.0.0.1:{gdb_port}"
        if gdb_endpoint:
            if gdb_endpoint.startswith("unix:"):
                transport, endpoint = "unix", gdb_endpoint.removeprefix("unix:")
                gdb_port = None
            else:
                transport = "tcp"
                endpoint = gdb_endpoint.removeprefix("tcp://")
                if ":" not in endpoint:
                    raise ValueError(
                        "--gdb-endpoint must be HOST:PORT, tcp://HOST:PORT, or unix:PATH"
                    )
                host, raw_port = endpoint.rsplit(":", 1)
                try:
                    gdb_port = int(raw_port)
                except ValueError as error:
                    raise ValueError("GDB TCP endpoint port must be an integer") from error
                if not 1 <= gdb_port <= 65535:
                    raise ValueError("GDB TCP endpoint port must be from 1 to 65535")
        elif endpoint and transport == "tcp":
            endpoint_host, _, _ = endpoint.rpartition(":")
            if endpoint_host and endpoint_host not in {"*", "0.0.0.0", "::"}:
                host = endpoint_host
        if not endpoint:
            candidates = device.get("tcpListeners") or device.get("unixListeners") or []
            hint = (
                f" Candidate listeners for pid {device.get('pid')}: {', '.join(candidates)}."
                if candidates
                else ""
            )
            raise ValueError(
                "GWemu has no identified GDB endpoint; use --gdb-endpoint to select "
                f"one from its process-owned listeners.{hint}"
            )
        target = {
            "kind": "gwemu",
            "host": host,
            "port": gdb_port,
            "endpoint": endpoint,
            "transport": transport,
        }
    elif device.get("kind") == "hardware" and device.get("probeId"):
        target = {
            "kind": "hardware",
            "probeId": str(device["probeId"]),
            "adapter": device.get("adapter"),
        }
    elif device.get("kind") == "remote-gdb":
        host = device.get("gdbHost")
        gdb_port = device.get("gdbPort")
        if not isinstance(host, str) or not host:
            raise ValueError("remote GDB device has no host")
        if not isinstance(gdb_port, int) or not 1 <= gdb_port <= 65535:
            raise ValueError("remote GDB device has an invalid port")
        target = {
            "kind": "remote-gdb",
            "host": host,
            "port": gdb_port,
            "url": device.get("gdbUrl") or f"gdb://{host}:{gdb_port}",
        }
    else:
        raise ValueError(
            f"serve does not support this device backend: {device.get('backend', 'unknown')}"
        )

    log_path = runtime_directory() / f"serve-{selected_port}.log"
    command = [
        sys.executable,
        "-m",
        "gwprov.serve_worker",
        "--target",
        json.dumps(target, separators=(",", ":")),
        "--bind",
        bind,
        "--port",
        str(selected_port),
    ]
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name != "nt"),
        )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            tail = log_path.read_text(errors="replace")[-2000:]
            raise RuntimeError(f"gnwmanager WebSocket server failed to start: {tail.strip()}")
        if not port_is_available(selected_port, host=bind):
            break
        time.sleep(0.05)
    else:
        process.terminate()
        raise TimeoutError(f"serve worker did not open {bind}:{selected_port}")

    row = {
        "pid": process.pid,
        "name": device.get("displayName") or device.get("name") or device_id,
        "kind": device.get("kind", "unknown"),
        "bind": bind,
        "port": selected_port,
        "url": f"ws://{bind}:{selected_port}/gdb",
        "log": str(log_path),
        "started": time.time(),
    }
    rows[device_id] = row
    _write_registry(rows)
    return {"device": device_id, **row}


def stop(device_id: str) -> dict:
    rows = _read_registry()
    row = rows.get(device_id)
    if row is None:
        raise ValueError(f"device has no GWProv serve session: {device_id}")
    if _worker_alive(row):
        process = psutil.Process(row["pid"])
        process.terminate()
        try:
            process.wait(timeout=5)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    rows.pop(device_id, None)
    _write_registry(rows)
    return {"device": device_id, **row}
