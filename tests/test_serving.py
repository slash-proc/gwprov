import json

import pytest

from gwprov import ports, serve_manager


def test_automatic_port_allocation_starts_after_default(monkeypatch):
    probes = []

    def available(port, *, host):
        probes.append((host, port))
        return port == 1237

    monkeypatch.setattr(ports, "port_is_available", available)
    monkeypatch.setattr(ports, "_RESERVED_PORTS", set())

    assert ports.find_free_port(1234, host="127.0.0.1") == 1237
    assert probes == [("127.0.0.1", 1235), ("127.0.0.1", 1236), ("127.0.0.1", 1237)]


def test_serve_start_registers_selected_tcp_gdb_endpoint(monkeypatch, tmp_path):
    registry = {}
    launched = {}

    class Process:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        launched["command"] = command
        return Process()

    monkeypatch.setattr(serve_manager, "runtime_directory", lambda: tmp_path)
    monkeypatch.setattr(serve_manager, "_read_registry", lambda: registry.copy())
    monkeypatch.setattr(serve_manager, "_write_registry", lambda rows: registry.update(rows))
    monkeypatch.setattr(serve_manager, "_worker_alive", lambda _row: False)
    monkeypatch.setattr(serve_manager, "find_free_port", lambda *_args, **_kwargs: 8766)
    monkeypatch.setattr(serve_manager, "port_is_available", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(serve_manager.subprocess, "Popen", fake_popen)

    result = serve_manager.start(
        {
            "id": "gwemu:17",
            "kind": "gwemu",
            "gdbTransport": "tcp",
            "gdbEndpoint": "127.0.0.1:1240",
            "gdbPort": 1240,
        }
    )

    target = json.loads(launched["command"][launched["command"].index("--target") + 1])
    assert target == {
        "kind": "gwemu",
        "host": "127.0.0.1",
        "port": 1240,
        "endpoint": "127.0.0.1:1240",
        "transport": "tcp",
    }
    assert result["url"] == "ws://127.0.0.1:8766/gdb"
    assert registry["gwemu:17"]["pid"] == 4321


def test_serve_requires_a_gdb_endpoint_for_rogue_gwemu(monkeypatch, tmp_path):
    monkeypatch.setattr(serve_manager, "runtime_directory", lambda: tmp_path)
    monkeypatch.setattr(serve_manager, "_read_registry", lambda: {})
    monkeypatch.setattr(serve_manager, "find_free_port", lambda *_args, **_kwargs: 8766)

    with pytest.raises(ValueError, match="no identified GDB endpoint"):
        serve_manager.start({"id": "gwemu:17", "kind": "gwemu"})


def test_serve_rejects_invalid_tcp_endpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(serve_manager, "runtime_directory", lambda: tmp_path)
    monkeypatch.setattr(serve_manager, "_read_registry", lambda: {})
    monkeypatch.setattr(serve_manager, "find_free_port", lambda *_args, **_kwargs: 8766)

    with pytest.raises(ValueError, match="port must be an integer"):
        serve_manager.start(
            {"id": "gwemu:17", "kind": "gwemu"}, gdb_endpoint="127.0.0.1:not-a-port"
        )
