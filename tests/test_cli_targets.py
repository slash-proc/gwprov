import importlib
from types import SimpleNamespace

import pytest

cli = importlib.import_module("gwprov.cli.main")


def test_bare_polymorphic_target_defaults_to_device():
    assert cli._resolve_profile_or_device_target("unit-a") == ("device", "unit-a")


def test_profile_target_requires_and_accepts_prefix(monkeypatch):
    from gwprov import devices
    from gwprov import profiles

    monkeypatch.setattr(devices, "device_rows", lambda: [])
    monkeypatch.setattr(profiles, "list_profiles", lambda: [{"name": "demo"}])

    with pytest.raises(ValueError, match="prefix it with `profile:`"):
        cli._resolve_device_selector("demo")
    assert cli._resolve_profile_or_device_target("profile:demo") == ("profile", "demo")


def test_start_hardware_gives_actionable_unsupported_message(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_resolve_profile_or_device_target",
        lambda target: ("device", target.removeprefix("device:")),
    )
    monkeypatch.setattr(cli, "_resolve_device_selector", lambda target: {"kind": "hardware"})
    args = SimpleNamespace(
        target="unit-a",
        name=None,
        gdb_port=None,
        audio=False,
        headless=False,
        timeline=None,
        record_timeline=None,
        timing_mode="default",
        icount=None,
        rtc_epoch=None,
        gwemu_bin=None,
    )

    with pytest.raises(ValueError, match="cannot start a hardware device"):
        cli._start_profile(args)


def test_reset_hardware_uses_backend_reset_and_closes(monkeypatch, capsys):
    calls = []

    class Backend:
        def __init__(self, unique_id, **kwargs):
            calls.append(("init", unique_id, kwargs["operation"]))

        def open(self):
            calls.append(("open",))

        def reset(self):
            calls.append(("reset",))

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(
        cli,
        "_resolve_device_selector",
        lambda _: {
            "id": "probe:unit-a",
            "kind": "hardware",
            "probeId": "unit-a",
            "name": "Unit A",
        },
    )
    monkeypatch.setattr("gwprov.backends.SelectedPyOCDBackend", Backend)

    assert cli._device_runtime_control(SimpleNamespace(device="unit-a", action="reset")) == 0
    assert [call[0] for call in calls] == ["init", "open", "reset", "close"]
    assert "Reset Unit A." in capsys.readouterr().out
