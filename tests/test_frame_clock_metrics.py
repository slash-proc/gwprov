import unittest
from gwprov.profiling import frame_clock_metrics
class Metrics(unittest.TestCase):
 def test_rates_and_budget(self):
  r=frame_clock_metrics(frame_count=600,elapsed_cycles=600*9389953.24,excluded_wait_cycles=600*(9389953.24-5732414.925),clock_hz=280000000)
  self.assertAlmostEqual(r['paced_frame_rate'],29.8191048,places=6);self.assertAlmostEqual(r['nonwait_capacity_fps'],48.8450337,places=6);self.assertAlmostEqual(r['target_cycles_per_frame'],4666666.6667,places=3);self.assertFalse(r['hardware_accurate'])
 def test_invalid(self):
  for update in ({'frame_count':0},{'clock_hz':float('nan')},{'elapsed_cycles':True},{'excluded_wait_cycles':101},{'excluded_wait_cycles':-1}):
   values=dict(frame_count=1,elapsed_cycles=100,clock_hz=280000000);values.update(update)
   with self.assertRaises(ValueError):frame_clock_metrics(**values)
 def test_wait_only_changes_paced_not_capacity(self):
  a=frame_clock_metrics(frame_count=1,elapsed_cycles=100,excluded_wait_cycles=20,clock_hz=1000);b=frame_clock_metrics(frame_count=1,elapsed_cycles=120,excluded_wait_cycles=40,clock_hz=1000)
  self.assertEqual(a['nonwait_capacity_fps'],b['nonwait_capacity_fps']);self.assertGreater(a['paced_frame_rate'],b['paced_frame_rate'])
if __name__ == '__main__':
 unittest.main()
