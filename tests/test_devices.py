from types import SimpleNamespace

from gwprov import backends, devices, gwemu_manager, target_leases


def test_unified_device_inventory_keeps_multiple_probe_states(monkeypatch):
    monkeypatch.setattr(gwemu_manager, "instances", lambda: [])
    monkeypatch.setattr(gwemu_manager, "_process_scan_restriction", lambda: None)
    monkeypatch.setattr(target_leases, "external_local_owner", lambda: None)
    monkeypatch.setattr(target_leases, "lease_owner", lambda key: None)
    monkeypatch.setattr(backends, "enumerate_local_probes", lambda: [
        {"id": "probe-a", "name": "ST-Link A", "vendor": "ST", "backend": "pyocd"},
        {"id": "probe-b", "name": "ST-Link B", "vendor": "ST", "backend": "pyocd"},
    ])

    class Backend:
        def __init__(self, probe_id, **kwargs):
            self.probe_id = probe_id
            self.target = SimpleNamespace(part_number="STM32H7B0")

        def open(self):
            return self

        def close(self):
            pass

        def read_uint32(self, address):
            assert address == 0xE000EDF0
            return (1 << 17) if self.probe_id == "probe-b" else 0

    monkeypatch.setattr(backends, "SelectedPyOCDBackend", Backend)
    rows = devices.device_rows()
    assert [(row["probeId"], row["status"]) for row in rows] == [
        ("probe-a", "running"), ("probe-b", "halted")]
    assert all(row["application"] == "Unknown" for row in rows)


def test_busy_target_is_reported_without_opening_or_polling(monkeypatch):
    monkeypatch.setattr(gwemu_manager, "instances", lambda: [])
    monkeypatch.setattr(gwemu_manager, "_process_scan_restriction", lambda: None)
    monkeypatch.setattr(backends, "enumerate_local_probes", lambda: [
        {"id": "busy-probe", "name": "ST-Link", "vendor": "ST", "backend": "pyocd"},
    ])
    monkeypatch.setattr(target_leases, "external_local_owner", lambda: None)
    owner = {"key": "probe:busy-probe", "operation": "hardware profiling", "pid": 1234}
    monkeypatch.setattr(target_leases, "lease_owner",
                        lambda key: owner if key == "probe:busy-probe" else None)

    class MustNotOpen:
        def __init__(self, *args, **kwargs):
            raise AssertionError("busy target must not create a backend")

    monkeypatch.setattr(backends, "SelectedPyOCDBackend", MustNotOpen)
    rows = devices.device_rows()
    assert len(rows) == 1
    assert rows[0]["status"] == "busy"
    assert rows[0]["application"] == "Unknown"
    assert rows[0]["busyOwner"] == owner
    assert "Target traffic skipped" in rows[0]["detail"]


def test_process_list_text_is_grouped_and_wraps_long_values():
    rows = [
        {"kind": "gwemu", "pid": 42, "status": "running", "running": True,
         "display": True, "gdbPort": 3333, "application": "In game",
         "profile": "/home/user/profiles/a-very-long-profile-name"},
        {"kind": "hardware", "status": "busy", "application": "Unknown",
         "name": "STM32H7B0", "probeId": "a-very-long-probe-identifier-value",
         "vendor": "ST", "detail": "Target traffic skipped during hardware profiling."},
    ]

    from gwprov.cli.text import render_process_list
    rendered = render_process_list(rows, title="GWProv devices", width=60)

    assert "GWProv devices (2)" in rendered
    assert "GWemu\n  RUNNING  a-very-long-profile-name" in rendered
    assert "PID 42 · Display on · GDB :3333" in rendered
    assert "Hardware\n  BUSY  STM32H7B0" in rendered
    assert "Target traffic skipped during hardware" in rendered
    assert all(len(line) <= 60 for line in rendered.splitlines())


def test_gwemu_ps_shows_halted_state_in_compact_text(monkeypatch, capsys):
    row = {"pid": 77, "status": "halted", "running": False, "display": True,
           "gdbPort": 3333, "application": "Game menu", "profile": "/profiles/dkc"}
    monkeypatch.setattr(gwemu_manager, "instances", lambda: [row.copy()])
    monkeypatch.setattr(gwemu_manager, "_application_state", lambda instance: "Game menu")

    assert gwemu_manager.show_instances() == 0
    output = capsys.readouterr().out
    assert "GWemu instances (1)" in output
    assert "HALTED  dkc" in output
    assert "PID 77 · Display on · GDB :3333" in output
    assert "Application: Game menu" in output
