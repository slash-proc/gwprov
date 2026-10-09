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
