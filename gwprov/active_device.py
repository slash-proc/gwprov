"""Persistent, user-selected device focus for commands that target one device."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse


def _state_path() -> Path:
    override = os.environ.get("GWPROV_STATE_DIR")
    if override:
        return Path(override).expanduser() / "active-device.json"
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return root / "gwprov" / "active-device.json"


def get_active() -> str | None:
    path = _state_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("device")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read active device selection {path}: {exc}") from exc
    return value if isinstance(value, str) and value else None


def get_active_origin() -> str | None:
    path = _state_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("origin")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read active device selection {path}: {exc}") from exc
    return value if isinstance(value, str) and value else None


def set_active(device: str | None, *, origin: str | None = None) -> Path:
    if device and device.startswith("remote:"):
        uri = device.removeprefix("remote:")
        parsed = urlparse(uri)
        if parsed.scheme not in {"ws", "wss"} or not parsed.netloc or parsed.path != "/gdb":
            raise ValueError("remote device ID must be remote:ws[s]://host:port/gdb")
    elif origin:
        raise ValueError("--remote-origin can only be used with a remote active device")
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps({"device": device, "origin": origin}, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)
    return path
