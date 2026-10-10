"""Per-user GWProv daemon for managed GWemu and active target leases."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import queue
import signal
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any

from .daemon_ipc import DaemonServer, endpoint, request, runtime_directory

IDLE_EXIT_SECONDS = 2.0
DAEMON_LOG = runtime_directory() / "daemon.log"


class ManagedVM:
    """A GWemu child whose private QMP stream is carried over stdio."""

    def __init__(self, process: subprocess.Popen, profile: str, headless: bool,
                 gdb_port: int | None, log_path: Path,
                 cleanup_path: Path | None = None):
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
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name=f"gwprov-qmp-{self.pid}")
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
            return {"pid": self.pid, "profile": self.profile, "status": "exited",
                    "running": None, "halted": None, "qmpStatus": "unavailable",
                    "qmpSocket": self.qmp_uri, "qmpHandle": self.qmp_uri,
                    "qmpTransport": "daemon-stdio",
                    "display": "headless" if self.headless else "visible",
                    "gdbPort": self.gdb_port, "created": self.created, "timing": self.timing, "binaryIdentity": self.binary_identity,
                    "stateDetail": f"GWemu exited with status {self.process.returncode}"}
        result = self.execute("query-status").get("return", {})
        running = result.get("running")
        return {"pid": self.pid, "profile": self.profile,
                "status": "running" if running is True else "halted" if running is False else "unknown",
                "running": running, "halted": not running if isinstance(running, bool) else None,
                "qmpStatus": result.get("status", "unknown"), "qmpSocket": self.qmp_uri,
                "qmpHandle": self.qmp_uri, "qmpTransport": "daemon-stdio",
                "display": "headless" if self.headless else "visible",
                "gdbPort": self.gdb_port, "created": self.created, "timing": self.timing, "binaryIdentity": self.binary_identity}


class Service:
    def __init__(self):
        self.vms: dict[int, ManagedVM] = {}
        self.leases: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.last_busy = time.monotonic()
        self.shutdown_requested = threading.Event()
        self.server = DaemonServer(self.dispatch)

    def dispatch(self, message: dict) -> dict:
        operation = message.get("op")
        with self.lock:
            self._reap()
            if operation == "hello":
                return {"daemonPid": os.getpid(), "protocol": 1}
            if operation == "start-vm":
                return self.start_vm(message)
            if operation == "list-vms":
                rows = []
                for pid, vm in list(self.vms.items()):
                    try:
                        rows.append(vm.row())
                    except (OSError, RuntimeError, ValueError) as error:
                        rows.append({"pid": pid, "profile": vm.profile, "status": "unknown",
                                     "running": None, "halted": None, "qmpStatus": "unavailable",
                                     "qmpSocket": vm.qmp_uri, "qmpHandle": vm.qmp_uri,
                                     "qmpTransport": "daemon-stdio",
                                     "display": "headless" if vm.headless else "visible",
                                     "gdbPort": vm.gdb_port, "created": vm.created,
                                     "stateDetail": str(error)})
                return {"instances": rows}
            if operation == "qmp":
                vm = self._vm(message.get("vm"))
                command = message.get("command")
                if not isinstance(command, str) or not command:
                    raise ValueError("QMP command must be a non-empty string")
                if command not in {"query-status", "screendump", "stop", "cont", "quit",
                                   "system_reset", "input-send-event", "send-key",
                                   "human-monitor-command"}:
                    raise ValueError(f"QMP command is not exposed by GWProv: {command}")
                arguments = message.get("arguments")
                if arguments is not None and not isinstance(arguments, dict):
                    raise ValueError("QMP arguments must be an object")
                if command == "human-monitor-command":
                    command_line = arguments.get("command-line", "") if arguments else ""
                    import re
                    memory_read = re.fullmatch(
                        r"xp /([1-9][0-9]*)wx 0x[0-9a-fA-F]+", command_line)
                    if command_line != "info registers" and not memory_read:
                        raise ValueError("GWProv permits only register reads and bounded word memory reads through HMP")
                    if memory_read and int(memory_read.group(1)) > 16384:
                        raise ValueError("GWProv HMP memory reads are limited to 64 KiB")
                return vm.execute(command, arguments)
            if operation == "lease-acquire":
                token, owner = message.get("token"), message.get("owner")
                if not isinstance(token, str) or not isinstance(owner, dict):
                    raise ValueError("invalid hardware lease registration")
                self.leases[token] = owner
                self.last_busy = time.monotonic()
                return {"registered": True}
            if operation == "lease-release":
                self.leases.pop(str(message.get("token")), None)
                self.last_busy = time.monotonic()
                return {"released": True}
            if operation == "quit-vm":
                vm = self._vm(message.get("vm"))
                vm.execute("quit")
                return {"stopping": True}
            if operation == "ping":
                self.last_busy = time.monotonic()
                return {"daemonPid": os.getpid(), "capabilities": ["gwemu-bin", "process-binary-inode", "timing-experimental-m7"]}
            raise ValueError(f"unsupported daemon operation: {operation!r}")

    def start_vm(self, message: dict) -> dict:
        from .launch import image_launch_spec, profile_launch_spec
        profile_value = message.get("profile")
        cleanup_path = None
        headless = bool(message.get("headless", False))
        audio = bool(message.get("audio", False))
        gdb_port = message.get("gdb_port")
        if profile_value:
            from .profiles import DeviceProfile
            profile = str(DeviceProfile.load(profile_value).root)
            if any(vm.profile == profile and vm.process.poll() is None for vm in self.vms.values()):
                raise ValueError(f"profile already has a managed GWemu instance: {profile}")
            cmd, cwd, env, log_path = profile_launch_spec(
                profile, headless=headless, audio=audio, timeline=message.get("timeline"),
                record_timeline=message.get("record_timeline"), gdb_port=gdb_port,
                qmp_stdio=True, start_halted=bool(message.get("start_halted", False)),
                shared_sd_root=message.get("shared_sd_root"),
                timing_mode=message.get("timing_mode", "default"),
                icount=message.get("icount"), rtc_epoch=message.get("rtc_epoch"),
                gwemu_bin=message.get("gwemu_bin"))
        else:
            profile = "raw image"
            cmd, cwd, env, log_path, cleanup_path = image_launch_spec(
                message.get("image", {}), headless=headless, audio=audio,
                timeline=message.get("timeline"),
                record_timeline=message.get("record_timeline"), gdb_port=gdb_port,
                icount=message.get("icount"), keep_temp=bool(message.get("keep_temp", False)),
                timing_mode=message.get("timing_mode", "default"), rtc_epoch=message.get("rtc_epoch"),
                gwemu_bin=message.get("gwemu_bin"))
        from .binary_identity import file_identity
        expected_binary = file_identity(cmd[0]) if message.get("gwemu_bin") else None
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_path.open("w") as log:
                process = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=log, bufsize=0)
            vm = ManagedVM(process, profile, headless, gdb_port, log_path, cleanup_path)
            if expected_binary and vm.binary_identity["sha256"] != expected_binary["sha256"]:
                raise RuntimeError("GWemu executable changed between launch validation and startup")
            with log_path.open("r", errors="replace") as startup_log:
                for line in startup_log.read(8192).splitlines():
                    if line.startswith("gwemu_version:"):
                        vm.binary_identity["version"] = line.partition(":")[2].strip()
                        break
            mode = message.get("timing_mode", "default")
            vm.timing = {"mode": mode, "icountShift": 0 if mode in ("baseline", "experimental-m7") else message.get("icount"),
                         "rtcEpoch": message.get("rtc_epoch"),
                         "cycleSemantics": "one-instruction-one-cycle" if mode == "baseline" else
                                           "experimental-m7-issue-dependency-model" if mode == "experimental-m7" else "virtual-elapsed-time",
                         "hardwareCycleAccurate": False}
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
        for pid, vm in list(self.vms.items()):
            if vm.process.poll() is not None:
                self.vms.pop(pid, None)
                if vm.cleanup_path:
                    shutil.rmtree(vm.cleanup_path, ignore_errors=True)
        for token, owner in list(self.leases.items()):
            pid = owner.get("pid")
            if not isinstance(pid, int) or not _pid_exists(pid):
                self.leases.pop(token, None)

    def run(self) -> None:
        for signum in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
            if signum is not None:
                signal.signal(signum, lambda *_: self.shutdown_requested.set())
        server_thread = threading.Thread(target=self.server.serve, daemon=True)
        server_thread.start()
        try:
            while server_thread.is_alive():
                if self.shutdown_requested.is_set():
                    break
                time.sleep(0.25)
                with self.lock:
                    self._reap()
                    if self.vms or self.leases:
                        self.last_busy = time.monotonic()
                    elif time.monotonic() - self.last_busy >= IDLE_EXIT_SECONDS:
                        self.server.stop()
                        break
        finally:
            self.server.stop()
            server_thread.join(timeout=2)
            self._stop_children()

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
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def ensure_running(*, timeout: float = 8.0) -> None:
    """Connect to the daemon, starting one if this is the first active client."""
    with _startup_lock():
        try:
            request({"op": "ping"}, timeout=0.4)
            return
        except (OSError, RuntimeError, ValueError):
            pass
        log_path = runtime_directory() / "daemon.log"
        log = log_path.open("ab")
        child_env = dict(os.environ)
        package_root = str(Path(__file__).resolve().parent.parent)
        child_env["PYTHONPATH"] = package_root + (os.pathsep + child_env["PYTHONPATH"]
                                                    if child_env.get("PYTHONPATH") else "")
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log,
                                  "stderr": subprocess.STDOUT, "close_fds": True,
                                  "env": child_env}
        if os.name == "nt":
            kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                                       | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
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
                request({"op": "ping"}, timeout=0.4)
                return
            except (OSError, RuntimeError, ValueError) as error:
                last_error = error
                time.sleep(0.05)
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
        raise RuntimeError("running GWProv daemon does not support --gwemu-bin; finish active VM/hardware operations before restarting the daemon")


def require_timing_support(timing_mode):
    if timing_mode != "experimental-m7":
        return
    capabilities = request({"op": "ping"}, timeout=1.0).get("capabilities", [])
    if "timing-experimental-m7" not in capabilities:
        raise RuntimeError("running GWProv daemon does not support experimental-m7; finish active VM/hardware operations and let the idle daemon exit before retrying")


def start_instance(profile: str, *, audio: bool = False, gdb_port: int | None = None,
                   headless: bool = False, timeline: str | None = None,
                   record_timeline: str | None = None,
                   start_halted: bool = False,
                   shared_sd_root: str | None = None, timing_mode: str = "default",
                   icount: int | None = None, rtc_epoch: int | None = None,
                   gwemu_bin: str | None = None) -> dict:
    ensure_running()
    require_binary_override_support(gwemu_bin)
    require_timing_support(timing_mode)
    return request({"op": "start-vm", "profile": profile, "audio": audio,
                    "gdb_port": gdb_port, "headless": headless,
                    "timeline": timeline, "record_timeline": record_timeline,
                    "start_halted": start_halted,
                    "shared_sd_root": shared_sd_root, "timing_mode": timing_mode,
                    "icount": icount, "rtc_epoch": rtc_epoch, "gwemu_bin": gwemu_bin}, timeout=20.0)["instance"]


def start_image(image: dict[str, str], *, audio: bool = False,
                gdb_port: int | None = None, headless: bool = False,
                timeline: str | None = None, record_timeline: str | None = None,
                icount: int | None = None, keep_temp: bool = False,
                timing_mode: str = "default", rtc_epoch: int | None = None,
                gwemu_bin: str | None = None) -> dict:
    ensure_running()
    require_binary_override_support(gwemu_bin)
    require_timing_support(timing_mode)
    return request({"op": "start-vm", "image": image, "audio": audio,
                    "gdb_port": gdb_port, "headless": headless,
                    "timeline": timeline, "record_timeline": record_timeline,
                    "icount": icount, "keep_temp": keep_temp,
                    "timing_mode": timing_mode, "rtc_epoch": rtc_epoch,
                    "gwemu_bin": gwemu_bin}, timeout=20.0)["instance"]


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
    return request({"op": "qmp", "vm": vm, "command": command,
                    "arguments": arguments}, timeout=10.0)


def register_lease(token: str, owner: dict) -> None:
    ensure_running()
    request({"op": "lease-acquire", "token": token, "owner": owner})


def release_lease(token: str) -> None:
    try:
        request({"op": "lease-release", "token": token}, timeout=0.5)
    except (OSError, RuntimeError, ValueError):
        pass


def _serve() -> int:
    Service().run()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["--serve"]:
        print("GWProv daemon is an internal service and has no public command interface.",
              file=sys.stderr)
        return 2
    try:
        return _serve()
    except Exception as error:
        print(f"gwprov daemon: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
