"""Cross-process leases that serialize traffic to one physical debug target."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path


class DeviceBusyError(RuntimeError):
    def __init__(self, key: str, owner: dict | None = None):
        self.key = key
        self.owner = owner or {}
        operation = self.owner.get("operation", "another target session")
        pid = self.owner.get("pid")
        suffix = f" (pid {pid})" if pid else ""
        super().__init__(f"target {key!r} is busy with {operation}{suffix}")


class DeviceRecoveryRequired(DeviceBusyError):
    """A previous operation left the target in a mode requiring explicit recovery."""

    def __init__(self, key: str, owner: dict | None = None):
        self.key = key
        self.owner = owner or {}
        phase = self.owner.get("phase", "hardware operation")
        super().__init__(key, {**self.owner,
                               "operation": f"interrupted {phase}; run `gwprov device recover`"})


def _lock_dir() -> Path:
    override = os.environ.get("GWPROV_LEASE_DIR")
    return Path(override).expanduser() if override else Path.home() / ".cache" / "gwprov" / "target-leases"


def _path_for(key: str) -> Path:
    token = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return _lock_dir() / f"{token}.lock"


def adapter_for_probe(name: str) -> str | None:
    value = name.casefold()
    if "stlink" in value or "st-link" in value:
        return "stlink"
    if "j-link" in value or "jlink" in value:
        return "jlink"
    if any(term in value for term in ("cmsis-dap", "picoprobe", "pico probe")):
        return "cmsis-dap"
    return None


def _lock(fd: int, *, blocking: bool) -> bool:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(fd, flags)
        return True
    except BlockingIOError:
        return False


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def _read_metadata(fd: int, key: str) -> dict:
    os.lseek(fd, 1 if os.name == "nt" else 0, os.SEEK_SET)
    try:
        return json.loads(os.read(fd, 4096).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"key": key}


def _write_metadata(fd: int, owner: dict) -> None:
    payload = json.dumps(owner, separators=(",", ":")).encode("utf-8")
    offset = 1 if os.name == "nt" else 0
    os.ftruncate(fd, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    os.write(fd, payload)
    os.fsync(fd)


def lease_owner(key: str) -> dict | None:
    """Return the live lease owner without contacting the debug probe."""
    path = _path_for(key)
    if not path.exists():
        return None
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt" and os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        if _lock(fd, blocking=False):
            owner = _read_metadata(fd, key)
            _unlock(fd)
            if owner.get("recovery_required"):
                return {**owner, "lease_active": False}
            return None
        owner = _read_metadata(fd, key)
        if not owner.get("operation"):
            owner = {"key": key, "operation": "target operation (owner metadata pending)"}
        return {**owner, "lease_active": True}
    finally:
        os.close(fd)


class TargetLease:
    """Exclusive process-shared target lease, held for a backend's lifetime."""

    def __init__(self, key: str, operation: str, *, wait: float = 30.0,
                 allow_recovery: bool = False):
        if not key:
            raise ValueError("target lease key must not be empty")
        self.key = key
        self.operation = operation
        self.wait = max(0.0, wait)
        self.allow_recovery = allow_recovery
        self.path = _path_for(key)
        self.fd: int | None = None
        self.daemon_token: str | None = None
        self.owner: dict | None = None

    def acquire(self) -> "TargetLease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        if os.name == "nt" and os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        deadline = time.monotonic() + self.wait
        while True:
            if _lock(fd, blocking=False):
                previous = _read_metadata(fd, self.key)
                if previous.get("recovery_required") and not self.allow_recovery:
                    _unlock(fd)
                    os.close(fd)
                    raise DeviceRecoveryRequired(self.key, previous)
                self.fd = fd
                owner = {**previous, "key": self.key, "operation": self.operation,
                         "pid": os.getpid(), "started": time.time()}
                self.owner = owner
                try:
                    _write_metadata(fd, owner)
                    token = secrets.token_hex(16)
                    from .daemon import register_lease
                    register_lease(token, owner)
                    self.daemon_token = token
                except Exception:
                    self.release()
                    raise
                return self
            if time.monotonic() >= deadline:
                os.lseek(fd, 1 if os.name == "nt" else 0, os.SEEK_SET)
                try:
                    owner = json.loads(os.read(fd, 4096).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    owner = None
                os.close(fd)
                raise DeviceBusyError(self.key, owner)
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def mark_recovery_required(self, phase: str) -> None:
        if self.fd is None or self.owner is None:
            raise RuntimeError("target lease must be held before recording its device mode")
        self.owner.update(recovery_required=True, phase=phase,
                          recovery_marked=time.time())
        _write_metadata(self.fd, self.owner)

    def clear_recovery_required(self) -> None:
        if self.fd is None or self.owner is None:
            raise RuntimeError("target lease must be held before clearing device mode")
        self.owner.pop("recovery_required", None)
        self.owner.pop("phase", None)
        self.owner.pop("recovery_marked", None)
        _write_metadata(self.fd, self.owner)

    def release(self) -> None:
        fd, self.fd = self.fd, None
        token, self.daemon_token = self.daemon_token, None
        if token:
            from .daemon import release_lease
            release_lease(token)
        if fd is not None:
            try:
                _unlock(fd)
            finally:
                os.close(fd)

    def __enter__(self) -> "TargetLease":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def external_local_owner() -> dict | None:
    """Detect a debug-server process that bypasses GWProv's lease files."""
    current = os.getpid()

    def describe(pid: int, name: str, command: str) -> dict | None:
        name, command = name.casefold(), command.casefold()
        if "openocd" in name or "openocd" in command:
            adapter = next((item for item in ("stlink", "jlink", "cmsis-dap", "rpi-gpio")
                            if item in command), None)
            return {"key": "external-openocd", "operation": "external OpenOCD session",
                    "pid": pid, "command": command[:240], "adapter": adapter}
        if "gnwmanager" in name or "gnwmanager" in command:
            adapter = next((item for item in ("stlink", "jlink", "cmsis-dap", "rpi-gpio")
                            if item in command), None)
            return {"key": "external-gnwmanager", "operation": "external gnwmanager session",
                    "pid": pid, "command": command[:240], "adapter": adapter}
        if "pyocd" in name or "pyocd" in command:
            return {"key": "external-pyocd", "operation": "external PyOCD session",
                    "pid": pid, "command": command[:240], "adapter": None}
        return None

    try:
        import psutil
        for process in psutil.process_iter(["pid", "name", "cmdline", "uids"]):
            try:
                info = process.info
                pid = info.get("pid")
                if not pid or pid == current:
                    continue
                owner = describe(pid, info.get("name") or "",
                                 " ".join(info.get("cmdline") or []))
                if owner:
                    return owner
            except Exception:
                continue
    except Exception:
        pass
    # Keep the important OpenOCD ownership check working on minimal Linux
    # installs where psutil is not present.
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit() or int(entry.name) == current:
                continue
            try:
                command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                name = (entry / "comm").read_text(errors="replace").strip()
                owner = describe(int(entry.name), name, command)
                if owner:
                    return owner
            except (OSError, ValueError):
                continue
    return None
