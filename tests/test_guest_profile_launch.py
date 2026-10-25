"""Guest timeline profiling launch and daemon transport."""

import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from gwprov.launch import image_launch_spec, profile_launch_spec


class GuestProfileLaunch(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="profile-launch-", dir=os.environ.get("TMPDIR", Path(__file__).parent)))
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        for name in ("bank1.bin", "bank2.bin", "extflash.bin"):
            (self.scratch / name).write_bytes(b"image")
        (self.scratch / "profile.toml").write_text(
            "version = 1\ndisplay_name = 'test'\n[flash]\n"
            "bank1 = 'bank1.bin'\nbank2 = 'bank2.bin'\nextflash = 'extflash.bin'\n",
            encoding="utf-8",
        )
        self.symbols_a = self.scratch / "a.json"
        self.symbols_b = self.scratch / "b.json"
        self.symbols_a.write_text("{}", encoding="utf-8")
        self.symbols_b.write_text("{}", encoding="utf-8")
        self.timeline = self.scratch / "input.tl"
        self.timeline.write_bytes(b"timeline")

    def test_profile_and_image_specs_set_only_explicit_profile_environment(self):
        output = self.scratch / "record.jsonl"
        inherited = {
            "GNW_PROFILE_OLD": "unexpected",
            "GNW_PROFILE_RECORD": "inherited.jsonl",
            "PATH": os.environ.get("PATH", ""),
        }
        with patch.dict(os.environ, inherited, clear=False):
            _, _, profile_env, _ = profile_launch_spec(
                self.scratch,
                headless=True,
                timeline=str(self.timeline),
                record_profile=str(output),
                profile_interval_ms=25,
                profile_symbols=[str(self.symbols_a), str(self.symbols_b)],
            )
            self.assertEqual(profile_env["GNW_PROFILE_RECORD"], str(output.resolve()))
            self.assertEqual(profile_env["GNW_PROFILE_INTERVAL_NS"], "25000000")
            self.assertEqual(
                profile_env["GNW_PROFILE_SYMBOLS"],
                os.pathsep.join((str(self.symbols_a.resolve()), str(self.symbols_b.resolve()))),
            )
            self.assertEqual(profile_env["GNW_TIMELINE"], str(self.timeline.resolve()))
            _, _, image_env, _, cleanup = image_launch_spec(
                {},
                headless=True,
                record_profile=str(output),
                profile_interval_ms=30,
                profile_symbols=[str(self.symbols_b)],
            )
            self.assertEqual(image_env["GNW_PROFILE_INTERVAL_NS"], "30000000")
            self.assertEqual(image_env["GNW_PROFILE_SYMBOLS"], str(self.symbols_b.resolve()))
            shutil.rmtree(cleanup, ignore_errors=True)
            _, _, plain_env, _, cleanup = image_launch_spec({}, headless=True)
            self.assertIsNotNone(cleanup)
            shutil.rmtree(cleanup, ignore_errors=True)
            self.assertFalse(any(key.startswith("GNW_PROFILE_") for key in plain_env))

    def test_validation_requires_recording_and_existing_manifests(self):
        with self.assertRaisesRegex(ValueError, "require --record-profile"):
            profile_launch_spec(self.scratch, profile_interval_ms=10)
        with self.assertRaisesRegex(ValueError, "1 to 60000"):
            profile_launch_spec(
                self.scratch,
                record_profile=str(self.scratch / "out"),
                profile_interval_ms=0,
            )
        with self.assertRaisesRegex(ValueError, "manifest not found"):
            profile_launch_spec(
                self.scratch,
                record_profile=str(self.scratch / "out"),
                profile_symbols=[str(self.scratch / "missing.json")],
            )
        with self.assertRaisesRegex(ValueError, "is a directory"):
            profile_launch_spec(self.scratch, record_profile=str(self.scratch))

    def test_daemon_transport_and_capability_are_explicit(self):
        from gwprov import daemon

        calls = []
        def request(message, **kwargs):
            calls.append(message)
            if message["op"] == "ping":
                return {"capabilities": ["guest-profile-recording"]}
            return {"instance": {"pid": 123}}
        with (
            patch.object(daemon, "ensure_running"),
            patch.object(daemon, "request", side_effect=request),
        ):
            daemon.start_instance(
                str(self.scratch),
                record_profile="out.jsonl",
                profile_interval_ms=10,
                profile_symbols=[str(self.symbols_a)],
                headless=True,
            )
        self.assertEqual(calls[0], {"op": "ping"})
        self.assertEqual(calls[1]["record_profile"], str(Path("out.jsonl").resolve()))
        self.assertEqual(calls[1]["profile_interval_ms"], 10)
        self.assertEqual(calls[1]["profile_symbols"], [str(self.symbols_a.resolve())])
        with patch.object(daemon, "request", return_value={"capabilities": []}):
            with self.assertRaisesRegex(RuntimeError, "does not support --record-profile"):
                daemon.require_profile_recording_support("out.jsonl")

    def test_cli_profile_options_and_interval_bounds(self):
        from gwprov.cli.main import build_parser

        parser = build_parser()
        parsed = parser.parse_args(
            [
                "start",
                "profile:demo",
                "--record-profile",
                "out.jsonl",
                "--profile-interval-ms",
                "50",
                "--profile-symbols",
                "a.json",
                "--profile-symbols",
                "b.json",
            ]
        )
        self.assertEqual(parsed.profile_interval_ms, 50)
        self.assertEqual(parsed.profile_symbols, ["a.json", "b.json"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["start", "profile:demo", "--profile-interval-ms", "60001"])


if __name__ == "__main__":
    unittest.main()
