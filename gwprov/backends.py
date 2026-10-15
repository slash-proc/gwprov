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
    OpenOCDBackend,
    _drain_stderr,
    _openocd_launch_commands,
)
from .target_leases import DeviceBusyError, TargetLease, adapter_for_probe


class _H7B0RomTableNoiseFilter(logging.Filter):
    """Hide PyOCD's known nonfatal H7B0 APB-D ROM-table probe diagnostics."""

    def filter(self, record):
        message = record.getMessage()
        if message == "Invalid coresight component, cidr=0x0":
            return False
        return not (
            message.startswith(
                "Error attempting to probe CoreSight component referenced by ROM table entry #5:"
            )
            and "Memory transfer fault (read) @ 0xe00f0fd0-0xe00f0fff" in message
        )


class _DaemonTargetProxy:
    """Small PyOCD target facade for debugger code running over daemon IPC."""

    def __init__(self, backend):
        self.backend = backend

    def read_core_register(self, name):
        return self.backend.read_register(name)

    def write_core_register(self, name, value):
        return self.backend.write_register(name, value)

    def read_memory_block8(self, address, size):
        return self.backend.read_memory(address, size)

    def write_memory_block8(self, address, data):
        return self.backend.write_memory(address, bytes(data))

    def get_state(self):
        name = self.backend._daemon_call("get_state")
        from pyocd.core.target import Target

        return Target.State[name]

    def step(self):
        return self.backend._daemon_call("step")

    def set_breakpoint(self, address, kind):
        return self.backend._daemon_call(
            "set_breakpoint", address, getattr(kind, "name", str(kind))
        )

    def remove_breakpoint(self, address):
        return self.backend._daemon_call("remove_breakpoint", address)

    def set_watchpoint(self, address, size, kind):
        return self.backend._daemon_call(
            "set_watchpoint", address, size, getattr(kind, "name", str(kind))
        )

    def remove_watchpoint(self, address, size, kind):
        return self.backend._daemon_call(
            "remove_watchpoint", address, size, getattr(kind, "name", str(kind))
        )


class SelectedPyOCDBackend(OCDBackend):
    """PyOCD backend pinned to one stable probe unique ID."""

    def __init__(
        self,
        unique_id: str,
        *,
        frequency: int | None = None,
        operation: str = "GWProv hardware session",
        lease_wait: float = 30.0,
        probe_adapter: str | None = None,
        allow_recovery_marker: bool = False,
        daemon_managed: bool = False,
        daemon_proxy: bool = False,
        resume_on_disconnect: bool = True,
    ):
        super().__init__()
        if not unique_id:
            raise ValueError("probe unique ID must not be empty")
        self.unique_id = unique_id
        self.probe_adapter = probe_adapter
        self.daemon_managed = daemon_managed
        self._daemon_proxy = daemon_proxy and not daemon_managed
        self._daemon_token: str | None = None
        self._daemon_heartbeat_stop = threading.Event()
        self._daemon_heartbeat_thread: threading.Thread | None = None
        self._operation = operation
        self._probe_name = "PyOCD probe"
        try:
            import pyocd
            from pyocd.core.helpers import ConnectHelper
            from pyocd.target import TARGET
        except ImportError as exc:
            raise RuntimeError(
                "install PyOCD to select local probes by ID: pip install pyocd"
            ) from exc

        options = {
            "connect_mode": "attach",
            "warning.cortex_m_default": False,
            "persist": True,
            "resume_on_disconnect": resume_on_disconnect,
        }
        self._target_is_h7b0 = "stm32h7b0xx" in TARGET
        if self._target_is_h7b0:
            options["target_override"] = "STM32h7b0xx"
            options["jlink.device"] = "STM32H7B0VB"
        self._connect_helper = ConnectHelper
        self._session_options = options
        self.session = None
        self._lease = (
            None
            if daemon_managed or self._daemon_proxy
            else TargetLease(
                f"probe:{unique_id}",
                operation,
                wait=lease_wait,
                allow_recovery_marker=allow_recovery_marker,
            )
        )
        self._opened = False
        self._frequency_override = frequency or 0
        self.version = tuple(int(x) for x in pyocd.__version__.split("."))

    @property
    def target(self):
        if self._daemon_proxy:
            if not self._daemon_token:
                raise RuntimeError("daemon target session is not open")
            return _DaemonTargetProxy(self)
        if self.session is None:
            raise RuntimeError("PyOCD session is not open")
        target = self.session.target
        if target is None:
            raise RuntimeError("PyOCD session has no target")
        return target

    @property
    def probe_name(self) -> str:
        if self._daemon_proxy:
            return self._probe_name
        if self.session is None:
            return "PyOCD probe"
        probe = self.session.probe
        return getattr(probe, "product_name", None) or "PyOCD probe"

    def open(self):
        if self._daemon_proxy:
            from .daemon import open_target_session

            probe = {
                "id": self.unique_id,
                "backend": "pyocd",
                "name": self._probe_name,
                "vendor": "STMicroelectronics",
                "adapter": self.probe_adapter,
            }
            opened = open_target_session(probe, self._operation)
            self._daemon_token = opened["token"]
            self._opened = True
            self._start_daemon_heartbeat()
            try:
                if self._frequency_override:
                    self.set_frequency(self._frequency_override)
            except Exception:
                self.close()
                raise
            return self
        from .target_leases import external_local_owner

        if self.probe_adapter is None:
            try:
                matching = next(
                    (probe for probe in enumerate_local_probes() if probe["id"] == self.unique_id),
                    None,
                )
                if matching:
                    self.probe_adapter = adapter_for_probe(
                        f"{matching['vendor']} {matching['name']}"
                    )
            except RuntimeError:
                pass
        if not self.daemon_managed:
            external = external_local_owner()
            if external and (
                external.get("adapter") is None
                or self.probe_adapter is None
                or external.get("adapter") == self.probe_adapter
            ):
                raise DeviceBusyError(f"probe:{self.unique_id}", external)
            self._lease.acquire()
        try:
            self.session = self._connect_helper.session_with_chosen_probe(
                unique_id=self.unique_id, options=self._session_options
            )
            if self.session is None:
                raise RuntimeError(f"debug probe {self.unique_id!r} is not available")
            # PyOCD's H7 discovery probes APB-D ROM-table entry #5, whose
            # component-ID read is a known nonfatal fault on this target. Keep
            # all other PyOCD diagnostics visible and limit this filter to the
            # target-session initialization that emits the noisy probe logs.
            if self._target_is_h7b0:
                rom_table_logger = logging.getLogger("pyocd.coresight.rom_table")
                noise_filter = _H7B0RomTableNoiseFilter()
                rom_table_logger.addFilter(noise_filter)
                try:
                    self.session.open()
                finally:
                    rom_table_logger.removeFilter(noise_filter)
            else:
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
        if self._daemon_proxy:
            self._stop_daemon_heartbeat()
            token, self._daemon_token = self._daemon_token, None
            self._opened = False
            if token:
                from .daemon import close_target_session

                close_target_session(token)
            return
        try:
            if self.session is not None:
                self.session.close()
        finally:
            self.session = None
            self._opened = False
            if self._lease is not None:
                self._lease.release()

    def _start_daemon_heartbeat(self):
        token = self._daemon_token
        if not token or (
            self._daemon_heartbeat_thread and self._daemon_heartbeat_thread.is_alive()
        ):
            return
        self._daemon_heartbeat_stop.clear()
        owner = {
            "key": f"probe:{self.unique_id}",
            "operation": self._operation,
            "pid": os.getpid(),
            "daemon_owned": True,
        }

        def heartbeat():
            from .daemon import heartbeat_proxy_session

            while not self._daemon_heartbeat_stop.wait(2.0):
                try:
                    heartbeat_proxy_session(token, owner)
                except Exception:
                    # A lost IPC session is reported by the next debugger call;
                    # heartbeat traffic must not terminate the interactive REPL.
                    continue

        self._daemon_heartbeat_thread = threading.Thread(
            target=heartbeat, daemon=True, name=f"gwprov-session-heartbeat-{self.unique_id}"
        )
        self._daemon_heartbeat_thread.start()

    def _stop_daemon_heartbeat(self):
        self._daemon_heartbeat_stop.set()
        thread, self._daemon_heartbeat_thread = self._daemon_heartbeat_thread, None
        if thread and thread is not threading.current_thread():
            # Do not release the daemon token while a heartbeat request could
            # still be queued; a late heartbeat must not recreate a closed lease.
            thread.join(timeout=2.0)

    def read_memory(self, addr: int, size: int) -> bytes:
        if self._daemon_proxy:
            if size < 0 or addr < 0 or addr + size > 0x100000000:
                raise ValueError("invalid PyOCD memory range")
            result = bytearray()
            for offset in range(0, size, 65536):
                count = min(size - offset, 65536)
                result.extend(self._daemon_call("read_memory", addr + offset, count))
            return bytes(result)
        return bytes(self.target.read_memory_block8(addr, size))

    def write_memory(self, addr: int, data: bytes):
        if self._daemon_proxy:
            data = bytes(data)
            if addr < 0 or addr + len(data) > 0x100000000:
                raise ValueError("invalid PyOCD memory range")
            for offset in range(0, len(data), 65536):
                self._daemon_call("write_memory", addr + offset, data[offset : offset + 65536])
            return None
        self.target.write_memory_block8(addr, data)

    def read_register(self, name: str) -> int:
        if self._daemon_proxy:
            return int(self._daemon_call("read_register", name))
        return int(self.target.read_core_register(name))

    def write_register(self, name: str, val: int):
        if self._daemon_proxy:
            return self._daemon_call("write_register", name, val)
        self.target.write_core_register(name, val)

    def set_frequency(self, freq: int):
        self._frequency_override = freq
        if self._daemon_proxy:
            if self._daemon_token:
                self._daemon_call("set_frequency", freq)
            return
        if self.session is not None:
            probe = self.session.probe
            if probe is not None:
                probe.set_clock(freq)

    def reset(self):
        if self._daemon_proxy:
            return self._daemon_call("reset")
        self.target.reset()

    def halt(self):
        if self._daemon_proxy:
            return self._daemon_call("halt")
        self.target.halt()

    def reset_and_halt(self):
        if self._daemon_proxy:
            return self._daemon_call("reset_and_halt")
        self.target.reset_and_halt()

    def resume(self):
        if self._daemon_proxy:
            return self._daemon_call("resume")
        self.target.resume()

    def _daemon_call(self, method: str, *arguments):
        from .daemon import call_target_session

        return call_target_session(self._daemon_token, method, list(arguments))

    def start_gdbserver(self, port, logging=True, blocking=True):
        if self._daemon_proxy:
            raise RuntimeError("PyOCD GDB server is not available through the daemon target proxy")
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

    def __init__(
        self,
        uri: str,
        *,
        frequency: int | None = None,
        timeout: float = 10.0,
        origin: str | None = None,
        operation: str = "GWProv remote session",
        lease_wait: float = 30.0,
        allow_recovery_marker: bool = False,
        daemon_managed: bool = False,
        daemon_proxy: bool = False,
    ):
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
        self._operation = operation
        self._daemon_proxy = daemon_proxy and not daemon_managed
        self._daemon_token: str | None = None
        self._daemon_heartbeat_stop = threading.Event()
        self._daemon_heartbeat_thread: threading.Thread | None = None
        self._lease = (
            None
            if daemon_managed or self._daemon_proxy
            else TargetLease(
                f"remote:{uri}",
                operation,
                wait=lease_wait,
                allow_recovery_marker=allow_recovery_marker,
            )
        )

    @property
    def probe_name(self) -> str:
        return f"{self._backend_name} ({urlparse(self.uri).netloc})"

    def open(self):
        if self._daemon_proxy:
            from .daemon import open_target_session

            opened = open_target_session(
                {
                    "url": self.uri,
                    "origin": self.origin,
                    "key": f"remote:{self.uri}",
                    "name": self.probe_name,
                },
                self._operation,
            )
            self._daemon_token = opened["token"]
            self._start_daemon_heartbeat()
            if self.frequency is not None:
                try:
                    self.set_frequency(self.frequency)
                except Exception:
                    self.close()
                    raise
            return self
        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            raise RuntimeError(
                'install remote support with `pip install "gwprov[remote]"`'
            ) from exc
        if self._lease is not None:
            self._lease.acquire()
        try:
            self._ws = connect(
                self.uri,
                open_timeout=self.timeout,
                close_timeout=self.timeout,
                max_size=self.MAX_MEMORY * 2 + 1024,
                origin=self.origin,
            )
            hello = json.loads(self._ws.recv(timeout=self.timeout))
            if (
                hello.get("type") != "hello"
                or hello.get("backend") != "gnwmanager"
                or hello.get("version") != self.PROTOCOL_VERSION
            ):
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
        if self._daemon_proxy:
            self._stop_daemon_heartbeat()
            token, self._daemon_token = self._daemon_token, None
            if token:
                from .daemon import close_target_session

                close_target_session(token)
            return
        ws, self._ws = self._ws, None
        try:
            if ws is not None:
                ws.close()
        finally:
            if self._lease is not None:
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

    def _daemon_call(self, method: str, *arguments):
        from .daemon import call_target_session

        return call_target_session(self._daemon_token, method, list(arguments))

    def _start_daemon_heartbeat(self):
        token = self._daemon_token
        if not token or (
            self._daemon_heartbeat_thread and self._daemon_heartbeat_thread.is_alive()
        ):
            return
        self._daemon_heartbeat_stop.clear()
        owner = {
            "key": f"remote:{self.uri}",
            "operation": self._operation,
            "pid": os.getpid(),
            "daemon_owned": True,
        }

        def heartbeat():
            from .daemon import heartbeat_proxy_session

            while not self._daemon_heartbeat_stop.wait(2.0):
                try:
                    heartbeat_proxy_session(token, owner)
                except Exception:
                    continue

        self._daemon_heartbeat_thread = threading.Thread(
            target=heartbeat, daemon=True, name="gwprov-remote-session-heartbeat"
        )
        self._daemon_heartbeat_thread.start()

    def _stop_daemon_heartbeat(self):
        self._daemon_heartbeat_stop.set()
        thread, self._daemon_heartbeat_thread = self._daemon_heartbeat_thread, None
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def read_memory(self, addr: int, size: int) -> bytes:
        if size < 0 or addr < 0 or addr + size > 0x100000000:
            raise ValueError("invalid remote memory range")
        if self._daemon_proxy:
            result = bytearray()
            for offset in range(0, size, self.MAX_MEMORY):
                count = min(size - offset, self.MAX_MEMORY)
                result.extend(self._daemon_call("read_memory", addr + offset, count))
            return bytes(result)
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
        if self._daemon_proxy:
            for offset in range(0, len(data), self.MAX_MEMORY):
                self._daemon_call(
                    "write_memory", addr + offset, data[offset : offset + self.MAX_MEMORY]
                )
            return None
        for offset in range(0, len(data), self.MAX_MEMORY):
            self._call(
                "write_memory", [addr + offset, data[offset : offset + self.MAX_MEMORY].hex()]
            )

    def read_register(self, name: str) -> int:
        if self._daemon_proxy:
            return int(self._daemon_call("read_register", name))
        return int(self._call("read_register", [name]))

    def write_register(self, name: str, val: int):
        if self._daemon_proxy:
            return self._daemon_call("write_register", name, val)
        self._call("write_register", [name, val])

    def set_frequency(self, freq: int):
        if self._daemon_proxy:
            return self._daemon_call("set_frequency", freq)
        self._call("set_frequency", [freq])

    def halt(self):
        if self._daemon_proxy:
            return self._daemon_call("halt")
        self._call("halt", [])

    def resume(self):
        if self._daemon_proxy:
            return self._daemon_call("resume")
        self._call("resume", [])

    def reset_and_halt(self):
        if self._daemon_proxy:
            return self._daemon_call("reset_and_halt")
        self._call("reset_and_halt", [])

    def reset(self):
        if self._daemon_proxy:
            return self._daemon_call("reset")
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
        unavailable = next(
            (
                line
                for line in captured
                if "no libusb" in line.casefold() or "not supported because" in line.casefold()
            ),
            None,
        )
        if unavailable:
            raise RuntimeError(f"PyOCD probe enumeration is unavailable: {unavailable}")
    rows = []
    for probe in probes:
        rows.append(
            {
                "id": str(getattr(probe, "unique_id", "") or ""),
                "name": str(getattr(probe, "product_name", "") or type(probe).__name__),
                "vendor": str(getattr(probe, "vendor_name", "") or ""),
                "product": str(getattr(probe, "product_name", "") or ""),
                "backend": "pyocd",
            }
        )
    return sorted(rows, key=lambda row: (row["name"].casefold(), row["id"]))


class SelectedOpenOCDBackend(OpenOCDBackend):
    """Open one explicit OpenOCD adapter without killing other sessions."""

    ADAPTERS = ("stlink", "jlink", "cmsis-dap", "rpi-gpio")

    def __init__(
        self,
        adapter: str,
        *,
        port: int | None = None,
        operation: str = "GWProv OpenOCD session",
        lease_wait: float = 30.0,
        allow_recovery_marker: bool = False,
    ):
        if adapter not in self.ADAPTERS:
            raise ValueError(
                f"unsupported OpenOCD adapter {adapter!r}; choose {', '.join(self.ADAPTERS)}"
            )
        if port is None:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
        super().__init__(port=port)
        self.adapter = adapter
        self._connected = False
        self._lease = TargetLease(
            f"adapter:{adapter}",
            operation,
            wait=lease_wait,
            allow_recovery_marker=allow_recovery_marker,
        )
        self._probe_leases = []
        self._allow_recovery_marker = allow_recovery_marker

    def open(self):
        from collections import deque
        from threading import Thread
        import subprocess
        import time

        from .target_leases import external_local_owner

        external = external_local_owner()
        if external and (
            external.get("adapter") is None or external.get("adapter") == self.adapter
        ):
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
                lease = TargetLease(
                    f"probe:{probe['id']}",
                    self._lease.operation,
                    wait=self._lease.wait,
                    allow_recovery_marker=self._allow_recovery_marker,
                )
                lease.acquire()
                self._probe_leases.append(lease)
            self._lease.acquire()
            command = next(
                (
                    cmd
                    for name, cmd in _openocd_launch_commands(self._address[1])
                    if name == self.adapter
                ),
                None,
            )
            if command is None:
                raise RuntimeError(f"gnwmanager has no OpenOCD config for {self.adapter}")
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._stderr_buffer = deque(maxlen=1000)
            self._openocd_process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            level = logging.INFO if os.environ.get("GNWMANAGER_OPENOCD_DEBUG") else logging.DEBUG
            self._stderr_thread = Thread(
                target=_drain_stderr,
                args=(self._openocd_process.stderr, self._stderr_buffer, level),
                daemon=True,
            )
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

    def __init__(
        self,
        *,
        operation: str = "GWProv OpenOCD session",
        port: int | None = None,
        allow_recovery_marker: bool = False,
    ):
        super().__init__()
        self.operation = operation
        self.port = port
        self.allow_recovery_marker = allow_recovery_marker
        self.backend = None
        self.version = (0, 0, 0)

    def open(self):
        failures = []
        for adapter in self.ADAPTERS:
            backend = SelectedOpenOCDBackend(
                adapter,
                port=self.port,
                operation=self.operation,
                lease_wait=0,
                allow_recovery_marker=self.allow_recovery_marker,
            )
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
        raise RuntimeError(
            "unable to open any supported OpenOCD adapter (" + "; ".join(failures) + ")"
        )

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
