import json

import pytest

from gwprov.reports import render_report


def test_html_report_is_self_contained(tmp_path):
    source = tmp_path / "profile.json"
    source.write_text(json.dumps({"title": "Hardware <profile>", "functions": [
        {"name": "render", "cycles": 123}], "target": "hardware"}))
    output = render_report(source, tmp_path / "profile.html", format="html")
    page = output.read_text()
    assert page.startswith("<!doctype html>")
    assert "Hardware &lt;profile&gt;" in page
    assert "https://" not in page
    assert "render" in page


def test_pdf_report_explains_optional_dependency(tmp_path):
    source = tmp_path / "report.json"
    source.write_text('{"target":"hardware"}')
    try:
        import reportlab  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="gwprov\\[reports\\]"):
            render_report(source, tmp_path / "report.pdf", format="pdf")
    else:
        assert render_report(source, tmp_path / "report.pdf", format="pdf").is_file()


def test_hardware_profile_default_html_keeps_json_report(monkeypatch, tmp_path):
    from gwprov import hw_profile

    monkeypatch.chdir(tmp_path)

    class Symbols:
        def __getitem__(self, name):
            assert name == "gwprov_trace_header"
            return 0x24000000

        def rebase_from_runtime_pointers(self, _read_memory):
            return {}

        def nearest(self, _pc):
            return None

    class Backend:
        def __init__(self, *_args, **_kwargs):
            pass

        def open(self):
            return self

        @property
        def probe_name(self):
            return "test probe"

        def read_memory(self, _address, size):
            return bytes(size)

        def close(self):
            pass

    snapshot = {"sequence": 0, "dropped": 0, "capacity": 1, "events": [],
                "missing": 0, "overwritten_since_sequence": 0,
                "flags": 1, "counter_hz": 280_000_000}
    monkeypatch.setattr(hw_profile, "_symbols", lambda *_args: Symbols())
    monkeypatch.setattr(hw_profile, "read_trace", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr("gwprov.backends.SelectedOpenOCDBackend", Backend)

    assert hw_profile.profile_hardware(programmer="stlink", duration=0.001,
                                       interval=0.001, output_format="html") == 2
    directory = tmp_path / "dev-local" / "reports"
    json_reports = list(directory.glob("hardware-profile-*.json"))
    html_reports = list(directory.glob("hardware-profile-*.html"))
    assert len(json_reports) == len(html_reports) == 1
    assert json.loads(json_reports[0].read_text())["measurement"] == "project GWProv trace ABI v1"
    assert "project GWProv trace ABI v1" in html_reports[0].read_text()
