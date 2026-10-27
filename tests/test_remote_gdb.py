import json
import time

import pytest

from gwprov.adapters import (
    add_gdb,
    add_remote,
    inventory,
    list_gdb,
    list_remote,
    remove_remote,
    set_target_profile,
)


def test_remote_gdb_registry_coexists_with_gnwmanager_adapters(tmp_path, monkeypatch):
    monkeypatch.setenv("GWPROV_CONFIG_DIR", str(tmp_path))

    add_remote("manager", "ws://manager.example:8765/gdb", origin="https://builder.example")
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

    assert remove_remote("test-target")["url"] == "gdb://debug-target.example.invalid:3333"
    assert list_gdb() == []
    assert len(list_remote()) == 1


def test_adapter_target_profile_is_explicit_and_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("GWPROV_CONFIG_DIR", str(tmp_path))

    remote = add_remote("h7-manager", "ws://manager.example:8765/gdb", target_profile="stm32h7b0")
    gdb = add_gdb("h7-gdb", "gdb://debug-target.example.invalid:3333", target_profile="stm32h7b0")

    assert remote["targetProfile"] == "stm32h7b0"
    assert gdb["targetProfile"] == "stm32h7b0"
    assert list_remote()[0]["targetProfile"] == "stm32h7b0"
    assert list_gdb()[0]["targetProfile"] == "stm32h7b0"

    with pytest.raises(ValueError, match="target profile"):
        add_gdb("unknown", "gdb://other.example:3333", target_profile="unknown-mcu")


def test_adapter_target_profile_can_be_updated_without_reregistering(tmp_path, monkeypatch):
    monkeypatch.setenv("GWPROV_CONFIG_DIR", str(tmp_path))
    add_gdb("rpi400-hw", "gdb://10.2.3.122:3333")

    updated = set_target_profile("rpi400-hw", "stm32h7b0")
    assert updated["targetProfile"] == "stm32h7b0"
    assert list_gdb()[0]["targetProfile"] == "stm32h7b0"
    assert set_target_profile("rpi400-hw", None)["name"] == "rpi400-hw"
    assert "targetProfile" not in list_gdb()[0]


def test_registered_target_profile_is_accepted_without_adapter_or_cli_changes(
    tmp_path, monkeypatch
):
    import importlib

    from gwprov import target_controls
    from gwprov.target_controls import register_target_control

    cli = importlib.import_module("gwprov.cli.main")

    monkeypatch.setenv("GWPROV_CONFIG_DIR", str(tmp_path))

    class SyntheticControl:
        name = "synthetic"
        facet_names = ("runtime",)

        def status(self, session, target_profile):
            raise NotImplementedError

        def set(self, session, facet, enabled, target_profile):
            raise NotImplementedError

    monkeypatch.setitem(target_controls._CONTROL_REGISTRY, "synthetic-mcu", {})
    register_target_control("synthetic-mcu", SyntheticControl())

    adapter = add_gdb(
        "synthetic", "gdb://synthetic.example.invalid:3333", target_profile="SYNTHETIC-MCU"
    )
    assert adapter["targetProfile"] == "synthetic-mcu"

    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "adapters",
            "add",
            "synthetic-2",
            "gdb://synthetic-2.example.invalid:3333",
            "--target-profile",
            "synthetic-mcu",
        ]
    )
    assert args.target_profile == "synthetic-mcu"


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


def test_adapter_inventory_keeps_remote_gdb_when_local_probe_discovery_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("GWPROV_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("GWPROV_LEASE_DIR", str(tmp_path / "leases"))
    add_gdb("rpi400-hw", "gdb://10.2.3.122:3333", target_profile="stm32h7b0")

    import gwprov.backends

    monkeypatch.setattr(
        gwprov.backends,
        "enumerate_local_probes",
        lambda: (_ for _ in ()).throw(RuntimeError("PyOCD USB backend unavailable")),
    )

    rows = inventory()

    remote = next(row for row in rows if row["name"] == "rpi400-hw")
    local = next(row for row in rows if row["id"] == "probe:*")
    assert remote["state"] == "registered"
    assert remote["targetProfile"] == "stm32h7b0"
    assert local["state"] == "unknown"
    assert "PyOCD USB backend unavailable" in local["detail"]


def test_device_inventory_and_completion_include_raw_gdb_without_connecting(tmp_path, monkeypatch):
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
        active = next(item for item in devices.device_rows() if item["name"] == "test-target")
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


def test_daemon_discovery_registers_remote_gdb_for_heartbeat_polling(tmp_path, monkeypatch):
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


def test_device_snapshot_reaps_dead_remote_gdb_session_and_resumes_polling(tmp_path, monkeypatch):
    monkeypatch.setenv("GWPROV_LEASE_DIR", str(tmp_path / "leases"))

    from gwprov import daemon

    key = "remote-gdb:gdb://debug-target.example.invalid:3333"
    service = daemon.Service()
    service.last_discovery = time.monotonic()

    class Poller:
        def __init__(self):
            self.key = key
            self.finished = 0
            self.state = {
                "id": "gdb:debug-target.example.invalid:3333",
                "kind": "remote-gdb",
                "name": "test-target",
                "status": "busy",
                "busyOwner": {"pid": 3075016, "operation": "gwprov remote GDB debug session"},
            }

        def snapshot(self):
            return self.state.copy()

        def invalidate_installed(self):
            pass

        def operation_finished(self):
            self.finished += 1
            self.state.pop("busyOwner", None)
            self.state.update(status="unknown", detail="Refreshing device state after operation")

    poller = Poller()
    service.hardware[key] = poller
    service.leases["abandoned-session"] = {
        "key": key,
        "pid": 3075016,
        "operation": "gwprov remote GDB debug session",
        "lastHeartbeat": time.monotonic(),
    }
    service.hardware_pause_tokens[key] = {
        "abandoned-session": {
            "pid": 3075016,
            "since": time.monotonic(),
            "expires": time.monotonic() + 60,
        }
    }
    service.hardware_paused.add(key)
    monkeypatch.setattr(daemon, "_pid_exists", lambda pid: False)

    rows = service.snapshot_devices()["devices"]

    assert service.leases == {}
    assert service.hardware_pause_tokens == {}
    assert key not in service.hardware_paused
    assert poller.monitor_paused is False
    assert poller.finished == 1
    assert rows[0]["id"] == "gdb:debug-target.example.invalid:3333"
    assert rows[0]["status"] == "unknown"


def test_device_snapshot_keeps_live_remote_gdb_session_paused(tmp_path, monkeypatch):
    monkeypatch.setenv("GWPROV_LEASE_DIR", str(tmp_path / "leases"))

    from gwprov import daemon

    key = "remote-gdb:gdb://debug-target.example.invalid:3333"
    service = daemon.Service()
    service.last_discovery = time.monotonic()

    class Poller:
        def __init__(self):
            self.key = key
            self.finished = 0
            self.state = {
                "id": "gdb:debug-target.example.invalid:3333",
                "kind": "remote-gdb",
                "status": "busy",
            }

        def snapshot(self):
            return self.state.copy()

        def invalidate_installed(self):
            pass

        def operation_finished(self):
            self.finished += 1
            self.state["status"] = "unknown"

    poller = Poller()
    service.hardware[key] = poller
    service.leases["live-session"] = {
        "key": key,
        "pid": 3075016,
        "operation": "gwprov remote GDB debug session",
        "lastHeartbeat": time.monotonic(),
    }
    service.hardware_pause_tokens[key] = {
        "live-session": {
            "pid": 3075016,
            "since": time.monotonic(),
            "expires": time.monotonic() + 60,
        }
    }
    service.hardware_paused.add(key)
    monkeypatch.setattr(daemon, "_pid_exists", lambda pid: True)

    rows = service.snapshot_devices()["devices"]

    assert "live-session" in service.leases
    assert "live-session" in service.hardware_pause_tokens[key]
    assert key in service.hardware_paused
    assert poller.finished == 0
    assert rows[0]["status"] == "busy"


def test_daemon_poller_records_remote_gdb_firmware_and_execution_state(tmp_path, monkeypatch):
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
        "targetProfile": "stm32h7b0",
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
            "targetProfile": "stm32h7b0",
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
    backend = RemoteGDBBackend("debug-target.example.invalid", 3333, daemon_managed=True)
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


def test_remote_gdb_prefers_advertised_msp_over_sp():
    from gwprov.debug_shell import RemoteGDBBackend

    backend = RemoteGDBBackend("example.invalid", 3333, daemon_managed=True)
    backend.register_numbers = {"sp": 13, "msp": 17}
    backend._is_running = False
    commands = []
    backend._send_command = lambda command: commands.append(command) or b"78563412"

    assert backend.read_register("msp") == 0x12345678
    assert commands == [b"p11"]


def test_remote_gdb_uses_advertised_sp_as_generic_msp_alias():
    from gwprov.debug_shell import RemoteGDBBackend

    backend = RemoteGDBBackend("example.invalid", 3333, daemon_managed=True)
    backend.register_numbers = {"sp": 13}
    backend._is_running = False
    commands = []
    backend._send_command = lambda command: commands.append(command) or b"f0ff0120"

    assert backend.read_register("msp") == 0x2001FFF0
    assert commands == [b"pd"]


@pytest.mark.parametrize(
    ("name", "selector", "dcrdr", "expected"),
    [
        ("msp", 0x11, 0x2001FFF0, 0x2001FFF0),
        ("psp", 0x12, 0x20010000, 0x20010000),
        ("control", 0x14, 0x02010001, 0x02),
        ("primask", 0x14, 0x02010001, 0x01),
    ],
)
def test_remote_gdb_reads_unadvertised_cortex_m_special_register(name, selector, dcrdr, expected):
    from gwprov.debug_shell import RemoteGDBBackend

    backend = RemoteGDBBackend(
        "example.invalid", 3333, daemon_managed=True, target_profile="stm32h7b0"
    )
    backend.register_numbers = {"xpsr": 16}
    backend._is_running = False
    writes = []
    backend.write_memory = lambda address, data: writes.append((address, data))
    values = {0xE000EDF0: (1 << 17) | (1 << 16), 0xE000EDF8: dcrdr}
    backend.read_memory = lambda address, size: values[address].to_bytes(size, "little")

    assert backend.read_register(name) == expected
    assert writes == [(0xE000EDF4, selector.to_bytes(4, "little"))]


@pytest.mark.parametrize(
    ("name", "initial", "requested", "expected", "selector"),
    [
        ("msp", 0, 0x2001FEF4, 0x2001FEF4, 0x11),
        ("psp", 0, 0x20010004, 0x20010004, 0x12),
        ("control", 0x02010001, 0x03, 0x03010001, 0x14),
        ("faultmask", 0x02000001, 0x01, 0x02010001, 0x14),
        ("basepri", 0x02010001, 0x80, 0x02018001, 0x14),
        ("primask", 0x02010001, 0x00, 0x02010000, 0x14),
    ],
)
def test_remote_gdb_writes_unadvertised_cortex_m_special_register(
    name, initial, requested, expected, selector
):
    from gwprov.debug_shell import RemoteGDBBackend

    backend = RemoteGDBBackend(
        "example.invalid", 3333, daemon_managed=True, target_profile="stm32h7b0"
    )
    backend.register_numbers = {"xpsr": 16}
    backend._is_running = False
    special_registers = {0x11: 0x2001FFF0, 0x12: 0x20010000, 0x14: initial}
    dcrdr = [0]
    writes = []

    def read_memory(address, size):
        if address == 0xE000EDF0:
            return ((1 << 17) | (1 << 16)).to_bytes(size, "little")
        if address == 0xE000EDF8:
            return dcrdr[0].to_bytes(size, "little")
        raise AssertionError(f"unexpected register read: {address:#x}")

    def write_memory(address, data):
        value = int.from_bytes(data, "little")
        writes.append((address, value))
        if address == 0xE000EDF8:
            dcrdr[0] = value
        elif address == 0xE000EDF4:
            selector_value = value & 0x1F
            if value & (1 << 16):
                special_registers[selector_value] = dcrdr[0]
            else:
                dcrdr[0] = special_registers[selector_value]
        else:
            raise AssertionError(f"unexpected register write: {address:#x}")

    backend.read_memory = read_memory
    backend.write_memory = write_memory
    backend.write_register(name, requested)
    if name in ("msp", "psp"):
        assert (0xE000EDF8, requested) in writes
    else:
        assert (0xE000EDF8, expected) in writes
    assert (0xE000EDF4, (1 << 16) | selector) in writes
    if name in ("msp", "psp"):
        expected_register = requested
    else:
        shift = {"control": 24, "faultmask": 16, "basepri": 8, "primask": 0}[name]
        mask = {"control": 0xFF, "faultmask": 1, "basepri": 0xFF, "primask": 0xFF}[name]
        expected_register = (expected >> shift) & mask
    assert backend.read_register(name) == expected_register


@pytest.mark.parametrize("target_state", [None, True])
def test_remote_gdb_special_register_write_requires_halted_known_target(target_state):
    from gwprov.debug_shell import RemoteGDBBackend

    backend = RemoteGDBBackend("example.invalid", 3333, daemon_managed=True)
    backend.register_numbers = {"sp": 13}
    backend._is_running = False if target_state is None else target_state
    if target_state is not None:
        backend.target_profile = "stm32h7b0"
        with pytest.raises(RuntimeError, match="halted target"):
            backend.write_register("psp", 0x20010000)
    else:
        with pytest.raises(ValueError, match="does not advertise"):
            backend.write_register("psp", 0x20010000)


def test_remote_gdb_special_register_requires_halted_known_target():
    from gwprov.debug_shell import RemoteGDBBackend

    backend = RemoteGDBBackend("example.invalid", 3333, daemon_managed=True)
    backend.register_numbers = {"sp": 13}
    backend._is_running = False
    with pytest.raises(ValueError, match="does not advertise"):
        backend.read_register("psp")

    backend.target_profile = "stm32h7b0"
    backend._is_running = True
    with pytest.raises(RuntimeError, match="halted target"):
        backend.read_register("psp")


def test_remote_gdb_special_register_readiness_timeout(monkeypatch):
    from gwprov.debug_shell import RemoteGDBBackend

    backend = RemoteGDBBackend(
        "example.invalid", 3333, daemon_managed=True, target_profile="stm32h7b0"
    )
    backend.register_numbers = {"sp": 13}
    backend._is_running = False
    backend.write_memory = lambda address, data: None
    states = iter(((1 << 17) | (1 << 16), 1 << 17, 1 << 17))
    backend.read_memory = lambda address, size: (
        next(states) if address == 0xE000EDF0 else 0
    ).to_bytes(size, "little")
    ticks = iter((0.0, 0.0, 0.2))
    monkeypatch.setattr("gwprov.debug_shell.time.monotonic", lambda: next(ticks))

    with pytest.raises(RuntimeError, match="timed out"):
        backend.read_register("control")


def test_remote_gdb_special_register_rejects_initial_not_ready():
    from gwprov.debug_shell import RemoteGDBBackend

    backend = RemoteGDBBackend(
        "example.invalid", 3333, daemon_managed=True, target_profile="stm32h7b0"
    )
    backend.register_numbers = {"sp": 13}
    backend._is_running = False
    writes = []
    backend.write_memory = lambda address, data: writes.append((address, data))
    backend.read_memory = lambda address, size: (1 << 17).to_bytes(size, "little")

    with pytest.raises(RuntimeError, match="not ready"):
        backend.read_register("psp")
    assert writes == []


def test_remote_gdb_special_register_rejects_target_leaving_halt():
    from gwprov.debug_shell import RemoteGDBBackend

    backend = RemoteGDBBackend(
        "example.invalid", 3333, daemon_managed=True, target_profile="stm32h7b0"
    )
    backend.register_numbers = {"sp": 13}
    backend._is_running = False
    states = iter(((1 << 17) | (1 << 16), 1 << 16))
    backend.write_memory = lambda address, data: None
    backend.read_memory = lambda address, size: next(states).to_bytes(size, "little")

    with pytest.raises(RuntimeError, match="left debug halt"):
        backend.read_register("psp")


def test_raw_gdb_backend_falls_back_when_target_xml_is_unsupported(tmp_path, monkeypatch):
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

    backend = RemoteGDBBackend("debug-target.example.invalid", 3333, daemon_managed=True)
    monkeypatch.setattr(
        backend,
        "_send_command",
        lambda command: (
            b"OK"
            if command.startswith(b"qSupported:")
            else (_ for _ in ()).throw(
                GDBError("GDB error response: 01 for qXfer:features:read:target.xml:0,fff")
            )
        ),
    )

    backend.open()
    assert backend.target_xml_error
    assert backend._register_index("pc") == 15
    backend.close()


def test_raw_gdb_backend_stops_after_qsupported_transport_failure(tmp_path, monkeypatch):
    from gnwmanager.ocdbackend.gdb_backend import GDBError

    class FakeSocket:
        def settimeout(self, timeout):
            pass

        def setsockopt(self, *args):
            pass

        def connect(self, endpoint):
            pass

        def makefile(self, *args, **kwargs):
            return self

        def close(self):
            pass

    monkeypatch.setenv("GWPROV_LEASE_DIR", str(tmp_path / "leases"))
    monkeypatch.setattr("gwprov.debug_shell.socket.socket", lambda *args: FakeSocket())
    from gwprov.debug_shell import RemoteGDBBackend

    backend = RemoteGDBBackend("debug-target.example.invalid", 3333, daemon_managed=True)
    commands = []

    def send(command):
        commands.append(command)
        raise GDBError("Socket error while writing to the GDB stub: [Errno 32] Broken pipe")

    monkeypatch.setattr(backend, "_send_command", send)

    with pytest.raises(RuntimeError, match="qSupported negotiation.*Broken pipe"):
        backend.open()

    assert commands == [b"qSupported:multiprocess+;xmlRegisters=i386;qRelocInsn+"]


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


@pytest.mark.parametrize("transport", ("hardware", "remote-gdb"))
def test_watchdog_freeze_is_available_for_hardware_gdb_transports(transport):
    from gwprov.debug_shell import DebugSession
    from gwprov.hw_lifecycle import DBGMCU_APB3FZ1, DBGMCU_APB4FZ1, DBG_WWDG1, DBG_IWDG1

    class MemoryBackend:
        def __init__(self):
            self.memory = {DBGMCU_APB3FZ1: 0, DBGMCU_APB4FZ1: 0}
            self.writes = []

        def read_memory(self, address, size):
            return self.memory[address].to_bytes(size, "little")

        def write_memory(self, address, data):
            value = int.from_bytes(data, "little")
            self.writes.append((address, value))
            self.memory[address] = value

    backend = MemoryBackend()
    session = DebugSession(backend, transport, target_profile="stm32h7b0")

    result = session.enable_watchdog_freeze()

    assert result == {
        "WWDG1": {"enabled": True, "already_enabled": False},
        "IWDG1": {"enabled": True, "already_enabled": False},
    }
    assert backend.writes == [
        (DBGMCU_APB3FZ1, DBG_WWDG1),
        (DBGMCU_APB4FZ1, DBG_IWDG1),
    ]


def test_watchdog_freeze_remains_unavailable_for_gwemu():
    from gwprov.debug_shell import DebugSession

    class Backend:
        pass

    session = DebugSession(Backend(), "gwemu")

    with pytest.raises(ValueError, match="does not model"):
        session.enable_watchdog_freeze()


def test_remote_gdb_monitor_handoff_blocks_stale_poll_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("GWPROV_LEASE_DIR", str(tmp_path / "leases"))

    from gwprov import daemon

    key = "remote-gdb:gdb://debug-target.example.invalid:3333"
    poller = daemon.HardwarePoller(
        {
            "key": key,
            "id": "gdb:debug-target.example.invalid:3333",
            "name": "test target",
            "remoteGdb": True,
            "host": "debug-target.example.invalid",
            "port": 3333,
            "url": "gdb://debug-target.example.invalid:3333",
        }
    )
    service = daemon.Service()
    service.hardware[key] = poller

    result = service.dispatch(
        {
            "op": "target-pause",
            "key": key,
            "pauseToken": "foreground-owner",
            "pid": 12345,
            "waitSeconds": 30,
        }
    )

    assert result == {"paused": True}
    assert key in service.hardware_paused
    assert poller.monitor_paused is True

    # Simulate _poll_hardware using its previously copied unpaused snapshot.
    # The poller's own locked entry guard must prevent reopening the backend.
    poller.poll()
    assert poller.backend is None

    service.dispatch({"op": "target-resume", "key": key, "pauseToken": "foreground-owner"})
    assert poller.monitor_paused is False


def test_pause_device_monitor_rejects_daemon_refusal(monkeypatch):
    from gwprov import daemon

    monkeypatch.setattr(daemon, "request", lambda *args, **kwargs: {"paused": False})

    with pytest.raises(RuntimeError, match="refused to pause the monitor"):
        daemon.pause_device_monitor("remote-gdb:target", "handoff-token", 12345, 30)


class ReconnectSocket:
    """In-memory RSP peer; a poisoned old reader must never be consulted."""

    def __init__(self, data=b"", *, poisoned=False, connect_error=None, peer_error=None):
        import io

        self.reader = io.BytesIO(data)
        self.poisoned = poisoned
        self.connect_error = connect_error
        self.peer_error = peer_error
        self.sent = []
        self.timeouts = []
        self.closed = False
        self.peer = ("192.0.2.8", 3333)

    def getpeername(self):
        if self.peer_error:
            raise self.peer_error
        return self.peer

    def settimeout(self, timeout):
        assert not self.closed
        self.timeouts.append(timeout)

    def setsockopt(self, *args):
        pass

    def connect(self, address):
        self.connected_to = address
        if self.connect_error:
            raise self.connect_error

    def makefile(self, *args, **kwargs):
        return self

    def read(self, count):
        assert not self.poisoned, "read reused poisoned buffered stream"
        return self.reader.read(count)

    def sendall(self, data):
        assert not self.poisoned, "write reused poisoned stream"
        self.sent.append(data)

    def close(self):
        self.closed = True


def reconnect_owned_backend(monkeypatch, fresh):
    from types import SimpleNamespace

    from gwprov.debug_shell import RemoteGDBBackend

    backend = RemoteGDBBackend("unresolvable.example.invalid", 3333)
    old = ReconnectSocket(poisoned=True)
    backend._socket = old
    backend._sock_file = old
    backend._monitor_pause_token = "retained-monitor-pause"

    def forbidden(*args, **kwargs):
        raise AssertionError("reconnect surrendered or reacquired session ownership")

    backend._remote_lease = SimpleNamespace(fd=123, acquire=forbidden, release=forbidden)
    monkeypatch.setattr("gwprov.daemon.pause_device_monitor", forbidden)
    monkeypatch.setattr("gwprov.daemon.resume_device_monitor", forbidden)
    monkeypatch.setattr("gwprov.debug_shell.socket.socket", lambda *args: fresh)
    return backend, old


def reconnect_reply(payload):
    return b"+$" + payload + b"#" + f"{sum(payload) % 256:02x}".encode()


def test_reconnect_transport_retains_ownership_and_negotiates_fresh_stream(monkeypatch):
    xml = b'<target><feature><reg name="pc" regnum="15"/></feature></target>'
    fresh = ReconnectSocket(reconnect_reply(b"OK") + reconnect_reply(b"l" + xml))
    backend, old = reconnect_owned_backend(monkeypatch, fresh)
    lease = backend._remote_lease
    backend.register_numbers = {"stale": 88}
    backend.target_xml_error = "stale failure"

    assert backend.reconnect_transport() is backend
    assert old.closed
    assert old.sent == []
    assert backend._remote_lease is lease
    assert lease.fd == 123
    assert backend._monitor_pause_token == "retained-monitor-pause"
    assert fresh.connected_to == old.peer
    assert backend.register_numbers == {"pc": 15}
    assert backend.target_xml_error is None
    assert backend._is_running is None
    assert backend._transport_deadline is None
    commands = [packet.split(b"#")[0][1:] for packet in fresh.sent if packet.startswith(b"$")]
    assert commands == [
        b"qSupported:multiprocess+;xmlRegisters=i386;qRelocInsn+",
        b"qXfer:features:read:target.xml:0,fff",
    ]
    assert fresh.timeouts[-1] == 5.0


def test_reconnect_transport_connect_failure_closes_fresh_socket_retains_lease(monkeypatch):
    fresh = ReconnectSocket(connect_error=ConnectionRefusedError("refused"))
    backend, old = reconnect_owned_backend(monkeypatch, fresh)
    backend.register_numbers = {"stale": 88}
    with pytest.raises(ConnectionRefusedError, match="refused"):
        backend.reconnect_transport()
    assert old.closed and fresh.closed
    assert backend._socket is None and backend._sock_file is None
    assert backend._is_running is None
    assert backend._remote_lease.fd == 123
    assert backend._monitor_pause_token == "retained-monitor-pause"
    assert backend.register_numbers == {}
    assert backend._transport_deadline is None


def test_reconnect_transport_negotiation_timeout_stops_before_target_xml(monkeypatch):
    fresh = ReconnectSocket()
    backend, old = reconnect_owned_backend(monkeypatch, fresh)
    commands = []

    def send(command):
        commands.append(command)
        raise TimeoutError("partial RSP packet timeout")

    monkeypatch.setattr(backend, "_send_command", send)
    with pytest.raises(TimeoutError, match="partial RSP"):
        backend.reconnect_transport()
    assert len(commands) == 1 and commands[0].startswith(b"qSupported:")
    assert old.closed and fresh.closed
    assert backend._socket is None
    assert backend._is_running is None
    assert backend._remote_lease.fd == 123


def test_reconnect_transport_deadline_bounds_connect_reads_and_writes(monkeypatch):
    fresh = ReconnectSocket()
    backend, old = reconnect_owned_backend(monkeypatch, fresh)
    clock = [10.0]
    monkeypatch.setattr("gwprov.debug_shell.time.monotonic", lambda: clock[0])
    recorded = []

    def send(command):
        clock[0] = 11.0
        backend._write(b"handshake")
        recorded.append(fresh.timeouts[-1])
        clock[0] = 12.0
        fresh.reader.write(b"X")
        fresh.reader.seek(0)
        assert backend._read(1) == b"X"
        recorded.append(fresh.timeouts[-1])
        clock[0] = 15.0
        # Expiration prevents any later write, including retransmission.
        backend._write(b"must-not-send")

    monkeypatch.setattr(backend, "_send_command", send)
    with pytest.raises(TimeoutError, match="deadline exceeded"):
        backend.reconnect_transport(timeout=5.0)
    assert fresh.timeouts[0] == 5.0
    assert recorded == [4.0, 3.0]
    assert fresh.sent == [b"handshake"]
    assert old.closed and fresh.closed
    assert backend._remote_lease.fd == 123


def test_reconnect_transport_rejects_unowned_session_and_invalid_budget(monkeypatch):
    backend, old = reconnect_owned_backend(monkeypatch, ReconnectSocket())
    for timeout in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="finite and positive"):
            backend.reconnect_transport(timeout=timeout)
        assert not old.closed
    backend._remote_lease.fd = None
    with pytest.raises(RuntimeError, match="owned remote GDB session"):
        backend.reconnect_transport()
    assert not old.closed
    backend._remote_lease.fd = 123
    backend._monitor_pause_token = None
    with pytest.raises(RuntimeError, match="owned remote GDB session"):
        backend.reconnect_transport()
    assert not old.closed


def test_remote_gdb_preserves_caller_socket_timeout_outside_reconnect(monkeypatch):
    from gwprov.debug_shell import RemoteGDBBackend

    backend = RemoteGDBBackend("127.0.0.1", 3333, daemon_managed=True)
    transport = ReconnectSocket(b"X")
    backend._socket = transport
    backend._sock_file = transport
    transport.settimeout(0.125)

    assert backend._read(1) == b"X"
    backend._write(b"ordinary command")

    assert transport.timeouts[-1] == 0.125
    assert transport.sent == [b"ordinary command"]


def test_reconnect_peer_lookup_failure_discards_old_stream_and_retains_lease(monkeypatch):
    fresh = ReconnectSocket()
    backend, old = reconnect_owned_backend(monkeypatch, fresh)
    old.peer_error = OSError("peer unavailable")

    with pytest.raises(OSError, match="peer unavailable"):
        backend.reconnect_transport()

    assert old.closed
    assert backend._socket is None and backend._sock_file is None
    assert backend._is_running is None
    assert backend._remote_lease.fd == 123
    assert backend._monitor_pause_token == "retained-monitor-pause"
    assert fresh.sent == []
