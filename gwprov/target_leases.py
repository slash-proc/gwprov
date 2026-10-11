"""Cross-process leases that serialize traffic to one physical debug target."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import threading
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


def operation_is_busy(owner: dict | None) -> bool:
    """Whether an operation needs exclusive status in `ps` (writes or a debugger)."""
    if not owner:
        return False
    operation = str(owner.get("operation", "")).casefold()
    return any(word in operation for word in
               ("deploy", "flash", "erase", "program", "write", "recover",
                "unlock", "stub start", "debug session", "debug console",
                "hardware profile"))


def operation_suppresses_poll(owner: dict | None) -> bool:
    """Skip the heartbeat only while an operation is actively writing/resetting."""
    if not owner:
        return False
    operation = str(owner.get("operation", "")).casefold()
    return any(word in operation for word in
               ("deploy", "flash", "erase", "program", "write", "recover",
                "unlock", "stub start"))


def operation_blocks_drain(owner: dict | None) -> bool:
    """Whether a live target operation must finish or be canceled before reload."""
    if not owner:
        return False
    operation = str(owner.get("operation", "")).casefold()
    return any(word in operation for word in
               ("deploy", "flash", "erase", "program", "write", "recover",
                "unlock", "stub start", "debug session", "debug console", "debug python",
                "hardware profile"))


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


def active_lease_owners() -> list[dict]:
    """Read all currently held process leases without contacting hardware."""
    owners = []
    try:
        paths = tuple(_lock_dir().glob("*.lock"))
    except OSError:
        return owners
    for path in paths:
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            continue
        try:
            if os.name == "nt" and os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            if _lock(fd, blocking=False):
                _unlock(fd)
                continue
            owner = _read_metadata(fd, "unknown target")
            owner["lease_active"] = True
            owners.append(owner)
        except OSError:
            continue
        finally:
            os.close(fd)
    return owners


def update_device_marker(key: str, *, phase: str | None = None,
                         flasher_ready: bool = False) -> None:
    """Persist observed RAM-service state without holding a BUSY operation lease."""
    path = _path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt" and os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        _lock(fd, blocking=True)
        owner = _read_metadata(fd, key)
        owner["key"] = key
        if phase is None:
            for field in ("recovery_required", "flasher_ready", "phase", "recovery_marked"):
                owner.pop(field, None)
        else:
            owner.update(recovery_required=True, flasher_ready=flasher_ready,
                         phase=phase, recovery_marked=time.time())
        _write_metadata(fd, owner)
        _unlock(fd)
    finally:
        os.close(fd)


class TargetLease:
    """Exclusive process-shared target lease, held for a backend's lifetime."""

    def __init__(self, key: str, operation: str, *, wait: float = 30.0,
                 allow_recovery_marker: bool = False,
                 register_with_daemon: bool = True, owner_pid: int | None = None,
                 daemon_owned: bool = False):
        if not key:
            raise ValueError("target lease key must not be empty")
        self.key = key
        self.operation = operation
        self.wait = max(0.0, wait)
        self.allow_recovery_marker = allow_recovery_marker
        self.register_with_daemon = register_with_daemon
        self.owner_pid = owner_pid
        self.daemon_owned = daemon_owned
        self.path = _path_for(key)
        self.fd: int | None = None
        self.daemon_token: str | None = None
        self.monitor_pause_token = secrets.token_hex(16)
        self.owner: dict | None = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    def acquire(self) -> "TargetLease":
        if self.register_with_daemon:
            from .daemon import pause_device_monitor
            pause_device_monitor(self.key, self.monitor_pause_token,
                                 self.owner_pid or os.getpid(), self.wait)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        if os.name == "nt" and os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        deadline = time.monotonic() + self.wait
        while True:
            if _lock(fd, blocking=False):
                previous = _read_metadata(fd, self.key)
                self.fd = fd
                owner = {**previous, "key": self.key, "operation": self.operation,
                         "pid": self.owner_pid or os.getpid(), "started": time.time(),
                         "daemon_owned": self.daemon_owned}
                self.owner = owner
                try:
                    _write_metadata(fd, owner)
                    if self.register_with_daemon:
                        token = secrets.token_hex(16)
                        from .daemon import register_lease
                        register_lease(token, owner)
                        self.daemon_token = token
                        self._heartbeat_stop.clear()
                        self._heartbeat_thread = threading.Thread(
                            target=self._heartbeat_loop, daemon=True,
                            name=f"gwprov-lease-heartbeat-{os.getpid()}")
                        self._heartbeat_thread.start()
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
                self._resume_monitor()
                raise DeviceBusyError(self.key, owner)
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def mark_recovery_required(self, phase: str) -> None:
        if self.fd is None or self.owner is None:
            raise RuntimeError("target lease must be held before recording its device mode")
        self.owner.pop("flasher_ready", None)
        self.owner.update(recovery_required=True, phase=phase,
                          recovery_marked=time.time())
        _write_metadata(self.fd, self.owner)

    def mark_flasher_ready(self) -> None:
        """Record a verified idle RAM flasher without calling it an active lease."""
        if self.fd is None or self.owner is None:
            raise RuntimeError("target lease must be held before recording its device mode")
        self.owner.update(recovery_required=True, flasher_ready=True,
                          phase="gnwmanager RAM flasher ready", recovery_marked=time.time())
        _write_metadata(self.fd, self.owner)

    def clear_recovery_required(self) -> None:
        if self.fd is None or self.owner is None:
            raise RuntimeError("target lease must be held before clearing device mode")
        self.owner.pop("recovery_required", None)
        self.owner.pop("flasher_ready", None)
        self.owner.pop("phase", None)
        self.owner.pop("recovery_marked", None)
        _write_metadata(self.fd, self.owner)

    def release(self) -> None:
        fd, self.fd = self.fd, None
        token, self.daemon_token = self.daemon_token, None
        self._heartbeat_stop.set()
        heartbeat_thread, self._heartbeat_thread = self._heartbeat_thread, None
        if heartbeat_thread and heartbeat_thread is not threading.current_thread():
            # The heartbeat request is independent of the target's write and
            # bounded by the daemon IPC timeout. Wait for any in-flight request
            # before releasing the token so it cannot recreate a closed lease.
            heartbeat_thread.join(timeout=10.0)
        if token:
            from .daemon import release_lease
            release_lease(token)
        if fd is not None:
            try:
                _unlock(fd)
            finally:
                os.close(fd)
        if self.register_with_daemon:
            self._resume_monitor()

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(2.0):
            token = self.daemon_token
            if not token:
                return
            try:
                from .daemon import heartbeat_lease
                heartbeat_lease(token, self.owner or {})
            except (OSError, RuntimeError, ValueError):
                # The daemon's dead-client watchdog will reclaim the target if
                # this process disappears; a later heartbeat can re-register
                # against a daemon that was restarted meanwhile.
                pass
            if self._heartbeat_stop.is_set():
                from .daemon import release_lease
                release_lease(token)
                return

    def _resume_monitor(self) -> None:
        from .daemon import resume_device_monitor
        resume_device_monitor(self.key, self.monitor_pause_token)

    def __enter__(self) -> "TargetLease":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def external_local_owner() -> dict | None:
    """Detect a debug-server process that bypasses GWProv's lease files."""
    current = os.getpid()

    def describe(pid: int, name: str, command: str) -> dict | None:
        name = name.casefold().removesuffix(".exe")
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = []
        executable = Path(argv[0]).name.casefold().removesuffix(".exe") if argv else ""

        # Match the executable or an explicit Python module invocation. A
        # substring search across the full command line mistakes an ancestor
        # shell (or this process's script text) for a debug server whenever
        # its arguments happen to mention "pyocd", "openocd", or
        # "gnwmanager".
        def is_module(module: str) -> bool:
            return any(arg == "-m" and index + 1 < len(argv)
                       and argv[index + 1] == module
                       for index, arg in enumerate(argv))

        if name == "openocd" or executable == "openocd":
            adapter = next((item for item in ("stlink", "jlink", "cmsis-dap", "rpi-gpio")
                            if item in argv), None)
            return {"key": "external-openocd", "operation": "external OpenOCD session",
                    "pid": pid, "command": command[:240], "adapter": adapter}
        if name == "gnwmanager" or executable == "gnwmanager" or is_module("gnwmanager"):
            # `gnwmanager -b gdb ...` is a QEMU GDB-stub client.  It does not
            # open or own a USB debug probe, so it must not block a physical
            # PyOCD/OpenOCD session.  Keep unknown and probe-backed manager
            # processes conservative because they may own the hardware.
            for index, arg in enumerate(argv):
                if arg in ("-b", "--backend") and index + 1 < len(argv):
                    if argv[index + 1] == "gdb":
                        return None
                if arg.startswith("--backend=") and arg.partition("=")[2] == "gdb":
                    return None
            adapter = next((item for item in ("stlink", "jlink", "cmsis-dap", "rpi-gpio")
                            if item in argv), None)
            return {"key": "external-gnwmanager", "operation": "external gnwmanager session",
                    "pid": pid, "command": command[:240], "adapter": adapter}
        if name == "pyocd" or executable == "pyocd" or is_module("pyocd"):
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
