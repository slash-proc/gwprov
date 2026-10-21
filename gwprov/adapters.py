"""Per-user registry of local and remote debug adapters."""

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


def normalize_gdb_url(value: str) -> str:
    """Validate and canonicalize a raw GDB Remote Serial Protocol endpoint."""
    parsed = urlparse(value)
    if parsed.scheme != "gdb" or not parsed.hostname:
        raise ValueError("remote GDB endpoint must look like gdb://HOST:PORT")
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("remote GDB endpoint cannot contain credentials, path, query, or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid remote GDB endpoint: {exc}") from exc
    if port is None:
        raise ValueError("remote GDB endpoint must include a port")
    if not 1 <= port <= 65535:
        raise ValueError("remote GDB port must be from 1 to 65535")
    host = parsed.hostname.casefold()
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return f"gdb://{netloc}"


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
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not isinstance(row.get("url"), str)
        ):
            raise RuntimeError(f"invalid remote adapter entry in {path}")
        item = {
            "name": row["name"],
            "url": normalize_remote_url(row["url"]),
            "origin": row.get("origin") if isinstance(row.get("origin"), str) else None,
        }
        profile = _target_profile(row.get("targetProfile"))
        if profile:
            item["targetProfile"] = profile
        result.append(item)
    return result


def list_gdb() -> list[dict[str, str | int]]:
    path = _config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read adapter registry {path}: {exc}") from exc
    rows = data.get("gdb", []) if isinstance(data, dict) else []
    if not isinstance(rows, list):
        raise RuntimeError(f"invalid remote GDB registry format in {path}")
    result = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not isinstance(row.get("url"), str)
        ):
            raise RuntimeError(f"invalid remote GDB entry in {path}")
        url = normalize_gdb_url(row["url"])
        parsed = urlparse(url)
        item = {
            "name": row["name"],
            "url": url,
            "host": parsed.hostname or "",
            "port": parsed.port or 0,
        }
        profile = _target_profile(row.get("targetProfile"))
        if profile:
            item["targetProfile"] = profile
        result.append(item)
    return result


def inventory() -> list[dict]:
    """List adapter identities without opening target debug sessions."""
    from .backends import enumerate_local_probes
    from .target_leases import adapter_for_probe, external_local_owner, lease_owner

    rows = []

    def owner_state(owner: dict | None, available: str) -> str:
        if owner is None:
            return available
        active = bool(owner.get("lease_active")) or str(owner.get("key", "")).startswith(
            "external-"
        )
        if active:
            from .target_leases import operation_is_busy

            return "busy" if operation_is_busy(owner) else available
        return available

    try:
        local_probes = enumerate_local_probes()
    except (ImportError, OSError, RuntimeError) as error:
        # A missing optional USB/PyOCD stack must not hide registered remote
        # transports; they have no dependency on local probe enumeration.
        local_probes = []
        rows.append(
            {
                "id": "probe:*",
                "name": "Local probe discovery",
                "adapter": "PyOCD / USB",
                "type": "local discovery",
                "state": "unknown",
                "detail": f"Local probes unavailable: {error}",
            }
        )
    for probe in local_probes:
        adapter_type = adapter_for_probe(f"{probe['vendor']} {probe['name']}") or probe["backend"]
        external = external_local_owner(adapter_type)
        owner = lease_owner(f"probe:{probe['id']}") or lease_owner(f"adapter:{adapter_type}")
        if external and external.get("adapter") in (None, adapter_type) and owner is None:
            owner = external
        detail = (
            owner.get("operation")
            if owner
            and (owner.get("lease_active") or str(owner.get("key", "")).startswith("external-"))
            else None
        )
        if detail and owner.get("pid"):
            detail = f"{detail} (pid {owner['pid']})"
        rows.append(
            {
                "id": f"probe:{probe['id']}",
                "name": probe["name"],
                "adapter": adapter_type,
                "type": "local",
                "state": owner_state(owner, "available"),
                "detail": detail,
                "owner": owner,
            }
        )
    for remote in list_remote():
        device_id = f"remote:{remote['url']}"
        owner = lease_owner(f"remote:{remote['url']}")
        detail = owner.get("operation") if owner and owner.get("lease_active") else None
        rows.append(
            {
                "id": device_id,
                "name": remote["name"],
                "adapter": "gnwmanager WebSocket",
                "type": "remote",
                "state": owner_state(owner, "registered"),
                "url": remote["url"],
                "origin": remote["origin"],
                "targetProfile": remote.get("targetProfile"),
                "detail": detail,
            }
        )
    for remote in list_gdb():
        key = f"remote-gdb:{remote['url']}"
        owner = lease_owner(key)
        detail = owner.get("operation") if owner and owner.get("lease_active") else None
        rows.append(
            {
                "id": f"gdb:{remote['url'].removeprefix('gdb://')}",
                "name": remote["name"],
                "adapter": "GDB Remote Serial Protocol",
                "type": "remote GDB",
                "state": owner_state(owner, "registered"),
                "url": remote["url"],
                "targetProfile": remote.get("targetProfile"),
                "detail": detail,
            }
        )
    return sorted(rows, key=lambda row: (row["type"], row["name"].casefold(), row["id"]))


def add_remote(
    name: str,
    url: str,
    *,
    origin: str | None = None,
    target_profile: str | None = None,
) -> dict[str, str | None]:
    name = name.strip()
    _validate_name(name)
    if origin is not None and any(char in origin for char in "\r\n"):
        raise ValueError("adapter Origin cannot contain line breaks")
    url = normalize_remote_url(url)
    rows = list_remote()
    gdb_rows = list_gdb()
    if any(row["name"].casefold() == name.casefold() for row in [*rows, *gdb_rows]):
        raise ValueError(f"remote adapter name already exists: {name}")
    if any(row["url"] == url for row in rows):
        raise ValueError(f"remote adapter URL is already registered: {url}")
    row = {"name": name, "url": url, "origin": origin}
    profile = _target_profile(target_profile)
    if profile:
        row["targetProfile"] = profile
    rows.append(row)
    _write(rows, gdb_rows)
    return rows[-1]


def add_gdb(
    name: str, url: str, *, target_profile: str | None = None
) -> dict[str, str | int | str | None]:
    name = name.strip()
    _validate_name(name)
    url = normalize_gdb_url(url)
    remote_rows = list_remote()
    gdb_rows = list_gdb()
    if any(row["name"].casefold() == name.casefold() for row in [*remote_rows, *gdb_rows]):
        raise ValueError(f"remote adapter name already exists: {name}")
    if any(row["url"] == url for row in gdb_rows):
        raise ValueError(f"remote GDB endpoint is already registered: {url}")
    parsed = urlparse(url)
    profile = _target_profile(target_profile)
    adapter = {
        "name": name,
        "url": url,
        "host": parsed.hostname or "",
        "port": parsed.port or 0,
    }
    row = {"name": name, "url": url}
    if profile:
        row["targetProfile"] = profile
        adapter["targetProfile"] = profile
    _write(remote_rows, [*gdb_rows, row])
    return adapter


def _target_profile(value: object) -> str | None:
    if value is None:
        return None
    from .target_controls import registered_target_profiles

    profiles = registered_target_profiles()
    normalized = value.casefold() if isinstance(value, str) else ""
    if normalized not in profiles:
        known = ", ".join(profiles) or "none"
        raise ValueError(f"target profile must be one of: {known}")
    return normalized


def set_target_profile(name_or_url: str, target_profile: str | None) -> dict[str, str | int | None]:
    """Update target capability metadata without changing endpoint ownership."""
    profile = _target_profile(target_profile)
    remote_rows = list_remote()
    gdb_rows = list_gdb()

    for rows in (remote_rows, gdb_rows):
        for row in rows:
            if row["name"] == name_or_url or row["url"] == name_or_url:
                if profile is None:
                    row.pop("targetProfile", None)
                else:
                    row["targetProfile"] = profile
                _write(remote_rows, gdb_rows)
                return row
    raise ValueError(f"remote adapter not found: {name_or_url}")


def _validate_name(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ValueError(
            "adapter name must start with a letter or number and contain only letters, numbers, dot, underscore, or hyphen"
        )


def remove_remote(name_or_url: str) -> dict[str, str | None]:
    rows = list_remote()
    gdb_rows = list_gdb()
    match = next(
        (row for row in rows if row["name"] == name_or_url or row["url"] == name_or_url), None
    )
    if match is not None:
        _write([row for row in rows if row != match], gdb_rows)
        return match
    gdb_match = next(
        (row for row in gdb_rows if row["name"] == name_or_url or row["url"] == name_or_url),
        None,
    )
    if gdb_match is None:
        raise ValueError(f"remote adapter not found: {name_or_url}")
    _write(rows, [row for row in gdb_rows if row != gdb_match])
    return {"name": gdb_match["name"], "url": gdb_match["url"], "origin": None}


def _write(
    rows: list[dict[str, str | None]], gdb_rows: list[dict[str, str | None]] | None = None
) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"schemaVersion": 1, "remote": rows, "gdb": gdb_rows or []}, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)
