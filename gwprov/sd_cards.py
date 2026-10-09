"""Registry for mounted SD-card directories used by GWProv."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path, PureWindowsPath


def _registry_path() -> Path:
    from .adapters import _config_path
    return _config_path().with_name("sd-cards.json")


def _default_name(path: Path) -> str:
    if os.name == "nt":
        drive = PureWindowsPath(str(path)).drive.rstrip(":\\/")
        if drive:
            return drive
    name = path.name
    if name:
        return name
    raise ValueError("SD card path has no folder name; provide an explicit NAME")


def list_cards() -> list[dict[str, str]]:
    path = _registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read SD-card registry {path}: {exc}") from exc
    rows = data.get("cards", []) if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"invalid SD-card registry format in {path}")
    result = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("path"), str):
            raise RuntimeError(f"invalid SD-card entry in {path}")
        result.append({"name": row["name"], "path": row["path"]})
    return result


def add_card(path_value: str | Path, name: str | None = None) -> dict[str, str]:
    path = Path(path_value).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError(f"SD card path must be an existing mounted directory: {path}")
    name = (name or _default_name(path)).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ValueError("SD-card name must start with a letter or number and contain only letters, numbers, dot, underscore, or hyphen")
    rows = list_cards()
    if any(row["name"].casefold() == name.casefold() for row in rows):
        raise ValueError(f"SD-card name already exists: {name}")
    if any(Path(row["path"]) == path for row in rows):
        raise ValueError(f"SD-card path is already registered: {path}")
    result = {"name": name, "path": str(path)}
    rows.append(result)
    _write(rows)
    return result


def remove_card(name: str) -> dict[str, str]:
    rows = list_cards()
    found = next((row for row in rows if row["name"].casefold() == name.casefold()), None)
    if found is None:
        raise ValueError(f"SD card not found: {name}")
    from .device_assignments import assignments
    users = [device for device, values in assignments().items()
             if values.get("sdcard", "").casefold() == found["name"].casefold()]
    if users:
        raise ValueError(f"SD card {found['name']!r} is assigned to {', '.join(users)}; clear those assignments first")
    _write([row for row in rows if row != found])
    return found


def _write(rows: list[dict[str, str]]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"schemaVersion": 1, "cards": rows}, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)
