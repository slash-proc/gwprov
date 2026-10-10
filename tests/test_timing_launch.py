"""Timing controls must preserve defaults and reject incompatible timelines."""
import unittest
from gwprov.launch import timing_launch_args


class TimingLaunch(unittest.TestCase):
    def test_default_does_not_change_timing(self):
        self.assertEqual(timing_launch_args(), [])

    def test_baseline_uses_zero_shift_and_explicit_soc_property(self):
        self.assertEqual(timing_launch_args(timing_mode="baseline", rtc_epoch=0), [
            "-global", "gnw-h7b0-soc.timing-mode=on", "-icount",
            "shift=0,align=off,sleep=off", "-global", "gnw-h7b0-rtc.initial-epoch=0"])

    def test_experimental_m7_separate_producer(self):
        self.assertEqual(timing_launch_args(timing_mode="experimental-m7"), [
            "-global", "gnw-h7b0-soc.timing-mode=on", "-global",
            "cortex-m7-arm-cpu.x-gnw-m7-cycle-model=on", "-icount",
            "shift=0,align=off,sleep=off"])
        with self.assertRaises(ValueError):
            timing_launch_args(timing_mode="experimental-m7", icount=1)

    def test_stale_daemon_timing_mode_rejected(self):
        from unittest.mock import patch
        from gwprov.daemon import require_timing_support
        with patch("gwprov.daemon.request", return_value={"capabilities": []}):
            with self.assertRaises(RuntimeError):
                require_timing_support("experimental-m7")
            require_timing_support("baseline")
        with patch("gwprov.daemon.request", return_value={"capabilities": ["timing-experimental-m7"]}):
            require_timing_support("experimental-m7")

    def test_default_icount_keeps_baseline_disabled(self):
        self.assertEqual(timing_launch_args(icount=4), ["-icount", "shift=4,align=off,sleep=off"])

    def test_invalid_controls_fail_before_launch(self):
        for options in ({"timing_mode": "baseline", "icount": 4},
                        {"icount": -1}, {"icount": 11}, {"rtc_epoch": -1},
                        {"timing_mode": "other"}, {"icount": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                timing_launch_args(**options)


class WideCounterSampling(unittest.TestCase):
    def test_sampling_preserves_values_above_32_bits(self):
        from unittest.mock import patch
        from gwprov.profiling import sample_profile
        class Symbols:
            def nm(self): return [{"name": "work_cycles", "type": "STT_OBJECT", "size": 8}]
            def __getitem__(self, name): return 0x20000000
            def functions(self): return []
            def rebase_from_runtime_pointers(self, reader): return {}
            def source_locations(self, addresses): return []
        class QMP:
            value = (1 << 40)
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, command): return {"return": {"running": True, "status": "running"}}
            def registers(self): return {"pc": 0, "sp": 0, "lr": 0}
            def read_memory(self, address, size):
                if address == 0x20000000:
                    if size != 8: raise AssertionError("wide counter was truncated")
                    QMP.value += (1 << 33)
                    return QMP.value.to_bytes(size, "little")
                return (1).to_bytes(4, "little") + bytes(4)
        clock = [0.0]
        def advance(seconds):
            clock[0] += seconds
        with patch("gwprov.profiling.QMPConnection", QMP), \
             patch("gwprov.profiling.time.monotonic", side_effect=lambda: clock[0]), \
             patch("gwprov.profiling.time.sleep", side_effect=advance):
            report = sample_profile("fake", Symbols(), duration=1.3, interval=0.1,
                                    progress_symbols=["work_cycles"])
        counter = report["progress"]["work_cycles"]
        self.assertEqual(counter["width_bits"], 64)
        self.assertGreater(counter["first"], (1 << 32))
        self.assertEqual(counter["delta"], (1 << 33))


class DaemonScreenshot(unittest.TestCase):
    def test_daemon_handle_is_preserved_for_screendump(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from gwprov.gwemu_manager import screenshot_qmp
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def execute(endpoint, command, arguments):
                self.assertEqual(endpoint, "gwprov://123")
                self.assertEqual(command, "screendump")
                Path(arguments["filename"]).write_bytes(b"P6\n1 1\n255\n" + bytes((255, 0, 0)))
                return {"return": {}}
            with patch("gwprov.daemon_ipc.runtime_directory", return_value=root), \
                    patch("gwprov.gwemu_manager._qmp_execute", side_effect=execute):
                report = screenshot_qmp("gwprov://123", root / "frame.png")
            self.assertFalse(report["all_black"])
            self.assertTrue((root / "frame.png").read_bytes().startswith(b"\x89PNG"))
            self.assertEqual(list(root.glob("*.ppm")), [])


class ConditionalGauges(unittest.TestCase):
    def test_zero_and_nonmonotonic_values_are_gauges_not_progress(self):
        from unittest.mock import patch
        from gwprov.profiling import sample_profile
        class Symbols:
            def nm(self, query=""):
                return [{"name": "virtual_pc", "type": "STT_OBJECT", "size": 4}]
            def __getitem__(self, name): return 0x20000000
            def functions(self):
                return [{"name": name, "address": address, "size": 16, "type": "STT_FUNC", "elf": "fake"}
                        for name, address in (("interp_run", 0x1000), ("renderer", 0x2000))]
            def rebase_from_runtime_pointers(self, reader): return {}
            def source_locations(self, addresses): return []
        class QMP:
            visits = 0
            values = []
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, command): return {"return": {"running": True, "status": "running"}}
            def registers(self):
                QMP.visits += 1
                return {"pc": 0x2000 if QMP.visits % 4 == 0 else 0x1000, "sp": 0, "lr": 0}
            def read_memory(self, address, size):
                if address == 0x20000000:
                    if QMP.visits % 4 == 0: raise AssertionError("unmatched function sampled")
                    value = (0, 5, 1)[len(QMP.values) % 3]
                    QMP.values.append(value)
                    return value.to_bytes(4, "little")
                return (1).to_bytes(4, "little") + bytes(4)
        with patch("gwprov.profiling.QMPConnection", QMP):
            report = sample_profile("fake", Symbols(), duration=0.06, interval=0.005,
                progress_symbols=[], sample_gauges=[{"symbol": "virtual_pc", "whenFunctions": ["interp*"]}])
        gauge = report["sample_gauges"]["virtual_pc"]
        self.assertEqual(gauge["eligible_samples"], len(QMP.values))
        self.assertEqual({item["value"] for item in gauge["values"]}, {0, 5, 1})
        self.assertNotIn("virtual_pc", report["progress"])
        self.assertEqual(report["progress_stalls"], [])

    def test_missing_gauge_symbol_fails_loudly(self):
        from gwprov.profiling import sample_profile
        class Symbols:
            def nm(self, query=""): return []
            def functions(self): return []
        with self.assertRaisesRegex(ValueError, "must be a named"):
            sample_profile("fake", Symbols(), progress_symbols=[],
                           sample_gauges=[{"symbol": "missing"}])


class InlineSourceAttribution(unittest.TestCase):
    def test_innermost_functions_group_lines_without_extra_sampling(self):
        from gwprov.profiling import attribute_inline_samples
        class Symbols:
            def source_locations(self, addresses):
                self.requested = addresses
                return [{"elf": "app.elf", "frames": [
                            {"function": "inner", "file": "render.c:10 (discriminator 2)"},
                            {"function": "outer", "file": "render.c:30"}]},
                        {"elf": "app.elf", "frames": [
                            {"function": "inner", "file": "render.c:20"}]},
                        {"frames": []}]
        symbols = Symbols()
        report = attribute_inline_samples({"raw_samples": [
            {"pc": 4}, {"pc": 8}, {"pc": 4}, {"pc": 12}]}, symbols)
        self.assertEqual(symbols.requested, [4, 8, 12])
        self.assertEqual(report["inline_functions"], [{
            "elf": "app.elf", "function": "inner", "source": "render.c",
            "samples": 3, "percent": 75.0}])
        self.assertEqual(report["inline_attribution"]["coverage_percent"], 75.0)

    def test_empty_samples_have_zero_coverage(self):
        from gwprov.profiling import attribute_inline_samples
        class Symbols:
            def source_locations(self, addresses): return []
        report = attribute_inline_samples({"raw_samples": []}, Symbols())
        self.assertEqual(report["inline_functions"], [])
        self.assertEqual(report["inline_attribution"]["coverage_percent"], 0)


class ExecutableIdentity(unittest.TestCase):
    def test_explicit_executable_is_validated(self):
        import tempfile
        from pathlib import Path
        from gwprov.launch import gwemu_executable
        self.assertEqual(gwemu_executable(), "gwemu")
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "gwemu"
            binary.write_bytes(b"example")
            binary.chmod(0o600)
            with self.assertRaises(ValueError): gwemu_executable(binary)
            binary.chmod(0o700)
            self.assertEqual(gwemu_executable(binary), str(binary.resolve()))

    def test_stale_daemon_cannot_silently_ignore_override(self):
        from unittest.mock import patch
        from gwprov.daemon import require_binary_override_support
        with patch("gwprov.daemon.request", return_value={"daemonPid": 1}):
            with self.assertRaisesRegex(RuntimeError, "does not support --gwemu-bin"):
                require_binary_override_support("pinned-gwemu")
        with patch("gwprov.daemon.request", return_value={"capabilities": ["gwemu-bin"]}):
            require_binary_override_support("pinned-gwemu")

    def test_live_identity_hashes_linux_process_inode(self):
        import os
        import sys
        from gwprov.binary_identity import process_binary_identity
        if not sys.platform.startswith("linux"):
            self.skipTest("Linux live executable inode test")
        identity = process_binary_identity(os.getpid())
        actual = os.stat(f"/proc/{os.getpid()}/exe")
        self.assertEqual(identity["identitySource"], "live-process-inode")
        self.assertEqual(identity["inode"], actual.st_ino)
        self.assertEqual(identity["bytes"], actual.st_size)


class PCContext(unittest.TestCase):
    def test_exception_return_requires_register_corroboration(self):
        from gwprov.profiling import classify_pc_sample
        row = classify_pc_sample({"pc": 0xffffffe8, "lr": 0xffffffe9, "xpsr": 0x8900000f})
        self.assertEqual(row["kind"], "exception-return")
        self.assertEqual(row["exception_number"], 15)
        self.assertFalse(row["fault_diagnosis"])
        self.assertEqual(classify_pc_sample({"pc": 0xffffffe8, "lr": 0, "xpsr": 0})["kind"], "exception-return-shaped")
        self.assertEqual(classify_pc_sample({"pc": 0xdeadbeef, "lr": 0})["kind"], "unresolved-address")
