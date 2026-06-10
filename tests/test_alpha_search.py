from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from server import alpha_search as asch

class TestTrajectoryMetrics(unittest.TestCase):
    def test_empty(self):
        m = asch.trajectory_metrics([])
        self.assertEqual(m['n'], 0)
        self.assertEqual(m['best'], 0)
        self.assertEqual(m['consecutive_declines'], 0)
    def test_best_is_max(self):
        self.assertEqual(asch.trajectory_metrics([3, 7, 5])['best'], 7)
    def test_consecutive_declines_counts_trailing(self):
        self.assertEqual(asch.trajectory_metrics([1, 9, 7, 5])['consecutive_declines'], 2)
        self.assertEqual(asch.trajectory_metrics([5, 6, 7])['consecutive_declines'], 0)
    def test_diversity_zero_for_constant(self):
        self.assertEqual(asch.trajectory_metrics([4, 4, 4])['diversity'], 0.0)
    def test_diversity_positive_for_varied(self):
        self.assertGreater(asch.trajectory_metrics([1, 9, 2, 8])['diversity'], 0.0)
    def test_convergence_in_range(self):
        for s in ([1,2,3,4], [4,3,2,1], [5,5,5], [], [3]):
            c = asch.trajectory_metrics(s)['convergence']
            self.assertGreaterEqual(c, -1.0); self.assertLessEqual(c, 1.0)
    def test_never_raises(self):
        for bad in [None, ['a','b'], [float('nan')], 'x']:
            m = asch.trajectory_metrics(bad)
            self.assertIn('n', m)

class TestPickMode(unittest.TestCase):
    def test_near_miss_is_refine(self):
        self.assertEqual(asch.pick_mode([5,5], has_near_miss=True, survivor_count=0), 'REFINE')
    def test_plateau_with_survivors_recombine(self):
        self.assertEqual(asch.pick_mode([9,7,5], has_near_miss=False, survivor_count=3), 'RECOMBINE')
    def test_plateau_without_survivors_explore(self):
        self.assertEqual(asch.pick_mode([9,7,5], has_near_miss=False, survivor_count=1), 'EXPLORE')
    def test_simplify_when_too_deep(self):
        self.assertEqual(asch.pick_mode([5,6], has_near_miss=False, survivor_count=0, max_depth_seen=9), 'SIMPLIFY')
    def test_default_is_explore(self):
        self.assertEqual(asch.pick_mode([5,6,7], has_near_miss=False, survivor_count=2), 'EXPLORE')
    def test_pick_mode_never_raises(self):
        self.assertIn(asch.pick_mode(None, has_near_miss=False, survivor_count=0),
                      ('EXPLORE','REFINE','RECOMBINE','SIMPLIFY'))

if __name__ == '__main__':
    unittest.main()
