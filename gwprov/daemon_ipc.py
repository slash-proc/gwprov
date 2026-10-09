"""Per-user local IPC for the GWProv service daemon.

Unix-domain sockets are protected by a private runtime directory and socket
mode, with peer-UID verification where the platform exposes credentials.
Windows uses a named pipe with an explicit DACL for the current user and
SYSTEM; the server also verifies the connecting process token's user SID.
Messages are newline-delimited UTF-8 JSON objects.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import stat
import sys
import threading
from typing import Callable

PROTOCOL_VERSION = 1


def runtime_directory() -> Path:
    """Return a short, per-user directory for daemon state and its endpoint."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        path = base / "GWProv" / "run"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Caches" / "gwprov" / "run"
    else:
        base = os.environ.get("XDG_RUNTIME_DIR")
        path = (Path(base) / "gwprov" if base else
                Path.home() / ".cache" / "gwprov" / "run")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            path.chmod(0o700)
        except OSError:
            pass
    return path


def endpoint() -> str:
    if os.name == "nt":
        sid = _windows_current_user_sid()
        import hashlib
        return r"\\.\pipe\gwprov-" + hashlib.sha256(sid.encode("ascii")).hexdigest()[:24]
    return str(runtime_directory() / "daemon.sock")


def _windows_current_user_sid() -> str:
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.HANDLE)]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                           wintypes.LPVOID, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID,
                                              ctypes.POINTER(wintypes.LPWSTR)]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return sid_text.value
        finally:
            kernel.LocalFree(sid_text)
    finally:
        kernel.CloseHandle(token)


def request(message: dict, *, timeout: float = 5.0) -> dict:
    """Send one request and return its structured response."""
    encoded = json.dumps({"protocol": PROTOCOL_VERSION, **message},
                         separators=(",", ":")).encode("utf-8") + b"\n"
    if os.name == "nt":
        return _windows_request(encoded, timeout)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(timeout)
        stream.connect(endpoint())
        stream.sendall(encoded)
        return _read_response(stream)


def _read_response(stream: socket.socket) -> dict:
    data = bytearray()
    while not data.endswith(b"\n"):
        block = stream.recv(65536)
        if not block:
            raise ConnectionError("GWProv daemon closed the IPC connection")
        data.extend(block)
        if len(data) > 4 * 1024 * 1024:
            raise ValueError("GWProv daemon response exceeded 4 MiB")
    response = json.loads(data)
    if not isinstance(response, dict):
        raise ValueError("GWProv daemon returned a non-object response")
    if response.get("protocol") != PROTOCOL_VERSION:
        raise RuntimeError("GWProv daemon protocol version mismatch; restart the daemon")
    if not response.get("ok", False):
        raise RuntimeError(response.get("error", "GWProv daemon request failed"))
    return response


def _windows_request(encoded: bytes, timeout: float) -> dict:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.WaitNamedPipeW.restype = wintypes.BOOL
    kernel.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel.ReadFile.restype = wintypes.BOOL
    kernel.WriteFile.restype = wintypes.BOOL
    kernel.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    pipe_name = endpoint()
    deadline = __import__("time").monotonic() + timeout
    handle = wintypes.HANDLE()
    while True:
        handle = kernel.CreateFileW(pipe_name, 0xC0000000, 0, None, 3, 0, None)
        if handle != wintypes.HANDLE(-1).value:
            break
        error = ctypes.get_last_error()
        if error not in (2, 231) or __import__("time").monotonic() >= deadline:
            raise ctypes.WinError(error)
        kernel.WaitNamedPipeW(pipe_name, 100)
    try:
        _write_handle(kernel, handle, encoded)
        return _read_handle_response(kernel, handle)
    finally:
        kernel.CloseHandle(handle)


def _write_handle(kernel, handle, payload: bytes) -> None:
    import ctypes
    from ctypes import wintypes
    offset = 0
    while offset < len(payload):
        written = wintypes.DWORD()
        chunk = payload[offset:offset + 65536]
        buffer = ctypes.create_string_buffer(chunk)
        if not kernel.WriteFile(handle, buffer, len(chunk), ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
        offset += written.value


def _read_handle_response(kernel, handle) -> dict:
    import ctypes
    from ctypes import wintypes
    data = bytearray()
    while not data.endswith(b"\n"):
        buffer = ctypes.create_string_buffer(65536)
        read = wintypes.DWORD()
        if not kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(read), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not read.value:
            raise ConnectionError("GWProv daemon closed the IPC connection")
        data.extend(buffer.raw[:read.value])
        if len(data) > 4 * 1024 * 1024:
            raise ValueError("GWProv daemon response exceeded 4 MiB")
    response = json.loads(data)
    if not isinstance(response, dict) or response.get("protocol") != PROTOCOL_VERSION:
        raise RuntimeError("GWProv daemon protocol version mismatch")
    if not response.get("ok", False):
        raise RuntimeError(response.get("error", "GWProv daemon request failed"))
    return response


class DaemonServer:
    """Serve one-request local IPC connections to a dispatch callback."""

    def __init__(self, dispatch: Callable[[dict], dict]):
        self.dispatch = dispatch
        self.stopping = threading.Event()
        self.sock: socket.socket | None = None
        self._windows_threads: list[threading.Thread] = []

    def serve(self) -> None:
        if os.name == "nt":
            self._serve_windows()
        else:
            self._serve_unix()

    def stop(self) -> None:
        self.stopping.set()
        if self.sock is not None:
            self.sock.close()
        elif os.name == "nt":
            _windows_wake_server(endpoint())

    def _serve_unix(self) -> None:
        path = Path(endpoint())
        if path.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    probe.connect(str(path))
            except OSError:
                if not stat.S_ISSOCK(path.lstat().st_mode):
                    raise RuntimeError(f"refusing to replace non-socket daemon endpoint: {path}")
                path.unlink(missing_ok=True)
            else:
                raise RuntimeError(f"GWProv daemon endpoint is already active: {path}")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock = listener
        listener.bind(str(path))
        endpoint_inode = path.stat().st_ino
        path.chmod(0o600)
        listener.listen(16)
        listener.settimeout(0.5)
        try:
            while not self.stopping.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stopping.is_set():
                        break
                    raise
                thread = threading.Thread(target=self._serve_connection,
                                          args=(connection,), daemon=True)
                thread.start()
        finally:
            listener.close()
            try:
                if path.stat().st_ino == endpoint_inode:
                    path.unlink()
            except FileNotFoundError:
                pass

    def _serve_connection(self, connection: socket.socket) -> None:
        with connection:
            try:
                self._verify_unix_peer(connection)
                data = bytearray()
                while not data.endswith(b"\n"):
                    block = connection.recv(65536)
                    if not block:
                        raise ConnectionError("client closed before sending a request")
                    data.extend(block)
                    if len(data) > 4 * 1024 * 1024:
                        raise ValueError("GWProv daemon request exceeded 4 MiB")
                request_data = json.loads(data)
                if not isinstance(request_data, dict):
                    raise ValueError("request must be a JSON object")
                if request_data.get("protocol") != PROTOCOL_VERSION:
                    raise RuntimeError("GWProv daemon protocol version mismatch")
                result = self.dispatch(request_data)
                response = {"protocol": PROTOCOL_VERSION, "ok": True,
                            **(result if isinstance(result, dict) else {"result": result})}
            except Exception as error:
                response = {"protocol": PROTOCOL_VERSION, "ok": False,
                            "error": str(error)}
            connection.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")

    @staticmethod
    def _verify_unix_peer(connection: socket.socket) -> None:
        if hasattr(socket, "SO_PEERCRED"):
            import struct
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack("3i", credentials)
            if hasattr(os, "getuid") and uid != os.getuid():
                raise PermissionError("GWProv daemon accepts connections only from its owner")
        elif hasattr(connection, "getpeereid"):
            uid, _ = connection.getpeereid()
            if hasattr(os, "getuid") and uid != os.getuid():
                raise PermissionError("GWProv daemon accepts connections only from its owner")

    def _serve_windows(self) -> None:
        # Implemented with native CreateNamedPipe so the endpoint receives an
        # explicit DACL rather than Windows' permissive default pipe ACL.
        while not self.stopping.is_set():
            handle = _windows_create_pipe(endpoint())
            if handle is None:
                return
            try:
                if not _windows_connect_pipe(handle):
                    continue
                _windows_verify_pipe_client(handle)
                _windows_handle_request(handle, self.dispatch)
            finally:
                _windows_close_pipe(handle)


def _windows_create_pipe(name: str):
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.DWORD, wintypes.LPVOID]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD)]
    sid = _windows_current_user_sid()
    sddl = f"D:P(A;;GA;;;SY)(A;;GA;;;{sid})"
    descriptor = wintypes.LPVOID()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID),
                    ("bInheritHandle", wintypes.BOOL)]

    attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
    handle = kernel.CreateNamedPipeW(name, 0x00000003, 0x00000008,
                                     1, 65536, 65536, 500, ctypes.byref(attributes))
    error = ctypes.get_last_error()
    kernel.LocalFree(descriptor)
    if handle == wintypes.HANDLE(-1).value:
        if error == 231:  # ERROR_PIPE_BUSY: another daemon owns the name.
            raise RuntimeError("GWProv daemon endpoint is already active")
        raise ctypes.WinError(error)
    return handle


def _windows_connect_pipe(handle) -> bool:
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.ConnectNamedPipe.restype = ctypes.c_int
    kernel.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    if kernel.ConnectNamedPipe(handle, None):
        return True
    return ctypes.get_last_error() == 535  # ERROR_PIPE_CONNECTED


def _windows_verify_pipe_client(handle) -> None:
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetNamedPipeClientProcessId.restype = ctypes.c_int
    kernel.GetNamedPipeClientProcessId.argtypes = [wintypes.HANDLE,
                                                   ctypes.POINTER(wintypes.ULONG)]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                           wintypes.LPVOID, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID,
                                              ctypes.POINTER(wintypes.LPWSTR)]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    pid = wintypes.ULONG()
    if not kernel.GetNamedPipeClientProcessId(handle, ctypes.byref(pid)):
        raise ctypes.WinError(ctypes.get_last_error())
    process = kernel.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    token = wintypes.HANDLE()
    try:
        if not advapi.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            needed = wintypes.DWORD()
            advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            buffer = ctypes.create_string_buffer(needed.value)
            if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
                raise ctypes.WinError(ctypes.get_last_error())
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            peer = wintypes.LPWSTR()
            current = wintypes.LPWSTR()
            if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(peer)):
                raise ctypes.WinError(ctypes.get_last_error())
            own_sid = _windows_current_user_sid()
            try:
                if peer.value != own_sid:
                    raise PermissionError("GWProv daemon accepts connections only from its owner")
            finally:
                kernel.LocalFree(peer)
        finally:
            kernel.CloseHandle(token)
    finally:
        kernel.CloseHandle(process)


def _windows_handle_request(handle, dispatch: Callable[[dict], dict]) -> None:
    import ctypes
    from ctypes import wintypes
    data = bytearray()
    while not data.endswith(b"\n"):
        buffer = ctypes.create_string_buffer(65536)
        read = wintypes.DWORD()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.ReadFile.restype = wintypes.BOOL
        kernel.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        if not kernel.ReadFile(
                handle, buffer, len(buffer), ctypes.byref(read), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not read.value:
            return
        data.extend(buffer.raw[:read.value])
        if len(data) > 4 * 1024 * 1024:
            raise ValueError("GWProv daemon request exceeded 4 MiB")
    try:
        request_data = json.loads(data)
        if request_data == {"protocol": 0, "op": "shutdown-wake"}:
            return
        if not isinstance(request_data, dict) or request_data.get("protocol") != PROTOCOL_VERSION:
            raise RuntimeError("GWProv daemon protocol version mismatch")
        result = dispatch(request_data)
        response = {"protocol": PROTOCOL_VERSION, "ok": True,
                    **(result if isinstance(result, dict) else {"result": result})}
    except Exception as error:
        response = {"protocol": PROTOCOL_VERSION, "ok": False, "error": str(error)}
    payload = json.dumps(response, separators=(",", ":")).encode() + b"\n"
    written = wintypes.DWORD()
    buffer = ctypes.create_string_buffer(payload)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.WriteFile.restype = wintypes.BOOL
    kernel.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    if not kernel.WriteFile(
            handle, buffer, len(payload), ctypes.byref(written), None):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_close_pipe(handle) -> None:
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.DisconnectNamedPipe(handle)
    kernel.CloseHandle(handle)


def _windows_wake_server(name: str) -> None:
    """Release a blocking ConnectNamedPipe so the server can observe shutdown."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.WriteFile.restype = wintypes.BOOL
    kernel.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    handle = kernel.CreateFileW(name, 0xC0000000, 0, None, 3, 0, None)
    if handle != wintypes.HANDLE(-1).value:
        payload = b'{"protocol":0,"op":"shutdown-wake"}\n'
        buffer = ctypes.create_string_buffer(payload)
        written = wintypes.DWORD()
        kernel.WriteFile(handle, buffer, len(payload), ctypes.byref(written), None)
        kernel.CloseHandle(handle)
