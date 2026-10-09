import pytest

from gwprov.target_leases import DeviceBusyError, TargetLease, lease_owner


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
