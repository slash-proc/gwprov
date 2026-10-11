"""Discover and remember verified stock firmware backups."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .adapters import _config_path
from .stock import EXPECTED


def _registry_path() -> Path:
    return _config_path().with_name("backups.json")


def _valid_pair(folder: Path, model: str) -> dict | None:
    internal = folder / f"internal_flash_backup_{model}.bin"
    external = folder / f"flash_backup_{model}.bin"
    if not internal.is_file() or not external.is_file():
        return None
    if not 0x20000 <= internal.stat().st_size <= 256 * 1024:
        return None
    if external.stat().st_size <= 0 or external.stat().st_size > 256 * 1024 * 1024:
        return None
    import hashlib
    internal_data = internal.read_bytes()
    external_data = external.read_bytes()
    check = external_data[:-8192] if model == "mario" else external_data[0x20000:0x3254a0]
    if (hashlib.sha1(internal_data[:0x20000]).hexdigest(), hashlib.sha1(check).hexdigest()) != EXPECTED[model]:
        return None
    return {"model": model, "directory": str(folder.resolve()),
            "bank1": str(internal.resolve()), "bank0": str(external.resolve()),
            "bank1Bytes": len(internal_data), "bank0Bytes": len(external_data)}


def search(directory: str | Path = ".") -> list[dict]:
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"backup search directory does not exist: {root}")
    found = []
    seen = set()
    for model in EXPECTED:
        pattern = f"internal_flash_backup_{model}.bin"
        for internal in root.rglob(pattern):
            row = _valid_pair(internal.parent, model)
            if row and row["directory"] not in seen:
                found.append(row)
                seen.add(row["directory"])
    registry = {row["directory"]: row for row in list_backups()}
    registry.update({row["directory"]: row for row in found})
    _write(list(registry.values()))
    return sorted(found, key=lambda row: (row["model"], row["directory"].casefold()))


def list_backups(model: str | None = None) -> list[dict]:
    path = _registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read backup registry {path}: {exc}") from exc
    rows = data.get("backups", []) if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"invalid backup registry format in {path}")
    current = []
    for item in rows:
        if not isinstance(item, dict) or item.get("model") not in EXPECTED:
            continue
        row = _valid_pair(Path(item.get("directory", "")), item["model"])
        if row:
            current.append(row)
    _write(current)
    if model:
        current = [row for row in current if row["model"] == model]
    return sorted(current, key=lambda row: (row["model"], row["directory"].casefold()))


def _write(rows: list[dict]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"schemaVersion": 1, "backups": rows}, indent=2) + "\n",
                         encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)
