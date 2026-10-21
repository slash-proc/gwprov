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
    monkeypatch.setattr(serve_manager, "validate_requested_port", lambda port, **_kwargs: port)
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


def test_serve_start_registers_remote_gdb_device(monkeypatch, tmp_path):
    registry = {}
    launched = {}

    class Process:
        pid = 4322

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
    monkeypatch.setattr(serve_manager, "validate_requested_port", lambda port, **_kwargs: port)
    monkeypatch.setattr(serve_manager, "port_is_available", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(serve_manager.subprocess, "Popen", fake_popen)

    result = serve_manager.start(
        {
            "id": "gdb:debug-target.example.invalid:3333",
            "kind": "remote-gdb",
            "gdbHost": "debug-target.example.invalid",
            "gdbPort": 3333,
            "gdbUrl": "gdb://debug-target.example.invalid:3333",
        },
        port=8765,
    )

    target = json.loads(launched["command"][launched["command"].index("--target") + 1])
    assert target == {
        "kind": "remote-gdb",
        "host": "debug-target.example.invalid",
        "port": 3333,
        "url": "gdb://debug-target.example.invalid:3333",
    }
    assert result["url"] == "ws://127.0.0.1:8765/gdb"
    assert registry["gdb:debug-target.example.invalid:3333"]["pid"] == 4322


def test_serve_worker_uses_remote_gdb_backend_and_protocol(monkeypatch):
    from gnwmanager import server
    from gwprov import debug_shell, serve_worker

    calls = {}

    class Backend:
        def __init__(self, host, port, **kwargs):
            calls["backend"] = (host, port, kwargs)

    async def fake_serve_backend(factory, **kwargs):
        calls["server"] = kwargs
        factory()

    monkeypatch.setattr(debug_shell, "RemoteGDBBackend", Backend)
    monkeypatch.setattr(server, "serve_backend", fake_serve_backend)

    import asyncio

    asyncio.run(
        serve_worker._serve(
            {
                "kind": "remote-gdb",
                "host": "debug-target.example.invalid",
                "port": 3333,
                "url": "gdb://debug-target.example.invalid:3333",
            },
            "127.0.0.1",
            8765,
        )
    )

    assert calls["backend"] == (
        "debug-target.example.invalid",
        3333,
        {
            "url": "gdb://debug-target.example.invalid:3333",
            "operation": "gwprov remote GDB debug session",
        },
    )
    assert calls["server"] == {
        "bind": "127.0.0.1",
        "port": 8765,
        "backend_name": "gdb",
    }


def test_serve_status_lists_registered_sessions_and_worker_state(monkeypatch):
    monkeypatch.setattr(
        serve_manager,
        "_read_registry",
        lambda: {
            "gdb:debug-target.example.invalid:3333": {
                "name": "test-target",
                "kind": "remote-gdb",
                "pid": 4323,
                "bind": "127.0.0.1",
                "port": 8765,
                "url": "ws://127.0.0.1:8765/gdb",
                "started": 1.0,
            },
            "gwemu:42": {"pid": 9999, "url": "ws://127.0.0.1:8766/gdb"},
        },
    )
    monkeypatch.setattr(
        serve_manager,
        "_worker_state",
        lambda row: "serving" if row["pid"] == 4323 else "stale",
    )

    rows = serve_manager.status()

    assert [row["device"] for row in rows] == [
        "gdb:debug-target.example.invalid:3333",
        "gwemu:42",
    ]
    assert rows[0]["name"] == "test-target"
    assert rows[0]["state"] == "serving"
    assert rows[1]["name"] == "gwemu:42"
    assert rows[1]["state"] == "stale"


def test_serve_status_command_accepts_short_form_and_json(monkeypatch, capsys):
    from gwprov import serve_manager
    from gwprov.cli.main import main

    rows = [{"device": "gwemu:42", "name": "vm", "state": "serving"}]
    monkeypatch.setattr(serve_manager, "status", lambda: rows)

    assert main(["serve", "status", "--output", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == rows

    assert main(["serve", "status"]) == 0
    output = capsys.readouterr().out
    assert "GWProv serve sessions (1)" in output
    assert "SERVING" in output


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
