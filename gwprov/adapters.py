"""Per-user registry of remote gnwmanager adapters."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse


def _config_path() -> Path:
    override = os.environ.get("GWPROV_CONFIG_DIR")
    if override:
        root = Path(override).expanduser()
    elif os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "gwprov"
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support/gwprov"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "gwprov"
    return root / "adapters.json"


def normalize_remote_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname or parsed.path != "/gdb":
        raise ValueError("remote adapter URL must be ws[s]://host:port/gdb")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("remote adapter URL cannot contain credentials, query, or fragment")
    try:
        if parsed.port is None:
            raise ValueError("remote adapter URL must include a port")
    except ValueError as exc:
        raise ValueError(f"invalid remote adapter URL: {exc}") from exc
    return value.rstrip("/")


def list_remote() -> list[dict[str, str | None]]:
    path = _config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read adapter registry {path}: {exc}") from exc
    rows = data.get("remote", []) if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"invalid adapter registry format in {path}")
    result = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("url"), str):
            raise RuntimeError(f"invalid remote adapter entry in {path}")
        result.append({"name": row["name"], "url": normalize_remote_url(row["url"]),
                       "origin": row.get("origin") if isinstance(row.get("origin"), str) else None})
    return result


def inventory() -> list[dict]:
    """List adapter identities without opening target debug sessions."""
    from .backends import enumerate_local_probes
    from .target_leases import (adapter_for_probe, external_local_owner,
                                lease_owner)

    external = external_local_owner()
    rows = []
    for probe in enumerate_local_probes():
        adapter_type = adapter_for_probe(f"{probe['vendor']} {probe['name']}") or probe["backend"]
        owner = (lease_owner(f"probe:{probe['id']}")
                 or lease_owner(f"adapter:{adapter_type}"))
        if owner is None and external and external.get("adapter") in (None, adapter_type):
            owner = external
        detail = None
        if owner:
            detail = (f"interrupted {owner.get('phase', 'hardware operation')}; "
                      "run `gwprov device recover` when idle"
                      if owner.get("recovery_required") else owner.get("operation"))
        rows.append({"id": f"probe:{probe['id']}", "name": probe["name"],
                     "adapter": adapter_type, "type": "local", "state": "busy" if owner else "available",
                     "detail": detail})
    for remote in list_remote():
        device_id = f"remote:{remote['url']}"
        owner = lease_owner(f"remote:{remote['url']}")
        detail = None
        if owner:
            detail = (f"interrupted {owner.get('phase', 'hardware operation')}; "
                      "run `gwprov device recover` when idle"
                      if owner.get("recovery_required") else owner.get("operation"))
        rows.append({"id": device_id, "name": remote["name"], "adapter": "gnwmanager WebSocket",
                     "type": "remote", "state": "busy" if owner else "registered",
                     "url": remote["url"], "origin": remote["origin"],
                     "detail": detail})
    return sorted(rows, key=lambda row: (row["type"], row["name"].casefold(), row["id"]))


def add_remote(name: str, url: str, *, origin: str | None = None) -> dict[str, str | None]:
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ValueError("adapter name must start with a letter or number and contain only letters, numbers, dot, underscore, or hyphen")
    if origin is not None and any(char in origin for char in "\r\n"):
        raise ValueError("adapter Origin cannot contain line breaks")
    url = normalize_remote_url(url)
    rows = list_remote()
    if any(row["name"].casefold() == name.casefold() for row in rows):
        raise ValueError(f"remote adapter name already exists: {name}")
    if any(row["url"] == url for row in rows):
        raise ValueError(f"remote adapter URL is already registered: {url}")
    rows.append({"name": name, "url": url, "origin": origin})
    _write(rows)
    return rows[-1]


def remove_remote(name_or_url: str) -> dict[str, str | None]:
    rows = list_remote()
    match = next((row for row in rows if row["name"] == name_or_url or row["url"] == name_or_url), None)
    if match is None:
        raise ValueError(f"remote adapter not found: {name_or_url}")
    _write([row for row in rows if row != match])
    return match


def _write(rows: list[dict[str, str | None]]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"schemaVersion": 1, "remote": rows}, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)
