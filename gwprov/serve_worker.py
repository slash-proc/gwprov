"""Background worker for a local gnwmanager WebSocket bridge."""

from __future__ import annotations

import argparse
import asyncio
import json
import socket


class _UnixGwemuGDBBackend:
    """Connect GWProv's dynamic-register GDB backend to a Unix socket."""

    def __new__(cls, path: str):
        from .debug_shell import GwemuGDBBackend

        class Backend(GwemuGDBBackend):
            def __init__(self, socket_path: str):
                super().__init__()
                self.socket_path = socket_path

            def open(self):
                self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._socket.settimeout(5.0)
                self._socket.connect(self.socket_path)
                self._sock_file = self._socket.makefile("rb", buffering=8192)
                from contextlib import suppress
                from gnwmanager.ocdbackend.gdb_backend import GDBError

                with suppress(GDBError):
                    self._send_command(b"qSupported:multiprocess+;xmlRegisters=i386;qRelocInsn+")
                self._opening_halted = False
                self.resume()
                self._load_register_numbers()
                return self

        return Backend(path)


def _backend(target: dict):
    if target.get("kind") == "gwemu":
        if target.get("transport") == "unix":
            return _UnixGwemuGDBBackend(str(target["endpoint"]))
        from .debug_shell import GwemuGDBBackend

        return GwemuGDBBackend(host=str(target.get("host", "127.0.0.1")), port=int(target["port"]))
    if target.get("kind") == "hardware":
        from .backends import SelectedPyOCDBackend

        return SelectedPyOCDBackend(
            str(target["probeId"]),
            probe_adapter=target.get("adapter"),
            operation="gnwmanager WebSocket serve",
            lease_wait=0,
            daemon_proxy=True,
        )
    if target.get("kind") == "remote-gdb":
        from .debug_shell import RemoteGDBBackend

        return RemoteGDBBackend(
            str(target["host"]),
            int(target["port"]),
            url=str(target.get("url") or f"gdb://{target['host']}:{target['port']}"),
            operation="gwprov remote GDB debug session",
        )
    raise ValueError(f"unsupported serve target kind {target.get('kind')!r}")


async def _serve(target: dict, bind: str, port: int) -> None:
    try:
        from gnwmanager.server import serve_backend
    except ImportError as error:
        raise RuntimeError('WebSocket serving requires `pip install "gwprov[remote]"`') from error
    backend_name = "pyocd" if target.get("kind") == "hardware" else "gdb"
    await serve_backend(lambda: _backend(target), bind=bind, port=port, backend_name=backend_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    target = json.loads(args.target)
    if not isinstance(target, dict):
        raise ValueError("serve target must be a JSON object")
    asyncio.run(_serve(target, args.bind, args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
