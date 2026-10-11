import pytest

from gwprov.target_leases import (
    DeviceBusyError, TargetLease, external_local_owner, lease_owner,
)


def test_gdb_backend_manager_is_not_a_hardware_owner(monkeypatch):
    import psutil

    class Process:
        def __init__(self, pid, name, cmdline):
            self.info = {"pid": pid, "name": name, "cmdline": cmdline,
                         "uids": None}

    processes = [
        Process(999_991, "gnwmanager", ["gnwmanager", "-b", "gdb", "serve"]),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: processes)
    assert external_local_owner() is None


def test_probe_backed_manager_remains_a_hardware_owner(monkeypatch):
    import psutil

    class Process:
        def __init__(self, pid, name, cmdline):
            self.info = {"pid": pid, "name": name, "cmdline": cmdline,
                         "uids": None}

    processes = [
        Process(999_992, "gnwmanager", ["gnwmanager", "-b", "pyocd", "info"]),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda _attrs: processes)
    owner = external_local_owner()
    assert owner is not None
    assert owner["key"] == "external-gnwmanager"


def test_lease_reports_owner_and_clears_when_released(tmp_path, monkeypatch):
    monkeypatch.setenv("GWPROV_LEASE_DIR", str(tmp_path))
    lease = TargetLease("probe:serial-123", "hardware profiling", wait=0).acquire()
    try:
        owner = lease_owner("probe:serial-123")
        assert owner["operation"] == "hardware profiling"
        assert owner["pid"]
        with pytest.raises(DeviceBusyError, match="hardware profiling"):
            TargetLease("probe:serial-123", "gwprov ps", wait=0).acquire()
    finally:
        lease.release()

    assert lease_owner("probe:serial-123") is None
