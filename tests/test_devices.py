import pytest

from gwprov import daemon, device_assignments, devices, gwemu_manager


def _hardware_rows():
    return [
        {
            "id": "probe:probe-a",
            "kind": "hardware",
            "probeId": "probe-a",
            "name": "ST-Link A",
            "status": "running",
            "application": "Retro-Go SD v2.0.0-rc1",
        },
        {
            "id": "probe:probe-b",
            "kind": "hardware",
            "probeId": "probe-b",
            "name": "ST-Link B",
            "status": "halted",
            "application": "Unknown",
        },
    ]


def test_device_inventory_uses_daemon_observed_states(monkeypatch, tmp_path):
    monkeypatch.setenv("GWPROV_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(gwemu_manager, "instances", lambda: [])
    monkeypatch.setattr(gwemu_manager, "_process_scan_restriction", lambda: None)
    monkeypatch.setattr(daemon, "device_snapshot", lambda **kwargs: _hardware_rows())
    monkeypatch.setattr(device_assignments, "assignments", lambda: {})

    rows = devices.device_rows()

    assert [(row["probeId"], row["status"]) for row in rows] == [
        ("probe-a", "running"),
        ("probe-b", "halted"),
    ]
    assert rows[0]["application"] == "Retro-Go SD v2.0.0-rc1"


def test_device_inventory_preserves_daemon_busy_state_without_polling(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("GWPROV_CONFIG_DIR", str(tmp_path / "config"))
    owner = {"key": "probe:busy-probe", "operation": "hardware profiling", "pid": 1234}
    monkeypatch.setattr(gwemu_manager, "instances", lambda: [])
    monkeypatch.setattr(gwemu_manager, "_process_scan_restriction", lambda: None)
    monkeypatch.setattr(
        daemon,
        "device_snapshot",
        lambda **kwargs: [
            {
                "id": "probe:busy-probe",
                "kind": "hardware",
                "probeId": "busy-probe",
                "name": "ST-Link",
                "status": "busy",
                "application": "Unknown",
                "busyOwner": owner,
            }
        ],
    )
    monkeypatch.setattr(device_assignments, "assignments", lambda: {})

    rows = devices.device_rows()

    assert len(rows) == 1
    assert rows[0]["status"] == "busy"
    assert rows[0]["busyOwner"] == owner


def test_device_list_text_groups_rows_and_wraps_long_values():
    rows = [
        {
            "kind": "gwemu",
            "pid": 42,
            "status": "running",
            "display": True,
            "gdbEndpoint": "127.0.0.1:3333",
            "application": "In game",
            "profile": "/home/user/profiles/a-very-long-profile-name",
        },
        {
            "kind": "hardware",
            "status": "busy",
            "application": "Unknown",
            "name": "STM32H7B0",
            "probeId": "a-very-long-probe-identifier-value",
            "vendor": "ST",
            "detail": "Target traffic skipped during hardware profiling.",
        },
    ]

    from gwprov.cli.text import render_process_list

    rendered = render_process_list(rows, title="GWProv devices", width=60)

    assert "GWProv devices" in rendered
    assert "RUNNING" in rendered
    assert "a-very-long-profile-" in rendered and "name" in rendered
    assert "PID 42" in rendered and "3333" in rendered
    assert "H7B0" in rendered
    assert "BUSY" in rendered
    assert "Target traffic" in rendered and "profiling." in rendered
    assert all(len(line) <= 60 for line in rendered.splitlines())


def test_gwemu_ps_shows_halted_state_in_compact_text(monkeypatch, capsys):
    row = {
        "pid": 77,
        "status": "halted",
        "running": False,
        "display": True,
        "gdbEndpoint": "127.0.0.1:3333",
        "application": "Game menu",
        "profile": "/profiles/dkc",
    }
    monkeypatch.setattr(gwemu_manager, "instances", lambda: [row.copy()])
    monkeypatch.setattr(gwemu_manager, "_application_state", lambda instance: "Game menu")

    assert gwemu_manager.show_instances() == 0
    output = capsys.readouterr().out
    assert "GWemu instances" in output
    assert "HALTED" in output and "dkc" in output
    assert "PID 77" in output and "3333" in output
    assert "Game menu" in output


def test_device_detail_reports_shared_gwemu_firmware_and_runtime_fields(tmp_path):
    import json
    import time

    from gwprov.cli.text import device_detail_data, render_device_detail

    profile = tmp_path / "zelda-rg"
    profile.mkdir()
    (profile / "profile.toml").write_text(
        '[flash]\nbank1 = "bank1.bin"\nbank2 = "bank2.bin"\nextflash = "extflash.bin"\n'
        '[sd]\nmode = "none"\n',
        encoding="utf-8",
    )
    (profile / "provision.json").write_text(json.dumps({"locked": False}), encoding="utf-8")
    (profile / "flash-map.yaml").write_text(
        "bank1:\n"
        "- name: Stock Zelda (+patch)\n  file: bank1.bin\n  offset: 0\n  size: 100\n"
        "- name: SD bootloader\n  file: boot.bin\n  offset: 200\n  size: 20\n"
        "bank2:\n"
        "- name: Retro-Go SD v2.0.0-rc1\n  file: bank2.bin\n  offset: 0\n  size: 200\n",
        encoding="utf-8",
    )
    (profile / "extflash.bin").write_bytes(b"\xff" * 1024)
    row = {
        "id": "gwemu:42",
        "kind": "gwemu",
        "displayName": "timing-test",
        "profile": str(profile),
        "status": "halted",
        "running": False,
        "qmpStatus": "paused",
        "application": "Retro-Go SD v2.0.0-rc1",
        "polledAt": time.time(),
        "pid": 42,
        "display": "headless",
        "qmpSocket": "gwprov://42",
        "qmpTransport": "daemon-stdio",
        "gdbEndpoint": "127.0.0.1:1235",
        "timing": {"mode": "experimental-m7", "pacing": "unthrottled"},
        "binaryIdentity": {"version": "GWemu 1.2.3", "commit": "abc1234"},
    }

    data = device_detail_data(row)

    assert data["summary"]["device"] == "timing-test"
    assert data["summary"]["state"] == "Paused"
    assert data["firmware"] == {
        "bootloader": "Bank 1 (dual-boot)",
        "bank1": "Stock Zelda (+patch)",
        "bank2": "Retro-Go SD v2.0.0-rc1",
        "running": "Retro-Go SD v2.0.0-rc1",
        "lock_state": "Unlocked (profile)",
    }
    assert data["details"]["timing_accuracy"] == "Full device timing (experimental WIP)"
    assert data["details"]["pacing"] == "No pacing"
    assert data["details"]["qmp"] == "GWProv daemon"
    assert data["details"]["gwemu_build"] == "GWemu 1.2.3 (abc1234)"
    assert all(len(line) <= 48 for line in render_device_detail(row, width=48).splitlines())


def test_hardware_detail_does_not_report_assigned_profile_as_detected_firmware():
    from gwprov.cli.text import device_detail_data

    data = device_detail_data(
        {
            "id": "probe:probe-a",
            "kind": "hardware",
            "displayName": "desk-device",
            "assignedProfile": "/profiles/zelda-rg",
            "status": "unknown",
            "application": "Unknown",
            "backend": "pyocd",
            "name": "ST-Link",
        }
    )

    assert data["summary"]["profile"] == "/profiles/zelda-rg"
    assert data["summary"]["state"] == "Unknown"
    assert data["firmware"]["bootloader"] == "Unknown"
    assert data["firmware"]["bank1"] == "Unknown"
    assert data["firmware"]["bank2"] == "Unknown"
    assert data["firmware"]["lock_state"] == "Unknown"


def test_hardware_detail_uses_live_bank_bootloader_and_lock_observations():
    from gwprov.cli.text import device_detail_data

    data = device_detail_data(
        {
            "id": "probe:probe-a",
            "kind": "hardware",
            "displayName": "desk-device",
            "status": "running",
            "application": "Stock Zelda",
            "backend": "pyocd",
            "name": "ST-Link",
            "runtime": {
                "locked": False,
                "banks": [
                    {"bank": 1, "image": "Stock Zelda (+patch) + Sylverb bootloader"},
                    {"bank": 2, "image": "Retro-Go SD v2.0.0-rc1"},
                ],
            },
        }
    )

    assert data["firmware"] == {
        "bootloader": "Bank 1 (dual-boot)",
        "bank1": "Zelda (patched)",
        "bank2": "Retro-Go SD v2.0.0-rc1",
        "running": "Zelda (patched)",
        "lock_state": "Unlocked",
    }


def test_ps_no_pager_and_removed_gwemu_namespace():
    from gwprov.cli.main import build_parser

    parser = build_parser()
    assert parser.parse_args(["ps", "--no-pager"]).no_pager is True
    detail = parser.parse_args(["show", "device:probe:SERIAL", "--output", "json"])
    assert detail.section == "device:probe:SERIAL"
    assert detail.output == "json"
    with pytest.raises(SystemExit):
        parser.parse_args(["gwemu", "ps", "--no-pager"])
