"""CLI-only regression tests: inventory and console are mocked; no target is opened."""

from types import SimpleNamespace

import pytest

from gwprov.cli.main import _debug_python, build_parser


@pytest.fixture
def debug_target(monkeypatch, tmp_path):
    root = tmp_path / "assigned"
    row = {
        "id": "gdb:example.invalid:3333",
        "name": "test-target",
        "kind": "hardware",
        "remoteGdb": True,
        "gdbHost": "example.invalid",
        "gdbPort": 3333,
        "gdbUrl": "gdb://example.invalid:3333",
        "assignedProfile": str(root),
        "installed": "Unknown",
    }
    captured = {}
    monkeypatch.setattr("gwprov.devices.device_rows", lambda: [row])
    monkeypatch.setattr("gwprov.debug_shell.python_shell", lambda **kw: captured.update(kw))
    return SimpleNamespace(root=root, row=row, captured=captured)


def run(*options):
    return _debug_python(build_parser().parse_args(["debug", "python", "test-target", *options]))


def test_explicit_disable_never_loads_or_discovers_profiles(debug_target, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("profile symbols must not be consulted")

    debug_target.row["installed"] = "Retro-Go SD v2.0.0-rc15"
    monkeypatch.setattr("gwprov.profiles.DeviceProfile.load", forbidden)
    monkeypatch.setattr("gwprov.debug_symbols.retro_go_profile_symbols", forbidden)
    run("--attach-only", "--no-watchdog-freeze", "--no-profile-symbols", "--symbols", "manual.elf")
    assert debug_target.captured["symbols"] == ["manual.elf"]
    assert debug_target.captured["retro_go_version"] is None
    assert debug_target.captured["profile_symbols"] is False
    assert debug_target.captured["attach_only"] is True
    assert debug_target.captured["watchdog_freeze"] is False
    assert debug_target.captured["remote_gdb_endpoints"][0]["host"] == "example.invalid"


def test_missing_assignment_allows_attach_only_with_warning(debug_target, capsys):
    run("--attach-only", "--no-watchdog-freeze")
    assert debug_target.captured["symbols"] == []
    assert debug_target.captured["attach_only"] is True
    output = capsys.readouterr()
    assert output.out == ""
    assert "Assigned profile is unavailable" in output.err
    assert str(debug_target.root) in output.err
    assert debug_target.row["assignedProfile"] == str(debug_target.root)


def test_missing_assignment_still_fails_normal_launch(debug_target):
    with pytest.raises(FileNotFoundError):
        run()
    assert not debug_target.captured


def test_explicit_missing_profile_still_fails_attach(debug_target):
    with pytest.raises(FileNotFoundError):
        run("--attach-only", "--profile", str(debug_target.root))
    assert not debug_target.captured


def test_malformed_assigned_profile_is_not_silently_ignored(debug_target):
    debug_target.root.mkdir()
    (debug_target.root / "profile.toml").write_text("[invalid")
    with pytest.raises(ValueError):
        run("--attach-only")
    assert not debug_target.captured


def test_existing_assigned_project_symbols_preserved(debug_target):
    symbols = debug_target.root / "debug" / "projects" / "project.elf"
    symbols.parent.mkdir(parents=True)
    symbols.write_bytes(b"test")
    (debug_target.root / "profile.toml").write_text("")
    run("--attach-only")
    assert debug_target.captured["symbols"] == [str(symbols)]


def test_explicit_profile_and_disable_are_mutually_exclusive():
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(
            ["debug", "python", "test-target", "--profile", "a", "--no-profile-symbols"]
        )
    assert error.value.code == 2


@pytest.mark.parametrize("version", [None, "v2.0.0-rc15"])
def test_shell_disable_covers_inferred_and_supplied_versions(monkeypatch, version):
    import gwprov.debug_shell as shell

    lookups = []
    console = {}

    class Backend:
        def __init__(self, *a, target_profile=None, **kw):
            self.target_profile = target_profile

        def open(self):
            return self

        def close(self):
            pass

    # Count rather than raise: shell catches lookup exceptions, which must not
    # allow this test to pass while still attempting automatic discovery.
    def runtime(*a, **kw):
        lookups.append("runtime")
        return {"application": "Retro-Go SD v2.0.0-rc15"}

    def profiles(*a, **kw):
        lookups.append("profiles")
        return None

    monkeypatch.setattr(shell, "RemoteGDBBackend", Backend)
    monkeypatch.setattr("gwprov.hw_lifecycle.identify_runtime", runtime)
    monkeypatch.setattr("gwprov.debug_symbols.retro_go_profile_symbols", profiles)
    monkeypatch.setattr(shell.code, "interact", lambda **kw: console.update(kw["local"]))
    shell.python_shell(
        target="hardware",
        remote_gdb_endpoints=[
            {
                "name": "test",
                "host": "example.invalid",
                "port": 3333,
                "url": "gdb://example.invalid:3333",
                "targetProfile": "stm32h7b0",
            }
        ],
        retro_go_version=version,
        profile_symbols=False,
        attach_only=True,
        watchdog_freeze=False,
    )
    assert "dbg" in console
    assert lookups == []
