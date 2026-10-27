"""Per-user GWProv daemon for managed GWemu and active target leases."""

from __future__ import annotations

import json
import hashlib
import base64
import importlib.metadata
import os
from pathlib import Path
import queue
import signal
import secrets
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any

from .daemon_ipc import DaemonServer, endpoint, request, runtime_directory

# Keep the service available while GWProv is in use. Device polling and
# registered adapters are not themselves client activity.
IDLE_EXIT_SECONDS = 10 * 60.0
LEASE_HEARTBEAT_TIMEOUT = 12.0
LEASE_CANCEL_GRACE_SECONDS = 2.0
DAEMON_LOG = runtime_directory() / "daemon.log"


class ManagedVM:
    """A GWemu child whose private QMP stream is carried over stdio."""

    def __init__(
        self,
        process: subprocess.Popen,
        profile: str,
        headless: bool,
        gdb_port: int | None,
        log_path: Path,
        cleanup_path: Path | None = None,
    ):
        self.process = process
        self.pid = process.pid
        self.profile = profile
        self.headless = headless
        self.gdb_port = gdb_port
        self.log_path = str(log_path)
        self.cleanup_path = cleanup_path
        self.created = time.time()
        self.timing = {"mode": "default", "cycleSemantics": "virtual-elapsed-time"}
        self._lock = threading.RLock()
        self._request_id = 0
        self._messages: queue.Queue[dict | None] = queue.Queue()
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("GWemu QMP stdio was not connected to GWProv")
        self._input = process.stdin
        self._output = process.stdout
        self._reader = threading.Thread(
            target=self._read_loop, daemon=True, name=f"gwprov-qmp-{self.pid}"
        )
        self._reader.start()
        greeting = self._read_message(timeout=10.0)
        if not isinstance(greeting.get("QMP"), dict):
            raise RuntimeError(f"GWemu did not send a QMP greeting: {greeting!r}")
        self._request_id += 1
        self._send({"execute": "qmp_capabilities", "id": self._request_id})
        self._read_reply(self._request_id, timeout=10.0)
        from .binary_identity import process_binary_identity

        self.binary_identity = process_binary_identity(process.pid)

    @property
    def qmp_uri(self) -> str:
        return f"gwprov://{self.pid}"

    def _send(self, message: dict) -> None:
        self._input.write(json.dumps(message, separators=(",", ":")).encode() + b"\r\n")
        self._input.flush()

    def _read_loop(self) -> None:
        try:
            for line in self._output:
                response = json.loads(line)
                if not isinstance(response, dict):
                    raise RuntimeError("GWemu returned a non-object QMP message")
                self._messages.put(response)
        except Exception as error:
            self._messages.put({"_gwprov_error": str(error)})
        finally:
            self._messages.put(None)

    def _read_message(self, *, timeout: float = 30.0) -> dict:
        try:
            response = self._messages.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError(f"timed out waiting for GWemu QMP (pid {self.pid})") from error
        if response is None:
            code = self.process.poll()
            raise RuntimeError(f"GWemu QMP stdio closed (exit status {code})")
        if "_gwprov_error" in response:
            raise RuntimeError(f"invalid GWemu QMP stream: {response['_gwprov_error']}")
        return response

    def _read_reply(self, request_id: int, *, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            response = self._read_message(timeout=max(0.0, deadline - time.monotonic()))
            if "event" in response:
                event = response.get("event")
                if event in {"STOP", "RESUME", "RESET", "SHUTDOWN", "SUSPEND"}:
                    from .qmp import record_control_request

                    record_control_request(self.qmp_uri, "qmp-event", str(event))
                continue
            if response.get("id") != request_id:
                raise RuntimeError(f"unexpected GWemu QMP reply: {response!r}")
            if "error" in response:
                raise RuntimeError(f"GWemu QMP command failed: {response['error']}")
            return response

    def execute(self, command: str, arguments: dict | None = None) -> dict:
        with self._lock:
            if self.process.poll() is not None:
                raise RuntimeError(f"GWemu pid {self.pid} has exited ({self.process.returncode})")
            self._request_id += 1
            message: dict[str, Any] = {"execute": command, "id": self._request_id}
            if arguments:
                message["arguments"] = arguments
            self._send(message)
            return self._read_reply(self._request_id)

    def row(self) -> dict:
        if self.process.poll() is not None:
            return {
                "pid": self.pid,
                "profile": self.profile,
                "status": "exited",
                "running": None,
                "halted": None,
                "qmpStatus": "unavailable",
                "qmpSocket": self.qmp_uri,
                "qmpHandle": self.qmp_uri,
                "qmpTransport": "daemon-stdio",
                "display": "headless" if self.headless else "visible",
                "gdbPort": self.gdb_port,
                "gdbEndpoint": f"127.0.0.1:{self.gdb_port}" if self.gdb_port else None,
                "gdbTransport": "tcp" if self.gdb_port else "unavailable",
                "gdbPid": self.pid if self.gdb_port else None,
                "created": self.created,
                "timing": self.timing,
                "binaryIdentity": self.binary_identity,
                "stateDetail": f"GWemu exited with status {self.process.returncode}",
            }
        result = self.execute("query-status").get("return", {})
        running = result.get("running")
        return {
            "pid": self.pid,
            "profile": self.profile,
            "status": "running" if running is True else "halted" if running is False else "unknown",
            "running": running,
            "halted": not running if isinstance(running, bool) else None,
            "qmpStatus": result.get("status", "unknown"),
            "qmpSocket": self.qmp_uri,
            "qmpHandle": self.qmp_uri,
            "qmpTransport": "daemon-stdio",
            "display": "headless" if self.headless else "visible",
            "gdbPort": self.gdb_port,
            "gdbEndpoint": f"127.0.0.1:{self.gdb_port}" if self.gdb_port else None,
            "gdbTransport": "tcp" if self.gdb_port else "unavailable",
            "gdbPid": self.pid if self.gdb_port else None,
            "created": self.created,
            "timing": self.timing,
            "binaryIdentity": self.binary_identity,
        }


class HardwarePoller:
    """Daemon-owned probe session with a five-second state heartbeat."""

    def __init__(self, probe: dict):
        self.probe = probe
        self.is_remote_gdb = bool(probe.get("remoteGdb"))
        self.key = probe.get("key") or (
            f"remote:{probe['url']}" if probe.get("url") else f"probe:{probe['id']}"
        )
        self.backend = None
        self.state: dict = {
            "id": probe.get("deviceId", self.key),
            "kind": "remote-gdb" if self.is_remote_gdb else "hardware",
            "backend": "Remote GDB" if self.is_remote_gdb else probe.get("backend", "pyocd"),
            "name": probe.get("name", "STM32"),
            "probeId": probe.get("id"),
            "targetProfile": probe.get("targetProfile"),
            "vendor": probe.get("vendor", ""),
            "status": "unknown",
            "application": "Unknown",
            "installed": "Unknown",
            "detail": "Waiting for daemon poll",
        }
        if self.is_remote_gdb:
            self.state.update(
                remoteGdb=True,
                gdbEndpoint=f"{probe.get('host')}:{probe.get('port')}",
                gdbHost=probe.get("host"),
                gdbPort=probe.get("port"),
                gdbUrl=probe.get("url"),
            )
        self.state_lock = threading.RLock()
        if probe.get("url") and not self.is_remote_gdb:
            self._update_state(
                backend="gnwmanager WebSocket",
                adapterName=probe.get("name"),
                remoteOrigin=probe.get("origin"),
            )
        self.last_poll = 0.0
        self.lock = threading.RLock()
        self.cached_banks: list[dict] | None = None
        self.banks_scanned_at = 0.0
        self.operation_lease = None
        # An external debugger owns the physical transport while this poller
        # remains the source of lifecycle state. Keep the last live target
        # observation visible until the external process exits; target reads
        # are intentionally suspended during the handoff.
        self.external_owner: dict | None = None
        # Set before handoff waits for this poller's lock. A poll that was
        # already selected from a stale service-level snapshot will then either
        # finish before handoff closes its backend or observe this flag before
        # opening a replacement connection.
        self.monitor_paused = False

    def poll(self, *, force: bool = False) -> None:
        with self.lock:
            if self.monitor_paused:
                return
            if self.release_for_external_session():
                return
            # A foreground CLI owns the interface while its process-shared
            # lease is held. This also protects a newly started daemon before
            # that CLI has re-registered its lease heartbeat.
            from .target_leases import lease_owner, operation_is_busy, operation_suppresses_poll

            owner = lease_owner(self.key)
            active_owner = owner if owner and owner.get("lease_active") else None
            if active_owner:
                if operation_is_busy(active_owner):
                    self._update_state(
                        status="busy",
                        busyOwner=active_owner,
                        detail=f"{active_owner.get('operation', 'device operation')} in progress",
                    )
                if operation_suppresses_poll(active_owner) or not active_owner.get("daemon_owned"):
                    # Keep the last known firmware classification visible while
                    # a direct CLI owns the adapter or a write/reset is in
                    # progress, but never contend with it. Daemon-proxied debug
                    # sessions are the one shared-connection exception.
                    self._update_state(polledAt=time.time())
                    self.last_poll = time.monotonic()
                    return
            # Daemon-proxied debug sessions share this connection and serialize
            # their calls with this poller's lock; passive reads continue between
            # debugger commands. Recovery and programming state are derived from
            # the target on every poll and are never represented by a durable
            # host-side marker.
            # Writes and resets suppress polling above. Locked-device stub
            # startup is separately guarded and verified below.
            if not active_owner or not operation_is_busy(active_owner):
                with self.state_lock:
                    self.state.pop("busyOwner", None)
                    self.state.pop("interruptedOperation", None)
            if self.backend is None:
                try:
                    if self.is_remote_gdb:
                        from .debug_shell import RemoteGDBBackend

                        self.backend = RemoteGDBBackend(
                            str(self.probe["host"]),
                            int(self.probe["port"]),
                            url=str(self.probe["url"]),
                            operation="GWProv daemon monitor",
                            daemon_managed=True,
                        )
                    elif self.probe.get("url"):
                        from .backends import WebSocketBackend

                        self.backend = WebSocketBackend(
                            self.probe["url"],
                            origin=self.probe.get("origin"),
                            operation="GWProv daemon monitor",
                            lease_wait=0,
                            allow_recovery_marker=True,
                            daemon_managed=True,
                        )
                    else:
                        from .backends import SelectedPyOCDBackend
                        from .target_leases import adapter_for_probe

                        self.backend = SelectedPyOCDBackend(
                            self.probe["id"],
                            operation="GWProv daemon monitor",
                            lease_wait=0,
                            probe_adapter=adapter_for_probe(
                                f"{self.probe.get('vendor', '')} {self.probe.get('name', '')}"
                            ),
                            allow_recovery_marker=True,
                            daemon_managed=True,
                            resume_on_disconnect=False,
                        )
                    self.backend.target_key = self.key
                    self.backend.open()
                    self.backend._lease = self.operation_lease
                except Exception as error:
                    self.backend = None
                    self._update_state(
                        status="unknown",
                        application="Unknown",
                        installed="Unknown",
                        detail=f"Probe poll unavailable: {error}",
                        polledAt=time.time(),
                    )
                    self.last_poll = time.monotonic()
                    return
            try:
                from .hw_lifecycle import identify_runtime

                rescan_banks = (
                    force
                    or self.cached_banks is None
                    or time.monotonic() - self.banks_scanned_at >= 90.0
                )
                runtime = identify_runtime(
                    self.backend, cached_banks=None if rescan_banks else self.cached_banks
                )
                if (
                    runtime.get("locked") is True
                    and not runtime.get("stubDetected")
                    and active_owner is None
                    and not self.is_remote_gdb
                ):
                    # Starting the RAM flasher resets the target and writes its
                    # image to SRAM. Hold a process-visible substantial-operation
                    # lease around that transition so daemon reload cannot kill
                    # the poller halfway through it.
                    from .target_leases import TargetLease

                    startup_lease = TargetLease(
                        self.key,
                        "gwprov stub start",
                        wait=0,
                        register_with_daemon=False,
                        owner_pid=os.getpid(),
                    ).acquire()
                    self._update_state(
                        status="busy",
                        busyOwner={
                            "key": self.key,
                            "operation": "gwprov stub start",
                            "pid": os.getpid(),
                        },
                        detail="Starting and verifying the RAM flasher",
                    )
                    previous_lease = getattr(self.backend, "_lease", None)
                    self.backend._lease = startup_lease
                    try:
                        runtime = identify_runtime(
                            self.backend,
                            start_locked_stub=True,
                            cached_banks=None if rescan_banks else self.cached_banks,
                        )
                    finally:
                        self.backend._lease = previous_lease
                        startup_lease.release()
                if rescan_banks and runtime.get("banks"):
                    self.cached_banks = runtime["banks"]
                    self.banks_scanned_at = time.monotonic()
                halted = runtime.get("halted")
                service_state = runtime.get("serviceState")
                detected_status = "unknown" if halted is None else "halted" if halted else "running"
                if service_state == "busy":
                    detected_status = "busy"
                elif service_state == "stalled":
                    detected_status = "unknown"
                values = dict(
                    status=(
                        "busy"
                        if (active_owner and operation_is_busy(active_owner))
                        or service_state == "busy"
                        else detected_status
                    ),
                    application=runtime.get("application", "Unknown"),
                    installed=runtime.get("installed", "Unknown"),
                    detail=runtime.get("detail"),
                    runtime=runtime,
                    polledAt=time.time(),
                )
                values.pop("externalOwner", None)
                values.pop("externalSession", None)
                values.pop("lastObserved", None)
                self.external_owner = None
                if active_owner and values["status"] == "busy":
                    values["busyOwner"] = active_owner
                self._update_state(**values)
            except Exception as error:
                self._update_state(
                    status="unknown",
                    application="Unknown",
                    installed="Unknown",
                    detail=f"Device poll failed: {error}",
                    polledAt=time.time(),
                )
                try:
                    # The target operation lease belongs to Service, even if this
                    # transport connection must be rebuilt after a read failure.
                    self.backend._lease = None
                    self.backend.close()
                except Exception:
                    pass
                self.backend = None
            if self.is_remote_gdb and self.backend is not None:
                # Raw GDB servers commonly allow one client at a time. Keep
                # the heartbeat connection only for the duration of this
                # sample so other debuggers can use the endpoint between polls.
                try:
                    self.backend.close()
                except Exception:
                    pass
                finally:
                    self.backend = None
            self.last_poll = time.monotonic()

    def invalidate_installed(self) -> None:
        self.cached_banks = None
        self.banks_scanned_at = 0.0

    def operation_finished(self) -> None:
        """Drop stale BUSY state and schedule a fresh observation after lease release."""
        with self.state_lock:
            self.state.pop("busyOwner", None)
            self.state.update(status="unknown", detail="Refreshing device state after operation")
        self.last_poll = 0.0

    def release_for_external_session(self) -> bool:
        """Yield a local adapter to gnwmanager/OpenOCD/PyOCD started outside GWProv."""
        if self.probe.get("url"):
            return False
        with self.lock:
            from .target_leases import adapter_for_probe, external_local_owner, lease_owner

            owner = lease_owner(self.key)
            if owner and owner.get("lease_active"):
                # A foreground GWProv operation owns the target for its duration.
                return False
            adapter = adapter_for_probe(
                f"{self.probe.get('vendor', '')} {self.probe.get('name', '')}"
            )
            external = external_local_owner(adapter)
            if external is None:
                if self.external_owner is None:
                    return False
                # The external owner disappeared. Preserve the last observed
                # runtime until the normal poll immediately refreshes it.
                self.external_owner = None
                self._update_state(
                    externalSession=None,
                    detail="External adapter session ended; refreshing device state",
                    polledAt=time.time(),
                )
                self.last_poll = 0.0
                return False
            now = time.time()
            if self.external_owner is None or self.external_owner.get("pid") != external.get("pid"):
                external = {**external, "started": now, "lastSeen": now}
            else:
                external = {
                    **external,
                    "started": self.external_owner.get("started", now),
                    "lastSeen": now,
                }
            self.external_owner = external
            with self.state_lock:
                last_observed = {
                    key: self.state.get(key)
                    for key in ("status", "application", "installed", "runtime", "polledAt")
                    if self.state.get(key) is not None
                }
            last_application = last_observed.get("application")
            cached_note = f"; last observed {last_application}" if last_application else ""
            if self.backend is None:
                self._update_state(
                    status="unknown",
                    lastObserved=last_observed,
                    externalOwner=external,
                    externalSession=external,
                    detail=(
                        f"Adapter owned by {external.get('operation', 'external debug session')} "
                        f"(pid {external.get('pid', 'unknown')}); target reads suspended"
                        f"{cached_note}"
                    ),
                    polledAt=now,
                )
                self.last_poll = time.monotonic()
                return True
            self.backend.close()
            self.backend = None
            self._update_state(
                status="unknown",
                lastObserved=last_observed,
                externalOwner=external,
                externalSession=external,
                detail=(
                    f"Adapter owned by {external.get('operation', 'external debug session')} "
                    f"(pid {external.get('pid', 'unknown')}); target reads suspended"
                    f"{cached_note}"
                ),
                polledAt=now,
            )
            self.last_poll = time.monotonic()
            return True

    def _update_state(self, **values) -> None:
        with self.state_lock:
            self.state.update(values)

    def handoff(self, *, wait: bool = True) -> bool:
        """Close the monitor connection before a foreground operation takes the probe.

        Shutdown uses a nonblocking handoff because a stuck adapter read must not
        keep an otherwise idle, stale daemon alive indefinitely. The process exit
        closes its remaining adapter handles at the OS boundary.
        """
        if not self.lock.acquire(blocking=wait):
            return False
        try:
            if self.backend is not None:
                self.backend.close()
                self.backend = None
            return True
        finally:
            self.lock.release()

    def snapshot(self) -> dict:
        with self.state_lock:
            return dict(self.state)

    def invoke(self, method: str, arguments: list):
        """Run one bounded backend/target call on the daemon-owned session."""
        with self.lock:
            if self.backend is None:
                self.poll()
            if self.backend is None:
                raise RuntimeError(self.state.get("detail", "target connection unavailable"))
            if method == "read_memory":
                address, size = arguments
                if (
                    not isinstance(address, int)
                    or not isinstance(size, int)
                    or not 0 <= size <= 4 * 1024 * 1024
                ):
                    raise ValueError("target memory reads are limited to 4 MiB per request")
                return base64.b64encode(self.backend.read_memory(address, size)).decode("ascii")
            if method == "write_memory":
                address, encoded = arguments
                data = base64.b64decode(encoded, validate=True)
                if len(data) > 4 * 1024 * 1024:
                    raise ValueError("target memory writes are limited to 4 MiB per request")
                self.backend.write_memory(address, data)
                return None
            if method in {"read_register", "read_core_register"}:
                return self.backend.read_register(str(arguments[0]))
            if method in {"write_register", "write_core_register"}:
                self.backend.write_register(str(arguments[0]), int(arguments[1]))
                return None
            if method in {"reset", "halt", "reset_and_halt", "resume"}:
                getattr(self.backend, method)()
                return None
            if method == "step":
                self.backend.target.step()
                return None
            if method == "set_frequency":
                self.backend.set_frequency(int(arguments[0]))
                return None
            if method == "get_state":
                from pyocd.core.target import Target

                state = self.backend.target.get_state()
                return getattr(state, "name", str(state))
            if method in {"set_breakpoint", "remove_breakpoint"}:
                from pyocd.core.target import Target

                address = int(arguments[0])
                if method == "remove_breakpoint":
                    self.backend.target.remove_breakpoint(address)
                    return True
                kind = Target.BreakpointType[arguments[1]]
                return bool(self.backend.target.set_breakpoint(address, kind))
            if method in {"set_watchpoint", "remove_watchpoint"}:
                from pyocd.core.target import Target

                address, size = int(arguments[0]), int(arguments[1])
                kind = Target.WatchpointType[arguments[2]]
                if method == "remove_watchpoint":
                    self.backend.target.remove_watchpoint(address, size, kind)
                    return True
                return bool(self.backend.target.set_watchpoint(address, size, kind))
            raise ValueError(f"unsupported daemon target operation: {method}")


class Service:
    def __init__(self):
        self.vms: dict[int, ManagedVM] = {}
        self.leases: dict[str, dict] = {}
        self.target_leases: dict[str, Any] = {}
        self.hardware: dict[str, HardwarePoller] = {}
        self.hardware_paused: set[str] = set()
        self.hardware_pause_tokens: dict[str, dict[str, dict[str, float | int]]] = {}
        self.lock = threading.RLock()
        self.discovery_lock = threading.Lock()
        self.last_busy = time.monotonic()
        self.shutdown_requested = threading.Event()
        self.draining = False
        self.cancelled_pids: dict[int, float] = {}
        self.cancel_lock = threading.RLock()
        self.heartbeat_updates: queue.Queue[tuple[str, dict]] = queue.Queue()
        self.last_discovery = 0.0
        self.discovery_detail: str | None = None
        # Capture this at process start. Computing it on each ping would report
        # files edited on disk even though this process still has old code in
        # memory, which is exactly the stale-daemon case clients need to see.
        self.identity = _daemon_identity()
        self.identity_check_at = time.monotonic() + 5.0
        self.server = DaemonServer(self.dispatch)

    def dispatch(self, message: dict) -> dict:
        operation = message.get("op")
        if operation == "lease-heartbeat":
            token, owner = message.get("token"), message.get("owner")
            if not isinstance(token, str) or not isinstance(owner, dict):
                raise ValueError("invalid hardware lease heartbeat")
            # A heartbeat must stay responsive even while another daemon
            # request is waiting on a slow adapter or VM operation.
            self.heartbeat_updates.put((token, owner))
            return {"ack": True}
        if operation == "hello":
            return {"daemonPid": os.getpid(), "protocol": 1}
        if operation == "ping":
            client_identity = message.get("clientIdentity")
            reload_required = (
                client_identity != self.identity if isinstance(client_identity, dict) else False
            )
            if reload_required:
                self.draining = True
            self.last_busy = time.monotonic()
            active_leases, blocking_leases = self._lease_counts()
            active_vms = sum(vm.process.poll() is None for vm in self.vms.copy().values())
            return {
                "daemonPid": os.getpid(),
                "daemonIdentity": self.identity,
                "reloadRequired": reload_required,
                "activeVMs": active_vms,
                "activeLeases": active_leases,
                "blockingLeases": blocking_leases,
                "capabilities": [
                    "gwemu-bin",
                    "process-binary-inode",
                    "timing-experimental-m7",
                    "timing-pacing",
                    "guest-profile-recording",
                ],
            }
        if operation in {"daemon-drain", "daemon-force-drain"}:
            self.draining = True
            self.last_busy = time.monotonic()
            cancelled = []
            if operation == "daemon-force-drain":
                from .target_leases import active_lease_owners, operation_blocks_drain

                for owner in active_lease_owners():
                    if not operation_blocks_drain(owner):
                        continue
                    pid = owner.get("pid")
                    if not isinstance(pid, int) or pid <= 1 or not _pid_exists(pid):
                        continue
                    if pid == os.getpid():
                        self.shutdown_requested.set()
                    else:
                        self._cancel_process(pid)
                        cancelled.append(pid)
            return {
                "draining": True,
                "cancelledPids": sorted(set(cancelled)),
                "blockingLeases": self._blocking_lease_count(),
            }
        if operation == "device-snapshot":
            return self.snapshot_devices(refresh=bool(message.get("refresh", False)))
        with self.lock:
            self._reap()
            if operation != "hello":
                self.last_busy = time.monotonic()
            if operation == "start-vm":
                if self.draining:
                    raise RuntimeError("GWProv daemon is draining to reload updated code")
                return self.start_vm(message)
            if operation == "list-vms":
                rows = []
                for pid, vm in list(self.vms.items()):
                    try:
                        rows.append(vm.row())
                    except (OSError, RuntimeError, ValueError) as error:
                        rows.append(
                            {
                                "pid": pid,
                                "profile": vm.profile,
                                "status": "unknown",
                                "running": None,
                                "halted": None,
                                "qmpStatus": "unavailable",
                                "qmpSocket": vm.qmp_uri,
                                "qmpHandle": vm.qmp_uri,
                                "qmpTransport": "daemon-stdio",
                                "display": "headless" if vm.headless else "visible",
                                "gdbPort": vm.gdb_port,
                                "created": vm.created,
                                "stateDetail": str(error),
                            }
                        )
                return {"instances": rows}
            if operation == "qmp":
                vm = self._vm(message.get("vm"))
                command = message.get("command")
                if not isinstance(command, str) or not command:
                    raise ValueError("QMP command must be a non-empty string")
                if command not in {
                    "query-status",
                    "screendump",
                    "stop",
                    "cont",
                    "quit",
                    "system_reset",
                    "input-send-event",
                    "send-key",
                    "human-monitor-command",
                }:
                    raise ValueError(f"QMP command is not exposed by GWProv: {command}")
                arguments = message.get("arguments")
                if arguments is not None and not isinstance(arguments, dict):
                    raise ValueError("QMP arguments must be an object")
                if command == "human-monitor-command":
                    command_line = arguments.get("command-line", "") if arguments else ""
                    import re

                    memory_read = re.fullmatch(r"xp /([1-9][0-9]*)wx 0x[0-9a-fA-F]+", command_line)
                    if command_line != "info registers" and not memory_read:
                        raise ValueError(
                            "GWProv permits only register reads and bounded word memory reads through HMP"
                        )
                    if memory_read and int(memory_read.group(1)) > 16384:
                        raise ValueError("GWProv HMP memory reads are limited to 64 KiB")
                return vm.execute(command, arguments)
            if operation == "lease-acquire":
                token, owner = message.get("token"), message.get("owner")
                if not isinstance(token, str) or not isinstance(owner, dict):
                    raise ValueError("invalid hardware lease registration")
                self.leases[token] = {**owner, "lastHeartbeat": time.monotonic()}
                return {"registered": True}
            if operation == "lease-release":
                owner = self.leases.pop(str(message.get("token")), None)
                if owner:
                    key = owner.get("key")
                    if isinstance(key, str):
                        poller = self.hardware.get(key)
                        if poller:
                            poller.invalidate_installed()
                            poller.operation_finished()
                self.last_busy = time.monotonic()
                return {"released": True}
            if operation == "device-watch":
                probes = message.get("probes", [])
                if not isinstance(probes, list):
                    raise ValueError("device watch probes must be a list")
                added = []
                for probe in probes:
                    if isinstance(probe, dict) and (
                        isinstance(probe.get("id"), str) or isinstance(probe.get("url"), str)
                    ):
                        key = probe.get("key") or (
                            f"remote:{probe['url']}" if probe.get("url") else f"probe:{probe['id']}"
                        )
                        if key not in self.hardware:
                            self.hardware[key] = self._new_hardware_poller(key, probe)
                            added.append(self.hardware[key])
                # On daemon start (or when an adapter is newly attached), do
                # the first observation before returning the initial ps view.
                for poller in added:
                    poller.poll()
                return {"devices": self._device_rows()}
            if operation == "target-open":
                if self.draining:
                    raise RuntimeError("GWProv daemon is draining to reload updated code")
                probe = message.get("probe")
                if not isinstance(probe, dict) or not (
                    isinstance(probe.get("id"), str) or isinstance(probe.get("url"), str)
                ):
                    raise ValueError("daemon target-open requires a probe ID or remote URL")
                key = probe.get("key") or (
                    f"remote:{probe['url']}" if probe.get("url") else f"probe:{probe['id']}"
                )
                from .target_leases import DeviceBusyError

                if any(owner.get("key") == key for owner in self.leases.values()):
                    owner = next(owner for owner in self.leases.values() if owner.get("key") == key)
                    raise DeviceBusyError(key, owner)
                if probe.get("id"):
                    from .target_leases import adapter_for_probe, external_local_owner

                    adapter = probe.get("adapter") or adapter_for_probe(
                        f"{probe.get('vendor', '')} {probe.get('name', '')}"
                    )
                    external = external_local_owner(adapter)
                    if external and (
                        external.get("adapter") is None
                        or adapter is None
                        or external.get("adapter") == adapter
                    ):
                        raise DeviceBusyError(key, external)
                # Classify the target and, when locked stock requires it, start
                # the verified RAM stub before this session's lease suppresses
                # that transition. A debug attach must never be the operation
                # that discovers it needs to initialize the service.
                poller = self.hardware.setdefault(key, self._new_hardware_poller(key, probe))
                poller.poll()
                if poller.backend is None:
                    raise RuntimeError(poller.state.get("detail", "target connection failed"))
                from .target_leases import TargetLease

                lease = TargetLease(
                    key,
                    str(message.get("operation", "hardware session")),
                    wait=0,
                    allow_recovery_marker=True,
                    register_with_daemon=False,
                    owner_pid=int(message.get("pid", 0)) or None,
                    daemon_owned=True,
                ).acquire()
                poller.operation_lease = lease
                if poller.backend is not None:
                    poller.backend._lease = lease
                try:
                    if poller.backend is None:
                        raise RuntimeError(poller.state.get("detail", "target connection failed"))
                except Exception:
                    poller.operation_lease = None
                    if poller.backend is not None:
                        poller.backend._lease = None
                    lease.release()
                    raise
                token = secrets.token_hex(16)
                owner = {
                    "key": key,
                    "operation": str(message.get("operation", "hardware session")),
                    "pid": int(message.get("pid", 0)),
                    "started": time.time(),
                    "daemon_owned": True,
                    "lastHeartbeat": time.monotonic(),
                }
                poller.backend._lease = lease
                self.target_leases[token] = lease
                self.leases[token] = owner
                self.last_busy = time.monotonic()
                return {"token": token, "device": poller.snapshot()}
            if operation == "target-call":
                token = str(message.get("token", ""))
                owner = self.leases.get(token)
                if not owner or not owner.get("daemon_owned"):
                    raise ValueError("daemon target session is no longer open")
                owner["lastHeartbeat"] = time.monotonic()
                poller = self.hardware.get(owner.get("key"))
                if poller is None:
                    raise RuntimeError("daemon target connection was removed")
                method, arguments = message.get("method"), message.get("arguments", [])
                if not isinstance(method, str) or not isinstance(arguments, list):
                    raise ValueError("invalid daemon target call")
                return {"result": poller.invoke(method, arguments)}
            if operation == "target-close":
                token = str(message.get("token", ""))
                owner = self.leases.pop(token, None)
                lease = self.target_leases.pop(token, None)
                if owner:
                    poller = self.hardware.get(owner.get("key"))
                    if poller:
                        poller.invalidate_installed()
                        poller.operation_lease = None
                        if poller.backend and getattr(poller.backend, "_lease", None) is lease:
                            poller.backend._lease = None
                        poller.operation_finished()
                if lease:
                    lease.release()
                self.last_busy = time.monotonic()
                return {"released": True}
            if operation == "target-pause":
                key = message.get("key")
                pause_token = message.get("pauseToken")
                pid = message.get("pid")
                wait_seconds = message.get("waitSeconds", 30)
                if (
                    isinstance(key, str)
                    and isinstance(pause_token, str)
                    and isinstance(pid, int)
                    and pid > 1
                ):
                    shared_session = next(
                        (
                            owner
                            for owner in self.leases.values()
                            if owner.get("key") == key and owner.get("daemon_owned")
                        ),
                        None,
                    )
                    if shared_session:
                        # A debugger session is actively using the daemon-owned
                        # connection. Do not close its backend during a competing
                        # client's handoff request: backend.close() would release
                        # the debugger's process-shared target lease.
                        return {"paused": False, "busyOwner": shared_session}
                    if not isinstance(wait_seconds, (int, float)):
                        wait_seconds = 30.0
                    timeout = max(1.0, min(3600.0, float(wait_seconds) + 10.0))
                    self.hardware_pause_tokens.setdefault(key, {})[pause_token] = {
                        "pid": pid,
                        "since": time.monotonic(),
                        "expires": time.monotonic() + timeout,
                    }
                    self.hardware_paused.add(key)
                    poller = self.hardware.get(key)
                    if poller:
                        # Publish the pause before taking the poller lock. If
                        # the passive loop had copied an unpaused snapshot, its
                        # eventual poll() entry must still see this handoff.
                        poller.monitor_paused = True
                        poller.handoff()
                return {"paused": isinstance(key, str) and key in self.hardware_paused}
            if operation == "target-resume":
                key = message.get("key")
                pause_token = message.get("pauseToken")
                if isinstance(key, str) and isinstance(pause_token, str):
                    tokens = self.hardware_pause_tokens.get(key)
                    if tokens:
                        tokens.pop(pause_token, None)
                        if not tokens:
                            self.hardware_pause_tokens.pop(key, None)
                            self.hardware_paused.discard(key)
                    poller = self.hardware.get(key)
                    if poller:
                        poller.monitor_paused = key in self.hardware_paused
                        poller.invalidate_installed()
                        poller.last_poll = 0.0
                        poller.operation_finished()
                return {"resumed": isinstance(key, str)}
            if operation == "quit-vm":
                vm = self._vm(message.get("vm"))
                vm.execute("quit")
                return {"stopping": True}
            raise ValueError(f"unsupported daemon operation: {operation!r}")

    def _blocking_lease_count(self) -> int:
        """Count live substantial operations from process-held OS leases."""
        from .target_leases import active_lease_owners, operation_blocks_drain

        count = 0
        for owner in active_lease_owners():
            pid = owner.get("pid")
            if operation_blocks_drain(owner) and isinstance(pid, int) and _pid_exists(pid):
                count += 1
        return count

    def _lease_counts(self) -> tuple[int, int]:
        from .target_leases import active_lease_owners, operation_blocks_drain

        owners = active_lease_owners()
        live = [
            owner
            for owner in owners
            if isinstance(owner.get("pid"), int) and _pid_exists(owner["pid"])
        ]
        return len(live), sum(operation_blocks_drain(owner) for owner in live)

    def _new_hardware_poller(self, key: str, probe: dict) -> HardwarePoller:
        poller = HardwarePoller(probe)
        poller.monitor_paused = key in self.hardware_paused
        return poller

    def _device_rows(self) -> list[dict]:
        rows = [poller.snapshot() for poller in self.hardware.copy().values()]
        if self.discovery_detail:
            rows.append(
                {
                    "id": "hardware:*",
                    "kind": "hardware",
                    "backend": "GWProv daemon",
                    "name": "Hardware discovery",
                    "status": "unknown",
                    "application": "Unknown",
                    "installed": "Unknown",
                    "detail": self.discovery_detail,
                }
            )
        return rows

    def snapshot_devices(self, *, refresh: bool = False) -> dict:
        """Serve cached state without waiting on unrelated target operations."""
        # The snapshot request is handled outside the normal request lock so it
        # stays responsive while an unrelated VM operation is running. Still
        # reconcile dead lease owners here: the passive poll loop intentionally
        # does not take that lock, and an abandoned target-pause token would
        # otherwise leave a remote adapter permanently unpolled after its
        # debugger client exits.
        with self.lock:
            self._reap()
        if not self.last_discovery or refresh:
            # The first inventory request after lazy startup waits for the
            # daemon's initial adapter discovery and state poll. Discovery is
            # single-flight and rate-limited; --live forces the state poll
            # below without launching duplicate USB enumerations.
            self._discover_hardware(poll_new=not refresh)
        self.last_busy = time.monotonic()
        pollers = list(self.hardware.copy().values())
        if refresh:
            for poller in pollers:
                if poller.key not in self.hardware_paused:
                    poller.poll(force=True)
        states = {poller.key: poller.snapshot() for poller in pollers}
        from .target_leases import active_lease_owners, operation_is_busy

        for owner in active_lease_owners():
            key = owner.get("key")
            if key in states and operation_is_busy(owner):
                states[key].update(
                    status="busy",
                    busyOwner=owner,
                    detail=f"{owner.get('operation', 'device operation')} in progress",
                )
        rows = list(states.values())
        if self.discovery_detail:
            rows.append(
                {
                    "id": "hardware:*",
                    "kind": "hardware",
                    "backend": "GWProv daemon",
                    "name": "Hardware discovery",
                    "status": "unknown",
                    "application": "Unknown",
                    "installed": "Unknown",
                    "detail": self.discovery_detail,
                }
            )
        return {"devices": rows}

    def start_vm(self, message: dict) -> dict:
        from .launch import image_launch_spec, profile_launch_spec

        profile_value = message.get("profile")
        cleanup_path = None
        headless = bool(message.get("headless", False))
        audio = bool(message.get("audio", False))
        gdb_port = message.get("gdb_port")
        if gdb_port is not None:
            from .ports import validate_requested_port

            gdb_port = validate_requested_port(gdb_port)
        if profile_value:
            from .profiles import DeviceProfile

            profile = str(DeviceProfile.load(profile_value).root)
            if any(vm.profile == profile and vm.process.poll() is None for vm in self.vms.values()):
                from .profiles import profile_reference

                raise ValueError(
                    f"profile {profile_reference(profile)} already has a managed GWemu instance"
                )
            cmd, cwd, env, log_path = profile_launch_spec(
                profile,
                headless=headless,
                audio=audio,
                timeline=message.get("timeline"),
                record_timeline=message.get("record_timeline"),
                record_profile=message.get("record_profile"),
                profile_interval_ms=message.get("profile_interval_ms"),
                profile_symbols=message.get("profile_symbols"),
                gdb_port=gdb_port,
                qmp_stdio=True,
                start_halted=bool(message.get("start_halted", False)),
                shared_sd_root=message.get("shared_sd_root"),
                timing_mode=message.get("timing_mode", "default"),
                icount=message.get("icount"),
                pacing=message.get("pacing"),
                rtc_epoch=message.get("rtc_epoch"),
                gwemu_bin=message.get("gwemu_bin"),
            )
        else:
            profile = "raw image"
            cmd, cwd, env, log_path, cleanup_path = image_launch_spec(
                message.get("image", {}),
                headless=headless,
                audio=audio,
                timeline=message.get("timeline"),
                record_timeline=message.get("record_timeline"),
                record_profile=message.get("record_profile"),
                profile_interval_ms=message.get("profile_interval_ms"),
                profile_symbols=message.get("profile_symbols"),
                gdb_port=gdb_port,
                icount=message.get("icount"),
                keep_temp=bool(message.get("keep_temp", False)),
                timing_mode=message.get("timing_mode", "default"),
                pacing=message.get("pacing"),
                rtc_epoch=message.get("rtc_epoch"),
                gwemu_bin=message.get("gwemu_bin"),
            )
        from .binary_identity import file_identity

        expected_binary = file_identity(cmd[0]) if message.get("gwemu_bin") else None
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_path.open("w") as log:
                process = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=log,
                    bufsize=0,
                )
            vm = ManagedVM(process, profile, headless, gdb_port, log_path, cleanup_path)
            if expected_binary and vm.binary_identity["sha256"] != expected_binary["sha256"]:
                raise RuntimeError("GWemu executable changed between launch validation and startup")
            with log_path.open("r", errors="replace") as startup_log:
                for line in startup_log.read(8192).splitlines():
                    if line.startswith("gwemu_version:"):
                        vm.binary_identity["version"] = line.partition(":")[2].strip()
                        break
            mode = message.get("timing_mode", "default")
            pacing = message.get("pacing") or ("realtime" if mode == "baseline" else "unthrottled")
            vm.timing = {
                "mode": mode,
                "pacing": pacing,
                "icountShift": (
                    0 if mode in ("baseline", "experimental-m7") else message.get("icount")
                ),
                "rtcEpoch": message.get("rtc_epoch"),
                "cycleSemantics": (
                    "one-instruction-one-cycle"
                    if mode == "baseline"
                    else (
                        "experimental-m7-issue-dependency-model"
                        if mode == "experimental-m7"
                        else "virtual-elapsed-time"
                    )
                ),
                "hardwareCycleAccurate": False,
            }
        except Exception:
            process = locals().get("process")
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if cleanup_path:
                shutil.rmtree(cleanup_path, ignore_errors=True)
            raise
        self.vms[process.pid] = vm
        self.last_busy = time.monotonic()
        return {"instance": vm.row()}

    def _vm(self, pid: Any) -> ManagedVM:
        try:
            vm = self.vms[int(pid)]
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"GWemu pid {pid!r} is not managed by this daemon") from error
        if vm.process.poll() is not None:
            self.vms.pop(vm.pid, None)
            raise ValueError(f"GWemu pid {vm.pid} has exited")
        return vm

    def _reap(self) -> None:
        while True:
            try:
                token, owner = self.heartbeat_updates.get_nowait()
            except queue.Empty:
                break
            current = self.leases.get(token)
            if current is None:
                self.leases[token] = {**owner, "lastHeartbeat": time.monotonic()}
            else:
                current["lastHeartbeat"] = time.monotonic()
            self.last_busy = time.monotonic()
        for pid, vm in list(self.vms.items()):
            if vm.process.poll() is not None:
                self.vms.pop(pid, None)
                if vm.cleanup_path:
                    shutil.rmtree(vm.cleanup_path, ignore_errors=True)
        for token, owner in list(self.leases.items()):
            pid = owner.get("pid")
            heartbeat_at = owner.get("lastHeartbeat", 0.0)
            if (
                isinstance(heartbeat_at, (int, float))
                and time.monotonic() - heartbeat_at > LEASE_HEARTBEAT_TIMEOUT
                and isinstance(pid, int)
                and _pid_exists(pid)
            ):
                self._cancel_process(pid)
            if not isinstance(pid, int) or not _pid_exists(pid):
                self.leases.pop(token, None)
                poller = self.hardware.get(owner.get("key"))
                if poller:
                    poller.invalidate_installed()
                lease = self.target_leases.pop(token, None)
                if lease:
                    poller = self.hardware.get(owner.get("key"))
                    if poller:
                        poller.operation_lease = None
                        if poller.backend and getattr(poller.backend, "_lease", None) is lease:
                            poller.backend._lease = None
                    lease.release()

        # A client can die after asking the monitor to hand off but before it
        # obtains/registers its OS lease. Pause tokens are process-scoped and
        # expire independently so that abandoned handoffs cannot stop polling
        # forever. Keep the monitor paused while any process still owns the OS
        # lease, including a second client waiting for the same target.
        from .target_leases import lease_owner

        for key, tokens in list(self.hardware_pause_tokens.items()):
            current_owner = lease_owner(key)
            if current_owner and current_owner.get("lease_active"):
                continue
            for pause_token, info in list(tokens.items()):
                pid = info.get("pid")
                expires = info.get("expires", 0.0)
                process_alive = isinstance(pid, int) and _pid_exists(pid)
                expired = isinstance(expires, (int, float)) and time.monotonic() >= expires
                if not process_alive or expired:
                    tokens.pop(pause_token, None)
            if not tokens:
                self.hardware_pause_tokens.pop(key, None)
                self.hardware_paused.discard(key)
                poller = self.hardware.get(key)
                if poller:
                    poller.monitor_paused = False
                    poller.invalidate_installed()
                    poller.last_poll = 0.0
                    poller.operation_finished()

        with self.cancel_lock:
            for pid, requested_at in list(self.cancelled_pids.items()):
                if not _pid_exists(pid):
                    self.cancelled_pids.pop(pid, None)
                elif time.monotonic() - requested_at >= LEASE_CANCEL_GRACE_SECONDS:
                    try:
                        os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
                    self.cancelled_pids.pop(pid, None)

    def _cancel_process(self, pid: int) -> None:
        with self.cancel_lock:
            if pid in self.cancelled_pids:
                return
            try:
                os.kill(pid, signal.SIGTERM)
                self.cancelled_pids[pid] = time.monotonic()
            except (OSError, ProcessLookupError):
                pass

    def run(self) -> None:
        for signum in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
            if signum is not None:
                signal.signal(signum, lambda *_: self.shutdown_requested.set())
        server_thread = threading.Thread(target=self.server.serve, daemon=True)
        server_thread.start()
        poll_thread = threading.Thread(
            target=self._poll_hardware, daemon=True, name="gwprov-device-heartbeat"
        )
        poll_thread.start()
        try:
            while server_thread.is_alive():
                if self.shutdown_requested.is_set():
                    break
                time.sleep(0.25)
                # A CLI ping used to be the only thing that noticed code had
                # changed. That left an idle daemon resident indefinitely
                # during development, and meant its next client had to perform
                # recovery synchronously. Watch the loaded package ourselves;
                # once it differs, drain as soon as protected work is done.
                if time.monotonic() >= self.identity_check_at:
                    self.identity_check_at = time.monotonic() + 5.0
                    try:
                        if _daemon_identity() != self.identity:
                            self.draining = True
                    except OSError:
                        # A package update can briefly replace files while the
                        # source tree is being installed. Retry on the next
                        # interval rather than terminating on a partial read.
                        pass
                with self.lock:
                    self._reap()
                    blocking_leases = self._blocking_lease_count() > 0
                    if self.draining and not self.vms and not blocking_leases:
                        self.server.stop()
                        break
                    if (
                        not self.vms
                        and not blocking_leases
                        and time.monotonic() - self.last_busy >= IDLE_EXIT_SECONDS
                    ):
                        self.server.stop()
                        break
        finally:
            self.server.stop()
            server_thread.join(timeout=2)
            poll_thread.join(timeout=1)

            def close_monitors():
                for poller in self.hardware.values():
                    try:
                        poller.handoff(wait=False)
                    except Exception:
                        pass

            close_thread = threading.Thread(
                target=close_monitors, daemon=True, name="gwprov-monitor-shutdown"
            )
            close_thread.start()
            close_thread.join(timeout=1)
            for lease in self.target_leases.values():
                try:
                    lease.release()
                except Exception:
                    pass
            self._stop_children()

    def _poll_hardware(self) -> None:
        """Discover adapters and poll each daemon-owned target every five seconds."""
        while not self.shutdown_requested.wait(0.25):
            # Draining refuses new sessions and waits for protected operations,
            # but those operations can remain interactive for a while (for
            # example, a Python debug console). Keep the passive heartbeat alive
            # until shutdown is actually requested so device state does not go
            # stale during that drain window.
            now = time.monotonic()
            if now - self.last_discovery >= 5.0:
                self._discover_hardware()
            with self.lock:
                pollers = list(self.hardware.values())
                paused = set(self.hardware_paused)
                leases = list(self.leases.values())
            now = time.monotonic()
            for poller in pollers:
                # Do this check independently of the five-second state poll so
                # another tool can acquire the adapter promptly after starting.
                if poller.release_for_external_session():
                    continue
                if poller.key in paused or now - poller.last_poll < 5.0:
                    continue
                from .target_leases import operation_suppresses_poll

                if any(
                    owner.get("key") == poller.key and operation_suppresses_poll(owner)
                    for owner in leases
                ):
                    continue
                poller.poll()

    def _discover_hardware(self, *, poll_new: bool = True) -> None:
        """Refresh local probe and configured remote-adapter inventory."""
        with self.discovery_lock:
            # `watch -n0 gwprov ps` can issue many concurrent inventory calls.
            # Recheck after taking the lock so they share one probe enumeration
            # instead of launching a burst of libusb scans and handler threads.
            if self.last_discovery and time.monotonic() - self.last_discovery < 5.0:
                return
            probes = []
            errors = []
            local_discovery_ok = True
            remote_discovery_ok = True
            gdb_discovery_ok = True
            try:
                from .backends import enumerate_local_probes

                probes.extend(enumerate_local_probes())
            except (RuntimeError, OSError) as error:
                local_discovery_ok = False
                errors.append(f"Local probe discovery failed: {error}")
            try:
                from .adapters import list_remote

                probes.extend(
                    [{**remote, "key": f"remote:{remote['url']}"} for remote in list_remote()]
                )
            except (RuntimeError, OSError, ValueError) as error:
                remote_discovery_ok = False
                errors.append(f"Remote adapter discovery failed: {error}")
            try:
                from .adapters import list_gdb

                probes.extend(
                    [
                        {
                            **remote,
                            "remoteGdb": True,
                            "deviceId": f"gdb:{remote['host']}:{remote['port']}",
                            "key": f"remote-gdb:{remote['url']}",
                        }
                        for remote in list_gdb()
                    ]
                )
            except (RuntimeError, OSError, ValueError) as error:
                gdb_discovery_ok = False
                errors.append(f"Remote GDB discovery failed: {error}")
            added = []
            present: set[str] = set()
            with self.lock:
                self.last_discovery = time.monotonic()
                self.discovery_detail = "; ".join(errors) if errors else None
                for probe in probes:
                    if not isinstance(probe, dict):
                        continue
                    if isinstance(probe.get("url"), str):
                        key = probe.get("key") or f"remote:{probe['url']}"
                    elif isinstance(probe.get("id"), str):
                        key = probe.get("key") or f"probe:{probe['id']}"
                    else:
                        continue
                    present.add(key)
                    if key not in self.hardware:
                        self.hardware[key] = self._new_hardware_poller(key, probe)
                        added.append(self.hardware[key])
                # Reconcile detached probes and removed remote endpoints. A failed
                # transport-wide inventory is not evidence that its devices vanished.
                # Active target sessions retain their row until the owning process
                # releases its lease; this prevents discovery from orphaning a session.
                for key, poller in list(self.hardware.items()):
                    if key in present:
                        continue
                    is_gdb = bool(poller.probe.get("remoteGdb"))
                    is_remote = bool(poller.probe.get("url")) and not is_gdb
                    if (
                        (is_gdb and not gdb_discovery_ok)
                        or (is_remote and not remote_discovery_ok)
                        or (not is_remote and not is_gdb and not local_discovery_ok)
                    ):
                        continue
                    from .target_leases import lease_owner

                    owner = lease_owner(key)
                    if owner and owner.get("lease_active"):
                        from .target_leases import operation_is_busy

                        if operation_is_busy(owner):
                            poller._update_state(
                                status="busy",
                                busyOwner=owner,
                                detail="Adapter is not currently detected; its active operation still owns the target",
                            )
                        else:
                            poller._update_state(
                                status="unknown",
                                detail="Adapter is not currently detected; its active session still owns the target",
                            )
                        continue
                    if poller.handoff(wait=False):
                        self.hardware.pop(key, None)
        if poll_new:
            for poller in added:
                poller.poll()

    def _stop_children(self) -> None:
        for vm in list(self.vms.values()):
            if vm.process.poll() is None:
                try:
                    vm.execute("quit")
                except (OSError, RuntimeError, ValueError):
                    pass
        deadline = time.monotonic() + 3.0
        for vm in list(self.vms.values()):
            remaining = max(0.0, deadline - time.monotonic())
            try:
                vm.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                vm.process.terminate()
                try:
                    vm.process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    vm.process.kill()
                    vm.process.wait()
            if vm.cleanup_path:
                shutil.rmtree(vm.cleanup_path, ignore_errors=True)


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        handle = ctypes.WinDLL("kernel32", use_last_error=True).OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        return True
    if os.path.isdir(f"/proc/{pid}"):
        try:
            stat_data = Path(f"/proc/{pid}/stat").read_text()
            remainder = stat_data.rpartition(")")[2].strip().split()
            if remainder and remainder[0] == "Z":
                return False
        except (OSError, IndexError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _daemon_identity() -> dict[str, str]:
    """Identify the installed version and Python source tree loaded at startup."""
    package_root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for source in sorted(package_root.rglob("*.py")):
        relative = source.relative_to(package_root).as_posix().encode("utf-8")
        content = source.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    try:
        package_version = importlib.metadata.version("gwprov")
    except importlib.metadata.PackageNotFoundError:
        package_version = "0.0.0"
    return {
        "version": package_version,
        "source": str(package_root),
        "sourceSha256": digest.hexdigest(),
    }


def _check_daemon_identity(response: dict) -> None:
    if "daemonIdentity" not in response:
        raise RuntimeError(
            "GWProv daemon does not report its code version "
            f"(pid {response.get('daemonPid', 'unknown')}); it predates daemon source "
            "identity reporting. Let active VM or hardware operations finish so it can "
            "exit, then retry. If it remains after those operations have ended, restart "
            "the GWProv daemon process."
        )
    identity = response.get("daemonIdentity")
    if not isinstance(identity, dict):
        raise RuntimeError(
            "GWProv daemon returned invalid code identity metadata "
            f"(pid {response.get('daemonPid', 'unknown')}); restart it and retry."
        )
    current = _daemon_identity()
    if not response.get("reloadRequired") and identity == current:
        return
    raise RuntimeError(
        "GWProv daemon is running stale code "
        f"(pid {response.get('daemonPid')}, version {identity.get('version', 'unknown')}, "
        f"source {identity.get('sourceSha256', 'unknown')[:12]}). "
        f"Current code is version {current['version']}; the daemon is draining and will "
        "restart when its active GWemu or substantial hardware operations finish. "
        f"It reports {response.get('activeVMs', 0)} active VM(s) and "
        f"{response.get('blockingLeases', response.get('activeLeases', 0))} "
        "blocking hardware operation(s)."
    )


def _is_gwprov_daemon(pid: int) -> bool:
    """Verify a stale PID belongs to this user's GWProv daemon before signaling it."""
    if pid <= 1 or pid == os.getpid():
        return False
    if os.path.isdir(f"/proc/{pid}"):
        try:
            arguments = [
                part.decode(errors="replace")
                for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                if part
            ]
        except OSError:
            return False
        # Match actual argv tokens. Substring matching can mistake a shell or
        # Python helper whose source text mentions `gwprov.daemon --serve` for
        # the daemon and would make a stale-process cleanup unsafe.
        return (
            len(arguments) >= 4
            and arguments[1:3] == ["-m", "gwprov.daemon"]
            and "--serve" in arguments[3:]
        )
    if os.name == "nt":
        # The named pipe is per-user and the PID came from its authenticated
        # daemon response. Signal handling is performed by the CLI service API.
        return True
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        command = result.stdout
        return "gwprov.daemon" in command and "--serve" in command
    except (OSError, subprocess.TimeoutExpired):
        return False


def _daemon_children(pid: int) -> list[int]:
    """Return direct child PIDs on Linux, used to protect daemon-owned VMs."""
    children_path = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        return [int(value) for value in children_path.read_text().split()]
    except (OSError, ValueError):
        return []


def _unresponsive_daemon_pids() -> list[int]:
    """Find same-user daemon processes when its IPC endpoint no longer answers."""
    if not hasattr(os, "getuid") or not Path("/proc").is_dir():
        return []
    endpoint_path = Path(runtime_directory()) / "daemon.sock"
    if not endpoint_path.exists():
        return []
    matches = []
    try:
        entries = Path("/proc").iterdir()
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid() or not _is_gwprov_daemon(pid):
                continue
        except OSError:
            continue
        matches.append(pid)
    return matches


def _stop_unresponsive_daemon(pid: int, *, timeout: float) -> None:
    """Replace a hung daemon only when no live operation or managed child exists."""
    from .target_leases import active_lease_owners, operation_blocks_drain

    owners = [
        owner
        for owner in active_lease_owners()
        if owner.get("pid") != os.getpid()
        and isinstance(owner.get("pid"), int)
        and _pid_exists(owner["pid"])
        and operation_blocks_drain(owner)
    ]
    if owners:
        operations = ", ".join(
            f"{owner.get('operation', 'device operation')} (pid {owner['pid']})" for owner in owners
        )
        raise RuntimeError(
            f"GWProv daemon pid {pid} is unresponsive while protected operation(s) are active: "
            f"{operations}. Let them finish or cancel them before retrying."
        )
    children = [child for child in _daemon_children(pid) if _pid_exists(child)]
    if children:
        raise RuntimeError(
            f"GWProv daemon pid {pid} is unresponsive and still owns child process(es) "
            f"{', '.join(map(str, children))}; stop those managed instances before retrying."
        )
    _stop_stale_daemon(pid, timeout=min(timeout, 1.0), force=True)


def _stop_stale_daemon(pid: int, *, timeout: float = 8.0, force: bool = False) -> None:
    if not _pid_exists(pid):
        return
    if not _is_gwprov_daemon(pid):
        raise RuntimeError(f"refusing to stop pid {pid}: it is not a verified GWProv daemon")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return
        time.sleep(0.05)
    if force and os.name != "nt":
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and _pid_exists(pid):
            time.sleep(0.05)
    if _pid_exists(pid):
        if force:
            raise RuntimeError(
                f"stale GWProv daemon pid {pid} did not stop after forced termination"
            )
        raise RuntimeError(
            f"stale GWProv daemon pid {pid} did not stop cleanly; "
            "it may still be finishing a device poll. Retry or use `gwprov daemon restart --force`."
        )


def ensure_running(*, timeout: float = 8.0) -> None:
    """Connect to the daemon, starting one if this is the first active client."""
    with _startup_lock():
        try:
            response = request({"op": "ping", "clientIdentity": _daemon_identity()}, timeout=0.4)
        except (OSError, RuntimeError, ValueError):
            response = None
        if response is not None:
            # Older daemon builds ignored clientIdentity and therefore never
            # set reloadRequired. Treat missing identity as stale too, then
            # apply the same process-held lease and child-process safeguards.
            reported_identity = response.get("daemonIdentity")
            stale = (
                response.get("reloadRequired")
                or not isinstance(reported_identity, dict)
                or reported_identity != _daemon_identity()
            )
            if stale:
                if int(response.get("activeVMs", 0)):
                    _check_daemon_identity(response)
                daemon_pid = int(response.get("daemonPid", 0))
                children = [child for child in _daemon_children(daemon_pid) if _pid_exists(child)]
                if children:
                    raise RuntimeError(
                        f"stale GWProv daemon pid {daemon_pid} still owns child process(es) "
                        f"{', '.join(map(str, children))}; stop those managed instances before "
                        "the daemon can reload."
                    )
                from .target_leases import active_lease_owners, operation_blocks_drain

                # A backend acquires its OS lease before registering with the
                # service. Ignore this caller's just-acquired lease here: it
                # has not opened the adapter yet, and restarting the stale
                # monitor is the next step in that same attach sequence.
                owners = [
                    owner for owner in active_lease_owners() if owner.get("pid") != os.getpid()
                ]
                # Trust live process-held leases, not a stale daemon's in-memory
                # lease count. The OS lock remains authoritative across daemon
                # upgrades and lets us ignore abandoned lease entries from old
                # code. A process that has exited no longer protects an operation.
                blocking = sum(
                    operation_blocks_drain(owner)
                    and isinstance(owner.get("pid"), int)
                    and _pid_exists(owner["pid"])
                    for owner in owners
                )
                if blocking:
                    raise RuntimeError(
                        f"stale GWProv daemon pid {daemon_pid} is waiting for "
                        f"{blocking} substantial device operation(s) to release their lease. "
                        "Run `gwprov daemon restart --force` to cancel them."
                    )
                # With no live VM or substantial process lease, a daemon stuck
                # in a passive adapter poll must not trap every subsequent CLI
                # command. Give it a short graceful window, then reap this
                # verified stale daemon so the current version can start.
                _stop_stale_daemon(daemon_pid, timeout=min(timeout, 2.0), force=True)
                response = None
            else:
                _check_daemon_identity(response)
                return
        else:
            # An accept loop can remain alive while a handler or adapter poll is
            # wedged. Starting another daemon then only produces a socket race.
            # Recover only a verified same-user daemon, and preserve any active
            # write/debug lease or daemon-owned child process.
            candidates = _unresponsive_daemon_pids()
            if len(candidates) > 1:
                raise RuntimeError(
                    "multiple unresponsive GWProv daemon processes own this user's socket: "
                    f"{', '.join(map(str, candidates))}; inspect them before retrying"
                )
            if candidates:
                _stop_unresponsive_daemon(candidates[0], timeout=timeout)
        log_path = runtime_directory() / "daemon.log"
        log = log_path.open("ab")
        child_env = dict(os.environ)
        package_root = str(Path(__file__).resolve().parent.parent)
        child_env["PYTHONPATH"] = package_root + (
            os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else ""
        )
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
            "env": child_env,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
            )
        else:
            kwargs["start_new_session"] = True
        try:
            subprocess.Popen([sys.executable, "-m", "gwprov.daemon", "--serve"], **kwargs)
        finally:
            log.close()
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                response = request(
                    {"op": "ping", "clientIdentity": _daemon_identity()}, timeout=0.4
                )
            except (OSError, RuntimeError, ValueError) as error:
                last_error = error
                time.sleep(0.05)
                continue
            _check_daemon_identity(response)
            return
        raise RuntimeError(f"GWProv daemon did not become ready at {endpoint()}: {last_error}")


@contextmanager
def _startup_lock():
    """Serialize lazy daemon startup across terminals without a port race."""
    path = runtime_directory() / "daemon-start.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def require_binary_override_support(binary):
    if binary is None:
        return
    capabilities = request({"op": "ping"}, timeout=1.0).get("capabilities", [])
    if "gwemu-bin" not in capabilities:
        raise RuntimeError(
            "running GWProv daemon does not support --gwemu-bin; finish active VM/hardware operations before restarting the daemon"
        )


def require_timing_support(timing_mode, pacing=None):
    if timing_mode == "default" and pacing is None:
        return
    capabilities = request({"op": "ping"}, timeout=1.0).get("capabilities", [])
    if "timing-pacing" not in capabilities:
        raise RuntimeError(
            "running GWProv daemon does not support explicit timing/pacing controls; "
            "finish active operations and let the idle daemon exit before retrying"
        )
    if timing_mode == "experimental-m7" and "timing-experimental-m7" not in capabilities:
        raise RuntimeError(
            "running GWProv daemon does not support experimental-m7; finish active VM/hardware operations and let the idle daemon exit before retrying"
        )


def require_profile_recording_support(
    record_profile, profile_interval_ms=None, profile_symbols=None
):
    if record_profile is None and profile_interval_ms is None and not profile_symbols:
        return
    capabilities = request({"op": "ping"}, timeout=1.0).get("capabilities", [])
    if "guest-profile-recording" not in capabilities:
        raise RuntimeError(
            "running GWProv daemon does not support --record-profile; finish active "
            "VM/hardware operations and let the idle daemon exit before retrying"
        )


def start_instance(
    profile: str,
    *,
    audio: bool = False,
    gdb_port: int | None = None,
    headless: bool = False,
    timeline: str | None = None,
    record_timeline: str | None = None,
    record_profile: str | None = None,
    profile_interval_ms: int | None = None,
    profile_symbols: list[str] | None = None,
    start_halted: bool = False,
    shared_sd_root: str | None = None,
    timing_mode: str = "default",
    icount: int | None = None,
    pacing: str | None = None,
    rtc_epoch: int | None = None,
    gwemu_bin: str | None = None,
) -> dict:
    from .timeline_launch import validate_profile_recording

    resolved_record, _, resolved_symbols = validate_profile_recording(
        record_profile, profile_interval_ms, profile_symbols
    )
    if resolved_record is not None:
        record_profile = str(resolved_record)
        profile_symbols = [str(path) for path in resolved_symbols]
    ensure_running()
    require_binary_override_support(gwemu_bin)
    require_timing_support(timing_mode, pacing)
    require_profile_recording_support(record_profile, profile_interval_ms, profile_symbols)
    return request(
        {
            "op": "start-vm",
            "profile": profile,
            "audio": audio,
            "gdb_port": gdb_port,
            "headless": headless,
            "timeline": timeline,
            "record_timeline": record_timeline,
            "record_profile": record_profile,
            "profile_interval_ms": profile_interval_ms,
            "profile_symbols": profile_symbols,
            "start_halted": start_halted,
            "shared_sd_root": shared_sd_root,
            "timing_mode": timing_mode,
            "icount": icount,
            "pacing": pacing,
            "rtc_epoch": rtc_epoch,
            "gwemu_bin": gwemu_bin,
        },
        timeout=20.0,
    )["instance"]


def start_image(
    image: dict[str, str],
    *,
    audio: bool = False,
    gdb_port: int | None = None,
    headless: bool = False,
    timeline: str | None = None,
    record_timeline: str | None = None,
    record_profile: str | None = None,
    profile_interval_ms: int | None = None,
    profile_symbols: list[str] | None = None,
    icount: int | None = None,
    keep_temp: bool = False,
    timing_mode: str = "default",
    pacing: str | None = None,
    rtc_epoch: int | None = None,
    gwemu_bin: str | None = None,
) -> dict:
    from .timeline_launch import validate_profile_recording

    resolved_record, _, resolved_symbols = validate_profile_recording(
        record_profile, profile_interval_ms, profile_symbols
    )
    if resolved_record is not None:
        record_profile = str(resolved_record)
        profile_symbols = [str(path) for path in resolved_symbols]
    ensure_running()
    require_binary_override_support(gwemu_bin)
    require_timing_support(timing_mode, pacing)
    require_profile_recording_support(record_profile, profile_interval_ms, profile_symbols)
    return request(
        {
            "op": "start-vm",
            "image": image,
            "audio": audio,
            "gdb_port": gdb_port,
            "headless": headless,
            "timeline": timeline,
            "record_timeline": record_timeline,
            "record_profile": record_profile,
            "profile_interval_ms": profile_interval_ms,
            "profile_symbols": profile_symbols,
            "icount": icount,
            "keep_temp": keep_temp,
            "timing_mode": timing_mode,
            "pacing": pacing,
            "rtc_epoch": rtc_epoch,
            "gwemu_bin": gwemu_bin,
        },
        timeout=20.0,
    )["instance"]


def managed_instances(*, start: bool = False) -> list[dict]:
    if start:
        ensure_running()
    try:
        return request({"op": "list-vms"}, timeout=3.0).get("instances", [])
    except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError):
        return []


def qmp_request(path: str, command: str, arguments: dict | None = None) -> dict:
    if not path.startswith("gwprov://"):
        raise ValueError("not a GWProv-managed QMP endpoint")
    vm = path.removeprefix("gwprov://")
    return request(
        {"op": "qmp", "vm": vm, "command": command, "arguments": arguments}, timeout=10.0
    )


def register_lease(token: str, owner: dict) -> None:
    ensure_running()
    request({"op": "lease-acquire", "token": token, "owner": owner})


def heartbeat_lease(token: str, owner: dict) -> None:
    """Keep one direct CLI-to-adapter lease alive independently of device I/O."""
    try:
        request({"op": "lease-heartbeat", "token": token, "owner": owner}, timeout=1.0)
    except OSError:
        # A daemon may have exited after an idle timeout or a code reload. Wake
        # the current version and re-register the still-held process lease.
        ensure_running()
        request({"op": "lease-heartbeat", "token": token, "owner": owner}, timeout=2.0)


def release_lease(token: str) -> None:
    try:
        request({"op": "lease-release", "token": token}, timeout=0.5)
    except (OSError, RuntimeError, ValueError):
        pass


def watch_devices(probes: list[dict]) -> list[dict]:
    """Register physical devices with the daemon and return its current cache."""
    ensure_running()
    timeout = max(30.0, min(120.0, 20.0 * len(probes)))
    return request({"op": "device-watch", "probes": probes}, timeout=timeout).get("devices", [])


def device_snapshot(*, refresh: bool = False) -> list[dict]:
    """Read the daemon's latest hardware state, optionally waiting for a live poll."""
    ensure_running()
    timeout = 60.0 if refresh else 30.0
    return request({"op": "device-snapshot", "refresh": refresh}, timeout=timeout).get(
        "devices", []
    )


def pause_device_monitor(key: str, pause_token: str, pid: int, wait_seconds: float) -> None:
    """Hand a monitored target connection to a foreground device operation."""
    try:
        response = request(
            {
                "op": "target-pause",
                "key": key,
                "pauseToken": pause_token,
                "pid": pid,
                "waitSeconds": wait_seconds,
            },
            timeout=60.0,
        )
        if response.get("paused") is not True:
            raise RuntimeError(f"GWProv daemon refused to pause the monitor for {key}: {response}")
    except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError):
        # No service owns a monitor connection, so the foreground backend can
        # proceed to its process-shared lease and direct adapter connection.
        return


def resume_device_monitor(key: str, pause_token: str) -> None:
    try:
        request({"op": "target-resume", "key": key, "pauseToken": pause_token}, timeout=1.0)
    except (OSError, RuntimeError, ValueError):
        pass


def open_target_session(probe: dict, operation: str) -> dict:
    ensure_running()
    return request(
        {"op": "target-open", "probe": probe, "operation": operation, "pid": os.getpid()},
        timeout=20.0,
    )


def call_target_session(token: str | None, method: str, arguments: list):
    if not token:
        raise RuntimeError("daemon target session is not open")
    if method == "write_memory":
        address, data = arguments
        arguments = [address, base64.b64encode(data).decode("ascii")]
    response = request(
        {"op": "target-call", "token": token, "method": method, "arguments": arguments},
        timeout=60.0,
    )
    result = response.get("result")
    if method == "read_memory":
        return base64.b64decode(result, validate=True)
    return result


def heartbeat_proxy_session(token: str, owner: dict) -> None:
    """Keep an idle daemon-proxied debug session alive without target traffic."""
    request({"op": "lease-heartbeat", "token": token, "owner": owner}, timeout=1.0)


def close_target_session(token: str) -> None:
    try:
        request({"op": "target-close", "token": token}, timeout=3.0)
    except (OSError, RuntimeError, ValueError):
        pass


def _serve() -> int:
    Service().run()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["--serve"]:
        print(
            "GWProv daemon is an internal service and has no public command interface.",
            file=sys.stderr,
        )
        return 2
    try:
        return _serve()
    except Exception as error:
        print(f"gwprov daemon: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
