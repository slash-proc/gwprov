import json

import pytest

from gwprov.adapters import (
    add_gdb,
    add_remote,
    inventory,
    list_gdb,
    list_remote,
    remove_remote,
)


def test_remote_gdb_registry_coexists_with_gnwmanager_adapters(tmp_path, monkeypatch):
    monkeypatch.setenv("GWPROV_CONFIG_DIR", str(tmp_path))

    add_remote(
        "manager", "ws://manager.example:8765/gdb", origin="https://builder.example"
    )
    adapter = add_gdb("test-target", "gdb://debug-target.example.invalid:3333")

    assert adapter == {
        "name": "test-target",
        "url": "gdb://debug-target.example.invalid:3333",
        "host": "debug-target.example.invalid",
        "port": 3333,
    }
    assert list_remote() == [
        {
            "name": "manager",
            "url": "ws://manager.example:8765/gdb",
            "origin": "https://builder.example",
        }
    ]
    assert list_gdb() == [adapter]

    config = json.loads((tmp_path / "adapters.json").read_text())
    assert config["schemaVersion"] == 1
    assert config["remote"][0]["name"] == "manager"
    assert config["gdb"][0] == {
        "name": "test-target",
        "url": "gdb://debug-target.example.invalid:3333",
    }

    assert (
        remove_remote("test-target")["url"] == "gdb://debug-target.example.invalid:3333"
    )
    assert list_gdb() == []
    assert len(list_remote()) == 1


@pytest.mark.parametrize(
    "url",
    (
        "tcp://debug-target.example.invalid:3333",
        "gdb://debug-target.example.invalid",
        "gdb://user@debug-target.example.invalid:3333",
        "gdb://debug-target.example.invalid:0",
        "gdb://debug-target.example.invalid:65536",
        "gdb://debug-target.example.invalid:3333/path",
    ),
)
def test_remote_gdb_registry_rejects_invalid_endpoints(tmp_path, monkeypatch, url):
    monkeypatch.setenv("GWPROV_CONFIG_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        add_gdb("bad-endpoint", url)


def test_remote_gdb_and_gnwmanager_names_share_one_namespace(tmp_path, monkeypatch):
    monkeypatch.setenv("GWPROV_CONFIG_DIR", str(tmp_path))
    add_remote("same-name", "ws://manager.example:8765/gdb")

    with pytest.raises(ValueError, match="already exists"):
        add_gdb("SAME-NAME", "gdb://debug-target.example.invalid:3333")


def test_remote_gdb_inventory_is_metadata_only(tmp_path, monkeypatch):
    monkeypatch.setenv("GWPROV_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("GWPROV_LEASE_DIR", str(tmp_path / "leases"))
    add_gdb("test-target", "gdb://debug-target.example.invalid:3333")

    import gwprov.backends

    monkeypatch.setattr(gwprov.backends, "enumerate_local_probes", lambda: [])
    row = next(item for item in inventory() if item["name"] == "test-target")

    assert row["id"] == "gdb:debug-target.example.invalid:3333"
    assert row["type"] == "remote GDB"
    assert row["state"] == "registered"
    assert row["url"] == "gdb://debug-target.example.invalid:3333"


def test_device_inventory_and_completion_include_raw_gdb_without_connecting(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GWPROV_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("GWPROV_LEASE_DIR", str(tmp_path / "leases"))
    add_gdb("test-target", "gdb://debug-target.example.invalid:3333")

    from gwprov import daemon, devices, gwemu_manager
    import gwprov.backends

    monkeypatch.setattr(gwemu_manager, "instances", lambda: [])
    monkeypatch.setattr(gwemu_manager, "_process_scan_restriction", lambda: None)
    monkeypatch.setattr(daemon, "device_snapshot", lambda **kwargs: [])
    rows = devices.device_rows()
    row = next(item for item in rows if item["name"] == "test-target")
    assert row["id"] == "gdb:debug-target.example.invalid:3333"
    assert row["kind"] == "remote-gdb"
    assert row["status"] == "unknown"
    assert row["remoteGdb"] is True

    from gwprov.target_leases import TargetLease

    lease = TargetLease(
        "remote-gdb:gdb://debug-target.example.invalid:3333",
        "gwprov remote GDB debug session",
        register_with_daemon=False,
    )
    lease.acquire()
    try:
        active = next(
            item for item in devices.device_rows() if item["name"] == "test-target"
        )
        assert active["status"] == "busy"
    finally:
        lease.release()

    import psutil

    monkeypatch.setattr(psutil, "process_iter", lambda *args, **kwargs: [])
    monkeypatch.setattr(gwprov.backends, "enumerate_local_probes", lambda: [])
    assert "test-target" in devices.device_identifiers(include_names=True)
    assert "gdb:debug-target.example.invalid:3333" in devices.device_identifiers()


def test_device_inventory_uses_daemon_polled_raw_gdb_state(tmp_path, monkeypatch):
    monkeypatch.setenv("GWPROV_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("GWPROV_LEASE_DIR", str(tmp_path / "leases"))
    add_gdb("test-target", "gdb://debug-target.example.invalid:3333")

    from gwprov import daemon, devices, gwemu_manager
    import gwprov.backends

    polled = {
        "id": "gdb:debug-target.example.invalid:3333",
        "kind": "remote-gdb",
        "backend": "Remote GDB",
        "name": "test-target",
        "remoteGdb": True,
        "status": "running",
        "application": "Retro-Go SD v2.0.0-rc15",
        "installed": "Stock Zelda; Retro-Go SD v2.0.0-rc15",
        "detail": None,
    }
    monkeypatch.setattr(gwemu_manager, "instances", lambda: [])
    monkeypatch.setattr(gwemu_manager, "_process_scan_restriction", lambda: None)
    monkeypatch.setattr(daemon, "device_snapshot", lambda **kwargs: [polled])
    monkeypatch.setattr(gwprov.backends, "enumerate_local_probes", lambda: [])

    rows = devices.device_rows()
    assert len(rows) == 1
    assert rows[0]["id"] == "gdb:debug-target.example.invalid:3333"
    assert rows[0]["status"] == "running"
    assert rows[0]["application"] == "Retro-Go SD v2.0.0-rc15"


def test_daemon_error_is_attached_to_registered_gdb_device(tmp_path, monkeypatch):
    monkeypatch.setenv("GWPROV_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("GWPROV_LEASE_DIR", str(tmp_path / "leases"))
    add_gdb("test-target", "gdb://debug-target.example.invalid:3333")

    from gwprov import daemon, devices, gwemu_manager

    monkeypatch.setattr(gwemu_manager, "instances", lambda: [])
    monkeypatch.setattr(gwemu_manager, "_process_scan_restriction", lambda: None)
    monkeypatch.setattr(
        daemon,
        "device_snapshot",
        lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("stale daemon did not exit after its idle grace period")
        ),
    )
    monkeypatch.setattr("gwprov.target_leases.lease_owner", lambda key: None)
    monkeypatch.setattr("gwprov.device_assignments.assignments", lambda: {})

    rows = devices.device_rows()

    assert len(rows) == 1
    assert rows[0]["id"] == "gdb:debug-target.example.invalid:3333"
    assert "Daemon state unavailable" in rows[0]["detail"]
    assert "stale daemon did not exit" in rows[0]["detail"]


def test_daemon_discovery_registers_remote_gdb_for_heartbeat_polling(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GWPROV_CONFIG_DIR", str(tmp_path / "config"))
    add_gdb("test-target", "gdb://debug-target.example.invalid:3333")
    monkeypatch.setattr("gwprov.backends.enumerate_local_probes", lambda: [])
    monkeypatch.setattr("gwprov.adapters.list_remote", lambda: [])

    from gwprov.daemon import HardwarePoller, Service

    service = Service()
    monkeypatch.setattr(HardwarePoller, "poll", lambda self: None)
    service._discover_hardware()

    key = "remote-gdb:gdb://debug-target.example.invalid:3333"
    assert key in service.hardware
    row = service.hardware[key].snapshot()
    assert row["id"] == "gdb:debug-target.example.invalid:3333"
    assert row["kind"] == "remote-gdb"
    assert row["remoteGdb"] is True


def test_daemon_poller_records_remote_gdb_firmware_and_execution_state(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GWPROV_LEASE_DIR", str(tmp_path / "leases"))
    from gwprov import daemon, debug_shell, hw_lifecycle

    opened = []

    class Backend:
        _lease = None

        closed = False

        def __init__(self, host, port, **kwargs):
            assert host == "debug-target.example.invalid"
            assert port == 3333
            assert kwargs["daemon_managed"] is True

        def open(self):
            opened.append(self)

        def close(self):
            self.closed = True

    runtime = {
        "locked": False,
        "stubDetected": False,
        "halted": False,
        "serviceState": None,
        "banks": [{"bank": 2, "image": "Retro-Go SD v2.0.0-rc15", "size": 1234}],
        "application": "Retro-Go SD v2.0.0-rc15",
        "installed": "Stock Zelda; Retro-Go SD v2.0.0-rc15",
        "detail": None,
    }
    monkeypatch.setattr(debug_shell, "RemoteGDBBackend", Backend)
    monkeypatch.setattr(hw_lifecycle, "identify_runtime", lambda *a, **kw: runtime)
    poller = daemon.HardwarePoller(
        {
            "name": "test-target",
            "host": "debug-target.example.invalid",
            "port": 3333,
            "url": "gdb://debug-target.example.invalid:3333",
            "remoteGdb": True,
            "deviceId": "gdb:debug-target.example.invalid:3333",
            "key": "remote-gdb:gdb://debug-target.example.invalid:3333",
        }
    )

    poller.poll()
    row = poller.snapshot()

    assert len(opened) == 1
    assert row["status"] == "running"
    assert row["application"] == "Retro-Go SD v2.0.0-rc15"
    assert row["installed"] == "Stock Zelda; Retro-Go SD v2.0.0-rc15"
    assert row["runtime"] == runtime
    assert opened[0].closed is True
    assert poller.backend is None


def test_raw_gdb_device_does_not_claim_assigned_profile_is_installed():
    from gwprov.cli.text import device_detail_data

    data = device_detail_data(
        {
            "id": "gdb:debug-target.example.invalid:3333",
            "kind": "remote-gdb",
            "name": "test-target",
            "assignedProfile": "/profiles/zelda-rg",
            "status": "unknown",
            "application": "Unknown",
        }
    )

    assert data["summary"]["profile"] == "/profiles/zelda-rg"
    assert data["firmware"] == {
        "bootloader": "Unknown",
        "bank1": "Unknown",
        "bank2": "Unknown",
        "running": "Unknown",
        "lock_state": "Unknown",
    }


def test_raw_gdb_device_selects_direct_debug_backend(tmp_path, monkeypatch):
    from gwprov.cli.main import _debug_python, build_parser

    row = {
        "id": "gdb:debug-target.example.invalid:3333",
        "kind": "hardware",
        "name": "test-target",
        "remoteGdb": True,
        "gdbUrl": "gdb://debug-target.example.invalid:3333",
        "gdbHost": "debug-target.example.invalid",
        "gdbPort": 3333,
        "application": "Unknown",
        "installed": "Unknown",
    }
    monkeypatch.setattr("gwprov.devices.device_rows", lambda: [row])
    captured = {}

    def fake_shell(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("gwprov.debug_shell.python_shell", fake_shell)
    args = build_parser().parse_args(["debug", "python", "test-target"])

    assert _debug_python(args) is None
    assert captured["target"] == "hardware"
    assert captured["remote_gdb_endpoints"] == [
        {
            "name": "test-target",
            "host": "debug-target.example.invalid",
            "port": 3333,
            "url": "gdb://debug-target.example.invalid:3333",
        }
    ]


def test_remote_gdb_backend_uses_target_xml_and_releases_lease(tmp_path, monkeypatch):
    from gwprov.debug_shell import RemoteGDBBackend

    class FakeSocket:
        def __init__(self):
            self.sent = []

        def settimeout(self, timeout):
            pass

        def setsockopt(self, *args):
            pass

        def connect(self, endpoint):
            self.endpoint = endpoint

        def makefile(self, *args, **kwargs):
            return self

        def sendall(self, data):
            self.sent.append(data)

        def close(self):
            pass

    monkeypatch.setenv("GWPROV_LEASE_DIR", str(tmp_path / "leases"))
    fake_socket = FakeSocket()
    monkeypatch.setattr("gwprov.debug_shell.socket.socket", lambda *args: fake_socket)
    backend = RemoteGDBBackend(
        "debug-target.example.invalid", 3333, daemon_managed=True
    )
    xml = (
        b'<target><feature name="org.gnu.gdb.arm.core">'
        b'<reg name="r0" regnum="0" bitsize="32" type="uint32"/>'
        b'<reg name="pc" regnum="15" bitsize="32" type="code_ptr"/>'
        b'<reg name="xpsr" regnum="25" bitsize="32" type="uint32"/>'
        b"</feature></target>"
    )
    commands = []

    def send(command):
        commands.append(command)
        return b"l" + xml if command.startswith(b"qXfer:") else b"OK"

    monkeypatch.setattr(backend, "_send_command", send)

    backend.open()
    assert backend.register_numbers == {"r0": 0, "pc": 15, "xpsr": 25}
    assert backend.target_xml_error is None
    assert not any(packet.startswith(b"$c#") for packet in fake_socket.sent)
    assert backend._is_running is None
    backend.close()


def test_raw_gdb_backend_falls_back_when_target_xml_is_unsupported(
    tmp_path, monkeypatch
):
    from gnwmanager.ocdbackend.gdb_backend import GDBError

    class FakeSocket:
        def __init__(self):
            self.sent = []

        def settimeout(self, timeout):
            pass

        def setsockopt(self, *args):
            pass

        def connect(self, endpoint):
            pass

        def makefile(self, *args, **kwargs):
            return self

        def sendall(self, data):
            self.sent.append(data)

        def close(self):
            pass

    monkeypatch.setenv("GWPROV_LEASE_DIR", str(tmp_path / "leases"))
    monkeypatch.setattr("gwprov.debug_shell.socket.socket", lambda *args: FakeSocket())
    from gwprov.debug_shell import RemoteGDBBackend

    backend = RemoteGDBBackend(
        "debug-target.example.invalid", 3333, daemon_managed=True
    )
    monkeypatch.setattr(
        backend,
        "_send_command",
        lambda command: (
            b"OK"
            if command.startswith(b"qSupported:")
            else (_ for _ in ()).throw(
                GDBError(
                    "GDB error response: 01 for qXfer:features:read:target.xml:0,fff"
                )
            )
        ),
    )

    backend.open()
    assert backend.target_xml_error
    assert backend._register_index("pc") == 15
    backend.close()


def test_debug_session_uses_gdb_remote_breakpoint_packets():
    from gwprov.debug_shell import DebugSession

    class Backend:
        def __init__(self):
            self.commands = []

        def _send_command(self, command):
            self.commands.append(command)
            return b"OK"

    backend = Backend()
    session = DebugSession(backend, "hardware")

    assert session.bp(0x08001235) == "OK"
    assert backend.commands == [b"Z1,8001234,2"]
    assert session.clear_bp(0x08001235) == "OK"
    assert backend.commands[-1] == b"z1,8001234,2"
    assert session.breakpoints() == []
