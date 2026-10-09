from pathlib import Path

import pytest

from gwprov.gwemu_manager import _application_state_from_target


class FakeSymbols:
    def __init__(self, current, callers=()):
        self.current = current
        self.callers = callers

    def nearest(self, _pc):
        return self.current

    def unwind(self, _registers, _read_memory, _limit):
        return {"frames": [{"symbol": row} for row in self.callers]}

    def nm(self, *_args):
        return []


def classify(current, callers=(), monkeypatch=None):
    if monkeypatch:
        monkeypatch.setattr("gwprov.gwemu_manager._dwarf_struct_members", lambda *_args: {})
        monkeypatch.setattr("gwprov.gwemu_manager._dwarf_typedef_members", lambda *_args: {})
    return _application_state_from_target(
        FakeSymbols(current, callers), Path("retro-go.elf"), [],
        lambda _address, size: bytes(size), {"pc": 0x1000})


@pytest.mark.parametrize(("function", "expected"), [
    ("run_gwhb_homebrew", "Starting"),
    ("_startup_init", "Initializing"),
    ("app_main", "Running"),
    ("odroid_overlay_game_menu", "Game menu"),
    ("odroid_overlay_game_settings_menu", "Pause/settings menu"),
    ("handle_time_menu", "Time settings menu"),
    ("odroid_overlay_settings_menu", "Settings menu"),
])
def test_retro_go_application_state_classification(function, expected, monkeypatch):
    current = {"name": function, "elf": "retro-go.elf"}
    assert classify(current, monkeypatch=monkeypatch) == expected


@pytest.mark.parametrize(("callers", "expected"), [
    ([{"name": "odroid_overlay_dialog"}, {"name": "gui_loop"}],
     "Overlay open in picker"),
    ([{"name": "odroid_overlay_dialog"}, {"name": "game_loop"}],
     "Overlay open in game"),
])
def test_dialog_overlay_context_is_reported(callers, expected, monkeypatch):
    current = {"name": "dialog_wait", "elf": "retro-go.elf"}
    assert classify(current, callers, monkeypatch) == expected


def test_homebrew_loader_stack_reports_starting(monkeypatch):
    current = {"name": "load_gnw_segments", "elf": "retro-go.elf"}
    callers = [{"name": "run_gwhb_homebrew", "elf": "retro-go.elf"}]
    assert classify(current, callers, monkeypatch) == "Starting"
