"""Persistent profile and SD-card assignments for managed devices."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def _path() -> Path:
    override = os.environ.get("GWPROV_STATE_DIR")
    if override:
        return Path(override).expanduser() / "device-assignments.json"
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return root / "gwprov" / "device-assignments.json"


def assignments() -> dict[str, dict[str, str]]:
    path = _path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read device assignments {path}: {exc}") from exc
    rows = data.get("devices", {}) if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        raise RuntimeError(f"invalid device assignments file: {path}")
    return {
        key: {
            name: value
            for name, value in row.items()
            if name in {"profile", "sdcard", "name"} and isinstance(value, str)
        }
        for key, row in rows.items()
        if isinstance(key, str) and isinstance(row, dict)
    }


def get_assignment(device_id: str) -> dict[str, str]:
    return assignments().get(device_id, {})


def set_assignment(device_id: str, field: str, value: str | None) -> Path:
    if field not in {"profile", "sdcard", "name"}:
        raise ValueError(f"unsupported device assignment: {field}")
    if field == "name" and value is not None:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("device name must contain only letters, numbers, '-' and '_'")
    rows = assignments()
    current = rows.setdefault(device_id, {})
    if value is None:
        current.pop(field, None)
        if not current:
            rows.pop(device_id, None)
    else:
        current[field] = value
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"schemaVersion": 1, "devices": rows}, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)
    return path
