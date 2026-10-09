import json
import sys
import types

import pytest

from gwprov.backends import WebSocketBackend


class FakeSocket:
    def __init__(self):
        self.requests = []
        self.closed = False

    def recv(self, timeout=None):
        if not self.requests:
            return json.dumps({"type": "hello", "version": 1, "backend": "gnwmanager",
                               "targetControl": "arm"})
        request = self.requests[-1]
        method = request["method"]
        if method == "read_memory":
            _, size = request["args"]
            result = (b"\0" * size).hex()
        elif method == "read_register":
            result = 0x1234
        else:
            result = True if method == "attach" else None
        return json.dumps({"id": 1, "result": result})

    def send(self, payload):
        self.requests.append(json.loads(payload))

    def close(self):
        self.closed = True


def fake_websockets(monkeypatch, tmp_path):
    monkeypatch.setenv("GWPROV_LEASE_DIR", str(tmp_path / "leases"))
    socket = FakeSocket()
    called = {}

    def connect(uri, **kwargs):
        called.update(uri=uri, **kwargs)
        return socket

    client = types.ModuleType("websockets.sync.client")
    client.connect = connect
    sync = types.ModuleType("websockets.sync")
    sync.client = client
    package = types.ModuleType("websockets")
    package.sync = sync
    monkeypatch.setitem(sys.modules, "websockets", package)
    monkeypatch.setitem(sys.modules, "websockets.sync", sync)
    monkeypatch.setitem(sys.modules, "websockets.sync.client", client)
    return socket, called


def test_websocket_backend_selects_origin_and_chunks_memory(monkeypatch, tmp_path):
    socket, called = fake_websockets(monkeypatch, tmp_path)
    backend = WebSocketBackend("wss://debug.example:8765/gdb", origin="https://gwprov.local")
    backend.MAX_MEMORY = 3
    backend.open()
    assert called["origin"] == "https://gwprov.local"
    assert socket.requests[0]["method"] == "attach"

    assert backend.read_memory(0x20000000, 8) == bytes(8)
    reads = [request for request in socket.requests if request["method"] == "read_memory"]
    assert [request["args"] for request in reads] == [
        [0x20000000, 3], [0x20000003, 3], [0x20000006, 2]]

    backend.write_memory(0x20000000, b"abcdefgh")
    backend.reset()
    assert [request["method"] for request in socket.requests[-2:]] == ["reset_and_halt", "resume"]
    writes = [request for request in socket.requests if request["method"] == "write_memory"]
    assert [bytes.fromhex(request["args"][1]) for request in writes] == [b"abc", b"def", b"gh"]
    backend.close()
    assert socket.closed


def test_websocket_backend_rejects_unexpected_protocol(monkeypatch, tmp_path):
    socket, _ = fake_websockets(monkeypatch, tmp_path)
    socket.recv = lambda timeout=None: json.dumps({"type": "hello", "version": 2,
                                                    "backend": "gnwmanager"})
    backend = WebSocketBackend("ws://localhost:8765/gdb")
    with pytest.raises(RuntimeError, match="unsupported gnwmanager"):
        backend.open()
    assert socket.closed


def test_websocket_backend_requires_gnwmanager_path():
    with pytest.raises(ValueError, match="/gdb"):
        WebSocketBackend("ws://localhost:8765/")


def test_unverified_device_inventory_returns_nonzero(monkeypatch, capsys):
    from gwprov import devices

    monkeypatch.setattr(devices, "device_rows", lambda profile=None: [{
        "id": "probe:*", "kind": "hardware", "status": "unknown",
        "application": "Unknown", "name": "Probe inventory",
    }])
    assert devices.show_devices(output="json") == 2
    assert '"status": "unknown"' in capsys.readouterr().out
