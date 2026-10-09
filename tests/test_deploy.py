import json

import pytest

from gwprov.deploy import deployment_plan


def make_profile(root):
    root.mkdir()
    (root / "bank1.bin").write_bytes(b"bootloader")
    (root / "bank2.bin").write_bytes(b"retro-go")
    (root / "extflash.bin").write_bytes(bytes(range(32)))
    (root / "profile.toml").write_text(
        '[flash]\nbank1 = "bank1.bin"\nbank2 = "bank2.bin"\nextflash = "extflash.bin"\n'
        '[sd]\nmode = "none"\n')
    (root / "provision.json").write_text(json.dumps({"layout": {
        "extflashBytes": 32, "frogfsBytes": 8,
        "littlefsOffset": 24, "littlefsBytes": 8,
    }}))


def test_deployment_plan_contains_both_internal_banks_and_offset_filesystems(tmp_path):
    root = tmp_path / "profile"
    make_profile(root)

    plan = deployment_plan(root)

    assert [(row["region"], row["bank"], row["offset"], row["bytes"])
            for row in plan["regions"]] == [
                ("bank1", 1, 0, 10), ("bank2", 2, 0, 8),
                ("frogfs", 0, 0, 8), ("littlefs", 0, 24, 8)]
    assert plan["bootAfterDeploy"] == "bank1 reset vector"
    assert all(len(row["sha256"]) == 64 for row in plan["regions"])


def test_deployment_plan_can_select_bank1_alone(tmp_path):
    root = tmp_path / "profile"
    make_profile(root)
    plan = deployment_plan(root, ["bank1"])
    assert [row["region"] for row in plan["regions"]] == ["bank1"]


def test_deployment_plan_rejects_absent_region(tmp_path):
    root = tmp_path / "profile"
    make_profile(root)
    with pytest.raises(ValueError, match="no deployable region"):
        deployment_plan(root, ["extflash"])


def test_apply_deployment_writes_exact_regions_then_starts_bank1(tmp_path, monkeypatch):
    from gwprov import deploy

    bank1 = tmp_path / "bank1.bin"
    extflash = tmp_path / "extflash.bin"
    bank1.write_bytes(b"BANK1")
    extflash.write_bytes(b"0123456789")
    plan = {"profile": "fake", "regions": [
        {"region": "bank1", "kind": "internal-flash", "bank": 1,
         "offset": 0, "bytes": 5, "path": str(bank1), "sha256": "a"},
        {"region": "littlefs", "kind": "external-flash", "bank": 0,
         "offset": 4, "bytes": 4, "path": str(extflash), "sha256": "b"},
    ], "bootAfterDeploy": "bank1 reset vector"}
    monkeypatch.setattr(deploy, "deployment_plan", lambda *_args, **_kwargs: plan)

    calls = []

    class Backend:
        def __init__(self, *_args, **_kwargs):
            pass

        def open(self):
            calls.append("open")

        def close(self):
            calls.append("close")

        def reset_and_halt(self):
            calls.append("reset_and_halt")

        def read_uint32(self, address):
            calls.append(("read", address))
            return {0x08000000: 0x24010000, 0x08000004: 0x08001235}[address]

        def write_register(self, name, value):
            calls.append(("register", name, value))

        def resume(self):
            calls.append("resume")

    class FakeGnW:
        def __init__(self, backend):
            self.backend = backend

        def start_gnwmanager(self):
            calls.append("start_gnwmanager")

        def flash(self, bank, offset, data):
            calls.append(("flash", bank, offset, data))

    from gnwmanager import gnw
    monkeypatch.setattr(gnw, "GnW", FakeGnW)
    monkeypatch.setattr("gwprov.backends.SelectedOpenOCDBackend", Backend)

    result = deploy.apply_deployment("fake", programmer="stlink")
    assert calls == [
        "open", "start_gnwmanager",
        ("flash", 1, 0, b"BANK1"), ("flash", 0, 4, b"4567"),
        "reset_and_halt", ("read", 0x08000000),
        ("register", "msp", 0x24010000), ("read", 0x08000004),
        ("register", "pc", 0x08001235), "resume", "close",
    ]
    assert result["booted"] == "bank1"
