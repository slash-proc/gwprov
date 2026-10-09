"""Structured, persistent QMP access independent of the GDB connection."""
from __future__ import annotations

import json
import re
import socket
import os
import time
import traceback
from pathlib import Path



def record_control_request(path: str | None, transport: str, command: str) -> None:
    """Keep a local audit of execution controls, with Python caller locations.

    Only control operations are recorded. Memory and register polling add no
    log traffic. No register contents, memory, or source lines are recorded.
    """
    if not path:
        return
    if path.startswith("gwprov://"):
        from .daemon_ipc import runtime_directory
        destination = runtime_directory() / f"control-{path.removeprefix('gwprov://')}.jsonl"
    else:
        destination = Path(path).expanduser().resolve().parent / "control.jsonl"
    callers = [{"file": frame.filename, "line": frame.lineno, "function": frame.name}
               for frame in traceback.extract_stack(limit=8)[:-1]]
    payload = json.dumps({"time_ns": time.time_ns(), "pid": os.getpid(),
                          "transport": transport, "command": command,
                          "callers": callers}) + "\n"
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, payload.encode())
    finally:
        os.close(descriptor)


def parse_arm_registers(text: str) -> dict[str, int]:
    registers = {f"r{int(index)}": int(value, 16)
                 for index, value in re.findall(r"\bR(\d{2})=([0-9a-fA-F]+)", text)}
    for alias, name in (("sp", "r13"), ("lr", "r14"), ("pc", "r15")):
        if name in registers:
            registers[alias] = registers[name]
    match = re.search(r"\bXPSR=([0-9a-fA-F]+)", text, re.IGNORECASE)
    if match:
        registers["xpsr"] = int(match.group(1), 16)
    if not {"sp", "lr", "pc"}.issubset(registers):
        raise RuntimeError("QMP info registers did not include ARM SP, LR, and PC")
    return registers


class QMPConnection:
    """One monitor connection, reusable for many reads; never halts by itself."""

    def __init__(self, path: str, timeout: float = 5.0):
        self.path = str(path)
        self.timeout = timeout
        self.sock = None
        self.stream = None
        self.request_id = 0

    def __enter__(self):
        if self.path.startswith("gwprov://"):
            # The daemon performs QMP's one-time capability negotiation when
            # it starts the child. This connection is a client RPC handle.
            self.stream = True
            return self
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(self.path)
            self.stream = self.sock.makefile("rwb", buffering=0)
            greeting = self.stream.readline()
            if not greeting or "QMP" not in json.loads(greeting):
                raise RuntimeError(f"invalid QMP greeting from {self.path}")
            self.execute("qmp_capabilities")
            return self
        except PermissionError as exc:
            self.close()
            raise PermissionError(
                f"access to GWemu QMP socket {self.path!r} was denied; run gwprov "
                "with permission to connect to this Unix socket "
                "(NET_ADMIN is not required for QMP Unix-socket access)"
            ) from exc
        except BaseException:
            self.close()
            raise

    def execute(self, command: str, arguments: dict | None = None) -> dict:
        if self.stream is None:
            raise RuntimeError("QMP connection is not open")
        if command in {"stop", "cont", "system_reset", "quit"}:
            record_control_request(self.path, "qmp", command)
        self.request_id += 1
        if self.path.startswith("gwprov://"):
            if command == "qmp_capabilities":
                return {"return": {}, "id": self.request_id}
            from .daemon_ipc import request
            response = request({"op": "qmp", "vm": self.path.removeprefix("gwprov://"),
                                "command": command, "arguments": arguments},
                               timeout=self.timeout)
            if "event" in response:
                return response
            # daemon_ipc envelopes the child reply with protocol metadata.
            return {key: value for key, value in response.items()
                    if key not in {"ok", "protocol"}}
        payload = {"execute": command, "id": self.request_id}
        if arguments:
            payload["arguments"] = arguments
        self.stream.write(json.dumps(payload).encode() + b"\r\n")
        while True:
            line = self.stream.readline()
            if not line:
                raise RuntimeError(f"GWemu closed QMP socket {self.path}")
            reply = json.loads(line)
            if "event" in reply:
                if reply["event"] in {"STOP", "RESUME", "RESET", "SHUTDOWN", "SUSPEND"}:
                    record_control_request(self.path, "qmp-event", reply["event"])
                continue
            if reply.get("id") != self.request_id:
                raise RuntimeError(f"unexpected QMP reply id from {self.path}")
            if "error" in reply:
                raise RuntimeError(f"QMP {command} failed: {reply['error']}")
            return reply

    def monitor(self, command: str) -> str:
        return str(self.execute("human-monitor-command", {"command-line": command})["return"])

    def registers(self) -> dict[str, int]:
        return parse_arm_registers(self.monitor("info registers"))

    def read_memory(self, address: int, size: int) -> bytes:
        if size <= 0:
            return b""
        aligned = address & ~3
        offset = address - aligned
        count = (offset + size + 3) // 4
        words = []
        for line in self.monitor(f"xp /{count}wx 0x{aligned:x}").splitlines():
            if ":" in line:
                words.extend(int(value, 16) for value in
                             re.findall(r"0x([0-9a-fA-F]{1,8})", line.split(":", 1)[1]))
        if len(words) < count:
            raise RuntimeError(f"QMP memory read at 0x{address:08x} returned "
                               f"{len(words)} words; expected {count}")
        data = b"".join(word.to_bytes(4, "little") for word in words[:count])
        return data[offset:offset + size]

    def u32(self, address: int) -> int:
        return int.from_bytes(self.read_memory(address, 4), "little")

    def close(self):
        if self.path.startswith("gwprov://"):
            self.stream = None
            return
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def __exit__(self, *args):
        self.close()
