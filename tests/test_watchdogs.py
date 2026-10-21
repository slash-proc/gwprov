import json
import importlib
from types import SimpleNamespace

import pytest

from gwprov.debug_shell import DebugSession
from gwprov.hw_lifecycle import DBGMCU_APB3FZ1, DBGMCU_APB4FZ1, DBG_IWDG1, DBG_WWDG1

cli = importlib.import_module("gwprov.cli.main")


class MemoryBackend:
    def __init__(self, registers=None):
        self.registers = dict(registers or {})
        self.writes = []
        self.opened = False
        self.closed = False

    def open(self):
        self.opened = True

    def close(self):
        self.closed = True

    def read_memory(self, address, size):
        return self.registers[address].to_bytes(size, "little")

    def write_memory(self, address, data):
        value = int.from_bytes(data, "little")
        self.writes.append((address, value))
        self.registers[address] = value


@pytest.mark.parametrize("transport", ("hardware", "remote-gdb"))
def test_watchdog_status_reports_halt_freeze_not_runtime_disable(transport):
    backend = MemoryBackend({DBGMCU_APB3FZ1: DBG_WWDG1, DBGMCU_APB4FZ1: 0})
    status = DebugSession(backend, transport, target_profile="stm32h7b0").watchdog_status()

    assert status["runtime_control"]["supported"] is False
    assert "cannot be stopped" in status["runtime_control"]["detail"]
    assert status["halt_freeze"]["supported"] is True
    assert status["halt_freeze"]["WWDG1"]["enabled"] is True
    assert status["halt_freeze"]["IWDG1"]["enabled"] is False


def test_watchdog_status_reports_readable_runtime_state_and_marks_iwdg_unknown():
    from gwprov.target_controls import WATCHDOG_PROFILES

    wwdg_cr = WATCHDOG_PROFILES["stm32h7b0"].runtime_state_registers[0][1]
    backend = MemoryBackend(
        {
            DBGMCU_APB3FZ1: 0,
            DBGMCU_APB4FZ1: 0,
            wwdg_cr: 1 << 7,
        }
    )
    session = DebugSession(backend, "remote-gdb", target_profile="stm32h7b0")

    status = session.watchdog_status()
    assert status["runtime_control"]["state"] == {"IWDG1": None, "WWDG1": True}
    assert "IWDG1 exposes no readable started bit" in status["runtime_control"]["state_detail"]

    backend.registers[wwdg_cr] = 0
    assert session.watchdog_status()["runtime_control"]["state"]["WWDG1"] is False


def test_watchdog_freeze_can_be_enabled_and_disabled_preserving_other_bits():
    backend = MemoryBackend({DBGMCU_APB3FZ1: 0x10, DBGMCU_APB4FZ1: 0x20})
    session = DebugSession(backend, "hardware", target_profile="stm32h7b0")

    enabled = session.set_watchdog_freeze(True)
    disabled = session.set_watchdog_freeze(False)

    assert enabled["halt_freeze"]["WWDG1"]["enabled"] is True
    assert enabled["halt_freeze"]["IWDG1"]["enabled"] is True
    assert disabled["halt_freeze"]["WWDG1"]["enabled"] is False
    assert disabled["halt_freeze"]["IWDG1"]["enabled"] is False
    assert backend.registers == {DBGMCU_APB3FZ1: 0x10, DBGMCU_APB4FZ1: 0x20}


def test_read_only_backend_reports_freeze_status_but_rejects_toggle():
    class ReadOnlyBackend:
        def read_memory(self, address, size):
            return (DBG_WWDG1 if address == DBGMCU_APB3FZ1 else 0).to_bytes(size, "little")

    session = DebugSession(ReadOnlyBackend(), "remote-gdb", target_profile="stm32h7b0")
    status = session.watchdog_status()

    assert status["halt_freeze"]["supported"] is False
    assert status["halt_freeze"]["status"] == "read-only"
    assert status["halt_freeze"]["WWDG1"]["enabled"] is True
    with pytest.raises(ValueError, match="cannot write"):
        session.set_watchdog_freeze(False)


def test_gwemu_watchdogs_report_not_modeled_and_do_not_claim_control():
    status = DebugSession(None, "gwemu").watchdog_status()

    assert status["runtime_control"]["status"] == "not-modeled"
    assert status["halt_freeze"]["status"] == "not-modeled"
    with pytest.raises(ValueError, match="does not model"):
        DebugSession(None, "gwemu").set_watchdog_freeze(True)


def test_device_watchdog_cli_reports_gwemu_without_opening_a_backend(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "_resolve_device_selector", lambda _: {"id": "gwemu:123", "kind": "gwemu"}
    )
    args = SimpleNamespace(device="gwemu:123", watchdog_action="status", output="text")

    assert cli._device_watchdog(args) == 0
    output = capsys.readouterr().out
    assert "not-modeled" in output
    assert "register-only stubs" in output


def test_device_watchdog_cli_opens_backend_and_disables_only_halt_freeze(monkeypatch, capsys):
    backend = MemoryBackend({DBGMCU_APB3FZ1: DBG_WWDG1 | 0x10, DBGMCU_APB4FZ1: DBG_IWDG1 | 0x20})
    monkeypatch.setattr(
        cli,
        "_resolve_device_selector",
        lambda _: {
            "id": "probe:unit-a",
            "kind": "hardware",
            "probeId": "unit-a",
            "displayName": "Unit A",
            "targetProfile": "stm32h7b0",
        },
    )

    class BackendFactory:
        def __init__(self, probe_id, **kwargs):
            assert probe_id == "unit-a"

        def open(self):
            backend.open()

        def close(self):
            backend.close()

        def read_memory(self, address, size):
            return backend.read_memory(address, size)

        def write_memory(self, address, data):
            backend.write_memory(address, data)

    monkeypatch.setattr("gwprov.backends.SelectedPyOCDBackend", BackendFactory)
    args = SimpleNamespace(
        device="Unit A", watchdog_action="freeze", state="disable", output="text"
    )

    assert cli._device_watchdog(args) == 0
    output = capsys.readouterr().out
    assert backend.opened and backend.closed
    assert backend.registers == {DBGMCU_APB3FZ1: 0x10, DBGMCU_APB4FZ1: 0x20}
    assert "Runtime watchdog control: not-controllable" in output
    assert "WWDG1: disabled" in output
    assert "IWDG1: disabled" in output


def test_device_watchdog_cli_uses_registered_remote_gdb_endpoint(monkeypatch, capsys):
    backend = MemoryBackend({DBGMCU_APB3FZ1: 0, DBGMCU_APB4FZ1: 0})
    monkeypatch.setattr(
        cli,
        "_resolve_device_selector",
        lambda _: {
            "id": "gdb:10.2.3.122:3333",
            "kind": "remote-gdb",
            "remoteGdb": True,
            "gdbHost": "10.2.3.122",
            "gdbPort": 3333,
            "name": "rpi400-hw",
            "targetProfile": "stm32h7b0",
        },
    )

    class RemoteBackendFactory:
        def __init__(self, host, port, **kwargs):
            assert (host, port) == ("10.2.3.122", 3333)

        def open(self):
            backend.open()

        def close(self):
            backend.close()

        def read_memory(self, address, size):
            return backend.read_memory(address, size)

        def write_memory(self, address, data):
            backend.write_memory(address, data)

    monkeypatch.setattr("gwprov.debug_shell.RemoteGDBBackend", RemoteBackendFactory)
    args = SimpleNamespace(device="rpi400-hw", watchdog_action="status", output="json")

    assert cli._device_watchdog(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert backend.opened and backend.closed
    assert report["target"] == "STM32H7B0"
    assert report["halt_freeze"]["supported"] is True
    assert report["runtime_control"]["supported"] is False


def test_device_watchdog_command_normalizes_and_help_explains_freeze_scope(capsys):
    parser = cli.build_parser()
    normalized = cli._normalize_device_object_form(
        ["device", "unit-a", "watchdog", "freeze", "enable"]
    )
    args = parser.parse_args(normalized)
    assert args.device == "unit-a"
    assert args.watchdog_action == "freeze"
    assert args.state == "enable"
    with pytest.raises(SystemExit):
        parser.parse_args(["device", "watchdog", "unit-a", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "pauses watchdogs only while the CPU is halted" in help_text


def test_watchdog_status_json_is_structured(capsys):
    status = DebugSession(
        MemoryBackend({DBGMCU_APB3FZ1: 0, DBGMCU_APB4FZ1: 0}),
        "hardware",
        target_profile="stm32h7b0",
    ).watchdog_status()
    cli._print_watchdog_status("probe:unit-a", status, "json")

    report = json.loads(capsys.readouterr().out)
    assert report["device"] == "probe:unit-a"
    assert report["runtime_control"]["status"] == "not-controllable"


def test_unknown_target_profile_does_not_probe_memory_and_is_actionable():
    backend = MemoryBackend({DBGMCU_APB3FZ1: DBG_WWDG1, DBGMCU_APB4FZ1: 0})
    session = DebugSession(backend, "remote-gdb")

    status = session.watchdog_status()

    assert status["halt_freeze"]["status"] == "unsupported"
    assert "did not probe target MMIO" in status["halt_freeze"]["detail"]
    assert backend.writes == []
    with pytest.raises(ValueError, match="did not probe target MMIO"):
        session.set_watchdog_freeze(False)


def test_control_registry_is_extensible_and_checks_each_action(monkeypatch):
    from gwprov import target_controls
    from gwprov.target_controls import register_target_control

    class TestControl:
        name = "synthetic"
        facet_names = ("runtime",)

        def __init__(self):
            self.value = False

        def status(self, session, target_profile):
            facet = {
                "supported": True,
                "status": "partially-available",
                "enabled": self.value,
                "detail": "Synthetic test control.",
                "actions": {
                    "enable": {"supported": True, "detail": "Supported."},
                    "disable": {"supported": False, "detail": "Disable is unsupported."},
                },
            }
            return {
                "name": self.name,
                "target": "TEST TARGET",
                "supported": True,
                "facets": {"runtime": facet},
            }

        def set(self, session, facet, enabled, target_profile):
            assert facet == "runtime"
            self.value = enabled
            return self.status(session, target_profile)

    handler = TestControl()
    monkeypatch.setitem(target_controls._CONTROL_REGISTRY, "gwprov-test-target", {})
    register_target_control("gwprov-test-target", handler)
    session = DebugSession(MemoryBackend(), "remote-gdb", target_profile="gwprov-test-target")

    assert (
        session.control_status()["controls"]["synthetic"]["facets"]["runtime"]["enabled"] is False
    )
    enabled = session.set_control("synthetic", "runtime", True)
    assert enabled["facets"]["runtime"]["enabled"] is True
    with pytest.raises(ValueError, match="Disable is unsupported"):
        session.set_control("synthetic", "runtime", False)
    assert handler.value is True


def test_registered_gwemu_control_is_not_blocked_by_transport(monkeypatch, capsys):
    from gwprov import target_controls
    from gwprov.target_controls import register_target_control

    class ModeledWatchdog:
        name = "watchdog"
        facet_names = ("runtime",)

        def __init__(self):
            self.enabled = True

        def status(self, session, target_profile):
            facet = {
                "supported": True,
                "status": "available",
                "enabled": self.enabled,
                "detail": "Modeled watchdog runtime control.",
                "actions": {
                    action: {"supported": True, "status": "available", "detail": "Supported."}
                    for action in ("enable", "disable")
                },
            }
            return {
                "name": self.name,
                "target": "GWemu",
                "supported": True,
                "facets": {"runtime": facet},
                "runtime_control": facet,
                "halt_freeze": None,
            }

        def set(self, session, facet, enabled, target_profile):
            assert facet == "runtime"
            self.enabled = enabled
            return self.status(session, target_profile)

    handler = ModeledWatchdog()
    monkeypatch.setitem(target_controls._CONTROL_REGISTRY, "gwemu", {})
    register_target_control("gwemu", handler)
    session = DebugSession(None, "gwemu")

    assert session.control_status("watchdog")["runtime_control"]["enabled"] is True
    monkeypatch.setattr(
        cli,
        "_resolve_device_selector",
        lambda _: {"id": "gwemu:123", "kind": "gwemu"},
    )
    args = SimpleNamespace(
        device="gwemu:123",
        watchdog_action="runtime",
        control_name="watchdog",
        facet="runtime",
        state="disable",
        output="text",
    )
    assert cli._device_watchdog(args) == 0
    assert handler.enabled is False
    assert capsys.readouterr().out == ""


def test_non_h7_watchdog_runtime_disable_routes_through_managed_device_selector(
    monkeypatch, capsys
):
    from gwprov import target_controls
    from gwprov.target_controls import register_target_control

    class SyntheticWatchdog:
        name = "watchdog"
        facet_names = ("runtime",)

        def __init__(self):
            self.enabled = True
            self.calls = []

        def status(self, session, target_profile):
            facet = {
                "supported": True,
                "status": "available",
                "enabled": self.enabled,
                "detail": "Synthetic target supports runtime watchdog control.",
                "actions": {
                    action: {
                        "supported": True,
                        "status": "available",
                        "detail": "Supported by this target profile.",
                    }
                    for action in ("enable", "disable")
                },
            }
            return {
                "name": self.name,
                "target": "Synthetic MCU",
                "supported": True,
                "facets": {"runtime": facet},
                "runtime_control": facet,
                "halt_freeze": None,
            }

        def set(self, session, facet, enabled, target_profile):
            assert facet == "runtime"
            assert target_profile == "synthetic-mcu"
            assert session.backend.opened
            self.enabled = enabled
            self.calls.append((target_profile, enabled))
            return self.status(session, target_profile)

    class RemoteBackend:
        def __init__(self, host, port, **kwargs):
            assert (host, port) == ("synthetic.example.invalid", 3333)
            self.opened = False
            self.closed = False

        def open(self):
            self.opened = True

        def close(self):
            self.closed = True

    handler = SyntheticWatchdog()
    monkeypatch.setitem(target_controls._CONTROL_REGISTRY, "synthetic-mcu", {})
    register_target_control("synthetic-mcu", handler)
    backend = RemoteBackend("synthetic.example.invalid", 3333)

    def backend_factory(host, port, **kwargs):
        assert (host, port) == ("synthetic.example.invalid", 3333)
        return backend

    monkeypatch.setattr(
        cli,
        "_resolve_device_selector",
        lambda _: {
            "id": "gdb:synthetic.example.invalid:3333",
            "kind": "remote-gdb",
            "remoteGdb": True,
            "gdbHost": "synthetic.example.invalid",
            "gdbPort": 3333,
            "gdbUrl": "gdb://synthetic.example.invalid:3333",
            "targetProfile": "synthetic-mcu",
        },
    )
    monkeypatch.setattr("gwprov.debug_shell.RemoteGDBBackend", backend_factory)
    args = SimpleNamespace(
        device="synthetic-device",
        watchdog_action="runtime",
        control_name="watchdog",
        facet="runtime",
        state="disable",
        output="text",
    )

    assert cli._device_watchdog(args) == 0
    assert handler.enabled is False
    assert handler.calls == [("synthetic-mcu", False)]
    assert backend.closed is True
    output = capsys.readouterr().out
    assert "Synthetic MCU" in output
    assert "runtime: available" in output
    assert "state: disabled" in output


def test_generic_control_cli_parses_named_facets_and_listing(capsys):
    parser = cli.build_parser()
    normalized = cli._normalize_device_object_form(
        ["device", "unit-a", "control", "watchdog", "set", "halt_freeze", "disable"]
    )
    set_args = parser.parse_args(normalized)
    assert set_args.device == "unit-a"
    assert set_args.control_name == "watchdog"
    assert set_args.watchdog_action == "set"
    assert set_args.facet == "halt_freeze"
    assert set_args.state == "disable"

    listed = DebugSession(None, "gwemu").control_status()
    cli._print_target_control_status("gwemu:123", listed, "text")
    output = capsys.readouterr().out
    assert "Control: watchdog" in output
    assert "not-modeled" in output


def test_watchdog_runtime_subcommand_is_explicit_and_never_means_freeze(capsys):
    parser = cli.build_parser()
    normalized = cli._normalize_device_object_form(
        ["device", "unit-a", "watchdog", "runtime", "disable"]
    )
    args = parser.parse_args(normalized)
    assert args.watchdog_action == "runtime"
    assert args.control_name == "watchdog"
    assert args.facet == "runtime"
    assert args.state == "disable"

    with pytest.raises(SystemExit):
        parser.parse_args(["device", "watchdog", "--help"])
    watchdog_help = capsys.readouterr().out
    assert "watchdog {status,freeze,runtime}" in watchdog_help

    normalized = cli._normalize_device_object_form(
        ["device", "unit-a", "watchdog", "runtime", "--help"]
    )
    with pytest.raises(SystemExit):
        parser.parse_args(normalized)
    runtime_help = capsys.readouterr().out
    assert "Usage: gwprov device DEVICE watchdog runtime STATE" in runtime_help
    assert "enable" in runtime_help and "disable" in runtime_help

    backend = MemoryBackend({DBGMCU_APB3FZ1: DBG_WWDG1, DBGMCU_APB4FZ1: DBG_IWDG1})
    session = DebugSession(backend, "hardware", target_profile="stm32h7b0")
    status = session.control_status("watchdog")
    actions = status["facets"]["runtime"]["actions"]
    assert actions["enable"]["supported"] is False
    assert actions["disable"]["supported"] is False
    with pytest.raises(ValueError, match="cannot be stopped"):
        session.set_control("watchdog", "runtime", False)
    with pytest.raises(ValueError, match="irreversible until reset"):
        session.set_control("watchdog", "runtime", True)
    assert backend.writes == []


def test_python_shell_propagates_registered_gdb_target_profile(monkeypatch):
    import gwprov.debug_shell as debug_shell

    captured = {}

    class Backend:
        def __init__(self, host, port, *, url, operation, target_profile):
            self.target_profile = target_profile

        def open(self):
            return self

        def close(self):
            pass

        def read_memory(self, address, size):
            return bytes(size)

        def write_memory(self, address, data):
            pass

    monkeypatch.setattr(debug_shell, "RemoteGDBBackend", Backend)
    monkeypatch.setattr(
        debug_shell.code,
        "interact",
        lambda **kwargs: captured.update(kwargs["local"]),
    )

    debug_shell.python_shell(
        target="hardware",
        remote_gdb_endpoints=[
            {
                "name": "registered",
                "host": "debug-target.example.invalid",
                "port": 3333,
                "url": "gdb://debug-target.example.invalid:3333",
                "targetProfile": "stm32h7b0",
            }
        ],
        retro_go_version="unmatched",
        watchdog_freeze=False,
    )

    assert captured["dbg"].target_profile == "stm32h7b0"
    assert captured["dbg"].watchdog_status()["target"] == "STM32H7B0"


def test_watchdog_status_available_for_unrecognized_managed_device_kind(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "_resolve_device_selector",
        lambda _: {"id": "other:1", "kind": "other-backend"},
    )
    args = SimpleNamespace(device="other:1", watchdog_action="status", output="json")

    assert cli._device_watchdog(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["halt_freeze"]["status"] == "unsupported"
