"""Selectable local probe and remote gnwmanager backends for GWProv."""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from urllib.parse import urlparse

from gnwmanager.ocdbackend.base import OCDBackend
from gnwmanager.ocdbackend.openocd_backend import (
    OpenOCDBackend, _drain_stderr, _openocd_launch_commands,
)
from .target_leases import DeviceBusyError, TargetLease, adapter_for_probe


class SelectedPyOCDBackend(OCDBackend):
    """PyOCD backend pinned to one stable probe unique ID."""

    def __init__(self, unique_id: str, *, frequency: int | None = None,
                 operation: str = "GWProv hardware session", lease_wait: float = 30.0,
                 probe_adapter: str | None = None, allow_recovery: bool = False):
        super().__init__()
        if not unique_id:
            raise ValueError("probe unique ID must not be empty")
        self.unique_id = unique_id
        self.probe_adapter = probe_adapter
        try:
            import pyocd
            from pyocd.core.helpers import ConnectHelper
            from pyocd.target import TARGET
        except ImportError as exc:
            raise RuntimeError("install PyOCD to select local probes by ID: pip install pyocd") from exc

        options = {
            "connect_mode": "attach",
            "warning.cortex_m_default": False,
            "persist": True,
        }
        if "stm32h7b0xx" in TARGET:
            options["target_override"] = "STM32h7b0xx"
            options["jlink.device"] = "STM32H7B0VB"
        self._connect_helper = ConnectHelper
        self._session_options = options
        self.session = None
        self._lease = TargetLease(f"probe:{unique_id}", operation, wait=lease_wait,
                                  allow_recovery=allow_recovery)
        self._opened = False
        self._frequency_override = frequency or 0
        self.version = tuple(int(x) for x in pyocd.__version__.split("."))

    @property
    def target(self):
        if self.session is None:
            raise RuntimeError("PyOCD session is not open")
        target = self.session.target
        if target is None:
            raise RuntimeError("PyOCD session has no target")
        return target

    @property
    def probe_name(self) -> str:
        if self.session is None:
            return "PyOCD probe"
        probe = self.session.probe
        return getattr(probe, "product_name", None) or "PyOCD probe"

    def open(self):
        from .target_leases import external_local_owner
        if self.probe_adapter is None:
            try:
                matching = next((probe for probe in enumerate_local_probes()
                                 if probe["id"] == self.unique_id), None)
                if matching:
                    self.probe_adapter = adapter_for_probe(
                        f"{matching['vendor']} {matching['name']}")
            except RuntimeError:
                pass
        external = external_local_owner()
        if external and (external.get("adapter") is None or self.probe_adapter is None
                         or external.get("adapter") == self.probe_adapter):
            raise DeviceBusyError(f"probe:{self.unique_id}", external)
        self._lease.acquire()
        try:
            self.session = self._connect_helper.session_with_chosen_probe(
                unique_id=self.unique_id, options=self._session_options)
            if self.session is None:
                raise RuntimeError(f"debug probe {self.unique_id!r} is not available")
            self.session.open()
            self._opened = True
            if self._frequency_override:
                self.session.probe.set_clock(self._frequency_override)
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise
        return self

    def close(self):
        try:
            if self.session is not None:
                self.session.close()
        finally:
            self.session = None
            self._opened = False
            self._lease.release()

    def read_memory(self, addr: int, size: int) -> bytes:
        return bytes(self.target.read_memory_block8(addr, size))

    def write_memory(self, addr: int, data: bytes):
        self.target.write_memory_block8(addr, data)

    def read_register(self, name: str) -> int:
        return int(self.target.read_core_register(name))

    def write_register(self, name: str, val: int):
        self.target.write_core_register(name, val)

    def set_frequency(self, freq: int):
        self._frequency_override = freq
        if self.session is not None:
            probe = self.session.probe
            if probe is not None:
                probe.set_clock(freq)

    def reset(self):
        self.target.reset()

    def halt(self):
        self.target.halt()

    def reset_and_halt(self):
        self.target.reset_and_halt()

    def resume(self):
        self.target.resume()

    def start_gdbserver(self, port, logging=True, blocking=True):
        from pyocd.gdbserver import GDBServer
        from pyocd.utility.color_log import build_color_logger

        self.session.options.set("gdbserver_port", port)
        if logging:
            build_color_logger(level=1)
        server = GDBServer(self.session, core=0)
        self.session.gdbservers[0] = server
        server.start()
        if blocking:
            while server.is_alive():
                threading.Event().wait(0.1)


class WebSocketBackend(OCDBackend):
    """Synchronous OCDBackend client for one `gnwmanager serve` instance."""

    PROTOCOL_VERSION = 1
    MAX_MEMORY = 65536

    def __init__(self, uri: str, *, frequency: int | None = None, timeout: float = 10.0,
                 origin: str | None = None, operation: str = "GWProv remote session",
                 lease_wait: float = 30.0, allow_recovery: bool = False):
        super().__init__()
        parsed = urlparse(uri)
        if parsed.scheme not in {"ws", "wss"} or not parsed.netloc or parsed.path != "/gdb":
            raise ValueError("remote manager URL must be ws[s]://host:port/gdb")
        self.uri = uri
        self.frequency = frequency
        self.timeout = timeout
        self.origin = origin
        self._ws = None
        self._lock = threading.Lock()
        self._backend_name = "gnwmanager remote"
        self.version = (0, 0, 0)
        self._lease = TargetLease(f"remote:{uri}", operation, wait=lease_wait,
                                  allow_recovery=allow_recovery)

    @property
    def probe_name(self) -> str:
        return f"{self._backend_name} ({urlparse(self.uri).netloc})"

    def open(self):
        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            raise RuntimeError('install remote support with `pip install "gwprov[remote]"`') from exc
        self._lease.acquire()
        try:
            self._ws = connect(self.uri, open_timeout=self.timeout, close_timeout=self.timeout,
                               max_size=self.MAX_MEMORY * 2 + 1024, origin=self.origin)
            hello = json.loads(self._ws.recv(timeout=self.timeout))
            if (hello.get("type") != "hello" or hello.get("backend") != "gnwmanager"
                    or hello.get("version") != self.PROTOCOL_VERSION):
                raise RuntimeError(f"unsupported gnwmanager WebSocket handshake: {hello!r}")
            self._backend_name = f"gnwmanager/{hello.get('targetControl', 'arm')}"
            self._call("attach", [])
            if self.frequency is not None:
                self.set_frequency(self.frequency)
        except Exception:
            self.close()
            raise
        return self

    def close(self):
        ws, self._ws = self._ws, None
        try:
            if ws is not None:
                ws.close()
        finally:
            self._lease.release()

    def _call(self, method: str, args: list):
        if self._ws is None:
            raise RuntimeError("remote gnwmanager session is closed")
        with self._lock:
            self._ws.send(json.dumps({"id": 1, "method": method, "args": args}))
            response = json.loads(self._ws.recv(timeout=self.timeout))
        if response.get("id") != 1:
            raise RuntimeError(f"unexpected remote response: {response!r}")
        if "error" in response:
            raise RuntimeError(response["error"])
        return response.get("result")

    def read_memory(self, addr: int, size: int) -> bytes:
        if size < 0 or addr < 0 or addr + size > 0x100000000:
            raise ValueError("invalid remote memory range")
        result = bytearray()
        while len(result) < size:
            count = min(size - len(result), self.MAX_MEMORY)
            part = bytes.fromhex(self._call("read_memory", [addr + len(result), count]))
            if len(part) != count:
                raise RuntimeError(f"remote read returned {len(part)} bytes; expected {count}")
            result.extend(part)
        return bytes(result)

    def write_memory(self, addr: int, data: bytes):
        data = bytes(data)
        if addr < 0 or addr + len(data) > 0x100000000:
            raise ValueError("invalid remote memory range")
        for offset in range(0, len(data), self.MAX_MEMORY):
            self._call("write_memory", [addr + offset,
                                         data[offset:offset + self.MAX_MEMORY].hex()])

    def read_register(self, name: str) -> int:
        return int(self._call("read_register", [name]))

    def write_register(self, name: str, val: int):
        self._call("write_register", [name, val])

    def set_frequency(self, freq: int):
        self._call("set_frequency", [freq])

    def halt(self):
        self._call("halt", [])

    def resume(self):
        self._call("resume", [])

    def reset_and_halt(self):
        self._call("reset_and_halt", [])

    def reset(self):
        # The remote protocol has reset-and-halt rather than reset. Preserve the
        # OCDBackend reset contract with an explicit, short halt/resume sequence.
        self.reset_and_halt()
        self.resume()

    def start_gdbserver(self, port, logging=True, blocking=True):
        raise NotImplementedError("the remote gnwmanager server owns its debug transport")


def enumerate_local_probes() -> list[dict]:
    """Return attached PyOCD probe identities without opening target sessions."""
    try:
        from pyocd.core.helpers import ConnectHelper
    except ImportError:
        return []
    captured = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    root_logger = logging.getLogger()
    handler = Capture()
    root_logger.addHandler(handler)
    try:
        probes = ConnectHelper.get_all_connected_probes(blocking=False, print_wait_message=False)
    finally:
        root_logger.removeHandler(handler)
    if not probes:
        unavailable = next((line for line in captured if "no libusb" in line.casefold()
                            or "not supported because" in line.casefold()), None)
        if unavailable:
            raise RuntimeError(f"PyOCD probe enumeration is unavailable: {unavailable}")
    rows = []
    for probe in probes:
        rows.append({
            "id": str(getattr(probe, "unique_id", "") or ""),
            "name": str(getattr(probe, "product_name", "") or type(probe).__name__),
            "vendor": str(getattr(probe, "vendor_name", "") or ""),
            "product": str(getattr(probe, "product_name", "") or ""),
            "backend": "pyocd",
        })
    return sorted(rows, key=lambda row: (row["name"].casefold(), row["id"]))


class SelectedOpenOCDBackend(OpenOCDBackend):
    """Open one explicit OpenOCD adapter without killing other sessions."""

    ADAPTERS = ("stlink", "jlink", "cmsis-dap", "rpi-gpio")

    def __init__(self, adapter: str, *, port: int | None = None,
                 operation: str = "GWProv OpenOCD session", lease_wait: float = 30.0):
        if adapter not in self.ADAPTERS:
            raise ValueError(f"unsupported OpenOCD adapter {adapter!r}; choose {', '.join(self.ADAPTERS)}")
        if port is None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
        super().__init__(port=port)
        self.adapter = adapter
        self._connected = False
        self._lease = TargetLease(f"adapter:{adapter}", operation, wait=lease_wait)
        self._probe_leases = []

    def open(self):
        from collections import deque
        from threading import Thread
        import subprocess
        import time

        from .target_leases import external_local_owner
        external = external_local_owner()
        if external and (external.get("adapter") is None or external.get("adapter") == self.adapter):
            raise DeviceBusyError("local hardware", external)
        try:
            matching_probes = []
            for probe in enumerate_local_probes():
                name = f"{probe['vendor']} {probe['name']}"
                probe_adapter = adapter_for_probe(name)
                if probe_adapter is None or probe_adapter == self.adapter:
                    matching_probes.append(probe)
        except DeviceBusyError:
            raise
        except RuntimeError:
            # PyOCD is optional; its USB layer may be unavailable even when
            # gnwmanager's OpenOCD adapter can still reach the probe.
            matching_probes = []
        try:
            for probe in sorted(matching_probes, key=lambda row: row["id"]):
                lease = TargetLease(f"probe:{probe['id']}", self._lease.operation,
                                    wait=self._lease.wait)
                lease.acquire()
                self._probe_leases.append(lease)
            self._lease.acquire()
            command = next((cmd for name, cmd in _openocd_launch_commands(self._address[1])
                            if name == self.adapter), None)
            if command is None:
                raise RuntimeError(f"gnwmanager has no OpenOCD config for {self.adapter}")
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._stderr_buffer = deque(maxlen=1000)
            self._openocd_process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE)
            level = logging.INFO if os.environ.get("GNWMANAGER_OPENOCD_DEBUG") else logging.DEBUG
            self._stderr_thread = Thread(
                target=_drain_stderr,
                args=(self._openocd_process.stderr, self._stderr_buffer, level), daemon=True)
            self._stderr_thread.start()
            deadline = time.monotonic() + 10.0
            last_error = None
            while time.monotonic() < deadline:
                if self._openocd_process.poll() is not None:
                    detail = "\n".join(self._stderr_buffer)[-2000:]
                    raise RuntimeError(f"OpenOCD {self.adapter} exited: {detail}")
                try:
                    self._socket.connect(self._address)
                    self._connected = True
                    return self
                except OSError as error:
                    last_error = error
                    time.sleep(0.1)
            raise RuntimeError(f"OpenOCD {self.adapter} did not accept a client: {last_error}")
        except Exception:
            self.close()
            raise

    def close(self):
        import subprocess
        try:
            if getattr(self, "_connected", False):
                try:
                    self("exit")
                except Exception:
                    pass
                self._connected = False
            sock = getattr(self, "_socket", None)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            process = getattr(self, "_openocd_process", None)
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            thread = getattr(self, "_stderr_thread", None)
            if thread is not None:
                thread.join(timeout=2)
                self._stderr_thread = None
            self._openocd_process = None
        finally:
            self._lease.release()
            for lease in reversed(self._probe_leases):
                lease.release()
            self._probe_leases.clear()


class AutoOpenOCDBackend(OCDBackend):
    """Try gnwmanager's adapter order while keeping every session leased."""

    ADAPTERS = SelectedOpenOCDBackend.ADAPTERS

    def __init__(self, *, operation: str = "GWProv OpenOCD session", port: int | None = None):
        super().__init__()
        self.operation = operation
        self.port = port
        self.backend = None
        self.version = (0, 0, 0)

    def open(self):
        failures = []
        for adapter in self.ADAPTERS:
            backend = SelectedOpenOCDBackend(adapter, port=self.port,
                                             operation=self.operation, lease_wait=0)
            try:
                backend.open()
            except Exception as error:
                backend.close()
                if isinstance(error, DeviceBusyError):
                    raise
                failures.append(f"{adapter}: {error}")
                continue
            self.backend = backend
            self.version = backend.version
            return self
        raise RuntimeError("unable to open any supported OpenOCD adapter (" +
                           "; ".join(failures) + ")")

    def close(self):
        backend, self.backend = self.backend, None
        if backend is not None:
            backend.close()

    @property
    def probe_name(self):
        return self.backend.probe_name if self.backend is not None else "OpenOCD auto-detect"

    def __getattr__(self, name):
        backend = self.__dict__.get("backend")
        if backend is None:
            raise AttributeError(name)
        return getattr(backend, name)
