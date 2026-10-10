"""Observed pauses must split freeze and routine-residency evidence."""
import unittest
from unittest.mock import patch
from gwprov.profiling import StateTimeline, no_progress_segments, sample_profile


class ProfileEpochs(unittest.TestCase):
    def test_brief_halt_splits_plateau_between_counter_reads(self):
        timeline = StateTimeline()
        for timestamp, running in ((0, True), (.45, False), (.55, True), (1, True)):
            timeline.observe(timestamp, 'running' if running else 'paused', running)
        self.assertEqual(timeline.epoch, 2)
        summary = timeline.summary(1)
        self.assertAlmostEqual(summary['observed_stopped_seconds'], .1)
        self.assertAlmostEqual(summary['observed_running_seconds'], .9)
        counters = [{'elapsed': t, 'running_epoch': epoch, 'values': {'frames': 5}}
                    for t, epoch in ((0, 1), (1, 2), (2, 2))]
        spans = no_progress_segments(counters, 'frames', 0xffffffff, 2.5)
        self.assertEqual([[x['elapsed'] for x in span] for span in spans], [[1, 2]])

    def test_gap_and_counter_change_split_plateaus(self):
        counters = [{'elapsed': t, 'running_epoch': 1, 'values': {'frames': value}}
                    for t, value in ((0, 1), (.1, 1), (.2, 2), (.3, 2), (2, 2))]
        self.assertEqual(len(no_progress_segments(counters, 'frames', 0xff, .3)), 2)

    def test_profile_same_function_across_pause_is_not_one_invocation(self):
        clock = [0.0]
        class Symbols:
            def nm(self): return [{'name': 'frames', 'type': 'STT_OBJECT', 'size': 4}]
            def __getitem__(self, name): return 0x20000000
            def functions(self):
                return [{'name': 'renderer', 'type': 'STT_FUNC', 'size': 32,
                         'address': 0x1000, 'elf': 'fake'}]
            def rebase_from_runtime_pointers(self, reader): return {}
            def source_locations(self, addresses): return []
        class QMP:
            def __init__(self, *args): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, command):
                running = not .22 <= clock[0] < .34
                return {'return': {'running': running, 'status': 'running' if running else 'paused'}}
            def registers(self): return {'pc': 0x1000, 'sp': 0x20001000, 'lr': 0x1101}
            def read_memory(self, address, size): return bytes(size)
        def sleep(seconds): clock[0] += seconds
        with patch('gwprov.profiling.QMPConnection', QMP), \
             patch('gwprov.profiling.time.monotonic', side_effect=lambda: clock[0]), \
             patch('gwprov.profiling.time.sleep', side_effect=sleep):
            report = sample_profile('fake', Symbols(), duration=.7, interval=.03,
                                    progress_symbols=['frames'], progress_interval=.1,
                                    stall_threshold=.05)
        self.assertEqual(len(report['state_timeline']['transitions']), 3)
        self.assertEqual({item['running_epoch'] for item in report['routine_residency']}, {1, 2})
        for item in report['routine_residency']:
            self.assertFalse(item['single_invocation_proven'])
            self.assertEqual(item['distinct_stack_contexts'], 1)
            self.assertFalse(item['start_elapsed'] < .22 and item['end_elapsed'] >= .34)
        for item in report['progress_stalls']:
            self.assertFalse(item['start_elapsed'] < .22 and item['end_elapsed'] >= .34)
        self.assertGreater(report['state_timeline']['observed_stopped_seconds'], 0)
        self.assertLess(report['progress']['frames']['observed_running_span_seconds'], .7)

    def test_interval_rejects_invalid_values_before_transport(self):
        for value in (0, -.1, .004, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'progress interval'):
                sample_profile('unused', None, progress_interval=value)
